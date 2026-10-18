"""Beam agent loop: multi-action stopping, guards, done gate. Offline fakes only."""

import pytest

from jev_ultrafast.beam_agent import BeamAgent
from jev_ultrafast.llm.base import LLMCallResult, ProviderError


def _action(id, kind, label, **extra):
    base = {
        "id": id, "kind": kind, "label": label, "role": "button" if kind == "click" else "textbox",
        "tag": "button" if kind == "click" else "input", "depth": 4, "node": 1, "frame": "f0",
        "rect": {"x": 10, "y": 10, "w": 100, "h": 20},
    }
    base.update(extra)
    return base


def _page(actions, url="https://example.test/", text="hello world"):
    return {
        "url": url, "title": "Example", "w": 1120, "h": 780, "text": text,
        "actions": actions, "scroll": {"y": 0, "height": 780}, "sections": [],
    }


def _step(*actions):
    return {
        "thinking": "t", "evaluation_previous_goal": "First step. Verdict: Success",
        "memory": "m", "next_goal": "g", "plan_update": "",
        "actions": [
            {
                "name": name, "index": -1, "text": "", "url": "", "new_tab": False,
                "tab": "", "engine": "", "clear": False, "keys": [], "option": "",
                "direction": "down", "amount": -1, "pattern": "", "key": "",
                "filename": "", "content": "", "success": False, **overrides,
            }
            for name, overrides in actions
        ],
    }


class FakeBrowser:
    """Scriptable stand-in. Every mutation is recorded, none retried."""

    def __init__(self, page, href=None, options=(), files_dir=None):
        self.page = dict(page)
        self.href = href if href is not None else page.get("url")
        self.tabs = {"t1": {"url": self.href, "title": page.get("title", "")}}
        self.active = "t1"
        self.session = "s1"
        self.config = {"files_dir": files_dir} if files_dir else {}
        self.frame_sessions = {}
        self.options = list(options)
        self.opened = []
        self.calls = []
        self.fail_back = False

    def observe(self, screenshot=False):
        self.page = dict(self.page, url=self.href)
        self.calls.append(("observe", self.href))
        return dict(self.page)

    def act(self, action, page, text=None, **extra):
        self.calls.append(("act", action.get("id"), action.get("kind")))
        if action.get("kind") == "fill":
            # Mirror the real contract: the typed value arrives as text=,
            # and the executor fails on anything but a string.
            assert isinstance(text, str), f"fill got text={text!r}"
            self.calls.append(("fill_text", text))
        return {"executed": action.get("id"), "text_verified": True}

    def navigate(self, url):
        self.calls.append(("navigate", url))
        self.href = url
        self.tabs[self.active]["url"] = url

    def new_tab(self):
        key = f"t{len(self.tabs) + 1}"
        self.tabs[key] = {"url": "about:blank", "title": ""}
        self.active = key
        self.calls.append(("new_tab", key))
        return key

    def go_back(self):
        self.calls.append(("go_back",))
        if self.fail_back:
            raise RuntimeError("no history")

    def activate(self, key):
        if key not in self.tabs:
            raise ValueError("unknown tab")
        self.active = key
        self.calls.append(("activate", key))
        return dict(self.tabs[key])

    def close_active(self):
        if len(self.tabs) <= 1:
            raise ValueError("last tab")
        del self.tabs[self.active]
        self.active = next(iter(self.tabs))
        self.calls.append(("close_active",))

    def wait_until_ready(self):
        pass

    def call(self, method, **params):
        self.calls.append(("call", method))
        if method == "Runtime.evaluate" and params.get("expression") == "location.href":
            return {"result": {"value": self.href}}
        if method == "Page.captureScreenshot":
            return {"data": ""}
        return {}

    def read_options(self, session, node):
        return list(self.options)

    def collect_new_tabs(self):
        opened, self.opened = list(self.opened), []
        return opened

    def tab_infos(self):
        return [
            {"key": key, "url": tab.get("url"), "title": tab.get("title"), "active": key == self.active}
            for key, tab in self.tabs.items()
        ]

    def close(self):
        pass

    def stop_cloud(self):
        pass


