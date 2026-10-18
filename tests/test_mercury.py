"""Offline Mercury Decide adapter and Mercury text requests. No network."""

import json
import logging

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
        self.text = json.dumps(self._payload)

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


def test_text_request_uses_mercury_instant_json(monkeypatch, caplog):
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
    with caplog.at_level(logging.DEBUG, logger="jev"):
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
    assert "[jev:text]" in caplog.text
    assert "reasoning=3" in caplog.text
    assert "test-key" not in caplog.text


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


def _text_env(monkeypatch):
    monkeypatch.setenv("INCEPTION_API_KEY", "test-key")
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("TEXT_MODEL", "mercury-2.5")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://api.inceptionlabs.ai/v1")
    monkeypatch.setenv("TEXT_MODEL_REASONING", "instant")


def test_http_error_logs_a_redacted_body(monkeypatch, caplog):
    monkeypatch.setenv("INCEPTION_API_KEY", "offline-key")
    body = {"error": {"message": "rejected offline-key", "code": "content_filter"}}
    monkeypatch.setattr(model.CLIENT, "post", lambda *args, **kwargs: Response(400, body))
    with caplog.at_level(logging.WARNING, logger="jev"):
        with pytest.raises(model.ModelHTTPError) as error:
            model.post_json("https://api.inceptionlabs.ai/v1/chat/completions", "offline-key", {})
    assert error.value.status_code == 400
    assert error.value.code == "content_filter"
    assert "content_filter" in caplog.text
    assert "[redacted]" in caplog.text
    assert "offline-key" not in caplog.text


def test_content_filter_retries_without_page_text(monkeypatch):
    _text_env(monkeypatch)
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        if len(calls) == 1:
            raise model.ModelHTTPError(400, json.dumps({"error": {"code": "content_filter"}}))
        return {"choices": [{"message": {"content": '{"text":"Gödel\\u2019s incompleteness theorems"}'}}]}

    monkeypatch.setattr(model, "post_json", post)
    context = {
        "goal": "Find the article",
        "field": {"label": "Search Wikipedia", "role": "searchbox", "value": ""},
        "page": {"title": "Wikipedia", "text": "live news that trips the filter"},
        "recent_actions": [],
    }
    value, _meta = model.field_text(context)
    assert value == "Gödel’s incompleteness theorems"
    assert len(calls) == 2
    retried = json.loads(calls[1]["messages"][1]["content"])
    assert retried["goal"] == context["goal"]
    assert retried["page"] == {"title": "Wikipedia"}
    assert "trips the filter" not in calls[1]["messages"][1]["content"]


def test_non_filter_http_error_still_stops(monkeypatch):
    _text_env(monkeypatch)
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        raise model.ModelHTTPError(400, json.dumps({"error": {"code": "invalid_request_error"}}))

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(model.ModelHTTPError, match="HTTP 400"):
        model.field_text({"goal": "Find a flight"})
    assert len(calls) == 1


def test_persistent_content_filter_skips_the_step(monkeypatch, capsys):
    _text_env(monkeypatch)
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        raise model.ModelHTTPError(400, json.dumps({"error": {"code": "content_filter"}}))

    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="content-filtered"):
        model.field_text({"goal": "Find the article", "page": {"title": "Wikipedia", "text": "news"}})
    assert len(calls) == 2
    assert "[redacted]" not in capsys.readouterr().err
