# Browser Use Cloud mode

Cloud browsers are opt-in. Local Chrome remains the default and the only path
that `Browser` uses unless `JEV_BROWSER=cloud` is set.

| Variable | Default | Effect |
| --- | --- | --- |
| `JEV_BROWSER` | `local` | `cloud` provisions one Browser Use Cloud browser per run. Invalid values stay local. |
| `JEV_CLOUD_PROXY_COUNTRY` | `us` | Managed proxy country. `none` sends `proxyCountryCode: null`. |
| `JEV_CLOUD_FALLBACK_COUNTRY` | empty | Proxy country for the one fresh cloud browser after a CAPTCHA wall. Empty keeps the session country. |
| `JEV_CLOUD_TIMEOUT_MIN` | `15` | Session minutes, clamped to 1-240. |
| `JEV_CLOUD_PROFILE_ID` | empty | Cloud profile UUID. |
| `JEV_CLOUD_RECORD` | `0` | `enableRecording`. |
| `JEV_CLOUD_CUSTOM_PROXY` | empty | JSON `{host, port, username, password}`. |
| `JEV_CLOUD_SHOW_LIVE_URL` | `0` | Print the live URL to stderr. It is never written to artifacts. |
| `JEV_CLOUD_LOCALE` | this machine | Locale for `Emulation.setLocaleOverride`. `none` disables. |
| `JEV_CLOUD_TIMEZONE` | this machine | Timezone for `Emulation.setTimezoneOverride`. `none` disables. |
| `JEV_MAX_DECISIONS` | `80` | Hard stop for decision calls per run. |

Every request also sends `browserScreenWidth: 1120`, `browserScreenHeight: 780`,
and `allowResizing: false`. Cloud mode defaults `JEV_VISIBLE_TAB` to on so the
live view shows the owned tab. `ensure_daemon()` is skipped: the named daemon
was already created by `start_remote_daemon()`, and a dead cloud daemon must not
fall back to local Chrome. `Agent.close()` stops the cloud browser in a
`finally`; an atexit hook and SIGINT/SIGTERM handlers use the same idempotent
stop. The API key is never printed.

Uploads are disabled in cloud mode. Local paths are not visible to the remote
browser, so `JEV_UPLOAD_FILES` is ignored with one warning. A future upload path
would use `POST /api/v2/files/browsers/{session_id}/presigned-url`; the
destination path is undocumented.

## Text helper content filter

The first cloud wiki pass exposed an HTTP 400 from the Mercury text helper:
`{"error":{"type":"content_filter_error","code":"content_filter"}}`. The request
was well-formed; the visible Wikipedia page text tripped the provider's content
filter. `post_json()` now logs the redacted response body, and `field_text()`
retries once without `page.text` while keeping the goal, field, title, and
recent actions. A persistent filter skips the step as a text failure instead of
aborting the run. Ten consecutive live calls failed before the fallback; five
of five succeeded after it.

## CDP round trips

Cloud mode now:

- rechecks the guard, resolves the target geometry, and reads the previous field
  value in one `Runtime.evaluate`;
- reads the post-insert value and `document.readyState` in one call, so a ready
  page skips the separate ready-state request;
- reads the live focused control if a click replaced the observed node, instead
  of falsely retyping every character;
- polls `document.readyState` every 500 ms after navigation instead of every
  20 ms.

Local mode keeps the original sequence of calls.

One cloud wiki profile, grouped per action (decision-only calls excluded):

| action | calls before | calls after |
| --- | ---: | ---: |
| fill "Search Wikipedia" | 80 | 10 |
| click the article result | 8 | 7 |
| mean per action | 44.0 | 8.5 |

The fill's 68 key events came from a false verification failure: Wikipedia
replaces the focused input when the search box is clicked, so the observed node
was disconnected. The live-control read removes that retype. Mean calls per
action fell by 80.7%.

## Date-picker loop

