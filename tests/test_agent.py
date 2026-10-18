"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import logging
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, bind_navigate, browser_operation, fingerprint, validate_navigate_url


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_pressed_state_and_history_note_reach_the_model():
    actions = [{
        "id": "e7", "kind": "click", "label": "November 16", "role": "button",
        "node": 9, "pressed": "true", "value": "",
    }]
    elements, targets, _controls = model.action_space(actions)
    assert elements[0]["pressed"] == "true"
    assert targets["CLICK"]["1"]["pressed"] == "true"
    description = model.action_description(actions[0], "CLICK", "1")
    assert description["pressed"] == "true"
    assert description["effect"] == "toggle this control"
    recent = model.recent_actions([{"action": "a", "note": "value unchanged"}])
    assert recent[0]["note"] == "value unchanged"
    failed = model.recent_actions([{"action": "a", "note": "text entry failed"}])
    assert failed[0]["note"] is None


def _loop_step(operation, label, url="u", title="t", progress=False):
    return {
        "key": (operation, label, url, title),
        "id": label,
        "label": label,
        "progress": progress,
    }


def test_detect_cycle_finds_ab_and_abc_without_false_positives():
    from jev_ultrafast.agent import detect_cycle

    ab = [_loop_step("CLICK", "A"), _loop_step("CLICK", "B")] * 2
    assert detect_cycle(ab)["labels"] == ["A", "B"]
    abc = [_loop_step("CLICK", name) for name in ("A", "B", "C")] * 2
    assert detect_cycle(abc)["labels"] == ["A", "B", "C"]
    assert detect_cycle([_loop_step("CLICK", "A"), _loop_step("CLICK", "B")]) is None
    progress = [
        _loop_step("CLICK", "A"),
        _loop_step("CLICK", "B", progress=True),
        _loop_step("CLICK", "A"),
        _loop_step("CLICK", "B"),
    ]
    assert detect_cycle(progress) is None
    moved = [
        _loop_step("CLICK", "A"),
        _loop_step("CLICK", "B"),
        _loop_step("CLICK", "A", url="u2"),
        _loop_step("CLICK", "B"),
    ]
    assert detect_cycle(moved) is None


def test_loop_detector_fires_twice_then_blocks(runner, monkeypatch):
    ids = ["e2", "e3"]

    def click(index):
        return {**decision(ids[index % 2]), "operation": "CLICK"}

    monkeypatch.setattr(loop, "choose", lambda *_args, **_kwargs: click(len(runner.state["history"])))
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["goal"])
    monkeypatch.setattr(
        loop,
        "judge_progress",
        lambda context, system: (
            {"satisfied": [], "facts": [], "complete": True, "missing": ""},
            {"model": "t", "latency_ms": 1, "usage": {}},
        ),
    )
    rerenders = {"n": 0}

    def rerender(*_args, **_kwargs):
        rerenders["n"] += 1
        return {**page(), "fingerprint": f"fp{rerenders['n']}"}

    runner.state["browser"].fresh.return_value = True
    runner.state["browser"].observe.side_effect = rerender
    runner.state["browser"].act.return_value = {"executed": "x"}
    for _ in range(12):
        runner.command("tick")
        if runner.state["status"] == "blocked":
            break
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "loop detected"
    assert runner.state["loop_fires"] >= 3
    assert any("loop detected" in (h.get("note") or "") for h in runner.state["history"])


def _click_step(label, progress=False):
    return {
        "key": ("CLICK", label, "u", "t"),
        "id": label, "label": label, "kind": "click", "role": "button",
        "progress": progress,
    }


def test_pair_key_groups_inverse_labels():
    from jev_ultrafast.agent import pair_key

    assert pair_key("Add adult")[:2] == ("adult", 0)
    assert pair_key("Remove adult")[:2] == ("adult", 0)
    assert pair_key("Add adult")[2] != pair_key("Remove adult")[2]
    assert pair_key("+")[:2] == pair_key("−")[:2]  # unicode minus pairs with plus
    assert pair_key("Search") is None


def test_detect_oscillation_rules():
    from jev_ultrafast.agent import detect_oscillation

    found = detect_oscillation([_click_step(label) for label in ("Add adult", "Remove adult") * 3])
    assert found["object"] == "adult"
    assert found["flips"] == 5
    assert set(found["labels"]) == {"Add adult", "Remove adult"}
    assert len(found["keys"]) == 2
    symbols = detect_oscillation([_click_step(label) for label in ("+", "−") * 3])
    assert symbols["object"] == ""
    assert symbols["flips"] == 5
    one_sided = [_click_step("Add adult")] * 5 + [_click_step("Remove adult")]
    assert detect_oscillation(one_sided) is None  # one flip only
    progressed = [
        _click_step(label, progress=(index == 2))
        for index, label in enumerate(("Add adult", "Remove adult") * 3)
    ]
    assert detect_oscillation(progressed) is None
    scattered = [_click_step(label) for label in ("Add adult", "Search", "Go", "Wait", "Add adult", "Search")]
    assert detect_oscillation(scattered) is None  # shared by 2 of 6 only
    scrolls = {"key": ("SCROLL", "Scroll down", "u", "t"), "label": "Scroll down",
               "kind": "scroll", "progress": False}
    few_clicks = [_click_step(label) for label in ("Add adult", "Remove adult", "Add adult")]
    assert detect_oscillation(few_clicks + [scrolls] * 3) is None  # scrolls are not clicks


def test_oscillation_hold_and_note_in_predict(runner, monkeypatch):
    monkeypatch.setattr(loop, "choose", lambda *_args, **_kwargs: decision("e3"))
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["goal"])
    runner.state["page"]["actions"].extend([
        {"id": "e4", "kind": "click", "label": "Add adult", "role": "button", "value": "2", "node": 30},
        {"id": "e5", "kind": "click", "label": "Remove adult", "role": "button", "value": "2", "node": 31},
        {"id": "e6", "kind": "click", "label": "Done", "role": "button", "node": 32},
    ])
    labels = ("Add adult", "Add adult", "Remove adult", "Add adult", "Remove adult", "Remove adult")
    runner.state["loop_window"] = [_click_step(label) for label in labels]
    runner.command("predict", {})
    held = runner.state["held_actions"] or {}
    add = loop.noop_key({"kind": "click", "label": "Add adult", "role": "button"})
    remove = loop.noop_key({"kind": "click", "label": "Remove adult", "role": "button"})
    assert add in held and remove in held
    assert runner.state["hold_reasons"][add] == "oscillation hold"
    note = runner.state["event_notes"][-1]["text"]
    assert "alternated" in note and "Current value: 2" in note and "Done" in note


def test_detect_pingpong_rules():
    from jev_ultrafast.agent import detect_pingpong

    assert detect_pingpong(["a", "b"] * 2) == {"pair": ["a", "b"]}
    assert detect_pingpong(["a", "b"] * 3) == {"pair": ["a", "b"]}
    assert detect_pingpong(["a", "a", "b", "b"]) is None
    assert detect_pingpong(["a", "b", "c"]) is None
    assert detect_pingpong(["a", "b", "a", "c"]) is None


def test_pingpong_excludes_both_urls_once(runner, monkeypatch):
    monkeypatch.setattr(loop, "choose", lambda *_args, **_kwargs: decision("e3"))
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["goal"])
    runner.state["visited_urls"] = ["a.test/x", "b.test/y"] * 2
    runner.command("predict", {})
    assert set(runner.state["failed_urls"]) == {"a.test/x", "b.test/y"}
    assert any("somewhere new" in note["text"] for note in runner.state["event_notes"])
    notes_before = len(runner.state["event_notes"])
    runner.command("predict", {})
    assert len(runner.state["event_notes"]) == notes_before  # no refire without new visits


def test_scroll_watch_rules():
    from jev_ultrafast.agent import SCROLL_EXHAUSTED_COUNT, scroll_exhausted, update_scroll_watch

    watch = None
    for _ in range(SCROLL_EXHAUSTED_COUNT - 1):
        watch = update_scroll_watch(watch, "p", 0, 0)
    assert scroll_exhausted(watch, 0, 0) is False
    watch = update_scroll_watch(watch, "p", 0, 0)
    assert scroll_exhausted(watch, 0, 0) is True
    assert scroll_exhausted(watch, 1, 0) is False  # a new fact excuses the streak
    assert scroll_exhausted(watch, 0, 1) is False  # sub-goal progress excuses it
    assert update_scroll_watch(watch, "p", 1, 0)["count"] == 1  # new facts restart
    assert update_scroll_watch(watch, "other", 0, 0)["count"] == 1  # a new page restarts


