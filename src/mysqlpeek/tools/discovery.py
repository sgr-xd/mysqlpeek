"""Schema discovery.

Every query here is written by mysqlpeek and takes only bound parameters — no caller
string ever reaches the SQL text, with one exception: `show_create_table`, where MySQL
cannot bind an identifier, so the name is validated and backtick-quoted instead. That
is why these tools do not go through the policy engine: there is no user SQL to police.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from ..registry import InstanceRegistry
from . import READ_ONLY
from ._common import (
    SYSTEM_SCHEMAS,
    database_or_error,
    human_size,
    quote_identifier,
    resolve,
    rows_as_dicts,
    run,
)


def register(mcp: MCPServer, registry: InstanceRegistry) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def list_instances() -> dict[str, Any]:
        """List the MySQL / MariaDB instances this server can reach.

        Every other tool takes an optional `instance` argument naming one of these.
        Pass it explicitly whenever more than one is configured — the default is only a
        convenience, and reading the wrong environment is a real mistake.
        """
        return {
            "instances": registry.describe_all(),
            "default": registry.default_name,
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_databases(instance: str | None = None) -> dict[str, Any]:
        """List databases (schemas) visible to the connected user.

        Start here when you do not know what is on the instance. `system: true` marks
        information_schema, performance_schema, mysql and sys.

        Args:
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT SCHEMA_NAME AS name,
                   DEFAULT_CHARACTER_SET_NAME AS charset,
                   DEFAULT_COLLATION_NAME AS collation
            FROM information_schema.SCHEMATA
            ORDER BY SCHEMA_NAME
            """,
        )
        if qerr:
            return qerr
        databases = rows_as_dicts(result)
        for db in databases:
            db["system"] = str(db["name"]).lower() in SYSTEM_SCHEMAS
        return {
            "instance": handle.name,
            "databases": databases,
            "count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_tables(
        database: str | None = None,
        name_like: str | None = None,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """List tables with the facts that determine query cost.

        `primary_key` and `partitioning` are the two things worth reading closely: a
        filter that matches the leading column of the primary key (or of an index —
        see `list_indexes`), or that pins a partition, is the difference between an
        index lookup and a table scan. `approx_rows` is InnoDB's estimate, and on
        MySQL 8 it can be up to a day stale (information_schema_stats_expiry).

        Args:
            database: Restrict to one database. Defaults to the instance's database.
            name_like: Optional SQL LIKE pattern on the table name, e.g. 'events%'.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        db, err = database_or_error(handle, database)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT
                t.TABLE_NAME AS name,
                t.TABLE_TYPE AS type,
                t.ENGINE AS engine,
                t.TABLE_ROWS AS approx_rows,
                t.DATA_LENGTH AS data_bytes,
                t.INDEX_LENGTH AS index_bytes,
                (IFNULL(t.DATA_LENGTH, 0) + IFNULL(t.INDEX_LENGTH, 0)) AS total_bytes,
                t.AUTO_INCREMENT AS auto_increment,
                t.ROW_FORMAT AS row_format,
                t.TABLE_COLLATION AS collation,
                t.CREATE_TIME AS created,
                t.UPDATE_TIME AS updated,
                t.TABLE_COMMENT AS comment,
                (SELECT GROUP_CONCAT(s.COLUMN_NAME ORDER BY s.SEQ_IN_INDEX SEPARATOR ', ')
                   FROM information_schema.STATISTICS s
                  WHERE s.TABLE_SCHEMA = t.TABLE_SCHEMA
                    AND s.TABLE_NAME = t.TABLE_NAME
                    AND s.INDEX_NAME = 'PRIMARY') AS primary_key,
                (SELECT CONCAT(p.PARTITION_METHOD, ' (', p.PARTITION_EXPRESSION, ')')
                   FROM information_schema.PARTITIONS p
                  WHERE p.TABLE_SCHEMA = t.TABLE_SCHEMA
                    AND p.TABLE_NAME = t.TABLE_NAME
                    AND p.PARTITION_NAME IS NOT NULL
                  LIMIT 1) AS partitioning,
                (SELECT COUNT(*)
                   FROM information_schema.PARTITIONS p
                  WHERE p.TABLE_SCHEMA = t.TABLE_SCHEMA
                    AND p.TABLE_NAME = t.TABLE_NAME
                    AND p.PARTITION_NAME IS NOT NULL) AS partition_count
            FROM information_schema.TABLES t
            WHERE t.TABLE_SCHEMA = %(db)s
              AND (%(pattern)s IS NULL OR t.TABLE_NAME LIKE %(pattern)s)
            ORDER BY total_bytes DESC, t.TABLE_NAME
            """,
            parameters={"db": db, "pattern": name_like},
        )
        if qerr:
            return qerr
        tables = rows_as_dicts(result)
        for table in tables:
            table["size"] = human_size(table.get("total_bytes"))
        return {
            "instance": handle.name,
            "database": db,
            "tables": tables,
            "count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def describe_table(
        table: str, database: str | None = None, instance: str | None = None
    ) -> dict[str, Any]:
        """Describe a table's columns.

        `key` says whether a column is indexed: PRI (primary key), UNI (unique index),
        MUL (leading column of a non-unique index). A filter on a column with no key
        entry cannot use an index — check `list_indexes` for the full picture, since a
        column that is second in a composite index shows nothing here.

        Args:
            table: Table name.
            database: Defaults to the instance's database.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        db, err = database_or_error(handle, database)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT
                COLUMN_NAME AS name,
                COLUMN_TYPE AS type,
                IS_NULLABLE AS nullable,
                COLUMN_KEY AS `key`,
                COLUMN_DEFAULT AS `default`,
                EXTRA AS extra,
                CHARACTER_SET_NAME AS charset,
                COLLATION_NAME AS collation,
                GENERATION_EXPRESSION AS generated_as,
                COLUMN_COMMENT AS comment
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %(db)s AND TABLE_NAME = %(tbl)s
            ORDER BY ORDINAL_POSITION
            """,
            parameters={"db": db, "tbl": table},
        )
        if qerr:
            return qerr
        if result.row_count == 0:
            return {
                "instance": handle.name,
                "database": db,
                "table": table,
                "columns": [],
                "error": f"table {db}.{table} not found on {handle.name}, or not visible",
            }
        columns = rows_as_dicts(result)
        for col in columns:
            col["nullable"] = str(col["nullable"]).upper() == "YES"
            if not col.get("generated_as"):
                col.pop("generated_as", None)
        return {
            "instance": handle.name,
            "database": db,
            "table": table,
            "columns": columns,
            "column_count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_indexes(
        table: str, database: str | None = None, instance: str | None = None
    ) -> dict[str, Any]:
        """List a table's indexes with their column order and cardinality.

        Column order is what matters: an index on (tenant_id, created_at) serves a
        filter on tenant_id, or on both, but not on created_at alone. `cardinality` is
        the estimated distinct-value count; a low figure on a large table means the
        index narrows little.

        Args:
            table: Table name.
            database: Defaults to the instance's database.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        db, err = database_or_error(handle, database)
        if err:
            return err
        result, qerr = run(
            handle,
            """
            SELECT
                INDEX_NAME AS name,
                MIN(NON_UNIQUE) = 0 AS is_unique,
                MAX(INDEX_TYPE) AS type,
                GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ', ') AS `columns`,
                MAX(CARDINALITY) AS cardinality,
                MAX(INDEX_COMMENT) AS comment
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %(db)s AND TABLE_NAME = %(tbl)s
            GROUP BY INDEX_NAME
            ORDER BY (INDEX_NAME = 'PRIMARY') DESC, INDEX_NAME
            """,
            parameters={"db": db, "tbl": table},
        )
        if qerr:
            return qerr
        indexes = rows_as_dicts(result)
        for idx in indexes:
            idx["is_unique"] = bool(idx["is_unique"])
        return {
            "instance": handle.name,
            "database": db,
            "table": table,
            "indexes": indexes,
            "count": result.row_count,
        }

    @mcp.tool(annotations=READ_ONLY)
    def show_create_table(
        table: str, database: str | None = None, instance: str | None = None
    ) -> dict[str, Any]:
        """Return the full CREATE TABLE (or CREATE VIEW) statement.

        Read this when you need the exact index definitions, foreign keys, partition
        clause or table options that the column view does not show.

        Args:
            table: Table name.
            database: Defaults to the instance's database.
            instance: Which configured instance to use. Defaults to the default instance.
        """
        handle, err = resolve(registry, instance)
        if err:
            return err
        db, err = database_or_error(handle, database)
        if err:
            return err
        try:
            qualified = f"{quote_identifier(db, 'database')}.{quote_identifier(table, 'table')}"
        except ValueError as exc:
            return {"error": str(exc), "instance": handle.name}
        result, qerr = run(handle, f"SHOW CREATE TABLE {qualified}")
        if qerr:
            return qerr
        if result.row_count == 0 or len(result.rows[0]) < 2:
            return {
                "instance": handle.name,
                "database": db,
                "table": table,
                "error": f"table {db}.{table} not found on {handle.name}",
            }
        return {
            "instance": handle.name,
            "database": db,
            "table": table,
            "kind": "view" if "View" in result.columns[1] else "table",
            "ddl": result.rows[0][1],
        }
