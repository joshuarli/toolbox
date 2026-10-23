Prefix: TOOL
Next: 15

# Backlog: tool

### TOOL-7 · P2 · Failed sidecar extraction leaves a remux that blocks retry

- **Area:** Data
- **Where:** `src/tool/movies/mkvtomp4.py:253`, `src/tool/movies/mkvtomp4.py:379`, `src/tool/movies/mkvtomp4.py:394`
- **Problem:** The MP4 is published before unsupported streams are extracted. If extraction fails, the MP4 remains; the next run rejects it as an existing output before retrying extraction. A focused run reproduced the failure and then the blocked retry.
- **Recommendation:** Stage the MP4 and sidecars together before publishing, or remove outputs created by a failed attempt.
- **Fix with:** `isolate-effects`, `error-model`
- **Effort:** M

### TOOL-8 · P2 · Failed loudness measurement is reported as a no-op

- **Area:** Errors
- **Where:** `src/tool/movies/loudnorm.py:51`, `src/tool/movies/loudnorm.py:154`
- **Problem:** When ffmpeg fails without emitting parseable measurement JSON, `measure_gain` returns `(0.0, 0.0)`. The caller prints “Already at target loudness” and exits successfully. A focused probe with a failed ffmpeg result reproduced the zero-gain value, so invalid inputs such as files without audio are mistaken for completed measurements.
- **Recommendation:** Check the process status and require valid measurement data before returning a gain; report measurement failures to the caller.
- **Fix with:** `error-model`
- **Effort:** S

### TOOL-9 · P2 · MusicBrainz request errors look like successful empty searches

- **Area:** Errors
- **Where:** `src/tool/music/api/musicbrainz.py:58`, `src/tool/music/tag.py:304`
- **Problem:** `search` catches every exception and returns the same empty list as a successful no-match response. A focused probe raised a timeout and received `[]`; `tag` then prints “No matches found,” hiding a service outage or other request failure.
- **Recommendation:** Preserve request failures as a distinct error or result so the CLI can distinguish outages from empty searches.
- **Fix with:** `error-model`, `isolate-effects`
- **Effort:** S

### TOOL-10 · P2 · Music API requests have no bounded timeout

- **Area:** Errors
- **Where:** `src/tool/music/http.py:45`, `src/tool/music/http.py:58`, `src/tool/music/http.py:92`
- **Problem:** The shared `PoolManager` and both request methods omit a timeout. The current urllib3 default uses the system socket timeout, which is unbounded in this process, so a stalled MusicBrainz, ListenBrainz, or Spotify request can keep a CLI command waiting indefinitely. A focused call confirmed the shared request boundary passes no timeout argument.
- **Recommendation:** Set explicit connect and read limits at the shared request boundary and propagate timeout failures to callers.
- **Fix with:** `error-model`, `isolate-effects`
- **Effort:** S

### TOOL-11 · P2 · The lint target rewrites files during a check

- **Area:** Build
- **Where:** `Makefile:9`, `Makefile:10`, `Makefile:11`, `Makefile:12`
- **Problem:** `make fmtlint` runs Ruff with `--fix --unsafe-fixes` and then formats the repository. Invoking a check can rewrite working files, and `ruff@latest` and `ty@latest` vary independently of the project lockfile.
- **Recommendation:** Keep the check target read-only, separate explicit fix/format commands, and use the locked tool versions.
- **Fix with:** `none`
- **Effort:** S

### TOOL-12 · P3 · Browser setup failures can leave a managed process running

- **Area:** Concurrency
- **Where:** `src/tool/ig/browser.py:80`, `src/tool/ig/browser.py:89`, `src/tool/ig/browser.py:115`, `src/tool/ig/browser.py:121`
- **Problem:** `browser_session` enters its cleanup block only after `ensure_browser` returns. If `_setup_tab` fails after Shadowdriver starts the browser, the caller never receives the context manager and no close runs. A focused setup-failure probe recorded browser start and setup failure with no close event; the process can remain until manually stopped.
- **Recommendation:** Unwind the browser and tab inside `ensure_browser` when setup fails, preserving the original exception.
- **Fix with:** `lifecycle-concurrency`, `error-model`
- **Effort:** S

### TOOL-13 · P3 · Matching MD5 hashes can send distinct notes to Trash

- **Area:** Data
- **Where:** `src/tool/notes/clean.py:170`, `src/tool/notes/clean.py:173`, `src/tool/notes/clean.py:178`, `src/tool/notes/clean.py:311`
- **Problem:** `find_duplicate_groups` treats matching MD5 values as proof of byte identity and `--apply` moves every non-survivor to Trash. Different contents with an intentionally constructed MD5 collision would therefore be classified as duplicates. A focused probe with two different files assigned the same digest confirmed there is no content comparison.
- **Recommendation:** Compare file contents within each hash group before treating notes as duplicates.
- **Fix with:** `durable-state`
- **Effort:** S

### TOOL-14 · P3 · No automated workflow runs the repository checks

- **Area:** Build
- **Where:** `Makefile:14`
- **Problem:** The repository has a test target but no CI workflow configuration. Test and static-check failures are caught only when a developer runs the commands locally, leaving changes without an automated gate.
- **Recommendation:** Add a small CI workflow that runs the read-only static checks and test suite on supported Python versions and platforms.
- **Fix with:** `none`
- **Effort:** M
