"""Reflection provider: retries, length growth, refusal, trace fields. No network."""

import json

import pytest

from jev_ultrafast.llm.base import ProviderError, RefusalError
from jev_ultrafast.llm.reflection import (
    MAX_COMPLETION_TOKENS,
    ProviderHTTPError,
    ReflectionProvider,
    retry_delay,
    should_retry,
)
from jev_ultrafast.llm.schema import PING_SCHEMA, response_format

FORMAT = response_format("BeamPing", PING_SCHEMA)
MESSAGES = [{"role": "user", "content": "hi"}]


def payload(text="{}", finish="stop", usage=None, refusal=None, reasoning="r"):
    message = {"role": "assistant", "content": text, "reasoning_content": reasoning}
    if refusal is not None:
        message["refusal"] = refusal
    return {
        "id": "chatcmpl-1",
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": usage or {
            "prompt_tokens": 10, "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


class FakeTrace:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def test_request_body_sends_only_supported_params():
    bodies = []

    def transport(body):
        bodies.append(body)
        return payload(), {"x-request-id": "req-1"}

    provider = ReflectionProvider(model="Beam-Test", effort="low", transport=transport)
    result = provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")
    assert result.output == {}
    assert result.request_id == "req-1"
    assert result.reasoning_content == "r"
    assert set(bodies[0]) == {
        "model", "messages", "temperature", "top_p",
        "max_completion_tokens", "reasoning_effort", "response_format",
    }
    assert bodies[0]["temperature"] == 0.7
    assert bodies[0]["top_p"] == 0.99
    assert bodies[0]["reasoning_effort"] == "low"
    assert "max_tokens" not in bodies[0]


def test_retry_then_success_honors_retry_after():
    waits = []
    calls = []

    def transport(body):
        calls.append(body)
        if len(calls) == 1:
            raise ProviderHTTPError(429, {"Retry-After": "2", "x-request-id": "a"})
        return payload(), {"x-request-id": "b"}

    provider = ReflectionProvider(transport=transport, sleeper=waits.append)
    result = provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")
    assert result.retries == 1
    assert waits == [2.0]
    assert result.request_id == "b"


def test_no_retry_when_provider_says_no():
    calls = []

    def transport(body):
        calls.append(body)
        raise ProviderHTTPError(500, {"x-should-retry": "false"})

    provider = ReflectionProvider(transport=transport, sleeper=lambda s: None)
    with pytest.raises(ProviderError):
        provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")
    assert len(calls) == 1


def test_retryable_statuses_and_give_up():
    calls = []

    def transport(body):
        calls.append(body)
        raise ProviderHTTPError(503, {})

    provider = ReflectionProvider(transport=transport, sleeper=lambda s: None, max_retries=2)
    with pytest.raises(ProviderError):
        provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")
    assert len(calls) == 3


def test_non_retryable_status_fails_fast():
    def transport(body):
        raise ProviderHTTPError(400, {})

    provider = ReflectionProvider(transport=transport)
    with pytest.raises(ProviderError):
        provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")


def test_length_cutoff_retries_once_with_higher_limit():
    limits = []

    def transport(body):
        limits.append(body["max_completion_tokens"])
        if len(limits) == 1:
            return payload(text=None, finish="length"), {}
        return payload(), {}

    provider = ReflectionProvider(transport=transport, max_completion_tokens=1000)
    result = provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")
    assert limits == [1000, 2000]
    assert result.retries == 1


def test_length_cutoff_respects_cap():
    def transport(body):
        return payload(text=None, finish="length"), {}

    provider = ReflectionProvider(transport=transport, max_completion_tokens=MAX_COMPLETION_TOKENS)
    with pytest.raises(ProviderError, match="empty content"):
        provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")


def test_refusal_raises_before_parsing():
    def transport(body):
        return payload(text="{not json", refusal="I cannot help"), {}

    provider = ReflectionProvider(transport=transport)
    with pytest.raises(RefusalError, match="cannot help"):
        provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")


def test_trace_logs_usage_reasoning_and_request_id():
    trace = FakeTrace()

    def transport(body):
        return payload(), {"x-request-id": "req-9"}

    provider = ReflectionProvider(model="Beam-Test", effort="medium", transport=transport)
    provider.complete(MESSAGES, response_format=FORMAT, purpose="step", step=3, trace=trace)
    assert len(trace.events) == 1
    event, fields = trace.events[0]
    assert event == "decision"
    assert fields["step"] == 3
    assert fields["model"] == "Beam-Test"
    assert fields["effort"] == "medium"
    assert fields["prompt_tokens"] == 10
    assert fields["completion_tokens"] == 5
    assert fields["reasoning_tokens"] == 3
    assert fields["request_id"] == "req-9"
    assert "REFLECTION" not in json.dumps(fields)


def test_invalid_json_is_not_retried():
    calls = []

    def transport(body):
        calls.append(body)
        return payload(text="nope{"), {}

    provider = ReflectionProvider(transport=transport)
    with pytest.raises(ProviderError, match="invalid JSON"):
        provider.complete(MESSAGES, response_format=FORMAT, purpose="ping")
    assert len(calls) == 1


def test_retry_helpers():
    assert should_retry({}) is True
    assert should_retry({"X-Should-Retry": "False"}) is False
    assert should_retry({"x-should-retry": "true"}) is True
    assert retry_delay({"retry-after": "3"}, 0) == 3.0
    assert retry_delay({}, 1) == 1.0
    assert retry_delay({"retry-after": "999"}, 0) == 30
