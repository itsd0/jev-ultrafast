"use strict";
/* Jev sessions: chat threads over one browser per session. No build step. */

const S = {
  token: localStorage.getItem("jev-token") || "",
  view: "session",
  sessions: [],
  current: null,
  streamAbort: null,
  streamId: null,
  rightTab: "browser",
  running: false,
  openTurns: new Set(),
  reloadTimer: null,
  timer: null,
};

const OP_ICON = {
  NAVIGATE: "\u2192", CLICK: "\u25cf", TYPE_TEXT: "\u270e", SELECT: "\u25be",
  SELECT_OPTION: "\u25be", UPLOAD_FILE: "\u21ea", SET_CHECKED: "\u2611",
  PRESS_KEY: "\u2328", SCROLL_DOWN: "\u2193", SCROLL_UP: "\u2191",
  SCROLL_LEFT: "\u2190", SCROLL_RIGHT: "\u2192", SWITCH_TAB: "\u21c4",
  NEW_TAB: "+", CLOSE_TAB: "\u2715", SAVE_FILE: "\u25a3", CAPTCHA: "\u26a0", RESTART: "\u21bb",
  DONE: "\u2713", BLOCKED: "\u25a0", WAIT: "\u2026",
};

const $ = (id) => document.getElementById(id);

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
  ));
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}

function fmtCost(value, priced) {
  if (priced === 0) return "price not set";
  if (value == null) return "$0";
  const num = Number(value);
  if (!isFinite(num)) return "$0";
  if (num === 0) return "$0";
  if (num < 0.01) return "$" + num.toFixed(4);
  return "$" + num.toFixed(2);
}

function fmtCloud(seconds, cost, priced) {
  if (seconds == null && cost == null) return "";
  if (!(seconds > 0) && !(cost > 0)) return "";
  const dur = seconds != null ? fmtDur(seconds * 1000) : "?";
  const price = (cost == null || priced === 0) ? "price not set" : fmtCost(cost, 1);
  return `browser ${dur} · ${price}`;
}

function fmtTurnCost(message) {
  const tokens = (message.input_tokens || 0) + (message.output_tokens || 0);
  const parts = [`${tokens} tokens`, fmtCost(message.cost_usd_estimate, message.priced)];
  const cloud = fmtCloud(message.cloud_seconds, message.cloud_cost_usd, 1);
  if (cloud) parts.push(cloud);
  return parts.join(" · ");
}

function fmtSessionCost(session) {
  const tokens = (session.total_input_tokens || 0) + (session.total_output_tokens || 0);
  const parts = [`${tokens} tokens`, fmtCost(session.total_cost_usd, session.priced)];
  const cloud = fmtCloud(session.total_cloud_seconds, session.total_cloud_cost_usd, session.cloud_priced);
  if (cloud) parts.push(cloud);
  return parts.join(" · ");
}

function fmtUsageCost(row) {
  const parts = [fmtCost(row.cost, row.priced)];
  const cloud = fmtCloud(row.cloud_seconds, row.cloud_cost, row.cloud_priced);
  if (cloud) parts.push(cloud);
  return parts.join(" · ");
}

function fmtDur(ms) {
  if (ms == null) return "";
  const s = Math.round(Number(ms) / 1000);
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m " + (s % 60) + "s";
}

function fmtTime(epoch) {
  if (!epoch) return "";
  const date = new Date(epoch * 1000);
  const now = Date.now();
  const diffMin = Math.floor((now - date.getTime()) / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return diffMin + "m ago";
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return diffH + "h ago";
  return date.toLocaleDateString();
}

async function api(method, path, body) {
  const headers = {"Content-Type": "application/json"};
  if (S.token) headers.Authorization = "Bearer " + S.token;
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401 && S.view !== "token") {
    showView("token");
    throw new Error("unauthorized");
  }
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
  if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
  return data;
}

/* ---------- views ---------- */

function showView(name) {
  S.view = name;
  for (const view of ["session", "usage", "config", "setup", "share", "token"]) {
    $("view-" + view).hidden = view !== name;
  }
  document.querySelectorAll("#side-views button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === name);
  });
  if (name === "usage") renderUsage();
  if (name === "config") renderConfig();
}

