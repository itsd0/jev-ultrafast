"""Reflection Beam over the OpenAI SDK. Structured json_schema, never tool calls.

Why json_schema instead of tool calling: one step is one atomic plan (thinking,
evaluation, memory, next goal) plus an ordered action list. A single strict
schema keeps that atomicity, keeps element indices as plain integers the code
re-validates, and keeps history as plain messages with reasoning_content
round-tripped. Tool calling would split the plan from its actions across tool
results and add a second validation surface for no executor gain: every Beam
action already maps to an observed element or a gated primitive.
"""

import json
import math
import os
import time

from ..model import reasoning_tokens, token_counts
from ..trace import get_logger
from .base import LLMCallResult, ProviderError, RefusalError
from .schema import validate_strict_schema

BASE_URL = "https://api.reflection.ai/openai/v1"
DEFAULT_MODEL = "Beam-1.0-501B-A23B"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
TEMPERATURE = 0.7
TOP_P = 0.99
DEFAULT_MAX_COMPLETION_TOKENS = 3000
MAX_COMPLETION_TOKENS = 12000
RETRY_STATUSES = {429, 500, 502, 503}
MAX_RETRIES = 3

LOG = get_logger("beam")


class ProviderHTTPError(ProviderError):
    def __init__(self, status, headers=None, message=""):
        super().__init__(f"Reflection returned HTTP {status}; no action executed.")
        self.status = status
        self.headers = dict(headers or {})
        self.detail = (message or "")[:500]


def effort_from_env():
    value = os.environ.get("JEV_REASONING_EFFORT", "low").strip().lower()
    return value if value in EFFORTS else "low"


def model_from_env():
    return os.environ.get("JEV_MODEL", "").strip() or DEFAULT_MODEL


def _header(headers, name):
    for key, value in (headers or {}).items():
        if str(key).lower() == name:
            return value
    return None


def should_retry(headers):
    """False only when the provider says so. Missing header means retryable."""
    value = _header(headers, "x-should-retry")
    return str(value).strip().lower() not in {"false", "0", "no"} if value is not None else True


def retry_delay(headers, attempt):
    raw = _header(headers, "retry-after")
    if raw is not None:
        try:
            seconds = float(str(raw).strip())
        except ValueError:
            seconds = None
        if seconds is not None and math.isfinite(seconds) and seconds >= 0:
            return min(seconds, 30)
    return min(0.5 * 2**attempt, 30)


def _message_dict(message):
    if isinstance(message, dict):
        data = dict(message)
        extra = message.get("model_extra") if isinstance(message.get("model_extra"), dict) else {}
        return {**extra, **data}
    dumped = None
    method = getattr(message, "model_dump", None)
    if callable(method):
        try:
            dumped = method()
        except Exception:
            dumped = None
    if isinstance(dumped, dict):
        extra = dumped.get("model_extra") if isinstance(dumped.get("model_extra"), dict) else {}
        merged = {**extra, **dumped}
        merged.pop("model_extra", None)
        return merged
    return {
        key: getattr(message, key)
        for key in ("content", "refusal", "reasoning_content", "role")
        if hasattr(message, key)
    }


def normalize_response(parsed):
    """SDK object or dict -> plain payload dict. Never includes credentials."""
    if isinstance(parsed, dict):
        payload = dict(parsed)
    else:
        method = getattr(parsed, "model_dump", None)
        try:
            payload = dict(method()) if callable(method) else {}
        except Exception:
            payload = {}
        if not payload:
            for key in ("id", "model", "choices", "usage"):
                if hasattr(parsed, key):
                    payload[key] = getattr(parsed, key)
    choices = payload.get("choices") or []
    if choices and not isinstance(choices[0], dict):
        first = choices[0]
        message = getattr(first, "message", None)
        payload["choices"] = [{
            "finish_reason": getattr(first, "finish_reason", None),
            "message": _message_dict(message) if message is not None else {},
        }]
    elif choices and isinstance(choices[0].get("message"), dict):
        payload["choices"][0]["message"] = _message_dict(choices[0]["message"])
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, dict):
        method = getattr(usage, "model_dump", None)
        try:
            payload["usage"] = dict(method()) if callable(method) else {}
        except Exception:
            payload["usage"] = {}
    return payload


def sdk_transport(body, *, base_url=BASE_URL, api_key=None, timeout=180):
    """Production transport through the OpenAI SDK. SDK retries stay off."""
    from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

    key = api_key if api_key is not None else os.environ.get("REFLECTION_API_KEY") or ""
    if not key.strip():
        raise ProviderError("REFLECTION_API_KEY is not set; no action executed.")
    client = OpenAI(base_url=base_url, api_key=key, max_retries=0, timeout=timeout)
    try:
        raw = client.chat.completions.with_raw_response.create(**body)
        parsed = raw.parse()
        headers = dict(raw.headers or {})
    except APIStatusError as error:
        headers = {}
        try:
            headers = dict(error.response.headers or {})
        except Exception:
            pass
        raise ProviderHTTPError(error.status_code, headers) from None
    except (APITimeoutError, APIConnectionError) as error:
        raise ProviderError(f"Reflection connection failed ({type(error).__name__}).") from None
    return normalize_response(parsed), headers


