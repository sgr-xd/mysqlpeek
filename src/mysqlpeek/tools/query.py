"""Query execution. Everything here passes through the policy engine first."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..audit import AuditLog
from ..connection import QueryError
from ..registry import InstanceRegistry
from . import READ_ONLY
from ._common import blocked_response, database_or_error, quote_identifier, resolve


def register(mcp: MCPServer, registry: InstanceRegistry, audit: AuditLog) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def run_select_query(
        sql: str, limit: int | None = None, instance: str | None = None
    ) -> dict[str, Any]:
        """Execute a read-only SELECT against MySQL / MariaDB.

        Run `estimate_query_cost` first on any query over an unfamiliar or large table —
        it reports what the query would examine without examining it.

        The statement must be a single SELECT/WITH/SHOW/DESCRIBE/EXPLAIN. A missing
        LIMIT is added and an oversized one is clamped; the SQL actually executed comes
        back in `executed_sql`, and the instance that answered in `instance`.
        `rows_examined` is what the server actually read to answer, `query_cost` the
        optimiser's own figure — compare them across two versions of a query.

        Args:
            sql: The statement to run.
            limit: Row limit for this call when the SQL has none of its own.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err

        decision = handle.policy.evaluate(sql, default_limit=limit)
        if not decision.allowed:
            audit.record(
                tool="run_select_query",
                instance=handle.name,
                sql=sql,
                query_hash=decision.query_hash,
                allowed=False,
                violations=decision.violations,
            )
            return blocked_response(decision, handle.name)

        try:
            result = handle.connection.execute(decision.sql, measure=True)
        except QueryError as exc:
            audit.record(
                tool="run_select_query",
                instance=handle.name,
                sql=decision.sql,
                query_hash=decision.query_hash,
                allowed=True,
                error=str(exc),
            )
            return {"error": str(exc), "instance": handle.name, "executed_sql": decision.sql}

        audit.record(
            tool="run_select_query",
            instance=handle.name,
            sql=decision.sql,
            query_hash=decision.query_hash,
            allowed=True,
            notes=decision.notes,
            effective_limit=decision.effective_limit,
            row_count=result.row_count,
            rows_read=result.rows_examined,
            elapsed_ms=result.elapsed_ms,
        )

        payload = result.to_dict()
        payload["instance"] = handle.name
        payload["executed_sql"] = decision.sql
        if decision.notes:
            payload["policy_notes"] = decision.notes
        if decision.effective_limit is not None and result.row_count >= decision.effective_limit:
            payload["truncated"] = True
            payload["policy_notes"] = payload.get("policy_notes", []) + [
                f"result reached the limit of {decision.effective_limit}; there may be more rows"
            ]
        return payload

    @mcp.tool(annotations=READ_ONLY)
    def sample_rows(
        table: str,
        database: str | None = None,
        limit: int = 10,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """Preview rows from a table without writing any SQL.

        Use this to see the shape of real values before composing a query. The
        statement is built here from validated identifiers, so no SQL is accepted.

        Args:
            table: Table name.
            database: Defaults to the instance's database.
            limit: Rows to return (capped by the instance's max limit).
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        db, err = database_or_error(handle, database)
        if err:
            return err

        try:
            qualified = f"{quote_identifier(db, 'database')}.{quote_identifier(table, 'table')}"
        except ValueError as exc:
            return {"error": str(exc), "instance": handle.name}

        capped = max(1, min(limit, handle.config.limits.max_limit))
        sql = f"SELECT * FROM {qualified} LIMIT {capped}"

        try:
            result = handle.connection.execute(sql, measure=True)
        except QueryError as exc:
            return {"error": str(exc), "instance": handle.name, "executed_sql": sql}

        audit.record(
            tool="sample_rows",
            instance=handle.name,
            sql=sql,
            query_hash="",
            allowed=True,
            row_count=result.row_count,
            rows_read=result.rows_examined,
            elapsed_ms=result.elapsed_ms,
        )

        payload = result.to_dict()
        payload["instance"] = handle.name
        payload["executed_sql"] = sql
        return payload
