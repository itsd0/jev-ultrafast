"""Vision clicks stay off unless JEV_VISION is enabled, and points must be confirmed."""

from jev_ultrafast.agent import Agent
from jev_ultrafast.config import resolve_config
from jev_ultrafast.vision import confirm_point, needs_vision

CANVAS = {"tag": "canvas", "rect": {"x": 0, "y": 0, "w": 80, "h": 80}}
PAGE = {"actions": [], "w": 100, "h": 100, "surfaces": [CANVAS], "screenshot": "abc", "title": "Draw"}


def test_vision_is_off_by_default(monkeypatch):
    monkeypatch.delenv("JEV_VISION", raising=False)
    assert resolve_config()["vision"] is False
    monkeypatch.setenv("JEV_VISION", "1")
    assert resolve_config()["vision"] is True
    assert resolve_config(vision=False)["vision"] is False
    agent = Agent.__new__(Agent)
    agent.config = {"vision": False}
    assert agent._maybe_vision({"page": PAGE, "goal": "click the canvas", "browser": None}) is None


def test_vision_runs_only_without_controls_or_when_canvas_covers_the_page():
    assert needs_vision({"actions": [], "w": 100, "h": 100, "surfaces": []}) is True
    assert needs_vision({"actions": [{"kind": "click"}], "w": 100, "h": 100, "surfaces": []}) is False
    mostly = {"actions": [{"kind": "click"}], "w": 100, "h": 100, "surfaces": [
        {"tag": "canvas", "rect": {"x": 0, "y": 0, "w": 80, "h": 80}},
    ]}
    assert needs_vision(mostly) is True


def test_point_must_be_confirmed_inside_a_surface():
    assert confirm_point({"x": 10, "y": 10, "confirm": True}, PAGE)["surface"] == "canvas"
    assert confirm_point({"x": 10, "y": 10}, PAGE) is None
    assert confirm_point({"x": 10, "y": 10, "confirm": False}, PAGE) is None
    assert confirm_point({"x": 150, "y": 10, "confirm": True}, PAGE) is None
    assert confirm_point({"x": 90, "y": 90, "confirm": True}, PAGE) is None


def test_enabled_vision_returns_a_coordinate_decision(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    agent = Agent.__new__(Agent)
    agent.config = {"vision": True}

    def post(_url, _key, body):
        assert "data:image/jpeg;base64,abc" in str(body)
        return {"choices": [{"message": {"content": '{"x": 10, "y": 12, "confirm": true}'}}]}

    monkeypatch.setattr("jev_ultrafast.agent.post_json", post)
    decision = agent._maybe_vision({"page": dict(PAGE), "goal": "click", "browser": None})
    assert decision["choice"] == "VISION_CLICK"
    assert decision["vision"] == {"x": 10.0, "y": 12.0, "surface": "canvas"}
    assert "selector" not in str(decision)


def test_rejected_point_falls_through(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    agent = Agent.__new__(Agent)
    agent.config = {"vision": True}
    monkeypatch.setattr(
        "jev_ultrafast.agent.post_json",
        lambda url, key, body: {"choices": [{"message": {"content": '{"x": 99, "y": 99, "confirm": true}'}}]},
    )
    assert agent._maybe_vision({"page": dict(PAGE), "goal": "click", "browser": None}) is None
