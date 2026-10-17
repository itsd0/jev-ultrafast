#!/usr/bin/env python3
"""Run bench/tasks.yaml through the jev-server API and verify each outcome.

A DONE choice is not proof of success: every task carries independent checks
(URL pattern, final page text, or structured-answer rules) applied to the
stored task evidence after it reaches a terminal status.

Usage: uv run scripts/bench.py [--server URL] [--runs N] [--jobs N] [--task ID]
"""

import argparse
import json
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

TERMINAL = {"done", "blocked", "failed"}


def final_host(final_url):
    try:
        return (urlparse(final_url or "").hostname or "").lower()
    except ValueError:
        return ""


def normalize(text):
    """Collapse all whitespace so line breaks in page text don't fail a check."""
    return re.sub(r"\s+", " ", text or "").strip().lower()


def run_checks(task, evidence):
    """Apply every check; return a list of {check, passed, detail}."""
    final_url = evidence.get("final_url") or ""
    page_text = evidence.get("final_text") or ""
    answer = evidence.get("answer_text")
    results = []
    for check in task.get("checks", []):
        kind, value = check["type"], check.get("value")
        if kind == "url_regex":
            passed = re.search(check["pattern"], final_url) is not None
            detail = final_url[:120]
        elif kind == "url_host":
            passed = final_host(final_url) == value.lower()
            detail = final_host(final_url)
        elif kind == "text_contains":
            passed = normalize(value) in normalize(page_text)
            detail = f"{len(page_text)} chars of final text"
        elif kind == "answer_contains_all":
            missing = [v for v in check["values"] if normalize(v) not in normalize(answer)]
            passed = answer is not None and not missing
            detail = f"missing={missing}" if missing else f"{len(answer or '')} chars"
        elif kind == "answer_contains_min":
            hits = [v for v in check["values"] if normalize(v) in normalize(answer)]
            passed = len(hits) >= check["min"]
            detail = f"{len(hits)}/{len(check['values'])} present"
        elif kind == "answer_regex":
            passed = answer is not None and re.search(check["pattern"], answer) is not None
            detail = (answer or "(no answer)")[:120]
        elif kind == "answer_min_length":
            passed = answer is not None and len(answer) >= value
            detail = f"{len(answer or '')} chars"
        else:
            raise ValueError(f"Unknown check type: {kind}")
        results.append({"check": check, "passed": passed, "detail": detail})
    return results


def classify(task, evidence, check_results, timed_out):
    """One failure reason per failed run; None when all checks pass."""
    if all(r["passed"] for r in check_results):
        return None
    if timed_out:
        return "timeout"
    if evidence.get("status") == "failed":
        if "budget" in (evidence.get("error") or "").lower():
            return "hit budget"
        return "model error"
    expect = (task.get("expect_host") or "").lower()
    if expect and expect not in final_host(evidence.get("final_url")):
        return "wrong page"
    if evidence.get("status") == "blocked":
        reason = evidence.get("blocked_reason") or ""
        if "loop" in reason:
            return "loop"
        if "budget" in reason:
            return "hit budget"
        if "invalid" in reason:
            return "model error"
        if "shadow" in reason:
            return "element not found"
        if not reason:
            return "gave up"
        return f"blocked: {reason}"
    return "false DONE"


def run_once(client, task, rep, timeout_s):
    body = {"goal": task["goal"], "browser": "local", "max_decisions": task.get("max_decisions", 80)}
    if task.get("url"):
        body["url"] = task["url"]
    started = time.monotonic()
    task_id = client.post("/api/tasks", json=body).raise_for_status().json()["id"]
    evidence, timed_out = {}, False
    while True:
        evidence = client.get(f"/api/tasks/{task_id}").raise_for_status().json()
        if evidence.get("status") in TERMINAL:
            break
        if time.monotonic() - started > timeout_s:
            timed_out = True
            client.post(f"/api/tasks/{task_id}/stop")
            evidence = client.get(f"/api/tasks/{task_id}").raise_for_status().json()
            break
        time.sleep(2)
    checks = run_checks(task, evidence)
    steps = evidence.get("steps") or []
    return {
        "task": task["id"],
        "rep": rep,
        "task_id": task_id,
        "wall_s": round(time.monotonic() - started, 1),
        "status": evidence.get("status"),
        "steps": len(steps),
        "decisions": evidence.get("decisions_count"),
        "tokens": (evidence.get("input_tokens") or 0) + (evidence.get("output_tokens") or 0),
        "cost_usd": evidence.get("cost_usd_estimate") or 0,
        "final_url": evidence.get("final_url"),
        "blocked_reason": evidence.get("blocked_reason"),
        "error": (evidence.get("error") or "")[:200],
        "checks": checks,
        "reason": classify(task, evidence, checks, timed_out),
    }


def med(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def report(suite, records):
    lines = ["| task | ok | median time | median steps | median decisions | median tokens | cost | failures |"]
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for task in suite["tasks"]:
        runs = [r for r in records if r["task"] == task["id"]]
        if not runs:
            continue
        ok = sum(1 for r in runs if r["reason"] is None)
        failures = ", ".join(f"r{r['rep']}:{r['reason']}" for r in runs if r["reason"]) or "-"
        lines.append(
            f"| {task['id']} | {ok}/{len(runs)}"
            f" | {med([r['wall_s'] for r in runs])}s"
            f" | {med([r['steps'] for r in runs])}"
            f" | {med([r['decisions'] for r in runs])}"
            f" | {med([r['tokens'] for r in runs])}"
            f" | ${sum(r['cost_usd'] for r in runs):.4f}"
            f" | {failures} |"
        )
    ok = sum(1 for r in records if r["reason"] is None)
    lines.append(f"\nTotal: {ok}/{len(records)} passed.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the jev complexity benchmark.")
    parser.add_argument("--server", default="http://127.0.0.1:8770")
    parser.add_argument("--tasks", default=str(Path(__file__).resolve().parent.parent / "bench" / "tasks.yaml"))
    parser.add_argument("--runs", type=int, default=None)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--task", action="append", default=None, help="Only run these task ids.")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    suite = yaml.safe_load(Path(args.tasks).read_text())
    tasks = [t for t in suite["tasks"] if not args.task or t["id"] in args.task]
    if not tasks:
        print("No matching tasks.", file=sys.stderr)
        return 1
    reps = args.runs or suite.get("runs", 3)
    timeout_s = suite.get("timeout_s", 900)
    try:
        client = httpx.Client(base_url=args.server, timeout=30)
        client.get("/api/health").raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Server {args.server} is unreachable: {exc}", file=sys.stderr)
        return 2
    work = [(task, rep) for task in tasks for rep in range(1, reps + 1)]
    records = []
    with ThreadPoolExecutor(max_workers=max(args.jobs, 1)) as pool:
        pending = {pool.submit(run_once, client, task, rep, timeout_s): (task["id"], rep) for task, rep in work}
        for future in as_completed(pending):
            task_id, rep = pending[future]
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad run must not lose the suite
                record = {"task": task_id, "rep": rep, "reason": f"bench error: {exc}"}
            records.append(record)
            mark = "ok" if record.get("reason") is None else record.get("reason")
            print(f"[{len(records)}/{len(work)}] {task_id} rep {rep}: {mark}", flush=True)
    records.sort(key=lambda r: (r["task"], r.get("rep", 0)))
    print("\n" + report(suite, records))
    out = Path(args.out or f"artifacts/bench-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"server": args.server, "tasks": args.task, "records": records}, indent=2))
    print(f"\nResults: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
