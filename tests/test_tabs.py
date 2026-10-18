"""Popup tabs use code-owned ids. Unrelated pages are ignored."""

from jev_ultrafast.browser import Browser, bind_tabs, new_page_targets
from jev_ultrafast.config import resolve_config
from jev_ultrafast.model import action_space


def test_only_pages_opened_by_our_tab_are_tracked():
    events = [
        {"method": "Target.targetCreated", "params": {"targetInfo": {
            "type": "page", "targetId": "other", "openerId": "someone-else", "url": "https://evil.test",
        }}},
        {"method": "Target.targetCreated", "params": {"targetInfo": {
            "type": "page", "targetId": "popup", "openerId": "ours", "url": "https://example.test/new",
        }}},
        {"method": "Target.targetCreated", "params": {"targetInfo": {
            "type": "iframe", "targetId": "frame", "openerId": "ours", "url": "https://example.test/frame",
        }}},
    ]
    found = new_page_targets(events, {"ours"})
    assert [info["targetId"] for info in found] == ["popup"]


def test_tab_controls_use_code_owned_ids_and_cap_at_five():
    browser = Browser.__new__(Browser)
    browser.config = {"tabs": True}
    browser.tabs = {}
    browser.active = None
    browser._tab_serial = 0
    for index in range(6):
        browser._register_tab(f"cdp-{index}", f"session-{index}", f"https://example.test/{index}")
    assert list(browser.tabs) == ["t1", "t2", "t3", "t4", "t5"]
    assert "cdp-0" not in browser.tabs
    browser.active = "t1"
    state = bind_tabs(
        {
            "actions": [{"id": "e1", "kind": "click", "node": 1, "label": "Go"}],
            "url": "u",
            "text": "",
            "scroll": {"y": 0},
        },
        browser,
    )
    kinds = {action["kind"] for action in state["actions"]}
    assert kinds == {"click", "switch_tab", "close_tab"}
    switches = [action for action in state["actions"] if action["kind"] == "switch_tab"]
    assert [action["tab"] for action in switches] == ["t2", "t3", "t4", "t5"]
    assert all(not action["id"].startswith("cdp-") for action in switches)
    _elements, targets, controls = action_space(state["actions"])
    assert set(targets["SWITCH_TAB"]) == {"t2", "t3", "t4", "t5"}
    assert "CLOSE_TAB" in controls
    assert "cdp-1" not in targets["SWITCH_TAB"]


def test_switch_and_close_update_the_active_session(monkeypatch):
    import jev_ultrafast.browser as browser

    closed = []
    monkeypatch.setattr(browser, "cdp", lambda method, **params: closed.append((method, params)) or {})
    tab = Browser.__new__(Browser)
    tab.config = {"tabs": True}
    tab.tabs = {}
    tab.active = None
    tab._tab_serial = 0
    tab.session = "s1"
    tab.target = "cdp-1"
    tab._register_tab("cdp-1", "s1", "https://example.test/a")
    tab._register_tab("cdp-2", "s2", "https://example.test/b")
    tab.active = "t1"
    tab.activate("t2")
    assert tab.session == "s2" and tab.target == "cdp-2"
    tab.close_active()
    assert tab.active == "t1"
    assert tab.session == "s1"
    assert closed[0][0] == "Target.closeTarget"
    assert closed[0][1]["targetId"] == "cdp-2"


def _tab_browser():
    browser = Browser.__new__(Browser)
    browser.config = {"tabs": True}
    browser.tabs = {}
    browser.active = None
    browser._tab_serial = 0
    return browser


def test_tab_infos_refreshes_titles_from_targets(monkeypatch):
    import jev_ultrafast.browser as browser_module

    seen = []

    def fake_cdp(method, **params):
        seen.append(method)
        return {"targetInfos": [
            {"targetId": "cdp-1", "url": "https://example.test/a", "title": "Page A"},
            {"targetId": "cdp-2", "url": "https://example.test/b", "title": "Page B"},
            {"targetId": "foreign", "url": "https://evil.test", "title": "Evil"},
        ]}

    monkeypatch.setattr(browser_module, "cdp", fake_cdp)
    browser = _tab_browser()
    browser._register_tab("cdp-1", "s1", "https://example.test/a")
    browser._register_tab("cdp-2", "s2", "https://example.test/b")
    browser.active = "t2"
    rows = browser.tab_infos()
    assert seen == ["Target.getTargets"]
    assert rows == [
        {"key": "t1", "url": "https://example.test/a", "title": "Page A", "active": False},
        {"key": "t2", "url": "https://example.test/b", "title": "Page B", "active": True},
    ]
    assert browser.tabs["t1"]["title"] == "Page A"


