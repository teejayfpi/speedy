"""Tests for Speedy.

Run with:  python3 -m unittest discover -s tests -v

The download tests run against a local HTTP server that supports range
requests, so they are hermetic (no internet) and exercise the real code
paths rather than mocks.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fetch  # noqa: E402
import speedy  # noqa: E402
import speedy_web  # noqa: E402

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB, deterministic
BIG = bytes(range(256)) * (8 * 1024 * 1024 // 256)  # 8 MiB, above the
# aria2c threshold so the segmented tests exercise real segmentation.


class RangeHandler(http.server.BaseHTTPRequestHandler):
    """Serves PAYLOAD at /file.bin with range support, plus /page.html."""

    def log_message(self, *args):
        pass  # keep test output quiet

    def _send(self, body, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/page.html":
            self._send(b"<html><body>hi</body></html>", "text/html")
            return
        if path == "/named":
            self._send(PAYLOAD, "application/pdf", extra={
                "Content-Disposition":
                    'attachment; filename="Quarterly Report.pdf"'})
            return
        if path == "/noext":
            self._send(PAYLOAD, "application/pdf")
            return
        if path in ("/file.bin", "/big.bin"):
            body = PAYLOAD if path == "/file.bin" else BIG
            rng = self.headers.get("Range")
            if rng:
                start_s, _, end_s = rng.replace("bytes=", "").partition("-")
                start = int(start_s)
                end = int(end_s) if end_s else len(body) - 1
                end = min(end, len(body) - 1)
                self._send(body[start:end + 1], "application/octet-stream",
                           status=206, extra={
                               "Content-Range":
                                   f"bytes {start}-{end}/{len(body)}",
                               "Accept-Ranges": "bytes"})
                return
            self._send(body, "application/octet-stream",
                       extra={"Accept-Ranges": "bytes"})
            return
        self._send(b"nope", "text/plain", status=404)


class ServerMixin:
    """Start a local HTTP server on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), RangeHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="speedy-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestNaming(unittest.TestCase):
    def test_sanitize_strips_path_traversal(self):
        for bad in ("../../etc/passwd", "a/b/c.pdf", "C:\\win\\x.zip",
                    "/abs/path.txt"):
            out = fetch.sanitize_name(bad)
            self.assertNotIn("/", out)
            self.assertNotIn("\\", out)

    def test_sanitize_removes_control_chars(self):
        self.assertEqual(fetch.sanitize_name("na\x00me.txt"), "na_me.txt")

    def test_sanitize_falls_back_when_empty(self):
        for bad in ("", "...", "   ", "/"):
            self.assertEqual(fetch.sanitize_name(bad), "download")

    def test_disposition_quoted(self):
        self.assertEqual(
            fetch._from_disposition('attachment; filename="a b.pdf"'),
            "a b.pdf")

    def test_disposition_rfc5987_utf8(self):
        self.assertEqual(
            fetch._from_disposition(
                "attachment; filename*=UTF-8''na%C3%AFve.pdf"),
            "naïve.pdf")

    def test_disposition_unquoted_and_missing(self):
        self.assertEqual(fetch._from_disposition("attachment; filename=x.zip"),
                         "x.zip")
        self.assertIsNone(fetch._from_disposition(""))


class TestParsing(unittest.TestCase):
    def test_to_bytes_units(self):
        self.assertEqual(fetch._to_bytes("1", "KiB"), 1024)
        self.assertEqual(fetch._to_bytes("1.5", "MiB"), 1.5 * 1024 ** 2)
        self.assertEqual(fetch._to_bytes("x", "MiB"), 0.0)

    def test_parse_eta(self):
        self.assertEqual(fetch._parse_eta("2m24s"), 144)
        self.assertEqual(fetch._parse_eta("1h2m3s"), 3723)
        self.assertIsNone(fetch._parse_eta(""))

    def test_human_bytes(self):
        self.assertEqual(fetch.human_bytes(None), "?")
        self.assertEqual(fetch.human_bytes(512), "512 B")
        self.assertIn("KiB", fetch.human_bytes(2048))


