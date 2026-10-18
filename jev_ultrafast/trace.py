"""One structured trace per run. Every failed run explains itself from its trace alone.

A Trace buffers events and writes them as JSONL to artifacts/runs/<run_id>/,
plus (under jev-server) a SQLite events table. JEV_LOG_LEVEL=off|info|debug
(default info) selects compact per-step events or full payloads. Anything that
looks like a secret is scrubbed before it reaches the trace; typed values into
password/card fields are redacted by the caller flagging the field label.
"""

import base64
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

LEVELS = {"off": 0, "info": 1, "debug": 2}
TRUNCATE_CHARS = 4000
TRUNCATE_ITEMS = 200
OFFERED_RING = 3
FLUSH_EVERY = 50

SECRET_ENV_NAMES = (
    "TYPESAFE_API_KEY",
    "INCEPTION_API_KEY",
    "TEXT_MODEL_API_KEY",
    "VISION_API_KEY",
    "BROWSER_USE_API_KEY",
    "JEV_SERVER_TOKEN",
    "REFLECTION_API_KEY",
)
SECRET_ENV_PREFIXES = ("JEV_SECRET_",)
SECRET_KEY = re.compile(
    r"(?i)^(authorization|x-browser-use-api-key|api[_\- ]?key|token|.*[_\-]token|.*Token"
    r"|.*secret.*|password|passwd|pwd)$"
)
BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/=]+")
INLINE_KEY = re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)([^\s\"']+)")
LONG_TOKEN = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|tsafe-[A-Za-z0-9_\-]{8,}|x-bu-[A-Za-z0-9_\-]{8,})\b")
SENSITIVE_FIELD = re.compile(
    r"(?i)password|passwd|pwd|credit.?card|card.?num|cc[-_ ]?num|cvv|cvc|expir|ssn|social.?security|iban"
)

PHASES = ("snapshot", "decision", "text", "action", "verify")


def log_level():
    return LEVELS.get(os.environ.get("JEV_LOG_LEVEL", "info").strip().lower(), 1)


def get_logger(name):
    return logging.getLogger(f"jev.{name}")


def configure_logging():
    """One stderr handler for the jev loggers. Idempotent; safe to call per run."""
    root = logging.getLogger("jev")
    if root.handlers:
        return
    level = log_level()
    if os.environ.get("JEV_DEBUG", "").strip().lower() in {"1", "2", "true", "yes", "on", "verbose"}:
        level = max(level, 2)
    root.setLevel(logging.WARNING if level <= 0 else logging.DEBUG if level >= 2 else logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)


def sensitive_field(label):
    return bool(label and SENSITIVE_FIELD.search(label))


def _truncate(value, chars=TRUNCATE_CHARS, items=TRUNCATE_ITEMS):
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + f"…(truncated {len(value) - chars} chars)"
    if isinstance(value, list) and len(value) > items:
        return [_truncate(item, chars, items) for item in value[:items]] + [f"…({len(value) - items} more)"]
    if isinstance(value, tuple):
        return tuple(_truncate(item, chars, items) for item in value)
    if isinstance(value, dict):
        return {key: _truncate(item, chars, items) for key, item in value.items()}
    return value


def scrub(value, _secrets=None):
    """Redact env secrets, auth headers, and inline key patterns. Never raises."""
    try:
        if _secrets is None:
            _secrets = [os.environ.get(name) or "" for name in SECRET_ENV_NAMES]
            for env_name, env_value in os.environ.items():
                if env_name.startswith(SECRET_ENV_PREFIXES) and "_DOMAINS" not in env_name:
                    _secrets.append(env_value or "")
            _secrets = [secret for secret in _secrets if len(secret) >= 8]
        if isinstance(value, dict):
            return {
                key: ("[redacted]" if SECRET_KEY.match(str(key)) else scrub(item, _secrets))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item, _secrets) for item in value]
        if isinstance(value, tuple):
            return tuple(scrub(item, _secrets) for item in value)
        if isinstance(value, str):
            redacted = value
            if BEARER.search(redacted):
                redacted = BEARER.sub("Bearer [redacted]", redacted)
            if INLINE_KEY.search(redacted):
                redacted = INLINE_KEY.sub(r"\1[redacted]", redacted)
            if LONG_TOKEN.search(redacted):
                redacted = LONG_TOKEN.sub("[redacted]", redacted)
            for secret in _secrets:
                if secret in redacted:
                    redacted = redacted.replace(secret, "[redacted]")
            return redacted
    except Exception:
        return "[redact-error]"
    return value


