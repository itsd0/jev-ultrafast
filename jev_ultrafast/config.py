"""Feature flags. Constructor options override environment variables."""

import os


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
):
    if frames is None:
        chosen = env_choice("JEV_FRAMES", "same", {"same", "all", "off"})
    else:
        chosen = str(frames).strip().lower()
        if chosen not in {"same", "all", "off"}:
            chosen = "same"
    return {
        "shadow_dom": env_bool("JEV_SHADOW_DOM", True) if shadow_dom is None else bool(shadow_dom),
        "frames": chosen,
        "frame_id": "f0",
        "files": parse_files(files),
        "tabs": env_bool("JEV_TABS", True) if tabs is None else bool(tabs),
        "nested_scroll": env_bool("JEV_NESTED_SCROLL", True) if nested_scroll is None else bool(nested_scroll),
        "keyboard": env_bool("JEV_KEYBOARD", True) if keyboard is None else bool(keyboard),
        "large_select": env_bool("JEV_LARGE_SELECT", True) if large_select is None else bool(large_select),
        "select_inline_max": _inline_max(select_inline_max),
        "vision": env_bool("JEV_VISION", False) if vision is None else bool(vision),
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