class TestDirectDownload(ServerMixin, unittest.TestCase):
    def test_downloads_correct_bytes(self):
        p = fetch.download(f"{self.base}/file.bin", self.tmp)
        self.assertEqual(hashlib.md5(p.read_bytes()).hexdigest(),
                         hashlib.md5(PAYLOAD).hexdigest())
        self.assertEqual(p.name, "file.bin")

    def test_name_from_content_disposition(self):
        p = fetch.download(f"{self.base}/named", self.tmp)
        self.assertEqual(p.name, "Quarterly Report.pdf")

    def test_extension_derived_from_content_type(self):
        p = fetch.download(f"{self.base}/noext", self.tmp)
        self.assertEqual(p.name, "noext.pdf")

    def test_webpage_is_refused(self):
        with self.assertRaises(ValueError):
            fetch.download(f"{self.base}/page.html", self.tmp)

    def test_existing_file_is_not_clobbered(self):
        first = fetch.download(f"{self.base}/file.bin", self.tmp)
        second = fetch.download(f"{self.base}/file.bin", self.tmp)
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, "file (1).bin")
        self.assertTrue(first.exists())

    def test_no_leftover_part_files(self):
        fetch.download(f"{self.base}/file.bin", self.tmp)
        self.assertEqual(list(self.tmp.glob("*.part")), [])

    def test_resume_completes_truncated_file(self):
        p = fetch.download(f"{self.base}/file.bin", self.tmp)
        part = p.with_name(p.name + ".part")
        p.rename(part)
        part.write_bytes(PAYLOAD[:200_000])  # simulate interruption
        resumed = fetch.download(f"{self.base}/file.bin", self.tmp)
        self.assertEqual(resumed.read_bytes(), PAYLOAD)

    def test_progress_callback_reports_totals(self):
        seen = []
        fetch.download(f"{self.base}/file.bin", self.tmp,
                       progress=lambda d, t, s, e: seen.append((d, t)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][1], len(PAYLOAD))

    def test_concurrent_same_url_all_succeed(self):
        """Regression: concurrent downloads shared one .part and crashed."""
        errors = []
        paths = []

        def go():
            try:
                paths.append(fetch.download(f"{self.base}/file.bin", self.tmp))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(paths), 4)
        self.assertEqual(len({str(p) for p in paths}), 4)  # distinct names
        for p in paths:
            self.assertEqual(p.read_bytes(), PAYLOAD)

    @unittest.skipUnless(shutil.which("aria2c"), "aria2c not installed")
    def test_segmented_matches_single(self):
        single = fetch.download(f"{self.base}/big.bin", self.tmp, segments=1)
        # Assert segmentation really happened: a vacuous single-connection
        # "pass" is what this test exists to prevent.
        with unittest.mock.patch.object(fetch, "_aria2",
                                        wraps=fetch._aria2) as spy:
            multi = fetch.download(f"{self.base}/big.bin", self.tmp, segments=4)
            self.assertTrue(spy.called, "aria2c path was not used")
        self.assertEqual(single.read_bytes(), multi.read_bytes())
        self.assertEqual(multi.read_bytes(), BIG)

    @unittest.skipUnless(shutil.which("aria2c"), "aria2c not installed")
    def test_segmented_concurrent(self):
        errors = []
        paths = []

        def go():
            try:
                paths.append(fetch.download(f"{self.base}/big.bin", self.tmp,
                                            segments=4))
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=go) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({str(p) for p in paths}), 3)
        for p in paths:
            self.assertEqual(p.read_bytes(), BIG)


class TestClaimTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="speedy-claim-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_claim_uses_base_name_when_free(self):
        dest, part, fd = fetch._claim_target(self.tmp / "a.bin", True)
        self.assertEqual(dest.name, "a.bin")
        self.assertEqual(part.name, "a.bin.part")
        fetch._release(fd)

    def test_claim_skips_existing_finished_file(self):
        (self.tmp / "a.bin").write_bytes(b"done")
        dest, part, fd = fetch._claim_target(self.tmp / "a.bin", True)
        self.assertEqual(dest.name, "a (1).bin")
        fetch._release(fd)

    def test_claim_reuses_unlocked_part_for_resume(self):
        (self.tmp / "a.bin.part").write_bytes(b"partial")
        dest, part, fd = fetch._claim_target(self.tmp / "a.bin", True)
        self.assertEqual(dest.name, "a.bin")  # resume, not a new name
        fetch._release(fd)

    def test_claim_moves_on_when_part_is_locked(self):
        first = fetch._claim_target(self.tmp / "a.bin", True)
        second = fetch._claim_target(self.tmp / "a.bin", True)
        self.assertNotEqual(first[0], second[0])
        fetch._release(first[2])
        fetch._release(second[2])


