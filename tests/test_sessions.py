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

    def __init__(self, url, goal, browser=None, script=None, background=""):
        self.url = url
        self.goal = goal
        self.browser = browser
        self.background = background
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


def test_followup_keeps_clean_goal_with_background(tmp_path):
    FakeAgent.seen.clear()
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    _inject(runner, sid, FakeBrowser("https://x.com"))
    first = runner.submit_message(sid, "open x.com")
    _wait_done(runner, sid, first)
    second = runner.submit_message(sid, "now click the second result")
    _wait_done(runner, sid, second)
    assert FakeAgent.seen[0].goal == "open x.com"
    assert FakeAgent.seen[0].background == ""
    # The goal stays the bare message: mechanical consumers (search fallback,
    # sub-goal splitting) must never see the context blob.
    assert FakeAgent.seen[1].goal == "now click the second result"
    background = FakeAgent.seen[1].background
    assert "open x.com" in background and "Current page: https://x.com" in background


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
    assert hosted.compose_reply(
        {"answer": "Opened x.com. It shows a login wall.", "visited_urls": ["x.com/"]}, "done", None
    ) == ("Opened x.com. It shows a login wall.")
    assert hosted.compose_reply({"answer": '{"a": 1}'}, "done", None) == '{"a": 1}.'
    reply = hosted.compose_reply({"blocked_reason": "gave up", "partial": 1, "answer": "X. Y."}, "blocked", None)
    assert reply.startswith("I couldn't fully finish (gave up). So far: X.")
    assert "{" not in hosted.compose_reply({}, "blocked", "stopped by user")
    reply = hosted.compose_reply(
        {"blocked_reason": "gave up; hit CAPTCHA walls on wall.test"}, "blocked", None
    )
    assert reply == "I couldn't finish (gave up; hit CAPTCHA walls on wall.test)."


def test_compose_reply_rejects_unvisited_site_claim():
    snapshot = {
        "answer": "I opened a new tab and navigated to the AccuWeather page for Bangkok weather.",
        "page": {"url": "https://www.google.com/search?q=Bangkok+weather", "title": "Bangkok weather - Google"},
        "visited_urls": ["google.com/search?q=Bangkok+weather"],
        "memo": [],
        "history": [],
    }
    reply = hosted.compose_reply(snapshot, "done", None)
    assert "accuweather" not in reply.lower()
    assert "google.com/search" in reply


def test_compose_reply_rejects_unvisited_host():
    snapshot = {
        "answer": "Opened example.com. It shows the docs.",
        "page": {"url": "https://www.google.com/", "title": "Google"},
        "visited_urls": ["google.com/"],
        "memo": [{"fact": "8.5 million residents (2021 estimate)", "source": "https://en.wikipedia.org/wiki/X"}],
        "history": [],
    }
    reply = hosted.compose_reply(snapshot, "done", None)
    assert "example.com" not in reply
    assert "Google" in reply and "8.5 million residents" in reply


def test_compose_reply_keeps_supported_page_name():
    snapshot = {
        "answer": "Opened the Wikipedia page. Population is 8.5 million.",
        "page": {"url": "https://en.wikipedia.org/wiki/Bangkok", "title": "Bangkok - Wikipedia"},
        "visited_urls": ["en.wikipedia.org/wiki/Bangkok"],
        "memo": [],
        "history": [],
    }
    assert hosted.compose_reply(snapshot, "done", None) == (
        "Opened the Wikipedia page. Population is 8.5 million."
    )


def test_tabs_event_published_on_tab_change(tmp_path):
    browser = FakeBrowser("https://example.test/a")
    ticks = []

    def script():
        ticks.append(1)
        if len(ticks) == 1:
            browser.tabs["t2"] = {"target": "bbb", "url": "https://example.test/b"}
            browser.active = "t2"
            return {"status": "running", "history": [], "page": {"url": browser.tabs["t2"]["url"]}}
        return {"status": "done", "history": [], "answer": "Two tabs."}

    def factory(url, goal, browser=None, background=""):
        return FakeAgent(url, goal, browser=browser, script=script, background=background)

    runner = _runner(tmp_path, agent_factory=factory)
    sid = runner.create_session("local")
    _inject(runner, sid, browser)
    mid = runner.submit_message(sid, "open a second tab")
    assert _wait_done(runner, sid, mid)["status"] == "done"
    tabs_events = [event for _, event in runner.replay(sid) if event["type"] == "tabs"]
    assert len(tabs_events) == 1
    assert [(tab["key"], tab["active"]) for tab in tabs_events[0]["tabs"]] == [
        ("t1", False), ("t2", True),
    ]


