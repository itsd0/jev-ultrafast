#!/usr/bin/env python3
"""Readable timeline plus a one-line failure diagnosis for one run trace.

Usage: uv run scripts/trace_view.py <run_id|path>
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jev_ultrafast.trace import diagnose_trace  # noqa: E402


def resolve(argument):
    candidate = Path(argument)
    if candidate.is_file():
        return candidate
    if candidate.is_dir() and (candidate / "trace.jsonl").exists():
        return candidate / "trace.jsonl"
    run_file = Path("artifacts") / "runs" / argument / "trace.jsonl"
    if run_file.exists():
        return run_file
    raise SystemExit(f"No trace for {argument!r} (looked in artifacts/runs/).")


def short_url(url):
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return (url or "?")[:60]
    path = (parsed.path or "/")[:40]
    return f"{parsed.hostname or '?'}{path}"


def load(path):
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def render(events):
    lines = []
    steps = sorted({event.get("step") for event in events if event.get("step") is not None})
    for step in steps:
        at = [event for event in events if event.get("step") == step]
        by_kind = {}
        for event in at:
            by_kind.setdefault(event.get("kind"), []).append(event.get("data") or {})
        head = f"#{step}"
        snap = (by_kind.get("snapshot") or [{}])[-1]
        if snap:
            modal = " modal" if snap.get("modal_scoped") else ""
            overlay = f" [{snap.get('overlay')}]" if snap.get("overlay") else ""
            lines.append(
                f"{head} page {short_url(snap.get('url'))} · {snap.get('controls')} controls"
                f"{modal}{overlay} · changed={snap.get('page_changed')} · indexer={snap.get('duration_ms')}ms"
            )
        offered = (by_kind.get("offered") or [{}])[0]
        if offered:
            hidden = {**offered.get("held", {}), **offered.get("suppressed", {})}
            lines.append(f"{head} offered {offered.get('offered_count')} controls, hidden={hidden or '-'}")
        for item in by_kind.get("decision", []):
            lines.append(
                f"{head} decide {item.get('provider')}/{item.get('model')} {item.get('operation')}"
                f"->{item.get('choice')} conf={item.get('confidence')} {item.get('latency_ms')}ms"
                f" retries={item.get('retries')}"
                + (f" INVALID {item.get('invalid_reason')}" if item.get("invalid_reason") else "")
            )
        for item in by_kind.get("text", []):
            value = item.get("value")
            shown = ("'" + str(value)[:40] + "'") if value else "-"
            lines.append(
                f"{head} text {item.get('purpose')} {shown} {item.get('latency_ms')}ms"
                f" valid={item.get('valid')} filtered={item.get('filtered')}"
                + (f" {item.get('reason')}" if item.get("reason") else "")
            )
        for item in by_kind.get("action", []):
            lines.append(
                f"{head} {item.get('operation')} '{item.get('target')}'"
                f" [{item.get('strategy')}] {'ok' if item.get('ok', True) else 'FAIL'}"
                f" {item.get('duration_ms')}ms cdp={item.get('cdp_calls')}/{item.get('cdp_ms')}ms"
            )
        for item in by_kind.get("verify", []):
            lines.append(
                f"{head} verify {item.get('kind')} {'ok' if item.get('success') else 'FAIL'}"
                f" {item.get('before')} -> {item.get('after')}"
                + (f" next: {item.get('next_strategy')}" if item.get("next_strategy") else "")
            )
        for item in by_kind.get("guard", []):
            lines.append(f"{head} guard {item.get('type')}: {item.get('pattern') or item.get('label') or ''}"
                         f" {item.get('evidence') or ''}".rstrip())
        for item in by_kind.get("progress", []):
            if item.get("gate_missing") is not None:
                lines.append(
                    f"{head} gate accept={item.get('gate_accept')} partial={item.get('gate_partial')}"
                    f" missing={item.get('gate_missing')}"
                )
            else:
                lines.append(
                    f"{head} progress satisfied={[s['text'] for s in item.get('satisfied', [])]}"
                    f" todo={item.get('todo')} facts+={len(item.get('facts_added', []))}"
                )
        for item in by_kind.get("error", []):
            lines.append(f"{head} ERROR {item.get('type')}: {item.get('message')} ({item.get('where')})")
    end = next((event.get("data") or {} for event in events if event.get("kind") == "end"), {})
    if end:
        phases = " ".join(f"{key}={value}ms" for key, value in (end.get("phases") or {}).items())
        lines.append(
            f"end: {end.get('status')} {end.get('blocked_reason') or ''}"
            f" decisions={end.get('decisions')} tokens={end.get('input_tokens')}+{end.get('output_tokens')}"
            f" cost=${end.get('cost_usd_estimate')} wall={end.get('wall_ms')}ms | {phases}"
        )
    cause, evidence = diagnose_trace(events)
    lines.append(f"Diagnosis: {cause or 'pass'} — {evidence}" if cause else "Diagnosis: pass")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        raise SystemExit("Usage: uv run scripts/trace_view.py <run_id|path>")
    print(render(load(resolve(argv[0]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
