"""Long native selects are one target. The label match never types a path into the control."""

import pytest

from jev_ultrafast.config import resolve_config
from jev_ultrafast.model import action_space, match_option

OPTIONS = [
    {"label": "Canada", "value": "ca", "disabled": False},
    {"label": "canada west", "value": "cw", "disabled": False},
    {"label": "Cameroon", "value": "cm", "disabled": False},
    {"label": "Japan", "value": "jp", "disabled": False},
    {"label": "Hidden", "value": "hid", "disabled": True},
]


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("Canada", "ca"),
        ("canada", "ca"),
        ("Came", "cm"),
        ("Jap", "jp"),
        ("west", "cw"),
        ("Hidden", None),
        ("Can", None),
        ("", None),
    ],
)
def test_option_matching_rules(label, value):
    matched = match_option(label, OPTIONS)
    assert (matched or {}).get("value") == value


def test_duplicate_exact_labels_do_not_match():
    options = [
        {"label": "A", "value": "1"},
        {"label": "A", "value": "2"},
    ]
    assert match_option("A", options) is None


def test_long_select_is_one_target_without_option_labels():
    action = {
        "id": "e1", "kind": "select_option", "node": 4, "role": "combobox",
        "label": "Country", "value": "", "current_value": "Option 001", "option_count": 200,
    }
    elements, targets, _controls = action_space([action])
    assert elements[0]["operations"] == ["SELECT_OPTION"]
    assert "options" not in elements[0]
    assert set(targets["SELECT_OPTION"]) == {"1"}
    assert "Option 150" not in str(elements)


def test_small_select_keeps_option_indexes():
    actions = [
        {"id": "e1", "kind": "select", "node": 4, "label": "Category → Design", "value": "Design", "role": "combobox"},
        {
            "id": "e2", "kind": "select", "node": 4, "label": "Category → Coastal",
            "value": "Coastal", "role": "combobox",
        },
    ]
    _elements, targets, _controls = action_space(actions)
    assert set(targets["SELECT"]) == {"1:1", "1:2"}
    assert "SELECT_OPTION" not in targets


def test_large_select_defaults(monkeypatch):
    monkeypatch.delenv("JEV_LARGE_SELECT", raising=False)
    monkeypatch.delenv("JEV_SELECT_INLINE_MAX", raising=False)
    config = resolve_config()
    assert config["large_select"] is True
    assert config["select_inline_max"] == 12
    assert resolve_config(select_inline_max=3)["select_inline_max"] == 3
    monkeypatch.setenv("JEV_LARGE_SELECT", "0")
    assert resolve_config()["large_select"] is False
