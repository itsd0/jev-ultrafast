"""Session store and runner contracts. No browsers, no paid APIs."""

import time

from jev_ultrafast import server as hosted


class FakeBrowser:
    def __init__(self, url="about:blank", cloud=False):
        self.tabs = {"t1": {"target": "aaa", "url": url}}
        self.active = "t1"
        self.session = None
        self.closed = False
        self.cloud_stopped = False
        self.cloud = {"browser_id": "c1"} if cloud else None

    def call(self, method, **params):
        return {}

    def close(self):
        self.closed = True

    def stop_cloud(self):
        self.cloud_stopped = True
        self.cloud = None
        return True


class FakeAgent:
    seen = []

    def __init__(self, url, goal, browser=None, script=None):
        self.url = url
        self.goal = goal
        self.browser = browser
        self.config = {}
        self.trace = None
        self.closed_keep = None
        self.script = script or (lambda: {"status": "done", "history": [], "answer": "Ok."})
        FakeAgent.seen.append(self)

    def command(self, name, body=None):
        assert name == "tick"
        return self.script()

    def snapshot(self):
        return self.script()

    def close(self, keep_browser=False):
        self.closed_keep = keep_browser


def test_session_lifecycle(tmp_path):
    store = hosted.Store(tmp_path / "runs.db")
    sid = store.create_session("open x.com", "local")
    session = store.get_session(sid)
    assert session["title"] == "open x.com"
    assert session["status"] == "active"
    assert session["messages"] == []
    mid = store.create_message(sid, "open x.com")
    store.add_step(mid + "-run", 1, "NAVIGATE", "x.com", "https://x.com", 12, "")
    store.update_message(mid, run_id=mid + "-run", status="done", agent_reply="Opened it.")
    session = store.get_session(sid)
    assert [m["n"] for m in session["messages"]] == [1]
    assert session["messages"][0]["steps"][0]["operation"] == "NAVIGATE"
    assert store.list_sessions()[0]["message_count"] == 1
    store.delete_session(sid)
    assert store.get_session(sid) is None
    assert store.list_sessions() == []


def test_old_db_gains_session_tables(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT)")
    conn.commit()
    conn.close()
    store = hosted.Store(path)
    assert store.create_session("t", "local")
    assert store.usage_by_day() == []


def test_usage_rolls_up_messages(tmp_path):
    store = hosted.Store(tmp_path / "runs.db")
    sid = store.create_session("t", "cloud")
    mid = store.create_message(sid, "hi")
    store.add_step(mid, 1, "CLICK", "x", "https://x.com", 5, "")
    store.update_message(
        mid, run_id=mid, status="done", input_tokens=10, output_tokens=4,
        cost_usd_estimate=0.001,
    )
    rows = store.usage_by_day()
    assert len(rows) == 1
    assert rows[0]["sessions"] == 1 and rows[0]["turns"] == 1
    assert rows[0]["tokens"] == 14 and rows[0]["steps"] == 1
    assert rows[0]["cost"] == 0.001


def _runner(tmp_path, **kwargs):
    store = hosted.Store(tmp_path / "runs.db")
    return hosted.SessionRunner(store, files_root=tmp_path / "files", **kwargs)


def _inject(runner, session_id, browser):
    runner._sessions[session_id]["browser"] = browser


def _wait_done(runner, session_id, message_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = runner.store.get_message(message_id)
        if message["status"] in {"done", "blocked", "failed"}:
            return message
        time.sleep(0.02)
    raise AssertionError("message did not finish")


def test_message_reuses_browser_across_turns(tmp_path):
    FakeAgent.seen.clear()
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    browser = FakeBrowser("https://x.com")
    _inject(runner, sid, browser)
    first = runner.submit_message(sid, "open x.com")
    assert _wait_done(runner, sid, first)["status"] == "done"
    second = runner.submit_message(sid, "now search x.com on google")
    assert _wait_done(runner, sid, second)["status"] == "done"
    assert [agent.browser for agent in FakeAgent.seen] == [browser, browser]
    assert all(agent.closed_keep is True for agent in FakeAgent.seen)
    assert browser.closed is False
    session = runner.store.get_session(sid)
    assert session["title"] == "open x.com"
    assert [m["n"] for m in session["messages"]] == [1, 2]


def test_followup_goal_includes_session_context(tmp_path):
    FakeAgent.seen.clear()
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    _inject(runner, sid, FakeBrowser("https://x.com"))
    first = runner.submit_message(sid, "open x.com")
    _wait_done(runner, sid, first)
    second = runner.submit_message(sid, "now click the second result")
    _wait_done(runner, sid, second)
    assert FakeAgent.seen[0].goal == "open x.com"
    followup = FakeAgent.seen[1].goal
    assert "open x.com" in followup and "Current page: https://x.com" in followup
    assert "New request: now click the second result" in followup


def test_idle_timeout_closes_cloud_browser_and_bills_nothing_more(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent, idle_min=0.05)
    sid = runner.create_session("cloud")
    browser = FakeBrowser(cloud=True)
    _inject(runner, sid, browser)
    runner._sessions[sid]["last_active"] = time.time() - 10
    runner.reap_idle()
    assert browser.closed is True
    assert runner._sessions[sid]["browser"] is None
    assert runner.store.get_session(sid, with_steps=False)["status"] == "active"


def test_close_session_stops_and_marks_closed(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    browser = FakeBrowser()
    _inject(runner, sid, browser)
    assert runner.close_session(sid) is True
    assert browser.closed is True
    assert runner.store.get_session(sid, with_steps=False)["status"] == "closed"
    runner.delete_session(sid)
    assert runner.store.get_session(sid) is None


def test_compose_reply_is_plain_sentences():
    assert hosted.compose_reply({"answer": "Opened x.com. It shows a login wall."}, "done", None) == (
        "Opened x.com. It shows a login wall."
    )
    assert hosted.compose_reply({"answer": '{"a": 1}'}, "done", None) == '{"a": 1}.'
    reply = hosted.compose_reply({"blocked_reason": "gave up", "partial": 1, "answer": "X. Y."}, "blocked", None)
    assert reply.startswith("I couldn't fully finish (gave up). So far: X.")
    assert "{" not in hosted.compose_reply({}, "blocked", "stopped by user")


def test_bus_replays_events(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    _inject(runner, sid, FakeBrowser())
    mid = runner.submit_message(sid, "hi")
    _wait_done(runner, sid, mid)
    events = [event for _, event in runner.replay(sid)]
    kinds = [event["type"] for event in events]
    assert kinds[0] == "message" and kinds[-1] == "reply"
    assert runner.replay(sid, since=10**9) == []
    assert runner.replay("nope") == []
