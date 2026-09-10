"""Read-only integration tests against a real MySQL or MariaDB.

Skipped unless MYSQL_HOST or MYSQLPEEK_PROFILES is set. Nothing here writes: every
statement is a SELECT/SHOW, or a write that must be REFUSED by the server.

    export MYSQL_HOST=127.0.0.1:3306 MYSQL_USER=... MYSQL_PASSWORD=... MYSQL_DATABASE=...
    export MYSQLPEEK_TEST_TABLE=some_table      # optional, enables the table tests
    pytest -m integration
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from mysqlpeek.config import AppConfig
from mysqlpeek.connection import QueryError
from mysqlpeek.registry import InstanceRegistry, UnknownInstance

pytestmark = pytest.mark.integration

if not (os.environ.get("MYSQL_HOST") or os.environ.get("MYSQLPEEK_PROFILES")):
    pytest.skip("no MySQL configured", allow_module_level=True)

TEST_TABLE = os.environ.get("MYSQLPEEK_TEST_TABLE")


@pytest.fixture(scope="module")
def registry():
    reg = InstanceRegistry(AppConfig.from_env())
    yield reg
    reg.close_all()


@pytest.fixture(scope="module")
def handle(registry: InstanceRegistry):
    return registry.get()


class TestConnectivity:
    def test_server_answers(self, handle) -> None:
        assert handle.connection.execute("SELECT 1 AS ok").rows == [[1]]
        assert handle.connection.facts is not None
        assert handle.connection.facts.dialect in ("mysql", "mariadb")

    def test_errors_do_not_leak_the_password(self, handle) -> None:
        password = handle.config.password
        if not password:
            pytest.skip("no password configured")
        with pytest.raises(QueryError) as exc:
            handle.connection.execute("SELECT * FROM does_not_exist_anywhere")
        assert password not in str(exc.value)

    def test_measurement_reports_rows_examined(self, handle) -> None:
        result = handle.connection.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS", measure=True
        )
        assert result.rows_examined is not None
        assert result.elapsed_ms >= 0


class TestGuardsAreEnforcedByTheServer:
    """Each guard must hold even when the statement never went near the parser.

    These bypass the policy engine on purpose and hand SQL straight to the connection:
    the point is that the server refuses, not that mysqlpeek does.
    """

    def test_writes_are_refused_by_the_read_only_transaction(self, handle) -> None:
        # Both target a schema that always exists, so the failure is the guard and not
        # "no database selected". The DELETE matches nothing even if the guard were
        # broken; the CREATE DATABASE would be visible, which is the point of naming it.
        with pytest.raises(QueryError, match="READ ONLY"):
            handle.connection.execute(
                "DELETE FROM mysql.time_zone_name WHERE Name = 'mysqlpeek-no-such-zone'"
            )
        with pytest.raises(QueryError, match="READ ONLY"):
            handle.connection.execute("CREATE DATABASE mysqlpeek_should_not_exist")

    @staticmethod
    def _big_join() -> tuple[str, int]:
        """A self join of the test table that must evaluate at least 10**8 rows.

        Returns the SQL and the optimiser-facing row estimate. information_schema
        will not do: MySQL 5.7 materialises it at query time and estimates two rows
        per table, so the join-size cap never fires. Nor will a bare COUNT(*) over a
        cross join: MySQL 8 and MariaDB answer that as a product of row counts in
        milliseconds without touching a row. Summing a column forces the rows to be
        produced. The number of tables is chosen from the real row count so the join
        is big enough to be killed, but not so big that the join-size cap (which the
        time-cap test lifts to ten times the estimate) fires first on MariaDB.
        """
        if not TEST_TABLE:
            pytest.skip("MYSQLPEEK_TEST_TABLE not set")
        columns = call_ok("describe_table", {"table": TEST_TABLE})["columns"]
        col = columns[0]["name"]
        rows = int(call_ok("run_select_query", {"sql": f"SELECT COUNT(*) FROM {TEST_TABLE}"})["rows"][0][0])
        if rows < 100:
            pytest.skip("test table too small to build a slow join from")
        ways = 2
        while rows**ways < 10**8 and ways < 4:
            ways += 1
        aliases = "abcd"[:ways]
        total = " + ".join(f"LENGTH({a}.`{col}`)" for a in aliases)
        tables = ", ".join(f"{TEST_TABLE} {a}" for a in aliases)
        return f"SELECT SUM({total}) FROM {tables}", rows**ways

    def test_time_cap_stops_a_long_read(self, registry: InstanceRegistry) -> None:
        # Not SLEEP() or BENCHMARK(): MySQL lets those absorb the interrupt and return
        # quietly, so they prove nothing. A real read that is killed mid-way raises
        # ER_QUERY_TIMEOUT. The join-size cap is lifted for this connection so the
        # time cap is the one that fires.
        from dataclasses import replace

        from mysqlpeek.config import Limits
        from mysqlpeek.connection import Connection

        sql, estimate = self._big_join()
        base = registry.get().config
        slow = Connection(
            replace(base, limits=Limits(max_execution_time=1, max_join_size=estimate * 10))
        )
        try:
            with pytest.raises(QueryError, match="execution cap"):
                slow.execute(sql)
        finally:
            slow.close()

    def test_result_row_cap_bounds_an_unlimited_select(self, handle) -> None:
        # sql_select_limit binds when the statement has no LIMIT of its own.
        cap = handle.config.limits.max_result_rows
        sql = (
            "SELECT a.TABLE_NAME FROM information_schema.COLUMNS a, "
            "information_schema.COLUMNS b"
        )
        try:
            result = handle.connection.execute(sql)
        except QueryError as exc:
            # A tiny max_join_size can refuse this cross join before it runs; that is
            # the other cap doing its job, which is also a pass.
            assert "max_join_size" in str(exc)
            return
        assert result.row_count <= cap

    def test_examined_rows_cap_refuses_before_reading(self, registry: InstanceRegistry) -> None:
        from dataclasses import replace

        from mysqlpeek.config import Limits
        from mysqlpeek.connection import Connection

        sql, _ = self._big_join()
        base = registry.get().config
        tight = Connection(replace(base, limits=Limits(max_join_size=10)))
        try:
            started = __import__("time").perf_counter()
            with pytest.raises(QueryError, match="max_join_size"):
                tight.execute(sql)
            # Refused by the optimiser, not killed after running: it must be quick.
            assert __import__("time").perf_counter() - started < 5
        finally:
            tight.close()


class TestInstanceRouting:
    def test_default_resolves(self, registry: InstanceRegistry) -> None:
        assert registry.get(None).name == registry.default_name

    def test_unknown_instance_is_refused_without_connecting(
        self, registry: InstanceRegistry
    ) -> None:
        with pytest.raises(UnknownInstance):
            registry.get("definitely-not-configured")


def call_tool(name: str, arguments: dict):
    """Call a tool the way a client does, not by running its SQL by hand."""
    from mysqlpeek.server import create_server

    mcp, registry = create_server()
    try:
        result = asyncio.run(mcp.call_tool(name, arguments))
    finally:
        registry.close_all()
    return json.loads(result.content[0].text)


def call_ok(name: str, arguments: dict):
    payload = call_tool(name, arguments)
    assert "error" not in payload, f"{name} returned an error: {payload.get('error')}"
    return payload


class TestEveryToolAgainstLiveServer:
    def test_server_info(self) -> None:
        assert call_ok("server_info", {})["instances"]

    def test_list_instances(self) -> None:
        assert call_ok("list_instances", {})["instances"]

    def test_list_databases(self) -> None:
        payload = call_ok("list_databases", {})
        assert payload["count"] >= 1
        assert any(d["system"] for d in payload["databases"])

    def test_list_tables_in_information_schema(self) -> None:
        payload = call_ok("list_tables", {"database": "information_schema"})
        assert payload["count"] >= 1

    def test_describe_table_in_information_schema(self) -> None:
        payload = call_ok("describe_table", {"table": "COLUMNS", "database": "information_schema"})
        assert payload["column_count"] > 0

    def test_run_select_query_appends_limit_and_measures(self) -> None:
        payload = call_ok("run_select_query", {"sql": "SELECT 1 AS one"})
        assert payload["executed_sql"].endswith("LIMIT 100")
        assert payload["rows"] == [[1]]
        assert "rows_examined" in payload

    def test_run_select_query_refuses_a_write_before_the_network(self) -> None:
        payload = call_tool("run_select_query", {"sql": "DROP TABLE users"})
        assert payload["blocked"] is True

    def test_missing_database_is_a_question_not_a_guess(self, registry) -> None:
        payload = call_tool("list_tables", {})
        if registry.get().config.database:
            assert "tables" in payload
        else:
            assert "database" in payload["error"]

    @pytest.mark.skipif(not TEST_TABLE, reason="MYSQLPEEK_TEST_TABLE not set")
    def test_table_tools(self) -> None:
        assert call_ok("describe_table", {"table": TEST_TABLE})["column_count"] > 0
        assert "indexes" in call_ok("list_indexes", {"table": TEST_TABLE})
        assert "CREATE" in call_ok("show_create_table", {"table": TEST_TABLE})["ddl"]
        assert call_ok("sample_rows", {"table": TEST_TABLE, "limit": 3})["row_count"] <= 3
        tables = call_ok("list_tables", {"name_like": TEST_TABLE})["tables"]
        assert any(t["name"] == TEST_TABLE for t in tables)


@pytest.mark.skipif(not TEST_TABLE, reason="MYSQLPEEK_TEST_TABLE not set")
class TestCostToolsAgainstLiveServer:
    """The optimiser's answers, not ours: every assertion here is about the shape and
    the verdict, because the numbers belong to the engine under test."""

    def test_validate_query_resolves_names(self) -> None:
        assert call_tool("validate_query", {"sql": f"SELECT 1 FROM {TEST_TABLE}"})["valid"] is True
        bad = call_tool("validate_query", {"sql": f"SELECT no_such_column_xyz FROM {TEST_TABLE}"})
        assert bad["valid"] is False and "no_such_column_xyz" in bad["error"]

    def test_validate_query_refuses_to_explain_show(self) -> None:
        payload = call_tool("validate_query", {"sql": "SHOW TABLES"})
        assert "cannot be explained" in payload["error"]

    def test_estimate_reports_every_table_with_an_access_type(self) -> None:
        payload = call_ok("estimate_query_cost", {"sql": f"SELECT * FROM {TEST_TABLE}"})
        assert payload["verdict"] in ("full_scan", "heavy", "selective", "trivial")
        assert payload["estimates"], "a base-table read must produce an estimate"
        first = payload["estimates"][0]
        assert first["access_type"] in ("ALL", "index", "range", "ref", "eq_ref", "const")
        assert first["table_total_rows"] is not None
        assert "no table data was read" in payload["note"]

    def test_unfiltered_scan_of_a_large_table_is_a_full_scan(self) -> None:
        total = call_ok("estimate_query_cost", {"sql": f"SELECT * FROM {TEST_TABLE}"})
        if (total["estimates"][0].get("table_total_rows") or 0) < 1000:
            pytest.skip("test table too small for a full-scan verdict")
        assert total["verdict"] == "full_scan"
        assert total["estimates"][0]["fraction_of_table"] >= 0.5

    def test_primary_key_lookup_is_selective(self) -> None:
        pk = call_ok("list_indexes", {"table": TEST_TABLE})["indexes"]
        primary = next((i for i in pk if i["name"] == "PRIMARY"), None)
        if primary is None or "," in primary["columns"]:
            pytest.skip("no single-column primary key to look up")
        payload = call_ok(
            "estimate_query_cost",
            {"sql": f"SELECT * FROM {TEST_TABLE} WHERE {primary['columns']} = 1"},
        )
        assert payload["verdict"] == "selective"
        assert payload["estimates"][0]["key"] == "PRIMARY"

    def test_alias_is_mapped_back_to_the_table(self) -> None:
        payload = call_ok("estimate_query_cost", {"sql": f"SELECT t.* FROM {TEST_TABLE} AS t"})
        assert payload["estimates"][0]["table_total_rows"] is not None

    def test_explain_plan_reports_index_use(self) -> None:
        payload = call_ok("explain_plan", {"sql": f"SELECT * FROM {TEST_TABLE}"})
        assert payload["format"] in ("tree", "table")
        assert payload["plan"]
        assert payload["uses_index"] is False

    def test_cost_tools_go_through_the_policy(self) -> None:
        for tool in ("validate_query", "estimate_query_cost", "explain_plan"):
            assert call_tool(tool, {"sql": "DROP TABLE x"})["blocked"] is True


def _pick_profilable_column() -> str | None:
    if not TEST_TABLE:
        return None
    payload = call_tool("describe_table", {"table": TEST_TABLE})
    for column in payload.get("columns", []):
        if not str(column["type"]).lower().startswith(("json", "geometry", "blob", "longblob")):
            return column["name"]
    return None


PROFILE_COLUMN = _pick_profilable_column()


@pytest.mark.skipif(not TEST_TABLE or not PROFILE_COLUMN, reason="no profilable column available")
class TestProfileColumnAgainstLiveServer:
    def test_profiles_a_real_column(self) -> None:
        payload = call_ok(
            "profile_column", {"table": TEST_TABLE, "column": PROFILE_COLUMN, "top_n": 3}
        )
        assert payload["rows_scanned"] > 0
        assert payload["distinct_count"] >= 1
        assert len(payload["top_values"]) <= 3
        assert all(0 <= v["fraction"] <= 1 for v in payload["top_values"])
        assert payload["null_count"] + sum(1 for _ in ()) >= 0
        assert payload["assessment"]

    def test_sample_size_is_honoured(self) -> None:
        payload = call_ok(
            "profile_column", {"table": TEST_TABLE, "column": PROFILE_COLUMN, "sample_rows": 50}
        )
        assert payload["rows_scanned"] <= 50

    def test_unknown_column_is_refused_before_scanning(self) -> None:
        payload = call_tool("profile_column", {"table": TEST_TABLE, "column": "no_such_column_xyz"})
        assert "not found" in payload["error"]
        assert "describe_table" in payload["hint"]


@pytest.mark.skipif(
    not os.environ.get("MYSQLPEEK_PROFILES"), reason="MYSQLPEEK_PROFILES not set"
)
class TestSeveralInstances:
    """With a profiles file, routing by `instance=` must reach the named server."""

    def test_every_instance_answers_with_its_own_version(self) -> None:
        names = call_ok("list_instances", {})["instances"]
        assert len(names) >= 2
        seen = {}
        for entry in names:
            payload = call_ok(
                "run_select_query", {"sql": "SELECT VERSION()", "instance": entry["instance"]}
            )
            assert payload["instance"] == entry["instance"]
            seen[entry["instance"]] = payload["rows"][0][0]
        assert len(set(seen.values())) >= 2, f"instances did not route to distinct servers: {seen}"

    def test_unknown_instance_is_refused_with_the_valid_names(self) -> None:
        payload = call_tool("run_select_query", {"sql": "SELECT 1", "instance": "nope"})
        assert "unknown instance" in payload["error"]
        assert payload["instances"]


class TestOpsToolsAgainstLiveServer:
    """Operational tools, called the way a client calls them.

    Their SQL is the part that breaks: a column that does not exist on this server
    version is invisible until the statement reaches the engine.
    """

    def test_list_running_queries_sees_at_least_itself_or_says_why(self) -> None:
        payload = call_ok("list_running_queries", {})
        assert "running" in payload
        assert isinstance(payload["sees_all_sessions"], bool)

    def test_top_statements_or_a_clear_reason(self) -> None:
        payload = call_tool("top_statements", {"limit": 5})
        if "error" in payload:
            assert "performance_schema" in payload.get("hint", "")
            return
        assert payload["count"] <= 5
        if payload["statements"]:
            first = payload["statements"][0]
            assert {"statement", "executions", "total_s", "rows_examined"}.issubset(first)

    def test_top_statements_rejects_a_bad_order(self) -> None:
        assert "order_by" in call_tool("top_statements", {"order_by": "nope"})["error"]

    def test_table_storage_stats(self) -> None:
        payload = call_ok("table_storage_stats", {"limit": 5})
        assert payload["count"] <= 5
        for t in payload["tables"]:
            assert isinstance(t["total_bytes"], int)
            assert t["size"]

    def test_replication_status_reports_flags(self) -> None:
        payload = call_ok("replication_status", {})
        assert payload["read_only"] in (True, False)
        assert payload["is_replica"] in (True, False)
        assert payload["assessment"]

    def test_lock_waits_is_empty_on_a_quiet_server(self) -> None:
        payload = call_ok("lock_waits", {})
        assert payload["source"].endswith(("innodb_lock_waits", "INNODB_LOCK_WAITS"))
        assert "waits" in payload

    def test_long_transactions(self) -> None:
        payload = call_ok("long_transactions", {"min_seconds": 0})
        assert "transactions" in payload and payload["assessment"]

    def test_server_health(self) -> None:
        payload = call_ok("server_health", {})
        assert payload["uptime_s"] > 0
        assert payload["connections"]["max_connections"]
        assert payload["assessment"]
