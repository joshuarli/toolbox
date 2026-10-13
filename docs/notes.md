# `tool notes`

These commands operate on the Obsidian vault at `~/iCloud~md~obsidian/Documents/Notes` by default. Both accept `--vault` to select another vault.

## Clean

`uv run tool notes clean` previews files it would move to the system Trash. It finds unreferenced files directly inside the vault's `z-images` folder, byte-identical non-empty Markdown notes (MD5 groups candidates, then byte comparisons confirm duplicates), and empty notes. Duplicate groups keep the shortest vault-relative path, breaking ties alphabetically. Pass `--apply` to move the other files to Trash; `rg` and the system `trash` command are required.

## Localize

`uv run tool notes localize [paths...]` previews remote Markdown image embeds it can download. Pass `--apply` to convert successful downloads to WebP in the vault's z-images folder and rewrite those embeds as Obsidian image links. Existing local images, HTML/reference images, code examples, and known video/social page URLs are left alone; failed downloads keep their original embeds.

Paths limit the scan to files/directories within the selected vault. Relative paths resolve from the current directory first, then from the vault; missing or outside-vault paths are errors. Markdown notes must be readable UTF-8; unreadable or invalid UTF-8 notes stop the command with an error. Symlinked notes are followed only when their resolved destination stays inside the selected vault. WebP conversion requires `ffmpeg`. `--quality` selects WebP quality from 1 to 100.

See `uv run tool notes clean --help` and `uv run tool notes localize --help` for all options. Implementation: [clean.py](../src/tool/notes/clean.py), [localize.py](../src/tool/notes/localize.py), and [common.py](../src/tool/notes/common.py).