def test_tab_infos_falls_back_to_cache_when_cdp_fails(monkeypatch):
    import jev_ultrafast.browser as browser_module

    def boom(method, **params):
        raise RuntimeError("no daemon")

    monkeypatch.setattr(browser_module, "cdp", boom)
    browser = _tab_browser()
    browser._register_tab("cdp-1", "s1", "https://example.test/a", title="Cached A")
    browser.active = "t1"
    assert browser.tab_infos() == [
        {"key": "t1", "url": "https://example.test/a", "title": "Cached A", "active": True},
    ]


def test_collect_new_tabs_captures_title(monkeypatch):
    import jev_ultrafast.browser as browser_module

    events = [{"method": "Target.targetCreated", "params": {"targetInfo": {
        "type": "page", "targetId": "popup", "openerId": "ours",
        "url": "https://example.test/new", "title": "Fresh Popup",
    }}}]
    monkeypatch.setattr(browser_module, "drain_events", lambda: events)
    monkeypatch.setattr(
        browser_module, "cdp",
        lambda method, **params: {"sessionId": "s-popup"} if method == "Target.attachToTarget" else {},
    )
    browser = _tab_browser()
    browser._register_tab("ours", "s1", "https://example.test/")
    browser.active = "t1"
    assert browser.collect_new_tabs() == ["t2"]
    assert browser.tabs["t2"]["title"] == "Fresh Popup"


def test_remember_tab_caches_active_url_and_title():
    browser = _tab_browser()
    browser._register_tab("cdp-1", "s1", "https://old.test")
    browser.active = "t1"
    browser._remember_tab({"url": "https://example.test/a", "title": "Page A"})
    assert browser.tabs["t1"]["url"] == "https://example.test/a"
    assert browser.tabs["t1"]["title"] == "Page A"
    browser._remember_tab({})
    assert browser.tabs["t1"]["title"] == "Page A"


def test_bind_tabs_prefers_title_in_labels():
    browser = _tab_browser()
    browser._register_tab("cdp-1", "s1", "https://example.test/a", title="Page A")
    browser._register_tab("cdp-2", "s2", "https://example.test/b")
    browser.active = "t1"
    state = bind_tabs({"actions": [], "url": "u", "text": "", "scroll": {"y": 0}}, browser)
    switches = [action for action in state["actions"] if action["kind"] == "switch_tab"]
    assert [action["label"] for action in switches] == ["Switch to tab https://example.test/b"]
    browser.active = "t2"
    state = bind_tabs({"actions": [], "url": "u", "text": "", "scroll": {"y": 0}}, browser)
    switches = [action for action in state["actions"] if action["kind"] == "switch_tab"]
    assert [action["label"] for action in switches] == ["Switch to tab Page A"]


def test_new_tab_offered_until_cap():
    browser = _tab_browser()
    browser._register_tab("cdp-1", "s1", "https://example.test/a")
    browser.active = "t1"
    state = bind_tabs({"actions": [], "url": "u", "text": "", "scroll": {"y": 0}}, browser)
    assert [action["kind"] for action in state["actions"]] == ["new_tab"]
    _elements, _targets, controls = action_space(state["actions"])
    assert controls["NEW_TAB"]["id"] == "new_tab"
    for index in range(2, 6):
        browser._register_tab(f"cdp-{index}", f"s{index}", f"https://example.test/{index}")
    state = bind_tabs({"actions": [], "url": "u", "text": "", "scroll": {"y": 0}}, browser)
    assert "new_tab" not in {action["kind"] for action in state["actions"]}


