"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import os
import random
import re
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from .browser import Browser, StalePage
from .config import resolve_config
from .model import (
    INVALID_RESPONSE,
    action_space,
    answer_context,
    choose,
    choose_mercury,
    decision_provider,
    decompose_goal,
    field_context,
    field_text,
    gate_context,
    judge_progress,
    match_option,
    post_json,
    token_counts,
    track_context,
)
from .questions import ANSWER_VALUE, GATE_SYSTEM, NAVIGATE_VALUE, TEXT_VALUE, TRACK_SYSTEM
from .trace import Trace, cost_estimate_usd, get_logger
from .vision import confirm_point, needs_vision, propose

_RELOAD_WORDS = frozenset({"reload", "refresh"})


def reload_like(action):
    """A control whose name says it loads the page again. Not tied to one site."""
    words = set(re.findall(r"[a-z]+", (action.get("label") or "").casefold()))
    return bool(words & _RELOAD_WORDS)


def note_no_progress(holds, action, page_changed, reasons=None, reason="no-progress hold"):
    """Hide an unchanged action for the next two decisions. A wait stays available.

    Keyed by noop_key, never by positional id: the hold must survive a re-render
    that shifts every id.
    """
    holds = dict(holds or {})
    if page_changed or not action.get("id") or action.get("kind") == "wait":
        return holds
    key = noop_key(action)
    holds[key] = 2
    if reasons is not None:
        reasons[key] = reason
    return holds


def offered_actions(actions, holds):
    held = holds or {}
    return [action for action in actions if held.get(noop_key(action), 0) <= 0]


def decay_holds(holds):
    return {key: left - 1 for key, left in (holds or {}).items() if left - 1 > 0}


LOOP_WINDOW = 8
LOOP_PERIODS = (2, 3)
LOOP_REPEATS = 2
LOOP_HOLD_STEPS = 3
LOOP_STOP_FIRES = 3
DEFAULT_MAX_DECISIONS = 80


def loop_step(decision, action, page, progress):
    """One executed step: operation, target label, and the page it acted on.

    Progress means a real navigation (new url/title) or a satisfied marker, not
    a DOM re-render of the same page.
    """
    return {
        "key": (
            decision.get("operation") or "",
            action.get("label") or "",
            page.get("url") or "",
            page.get("title") or "",
        ),
        "identity": (page.get("url") or "", page.get("title") or ""),
        "id": action.get("id"),
        "label": action.get("label") or "?",
        "kind": action.get("kind"),
        "role": action.get("role"),
        "progress": bool(progress),
    }


def same_page(window, page):
    """True when this page keeps the url and title of the previous step."""
    return bool(window) and window[-1].get("identity") == (page.get("url") or "", page.get("title") or "")


def detect_cycle(window, periods=LOOP_PERIODS, repeats=LOOP_REPEATS):
    """The smallest repeated A-B / A-B-C cycle at the tail with no progress step."""
    for period in periods:
        span = period * repeats
        if len(window) < span:
            continue
        tail = window[-span:]
        if any(step.get("progress") for step in tail):
            continue
        pattern = tail[:period]
        if not all(step.get("key") for step in pattern):
            continue
        if all(tail[index].get("key") == pattern[index % period].get("key") for index in range(span)):
            return {
                "ids": [step.get("id") for step in pattern],
                "labels": [step.get("label") or "?" for step in pattern],
                "keys": [noop_key(step) for step in pattern],
            }
    return None


def hold_actions(holds, keys, steps=LOOP_HOLD_STEPS, reasons=None, reason="held"):
    """Hold noop_keys (kind+label+role), never positional ids."""
    holds = dict(holds or {})
    for key in keys:
        if key:
            holds[key] = max(holds.get(key, 0), steps)
            if reasons is not None:
                reasons[key] = reason
    return holds


def decay_hold_reasons(reasons, holds):
    return {key: text for key, text in (reasons or {}).items() if (holds or {}).get(key, 0) > 0}


STALL_REPEATS = 3
STALL_HOLD_STEPS = 5
STALL_KINDS = {"click", "key", "select", "select_option", "navigate", "vision", "check"}
MAX_BACKS = 3

_SCORE_STOPWORDS = frozenset(
    "the a an to of and for on in with or by from click open link page more next result results show all".split()
)


def _score_words(text):
    return {
        word
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if word not in _SCORE_STOPWORDS and len(word) > 1
    }


def normalize_link_url(url):
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return ""
    if not parsed.hostname:
        return ""
    return (parsed.hostname.lower() + (parsed.path or "").rstrip("/")).rstrip("/")


def score_link(label, url, nav, todo_words, tried):
    """Rank a link for the current sub-goal. Result items win by word overlap;
    nav chrome and already-tried detours lose."""
    score = 2 * len(todo_words & _score_words(label)) + len(todo_words & _score_words(url))
    if nav:
        score -= 4
    if normalize_link_url(url) in tried:
        score -= 10
    return score


def detect_control_stall(window):
    """The same control actuated 3x with no progress: the widget is not responding.

    Typing and scrolling repeat legitimately, so only click-like actions stall.
    """
    if len(window) < STALL_REPEATS:
        return None
    tail = window[-STALL_REPEATS:]
    if any(step.get("progress") for step in tail):
        return None
    if any(step.get("kind") not in STALL_KINDS for step in tail):
        return None
    keys = {step.get("key")[:2] for step in tail if step.get("key")}
    if len(keys) != 1:
        return None
    return {"key": noop_key(tail[0]), "label": tail[0].get("label") or "?"}


def goal_page_target(goal):
    """A counted page goal: 'page 3' or '3rd page'. None when the goal counts nothing."""
    match = re.search(r"page\s+(\d+)", goal or "", re.IGNORECASE) or re.search(
        r"(\d+)(?:st|nd|rd|th)\s+page", goal or "", re.IGNORECASE
    )
    return int(match.group(1)) if match else None


def current_page_number(url, text):
    """The current page from generic pagination markers: ?p=/page/N or 'Page N of'."""
    match = re.search(r"[?&](?:p|page)=(\d+)", url or "") or re.search(r"/page/(\d+)", url or "")
    if match:
        return int(match.group(1))
    match = re.search(r"[Pp]age\s+(\d+)\s+of\b", text or "")
    return int(match.group(1)) if match else None


SUBGOAL_ACTIVATE_STEPS = 8
MEMO_CAP = 20


def distinct_hosts(history):
    hosts = set()
    for item in history or []:
        try:
            host = urlparse(item.get("url") or "").hostname or ""
        except ValueError:
            host = ""
        if host:
            hosts.add(host.lower())
    return hosts


def should_activate_subgoals(state):
    """Multi-site or long runs get sub-goal tracking; simple runs skip the overhead."""
    if state.get("subgoals") is not None:
        return False
    if len(distinct_hosts(state.get("history"))) >= 2:
        return True
    return len(state.get("history") or []) >= SUBGOAL_ACTIVATE_STEPS


def apply_judgment(state, judgment, source_url):
    """Fold satisfied sub-goals (with evidence) and new facts into the run."""
    subgoals = state.get("subgoals") or []
    by_id = {item["id"]: item for item in subgoals}
    for item in judgment.get("satisfied") or []:
        target = by_id.get(item.get("id"))
        if target is not None and not target.get("satisfied") and item.get("value"):
            target["satisfied"] = True
            target["value"] = item["value"]
            target["source"] = source_url
    memo = state.get("memo") or []
    seen = {item["fact"] for item in memo}
    for fact in judgment.get("facts") or []:
        if fact and fact not in seen:
            seen.add(fact)
            memo.append({"fact": fact, "source": source_url})
    state["memo"] = memo[-MEMO_CAP:]
    state["leave_site"] = bool(judgment.get("leave_site"))
    state["leave_site_url"] = source_url


def add_event_note(state, text):
    """One short-lived guidance note for the next few decisions."""
    step = len(state.get("history") or [])
    notes = [note for note in state.get("event_notes") or [] if step - note.get("step", 0) <= 4]
    notes.append({"step": step, "text": text})
    state["event_notes"] = notes[-2:]