function showRightTab(name) {
  S.rightTab = name;
  $("tab-browser").classList.toggle("active", name === "browser");
  $("tab-files").classList.toggle("active", name === "files");
  $("pane-browser").hidden = name !== "browser";
  $("pane-files").hidden = name !== "files";
}

/* ---------- sessions ---------- */

async function loadSessions() {
  try {
    S.sessions = await api("GET", "/api/sessions");
  } catch (e) {
    if (e.message !== "unauthorized") toast("Could not load sessions: " + e.message);
    return;
  }
  const list = $("session-list");
  list.innerHTML = "";
  for (const session of S.sessions) {
    const li = document.createElement("li");
    if (S.current && session.id === S.current.id) li.className = "active";
    const btn = document.createElement("button");
    btn.innerHTML = `<span class="t">${esc(session.title)}</span>` +
      `<span class="m">${esc(fmtTime(session.last_active_at))} · ${esc(fmtCost(session.total_cost_usd, session.priced))}</span>`;
    btn.onclick = () => openSession(session.id, true);
    li.appendChild(btn);
    list.appendChild(li);
  }
}

async function openSession(id, push) {
  stopStream();
  try {
    S.current = await api("GET", "/api/sessions/" + id);
  } catch (e) {
    if (e.message !== "unauthorized") toast("Could not open session: " + e.message);
    return;
  }
  if (push) history.pushState({session: id}, "", "/session/" + id);
  showView("session");
  renderSession();
  loadSessions();
  streamSession(id);
}

function newSessionLocal() {
  stopStream();
  S.current = null;
  S.running = false;
  history.pushState({}, "", "/");
  showView("session");
  renderSession();
  loadSessions();
}

function renderSession() {
  const session = S.current;
  const thread = $("thread");
  const stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
  if (!session) {
    $("session-title").textContent = "New session";
    $("session-meta").textContent = "Send a message to start.";
    $("composer-cost").textContent = "";
    thread.innerHTML = `<div class="turn"><p class="reply">Start a chat. The browser stays alive across messages, so follow-ups continue where the last turn left off.</p></div>`;
    renderTabs([]);
    renderFiles([]);
    setLiveIdle("No live browser. Send a message to start one.");
    updateRunning(false);
    return;
  }
  $("session-title").textContent = session.title || "Session";
  const cost = fmtSessionCost(session);
  $("session-meta").textContent = `${session.browser} · ${session.status} · ${session.messages.length} turns · ${cost}`;
  $("composer-cost").textContent = "cost so far " + cost;
  thread.innerHTML = "";
  for (const message of session.messages) {
    const bubble = document.createElement("div");
    bubble.className = "msg-user";
    bubble.textContent = message.user_text;
    thread.appendChild(bubble);
    thread.appendChild(renderTurn(message));
  }
  const last = session.messages[session.messages.length - 1];
  updateRunning(!!last && (last.status === "queued" || last.status === "running"));
  renderTabs(session.tabs || session.last_tabs || []);
  renderFiles(session.files || []);
  renderLive(session);
  if (stick) thread.scrollTop = thread.scrollHeight;
}

function renderTurn(message) {
  const div = document.createElement("div");
  div.className = "turn";
  div.id = "turn-" + message.id;
  const steps = message.steps || [];
  const head = document.createElement("div");
  head.className = "turn-head";
  const worked = message.duration_ms != null ? fmtDur(message.duration_ms) : null;
  head.innerHTML =
    (worked ? `Worked for ${esc(worked)} · ` : "") +
    `${steps.length} steps · ${esc(fmtTurnCost(message))} ` +
    `<span class="status ${esc(message.status)}">${esc(message.status)}</span>`;
  div.appendChild(head);
  if (steps.length) {
    const details = document.createElement("details");
    if (S.openTurns.has(message.id)) details.open = true;
    details.ontoggle = () => {
      if (details.open) S.openTurns.add(message.id);
      else S.openTurns.delete(message.id);
    };
    const summary = document.createElement("summary");
    summary.textContent = steps.length === 1 ? "1 step" : steps.length + " steps";
    details.appendChild(summary);
    const ol = document.createElement("ol");
    ol.className = "steps";
    for (const step of steps) ol.appendChild(renderStep(step));
    details.appendChild(ol);
    div.appendChild(details);
  }
  if (message.agent_reply) {
    const reply = document.createElement("p");
    reply.className = "reply";
    reply.textContent = message.agent_reply;
    div.appendChild(reply);
  } else if (message.status === "running" || message.status === "queued") {
    const reply = document.createElement("p");
    reply.className = "reply";
    reply.textContent = "Working…";
    div.appendChild(reply);
  }
  if (message.error && message.status !== "done") {
    const err = document.createElement("p");
    err.className = "reply";
    err.textContent = "Error: " + message.error;
    div.appendChild(err);
  }
  return div;
}

