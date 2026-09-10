"""Entrypoint: `mysqlpeek`, `python -m mysqlpeek`, or `uvx mysqlpeek`."""

from __future__ import annotations

import sys

from .config import ConfigError
from .server import create_server, version

USAGE = """\
mysqlpeek — a read-only MySQL / MariaDB MCP server.

Usage:
  mysqlpeek              start the server on stdio (how an MCP client runs it)
  mysqlpeek --version    print the version and exit
  mysqlpeek --help       print this message and exit

Configuration comes from the environment:
  MYSQL_HOST             host, host:port, or a URL such as mysqls://host:3306
  MYSQL_USER             database user; prefer one granted only SELECT
  MYSQL_PASSWORD         or MYSQL_PASSWORD_FILE, which is preferred
  MYSQL_DATABASE         default database for tool calls (optional)
  MYSQLPEEK_PROFILES     path to a JSON profiles file, for several instances
  MYSQLPEEK_AUDIT_LOG    path to record every query decision as JSONL

Full documentation: https://github.com/sgr-xd/mysqlpeek
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    # Answered before any configuration is read, so `--version` works on a broken
    # install — which is exactly when you need to know which version is running.
    if "--version" in args or "-V" in args:
        print(f"mysqlpeek {version()}")
        return 0
    if "--help" in args or "-h" in args:
        print(USAGE, end="")
        return 0
    if args:
        print(f"mysqlpeek: unrecognised argument {args[0]!r}\n", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2

    try:
        mcp, registry = create_server()
    except ConfigError as exc:
        # stderr, because stdout is the MCP transport — anything written there that is
        # not a protocol message corrupts the session.
        print(f"mysqlpeek: configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        mcp.run(transport="stdio")
    finally:
        registry.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
