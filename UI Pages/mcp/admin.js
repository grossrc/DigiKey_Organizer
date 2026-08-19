// UI Pages/mcp/admin.js
const API = "/mcp-admin/api";

let state = null;

const $ = (id) => document.getElementById(id);

async function call(path, options) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
  return body;
}

function banner(text, kind) {
  const el = $("banner");
  el.textContent = text;
  el.className = "banner" + (kind === "warn" ? " warn" : "");
  el.classList.toggle("hidden", !text);
}

function endpointUrl(base, secret) {
  if (!base) return "";
  return `${base}${state.mount_path}${secret ? "/" + secret : ""}`;
}

function activeKeys() {
  return state.keys.filter((k) => k.enabled);
}

function selectedKey() {
  const picked = $("key-picker").value;
  return state.keys.find((k) => String(k.key_id) === picked) || activeKeys()[0] || null;
}

function renderEndpoint() {
  $("lan-url").textContent = endpointUrl(state.lan_base, "<key>");
  $("public-url").textContent = state.public_base
    ? endpointUrl(state.public_base, "<key>")
    : "not connected";

  const t = state.tunnel;
  let note;
  if (!t.enabled) note = "Disabled — no ngrok auth token set below.";
  else if (!t.connected) note = "Auth token is set but no tunnel is open. Restart the service.";
  else if (t.domain) note = `Reserved domain — this URL is stable across restarts.`;
  else note = "Random domain — this URL changes every restart. Reserve a domain to pin it.";
  $("tunnel-state").textContent = note;
}

function renderKeys() {
  const tbody = document.querySelector("#keys tbody");
  tbody.innerHTML = "";

  $("store-error").textContent = state.store_error || "";
  $("store-error").classList.toggle("hidden", !state.store_error);
  $("no-keys").classList.toggle("hidden", state.keys.length > 0 || !!state.store_error);

  const base = state.public_base || state.lan_base;

  for (const key of state.keys) {
    const tr = document.createElement("tr");
    if (!key.enabled) tr.className = "disabled";

    const label = document.createElement("td");
    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.value = key.label;
    labelInput.maxLength = 80;
    labelInput.onchange = () => patch(key.key_id, { label: labelInput.value });
    label.appendChild(labelInput);

    const url = document.createElement("td");
    url.className = "url";
    const code = document.createElement("code");
    code.textContent = endpointUrl(base, key.secret);
    const copy = document.createElement("button");
    copy.textContent = "Copy";
    copy.onclick = () => copyText(code.textContent, copy);
    const row = document.createElement("div");
    row.className = "copyrow";
    row.append(code, copy);
    url.appendChild(row);

    const used = document.createElement("td");
    used.textContent = key.last_used_at
      ? `${new Date(key.last_used_at).toLocaleString()} (${key.use_count})`
      : "never";

    const active = document.createElement("td");
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = key.enabled;
    toggle.onchange = () => patch(key.key_id, { enabled: toggle.checked });
    const toggleLabel = document.createElement("label");
    toggleLabel.className = "toggle";
    toggleLabel.append(toggle, document.createTextNode(key.enabled ? "on" : "off"));
    active.appendChild(toggleLabel);

    const actions = document.createElement("td");
    const del = document.createElement("button");
    del.textContent = "Delete";
    del.className = "danger";
    del.onclick = () => remove(key.key_id, key.label);
    actions.appendChild(del);

    tr.append(label, url, used, active, actions);
    tbody.appendChild(tr);
  }
}

function renderPicker() {
  const picker = $("key-picker");
  const previous = picker.value;
  picker.innerHTML = "";

  for (const key of activeKeys()) {
    const option = document.createElement("option");
    option.value = String(key.key_id);
    option.textContent = key.label;
    picker.appendChild(option);
  }
  if (previous && [...picker.options].some((o) => o.value === previous)) picker.value = previous;
  renderSnippets();
}

function renderSnippets() {
  const key = selectedKey();
  const base = state.public_base || state.lan_base;
  const url = key ? endpointUrl(base, key.secret) : endpointUrl(base, "<generate a key first>");

  $("snippet-chatgpt").textContent = url;
  $("snippet-vscode").textContent = JSON.stringify(
    { servers: { "lab-parts": { type: "http", url } } },
    null,
    2
  );
  $("snippet-curl").textContent =
    `curl -sS -X POST ${url} \\\n` +
    `  -H "Content-Type: application/json" \\\n` +
    `  -H "ngrok-skip-browser-warning: 1" \\\n` +
    `  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'`;
}

function render() {
  renderEndpoint();
  renderKeys();
  renderPicker();
  if (document.activeElement !== $("ngrok-domain")) $("ngrok-domain").value = state.tunnel.domain;
  $("authtoken-masked").textContent = state.tunnel.authtoken_masked || "not set";
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const original = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => (button.textContent = original), 1200);
  } catch {
    banner("Clipboard blocked by the browser. Select the text manually.", "warn");
  }
}

async function refresh() {
  try {
    state = await call("/state");
    render();
  } catch (err) {
    banner(err.message, "warn");
  }
}

async function patch(keyId, body) {
  try {
    state = await call(`/keys/${keyId}`, { method: "PATCH", body: JSON.stringify(body) });
    render();
    banner("");
  } catch (err) {
    banner(err.message, "warn");
  }
}

async function remove(keyId, label) {
  if (!confirm(`Delete "${label}"? Any client using it stops working immediately.`)) return;
  try {
    state = await call(`/keys/${keyId}`, { method: "DELETE" });
    render();
    banner("");
  } catch (err) {
    banner(err.message, "warn");
  }
}

async function saveSettings(payload) {
  try {
    state = await call("/settings", { method: "POST", body: JSON.stringify(payload) });
    $("ngrok-authtoken").value = "";
    render();
    banner("Saved to .env. Run: sudo systemctl restart catalog", "warn");
  } catch (err) {
    banner(err.message, "warn");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("new-key").onsubmit = async (event) => {
    event.preventDefault();
    const label = $("new-key-label").value;
    try {
      state = await call("/keys", { method: "POST", body: JSON.stringify({ label }) });
      $("new-key-label").value = "";
      render();
      banner("Key created. Copy its URL into your LLM client.");
    } catch (err) {
      banner(err.message, "warn");
    }
  };

  $("settings").onsubmit = async (event) => {
    event.preventDefault();
    // The token is write-only, so only send it when the user typed a new one.
    const payload = { NGROK_DOMAIN: $("ngrok-domain").value.trim() };
    const token = $("ngrok-authtoken").value.trim();
    if (token) payload.NGROK_AUTHTOKEN = token;
    await saveSettings(payload);
  };

  $("disable-tunnel").onclick = async () => {
    if (!confirm("Clear the ngrok token? The database will only be reachable from the LAN.")) return;
    await saveSettings({ NGROK_AUTHTOKEN: "" });
  };

  $("key-picker").onchange = renderSnippets;
  const copyButtons = document.querySelectorAll("button.copy[data-copy]");
  copyButtons.forEach((button) => {
    button.onclick = () => copyText($(button.dataset.copy).textContent, button);
  });

  refresh();
});
