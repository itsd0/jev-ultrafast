"""Minimal hosted-agent layer: task queue, SQLite run log, one-page UI.

New product surface only. The agent core (agent/browser/model) is unchanged:
each task drives a real Agent tick by tick, closes it in a finally block, and
records steps. Screenshots stay off; credentials are never logged or stored.
"""

import base64
import binascii
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from browser_harness import admin as harness_admin
from browser_harness import helpers as harness_helpers

from .agents import default_agent_factory
from .browser import Browser, full_text_expression
from .config import env_bool
from .demo import load_environment
from .llm.reflection import DEFAULT_MODEL, EFFORTS
from .model import (
    cloud_rate_per_hour,
    priced_cost,
    redact_secrets,
    token_counts,
    validate_save_filename,
)
from .trace import configure_logging, get_logger, trace_dir

LOG = get_logger("server")

ROOT = Path(__file__).parent
TERMINAL = {"done", "blocked", "failed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    url TEXT NOT NULL,
    browser TEXT NOT NULL,
    max_decisions INTEGER,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL,
    final_url TEXT,
    verified_by_agent INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd_estimate REAL,
    error TEXT,
    live_url TEXT,
    final_text TEXT,
    answer_text TEXT,
    decisions_count INTEGER,
    blocked_reason TEXT,
    partial INTEGER
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    n INTEGER NOT NULL,
    operation TEXT,
    target_label TEXT,
    url TEXT,
    ms INTEGER,
    note TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS steps_task ON steps(task_id, n);
CREATE TABLE IF NOT EXISTS events (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    seq INTEGER NOT NULL,
    step INTEGER,
    kind TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    browser TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    total_cloud_seconds REAL NOT NULL DEFAULT 0,
    total_cloud_cost_usd REAL NOT NULL DEFAULT 0,
    share_token TEXT,
    last_url TEXT,
    last_tabs TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    n INTEGER NOT NULL,
    user_text TEXT NOT NULL,
    agent_reply TEXT,
    status TEXT NOT NULL,
    run_id TEXT,
    created_at REAL NOT NULL,
    ended_at REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd_estimate REAL,
    priced INTEGER,
    cloud_seconds REAL,
    cloud_cost_usd REAL,
    duration_ms INTEGER,
    error TEXT,
    blocked_reason TEXT,
    partial INTEGER,
    final_url TEXT,
    final_text TEXT,
    answer_text TEXT,
    decisions_count INTEGER,
    start_url TEXT,
    model TEXT,
    effort TEXT
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, n);
"""


class Store:
    """SQLite run log. One connection, one lock; safe across threads."""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            migrations = {
                "tasks": (
                    ("final_text", "TEXT"),
                    ("answer_text", "TEXT"),
                    ("decisions_count", "INTEGER"),
                    ("blocked_reason", "TEXT"),
                    ("partial", "INTEGER"),
                ),
                "messages": (
                    ("priced", "INTEGER"),
                    ("cloud_seconds", "REAL"),
                    ("cloud_cost_usd", "REAL"),
                    ("model", "TEXT"),
                    ("effort", "TEXT"),
                ),
                "sessions": (
                    ("total_cloud_seconds", "REAL"),
                    ("total_cloud_cost_usd", "REAL"),
                ),
                "steps": (("detail", "TEXT"),),
            }
            for table, columns in migrations.items():
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for column, ctype in columns:
                    if column not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
            self.conn.commit()

    def create_task(self, goal, url, browser, max_decisions, task_id=None):
        task_id = task_id or uuid.uuid4().hex
        with self.lock:
            self.conn.execute(
                "INSERT INTO tasks (id, goal, url, browser, max_decisions, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (task_id, goal, url, browser, max_decisions, time.time()),
            )
            self.conn.commit()
        return task_id

    def get_task(self, task_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            task = dict(row)
            task["steps"] = [dict(r) for r in self.conn.execute(
                "SELECT n, operation, target_label, url, ms, note, detail FROM steps"
                " WHERE task_id = ? ORDER BY n",
                (task_id,),
            )]
        return task

    def list_tasks(self, limit=50):
        with self.lock:
            rows = self.conn.execute(
                "SELECT t.*, (SELECT COUNT(*) FROM steps s WHERE s.task_id = t.id) AS step_count"
                " FROM tasks t ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        tasks = []
        for row in rows:
            task = dict(row)
            task.pop("final_text", None)  # Detail endpoint only; keeps history lists light.
            tasks.append(task)
        return tasks

    def update_task(self, task_id, **fields):
        if not fields:
            return
        names = ", ".join(f"{key} = ?" for key in fields)
        with self.lock:
            self.conn.execute(f"UPDATE tasks SET {names} WHERE id = ?", (*fields.values(), task_id))
            self.conn.commit()

    def add_step(self, task_id, n, operation, target_label, url, ms, note, detail=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO steps (task_id, n, operation, target_label, url, ms, note, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, n, operation, target_label, url, ms, note, detail),
            )
            self.conn.commit()

    def add_events(self, task_id, rows):
        """One buffered batch from a run's Trace. Old DBs gain the table on open."""
        if not rows:
            return
        with self.lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO events (task_id, seq, step, kind, ts_ms, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(task_id, seq, step, kind, ts_ms, data) for seq, step, kind, ts_ms, data in rows],
            )
            self.conn.commit()

    def get_events(self, task_id):
        with self.lock:
            rows = self.conn.execute(
                "SELECT seq, step, kind, ts_ms, data FROM events WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [
            {"seq": row[0], "step": row[1], "kind": row[2], "ts_ms": row[3],
             "data": json.loads(row[4])}
            for row in rows
        ]

    def create_session(self, title, browser):
        session_id = uuid.uuid4().hex
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT INTO sessions (id, title, browser, status, created_at, last_active_at)"
                " VALUES (?, ?, ?, 'active', ?, ?)",
                (session_id, title, browser, now, now),
            )
            self.conn.commit()
        return session_id

    def get_session(self, session_id, with_steps=True):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            session = dict(row)
            try:
                session["last_tabs"] = json.loads(session.get("last_tabs") or "[]")
            except ValueError:
                session["last_tabs"] = []
            messages = [
                dict(r) for r in self.conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY n", (session_id,)
                )
            ]
            if with_steps:
                for message in messages:
                    run_id = message.get("run_id")
                    message["steps"] = [dict(r) for r in self.conn.execute(
                        "SELECT n, operation, target_label, url, ms, note, detail FROM steps"
                        " WHERE task_id = ? ORDER BY n",
                        (run_id,),
                    )] if run_id else []
        session["messages"] = messages
        # NULL priced = pre-pricing row: keep the legacy $ display.
        session["priced"] = max(
            [1 if m.get("priced") is None else m.get("priced") for m in messages] or [1]
        )
        session["cloud_priced"] = (
            1 if any(m.get("cloud_cost_usd") is not None for m in messages) else 0
        )
        return session

    def list_sessions(self, limit=50):
        with self.lock:
            rows = self.conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)"
                " AS message_count,"
                " COALESCE((SELECT MAX(m.priced) FROM messages m WHERE m.session_id = s.id), 1)"
                " AS priced,"
                " COALESCE((SELECT MAX(m.cloud_cost_usd IS NOT NULL) FROM messages m"
                " WHERE m.session_id = s.id), 0)"
                " AS cloud_priced FROM sessions s ORDER BY last_active_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        sessions = []
        for row in rows:
            session = dict(row)
            session.pop("last_tabs", None)
            sessions.append(session)
        return sessions

    def get_session_by_share_token(self, token):
        if not token:
            return None
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE share_token = ?", (token,)
            ).fetchone()
            if row is None:
                return None
            session_id = row["id"]
        return self.get_session(session_id)

    def update_session(self, session_id, **fields):
        if not fields:
            return
        names = ", ".join(f"{key} = ?" for key in fields)
        with self.lock:
            self.conn.execute(
                f"UPDATE sessions SET {names} WHERE id = ?", (*fields.values(), session_id)
            )
            self.conn.commit()

    def delete_session(self, session_id):
        with self.lock:
            runs = [
                r[0] for r in self.conn.execute(
                    "SELECT run_id FROM messages WHERE session_id = ? AND run_id IS NOT NULL",
                    (session_id,),
                )
            ]
            for run_id in runs:
                self.conn.execute("DELETE FROM steps WHERE task_id = ?", (run_id,))
                self.conn.execute("DELETE FROM events WHERE task_id = ?", (run_id,))
            self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self.conn.commit()

    def create_message(self, session_id, user_text):
        message_id = uuid.uuid4().hex
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(n), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            self.conn.execute(
                "INSERT INTO messages (id, session_id, n, user_text, status, created_at)"
                " VALUES (?, ?, ?, ?, 'queued', ?)",
                (message_id, session_id, row[0], user_text, time.time()),
            )
            self.conn.execute(
                "UPDATE sessions SET last_active_at = ?, status = 'active' WHERE id = ?",
                (time.time(), session_id),
            )
            self.conn.commit()
        return message_id

    def get_message(self, message_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def update_message(self, message_id, **fields):
        if not fields:
            return
        names = ", ".join(f"{key} = ?" for key in fields)
        with self.lock:
            self.conn.execute(
                f"UPDATE messages SET {names} WHERE id = ?", (*fields.values(), message_id)
            )
            self.conn.commit()

    def session_messages(self, session_id):
        with self.lock:
            rows = self.conn.execute(
                "SELECT user_text, agent_reply, status FROM messages"
                " WHERE session_id = ? ORDER BY n",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def usage_by_day(self, days=30):
        """Totals per day for the Usage page. Costs roll up from messages."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT date(m.created_at, 'unixepoch') AS day,"
                " COUNT(DISTINCT m.session_id) AS sessions,"
                " COUNT(DISTINCT CASE WHEN m.status IN ('done', 'blocked', 'failed')"
                " THEN m.id END) AS turns,"
                " COALESCE(SUM(m.input_tokens + m.output_tokens), 0) AS tokens,"
                " COALESCE(SUM(m.cost_usd_estimate), 0) AS cost,"
                " COALESCE(MAX(m.priced), 1) AS priced,"
                " COALESCE(SUM(m.cloud_seconds), 0) AS cloud_seconds,"
                " COALESCE(SUM(m.cloud_cost_usd), 0) AS cloud_cost,"
                " COALESCE(MAX(m.cloud_cost_usd IS NOT NULL), 0) AS cloud_priced,"
                " (SELECT COUNT(*) FROM steps s JOIN messages m2 ON s.task_id = m2.run_id"
                "  WHERE date(m2.created_at, 'unixepoch')"
                "  = date(m.created_at, 'unixepoch')) AS steps"
                " FROM messages m WHERE m.created_at >= strftime('%s', 'now', ?)"
                " GROUP BY day ORDER BY day DESC",
                (f"-{int(days)} days",),
            ).fetchall()
        return [dict(r) for r in rows]


def tally_tokens(snapshot):
    """Prompt/completion sums with the same usage keys as the measure scripts."""
    prompt = completion = 0
    for item in snapshot.get("decisions") or []:
        for usage in (item.get("usage"), item.get("verifier_usage"), item.get("decompose_usage")):
            pin, pout = token_counts(usage)
            prompt += pin
            completion += pout
    for item in snapshot.get("text_calls") or []:
        pin, pout = token_counts(item.get("usage"))
        prompt += pin
        completion += pout
    return prompt, completion


def price_message(snapshot):
    """(token_cost_usd, priced) for one finished turn. Each decision and text call
    is priced against its recorded model's JEV_PRICE_<MODEL>_IN/OUT rate."""
    calls = []
    for item in snapshot.get("decisions") or []:
        usages = [
            (item.get("model"), item.get("usage")),
            (item.get("verifier_model"), item.get("verifier_usage")),
            (item.get("decompose_model"), item.get("decompose_usage")),
        ]
        for model, usage in usages:
            pin, pout = token_counts(usage)
            if pin or pout:
                calls.append((model, pin, pout))
    for item in snapshot.get("text_calls") or []:
        pin, pout = token_counts(item.get("usage"))
        if pin or pout:
            calls.append((item.get("model"), pin, pout))
    return priced_cost(calls)


def reclaim_daemon(name):
    """Stop our own daemon when its browser link is dead. Never touches others'.

    A live socket with a dead CDP link (e.g. Chrome restarted underneath it)
    answers ping but fails a real Target call. restart_daemon only stops; the
    next local task respawns a fresh daemon through ensure_daemon().
    """
    if not harness_admin.daemon_alive(name):
        return
    try:
        harness_helpers.cdp("Target.getTargets")
        return
    except Exception:
        pass
    LOG.info(f"[jev:server] daemon {name} is stale; stopping it")
    try:
        harness_admin.restart_daemon(name)
    except Exception as error:
        LOG.warning(f"[jev:server] daemon stop failed: {error}")


class Viewer:
    """One local task's frame source. Screencast primary, screenshot fallback.

    Frames arrive through the shared FramePump; the fallback captures directly.
    A screencast that stays silent past SHOT_FALLBACK_S (suspended paint, no
    compositor frames) flips to screenshots instead of restarting forever.
    Quiet-restarting heals ack stalls once frames have flowed at least once.
    """

    QUIET_RESTART_S = 3.0
    SHOT_EVERY_S = 1.0
    SHOT_FALLBACK_S = 6.0

    def __init__(self, pump, call, session):
        self.pump = pump
        self.call = call
        self.session = session
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.viewers = 0
        self.seq = 0
        self.latest = None
        self.mode = None
        self.last_frame_at = 0.0
        self._stop = threading.Event()

    def offer(self, image_b64):
        with self.cond:
            self.latest = image_b64
            self.seq += 1
            self.last_frame_at = time.monotonic()
            self.cond.notify_all()

    def add_viewer(self):
        with self.lock:
            self.viewers += 1
            if self.viewers == 1:
                self._start_source()

    def remove_viewer(self):
        with self.lock:
            self.viewers = max(0, self.viewers - 1)
            if self.viewers == 0:
                self._stop_source()

    def shutdown(self):
        with self.lock:
            self._stop.set()
        self.pump.unregister(self)
        try:
            self.call("Page.stopScreencast")
        except Exception:
            pass

    def _start_screencast(self):
        self.call("Page.startScreencast", format="jpeg", quality=60, maxWidth=1120, everyNthFrame=2)

    def _start_source(self):
        self._stop.clear()
        self._source_started_at = time.monotonic()
        self._framed_at_start = self.seq
        try:
            self._start_screencast()
            self.mode = "screencast"
            self.last_frame_at = time.monotonic()
            self.pump.register(self)
        except Exception:
            self.mode = "screenshot"
        worker = threading.Thread(target=self._maintain, daemon=True)
        worker.start()

    def _maintain(self):
        while not self._stop.is_set():
            if self.mode == "screenshot":
                try:
                    shot = self.call("Page.captureScreenshot", format="jpeg", quality=60)
                    if isinstance(shot, dict) and shot.get("data"):
                        self.offer(shot["data"])
                except Exception:
                    pass
                self._stop.wait(self.SHOT_EVERY_S)
            else:
                if time.monotonic() - self.last_frame_at > self.QUIET_RESTART_S:
                    silent_since_start = (
                        self.seq == getattr(self, "_framed_at_start", 0)
                        and time.monotonic() - getattr(self, "_source_started_at", 0)
                        > self.SHOT_FALLBACK_S
                    )
                    if silent_since_start:
                        self.mode = "screenshot"
                        self.pump.unregister(self)
                        try:
                            self.call("Page.stopScreencast")
                        except Exception:
                            pass
                    else:
                        try:
                            self.call("Page.stopScreencast")
                            self._start_screencast()
                            self.last_frame_at = time.monotonic()
                        except Exception:
                            self.mode = "screenshot"
                self._stop.wait(0.5)

    def _stop_source(self):
        self._stop.set()
        self.pump.unregister(self)
        if self.mode == "screencast":
            try:
                self.call("Page.stopScreencast")
            except Exception:
                pass
        self.mode = None


class FramePump:
    """One global drain routing screencast frames to task viewers by session.

    drain_events is destructive and daemon-wide, so exactly one pump runs, only
    while at least one viewer is connected. Every screencast frame is acked,
    even for unknown sessions, so the browser never stalls. Non-frame events
    consumed here are unavailable to tab/network watchers until viewing ends.
    """

    def __init__(self, daemon_name):
        self.daemon_name = daemon_name
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.sessions = {}
        worker = threading.Thread(target=self._loop, daemon=True)
        worker.start()

    def register(self, viewer):
        with self.cond:
            self.sessions[viewer.session] = viewer
            self.cond.notify()

    def unregister(self, viewer):
        with self.lock:
            if self.sessions.get(viewer.session) is viewer:
                del self.sessions[viewer.session]

    def step(self):
        if harness_helpers.NAME != self.daemon_name:
            return 0
        try:
            events = harness_helpers.drain_events()
        except Exception:
            return 0
        routed = 0
        for event in events or []:
            if event.get("method") != "Page.screencastFrame":
                continue
            params = event.get("params") or {}
            cdp_session = event.get("session_id")
            if params.get("sessionId"):
                try:
                    harness_helpers.cdp(
                        "Page.screencastFrameAck", session_id=cdp_session,
                        sessionId=params["sessionId"],
                    )
                except Exception:
                    pass
            with self.lock:
                viewer = self.sessions.get(cdp_session)
            if viewer is not None and params.get("data"):
                viewer.offer(params["data"])
                routed += 1
        return routed

    def _loop(self):
        while True:
            with self.cond:
                while not self.sessions:
                    self.cond.wait()
            self.step()
            time.sleep(0.03)


def final_text_for(browser, page):
    """Bench evidence: full-document text for checks only. Viewport on failure."""
    evaluate = getattr(browser, "evaluate", None)
    if evaluate is not None:
        try:
            full = evaluate(full_text_expression())
        except Exception:
            full = None
        if isinstance(full, str) and full.strip():
            return full[:20000]
    return (page.get("text") or "")[:20000]


def session_title(text):
    """A chat title from the first message: first line, capped, no newlines."""
    title = (text or "").strip().splitlines()[0:1]
    title = title[0] if title else "New session"
    return title if len(title) <= 60 else title[:57] + "..."


def session_tabs(browser):
    """Live tab list for context and the tab strip."""
    infos = getattr(browser, "tab_infos", None)
    if callable(infos):
        try:
            return infos()
        except Exception:
            pass
    tabs = getattr(browser, "tabs", None) or {}
    active = getattr(browser, "active", None)
    return [
        {
            "key": key, "url": tab.get("url") or "",
            "title": tab.get("title") or "", "active": key == active,
        }
        for key, tab in tabs.items()
    ]


def tabs_signature(browser):
    """Cheap tab-set identity from cached values. No CDP, for change detection."""
    tabs = getattr(browser, "tabs", None) or {}
    active = getattr(browser, "active", None)
    return tuple(sorted(
        (key, tab.get("url") or "", key == active) for key, tab in tabs.items()
    ))


def session_file_names(files_dir):
    """Sorted plain names in a session files dir. Never raises."""
    try:
        return sorted(
            entry.name for entry in Path(files_dir).iterdir()
            if entry.is_file() and _file_name_ok(entry.name)
        )
    except OSError:
        return []


def _file_name_ok(name):
    return (
        isinstance(name, str) and bool(name) and "/" not in name
        and "\\" not in name and ".." not in name and not name.startswith(".")
    )


def session_files(files_dir):
    """Files-tab rows: name, bytes, mtime. Never raises."""
    rows = []
    try:
        entries = list(Path(files_dir).iterdir())
    except OSError:
        return rows
    for entry in entries:
        if not entry.is_file() or not _file_name_ok(entry.name):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        rows.append({"name": entry.name, "bytes": stat.st_size, "mtime": stat.st_mtime})
    rows.sort(key=lambda row: row["name"])
    return rows


def files_signature(files_dir):
    """Cheap files identity for change detection."""
    return tuple((row["name"], row["bytes"], row["mtime"]) for row in session_files(files_dir))


def session_background(prior, url, tabs):
    """Earlier turns plus where the browser is. Never part of the goal, so
    mechanical consumers (search fallback, sub-goal splitting) only see the
    current message. Empty on the first turn."""
    if not prior:
        return ""
    lines = []
    for turn in prior[-4:]:
        lines.append(f"- They asked: {(turn.get('user_text') or '')[:300]}")
        if turn.get("agent_reply"):
            lines.append(f"  You did: {(turn['agent_reply'] or '')[:300]}")
    lines.append(f"Current page: {url or 'a blank page'}.")
    if tabs:
        lines.append("Open tabs: " + ", ".join(
            f"{tab['key']}{'*' if tab['active'] else ''} {tab['url'] or 'blank'}"
            for tab in tabs
        ))
    return "Session so far:\n" + "\n".join(lines)


def _first_sentences(text, limit=2, cap=500):
    short = " ".join((text or "").split())
    if not short:
        return ""
    parts, current = [], ""
    for chunk in short.replace("! ", ". ").replace("? ", ". ").split(". "):
        current = f"{current}. {chunk}".strip(". ") if current else chunk
        parts.append(current)
        if len(parts) >= limit:
            break
    reply = parts[-1] if parts else short
    if not reply.endswith((".", "!", "?")):
        reply += "."
    return reply if len(reply) <= cap else reply[: cap - 3] + "..."


_HOST_MENTION_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b", re.IGNORECASE
)
_PAGE_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&]*(?:\s+[A-Z][A-Za-z0-9&]*){0,2})\s+(?:page|site|website|homepage)\b"
)
_GENERIC_PAGE_NAMES = frozenset({
    "home", "search", "new", "blank", "main", "web", "start", "error", "login",
    "results", "result", "detail", "details", "product", "article", "list",
})