def cost_estimate_usd(prompt, completion):
    try:
        rate = float(os.environ.get("JEV_COST_PER_1K_TOKENS") or 0)
    except ValueError:
        rate = 0
    return round((prompt + completion) / 1000 * max(rate, 0), 6)


def trace_dir(run_id):
    base = os.environ.get("JEV_TRACE_DIR")
    root = Path(base) if base else Path.cwd() / "artifacts" / "runs"
    return root / run_id


class Trace:
    """Buffered per-run event sink: JSONL always, SQLite when attached."""

    def __init__(self, run_id=None, level=None):
        configure_logging()
        self.run_id = run_id or uuid.uuid4().hex
        self.level = log_level() if level is None else level
        self.dir = trace_dir(self.run_id)
        self.log = get_logger("run")
        self.lock = threading.Lock()
        self.buf = []
        self.seq = 0
        self.file = None
        self.store = None
        self.task_id = None
        self.offered_ring = deque(maxlen=OFFERED_RING)
        self.phase_ms = {phase: 0.0 for phase in PHASES}
        self.ended = False
        self.started = time.perf_counter()

    def attach(self, task_id, store):
        with self.lock:
            self.task_id = task_id
            self.store = store

    @property
    def enabled(self):
        return self.level >= 1

    def emit(self, event, step=None, **fields):
        if not self.enabled:
            return None
        if event == "text" and sensitive_field(fields.get("field_label")):
            fields = {**fields, "value": "[redacted]"}
        if event == "offered" and "offered" in fields:
            self.offered_ring.append((step, fields["offered"]))
        duration = fields.get("duration_ms")
        if event in self.phase_ms and isinstance(duration, (int, float)):
            self.phase_ms[event] += max(duration, 0)
        record = {
            "seq": self.seq,
            "step": step,
            "kind": event,
            "ts_ms": round(time.time() * 1000),
            "data": _truncate(scrub(fields)),
        }
        with self.lock:
            self.seq += 1
            self.buf.append(record)
            if event == "end":
                self.ended = True
            need_flush = len(self.buf) >= FLUSH_EVERY
        if event == "action":
            self._action_line(step, fields)
        elif event == "error":
            self.log.error(f"[jev {self.run_id[:8]} #{step}] {event}: {fields.get('where')}: {fields.get('message')}")
        elif event == "end":
            self.log.info(
                f"[jev {self.run_id[:8]}] end: {fields.get('status')}"
                f" {fields.get('blocked_reason') or ''} steps={step}"
                f" decisions={fields.get('decisions')} wall={fields.get('wall_ms')}ms".rstrip()
            )
        if need_flush:
            self.flush()
        return record

    def _action_line(self, step, fields):
        status = "ok" if fields.get("ok", True) else "FAIL"
        self.log.info(
            f"[jev {self.run_id[:8]} #{step}] {fields.get('operation')}"
            f" '{fields.get('target')}' {status} {fields.get('duration_ms', 0)}ms"
        )

    def flush(self):
        with self.lock:
            batch = self.buf
            self.buf = []
            store, task_id = self.store, self.task_id
        if not batch:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.file is None:
                self.file = (self.dir / "trace.jsonl").open("a", encoding="utf-8")
            for event in batch:
                self.file.write(json.dumps(event) + "\n")
            self.file.flush()
        except OSError as error:
            self.log.warning(f"[jev {self.run_id[:8]}] trace write failed: {error}")
        if store is not None and task_id is not None:
            try:
                store.add_events(
                    task_id,
                    [
                        (event["seq"], event["step"], event["kind"], event["ts_ms"], json.dumps(event["data"]))
                        for event in batch
                    ],
                )
            except Exception as error:
                self.log.warning(f"[jev {self.run_id[:8]}] events write failed: {error}")

    def close(self):
        try:
            self.flush()
        finally:
            with self.lock:
                handle, self.file = self.file, None
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    def save_failure(self, step, screenshot_b64=None):
        """Failure bundle: screenshot plus the last 3 offered-controls dumps."""
        if not self.enabled:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if screenshot_b64:
                raw = base64.b64decode(screenshot_b64) if isinstance(screenshot_b64, str) else screenshot_b64
                (self.dir / f"fail-step-{step}.png").write_bytes(raw)
            offered = [{"step": item[0], "offered": item[1]} for item in self.offered_ring]
            (self.dir / "offered-last3.json").write_text(json.dumps(offered, indent=2))
        except (OSError, ValueError) as error:
            self.log.warning(f"[jev {self.run_id[:8]}] failure bundle failed: {error}")
        self.flush()

    def maybe_step_shot(self, step, screenshot_b64):
        if not self.enabled or not screenshot_b64:
            return
        if os.environ.get("JEV_TRACE_SCREENSHOTS", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            raw = base64.b64decode(screenshot_b64) if isinstance(screenshot_b64, str) else screenshot_b64
            (self.dir / f"step-{step}.png").write_bytes(raw)
        except (OSError, ValueError):
            pass


def diagnose_trace(events):
    """One failure cause from a trace, with evidence. Generic across sites.

    Priority: captcha, environment error, transport error, then budget, loop,
    widget not verified, wandering, second-hop missing. Returns (cause, evidence) or (None, "").
    """
    kinds = [event.get("kind") for event in events]
    data = [event.get("data") or {} for event in events]
    end = next((item for kind, item in zip(kinds, data) if kind == "end"), {})
    if end.get("status") == "done":
        return None, ""
    guards = [item for kind, item in zip(kinds, data) if kind == "guard"]
    captcha = [item for item in guards if item.get("type") == "captcha"]
    if captcha:
        return "captcha", f"captcha wall on {captcha[-1].get('host', '?')}"[:160]
    reason = end.get("blocked_reason") or ""
    if reason.lower().startswith("env:"):
        return "environment/API error", reason[:160]
    errors = [item for kind, item in zip(kinds, data) if kind == "error"]
    for item in errors:
        message = f"{item.get('type', '')} {item.get('message', '')}".lower()
        if any(word in message for word in ("connection", "http 4", "http 5", "429", "timeout", "unavailable")):
            return "environment/API error", f"{item.get('type')}: {item.get('message')}"[:160]
    lowered = reason.lower()
    if "budget" in lowered:
        return "budget", reason
    if "loop" in lowered or sum(1 for item in guards if item.get("type") == "loop") >= 3:
        evidence = next((item.get("pattern", "") for item in guards if item.get("type") == "loop"), "")
        return "loop", f"loop detected {evidence}"[:160] or "loop detected"
    verify = [item for kind, item in zip(kinds, data) if kind == "verify"]
    failed = [item for item in verify if item.get("success") is False]
    if len(failed) >= 2 or (failed and end.get("status") == "blocked"):
        kinds_failed = sorted({item.get("kind", "?") for item in failed})
        return "widget not verified", f"{len(failed)} failed verifies ({'/'.join(kinds_failed)})"
    backs = [item for item in guards if item.get("type") == "back"]
    if backs:
        return "wandering", f"{len(backs)} BACK excursion(s); last: {backs[-1].get('label', '?')}"[:160]
    progress = [item for kind, item in zip(kinds, data) if kind == "progress"]
    if progress:
        last = progress[-1]
        todo = last.get("todo") or []
        if todo and end.get("status") in {"blocked", "failed", "done"} and not last.get("complete", True):
            return "second-hop missing", f"still missing: {'; '.join(todo)[:120]}"
    if end.get("status") == "blocked":
        return f"blocked: {reason or 'gave up'}", (end.get("blocked_reason") or "gave up")[:160]
    if end.get("status") == "failed":
        return "model error", (end.get("error") or "failed")[:160]
    if end:
        return None, ""
    return "no end event", "trace cut off before the run finished"
