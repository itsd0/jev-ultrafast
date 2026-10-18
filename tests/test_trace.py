"""Offline contracts for structured run traces. No browsers, no paid APIs."""

import base64
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from jev_ultrafast import server as hosted
from jev_ultrafast.trace import LEVELS, Trace, diagnose_trace, scrub, trace_dir


def test_log_levels_default_to_info(monkeypatch):
    monkeypatch.delenv("JEV_LOG_LEVEL", raising=False)
    from jev_ultrafast.trace import log_level

    assert log_level() == LEVELS["info"]
    monkeypatch.setenv("JEV_LOG_LEVEL", "debug")
    assert log_level() == LEVELS["debug"]
    monkeypatch.setenv("JEV_LOG_LEVEL", "off")
    assert log_level() == LEVELS["off"]


def test_tracer_buffers_and_flushes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path))
    trace = Trace(run_id="abc123", level=1)
    trace.emit("snapshot", step=1, url="https://example.test/", controls=3, duration_ms=12)
    assert not (tmp_path / "abc123" / "trace.jsonl").exists()
    trace.flush()
    lines = (tmp_path / "abc123" / "trace.jsonl").read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert (event["seq"], event["step"], event["kind"]) == (0, 1, "snapshot")
    assert event["data"]["controls"] == 3
    trace.close()


def test_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path))
    trace = Trace(run_id="silent", level=0)
    assert trace.emit("snapshot", step=1, url="https://example.test/") is None
    trace.flush()
    trace.save_failure(1, "aGk=")
    assert not (tmp_path / "silent").exists()
    trace.close()


def test_fake_key_never_reaches_the_trace(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-test-key-987654321")
    monkeypatch.setenv("BROWSER_USE_API_KEY", "fake-bu-key-123456789")
    trace = Trace(run_id="scrubbed", level=2)
    trace.emit("decision", step=1, raw={"key": "fake-test-key-987654321"})
    trace.emit("error", step=1, message="using fake-bu-key-123456789 failed")
    trace.emit("snapshot", step=1, headers={"Authorization": "Bearer abcdef", "X-Browser-Use-API-Key": "k"})
    trace.flush()
    body = (tmp_path / "scrubbed" / "trace.jsonl").read_text()
    assert "fake-test-key-987654321" not in body
    assert "fake-bu-key-123456789" not in body
    assert "Bearer abcdef" not in body
    assert "[redacted]" in body
    trace.close()


def test_sensitive_field_values_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path))
    trace = Trace(run_id="cards", level=1)
    trace.emit("text", step=2, purpose="field", field_label="Credit card number", value="4111111111111111")
    trace.emit("text", step=3, purpose="field", field_label="Search", value="usb-c hub")
    trace.flush()
    body = (tmp_path / "cards" / "trace.jsonl").read_text()
    assert "4111111111111111" not in body
    assert "usb-c hub" in body
    trace.close()


def test_scrub_handles_headers_and_inline_keys():
    assert scrub({"Authorization": "Bearer xyz", "other": "ok"}) == {"Authorization": "[redacted]", "other": "ok"}
    assert scrub("call with api_key=supersecret9 here") == "call with api_key=[redacted] here"
    assert scrub("Bearer abc.def-ghi") == "Bearer [redacted]"
    assert scrub({"nested": [{"X-Browser-Use-API-Key": "k"}]}) == {"nested": [{"X-Browser-Use-API-Key": "[redacted]"}]}


def test_scrub_keeps_token_counts():
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "outputTokens": 9, "access_token": "secret"}
    cleaned = scrub(usage)
    assert cleaned["access_token"] == "[redacted]"
    assert (cleaned["prompt_tokens"], cleaned["completion_tokens"], cleaned["outputTokens"]) == (100, 50, 9)


def test_debug_payloads_are_truncated(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path))
    trace = Trace(run_id="big", level=2)
    trace.emit("offered", step=1, offered_full=[{"id": f"e{i}"} for i in range(500)])
    trace.flush()
    event = json.loads((tmp_path / "big" / "trace.jsonl").read_text().splitlines()[0])
    assert len(event["data"]["offered_full"]) < 500
    trace.close()