The snapshot exposes `pressed` (with `checked`, `selected`, `expanded`) and the
committed value on the fill and "Open …" click twins. Three generic guards stop
a control that is already in the goal state from being clicked again:

- a fill whose observed value already equals the generated text is skipped with
  a `value already set` history note instead of re-typed;
- a click whose same-labelled control still carries the same value, or is still
  pressed/selected, is held for two steps with a `value unchanged` or
  `already selected` note;
- a target that goes stale at execution time is held for two steps instead of
  being re-picked against the same page.

Only those satisfied-step notes reach the model; `text entry failed` and the
other failure notes stay internal.

## Loop detector and decision budget

A run keeps the last 8 executed `(operation, target label, url, title)` steps
and whether each one made progress (a new url/title or a satisfied marker). An
A-B or A-B-C tail that repeats twice with no progress is a loop:

- the repeated targets are held for three steps and the next history entry says
  `loop detected: avoid X/Y`;
- the third firing in one run stops the run with `status = blocked`
  (`blocked_reason = loop detected`) and no further model calls.

A hard decision budget of `JEV_MAX_DECISIONS` (default 80) also stops the run
cleanly with `blocked_reason = decision budget`; it replaces the old 2x
`MAX_STEPS` exception. No site-specific rules are involved.

## Cloud viewport and locale

The cloud window ignores the metrics override sent at attach. After the first
navigation, cloud mode re-applies
`Emulation.setDeviceMetricsOverride(1120x780, deviceScaleFactor=1, mobile=false)`
and reads `innerWidth`/`innerHeight`/`devicePixelRatio` back, logging a mismatch
if it still differs. In this pass every cloud run logged a mismatch: the window
stayed at 1120x524/534/568 with DPR 1-2, while local is 1120x780 at DPR 1. The
re-apply did not change it.

`Emulation.setLocaleOverride` is rejected by the cloud Chrome, so it is recorded
in `cloud.UNSUPPORTED_METHODS` and skipped. `Emulation.setTimezoneOverride` is
applied to match this machine.

## Measurement: 2026-10-17

Local host was macOS (Asia/Bangkok); local Chrome listened on
`127.0.0.1:9222` (isolated headless profile). Cloud used the managed proxy
disabled for wiki and `us` for flights, with the timezone matched to local.
Decision provider was TypeSafe (`jev-latest`), field text was Mercury 2.5 with
`reasoning_effort=instant`. Flights ran 5 times per browser and wiki 3 times.
Cloud startup is the create-browser call, measured separately and excluded from
total seconds. Total seconds start at the first decision, as in
[performance.md](performance.md). Hotel was not rerun.

| provider | browser | task | run | verified | total s | startup s | actions | decision calls | median decision ms | verifier calls | input tokens | output tokens |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| typesafe | cloud | wiki | 1 | True | 11.158 | 4.761 | 2 | 3 | 391 | 0 | 15760 | 1332 |
| typesafe | cloud | wiki | 2 | True | 9.223 | 5.146 | 2 | 3 | 278 | 0 | 16612 | 1399 |
| typesafe | cloud | wiki | 3 | True | 9.636 | 5.142 | 2 | 3 | 344 | 0 | 15804 | 1344 |
| typesafe | cloud | flights | 1 | True | 49.976 | 5.002 | 10 | 11 | 306 | 0 | 63474 | 4849 |
| typesafe | cloud | flights | 2 | False | 173.259 | 4.963 | 23 | 80 | 342.0 | 0 | 1107884 | 76208 |
| typesafe | cloud | flights | 3 | True | 54.735 | 4.879 | 10 | 11 | 378 | 0 | 63485 | 5165 |
| typesafe | cloud | flights | 4 | False | 93.444 | 6.098 | 20 | 31 | 340 | 0 | 370060 | 25744 |
| typesafe | cloud | flights | 5 | True | 57.751 | 4.965 | 14 | 15 | 319 | 0 | 84645 | 6532 |
| typesafe | local | wiki | 1 | True | 3.714 | 0.0 | 2 | 4 | 341.5 | 0 | 29447 | 2388 |
| typesafe | local | wiki | 2 | True | 4.512 | 0.0 | 3 | 6 | 310.5 | 0 | 42106 | 3450 |
| typesafe | local | wiki | 3 | True | 3.971 | 0.0 | 3 | 6 | 317.5 | 0 | 42096 | 3477 |
| typesafe | local | flights | 1 | False | 34.541 | 0.0 | 35 | 66 | 309.5 | 0 | 525595 | 38817 |
| typesafe | local | flights | 2 | False | 12.997 | 0.0 | 12 | 20 | 289.0 | 0 | 103494 | 8657 |
| typesafe | local | flights | 3 | False | 21.304 | 0.0 | 26 | 35 | 298 | 0 | 185442 | 14656 |
| typesafe | local | flights | 4 | False | 9.313 | 0.0 | 10 | 15 | 300 | 0 | 87059 | 6934 |
| typesafe | local | flights | 5 | False | 28.422 | 0.0 | 27 | 45 | 296 | 0 | 229796 | 18537 |

