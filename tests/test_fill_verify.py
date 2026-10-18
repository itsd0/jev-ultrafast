"""Fill verify reads the focused twin and compares dates semantically. No paid APIs."""

import json
import shutil
import subprocess

import pytest

from jev_ultrafast.browser import (
    commit_date,
    commit_key_expression,
    cover_info_expression,
    detect_slider,
    field_value_expression,
    link_href_expression,
    parse_date,
    scroll_into_view_expression,
    slider_fill,
    slider_geometry_expression,
    slider_set_native_expression,
    slider_value_expression,
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


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


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


def test_exact_read_verifies_without_retype():
    result, calls = _verify("June 12 2027", "", ["June 12 2027"])
    assert result == {"text_verified": True, "typed": "June 12 2027"}
    assert not _retyped(calls)


def _commit(expected, previous, reads, monkeypatch):
    seen = []
    values = list(reads)
    monkeypatch.setattr("time.sleep", lambda seconds: seen.append(("sleep", seconds)))

    def evaluate(expression):
        seen.append(expression)
        if "KeyboardEvent" in expression:
            return {"focused": True, "ordered": True}
        return values.pop(0)

    result = commit_date(lambda *args, **kwargs: None, evaluate, {"node": 1, "frame": "f0"},
                         expected, previous)
    return result, seen


def test_commit_date_uses_the_twin_then_enter(monkeypatch):
    result, seen = _commit("June 12 2027", "", ["Fri, Jun 11", "Sat, Jun 12", "Sat, Jun 12"], monkeypatch)
    assert result["date_committed"] is True
    assert result["ordering"] == {"key": "Enter", "focused": True, "ordered": True}
    assert any("KeyboardEvent" in item for item in seen if isinstance(item, str))
    assert ("sleep", 0.6) in seen


def test_commit_date_fails_without_dispatch_when_the_twin_lacks_it(monkeypatch):
    result, seen = _commit("June 12 2027", "", ["Fri, Jun 11", "Fri, Jun 11"], monkeypatch)
    assert result == {"date_committed": False}
    assert not any("KeyboardEvent" in item for item in seen if isinstance(item, str))
    assert not any(isinstance(item, tuple) for item in seen)


def test_commit_date_ignores_non_dates(monkeypatch):
    result, seen = _commit("Zurich", "", [], monkeypatch)
    assert result == {}
    assert seen == []


def test_commit_date_accepts_an_already_committed_value(monkeypatch):
    result, seen = _commit("June 12 2027", "", ["Sat, Jun 12"], monkeypatch)
    assert result == {"date_committed": True}
    assert len([item for item in seen if isinstance(item, str)]) == 1


def _run_commit(detached, active):
    expression = commit_key_expression({"node": 1, "frame": "f0"}, "Enter")
    script = (
        "const events = [];"
        "const twin = {tagName: 'INPUT', value: 'x', isConnected: true,"
        " focus() { globalThis.document.activeElement = twin; },"
        " dispatchEvent(e) { events.push('twin:' + e.type); return true; }};"
        "const stale = {tagName: 'INPUT', value: '', isConnected: false,"
        " focus() {}, dispatchEvent(e) { events.push('stale:' + e.type); return true; }};"
        "globalThis.window = {__jevFast: {resolve: () => (" + ("stale" if detached else "twin") + ")}};"
        "globalThis.document = {activeElement: " + ("twin" if active == "twin" else "{tagName: 'BODY'}") + "};"
        "globalThis.KeyboardEvent = class { constructor(type) { this.type = type; } };"
        "globalThis.InputEvent = class { constructor(type) { this.type = type; } };"
        "const out = eval(" + json.dumps(expression) + ");"
        "console.log(JSON.stringify({out, events}));"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_commit_key_orders_keydown_beforeinput_input():
    assert _run_commit(False, "body") == {
        "out": {"focused": True, "ordered": True},
        "events": ["twin:keydown", "twin:beforeinput", "twin:input"],
    }


@needs_node
def test_commit_key_falls_back_to_the_focused_twin():
    assert _run_commit(True, "twin") == {
        "out": {"focused": True, "ordered": True},
        "events": ["twin:keydown", "twin:beforeinput", "twin:input"],
    }


def test_token_subset_counts_as_rewritten():
    result, calls = _verify("usb-c hub", "", ["USB-C Hub 7-in-1"])
    assert result["text_verified"] is True
    assert result["rewritten"] is True
    assert not _retyped(calls)


def test_text_model_is_told_to_skip_the_weekday():
    assert "without a weekday" in TEXT_VALUE


def test_text_model_is_told_sliders_take_a_plain_number():
    assert "plain number" in TEXT_VALUE


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


@needs_node
def test_slider_expressions_are_valid_javascript(tmp_path):
    action = {"node": 3, "frame": "f0"}
    expressions = [
        slider_set_native_expression(action, 200.0),
        slider_geometry_expression(action),
        slider_value_expression(action),
    ]
    for index, expression in enumerate(expressions):
        path = tmp_path / f"slider_{index}.js"
        path.write_text(expression)
        proc = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr


@needs_node
def test_covered_expressions_are_valid_javascript(tmp_path):
    action = {"node": 3, "frame": "f0"}
    expressions = [
        scroll_into_view_expression(action),
        link_href_expression(action),
        cover_info_expression(action),
    ]
    for index, expression in enumerate(expressions):
        path = tmp_path / f"covered_{index}.js"
        path.write_text(expression)
        proc = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr


@needs_node
def test_twin_input_is_read_when_it_is_the_only_editable():
    twin = {"tagName": "INPUT", "value": "Sat, Jun 12", "isConnected": True}
    assert _run_reader("Fri, Jun 11", [twin], twin) == "Sat, Jun 12"


@needs_node
def test_twin_input_is_ignored_when_ambiguous():
    twin = {"tagName": "INPUT", "value": "Sat, Jun 12", "isConnected": True}
    other = {"tagName": "INPUT", "value": "other", "isConnected": True}
    assert _run_reader("Fri, Jun 11", [twin, other], twin) == "Fri, Jun 11"


def _slider(slider, text, reads):
    calls = []
    values = list(reads)

    def call(method, **params):
        calls.append((method, params))

    def evaluate(_expression):
        return values.pop(0)

    action = {"node": 1, "frame": "f0", "role": "slider", "slider": slider}
    return slider_fill(call, evaluate, action, text), calls


def _presses(calls, key):
    return sum(
        1 for method, params in calls
        if method == "Input.dispatchKeyEvent"
        and params.get("type") == "keyDown"
        and params.get("key") == key
    )


def test_detect_slider_matches_role_only():
    assert detect_slider({"role": "slider"}) is True
    assert detect_slider({"role": "textbox"}) is False
    assert detect_slider({}) is False
    assert detect_slider(None) is False


def test_slider_fill_sets_native_first_without_keys():
    slider = {"min": 0, "max": 500, "now": 150, "step": 10}
    result, calls = _slider(slider, "200", [200])
    assert result["text_verified"] is True
    assert result["typed"] == 200
    assert result["slider_moves"] == 0
    assert calls == []


def test_slider_fill_falls_back_to_keys_when_native_reverts():
    slider = {"min": 0, "max": 500, "now": 150, "step": 10}
    result, calls = _slider(slider, "170", [150, {}, 170])
    assert result["text_verified"] is True
    assert _presses(calls, "ArrowRight") == 2
    assert result["slider_moves"] == 2


def test_slider_fill_moves_with_arrows_and_verifies():
    slider = {"min": 0, "max": 500, "now": 150, "step": 10}
    result, calls = _slider(slider, "170", [None, {}, 170])
    assert result["text_verified"] is True
    assert result["typed"] == 170
    assert result["slider_moves"] == 2
    assert _presses(calls, "ArrowRight") == 2
    assert _presses(calls, "ArrowLeft") == 0


def test_slider_fill_jumps_home_when_closer():
    slider = {"min": 0, "max": 500, "now": 480, "step": 10}
    result, calls = _slider(slider, "10", [None, {}, 10])
    assert result["text_verified"] is True
    assert _presses(calls, "Home") == 1
    assert _presses(calls, "ArrowRight") == 1
    assert result["slider_moves"] == 2
    assert any(params.get("windowsVirtualKeyCode") == 36 for _, params in calls)


def test_slider_fill_uses_page_keys_for_a_long_way_without_a_jump():
    slider = {"now": 0, "step": 1}
    result, calls = _slider(slider, "500", [None, 500])
    assert result["text_verified"] is True
    assert _presses(calls, "PageUp") == 1
    assert result["slider_moves"] == 1


def test_slider_fill_clicks_to_position_then_adjusts():
    slider = {"min": 0, "max": 500, "now": 0, "step": 10}
    geometry = {"x": 10, "y": 10, "width": 200, "height": 20}
    result, calls = _slider(slider, "250", [None, geometry, 200, 250])
    assert result["text_verified"] is True
    assert result["slider_moves"] == 5
    press = next(
        params for method, params in calls
        if method == "Input.dispatchMouseEvent" and params["type"] == "mousePressed"
    )
    assert press["x"] == 110.0
    assert press["y"] == 20.0
    assert _presses(calls, "ArrowRight") == 5


def test_slider_fill_parses_currency_and_clamps_to_max():
    slider = {"min": 0, "max": 500, "now": 150, "step": 10}
    result, calls = _slider(slider, "$900", [None, {}, 500])
    assert result["text_verified"] is True
    assert result["typed"] == 500
    assert _presses(calls, "End") == 1
    assert _presses(calls, "ArrowRight") == 0


def test_slider_fill_reads_the_current_value_from_valuetext():
    slider = {"min": 0, "max": 500, "now": None, "valuetext": "about $150 total", "step": 10}
    result, calls = _slider(slider, "170", [None, {}, 170])
    assert result["text_verified"] is True
    assert _presses(calls, "ArrowRight") == 2


def test_slider_fill_without_a_number_sends_nothing():
    slider = {"min": 0, "max": 500, "now": 150, "step": 10}
    result, calls = _slider(slider, "cheaper please", [])
    assert result["text_verified"] is False
    assert calls == []


def test_slider_fill_needs_a_known_current_value():
    result, calls = _slider({}, "150", [])
    assert result["text_verified"] is False
    assert result["typed"] is None
    assert calls == []


def test_slider_fill_is_bounded_when_the_widget_ignores_keys():
    slider = {"min": 0, "max": 100000, "now": 0, "step": 1}
    result, _ = _slider(slider, "99999", [None, {}, 0, 0])
    assert result["text_verified"] is False
    assert result["slider_moves"] == 200
