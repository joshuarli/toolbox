"""Match a whole album folder to one MusicBrainz release and write each track's identity.

Fingerprint and name search identify one file at a time.  A folder carries more
evidence: the album title, album artist, track count, and track positions.  This
command searches releases as a unit and writes only when one release group and one
track mapping survive, so a folder is either matched whole or left alone.
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from tool.music.api.musicbrainz import (
    MusicBrainzRequestError,
    ReleaseSummary,
    ReleaseTrack,
    enrich,
    release_tracklist,
    search_releases,
)
from tool.music.constants import GREEN, GREY, RED
from tool.music.metadata import Candidate, Metadata
from tool.music.resolve import album_score, normalize, preference
from tool.music.scan import collect_files
from tool.music.tags import read_tags, write_tags
from tool.music.ui import bold, colored


def folder_album(metas: list[Metadata]) -> tuple[str, str] | None:
    """The folder's album title and artist, or None when the tags disagree or are absent."""
    albums = Counter(meta.album for meta in metas if meta.album)
    if len({normalize(album) for album in albums}) != 1:
        return None
    artists = Counter(meta.albumartist for meta in metas if meta.albumartist)
    if len({normalize(artist) for artist in artists}) > 1:
        return None
    if not artists:
        artists = Counter(meta.artist for meta in metas if meta.artist)
        if len({normalize(artist) for artist in artists}) != 1:
            return None
    if not albums or not artists:
        return None
    return albums.most_common(1)[0][0], artists.most_common(1)[0][0]


def matching_releases(
    album: str, size: int, hits: list[ReleaseSummary], *, at_least: bool = False
) -> list[ReleaseSummary]:
    """Search hits whose title matches *album* and whose track count fits the folder."""
    if at_least:
        return [hit for hit in hits if hit.track_count >= size and album_score(album, hit.title) > 0]
    return [hit for hit in hits if hit.track_count == size and album_score(album, hit.title) > 0]


def _release_meta(hit: ReleaseSummary) -> Metadata:
    return Metadata(
        album=hit.title,
        musicbrainz_albumid=hit.id,
        musicbrainz_releasegroupid=hit.group_id,
        releasestatus=hit.status,
        releasetype=hit.types,
        date=hit.date,
        releasecountry=hit.country,
        media=hit.media[0] if len(hit.media) == 1 else None,
    )


def choose_edition(
    album: str, size: int, hits: list[ReleaseSummary], folder: Metadata
) -> tuple[ReleaseSummary, bool] | None:
    """The single release among *hits* that the folder can be, or None when it is ambiguous.

    A full folder must match a release with the same track count.  A partial folder may
    match a longer release, and then every file must also agree by title, because track
    positions alone would let any single-file folder claim a track on a larger album.
    Editions are ranked the same way `music resolve` ranks them.
    """
    kept = matching_releases(album, size, hits)
    partial = not kept
    if partial:
        kept = matching_releases(album, size, hits, at_least=True)
    groups = {hit.group_id for hit in kept if hit.group_id}
    if len(groups) != 1:
        return None
    return min(kept, key=lambda hit: preference(folder, Candidate(1.0, _release_meta(hit)))), partial


def map_tracks(
    metas: list[Metadata], tracks: list[ReleaseTrack], *, require_title: bool = False
) -> list[ReleaseTrack] | None:
    """One release track per file, by normalized title or by disc/track number.

    Titles win when they map cleanly, because a folder can carry correct titles with
    wrong track numbers (or none).  Every file must map to a distinct track; a folder
    that matches partially is left unmapped rather than guessed.  With *require_title*,
    a position match must also agree on the track title.
    """

    def agreed(pairs: list[tuple[Metadata, ReleaseTrack | None]]) -> bool:
        for meta, track in pairs:
            if track is None:
                return False
            if require_title and normalize(meta.title) != normalize(track.title):
                return False
        return True

    by_title: dict[str, ReleaseTrack] = {}
    for track in tracks:
        key = normalize(track.title)
        if key in by_title:
            by_title = {}
            break
        by_title[key] = track
    if by_title:
        pairs = [(meta, by_title.get(normalize(meta.title))) for meta in metas]
        if agreed(pairs) and len({track.track_id for _, track in pairs if track}) == len(pairs):
            return [track for _, track in pairs if track]
    if all(meta.discnumber and meta.tracknumber for meta in metas):
        by_position = {(track.disc_number, track.track_number): track for track in tracks}
        wanted = [(meta.discnumber, meta.tracknumber) for meta in metas]
        if len(set(wanted)) == len(wanted):
            pairs = [(meta, by_position.get(key)) for meta, key in zip(metas, wanted, strict=True)]
            if agreed(pairs):
                return [track for _, track in pairs]
    if all(meta.tracknumber for meta in metas) and len({track.disc_number for track in tracks}) == 1:
        by_number = {track.track_number: track for track in tracks}
        wanted = [meta.tracknumber for meta in metas]
        if len(set(wanted)) == len(wanted):
            pairs = [(meta, by_number.get(number)) for meta, number in zip(metas, wanted, strict=True)]
            if agreed(pairs):
                return [track for _, track in pairs]
    return None


