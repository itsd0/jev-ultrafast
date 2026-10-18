"""NAVIGATE knows where it is and where it has been. No paid APIs."""

import time
from unittest.mock import Mock
from urllib.parse import quote_plus

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint
from jev_ultrafast.model import navigate_context
from jev_ultrafast.trace import Trace, get_logger

NAVIGATE = {"id": "navigate", "kind": "navigate", "label": "Navigate to a URL from the goal"}


def page(**overrides):
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            dict(NAVIGATE),
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state.update(overrides)
    state["fingerprint"] = fingerprint(state)
    return state


def decision(action="navigate"):
    return {
        "choice": action,
        "operation": "NAVIGATE",
        "target": None,
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
    agent.trace = Trace(run_id="test-navigate", level=0)
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
def isolated_hosts(monkeypatch):
    monkeypatch.setattr(loop, "_HOST_BLOCKS", {})


def test_navigate_context_names_current_todo_and_seen():
    context = navigate_context(
        "goal", "current weather", [{"fact": "pop 8M"}], page(), [],
        visited=["a.test/x", "b.test"], failed=["c.test"], blocked={"wall.test"},
    )
    assert context["current_url"] == "https://example.test/"
    assert context["todo"] == "current weather"
    assert context["facts"] == ["pop 8M"]
    assert context["visited_urls"] == ["a.test/x", "b.test"]
    assert context["failed_urls"] == ["c.test"]
    assert context["blocked_hosts"] == ["wall.test"]


def test_current_url_is_replaced_by_search_fallback(runner, monkeypatch, isolated_hosts):
    seen = {}

    def fake_field_text(context, **kwargs):
        seen["context"] = context
        return "https://example.test/", {"model": "t", "latency_ms": 1}

    monkeypatch.setattr(loop, "field_text", fake_field_text)
    runner.state["browser"].navigate = Mock(return_value={"executed": "navigate"})
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    fallback = runner.state["browser"].navigate.call_args[0][0]
    assert fallback.startswith("https://www.bing.com/search?q=")
    assert quote_plus("the population of Bangkok") in fallback  # UI verb "Find" stripped
    assert runner.state["history"][-1]["text"] == fallback
    assert runner.state["history"][-1]["note"] == "used search fallback"
    assert seen["context"]["current_url"] == "https://example.test/"
    assert runner.state["visited_urls"] == ["example.test"]


def test_blocked_engine_is_skipped_for_the_next_one(runner, monkeypatch, isolated_hosts):
    loop.block_host(runner.state, "www.bing.com")
    monkeypatch.setattr(
        loop, "field_text", Mock(return_value=("https://example.test/", {"model": "t", "latency_ms": 1}))
    )
    runner.state["browser"].navigate = Mock(return_value={"executed": "navigate"})
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    fallback = runner.state["browser"].navigate.call_args[0][0]
    assert fallback.startswith("https://duckduckgo.com/?q=")


def test_no_unblocked_engine_skips_the_step(runner, monkeypatch, isolated_hosts):
    for host in ("www.bing.com", "duckduckgo.com", "search.brave.com", "www.google.com"):
        loop.block_host(runner.state, host)
        runner.state["blocked_hosts"][host]["retries"] = 1
    monkeypatch.setattr(
        loop, "field_text", Mock(return_value=("https://example.test/", {"model": "t", "latency_ms": 1}))
    )
    runner.state["browser"].navigate = Mock(return_value={"executed": "navigate"})
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].navigate.assert_not_called()
    assert runner.state["history"][-1]["note"] == "no unblocked navigation target"


def test_page_identity_keeps_query_but_drops_tokens():
    assert loop.page_identity("https://news.test/?p=3") != loop.page_identity("https://news.test/?p=4")
    assert loop.page_identity("https://s.test/?b=2&a=1") == loop.page_identity("https://s.test/?a=1&b=2")
    token = "sei=" + "A" * 20 + "&ved=" + "z" * 24
    assert loop.page_identity(f"https://g.test/search?q=lisbon&{token}") == "g.test/search?q=lisbon"
    assert loop.page_identity("https://g.test/?utm_source=x&p=2") == "g.test?p=2"
    assert loop.page_identity("https://example.test/") == "example.test"
    assert loop.page_identity("not a url") == ""
    assert loop.page_identity("") == ""


def test_back_pagination_proposal_is_accepted(runner, isolated_hosts):
    runner.state["page"]["url"] = "https://news.test/?p=4"
    runner.state["visited_urls"] = ["news.test?p=2", "news.test?p=3", "news.test?p=4"]
    accepted = runner._validated_navigate_url(runner.state, "https://news.test/?p=3", "todo", 4)
    assert accepted == "https://news.test/?p=3"
    assert runner.state["revisited_once"] == ["news.test?p=3"]


def test_revisit_policy_current_failed_and_twice_visited(runner, isolated_hosts):
    runner.state["page"]["url"] = "https://news.test/?p=4"
    runner.state["visited_urls"] = ["news.test?p=3"]
    runner.state["failed_urls"] = ["shop.test/bad"]
    current = runner._validated_navigate_url(runner.state, "https://news.test/?p=4", "todo", 1)
    assert current.startswith("https://www.bing.com/search?q=")
    failed = runner._validated_navigate_url(runner.state, "https://shop.test/bad", "todo", 1)
    assert failed.startswith("https://www.bing.com/search?q=")
    once = runner._validated_navigate_url(runner.state, "https://news.test/?p=3", "todo", 1)
    assert once == "https://news.test/?p=3"
    twice = runner._validated_navigate_url(runner.state, "https://news.test/?p=3", "todo", 2)
    assert twice.startswith("https://www.bing.com/search?q=")


