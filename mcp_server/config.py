# mcp_server/config.py
"""Environment-driven settings for the read-only MCP server."""
from __future__ import annotations

import os

SERVER_NAME = "digikey-organizer"
SERVER_VERSION = "1.0.0"

# MCP spec revision this server implements (Streamable HTTP, JSON responses only).
PROTOCOL_VERSION = "2025-06-18"

# Path the MCP endpoint is mounted at inside the Flask app.
MOUNT_PATH = "/mcp"


def bearer_token() -> str:
    return (os.getenv("MCP_BEARER_TOKEN") or "").strip()


def ngrok_authtoken() -> str:
    """Non-empty NGROK_AUTHTOKEN is the opt-in switch for the public tunnel."""
    return (os.getenv("NGROK_AUTHTOKEN") or "").strip()


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def default_rows() -> int:
    return _int_env("MCP_DEFAULT_ROWS", 100, 1, 1000)


def max_rows() -> int:
    return _int_env("MCP_MAX_ROWS", 500, 1, 5000)


def statement_timeout_ms() -> int:
    return _int_env("MCP_STATEMENT_TIMEOUT_MS", 10_000, 500, 120_000)


def max_response_bytes() -> int:
    return _int_env("MCP_MAX_RESPONSE_BYTES", 262_144, 4_096, 4_194_304)


def cache_ttl_seconds() -> int:
    return _int_env("MCP_CACHE_TTL_SECONDS", 300, 0, 86_400)
