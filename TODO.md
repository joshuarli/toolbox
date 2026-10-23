Prefix: TOOL
Next: 15

# Backlog: tool

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
