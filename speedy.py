#!/usr/bin/env python3
"""Speedy - fast media and file downloader.

Downloads media (YouTube and other yt-dlp supported sites) and plain files
(PDF, DOCX, XLSX, ZIP, images, and any direct download link).

Each URL is routed automatically: web pages go to yt-dlp for media extraction,
direct file links are fetched with Speedy's own downloader (yt-dlp would
mislable a PDF as *.unknown_video). Use --direct or --media to force a path.

Intended for content you own, content licensed for reuse (e.g. Creative
Commons), or content you otherwise have permission to download. Downloading
copyrighted media without permission may violate the platform's Terms of
Service and local copyright law.

Usage examples:
    python speedy.py <url>                    # media page or direct file
    python speedy.py <url> -f 720p
    python speedy.py <url> -a                 # audio only, mp3
    python speedy.py <paper.pdf>              # documents work directly
    python speedy.py <file-url> --segments 8  # faster: 8 parallel connections
    python speedy.py <url> --info             # list available formats
    python speedy.py -b urls.txt              # batch from file
    python speedy.py -b urls.txt --archive done.txt   # skip already-downloaded

Speed: --segments N splits plain files across N connections using aria2c
(requires aria2 installed). Fragmented streams (YouTube/DASH/HLS) use
--connections N instead, which needs no extra tool.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.error
from pathlib import Path

import yt_dlp

import fetch

APP_NAME = "Speedy"
VERSION = "1.1.0"


def human_bytes(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n:.0f}B"
        n /= 1024
    return f"{n:.1f}PiB"


def human_time(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class ProgressPrinter:
    """Single-line progress display written to stderr.

    Works for both plain downloads and fragmented (DASH/HLS) streams, where
    progress arrives per-fragment rather than as a contiguous byte count.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._last_len = 0
        self._finished: set[str] = set()

    def _render(self, line: str) -> None:
        pad = max(0, self._last_len - len(line))
        sys.stderr.write("\r" + line + " " * pad)
        sys.stderr.flush()
        self._last_len = len(line)

    def __call__(self, d: dict) -> None:
        if not self.enabled:
            return

        status = d.get("status")
        info = d.get("info_dict") or {}
        title = (info.get("title") or "media")[:45]

        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            pct = f"{done / total * 100:5.1f}%" if total else "  ?.?%"
            speed = d.get("speed")
            speed_s = f"{human_bytes(speed)}/s" if speed else "--"
            eta = human_time(d.get("eta"))
            size = f"{human_bytes(done)}/{human_bytes(total)}"
            self._render(f"{pct}  {size:>21}  {speed_s:>11}  ETA {eta}  {title}")

        elif status == "finished":
            key = info.get("id", title)
            if key not in self._finished:
                self._finished.add(key)
                self._render(f"100.0%  download complete, post-processing...  {title}\n")

        elif status == "error":
            self._render(f"ERROR  {title}\n")


# Height -> format selector. Highest quality video plus best audio, merged by
# ffmpeg. The "bv*+ba/b" tail is the fallback when no separate streams exist.
VIDEO_FORMATS = {
    "best": "bv*+ba/b",
    "2160p": "bv*[height<=2160]+ba/b[height<=2160]",
    "1440p": "bv*[height<=1440]+ba/b[height<=1440]",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
    "720p": "bv*[height<=720]+ba/b[height<=720]",
    "480p": "bv*[height<=480]+ba/b[height<=480]",
    "360p": "bv*[height<=360]+ba/b[height<=360]",
}


def build_options(args: argparse.Namespace) -> dict:
    outtmpl = str(Path(args.output_dir) / "%(title)s [%(id)s].%(ext)s")

    opts: dict = {
        "outtmpl": outtmpl,
        "quiet": not args.verbose,  # verbose surfaces yt-dlp's own diagnostics
        "no_warnings": not args.verbose,
        "ignoreerrors": args.batch,  # keep going if one URL in a batch fails
        "noprogress": True,     # disable yt-dlp's built-in bar
        "progress_hooks": [ProgressPrinter(enabled=not args.no_progress)],
        "retries": 10,
        "fragment_retries": 10,
        # Fragmented (DASH/HLS) streams: fetch this many chunks in parallel.
        "concurrent_fragment_downloads": args.connections,
        "windowsfilenames": True,
        "restrictfilenames": False,
        # Resume a partially downloaded file instead of restarting it.
        "continuedl": not args.no_resume,
        "part": True,
    }

    if args.segments > 1:
        # Plain HTTP files arrive as one continuous stream, which yt-dlp cannot
        # split itself. aria2c does ranged multi-connection downloads instead.
        if shutil.which("aria2c"):
            n = args.segments
            opts["external_downloader"] = {"default": "aria2c"}
            opts["external_downloader_args"] = {
                "aria2c": [
                    "-x", str(n),              # connections per server
                    "-s", str(n),              # parallel splits
                    "-k", "1M",                # minimum split size
                    "--file-allocation=none",  # skip slow preallocation
                    "--no-conf",               # ignore the user's aria2c config
                ]
            }
        else:
            print("warning: aria2c not found, falling back to a single "
                  "connection (install aria2 for segmented downloads)",
                  file=sys.stderr)

    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)

    if args.rate_limit:
        opts["ratelimit"] = args.rate_limit * 1024  # KiB/s -> bytes/s

    if args.archive:
        opts["download_archive"] = args.archive

    if args.playlist:
        opts["yes_playlist"] = True
    else:
        # Download only the single video even if the URL points at a playlist.
        opts["noplaylist"] = True

    if args.audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": args.audio_format,
                "preferredquality": args.audio_quality,
            }
        ]
        # Multichannel sources (e.g. 5.1 AC-3) cannot be encoded to opus, and
        # are often unwanted elsewhere; downmix to the requested channel count.
        opts["postprocessor_args"] = {
            "extractaudio": ["-ac", str(args.channels)]
        }
    else:
        opts["format"] = VIDEO_FORMATS[args.format]
        opts["merge_output_format"] = args.merge_format

    if args.subtitles:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = args.subtitles_langs
        opts["subtitlesformat"] = "srt/best"

    if args.thumbnail:
        opts["writethumbnail"] = True
        opts["postprocessors"] = opts.get("postprocessors", []) + [
            {"key": "EmbedThumbnail", "already_have_thumbnail": False}
        ]

    if args.metadata:
        opts["postprocessors"] = opts.get("postprocessors", []) + [
            {"key": "FFmpegMetadata", "add_metadata": True}
        ]

    return opts


