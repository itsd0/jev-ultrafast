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
        ({"tagName": "DIV", "type": "", "role": "slider", "editable": False}, None),
    ]
    script = (
        "const cases = " + json.dumps(cases) + ";"
        "console.log(JSON.stringify(cases.map(([stub, _]) => role({"
        " tagName: stub.tagName, type: stub.type, isContentEditable: stub.editable,"
        " getAttribute: (name) => name === 'role' ? stub.role : null}))));"
    )
    assert run_block(block, script) == [want for _, want in cases]