def test_no_tabs_event_without_change(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    _inject(runner, sid, FakeBrowser())
    mid = runner.submit_message(sid, "hi")
    _wait_done(runner, sid, mid)
    kinds = [event["type"] for _, event in runner.replay(sid)]
    assert "tabs" not in kinds


def test_session_tabs_reads_cached_titles():
    browser = FakeBrowser("https://example.test/a")
    browser.tabs["t1"]["title"] = "Page A"
    assert hosted.session_tabs(browser) == [
        {"key": "t1", "url": "https://example.test/a", "title": "Page A", "active": True},
    ]
    assert hosted.tabs_signature(browser) == (("t1", "https://example.test/a", True),)


def test_viewer_follows_active_tab(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    browser = FakeBrowser()
    browser.session = "s1"
    _inject(runner, sid, browser)
    first, status = runner.get_viewer(sid)
    assert status == 200 and first.session == "s1"
    browser.session = "s2"
    second, status = runner.get_viewer(sid)
    assert status == 200 and second.session == "s2"
    assert second is not first


def test_files_event_published_when_agent_writes(tmp_path):
    runner = _runner(tmp_path, agent_factory=None)
    sid = runner.create_session("local")
    files_dir = runner.session_files_dir(sid)
    ticks = []

    def script():
        ticks.append(1)
        if len(ticks) == 1:
            (files_dir / "results.csv").write_text("a,b\n")
            return {"status": "running", "history": [], "page": {"url": "about:blank"}}
        return {"status": "done", "history": [], "answer": "Saved."}

    def factory(url, goal, browser=None, background=""):
        return FakeAgent(url, goal, browser=browser, script=script, background=background)

    runner.agent_factory = factory
    _inject(runner, sid, FakeBrowser())
    mid = runner.submit_message(sid, "save it")
    assert _wait_done(runner, sid, mid)["status"] == "done"
    files_events = [event for _, event in runner.replay(sid) if event["type"] == "files"]
    assert len(files_events) == 1
    assert files_events[0]["files"] == [
        {"name": "results.csv", "bytes": 4, "mtime": files_events[0]["files"][0]["mtime"]},
    ]


def test_no_files_event_without_change(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    _inject(runner, sid, FakeBrowser())
    mid = runner.submit_message(sid, "hi")
    _wait_done(runner, sid, mid)
    kinds = [event["type"] for _, event in runner.replay(sid)]
    assert "files" not in kinds


def test_session_files_join_upload_allowlist(tmp_path):
    FakeAgent.seen.clear()
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    files_dir = runner.session_files_dir(sid)
    (files_dir / "note.txt").write_text("hello")
    _inject(runner, sid, FakeBrowser())
    mid = runner.submit_message(sid, "hi")
    _wait_done(runner, sid, mid)
    assert FakeAgent.seen[-1].config["files"] == {"note.txt": str(files_dir / "note.txt")}


def test_ensure_browser_routes_downloads(tmp_path):
    class DownloadBrowser(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.config = {}
            self.download_path = None

        def configure_downloads(self, path):
            self.download_path = path

    made = []
    runner = _runner(
        tmp_path, agent_factory=FakeAgent,
        browser_factory=lambda url: made.append(DownloadBrowser()) or made[-1],
    )
    sid = runner.create_session("local")
    mid = runner.submit_message(sid, "hi")
    _wait_done(runner, sid, mid)
    assert made[0].download_path == runner.session_files_dir(sid)
    assert made[0].config["files_dir"] == str(runner.session_files_dir(sid))


class WallAgent(FakeAgent):
    def __init__(self, url, goal, browser=None, background=""):
        super().__init__(url, goal, browser=browser, background=background)
        self.state = {
            "history": [], "browser": browser,
            "page": {"url": "https://wall.test/sorry"},
        }
        self.ticks = 0

    def command(self, name, body=None):
        assert name == "tick"
        self.ticks += 1
        if self.ticks == 1:
            self.state["cloud_restart_wanted"] = "wall.test"
        self.state["history"].append({
            "step": len(self.state["history"]) + 1,
            "action": "Blocked by a CAPTCHA on wall.test",
            "kind": "captcha",
            "operation": "CAPTCHA",
            "url": "https://wall.test/sorry",
            "elapsed_ms": 5,
            "note": "captcha path",
        })
        if self.ticks == 1:
            return {
                "status": "running", "history": self.state["history"],
                "page": {"url": "https://wall.test/sorry"},
            }
        return {
            "status": "done", "history": self.state["history"],
            "answer": "Got through.", "page": {"url": "https://example.test/"},
        }


def test_cloud_restart_swaps_browser_once(tmp_path, monkeypatch):
    monkeypatch.delenv("JEV_CLOUD_FALLBACK_COUNTRY", raising=False)
    monkeypatch.delenv("JEV_CLOUD_PROXY_COUNTRY", raising=False)
    made = []
    seen_agents = []

    def agent_factory(url, goal, browser=None, background=""):
        agent = WallAgent(url, goal, browser=browser, background=background)
        seen_agents.append(agent)
        return agent

    runner = _runner(
        tmp_path, agent_factory=agent_factory,
        browser_factory=lambda url: made.append(FakeBrowser(cloud=True)) or made[-1],
    )
    sid = runner.create_session("cloud")
    mid = runner.submit_message(sid, "search the walled site")
    assert _wait_done(runner, sid, mid)["status"] == "done"
    assert len(made) == 2
    assert made[0].closed is True and made[0].cloud_stopped is True
    assert runner._sessions[sid]["browser"] is made[1]
    agent = seen_agents[-1]
    assert agent.browser is made[1] and agent.state["browser"] is made[1]
    assert agent.state["cloud_restarted"] is True
    assert agent.state["cloud_restart_wanted"] is None
    operations = [step["operation"] for step in runner.task_view(mid)["steps"]]
    assert operations == ["CAPTCHA", "RESTART", "CAPTCHA"]
    restarts = [event for _, event in runner.replay(sid) if event["type"] == "restart"]
    assert len(restarts) == 1
    assert restarts[0]["host"] == "wall.test" and restarts[0]["country"] is None


def test_cloud_restart_uses_fallback_country(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_CLOUD_FALLBACK_COUNTRY", "de")
    monkeypatch.delenv("JEV_CLOUD_PROXY_COUNTRY", raising=False)
    countries = []

    def factory(url):
        import os

        countries.append(os.environ.get("JEV_CLOUD_PROXY_COUNTRY"))
        return FakeBrowser(cloud=True)

    runner = _runner(tmp_path, agent_factory=WallAgent, browser_factory=factory)
    sid = runner.create_session("cloud")
    mid = runner.submit_message(sid, "search the walled site")
    _wait_done(runner, sid, mid)
    assert countries == [None, "de"]
    import os

    assert os.environ.get("JEV_CLOUD_PROXY_COUNTRY") is None
    restarts = [event for _, event in runner.replay(sid) if event["type"] == "restart"]
    assert restarts[0]["country"] == "de"


def test_second_wall_does_not_restart_again(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("cloud")
    browser = FakeBrowser(cloud=True)
    _inject(runner, sid, browser)
    entry = runner._sessions[sid]
    agent = FakeAgent("about:blank", "hi", browser=browser)
    agent.state = {"history": [], "browser": browser}
    agent.state["cloud_restart_wanted"] = "wall.test"
    agent.state["cloud_restarted"] = True
    assert runner._maybe_restart_cloud(sid, "m1", entry, agent) is False
    assert runner._sessions[sid]["browser"] is browser
    assert browser.closed is False


def test_local_wall_never_restarts(tmp_path):
    made = []
    seen_agents = []

    def agent_factory(url, goal, browser=None, background=""):
        agent = WallAgent(url, goal, browser=browser, background=background)
        seen_agents.append(agent)
        return agent

    runner = _runner(
        tmp_path, agent_factory=agent_factory,
        browser_factory=lambda url: made.append(FakeBrowser()) or made[-1],
    )
    sid = runner.create_session("local")
    mid = runner.submit_message(sid, "search the walled site")
    assert _wait_done(runner, sid, mid)["status"] == "done"
    assert len(made) == 1
    assert seen_agents[-1].state["cloud_restart_wanted"] is None
    kinds = [event["type"] for _, event in runner.replay(sid)]
    assert "restart" not in kinds


def test_final_frame_captured_for_replay(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    browser = FakeBrowser()
    _inject(runner, sid, browser)

    class Watcher:
        latest = "aGk="

    runner._sessions[sid]["viewer"] = Watcher()
    mid = runner.submit_message(sid, "hi")
    _wait_done(runner, sid, mid)
    assert runner.final_frame(sid, mid) == "aGk="
    assert (tmp_path / "files" / sid / "frames" / f"{mid}.jpg").read_bytes() == b"hi"
    assert runner.final_frame(sid, "nope") is None


def test_config_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXT_MODEL", raising=False)
    monkeypatch.delenv("DECISION_PROVIDER", raising=False)
    path = tmp_path / "server.json"
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    assert runner.load_config(path) == {}
    runner.apply_config({"browser_default": "cloud", "max_decisions_default": 30})
    runner.save_config({"browser_default": "cloud", "max_decisions_default": 30})
    fresh = _runner(tmp_path, agent_factory=FakeAgent)
    fresh.load_config(path)
    assert fresh.default_browser == "cloud"
    assert fresh.default_max_decisions == 30
    assert fresh.config_view()["browser_default"] == "cloud"


def test_default_max_decisions_reaches_agent(tmp_path):
    FakeAgent.seen.clear()
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    runner.apply_config({"max_decisions_default": 12})
    sid = runner.create_session("local")
    _inject(runner, sid, FakeBrowser())
    mid = runner.submit_message(sid, "hi")
    _wait_done(runner, sid, mid)
    assert FakeAgent.seen[-1].config["max_decisions"] == 12


def test_fresh_browser_reopens_last_url(tmp_path):
    navigated = []

    class NavBrowser(FakeBrowser):
        def navigate(self, url):
            navigated.append(url)
            return {"navigated": True}

    runner = _runner(
        tmp_path, agent_factory=FakeAgent, browser_factory=lambda url: NavBrowser(),
    )
    sid = runner.create_session("local")
    runner.store.update_session(sid, last_url="https://example.test/there")
    mid = runner.submit_message(sid, "continue")
    _wait_done(runner, sid, mid)
    assert navigated == ["https://example.test/there"]


def test_explicit_url_wins_over_last_url(tmp_path):
    navigated = []

    class NavBrowser(FakeBrowser):
        def navigate(self, url):
            navigated.append(url)
            return {"navigated": True}

    runner = _runner(
        tmp_path, agent_factory=FakeAgent, browser_factory=lambda url: NavBrowser(),
    )
    sid = runner.create_session("local")
    runner.store.update_session(sid, last_url="https://example.test/there")
    mid = runner.submit_message(sid, "go", url="https://example.test/explicit")
    _wait_done(runner, sid, mid)
    assert navigated == ["https://example.test/explicit"]


def test_blank_finish_keeps_last_url(tmp_path):
    runner = _runner(tmp_path, agent_factory=FakeAgent)
    sid = runner.create_session("local")
    runner.store.update_session(sid, last_url="https://example.test/there")
    _inject(runner, sid, FakeBrowser("about:blank"))
    mid = runner.submit_message(sid, "hi")
    _wait_done(runner, sid, mid)
    session = runner.store.get_session(sid, with_steps=False)
    assert session["last_url"] == "https://example.test/there"


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