def test_fallback_query_has_no_ui_words(isolated_hosts):
    state = {}
    engines = ["https://www.bing.com/search?q={q}"]
    url = loop.search_fallback("Click the 'More' link to navigate to the second page.", engines, state)
    query = url.split("?q=", 1)[1].lower()
    assert "click" not in query and "link" not in query and "navigate" not in query and "page" not in query
    assert "more" in query and "second" in query
    assert loop.search_fallback("Click link navigate page", engines, state) is None
    todo = "Click link navigate page"
    assert loop.search_fallback(todo, engines, state, goal="Bangkok weather") is not None


def test_blocked_host_proposal_uses_fallback(runner, monkeypatch, isolated_hosts):
    loop.block_host(runner.state, "wall.test")
    monkeypatch.setattr(
        loop, "field_text", Mock(return_value=("https://wall.test/page", {"model": "t", "latency_ms": 1}))
    )
    runner.state["browser"].navigate = Mock(return_value={"executed": "navigate"})
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["browser"].navigate.call_args[0][0].startswith("https://www.bing.com/search?q=")


def test_forced_mode_executes_navigate_directly(runner, monkeypatch, isolated_hosts):
    choose = Mock(return_value=decision("e1"))
    monkeypatch.setattr(loop, "choose", choose)
    runner.state["status"] = "ready"
    loop.block_host(runner.state, "example.test")
    runner.command("predict")
    choose.assert_not_called()
    assert runner.state["decision"]["operation"] == "NAVIGATE"
    assert runner.state["decision"]["choice"] == "navigate"
    assert runner.state["status"] == "predicted"


def test_forced_mode_overrides_one_blocked(runner, monkeypatch, isolated_hosts):
    monkeypatch.setattr(
        loop, "field_text", Mock(return_value=("https://else.test/", {"model": "t", "latency_ms": 1}))
    )
    runner.state["browser"].navigate = Mock(return_value={"executed": "navigate"})
    runner.state["force_navigate"] = True
    runner.blocked_streak = 3
    runner.state["decision"] = {**decision("BLOCKED"), "operation": "BLOCKED"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].navigate.assert_called_once_with("https://else.test/")
    assert runner.state["status"] == "ready"
    assert runner.state["forced_override_used"] is True
    runner.blocked_streak = 3
    runner.state["decision"] = {**decision("BLOCKED"), "operation": "BLOCKED"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "gave up"


def test_off_topic_landing_is_marked_failed(runner, monkeypatch, isolated_hosts):
    monkeypatch.setattr(
        loop, "field_text", Mock(return_value=("https://else.test/pop", {"model": "t", "latency_ms": 1}))
    )
    runner.state["browser"].navigate = Mock(return_value={"executed": "navigate"})
    runner.state["subgoals"] = [{"id": 0, "text": "current weather"}]
    runner.state["browser"].observe.return_value = page(
        url="https://else.test/pop", title="Population", text="Bangkok population 8M",
    )
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["failed_urls"] == ["else.test/pop"]
    assert "off-topic landing" in (runner.state["history"][-1]["note"] or "")
    assert runner.state["loop_window"][-1]["progress"] is False


def test_on_topic_landing_counts_progress(runner, monkeypatch, isolated_hosts):
    monkeypatch.setattr(
        loop, "field_text", Mock(return_value=("https://else.test/wx", {"model": "t", "latency_ms": 1}))
    )
    runner.state["browser"].navigate = Mock(return_value={"executed": "navigate"})
    runner.state["subgoals"] = [{"id": 0, "text": "current weather"}]
    runner.state["browser"].observe.return_value = page(
        url="https://else.test/wx", title="Weather", text="Bangkok current weather 31C",
    )
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state.get("failed_urls") in (None, [])
    assert runner.state["loop_window"][-1]["progress"] is True


def test_override_flag_resets_when_force_clears(runner):
    runner.state["force_navigate"] = False
    runner.state["forced_override_used"] = True
    runner._update_suppression(runner.state)
    assert "forced_override_used" not in runner.state


def test_default_engines_produce_valid_urls():
    from jev_ultrafast.browser import validate_navigate_url
    from jev_ultrafast.config import parse_search_engines

    engines = parse_search_engines()
    assert engines[0] == "https://www.bing.com/search?q={q}"
    assert engines[-1] == "https://www.google.com/search?q={q}"
    for template in engines:
        validate_navigate_url(template.format(q="test+query"))


def test_search_engines_env_override(monkeypatch):
    from jev_ultrafast.config import parse_search_engines, resolve_config

    monkeypatch.setenv("JEV_SEARCH_ENGINES", "https://a.test/?q={q} , https://b.test/s?q={q}")
    assert parse_search_engines() == ["https://a.test/?q={q}", "https://b.test/s?q={q}"]
    assert resolve_config()["search_engines"] == ["https://a.test/?q={q}", "https://b.test/s?q={q}"]
    assert resolve_config(search_engines=["https://c.test/?q={q}"])["search_engines"] == [
        "https://c.test/?q={q}"
    ]
