# Mercury Decide is experimental

TypeSafe remains the default (`DECISION_PROVIDER` unset or `typesafe`). Mercury Decide is opt-in with `DECISION_PROVIDER=mercury` and `INCEPTION_API_KEY`. Field text in both arms is Mercury 2.5. These runs do not replace the published Wikipedia, hotel, or flights figures in [performance.md](performance.md).

## Why TypeSafe stays the default

On 2026-10-17, Chrome 155 on port 9222, text reasoning `instant`, n=3. “Verified” is an independent page check, not the model’s DONE choice. Decision latency is the median call inside that run.

| provider | task | run | verified | total s | actions | decision calls | median decision ms | verifier calls | input tokens | output tokens |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| typesafe | wiki | 1 | True | 3.352 | 2 | 3 | 291 | 0 | 22765 | 1915 |
| typesafe | wiki | 2 | True | 3.567 | 2 | 3 | 350 | 0 | 22743 | 1871 |
| typesafe | wiki | 3 | True | 3.110 | 2 | 3 | 295 | 0 | 22741 | 1885 |
| typesafe | hotel | 1 | True | 3.212 | 5 | 6 | 285 | 0 | 23144 | 1943 |
| typesafe | hotel | 2 | True | 3.054 | 5 | 6 | 292 | 0 | 23139 | 1957 |
| typesafe | hotel | 3 | True | 3.444 | 5 | 6 | 288.5 | 0 | 23133 | 1957 |
| mercury | wiki | 1 | True | 7.321 | 3 | 5 | 910 | 0 | 36258 | 266 |
| mercury | wiki | 2 | True | 11.515 | 4 | 7 | 843 | 0 | 50795 | 321 |
| mercury | wiki | 3 | True | 12.632 | 2 | 7 | 919 | 0 | 48902 | 289 |
| mercury | hotel | 1 | True | 14.434 | 10 | 11 | 589 | 1 | 38145 | 1305 |
| mercury | hotel | 2 | True | 9.339 | 5 | 6 | 717.5 | 1 | 19501 | 600 |
| mercury | hotel | 3 | True | 15.616 | 9 | 10 | 673 | 1 | 31967 | 767 |
| mercury | flights | 1 | False | 86.388 | 60 | 70 | 956.5 | 0 | 510081 | 1015 |
| mercury | flights | 2 | True | 45.730 | 17 | 23 | 896 | 4 | 160244 | 4397 |
| mercury | flights | 3 | True | 91.249 | 60 | 73 | 967 | 1 | 512393 | 1297 |
| typesafe | flights | 1 | True | 9.593 | 11 | 17 | 316 | 0 | 107191 | 8403 |
| typesafe | flights | 2 | True | 9.240 | 11 | 17 | 299 | 0 | 107195 | 8435 |
| typesafe | flights | 3 | True | 10.735 | 11 | 18 | 291.5 | 0 | 115306 | 8960 |

TypeSafe finished every Wikipedia, hotel, and November 16 2026 flights run with DONE, and the independent checks passed 9/9. Mercury hotel also passed 3/3, after the DONE gate rejected early completion (Lisbon search, filtered results, or Casa Flora not yet proven). Mercury Wikipedia reached the article URL in 3/3, then runs 1 and 2 stopped because the text helper returned invalid JSON, and run 3 chose BLOCKED. Mercury flights took 46–91 s; one failed the route check at the 60-action budget, and the other two did not end on a clean DONE. TypeSafe flights finished in 9.2–10.7 s.

TypeSafe is faster and more reliable for these multi-step tasks. Mercury Decide stays available for experiments. It spends fewer output tokens per decision, and the requirement gate stopped a false hotel DONE, but the extra verifier calls, invalid text replies, and long flights runs are not the default path.

## TypeSafe re-run after the strict text schema

The text helper now asks for `{"text": string}` with `json_schema` and `strict: true`. A parse failure retries once at `reasoning_effort=low`. A second failure logs `error=no valid field value; step skipped` and continues the run. The next nine runs use TypeSafe decisions and Mercury 2.5 text at `instant`. n=3. No text step was skipped.

| provider | task | run | verified | total s | actions | decision calls | median decision ms | verifier calls | input tokens | output tokens |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| typesafe | wiki | 1 | True | 3.539 | 2 | 4 | 373.5 | 0 | 29531 | 2421 |
| typesafe | wiki | 2 | True | 2.679 | 2 | 3 | 299 | 0 | 22772 | 1861 |
| typesafe | wiki | 3 | True | 2.930 | 2 | 3 | 276 | 0 | 22765 | 1869 |
| typesafe | hotel | 1 | True | 3.009 | 5 | 6 | 272 | 0 | 23145 | 1953 |
| typesafe | hotel | 2 | True | 3.354 | 5 | 6 | 275 | 0 | 23133 | 1950 |
| typesafe | hotel | 3 | True | 3.315 | 5 | 6 | 271 | 0 | 23133 | 1973 |
| typesafe | flights | 1 | True | 9.316 | 13 | 20 | 284 | 0 | 123966 | 9726 |
| typesafe | flights | 2 | True | 8.736 | 11 | 16 | 289.5 | 0 | 107000 | 8320 |
| typesafe | flights | 3 | False | 10.147 | 16 | 22 | 273.5 | 0 | 132152 | 10470 |

Wikipedia and the hotel fixture passed 6/6, each ending on DONE. Flights for November 16, 2026 passed 2/3. Run 3 set one-way, Zürich, London, and the date, then clicked Reload until three no-progress actions stopped it. The results check failed. That was not a text-JSON failure.

## TypeSafe re-run after the reload wait

A reload or refresh click that does not change the page now waits up to 3 seconds for a DOM change or for network traffic to finish. An action that still leaves the page unchanged is hidden for the next two decisions. The same TypeSafe plus Mercury 2.5 `instant` setup, Chrome on port 9222, November 16 2026. Wikipedia and hotel n=3, flights n=5. No text step was skipped.

| provider | task | run | verified | total s | actions | decision calls | median decision ms | verifier calls | input tokens | output tokens |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| typesafe | wiki | 1 | True | 3.215 | 2 | 3 | 314 | 0 | 22752 | 1863 |
| typesafe | wiki | 2 | True | 3.083 | 2 | 4 | 299.0 | 0 | 29643 | 2419 |
| typesafe | wiki | 3 | True | 3.166 | 2 | 4 | 290.5 | 0 | 29643 | 2436 |
| typesafe | hotel | 1 | True | 2.973 | 5 | 6 | 300.0 | 0 | 23144 | 1920 |
| typesafe | hotel | 2 | True | 3.428 | 5 | 6 | 278.5 | 0 | 23140 | 1964 |
| typesafe | hotel | 3 | True | 2.699 | 5 | 6 | 284.0 | 0 | 23133 | 1972 |
| typesafe | flights | 1 | True | 9.082 | 11 | 17 | 302 | 0 | 107082 | 8437 |
| typesafe | flights | 2 | True | 9.298 | 11 | 17 | 305 | 0 | 107076 | 8429 |
| typesafe | flights | 3 | True | 8.069 | 11 | 17 | 294 | 0 | 107080 | 8397 |
| typesafe | flights | 4 | True | 9.945 | 10 | 19 | 302 | 0 | 128728 | 9712 |
| typesafe | flights | 5 | True | 8.871 | 11 | 17 | 318 | 0 | 107076 | 8436 |

Wikipedia 3/3, hotel 3/3, and flights 5/5 all ended on DONE, and the independent checks passed 11/11. Flights walls were 8.069–9.945 s.
