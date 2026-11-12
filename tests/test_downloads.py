import hashlib
import logging
import os
import threading
import unittest
from collections import namedtuple
from unittest import mock

from backend import app, config, downloads
from tests.engine_helpers import FakeHub, make_tar_gz, make_zip, quiet_logger
from tests.helpers import LowRAMTestCase, wait_until

HF = "huggingface.co/org/repo/resolve/main/model.gguf"
URL = "https://" + HF
Usage = namedtuple("Usage", "total used free")


class DownloadTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.hub = FakeHub()
        self.addCleanup(self.hub.close)
        downloads.set_opener(self.hub.opener)
        self.addCleanup(downloads.set_opener, None)
        self.addCleanup(downloads._reset_for_tests)
        quiet_logger(self, "lowram.downloads")
        for name, value in (("RETRY_DELAYS_S", (0.01,)), ("PROGRESS_INTERVAL_S", 0.0)):
            patcher = mock.patch.object(downloads, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dest = config.models_dir() / "model.gguf"
        self.data = os.urandom(300_000)
        self.sha = hashlib.sha256(self.data).hexdigest()

    def run_job(self, url=URL, dest=None, job_id="job1", **kw):
        kw.setdefault("label", "Test")
        kw.setdefault("kind", "model")
        downloads.start(job_id, url, dest or self.dest, **kw)
        job = downloads.wait(job_id, 15)
        self.assertIn(job["state"], downloads.FINAL_STATES, job)
        return job


class UrlPolicyTests(unittest.TestCase):
    def test_allowed_hosts(self):
        for url in (
            "https://huggingface.co/x", "https://cdn-lfs.huggingface.co/x", "https://cas-bridge.xethub.hf.co/x?sig=1",
            "https://github.com/a/b/releases/download/x", "https://api.github.com/repos/a/b",
            "https://objects.githubusercontent.com/x", "https://release-assets.githubusercontent.com/x",
            "https://HuggingFace.co:443/x",
        ):
            self.assertEqual(downloads.check_url(url), url)

    def test_refused_urls(self):
        for url in (
            "http://huggingface.co/x", "ftp://huggingface.co/x", "file:///etc/passwd", "https://hf.co/x",
            "https://huggingface.co.evil.com/x", "https://evilhuggingface.co/x", "https://evil.com/huggingface.co",
            "https://user:pw@huggingface.co/x", "https://huggingface.co:8443/x", "https://127.0.0.1/x",
            "https://githubusercontent.com/x", "https://raw.githubusercontent.com/x", "", "https://hugging face.co/",
        ):
            with self.subTest(url=url), self.assertRaises(downloads.DownloadError):
                downloads.check_url(url)

    def test_redact_drops_query_and_credentials(self):
        self.assertEqual(
            downloads.redact("https://u:p@cas-bridge.xethub.hf.co/a/b?X-Amz-Signature=secret#frag"),
            "https://cas-bridge.xethub.hf.co/a/b",
        )


class DownloadTests(DownloadTestCase):
    def test_download_verifies_and_renames(self):
        self.hub.files[HF] = self.data
        job = self.run_job(sha256=self.sha, size=len(self.data))
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertFalse((self.dest.parent / "model.gguf.part").exists())
        self.assertEqual((job["bytes_done"], job["bytes_total"]), (len(self.data), len(self.data)))
        self.assertIsNotNone(job["started_at"])
        self.assertEqual(set(job), {"id", "label", "kind", "state", "bytes_done", "bytes_total", "error",
                                    "started_at", "finished_at"})

    def test_progress_is_reported(self):
        self.hub.files[HF] = self.data
        self.hub.chunk, self.hub.delay = 20_000, 0.02
        downloads.start("job1", URL, self.dest, label="T", kind="model")
        self.assertTrue(wait_until(lambda: 0 < downloads.get("job1")["bytes_done"] < len(self.data), 10))
        self.assertEqual(downloads.wait("job1", 15)["state"], "done")

    def test_resume_with_range(self):
        self.hub.files[HF] = self.data
        part = self.dest.parent / "model.gguf.part"
        part.write_bytes(self.data[:100_000])
        job = self.run_job(sha256=self.sha, size=len(self.data))
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(self.hub.ranges(HF), ["bytes=100000-"])
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_server_ignoring_range_restarts(self):
        self.hub.files[HF] = self.data
        self.hub.ignore_range = True
        (self.dest.parent / "model.gguf.part").write_bytes(b"x" * 1000)
        job = self.run_job(sha256=self.sha)
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_connection_drop_is_resumed_automatically(self):
        self.hub.files[HF] = self.data
        self.hub.drops[HF] = [120_000]
        job = self.run_job(sha256=self.sha, size=len(self.data))
        self.assertEqual(job["state"], "done", job)
        ranges = self.hub.ranges(HF)
        self.assertIsNone(ranges[0])
        self.assertTrue(ranges[1].startswith("bytes=") and ranges[1] != "bytes=0-", ranges)

    def test_network_failure_keeps_part_for_resume(self):
        self.hub.files[HF] = self.data
        self.hub.drops[HF] = [50_000] * 10
        with mock.patch.object(downloads, "MAX_ATTEMPTS", 2):
            job = self.run_job(sha256=self.sha, size=len(self.data))
        self.assertEqual(job["state"], "failed")
        self.assertIn("closed early", job["error"])
        self.assertTrue((self.dest.parent / "model.gguf.part").exists())
        self.assertFalse(self.dest.exists())

    def test_sha_mismatch_fails_and_removes_part(self):
        self.hub.files[HF] = self.data
        job = self.run_job(sha256="0" * 64)
        self.assertEqual(job["state"], "failed")
        self.assertIn("SHA-256", job["error"])
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.dest.parent / "model.gguf.part").exists())

    def test_size_mismatch_fails(self):
        self.hub.files[HF] = self.data
        job = self.run_job(size=len(self.data) + 5)
        self.assertEqual(job["state"], "failed")
        self.assertFalse(self.dest.exists())

    def test_http_error_is_reported(self):
        self.hub.status[HF] = 404
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("HTTP 404", job["error"])

    def test_cancel_stops_promptly_and_removes_part(self):
        self.hub.files[HF] = os.urandom(3_000_000)
        self.hub.chunk, self.hub.delay = 16_384, 0.02
        downloads.start("job1", URL, self.dest, label="T", kind="model")
        self.assertTrue(wait_until(lambda: downloads.get("job1")["bytes_done"] > 0, 10))
        job = downloads.cancel("job1")
        self.assertEqual(job["state"], "cancelled", job)
        self.assertFalse((self.dest.parent / "model.gguf.part").exists())
        self.assertFalse(self.dest.exists())
        self.assertIsNone(downloads.cancel("missing"))

    def test_start_same_id_while_active_returns_running_job(self):
        self.hub.files[HF] = self.data
        self.hub.chunk, self.hub.delay = 16_384, 0.05
        first = downloads.start("job1", URL, self.dest, label="T", kind="model")
        second = downloads.start("job1", URL, self.dest, label="Other", kind="model")
        self.assertEqual(second["label"], first["label"])
        with self.assertRaises(ValueError):
            downloads.start("job2", URL, self.dest, label="T", kind="model")  # same destination

    def test_redirect_to_allowed_cdn_with_signed_url(self):
        self.hub.redirects[HF] = "https://cas-bridge.xethub.hf.co/xet/abc?X-Amz-Signature=topsecret"
        self.hub.files["cas-bridge.xethub.hf.co/xet/abc"] = self.data
        with self.assertLogs("lowram.downloads", logging.INFO) as logs:
            job = self.run_job(sha256=self.sha)
        self.assertEqual(job["state"], "done", job)
        self.assertNotIn("topsecret", "\n".join(logs.output))

    def test_redirect_to_unlisted_host_is_refused(self):
        self.hub.redirects[HF] = "https://evil.example.com/payload?sig=topsecret"
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("not allowed", job["error"])
        self.assertNotIn("topsecret", job["error"])
        self.assertFalse(any("evil" in url for url in self.hub.opened))

    def test_redirect_to_plain_http_is_refused(self):
        self.hub.redirects[HF] = "http://huggingface.co/other"
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("https", job["error"])

    def test_redirect_loop_is_limited(self):
        self.hub.redirects[HF] = URL
        job = self.run_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("Too many redirects", job["error"])
        self.assertEqual(len(self.hub.opened), downloads.MAX_REDIRECTS + 1)

    def test_disk_space_check(self):
        self.hub.files[HF] = self.data
        with mock.patch.object(downloads.shutil, "disk_usage", return_value=Usage(10**12, 0, 512 * 1024**2)):
            job = self.run_job(size=len(self.data))
        self.assertEqual(job["state"], "failed")
        self.assertIn("Not enough disk space", job["error"])
        self.assertEqual(self.hub.opened, [])

    def test_input_validation(self):
        good = {"label": "T", "kind": "model"}
        cases = [
            ("bad id!", URL, self.dest, good),
            ("ok", "http://huggingface.co/x", self.dest, good),
            ("ok", "https://evil.com/x", self.dest, good),
            ("ok", URL, self.tmp / "outside.gguf", good),
            ("ok", URL, config.models_dir() / ".." / ".." / "x", good),
            ("ok", URL, self.dest, {"label": "T", "kind": "weird"}),
            ("ok", URL, self.dest, {**good, "sha256": "xyz"}),
            ("ok", URL, self.dest, {**good, "size": -1}),
            ("ok", URL, self.dest, {**good, "extract": "rar"}),
        ]
        for job_id, url, dest, kw in cases:
            with self.subTest(job_id=job_id, url=url, dest=str(dest), kw=kw), self.assertRaises(ValueError):
                downloads.start(job_id, url, dest, **kw)
        self.assertEqual(downloads.list_jobs(), [])

    def test_refuses_symlinked_destination(self):
        target = self.tmp / "elsewhere"
        target.write_text("x")
        os.symlink(target, self.dest)
        with self.assertRaises(ValueError):
            downloads.start("job1", URL, self.dest, label="T", kind="model")

    def test_on_complete_failure_fails_job_and_removes_file(self):
        self.hub.files[HF] = self.data

        def boom(path):
            raise RuntimeError("post-check failed")

        job = self.run_job(on_complete=boom)
        self.assertEqual(job["state"], "failed")
        self.assertIn("post-check failed", job["error"])
        self.assertFalse(self.dest.exists())


