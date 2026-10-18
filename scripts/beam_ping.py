"""One tiny Reflection call. Prints latency/tokens only; never secrets."""

import json
import sys

sys.path.insert(0, ".")

from jev_ultrafast.llm.reflection import ReflectionProvider
from jev_ultrafast.llm.schema import PING_SCHEMA, response_format


def main():
    provider = ReflectionProvider.from_env()
    result = provider.complete(
        [
            {"role": "system", "content": "Reply with the required JSON only."},
            {"role": "user", "content": 'Return {"text": "beam-ok"}.'},
        ],
        response_format=response_format("BeamPing", PING_SCHEMA),
        purpose="ping",
        max_completion_tokens=200,
    )
    usage = result.usage if isinstance(result.usage, dict) else {}
    print(json.dumps({
        "model": result.model,
        "effort": result.effort,
        "latency_ms": result.latency_ms,
        "finish_reason": result.finish_reason,
        "retries": result.retries,
        "request_id": bool(result.request_id),
        "has_reasoning": bool(result.reasoning_content),
        "usage_keys": sorted(usage),
        "output": result.output,
    }))


if __name__ == "__main__":
    main()
