# Browser Use Cloud mode

Cloud browsers are opt-in. Local Chrome remains the default and the only path
that `Browser` uses unless `JEV_BROWSER=cloud` is set.

| Variable | Default | Effect |
| --- | --- | --- |
| `JEV_BROWSER` | `local` | `cloud` provisions one Browser Use Cloud browser per run. Invalid values stay local. |
| `JEV_CLOUD_PROXY_COUNTRY` | `us` | Managed proxy country. `none` sends `proxyCountryCode: null`. |
| `JEV_CLOUD_TIMEOUT_MIN` | `15` | Session minutes, clamped to 1-240. |
| `JEV_CLOUD_PROFILE_ID` | empty | Cloud profile UUID. |
| `JEV_CLOUD_RECORD` | `0` | `enableRecording`. |
| `JEV_CLOUD_CUSTOM_PROXY` | empty | JSON `{host, port, username, password}`. |
| `JEV_CLOUD_SHOW_LIVE_URL` | `0` | Print the live URL to stderr. It is never written to artifacts. |
| `JEV_CLOUD_LOCALE` | this machine | Locale passed to `Emulation.setLocaleOverride`. `none` disables. |
| `JEV_CLOUD_TIMEZONE` | this machine | Timezone passed to `Emulation.setTimezoneOverride`. `none` disables. |

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
other failure notes stay internal. On the traced Google Flights runs the model
now clicks the date cell and then Done on the first try, the same step local
takes.

## Measurement: 2026-10-17

Local host was macOS (Asia/Bangkok); local Chrome listened on
`127.0.0.1:9222` (isolated headless profile). Cloud used the managed proxy
disabled for wiki and `us` for flights, with the timezone matched to local.
Decision provider was TypeSafe (`jev-latest`), field text was Mercury 2.5 with
`reasoning_effort=instant`. n = 3 attempts per cell. Cloud startup is the
create-browser call, measured separately and excluded from total seconds. Total
seconds start at the first decision, as in [performance.md](performance.md).
This pass reran wiki and flights; hotel was not rerun.

| provider | browser | task | run | verified | total s | startup s | actions | decision calls | median decision ms | verifier calls | input tokens | output tokens |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| typesafe | cloud | wiki | 1 | True | 9.49 | 4.848 | 2 | 3 | 330 | 0 | 15796 | 1341 |
| typesafe | cloud | wiki | 2 | True | 9.548 | 4.809 | 2 | 3 | 275 | 0 | 15805 | 1338 |
| typesafe | cloud | wiki | 3 | True | 11.169 | 4.791 | 2 | 3 | 399 | 0 | 15749 | 1357 |
| typesafe | cloud | flights | 1 | False | 234.163 | 4.834 | 22 | 120 | 329.0 | 0 | 1732342 | 118968 |
| typesafe | cloud | flights | 2 | True | 44.518 | 5.265 | 10 | 11 | 300 | 0 | 63436 | 4870 |
| typesafe | cloud | flights | 3 | True | 56.221 | 5.153 | 10 | 11 | 300 | 0 | 63451 | 4847 |
| typesafe | local | wiki | 1 | True | 3.462 | 0.0 | 2 | 4 | 282.5 | 0 | 29430 | 2426 |
| typesafe | local | wiki | 2 | True | 6.534 | 0.0 | 4 | 8 | 302.0 | 0 | 44953 | 3770 |
| typesafe | local | wiki | 3 | True | 4.397 | 0.0 | 4 | 6 | 323.5 | 0 | 42477 | 3453 |
| typesafe | local | flights | 1 | False | 29.158 | 0.0 | 37 | 52 | 293.5 | 0 | 281076 | 21407 |
| typesafe | local | flights | 2 | True | 12.865 | 0.0 | 12 | 23 | 278 | 0 | 135369 | 10430 |
| typesafe | local | flights | 3 | True | 26.464 | 0.0 | 26 | 45 | 270 | 0 | 268831 | 20582 |

Medians use verified runs only (`n` is the verified count); every attempt stays
in the table above.

| task | local median s | local n | cloud median s | cloud n | cloud startup s | per-action overhead |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| wiki | 4.397 | 3/3 | 9.548 | 3/3 | 4.809 | +2,576 ms |
| flights | 19.665 | 2/3 | 50.370 | 2/3 | 5.209 | +3,071 ms |

Per-action overhead is `(cloud median - local median) / cloud median actions`
using the verified medians above. Cloud startup was 4.79-5.27 s across runs and
is not included in either total.

### Failures and limits

- Cloud flights run 1 reached the results page, then ping-ponged between the
  results pager's "Next"/"Previous" controls until the 120-decision budget
  stopped it. Runs 2 and 3 verified in 44.5 s and 56.2 s. The date picker was
  not the cause: all three runs clicked Done on the first try.
- Local flights run 1 met a different Google Flights variant ("Explore deals
  with AI" / "When, where, and how do you want to travel?") and wandered for 37
  actions; runs 2 and 3 verified.
- Cloud hotel was not rerun. The fixture at `127.0.0.1:8766` is not reachable
  from the cloud browser and `JEV_FIXTURE_PUBLIC_URL` was unset.
- Cloud wiki was 3/3. Each run paid one content-filtered text request before
  the page-text-free retry succeeded.
- `Emulation.setLocaleOverride` is rejected by the cloud Chrome and is recorded
  as unsupported; the timezone override succeeds. Both sides were `en-US`, so
  only the timezone actually differed.
- The cloud window is 1120x559-568 at DPR 1.25-2, while local is 1120x780 at
  DPR 1. The metrics override sent at attach does not stick on the cloud
  browser, so the two sides do not render the same viewport.
- No other CDP method failed. `Runtime.evaluate`, `Input.insertText`,
  `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`, `Target.createTarget`,
  `Target.attachToTarget`, `Target.setDiscoverTargets`,
  `Emulation.setDeviceMetricsOverride`, `Emulation.setFocusEmulationEnabled`,
  `Network.enable`, and `Page.captureScreenshot` all succeeded.
- Raw rows are in `artifacts/browser-measurement.json` (gitignored). It contains
  no API keys, live URLs, or CDP URLs.

Local was measured with an isolated headless Chrome, while the cloud browser is
headful behind a managed proxy. Network distance and rendering differ; these
numbers describe this setup, not a general latency claim.
