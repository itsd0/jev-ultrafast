"""Wikipedia, hotel, and flights timings for TypeSafe and Mercury Decide.

Text is Mercury 2.5 with reasoning_effort=instant. No values from this file
are secrets. Chrome must already be listening on port 9222.

    BU_CDP_URL=http://127.0.0.1:9222 uv run --env-file .env python scripts/measure_providers.py
"""

import json
import os
import statistics
from pathlib import Path
from urllib.parse import unquote

from jev_ultrafast import Agent
from jev_ultrafast.demo import load_environment
from jev_ultrafast.model import reasoning_tokens, token_counts

ROOT = Path(__file__).resolve().parents[1]
WIKI = "https://en.wikipedia.org/wiki/Main_Page"
WIKI_GOAL = "Find and open the Wikipedia article about Gödel’s incompleteness theorems."


HOTEL_GOAL = (
    "Use the destination search and filters to find Design stays in Lisbon with Free cancellation, "
    "then open Casa Flora."
)


def summarize(state, verified, detail):
    decisions = state.get("decisions") or []
    history = state.get("history") or []
    decision_latency = [item.get("latency_ms") or 0 for item in decisions]
    text_latency = [item.get("text_latency_ms") or 0 for item in history if item.get("text_helper")]
    decision_prompt = decision_completion = decision_reasoning = 0
    verifier_calls = 0
    gates = []
    for item in decisions:
        verifier_calls += int(item.get("verifier_calls") or 0)
        if item.get("done_gate"):
            gates.append(item["done_gate"])
        for usage in (item.get("usage"), item.get("verifier_usage"), item.get("decompose_usage")):
            prompt, completion = token_counts(usage)
            decision_prompt += prompt
            decision_completion += completion
            decision_reasoning += reasoning_tokens(usage)
    text_prompt = text_completion = text_reasoning = 0
    for item in state.get("text_calls") or []:
        prompt, completion = token_counts(item.get("usage"))
        text_prompt += prompt
        text_completion += completion
        text_reasoning += reasoning_tokens(item.get("usage"))
    return {
        "wall_ms": state.get("elapsed_ms"),
        "verified": bool(verified),
        "detail": detail,
        "decisions": len(decisions),
        "actions": len(history),
        "status": state.get("status"),
        "decision_latency_ms": decision_latency,
        "median_decision_ms": statistics.median(decision_latency) if decision_latency else None,
        "text_latency_ms": text_latency,
        "median_text_ms": statistics.median(text_latency) if text_latency else None,
        "verifier_calls": verifier_calls,
        "done_gates": gates,
        "decision_tokens": {
            "prompt": decision_prompt,
            "completion": decision_completion,
            "reasoning": decision_reasoning,
        },
        "text_tokens": {
            "prompt": text_prompt,
            "completion": text_completion,
            "reasoning": text_reasoning,
        },
    }


def run_agent(url, goal, verify):
    agent = None
    error = None
    state = {
        "elapsed_ms": None,
        "decisions": [],
        "history": [],
        "text_calls": [],
        "page": {},
        "status": "error",
    }
    try:
        agent = Agent(url, goal)
        for state in agent.run():
            last = state["history"][-1]["action"] if state["history"] else ""
            print(state["elapsed_ms"], state["status"], last, flush=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print("ERROR", error, flush=True)
    finally:
        if agent is not None:
            try:
                state = agent.snapshot()
            except Exception as exc:
                error = error or f"snapshot {type(exc).__name__}: {exc}"
            try:
                state["verification_text"] = agent.browser.evaluate("document.body.innerText")
            except Exception:
                state["verification_text"] = ""
            try:
                agent.close()
            except Exception:
                pass
    try:
        verified, detail = verify(state)
    except Exception as exc:
        verified, detail = False, f"verify {type(exc).__name__}: {exc}"
    row = summarize(state, verified, detail)
    row["error"] = error
    return row


def verify_wiki(state):
    url = unquote((state.get("page") or {}).get("url") or "")
    ok = url.rstrip("/").endswith("Gödel's_incompleteness_theorems")
    return ok, url


def verify_hotel(state):
    page = state.get("page") or {}
    url = page.get("url") or ""
    text = state.get("verification_text") or ""
    ok = (
        state.get("status") == "done"
        and url.endswith("#casa-flora")
        and "Your filters: Design · Free cancellation enabled · Destination Lisbon" in text
    )
    return ok, {"url": url, "status": state.get("status")}


def verify_flights(state):
    from examples.flights import verify

    page = state.get("page") or {}
    result = verify(page)
    return result["passed"], result["checks"]


def median(values):
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def main():
    load_environment()
    os.environ["BU_CDP_URL"] = os.environ.get("BU_CDP_URL", "http://127.0.0.1:9222")
    os.environ["TEXT_MODEL"] = "mercury-2.5"
    os.environ["TEXT_MODEL_BASE_URL"] = "https://api.inceptionlabs.ai/v1"
    os.environ["TEXT_MODEL_REASONING"] = "instant"
    output = ROOT / "artifacts" / "provider-measurement.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"text_model": "mercury-2.5", "reasoning_effort": "instant", "arms": {}}
    arms = (
        ("typesafe_mercury_text", "typesafe", ("wiki", "hotel", "flights")),
    )
    for arm, provider, tasks in arms:
        os.environ["DECISION_PROVIDER"] = provider
        report["arms"][arm] = {"provider": provider, "tasks": {}}
        for task in tasks:
            rows = []
            repeats = {"wiki": 3, "hotel": 3, "flights": 5}.get(task, 3)
            for index in range(repeats):
                print(f"\n=== {arm} {task} run {index + 1} ===", flush=True)
                if task == "wiki":
                    row = run_agent(WIKI, WIKI_GOAL, verify_wiki)
                elif task == "hotel":
                    row = run_agent(
                        "http://127.0.0.1:8766/fixture.html?scenario=travel",
                        HOTEL_GOAL,
                        verify_hotel,
                    )
                else:
                    from examples.flights import GOALS, URL

                    row = run_agent(URL, GOALS, verify_flights)
                row["run"] = index + 1
                rows.append(row)
                report["arms"][arm]["tasks"][task] = {
                    "runs": rows,
                    "median_wall_ms": median([item["wall_ms"] for item in rows]),
                    "median_decision_ms": median([item["median_decision_ms"] for item in rows]),
                    "median_text_ms": median([item["median_text_ms"] for item in rows]),
                    "verified": sum(bool(item["verified"]) for item in rows),
                }
                output.write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps({k: row[k] for k in row if k != "done_gates"}, indent=2), flush=True)
                if row.get("done_gates"):
                    print("DONE gates", json.dumps(row["done_gates"]), flush=True)
    lines = [
        "| provider | task | run | verified | total s | actions | decision calls | "
        "median decision ms | verifier calls | input tokens | output tokens |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm, data in report["arms"].items():
        for task, block in data["tasks"].items():
            for run in block["runs"]:
                tokens_in = run["decision_tokens"]["prompt"] + run["text_tokens"]["prompt"]
                tokens_out = run["decision_tokens"]["completion"] + run["text_tokens"]["completion"]
                wall = run["wall_ms"]
                lines.append(
                    f"| {data['provider']} | {task} | {run['run']} | {run['verified']} | "
                    f"{'' if wall is None else round(wall / 1000, 3)} | {run['actions']} | {run['decisions']} | "
                    f"{run['median_decision_ms']} | {run['verifier_calls']} | {tokens_in} | {tokens_out} |"
                )
    print("\n".join(lines), flush=True)
    output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
