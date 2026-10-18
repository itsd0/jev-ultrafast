"""Decision providers choose an operation and a target. A text model writes field values."""

import json
import math
import os
import sys
import time

import httpx

from .questions import (
    DECOMPOSE_SYSTEM,
    MERCURY_BLOCKED,
    MERCURY_DONE,
    NEXT_ACTION,
    TARGET,
    TEXT_VALUE,
    VERIFY_SYSTEM,
    mercury_next_instructions,
    mercury_requirement_instructions,
)

CLIENT = httpx.Client(http2=True, timeout=25)
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
MAX_POSTS = 4  # first try plus three retries
CHOICE_LIMIT = 255
REASONING_EFFORTS = {"instant", "low", "medium", "high"}
INVALID_RESPONSE = "Invalid TypeSafe response; no action executed."


def debug_level():
    value = os.environ.get("JEV_DEBUG", "").strip().lower()
    if value in {"2", "verbose"}:
        return 2
    if value in {"1", "true", "yes", "on"}:
        return 1
    return 0


def token_counts(usage):
    if not isinstance(usage, dict):
        return 0, 0

    def count(*keys):
        for key in keys:
            value = usage.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                return int(value)
        return 0

    return (
        count("prompt_tokens", "inputTokens", "input_tokens"),
        count("completion_tokens", "outputTokens", "output_tokens"),
    )


