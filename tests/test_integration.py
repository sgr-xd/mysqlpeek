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

    def test_time_cap_stops_a_long_read(self, registry: InstanceRegistry) -> None:
        # Not SLEEP() or BENCHMARK(): MySQL lets those absorb the interrupt and return
        # quietly, so they prove nothing. A real read that is killed mid-way raises
        # ER_QUERY_TIMEOUT. The join-size cap is lifted for this connection so the
        # time cap is the one that fires.
        from dataclasses import replace

        from mysqlpeek.config import Limits
        from mysqlpeek.connection import Connection

        base = registry.get().config
        slow = Connection(
            replace(base, limits=Limits(max_execution_time=1, max_join_size=10**15))
        )
        try:
            with pytest.raises(QueryError, match="execution cap"):
                slow.execute(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS a, "
                    "information_schema.COLUMNS b, information_schema.COLUMNS c"
                )
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

        base = registry.get().config
        tight = Connection(replace(base, limits=Limits(max_join_size=10)))
        try:
            with pytest.raises(QueryError, match="max_join_size"):
                tight.execute(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS a, "
                    "information_schema.COLUMNS b, information_schema.COLUMNS c"
                )
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

    def test_missing_database_is_a_question_not_a_guess(self) -> None:
        payload = call_tool("list_tables", {})
        if os.environ.get("MYSQL_DATABASE"):
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
