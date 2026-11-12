import hashlib
import io
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from backend import config, skills
from tests.helpers import LowRAMTestCase

URL = "https://raw.githubusercontent.com/someone/repo/main/skill.md"


class FakeResponse:
    def __init__(self, data: bytes, headers: dict | None = None):
        self._data = io.BytesIO(data)
        self.headers = headers or {}

    def read(self, n=-1):
        return self._data.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, data: bytes = b"# Skill\n", headers: dict | None = None, error: Exception | None = None):
        self.data, self.headers, self.error = data, headers, error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.data, self.headers)


class ValidationTests(unittest.TestCase):
    def test_valid_urls(self):
        self.assertEqual(skills.validate_url(URL), URL)
        skills.validate_url("https://gist.githubusercontent.com/u/abc/raw/x.MD")
        skills.validate_url("https://raw.githubusercontent.com:443/a/b.md")

    def test_invalid_urls(self):
        bad = [
            "http://raw.githubusercontent.com/a/b.md",
            "https://evil.com/a/b.md",
            "https://raw.githubusercontent.com.evil.com/a.md",
            "https://user:pw@raw.githubusercontent.com/a.md",
            "https://user@raw.githubusercontent.com/a.md",
            "https://raw.githubusercontent.com:8443/a.md",
            "https://raw.githubusercontent.com/a/b.txt",
            "https://raw.githubusercontent.com/a/b.md/more",
            "file:///etc/passwd",
            "",
            None,
            5,
        ]
        for url in bad:
            with self.subTest(url=url), self.assertRaises(skills.SkillError):
                skills.validate_url(url)

    def test_names(self):
        for ok in ("a", "my-skill", "x.y_z", "0abc", "a" * 64):
            self.assertEqual(skills.validate_name(ok), ok)
        for bad in ("", "A", "-a", ".a", "a/b", "a b", "a..b", "a" * 65, None, "../x", "a\n"):
            with self.subTest(name=bad), self.assertRaises(skills.SkillError):
                skills.validate_name(bad)


