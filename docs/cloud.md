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

## Measurement: 2026-10-17

Local host was macOS; local Chrome listened on `127.0.0.1:9222` (isolated
headless profile). Cloud used the managed proxy disabled for wiki and `us` for
flights. Decision provider was TypeSafe (`jev-latest`), field text was Mercury
2.5 with `reasoning_effort=instant`. n = 3 attempts per cell. Cloud startup is
the create-browser call, measured separately and excluded from total seconds.
Total seconds start at the first decision, as in [performance.md](performance.md).
This pass reran wiki and flights; hotel was not rerun.

| provider | browser | task | run | verified | total s | startup s | actions | decision calls | median decision ms | verifier calls | input tokens | output tokens |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| typesafe | local | wiki | 1 | True | 3.082 | 0.0 | 2 | 4 | 306.5 | 0 | 29612 | 2451 |
| typesafe | local | wiki | 2 | True | 3.086 | 0.0 | 2 | 5 | 286 | 0 | 36412 | 2994 |
| typesafe | local | wiki | 3 | True | 2.635 | 0.0 | 2 | 3 | 284 | 0 | 22743 | 1884 |
| typesafe | local | flights | 1 | True | 8.006 | 0.0 | 10 | 18 | 289.5 | 0 | 115199 | 8960 |
| typesafe | local | flights | 2 | True | 7.657 | 0.0 | 10 | 17 | 286 | 0 | 105883 | 8296 |
| typesafe | local | flights | 3 | True | 8.463 | 0.0 | 10 | 17 | 285 | 0 | 105907 | 8876 |
| typesafe | cloud | wiki | 1 | True | 9.779 | 7.446 | 2 | 3 | 414 | 0 | 15781 | 1335 |
| typesafe | cloud | wiki | 2 | True | 9.536 | 4.802 | 2 | 3 | 357 | 0 | 16418 | 1394 |
| typesafe | cloud | wiki | 3 | True | 9.506 | 4.98 | 2 | 3 | 326 | 0 | 19458 | 1641 |
| typesafe | cloud | flights | 1 | False | 78.487 | 4.742 | 20 | 24 | 327.0 | 0 | 138753 | 10838 |
| typesafe | cloud | flights | 2 | False | 52.189 | 5.268 | 12 | 16 | 344.5 | 0 | 147330 | 10603 |
| typesafe | cloud | flights | 3 | False | 68.054 | 4.795 | 16 | 23 | 353 | 0 | 245721 | 17322 |

Medians use verified runs only (`n` is the verified count); every attempt stays
in the table above.

| task | local median s | local n | cloud median s | cloud n | cloud startup s | per-action overhead |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| wiki | 3.082 | 3/3 | 9.536 | 3/3 | 4.980 | +3,227 ms |
| flights | 8.006 | 3/3 | n/a | 0/3 | n/a | n/a |

Per-action overhead is `(cloud median - local median) / cloud median actions`
using the verified medians above. Cloud startup was 4.74-7.45 s across runs and
is not included in either total.

### Failures and limits

- Cloud flights was 0/3 this pass. All three runs set the correct one-way
  route and November 16, 2026 date, then looped on the date picker's selected
  date and "Open Departure" until the no-progress stop. The independent
  route/date/results verifier failed. The same loop appeared in one of the
  three cloud flights runs in the earlier pass. It is a policy/UI problem, not a
  CDP failure.
- Cloud hotel was not rerun. The fixture at `127.0.0.1:8766` is not reachable
  from the cloud browser and `JEV_FIXTURE_PUBLIC_URL` was unset.
- Cloud wiki was 3/3. Each run paid one content-filtered text request before
  the page-text-free retry succeeded.
- No CDP method failed in cloud mode. `Runtime.evaluate`, `Input.insertText`,
  `Input.dispatchMouseEvent`, `Input.dispatchKeyEvent`, `Target.createTarget`,
  `Target.attachToTarget`, `Target.setDiscoverTargets`,
  `Emulation.setDeviceMetricsOverride`, `Emulation.setFocusEmulationEnabled`,
  `Network.enable`, and `Page.captureScreenshot` all succeeded.
- Raw rows are in `artifacts/browser-measurement.json` (gitignored). It contains
  no API keys, live URLs, or CDP URLs.

Local was measured with an isolated headless Chrome, while the cloud browser is
headful behind a managed proxy. Network distance and rendering differ; these
numbers describe this setup, not a general latency claim.
