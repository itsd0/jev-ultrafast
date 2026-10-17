"""Feature flags. Constructor options override environment variables."""

import os


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def resolve_config(*, shadow_dom=None):
    return {
        "shadow_dom": env_bool("JEV_SHADOW_DOM", True) if shadow_dom is None else bool(shadow_dom),
    }


def snapshot_config(config):
    """JSON-safe options read by snapshot.js. Never includes secrets or file paths."""
    return {"shadow": bool(config.get("shadow_dom", True))}
