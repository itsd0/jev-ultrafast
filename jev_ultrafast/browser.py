"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp, drain_events

from . import cloud
from .config import env_bool, resolve_config, snapshot_config
from .trace import get_logger

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()


def snapshot_expression(config=None):
    payload = json.dumps(snapshot_config(config or resolve_config()))
    return f"(()=>{{globalThis.__jevCfg={payload};return ({READ_STATE});}})()"


def marker_expression(config=None):
    return f"(() => {{ const state={snapshot_expression(config)}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class CoveredTarget(ValueError):
    """The target is live but another element paints over its click point."""


class Browser:
    def __init__(self, url, config=None):
        self.config = config or resolve_config()
        self.frame_sessions = {}
        self._target_sessions = {}
        self.tabs = {}
        self.active = None
        self._tab_serial = 0
        self._ready_after_act = False
        self.cloud = None
        self.cdp_counter = {"calls": 0, "ms": 0.0}
        self.context = None
        cloud_mode = self.config.get("browser") == "cloud"
        if cloud_mode:
            # The cloud daemon is already attached. ensure_daemon() would spawn a
            # local Chrome daemon if the cloud one died.
            self.cloud = cloud.start(self.config)
        else:
            ensure_daemon()
            # A fresh context per run: history suggestions and cookies from the
            # shared profile never leak into a run's comboboxes. Falls back to
            # the default context when the daemon predates the command.
            try:
                response = cdp("Target.createBrowserContext")
                self.context = response.get("browserContextId") if isinstance(response, dict) else None
            except Exception as error:
                get_logger("browser").warning(f"[jev:browser] no fresh context ({error}); using shared profile")
                self.context = None
        try:
            visible_env = os.environ.get("JEV_VISIBLE_TAB")
            if visible_env is None or not visible_env.strip():
                visible = cloud_mode
            else:
                visible = visible_env.strip().lower() in {"1", "true", "yes", "on"}
            params = {"url": "about:blank", "background": not visible, "newWindow": False}
            if self.context:
                # A context cannot share the user's window: the run gets its own.
                params["browserContextId"] = self.context
                params["newWindow"] = True
            self.target = cdp("Target.createTarget", **params)["targetId"]
            self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            if visible:
                cdp("Target.activateTarget", targetId=self.target)
                self.call("Page.bringToFront")
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
            # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            options = self.config.get("cloud") or {}
            if options.get("locale") and cloud.supported("Emulation.setLocaleOverride"):
                try:
                    self.call("Emulation.setLocaleOverride", locale=options["locale"])
                except RuntimeError:
                    pass
            if options.get("timezone"):
                try:
                    self.call("Emulation.setTimezoneOverride", timezoneId=options["timezone"])
                except RuntimeError:
                    pass
            self.active = self._register_tab(self.target, self.session, url)
            if self.config.get("tabs", True):
                try:
                    cdp("Target.setDiscoverTargets", discover=True)
                except RuntimeError:
                    pass
            self.call("Page.navigate", url=url)
            deadline = time.monotonic() + 15
            poll = 0.5 if cloud_mode else 0.02
            while time.monotonic() < deadline:
                if self.evaluate("document.readyState") == "complete":
                    break
                time.sleep(poll)
            if cloud_mode:
                self._verify_viewport()
        except BaseException:
            self.stop_cloud()
            raise

    def call(self, method, **params):
        started = time.perf_counter()
        try:
            return cdp(method, session_id=self.session, **params)
        finally:
            self._bump_cdp(time.perf_counter() - started)

    def _bump_cdp(self, seconds):
        counter = getattr(self, "cdp_counter", None)
        if counter is None:
            return
        counter["calls"] += 1
        counter["ms"] += seconds * 1000

    def cdp_snapshot(self):
        counter = getattr(self, "cdp_counter", None) or {"calls": 0, "ms": 0.0}
        return counter["calls"], counter["ms"]

    VIEWPORT = (1120, 780, 1)

    def _set_viewport(self):
        width, height, dpr = self.VIEWPORT
        self.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=dpr,
            mobile=False,
        )

    def _viewport_reading(self):
        return self.evaluate("(()=>({w:innerWidth,h:innerHeight,dpr:devicePixelRatio}))()")

    def _verify_viewport(self):
        """The cloud window ignores the attach-time override; re-apply and report."""
        expected = {"w": self.VIEWPORT[0], "h": self.VIEWPORT[1], "dpr": self.VIEWPORT[2]}
        try:
            actual = self._viewport_reading()
        except StalePage:
            return None
        if not isinstance(actual, dict) or actual == expected:
            return actual
        try:
            self._set_viewport()
        except RuntimeError:
            pass
        try:
            retry = self._viewport_reading()
        except StalePage:
            return actual
        if isinstance(retry, dict) and retry != expected:
            get_logger("cloud").warning(
                f"[jev:cloud] viewport mismatch: got {retry.get('w')}x{retry.get('h')} "
                f"dpr={retry.get('dpr')}, expected {expected['w']}x{expected['h']} dpr={expected['dpr']}"
            )
        return retry

    def vision_click(self, x, y):
        """Coordinate input. This is the only path that does not use a code-owned node id."""
        get_logger("vision").warning(
            f"[jev:vision] coordinate-based click x={x:.1f} y={y:.1f} breaks the node-id invariant"
        )
        for event in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
        self.after_input = {"kind": "vision", "node": None}
        return {"executed": "VISION_CLICK", "coordinate_based": True}

    def read_options(self, action):
        """Current enabled-state of a native select. Labels stay out of the model request."""
        if type(action.get("node")) is not int:
            return []
        frame = json.dumps(action.get("frame") or "f0")
        node = int(action["node"])
        value = self.evaluate(
            "(() => { const c=window.__jevFast; if (!c) return null; "
            f"const win=(c.frameWins&&c.frameWins[{frame}])||window; const local=win.__jevFast; "
            f"const e=local&&local.nodes.get({node}); if (!e||e.tagName!=='SELECT') return null; "
            "return [...e.options].map(o=>({label:o.label, value:o.value, "
            "disabled:!!(o.disabled||o.closest('optgroup[disabled]'))})); })()"
        )
        return value or []

    def evaluate(self, expression, session=None, await_promise=False):
        started = time.perf_counter()
        try:
            response = cdp(
                "Runtime.evaluate",
                session_id=session or self.session,
                expression=expression,
                returnByValue=True,
                awaitPromise=await_promise,
            )
        finally:
            self._bump_cdp(time.perf_counter() - started)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        self.settle_dom()
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        request = {
            "operation": "observe",
            "session": self.session,
            "screenshot": screenshot,
            "config": self.config,
            "counter": getattr(self, "cdp_counter", None),
        }
        for attempt in range(12):
            try:
                state = self._finish_observation(browser_operation(request))
                if modal_observed_empty(state):
                    # A freshly opened dialog can snapshot before its controls
                    # paint; settle once more and re-observe instead of
                    # deciding on an empty modal.
                    self.settle_dom(quiet_ms=300, cap_ms=2000)
                    try:
                        state = self._finish_observation(browser_operation(request))
                    except StalePage:
                        pass
                return state
            except StalePage:
                if attempt == 11:
                    raise
                # Heavy pages can navigate for seconds; back off instead of
                # failing the run after 200ms of impatience.
                time.sleep(min(0.05 * (2**attempt), 1.0))
        raise StalePage("Page did not settle")

    def _finish_observation(self, state):
        if self.config.get("frames") == "all" and state.get("cross_frames"):
            state = self._attach_oopif(state)
        state = bind_uploads(state, self.config.get("files") or {})
        state = bind_navigate(state)
        state = bind_tabs(state, self)
        return bind_keys(state, self.config.get("keyboard", True))

    def _attach_oopif(self, state):
        """Attach each visible cross-origin frame once per observation. Same-origin frames are already inlined."""
        try:
            self._frame_tree = self.call("Page.getFrameTree")
            targets = cdp("Target.getTargets").get("targetInfos", [])
        except (RuntimeError, KeyError, TypeError):
            return state
        children = []
        for frame, target in match_iframe_targets(state.get("cross_frames") or [], targets):
            target_id = target.get("targetId")
            try:
                session = self._target_sessions.get(target_id)
                if not session:
                    session = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
                    self._target_sessions[target_id] = session
                child_config = {**self.config, "frames": "same", "frame_id": "f0"}
                child = browser_operation(
                    {
                        "operation": "observe",
                        "session": session,
                        "screenshot": False,
                        "config": child_config,
                        "counter": getattr(self, "cdp_counter", None),
                    }
                )
            except (RuntimeError, StalePage, KeyError, TypeError):
                continue
            children.append(
                {
                    "id": frame["id"],
                    "origin": {"x": frame["rect"]["x"], "y": frame["rect"]["y"]},
                    "state": child,
                }
            )
            self.frame_sessions[frame["id"]] = session
        if not children:
            return state
        merged = merge_oopif_state(state, children)
        merged["fingerprint"] = fingerprint(merged)
        return merged

    def _identity_expression(self, action):
        frame = json.dumps(action.get("frame") or "f0")
        node = int(action["node"])
        return (
            "(() => { const c=window.__jevFast; if (!c||!c.pageKey) return null; "
            f"const win=(c.frameWins&&c.frameWins[{frame}])||window; const local=win.__jevFast; "
            f"if (!local||!local.describeNode) return null; "
            f"return [c.pageKey(), local.describeNode({node})]; }})()"
        )

    def _oopif_keys_fresh(self, page):
        for route, key in (page.get("frame_keys") or {}).items():
            session = self.frame_sessions.get(route)
            if not session:
                return False
            if self.evaluate("window.__jevFast ? window.__jevFast.pageKey() : null", session) != key:
                return False
        return True

    def fresh(self, page, action=None):
        """Element identity for targeted actions; document identity otherwise.

        Live text, input values and viewport geometry never invalidate a
        decision. Navigation, tab switches and terminal choices are not gated.
        """
        if not self._oopif_keys_fresh(page):
            return False
        if action is None:
            return self._doc_fresh(page)
        kind = action.get("kind")
        if kind in {"scroll", "key", "wait", "vision"}:
            return self._doc_fresh(page)
        if kind not in {"click", "fill", "select", "select_option", "check", "upload"}:
            return True
        return self._target_fresh(page, action)

    def _doc_fresh(self, page):
        return self.evaluate("[performance.timeOrigin, location.href]") == page.get("marker")

    def settle_dom(self, quiet_ms=150, cap_ms=1500):
        """Wait for 150 ms of DOM quiet, capped at 1.5 s. Never raises."""
        expression = (
            "new Promise(resolve => {"
            f" const cap=setTimeout(done,{int(cap_ms)}); let quiet=0; let obs=null;"
            " const done=()=>{clearTimeout(cap);clearTimeout(quiet);"
            " try{obs&&obs.disconnect();}catch(e){} resolve(true);};"
            f" const arm=()=>{{clearTimeout(quiet);quiet=setTimeout(done,{int(quiet_ms)});}};"
            " try{obs=new MutationObserver(arm);"
            " obs.observe(document,{childList:true,subtree:true,characterData:true});}"
            " catch(e){done();return;}"
            " arm(); })"
        )
        try:
            self.evaluate(expression, await_promise=True)
        except (RuntimeError, KeyError, TypeError, StalePage):
            pass

    def _target_fresh(self, page, action):
        node = action.get("node")
        if type(node) is not int:
            return self._doc_fresh(page)
        if action.get("route"):
            session = self.frame_sessions.get(action["route"])
            if not session:
                return False
            current = self.evaluate(self._identity_expression(action), session)
            guard_key = f"{action['route']}:{action.get('frame') or 'f0'}:{node}"
            expected_key = page.get("frame_keys", {}).get(action["route"])
        else:
            current = self.evaluate(self._identity_expression(action))
            guard_key = f"{action.get('frame') or 'f0'}:{node}"
            expected_key = page.get("page_key")
        if not isinstance(current, list) or len(current) != 2:
            return False
        live_key, identity = current
        if live_key != expected_key:
            return False
        expected = (page.get("guards") or {}).get(guard_key) or {}
        if not isinstance(identity, dict) or not isinstance(expected, dict):
            return False
        return identity.get("role") == expected.get("role") and identity.get("name") == expected.get("name")

    def wait_for_refresh(self, previous_fingerprint, timeout=3.0, screenshot=False):
        """After a reload-like click, wait for a DOM change or for network traffic to finish.

        Returns the latest observation. The wait stops at `timeout` seconds.
        """
        deadline = time.monotonic() + timeout
        inflight = 0
        saw_request = False
        try:
            self.call("Network.enable")
        except (RuntimeError, TypeError):
            pass
        latest = None
        while time.monotonic() < deadline:
            try:
                events = drain_events()
            except RuntimeError:
                events = []
            for event in events or []:
                method = event.get("method")
                if method == "Network.requestWillBeSent":
                    inflight += 1
                    saw_request = True
                elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
                    inflight = max(0, inflight - 1)
            try:
                latest = self.observe(screenshot=screenshot)
            except StalePage:
                time.sleep(0.1)
                continue
            if latest.get("fingerprint") != previous_fingerprint:
                return latest
            if saw_request and inflight == 0:
                time.sleep(0.2)
                try:
                    return self.observe(screenshot=screenshot)
                except StalePage:
                    return latest
            time.sleep(0.1)
        if latest is None:
            try:
                return self.observe(screenshot=False)
            except StalePage:
                return None
        return latest

    def wait_until_ready(self, timeout=10):
        """Bounded wait for document completion. Returns immediately if already ready."""
        if getattr(self, "_ready_after_act", False):
            self._ready_after_act = False
            return True
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return True
            except StalePage:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def settled(self, page, timeout=10):
        """True once the document is complete and two consecutive snapshots match."""
        deadline = time.monotonic() + timeout
        last, quiet = page.get("fingerprint"), 0
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") != "complete":
                    quiet = 0
                else:
                    current = self.observe(screenshot=False)["fingerprint"]
                    if current == last:
                        quiet += 1
                        if quiet >= 2:
                            return True
                    else:
                        quiet, last = 0, current
            except StalePage:
                quiet = 0
            time.sleep(0.25)
        return False

    def navigate(self, url):
        """Model-directed first navigation. The URL gate fails closed.

        An IPC timeout retries once: the daemon may have died, or the
        navigate committed and only the acknowledgement was lost. A second
        timeout records a failed step instead of raising.
        """
        target = validate_navigate_url(url)
        try:
            self.call("Page.navigate", url=target)
            self.wait_until_ready()
        except TimeoutError:
            pass
        else:
            self.after_input = None
            return {"executed": "navigate", "url": target}
        try:
            current = self.evaluate("location.href")
        except (TimeoutError, StalePage):
            current = None
        if isinstance(current, str) and current.rstrip("/") == target.rstrip("/"):
            self.after_input = None
            return {"executed": "navigate", "url": target}
        if (getattr(self, "config", None) or {}).get("browser") != "cloud":
            # A stale cloud daemon owns a billable browser; only a local
            # navigate respawns the daemon. A cloud navigate just retries.
            ensure_daemon()
        try:
            self.call("Page.navigate", url=target)
            self.wait_until_ready()
        except TimeoutError as error:
            self.after_input = None
            get_logger("browser").warning(f"[jev:navigate] {target} timed out twice; recording a failed step")
            return {
                "executed": "navigate", "url": target, "navigated": False,
                "error": f"{type(error).__name__}: {error}"[:200],
            }
        self.after_input = None
        return {"executed": "navigate", "url": target}

    def go_back(self):
        """One step back in session history. A no-op with no history behind."""
        self.evaluate("history.back()")
        self.after_input = None

    def act(self, action, page, text=None, goal=None, tiebreak=None):
        cloud_mode = (getattr(self, "config", None) or {}).get("browser") == "cloud"
        self.settle_dom()
        probe = None
        if cloud_mode and action["kind"] in {"click", "fill", "select"}:
            # One cloud call rechecks the guard, resolves geometry, and reads the
            # previous field value. Local keeps its existing calls.
            probe = action_freshness(page, action)
            if probe is None:
                raise StalePage("Page changed since this decision. Observe again.")
        elif not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        if action["kind"] == "switch_tab":
            self.activate(action.get("tab"))
            self.after_input = None
            return {"executed": action["id"]}
        if action["kind"] == "close_tab":
            self.close_active()
            self.after_input = None
            return {"executed": action["id"]}
        session = self.session
        if action.get("route"):
            session = self.frame_sessions.get(action["route"])
            if not session:
                raise StalePage("Frame is gone. Observe again.")
        self._ready_after_act = False
        result = browser_operation({
            "operation": "act",
            "session": session,
            "action": action,
            "text": text,
            "files": (getattr(self, "config", None) or {}).get("files") or {},
            "keyboard": (getattr(self, "config", None) or {}).get("keyboard", True),
            "probe": probe,
            "verify_wait": cloud_mode,
            "goal": goal or "",
            "tiebreak": tiebreak,
            "counter": getattr(self, "cdp_counter", None),
        })
        if result.get("ready"):
            self._ready_after_act = True
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        try:
            owned = [tab["target"] for tab in self.tabs.values()]
            if not owned and getattr(self, "target", None):
                owned = [self.target]
            targets = owned
            self.target = None
            self.tabs = {}
            for target in targets:
                try:
                    cdp("Target.closeTarget", targetId=target)
                except RuntimeError:
                    pass
            context, self.context = self.context, None
            if context:
                try:
                    cdp("Target.disposeBrowserContext", browserContextId=context)
                except RuntimeError:
                    pass
        finally:
            self.stop_cloud()

    def stop_cloud(self):
        handle, self.cloud = self.cloud, None
        if handle:
            cloud.stop(handle)
            return True
        return False

    def _register_tab(self, target_id, session, url, opener=None):
        for key, tab in self.tabs.items():
            if tab["target"] == target_id:
                tab.update(session=session, url=url or tab.get("url") or "")
                return key
        if len(self.tabs) >= 5:
            return None
        self._tab_serial += 1
        key = f"t{self._tab_serial}"
        self.tabs[key] = {"target": target_id, "session": session, "url": url or "", "opener": opener}
        return key

    def activate(self, key):
        tab = self.tabs.get(key)
        if not tab:
            raise StalePage("Tab is gone. Observe again.")
        self.active = key
        self.target = tab["target"]
        self.session = tab["session"]
        return tab

    def close_active(self):
        if len(self.tabs) <= 1:
            raise ValueError("The last tab stays open")
        key = self.active
        tab = self.tabs.pop(key)
        try:
            cdp("Target.closeTarget", targetId=tab["target"])
        except RuntimeError:
            pass
        self.activate(next(iter(self.tabs)))
        return key

    def collect_new_tabs(self):
        """Attach pages opened by one of our tabs. Code-owned ids stay t1, t2, ..."""
        if not self.config.get("tabs", True):
            return []
        try:
            events = drain_events()
        except RuntimeError:
            return []
        owned = {tab["target"] for tab in self.tabs.values()}
        opened = []
        for info in new_page_targets(events, owned):
            if len(self.tabs) >= 5:
                break
            target_id = info["targetId"]
            try:
                session = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
                cdp("Emulation.setFocusEmulationEnabled", session_id=session, enabled=True)
            except (RuntimeError, KeyError, TypeError):
                continue
            key = self._register_tab(target_id, session, info.get("url") or "", info.get("openerId"))
            if key:
                opened.append(key)
                owned.add(target_id)
        return opened


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll") if k in state}
    if state.get("frame_keys"):
        content["frame_keys"] = state["frame_keys"]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


