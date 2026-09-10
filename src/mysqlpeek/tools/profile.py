"""Column profiling: what a schema cannot tell you about the values."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..audit import AuditLog
from ..connection import QueryError
from ..registry import InstanceRegistry
from . import READ_ONLY
from ._common import database_or_error, quote_identifier, resolve, run

_LOW_CARDINALITY = 32
_DEFAULT_SAMPLE = 100_000
_MAX_SAMPLE = 5_000_000


def register(mcp: MCPServer, registry: InstanceRegistry, audit: AuditLog) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def profile_column(
        table: str,
        column: str,
        database: str | None = None,
        sample_rows: int = _DEFAULT_SAMPLE,
        top_n: int = 10,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """Profile one column: nulls, distinct count, range and most common values.

        Answers what the schema cannot — that a VARCHAR column holds twelve distinct
        values, not four thousand, or that a "not null" column is 90% empty string.
        Reads at most `sample_rows` rows (the first ones in primary-key order), so on
        a large table the figures describe the sample and `rows_scanned` says how big
        it was.

        Args:
            table: Table name.
            column: Column to profile.
            database: Defaults to the instance's database.
            sample_rows: Rows to scan, capped at 5,000,000.
            top_n: How many of the most common values to return.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        db, err = database_or_error(handle, database)
        if err:
            return err

        # The column must exist before anything is scanned; an unknown name is a
        # question back to the caller, not a 100 000-row scan that ends in an error.
        meta, qerr = run(
            handle,
            """
            SELECT COLUMN_TYPE, DATA_TYPE FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %(db)s AND TABLE_NAME = %(tbl)s AND COLUMN_NAME = %(col)s
            """,
            parameters={"db": db, "tbl": table, "col": column},
        )
        if qerr:
            return qerr
        if meta.row_count == 0:
            return {
                "error": f"column {column!r} not found on {db}.{table}",
                "instance": handle.name,
                "hint": "call describe_table to see the column names",
            }
        column_type = str(meta.rows[0][0])
        data_type = str(meta.rows[0][1]).lower()

        try:
            qualified = f"{quote_identifier(db, 'database')}.{quote_identifier(table, 'table')}"
            col = quote_identifier(column, "column")
        except ValueError as exc:
            return {"error": str(exc), "instance": handle.name}

        sample = max(1, min(int(sample_rows), _MAX_SAMPLE))
        top = max(1, min(int(top_n), 100))
        # MIN/MAX on a JSON or geometry column is either meaningless or an error.
        orderable = data_type not in ("json", "geometry", "point", "linestring", "polygon")
        range_cols = f", MIN({col}) AS min_value, MAX({col}) AS max_value" if orderable else ""

        stats_sql = (
            f"SELECT COUNT(*) AS rows_scanned, COUNT({col}) AS non_null, "
            f"COUNT(DISTINCT {col}) AS distinct_count{range_cols} "
            f"FROM (SELECT {col} FROM {qualified} LIMIT {sample}) AS s"
        )
        top_sql = (
            f"SELECT {col} AS value, COUNT(*) AS n "
            f"FROM (SELECT {col} FROM {qualified} LIMIT {sample}) AS s "
            f"GROUP BY {col} ORDER BY n DESC LIMIT {top}"
        )
        try:
            stats = handle.connection.execute(stats_sql, measure=True)
            top_values = handle.connection.execute(top_sql)
        except QueryError as exc:
            return {"error": str(exc), "instance": handle.name}

        row = dict(zip(stats.columns, stats.rows[0], strict=True))
        scanned = int(row["rows_scanned"] or 0)
        non_null = int(row["non_null"] or 0)
        distinct = int(row["distinct_count"] or 0)

        audit.record(
            tool="profile_column",
            instance=handle.name,
            sql=stats_sql,
            query_hash="",
            allowed=True,
            row_count=scanned,
            rows_read=stats.rows_examined,
            elapsed_ms=stats.elapsed_ms,
        )

        payload: dict[str, Any] = {
            "instance": handle.name,
            "database": db,
            "table": table,
            "column": column,
            "column_type": column_type,
            "rows_scanned": scanned,
            "sampled": scanned >= sample,
            "null_count": scanned - non_null,
            "null_fraction": round((scanned - non_null) / scanned, 4) if scanned else None,
            "distinct_count": distinct,
            "top_values": [
                {"value": v, "count": int(n), "fraction": round(int(n) / scanned, 4) if scanned else None}
                for v, n in top_values.rows
            ],
            "rows_examined": stats.rows_examined,
            "elapsed_ms": stats.elapsed_ms + top_values.elapsed_ms,
        }
        if orderable:
            payload["min_value"] = row.get("min_value")
            payload["max_value"] = row.get("max_value")
        payload["assessment"] = _assess(distinct, scanned, non_null, data_type)
        return payload


def _assess(distinct: int, scanned: int, non_null: int, data_type: str) -> str:
    if scanned == 0:
        return "the table is empty (or the sample is)"
    parts: list[str] = []
    if distinct <= _LOW_CARDINALITY:
        parts.append(
            f"{distinct} distinct values: a good GROUP BY key and, if it is filtered on "
            "often, a candidate for an index (or an ENUM)"
        )
    elif non_null and distinct >= non_null * 0.9:
        parts.append(
            f"almost every row is distinct ({distinct:,} of {non_null:,}): GROUP BY on it "
            "produces one group per row, and only an equality lookup benefits from an index"
        )
    else:
        parts.append(f"{distinct:,} distinct values in {non_null:,} non-null rows")
    if non_null < scanned * 0.5:
        parts.append(f"mostly NULL ({scanned - non_null:,} of {scanned:,})")
    if data_type in ("text", "mediumtext", "longtext", "blob", "mediumblob", "longblob"):
        parts.append("a LOB type: cannot be fully indexed and is costly to GROUP BY")
    return "; ".join(parts)