class FakeProvider:
    def __init__(self, script, model="Beam-Test", effort="low"):
        self.script = list(script)
        self.calls = []
        self.model = model
        self.effort = effort

    def complete(self, messages, *, response_format, purpose, step=None, trace=None,
                 max_completion_tokens=None):
        self.calls.append({"messages": messages, "purpose": purpose, "step": step})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        output, usage = item
        return LLMCallResult(
            output=output, usage=usage or {}, latency_ms=7, model=self.model,
            effort=self.effort, reasoning_content="why", finish_reason="stop",
        )


@pytest.fixture(autouse=True)
def quiet_trace(monkeypatch):
    monkeypatch.setenv("JEV_LOG_LEVEL", "off")


def _agent(page, script, helpers=(), **kwargs):
    browser = FakeBrowser(page, **kwargs.pop("browser_kwargs", {}))
    agent = BeamAgent(
        page.get("url"), "Do the thing", browser=browser,
        provider=FakeProvider(script), helper_provider=FakeProvider(list(helpers)),
        **kwargs,
    )
    return agent, browser


def test_click_then_input_runs_until_url_change():
    page = _page([
        _action("e1", "click", "Next", node=1),
        _action("e2", "fill", "Name", node=2),
    ])
    agent, browser = _agent(page, [(_step(("click", {"index": 1}), ("input", {"index": 2, "text": "x"})), {})])
    real_act = browser.act

    def navigating_act(action, observed, **extra):
        if action.get("id") == "e1":
            browser.href = "https://example.test/next"
        return real_act(action, observed, **extra)

    browser.act = navigating_act
    snapshot = agent.command("tick")
    acts = [call for call in browser.calls if call[0] == "act"]
    assert [call[1] for call in acts] == ["e1"]
    assert snapshot["status"] == "ready"
    assert snapshot["history"][-1]["operation"] == "CLICK"
    assert any("page changed" in (note.get("text") or "") for note in agent.state["event_notes"])


def test_page_changer_stops_the_sequence():
    page = _page([_action("e1", "click", "Next", node=1)])
    agent, browser = _agent(page, [(
        _step(("navigate", {"url": "https://example.test/b"}), ("click", {"index": 1})), {},
    )])
    agent.command("tick")
    assert ("navigate", "https://example.test/b") in browser.calls
    assert not [call for call in browser.calls if call[0] == "act"]
    assert agent.state["history"][-1]["operation"] == "NAVIGATE"


def test_invalid_index_stops_without_mutation():
    page = _page([_action("e1", "click", "Next", node=1)])
    agent, browser = _agent(page, [(_step(("click", {"index": 99})), {})])
    snapshot = agent.command("tick")
    assert not [call for call in browser.calls if call[0] == "act"]
    assert snapshot["status"] == "ready"
    assert agent.state["failures"] == 1


def test_done_success_passes_judge_and_finishes():
    page = _page([])
    agent, browser = _agent(
        page,
        [(_step(("done", {"text": "The answer is 42.", "success": True})), {})],
        helpers=[({"complete": True, "missing": []}, {})],
    )
    snapshot = agent.command("tick")
    assert snapshot["status"] == "done"
    assert snapshot["answer"] == "The answer is 42."
    assert snapshot["history"][-1]["operation"] == "DONE"
    assert len(agent.state["text_calls"]) == 1


def test_done_rejected_three_times_blocks_with_partial():
    page = _page([])
    script = [(_step(("done", {"text": "maybe 42", "success": True})), {})] * 3
    helpers = [({"complete": False, "missing": ["no digits quoted"]}, {})] * 3
    agent, browser = _agent(page, script, helpers=helpers)
    for _ in range(3):
        snapshot = agent.command("tick")
    assert snapshot["status"] == "blocked"
    assert snapshot["partial"] == 1
    assert "no digits quoted" in snapshot["blocked_reason"]