def reasoning_tokens(usage):
    if not isinstance(usage, dict):
        return 0
    details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if isinstance(details, dict):
        value = details.get("reasoning_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return int(value)
    value = usage.get("reasoning_tokens")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return int(value)
    return 0


def decision_provider():
    """Unset and "typesafe" keep the original TypeSafe request. "mercury" is opt-in."""
    value = os.environ.get("DECISION_PROVIDER", "typesafe").strip().lower()
    return "mercury" if value == "mercury" else "typesafe"


def reasoning_effort():
    value = os.environ.get("TEXT_MODEL_REASONING", "instant").strip().lower()
    return value if value in REASONING_EFFORTS else "instant"


def log_model_call(kind, url, body, result, latency_ms, summary):
    """Server-side timing log. Credentials are never passed here, so they cannot leak."""
    level = debug_level()
    if level < 1:
        return
    usage = result.get("usage") if isinstance(result, dict) else None
    prompt, completion = token_counts(usage)
    reasoning = reasoning_tokens(usage)
    seconds = max(latency_ms / 1000, 1e-6)
    rate = f"{completion / seconds:.1f}" if completion else "n/a"
    print(
        f"[jev:{kind}] model={body.get('model', '?')} latency_ms={latency_ms} "
        f"prompt={prompt} completion={completion} tok/s={rate} reasoning={reasoning} {summary}",
        file=sys.stderr,
        flush=True,
    )
    if level >= 2:
        for label, payload in (("request", {"url": url, **body}), ("response", result)):
            dumped = json.dumps(redact_secrets(payload))
            print(f"[jev:{kind}] {label}: {dumped[:4000]}", file=sys.stderr, flush=True)


def retry_delay(response, attempt):
    """Honor Retry-After seconds when present. Otherwise wait 0.5s, 1s, 2s."""
    header = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if header:
        try:
            seconds = float(str(header).strip())
        except ValueError:
            seconds = None
        if seconds is not None and math.isfinite(seconds) and seconds >= 0:
            return min(seconds, 30)
    return 0.5 * 2**attempt


def retry_statuses(url):
    statuses = {429, 503, 529}
    if str(url).rstrip("/").endswith("/decisions"):
        statuses |= {502, 504}
    return statuses


def redact_secrets(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() == "authorization":
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = redact_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        redacted = value
        if redacted.lower().startswith("bearer "):
            return "Bearer [redacted]"
        for name in ("INCEPTION_API_KEY", "TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY", "VISION_API_KEY"):
            secret = os.environ.get(name) or ""
            if len(secret) >= 8:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted
    return value


class ModelHTTPError(RuntimeError):
    """A provider HTTP error with its status and machine-readable code."""

    def __init__(self, status_code, body=""):
        super().__init__(f"Model provider returned HTTP {status_code}; no action executed.")
        self.status_code = status_code
        self.body = body
        self.code = ""
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                self.code = str(error.get("code") or error.get("type") or "")
            elif payload.get("code"):
                self.code = str(payload["code"])


def post_json(url, key, body):
    for attempt in range(MAX_POSTS):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in retry_statuses(url) and attempt < MAX_POSTS - 1:
            delay = retry_delay(response, attempt)
            if debug_level() >= 1:
                print(
                    f"[jev:http] retry url={url} status={response.status_code} "
                    f"attempt={attempt + 1} wait_s={delay}",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(delay)
            continue
        if response.is_error:
            # The body usually names the invalid field. Credentials are redacted
            # before it reaches stderr or any captured log.
            try:
                safe_body = str(redact_secrets(response.text))
            except Exception:
                safe_body = ""
            print(
                f"[jev:http] error url={url} status={response.status_code} body={safe_body[:2000]}",
                file=sys.stderr,
                flush=True,
            )
            raise ModelHTTPError(response.status_code, safe_body)
        return response.json()
    raise RuntimeError("Model unavailable")


def prepare_choice_answer(answer):
    """Renormalize a choice distribution that rounding pushed outside validate_choice's 0.02 band.

    The chosen id is left untouched. validate_choice still rejects a choice that is not the argmax.
    """
    if not isinstance(answer, dict):
        return answer
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or not probabilities:
        return answer
    numbers = list(probabilities.values())
    if not all(type(n) in (int, float) and math.isfinite(n) for n in numbers):
        return answer
    total = sum(numbers)
    if total <= 0 or abs(total - 1) < 0.02:
        return answer
    print(
        f"[jev:decision] renormalized probabilities sum={total:.6f}",
        file=sys.stderr,
        flush=True,
    )
    return {**answer, "probabilities": {key: value / total for key, value in probabilities.items()}}


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(INVALID_RESPONSE)
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {
        "click": "CLICK",
        "fill": "TYPE_TEXT",
        "select": "SELECT",
        "select_option": "SELECT_OPTION",
        "upload": "UPLOAD_FILE",
    }
    for action in actions:
        kind = action["kind"]
        if kind == "key":
            key = action.get("key")
            if key not in {
                "Enter", "Tab", "Escape", "ArrowDown", "ArrowUp",
                "ArrowLeft", "ArrowRight", "Space", "Backspace",
            }:
                continue
            targets.setdefault("PRESS_KEY", {})[key] = action
            continue
        if kind == "scroll":
            delta = action.get("delta") or 0
            if action.get("axis") == "x":
                operation_name = "SCROLL_RIGHT" if delta > 0 else "SCROLL_LEFT"
            else:
                operation_name = "SCROLL_DOWN" if delta > 0 else "SCROLL_UP"
            targets.setdefault(operation_name, {})[action["id"]] = action
            continue
        if kind == "switch_tab":
            tab = action.get("tab")
            if not isinstance(tab, str) or not tab.startswith("t") or not tab[1:].isdigit():
                continue
            targets.setdefault("SWITCH_TAB", {})[tab] = action
            continue
        if kind == "close_tab":
            controls["CLOSE_TAB"] = action
            continue
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        if kind == "upload":
            key = action.get("upload_key")
            if not isinstance(key, str) or not key or "/" in key or "\\" in key or ".." in key:
                continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            state_keys = ("role", "value", "checked", "selected", "expanded", "pressed")
            element = {k: action[k] for k in state_keys if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "upload":
            key = action.get("upload_key")
            if not isinstance(key, str) or not key or "/" in key or "\\" in key:
                continue
            target = f"{index}:{key}"
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


PROGRESS_NOTES = frozenset({"value already set", "value unchanged", "already selected"})


def recent_actions(history, limit=10):
    """Only satisfied-step markers reach the model; failure notes stay internal."""
    return [
        {
            **{k: item.get(k) for k in ("action", "kind", "text", "page_changed")},
            "note": item.get("note") if item.get("note") in PROGRESS_NOTES else None,
        }
        for item in history[-limit:]
    ]


def cap_criteria(criteria):
    if len(criteria) <= CHOICE_LIMIT:
        return criteria
    print(
        f"[jev:decision] capped choice options {len(criteria)} -> {CHOICE_LIMIT}",
        file=sys.stderr,
        flush=True,
    )
    return dict(list(criteria.items())[:CHOICE_LIMIT])


def operation_catalog(targets, controls):
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
        "SELECT_OPTION": "Choose one option in a long dropdown. A small LLM will name the option.",
        "UPLOAD_FILE": "Upload an allowlisted file. Choose the offered key. A key is not a filesystem path.",
        "SWITCH_TAB": "Switch to another tab opened by this task. Choose an offered tab id.",
        "SCROLL_DOWN": "Scroll down the document or an offered container.",
        "SCROLL_UP": "Scroll up the document or an offered container.",
        "SCROLL_LEFT": "Scroll an offered container left.",
        "SCROLL_RIGHT": "Scroll an offered container right.",
        "PRESS_KEY": "Press one offered key on the focused control.",
    }
    return {
        **{key: labels[key] for key in targets},
        **{key: value["label"] for key, value in controls.items()},
        "DONE": "Every requirement is visibly satisfied.",
        "BLOCKED": "No supported operation can progress.",
    }


def choose(state, goal, history, requirements=None, progress=None, guidance=None):
    if decision_provider() == "mercury":
        return choose_mercury(state, goal, history, requirements or [goal], progress or {})
    elements, targets, controls = action_space(state["actions"])
    operations = operation_catalog(targets, controls)
    body = typesafe_body(state, goal, history, elements, targets, operations, tuple(guidance or ()))
    url = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
    started = time.perf_counter()
    result = post_json(url, os.environ["TYPESAFE_API_KEY"], body)
    try:
        operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
        operation = operation_answer["choice"]
        target = None
        target_answer = None
        probabilities = {}
        if operation in targets:
            # Unused target heads cannot cause an action. Validate the head selected by the operation.
            target_answer = validate_choice(
                result["answers"].get(operation.lower() + "_target", {}), targets[operation]
            )
            target = target_answer["choice"]
            choice = targets[operation][target]["id"]
            probabilities = {
                action["id"]: target_answer["probabilities"][index] for index, action in targets[operation].items()
            }
        else:
            choice = controls[operation]["id"] if operation in controls else operation
            probabilities[choice] = operation_answer["probabilities"][operation]
    except (ValueError, KeyError, TypeError) as error:
        latency_ms = round((time.perf_counter() - started) * 1000)
        log_model_call("typesafe", url, body, result, latency_ms, f"error={error}")
        try:
            safe_result = str(redact_secrets(result))
        except Exception:
            safe_result = ""
        print(
            f"[jev:decision] invalid response body={safe_result[:2000]}",
            file=sys.stderr,
            flush=True,
        )
        raise ValueError(INVALID_RESPONSE) from error
    latency_ms = round((time.perf_counter() - started) * 1000)
    log_model_call(
        "typesafe",
        url,
        body,
        result,
        latency_ms,
        f"operation={operation} target={target} choice={choice} "
        f"confidence={operation_answer['confidence']:.2f}",
    )
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": latency_ms,
        "request": body,
    }


def inception_key():
    key = os.environ.get("INCEPTION_API_KEY") or os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("Mercury calls need INCEPTION_API_KEY; no action executed.")
    return key


def inception_base():
    return os.environ.get("INCEPTION_API_BASE", "https://api.inceptionlabs.ai/v1").rstrip("/")


def display_operation(operation, action):
    kind = action.get("kind")
    if kind == "fill" or operation == "TYPE_TEXT":
        return "TYPE_TEXT"
    if kind in {"select", "select_option"} or operation in {"SELECT", "SELECT_OPTION"}:
        return "SELECT"
    if kind == "scroll" or str(operation).startswith("SCROLL"):
        return "SCROLL"
    if kind == "wait" or operation == "WAIT":
        return "WAIT"
    if kind == "click" or operation == "CLICK":
        return "CLICK"
    return operation


def effect_for(action, operation):
    label = action.get("label") or ""
    current = action.get("current_value", action.get("value", ""))
    shown = display_operation(operation, action)
    if shown == "TYPE_TEXT":
        if current:
            return "replace the current value in this field with a goal value"
        return "type a goal value into this empty field"
    if shown == "SELECT" and " → " in label:
        base, option = label.split(" → ", 1)
        if current:
            return f"change {base.strip()} from {current} to {option.strip()}"
        return f"change {base.strip()} to {option.strip()}"
    if shown == "SELECT":
        return "choose this offered option"
    if shown == "SCROLL":
        delta = action.get("delta") or 0
        if action.get("axis") == "x":
            direction = "right" if delta > 0 else "left"
        else:
            direction = "down" if delta > 0 else "up"
        return f"scroll {direction}"
    if shown == "WAIT":
        return "wait for the page to update"
    lowered = label.lower()
    if lowered.startswith("view ") or lowered.startswith("open "):
        return "open this result page"
    if "checked" in action or "pressed" in action:
        return "toggle this control"
    return "activate this control"


def action_description(action, operation, target_key):
    label = action.get("label") or operation
    description = {
        "operation": display_operation(operation, action),
        "element": f"[{target_key}] {label}",
        "current_value": action.get("current_value", action.get("value", "")),
        "effect": effect_for(action, operation),
    }
    if action.get("role"):
        description["role"] = action["role"]
    for key in ("checked", "selected", "expanded", "pressed"):
        if key in action:
            description[key] = action[key]
    return description


def flat_catalog(state):
    """One choice per observed action id, plus DONE and BLOCKED. Never more than 255."""
    elements, targets, controls = action_space(state["actions"])
    refs = {}
    for operation, group in targets.items():
        for target_key, action in group.items():
            refs[action["id"]] = {"operation": operation, "target": target_key, "action": action}
    for name, action in controls.items():
        refs[action["id"]] = {"operation": name, "target": None, "action": action}
    ordered = []
    for action in state.get("actions") or []:
        if action.get("id") in refs and action["id"] not in ordered:
            ordered.append(action["id"])
    room = CHOICE_LIMIT - 2
    if len(ordered) > room:
        print(
            f"[jev:decision] capped choice options {len(ordered) + 2} -> {CHOICE_LIMIT}",
            file=sys.stderr,
            flush=True,
        )
        ordered = ordered[:room]
    criteria = {}
    for action_id in ordered:
        ref = refs[action_id]
        criteria[action_id] = action_description(ref["action"], ref["operation"], ref["target"] or action_id)
    criteria["DONE"] = MERCURY_DONE
    criteria["BLOCKED"] = MERCURY_BLOCKED
    assert len(criteria) <= CHOICE_LIMIT
    return elements, refs, criteria


def requirement_rows(requirements, progress):
    rows = []
    for index, text in enumerate(requirements, start=1):
        probability = (progress or {}).get(f"r{index}")
        if probability is None and progress:
            probability = progress.get(text)
        try:
            number = float(probability)
        except (TypeError, ValueError):
            number = None
        if number is not None and not math.isfinite(number):
            number = None
        status = "satisfied" if number is not None and number >= 0.85 else "NOT yet satisfied"
        rows.append({
            "id": f"r{index}",
            "text": text,
            "last_probability": number,
            "status": status,
        })
    return rows


def mercury_body(state, goal, history, requirements, progress):
    elements, refs, criteria = flat_catalog(state)
    rows = requirement_rows(requirements, progress)
    questions = {
        "next_action": {
            "type": "choice",
            "criteria": criteria,
            "instructions": mercury_next_instructions(
                goal,
                [f"{row['id']}: {row['status']} (p={row['last_probability']}) {row['text']}" for row in rows],
            ),
        }
    }
    for row in rows:
        questions[f"req_{row['id'][1:]}"] = {
            "type": "noul",
            "instructions": mercury_requirement_instructions(row["text"]),
            "criteria": {
                "true": "Visibly proven by current controls or explicit confirmation text",
                "false": "Not set, not applied, contradicted (e.g. text says 'anywhere'/'all'), or unknown",
            },
        }
    body = {
        "model": os.environ.get("DECISION_MODEL", "mercury-decide"),
        "state": {
            "goal": goal,
            "requirements": rows,
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": recent_actions(history),
        },
        "questions": questions,
    }
    return body, refs


def noul_probability(answer):
    if not isinstance(answer, dict):
        return 0.0
    value = answer.get("noul")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return 0.0


def chat_json(payload, kind):
    endpoint = inception_base() + "/chat/completions"
    started = time.perf_counter()
    result = post_json(endpoint, inception_key(), payload)
    latency_ms = round((time.perf_counter() - started) * 1000)
    log_model_call(kind, endpoint, payload, result if isinstance(result, dict) else {}, latency_ms, kind)
    return result, latency_ms


def decompose_goal(goal):
    """One Mercury chat call. Any invalid result falls back to the original goal."""
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "Requirements",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "requirements": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "string"},
                    }
                },
                "required": ["requirements"],
                "additionalProperties": False,
            },
        },
    }
    payload = {
        "model": os.environ.get("TEXT_MODEL", "mercury-2.5"),
        "reasoning_effort": "instant",
        "temperature": 0.5,
        "max_completion_tokens": 1000,
        "response_format": schema,
        "messages": [
            {"role": "system", "content": DECOMPOSE_SYSTEM},
            {"role": "user", "content": json.dumps({"goal": goal})},
        ],
    }
    try:
        result, _latency = chat_json(payload, "text")
        output = json.loads(result["choices"][0]["message"]["content"])
        requirements = output["requirements"]
        if (
            set(output) != {"requirements"}
            or not isinstance(requirements, list)
            or not 1 <= len(requirements) <= 8
            or not all(isinstance(item, str) and item.strip() for item in requirements)
        ):
            raise ValueError("invalid requirements")
        decompose_goal.last_usage = result.get("usage", {}) if isinstance(result, dict) else {}
        return [item.strip() for item in requirements]
    except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        decompose_goal.last_usage = {}
        return [goal]


