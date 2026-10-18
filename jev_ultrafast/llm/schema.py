"""Strict JSON schemas for the single-LLM agent. No unions, no oneOf/allOf.

Every object sets additionalProperties:false and requires every property, so
Reflection's strict json_schema mode accepts the payload as-is. Optional action
arguments use sentinels (-1, "", false) instead of null unions; the executor
validates per-action requirements against the live observation.
"""

ACTION_NAMES = (
    "search", "navigate", "go_back", "switch_tab", "close_tab", "click", "input",
    "send_keys", "select_dropdown", "dropdown_options", "scroll", "find_text",
    "search_page", "extract", "upload_file", "save_file", "read_file", "wait", "done",
)

SCROLL_DIRECTIONS = ("up", "down", "left", "right")

SEND_KEYS = (
    "Enter", "Tab", "Escape", "ArrowDown", "ArrowUp",
    "ArrowLeft", "ArrowRight", "Space", "Backspace",
)


def _action_schema():
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": list(ACTION_NAMES)},
            # Element index from this step's DOM tree, or -1 when the action
            # takes no element (navigate, search, go_back, wait, done, ...).
            "index": {"type": "integer"},
            # Free text for input/search/extract/done, "" otherwise. Done text
            # is the final answer; input text may hold <secret>NAME</secret>.
            "text": {"type": "string"},
            "url": {"type": "string"},
            "new_tab": {"type": "boolean"},
            # Tab id (t1..t5) for switch_tab, "" otherwise.
            "tab": {"type": "string"},
            # Search engine name (duckduckgo, google, bing), "" for default.
            "engine": {"type": "string"},
            "clear": {"type": "boolean"},
            "keys": {"type": "array", "items": {"type": "string", "enum": list(SEND_KEYS)}},
            # Visible option label for select_dropdown, "" otherwise.
            "option": {"type": "string"},
            "direction": {"type": "string", "enum": list(SCROLL_DIRECTIONS)},
            # Scroll pages (>=1) or wait seconds (0-10); -1 when unused.
            "amount": {"type": "number"},
            # Regex/substring for find_text and search_page, "" otherwise.
            "pattern": {"type": "string"},
            # Allowlisted upload key, "" otherwise.
            "key": {"type": "string"},
            "filename": {"type": "string"},
            "content": {"type": "string"},
            # Done verdict only; false for every other action.
            "success": {"type": "boolean"},
        },
        "required": [
            "name", "index", "text", "url", "new_tab", "tab", "engine", "clear",
            "keys", "option", "direction", "amount", "pattern", "key",
            "filename", "content", "success",
        ],
        "additionalProperties": False,
    }


STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "thinking": {"type": "string"},
        "evaluation_previous_goal": {"type": "string"},
        "memory": {"type": "string"},
        "next_goal": {"type": "string"},
        # Empty string means the plan is unchanged.
        "plan_update": {"type": "string"},
        "actions": {"type": "array", "minItems": 1, "maxItems": 5, "items": _action_schema()},
    },
    "required": [
        "thinking", "evaluation_previous_goal", "memory", "next_goal",
        "plan_update", "actions",
    ],
    "additionalProperties": False,
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["complete", "missing"],
    "additionalProperties": False,
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

PING_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def response_format(name, schema):
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


_FORBIDDEN_KEYS = {"oneOf", "anyOf", "allOf", "not", "$ref", "$defs", "definitions"}


def validate_strict_schema(schema, _path="$"):
    """Raise ValueError unless the schema meets strict-mode rules."""
    if not isinstance(schema, dict):
        raise ValueError(f"{_path}: schema node must be an object")
    forbidden = _FORBIDDEN_KEYS & set(schema)
    if forbidden:
        raise ValueError(f"{_path}: forbidden keys {sorted(forbidden)}")
    node_type = schema.get("type")
    if isinstance(node_type, list):
        raise ValueError(f"{_path}: type unions are not allowed")
    if node_type == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise ValueError(f"{_path}: objects need a non-empty properties map")
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{_path}: objects need additionalProperties:false")
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            raise ValueError(f"{_path}: required must list every property")
        for name, child in properties.items():
            validate_strict_schema(child, f"{_path}.{name}")
    elif node_type == "array":
        if "items" not in schema:
            raise ValueError(f"{_path}: arrays need items")
        validate_strict_schema(schema["items"], f"{_path}[]")
    elif node_type in {"string", "integer", "number", "boolean"}:
        if "enum" in schema and (
            not isinstance(schema["enum"], list) or not schema["enum"]
        ):
            raise ValueError(f"{_path}: enum must be a non-empty list")
    elif node_type is None:
        if "enum" not in schema:
            raise ValueError(f"{_path}: schema needs a type or enum")
    else:
        raise ValueError(f"{_path}: unsupported type {node_type!r}")
    return True
