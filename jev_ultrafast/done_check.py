"""Deterministic pre-done evidence check, shared by classic and Beam.

Before accepting done(success=true), every requirement extractable from the
goal must have evidence in the run: named entities seen in observed text,
required tabs open, widget settings committed per verify events, and named
filter values not visibly unselected. Anything missing forces success=false;
the caller reports done-vs-missing from the same verdict. General matching
only: normalized substrings and token containment, no site knowledge.
"""

GOAL_VERBS = frozenset({
    "find", "list", "get", "open", "go", "show", "search", "report", "visit",
    "dismiss", "click", "buy", "add", "set", "check", "enter", "type", "press",
    "scroll", "navigate", "switch", "close", "save", "read", "upload", "wait",
    "extract", "book",
})
UI_WORDS = frozenset({
    "more", "less", "next", "previous", "prev", "back", "close", "closed",
    "open", "opened", "menu", "home", "all", "first", "last", "show", "shown",
    "hide", "hidden", "expand", "expanded", "collapse", "collapsed", "load",
    "loading", "view", "submit", "cancel", "apply", "applied", "clear",
    "cleared", "reset", "done", "continue", "skip", "ok", "yes", "no",
})
MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})
# Goal words that need a committed widget setting (filter, date, traveler).
COMMIT_TRIGGERS = (
    "filter", "sort", "nonstop", "direct", "select", "maximum", "minimum",
    "check-in", "check-out", "checkin", "checkout", "depart", "return",
    "adult", "guest", "per night",
)
# Lowercase filter values worth a selected-state check on the final page.
FILTER_VOCAB = frozenset({"nonstop", "non-stop", "direct"})
NEGATIVE_STATE = ("not selected", "unselected", "not set")
TAB_WORDS = frozenset({"new", "another", "separate", "second"})
STRIP_CHARS = ".,'_-!?:;()[]{}‘’“”/"

_QUOTES = "'\""


def _quoted_spans(text):
    """(start, end) of double- and single-quoted spans, apostrophes excluded.

    A quote opens only after whitespace or an opening bracket, and closes
    before whitespace, punctuation, or end: Bangkok's pairs with nothing.
    """
    spans = []
    for mark in ("'", '"'):
        idx = 0
        while True:
            start = text.find(mark, idx)
            if start < 0:
                break
            before = text[start - 1] if start else " "
            if start and (before.isalnum() or before == "_"):
                idx = start + 1
                continue
            end = text.find(mark, start + 1)
            if end < 0:
                break
            after = text[end + 1] if end + 1 < len(text) else " "
            if after.isalnum() or after == "_":
                idx = start + 1
                continue
            spans.append((start, end))
            idx = end + 1
    return spans


def normalize(text):
    """Lowercase, possessive-stripped, punctuation-flattened comparison form."""
    clean = (text or "").lower().replace("’s", "").replace("'s", "")
    return " ".join("".join(ch if ch.isalnum() else " " for ch in clean).split())


def quoted_values(text):
    """Quoted phrases, minus apostrophe artifacts and bare numbers."""
    values = []
    for start, end in _quoted_spans(text or ""):
        bare = text[start + 1:end].strip()
        if len(bare) < 2 or len(bare) > 80:
            continue
        squashed = bare.replace("$", "").replace(".", "").replace(",", "").strip()
        if not squashed or squashed.isdigit():
            continue
        values.append(bare)
    return values


def _goal_tokens(goal):
    return [tok.strip(STRIP_CHARS) for tok in (goal or "").split()]


def goal_entities(goal):
    """Checkable names from goal text: quoted phrases, dotted tokens, and
    capitalized word runs. Skips leading verbs, common verbs, months, and
    bare numbers: those never verify deterministically."""
    found, seen = [], set()

    def add(raw):
        norm = normalize(raw)
        if len(norm) < 2 or norm in seen or norm.replace(" ", "").isdigit():
            return
        seen.add(norm)
        found.append(raw.strip())

    for match in quoted_values(goal):
        add(match)
    tokens = [tok for tok in _goal_tokens(goal) if tok]
    for token in tokens:
        if "." in token.strip(".") and any(ch.isalpha() for ch in token):
            add(token)
    skip_lead = GOAL_VERBS | {
        "on", "in", "at", "the", "a", "an", "to", "for", "of",
        "do", "does", "did", "is", "are", "was", "were", "be", "been", "being",
        "will", "would", "can", "could", "should", "must", "may", "might", "shall",
        "have", "has", "had", "having",
    }
    run = []

    def flush():
        while run and run[0].lower() in skip_lead:
            run.pop(0)
        if len(run) == 1 and run[0].lower() in UI_WORDS:
            run.clear()
        if run and not any(word.lower() in MONTHS for word in run):
            add(" ".join(run))
        run.clear()

    raws = (goal or "").split()
    for raw in raws:
        token = raw.strip(STRIP_CHARS)
        if token and any(ch.isupper() for ch in token):
            run.append(token)
            if raw.rstrip().endswith((",", ";", ":", "(", ")")):
                flush()
            continue
        if run:
            flush()
    if run:
        flush()
    return found