def verify_done(goal, requirements, page, history):
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "Completion",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "complete": {"type": "boolean"},
                    "missing": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["complete", "missing"],
                "additionalProperties": False,
            },
        },
    }
    payload = {
        "model": os.environ.get("TEXT_MODEL", "mercury-2.5"),
        "reasoning_effort": "low",
        "temperature": 0.5,
        "max_completion_tokens": 1500,
        "response_format": schema,
        "messages": [
            {"role": "system", "content": VERIFY_SYSTEM},
            {
                "role": "user",
                "content": json.dumps({
                    "goal": goal,
                    "requirements": requirements,
                    "page": {k: page.get(k) for k in ("url", "title", "text")},
                    "recent_actions": recent_actions(history),
                }),
            },
        ],
    }
    try:
        result, latency_ms = chat_json(payload, "text")
        output = json.loads(result["choices"][0]["message"]["content"])
        missing = output.get("missing")
        if not isinstance(output.get("complete"), bool) or not isinstance(missing, list):
            raise ValueError("invalid verifier")
        if not all(isinstance(item, str) for item in missing):
            raise ValueError("invalid verifier")
        return {
            "complete": output["complete"],
            "missing": missing,
            "latency_ms": latency_ms,
            "usage": result.get("usage", {}),
        }
    except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"complete": False, "missing": ["verifier failed"], "latency_ms": 0, "usage": {}}


