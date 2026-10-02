import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TIPL, TRCK, TXXX
from mutagen.mp4 import MP4, MP4Cover

from tool.music.metadata import Metadata
from tool.music.tags import _WRITABLE_TYPES, _write_tags_direct, read_tags, write_tags

FORMATS = ["flac_file", "mp3_file", "m4a_file", "opus_file", "ogg_file"]

REC = "1db8dba2-0d42-43e2-94d7-0a6a8df7d2d7"
TRACK = "82185c30-f97f-4b1b-8c70-a0bd3d077649"
ALBUM = "490a58ac-6f2d-482e-8915-b703e229794b"
GROUP = "4cda8183-371b-4189-bcb1-265bd0ff4b5f"
ARTIST_A = "7c67fb96-7e24-4893-a020-fcc53a1f7814"
ARTIST_B = "fa34b363-79df-434f-a5b8-be4e6898543f"
ALBUM_ARTIST = "11111111-2222-3333-4444-555555555555"
WORK = "9382d3f9-eff6-41ac-88d7-bdf06dd827c1"
FINGERPRINT = "AQADtImSSEmSJFGkCTgOw8BxHDiOAziOHMexAz94HK-w42h-nCF-_MiP4jj6AVceANqPw8eBoz_wE4X" * 40


def rich() -> Metadata:
    """Every field set, with multi-values and awkward characters."""
    return Metadata(
        title="It Gets Easier",
        artist="Kalandra & Friends",
        artists=["Kalandra", "Friends"],
        artistsort="Kalandra & Friends",
        album="The Line (Deluxe)",
        albumartist="Various Artists",
        albumartistsort="Various Artists",
        tracknumber="11",
        totaltracks="14",
        discnumber="2",
        totaldiscs="3",
        date="2020-10-23",
        originaldate="2019-05",
        genre=["alternative pop", "folk"],
        releasecountry="NO",
        releasestatus="Official",
        releasetype=["album", "compilation"],
        media='12" Vinyl',
        label=["By Norse Music", "Other Label"],
        catalognumber=["BNM017CD", "BNM017LP"],
        barcode="612608050884",
        isrc=["NONAT2001110", "NONAT2001111"],
        musicbrainz_recordingid=REC,
        musicbrainz_trackid=TRACK,
        musicbrainz_albumid=ALBUM,
        musicbrainz_releasegroupid=GROUP,
        musicbrainz_artistid=[ARTIST_A, ARTIST_B],
        musicbrainz_albumartistid=[ALBUM_ARTIST],
        musicbrainz_workid=[WORK, "22222222-2222-2222-2222-222222222222"],
        acoustid_id="e8187d3a-71a8-49ef-8f95-2fa590426190",
        acoustid_fingerprint=FINGERPRINT,
        composer=["Jogeir Daae Maeland", "Katrine Stenbekk"],
        lyricist=["Katrine Stenbekk"],
        writer=["Oskar Johnsen Rydh", "Florian Döderlein Winter"],
        arranger=["Ludovico Einaudi"],
        producer=["Max Richter", "Jogeir Daae Maeland"],
        conductor=["Some Conductor"],
        remixer=["Some Remixer"],
        performer=["Alice (violin)", "Bob (Moog, synthesizer)", "Carol (feat.) (lead vocals)", "Dave"],
    )


@pytest.fixture
def tagged(request):
    return request.getfixturevalue(request.param)


class TestRoundTrip:
    @pytest.mark.parametrize("fmt", FORMATS)
    def test_rich_metadata_round_trips(self, fmt, request):
        path = request.getfixturevalue(fmt)
        write_tags(path, rich())
        assert read_tags(path) == rich()

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_second_write_replaces_values(self, fmt, request):
        path = request.getfixturevalue(fmt)
        write_tags(path, rich())
        write_tags(path, Metadata(title="New", genre=["ambient"], isrc=[], performer=["Eve (cello)"]))
        back = read_tags(path)
        assert back.title == "New"
        assert back.genre == ["ambient"]
        assert back.performer == ["Eve (cello)"]
        # untouched fields (and empty ones in the update) are left alone
        assert back.isrc == rich().isrc
        assert back.musicbrainz_albumid == ALBUM

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_empty_file_reads_empty(self, fmt, request):
        assert read_tags(request.getfixturevalue(fmt)) == Metadata()

    @pytest.mark.parametrize("fmt", ["flac_file", "mp3_file", "m4a_file"])
    def test_track_total_survives_updating_track_number(self, fmt, request):
        path = request.getfixturevalue(fmt)
        write_tags(path, Metadata(tracknumber="3", totaltracks="12", discnumber="1", totaldiscs="2"))
        write_tags(path, Metadata(tracknumber="4", discnumber="2"))
        back = read_tags(path)
        assert (back.tracknumber, back.totaltracks, back.discnumber, back.totaldiscs) == ("4", "12", "2", "2")


