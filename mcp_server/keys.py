# mcp_server/keys.py
"""Storage for MCP access keys.

A key is just a high-entropy string. It authenticates either as the last segment
of the endpoint URL (for clients like ChatGPT that cannot send custom headers) or
as a bearer token. Keys live in the database so they can be issued and revoked
from the admin page without restarting the service.
"""
from __future__ import annotations

import secrets
from contextlib import closing
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from db_helper import get_conn

SECRET_BYTES = 32
MAX_LABEL = 80


class KeyStoreUnavailable(RuntimeError):
    """The mcp_access_keys table does not exist yet."""


def _rows(sql: str, params: Any = None, fetch: bool = True) -> list[dict]:
    try:
        with closing(get_conn()) as conn:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, params or ())
                    return [dict(r) for r in cur.fetchall()] if fetch else []
    except psycopg2.errors.UndefinedTable as exc:
        raise KeyStoreUnavailable(
            "Run deploy/migrations/20260819_add_mcp_access_keys.sql to enable MCP access keys."
        ) from exc


_PUBLIC_COLUMNS = "key_id, label, secret, enabled, created_at, last_used_at, use_count"


def list_keys() -> list[dict]:
    return _rows(f"SELECT {_PUBLIC_COLUMNS} FROM mcp_access_keys ORDER BY created_at DESC")


def create_key(label: str) -> dict:
    clean = (label or "").strip()[:MAX_LABEL] or "unnamed client"
    rows = _rows(
        f"INSERT INTO mcp_access_keys (label, secret) VALUES (%s, %s) RETURNING {_PUBLIC_COLUMNS}",
        (clean, secrets.token_urlsafe(SECRET_BYTES)),
    )
    return rows[0]


def update_key(key_id: int, *, label: str | None = None, enabled: bool | None = None) -> dict | None:
    sets, params = [], []
    if label is not None:
        sets.append("label = %s")
        params.append(label.strip()[:MAX_LABEL] or "unnamed client")
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(bool(enabled))
    if not sets:
        return None
    params.append(key_id)
    rows = _rows(
        f"UPDATE mcp_access_keys SET {', '.join(sets)} WHERE key_id = %s RETURNING {_PUBLIC_COLUMNS}",
        tuple(params),
    )
    return rows[0] if rows else None


def delete_key(key_id: int) -> bool:
    return bool(_rows("DELETE FROM mcp_access_keys WHERE key_id = %s RETURNING key_id", (key_id,)))


def verify(secret: str) -> dict | None:
    """Match an enabled key and record the usage. Returns the key row, or None."""
    if not secret:
        return None
    try:
        rows = _rows(
            """
            UPDATE mcp_access_keys
               SET last_used_at = now(), use_count = use_count + 1
             WHERE secret = %s AND enabled
            RETURNING key_id, label
            """,
            (secret,),
        )
    except KeyStoreUnavailable:
        return None
    return rows[0] if rows else None


def has_enabled_keys() -> bool:
    try:
        return bool(_rows("SELECT 1 FROM mcp_access_keys WHERE enabled LIMIT 1"))
    except KeyStoreUnavailable:
        return False
