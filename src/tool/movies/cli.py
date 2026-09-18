import argparse
from pathlib import Path

import uvloop

from tool.movies import (
    encode,
    generate_movie_library,
    loudnorm,
    merge_srt,
    mkvtomp4,
    scan_encodes,
    scraper,
)
from tool.movies.rename import apply_rename, collect_media, propose_rename
from tool.movies.search import _blob, _name, confidence_breakdown, match_confidence, search

R = "\033[0m"
B = "\033[1m"
D = "\033[2m"
CY = "\033[36m"
BW = "\033[97m"
BC = "\033[96m"
BY = "\033[93m"
BG = "\033[92m"
BR = "\033[91m"


def _cc(pct: int) -> str:
    if pct >= 80:
        return BG
    if pct >= 50:
        return BY
    return BR


def _fmt_conf(query_title: str, query_year: int | None, row) -> str:
    bd = confidence_breakdown(query_title, query_year, row)
    pct = match_confidence(query_title, query_year, row)
    cc = _cc(pct)

    tc = BG if bd["title"] >= 80 else (BY if bd["title"] >= 50 else BR)
    title_part = f"{D}title{R} {tc}{bd['title']}{R}"

    if bd["year"] is not None:
        yc = BG if bd["year"] else BR
        ys = "✓" if bd["year"] else "✗"
        year_part = f"{D}year{R} {yc}{ys}{R}"
    else:
        year_part = None

    pop_part = f"{D}pop {bd['pop']}{R}"
    parts = [title_part] + ([year_part] if year_part else []) + [pop_part]
    breakdown = f"  {D}·{R}  ".join(parts)
    return f"{cc}{B}[{pct}%]{R}  {breakdown}"


def cmd_search(args: argparse.Namespace) -> None:
    results = search(args.query)
    if not results:
        print("No results.")
        return
    for i, row in enumerate(results):
        if i:
            print()
        alt = f"  {D}/{R} {D}{row['alt_title']}{R}" if row["alt_title"] else ""
        rating = f"  {BY}★ {row['rating']:.1f}{R}  {D}({row['rating_count']:,}){R}" if row["rating"] else ""
        dirs = ", ".join(_name(s) for s in _blob(row["directors"]))
        director = f"  {D}dir.{R} {CY}{dirs}{R}" if dirs else ""
        print(f"{B}{BW}{row['title']}{R}{alt}  {BC}({row['year']}){R}{rating}{director}")
        print(f"  {D}https://letterboxd.com/film/{row['slug']}/{R}")
        print(f"  {_fmt_conf(args.query, None, row)}")


def cmd_rename(args: argparse.Namespace) -> None:
    dry_run = not args.apply
    if dry_run:
        print(f"{D}DRY RUN — pass --apply to rename{R}\n")

    paths: list[Path] = []
    for target in args.path:
        paths.extend(collect_media(Path(target)))

    if not paths:
        print("No media files found.")
        return

    n_rename = 0
    n_skip = 0
    n_error = 0

    for path in paths:
        new_path, _match, _year, err = propose_rename(path)

        if err == "already correctly named":
            n_skip += 1
            continue

        if err or new_path is None:
            print(f"  {BR}✗{R} {D}{path.name}{R}  {BR}{err or 'unknown error'}{R}")
            n_error += 1
            continue

        if not dry_run:
            rename_err = apply_rename(path, new_path)
            if rename_err:
                print(f"  {BR}✗{R} {D}{path.name}{R}  {BR}{rename_err}{R}")
                n_error += 1
                continue

        print(f"  {D}{path.name}{R}  {D}→{R}  {B}{BW}{new_path.name}{R}")
        n_rename += 1

    total = len(paths)
    skipped = f", {D}{n_skip} already correct{R}" if n_skip else ""
    errors = f", {BR}{n_error} error{'s' if n_error != 1 else ''}{R}" if n_error else ""
    action = "would be renamed" if dry_run else "renamed"
    print(f"\n{D}{total} file{'s' if total != 1 else ''} scanned{R}, {BG}{n_rename} {action}{R}{skipped}{errors}")


def cmd_encode(args: argparse.Namespace) -> None:
    try:
        encode.run_from_args(args)
    except ValueError as e:
        args.parser.error(str(e))


def cmd_loudnorm(args: argparse.Namespace) -> None:
    for file in args.file:
        loudnorm.normalize(file)


def cmd_scan(args: argparse.Namespace) -> None:
    scan_encodes.run_from_args(args)


def cmd_scrape(args: argparse.Namespace) -> None:
    try:
        uvloop.run(scraper.scrape_from_args(args))
    except KeyboardInterrupt:
        pass


def cmd_login(_args: argparse.Namespace) -> None:
    try:
        uvloop.run(scraper.login())
    except KeyboardInterrupt:
        pass


def cmd_kill(_args: argparse.Namespace) -> None:
    try:
        uvloop.run(scraper.kill_browser())
    except KeyboardInterrupt:
        pass


def cmd_mkvtomp4(args: argparse.Namespace) -> None:
    for file in args.file:
        mkvtomp4.run(Path(file), apply=args.apply, dest=args.dest)


def cmd_merge_srt(args: argparse.Namespace) -> None:
    merge_srt.merge_all(args.directory)


