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


def test_tabs_default_on(monkeypatch):
    monkeypatch.delenv("JEV_TABS", raising=False)
    assert resolve_config()["tabs"] is True
    monkeypatch.setenv("JEV_TABS", "0")
    assert resolve_config()["tabs"] is False
    assert resolve_config(tabs=True)["tabs"] is True
