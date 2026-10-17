"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import os
import re
import time
from pathlib import Path

from .browser import Browser, StalePage
from .config import resolve_config
from .model import (
    action_space,
    choose,
    choose_mercury,
    decision_provider,
    decompose_goal,
    field_context,
    field_text,
    match_option,
    post_json,
)
from .questions import MAX_STEPS
from .vision import confirm_point, needs_vision, propose

_RELOAD_WORDS = frozenset({"reload", "refresh"})


def reload_like(action):
    """A control whose name says it loads the page again. Not tied to one site."""
    words = set(re.findall(r"[a-z]+", (action.get("label") or "").casefold()))
    return bool(words & _RELOAD_WORDS)


def note_no_progress(holds, action, page_changed):
    """Hide an unchanged action for the next two decisions. A wait stays available."""
    holds = dict(holds or {})
    action_id = action.get("id")
    if page_changed or not action_id or action.get("kind") == "wait":
        return holds
    holds[action_id] = 2
    return holds


def offered_actions(actions, holds):
    held = holds or {}
    return [action for action in actions if held.get(action.get("id"), 0) <= 0]


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
            }
    return None


def hold_actions(holds, action_ids, steps=LOOP_HOLD_STEPS):
    holds = dict(holds or {})
    for action_id in action_ids:
        if action_id:
            holds[action_id] = max(holds.get(action_id, 0), steps)
    return holds


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
    for key in ("pressed", "selected"):
        if action.get(key) == "true" and match.get(key) == "true":
            return "already selected"
    return None


def page_for_choice(state):
    holds = state.get("held_actions") or {}
    if not holds:
        return state["page"]
    return {**state["page"], "actions": offered_actions(state["page"]["actions"], holds)}


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
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
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
            return choose(page, state["goal"], state["history"])
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

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            pending = None
            try:
                self.command("predict", {})
                if state["status"] in {"done", "blocked"}:
                    return self.snapshot()
                pending = (state.get("decision") or {}).get("choice")
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                if pending and pending not in {"DONE", "BLOCKED"}:
                    # This target stopped matching at execution time. Re-picking
                    # it against the same page would loop forever.
                    state["held_actions"] = note_no_progress(
                        state.get("held_actions"), {"id": pending, "kind": None}, False
                    )
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            cycle = detect_cycle(state.get("loop_window") or [])
            if cycle:
                state["held_actions"] = hold_actions(state.get("held_actions"), cycle["ids"])
                state["loop_fires"] = state.get("loop_fires", 0) + 1
                state["loop_note"] = "loop detected: avoid " + "/".join(cycle["labels"])
                if state["loop_fires"] >= LOOP_STOP_FIRES:
                    state["status"] = "blocked"
                    state["blocked_reason"] = "loop detected"
                    state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                    return self.snapshot()
            config = getattr(self, "config", None) or {}
            budget = config.get("max_decisions") or DEFAULT_MAX_DECISIONS
            if len(state["decisions"]) >= budget:
                state["status"] = "blocked"
                state["blocked_reason"] = "decision budget"
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            state["decision"] = self._maybe_vision(state) or self._decide(state, page_for_choice(state))
            state["held_actions"] = decay_holds(state.get("held_actions"))
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
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                if selected == "BLOCKED" and self.blocked_streak < 3:
                    # A loading page looks identical to a dead end. Only trust
                    # BLOCKED once the page has settled; otherwise look again.
                    self.blocked_streak += 1
                    state["browser"].settled(page)
                    state["decision"] = None
                    state["page"] = state["browser"].observe(screenshot=self.screenshots)
                    state["status"] = "ready"
                    return self.snapshot()
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
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
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            unmatched_option = False
            if action["kind"] in {"fill", "select_option"}:
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    try:
                        text, helper = field_text(context)
                    except ValueError:
                        return self._skip_text_step(state, page, action, decision, selected)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
                if action["kind"] == "fill" and action.get("value") == text:
                    # The committed value already equals the goal value. Re-typing
                    # it is a mutation with no effect; mark the step satisfied.
                    return self._skip_already_set(state, page, action, decision, selected, text, helper)
            result = {"executed": action["id"]}
            if action["kind"] == "select_option":
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
            if not unmatched_option:
                self.pending_text = None
                self.blocked_streak = 0
            text_failed = isinstance(result, dict) and result.get("text_verified") is False
            if unmatched_option:
                text_failed = False
            switched = None
            if getattr(type(state["browser"]), "collect_new_tabs", None):
                opened = state["browser"].collect_new_tabs()
                if opened:
                    tab = state["browser"].activate(opened[-1])
                    url = tab.get("url") if isinstance(tab, dict) else ""
                    switched = f"switched to new tab {url or opened[-1]}"
            state["browser"].wait_until_ready()
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
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
                    ) if part) or None,
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            changed = state["page"]["fingerprint"] != page["fingerprint"]
            unchanged = control_unchanged(action, page, state["page"])
            if unchanged:
                base_note = state["history"][-1]["note"]
                state["history"][-1]["note"] = f"{base_note}; {unchanged}" if base_note else unchanged
            if not changed and reload_like(action) and getattr(state["browser"], "wait_for_refresh", None):
                refreshed = state["browser"].wait_for_refresh(
                    page["fingerprint"], timeout=3, screenshot=self.screenshots
                )
                if isinstance(refreshed, dict) and refreshed.get("fingerprint"):
                    state["page"] = refreshed
                    changed = refreshed["fingerprint"] != page["fingerprint"]
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=changed,
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            state["held_actions"] = note_no_progress(
                state.get("held_actions"),
                {"id": selected, "kind": action.get("kind")},
                False if unchanged else changed,
            )
            if action.get("kind") != "wait":
                window = state.get("loop_window") or []
                progress = bool(switched) or not same_page(window, state["page"])
                window.append(loop_step(decision, action, state["page"], progress))
                state["loop_window"] = window[-LOOP_WINDOW:]
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            stalled = len(repeated) == 3 and all(
                h["page_changed"] is False and h["kind"] != "wait" for h in repeated
            )
            state["status"] = "blocked" if stalled else "ready"
            if stalled and any(h.get("possible_closed_shadow") for h in repeated):
                state["blocked_reason"] = "possible closed shadow root"
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
        state["held_actions"] = note_no_progress(
            state.get("held_actions"), {"id": selected, "kind": action.get("kind")}, False
        )
        state.setdefault("loop_window", []).append(loop_step(decision, action, page, True))
        state["loop_window"] = state["loop_window"][-LOOP_WINDOW:]
        state["status"] = "ready"
        return self.snapshot()

    def _skip_text_step(self, state, page, action, decision, selected):
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
                "note": "; ".join(part for part in (state.pop("loop_note", None), "text helper failed") if part),
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

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        try:
            self.browser.close()
        finally:
            self.browser.stop_cloud()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
