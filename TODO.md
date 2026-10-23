Prefix: TOOL
Next: 15

# Backlog: tool

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
