"""Beam server wiring: agent selection, model/effort, step detail. Offline."""

import json
import sqlite3

import pytest

from jev_ultrafast import server as hosted
from jev_ultrafast.agents import agent_class, agent_name, default_agent_factory
from jev_ultrafast.beam_agent import BeamAgent


def test_agent_selection_defaults_to_classic(monkeypatch):
    monkeypatch.delenv("JEV_AGENT", raising=False)
    assert agent_name() == "classic"
    assert agent_class().__name__ == "Agent"
    monkeypatch.setenv("JEV_AGENT", "new")
    assert agent_name() == "new"
    assert agent_class() is BeamAgent


def test_default_factory_builds_selected_agent(monkeypatch):
    class FakeBrowser:
        def __init__(self):
            self.config = {}

        def observe(self, screenshot=False):
            return {"url": "about:blank", "title": "", "actions": [], "text": ""}

    monkeypatch.setenv("JEV_LOG_LEVEL", "off")
    monkeypatch.setenv("JEV_AGENT", "new")
    agent = default_agent_factory("about:blank", "hi", browser=FakeBrowser())
    assert isinstance(agent, BeamAgent)


def test_submit_message_validates_model_and_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_LOG_LEVEL", "off")
    store = hosted.Store(tmp_path / "runs.db")
    runner = hosted.SessionRunner(store, agent_factory=lambda *a, **k: None)
    session_id = runner.create_session("local")
    with pytest.raises(ValueError, match="effort"):
        runner.submit_message(session_id, "hi", effort="ultra")
    with pytest.raises(ValueError, match="model"):
        runner.submit_message(session_id, "hi", model="x" * 201)


def test_create_agent_applies_message_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_LOG_LEVEL", "off")
    made = {}

    class FakeAgent:
        def __init__(self, url, goal, browser=None, background=""):
            made["goal"] = goal
            self.config = {}
            self.browser = browser
            self.trace = None

    store = hosted.Store(tmp_path / "runs.db")
    runner = hosted.SessionRunner(store, agent_factory=FakeAgent)
    session_id = runner.create_session("local")
    message_id = store.create_message(session_id, "hi")
    store.update_message(message_id, model="Beam-Custom", effort="high")
    agent = runner._create_agent(session_id, message_id, "hi", object(), None)
    assert agent.config["model"] == "Beam-Custom"
    assert agent.config["effort"] == "high"
    assert made["goal"] == "hi"


def test_steps_carry_detail(tmp_path):
    store = hosted.Store(tmp_path / "runs.db")
    session_id = store.create_session("t", "local")
    message_id = store.create_message(session_id, "hi")
    store.update_message(message_id, run_id=message_id, status="done")
    detail = json.dumps({"evaluation": "Verdict: Success", "thinking_ms": 12})
    store.add_step(message_id, 1, "CLICK", "Next", "https://x.test/", 3, "", detail)
    session = store.get_session(session_id)
    assert session["messages"][0]["steps"][0]["detail"] == detail


def test_config_accepts_beam_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_MODEL", raising=False)
    monkeypatch.delenv("JEV_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("JEV_AGENT", raising=False)
    store = hosted.Store(tmp_path / "runs.db")
    runner = hosted.SessionRunner(store, agent_factory=lambda *a, **k: None)
    view = runner.apply_config({"agent": "new", "model_default": "Beam-X", "effort_default": "high"})
    assert view["agent"] == "new"
    assert view["model_default"] == "Beam-X"
    assert view["effort_default"] == "high"
    with pytest.raises(ValueError, match="effort_default"):
        runner.apply_config({"effort_default": "ultra"})
    with pytest.raises(ValueError, match="agent must be"):
        runner.apply_config({"agent": "other"})


def test_store_migrates_beam_columns(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT NOT NULL)")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
    conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, n INTEGER)")
    conn.execute(
        "CREATE TABLE steps (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
        " n INTEGER NOT NULL, operation TEXT, target_label TEXT, url TEXT, ms INTEGER, note TEXT)"
    )
    conn.commit()
    conn.close()
    store = hosted.Store(db)
    messages = {row[1] for row in store.conn.execute("PRAGMA table_info(messages)")}
    steps = {row[1] for row in store.conn.execute("PRAGMA table_info(steps)")}
    assert {"model", "effort"} <= messages
    assert "detail" in steps
