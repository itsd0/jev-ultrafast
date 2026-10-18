"""Evidence must be grounded in observed text; judges see matching sections."""

from jev_ultrafast import agent as loop
from jev_ultrafast import model


def _page():
    return {
        "url": "https://docs.test/array/flatmap",
        "title": "Array flatMap",
        "text": "Try it: const result = arr.flatMap((num) => num * 2);",
        "sections": [
            {"heading": "Try it", "text": "const result = arr.flatMap((num) => num * 2);"},
            {"heading": "Syntax", "text": "flatMap(callbackFn)\nflatMap(callbackFn, thisArg)"},
        ],
    }


def test_hallucinated_value_and_fact_are_dropped():
    state = {"subgoals": [{"id": 0, "text": "syntax"}], "memo": [], "page": _page()}
    loop.apply_judgment(
        state,
        {
            "satisfied": [{"id": 0, "value": "arr.flatMap(old, signature)"}],
            "facts": ["flatMap was invented in 1999"],
            "leave_site": False,
        },
        "https://docs.test/array/flatmap",
    )
    assert state["subgoals"][0].get("satisfied") is not True
    assert state["memo"] == []
    assert [drop["value"] for drop in state["_ungrounded"]] == [
        "arr.flatMap(old, signature)",
        "flatMap was invented in 1999",
    ]


def test_grounding_accepts_sections_numbers_and_long_overlap():
    page = {
        "url": "https://docs.test/x",
        "title": "t",
        "text": "population 5494932 and other words here",
        "sections": [{"heading": "Syntax", "text": "flatMap(callbackFn, thisArg)"}],
    }
    state = {
        "subgoals": [{"id": 0, "text": "a"}, {"id": 1, "text": "b"}, {"id": 2, "text": "c"}],
        "memo": [],
        "page": page,
    }
    long_value = "flatMap callbackFn thisArg population 5494932 other words here plus"
    assert len(long_value) > 40
    loop.apply_judgment(
        state,
        {
            "satisfied": [
                {"id": 0, "value": "flatMap(callbackFn, thisArg)"},  # sections only
                {"id": 1, "value": "5,494,932"},  # formatting differs
                {"id": 2, "value": long_value},  # 8/9 tokens overlap
            ],
            "facts": [],
            "leave_site": False,
        },
        "https://docs.test/x",
    )
    assert [item.get("satisfied") for item in state["subgoals"]] == [True, True, True]
    assert state["_ungrounded"] == []
    novel = "completely novel claims about nothing relevant whatsoever today sire"
    assert len(novel) > 40
    state2 = {"subgoals": [{"id": 0, "text": "a"}], "memo": [], "page": page}
    loop.apply_judgment(
        state2, {"satisfied": [{"id": 0, "value": novel}], "facts": [], "leave_site": False},
        "https://docs.test/x",
    )
    assert state2["subgoals"][0].get("satisfied") is not True
    assert [drop["value"] for drop in state2["_ungrounded"]] == [novel]


def test_syntax_goal_gets_the_syntax_section():
    page = _page()
    goal = "Report the syntax of Array flatMap"
    picked = model.matching_sections(goal, ["locate the syntax section"], page["sections"])
    assert [section["heading"] for section in picked] == ["Syntax"]
    answer = model.answer_context(
        goal, page, [],
        evidence=[{"subgoal": "locate the syntax", "value": "v", "source": "u"}], memo=[],
    )
    assert [section["heading"] for section in answer["sections"]] == ["Syntax"]
    track = model.track_context(
        goal, [{"id": 0, "text": "locate the syntax", "required": True}], [], page, [],
    )
    assert [section["heading"] for section in track["sections"]] == ["Syntax"]
    gate = model.gate_context(
        goal, [{"id": 0, "text": "locate the syntax", "required": True}], [], page, [],
    )
    assert [section["heading"] for section in gate["sections"]] == ["Syntax"]


def test_answer_prompt_requires_verbatim_quotes():
    from jev_ultrafast.questions import ANSWER_VALUE

    lowered = ANSWER_VALUE.lower()
    assert "verbatim" in lowered
    for trigger in ("syntax", "signature", "code", "title", "exact line"):
        assert trigger in lowered