TAR = "github.com/ggml-org/llama.cpp/releases/download/b1/llama.tar.gz"
ZIP = "github.com/ggml-org/llama.cpp/releases/download/b1/llama.zip"


class ExtractionTests(DownloadTestCase):
    def extract(self, key, data, fmt):
        self.hub.files[key] = data
        dest = config.bin_dir() / "rt"
        job = self.run_job("https://" + key, dest, extract=fmt, kind="runtime")
        return job, dest

    def test_tar_gz_with_exec_bit_and_sibling_symlink(self):
        data = make_tar_gz([
            ("dir", "llama-b1"), ("file", "llama-b1/llama-server", b"#!bin", 0o755),
            ("file", "llama-b1/libggml.so.0", b"lib"), ("symlink", "llama-b1/libggml.so", "libggml.so.0"),
            ("hardlink", "llama-b1/copy", "llama-b1/libggml.so.0"),
        ])
        job, dest = self.extract(TAR, data, "tar.gz")
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(os.access(dest / "llama-b1" / "llama-server", os.X_OK))
        self.assertFalse(os.access(dest / "llama-b1" / "libggml.so.0", os.X_OK))
        self.assertEqual(os.readlink(dest / "llama-b1" / "libggml.so"), "libggml.so.0")
        self.assertEqual((dest / "llama-b1" / "copy").read_bytes(), b"lib")
        self.assertFalse(downloads._part_path(dest).exists())
        self.assertEqual([p.name for p in dest.parent.iterdir()], ["rt"])

    def test_zip_extraction(self):
        job, dest = self.extract(ZIP, make_zip([("file", "build/bin/llama-server", b"x", 0o755)]), "zip")
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(os.access(dest / "build" / "bin" / "llama-server", os.X_OK))

    def assert_refused(self, key, data, fmt):
        job, dest = self.extract(key, data, fmt)
        self.assertEqual(job["state"], "failed", job)
        self.assertFalse(dest.exists())
        self.assertFalse(downloads._part_path(dest).exists())
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertEqual([p.name for p in dest.parent.iterdir()], [])
        return job

    def test_zip_slip_refused(self):
        job = self.assert_refused(ZIP, make_zip([("file", "ok.txt", b"1"), ("file", "../../../evil.txt", b"x")]), "zip")
        self.assertIn("leaves its folder", job["error"])

    def test_absolute_paths_refused(self):
        self.assert_refused(ZIP, make_zip([("file", "/tmp/evil.txt", b"x")]), "zip")
        self.assert_refused(TAR, make_tar_gz([("file", "/tmp/evil.txt", b"x")]), "tar.gz")

    def test_tar_traversal_refused(self):
        self.assert_refused(TAR, make_tar_gz([("file", "a/../../evil.txt", b"x")]), "tar.gz")

    def test_symlink_escape_refused(self):
        for entries in (
            [("symlink", "link", "/etc")],
            [("symlink", "link", "../..")],
            [("symlink", "link", "sub/../../x")],
            [("dir", "d"), ("symlink", "d/up", "..")],
        ):
            with self.subTest(entries=entries):
                job = self.assert_refused(TAR, make_tar_gz(entries), "tar.gz")
                self.assertIn("outside", job["error"])
        self.assert_refused(ZIP, make_zip([("symlink", "link", "../../etc/passwd")]), "zip")

    def test_writing_through_symlink_refused(self):
        entries = [("file", "real", b"1"), ("symlink", "alias", "real"), ("file", "alias/x", b"2")]
        job = self.assert_refused(TAR, make_tar_gz(entries), "tar.gz")
        self.assertIn("symbolic link", job["error"])

    def test_special_files_refused(self):
        self.assert_refused(TAR, make_tar_gz([("fifo", "pipe")]), "tar.gz")
        self.assert_refused(TAR, make_tar_gz([("hardlink", "h", "/etc/passwd")]), "tar.gz")

    def test_damaged_archive(self):
        job = self.assert_refused(TAR, b"not a tarball at all" * 10, "tar.gz")
        self.assertIn("damaged", job["error"])

    def test_extraction_size_limit(self):
        with mock.patch.object(downloads, "MAX_EXTRACT_BYTES", 10):
            self.assert_refused(TAR, make_tar_gz([("file", "big", b"x" * 100)]), "tar.gz")


