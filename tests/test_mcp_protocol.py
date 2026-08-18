# tests/test_mcp_protocol.py
import json

import pytest

TOKEN = "test-token-do-not-use-in-production"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", TOKEN)
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def rpc(client, method, params=None, token=TOKEN, req_id=1):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=headers)


def test_requires_bearer_token(client):
    assert rpc(client, "ping", token=None).status_code == 401
    assert rpc(client, "ping", token="wrong").status_code == 401


def test_disabled_without_configured_token(client, monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "")
    assert rpc(client, "ping").status_code == 503


def test_initialize_advertises_capabilities(client):
    result = rpc(client, "initialize").get_json()["result"]
    assert result["serverInfo"]["name"] == "digikey-organizer"
    assert "tools" in result["capabilities"]
    assert "movements" in result["instructions"]


def test_tools_list_shape(client):
    tools = rpc(client, "tools/list").get_json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"search_parts", "get_part", "execute_sql", "explain_sql"} <= names
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]


def test_resources_list_and_static_read(client):
    uris = {r["uri"] for r in rpc(client, "resources/list").get_json()["result"]["resources"]}
    assert "schema://overview" in uris

    contents = rpc(client, "resources/read", {"uri": "schema://overview"}).get_json()
    text = contents["result"]["contents"][0]["text"]
    assert "movements" in text and "raw_vendor_json" in text


def test_unknown_method_and_resource(client):
    assert rpc(client, "nope/nope").get_json()["error"]["code"] == -32601
    assert rpc(client, "resources/read", {"uri": "schema://nope"}).get_json()["error"]["code"] == -32602


def test_notifications_get_no_body(client):
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 202


def test_get_is_not_supported(client):
    resp = client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 405


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE parts SET mpn = 'x'",
        "DROP TABLE parts",
        "SELECT 1; DELETE FROM parts",
        "SELECT pg_read_file('/etc/passwd')",
    ],
)
def test_execute_sql_rejects_writes_without_touching_the_database(client, sql):
    resp = rpc(client, "tools/call", {"name": "execute_sql", "arguments": {"sql": sql}})
    payload = resp.get_json()["result"]
    body = json.loads(payload["content"][0]["text"])
    assert "error" in body


def test_unknown_tool(client):
    resp = rpc(client, "tools/call", {"name": "definitely_not_a_tool", "arguments": {}})
    assert resp.get_json()["error"]["code"] == -32602


TUNNEL_HOST = "example-tunnel.ngrok-free.app"


@pytest.fixture()
def tunnelled(client, monkeypatch):
    monkeypatch.setenv("MCP_TUNNEL_HOST", TUNNEL_HOST)
    return client


@pytest.mark.parametrize("path", ["/", "/catalog", "/DBreset", "/api/available_parts"])
def test_tunnel_only_serves_mcp(tunnelled, path):
    assert tunnelled.get(path, headers={"Host": TUNNEL_HOST}).status_code == 404


def test_tunnel_blocks_the_destructive_reset_route(tunnelled):
    assert tunnelled.post("/DBreset/confirm", headers={"Host": TUNNEL_HOST}).status_code == 404


def test_tunnel_still_serves_mcp(tunnelled):
    resp = tunnelled.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Host": TUNNEL_HOST, "Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200


def test_lan_access_is_unaffected_while_tunnelled(tunnelled):
    assert tunnelled.get("/", headers={"Host": "lab-parts.local"}).status_code == 200
