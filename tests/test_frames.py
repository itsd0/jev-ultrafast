"""Frame namespacing and cross-origin routing. No browser and no network."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

from jev_ultrafast.browser import Browser, fingerprint, match_iframe_targets, merge_oopif_state
from jev_ultrafast.config import resolve_config

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "jev_ultrafast" / "snapshot.js"

NODE = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[1], "utf8");
const context = {globalThis: {}, console};
context.globalThis = context;
const api = vm.runInNewContext(src, context);

function element(tag, children) {
  const node = {
    tagName: tag.toUpperCase(), children: children || [], shadowRoot: null,
    parentElement: null, parentNode: null, src: "",
  };
  for (const child of node.children) {
    child.parentElement = node;
    child.parentNode = node;
  }
  node.querySelectorAll = function query(sel) {
    const found = [];
    (function walk(current) {
      for (const child of current.children || []) {
        found.push(child);
        walk(child);
      }
    })(node);
    if (sel === "*") return found;
    return found.filter(item => item.tagName === String(sel).toUpperCase());
  };
  node.getAttribute = () => node.src;
  node.getBoundingClientRect = () => ({x: 0, y: 0, width: 10, height: 10});
  return node;
}

const button = element("button");
button.getBoundingClientRect = () => ({x: 4, y: 6, width: 20, height: 10});
const child = element("body", [button]);
child.body = child;
const iframe = element("iframe");
iframe.src = "https://frames.test/child";
iframe.getBoundingClientRect = () => ({x: 30, y: 40, width: 200, height: 100});
iframe.contentDocument = child;
const offscreen = element("iframe");
offscreen.src = "https://frames.test/off";
offscreen.getBoundingClientRect = () => ({x: 0, y: 900, width: 50, height: 40});
offscreen.contentDocument = element("body");
offscreen.contentDocument.body = offscreen.contentDocument;
const cross = element("iframe");
cross.src = "https://frames.test/cross";
cross.getBoundingClientRect = () => ({x: 8, y: 8, width: 80, height: 60});
Object.defineProperty(cross, "contentDocument", {get() { throw new Error("cross-origin"); }});
const doc = element("body", [iframe, offscreen, cross]);
context.globalThis.__jevCfg = {shadow: false, frames: "same"};
let serial = 0;
const entries = api.frameEntries(doc, {x: 0, y: 0}, {w: 800, h: 600}, () => "f" + (++serial));
if (entries.length !== 2) throw new Error("expected same-origin and cross-origin frames, got " + entries.length);
if (entries[0].id !== "f1" || !entries[0].same || entries[0].rect.x !== 30 || entries[0].rect.y !== 40) {
  throw new Error("same-origin frame was not offset");
}
if (entries[1].same || entries[1].url !== "https://frames.test/cross") {
  throw new Error("cross-origin frame was inlined");
}
context.globalThis.__jevCfg = {frames: "off"};
if (api.frameEntries(doc, {x: 0, y: 0}, {w: 800, h: 600}, () => "f9").length !== 0) {
  throw new Error("frames=off still listed iframes");
}
const frameHit = element("iframe");
frameHit.getBoundingClientRect = () => ({x: 10, y: 20, width: 100, height: 50});
const inner = element("button");
const frameDoc = element("body", [inner]);
frameDoc.elementFromPoint = () => inner;
frameHit.contentDocument = frameDoc;
const page = element("body", [frameHit]);
page.elementFromPoint = () => frameHit;
context.globalThis.__jevCfg = {shadow: false, frames: "same"};
if (api.resolveHit(page, 15, 25) !== inner) throw new Error("hit-test did not enter the frame");
context.globalThis.__jevCfg = {frames: "off"};
if (api.resolveHit(page, 15, 25) !== frameHit) throw new Error("frames=off stepped into a frame");
console.log("ok");
"""


