# `tool` CLI bootstrap design

## Goal

Bootstrap `joshuarli/tool` as a standard Python 3.14 `uv` application and make the existing macOS SD-card qualification utility the first subcommand:

```text
tool sdqual disk4
tool sdqual --seconds 60 disk4
tool sdqual --verify-only disk4
```

The project remains dependency-free at runtime; `fio` is an external system prerequisite for `sdqual`.

## Project layout

Use the current standard packaged `uv` application layout:

```text
.
├── .gitignore
├── .python-version
├── README.md
├── pyproject.toml
├── uv.lock
├── src/
│   └── tool/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       └── sdqual.py
└── tests/
    └── test_sdqual.py
```

`pyproject.toml` will declare Python `>=3.14`, no Python dependencies, `uv_build` as the build backend, and:

```toml
[project.scripts]
tool = "tool.cli:main"
```

`.python-version` pins `3.14` for local `uv` use.

## CLI architecture

`src/tool/cli.py` owns only top-level argument parsing and subcommand dispatch. It uses stdlib `argparse` and registers `sdqual` as a subparser.

`src/tool/sdqual.py` owns all SD-card-specific behavior: disk discovery and validation, destructive confirmation, `diskutil` plist parsing, raw-device handling, `fio` command construction/execution, reporting, and verify-only mode.

`src/tool/__main__.py` delegates to `tool.cli:main`, so both of these work:

```text
uv run tool sdqual disk4
uv run python -m tool sdqual disk4
```

No CLI framework dependency is warranted for one small command hierarchy.

## `sdqual` behavior

Preserve the approved qualification flow:

1. macOS-only guard.
2. Accept only a whole-disk identifier (`diskN` or `/dev/diskN`), never a partition.
3. Use `diskutil ... -plist` plus `plistlib`; never parse human-oriented `diskutil` output for safety decisions.
4. Require the target to be a physical, writable removable/external disk and reject the physical store backing the running macOS root filesystem.
5. Display target identity and require exact destructive confirmation (`ERASE diskN`) in normal mode.
6. Unmount and re-identify the device before raw access.
7. Use `/dev/rdiskN` and invoke all subprocesses with argv lists; never `shell=True`.
8. Run QD1 fio workloads:
   - 1 MiB sequential read
   - 1 MiB sequential write
   - 4 KiB random read
   - 4 KiB random write
   - 4 KiB 70/30 random read/write
9. Finish with an unrestricted full-device write/read CRC32C verification pass.
10. Support `--verify-only` for a post-eject/reinsert persistence check.
11. Keep fio output human-readable and finish with a compact PASS/FAIL summary; do not invent performance grades.
12. Keep the target unmounted after testing.

## Safety model

Safety checks fail closed. There is no `--force` escape hatch for an internal/system disk. `sudo` is used only for `diskutil`/raw fio operations and is refreshed before destructive workloads. `caffeinate` prevents sleep during long qualification runs.

Any fio I/O error or verification mismatch is a qualification failure. Performance values are reported, not graded.

## Testing

Use stdlib `unittest` only. Unit tests cover logic that can be exercised safely without macOS hardware:

- disk identifier parsing/rejection
- physical/removable/internal media gating
- running-system physical-store rejection
- plist interpretation
- `/dev/diskN` -> `/dev/rdiskN` conversion
- exact fio argv construction for each workload
- top-level `tool sdqual` argument dispatch
- verify-only behavior

The destructive macOS/raw-device path cannot be integration-tested in ordinary CI without dedicated disposable hardware; the README will state this explicitly.

Verification for the repository:

```text
uv sync
uv run python -m unittest discover -s tests -v
uv run tool --help
uv run tool sdqual --help
uv build
```

## Documentation

README usage stays concise: prerequisites (`macOS`, Python 3.14 via `uv`, `fio`), installation/run examples, destructive warning, benchmark set, and the recommended physical power-cycle + `--verify-only` persistence check.
