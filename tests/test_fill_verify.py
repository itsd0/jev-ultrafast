"""Fill verify reads the focused twin and compares dates semantically. No paid APIs."""

import json
import shutil
import subprocess

import pytest

from jev_ultrafast.browser import (
    field_value_expression,
    parse_date,
    verify_typed_text,
)
from jev_ultrafast.questions import TEXT_VALUE


def test_parse_date_suite_formats():
    assert parse_date("June 12 2027") == (6, 12, 2027)
    assert parse_date("Sat, Jun 12") == (6, 12, None)
    assert parse_date("12 June 2027") == (6, 12, 2027)
    assert parse_date("2027-06-12") == (6, 12, 2027)
    assert parse_date("Jun 12") == (6, 12, None)


def test_parse_date_ignores_a_wrong_weekday():
    # June 12 2027 is a Saturday; the invented Friday must not fail the fill.
    assert parse_date("Fri, Jun 12") == (6, 12, None)


def test_parse_date_rejects_non_dates_and_ambiguous_numerics():
    assert parse_date("Lisbon, Portugal") is None
    assert parse_date("") is None
    assert parse_date(None) is None
    assert parse_date("06/07") is None
    assert parse_date("06/07/2027") is None
    assert parse_date("06/19/2027") == (6, 19, 2027)


def _verify(expected, previous, reads):
    calls = []
    values = list(reads)

    def call(method, **params):
        calls.append((method, params))

    def evaluate(_expression):
        return values.pop(0)

    result = verify_typed_text(
        call, evaluate, {"node": 1, "frame": "f0"}, expected, previous
    )
    return result, calls


def _retyped(calls):
    return any(
        method == "Input.dispatchKeyEvent" and params.get("key") == "Backspace"
        for method, params in calls
    )


def test_same_date_verifies_without_retype():
    result, calls = _verify("June 12 2027", "Fri, Jun 11", ["Sat, Jun 12"])
    assert result == {"text_verified": True, "typed": "Sat, Jun 12", "same_date": True}
    assert not _retyped(calls)


def test_wrong_weekday_still_compares_by_date():
    result, calls = _verify("June 12", "Jun 11", ["Sat, Jun 12"])
    assert result["text_verified"] is True
    assert result["same_date"] is True
    assert not _retyped(calls)


def test_field_format_disambiguates_numeric_order():
    # The field's own previous value proves day-first order.
    result, _ = _verify("20/06/2027", "19/06/2027", ["20 Jun 2027"])
    assert result["text_verified"] is True
    assert result["same_date"] is True


def test_ambiguous_numeric_falls_back_to_strict():
    result, calls = _verify("06/07/2027", "", ["07/06/2027"])
    assert result == {"text_verified": False, "typed": "07/06/2027"}
    assert not _retyped(calls)


def test_unchanged_value_still_retypes_once():
    result, calls = _verify("06/07/2027", "", ["", "06/07/2027"])
    assert result == {"text_verified": True, "typed": "06/07/2027"}
    assert _retyped(calls)


def test_unrelated_rewrite_fails_without_retype():
    result, calls = _verify("abc", "", ["xyz"])
    assert result == {"text_verified": False, "typed": "xyz"}
    assert not _retyped(calls)


def test_token_subset_counts_as_rewritten():
    result, calls = _verify("usb-c hub", "", ["USB-C Hub 7-in-1"])
    assert result["text_verified"] is True
    assert result["rewritten"] is True
    assert not _retyped(calls)


def test_text_model_is_told_to_skip_the_weekday():
    assert "without a weekday" in TEXT_VALUE


def _run_reader(detached_value, editables, active):
    expression = field_value_expression({"node": 1, "frame": "f0"})
    script = (
        "globalThis.window = {__jevFast: {resolve: () => ("
        + json.dumps({"tagName": "INPUT", "value": detached_value, "isConnected": False})
        + ")}};"
        "globalThis.document = {querySelectorAll: () => "
        + json.dumps(editables)
        + ", activeElement: "
        + json.dumps(active)
        + "};"
        "console.log(JSON.stringify(eval(" + json.dumps(expression) + ")));"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
def test_twin_input_is_read_when_it_is_the_only_editable():
    twin = {"tagName": "INPUT", "value": "Sat, Jun 12", "isConnected": True}
    assert _run_reader("Fri, Jun 11", [twin], twin) == "Sat, Jun 12"


@needs_node
def test_twin_input_is_ignored_when_ambiguous():
    twin = {"tagName": "INPUT", "value": "Sat, Jun 12", "isConnected": True}
    other = {"tagName": "INPUT", "value": "other", "isConnected": True}
    assert _run_reader("Fri, Jun 11", [twin, other], twin) == "Fri, Jun 11"
