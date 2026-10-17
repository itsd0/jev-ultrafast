const $ = (id) => document.getElementById(id);
let currentId = null;
let liveAbort = null;
let liveFor = null;

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
        `<tr><td>${escape(s.n)}</td><td>${escape(s.operation)}</td>` +
        `<td>${escape(s.target_label)}${s.note ? ` <span title="${escape(s.note)}">*</span>` : ""}</td>` +
        `<td>${escape(s.ms)}</td></tr>`,
    )
    .join("");
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
  stopStream();
  $("live").removeAttribute("src");
  await refreshCurrent();
  await refreshHistory();
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
