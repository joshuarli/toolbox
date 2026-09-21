# `tool notes`

Two CLI tools for the hardcoded Obsidian vault at
`~/iCloud~md~obsidian/Documents/Notes`:

- **`clean`** — move files in `z-images/` that no markdown note references,
  plus duplicate and empty notes, to the Trash. Dry run by default;
  `--apply` deletes via the system `trash` command (recoverable from the
  Trash).
- **`localize`** — download remote `![alt](https://...)` images into
  `z-images/` as WebP and rewrite the embeds to `![[<sha>.webp]]`. Dry run by
  default; `--apply` downloads and rewrites.

```bash
uv run tool notes clean                       # dry run: list unreferenced images + duplicate notes
uv run tool notes clean --apply               # move them to the Trash
uv run tool notes localize                    # dry run: list remote images
uv run tool notes localize --apply            # download + convert + rewrite
uv run tool notes localize --apply Clippings/  # limit to a note or directory
make fmtlint
uv run pytest tests/notes
```

`localize` positional paths are optional scope limiters: relative paths
resolve against the current directory, falling back to the vault; missing
paths and paths outside the vault are errors, never silent no-ops.

## Layout

```
src/tool/notes/
├── __init__.py   # empty
├── common.py     # hardcoded VAULT, z-images/ name, WebP quality, NotesError
├── clean.py      # rg reference scan, md5 duplicate scan, trash deletion, run()
├── localize.py   # remote-image parser, downloader, ffmpeg WebP, rewriter, run()
└── cli.py        # configure_parser()/build_parser() for clean + localize
```

## `clean` reference detection

`clean` lists files directly inside `z-images/` (the webp-paste attachment
folder; flat by convention) and shells out to `rg` — Python never scans note
contents. All basenames go into a patterns file and one
`rg -o -F -f patterns -g '*.md'` pass reports which names occur anywhere in
the vault. Plain fixed-string matching covers every Obsidian reference style:

- `![[abc.webp]]`, `![[abc.webp|300]]`, `[[abc.webp]]`
- `![](z-images/abc.webp)`, `![alt](abc.webp)`
- folder-prefixed `![[z-images/abc.webp]]` (what Loupe imports emit)

Files whose exact basename (with extension) appears nowhere are unreferenced.
Extensionless `[[note]]` links are note links, not image references, and do
not keep images alive. The vault's `.trash` and `.obsidian` directories are
excluded: a note rotting in `.trash` must not pin its images.

`--apply` moves files with the system `trash` command (`/usr/bin/trash`,
macOS 15+) in chunks of 500, so deletions are recoverable.

## `clean` duplicate detection

Non-empty `*.md` files are grouped by md5 of their exact bytes; empty
(0-byte) notes are listed separately and every one is trashed. In each
duplicate group the survivor is the shortest vault-relative path, ties
broken alphabetically — so `hackernews.md` is kept over `hackernews 2.md`
and `Custom Linux Kernel.md`, and `dev/Linux/Oasis Linux Server.md` is
kept over its `archive/` copy. The vault's `.trash` and `.obsidian`
directories are excluded from the scan, same as the image reference scan.

## `localize` scope and naming

Scope matches the webp-paste plugin's Loupe import
(`~/d/obsidian-webp-paste`, `src/loupe-import.ts`): inline
`![alt](destination)` embeds with absolute `http(s)` destinations. Out of
scope, left byte-for-byte intact: `data:` URLs, local paths, HTML
`<img src>`, reference-style images, and anything inside fenced code blocks
or inline code spans (detected with a line-based fence tracker plus
best-effort per-line backtick pairing; exotic nested-backtick cases may slip
through — check dry-run output).

Video and social *page* URLs are also skipped, never attempted: clipping
pipelines sometimes serialize a video as `![](https://www.youtube.com/watch?v=…)`,
and those routes never serve image bytes. `is_page_url()` matches only
routes that provably never serve images — YouTube `watch`/`shorts`/`live`/
`embed`/`playlist`/`clips`, `youtu.be`, `vimeo.com`/`player.vimeo.com` pages,
and X/Twitter status and `/i/` routes — while thumbnails on dedicated image
hosts (`i.ytimg.com`, `i.vimeocdn.com`, `pbs.twimg.com`) stay localizable.
Skipped page embeds are listed separately in dry-run output so a
misclassification stays visible.

Downloads send the plugin's browser-like headers (`Accept: image/...`,
Chrome `User-Agent`) with 3 attempts and exponential backoff for transient
network errors and HTTP 408/425/429/5xx. Responses larger than 100 MB are
refused; responses served as HTML/text are skipped without further work;
anything else is handed to the converter, whose failure also leaves the
embed remote.

## WebP conventions

Follow `~/d/obsidian-webp-paste`:

- Quality 85 (`--quality` overrides, 1–100), lossy for every source format.
- Already-WebP bytes (RIFF `....WEBP` magic) are stored as-is, never
  re-encoded — a lossy re-encode would only shed quality.
- Conversion runs `ffmpeg -c:v libwebp -quality 85` (libwebp is the same
  encoder family as the plugin's Chromium canvas path and `cwebp -q 85`).
- The filename is `sha256(final webp bytes).hexdigest() + ".webp"`, so
  identical images deduplicate to one file and reruns converge.
- The rewrite format is `![[<sha>.webp]]` (bare filename, no `z-images/`
  prefix, no size suffix), matching existing vault embeds.

`--apply` downloads unique URLs concurrently (`-j/--jobs`, default 8) and
stores each image with an atomic temp-file-plus-rename so a crash can never
leave a truncated file behind under a valid hash name. Existing files are
never rewritten (same hash implies identical bytes), then each note is
rescanned and rewritten, preserving everything else byte-for-byte. Partial
localization is normal: failures keep their remote embeds and are reported
as `skipped <url> (<reason>)`.
