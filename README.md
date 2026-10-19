# Speedy

A fast downloader for **media** (YouTube and other yt-dlp supported sites) and
**plain files** (PDF, DOCX, XLSX, ZIP, images, and any direct download link).

Each URL is routed automatically: web pages go to yt-dlp for media extraction,
direct file links are fetched by Speedy's own downloader. This matters because
yt-dlp's generic extractor mislabels unknown files — a PDF would be saved as
`report.unknown_video`. Speedy names it `report.pdf`.

Only download content you own, content licensed for reuse, or content you
otherwise have permission to download.

Built and designed by **Ayanlowo Olatunji Ayobami** — WhatsApp [+234 703 819 3753](https://wa.me/2347038193753).

## Install

Speedy runs in three places. In every case the **server does the actual
downloading**; the Android and desktop apps are thin clients that connect to it
over your local network.

### 1. Server (required)

```bash
pip install -r requirements.txt   # yt-dlp
python speedy_web.py              # http://127.0.0.1:8000
```

To let a phone or another computer connect, bind to your LAN:

```bash
python speedy_web.py --host 0.0.0.0 --port 8000
```

There is no password, so only do this on a network you trust.

### 2. Android app

Download the APK from the **Build Android APK** workflow (Actions tab), or
publish it as a release by running that workflow with `release` ticked. Then
enter your server's address, e.g. `http://192.168.1.5:8000`.

### 3. Desktop app (Windows, macOS, Linux)

Installers come from the **Build Desktop Installers** workflow, attached to each
run and to a release when `release` is ticked.

Optional extras for the server:

| Tool | Why | Install |
| --- | --- | --- |
| `ffmpeg` | merge video+audio, audio extraction | `apt install ffmpeg` |
| `aria2` | segmented downloads for plain files | `apt install aria2` |

## Command line

```bash
python speedy.py <url>                     # media page or direct file
python speedy.py <url> -f 720p             # cap video quality
python speedy.py <url> -a                  # audio only (mp3)
python speedy.py <paper.pdf>               # documents work directly
python speedy.py <file-url> --segments 8   # 8 parallel connections
python speedy.py <url> --info              # list available formats
python speedy.py -b urls.txt               # batch from a file
python speedy.py -b urls.txt --archive done.txt   # skip already-downloaded
```

Force a path when detection guesses wrong:

```bash
python speedy.py <url> --direct   # treat as a plain file, skip extraction
python speedy.py <url> --media    # force media handling (yt-dlp)
```

## Web UI

```bash
python speedy_web.py                 # http://127.0.0.1:8000
python speedy_web.py --port 12000
```

Pick a format, paste a URL, and watch live progress. The **File / document**
option downloads the bytes as-is.

## Speed

- `--segments N` splits a plain file across N connections via aria2c.
  Measured on a 317 MB file: 1 connection 11.2s, 8 connections 6.2s (1.8x).
  Output is byte-identical to a single-connection download.
- `--connections N` fetches DASH/HLS fragments in parallel (no extra tool).
  8 is the sweet spot; 16 adds little.

## API

`POST /api/download` with JSON:

```json
{"url": "https://...", "format": "best", "mode": "auto",
 "output_dir": "downloads", "segments": 8}
```

`mode` is `auto`, `media`, or `direct`. `format` is `best`, a height like
`720p`, or `audio`. `GET /api/jobs` returns live job state.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Hermetic — a local HTTP server on an ephemeral port, no internet, no mocks of
the code under test. 51 tests covering naming, resume, concurrent downloads,
segmented transfers, CORS boundaries, and the web API.

## Files

| File | Purpose |
| --- | --- |
| `speedy.py` | CLI: routing, yt-dlp options, progress |
| `fetch.py` | native HTTP/aria2c downloader for plain files |
| `speedy_web.py` | local web UI (stdlib `http.server`, no framework) |
| `make_assets.py` | regenerates the app icon and brand assets |
| `assets/` | generated icons, logo, social card, preview |
| `client/` | Electron desktop client (connect screen + window) |
| `client/android/` | Capacitor Android client and setup script |
| `.github/workflows/` | CI, Android APK, and desktop installer builds |
