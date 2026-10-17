"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp, drain_events

from . import cloud
from .config import env_bool, resolve_config, snapshot_config

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()


def snapshot_expression(config=None):
    payload = json.dumps(snapshot_config(config or resolve_config()))
    return f"(()=>{{globalThis.__jevCfg={payload};return ({READ_STATE});}})()"


def marker_expression(config=None):
    return f"(() => {{ const state={snapshot_expression(config)}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


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
        cloud_mode = self.config.get("browser") == "cloud"
        if cloud_mode:
            # The cloud daemon is already attached. ensure_daemon() would spawn a
            # local Chrome daemon if the cloud one died.
            self.cloud = cloud.start(self.config)
        else:
            ensure_daemon()
        try:
            visible_env = os.environ.get("JEV_VISIBLE_TAB")
            if visible_env is None or not visible_env.strip():
                visible = cloud_mode
            else:
                visible = visible_env.strip().lower() in {"1", "true", "yes", "on"}
            self.target = cdp(
                "Target.createTarget", url="about:blank", background=not visible, newWindow=False
            )["targetId"]
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
        return cdp(method, session_id=self.session, **params)

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
            print(
                f"[jev:cloud] viewport mismatch: got {retry.get('w')}x{retry.get('h')} "
                f"dpr={retry.get('dpr')}, expected {expected['w']}x{expected['h']} dpr={expected['dpr']}",
                file=sys.stderr,
                flush=True,
            )
        return retry

    def vision_click(self, x, y):
        """Coordinate input. This is the only path that does not use a code-owned node id."""
        print(
            f"[jev:vision] coordinate-based click x={x:.1f} y={y:.1f} "
            "breaks the node-id invariant",
            file=sys.stderr,
            flush=True,
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

    def evaluate(self, expression, session=None):
        response = cdp(
            "Runtime.evaluate",
            session_id=session or self.session,
            expression=expression,
            returnByValue=True,
        )
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
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
        for attempt in range(10):
            try:
                state = browser_operation(
                    {
                        "operation": "observe",
                        "session": self.session,
                        "screenshot": screenshot,
                        "config": self.config,
                    }
                )
                return self._finish_observation(state)
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
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
                    {"operation": "observe", "session": session, "screenshot": False, "config": child_config}
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

    def _guard_expression(self, action):
        frame = json.dumps(action.get("frame") or "f0")
        node = int(action["node"])
        return (
            "(() => { const c=window.__jevFast; if (!c||!c.guard) return null; "
            f"const win=(c.frameWins&&c.frameWins[{frame}])||window; const local=win.__jevFast; "
            f"if (!local||!local.guard) return null; "
            f"return [c.pageKey(), local.guard(local.nodes.get({node}))]; }})()"
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
        if not self._oopif_keys_fresh(page):
            return False
        if action is not None and action.get("route"):
            node = action.get("node")
            if type(node) is not int:
                return False
            session = self.frame_sessions.get(action["route"])
            if not session:
                return False
            current = self.evaluate(self._guard_expression(action), session)
            guard_key = f"{action['route']}:{action.get('frame') or 'f0'}:{node}"
            return current == [page.get("frame_keys", {}).get(action["route"]), page.get("guards", {}).get(guard_key)]
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(self._guard_expression(action))
            guard_key = f"{action.get('frame') or 'f0'}:{node}"
            return current == [page["page_key"], page["guards"].get(guard_key)]
        return self.evaluate(marker_expression(self.config)) == page["marker"]

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
        """Model-directed first navigation. The URL gate fails closed."""
        target = validate_navigate_url(url)
        self.call("Page.navigate", url=target)
        self.wait_until_ready()
        self.after_input = None
        return {"executed": "navigate", "url": target}

    def act(self, action, page, text=None):
        cloud_mode = (getattr(self, "config", None) or {}).get("browser") == "cloud"
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
            "verify_active": cloud_mode,
            "verify_wait": cloud_mode,
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
    """Offer one NAVIGATE control on a blank page. The text model names the URL."""
    if (state.get("url") or "").strip() not in {"about:blank", ""}:
        return state
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


def bind_keys(state, enabled):
    if not enabled:
        return state
    actions = [action for action in state.get("actions", []) if action.get("kind") != "key"]
    for name in PRESS_KEYS:
        actions.append({"id": f"key:{name}", "kind": "key", "key": name, "label": f"Press {name}"})
    updated = {**state, "actions": actions}
    updated["fingerprint"] = fingerprint(updated)
    return updated


def _field_value_reader(fallback=False):
    active = ""
    if fallback:
        # A click can make the page replace the focused control. Read the live
        # replacement instead of the disconnected observed node.
        active = (
            "if (e && !e.isConnected && document.activeElement) { const a=document.activeElement; "
            "if (a.isContentEditable || a.tagName==='INPUT' || a.tagName==='TEXTAREA') e=a; } "
        )
    return (
        "(action => { const fast=window.__jevFast; "
        "let e=fast?.resolve ? fast.resolve(action) : fast?.nodes.get(action.node); "
        + active
        + "if (!e) return null; "
        "return e.isContentEditable ? e.innerText : ('value' in e ? String(e.value) : ''); })"
    )


def field_value_expression(action, fallback=False):
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return _field_value_reader(fallback) + "(" + json.dumps(locator) + ")"


def ready_value_expression(action, fallback=False):
    """One read for the post-input value and readyState, so a ready page skips another call."""
    locator = {k: action[k] for k in ("node", "frame") if k in action}
    return (
        "(action => { const read=" + _field_value_reader(fallback) + "; "
        "return {ready: document.readyState === 'complete', value: read(action)}; })("
        + json.dumps(locator)
        + ")"
    )


def verify_typed_text(
    call,
    evaluate,
    action,
    expected,
    previous=None,
    *,
    fallback=False,
    wait_ready=False,
):
    """Read the field after insertText. Retype once only if the value did not change.

    Comboboxes rewrite the inserted text (autocomplete, accents). Clearing that
    result drops the suggestion list. Keyboard-only fields leave the value
    untouched, and those are the ones that need key events.
    """
    expression = field_value_expression(action, fallback)
    ready = None
    if wait_ready:
        combined = evaluate(ready_value_expression(action, fallback))
        if isinstance(combined, dict):
            actual, ready = combined.get("value"), bool(combined.get("ready"))
        else:
            actual = combined
    else:
        actual = evaluate(expression)
    result = _verify_value(call, evaluate, action, expected, previous, actual, fallback)
    if ready is not None:
        result["ready"] = ready
    return result


def _verify_value(call, evaluate, action, expected, previous, actual, fallback):
    expression = field_value_expression(action, fallback)
    if actual == expected:
        return {"text_verified": True, "typed": actual}
    if actual not in (None, "") and actual != previous:
        return {"text_verified": True, "typed": actual, "rewritten": True}
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
    return {"text_verified": actual == expected, "typed": actual}


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
      if (covered) return {fresh:true,target:null};
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


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

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
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            previous = None
            probe = request.get("probe") if kind in {"click", "fill", "select"} else None
            if probe:
                probed = evaluate(action_probe_expression(action, probe))
                if not probed or not probed.get("fresh"):
                    raise StalePage("Page changed since this decision. Observe again.")
                target = probed.get("target")
                if kind == "fill":
                    previous = probed.get("previous")
            else:
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
                  if (covered) return null;
                  if (action.kind==='select') {
                    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                    e.value=action.value;
                    e.dispatchEvent(new Event('input',{bubbles:true}));
                    e.dispatchEvent(new Event('change',{bubbles:true}));
                  }
                  return {x,y};
                })(""" + json.dumps(action) + ")")
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
                    if request.get("keyboard", True):
                        extra.update(
                            verify_typed_text(
                                call,
                                evaluate,
                                action,
                                request.get("text") or "",
                                previous,
                                fallback=bool(request.get("verify_active")),
                                wait_ready=bool(request.get("verify_wait")),
                            )
                        )
        return {"executed": action["id"], **extra}

    info = evaluate(snapshot_expression(request.get("config")))
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
