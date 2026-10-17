"""Local Chrome vs Browser Use Cloud timings for the same three tasks.

Live measurement: this script calls paid decision, text, and cloud browser APIs.
Tests never import it. Chrome must already listen on 127.0.0.1:9222 for the
local arm. The hotel fixture must already listen on 127.0.0.1:8766.

    uv run --env-file .env python scripts/measure_browsers.py

Cloud hotel is skipped unless JEV_FIXTURE_PUBLIC_URL points at a reachable copy.
One cloud browser is created per run and stopped in a finally block.
"""

import argparse
import json
import os
import platform
import re
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from measure_providers import summarize, verify_flights, verify_hotel, verify_wiki  # noqa: E402

import jev_ultrafast.browser as browser_module  # noqa: E402
import jev_ultrafast.cloud as cloud_module  # noqa: E402
from jev_ultrafast import Agent  # noqa: E402
from jev_ultrafast.config import resolve_config  # noqa: E402
from jev_ultrafast.demo import load_environment  # noqa: E402

WIKI = "https://en.wikipedia.org/wiki/Main_Page"
WIKI_GOAL = "Find and open the Wikipedia article about Gödel’s incompleteness theorems."
HOTEL_LOCAL = "http://127.0.0.1:8766/fixture.html?scenario=travel"
HOTEL_GOAL = (
    "Use the destination search and filters to find Design stays in Lisbon with Free cancellation, "
    "then open Casa Flora."
)
BILLING_URL = "https://api.browser-use.com/api/v2/billing/account"
DEFAULT_MIN_BALANCE_USD = 0.50
TASKS = ("wiki", "hotel", "flights")
RUNS = 3

_raw_cdp = browser_module.cdp
_original_cloud_start = cloud_module.start
_live = {"startup_s": None, "failures": []}


def _record_cdp(method, *args, **kwargs):
    try:
        return _raw_cdp(method, *args, **kwargs)
    except Exception as exc:
        _live["failures"].append({"method": method, "error": type(exc).__name__})
        raise


def _timed_cloud_start(config):
    started = time.perf_counter()
    try:
        return _original_cloud_start(config)
    finally:
        _live["startup_s"] = time.perf_counter() - started


def sanitize(value, key=""):
    text = str(value)
    text = re.sub(r"https?://\S+", "<url>", text)
    if key:
        text = text.replace(key, "<redacted>")
    return text[:300]


def billing_guard():
    """Refuse to create a cloud browser while another is active or the balance is low."""
    key = os.environ.get("BROWSER_USE_API_KEY", "")
    if not key:
        raise SystemExit("BROWSER_USE_API_KEY is missing; cloud arm cannot start")
    request = urllib.request.Request(BILLING_URL, headers={"X-Browser-Use-API-Key": key})
    with urllib.request.urlopen(request, timeout=30) as response:
        account = json.loads(response.read() or b"{}")
    try:
        balance = float(account["totalCreditsBalanceUsd"])
    except (KeyError, TypeError, ValueError):
        raise SystemExit("billing account response has no totalCreditsBalanceUsd") from None
    active = int(account.get("activeSessionCount") or 0)
    print(f"cloud balance {balance:.4f}", flush=True)
    if active > 0:
        raise SystemExit("an active cloud session already exists; stop it before measuring")
    minimum = float(os.environ.get("JEV_CLOUD_MIN_BALANCE_USD", DEFAULT_MIN_BALANCE_USD))
    if balance < minimum:
        raise SystemExit(f"cloud balance is below the {minimum:.2f} guard; aborting")


def hotel_url(browser):
    if browser == "local":
        return HOTEL_LOCAL
    public = os.environ.get("JEV_FIXTURE_PUBLIC_URL", "").strip()
    if not public:
        return None
    if "fixture.html" in public:
        return public
    return public.rstrip("/") + "/fixture.html?scenario=travel"


