import http.client
import io
import json
import os
import threading
import urllib.error
from pathlib import Path
from unittest import mock

from backend import app, diagnostics, downloads, settings
from tests.engine_helpers import quiet_logger
from tests.helpers import LowRAMTestCase, closed_port_url

RELEASE = "https://api.github.com/repos/pauls234/LowRam-Ai/releases/latest"
IDS = {"app", "runtime", "model", "server", "ram", "disk", "models_folder", "media_runtime", "lan"}


class FakeResponse:
    def __init__(self, body: bytes):
        self.status = 200
        self.headers = {}
        self._buf = io.BytesIO(body)

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        pass


class FakeGitHub:
    """Opener for downloads.set_opener: records requests, answers from a script."""

    def __init__(self):
        self.calls = []
        self.answer = {"tag_name": "v0.0.1", "html_url": "https://github.com/pauls234/LowRam-Ai/releases/tag/v0.0.1"}
        self.status = None
        self.error = None

    def __call__(self, request, timeout):
        self.calls.append({"url": request.full_url, "timeout": timeout, "headers": dict(request.header_items())})
        if self.error is not None:
            raise self.error
        if self.status is not None:
            raise urllib.error.HTTPError(request.full_url, self.status, "x", {}, None)
        return FakeResponse(json.dumps(self.answer).encode())


class DiagnosticsTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        quiet_logger(self, "lowram.diagnostics")
        self.github = FakeGitHub()
        downloads.set_opener(self.github)
        self.addCleanup(downloads.set_opener, None)
        diagnostics._reset_for_tests()
        self.addCleanup(diagnostics._reset_for_tests)


class CheckTests(DiagnosticsTestCase):
    def test_all_checks_present_and_well_formed(self):
        data = diagnostics.diagnostics()
        self.assertEqual({c["id"] for c in data["checks"]}, IDS)
        for check in data["checks"]:
            self.assertEqual(set(check), {"id", "title", "status", "detail", "hint"})
            self.assertIn(check["status"], ("ok", "warn", "fail"))
        self.assertIn("LowRAM AI", data["report"])

    def test_report_hides_home_and_secrets(self):
        home = str(Path.home())
        os.environ["LOWRAM_LLM_URL"] = "https://user:pw@example.com/v1/chat/completions?key=secret"
        bad = diagnostics._check("x", "X", "warn", f"{home}/models token=abc123 https://u:p@h.example/x?api_key=zz")
        report = diagnostics.build_report([bad, *diagnostics.run_checks()])
        self.assertNotIn(home, report)
        self.assertIn("~/models", report)
        for secret in ("abc123", "secret", "u:p@", "zz", "pw@"):
            self.assertNotIn(secret, report)

    def test_checks_never_raise(self):
        def boom():
            raise RuntimeError("kaputt")

        with mock.patch.object(diagnostics, "CHECKS", (("ram", "RAM", boom),)):
            checks = diagnostics.run_checks()
        self.assertEqual((checks[0]["id"], checks[0]["status"]), ("ram", "warn"))
        with mock.patch.object(diagnostics, "_engine", return_value=None):
            data = diagnostics.diagnostics()
        self.assertEqual({c["id"] for c in data["checks"]}, IDS)

    def test_missing_models_folder_and_model(self):
        os.environ.pop("LOWRAM_LLM_URL", None)
        settings.update("engine", {"models_dir": str(self.tmp / "unplugged"), "model_id": "qwen2.5-1.5b-instruct"})
        checks = {c["id"]: c for c in diagnostics.run_checks()}
        self.assertEqual(checks["models_folder"]["status"], "fail")
        self.assertEqual(checks["model"]["status"], "fail")
        self.assertIn("Festplatte", checks["model"]["hint"])
        self.assertEqual(checks["server"]["status"], "warn")


class SelftestTests(DiagnosticsTestCase):
    def test_selftest_ok(self):
        result = diagnostics.selftest()
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["error"])
        self.assertGreaterEqual(result["seconds"], 0)
        self.assertIn("Antworte mit OK", self.llm.user_prompt())

    def test_selftest_unreachable(self):
        os.environ["LOWRAM_LLM_URL"] = closed_port_url()
        result = diagnostics.selftest()
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])
        self.assertIsNone(result["tokens_per_s"])

    def test_selftest_without_running_engine(self):
        os.environ.pop("LOWRAM_LLM_URL", None)
        result = diagnostics.selftest()
        self.assertFalse(result["ok"])
        self.assertIn("kein Modell", result["error"])


class UpdateTests(DiagnosticsTestCase):
    def test_newer_release_and_cache(self):
        self.github.answer = {"tag_name": "v99.0.0", "html_url": "https://github.com/pauls234/LowRam-Ai/releases/tag/v99.0.0"}
        result = diagnostics.update_status()
        self.assertEqual((result["checked"], result["available"], result["latest"]), (True, True, "v99.0.0"))
        self.assertTrue(result["url"].startswith("https://github.com/pauls234/LowRam-Ai/"))
        self.assertEqual(len(self.github.calls), 1)
        call = self.github.calls[0]
        self.assertEqual((call["url"], call["timeout"]), (RELEASE, 5.0))
        self.assertNotIn("Authorization", call["headers"])
        diagnostics.update_status()
        self.assertEqual(len(self.github.calls), 1)  # cached

    def test_same_version_and_foreign_url(self):
        self.github.answer = {"tag_name": "v0.0.1", "html_url": "https://evil.example/x"}
        result = diagnostics.update_status()
        self.assertEqual((result["checked"], result["available"], result["url"]), (True, False, None))

    def test_private_repo_is_silent(self):
        self.github.status = 404
        result = diagnostics.update_status()
        self.assertEqual((result["checked"], result["error"], result["available"]), (False, None, False))

    def test_network_error_reported(self):
        self.github.error = urllib.error.URLError(TimeoutError("timed out"))
        result = diagnostics.update_status()
        self.assertFalse(result["checked"])
        self.assertTrue(result["error"])

    def test_disabled(self):
        settings.update("ui", {"check_updates": False})
        result = diagnostics.update_status()
        self.assertFalse(result["checked"])
        self.assertEqual(self.github.calls, [])

    def test_version_compare(self):
        self.assertTrue(diagnostics.is_newer("v0.3.10", "0.3.2"))
        self.assertFalse(diagnostics.is_newer("0.3", "0.3.0"))
        self.assertFalse(diagnostics.is_newer("nightly", "0.3.0"))


class RouteTests(DiagnosticsTestCase):
    def setUp(self):
        super().setUp()
        self.server = app.LowRAMServer(0, token="t")
        port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(5)))
        self.port = port

    def api(self, method, path, body=None):
        headers = {"Connection": "close"}
        data = None
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-LowRAM-Token": "t"})
            data = json.dumps(body or {}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_routes(self):
        status, data = self.api("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual({c["id"] for c in data["checks"]}, IDS)
        status, data = self.api("POST", "/api/diagnostics/selftest")
        self.assertEqual((status, data["ok"]), (200, True))
        status, data = self.api("GET", "/api/update")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"current", "latest", "url", "available", "checked", "error"})
