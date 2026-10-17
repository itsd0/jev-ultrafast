const $ = (id) => document.getElementById(id);
let currentId = null;

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
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  return data;
}

function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
}

function renderTask(task) {
  const done = ["done", "blocked", "failed"].includes(task.status);
  $("current-meta").textContent =
    `${task.id.slice(0, 8)} · ${task.status} · ${task.browser} · ` +
    `${task.steps.length} steps · in=${task.input_tokens ?? "–"} out=${task.output_tokens ?? "–"} · ` +
    `~$${task.cost_usd_estimate ?? "–"} · agent-done=${task.verified_by_agent ? "yes" : "no"}` +
    (task.error ? ` · ${task.error}` : "") +
    (task.final_url ? ` · ${task.final_url}` : "");
  $("stop").disabled = done;
  $("live").innerHTML = task.live_url
    ? `<a href="${escape(task.live_url)}" target="_blank" rel="noreferrer">live view</a>`
    : "";
  $("steps").innerHTML = task.steps
    .map(
      (s) =>
        `<tr><td>${escape(s.n)}</td><td>${escape(s.operation)}</td>` +
        `<td>${escape(s.target_label)}</td><td>${escape(s.url)}</td><td>${escape(s.ms)}</td></tr>`,
    )
    .join("");
  return done;
}

async function refreshCurrent() {
  if (!currentId) return true;
  try {
    return renderTask(await api(`/api/tasks/${currentId}`));
  } catch (error) {
    showError(error.message);
    return true;
  }
}

async function refreshHistory() {
  try {
    const tasks = await api("/api/tasks");
    $("history").innerHTML = tasks
      .map(
        (t) =>
          `<li><a data-id="${escape(t.id)}">${escape(t.id.slice(0, 8))}</a> ` +
          `${escape(t.status)} · ${escape(t.browser)} · ${escape(t.step_count)} steps · ` +
          `${escape(t.goal.slice(0, 80))}</li>`,
      )
      .join("");
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
  await refreshCurrent();
});

$("token").value = sessionStorage.getItem("jev-token") || "";

setInterval(refreshCurrent, 1000);
setInterval(refreshHistory, 3000);
refreshHistory();