_CAPTCHA_PATH = re.compile(r"/sorry|captcha|challenge", re.IGNORECASE)
_CHALLENGE_FRAME = re.compile(r"recaptcha|hcaptcha|turnstile|cloudflare", re.IGNORECASE)
_CAPTCHA_TEXT = re.compile(
    r"unusual traffic|verify you(?:'re| are)(?: a)? human|are you a robot|not a robot|access denied",
    re.IGNORECASE,
)


def detect_captcha(state):
    """A generic environment wall: sorry/captcha URL, challenge iframe, 403/429, or challenge text.

    Only visible cross-origin iframes are reported, so an invisible background
    captcha that never blocks input does not fire. Returns a reason, or None.
    """
    url = state.get("url") or ""
    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    if parsed is not None and (parsed.scheme or "").lower() in {"http", "https"}:
        if _CAPTCHA_PATH.search(parsed.path or ""):
            return f"captcha path: {(parsed.path or '')[:80]}"
    for frame in state.get("cross_frames") or []:
        src = frame.get("url") or frame.get("src") or ""
        if _CHALLENGE_FRAME.search(src):
            return f"challenge iframe: {src[:80]}"
    if state.get("status") in (403, 429):
        return f"http {state['status']}"
    text = f"{state.get('title') or ''}\n{state.get('text') or ''}"
    match = _CAPTCHA_TEXT.search(text)
    if match:
        return f"challenge text: {match.group(0)[:60]}"
    return None


