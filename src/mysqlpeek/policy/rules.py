"""Statement-shape rules.

Everything here is a *rejection* rule. Nothing in this module rewrites SQL — see
`limits.py` for the single exception (appending a LIMIT).

Keyword matching runs against a masked copy of the query in which comments and string
literals have been blanked out. Without that, `SELECT 'drop table users' AS note` would
be rejected, and `SELECT 1 -- \\nDROP TABLE t` would be accepted. Both matter.

These rules are the first of three layers, not the last. The connection runs every
statement inside a READ ONLY transaction with server-side caps (see `connection.py`),
and the account should hold only SELECT. A parser can be out-thought; a missing GRANT
cannot.
"""

from __future__ import annotations

import re

# Statements a read-only tool may legitimately issue. Because a query must begin with
# one of these AND may not contain a statement separator, no write statement can reach
# the server even if a write keyword survives the checks below. Every other MySQL
# statement head — SET, USE, CALL, DO, HANDLER, LOCK, LOAD, PREPARE, FLUSH, KILL,
# START/COMMIT, CREATE/ALTER/DROP, and so on — fails this one check.
_ALLOWED_HEADS = ("SELECT", "WITH", "SHOW", "DESC", "DESCRIBE", "EXPLAIN")

# Write/DDL words rejected anywhere in the query, as defence in depth behind the head
# check. Deliberately excluded because MySQL has ordinary functions, clauses or
# columns by these names, and rejecting them would break legitimate read queries:
#
#   TRUNCATE(x, d)   numeric function        REPLACE(s, a, b)   string function
#   CREATE           SHOW CREATE TABLE is the most useful read statement there is
#   SET              CAST(x AS CHAR CHARACTER SET utf8mb4); the SET statement is a head
#   LOAD / CALL / USE / LOCK / HANDLER / DO   only valid as a statement head, already closed
#
# UPDATE catches `SELECT ... FOR UPDATE` too, which takes row locks even inside a
# read-only transaction. INTO catches `SELECT ... INTO OUTFILE`, `INTO DUMPFILE` and
# `INTO @var`; there is no read-only use of INTO.
_BLOCKED_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "ALTER",
    "DROP",
    "RENAME",
    "GRANT",
    "REVOKE",
    "KILL",
    "INTO",
)

# Functions a read-only server must not offer. LOAD_FILE reads the server's disk;
# SLEEP and BENCHMARK burn server time on purpose; the lock functions block other
# sessions; the replication wait functions block until another server does something;
# SYS_EXEC / SYS_EVAL are the lib_mysqludf_sys UDFs that run programs.
_BLOCKED_FUNCTIONS = (
    "LOAD_FILE",
    "SLEEP",
    "BENCHMARK",
    "GET_LOCK",
    "RELEASE_LOCK",
    "RELEASE_ALL_LOCKS",
    "MASTER_POS_WAIT",
    "SOURCE_POS_WAIT",
    "WAIT_FOR_EXECUTED_GTID_SET",
    "WAIT_UNTIL_SQL_THREAD_AFTER_GTIDS",
    "SYS_EXEC",
    "SYS_EVAL",
    "SYS_SET",
)

# Tables in the `mysql` schema holding credentials, grants or replication passwords.
# The rest of the schema (time zones, help tables, statistics) stays available.
# general_log is included because it records statement text verbatim, CREATE USER
# and its password included.
_BLOCKED_MYSQL_TABLES = (
    "user",
    "db",
    "tables_priv",
    "columns_priv",
    "procs_priv",
    "proxies_priv",
    "global_grants",
    "role_edges",
    "default_roles",
    "password_history",
    "slave_master_info",
    "slave_relay_log_info",
    "general_log",
)

# Comments and quoted spans. MySQL: strings in ' or "; identifiers in backticks;
# `#` and `-- ` line comments (the `--` form needs whitespace after it, and
# `SELECT 1--1` is arithmetic); /* */ block comments.
_STRING_OR_COMMENT = re.compile(
    r"""
      '(?:[^'\\]|\\.|'')*'      # single-quoted string, with backslash and '' escapes
    | "(?:[^"\\]|\\.|"")*"      # double-quoted string
    | `(?:[^`]|``)*`            # backtick identifier
    | --(?:[ \t][^\n]*)?(?=\n|\Z)   # line comment: `-- ` or `--` at end of line
    | \#[^\n]*                  # line comment
    | /\*[\s\S]*?\*/            # block comment
    """,
    re.VERBOSE,
)
# The same without the identifier rule, for checks that must see through backticks.
_STRING_OR_COMMENT_KEEP_IDENTIFIERS = re.compile(
    r"""
      '(?:[^'\\]|\\.|'')*'
    | "(?:[^"\\]|\\.|"")*"
    | --(?:[ \t][^\n]*)?(?=\n|\Z)
    | \#[^\n]*
    | /\*[\s\S]*?\*/
    """,
    re.VERBOSE,
)

