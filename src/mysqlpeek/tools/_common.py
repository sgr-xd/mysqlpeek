"""Shared helpers for tool modules."""

from __future__ import annotations

import re
from typing import Any

from ..connection import QueryError, QueryResult
from ..policy import Decision
from ..registry import InstanceHandle, InstanceRegistry, UnknownInstance

# Every tool takes this argument. Kept in one place so the wording an agent reads is
# identical across all of them.
INSTANCE_ARG_DOC = "Which configured instance to use. Defaults to the default instance."

# MySQL identifiers may contain more than this, but a name outside this set is rare
# enough that refusing it is a better trade than widening the escape surface.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]{1,64}$")

SYSTEM_SCHEMAS = ("information_schema", "performance_schema", "mysql", "sys")


def quote_identifier(name: str, kind: str) -> str:
    """Validate then backtick-quote. Rejects anything that is not a plain identifier.

    Identifiers cannot be bound as parameters, so this is the guarantee that a table
    name never carries SQL into a statement built here.
    """
    if not _IDENTIFIER.match(name or ""):
        raise ValueError(
            f"invalid {kind} name {name!r}: expected letters, digits, underscores or $, "
            "at most 64 characters"
        )
    return f"`{name}`"


def resolve(
    registry: InstanceRegistry, instance: str | None
) -> tuple[InstanceHandle | None, dict[str, Any] | None]:
    """Look up an instance, or produce an error payload naming the valid choices."""
    try:
        return registry.get(instance), None
    except UnknownInstance as exc:
        return None, {
            "error": str(exc),
            "instances": registry.names,
            "hint": "call list_instances to see what is configured",
        }


def database_or_error(
    handle: InstanceHandle, database: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """The database a call applies to, or an error asking for one.

    MySQL does not require a default database, and many read-only accounts have none,
    so a missing name is a question back to the caller rather than a guess.
    """
    db = (database or handle.config.database or "").strip()
    if db:
        return db, None
    return None, {
        "error": "no database given and this instance has no default database",
        "instance": handle.name,
        "hint": "call list_databases, then pass database=<name>",
    }


def run(
    handle: InstanceHandle, sql: str, parameters: dict[str, Any] | None = None
) -> tuple[QueryResult | None, dict[str, Any] | None]:
    """Execute a mysqlpeek-authored query, turning failure into a payload.

    A tool that lets QueryError escape becomes a protocol-level error, which tells the
    caller nothing about which instance failed. That matters most with several
    instances configured, where one being unreachable must not look like a broken tool.
    """
    try:
        return handle.connection.execute(sql, parameters=parameters), None
    except QueryError as exc:
        return None, {"error": str(exc), "instance": handle.name}


def rows_as_dicts(result: QueryResult) -> list[dict[str, Any]]:
    return [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def human_size(num_bytes: Any) -> str | None:
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return None
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return None


def blocked_response(decision: Decision, instance: str) -> dict[str, Any]:
    """Shape a rejection so a model can act on it rather than just retrying."""
    return {
        "blocked": True,
        "instance": instance,
        "error": decision.rejection_message(),
        "violations": decision.violations,
        "hint": (
            "mysqlpeek is read-only. Rewrite as a single SELECT/WITH/SHOW/DESCRIBE/"
            "EXPLAIN statement over this instance's own tables."
        ),
    }
