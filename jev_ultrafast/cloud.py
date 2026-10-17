"""Optional Browser Use Cloud browsers. Local runs never enter this path."""

import atexit
import os
import signal
import sys
import threading

from browser_harness import admin, helpers
from browser_harness.admin import start_remote_daemon, stop_remote_daemon

SCREEN_WIDTH = 1120
SCREEN_HEIGHT = 780

_lock = threading.RLock()
_active = {}
_state = {}
_previous_signals = {}


def request_body(config):
    """The Browser Use create-browser body. Secrets stay in the custom proxy object."""
    options = (config or {}).get("cloud") or {}
    try:
        timeout = int(options.get("timeout_min", 15))
    except (TypeError, ValueError):
        timeout = 15
    body = {
        "proxyCountryCode": options.get("proxy_country", "us"),
        "timeout": max(1, min(240, timeout)),
        "browserScreenWidth": SCREEN_WIDTH,
        "browserScreenHeight": SCREEN_HEIGHT,
        "allowResizing": False,
        "enableRecording": bool(options.get("record", False)),
    }
    if options.get("profile_id"):
        body["profileId"] = options["profile_id"]
    if options.get("custom_proxy") is not None:
        body["customProxy"] = options["custom_proxy"]
    return body


def start(config):
    """Provision one cloud browser and point the harness globals at its daemon."""
    if not str(os.environ.get("BROWSER_USE_API_KEY") or "").strip():
        raise RuntimeError("JEV_BROWSER=cloud requires BROWSER_USE_API_KEY")
    options = (config or {}).get("cloud") or {}
    name = f"jev-cloud-{os.getpid()}"
    os.environ["BH_OPEN_LIVE_URL"] = "0"
    browser = start_remote_daemon(name, **request_body(config))
    handle = {
        "name": name,
        "browser_id": browser.get("id"),
        "live_url": browser.get("liveUrl"),
    }
    with _lock:
        _state[name] = {
            "stopped": False,
            "previous_helpers_name": helpers.NAME,
            "previous_admin_name": admin.NAME,
        }
        _active[name] = handle
    helpers.NAME = name
    admin.NAME = name
    _register_cleanup(handle)
    print(f"[jev:cloud] browser {handle['browser_id']}", file=sys.stderr, flush=True)
    if options.get("show_live_url") and handle["live_url"]:
        print(f"[jev:cloud] live view {handle['live_url']}", file=sys.stderr, flush=True)
    return handle


def stop(handle):
    """Stop the remote daemon and its billable browser. Never raises past logging."""
    if not isinstance(handle, dict):
        return
    name = handle.get("name")
    if not name:
        return
    with _lock:
        state = _state.get(name)
        if state is None or state["stopped"]:
            return
        state["stopped"] = True
    try:
        stop_remote_daemon(name)
    except Exception as exc:
        # The API key and CDP URLs never enter this message.
        print(f"[jev:cloud] stop failed: {type(exc).__name__}", file=sys.stderr, flush=True)
    finally:
        with _lock:
            _active.pop(name, None)
            state = _state.pop(name, {})
        helpers.NAME = state.get("previous_helpers_name", helpers.NAME)
        admin.NAME = state.get("previous_admin_name", admin.NAME)
        _restore_signals()


def stop_all():
    with _lock:
        handles = list(_active.values())
    for handle in handles:
        stop(handle)


def _register_cleanup(handle):
    atexit.register(stop, handle)
    if threading.current_thread() is not threading.main_thread():
        return
    with _lock:
        for signum in (signal.SIGINT, signal.SIGTERM):
            if signum in _previous_signals:
                continue
            try:
                _previous_signals[signum] = signal.getsignal(signum)
                signal.signal(signum, _signal_handler)
            except (ValueError, OSError):
                _previous_signals.pop(signum, None)


def _restore_signals():
    with _lock:
        if _active:
            return
        previous = dict(_previous_signals)
        _previous_signals.clear()
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass


def _signal_handler(signum, frame):
    with _lock:
        previous = _previous_signals.get(signum)
    stop_all()
    if callable(previous):
        previous(signum, frame)
        return
    signal.signal(signum, signal.SIG_DFL if previous is None else previous)
    if previous == signal.SIG_IGN:
        return
    os.kill(os.getpid(), signum)