def test_action_emits_a_one_line_summary(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path))
    trace = Trace(run_id="9339c1e59999", level=1)
    with caplog.at_level(logging.INFO, logger="jev"):
        trace.emit("action", step=4, operation="CLICK", target="Sunday, November 15", duration_ms=312)
    assert "[jev 9339c1e5 #4] CLICK 'Sunday, November 15' ok 312ms" in caplog.text
    trace.close()


def test_failure_bundle_saves_shot_and_last_offered(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path))
    trace = Trace(run_id="failed-run", level=1)
    for step in range(1, 6):
        trace.emit("offered", step=step, offered=[{"id": f"e{step}"}], offered_count=1)
    trace.save_failure(5, base64.b64encode(b"fake-png").decode())
    assert (tmp_path / "failed-run" / "fail-step-5.png").read_bytes() == b"fake-png"
    offered = json.loads((tmp_path / "failed-run" / "offered-last3.json").read_text())
    assert [item["step"] for item in offered] == [3, 4, 5]
    trace.close()


def test_step_shots_are_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path))
    monkeypatch.setenv("JEV_TRACE_SCREENSHOTS", "1")
    trace = Trace(run_id="shots", level=1)
    trace.maybe_step_shot(2, base64.b64encode(b"png").decode())
    assert (tmp_path / "shots" / "step-2.png").exists()
    trace.close()


def test_events_table_migrates_old_dbs(tmp_path):
    import sqlite3

    old = tmp_path / "old.db"
    conn = sqlite3.connect(str(old))
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT, url TEXT, browser TEXT)")
    conn.execute("INSERT INTO tasks (id, goal, url, browser) VALUES ('t1', 'g', 'u', 'local')")
    conn.commit()
    conn.close()
    store = hosted.Store(old)
    store.add_events("t1", [(0, 1, "snapshot", 111, '{"controls": 2}')])
    assert store.get_events("t1")[0]["data"] == {"controls": 2}
    assert store.get_task("t1")["goal"] == "g"


def test_events_roundtrip(tmp_path):
    store = hosted.Store(tmp_path / "runs.db")
    task_id = store.create_task("goal", "about:blank", "local", None)
    store.add_events(task_id, [(0, 1, "action", 10, "{}"), (1, 1, "verify", 11, "{}")])
    events = store.get_events(task_id)
    assert [(event["seq"], event["kind"]) for event in events] == [(0, "action"), (1, "verify")]


class SilentAgent:
    """A finished task with no trace of its own; the server still ends it cleanly."""

    def __init__(self, url, goal, screenshots=False):
        self.config = {}

    def command(self, name, body=None):
        return {"status": "done", "history": [], "decisions": [], "text_calls": [], "page": {"url": "u"}}

    def snapshot(self):
        return self.command("tick")

    def close(self):
        pass


