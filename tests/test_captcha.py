"""Generic captcha/sorry walls block the host; the run leaves, never solves. No paid APIs."""

import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import detect_captcha, fingerprint
from jev_ultrafast.trace import Trace, diagnose_trace, get_logger


def page(**overrides):
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "status": 200,
        "cross_frames": [],
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state.update(overrides)
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
    agent.trace = Trace(run_id="test-captcha", level=0)
    board = page()
    agent.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=board)),
        "page": board,
        "decision": None,
        "goal": "Find the population of Bangkok",
        "history": [],
        "decisions": [],
        "status": "ready",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return agent


@pytest.fixture
def isolated_hosts(monkeypatch):
    monkeypatch.setattr(loop, "_HOST_BLOCKS", {})


def test_detect_sorry_url():
    found = detect_captcha(page(url="https://www.google.com/sorry/index?continue=x", text="Sorry"))
    assert found and "captcha path" in found


def test_detect_recaptcha_iframe():
    found = detect_captcha(page(
        url="https://shop.test/checkout",
        text="Checkout",
        cross_frames=[{"url": "https://www.google.com/recaptcha/api2/anchor?k=x"}],
    ))
    assert found and "challenge iframe" in found


def test_detect_unusual_traffic_text():
    found = detect_captcha(page(
        url="https://www.google.com/search?q=x",
        title="Before you continue",
        text="Our systems have detected unusual traffic from your computer network.",
    ))
    assert found and "challenge text" in found


def test_detect_status_and_robot_text():
    assert detect_captcha(page(status=429)) == "http 429"
    assert "challenge text" in detect_captcha(page(text="Please verify you are human to continue"))
    assert "challenge text" in detect_captcha(page(text="Verify you're human"))
    assert "challenge text" in detect_captcha(page(text="I'm not a robot"))
    assert detect_captcha(page()) is None


def test_detect_ignores_data_urls_and_press_hold_buttons():
    assert detect_captcha(page(url="data:text/html,<p>captcha here</p>", text="captcha here")) is None
    assert detect_captcha(page(text="Press and hold to confirm")) is None


def test_blocked_host_backoff_then_one_retry(isolated_hosts):
    state = {}
    entry = loop.block_host(state, "wall.test", now=1000.0)
    assert 20 <= entry["until"] - 1000.0 <= 40
    assert loop.host_blocked(state, "wall.test", now=1000.0) is True
    assert loop.take_host_retry(state, "wall.test", now=1000.0) is False
    assert loop.take_host_retry(state, "wall.test", now=entry["until"]) is True
    assert loop.host_blocked(state, "wall.test", now=entry["until"] + 1) is True
    assert loop.take_host_retry(state, "wall.test", now=entry["until"] + 1) is False


def test_process_cache_skips_the_host_for_other_runs(isolated_hosts):
    loop.block_host({}, "wall.test", now=1000.0)
    assert loop.host_blocked({}, "wall.test", now=1000.0) is True
    assert loop.take_host_retry({}, "wall.test", now=1000.0) is False
    assert loop.host_blocked({}, "wall.test", now=1000.0 + 30 * 60 + 1) is False


