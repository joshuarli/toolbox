"""Suggest artists to add to a playlist: seed from the playlist's artists, ask ListenBrainz who
listeners of those artists also play, and drop anyone already in the library.

Prints a ranked list.  The one thing it writes is tags: playlist files with no artist identity
are auto-tagged when their fingerprint match is unambiguous (`tag.autotag`), unless `--no-tag`.
"""

import argparse
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from tool.music.api.listenbrainz import Similar, similar_artists
from tool.music.api.musicbrainz import MusicBrainzRequestError, find_artist
from tool.music.constants import AUDIO_EXTENSIONS
from tool.music.metadata import Artist, Metadata
from tool.music.tag import autotag
from tool.music.tags import read_tags
from tool.music.ui import bold, dim

DEFAULT_LIBRARY = "~/Sync/M"
_VIA_SHOWN = 3
_COMMENT_SHOWN = 50


def _audio_files(root: Path) -> list[Path]:
    return sorted(f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS)


def _credits(meta: Metadata) -> list[tuple[str | None, str]]:
    """(artist MBID or None, name) for each artist credited on a track."""
    ids = meta.musicbrainz_artistid
    if ids and len(meta.artists) == len(ids):
        return list(zip(ids, meta.artists, strict=True))
    if ids:
        return [(i, meta.artist or i) for i in ids]
    return [(None, meta.artist)] if meta.artist is not None and meta.artist else []


def _name_key(name: str) -> str:
    return " ".join(name.casefold().split())