class MediaStyleExtractTests(DownloadTestCase):
    """``extract=<directory Path>`` as used by backend/media.py."""

    def test_extract_into_directory_detects_format_and_keeps_archive(self):
        cases = [
            ("rt.zip", make_zip([("file", "bin/sd", b"x", 0o755)]), "bin/sd"),
            ("rt.tar.gz", make_tar_gz([("file", "bin/sd", b"y", 0o755)]), "bin/sd"),
            ("archive-without-suffix", make_tar_gz([("file", "sd", b"z", 0o755)]), "sd"),  # magic bytes
            ("zip-without-suffix", make_zip([("file", "sd", b"w", 0o755)]), "sd"),
        ]
        for name, data, inner in cases:
            with self.subTest(name=name):
                key = f"github.com/leejet/sd/releases/download/v1/{name}"
                self.hub.files[key] = data
                archive = config.bin_dir() / "media" / name
                target = config.bin_dir() / "media" / f"{name}-dir"
                job = self.run_job("https://" + key, archive, job_id="media-runtime", kind="media-runtime",
                                   extract=target, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
                self.assertEqual(job["state"], "done", job)
                self.assertTrue(os.access(target / inner, os.X_OK))
                self.assertEqual(archive.read_bytes(), data)
                self.assertFalse(downloads._part_path(archive).exists())

    def test_dest_appears_only_after_completion(self):
        data = make_tar_gz([("file", "model.bin", os.urandom(400_000))])
        key = "huggingface.co/org/media/resolve/main/pack.tar.gz"
        self.hub.files[key] = data
        self.hub.chunk, self.hub.delay = 16_384, 0.01
        archive, target = config.models_dir() / "pack.tar.gz", config.models_dir() / "pack"
        downloads.start("media-model-sd15-unet", "https://" + key, archive, label="SD", kind="media-model",
                        extract=str(target), sha256=hashlib.sha256(data).hexdigest())
        seen, state = [], "queued"
        while state in downloads.ACTIVE_STATES:
            exists = archive.exists() or target.exists()  # checked before the state: no false alarms
            state = downloads.get("media-model-sd15-unet")["state"]
            if state in ("queued", "downloading", "verifying"):
                seen.append(exists)
        self.assertEqual(state, "done")
        self.assertGreater(len(seen), 5)
        self.assertFalse(any(seen))
        self.assertTrue((target / "model.bin").is_file() and archive.is_file())

    def test_bad_archive_and_invalid_targets(self):
        key = "huggingface.co/org/media/resolve/main/blob"
        self.hub.files[key] = b"plain bytes, not an archive"
        archive, target = config.models_dir() / "blob", config.models_dir() / "blob-dir"
        job = self.run_job("https://" + key, archive, kind="media-model", extract=target)
        self.assertEqual(job["state"], "failed")
        self.assertIn("neither", job["error"])
        self.assertFalse(archive.exists() or target.exists())
        for bad in ("relative/dir", self.tmp / "outside", archive):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                downloads.start("media-x", "https://" + key, archive, label="T", kind="media-model", extract=bad)
        self.assertIsNone(downloads.get("media-model-unknown"))


class JsonFetchTests(DownloadTestCase):
    def test_fetch_json_and_pagination_link(self):
        self.hub.json["api.github.com/x"] = ({"a": 1}, '<https://api.github.com/x?page=2>; rel="next"')
        data, nxt = downloads.fetch_json_with_next("https://api.github.com/x")
        self.assertEqual(data, {"a": 1})
        self.assertEqual(nxt, "https://api.github.com/x?page=2")

    def test_fetch_json_errors(self):
        self.hub.files["api.github.com/bad"] = b"<html>"
        with self.assertRaises(downloads.DownloadError):
            downloads.fetch_json("https://api.github.com/bad")
        with self.assertRaises(downloads.DownloadError):
            downloads.fetch_json("http://api.github.com/x")

    def test_tls_failure_message_is_actionable(self):
        import ssl
        import urllib.error

        def failing(request, timeout):
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))

        with self.assertRaises(downloads.DownloadError) as ctx:
            downloads.open_url("https://huggingface.co/x?token=secret", opener=failing)
        self.assertIn("certificate", str(ctx.exception))
        self.assertNotIn("secret", str(ctx.exception))
        self.assertFalse(ctx.exception.transient)