def apply_done_gate(answer, reqs, verifier):
    """Drop DONE when a requirement or the verifier disagrees, then validate the replacement."""
    probabilities = dict(answer["probabilities"])
    blocked = any(item["probability"] < 0.85 for item in reqs) or not verifier.get("complete", False)
    if answer.get("choice") != "DONE" or not blocked:
        return answer, None
    remaining = {key: value for key, value in probabilities.items() if key != "DONE"}
    if not remaining:
        raise ValueError(INVALID_RESPONSE)
    total = sum(value for value in remaining.values() if isinstance(value, (int, float)))
    if total <= 0:
        share = 1 / len(remaining)
        remaining = {key: share for key in remaining}
    else:
        remaining = {key: value / total for key, value in remaining.items()}
    choice = max(remaining, key=remaining.get)
    rebuilt = {"choice": choice, "confidence": remaining[choice], "probabilities": remaining}
    validate_choice(rebuilt, remaining)
    return rebuilt, {
        "blocked": True,
        "reqs": {item["id"]: item["probability"] for item in reqs},
        "verifier": {k: verifier.get(k) for k in ("complete", "missing")},
    }


def choose_mercury(state, goal, history, requirements, progress):
    body, refs = mercury_body(state, goal, history, requirements, progress)
    url = inception_base() + "/decisions"
    started = time.perf_counter()
    result = post_json(url, inception_key(), body)
    verifier = {"complete": True, "missing": []}
    verifier_calls = 0
    try:
        answers = result["answers"]
        raw = answers.get("next_action", {})
        answer = {
            "choice": raw.get("choice"),
            "confidence": raw.get("confidence"),
            "probabilities": raw.get("probabilities"),
        }
        validate_choice(answer, body["questions"]["next_action"]["criteria"])
        reqs = []
        for index, text in enumerate(requirements, start=1):
            probability = noul_probability(answers.get(f"req_{index}"))
            reqs.append({"id": f"r{index}", "text": text, "probability": probability})
        if answer["choice"] == "DONE":
            if any(item["probability"] < 0.85 for item in reqs):
                verifier = {"complete": False, "missing": [
                    item["text"] for item in reqs if item["probability"] < 0.85
                ]}
            else:
                verifier = verify_done(goal, requirements, state, history)
                verifier_calls = 1
            answer, gate = apply_done_gate(
                answer,
                reqs,
                verifier if verifier_calls or any(item["probability"] < 0.85 for item in reqs) else {"complete": True},
            )
        else:
            gate = None
            validate_choice(answer, body["questions"]["next_action"]["criteria"])
    except (ValueError, KeyError, TypeError) as error:
        latency_ms = round((time.perf_counter() - started) * 1000)
        log_model_call("decision", url, body, result if isinstance(result, dict) else {}, latency_ms, f"error={error}")
        raise
    choice = answer["choice"]
    if choice in refs:
        operation = refs[choice]["operation"]
        target = refs[choice]["target"]
    elif choice == "DONE":
        operation, target = "DONE", None
    elif choice == "BLOCKED":
        operation, target = "BLOCKED", None
    else:
        operation, target = choice, None
    latency_ms = round((time.perf_counter() - started) * 1000)
    log_model_call(
        "decision",
        url,
        body,
        result,
        latency_ms,
        f"operation={operation} target={target} choice={choice} confidence={answer['confidence']}",
    )
    decision = {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": answer["confidence"],
        "probabilities": answer["probabilities"],
        "operation_probabilities": answer["probabilities"],
        "target_probabilities": {},
        "target_confidence": None,
        "raw_answers": result.get("answers"),
        "model": result.get("model", body["model"]),
        "provider": "mercury",
        "usage": result.get("usage", {}),
        "latency_ms": latency_ms,
        "request": redact_secrets(body),
        "requirements": reqs,
        "verifier_calls": verifier_calls,
    }
    if gate:
        decision["done_gate"] = gate
    if verifier_calls:
        extra = verifier.get("usage") if isinstance(verifier.get("usage"), dict) else {}
        decision["verifier_usage"] = extra
        decision["verifier_latency_ms"] = verifier.get("latency_ms", 0)
    return decision