function renderStep(step) {
  const li = document.createElement("li");
  if (step.operation === "CAPTCHA") li.className = "wall";
  const icon = OP_ICON[step.operation] || "·";
  const ms = step.ms != null ? Math.round(step.ms) + "ms" : "";
  li.innerHTML = `<span class="op">${esc(icon)}</span>` +
    `<span><span>${esc(step.target_label || step.operation || "")}</span>` +
    (step.note ? `<span class="note">${esc(step.note)}</span>` : "") + `</span>` +
    `<span class="ms">${esc(ms)}</span>`;
  return li;
}

function updateRunning(running) {
  S.running = running;
  $("send-btn").disabled = running;
  $("stop-btn").hidden = !running;
  clearInterval(S.timer);
  if (running) {
    S.timer = setInterval(() => {
      if (S.current) renderSession();
    }, 2000);
  }
}

/* ---------- right panel ---------- */

function renderTabs(tabs) {
  const strip = $("tabstrip");
  strip.innerHTML = "";
  let activeUrl = "";
  for (const tab of tabs || []) {
    const span = document.createElement("span");
    span.className = "tab" + (tab.active ? " on" : "");
    span.textContent = tab.title || tab.url || tab.key;
    span.title = tab.url || "";
    strip.appendChild(span);
    if (tab.active) activeUrl = tab.url || "";
  }
  $("addressbar").textContent = activeUrl;
}