def new_page_targets(events, owned_ids):
    """Pages whose opener is one of our targets. Unrelated Chrome tabs are ignored."""
    found = []
    seen = set(owned_ids)
    for event in events or []:
        if event.get("method") not in {"Target.targetCreated", "Target.attachedToTarget"}:
            continue
        info = (event.get("params") or {}).get("targetInfo") or {}
        if info.get("type") != "page":
            continue
        target_id = info.get("targetId")
        if info.get("openerId") not in owned_ids or not target_id or target_id in seen:
            continue
        seen.add(target_id)
        found.append(info)
    return found


def validate_navigate_url(url):
    """Fail-closed gate for model-proposed navigation. Returns the cleaned URL."""
    text = (url or "").strip()
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError(f"Refusing to navigate: need an http(s) URL with a host (scheme={scheme or 'none'})")
    local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
    if scheme != "https" and not local:
        raise ValueError("Refusing to navigate to a non-https URL")
    if local and not env_bool("JEV_ALLOW_LOCAL", False):
        raise ValueError("Refusing to navigate to a local URL without JEV_ALLOW_LOCAL=1")
    return text


def bind_navigate(state):
    """Offer one NAVIGATE control on every page. The text model names the URL
    from the goal and the progress so far, so multi-site goals can leave the
    current page. The URL gate and the loop detector bound abuse."""
    actions = state.get("actions") or []
    if any(action.get("kind") == "navigate" for action in actions):
        return state
    updated = {
        **state,
        "actions": [
            *actions,
            {"id": "navigate", "kind": "navigate", "label": "Navigate to a URL from the goal"},
        ],
    }
    updated["fingerprint"] = fingerprint(updated)
    return updated


def bind_tabs(state, browser):
    """Add SWITCH_TAB / CLOSE_TAB controls. Choice ids are t1, t2, not CDP target ids."""
    tabs = getattr(browser, "tabs", {}) or {}
    if not (getattr(browser, "config", None) or {}).get("tabs", True) or len(tabs) <= 1:
        return state
    actions = [action for action in state.get("actions", []) if action.get("kind") not in {"switch_tab", "close_tab"}]
    active = getattr(browser, "active", None)
    for key, tab in tabs.items():
        if key == active:
            continue
        actions.append({
            "id": f"switch:{key}",
            "kind": "switch_tab",
            "tab": key,
            "label": f"Switch to tab {tab.get('url') or key}",
        })
    actions.append({"id": "close_tab", "kind": "close_tab", "label": "Close current tab"})
    updated = {**state, "actions": actions}
    updated["fingerprint"] = fingerprint(updated)
    return updated


PRESS_KEYS = (
    "Enter",
    "Tab",
    "Escape",
    "ArrowDown",
    "ArrowUp",
    "ArrowLeft",
    "ArrowRight",
    "Space",
    "Backspace",
)

_KEY_SPECS = {
    "Enter": {"key": "Enter", "code": "Enter", "text": "\r", "windowsVirtualKeyCode": 13},
    "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "ArrowUp": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
    "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "ArrowRight": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "Space": {"key": " ", "code": "Space", "text": " ", "windowsVirtualKeyCode": 32},
    "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
}


def dispatch_key(call, name):
    if name not in _KEY_SPECS:
        raise ValueError("Key is not in the PRESS_KEY enum; nothing was sent.")
    spec = _KEY_SPECS[name]
    for event in ("keyDown", "char", "keyUp"):
        if event == "char" and "text" not in spec:
            continue
        params = {
            "type": event,
            "key": spec["key"],
            "code": spec["code"],
            "windowsVirtualKeyCode": spec["windowsVirtualKeyCode"],
        }
        if "text" in spec and event != "keyUp":
            params["text"] = spec["text"]
        call("Input.dispatchKeyEvent", **params)


def dispatch_char(call, character):
    # Text goes on keyDown only. A char event with the same text inserts a second copy.
    call("Input.dispatchKeyEvent", type="keyDown", key=character, text=character, code="")
    call("Input.dispatchKeyEvent", type="keyUp", key=character, code="")


