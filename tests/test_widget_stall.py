"""Widget-stall escalation: shared tracker plus the classic hook. Offline."""

from jev_ultrafast import agent as loop
from jev_ultrafast.trace import Trace
from jev_ultrafast.widget_stall import (
    RUN_HOLD_STEPS,
    WidgetStallTracker,
    categorize,
    control_key,
    is_modal_key,
    modal_key,
    stall_after,
    suggest_change,
    widget_key_for,
)


def test_three_unverified_escalates_then_gives_up():
    tracker = WidgetStallTracker(after=3)
    key = control_key("click", "Nonstop")
    events = [
        tracker.note(key, label="Nonstop", verified=False, strategy="click", fingerprint="f1")
        for _ in range(3)
    ]
    assert events[:2] == [None, None]
    first = events[2]
    assert first["level"] == 1 and first["give_up"] is False
    assert first["key"] == key and first["attempts"] == 3
    events = [
        tracker.note(key, label="Nonstop", verified=False, strategy="keyboard", fingerprint="f1")
        for _ in range(3)
    ]
    assert events[:2] == [None, None]
    second = events[2]
    assert second["level"] == 2 and second["give_up"] is True
    assert key in tracker.abandoned
    # Abandoned keys never fire again.
    assert tracker.note(key, verified=False, strategy="click", fingerprint="f1") is None


def test_verified_resets_the_block_but_keeps_the_level():
    tracker = WidgetStallTracker(after=2)
    key = control_key("fill", "Max price")
    tracker.note(key, verified=False, strategy="type", fingerprint="f1")
    assert tracker.note(key, verified=False, strategy="type", fingerprint="f1")["level"] == 1
    assert tracker.note(key, verified=True, fingerprint="f1") is None
    tracker.note(key, verified=False, strategy="type", fingerprint="f1")
    # A success between blocks does not forgive the earlier escalation.
    assert tracker.note(key, verified=False, strategy="type", fingerprint="f1")["level"] == 2


def test_other_keys_do_not_trigger_and_unknown_ignored():
    tracker = WidgetStallTracker(after=2)
    one = control_key("click", "Next")
    two = control_key("click", "Back")
    assert tracker.note(one, verified=False, fingerprint="f1") is None
    assert tracker.note(two, verified=False, fingerprint="f1") is None
    assert tracker.note(one, verified=False, fingerprint="f1")["level"] == 1
    assert tracker.note(two, verified=False, fingerprint="f1")["level"] == 1
    assert tracker.note(None, verified=False, fingerprint="f1") is None


def test_fingerprint_churn_keeps_levels_but_navigation_resets():
    tracker = WidgetStallTracker(after=2)
    key = modal_key("Stops")
    tracker.note(key, verified=False, strategy="keys", fingerprint="f1")
    assert tracker.note(key, verified=False, strategy="keys", fingerprint="f1")["level"] == 1
    # Same dialog across a re-render: the next block is escalation 2.
    tracker.note(key, verified=False, strategy="keys", fingerprint="f2")
    assert tracker.note(key, verified=False, strategy="keys", fingerprint="f2")["level"] == 2
    # A new page starts over.
    tracker.reset()
    tracker.note(key, verified=False, strategy="keys", fingerprint="f3")
    assert tracker.note(key, verified=False, strategy="keys", fingerprint="f3")["level"] == 1


def test_stall_after_env(monkeypatch):
    monkeypatch.delenv("JEV_WIDGET_STALL_N", raising=False)
    assert stall_after() == 3
    assert WidgetStallTracker().after == 3
    monkeypatch.setenv("JEV_WIDGET_STALL_N", "2")
    assert stall_after() == 2
    tracker = WidgetStallTracker()
    key = control_key("click", "Go")
    assert tracker.note(key, verified=False, fingerprint="f1") is None
    assert tracker.note(key, verified=False, fingerprint="f1")["level"] == 1
    monkeypatch.setenv("JEV_WIDGET_STALL_N", "bogus")
    assert stall_after() == 3
    monkeypatch.setenv("JEV_WIDGET_STALL_N", "0")
    assert stall_after() == 3


def test_suggest_change_skips_tried_strategies():
    assert suggest_change([], True) == "type the value directly into the field and press Enter"
    assert suggest_change(["type+enter"], True).startswith("drive it from the keyboard")
    assert suggest_change(["type", "click", "keyboard"], True).startswith("close and reopen")
    assert suggest_change(["type", "click", "keys", "reopen"], True) == (
        "a different control in the same dialog"
    )
    # No field to type into: keyboard comes first.
    assert suggest_change(["click"], False).startswith("drive it from the keyboard")
    assert categorize("send_keys") == "keys"
    assert categorize("type+commit") == "type"
    assert categorize("SELECT_OPTION") == "select option"


