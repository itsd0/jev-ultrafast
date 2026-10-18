const $ = (id) => document.getElementById(id);
let currentId = null;
let liveAbort = null;
let liveFor = null;
let traceCache = { id: null, steps: -1, events: [] };
let debugStep = null;
const PHASE_COLORS = { snapshot: "#4c8caf", decision: "#4caf7d", text: "#c9a227", action: "#b06ab0", verify: "#777" };

const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

function headers() {
  const h = { "Content-Type": "application/json" };
  const token = sessionStorage.getItem("jev-token") || $("token").value.trim();
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(), ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.error || `Request failed (${response.status})`);
  return data;
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
}

function wallClock(task) {
  if (!task.started_at) return "–";
  const end = task.ended_at || Date.now() / 1000;
  return `${(end - task.started_at).toFixed(1)} s`;
}

function stopStream() {
  if (liveAbort) {
    liveAbort.abort();
    liveAbort = null;
    liveFor = null;
  }
}

function showIdle(text) {
  $("live").style.display = "none";
  $("liveframe").style.display = "none";
  $("liveframe").src = "about:blank";
  $("liveidle").style.display = "block";
  $("liveidle").textContent = text;
}

function showCloud(liveUrl) {
  const sep = liveUrl.includes("?") ? "&" : "?";
  $("liveframe").src = `${liveUrl}${sep}readOnly=true`;
  $("liveframe").style.display = "block";
  $("live").style.display = "none";
  $("liveidle").style.display = "none";
}

async function streamLocal(taskId) {
  if (liveFor === taskId) return;
  stopStream();
  const controller = new AbortController();
  liveAbort = controller;
  liveFor = taskId;
  $("live").style.display = "block";
  $("liveframe").style.display = "none";
  $("liveidle").style.display = "none";
  try {
    const response = await fetch(`/api/tasks/${taskId}/live`, {
      signal: controller.signal,
      headers: headers(),
    });
    if (!response.ok) throw Error(`Live view unavailable (${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const event = JSON.parse(line.slice(5));
          if (event.ended) return;
          if (event.image) $("live").src = `data:image/jpeg;base64,${event.image}`;
        }
      }
    }
  } catch (error) {
    if (error.name !== "AbortError" && liveFor === taskId) {
      showIdle(`Live view ended (${error.message}). Last frame kept.`);
      liveFor = null;
    }
  }
}

function renderTask(task) {
  const terminal = ["done", "blocked", "failed"].includes(task.status);
  $("badge").textContent = task.status;
  $("badge").className = `badge ${task.status}`;
  $("current-meta").innerHTML =
    `<b>${escape(task.id.slice(0, 8))}</b> · ${escape(task.browser)} · ${escape(task.goal.slice(0, 90))}<br>` +
    `time <b>${wallClock(task)}</b> · steps <b>${task.steps.length}</b> · ` +
    `tokens <b>${task.input_tokens ?? "–"}/${task.output_tokens ?? "–"}</b> · ` +
    `cost <b>~$${task.cost_usd_estimate ?? "–"}</b> · agent-done <b>${task.verified_by_agent ? "yes" : "no"}</b>` +
    (task.error ? `<br>error: ${escape(task.error)}` : "");
  $("stop").disabled = terminal;
  $("steps").innerHTML = task.steps
    .map(
      (s) =>
        `<tr data-n="${escape(s.n)}" class="${s.n === debugStep ? "selected" : ""}"><td>${escape(s.n)}</td>` +
        `<td>${escape(s.operation)}</td>` +
        `<td>${escape(s.target_label)}${s.note ? ` <span title="${escape(s.note)}">*</span>` : ""}</td>` +
        `<td>${escape(s.ms)}</td></tr>`,
    )
    .join("");
  refreshDebug(task, terminal);
  const lastUrl = task.steps.length ? task.steps[task.steps.length - 1].url : null;
  $("urlbar").textContent = task.final_url || lastUrl || task.url || "";
  if (!terminal && task.browser === "local") {
    streamLocal(task.id);
  } else if (!terminal && task.browser === "cloud" && task.live_url) {
    stopStream();
    showCloud(task.live_url);
  } else if (!terminal) {
    stopStream();
    showIdle("Live view is not available for this task.");
  } else {
    stopStream();
    if (liveFor === null && $("live").src) {
      $("liveidle").style.display = "none";
    } else if (!$("live").src) {
      showIdle(`Task ${task.status}. No frames were captured.`);
    }
  }
  return terminal;
}

async function refreshDebug(task, terminal) {
  if (traceCache.id !== task.id || traceCache.steps !== task.steps.length) {
    try {
      const trace = await api(`/api/tasks/${task.id}/trace`);
      traceCache = { id: task.id, steps: task.steps.length, events: trace.events || [] };
    } catch (error) {
      traceCache = { id: task.id, steps: task.steps.length, events: [] };
    }
  }
  const events = traceCache.events;
  const end = events.find((e) => e.kind === "end");
  if (end && end.data.phases) {
    const phases = end.data.phases;
    const total = Object.values(phases).reduce((a, b) => a + b, 0) || 1;
    $("phasebar").innerHTML = Object.entries(phases)
      .map(
        ([name, ms]) =>
          `<span style="flex:${ms};background:${PHASE_COLORS[name] || "#555"}" ` +
          `title="${escape(name)} ${escape(ms)}ms"></span>`,
      )
      .join("");
    $("phasebar").title = `snapshot ${phases.snapshot}ms · decision ${phases.decision}ms · ` +
      `text ${phases.text}ms · action ${phases.action}ms · verify ${phases.verify}ms (total ${total}ms)`;
  } else {
    $("phasebar").innerHTML = "";
  }
  if (terminal && (task.status === "blocked" || task.status === "failed") && !traceCache.shotDone) {
    traceCache.shotDone = true;
    try {
      const response = await fetch(`/api/tasks/${task.id}/failshot`, { headers: headers() });
      if (response.ok) {
        const blob = await response.blob();
        $("failshot").src = URL.createObjectURL(blob);
        $("failshot").hidden = false;
      }
    } catch (error) {
      /* No failure screenshot; the trace still explains the run. */
    }
  }
  if (!task.steps.length || !events.length) {
    if (!task.steps.length) $("debug-panel").hidden = true;
    return;
  }
  $("debug-panel").hidden = false;
  if (debugStep === null || !task.steps.some((s) => s.n === debugStep)) {
    const failed = [...events].reverse().find((e) => e.kind === "verify" && e.data.success === false);
    debugStep = failed ? failed.step : task.steps[task.steps.length - 1].n;
  }
  renderDebugStep(events);
}

