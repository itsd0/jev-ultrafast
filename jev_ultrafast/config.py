"""Feature flags. Constructor options override environment variables."""

import json
import os

from .trace import get_logger

_cloud_upload_warned = False


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_choice(name, default, allowed):
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    value = str(value).strip().lower()
    return value if value in allowed else default


DEFAULT_SEARCH_ENGINES = (
    "https://www.bing.com/search?q={q}",
    "https://duckduckgo.com/?q={q}",
    "https://search.brave.com/search?q={q}",
    "https://www.google.com/search?q={q}",
)


def parse_search_engines(override=None):
    """Generic search URL templates, each embedding {q}. Non-Google engines first."""
    if override:
        engines = [str(item).strip() for item in override]
    else:
        raw = os.environ.get("JEV_SEARCH_ENGINES")
        if raw is None or not str(raw).strip():
            return list(DEFAULT_SEARCH_ENGINES)
        engines = [item.strip() for item in str(raw).split(",")]
    return [item for item in engines if item]


def parse_files(override=None):
    files = {}
    for part in os.environ.get("JEV_UPLOAD_FILES", "").split(","):
        if "=" not in part:
            continue
        key, path = part.split("=", 1)
        key, path = key.strip(), path.strip()
        if key and path and "/" not in key and "\\" not in key and ".." not in key:
            files[key] = path
    if override:
        for key, path in override.items():
            key, path = str(key), str(path)
            if key and path and "/" not in key and "\\" not in key and ".." not in key:
                files[key] = path
    return files


def _clamped_int(name, default, low, high):
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip()) if raw is not None and str(raw).strip() else default
    except ValueError:
        value = default
    return max(low, min(high, value))


def _custom_proxy():
    raw = os.environ.get("JEV_CLOUD_CUSTOM_PROXY")
    if raw is None or not str(raw).strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("JEV_CLOUD_CUSTOM_PROXY must be a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("JEV_CLOUD_CUSTOM_PROXY must be a JSON object")
    return value


def _local_locale():
    """Best-effort BCP-47 locale from the shell environment, e.g. "en-US"."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.split(".")[0].strip().replace("_", "-") or None
    return None


def _local_timezone():
    """IANA zone name from /etc/localtime, e.g. "Asia/Bangkok". None when unknown."""
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    index = target.rfind(marker)
    return target[index + len(marker):] if index >= 0 else None


def _matched_env(name, detected):
    """Env value, the local default, or "none" to disable."""
    raw = os.environ.get(name)
    if raw is None:
        return detected
    raw = raw.strip()
    if not raw or raw.lower() == "none":
        return None
    return raw


def cloud_settings():
    """Browser Use Cloud request options. Read only when the cloud browser is selected."""
    country = str(os.environ.get("JEV_CLOUD_PROXY_COUNTRY") or "us").strip() or "us"
    return {
        "proxy_country": None if country.lower() == "none" else country,
        "timeout_min": _clamped_int("JEV_CLOUD_TIMEOUT_MIN", 15, 1, 240),
        "profile_id": str(os.environ.get("JEV_CLOUD_PROFILE_ID") or "").strip() or None,
        "record": env_bool("JEV_CLOUD_RECORD", False),
        "custom_proxy": _custom_proxy(),
        "show_live_url": env_bool("JEV_CLOUD_SHOW_LIVE_URL", False),
        # Cloud Chrome drifts to a US timezone and locale. Match this machine by
        # default so pages render the same; "none" opts out.
        "locale": _matched_env("JEV_CLOUD_LOCALE", _local_locale()),
        "timezone": _matched_env("JEV_CLOUD_TIMEZONE", _local_timezone()),
    }


def disable_cloud_uploads(files):
    """Local paths are not visible to a remote browser, so fail closed."""
    global _cloud_upload_warned
    if not files:
        return files
    if not _cloud_upload_warned:
        _cloud_upload_warned = True
        # TODO: upload through POST /api/v2/files/browsers/{session_id}/presigned-url
        # (the destination path is undocumented), then reach the remote copy from
        # the browser. Until that exists, cloud runs offer no upload targets.
        get_logger("cloud").warning("[jev:cloud] uploads disabled: the remote browser cannot read local paths")
    return {}


def _inline_max(override):
    if override is None:
        raw = os.environ.get("JEV_SELECT_INLINE_MAX", "12")
        try:
            override = int(raw)
        except ValueError:
            override = 12
    return max(0, int(override))


def resolve_config(
    *,
    shadow_dom=None,
    frames=None,
    files=None,
    tabs=None,
    nested_scroll=None,
    keyboard=None,
    large_select=None,
    select_inline_max=None,
    vision=None,
    search_engines=None,
):
    if frames is None:
        chosen = env_choice("JEV_FRAMES", "same", {"same", "all", "off"})
    else:
        chosen = str(frames).strip().lower()
        if chosen not in {"same", "all", "off"}:
            chosen = "same"
    browser = env_choice("JEV_BROWSER", "local", {"local", "cloud"})
    uploads = parse_files(files)
    if browser == "cloud":
        uploads = disable_cloud_uploads(uploads)
    return {
        "browser": browser,
        "cloud": cloud_settings() if browser == "cloud" else {},
        "shadow_dom": env_bool("JEV_SHADOW_DOM", True) if shadow_dom is None else bool(shadow_dom),
        "frames": chosen,
        "frame_id": "f0",
        "files": uploads,
        "tabs": env_bool("JEV_TABS", True) if tabs is None else bool(tabs),
        "nested_scroll": env_bool("JEV_NESTED_SCROLL", True) if nested_scroll is None else bool(nested_scroll),
        "keyboard": env_bool("JEV_KEYBOARD", True) if keyboard is None else bool(keyboard),
        "large_select": env_bool("JEV_LARGE_SELECT", True) if large_select is None else bool(large_select),
        "select_inline_max": _inline_max(select_inline_max),
        "vision": env_bool("JEV_VISION", False) if vision is None else bool(vision),
        "max_decisions": _clamped_int("JEV_MAX_DECISIONS", 80, 1, 10000),
        "search_engines": parse_search_engines(search_engines),
    }


def snapshot_config(config):
    """JSON-safe options read by snapshot.js. Never includes secrets or file paths."""
    payload = {
        "shadow": bool(config.get("shadow_dom", True)),
        "frames": config.get("frames", "same"),
        "nested": bool(config.get("nested_scroll", True)),
        "largeSelect": bool(config.get("large_select", True)),
        "selectInlineMax": int(config.get("select_inline_max", 12)),
        "vision": bool(config.get("vision", False)),
    }
    if config.get("frame_id"):
        payload["frameId"] = config["frame_id"]
    return payload