class TestPicardMappings:
    """Raw on-disk names must match MusicBrainz Picard's, notably recording vs release track."""

    def test_vorbis(self, flac_file):
        write_tags(flac_file, rich())
        tags = FLAC(flac_file).tags
        assert tags["MUSICBRAINZ_TRACKID"] == [REC]  # recording MBID, despite the name
        assert tags["MUSICBRAINZ_RELEASETRACKID"] == [TRACK]
        assert tags["MUSICBRAINZ_ALBUMID"] == [ALBUM]
        assert tags["MUSICBRAINZ_RELEASEGROUPID"] == [GROUP]
        assert tags["MUSICBRAINZ_ARTISTID"] == [ARTIST_A, ARTIST_B]
        assert tags["MUSICBRAINZ_ALBUMARTISTID"] == [ALBUM_ARTIST]
        assert tags["ACOUSTID_ID"] == ["e8187d3a-71a8-49ef-8f95-2fa590426190"]
        assert tags["ACOUSTID_FINGERPRINT"] == [FINGERPRINT]
        assert tags["TOTALTRACKS"] == ["14"]
        assert tags["GENRE"] == ["alternative pop", "folk"]

    def test_id3(self, mp3_file):
        write_tags(mp3_file, rich())
        id3 = ID3(mp3_file)
        assert id3["UFID:http://musicbrainz.org"].data == REC.encode()  # recording MBID
        assert id3["TXXX:MusicBrainz Release Track Id"].text == [TRACK]
        assert id3["TXXX:MusicBrainz Album Id"].text == [ALBUM]
        assert id3["TXXX:MusicBrainz Release Group Id"].text == [GROUP]
        assert id3["TXXX:MusicBrainz Artist Id"].text == [ARTIST_A, ARTIST_B]
        assert id3["TXXX:MusicBrainz Album Artist Id"].text == [ALBUM_ARTIST]
        assert id3["TXXX:Acoustid Id"].text == ["e8187d3a-71a8-49ef-8f95-2fa590426190"]
        assert id3["TXXX:Acoustid Fingerprint"].text == [FINGERPRINT]
        assert id3["TXXX:MusicBrainz Album Type"].text == ["album", "compilation"]
        assert id3["TRCK"].text == ["11/14"]
        assert id3["TPOS"].text == ["2/3"]
        assert [str(t) for t in id3["TDOR"].text] == ["2019-05"]
        assert id3["TPUB"].text == ["By Norse Music", "Other Label"]
        assert id3["TCOM"].text == ["Jogeir Daae Maeland", "Katrine Stenbekk"]
        assert id3["TMCL"].people[2] == ["lead vocals", "Carol (feat.)"]
        assert ["producer", "Max Richter"] in id3["TIPL"].people
        assert id3.version == (2, 4, 0)

    def test_mp4(self, m4a_file):
        write_tags(m4a_file, rich())
        tags = MP4(m4a_file).tags
        ff = "----:com.apple.iTunes:"
        assert tags[ff + "MusicBrainz Track Id"] == [REC.encode()]  # recording MBID
        assert tags[ff + "MusicBrainz Release Track Id"] == [TRACK.encode()]
        assert tags[ff + "MusicBrainz Album Id"] == [ALBUM.encode()]
        assert tags[ff + "MusicBrainz Release Group Id"] == [GROUP.encode()]
        assert tags[ff + "MusicBrainz Artist Id"] == [ARTIST_A.encode(), ARTIST_B.encode()]
        assert tags[ff + "MusicBrainz Album Artist Id"] == [ALBUM_ARTIST.encode()]
        assert tags[ff + "Acoustid Id"] == [b"e8187d3a-71a8-49ef-8f95-2fa590426190"]
        assert tags[ff + "Acoustid Fingerprint"] == [FINGERPRINT.encode()]
        assert tags["trkn"] == [(11, 14)]
        assert tags["disk"] == [(2, 3)]
        assert tags["\xa9gen"] == ["alternative pop", "folk"]

    def test_mp3_recording_id_is_not_release_track(self, mp3_file):
        write_tags(mp3_file, Metadata(musicbrainz_recordingid=REC))
        back = read_tags(mp3_file)
        assert back.musicbrainz_recordingid == REC
        assert back.musicbrainz_trackid is None


class TestLegacyTags:
    def test_track_total_in_track_number(self, mp3_file):
        id3 = ID3()
        id3.add(TRCK(encoding=3, text=["3/5"]))
        id3.save(mp3_file)
        back = read_tags(mp3_file)
        assert (back.tracknumber, back.totaltracks) == ("3", "5")

    def test_vorbis_tracktotal_alias(self, flac_file):
        audio = FLAC(flac_file)
        audio["TRACKNUMBER"] = "3/5"
        audio["DISCTOTAL"] = "2"
        audio.save()
        back = read_tags(flac_file)
        assert (back.tracknumber, back.totaltracks, back.totaldiscs) == ("3", "5", "2")


