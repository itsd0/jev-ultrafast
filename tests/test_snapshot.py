"""Offline tests for shipped snapshot.js logic. No browser needed.

Marked regions of jev_ultrafast/snapshot.js (@test-export-start/end) are
extracted verbatim and evaluated in node with stub DOM objects, so the tests
execute the shipped code, not a copy.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parent.parent / "jev_ultrafast" / "snapshot.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def snapshot_block(name):
    """The shipped source between @test-export markers. Fails loudly when absent."""
    source = SNAPSHOT.read_text()
    _, _, rest = source.partition(f"// @test-export-start: {name}")
    assert rest, f"missing @test-export-start: {name}"
    block, _, tail = rest.partition(f"// @test-export-end: {name}")
    assert tail, f"missing @test-export-end: {name}"
    return block


def run_block(block, script):
    proc = subprocess.run(["node", "-e", block + script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_role_mapping_distinguishes_search_from_text():
    block = snapshot_block("role-mapping")
    cases = [
        ({"tagName": "INPUT", "type": "search", "role": None, "editable": False}, "searchbox"),
        ({"tagName": "INPUT", "type": "text", "role": None, "editable": False}, "textbox"),
        ({"tagName": "INPUT", "type": "email", "role": None, "editable": False}, "textbox"),
        ({"tagName": "INPUT", "type": "url", "role": None, "editable": False}, "textbox"),
        ({"tagName": "INPUT", "type": "tel", "role": None, "editable": False}, "textbox"),
        ({"tagName": "TEXTAREA", "type": "", "role": None, "editable": False}, "textbox"),
        ({"tagName": "DIV", "type": "", "role": None, "editable": True}, "textbox"),
        ({"tagName": "INPUT", "type": "number", "role": None, "editable": False}, "spinbutton"),
        ({"tagName": "INPUT", "type": "checkbox", "role": None, "editable": False}, "checkbox"),
        ({"tagName": "BUTTON", "type": "", "role": None, "editable": False}, "button"),
        ({"tagName": "SELECT", "type": "", "role": None, "editable": False}, "combobox"),
        ({"tagName": "INPUT", "type": "range", "role": None, "editable": False}, "slider"),
        ({"tagName": "DIV", "type": "", "role": "slider", "editable": False}, "slider"),
    ]
    script = (
        "const cases = " + json.dumps(cases) + ";"
        "console.log(JSON.stringify(cases.map(([stub, _]) => role({"
        " tagName: stub.tagName, type: stub.type, isContentEditable: stub.editable,"
        " getAttribute: (name) => name === 'role' ? stub.role : null}))));"
    )
    assert run_block(block, script) == [want for _, want in cases]


@needs_node
def test_slider_state_reads_range_inputs_and_aria():
    block = snapshot_block("slider-state")
    cases = [
        (
            {"attrs": {}, "min": "0", "max": "500", "value": "150", "step": ""},
            {"min": 0, "max": 500, "now": 150, "valuetext": None, "step": None},
        ),
        (
            {
                "attrs": {
                    "aria-valuemin": "20", "aria-valuemax": "300", "aria-valuenow": "120",
                    "aria-valuetext": "120 dollars",
                },
                "min": None, "max": None, "value": None, "step": None,
            },
            {"min": 20, "max": 300, "now": 120, "valuetext": "120 dollars", "step": None},
        ),
        (
            {"attrs": {"aria-valuenow": "150"}, "min": None, "max": None, "value": "7", "step": None},
            {"min": None, "max": None, "now": 150, "valuetext": None, "step": None},
        ),
        (
            {"attrs": {}, "min": None, "max": None, "value": None, "step": None},
            {"min": None, "max": None, "now": None, "valuetext": None, "step": None},
        ),
    ]
    script = (
        "const cases = " + json.dumps(cases) + ";"
        "console.log(JSON.stringify(cases.map(([stub, _]) => sliderState({"
        " getAttribute: (name) => stub.attrs[name] ?? null,"
        " min: stub.min, max: stub.max, value: stub.value, step: stub.step}))));"
    )
    assert run_block(block, script) == [want for _, want in cases]
