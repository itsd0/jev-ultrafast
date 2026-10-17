"""Minimal hosted-agent layer: task queue, SQLite run log, one-page UI.

New product surface only. The agent core (agent/browser/model) is unchanged:
each task drives a real Agent tick by tick, closes it in a finally block, and
records steps. Screenshots stay off; credentials are never logged or stored.
"""

import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from browser_harness import admin as harness_admin
from browser_harness import helpers as harness_helpers

from .agent import Agent
from .config import env_bool
from .demo import load_environment
from .model import redact_secrets, token_counts

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
    live_url TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    n INTEGER NOT NULL,
    operation TEXT,
    target_label TEXT,
    url TEXT,
    ms INTEGER,
    note TEXT
);
CREATE INDEX IF NOT EXISTS steps_task ON steps(task_id, n);
"""


class Store:
    """SQLite run log. One connection, one lock; safe across threads."""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def create_task(self, goal, url, browser, max_decisions):
        task_id = uuid.uuid4().hex
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
                "SELECT n, operation, target_label, url, ms, note FROM steps"
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
        return [dict(row) for row in rows]

    def update_task(self, task_id, **fields):
        if not fields:
            return
        names = ", ".join(f"{key} = ?" for key in fields)
        with self.lock:
            self.conn.execute(f"UPDATE tasks SET {names} WHERE id = ?", (*fields.values(), task_id))
            self.conn.commit()

    def add_step(self, task_id, n, operation, target_label, url, ms, note):
        with self.lock:
            self.conn.execute(
                "INSERT INTO steps (task_id, n, operation, target_label, url, ms, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, n, operation, target_label, url, ms, note),
            )
            self.conn.commit()


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


def cost_estimate_usd(prompt, completion):
    """Token-count cost. The rate is 0 unless JEV_COST_PER_1K_TOKENS is set."""
    try:
        rate = float(os.environ.get("JEV_COST_PER_1K_TOKENS") or 0)
    except ValueError:
        rate = 0
    return round((prompt + completion) / 1000 * max(rate, 0), 6)


def live_url_for(agent):
    """A cloud run's live view, and only when the operator opted into showing it."""
    if not env_bool("JEV_CLOUD_SHOW_LIVE_URL", False):
        return None
    handle = getattr(getattr(agent, "browser", None), "cloud", None)
    if isinstance(handle, dict):
        return handle.get("live_url")
    return None


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
    print(f"[jev:server] daemon {name} is stale; stopping it", file=sys.stderr, flush=True)
    try:
        harness_admin.restart_daemon(name)
    except Exception as error:
        print(f"[jev:server] daemon stop failed: {error}", file=sys.stderr, flush=True)


class Viewer:
    """One local task's frame source. Screencast primary, screenshot fallback.

    Frames arrive through the shared FramePump; the fallback captures directly.
    Quiet-restarting the screencast heals ack stalls (a drain elsewhere eats a
    frame) and is a no-op on a static page, which simply keeps its last frame.
    """

    QUIET_RESTART_S = 3.0
    SHOT_EVERY_S = 1.0

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


