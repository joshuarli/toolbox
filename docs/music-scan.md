# Music scan verdicts

`tool music scan` uses `ffprobe` to report audio streams and reads tags through the same tag adapter used by `music tag`. It does not modify the scanned files. A directory scan recurses over known audio extensions; a single file gets a detailed stream/tag breakdown.

For lossless files, a spectral heuristic looks for abrupt energy drops across five high-frequency bands. It scans a 60-second section around the middle of long files. Lossy codecs and DSD are reported separately; low overall energy or no high-frequency content makes the result inconclusive. A suspected brickwall is evidence of a lossy transcode, not proof of file provenance.

The default brickwall drop threshold is 25 dB. Other tunable thresholds cover low energy (−50 dB), missing 15 kHz energy (−72 dB), and missing energy above 25 kHz in hi-res files (−50 dB). See `tool music scan --help` for the corresponding flags.

Unless `--no-mastering` is set, scan also runs an EBU R 128 measurement on a separate pass. It flags true peak above 0 dBTP as clipping and loudness range below 5 LU as dynamically flat; the defaults can be changed with `--true-peak-limit` and `--lra-flat-limit`. Long files use a middle 60-second sample, so this is a screening result rather than a full-program mastering measurement.

The spectral detector is implemented in [verdict.py](../src/tool/music/verdict.py); EBU R 128 analysis is in [loudness.py](../src/tool/music/loudness.py), and CLI collection/reporting is in [scan.py](../src/tool/music/scan.py).