_CHECK_RESOLVE = """(action => {
  const fast=window.__jevFast;
  const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkVisibilityCSS:true})) return null;
  const r=e.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const x=r.x+r.width/2, y=r.y+r.height/2;
  if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
  const native=(e.tagName==='INPUT' && ['checkbox','radio'].includes(e.type)) ? e.checked : null;
  const aria=e.getAttribute('aria-checked');
  const checked=native!==null ? native : (aria==='true' ? true : aria==='false' ? false : null);
  const lab=(e.labels && e.labels[0]) || e.closest('label');
  let lx=null, ly=null;
  if (lab && lab!==e) {
    const q=lab.getBoundingClientRect();
    if (q.width && q.height && q.x+q.width/2>=0 && q.y+q.height/2>=0 &&
        q.x+q.width/2<innerWidth && q.y+q.height/2<innerHeight) { lx=q.x+q.width/2; ly=q.y+q.height/2; }
  }
  return {x, y, lx, ly, checked};
})"""

_CHECK_FOCUS = """(action => {
  const fast=window.__jevFast;
  const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node);
  if (!e?.isConnected) return false;
  e.focus();
  return document.activeElement===e;
})"""


def act_check(call, evaluate, action):
    """Set a checkbox/radio/switch to its desired state, verifying after each strategy.

    Transparent native inputs and painted overlays are expected here, so there
    is no coverage gate: the flipped state is the proof. Each strategy runs once:
    input center, label center, then focus plus Space.
    """
    if type(action.get("node")) is not int:
        raise ValueError("Invalid observed node")
    locator = json.dumps({k: action[k] for k in ("node", "frame", "kind") if k in action})
    desired = bool(action.get("desired", True))

    def read():
        return evaluate(_CHECK_RESOLVE + "(" + locator + ")")

    state = read()
    if state is None:
        raise StalePage("Target changed. Observe again.")
    if state.get("checked") is not None and bool(state["checked"]) == desired:
        return {"executed": action["id"], "already_set": True, "checked": state["checked"]}
    points = [(state["x"], state["y"])]
    if state.get("lx") is not None and (state["lx"], state["ly"]) != (state["x"], state["y"]):
        points.append((state["lx"], state["ly"]))
    for x, y in points:
        for event in ("mousePressed", "mouseReleased"):
            call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
        state = read()
        if state is None:
            raise StalePage("Target changed after input. Observe again.")
        if state.get("checked") == desired:
            return {"executed": action["id"], "verified": True, "checked": state["checked"]}
    if evaluate(_CHECK_FOCUS + "(" + locator + ")"):
        dispatch_key(call, "Space")
        state = read()
        if state is None:
            raise StalePage("Target changed after input. Observe again.")
        if state.get("checked") == desired:
            return {"executed": action["id"], "verified": True, "checked": state["checked"]}
    return {"executed": action["id"], "verified": False, "checked": state.get("checked")}


def bind_keys(state, enabled):
    if not enabled:
        return state
    actions = [action for action in state.get("actions", []) if action.get("kind") != "key"]
    for name in PRESS_KEYS:
        actions.append({"id": f"key:{name}", "kind": "key", "key": name, "label": f"Press {name}"})
    updated = {**state, "actions": actions}
    updated["fingerprint"] = fingerprint(updated)
    return updated