def _normalize_host(host):
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _snapshot_hosts(snapshot):
    """Every hostname the run actually touched, www-folded."""
    urls = list(snapshot.get("visited_urls") or [])
    for item in snapshot.get("history") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(item["url"])
    page = snapshot.get("page") or {}
    if page.get("url"):
        urls.append(page["url"])
    hosts = set()
    for url in urls:
        try:
            host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
        except ValueError:
            continue
        if host:
            hosts.add(_normalize_host(host))
    return hosts


def _host_supported(claimed, seen):
    return any(
        claimed == host or claimed.endswith("." + host) or host.endswith("." + claimed)
        for host in seen
    )


def _snapshot_name_haystack(snapshot):
    parts = []
    page = snapshot.get("page") or {}
    parts.append(page.get("title") or "")
    parts.append(page.get("url") or "")
    for item in snapshot.get("memo") or []:
        if isinstance(item, dict):
            parts.append(item.get("fact") or "")
            parts.append(item.get("source") or "")
    parts.extend(_snapshot_hosts(snapshot))
    return " ".join(parts).lower()


def unsupported_reply_claims(answer, snapshot):
    """Site/page names in the answer the trace cannot back. Empty means honest."""
    problems = []
    hosts = _snapshot_hosts(snapshot)
    for match in _HOST_MENTION_RE.finditer(answer or ""):
        claimed = _normalize_host(match.group(0))
        if claimed and not _host_supported(claimed, hosts):
            problems.append(match.group(0))
    if problems:
        return problems
    haystack = _snapshot_name_haystack(snapshot)
    for match in _PAGE_NAME_RE.finditer(answer or ""):
        name = match.group(1)
        if len(name) < 3 or name.lower() in _GENERIC_PAGE_NAMES:
            continue
        if name.lower() not in haystack:
            problems.append(match.group(0))
    return problems