function renderDebugStep(events) {
  $("debug-step").textContent = `#${debugStep}`;
  document.querySelectorAll("#steps tr[data-n]").forEach((row) => {
    row.classList.toggle("selected", Number(row.dataset.n) === debugStep);
  });
  const at = events.filter((e) => e.step === debugStep);
  const first = (kind) => at.find((e) => e.kind === kind);
  const offered = first("offered");
  const decision = first("decision");
  const verify = first("verify");
  const guards = at.filter((e) => e.kind === "guard");
  let html = "";
  if (offered) {
    const rows = (offered.data.offered || [])
      .map((c) => `[${escape(c.id)}] ${escape(c.label)} (${escape(c.role || c.kind)})`)
      .join("\n");
    html += `<h3>Offered (${offered.data.offered_count} controls)</h3><pre>${rows || "—"}</pre>`;
    const hidden = { ...(offered.data.held || {}), ...(offered.data.suppressed || {}) };
    if (Object.keys(hidden).length) {
      html += `<h3>Held / suppressed</h3><pre>${escape(JSON.stringify(hidden, null, 1))}</pre>`;
    }
  }
  if (decision) {
    html += `<h3>Decision</h3><pre>${escape(JSON.stringify({
      choice: decision.data.choice, operation: decision.data.operation,
      confidence: decision.data.confidence, latency_ms: decision.data.latency_ms,
      retries: decision.data.retries, invalid_reason: decision.data.invalid_reason,
    }, null, 1))}</pre>`;
  }
  if (verify) {
    html += `<h3>Verify (${verify.data.success ? "ok" : "FAILED"})</h3><pre>${escape(JSON.stringify({
      before: verify.data.before, after: verify.data.after,
      changed: verify.data.changed, next: verify.data.next_strategy,
    }, null, 1))}</pre>`;
  }
  if (guards.length) {
    html += `<h3>Guards</h3><pre>${escape(JSON.stringify(guards.map((g) => g.data), null, 1))}</pre>`;
  }
  $("debug").innerHTML = html || "<p>No trace events for this step yet.</p>";
}

async function refreshCurrent() {
  if (!currentId) return;
  try {
    await api(`/api/tasks/${currentId}`).then(renderTask);
  } catch (error) {
    showError(error.message);
  }
}

async function refreshHistory() {
  try {
    const tasks = await api("/api/tasks");
    $("history").innerHTML = tasks.length
      ? tasks
          .map(
            (t) =>
              `<li data-id="${escape(t.id)}" class="${t.id === currentId ? "current" : ""}">` +
              `<b>${escape(t.id.slice(0, 8))}</b> ${escape(t.status)} · ${escape(t.browser)} · ` +
              `${escape(t.step_count)} steps · ${escape(t.goal.slice(0, 70))}</li>`,
          )
          .join("")
      : "<li>No tasks yet. Describe a goal above and press Run.</li>";
  } catch (error) {
    showError(error.message);
  }
}

$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("error").hidden = true;
  sessionStorage.setItem("jev-token", $("token").value.trim());
  const maxDecisions = $("max-decisions").value.trim();
  try {
    const created = await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        goal: $("goal").value,
        url: $("url").value,
        browser: $("browser").value,
        ...(maxDecisions ? { max_decisions: Number(maxDecisions) } : {}),
      }),
    });
    currentId = created.id;
    stopStream();
    $("live").removeAttribute("src");
    showIdle("Connecting to the task browser…");
    await refreshCurrent();
    await refreshHistory();
  } catch (error) {
    showError(error.message);
  }
});

$("stop").addEventListener("click", async () => {
  if (!currentId) return;
  try {
    await api(`/api/tasks/${currentId}/stop`, { method: "POST", body: "{}" });
    await refreshCurrent();
  } catch (error) {
    showError(error.message);
  }
});

$("history").addEventListener("click", async (event) => {
  const id = event.target.closest("[data-id]")?.dataset.id;
  if (!id) return;
  currentId = id;
  traceCache = { id: null, steps: -1, events: [] };
  debugStep = null;
  $("failshot").hidden = true;
  $("failshot").removeAttribute("src");
  stopStream();
  $("live").removeAttribute("src");
  await refreshCurrent();
  await refreshHistory();
});

$("steps").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-n]");
  if (!row) return;
  debugStep = Number(row.dataset.n);
  if (traceCache.events.length) renderDebugStep(traceCache.events);
});

$("token").value = sessionStorage.getItem("jev-token") || "";

const deepLink = new URLSearchParams(location.search).get("task");
if (deepLink) {
  currentId = deepLink;
  refreshCurrent();
}

setInterval(refreshCurrent, 1000);
setInterval(refreshHistory, 3000);
refreshHistory();
