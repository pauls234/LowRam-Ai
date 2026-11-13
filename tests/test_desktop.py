"""Desktop launcher (no GUI): ports, single instance, browser/window paths, signals, JS API, frozen paths."""

import json
import logging
import os
import signal
import socket
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend import __version__, app, web
from desktop import launcher
from tests.helpers import LowRAMTestCase

REPO = Path(__file__).resolve().parent.parent
TOKEN = "desktop-test-token-0123456789"
ABS_DIR = str(Path.home().resolve() / "LowRAM models")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening_socket() -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


def answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def fake_webview(create_window, start) -> types.ModuleType:
    module = types.ModuleType("webview")
    module.settings = {"ALLOW_DOWNLOADS": False, "ALLOW_FILE_URLS": True}
    module.FileDialog = types.SimpleNamespace(OPEN=10, FOLDER=20, SAVE=30)
    module.create_window, module.start = create_window, start
    return module


class _OtherJSON(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps({"hello": "not lowram"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DesktopTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        # Feature modules may register startup hooks (e.g. an engine); keep tests isolated.
        self.events = []
        for name, hooks in (("STARTUP_HOOKS", [lambda s: self.events.append("startup")]),
                            ("SHUTDOWN_HOOKS", [lambda s: self.events.append("shutdown")])):
            patcher = mock.patch.object(web, name, hooks)
            patcher.start()
            self.addCleanup(patcher.stop)
        quiet = mock.patch("desktop.launcher.say")  # keep the test output clean
        quiet.start()
        self.addCleanup(quiet.stop)
        null = logging.NullHandler()  # expected warnings must not reach stderr (logging.lastResort)
        logging.getLogger("lowram.desktop").addHandler(null)
        self.addCleanup(logging.getLogger("lowram.desktop").removeHandler, null)
        self.home.mkdir(parents=True, exist_ok=True)

    def serve_lowram(self):
        server = app.LowRAMServer(0, token=TOKEN)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)

        self.addCleanup(stop)
        return server.server_address[1]


class PortTests(DesktopTestCase):
    def test_candidate_ports(self):
        self.assertEqual(launcher.candidate_ports(), list(range(8765, 8786)))
        self.assertEqual(launcher.candidate_ports(9100), [9100])

    def test_port_is_free(self):
        busy = listening_socket()
        self.addCleanup(busy.close)
        self.assertFalse(launcher.port_is_free(busy.getsockname()[1]))
        self.assertTrue(launcher.port_is_free(free_port()))

    def test_start_server_skips_busy_port_and_binds_loopback(self):
        busy = listening_socket()
        self.addCleanup(busy.close)
        target = free_port()
        server = launcher.start_server(app, [busy.getsockname()[1], target])
        self.addCleanup(app.shutdown, server)
        self.assertEqual(server.server_address, ("127.0.0.1", target))
        self.assertEqual(os.environ["LOWRAM_PORT"], str(target))
        self.assertEqual(launcher.wait_until_ready(target, 5)["version"], __version__)
        self.assertIn("startup", self.events)

    def test_no_free_port_is_an_explicit_error(self):
        busy = listening_socket()
        self.addCleanup(busy.close)
        with self.assertRaises(launcher.LauncherError) as ctx:
            launcher.start_server(app, [busy.getsockname()[1]])
        self.assertIn("used by another program", str(ctx.exception))
        self.assertNotIn("LOWRAM_PORT", os.environ)

    def test_port_argument_validation_and_finder_arguments(self):
        parser = launcher.build_parser()
        with mock.patch("sys.stderr"):
            for bad in ("80", "70000", "abc"):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["--port", bad])
        args, unknown = parser.parse_known_args(["-psn_0_12345", "--no-window"])
        self.assertTrue(args.no_window)
        self.assertEqual(unknown, ["-psn_0_12345"])


class InstanceTests(DesktopTestCase):
    def test_probe_recognises_lowram_only(self):
        port = self.serve_lowram()
        self.assertEqual(launcher.probe_instance(port)["features"], list(app.LOADED_FEATURES))
        other = ThreadingHTTPServer(("127.0.0.1", 0), _OtherJSON)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.assertIsNone(launcher.probe_instance(other.server_address[1]))
        self.assertIsNone(launcher.probe_instance(free_port()))

    def test_verified_instance_is_reused_and_file_lifecycle(self):
        port = self.serve_lowram()
        path, nonce = launcher.write_instance_file(self.home, port, TOKEN)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        data = json.loads(path.read_text())
        self.assertEqual((data["port"], data["pid"]), (port, os.getpid()))
        self.assertNotIn(TOKEN, path.read_text(), "only a hash of the page token is stored")
        self.assertEqual(launcher.find_verified_instance(self.home), port)
        launcher.remove_instance_file(path, "someone-else")
        self.assertTrue(path.exists(), "a file written by another instance is left alone")
        launcher.remove_instance_file(path, nonce)
        self.assertFalse(path.exists())
        self.assertIsNone(launcher.find_verified_instance(self.home))

    def test_unproven_instances_are_not_reused(self):
        port = self.serve_lowram()
        path, _ = launcher.write_instance_file(self.home, port, "an-impostor-token-value")
        self.assertIsNone(launcher.find_verified_instance(self.home), "token hash mismatch")
        good = {**json.loads(path.read_text()), "token_sha256": launcher._token_digest(TOKEN)}
        for change in ({"create_time": good["create_time"] + 60}, {"pid": 4_206_649}, {"port": "8765"}, {}):
            path.write_text(json.dumps({**good, **change}))
            expected = port if not change else None
            self.assertEqual(launcher.find_verified_instance(self.home), expected, change)
        if os.name != "nt":
            os.chmod(path, 0o666)
            self.assertIsNone(launcher.find_verified_instance(self.home), "writable by others")
            os.chmod(path, 0o600)
            real = self.home / "elsewhere.json"
            path.rename(real)
            path.symlink_to(real)
            self.assertIsNone(launcher.find_verified_instance(self.home), "symlink")

    def test_lowram_without_proof_gets_its_own_server(self):
        port = self.serve_lowram()  # answers /api/status but wrote no instance.json
        backend = launcher.start_or_reuse([port, free_port()], self.home, 5)
        self.addCleanup(backend.stop)
        self.assertTrue(backend.owned)
        self.assertNotEqual(backend.port, port)
        self.assertEqual(launcher.find_verified_instance(self.home), backend.port)

    def test_second_launch_opens_verified_instance_without_new_server(self):
        port = self.serve_lowram()
        launcher.write_instance_file(self.home, port, TOKEN)
        with mock.patch("desktop.launcher.webbrowser.open", return_value=True) as opened, \
                mock.patch("desktop.launcher.start_server", side_effect=AssertionError("must not start")):
            code = launcher.main(["--no-window"])
        self.assertEqual(code, 0)
        opened.assert_called_once_with(f"http://127.0.0.1:{port}/", new=2)
        self.assertIsNotNone(launcher.probe_instance(port), "the running instance must keep running")
        self.assertNotIn("shutdown", self.events)


class RunTests(DesktopTestCase):
    def test_no_window_opens_browser_serves_and_shuts_down_cleanly(self):
        port = free_port()
        seen = {}

        def fake_open(url, new=0):
            seen["url"] = url
            seen["status"] = launcher.probe_instance(port)
            return True

        def fake_wait(stop=None):
            conn = launcher.http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/")
            resp = conn.getresponse()
            seen["index"] = (resp.status, b"<html" in resp.read().lower())
            conn.close()
            seen["instance"] = json.loads((self.home / "instance.json").read_text())

        with mock.patch("desktop.launcher.webbrowser.open", side_effect=fake_open), \
                mock.patch("desktop.launcher.wait_for_stop", side_effect=fake_wait):
            code = launcher.main(["--no-window", "--port", str(port)])
        self.assertEqual(code, 0)
        self.assertEqual(seen["url"], f"http://127.0.0.1:{port}/")
        self.assertIsNotNone(seen["status"])
        self.assertEqual(seen["index"], (200, True))
        self.assertEqual((seen["instance"]["port"], seen["instance"]["pid"]), (port, os.getpid()))
        self.assertFalse((self.home / "instance.json").exists(), "removed on clean shutdown")
        self.assertEqual(self.events, ["startup", "shutdown"])
        self.assertFalse(answers(port), "server socket must be closed after shutdown")
        log_text = (self.home / "logs" / "app.log").read_text(encoding="utf-8")
        self.assertIn("is running at", log_text)
        self.assertIn("LowRAM AI stopped", log_text)

    def test_window_path_loads_url_and_shuts_down_when_closed(self):
        port = free_port()
        calls = []

        class FakeWindow:
            def load_url(self, url):
                calls.append(("load_url", url, launcher.probe_instance(port) is not None))

            def load_html(self, html):
                calls.append(("load_html", html))

        def create_window(title, **kw):
            calls.append(("create", title, kw["width"], kw["height"], kw["min_size"], kw["text_select"],
                          "Starting" in kw["html"]))
            self.api = kw["js_api"]
            return FakeWindow()

        def start(func, **kw):
            calls.append(("start", kw["private_mode"]))
            func()  # the GUI loop runs func in a thread; the "window" closes when it returns

        fake = fake_webview(create_window, start)
        with mock.patch.dict(sys.modules, {"webview": fake}), \
                mock.patch("desktop.launcher.webbrowser.open", side_effect=AssertionError("no browser")):
            code = launcher.main(["--port", str(port)])
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("create", "LowRAM AI", 1280, 820, (900, 600), True, True))
        self.assertIn(("load_url", f"http://127.0.0.1:{port}/", True), calls)
        self.assertIsInstance(self.api, launcher.DesktopApi)
        self.assertIsInstance(self.api._window, FakeWindow)
        self.assertEqual(fake.settings, {"ALLOW_DOWNLOADS": True, "ALLOW_FILE_URLS": False})
        self.assertEqual(self.events, ["startup", "shutdown"])
        self.assertFalse(answers(port))

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sigterm_in_window_mode_stops_server_then_closes_window(self):
        port = free_port()
        order = []
        destroyed = threading.Event()

        class FakeWindow:
            def load_url(self, url):
                order.append("loaded")

            def load_html(self, html):
                order.append("error page")

            def destroy(self):
                order.append("destroy, server " + ("up" if answers(port) else "down"))
                destroyed.set()

        def start(func, **kw):  # a GUI loop that only ends when the window is destroyed
            func()
            os.kill(os.getpid(), signal.SIGTERM)
            if not destroyed.wait(10):
                raise AssertionError("window was not closed after SIGTERM")

        before = signal.getsignal(signal.SIGTERM)
        fake = fake_webview(lambda title, **kw: FakeWindow(), start)
        with mock.patch.dict(sys.modules, {"webview": fake}), \
                mock.patch("desktop.launcher.webbrowser.open", side_effect=AssertionError("no browser")):
            code = launcher.main(["--port", str(port)])
        self.assertEqual(code, 0)
        self.assertEqual(order, ["loaded", "destroy, server down"])
        self.assertEqual(self.events, ["startup", "shutdown"])
        self.assertFalse((self.home / "instance.json").exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_window_failure_shows_error_page(self):
        busy = listening_socket()
        self.addCleanup(busy.close)
        pages = []
        window = types.SimpleNamespace(load_url=lambda url: pages.append(url), load_html=pages.append)
        fake = fake_webview(lambda title, **kw: window, lambda func, **kw: func())
        with mock.patch.dict(sys.modules, {"webview": fake}):
            code = launcher.main(["--port", str(busy.getsockname()[1])])
        self.assertEqual(code, 1)
        self.assertEqual(len(pages), 1)
        self.assertIn("could not start", pages[0])
        self.assertIn("used by another program", pages[0])
        self.assertIn("app.log", pages[0])

    def test_no_native_webview_falls_back_to_browser(self):
        port = free_port()

        def start(func, **kw):
            raise RuntimeError("You must have either QT or GTK")

        fake = fake_webview(lambda title, **kw: types.SimpleNamespace(), start)
        with mock.patch.dict(sys.modules, {"webview": fake}), \
                mock.patch("desktop.launcher.webbrowser.open", return_value=True) as opened, \
                mock.patch("desktop.launcher.wait_for_stop"):
            code = launcher.main(["--port", str(port)])
        self.assertEqual(code, 0)
        opened.assert_called_once_with(f"http://127.0.0.1:{port}/", new=2)
        self.assertEqual(self.events, ["startup", "shutdown"])

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_wait_for_stop_returns_on_sigterm_and_restores_handler(self):
        before = signal.getsignal(signal.SIGTERM)
        timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        self.addCleanup(timer.cancel)
        launcher.wait_for_stop()
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_wait_for_stop_returns_when_event_set(self):
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        launcher.wait_for_stop(stop)
        self.assertTrue(stop.is_set())


class DesktopApiTests(unittest.TestCase):
    def make(self, result):
        api = launcher.DesktopApi(types.SimpleNamespace(FileDialog=types.SimpleNamespace(OPEN=10, FOLDER=20)))
        calls = []

        def create_file_dialog(kind, **kw):
            calls.append((kind, kw))
            return result

        api._window = types.SimpleNamespace(create_file_dialog=create_file_dialog)
        return api, calls

    def test_only_the_two_dialogs_are_exposed_to_javascript(self):
        api, _ = self.make(None)
        # pywebview exposes every attribute whose name does not start with "_".
        self.assertEqual([name for name in dir(api) if not name.startswith("_")], ["pick_file", "pick_folder"])
        with self.assertRaises(AttributeError):
            api.extra = 1  # __slots__: nothing can be added later

    def test_pick_folder(self):
        api, calls = self.make((ABS_DIR,))
        self.assertEqual(api.pick_folder("Choose a models folder"), ABS_DIR)
        self.assertEqual(calls[0][0], 20)
        self.assertFalse(calls[0][1]["allow_multiple"])
        for result in (None, (), ("relative/folder",), (42,)):
            api, _ = self.make(result)
            self.assertIsNone(api.pick_folder("x"), result)

    def test_pick_file_patterns(self):
        model = str(Path(ABS_DIR) / "model.gguf")
        api, calls = self.make([model])
        self.assertEqual(api.pick_file("Import a model", ["*.gguf", "*.bin"]), model)
        self.assertEqual(calls[0], (10, {"directory": str(Path.home()), "allow_multiple": False,
                                         "file_types": ("Supported files (*.gguf;*.bin)",)}))
        api.pick_file("x", ["evil);(*.*", "*.gguf;rm"])
        api.pick_file("x", "not-a-list")
        self.assertEqual([c[1]["file_types"] for c in calls[1:]], [("Supported files (*.gguf)",)] * 2)

    def test_dialog_errors_and_missing_window_give_none(self):
        api = launcher.DesktopApi(types.SimpleNamespace(OPEN_DIALOG=10, FOLDER_DIALOG=20))
        self.assertIsNone(api.pick_folder("x"))

        def broken(kind, **kw):
            raise RuntimeError("no dialog")

        api._window = types.SimpleNamespace(create_file_dialog=broken)
        with self.assertLogs("lowram.desktop", logging.ERROR):
            self.assertIsNone(api.pick_file("x"))


class FrozenPathTests(DesktopTestCase):
    def test_source_layout_matches_bundle_layout(self):
        self.assertFalse(launcher.is_frozen())
        self.assertEqual(launcher.bundle_root(), REPO)
        # The spec copies backend/static to <bundle>/backend/static, the same
        # relative place backend.app derives from its own __file__.
        self.assertEqual(Path(app.STATIC_DIR), launcher.bundled_static_dir())
        self.assertTrue((launcher.bundle_root() / "desktop" / "icon.svg").is_file())

    def test_frozen_root_is_meipass(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(self.tmp), create=True):
            self.assertTrue(launcher.is_frozen())
            self.assertEqual(launcher.bundle_root(), self.tmp)
            self.assertEqual(launcher.bundled_static_dir(), self.tmp / "backend" / "static")

    def test_ensure_static_dir(self):
        fake_app = types.SimpleNamespace(STATIC_DIR=app.STATIC_DIR, STATIC_TYPES=app.STATIC_TYPES, STATIC_FILES={})
        self.assertEqual(launcher.ensure_static_dir(fake_app), Path(app.STATIC_DIR))  # correct: unchanged
        bundled = self.tmp / "backend" / "static"
        bundled.mkdir(parents=True)
        (bundled / "index.html").write_text("<html></html>")
        (bundled / "app.js").write_text("")
        fake_app.STATIC_DIR = self.tmp / "nowhere"
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(self.tmp), create=True), \
                self.assertLogs("lowram.desktop", logging.WARNING):
            self.assertEqual(launcher.ensure_static_dir(fake_app), bundled)
        self.assertEqual(fake_app.STATIC_DIR, bundled)
        self.assertEqual(set(fake_app.STATIC_FILES), {"app.js"})
        fake_app.STATIC_DIR = self.tmp / "nowhere"
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(self.tmp / "empty"), create=True):
            with self.assertRaises(launcher.LauncherError):
                launcher.ensure_static_dir(fake_app)


