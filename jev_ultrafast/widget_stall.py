"""Widget-stall escalation, shared by the classic and Beam agents.

A widget that absorbs actions without verifiable change is stalled: after N
unverified actions on the same control (or open dialog), the run must change
strategy instead of repeating; after a second round of N it marks the widget
unmet and moves on instead of burning the budget. Identity is kind+label based,
never positional; nothing here names any site or widget.
"""

import os

DEFAULT_AFTER = 3
MAX_ESCALATIONS = 2
RUN_HOLD_STEPS = 100000

TYPE_STRATEGIES = frozenset({"fill", "input", "type", "type+enter", "type+commit", "typing"})
KEY_STRATEGIES = frozenset({"key", "keys", "press", "send_keys", "keyboard"})


def stall_after():
    """Unverified actions on one widget before each escalation. Env-tunable."""
    try:
        value = int(os.environ.get("JEV_WIDGET_STALL_N", "") or DEFAULT_AFTER)
    except ValueError:
        return DEFAULT_AFTER
    return value if value >= 1 else DEFAULT_AFTER


def control_key(kind, label, role=""):
    """Stable control identity: kind + normalized label + role. Never an index."""
    clean = " ".join((label or "").split()).casefold()
    return f"{kind or ''}\x00{clean}\x00{role or ''}"


def modal_key(overlay):
    """Identity for element-less actions (key presses) inside an open dialog."""
    clean = " ".join((overlay or "").split()).casefold()
    return f"modal\x00{clean}\x00"


def widget_key_for(action, page):
    """(key, label) for a widget action, or (None, label) when unidentifiable.

    Element actions key by control; element-less ones (key presses) fall back
    to the open dialog's label. Anything else is untrackable: no key, no count.
    """
    action = action or {}
    page = page or {}
    label = (action.get("label") or "").strip()
    if label and action.get("kind") != "key":
        return control_key(action.get("kind"), label, action.get("role")), label
    overlay = (page.get("overlay") or "").strip()
    if overlay:
        return modal_key(overlay), overlay
    return None, label or ""


def is_modal_key(key):
    """True for dialog-level keys (element-less actions like key presses)."""
    return (key or "").startswith("modal\x00")


def categorize(strategy):
    """Normalize an executor strategy name to type/keys/itself."""
    name = (strategy or "").strip().casefold().replace("_", " ")
    compact = name.replace(" ", "").replace("-", "").replace("+", "")
    if compact in {"fill", "input", "type", "typeenter", "typecommit", "typing"}:
        return "type"
    if compact in {"key", "keys", "press", "sendkeys", "keyboard", "presskey"}:
        return "keys"
    return name or "click"


def suggest_change(tried, can_type):
    """First untried strategy change. General fallbacks only, in order."""
    tried = {categorize(item) for item in tried or []}
    if can_type and "type" not in tried:
        return "type the value directly into the field and press Enter"
    if "keys" not in tried:
        return "drive it from the keyboard (Tab/arrows to move, Enter to confirm)"
    if "reopen" not in tried:
        return "close and reopen the widget, then act on it once"
    return "a different control in the same dialog"


class WidgetStallTracker:
    """Per-run escalation state. Level 1 forces a strategy change, level 2
    abandons the widget. Verified actions and page changes reset counts."""

    def __init__(self, after=None):
        self.after = after if isinstance(after, int) and after >= 1 else stall_after()
        self._counts = {}
        self._tried = {}
        self._levels = {}
        self._labels = {}
        self._fingerprint = None

    def reset(self):
        """Full reset: call when the run navigates to a new page."""
        self._counts.clear()
        self._tried.clear()
        self._levels.clear()

    def note(self, key, *, label="", verified=False, strategy="", fingerprint=""):
        """Record one widget action. Returns an escalation event or None."""
        if fingerprint != self._fingerprint:
            # In-place churn ends the current block but keeps escalation
            # history: the same stuck dialog stays stuck. Navigation resets
            # everything via reset(); without that, same-label controls on a
            # new page would inherit a stranger's levels.
            self._fingerprint = fingerprint
            self._counts.clear()
        if not key or key in self._abandoned():
            return None
        if label:
            self._labels[key] = label
        if verified:
            self._counts[key] = 0
            self._tried[key] = set()
            return None
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        tried = self._tried.setdefault(key, set())
        if strategy:
            tried.add(categorize(strategy))
        level = self._levels.get(key, 0)
        if level < MAX_ESCALATIONS and count >= self.after:
            level += 1
            self._levels[key] = level
            self._counts[key] = 0
            return {
                "level": level,
                "key": key,
                "label": self._labels.get(key, label),
                "attempts": count,
                "tried": sorted(tried),
                "give_up": level >= MAX_ESCALATIONS,
            }
        return None

    def _abandoned(self):
        return {key for key, level in self._levels.items() if level >= MAX_ESCALATIONS}

    @property
    def abandoned(self):
        """Keys that hit level 2 and must not be offered again."""
        return self._abandoned()
