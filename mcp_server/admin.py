# mcp_server/admin.py
"""LAN-only management page for MCP connections.

Anyone who can reach the local network is treated as trusted, so this page has
no login of its own. It is never reachable from the ngrok tunnel: requests
arriving on the public port are refused before any handler runs.
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, render_template, request

from . import config, keys, tunnel

log = logging.getLogger(__name__)

mcp_admin_bp = Blueprint("mcp_admin", __name__, url_prefix=config.ADMIN_PATH)

# .env is read once per process at startup, so edits need a service restart.
ENV_SETTINGS = {"NGROK_AUTHTOKEN", "NGROK_DOMAIN", "MCP_BEARER_TOKEN"}


@mcp_admin_bp.before_request
def _lan_only():
    if tunnel.is_public_request():
        return jsonify({"error": "not found"}), 404
    return None


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-4:] if len(value) > 12 else "…"


def _lan_base() -> str:
    return request.host_url.rstrip("/")


def _state() -> dict:
    try:
        key_rows = keys.list_keys()
        store_error = ""
    except keys.KeyStoreUnavailable as exc:
        key_rows, store_error = [], str(exc)

    public_base = tunnel.tunnel_url()
    return {
        "mount_path": config.MOUNT_PATH,
        "lan_base": _lan_base(),
        "public_base": public_base,
        "tunnel": {
            "enabled": bool(config.ngrok_authtoken()),
            "connected": bool(public_base),
            "domain": config.ngrok_domain(),
            "authtoken_masked": _mask(config.ngrok_authtoken()),
            "split_port": config.public_port(),
        },
        "legacy_bearer_set": bool(config.bearer_token()),
        "keys": [
            {
                "key_id": row["key_id"],
                "label": row["label"],
                "secret": row["secret"],
                "enabled": row["enabled"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
                "use_count": row["use_count"],
            }
            for row in key_rows
        ],
        "store_error": store_error,
    }


@mcp_admin_bp.route("", methods=["GET"])
@mcp_admin_bp.route("/", methods=["GET"])
def page():
    return render_template("mcp/admin.html")


@mcp_admin_bp.route("/api/state", methods=["GET"])
def api_state():
    return jsonify(_state())


@mcp_admin_bp.route("/api/keys", methods=["POST"])
def api_create_key():
    payload = request.get_json(silent=True) or {}
    try:
        created = keys.create_key(payload.get("label") or "")
    except keys.KeyStoreUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    log.info("MCP access key created: %s", created["label"])
    return jsonify(_state()), 201


@mcp_admin_bp.route("/api/keys/<int:key_id>", methods=["PATCH"])
def api_update_key(key_id: int):
    payload = request.get_json(silent=True) or {}
    label = payload.get("label")
    enabled = payload.get("enabled")
    try:
        updated = keys.update_key(
            key_id,
            label=label if isinstance(label, str) else None,
            enabled=bool(enabled) if enabled is not None else None,
        )
    except keys.KeyStoreUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    if updated is None:
        return jsonify({"error": "No such key, or nothing to change."}), 404
    return jsonify(_state())


@mcp_admin_bp.route("/api/keys/<int:key_id>", methods=["DELETE"])
def api_delete_key(key_id: int):
    try:
        removed = keys.delete_key(key_id)
    except keys.KeyStoreUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    if not removed:
        return jsonify({"error": "No such key."}), 404
    return jsonify(_state())


@mcp_admin_bp.route("/api/settings", methods=["POST"])
def api_settings():
    payload = request.get_json(silent=True) or {}
    updates = {
        name: str(payload[name]).strip()
        for name in ENV_SETTINGS
        if name in payload and payload[name] is not None
    }
    if not updates:
        return jsonify({"error": "Nothing to update."}), 400

    try:
        config.update_env_file(updates)
    except OSError as exc:
        return jsonify({"error": f"Could not write {config.ENV_PATH}: {exc}"}), 500

    log.warning("MCP settings changed via admin page: %s", ", ".join(sorted(updates)))
    state = _state()
    state["restart_required"] = True
    return jsonify(state)
