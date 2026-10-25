"""The metadata model shared by the AcoustID/MusicBrainz clients, `tags.py` and `tag.py`.

Field names are MusicBrainz Picard's internal tag names.  In particular
`musicbrainz_recordingid` is the recording MBID and `musicbrainz_trackid` is the
*release track* MBID; `tags.py` maps them to Picard's (historically confusing)
on-disk names.  Scalar fields are `str | None`, multi-value fields are lists.
"""

from dataclasses import MISSING, dataclass, field, fields


@dataclass
class Metadata:
    # Descriptive
    title: str | None = None
    artist: str | None = None  # artist credit as printed, e.g. "A feat. B"
    artists: list[str] = field(default_factory=list)  # individual credited names
    artistsort: str | None = None
    album: str | None = None
    albumartist: str | None = None
    albumartistsort: str | None = None
    tracknumber: str | None = None  # position on the medium (not the printed "A1")
    totaltracks: str | None = None  # tracks on this medium
    discnumber: str | None = None
    totaldiscs: str | None = None
    date: str | None = None  # release date, as precise as MusicBrainz knows
    originaldate: str | None = None  # release-group first release date
    genre: list[str] = field(default_factory=list)
    releasecountry: str | None = None
    releasestatus: str | None = None
    releasetype: list[str] = field(default_factory=list)  # primary type, then secondary types
    media: str | None = None  # medium format: "CD", "Digital Media", ...
    label: list[str] = field(default_factory=list)
    catalognumber: list[str] = field(default_factory=list)
    barcode: str | None = None
    isrc: list[str] = field(default_factory=list)
    # MusicBrainz identity
    musicbrainz_recordingid: str | None = None
    musicbrainz_trackid: str | None = None  # release track
    musicbrainz_albumid: str | None = None  # release
    musicbrainz_releasegroupid: str | None = None
    musicbrainz_artistid: list[str] = field(default_factory=list)
    musicbrainz_albumartistid: list[str] = field(default_factory=list)
    musicbrainz_workid: list[str] = field(default_factory=list)
    # Acoustic identity
    acoustid_id: str | None = None
    acoustid_fingerprint: str | None = None
    # Relationship-derived credits
    composer: list[str] = field(default_factory=list)
    lyricist: list[str] = field(default_factory=list)
    writer: list[str] = field(default_factory=list)
    arranger: list[str] = field(default_factory=list)
    producer: list[str] = field(default_factory=list)
    conductor: list[str] = field(default_factory=list)
    remixer: list[str] = field(default_factory=list)
    performer: list[str] = field(default_factory=list)  # "Name (instrument)"

    def to_values(self) -> dict[str, list[str]]:
        """Non-empty fields as `{name: [values]}`, in declaration order."""
        out: dict[str, list[str]] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if v:
                out[f.name] = v if isinstance(v, list) else [v]
        return out

    @classmethod
    def from_values(cls, values: dict[str, list[str]]) -> Metadata:
        """Inverse of `to_values`.  Also splits legacy "3/12" track/disc numbers."""
        kwargs: dict[str, str | list[str]] = {}
        for f in fields(cls):
            vals = [s for v in values.get(f.name, []) if (s := v.strip())]
            if vals:
                kwargs[f.name] = vals if f.default_factory is not MISSING else vals[0]
        for num, total in (("tracknumber", "totaltracks"), ("discnumber", "totaldiscs")):
            n, _, t = str(kwargs.get(num, "")).partition("/")
            if t:
                kwargs[num] = n.strip()
                kwargs.setdefault(total, t.strip())
        return cls(**kwargs)  # ty: ignore[invalid-argument-type]

    def present(self) -> dict[str, str]:
        """Non-empty fields as display strings (for scan and diffs)."""
        return {k: show(k, v) for k, v in self.to_values().items()}

    def is_complete(self) -> bool:
        """Whether the file carries durable MusicBrainz identity.

        Optional fields (label, ISRC, credits, ...) legitimately may not exist, so only
        identity counts.  A recording with no release has no album to identify.
        """
        release = (
            (self.musicbrainz_albumid and self.musicbrainz_releasegroupid) or self.album is None or not self.album
        )
        return bool(self.title and self.artist and self.musicbrainz_recordingid and release)


@dataclass
class Candidate:
    """One selectable match: an exact recording on an exact release/medium/track
    (where known), with whatever descriptive fields the lookup already supplied."""

    score: float
    meta: Metadata


@dataclass(frozen=True)
class Artist:
    """A MusicBrainz artist, as used by recommendations."""

    mbid: str
    name: str
    comment: str = ""  # MusicBrainz disambiguation, e.g. "French band"


_FINGERPRINT_SHOWN = 16


def show(name: str, values: list[str]) -> str:
    """Human display of a field's values; abbreviates the multi-KB fingerprint."""
    if name == "acoustid_fingerprint":
        fp = values[0]
        return f"{fp[:_FINGERPRINT_SHOWN]}… ({len(fp)} chars)" if len(fp) > _FINGERPRINT_SHOWN else fp
    return "; ".join(values)
