# `tool notes`

Two CLI tools for the hardcoded Obsidian vault at
`~/iCloud~md~obsidian/Documents/Notes`:

- **`clean`** — move unreferenced `z-images/` files, duplicate notes, and empty
  notes to the Trash. Dry run by default; `--apply` is recoverable from Trash.
- **`localize`** — download remote `![alt](https://...)` images into
  `z-images/` as WebP and rewrite the embeds. Dry run by default; `--apply`
  downloads and rewrites.

```bash
uv run tool notes clean
uv run tool notes clean --apply
uv run tool notes localize
uv run tool notes localize --apply
```
`localize` positional paths are optional scope limiters: relative paths resolve
against the current directory, falling back to the vault; missing paths and
paths outside the vault are errors.

## Layout

```
src/tool/notes/
├── common.py       # vault path, shared errors, image conventions
├── clean.py        # image reference and duplicate-note cleanup
└── localize.py     # remote-image parser, downloader, WebP rewrite
```

## `clean`

`clean` lists files directly inside `z-images/` and shells out to `rg` for one
fixed-string reference scan. It excludes `.trash` and `.obsidian`. `--apply`
moves files with the system `trash` command, so the operation remains
recoverable from the Trash. Duplicate and empty-note cleanup uses exact file
bytes and keeps the shortest vault-relative path, with alphabetical tie
breaking.

## `localize`

`localize` handles inline `![alt](https://...)` image embeds outside fenced and
inline code. Local paths, data URLs, HTML image tags, reference-style images,
and known video/social page URLs remain unchanged. Downloads use bounded
retries, reject oversized/non-image responses, and store content-addressed
WebP files. Failed downloads leave their original embeds intact.
