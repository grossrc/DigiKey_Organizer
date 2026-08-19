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
  const note = $("endpoint-note");
  const t = state.tunnel;
  if (t.connected) {
    note.textContent = `Keys connect at ${endpointUrl(state.public_base, "<key>")}`;
  } else {
    note.textContent = `No tunnel — keys work on this network only, at ${endpointUrl(state.lan_base, "<key>")}`;
  }

  let status;
  if (!t.enabled) status = "Disabled — no ngrok auth token set.";
  else if (!t.connected) status = "Auth token is set but no tunnel is open. Restart the service.";
  else if (t.domain) status = `Connected on ${t.domain} — this URL is stable across restarts.`;
  else status = "Connected on a random domain — the URL changes every restart. Reserve a domain to pin it.";
  $("tunnel-state").textContent = status;
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

// navigator.clipboard is unavailable on plain-HTTP LAN pages, so fall back to execCommand.
function legacyCopy(text) {
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.setAttribute("readonly", "");
  scratch.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(scratch);
  scratch.select();
  scratch.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  scratch.remove();
  return ok;
}

function selectAll(node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

async function copyText(text, button) {
  let ok = false;
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      ok = false;
    }
  }
  if (!ok) ok = legacyCopy(text);

  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  button.textContent = ok ? "Copied" : "Ctrl+C";
  if (!ok) selectAll(button.closest(".copyrow").querySelector("code, pre"));
  setTimeout(() => (button.textContent = original), 1500);
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

  document.addEventListener("click", (event) => {
    const block = event.target.closest(".copyrow code, .copyrow pre");
    if (block) selectAll(block);
  });

  refresh();
});
