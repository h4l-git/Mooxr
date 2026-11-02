"""Command line. With no sources it opens the Mooxr window.

    python -m mooxr
    python -m mooxr "My Band - First Song.mp4"
    python -m mooxr clip.mov --title "First Song" --artist "My Band"
    python -m mooxr one.mp4 two.mp4 --out ~/Music/Spotify
"""

from __future__ import annotations

import argparse
import sys

from .core import DEFAULT_ALBUM, DEFAULT_OUT_DIR, MooxError, moox


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mooxr", description="Moox video files or links into tagged MP3s.")
    parser.add_argument("sources", nargs="*", help="video/audio files or links (none: open the window)")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help=f"output folder (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--title", help="track title (only with a single source)")
    parser.add_argument("--artist", help="artist name")
    parser.add_argument("--album", default=DEFAULT_ALBUM, help=f'album name (default: "{DEFAULT_ALBUM}")')
    parser.add_argument("--cover", help="image file to use as cover art")
    parser.add_argument("--bitrate", default="320k", help="MP3 bitrate (default: 320k)")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing file of the same name")
    args = parser.parse_args(argv)

    if not args.sources:
        from .app import main as open_window
        return open_window()

    if args.title and len(args.sources) > 1:
        parser.error("--title only makes sense with one source")

    failures = 0
    for source in args.sources:
        def show(stage: str, fraction: float) -> None:
            print(f"\r{stage.capitalize()}... {fraction:4.0%}", end="", file=sys.stderr, flush=True)

        try:
            result = moox(
                source, args.out,
                title=args.title, artist=args.artist, album=args.album,
                cover=args.cover, bitrate=args.bitrate, overwrite=args.overwrite,
                on_progress=show,
            )
        except MooxError as problem:
            print(f"\rCouldn't moox {source}: {problem}", file=sys.stderr)
            failures += 1
            continue
        print("\r" + " " * 24 + "\r", end="", file=sys.stderr)
        print(f"Mooxed: {result.artist} - {result.title}  ->  {result.path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