def _factual_fallback(snapshot):
    """What actually happened: final page plus verified memo facts."""
    page = snapshot.get("page") or {}
    url = page.get("url") or ""
    title = (page.get("title") or "").strip()
    if url and url != "about:blank":
        base = f"Opened {title} ({url})." if title else f"Opened {url}."
    else:
        base = "The browser ended on a blank page."
    verified = []
    for item in snapshot.get("memo") or []:
        if isinstance(item, dict) and (item.get("fact") or "").strip():
            bit = item["fact"].strip()
            if item.get("source"):
                bit += f" ({item['source']})"
            verified.append(bit)
        if len(verified) >= 2:
            break
    if verified:
        return base + " Verified: " + " ".join(bit.rstrip(".") + "." for bit in verified)
    return base


def compose_reply(snapshot, status, error):
    """One or two plain sentences from the run outcome. Never raw JSON."""
    answer = (snapshot.get("answer") or "").strip()
    if answer and unsupported_reply_claims(answer, snapshot):
        answer = _factual_fallback(snapshot)
    if status == "done" and answer:
        return _first_sentences(answer) or "Done."
    if status == "done":
        return "Done."
    reason = snapshot.get("blocked_reason") or error or "I got stuck"
    reason = str(reason).strip().rstrip(".")
    if snapshot.get("partial") and answer:
        return f"I couldn't fully finish ({reason}). So far: {_first_sentences(answer, limit=1)}"
    if answer:
        return f"I couldn't finish ({reason}). What I found: {_first_sentences(answer, limit=1)}"
    return f"I couldn't finish ({reason})."


class SessionBus:
    """One session's SSE backlog. Bounded; replayable from any seq."""

    def __init__(self, limit=500):
        self.limit = limit
        self.events = []
        self.seq = 0
        self.cond = threading.Condition()

    def publish(self, event):
        with self.cond:
            self.seq += 1
            self.events.append((self.seq, event))
            del self.events[: max(0, len(self.events) - self.limit)]
            self.cond.notify_all()
        return self.seq

    def replay(self, since=0):
        with self.cond:
            return [(seq, event) for seq, event in self.events if seq > since]


