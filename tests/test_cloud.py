"""Cloud browser mode is opt-in, offline-testable, and never prints the key."""

import json
from unittest.mock import Mock

import pytest

import jev_ultrafast.browser as browser_module
import jev_ultrafast.cloud as cloud
from jev_ultrafast import Agent
from jev_ultrafast.browser import (
    Browser,
    StalePage,
    action_freshness,
    browser_operation,
    field_value_expression,
)
from jev_ultrafast.config import resolve_config

FAKE_KEY = "bu_test_SECRET"


def _cdp(calls):
    def call(method, **params):
        calls.append((method, params))
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            return {"result": {"value": "complete"}}
        return {}

    return call


@pytest.fixture
def fake_cloud(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_API_KEY", FAKE_KEY)
    started = []

    def start(name, **body):
        started.append({"name": name, "body": body})
        return {"id": "browser-1", "liveUrl": "https://live.invalid/owned-tab"}

    stopped = []
    monkeypatch.setattr(cloud, "start_remote_daemon", start)
    monkeypatch.setattr(cloud, "stop_remote_daemon", lambda name: stopped.append(name))
    return started, stopped


@pytest.fixture
def local_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_module, "cdp", _cdp(calls))
    ensured = []
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: ensured.append(True))
    monkeypatch.delenv("JEV_BROWSER", raising=False)
    monkeypatch.delenv("JEV_VISIBLE_TAB", raising=False)
    monkeypatch.delenv("JEV_UPLOAD_FILES", raising=False)
    return calls, ensured


def cloud_browser(monkeypatch, calls):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.delenv("JEV_VISIBLE_TAB", raising=False)
    monkeypatch.setattr(browser_module, "cdp", _cdp(calls))
    ensured = []
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: ensured.append(True))
    return Browser("https://example.test/"), ensured


def test_local_run_uses_a_fresh_browser_context(monkeypatch):
    calls = []

    def call(method, **params):
        calls.append((method, params))
        if method == "Target.createBrowserContext":
            return {"browserContextId": "ctx1"}
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            return {"result": {"value": "complete"}}
        return {}

    monkeypatch.setattr(browser_module, "cdp", call)
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    monkeypatch.delenv("JEV_BROWSER", raising=False)
    monkeypatch.delenv("JEV_VISIBLE_TAB", raising=False)
    monkeypatch.delenv("JEV_UPLOAD_FILES", raising=False)
    tab = Browser("https://example.test/")
    try:
        create = next(params for method, params in calls if method == "Target.createTarget")
        assert create["browserContextId"] == "ctx1"
        assert create["newWindow"] is True
    finally:
        tab.close()
    dispose = [params for method, params in calls if method == "Target.disposeBrowserContext"]
    assert dispose == [{"browserContextId": "ctx1"}]


def test_default_is_local_and_uses_ensure_daemon(local_browser, monkeypatch, fake_cloud):
    started, _ = fake_cloud
    calls, ensured = local_browser
    tab = Browser("https://example.test/")
    try:
        assert ensured == [True]
        assert started == []
        assert tab.cloud is None
        create = next(params for method, params in calls if method == "Target.createTarget")
        assert create["background"] is True
    finally:
        tab.close()


