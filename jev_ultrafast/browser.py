"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from .config import resolve_config, snapshot_config

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
        ensure_daemon()
        visible = os.environ.get("JEV_VISIBLE_TAB", "").strip().lower() in {"1", "true", "yes", "on"}
        self.target = cdp("Target.createTarget", url="about:blank", background=not visible)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        if visible:
            self.call("Page.bringToFront")
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

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
        return bind_uploads(state, self.config.get("files") or {})

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

    def wait_until_ready(self, timeout=10):
        """Bounded wait for document completion. Returns immediately if already ready."""
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

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        session = self.session
        if action.get("route"):
            session = self.frame_sessions.get(action["route"])
            if not session:
                raise StalePage("Frame is gone. Observe again.")
        result = browser_operation({
            "operation": "act",
            "session": session,
            "action": action,
            "text": text,
            "files": (getattr(self, "config", None) or {}).get("files") or {},
        })
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll") if k in state}
    if state.get("frame_keys"):
        content["frame_keys"] = state["frame_keys"]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


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


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
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
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
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
        return {"executed": action["id"]}

    info = evaluate(snapshot_expression(request.get("config")))
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