class ReflectionProvider:
    """Single-LLM provider. One structured call per step, explicit retries."""

    name = "reflection"

    def __init__(
        self, model=None, effort=None, max_completion_tokens=None,
        transport=None, sleeper=None, max_retries=MAX_RETRIES,
    ):
        self.model = model or model_from_env()
        self.effort = effort if effort in EFFORTS else effort_from_env()
        self.max_completion_tokens = max_completion_tokens or DEFAULT_MAX_COMPLETION_TOKENS
        self._transport = transport or sdk_transport
        self._sleep = sleeper or time.sleep
        self._max_retries = max(0, int(max_retries))

    @classmethod
    def from_env(cls, **overrides):
        return cls(**overrides)

    def request_body(self, messages, response_format, max_completion_tokens=None):
        schema = (response_format or {}).get("json_schema", {}).get("schema")
        if schema is not None:
            validate_strict_schema(schema)
        return {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
            "max_completion_tokens": max_completion_tokens or self.max_completion_tokens,
            "reasoning_effort": self.effort,
            "response_format": response_format,
        }

    def complete(
        self, messages, *, response_format, purpose,
        step=None, trace=None, max_completion_tokens=None,
    ):
        limit = max_completion_tokens or self.max_completion_tokens
        retries = 0
        length_retried = False
        started = time.perf_counter()
        while True:
            body = self.request_body(messages, response_format, limit)
            try:
                payload, headers = self._transport(body)
            except ProviderHTTPError as error:
                if (
                    error.status in RETRY_STATUSES
                    and retries < self._max_retries
                    and should_retry(error.headers)
                ):
                    delay = retry_delay(error.headers, retries)
                    LOG.debug(
                        f"[jev:beam] retry status={error.status} "
                        f"attempt={retries + 1} wait_s={delay}"
                    )
                    self._sleep(delay)
                    retries += 1
                    continue
                raise ProviderError(str(error)) from None
            message, finish = self._first_message(payload)
            refusal = (message.get("refusal") or "").strip() if isinstance(message, dict) else ""
            if refusal:
                raise RefusalError(f"Beam refused ({purpose}): {refusal[:300]}")
            content = message.get("content") if isinstance(message, dict) else None
            if (content is None or content == "") and finish == "length" and not length_retried:
                grown = min(limit * 2, MAX_COMPLETION_TOKENS)
                if grown > limit:
                    limit = grown
                    length_retried = True
                    retries += 1
                    LOG.debug(f"[jev:beam] length cut-off; retrying with {limit} tokens")
                    continue
            latency_ms = round((time.perf_counter() - started) * 1000)
            output = self._parse_content(content, purpose)
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            request_id = _header(headers, "x-request-id")
            if request_id is not None:
                request_id = str(request_id)
            reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
            if trace is not None:
                prompt, completion = token_counts(usage)
                trace.emit(
                    "decision", step=step, purpose=purpose, provider=self.name,
                    model=self.model, effort=self.effort, latency_ms=latency_ms,
                    duration_ms=latency_ms, retries=retries, request_id=request_id,
                    finish_reason=finish, prompt_tokens=prompt,
                    completion_tokens=completion,
                    reasoning_tokens=reasoning_tokens(usage),
                    output_chars=len(content or ""),
                )
            return LLMCallResult(
                output=output, usage=usage, latency_ms=latency_ms, model=self.model,
                effort=self.effort, request_id=request_id, retries=retries,
                reasoning_content=reasoning if isinstance(reasoning, str) and reasoning else None,
                finish_reason=finish,
            )

    @staticmethod
    def _first_message(payload):
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not choices or not isinstance(choices[0], dict):
            raise ProviderError("Beam returned no choices; no action executed.")
        first = choices[0]
        message = first.get("message")
        if not isinstance(message, dict):
            raise ProviderError("Beam returned no message; no action executed.")
        return message, first.get("finish_reason")

    @staticmethod
    def _parse_content(content, purpose):
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(f"Beam returned empty content ({purpose}); no action executed.")
        try:
            output = json.loads(content)
        except ValueError:
            raise ProviderError(f"Beam returned invalid JSON ({purpose}); no action executed.") from None
        if not isinstance(output, dict):
            raise ProviderError(f"Beam returned a non-object ({purpose}); no action executed.")
        return output
