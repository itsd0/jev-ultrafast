"""Offline contracts for element-identity freshness. No browsers, no paid APIs."""

from unittest.mock import Mock

from jev_ultrafast import agent as loop
from jev_ultrafast import browser as browser_module
from jev_ultrafast.browser import Browser, StalePage, modal_observed_empty


def page():
    return {
        "url": "https://example.test/",
        "title": "Shop",
        "text": "Buy the book, only 3 left",
        "marker": [111.0, "https://example.test/"],
        "page_key": [[111.0, "https://example.test/"]],
        "fingerprint": "fp",
        "guards": {"f0:7": {"role": "button", "name": "buy", "tag": "BUTTON", "href": None}},
        "actions": [
            {"id": "e1", "kind": "click", "role": "button", "label": "Buy", "node": 7, "frame": "f0"},
            {"id": "scroll_down", "kind": "scroll", "label": "Scroll down"},
        ],
    }


def browser(evaluate=None, call=None):
    tab = Browser.__new__(Browser)
    tab.session = "sess-test"
    tab.config = {}
    tab.frame_sessions = {}
    tab.evaluate = Mock(side_effect=evaluate or [])
    tab.call = Mock(side_effect=call)
    return tab


def test_targeted_fresh_ignores_live_text_and_values():
    """The countdown text changed and a field was typed elsewhere; the Buy
    button's identity did not, so the decision stands."""
    tab = browser(evaluate=[[[[111.0, "https://example.test/"]], {"role": "button", "name": "buy"}]])
    assert tab.fresh(page(), page()["actions"][0]) is True


def test_targeted_stale_on_replaced_or_renamed_node():
    tab = browser(evaluate=[
        [[[[111.0, "https://example.test/"]], None]],  # node detached by a re-render
    ])
    assert tab.fresh(page(), page()["actions"][0]) is False
    tab = browser(evaluate=[
        [[[[111.0, "https://example.test/"]], {"role": "button", "name": "borrow"}]],
    ])
    assert tab.fresh(page(), page()["actions"][0]) is False


def test_scroll_uses_document_identity_only():
    tab = browser(evaluate=[[111.0, "https://example.test/"]])
    moved = page()
    moved["text"] = "an entirely different lazy-loaded page"
    moved["actions"] = [{"id": "scroll_down", "kind": "scroll", "label": "Scroll down"}]
    assert tab.fresh(moved, moved["actions"][0]) is True


def test_navigation_invalidates_document_identity():
    tab = browser(evaluate=[[222.0, "https://other.test/"], [222.0, "https://other.test/"]])
    assert tab.fresh(page()) is False
    assert tab.fresh(page(), {"kind": "scroll", "label": "Scroll down"}) is False


def test_ungated_kinds_skip_evaluation():
    tab = browser()
    for kind in ("navigate", "switch_tab", "close_tab"):
        assert tab.fresh(page(), {"kind": kind, "label": kind}) is True
    tab.evaluate.assert_not_called()


def test_missing_route_session_is_stale():
    tab = browser()
    action = {"kind": "click", "role": "button", "label": "Buy", "node": 7, "route": "gone"}
    assert tab.fresh(page(), action) is False
    tab.evaluate.assert_not_called()


def test_settle_dom_never_raises():
    tab = browser(evaluate=RuntimeError("navigated mid-settle"))
    tab.settle_dom()  # must not raise
    tab.evaluate.assert_called_once()
    assert "MutationObserver" in tab.evaluate.call_args.args[0]
    assert tab.evaluate.call_args.kwargs == {"await_promise": True}
    tab.call.assert_not_called()


def test_retarget_finds_the_same_control_under_a_shifted_id():
    locator = loop.pending_locator(page(), "e1")
    assert locator == {"kind": "click", "role": "button", "label": "Buy", "frame": "f0", "old_id": "e1"}
    shifted = page()
    shifted["actions"] = [
        {"id": "e9", "kind": "click", "role": "button", "label": "  BUY ", "node": 30, "frame": "f0"},
        {"id": "e10", "kind": "click", "role": "button", "label": "Sell", "node": 31, "frame": "f0"},
    ]
    match, count = loop.retarget_action(shifted, locator)
    assert (match["id"], count) == ("e9", 1)


def test_retarget_rejects_ambiguous_missing_and_cross_frame():
    locator = {"kind": "click", "role": "button", "label": "Buy", "frame": "f0"}
    twin = {"id": "e1", "kind": "click", "role": "button", "label": "Buy", "frame": "f0"}
    assert loop.retarget_action({"actions": [twin, {**twin, "id": "e2"}]}, locator) == (None, 2)
    assert loop.retarget_action({"actions": []}, locator) == (None, 0)
    assert loop.retarget_action({"actions": [{**twin, "frame": "f3"}]}, locator) == (None, 0)
    assert loop.retarget_action({"actions": [twin]}, None) == (None, 0)


def test_retarget_renamed_control_picks_the_same_role_first():
    locator = {"kind": "fill", "role": "searchbox", "label": "Search", "frame": "f0"}
    renamed = {"id": "e9", "kind": "fill", "role": "searchbox", "label": "Search products",
               "node": 30, "frame": "f0"}
    match, count = loop.retarget_action({"actions": [renamed]}, locator)
    assert (match["id"], count) == ("e9", 1)
    other_role = {"id": "e2", "kind": "fill", "role": "textbox", "label": "Search the shop",
                  "node": 31, "frame": "f0"}
    assert loop.retarget_action({"actions": [other_role]}, locator) == (None, 0)
    assert loop.retarget_action({"actions": [renamed, other_role]}, locator)[0]["id"] == "e9"
    second = {"id": "e3", "kind": "fill", "role": "searchbox", "label": "Search brands",
              "node": 32, "frame": "f0"}
    assert loop.retarget_action({"actions": [renamed, second]}, locator) == (None, 2)


