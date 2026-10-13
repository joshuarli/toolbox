# Movie encoding and remux

Local conversion commands use `ffmpeg` and `ffprobe`. The implementation is in [encode.py](../src/tool/movies/encode.py), [mkvtomp4.py](../src/tool/movies/mkvtomp4.py), and [loudnorm.py](../src/tool/movies/loudnorm.py).

## Encode

`tool movies encode` accepts `hq`, `hq-720`, `sq-720`, `web`, `uhq`, or `x265-hq`; run its `--help` for descriptions and options. Outputs are MP4 with HEVC video and AAC audio. Black-border detection/cropping is enabled by default and can be disabled with `--no-crop`.

By default, each output is written beside its source as `<stem>-encoded-<preset>.mp4`; an existing output is skipped.

Sources that already score 72 or higher on the local encode-quality check are skipped to avoid generation loss. `--yes` forces those re-encodes. `--delete` encodes through a temporary output and removes the source only when the final output probes strictly better; it cannot be combined with `--yes`.

HDR-tagged sources stop by default. `--hdr-policy retag` writes SDR color tags without changing pixel values, which is suitable only when the pixels are already SDR. `--hdr-policy tonemap` applies an HDR-to-SDR filter. Both policies emit BT.709 output tags.

The encoder converts compatible text subtitles to MP4 `mov_text`. Selected VobSub/DVD subtitles are extracted as Matroska sidecar files; other image subtitles and font attachments are not included in MP4 output.

## Other conversions

- `movies mkvtomp4` is a dry-run by default. With `--apply`, it copies streams supported by MP4, converts compatible text subtitles, and extracts unsupported audio and subtitle streams into sidecar files. Outputs are staged and published after extraction succeeds; existing output paths are preserved. It refuses files with no video or an unsupported video codec.
- `movies loudnorm` measures loudness, targets −16 LUFS and −1.5 dBTP, then re-encodes the first audio stream while copying the other streams. If measurement fails or returns invalid values, the command exits with an error. If the measured gain is negligible or the `-loudnorm` output already exists, it skips writing.
- `movies merge-srt DIR` merges matching sidecar `.srt` files into MP4/MKV containers.

`movies encode`, `movies mkvtomp4`, and `movies loudnorm` help show the command-specific flags.