def test_scroll_exhaustion_holds_scrolls_in_act(runner):
    scroll = {"id": "s1", "kind": "scroll", "label": "Scroll down page", "role": None, "node": 9}
    runner.state["page"]["actions"].append(scroll)
    runner.state["scroll_watch"] = {
        "key": loop.normalize_link_url(runner.state["page"]["url"]),
        "count": 3, "memo": 0, "satisfied": 0,
    }
    runner.state["decision"] = {**decision("s1"), "operation": "SCROLL_DOWN"}
    runner.state["browser"].act.return_value = {"executed": "s1"}
    runner.state["browser"].observe.return_value = {**runner.state["page"], "fingerprint": "fp2"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert loop.noop_key(scroll) in (runner.state["held_actions"] or {})
    assert any("exhausted" in note["text"] for note in runner.state["event_notes"])


def test_stem_strips_plurals():
    from jev_ultrafast.agent import _stem

    assert _stem("filters") == "filter"
    assert _stem("cities") == "city"
    assert _stem("passes") == "pass"
    assert _stem("churches") == "church"
    assert _stem("houses") == "house"
    assert _stem("class") == "class"
    assert _stem("gas") == "gas"
    assert _stem("plus") == "plus"


def test_guidance_ranks_buttons_and_stems_plurals():
    from jev_ultrafast.agent import compose_guidance

    state = {
        "history": [], "event_notes": [],
        "subgoals": [{"id": 0, "text": "set the filters", "required": True}],
        "page": {"actions": [
            {"id": "e1", "kind": "click", "role": "button", "label": "Filter"},
            {"id": "e2", "kind": "click", "role": "link", "label": "Help"},
        ]},
    }
    guidance = compose_guidance(state)
    assert any("Filter" in line and "consider" in line for line in guidance)


def test_done_runs_the_judge_on_demand(runner, monkeypatch):
    calls = []

    def fake_judge(context, system):
        calls.append(system)
        return (
            {"satisfied": [{"id": 0, "value": "9M"}], "facts": [], "complete": True, "missing": ""},
            {"model": "t", "latency_ms": 1, "usage": {}},
        )

    monkeypatch.setattr(loop, "judge_progress", fake_judge)
    runner.state["subgoals"] = [{"id": 0, "text": "population", "required": True}]
    runner.state["judged_url"] = runner.state["page"]["url"]  # the throttle would skip
    runner.state["judged_step"] = 99
    verdict = runner._done_verdict(runner.state, runner.state["page"], 1)
    assert calls  # judged despite the throttle
    assert verdict["accept"] is True


def test_unblock_scroll_emits_action_and_verify(runner):
    events = []
    runner.trace.emit = lambda event, **fields: events.append((event, fields))
    scroll = {"id": "scroll_down", "kind": "scroll", "label": "Scroll down page", "node": 7}
    runner.state["page"]["actions"].append(scroll)
    runner.state["browser"].act.return_value = {"executed": "scroll_down"}
    runner.state["browser"].observe.return_value = {**runner.state["page"], "fingerprint": "fp2"}
    assert runner._unblock_scroll(runner.state, runner.state["page"], decision("BLOCKED")) is True
    by_kind = {event: fields for event, fields in events}
    assert by_kind["action"]["strategy"] == "unblock"
    assert by_kind["verify"]["kind"] == "scroll"
    assert by_kind["verify"]["success"] is True


def test_decision_budget_stops_cleanly(runner, monkeypatch):
    ids = ["e2", "e3"]
    monkeypatch.setattr(
        loop, "choose",
        lambda *_args, **_kwargs: {**decision(ids[len(runner.state["history"]) % 2]), "operation": "CLICK"},
    )
    runner.config = {"max_decisions": 3}
    rerenders = {"n": 0}

    def rerender(*_args, **_kwargs):
        rerenders["n"] += 1
        return {**page(), "fingerprint": f"fp{rerenders['n']}"}

    runner.state["browser"].fresh.return_value = True
    runner.state["browser"].observe.side_effect = rerender
    runner.state["browser"].act.return_value = {"executed": "x"}
    for _ in range(6):
        runner.command("tick")
        if runner.state["status"] == "blocked":
            break
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "decision budget"
    assert len(runner.state["decisions"]) == 3


def test_invalid_decision_retries_once_then_blocks(runner, monkeypatch):
    calls = {"n": 0}

    def flaky(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError(model.INVALID_RESPONSE)
        return {**decision("e3"), "operation": "CLICK"}

    monkeypatch.setattr(loop, "choose", flaky)
    runner.state["status"] = "ready"
    runner.command("predict")
    assert runner.state["status"] == "predicted"
    assert calls["n"] == 2
    monkeypatch.setattr(loop, "choose", Mock(side_effect=ValueError("boom")))
    with pytest.raises(ValueError, match="boom"):
        runner.command("predict")
    monkeypatch.setattr(loop, "choose", Mock(side_effect=ValueError(model.INVALID_RESPONSE)))
    runner.command("predict")
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "invalid decision response"


def test_navigate_url_gate(monkeypatch):
    monkeypatch.delenv("JEV_ALLOW_LOCAL", raising=False)
    assert validate_navigate_url("https://en.wikipedia.org/wiki/X") == "https://en.wikipedia.org/wiki/X"
    assert validate_navigate_url("HTTPS://Example.COM/") == "HTTPS://Example.COM/"
    for bad in ("http://example.test/", "file:///etc/passwd", "chrome://settings",
                "javascript:alert(1)", "data:text/plain,x", "https://", "", "not a url",
                "http://127.0.0.1:8766/fixture.html", "http://localhost/"):
        with pytest.raises(ValueError, match="Refusing to navigate"):
            validate_navigate_url(bad)
    monkeypatch.setenv("JEV_ALLOW_LOCAL", "1")
    assert validate_navigate_url("http://127.0.0.1:8766/fixture.html") == "http://127.0.0.1:8766/fixture.html"
    with pytest.raises(ValueError, match="Refusing to navigate"):
        validate_navigate_url("http://example.test/")


def test_navigate_offered_on_every_page_exactly_once():
    # Multi-site goals must leave the current page, so the blank-only rule is gone.
    blank = page()
    blank["url"] = "about:blank"
    bound = bind_navigate(blank)
    assert [a["id"] for a in bound["actions"] if a.get("kind") == "navigate"] == ["navigate"]
    assert bind_navigate(bound)["actions"].count(bound["actions"][-1]) == 1
    loaded = bind_navigate(page())
    assert [a["id"] for a in loaded["actions"] if a.get("kind") == "navigate"] == ["navigate"]


def test_navigate_act_uses_model_url(runner, monkeypatch):
    from jev_ultrafast.questions import NAVIGATE_VALUE

    seen = {}
    action = {"id": "navigate", "kind": "navigate", "label": "Navigate to a URL from the goal"}
    runner.state["page"]["actions"] = [action, {"id": "wait", "kind": "wait", "label": "Wait"}]
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])

    def fake_field_text(context, system=None):
        seen["system"] = system
        return "https://example.test/start", {"model": "test", "latency_ms": 3}

    monkeypatch.setattr(loop, "field_text", fake_field_text)
    runner.state["browser"].navigate = Mock(return_value={"executed": "navigate"})
    runner.state["decision"] = {**decision("navigate"), "operation": "NAVIGATE"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert seen["system"] == NAVIGATE_VALUE
    runner.state["browser"].navigate.assert_called_once_with("https://example.test/start")
    assert runner.state["history"][-1]["text"] == "https://example.test/start"
    assert runner.state["status"] == "ready"


def test_navigate_act_skips_invalid_url(runner, monkeypatch):
    action = {"id": "navigate", "kind": "navigate", "label": "Navigate to a URL from the goal"}
    runner.state["page"]["actions"] = [action, {"id": "wait", "kind": "wait", "label": "Wait"}]
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])
    monkeypatch.setattr(
        loop, "field_text", lambda context, system=None: ("file:///etc/passwd", {"model": "t"})
    )
    runner.state["browser"].navigate = Mock(side_effect=ValueError("Refusing to navigate"))
    runner.state["decision"] = {**decision("navigate"), "operation": "NAVIGATE"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["note"] == "invalid navigation URL"
    assert runner.state["status"] == "ready"


def test_control_unchanged_rules():
    from jev_ultrafast.agent import control_unchanged

    field = {"kind": "click", "label": "Open Departure", "value": "Mon, Nov 16"}
    same = {"actions": [{"kind": "click", "label": "Open Departure", "value": "Mon, Nov 16"}]}
    assert control_unchanged(field, {}, same) == "value unchanged"
    cell = {"kind": "click", "label": "Nov 16", "pressed": "true"}
    still = {"actions": [{"kind": "click", "label": "Nov 16", "pressed": "true"}]}
    assert control_unchanged(cell, {}, still) == "already selected"
    moved = {"actions": [{"kind": "click", "label": "Open Departure", "value": ""}]}
    assert control_unchanged(field, {}, moved) is None
    assert control_unchanged(field, {}, {"actions": []}) is None
    assert control_unchanged({"kind": "fill", "label": "Open Departure", "value": "x"}, {}, same) is None


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_invalid_decision_logs_redacted_body(monkeypatch, caplog):
    def post(_url, _key, _body):
        return {
            "model": "test",
            "answers": {"operation": {"choice": "CLICK", "leaked": "fake-test-key-12345678"}},
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-test-key-12345678")
    monkeypatch.setattr(model, "post_json", post)
    with caplog.at_level(logging.WARNING, logger="jev"):
        with pytest.raises(ValueError, match="Invalid TypeSafe"):
            model.choose(page(), "Find a book", [])
    assert "[jev:decision] invalid response body=" in caplog.text
    assert "fake-test-key-12345678" not in caplog.text
    assert "[redacted]" in caplog.text


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("INCEPTION_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    from jev_ultrafast.trace import Trace, get_logger

    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.blocked_streak = 0
    a.log = get_logger("agent")
    a.trace = Trace(run_id="test-run", level=0)
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_blocked_choice_reobserves_instead_of_stopping(runner):
    runner.state["decision"] = {**decision("BLOCKED"), "operation": "BLOCKED"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    assert runner.blocked_streak == 1
    runner.state["browser"].settled.assert_called_once()


def test_blocked_stops_after_three_retries(runner):
    runner.blocked_streak = 3
    runner.state["decision"] = {**decision("BLOCKED"), "operation": "BLOCKED"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    runner.state["browser"].settled.assert_not_called()


def test_closed_shadow_strike_explains_blocked(runner):
    action = next(item for item in runner.state["page"]["actions"] if item["id"] == "e3")
    action["possible_closed_shadow"] = True
    for _ in range(3):
        runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "possible closed shadow root"
    assert runner.state["history"][-1]["possible_closed_shadow"] is True


def test_no_progress_stop_has_no_shadow_reason(runner):
    for _ in range(3):
        runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert "blocked_reason" not in runner.state


def test_new_tab_is_recorded_on_the_action(runner):
    class Tabs:
        def __init__(self):
            browser = runner.state["browser"]
            self.fresh = browser.fresh
            self.act = browser.act
            self.observe = browser.observe
            self.wait_until_ready = browser.wait_until_ready
            self.settled = browser.settled

        def collect_new_tabs(self):
            return ["t2"]

        def activate(self, key):
            assert key == "t2"
            return {"url": "https://example.test/popup"}

    runner.state["browser"] = Tabs()
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["note"] == "switched to new tab https://example.test/popup"


def test_unmatched_option_does_not_mutate(runner, monkeypatch):
    runner.state["page"]["actions"].append({
        "id": "e9", "kind": "select_option", "node": 4, "label": "Country", "role": "combobox", "value": "",
    })
    runner.state["decision"] = {
        "choice": "e9", "operation": "SELECT_OPTION", "target": "1", "confidence": 1,
        "probabilities": {"e9": 1}, "latency_ms": 1, "usage": {},
    }
    monkeypatch.setattr(loop, "field_text", lambda _context: ("Nowhere", {"model": "t", "latency_ms": 1}))
    runner.state["browser"].read_options.return_value = [{"label": "Canada", "value": "ca"}]
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["history"][-1]["note"] == "no matching option"
    assert runner.pending_text is not None


def test_text_mismatch_is_recorded_without_another_attempt(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("book", {"model": "t", "latency_ms": 1})))
    runner.state["browser"].act.return_value = {"executed": "e1", "text_verified": False, "typed": "Z"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["note"] == "text entry failed"
    assert runner.state["browser"].act.call_count == 1


def test_same_date_fill_names_both_sides(runner, monkeypatch):
    monkeypatch.setattr(
        loop, "field_text", Mock(return_value=("June 12 2027", {"model": "t", "latency_ms": 1}))
    )
    runner.state["browser"].act.return_value = {
        "executed": "e1", "text_verified": True, "typed": "Sat, Jun 12", "same_date": True,
    }
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["note"] == "field shows Sat, Jun 12 for June 12 2027 (same date)"


def test_fill_already_set_skips_the_mutation_and_holds(runner, monkeypatch):
    runner.state["page"]["actions"].append(
        {"id": "e5", "kind": "fill", "label": "Departure", "value": "Mon, Nov 16", "node": 30}
    )
    monkeypatch.setattr(loop, "field_text", Mock(return_value=("Mon, Nov 16", {"model": "t", "latency_ms": 1})))
    runner.state["decision"] = {
        "choice": "e5", "operation": "TYPE_TEXT", "target": "5", "confidence": 1,
        "probabilities": {"e5": 1}, "latency_ms": 1, "usage": {},
    }
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["history"][-1]["note"] == "value already set"
    assert runner.state["history"][-1]["page_changed"] is False
    assert runner.state["held_actions"][loop.noop_key({"kind": "fill", "label": "Departure"})] == 2
    assert runner.state["status"] == "ready"


def test_click_with_unchanged_value_is_held(runner):
    runner.state["page"]["actions"].append(
        {"id": "e6", "kind": "click", "label": "Open Departure", "value": "Mon, Nov 16", "node": 40}
    )
    runner.state["decision"] = {
        "choice": "e6", "operation": "CLICK", "target": "6", "confidence": 1,
        "probabilities": {"e6": 1}, "latency_ms": 1, "usage": {},
    }
    after = page()
    after["fingerprint"] = "changed"
    after["actions"].append(
        {"id": "e9", "kind": "click", "label": "Open Departure", "value": "Mon, Nov 16", "node": 50}
    )
    runner.state["browser"].observe.return_value = after
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_called_once()
    assert runner.state["history"][-1]["note"] == "value unchanged"
    assert runner.state["held_actions"][loop.noop_key({"kind": "click", "label": "Open Departure"})] == 2
    # The hold survives the id shift: the same control reappears as e9.
    assert "e9" not in [a["id"] for a in loop.offered_actions(after["actions"], runner.state["held_actions"])]


def test_executed_action_resets_blocked_streak(runner):
    runner.blocked_streak = 2
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.blocked_streak == 0
    runner.state["browser"].wait_until_ready.assert_called_once()


def test_wait_until_ready_returns_immediately_when_complete():
    import jev_ultrafast.browser as browser

    tab = browser.Browser.__new__(browser.Browser)
    tab.evaluate = Mock(return_value="complete")
    assert tab.wait_until_ready() is True
    tab.evaluate.assert_called_once()


def test_wait_until_ready_gives_up_after_timeout():
    import jev_ultrafast.browser as browser

    tab = browser.Browser.__new__(browser.Browser)
    tab.evaluate = Mock(return_value="loading")
    assert tab.wait_until_ready(timeout=0) is False


def test_settled_requires_two_matching_snapshots(monkeypatch):
    import jev_ultrafast.browser as browser

    tab = browser.Browser.__new__(browser.Browser)
    tab.evaluate = Mock(return_value="complete")
    tab.observe = Mock(side_effect=[{"fingerprint": "b"}, {"fingerprint": "b"}, {"fingerprint": "b"}])
    monkeypatch.setattr(browser.time, "sleep", lambda *_a: None)
    assert tab.settled({"fingerprint": "a"}) is True
    assert tab.observe.call_count == 3


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    b.settle_dom = Mock()
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import TRIP_ISO, TRIP_LONG, TRIP_SHORT, verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": f"Track prices from Zürich to London departing {TRIP_ISO}",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", TRIP_SHORT),
                (f"Nonstop flight on {TRIP_LONG}. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace(TRIP_ISO[:4], "1999")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def test_stale_target_retargets_without_a_hold_or_new_decision(runner, monkeypatch):
    """A stale target is the same control under a shifted id: re-observe, retarget,
    and execute in the same tick. No stale hold, no new decision consumed."""
    monkeypatch.setattr(loop, "choose", lambda *_args, **_kwargs: decision("e3"))
    runner.state["browser"].fresh.return_value = True
    shifted = page()
    shifted["actions"] = [
        {**a, "id": f"n{i}"} if a["id"].startswith("e") else a
        for i, a in enumerate(shifted["actions"])
    ]
    runner.state["browser"].observe.side_effect = [shifted, shifted]
    runner.state["browser"].act.side_effect = [
        StalePage("Target changed or is covered. Observe again."),
        {"executed": "n2"},
    ]
    runner.command("tick")
    assert "stale hold" not in (runner.state.get("hold_reasons") or {}).values()
    assert runner.state["history"][-1]["choice"] == "n2"
    assert len(runner.state["decisions"]) == 1


def test_stale_ambiguous_target_redecides_without_a_hold(runner, monkeypatch):
    """Two identical controls after a re-render: no unique retarget, so the run
    re-decides next tick instead of guessing. Still no stale hold."""
    monkeypatch.setattr(loop, "choose", lambda *_args, **_kwargs: decision("e3"))
    runner.state["browser"].fresh.return_value = True
    twin = page()
    go = next(a for a in twin["actions"] if a["id"] == "e3")
    twin["actions"] = [go, {**go, "id": "e3b"}]
    twin["fingerprint"] = fingerprint(twin)
    runner.state["browser"].observe.side_effect = [twin, twin]
    runner.state["browser"].act.side_effect = StalePage("Target changed or is covered. Observe again.")
    runner.command("tick")
    assert runner.state.get("held_actions", {}) == {}
    assert runner.state["history"] == []
    assert runner.state["status"] == "ready"


def test_debug_log_reports_tokens_per_second(monkeypatch, caplog):
    def post(_url, _key, body):
        return {
            "model": "test",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "SECRETKEY123")
    monkeypatch.setenv("JEV_DEBUG", "2")
    monkeypatch.setattr(model, "post_json", post)
    with caplog.at_level(logging.DEBUG, logger="jev"):
        model.choose(page(), "Find a book", [])
    assert "[jev:typesafe]" in caplog.text
    assert "prompt=100 completion=50 tok/s=" in caplog.text
    assert "operation=TYPE_TEXT target=1 choice=e1" in caplog.text
    assert "SECRETKEY123" not in caplog.text


def test_debug_log_parses_gateway_usage_keys(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("JEV_DEBUG", "1")
    monkeypatch.setattr(
        model,
        "post_json",
        Mock(
            return_value={
                "choices": [{"message": {"content": '{"text":"Zurich"}'}}],
                "usage": {"inputTokens": 200, "outputTokens": 25},
            }
        ),
    )
    with caplog.at_level(logging.DEBUG, logger="jev"):
        model.field_text({"goal": "Find a flight"})
    assert "[jev:text]" in caplog.text
    assert "prompt=200 completion=25 tok/s=" in caplog.text
    assert "value='Zurich'" in caplog.text


def test_debug_log_quiet_by_default(monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.delenv("JEV_DEBUG", raising=False)
    monkeypatch.setattr(
        model,
        "post_json",
        Mock(return_value={"model": "test", "answers": {"operation": choice(["DONE", "BLOCKED"], "DONE")}}),
    )
    model.choose({"url": "", "title": "", "text": "", "actions": []}, "Done", [])
    assert capsys.readouterr().err == ""


def test_debug_verbose_dumps_request_and_response(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("JEV_DEBUG", "2")
    monkeypatch.setattr(
        model, "post_json", Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    )
    with caplog.at_level(logging.DEBUG, logger="jev"):
        model.field_text({"goal": "Find a flight"})
    assert "[jev:text] request:" in caplog.text
    assert "[jev:text] response:" in caplog.text
    assert "sk-" not in caplog.text and "Bearer" not in caplog.text


def test_debug_log_covers_rejected_values(monkeypatch, caplog):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("JEV_DEBUG", "1")
    monkeypatch.setattr(
        model, "post_json", Mock(return_value={"choices": [{"message": {"content": '{"text":null}'}}]})
    )
    with caplog.at_level(logging.DEBUG, logger="jev"):
        with pytest.raises(ValueError, match="nothing typed"):
            model.field_text({"goal": "Find a flight"})
    assert "error=no valid field value" in caplog.text


def test_text_max_tokens_override(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL_MAX_TOKENS", "4096")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    model.field_text({"goal": "Find a flight"})
    assert post.call_args.args[2]["max_tokens"] == 4096


@pytest.mark.parametrize("flag,background,front", [("1", False, True), ("", True, False)])
def test_visible_tab_flag_controls_foreground(monkeypatch, flag, background, front):
    import jev_ultrafast.browser as browser

    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            return {"result": {"value": "complete"}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    monkeypatch.setattr(browser, "ensure_daemon", lambda: None)
    if flag:
        monkeypatch.setenv("JEV_VISIBLE_TAB", flag)
    else:
        monkeypatch.delenv("JEV_VISIBLE_TAB", raising=False)
    tab = browser.Browser("https://example.test/")
    tab.close()
    create = next(params for method, params in calls if method == "Target.createTarget")
    assert create["background"] is background
    assert ("Page.bringToFront" in [method for method, _ in calls]) is front


def _spread(ids, selected, mass=0.7):
    others = [item for item in ids if item != selected]
    probabilities = {selected: mass if others else 1.0}
    if others:
        share = (1 - mass) / len(others)
        probabilities.update({item: share for item in others})
    probabilities[selected] += 1 - sum(probabilities.values())
    return probabilities


def _mercury_decision(body, selected, noul=0.1):
    criteria = body["questions"]["next_action"]["criteria"]
    probabilities = _spread(criteria, selected)
    answers = {
        "next_action": {
            "type": "choice",
            "choice": selected,
            "confidence": probabilities[selected],
            "probabilities": probabilities,
        }
    }
    for name in body["questions"]:
        if name.startswith("req_"):
            answers[name] = {"type": "noul", "noul": noul}
    return {"model": "mercury-decide", "answers": answers, "usage": {"input_tokens": 11, "output_tokens": 4}}


def _checker(page_state):
    return {
        "id": "e9",
        "kind": "click",
        "label": "Refundable",
        "role": "checkbox",
        "checked": "false",
        "value": "on",
        "node": 7,
        **page_state,
    }


def test_unchanged_action_is_hidden_for_two_steps():
    from jev_ultrafast.agent import decay_holds, note_no_progress, offered_actions, reload_like

    actions = [
        {"id": "e31", "kind": "click", "label": "Reload"},
        {"id": "e22", "kind": "click", "label": "Search"},
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]
    holds = note_no_progress({}, actions[0], page_changed=False)
    assert [a["id"] for a in offered_actions(actions, holds)] == ["e22", "wait"]
    holds = decay_holds(holds)
    assert "e31" not in [a["id"] for a in offered_actions(actions, holds)]
    holds = decay_holds(holds)
    assert [a["id"] for a in offered_actions(actions, holds)] == ["e31", "e22", "wait"]
    assert note_no_progress({}, actions[0], page_changed=True) == {}
    assert note_no_progress({}, actions[2], page_changed=False) == {}
    assert reload_like({"label": "Reload"})
    assert reload_like({"label": "Refresh the list"})
    assert not reload_like({"label": "Search"})
    assert not reload_like({"label": "Preloaded content"})


def test_reload_without_change_waits_before_the_next_decision(monkeypatch, runner):
    runner.state["page"]["fingerprint"] = "fp1"
    runner.state["page"]["actions"].append({"id": "e31", "kind": "click", "label": "Reload", "node": 9})
    runner.state["decision"] = {
        "choice": "e31",
        "operation": "CLICK",
        "target": "9",
        "confidence": 1,
        "probabilities": {"e31": 1},
        "latency_ms": 1,
        "usage": {},
    }
    same = {**runner.state["page"], "fingerprint": "fp1"}
    runner.state["browser"].observe.return_value = same
    runner.state["browser"].wait_for_refresh.return_value = same
    runner.state["browser"].wait_until_ready.return_value = True
    runner.command("act", {"fingerprint": "fp1"})
    runner.state["browser"].wait_for_refresh.assert_called_once()
    assert runner.state["history"][-1]["page_changed"] is False
    assert runner.state["held_actions"][loop.noop_key({"kind": "click", "label": "Reload"})] == 2


def test_invalid_text_retries_once_at_low_effort_then_succeeds(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL", "mercury-2.5")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "instant")
    efforts = []
    contents = iter(["not json", '{"text":"Zurich"}'])

    def post(_url, _key, body):
        efforts.append(body["reasoning_effort"])
        return {"choices": [{"message": {"content": next(contents)}}], "usage": {}}

    monkeypatch.setattr(model, "post_json", post)
    assert model.field_text({"goal": "Find a flight"})[0] == "Zurich"
    assert efforts == ["instant", "low"]


def test_invalid_text_twice_fails_cleanly(monkeypatch, caplog, runner):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL", "mercury-2.5")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "instant")
    efforts = []

    def post(_url, _key, body):
        efforts.append(body["reasoning_effort"])
        return {"choices": [{"message": {"content": "still not json"}}], "usage": {}}

    monkeypatch.setattr(model, "post_json", post)
    with caplog.at_level(logging.WARNING, logger="jev"):
        with pytest.raises(ValueError, match="nothing typed"):
            model.field_text({"goal": "Find a flight"})
    assert efforts == ["instant", "low"]
    assert "error=no valid field value; step skipped" in caplog.text
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["status"] == "ready"
    assert runner.state["history"][-1]["note"] == "text helper failed"
    assert runner.state["history"][-1]["page_changed"] is False


def test_unset_provider_keeps_the_typesafe_request(monkeypatch):
    monkeypatch.delenv("DECISION_PROVIDER", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-secret-value")
    seen = {}

    def post(url, key, body):
        seen["url"] = url
        seen["body"] = body
        return {
            "model": "jev-latest",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(body["questions"]["click_target"]["criteria"], "2"),
            },
            "usage": {},
        }

    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(page(), "Find a book", [])
    assert seen["url"].endswith("/v1/systemone")
    assert "next_action" not in seen["body"]["questions"]
    sample = next(iter(seen["body"]["questions"]["click_target"]["criteria"].values()))
    assert "current_value" in sample
    assert decision["choice"] == "e3"
    assert "provider" not in decision
    assert "typesafe-secret-value" not in json.dumps(decision)


def test_mercury_flat_criteria_keep_control_state_and_observed_ids(monkeypatch):
    monkeypatch.setenv("DECISION_PROVIDER", "mercury")
    monkeypatch.setenv("INCEPTION_API_KEY", "inception-secret-value")
    observed = page()
    observed["actions"].append({
        "id": "e9", "kind": "click", "label": "Refundable", "role": "checkbox",
        "checked": "false", "selected": False, "value": "on", "node": 7,
    })
    seen = {}

    def post(url, key, body):
        seen["url"] = url
        seen["body"] = body
        assert "inception-secret-value" not in json.dumps(body)
        return _mercury_decision(body, "e9")

    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose_mercury(observed, "Find a book", [], ["Find a book"], {})
    criteria = seen["body"]["questions"]["next_action"]["criteria"]
    assert seen["url"].endswith("/decisions")
    assert len(criteria) <= 255
    assert criteria["e9"]["current_value"] == "on"
    assert criteria["e9"]["checked"] == "false"
    assert criteria["e9"]["selected"] is False
    ids = {action["id"] for action in observed["actions"]}
    assert set(criteria) - {"DONE", "BLOCKED"} <= ids
    assert decision["choice"] == "e9"
    assert decision["operation"] == "CLICK"
    assert "inception-secret-value" not in json.dumps(decision["request"])
    assert "<goal>" in seen["body"]["questions"]["next_action"]["instructions"]
    assert "<rules>" in seen["body"]["questions"]["next_action"]["instructions"]
    assert "req_1" in seen["body"]["questions"]


def test_mercury_choice_list_never_exceeds_255(monkeypatch):
    monkeypatch.setenv("INCEPTION_API_KEY", "inception-secret-value")
    actions = [
        {"id": f"e{i}", "kind": "click", "label": f"Item {i}", "role": "button", "value": "", "node": i}
        for i in range(1, 301)
    ]
    seen = {}

    def post(url, key, body):
        seen["criteria"] = body["questions"]["next_action"]["criteria"]
        return _mercury_decision(body, "e1")

    monkeypatch.setattr(model, "post_json", post)
    model.choose_mercury(
        {"url": "u", "title": "t", "text": "t", "actions": actions},
        "Pick one",
        [],
        ["Pick one"],
        {},
    )
    assert len(seen["criteria"]) == 255
    assert {"DONE", "BLOCKED"} <= set(seen["criteria"])
    assert "e1" in seen["criteria"]


def test_done_is_blocked_when_a_requirement_is_below_the_bar(monkeypatch):
    monkeypatch.setenv("INCEPTION_API_KEY", "inception-secret-value")
    calls = []

    def post(url, key, body):
        calls.append(url)
        return _mercury_decision(body, "DONE", noul=0.2)

    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose_mercury(page(), "Find a book", [], ["The article is open"], {})
    assert decision["choice"] != "DONE"
    assert decision["done_gate"]["blocked"] is True
    assert decision["done_gate"]["reqs"]["r1"] == 0.2
    assert abs(sum(decision["probabilities"].values()) - 1) < 0.02
    assert "DONE" not in decision["probabilities"]
    assert calls == ["https://api.inceptionlabs.ai/v1/decisions"]


def test_done_is_blocked_when_the_verifier_is_incomplete(monkeypatch):
    monkeypatch.setenv("INCEPTION_API_KEY", "inception-secret-value")

    def post(url, key, body):
        if url.endswith("/decisions"):
            return _mercury_decision(body, "DONE", noul=0.95)
        return {
            "choices": [{"message": {"content": json.dumps({
                "complete": False,
                "missing": ["destination was not applied"],
            })}}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }

    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose_mercury(page(), "Find a book", [], ["The article is open"], {})
    assert decision["choice"] != "DONE"
    assert decision["done_gate"]["blocked"] is True
    assert decision["done_gate"]["verifier"]["complete"] is False
    assert decision["done_gate"]["verifier"]["missing"] == ["destination was not applied"]
    assert decision["verifier_calls"] == 1


def test_done_is_accepted_when_requirements_and_verifier_agree(monkeypatch):
    monkeypatch.setenv("INCEPTION_API_KEY", "inception-secret-value")

    def post(url, key, body):
        if url.endswith("/decisions"):
            return _mercury_decision(body, "DONE", noul=0.91)
        return {
            "choices": [{"message": {"content": json.dumps({"complete": True, "missing": []})}}],
            "usage": {},
        }

    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose_mercury(page(), "Find a book", [], ["The article is open"], {})
    assert decision["choice"] == "DONE"
    assert decision["operation"] == "DONE"
    assert "done_gate" not in decision
    assert decision["requirements"][0]["probability"] == 0.91


def test_decompose_falls_back_to_the_goal(monkeypatch):
    monkeypatch.setenv("INCEPTION_API_KEY", "inception-secret-value")
    monkeypatch.setattr(
        model,
        "post_json",
        Mock(return_value={"choices": [{"message": {"content": "not json"}}]}),
    )
    assert model.decompose_goal("Find a book") == ["Find a book"]


def test_requirement_probabilities_are_reused_on_the_next_step(monkeypatch, runner):
    monkeypatch.setenv("DECISION_PROVIDER", "mercury")
    seen = {"decompose": 0, "progress": []}

    def decompose(goal):
        seen["decompose"] += 1
        return ["The article is open"]

    def mercury(state, goal, history, requirements, progress):
        seen["progress"].append(dict(progress))
        return {
            "choice": "e3",
            "operation": "CLICK",
            "target": "2",
            "confidence": 1,
            "probabilities": {"e3": 1},
            "latency_ms": 1,
            "usage": {},
            "requirements": [{"id": "r1", "text": requirements[0], "probability": 0.4}],
        }

    monkeypatch.setattr(loop, "decompose_goal", decompose)
    monkeypatch.setattr(loop, "choose_mercury", mercury)
    runner.command("predict", {})
    runner.state["status"] = "ready"
    runner.state["decision"] = None
    runner.command("predict", {})
    assert seen["decompose"] == 1
    assert seen["progress"] == [{}, {"r1": 0.4}]


def test_done_records_final_answer(runner, monkeypatch):
    from jev_ultrafast.questions import ANSWER_VALUE

    seen = {}

    def fake_field_text(context, system=None):
        seen["system"] = system
        seen["goal"] = context["goal"]
        return "The answer.", {"model": "test", "latency_ms": 3, "usage": {}}

    monkeypatch.setattr(loop, "field_text", fake_field_text)
    monkeypatch.setattr(
        loop,
        "judge_progress",
        lambda context, system: (
            {"satisfied": [], "facts": [], "complete": True, "missing": ""},
            {"model": "t", "latency_ms": 1, "usage": {}},
        ),
    )
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "done"
    assert runner.state["answer"] == "The answer."
    assert seen["system"] == ANSWER_VALUE
    assert seen["goal"] == "Find a book"
    assert runner.state["text_calls"][-1]["field"] == "final answer"


def test_done_without_answer_when_helper_fails(runner, monkeypatch):
    def boom(context, system=None):
        raise ValueError("invalid field text")

    monkeypatch.setattr(loop, "field_text", boom)
    monkeypatch.setattr(
        loop,
        "judge_progress",
        lambda context, system: (
            {"satisfied": [], "facts": [], "complete": True, "missing": ""},
            {"model": "t", "latency_ms": 1, "usage": {}},
        ),
    )
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "done"
    assert runner.state["answer"] is None


def test_blocked_third_time_scrolls_once_then_accepts(runner):
    scrolled = page()
    scrolled["url"] = "https://example.test/scrolled"
    scrolled["fingerprint"] = fingerprint(scrolled)
    runner.state["page"]["actions"].append(
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560}
    )
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])
    runner.state["browser"].act = Mock(return_value={"executed": "scroll_down"})
    runner.state["browser"].wait_until_ready = Mock()
    runner.state["browser"].observe = Mock(return_value=scrolled)
    runner.blocked_streak = 2
    runner.state["decision"] = decision("BLOCKED")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "ready"
    assert runner.state["history"][-1]["operation"] == "SCROLL_DOWN"
    assert runner.blocked_streak == 3
    runner.state["decision"] = decision("BLOCKED")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    runner.state["browser"].act.assert_called_once()


def test_blocked_without_scroll_settles_as_before(runner):
    runner.state["browser"].settled = Mock()
    runner.blocked_streak = 2
    runner.state["decision"] = decision("BLOCKED")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "ready"
    runner.state["browser"].settled.assert_called_once()
    runner.state["browser"].act.assert_not_called()


def test_action_budget_follows_decision_budget(runner):
    runner.config = {"max_decisions": 3}
    runner.state["history"] = [{"step": 1}, {"step": 2}, {"step": 3}]
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "action budget"


def test_stale_tick_blocks_when_page_never_settles(runner):
    runner.state["browser"].fresh = Mock(return_value=False)
    runner.state["browser"].observe = Mock(side_effect=StalePage("Document is navigating"))
    runner.command("tick", {})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "page never settled"


def test_observe_backs_off_through_navigation(monkeypatch):
    import jev_ultrafast.browser as browser_mod

    browser = browser_mod.Browser.__new__(browser_mod.Browser)
    browser.session = "sess-test"
    browser.config = {}
    browser.settle_dom = Mock()
    calls, sleeps = [], []

    def flaky(request):
        calls.append(request)
        if len(calls) < 3:
            raise StalePage("Document is navigating")
        return {"ok": True}

    monkeypatch.setattr(browser_mod, "browser_operation", flaky)
    monkeypatch.setattr(browser_mod.Browser, "_finish_observation", lambda self, state: state)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    assert browser.observe(screenshot=False) == {"ok": True}
    assert [round(s, 3) for s in sleeps] == [0.05, 0.1]
    assert len(calls) == 3


def _stall_step(operation, label, kind="click", progress=False, step_id="e9", role="button"):
    return {
        "key": (operation, label, "u", "t"),
        "identity": ("u", "t"),
        "id": step_id,
        "label": label,
        "kind": kind,
        "role": role,
        "progress": progress,
    }


def test_detect_control_stall_rules():
    from jev_ultrafast.agent import detect_control_stall

    stuck = [_stall_step("CLICK", "Open Return")] * 3
    assert detect_control_stall(stuck)["key"] == loop.noop_key(
        {"kind": "click", "label": "Open Return", "role": "button"}
    )
    moving = [_stall_step("CLICK", "Open Return"), _stall_step("CLICK", "Open Return", progress=True)]
    assert detect_control_stall(moving + [_stall_step("CLICK", "Open Return")]) is None
    assert detect_control_stall([_stall_step("TYPE_TEXT", "Departure", kind="fill")] * 3) is None
    assert detect_control_stall([_stall_step("SCROLL_DOWN", "Scroll down", kind="scroll")] * 3) is None
    mixed = [_stall_step("CLICK", "A"), _stall_step("CLICK", "B"), _stall_step("CLICK", "A")]
    assert detect_control_stall(mixed) is None
    assert detect_control_stall([_stall_step("CLICK", "A")] * 2) is None


def test_stall_check_holds_and_advises_typing(runner, monkeypatch):
    seen = {}
    runner.state["loop_window"] = [
        _stall_step("CLICK", "Open Search", step_id="e2", role="textbox")
    ] * 3
    runner.state["history"] = [{"action": "x", "url": "https://example.test/"}]

    def fake_choose(*args, **kwargs):
        seen["guidance"] = kwargs.get("guidance")
        return decision("e3")

    monkeypatch.setattr(loop, "choose", fake_choose)
    runner.command("predict", {})
    key = loop.noop_key({"kind": "click", "label": "Open Search", "role": "textbox"})
    assert runner.state["held_actions"][key] == 4  # Held for 5, decayed once by this predict.
    assert any("typing the value in Search" in item for item in seen["guidance"])


def test_date_rule_reaches_the_model():
    from jev_ultrafast.questions import NEXT_ACTION

    assert "TYPE the date" in NEXT_ACTION and "press Enter" in NEXT_ACTION


def _judgment(complete=True, missing="", satisfied=(), facts=(), leave_site=False):
    return (
        {
            "satisfied": list(satisfied),
            "facts": list(facts),
            "complete": complete,
            "missing": missing,
            "leave_site": leave_site,
        },
        {"model": "t", "latency_ms": 1, "usage": {}},
    )


def test_subgoals_activate_on_second_host(runner, monkeypatch):
    calls = []
    runner.state["history"] = [
        {"action": "a", "url": "https://one.test/"},
        {"action": "b", "url": "https://two.test/"},
    ]
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: calls.append(goal) or ["one", "two"])
    monkeypatch.setattr(loop, "judge_progress", lambda context, system: _judgment())
    monkeypatch.setattr(loop, "choose", lambda *_args, **_kwargs: decision("e3"))
    runner.command("predict", {})
    assert [item["text"] for item in runner.state["subgoals"]] == ["one", "two"]
    assert runner.state["judged_url"] == "https://example.test/"
    assert calls == ["Find a book"]


def test_subgoals_decompose_on_first_tick_before_any_decision(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(
        loop, "decompose_goal", lambda goal: calls.append("decompose") or ["one", "two"]
    )
    monkeypatch.setattr(loop, "judge_progress", lambda context, system: _judgment())
    monkeypatch.setattr(
        loop, "choose", lambda *_args, **kwargs: calls.append("choose") or decision("e3")
    )
    runner.command("predict", {})
    assert calls == ["decompose", "choose"]
    assert [item["text"] for item in runner.state["subgoals"]] == ["one", "two"]
    assert runner.state["_decomposed"] == ["one", "two"]


def test_single_requirement_stays_dormant(runner, monkeypatch):
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["Find a book"])
    monkeypatch.setattr(loop, "judge_progress", Mock(side_effect=AssertionError("no judge call")))
    monkeypatch.setattr(loop, "choose", lambda *_args, **kwargs: decision("e3"))
    runner.command("predict", {})
    assert runner.state.get("subgoals") is None
    assert runner.state["_decomposed"] == ["Find a book"]


def test_split_requirements_is_one_level_and_deduped():
    assert loop.split_requirements("Find the population of Bangkok, then find its weather") == [
        "Find the population of Bangkok", "find its weather",
    ]
    assert loop.split_requirements("A and B and C and D and E") == ["A", "B", "C", "D and E"]
    assert loop.split_requirements("Find a book") == ["Find a book"]
    assert loop.split_requirements("") == []
    assert loop.split_requirements("hotels in Lisbon and 2 adults", ["search hotels in Lisbon"]) == [
        "2 adults",
    ]


def test_model_single_item_is_split_into_subgoals(runner, monkeypatch):
    monkeypatch.setattr(
        loop, "decompose_goal", lambda goal: ["find the population of Bangkok, then find its weather"]
    )
    monkeypatch.setattr(loop, "judge_progress", lambda context, system: _judgment())
    monkeypatch.setattr(loop, "choose", lambda *_args, **kwargs: decision("e3"))
    runner.command("predict", {})
    assert [item["text"] for item in runner.state["subgoals"]] == [
        "find the population of Bangkok", "find its weather",
    ]


def test_required_cue_verbs_mark_strict_items():
    state = {
        "subgoals": [
            {"id": 0, "text": "must find the price", "required": True},
            {"id": 1, "text": "find the seller rating", "required": False},
        ],
        "memo": [],
    }
    loop.apply_judgment(
        state,
        {"satisfied": [], "facts": ["seller rating 98% from the listing page"]},
        "https://shop.test/item",
    )
    assert state["subgoals"][0].get("satisfied") is not True
    assert state["subgoals"][1]["satisfied"] is True
    assert state["subgoals"][1]["value"] == "seller rating 98% from the listing page"
    assert state["subgoals"][1]["source"] == "https://shop.test/item"
    guidance = loop.compose_guidance({**state, "history": [], "goal": "g"})
    assert any("Must still: must find the price" in line for line in guidance)


def test_evidence_rules_survive_in_prompts():
    from jev_ultrafast.questions import DECOMPOSE_SYSTEM, TRACK_SYSTEM

    assert "neutral imperative" in DECOMPOSE_SYSTEM
    assert "no nested sub-plans" in DECOMPOSE_SYSTEM
    assert "copied" in TRACK_SYSTEM and "exact page URL" in TRACK_SYSTEM
    assert "must match the sub-goal's item" in TRACK_SYSTEM
    assert "same items on both sides" in TRACK_SYSTEM


def test_judge_early_exits_without_leaving(runner, monkeypatch):
    judge = Mock(return_value=_judgment())
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["population of Bangkok", "Bangkok weather"])
    monkeypatch.setattr(loop, "judge_progress", judge)
    monkeypatch.setattr(loop, "choose", lambda *_args, **kwargs: decision("e3"))
    runner.command("predict", {})
    judge.assert_not_called()
    assert runner.state["leave_site"] is False  # a content page never forces a leave
    assert runner.state["force_navigate"] is False
    assert runner.state["judged_url"] == "https://example.test/"


def test_early_exit_still_leaves_blank_and_error_pages(runner, monkeypatch):
    judge = Mock(return_value=_judgment())
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["population of Bangkok", "Bangkok weather"])
    monkeypatch.setattr(loop, "judge_progress", judge)
    monkeypatch.setattr(loop, "choose", lambda *_args, **kwargs: decision("e3"))
    runner.state["page"]["url"] = "about:blank"
    runner.state["page"]["text"] = ""
    runner.command("predict", {})
    assert runner.state["leave_site"] is True
    assert runner.state["force_navigate"] is True
    runner.state["page"]["url"] = "https://example.test/"
    runner.state["page"]["text"] = "This site can't be reached. ERR_NAME_NOT_RESOLVED."
    runner.state["judged_url"] = None
    runner.state["judged_step"] = -9
    runner.state["leave_site"] = False
    runner.command("predict", {})
    assert runner.state["leave_site"] is True


def test_pagination_run_never_early_exits(runner, monkeypatch):
    judge = Mock(return_value=_judgment())
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["population of Bangkok", "Bangkok weather"])
    monkeypatch.setattr(loop, "judge_progress", judge)
    monkeypatch.setattr(loop, "choose", lambda *_args, **kwargs: decision("e3"))
    runner.state["page_target"] = 3
    runner.command("predict", {})
    judge.assert_called()  # no early skip while paginating
    assert runner.state["leave_site"] is False


def test_home_host_needs_two_judge_votes_to_force(runner):
    runner.state["subgoals"] = [{"id": 0, "text": "population"}, {"id": 1, "text": "weather"}]
    runner.state["leave_site"] = True
    runner.state["leave_site_url"] = runner.state["page"]["url"]
    runner.state["home_host"] = "example.test"
    runner.state["home_leave_votes"] = 1
    runner._update_suppression(runner.state)
    assert runner.state["force_navigate"] is False
    runner.state["home_leave_votes"] = 2
    runner._update_suppression(runner.state)
    assert runner.state["force_navigate"] is True
    runner.state["page"]["url"] = "https://away.test/"
    runner.state["leave_site_url"] = "https://away.test/"
    runner._update_suppression(runner.state)
    assert runner.state["home_leave_votes"] == 0  # votes reset off the home host


def test_home_host_set_from_first_evidence():
    state = {"subgoals": [{"id": 0, "text": "population"}], "memo": []}
    loop.apply_judgment(state, {"satisfied": [{"id": 0, "value": "9M"}], "facts": [], "leave_site": False},
                        "https://one.test/x")
    assert state["home_host"] == "one.test"
    loop.apply_judgment(state, {"satisfied": [], "facts": ["more"], "leave_site": False},
                        "https://two.test/y")
    assert state["home_host"] == "one.test"  # first evidence wins
    blank = {"subgoals": [{"id": 0, "text": "population"}], "memo": []}
    loop.apply_judgment(blank, {"satisfied": [{"id": 0, "value": "9M"}], "facts": [], "leave_site": False}, "")
    assert blank.get("home_host") is None


def test_judge_runs_with_overlap_or_dialog(runner, monkeypatch):
    judge = Mock(return_value=_judgment())
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["population of Bangkok", "Bangkok weather"])
    monkeypatch.setattr(loop, "judge_progress", judge)
    monkeypatch.setattr(loop, "choose", lambda *_args, **kwargs: decision("e3"))
    runner.state["page"]["text"] = "Bangkok weather today"
    runner.command("predict", {})
    judge.assert_called_once()
    runner.state["page"]["text"] = "Search"
    runner.state["page"]["modal_scoped"] = True
    runner.state["judged_url"] = None
    runner.state["judged_step"] = -9
    runner.state["status"] = "ready"
    runner.state["decision"] = None
    runner.command("predict", {})
    assert judge.call_count == 2


def test_apply_judgment_folds_evidence_and_memo():
    from jev_ultrafast.agent import apply_judgment

    state = {
        "subgoals": [{"id": 0, "text": "population"}, {"id": 1, "text": "temperature"}],
        "memo": [{"fact": "old", "source": "u"}],
    }
    judgment = {
        "satisfied": [{"id": 0, "value": "8.9M"}, {"id": 9, "value": "bogus"}],
        "facts": ["Bangkok is hot", "Bangkok is hot", ""],
    }
    apply_judgment(state, judgment, "https://example.test/w")
    assert state["subgoals"][0]["value"] == "8.9M"
    assert state["subgoals"][0]["source"] == "https://example.test/w"
    assert "satisfied" not in state["subgoals"][1]
    assert [item["fact"] for item in state["memo"]] == ["old", "Bangkok is hot"]


def test_done_gate_rejects_twice_then_partial_accepts(runner, monkeypatch):
    monkeypatch.setattr(
        loop, "judge_progress", lambda context, system: _judgment(False, missing="the temperature")
    )
    monkeypatch.setattr(loop, "field_text", lambda context, system=None: ("answer", {"model": "t"}))
    for _ in range(2):
        runner.state["decision"] = decision("DONE")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        assert runner.state["status"] == "ready"
    assert runner.state["done_rejections"] == 2
    assert any("temperature" in note["text"] for note in runner.state["event_notes"])
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "done"
    assert runner.state["partial"] is True
    assert runner.state["answer"] == "answer"


def test_done_gate_accepts_complete(runner, monkeypatch):
    monkeypatch.setattr(loop, "judge_progress", lambda context, system: _judgment(True))
    monkeypatch.setattr(loop, "field_text", lambda context, system=None: ("answer", {"model": "t"}))
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "done"
    assert runner.state["partial"] is False


def test_done_gate_trusts_subgoal_evidence_over_model(runner, monkeypatch):
    runner.state["subgoals"] = [{"id": 0, "text": "all", "satisfied": True, "value": "v", "source": "u"}]
    monkeypatch.setattr(loop, "judge_progress", lambda context, system: _judgment(False, missing="x"))
    monkeypatch.setattr(loop, "field_text", lambda context, system=None: ("answer", {"model": "t"}))
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "done"
    assert runner.state["partial"] is False


def test_parse_judge_rules():
    from jev_ultrafast.model import parse_judge

    def result(content):
        return {"choices": [{"message": {"content": json.dumps(content)}}]}

    good = parse_judge(result({"satisfied": [{"id": 1, "value": " 29C "}], "facts": [" hot "],
                               "complete": True, "missing": "", "leave_site": True}))
    assert good == {"satisfied": [{"id": 1, "value": "29C"}], "facts": ["hot"], "complete": True,
                    "missing": "", "leave_site": True}
    with pytest.raises(ValueError, match="invalid judgment"):
        parse_judge(result({"satisfied": [], "facts": []}))
    with pytest.raises(ValueError, match="invalid judgment"):
        parse_judge(result({"satisfied": [], "facts": [], "complete": True, "missing": "", "leave_site": "x"}))
    dropped = parse_judge(result({"satisfied": [{"id": "x", "value": ""}, {"id": 2}],
                                  "facts": ["", 7], "complete": False, "missing": "y",
                                  "leave_site": False}))
    assert dropped["satisfied"] == [] and dropped["facts"] == []


def test_judge_progress_uses_structured_schema(monkeypatch):
    from jev_ultrafast import model as model_mod

    seen = {}

    def fake_field_text(context, system=None, **kwargs):
        seen.update(kwargs)
        return {}, {}

    monkeypatch.setattr(model_mod, "field_text", fake_field_text)
    model_mod.judge_progress({}, system="sys")
    assert seen["schema"] == model_mod.JUDGE_SCHEMA
    assert seen["name"] == "Progress"
    assert seen["parse"] == model_mod.parse_judge
    assert seen["label"] == "judgment"


def test_guidance_reaches_typesafe_body():
    elements, targets, _controls = model.action_space(page()["actions"])
    operations = model.operation_catalog(targets, {})
    body = model.typesafe_body(page(), "goal", [], elements, targets, operations, guidance=["stay left"])
    assert body["state"]["recent_actions"][-1] == {
        "action": "(guidance)", "kind": "guidance", "text": None, "page_changed": None, "note": "stay left",
    }


def test_winding_down_flags_at_75_percent(runner, monkeypatch):
    runner.config = {"max_decisions": 4}
    runner.state["history"] = [{}, {}, {}]
    monkeypatch.setattr(loop, "choose", lambda *_args, **_kwargs: decision("e3"))
    runner.command("predict", {})
    assert runner.state["winding_down"] is True
    assert runner.state["status"] == "predicted"


def test_stale_decisions_do_not_spend_the_action_budget(runner, monkeypatch):
    runner.config = {"max_decisions": 2}
    runner.state["decisions"] = [{}, {}]
    runner.state["history"] = []
    monkeypatch.setattr(loop, "choose", lambda *_args, **_kwargs: decision("e3"))
    runner.command("predict", {})
    assert runner.state["status"] == "predicted"
    assert runner.state.get("winding_down") is not True


def test_stale_cap_blocks_the_run(runner):
    runner.state["stale_used"] = loop.STALE_CAP
    runner.command("predict", {})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "stale budget"


def test_budget_stop_attaches_partial_answer(runner, monkeypatch):
    runner.config = {"max_decisions": 2}
    runner.state["history"] = [{}, {}]
    runner.state["memo"] = [{"fact": "Mercury has 0 moons", "source": "https://example.test/"}]
    seen = {}

    def fake_field_text(context, system=None):
        seen["ctx"] = context
        return "partial answer", {"model": "t"}

    monkeypatch.setattr(loop, "field_text", fake_field_text)
    runner.command("predict", {})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "decision budget"
    assert runner.state["partial"] is True
    assert runner.state["answer"] == "partial answer"
    assert seen["ctx"]["partial"] is True
    assert seen["ctx"]["facts"] == ["Mercury has 0 moons"]


def test_goal_page_target_rules():
    from jev_ultrafast.agent import goal_page_target

    assert goal_page_target("On HN, go to the 3rd page of stories") == 3
    assert goal_page_target("go to page 3 and report the author") == 3
    assert goal_page_target("list the top 5 stories with points") is None
    assert goal_page_target("report the price of the first 3 results") is None
    assert goal_page_target("find the population of Bangkok") is None


def test_current_page_number_rules():
    from jev_ultrafast.agent import current_page_number

    assert current_page_number("https://news.ycombinator.com/?p=3", "") == 3
    assert current_page_number("https://quotes.toscrape.com/page/2/", "") == 2
    assert current_page_number("https://example.test/", "Results Page 4 of 12") == 4
    assert current_page_number("https://example.test/", "no marker here") is None


def test_done_gate_rejects_wrong_page_without_judge(runner, monkeypatch):
    def boom(context, system):
        raise AssertionError("pagination needs no judge call")

    monkeypatch.setattr(loop, "judge_progress", boom)
    runner.state["page_target"] = 3
    runner.state["page_current"] = 2
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "ready"
    assert any("page 3" in note["text"] for note in runner.state["event_notes"])


def test_page_counter_guidance():
    from jev_ultrafast.agent import compose_guidance

    state = {"history": [], "page_target": 3, "page_current": 1}
    assert compose_guidance(state) == ["On page 1 of 3: keep going to the next page."]
    state["page_current"] = 3
    assert compose_guidance(state) == []


def _link_page():
    guards = {}
    actions = []
    for index, (label, href, nav) in enumerate(
        [
            ("USB-C Hub 8-in-1", "https://shop.test/itm/hub-8in1", False),
            ("Electronics", "https://shop.test/categories/electronics", True),
            ("Help", "https://shop.test/help", True),
            ("USB-C Hub Pro", "https://shop.test/itm/hub-pro", False),
            ("navigate", "", False),
        ]
    ):
        node = 100 + index
        actions.append(
            {
                "id": f"e{index}",
                "kind": "click" if label != "navigate" else "navigate",
                "role": "link" if label != "navigate" else None,
                "label": label,
                "node": node,
                "frame": "f0",
                **({"nav": True} if nav else {}),
            }
        )
        guards[f"f0:{node}"] = {"role": "link", "name": label.lower(), "tag": "A", "href": href}
    return {"url": "https://shop.test/search", "title": "", "text": "", "fingerprint": "fp",
            "actions": actions, "guards": guards}


def test_score_link_rules():
    from jev_ultrafast.agent import _score_words, score_link

    todo = _score_words("report the price of usb-c hub results")
    assert todo == {"report", "price", "usb", "hub"}
    item = score_link("USB-C Hub 8-in-1", "https://shop.test/itm/hub-8in1", False, todo, set())
    category = score_link("Electronics", "https://shop.test/categories", True, todo, set())
    assert item > 0 and category < 0
    assert score_link("USB-C Hub", "https://shop.test/itm/1", False, todo, {"shop.test/itm/1"}) < 0


def test_compute_suppressed_rules():
    from jev_ultrafast.agent import compute_suppressed

    page = _link_page()
    assert compute_suppressed({"page": page}, page) == set()  # No sub-goals: no scoring.
    state = {
        "subgoals": [{"id": 0, "text": "price of usb-c hub", "satisfied": True, "value": "1",
                      "source": "u"},
                     {"id": 1, "text": "seller rating of usb-c hub"}],
        "page_target": None,
    }
    def key(label):
        return loop.noop_key({"kind": "click", "label": label, "role": "link"})

    dropped = compute_suppressed(state, page)
    assert dropped == {key("Electronics"), key("Help")}  # Zero-signal nav hidden; results kept.
    state["tried_links"] = ["shop.test/itm/hub-8in1"]
    # The tried item joins the drop set, but one untried result link remains, so no strand guard.
    assert compute_suppressed(state, page) == {key("USB-C Hub 8-in-1"), key("Electronics"), key("Help")}
    state["tried_links"] = ["shop.test/itm/hub-8in1", "shop.test/itm/hub-pro"]
    assert compute_suppressed(state, page) == set()  # All links would drop: keep all instead.
    state["page_target"] = 3
    assert compute_suppressed(state, page) == set()  # Pagination follows the counter.
    state["page_target"] = None
    state["force_navigate"] = True
    assert compute_suppressed(state, page) == {
        key("USB-C Hub 8-in-1"), key("Electronics"), key("Help"), key("USB-C Hub Pro")
    }


def test_page_for_choice_suppression_by_fingerprint(runner):
    runner.state["suppressed_fp"] = runner.state["page"]["fingerprint"]
    go = next(a for a in runner.state["page"]["actions"] if a["id"] == "e3")
    runner.state["suppressed_keys"] = {loop.noop_key(go)}
    offered = loop.page_for_choice(runner.state)
    assert [a["id"] for a in offered["actions"]] == ["e1", "e2", "wait"]
    runner.state["suppressed_fp"] = "other"
    assert loop.page_for_choice(runner.state) is runner.state["page"]


def test_page_for_choice_sorts_generic_targets_last(runner):
    actions = runner.state["page"]["actions"]
    first, second = actions[0], actions[1]
    first["generic"] = True
    runner.state["suppressed_fp"] = runner.state["page"]["fingerprint"]
    runner.state["suppressed_keys"] = set()
    offered = loop.page_for_choice(runner.state)
    assert [a["id"] for a in offered["actions"]] == ["e2", "e3", "wait", "e1"]
    offered["actions"][0]["label"] = "sentinel"
    assert second["label"] == "sentinel"  # sorted copy still shares the action dicts


def test_force_navigate_updates_suppression(runner):
    runner.state["subgoals"] = [{"id": 0, "text": "population", "satisfied": True, "value": "9M",
                                 "source": "https://one.test/"},
                                {"id": 1, "text": "temperature"}]
    runner.state["leave_site"] = True
    runner.state["leave_site_url"] = runner.state["page"]["url"]
    runner._update_suppression(runner.state)
    assert runner.state["force_navigate"] is True
    assert runner.state["suppressed_keys"] == {
        loop.noop_key(a) for a in runner.state["page"]["actions"] if a["kind"] != "navigate"
    }
    runner.state["held_actions"] = {loop.noop_key({"kind": "navigate", "label": "Go to URL"}): 2}
    runner._update_suppression(runner.state)
    assert runner.state["force_navigate"] is False


def test_excursion_recorded_on_navigating_link(runner):
    runner.state["subgoals"] = [{"id": 0, "text": "something"}]
    go = next(a for a in runner.state["page"]["actions"] if a["id"] == "e3")
    go["role"] = "link"
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])
    moved = page()
    moved["url"] = "https://example.test/other"
    moved["fingerprint"] = fingerprint(moved)
    runner.state["browser"].observe = Mock(return_value=moved)
    runner.state["browser"].act = Mock(return_value={"executed": "e3"})
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["excursion"]["at"] == "https://example.test/other"
    assert runner.state["excursion"]["label"] == "Go"


def test_excursion_ignores_commits_and_query_only_changes():
    link = {"kind": "click", "role": "link", "label": "Gadget"}
    before = {"url": "https://shop.test/s?q=1"}
    assert loop.excursion_for(link, before, {"url": "https://shop.test/s?q=2"}) is None
    assert loop.excursion_for(link, before, {"url": "https://shop.test/s?q=1#reviews"}) is None
    button = {"kind": "click", "role": "button", "label": "Gadget"}
    assert loop.excursion_for(button, before, {"url": "https://shop.test/itm/9"}) is None
    done = {"kind": "click", "role": "link", "label": "Done"}
    assert loop.excursion_for(done, before, {"url": "https://shop.test/itm/9"}) is None
    show = {"kind": "click", "role": "link", "label": "Show details"}
    assert loop.excursion_for(show, before, {"url": "https://shop.test/itm/9"}) is None
    dialog = {"url": "https://shop.test/s?q=1", "modal_scoped": True}
    assert loop.excursion_for(link, dialog, {"url": "https://shop.test/itm/9"}) is None
    outing = loop.excursion_for(link, before, {"url": "https://shop.test/itm/9"})
    assert (outing["from"], outing["at"], outing["label"]) == (
        "https://shop.test/s?q=1", "https://shop.test/itm/9", "Gadget",
    )


def test_dialog_done_click_sets_no_excursion(runner):
    runner.state["subgoals"] = [{"id": 0, "text": "something"}]
    link = {"id": "e3", "kind": "click", "role": "link", "label": "Done", "node": 20}
    runner.state["page"]["actions"] = [link, {"id": "wait", "kind": "wait", "label": "Wait"}]
    runner.state["page"]["modal_scoped"] = True
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])
    moved = page()
    moved["url"] = "https://example.test/other"
    moved["fingerprint"] = fingerprint(moved)
    runner.state["browser"].observe = Mock(return_value=moved)
    runner.state["browser"].act = Mock(return_value={"executed": "e3"})
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert "excursion" not in runner.state


