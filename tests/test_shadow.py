"""Open-shadow traversal and hit testing. No browser and no network."""

import json
import subprocess
from pathlib import Path

from jev_ultrafast.config import resolve_config, snapshot_config

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "jev_ultrafast" / "snapshot.js"

NODE = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[1], "utf8");
const context = {globalThis: {}, console};
context.globalThis = context;
const api = vm.runInNewContext(src, context);
if (!api || !api.queryAllDeep) throw new Error("snapshot did not export its helpers");

function scopeFrom(node) {
  return {
    querySelectorAll(sel) {
      const found = [];
      (function walk(current) {
        for (const child of current.children || []) {
          found.push(child);
          walk(child);
        }
      })(node);
      if (sel === "*") return found;
      return found.filter(element => element.tagName === sel.toUpperCase());
    },
  };
}

function element(tag, children) {
  const node = {
    tagName: tag.toUpperCase(), children: children || [], shadowRoot: null,
    parentElement: null, parentNode: null,
  };
  for (const child of node.children) {
    child.parentElement = node;
    child.parentNode = node;
  }
  node.querySelectorAll = scopeFrom(node).querySelectorAll;
  node.querySelector = sel => node.querySelectorAll(sel)[0] || null;
  return node;
}

function attachShadow(host, tree, hitAt) {
  const root = scopeFrom(tree);
  root.host = host;
  root.nodeType = 11;
  root.elementFromPoint = hitAt;
  tree.parentElement = null;
  tree.parentNode = root;
  host.shadowRoot = root;
  return root;
}

const light = element("button");
const inner = element("button");
const nested = element("button");
const host = element("div");
const innerHost = element("span");
const innerTree = element("div", [nested]);
attachShadow(innerHost, innerTree, () => nested);
const outerTree = element("div", [inner, innerHost]);
attachShadow(host, outerTree, (x) => (x > 50 ? innerHost : inner));
const doc = element("body", [light, host]);
doc.elementFromPoint = () => host;
doc.nodeType = 9;
context.globalThis.__jevCfg = {shadow: true};
const deep = api.queryAllDeep(doc, "button");
if (deep.length !== 3 || deep[0] !== light || deep[1] !== inner || deep[2] !== nested) {
  throw new Error("deep walk failed: " + deep.length);
}
context.globalThis.__jevCfg = {shadow: false};
if (api.queryAllDeep(doc, "button").length !== 1) throw new Error("shadow flag off still walked a root");

context.globalThis.__jevCfg = {shadow: true};
const hit = api.resolveHit(doc, 10, 10);
if (hit !== inner) throw new Error("hit did not enter the open shadow root");
if (!api.owns(inner, hit) || !api.owns(host, hit)) throw new Error("owns rejected the node or its host");
const other = element("button");
if (api.owns(other, hit)) throw new Error("owns accepted an unrelated node");
const nestedHit = api.resolveHit(doc, 80, 10);
if (nestedHit !== nested) throw new Error("hit did not walk nested open roots");
if (!api.owns(nested, nestedHit) || !api.owns(innerHost, nestedHit) || !api.owns(host, nestedHit)) {
  throw new Error("nested owns failed");
}

const closed = {tagName: "CLOSED-PANEL", shadowRoot: null};
const open = {tagName: "OPEN-PANEL", shadowRoot: {}};
if (!api.possibleClosedShadow(closed)) throw new Error("closed custom element was not marked");
if (api.possibleClosedShadow(open)) throw new Error("open shadow was marked closed");
if (api.possibleClosedShadow({tagName: "DIV", shadowRoot: null})) throw new Error("plain div was marked closed");
context.globalThis.__jevCfg = {shadow: false};
if (api.possibleClosedShadow(closed)) throw new Error("flag off still marks closed shadow");
console.log("ok");
"""


def test_shadow_helpers_traverse_and_hit_test():
    completed = subprocess.run(
        ["node", "-e", NODE, str(SNAPSHOT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_shadow_dom_defaults_on_and_constructor_overrides(monkeypatch):
    monkeypatch.delenv("JEV_SHADOW_DOM", raising=False)
    assert resolve_config()["shadow_dom"] is True
    assert snapshot_config(resolve_config())["shadow"] is True
    monkeypatch.setenv("JEV_SHADOW_DOM", "0")
    assert resolve_config()["shadow_dom"] is False
    assert snapshot_config(resolve_config(shadow_dom=True))["shadow"] is True


def test_snapshot_expression_sets_flag():
    from jev_ultrafast.browser import snapshot_expression

    source = snapshot_expression({"shadow_dom": False})
    payload = json.loads(source.split("globalThis.__jevCfg=", 1)[1].split(";", 1)[0])
    assert payload["shadow"] is False
    assert "shadowRoot.elementFromPoint" in source