def test_post_act_matching_unifies_only_same_role_twins():
    action = {"id": "e1", "kind": "fill", "role": "searchbox", "label": "Search", "value": "x"}
    before = {"fingerprint": "fp1", "actions": [action]}
    other_role = {"id": "e2", "kind": "fill", "role": "textbox", "label": "Search", "value": ""}
    same_role = {"id": "e3", "kind": "fill", "role": "searchbox", "label": "Search", "value": "x"}
    after = {"fingerprint": "fp2", "actions": [other_role, same_role]}
    info = loop.Agent._verify_info(action, before, after, {"text_verified": True})
    assert info["after"]["value"] == "x"  # matched the searchbox, not the first label twin
    click = {"id": "c1", "kind": "click", "role": "button", "label": "Search", "value": "x"}
    wrong = {"id": "c2", "kind": "click", "role": "textbox", "label": "Search", "value": "x"}
    right = {"id": "c3", "kind": "click", "role": "button", "label": "Search", "value": "y"}
    assert loop.control_unchanged(click, before, {"actions": [wrong, right]}) is None


def test_pending_locator_skips_terminal_and_coordinate_choices():
    assert loop.pending_locator(page(), "DONE") is None
    assert loop.pending_locator(page(), "BLOCKED") is None
    assert loop.pending_locator(page(), "VISION_CLICK") is None
    assert loop.pending_locator(page(), "e404") is None


def test_loop_hold_survives_an_id_shift():
    from jev_ultrafast.agent import detect_cycle, hold_actions, offered_actions

    window = [
        {"key": ("CLICK", "More", "u", "t"), "identity": ("u", "t"), "id": "e2",
         "label": "More", "kind": "click", "role": "link", "progress": False},
    ] * 4
    cycle = detect_cycle(window)
    assert set(cycle["keys"]) == {loop.noop_key({"kind": "click", "label": "More", "role": "link"})}
    holds = hold_actions({}, cycle["keys"])
    shifted = [{"id": "e7", "kind": "click", "role": "link", "label": "More"},
               {"id": "e8", "kind": "click", "role": "link", "label": "Less"}]
    assert [a["id"] for a in offered_actions(shifted, holds)] == ["e8"]


def test_stale_act_gives_up_to_redecide_after_two_retargets():
    import time

    from jev_ultrafast.trace import Trace, get_logger

    agent = loop.Agent.__new__(loop.Agent)
    agent.trace = Trace(run_id="test-fresh", level=0)
    agent.log = get_logger("agent")
    agent.screenshots = False
    agent.pending_text = None
    agent.blocked_streak = 0
    agent.config = {}
    p = page()
    browser = Mock()
    browser.observe.return_value = p
    browser.act.side_effect = StalePage("still stale")
    agent.state = {
        "browser": browser, "page": p, "history": [], "status": "predicted",
        "started_at": time.perf_counter(), "text_calls": [], "loop_window": [],
        "decision": {"choice": "e1", "operation": "CLICK", "target": "1",
                     "confidence": 1.0, "probabilities": {"e1": 1.0},
                     "latency_ms": 1, "usage": {}},
    }
    out = agent._act_resilient(
        agent.state, 1, agent.state["decision"],
        {"kind": "click", "role": "button", "label": "Buy", "frame": "f0", "old_id": "e1"},
    )
    assert browser.act.call_count == 3  # initial try plus two retargets
    assert out["status"] == "ready" and out["decision"] is None
    assert agent.state.get("held_actions", {}) == {}


def test_modal_observed_empty_triggers_only_on_a_control_free_modal():
    wait = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}
    assert modal_observed_empty({"modal_scoped": True, "actions": [wait]}) is True
    assert modal_observed_empty({"modal_scoped": True, "actions": []}) is True
    assert modal_observed_empty(None) is False
    assert modal_observed_empty({"modal_scoped": False, "actions": [wait]}) is False
    assert modal_observed_empty({"actions": [wait]}) is False
    filled = [wait, {"id": "e1", "kind": "click", "label": "Close"}]
    assert modal_observed_empty({"modal_scoped": True, "actions": filled}) is False


def test_observe_re_settles_an_empty_modal_once(monkeypatch):
    tab = browser()
    tab.settle_dom = Mock()
    tab._finish_observation = Mock(side_effect=lambda state: state)
    empty = {"modal_scoped": True, "actions": [{"id": "wait", "kind": "wait"}]}
    filled = {"modal_scoped": True, "actions": [{"id": "e1", "kind": "click"}]}
    monkeypatch.setattr(
        browser_module, "browser_operation",
        Mock(side_effect=[empty, filled, filled]),
    )
    assert tab.observe(screenshot=False) is filled
    assert browser_module.browser_operation.call_count == 2
    assert tab.settle_dom.call_count == 2
    assert tab.settle_dom.call_args_list[1].kwargs["quiet_ms"] == 300
    assert tab.observe(screenshot=False) is filled
    assert browser_module.browser_operation.call_count == 3


def test_observe_keeps_the_first_snapshot_when_the_modal_retry_goes_stale(monkeypatch):
    tab = browser()
    tab.settle_dom = Mock()
    tab._finish_observation = Mock(side_effect=lambda state: state)
    empty = {"modal_scoped": True, "actions": [{"id": "wait", "kind": "wait"}]}
    monkeypatch.setattr(
        browser_module, "browser_operation",
        Mock(side_effect=[empty, StalePage("navigating"), empty]),
    )
    assert tab.observe(screenshot=False) is empty
    assert browser_module.browser_operation.call_count == 2