def configure(browser, task):
    os.environ["DECISION_PROVIDER"] = "typesafe"
    os.environ["TEXT_MODEL"] = "mercury-2.5"
    os.environ["TEXT_MODEL_BASE_URL"] = "https://api.inceptionlabs.ai/v1"
    os.environ["TEXT_MODEL_REASONING"] = "instant"
    if browser == "local":
        os.environ["JEV_BROWSER"] = "local"
        os.environ["BU_CDP_URL"] = os.environ.get("BU_CDP_URL", "http://127.0.0.1:9222")
        os.environ.pop("JEV_CLOUD_PROXY_COUNTRY", None)
        return
    os.environ.pop("BU_CDP_URL", None)
    os.environ["JEV_BROWSER"] = "cloud"
    os.environ["JEV_CLOUD_PROXY_COUNTRY"] = "us" if task == "flights" else "none"


def skipped_row(browser, task, run, detail):
    return {
        "provider": "typesafe",
        "browser": browser,
        "task": task,
        "run": run,
        "verified": "skipped",
        "total_s": None,
        "startup_s": None,
        "actions": None,
        "decision_calls": None,
        "median_decision_ms": None,
        "verifier_calls": None,
        "input_tokens": None,
        "output_tokens": None,
        "detail": detail,
        "error": None,
        "cdp_failures": [],
        "skipped": True,
    }