def test_query_only_click_sets_no_excursion(runner):
    runner.state["subgoals"] = [{"id": 0, "text": "something"}]
    go = next(a for a in runner.state["page"]["actions"] if a["id"] == "e3")
    go["role"] = "link"
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])
    moved = page()
    moved["url"] = "https://example.test/?q=2"
    moved["fingerprint"] = fingerprint(moved)
    runner.state["browser"].observe = Mock(return_value=moved)
    runner.state["browser"].act = Mock(return_value={"executed": "e3"})
    runner.state["decision"] = decision("e3")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert "excursion" not in runner.state


def test_maybe_go_back_rules(runner):
    browser = runner.state["browser"]
    browser.go_back = Mock()
    browser.wait_until_ready = Mock()
    browser.observe = Mock(return_value=page())
    runner.state["subgoals"] = [{"id": 0, "text": "price of usb-c hub"}]
    runner.state["excursion"] = {"from": "https://shop.test/s", "at": "https://shop.test/itm/9",
                                 "label": "Gadget", "page": None}
    runner._maybe_go_back(runner.state, "https://shop.test/itm/9", False, ["unrelated story"])
    assert browser.go_back.call_count == 1
    assert runner.state["tried_links"] == ["shop.test/itm/9"]
    assert runner.state["excursion"] is None
    assert any("Went back" in note["text"] for note in runner.state["event_notes"])

    browser.go_back.reset_mock()
    runner.state["page"] = {**page(), "title": "usb-c hub", "text": "usb-c hub price deals"}
    runner.state["excursion"] = {"from": "a", "at": "https://shop.test/itm/9", "label": "G", "page": None}
    runner._maybe_go_back(runner.state, "https://shop.test/itm/9", True, [])
    assert browser.go_back.call_count == 0  # New evidence on an on-goal page: stay.

    runner.state["excursion"] = {"from": "a", "at": "https://shop.test/itm/9", "label": "G", "page": None}
    runner._maybe_go_back(runner.state, "https://shop.test/itm/9", False, ["usb-c hub price 100"])
    assert browser.go_back.call_count == 0  # On-topic facts on an on-goal page: stay.

    runner.state["page"] = page()
    runner.state["excursion"] = {"from": "a", "at": "https://shop.test/itm/9", "label": "G", "page": None}
    runner._maybe_go_back(runner.state, "https://shop.test/itm/9", True, [])
    assert browser.go_back.call_count == 1  # Off-goal detour overrides claimed gain.

    runner.state["excursion"] = {"from": "a", "at": "https://else.test/", "label": "G", "page": None}
    runner.state["backs_used"] = 3
    runner._maybe_go_back(runner.state, "https://else.test/", False, [])
    assert browser.go_back.call_count == 1  # Back budget spent.
    assert runner.state["excursion"] is None