def _add_encode_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "encode",
        help="encode files with Apple-friendly HEVC/AAC presets",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("preset", choices=encode.PRESETS.keys())
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument(
        "--hdr-policy",
        choices=("abort", "retag", "tonemap"),
        default="abort",
        help="how to handle HDR-tagged sources",
    )
    p.add_argument(
        "--no-crop",
        action="store_true",
        help="disable automatic black-border detection and cropping",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="force encoding sources that would otherwise be skipped as already decent",
    )
    p.add_argument(
        "--delete",
        action="store_true",
        help="encode to a temporary directory, then delete the source only if the output probes strictly better",
    )
    p.add_argument(
        "--tmp-dir",
        type=Path,
        help="scratch directory for --delete temporary encodes; defaults to the system temp directory",
    )
    p.add_argument(
        "--progress",
        choices=("auto", "live", "plain"),
        default="auto",
        help="progress renderer; live forces ANSI in-place updates",
    )
    p.set_defaults(handler=cmd_encode, parser=p)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Encode/audit video files and manage a Letterboxd-backed movie library."
    )
    sub = parser.add_subparsers(dest="movies_cmd", required=True)

    _add_encode_parser(sub)

    p_mkvtomp4 = sub.add_parser("mkvtomp4", help="remux MKV to MP4 with -c copy (no re-encoding)")
    p_mkvtomp4.add_argument("file", nargs="+", type=Path, help="MKV file(s) to remux")
    p_mkvtomp4.add_argument("--apply", action="store_true", help="actually remux (default is dry run)")
    p_mkvtomp4.add_argument("--dest", default=None, help="output directory (default: same as source)")
    p_mkvtomp4.set_defaults(handler=cmd_mkvtomp4)

    p_loudnorm = sub.add_parser("loudnorm", help="normalize audio loudness to -16 LUFS")
    p_loudnorm.add_argument("file", nargs="+", type=Path)
    p_loudnorm.set_defaults(handler=cmd_loudnorm)

    p_scan = sub.add_parser("scan", help="scan movie files for encode quality issues")
    p_scan.add_argument("paths", nargs="*", type=Path, default=[Path(".")], help="files or directories to scan")
    p_scan.set_defaults(handler=cmd_scan)

    p_search = sub.add_parser("search", help="search the Letterboxd DB")
    p_search.add_argument("query")
    p_search.set_defaults(handler=cmd_search)

    p_rename = sub.add_parser("rename", help="rename video/subtitle files using DB lookup")
    p_rename.add_argument("path", nargs="+", help="file(s) or directories to rename")
    p_rename.add_argument("--apply", action="store_true", help="actually rename (default is dry run)")
    p_rename.set_defaults(handler=cmd_rename)

    p_scrape = sub.add_parser("scrape", help="scrape Letterboxd metadata into the DB")
    p_scrape.add_argument("slug", nargs="?")
    p_scrape.add_argument(
        "--concurrency", type=int, default=2, help="enrichment workers (plus one discovery tab in full mode)"
    )
    p_scrape.add_argument("--live", action="store_true", help="run headed Chromium for debugging")
    p_scrape.add_argument("--telemetry", action="store_true", help="print periodic Chromium/page telemetry")
    p_scrape.add_argument(
        "--rotate-tabs-every",
        type=int,
        default=0,
        help="replace each scraper tab after this many attempts (0 disables rotation)",
    )
    p_scrape.set_defaults(handler=cmd_scrape)

    p_seed = sub.add_parser("seed", help="seed film slugs from a Letterboxd diary/watchlist CSV export")
    p_seed.add_argument("csv", help="export CSV containing Letterboxd URIs")
    p_seed.set_defaults(handler=lambda args: scraper.seed(args.csv))

    p_login = sub.add_parser(
        "login",
        help="log in to Letterboxd in headed Chromium (optional; only rating stats need it)",
    )
    p_login.set_defaults(handler=cmd_login)

    p_kill = sub.add_parser("kill", help="stop the isolated Chromium browser")
    p_kill.set_defaults(handler=cmd_kill)

    p_merge_srt = sub.add_parser("merge-srt", help="merge sidecar .srt files into .mp4/.mkv containers")
    p_merge_srt.add_argument("directory", type=Path, help="directory containing .mp4 and .srt pairs")
    p_merge_srt.set_defaults(handler=cmd_merge_srt)

    p_library = sub.add_parser("generate-library", help="generate or fetch static movie-library metadata")
    library_sub = p_library.add_subparsers(dest="library_cmd", required=True)
    p_library_generate = library_sub.add_parser("generate", help="generate the HTML movie library page")
    p_library_generate.add_argument("dir")
    p_library_generate.set_defaults(handler=lambda args: generate_movie_library.run_generate(args.dir))
    p_library_fetch = library_sub.add_parser("fetch", help="fetch metadata for a specific movie")
    p_library_fetch.add_argument("query")
    p_library_fetch.set_defaults(handler=lambda args: generate_movie_library.run_fetch(args.query))


def build_parser() -> argparse.ArgumentParser:
    """Standalone ``movies`` parser, for direct testing outside the ``tool`` CLI."""
    parser = argparse.ArgumentParser(prog="movies")
    configure_parser(parser)
    return parser
