# Jev Ultrafast

Read README.md before editing. Keep the loop small: page -> indexed elements -> validated actions -> execution.

- The input is one natural-language goal. Do not add site-specific plans or hardcoded field values.
- Default core is one LLM call per step (thinking, evaluation with verdict, memory, next goal, 1-5 actions). `JEV_AGENT=classic` keeps the TypeSafe-decision + Mercury-text split; in classic, TypeSafe chooses an operation and operation-specific target heads in one request, and only the selected operation's target is consumed.
- Targets must map to observed elements and supported operations. Never let the model emit selectors or executable code.
- TYPE_TEXT invokes the text LLM. Cache a stale retry's value only while its entire helper input is identical.
- Never retry a browser mutation. Log execution before observing its result.
- Screenshots are optional; the model does not consume them. Keep demonstration footage at its original speed.
- Keep credentials server-side and .env ignored. Tests must not call paid APIs.
- Secrets travel as `<secret>NAME</secret>` placeholders; values substitute at execution on matching domains only and never reach prompts, history, traces, or logs.
- Verify actual final outcomes independently. A DONE choice is not proof of success.
- Keep examples, README claims, raw evidence, and model-call counts consistent.
- Do not commit or push unless the user requests it.

Checks: uv run ruff check ., uv run pytest, node --check jev_ultrafast/static/app.js, uv build.
