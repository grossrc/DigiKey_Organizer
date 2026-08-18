# mcp_server/readonly_db.py
"""Read-only database access for the MCP server.

Every statement runs inside an explicit READ ONLY transaction that is always
rolled back, so PostgreSQL itself is the last line of defence even if the SQL
guard in `sql_guard.py` were ever bypassed.
"""
from __future__ import annotations

import datetime
import decimal
import json
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from psycopg2.extras import RealDictCursor

from db_helper import get_conn
from . import config


class ReadOnlyQueryError(RuntimeError):
    """Raised when a query is rejected or fails during execution."""


@contextmanager
def readonly_cursor() -> Iterator[RealDictCursor]:
    conn = get_conn()
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Must be the first statement of the transaction to take effect.
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {config.statement_timeout_ms()}")
            cur.execute("SET LOCAL idle_in_transaction_session_timeout = 30000")
            cur.execute("SET LOCAL search_path = public, pg_catalog")
            yield cur
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def fetch(sql: str, params: Sequence[Any] | dict | None = None) -> list[dict]:
    """Run a trusted internal query and return plain JSON-serialisable rows."""
    with readonly_cursor() as cur:
        cur.execute(sql, params or ())
        rows = cur.fetchall() if cur.description is not None else []
    return [jsonify_row(dict(r)) for r in rows]


def fetch_one(sql: str, params: Sequence[Any] | dict | None = None) -> dict | None:
    rows = fetch(sql, params)
    return rows[0] if rows else None


def _coerce(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    return value


def jsonify_row(row: dict) -> dict:
    return {k: _coerce(v) for k, v in row.items()}


def dump(payload: Any) -> str:
    """Serialise a payload, truncating rows if it exceeds the response budget."""
    budget = config.max_response_bytes()
    text = json.dumps(payload, indent=2, default=str)
    if len(text.encode("utf-8")) <= budget:
        return text

    rows = payload.get("rows") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        keep = len(rows)
        while keep > 1:
            keep //= 2
            trimmed = dict(payload)
            trimmed["rows"] = rows[:keep]
            trimmed["truncated"] = (
                f"Response exceeded {budget} bytes; showing {keep} of {len(rows)} rows. "
                "Select fewer columns or add a tighter WHERE clause."
            )
            text = json.dumps(trimmed, indent=2, default=str)
            if len(text.encode("utf-8")) <= budget:
                return text

    return json.dumps(
        {
            "error": f"Response exceeded the {budget} byte limit even after truncation.",
            "hint": "Select fewer columns (especially raw_vendor_json) or aggregate instead.",
        },
        indent=2,
    )
