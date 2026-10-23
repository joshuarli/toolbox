Prefix: TOOL
Next: 15

# Backlog: tool

### TOOL-1 · P1 · Localizing notes can truncate user content on write failure

- **Area:** Data
- **Where:** `src/tool/notes/localize.py:651`
- **Problem:** `Path.write_text` truncates the existing note before writing its replacement. A focused fault injection raised an `OSError` after writing a prefix; `_rewrite_notes` reported `NotesError` while the note remained truncated. A crash or storage error during `notes localize --apply` can therefore destroy the rest of a user's note.
- **Recommendation:** Write each replacement to a sibling temporary file, then atomically replace the note and remove the temporary file on failure.
- **Fix with:** `durable-state`, `os-boundaries`
- **Effort:** M

### TOOL-2 · P2 · Imported usernames can direct media writes outside the media root

- **Area:** Security
- **Where:** `src/tool/ig/cli/import_db.py:174`, `src/tool/ig/media.py:35`, `src/tool/ig/scraper.py:876`, `src/tool/ig/scraper.py:753`
- **Problem:** Database import copies `username` values without validating them. Media paths append that value as a directory component, and the writer creates parent directories before replacing the destination. Calling the real path helper with an imported username of `../../outside` resolves the destination outside the configured media directory. A crafted imported database can make later downloads write outside the archive tree.
- **Recommendation:** Validate imported identifiers and enforce that every resolved media destination remains inside the configured media root at the path boundary.
- **Fix with:** `os-boundaries`
- **Effort:** M

### TOOL-3 · P2 · An interrupted profile-post migration hides existing memberships

- **Area:** Data
- **Where:** `src/tool/ig/db.py:25`, `src/tool/ig/db.py:58`, `src/tool/ig/db.py:88`
- **Problem:** The migration renames the old table, creates and fills a replacement, then drops the old table in `executescript` without an explicit transaction. Reopening a database left immediately after the rename created an empty valid `profile_post`; the next migration check skipped it while the old membership row remained stranded in `profile_post_old`.
- **Recommendation:** Make the migration atomic and make startup recover from an interrupted intermediate schema.
- **Fix with:** `durable-state`
- **Effort:** M

### TOOL-4 · P2 · Music transcode silently replaces existing output

- **Area:** Data
- **Where:** `src/tool/music/transcode.py:81`, `src/tool/music/transcode.py:91`
- **Problem:** Every transcode passes `-y` to ffmpeg, so a same-stem `.m4a` or explicit `-o` destination is replaced without prompting. The command can discard a prior output file without an explicit overwrite choice.
- **Recommendation:** Refuse existing destinations by default and make replacement explicit.
- **Fix with:** `os-boundaries`, `durable-state`
- **Effort:** S

### TOOL-5 · P2 · Localization reports unreadable notes as no work

- **Area:** Errors
- **Where:** `src/tool/notes/localize.py:502`, `src/tool/notes/localize.py:584`, `src/tool/notes/localize.py:670`
- **Problem:** `_read_text` turns both decode and filesystem errors into `None`, and callers silently skip those paths. A focused run with a non-UTF-8 Markdown file printed “No remote images found” and returned success, so the user cannot tell that a selected note was never examined.
- **Recommendation:** Preserve the path and cause, then report skipped files or fail with a `NotesError`; define the policy for non-UTF-8 notes.
- **Fix with:** `error-model`, `isolate-effects`
- **Effort:** S

### TOOL-6 · P2 · Movie search keeps stale text after an indexed field changes

- **Area:** Data
- **Where:** `src/tool/movies/search.py:186`, `src/tool/movies/search.py:190`, `src/tool/movies/scraper.py:908`
- **Problem:** `ensure_fts` rebuilds only when the number of indexed movies changes. Scraping can update a title or director while the count stays constant. A focused run changed a title from “Old title” to “New title”; the index still contained “Old title” after `ensure_fts`, so search results can be silently stale.
- **Recommendation:** Keep FTS rows synchronized with movie updates or track indexed-field freshness rather than only row count.
- **Fix with:** `durable-state`, `test-audit`
- **Effort:** M

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