@pytest.fixture
def trace_service(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_TRACE_DIR", str(tmp_path / "runs"))
    services = []

    def start(token=""):
        store = hosted.Store(tmp_path / f"runs-{len(services)}.db")
        runner = hosted.Runner(store, agent_factory=SilentAgent)
        handler = type("Handler", (hosted.Handler,), {})
        handler.STORE = store
        handler.RUNNER = runner
        handler.HOST = "127.0.0.1"
        handler.TOKEN = token
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        handler.PORT = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        services.append((server, thread))
        return f"http://127.0.0.1:{handler.PORT}", store

    yield start
    for server, thread in services:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def call(base, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + path, method=method, data=None if method == "GET" else json.dumps(body or {}).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def test_trace_endpoints_need_the_token(trace_service):
    base, _store = trace_service(token="secret")
    status, _, _ = call(base, "GET", "/api/tasks/abc/trace")
    assert status == 401
    status, _, _ = call(base, "GET", "/api/tasks/abc/trace.jsonl")
    assert status == 401
    status, _, _ = call(base, "GET", "/api/tasks/abc/failshot")
    assert status == 401


def test_trace_endpoints_serve_events_and_download(trace_service):
    base, store = trace_service()
    status, body, _ = call(base, "POST", "/api/tasks", {"goal": "Do it", "browser": "local"})
    assert status == 201
    task_id = json.loads(body)["id"]
    store.add_events(task_id, [(0, 1, "action", 10, '{"ok": true}')])
    status, body, _ = call(base, "GET", f"/api/tasks/{task_id}/trace")
    assert status == 200
    assert json.loads(body)["events"][0]["kind"] == "action"
    status, body, headers = call(base, "GET", f"/api/tasks/{task_id}/trace.jsonl")
    assert status == 200
    assert json.loads(body.decode().splitlines()[0])["kind"] == "action"
    assert "attachment" in headers.get("Content-Disposition", "")
    status, _, _ = call(base, "GET", "/api/tasks/missing/trace")
    assert status == 404


def test_failshot_serves_only_real_runs(trace_service, tmp_path):
    base, store = trace_service()
    status, body, _ = call(base, "POST", "/api/tasks", {"goal": "Do it", "browser": "local"})
    task_id = json.loads(body)["id"]
    status, _, _ = call(base, "GET", f"/api/tasks/{task_id}/failshot")
    assert status == 404
    shot = trace_dir(task_id)
    shot.mkdir(parents=True, exist_ok=True)
    (shot / "fail-step-2.png").write_bytes(b"png-bytes")
    status, body, _ = call(base, "GET", f"/api/tasks/{task_id}/failshot")
    assert status == 200
    assert body == b"png-bytes"
    status, _, _ = call(base, "GET", "/api/tasks/../x/failshot")
    assert status == 404


def test_no_print_statements_in_the_package():
    root = Path(__file__).resolve().parent.parent / "jev_ultrafast"
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name.startswith("._"):
            continue  # AppleDouble metadata, not source.
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*print\(", line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == []


def _event(event, step=1, **data):
    return {"seq": 0, "step": step, "kind": event, "ts_ms": 0, "data": data}


def test_diagnose_maps_each_cause():
    assert diagnose_trace([_event("error", type="RuntimeError", message="Model connection failed")])[0] == (
        "environment/API error"
    )
    assert diagnose_trace([_event("end", status="blocked", blocked_reason="decision budget")])[0] == "budget"
    guards = [_event("guard", type="loop", pattern="A/B") for _ in range(3)]
    assert diagnose_trace(guards + [_event("end", status="blocked", blocked_reason="loop detected")])[0] == "loop"
    verify = [_event("verify", step=n, kind="check", success=False) for n in (1, 2)]
    assert diagnose_trace(verify + [_event("end", status="blocked")])[0] == "widget not verified"
    back = [_event("guard", type="back", label="More")]
    assert diagnose_trace(back + [_event("end", status="blocked", blocked_reason="gave up")])[0] == "wandering"
    progress = [_event("progress", todo=["weather"], complete=False)]
    assert diagnose_trace(progress + [_event("end", status="blocked", blocked_reason="gave up")])[0] == (
        "second-hop missing"
    )
    assert diagnose_trace([_event("end", status="done")]) == (None, "")
    assert diagnose_trace([_event("snapshot", step=1)]) == ("no end event", "trace cut off before the run finished")


def test_trace_view_shows_stale_targets_and_covers():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "trace_view", Path(__file__).resolve().parent.parent / "scripts" / "trace_view.py"
    )
    view = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(view)
    stale = {
        "kind": "stale", "step": 4,
        "data": {
            "resolution": "retargeted", "candidates": 1,
            "diff": {
                "target": [
                    {"kind": "click", "role": "link", "label": "Earth"},
                    {"kind": "click", "role": "link", "label": "Earth facts"},
                ],
            },
        },
    }
    line = view.one_line(stale)
    assert "retargeted" in line and "Earth" in line and "Earth facts" in line
    covered = {
        "kind": "guard", "step": 5,
        "data": {"type": "covered", "cover_tag": "DIV", "cover_role": "dialog", "evidence": "x"},
    }
    assert "by <DIV>/dialog" in view.one_line(covered)
    legacy = {"kind": "stale", "step": 6, "data": {"resolution": "missing", "diff": {}}}
    assert "missing" in view.one_line(legacy)
