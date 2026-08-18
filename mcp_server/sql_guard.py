# mcp_server/sql_guard.py
"""Validation for LLM-supplied SQL.

Only a single read-only SELECT (optionally with CTEs) against known catalog
tables is allowed. The statement is parsed into an AST rather than pattern
matched, then wrapped in an outer LIMIT before execution.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

DIALECT = "postgres"


class SqlRejected(ValueError):
    """The submitted SQL is not an acceptable read-only query."""


# Nodes that write, change session state, or execute anything sqlglot could not
# fully parse (COPY, VACUUM, CALL, DO, ... all land in exp.Command).
FORBIDDEN_NODES: tuple[type, ...] = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in (
            "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter",
            "TruncateTable", "Command", "Transaction", "Commit", "Rollback",
            "Grant", "Revoke", "Set", "SetItem", "Use", "Copy", "Lock",
            "Analyze", "AlterSet", "Refresh", "Attach", "Detach",
        )
    )
    if isinstance(node, type)
)

# Functions that can touch the filesystem, the network, other sessions, or
# sequence state -- all still permitted inside a READ ONLY transaction.
FORBIDDEN_FUNCTIONS = {
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_read_server_files", "lo_import", "lo_export", "lo_get", "lo_put",
    "dblink", "dblink_exec", "dblink_connect", "dblink_send_query",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "pg_rotate_logfile", "pg_promote", "pg_create_restore_point",
    "set_config", "nextval", "setval", "currval", "query_to_xml",
    "pg_logdir_ls", "pg_file_read", "pg_file_write", "pg_execute_server_program",
}

# Base relations the MCP is allowed to read.
ALLOWED_TABLES = {
    "parts", "intakes", "movements", "locations", "categories",
    "v_current_inventory", "v_inventory_available", "v_inventory_on_loan",
    "v_inventory_totals",
}

# Metadata schemas are readable so the model can introspect, minus the
# relations that leak credentials.
METADATA_SCHEMAS = {"information_schema", "pg_catalog"}
FORBIDDEN_METADATA_TABLES = {
    "pg_authid", "pg_shadow", "pg_user_mappings", "pg_statistic",
    "pg_subscription", "pg_largeobject",
}

_TRAILING_SEMICOLONS = re.compile(r"[;\s]+$")


def _cte_names(root: exp.Expression) -> set[str]:
    names = set()
    for cte in root.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            names.add(alias.lower())
    return names


def _check_tables(root: exp.Expression) -> None:
    local = _cte_names(root)
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.db or "").lower()
        if not name or name in local:
            continue
        if schema in METADATA_SCHEMAS or name.startswith(("pg_", "information_schema")):
            if name in FORBIDDEN_METADATA_TABLES:
                raise SqlRejected(f"Reading system relation '{name}' is not allowed.")
            continue
        if schema not in ("", "public"):
            raise SqlRejected(f"Schema '{schema}' is not readable through this server.")
        if name not in ALLOWED_TABLES:
            raise SqlRejected(
                f"Table '{name}' is not in the allow-list. "
                f"Readable relations: {', '.join(sorted(ALLOWED_TABLES))}."
            )


def _check_functions(root: exp.Expression) -> None:
    for node in root.find_all(exp.Anonymous):
        name = str(node.this or "").lower()
        if name in FORBIDDEN_FUNCTIONS:
            raise SqlRejected(f"Function '{name}()' is not allowed.")
    for node in root.find_all(exp.Func):
        name = (node.sql_name() or "").lower()
        if name in FORBIDDEN_FUNCTIONS:
            raise SqlRejected(f"Function '{name}()' is not allowed.")


def validate(sql: str) -> str:
    """Validate `sql` and return it normalised (no trailing semicolon)."""
    if not sql or not sql.strip():
        raise SqlRejected("Empty query.")

    cleaned = _TRAILING_SEMICOLONS.sub("", sql.strip())

    try:
        statements = [s for s in sqlglot.parse(cleaned, read=DIALECT) if s is not None]
    except Exception as exc:  # sqlglot raises several parse error types
        raise SqlRejected(f"Could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise SqlRejected("Exactly one statement may be submitted per call.")

    root = statements[0]
    if not isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        raise SqlRejected(
            f"Only SELECT queries are allowed (got {type(root).__name__.upper()})."
        )

    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(
                f"'{type(node).__name__.upper()}' is not permitted; this endpoint is read-only."
            )

    _check_tables(root)
    _check_functions(root)
    return cleaned


def wrap_with_limit(sql: str, limit: int) -> str:
    """Wrap a validated query so it can never return more than `limit` rows.

    The newline before the closing paren keeps a trailing `--` comment in the
    inner query from swallowing the LIMIT.
    """
    return f"SELECT * FROM (\n{sql}\n) AS _mcp_q\nLIMIT {int(limit)}"


def validate_and_wrap(sql: str, limit: int) -> str:
    return wrap_with_limit(validate(sql), limit)
