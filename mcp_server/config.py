# mcp_server/config.py
"""Environment-driven settings for the read-only MCP server."""
from __future__ import annotations

import os
import re
from pathlib import Path

SERVER_NAME = "digikey-organizer"
SERVER_VERSION = "1.0.0"

# MCP spec revision this server implements (Streamable HTTP, JSON responses only).
PROTOCOL_VERSION = "2025-06-18"

# Path the MCP endpoint is mounted at inside the Flask app.
MOUNT_PATH = "/mcp"

# Where the LAN-only connection manager lives.
ADMIN_PATH = "/mcp-admin"

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def bearer_token() -> str:
    return (os.getenv("MCP_BEARER_TOKEN") or "").strip()


def ngrok_authtoken() -> str:
    """Non-empty NGROK_AUTHTOKEN is the opt-in switch for the public tunnel."""
    return (os.getenv("NGROK_AUTHTOKEN") or "").strip()


def ngrok_domain() -> str:
    return (os.getenv("NGROK_DOMAIN") or "").strip()


def public_port() -> int:
    """Loopback port dedicated to tunnel traffic, or 0 when not running split-port.

    gunicorn binds this in addition to the LAN port so the origin of a request can
    be told from the listening socket rather than a spoofable Host header.
    """
    try:
        return int((os.getenv("MCP_PUBLIC_PORT") or "0").strip())
    except ValueError:
        return 0


def read_env_file() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def update_env_file(updates: dict[str, str]) -> None:
    """Rewrite .env in place, preserving comments and ordering."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)

    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=)", line)
        if match and match.group(2) in remaining:
            key = match.group(2)
            lines[index] = f"{match.group(1)}{key}={remaining.pop(key)}"

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    os.environ.update(updates)


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
