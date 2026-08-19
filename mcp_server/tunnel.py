# mcp_server/tunnel.py
"""Optional ngrok tunnel for the MCP endpoint.

The tunnel is entirely opt-in: it exists only when NGROK_AUTHTOKEN is non-empty
in .env. Leaving that line blank disables it and the rest of the application is
unaffected.
"""
from __future__ import annotations

import atexit
import ipaddress
import logging
import os
from pathlib import Path

from flask import request

from . import config

log = logging.getLogger(__name__)

# Set once by the process that opens the tunnel; gunicorn workers inherit it
# through the environment when forked from the master.
TUNNEL_HOST_ENV = "MCP_TUNNEL_HOST"

# journald renders pyngrok's download progress bar as "[N blob data]", which can
# bury the startup log line, so the URL is also written here.
URL_FILE = Path(__file__).resolve().parent.parent / ".mcp_tunnel_url"


def tunnel_host() -> str:
    return (os.environ.get(TUNNEL_HOST_ENV) or "").strip().lower()


def tunnel_url() -> str:
    host = tunnel_host()
    return f"https://{host}" if host else ""


def maybe_start_tunnel(port: int | None = None) -> str | None:
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
    options = {"addr": port or config.public_port() or 5000, "proto": "http"}
    domain = config.ngrok_domain()
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

    message = f"MCP tunnel open: {public_url}{config.MOUNT_PATH} (nothing else is served publicly)"
    log.warning(message)
    print(message, flush=True)
    try:
        URL_FILE.write_text(public_url + "\n", encoding="utf-8")
    except OSError:
        log.warning("Could not write %s; read the URL from the log instead.", URL_FILE)
    return public_url


def _shutdown() -> None:
    try:
        from pyngrok import ngrok

        ngrok.kill()
    except Exception:
        pass
    URL_FILE.unlink(missing_ok=True)


def _is_lan_host(value: str) -> bool:
    """True for hostnames the kiosk/LAN would use, false for anything routable."""
    name = value.split(",")[0].strip().lower().rsplit(":", 1)[0].strip("[]")
    if not name:
        return False
    if name in ("localhost", "::1") or name.endswith(".local") or name.endswith(".lan"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def is_public_request() -> bool:
    """Did this request arrive from the internet rather than the LAN?

    In production gunicorn binds a second loopback port that only the ngrok
    tunnel talks to, so the answer comes from the listening socket and cannot be
    forged with a fake Host header. Single-port dev runs fall back to hostname
    matching, which is weaker but never faces the internet.
    """
    port = config.public_port()
    if port:
        try:
            return int(request.environ.get("SERVER_PORT") or 0) == port
        except (TypeError, ValueError):
            return False

    if not tunnel_host():
        return False
    return not _is_lan_host(request.headers.get("X-Forwarded-Host") or request.host or "")


def install_tunnel_guard(app) -> None:
    """Expose only the MCP endpoint to the outside world.

    The catalog UI, the destructive /DBreset routes and the MCP admin page all
    stay on the local network where they were designed to live.
    """

    @app.before_request
    def _restrict_public_traffic():
        if not is_public_request():
            return None
        path = request.path
        if path == config.MOUNT_PATH or path.startswith(config.MOUNT_PATH + "/"):
            return None
        return {"error": "not found"}, 404
