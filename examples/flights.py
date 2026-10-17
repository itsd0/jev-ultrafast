"""Live Google Flights search. Calls the configured decision model; never selects or books a flight."""

import argparse
import base64
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jev_ultrafast import Agent

URL = "https://www.google.com/travel/flights?hl=en"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
WEEKDAYS_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS_SHORT = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def trip_day(today=None):
    """A date Google Flights still offers. September 20, 2026 is now in the past."""
    return (today or date.today()) + timedelta(days=30)


TRIP_DAY = trip_day()
TRIP_ISO = TRIP_DAY.isoformat()
TRIP_SHORT = f"{WEEKDAYS_SHORT[TRIP_DAY.weekday()]}, {MONTHS_SHORT[TRIP_DAY.month - 1]} {TRIP_DAY.day}"
TRIP_LONG = f"{WEEKDAYS[TRIP_DAY.weekday()]}, {MONTHS[TRIP_DAY.month - 1]} {TRIP_DAY.day}"
GOALS = (
    f"Find one-way flights from Zurich to London on {TRIP_LONG}, {TRIP_DAY.year} ({TRIP_ISO}), "
    "for one adult in economy. "
    "Stop when matching flight options are visible. Do not select or book a flight."
)


def verify(page, day=None):
    """Independent checks on the resulting page, not the model's DONE answer."""
    day = day or TRIP_DAY
    iso = day.isoformat()
    short = f"{WEEKDAYS_SHORT[day.weekday()]}, {MONTHS_SHORT[day.month - 1]} {day.day}"
    long = f"{WEEKDAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}"
    parsed = urlparse(page["url"])
    encoded = parse_qs(parsed.query).get("tfs", [""])[0]
    try:
        date_in_url = iso.encode() in base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError:
        date_in_url = False
    actions = page["actions"]
    values = {a["label"].strip(): a.get("value") for a in actions}
    flights = [a["label"] for a in actions if "Select flight" in a["label"]]
    checks = {
        "search_page": parsed.hostname == "www.google.com" and parsed.path == "/travel/flights/search",
        "one_way": values.get("Change ticket type. One way") == "One way",
        "origin": values.get("Where from?") == "Zürich",
        "destination": values.get("Where to?") == "London",
        "date": values.get("Departure") == short,
        "year": date_in_url or f"departing {iso}" in page["text"],
        "results": bool(flights) and all(long in flight for flight in flights),
    }
    return {"passed": all(checks.values()), "checks": checks, "visible_flights": flights}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/flights/latest")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)
    agent = Agent(URL, GOALS)
    try:
        for state in agent.run():
            last = state["history"][-1] if state["history"] else {}
            print(state["elapsed_ms"], state["status"], last.get("action", ""), flush=True)
    finally:
        state = agent.snapshot()
        state["verification"] = verify(state["page"])
        (folder / "state.json").write_text(json.dumps(state, indent=2))
        (folder / "session.json").write_text(
            json.dumps({"target": agent.browser.target, "session": agent.browser.session})
        )
        if not args.keep_open:
            agent.close()
    print(json.dumps(state["verification"], indent=2))
    if not state["verification"]["passed"]:
        raise SystemExit("Final page did not satisfy the route/date checks")


if __name__ == "__main__":
    main()
