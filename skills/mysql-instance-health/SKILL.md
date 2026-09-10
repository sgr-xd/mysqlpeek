---
name: mysql-instance-health
description: Run a one-shot health check on a MySQL or MariaDB instance and report what is wrong. Use when asked how the database is doing, whether it is healthy, why it feels slow, or to check replication lag, lock waits, long-running transactions, connection pressure, buffer pool hit rate, temp tables spilling to disk, the InnoDB history list, or which statements cost the server most. Also use before and after a failover, a version upgrade or a schema migration. Does NOT cover writing or tuning a specific query: use mysql-query-craft for that.
license: Apache-2.0
metadata:
  version: "1.0.0"
---

# MySQL instance health

One pass, every check, one verdict. This skill answers "is this instance healthy right
now" — it holds no state, keeps no history, and schedules nothing. Everything it needs comes
from server status tables in a single run.

## How to run it

**1. Find out what you are talking to.** Call `list_instances` first. Everything below runs
once *per instance*, passing `instance=` explicitly on every call — never rely on the
default. A single-server setup returns one entry named `default`; that is the normal case
and the checks are unchanged.

If the deployment is a primary with replicas, check each one: a replica's lag is invisible
from the primary, and a primary's write load is invisible from a replica.

**2. Run the checks below**, in order. Each is one tool call.

**3. Report** using the format at the end.

Do not stop at the first problem. A long-open transaction, a growing history list and a
lock wait are usually the same story, and you can only tell by having all three.

### These calls do not need costing

The sibling `mysql-query-craft` skill teaches explore → estimate → run. That discipline is
for user tables. Everything here reads server metadata — already bounded, no index to
miss. Run the tools directly.

## The checks

### 1. Replication

`replication_status` — for every instance.

- `is_replica: false` on a server you believed was a replica is itself a finding.
- `io_running` or `sql_running` anything but `Yes` — replication has **stopped**. Serious
  regardless of any other number; `last_io_error` / `last_sql_error` say why.
- `seconds_behind_source` over 60 is worth reporting; over 300 is serious. It is measured
  from the timestamp of the event being applied, so a replica that is *idle* because the
  source is idle shows 0, and a replica with a stopped SQL thread shows NULL, not a large
  number. Read it together with the thread flags.
- `read_only` / `super_read_only` false on a replica means an application can write to
  it and silently diverge. Report it even when everything else is fine.

### 2. Lock waits

`lock_waits` — a healthy server returns nothing. Anything listed is a session blocked
*right now*; `blocking_pid` is who to look at. A wait older than a few seconds on an OLTP
system is a user staring at a spinner.

### 3. Long transactions

`long_transactions` with the default `min_seconds` (10), then again with `min_seconds=300`
to separate slow from stuck.

- An open transaction with `query_sample: null` is idle inside the transaction — an
  application that began work and never committed. It holds every lock it took and pins
  undo history for every row it read. The most common cause of both a growing history
  list and lock waits, and the hardest to see without this tool.
- `rows_modified` above zero on a minutes-old transaction is uncommitted change that will
  either land all at once or roll back for as long as it ran.

### 4. Server counters

`server_health` — read these in this order:

- `connections.current` against `max_connections`: above 80% and the next spike refuses
  logins. `aborted_connects` climbing means clients failing to authenticate or timing out.
- `buffer_pool.hit_rate` below 0.99 on a server with real traffic means the working set
  does not fit in memory and reads are going to disk. `wait_free` above zero means InnoDB
  had to evict pages under pressure to make room.
- `queries.tmp_disk_fraction` above 0.25 means a quarter of temporary tables spill to disk
  — GROUP BY / DISTINCT / UNION over columns without a supporting index. `top_statements`
  with `order_by="rows_examined"` usually names the culprits.
- `innodb_history_list_length` in the millions means purge cannot keep up. Cross-check 3:
  a long-open transaction is almost always why.
- `locks.row_lock_current_waits` — the same thing check 2 shows, as a number.

Counters are cumulative since `uptime_s` ago. Turn them into rates before judging:
`select_scan / uptime_s` is scans per second, which means something; `select_scan` alone
does not.

### 5. Load right now

`list_running_queries` — anything over 60 seconds deserves a mention; over 300, name it with
its `id` and `user` so someone can act on it. This is a point-in-time sample: a quiet
result means nothing was running at this instant, not that the server is idle. If
`sees_all_sessions` is false, say so — you saw only your own connection.

### 6. What costs the most

`top_statements` three times: `order_by="total_time"` (where the server's time goes),
`order_by="rows_examined"` (which shapes read the most), and `no_index_only=true` (which
ran without an index at all). `examined_per_row_sent` in the thousands is a missing index
with a statement attached — hand it to `mysql-query-craft`.

`first_seen` says how long these counters have accumulated. A digest table truncated an
hour ago tells you about the last hour; one from a server up for a year tells you about the
year, including problems already fixed.

### 7. Storage

`table_storage_stats` — the biggest tables, `fragmentation` (space InnoDB holds but does not
use), `index_to_data` above 1 (more index than data: candidates for `list_indexes` and a
look at redundant ones), and `auto_increment_headroom` — below 0.1 on an `INT` column is a
table that will stop accepting inserts, and the failure arrives all at once.

## Reporting

Lead with the verdict, then the evidence. Someone should be able to stop reading after the
first line and still know whether to worry.

```
MySQL health — <N> instance(s) checked, <UTC timestamp>

VERDICT: healthy | degraded | serious

Serious
  • <finding, with the number that makes it serious and the instance it is on>

Worth watching
  • <finding>

Checked and clean
  replication · locks · transactions · connections · buffer pool · temp tables · load · storage
```

Rules for the report:

- **Name the instance.** Every response carries the `instance` that answered it. A finding
  without an instance attached is not actionable.
- **Quote the number.** "Replica 412s behind on `prod-replica`" beats "replication is
  lagging".
- **List what was clean.** The absence of findings is a result, and without it the reader
  cannot tell a healthy server from a check that failed to run.
- **Say what you could not check.** A missing PROCESS privilege, performance_schema off, an
  unreachable instance — all of it goes in the report. Silence reads as "fine".
- **Connect findings that share a cause.** Idle transaction → history list growing → lock
  waits → slow queries is one incident, and reporting it as four is how the cause gets
  missed.
- **Do not diagnose past the evidence.** These checks show *what* is wrong now. Why it
  started usually needs history this skill does not have. Offer the hypothesis, label it
  as one.

## What this skill does not do

- **It does not fix anything.** Every tool here is read-only, and remediation — `KILL`, a
  `STOP REPLICA`, an `ALTER TABLE`, a restart — is a human decision made with a
  write-capable connection this server deliberately does not have. Recommend the action,
  never attempt it.
- **It does not compare against history.** No previous run to diff against, so "growing",
  "since yesterday" and "new" are not claims you can make from one pass. If a trend
  matters, say what a second run would settle.
- **It does not read the error log or the slow log file.** Both live on the server's disk,
  outside SQL. `top_statements` is the in-database view of the same information.
