"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


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
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.blocked_streak = 0
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


def test_debug_log_reports_tokens_per_second(monkeypatch, capsys):
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
    model.choose(page(), "Find a book", [])
    err = capsys.readouterr().err
    assert "[jev:typesafe]" in err
    assert "prompt=100 completion=50 tok/s=" in err
    assert "operation=TYPE_TEXT target=1 choice=e1" in err
    assert "SECRETKEY123" not in err


def test_debug_log_parses_gateway_usage_keys(monkeypatch, capsys):
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
    model.field_text({"goal": "Find a flight"})
    err = capsys.readouterr().err
    assert "[jev:text]" in err
    assert "prompt=200 completion=25 tok/s=" in err
    assert "value='Zurich'" in err


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


def test_debug_verbose_dumps_request_and_response(monkeypatch, capsys):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("JEV_DEBUG", "2")
    monkeypatch.setattr(
        model, "post_json", Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    )
    model.field_text({"goal": "Find a flight"})
    err = capsys.readouterr().err
    assert "[jev:text] request:" in err
    assert "[jev:text] response:" in err
    assert "sk-" not in err and "Bearer" not in err


def test_debug_log_covers_rejected_values(monkeypatch, capsys):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("JEV_DEBUG", "1")
    monkeypatch.setattr(
        model, "post_json", Mock(return_value={"choices": [{"message": {"content": '{"text":null}'}}]})
    )
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})
    assert "error=no valid field value" in capsys.readouterr().err


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


def test_invalid_text_twice_fails_cleanly(monkeypatch, capsys, runner):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL", "mercury-2.5")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "instant")
    efforts = []

    def post(_url, _key, body):
        efforts.append(body["reasoning_effort"])
        return {"choices": [{"message": {"content": "still not json"}}], "usage": {}}

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})
    assert efforts == ["instant", "low"]
    assert "error=no valid field value; step skipped" in capsys.readouterr().err
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
