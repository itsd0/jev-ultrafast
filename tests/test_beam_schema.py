"""Strict schemas meet Reflection's json_schema rules. No network."""

import copy

import pytest

from jev_ultrafast.llm import schema as beam_schema


def test_all_schemas_are_strict():
    for name in ("STEP_SCHEMA", "JUDGE_SCHEMA", "EXTRACT_SCHEMA", "PING_SCHEMA"):
        assert beam_schema.validate_strict_schema(getattr(beam_schema, name)) is True


def test_step_schema_action_shape():
    actions = beam_schema.STEP_SCHEMA["properties"]["actions"]
    assert actions["minItems"] == 1
    assert actions["maxItems"] == 5
    item = actions["items"]
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False
    assert "done" in item["properties"]["name"]["enum"]
    assert set(item["properties"]["direction"]["enum"]) == {"up", "down", "left", "right"}


def test_validator_rejects_missing_additional_properties():
    bad = copy.deepcopy(beam_schema.JUDGE_SCHEMA)
    del bad["additionalProperties"]
    with pytest.raises(ValueError, match="additionalProperties"):
        beam_schema.validate_strict_schema(bad)


def test_validator_rejects_partial_required():
    bad = copy.deepcopy(beam_schema.JUDGE_SCHEMA)
    bad["required"] = ["complete"]
    with pytest.raises(ValueError, match="required"):
        beam_schema.validate_strict_schema(bad)


def test_validator_rejects_unions_and_combinators():
    bad = copy.deepcopy(beam_schema.EXTRACT_SCHEMA)
    bad["properties"]["text"]["type"] = ["string", "null"]
    with pytest.raises(ValueError, match="unions"):
        beam_schema.validate_strict_schema(bad)
    bad = copy.deepcopy(beam_schema.EXTRACT_SCHEMA)
    bad["properties"]["text"] = {"oneOf": [{"type": "string"}, {"type": "null"}]}
    with pytest.raises(ValueError, match="forbidden"):
        beam_schema.validate_strict_schema(bad)


def test_validator_rejects_bare_arrays():
    bad = copy.deepcopy(beam_schema.JUDGE_SCHEMA)
    bad["properties"]["missing"] = {"type": "array"}
    with pytest.raises(ValueError, match="items"):
        beam_schema.validate_strict_schema(bad)


def test_response_format_is_strict_json_schema():
    payload = beam_schema.response_format("BeamStep", beam_schema.STEP_SCHEMA)
    assert payload["type"] == "json_schema"
    assert payload["json_schema"]["strict"] is True
    assert payload["json_schema"]["name"] == "BeamStep"
