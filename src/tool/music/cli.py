import argparse

from tool.music import scan, suggest, tag, transcode


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = "Scan, tag, transcode, and find recommendations for a local audio library."
    sub = parser.add_subparsers(dest="music_cmd", required=True)

    p_scan = sub.add_parser(
        "scan", help="report codec, bitrate, tags, lossy-transcode suspicion, and mastering flags for audio files"
    )
    scan.configure_parser(p_scan)
    p_scan.set_defaults(handler=scan.run)

    p_tag = sub.add_parser("tag", help="fingerprint a file and write MusicBrainz/AcoustID metadata")
    tag.configure_parser(p_tag)
    p_tag.set_defaults(handler=tag.run)

    p_suggest = sub.add_parser("suggest", help="suggest artists to add to a playlist (ListenBrainz similar artists)")
    suggest.configure_parser(p_suggest)
    p_suggest.set_defaults(handler=suggest.run)

    p_transcode = sub.add_parser("transcode", help="transcode audio to AAC/M4A")
    transcode.configure_parser(p_transcode)
    p_transcode.set_defaults(handler=transcode.run)


def build_parser() -> argparse.ArgumentParser:
    """Standalone ``music`` parser, for direct testing outside the ``tool`` CLI."""
    parser = argparse.ArgumentParser(prog="music")
    configure_parser(parser)
    return parser
