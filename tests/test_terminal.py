"""Every terminal path keeps the memo as a partial answer. No paid APIs."""

import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import Browser, fingerprint
from jev_ultrafast.trace import Trace, get_logger

FACT = "Bangkok population 8M"
ANSWER = "Bangkok population 8M (partial)"


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


@pytest.fixture
def runner():
    agent = loop.Agent.__new__(loop.Agent)
    agent.screenshots = False
    agent.pending_text = None
    agent.blocked_streak = 0
    agent.log = get_logger("agent")
    agent.trace = Trace(run_id="test-terminal", level=0)
    board = page()
    agent.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=board)),
        "page": board,
        "decision": decision(),
        "goal": "Find the population of Bangkok",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return agent


@pytest.fixture
def memo_answer(monkeypatch):
    seen = {}

    def fake_field_text(context, **_kwargs):
        seen["facts"] = context.get("facts")
        return ANSWER, {"model": "t", "latency_ms": 1}

    monkeypatch.setattr(loop, "field_text", fake_field_text)
    return seen


def test_blocked_streak_keeps_the_memo(runner, memo_answer):
    runner.state["memo"] = [{"fact": FACT}]
    runner.blocked_streak = 3
    runner.state["decision"] = {**decision("BLOCKED"), "operation": "BLOCKED"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "gave up"
    assert runner.state["answer"] == ANSWER
    assert runner.state["partial"] is True
    assert memo_answer["facts"] == [FACT]


def test_stall_keeps_the_memo(runner, memo_answer):
    runner.state["memo"] = [{"fact": FACT}]
    others = [
        loop.noop_key(item)
        for item in runner.state["page"]["actions"]
        if item["id"] in {"e1", "e2"}
    ]
    runner.state["held_actions"] = loop.hold_actions({}, others, loop.STALL_HOLD_STEPS)
    for _ in range(3):
        runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "stall"
    assert runner.state["answer"] == ANSWER
    assert runner.state["partial"] is True
    assert memo_answer["facts"] == [FACT]


def test_loop_keeps_the_memo(runner, memo_answer, monkeypatch):
    ids = ["e2", "e3"]
    monkeypatch.setattr(
        loop, "choose",
        lambda *_args, **_kwargs: {**decision(ids[len(runner.state["history"]) % 2]), "operation": "CLICK"},
    )
    monkeypatch.setattr(loop, "decompose_goal", lambda goal: ["goal"])
    monkeypatch.setattr(
        loop, "judge_progress",
        lambda context, system: (
            {"satisfied": [], "facts": [], "complete": True, "missing": ""},
            {"model": "t", "latency_ms": 1, "usage": {}},
        ),
    )
    rerenders = {"n": 0}

    def rerender(*_args, **_kwargs):
        rerenders["n"] += 1
        return {**page(), "fingerprint": f"fp{rerenders['n']}"}

    runner.state["memo"] = [{"fact": FACT}]
    runner.state["browser"].observe.side_effect = rerender
    runner.state["browser"].act.return_value = {"executed": "x"}
    for _ in range(12):
        runner.command("tick")
        if runner.state["status"] == "blocked":
            break
    assert runner.state["blocked_reason"] == "loop detected"
    assert runner.state["answer"] == ANSWER
    assert runner.state["partial"] is True
    assert memo_answer["facts"] == [FACT]


def test_budget_keeps_the_memo(runner, memo_answer, monkeypatch):
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

    runner.state["memo"] = [{"fact": FACT}]
    runner.state["browser"].observe.side_effect = rerender
    runner.state["browser"].act.return_value = {"executed": "x"}
    for _ in range(6):
        runner.command("tick")
        if runner.state["status"] == "blocked":
            break
    assert runner.state["blocked_reason"] == "decision budget"
    assert runner.state["answer"] == ANSWER
    assert runner.state["partial"] is True


def test_tick_exception_blocks_with_reason_and_partial(runner, memo_answer, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(side_effect=RuntimeError("daemon is gone")))
    runner.state["memo"] = [{"fact": FACT}]
    runner.state["status"] = "ready"
    runner.command("tick")
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "env:RuntimeError"
    assert runner.state["answer"] == ANSWER
    assert runner.state["partial"] is True


def test_terminal_without_memo_calls_no_model(runner, monkeypatch):
    field_text = Mock(return_value=(ANSWER, {"model": "t", "latency_ms": 1}))
    monkeypatch.setattr(loop, "field_text", field_text)
    runner.blocked_streak = 3
    runner.state["decision"] = {**decision("BLOCKED"), "operation": "BLOCKED"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state.get("answer") is None
    field_text.assert_not_called()


def _tab(monkeypatch):
    tab = Browser.__new__(Browser)
    tab.config = {"browser": "local"}
    tab.after_input = {"kind": "fill", "node": 1}
    ensured = []
    monkeypatch.setattr("jev_ultrafast.browser.ensure_daemon", lambda: ensured.append(True))
    return tab, ensured


def test_navigate_timeout_then_success(monkeypatch):
    tab, ensured = _tab(monkeypatch)
    tab.call = Mock(side_effect=[TimeoutError("ipc is slow"), None])
    tab.wait_until_ready = Mock()
    tab.evaluate = Mock(return_value="https://else.invalid/")
    result = tab.navigate("https://example.test/")
    assert result == {"executed": "navigate", "url": "https://example.test/"}
    assert tab.call.call_count == 2
    assert ensured == [True]
    assert tab.after_input is None


def test_navigate_timeout_after_commit_is_not_retried(monkeypatch):
    tab, ensured = _tab(monkeypatch)
    tab.call = Mock(side_effect=TimeoutError("ack was lost"))
    tab.wait_until_ready = Mock()
    tab.evaluate = Mock(return_value="https://example.test/")
    result = tab.navigate("https://example.test/")
    assert result == {"executed": "navigate", "url": "https://example.test/"}
    assert tab.call.call_count == 1
    assert ensured == []


def test_navigate_timeout_twice_records_a_failed_step(monkeypatch):
    tab, ensured = _tab(monkeypatch)
    tab.call = Mock(side_effect=TimeoutError("daemon is gone"))
    tab.wait_until_ready = Mock()
    tab.evaluate = Mock(side_effect=TimeoutError("daemon is gone"))
    result = tab.navigate("https://example.test/")
    assert result["executed"] == "navigate"
    assert result["navigated"] is False
    assert tab.call.call_count == 2
    assert ensured == [True]
    assert tab.after_input is None


def test_failed_navigate_step_is_noted(runner, monkeypatch):
    runner.state["page"]["actions"].append(
        {"id": "navigate", "kind": "navigate", "label": "Navigate to a URL from the goal"}
    )
    monkeypatch.setattr(
        loop, "field_text", Mock(return_value=("https://else.test/", {"model": "t", "latency_ms": 1}))
    )
    runner.state["browser"].navigate.return_value = {
        "executed": "navigate", "url": "https://else.test/", "navigated": False,
    }
    runner.state["decision"] = {**decision("navigate"), "operation": "NAVIGATE"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["note"] == "navigation failed"


def test_double_timeout_tick_blocks_with_partial(runner, monkeypatch):
    """The failed navigate step is recorded; the dead daemon then blocks the tick."""
    runner.state["page"]["actions"].append(
        {"id": "navigate", "kind": "navigate", "label": "Navigate to a URL from the goal"}
    )
    runner.state["memo"] = [{"fact": FACT}]
    runner.state["status"] = "ready"
    monkeypatch.setattr(
        loop, "choose", lambda *_args, **_kwargs: {**decision("navigate"), "operation": "NAVIGATE"}
    )
    calls = {"n": 0}

    def fake_field_text(_context, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "https://else.test/", {"model": "t", "latency_ms": 1}
        return ANSWER, {"model": "t", "latency_ms": 1}

    monkeypatch.setattr(loop, "field_text", fake_field_text)
    runner.state["browser"].navigate.return_value = {
        "executed": "navigate", "url": "https://example.test/", "navigated": False,
    }
    runner.state["browser"].observe.side_effect = TimeoutError("daemon is gone")
    runner.command("tick")
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "env:TimeoutError"
    assert runner.state["answer"] == ANSWER
    assert runner.state["partial"] is True
    assert runner.state["history"][-1]["note"] == "navigation failed"
