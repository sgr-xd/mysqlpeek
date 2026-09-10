"""Operational tools: what the server is doing, and whether it is keeping up.

Every statement here is written by mysqlpeek against information_schema,
performance_schema, sys or SHOW output, so none of it goes through the policy engine.
Several need privileges beyond SELECT — PROCESS to see other sessions' queries,
REPLICATION CLIENT for replica status — and each tool says so when it cannot see.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..connection import QueryError
from ..registry import InstanceRegistry
from . import READ_ONLY
from ._common import human_size, resolve, rows_as_dicts, run

_PICOS = 1_000_000_000_000  # performance_schema timers are in picoseconds


def register(mcp: MCPServer, registry: InstanceRegistry) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_running_queries(
        min_seconds: int = 0, limit: int = 50, instance: str | None = None
    ) -> dict[str, Any]:
        """Statements executing right now, longest-running first.

        A point-in-time sample: an empty result means nothing was running at this
        instant, not that the server is idle. Seeing other users' sessions needs the
        PROCESS privilege; without it only your own connection appears, and the
        payload says so.

        Args:
            min_seconds: Only sessions that have been in their current state at least this long.
            limit: Maximum sessions to return.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT ID AS id, USER AS user, HOST AS host, DB AS db, COMMAND AS command,
                   TIME AS seconds, STATE AS state, LEFT(INFO, 300) AS query_sample
            FROM information_schema.PROCESSLIST
            WHERE COMMAND <> 'Sleep' AND ID <> CONNECTION_ID() AND TIME >= %(min_s)s
            ORDER BY TIME DESC
            LIMIT %(lim)s
            """,
            parameters={"min_s": max(0, int(min_seconds)), "lim": max(1, min(int(limit), 500))},
        )
        if qerr:
            return qerr
        idle, _ = run(
            handle,
            "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE COMMAND = 'Sleep'",
        )
        has_process = _has_privilege(handle, "PROCESS")
        return {
            "instance": handle.name,
            "running": rows_as_dicts(result),
            "count": result.row_count,
            "idle_connections": idle.rows[0][0] if idle and idle.rows else None,
            "sees_all_sessions": has_process,
            "note": None
            if has_process
            else "this account lacks PROCESS, so only its own session is visible",
        }

    @mcp.tool(annotations=READ_ONLY)
    def top_statements(
        order_by: str = "total_time",
        limit: int = 20,
        min_count: int = 1,
        no_index_only: bool = False,
        failed_only: bool = False,
        database: str | None = None,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """The statement shapes that cost the server most, from performance_schema.

        Statements are grouped by digest (literals normalised), with total and average
        time, rows examined versus rows sent, and how many executions used no index.
        A high `rows_examined` to `rows_sent` ratio is the classic sign of a missing
        index. Figures are cumulative since the table was last truncated or the server
        restarted (`first_seen` says how far back).

        Args:
            order_by: total_time | avg_time | count | rows_examined | no_index.
            limit: Digests to return.
            min_count: Ignore digests executed fewer times than this.
            no_index_only: Only digests that ran at least once without an index.
            failed_only: Only digests that produced errors.
            database: Restrict to one schema.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        orders = {
            "total_time": "SUM_TIMER_WAIT DESC",
            "avg_time": "AVG_TIMER_WAIT DESC",
            "count": "COUNT_STAR DESC",
            "rows_examined": "SUM_ROWS_EXAMINED DESC",
            "no_index": "SUM_NO_INDEX_USED DESC",
        }
        if order_by not in orders:
            return {"error": f"order_by must be one of {', '.join(orders)}", "instance": handle.name}
        conditions = ["COUNT_STAR >= %(min_count)s", "DIGEST_TEXT IS NOT NULL"]
        if no_index_only:
            conditions.append("SUM_NO_INDEX_USED > 0")
        if failed_only:
            conditions.append("SUM_ERRORS > 0")
        if database:
            conditions.append("SCHEMA_NAME = %(db)s")
        result, qerr = run(
            handle,
            f"""
            SELECT SCHEMA_NAME AS db,
                   LEFT(DIGEST_TEXT, 400) AS statement,
                   COUNT_STAR AS executions,
                   ROUND(SUM_TIMER_WAIT / {_PICOS}, 3) AS total_s,
                   ROUND(AVG_TIMER_WAIT / {_PICOS}, 6) AS avg_s,
                   ROUND(MAX_TIMER_WAIT / {_PICOS}, 3) AS max_s,
                   SUM_ROWS_EXAMINED AS rows_examined,
                   SUM_ROWS_SENT AS rows_sent,
                   SUM_NO_INDEX_USED AS no_index_used,
                   SUM_NO_GOOD_INDEX_USED AS no_good_index_used,
                   SUM_CREATED_TMP_DISK_TABLES AS tmp_disk_tables,
                   SUM_SORT_ROWS AS sort_rows,
                   SUM_ERRORS AS errors,
                   FIRST_SEEN AS first_seen,
                   LAST_SEEN AS last_seen
            FROM performance_schema.events_statements_summary_by_digest
            WHERE {" AND ".join(conditions)}
            ORDER BY {orders[order_by]}
            LIMIT %(lim)s
            """,
            parameters={
                "min_count": max(1, int(min_count)),
                "db": database,
                "lim": max(1, min(int(limit), 200)),
            },
        )
        if qerr:
            if "performance_schema" in qerr["error"] or "doesn't exist" in qerr["error"]:
                qerr["hint"] = (
                    "performance_schema is off or not readable on this instance; "
                    "statement digests are not available"
                )
            return qerr
        entries = rows_as_dicts(result)
        for e in entries:
            sent = e.get("rows_sent") or 0
            examined = e.get("rows_examined") or 0
            e["examined_per_row_sent"] = round(examined / sent, 1) if sent else None
        payload = {
            "instance": handle.name,
            "order_by": order_by,
            "statements": entries,
            "count": result.row_count,
            "note": "cumulative since the last restart or truncation of the digest table",
        }
        if not entries:
            # MariaDB ships with performance_schema off: the table exists and is empty,
            # which looks like a quiet server unless the flag is checked.
            flag, _ = run(handle, "SELECT @@performance_schema")
            if flag and flag.rows and not flag.rows[0][0]:
                payload["note"] = (
                    "performance_schema is OFF on this instance, so no digests are "
                    "collected; enable it in the server configuration to use this tool"
                )
        return payload

    @mcp.tool(annotations=READ_ONLY)
    def table_storage_stats(
        database: str | None = None, limit: int = 30, instance: str | None = None
    ) -> dict[str, Any]:
        """Tables ranked by size, with the facts that explain where the bytes are.

        `index_to_data` above 1.0 means more bytes in indexes than in rows — worth a
        look at `list_indexes` for redundant ones. `data_free` is space InnoDB has
        allocated but is not using; a large share of the total means fragmentation.
        `auto_increment_headroom` is how much of the column's range is left.

        Args:
            database: One schema, or every non-system schema when omitted.
            limit: Tables to return.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT t.TABLE_SCHEMA AS db, t.TABLE_NAME AS name, t.ENGINE AS engine,
                   t.TABLE_ROWS AS approx_rows,
                   t.DATA_LENGTH AS data_bytes, t.INDEX_LENGTH AS index_bytes,
                   (IFNULL(t.DATA_LENGTH, 0) + IFNULL(t.INDEX_LENGTH, 0)) AS total_bytes,
                   t.DATA_FREE AS data_free_bytes,
                   t.AUTO_INCREMENT AS auto_increment,
                   (SELECT c.COLUMN_TYPE FROM information_schema.COLUMNS c
                     WHERE c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
                       AND c.EXTRA LIKE '%%auto_increment%%' LIMIT 1) AS auto_increment_type
            FROM information_schema.TABLES t
            WHERE t.TABLE_TYPE = 'BASE TABLE'
              AND (%(db)s IS NOT NULL AND t.TABLE_SCHEMA = %(db)s
                   OR %(db)s IS NULL AND t.TABLE_SCHEMA NOT IN
                       ('information_schema', 'performance_schema', 'mysql', 'sys'))
            ORDER BY total_bytes DESC
            LIMIT %(lim)s
            """,
            parameters={"db": database, "lim": max(1, min(int(limit), 500))},
        )
        if qerr:
            return qerr
        tables = rows_as_dicts(result)
        for t in tables:
            t["size"] = human_size(t.get("total_bytes"))
            data = t.get("data_bytes") or 0
            t["index_to_data"] = round((t.get("index_bytes") or 0) / data, 2) if data else None
            total = t.get("total_bytes") or 0
            free = t.get("data_free_bytes") or 0
            t["fragmentation"] = round(free / (total + free), 3) if (total + free) else None
            t["auto_increment_headroom"] = _headroom(t.pop("auto_increment_type", None), t.get("auto_increment"))
        return {"instance": handle.name, "tables": tables, "count": result.row_count}

    @mcp.tool(annotations=READ_ONLY)
    def replication_status(instance: str | None = None) -> dict[str, Any]:
        """Whether this instance is a replica, and if so how far behind and whether both
        threads run.

        Also reports `read_only` / `super_read_only`, which is how a replica is meant to
        be protected from stray writes. Needs the REPLICATION CLIENT privilege (or
        REPLICATION_SLAVE_ADMIN) to see replica status; without it the payload says so.

        Args:
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        payload: dict[str, Any] = {"instance": handle.name}
        flags, _ = run(handle, "SELECT @@read_only")
        payload["read_only"] = bool(flags.rows[0][0]) if flags and flags.rows else None
        sro, _ = run(handle, "SELECT @@super_read_only")
        payload["super_read_only"] = bool(sro.rows[0][0]) if sro and sro.rows else None

        result = None
        last_error = None
        for stmt in ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"):
            try:
                result = handle.connection.execute(stmt)
                break
            except QueryError as exc:
                last_error = str(exc)
        if result is None:
            payload["error"] = last_error or "replica status unavailable"
            payload["hint"] = "needs REPLICATION CLIENT; or this engine has neither statement"
            return payload
        if result.row_count == 0:
            payload["is_replica"] = False
            payload["assessment"] = "not a replica (no replication source configured)"
            return payload

        row = dict(zip(result.columns, result.rows[0], strict=True))
        # 8.0.22 renamed the columns; read either spelling.
        pick = lambda *names: next((row[n] for n in names if n in row), None)  # noqa: E731
        io_running = str(pick("Replica_IO_Running", "Slave_IO_Running") or "")
        sql_running = str(pick("Replica_SQL_Running", "Slave_SQL_Running") or "")
        behind = pick("Seconds_Behind_Source", "Seconds_Behind_Master")
        summary = {
            "is_replica": True,
            "source_host": pick("Source_Host", "Master_Host"),
            "io_running": io_running,
            "sql_running": sql_running,
            "seconds_behind_source": behind,
            "last_io_error": pick("Last_IO_Error") or None,
            "last_sql_error": pick("Last_SQL_Error") or None,
            "gtid_mode": bool(pick("Auto_Position")),
            "channel": pick("Channel_Name"),
        }
        problems = []
        if io_running.lower() != "yes":
            problems.append(f"IO thread is not running ({io_running or 'unknown'})")
        if sql_running.lower() != "yes":
            problems.append(f"SQL thread is not running ({sql_running or 'unknown'})")
        if behind is None and not problems:
            problems.append("lag is unknown (Seconds_Behind_Source is NULL)")
        elif behind is not None and int(behind) > 60:
            problems.append(f"{int(behind)}s behind the source")
        if summary["last_io_error"] or summary["last_sql_error"]:
            problems.append("a replication error is recorded")
        summary["assessment"] = "; ".join(problems) if problems else "replicating, both threads running, lag under 60s"
        payload.update(summary)
        return payload

    @mcp.tool(annotations=READ_ONLY)
    def lock_waits(instance: str | None = None) -> dict[str, Any]:
        """Sessions blocked on a row lock right now, and who is blocking them.

        Reads sys.innodb_lock_waits where the sys schema exists (MySQL 5.7+, MariaDB
        10.6+), otherwise information_schema.INNODB_LOCK_WAITS. A healthy server
        returns nothing here. When something is listed, `blocking_pid` is the session
        to look at with `list_running_queries` or `long_transactions`.

        Args:
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT wait_started, wait_age_secs, locked_table, locked_index, locked_type,
                   waiting_pid, LEFT(waiting_query, 300) AS waiting_query,
                   waiting_lock_mode, blocking_pid, LEFT(blocking_query, 300) AS blocking_query,
                   blocking_lock_mode, blocking_trx_age, blocking_trx_rows_modified
            FROM sys.innodb_lock_waits
            ORDER BY wait_age_secs DESC
            LIMIT 100
            """,
        )
        source = "sys.innodb_lock_waits"
        if qerr:
            result, qerr2 = run(
                handle,
                """
                SELECT w.requesting_trx_id AS waiting_trx, r.trx_mysql_thread_id AS waiting_pid,
                       LEFT(r.trx_query, 300) AS waiting_query,
                       w.blocking_trx_id AS blocking_trx, b.trx_mysql_thread_id AS blocking_pid,
                       LEFT(b.trx_query, 300) AS blocking_query,
                       TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS wait_age_secs
                FROM information_schema.INNODB_LOCK_WAITS w
                JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_trx_id
                JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_trx_id
                ORDER BY wait_age_secs DESC
                LIMIT 100
                """,
            )
            source = "information_schema.INNODB_LOCK_WAITS"
            if qerr2:
                return {**qerr, "hint": "neither sys.innodb_lock_waits nor information_schema.INNODB_LOCK_WAITS is readable here"}
        waits = rows_as_dicts(result)
        return {
            "instance": handle.name,
            "source": source,
            "waits": waits,
            "count": result.row_count,
            "assessment": "no lock waits at this instant"
            if not waits
            else f"{len(waits)} session(s) waiting on row locks; longest {waits[0].get('wait_age_secs')}s",
        }

    @mcp.tool(annotations=READ_ONLY)
    def long_transactions(
        min_seconds: int = 10, limit: int = 50, instance: str | None = None
    ) -> dict[str, Any]:
        """InnoDB transactions open longer than `min_seconds`, oldest first.

        A transaction that has been open for minutes holds its undo history, blocks
        purge (history list grows, everything slows) and may hold row locks that
        others wait on. `rows_modified` says whether it has written anything;
        `query_sample` is NULL when the session is idle inside the transaction — the
        common case, and the one that is hardest to spot any other way.

        Args:
            min_seconds: Only transactions open at least this long.
            limit: Transactions to return.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT trx_id, trx_state AS state, trx_started AS started,
                   TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_s,
                   trx_mysql_thread_id AS pid,
                   trx_rows_locked AS rows_locked, trx_rows_modified AS rows_modified,
                   trx_lock_structs AS lock_structs,
                   trx_isolation_level AS isolation,
                   LEFT(trx_query, 300) AS query_sample
            FROM information_schema.INNODB_TRX
            WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) >= %(min_s)s
              AND trx_mysql_thread_id <> CONNECTION_ID()
            ORDER BY trx_started
            LIMIT %(lim)s
            """,
            parameters={"min_s": max(0, int(min_seconds)), "lim": max(1, min(int(limit), 500))},
        )
        if qerr:
            return qerr
        trx = rows_as_dicts(result)
        return {
            "instance": handle.name,
            "transactions": trx,
            "count": result.row_count,
            "assessment": f"no transaction open for {min_seconds}s or more"
            if not trx
            else f"oldest open transaction is {trx[0].get('age_s')}s old (pid {trx[0].get('pid')})",
        }

    @mcp.tool(annotations=READ_ONLY)
    def server_health(instance: str | None = None) -> dict[str, Any]:
        """One-shot health figures: connections, buffer pool hit rate, temp tables on
        disk, full scans, lock waits and the InnoDB history list.

        Counters are cumulative since the server started, so a non-zero figure is not
        by itself a problem — `uptime_s` gives the denominator. Ratios (buffer pool
        hit rate, temp tables spilling to disk) are meaningful on their own.

        Args:
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        wanted = (
            "Uptime", "Threads_connected", "Threads_running", "Max_used_connections",
            "Questions", "Slow_queries", "Aborted_connects", "Aborted_clients",
            "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads",
            "Innodb_buffer_pool_wait_free", "Innodb_row_lock_waits", "Innodb_row_lock_time_avg",
            "Innodb_row_lock_current_waits", "Created_tmp_tables", "Created_tmp_disk_tables",
            "Select_full_join", "Select_scan", "Sort_merge_passes", "Table_open_cache_misses",
            "Open_tables", "Opened_tables", "Innodb_os_log_written", "Bytes_received", "Bytes_sent",
        )
        result, qerr = run(handle, "SHOW GLOBAL STATUS")
        if qerr:
            return qerr
        status = {name: _num(value) for name, value in result.rows if name in wanted}
        variables, _ = run(
            handle,
            "SELECT @@max_connections, @@innodb_buffer_pool_size, @@read_only, @@version, @@version_comment",
        )
        v = variables.rows[0] if variables and variables.rows else [None] * 5
        history, _ = run(
            handle,
            "SELECT COUNT FROM information_schema.INNODB_METRICS WHERE NAME = 'trx_rseg_history_len'",
        )
        history_len = history.rows[0][0] if history and history.rows else None

        req = status.get("Innodb_buffer_pool_read_requests") or 0
        miss = status.get("Innodb_buffer_pool_reads") or 0
        tmp = status.get("Created_tmp_tables") or 0
        tmp_disk = status.get("Created_tmp_disk_tables") or 0
        payload: dict[str, Any] = {
            "instance": handle.name,
            "version": f"{v[3]} ({v[4]})" if v[3] else None,
            "uptime_s": status.get("Uptime"),
            "read_only": bool(v[2]) if v[2] is not None else None,
            "connections": {
                "current": status.get("Threads_connected"),
                "running": status.get("Threads_running"),
                "max_used": status.get("Max_used_connections"),
                "max_connections": v[0],
                "aborted_connects": status.get("Aborted_connects"),
                "aborted_clients": status.get("Aborted_clients"),
            },
            "buffer_pool": {
                "size": human_size(v[1]),
                "read_requests": req,
                "disk_reads": miss,
                "hit_rate": round(1 - miss / req, 4) if req else None,
                "wait_free": status.get("Innodb_buffer_pool_wait_free"),
            },
            "locks": {
                "row_lock_waits": status.get("Innodb_row_lock_waits"),
                "row_lock_current_waits": status.get("Innodb_row_lock_current_waits"),
                "row_lock_time_avg_ms": status.get("Innodb_row_lock_time_avg"),
            },
            "queries": {
                "questions": status.get("Questions"),
                "slow_queries": status.get("Slow_queries"),
                "select_full_join": status.get("Select_full_join"),
                "select_scan": status.get("Select_scan"),
                "sort_merge_passes": status.get("Sort_merge_passes"),
                "tmp_tables": tmp,
                "tmp_disk_tables": tmp_disk,
                "tmp_disk_fraction": round(tmp_disk / tmp, 4) if tmp else None,
            },
            "tables": {
                "open": status.get("Open_tables"),
                "opened_total": status.get("Opened_tables"),
                "open_cache_misses": status.get("Table_open_cache_misses"),
            },
            "innodb_history_list_length": history_len,
        }
        payload["assessment"] = _assess_health(payload)
        return payload


