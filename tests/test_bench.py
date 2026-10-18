"""Offline tests for scripts/bench.py checkers and failure classifier."""

import importlib.util
from pathlib import Path

bench = importlib.util.module_from_spec(
    spec := importlib.util.spec_from_file_location(
        "bench", Path(__file__).resolve().parent.parent / "scripts" / "bench.py"
    )
)
spec.loader.exec_module(bench)


def evidence(**overrides):
    base = {
        "status": "done",
        "final_url": "https://news.ycombinator.com/",
        "final_text": "1. Show HN: Something (123 points)",
        "answer_text": "1. Show HN: Something - 123 points\n2. Other - 45 points",
        "blocked_reason": None,
        "error": "",
    }
    base.update(overrides)
    return base


def test_url_and_text_checks():
    task = {"checks": [
        {"type": "url_regex", "pattern": "ycombinator"},
        {"type": "url_host", "value": "news.ycombinator.com"},
        {"type": "text_contains", "value": "points"},
    ]}
    assert all(r["passed"] for r in bench.run_checks(task, evidence()))
    failed = bench.run_checks(task, evidence(final_url="https://example.com/", final_text="empty"))
    assert [r["passed"] for r in failed] == [False, False, False]


def test_answer_checks_need_an_answer():
    task = {"checks": [
        {"type": "answer_contains_all", "values": ["points"]},
        {"type": "answer_contains_min", "values": ["Something", "Other", "Missing"], "min": 2},
        {"type": "answer_regex", "pattern": r"\d+ points"},
        {"type": "answer_min_length", "value": 10},
    ]}
    assert all(r["passed"] for r in bench.run_checks(task, evidence()))
    missing = bench.run_checks(task, evidence(answer_text=None))
    assert not any(r["passed"] for r in missing)
    short = bench.run_checks(task, evidence(answer_text="n/a"))
    assert [r["passed"] for r in short] == [False, False, False, False]


def test_classify_reasons():
    task = {"expect_host": "ycombinator.com", "checks": [{"type": "url_host", "value": "x.io"}]}
    bad = bench.run_checks(task, evidence())
    assert bench.classify(task, evidence(), bad, False) == "false DONE"
    assert bench.classify(task, evidence(status="failed"), bad, False) == "model error"
    assert bench.classify(task, evidence(), bad, True) == "timeout"
    assert bench.classify(task, evidence(final_url="https://example.com/"), bad, False) == "wrong page"
    blocked = evidence(status="blocked", final_url="https://news.ycombinator.com/")
    assert bench.classify(task, blocked | {"blocked_reason": "loop detected"}, bad, False) == "loop"
    assert bench.classify(task, blocked | {"blocked_reason": "decision budget"}, bad, False) == "hit budget"
    assert bench.classify(task, blocked | {"blocked_reason": "invalid decision response"}, bad, False) == "model error"
    shadow = blocked | {"blocked_reason": "possible closed shadow root"}
    assert bench.classify(task, shadow, bad, False) == "element not found"
    assert bench.classify(task, blocked, bad, False) == "gave up"


def test_text_check_ignores_line_breaks():
    task = {"checks": [{"type": "text_contains", "value": "Python Tutorial"}]}
    assert bench.run_checks(task, evidence(final_text="Python\nTutorial"))[0]["passed"]
    task = {"checks": [{"type": "answer_contains_all", "values": ["New  York"]}]}
    assert bench.run_checks(task, evidence(answer_text="New\nYork"))[0]["passed"]


def test_failed_budget_error_classifies_as_hit_budget():
    task = {"checks": [{"type": "url_host", "value": "x.io"}]}
    bad = bench.run_checks(task, evidence())
    failed = evidence(status="failed", error="ValueError: Stopped at the 60-action demo budget")
    assert bench.classify(task, failed, bad, False) == "hit budget"


def test_classify_success_is_none():
    task = {"checks": [{"type": "text_contains", "value": "points"}]}
    good = bench.run_checks(task, evidence())
    assert bench.classify(task, evidence(), good, False) is None


def test_text_regex_and_hotels_checks():
    task = {"checks": [{"type": "text_regex", "pattern": "(?i)lisbon"}]}
    assert bench.run_checks(task, evidence(final_text="Hotels in Lisbon"))[0]["passed"]
    assert not bench.run_checks(task, evidence(final_text="Hotels in Paris"))[0]["passed"]
    hotels = {"checks": [
        {"type": "url_regex", "pattern": "/travel/(hotels|search)"},
        {"type": "text_contains", "value": "per night"},
        {"type": "text_regex", "pattern": "(?i)lisbon"},
    ]}
    search_url = "https://www.google.com/travel/search?q=lisbon"
    good = evidence(final_url=search_url, final_text="Lisbon · $120 per night")
    assert all(r["passed"] for r in bench.run_checks(hotels, good))
    wrong_city = evidence(final_url=search_url, final_text="Paris · $120 per night")
    assert not all(r["passed"] for r in bench.run_checks(hotels, wrong_city))
