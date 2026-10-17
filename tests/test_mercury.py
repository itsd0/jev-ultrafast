"""Offline Mercury Decide adapter and Mercury text requests. No network."""

import pytest

from jev_ultrafast import model


class Response:
    def __init__(self, status, payload=None, retry_after=None):
        self.status_code = status
        self.is_error = status >= 400
        self._payload = payload or {}
        self.headers = {}
        if retry_after is not None:
            self.headers["retry-after"] = retry_after

    def json(self):
        return self._payload


def test_429_retries_with_retry_after_then_accepts(monkeypatch):
    sleeps = []
    calls = {"n": 0}

    def post(url, json, headers):
        calls["n"] += 1
        assert "offline-key" not in str(json)
        assert headers["Authorization"] == "Bearer offline-key"
        if calls["n"] == 1:
            return Response(429, retry_after="0")
        return Response(200, {"ok": True})

    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model.time, "sleep", lambda delay: sleeps.append(delay))
    assert model.post_json("https://api.inceptionlabs.ai/v1/decisions", "offline-key", {"model": "mercury-decide"}) == {
        "ok": True
    }
    assert calls["n"] == 2
    assert sleeps == [0]


def test_decide_retries_502_and_chat_does_not(monkeypatch):
    sleeps = []
    calls = {"n": 0}

    def post(url, json, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(502)
        return Response(200, {"ok": True})

    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model.time, "sleep", lambda delay: sleeps.append(delay))
    with pytest.raises(RuntimeError, match="HTTP 502"):
        model.post_json("https://api.inceptionlabs.ai/v1/chat/completions", "offline-key", {})
    assert calls["n"] == 1
    assert sleeps == []
    calls["n"] = 0
    assert model.post_json("https://api.inceptionlabs.ai/v1/decisions", "offline-key", {}) == {"ok": True}
    assert calls["n"] == 2
    assert sleeps == [0.5]


def test_status_without_retry_after_uses_backoff_and_stops(monkeypatch):
    sleeps = []
    calls = {"n": 0}

    def post(url, json, headers):
        calls["n"] += 1
        return Response(503)

    monkeypatch.setattr(model.CLIENT, "post", post)
    monkeypatch.setattr(model.time, "sleep", lambda delay: sleeps.append(delay))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        model.post_json("https://api.inceptionlabs.ai/v1/decisions", "offline-key", {})
    assert calls["n"] == model.MAX_POSTS
    assert sleeps == [0.5, 1, 2]


def test_text_request_uses_mercury_instant_json(monkeypatch, capsys):
    monkeypatch.setenv("INCEPTION_API_KEY", "test-key")
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("TEXT_MODEL", "mercury-2.5")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://api.inceptionlabs.ai/v1")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "instant")
    monkeypatch.delenv("TEXT_MODEL_MAX_TOKENS", raising=False)
    monkeypatch.setenv("JEV_DEBUG", "1")
    seen = {}

    def post(url, json, headers):
        seen["url"] = url
        seen["body"] = json
        return Response(
            200,
            {
                "choices": [{"message": {"content": '{"text":"Zurich"}'}}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 8,
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    monkeypatch.setattr(model.CLIENT, "post", post)
    value, meta = model.field_text({"goal": "Fly from Zurich"})
    assert value == "Zurich"
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["model"] == "mercury-2.5"
    assert seen["body"]["reasoning_effort"] == "instant"
    schema = seen["body"]["response_format"]
    assert schema["type"] == "json_schema"
    assert schema["json_schema"]["strict"] is True
    assert schema["json_schema"]["schema"]["properties"]["text"]["type"] == "string"
    assert seen["body"]["max_completion_tokens"] >= 1000
    assert "extra_body" not in seen["body"]
    assert meta["latency_ms"] >= 0
    err = capsys.readouterr().err
    assert "[jev:text]" in err
    assert "reasoning=3" in err
    assert "test-key" not in err


def test_text_reasoning_levels(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://api.inceptionlabs.ai/v1")
    monkeypatch.setenv("TEXT_MODEL", "mercury-2.5")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "high")
    seen = {}

    def post(url, json, headers):
        seen["effort"] = json["reasoning_effort"]
        return Response(200, {"choices": [{"message": {"content": '{"text":"London"}'}}], "usage": {}})

    monkeypatch.setattr(model.CLIENT, "post", post)
    assert model.field_text({"goal": "To London"})[0] == "London"
    assert seen["effort"] == "high"
    monkeypatch.setenv("TEXT_MODEL_REASONING", "none")
    model.field_text({"goal": "To London"})
    assert seen["effort"] == "instant"


def test_default_provider_is_typesafe(monkeypatch):
    monkeypatch.delenv("DECISION_PROVIDER", raising=False)
    assert model.decision_provider() == "typesafe"
    monkeypatch.setenv("DECISION_PROVIDER", "mercury")
    assert model.decision_provider() == "mercury"
