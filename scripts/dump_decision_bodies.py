"""Print hotel-fixture decision bodies for TypeSafe and Mercury. No model APIs.

Monkeypatches post_json. Needs the local fixture server and Chrome on port 9222.

    BU_CDP_URL=http://127.0.0.1:9222 uv run python scripts/dump_decision_bodies.py
"""

import json
import os
import sys
from pathlib import Path

from jev_ultrafast.browser import Browser
from jev_ultrafast.model import choose

ROOT = Path(__file__).resolve().parents[1]


def chars(value):
    return len(json.dumps(value, ensure_ascii=False))


def fake_answer(body):
    answers = {}
    for name, question in (body.get("questions") or {}).items():
        if question.get("type") == "noul":
            answers[name] = {"type": "noul", "noul": 0.1}
            continue
        criteria = question.get("criteria") or {}
        if not criteria:
            continue
        choice = next((item for item in criteria if item not in {"DONE", "BLOCKED"}), next(iter(criteria)))
        answers[name] = {
            "type": "choice",
            "choice": choice,
            "confidence": 1,
            "probabilities": {item: float(item == choice) for item in criteria},
        }
    return {"model": "offline", "answers": answers, "usage": {}}


def install_capture(model):
    captured = []

    def post(_url, _key, body):
        captured.append(json.loads(json.dumps(body)))
        return fake_answer(body)

    model.post_json = post
    return captured


def section_report(body):
    state = body.get("state") or {}
    rows = [
        ("state.page", chars(state.get("page"))),
        ("state.elements", chars(state.get("elements"))),
        ("state.recent_actions", chars(state.get("recent_actions"))),
        ("state.goal", chars(state.get("goal"))),
        ("state.rules", chars(state.get("rules"))),
        ("state.requirements", chars(state.get("requirements"))),
    ]
    questions = body.get("questions") or {}
    for name, question in questions.items():
        rows.append((f"q.{name}.instructions", chars(question.get("instructions"))))
        rows.append((f"q.{name}.criteria", chars(question.get("criteria"))))
    return rows


def criteria_fields(body):
    found = set()
    missing_examples = []
    for name, question in (body.get("questions") or {}).items():
        criteria = question.get("criteria") or {}
        if not isinstance(criteria, dict):
            continue
        for key, value in criteria.items():
            if isinstance(value, dict):
                found.update(value)
            elif name not in {"operation"} and key not in {"DONE", "BLOCKED"}:
                missing_examples.append((name, key, type(value).__name__))
    return found, missing_examples[:5]


def capture(model, provider, page, history):
    os.environ["DECISION_PROVIDER"] = provider
    os.environ["TYPESAFE_API_KEY"] = "offline"
    os.environ["INCEPTION_API_KEY"] = "offline"
    captured = install_capture(model)
    if provider == "mercury" and hasattr(model, "choose_mercury"):
        requirements = [GOAL]
        if hasattr(model, "decompose_goal"):
            requirements = model.decompose_goal(GOAL)
        model.choose_mercury(page, GOAL, history, requirements, {})
    else:
        choose(page, GOAL, history)
    return captured[-1]


def click_label(browser, label):
    page = browser.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("label") == label and a.get("kind") != "scroll")
    browser.act(action, page)
    browser.wait_until_ready()
    return action


def load_goal():
    sys.path.insert(0, str(ROOT))
    from scripts.smoke import GOALS

    return "\n".join(GOALS).strip() if not isinstance(GOALS, str) else GOALS.strip()


def main():
    global GOAL
    GOAL = load_goal()
    import jev_ultrafast.model as model

    os.environ.setdefault("BU_CDP_URL", "http://127.0.0.1:9222")
    browser = Browser("http://127.0.0.1:8766/fixture.html?scenario=travel")
    history = []
    stages = []
    try:
        browser.wait_until_ready()
        stages.append(("start", browser.observe(screenshot=False), list(history)))
        action = click_label(browser, "Free cancellation")
        history.append({
            "action": action["label"],
            "kind": action["kind"],
            "text": None,
            "page_changed": True,
        })
        stages.append(("after Free cancellation", browser.observe(screenshot=False), list(history)))
        action = click_label(browser, "View Casa Flora")
        history.append({
            "action": action["label"],
            "kind": action["kind"],
            "text": None,
            "page_changed": True,
        })
        stages.append(("Casa Flora detail", browser.observe(screenshot=False), list(history)))
    finally:
        browser.close()

    for title, page, hist in stages:
        print(f"\n## {title}")
        print(f"url={page.get('url', '')[:120]}")
        bodies = {}
        for provider in ("typesafe", "mercury"):
            bodies[provider] = capture(model, provider, page, hist)
            print(f"\n{provider} chars")
            for name, count in section_report(bodies[provider]):
                print(f"  {name}: {count}")
            fields, samples = criteria_fields(bodies[provider])
            print(f"  criteria object fields: {sorted(fields) or '(none)'}")
            if samples:
                print(f"  non-object criteria sample: {samples}")
        type_fields, _ = criteria_fields(bodies["typesafe"])
        mercury_fields, mercury_samples = criteria_fields(bodies["mercury"])
        for field in ("current_value", "checked", "selected"):
            print(
                f"gap {field}: typesafe={field in type_fields} mercury={field in mercury_fields}"
            )
        type_state = bodies["typesafe"].get("state") or {}
        mercury_state = bodies["mercury"].get("state") or {}
        type_instructions = json.dumps(bodies["typesafe"].get("questions"))
        mercury_instructions = json.dumps(bodies["mercury"].get("questions"))
        print(f"goal in mercury state={bool(mercury_state.get('goal'))} typesafe state={bool(type_state.get('goal'))}")
        print(
            "rules in mercury state="
            f"{bool(mercury_state.get('rules'))} typesafe instructions={'rules' in type_instructions}"
        )
        print(f"goal in mercury instructions={'<goal>' in mercury_instructions or 'goal' in mercury_instructions}")
        print(f"mercury non-object criteria={bool(mercury_samples)}")


if __name__ == "__main__":
    main()
