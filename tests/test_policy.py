"""Policy engine behaviour on legitimate queries.

The false-positive cases matter as much as the blocking ones: a guard that rejects
ordinary SQL gets switched off, and then it guards nothing.
"""

from __future__ import annotations

import pytest

from mysqlpeek.config import Limits
from mysqlpeek.policy import PolicyEngine
from mysqlpeek.policy import rules as policy_rules

CAPS = Limits(default_limit=100, max_limit=1000)
ENGINE = PolicyEngine(CAPS)


MUST_ALLOW = [
    "SELECT 1",
    "SELECT COUNT(*) FROM events WHERE ts > NOW() - INTERVAL 1 DAY",
    "WITH recent AS (SELECT * FROM events LIMIT 10) SELECT * FROM recent",
    "WITH RECURSIVE n AS (SELECT 1 AS x UNION ALL SELECT x + 1 FROM n WHERE x < 5) SELECT * FROM n",
    "SHOW TABLES",
    "SHOW CREATE TABLE events",
    "SHOW FULL PROCESSLIST",
    "SHOW ENGINE INNODB STATUS",
    "SHOW GRANTS",
    "DESCRIBE events",
    "DESC events",
    "EXPLAIN SELECT 1",
    "EXPLAIN FORMAT=JSON SELECT * FROM events WHERE id = 1",
    "EXPLAIN ANALYZE SELECT * FROM events WHERE id = 1",
    # ordinary functions and clauses that share a spelling with a statement
    "SELECT TRUNCATE(3.7, 0) AS t",
    "SELECT REPLACE(name, 'a', 'b') FROM events",
    "SELECT CAST(payload AS CHAR CHARACTER SET utf8mb4) FROM events",
    "SELECT FIND_IN_SET('a', tags) FROM events",
    "SELECT * FROM events WHERE JSON_CONTAINS(doc, '1', '$.set')",
    "SELECT 1 /*+ INDEX(events idx_ts) */",
    # a write word inside a string literal is data, not a statement
    "SELECT 'drop table users' AS note",
    "SELECT * FROM events WHERE action = 'DELETE'",
    'SELECT * FROM events WHERE action = "INSERT INTO x"',
    # ... and inside a comment
    "SELECT 1 -- drop table users",
    "SELECT 1 # drop table users",
    "SELECT /* insert into */ 1",
    # `--` without a following space is arithmetic in MySQL, not a comment
    "SELECT 3--1",
    # column names that merely start with or contain a blocked word
    "SELECT create_time, update_time FROM information_schema.tables",
    "SELECT drop_priv_count FROM some_table",
    "SELECT * FROM `drop_log`",
    "SELECT updated_at, deleted_at FROM users",
    # non-sensitive system schemas stay available for diagnosis
    "SELECT * FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'shop'",
    "SELECT * FROM performance_schema.events_statements_summary_by_digest",
    "SELECT * FROM sys.schema_unused_indexes",
    "SELECT * FROM mysql.time_zone_name",
    "SELECT * FROM mysql.innodb_table_stats",
    "SELECT @@version, @@read_only",
]


@pytest.mark.parametrize("sql", MUST_ALLOW, ids=lambda s: s[:48].replace("\n", " "))
def test_must_allow(sql: str) -> None:
    decision = ENGINE.evaluate(sql)
    assert decision.allowed, f"policy rejected a legitimate query {sql!r}: {decision.violations}"


class TestLimitEnforcement:
    def test_appends_limit_when_absent(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events")
        assert decision.sql == "SELECT * FROM events LIMIT 100"
        assert decision.effective_limit == 100
        assert decision.notes

    def test_leaves_acceptable_limit_alone(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events LIMIT 5")
        assert decision.sql == "SELECT * FROM events LIMIT 5"
        assert decision.effective_limit == 5
        assert not decision.notes

    def test_clamps_oversized_limit(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events LIMIT 500000")
        assert decision.sql == "SELECT * FROM events LIMIT 1000"
        assert decision.effective_limit == 1000
        assert "clamped" in " ".join(decision.notes)

    def test_offset_form_counts_the_second_number(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events LIMIT 20, 5000")
        assert decision.effective_limit == 1000
        assert decision.sql.endswith("LIMIT 20, 1000")

    def test_limit_offset_keyword_form_preserves_offset(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events LIMIT 5000 OFFSET 20")
        assert decision.effective_limit == 1000
        assert decision.sql == "SELECT * FROM events LIMIT 1000 OFFSET 20"

    def test_subquery_limit_does_not_count_as_the_outer_limit(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM (SELECT * FROM events LIMIT 5) AS x")
        assert decision.sql.endswith("LIMIT 100")
        assert decision.effective_limit == 100

    def test_self_bounded_statements_get_no_limit(self) -> None:
        for sql in ("SHOW TABLES", "DESCRIBE events", "EXPLAIN SELECT 1"):
            decision = ENGINE.evaluate(sql)
            assert decision.effective_limit is None
            assert "LIMIT" not in decision.sql.upper()

    def test_trailing_semicolon_is_stripped_before_appending(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events;")
        assert decision.sql == "SELECT * FROM events LIMIT 100"

    def test_explain_wrapper_skips_limit_enforcement(self) -> None:
        decision = ENGINE.evaluate("SELECT * FROM events", enforce_limit=False)
        assert decision.allowed
        assert decision.sql == "SELECT * FROM events"
        assert decision.effective_limit is None


class TestLiteralMasking:
    def test_masking_preserves_length(self) -> None:
        sql = "SELECT 'abc' FROM t"
        assert len(policy_rules.mask_literals(sql)) == len(sql)

    def test_masking_preserves_newlines(self) -> None:
        sql = "SELECT 1 /* a\nb */ FROM t"
        assert policy_rules.mask_literals(sql).count("\n") == sql.count("\n")

    def test_identifier_preserving_mask_strips_backticks(self) -> None:
        assert "mysql.user" in policy_rules.mask_strings_and_comments("SELECT * FROM `mysql`.`user`")

    def test_double_dash_needs_a_space_to_comment(self) -> None:
        # MySQL rule: `-- ` comments, `--1` is arithmetic.
        assert policy_rules.mask_literals("SELECT 3--1") == "SELECT 3--1"
        assert policy_rules.mask_literals("SELECT 3 -- x").rstrip() == "SELECT 3"


class TestDecision:
    def test_rejection_message_lists_violations(self) -> None:
        decision = ENGINE.evaluate("DROP TABLE t")
        assert not decision.allowed
        message = decision.rejection_message()
        assert "rejected" in message.lower()
        assert "DROP" in message

    def test_query_hash_is_stable(self) -> None:
        a = ENGINE.evaluate("SELECT 1 LIMIT 1")
        b = ENGINE.evaluate("SELECT 1 LIMIT 1")
        assert a.query_hash == b.query_hash
        assert len(a.query_hash) == 16