def test_done_false_blocks_without_judge():
    page = _page([])
    helpers = FakeProvider([])
    browser = FakeBrowser(page)
    agent = BeamAgent(
        page.get("url"), "Do the thing", browser=browser,
        provider=FakeProvider([(_step(("done", {"text": "Login wall.", "success": False})), {})]),
        helper_provider=helpers,
    )
    snapshot = agent.command("tick")
    assert snapshot["status"] == "blocked"
    assert snapshot["answer"] == "Login wall."
    assert helpers.calls == []


def test_last_step_without_done_forces_a_stop():
    page = _page([_action("e1", "click", "Next", node=1)])
    agent, browser = _agent(page, [(_step(("click", {"index": 1})), {})])
    agent.config["max_decisions"] = 1
    snapshot = agent.command("tick")
    assert snapshot["status"] == "blocked"
    assert "last step" in snapshot["blocked_reason"]
    assert not [call for call in browser.calls if call[0] == "act"]


def test_five_consecutive_failures_stop():
    page = _page([])
    agent, browser = _agent(page, [ProviderError("boom")] * 5)
    for _ in range(5):
        snapshot = agent.command("tick")
    assert snapshot["status"] == "blocked"
    assert "5 consecutive failures" in snapshot["blocked_reason"]


def test_captcha_wall_backs_off_and_bans_the_url():
    page = _page(
        [_action("e1", "click", "Next", node=1)],
        url="https://example.test/sorry/index",
        text="Sorry, unusual traffic from your network",
    )
    agent, browser = _agent(page, [(_step(("find_text", {"pattern": "x"})), {})])
    snapshot = agent.command("tick")
    assert snapshot["history"][0]["operation"] == "CAPTCHA"
    assert agent.state["wall_hosts"] == ["example.test"]
    assert agent.state["failed_urls"] == ["example.test/sorry/index"]
    assert ("go_back",) in browser.calls
    assert snapshot["status"] == "ready"
    _prepared, error = agent._prepare_action(
        agent.state, agent.state["page"],
        {"name": "navigate", "url": "https://example.test/sorry/index", "new_tab": False},
    )
    assert error == "that URL already failed; try another route"


def test_repeated_walls_stop_with_takeover_note():
    page = _page(
        [], url="https://example.test/sorry/index",
        text="Sorry, unusual traffic from your network",
    )
    agent, browser = _agent(page, [(_step(("wait", {})), {})] * 3)
    for _ in range(3):
        browser.href = f"https://example.test/sorry/index?{len(agent.state['history'])}"
        snapshot = agent.command("tick")
    assert snapshot["status"] == "blocked"
    assert "Take over" in snapshot["blocked_reason"]


def test_cloud_wall_marks_restart_and_skips_planning():
    page = _page(
        [], url="https://example.test/sorry/index",
        text="Sorry, unusual traffic from your network",
    )
    agent, browser = _agent(page, [(_step(("wait", {})), {})])
    browser.config["browser"] = "cloud"
    snapshot = agent.command("tick")
    assert agent.state["cloud_restart_wanted"] == "example.test"
    assert agent._provider_override.calls == []
    assert snapshot["status"] == "ready"


