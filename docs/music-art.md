# Music artwork

`tool music art` fetches front covers from [Cover Art Archive](https://musicbrainz.org/doc/Cover_Art_Archive/API) using the **release MBID already stored in the audio file**. If an exact release has no front image, it tries smaller thumbnails and then the release-group front image (which may come from a different edition). It does not guess artwork from artist or album names. No API key, image library, or additional dependency is required.

```sh
uv run tool music art ~/Sync/M --dry-run
uv run tool music art ~/Sync/M --embed
uv run tool music art ~/Sync/M --sidecar
uv run tool music art ~/Sync/M --embed --size 500 --no-group-fallback
```

- Embedding is the default; `--embed` makes it explicit. Supported writable files are FLAC, MP3, MP4/M4A, Ogg Vorbis, Ogg FLAC and Opus. Existing artwork is skipped; `--force` replaces the **front** cover and preserves other pictures where the container permits. Existing descriptive and MusicBrainz tags are left alone.
- `--sidecar` saves `cover.jpg` (or `cover.png`, depending on the response) once per album directory. Existing `cover.*` or `folder.*` artwork is not overwritten without `--force`. A directory with multiple distinct release MBIDs is skipped rather than guessing which sidecar belongs there. To cover a mixed-album directory, use `--embed`.
- `--dry-run` is offline and does not download or write anything. `--size` accepts 1200 (default), 500, and 250 pixels. A missing thumbnail is retried at smaller sizes before falling back to the release group. `--no-group-fallback` disables the release-group lookup entirely.
- A missing MusicBrainz ID is reported and skipped: first use `music tag`, `music autotag`, or `music resolve` to identify the release. An HTTP/service failure is **not** treated as missing artwork and causes a nonzero exit status. Images are validated as JPEG/PNG and capped at 10 MiB. A fetched cover is reused for all files with the same release identity within one invocation.

`music scan` detects both ffprobe attached pictures and Mutagen embedded artwork, including Ogg's `METADATA_BLOCK_PICTURE` comments.
