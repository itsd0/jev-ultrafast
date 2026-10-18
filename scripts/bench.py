#!/usr/bin/env python3
"""Run bench/tasks.yaml through the jev-server API and verify each outcome.

A DONE choice is not proof of success: every task carries independent checks
(URL pattern, final page text, or structured-answer rules) applied to the
stored task evidence after it reaches a terminal status.

Usage: uv run scripts/bench.py [--server URL] [--runs N] [--jobs N] [--task ID]
"""

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jev_ultrafast.trace import diagnose_trace

TERMINAL = {"done", "blocked", "failed"}
TRACE_CAUSE_LABELS = {"budget": "hit budget", "blocked: gave up": "gave up"}


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
        elif kind == "text_regex":
            passed = re.search(check["pattern"], page_text or "") is not None
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


def classify(task, evidence, check_results, timed_out, trace_events=None):
    """One failure reason per failed run; None when all checks pass.

    A trace classifies from evidence (verify failures, guards, errors) instead
    of the old by-hand status mapping, which stays as the fallback.
    """
    if all(r["passed"] for r in check_results):
        return None
    if trace_events:
        cause, _evidence = diagnose_trace(trace_events)
        if cause:
            return TRACE_CAUSE_LABELS.get(cause, cause)
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
    try:
        trace_events = client.get(f"/api/tasks/{task_id}/trace").raise_for_status().json().get("events") or []
    except Exception:
        trace_events = []
    cause, diagnosis = diagnose_trace(trace_events) if trace_events else (None, "")
    return {
        "task": task["id"],
        "rep": rep,
        "task_id": task_id,
        "run_id": task_id,
        "diagnosis": f"{cause} — {diagnosis}" if cause else "",
        "trace_events": len(trace_events),
        "wall_s": round(time.monotonic() - started, 1),
        "status": evidence.get("status"),
        "steps": len(steps),
        "decisions": evidence.get("decisions_count"),
        "tokens": (evidence.get("input_tokens") or 0) + (evidence.get("output_tokens") or 0),
        "cost_usd": evidence.get("cost_usd_estimate") or 0,
        "final_url": evidence.get("final_url"),
        "blocked_reason": evidence.get("blocked_reason"),
        "partial": bool(evidence.get("partial")),
        "error": (evidence.get("error") or "")[:200],
        "checks": checks,
        "reason": classify(task, evidence, checks, timed_out, trace_events),
        "env_blocked": env_blocked(trace_events),
    }


def med(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


TRANSPORT_WORDS = ("connection", "http 4", "http 5", "429", "timeout", "unavailable")


def env_blocked(trace_events):
    """Env-blocked evidence, or None. A wall (captcha/transport error) plus no
    alternate route left (a navigate fallback with nowhere to go)."""
    guards = [e.get("data") or {} for e in trace_events or [] if e.get("kind") == "guard"]
    wall = next((g for g in guards if g.get("type") == "captcha"), None)
    if wall is None:
        for event in trace_events or []:
            if event.get("kind") != "error":
                continue
            data = event.get("data") or {}
            message = f"{data.get('type', '')} {data.get('message', '')}".lower()
            if any(word in message for word in TRANSPORT_WORDS):
                wall = data
                break
    if wall is None:
        return None
    stranded = next(
        (g for g in guards if g.get("type") == "navigate_fallback" and g.get("fallback") is None),
        None,
    )
    if stranded is None:
        return None
    where = wall.get("host") or f"{wall.get('type')}: {(wall.get('message') or '')[:60]}"
    return f"wall at {where}; no search route left"


def summarize(suite, records):
    """Pass@1, pass@3 per task, and the env-excluded rate with excluded runs."""
    tasks = []
    for task in suite["tasks"]:
        runs = [r for r in records if r["task"] == task["id"]]
        if not runs:
            continue
        ok = sum(1 for r in runs if r["reason"] is None)
        tasks.append({"task": task["id"], "runs": len(runs), "ok": ok, "pass_at_3": ok > 0})
    ok = sum(t["ok"] for t in tasks)
    total = sum(t["runs"] for t in tasks)
    excluded = [r for r in records if r.get("reason") is not None and r.get("env_blocked")]
    return {
        "tasks": tasks,
        "pass_at_1": {"ok": ok, "total": total},
        "pass_at_3": {"ok": sum(1 for t in tasks if t["pass_at_3"]), "total": len(tasks)},
        "env_excluded": {
            "ok": ok,
            "total": total - len(excluded),
            "excluded": [
                {"task": r["task"], "rep": r.get("rep"), "evidence": r.get("env_blocked")}
                for r in excluded
            ],
        },
    }


def git_info():
    """The tree the bench ran on: HEAD sha, dirty flag, and diff hash."""
    root = Path(__file__).resolve().parent.parent
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "HEAD"], cwd=root, capture_output=True
        ).stdout
    except OSError:
        return {"sha": None, "dirty": None, "diff_hash": None}
    return {
        "sha": sha or None,
        "dirty": bool(porcelain),
        "diff_hash": hashlib.sha256(diff).hexdigest(),
    }


def report(suite, records):
    lines = ["| task | ok | pass@3 | median time | median steps | median decisions | median tokens | cost | failures |"]
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for task in suite["tasks"]:
        runs = [r for r in records if r["task"] == task["id"]]
        if not runs:
            continue
        ok = sum(1 for r in runs if r["reason"] is None)
        failures = ", ".join(
            f"r{r['rep']}:{r['reason']}{'*' if r.get('partial') else ''}" for r in runs if r["reason"]
        ) or "-"
        lines.append(
            f"| {task['id']} | {ok}/{len(runs)} | {'yes' if ok else 'no'}"
            f" | {med([r['wall_s'] for r in runs])}s"
            f" | {med([r['steps'] for r in runs])}"
            f" | {med([r['decisions'] for r in runs])}"
            f" | {med([r['tokens'] for r in runs])}"
            f" | ${sum(r['cost_usd'] for r in runs):.4f}"
            f" | {failures} |"
        )
    summary = summarize(suite, records)
    at1, at3, env = summary["pass_at_1"], summary["pass_at_3"], summary["env_excluded"]
    lines.append(f"\nTotal: {at1['ok']}/{at1['total']} passed. (* = partial answer attached)")
    lines.append(f"pass@3: {at3['ok']}/{at3['total']} tasks passed at least once.")
    if env["excluded"]:
        listed = ", ".join(
            f"{item['task']} r{item['rep']} ({item['evidence']})" for item in env["excluded"]
        )
        lines.append(f"Env-excluded: {env['ok']}/{env['total']} passed ({len(env['excluded'])} excluded: {listed}).")
    else:
        lines.append(f"Env-excluded: {env['ok']}/{env['total']} passed (0 excluded).")
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
    git = git_info()
    print(f"Bench on {git['sha']} (dirty={git['dirty']}, diff={git['diff_hash'][:12]})")
    print("\n" + report(suite, records))
    out = Path(args.out or f"artifacts/bench-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "server": args.server, "tasks": args.task, "git": git,
                "summary": summarize(suite, records), "records": records,
            },
            indent=2,
        )
    )
    print(f"\nResults: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