def page_text(page):
    """Whole-page text plus section bodies, the evidence the run observed."""
    page = page or {}
    parts = [page.get("text") or ""]
    for section in page.get("sections") or []:
        heading = (section or {}).get("heading") or ""
        body = (section or {}).get("text") or ""
        parts.append(f"{heading}\n{body}" if heading else body)
    return "\n\n".join(part for part in parts if part)


def tabs_open(browser):
    """Open tab count, or None when the browser does not track tabs."""
    try:
        tabs = getattr(browser, "tabs", None)
    except Exception:
        return None
    if isinstance(tabs, dict) and tabs:
        return len(tabs)
    if isinstance(tabs, (list, tuple)) and tabs:
        return len(tabs)
    return None


def needs_new_tab(goal):
    """The goal asks for a second tab."""
    words = normalize(goal).split()
    return any(
        first in TAB_WORDS and second == "tab"
        for first, second in zip(words, words[1:])
    )


def _control_state(action):
    """Selected / unselected / unknown for a final-page control."""
    label = normalize((action or {}).get("label"))
    if any(phrase in label for phrase in NEGATIVE_STATE):
        return "unselected"
    for key in ("checked", "selected"):
        value = (action or {}).get(key)
        if value in (True, "true", "1", 1, "checked", "selected"):
            return "selected"
        if value in (False, "false", "0", 0):
            return "unselected"
    return "unknown"


def _related(value, label):
    """Bidirectional token containment (long tokens only): 'nonstop' relates
    to both 'Nonstop only' and 'Stops, Not selected'."""

    def variants(text):
        out = set()
        for tok in normalize(text).split():
            if len(tok) < 4:
                continue
            out.add(tok)
            if len(tok) >= 5 and tok.endswith("s"):
                out.add(tok[:-1])
        return out

    mine, theirs = variants(value), variants(label)
    return any(a in b or b in a for a in mine for b in theirs)


def check_done(
    goal, *, text="", title="", memo=(), visited=(), tabs_open=None,
    widget_commits=0, controls=(),
):
    """Verify goal requirements against run evidence.

    Returns {"ok", "missing", "done"}: missing is fatal for success=true,
    done lists the evidenced requirements for the reply.
    """
    missing, done = [], []
    parts = [text or "", title or ""]
    parts.extend(
        str(item.get("fact") if isinstance(item, dict) else item or "") for item in memo
    )
    parts.extend(str(url or "") for url in visited)
    haystack = normalize(" ".join(parts))
    for entity in goal_entities(goal):
        if normalize(entity) in haystack:
            done.append(entity)
        else:
            missing.append(f"goal names {entity}, not seen in evidence")
    if needs_new_tab(goal):
        if tabs_open is None:
            pass
        elif tabs_open >= 2:
            done.append("new tab open")
        else:
            missing.append("goal needs a new tab")
    if any(trigger in normalize(goal) for trigger in COMMIT_TRIGGERS):
        if (widget_commits or 0) >= 1:
            done.append("widget setting committed")
        else:
            missing.append("goal needs a filter/date/traveler setting, none verified as committed")
    values = quoted_values(goal)
    goal_tokens = set(normalize(goal).split())
    values.extend(sorted(word for word in FILTER_VOCAB if word in goal_tokens))
    for value in values:
        related = [item for item in controls if _related(value, (item or {}).get("label"))]
        if not related:
            continue
        states = {_control_state(item) for item in related}
        if "selected" in states or "unknown" in states:
            done.append(f"{value} not contradicted")
        elif "unselected" in states:
            missing.append(f"{value} looks unselected")
    return {"ok": not missing, "missing": missing[:8], "done": done[:12]}