def test_cloud_body_switches_names_and_skips_ensure_daemon(monkeypatch, fake_cloud, caplog):
    started, _ = fake_cloud
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.setenv("JEV_CLOUD_PROXY_COUNTRY", "none")
    monkeypatch.setenv("JEV_CLOUD_TIMEOUT_MIN", "999")
    monkeypatch.setenv("JEV_CLOUD_PROFILE_ID", "profile-uuid")
    monkeypatch.setenv("JEV_CLOUD_RECORD", "1")
    monkeypatch.setenv(
        "JEV_CLOUD_CUSTOM_PROXY",
        '{"host":"proxy.example","port":8080,"username":"u","password":"p"}',
    )
    config = resolve_config()
    assert config["browser"] == "cloud"
    calls = []
    tab, ensured = cloud_browser(monkeypatch, calls)
    try:
        assert ensured == []
        assert len(started) == 1
        sent = started[0]
        assert sent["name"] == f"jev-cloud-{__import__('os').getpid()}"
        assert sent["body"] == {
            "proxyCountryCode": None,
            "timeout": 240,
            "browserScreenWidth": 1120,
            "browserScreenHeight": 780,
            "allowResizing": False,
            "enableRecording": True,
            "profileId": "profile-uuid",
            "customProxy": {"host": "proxy.example", "port": 8080, "username": "u", "password": "p"},
        }
        assert cloud.helpers.NAME == sent["name"]
        assert cloud.admin.NAME == sent["name"]
        assert tab.cloud == {
            "name": sent["name"],
            "browser_id": "browser-1",
            "live_url": "https://live.invalid/owned-tab",
        }
        # The live view shows the owned tab by default in cloud mode.
        create = next(params for method, params in calls if method == "Target.createTarget")
        assert create["background"] is False
        assert "browser-1" in caplog.text
        assert "live.invalid" not in caplog.text
    finally:
        tab.close()


@pytest.mark.parametrize("raw, expected", [("0", 1), ("999", 240), ("nope", 15), ("", 15)])
def test_timeout_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.setenv("JEV_CLOUD_TIMEOUT_MIN", raw)
    assert resolve_config()["cloud"]["timeout_min"] == expected


def test_show_live_url_is_opt_in(monkeypatch, fake_cloud, caplog):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.setenv("JEV_CLOUD_SHOW_LIVE_URL", "1")
    config = resolve_config()
    handle = cloud.start(config)
    try:
        assert "https://live.invalid/owned-tab" in caplog.text
    finally:
        cloud.stop(handle)


def test_normal_close_stops_the_cloud_browser_once(monkeypatch, fake_cloud):
    _, stopped = fake_cloud
    tab, _ = cloud_browser(monkeypatch, [])
    name = tab.cloud["name"]
    tab.close()
    tab.close()
    assert stopped == [name]
    assert tab.cloud is None


def test_close_after_an_exception_still_stops(monkeypatch, fake_cloud):
    _, stopped = fake_cloud
    tab, _ = cloud_browser(monkeypatch, [])
    name = tab.cloud["name"]
    agent = Agent.__new__(Agent)
    agent.browser = tab
    with pytest.raises(RuntimeError, match="boom"):
        with agent:
            raise RuntimeError("boom")
    assert stopped == [name]


