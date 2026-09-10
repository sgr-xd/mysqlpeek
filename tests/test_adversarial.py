"""Every statement here must be rejected. A failure in this file blocks release.

The list is append-only: when a bypass is found, it gets a line here before it gets a
fix in `rules.py`.
"""

from __future__ import annotations

import pytest

from mysqlpeek.config import Limits
from mysqlpeek.policy import PolicyEngine

ENGINE = PolicyEngine(Limits())

MUST_BLOCK = [
    # --- direct writes and DDL -------------------------------------------------
    "DROP TABLE users",
    "DROP DATABASE shop",
    "INSERT INTO events VALUES (1)",
    "INSERT INTO events SELECT * FROM other",
    "REPLACE INTO events VALUES (1)",
    "UPDATE events SET a = 1",
    "DELETE FROM events WHERE 1=1",
    "TRUNCATE TABLE events",
    "CREATE TABLE x (a INT)",
    "CREATE TEMPORARY TABLE x AS SELECT 1",
    "ALTER TABLE events ADD COLUMN x INT",
    "RENAME TABLE a TO b",
    "LOAD DATA INFILE '/tmp/x' INTO TABLE t",
    "ANALYZE TABLE events",
    "OPTIMIZE TABLE events",
    # --- session, locks, procedures and server state ---------------------------
    "SET SESSION sql_select_limit = 999999999",
    "SET GLOBAL read_only = 0",
    "SET @x = 1",
    "USE other_database",
    "CALL some_procedure()",
    "DO SLEEP(5)",
    "LOCK TABLES events WRITE",
    "HANDLER events OPEN",
    "PREPARE s FROM 'DROP TABLE t'",
    "FLUSH TABLES",
    "KILL 42",
    "START TRANSACTION",
    "COMMIT",
    "GRANT SELECT ON *.* TO someone",
    "REVOKE SELECT ON *.* FROM someone",
    "SHUTDOWN",
    # --- statement stacking ----------------------------------------------------
    "SELECT 1; DROP TABLE users",
    "SELECT 1 ; INSERT INTO t VALUES (1)",
    "SELECT /* comment */ 1; DROP TABLE t",
    "SELECT 1 -- harmless\nDROP TABLE t",
    "SELECT 1 # harmless\nDROP TABLE t",
    # --- executable comments and cap-raising hints -----------------------------
    "SELECT 1 /*! ; DROP TABLE t */",
    "/*!50000 DROP TABLE t */ SELECT 1",
    "SELECT /*!80000 SLEEP(10) */",
    "SELECT /*+ MAX_EXECUTION_TIME(9999999) */ * FROM events",
    "SELECT /*+ SET_VAR(sql_select_limit = 999999999) */ * FROM events",
    "SELECT /*+ SET_VAR(sql_big_selects=1) */ * FROM events",
    # --- locking reads (take locks even inside a READ ONLY transaction) --------
    "SELECT * FROM events FOR UPDATE",
    "SELECT * FROM events FOR SHARE",
    "SELECT * FROM events LOCK IN SHARE MODE",
    "SELECT * FROM events WHERE id = 1 LIMIT 1 FOR UPDATE",
    # --- reading the filesystem, burning time, holding locks -------------------
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT SLEEP(100)",
    "SELECT BENCHMARK(100000000, MD5('x'))",
    "SELECT GET_LOCK('x', 100)",
    "SELECT RELEASE_LOCK('x')",
    "SELECT MASTER_POS_WAIT('f', 1)",
    "SELECT SYS_EXEC('id')",
    # --- exfiltration and variables --------------------------------------------
    "SELECT * FROM events INTO OUTFILE '/tmp/dump.csv'",
    "SELECT * FROM events INTO DUMPFILE '/tmp/dump'",
    "SELECT 1 INTO @x",
    # --- credential and grant tables -------------------------------------------
    "SELECT * FROM mysql.user",
    "SELECT authentication_string FROM mysql . user",
    "SELECT * FROM `mysql`.`user`",
    "SELECT * FROM mysql.`user`",
    "SELECT * FROM mysql.db",
    "SELECT * FROM mysql.global_grants",
    "SELECT * FROM mysql.slave_master_info",
    "SELECT * FROM mysql.general_log",
    "SHOW CREATE USER 'root'@'localhost'",
    # --- degenerate input ------------------------------------------------------
    "",
    "   ",
    "-- just a comment",
    "# just a comment",
    "/* only a block comment */",
]


@pytest.mark.parametrize("sql", MUST_BLOCK, ids=lambda s: (s[:48] or "<empty>").replace("\n", " "))
def test_must_block(sql: str) -> None:
    decision = ENGINE.evaluate(sql)
    assert not decision.allowed, f"policy allowed a dangerous statement: {sql!r}"
    assert decision.violations, "a rejection must explain itself"


FALSE_POSITIVES = [
    # Names and functions that merely resemble the blocked ones.
    "SHOW CREATE TABLE events",
    "SHOW CREATE VIEW v",
    "SELECT sleep_ms, benchmark_id FROM runs",
    "SELECT * FROM lock_audit",
    "SELECT load_file_name FROM uploads",
    "SELECT * FROM events WHERE mode = 'FOR UPDATE'",
    "SELECT `user`.name FROM `user`",
    "SELECT * FROM shop.user",
    "SELECT * FROM mysqlx.user",
    "SELECT * FROM users u WHERE u.db = 'mysql'",
]


@pytest.mark.parametrize("sql", FALSE_POSITIVES, ids=lambda s: s[:44])
def test_rules_do_not_overreach(sql: str) -> None:
    decision = ENGINE.evaluate(sql)
    assert decision.allowed, f"policy wrongly blocked: {sql!r} -> {decision.violations}"
