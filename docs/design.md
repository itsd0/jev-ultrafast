# Dynamic operation + target

The input is a natural-language goal. Every page observation builds an indexed table of accessible elements and their current values. One node receives one index, even when it supports both clicking and typing.

One decision request asks which operation to perform and which target would be appropriate for each available operation. The executor consumes only the target head corresponding to the selected operation. This avoids serial operation-then-target calls and rejects targets incompatible with the operation. Dropdown targets include a code-owned option index.

`DECISION_PROVIDER` defaults to TypeSafe: one request, an operation choice, and a target head per operation. `DECISION_PROVIDER=mercury` is opt-in. It posts one `mercury-decide` request to `POST /v1/decisions` whose `next_action` choices are the observed action ids. Each description carries the operation, label, role, current value, and checked/selected/expanded state when the snapshot has them. Separate noul questions score the task requirements. A DONE choice is replaced when any requirement is below 0.85 or the completion verifier says the page is incomplete. Neither provider's output is used as a selector, coordinate, or script. Text uses Mercury 2.5 `POST /v1/chat/completions` with `reasoning_effort` (`TEXT_MODEL_REASONING`, default `instant`), JSON object output, and `max_completion_tokens` of at least 1000. HTTP 429 and 503 retry up to three times, and Decide also retries 502 and 504, honoring `Retry-After` when it is a number of seconds.

Operation and target questions receive the same next-step rules. Target criteria include current values and checked/selected state. The questions run independently: a target cannot read the operation answer, so its premise explicitly names the operation it assumes.

TYPE_TEXT sends the goal, selected field, visible page context, and recent actions to a small LLM. Its JSON must contain exactly one valid `text` value. The code does not extract quoted literals. A value can be reused after a stale decision only while the entire helper input is identical, and is discarded after a successful mutation.

## Runtime

One browser-side DOM snapshot supplies common HTML/ARIA roles, names, values, visible text, and executable targets. A WeakMap gives each actual node a code-owned identity; a Map keeps the live references used for execution. Replaced elements receive new identities, disconnected references are pruned, and navigation starts a new cache. These IDs are not CDP backend node IDs. Geometry is always read again immediately before input.

The model sees visible text. Background focus emulation keeps animation frames running in the owned tab. Screenshots are optional and disabled in library calls by default; `screenshots=True` or `record_dir=...` enables them. The inspector enables them explicitly. A continuous screencast can record a run separately.

Freshness compares semantic state instead of counting DOM mutations. Before a click/select, guards compare the document, full URL, viewport, safe form values/states, selected target, and nearby form/dialog/row context. Text generation, typing, scrolling, waiting, and completion use a full semantic comparison. The executor rechecks target visibility, enabled state, geometry, and click occlusion. Scoped guards intentionally permit unrelated visible content to change; this is a practical heuristic, not proof that arbitrary page changes are irrelevant to the goal.

Browser mutations are not retried by transport recovery. Completed execution is logged before the next observation, including when that observation encounters a navigation. An interrupted native-select evaluation stops because its change event may already have fired. Typing uses a browser select-all command followed by CDP text insertion, so existing input contents are replaced.

The next observation waits for up to two animation frames or 50 ms after an interaction. Editable ARIA comboboxes instead wait for visible options, capped at 200 ms. This avoids paying for a prediction before autocomplete suggestions arrive. An explicit WAIT remains 100 ms; network loading is never fast-forwarded in the recording.

## What changed after the first demo

The initial prototype used five manually prepared steps and copied quoted strings. That proved finite-choice browser execution but did not demonstrate task decomposition or text generation. The current policy removes that shortcut and uses the original goal throughout. Operation/target distributions replace the old flat-choice/lookahead/Noul arrangement.

The audit also found that treating every INPUT as editable misclassified checkboxes. Editable roles now control TYPE_TEXT availability. Tests cover checkbox/radio/button distinction, invalid operation/target outputs, stale decisions, text-cache invalidation, missing credentials, waits, and final-route verification.

## Boundaries

Sixty browser actions and 120 decision requests bound a run. Up to 250 action candidates are retained; truncated candidates cannot be selected. The service stays loopback-only, serializes inspector actions, and checks Host, Origin, and a local request token. Credentials remain server-side. Tabs share the existing Chrome profile.

The policy is generic, but two websites do not establish broad reliability. Name resolution covers common labels, ARIA references, and text; it is not the browser's full accessibility algorithm. Shadow roots, frames, canvas, uploads, nested scrolling, pop-ups, and complex keyboard interactions can block progress. A valid action can still be wrong. Independent checks, rather than the model's DONE choice, determine whether the demonstrated task succeeded.

## Open shadow DOM

`JEV_SHADOW_DOM` defaults on (`Agent(..., shadow_dom=True)`). `snapshot.js` keeps one evaluation. After the light-DOM `querySelectorAll`, it walks each open `shadowRoot` and stores those nodes in the same `WeakMap` / `Map`. Ids stay code-owned integers. `elementFromPoint` stops at a shadow host, so the executor calls `shadowRoot.elementFromPoint` until the hit settles, then accepts the observed node or a descendant across open roots.

Closed roots have no `shadowRoot` to enter. A visible custom element with no open root and no light-DOM control is offered as a click. If that click is one of three actions that leave the page unchanged, the run stops with `blocked_reason` `possible closed shadow root`. Turning the flag off restores the light-DOM-only snapshot and the original `contains` / `elementFromPoint` check.