class InstallTests(LowRAMTestCase):
    def test_success(self):
        body = "# Skill\nDo things.\n".encode()
        opener = FakeOpener(body)
        meta = skills.install_skill(URL, "my-skill", opener=opener)
        self.assertEqual(meta["sha256"], hashlib.sha256(body).hexdigest())
        self.assertIs(meta["trusted"], False)
        self.assertEqual(meta["bytes"], len(body))
        folder = config.skills_dir()
        self.assertEqual((folder / "my-skill.md").read_bytes(), body)
        stored = json.loads((folder / "my-skill.json").read_text())
        self.assertEqual(stored["sha256"], meta["sha256"])
        self.assertIs(stored["trusted"], False)
        self.assertEqual(stored["url"], URL)
        self.assertEqual(len(opener.requests), 1)
        self.assertIn(str(self.home), meta["path"])

    def test_size_limit(self):
        ok = FakeOpener(b"a" * skills.MAX_SKILL_BYTES)
        self.assertEqual(skills.install_skill(URL, "exact", opener=ok)["bytes"], skills.MAX_SKILL_BYTES)
        big = FakeOpener(b"a" * (skills.MAX_SKILL_BYTES + 1))
        with self.assertRaisesRegex(skills.SkillError, "larger"):
            skills.install_skill(URL, "big", opener=big)
        self.assertFalse((config.skills_dir() / "big.md").exists())

    def test_content_length_header_rejected_early(self):
        opener = FakeOpener(b"tiny", headers={"Content-Length": str(10**9)})
        with self.assertRaisesRegex(skills.SkillError, "larger"):
            skills.install_skill(URL, "liar", opener=opener)

    def test_binary_and_non_utf8_rejected(self):
        with self.assertRaises(skills.SkillError):
            skills.install_skill(URL, "bin1", opener=FakeOpener(b"abc\x00def"))
        with self.assertRaisesRegex(skills.SkillError, "UTF-8"):
            skills.install_skill(URL, "bin2", opener=FakeOpener(b"\xff\xfe\xfa"))
        self.assertEqual(skills.list_skills(), [])

    def test_download_errors(self):
        for err in (
            urllib.error.HTTPError(URL, 404, "nf", {}, None),
            urllib.error.URLError("no route"),
            TimeoutError("slow"),
        ):
            with self.subTest(err=type(err).__name__), self.assertRaises(skills.SkillError):
                skills.install_skill(URL, "x", opener=FakeOpener(error=err))

    def test_redirect_error_propagates_as_skill_error(self):
        err = urllib.error.URLError(skills.SkillError("Redirects are not followed"))
        with self.assertRaisesRegex(skills.SkillError, "Redirects"):
            skills.install_skill(URL, "x", opener=FakeOpener(error=err))

    def test_redirect_handler_refuses(self):
        handler = skills._NoRedirect()
        req = urllib.request.Request(URL)
        with self.assertRaisesRegex(skills.SkillError, "Redirects are not followed"):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.com/x.md")

    def test_real_opener_refuses_redirect(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/x.md")
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        env = {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        with mock.patch.dict(os.environ, env):
            for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
                os.environ.pop(key, None)
            with self.assertRaisesRegex(skills.SkillError, "Redirects"):
                skills._download(f"http://127.0.0.1:{srv.server_address[1]}/a.md", None)

    def test_overwrite_rule(self):
        skills.install_skill(URL, "dup", opener=FakeOpener(b"one\n"))
        opener = FakeOpener(b"two\n")
        with self.assertRaisesRegex(skills.SkillError, "already exists"):
            skills.install_skill(URL, "dup", opener=opener)
        self.assertEqual(opener.requests, [])
        self.assertEqual((config.skills_dir() / "dup.md").read_bytes(), b"one\n")
        meta = skills.install_skill(URL, "dup", overwrite=True, opener=opener)
        self.assertEqual((config.skills_dir() / "dup.md").read_bytes(), b"two\n")
        self.assertEqual(meta["sha256"], hashlib.sha256(b"two\n").hexdigest())

    def test_invalid_input_makes_no_request(self):
        opener = FakeOpener()
        with self.assertRaises(skills.SkillError):
            skills.install_skill("https://evil.com/a.md", "ok", opener=opener)
        with self.assertRaises(skills.SkillError):
            skills.install_skill(URL, "BAD NAME", opener=opener)
        self.assertEqual(opener.requests, [])


class ListTests(LowRAMTestCase):
    def test_list_and_checksum(self):
        self.assertEqual(skills.list_skills(), [])
        skills.install_skill(URL, "b-skill", opener=FakeOpener(b"b\n"))
        skills.install_skill(URL, "a-skill", opener=FakeOpener(b"a\n"))
        items = skills.list_skills()
        self.assertEqual([s["name"] for s in items], ["a-skill", "b-skill"])
        self.assertTrue(all(s["checksum_ok"] for s in items))
        (config.skills_dir() / "a-skill.md").write_bytes(b"tampered\n")
        by_name = {s["name"]: s for s in skills.list_skills()}
        self.assertFalse(by_name["a-skill"]["checksum_ok"])
        self.assertTrue(by_name["b-skill"]["checksum_ok"])

    def test_broken_metadata_and_orphans_are_listed_with_error(self):
        folder = config.skills_dir()
        (folder / "broken.json").write_text("{nope")
        (folder / "orphan.json").write_text(json.dumps({"name": "orphan", "sha256": "x"}))
        (folder / "list.json").write_text("[1]")
        items = {s["name"]: s for s in skills.list_skills()}
        self.assertEqual(set(items), {"broken", "orphan", "list"})
        for item in items.values():
            self.assertIs(item["checksum_ok"], False)
            self.assertTrue(item["error"])
        self.assertIn("unreadable", items["broken"]["error"])
        self.assertIn("missing", items["orphan"]["error"])

    def test_skill_path(self):
        self.assertEqual(skills.skill_path("abc"), config.skills_dir() / "abc.md")
        with self.assertRaises(skills.SkillError):
            skills.skill_path("../abc")


if __name__ == "__main__":
    unittest.main()