def _field_value_reader():
    # SPAs often replace the focused control while keeping the same logical
    # field. When the observed node is detached and the DOM holds exactly one
    # editable, read the live replacement via the focused element. With two or
    # more editables the replacement is ambiguous, so the read stays a miss.
    return (
        "(action => { const fast=window.__jevFast; "
        "let e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        "if (e && !e.isConnected) { "
        "const editable=document.querySelectorAll("
        "'input,textarea,[contenteditable=\"\"],[contenteditable=\"true\"]'); "
        "if (editable.length===1) { const a=document.activeElement; "
        "if (a && (a.isContentEditable||a.tagName==='INPUT'||a.tagName==='TEXTAREA')) e=a; } } "
        "if (!e) return null; "
        "return e.isContentEditable ? e.innerText : ('value' in e ? String(e.value) : ''); })"
    )


def field_value_expression(action):
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return _field_value_reader() + "(" + json.dumps(locator) + ")"


def ready_value_expression(action):
    """One read for the post-input value and readyState, so a ready page skips another call."""
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return (
        "(action => { const read=" + _field_value_reader() + "; "
        "return {ready: document.readyState === 'complete', value: read(action)}; })("
        + json.dumps(locator)
        + ")"
    )


_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_WEEKDAYS = {
    "mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday", "thu",
    "thur", "thurs", "thursday", "fri", "friday", "sat", "saturday", "sun", "sunday",
}
_NUMERIC_DATE = re.compile(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?")


def parse_date(value, order=None):
    """Parse the suite's date displays into (month, day, year or None).

    Accepts ISO ("2027-06-12"), "June 12 2027", "12 June 2027", "Jun 12",
    and "Sat, Jun 12". The weekday is parsed and ignored, never validated.
    Numeric forms use the caller's order ("mdy"/"dmy", taken from the field's
    own format); without one only self-disambiguating numerics parse, so
    "06/07" stays a non-date and falls back to strict comparison.
    """
    if not isinstance(value, str):
        return None
    parts = value.strip().lower().replace(",", " ").split()
    if parts and parts[0] in _WEEKDAYS:
        parts = parts[1:]
    if not parts:
        return None
    first = parts[0]
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", first)
    if iso and len(parts) == 1:
        year, month, day = (int(group) for group in iso.groups())
        return (month, day, year) if 1 <= month <= 12 and 1 <= day <= 31 else None
    numeric = _NUMERIC_DATE.fullmatch(first)
    if numeric and len(parts) == 1:
        first_num, second_num, year_text = numeric.groups()
        year = None
        if year_text is not None:
            year = int(year_text) + (2000 if len(year_text) == 2 else 0)
        orders = [order] if order in ("mdy", "dmy") else ("mdy", "dmy")
        found = None
        for candidate in orders:
            one, two = (first_num, second_num) if candidate == "mdy" else (second_num, first_num)
            month, day = int(one), int(two)
            if 1 <= month <= 12 and 1 <= day <= 31:
                if order in ("mdy", "dmy"):
                    return (month, day, year)
                if found is not None:
                    return None
                found = (month, day, year)
        return found
    month = day = year = None
    for token in parts:
        if token in _MONTHS and month is None:
            month = _MONTHS[token]
        elif token.isdigit() and len(token) == 4 and year is None:
            year = int(token)
        elif token.isdigit() and day is None:
            day = int(token)
        else:
            return None
    if month is None or day is None or not 1 <= day <= 31:
        return None
    return (month, day, year)


def _numeric_order(previous):
    """The field's own numeric date order, or None when it does not disambiguate."""
    if not isinstance(previous, str):
        return None
    tokens = previous.strip().lower().replace(",", " ").split()
    if len(tokens) != 1 or not _NUMERIC_DATE.fullmatch(tokens[0]):
        return None
    month_first = parse_date(previous, "mdy")
    day_first = parse_date(previous, "dmy")
    if bool(month_first) == bool(day_first):
        return None
    return "mdy" if month_first else "dmy"


def _norm_text(value):
    folded = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def _compare_values(expected, actual, order=None):
    """Classify a fill read-back as exact, same_date, rewritten, or no match."""
    if actual == expected:
        return "exact"
    if not isinstance(actual, str) or not actual:
        return None
    want = parse_date(expected, order) if isinstance(expected, str) else None
    got = parse_date(actual, order)
    if want and got and want[:2] == got[:2]:
        if want[2] is None or got[2] is None or want[2] == got[2]:
            return "same_date"
    norm_want, norm_got = _norm_text(expected), _norm_text(actual)
    if norm_want and (
        norm_want in norm_got or set(norm_want.split()) <= set(norm_got.split())
    ):
        return "rewritten"
    return None


def verify_typed_text(
    call,
    evaluate,
    action,
    expected,
    previous=None,
    *,
    wait_ready=False,
):
    """Read the field after insertText. Retype once only if the value did not change.

    Comboboxes rewrite the inserted text (autocomplete, accents). Clearing that
    result drops the suggestion list. Keyboard-only fields leave the value
    untouched, and those are the ones that need key events.
    """
    expression = field_value_expression(action)
    ready = None
    if wait_ready:
        combined = evaluate(ready_value_expression(action))
        if isinstance(combined, dict):
            actual, ready = combined.get("value"), bool(combined.get("ready"))
        else:
            actual = combined
    else:
        actual = evaluate(expression)
    result = _verify_value(call, evaluate, action, expected, previous, actual)
    if ready is not None:
        result["ready"] = ready
    return result


def _verify_value(call, evaluate, action, expected, previous, actual):
    expression = field_value_expression(action)
    order = _numeric_order(previous)
    outcome = _compare_values(expected, actual, order)
    if outcome and (actual == expected or (actual not in (None, "") and actual != previous)):
        result = {"text_verified": True, "typed": actual}
        if outcome == "same_date":
            result["same_date"] = True
        elif outcome == "rewritten":
            result["rewritten"] = True
        return result
    if actual not in (None, "") and actual != previous:
        # The field changed to something unrelated. Retyping over a live
        # rewrite (autocomplete, twin swap) only garbles it further.
        return {"text_verified": False, "typed": actual}
    modifier = 4 if sys.platform == "darwin" else 2
    call(
        "Input.dispatchKeyEvent",
        type="keyDown", key="a", code="KeyA", modifiers=modifier, commands=["selectAll"],
    )
    call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=modifier)
    dispatch_key(call, "Backspace")
    for character in expected:
        dispatch_char(call, character)
    actual = evaluate(expression)
    outcome = _compare_values(expected, actual, order)
    result = {"text_verified": bool(outcome), "typed": actual}
    if outcome == "same_date":
        result["same_date"] = True
    elif outcome == "rewritten":
        result["rewritten"] = True
    return result


def twin_value_expression():
    """Read the focused editable directly (the live twin), or null."""
    return (
        "(() => { const a=document.activeElement; "
        "if (!a || !(a.isContentEditable||a.tagName==='INPUT'||a.tagName==='TEXTAREA')) return null; "
        "return a.isContentEditable ? a.innerText : ('value' in a ? String(a.value) : ''); })()"
    )


_COMMIT_KEY_CODES = {"Enter": "Enter", "Tab": "Tab", "Escape": "Escape"}


def commit_key_expression(action, key="Enter"):
    """Commit the field with a key in keydown-beforeinput-input order.

    Resolves the focused twin when the observed node detached, asserts
    document.activeElement is the target (focusing twice if needed), then
    dispatches keydown, beforeinput, and input. Reports {focused, ordered}.
    """
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    code = _COMMIT_KEY_CODES.get(key, key)
    input_type = "insertLineBreak" if key == "Enter" else "insertText"
    return (
        "(payload => { const fast=window.__jevFast; "
        "let e=fast?.resolve ? fast.resolve(payload.action) : fast?.nodes.get(payload.action.node); "
        "if (e && !e.isConnected) { const a=document.activeElement; "
        "if (a && (a.isContentEditable||a.tagName==='INPUT'||a.tagName==='TEXTAREA')) e=a; } "
        "if (!e) return {focused:false, ordered:false}; "
        "if (document.activeElement!==e) e.focus(); "
        "if (document.activeElement!==e) e.focus(); "
        "const focused=document.activeElement===e; "
        "e.dispatchEvent(new KeyboardEvent('keydown',"
        "{key:payload.key,code:payload.code,bubbles:true,cancelable:true})); "
        "e.dispatchEvent(new InputEvent('beforeinput',"
        "{inputType:payload.inputType,bubbles:true,cancelable:true})); "
        "e.dispatchEvent(new InputEvent('input',{inputType:payload.inputType,bubbles:true})); "
        "return {focused, ordered:true}; })("
        + json.dumps({"action": locator, "key": key, "code": code, "inputType": input_type})
        + ")"
    )


def modal_observed_empty(state):
    """A modal dialog whose snapshot caught no controls: observe once more."""
    if not (state or {}).get("modal_scoped"):
        return False
    return not any(
        action.get("kind") not in {"wait", "scroll"}
        for action in state.get("actions") or []
    )


def detect_slider(action):
    """A slider fill: role slider, set natively or via keys, not inserted text."""
    return (action or {}).get("role") == "slider"


def _parse_amount(text):
    """First number in text ($150 -> 150.0). None when there is none."""
    if not isinstance(text, str):
        return None
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def slider_geometry_expression(action):
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return (
        "(action => { const fast=window.__jevFast; "
        "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        "if (!e) return null; const r=e.getBoundingClientRect(); "
        "return {x:r.x, y:r.y, width:r.width, height:r.height}; })("
        + json.dumps(locator) + ")"
    )


def slider_value_expression(action):
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return (
        "(action => { const fast=window.__jevFast; "
        "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        "if (!e) return null; "
        "const aria=e.getAttribute ? e.getAttribute('aria-valuenow') : null; "
        "const raw=aria ?? (('value' in e) ? e.value : null); "
        "const num=parseFloat(raw); return Number.isFinite(num) ? num : null; })("
        + json.dumps(locator) + ")"
    )


# Commit keys for sliders: raw input only, never offered to the decision model.
_COMMIT_KEYS = {"Home": 36, "End": 35, "PageUp": 33, "PageDown": 34}


def _dispatch_commit_key(call, name):
    for event in ("keyDown", "keyUp"):
        call(
            "Input.dispatchKeyEvent", type=event, key=name, code=name,
            windowsVirtualKeyCode=_COMMIT_KEYS[name],
        )


def slider_set_native_expression(action, target):
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return (
        "((action, target) => { const fast=window.__jevFast; "
        "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        "if (!e || !('value' in e)) return null; "
        "const proto=e.tagName==='INPUT' ? window.HTMLInputElement.prototype : "
        "(e.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype : null); "
        "try { const desc=proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null; "
        "if (desc && desc.set) desc.set.call(e, String(target)); else e.value=String(target); "
        "e.dispatchEvent(new Event('input', {bubbles:true})); "
        "e.dispatchEvent(new Event('change', {bubbles:true})); } catch (err) { return null; } "
        "const num=parseFloat(e.value); return Number.isFinite(num) ? num : null; })("
        + json.dumps(locator) + ", " + json.dumps(target) + ")"
    )


_SLIDER_MAX_KEYS = 200


def slider_fill(call, evaluate, action, text):
    """Move a slider to the target amount and verify by value.

    Native sliders get the native value setter first; everything else is
    trusted keyboard (click-to-position, then Home/End/PageUp/PageDown and
    arrows) with a read-back after each round. Bounded at 200 keys.
    """
    target = _parse_amount(text)
    slider = action.get("slider") or {}
    current = slider.get("now")
    if current is None:
        current = _parse_amount(slider.get("valuetext"))
    if target is None or not isinstance(current, (int, float)):
        return {"text_verified": False, "typed": current}
    minimum, maximum = slider.get("min"), slider.get("max")
    step = slider.get("step") or 1
    if not isinstance(step, (int, float)) or step <= 0:
        step = 1
    if isinstance(minimum, (int, float)):
        target = max(target, minimum)
    if isinstance(maximum, (int, float)):
        target = min(target, maximum)
    moves = 0

    def read():
        value = evaluate(slider_value_expression(action))
        return value if isinstance(value, (int, float)) else None

    def within(value):
        return abs(value - target) <= step / 2 + 1e-9

    def arrows(count):
        nonlocal moves
        name = "ArrowRight" if count > 0 else "ArrowLeft"
        for _ in range(min(abs(count), _SLIDER_MAX_KEYS - moves)):
            dispatch_key(call, name)
            moves += 1

    def plan(value, jumps_dead):
        need = round((target - value) / step)
        if jumps_dead:
            return None, need
        options = [(None, need)]
        if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
            options.append(("Home", round((target - minimum) / step)))
            options.append(("End", -round((maximum - target) / step)))
        return min(options, key=lambda option: (option[0] is not None) + abs(option[1]))

    native = evaluate(slider_set_native_expression(action, target))
    if isinstance(native, (int, float)):
        current = native
        if within(current):
            return {"text_verified": True, "typed": current, "slider_moves": 0}
    if (
        isinstance(minimum, (int, float))
        and isinstance(maximum, (int, float))
        and maximum > minimum
    ):
        geometry = evaluate(slider_geometry_expression(action)) or {}
        width = geometry.get("width") or 0
        if width > 0:
            fraction = max(0.0, min(1.0, (target - minimum) / (maximum - minimum)))
            x = geometry.get("x", 0) + fraction * width
            y = geometry.get("y", 0) + (geometry.get("height") or 0) / 2
            for event in ("mousePressed", "mouseReleased"):
                call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
            current = read()
    jumps_dead = False
    for _ in range(8):
        if current is None:
            break
        if within(current):
            return {"text_verified": True, "typed": current, "slider_moves": moves}
        if moves >= _SLIDER_MAX_KEYS:
            break
        jump, count = plan(current, jumps_dead)
        if jump is not None:
            _dispatch_commit_key(call, jump)
            moves += 1
        elif abs(count) > 10 and not jumps_dead:
            # A long way with no cheap jump: page keys first, then re-read;
            # their stride is widget-defined so arrows follow next round.
            _dispatch_commit_key(call, "PageUp" if count > 0 else "PageDown")
            moves += 1
            count = 0
        arrows(count)
        before, current = current, read()
        if current == before and (jump is not None or count == 0):
            jumps_dead = True
    verified = current is not None and within(current)
    return {"text_verified": verified, "typed": current, "slider_moves": moves}


def commit_date(call, evaluate, action, expected, previous):
    """Commit a typed date: when the field doesn't show it but the focused twin
    does, commit with Enter in keydown-beforeinput order and re-poll once."""
    order = _numeric_order(previous)
    if parse_date(expected, order) is None:
        return {}
    if _compare_values(expected, evaluate(field_value_expression(action)), order) in (
        "exact", "same_date",
    ):
        return {"date_committed": True}
    if _compare_values(expected, evaluate(twin_value_expression()), order) not in (
        "exact", "same_date",
    ):
        return {"date_committed": False}
    ordering = evaluate(commit_key_expression(action, "Enter"))
    if not isinstance(ordering, dict):
        ordering = {}
    time.sleep(0.6)
    read = evaluate(field_value_expression(action))
    committed = _compare_values(expected, read, order) in ("exact", "same_date")
    return {"date_committed": committed, "ordering": {"key": "Enter", **ordering}}


_TOKEN_STOPWORDS = frozenset("the a an to of and for on in with or by from".split())


def _stem(word):
    if word.endswith("s") and len(word) > 3 and not word.endswith("ss"):
        return word[:-1]
    return word


def _combo_words(text):
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return {word for word in re.findall(r"[a-z0-9]+", stripped) if len(word) > 1}


def _significant_tokens(text):
    """Stemmed content tokens: stopwords and bare numbers are not distinctive."""
    return {
        _stem(word)
        for word in _combo_words(text)
        if word not in _TOKEN_STOPWORDS and not word.isdigit()
    }


def _first_segment(label):
    return re.split(r",|·|\n| - ", label or "", maxsplit=1)[0].strip()


def _extra_tokens(first, typed, pool):
    """First-segment tokens the typed text and goal/field do not imply.

    A token is covered by exact/stem match, or when the typed text is a
    prefix fragment of it ("lisb" typed for "Lisbon").
    """
    typed_words = _combo_words(typed)
    allowed = _significant_tokens(typed) | set(pool)
    return {
        token
        for token in _significant_tokens(first)
        if token not in allowed and not any(token.startswith(word) for word in typed_words)
    }


def rank_combobox_options(typed, options, goal="", field=""):
    """Score listbox options by entity, best first. Stable: exact ties keep list order.

    Scoring uses the option's first segment (before any comma, middle dot,
    newline, or " - "): exact 3, prefix 2, all-typed-tokens 1, any-token 0.5.
    Extra distinctive tokens cost 0.5 each unless the typed text or goal
    implies them; rich rows gain 0.3 for secondary text and 0.5 more when it
    agrees with the goal or field on a type word.
    """
    target = (typed or "").strip().lower()
    words = _combo_words(typed)
    pool = _significant_tokens(f"{goal} {field}")
    typed_sig = _significant_tokens(typed)
    ranked = []
    for option in options or []:
        label = (option.get("label") or "").strip()
        if not label:
            continue
        low = _first_segment(label).lower()
        if target and low == target:
            score = 3.0
        elif target and low.startswith(target):
            score = 2.0
        elif words:
            held = _combo_words(_first_segment(label))
            if words <= held:
                score = 1.0
            elif words & held:
                score = 0.5
            else:
                continue
        else:
            continue
        extra = _extra_tokens(_first_segment(label), typed, pool)
        score -= 0.5 * len(extra)
        secondary = (option.get("secondary") or "").strip()
        if secondary:
            score += 0.3
            if _significant_tokens(secondary) & (pool - typed_sig):
                score += 0.5
        ranked.append((score, option))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def has_extra_tokens(value, typed, goal=""):
    """The committed value names a different entity than the typed text.

    Only the first segment counts: qualifier segments ("Lisbon, Portugal")
    and added years are benign expansion, not a wrong entity.
    """
    return bool(_extra_tokens(_first_segment(value), typed, _significant_tokens(goal)))


def match_combobox_option(typed, options, goal="", field="", tiebreak=None):
    """Best listbox option for typed text. A near-tie (top two within 0.25)
    goes to the tiebreak callback over up to 8 option texts; without one the
    first top score wins. No match returns None: a wrong commit is worse.
    """
    ranked = rank_combobox_options(typed, options, goal, field)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] <= 0.25 and tiebreak is not None:
        texts = [(option.get("label") or "") for _, option in ranked[:8]]
        try:
            pick = tiebreak(typed, texts, goal, field)
        except Exception as error:
            get_logger("browser").info(f"[jev:commit] tiebreak failed ({error}); keeping top score")
            pick = None
        if isinstance(pick, int) and not isinstance(pick, bool) and 0 <= pick < len(texts):
            return ranked[pick][1]
    return ranked[0][1]


