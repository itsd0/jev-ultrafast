"""Single-LLM agent loop powered by Reflection Beam.

One structured planner call per step, then ordered tool execution against the
observed page. Classic TypeSafe+Mercury stays available via JEV_AGENT=classic.
"""

import json
import os
import re
import sys
import time
from urllib.parse import quote_plus, urlparse

from .agent import (
    DEFAULT_MAX_DECISIONS,
    add_event_note,
    modal_scroll_route,
    normalize_link_url,
    note_failed_url,
    note_visit,
    page_host,
)
from .beam_state import (
    EXTRACT_SYSTEM,
    JUDGE_SYSTEM,
    SYSTEM_PROMPT,
    LoopMonitor,
    Transcript,
    action_hash_for,
    build_state_message,
    format_tabs,
    render_dom,
    validate_step_structure,
)
from .browser import (
    Browser,
    StalePage,
    browser_operation,
    cdp,
    detect_captcha,
    field_value_expression,
    fingerprint,
    real_upload_path,
    validate_navigate_url,
)
from .config import resolve_config
from .llm.base import ProviderError, RefusalError
from .llm.reflection import ReflectionProvider
from .llm.schema import EXTRACT_SCHEMA, JUDGE_SCHEMA, STEP_SCHEMA, response_format
from .model import (
    match_option,
    redact_secrets,
    token_counts,
    validate_save_filename,
    write_saved_file,
)
from .secrets import substitute, unavailable
from .trace import Trace, cost_estimate_usd, get_logger

STEP_FORMAT = response_format("BeamStep", STEP_SCHEMA)
JUDGE_FORMAT = response_format("BeamJudge", JUDGE_SCHEMA)
EXTRACT_FORMAT = response_format("BeamExtract", EXTRACT_SCHEMA)

PAGE_CHANGERS = {"search", "navigate", "go_back", "switch_tab", "close_tab"}
CONSECUTIVE_FAILURE_CAP = 5
WALL_CAP = 3
DONE_REJECTION_CAP = 3

CHALLENGE_RE = re.compile(
    r"captcha|recaptcha|hcaptcha|turnstile|i.?m not a robot|"
    r"verify (you are|you.?re) (a )?human|prove you.?re human|are you a robot|"
    r"human verification|cloudflare.+challenge",
    re.IGNORECASE,
)
SENSITIVE_FIELD_RE = re.compile(
    r"password|passcode|2fa|two.factor|mfa|otp|one.time|authenticator|"
    r"verification code|security code|pin\b",
    re.IGNORECASE,
)
SECRET_ONLY_RE = re.compile(r"^(?:\s*<secret>[A-Za-z0-9_]{1,64}</secret>\s*)+$")


def _clean(text, limit=200):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _host(url):
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


