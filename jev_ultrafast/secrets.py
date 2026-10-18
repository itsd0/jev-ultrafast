"""Domain-scoped secrets for typed input. Values never reach prompts or logs.

A secret lives in JEV_SECRET_<NAME> with its allowed hosts in
JEV_SECRET_<NAME>_DOMAINS (comma-separated, matched exactly or by parent
domain). The model only ever sees the <secret>NAME</secret> placeholder; the
executor substitutes the value at input time and only on an allowed host.
"""

import os
import re
from urllib.parse import urlparse

PLACEHOLDER_RE = re.compile(r"<secret>([A-Za-z0-9_]{1,64})</secret>")


def host_of(url):
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _domains(name):
    raw = os.environ.get(f"JEV_SECRET_{name}_DOMAINS") or ""
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def host_allowed(host, domains):
    host = (host or "").lower()
    if not host:
        return False
    return any(host == item or host.endswith("." + item) for item in domains)


def substitute(text, url):
    """Replace placeholders with secret values for this host. Fail closed.

    Returns (replacement_or_None, error_or_None). The error names the
    placeholder, never the value.
    """
    names = PLACEHOLDER_RE.findall(text or "")
    if not names:
        return text, None
    host = host_of(url)
    replaced = text
    for name in dict.fromkeys(names):
        value = os.environ.get(f"JEV_SECRET_{name}") or ""
        if len(value) < 1:
            return None, f"secret <secret>{name}</secret> is not configured"
        if not host_allowed(host, _domains(name)):
            return None, f"secret <secret>{name}</secret> is not allowed on this host"
        replaced = replaced.replace(f"<secret>{name}</secret>", value)
    return replaced, None


def has_placeholders(text):
    return bool(PLACEHOLDER_RE.search(text or ""))


def unavailable(text, url):
    """Placeholder names that would not resolve here. Values never surface."""
    host = host_of(url)
    missing = []
    for name in dict.fromkeys(PLACEHOLDER_RE.findall(text or "")):
        value = os.environ.get(f"JEV_SECRET_{name}") or ""
        if not value or not host_allowed(host, _domains(name)):
            missing.append(name)
    return missing


def redact_placeholders(text):
    """Placeholders stay readable in traces; only values are secret."""
    return text
