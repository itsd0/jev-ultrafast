"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import os
import time
from pathlib import Path

from .browser import Browser, StalePage
from .config import resolve_config
from .model import action_space, choose, field_context, field_text, match_option, post_json
from .questions import MAX_STEPS
from .vision import confirm_point, needs_vision, propose


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

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
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
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the demo's model-call budget")
            state["decision"] = self._maybe_vision(state) or choose(state["page"], state["goal"], state["history"])
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
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
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
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
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

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
