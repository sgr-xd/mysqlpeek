---
name: mysql-query-craft
description: Write MySQL and MariaDB queries that stay cheap, and check what one will cost before running it. Use when composing, reviewing or debugging a specific SQL statement against MySQL or MariaDB — choosing which columns to filter on, whether an index will serve a WHERE, JOIN or ORDER BY, why a query examines far more rows than it returns, or reading EXPLAIN output to decide whether a statement is safe to run on a large table. Does NOT cover the health of the server — replication lag, lock waits, long transactions, connection pressure, what is running right now: use mysql-instance-health for those.
license: Apache-2.0
metadata:
  version: "1.0.0"
---

# MySQL query craft

MySQL rarely refuses a bad query. It examines a million rows to return ten, takes a few
seconds, and moves on — and on a production replica those seconds are someone else's
latency. So the discipline is to find out what a query will examine *before* running it.

## The loop

**Explore → estimate → fix → run.** Skipping the estimate is the mistake this skill exists
to prevent.

1. **`list_tables`** — note `approx_rows` and `primary_key` so you know what "expensive"
   means for this table. Then **`list_indexes`** for the table you will filter: the column
   *order* inside each index is what decides whether your WHERE can use it.
2. **`estimate_query_cost`** — reports, per table, the `access_type`, the `key` chosen,
   `rows_examined_per_scan` and `fraction_of_table`. Nothing is read to produce this.
3. If the verdict is **`full_scan`** or **`heavy`**, fix the query (below) and estimate
   again.
4. **`run_select_query`** once the verdict is `selective` — or once you have accepted that a
   scan is genuinely what you need. The response carries `rows_examined`: compare it with
   the estimate, and with `row_count`. A thousand examined for one returned is the
   signature of a missing index.

Use `validate_query` when you are unsure the SQL is even legal. It runs `EXPLAIN`, which
resolves every table and column name, and reads no data — never validate by running a
query with `LIMIT 1`.

## Reading `access_type`

The single most informative field in the estimate:

| access_type | Meaning | Verdict |
|---|---|---|
| `const`, `eq_ref` | one row by primary or unique key | ideal |
| `ref` | index lookup on a non-unique key | good |
| `range` | index range scan (`BETWEEN`, `>`, `IN`, `LIKE 'abc%'`) | good if the range is narrow |
| `index` | reads the *whole index* in order | a scan wearing an index |
| `ALL` | reads the whole table | a scan |

`index` is the one that misleads: it shows a `key`, so it looks indexed, but it touches
every entry. Judge by `fraction_of_table`, not by whether a key is named.

## What makes a query cheap

**Filter on the leading column of an index.** An index on `(tenant_id, created_at)` serves
`WHERE tenant_id = 7`, and `WHERE tenant_id = 7 AND created_at > …`, but not
`WHERE created_at > …` alone — the leading column is missing, and MySQL cannot skip into
the middle of the index.

```sql
-- KEY idx (tenant_id, created_at)
WHERE tenant_id = 7 AND created_at >= NOW() - INTERVAL 1 DAY   -- range on the index
WHERE created_at >= NOW() - INTERVAL 1 DAY                     -- ALL: every tenant, every row
```

**Keep the indexed column bare.** `WHERE DATE(created_at) = CURDATE()` cannot use an index
on `created_at`; `WHERE created_at >= CURDATE() AND created_at < CURDATE() + INTERVAL 1 DAY`
can. The same applies to `LOWER(email) = …`, `id + 0 = …`, and implicit casts — comparing a
`VARCHAR` column to a number makes MySQL cast every row.

**Match the ORDER BY to an index.** If the rows come out of the index already in the order
you asked for, there is no sort. Otherwise the plan shows `using_filesort`, which is a
separate pass over every matching row — fine for a hundred rows, not for a million.
`estimate_query_cost` lists it under `warnings`.

**Prefer `IN (…)` lists and `UNION ALL` to `OR` across different columns.** `WHERE a = 1 OR
b = 2` usually scans; two indexed lookups joined by `UNION ALL` do not.

**Bound the result.** A `LIMIT` is appended if you omit one, but a `LIMIT` without an
`ORDER BY` on an indexed column still examines everything before returning the first
hundred. `ORDER BY indexed_col LIMIT 100` stops early.

**Covering indexes.** If every column the query touches is inside the index, MySQL never
reads the table row (`Using index` in the plan). Selecting three columns instead of `*`
is often what makes an index covering.

## What makes a query expensive

**Joins without an index on the join column.** The plan shows `ALL` on the inner table
and, on MySQL 8, a hash join; on older servers a block nested loop. `estimate_query_cost`
reports both tables, and the inner one carries the damage.

**`SELECT DISTINCT` and `GROUP BY` on unindexed high-cardinality columns.** They build a
temporary table; if it does not fit in memory it goes to disk (`using_temporary_table`
in the warnings, and `tmp_disk_tables` in `top_statements`).

**Leading-wildcard `LIKE`.** `LIKE '%foo'` cannot use a B-tree index. `LIKE 'foo%'` can.

**Correlated subqueries in the SELECT list** run once per outer row. Rewrite as a JOIN.

**`COUNT(*)` on a large InnoDB table** is a full index scan every time — there is no
stored row count. `list_tables` gives `approx_rows` for free; use it when approximate is
enough.

## Reading `explain_plan`

On MySQL 8.0.16+ the plan is a tree, read top-down: each line is an operation, and
`(cost=… rows=…)` is the optimiser's estimate for it. Look for `Table scan on`, `Index scan
on`, `Filter:` lines that follow a scan (the filter was not pushed into the index) and
`Sort:` lines (filesort). On MySQL 5.7 and MariaDB the plan is the classic table: `type`
is the access type above, `Extra` carries `Using filesort`, `Using temporary`, `Using
index` and `Using where`.

`uses_index` in the response is true only when every base table is reached through an
index lookup. A named `key` with `access_type: index` does not count.

Estimates come from index statistics and are approximate — `approx_rows` on MySQL 8 can be
a day stale (`information_schema_stats_expiry`). Treat them as an order of magnitude, not a
promise, and check `rows_examined` after running.

## More than one instance

Call `list_instances` first. If it returns more than one, pass `instance` explicitly on
every call rather than relying on the default — the caps differ per instance, and so does
the data. There is no "current" instance to switch to; the argument is the only thing that
decides where a query goes. Every response tells you which instance answered it, so check
that field when a result looks unfamiliar.

## Notes on this connection

Every tool here is read-only, and the server applies row, join-size and time caps to each
query. A missing `LIMIT` is added automatically and an oversized one is clamped — check
`executed_sql` in the response to see what actually ran, and `policy_notes` for what
changed. A query the optimiser expects to examine more rows than the instance's
`max_join_size` is refused before it starts; the error says so, and the fix is a tighter
filter, not a retry.

A `truncated: true` in the result means you hit the limit and there are more rows.
Aggregate rather than paginating: `COUNT(*)` with a filter, `GROUP BY`, or a narrower
`WHERE` almost always answers the question better than fetching page two.