## Frames

`JEV_FRAMES` defaults to `same` (`Agent(..., frames="same"|"all"|"off")`). Same-origin `contentDocument`s are read inside the top snapshot. Bounds are shifted by each frame's rectangle, and only frames that intersect the viewport are included. Node ids stay integers in that frame's `WeakMap`. Action ids for nested frames are prefixed (`f2:e4`). The top frame keeps `e1` style ids. Execution still looks up the code-owned node; `frameWins` selects the window. Hit-testing converts to top-viewport coordinates, steps into the frame, and accepts the node or a descendant.

Cross-origin frames are not readable from the parent. With `frames=all`, one `Page.getFrameTree` and one `Target.getTargets` run per observation. Each visible OOPIF gets one snapshot evaluation in its own session. Python merges those actions, prefixes the code-owned frame id, and routes later input to that session. Coordinates in the merged table are top-viewport; the child session dispatches in its own viewport. The 250-action cap is global. `frame_keys` are part of the freshness guard. `frames=all` adds those protocol calls and can be slower than `same`. A frame whose URL does not match an iframe target is skipped.

## File upload

`input[type=file]` is collected as `UPLOAD_FILE`. With no allowlist the action is removed before the model sees the page, so the operation is not offered. `JEV_UPLOAD_FILES` or `Agent(..., files={"resume": "/absolute/resume.pdf"})` expands one target per key. Keys cannot contain a slash or `..`. The target id is `index:key`. The request sent to TypeSafe contains the key and the field label, not the path. Execution calls `DOM.setFileInputFiles` with `os.path.realpath` of the allowlisted entry. A path planted on the action is ignored. An unknown key raises before any CDP call.

## Popup tabs

`JEV_TABS` defaults on (`Agent(..., tabs=True)`). `Target.setDiscoverTargets` runs once at startup. After each action, `drain_events` keeps `Target.targetCreated` / `Target.attachedToTarget` pages whose `openerId` is one of our targets. Other Chrome tabs are ignored. Each accepted page gets a code-owned id `t1`…`t5`. The sixth page is not tracked. The agent activates the new tab and stores `switched to new tab <url>` on that history entry before the next observation. `SWITCH_TAB` targets are those ids. `CLOSE_TAB` closes the current tab and returns to a remaining one. The last tab cannot be closed. Input still uses the session stored for that id, never a model-supplied target id.

## Nested scrolling

`JEV_NESTED_SCROLL` defaults on (`Agent(..., nested_scroll=True)`). During the single snapshot, visible elements with `overflow` of `auto`, `scroll`, or `overlay` and a scroll range above 4 pixels are ranked by visible area. At most eight are kept. Each gets `SCROLL_DOWN` / `SCROLL_UP`, plus `SCROLL_LEFT` / `SCROLL_RIGHT` when the overflow is horizontal. Delta is `0.8 * clientHeight` (or width). Document `scroll_down` / `scroll_up` stay as they are. Container targets carry the code-owned node id. Execution wheels at the current center, reads `scrollTop` or `scrollLeft`, and calls `scrollBy` on that node only when the wheel did not move it. The container list is not rebuilt per action.

## Keyboard

`JEV_KEYBOARD` defaults on (`Agent(..., keyboard=True)`). After `Input.insertText`, the executor reads the node's value (or `innerText` for a contenteditable). A match is done. If the value is unchanged, the control ignored `insertText`: the executor selects all, sends Backspace, then `keyDown` / `keyUp` per character, and reads once more. The text is carried on `keyDown` only; a `char` event with the same text inserts a second copy. A second mismatch is stored as `text entry failed` and is not retried. If the value changed to something else, the site accepted the text (a combobox often rewrites it) and the executor leaves it in place. `PRESS_KEY` targets are exactly Enter, Tab, Escape, ArrowDown, ArrowUp, ArrowLeft, ArrowRight, Space, and Backspace. Any other key is rejected before a CDP key event. The event goes to the focused node.

## Large native selects

`JEV_LARGE_SELECT` defaults on. `JEV_SELECT_INLINE_MAX` defaults to 12 (`Agent(..., select_inline_max=12)`). A `<select>` with more enabled options than that emits one `SELECT_OPTION` action instead of one action per option. The option list is not copied into the TypeSafe request. The text helper returns a label. `match_option` accepts one exact label, otherwise one case-insensitive label, otherwise one prefix, otherwise one contains. Disabled options and ambiguous sets do not match. No match skips the mutation and records `no matching option`. A match executes the existing native-select path with the option's value. Smaller selects keep `index:option` targets.

## Vision fallback

`JEV_VISION` defaults off (`Agent(..., vision=False)`). This is the one path that clicks a coordinate instead of a code-owned node, and it lives in `vision.py`. It runs only when the flag is on and either the page has no click, fill, select, or upload target, or a canvas/svg covers at least 60% of the viewport. The snapshot then records those surface rectangles. One JPEG is captured if the page does not already have one. The model must return `{x, y, confirm: true}`. The point is rejected unless it lies in the viewport and inside one of those rectangles. A rejected point falls through to the normal operation choice. An accepted point is dispatched with `Input.dispatchMouseEvent` and logged as `coordinate-based`, including on stderr. Closed shadow roots, and cross-origin frames whose target URL cannot be matched, stay unsupported.