class TestRouting(unittest.TestCase):
    def test_flags_default_off(self):
        args = speedy.default_args()
        self.assertFalse(args.direct)
        self.assertFalse(args.media)

    def test_default_args_rejects_unknown_option(self):
        with self.assertRaises(AttributeError):
            speedy.default_args(no_such_option=1)

    def test_direct_mode_skips_media(self):
        args = speedy.default_args(direct=True)
        with unittest.mock.patch.object(speedy, "download_direct",
                                        return_value=0) as m:
            speedy.run_urls(["http://x/a.pdf"], args)
            self.assertTrue(m.called)

    def test_media_mode_skips_direct(self):
        args = speedy.default_args(media=True)
        with unittest.mock.patch.object(speedy, "download",
                                        return_value=0) as m:
            speedy.run_urls(["http://x/a.pdf"], args)
            self.assertTrue(m.called)


class TestWebApi(ServerMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        speedy_web.Handler.manager = speedy_web.JobManager()
        cls.api = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), speedy_web.Handler)
        cls.api_port = cls.api.server_address[1]
        cls.api_thread = threading.Thread(target=cls.api.serve_forever,
                                          daemon=True)
        cls.api_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        super().tearDownClass()

    def post(self, payload, raw=False):
        url = f"http://127.0.0.1:{self.api_port}/api/download"
        data = payload if raw else json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path):
        url = f"http://127.0.0.1:{self.api_port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.read(), r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), None

    def wait_for(self, job_id, timeout=15.0):
        deadline = timeout / 0.1
        job = None
        for _ in range(int(deadline)):
            _, jobs_body, _ = self.get("/api/jobs")
            job = next(j for j in json.loads(jobs_body) if j["id"] == job_id)
            if job["status"] in ("finished", "error"):
                return job
            threading.Event().wait(0.1)
        return job

    def test_index_served(self):
        status, body, ctype = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"Speedy", body)

    def test_asset_served(self):
        status, body, ctype = self.get("/favicon.ico")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/x-icon")
        self.assertTrue(body.startswith(b"\x00\x00\x01\x00"))  # ICO magic

    def test_manifest_served(self):
        status, body, ctype = self.get("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertIn("manifest", ctype)
        self.assertEqual(json.loads(body)["name"], "Speedy")

    def test_unknown_path_404(self):
        status, _, _ = self.get("/nope")
        self.assertEqual(status, 404)

    def test_rejects_non_http_url(self):
        status, body = self.post({"url": "ftp://x/y"})
        self.assertEqual(status, 400)
        self.assertIn("http", json.loads(body)["error"])

    def test_rejects_unknown_mode(self):
        status, body = self.post({"url": "https://x/a.pdf", "mode": "evil"})
        self.assertEqual(status, 400)
        self.assertIn("mode", json.loads(body)["error"])

    def test_rejects_unknown_format(self):
        status, body = self.post({"url": "https://x/a.pdf", "format": "8k"})
        self.assertEqual(status, 400)
        self.assertIn("format", json.loads(body)["error"])

    def test_rejects_bad_json(self):
        status, _ = self.post(b"{not json", raw=True)
        self.assertEqual(status, 400)

    def test_accepts_valid_job(self):
        status, body = self.post({
            "url": f"{self.base}/file.bin", "mode": "direct",
            "output_dir": str(self.tmp), "segments": 9999})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["status"], "queued")

    def test_direct_download_end_to_end(self):
        status, body = self.post({
            "url": f"{self.base}/file.bin", "mode": "direct",
            "output_dir": str(self.tmp), "segments": 1})
        self.assertEqual(status, 202)
        job = self.wait_for(json.loads(body)["id"])
        self.assertEqual(job["status"], "finished", job.get("error"))
        self.assertEqual((self.tmp / "file.bin").read_bytes(), PAYLOAD)

    def test_webpage_in_direct_mode_errors_cleanly(self):
        status, body = self.post({
            "url": f"{self.base}/page.html", "mode": "direct",
            "output_dir": str(self.tmp)})
        self.assertEqual(status, 202)
        job = self.wait_for(json.loads(body)["id"])
        self.assertEqual(job["status"], "error")
        self.assertIn("web page", job["error"])


class TestJobManager(unittest.TestCase):
    def test_caps_history(self):
        mgr = speedy_web.JobManager()
        with unittest.mock.patch.object(speedy_web, "MAX_JOBS", 5), \
                unittest.mock.patch.object(speedy_web.JobManager, "_run"):
            for i in range(8):
                mgr.create(f"https://example.invalid/{i}", mode="direct",
                           output_dir="/tmp", fmt="best", segments=1)
            self.assertLessEqual(len(mgr.all()), 5)

    def test_snapshot_shape(self):
        job = speedy_web.Job("https://x/y.bin", mode="direct",
                             output_dir="/tmp", fmt="best", segments=1)
        self.assertEqual(
            set(job.snapshot()),
            {"id", "url", "status", "title", "percent", "speed", "eta",
             "downloaded", "total", "filename", "error"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
