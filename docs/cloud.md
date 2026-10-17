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

## Measurement: 2026-10-17

Local host was macOS; local Chrome listened on `127.0.0.1:9222` (isolated
headless profile). Cloud used the managed proxy disabled for wiki and hotel and
`us` for flights. Decision provider was TypeSafe (`jev-latest`), field text was
Mercury 2.5 with `reasoning_effort=instant`. n = 3 attempts per cell. Cloud
startup is the create-browser call, measured separately and excluded from total
seconds. Total seconds start at the first decision, as in
[performance.md](performance.md).

| provider | browser | task | run | verified | total s | startup s | actions | decision calls | median decision ms | verifier calls | input tokens | output tokens |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| typesafe | local | wiki | 1 | True | 4.141 | 0.0 | 2 | 5 | 336 | 0 | 36528 | 2969 |
| typesafe | local | wiki | 2 | True | 2.839 | 0.0 | 2 | 4 | 302.5 | 0 | 29636 | 2428 |
| typesafe | local | wiki | 3 | True | 3.448 | 0.0 | 2 | 5 | 296 | 0 | 36405 | 2973 |
| typesafe | local | hotel | 1 | True | 3.275 | 0.0 | 5 | 6 | 265.5 | 0 | 23144 | 1963 |
| typesafe | local | hotel | 2 | True | 2.632 | 0.0 | 5 | 6 | 270.5 | 0 | 23144 | 1972 |
| typesafe | local | hotel | 3 | True | 2.691 | 0.0 | 5 | 6 | 288.0 | 0 | 23140 | 1963 |
| typesafe | local | flights | 1 | True | 9.472 | 0.0 | 10 | 18 | 282.5 | 0 | 110557 | 8580 |
| typesafe | local | flights | 2 | True | 8.876 | 0.0 | 10 | 18 | 288.5 | 0 | 115367 | 8947 |
| typesafe | local | flights | 3 | True | 9.04 | 0.0 | 10 | 19 | 293 | 0 | 120580 | 9405 |
| typesafe | cloud | wiki | 1 | False | 0.0 | 4.827 | 0 | 1 | 421 | 0 | 5046 | 428 |
| typesafe | cloud | wiki | 2 | False | 0.0 | 4.734 | 0 | 1 | 396 | 0 | 5046 | 428 |
| typesafe | cloud | wiki | 3 | True | 29.606 | 4.818 | 2 | 3 | 388 | 0 | 16418 | 1397 |
| typesafe | cloud | hotel | 1 | skipped |  |  |  |  |  |  |  |  |
| typesafe | cloud | hotel | 2 | skipped |  |  |  |  |  |  |  |  |
| typesafe | cloud | hotel | 3 | skipped |  |  |  |  |  |  |  |  |
| typesafe | cloud | flights | 1 | True | 58.888 | 4.801 | 10 | 11 | 350 | 0 | 63126 | 4872 |
| typesafe | cloud | flights | 2 | False | 56.499 | 4.767 | 12 | 16 | 352.0 | 0 | 147488 | 10586 |
| typesafe | cloud | flights | 3 | True | 68.863 | 4.887 | 13 | 17 | 340 | 0 | 100357 | 7669 |

Medians use verified runs only (`n` is the verified count); every attempt stays
in the table above.

| task | local median s | local n | cloud median s | cloud n | cloud startup s | per-action overhead |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| wiki | 3.448 | 3/3 | 29.606 | 1/3 | 4.818 | +13,079 ms |
| hotel | 2.691 | 3/3 | n/a | 0/3 | n/a | n/a |
| flights | 9.040 | 3/3 | 63.876 | 2/3 | 4.844 | +4,768 ms |

Per-action overhead is `(cloud median - local median) / cloud median actions`
using the verified medians above. Cloud startup was 4.73-4.89 s across runs and
is not included in either total.

### Failures and limits

- Cloud wiki runs 1 and 2 stopped after the first decision with
  `Model provider returned HTTP 400`. That is the Mercury text helper, not CDP.
  Run 3 verified but spent 24.7 s waiting for Wikipedia before the text call.
- Cloud flights run 2 reached the date picker and then repeated two clicks until
  the no-progress stop; the independent route/date verifier failed. Runs 1 and 3
  verified.
- Cloud hotel was not attempted: the fixture at `127.0.0.1:8766` is not
  reachable from the cloud browser and `JEV_FIXTURE_PUBLIC_URL` was unset.
  It is marked `skipped (fixture not reachable)`.
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
