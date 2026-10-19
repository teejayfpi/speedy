"""Direct HTTP(S) file downloading for Speedy.

yt-dlp only understands media sites, and its generic extractor names unknown
files `*.unknown_video` -- a downloaded PDF would arrive as
`report.unknown_video`. So documents and arbitrary files (PDF, DOCX, XLSX,
ZIP, images, plain media URLs, ...) are fetched here instead.

Small files stream through urllib with resume support. Larger files can be
handed to aria2c for segmented multi-connection transfers.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import fcntl
except ImportError:  # Windows has no flock; see _claim_target
    fcntl = None  # type: ignore[assignment]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

CHUNK = 256 * 1024
# Below this, the overhead of spawning aria2c outweighs the parallel gain.
ARIA2_MIN_BYTES = 4 * 1024 * 1024

# Content-Type -> extension, used only when the URL and server provide no name.
EXT_BY_TYPE = {
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
    "application/x-7z-compressed": ".7z",
    "application/x-rar-compressed": ".rar",
    "application/vnd.rar": ".rar",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/epub+zip": ".epub",
    "application/rtf": ".rtf",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/octet-stream": "",  # no useful hint
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/html": ".html",
    "text/xml": ".xml",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpeg",
}

WEBPAGE_TYPES = {"text/html", "application/xhtml+xml"}


def human_bytes(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def _to_bytes(value: str, unit: str) -> float:
    mult = {
        "B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3,
        "TIB": 1024 ** 4, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3,
        "TB": 1000 ** 4,
    }
    try:
        return float(value) * mult.get(unit.upper(), 1)
    except ValueError:
        return 0.0


def _parse_eta(text: str) -> int | None:
    if not text:
        return None
    total = 0
    for value, unit in re.findall(r"(\d+)([hms])", text):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total or None


def sanitize_name(name: str) -> str:
    """Strip anything that could escape the output directory."""
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    return name or "download"


def _from_disposition(value: str) -> str | None:
    if not value:
        return None
    # RFC 5987 form: filename*=UTF-8''na%C3%AFve.pdf
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.I)
    if m:
        return unquote(m.group(2).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if m:
        return m.group(1).strip()
    return None


def probe(url: str, timeout: float = 30) -> dict:
    """Request headers only, using a one-byte range request.

    Returns final URL, headers, total size and whether ranges are supported.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Range": "bytes=0-0",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        headers = {k.lower(): v for k, v in r.headers.items()}
        status = r.status

    size = None
    if status == 206:
        m = re.search(r"/(\d+)\s*$", headers.get("content-range", ""))
        if m:
            size = int(m.group(1))
    if size is None:
        cl = headers.get("content-length", "")
        if cl.isdigit():
            size = int(cl)

    return {
        "url": r.geturl(),
        "status": status,
        "headers": headers,
        "size": size,
        "ranges": status == 206 or "bytes" in headers.get("accept-ranges", "").lower(),
        "content_type": headers.get("content-type", "").split(";")[0].strip().lower(),
    }


def resolve_name(info: dict) -> str:
    """Pick a filename from Content-Disposition, else the URL path, else type."""
    name = _from_disposition(info["headers"].get("content-disposition", ""))
    if not name:
        name = unquote(Path(urlparse(info["url"]).path).name)
    if not name or name in ("", "/"):
        name = "download"
    if not Path(name).suffix:
        ext = EXT_BY_TYPE.get(info["content_type"], "")
        if ext:
            name += ext
    return sanitize_name(name)


def looks_like_webpage(info: dict) -> bool:
    """True when the URL serves a web page rather than a file."""
    return info["content_type"] in WEBPAGE_TYPES


def _name_candidates(base: Path):
    """Yield base, then 'base (1)', 'base (2)', ... for collision fallback."""
    yield base
    for i in range(1, 1000):
        yield base.with_name(f"{base.stem} ({i}){base.suffix}")
    raise RuntimeError(f"cannot find a free name for {base.name}")


def _claim_target(base: Path, resume: bool):
    """Pick a (dest, part, lockfd) triple no other download is writing to.

    Downloads of the same URL must not share a temp file: whichever finishes
    first renames it away, and the others then fail on a missing path. So each
    download takes an exclusive lock on its own `<dest>.part`; if that path is
    already locked, the next candidate name is used instead.

    A finished file is never overwritten -- the candidate is skipped -- while an
    unlocked leftover part file is reused, which is what makes resume work.
    """
    for cand in _name_candidates(base):
        if cand.exists():
            continue  # already downloaded; never clobber it
        part = cand.with_name(cand.name + ".part")
        if fcntl is None:  # no locking available (e.g. Windows)
            return cand, part, None
        fd = os.open(part, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)  # another live download owns this name
            continue
        return cand, part, fd
    raise RuntimeError(f"cannot find a free name for {base.name}")