def combo_state_expression(action):
    """One read: is this field a combobox, its expansion, and its value."""
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return (
        "/*__jevComboState*/(action => { const fast=window.__jevFast; "
        "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        "if (!e || !e.isConnected) return null; "
        "const list=(e.getAttribute('aria-controls')||e.getAttribute('aria-owns')||'')"
        ".split(/\\s+/).filter(Boolean); "
        "return {combo: e.getAttribute('role')==='combobox' || !!e.getAttribute('aria-autocomplete')"
        " || list.length>0, expanded: e.getAttribute('aria-expanded'), "
        "value: ('value' in e) ? String(e.value) : (e.innerText||'')}; })(" + json.dumps(locator) + ")"
    )


def combo_options_expression():
    """Visible listbox options with labels, secondary text, and centers. Capped at twelve."""
    return (
        "/*__jevComboOptions*/(() => { const out=[]; "
        "for (const box of document.querySelectorAll('[role=\"listbox\"]')) { "
        "const r=box.getBoundingClientRect(); if (!r.width || !r.height) continue; "
        "for (const o of box.querySelectorAll('[role=\"option\"]')) { "
        "const q=o.getBoundingClientRect(); "
        "if (!q.width || !q.height || q.bottom<=0 || q.top>=innerHeight) continue; "
        "const label=(o.innerText||'').split('\\n')[0].trim().slice(0,80); if (!label) continue; "
        "const kids=[...o.children].map(c=>(c.innerText||'').trim()).filter(Boolean); "
        "const secondary=kids.slice(1).join(' ').trim().slice(0,80); "
        "out.push({label, secondary, x:q.x+q.width/2, y:q.y+q.height/2}); "
        "if (out.length>=12) break; } } "
        "return out; })()"
    )


def combo_focus_expression(action):
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return (
        "/*__jevComboFocus*/(action => { const fast=window.__jevFast; "
        "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        "if (!e || !e.isConnected) return false; e.focus(); return true; })(" + json.dumps(locator) + ")"
    )


def _commit_ok(typed, final, previous, goal):
    """The committed value is the typed entity: semantically equal, no extra tokens.

    Dates compare by date (an added year is completion, not a wrong entity);
    other values must add no distinctive first-segment token the goal lacks.
    """
    if not isinstance(final, str) or not final or not (typed or "").strip():
        return False
    kind = _compare_values(typed, final, _numeric_order(previous))
    if kind is None:
        return False
    return kind == "same_date" or not has_extra_tokens(final, typed, goal)


def _read_combo_options(evaluate, rounds):
    for _ in range(rounds):
        options = [o for o in (evaluate(combo_options_expression()) or []) if isinstance(o, dict)]
        if options:
            return options
        time.sleep(0.1)
    return []


def _click_option(call, option):
    for event in ("mousePressed", "mouseReleased"):
        call("Input.dispatchMouseEvent", type=event, x=option["x"], y=option["y"],
             button="left", clickCount=1)


