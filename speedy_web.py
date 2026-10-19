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
import shutil
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

AUTHOR = "Ayanlowo Olatunji Ayobami"
WHATSAPP = "2347038193753"
PHONE_DISPLAY = "+234 703 819 3753"
VERSION = "1.2.0"

# Static files served from ./assets. Paths are relative to ASSETS, and each
# extension maps to a MIME type so the service worker and manifest are served
# with the types browsers require.
STATIC_ASSETS = {
    "/favicon.ico": ("web/favicon.ico", "image/x-icon"),
    "/icon-192.png": ("web/icon-192.png", "image/png"),
    "/icon-512.png": ("web/icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("web/apple-touch-icon.png", "image/png"),
    "/manifest.webmanifest": ("web/manifest.webmanifest",
                              "application/manifest+json"),
    "/sw.js": ("web/sw.js", "text/javascript; charset=utf-8"),
    "/social/og-image.png": ("social/og-image.png", "image/png"),
}


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
        job.filename = path.name
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
                job.filename = Path(path).name

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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Speedy — Fast Media &amp; File Downloader</title>
<meta name="description" content="Speedy downloads video, audio, documents and any file you have the right to download. Built by Ayanlowo Olatunji Ayobami.">
<meta name="author" content="Ayanlowo Olatunji Ayobami">
<meta name="theme-color" content="#0b0d12">
<meta name="color-scheme" content="dark">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Speedy">
<meta property="og:title" content="Speedy — Fast Media &amp; File Downloader">
<meta property="og:description" content="Download video, audio, documents and any file you have the right to download.">
<meta property="og:type" content="website">
<meta property="og:image" content="/social/og-image.png">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="/icon-192.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.webmanifest">
<style>
  :root {
    --bg: #0b0d12;
    --bg-soft: #12151c;
    --card: #161a23;
    --card-2: #1b202b;
    --line: #262c39;
    --line-soft: #1f242f;
    --ink: #eef1f6;
    --ink-2: #a7b0c0;
    --ink-3: #6f798c;
    --accent: #4c8dff;
    --accent-2: #8b5cf6;
    --ok: #34d399;
    --warn: #fbbf24;
    --bad: #f87171;
    --radius: 14px;
    --radius-sm: 10px;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px -12px rgba(0,0,0,.6);
    --ring: 0 0 0 3px rgba(76,141,255,.28);
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background:
      radial-gradient(900px 500px at 12% -8%, rgba(76,141,255,.14), transparent 60%),
      radial-gradient(700px 420px at 92% 4%, rgba(139,92,246,.12), transparent 62%),
      var(--bg);
    color: var(--ink);
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    min-height: 100vh;
    padding-bottom: env(safe-area-inset-bottom);
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .wrap { max-width: 940px; margin: 0 auto; padding: 22px 20px 80px; }

  /* ---------- header ---------- */
  header.top {
    position: sticky; top: 0; z-index: 20;
    margin: -22px -20px 22px; padding: 14px 20px;
    background: rgba(11,13,18,.82);
    backdrop-filter: saturate(150%) blur(14px);
    -webkit-backdrop-filter: saturate(150%) blur(14px);
    border-bottom: 1px solid var(--line-soft);
    display: flex; align-items: center; gap: 12px;
  }
  .brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
  .brand img { width: 36px; height: 36px; border-radius: 10px; flex: none;
               box-shadow: var(--shadow); }
  .brand h1 { font-size: 17px; margin: 0; letter-spacing: -.01em; font-weight: 650; }
  .brand .tag { font-size: 11.5px; color: var(--ink-3); margin: 0;
                letter-spacing: .02em; }
  .top-actions { margin-left: auto; display: flex; align-items: center; gap: 8px; }
  .chip { display: inline-flex; align-items: center; gap: 7px; font-size: 12px;
          color: var(--ink-2); background: var(--card); border: 1px solid var(--line);
          padding: 6px 11px; border-radius: 999px; white-space: nowrap; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok);
         box-shadow: 0 0 0 3px rgba(52,211,153,.16); }
  .dot.off { background: var(--bad); box-shadow: 0 0 0 3px rgba(248,113,113,.16); }

  /* ---------- hero ---------- */
  .hero { margin: 6px 0 20px; }
  .hero h2 { font-size: 27px; line-height: 1.22; margin: 0 0 8px;
             letter-spacing: -.022em; font-weight: 700; }
  .hero h2 .grad {
    background: linear-gradient(92deg, var(--accent), var(--accent-2));
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: transparent;
  }
  .hero p { margin: 0; color: var(--ink-2); max-width: 62ch; }

  /* ---------- cards ---------- */
  .card { background: linear-gradient(180deg, var(--card), var(--bg-soft));
          border: 1px solid var(--line); border-radius: var(--radius);
          box-shadow: var(--shadow); }
  form { padding: 20px; display: grid; gap: 16px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  label { display: block; font-size: 11.5px; text-transform: uppercase;
          letter-spacing: .06em; color: var(--ink-3); margin-bottom: 7px;
          font-weight: 600; }
  input, select {
    width: 100%; padding: 12px 13px; background: #0e1117; color: var(--ink);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    font-size: 15px; font-family: inherit; min-height: 46px;
    transition: border-color .15s, box-shadow .15s;
  }
  input::placeholder { color: #5c6678; }
  input:focus, select:focus { outline: none; border-color: var(--accent);
                              box-shadow: var(--ring); }
  select { appearance: none; -webkit-appearance: none;
           background-image: linear-gradient(45deg, transparent 50%, #8b94a6 50%),
                             linear-gradient(135deg, #8b94a6 50%, transparent 50%);
           background-position: calc(100% - 18px) 21px, calc(100% - 13px) 21px;
           background-size: 5px 5px, 5px 5px; background-repeat: no-repeat;
           padding-right: 38px; }
  .hint { font-size: 12px; color: var(--ink-3); margin-top: -8px; }

  button.primary {
    min-height: 50px; padding: 13px 22px; border: 0; border-radius: var(--radius-sm);
    background: linear-gradient(92deg, var(--accent), #6366f1);
    color: #fff; font-size: 15.5px; font-weight: 650; cursor: pointer;
    font-family: inherit; letter-spacing: .01em;
    box-shadow: 0 6px 18px -8px rgba(76,141,255,.9);
    transition: transform .12s, filter .15s, opacity .15s;
  }
  button.primary:hover { filter: brightness(1.07); }
  button.primary:active { transform: translateY(1px); }
  button.primary:disabled { opacity: .55; cursor: default; filter: none; }
  button.ghost {
    min-height: 38px; padding: 8px 14px; border-radius: 999px;
    background: var(--card-2); color: var(--ink-2); border: 1px solid var(--line);
    font: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer;
    transition: color .15s, border-color .15s;
  }
  button.ghost:hover { color: var(--ink); border-color: #38404f; }

  /* ---------- stats ---------- */
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
           margin: 18px 0 26px; }
  .stat { background: var(--card); border: 1px solid var(--line);
          border-radius: var(--radius-sm); padding: 13px 14px; }
  .stat .k { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
             color: var(--ink-3); font-weight: 600; }
  .stat .v { font-size: 19px; font-weight: 680; margin-top: 3px;
             letter-spacing: -.01em; }

  /* ---------- jobs ---------- */
  .sec-head { display: flex; align-items: center; gap: 10px;
              margin: 30px 0 12px; }
  .sec-head h3 { font-size: 13px; text-transform: uppercase;
                 letter-spacing: .07em; color: var(--ink-3); margin: 0;
                 font-weight: 650; }
  .sec-head .count { font-size: 11.5px; color: var(--ink-3);
                     background: var(--card); border: 1px solid var(--line);
                     border-radius: 999px; padding: 2px 9px; }
  .sec-head .spacer { margin-left: auto; }
  .job { background: var(--card); border: 1px solid var(--line);
         border-radius: var(--radius-sm); padding: 14px 15px;
         margin-bottom: 10px; animation: rise .22s ease both; }
  @keyframes rise { from { opacity: 0; transform: translateY(4px); }
                    to { opacity: 1; transform: none; } }
  .job-top { display: flex; align-items: flex-start; gap: 12px; }
  .jt { font-weight: 600; font-size: 14.5px; overflow: hidden;
        text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
  .badge { font-size: 11px; padding: 3px 10px; border-radius: 999px;
           white-space: nowrap; font-weight: 650; text-transform: capitalize;
           flex: none; }
  .queued { background: #2a3040; color: #a8b3cc; }
  .downloading { background: #14304f; color: #8ec2ff; }
  .finished { background: #10352a; color: #6ee7b7; }
  .error { background: #3d1f22; color: #fca5a5; }
  .bar { height: 6px; background: #20252f; border-radius: 999px;
         margin-top: 11px; overflow: hidden; }
  .fill { height: 100%; border-radius: 999px;
          background: linear-gradient(92deg, var(--accent), var(--accent-2));
          transition: width .3s ease; }
  .job.error .fill { background: linear-gradient(92deg, #ef4444, #f87171); }
  .job.finished .fill { background: linear-gradient(92deg, #10b981, #34d399); }
  .meta { font-size: 12px; color: var(--ink-3); margin-top: 9px;
          display: flex; gap: 15px; flex-wrap: wrap; }
  .meta b { color: var(--ink-2); font-weight: 600; }
  .err { color: #fca5a5; font-size: 12.5px; margin-top: 9px;
         word-break: break-word; background: rgba(248,113,113,.07);
         border: 1px solid rgba(248,113,113,.2); border-radius: 8px;
         padding: 8px 10px; }
  .empty { color: var(--ink-3); font-size: 13.5px; text-align: center;
           padding: 30px 16px; border: 1px dashed var(--line);
           border-radius: var(--radius-sm); background: rgba(22,26,35,.5); }

  /* ---------- about ---------- */
  .about { margin-top: 34px; }
  .about .card { padding: 22px; }
  .about h3 { margin: 0 0 4px; font-size: 17px; letter-spacing: -.01em; }
  .about .role { color: var(--ink-3); font-size: 13px; margin: 0 0 16px; }
  .contact { display: flex; flex-wrap: wrap; gap: 10px; }
  .btn-wa, .btn-tel, .btn-mail {
    display: inline-flex; align-items: center; gap: 9px;
    min-height: 46px; padding: 11px 17px; border-radius: var(--radius-sm);
    font-weight: 620; font-size: 14.5px; border: 1px solid transparent;
    transition: transform .12s, filter .15s;
  }
  .btn-wa { background: #25d366; color: #06281a; }
  .btn-wa:hover { filter: brightness(1.06); text-decoration: none; }
  .btn-tel { background: var(--card-2); color: var(--ink);
             border-color: var(--line); }
  .btn-tel:hover { border-color: #3a4354; text-decoration: none; }
  .btn-wa:active, .btn-tel:active { transform: translateY(1px); }
  .about .note { color: var(--ink-3); font-size: 12.5px; margin: 16px 0 0;
                 padding-top: 14px; border-top: 1px solid var(--line-soft); }
  .feat { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
          margin-top: 18px; }
  .feat div { font-size: 13px; color: var(--ink-2); }
  .feat b { display: block; color: var(--ink); font-size: 13.5px;
            margin-bottom: 2px; }

  footer.bottom { margin-top: 40px; padding-top: 20px;
                  border-top: 1px solid var(--line-soft); color: var(--ink-3);
                  font-size: 12.5px; display: flex; flex-wrap: wrap;
                  gap: 6px 16px; align-items: center; }
  footer.bottom .spacer { margin-left: auto; }

  .toast { position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%);
           background: var(--card-2); color: var(--ink); border: 1px solid var(--line);
           padding: 11px 18px; border-radius: 999px; font-size: 13.5px;
           box-shadow: var(--shadow); opacity: 0; pointer-events: none;
           transition: opacity .25s, transform .25s; z-index: 40; }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(-4px); }

  @media (max-width: 720px) {
    .wrap { padding: 18px 15px 60px; }
    header.top { margin: -18px -15px 18px; padding: 12px 15px; }
    .brand .tag { display: none; }
    .hero h2 { font-size: 22px; }
    .grid-2 { grid-template-columns: 1fr; }
    .stats { grid-template-columns: 1fr 1fr; }
    .feat { grid-template-columns: 1fr; }
    form { padding: 16px; }
    .top-actions .chip.conn { display: none; }
  }
</style>
</head>
<body>
<div class="wrap">

  <header class="top">
    <div class="brand">
      <img src="/icon-192.png" alt="Speedy logo" width="36" height="36">
      <div>
        <h1>Speedy</h1>
        <p class="tag">Media &amp; file downloader</p>
      </div>
    </div>
    <div class="top-actions">
      <span class="chip conn" id="conn"><span class="dot" id="dot"></span>
        <span id="connText">Connected</span></span>
      <button class="ghost" id="installBtn" hidden>Install app</button>
    </div>
  </header>

  <section class="hero">
    <h2>Download <span class="grad">anything you have the right to</span><br>at full speed.</h2>
    <p>Video, audio, PDFs, documents, archives and images — pasted as a link.
       Eight parallel connections, automatic resume, and correct filenames.</p>
  </section>

  <form id="f" class="card">
    <div>
      <label for="url">Link</label>
      <input id="url" name="url" type="url" inputmode="url"
             placeholder="https://... (video page, PDF, ZIP, image)"
             required autocomplete="off" autocapitalize="off" spellcheck="false">
    </div>
    <div class="grid-2">
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
        <label for="segments">Speed</label>
        <select id="segments" name="segments">
          <option value="8" selected>Fast — 8 connections (recommended)</option>
          <option value="4">Moderate — 4 connections</option>
          <option value="16">Maximum — 16 connections</option>
          <option value="1">Single connection (gentle on servers)</option>
        </select>
      </div>
    </div>
    <div>
      <label for="output_dir">Save to (on the machine running Speedy)</label>
      <input id="output_dir" name="output_dir" value="downloads" autocomplete="off">
    </div>
    <button class="primary" type="submit" id="go">Download</button>
    <p class="hint">Only download content you own, that is licensed for reuse,
       or that you otherwise have permission to download.</p>
  </form>

  <div class="stats">
    <div class="stat"><div class="k">Active</div><div class="v" id="sActive">0</div></div>
    <div class="stat"><div class="k">Completed</div><div class="v" id="sDone">0</div></div>
    <div class="stat"><div class="k">Downloaded</div><div class="v" id="sBytes">0 B</div></div>
    <div class="stat"><div class="k">Failed</div><div class="v" id="sFail">0</div></div>
  </div>

  <div class="sec-head">
    <h3>Downloads</h3>
    <span class="count" id="jobCount">0</span>
    <span class="spacer"></span>
    <button class="ghost" id="clearBtn">Clear finished</button>
  </div>
  <div id="jobs"><div class="empty">No downloads yet — paste a link above to start.</div></div>

  <section class="about">
    <div class="card">
      <h3>Built by Ayanlowo Olatunji Ayobami</h3>
      <p class="role">Designer &amp; Developer of Speedy</p>
      <div class="contact">
        <a class="btn-wa" id="waLink" rel="noopener"
           href="https://wa.me/2347038193753?text=Hello%20Ayanlowo%2C%20I%20am%20using%20Speedy.">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M17.47 14.38c-.3-.15-1.76-.87-2.03-.97-.27-.1-.47-.15-.67.15-.2.3-.77.97-.95 1.17-.17.2-.35.22-.65.07-.3-.15-1.26-.46-2.4-1.48-.89-.79-1.49-1.77-1.66-2.07-.17-.3-.02-.46.13-.61.13-.13.3-.35.45-.52.15-.17.2-.3.3-.5.1-.2.05-.37-.02-.52-.08-.15-.67-1.62-.92-2.22-.24-.58-.49-.5-.67-.51h-.57c-.2 0-.52.07-.79.37-.27.3-1.04 1.02-1.04 2.48s1.06 2.88 1.21 3.08c.15.2 2.09 3.2 5.07 4.49.71.3 1.26.49 1.69.63.71.22 1.36.19 1.87.12.57-.09 1.76-.72 2.01-1.41.25-.7.25-1.29.17-1.42-.07-.13-.27-.2-.57-.35zM12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2 22l5.28-1.38c1.45.79 3.08 1.21 4.76 1.21 5.46 0 9.91-4.45 9.91-9.91S17.5 2 12.04 2zm0 18.15c-1.53 0-3.03-.41-4.34-1.19l-.31-.18-3.13.82.84-3.05-.2-.32a8.2 8.2 0 01-1.26-4.36c0-4.54 3.7-8.23 8.24-8.23 4.54 0 8.23 3.69 8.23 8.23 0 4.54-3.69 8.28-8.07 8.28z"/></svg>
          Message me on WhatsApp
        </a>
        <a class="btn-tel" id="telLink" href="tel:+2347038193753">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6.62 10.79a15.05 15.05 0 006.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1C10.4 21 3 13.6 3 4.5c0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/></svg>
          Call +234 703 819 3753
        </a>
      </div>
      <div class="feat">
        <div><b>Automatic routing</b>Media pages and direct files, detected for you.</div>
        <div><b>Genuinely fast</b>Segmented transfers, byte-identical output.</div>
        <div><b>Installable</b>Add to your phone or laptop home screen.</div>
      </div>
      <p class="note">Speedy runs on the computer you start it on. Installed apps
         connect to it over your network. For business enquiries, support, or
         custom work, message me on WhatsApp.</p>
    </div>
  </section>

  <footer class="bottom">
    <span>Speedy <b id="ver">1.2.0</b></span>
    <span>Built &amp; designed by Ayanlowo Olatunji Ayobami</span>
    <span class="spacer"></span>
    <span>Only download what you have the right to.</span>
  </footer>
</div>

<div class="toast" id="toast"></div>

<script>
"use strict";
var $ = function (s) { return document.querySelector(s); };

function fmtBytes(n) {
  if (!n) return "0 B";
  var u = ["B","KiB","MiB","GiB","TiB"], i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
}
function fmtTime(s) {
  if (!s || s < 0) return "--:--";
  s = Math.floor(s);
  var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  var mm = String(m).padStart(2, "0"), sss = String(ss).padStart(2, "0");
  return h ? h + ":" + mm + ":" + sss : mm + ":" + sss;
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function toast(msg) {
  var t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(function () { t.classList.remove("show"); }, 2600);
}

var lastJobs = [];

async function submit(e) {
  e.preventDefault();
  var btn = $("#go");
  var fmt = $("#format").value;
  btn.disabled = true;
  try {
    var r = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: $("#url").value.trim(),
        // "File" skips media extraction and downloads the bytes as-is.
        format: fmt === "file" ? "best" : fmt,
        mode: fmt === "file" ? "direct" : "auto",
        output_dir: $("#output_dir").value.trim() || "downloads",
        segments: parseInt($("#segments").value, 10) || 8
      })
    });
    if (!r.ok) {
      var body = await r.json().catch(function () { return {}; });
      toast("Error: " + (body.error || r.status));
    } else {
      $("#url").value = "";
      $("#url").focus();
      toast("Download queued");
      refresh();
    }
  } catch (err) {
    toast("Request failed: " + err.message);
  } finally {
    btn.disabled = false;
  }
}

function render(jobs) {
  lastJobs = jobs;
  var box = $("#jobs");
  var active = 0, done = 0, fail = 0, bytes = 0;

  jobs.forEach(function (j) {
    bytes += j.downloaded || 0;
    if (j.status === "downloading" || j.status === "queued") active++;
    else if (j.status === "finished") done++;
    else if (j.status === "error") fail++;
  });
  $("#sActive").textContent = active;
  $("#sDone").textContent = done;
  $("#sFail").textContent = fail;
  $("#sBytes").textContent = fmtBytes(bytes);
  $("#jobCount").textContent = jobs.length;

  if (!jobs.length) {
    box.innerHTML = '<div class="empty">No downloads yet — paste a link above to start.</div>';
    return;
  }

  box.innerHTML = jobs.slice().reverse().map(function (j) {
    var extra = "";
    if (j.status === "downloading" && j.speed) {
      extra += "<span><b>" + fmtBytes(j.speed) + "/s</b></span>";
    }
    if (j.status === "downloading" && j.eta) {
      extra += "<span>ETA <b>" + fmtTime(j.eta) + "</b></span>";
    }
    if (j.filename) {
      extra += "<span>saved as <b>" + esc(j.filename) + "</b></span>";
    }
    return '<div class="job ' + esc(j.status) + '">' +
      '<div class="job-top">' +
        '<div class="jt" title="' + esc(j.url) + '">' + esc(j.title) + "</div>" +
        '<div class="badge ' + esc(j.status) + '">' + esc(j.status) + "</div>" +
      "</div>" +
      '<div class="bar"><div class="fill" style="width:' +
        Math.max(0, Math.min(100, j.percent)) + '%"></div></div>' +
      '<div class="meta">' +
        "<span><b>" + j.percent.toFixed(1) + "%</b></span>" +
        "<span>" + fmtBytes(j.downloaded) + " / " + fmtBytes(j.total) + "</span>" +
        extra +
      "</div>" +
      (j.error ? '<div class="err">' + esc(j.error) + "</div>" : "") +
    "</div>";
  }).join("");
}

function setConn(ok) {
  $("#dot").className = "dot" + (ok ? "" : " off");
  $("#connText").textContent = ok ? "Connected" : "Reconnecting";
}

async function refresh() {
  try {
    var r = await fetch("/api/jobs", { cache: "no-store" });
    render(await r.json());
    setConn(true);
  } catch (e) {
    setConn(false);
  }
}

$("#f").addEventListener("submit", submit);
$("#clearBtn").addEventListener("click", function () {
  var finished = lastJobs.filter(function (j) {
    return j.status === "finished" || j.status === "error";
  }).length;
  toast(finished ? "Finished downloads stay in the list" : "Nothing finished yet");
});

// ---- contact links ----
// hrefs are hardcoded in the HTML so they work even if this script fails.
// Keep the prefilled greeting in sync with the markup above.
$("#waLink").href = "https://wa.me/2347038193753" +
  "?text=" + encodeURIComponent("Hello Ayanlowo, I am using Speedy.");

// ---- install prompt (PWA) ----
var deferred = null;
window.addEventListener("beforeinstallprompt", function (e) {
  e.preventDefault();
  deferred = e;
  $("#installBtn").hidden = false;
});
$("#installBtn").addEventListener("click", async function () {
  if (!deferred) return;
  deferred.prompt();
  await deferred.userChoice;
  deferred = null;
  $("#installBtn").hidden = true;
});
window.addEventListener("appinstalled", function () {
  $("#installBtn").hidden = true;
  toast("Speedy installed");
});

// ---- service worker (offline shell) ----
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  });
}

// ---- version ----
fetch("/api/health", { cache: "no-store" })
  .then(function (r) { return r.json(); })
  .then(function (h) { if (h.version) $("#ver").textContent = h.version; })
  .catch(function () {});

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    manager: JobManager
    _head_only = False  # set by do_HEAD so _send omits the body

    def log_message(self, fmt: str, *args) -> None:
        pass  # keep the console clean; jobs are visible in the UI

    def _send(self, code: int, body: bytes, ctype: str,
              cache: str = "no-store", cors: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if cors:
            # Only /api/health sets this. Installed clients (Electron, Android)
            # load from a different origin and must be able to probe the
            # server. Read-only, non-sensitive, and deliberately not extended
            # to /api/download, which would let any web page queue downloads.
            self.send_header("Access-Control-Allow-Origin", "*")
        self._security_headers()
        self.end_headers()
        if not self._head_only:
            self.wfile.write(body)

    def _security_headers(self) -> None:
        # The UI is entirely self-contained: no remote scripts, styles, images
        # or frames, so everything can be locked to 'self'.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy",
                         "geolocation=(), microphone=(), camera=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' "
            "'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'")

    def _json(self, code: int, payload, cors: bool = False) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json",
                   cors=cors)

    def do_OPTIONS(self) -> None:
        """Preflight for the read-only health probe used by installed clients."""
        if self.path.split("?", 1)[0] != "/api/health":
            self._json(404, {"error": "not found"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def do_HEAD(self) -> None:
        """Answer HEAD like GET but without a body.

        Link previewers, uptime checks and browsers probing installability all
        use HEAD; the default 501 makes the app look broken to them.
        """
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/jobs":
            self._json(200, self.manager.all())
        elif path == "/api/health":
            self._json(200, {
                "status": "ok",
                "app": "Speedy",
                "version": VERSION,
                "author": AUTHOR,
                "whatsapp": WHATSAPP,
                "phone": PHONE_DISPLAY,
                "aria2c": bool(shutil.which("aria2c")),
                "ffmpeg": bool(shutil.which("ffmpeg")),
                "jobs": len(self.manager.all()),
            }, cors=True)
        elif path in STATIC_ASSETS:
            self._send_asset(path)
        else:
            self._json(404, {"error": "not found"})

    def _send_asset(self, path: str) -> None:
        """Serve the generated brand assets from ./assets."""
        name, ctype = STATIC_ASSETS[path]
        f = ASSETS / name
        if not f.is_file():
            self._json(404, {"error": "asset missing"})
            return
        # The service worker must never be served stale, or updates stall.
        cache = ("no-cache" if name.endswith("sw.js")
                 else "public, max-age=86400")
        self._send(200, f.read_bytes(), ctype, cache=cache)

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
