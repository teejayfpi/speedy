#!/usr/bin/env python3
"""Speedy web UI.

Serves a small single-page app on localhost so downloads can be queued from a
browser (including a phone on the same network). Uses only the standard library
plus yt-dlp; no web framework required.

    python speedy_web.py                 # http://127.0.0.1:8000
    python speedy_web.py --port 9000 --host 0.0.0.0

Only download content you are allowed to download. Binding to 0.0.0.0 exposes
the server to your network and, because it has no authentication, to anyone who
can reach the port; the default 127.0.0.1 binding keeps it local.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yt_dlp

import fetch
import speedy

MAX_JOBS = 200  # cap history so memory does not grow without bound
DEFAULT_SEGMENTS = 8   # connections for plain HTTP files (measured sweet spot)
MAX_SEGMENTS = 16      # cap so a bad request cannot flood the server
ASSETS = Path(__file__).parent / "assets"


class Job:
    """A single download request and its live state.

    Carries the raw request rather than prebuilt yt-dlp options, so the worker
    thread can decide between the media and direct-file paths without blocking
    the HTTP handler on a network probe.
    """

    def __init__(self, url: str, *, mode: str, output_dir: str,
                 fmt: str, segments: int) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.mode = mode  # auto|media|direct
        self.output_dir = output_dir
        self.fmt = fmt
        self.segments = segments
        self.status = "queued"  # queued|downloading|finished|error
        self.title = url
        self.percent = 0.0
        self.speed = None
        self.eta = None
        self.downloaded = 0
        self.total = None
        self.filename = None
        self.error = None
        self.created = time.time()
        self._finished_seen: set[str] = set()

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "title": self.title,
            "percent": round(self.percent, 1),
            "speed": self.speed,
            "eta": self.eta,
            "downloaded": self.downloaded,
            "total": self.total,
            "filename": self.filename,
            "error": self.error,
        }


class JobManager:
    """Runs downloads on background threads and tracks their progress."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def create(self, url: str, **kw) -> Job:
        job = Job(url, **kw)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > MAX_JOBS:
                stale = self._order.pop(0)
                self._jobs.pop(stale, None)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def all(self) -> list[dict]:
        with self._lock:
            return [self._jobs[jid].snapshot() for jid in self._order
                    if jid in self._jobs]

    def _progress(self, job: Job, d: dict) -> None:
        info = d.get("info_dict") or {}
        if info.get("title"):
            job.title = info["title"]
        status = d.get("status")
        if status == "downloading":
            job.status = "downloading"
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            job.downloaded = done
            job.total = total
            job.percent = (done / total * 100) if total else 0.0
            job.speed = d.get("speed")
            job.eta = d.get("eta")
        elif status == "finished":
            key = info.get("id", job.id)
            if key not in job._finished_seen:
                job._finished_seen.add(key)
                job.percent = 100.0

    def _run_direct(self, job: Job) -> None:
        def on_progress(done, total, speed, eta):
            job.status = "downloading"
            job.downloaded = done
            job.total = total
            job.percent = (done / total * 100) if total else 0.0
            job.speed = speed
            job.eta = eta

        path = fetch.download(job.url, job.output_dir, segments=job.segments,
                              progress=on_progress)
        job.title = path.name
        job.filename = str(path)
        job.status = "finished"
        job.percent = 100.0

    def _run_media(self, job: Job) -> None:
        common = {"output_dir": job.output_dir, "segments": job.segments,
                  "connections": job.segments}
        if job.fmt == "audio":
            args = speedy.default_args(audio_only=True, **common)
        else:
            args = speedy.default_args(format=job.fmt, **common)
        args.no_progress = True  # progress is reported through the job object

        def postproc(d: dict) -> None:
            if d.get("status") != "finished":
                return
            info = d.get("info_dict") or {}
            path = info.get("filepath") or d.get("filepath")
            if path:
                job.filename = path

        opts = speedy.build_options(args)
        opts["progress_hooks"] = [lambda d: self._progress(job, d)]
        opts["postprocessor_hooks"] = [postproc]

        Path(opts["outtmpl"]).parent.mkdir(parents=True, exist_ok=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            rc = ydl.download([job.url])
        if rc != 0:
            raise RuntimeError(f"yt-dlp exit code {rc}")
        job.status = "finished"
        job.percent = 100.0

    def _run(self, job: Job) -> None:
        try:
            mode = job.mode
            if mode == "auto":
                mode = "direct" if speedy.is_direct_file(job.url) else "media"
            if mode == "direct":
                self._run_direct(job)
            else:
                self._run_media(job)
        except yt_dlp.utils.DownloadError as exc:
            job.status = "error"
            job.error = str(exc)
        except (urllib.error.URLError, ValueError, RuntimeError, OSError) as exc:
            job.status = "error"
            job.error = str(exc)
        except Exception as exc:  # keep one bad job from killing the thread
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Speedy</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="/icon-192.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#3a78f0">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: #14161a; color: #e6e8eb;
         font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }
  .wrap { max-width: 820px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #8b93a1; font-size: 13px; margin-bottom: 20px; }
  form { display: grid; gap: 12px; background: #1c1f26; padding: 18px;
         border-radius: 12px; border: 1px solid #272b34; }
  label { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
          color: #8b93a1; }
  input, select { width: 100%; padding: 10px 12px; background: #14161a;
                  border: 1px solid #2e333d; border-radius: 8px; color: #e6e8eb;
                  font-size: 15px; }
  input:focus, select:focus { outline: 2px solid #4c8dff; border-color: #4c8dff; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  button { padding: 11px 18px; background: #4c8dff; color: #fff; border: 0;
           border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; }
  button:hover { background: #3d7ae8; }
  button:disabled { opacity: .5; cursor: default; }
  h2 { font-size: 14px; color: #8b93a1; text-transform: uppercase;
       letter-spacing: .04em; margin: 28px 0 10px; }
  .job { background: #1c1f26; border: 1px solid #272b34; border-radius: 10px;
         padding: 14px; margin-bottom: 10px; }
  .job-top { display: flex; justify-content: space-between; gap: 12px; }
  .jt { font-weight: 600; overflow: hidden; text-overflow: ellipsis;
        white-space: nowrap; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 20px;
           white-space: nowrap; height: fit-content; }
  .queued { background: #33384a; color: #a8b3cc; }
  .downloading { background: #1e3a5f; color: #8ec2ff; }
  .finished { background: #1d3d2a; color: #7ee0a2; }
  .error { background: #4a2226; color: #ff9aa6; }
  .bar { height: 6px; background: #272b34; border-radius: 4px; margin-top: 10px;
         overflow: hidden; }
  .fill { height: 100%; background: #4c8dff; transition: width .25s; }
  .meta { font-size: 12px; color: #8b93a1; margin-top: 8px;
          display: flex; gap: 14px; flex-wrap: wrap; }
  .err { color: #ff9aa6; font-size: 12px; margin-top: 8px; word-break: break-word; }
  .empty { color: #6b7280; font-size: 13px; }
</style>
</head>
<body>
<div class="wrap">
  <h1><img src="/icon-192.png" alt="" width="26" height="26"
           style="vertical-align:-5px;margin-right:8px;border-radius:6px">Speedy</h1>
  <div class="sub">Download media, documents, and any file you own or are licensed to use.</div>

  <form id="f">
    <div>
      <label for="url">URL</label>
      <input id="url" name="url" placeholder="https://... (video, PDF, ZIP, image)"
             required autocomplete="off">
    </div>
    <div class="row">
      <div>
        <label for="format">Format</label>
        <select id="format" name="format">
          <option value="best">Best video</option>
          <option value="1080p">1080p</option>
          <option value="720p">720p</option>
          <option value="480p">480p</option>
          <option value="360p">360p</option>
          <option value="audio">Audio only (mp3)</option>
          <option value="file">File / document (as-is)</option>
        </select>
      </div>
      <div>
        <label for="output_dir">Save to</label>
        <input id="output_dir" name="output_dir" value="downloads">
      </div>
    </div>
    <div>
      <label for="segments">Speed (parallel connections)</label>
      <select id="segments" name="segments">
        <option value="8" selected>Fast — 8 connections (recommended)</option>
        <option value="4">Moderate — 4 connections</option>
        <option value="16">Maximum — 16 connections</option>
        <option value="1">Single connection (gentle on servers)</option>
      </select>
    </div>
    <button type="submit" id="go">Download</button>
  </form>

  <h2>Jobs</h2>
  <div id="jobs"><div class="empty">No jobs yet.</div></div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const fmtBytes = (n) => {
  if (!n) return "?";
  const u = ["B","KiB","MiB","GiB","TiB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + u[i];
};
const fmtTime = (s) => {
  if (!s || s < 0) return "--:--";
  s = Math.floor(s); const h = Math.floor(s/3600), m = Math.floor(s%3600/60);
  const ss = s%60; return h ? `${h}:${String(m).padStart(2,"0")}:${String(ss).padStart(2,"0")}`
                            : `${String(m).padStart(2,"0")}:${String(ss).padStart(2,"0")}`;
};

async function submit(e) {
  e.preventDefault();
  const btn = $("#go"); btn.disabled = true;
  const fmt = $("#format").value;
  try {
    const r = await fetch("/api/download", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        url: $("#url").value.trim(),
        // "File" skips media extraction and downloads the bytes as-is.
        format: fmt === "file" ? "best" : fmt,
        mode: fmt === "file" ? "direct" : "auto",
        output_dir: $("#output_dir").value.trim() || "downloads",
        segments: parseInt($("#segments").value, 10) || 8,
      }),
    });
    if (!r.ok) { const t = await r.text(); alert("Error: " + t); }
    else { $("#url").value = ""; refresh(); }
  } catch (err) { alert("Request failed: " + err); }
  finally { btn.disabled = false; }
}

function render(jobs) {
  const box = $("#jobs");
  if (!jobs.length) { box.innerHTML = '<div class="empty">No jobs yet.</div>'; return; }
  box.innerHTML = jobs.slice().reverse().map(j => `
    <div class="job">
      <div class="job-top">
        <div class="jt" title="${j.url}">${j.title.replace(/</g,"&lt;")}</div>
        <div class="badge ${j.status}">${j.status}</div>
      </div>
      <div class="bar"><div class="fill" style="width:${j.percent}%"></div></div>
      <div class="meta">
        <span>${j.percent.toFixed(1)}%</span>
        <span>${fmtBytes(j.downloaded)} / ${fmtBytes(j.total)}</span>
        ${j.speed ? `<span>${fmtBytes(j.speed)}/s</span>` : ""}
        ${j.status === "downloading" ? `<span>ETA ${fmtTime(j.eta)}</span>` : ""}
      </div>
      ${j.error ? `<div class="err">${j.error.replace(/</g,"&lt;")}</div>` : ""}
    </div>`).join("");
}

async function refresh() {
  try {
    const r = await fetch("/api/jobs");
    render(await r.json());
  } catch (e) { /* server restarting; retry on next tick */ }
}

$("#f").addEventListener("submit", submit);
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    manager: JobManager

    def log_message(self, fmt: str, *args) -> None:
        pass  # keep the console clean; jobs are visible in the UI

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/jobs":
            self._json(200, self.manager.all())
        elif path in ("/favicon.ico", "/icon-192.png", "/icon-512.png",
                      "/apple-touch-icon.png", "/manifest.webmanifest"):
            self._send_asset(path)
        else:
            self._json(404, {"error": "not found"})

    def _send_asset(self, path: str) -> None:
        """Serve the generated brand assets from ./assets/web."""
        name = {"/favicon.ico": "favicon.ico",
                "/icon-192.png": "icon-192.png",
                "/icon-512.png": "icon-512.png",
                "/apple-touch-icon.png": "apple-touch-icon.png",
                "/manifest.webmanifest": "manifest.webmanifest"}[path]
        f = ASSETS / "web" / name
        if not f.is_file():
            self._json(404, {"error": "asset missing"})
            return
        ctype = ("image/x-icon" if name.endswith(".ico")
                 else "application/manifest+json" if name.endswith("webmanifest")
                 else "image/png")
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/download":
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return
        if length > 64 * 1024:
            self._json(413, {"error": "request too large"})
            return

        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return

        url = (payload.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            self._json(400, {"error": "url must start with http:// or https://"})
            return

        fmt = payload.get("format") or "best"
        output_dir = (payload.get("output_dir") or "downloads").strip() or "downloads"
        mode = payload.get("mode") or "auto"
        if mode not in ("auto", "media", "direct"):
            self._json(400, {"error": f"unknown mode: {mode}"})
            return
        if fmt != "audio" and fmt not in speedy.VIDEO_FORMATS:
            self._json(400, {"error": f"unknown format: {fmt}"})
            return

        # Segmented downloading is on by default in the UI; clamp to a sane
        # range so a bad request cannot spawn hundreds of connections.
        try:
            segments = int(payload.get("segments") or DEFAULT_SEGMENTS)
        except (TypeError, ValueError):
            segments = DEFAULT_SEGMENTS
        segments = max(1, min(segments, MAX_SEGMENTS))

        job = self.manager.create(url, mode=mode, output_dir=output_dir,
                                  fmt=fmt, segments=segments)
        self._json(202, job.snapshot())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Speedy web UI.")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1, local only)")
    p.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    Handler.manager = JobManager()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"Speedy web UI on http://{shown}:{args.port}  (Ctrl+C to stop)")
    if args.host in ("0.0.0.0", "::"):
        print("warning: reachable from your network and has no authentication")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