def commit_combobox(call, evaluate, action, typed, previous=None, goal="", tiebreak=None):
    """Commit a combobox value after typing. Plain fields return {}.

    An uncommitted combobox reverts on blur, so typing alone is not done: wait
    for the listbox and click the best-matching option. With no listbox the
    typed value is kept as-is (Enter only submits a non-empty search box);
    ArrowDown and Tab would clear the field. Blur once to detect a revert,
    then retry a single time with the next-best option. Each stage is a
    distinct strategy, never a repeated mutation.
    """
    if action.get("role") not in {"combobox", "textbox", "searchbox"}:
        return {}
    state = evaluate(combo_state_expression(action))
    if not isinstance(state, dict):
        return {"commit": "stale", "committed": False}
    field = action.get("label") or ""
    options = []
    if state.get("combo"):
        options = _read_combo_options(evaluate, 6)
    if not options and state.get("combo"):
        # No listbox: keep the typed value. Only a search box is submitted,
        # and only when non-empty; Tab would clear an uncommitted field.
        value = evaluate(field_value_expression(action))
        if value and action.get("role") == "searchbox":
            dispatch_key(call, "Enter")
        return {
            "commit": "keep-typed",
            "picked": None,
            "committed": _commit_ok(typed, value, previous, goal),
            "committed_value": value,
            "reverted": False,
            "retried": False,
        }
    if not options:
        return {}
    commit, picked = None, None
    picked = match_combobox_option(typed, options, goal, field, tiebreak)
    if picked is not None:
        _click_option(call, picked)
        commit = "option-click"
    else:
        commit = "no-match"
    time.sleep(0.15)
    first = evaluate(field_value_expression(action))
    dispatch_key(call, "Tab")
    time.sleep(0.15)
    final = evaluate(field_value_expression(action))
    reverted = first != previous and final == previous and first != final
    committed = _commit_ok(typed, final, previous, goal)
    retried = False
    if not committed:
        # One retry with the next-best option: refocus, clear the rejected
        # value so the reopened list is unfiltered, click another match.
        # A lone option is re-clicked: the first click may simply have missed.
        retried = True
        rejected = {(picked or {}).get("label")}
        if evaluate(combo_focus_expression(action)):
            modifier = 4 if sys.platform == "darwin" else 2
            call(
                "Input.dispatchKeyEvent",
                type="keyDown", key="a", code="KeyA", modifiers=modifier,
                commands=["selectAll"],
            )
            call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=modifier)
            dispatch_key(call, "Backspace")
            dispatch_key(call, "ArrowDown")
            reopened = _read_combo_options(evaluate, 4)
            ranked = rank_combobox_options(typed, reopened, goal, field)
            retry_pick = next(
                (option for _, option in ranked if option.get("label") not in rejected), None
            )
            if retry_pick is None:
                retry_pick = next((option for _, option in ranked), None)
            if retry_pick is not None:
                picked = retry_pick
                _click_option(call, picked)
                commit = "option-click"
                time.sleep(0.15)
                final = evaluate(field_value_expression(action))
                committed = _commit_ok(typed, final, previous, goal)
    return {
        "commit": commit or "none",
        "picked": (picked or {}).get("label"),
        "committed": bool(committed),
        "committed_value": final,
        "reverted": bool(reverted),
        "retried": retried,
    }


def _upload_key_ok(key):
    return isinstance(key, str) and bool(key) and "/" not in key and "\\" not in key and ".." not in key


def bind_uploads(state, files):
    """Offer one target per allowlist key. Paths stay out of the action list."""
    actions = state.get("actions") or []
    if not any(action.get("kind") == "upload" for action in actions):
        return state
    allowed = {key: path for key, path in (files or {}).items() if _upload_key_ok(key)}
    bound = []
    for action in actions:
        if action.get("kind") != "upload":
            bound.append(action)
            continue
        for key in allowed:
            bound.append({
                **{k: v for k, v in action.items() if k not in {"path", "filename"}},
                "id": f"{action['id']}:{key}",
                "upload_key": key,
                "label": f"{action.get('label') or 'Choose file'} → {key}",
                "value": "",
            })
    updated = {**state, "actions": bound}
    updated["fingerprint"] = fingerprint(updated)
    return updated


def real_upload_path(files, key):
    """Resolve an allowlisted key to a real file. The model never supplies this path."""
    if not _upload_key_ok(key) or not isinstance(files, dict) or key not in files:
        raise ValueError("Upload key is not allowlisted; no file was set.")
    raw = files[key]
    if not isinstance(raw, str) or not os.path.isabs(raw):
        raise ValueError("Allowlisted upload is not an absolute path; no file was set.")
    path = os.path.realpath(raw)
    if not os.path.isfile(path):
        raise ValueError("Allowlisted upload is not a file; no file was set.")
    return path


def merge_oopif_state(parent, children):
    """Offset cross-origin frame actions into the top viewport and cap the combined list."""
    element = [action for action in parent.get("actions", []) if action.get("kind") not in {"scroll", "wait"}]
    tail = [action for action in parent.get("actions", []) if action.get("kind") in {"scroll", "wait"}]
    guards = dict(parent.get("guards") or {})
    frame_keys = {}
    width = parent.get("w") or 1120
    height = parent.get("h") or 780
    for child in children:
        frame_keys[child["id"]] = child["state"].get("page_key")
        origin = child["origin"]
        for action in child["state"].get("actions") or []:
            if action.get("kind") in {"scroll", "wait"}:
                continue
            rect = action.get("rect")
            if rect:
                moved = {
                    "x": rect["x"] + origin["x"],
                    "y": rect["y"] + origin["y"],
                    "w": rect["w"],
                    "h": rect["h"],
                }
                center_x = moved["x"] + moved["w"] / 2
                center_y = moved["y"] + moved["h"] / 2
                if center_x < 0 or center_y < 0 or center_x >= width or center_y >= height:
                    continue
                action = {**action, "rect": moved}
            action = {
                **action,
                "route": child["id"],
                "oopif": True,
                "id": f"{child['id']}:{action['id']}",
            }
            element.append(action)
        for key, guard in (child["state"].get("guards") or {}).items():
            guards[f"{child['id']}:{key}"] = guard
    omitted = max(0, len(element) - 250) + parent.get("omitted_actions", 0)
    merged = {
        **parent,
        "actions": element[:250] + tail,
        "guards": guards,
        "frame_keys": frame_keys,
        "omitted_actions": omitted,
    }
    return merged


def match_iframe_targets(frames, target_infos):
    """Pair code-owned frame ids with CDP iframe targets. URLs are matched, never chosen by the model."""
    pool = [info for info in target_infos if info.get("type") == "iframe"]
    used = set()
    matched = []
    for frame in frames:
        url = frame.get("url") or ""
        src = frame.get("src") or ""
        found = None
        for target in pool:
            target_id = target.get("targetId")
            if not target_id or target_id in used:
                continue
            target_url = target.get("url") or ""
            if url and target_url == url:
                found = target
                break
            if src and target_url.endswith(src):
                found = target
                break
        if found:
            used.add(found["targetId"])
            matched.append((frame, found))
    return matched


def action_freshness(page, action):
    """The guard pair a cloud probe compares against the live page."""
    node = action.get("node")
    if type(node) is not int:
        return None
    frame = action.get("frame") or "f0"
    if action.get("route"):
        return [
            page.get("frame_keys", {}).get(action["route"]),
            page.get("guards", {}).get(f"{action['route']}:{frame}:{node}"),
        ]
    return [page.get("page_key"), page.get("guards", {}).get(f"{frame}:{node}")]


def action_probe_expression(action, expected):
    """Guard, geometry, and the previous value in one call. Select still mutates here."""
    payload = json.dumps({
        "action": {key: action[key] for key in ("node", "frame", "kind", "value") if key in action},
        "expected": expected,
    })
    return """(payload => {
      const action=payload.action, expected=payload.expected;
      const c=window.__jevFast;
      if (!c || !c.guard) return {fresh:false};
      const win=(c.frameWins&&c.frameWins[action.frame])||window;
      const local=win.__jevFast;
      if (!local || !local.guard) return {fresh:false};
      const e=local.nodes.get(action.node);
      const current=[c.pageKey ? c.pageKey() : null, local.guard(e)];
      if (JSON.stringify(current)!==JSON.stringify(expected)) return {fresh:false};
      if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
          !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return {fresh:true,target:null};
      if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) {
        return {fresh:true,target:null};
      }
      const point=window.__jevFast?.toTop ? window.__jevFast.toTop(e) : null;
      const r=e.getBoundingClientRect(), x=point?point.x:r.x+r.width/2, y=point?point.y:r.y+r.height/2;
      if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) {
        return {fresh:true,target:null};
      }
      const hit=window.__jevFast?.hit ? window.__jevFast.hit(x,y) : document.elementFromPoint(x,y);
      const covered=window.__jevFast?.owns ? !window.__jevFast.owns(e,hit) : !e.contains(hit);
      if (covered) return {fresh:true,target:null,covered:true};
      if (action.kind==='select') {
        if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
            !o.disabled && !o.closest('optgroup[disabled]'))) return {fresh:true,target:null};
        e.value=action.value;
        e.dispatchEvent(new Event('input',{bubbles:true}));
        e.dispatchEvent(new Event('change',{bubbles:true}));
      }
      const previous = action.kind==='fill'
        ? (e.isContentEditable ? e.innerText : ('value' in e ? String(e.value) : ''))
        : null;
      return {fresh:true,target:{x,y},previous};
    })(""" + payload + ")"