def test_back_un_satisfies_only_reverted_evidence(runner):
    browser = runner.state["browser"]
    browser.go_back = Mock()
    browser.wait_until_ready = Mock()
    reverted = page()
    reverted.update(url="https://fly.test/search", title="Search", text="1 adult, economy")
    browser.observe = Mock(return_value=reverted)
    runner.state["subgoals"] = [
        {"id": 0, "text": "passengers", "satisfied": True, "value": "2 adults",
         "source": "https://fly.test/search?a=1"},
        {"id": 1, "text": "cabin", "satisfied": True, "value": "economy",
         "source": "https://fly.test/search?a=1"},
        {"id": 2, "text": "weather", "satisfied": True, "value": "31C",
         "source": "https://other.test/wx"},
    ]
    runner.state["excursion"] = {"from": "https://fly.test/search", "at": "https://fly.test/away",
                                 "label": "Gadget", "page": None}
    runner._maybe_go_back(runner.state, "https://fly.test/away", False, ["unrelated story"])
    assert browser.go_back.call_count == 1
    goals = {item["text"]: item for item in runner.state["subgoals"]}
    assert goals["passengers"]["satisfied"] is False
    assert "value" not in goals["passengers"] and "source" not in goals["passengers"]
    assert goals["cabin"]["satisfied"] is True
    assert goals["weather"]["satisfied"] is True


