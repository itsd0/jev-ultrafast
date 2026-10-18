"""SAVE_FILE operation and session files. No browsers, no paid APIs."""

import json
import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast.browser import bind_save_file, fingerprint
from jev_ultrafast.model import (
    action_space,
    parse_save_file,
    save_file_context,
    validate_save_filename,
    write_saved_file,
)
from jev_ultrafast.trace import Trace, get_logger

SAVE = {"id": "save_file", "kind": "save_file", "label": "Save collected facts to a file"}


def _result(payload):
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


def test_parse_save_file_accepts_filename_and_content():
    parsed = parse_save_file(_result({"filename": "results.csv", "content": "a,b\n1,2\n"}))
    assert parsed == {"filename": "results.csv", "content": "a,b\n1,2\n"}


@pytest.mark.parametrize(
    "payload",
    [
        {"filename": "../evil.txt", "content": "x"},
        {"filename": "sub/dir.txt", "content": "x"},
        {"filename": ".hidden", "content": "x"},
        {"filename": "ok.txt", "content": "   "},
        {"filename": "ok.txt", "content": "x" * 200_001},
        {"filename": "ok.txt", "content": "x", "extra": 1},
        {"filename": "ok.txt"},
    ],
)
def test_parse_save_file_rejects_bad_shapes(payload):
    with pytest.raises(ValueError):
        parse_save_file(_result(payload))


def test_validate_save_filename_allows_plain_names():
    assert validate_save_filename("results.csv") == "results.csv"
    assert validate_save_filename("my file 2.JSON") == "my file 2.JSON"


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a\\b", ".hidden", "x" * 101, None, 42])
def test_validate_save_filename_rejects_paths(name):
    with pytest.raises(ValueError):
        validate_save_filename(name)


def test_write_saved_file_writes_inside_root(tmp_path):
    name = write_saved_file(tmp_path / "files", "pop.csv", "city,pop\n")
    assert (tmp_path / "files" / name).read_text() == "city,pop\n"
    with pytest.raises(ValueError):
        write_saved_file(tmp_path / "files", "../escape.txt", "x")
    assert not (tmp_path / "escape.txt").exists()


def test_save_file_context_carries_facts():
    page = {"url": "https://example.test/", "title": "T"}
    context = save_file_context("goal", [{"fact": "a"}, {"fact": "b"}], [{"action": "CLICK x"}], page)
    assert context["facts"] == ["a", "b"]
    assert context["current_url"] == "https://example.test/"
    assert context["recent_actions"] == ["CLICK x"]


def test_action_space_exposes_save_file_control():
    _elements, _targets, controls = action_space([dict(SAVE)])
    assert controls["SAVE_FILE"]["id"] == "save_file"


def test_bind_save_file_needs_a_files_dir():
    state = {"url": "u", "text": "", "actions": [], "scroll": {"y": 0}}
    assert bind_save_file(dict(state), None)["actions"] == []
    assert bind_save_file(dict(state), "")["actions"] == []
    offered = bind_save_file(dict(state), "/tmp/files")
    assert [action["kind"] for action in offered["actions"]] == ["save_file"]
    assert offered["fingerprint"] != fingerprint(state)
    again = bind_save_file(offered, "/tmp/files")
    assert [action["kind"] for action in again["actions"]] == ["save_file"]


def test_configure_downloads_routes_context(monkeypatch, tmp_path):
    import jev_ultrafast.browser as browser_module

    calls = []
    monkeypatch.setattr(
        browser_module, "cdp", lambda method, **params: calls.append((method, params)) or {}
    )
    browser = browser_module.Browser.__new__(browser_module.Browser)
    browser.context = "ctx-1"
    browser.configure_downloads(tmp_path / "files")
    assert calls == [(
        "Browser.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": str(tmp_path / "files"), "browserContextId": "ctx-1"},
    )]


def test_configure_downloads_fails_soft(monkeypatch, tmp_path):
    import jev_ultrafast.browser as browser_module

    def boom(method, **params):
        raise RuntimeError("unsupported")

    monkeypatch.setattr(browser_module, "cdp", boom)
    browser = browser_module.Browser.__new__(browser_module.Browser)
    browser.context = None
    browser.configure_downloads(tmp_path)  # Must not raise.


def _page(**overrides):
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [dict(SAVE), {"id": "wait", "kind": "wait", "label": "Wait"}],
    }
    state.update(overrides)
    state["fingerprint"] = fingerprint(state)
    return state


@pytest.fixture
def runner(tmp_path):
    agent = loop.Agent.__new__(loop.Agent)
    agent.screenshots = False
    agent.pending_text = None
    agent.blocked_streak = 0
    agent.log = get_logger("agent")
    agent.trace = Trace(run_id="test-save-file", level=0)
    board = _page()
    agent.state = {
        "browser": Mock(
            fresh=Mock(return_value=True),
            observe=Mock(return_value=board),
            config={"files_dir": str(tmp_path / "files")},
        ),
        "page": board,
        "decision": {
            "choice": "save_file",
            "operation": "SAVE_FILE",
            "target": None,
            "confidence": 1.0,
            "probabilities": {"save_file": 1.0},
            "latency_ms": 10,
            "usage": {},
        },
        "goal": "List the top HN stories",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
        "memo": [{"fact": "1. Example story (100 points)"}],
    }
    return agent


def test_save_branch_writes_file_and_notes_it(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        loop,
        "field_text",
        Mock(return_value=(
            {"filename": "stories.csv", "content": "title,points\nExample,100\n"},
            {"model": "t", "latency_ms": 1},
        )),
    )
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert (tmp_path / "files" / "stories.csv").read_text() == "title,points\nExample,100\n"
    last = runner.state["history"][-1]
    assert last["operation"] == "SAVE_FILE"
    assert last["note"] == "saved stories.csv"
    assert last["text"] == "saved stories.csv"


def test_save_branch_skips_when_nothing_collected(runner, monkeypatch, tmp_path):
    runner.state["memo"] = []
    field_text = Mock()
    monkeypatch.setattr(loop, "field_text", field_text)
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    field_text.assert_not_called()
    assert runner.state["history"][-1]["note"] == "nothing to save yet"
    assert not (tmp_path / "files").exists()


def test_save_branch_skips_bad_filename(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(
        loop,
        "field_text",
        Mock(return_value=(
            {"filename": "../evil.txt", "content": "x"},
            {"model": "t", "latency_ms": 1},
        )),
    )
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert "invalid file name" in runner.state["history"][-1]["note"]
    assert not (tmp_path / "evil.txt").exists()


def test_suppression_hides_save_until_facts_exist():
    page = _page()
    reasons = loop.suppression_reasons({"memo": []}, page)
    assert reasons[loop.noop_key(SAVE)] == "nothing collected yet"
    assert loop.suppression_reasons({"memo": [{"fact": "x"}]}, page) == {}
