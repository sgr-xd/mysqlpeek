"""MySQL / MariaDB connection handling.

One lazily-created connection per instance, reused for the process lifetime and
serialised with a lock: PyMySQL connections are not safe to share between threads.

Every connection is put into a read-only, capped state the moment it is opened, and a
connection whose guards cannot all be applied is closed and refused. That is the
second of three layers (parser → session guards → grants), and the one that holds when
a statement gets past the parser.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
import ssl as ssl_lib
import threading
import time
from dataclasses import dataclass
from typing import Any

import pymysql
import pymysql.cursors

from .config import InstanceConfig, Limits


class QueryError(RuntimeError):
    """A query failed server-side. Message is sanitized before it reaches a caller."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    elapsed_ms: int
    # What the server actually touched to answer, from the session's Handler_read_*
    # counters. Surfacing it lets a caller learn the real cost of the query it just
    # ran, not only the estimate. None when the caller did not ask for measurement.
    rows_examined: int | None = None
    # The optimiser's own cost figure for the statement (Last_query_cost), in its
    # abstract "page read" units. Comparable between two queries on the same server.
    query_cost: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.rows_examined is not None:
            out["rows_examined"] = self.rows_examined
        if self.query_cost is not None:
            out["query_cost"] = self.query_cost
        return out


@dataclass(frozen=True)
class ServerFacts:
    version: str
    dialect: str  # "mysql" or "mariadb"

    @property
    def is_mariadb(self) -> bool:
        return self.dialect == "mariadb"


def session_guards(limits: Limits, facts: ServerFacts) -> list[tuple[str, str]]:
    """The statements that make a session read-only and capped, in order.

    Each is a (label, sql) pair; the label is what a caller sees if one cannot be
    applied. The time cap is the one that differs by engine: MySQL takes milliseconds
    in `max_execution_time`, MariaDB takes seconds in `max_statement_time`.

    `max_join_size` only bites while `sql_big_selects` is 0 — MySQL flips the latter
    off automatically when the former is set, but saying so explicitly costs nothing
    and does not depend on that behaviour.
    """
    if facts.is_mariadb:
        time_cap = f"SET SESSION max_statement_time = {int(limits.max_execution_time)}"
    else:
        time_cap = f"SET SESSION max_execution_time = {int(limits.max_execution_time) * 1000}"
    return [
        ("read-only transaction", "SET SESSION TRANSACTION READ ONLY"),
        ("execution time cap", time_cap),
        ("result row cap", f"SET SESSION sql_select_limit = {int(limits.max_result_rows)}"),
        ("examined-rows cap", f"SET SESSION max_join_size = {int(limits.max_join_size)}"),
        ("examined-rows cap", "SET SESSION sql_big_selects = 0"),
    ]


# Session status counters that together approximate rows examined by the storage
# engine. The same figure the slow log reports as Rows_examined.
_HANDLER_READ_COUNTERS = (
    "Handler_read_first",
    "Handler_read_key",
    "Handler_read_last",
    "Handler_read_next",
    "Handler_read_prev",
    "Handler_read_rnd",
    "Handler_read_rnd_next",
)