def test_missing_key_is_a_clear_error_without_key_text(monkeypatch, fake_cloud):
    started, _ = fake_cloud
    monkeypatch.delenv("BROWSER_USE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="BROWSER_USE_API_KEY") as error:
        cloud.start({"cloud": {}})
    assert FAKE_KEY not in str(error.value)
    assert started == []


def test_stop_failure_is_logged_without_raising_or_leaking(monkeypatch, fake_cloud, caplog):
    def fail(_name):
        raise RuntimeError(f"key={FAKE_KEY} https://live.invalid/owned-tab")

    monkeypatch.setattr(cloud, "stop_remote_daemon", fail)
    handle = cloud.start({"cloud": {}})
    cloud.stop(handle)
    assert "stop failed" in caplog.text
    assert FAKE_KEY not in caplog.text
    assert "live.invalid" not in caplog.text


def test_uploads_are_disabled_and_warn_once_in_cloud_mode(monkeypatch, caplog):
    import jev_ultrafast.config as config_module

    monkeypatch.setattr(config_module, "_cloud_upload_warned", False)
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.setenv("JEV_UPLOAD_FILES", "resume=/tmp/resume.pdf")
    assert resolve_config()["files"] == {}
    assert "uploads disabled" in caplog.text
    caplog.clear()
    assert resolve_config()["files"] == {}
    assert caplog.text == ""


def test_local_uploads_are_unchanged(monkeypatch):
    monkeypatch.delenv("JEV_BROWSER", raising=False)
    monkeypatch.setenv("JEV_UPLOAD_FILES", "resume=/tmp/resume.pdf")
    assert resolve_config()["files"] == {"resume": "/tmp/resume.pdf"}


def test_secrets_never_reach_output(monkeypatch, fake_cloud, caplog):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    config = resolve_config()
    handle = cloud.start(config)
    try:
        encoded = json.dumps(handle)
        assert FAKE_KEY not in caplog.text + encoded
    finally:
        cloud.stop(handle)
    assert FAKE_KEY not in caplog.text


def test_action_freshness_uses_the_frame_pair():
    page = {
        "page_key": "parent",
        "guards": {"f0:1": "guard-1", "f1:f0:2": "guard-2"},
        "frame_keys": {"f1": "child"},
    }
    assert action_freshness(page, {"node": 1, "frame": "f0"}) == ["parent", "guard-1"]
    assert action_freshness(page, {"node": 2, "frame": "f0", "route": "f1"}) == ["child", "guard-2"]


def test_active_element_fallback_is_unconditional():
    action = {"node": 1, "frame": "f0"}
    expression = field_value_expression(action)
    assert "document.activeElement" in expression
    assert "editable.length===1" in expression


def test_cloud_probe_merges_guard_locate_and_previous(monkeypatch):
    calls = []

    def cdp(method, **params):
        calls.append((method, params))
        if method != "Runtime.evaluate":
            return {}
        expression = params["expression"]
        if "fresh:false" in expression:
            return {"result": {"value": {"fresh": True, "target": {"x": 4, "y": 5}, "previous": "old"}}}
        if "document.readyState === 'complete'" in expression:
            return {"result": {"value": {"ready": True, "value": "new"}}}
        return {"result": {"value": None}}

    monkeypatch.setattr(browser_module, "cdp", cdp)
    result = browser_operation({
        "operation": "act",
        "session": "s",
        "text": "new",
        "action": {"id": "e1", "kind": "fill", "node": 1, "frame": "f0"},
        "probe": ["parent", "guard-1"],
        "keyboard": True,
        "verify_wait": True,
    })
    evaluates = [params["expression"] for method, params in calls if method == "Runtime.evaluate"]
    assert len(evaluates) == 2
    assert "expected" in evaluates[0]
    assert "document.readyState" in evaluates[1]
    assert result == {"executed": "e1", "text_verified": True, "typed": "new", "ready": True}
    assert sum(method == "Input.insertText" for method, _ in calls) == 1
    assert sum(method == "Input.dispatchKeyEvent" for method, _ in calls) == 2  # select-all only
    assert sum(method == "Input.dispatchMouseEvent" for method, _ in calls) == 2


def test_cloud_probe_stale_page_dispatches_no_input(monkeypatch):
    calls = []
    monkeypatch.setattr(
        browser_module,
        "cdp",
        lambda method, **params: calls.append((method, params)) or {"result": {"value": {"fresh": False}}},
    )
    with pytest.raises(StalePage, match="Page changed"):
        browser_operation({
            "operation": "act",
            "session": "s",
            "text": "new",
            "action": {"id": "e1", "kind": "fill", "node": 1, "frame": "f0"},
            "probe": ["parent", "guard-1"],
            "keyboard": True,
        })
    assert [method for method, _ in calls] == ["Runtime.evaluate"]


def test_local_act_never_sends_a_probe(monkeypatch):
    seen = []
    monkeypatch.setattr(
        browser_module,
        "browser_operation",
        lambda request: seen.append(request) or {"executed": "e1"},
    )
    tab = Browser.__new__(Browser)
    tab.config = {"browser": "cloud"}
    tab.fresh = Mock(return_value=True)
    tab.session = "s"
    tab.frame_sessions = {}
    tab.after_input = None
    tab._ready_after_act = False
    page = {"page_key": "parent", "guards": {"f0:1": "guard-1"}}
    action = {"id": "e1", "kind": "click", "node": 1, "frame": "f0"}
    tab.act(action, page)
    assert seen[-1]["probe"] == ["parent", "guard-1"]
    assert "verify_active" not in seen[-1]
    tab.config = {"browser": "local"}
    tab.act(action, page)
    assert seen[-1]["probe"] is None
    assert "verify_active" not in seen[-1]


def test_ready_after_the_verify_skips_the_extra_ready_call():
    tab = Browser.__new__(Browser)
    tab._ready_after_act = True
    tab.evaluate = Mock()
    assert tab.wait_until_ready() is True
    tab.evaluate.assert_not_called()
    assert tab.wait_until_ready(timeout=0) is False


def test_cloud_locale_and_timezone_defaults_and_overrides(monkeypatch):
    import jev_ultrafast.config as config_module

    monkeypatch.delenv("JEV_CLOUD_LOCALE", raising=False)
    monkeypatch.delenv("JEV_CLOUD_TIMEZONE", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setattr(config_module, "_local_timezone", lambda: "Asia/Bangkok")
    settings = config_module.cloud_settings()
    assert settings["locale"] == "en-US"
    assert settings["timezone"] == "Asia/Bangkok"
    monkeypatch.setenv("JEV_CLOUD_LOCALE", "th-TH")
    monkeypatch.setenv("JEV_CLOUD_TIMEZONE", "none")
    settings = config_module.cloud_settings()
    assert settings["locale"] == "th-TH"
    assert settings["timezone"] is None


def test_cloud_browser_sets_timezone_and_skips_unsupported_locale(monkeypatch, fake_cloud):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.setenv("JEV_CLOUD_LOCALE", "en-US")
    monkeypatch.setenv("JEV_CLOUD_TIMEZONE", "Asia/Bangkok")
    monkeypatch.delenv("JEV_VISIBLE_TAB", raising=False)
    calls = []
    monkeypatch.setattr(browser_module, "cdp", _cdp(calls))
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    Browser("https://example.test/").close()
    methods = [method for method, _ in calls]
    assert cloud.supported("Emulation.setLocaleOverride") is False
    assert "Emulation.setLocaleOverride" not in methods
    assert "Emulation.setTimezoneOverride" in methods


def test_local_browser_never_sends_locale_overrides(local_browser):
    calls, _ = local_browser
    Browser("https://example.test/").close()
    assert not [method for method, _ in calls if method.startswith("Emulation.setLocaleOverride")]
    assert not [method for method, _ in calls if method.startswith("Emulation.setTimezoneOverride")]


def _viewport_cdp(calls, reading):
    def cdp(method, **params):
        calls.append((method, params))
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            if "innerWidth" in params["expression"]:
                return {"result": {"value": reading}}
            return {"result": {"value": "complete"}}
        return {}

    return cdp


def test_cloud_viewport_mismatch_is_reapplied_and_logged(monkeypatch, fake_cloud, caplog):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.delenv("JEV_VISIBLE_TAB", raising=False)
    calls = []
    monkeypatch.setattr(browser_module, "cdp", _viewport_cdp(calls, {"w": 1120, "h": 568, "dpr": 1.25}))
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    Browser("https://example.test/").close()
    metrics = [params for method, params in calls if method == "Emulation.setDeviceMetricsOverride"]
    assert len(metrics) == 2
    assert "viewport mismatch" in caplog.text


def test_cloud_viewport_match_is_not_reapplied(monkeypatch, fake_cloud, caplog):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.delenv("JEV_VISIBLE_TAB", raising=False)
    calls = []
    monkeypatch.setattr(browser_module, "cdp", _viewport_cdp(calls, {"w": 1120, "h": 780, "dpr": 1}))
    monkeypatch.setattr(browser_module, "ensure_daemon", lambda: None)
    Browser("https://example.test/").close()
    metrics = [params for method, params in calls if method == "Emulation.setDeviceMetricsOverride"]
    assert len(metrics) == 1
    assert "viewport mismatch" not in caplog.text


def test_max_decisions_default_and_override(monkeypatch):
    monkeypatch.delenv("JEV_MAX_DECISIONS", raising=False)
    assert resolve_config()["max_decisions"] == 80
    monkeypatch.setenv("JEV_MAX_DECISIONS", "5")
    assert resolve_config()["max_decisions"] == 5
