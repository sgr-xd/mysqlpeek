# mysqlpeek

A read-only [MySQL](https://www.mysql.com) / [MariaDB](https://mariadb.org) MCP server that
lets an agent explore your schema, **cost a query before running it**, and pick which of
several instances it is talking to — per call, never by a mode it can forget it is in.

Most MySQL MCP servers give a model one blunt instrument, "run this SQL", on one host, with a
regular expression standing between it and `DROP TABLE`. mysqlpeek is different on three
counts:

- **Writes are refused by the engine, not only by a parser.** Every connection runs inside a
  `READ ONLY` transaction with server-side caps on execution time, result rows and rows
  examined. A statement that gets past the parser still cannot write, and cannot run away.
- **Cost before execution.** `EXPLAIN` tells you how many rows a query would examine before a
  single one is read, and `max_join_size` makes the server refuse a query the optimiser
  expects to be too big — before it starts.
- **Several instances, one server.** A profiles file names your prod replica, staging and
  local boxes; every tool takes `instance=`, and every response says which one answered.

Works with MySQL 5.7.8+, MySQL 8 and 9, MariaDB 10.1+, and the managed flavours that keep
the standard session variables (RDS, Cloud SQL, Azure Database for MySQL). MariaDB reports
no `query_cost` and no tree-shaped plan; everything else behaves the same.

## Install

### As a Claude Code plugin (recommended)

```
/plugin marketplace add sgr-xd/mysqlpeek
/plugin install mysqlpeek
```

Claude Code then asks for four things:

| Field | Example |
|---|---|
| **Server** | `db.internal`, `host:3307`, or `mysqls://db.example.com` for TLS |
| **Username** | `readonly_user` |
| **Password** | masked; goes to your operating system's secure storage, never to a settings file |
| **Database** | optional — leave empty and every call names its own |

Port and TLS come from whatever you put in Server, so there is nothing else to set.

This also installs two skills: `mysql-query-craft`, which teaches the
explore → estimate → run discipline the tools are built around, and
`mysql-instance-health`, a one-shot sweep of replication, lock waits, long transactions,
connections, buffer pool, temp tables and top statements that reports whether the
instance is healthy right now.

### As a standalone MCP server

```bash
claude mcp add mysqlpeek --scope user \
  -e MYSQL_HOST=db.internal \
  -e MYSQL_USER=readonly_user \
  -e MYSQL_PASSWORD_FILE=$HOME/.config/mysqlpeek/password \
  -e MYSQL_DATABASE=shop \
  -- uvx mysqlpeek
```

Works with any MCP client, not only Claude Code — point it at `uvx mysqlpeek` over stdio.

### From source

```bash
git clone https://github.com/sgr-xd/mysqlpeek && cd mysqlpeek
uv venv && uv pip install -e ".[dev]"
pytest -m "not integration"      # unit tests, no database needed
pytest                           # adds live tests against a real server
```

## Configure

Only needed for the standalone and from-source paths; the plugin asks instead.

```bash
export MYSQL_HOST=db.internal                     # or host:port, or mysqls://host
export MYSQL_USER=readonly_user
export MYSQL_PASSWORD_FILE=~/.config/mysqlpeek/password   # preferred over MYSQL_PASSWORD
export MYSQL_DATABASE=shop                        # optional
```

| You enter | Host | Port | TLS |
|---|---|---|---|
| `db.internal` | db.internal | 3306 | no |
| `db.internal:3307` | db.internal | 3307 | no |
| `mysql://db.internal:3306` | db.internal | 3306 | no |
| `mysqls://db.example.com` | db.example.com | 3306 | yes |

`MYSQL_PORT`, `MYSQL_SSL`, `MYSQL_SSL_CA` and `MYSQL_SSL_VERIFY` still work and take
precedence if you set them. The password is never accepted as a command-line argument —
arguments leak through shell history and `ps`.

| Variable | Purpose |
|---|---|
| `MYSQLPEEK_PROFILES` | Path to a multi-instance profiles file — see below |
| `MYSQLPEEK_AUDIT_LOG` | Path to a JSONL record of every query decision |

## Tools

**Discovery** — schema exploration, no user SQL accepted:

| Tool | Purpose |
|---|---|
| `list_instances` | Configured instances, which is default, and each one's limits |
| `list_databases` | Schemas visible to your user, system ones flagged |
| `list_tables` | Tables with engine, approximate rows, size, primary key, partitioning |
| `describe_table` | Columns, types, nullability, key membership, charset, comments |
| `list_indexes` | Every index with its column order and cardinality |
| `show_create_table` | Full `CREATE TABLE` / `CREATE VIEW` DDL |

**Query**:

| Tool | Purpose |
|---|---|
| `run_select_query` | Execute a `SELECT` under enforced caps; reports `rows_examined` and the optimiser's `query_cost` |
| `sample_rows` | Preview rows from a table (SQL built server-side) |
| `profile_column` | One column's nulls, distinct count, range and most common values, over a bounded sample |

**Operations** — what the server is doing, and whether it is keeping up:

| Tool | Purpose |
|---|---|
| `list_running_queries` | Statements executing now, longest first (needs PROCESS to see other sessions) |
| `top_statements` | Statement digests from performance_schema ranked by time, count, rows examined or missing index |
| `table_storage_stats` | Tables by size with fragmentation, index-to-data ratio and auto-increment headroom |
| `replication_status` | Replica threads, lag, last error, `read_only` / `super_read_only` |
| `lock_waits` | Sessions blocked on row locks and who blocks them |
| `long_transactions` | Transactions open longer than N seconds, idle-in-transaction included |
| `server_health` | Connections, buffer pool hit rate, temp tables on disk, lock waits, history list |

**Cost & validation** — these read no table data:

| Tool | Purpose |
|---|---|
| `validate_query` | `EXPLAIN` — is the SQL legal, and do the names resolve? |
| `estimate_query_cost` | `EXPLAIN FORMAT=JSON` — per table: access type, index chosen, rows it would examine, share of the table; a `full_scan` / `heavy` / `selective` verdict |
| `explain_plan` | `EXPLAIN FORMAT=TREE` (MySQL 8.0.16+) or the classic table, plus whether every base table is reached through an index |

Every response names the instance that answered and the SQL actually executed, so a
rewritten `LIMIT` is visible rather than silent.

## Safety

mysqlpeek is read-only, enforced in three independent layers:

1. **Statement policy** — single statement only; must open with `SELECT`, `WITH…SELECT`,
   `SHOW`, `DESCRIBE` or `EXPLAIN`; DML/DDL keywords, `INTO OUTFILE`, locking reads
   (`FOR UPDATE`, `FOR SHARE`), `LOAD_FILE()`, `SLEEP()`, lock functions, executable
   comments (`/*! … */`) and cap-raising hints (`SET_VAR`, `MAX_EXECUTION_TIME`) are
   rejected. `mysql.user` and the other credential tables are refused, backticked or not.
   A missing `LIMIT` is appended; an oversized one is clamped.
2. **Session guards** — every connection is put into this state the moment it opens, and a
   connection where any guard fails is refused outright:

   | Guard | Statement |
   |---|---|
   | no writes | `SET SESSION TRANSACTION READ ONLY` |
   | time cap | `SET SESSION max_execution_time = …` (MariaDB: `max_statement_time`) |
   | result cap | `SET SESSION sql_select_limit = …` |
   | examined-rows cap | `SET SESSION max_join_size = …` with `sql_big_selects = 0` — the optimiser refuses a statement it expects to examine more rows than this, before reading any |

   Multi-statement execution is off at the protocol level, so `SELECT 1; DROP …` is a
   syntax error to the server.
3. **Database grants** — connect as a user with only `SELECT`. This is the layer that cannot
   be argued with, and the one you should not skip. It is also the only layer that stops
   server-state statements such as `SET GLOBAL`: the parser refuses them, but a
   `READ ONLY` transaction does not, so an account holding `SUPER` or
   `SYSTEM_VARIABLES_ADMIN` is one parser bug away from changing the server. Do not point
   mysqlpeek at `root`:

```sql
CREATE USER 'readonly_user'@'%' IDENTIFIED BY '…';
GRANT SELECT ON shop.* TO 'readonly_user'@'%';
-- and, if you want the ops tools:
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'readonly_user'@'%';
```

### Tuning the caps

| Variable | Default |
|---|---|
| `MYSQLPEEK_MAX_EXECUTION_TIME` | `30` (seconds) |
| `MYSQLPEEK_MAX_RESULT_ROWS` | `10000` |
| `MYSQLPEEK_MAX_JOIN_SIZE` | `100000000` (rows the optimiser may plan to examine) |
| `MYSQLPEEK_DEFAULT_LIMIT` | `100` |
| `MYSQLPEEK_MAX_LIMIT` | `10000` |

## Several instances

Point `MYSQLPEEK_PROFILES` at a JSON file, and every tool gains an optional `instance`
argument.

**The file holds references, never values** — so it is safe to commit:

```json
{
  "default": "prod-replica",
  "limits": { "max_limit": 5000 },
  "instances": {
    "prod-replica": {
      "description": "read replica of the primary",
      "host":     "mysqls://db-ro.example.com",
      "user":     "${env:PROD_MYSQL_USER:-readonly}",
      "password": "${cmd:vault kv get -field=password secret/mysql-prod}",
      "database": "shop",
      "limits":   { "default_limit": 25, "max_limit": 200, "max_join_size": 5000000 }
    },
    "staging": {
      "host":     "db-staging.example.com:3307",
      "password": "${file:~/.config/mysqlpeek/staging.pw}"
    },
    "local": {
      "host":     "127.0.0.1",
      "user":     "root",
      "password": "${env:LOCAL_MYSQL_PW:-}"
    }
  }
}
```

| Scheme | Example | Notes |
|---|---|---|
| `env` | `${env:PROD_MYSQL_PASSWORD}` | `${env:VAR:-default}` supplies a fallback |
| `file` | `${file:~/.config/mysqlpeek/prod.pw}` | Trailing newline stripped |
| `cmd` | `${cmd:op read op://vault/mysql-prod/password}` | stdout of a command, run without a shell |

A literal password in the file is rejected with an error pointing at the reference syntax.
A profiles file writable by anyone but its owner is refused, because a `${cmd:…}` reference
means the file decides what gets executed.

Limits layer: environment defaults → file-wide `limits` → per-instance `limits`. Connections
are lazy, so an unreachable instance does not stop the server starting. There is
deliberately no `use_instance` tool: selection is an argument on every call, and every
response carries the `instance` that answered it.

## Releasing

Three files carry the version and must agree: `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json` (`plugins[0].version`) and `pyproject.toml`. **Bump on
every shipped change**, documentation included — the plugin cache is keyed by version, so an
unchanged version means installed copies silently keep the previous build.

## License

Apache-2.0