function renderFiles(files) {
  const list = $("file-list");
  list.innerHTML = "";
  const badge = $("files-badge");
  badge.hidden = !files.length;
  badge.textContent = files.length;
  for (const file of files || []) {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = "#";
    link.textContent = file.name;
    link.onclick = (event) => { event.preventDefault(); downloadFile(file.name); };
    li.appendChild(link);
    const size = document.createElement("span");
    size.className = "sz";
    size.textContent = formatBytes(file.bytes);
    li.appendChild(size);
    list.appendChild(li);
  }
  if (!files.length) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="sz">Downloads and saved results land here.</span>`;
    list.appendChild(li);
  }
}

function formatBytes(value) {
  if (value == null) return "";
  if (value < 1024) return value + " B";
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
  return (value / (1024 * 1024)).toFixed(1) + " MB";
}

async function downloadFile(name) {
  if (!S.current) return;
  const headers = {};
  if (S.token) headers.Authorization = "Bearer " + S.token;
  try {
    const response = await fetch(`/api/sessions/${S.current.id}/files/${encodeURIComponent(name)}`, {headers});
    if (!response.ok) throw new Error("HTTP " + response.status);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) {
    toast("Download failed: " + e.message);
  }
}

function renderLive(session) {
  const img = $("live-img");
  const frame = $("live-frame");
  if (session.browser === "cloud" && session.live_url) {
    const sep = session.live_url.includes("?") ? "&" : "?";
    const src = session.live_url + sep + "readOnly=true";
    if (frame.src !== src) frame.src = src;
    frame.hidden = false;
    img.hidden = true;
    $("live-idle").style.display = "none";
    return;
  }
  frame.hidden = true;
  frame.src = "about:blank";
  if (!img.src || img.hidden) setLiveIdle(session.browser_alive ? "Waiting for frames…" : "Browser is closed.");
}

function setLiveIdle(text) {
  $("live-img").hidden = true;
  $("live-img").removeAttribute("src");
  $("live-frame").hidden = true;
  const idle = $("live-idle");
  idle.style.display = "flex";
  idle.textContent = text;
}

function showFrame(image) {
  if (!image) return;
  const img = $("live-img");
  img.src = "data:image/jpeg;base64," + image;
  img.hidden = false;
  $("live-frame").hidden = true;
  $("live-idle").style.display = "none";
}

/* ---------- live stream ---------- */

function stopStream() {
  if (S.streamAbort) {
    S.streamAbort.abort();
    S.streamAbort = null;
  }
  S.streamId = null;
}

function scheduleReload() {
  clearTimeout(S.reloadTimer);
  S.reloadTimer = setTimeout(async () => {
    if (!S.current || S.view !== "session") return;
    try {
      S.current = await api("GET", "/api/sessions/" + S.current.id);
      renderSession();
    } catch (e) { /* session may be deleted */ }
  }, 250);
}

function onEvent(event) {
  if (!event || !S.current) return;
  switch (event.type) {
    case "frame":
      if (S.current.browser === "local") showFrame(event.image);
      break;
    case "tabs":
      renderTabs(event.tabs || []);
      if (S.current) S.current.tabs = event.tabs;
      break;
    case "files":
      renderFiles(event.files || []);
      if (S.current) S.current.files = event.files;
      break;
    case "live_url":
      if (S.current) {
        S.current.live_url = event.live_url;
        renderLive(S.current);
      }
      break;
    case "step":
    case "message":
    case "reply":
    case "restart":
    case "status":
      scheduleReload();
      break;
    default:
      break;
  }
}

async function streamSession(id) {
  stopStream();
  const ctrl = new AbortController();
  S.streamAbort = ctrl;
  S.streamId = id;
  const headers = {};
  if (S.token) headers.Authorization = "Bearer " + S.token;
  let response;
  try {
    response = await fetch(`/api/sessions/${id}/events`, {headers, signal: ctrl.signal});
  } catch (e) {
    return;
  }
  if (!response.ok || !response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream: true});
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          try {
            if (S.streamId === id) onEvent(JSON.parse(line.slice(6)));
          } catch (e) { /* partial chunk */ }
        }
      }
    }
  } catch (e) {
    /* aborted or dropped */
  }
  if (S.streamId === id && S.current && S.current.id === id) {
    setTimeout(() => {
      if (S.streamId === id) streamSession(id);
    }, 2000);
  }
}

/* ---------- composer ---------- */

async function sendMessage(text) {
  text = (text || "").trim();
  if (!text || S.running) return;
  try {
    if (!S.current) {
      const created = await api("POST", "/api/sessions", {browser: $("browser-picker").value});
      history.pushState({session: created.id}, "", "/session/" + created.id);
      S.current = await api("GET", "/api/sessions/" + created.id);
      streamSession(created.id);
    }
    await api("POST", `/api/sessions/${S.current.id}/messages`, {text});
    $("message-box").value = "";
    S.current = await api("GET", "/api/sessions/" + S.current.id);
    renderSession();
    loadSessions();
  } catch (e) {
    if (e.message !== "unauthorized") toast("Send failed: " + e.message);
  }
}

async function stopCurrent() {
  if (!S.current) return;
  try {
    await api("POST", `/api/sessions/${S.current.id}/stop`, {});
    scheduleReload();
  } catch (e) {
    toast("Stop failed: " + e.message);
  }
}

async function deleteCurrent() {
  if (!S.current) return;
  if (!confirm("Delete this session and its browser?")) return;
  try {
    await api("DELETE", "/api/sessions/" + S.current.id);
    newSessionLocal();
  } catch (e) {
    toast("Delete failed: " + e.message);
  }
}

async function shareCurrent() {
  if (!S.current) {
    toast("Open a session first.");
    return;
  }
  try {
    const data = await api("POST", `/api/sessions/${S.current.id}/share`, {});
    const url = location.origin + data.url;
    await navigator.clipboard.writeText(url);
    toast("Read-only link copied.");
  } catch (e) {
    toast("Share failed: " + e.message);
  }
}

async function attachFile(file) {
  if (!S.current) {
    toast("Open or start a session first.");
    return;
  }
  if (!file || file.size > 8 * 1024 * 1024) {
    toast("File must be under 8 MiB.");
    return;
  }
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  const content = String(dataUrl).split(",", 2)[1] || "";
  try {
    await api("POST", `/api/sessions/${S.current.id}/files`, {name: file.name, content_b64: content});
    toast("Attached " + file.name);
    scheduleReload();
  } catch (e) {
    toast("Attach failed: " + e.message);
  }
}

async function takeOver() {
  if (!S.current) {
    toast("Open a session first.");
    return;
  }
  try {
    const data = await api("POST", `/api/sessions/${S.current.id}/takeover`, {});
    if (data.mode === "cloud" && data.live_url) {
      window.open(data.live_url, "_blank", "noopener");
    } else if (data.mode === "cloud") {
      toast("Live URL is hidden. Set JEV_CLOUD_SHOW_LIVE_URL=1 on the server.");
    } else {
      toast("Browser brought forward. Drive it yourself; the agent keeps its tabs.");
    }
  } catch (e) {
    toast("Take over failed: " + e.message);
  }
}

/* ---------- usage / config / share ---------- */

async function renderUsage() {
  const body = $("usage-body");
  try {
    const rows = await api("GET", "/api/usage");
    if (!rows.length) {
      body.innerHTML = "<p>No usage yet.</p>";
      return;
    }
    const totals = {sessions: 0, turns: 0, steps: 0, tokens: 0, cost: 0, priced: 0, cloud_seconds: 0, cloud_cost: 0};
    let html = `<table class="usage"><tr><th>Day</th><th>Sessions</th><th>Turns</th><th>Steps</th><th>Tokens</th><th>Cost</th></tr>`;
    for (const row of rows) {
      totals.sessions += row.sessions || 0;
      totals.turns += row.turns || 0;
      totals.steps += row.steps || 0;
      totals.tokens += row.tokens || 0;
      totals.cost += row.cost || 0;
      if (row.priced === 1) totals.priced = 1;
      totals.cloud_seconds += row.cloud_seconds || 0;
      totals.cloud_cost += row.cloud_cost || 0;
      html += `<tr><td>${esc(row.day)}</td><td>${row.sessions}</td><td>${row.turns}</td>` +
        `<td>${row.steps}</td><td>${row.tokens}</td><td>${esc(fmtUsageCost(row))}</td></tr>`;
    }
    html += `<tr><td><strong>Total</strong></td><td>${totals.sessions}</td><td>${totals.turns}</td>` +
      `<td>${totals.steps}</td><td>${totals.tokens}</td><td>${esc(fmtUsageCost(totals))}</td></tr></table>`;
    body.innerHTML = html;
  } catch (e) {
    if (e.message !== "unauthorized") body.innerHTML = "<p>Could not load usage.</p>";
  }
}

async function renderConfig() {
  const form = $("config-form");
  try {
    const config = await api("GET", "/api/config");
    form.innerHTML =
      `<label>Default browser for new sessions
        <select name="browser_default">
          <option value="local"${config.browser_default === "local" ? " selected" : ""}>local</option>
          <option value="cloud"${config.browser_default === "cloud" ? " selected" : ""}>cloud</option>
        </select></label>
       <label>Default max decisions per turn (empty = agent default)
        <input name="max_decisions_default" type="number" min="1" max="10000" value="${config.max_decisions_default ?? ""}" /></label>
       <label>Text model
        <input name="text_model" value="${esc(config.text_model || "")}" /></label>
       <label>Decision provider
        <select name="decision_provider">
          <option value="typesafe"${config.decision_provider === "typesafe" ? " selected" : ""}>typesafe</option>
          <option value="mercury"${config.decision_provider === "mercury" ? " selected" : ""}>mercury</option>
        </select></label>
       <label>Server token ${config.server_token_set ? "(set — enter a new one to rotate)" : "(not set)"}
        <input name="server_token" type="password" autocomplete="off" placeholder="unchanged" /></label>
       <button type="submit" class="primary">Save</button>`;
  } catch (e) {
    if (e.message !== "unauthorized") form.innerHTML = "<p>Could not load configuration.</p>";
  }
}

async function saveConfig(form) {
  const data = new FormData(form);
  const body = {
    browser_default: data.get("browser_default"),
    decision_provider: data.get("decision_provider"),
    text_model: String(data.get("text_model") || "").trim(),
  };
  const maxDecisions = String(data.get("max_decisions_default") || "").trim();
  body.max_decisions_default = maxDecisions === "" ? null : Number(maxDecisions);
  const token = String(data.get("server_token") || "");
  if (token) body.server_token = token;
  try {
    await api("POST", "/api/config", body);
    if (token) {
      S.token = token;
      localStorage.setItem("jev-token", token);
    }
    toast("Configuration saved.");
    renderConfig();
  } catch (e) {
    if (e.message !== "unauthorized") toast("Save failed: " + e.message);
  }
}

async function renderShare(token) {
  showView("share");
  try {
    const response = await fetch("/api/share/" + token);
    if (!response.ok) throw new Error("HTTP " + response.status);
    const replay = await response.json();
    $("share-title").textContent = replay.title || "Shared session";
    $("share-meta").textContent = `${replay.browser || ""} · read-only replay`;
    const thread = $("share-thread");
    thread.innerHTML = "";
    for (const turn of replay.turns || []) {
      const bubble = document.createElement("div");
      bubble.className = "msg-user";
      bubble.textContent = turn.user_text;
      thread.appendChild(bubble);
      const div = document.createElement("div");
      div.className = "turn";
      const steps = turn.steps || [];
      div.innerHTML = `<div class="turn-head">` +
        (turn.duration_ms != null ? `Worked for ${esc(fmtDur(turn.duration_ms))} · ` : "") +
        `${steps.length} steps · ${esc(fmtTurnCost({...turn, cost_usd_estimate: turn.cost}))} ` +
        `<span class="status ${esc(turn.status)}">${esc(turn.status)}</span></div>`;
      if (steps.length) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = steps.length === 1 ? "1 step" : steps.length + " steps";
        details.appendChild(summary);
        const ol = document.createElement("ol");
        ol.className = "steps";
        for (const step of steps) ol.appendChild(renderStep(step));
        details.appendChild(ol);
        div.appendChild(details);
      }
      if (turn.agent_reply) {
        const reply = document.createElement("p");
        reply.className = "reply";
        reply.textContent = turn.agent_reply;
        div.appendChild(reply);
      }
      if (turn.frame) {
        const img = document.createElement("img");
        img.className = "frame";
        img.alt = "Final frame";
        img.src = "data:image/jpeg;base64," + turn.frame;
        div.appendChild(img);
      }
      thread.appendChild(div);
    }
  } catch (e) {
    $("share-title").textContent = "Link not found";
    $("share-meta").textContent = "";
    $("share-thread").innerHTML = "";
  }
}

/* ---------- wiring ---------- */

function route() {
  const path = location.pathname;
  if (path.startsWith("/share/")) {
    renderShare(path.slice("/share/".length));
    return;
  }
  if (path.startsWith("/session/")) {
    const id = path.slice("/session/".length);
    if (id) {
      openSession(id, false);
      return;
    }
  }
  loadSessions();
  renderSession();
}

document.addEventListener("DOMContentLoaded", () => {
  $("home-btn").onclick = newSessionLocal;
  document.querySelectorAll("#side-views button").forEach((btn) => {
    btn.onclick = () => {
      if (btn.dataset.view === "usage" || btn.dataset.view === "config" || btn.dataset.view === "setup") {
        showView(btn.dataset.view);
      }
    };
  });
  $("tab-browser").onclick = () => showRightTab("browser");
  $("tab-files").onclick = () => showRightTab("files");
  $("composer").onsubmit = (event) => {
    event.preventDefault();
    sendMessage($("message-box").value);
  };
  $("message-box").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage($("message-box").value);
    }
  });
  $("stop-btn").onclick = stopCurrent;
  $("delete-btn").onclick = deleteCurrent;
  $("share-btn").onclick = shareCurrent;
  $("takeover-btn").onclick = takeOver;
  $("fullscreen-btn").onclick = () => {
    const wrap = $("live-wrap");
    if (document.fullscreenElement) document.exitFullscreen();
    else if (wrap.requestFullscreen) wrap.requestFullscreen();
  };
  $("attach-input").onchange = (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (file) attachFile(file);
  };
  $("token-form").onsubmit = (event) => {
    event.preventDefault();
    S.token = $("token-input").value.trim();
    localStorage.setItem("jev-token", S.token);
    $("token-input").value = "";
    showView("session");
    route();
  };
  $("config-form").onsubmit = (event) => {
    event.preventDefault();
    saveConfig($("config-form"));
  };
  window.addEventListener("popstate", route);
  route();
});