def compose_guidance(state):
    """Derived run guidance for the decision model: sub-goals, facts, page count, budget."""
    step = len(state.get("history") or [])
    fresh = [note for note in state.get("event_notes") or [] if step - note.get("step", 0) <= 4]
    state["event_notes"] = fresh
    guidance = [note["text"] for note in fresh]
    if state.get("winding_down"):
        guidance.append("Budget nearly used: finish now with what you have.")
    subgoals = state.get("subgoals") or []
    if subgoals:
        done = [f"{item['text']} ({item['value']})" for item in subgoals if item.get("satisfied")]
        todo = [item["text"] for item in subgoals if not item.get("satisfied")]
        if todo:
            guidance.append(f"Done: {'; '.join(done) or 'nothing yet'}. Still need: {'; '.join(todo)}")
    memo = state.get("memo") or []
    if memo:
        known = f"Known facts ({len(memo)}): " + "; ".join(item["fact"] for item in memo[-8:])
        guidance.append(known[:400])
    target, current = state.get("page_target"), state.get("page_current")
    if target and current and current < target:
        guidance.append(f"On page {current} of {target}: keep going to the next page.")
    if state.get("force_navigate") and current_todo(state):
        guidance.append(f"Leave this site now: NAVIGATE for the next step ({current_todo(state)[:80]}).")
    elif state.get("subgoals") is not None and not target:
        todo_words = _score_words(current_todo(state))
        tried = set(state.get("tried_links") or [])
        ranked = sorted(
            (
                a
                for a in (state.get("page") or {}).get("actions", [])
                if a.get("kind") == "click" and a.get("role") == "link"
            ),
            key=lambda a: score_link(
                a.get("label"), action_url(state["page"], a), a.get("nav"), todo_words, tried
            ),
            reverse=True,
        )
        top = [
            a for a in ranked
            if score_link(a.get("label"), action_url(state["page"], a), a.get("nav"), todo_words, tried) > 0
        ][:3]
        if top:
            names = "; ".join((a.get("label") or "?")[:40] for a in top)
            guidance.append(f"For '{current_todo(state)[:60]}': consider {names}.")
    return guidance[:4]


def control_unchanged(action, before, after):
    """The click's own committed state did not move, even if the page re-rendered.

    A calendar cell that is already selected, or an "Open Departure" field that
    still shows the same value, must not be clicked again two steps later.
    """
    if action.get("kind") != "click":
        return None
    label = action.get("label")
    if not label:
        return None
    match = next(
        (item for item in (after.get("actions") or []) if item.get("kind") == "click" and item.get("label") == label),
        None,
    )
    if match is None:
        return None
    if action.get("value") and match.get("value") == action.get("value"):
        return "value unchanged"
    if action.get("expanded") == "true" and match.get("expanded") == "true":
        return "already open"
    for key in ("pressed", "selected"):
        if action.get(key) == "true" and match.get(key) == "true":
            return "already selected"
    return None


def norm_label(label):
    """Label identity: whitespace-collapsed and case-folded, so re-renders match."""
    return " ".join((label or "").split()).casefold()


def noop_key(action):
    """A control's identity across snapshots. Positional ids shift; kind+label+role do not.

    A plain string: state must stay JSON-serializable for the inspector.
    Every hold (loop, stall, no-progress, suppression) is keyed by this.
    """
    return f"{action.get('kind') or ''}\u0000{norm_label(action.get('label'))}\u0000{action.get('role') or ''}"


def pending_locator(page, choice):
    """The decided control's identity for stale retargeting. None when the choice
    is terminal, coordinate-based, or unknown: those re-decide instead."""
    if choice in {None, "DONE", "BLOCKED", "VISION_CLICK"}:
        return None
    action = next((a for a in (page.get("actions") or []) if a.get("id") == choice), None)
    if action is None:
        return None
    return {
        "kind": action.get("kind"),
        "role": action.get("role"),
        "label": action.get("label"),
        "frame": action.get("frame") or "f0",
        "old_id": action.get("id"),
    }


def retarget_action(page, locator):
    """Re-resolve a stale target in a fresh page by kind+role+label+frame.

    Returns (action, candidate_count). Only a unique match retargets; zero or
    several candidates mean the page genuinely changed, so the run re-decides.
    """
    if not locator:
        return None, 0
    wanted = norm_label(locator.get("label"))
    matches = [
        action
        for action in (page.get("actions") or [])
        if action.get("kind") == locator.get("kind")
        and (action.get("role") or None) == (locator.get("role") or None)
        and norm_label(action.get("label")) == wanted
        and (action.get("frame") or "f0") == (locator.get("frame") or "f0")
    ]
    if len(matches) == 1:
        return matches[0], 1
    return None, len(matches)


def page_entity_match(page, todo):
    """The page shares at least one distinctive word with the current sub-goal."""
    words = _score_words(todo)
    if not words:
        return True
    hay = _score_words(
        (page.get("title") or "") + " " + (page.get("url") or "") + " " + (page.get("text") or "")[:2000]
    )
    return bool(words & hay)


def action_url(page, action):
    guards = page.get("guards") or {}
    guard = guards.get(f"{action.get('frame') or 'f0'}:{action.get('node')}")
    if isinstance(guard, dict):
        return guard.get("href") or ""
    return ""


def current_todo(state):
    subgoals = state.get("subgoals") or []
    todo = [item["text"] for item in subgoals if not item.get("satisfied")]
    return todo[0] if todo else ""


HOST_BLOCK_TTL_S = 30 * 60
HOST_RETRY_MIN_S = 20
HOST_RETRY_MAX_S = 40
MAX_HOST_RETRIES = 1

_HOST_BLOCKS = {}


def block_host(state, host, now=None):
    """Block a captcha-walled host: this run backs off with jitter, other runs skip it for ~30 min."""
    if not host:
        return None
    now = time.monotonic() if now is None else now
    hosts = dict(state.get("blocked_hosts") or {})
    entry = dict(hosts.get(host) or {})
    entry["until"] = now + random.uniform(HOST_RETRY_MIN_S, HOST_RETRY_MAX_S)
    entry.setdefault("retries", 0)
    hosts[host] = entry
    state["blocked_hosts"] = hosts
    _HOST_BLOCKS[host] = now + HOST_BLOCK_TTL_S
    return entry


def host_blocked(state, host, now=None):
    """True while a host must be skipped: backoff running, retry spent, or another run blocked it."""
    if not host:
        return False
    now = time.monotonic() if now is None else now
    entry = (state.get("blocked_hosts") or {}).get(host)
    if entry is not None:
        if now < entry.get("until", 0):
            return True
        return entry.get("retries", 0) >= MAX_HOST_RETRIES
    return _HOST_BLOCKS.get(host, 0) > now


def take_host_retry(state, host, now=None):
    """Consume the single post-backoff retry. False when the host stays skipped."""
    if not host:
        return True
    now = time.monotonic() if now is None else now
    entry = (state.get("blocked_hosts") or {}).get(host)
    if entry is None:
        return _HOST_BLOCKS.get(host, 0) <= now
    if now < entry.get("until", 0) or entry.get("retries", 0) >= MAX_HOST_RETRIES:
        return False
    entry["retries"] = entry.get("retries", 0) + 1
    return True


def on_blocked_host(state):
    """The current page's host is blocked: the run must navigate away, not decide here."""
    url = (state.get("page") or {}).get("url") or ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host_blocked(state, host)


def suppression_reasons(state, page):
    """Suppressed noop_key -> reason. Forced-NAVIGATE keeps only navigate;
    otherwise zero-signal nav links and tried detours are hidden while choices remain."""
    actions = page.get("actions") or []
    if state.get("force_navigate"):
        return {noop_key(a): "forced NAVIGATE" for a in actions if a.get("kind") != "navigate"}
    if state.get("subgoals") is None or state.get("page_target"):
        # Pagination runs follow the page counter, not link scores.
        return {}
    todo_words = _score_words(current_todo(state))
    if not todo_words:
        return {}
    tried = set(state.get("tried_links") or [])
    links = [a for a in actions if a.get("kind") == "click" and a.get("role") == "link"]
    if not links:
        return {}
    drop = {}
    for action in links:
        norm = normalize_link_url(action_url(page, action))
        if norm and norm in tried:
            drop[noop_key(action)] = "tried link"
            continue
        score = score_link(action.get("label"), action_url(page, action), True, todo_words, tried)
        if action.get("nav") and score <= 0:
            drop[noop_key(action)] = "nav suppression"
    if set(drop) >= {noop_key(a) for a in links}:
        return {}
    return drop


def compute_suppressed(state, page):
    return set(suppression_reasons(state, page))


