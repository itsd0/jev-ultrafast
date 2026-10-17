# Faster on the real web

The current video completes the Google Flights task in **7.073 seconds at 1×**. It starts with one natural-language goal and uses dynamic controls throughout. Jev selects operation + target in one request; Mercury generates the city strings when TYPE_TEXT is selected.

[Video](demo.mp4) · [Recording measurements](flights-measurement.json) · [Matched run measurements](full-speed-measurement.json)

## Matched runtime comparison

Six alternating runs, one task, one existing Chrome profile. Both arms used the same natural-language goal, independent result checker, 1120×780 viewport, TypeSafe `jev-1.13.0`, `inception/mercury-2.5`, disabled text reasoning, and action/request budgets. Initial navigation is excluded in both arms. Each run creates and closes its own tab. All six attempts are included; no provider or verification failures occurred.

| Pair | Original runtime | Optimized runtime | Verified |
| --- | ---: | ---: | --- |
| 1 | 11.214 s | 6.964 s | Both |
| 2 | 8.984 s | 7.913 s | Both |
| 3 | 9.450 s | 7.092 s | Both |
| **Median** | **9.450 s** | **7.092 s** | **3/3 each** |

The optimized runtime was faster in all three pairs. Median task time was **25.0% lower**, median TypeSafe requests fell **22 → 17**, and median browser protocol calls fell **1,092 → 101**. Three pairs are too few for a strong statistical claim (two-sided sign-test p = 0.25). This is a small controlled-input comparison, not a broad agent benchmark; Google, network responses, routing, and browser caches remain live.

The original arm is the frozen source from `68c077bf79caca4e817b8e8a5854b2efa0c81ff6`. Both arms use Mercury so the runtime comparison does not conflate a helper-model change with code changes. Per-run source hashes, model settings, token counts, helper costs, browser version, protocol counts, and verification results are in the measurement JSON.

## Where the time went

The original loop invalidated decisions on every DOM mutation, including animations. It also read the accessibility tree repeatedly and resolved hundreds of DOM nodes. The new snapshot reads common HTML/ARIA controls in one browser call. Click guards compare the selected target and nearby context, plus document/form state. Current geometry and hit-testing still run before input.

A brief event-based combobox wait lets suggestions arrive before asking Jev to choose from an incomplete popup. Text comes from an actual LLM: the recorded run generated **Zurich in 581 ms** and **London in 346 ms**. Native text replacement was also fixed to issue the browser's select-all command explicitly.

The recording contains **17 Jev requests**, **10 interactions plus one explicit WAIT**, and **two helper calls**. Median Jev latency was **178 ms**. Search executed at **5.217 s**; final verified completion was **7.073 s**. That final interval includes Google's results loading, state changes, and the completion decision. It stays in the video.

Timing begins at the first prediction after initial homepage observation and ends at the accepted DONE choice. It includes text generation, model requests, browser work, stale decisions, and loading. Browser setup, initial navigation, and fresh independent post-run verification are outside the clock. The video contains 186 continuous screencast frames plus the initial screenshot, uses original timestamps, has no opening hold, and adds a 0.5-second final hold. Only the top account/navigation strip is cropped.

The recording reports 90,558 TypeSafe input tokens and 6,325 output tokens across all requests. OpenRouter reported **$0.00006272** for the two text calls. That is the text-helper charge, not total task cost: the TypeSafe responses contain token counts without a billed dollar amount, and browser costs are excluded.

## Other checks

| Task | Time | Independent result |
| --- | ---: | --- |
| Wikipedia: open Gödel’s incompleteness theorems | 2.798 s | Exact article URL |
| Local hotel fixture: search Lisbon, Design, Free cancellation, open Casa Flora | 1.896 s | Property plus all three applied filters |

These are separate smoke checks, not matched speed comparisons. Local browser checks cover moved/replaced/hidden/disabled controls, field and checkbox properties, changed nearby context, overlay blocking, native-select execution, real text replacement, autocomplete arrival, and navigation. Offline tests cover the model contract, stale retries, interrupted mutations, helper validation, and independent trip verification.

After the timed runs, native-select interruption handling was tightened: uncertain mutation results stop instead of being treated as retryable stale reads. Flights does not exercise native SELECT. Its timing and recording hashes are retained unchanged; the final failure path is covered by offline fault injection and local browser checks.

## Development attempts retained

Before freezing the candidate, the original runtime passed once in 9.302 s. Two accessibility-tree/semantic-guard candidates took 9.395 s and 10.157 s. The first direct-DOM candidate took 8.697 s but failed independent verification because name/value extraction was incomplete. Recursive labels and combobox values fixed that failure; subsequent verified diagnostics took 8.051, 8.631, 8.395, 8.385, and 7.741 s. A Mercury diagnostic passed in 7.559 s. These are changed-code development attempts, not the matched comparison above.