def test_secret_placeholder_substitutes_only_on_allowed_host(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_SECRET_API", "s3cr3t-value")
    monkeypatch.setenv("JEV_SECRET_API_DOMAINS", "example.test")
    page = _page([_action("e1", "fill", "Token", node=1)])
    agent, browser = _agent(
        page, [(_step(("input", {"index": 1, "text": "<secret>API</secret>"})), {})],
    )
    seen = {}
    real_act = browser.act

    def capture(action, observed, text=None, **extra):
        seen["value"] = text
        return real_act(action, observed, text=text, **extra)

    browser.act = capture
    agent.command("tick")
    assert seen["value"] == "s3cr3t-value"
    note = agent.state["history"][-1]["note"]
    assert "s3cr3t-value" not in note
    agent.state["page"]["url"] = "https://evil.test/"
    _prepared, error = agent._prepare_action(
        agent.state, agent.state["page"],
        {"name": "input", "index": 1, "text": "<secret>API</secret>", "clear": False},
    )
    assert error == "secret API is not available on this host"


def test_sensitive_field_rejects_plain_text():
    page = _page([_action("e1", "fill", "Password", node=1)])
    agent, browser = _agent(page, [(_step(("input", {"index": 1, "text": "hunter2"})), {})])
    agent.command("tick")
    assert not [call for call in browser.calls if call[0] == "act"]
    assert agent.state["failures"] == 1


def test_challenge_click_is_refused():
    page = _page([_action("e1", "click", "I'm not a robot", node=1)])
    agent, browser = _agent(page, [(_step(("click", {"index": 1})), {})])
    agent.command("tick")
    assert not [call for call in browser.calls if call[0] == "act"]


def test_walled_page_suppresses_clicks():
    page = _page(
        [_action("e1", "click", "Next", node=1)],
        url="https://example.test/sorry/index",
        text="Sorry, unusual traffic from your network",
    )
    agent, browser = _agent(page, [(_step(("click", {"index": 1})), {})])
    browser.fail_back = True
    agent.command("tick")
    assert ("go_back",) in browser.calls
    assert not [call for call in browser.calls if call[0] == "act"]


def test_extract_records_memo_and_verified_fact():
    page = _page([])
    agent, browser = _agent(
        page, [(_step(("extract", {"text": "population"})), {})],
        helpers=[({"text": "The region has 10,722,000 residents."}, {})],
    )
    snapshot = agent.command("tick")
    assert snapshot["history"][-1]["operation"] == "EXTRACT"
    assert agent.state["memo"][-1]["source"] == "https://example.test/"
    assert "10,722,000" in agent.transcript.turns[-1]["verified_facts"][0]


def test_read_only_actions_do_not_stop_the_sequence():
    page = _page([_action("e1", "click", "Next", node=1)])
    agent, browser = _agent(page, [(
        _step(("find_text", {"pattern": "hello"}), ("click", {"index": 1})), {},
    )])
    agent.command("tick")
    assert ("act", "e1", "click") in browser.calls
    assert [entry["operation"] for entry in agent.state["history"]] == ["FIND_TEXT", "CLICK"]


def test_select_dropdown_matches_and_executes():
    page = _page([_action("e1", "select", "Sort", node=3)])
    agent, browser = _agent(page, [(_step(("select_dropdown", {"index": 1, "option": "Price"})), {})])
    browser.options = [
        {"label": "Relevance", "value": "r", "disabled": False},
        {"label": "Price", "value": "p", "disabled": False},
    ]
    agent.command("tick")
    assert agent.state["history"][-1]["operation"] == "SELECT_OPTION"


def test_scroll_container_repeats_pages():
    page = _page([
        {"id": "s1", "kind": "scroll", "label": "Scroll down box", "node": 9, "frame": "f0",
         "axis": "y", "delta": 80, "tag": "div"},
        {"id": "s2", "kind": "scroll", "label": "Scroll up box", "node": 9, "frame": "f0",
         "axis": "y", "delta": -80, "tag": "div"},
    ])
    agent, browser = _agent(page, [(_step(("scroll", {"index": 1, "direction": "down", "amount": 3})), {})])
    agent.command("tick")
    assert browser.calls.count(("act", "s1", "scroll")) == 3


def test_upload_requires_allowlisted_key(tmp_path):
    target = tmp_path / "doc.pdf"
    target.write_text("x")
    page = _page([_action("e1", "upload", "Attach", node=1)])
    agent, browser = _agent(page, [(_step(("upload_file", {"index": 1, "key": "nope"})), {})])
    agent.config["files"] = {"doc": str(target)}
    agent.command("tick")
    assert not [call for call in browser.calls if call[0] == "act"]
    assert agent.state["failures"] == 1


def test_save_and_read_file_round_trip(tmp_path):
    page = _page([])
    agent, browser = _agent(page, [(
        _step(
            ("save_file", {"filename": "facts.json", "content": '{"a": 1}'}),
            ("read_file", {"filename": "facts.json"}),
        ), {},
    )])
    browser.config["files_dir"] = str(tmp_path)
    agent.command("tick")
    assert (tmp_path / "facts.json").read_text() == '{"a": 1}'
    assert [entry["operation"] for entry in agent.state["history"]] == ["SAVE_FILE", "READ_FILE"]


def test_close_last_tab_is_refused():
    page = _page([])
    agent, browser = _agent(page, [(_step(("close_tab", {})), {})])
    agent.command("tick")
    assert ("close_active",) not in browser.calls
    assert agent.state["failures"] == 1


def test_step_detail_carries_thinking_and_plan():
    page = _page([_action("e1", "click", "Next", node=1)])
    output = _step(("click", {"index": 1}))
    output["evaluation_previous_goal"] = "Looking good. Verdict: Success"
    output["next_goal"] = "Open the next page"
    agent, browser = _agent(page, [(output, {})])
    agent.command("tick")
    detail = agent.state["history"][-1]["detail"]
    assert detail["thinking_ms"] == 7
    assert detail["evaluation"].endswith("Verdict: Success")
    assert detail["next_goal"] == "Open the next page"
    assert detail["actions"][0]["name"] == "click"
    assert detail["model"] == "Beam-Test"


def test_last_step_prompt_forces_done():
    page = _page([])
    agent, browser = _agent(page, [(_step(("wait", {"amount": 0})), {})] * 2)
    agent.config["max_decisions"] = 2
    agent.command("tick")
    agent.command("tick")
    final_prompt = agent._provider_override.calls[-1]["messages"][-1]["content"]
    assert "LAST STEP" in final_prompt


def test_judge_context_includes_final_page_text():
    page = _page([], text="The region has 10,722,000 residents.")
    agent, _browser = _agent(page, [])
    context = agent._judge_context(agent.state, page, "10,722,000 residents")
    assert "10,722,000" in context["final_page_text"]
    assert context["claimed_answer"] == "10,722,000 residents"


def test_judge_context_includes_open_tabs():
    page = _page([])
    agent, browser = _agent(page, [])
    browser.tabs["t2"] = {"url": "https://duckduckgo.com/?q=Bangkok+weather", "title": "weather"}
    context = agent._judge_context(agent.state, page, "opened weather in a new tab")
    assert "weather" in context["open_tabs"]
    assert "Example" in context["open_tabs"]


def test_executor_exception_records_failed_step_without_crashing():
    page = _page([_action("e1", "click", "Next", node=1)])
    agent, browser = _agent(page, [(_step(("click", {"index": 1})), {})])

    def boom(action, observed, **extra):
        raise RuntimeError("cdp exploded")

    browser.act = boom
    snapshot = agent.command("tick")
    assert snapshot["status"] == "ready"
    assert snapshot["history"][-1]["operation"] == "CLICK"
    assert "cdp exploded" in snapshot["history"][-1]["note"]


def _prompt(agent, call=-1):
    return agent._provider_override.calls[call]["messages"][-1]["content"]


def test_unverified_clicks_escalate_then_suppress():
    page = _page([_action("e1", "click", "Nonstop", node=1)])
    script = [(_step(("click", {"index": 1})), {})] * 7
    agent, _browser = _agent(page, script)
    for _ in range(3):
        agent.command("tick")
    assert "did not respond" not in _prompt(agent)
    agent.command("tick")
    assert "'Nonstop' did not respond after 3 tries" in _prompt(agent)
    for _ in range(3):
        agent.command("tick")
    assert "'Nonstop' is marked unmet after 6 tries" in _prompt(agent)
    assert "Nonstop" not in [item.get("label") for item in agent._by_index.values()]


def test_unverified_keys_in_dialog_escalate_without_suppression():
    page = _page([])
    agent, _browser = _agent(page, [])
    prev = {"fingerprint": "f1", "actions": []}
    fresh = _page([])
    fresh["fingerprint"] = "f1"
    fresh["overlay"] = "Stops"
    for _ in range(3):
        agent.state["_pending_verify"] = [{
            "name": "send_keys", "element": None, "exec": {},
            "fingerprint": "f1", "overlay": "Stops",
        }]
        agent._verify_pending(agent.state, prev, fresh, 1)
    notes = [note.get("text") or "" for note in agent.state["event_notes"]]
    assert any("'Stops' did not respond after 3 tries" in text for text in notes)
    assert any("close and reopen" in text for text in notes)
    assert agent._suppressed_widgets == set()
