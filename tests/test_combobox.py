"""Offline contracts for combobox commit and no-op toggles. No browsers, no paid APIs."""

from unittest.mock import Mock

from jev_ultrafast import agent as loop
from jev_ultrafast.agent import control_unchanged, noop_key, page_entity_match, page_for_choice
from jev_ultrafast.browser import commit_combobox, match_combobox_option
from jev_ultrafast.trace import Trace, get_logger


def options(*labels):
    return [{"label": label, "x": 10.0, "y": 20.0} for label in labels]


def test_match_prefers_exact_then_prefix_then_tokens():
    opts = options("Lisbon Falls, Maine", "Lisbon, Portugal", "Lismore, Australia")
    assert match_combobox_option("Lisbon, Portugal", opts)["label"] == "Lisbon, Portugal"
    assert match_combobox_option("Lisbon", opts)["label"] == "Lisbon Falls, Maine"  # prefix tie: first
    assert match_combobox_option("portugal lisbon", opts)["label"] == "Lisbon, Portugal"  # all tokens
    assert match_combobox_option("maine boston", opts)["label"] == "Lisbon Falls, Maine"  # any token


def test_match_skips_empties_and_rejects_non_matches():
    assert match_combobox_option("Lisbon", options("", "  ")) is None
    assert match_combobox_option("Xyz", options("Lisbon, Portugal")) is None
    assert match_combobox_option("", options("Lisbon, Portugal")) is None


