# mcp_server/tunnel.py
"""Optional ngrok tunnel for the MCP endpoint.

The tunnel is entirely opt-in: it exists only when NGROK_AUTHTOKEN is non-empty
in .env. Leaving that line blank disables it and the rest of the application is
unaffected.
"""
from __future__ import annotations

import atexit
import logging
import os

from flask import request

from . import config

log = logging.getLogger(__name__)

# Set once by the process that opens the tunnel; gunicorn workers inherit it
# through the environment when forked from the master.
TUNNEL_HOST_ENV = "MCP_TUNNEL_HOST"


def tunnel_host() -> str:
    return (os.environ.get(TUNNEL_HOST_ENV) or "").strip().lower()


def maybe_start_tunnel(port: int = 5000) -> str | None:
    """Open an ngrok tunnel if the user supplied an auth token. Returns the public URL."""
    token = config.ngrok_authtoken()
    if not token:
        log.info("NGROK_AUTHTOKEN is empty; MCP tunnel disabled (LAN access still works).")
        return None
    if tunnel_host():
        return None

    try:
        from pyngrok import conf, ngrok
    except ImportError:
        log.error("NGROK_AUTHTOKEN is set but pyngrok is not installed; run 'pip install pyngrok'.")
        return None

    conf.get_default().auth_token = token
    options = {"addr": port, "proto": "http"}
    domain = (os.getenv("NGROK_DOMAIN") or "").strip()
    if domain:
        options["domain"] = domain

    try:
        tunnel = ngrok.connect(**options)
    except Exception:
        log.exception("Failed to open ngrok tunnel; continuing without it.")
        return None

    public_url = tunnel.public_url
    os.environ[TUNNEL_HOST_ENV] = public_url.split("://", 1)[-1].lower()
    atexit.register(_shutdown)

    log.warning(
        "MCP reachable at %s%s (only %s* is served over the tunnel)",
        public_url, config.MOUNT_PATH, config.MOUNT_PATH,
    )
    return public_url


def _shutdown() -> None:
    try:
        from pyngrok import ngrok

        ngrok.kill()
    except Exception:
        pass


def install_tunnel_guard(app) -> None:
    """Serve only the MCP endpoint over the public tunnel.

    The rest of the app (including the destructive /DBreset routes) stays on the
    LAN, where it was designed to live.
    """

    @app.before_request
    def _restrict_tunnel_to_mcp():
        host = tunnel_host()
        if not host:
            return None
        forwarded = (request.headers.get("X-Forwarded-Host") or request.host or "").lower()
        if forwarded.split(",")[0].strip() != host:
            return None
        if request.path == config.MOUNT_PATH or request.path.startswith(config.MOUNT_PATH + "/"):
            return None
        return {"error": "not found"}, 404