ACTION_STRATEGY = {
    "click": "click",
    "fill": "type+enter",
    "select": "select",
    "select_option": "SELECT_OPTION",
    "upload": "upload",
    "check": "SET_CHECKED",
    "key": "keyboard",
    "scroll": "scroll",
    "wait": "wait",
    "navigate": "navigate",
    "switch_tab": "switch_tab",
    "close_tab": "close_tab",
    "vision": "coordinate",
}


def compact_action(action):
    """The offered-controls row: id, label, role, kind, and widget state."""
    row = {"id": action.get("id"), "label": action.get("label"), "role": action.get("role"), "kind": action.get("kind")}
    for key in ("checked", "selected", "expanded", "pressed", "value", "current_value", "group"):
        if action.get(key) not in (None, ""):
            row[key] = action.get(key)
    return row


def page_for_choice(state):
    holds = state.get("held_actions") or {}
    actions = state["page"]["actions"]
    if holds:
        actions = offered_actions(actions, holds)
    suppressed = state.get("suppressed_keys") or set()
    if suppressed and state.get("suppressed_fp") == state["page"].get("fingerprint"):
        actions = [a for a in actions if noop_key(a) not in suppressed]
    dead = (state.get("noop_page") or {}).get(state["page"].get("url") or "")
    if dead:
        kept = [a for a in actions if noop_key(a) not in dead]
        if kept:
            actions = kept
            # The toggle is already open: offer the controls inside it first.
            actions = sorted(actions, key=lambda a: not a.get("inner"))
    if actions == state["page"]["actions"]:
        return state["page"]
    return {**state["page"], "actions": actions}