def test_plain_fields_skip_the_commit(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    calls = []

    def evaluate(expression, _await_promise=False):
        calls.append(expression)
        if "__jevComboState" in expression:
            return {"combo": False}
        return []

    assert commit_combobox(Mock(), evaluate, {"role": "spinbutton", "node": 1}, "5") == {}
    assert commit_combobox(Mock(), evaluate, {"role": "textbox", "node": 1}, "hi") == {}
    assert calls and all("__jevCombo" in expression for expression in calls)


def _combo_script(monkeypatch, states, option_lists, values):
    """Fake CDP: scripted combo state, option polls, and field reads."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    calls = []

    def call(method, **params):
        calls.append((method, params))

    def evaluate(expression, _await_promise=False):
        if "__jevComboState" in expression:
            return states.pop(0)
        if "__jevComboOptions" in expression:
            return option_lists.pop(0) if option_lists else []
        if "__jevComboFocus" in expression:
            return True
        return values.pop(0) if values else None

    return call, evaluate, calls


def test_commit_clicks_the_best_option(monkeypatch):
    call, evaluate, calls = _combo_script(
        monkeypatch,
        states=[{"combo": True, "expanded": "true"}],
        option_lists=[[], options("Lisbon Falls, Maine", "Lisbon, Portugal")],
        values=["Lisbon, Portugal", "Lisbon, Portugal"],
    )
    result = commit_combobox(call, evaluate, {"role": "combobox", "node": 3}, "Lisbon", "Porto")
    assert result["commit"] == "option-click"
    assert result["picked"] == "Lisbon Falls, Maine"
    assert result["committed"] is True and result["reverted"] is False and result["retried"] is False
    pressed = [params for method, params in calls if method == "Input.dispatchMouseEvent"]
    assert pressed and all(params["x"] == 10.0 for params in pressed)


def test_commit_falls_back_to_arrow_down_enter(monkeypatch):
    call, evaluate, _calls = _combo_script(
        monkeypatch,
        states=[{"combo": True, "expanded": "false"}],
        option_lists=[[] for _ in range(10)],
        values=["12/06/2027", "12/06/2027"],
    )
    keys = []
    orig_call = call

    def recording_call(method, **params):
        if method == "Input.dispatchKeyEvent" and params.get("type") == "keyDown":
            keys.append(params.get("key"))
        return orig_call(method, **params)

    result = commit_combobox(recording_call, evaluate, {"role": "combobox", "node": 3}, "12/06", "06/01")
    assert result["commit"] == "arrow-enter"
    assert result["committed"] is True
    assert keys[:2] == ["ArrowDown", "Enter"]


def test_revert_retries_once_without_a_wrong_click(monkeypatch):
    call, evaluate, _calls = _combo_script(
        monkeypatch,
        states=[{"combo": True, "expanded": "true"}],
        option_lists=[[], options("Lisbon, Portugal"), options("Lisbon, Portugal")],
        values=["Xyz", "Porto"],
    )
    result = commit_combobox(call, evaluate, {"role": "combobox", "node": 3}, "Xyz", "Porto")
    assert result["commit"] == "no-match"
    assert result["picked"] is None
    assert result["reverted"] is True and result["retried"] is True
    assert result["committed"] is False


def test_stale_field_reports_an_uncommitted_commit(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    result = commit_combobox(Mock(), lambda *_args: None, {"role": "combobox", "node": 3}, "x")
    assert result == {"commit": "stale", "committed": False}


def test_fill_verify_fails_an_uncommitted_combo():
    action = {"kind": "fill", "label": "Destination", "value": "Porto"}
    before = {"fingerprint": "a", "actions": []}
    after = {"fingerprint": "b", "actions": []}
    ok = loop.Agent._verify_info(action, before, after, {"text_verified": True, "commit": "option-click",
                                                         "committed": True})
    assert ok["success"] is True and ok["commit"]["strategy"] == "option-click"
    bad = loop.Agent._verify_info(action, before, after, {"text_verified": True, "commit": "arrow-enter",
                                                          "committed": False, "reverted": True})
    assert bad["success"] is False and "commit failed" in bad["next"]
    expanded = loop.Agent._verify_info(action, before, after, {"text_verified": False, "commit": "option-click",
                                                               "committed": True})
    assert expanded["success"] is True and expanded["next"] is None


def test_expanded_toggle_reports_already_open():
    action = {"kind": "click", "label": "Open Search", "expanded": "true"}
    after = {"actions": [{"kind": "click", "label": "Open Search", "expanded": "true"}]}
    assert control_unchanged(action, {}, after) == "already open"
    closed = {"actions": [{"kind": "click", "label": "Open Search", "expanded": "false"}]}
    assert control_unchanged(action, {}, closed) is None


def test_noop_key_is_stable_and_json_safe():
    import json

    key = noop_key({"kind": "click", "id": "e4", "label": "Open Search"})
    assert key == noop_key({"kind": "click", "id": "e9", "label": "Open Search"})
    assert json.dumps({"dead": [key]})


def _bare_agent():
    agent = loop.Agent.__new__(loop.Agent)
    agent.screenshots = False
    agent.log = get_logger("agent")
    agent.trace = Trace(run_id="test-noop", level=0)
    return agent


def test_note_noop_suppresses_after_two_unchanged_clicks():
    agent = _bare_agent()
    state = {"page": {"url": "https://example.test/"}, "history": [{"step": 1}], "event_notes": []}
    action = {"kind": "click", "label": "Open Search"}
    agent._note_noop(state, action, "value unchanged")
    assert state["noop_page"] == {}
    agent._note_noop(state, action, "value unchanged")
    assert state["noop_page"] == {"https://example.test/": [noop_key(action)]}
    assert "already open" in state["event_notes"][-1]["text"]
    assert "use the controls inside it" in state["event_notes"][-1]["text"]


def test_note_noop_suppresses_already_open_immediately():
    agent = _bare_agent()
    state = {"page": {"url": "https://example.test/"}, "history": [], "event_notes": []}
    agent._note_noop(state, {"kind": "click", "label": "Open Search"}, "already open")
    assert state["noop_page"] == {"https://example.test/": [noop_key({"kind": "click", "label": "Open Search"})]}


def test_page_for_choice_drops_noops_and_orders_inner_first():
    toggle = {"id": "e1", "kind": "click", "label": "Open Search"}
    inner = {"id": "e2", "kind": "fill", "label": "Search", "inner": True}
    other = {"id": "e3", "kind": "click", "label": "Menu"}
    state = {
        "page": {"url": "https://example.test/", "fingerprint": "f", "actions": [toggle, other, inner]},
        "noop_page": {"https://example.test/": [noop_key(toggle)]},
    }
    shown = page_for_choice(state)
    assert [a["id"] for a in shown["actions"]] == ["e2", "e3"]


def test_open_twin_expanded_is_suppressed_up_front():
    agent = _bare_agent()
    twin = {"id": "e1", "kind": "click", "label": "Open City", "expanded": "true"}
    agent.state = {"page": {"url": "https://example.test/", "fingerprint": "f", "actions": [twin]},
                   "event_notes": [], "history": []}
    agent._update_suppression(agent.state)
    assert agent.state["noop_page"] == {"https://example.test/": [noop_key(twin)]}
    assert "already open" in agent.state["event_notes"][-1]["text"]


def test_entity_match_compares_page_to_subgoal():
    page = {"title": "Jupiter - Wikipedia", "url": "https://en.wikipedia.org/wiki/Jupiter",
            "text": "Jupiter is the fifth planet. It has 95 moons."}
    assert page_entity_match(page, "Find how many moons Jupiter has") is True
    god = {"title": "Jupiter (god) - Wikipedia", "url": "https://en.wikipedia.org/wiki/Jupiter_(god)",
           "text": "Jupiter was the chief deity of Roman state religion."}
    assert page_entity_match(god, "Find the boiling point of tungsten") is False
    assert page_entity_match(god, "") is True


def test_back_fires_on_off_goal_detour_despite_claimed_gain():
    agent = _bare_agent()
    page = {"url": "https://en.wikipedia.org/wiki/Jupiter_(god)", "title": "Jupiter (god)",
            "text": "chief deity of Roman state religion"}
    agent.state = {
        "browser": Mock(**{"observe.return_value": {"url": "https://example.test/"}}),
        "page": page,
        "history": [],
        "subgoals": [{"id": 0, "text": "Find the boiling point of tungsten"}],
        "excursion": {"from": "https://example.test/", "at": page["url"], "label": "Jupiter"},
        "event_notes": [],
    }
    agent._maybe_go_back(agent.state, page["url"], True, ["tungsten boils at 5555 C"])
    assert agent.state["backs_used"] == 1
    assert agent.state["excursion"] is None


def test_back_keeps_on_goal_pages_with_gain():
    agent = _bare_agent()
    page = {"url": "https://en.wikipedia.org/wiki/Tungsten", "title": "Tungsten",
            "text": "Tungsten boils at 5555 C."}
    browser = Mock(**{"observe.return_value": page})
    agent.state = {
        "browser": browser, "page": page, "history": [],
        "subgoals": [{"id": 0, "text": "Find the boiling point of tungsten"}],
        "excursion": {"from": "https://example.test/", "at": page["url"], "label": "Tungsten"},
        "event_notes": [],
    }
    agent._maybe_go_back(agent.state, page["url"], True, [])
    assert agent.state["excursion"] is None
    browser.go_back.assert_not_called()