def test_back_releases_the_reverted_control(runner):
    browser = runner.state["browser"]
    browser.go_back = Mock()
    browser.wait_until_ready = Mock()
    browser.observe = Mock(return_value=page())
    runner.state["subgoals"] = [{"id": 0, "text": "price of usb-c hub"}]
    done_key = loop.noop_key({"kind": "click", "label": "Done", "role": "button"})
    other_key = loop.noop_key({"kind": "click", "label": "Other", "role": "button"})
    runner.state["held_actions"] = {done_key: 2, other_key: 2}
    runner.state["hold_reasons"] = {done_key: "no-progress hold"}
    runner.state["noop_streak"] = {done_key: 2}
    runner.state["noop_page"] = {"https://example.test/": [done_key, other_key]}
    runner.state["noop_noted"] = [done_key]
    runner.state["excursion"] = {"from": "a", "at": "https://shop.test/itm/9",
                                 "label": "Done", "page": None}
    runner._maybe_go_back(runner.state, "https://shop.test/itm/9", False, ["unrelated story"])
    assert browser.go_back.call_count == 1
    assert done_key not in runner.state["held_actions"]
    assert done_key not in runner.state["hold_reasons"]
    assert done_key not in runner.state["noop_streak"]
    assert done_key not in runner.state["noop_page"]["https://example.test/"]
    assert done_key not in runner.state["noop_noted"]
    assert runner.state["held_actions"] == {other_key: 2}