def test_forcing_stays_while_on_a_blocked_host(runner, isolated_hosts, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e1")))
    runner.state["page"] = page(url="https://wall.test/sorry")
    loop.block_host(runner.state, "wall.test")
    runner.command("predict")
    assert runner.state["force_navigate"] is True
    assert loop.on_blocked_host(runner.state) is True
    runner.state["page"] = page(url="https://example.test/")
    runner.command("predict")
    assert runner.state["force_navigate"] is False


def test_captcha_page_blocks_backs_and_forces_without_a_decision(runner, isolated_hosts, monkeypatch):
    choose = Mock(return_value=decision("e1"))
    monkeypatch.setattr(loop, "choose", choose)
    sorry = page(url="https://wall.test/sorry/index", text="Sorry, unusual traffic")
    sorry["captcha_reason"] = detect_captcha(sorry)
    assert sorry["captcha_reason"]
    runner.state["page"] = sorry
    runner.state["browser"].observe.return_value = page(url="https://wall.test/search")
    emitted = []
    monkeypatch.setattr(runner.trace, "emit", lambda event, **fields: emitted.append((event, fields)))
    runner.command("predict")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    assert runner.state["decisions"] == []
    choose.assert_not_called()
    assert "wall.test" in (runner.state.get("blocked_hosts") or {})
    assert runner.state["force_navigate"] is True
    runner.state["browser"].go_back.assert_called_once()
    assert runner.state["page"]["url"] == "https://wall.test/search"
    captcha = [fields for event, fields in emitted if fields.get("type") == "captcha"]
    assert len(captcha) == 1 and captcha[0]["host"] == "wall.test"


def test_captcha_records_a_red_step(runner, isolated_hosts, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e1")))
    sorry = page(url="https://wall.test/sorry/index", text="Sorry, unusual traffic")
    sorry["captcha_reason"] = detect_captcha(sorry)
    runner.state["page"] = sorry
    runner.state["browser"].observe.return_value = page(url="https://wall.test/search")
    runner.command("predict")
    assert len(runner.state["history"]) == 1
    entry = runner.state["history"][0]
    assert entry["step"] == 1
    assert entry["operation"] == "CAPTCHA"
    assert entry["action"] == "Blocked by a CAPTCHA on wall.test"
    assert entry["url"] == "https://wall.test/sorry/index"
    assert entry["kind"] == "captcha"
    assert runner.state["wall_hosts"] == ["wall.test"]
    assert runner.state["loop_window"][-1]["kind"] == "captcha"


def test_cloud_restart_wanted_once_per_run(runner, isolated_hosts, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e1")))
    runner.state["browser"].config = {"browser": "cloud"}
    sorry = page(url="https://wall.test/sorry/index", text="Sorry, unusual traffic")
    sorry["captcha_reason"] = detect_captcha(sorry)
    runner.state["page"] = sorry
    runner.command("predict")
    assert runner.state["cloud_restart_wanted"] == "wall.test"
    runner.state["cloud_restarted"] = True
    runner.state["cloud_restart_wanted"] = None
    runner.state["force_navigate"] = False
    runner.state["page"] = sorry
    runner.command("predict")
    assert runner.state.get("cloud_restart_wanted") is None


def test_local_wall_wants_no_restart(runner, isolated_hosts, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e1")))
    sorry = page(url="https://wall.test/sorry/index", text="Sorry, unusual traffic")
    sorry["captcha_reason"] = detect_captcha(sorry)
    runner.state["page"] = sorry
    runner.command("predict")
    assert runner.state.get("cloud_restart_wanted") is None


def test_terminal_reason_names_the_walls(runner):
    runner.state["wall_hosts"] = ["a.test", "b.test"]
    runner._terminal(runner.state, "gave up", 1)
    assert runner.state["blocked_reason"] == "gave up; hit CAPTCHA walls on a.test, b.test"
    runner.state["wall_hosts"] = []
    runner._terminal(runner.state, "gave up", 1)
    assert runner.state["blocked_reason"] == "gave up"


def test_captcha_handler_is_skipped_while_forcing(runner, isolated_hosts, monkeypatch):
    choose = Mock(return_value=decision("e1"))
    monkeypatch.setattr(loop, "choose", choose)
    sorry = page(url="https://wall.test/sorry/index", text="Sorry, unusual traffic")
    sorry["captcha_reason"] = detect_captcha(sorry)
    runner.state["page"] = sorry
    runner.state["force_navigate"] = True
    runner.command("predict")
    choose.assert_called_once()
    assert runner.state["decision"]["choice"] == "e1"


def _event(name, **data):
    return {"seq": 0, "step": 1, "kind": name, "ts_ms": 0, "data": data}


def test_diagnose_ranks_captcha_env_first():
    trace = [
        _event("verify", kind="fill", success=False),
        _event("verify", kind="fill", success=False),
        _event("guard", type="loop", pattern="A/B"),
        _event("guard", type="captcha", host="wall.test"),
        _event("end", status="blocked", blocked_reason="decision budget"),
    ]
    assert diagnose_trace(trace)[0] == "captcha"
    env = [
        _event("verify", kind="fill", success=False),
        _event("verify", kind="fill", success=False),
        _event("end", status="blocked", blocked_reason="env:TimeoutError"),
    ]
    assert diagnose_trace(env)[0] == "environment/API error"
    transport = [
        _event("error", type="TimeoutError", message="ipc timed out"),
        _event("end", status="blocked", blocked_reason="decision budget"),
    ]
    assert diagnose_trace(transport)[0] == "environment/API error"


def test_captcha_tick_ends_without_acting(runner, isolated_hosts, monkeypatch):
    """A full tick on a sorry page: red step, forced way out, run keeps going."""
    choose = Mock(return_value=decision("e1"))
    monkeypatch.setattr(loop, "choose", choose)
    sorry = page(url="https://wall.test/sorry/index", text="Sorry, unusual traffic")
    sorry["captcha_reason"] = detect_captcha(sorry)
    assert sorry["captcha_reason"]
    runner.state["page"] = sorry
    runner.state["browser"].observe.return_value = page(url="https://wall.test/search")
    emitted = []
    monkeypatch.setattr(runner.trace, "emit", lambda event, **fields: emitted.append((event, fields)))
    snapshot = runner.command("tick")
    assert snapshot["status"] not in {"done", "blocked", "failed"}
    assert snapshot["history"][-1]["operation"] == "CAPTCHA"
    assert runner.state["force_navigate"] is True
    assert runner.state["decision"] is None
    choose.assert_not_called()
    assert "_captcha_tick" not in runner.state
    assert [fields for event, fields in emitted if event == "error"] == []


def test_captcha_tick_wants_cloud_restart_while_running(runner, isolated_hosts, monkeypatch):
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e1")))
    runner.state["browser"].config = {"browser": "cloud"}
    sorry = page(url="https://wall.test/sorry/index", text="Sorry, unusual traffic")
    sorry["captcha_reason"] = detect_captcha(sorry)
    runner.state["page"] = sorry
    runner.state["browser"].observe.return_value = page(url="https://wall.test/search")
    snapshot = runner.command("tick")
    assert snapshot["status"] not in {"done", "blocked", "failed"}
    assert runner.state["cloud_restart_wanted"] == "wall.test"