def run_one(browser, task, run):
    url = WIKI if task == "wiki" else hotel_url(browser)
    if task == "hotel" and url is None:
        return skipped_row(browser, task, run, "skipped (fixture not reachable)")
    if task == "flights":
        from examples.flights import GOALS, URL

        url, goal, verifier = URL, GOALS, verify_flights
    elif task == "wiki":
        url, goal, verifier = url, WIKI_GOAL, verify_wiki
    else:
        url, goal, verifier = url, HOTEL_GOAL, verify_hotel
    if browser == "cloud":
        billing_guard()
    configure(browser, task)
    resolve_config()
    print(f"\n=== {browser} {task} run {run} ===", flush=True)
    _live["startup_s"] = 0.0
    _live["failures"] = []
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
    started = time.perf_counter()
    try:
        agent = Agent(url, goal)
        for state in agent.run():
            last = state["history"][-1]["action"] if state["history"] else ""
            print(state["elapsed_ms"], state["status"], last, flush=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {sanitize(exc, os.environ.get('BROWSER_USE_API_KEY', ''))}"
        print("ERROR", error, flush=True)
    finally:
        if agent is not None:
            try:
                state = agent.snapshot()
            except Exception as exc:
                error = error or f"snapshot {type(exc).__name__}"
            try:
                state["verification_text"] = agent.browser.evaluate("document.body.innerText")
            except Exception:
                state["verification_text"] = ""
            try:
                agent.close()
            except Exception as exc:
                error = error or f"close {type(exc).__name__}"
    startup = _live["startup_s"] if browser == "cloud" else 0.0
    try:
        verified, detail = verifier(state)
    except Exception as exc:
        verified, detail = False, f"verify {type(exc).__name__}: {sanitize(exc)}"
    measured = summarize(state, verified, detail)
    row = {
        "provider": "typesafe",
        "browser": browser,
        "task": task,
        "run": run,
        "verified": bool(verified),
        "total_s": None if state.get("elapsed_ms") is None else round(state["elapsed_ms"] / 1000, 3),
        "startup_s": round(startup, 3),
        "actions": measured["actions"],
        "decision_calls": measured["decisions"],
        "median_decision_ms": measured["median_decision_ms"],
        "verifier_calls": measured["verifier_calls"],
        "input_tokens": measured["decision_tokens"]["prompt"] + measured["text_tokens"]["prompt"],
        "output_tokens": measured["decision_tokens"]["completion"] + measured["text_tokens"]["completion"],
        "detail": measured["detail"],
        "error": error,
        "cdp_failures": list({item["method"] for item in _live["failures"]}),
        "skipped": False,
        "wall_build_s": round(time.perf_counter() - started, 3),
    }
    print(
        f"verified={row['verified']} total={row['total_s']}s startup={row['startup_s']}s "
        f"actions={row['actions']} decisions={row['decision_calls']}",
        flush=True,
    )
    return row


def median(values):
    values = [value for value in values if value is not None]
    return round(statistics.median(values), 3) if values else None


def aggregate(rows):
    report = {"tasks": {}, "cdp_failures": []}
    for task in TASKS:
        runs = [row for row in rows if row["task"] == task]
        attempted = [row for row in runs if not row["skipped"]]
        usable = [row for row in attempted if row["total_s"] is not None and row["verified"] is True]
        report["tasks"][task] = {
            "runs": runs,
            "attempts": len(attempted),
            "verified": len(usable),
            "n": len(usable),
            "median_total_s": median([row["total_s"] for row in usable]),
            "median_startup_s": median([row["startup_s"] for row in usable]),
            "median_actions": median([row["actions"] for row in usable]),
            "median_decision_calls": median([row["decision_calls"] for row in usable]),
            "median_decision_ms": median([row["median_decision_ms"] for row in usable]),
            "median_verifier_calls": median([row["verifier_calls"] for row in usable]),
            "median_input_tokens": median([row["input_tokens"] for row in usable]),
            "median_output_tokens": median([row["output_tokens"] for row in usable]),
        }
        report["cdp_failures"] = sorted({name for row in runs for name in row["cdp_failures"]})
    return report


def table(rows):
    lines = [
        "| provider | browser | task | run | verified | total s | startup s | actions | "
        "decision calls | median decision ms | verifier calls | input tokens | output tokens |",
        "| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['provider']} | {row['browser']} | {row['task']} | {row['run']} | "
            f"{'skipped' if row['skipped'] else row['verified']} | "
            f"{'' if row['total_s'] is None else row['total_s']} | "
            f"{'' if row['startup_s'] is None else row['startup_s']} | "
            f"{'' if row['actions'] is None else row['actions']} | "
            f"{'' if row['decision_calls'] is None else row['decision_calls']} | "
            f"{'' if row['median_decision_ms'] is None else row['median_decision_ms']} | "
            f"{'' if row['verifier_calls'] is None else row['verifier_calls']} | "
            f"{'' if row['input_tokens'] is None else row['input_tokens']} | "
            f"{'' if row['output_tokens'] is None else row['output_tokens']} |"
        )
    return "\n".join(lines)


def overhead(local, remote):
    result = {}
    for task in TASKS:
        left, right = local["tasks"][task], remote["tasks"][task]
        if left["median_total_s"] is None or right["median_total_s"] is None:
            result[task] = None
            continue
        actions = right["median_actions"] or 1
        result[task] = round((right["median_total_s"] - left["median_total_s"]) * 1000 / actions, 1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "browser-measurement.json"))
    parser.add_argument("--browsers", nargs="+", choices=("local", "cloud"), default=["local", "cloud"])
    parser.add_argument("--runs", type=int, default=RUNS)
    parser.add_argument("--region", default=os.environ.get("JEV_REGION", "unspecified"))
    args = parser.parse_args()
    load_environment()
    browser_module.cdp = _record_cdp
    cloud_module.start = _timed_cloud_start
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "date": time.strftime("%Y-%m-%d"),
        "region": args.region,
        "platform": f"{platform.system()} {platform.release()}",
        "decision_provider": "typesafe",
        "text_model": "mercury-2.5",
        "text_reasoning": "instant",
        "runs_per_cell": args.runs,
        "cloud_proxy": {"wiki": "none", "hotel": "none", "flights": "us"},
        "arms": {},
        "cdp_failures": {},
        "medians": {},
        "per_action_overhead_ms": {},
    }
    rows = []
    for browser in args.browsers:
        for task in TASKS:
            for run in range(1, args.runs + 1):
                rows.append(run_one(browser, task, run))
                arm_rows = [row for row in rows if row["browser"] == browser]
                report["arms"][browser] = aggregate(arm_rows)
                output.write_text(json.dumps(report, indent=2) + "\n")
    for browser in args.browsers:
        report["medians"][browser] = {
            task: {
                key: report["arms"][browser]["tasks"][task][key]
                for key in ("median_total_s", "median_startup_s", "median_actions", "median_decision_ms")
            }
            for task in TASKS
        }
        report["cdp_failures"][browser] = report["arms"][browser]["cdp_failures"]
    if "local" in report["arms"] and "cloud" in report["arms"]:
        report["per_action_overhead_ms"] = overhead(report["arms"]["local"], report["arms"]["cloud"])
    print("\n" + table(rows), flush=True)
    print(json.dumps(report["medians"], indent=2), flush=True)
    print("per-action overhead ms", json.dumps(report["per_action_overhead_ms"]), flush=True)
    print("cdp failures", json.dumps(report["cdp_failures"]), flush=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"saved {output}", flush=True)


if __name__ == "__main__":
    main()
