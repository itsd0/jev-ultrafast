"""Nested scroll containers are capped and wheeled at their center."""

import subprocess
from pathlib import Path

from jev_ultrafast.browser import browser_operation
from jev_ultrafast.config import resolve_config
from jev_ultrafast.model import action_space

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"

NODE = r"""
const fs = require("fs");
const vm = require("vm");
const context = {globalThis: {}, console};
context.globalThis = context;
const api = vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const candidates = [];
for (let i = 0; i < 9; i += 1) {
  candidates.push({
    node: i + 1, frame: "f0", vertical: true, horizontal: i === 8, area: i + 1,
    clientHeight: 100, clientWidth: 50, label: "box " + i,
  });
}
const actions = api.scrollTargets(candidates);
const nodes = [...new Set(actions.map(action => action.node))];
if (nodes.length !== 8) throw new Error("cap failed: " + nodes.length);
if (!nodes.includes(9) || nodes.includes(1)) throw new Error("area ranking dropped the wrong container");
const down = actions.filter(action => action.label.startsWith("Scroll down"));
if (down[0].delta !== 80) throw new Error("delta was not 0.8 * clientHeight");
const first = actions.filter(action => action.node === 9);
if (!first.some(action => action.axis === "x" && action.delta === 40)) {
  throw new Error("horizontal target missing");
}
if (!first.some(action => action.label.startsWith("Scroll left") && action.delta === -40)) {
  throw new Error("left target missing");
}
console.log("ok");
"""


def test_scroll_targets_keep_eight_largest_containers():
    completed = subprocess.run(["node", "-e", NODE, str(SNAPSHOT)], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_document_and_container_scroll_share_an_operation():
    actions = [
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
        {"id": "scroll:f0:4:1", "kind": "scroll", "label": "Scroll down box", "delta": 80,
         "node": 4, "frame": "f0", "axis": "y"},
        {"id": "scroll:f0:4:2", "kind": "scroll", "label": "Scroll up box", "delta": -80,
         "node": 4, "frame": "f0", "axis": "y"},
    ]
    _elements, targets, controls = action_space(actions)
    assert set(targets["SCROLL_DOWN"]) == {"scroll_down", "scroll:f0:4:1"}
    assert set(targets["SCROLL_UP"]) == {"scroll:f0:4:2"}
    assert "SCROLL_DOWN" not in controls


def test_container_wheel_uses_its_center_and_can_fall_back(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        if method == "Runtime.evaluate":
            if sum(item[0] == "Runtime.evaluate" for item in calls) == 1:
                return {"result": {"value": {"x": 12, "y": 34, "before": 0}}}
            return {"result": {"value": {"after": 80, "fallback": True}}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    result = browser_operation({
        "operation": "act",
        "session": "s",
        "action": {"id": "scroll:f0:4:1", "kind": "scroll", "node": 4, "frame": "f0", "axis": "y", "delta": 80},
    })
    wheel = next(params for method, params in calls if method == "Input.dispatchMouseEvent")
    assert (wheel["x"], wheel["y"], wheel["deltaY"]) == (12, 34, 80)
    assert wheel["deltaX"] == 0
    fallback = [params["expression"] for method, params in calls if method == "Runtime.evaluate"][1]
    assert "scrollBy" in fallback
    assert result["scrolled"] is True


def test_nested_scroll_defaults_on(monkeypatch):
    monkeypatch.delenv("JEV_NESTED_SCROLL", raising=False)
    assert resolve_config()["nested_scroll"] is True
    monkeypatch.setenv("JEV_NESTED_SCROLL", "0")
    assert resolve_config()["nested_scroll"] is False
    assert resolve_config(nested_scroll=True)["nested_scroll"] is True
