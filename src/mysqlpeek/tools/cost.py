"""Validation and cost estimation.

These are the tools that make a read-only MySQL server genuinely safe to point an agent
at: all three ask the optimiser what a query *would* do, and none of them read a row of
table data. `EXPLAIN` on MySQL resolves every name, builds the plan and stops there.
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.server import MCPServer

from ..audit import AuditLog
from ..connection import Connection, QueryError
from ..policy.rules import mask_literals
from ..registry import InstanceRegistry
from . import READ_ONLY
from ._common import blocked_response, resolve

# Above this share of a table, a query is doing a scan rather than a lookup.
_FULL_SCAN_FRACTION = 0.5
_HEAVY_FRACTION = 0.1
# A scan of a table smaller than this is not worth a warning on its own.
_SMALL_TABLE_ROWS = 1_000

# Access types that read the whole table or the whole index, in optimiser terms.
_SCAN_ACCESS = {"ALL", "index"}
_EXPLAINABLE_HEADS = ("SELECT", "WITH")
_HEAD_RE = re.compile(r"\s*([A-Za-z_]+)")


def register(mcp: MCPServer, registry: InstanceRegistry, audit: AuditLog) -> None:
    def _prepare(sql: str, tool: str, instance: str | None):
        """Resolve the instance and clear the statement, or return an error payload."""
        handle, err = resolve(registry, instance)
        if err:
            return None, None, err

        decision = handle.policy.evaluate(sql, enforce_limit=False)
        if not decision.allowed:
            audit.record(
                tool=tool,
                instance=handle.name,
                sql=sql,
                query_hash=decision.query_hash,
                allowed=False,
                violations=decision.violations,
            )
            return None, None, blocked_response(decision, handle.name)

        head_match = _HEAD_RE.match(mask_literals(decision.sql))
        head = head_match.group(1).upper() if head_match else ""
        if head not in _EXPLAINABLE_HEADS:
            return None, None, {
                "instance": handle.name,
                "error": f"{head or 'this'} statements cannot be explained; only SELECT/WITH can",
                "hint": "SHOW and DESCRIBE read server metadata and are cheap to run directly",
            }
        return handle, decision, None

    @mcp.tool(annotations=READ_ONLY)
    def validate_query(sql: str, instance: str | None = None) -> dict[str, Any]:
        """Check whether a query is valid SQL, without running it.

        Uses EXPLAIN, which parses the statement and resolves every table and column
        name against the schema, then stops — nothing is read. This is the cheap way
        to check a query you just composed, instead of running it with LIMIT 1 and
        hoping.

        Args:
            sql: The statement to check.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, decision, err = _prepare(sql, "validate_query", instance)
        if err:
            return err
        try:
            handle.connection.execute(f"EXPLAIN {decision.sql}")
        except QueryError as exc:
            return {"valid": False, "instance": handle.name, "error": str(exc)}
        return {
            "valid": True,
            "instance": handle.name,
            "note": "syntax, names and plan all resolved; no data was read",
        }

    @mcp.tool(annotations=READ_ONLY)
    def estimate_query_cost(sql: str, instance: str | None = None) -> dict[str, Any]:
        """Estimate what a query would examine, before running it.

        Uses EXPLAIN FORMAT=JSON to report, per table, how the optimiser will access it
        (`access_type`: const / eq_ref / ref / range are lookups; ALL and index are
        scans), which index it picked, and how many rows it expects to examine — then
        compares that with the table's row count.

        Call this before `run_select_query` on any large or unfamiliar table. If the
        verdict is `full_scan`, filter on a column that leads an index (see
        `list_indexes`) and estimate again.

        Args:
            sql: The statement to estimate.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, decision, err = _prepare(sql, "estimate_query_cost", instance)
        if err:
            return err

        plan, perr = _explain_json(handle.connection, decision.sql)
        if perr:
            return {
                "error": perr,
                "instance": handle.name,
                "hint": "run validate_query first to check the syntax",
            }

        tables = _collect_tables(plan)
        aliases = _alias_map(decision.sql, handle.config.database)
        estimates: list[dict[str, Any]] = []
        total_examined = 0
        for t in tables:
            rows = _int(t.get("rows_examined_per_scan", t.get("rows")))
            entry: dict[str, Any] = {
                "table": t.get("table_name"),
                "access_type": t.get("access_type"),
                "key": t.get("key"),
                "possible_keys": t.get("possible_keys"),
                "rows_examined_per_scan": rows,
                "filtered_pct": _float(t.get("filtered")),
            }
            if t.get("partitions"):
                entry["partitions"] = t["partitions"]
            name = str(t.get("table_name") or "")
            if name.startswith("<"):
                # <derived2>, <subquery3>, <union1,2>: a materialised intermediate,
                # not a base table. Its inner tables were collected separately.
                entry["derived"] = True
            else:
                db, real = aliases.get(name.lower(), (handle.config.database, name))
                total = _table_total_rows(handle.connection, db, real)
                entry["table_total_rows"] = total
                if total and rows is not None and total > 0:
                    entry["fraction_of_table"] = round(min(rows / total, 1.0), 4)
            total_examined += rows or 0
            estimates.append(entry)

        query_cost = _float(((plan.get("query_block") or {}).get("cost_info") or {}).get("query_cost"))
        warnings = _plan_warnings(plan)
        verdict, assessment = _assess(estimates)

        audit.record(
            tool="estimate_query_cost",
            instance=handle.name,
            sql=decision.sql,
            query_hash=decision.query_hash,
            allowed=True,
            rows_read=total_examined,
        )

        payload: dict[str, Any] = {
            "instance": handle.name,
            "estimates": estimates,
            "total_rows_examined_estimate": total_examined,
            "verdict": verdict,
            "assessment": assessment,
            "note": "estimate only; no table data was read",
        }
        if query_cost is not None:
            payload["query_cost"] = query_cost
        if warnings:
            payload["warnings"] = warnings
        return payload

    @mcp.tool(annotations=READ_ONLY)
    def explain_plan(sql: str, instance: str | None = None) -> dict[str, Any]:
        """Show the query plan the optimiser chose, in its most readable form.

        MySQL 8.0.16+ answers with EXPLAIN FORMAT=TREE, which reads top-down as the
        operations the server will perform; older MySQL and MariaDB answer with the
        classic tabular EXPLAIN. Either way the JSON plan is also parsed so
        `uses_index` and the per-table access types are reported in the same shape
        `estimate_query_cost` uses.

        Args:
            sql: The statement to explain.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, decision, err = _prepare(sql, "explain_plan", instance)
        if err:
            return err

        payload: dict[str, Any] = {"instance": handle.name, "note": "plan only; no table data was read"}

        facts = handle.connection.facts
        tree = None
        if facts is None or (not facts.is_mariadb):
            try:
                result = handle.connection.execute(f"EXPLAIN FORMAT=TREE {decision.sql}")
                tree = "\n".join(str(row[0]) for row in result.rows)
            except QueryError:
                tree = None  # pre-8.0.16: fall through to the tabular form
        if tree:
            payload["format"] = "tree"
            payload["plan"] = tree
        else:
            try:
                result = handle.connection.execute(f"EXPLAIN {decision.sql}")
            except QueryError as exc:
                return {"error": str(exc), "instance": handle.name}
            payload["format"] = "table"
            payload["plan"] = [dict(zip(result.columns, row, strict=True)) for row in result.rows]

        plan, perr = _explain_json(handle.connection, decision.sql)
        if not perr:
            tables = _collect_tables(plan)
            accesses = [
                {
                    "table": t.get("table_name"),
                    "access_type": t.get("access_type"),
                    "key": t.get("key"),
                    "rows_examined_per_scan": _int(t.get("rows_examined_per_scan", t.get("rows"))),
                }
                for t in tables
            ]
            base = [a for a in accesses if not str(a["table"] or "").startswith("<")]
            payload["tables"] = accesses
            payload["uses_index"] = bool(base) and all(
                a["access_type"] not in _SCAN_ACCESS for a in base
            )
            warnings = _plan_warnings(plan)
            if warnings:
                payload["warnings"] = warnings
        return payload