def test_frame_entries_namespace_offset_and_skip_offscreen():
    completed = subprocess.run(["node", "-e", NODE, str(SNAPSHOT)], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_frames_flag_defaults_to_same(monkeypatch):
    monkeypatch.delenv("JEV_FRAMES", raising=False)
    assert resolve_config()["frames"] == "same"
    monkeypatch.setenv("JEV_FRAMES", "all")
    assert resolve_config()["frames"] == "all"
    assert resolve_config(frames="off")["frames"] == "off"
    monkeypatch.setenv("JEV_FRAMES", "nope")
    assert resolve_config()["frames"] == "same"


def test_merge_prefixes_frame_ids_and_offsets_rects():
    parent = {
        "url": "https://example.test/",
        "text": "top",
        "w": 800,
        "h": 600,
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "click", "node": 1, "frame": "f0", "rect": {"x": 1, "y": 1, "w": 2, "h": 2}},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
        "guards": {"f0:1": [1]},
        "omitted_actions": 0,
    }
    child = {
        "actions": [
            {"id": "e1", "kind": "click", "node": 7, "frame": "f0", "label": "Inside",
             "rect": {"x": 3, "y": 4, "w": 5, "h": 6}},
        ],
        "guards": {"f0:7": ["guard"]},
        "page_key": ["child-key"],
    }
    merged = merge_oopif_state(parent, [{"id": "f2", "origin": {"x": 10, "y": 20}, "state": child}])
    routed = merged["actions"][1]
    assert routed["id"] == "f2:e1"
    assert routed["route"] == "f2"
    assert routed["node"] == 7
    assert routed["rect"] == {"x": 13, "y": 24, "w": 5, "h": 6}
    assert merged["actions"][-1]["kind"] == "wait"
    assert merged["frame_keys"] == {"f2": ["child-key"]}
    assert merged["guards"]["f2:f0:7"] == ["guard"]
    assert "selector" not in json.dumps(routed)


def test_merge_keeps_the_global_action_cap():
    parent = {
        "w": 100, "h": 100, "actions": [
            {"id": f"e{i}", "kind": "click", "node": i, "frame": "f0"} for i in range(250)
        ],
        "guards": {}, "omitted_actions": 0,
    }
    child = {"actions": [{"id": "e1", "kind": "click", "node": 1, "frame": "f0"}], "guards": {}, "page_key": []}
    merged = merge_oopif_state(parent, [{"id": "f1", "origin": {"x": 0, "y": 0}, "state": child}])
    assert len([a for a in merged["actions"] if a["kind"] == "click"]) == 250
    assert merged["omitted_actions"] == 1


def test_iframe_targets_match_by_url_not_by_model_text():
    frames = [{"id": "f2", "url": "https://cdn.test/widget", "src": "/widget"}]
    targets = [
        {"type": "page", "targetId": "page", "url": "https://cdn.test/widget"},
        {"type": "iframe", "targetId": "frame-9", "url": "https://cdn.test/widget"},
    ]
    matched = match_iframe_targets(frames, targets)
    assert matched == [(frames[0], targets[1])]


def test_oopif_click_routes_to_the_child_session(monkeypatch):
    tab = Browser.__new__(Browser)
    tab.session = "top"
    tab.frame_sessions = {"f2": "child-session"}
    tab.fresh = Mock(return_value=True)
    tab.after_input = None
    seen = {}

    def operate(request):
        seen["session"] = request["session"]
        return {"executed": request["action"]["id"]}

    monkeypatch.setattr("jev_ultrafast.browser.browser_operation", operate)
    action = {"id": "f2:e1", "kind": "click", "node": 7, "frame": "f0", "route": "f2", "oopif": True}
    page = {"actions": [action], "fingerprint": "x", "page_key": [], "guards": {}, "marker": []}
    tab.act(action, page)
    assert seen["session"] == "child-session"
    same = {"id": "e1", "kind": "click", "node": 1, "frame": "f0"}
    tab.act(same, page)
    assert seen["session"] == "top"


def test_frame_keys_change_the_fingerprint():
    page = {"url": "u", "text": "t", "actions": [], "scroll": {"y": 0}}
    other = {**page, "frame_keys": {"f2": ["a"]}}
    assert fingerprint(page) != fingerprint(other)
