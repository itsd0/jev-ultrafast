"""Offline contracts for the hosted task layer. No browsers, no paid APIs."""

import json
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
    def __init__(self, url, goal, script, screenshots=False):
        self.url = url
        self.goal = goal
        self.script = script
        self.config = {}
        self.browser = None
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

    def close(self):
        self.closed = True


@pytest.fixture
def start_service(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_COST_PER_1K_TOKENS", raising=False)
    monkeypatch.delenv("JEV_SERVER_TOKEN", raising=False)
    services = []

    def start(script, max_concurrent=2, token="", browser=None):
        fakes = []

        def factory(url, goal, screenshots=False):
            fake = FakeAgent(url, goal, script, screenshots=screenshots)
            fake.browser = browser() if callable(browser) else browser
            fakes.append(fake)
            return fake

        store = hosted.Store(tmp_path / f"runs-{len(services)}.db")
        runner = hosted.Runner(store, max_concurrent=max_concurrent, agent_factory=factory)
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

    def factory(url, goal, screenshots=False):
        gate.wait(timeout=10)
        fake = FakeAgent(url, goal, script, screenshots=screenshots)
        fake.browser = browser
        return fake

    # A service whose factory blocks until the viewer is already waiting.
    store = hosted.Store(":memory:")
    runner = hosted.Runner(store, agent_factory=factory)
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
    assert api(base, "POST", "/api/tasks", {"goal": "x"})[0] == 400
    assert api(base, "POST", "/api/tasks", {
        "goal": "x", "url": "https://example.test/", "browser": "mars",
    })[0] == 400
    assert api(base, "POST", "/api/tasks", {
        "goal": "x", "url": "https://example.test/", "max_decisions": 0,
    })[0] == 400
    assert api(base, "GET", "/api/tasks/nope")[0] == 404
