import pytest

from tool.movies.search import extract_title_year, parse_filename


@pytest.mark.parametrize(
    ("stem", "expected_title", "expected_year"),
    [
        # Year in parens, resolution/source junk after
        ("Bumming in Beijing The Last Dreamers (1990) Bluray-576p", "Bumming in Beijing The Last Dreamers", 1990),
        # Year in parens with parenthetical director credit
        ("Disorder (2009, Huang Weikai) [ICdBGTy5g40]-encoded-hq", "Disorder", 2009),
        # Year in parens with director + YouTube ID
        ("Floating (2004, Huang Weikai) [QXprc7TSBkU]", "Floating", 2004),
        # Dot-separated scene name: dots become spaces, year is anchor
        ("Linda.Linda.Linda.2005.1080p.WEBRip.x264.AAC-[YTS.MX]", "Linda Linda Linda", 2005),
        # Year in parens with director name, encoder suffix
        ("The Silent Holy Stones (Pema Tseden, 2005)-encoded-hq", "The Silent Holy Stones", 2005),
        # Dot-separated with encoder group and suffix
        ("The.Traveler.1974.1080p.BluRay.x264-iBEX-encoded-hq", "The Traveler", 1974),
    ],
)
def test_extract_title_year(stem, expected_title, expected_year):
    title, year = extract_title_year(stem)
    assert title == expected_title
    assert year == expected_year


@pytest.mark.parametrize(
    ("filename", "expected_title", "expected_year", "expected_ext"),
    [
        (
            "Bumming in Beijing The Last Dreamers (1990) Bluray-576p.mkv",
            "Bumming in Beijing The Last Dreamers",
            1990,
            "mkv",
        ),
        ("Disorder (2009, Huang Weikai) [ICdBGTy5g40]-encoded-hq.mp4", "Disorder", 2009, "mp4"),
        ("Floating (2004, Huang Weikai) [QXprc7TSBkU].webm", "Floating", 2004, "webm"),
        ("Linda.Linda.Linda.2005.1080p.WEBRip.x264.AAC-[YTS.MX].mp4", "Linda Linda Linda", 2005, "mp4"),
        ("The Silent Holy Stones (Pema Tseden, 2005)-encoded-hq.mp4", "The Silent Holy Stones", 2005, "mp4"),
        ("The.Traveler.1974.1080p.BluRay.x264-iBEX-encoded-hq.mp4", "The Traveler", 1974, "mp4"),
    ],
)
def test_parse_filename(filename, expected_title, expected_year, expected_ext):
    title, year, ext = parse_filename(filename)
    assert title == expected_title
    assert year == expected_year
    assert ext == expected_ext


def test_no_year():
    title, year = extract_title_year("Some Movie Without A Year")
    assert title == "Some Movie Without A Year"
    assert year is None


def test_no_year_parse_filename():
    title, year, ext = parse_filename("Some Movie Without A Year.mkv")
    assert title == "Some Movie Without A Year"
    assert year is None
    assert ext == "mkv"