class Seeds:
    """The playlist's artists, weighted by how many tracks each has."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}  # MBID -> display name
        self.weight: dict[str, int] = defaultdict(int)  # MBID -> tracks
        self.unresolved: dict[str, int] = defaultdict(int)  # name -> tracks, no MBID in the tags

    def add(self, mbid: str, name: str, tracks: int = 1) -> None:
        self.names.setdefault(mbid, name)
        self.weight[mbid] += tracks


def _can_autotag() -> bool:
    return bool(os.environ.get("ACOUSTID_API_KEY")) and shutil.which("fpcalc") is not None


def _autotag(f: Path) -> Metadata | None:
    """Identify an untagged file by fingerprint and write its tags; report what happened."""
    meta = autotag(str(f))
    if meta is None:
        print(f"  {f.name}: no unambiguous fingerprint match, left untagged")
        return None
    kind = (
        "recording + release"
        if meta.musicbrainz_albumid is not None and meta.musicbrainz_albumid
        else "recording only; run `tool music tag` to pick the release"
    )
    print(f"  {f.name}: tagged {meta.artist} — {meta.title} ({kind})")
    return meta


def _collect_seeds(files: list[Path], *, tag: bool) -> Seeds:
    seeds = Seeds()
    noted = False
    for f in files:
        meta = read_tags(str(f))
        if tag and not meta.musicbrainz_artistid:
            if _can_autotag():
                meta = _autotag(f) or meta
            elif not noted:
                print("  (set ACOUSTID_API_KEY and install fpcalc to auto-tag untagged files)")
                noted = True
        for mbid, name in _credits(meta):
            if mbid is not None and mbid:
                seeds.add(mbid, name)
            else:
                seeds.unresolved[name] += 1
    return seeds


def _resolve_by_name(seeds: Seeds) -> None:
    """Look up seeds whose files carry no artist MBID, saying what each name resolved to."""
    for name, tracks in sorted(seeds.unresolved.items()):
        artist = find_artist(name)
        if artist is None:
            print(f"  {name}: no clear MusicBrainz match — run `tool music tag` on its files to identify it")
            continue
        extra = f" ({artist.comment})" if artist.comment else ""
        print(f"  {name} → {artist.name}{extra} {dim(artist.mbid[:8])}")
        seeds.add(artist.mbid, artist.name, tracks)


def _known(files: list[Path]) -> tuple[set[str], set[str]]:
    """Artist MBIDs and normalised names already present in *files*."""
    mbids: set[str] = set()
    names: set[str] = set()
    for f in files:
        meta = read_tags(str(f))
        mbids.update(meta.musicbrainz_artistid)
        mbids.update(meta.musicbrainz_albumartistid)
        names.update(_name_key(n) for n in [*meta.artists, meta.artist or "", meta.albumartist or ""] if n)
    return mbids, names


class Suggestion:
    def __init__(self, artist: Artist) -> None:
        self.artist = artist
        self.score = 0.0
        self.via: dict[str, float] = {}  # seed name -> normalised similarity


def _rank(seeds: Seeds, similar: list[Similar], known_mbids: set[str], known_names: set[str]) -> list[Suggestion]:
    # Per-seed scales differ by orders of magnitude; normalise each seed's best match to 1.
    best: dict[str, int] = defaultdict(int)
    for s in similar:
        best[s.seed] = max(best[s.seed], s.score)

    found: dict[str, Suggestion] = {}
    for s in similar:
        a = s.artist
        if a.mbid in seeds.weight or a.mbid in known_mbids or _name_key(a.name) in known_names:
            continue
        sug = found.setdefault(a.mbid, Suggestion(a))
        norm = s.score / best[s.seed]
        sug.score += seeds.weight[s.seed] * norm
        sug.via[seeds.names[s.seed]] = norm
    return sorted(found.values(), key=lambda x: x.score, reverse=True)


def configure_parser(p: argparse.ArgumentParser) -> None:
    p.description = (
        "Suggest artists to add to a playlist directory. Seeds from the playlist's artists: untagged "
        "files are fingerprinted and auto-tagged when the match is unambiguous (disable with --no-tag), "
        "remaining names are looked up on MusicBrainz. Asks ListenBrainz for similar artists and "
        "drops anyone already in the library."
    )
    p.add_argument("playlist", help="playlist directory (or audio file) to seed from")
    p.add_argument(
        "--library",
        default=DEFAULT_LIBRARY,
        help=f"directory of music you already have, excluded from results (default: {DEFAULT_LIBRARY})",
    )
    p.add_argument(
        "--no-tag", action="store_true", help="don't auto-tag untagged playlist files (names are still resolved)"
    )
    p.add_argument("-n", "--limit", type=int, default=30, help="number of suggestions to show (default: 30)")


def run(args: argparse.Namespace) -> None:
    root = Path(args.playlist).expanduser()
    files = _audio_files(root) if root.is_dir() else [root]
    if not files:
        print(f"error: no audio files in {root}", file=sys.stderr)
        sys.exit(1)

    print(f"Seeding from {len(files)} file(s) in {root}...")
    seeds = _collect_seeds(files, tag=not args.no_tag)
    try:
        _resolve_by_name(seeds)
    except MusicBrainzRequestError as exc:
        cause = f": {exc.__cause__}" if exc.__cause__ else ""
        print(f"error: {exc}{cause}", file=sys.stderr)
        sys.exit(1)
    if not seeds.weight:
        print("error: no artists to seed from (tag the files first: `tool music tag`)", file=sys.stderr)
        sys.exit(1)

    library = Path(args.library).expanduser()
    known_mbids, known_names = _known(_audio_files(library) if library.is_dir() else files)

    try:
        similar = similar_artists(list(seeds.weight))
    except Exception as e:
        print(f"error: ListenBrainz lookup failed: {e}", file=sys.stderr)
        sys.exit(1)
    ranked = _rank(seeds, similar, known_mbids, known_names)

    if not ranked:
        print("No new artists found.")
        return

    top = ranked[0].score
    print(f"\nSeeds: {', '.join(seeds.names[m] for m in seeds.weight)}\n")
    width = len(str(min(args.limit, len(ranked))))
    for i, sug in enumerate(ranked[: args.limit], 1):
        via = sorted(sug.via, key=lambda n: sug.via[n], reverse=True)[:_VIA_SHOWN]
        line = f"  {i:>{width}}. {bold(sug.artist.name)}  {sug.score / top * 100:3.0f}  {dim('via ' + ', '.join(via))}"
        if sug.artist.comment:
            line += dim(f"  ({sug.artist.comment[:_COMMENT_SHOWN]})")
        print(line)