class Runner:
    """In-process FIFO queue. Worker threads drive one Agent each, tick by tick."""

    def __init__(self, store, max_concurrent=2, agent_factory=None, daemon_name=None):
        self.store = store
        self.max_concurrent = max(1, int(max_concurrent))
        self.agent_factory = agent_factory or Agent
        self.pump = FramePump(daemon_name or os.environ.get("BU_NAME", "default"))
        self._pending = deque()
        self._active = {}
        self._cond = threading.Condition()
        self._create_lock = threading.Lock()
        worker = threading.Thread(target=self._dispatch_loop, daemon=True)
        worker.start()

    def submit(self, goal, url, browser="local", max_decisions=None):
        goal = (goal or "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter a goal of 1-2,000 characters")
        url = (url or "").strip() or "about:blank"
        if url != "about:blank":
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Enter an http(s) start URL or leave it blank to navigate")
        if browser not in {"local", "cloud"}:
            raise ValueError("browser must be local or cloud")
        if max_decisions is not None and not 1 <= int(max_decisions) <= 10000:
            raise ValueError("max_decisions must be 1-10,000")
        task_id = self.store.create_task(goal, url, browser, max_decisions)
        with self._cond:
            self._pending.append(task_id)
            self._cond.notify()
        print(f"[jev:server] task {task_id[:8]} queued ({browser})", flush=True)
        return task_id

    def stop(self, task_id):
        with self._cond:
            task = self.store.get_task(task_id)
            if task is None:
                return None
            if task["status"] == "queued" and task_id in self._pending:
                self._pending.remove(task_id)
                self.store.update_task(
                    task_id, status="blocked", ended_at=time.time(), error="stopped by user"
                )
            elif task["status"] == "running" and task_id in self._active:
                self._active[task_id]["stop"].set()
            return self.store.get_task(task_id)

    def _dispatch_loop(self):
        while True:
            with self._cond:
                while not self._pending or len(self._active) >= self.max_concurrent:
                    self._cond.wait()
                task_id = self._pending.popleft()
                self._active[task_id] = {"stop": threading.Event()}
            worker = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
            with self._cond:
                self._active[task_id]["thread"] = worker
            worker.start()

    def _create_agent(self, task):
        # JEV_BROWSER is read once during construction. The lock plus restore
        # keeps mixed local/cloud tasks from seeing each other's value.
        with self._create_lock:
            previous = os.environ.get("JEV_BROWSER")
            os.environ["JEV_BROWSER"] = task["browser"]
            try:
                agent = self.agent_factory(task["url"], task["goal"], screenshots=False)
            finally:
                if previous is None:
                    os.environ.pop("JEV_BROWSER", None)
                else:
                    os.environ["JEV_BROWSER"] = previous
        if task["max_decisions"] is not None:
            agent.config["max_decisions"] = task["max_decisions"]
        return agent

    def _record_new_steps(self, task_id, snapshot, recorded):
        history = snapshot.get("history") or []
        for entry in history[recorded:]:
            self.store.add_step(
                task_id,
                entry.get("step", recorded + 1),
                entry.get("operation"),
                entry.get("action"),
                entry.get("url"),
                entry.get("elapsed_ms"),
                entry.get("note"),
            )
        return len(history)

    def _finish(self, task_id, snapshot, status, error=None):
        prompt, completion = tally_tokens(snapshot)
        page = snapshot.get("page") or {}
        self.store.update_task(
            task_id,
            status=status,
            ended_at=time.time(),
            final_url=page.get("url"),
            verified_by_agent=int(status == "done"),
            input_tokens=prompt,
            output_tokens=completion,
            cost_usd_estimate=cost_estimate_usd(prompt, completion),
            error=error,
        )
        print(
            f"[jev:server] task {task_id[:8]} {status}"
            f" tokens={prompt + completion} error={error or '-'}",
            flush=True,
        )

    def wait_for_viewer(self, task_id, timeout=20):
        """Like get_viewer, but waits out queueing and browser startup."""
        deadline = time.monotonic() + timeout
        while True:
            viewer, status = self.get_viewer(task_id)
            if viewer is not None or status == 404:
                return viewer, status
            task = self.store.get_task(task_id)
            if task is None or task["status"] in TERMINAL or task["browser"] != "local":
                return None, status
            if time.monotonic() >= deadline:
                return None, status
            time.sleep(0.2)

    def get_viewer(self, task_id):
        """A task's live frame source, or (None, status) when unavailable."""
        with self._cond:
            entry = self._active.get(task_id)
        task = self.store.get_task(task_id)
        if task is None:
            return None, 404
        if task["status"] != "running" or entry is None or task["browser"] != "local":
            return None, 409
        browser = getattr(entry.get("agent"), "browser", None)
        session = getattr(browser, "session", None)
        call = getattr(browser, "call", None)
        if session is None or call is None:
            return None, 409
        with self._cond:
            viewer = entry.get("viewer")
            if viewer is None:
                viewer = Viewer(self.pump, call, session)
                entry["viewer"] = viewer
        return viewer, 200

    def live_url(self, task_id):
        """A running cloud task's live view. In-memory only, never in the DB."""
        with self._cond:
            entry = self._active.get(task_id)
        if entry is None:
            return None
        return entry.get("live_url")

    def _run_task(self, task_id):
        with self._cond:
            stopper = self._active[task_id]["stop"]
        self.store.update_task(task_id, status="running", started_at=time.time())
        agent = None
        try:
            agent = self._create_agent(self.store.get_task(task_id))
            with self._cond:
                self._active[task_id]["agent"] = agent
                self._active[task_id]["live_url"] = live_url_for(agent)
            recorded = 0
            while True:
                snapshot = agent.command("tick")
                recorded = self._record_new_steps(task_id, snapshot, recorded)
                if stopper.is_set():
                    self._finish(task_id, snapshot, "blocked", "stopped by user")
                    return
                if snapshot.get("status") in TERMINAL - {"failed"}:
                    self._finish(task_id, snapshot, snapshot["status"])
                    return
        except Exception as error:
            try:
                snapshot = agent.snapshot() if agent is not None else {}
            except Exception:
                snapshot = {}
            safe = str(redact_secrets(f"{type(error).__name__}: {error}"))[:500]
            self._finish(task_id, snapshot if isinstance(snapshot, dict) else {}, "failed", safe)
        finally:
            if agent is not None:
                try:
                    agent.close()
                except Exception:
                    pass
            with self._cond:
                entry = self._active.pop(task_id, None)
                self._cond.notify()
            if entry is not None and entry.get("viewer") is not None:
                entry["viewer"].shutdown()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    STORE = None
    RUNNER = None
    HOST = "127.0.0.1"
    PORT = 8770
    TOKEN = ""

    def send(self, status, content, mime="application/json"):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def authorized(self):
        if not self.TOKEN:
            return True
        presented = self.headers.get("Authorization") or ""
        return hmac.compare_digest(presented, f"Bearer {self.TOKEN}")

    def do_GET(self):
        if self.headers.get("Host") != f"{self.HOST}:{self.PORT}":
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/":
            return self.send(200, (ROOT / "static" / "tasks.html").read_text(), "text/html; charset=utf-8")
        if path == "/tasks.js":
            return self.send(
                200, (ROOT / "static" / "tasks.js").read_text(), "text/javascript; charset=utf-8"
            )
        if not path.startswith("/api/"):
            return self.send(404, json.dumps({"error": "Not found"}))
        if not self.authorized():
            return self.send(401, json.dumps({"error": "Bearer token required"}))
        if path == "/api/tasks":
            return self.send(200, json.dumps(self.STORE.list_tasks()))
        if path.startswith("/api/tasks/") and path.endswith("/live"):
            return self.stream_live(path.removeprefix("/api/tasks/").removesuffix("/live"))
        task_id = path.removeprefix("/api/tasks/")
        if "/" in task_id or not task_id:
            return self.send(404, json.dumps({"error": "Not found"}))
        task = self.STORE.get_task(task_id)
        if task is None:
            return self.send(404, json.dumps({"error": "Unknown task"}))
        task["live_url"] = self.RUNNER.live_url(task_id)
        return self.send(200, json.dumps(task))

    def stream_live(self, task_id):
        viewer, status = self.RUNNER.wait_for_viewer(task_id)
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
                task = self.STORE.get_task(task_id)
                if task is None or task["status"] in TERMINAL:
                    payload = {"ended": True, "status": (task or {}).get("status")}
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

    def do_POST(self):
        origin = f"http://{self.HOST}:{self.PORT}"
        if (
            self.headers.get("Host") != f"{self.HOST}:{self.PORT}"
            or self.headers.get("Origin") not in (None, origin)
        ):
            return self.send(403, json.dumps({"error": "Local requests only"}))
        if not self.authorized():
            return self.send(401, json.dumps({"error": "Bearer token required"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length < 16384:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/tasks":
                task_id = self.RUNNER.submit(
                    body.get("goal"),
                    body.get("url"),
                    body.get("browser", "local"),
                    body.get("max_decisions"),
                )
                return self.send(201, json.dumps({"id": task_id}))
            if path.startswith("/api/tasks/") and path.endswith("/stop"):
                task_id = path.removeprefix("/api/tasks/").removesuffix("/stop")
                task = self.RUNNER.stop(task_id)
                if task is None:
                    return self.send(404, json.dumps({"error": "Unknown task"}))
                return self.send(200, json.dumps({"id": task_id, "status": task["status"]}))
            return self.send(404, json.dumps({"error": "Not found"}))
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            self.send(500, json.dumps({"error": "Task request failed; nothing was retried."}))

    def log_message(self, *_args):
        pass


def main():
    load_environment()
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
    Handler.RUNNER = Runner(Handler.STORE, max_concurrent=max_concurrent)
    Handler.HOST = host
    Handler.PORT = port
    Handler.TOKEN = os.environ.get("JEV_SERVER_TOKEN", "")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Jev tasks: http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
