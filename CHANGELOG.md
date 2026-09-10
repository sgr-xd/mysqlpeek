# Changelog

All notable changes to mysqlpeek. Versions follow [semantic versioning](https://semver.org).

The version must be bumped for **any** shipped change, including documentation — the
plugin cache is keyed by version, so an unchanged version means installed copies silently
keep the previous build.

## [Unreleased]

Planned before 0.1.0: `validate_query`, `estimate_query_cost` and `explain_plan` on
`EXPLAIN FORMAT=JSON`; `profile_column`; the ops tools (running queries, statement digests,
replication status, lock waits, long transactions); the `mysql-query-craft` and
`mysql-instance-health` skills.

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