class SessionRunner:
    """Chat sessions. One browser per session, reused across messages.

    Each message runs an agent against the session browser with the goal set
    to the message plus session context. Classic (TypeSafe+Mercury) is the
    default agent; JEV_AGENT=new selects Beam. Idle browsers close after
    JEV_SESSION_IDLE_MIN minutes; cloud browsers always stop on close.
    """

    def __init__(self, store, max_concurrent=2, agent_factory=None, daemon_name=None,
                 files_root=None, idle_min=None, browser_factory=None):
        self.store = store
        self.max_concurrent = max(1, int(max_concurrent))
        self.agent_factory = agent_factory or default_agent_factory
        self.browser_factory = browser_factory or Browser
        self.pump = FramePump(daemon_name or os.environ.get("BU_NAME", "default"))
        self.files_root = Path(files_root) if files_root else Path.cwd() / "artifacts" / "sessions"
        if idle_min is None:
            try:
                idle_min = float(os.environ.get("JEV_SESSION_IDLE_MIN", "10") or 10)
            except ValueError:
                idle_min = 10
        self.idle_min = max(0.05, idle_min)
        self._sessions = {}
        self._cond = threading.Condition()
        self._running = 0
        self._create_lock = threading.Lock()
        self.default_browser = "local"
        self.default_max_decisions = None
        self.config_path = None
        reaper = threading.Thread(target=self._reap_loop, daemon=True)
        reaper.start()

    def load_config(self, path):
        """Apply persisted UI config. Returns the stored values."""
        self.config_path = str(path)
        try:
            values = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            values = {}
        if isinstance(values, dict):
            self.apply_config(values)
        return values if isinstance(values, dict) else {}

    def apply_config(self, values):
        """Validate and apply UI config values. Unknown keys are ignored."""
        browser_default = values.get("browser_default")
        if browser_default in {"local", "cloud"}:
            self.default_browser = browser_default
        if "max_decisions_default" in values:
            max_decisions = values["max_decisions_default"]
            if max_decisions is None:
                self.default_max_decisions = None
            elif isinstance(max_decisions, int) and 1 <= max_decisions <= 10000:
                self.default_max_decisions = max_decisions
            else:
                raise ValueError("max_decisions_default must be 1-10,000")
        text_model = values.get("text_model")
        if isinstance(text_model, str) and text_model.strip() and len(text_model) <= 200:
            os.environ["TEXT_MODEL"] = text_model.strip()
        decision_provider = values.get("decision_provider")
        if decision_provider in {"typesafe", "mercury"}:
            os.environ["DECISION_PROVIDER"] = decision_provider
        model_default = values.get("model_default")
        if model_default is None:
            pass
        elif isinstance(model_default, str) and len(model_default.strip()) <= 200:
            if model_default.strip():
                os.environ["JEV_MODEL"] = model_default.strip()
            else:
                os.environ.pop("JEV_MODEL", None)
        else:
            raise ValueError("model_default must be a short string")
        effort_default = values.get("effort_default")
        if effort_default is None:
            pass
        elif effort_default in EFFORTS:
            os.environ["JEV_REASONING_EFFORT"] = effort_default
        else:
            raise ValueError("effort_default must be low, medium, high, xhigh, or max")
        agent = values.get("agent")
        if agent is None:
            pass
        elif agent in {"new", "classic"}:
            os.environ["JEV_AGENT"] = agent
        else:
            raise ValueError("agent must be new or classic")
        return self.config_view()

    def save_config(self, values):
        """Persist UI config values. Best-effort, owner-only file."""
        if not self.config_path:
            return
        try:
            current = json.loads(Path(self.config_path).read_text())
            if not isinstance(current, dict):
                current = {}
        except (OSError, ValueError):
            current = {}
        current.update(values)
        try:
            Path(self.config_path).write_text(json.dumps(current, indent=2))
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass

    def config_view(self):
        return {
            "browser_default": self.default_browser,
            "max_decisions_default": self.default_max_decisions,
            "text_model": os.environ.get("TEXT_MODEL", "mercury-2.5"),
            "decision_provider": os.environ.get("DECISION_PROVIDER", "typesafe"),
            "model_default": os.environ.get("JEV_MODEL", DEFAULT_MODEL),
            "effort_default": os.environ.get("JEV_REASONING_EFFORT", "low"),
            "agent": os.environ.get("JEV_AGENT", "classic"),
            "idle_min": self.idle_min,
        }

    def session_files_dir(self, session_id):
        path = self.files_root / session_id / "files"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _capture_final_frame(self, session_id, message_id):
        """Keep the last live frame for shared replays. Best-effort."""
        entry = self._entry(session_id)
        viewer = entry.get("viewer") if entry else None
        latest = getattr(viewer, "latest", None)
        if not latest:
            return
        try:
            raw = base64.b64decode(latest)
        except (binascii.Error, ValueError):
            return
        try:
            frames_dir = self.files_root / session_id / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)
            (frames_dir / f"{message_id}.jpg").write_bytes(raw)
        except OSError:
            pass

    def final_frame(self, session_id, message_id):
        """Base64 final frame for a replay. None when no viewer watched."""
        try:
            raw = (self.files_root / session_id / "frames" / f"{message_id}.jpg").read_bytes()
        except OSError:
            return None
        return base64.b64encode(raw).decode()

    def create_session(self, browser=None, title=None):
        if browser is None:
            browser = self.default_browser
        if browser not in {"local", "cloud"}:
            raise ValueError("browser must be local or cloud")
        session_id = self.store.create_session(title or "New session", browser)
        with self._cond:
            self._sessions[session_id] = {
                "browser": None, "agent": None, "stop": threading.Event(),
                "thread": None, "message_id": None, "viewer": None,
                "live_url": None, "lock": threading.Lock(), "bus": SessionBus(),
                "last_active": time.time(),
            }
        LOG.info(f"[jev:server] session {session_id[:8]} created ({browser})")
        return session_id

    def _entry(self, session_id):
        with self._cond:
            entry = self._sessions.get(session_id)
        if entry is None and self.store.get_session(session_id, with_steps=False) is not None:
            with self._cond:
                entry = self._sessions.setdefault(session_id, {
                    "browser": None, "agent": None, "stop": threading.Event(),
                    "thread": None, "message_id": None, "viewer": None,
                    "live_url": None, "lock": threading.Lock(), "bus": SessionBus(),
                    "last_active": time.time(),
                })
        return entry

    def _publish(self, session_id, event):
        entry = self._entry(session_id)
        if entry is not None:
            entry["bus"].publish(event)

    def replay(self, session_id, since=0):
        entry = self._entry(session_id)
        return entry["bus"].replay(since) if entry is not None else []

    def _wait_slot(self, stopper):
        with self._cond:
            while self._running >= self.max_concurrent and not stopper.is_set():
                self._cond.wait(timeout=0.2)
            if stopper.is_set():
                return False
            self._running += 1
            return True

    def _release_slot(self):
        with self._cond:
            self._running = max(0, self._running - 1)
            self._cond.notify_all()

    def submit_task(self, goal, url, browser="local", max_decisions=None):
        """The old one-shot task API: one session, one message."""
        goal = (goal or "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter a goal of 1-2,000 characters")
        url = (url or "").strip() or "about:blank"
        if url != "about:blank":
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Enter an http(s) start URL or leave it blank to navigate")
        session_id = self.create_session(browser, session_title(goal))
        message_id = self.submit_message(
            session_id, goal, max_decisions,
            url=None if url == "about:blank" else url,
        )
        self.store.update_message(message_id, start_url=url)
        # Legacy mirror so old tasks-table readers keep working; live_url stays NULL.
        self.store.create_task(goal, url, browser, max_decisions, task_id=message_id)
        return message_id

    def task_view(self, message_id, with_steps=True, with_text=True):
        """A message rendered as the legacy task shape. None when unknown."""
        message = self.store.get_message(message_id)
        if message is None:
            return None
        session = self.store.get_session(message["session_id"], with_steps=False)
        if session is None:
            return None
        run_id = message.get("run_id") or message_id
        view = {
            "id": message_id,
            "goal": message["user_text"],
            "url": message.get("start_url") or "about:blank",
            "browser": session["browser"],
            "max_decisions": None,
            "status": message["status"],
            "created_at": message["created_at"],
            "started_at": message["created_at"],
            "ended_at": message.get("ended_at"),
            "final_url": message.get("final_url"),
            "verified_by_agent": int(message["status"] == "done"),
            "input_tokens": message.get("input_tokens"),
            "output_tokens": message.get("output_tokens"),
            "cost_usd_estimate": message.get("cost_usd_estimate"),
            "error": message.get("error"),
            "live_url": self.live_url(message["session_id"]) if message["status"] == "running" else None,
            "answer_text": message.get("answer_text"),
            "decisions_count": message.get("decisions_count"),
            "blocked_reason": message.get("blocked_reason"),
            "partial": message.get("partial"),
        }
        if with_text:
            view["final_text"] = message.get("final_text")
        if with_steps:
            view["steps"] = [dict(r) for r in self._message_steps(run_id)]
            view["step_count"] = len(view["steps"])
        return view

    def _message_steps(self, run_id):
        with self.store.lock:
            return self.store.conn.execute(
                "SELECT n, operation, target_label, url, ms, note, detail FROM steps"
                " WHERE task_id = ? ORDER BY n",
                (run_id,),
            ).fetchall()

    def list_task_views(self, limit=50):
        """Legacy task list: every session's first message plus pre-session rows."""
        views = []
        for session in self.store.list_sessions(limit=limit):
            full = self.store.get_session(session["id"], with_steps=False)
            messages = (full or {}).get("messages") or []
            if not messages:
                continue
            view = self.task_view(messages[0]["id"], with_steps=False, with_text=False)
            if view is not None:
                view["step_count"] = self._step_count(messages[0].get("run_id"))
                views.append(view)
        legacy_ids = {view["id"] for view in views}
        for task in self.store.list_tasks(limit=limit):
            if task["id"] not in legacy_ids:
                views.append(task)
        views.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
        return views[:limit]

    def _step_count(self, run_id):
        if not run_id:
            return 0
        with self.store.lock:
            row = self.store.conn.execute(
                "SELECT COUNT(*) FROM steps WHERE task_id = ?", (run_id,)
            ).fetchone()
        return row[0] if row else 0

    def stop_message(self, message_id):
        """Stop one turn: queued turns block immediately, running turns stop."""
        message = self.store.get_message(message_id)
        if message is None:
            return None
        if message["status"] == "queued":
            self.store.update_message(
                message_id, status="blocked", ended_at=time.time(), error="stopped by user"
            )
        else:
            self.stop(message["session_id"])
        return self.task_view(message_id, with_steps=False, with_text=False)

    def wait_for_viewer(self, session_id, timeout=20):
        deadline = time.monotonic() + timeout
        while True:
            viewer, status = self.get_viewer(session_id)
            if viewer is not None or status == 404:
                return viewer, status
            session = self.store.get_session(session_id, with_steps=False)
            if session is None or session["browser"] != "local":
                return None, status
            if time.monotonic() >= deadline:
                return None, status
            time.sleep(0.2)

    def submit_message(self, session_id, text, max_decisions=None, url=None, model=None, effort=None):
        text = (text or "").strip()
        if not text or len(text) > 4000:
            raise ValueError("Enter a message of 1-4,000 characters")
        if max_decisions is None:
            max_decisions = self.default_max_decisions
        if max_decisions is not None and not 1 <= int(max_decisions) <= 10000:
            raise ValueError("max_decisions must be 1-10,000")
        url = (url or "").strip() or None
        if url is not None:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Enter an http(s) start URL")
        model = (model or "").strip() or None
        if model is not None and len(model) > 200:
            raise ValueError("model must be a short string")
        effort = (effort or "").strip() or None
        if effort is not None and effort not in EFFORTS:
            raise ValueError("effort must be low, medium, high, xhigh, or max")
        entry = self._entry(session_id)
        if entry is None:
            return None
        message_id = self.store.create_message(session_id, text)
        if model is not None or effort is not None:
            self.store.update_message(message_id, model=model, effort=effort)
        session = self.store.get_session(session_id, with_steps=False)
        if session is not None and len(session["messages"]) == 1:
            self.store.update_session(session_id, title=session_title(text))
        worker = threading.Thread(
            target=self._run_message,
            args=(session_id, message_id, text, max_decisions, url),
            daemon=True,
        )
        with self._cond:
            entry["message_id"] = message_id
            entry["thread"] = worker
            entry["stop"].clear()
        worker.start()
        self._publish(session_id, {"type": "message", "message_id": message_id, "status": "queued"})
        return message_id

    def stop(self, session_id):
        entry = self._entry(session_id)
        if entry is None:
            return False
        entry["stop"].set()
        return True

    def close_session(self, session_id):
        """Stop any running turn and close the browser. Cloud browsers stop here."""
        entry = self._entry(session_id)
        if entry is None:
            return False
        entry["stop"].set()
        thread = entry.get("thread")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        with entry["lock"]:
            viewer, entry["viewer"] = entry.get("viewer"), None
            browser, entry["browser"] = entry.get("browser"), None
        if viewer is not None:
            try:
                viewer.shutdown()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            browser_stop = getattr(browser, "stop_cloud", None)
            if callable(browser_stop):
                browser_stop()
        except Exception:
            pass
        self.store.update_session(session_id, status="closed")
        self._publish(session_id, {"type": "status", "status": "closed"})
        LOG.info(f"[jev:server] session {session_id[:8]} closed")
        return True

    def delete_session(self, session_id):
        self.close_session(session_id)
        self.store.delete_session(session_id)
        with self._cond:
            self._sessions.pop(session_id, None)

    def _reap_loop(self):
        while True:
            time.sleep(30)
            self.reap_idle()

    def reap_idle(self):
        """Close browsers idle past the timeout. The session row stays for resume."""
        cutoff = time.time() - self.idle_min * 60
        with self._cond:
            ids = list(self._sessions)
        for session_id in ids:
            entry = self._entry(session_id)
            if entry is None or entry.get("browser") is None:
                continue
            if entry.get("thread") is not None and entry["thread"].is_alive():
                continue
            if entry["last_active"] > cutoff:
                continue
            with entry["lock"]:
                viewer, entry["viewer"] = entry.get("viewer"), None
                browser, entry["browser"] = entry.get("browser"), None
            if viewer is not None:
                try:
                    viewer.shutdown()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            LOG.info(f"[jev:server] session {session_id[:8]} idle; browser closed")

    def _ensure_browser(self, session_id, browser_mode):
        entry = self._entry(session_id)
        browser = entry.get("browser")
        if browser is not None:
            return browser
        with self._create_lock:
            previous = os.environ.get("JEV_BROWSER")
            os.environ["JEV_BROWSER"] = browser_mode
            try:
                browser = self.browser_factory("about:blank")
            finally:
                if previous is None:
                    os.environ.pop("JEV_BROWSER", None)
                else:
                    os.environ["JEV_BROWSER"] = previous
        entry["browser"] = browser
        entry["fresh_browser"] = True
        entry["live_url"] = live_url_for_agent_browser(browser)
        files_dir = self.session_files_dir(session_id)
        browser_config = getattr(browser, "config", None)
        if isinstance(browser_config, dict):
            browser_config["files_dir"] = str(files_dir)
        configure = getattr(browser, "configure_downloads", None)
        if callable(configure):
            configure(files_dir)
        try:
            self._publish(session_id, {"type": "tabs", "tabs": session_tabs(browser)})
        except Exception:
            pass
        return browser

    def _create_agent(self, session_id, run_id, goal, browser, max_decisions, background=""):
        with self._create_lock:
            previous_browser = os.environ.get("JEV_BROWSER")
            previous_run = os.environ.get("JEV_RUN_ID")
            session = self.store.get_session(session_id, with_steps=False)
            os.environ["JEV_BROWSER"] = (session or {}).get("browser", "local")
            os.environ["JEV_RUN_ID"] = run_id
            try:
                agent = self.agent_factory("about:blank", goal, browser=browser, background=background)
            finally:
                if previous_browser is None:
                    os.environ.pop("JEV_BROWSER", None)
                else:
                    os.environ["JEV_BROWSER"] = previous_browser
                if previous_run is None:
                    os.environ.pop("JEV_RUN_ID", None)
                else:
                    os.environ["JEV_RUN_ID"] = previous_run
        config = getattr(agent, "config", None)
        if isinstance(config, dict):
            if max_decisions is not None:
                config["max_decisions"] = max_decisions
            message = self.store.get_message(run_id)
            if message is not None:
                if message.get("model"):
                    config["model"] = message["model"]
                if message.get("effort"):
                    config["effort"] = message["effort"]
            files_dir = self.session_files_dir(session_id)
            config["files_dir"] = str(files_dir)
            allowed = config.setdefault("files", {})
            if isinstance(allowed, dict):
                for name in session_file_names(files_dir):
                    allowed.setdefault(name, str(files_dir / name))
        trace = getattr(agent, "trace", None)
        if trace is not None:
            trace.attach(run_id, self.store)
        return agent

    def _run_message(self, session_id, message_id, text, max_decisions, url=None):
        entry = self._entry(session_id)
        if entry is None:
            return
        with entry["lock"]:
            if not self._wait_slot(entry["stop"]):
                self.store.update_message(message_id, status="blocked", error="stopped by user")
                return
            try:
                current = self.store.get_message(message_id)
                if current is None or current["status"] != "queued":
                    return
                self._drive_message(session_id, message_id, text, max_decisions, entry, url)
            finally:
                self._release_slot()
                entry["last_active"] = time.time()

    def _record_and_publish_steps(self, session_id, message_id, run_id, snapshot, recorded):
        history = snapshot.get("history") or []
        new_entries = history[recorded:]
        recorded = self._record_new_steps(run_id, snapshot, recorded)
        self._publish(session_id, {
            "type": "step", "message_id": message_id,
            "steps": [
                {"n": item.get("step"), "operation": item.get("operation"),
                 "target_label": item.get("action"), "url": item.get("url"),
                 "ms": item.get("elapsed_ms"), "note": item.get("note"),
                 "detail": item.get("detail")}
                for item in new_entries
            ],
        })
        return recorded

    def _maybe_restart_cloud(self, session_id, message_id, entry, agent):
        """One fresh cloud browser per run after a wall. False when not wanted."""
        state = getattr(agent, "state", None)
        if not isinstance(state, dict):
            return False
        host = state.get("cloud_restart_wanted")
        if not host or state.get("cloud_restarted"):
            return False
        session = self.store.get_session(session_id, with_steps=False)
        if session is None or session["browser"] != "cloud":
            state["cloud_restart_wanted"] = None
            return False
        old = entry.get("browser")
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        try:
            stopper = getattr(old, "stop_cloud", None)
            if callable(stopper):
                stopper()
        except Exception:
            pass
        viewer, entry["viewer"] = entry.get("viewer"), None
        if viewer is not None:
            try:
                viewer.shutdown()
            except Exception:
                pass
        entry["browser"] = None
        fallback = (os.environ.get("JEV_CLOUD_FALLBACK_COUNTRY") or "").strip()
        previous = os.environ.get("JEV_CLOUD_PROXY_COUNTRY")
        if fallback:
            os.environ["JEV_CLOUD_PROXY_COUNTRY"] = fallback
        try:
            browser = self._ensure_browser(session_id, "cloud")
        finally:
            if fallback:
                if previous is None:
                    os.environ.pop("JEV_CLOUD_PROXY_COUNTRY", None)
                else:
                    os.environ["JEV_CLOUD_PROXY_COUNTRY"] = previous
        try:
            agent.browser = browser
        except Exception:
            pass
        state["browser"] = browser
        state["cloud_restarted"] = True
        state["cloud_restart_wanted"] = None
        history = state.get("history")
        if isinstance(history, list):
            label = "Switched to a fresh cloud browser"
            if fallback:
                label += f" ({fallback} proxy)"
            history.append({
                "step": len(history) + 1,
                "action": label,
                "kind": "restart",
                "choice": "restart",
                "probability": None,
                "confidence": None,
                "latency_ms": 0,
                "text": None,
                "text_helper": None,
                "text_latency_ms": 0,
                "operation": "RESTART",
                "target": None,
                "possible_closed_shadow": False,
                "note": f"CAPTCHA wall on {host}",
                "page_changed": False,
                "url": self._browser_url(browser),
                "usage": {},
                "executed_ms": state.get("elapsed_ms") or 0,
                "elapsed_ms": state.get("elapsed_ms") or 0,
            })
        trace = getattr(agent, "trace", None)
        if trace is not None:
            try:
                trace.emit(
                    "guard", step=len(history) if isinstance(history, list) else 0,
                    type="cloud_restart", host=host, country=fallback or None,
                )
            except Exception:
                pass
        self._publish(session_id, {
            "type": "restart", "message_id": message_id, "host": host,
            "country": fallback or None,
        })
        LOG.info(f"[jev:server] session {session_id[:8]} cloud restart after wall on {host}")
        return True

    @staticmethod
    def _close_turn_agent(entry, agent):
        """Release the turn's agent before its terminal status is visible. Idempotent."""
        if agent is None or entry.get("agent") is not agent:
            return
        entry["agent"] = None
        try:
            agent.close(keep_browser=True)
        except Exception:
            pass

    def _drive_message(self, session_id, message_id, text, max_decisions, entry, url=None):
        session = self.store.get_session(session_id, with_steps=False)
        if session is None:
            return
        run_id = message_id
        started = time.monotonic()
        self.store.update_message(message_id, status="running", run_id=run_id)
        self._publish(session_id, {"type": "message", "message_id": message_id, "status": "running"})
        agent = trace = None
        try:
            browser = self._ensure_browser(session_id, session["browser"])
            if url and not entry.get("navigated"):
                navigate = getattr(browser, "navigate", None)
                if callable(navigate):
                    navigate(url)
                entry["navigated"] = True
            elif (
                entry.pop("fresh_browser", False) and url is None
                and session.get("last_url") not in (None, "", "about:blank")
            ):
                # Idle-timeout or restart resume: reopen where the session was.
                navigate = getattr(browser, "navigate", None)
                if callable(navigate):
                    try:
                        navigate(session["last_url"])
                    except Exception:
                        pass
            current_url = self._browser_url(browser)
            background = session_background(
                self._prior_turns(session_id, message_id),
                current_url, session_tabs(browser),
            )
            agent = self._create_agent(
                session_id, run_id, text, browser, max_decisions, background=background,
            )
            trace = getattr(agent, "trace", None)
            entry["agent"] = agent
            recorded = 0
            last_tabs = tabs_signature(browser)
            files_dir = self.session_files_dir(session_id)
            last_files = files_signature(files_dir)
            while True:
                snapshot = agent.command("tick")
                recorded = self._record_and_publish_steps(
                    session_id, message_id, run_id, snapshot, recorded
                )
                tabs_now = tabs_signature(browser)
                if tabs_now != last_tabs:
                    last_tabs = tabs_now
                    self._publish(session_id, {
                        "type": "tabs", "message_id": message_id,
                        "tabs": session_tabs(browser),
                    })
                files_now = files_signature(files_dir)
                if files_now != last_files:
                    last_files = files_now
                    self._publish(session_id, {
                        "type": "files", "message_id": message_id,
                        "files": session_files(files_dir),
                    })
                if entry["stop"].is_set():
                    self._close_turn_agent(entry, agent)
                    self._finish_message(
                        session_id, message_id, snapshot, "blocked", "stopped by user",
                        trace, started, browser,
                    )
                    return
                if snapshot.get("status") in TERMINAL - {"failed"}:
                    self._close_turn_agent(entry, agent)
                    self._finish_message(
                        session_id, message_id, snapshot, snapshot["status"], None,
                        trace, started, browser,
                    )
                    return
                if self._maybe_restart_cloud(session_id, message_id, entry, agent):
                    browser = entry.get("browser") or browser
                    recorded = self._record_and_publish_steps(
                        session_id, message_id, run_id, snapshot, recorded
                    )
        except Exception as error:
            try:
                snapshot = agent.snapshot() if agent is not None else {}
            except Exception:
                snapshot = {}
            if trace is not None:
                trace.emit(
                    "error", step=len((snapshot or {}).get("history") or []),
                    type=type(error).__name__, message=str(error)[:300],
                    where="run", retried=False,
                )
            safe = str(redact_secrets(f"{type(error).__name__}: {error}"))[:500]
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            self._close_turn_agent(entry, agent)
            self._finish_message(
                session_id, message_id, snapshot, "failed", safe, trace, started,
                getattr(agent, "browser", None),
            )
        finally:
            self._close_turn_agent(entry, agent)

    def _prior_turns(self, session_id, message_id):
        current = self.store.get_message(message_id) or {}
        cutoff = current.get("n") or 0
        turns = self.store.session_messages(session_id)
        # session_messages is n-ordered with no ids; the current turn is last.
        return turns[: max(0, cutoff - 1)] if cutoff else turns[:-1]

    @staticmethod
    def _browser_url(browser):
        tabs = getattr(browser, "tabs", None) or {}
        active = getattr(browser, "active", None)
        if active in tabs:
            return tabs[active].get("url") or ""
        return ""

    def _record_new_steps(self, run_id, snapshot, recorded):
        history = snapshot.get("history") or []
        for entry in history[recorded:]:
            detail = entry.get("detail")
            if isinstance(detail, dict):
                detail = json.dumps(detail)
            self.store.add_step(
                run_id,
                entry.get("step", recorded + 1),
                entry.get("operation"),
                entry.get("action"),
                entry.get("url"),
                entry.get("elapsed_ms"),
                entry.get("note"),
                detail if isinstance(detail, str) else None,
            )
        return len(history)

    def _finish_message(self, session_id, message_id, snapshot, status, error, trace, started, browser):
        session = self.store.get_session(session_id, with_steps=False) or {}
        prompt, completion = tally_tokens(snapshot)
        cost, priced = price_message(snapshot)
        reply = compose_reply(snapshot, status, error)
        duration_ms = round((time.monotonic() - started) * 1000)
        cloud_seconds = cloud_cost = None
        if session.get("browser") == "cloud" and browser is not None:
            cloud_seconds = round(duration_ms / 1000, 3)
            rate = cloud_rate_per_hour()
            if rate is not None:
                cloud_cost = round(cloud_seconds / 3600 * rate, 6)
        page = snapshot.get("page") or {}
        final_text = final_text_for(browser, page)
        self._capture_final_frame(session_id, message_id)
        self.store.update_message(
            message_id,
            status=status,
            ended_at=time.time(),
            agent_reply=reply,
            input_tokens=prompt,
            output_tokens=completion,
            cost_usd_estimate=cost,
            priced=int(priced),
            cloud_seconds=cloud_seconds,
            cloud_cost_usd=cloud_cost,
            duration_ms=duration_ms,
            error=error,
            blocked_reason=snapshot.get("blocked_reason"),
            partial=int(bool(snapshot.get("partial"))),
            final_url=page.get("url"),
            final_text=final_text,
            answer_text=snapshot.get("answer"),
            decisions_count=len(snapshot.get("decisions") or []),
        )
        # No-op unless this message backs a legacy /api/tasks row.
        self.store.update_task(
            message_id,
            status=status,
            ended_at=time.time(),
            final_url=page.get("url"),
            verified_by_agent=int(status == "done"),
            input_tokens=prompt,
            output_tokens=completion,
            cost_usd_estimate=cost,
            error=error,
            final_text=final_text,
            answer_text=snapshot.get("answer"),
            decisions_count=len(snapshot.get("decisions") or []),
            blocked_reason=snapshot.get("blocked_reason"),
            partial=int(bool(snapshot.get("partial"))),
        )
        fields = {
            "last_active_at": time.time(),
            "total_input_tokens": (session.get("total_input_tokens") or 0) + prompt,
            "total_output_tokens": (session.get("total_output_tokens") or 0) + completion,
            "total_cost_usd": (session.get("total_cost_usd") or 0) + cost,
            "total_cloud_seconds": (session.get("total_cloud_seconds") or 0) + (cloud_seconds or 0),
            "total_cloud_cost_usd": (session.get("total_cloud_cost_usd") or 0) + (cloud_cost or 0),
            "last_tabs": json.dumps(session_tabs(browser)),
        }
        landed = (snapshot.get("page") or {}).get("url") or self._browser_url(browser)
        if landed and landed != "about:blank":
            fields["last_url"] = landed
        self.store.update_session(session_id, **fields)
        if trace is not None and not trace.ended:
            trace.emit(
                "end", step=len(snapshot.get("history") or []), status=status,
                blocked_reason=snapshot.get("blocked_reason"), error=(error or "")[:200],
                decisions=len(snapshot.get("decisions") or []),
                input_tokens=prompt, output_tokens=completion,
                cost_usd_estimate=cost, wall_ms=snapshot.get("elapsed_ms"),
                phases={phase: round(value) for phase, value in trace.phase_ms.items()},
            )
            trace.flush()
            try:
                trace.close()
            except Exception:
                pass
        self._publish(session_id, {
            "type": "reply", "message_id": message_id, "status": status,
            "reply": reply, "cost": cost, "duration_ms": duration_ms,
        })
        LOG.info(f"[jev:server] session {session_id[:8]} message {message_id[:8]} {status}")

    def get_viewer(self, session_id):
        """The session's live frame source, following the agent's active tab."""
        entry = self._entry(session_id)
        session = self.store.get_session(session_id, with_steps=False) if entry else None
        if session is None:
            return None, 404
        if session["browser"] != "local":
            return None, 409
        browser = entry.get("browser")
        active_session = getattr(browser, "session", None)
        call = getattr(browser, "call", None)
        if active_session is None or call is None:
            return None, 409
        with self._cond:
            viewer = entry.get("viewer")
            if viewer is not None and viewer.session != active_session:
                try:
                    viewer.shutdown()
                except Exception:
                    pass
                viewer = entry["viewer"] = None
            if viewer is None:
                viewer = Viewer(self.pump, call, active_session)
                entry["viewer"] = viewer
        return viewer, 200

    def live_url(self, session_id):
        entry = self._entry(session_id)
        if entry is None:
            return None
        return entry.get("live_url")