def test_gate_holds_done_until_wind_down_with_todo(runner, monkeypatch):
    runner.state["subgoals"] = [{"id": 0, "text": "population", "satisfied": True, "value": "9M",
                                 "source": "u"},
                                {"id": 1, "text": "temperature"}]
    monkeypatch.setattr(loop, "judge_progress", lambda context, system: _judgment(True))
    monkeypatch.setattr(loop, "field_text", lambda context, system=None: ("answer", {"model": "t"}))
    for _ in range(3):
        runner.state["decision"] = decision("DONE")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        assert runner.state["status"] == "ready"
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "done"  # Same verdict 4x: spin-breaker accepts partial.
    assert runner.state["partial"] is True


def test_gate_partial_accepts_at_wind_down(runner, monkeypatch):
    runner.state["subgoals"] = [{"id": 0, "text": "temperature"}]
    runner.state["winding_down"] = True
    runner.state["done_rejections"] = 2
    monkeypatch.setattr(loop, "judge_progress", lambda context, system: _judgment(False, "temperature"))
    monkeypatch.setattr(loop, "field_text", lambda context, system=None: ("answer", {"model": "t"}))
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "done"
    assert runner.state["partial"] is True


def test_stall_advice_prefers_keyboard_in_grids(runner):
    runner.state["page"]["actions"].append(
        {"id": "e9", "kind": "click", "role": "gridcell", "label": "Thursday, June 18, 2027",
         "node": 99, "frame": "f0", "in_grid": True}
    )
    stall = loop.detect_control_stall(
        [_stall_step("CLICK", "Thursday, June 18, 2027", role="gridcell")] * 3
    )
    advice = runner._stall_advice(runner.state, stall)
    assert "Arrow keys" in advice and "Enter" in advice


def test_check_act_records_verification(runner):
    check = {"id": "e9", "kind": "check", "role": "checkbox", "label": "Set Wifi", "desired": True,
             "node": 99, "frame": "f0"}
    runner.state["page"]["actions"].append(check)
    results = [
        {"executed": "e9", "verified": True, "checked": True},
        {"executed": "e9", "already_set": True, "checked": True},
        {"executed": "e9", "verified": False, "checked": False},
    ]
    runner.state["browser"].act = Mock(side_effect=results)
    runner.state["browser"].wait_until_ready = Mock()
    for _ in results:
        runner.state["decision"] = decision("e9")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert [h.get("note") for h in runner.state["history"]] == [None, "already selected", "value unchanged"]


def test_set_checked_operation_mapping():
    actions = [{"kind": "check", "label": "Set Wifi", "node": 7, "frame": "f0"}]
    elements, targets, controls = model.action_space(actions)
    operations = model.operation_catalog(targets, controls)
    assert operations["SET_CHECKED"] == (
        "Set or clear an offered checkbox, radio, or switch. The state is verified after."
    )
    assert targets["SET_CHECKED"] == {"1": actions[0]}
    assert elements[0]["operations"] == ["SET_CHECKED"]
