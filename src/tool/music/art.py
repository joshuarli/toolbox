"""Fetch album covers by MusicBrainz release ID and embed or save beside audio."""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from mutagen import MutagenError

from tool.music.api.coverart import (
    Cover,
    CoverArtError,
    InvalidMusicBrainzIdError,
    fetch_cover,
)
from tool.music.artwork import embed_art, has_embedded_art
from tool.music.scan import collect_files
from tool.music.tags import is_writable, read_tags

_SIDECAR_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png")


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = "Fetch front covers from Cover Art Archive using MusicBrainz release IDs."
    p.add_argument("paths", nargs="+", help="audio files or library directories")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--embed", action="store_true", help="embed artwork in audio tags (default)")
    mode.add_argument("--sidecar", action="store_true", help="write cover.jpg/cover.png beside album files")
    p.add_argument("--dry-run", action="store_true", help="report eligible files without downloads or writes")
    p.add_argument("--force", action="store_true", help="replace existing front artwork")
    p.add_argument("--size", type=int, choices=(250, 500, 1200), default=1200)
    p.add_argument("--no-group-fallback", action="store_true", help="do not borrow cover art from another release in the group")


def _ident(meta, *, group_fallback: bool) -> tuple[str | None, str | None]:
    return (meta.musicbrainz_albumid, meta.musicbrainz_releasegroupid if group_fallback else None)


def run(args: argparse.Namespace) -> None:
    files = collect_files(args.paths)
    if not files:
        print("No audio files found.")
        return

    entries = [(path, display, read_tags(str(path))) for path, display in files]
    # A sidecar is shared by a whole directory. Never guess when multiple editions coexist.
    releases: dict[Path, set[tuple[str, str]]] = defaultdict(set)
    for path, _, meta in entries:
        if meta.musicbrainz_albumid:
            releases[path.parent].add(("release", meta.musicbrainz_albumid))
        elif meta.musicbrainz_releasegroupid:
            releases[path.parent].add(("release-group", meta.musicbrainz_releasegroupid))

    cache: dict[tuple[str | None, str | None], Cover | None | CoverArtError] = {}
    finished_dirs: set[Path] = set()
    written = missing = skipped = errors = planned = 0
    for path, display, meta in entries:
        release, group = _ident(meta, group_fallback=not args.no_group_fallback)
        if args.sidecar:
            if path.parent in finished_dirs:
                continue
            if len(releases[path.parent]) > 1:
                print(f"skip  {path.parent}: multiple MusicBrainz releases in one directory")
                finished_dirs.add(path.parent)
                skipped += 1
                continue
            existing = [path.parent / name for name in _SIDECAR_NAMES if (path.parent / name).exists()]
            if existing and not args.force:
                print(f"skip  {path.parent}: existing sidecar ({existing[0].name})")
                finished_dirs.add(path.parent)
                skipped += 1
                continue
        else:
            if not is_writable(str(path)):
                print(f"skip  {display}: unsupported artwork container (run music transcode)")
                skipped += 1
                continue
            if not args.force and has_embedded_art(str(path)):
                print(f"skip  {display}: already has artwork")
                skipped += 1
                continue

        if not release and not group:
            print(f"skip  {display}: no release/release-group MBID (run music tag/resolve)")
            skipped += 1
            continue
        if args.dry_run:
            mode = "save sidecar" if args.sidecar else "embed"
            print(f"would {mode}  {display}: release={release or '-'}, group={group or '-'}")
            if args.sidecar:
                finished_dirs.add(path.parent)
            planned += 1
            continue

        key = (release, group)
        if key not in cache:
            try:
                cache[key] = fetch_cover(release, group, size=args.size, group_fallback=not args.no_group_fallback)
            except CoverArtError as exc:
                cache[key] = exc
        cover = cache[key]
        if isinstance(cover, InvalidMusicBrainzIdError):
            print(f"skip  {display}: {cover} (fix tags or run music tag/resolve)")
            if args.sidecar:
                finished_dirs.add(path.parent)
            skipped += 1
            continue
        if isinstance(cover, CoverArtError):
            print(f"error {display}: {cover}", file=sys.stderr)
            if args.sidecar:
                finished_dirs.add(path.parent)
            errors += 1
            continue
        if cover is None:
            print(f"miss  {display}: no cover in Cover Art Archive")
            if args.sidecar:
                finished_dirs.add(path.parent)
            missing += 1
            continue
        try:
            if args.sidecar:
                dest = path.parent / f"cover.{cover.extension}"
                other = [path.parent / name for name in _SIDECAR_NAMES if (path.parent / name).exists() and (path.parent / name) != dest]
                if other:
                    raise ValueError(f"existing sidecar of another name ({other[0].name}); not creating competing cover")
                with dest.open("wb" if args.force else "xb") as stream:
                    stream.write(cover.data)
                finished_dirs.add(path.parent)
            else:
                if not embed_art(str(path), cover.data, cover.mime, force=args.force):
                    print(f"skip  {display}: already has artwork")
                    skipped += 1
                    continue
        except (OSError, ValueError, MutagenError) as exc:
            print(f"error {display}: {exc}", file=sys.stderr)
            if args.sidecar:
                finished_dirs.add(path.parent)
            errors += 1
            continue
        print(f"ok    {display}: {cover.source} -> {'sidecar' if args.sidecar else 'embedded'}")
        written += 1

    print(f"Art: {written} written, {planned} planned, {skipped} skipped, {missing} missing, {errors} errors")
    if errors:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="music art")
    configure_parser(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