class TestPreservesUnrelatedTags:
    def test_flac(self, flac_file):
        audio = FLAC(flac_file)
        audio["COMMENT"] = "keep me"
        audio["REPLAYGAIN_TRACK_GAIN"] = "-6.5 dB"
        pic = Picture()
        pic.data = b"\x89PNG-fake"
        pic.type = 3
        pic.mime = "image/png"
        audio.add_picture(pic)
        audio.save()

        write_tags(flac_file, rich())

        audio = FLAC(flac_file)
        assert audio["COMMENT"] == ["keep me"]
        assert audio["REPLAYGAIN_TRACK_GAIN"] == ["-6.5 dB"]
        assert [p.data for p in audio.pictures] == [b"\x89PNG-fake"]

    def test_opus(self, opus_file):
        from mutagen.oggopus import OggOpus

        audio = OggOpus(opus_file)
        audio["COMMENT"] = "keep me"
        audio.save()
        write_tags(opus_file, rich())
        assert OggOpus(opus_file)["COMMENT"] == ["keep me"]

    def test_mp3(self, mp3_file):
        id3 = ID3()
        id3.add(COMM(encoding=3, lang="eng", desc="", text=["keep me"]))
        id3.add(TXXX(encoding=3, desc="MyCustom", text=["custom"]))
        id3.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=b"\x89PNG-fake"))
        id3.add(TIPL(encoding=3, people=[["engineer", "Eve"], ["producer", "Old Producer"]]))
        id3.save(mp3_file)

        write_tags(mp3_file, rich())

        id3 = ID3(mp3_file)
        assert id3["COMM::eng"].text == ["keep me"]
        assert id3["TXXX:MyCustom"].text == ["custom"]
        assert id3["APIC:"].data == b"\x89PNG-fake"
        # only the roles we own are replaced inside TIPL
        assert ["engineer", "Eve"] in id3["TIPL"].people
        assert ["producer", "Old Producer"] not in id3["TIPL"].people
        assert ["producer", "Max Richter"] in id3["TIPL"].people
        assert ["arranger", "Ludovico Einaudi"] in id3["TIPL"].people

    def test_m4a(self, m4a_file):
        audio = MP4(m4a_file)
        audio["\xa9cmt"] = ["keep me"]
        audio["covr"] = [MP4Cover(b"\x89PNG-fake", imageformat=MP4Cover.FORMAT_PNG)]
        audio["----:com.apple.iTunes:MyCustom"] = [b"custom"]
        audio.save()

        write_tags(m4a_file, rich())

        audio = MP4(m4a_file)
        assert audio["\xa9cmt"] == ["keep me"]
        assert bytes(audio["covr"][0]) == b"\x89PNG-fake"
        assert audio["----:com.apple.iTunes:MyCustom"] == [b"custom"]

    @pytest.mark.parametrize("fmt", FORMATS)
    def test_partial_write_keeps_other_owned_fields(self, fmt, request):
        path = request.getfixturevalue(fmt)
        write_tags(path, Metadata(title="T", artist="A", album="B"))
        write_tags(path, Metadata(title="New Title"))
        back = read_tags(path)
        assert (back.title, back.artist, back.album) == ("New Title", "A", "B")


class TestWriteTagsErrors:
    def test_wav_unsupported(self, wav_file):
        with pytest.raises(SystemExit) as exc:
            write_tags(wav_file, Metadata(title="Test"))
        assert exc.value.code == 1

    def test_nonexistent_file(self):
        # mutagen raises MutagenError for nonexistent files before we can check
        # writability — we treat this as an error (exit 1) too.
        from mutagen import MutagenError

        with pytest.raises((SystemExit, MutagenError)):
            write_tags("/nonexistent/file.xyz", Metadata(title="Test"))

    def test_wav_error_message(self, wav_file, capsys):
        with pytest.raises(SystemExit):
            write_tags(wav_file, Metadata(title="Test"))
        captured = capsys.readouterr()
        assert "cannot write tags" in captured.err.lower()
        assert "transcode" in captured.err.lower()

    def test_nonexistent_read(self):
        assert read_tags("/nonexistent/path.ogg") == Metadata()


class TestWriteTagsDirect:
    def test_unsupported_format_errors(self, wav_file):
        # WAV is not in _WRITABLE_TYPES, so it should exit
        with pytest.raises(SystemExit) as exc:
            _write_tags_direct(wav_file, Metadata(title="Test"))
        assert exc.value.code == 1

    def test_writable_types_are_comprehensive(self):
        # Verify that types handled by _write_tags_direct match _WRITABLE_TYPES
        from mutagen.flac import FLAC as M_FLAC
        from mutagen.mp3 import MP3 as M_MP3
        from mutagen.mp4 import MP4 as M_MP4
        from mutagen.oggflac import OggFLAC as M_OggFLAC
        from mutagen.oggopus import OggOpus as M_OggOpus
        from mutagen.oggvorbis import OggVorbis as M_OggVorbis

        expected = {M_FLAC, M_OggFLAC, M_OggVorbis, M_OggOpus, M_MP3, M_MP4}
        assert set(_WRITABLE_TYPES) == expected