class DownloadRouteTests(DownloadTestCase):
    def setUp(self):
        super().setUp()
        from tests.test_app import AppTestCase

        self.server = app.LowRAMServer(0, token="t")
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(5)))
        self.api = lambda *a, **kw: AppTestCase.api(self, *a, **kw)
        self.request = lambda *a, **kw: AppTestCase.request(self, *a, **kw)

    def test_list_and_cancel(self):
        self.assertEqual(self.api("GET", "/api/downloads"), (200, {"downloads": []}))
        self.hub.files[HF] = os.urandom(2_000_000)
        self.hub.chunk, self.hub.delay = 16_384, 0.02
        downloads.start("model-x", URL, self.dest, label="X", kind="model")
        status, data = self.api("GET", "/api/downloads")
        self.assertEqual([d["id"] for d in data["downloads"]], ["model-x"])
        self.assertEqual(self.api("POST", "/api/downloads/model-x/cancel", token="bad")[0], 403)
        status, data = self.api("POST", "/api/downloads/model-x/cancel")
        self.assertEqual(status, 200)
        self.assertEqual(data["download"]["state"], "cancelled")
        self.assertEqual(self.api("POST", "/api/downloads/nope/cancel")[0], 404)
        self.assertEqual(self.api("POST", "/api/downloads/12345/cancel")[0], 404)
        self.assertEqual(self.api("POST", "/api/downloads/a%2Fb/cancel")[0], 404)


if __name__ == "__main__":
    unittest.main()
