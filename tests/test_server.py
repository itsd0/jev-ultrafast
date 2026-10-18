"""Offline contracts for the hosted task layer. No browsers, no paid APIs."""

import json
import pathlib
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jev_ultrafast import server as hosted


def snap(status, steps, url="https://example.test/"):
    history = [
        {
            "step": index + 1,
            "action": f"Target {index + 1}",
            "kind": "click",
            "choice": f"e{index + 1}",
            "operation": "CLICK",
            "target": str(index + 1),
            "url": url,
            "elapsed_ms": (index + 1) * 100,
            "note": None,
        }
        for index in range(steps)
    ]
    return {
        "status": status,
        "history": history,
        "decisions": [{"usage": {"prompt_tokens": 10, "completion_tokens": 2}} for _ in range(steps)],
        "text_calls": [],
        "page": {"url": url},
    }


class FakeBrowser:
    def __init__(self, fail_screencast=False):
        self.session = "sess-1"
        self.calls = []
        self.fail_screencast = fail_screencast
        self.cloud = None

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "Page.startScreencast" and self.fail_screencast:
            raise RuntimeError("no screencast")
        if method == "Page.captureScreenshot":
            return {"data": "shot-bytes"}
        return {}


class FakeAgent:
    def __init__(self, url, goal, script, browser=None, screenshots=False, background=""):
        self.url = url
        self.goal = goal
        self.script = script
        self.config = {}
        self.browser = browser
        self.background = background
        self.closed = False
        self.ticks = 0
        self.last = snap("running", 0)

    def command(self, name, body=None):
        assert name == "tick"
        self.ticks += 1
        self.last = self.script(self)
        return self.last

    def snapshot(self):
        return self.last

    def close(self, keep_browser=False):
        self.closed = True


