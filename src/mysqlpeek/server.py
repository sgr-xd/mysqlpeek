"""MCP application factory.

Tool modules each expose `register(mcp, registry, ...)` and are wired in here, so
adding a tool family never means editing the entrypoint.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from .audit import AuditLog
from .config import AppConfig
from .registry import InstanceRegistry
from .tools import READ_ONLY

INSTRUCTIONS = """\
mysqlpeek gives read-only access to one or more MySQL or MariaDB instances.

Work in this order, especially on tables you have not seen before:

1. `list_tables` / `describe_table` / `list_indexes` to learn the schema and which
   columns are indexed.
2. `estimate_query_cost` to see how many rows a query would examine BEFORE running it.
   If it reports a full scan, add a filter on an indexed column and estimate again.
3. `run_select_query` to execute.

`validate_query` checks a statement without reading any data. Writes are not possible:
every query runs inside a READ ONLY transaction under server-enforced row, join-size
and time caps, and the parser refuses anything but a single SELECT / SHOW / DESCRIBE /
EXPLAIN.

When more than one instance is configured, pass `instance` explicitly on every call.
Call `list_instances` first to see what exists. There is no "current" instance to
switch — the argument is the only thing that decides where a query goes, and every
response names the instance that answered it.
"""


def create_server(config: AppConfig | None = None) -> tuple[MCPServer, InstanceRegistry]:
    config = config or AppConfig.from_env()
    registry = InstanceRegistry(config)
    audit = AuditLog(config.audit_path)

    mcp = MCPServer("mysqlpeek", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    def server_info() -> dict[str, Any]:
        """Report how mysqlpeek is configured and which limits are in force.

        Useful as a first call to confirm the target instances and the caps that apply,
        without touching any data.
        """
        return {
            "mysqlpeek_version": version(),
            "default_instance": registry.default_name,
            "instances": registry.describe_all(),
            "audit_log_enabled": audit.enabled,
        }

    from .tools import discovery, query

    discovery.register(mcp, registry)
    query.register(mcp, registry, audit)

    return mcp, registry


def version() -> str:
    """The installed distribution's version.

    Read from package metadata rather than hardcoded, so there is one source of truth.
    Note that an editable development install records its version at install time and
    does not track later edits to pyproject.toml — re-run `uv pip install -e .` if this
    disagrees with the file.
    """
    try:
        from importlib.metadata import version as distribution_version

        return distribution_version("mysqlpeek")
    except Exception:  # noqa: BLE001 - a missing version must not stop the server
        return "unknown"