Medians use verified runs only (`n` is the verified count); every attempt stays
in the table above.

| task | local median s | local n | cloud median s | cloud n | cloud startup s | per-action overhead |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| wiki | 3.971 | 3/3 | 9.636 | 3/3 | 5.142 | +2,833 ms |
| flights | n/a | 0/5 | 54.735 | 3/5 | 4.965 | n/a |

Per-action overhead is `(cloud median - local median) / cloud median actions`
using the verified medians above. Cloud startup was 4.76-6.10 s across runs and
is not included in either total.

### Failures and limits

- Cloud flights run 2 hit the 80-decision budget in the results pager's
  "Next"/"Previous" cycle. Run 4 was stopped by the loop detector at 31
  decisions, `blocked_reason = loop detected`. Runs 1, 3, and 5 verified. The
  date picker was not the cause: every run clicked Done on the first try.
- Local flights was 0/5 this pass. The isolated Chrome profile was served
  Google's alternate Flights UI ("Explore deals with AI" / "When, where, and how
  do you want to travel?"), where the classic route/date/results verifier
  cannot pass. Earlier local passes on the classic UI verified 3/3, so this is
  the served variant, not a code change. Runs 2 and 4 additionally aborted on
  an invalid TypeSafe response (`Invalid TypeSafe response; no action
  executed`). A fresh profile would be needed for a classic-UI comparison.
- Cloud wiki was 3/3. Each run paid one content-filtered text request before the
  page-text-free retry succeeded.
- Cloud hotel was not rerun. The fixture at `127.0.0.1:8766` is not reachable
  from the cloud browser and `JEV_FIXTURE_PUBLIC_URL` was unset.
- Every cloud run logged a viewport mismatch; the metrics re-apply did not fix
  it (1120x524-568 DPR 1-2 vs local 1120x780 DPR 1).
- No CDP method failed this pass. The locale override is skipped, not attempted.
  `Runtime.evaluate`, `Input.insertText`, `Input.dispatchMouseEvent`,
  `Input.dispatchKeyEvent`, `Target.createTarget`, `Target.attachToTarget`,
  `Target.setDiscoverTargets`, `Emulation.setDeviceMetricsOverride`,
  `Emulation.setFocusEmulationEnabled`, `Emulation.setTimezoneOverride`,
  `Network.enable`, and `Page.captureScreenshot` all succeeded.
- Raw rows are in `artifacts/browser-measurement.json` (gitignored). It contains
  no API keys, live URLs, or CDP URLs.

Local was measured with an isolated headless Chrome, while the cloud browser is
headful behind a managed proxy. Network distance and rendering differ; these
numbers describe this setup, not a general latency claim.
