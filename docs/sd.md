# `tool sd`

`sd` destructively benchmarks and verifies an entire removable/raw disk.

**Normal mode erases the selected disk. Check the `diskN` identifier carefully.**

Requires macOS 26+ and `fio` >= 3.42 (checked on every run):

```sh
brew install fio
```

The dumbed-down default is interactive: pick a card from a numbered list
and it runs and reports overall health.

```sh
uv run tool sd
```

Power users can pass the disk and flags explicitly:

```sh
diskutil list
uv run tool sd disk4
```

Use longer benchmark samples when comparing cards:

```sh
uv run tool sd --seconds 60 disk4
```

The performance suite is deliberately QD1:

- 1 MiB sequential read
- 1 MiB sequential write
- 4 KiB random read
- 4 KiB random write
- 4 KiB 70/30 random read/write

It then writes fio verification data across the raw device and performs an
unrestricted CRC32C readback pass. Any fio I/O or verification error is a
qualification failure. Performance is reported without an arbitrary grade;
compare bandwidth, IOPS, and especially high-percentile latency across cards
using the same reader and port.

The target must be a physical, writable removable/external whole disk. `sd`
fails closed for the running macOS system disk and ordinary internal fixed
media, has no `--force` bypass, unmounts before raw access, and leaves the disk
unmounted afterward.

## Persistence check

After a successful full run, test whether the verification data survives a
real power/removal cycle:

```sh
diskutil eject /dev/disk4
# Physically remove and reinsert the card, then find its new identifier:
diskutil list
uv run tool sd --verify-only disk4
```

`--verify-only` does not intentionally rewrite the card; it verifies the fio
data written by the preceding full qualification pass.
