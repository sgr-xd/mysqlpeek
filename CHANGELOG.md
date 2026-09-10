# Changelog

All notable changes to mysqlpeek. Versions follow [semantic versioning](https://semver.org).

The version must be bumped for **any** shipped change, including documentation — the
plugin cache is keyed by version, so an unchanged version means installed copies silently
keep the previous build.

## [Unreleased]

Planned before 0.1.0: the ops tools (running queries, statement digests, replication
status, lock waits, long transactions); the `mysql-query-craft` and
`mysql-instance-health` skills.

### Added

- `validate_query`, `estimate_query_cost` and `explain_plan`, all on `EXPLAIN`, so a query
  is costed before it runs. `estimate_query_cost` walks `EXPLAIN FORMAT=JSON` in both the
  MySQL and MariaDB shapes, maps each alias back to its table for a share-of-table figure,
  and reports `query_cost` where the engine provides one. `explain_plan` prefers
  `FORMAT=TREE` and falls back to the classic table on MySQL 5.7 and MariaDB.
- `profile_column`: nulls, distinct count, range and most common values over a bounded
  sample, refusing an unknown column before scanning anything.
- Live tests now run against MySQL 5.7, 8.4, 9.3 and MariaDB 11. Three findings from
  them shaped the tests: `SLEEP()` and `BENCHMARK()` absorb the time-cap kill and return
  quietly; MySQL 8 and MariaDB answer `COUNT(*)` over a cross join as a product of row
  counts without touching a row; MySQL 5.7 estimates information_schema tables at two
  rows, so the join-size cap never fires on them.

## [0.0.1]

Skeleton release, not yet published to PyPI.

- Single-instance configuration from `MYSQL_*` variables, or several instances from a
  profiles file whose passwords must be `${env:}`, `${file:}` or `${cmd:}` references.
- Connection layer on PyMySQL: every session is placed in a `READ ONLY` transaction with
  `max_execution_time` / `max_statement_time`, `sql_select_limit`, `max_join_size` and
  `sql_big_selects = 0` applied before any query, and refused if any guard fails.
- Statement policy for MySQL: single SELECT/WITH/SHOW/DESCRIBE/EXPLAIN; DML/DDL, `INTO`,
  locking reads, `LOAD_FILE`/`SLEEP`/`BENCHMARK`/lock functions, executable comments,
  cap-raising optimizer hints and the `mysql.*` credential tables rejected — backticked
  identifiers included. LIMIT appended or clamped.
- Tools: `server_info`, `list_instances`, `list_databases`, `list_tables`, `describe_table`,
  `list_indexes`, `show_create_table`, `run_select_query` (with `rows_examined` and
  `query_cost`), `sample_rows`.
