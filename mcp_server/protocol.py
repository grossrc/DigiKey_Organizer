# mcp_server/protocol.py
"""MCP Streamable HTTP endpoint implemented as a Flask blueprint.

Stateless and JSON-only: every POST carries one JSON-RPC message and gets one
JSON response. No SSE, so it works unchanged behind gunicorn's sync workers and
the existing nginx proxy.
"""
from __future__ import annotations

import hmac
import json
import logging

from flask import Blueprint, Response, current_app, jsonify, request

from . import config, keys, resources, tools

log = logging.getLogger(__name__)

mcp_bp = Blueprint("mcp", __name__, url_prefix=config.MOUNT_PATH)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = """Read-only access to an electronics parts inventory database.

Before answering anything non-trivial, read the `schema://overview` resource. It
explains the two things that are easy to get wrong:

1. Current stock is a SUM over the `movements` ledger, not a column. Prefer the
   `v_inventory_totals` view or the `inventory_summary` tool. Bins whose
   position_code starts with OUT mean "checked out", not "in storage".
2. All electrical specifications live in the `parts.attributes` JSONB column,
   keyed by canonical names whose suffix gives the unit, stored in base SI units
   (10 kOhm is 10000, 100 nF is 1e-07). Consult `schema://profiles` and
   `schema://jsonb` for the keys valid in a given category.

Typical flow: list_categories -> attribute_keys -> search_parts. Fall back to
execute_sql for anything the curated tools cannot express; see
`schema://query_cookbook`. Never select parts.raw_vendor_json in bulk -- request
it via get_part(include_raw_vendor_json=true) only when a specification is
genuinely missing from both attributes and unknown_parameters."""


def _error(req_id, code: int, message: str, http: int = 200):
    payload = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    return jsonify(payload), http


def _result(req_id, result: dict):
    return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})


@mcp_bp.before_request
def _authenticate():
    legacy = config.bearer_token()
    presented = _presented_secrets()

    if legacy and any(hmac.compare_digest(candidate, legacy) for candidate in presented):
        return None
    if any(keys.verify(candidate) for candidate in presented):
        return None

    if not legacy and not keys.has_enabled_keys():
        return (
            jsonify({
                "error": "MCP server is not configured.",
                "detail": f"Create an access key at {config.ADMIN_PATH} on the local network.",
            }),
            503,
        )

    log.warning("MCP auth failure from %s for %s", request.remote_addr, request.path)
    return (
        jsonify({"error": "unauthorized"}),
        401,
        {"WWW-Authenticate": 'Bearer realm="mcp"'},
    )


def _is_error_payload(text: str) -> bool:
    try:
        payload = json.loads(text)
    except ValueError:
        return False
    return isinstance(payload, dict) and "error" in payload


def _presented_secrets() -> list[str]:
    """A key may arrive as the last URL segment or as a bearer token."""
    candidates = []
    from_path = (request.view_args or {}).get("key")
    if from_path:
        candidates.append(str(from_path).strip())

    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        candidates.append(value.strip())
    return candidates


@mcp_bp.route("", methods=["GET", "DELETE"])
@mcp_bp.route("/", methods=["GET", "DELETE"])
@mcp_bp.route("/<key>", methods=["GET", "DELETE"])
def _unsupported(key=None):
    return (
        jsonify({"error": "This MCP endpoint is JSON-only; POST a JSON-RPC message."}),
        405,
        {"Allow": "POST"},
    )


@mcp_bp.route("", methods=["POST"])
@mcp_bp.route("/", methods=["POST"])
@mcp_bp.route("/<key>", methods=["POST"])
def rpc(key=None):
    message = request.get_json(silent=True)
    if message is None:
        return _error(None, PARSE_ERROR, "Request body is not valid JSON.", http=400)
    if isinstance(message, list):
        return _error(None, INVALID_REQUEST, "Batched requests are not supported.", http=400)
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Request must be a JSON-RPC object.", http=400)

    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    # Notifications carry no id and expect no body.
    if req_id is None:
        return Response(status=202)

    try:
        return _dispatch(method, params, req_id)
    except Exception as exc:
        current_app.logger.exception("MCP method %s failed", method)
        return _error(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


def _dispatch(method: str, params: dict, req_id):
    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": config.PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": config.SERVER_NAME, "version": config.SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": tools.list_tools()})

    if method == "tools/call":
        name = params.get("name")
        try:
            text = tools.call_tool(name, params.get("arguments") or {})
        except KeyError:
            return _error(req_id, INVALID_PARAMS, f"Unknown tool: {name!r}")
        except Exception as exc:
            current_app.logger.exception("MCP tool %s failed", name)
            return _result(req_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
        return _result(req_id, {
            "content": [{"type": "text", "text": text}],
            # Tools report rejected input in-band, so surface that as a tool error.
            "isError": _is_error_payload(text),
        })

    if method == "resources/list":
        return _result(req_id, {"resources": resources.list_resources()})

    if method == "resources/templates/list":
        return _result(req_id, {"resourceTemplates": []})

    if method == "resources/read":
        uri = params.get("uri")
        try:
            return _result(req_id, {"contents": [resources.read_resource(uri)]})
        except KeyError:
            return _error(req_id, INVALID_PARAMS, f"Unknown resource: {uri!r}")

    if method in ("prompts/list",):
        return _result(req_id, {"prompts": []})

    return _error(req_id, METHOD_NOT_FOUND, f"Unsupported method: {method!r}")