@pytest.fixture
def start_service(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_COST_PER_1K_TOKENS", raising=False)
    monkeypatch.delenv("JEV_SERVER_TOKEN", raising=False)
    services = []

    def start(script, max_concurrent=2, token="", browser=None):
        fakes = []

        def factory(url, goal, browser=None, background=""):
            fake = FakeAgent(url, goal, script, browser=browser, background=background)
            fakes.append(fake)
            return fake

        def browsers(url):
            if callable(browser):
                return browser()
            return browser if browser is not None else FakeBrowser()

        store = hosted.Store(tmp_path / f"runs-{len(services)}.db")
        runner = hosted.SessionRunner(
            store, max_concurrent=max_concurrent, agent_factory=factory,
            browser_factory=browsers, files_root=tmp_path / "files",
        )
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
        return f"http://127.0.0.1:{handler.PORT}", fakes

    yield start

    for server, thread in services:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def api(base, method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if method == "GET" else json.dumps(body or {}).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def wait_for(base, task_id, statuses, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, task = api(base, "GET", f"/api/tasks/{task_id}")
        if task["status"] in statuses:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never reached {statuses}")


def test_create_runs_to_done_and_records_steps(start_service):
    base, fakes = start_service(lambda fake: snap("done", 2) if fake.ticks > 1 else snap("running", 1))
    status, created = api(base, "POST", "/api/tasks", {
        "goal": "Do the thing", "url": "https://example.test/", "max_decisions": 5,
    })
    assert status == 201
    task = wait_for(base, created["id"], {"done"})
    assert task["final_url"] == "https://example.test/"
    assert task["verified_by_agent"] == 1
    assert task["input_tokens"] == 20
    assert task["output_tokens"] == 4
    assert task["cost_usd_estimate"] == 0.0
    assert [(s["operation"], s["target_label"], s["url"], s["ms"]) for s in task["steps"]] == [
        ("CLICK", "Target 1", "https://example.test/", 100),
        ("CLICK", "Target 2", "https://example.test/", 200),
    ]
    assert fakes[0].config["max_decisions"] == 5
    assert fakes[0].closed is True
    _, listed = api(base, "GET", "/api/tasks")
    assert [t["id"] for t in listed] == [created["id"]]
    assert listed[0]["step_count"] == 2


def test_stop_running_task_cancels_cleanly(start_service):
    gate = threading.Event()
    base, fakes = start_service(
        lambda fake: snap("running", 1) if gate.wait(timeout=10) else snap("running", 1)
    )
    _, created = api(base, "POST", "/api/tasks", {"goal": "Slow", "url": "https://example.test/"})
    wait_for(base, created["id"], {"running"})
    _, stopped = api(base, "POST", f"/api/tasks/{created['id']}/stop", {})
    assert stopped["status"] == "running"
    gate.set()
    task = wait_for(base, created["id"], {"blocked"})
    assert task["error"] == "stopped by user"
    assert fakes[0].closed is True


def test_concurrency_limit_keeps_second_task_queued(start_service):
    gate = threading.Event()
    base, fakes = start_service(
        lambda fake: snap("done", 1) if gate.wait(timeout=10) else snap("done", 1),
        max_concurrent=1,
    )
    _, first = api(base, "POST", "/api/tasks", {"goal": "One", "url": "https://example.test/"})
    _, second = api(base, "POST", "/api/tasks", {"goal": "Two", "url": "https://example.test/"})
    wait_for(base, first["id"], {"running"})
    _, queued = api(base, "GET", f"/api/tasks/{second['id']}")
    assert queued["status"] == "queued"
    _, stopped = api(base, "POST", f"/api/tasks/{second['id']}/stop", {})
    assert stopped["status"] == "blocked"
    gate.set()
    assert wait_for(base, first["id"], {"done"})["status"] == "done"
    assert len(fakes) == 1


def test_cloud_failure_stops_the_browser(start_service, monkeypatch):
    monkeypatch.setenv("JEV_BROWSER", "local")

    def script(fake):
        if fake.ticks > 1:
            raise RuntimeError("boom")
        return snap("running", 1)

    base, fakes = start_service(script)
    _, created = api(base, "POST", "/api/tasks", {
        "goal": "Fail", "url": "https://example.test/", "browser": "cloud",
    })
    task = wait_for(base, created["id"], {"failed"})
    assert "boom" in task["error"]
    assert len(task["steps"]) == 1
    assert fakes[0].closed is True
    import os

    assert os.environ["JEV_BROWSER"] == "local"


def test_bearer_token_gates_the_api(start_service):
    base, _ = start_service(lambda fake: snap("done", 0), token="secret")
    assert api(base, "GET", "/api/tasks")[0] == 401
    assert api(base, "GET", "/api/tasks", token="wrong")[0] == 401
    assert api(base, "GET", "/api/tasks", token="secret")[0] == 200
    status, _ = api(base, "POST", "/api/tasks", {"goal": "x", "url": "https://example.test/"})
    assert status == 401
    status, body = api(base, "GET", "/api/health")
    assert (status, body) == (200, {"status": "ok"})


def read_sse_image(base, task_id, timeout=10):
    import http.client
    from urllib.parse import urlparse

    parts = urlparse(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    conn.request("GET", f"/api/tasks/{task_id}/live")
    response = conn.getresponse()
    assert response.status == 200
    image = None
    deadline = time.time() + timeout
    while time.time() < deadline and image is None:
        line = response.readline()
        if line.startswith(b"data:"):
            image = json.loads(line[len(b"data:"):]).get("image")
    conn.close()
    return image


def wait_for_call(browser, method, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if method in [name for name, _ in browser.calls]:
            return True
        time.sleep(0.05)
    return False


def test_live_screencast_streams_and_acks(start_service, monkeypatch):
    gate = threading.Event()
    frame = {
        "method": "Page.screencastFrame",
        "params": {"data": "frame-bytes", "sessionId": "frame-1"},
        "session_id": "sess-1",
    }
    acks = []

    def fake_cdp(method, session_id=None, **params):
        acks.append((method, session_id, params))
        return {}

    monkeypatch.setattr(hosted.harness_helpers, "drain_events", lambda: [frame])
    monkeypatch.setattr(hosted.harness_helpers, "cdp", fake_cdp)
    browser = FakeBrowser()
    base, _ = start_service(
        lambda fake: snap("done", 1) if gate.wait(timeout=10) else snap("done", 1),
        browser=browser,
    )
    _, created = api(base, "POST", "/api/tasks", {"goal": "Watch", "url": "https://example.test/"})
    wait_for(base, created["id"], {"running"})
    assert read_sse_image(base, created["id"]) == "frame-bytes"
    assert ("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": 1120,
                                     "everyNthFrame": 2}) in browser.calls
    assert ("Page.screencastFrameAck", "sess-1", {"sessionId": "frame-1"}) in acks
    assert wait_for_call(browser, "Page.stopScreencast")
    gate.set()
    assert wait_for(base, created["id"], {"done"})["status"] == "done"


def test_live_waits_for_browser_startup(monkeypatch):
    from http.server import ThreadingHTTPServer

    frame = {
        "method": "Page.screencastFrame",
        "params": {"data": "frame-bytes", "sessionId": "frame-1"},
        "session_id": "sess-1",
    }
    monkeypatch.setattr(hosted.harness_helpers, "drain_events", lambda: [frame])
    monkeypatch.setattr(hosted.harness_helpers, "cdp", lambda *a, **k: {})
    gate = threading.Event()
    run_gate = threading.Event()
    browser = FakeBrowser()

    def script(fake):
        if fake.ticks == 1:
            run_gate.wait(timeout=10)
            return snap("running", 0)
        return snap("done", 0)

    def factory(url, goal, browser=None, background=""):
        gate.wait(timeout=10)
        return FakeAgent(url, goal, script, browser=browser, background=background)

    # A service whose factory blocks until the viewer is already waiting.
    store = hosted.Store(":memory:")
    runner = hosted.SessionRunner(
        store, agent_factory=factory, browser_factory=lambda url: browser,
        files_root=pathlib.Path(tempfile.mkdtemp()),
    )
    handler = type("Handler", (hosted.Handler,), {})
    handler.STORE = store
    handler.RUNNER = runner
    handler.HOST = "127.0.0.1"
    handler.TOKEN = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.PORT = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{handler.PORT}"
        _, created = api(base, "POST", "/api/tasks", {"goal": "Slow start", "url": "https://x.test/"})
        seen = {}
        reader = threading.Thread(
            target=lambda: seen.setdefault("image", read_sse_image(base, created["id"])),
            daemon=True,
        )
        reader.start()
        time.sleep(0.5)
        gate.set()
        reader.join(timeout=10)
        assert seen.get("image") == "frame-bytes"
        run_gate.set()
        assert wait_for(base, created["id"], {"done"})["status"] == "done"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_live_falls_back_to_screenshots(start_service):
    gate = threading.Event()
    browser = FakeBrowser(fail_screencast=True)
    base, _ = start_service(
        lambda fake: snap("done", 1) if gate.wait(timeout=15) else snap("done", 1),
        browser=browser,
    )
    _, created = api(base, "POST", "/api/tasks", {"goal": "Watch", "url": "https://example.test/"})
    wait_for(base, created["id"], {"running"})
    assert read_sse_image(base, created["id"]) == "shot-bytes"
    assert "Page.captureScreenshot" in [name for name, _ in browser.calls]
    gate.set()
    assert wait_for(base, created["id"], {"done"})["status"] == "done"


def test_live_silent_screencast_flips_to_screenshots(monkeypatch):
    import types

    monkeypatch.setattr(hosted.Viewer, "QUIET_RESTART_S", 0.05)
    monkeypatch.setattr(hosted.Viewer, "SHOT_FALLBACK_S", 0.1)
    monkeypatch.setattr(hosted.Viewer, "SHOT_EVERY_S", 0.05)
    calls = []

    def fake_call(method, **params):
        calls.append(method)
        if method == "Page.captureScreenshot":
            return {"data": "shot-bytes"}
        return {}

    pump = types.SimpleNamespace(register=lambda viewer: None, unregister=lambda viewer: None)
    viewer = hosted.Viewer(pump, fake_call, "sess-1")
    viewer.add_viewer()
    try:
        deadline = time.time() + 5
        while viewer.mode != "screenshot" and time.time() < deadline:
            time.sleep(0.02)
        assert viewer.mode == "screenshot"
        deadline = time.time() + 5
        while viewer.latest is None and time.time() < deadline:
            time.sleep(0.02)
        assert viewer.latest == "shot-bytes"
        assert "Page.stopScreencast" in calls
    finally:
        viewer.remove_viewer()
        viewer.shutdown()


def test_live_unavailable_states(start_service):
    browser = FakeBrowser()
    base, _ = start_service(lambda fake: snap("done", 0), browser=browser)
    _, created = api(base, "POST", "/api/tasks", {"goal": "Quick", "url": "https://example.test/"})
    wait_for(base, created["id"], {"done"})
    import http.client
    from urllib.parse import urlparse

    def live_status(service_base, task_id):
        parts = urlparse(service_base)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        conn.request("GET", f"/api/tasks/{task_id}/live")
        status = conn.getresponse().status
        conn.close()
        return status

    assert live_status(base, "nope") == 404
    assert live_status(base, created["id"]) == 409
    gate = threading.Event()
    base2, _ = start_service(
        lambda fake: snap("done", 1) if gate.wait(timeout=10) else snap("done", 1),
        browser=FakeBrowser(),
    )
    _, cloud = api(base2, "POST", "/api/tasks", {
        "goal": "Cloud", "url": "https://example.test/", "browser": "cloud",
    })
    wait_for(base2, cloud["id"], {"running"})
    assert live_status(base2, cloud["id"]) == 409
    gate.set()
    assert wait_for(base2, cloud["id"], {"done"})["status"] == "done"


def test_live_url_is_transient_never_in_db(start_service, tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("JEV_CLOUD_SHOW_LIVE_URL", "1")
    gate = threading.Event()
    browser = FakeBrowser()
    browser.cloud = {"live_url": "https://example.invalid/live/1"}
    base, _ = start_service(
        lambda fake: snap("done", 1) if gate.wait(timeout=10) else snap("done", 1),
        browser=browser,
    )
    _, created = api(base, "POST", "/api/tasks", {
        "goal": "Cloud", "url": "https://example.test/", "browser": "cloud",
    })
    wait_for(base, created["id"], {"running"})
    _, running = api(base, "GET", f"/api/tasks/{created['id']}")
    assert running["live_url"] == "https://example.invalid/live/1"
    gate.set()
    done = wait_for(base, created["id"], {"done"})
    assert done["live_url"] is None
    conn = sqlite3.connect(tmp_path / "runs-0.db")
    assert conn.execute("SELECT live_url FROM tasks WHERE id = ?", (created["id"],)).fetchone() == (None,)
    conn.close()


def test_task_validation(start_service):
    base, _ = start_service(lambda fake: snap("done", 0))
    assert api(base, "POST", "/api/tasks", {"goal": "", "url": "https://example.test/"})[0] == 400
    status, created = api(base, "POST", "/api/tasks", {"goal": "x"})
    assert status == 201
    _, task = api(base, "GET", f"/api/tasks/{created['id']}")
    assert task["url"] == "about:blank"
    assert api(base, "POST", "/api/tasks", {"goal": "x", "url": "gopher://x/"})[0] == 400
    assert api(base, "POST", "/api/tasks", {
        "goal": "x", "url": "https://example.test/", "browser": "mars",
    })[0] == 400
    assert api(base, "POST", "/api/tasks", {
        "goal": "x", "url": "https://example.test/", "max_decisions": 0,
    })[0] == 400
    assert api(base, "GET", "/api/tasks/nope")[0] == 404


def test_finish_stores_bench_evidence(start_service):
    def script(fake):
        finished = snap("done", 2)
        finished["page"] = {"url": "https://example.test/", "text": "Hello bench"}
        finished["answer"] = "The answer is 42."
        finished["blocked_reason"] = None
        finished["partial"] = True
        return finished

    base, _ = start_service(script)
    _, created = api(base, "POST", "/api/tasks", {"goal": "Bench", "url": "https://example.test/"})
    done = wait_for(base, created["id"], {"done"})
    assert done["final_text"] == "Hello bench"
    assert done["answer_text"] == "The answer is 42."
    assert done["decisions_count"] == 2
    assert done["blocked_reason"] is None
    assert done["partial"] == 1
    _, listed = api(base, "GET", "/api/tasks")
    assert "final_text" not in listed[0]


def test_finish_prefers_full_document_text(start_service):
    class FullBrowser(FakeBrowser):
        def __init__(self, text):
            super().__init__()
            self.text = text

        def evaluate(self, expression):
            assert "innerText" in expression
            if isinstance(self.text, Exception):
                raise self.text
            return self.text

    def script(fake):
        finished = snap("done", 1)
        finished["page"] = {"url": "https://example.test/", "text": "Viewport only"}
        return finished

    base, _ = start_service(script, browser=FullBrowser("Whole document here"))
    _, created = api(base, "POST", "/api/tasks", {"goal": "Bench", "url": "https://example.test/"})
    done = wait_for(base, created["id"], {"done"})
    assert done["final_text"] == "Whole document here"

    base, _ = start_service(script, browser=FullBrowser(RuntimeError("dead")))
    _, created = api(base, "POST", "/api/tasks", {"goal": "Bench", "url": "https://example.test/"})
    done = wait_for(base, created["id"], {"done"})
    assert done["final_text"] == "Viewport only"


def test_store_migrates_old_schema(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT NOT NULL)")
    conn.execute("INSERT INTO tasks (id, goal) VALUES ('t1', 'Old')")
    conn.commit()
    conn.close()
    store = hosted.Store(db)
    row = store.get_task("t1")
    assert row["goal"] == "Old"
    assert row["answer_text"] is None
    assert row["decisions_count"] is None


def wait_session(base, session_id, statuses=("done", "blocked", "failed"), timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, session = api(base, "GET", f"/api/sessions/{session_id}")
        messages = session["messages"]
        if messages and messages[-1]["status"] in statuses:
            return session
        time.sleep(0.05)
    raise AssertionError("session message did not finish")


def test_sessions_http_lifecycle(start_service):
    base, _ = start_service(lambda fake: snap("done", 1))
    status, created = api(base, "POST", "/api/sessions", {"browser": "local"})
    assert status == 201
    _, listed = api(base, "GET", "/api/sessions")
    assert [session["id"] for session in listed] == [created["id"]]
    status, posted = api(base, "POST", f"/api/sessions/{created['id']}/messages", {"text": "hi"})
    assert status == 201
    session = wait_session(base, created["id"])
    assert session["messages"][0]["agent_reply"]
    assert session["messages"][0]["id"] == posted["id"]
    status, _ = api(base, "DELETE", f"/api/sessions/{created['id']}")
    assert status == 200
    assert api(base, "GET", f"/api/sessions/{created['id']}")[0] == 404


def test_sessions_http_auth(start_service):
    base, _ = start_service(lambda fake: snap("done", 0), token="secret")
    assert api(base, "GET", "/api/sessions")[0] == 401
    assert api(base, "POST", "/api/sessions", {"browser": "local"})[0] == 401
    status, created = api(base, "POST", "/api/sessions", {"browser": "local"}, token="secret")
    assert status == 201
    _, session = api(base, "GET", f"/api/sessions/{created['id']}", token="secret")
    assert session["id"] == created["id"]


def test_session_files_roundtrip(start_service):
    import base64

    base, _ = start_service(lambda fake: snap("done", 0))
    _, created = api(base, "POST", "/api/sessions", {"browser": "local"})
    sid = created["id"]
    _, listed = api(base, "GET", f"/api/sessions/{sid}/files")
    assert listed == {"files": []}
    body = {"name": "results.csv", "content_b64": base64.b64encode(b"a,b\n1,2\n").decode()}
    status, stored = api(base, "POST", f"/api/sessions/{sid}/files", body)
    assert status == 201
    assert stored == {"name": "results.csv", "bytes": 8}
    _, listed = api(base, "GET", f"/api/sessions/{sid}/files")
    assert [row["name"] for row in listed["files"]] == ["results.csv"]
    assert listed["files"][0]["bytes"] == 8
    request = urllib.request.Request(base + f"/api/sessions/{sid}/files/results.csv", method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        assert response.read() == b"a,b\n1,2\n"
        assert "attachment" in response.headers.get("Content-Disposition", "")
    _, session = api(base, "GET", f"/api/sessions/{sid}")
    assert [row["name"] for row in session["files"]] == ["results.csv"]


def test_session_files_auth_and_validation(start_service):
    base, _ = start_service(lambda fake: snap("done", 0), token="secret")
    _, created = api(base, "POST", "/api/sessions", {"browser": "local"}, token="secret")
    sid = created["id"]
    assert api(base, "GET", f"/api/sessions/{sid}/files")[0] == 401
    assert api(base, "POST", f"/api/sessions/{sid}/files", {"name": "a.txt"})[0] == 401
    good = {"name": "a.txt", "content_b64": "aGk="}
    assert api(base, "POST", "/api/sessions/nope/files", good, token="secret")[0] == 404
    assert api(base, "GET", "/api/sessions/nope/files", token="secret")[0] == 404
    assert api(base, "POST", f"/api/sessions/{sid}/files", {**good, "name": "../x"}, token="secret")[0] == 400
    assert api(base, "POST", f"/api/sessions/{sid}/files", {**good, "content_b64": "!!"}, token="secret")[0] == 400
    assert api(base, "GET", f"/api/sessions/{sid}/files/../x", token="secret")[0] == 404


def test_share_replay_is_public_but_token_guarded(start_service):
    base, _ = start_service(lambda fake: snap("done", 1))
    _, created = api(base, "POST", "/api/sessions", {"browser": "local"})
    sid = created["id"]
    _, posted = api(base, "POST", f"/api/sessions/{sid}/messages", {"text": "hi"})
    assert posted["id"]
    wait_session(base, sid)
    assert api(base, "POST", "/api/sessions/nope/share", {})[0] == 404
    status, shared = api(base, "POST", f"/api/sessions/{sid}/share", {})
    assert status == 200
    assert shared["url"] == f"/share/{shared['token']}"
    status, again = api(base, "POST", f"/api/sessions/{sid}/share", {})
    assert again["token"] == shared["token"]
    request = urllib.request.Request(base + f"/api/share/{shared['token']}", method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        replay = json.loads(response.read())
    assert replay["title"] == "hi"
    assert len(replay["turns"]) == 1
    assert replay["turns"][0]["steps"][0]["operation"] == "CLICK"
    assert replay["turns"][0]["frame"] is None
    assert "live_url" not in replay and "share_token" not in json.dumps(replay)
    assert api(base, "GET", "/api/share/nope")[0] == 404


def test_config_roundtrip_and_token_rotation(start_service, monkeypatch):
    monkeypatch.delenv("TEXT_MODEL", raising=False)
    monkeypatch.delenv("DECISION_PROVIDER", raising=False)
    base, _ = start_service(lambda fake: snap("done", 0))
    _, config = api(base, "GET", "/api/config")
    assert config["browser_default"] == "local"
    assert config["max_decisions_default"] is None
    assert config["server_token_set"] is False
    status, updated = api(base, "POST", "/api/config", {
        "browser_default": "cloud",
        "max_decisions_default": 25,
        "text_model": "mercury-2.5",
        "decision_provider": "mercury",
    })
    assert status == 200
    assert updated["browser_default"] == "cloud"
    assert updated["max_decisions_default"] == 25
    assert api(base, "POST", "/api/config", {"max_decisions_default": 0})[0] == 400
    _, created = api(base, "POST", "/api/sessions", {})
    _, session = api(base, "GET", f"/api/sessions/{created['id']}")
    assert session["browser"] == "cloud"
    status, rotated = api(base, "POST", "/api/config", {"server_token": "rotated"})
    assert status == 200 and rotated["server_token_set"] is True
    assert "rotated" not in json.dumps(rotated)
    assert api(base, "GET", "/api/sessions")[0] == 401
    assert api(base, "GET", "/api/sessions", token="rotated")[0] == 200


def test_takeover_modes(start_service):
    import threading

    gate = threading.Event()
    browser = FakeBrowser()
    base, _ = start_service(
        lambda fake: snap("done", 1) if gate.wait(timeout=10) else snap("done", 1),
        browser=browser,
    )
    _, created = api(base, "POST", "/api/sessions", {"browser": "local"})
    assert api(base, "POST", f"/api/sessions/{created['id']}/takeover", {})[0] == 409
    assert api(base, "POST", "/api/sessions/nope/takeover", {})[0] == 404
    api(base, "POST", f"/api/sessions/{created['id']}/messages", {"text": "hi"})
    wait_session(base, created["id"], {"running"})
    status, taken = api(base, "POST", f"/api/sessions/{created['id']}/takeover", {})
    assert status == 200 and taken == {"mode": "local", "ok": True}
    assert "Page.bringToFront" in [name for name, _ in browser.calls]
    gate.set()
    wait_session(base, created["id"])
    _, cloud = api(base, "POST", "/api/sessions", {"browser": "cloud"})
    status, taken = api(base, "POST", f"/api/sessions/{cloud['id']}/takeover", {})
    assert status == 200 and taken["mode"] == "cloud"


def test_new_app_routes(start_service):
    base, _ = start_service(lambda fake: snap("done", 0))
    for path in ("/", "/session/abc", "/share/abc", "/sessions.js", "/sessions.css"):
        request = urllib.request.Request(base + path, method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
    request = urllib.request.Request(base + "/tasks.html", method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
