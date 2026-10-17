"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import sys
import time

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)


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


def log_model_call(kind, url, body, result, latency_ms, summary):
    """Server-side timing log. Credentials are never passed here, so they cannot leak."""
    level = debug_level()
    if level < 1:
        return
    usage = result.get("usage") if isinstance(result, dict) else None
    prompt, completion = token_counts(usage)
    seconds = max(latency_ms / 1000, 1e-6)
    rate = f"{completion / seconds:.1f}" if completion else "n/a"
    print(
        f"[jev:{kind}] model={body.get('model', '?')} latency_ms={latency_ms} "
        f"prompt={prompt} completion={completion} tok/s={rate} {summary}",
        file=sys.stderr,
        flush=True,
    )
    if level >= 2:
        for label, payload in (("request", {"url": url, **body}), ("response", result)):
            print(f"[jev:{kind}] {label}: {json.dumps(payload)[:4000]}", file=sys.stderr, flush=True)


def post_json(url, key, body):
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            if debug_level() >= 1:
                print(
                    f"[jev:http] retry url={url} status={response.status_code} attempt={attempt + 1}",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


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
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT", "upload": "UPLOAD_FILE"}
    for action in actions:
        kind = action["kind"]
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
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
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


def choose(state, goal, history):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
        "UPLOAD_FILE": "Upload an allowlisted file. Choose the offered key. A key is not a filesystem path.",
        "SWITCH_TAB": "Switch to another tab opened by this task. Choose an offered tab id.",
        "SCROLL_DOWN": "Scroll down the document or an offered container.",
        "SCROLL_UP": "Scroll up the document or an offered container.",
        "SCROLL_LEFT": "Scroll an offered container left.",
        "SCROLL_RIGHT": "Scroll an offered container right.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    url = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
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
                a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()
            }
        else:
            choice = controls[operation]["id"] if operation in controls else operation
            probabilities[choice] = operation_answer["probabilities"][operation]
    except (ValueError, KeyError, TypeError) as error:
        latency_ms = round((time.perf_counter() - started) * 1000)
        log_model_call("typesafe", url, body, result, latency_ms, f"error={error}")
        raise
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


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_text(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if "api.meta.ai/" in base:
        reasoning = {"reasoning_effort": "minimal"}
    if os.environ.get("TEXT_MODEL_REASONING") == "none" and "api.meta.ai/" not in base:
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    endpoint = base + "/chat/completions"
    try:
        max_tokens = max(256, int(os.environ.get("TEXT_MODEL_MAX_TOKENS", "1024")))
    except ValueError:
        max_tokens = 1024
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        **reasoning,
        "messages": [
            {"role": "system", "content": TEXT_VALUE},
            {
                "role": "user",
                "content": json.dumps(context),
            },
        ],
    }
    result = post_json(endpoint, key, payload)
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        latency_ms = round((time.perf_counter() - started) * 1000)
        log_model_call("text", endpoint, payload, result, latency_ms, "error=no valid field value")
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    latency_ms = round((time.perf_counter() - started) * 1000)
    log_model_call("text", endpoint, payload, result, latency_ms, f"value={value!r}")
    return value, {
        "model": model,
        "latency_ms": latency_ms,
        "usage": result.get("usage", {}),
    }
