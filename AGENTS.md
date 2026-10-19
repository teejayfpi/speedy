# Speedy — agent notes

Fast downloader for media (yt-dlp sites) and plain files (PDF, ZIP, DOCX, ...).

## Layout

| File | Role |
| --- | --- |
| `speedy.py` | CLI. URL routing, yt-dlp options, progress display. |
| `fetch.py` | Native HTTP/aria2c downloader for direct files. Stdlib only. |
| `speedy_web.py` | Local web UI on stdlib `http.server`. No framework. |
| `make_assets.py` | Regenerates `assets/` (icons, logo, OG card, manifest) and `client/build/` icons. Single source of truth for the manifest. |
| `assets/` | Generated — regenerate with `python make_assets.py`. |
| `client/` | Electron desktop client. `www/index.html` is the connect screen. |
| `client/android/` | Capacitor Android client. `setup_android.py` patches the generated project. |
| `.github/workflows/` | `ci.yml`, `android.yml`, `desktop.yml`. |

## Native clients

Both clients are thin wrappers: they connect to a Speedy server over the LAN and
the *server* does the downloading. Neither downloads anything itself.

- `/api/health` is the only endpoint that sends `Access-Control-Allow-Origin`,
  so an installed client on a different origin (`file://`, `localhost`) can
  probe it. **Do not add CORS to `/api/download`** — that would let any web page
  queue downloads on a server with no authentication. `do_OPTIONS` answers
  preflight for `/api/health` only.
- Capacitor generates `client/android/android/` at build time, so it is
  gitignored. `setup_android.py` runs afterwards and (a) adds
  `android:usesCleartextTraffic="true"` — a LAN server is plain `http://` and
  Android blocks cleartext by default — and (b) installs the launcher icons.
  It is idempotent.
- The client navigates top-level to the server rather than using an iframe:
  the server sends `frame-ancestors 'none'`, which blocks framing.
- Brand contact details live in `speedy_web.py` as `AUTHOR`, `WHATSAPP`,
  `PHONE_DISPLAY`. Do not invent an email address for the author; none was
  given.

## CI gotchas

- Java **17**, not 21: the generated project uses Gradle 8.2.1 / AGP 8.2.1,
  which cannot run on Java 21.
- Do **not** use `android-actions/setup-android@v3`. It tries to install the
  legacy `tools` package, which no longer exists, and the step fails. The
  runner image already has a complete SDK — use it and install only
  `platforms;android-34`.
- `ci.yml` also runs the suite with `aria2c` removed, to prove the
  single-connection fallback works. The suite skips (not fails) without it.
- The asset job validates presence and PNG magic bytes. It deliberately does
  **not** regenerate and diff: PNG bytes vary with font rendering, so that
  check fails for reasons unrelated to correctness.


## Why there are two download paths

yt-dlp's generic extractor names unknown files `*.unknown_video`, so a PDF
arrives as `report.unknown_video`. Direct files therefore go through `fetch.py`.

Routing is by content type: a one-byte ranged request (`fetch.probe`); HTML
means a media page (yt-dlp), anything else is a direct file. `--direct` and
`--media` override, and the web UI exposes the same choice as the "File /
document (as-is)" format option.

## Gotchas

- `--segments N` (aria2c) only helps plain files; DASH/HLS uses
  `--connections N`. Segmented output is byte-identical — verify with md5sum
  against a single-connection download when touching `fetch.py`.
- `--archive` is implemented separately for each path (`load_archive` /
  `record_archive` in `speedy.py`, plus yt-dlp's own `download_archive`).
  Add to both when changing it.
- `Job` in `speedy_web.py` stores raw request params, not yt-dlp options, so
  the network probe runs on the worker thread and never blocks a request.
- `assets/icon-192.png` etc. are served by `speedy_web.py`; the page head
  references `/favicon.ico`, `/icon-192.png`, `/manifest.webmanifest`.
- Concurrent downloads must not share a temp file. `fetch._claim_target`
  takes an exclusive `flock` on `<dest>.part` and falls back to the next free
  name. Do not reintroduce a fixed `.part` path: whichever download finishes
  first renames it away and the others die with `FileNotFoundError`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Hermetic: a local `http.server` on an ephemeral port, no internet, no mocks
of the code under test. Covers naming (incl. traversal and UTF-8
`Content-Disposition`), resume, concurrency, segmentation, and the web API.

Two notes when editing tests:
- Segmented tests use `/big.bin` (8 MiB) because aria2c is skipped for files
  under `ARIA2_MIN_BYTES`; a smaller fixture makes them pass vacuously.
  `test_segmented_matches_single` asserts `_aria2` was called for this reason.
- `TestWebApi` needs `speedy_web.Handler.manager` set before the server starts.

## Conventions

- Python 3.10+ type hints, `from __future__ import annotations`.
- Comments explain *why*, not *what*. No change-history narration.
- `make_assets.py` measures artwork geometry from rendered pixels
  (`art_bbox`) rather than hard-coded coordinates — keep it that way.

## Verify changes

```bash
python3 -m py_compile speedy.py speedy_web.py fetch.py make_assets.py
python3 speedy.py --version
python3 speedy.py <pdf-url> -o /tmp/t --no-progress   # direct path
python3 speedy.py <media-page-url> -o /tmp/t -a --no-progress   # media path
```
