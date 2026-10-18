"""One small interface for single-LLM providers. Classic stays behind JEV_AGENT=classic."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMCallResult:
    output: dict
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""
    effort: str = ""
    request_id: str | None = None
    retries: int = 0
    reasoning_content: str | None = None
    finish_reason: str | None = None


class ProviderError(RuntimeError):
    """A provider failure after retries. Never carries credentials."""


class RefusalError(ProviderError):
    """The model refused. The run stops honestly instead of guessing."""


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any],
        purpose: str,
        step: int | None = None,
        trace: Any = None,
        max_completion_tokens: int | None = None,
    ) -> LLMCallResult:
        """One structured chat call. Raises ProviderError (or RefusalError)."""
        ...