A six-call helper probe used the two real flight-field contexts with Gemini 2.5 Flash Lite, Gemini 3.1 Flash Lite, and Mercury 2.5. All returned the correct values in this tiny probe. Mercury then passed the live Flights, Wikipedia, and local filter checks. This does not establish general semantic accuracy. Earlier probes had rejected a model that swapped origin/destination and another that emitted commentary instead of valid JSON.

The previous 11.387-second recording and post-recording 12.898-second policy regression are described in the [original performance report](https://github.com/browser-use/jev-ultrafast/blob/68c077bf79caca4e817b8e8a5854b2efa0c81ff6/docs/performance.md). The older prepared-step prototype remains in [performance-prepared.md](performance-prepared.md). Raw attempts and original-timestamp frames remain in ignored local artifacts.

## Feature flags

`scripts/measure_features.py` loads the local fixtures (shadow, iframe, scroll, keyboard, select, upload) three times with every flag off and three times with the defaults. It records median wall time, CDP call count, and snapshot time. It does not call TypeSafe or the text model.

Re-run on 2026-10-17 against Chrome 155.0.8059.40 started with `--remote-debugging-port=9222` and a dedicated user-data-dir. Browser Harness used `BU_CDP_URL=http://127.0.0.1:9222`. n=3 per arm. Wall time includes six data-URL navigations and one snapshot each. One flags-off run and one default run were much slower than the other two, so the median is the figure to use.

| Arm | Median wall | Median CDP calls | Median snapshot | Samples (wall ms) |
| --- | ---: | ---: | ---: | --- |
| Flags off | 668.4 ms | 20 | 43.4 ms | 668.4, 1181.6, 657.8 |
| Defaults | 653.6 ms | 20 | 34.9 ms | 402.1, 908.2, 653.6 |

Default wall / flags-off wall = 0.978. Three runs are too few to call that a speedup. They do not show the defaults making fixture snapshots slower. `JEV_FRAMES=all` was not in this arm. It adds `Page.getFrameTree`, `Target.getTargets`, and one snapshot evaluation per visible cross-origin frame, so it can be slower than `JEV_FRAMES=same` and was not measured.

### Published smoke checks, same day

Model settings were the local `.env`: TypeSafe `jev-latest` (the published flights arm used `jev-1.13.0`), Mercury 2.5, reasoning off, `JEV_DEBUG=1`, `JEV_VISIBLE_TAB=1`. Feature flags stayed at their defaults unless noted. Mercury returned HTTP 429 on several text calls; the client sleeps 0.5 s then 1 s and retries twice.

| Task | Runs | Wall time | Independent result | Text-call time inside the wall |
| --- | ---: | --- | --- | --- |
| Wikipedia, defaults | 2 | 7.314 s, 9.073 s | Exact article URL both times | 4.744 s (one 429), 6.465 s (two 429s) |
| Wikipedia, every flag off | 1 | 10.120 s | Exact article URL | 7.799 s |
| Hotel fixture, defaults | 1 | 8.240 s | Casa Flora plus Design, Free cancellation, Lisbon | 5.614 s for Lisbon |
| Flights, defaults | 4 attempts | no verified completion | Route/date checks failed or the text call returned 429 | 2.9–12.2 s per successful text call |

The 10% bands around the published numbers are 3.078 s, 2.086 s, and 7.801 s. None of these re-runs landed inside those bands. The fixture table is the flag comparison: turning the defaults on did not add wall time there. On Wikipedia, the flags-off run was the slowest because its text call was the slowest. Subtracting the reported text latency leaves about 2.3–2.6 s of Wikipedia time with or without the flags. The first Wikipedia decision was 6,791 prompt tokens with defaults and 6,084 with the flags off. Hotel's seven TypeSafe calls summed to about 2.36 s and the browser work outside the model calls was about 0.26 s, while the Lisbon text call alone was 5.614 s.

Flights did not produce a time to compare with 7.092 s. One attempt stopped on HTTP 429 before typing a city. Later attempts selected one-way, Zürich, and London, then stayed on the October/November 2026 calendar. The final page text contained no "September". The goal date, 2026-09-20, is before the run date, 2026-10-17. The text model also returned `Zurich` for the Departure field. A keyboard bug found in these runs typed each fallback character twice (`LLoonnddoonn`) and cleared combobox text that `insertText` had already changed. That path now retypes only when the field value is unchanged, and it sends the character on `keyDown` only. After that fix, London was selectable, and the run still blocked at 21.253 s without a verified result. `JEV_FRAMES=all` was not used.

## Limits

The DOM reader still does not implement the full accessible-name algorithm. Open shadow roots are walked when `JEV_SHADOW_DOM` is on. Closed shadow roots are not entered; a no-progress click on a custom-element host is reported as `possible closed shadow root`. Same-origin frames are inlined by default. Cross-origin frames need `JEV_FRAMES=all` and a CDP target whose URL matches the iframe. File uploads require an allowlist. Popup tabs are capped at five and only when this task opened them. Nested scroll keeps eight containers. `PRESS_KEY` is a fixed enum. Canvas clicks exist only with `JEV_VISION=1` and are coordinate-based. A valid operation can still be wrong, and DONE is never independent evidence of success.