def typesafe_body(state, goal, history, elements, targets, operations, guidance=()):
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {action['label']}",
                    "current_value": action.get("current_value", action.get("value", "")),
                    **{k: action[k] for k in ("role", "checked", "selected", "expanded", "pressed") if k in action},
                }
                for index, action in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    recent = recent_actions(history)
    recent.extend(
        {"action": "(guidance)", "kind": "guidance", "text": None, "page_changed": None, "note": item}
        for item in guidance
    )
    return {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": recent,
        },
        "questions": questions,
    }


def match_option(label, options):
    """Exact, then case-insensitive, then a unique prefix, then a unique contains. Ambiguous is no match."""
    if not isinstance(label, str) or not label.strip():
        return None
    enabled = [option for option in options or [] if not option.get("disabled")]

    def unique(predicate):
        found = [option for option in enabled if predicate(option)]
        return found[0] if len(found) == 1 else None

    exact = [option for option in enabled if option.get("label") == label]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    folded = label.casefold()
    insensitive = [option for option in enabled if str(option.get("label", "")).casefold() == folded]
    if len(insensitive) == 1:
        return insensitive[0]
    if len(insensitive) > 1:
        return None
    prefix = unique(lambda option: str(option.get("label", "")).casefold().startswith(folded))
    if prefix or sum(str(option.get("label", "")).casefold().startswith(folded) for option in enabled) > 1:
        return prefix
    return unique(lambda option: folded in str(option.get("label", "")).casefold())