def test_widget_key_for_element_modal_and_unknown():
    key, label = widget_key_for(
        {"kind": "check", "label": "Nonstop", "role": "checkbox"}, {"overlay": "Stops"},
    )
    assert key == control_key("check", "Nonstop", "checkbox")
    assert not is_modal_key(key)
    assert label == "Nonstop"
    # Key presses carry no target: they key by the open dialog.
    key, label = widget_key_for({"kind": "key", "label": "Pressed Enter"}, {"overlay": "Stops"})
    assert key == modal_key("Stops") and is_modal_key(key)
    assert label == "Stops"
    # No element and no dialog: untrackable.
    assert widget_key_for({"kind": "key"}, {"overlay": ""}) == (None, "")


def _classic_agent():
    agent = loop.Agent.__new__(loop.Agent)
    agent.trace = Trace(run_id="test-widget-stall", level=0)
    agent._widget_stall = WidgetStallTracker(after=3)
    return agent


def _classic_state(fingerprint="f1", actions=(), overlay=""):
    return {
        "page": {"fingerprint": fingerprint, "actions": list(actions), "overlay": overlay},
        "held_actions": {},
        "hold_reasons": {},
        "event_notes": [],
        "history": [],
    }


def test_classic_hook_holds_then_marks_unmet():
    agent = _classic_agent()
    action = {"kind": "click", "label": "Nonstop", "role": "checkbox", "node": 7}
    page = {"fingerprint": "f1", "overlay": ""}
    verify = {"success": False}
    state = _classic_state()
    for step in range(1, 4):
        agent._widget_stall_hook(state, action, page, verify, False, "click", step)
    key = control_key("click", "Nonstop", "checkbox")
    assert state["held_actions"].get(key) == loop.STALL_HOLD_STEPS
    assert "did not respond" in state["event_notes"][-1]["text"]
    for step in range(4, 7):
        agent._widget_stall_hook(state, action, page, verify, False, "keyboard", step)
    assert state["held_actions"].get(key) == RUN_HOLD_STEPS
    assert state["unmet_widgets"] == [{"key": key, "label": "Nonstop", "step": 6}]
    assert "marked unmet" in state["event_notes"][-1]["text"]


def test_classic_hook_ignores_verified_and_untracked():
    agent = _classic_agent()
    action = {"kind": "click", "label": "Next", "node": 3}
    page = {"fingerprint": "f1", "overlay": ""}
    state = _classic_state()
    agent._widget_stall_hook(state, action, page, {"success": True}, True, "click", 1)
    agent._widget_stall_hook(state, action, page, {"success": False}, False, "click", 2)
    agent._widget_stall_hook(state, action, page, {"success": False}, False, "click", 3)
    assert state["held_actions"] == {}
    assert state["event_notes"] == []
    scroller = {"kind": "scroll", "label": "Results", "node": 9}
    for step in range(4, 8):
        agent._widget_stall_hook(state, scroller, page, {"success": False}, False, "scroll", step)
    assert state["held_actions"] == {}


def test_classic_hook_modal_keys_note_without_hold():
    agent = _classic_agent()
    action = {"kind": "key", "key": "Enter"}
    page = {"fingerprint": "f1", "overlay": "Stops"}
    state = _classic_state(overlay="Stops")
    for step in range(1, 4):
        agent._widget_stall_hook(state, action, page, {"success": False}, False, "keyboard", step)
    assert state["held_actions"] == {}
    assert "close and reopen" in state["event_notes"][-1]["text"]
    for step in range(4, 7):
        agent._widget_stall_hook(state, action, page, {"success": False}, False, "keyboard", step)
    assert state["unmet_widgets"][0]["label"] == "Stops"


def test_classic_hook_resets_on_navigation():
    agent = _classic_agent()
    action = {"kind": "click", "label": "Next", "node": 3}
    page = {"fingerprint": "f1", "overlay": ""}
    state = _classic_state()
    state["page"]["url"] = "https://a.test/"
    for step in range(1, 4):
        agent._widget_stall_hook(state, action, page, {"success": False}, False, "click", step)
    assert "did not respond" in state["event_notes"][-1]["text"]
    # Same label on a new page: a stranger's levels must not leak.
    state["page"]["url"] = "https://b.test/"
    state["event_notes"] = []
    for step in range(4, 6):
        agent._widget_stall_hook(state, action, page, {"success": False}, False, "click", step)
    assert state["event_notes"] == []
    agent._widget_stall_hook(state, action, page, {"success": False}, False, "click", 6)
    assert "did not respond" in state["event_notes"][-1]["text"]
    assert "marked unmet" not in state["event_notes"][-1]["text"]


def test_classic_hook_suggests_typing_for_fill_twins():
    agent = _classic_agent()
    action = {"kind": "click", "label": "Max price", "node": 11}
    page = {"fingerprint": "f1", "overlay": ""}
    twin = {"kind": "fill", "label": "Max price", "node": 11}
    state = _classic_state(actions=[twin])
    for step in range(1, 4):
        agent._widget_stall_hook(state, action, page, {"success": False}, False, "click", step)
    assert "type the value directly" in state["event_notes"][-1]["text"]