# -- plan handling -------------------------------------------------------------------


def _explain_json(conn: Connection, sql: str) -> tuple[dict[str, Any], str | None]:
    try:
        result = conn.execute(f"EXPLAIN FORMAT=JSON {sql}")
    except QueryError as exc:
        return {}, str(exc)
    if not result.rows:
        return {}, "EXPLAIN returned nothing"
    raw = result.rows[0][0]
    try:
        plan = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}, "EXPLAIN FORMAT=JSON returned something that is not JSON"
    return plan if isinstance(plan, dict) else {}, None


def _collect_tables(node: Any, out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every table access in the plan, in document order.

    MySQL nests them under nested_loop / ordering_operation / grouping_operation /
    materialized_from_subquery; MariaDB under nested_loop, block-nl-join and friends.
    Walking every dict and list means neither shape needs special-casing: a table is
    any object that has a `table_name`.
    """
    if out is None:
        out = []
    if isinstance(node, dict):
        if "table_name" in node and ("access_type" in node or "rows" in node):
            out.append(node)
        for value in node.values():
            _collect_tables(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_tables(item, out)
    return out


def _plan_warnings(plan: dict[str, Any]) -> list[str]:
    """The plan features that mean extra work beyond the row reads."""
    found: list[str] = []
    text = json.dumps(plan)
    if '"using_filesort": true' in text:
        found.append("using_filesort: the result is sorted in a separate pass (no index serves the ORDER BY)")
    if '"using_temporary_table": true' in text:
        found.append("using_temporary_table: an intermediate table is built for GROUP BY / DISTINCT / UNION")
    if '"using_index_for_group_by"' in text and '"using_index_for_group_by": false' not in text:
        pass
    return found


_FROM_JOIN = re.compile(
    r"""
    \b(?:FROM|JOIN)\s+
    (?:`?(?P<db>[\w$]+)`?\s*\.\s*)?`?(?P<table>[\w$]+)`?
    (?:\s+(?:AS\s+)?`?(?P<alias>(?!(?:WHERE|ON|USING|JOIN|INNER|LEFT|RIGHT|CROSS|NATURAL|STRAIGHT_JOIN|GROUP|ORDER|LIMIT|HAVING|UNION|SET|FOR|LOCK|WINDOW|PARTITION|IGNORE|USE|FORCE)\b)[A-Za-z_][\w$]*)`?)?
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _alias_map(sql: str, default_db: str) -> dict[str, tuple[str, str]]:
    """alias (lower) -> (database, table) for every FROM/JOIN reference we can see.

    EXPLAIN reports the alias, not the table, so the table's row count has to be
    looked up by the name the query used. Comma joins are not parsed; a reference
    this misses simply gets no `fraction_of_table`, never a wrong one.
    """
    out: dict[str, tuple[str, str]] = {}
    for m in _FROM_JOIN.finditer(mask_literals(sql) if "`" not in sql else sql):
        db = m.group("db") or default_db
        table = m.group("table")
        out[table.lower()] = (db, table)
        if m.group("alias"):
            out[m.group("alias").lower()] = (db, table)
    return out


def _table_total_rows(conn: Connection, database: Any, table: Any) -> int | None:
    """Approximate rows in a table, or None if it cannot be determined.

    information_schema.TABLES.TABLE_ROWS is InnoDB's estimate, and on MySQL 8 it can be
    up to a day old. That is fine for a share-of-table figure; it is not a count.
    """
    if not database or not table:
        return None
    try:
        result = conn.execute(
            """
            SELECT TABLE_ROWS FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %(db)s AND TABLE_NAME = %(tbl)s
            """,
            parameters={"db": str(database), "tbl": str(table)},
        )
    except QueryError:
        return None
    if result.row_count == 0:
        return None
    return _int(result.rows[0][0])


def _assess(estimates: list[dict[str, Any]]) -> tuple[str, str]:
    base = [e for e in estimates if not e.get("derived")]
    if not base:
        return "trivial", "the optimiser expects to read no base table"

    worst: dict[str, Any] | None = None
    for e in base:
        scan = e.get("access_type") in _SCAN_ACCESS
        fraction = e.get("fraction_of_table")
        total = e.get("table_total_rows") or 0
        rows = e.get("rows_examined_per_scan") or 0
        severity = 0.0
        if fraction is not None:
            severity = fraction
        elif scan:
            severity = 1.0 if total >= _SMALL_TABLE_ROWS or total == 0 else 0.0
        if scan and total and total < _SMALL_TABLE_ROWS:
            severity = min(severity, _HEAVY_FRACTION - 0.01)  # scanning a tiny table is fine
        if worst is None or severity > worst["_severity"] or (
            severity == worst["_severity"] and rows > (worst.get("rows_examined_per_scan") or 0)
        ):
            worst = {**e, "_severity": severity}

    assert worst is not None
    severity = worst["_severity"]
    name = worst.get("table")
    rows = worst.get("rows_examined_per_scan") or 0
    total = worst.get("table_total_rows")
    access = worst.get("access_type")
    key = worst.get("key")

    if access in _SCAN_ACCESS and total and total < _SMALL_TABLE_ROWS:
        return (
            "selective",
            f"{name}: a full scan, but the table is small ({total:,} rows) — cheap as it is.",
        )
    if severity >= _FULL_SCAN_FRACTION:
        detail = f"{rows:,} of {total:,} rows" if total else f"about {rows:,} rows"
        return (
            "full_scan",
            f"{name}: access_type={access}, no index narrows this — it examines {detail}. "
            "Filter on a column that leads an index (list_indexes), then estimate again.",
        )
    if severity >= _HEAVY_FRACTION:
        return (
            "heavy",
            f"{name}: examines roughly {severity * 100:.1f}% of the table ({rows:,} rows) "
            f"via {access}{' on ' + key if key else ''}. Workable, but a tighter filter "
            "would cut it considerably.",
        )
    return (
        "selective",
        f"{name}: {access}{' on ' + key if key else ''} examines about {rows:,} rows — the "
        "index is doing its job.",
    )


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