def list_formats(url: str, cookies_from_browser: str | None) -> int:
    opts: dict = {"quiet": True, "no_warnings": True, "listformats": True}
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return 0


def download(urls: list[str], args: argparse.Namespace) -> int:
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    opts = build_options(args)

    # Keyed by video id: postprocessors run in sequence, so the last hook to
    # report a given id wins -- that is the final, post-conversion path.
    saved: dict[str, str] = {}

    def collect_postproc(d: dict) -> None:
        if d.get("status") != "finished":
            return
        info = d.get("info_dict") or {}
        path = info.get("filepath") or d.get("filepath")
        key = info.get("id") or path
        if path and key:
            saved[key] = path

    opts["postprocessor_hooks"] = [collect_postproc]

    with yt_dlp.YoutubeDL(opts) as ydl:
        # return code: 0 ok, 1 some downloads failed, 2 all failed / fatal
        rc = ydl.download(urls)

    if saved:
        print(f"\n{len(saved)} file(s) saved to "
              f"{Path(args.output_dir).resolve()}:", file=sys.stderr)
        for path in saved.values():
            print(f"  {Path(path).name}", file=sys.stderr)

    return rc


def read_urls(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def is_direct_file(url: str, timeout: float = 30) -> bool:
    """Decide whether a URL is a plain file rather than a media page.

    A one-byte ranged request is enough: HTML means a web page (let yt-dlp
    handle it), anything else means a file we should fetch ourselves. Failing
    to reach the server returns False so yt-dlp can report the real error.
    """
    try:
        info = fetch.probe(url, timeout=timeout)
    except Exception:
        return False
    return not fetch.looks_like_webpage(info)


def load_archive(path: str) -> set[str]:
    """Read a download-archive file into a set of recorded keys."""
    try:
        with open(path, encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except FileNotFoundError:
        return set()


def record_archive(path: str, key: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(key + "\n")


def download_direct(urls: list[str], args: argparse.Namespace) -> int:
    """Fetch plain files (documents, archives, media) with the native fetcher."""
    printer = ProgressPrinter(enabled=not args.no_progress)
    out = Path(args.output_dir)
    failures = 0
    done = load_archive(args.archive) if args.archive else set()

    for url in urls:
        # Show the URL's basename rather than the full URL, which is too long
        # for a single-line progress display.
        label = Path(url.split("?", 1)[0].rstrip("/")).name or url

        if args.archive and url in done:
            print(f"  skipping (already downloaded): {label}", file=sys.stderr)
            continue

        def on_progress(ndone, total, speed, eta, _l=label):
            printer({"status": "downloading", "info_dict": {"title": _l},
                     "downloaded_bytes": ndone, "total_bytes": total,
                     "speed": speed, "eta": eta})

        try:
            path = fetch.download(url, out, segments=args.segments,
                                  resume=not args.no_resume,
                                  progress=on_progress)
        except (urllib.error.URLError, ValueError, RuntimeError, OSError) as exc:
            failures += 1
            printer({"status": "error", "info_dict": {"title": label}})
            print(f"error: {url}: {exc}", file=sys.stderr)
            if not args.batch:
                return 1
            continue
        if args.archive:
            record_archive(args.archive, url)
        if not args.no_progress:
            sys.stderr.write("\r" + " " * 100 + "\r")
        print(f"  saved {path.name} ({human_bytes(path.stat().st_size)})",
              file=sys.stderr)

    if failures:
        print(f"{failures} of {len(urls)} download(s) failed", file=sys.stderr)
    return 1 if failures else 0


def run_urls(urls: list[str], args: argparse.Namespace) -> int:
    """Route each URL to the direct fetcher or to yt-dlp.

    Explicit --direct / --media flags win; otherwise each URL is probed. This
    keeps one command working for both a YouTube link and a PDF.
    """
    if args.direct:
        return download_direct(urls, args)

    if args.media:
        return download(urls, args)

    direct = [u for u in urls if is_direct_file(u)]
    media = [u for u in urls if u not in direct]

    rc = 0
    if direct:
        rc |= download_direct(direct, args)
    if media:
        rc |= download(media, args)
    return rc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="speedy",
        description=f"{APP_NAME} - fast media and file downloader.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Only download content you are allowed to download.",
    )
    p.add_argument("--version", action="version",
                   version=f"{APP_NAME} {VERSION}")
    p.add_argument("url", nargs="*",
                   help="one or more URLs (media pages or direct files)")
    p.add_argument("-b", "--batch", metavar="FILE",
                   help="read URLs from a text file (one per line, # comments ok)")
    p.add_argument("-o", "--output-dir", default="downloads",
                   help="destination directory (default: downloads)")
    p.add_argument("-f", "--format", default="best", choices=sorted(VIDEO_FORMATS),
                   help="max video quality (default: best)")
    p.add_argument("-a", "--audio-only", action="store_true",
                   help="extract audio only")
    p.add_argument("--audio-format", default="mp3",
                   choices=["mp3", "m4a", "opus", "flac", "wav", "best"],
                   help="audio codec when using -a (default: mp3)")
    p.add_argument("--audio-quality", default="0",
                   help="audio quality, 0=best (default: 0)")
    p.add_argument("--channels", type=int, default=2, choices=[1, 2],
                   help="downmix audio to N channels: 1=mono, 2=stereo "
                        "(default: 2; multichannel sources fail some encoders)")
    p.add_argument("--merge-format", default="mp4", choices=["mp4", "mkv", "webm"],
                   help="container for merged video (default: mp4)")
    p.add_argument("--subtitles", action="store_true",
                   help="download subtitles (including auto-generated)")
    p.add_argument("--subtitles-langs", nargs="+", default=["en"],
                   help="subtitle languages (default: en)")
    p.add_argument("--thumbnail", action="store_true",
                   help="download and embed the thumbnail")
    p.add_argument("--metadata", action="store_true",
                   help="embed title/artist metadata into the file")
    p.add_argument("--playlist", action="store_true",
                   help="allow downloading an entire playlist")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   choices=["chrome", "firefox", "edge", "brave", "safari",
                            "chromium", "opera", "vivaldi", "whale"],
                   help="use cookies from a local browser (for age/member-only "
                        "content you have access to)")
    p.add_argument("--info", action="store_true",
                   help="list available formats and exit")
    p.add_argument("--no-progress", action="store_true",
                   help="disable the progress display")
    p.add_argument("--no-resume", action="store_true",
                   help="restart partial downloads instead of resuming them")
    p.add_argument("--rate-limit", type=int, metavar="KIB",
                   help="cap download speed in KiB/s (e.g. 500)")
    p.add_argument("--segments", type=int, default=1, metavar="N",
                   help="split plain HTTP files across N connections using "
                        "aria2c, for faster downloads (default: 1 = off; "
                        "try 8)")
    p.add_argument("--connections", type=int, default=4, metavar="N",
                   help="parallel fragments for DASH/HLS streams "
                        "(default: 4; try 16)")
    p.add_argument("--direct", action="store_true",
                   help="treat URLs as plain files (PDF, ZIP, DOCX, ...) and "
                        "skip media extraction; downloads much faster")
    p.add_argument("--media", action="store_true",
                   help="force media handling (yt-dlp) even for direct file URLs")
    p.add_argument("--archive", metavar="FILE",
                   help="record completed downloads in FILE and skip them next "
                        "time (useful for large batches)")
    p.add_argument("-v", "--verbose", action="store_true", help="verbose output")
    return p.parse_args(argv)


def default_args(**overrides) -> argparse.Namespace:
    """Namespace of CLI defaults, for programmatic callers (e.g. the web UI).

    Any keyword overrides the corresponding CLI default, so callers can reuse
    build_options() without constructing a Namespace by hand.
    """
    args = parse_args([])
    for key, value in overrides.items():
        if not hasattr(args, key):
            raise AttributeError(f"unknown option: {key}")
        setattr(args, key, value)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    urls = list(args.url)
    if args.batch:
        try:
            urls += read_urls(args.batch)
        except OSError as exc:
            print(f"error: cannot read batch file: {exc}", file=sys.stderr)
            return 2

    if not urls:
        print("error: no URLs given (pass URLs or use -b FILE)", file=sys.stderr)
        return 2

    if args.info:
        if len(urls) > 1:
            print("error: --info accepts a single URL", file=sys.stderr)
            return 2
        return list_formats(urls[0], args.cookies_from_browser)

    try:
        return run_urls(urls, args)
    except yt_dlp.utils.DownloadError as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
