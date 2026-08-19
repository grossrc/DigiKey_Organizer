# tests/test_mcp_admin.py
import pytest

from mcp_server import admin, config, keys


@pytest.fixture()
def store(monkeypatch):
    """In-memory stand-in for the mcp_access_keys table."""
    rows = []
    counter = {"n": 0}

    def create_key(label):
        counter["n"] += 1
        row = {
            "key_id": counter["n"],
            "label": (label or "").strip() or "unnamed client",
            "secret": f"secret-{counter['n']}",
            "enabled": True,
            "created_at": None,
            "last_used_at": None,
            "use_count": 0,
        }
        rows.append(row)
        return row

    def update_key(key_id, *, label=None, enabled=None):
        for row in rows:
            if row["key_id"] == key_id:
                if label is not None:
                    row["label"] = label
                if enabled is not None:
                    row["enabled"] = enabled
                return row
        return None

    def delete_key(key_id):
        before = len(rows)
        rows[:] = [r for r in rows if r["key_id"] != key_id]
        return len(rows) < before

    monkeypatch.setattr(keys, "list_keys", lambda: list(rows))
    monkeypatch.setattr(keys, "create_key", create_key)
    monkeypatch.setattr(keys, "update_key", update_key)
    monkeypatch.setattr(keys, "delete_key", delete_key)
    return rows


@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "")
    monkeypatch.setenv("NGROK_AUTHTOKEN", "")
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_key_lifecycle(client):
    created = client.post("/mcp-admin/api/keys", json={"label": "ChatGPT"})
    assert created.status_code == 201
    key = created.get_json()["keys"][0]
    assert key["label"] == "ChatGPT" and key["enabled"] and key["secret"]

    disabled = client.patch(f"/mcp-admin/api/keys/{key['key_id']}", json={"enabled": False})
    assert disabled.get_json()["keys"][0]["enabled"] is False

    renamed = client.patch(f"/mcp-admin/api/keys/{key['key_id']}", json={"label": "Claude"})
    assert renamed.get_json()["keys"][0]["label"] == "Claude"

    assert client.delete(f"/mcp-admin/api/keys/{key['key_id']}").get_json()["keys"] == []
    assert client.delete(f"/mcp-admin/api/keys/{key['key_id']}").status_code == 404


def test_state_reports_endpoints(client):
    state = client.get("/mcp-admin/api/state").get_json()
    assert state["mount_path"] == "/mcp"
    assert state["lan_base"].startswith("http://")
    assert state["tunnel"]["enabled"] is False


def test_page_renders(client):
    assert b"MCP Connections" in client.get("/mcp-admin").data


def test_settings_are_written_to_the_env_file(client, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("NGROK_DOMAIN=old.example\n# comment\nPGHOST=localhost\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_PATH", env)

    resp = client.post("/mcp-admin/api/settings", json={"NGROK_DOMAIN": "new.example"})
    assert resp.get_json()["restart_required"] is True

    text = env.read_text(encoding="utf-8")
    assert "NGROK_DOMAIN=new.example" in text
    assert "# comment" in text and "PGHOST=localhost" in text


def test_settings_ignore_unknown_keys(client):
    assert client.post("/mcp-admin/api/settings", json={"PGPASSWORD": "hunter2"}).status_code == 400


def test_admin_is_unreachable_from_the_tunnel(client, monkeypatch):
    monkeypatch.setattr(admin.tunnel, "is_public_request", lambda: True)
    assert client.get("/mcp-admin/api/state").status_code == 404
    assert client.post("/mcp-admin/api/keys", json={"label": "attacker"}).status_code == 404


def test_public_port_identifies_tunnel_traffic(monkeypatch):
    from mcp_server import tunnel
    from app import app

    monkeypatch.setenv("MCP_PUBLIC_PORT", "5001")
    with app.test_request_context("/", environ_overrides={"SERVER_PORT": "5001"}):
        assert tunnel.is_public_request() is True
    # A forged Host header cannot make a LAN request look public, or vice versa.
    with app.test_request_context(
        "/", headers={"Host": "evil.example"}, environ_overrides={"SERVER_PORT": "5000"}
    ):
        assert tunnel.is_public_request() is False
