"""Upload targets are allowlist keys. The model cannot choose a path."""

import json
import os

import pytest

from jev_ultrafast.browser import bind_uploads, browser_operation, real_upload_path
from jev_ultrafast.config import parse_files, resolve_config
from jev_ultrafast.model import action_space, choose


def _upload_page(key="resume"):
    return {
        "url": "https://example.test/",
        "title": "Apply",
        "text": "Resume",
        "actions": [{
            "id": "e4",
            "kind": "upload",
            "label": "Resume",
            "role": "button",
            "node": 9,
            "frame": "f0",
            "value": "/tmp/should-not-leak.pdf",
            "path": "/tmp/should-not-leak.pdf",
        }],
        "scroll": {"y": 0},
    }


def test_missing_allowlist_hides_file_inputs():
    bound = bind_uploads(_upload_page(), {})
    assert bound["actions"] == []
    assert "/tmp/should-not-leak.pdf" not in json.dumps(bound["actions"])


def test_allowlist_expands_keys_without_paths(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF")
    bound = bind_uploads(_upload_page(), {"resume": str(resume)})
    action = bound["actions"][0]
    assert action["upload_key"] == "resume"
    assert action["id"] == "e4:resume"
    assert action["value"] == ""
    assert "path" not in action
    assert str(resume) not in json.dumps(action)
    elements, targets, _controls = action_space(bound["actions"])
    assert elements[0]["operations"] == ["UPLOAD_FILE"]
    assert set(targets["UPLOAD_FILE"]) == {"1:resume"}
    assert str(resume) not in json.dumps(targets)


def test_choose_request_contains_the_key_and_not_the_path(monkeypatch, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF")
    page = bind_uploads(_upload_page(), {"resume": str(resume)})
    seen = {}

    def post(_url, _key, body):
        seen["body"] = body
        criteria = body["questions"]["operation"]["criteria"]
        target = body["questions"]["upload_file_target"]["criteria"]
        return {
            "model": "test",
            "answers": {
                "operation": {
                    "choice": "UPLOAD_FILE",
                    "confidence": 1,
                    "probabilities": {name: float(name == "UPLOAD_FILE") for name in criteria},
                },
                "upload_file_target": {
                    "choice": "1:resume",
                    "confidence": 1,
                    "probabilities": {name: float(name == "1:resume") for name in target},
                },
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr("jev_ultrafast.model.post_json", post)
    decision = choose(page, "Upload the resume", [])
    assert decision["operation"] == "UPLOAD_FILE"
    assert decision["target"] == "1:resume"
    encoded = json.dumps(seen["body"])
    assert "resume" in encoded
    assert str(resume) not in encoded
    assert "should-not-leak" not in encoded


def test_executor_uses_allowlist_path_only(monkeypatch, tmp_path):
    import jev_ultrafast.browser as browser

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF")
    planted = tmp_path / "other.pdf"
    planted.write_bytes(b"%PDF")
    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "obj-1"}}
        return {}

    monkeypatch.setattr(browser, "cdp", cdp)
    browser_operation({
        "operation": "act",
        "session": "s",
        "files": {"resume": str(resume)},
        "action": {
            "id": "e4:resume",
            "kind": "upload",
            "node": 9,
            "frame": "f0",
            "upload_key": "resume",
            "path": str(planted),
            "value": str(planted),
        },
    })
    uploaded = next(params for method, params in calls if method == "DOM.setFileInputFiles")
    assert uploaded["files"] == [os.path.realpath(resume)]
    assert str(planted) not in json.dumps(uploaded)
    assert uploaded["objectId"] == "obj-1"


def test_unknown_upload_key_does_not_touch_the_filesystem(monkeypatch, tmp_path):
    import jev_ultrafast.browser as browser

    calls = []
    monkeypatch.setattr(browser, "cdp", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="not allowlisted"):
        browser_operation({
            "operation": "act",
            "session": "s",
            "files": {},
            "action": {"id": "e4:../etc/passwd", "kind": "upload", "node": 9, "upload_key": "../etc/passwd"},
        })
    assert calls == []
    with pytest.raises(ValueError, match="not allowlisted"):
        real_upload_path({"ok": str(tmp_path / "missing.pdf")}, "nope")


def test_upload_files_env_and_constructor(monkeypatch, tmp_path):
    resume = tmp_path / "resume.pdf"
    monkeypatch.setenv("JEV_UPLOAD_FILES", f"resume={resume},../bad=/tmp/x")
    parsed = parse_files()
    assert parsed == {"resume": str(resume)}
    overridden = resolve_config(files={"photo": "/abs/photo.png"})
    assert overridden["files"]["photo"] == "/abs/photo.png"
    assert overridden["files"]["resume"] == str(resume)