def scroll_into_view_expression(action):
    return (
        "(action => { const fast=window.__jevFast; "
        "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        "if (!e?.isConnected) return false; "
        "e.scrollIntoView({block:'center',behavior:'instant'}); return true; })("
        + json.dumps(action)
        + ")"
    )


def link_href_expression(action):
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return (
        "(action => { const fast=window.__jevFast; "
        "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        "if (!e || e.tagName!=='A' || !e.href) return null; return e.href; })("
        + json.dumps(locator)
        + ")"
    )


def covered_link_fallback(call, evaluate, action):
    """A covered link still has its href: navigate there instead of clicking."""
    if action.get("role") == "link":
        href = evaluate(link_href_expression(action))
        if href:
            try:
                target = validate_navigate_url(href)
            except ValueError:
                target = None
            if target:
                call("Page.navigate", url=target)
                return {"executed": action["id"], "covered_navigate": True, "url": target}
    raise CoveredTarget("Target is covered by another element. Observe again.")


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]
    counter = request.get("counter")

    def call(method, **params):
        started = time.perf_counter()
        try:
            return cdp(method, session_id=session, **params)
        finally:
            if counter is not None:
                counter["calls"] += 1
                counter["ms"] += (time.perf_counter() - started) * 1000

    def evaluate(expression, await_promise=False):
        result = call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
        )
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        extra = {}
        if kind == "scroll" and type(action.get("node")) is int:
            axis = "x" if action.get("axis") == "x" else "y"
            delta = int(action.get("delta") or 0)
            locator = {k: action[k] for k in ("node", "frame") if k in action}
            before = evaluate(
                "(action => { const fast=window.__jevFast; "
                "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); if (!e) return null; "
                "const point=fast.toTop ? fast.toTop(e) : null; const r=e.getBoundingClientRect(); "
                "return {x:point?point.x:r.x+r.width/2, y:point?point.y:r.y+r.height/2, "
                "before: action.axis==='x' ? e.scrollLeft : e.scrollTop}; })("
                + json.dumps({**locator, "axis": axis})
                + ")"
            )
            if not before:
                raise StalePage("Scroll container changed. Observe again.")
            call(
                "Input.dispatchMouseEvent",
                type="mouseWheel",
                x=before["x"],
                y=before["y"],
                deltaX=delta if axis == "x" else 0,
                deltaY=delta if axis == "y" else 0,
            )
            after = evaluate(
                "(action => { const fast=window.__jevFast; "
                "const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); if (!e) return null; "
                "const value=action.axis==='x' ? e.scrollLeft : e.scrollTop; "
                "if (value===action.before) "
                "e.scrollBy(action.axis==='x'?action.delta:0, action.axis==='y'?action.delta:0); "
                "const now=action.axis==='x' ? e.scrollLeft : e.scrollTop; "
                "return {after:now, fallback:value===action.before}; })("
                + json.dumps({**locator, "axis": axis, "before": before["before"], "delta": delta})
                + ")"
            )
            return {"executed": action["id"], "scrolled": bool(after and after.get("after") != before["before"])}
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind == "upload":
            if type(action.get("node")) is not int:
                raise ValueError("Invalid observed node")
            # The path comes only from the allowlist. Action fields cannot change it.
            path = real_upload_path(request.get("files") or {}, action.get("upload_key"))
            locator = {k: action[k] for k in ("node", "frame", "kind") if k in action}
            located = call(
                "Runtime.evaluate",
                expression="""(action => {
                  const fast=window.__jevFast;
                  const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node);
                  if (!e || e.tagName!=='INPUT' || e.type!=='file' || !e.isConnected) return null;
                  return e;
                })(""" + json.dumps(locator) + ")",
                returnByValue=False,
            )
            object_id = (located.get("result") or {}).get("objectId")
            if not object_id:
                raise StalePage("File input changed. Observe again.")
            try:
                call("DOM.setFileInputFiles", files=[path], objectId=object_id)
            finally:
                try:
                    call("Runtime.releaseObject", objectId=object_id)
                except RuntimeError:
                    pass
        elif kind == "key":
            dispatch_key(call, action.get("key"))
        elif kind == "check":
            return act_check(call, evaluate, action)
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            previous = None
            if action.get("offscreen"):
                # Pager below the fold: scroll it into view, then resolve fresh.
                if not evaluate(scroll_into_view_expression(action)):
                    raise StalePage("Target changed or is covered. Observe again.")
            probe = request.get("probe") if kind in {"click", "fill", "select"} else None

            def resolve_target():
                if probe:
                    probed = evaluate(action_probe_expression(action, probe))
                    if not probed or not probed.get("fresh"):
                        raise StalePage("Page changed since this decision. Observe again.")
                    return probed.get("target"), bool(probed.get("covered")), probed.get("previous")
                # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
                target = evaluate("""(action => {
                  const fast=window.__jevFast;
                  const e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node);
                  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
                  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
                  const point=window.__jevFast?.toTop ? window.__jevFast.toTop(e) : null;
                  const r=e.getBoundingClientRect(), x=point?point.x:r.x+r.width/2, y=point?point.y:r.y+r.height/2;
                  if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
                  const hit=window.__jevFast?.hit ? window.__jevFast.hit(x,y) : document.elementFromPoint(x,y);
                  const covered=window.__jevFast?.owns ? !window.__jevFast.owns(e,hit) : !e.contains(hit);
                  if (covered) return {covered:true};
                  if (action.kind==='select') {
                    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                    e.value=action.value;
                    e.dispatchEvent(new Event('input',{bubbles:true}));
                    e.dispatchEvent(new Event('change',{bubbles:true}));
                  }
                  return {x,y};
                })(""" + json.dumps(action) + ")")
                if isinstance(target, dict) and target.get("covered"):
                    return None, True, None
                return target, False, None

            target, covered, probed_previous = resolve_target()
            if kind == "fill" and probe is not None:
                previous = probed_previous
            if covered and evaluate(scroll_into_view_expression(action)):
                # Covered is paint, not staleness: center it and retry once.
                target, covered, probed_previous = resolve_target()
                if kind == "fill" and probe is not None:
                    previous = probed_previous
            if covered:
                return covered_link_fallback(call, evaluate, action)
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    if probe is None:
                        previous = (
                            evaluate(field_value_expression(action))
                            if request.get("keyboard", True)
                            else None
                        )
                    if detect_slider(action) and request.get("keyboard", True):
                        extra.update(
                            slider_fill(
                                call, evaluate, action, request.get("text") or ""
                            )
                        )
                    else:
                        call(
                            "Input.dispatchKeyEvent",
                            type="keyDown",
                            key="a",
                            code="KeyA",
                            modifiers=4 if sys.platform == "darwin" else 2,
                            commands=["selectAll"],
                        )
                        call(
                            "Input.dispatchKeyEvent",
                            type="keyUp",
                            key="a",
                            code="KeyA",
                            modifiers=4 if sys.platform == "darwin" else 2,
                        )
                        call("Input.insertText", text=request["text"])
                    if request.get("keyboard", True) and not detect_slider(action):
                        extra.update(
                            verify_typed_text(
                                call,
                                evaluate,
                                action,
                                request.get("text") or "",
                                previous,
                                wait_ready=bool(request.get("verify_wait")),
                            )
                        )
                        extra.update(
                            commit_date(
                                call, evaluate, action, request.get("text") or "",
                                previous,
                            )
                        )
                        extra.update(
                            commit_combobox(
                                call, evaluate, action, request.get("text") or "",
                                previous, request.get("goal") or "",
                                request.get("tiebreak"),
                            )
                        )
        return {"executed": action["id"], **extra}

    info = evaluate(snapshot_expression(request.get("config")))
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    info["captcha_reason"] = detect_captcha(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