_BLOCKED_KEYWORD_RE = re.compile(r"\b(" + "|".join(_BLOCKED_KEYWORDS) + r")\b", re.IGNORECASE)
_BLOCKED_FUNCTION_RE = re.compile(
    r"\b(" + "|".join(_BLOCKED_FUNCTIONS) + r")\s*\(", re.IGNORECASE
)
# Matched on the identifier-preserving mask with backticks stripped, so
# `mysql`.`user` is seen for what it is.
_BLOCKED_MYSQL_TABLE_RE = re.compile(
    r"\bmysql\s*\.\s*(" + "|".join(_BLOCKED_MYSQL_TABLES) + r")\b", re.IGNORECASE
)
_SHOW_CREATE_USER_RE = re.compile(r"\bSHOW\s+CREATE\s+USER\b", re.IGNORECASE)
_LOCKING_READ_RE = re.compile(r"\bLOCK\s+IN\s+SHARE\s+MODE\b|\bFOR\s+SHARE\b", re.IGNORECASE)
# `/*! ... */` is not a comment to MySQL: the server executes what is inside it. It is
# rejected on the raw text, before masking, because masking would hide it.
_EXECUTABLE_COMMENT_RE = re.compile(r"/\*!")
# Optimizer hints that raise the caps set on the session. Checked on the raw text for
# the same reason: they live inside `/*+ ... */`, which masking blanks out.
_CAP_RAISING_HINT_RE = re.compile(r"\b(SET_VAR|MAX_EXECUTION_TIME)\b", re.IGNORECASE)
_HEAD_RE = re.compile(r"\s*([A-Za-z_]+)")


def _blank(match: re.Match[str]) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in match.group(0))


def mask_literals(sql: str) -> str:
    """Blank out comments and quoted spans, preserving length and line structure.

    Each masked span becomes spaces (newlines kept), so offsets in the masked copy still
    line up with the original if a rule ever needs to report a position.
    """
    return _STRING_OR_COMMENT.sub(_blank, sql)


def mask_strings_and_comments(sql: str) -> str:
    """Like `mask_literals` but keeps backtick identifiers, with the backticks removed.

    A table reference can be spelled `mysql`.`user`; a rule that only saw the masked
    copy would see two blanks and a dot. Length is not preserved here.
    """
    masked = _STRING_OR_COMMENT_KEEP_IDENTIFIERS.sub(_blank, sql)
    return masked.replace("`", "")


def split_statements(masked: str) -> list[str]:
    """Split on semicolons that are real separators, not ones inside a literal."""
    return [part.strip() for part in masked.split(";") if part.strip()]


def check(sql: str) -> list[str]:
    """Return a list of violations. Empty means the statement shape is acceptable."""
    if not sql or not sql.strip():
        return ["query is empty"]

    masked = mask_literals(sql)
    if not masked.strip():
        return ["query contains no executable statement (comments only)"]

    violations: list[str] = []

    if _EXECUTABLE_COMMENT_RE.search(sql):
        violations.append(
            "executable comments (/*! ... */) are not allowed; MySQL runs their contents"
        )
    if _CAP_RAISING_HINT_RE.search(sql):
        violations.append(
            "SET_VAR and MAX_EXECUTION_TIME hints are not allowed; they would override "
            "the caps set on this connection"
        )

    statements = split_statements(masked)
    if len(statements) > 1:
        violations.append(
            f"multi-statement queries are not allowed ({len(statements)} statements found)"
        )

    head_match = _HEAD_RE.match(masked)
    head = head_match.group(1).upper() if head_match else ""
    if head not in _ALLOWED_HEADS:
        violations.append(
            f"query must begin with one of {', '.join(_ALLOWED_HEADS)}; got {head or '?'}"
        )

    for keyword in sorted({m.group(1).upper() for m in _BLOCKED_KEYWORD_RE.finditer(masked)}):
        violations.append(f"forbidden keyword: {keyword}")

    for fn in sorted({m.group(1).upper() for m in _BLOCKED_FUNCTION_RE.finditer(masked)}):
        violations.append(f"forbidden function: {fn}()")

    if _LOCKING_READ_RE.search(masked):
        violations.append("locking reads (FOR SHARE / LOCK IN SHARE MODE) are not allowed")

    with_identifiers = mask_strings_and_comments(sql)
    for tbl in sorted({m.group(1).lower() for m in _BLOCKED_MYSQL_TABLE_RE.finditer(with_identifiers)}):
        violations.append(f"forbidden table: mysql.{tbl} holds credentials or grants")

    if _SHOW_CREATE_USER_RE.search(with_identifiers):
        violations.append("SHOW CREATE USER is not allowed; it reveals authentication data")

    return violations