# -- helpers ----------------------------------------------------------------------


def _has_privilege(handle: Any, privilege: str) -> bool:
    result, qerr = run(handle, "SHOW GRANTS")
    if qerr or result is None:
        return False
    text = " ".join(str(row[0]) for row in result.rows).upper()
    return f" {privilege} " in text.replace(",", " ") or "ALL PRIVILEGES" in text


def _num(value: Any) -> Any:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    return int(f) if f.is_integer() else f


_INT_MAX = {
    "tinyint": 127, "smallint": 32767, "mediumint": 8388607, "int": 2147483647,
    "bigint": 9223372036854775807,
}


def _headroom(column_type: Any, current: Any) -> float | None:
    """Share of an auto-increment column's range still unused, or None."""
    if not column_type or current is None:
        return None
    text = str(column_type).lower()
    base = text.split("(")[0].strip()
    limit = _INT_MAX.get(base)
    if limit is None:
        return None
    if "unsigned" in text:
        limit = limit * 2 + 1
    try:
        return round(1 - int(current) / limit, 4)
    except (TypeError, ValueError):
        return None


def _assess_health(p: dict[str, Any]) -> str:
    findings: list[str] = []
    c = p["connections"]
    if c["current"] is not None and c["max_connections"]:
        share = c["current"] / c["max_connections"]
        if share > 0.8:
            findings.append(f"connections at {share * 100:.0f}% of max_connections")
    bp = p["buffer_pool"]
    if bp["hit_rate"] is not None and bp["read_requests"] and bp["read_requests"] > 100_000 and bp["hit_rate"] < 0.99:
        findings.append(f"buffer pool hit rate {bp['hit_rate'] * 100:.2f}% — working set larger than the pool")
    if bp["wait_free"]:
        findings.append(f"{bp['wait_free']} waits for a free buffer pool page")
    q = p["queries"]
    if q["tmp_disk_fraction"] is not None and q["tmp_tables"] > 1000 and q["tmp_disk_fraction"] > 0.25:
        findings.append(f"{q['tmp_disk_fraction'] * 100:.0f}% of temporary tables spill to disk")
    lk = p["locks"]
    if lk["row_lock_current_waits"]:
        findings.append(f"{lk['row_lock_current_waits']} session(s) waiting on row locks now")
    if p["innodb_history_list_length"] and int(p["innodb_history_list_length"]) > 1_000_000:
        findings.append("InnoDB history list over 1M — purge is falling behind, look for long transactions")
    return "; ".join(findings) if findings else "nothing stands out in the global counters"
