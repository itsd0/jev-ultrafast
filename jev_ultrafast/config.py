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


def resolve_config(*, shadow_dom=None, frames=None, files=None, tabs=None):
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
    }


def snapshot_config(config):
    """JSON-safe options read by snapshot.js. Never includes secrets or file paths."""
    payload = {
        "shadow": bool(config.get("shadow_dom", True)),
        "frames": config.get("frames", "same"),
    }
    if config.get("frame_id"):
        payload["frameId"] = config["frame_id"]
    return payload