def test_new_tab_registers_and_activates(monkeypatch):
    import jev_ultrafast.browser as browser_module

    calls = []

    def fake_cdp(method, **params):
        calls.append(method)
        if method == "Target.createTarget":
            return {"targetId": "cdp-9"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s9"}
        return {}

    monkeypatch.setattr(browser_module, "cdp", fake_cdp)
    browser = _tab_browser()
    browser.context = None
    browser._register_tab("cdp-1", "s1", "https://example.test/a")
    browser.active = "t1"
    tab = browser.new_tab()
    assert browser.active == "t2"
    assert tab["target"] == "cdp-9" and browser.session == "s9"
    assert "Target.createTarget" in calls


def test_tabs_default_on(monkeypatch):
    monkeypatch.delenv("JEV_TABS", raising=False)
    assert resolve_config()["tabs"] is True
    monkeypatch.setenv("JEV_TABS", "0")
    assert resolve_config()["tabs"] is False
    assert resolve_config(tabs=True)["tabs"] is True


def test_new_tab_reuses_idle_blank(monkeypatch):
    import jev_ultrafast.browser as browser_module

    calls = []
    monkeypatch.setattr(browser_module, "cdp", lambda method, **kw: calls.append(method) or {})
    browser = _tab_browser()
    browser._register_tab("cdp-1", "s1", "https://example.test/a")
    browser._register_tab("cdp-2", "s2", "about:blank")
    browser.active = "t1"
    browser.session = "s1"
    browser.target = "cdp-1"
    browser.new_tab()
    assert browser.active == "t2"
    assert browser.session == "s2"
    assert calls == []


def test_new_tab_opens_when_no_blank(monkeypatch):
    import jev_ultrafast.browser as browser_module

    calls = []

    def fake_cdp(method, **params):
        calls.append(method)
        if method == "Target.createTarget":
            return {"targetId": "cdp-new"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s-new"}
        return {}

    monkeypatch.setattr(browser_module, "cdp", fake_cdp)
    browser = _tab_browser()
    browser._register_tab("cdp-1", "s1", "https://example.test/a")
    browser.active = "t1"
    browser.session = "s1"
    browser.target = "cdp-1"
    browser.new_tab()
    assert browser.active == "t2"
    assert browser.tabs["t2"]["url"] == "about:blank"
    assert "Target.createTarget" in calls


def test_new_tab_skips_the_active_blank(monkeypatch):
    import jev_ultrafast.browser as browser_module

    def fake_cdp(method, **params):
        if method == "Target.createTarget":
            return {"targetId": "cdp-new"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s-new"}
        return {}

    monkeypatch.setattr(browser_module, "cdp", fake_cdp)
    browser = _tab_browser()
    browser._register_tab("cdp-1", "s1", "about:blank")
    browser.active = "t1"
    browser.session = "s1"
    browser.target = "cdp-1"
    browser.new_tab()
    assert browser.active == "t2"


def _tab_gate_page():
    return {
        "url": "https://example.test/",
        "actions": [
            {"id": "new_tab", "kind": "new_tab", "label": "Open a new tab"},
            {"id": "e1", "kind": "click", "node": 1, "label": "Go"},
        ],
    }


def test_new_tab_gate_suppresses_without_goal_words():
    from jev_ultrafast.agent import goal_wants_new_tab, noop_key, suppression_reasons

    assert not goal_wants_new_tab("open x.com")
    assert not goal_wants_new_tab("find the population of Bangkok on Wikipedia")
    reasons = suppression_reasons({"goal": "open x.com"}, _tab_gate_page())
    key = noop_key({"kind": "new_tab", "label": "Open a new tab"})
    assert reasons[key] == "goal needs no new tab"


def test_new_tab_gate_keeps_for_tab_goals():
    from jev_ultrafast.agent import goal_wants_new_tab, noop_key, suppression_reasons

    assert goal_wants_new_tab("now open the weather for Bangkok in a new tab")
    assert goal_wants_new_tab("keep this page open and compare prices")
    reasons = suppression_reasons(
        {"goal": "now open the weather for Bangkok in a new tab"}, _tab_gate_page()
    )
    key = noop_key({"kind": "new_tab", "label": "Open a new tab"})
    assert key not in reasons
