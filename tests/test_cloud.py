"""Cloud browser mode is opt-in, offline-testable, and never prints the key."""

import json

import pytest

import jev_ultrafast.browser as browser_module
import jev_ultrafast.cloud as cloud
from jev_ultrafast import Agent
from jev_ultrafast.browser import Browser
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


def test_cloud_body_switches_names_and_skips_ensure_daemon(monkeypatch, fake_cloud, capsys):
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
        err = capsys.readouterr().err
        assert "browser-1" in err
        assert "live.invalid" not in err
    finally:
        tab.close()


@pytest.mark.parametrize("raw, expected", [("0", 1), ("999", 240), ("nope", 15), ("", 15)])
def test_timeout_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.setenv("JEV_CLOUD_TIMEOUT_MIN", raw)
    assert resolve_config()["cloud"]["timeout_min"] == expected


def test_show_live_url_is_opt_in(monkeypatch, fake_cloud, capsys):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.setenv("JEV_CLOUD_SHOW_LIVE_URL", "1")
    config = resolve_config()
    handle = cloud.start(config)
    try:
        assert "https://live.invalid/owned-tab" in capsys.readouterr().err
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


def test_stop_failure_is_logged_without_raising_or_leaking(monkeypatch, fake_cloud, capsys):
    def fail(_name):
        raise RuntimeError(f"key={FAKE_KEY} https://live.invalid/owned-tab")

    monkeypatch.setattr(cloud, "stop_remote_daemon", fail)
    handle = cloud.start({"cloud": {}})
    cloud.stop(handle)
    err = capsys.readouterr().err
    assert "stop failed" in err
    assert FAKE_KEY not in err
    assert "live.invalid" not in err


def test_uploads_are_disabled_and_warn_once_in_cloud_mode(monkeypatch, capsys):
    import jev_ultrafast.config as config_module

    monkeypatch.setattr(config_module, "_cloud_upload_warned", False)
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    monkeypatch.setenv("JEV_UPLOAD_FILES", "resume=/tmp/resume.pdf")
    assert resolve_config()["files"] == {}
    assert "uploads disabled" in capsys.readouterr().err
    assert resolve_config()["files"] == {}
    assert capsys.readouterr().err == ""


def test_local_uploads_are_unchanged(monkeypatch):
    monkeypatch.delenv("JEV_BROWSER", raising=False)
    monkeypatch.setenv("JEV_UPLOAD_FILES", "resume=/tmp/resume.pdf")
    assert resolve_config()["files"] == {"resume": "/tmp/resume.pdf"}


def test_secrets_never_reach_output(monkeypatch, fake_cloud, capsys):
    monkeypatch.setenv("JEV_BROWSER", "cloud")
    config = resolve_config()
    handle = cloud.start(config)
    try:
        captured = capsys.readouterr()
        encoded = json.dumps(handle)
        assert FAKE_KEY not in captured.out + captured.err + encoded
    finally:
        cloud.stop(handle)
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err