class LoggingTests(DesktopTestCase):
    def test_rotating_log_file(self):
        handler, path = launcher.setup_logging(self.home)
        self.addCleanup(handler.close)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.assertEqual(path, self.home / "logs" / "app.log")
        self.assertEqual((handler.maxBytes, handler.backupCount), (1024 * 1024, 5))

    def test_access_log_filter(self):
        quiet = launcher.QuietAccessLog()

        def record(line, level=logging.INFO, name="lowram.app"):
            return logging.LogRecord(name, level, __file__, 1, "%s %s", ("127.0.0.1", line), None)

        self.assertFalse(quiet.filter(record('"GET /api/status HTTP/1.1" 200 -')))
        self.assertTrue(quiet.filter(record('"POST /api/settings HTTP/1.1" 200 -')))
        self.assertTrue(quiet.filter(record('"GET /api/x HTTP/1.1" 500 -')))
        self.assertTrue(quiet.filter(record("Loop crashed", logging.ERROR)))

    def test_uncaught_exceptions_are_logged(self):
        restore = launcher.install_excepthooks()
        self.addCleanup(restore)
        with self.assertLogs("lowram.desktop", logging.CRITICAL) as logs:
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        self.assertIn("Uncaught exception", logs.output[0])


if __name__ == "__main__":
    unittest.main()
