# `tool sd`

`sd` qualifies a removable disk by benchmarking it and writing verification data across the entire device.

**A normal run erases the selected disk. Check the whole-disk identifier before confirming.**

Requires macOS 26 or newer and `fio` 3.42 or newer:

```sh
brew install fio
uv run tool sd --help
uv run tool sd
```

With no disk argument, the command offers an interactive picker. You can pass `diskN` or `/dev/diskN`; partition identifiers are rejected. Before raw access, `sd` checks that the target is a physical, writable removable or external whole disk, refuses the running system disk, asks for an exact `ERASE diskN` confirmation, and leaves the target unmounted.

The default performance suite runs five QD1 workloads for 30 seconds each: 1 MiB sequential read/write, 4 KiB random read/write, and 4 KiB 70/30 random read/write. It then writes verification data across the device and checks it with CRC32C. I/O or verification errors fail qualification; performance has no pass grade.

Use `--seconds N` to change the duration of each performance workload. After a successful full run, eject and power-cycle the disk, then verify the saved data without intentionally rewriting it:

```sh
diskutil eject /dev/disk4
# Reinsert the disk and check its current identifier.
diskutil list
uv run tool sd --verify-only disk4
```

`--verify-only` skips the performance suite and checks the verification data from a previous full run.
