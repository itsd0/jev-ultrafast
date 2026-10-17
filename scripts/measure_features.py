"""Local fixture timing with feature flags off vs the defaults. No paid APIs.

Runs each HTML fixture under tests/fixtures three times. Prints median wall time,
CDP call count, and snapshot time. Wikipedia, hotel, and flights are not called:
those need a live browser plus paid model APIs. `JEV_FRAMES=all` is not the default
and is expected to be slower because each cross-origin frame adds a snapshot.

Usage:
    uv run python scripts/measure_features.py
    uv run python scripts/measure_features.py --output artifacts/feature-measurement.json
"""

import argparse
import json
import statistics
import time
from pathlib import Path
from urllib.parse import quote

from jev_ultrafast import browser as browser_module
from jev_ultrafast.browser import Browser
from jev_ultrafast.config import resolve_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TASKS = ("shadow.html", "iframe.html", "scroll.html", "keyboard.html", "select.html", "upload.html")
UPLOAD = ROOT / "artifacts" / "measure-upload.txt"


def data_url(name):
    return "data:text/html," + quote((FIXTURES / name).read_text())


def median(values):
    return statistics.median(values) if values else None


def config_for(flags_off):
    if not flags_off:
        return resolve_config(files={"resume": str(UPLOAD)})
    return resolve_config(
        shadow_dom=False,
        frames="off",
        tabs=False,
        nested_scroll=False,
        keyboard=False,
        large_select=False,
        vision=False,
        files=None,
    )


def run_pass(config):
    calls = []
    snapshot_ms = []
    raw = browser_module.cdp

    def timed(method, *args, **kwargs):
        started = time.perf_counter()
        result = raw(method, *args, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        calls.append((method, elapsed, kwargs.get("expression", "")))
        return result

    browser_module.cdp = timed
    browser = None
    try:
        browser = Browser("about:blank", config=config)
        calls.clear()
        started = time.perf_counter()
        for name in TASKS:
            browser.call("Page.navigate", url=data_url(name))
            browser.wait_until_ready()
            snap_started = time.perf_counter()
            browser.observe(screenshot=False)
            snapshot_ms.append((time.perf_counter() - snap_started) * 1000)
        wall_ms = (time.perf_counter() - started) * 1000
        return {
            "wall_ms": round(wall_ms, 1),
            "cdp_calls": len(calls),
            "snapshot_ms": round(sum(snapshot_ms), 1),
        }
    finally:
        browser_module.cdp = raw
        if browser is not None:
            browser.close()


def summarize(runs):
    return {
        "runs": len(runs),
        "median_wall_ms": round(median([run["wall_ms"] for run in runs]), 1),
        "median_cdp_calls": median([run["cdp_calls"] for run in runs]),
        "median_snapshot_ms": round(median([run["snapshot_ms"] for run in runs]), 1),
        "samples": runs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    UPLOAD.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD.write_text("resume")
    report = {"tasks": list(TASKS), "repeats": args.repeats, "live_model_tasks": "not run"}
    try:
        report["flags_off"] = summarize([run_pass(config_for(True)) for _ in range(args.repeats)])
        report["defaults"] = summarize([run_pass(config_for(False)) for _ in range(args.repeats)])
        off = report["flags_off"]["median_wall_ms"]
        on = report["defaults"]["median_wall_ms"]
        report["wall_ratio"] = round(on / off, 3) if off else None
        report["measured"] = True
    except Exception as exc:
        report["measured"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
    if not report["measured"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