def parse_field_text(result):
    output = json.loads(result["choices"][0]["message"]["content"])
    value = output["text"]
    if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise ValueError("invalid field text")
    return value


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def answer_context(goal, page, history, evidence=(), memo=(), partial=False):
    return {
        "goal": goal,
        "page": {"url": page.get("url"), "title": page.get("title"), "text": (page.get("text") or "")[:6000]},
        "evidence": [
            {"subgoal": item["subgoal"], "value": item["value"], "source": item["source"]} for item in evidence
        ],
        "facts": [item["fact"] for item in memo],
        "partial": partial,
        "recent_actions": [
            {k: h.get(k) for k in ("action", "text", "url")} for h in history[-10:]
        ],
    }


def track_context(goal, subgoals, memo, page, history):
    return {
        "goal": goal,
        "subgoals": [
            {"id": item["id"], "text": item["text"], "satisfied": bool(item.get("satisfied"))}
            for item in subgoals
        ],
        "known_facts": [item["fact"] for item in memo][-12:],
        "page": {"url": page.get("url"), "title": page.get("title"), "text": (page.get("text") or "")[:4000]},
        "recent_actions": [
            {k: h.get(k) for k in ("action", "text", "url")} for h in history[-6:]
        ],
    }


def gate_context(goal, subgoals, memo, page, history):
    context = track_context(goal, subgoals or [], memo, page, history)
    context["done_claimed"] = True
    return context


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "satisfied": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "value": {"type": "string"}},
                "required": ["id", "value"],
                "additionalProperties": False,
            },
        },
        "facts": {"type": "array", "items": {"type": "string"}},
        "complete": {"type": "boolean"},
        "missing": {"type": "string"},
    },
    "required": ["satisfied", "facts", "complete", "missing"],
    "additionalProperties": False,
}


