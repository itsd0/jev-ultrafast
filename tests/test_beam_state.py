"""DOM renderer, transcript compaction, loop nudges, output validation. Offline."""

from jev_ultrafast.beam_state import (
    LoopMonitor,
    Transcript,
    action_hash_for,
    build_state_message,
    render_dom,
    validate_step_structure,
)


def _action(id, kind, label, **extra):
    base = {
        "id": id, "kind": kind, "label": label, "role": "button" if kind == "click" else "textbox",
        "tag": "button" if kind == "click" else "input", "depth": 4, "node": 1, "frame": "f0",
        "rect": {"x": 10, "y": 10, "w": 100, "h": 20},
    }
    base.update(extra)
    return base


def _page(actions, **extra):
    page = {
        "url": "https://example.test/", "title": "Example", "w": 1120, "h": 780,
        "text": "hello", "actions": actions, "scroll": {"y": 0, "height": 780},
    }
    page.update(extra)
    return page


def _step_output(*actions):
    return {
        "thinking": "t", "evaluation_previous_goal": "First step. Verdict: Success",
        "memory": "m", "next_goal": "g", "plan_update": "",
        "actions": [
            {
                "name": name, "index": -1, "text": "", "url": "", "new_tab": False,
                "tab": "", "engine": "", "clear": False, "keys": [], "option": "",
                "direction": "down", "amount": -1, "pattern": "", "key": "",
                "filename": "", "content": "", "success": False, **overrides,
            }
            for name, overrides in actions
        ],
    }


def test_dom_indexes_viewport_first_and_indents():
    actions = [
        _action("e1", "click", "Far", node=1, depth=6, rect={"x": 10, "y": 2000, "w": 10, "h": 10}),
        _action("e2", "click", "Near", node=2, depth=4, rect={"x": 10, "y": 20, "w": 10, "h": 10}),
    ]
    render = render_dom(_page(actions))
    assert "[1]<button" in render["text"].splitlines()[0]
    assert "Near" in render["text"].splitlines()[0]
    assert render["by_index"][1]["action"]["id"] == "e2"
    assert "  [2]<button" in render["text"]


def test_dom_marks_new_and_shadow_and_caps():
    first = render_dom(_page([_action("e1", "click", "A", node=1)]))
    assert "*" not in first["text"]
    actions = [
        _action("e1", "click", "A", node=1),
        _action("e2", "fill", "B", node=2, shadow=True, value="x"),
    ]
    second = render_dom(_page(actions), first["new_keys"])
    assert "*" in second["text"]
    assert "shadow" in second["text"]
    big = render_dom(
        _page([_action(f"e{i}", "click", f"B{i}", node=i) for i in range(1, 40)]),
        max_rows=5,
    )
    assert "capped" in big["text"]


def test_dom_lists_scroll_containers_and_page_scroll():
    actions = [
        _action("e1", "click", "A", node=1),
        {"id": "s1", "kind": "scroll", "label": "Scroll down box", "node": 9, "frame": "f0",
         "axis": "y", "delta": 80, "tag": "div"},
        {"id": "s2", "kind": "scroll", "label": "Scroll up box", "node": 9, "frame": "f0",
         "axis": "y", "delta": -80, "tag": "div"},
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
    ]
    render = render_dom(_page(actions))
    assert '<scroll div dirs="down/up">' in render["text"]
    assert "index -1" in render["text"]
    assert set(render["by_index"][2]["dirs"]) == {"down", "up"}


def test_state_message_bounds_sections():
    message = build_state_message(
        goal="Do it", background="ctx", step_no=2, budget=10, plan="p",
        tabs_text="[t1]", page_url="https://x.test/", page_title="X",
        dom_text="D" * 20000, page_text="P" * 20000, history_text="H" * 20000,
        notes=["n"], results_text="R" * 20000,
    )
    assert "User request:\nDo it" in message
    assert "Budget: step 2 of 10." in message
    assert len(message) < 30000


def test_transcript_round_trips_reasoning_and_drops_bare_turns():
    transcript = Transcript()
    transcript.add_turn(1, "state-1", '{"a": 1}', "why-1", next_goal="g1",
                        results=[{"label": "clicked", "ok": True}])
    transcript.add_turn(2, "state-2", '{"a": 2}', None, next_goal="g2",
                        results=[{"label": "typed", "ok": True}])
    messages = transcript.messages("SYS", "CURRENT")
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[-1] == {"role": "user", "content": "CURRENT"}
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["reasoning_content"] == "why-1"
    assert "state-2" not in str(messages)


def test_transcript_compacts_old_turns_and_marks_verified_only():
    transcript = Transcript()
    for step in range(1, 15):
        transcript.add_turn(
            step, f"state-{step}", '{"a": 1}', "why", next_goal=f"g{step}",
            results=[{"label": "clicked", "ok": step % 2 == 0}],
            verified_facts=["10.7 million people"] if step == 1 else [],
        )
    messages = transcript.messages("SYS", "CURRENT", keep=6, budget=10)
    assert messages[1]["content"].startswith("Earlier steps (compacted; only VERIFIED")
    assert "10.7 million people" in messages[1]["content"]
    assistants = [m for m in messages if m["role"] == "assistant"]
    assert len(assistants) == 6
    assert all(item["reasoning_content"] == "why" for item in assistants)


def test_loop_nudges_at_5_8_12_and_resets():
    monitor = LoopMonitor()
    seen = []
    for step in range(12):
        seen.extend(monitor.add("same", f"fp-{step}"))
    assert len(seen) == 3
    assert "5 actions" in seen[0]
    assert "8 identical" in seen[1]
    assert "12 identical" in seen[2]
    assert monitor.add("other", "fp") == []
    assert monitor.add("same", "fp") == []


def test_loop_nudges_on_unchanged_fingerprint():
    monitor = LoopMonitor()
    nudges = []
    for step in range(5):
        nudges.extend(monitor.add(f"hash-{step}", "same-fp"))
    assert len(nudges) == 1
    assert "unchanged for 5 steps" in nudges[0]
    assert monitor.add("hash-x", "new-fp") == []


def test_action_hash_normalizes():
    assert action_hash_for("click", "Click:button:Save ", "X.test") == action_hash_for(
        "click", "click:button:save", "x.test")


def test_step_structure_validation():
    output = _step_output(("click", {"index": 1}))
    cleaned, error = validate_step_structure(output)
    assert error is None
    assert cleaned["actions"][0]["name"] == "click"

    bad = _step_output(("click", {"index": 1}))
    bad["evaluation_previous_goal"] = "no verdict here"
    assert validate_step_structure(bad)[1] is not None

    bad = _step_output(("done", {"text": "x", "success": True}), ("click", {"index": 1}))
    _cleaned, error = validate_step_structure(bad)
    assert error == "done must be the only action in its step"

    bad = _step_output(("nope", {}))
    assert validate_step_structure(bad)[1] is not None

    bad = _step_output(("click", {"index": True}))
    _cleaned, error = validate_step_structure(bad)
    assert "wrong type" in (error or "")