def _selection(meta: Metadata, track: ReleaseTrack, summary: ReleaseSummary) -> Metadata:
    return Metadata(
        title=meta.title,
        artist=meta.artist,
        musicbrainz_recordingid=track.recording_id,
        musicbrainz_albumid=summary.id,
        musicbrainz_releasegroupid=summary.group_id,
        musicbrainz_trackid=track.track_id,
        tracknumber=track.track_number,
        discnumber=track.disc_number,
    )


def _describe(summary: ReleaseSummary) -> str:
    media = ", ".join(name for name in summary.media if name)
    edition = " · ".join(part for part in (summary.date, summary.country, media) if part)
    return f"[{summary.title}" + (f" · {edition}" if edition else "") + "]"


def _plan(entries: list[tuple[Path, str]], *, dry_run: bool) -> tuple[str, int]:
    """Match one folder; returns its status and the number of files written."""
    metas = [read_tags(str(path)) for path, _ in entries]
    pending = [meta for meta in metas if not meta.is_complete()]
    if not pending:
        return "complete", 0
    identity = folder_album(pending)
    if identity is None:
        return "no-identity", 0
    album, artist = identity
    chosen = choose_edition(album, len(metas), search_releases(album, artist), pending[0])
    if chosen is None:
        return "ambiguous", 0
    summary, partial = chosen
    mapped = map_tracks(metas, release_tracklist(summary).tracks, require_title=partial)
    if mapped is None:
        return "unmapped", 0
    label = str(Path(entries[0][1]).parent)
    if dry_run:
        print(f"  {colored('would match', GREEN)} {label}  {_describe(summary)}  {len(metas)} files")
        for (_, display), track in zip(entries, mapped, strict=True):
            print(f"    {display} -> {track.disc_number}.{track.track_number} {track.title}")
        return "matched", 0
    written = 0
    for (path, _), meta, track in zip(entries, metas, mapped, strict=True):
        if meta.is_complete():
            continue
        enriched = enrich(_selection(meta, track, summary))
        if not (enriched.is_complete() and bool(enriched.musicbrainz_trackid)):
            return "enrichment-failed", written
        write_tags(str(path), enriched)
        written += 1
    print(f"  {colored('matched', GREEN)} {label}  {_describe(summary)}  {written} files")
    return "matched", written


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Match an album folder to one MusicBrainz release by album title, artist, and track count, "
        "then write each file's release and recording identity. Ambiguous folders are left for "
        "`music tag`."
    )
    p.add_argument("paths", nargs="+", help="album directories or files")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="search MusicBrainz and print the proposed release and track mapping without writing",
    )


def run(args: argparse.Namespace) -> None:
    files = collect_files(args.paths)
    if not files:
        print("No audio files found.")
        return

    folders: dict[Path, list[tuple[Path, str]]] = defaultdict(list)
    for path, display in files:
        folders[path.parent].append((path, display))

    counts: Counter[str] = Counter()
    written = errors = 0
    print(bold(f"  Matching {len(folders)} folders"))
    for entries in folders.values():
        try:
            status, count = _plan(entries, dry_run=args.dry_run)
        except MusicBrainzRequestError as exc:
            print(f"  {colored('error', RED)} {entries[0][1]}: {exc}", file=sys.stderr)
            counts["error"] += 1
            errors += 1
            continue
        counts[status] += 1
        written += count
        if status in {"no-identity", "ambiguous", "unmapped", "enrichment-failed"}:
            print(f"  {colored(f'{status:<17s}', GREY)} {Path(entries[0][1]).parent}")

    print()
    print(
        f"  {counts['matched']} folders matched ({written} files), "
        f"{counts['ambiguous']} ambiguous, {counts['unmapped']} unmapped, "
        f"{counts['no-identity']} without album identity, "
        f"{counts['complete']} already complete, {counts['error']} errors"
    )
    if errors:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="music match-album")
    configure_parser(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