class Agent:
    def __init__(
        self,
        url,
        goals,
        *,
        record_dir=None,
        screenshots=False,
        shadow_dom=None,
        frames=None,
        files=None,
        tabs=None,
        nested_scroll=None,
        keyboard=None,
        large_select=None,
        select_inline_max=None,
        vision=None,
    ):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self.blocked_streak = 0
        self.config = resolve_config(
            shadow_dom=shadow_dom,
            frames=frames,
            files=files,
            tabs=tabs,
            nested_scroll=nested_scroll,
            keyboard=keyboard,
            large_select=large_select,
            select_inline_max=select_inline_max,
            vision=vision,
        )
        self.browser = Browser(url, config=self.config)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        self.log = get_logger("agent")
        self.trace = Trace(run_id=os.environ.get("JEV_RUN_ID") or None)
        try:
            started = time.perf_counter()
            page = self.browser.observe(screenshot=self.screenshots)
            indexer_ms = round((time.perf_counter() - started) * 1000)
        except Exception as error:
            self.trace.emit(
                "error", step=0, type=type(error).__name__, message=str(error)[:300],
                where="observe", retried=False,
                stack=traceback.format_exc(limit=5) if self.trace.level >= 2 else None,
            )
            self.trace.close()
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))
        self._emit_snapshot(self.state, 0, indexer_ms, page_changed=None)

    def _maybe_vision(self, state):
        if not (getattr(self, "config", None) or {}).get("vision"):
            return None
        page = state["page"]
        if not needs_vision(page):
            return None
        key = os.environ.get("VISION_API_KEY") or os.environ.get("TEXT_MODEL_API_KEY")
        if not key:
            return None
        if not page.get("screenshot") and getattr(state["browser"], "call", None):
            try:
                shot = state["browser"].call("Page.captureScreenshot", format="jpeg", quality=72)
                page = {**page, "screenshot": shot.get("data")}
                state["page"] = page
            except (RuntimeError, TypeError, KeyError):
                return None
        if not page.get("screenshot"):
            return None
        base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")

        def post(body):
            return post_json(base + "/chat/completions", key, body)

        try:
            raw = propose(page, state["goal"], post)
        except (RuntimeError, ValueError):
            return None
        confirmed = confirm_point(raw, page)
        if not confirmed:
            return None
        return {
            "choice": "VISION_CLICK",
            "operation": "VISION_CLICK",
            "target": None,
            "confidence": 1.0,
            "probabilities": {"VISION_CLICK": 1.0},
            "latency_ms": 0,
            "usage": {},
            "vision": confirmed,
            "model": "vision",
        }

    def _decide(self, state, page=None):
        page = page or state["page"]
        if decision_provider() != "mercury":
            return choose(page, state["goal"], state["history"], guidance=compose_guidance(state))
        fresh_requirements = state.get("requirements_goal") != state["goal"]
        if fresh_requirements:
            state["requirements"] = decompose_goal(state["goal"])
            state["requirements_goal"] = state["goal"]
            state["requirement_progress"] = {}
        decision = choose_mercury(
            page,
            state["goal"],
            state["history"],
            state["requirements"],
            state.get("requirement_progress") or {},
        )
        if fresh_requirements:
            decision["decompose_usage"] = getattr(decompose_goal, "last_usage", {}) or {}
        progress = {}
        for item in decision.get("requirements") or []:
            if item.get("id"):
                progress[item["id"]] = item.get("probability")
        state["requirement_progress"] = progress
        return decision

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def _emit_snapshot(self, state, step, indexer_ms, page_changed):
        page = state["page"]
        self.trace.emit(
            "snapshot",
            step=step,
            url=page.get("url"),
            title=page.get("title"),
            controls=len(page.get("actions") or []),
            modal_scoped=page.get("modal_scoped"),
            overlay=page.get("overlay"),
            fingerprint=(page.get("fingerprint") or "")[:16],
            page_changed=page_changed,
            duration_ms=indexer_ms,
        )

    def _emit_offered(self, state, step, shown):
        ids = {action.get("id") for action in shown.get("actions") or []}
        shown_keys = {noop_key(action) for action in shown.get("actions") or []}
        holds = state.get("held_actions") or {}
        reasons = state.get("hold_reasons") or {}
        held = {key: reasons.get(key, "held") for key, left in holds.items() if left > 0}
        suppressed = {
            key: reason
            for key, reason in (state.get("suppression_reasons") or {}).items()
            if key not in shown_keys
        }
        fields = {
            "offered": [compact_action(action) for action in shown.get("actions") or []],
            "offered_count": len(ids),
            "held": held,
            "suppressed": suppressed,
        }
        if self.trace.level >= 2:
            fields["offered_full"] = shown.get("actions") or []
        self.trace.emit("offered", step=step, **fields)

    def _emit_end(self, state, status):
        if self.trace.ended:
            return
        prompt = completion = 0
        for item in state.get("decisions") or []:
            for usage in (item.get("usage"), item.get("verifier_usage"), item.get("decompose_usage")):
                pin, pout = token_counts(usage)
                prompt += pin
                completion += pout
        for item in state.get("text_calls") or []:
            pin, pout = token_counts(item.get("usage"))
            prompt += pin
            completion += pout
        self.trace.emit(
            "end",
            step=len(state.get("history") or []),
            status=status,
            blocked_reason=state.get("blocked_reason"),
            decisions=len(state.get("decisions") or []),
            input_tokens=prompt,
            output_tokens=completion,
            cost_usd_estimate=cost_estimate_usd(prompt, completion),
            wall_ms=state.get("elapsed_ms"),
            phases={phase: round(value) for phase, value in self.trace.phase_ms.items()},
        )

    def _failshot(self, state):
        """A failure screenshot, captured on demand. Screenshots stay off otherwise."""
        try:
            shot = state["browser"].call("Page.captureScreenshot", format="jpeg", quality=60)
        except Exception:
            return None
        return shot.get("data") if isinstance(shot, dict) else None

    def _terminal(self, state, reason, step):
        """The single blocked stop: keeps the memo as a partial answer, then the evidence bundle."""
        state["status"] = "blocked"
        if reason is not None:
            state["blocked_reason"] = reason
        self._partial_close(state)
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        self.trace.save_failure(step, self._failshot(state))
        self._emit_end(state, "blocked")
        self.trace.flush()
        return self.snapshot()

    @staticmethod
    def _cdp(browser):
        snapshot = getattr(browser, "cdp_snapshot", None)
        if snapshot is None:
            return 0, 0.0
        try:
            calls, ms = snapshot()
            return int(calls), float(ms)
        except Exception:
            return 0, 0.0

    def _gen_text(self, state, step, context, system=TEXT_VALUE, purpose="field", action=None):
        """One text-helper call with its trace event. Failures emit, then raise."""
        label = (action or {}).get("label")
        if self.pending_text and self.pending_text[0] == context:
            _, text, helper = self.pending_text
            self.trace.emit(
                "text", step=step, purpose=purpose, model=(helper or {}).get("model"),
                effort=(helper or {}).get("effort"), latency_ms=0, duration_ms=0,
                retries=0, filtered=False, valid=True, cached=True,
                field_label=label, value=text,
            )
            return text, helper
        started = time.perf_counter()
        try:
            if system == TEXT_VALUE and purpose == "field":
                text, helper = field_text(context)
            else:
                text, helper = field_text(context, system=system)
        except (ValueError, RuntimeError) as error:
            self.trace.emit(
                "text", step=step, purpose=purpose, latency_ms=round((time.perf_counter() - started) * 1000),
                duration_ms=round((time.perf_counter() - started) * 1000),
                retries=0, filtered=False, valid=False, reason=str(error)[:200],
                field_label=label, value=None,
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000)
        self.trace.emit(
            "text", step=step, purpose=purpose, model=helper.get("model"), effort=helper.get("effort"),
            latency_ms=helper.get("latency_ms"), duration_ms=duration_ms,
            retries=helper.get("retries", 0), filtered=helper.get("filtered", False),
            valid=True, cached=False, field_label=label, value=text,
        )
        return text, helper

    @staticmethod
    def _verify_info(action, before_page, after_page, result):
        """Before/after widget state for the verify event, with the next strategy."""
        before = {key: action.get(key) for key in ("value", "checked", "selected", "expanded", "pressed")}
        label = action.get("label")
        match = next(
            (
                item
                for item in (after_page.get("actions") or [])
                if item.get("label") == label and item.get("kind") == action.get("kind")
            ),
            None,
        )
        after = (
            {key: match.get(key) for key in ("value", "checked", "selected", "expanded", "pressed")}
            if match is not None
            else None
        )
        kind = action.get("kind")
        commit = (result or {}).get("commit")
        if kind == "fill":
            verified = bool((result or {}).get("text_verified", True))
            if commit and (result or {}).get("committed", True):
                # The commit expanded the typed fragment to the full option
                # ("lisbon" -> "Lisbon, Portugal"); the text mismatch is expected.
                success, nxt = True, None
            elif commit:
                success, nxt = False, f"commit failed ({commit}); held"
            else:
                success, nxt = verified, None
            if not success and nxt is None:
                nxt = "held; try typing in the field twin and pressing Enter"
            changed = before.get("value") != (after or {}).get("value") if after else None
        elif kind == "check":
            success = bool((result or {}).get("already_set") or (result or {}).get("verified", False))
            changed = before.get("checked") != (after or {}).get("checked") if after else None
            nxt = None if success else "held as already selected; try a different control"
        elif kind == "select_option":
            success = match is not None or (result or {}).get("executed") is not None
            changed = before.get("value") != (after or {}).get("value") if after else None
            nxt = None if success else "no matching option; held"
        else:
            success = after_page.get("fingerprint") != before_page.get("fingerprint") or match is None
            changed = after_page.get("fingerprint") != before_page.get("fingerprint")
            nxt = None if success else "held for two steps"
        info = {"before": before, "after": after, "changed": changed, "success": success, "next": nxt}
        if commit:
            info["commit"] = {
                "strategy": commit, "picked": (result or {}).get("picked"),
                "committed": (result or {}).get("committed"), "reverted": (result or {}).get("reverted"),
                "retried": (result or {}).get("retried"),
            }
        return info

    def _tick(self, state, step):
        """One predict-act cycle. Unexpected errors propagate to tick's env catch."""
        try:
            self.command("predict", {})
        except StalePage as error:
            self.trace.emit(
                "error", step=step, type="StalePage", message=str(error)[:300],
                where="predict", retried=True,
                stack=traceback.format_exc(limit=5) if self.trace.level >= 2 else None,
            )
            state["decision"] = None
            state["status"] = "ready"
            try:
                started = time.perf_counter()
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                indexer_ms = round((time.perf_counter() - started) * 1000)
            except StalePage:
                # The document never settled, even after observe's own
                # backoff. Block honestly instead of failing the run.
                return self._terminal(state, "page never settled", step)
            self._emit_snapshot(state, step, indexer_ms, page_changed=True)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            self.trace.flush()
            return self.snapshot()
        if state["status"] in {"done", "blocked"}:
            return self.snapshot()
        decision = state.get("decision") or {}
        locator = pending_locator(state["page"], decision.get("choice"))
        return self._act_resilient(state, step, decision, locator)

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            step = len(state.get("history") or []) + 1
            try:
                return self._tick(state, step)
            except Exception as error:
                # Anything unexpected (dead daemon, API outage) ends the run
                # as blocked with the memo kept, never as a crashed run.
                self.trace.emit(
                    "error", step=step, type=type(error).__name__, message=str(error)[:300],
                    where="tick", retried=False,
                    stack=traceback.format_exc(limit=5) if self.trace.level >= 2 else None,
                )
                return self._terminal(state, f"env:{type(error).__name__}", step)

        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            step = len(state.get("history") or []) + 1
            prev_fp = state.get("_prev_fp")
            if not state["browser"].fresh(state["page"]):
                started = time.perf_counter()
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                indexer_ms = round((time.perf_counter() - started) * 1000)
            else:
                indexer_ms = 0
            changed = prev_fp is not None and state["page"].get("fingerprint") != prev_fp
            self._emit_snapshot(state, step, indexer_ms, page_changed=changed)
            state["_prev_fp"] = state["page"].get("fingerprint")
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            captcha = (state.get("page") or {}).get("captcha_reason")
            if captcha and not state.get("force_navigate"):
                # A wall, not a page: block the host and step back without
                # spending a decision. Once forcing, decide the way out.
                self._handle_captcha(state, step, captcha)
                return self.snapshot()
            cycle = detect_cycle(state.get("loop_window") or [])
            if cycle:
                reasons = dict(state.get("hold_reasons") or {})
                state["held_actions"] = hold_actions(
                    state.get("held_actions"), cycle["keys"], reasons=reasons, reason="loop hold"
                )
                state["hold_reasons"] = reasons
                state["loop_fires"] = state.get("loop_fires", 0) + 1
                state["loop_note"] = "loop detected: avoid " + "/".join(cycle["labels"])
                tail = (state.get("loop_window") or [])[-6:]
                self.trace.emit(
                    "guard", step=step, type="loop",
                    pattern="/".join(cycle["labels"]), ids=cycle["ids"], fires=state["loop_fires"],
                    evidence=[{"op": item.get("key", ["?"])[0], "label": item.get("label")} for item in tail],
                )
                if state["loop_fires"] >= LOOP_STOP_FIRES:
                    return self._terminal(state, "loop detected", step)
            self._stall_check(state, step)
            if "page_target" not in state:
                state["page_target"] = goal_page_target(state["goal"])
            if state["page_target"]:
                state["page_current"] = (
                    current_page_number(state["page"].get("url"), state["page"].get("text")) or 1
                )
            config = getattr(self, "config", None) or {}
            budget = config.get("max_decisions") or DEFAULT_MAX_DECISIONS
            if len(state["decisions"]) >= budget:
                self.trace.emit("guard", step=step, type="budget", used=len(state["decisions"]), budget=budget)
                return self._terminal(state, "decision budget", step)
            if len(state["decisions"]) >= budget * 0.75 and not state.get("winding_down"):
                state["winding_down"] = True
                self.trace.emit("guard", step=step, type="wind_down", used=len(state["decisions"]), budget=budget)
            self._track_progress(state, step)
            self._update_suppression(state, step)
            shown = page_for_choice(state)
            self._emit_offered(state, step, shown)
            retries = 0
            invalid_reason = None
            started = time.perf_counter()
            try:
                state["decision"] = self._maybe_vision(state) or self._decide(state, shown)
            except ValueError as exc:
                if str(exc) != INVALID_RESPONSE:
                    raise
                self.log.info("[jev:decision] invalid response; retrying once")
                invalid_reason = getattr(choose, "last_invalid", "") or "invalid response"
                try:
                    retries = 1
                    state["decision"] = self._maybe_vision(state) or self._decide(state, shown)
                except ValueError as retry_exc:
                    if str(retry_exc) != INVALID_RESPONSE:
                        raise
                    invalid_reason = getattr(choose, "last_invalid", "") or "invalid response"
                    self.trace.emit(
                        "decision", step=step, provider=decision_provider(), model=None,
                        latency_ms=None, duration_ms=round((time.perf_counter() - started) * 1000),
                        choice=None, retries=retries, invalid_reason=invalid_reason,
                    )
                    self.trace.emit(
                        "error", step=step, type="ValueError", message=INVALID_RESPONSE,
                        where="predict", retried=True,
                    )
                    return self._terminal(state, "invalid decision response", step)
            decision = state["decision"]
            fields = {
                "provider": decision_provider(), "model": decision.get("model"),
                "latency_ms": decision.get("latency_ms"),
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "choice": decision.get("choice"), "operation": decision.get("operation"),
                "target": decision.get("target"), "confidence": decision.get("confidence"),
                "retries": retries, "invalid_reason": invalid_reason,
            }
            if self.trace.level >= 2:
                fields["raw"] = decision.get("raw_answers")
            self.trace.emit("decision", step=step, **fields)
            state["held_actions"] = decay_holds(state.get("held_actions"))
            state["hold_reasons"] = decay_hold_reasons(state.get("hold_reasons"), state.get("held_actions"))
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            step = len(state.get("history") or []) + 1
            if selected in {"DONE", "BLOCKED"}:
                # Terminal choices are never freshness-gated: DONE must not go
                # stale on a live page, and BLOCKED must stay decidable.
                if selected == "BLOCKED" and self.blocked_streak < 3:
                    # A loading page looks identical to a dead end. Only trust
                    # BLOCKED once the page has settled; otherwise look again.
                    self.blocked_streak += 1
                    if self.blocked_streak == 3 and self._unblock_scroll(state, page, decision):
                        return self.snapshot()
                    state["browser"].settled(page)
                    state["decision"] = None
                    state["page"] = state["browser"].observe(screenshot=self.screenshots)
                    state["status"] = "ready"
                    return self.snapshot()
                if selected == "DONE" and decision_provider() != "mercury":
                    verdict = self._done_verdict(state, page, step)
                    self.trace.emit(
                        "progress", step=step, gate_accept=verdict["accept"],
                        gate_partial=verdict["partial"], gate_missing=verdict["missing"][:200],
                        todo=[item["text"] for item in state.get("subgoals") or [] if not item.get("satisfied")],
                    )
                    if not verdict["accept"]:
                        state["done_rejections"] = state.get("done_rejections", 0) + 1
                        add_event_note(state, f"Not done yet: still missing {verdict['missing'][:160]}.")
                        state["status"] = "ready"
                        return self.snapshot()
                    state["partial"] = verdict["partial"]
                state["plan_index"] = int(selected == "DONE")
                if selected == "BLOCKED":
                    return self._terminal(state, state.get("blocked_reason") or "gave up", step)
                state["status"] = "done"
                state["answer"] = self._final_answer(state, page, step)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                self._emit_end(state, state["status"])
                self.trace.flush()
                return self.snapshot()
            if selected == "VISION_CLICK":
                point = decision.get("vision") or {}
                action = {
                    "id": "VISION_CLICK",
                    "kind": "vision",
                    "label": f"coordinate-based click at {float(point.get('x', 0)):.0f},{float(point.get('y', 0)):.0f}",
                    "x": point.get("x"),
                    "y": point.get("y"),
                }
            else:
                action = next(a for a in page["actions"] if a["id"] == selected)
            action_budget = (getattr(self, "config", None) or {}).get("max_decisions") or DEFAULT_MAX_DECISIONS
            if len(state["history"]) >= action_budget:
                # Every action costs a decision, so the action cap follows the
                # run's decision budget instead of a smaller fixed number.
                return self._terminal(state, "action budget", step)
            text, helper = None, None
            unmatched_option = False
            if action["kind"] in {"fill", "select_option"}:
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                cached = self.pending_text and self.pending_text[0] == context
                try:
                    text, helper = self._gen_text(state, step, context, TEXT_VALUE, "field", action)
                except ValueError:
                    return self._skip_text_step(state, page, action, decision, selected)
                if not cached:
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
                if action["kind"] == "fill" and action.get("value") == text:
                    # The committed value already equals the goal value. Re-typing
                    # it is a mutation with no effect; mark the step satisfied.
                    return self._skip_already_set(state, page, action, decision, selected, text, helper)
            result = {"executed": action["id"]}
            cdp_calls, cdp_ms = self._cdp(state["browser"])
            act_started = time.perf_counter()
            if action["kind"] == "navigate":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                cached = self.pending_text and self.pending_text[0] == context
                try:
                    text, helper = self._gen_text(state, step, context, NAVIGATE_VALUE, "navigate", action)
                except ValueError:
                    return self._skip_text_step(state, page, action, decision, selected)
                if not cached:
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
                try:
                    result = state["browser"].navigate(text)
                except ValueError:
                    return self._skip_text_step(
                        state, page, action, decision, selected, note="invalid navigation URL"
                    )
                self.pending_text = None
                self.blocked_streak = 0
            elif action["kind"] == "select_option":
                options = state["browser"].read_options(action) or []
                matched = match_option(text, options)
                if matched:
                    action = {**action, "kind": "select", "value": matched["value"]}
                    result = state["browser"].act(action, page, text=text)
                else:
                    unmatched_option = True
            elif action["kind"] == "vision":
                result = state["browser"].vision_click(action["x"], action["y"])
            else:
                # Browser.act checks freshness immediately before input, including after text generation.
                result = state["browser"].act(action, page, text=text)
            act_ms = round((time.perf_counter() - act_started) * 1000)
            cdp_after, cdp_ms_after = self._cdp(state["browser"])
            cdp_calls, cdp_ms = cdp_after - cdp_calls, round(cdp_ms_after - cdp_ms)
            if not unmatched_option:
                self.pending_text = None
                self.blocked_streak = 0
            text_failed = isinstance(result, dict) and result.get("text_verified") is False
            if unmatched_option:
                text_failed = False
            if action.get("kind") == "fill" and isinstance(result, dict) and result.get("commit"):
                # A committed expansion rewrites the typed fragment by design.
                text_failed = not result.get("committed", True)
            switched = None
            if getattr(type(state["browser"]), "collect_new_tabs", None):
                opened = state["browser"].collect_new_tabs()
                if opened:
                    tab = state["browser"].activate(opened[-1])
                    url = tab.get("url") if isinstance(tab, dict) else ""
                    switched = f"switched to new tab {url or opened[-1]}"
            state["browser"].wait_until_ready()
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            check_set = (
                action["kind"] == "check" and isinstance(result, dict) and result.get("already_set")
            )
            check_failed = (
                action["kind"] == "check"
                and isinstance(result, dict)
                and not result.get("already_set")
                and not result.get("verified", False)
            )
            navigate_failed = (
                action["kind"] == "navigate"
                and isinstance(result, dict)
                and result.get("navigated") is False
            )
            same_date = (
                action["kind"] == "fill"
                and isinstance(result, dict)
                and result.get("same_date")
            )
            date_note = (
                f"field shows {result.get('typed')} for {text} (same date)"
                if same_date
                else None
            )
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "possible_closed_shadow": bool(action.get("possible_closed_shadow")),
                    "note": "; ".join(part for part in (
                        state.pop("loop_note", None),
                        switched,
                        "text entry failed" if text_failed else None,
                        "no matching option" if unmatched_option else None,
                        "coordinate-based" if action.get("kind") == "vision" else None,
                        "already selected" if check_set else None,
                        "value unchanged" if check_failed else None,
                        "navigation failed" if navigate_failed else None,
                        date_note,
                    ) if part) or None,
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            done_step = len(state["history"])
            strategy = ACTION_STRATEGY.get(action.get("kind"), action.get("kind"))
            commit_failed = False
            if action.get("kind") == "fill" and isinstance(result, dict) and result.get("commit"):
                strategy = "type+commit"
                commit_failed = not result.get("committed", True)
            self.trace.emit(
                "action", step=done_step, operation=decision["operation"], target=action["label"],
                kind=action.get("kind"), strategy=strategy,
                cdp_calls=cdp_calls, cdp_ms=cdp_ms, duration_ms=act_ms,
                ok=not (text_failed or check_failed or unmatched_option or commit_failed or navigate_failed),
            )
            verify_started = time.perf_counter()
            post_started = time.perf_counter()
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            post_ms = round((time.perf_counter() - post_started) * 1000)
            changed = state["page"]["fingerprint"] != page["fingerprint"]
            unchanged = control_unchanged(action, page, state["page"])
            if unchanged:
                base_note = state["history"][-1]["note"]
                state["history"][-1]["note"] = f"{base_note}; {unchanged}" if base_note else unchanged
                self._note_noop(state, action, unchanged)
            elif action.get("kind") == "click":
                (state.get("noop_streak") or {}).pop(noop_key(action), None)
            if not changed and reload_like(action) and getattr(state["browser"], "wait_for_refresh", None):
                refreshed = state["browser"].wait_for_refresh(
                    page["fingerprint"], timeout=3, screenshot=self.screenshots
                )
                if isinstance(refreshed, dict) and refreshed.get("fingerprint"):
                    state["page"] = refreshed
                    changed = refreshed["fingerprint"] != page["fingerprint"]
            verify = self._verify_info(action, page, state["page"], result)
            self.trace.emit(
                "verify", step=done_step, kind=action.get("kind"), before=verify["before"],
                after=verify["after"], changed=verify["changed"], success=verify["success"],
                next_strategy=verify["next"], commit=verify.get("commit"),
                duration_ms=round((time.perf_counter() - verify_started) * 1000),
            )
            self._emit_snapshot(state, done_step, post_ms, page_changed=changed)
            state["_prev_fp"] = state["page"].get("fingerprint")
            self.trace.maybe_step_shot(done_step, state["page"].get("screenshot"))
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=changed,
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            reasons = dict(state.get("hold_reasons") or {})
            state["held_actions"] = note_no_progress(
                state.get("held_actions"),
                action,
                False if unchanged else changed,
                reasons,
                unchanged if unchanged in ("already selected", "already open") else "no-progress hold",
            )
            state["hold_reasons"] = reasons
            if action.get("kind") != "wait":
                window = state.get("loop_window") or []
                progress = bool(switched) or not same_page(window, state["page"])
                window.append(loop_step(decision, action, state["page"], progress))
                state["loop_window"] = window[-LOOP_WINDOW:]
            if (
                action.get("kind") == "click"
                and state.get("subgoals") is not None
                and state["page"].get("url") != page.get("url")
            ):
                state["excursion"] = {
                    "from": page.get("url") or "",
                    "at": state["page"].get("url") or "",
                    "label": action.get("label") or "?",
                    "page": state.get("page_current"),
                }
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            stalled = len(repeated) == 3 and all(
                h["page_changed"] is False and h["kind"] != "wait" for h in repeated
            )
            state["status"] = "blocked" if stalled else "ready"
            if stalled:
                if any(h.get("possible_closed_shadow") for h in repeated):
                    state["blocked_reason"] = "possible closed shadow root"
                self.trace.emit(
                    "guard", step=len(state["history"]), type="stall",
                    evidence=[h.get("action") for h in repeated],
                )
                return self._terminal(state, state.get("blocked_reason"), len(state["history"]))
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def _skip_already_set(self, state, page, action, decision, selected, _text, helper):
        """The field already holds the goal value. Do not mutate it again."""
        self.pending_text = None
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        state["history"].append(
            {
                "step": len(state["history"]) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "choice": selected,
                "probability": decision["probabilities"].get(selected),
                "confidence": decision["confidence"],
                "latency_ms": decision["latency_ms"],
                "text": None,
                "text_helper": helper["model"] if helper else None,
                "text_latency_ms": helper["latency_ms"] if helper else 0,
                "operation": decision["operation"],
                "target": decision["target"],
                "possible_closed_shadow": False,
                "note": "; ".join(part for part in (state.pop("loop_note", None), "value already set") if part),
                "page_changed": False,
                "url": page["url"],
                "usage": decision.get("usage", {}),
                "executed_ms": state["elapsed_ms"],
                "elapsed_ms": state["elapsed_ms"],
            }
        )
        reasons = dict(state.get("hold_reasons") or {})
        state["held_actions"] = note_no_progress(
            state.get("held_actions"), action, False,
            reasons, "already set",
        )
        state["hold_reasons"] = reasons
        done_step = len(state["history"])
        self.trace.emit(
            "action", step=done_step, operation=decision["operation"], target=action["label"],
            kind=action.get("kind"), strategy="already-set", cdp_calls=0, cdp_ms=0,
            duration_ms=0, ok=True,
        )
        self.trace.emit(
            "verify", step=done_step, kind=action.get("kind"),
            before={"value": action.get("value")}, after={"value": action.get("value")},
            changed=False, success=True, next_strategy=None, duration_ms=0,
        )
        state.setdefault("loop_window", []).append(loop_step(decision, action, page, True))
        state["loop_window"] = state["loop_window"][-LOOP_WINDOW:]
        state["status"] = "ready"
        return self.snapshot()

    def _final_answer(self, state, page, step=None):
        """One best-effort summary of what the goal asked for. Never fails the run."""
        evidence = [
            {"subgoal": item["text"], "value": item.get("value"), "source": item.get("source")}
            for item in state.get("subgoals") or []
            if item.get("satisfied")
        ]
        step = len(state.get("history") or []) if step is None else step
        try:
            text, helper = self._gen_text(
                state,
                step,
                answer_context(
                    state["goal"],
                    page,
                    state["history"],
                    evidence,
                    state.get("memo") or [],
                    bool(state.get("partial")),
                ),
                ANSWER_VALUE,
                "answer",
            )
        except Exception:
            self.log.info("[jev:answer] text helper failed; finishing without an answer")
            return None
        state["text_calls"].append({**helper, "field": "final answer", "value": text})
        return text

    def _unblock_scroll(self, state, page, decision):
        """One look below the fold before accepting BLOCKED. Only
        visible-viewport controls are indexed, so a long page can hide the way
        forward. Bounded: once per page, and the streak stays spent, so the
        next consecutive BLOCKED still ends the run."""
        if state.get("unblock_scrolled") == page["fingerprint"]:
            return False
        scroll = next((a for a in page.get("actions", []) if a["id"] == "scroll_down"), None)
        if scroll is None:
            return False
        state["unblock_scrolled"] = page["fingerprint"]
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        state["history"].append(
            {
                "step": len(state["history"]) + 1,
                "action": "Scroll down (look below the fold)",
                "kind": "scroll",
                "choice": scroll["id"],
                "probability": (decision.get("probabilities") or {}).get(decision.get("choice")),
                "confidence": decision.get("confidence"),
                "latency_ms": decision.get("latency_ms"),
                "text": None,
                "text_helper": None,
                "text_latency_ms": 0,
                "operation": "SCROLL_DOWN",
                "target": scroll["id"],
                "possible_closed_shadow": False,
                "note": "exploring below the fold before giving up",
                "page_changed": None,
                "url": page["url"],
                "usage": decision.get("usage", {}),
                "executed_ms": state["elapsed_ms"],
                "elapsed_ms": state["elapsed_ms"],
            }
        )
        state["browser"].act(scroll, page)
        state["browser"].wait_until_ready()
        state["page"] = state["browser"].observe(screenshot=self.screenshots)
        changed = state["page"]["fingerprint"] != page["fingerprint"]
        state["history"][-1].update(page_changed=changed, url=state["page"]["url"])
        window = state.get("loop_window") or []
        window.append(loop_step({**decision, "operation": "SCROLL_DOWN"}, scroll, state["page"], changed))
        state["loop_window"] = window[-LOOP_WINDOW:]
        state["status"] = "ready"
        return True

    def _stall_check(self, state, step=None):
        stall = detect_control_stall(state.get("loop_window") or [])
        if not stall or not stall["key"]:
            return
        notified = state.get("stall_notified") or {}
        step = len(state.get("history") or []) if step is None else step
        if notified.get("key") == stall["key"] and step - notified.get("step", 0) < 6:
            return
        state["stall_notified"] = {"key": stall["key"], "step": step}
        reasons = dict(state.get("hold_reasons") or {})
        state["held_actions"] = hold_actions(
            state.get("held_actions"), [stall["key"]], STALL_HOLD_STEPS, reasons, "stall"
        )
        state["hold_reasons"] = reasons
        advice = self._stall_advice(state, stall)
        add_event_note(state, advice)
        self.trace.emit(
            "guard", step=step, type="stall", key=stall["key"], label=stall["label"],
            evidence=f"same control {STALL_REPEATS}x with no progress; {advice}"[:200],
        )

    def _stall_advice(self, state, stall):
        actions = state["page"].get("actions", [])
        action = next((item for item in actions if noop_key(item) == stall["key"]), {})
        if action.get("in_grid"):
            return (
                f"{stall['label'][:60]} is not responding; focus a day and use the Arrow keys "
                "to move, Enter to select."
            )
        sibling = next(
            (
                item
                for item in actions
                if item.get("kind") == "fill"
                and item.get("node") is not None
                and item.get("node") == action.get("node")
            ),
            None,
        )
        if sibling:
            return (
                f"{stall['label']} is not responding; try typing the value in "
                f"{sibling.get('label') or 'the field'} and pressing Enter instead."
            )
        return f"{stall['label']} is not responding; try a different strategy instead of clicking it again."

    def _note_noop(self, state, action, unchanged):
        """A click that cannot move its own state is a no-op: suppress it for the
        rest of this page and point the model at the controls inside it."""
        if action.get("kind") != "click":
            return
        url_now = state["page"].get("url") or ""
        if state.get("noop_url") != url_now:
            state["noop_url"] = url_now
            state["noop_page"] = {}
            state["noop_streak"] = {}
            state["noop_noted"] = []
        key = noop_key(action)
        streak = state["noop_streak"]
        streak[key] = streak.get(key, 0) + 1
        if unchanged != "already open" and streak[key] < 2:
            return
        dead = state["noop_page"].setdefault(url_now, [])
        if key not in dead:
            dead.append(key)
        if key in state["noop_noted"]:
            return
        state["noop_noted"].append(key)
        add_event_note(state, f"'{action.get('label')}' is already open; use the controls inside it.")
        self.trace.emit(
            "guard", step=len(state.get("history") or []), type="noop", label=action.get("label"),
            evidence=f"{unchanged} (streak {streak[key]})",
        )

    def _track_progress(self, state, step=None):
        step = len(state.get("history") or []) if step is None else step
        if should_activate_subgoals(state):
            started = time.perf_counter()
            items = decompose_goal(state["goal"])
            state["subgoals"] = [{"id": index, "text": text} for index, text in enumerate(items)]
            state["text_calls"].append(
                {
                    "model": os.environ.get("TEXT_MODEL", "mercury-2.5"),
                    "latency_ms": 0,
                    "usage": getattr(decompose_goal, "last_usage", {}) or {},
                    "field": "sub-goal decomposition",
                    "value": None,
                }
            )
            self.trace.emit(
                "text", step=step, purpose="decompose", model=os.environ.get("TEXT_MODEL", "mercury-2.5"),
                latency_ms=0, duration_ms=round((time.perf_counter() - started) * 1000),
                retries=0, filtered=False, valid=True, cached=False,
                field_label=None, value=None,
            )
            self.trace.emit(
                "progress", step=step, subgoals=[item["text"] for item in state["subgoals"]],
                satisfied=[], todo=[item["text"] for item in state["subgoals"]],
                facts_added=[], leave_site=False, complete=False,
            )
        if state.get("subgoals") is None:
            return
        url = state["page"].get("url") or ""
        if url == state.get("judged_url"):
            return
        if step - state.get("judged_step", -9) < 3:
            return
        state["judged_url"] = url
        state["judged_step"] = step
        started = time.perf_counter()
        try:
            judgment, helper = judge_progress(
                track_context(
                    state["goal"], state["subgoals"], state.get("memo") or [], state["page"], state["history"]
                ),
                TRACK_SYSTEM,
            )
        except (ValueError, RuntimeError) as error:
            self.log.info("[jev:track] judgment failed; keeping previous progress")
            self.trace.emit(
                "error", step=step, type=type(error).__name__, message=str(error)[:200],
                where="track", retried=False,
            )
            return
        self.trace.emit(
            "text", step=step, purpose="track", model=helper.get("model"), effort=helper.get("effort"),
            latency_ms=helper.get("latency_ms"), duration_ms=round((time.perf_counter() - started) * 1000),
            retries=helper.get("retries", 0), filtered=helper.get("filtered", False),
            valid=True, cached=False, field_label=None, value=None,
        )
        state["text_calls"].append({**helper, "field": "progress judgment", "value": None})
        had = sum(1 for item in state.get("subgoals") or [] if item.get("satisfied"))
        memo_before = len(state.get("memo") or [])
        apply_judgment(state, judgment, url)
        gained = sum(1 for item in state.get("subgoals") or [] if item.get("satisfied")) > had
        new_facts = [item["fact"] for item in (state.get("memo") or [])[memo_before:]]
        subgoals = state.get("subgoals") or []
        self.trace.emit(
            "progress", step=step,
            subgoals=[item["text"] for item in subgoals],
            satisfied=[
                {"text": item["text"], "value": item.get("value"), "source": item.get("source")}
                for item in subgoals if item.get("satisfied")
            ],
            todo=[item["text"] for item in subgoals if not item.get("satisfied")],
            facts_added=new_facts, leave_site=bool(state.get("leave_site")),
            complete=not any(not item.get("satisfied") for item in subgoals),
        )
        self._maybe_go_back(state, url, gained, new_facts, step)

    def _maybe_go_back(self, state, url, gained, new_facts, step=None):
        """A click that lands nowhere new is a detour: go BACK once and mark it tried."""
        step = len(state.get("history") or []) if step is None else step
        excursion = state.get("excursion")
        if not excursion or url != excursion.get("at"):
            if excursion:
                state["excursion"] = None
            return
        if state.get("page_target") and state.get("page_current") != excursion.get("page"):
            state["excursion"] = None
            return
        # An off-goal detour is a dead end even when the judge claims progress:
        # the page shares no distinctive word with the current sub-goal entity.
        off_goal = bool(current_todo(state)) and not page_entity_match(state["page"], current_todo(state))
        if not off_goal:
            if gained:
                state["excursion"] = None
                return
            todo_words = _score_words(current_todo(state))
            if todo_words and any(todo_words & _score_words(fact) for fact in new_facts):
                state["excursion"] = None
                return
        if (state.get("backs_used") or 0) >= MAX_BACKS:
            state["excursion"] = None
            return
        try:
            state["browser"].go_back()
            state["browser"].wait_until_ready()
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
        except (StalePage, RuntimeError) as error:
            self.log.info(f"[jev:back] could not go back ({error}); continuing")
            self.trace.emit(
                "error", step=step, type=type(error).__name__, message=str(error)[:200],
                where="back", retried=False,
            )
            state["excursion"] = None
            return
        state["backs_used"] = (state.get("backs_used") or 0) + 1
        tried = state.get("tried_links") or []
        norm = normalize_link_url(excursion.get("at"))
        if norm and norm not in tried:
            tried.append(norm)
        state["tried_links"] = tried[-20:]
        state["excursion"] = None
        add_event_note(state, f"Went back; '{excursion.get('label')}' led nowhere new.")
        self.trace.emit(
            "guard", step=step, type="back", label=excursion.get("label"),
            back_from=excursion.get("at"), back_to=state["page"].get("url"),
            backs_used=state["backs_used"], off_goal=off_goal,
            evidence=(
                "off-goal detour: page shares no word with the current sub-goal"
                if off_goal
                else "click landed on a page that advanced no sub-goal and added no on-topic fact"
            ),
        )
        state["judged_url"] = state["page"].get("url") or ""
        state["judged_step"] = len(state.get("history") or [])

    def _handle_captcha(self, state, step, reason):
        """A wall, not a page: block the host, step back, and force a way out. Never solve it."""
        url = (state.get("page") or {}).get("url") or ""
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            host = ""
        block_host(state, host)
        self.trace.emit("guard", step=step, type="captcha", host=host, reason=reason, url=url)
        state["force_navigate"] = True
        try:
            state["browser"].go_back()
            state["browser"].wait_until_ready()
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
        except (StalePage, RuntimeError) as error:
            self.log.info(f"[jev:captcha] could not go back ({error}); forcing navigation instead")
        add_event_note(state, f"Captcha wall on {host or 'this page'}; leaving it alone.")

    def _update_suppression(self, state, step=None):
        subgoals = state.get("subgoals") or []
        todo = any(not item.get("satisfied") for item in subgoals)
        held_nav = any(
            left > 0 and key.split("\u0000", 1)[0] == "navigate"
            for key, left in (state.get("held_actions") or {}).items()
        )
        forced = bool(
            todo
            and state.get("leave_site")
            and state.get("leave_site_url") == state["page"].get("url")
            and not held_nav
        ) or on_blocked_host(state)
        if forced and not state.get("force_navigate"):
            step = len(state.get("history") or []) if step is None else step
            self.trace.emit(
                "guard", step=step, type="force_navigate", todo=current_todo(state)[:160],
                evidence=f"leave_site on {state.get('leave_site_url')}",
            )
        state["force_navigate"] = forced
        state["suppressed_fp"] = state["page"].get("fingerprint")
        state["suppression_reasons"] = suppression_reasons(state, state["page"])
        state["suppressed_keys"] = set(state["suppression_reasons"])
        url_now = state["page"].get("url") or ""
        if state.get("noop_url") != url_now:
            state["noop_url"] = url_now
            state["noop_page"] = {}
            state["noop_streak"] = {}
            state["noop_noted"] = []
        dead = state["noop_page"].setdefault(url_now, [])
        noted = state["noop_noted"]
        for action in state["page"].get("actions") or []:
            # Our own Open twin on an already-expanded field: satisfied, not a choice.
            if (
                action.get("kind") == "click"
                and (action.get("label") or "").startswith("Open ")
                and action.get("expanded") == "true"
                and noop_key(action) not in dead
            ):
                dead.append(noop_key(action))
                if noop_key(action) not in noted:
                    noted.append(noop_key(action))
                    add_event_note(state, f"'{action.get('label')}' is already open; use the controls inside it.")

    def _done_verdict(self, state, page, step=None):
        """Accept DONE only when pagination and every sub-goal check out. Bounded: two
        rejections, then accept as partial so a strict judge cannot trap the run."""
        step = len(state.get("history") or []) if step is None else step
        target, current = state.get("page_target"), state.get("page_current")
        if target and current != target:
            missing = f"page {target} (now on page {current or '?'})"
        else:
            started = time.perf_counter()
            try:
                judgment, helper = judge_progress(
                    gate_context(
                        state["goal"], state.get("subgoals"), state.get("memo") or [], page, state["history"]
                    ),
                    GATE_SYSTEM,
                )
            except (ValueError, RuntimeError) as error:
                self.log.info("[jev:gate] judgment failed; treating DONE as unverified")
                self.trace.emit(
                    "error", step=step, type=type(error).__name__, message=str(error)[:200],
                    where="gate", retried=False,
                )
                judgment, helper = None, None
            else:
                self.trace.emit(
                    "text", step=step, purpose="gate", model=helper.get("model"), effort=helper.get("effort"),
                    latency_ms=helper.get("latency_ms"),
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    retries=helper.get("retries", 0), filtered=helper.get("filtered", False),
                    valid=True, cached=False, field_label=None, value=None,
                )
            if helper is not None:
                state["text_calls"].append({**helper, "field": "done gate", "value": None})
            if judgment is not None:
                apply_judgment(state, judgment, page.get("url") or "")
            if state.get("subgoals") is not None:
                todo = [item["text"] for item in state["subgoals"] if not item.get("satisfied")]
                missing = "; ".join(todo) if todo else ""
            elif judgment is not None and judgment.get("complete"):
                missing = ""
            else:
                missing = (judgment or {}).get("missing") or "unverified completion"
        if not missing:
            return {"accept": True, "partial": False, "missing": ""}
        todo_remain = state.get("subgoals") is not None and any(
            not item.get("satisfied") for item in state["subgoals"]
        )
        if todo_remain and not state.get("winding_down"):
            # Sub-goals remain: no DONE or partial until the wind-down, unless the
            # same verdict repeats and the run would only spin on it.
            streak = state.get("gate_same_missing", 0) + 1 if missing == state.get("gate_missing") else 1
            state["gate_missing"] = missing
            state["gate_same_missing"] = streak
            if streak >= 4:
                return {"accept": True, "partial": True, "missing": missing}
            return {"accept": False, "partial": False, "missing": missing}
        if state.get("done_rejections", 0) >= 2:
            return {"accept": True, "partial": True, "missing": missing}
        return {"accept": False, "partial": False, "missing": missing}

    def _partial_close(self, state):
        """Finish blocked-but-informed: keep collected evidence and facts as a partial answer."""
        if state.get("answer") is not None:
            return
        memo = state.get("memo") or []
        evidence = [item for item in state.get("subgoals") or [] if item.get("satisfied")]
        if not memo and not evidence:
            return
        state["partial"] = True
        state["answer"] = self._final_answer(state, state["page"])

    def _skip_text_step(self, state, page, action, decision, selected, note="text helper failed"):
        """A bad text response skips this step. The run keeps going."""
        self.pending_text = None
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        state["history"].append(
            {
                "step": len(state["history"]) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "choice": selected,
                "probability": decision["probabilities"].get(selected),
                "confidence": decision["confidence"],
                "latency_ms": decision["latency_ms"],
                "text": None,
                "text_helper": None,
                "text_latency_ms": 0,
                "operation": decision["operation"],
                "target": decision["target"],
                "possible_closed_shadow": False,
                "note": "; ".join(part for part in (state.pop("loop_note", None), note) if part),
                "page_changed": False,
                "url": page["url"],
                "usage": decision.get("usage", {}),
                "executed_ms": state["elapsed_ms"],
                "elapsed_ms": state["elapsed_ms"],
            }
        )
        state.setdefault("loop_window", []).append(loop_step(decision, action, page, False))
        state["loop_window"] = state["loop_window"][-LOOP_WINDOW:]
        state["status"] = "ready"
        return self.snapshot()

    def _act_resilient(self, state, step, decision, locator, attempts=2):
        """Execute the decision; on StalePage re-observe and retarget the same
        control without spending a decision. Never holds the stale target:
        it is usually the correct one under a shifted id."""
        page_before = state["page"]
        for attempt in range(attempts + 1):
            try:
                result = self.command("act", {"fingerprint": state["page"]["fingerprint"]})
                self.trace.flush()
                return result
            except StalePage as error:
                self.trace.emit(
                    "error", step=step, type="StalePage", message=str(error)[:300],
                    where="tick", retried=attempt < attempts and locator is not None,
                    stack=traceback.format_exc(limit=5) if self.trace.level >= 2 else None,
                )
                try:
                    started = time.perf_counter()
                    state["page"] = state["browser"].observe(screenshot=self.screenshots)
                    indexer_ms = round((time.perf_counter() - started) * 1000)
                except StalePage:
                    return self._terminal(state, "page never settled", step)
                self._emit_snapshot(state, step, indexer_ms, page_changed=True)
                state["decision"] = None
                state["status"] = "ready"
                match, candidates = retarget_action(state["page"], locator)
                if attempt >= attempts or locator is None or match is None:
                    if locator is None or attempt >= attempts:
                        resolution = "re-decide"
                    elif candidates > 1:
                        resolution = "ambiguous"
                    else:
                        resolution = "missing"
                    self._emit_stale(state, step, locator, page_before, candidates, resolution, attempt)
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    self.trace.flush()
                    return self.snapshot()
                remapped = dict(decision.get("probabilities") or {})
                if locator.get("old_id") in remapped:
                    remapped[match["id"]] = remapped.pop(locator["old_id"])
                else:
                    remapped.setdefault(match["id"], 1.0)
                state["decision"] = {**decision, "choice": match["id"], "probabilities": remapped}
                self._emit_stale(
                    state, step, locator, page_before, candidates, "retargeted", attempt, new_id=match["id"]
                )
                page_before = state["page"]
                decision = state["decision"]
        raise AssertionError("unreachable stale loop")

    def _emit_stale(self, state, step, locator, page_before, candidates, resolution, attempt, new_id=None):
        """A stale event with a field-level diff: the decided target vs the fresh page."""
        before = page_before or {}
        after = state["page"] or {}
        self.trace.emit(
            "stale", step=step, attempt=attempt, resolution=resolution,
            pending=locator or {},
            candidates=candidates,
            new_id=new_id,
            diff={
                "url": [before.get("url"), after.get("url")],
                "title": [before.get("title"), after.get("title")],
                "controls": [len(before.get("actions") or []), len(after.get("actions") or [])],
            },
        )
    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        try:
            trace = getattr(self, "trace", None)
            if trace is not None:
                trace.close()
        finally:
            try:
                self.browser.close()
            finally:
                self.browser.stop_cloud()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
