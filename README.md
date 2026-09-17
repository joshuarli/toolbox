# tool

Small, focused command-line utilities.

## Requirements

- macOS for `sdqual`
- Python 3.14, managed with [`uv`](https://docs.astral.sh/uv/)
- [`fio`](https://fio.readthedocs.io/) for SD/removable-media qualification

```sh
brew install fio
uv sync
```

## `tool sdqual`

`sdqual` destructively benchmarks and verifies an entire removable/raw disk.

**Normal mode erases the selected disk. Check the `diskN` identifier carefully.**

```sh
diskutil list
uv run tool sdqual disk4
```

Use longer benchmark samples when comparing cards:

```sh
uv run tool sdqual --seconds 60 disk4
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

The target must be a physical, writable removable/external whole disk. `sdqual`
fails closed for the running macOS system disk and ordinary internal fixed
media, has no `--force` bypass, unmounts before raw access, and leaves the disk
unmounted afterward.

### Persistence check

After a successful full run, test whether the verification data survives a
real power/removal cycle:

```sh
diskutil eject /dev/disk4
# Physically remove and reinsert the card, then find its new identifier:
diskutil list
uv run tool sdqual --verify-only disk4
```

`--verify-only` does not intentionally rewrite the card; it verifies the fio
data written by the preceding full qualification pass.

You can also invoke the package directly:

```sh
uv run python -m tool sdqual --help
```

## Testing

```sh
uv run python -m unittest discover -s tests -v
uv run tool --help
uv run tool sdqual --help
uv build
```

The safety logic, plist interpretation, CLI dispatch, and exact fio argv are
unit-tested without hardware. The destructive raw-device path is intentionally
not integration-tested in ordinary CI because meaningful coverage requires
macOS plus dedicated disposable removable media.