def live_url_for_agent_browser(browser):
    """A cloud session's live view, only when the operator opted into showing it."""
    if not env_bool("JEV_CLOUD_SHOW_LIVE_URL", False):
        return None
    handle = getattr(browser, "cloud", None)
    if isinstance(handle, dict):
        return handle.get("live_url")
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    STORE = None
    RUNNER = None
    HOST = "127.0.0.1"
    PORT = 8770
    TOKEN = ""

    def send(self, status, content, mime="application/json", extra_headers=None):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def authorized(self):
        if not self.TOKEN:
            return True
        presented = self.headers.get("Authorization") or ""
        return hmac.compare_digest(presented, f"Bearer {self.TOKEN}")

    def host_ok(self):
        if self.HOST in {"0.0.0.0", "::"}:
            return True
        return self.headers.get("Host") == f"{self.HOST}:{self.PORT}"

    def origin_ok(self):
        if self.HOST in {"0.0.0.0", "::"}:
            return True
        return self.headers.get("Origin") in (None, f"http://{self.HOST}:{self.PORT}")

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/" or path.startswith("/session/") or path.startswith("/share/"):
            return self.send(
                200, (ROOT / "static" / "sessions.html").read_text(), "text/html; charset=utf-8"
            )
        if path == "/sessions.js":
            return self.send(
                200, (ROOT / "static" / "sessions.js").read_text(), "text/javascript; charset=utf-8"
            )
        if path == "/sessions.css":
            return self.send(
                200, (ROOT / "static" / "sessions.css").read_text(), "text/css; charset=utf-8"
            )
        if path == "/tasks.html":
            return self.send(200, (ROOT / "static" / "tasks.html").read_text(), "text/html; charset=utf-8")
        if path == "/tasks.js":
            return self.send(
                200, (ROOT / "static" / "tasks.js").read_text(), "text/javascript; charset=utf-8"
            )
        if not path.startswith("/api/"):
            return self.send(404, json.dumps({"error": "Not found"}))
        if path == "/api/health":
            return self.send(200, json.dumps({"status": "ok"}))
        if path.startswith("/api/share/"):
            return self.serve_shared_replay(path.removeprefix("/api/share/"))
        if not self.authorized():
            return self.send(401, json.dumps({"error": "Bearer token required"}))
        if path == "/api/sessions":
            return self.send(200, json.dumps(self.STORE.list_sessions()))
        if path == "/api/usage":
            return self.send(200, json.dumps(self.STORE.usage_by_day()))
        if path == "/api/config":
            view = self.RUNNER.config_view()
            view["server_token_set"] = bool(self.TOKEN)
            return self.send(200, json.dumps(view))
        if path.startswith("/api/sessions/") and path.endswith("/events"):
            session_id = path.removeprefix("/api/sessions/").removesuffix("/events")
            query = dict(
                part.split("=", 1) if "=" in part else (part, "")
                for part in urlparse(self.path).query.split("&") if part
            )
            try:
                since = int(query.get("since", "0") or 0)
            except ValueError:
                since = 0
            return self.stream_session_events(session_id, since)
        if path.startswith("/api/sessions/") and "/files" in path:
            rest = path.removeprefix("/api/sessions/")
            session_id, _, name = rest.partition("/files")
            if not session_id or (name and not name.startswith("/")):
                return self.send(404, json.dumps({"error": "Not found"}))
            if not name:
                return self.serve_file_list(session_id)
            return self.serve_file_download(session_id, name.removeprefix("/"))
        if path.startswith("/api/sessions/"):
            session_id = path.removeprefix("/api/sessions/")
            if "/" in session_id or not session_id:
                return self.send(404, json.dumps({"error": "Not found"}))
            return self.serve_session(session_id)
        if path == "/api/tasks":
            return self.send(200, json.dumps(self.RUNNER.list_task_views()))
        if path.startswith("/api/tasks/") and path.endswith("/live"):
            return self.stream_live(path.removeprefix("/api/tasks/").removesuffix("/live"))
        if path.startswith("/api/tasks/") and path.endswith("/trace"):
            return self.serve_trace(path.removeprefix("/api/tasks/").removesuffix("/trace"), download=False)
        if path.startswith("/api/tasks/") and path.endswith("/trace.jsonl"):
            return self.serve_trace(
                path.removeprefix("/api/tasks/").removesuffix("/trace.jsonl"), download=True
            )
        if path.startswith("/api/tasks/") and path.endswith("/failshot"):
            return self.serve_failshot(path.removeprefix("/api/tasks/").removesuffix("/failshot"))
        task_id = path.removeprefix("/api/tasks/")
        if "/" in task_id or not task_id:
            return self.send(404, json.dumps({"error": "Not found"}))
        task = self.RUNNER.task_view(task_id)
        if task is None:
            task = self.STORE.get_task(task_id)
        if task is None:
            return self.send(404, json.dumps({"error": "Unknown task"}))
        return self.send(200, json.dumps(task))

    def serve_session(self, session_id):
        session = self.STORE.get_session(session_id)
        if session is None:
            return self.send(404, json.dumps({"error": "Unknown session"}))
        entry = self.RUNNER._entry(session_id)
        browser = entry.get("browser") if entry else None
        session["tabs"] = session_tabs(browser) if browser is not None else session["last_tabs"]
        session["live_url"] = self.RUNNER.live_url(session_id)
        session["browser_alive"] = browser is not None
        session["files"] = session_files(self.RUNNER.session_files_dir(session_id))
        return self.send(200, json.dumps(session))

    def serve_file_list(self, session_id):
        if self.STORE.get_session(session_id, with_steps=False) is None:
            return self.send(404, json.dumps({"error": "Unknown session"}))
        files_dir = self.RUNNER.session_files_dir(session_id)
        return self.send(200, json.dumps({"files": session_files(files_dir)}))

    def serve_file_download(self, session_id, name):
        if self.STORE.get_session(session_id, with_steps=False) is None:
            return self.send(404, json.dumps({"error": "Unknown session"}))
        name = unquote(name)
        if not _file_name_ok(name):
            return self.send(404, json.dumps({"error": "Not found"}))
        path = self.RUNNER.session_files_dir(session_id) / name
        if path.parent != self.RUNNER.session_files_dir(session_id):
            return self.send(404, json.dumps({"error": "Not found"}))
        try:
            body = path.read_bytes()
        except OSError:
            return self.send(404, json.dumps({"error": "Not found"}))
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return self.send(
            200, body, mime, {"Content-Disposition": f'attachment; filename="{name}"'},
        )

    def serve_file_upload(self, session_id, body):
        if self.STORE.get_session(session_id, with_steps=False) is None:
            return self.send(404, json.dumps({"error": "Unknown session"}))
        try:
            name = validate_save_filename((body or {}).get("name"))
        except ValueError as error:
            return self.send(400, json.dumps({"error": str(error)}))
        try:
            raw = base64.b64decode((body or {}).get("content_b64") or "", validate=True)
        except (binascii.Error, ValueError):
            return self.send(400, json.dumps({"error": "content_b64 is not valid base64"}))
        if not raw or len(raw) > 8 * 1024 * 1024:
            return self.send(400, json.dumps({"error": "File must be 1 byte to 8 MiB"}))
        files_dir = self.RUNNER.session_files_dir(session_id)
        path = files_dir / name
        if path.parent != files_dir:
            return self.send(400, json.dumps({"error": "Invalid file name"}))
        try:
            path.write_bytes(raw)
        except OSError:
            return self.send(500, json.dumps({"error": "Could not store the file"}))
        self.RUNNER._publish(
            session_id, {"type": "files", "files": session_files(files_dir)}
        )
        return self.send(201, json.dumps({"name": name, "bytes": len(raw)}))

    def serve_shared_replay(self, token):
        """Public read-only replay. The unguessable token is the auth."""
        if "/" in token or not token:
            return self.send(404, json.dumps({"error": "Not found"}))
        session = self.STORE.get_session_by_share_token(token)
        if session is None:
            return self.send(404, json.dumps({"error": "Not found"}))
        turns = []
        for message in session.get("messages") or []:
            turns.append({
                "n": message.get("n"),
                "user_text": message.get("user_text"),
                "agent_reply": message.get("agent_reply"),
                "status": message.get("status"),
                "cost": message.get("cost_usd_estimate"),
                "priced": message.get("priced"),
                "input_tokens": message.get("input_tokens"),
                "output_tokens": message.get("output_tokens"),
                "cloud_seconds": message.get("cloud_seconds"),
                "cloud_cost_usd": message.get("cloud_cost_usd"),
                "duration_ms": message.get("duration_ms"),
                "final_url": message.get("final_url"),
                "steps": message.get("steps") or [],
                "frame": self.RUNNER.final_frame(session["id"], message.get("id")),
            })
        return self.send(200, json.dumps({
            "title": session.get("title"),
            "browser": session.get("browser"),
            "created_at": session.get("created_at"),
            "turns": turns,
        }))

    def serve_config_update(self, body):
        try:
            self.RUNNER.apply_config(body)
        except ValueError as error:
            return self.send(400, json.dumps({"error": str(error)}))
        if "server_token" in body:
            token = body["server_token"]
            if token is None:
                token = ""
            if not isinstance(token, str) or len(token) > 200:
                return self.send(400, json.dumps({"error": "server_token must be a short string"}))
            type(self).TOKEN = token
        self.RUNNER.save_config({
            key: body[key]
            for key in (
                "browser_default", "max_decisions_default", "text_model",
                "decision_provider", "model_default", "effort_default",
                "agent", "server_token",
            )
            if key in body
        })
        view = self.RUNNER.config_view()
        view["server_token_set"] = bool(self.TOKEN)
        return self.send(200, json.dumps(view))

    def serve_takeover(self, session_id):
        session = self.STORE.get_session(session_id, with_steps=False)
        if session is None:
            return self.send(404, json.dumps({"error": "Unknown session"}))
        if session["browser"] == "cloud":
            return self.send(200, json.dumps({
                "mode": "cloud", "live_url": self.RUNNER.live_url(session_id),
            }))
        entry = self.RUNNER._entry(session_id)
        browser = entry.get("browser") if entry else None
        if browser is None:
            return self.send(409, json.dumps({"error": "No live browser for this session"}))
        target = getattr(browser, "target", None)
        if target:
            try:
                harness_helpers.cdp("Target.activateTarget", targetId=target)
            except Exception:
                pass
        bring = getattr(browser, "call", None)
        if callable(bring):
            try:
                bring("Page.bringToFront")
            except Exception:
                pass
        return self.send(200, json.dumps({"mode": "local", "ok": True}))

    def _run_id_for(self, task_id):
        """A task id is a message id; its run id keys steps, events, and traces."""
        message = self.STORE.get_message(task_id)
        if message is not None:
            return message.get("run_id") or task_id
        if self.STORE.get_task(task_id) is not None:
            return task_id
        return None

    def serve_trace(self, task_id, download):
        if "/" in task_id or not task_id:
            return self.send(404, json.dumps({"error": "Not found"}))
        run_id = self._run_id_for(task_id)
        if run_id is None:
            return self.send(404, json.dumps({"error": "Unknown task"}))
        events = self.STORE.get_events(run_id)
        if not download:
            return self.send(200, json.dumps({"task_id": task_id, "events": events}))
        body = "".join(json.dumps(event) + "\n" for event in events)
        return self.send(
            200, body, "application/x-ndjson",
            {"Content-Disposition": f'attachment; filename="trace-{task_id}.jsonl"'},
        )

    def serve_failshot(self, task_id):
        if "/" in task_id or not task_id or not all(char.isalnum() or char == "-" for char in task_id):
            return self.send(404, json.dumps({"error": "Not found"}))
        run_id = self._run_id_for(task_id)
        if run_id is None:
            return self.send(404, json.dumps({"error": "Unknown task"}))
        try:
            shots = sorted(trace_dir(run_id).glob("fail-step-*.png"))
        except OSError:
            shots = []
        if not shots:
            return self.send(404, json.dumps({"error": "No failure screenshot"}))
        try:
            return self.send(200, shots[-1].read_bytes(), "image/png")
        except OSError:
            return self.send(404, json.dumps({"error": "No failure screenshot"}))

    def stream_live(self, task_id):
        message = self.STORE.get_message(task_id)
        if message is None:
            if self.STORE.get_task(task_id) is None:
                return self.send(404, json.dumps({"error": "Unknown task"}))
            return self.send(409, json.dumps({"error": "Live view needs a running local task"}))
        if message["status"] in TERMINAL:
            return self.send(409, json.dumps({"error": "Live view needs a running local task"}))
        viewer, status = self.RUNNER.wait_for_viewer(message["session_id"])
        if viewer is None:
            if status == 404:
                return self.send(404, json.dumps({"error": "Unknown task"}))
            return self.send(409, json.dumps({"error": "Live view needs a running local task"}))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        viewer.add_viewer()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            last_seq = 0
            while True:
                with viewer.cond:
                    viewer.cond.wait_for(lambda: viewer.seq > last_seq, timeout=1.0)
                message = self.STORE.get_message(task_id)
                if message is None or message["status"] in TERMINAL:
                    payload = {"ended": True, "status": (message or {}).get("status")}
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                    self.wfile.flush()
                    return
                if viewer.seq > last_seq:
                    last_seq = viewer.seq
                    payload = {"seq": last_seq, "image": viewer.latest}
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            viewer.remove_viewer()

    def stream_session_events(self, session_id, since=0):
        if "/" in session_id or not session_id:
            return self.send(404, json.dumps({"error": "Not found"}))
        session = self.STORE.get_session(session_id, with_steps=False)
        if session is None:
            return self.send(404, json.dumps({"error": "Unknown session"}))
        entry = self.RUNNER._entry(session_id)
        bus = entry["bus"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        viewer = None
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            if session["browser"] == "local":
                viewer, _ = self.RUNNER.get_viewer(session_id)
                if viewer is not None:
                    viewer.add_viewer()
            else:
                live_url = self.RUNNER.live_url(session_id)
                if live_url:
                    self.wfile.write(
                        f"data: {json.dumps({'type': 'live_url', 'live_url': live_url})}\n\n".encode()
                    )
                    self.wfile.flush()
            last_seq, last_frame = since, 0
            for seq, event in self.RUNNER.replay(session_id, since):
                last_seq = seq
                self.wfile.write(f"data: {json.dumps({'seq': seq, **event})}\n\n".encode())
            # Replayed tabs/files may be stale; the live snapshot wins by order.
            entry_now = self.RUNNER._entry(session_id)
            live_browser = entry_now.get("browser") if entry_now else None
            if live_browser is not None:
                self.wfile.write(
                    f"data: {json.dumps({'type': 'tabs', 'tabs': session_tabs(live_browser)})}\n\n".encode()
                )
            files_dir = self.RUNNER.session_files_dir(session_id)
            self.wfile.write(
                f"data: {json.dumps({'type': 'files', 'files': session_files(files_dir)})}\n\n".encode()
            )
            self.wfile.flush()
            while True:
                with bus.cond:
                    bus.cond.wait_for(lambda: bus.seq > last_seq, timeout=1.0)
                if viewer is not None:
                    current, _ = self.RUNNER.get_viewer(session_id)
                    if current is not viewer:
                        viewer.remove_viewer()
                        viewer = current
                        if viewer is not None:
                            viewer.add_viewer()
                if viewer is not None and viewer.seq > last_frame:
                    last_frame = viewer.seq
                    payload = {"type": "frame", "seq": last_frame, "image": viewer.latest}
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                elif bus.seq > last_seq:
                    for seq, event in self.RUNNER.replay(session_id, last_seq):
                        last_seq = seq
                        self.wfile.write(
                            f"data: {json.dumps({'seq': seq, **event})}\n\n".encode()
                        )
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if viewer is not None:
                viewer.remove_viewer()

    def do_POST(self):
        if not (self.host_ok() and self.origin_ok()):
            return self.send(403, json.dumps({"error": "Local requests only"}))
        if not self.authorized():
            return self.send(401, json.dumps({"error": "Bearer token required"}))
        try:
            path = urlparse(self.path).path
            limit = 12 * 1024 * 1024 if path.endswith("/files") else 16384
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length < limit:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/sessions":
                session_id = self.RUNNER.create_session(
                    body.get("browser") or None, body.get("title")
                )
                return self.send(201, json.dumps({"id": session_id}))
            if path == "/api/config":
                return self.serve_config_update(body or {})
            if path.startswith("/api/sessions/") and path.endswith("/messages"):
                session_id = path.removeprefix("/api/sessions/").removesuffix("/messages")
                message_id = self.RUNNER.submit_message(
                    session_id, body.get("text"), body.get("max_decisions"), None,
                    body.get("model"), body.get("effort"),
                )
                if message_id is None:
                    return self.send(404, json.dumps({"error": "Unknown session"}))
                return self.send(201, json.dumps({"id": message_id}))
            if path.startswith("/api/sessions/") and path.endswith("/stop"):
                session_id = path.removeprefix("/api/sessions/").removesuffix("/stop")
                if self.STORE.get_session(session_id, with_steps=False) is None:
                    return self.send(404, json.dumps({"error": "Unknown session"}))
                self.RUNNER.stop(session_id)
                return self.send(200, json.dumps({"id": session_id, "status": "stopping"}))
            if path.startswith("/api/sessions/") and path.endswith("/files"):
                session_id = path.removeprefix("/api/sessions/").removesuffix("/files")
                return self.serve_file_upload(session_id, body)
            if path.startswith("/api/sessions/") and path.endswith("/takeover"):
                session_id = path.removeprefix("/api/sessions/").removesuffix("/takeover")
                return self.serve_takeover(session_id)
            if path.startswith("/api/sessions/") and path.endswith("/share"):
                session_id = path.removeprefix("/api/sessions/").removesuffix("/share")
                session = self.STORE.get_session(session_id, with_steps=False)
                if session is None:
                    return self.send(404, json.dumps({"error": "Unknown session"}))
                token = session.get("share_token") or secrets.token_urlsafe(24)
                if not session.get("share_token"):
                    self.STORE.update_session(session_id, share_token=token)
                return self.send(200, json.dumps({"token": token, "url": f"/share/{token}"}))
            if path == "/api/tasks":
                task_id = self.RUNNER.submit_task(
                    body.get("goal"),
                    body.get("url"),
                    body.get("browser", "local"),
                    body.get("max_decisions"),
                )
                return self.send(201, json.dumps({"id": task_id}))
            if path.startswith("/api/tasks/") and path.endswith("/stop"):
                task_id = path.removeprefix("/api/tasks/").removesuffix("/stop")
                task = self.RUNNER.stop_message(task_id)
                if task is None:
                    if self.STORE.get_task(task_id) is None:
                        return self.send(404, json.dumps({"error": "Unknown task"}))
                    return self.send(200, json.dumps({"id": task_id, "status": "blocked"}))
                return self.send(200, json.dumps({"id": task_id, "status": task["status"]}))
            return self.send(404, json.dumps({"error": "Not found"}))
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            self.send(500, json.dumps({"error": "Task request failed; nothing was retried."}))

    def do_DELETE(self):
        if not (self.host_ok() and self.origin_ok()):
            return self.send(403, json.dumps({"error": "Local requests only"}))
        if not self.authorized():
            return self.send(401, json.dumps({"error": "Bearer [REDACTED] required"}))
        path = urlparse(self.path).path
        if path.startswith("/api/sessions/"):
            session_id = path.removeprefix("/api/sessions/")
            if "/" in session_id or not session_id:
                return self.send(404, json.dumps({"error": "Not found"}))
            if self.STORE.get_session(session_id, with_steps=False) is None:
                return self.send(404, json.dumps({"error": "Unknown session"}))
            self.RUNNER.delete_session(session_id)
            return self.send(200, json.dumps({"id": session_id, "deleted": True}))
        return self.send(404, json.dumps({"error": "Not found"}))

    def log_message(self, *_args):
        pass


def main():
    load_environment()
    configure_logging()
    # Local tasks drive one pinned Chrome through our own daemon, in visible
    # tabs. BU_CDP_URL selects the Chrome (e.g. http://127.0.0.1:9222).
    os.environ.setdefault("BU_NAME", "jev-server")
    harness_helpers.NAME = harness_admin.NAME = os.environ["BU_NAME"]
    os.environ.setdefault("JEV_VISIBLE_TAB", "1")
    reclaim_daemon(os.environ["BU_NAME"])
    host = os.environ.get("JEV_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("JEV_SERVER_PORT", "8770"))
    db_path = os.environ.get("JEV_RUNS_DB", str(Path.cwd() / "jev_runs.db"))
    try:
        max_concurrent = max(1, int(os.environ.get("JEV_MAX_CONCURRENT", "2")))
    except ValueError:
        max_concurrent = 2
    Handler.STORE = Store(db_path)
    Handler.RUNNER = SessionRunner(Handler.STORE, max_concurrent=max_concurrent)
    Handler.HOST = host
    Handler.PORT = port
    Handler.TOKEN = os.environ.get("JEV_SERVER_TOKEN", "")
    config_path = os.environ.get(
        "JEV_SERVER_CONFIG", str(Path(db_path).parent / "jev_server_config.json")
    )
    stored = Handler.RUNNER.load_config(config_path)
    if not Handler.TOKEN and isinstance(stored.get("server_token"), str):
        Handler.TOKEN = stored["server_token"]
    if host not in {"127.0.0.1", "localhost", "::1"} and not Handler.TOKEN:
        raise SystemExit(f"Refusing to bind {host} without JEV_SERVER_TOKEN")
    server = ThreadingHTTPServer((host, port), Handler)
    LOG.info(f"Jev tasks: http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