class Connection:
    def __init__(self, config: InstanceConfig) -> None:
        self._config = config
        self._client: pymysql.connections.Connection | None = None
        self._facts: ServerFacts | None = None
        self._lock = threading.RLock()

    @property
    def config(self) -> InstanceConfig:
        return self._config

    @property
    def facts(self) -> ServerFacts | None:
        """Version and dialect, once connected. None before the first query."""
        return self._facts

    # -- connecting ----------------------------------------------------------------

    def _connect(self) -> pymysql.connections.Connection:
        s = self._config
        try:
            client = pymysql.connect(
                host=s.host,
                port=s.port,
                user=s.username,
                password=s.password,
                database=s.database or None,
                connect_timeout=s.connect_timeout,
                # The server-side time cap is what stops a query; this is the socket
                # backstop for a server that stops answering altogether.
                read_timeout=s.limits.max_execution_time + 15,
                write_timeout=30,
                charset="utf8mb4",
                autocommit=True,
                ssl=_ssl_context(s),
                program_name="mysqlpeek",
                # PyMySQL's default client flags do not include MULTI_STATEMENTS, and
                # the cursor class returns tuples. Both are relied on: a stacked
                # `SELECT 1; DROP ...` is a syntax error to the server, not two
                # statements.
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as QueryError
            raise QueryError(
                f"cannot connect to {s.host}:{s.port} as {s.username!r}: "
                f"{sanitize(_message(exc), s)}"
            ) from None

        try:
            facts = self._detect(client)
            for label, sql in session_guards(s.limits, facts):
                try:
                    with client.cursor() as cur:
                        cur.execute(sql)
                except Exception as exc:  # noqa: BLE001
                    raise QueryError(
                        f"instance {s.name!r} refused: the {label} guard could not be "
                        f"applied ({sql}): {sanitize(_message(exc), s)}. mysqlpeek does "
                        "not run without its guards; check the server version "
                        "(MySQL 5.7.8+ or MariaDB 10.1+) and the account's privileges."
                    ) from None
        except QueryError:
            client.close()
            raise

        self._facts = facts
        return client

    @staticmethod
    def _detect(client: pymysql.connections.Connection) -> ServerFacts:
        with client.cursor() as cur:
            cur.execute("SELECT VERSION()")
            row = cur.fetchone()
        version = str(row[0]) if row else "unknown"
        dialect = "mariadb" if "mariadb" in version.lower() else "mysql"
        return ServerFacts(version=version, dialect=dialect)

    def _get_client(self) -> pymysql.connections.Connection:
        if self._client is None:
            self._client = self._connect()
        return self._client

    def _discard(self) -> None:
        """Drop a connection that is no longer usable, so the next call reconnects.

        Never `ping(reconnect=True)`: PyMySQL's own reconnect re-runs none of the
        session guards, which would hand out an unguarded connection.
        """
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    # -- executing -----------------------------------------------------------------

    def execute(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
        *,
        measure: bool = False,
    ) -> QueryResult:
        """Run SQL under the session's read-only guards.

        Callers pass user-supplied SQL only after the policy engine has cleared it.
        Discovery tools use this with parameters and their own server-built SQL.

        `measure` adds two cheap status reads around the statement to report rows
        examined and the optimiser's cost. Off for internal queries.
        """
        with self._lock:
            client = self._get_client()
            try:
                before = self._handler_reads(client) if measure else None
                started = time.perf_counter()
                with client.cursor() as cur:
                    cur.execute(sql, parameters or None)
                    columns = [d[0] for d in (cur.description or ())]
                    rows = [[_jsonable(v) for v in row] for row in cur.fetchall()]
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                examined = cost = None
                if measure:
                    after = self._handler_reads(client)
                    if before is not None and after is not None:
                        examined = max(after - before, 0)
                    cost = self._last_query_cost(client)
            except pymysql.MySQLError as exc:
                if _is_connection_loss(exc):
                    self._discard()
                raise QueryError(_friendly(exc, self._config)) from None
            except Exception as exc:  # noqa: BLE001 - normalized for the caller
                self._discard()
                raise QueryError(sanitize(_message(exc), self._config)) from None

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=elapsed_ms,
            rows_examined=examined,
            query_cost=cost,
        )

    @staticmethod
    def _handler_reads(client: pymysql.connections.Connection) -> int | None:
        try:
            with client.cursor() as cur:
                cur.execute("SHOW SESSION STATUS LIKE 'Handler_read%%'")
                total = 0
                for name, value in cur.fetchall():
                    if name in _HANDLER_READ_COUNTERS:
                        total += int(value)
                return total
        except Exception:  # noqa: BLE001 - measurement must never fail the query
            return None

    @staticmethod
    def _last_query_cost(client: pymysql.connections.Connection) -> float | None:
        try:
            with client.cursor() as cur:
                cur.execute("SHOW SESSION STATUS LIKE 'Last_query_cost'")
                row = cur.fetchone()
            return float(row[1]) if row else None
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        with self._lock:
            self._discard()


# -- helpers -------------------------------------------------------------------------


def _ssl_context(config: InstanceConfig) -> ssl_lib.SSLContext | None:
    if not config.ssl:
        return None
    ctx = ssl_lib.create_default_context(cafile=config.ssl_ca or None)
    if not config.ssl_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl_lib.CERT_NONE
    return ctx


def _jsonable(value: Any) -> Any:
    """Coerce driver values into something the MCP layer can serialise.

    PyMySQL hands back bytes for BLOB and BINARY columns, Decimal for DECIMAL, and the
    datetime family for temporal types. Bytes that are not UTF-8 become a hex literal
    rather than an exception.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + bytes(value).hex()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    return str(value)


def _message(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 2 and isinstance(args[0], int):
        return f"{args[1]} (errno {args[0]})"
    return str(exc)


def _errno(exc: BaseException) -> int | None:
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


# Server errors that mean the session guards did their job. Translated so a caller
# reads what happened, not a number.
_ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION = 1792
_ER_TOO_BIG_SELECT = 1104
_TIME_CAP_ERRNOS = (
    3024,  # MySQL: ER_QUERY_TIMEOUT
    1317,  # ER_QUERY_INTERRUPTED
    1969,  # MariaDB: ER_STATEMENT_TIMEOUT
)
_CONNECTION_LOSS_ERRNOS = (2003, 2006, 2013, 2055)


def _friendly(exc: pymysql.MySQLError, config: InstanceConfig) -> str:
    errno = _errno(exc)
    limits = config.limits
    if errno == _ER_CANT_EXECUTE_IN_READ_ONLY_TRANSACTION:
        return "refused by the server: this session is a READ ONLY transaction"
    if errno == _ER_TOO_BIG_SELECT:
        return (
            f"refused before running: the optimiser expects this to examine more than "
            f"{limits.max_join_size:,} rows (max_join_size). Add a filter on an indexed "
            "column, or raise max_join_size for this instance."
        )
    if errno in _TIME_CAP_ERRNOS:
        return (
            f"stopped by the server: exceeded the {limits.max_execution_time}s execution "
            "cap. Narrow the query, or raise max_execution_time for this instance."
        )
    return sanitize(_message(exc), config)


def _is_connection_loss(exc: pymysql.MySQLError) -> bool:
    if isinstance(exc, pymysql.err.InterfaceError):
        return True
    return _errno(exc) in _CONNECTION_LOSS_ERRNOS


_PASSWORD_IN_URL = re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)")


def sanitize(message: str, config: InstanceConfig) -> str:
    """Strip credentials out of a server or driver error before it is returned."""
    cleaned = _PASSWORD_IN_URL.sub(r"\1***\2", message)
    if config.password:
        cleaned = cleaned.replace(config.password, "***")
    return cleaned.strip()
