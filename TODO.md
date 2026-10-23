Prefix: TOOL
Next: 15

# Backlog: tool

### TOOL-14 · P3 · No automated workflow runs the repository checks

- **Area:** Build
- **Where:** `Makefile:14`
- **Problem:** The repository has a test target but no CI workflow configuration. Test and static-check failures are caught only when a developer runs the commands locally, leaving changes without an automated gate.
- **Recommendation:** Add a small CI workflow that runs the read-only static checks and test suite on supported Python versions and platforms.
- **Fix with:** `none`
- **Effort:** M
