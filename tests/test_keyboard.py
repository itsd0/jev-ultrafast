"""PRESS_KEY is a fixed enum. A missed insertText is retyped once."""

import pytest

from jev_ultrafast.browser import PRESS_KEYS, bind_keys, browser_operation
from jev_ultrafast.config import resolve_config
from jev_ultrafast.model import action_space


def test_press_key_enum_rejects_anything_else(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []
    monkeypatch.setattr(browser, "cdp", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="PRESS_KEY"):
        browser_operation({
            "operation": "act",
            "session": "s",
            "action": {"id": "key:Delete", "kind": "key", "key": "Delete"},
        })
    assert calls == []
    actions = [{"id": f"key:{name}", "kind": "key", "key": name, "label": f"Press {name}"} for name in PRESS_KEYS]
    actions.append({"id": "key:F5", "kind": "key", "key": "F5", "label": "Press F5"})
    _elements, targets, _controls = action_space(actions)
    assert set(targets["PRESS_KEY"]) == set(PRESS_KEYS)
    assert "F5" not in targets["PRESS_KEY"]


def test_insert_text_mismatch_retypes_once_and_reports_failure(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []
    reads = {"n": 0}

    def cdp(method, **params):
        calls.append((method, params))
        if method != "Runtime.evaluate":
            return {}
        if "isContentEditable" in params.get("expression", ""):
            reads["n"] += 1
            # Previous value, post-insertText value, then the retype read.
            return {"result": {"value": "" if reads["n"] < 3 else "Z"}}
        return {"result": {"value": {"x": 5, "y": 6}}}

    monkeypatch.setattr(browser, "cdp", cdp)
    result = browser_operation({
        "operation": "act",
        "session": "s",
        "keyboard": True,
        "text": "ab",
        "action": {"id": "e1", "kind": "fill", "node": 3, "frame": "f0", "label": "Code"},
    })
    keys = [params.get("text") or params.get("key") for method, params in calls if method == "Input.dispatchKeyEvent"]
    assert "a" in keys and "b" in keys
    assert result["text_verified"] is False
    assert result["typed"] == "Z"


def test_rewritten_value_is_not_cleared(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []
    reads = {"n": 0}

    def cdp(method, **params):
        calls.append((method, params))
        if method == "Runtime.evaluate" and "isContentEditable" in params.get("expression", ""):
            reads["n"] += 1
            return {"result": {"value": "" if reads["n"] == 1 else "Zürich"}}
        if method == "Runtime.evaluate":
            return {"result": {"value": {"x": 1, "y": 2}}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    result = browser_operation({
        "operation": "act",
        "session": "s",
        "keyboard": True,
        "text": "Zurich",
        "action": {"id": "e1", "kind": "fill", "node": 3, "frame": "f0"},
    })
    assert result["text_verified"] is True
    assert result["rewritten"] is True
    assert result["typed"] == "Zürich"
    assert not any(
        method == "Input.dispatchKeyEvent" and params.get("key") == "Backspace"
        for method, params in calls
    )


def test_matching_insert_text_does_not_retype(monkeypatch):
    import jev_ultrafast.browser as browser

    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        if method == "Runtime.evaluate" and "isContentEditable" in params.get("expression", ""):
            return {"result": {"value": "ab"}}
        if method == "Runtime.evaluate":
            return {"result": {"value": {"x": 1, "y": 2}}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    result = browser_operation({
        "operation": "act",
        "session": "s",
        "keyboard": True,
        "text": "ab",
        "action": {"id": "e1", "kind": "fill", "node": 3, "frame": "f0"},
    })
    assert result["text_verified"] is True
    typed = [
        params for method, params in calls
        if method == "Input.dispatchKeyEvent" and params.get("text") in {"a", "b"}
    ]
    assert typed == []


def test_keys_are_bound_only_when_enabled():
    state = {"actions": [], "url": "u", "text": "", "scroll": {"y": 0}}
    assert bind_keys(state, False)["actions"] == []
    bound = bind_keys(state, True)
    assert [action["key"] for action in bound["actions"]] == list(PRESS_KEYS)


def test_keyboard_defaults_on(monkeypatch):
    monkeypatch.delenv("JEV_KEYBOARD", raising=False)
    assert resolve_config()["keyboard"] is True
    monkeypatch.setenv("JEV_KEYBOARD", "0")
    assert resolve_config()["keyboard"] is False
    assert resolve_config(keyboard=True)["keyboard"] is True
