"""Resolve release identity for files that already have a recording MBID.

The automatic tag pass stops at recording-level identity when the release is not
unambiguous.  This command revisits those files and chooses a release edition
when the file's album title and track position point at exactly one release
group.  Ambiguous or unknown albums are reported for `music tag`.
"""

import argparse
import re
import sys
import unicodedata

from tool.music.api.musicbrainz import (
    MusicBrainzRequestError,
    enrich,
    release_candidates,
)
from tool.music.constants import GREEN, GREY
from tool.music.metadata import Candidate, Metadata
from tool.music.scan import collect_files
from tool.music.tags import read_tags, write_tags
from tool.music.ui import bold, colored

_EDITION_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_ARTIST_SEPARATORS = (": ", " - ", " – ")


def _normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold().replace("’", "'")
    return " ".join(text.split())


def _album_score(file_album: str | None, release_title: str | None) -> int:
    """How well a release title matches the file's album: 3 exact, 2 compatible, 0 no."""
    album, title = _normalize(file_album), _normalize(release_title)
    if not title:
        return 0
    if album == title:
        return 3
    if any(album.endswith(sep + title) or album.startswith(title + sep) for sep in _ARTIST_SEPARATORS):
        return 2
    if _EDITION_RE.sub("", album) == _EDITION_RE.sub("", title):
        return 2
    return 0


def _track_matches(meta: Metadata, candidate: Candidate) -> bool:
    if bool(meta.tracknumber) and candidate.meta.tracknumber != meta.tracknumber:
        return False
    return not (bool(meta.discnumber) and candidate.meta.discnumber != meta.discnumber)


def _preference(meta: Metadata, candidate: Candidate) -> tuple[object, ...]:
    """Sort key for competing editions of one release group, best first."""
    found = candidate.meta
    return (
        found.releasestatus != "Official",
        bool(meta.media) and _normalize(found.media) != _normalize(meta.media),
        not bool(meta.media) and _normalize(found.media) != "digital media",
        bool(meta.releasecountry) and found.releasecountry != meta.releasecountry,
        not bool(meta.releasecountry) and found.releasecountry != "XW",
        bool(meta.date) and found.date != meta.date,
        found.date or "9999",
        found.discnumber or "999",
        found.tracknumber or "999",
        found.musicbrainz_albumid or "",
    )


def choose_release(meta: Metadata, candidates: list[Candidate]) -> Candidate | None:
    """The best release edition for *meta*, or None when the album is ambiguous or absent."""
    if not bool(meta.album) or not bool(meta.musicbrainz_recordingid):
        return None
    scored = [
        (_album_score(meta.album, c.meta.album), c)
        for c in candidates
        if _track_matches(meta, c) and bool(c.meta.album)
    ]
    scored = [(score, c) for score, c in scored if score > 0]
    if not scored:
        return None
    best = max(score for score, _ in scored)
    top = [c for score, c in scored if score == best]
    groups = {c.meta.musicbrainz_releasegroupid for c in top if bool(c.meta.musicbrainz_releasegroupid)}
    if len(groups) != 1:
        return None
    return min(top, key=lambda c: _preference(meta, c))


def resolve_file(path: str) -> Metadata | None:
    """Resolve and write release identity for one file, or leave it untouched."""
    meta = read_tags(path)
    if not bool(meta.musicbrainz_recordingid):
        return None
    chosen = choose_release(meta, release_candidates(meta.musicbrainz_recordingid))
    if chosen is None:
        return None
    selection = Metadata(
        title=meta.title,
        artist=meta.artist,
        musicbrainz_recordingid=meta.musicbrainz_recordingid,
        musicbrainz_albumid=chosen.meta.musicbrainz_albumid,
        musicbrainz_releasegroupid=chosen.meta.musicbrainz_releasegroupid,
        musicbrainz_trackid=chosen.meta.musicbrainz_trackid,
        tracknumber=chosen.meta.tracknumber,
        discnumber=chosen.meta.discnumber,
    )
    enriched = enrich(selection)
    if not (enriched.is_complete() and bool(enriched.musicbrainz_trackid)):
        return None
    write_tags(path, enriched)
    return enriched


def _print_result(status: str, color: int, path: str, detail: str = "") -> None:
    print(f"  {colored(f'{status:<14s}', color)} {path}{detail}", flush=True)


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Resolve release identity for files that already have a recording MBID. Chooses an "
        "edition only when the file's album and track point at one release group; ambiguous "
        "files are left for `music tag`."
    )
    p.add_argument("paths", nargs="+", help="directories or files to resolve")
    p.add_argument("--dry-run", action="store_true", help="list the queue without network access or writes")


def run(args: argparse.Namespace) -> None:
    files = collect_files(args.paths)
    if not files:
        print("No audio files found.")
        return

    if args.dry_run:
        queued = 0
        for fp, display in files:
            meta = read_tags(str(fp))
            if meta.is_complete() or not bool(meta.musicbrainz_recordingid):
                continue
            queued += 1
            track = f" track {meta.tracknumber}" if bool(meta.tracknumber) else ""
            print(f"  {display}  [{meta.album}{track}]")
        print(f"\n  {queued} files would be resolved.")
        return

    counts = {"resolved": 0, "ambiguous": 0, "needs-identity": 0, "complete": 0}
    print(bold(f"  Resolving {len(files)} files"))
    for fp, display in files:
        meta = read_tags(str(fp))
        if meta.is_complete():
            counts["complete"] += 1
            continue
        if not bool(meta.musicbrainz_recordingid):
            counts["needs-identity"] += 1
            print(f"  {colored(f'{'needs identity':<14s}', GREY)} {display}  (`tool music autotag` first)")
            continue

        try:
            resolved = resolve_file(str(fp))
        except MusicBrainzRequestError as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            sys.exit(1)

        if resolved is None:
            counts["ambiguous"] += 1
            _print_result("ambiguous", GREY, display)
            continue
        counts["resolved"] += 1
        edition = ", ".join(p for p in (resolved.media, resolved.releasecountry, resolved.date) if bool(p))
        _print_result("resolved", GREEN, display, f"  [{resolved.album}" + (f" · {edition}" if edition else "") + "]")

    print()
    print(
        f"  {counts['resolved']} resolved, {counts['ambiguous']} ambiguous, "
        f"{counts['needs-identity']} need identity, {counts['complete']} already complete"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    configure_parser(parser)
    run(parser.parse_args())