def _release(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _stream(url: str, dest: Path, part: Path, lockfd: int | None, *,
            resume: bool = True, progress=None, timeout: float = 60) -> Path:
    """Single-connection download with resume, via urllib."""
    try:
        have = part.stat().st_size if (resume and part.exists()) else 0

        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if have:
            headers["Range"] = f"bytes={have}-"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            total = None
            if r.status == 206:
                m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
                if m:
                    total = int(m.group(1))
            else:
                have = 0  # server ignored the range; start over
            if total is None:
                cl = r.headers.get("Content-Length", "")
                if cl.isdigit():
                    total = int(cl) + have

            done = have
            started = time.time()
            mode = "ab" if have else "wb"
            with open(part, mode) as fh:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        elapsed = max(time.time() - started, 1e-6)
                        speed = (done - have) / elapsed
                        eta = (int((total - done) / speed)
                               if total and speed else None)
                        progress(done, total, speed, eta)

        part.replace(dest)
        return dest
    finally:
        _release(lockfd)


_ARIA2_RE = re.compile(
    r"\[#[0-9a-f]+\s+([\d.]+)([KMGTP]?i?B)/([\d.]+)([KMGTP]?i?B)"
    r"\((\d+)%\)(?:[^\]]*?DL:([\d.]+)([KMGTP]?i?B))?"
    r"(?:[^\]]*?ETA:([0-9hms]+))?"
)


def _aria2(url: str, dest: Path, part: Path, lockfd: int | None, segments: int,
           *, progress=None) -> Path:
    """Segmented multi-connection download via aria2c."""
    cmd = [
        "aria2c",
        "-x", str(segments), "-s", str(segments), "-k", "1M",
        "--file-allocation=none", "--no-conf",
        "--summary-interval=1", "--console-log-level=warn",
        "--allow-overwrite=true", "--auto-file-renaming=false",
        "--continue=true", "--remove-control-file=true",
        "--header", f"User-Agent: {USER_AGENT}",
        "--dir", str(dest.parent), "--out", part.name, url,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                m = _ARIA2_RE.search(line)
                if not m or not progress:
                    continue
                done = _to_bytes(m.group(1), m.group(2))
                total = _to_bytes(m.group(3), m.group(4))
                speed = _to_bytes(m.group(6), m.group(7)) if m.group(6) else None
                progress(done, total, speed, _parse_eta(m.group(8)))
        finally:
            proc.wait()

        if proc.returncode != 0 or not part.exists():
            raise RuntimeError(f"aria2c failed (exit {proc.returncode})")
        part.replace(dest)
        return dest
    finally:
        _release(lockfd)


def download(url: str, dest_dir: str | Path, *, segments: int = 1,
             resume: bool = True, progress=None,
             timeout: float = 60) -> Path:
    """Download a direct file URL into dest_dir and return the saved path.

    `progress` is called as progress(downloaded, total, speed, eta).
    """
    out = Path(dest_dir)
    out.mkdir(parents=True, exist_ok=True)

    info = probe(url, timeout=timeout)
    if looks_like_webpage(info):
        raise ValueError("URL serves a web page, not a file")

    base = out / resolve_name(info)
    size = info["size"]

    use_aria2 = (
        segments > 1
        and shutil.which("aria2c")
        and info["ranges"]
        and (size is None or size >= ARIA2_MIN_BYTES)
    )
    if segments > 1 and not use_aria2:
        reason = ("aria2c not installed" if not shutil.which("aria2c")
                  else "server does not support ranged requests"
                  if not info["ranges"] else "file is small")
        print(f"note: single connection ({reason})", file=sys.stderr)

    dest, part, lockfd = _claim_target(base, resume)
    if use_aria2:
        return _aria2(url, dest, part, lockfd, segments, progress=progress)
    return _stream(url, dest, part, lockfd, resume=resume, progress=progress,
                   timeout=timeout)


def _main(argv: list[str]) -> int:
    """Minimal CLI for manual testing: python fetch.py URL [outdir]."""
    import argparse

    p = argparse.ArgumentParser(prog="fetch", description=__doc__)
    p.add_argument("url")
    p.add_argument("outdir", nargs="?", default=".")
    p.add_argument("-s", "--segments", type=int, default=1)
    args = p.parse_args(argv)

    def show(done, total, speed, eta):
        pct = f"{done / total * 100:5.1f}%" if total else "  ?  "
        spd = f"{human_bytes(speed)}/s" if speed else "?"
        print(f"\r  {pct} {human_bytes(done)}/{human_bytes(total)} {spd}",
              end="", file=sys.stderr)

    try:
        path = download(args.url, args.outdir, segments=args.segments,
                        progress=show)
    except (urllib.error.URLError, ValueError, RuntimeError) as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    print(f"\nsaved: {path} ({human_bytes(path.stat().st_size)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