class BeamAgent:
    """One planner call per step; tools execute against observed elements."""

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
        browser=None,
        background="",
        provider=None,
        helper_provider=None,
    ):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
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
        self._owns_browser = browser is None
        self.browser = browser if browser is not None else Browser(url, config=self.config)
        self.record_dir = record_dir
        self.screenshots = screenshots
        self.log = get_logger("beam")
        self.trace = Trace(run_id=os.environ.get("JEV_RUN_ID") or None)
        self.transcript = Transcript()
        self.loops = LoopMonitor()
        self._by_index = {}
        self._provider_override = provider
        self._helper_override = helper_provider
        started = time.perf_counter()
        page = self.browser.observe(screenshot=screenshots)
        page["fingerprint"] = fingerprint(page)
        self.state = {
            "browser": self.browser,
            "goal": task,
            "background": background or "",
            "history": [],
            "decisions": [],
            "text_calls": [],
            "page": page,
            "status": "ready",
            "started_at": started,
            "plan": "",
            "memo": [],
            "visited_urls": [],
            "failed_urls": [],
            "wall_hosts": [],
            "llm_steps": 0,
            "failures": 0,
            "done_rejections": 0,
            "wall_count": 0,
            "prev_keys": [],
            "event_notes": [],
            "answer": None,
            "blocked_reason": None,
            "partial": 0,
        }
        note_visit(self.state, page.get("url"))
        self.trace.emit(
            "snapshot", step=1, url=page.get("url"), title=page.get("title"),
            controls=len(page.get("actions") or []),
            fingerprint=(page.get("fingerprint") or "")[:16], page_changed=True,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )
        self.trace.flush()

    # -- framework surface (mirrors Agent) ---------------------------------

    def command(self, name, body=None):
        if name == "tick":
            return self._tick(self.state)
        raise ValueError(f"Unknown beam command {name!r}")

    def snapshot(self):
        return {k: v for k, v in self.state.items() if k != "browser"}

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self, keep_browser=False):
        try:
            trace = getattr(self, "trace", None)
            if trace is not None:
                trace.close()
        finally:
            if keep_browser or not getattr(self, "_owns_browser", True):
                pass
            else:
                try:
                    self.browser.close()
                finally:
                    self.browser.stop_cloud()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    # -- provider -----------------------------------------------------------

    def _provider(self):
        if self._provider_override is not None:
            return self._provider_override
        config = getattr(self, "config", None) or {}
        model = config.get("model") or None
        effort = config.get("effort") or None
        self._provider_override = ReflectionProvider(model=model, effort=effort or None)
        return self._provider_override

    def _helpers(self):
        if self._helper_override is None:
            main = self._provider()
            self._helper_override = ReflectionProvider(model=main.model, effort="low")
        return self._helper_override

    def _budget(self):
        return (getattr(self, "config", None) or {}).get("max_decisions") or DEFAULT_MAX_DECISIONS

    # -- tick ---------------------------------------------------------------

    def _tick(self, state):
        if state["status"] in {"done", "blocked"}:
            return self.snapshot()
        budget = self._budget()
        used = state["llm_steps"]
        if used >= budget:
            return self._finish_blocked(state, used + 1, "hit budget")
        observe_started = time.perf_counter()
        try:
            page = state["browser"].observe(screenshot=self.screenshots)
        except Exception as error:
            return self._terminal(state, f"env:{type(error).__name__}", used + 1)
        indexer_ms = round((time.perf_counter() - observe_started) * 1000)
        page_changed = page.get("fingerprint") != (state.get("page") or {}).get("fingerprint")
        page["fingerprint"] = fingerprint(page)
        state["page"] = page
        note_visit(state, page.get("url"))
        step_no = used + 1
        self.trace.emit(
            "snapshot", step=step_no, url=page.get("url"), title=page.get("title"),
            controls=len(page.get("actions") or []),
            modal_scoped=page.get("modal_scoped"), overlay=page.get("overlay"),
            fingerprint=(page.get("fingerprint") or "")[:16],
            page_changed=bool(page_changed), duration_ms=indexer_ms,
        )
        notes = [note.get("text") for note in state.get("event_notes") or [] if note.get("text")]
        state["event_notes"] = []
        captcha = detect_captcha(page)
        if captcha and page.get("url") != state.get("_wall_url"):
            handled = self._handle_wall(state, page, captcha, step_no)
            if handled.get("stopped"):
                return self.snapshot()
            notes.append(handled["note"])
            page = state["page"]
            if handled.get("restart_pending"):
                self.trace.flush()
                return self.snapshot()
        elif captcha:
            notes.append(
                "This page is still walled. Clicking and typing here are disabled: "
                "navigate to another route or stop with success=false."
            )
        render = render_dom(page, state.get("prev_keys") or [])
        state["prev_keys"] = sorted(
            [tuple(key) for key in render["new_keys"]], key=repr,
        )
        self._by_index = render["by_index"]
        self.trace.emit(
            "offered", step=step_no, offered_count=len(render["by_index"]),
            rows=render["rows"], dom_chars=len(render["text"]),
        )
        remaining = budget - used
        if not state.get("winding_down") and used >= budget * 0.75:
            state["winding_down"] = True
            self.trace.emit("guard", step=step_no, type="wind_down", used=used, budget=budget)
        if state.get("winding_down"):
            notes.append(
                f"Final phase ({remaining} steps left): verify and finish with "
                "verified facts; say what is missing instead of exploring."
            )
        last_step = remaining == 1
        if last_step:
            notes.append(
                "LAST STEP: output done alone with the collected answer, or "
                "success=false saying what is missing."
            )
        tabs = self._tabs(state)
        state_message = build_state_message(
            goal=state["goal"], background=state.get("background") or "",
            step_no=step_no, budget=budget, plan=state.get("plan") or "",
            tabs_text=format_tabs(tabs), page_url=page.get("url") or "",
            page_title=page.get("title") or "", dom_text=render["text"],
            page_text=self._page_text(page),
            history_text=self.transcript.recent_history_text(),
            notes=[note for note in notes if note],
        )
        messages = self.transcript.messages(SYSTEM_PROMPT, state_message)
        provider = self._provider()
        try:
            result = provider.complete(
                messages, response_format=STEP_FORMAT, purpose="step",
                step=step_no, trace=self.trace,
            )
        except RefusalError as error:
            return self._finish_blocked(state, step_no, f"model refused: {error}")
        except ProviderError as error:
            return self._model_failure(state, step_no, str(error), usage={})
        state["llm_steps"] = used + 1
        record = {
            "model": result.model, "usage": result.usage,
            "latency_ms": result.latency_ms, "retries": result.retries,
            "effort": result.effort, "request_id": result.request_id,
            "finish_reason": result.finish_reason,
        }
        state["decisions"].append(record)
        output, error = validate_step_structure(result.output)
        if error is not None:
            self.trace.emit("guard", step=step_no, type="invalid_output", reason=error)
            return self._register_failure(
                state, step_no,
                f"Invalid output ({error}). Fix the format and retry; no action executed.",
            )
        state["failures"] = 0
        if (output.get("plan_update") or "").strip():
            state["plan"] = output["plan_update"].strip()[:2000]
        if output["actions"][0]["name"] == "done":
            return self._finish_done(state, page, output, result, step_no, last_step)
        if last_step:
            return self._forced_done(state, step_no, "last step produced no done")
        return self._execute_step(state, page, output, result, step_no, tabs)

    # -- walls -----------------------------------------------------------------

    def _handle_wall(self, state, page, captcha, step_no):
        url = page.get("url") or ""
        host = page_host(url) or _host(url)
        entry = {
            "step": len(state["history"]) + 1, "action": f"Blocked by a CAPTCHA on {host or 'this page'}",
            "kind": "captcha", "operation": "CAPTCHA", "url": url, "elapsed_ms": 0,
            "note": _clean(captcha, 160),
        }
        state["history"].append(entry)
        if host and host not in (state.get("wall_hosts") or []):
            state["wall_hosts"] = list(state.get("wall_hosts") or []) + [host]
        note_failed_url(state, url)
        state["wall_count"] = state.get("wall_count", 0) + 1
        state["_wall_url"] = url
        self.trace.emit("guard", step=step_no, type="captcha", host=host, reason=_clean(captcha, 160))
        browser = state["browser"]
        cloud_mode = (getattr(browser, "config", None) or {}).get("browser") == "cloud"
        if cloud_mode and host and not state.get("cloud_restarted"):
            state["cloud_restart_wanted"] = host
        if state.get("wall_count", 0) >= WALL_CAP:
            hosts = ", ".join((state.get("wall_hosts") or [])[-3:])
            self._finish_blocked(
                state, step_no,
                f"Stopped after repeated CAPTCHA walls ({hosts}); use Take over to continue manually",
            )
            return {"stopped": True, "note": ""}
        try:
            browser.go_back()
            browser.wait_until_ready()
            fresh_page = browser.observe(screenshot=self.screenshots)
            fresh_page["fingerprint"] = fingerprint(fresh_page)
            state["page"] = fresh_page
            note_visit(state, fresh_page.get("url"))
        except Exception as error:
            self.trace.emit(
                "error", step=step_no, type=type(error).__name__,
                where="wall-back", message=str(error)[:200], retried=False,
            )
        norm = normalize_link_url(url)
        note = (
            f"CAPTCHA/traffic wall on {host or 'this page'} ({norm or url}): do not "
            "navigate there again; try another route or search engine, else stop "
            "with success=false. Clicking and typing on the walled page are disabled."
        )
        return {"stopped": False, "note": note, "restart_pending": bool(state.get("cloud_restart_wanted"))}

    # -- failures -----------------------------------------------------------

    def _model_failure(self, state, step_no, message, usage):
        state["llm_steps"] = state.get("llm_steps", 0) + 1
        state["decisions"].append({"model": self._provider().model, "usage": usage, "error": message[:200]})
        self.trace.emit("guard", step=step_no, type="model_error", reason=message[:200])
        return self._register_failure(state, step_no, f"Model call failed ({message[:160]}). Retry simply.")

    def _register_failure(self, state, step_no, note):
        state["failures"] = state.get("failures", 0) + 1
        if state["failures"] >= CONSECUTIVE_FAILURE_CAP:
            return self._finish_blocked(
                state, step_no,
                f"Stopped after {CONSECUTIVE_FAILURE_CAP} consecutive failures ({note[:160]})",
            )
        add_event_note(state, note)
        self.trace.flush()
        return self.snapshot()

    def _forced_done(self, state, step_no, reason):
        self.trace.emit("guard", step=step_no, type="forced_done", reason=reason)
        facts = [fact.get("fact") for fact in state.get("memo") or [] if fact.get("fact")]
        text = "; ".join(facts[:5]) if facts else "I ran out of steps before finishing."
        return self._finish_blocked(state, step_no, f"{reason}; partial: {text}"[:500])

    def _finish_blocked(self, state, step_no, reason):
        state["status"] = "blocked"
        state["blocked_reason"] = reason
        if state.get("answer") is None:
            facts = [fact.get("fact") for fact in state.get("memo") or [] if fact.get("fact")]
            state["answer"] = "; ".join(facts[:5]) if facts else None
            state["partial"] = 1 if state.get("answer") else 0
        state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
        self._emit_end(state, "blocked")
        self.trace.save_failure(step_no, self._failshot(state))
        self.trace.flush()
        return self.snapshot()

    @staticmethod
    def _failshot(state):
        """A failure screenshot, captured on demand. Screenshots stay off otherwise."""
        try:
            shot = state["browser"].call("Page.captureScreenshot", format="jpeg", quality=60)
        except Exception:
            return None
        return shot.get("data") if isinstance(shot, dict) else None

    def _terminal(self, state, reason, step_no):
        return self._finish_blocked(state, step_no, reason)

    def _emit_end(self, state, status):
        prompt = completion = 0
        for item in state.get("decisions") or []:
            pin, pout = token_counts(item.get("usage"))
            prompt += pin
            completion += pout
        for item in state.get("text_calls") or []:
            pin, pout = token_counts(item.get("usage"))
            prompt += pin
            completion += pout
        self.trace.emit(
            "end", step=len(state.get("history") or []), status=status,
            blocked_reason=state.get("blocked_reason"),
            decisions=len(state.get("decisions") or []),
            prompt_tokens=prompt, completion_tokens=completion,
            cost_usd_estimate=cost_estimate_usd(prompt, completion),
            wall_ms=round((time.perf_counter() - state["started_at"]) * 1000),
        )

    # -- done + judge -------------------------------------------------------

    def _judge_context(self, state, page, text):
        facts = [fact.get("fact") for fact in state.get("memo") or [] if fact.get("fact")]
        recent = self.transcript.recent_history_text(limit=2)
        memories = [
            turn.get("memory") for turn in self.transcript.turns[-3:]
            if turn.get("memory")
        ]
        return {
            "goal": state["goal"],
            "claimed_answer": text,
            "final_url": page.get("url"),
            "final_title": page.get("title"),
            "verified_facts": facts[-10:],
            "recent_results": recent[-2000:],
            # The judge verifies quotes against the pages the agent saw: its
            # recorded per-step memory plus the final page text.
            "agent_memory": memories,
            "compacted_verified": self.transcript.summary[-2000:],
            "final_page_text": self._page_text(page)[:8000],
            # Tab state lets the judge verify "in a new tab" style goals.
            "open_tabs": format_tabs(self._tabs(state)),
        }

    def _run_judge(self, state, page, text, step_no):
        try:
            result = self._helpers().complete(
                [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": json.dumps(self._judge_context(state, page, text))},
                ],
                response_format=JUDGE_FORMAT, purpose="judge", step=step_no, trace=self.trace,
                max_completion_tokens=800,
            )
        except (ProviderError, RefusalError) as error:
            self.trace.emit(
                "text", step=step_no, purpose="judge", model=self._helpers().model,
                effort="low", latency_ms=0, duration_ms=0, retries=0,
                filtered=False, valid=False, error=str(error)[:160],
            )
            return None
        state["text_calls"].append({
            "model": result.model, "usage": result.usage, "latency_ms": result.latency_ms,
            "effort": "low", "field": "done judge",
        })
        output = result.output if isinstance(result.output, dict) else {}
        if not isinstance(output.get("complete"), bool) or not isinstance(output.get("missing"), list):
            return {"complete": False, "missing": ["judge output was invalid"]}
        return {"complete": output["complete"], "missing": [str(item)[:200] for item in output["missing"]]}

    def _finish_done(self, state, page, output, result, step_no, last_step):
        action = output["actions"][0]
        text = (action.get("text") or "").strip()
        success = bool(action.get("success"))
        if not text:
            self.trace.emit("guard", step=step_no, type="invalid_output", reason="done needs text")
            return self._register_failure(state, step_no, "Invalid done (empty text). Say what happened.")
        summary = [{"name": "done", "target": text[:80], "ok": True, "note": ""}]
        if not success:
            self._record(
                state, output, result, step_no, summary,
                {"operation": "DONE", "label": text[:120], "ok": True, "note": ""},
            )
            state["status"] = "blocked"
            state["answer"] = text
            state["partial"] = 1
            state["blocked_reason"] = f"gave up: {text[:200]}"
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            self._emit_end(state, "blocked")
            self.trace.flush()
            return self.snapshot()
        verdict = self._run_judge(state, page, text, step_no)
        if verdict is None:
            return self._register_failure(
                state, step_no, "Verification was unavailable; re-check the page and retry done.",
            )
        if verdict["complete"]:
            self._record(
                state, output, result, step_no, summary,
                {"operation": "DONE", "label": text[:120], "ok": True, "note": ""},
            )
            state["status"] = "done"
            state["answer"] = text
            state["partial"] = 0
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            self._emit_end(state, "done")
            self.trace.flush()
            self.transcript.add_turn(
                step_no, "", json.dumps(result.output, sort_keys=True),
                result.reasoning_content, next_goal="done",
                results=[{"label": "done accepted", "ok": True}],
                verified_facts=[text[:300]], memory=output.get("memory") or "",
            )
            return self.snapshot()
        state["done_rejections"] = state.get("done_rejections", 0) + 1
        missing = "; ".join(verdict["missing"][:5]) or "unverified claims"
        if state["done_rejections"] >= DONE_REJECTION_CAP or last_step:
            self._record(
                state, output, result, step_no, summary,
                {"operation": "DONE", "label": text[:120], "ok": True, "note": ""},
            )
            state["status"] = "blocked"
            state["answer"] = text
            state["partial"] = 1
            state["blocked_reason"] = f"gave up: done rejected ({missing[:200]})"
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            self._emit_end(state, "blocked")
            self.trace.flush()
            return self.snapshot()
        self.trace.emit("guard", step=step_no, type="done_rejected", missing=missing[:300])
        add_event_note(state, f"Done was rejected: {missing[:300]}. Re-verify from the page.")
        self.transcript.add_turn(
            step_no, "", json.dumps(result.output, sort_keys=True),
            result.reasoning_content, next_goal=output.get("next_goal") or "",
            results=[{"label": "done rejected", "ok": False, "note": missing[:200]}],
            memory=output.get("memory") or "",
        )
        self.trace.flush()
        return self.snapshot()

    # -- execution ----------------------------------------------------------

    def _page_text(self, page):
        parts = [page.get("text") or ""]
        for section in page.get("sections") or []:
            heading = section.get("heading") or ""
            body = section.get("text") or ""
            parts.append(f"{heading}\n{body}" if heading else body)
        return "\n\n".join(part for part in parts if part)

    def _current_href(self, state):
        try:
            value = state["browser"].call(
                "Runtime.evaluate",
                expression="location.href",
                returnByValue=True,
            )
        except Exception:
            return None
        try:
            return (value.get("result") or {}).get("value") or None
        except AttributeError:
            return None

    def _tabs(self, state):
        browser = state.get("browser")
        infos = getattr(browser, "tab_infos", None)
        if callable(infos):
            try:
                return infos() or []
            except Exception:
                pass
        try:
            tabs = getattr(browser, "tabs", None) or {}
            active = getattr(browser, "active", None)
            return [
                {
                    "key": key, "url": tab.get("url") or "",
                    "title": tab.get("title") or "", "active": key == active,
                }
                for key, tab in tabs.items()
            ]
        except Exception:
            return []

    def _execute_step(self, state, page, output, result, step_no, tabs):
        start_active = next((tab.get("key") for tab in tabs if tab.get("active")), None)
        start_href = page.get("url")
        results, verified, actions_summary = [], [], []
        stop = None
        for action in output["actions"]:
            prepared, error = self._prepare_action(state, page, action)
            if error is not None:
                results.append({"label": f"{action.get('name')} (invalid)", "ok": False, "note": error})
                actions_summary.append({"name": action.get("name"), "target": "", "ok": False, "note": error})
                stop = "invalid action; re-plan with the fresh tree"
                break
            outcome = self._execute_action(state, page, prepared)
            results.append({"label": outcome["label"], "ok": outcome["ok"], "note": outcome.get("note", "")})
            actions_summary.append({
                "name": action.get("name"), "target": outcome.get("target", ""),
                "ok": outcome["ok"], "note": outcome.get("note", ""),
            })
            if outcome.get("verified_fact"):
                verified.append(outcome["verified_fact"][:300])
            if outcome.get("memo_fact"):
                state["memo"] = list(state.get("memo") or []) + [outcome["memo_fact"]]
            self._record(state, output, result, step_no, actions_summary, {
                "operation": outcome["operation"], "label": outcome["label"],
                "ok": outcome["ok"], "note": outcome.get("note", ""),
                "url": outcome.get("url") or page.get("url"),
                "elapsed_ms": outcome.get("elapsed_ms", 0),
                "kind": outcome.get("kind", ""),
            })
            if not outcome["ok"]:
                if action.get("name") == "navigate" and action.get("url"):
                    note_failed_url(state, action["url"])
                stop = "action failed; re-plan"
                break
            if action.get("name") in PAGE_CHANGERS:
                stop = "page context changed"
                break
            href = self._current_href(state) or page.get("url")
            active = self._active_key(state)
            opened = self._collect_tabs(state)
            if opened:
                stop = "new tab opened"
                break
            if href != start_href or (start_active and active and active != start_active):
                stop = "page changed"
                break
        nudges = self._note_loops(state, actions_summary, step_no)
        for nudge in nudges:
            add_event_note(state, nudge)
        self.transcript.add_turn(
            step_no, "", json.dumps(result.output, sort_keys=True),
            result.reasoning_content, next_goal=output.get("next_goal") or "",
            results=results, verified_facts=verified, memory=output.get("memory") or "",
        )
        if any(item.get("ok") for item in actions_summary):
            state["failures"] = 0
        else:
            first_note = results[0].get("note") if results else ""
            return self._register_failure(state, step_no, first_note or "no action ran")
        if stop:
            add_event_note(state, f"Sequence stopped: {stop}.")
        self.trace.flush()
        return self.snapshot()

    def _active_key(self, state):
        for tab in self._tabs(state):
            if tab.get("active"):
                return tab.get("key")
        return None

    def _collect_tabs(self, state):
        browser = state["browser"]
        collect = getattr(type(browser), "collect_new_tabs", None)
        if collect is None:
            return []
        try:
            opened = browser.collect_new_tabs()
        except Exception:
            return []
        if not opened:
            return []
        try:
            tab = browser.activate(opened[-1])
            url = tab.get("url") if isinstance(tab, dict) else ""
        except Exception:
            url = ""
        state["history"].append({
            "step": len(state["history"]) + 1, "action": f"switched to new tab {url or opened[-1]}",
            "kind": "switch_tab", "operation": "SWITCH_TAB", "url": url or "",
            "elapsed_ms": 0, "note": "",
        })
        return opened

    def _note_loops(self, state, actions_summary, step_no):
        host = _host((state.get("page") or {}).get("url"))
        fingerprint_now = (state.get("page") or {}).get("fingerprint") or ""
        nudges = []
        for item in actions_summary:
            for nudge in self.loops.add(
                action_hash_for(item["name"], item.get("target", ""), host), fingerprint_now,
            ):
                nudges.append(nudge)
        if nudges:
            self.trace.emit("guard", step=step_no, type="loop_nudge", nudges=nudges)
        return nudges

    def _record(self, state, output, result, step_no, actions_summary, entry):
        detail = {
            "step": step_no,
            "thinking_ms": result.latency_ms,
            "evaluation": output.get("evaluation_previous_goal") or "",
            "next_goal": output.get("next_goal") or "",
            "memory": output.get("memory") or "",
            "model": result.model,
            "effort": result.effort,
            "actions": actions_summary or [{
                "name": "done", "target": "", "ok": entry.get("ok", True), "note": "",
            }],
        }
        record = {
            "step": len(state["history"]) + 1,
            "action": entry.get("label", ""),
            "kind": entry.get("kind", ""),
            "operation": entry.get("operation", ""),
            "url": entry.get("url", ""),
            "elapsed_ms": entry.get("elapsed_ms", 0),
            "note": redact_secrets(entry.get("note", "")),
            "detail": detail,
        }
        state["history"].append(record)
        self.trace.emit(
            "action", step=step_no, operation=record["operation"], target=record["action"][:120],
            duration_ms=record["elapsed_ms"], ok=bool(entry.get("ok", True)), note=record["note"][:200],
        )
        return record

    # -- preparation (validation only; never mutates) -----------------------

    def _row(self, index):
        if type(index) is not int or index < 1:
            return None
        return self._by_index.get(index)

    def _element(self, index, kinds):
        row = self._row(index)
        if row is None or row.get("row") != "element":
            return None, f"index {index} is not an element in this step's tree"
        action = row.get("action") or {}
        if action.get("kind") not in kinds:
            return None, f"index {index} is {action.get('kind')}, not usable here"
        return action, None

    def _search_template(self, engine):
        engines = (getattr(self, "config", None) or {}).get("search_engines") or []
        want = (engine or "").strip().lower()
        if want:
            for template in engines:
                if want in _host(template):
                    return template, want
        for template in engines:
            if "duckduckgo" in _host(template):
                return template, "duckduckgo"
        if engines:
            return engines[0], _host(engines[0]) or "default"
        return "", ""

    def _files_dir(self, state):
        return (getattr(state.get("browser"), "config", None) or {}).get("files_dir")

    def _prepare_action(self, state, page, action):
        """Validate one action against the live index map. Returns (prepared, error)."""
        name = action.get("name")
        walled = bool(detect_captcha(page))
        if walled and name in {"click", "input", "send_keys", "select_dropdown", "upload_file"}:
            return None, "this page is walled; navigate to another route or stop with success=false"
        if name == "search":
            if not (action.get("text") or "").strip():
                return None, "search needs text"
            template, engine = self._search_template(action.get("engine"))
            if "{q}" not in template:
                return None, "no search template is configured"
            return {"name": name, "url": template.replace("{q}", quote_plus(action["text"].strip())),
                    "engine": engine, "query": action["text"].strip()}, None
        if name == "navigate":
            url = (action.get("url") or "").strip()
            if not url:
                return None, "navigate needs a URL"
            try:
                validate_navigate_url(url)
            except ValueError as error:
                return None, str(error)[:200]
            if normalize_link_url(url) in (state.get("failed_urls") or []):
                return None, "that URL already failed; try another route"
            if action.get("new_tab"):
                try:
                    tabs = self._tabs(state)
                except Exception:
                    tabs = []
                if len(tabs) >= 5:
                    return None, "the five-tab cap is reached; close a tab first"
            return {"name": name, "url": url, "new_tab": bool(action.get("new_tab"))}, None
        if name in {"go_back", "close_tab", "wait"}:
            if name == "wait":
                amount = action.get("amount")
                seconds = 0.1 if amount == -1 else amount
                if not isinstance(seconds, (int, float)) or not 0 <= seconds <= 30:
                    return None, "wait needs 0-30 seconds"
                return {"name": name, "seconds": min(float(seconds), 10)}, None
            if name == "close_tab":
                try:
                    tabs = self._tabs(state)
                except Exception:
                    tabs = []
                if len(tabs) <= 1 and tabs:
                    return None, "refusing to close the last tab"
            return {"name": name}, None
        if name == "switch_tab":
            tab = (action.get("tab") or "").strip()
            if not re.fullmatch(r"t[1-5]", tab):
                return None, "switch_tab needs a tab id like t2"
            return {"name": name, "tab": tab}, None
        if name == "click":
            element, error = self._element(action.get("index"), {"click"})
            if error:
                return None, error
            if CHALLENGE_RE.search(element.get("label") or ""):
                return None, "challenge controls are never clicked; leave the page or stop"
            return {"name": name, "element": element}, None
        if name == "input":
            element, error = self._element(action.get("index"), {"fill"})
            if error:
                return None, error
            text = action.get("text") or ""
            if not text and not action.get("clear"):
                return None, "input needs text or clear=true"
            if SENSITIVE_FIELD_RE.search(element.get("label") or ""):
                if not text or not SECRET_ONLY_RE.match(text):
                    return None, "sensitive fields only accept a <secret>NAME</secret> placeholder"
            missing = unavailable(text, page.get("url"))
            if missing:
                return None, f"secret {missing[0]} is not available on this host"
            return {"name": name, "element": element, "text": text, "clear": bool(action.get("clear"))}, None
        if name == "send_keys":
            if not action.get("keys"):
                return None, "send_keys needs at least one key"
            return {"name": name, "keys": list(action["keys"])}, None
        if name in {"select_dropdown", "dropdown_options"}:
            element, error = self._element(action.get("index"), {"select"})
            if error:
                return None, error
            if name == "select_dropdown" and not (action.get("option") or "").strip():
                return None, "select_dropdown needs an option label"
            return {"name": name, "element": element, "option": (action.get("option") or "").strip()}, None
        if name == "scroll":
            direction = action.get("direction")
            amount = action.get("amount")
            pages = 1 if amount == -1 else amount
            if not isinstance(pages, (int, float)) or not 1 <= pages <= 5:
                return None, "scroll needs 1-5 pages"
            index = action.get("index")
            if index == -1:
                if direction not in {"up", "down"}:
                    return None, "page scroll supports up/down only"
                want_down = direction == "down"
                for item in page.get("actions") or []:
                    if item.get("kind") != "scroll" or type(item.get("node")) is int:
                        continue
                    if ((item.get("delta") or 0) > 0) == want_down:
                        routed, note = modal_scroll_route(page, item)
                        return {"name": name, "scroll": routed, "pages": int(pages),
                                "direction": direction, "modal_note": note}, None
                return None, f"the page cannot scroll {direction}"
            row = self._row(index)
            if row is None or row.get("row") != "scroll":
                return None, f"index {index} is not a scroll container in this step's tree"
            if direction not in (row.get("dirs") or {}):
                return None, f"that container cannot scroll {direction}"
            return {"name": name, "scroll": row["dirs"][direction], "pages": int(pages),
                    "direction": direction, "modal_note": None}, None
        if name in {"find_text", "search_page"}:
            pattern = action.get("pattern") or ""
            if not pattern or len(pattern) > 200:
                return None, "pattern must hold 1-200 characters"
            if name == "search_page":
                try:
                    re.compile(pattern)
                except re.error as error:
                    return None, f"invalid regex: {error}"[:200]
            return {"name": name, "pattern": pattern}, None
        if name == "extract":
            if not (action.get("text") or "").strip():
                return None, "extract needs a query"
            return {"name": name, "query": action["text"].strip()}, None
        if name == "upload_file":
            element, error = self._element(action.get("index"), {"upload"})
            if error:
                return None, error
            key = (action.get("key") or "").strip()
            if not key:
                return None, "upload_file needs an allowlisted key"
            files = (getattr(self, "config", None) or {}).get("files") or {}
            try:
                path = real_upload_path(files, key)
            except ValueError as error:
                return None, str(error)[:200]
            return {"name": name, "element": element, "path": path, "key": key}, None
        if name == "save_file":
            filename = (action.get("filename") or "").strip()
            content = action.get("content") or ""
            if not filename or not content:
                return None, "save_file needs a filename and content"
            try:
                validate_save_filename(filename)
            except ValueError as error:
                return None, str(error)[:200]
            if not self._files_dir(state):
                return None, "no session files directory is configured"
            return {"name": name, "filename": filename, "content": content}, None
        if name == "read_file":
            filename = (action.get("filename") or "").strip()
            if not filename:
                return None, "read_file needs a filename"
            try:
                validate_save_filename(filename)
            except ValueError as error:
                return None, str(error)[:200]
            files_dir = self._files_dir(state)
            if not files_dir:
                return None, "no session files directory is configured"
            path = os.path.realpath(os.path.join(files_dir, filename))
            if os.path.commonpath([path, os.path.realpath(files_dir)]) != os.path.realpath(files_dir):
                return None, "that file is outside the session directory"
            if not os.path.isfile(path):
                return None, "that file does not exist in this session"
            return {"name": name, "filename": filename, "path": path}, None
        return None, f"unknown action {name!r}"

    # -- execution (each action runs at most once; failures never retry) ----

    def _execute_action(self, state, page, prepared):
        name = prepared["name"]
        started = time.perf_counter()
        try:
            outcome = getattr(self, f"_do_{name}")(state, page, prepared)
        except StalePage:
            outcome = {"ok": False, "note": "the page changed mid-action; re-plan with the fresh tree"}
        except Exception as error:
            outcome = {"ok": False, "note": f"{type(error).__name__}: {error}"[:200]}
        outcome.setdefault("label", name)
        outcome.setdefault("operation", name.upper())
        outcome.setdefault("kind", "")
        outcome.setdefault("target", "")
        outcome["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        outcome["note"] = redact_secrets(outcome.get("note", ""))
        try:
            outcome["url"] = self._current_href(state) or page.get("url")
        except Exception:
            outcome["url"] = page.get("url")
        return outcome

    def _do_search(self, state, page, prepared):
        return self._do_navigate(state, page, prepared, operation="SEARCH")

    def _do_navigate(self, state, page, prepared, operation="NAVIGATE"):
        browser = state["browser"]
        if prepared.get("new_tab"):
            browser.new_tab()
        browser.navigate(prepared["url"])
        browser.wait_until_ready()
        href = self._current_href(state) or prepared["url"]
        note_visit(state, href)
        label = prepared.get("query") or prepared["url"]
        return {"ok": True, "operation": operation, "kind": "navigate",
                "label": f"{operation.title()} {label}"[:160], "target": _host(prepared["url"]),
                "note": f"engine={prepared['engine']}" if operation == "SEARCH" else ""}

    def _do_go_back(self, state, page, prepared):
        browser = state["browser"]
        browser.go_back()
        browser.wait_until_ready()
        href = self._current_href(state) or page.get("url")
        note_visit(state, href)
        return {"ok": True, "operation": "GO_BACK", "kind": "back",
                "label": "Went back", "target": _host(href),
                "note": "" if href != page.get("url") else "back did not change the page"}

    def _do_switch_tab(self, state, page, prepared):
        state["browser"].activate(prepared["tab"])
        href = self._current_href(state) or ""
        note_visit(state, href)
        return {"ok": True, "operation": "SWITCH_TAB", "kind": "switch_tab",
                "label": f"Switched to {prepared['tab']}", "target": prepared["tab"], "note": ""}

    def _do_close_tab(self, state, page, prepared):
        state["browser"].close_active()
        return {"ok": True, "operation": "CLOSE_TAB", "kind": "close_tab",
                "label": "Closed the tab", "target": "", "note": ""}

    def _do_click(self, state, page, prepared):
        result = state["browser"].act(prepared["element"], page)
        label = prepared["element"].get("label") or prepared["element"].get("id")
        executed = result.get("executed") if isinstance(result, dict) else None
        return {"ok": True, "operation": "CLICK", "kind": "click",
                "label": f"Clicked '{_clean(label, 80)}'", "target": self._target_desc(prepared["element"]),
                "note": "" if executed else "click dispatched"}

    def _do_input(self, state, page, prepared):
        element, label = prepared["element"], prepared["element"].get("label") or "field"
        if not prepared["text"] and prepared["clear"]:
            value = self._clear_field(state, page, element)
            verified = value == ""
            self.trace.emit("verify", step=len(state.get("history") or []) + 1,
                            control=_clean(label, 80), verified=verified)
            return {"ok": verified, "operation": "TYPE_TEXT", "kind": "fill",
                    "label": f"Cleared '{_clean(label, 80)}'",
                    "target": self._target_desc(element),
                    "note": "" if verified else "the field kept its value"}
        value, error = substitute(prepared["text"], page.get("url"))
        if error is not None:
            return {"ok": False, "operation": "TYPE_TEXT", "kind": "fill",
                    "label": f"Typed into '{_clean(label, 80)}'",
                    "target": self._target_desc(element), "note": error}
        result = state["browser"].act(element, page, text=value)
        verified = not isinstance(result, dict) or result.get("text_verified", True) is not False
        self.trace.emit("verify", step=len(state.get("history") or []) + 1,
                        control=_clean(label, 80), verified=bool(verified))
        shown = _clean(prepared["text"], 80)
        return {"ok": bool(verified), "operation": "TYPE_TEXT", "kind": "fill",
                "label": f"Typed into '{_clean(label, 80)}'",
                "target": self._target_desc(element),
                "note": "" if verified else f"the page did not keep {shown!r}"}

    def _clear_field(self, state, page, element):
        browser = state["browser"]
        focus = {"id": f"{element.get('id')}:focus", "kind": "click",
                 "node": element.get("node"), "rect": element.get("rect")}
        if element.get("route"):
            focus["route"] = element["route"]
        browser.act(focus, page)
        session = browser.session
        if element.get("route"):
            session = (browser.frame_sessions or {}).get(element["route"]) or session

        def call(method, **params):
            return cdp(method, session_id=session, **params)

        modifier = 4 if sys.platform == "darwin" else 2
        call("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA",
             modifiers=modifier, commands=["selectAll"])
        call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=modifier)
        call("Input.dispatchKeyEvent", type="keyDown", key="Backspace", code="Backspace",
             windowsVirtualKeyCode=8)
        call("Input.dispatchKeyEvent", type="keyUp", key="Backspace", code="Backspace",
             windowsVirtualKeyCode=8)
        try:
            return browser.evaluate(field_value_expression(element), session) or ""
        except Exception:
            return ""

    def _do_send_keys(self, state, page, prepared):
        browser = state["browser"]
        key_action = {"id": "beam-keys", "kind": "key"}
        for key in prepared["keys"]:
            browser_operation({"operation": "key", "session": browser.session, "key": key,
                               "action": key_action, "page": page})
        return {"ok": True, "operation": "PRESS_KEY", "kind": "key",
                "label": f"Pressed {', '.join(prepared['keys'])}",
                "target": ", ".join(prepared["keys"]), "note": ""}

    def _do_select_dropdown(self, state, page, prepared):
        browser = state["browser"]
        options = browser.read_options(browser.session, prepared["element"].get("node"))
        match = match_option(prepared["option"], options)
        if match is None:
            return {"ok": False, "operation": "SELECT_OPTION", "kind": "select",
                    "label": f"Selected '{_clean(prepared['option'], 60)}'",
                    "target": self._target_desc(prepared["element"]),
                    "note": "no matching enabled option"}
        browser.act({**prepared["element"], "value": match.get("value")}, page)
        self.trace.emit("verify", step=len(state.get("history") or []) + 1,
                        control=_clean(prepared["element"].get("label"), 80), verified=True)
        return {"ok": True, "operation": "SELECT_OPTION", "kind": "select",
                "label": f"Selected '{_clean(match.get('label'), 60)}'",
                "target": self._target_desc(prepared["element"]), "note": ""}

    def _do_dropdown_options(self, state, page, prepared):
        browser = state["browser"]
        options = browser.read_options(browser.session, prepared["element"].get("node"))
        labels = [str(item.get("label") or "") for item in options or [] if item.get("label")]
        shown = "; ".join(labels[:12])
        more = f" (+{len(labels) - 12} more)" if len(labels) > 12 else ""
        fact = f"{prepared['element'].get('label') or 'dropdown'} options: {shown}{more}"[:300]
        return {"ok": True, "operation": "READ_OPTIONS", "kind": "select",
                "label": f"Read {len(labels)} options", "target": self._target_desc(prepared["element"]),
                "note": f"{len(labels)} options", "verified_fact": fact}

    def _do_scroll(self, state, page, prepared):
        for _ in range(prepared["pages"]):
            state["browser"].act(prepared["scroll"], page)
        note = prepared.get("modal_note") or ""
        return {"ok": True, "operation": f"SCROLL_{prepared['direction'].upper()}",
                "kind": "scroll", "label": f"Scrolled {prepared['direction']} x{prepared['pages']}",
                "target": prepared["direction"], "note": _clean(note, 160)}

    def _do_find_text(self, state, page, prepared):
        return self._search_text(page, prepared, operation="FIND_TEXT", regex=False)

    def _do_search_page(self, state, page, prepared):
        return self._search_text(page, prepared, operation="SEARCH_PAGE", regex=True)

    def _search_text(self, page, prepared, operation, regex):
        text = self._page_text(page)
        pattern = prepared["pattern"]
        matches = []
        if regex:
            try:
                for match in re.finditer(pattern, text):
                    matches.append(match.start())
                    if len(matches) >= 50:
                        break
            except re.error as error:
                return {"ok": False, "operation": operation, "kind": "read",
                        "label": f"{operation.title()} '{_clean(pattern, 60)}'",
                        "target": _clean(pattern, 60), "note": f"invalid regex: {error}"[:160]}
        else:
            haystack, needle = text.lower(), pattern.lower()
            start = 0
            while len(matches) < 50:
                found = haystack.find(needle, start)
                if found < 0:
                    break
                matches.append(found)
                start = found + max(len(needle), 1)
        snippets = []
        for at in matches[:3]:
            snippets.append(_clean(text[max(at - 60, 0): at + 120], 180))
        fact = f"'{pattern[:60]}': {len(matches)} matches" + (f"; e.g. {snippets[0]}" if snippets else "")
        return {"ok": True, "operation": operation, "kind": "read",
                "label": f"{operation.title()} '{_clean(pattern, 60)}': {len(matches)}",
                "target": _clean(pattern, 60),
                "note": "; ".join(snippets)[:300] if snippets else "no matches",
                "verified_fact": fact[:300]}

    def _do_extract(self, state, page, prepared):
        body = (
            f"Query: {prepared['query']}\n\nPage ({page.get('url')}):\n"
            f"{self._page_text(page)[:12000]}"
        )
        try:
            result = self._helpers().complete(
                [{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": body}],
                response_format=EXTRACT_FORMAT, purpose="extract",
                step=len(state.get("history") or []) + 1, trace=self.trace,
                max_completion_tokens=1200,
            )
        except (ProviderError, RefusalError) as error:
            return {"ok": False, "operation": "EXTRACT", "kind": "read",
                    "label": f"Extracted '{_clean(prepared['query'], 60)}'",
                    "target": _clean(prepared["query"], 60), "note": str(error)[:160]}
        state["text_calls"].append({
            "model": result.model, "usage": result.usage, "latency_ms": result.latency_ms,
            "effort": "low", "field": "extract", "value": result.output.get("text", ""),
        })
        text = (result.output.get("text") or "").strip()
        if not text:
            return {"ok": False, "operation": "EXTRACT", "kind": "read",
                    "label": f"Extracted '{_clean(prepared['query'], 60)}'",
                    "target": _clean(prepared["query"], 60), "note": "empty extraction"}
        return {"ok": True, "operation": "EXTRACT", "kind": "read",
                "label": f"Extracted '{_clean(prepared['query'], 60)}'",
                "target": _clean(prepared["query"], 60), "note": _clean(text, 300),
                "verified_fact": text[:300],
                "memo_fact": {"fact": text[:500], "source": page.get("url") or ""}}

    def _do_upload_file(self, state, page, prepared):
        state["browser"].act({**prepared["element"], "upload_path": prepared["path"]}, page)
        return {"ok": True, "operation": "UPLOAD", "kind": "upload",
                "label": f"Uploaded '{prepared['key']}'",
                "target": self._target_desc(prepared["element"]), "note": ""}

    def _do_save_file(self, state, page, prepared):
        write_saved_file(self._files_dir(state), prepared["filename"], prepared["content"])
        return {"ok": True, "operation": "SAVE_FILE", "kind": "save_file",
                "label": f"Saved '{prepared['filename']}'", "target": prepared["filename"],
                "note": f"{len(prepared['content'])} chars"}

    def _do_read_file(self, state, page, prepared):
        with open(prepared["path"], encoding="utf-8", errors="replace") as handle:
            content = handle.read(20000)
        return {"ok": True, "operation": "READ_FILE", "kind": "read",
                "label": f"Read '{prepared['filename']}'", "target": prepared["filename"],
                "note": _clean(content, 1500), "verified_fact": _clean(content, 300)}

    def _do_wait(self, state, page, prepared):
        time.sleep(prepared["seconds"])
        return {"ok": True, "operation": "WAIT", "kind": "wait",
                "label": f"Waited {prepared['seconds']:g}s", "target": "", "note": ""}

    @staticmethod
    def _target_desc(element):
        parts = [element.get("kind") or "", element.get("role") or ""]
        label = _clean(element.get("label"), 40).lower()
        if label:
            parts.append(label)
        return ":".join(part for part in parts if part)