def parse_judge(result):
    output = json.loads(result["choices"][0]["message"]["content"])
    if (
        not isinstance(output, dict)
        or not isinstance(output.get("satisfied"), list)
        or not isinstance(output.get("facts"), list)
        or not isinstance(output.get("complete"), bool)
        or not isinstance(output.get("missing"), str)
    ):
        raise ValueError("invalid judgment")
    satisfied = []
    for item in output["satisfied"][:8]:
        if (
            isinstance(item, dict)
            and isinstance(item.get("id"), int)
            and isinstance(item.get("value"), str)
            and item["value"].strip()
        ):
            satisfied.append({"id": item["id"], "value": item["value"].strip()[:500]})
    facts = [item.strip()[:300] for item in output["facts"][:8] if isinstance(item, str) and item.strip()]
    return {"satisfied": satisfied, "facts": facts, "complete": output["complete"], "missing": output["missing"][:500]}


def judge_progress(context, system):
    """One structured progress judgment. Raises ValueError when unusable; the caller decides."""
    return field_text(
        context, system=system, schema=JUDGE_SCHEMA, name="Progress", parse=parse_judge, label="judgment"
    )


def field_text(context, system=TEXT_VALUE, schema=None, name="FieldText", parse=None, label=None):
    parse = parse or parse_field_text
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.inceptionlabs.ai/v1").rstrip("/")
    model_name = os.environ.get("TEXT_MODEL", "mercury-2.5")
    inception = "api.inceptionlabs.ai" in base or model_name.startswith("mercury")
    if inception:
        key = os.environ.get("INCEPTION_API_KEY") or os.environ.get("TEXT_MODEL_API_KEY")
    else:
        key = os.environ.get("TEXT_MODEL_API_KEY") or os.environ.get("INCEPTION_API_KEY")
    if not key:
        raise ValueError(
            "TYPE_TEXT needs TEXT_MODEL_API_KEY or INCEPTION_API_KEY; "
            "no text is hardcoded or guessed by the executor."
        )
    if inception:
        reasoning = {"reasoning_effort": reasoning_effort()}
    else:
        if "api.deepseek.com/" in base:
            reasoning = {"thinking": {"type": "disabled"}}
        else:
            reasoning = {"reasoning": {"effort": "low"}}
        if "api.meta.ai/" in base:
            reasoning = {"reasoning_effort": "minimal"}
        if os.environ.get("TEXT_MODEL_REASONING") == "none" and "api.meta.ai/" not in base:
            reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    endpoint = base + "/chat/completions"
    try:
        max_tokens = max(1000, int(os.environ.get("TEXT_MODEL_MAX_TOKENS", "1024")))
    except ValueError:
        max_tokens = 1024
    text_schema = {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema
            or {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }
    payload = {
        "model": model_name,
        "max_tokens": max_tokens,
        "max_completion_tokens": max_tokens,
        "response_format": text_schema,
        **reasoning,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(context),
            },
        ],
    }
    def without_page_text(value):
        if not isinstance(value, dict):
            return value
        lean = dict(value)
        page = lean.get("page")
        if isinstance(page, dict):
            lean["page"] = {key: item for key, item in page.items() if key != "text"}
        return lean

    filtered = False

    def request_text():
        nonlocal payload, filtered
        try:
            return post_json(endpoint, key, payload)
        except ModelHTTPError as error:
            if error.code != "content_filter" or filtered:
                raise
            # Live news text can trip the provider's content filter. The goal,
            # field, title, and recent actions still identify the value.
            filtered = True
            payload = {
                **payload,
                "messages": [
                    payload["messages"][0],
                    {"role": "user", "content": json.dumps(without_page_text(context))},
                ],
            }
            print(
                "[jev:text] provider content filter; retrying without page text",
                file=sys.stderr,
                flush=True,
            )
            return post_json(endpoint, key, payload)

    result = {}
    for attempt in range(2):
        if attempt:
            payload = {**payload, "reasoning_effort": "low"}
        try:
            result = request_text()
        except ModelHTTPError as error:
            if error.code == "content_filter":
                print("[jev:text] error=provider content filter; step skipped", file=sys.stderr, flush=True)
                raise ValueError("Text helper was content-filtered; nothing typed.") from None
            raise
        try:
            value = parse(result)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            latency_ms = round((time.perf_counter() - started) * 1000)
            logged = result if isinstance(result, dict) else {}
            problem = f"error=no valid {label}" if label else "error=no valid field value"
            log_model_call("text", endpoint, payload, logged, latency_ms, problem)
            if attempt:
                if label:
                    print(f"[jev:text] {problem}; skipped", file=sys.stderr, flush=True)
                    raise ValueError(f"Text helper returned no valid {label}.") from None
                print("[jev:text] error=no valid field value; step skipped", file=sys.stderr, flush=True)
                raise ValueError("Text helper returned no valid field value; nothing typed.") from None
            continue
        latency_ms = round((time.perf_counter() - started) * 1000)
        log_model_call("text", endpoint, payload, result, latency_ms, f"value={value!r}"[:300])
        return value, {
            "model": model_name,
            "latency_ms": latency_ms,
            "usage": result.get("usage", {}) if isinstance(result, dict) else {},
        }
    raise ValueError("Text helper returned no valid field value; nothing typed.")
