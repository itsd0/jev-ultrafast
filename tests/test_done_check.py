"""Deterministic pre-done evidence check. Offline; goals quoted from the bench."""

from jev_ultrafast.done_check import check_done, goal_entities

FLIGHTS = (
    "On Google Flights, search a round trip from Zurich to London, departing June 12 2027 "
    "and returning June 19 2027, for 2 adults, and filter to nonstop flights only."
)
HOTELS = (
    "On Google Hotels, search hotels in Lisbon for June 12 to June 19 2027, 2 adults, "
    "set the maximum price to $150 per night, and sort by price from low to high."
)
BANGKOK = (
    "Find the population of Bangkok on Wikipedia, then find Bangkok's current weather. "
    "Report both the population and the current temperature."
)
PLANETS = (
    "On Wikipedia, visit the articles for Mercury, Venus, Earth, Mars, Jupiter, Saturn, "
    "Uranus, Neptune, Pluto, and Ceres, and report how many moons each one has."
)
EBAY = "Search eBay for 'usb-c hub' and report the price and the seller rating of the first 3 results."
HNPAGE3 = (
    "On Hacker News, go to the 3rd page of stories (click More twice) and report the title "
    "of the first story there."
)
USDTHB = "Find how many Thai Baht 100 US dollars buys right now and report the amount."
WIKI404 = "Find the Wikipedia article on Marie Curie."
MDN = "On MDN Web Docs, find the Array.prototype.flatMap page and report its syntax line."
JSONDUMPS = "In the official Python documentation, find the json.dumps function and report its full signature."
W3SCHOOLS = "On W3Schools, dismiss any cookie or popup dialogs and open the Python tutorial page."
HNTOP5 = "List the top 5 stories on the Hacker News front page right now, each with its points score."
QUOTES = "On quotes.toscrape.com, go to page 3 and report the author of the first quote on that page."


def test_entities_skip_verbs_months_numbers():
    assert goal_entities(FLIGHTS) == ["Google Flights", "Zurich", "London"]
    assert goal_entities(HOTELS) == ["Google Hotels", "Lisbon"]
    assert goal_entities(EBAY) == ["usb-c hub", "eBay"]
    assert goal_entities(BANGKOK) == ["Bangkok", "Wikipedia"]
    assert goal_entities(HNPAGE3) == ["Hacker News"]
    assert goal_entities("Click More twice, then press Next and Close the dialog.") == []
    assert goal_entities(WIKI404) == ["Wikipedia", "Marie Curie"]
    assert goal_entities(USDTHB) == ["Thai Baht", "US"]
    assert goal_entities(MDN) == ["Array.prototype.flatMap", "MDN Web Docs"]
    assert goal_entities(JSONDUMPS) == ["json.dumps", "Python"]
    assert goal_entities(W3SCHOOLS) == ["W3Schools", "Python"]
    assert goal_entities(HNTOP5) == ["Hacker News"]
    assert goal_entities(QUOTES) == ["quotes.toscrape.com"]
    assert "Mercury" in goal_entities(PLANETS) and "Ceres" in goal_entities(PLANETS)


def test_wrong_origin_is_missing():
    verdict = check_done(
        FLIGHTS, text="Amsterdam to London flights", title="Google Flights",
        memo=[{"fact": "22 results", "source": "https://www.google.com/travel/flights/search"}],
        visited=["https://www.google.com/travel/flights/search"],
        widget_commits=3, controls=[],
    )
    assert verdict["ok"] is False
    assert any("Zurich" in item for item in verdict["missing"])
    assert "Google Flights" in verdict["done"]


def test_correct_run_passes_with_url_and_memo_evidence():
    verdict = check_done(
        PLANETS, text="Ceres has 0 moons", title="Ceres - Wikipedia",
        memo=[{"fact": f"{name} moons noted", "source": f"https://en.wikipedia.org/wiki/{name}"}
              for name in ("Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn",
                           "Uranus", "Neptune", "Pluto")],
        visited=[f"https://en.wikipedia.org/wiki/{name}" for name in ("Mercury", "Ceres")],
        controls=[],
    )
    assert verdict == {"ok": True, "missing": [], "done": verdict["done"]}
    assert "Ceres" in verdict["done"] and "Wikipedia" in verdict["done"]


def test_new_tab_requirement():
    goal = "Find the population of Bangkok on Wikipedia, now open Bangkok weather in a new tab."
    page = "Bangkok weather Wikipedia"
    assert check_done(goal, text=page, tabs_open=2)["ok"] is True
    verdict = check_done(goal, text=page, tabs_open=1)
    assert verdict["ok"] is False
    assert verdict["missing"] == ["goal needs a new tab"]
    assert check_done("Find the population of Bangkok.", text="Bangkok", tabs_open=1)["ok"] is True


def test_widget_commit_triggers():
    assert check_done(FLIGHTS, text="Zurich London Google Flights", widget_commits=1)["ok"] is True
    verdict = check_done(FLIGHTS, text="Zurich London Google Flights", widget_commits=0)
    assert verdict["missing"] == [
        "goal needs a filter/date/traveler setting, none verified as committed"
    ]
    assert check_done(HOTELS, text="Lisbon Google Hotels", widget_commits=0)["ok"] is False
    # Reading tasks need no widget commits.
    assert check_done(EBAY, text="usb-c hub eBay price rating")["ok"] is True
    assert check_done(HNTOP5, text="Hacker News top stories points")["ok"] is True
    assert check_done(quotes_text(), text="quotes.toscrape.com page 3")["ok"] is True


def quotes_text():
    return QUOTES


def test_unselected_filter_value_is_missing():
    goal = FLIGHTS
    page = "Google Flights Zurich London nonstop"
    option = {"kind": "check", "label": "Nonstop only", "checked": False}
    button = {"kind": "click", "label": "Stops, Not selected"}
    verdict = check_done(goal, text=page, widget_commits=2, controls=[option])
    assert verdict["ok"] is False
    assert verdict["missing"] == ["nonstop looks unselected"]
    verdict = check_done(goal, text=page, widget_commits=2, controls=[button])
    assert verdict["ok"] is False
    assert verdict["missing"] == ["nonstop looks unselected"]
    checked = {"kind": "check", "label": "Nonstop only", "checked": True}
    assert check_done(goal, text=page, widget_commits=2, controls=[checked])["ok"] is True
    # Dialog closed at the end: nothing related, nothing claimed.
    assert check_done(goal, text=page, widget_commits=2, controls=[])["ok"] is True
    plain = {"kind": "click", "label": "Stops"}
    assert check_done(goal, text=page, widget_commits=2, controls=[plain])["ok"] is True
