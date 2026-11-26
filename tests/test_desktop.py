"""Desktop launcher (no GUI): ports, single instance, browser/window paths, signals, JS API, frozen paths."""

import contextlib
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
        self.assertIn("anderen Programm verwendet", str(ctx.exception))
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


class WindowSelfTestTests(unittest.TestCase):
    def test_reports_what_the_page_can_use_and_quits(self):
        import tempfile

        answers = iter([None, '{"speech": false}',
                        '{"speech": true, "secure": true, "mediaDevices": true, "audioWorklet": true}'])

        class Window:
            def evaluate_js(self, script):
                self.script = script
                return next(answers)

        quits = []
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "selftest.json"
            window = Window()
            result = launcher.window_selftest(window, out, lambda: quits.append(1), attempts=10, interval=0.01)
            self.assertEqual(json.loads(out.read_text()), result)
        self.assertTrue(result["mediaDevices"] and result["secure"])
        self.assertEqual(quits, [1])
        self.assertIn("getUserMedia", window.script)  # only reads – nothing is recorded


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
                          "startet" in kw["html"]))
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
        self.assertIn("konnte nicht starten", pages[0])
        self.assertIn("anderen Programm verwendet", pages[0])
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
                                         "file_types": ("Unterstützte Dateien (*.gguf;*.bin)",)}))
        api.pick_file("x", ["evil);(*.*", "*.gguf;rm"])
        api.pick_file("x", "not-a-list")
        self.assertEqual([c[1]["file_types"] for c in calls[1:]], [("Unterstützte Dateien (*.gguf)",)] * 2)

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


class _Hook:
    """pywebview-style event: ``events.closing += handler``."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeTray:
    visible = True  # the icon is confirmed in the menu bar

    def __init__(self, on_open, on_quit, status):
        self.on_open, self.on_quit, self.status = on_open, on_quit, status
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class FakeStarter:
    def __init__(self, error=None):
        self.closed, self.error = 0, error

    def close(self):
        self.closed += 1
        if self.error:
            raise self.error


class _ObjC:
    """Stand-in for a pyobjc object: records method calls and returns configured values."""

    def __init__(self, **returns):
        self.returns, self.calls = returns, []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*args):
            self.calls.append((name, args))
            value = self.returns.get(name)
            return value(*args) if callable(value) else value

        return method


class _FakeNSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


def fake_cocoa(mode="sync", button_window=True, item_gate=None):
    """Fake AppKit/Foundation/PyObjCTools; ``mode``: how callAfter runs (sync, defer, thread)."""
    queue = []

    def call_after(func, *args):
        if mode == "defer":
            queue.append((func, args))
        elif mode == "thread":
            threading.Thread(target=func, args=args, daemon=True).start()
        else:
            func(*args)

    def status_item_with_length(length):
        if item_gate is not None:
            item_gate.wait(5)
        return status_item

    apphelper = types.ModuleType("PyObjCTools.AppHelper")
    apphelper.callAfter = call_after
    package = types.ModuleType("PyObjCTools")
    package.AppHelper = apphelper
    status_item = _ObjC(button=_ObjC(window=_ObjC() if button_window else None), isVisible=True)
    bar = _ObjC(statusItemWithLength_=status_item_with_length)
    quit_item, other_item = _ObjC(action="terminate:"), _ObjC(action="hide:")
    nsapp = _ObjC(mainMenu=_ObjC(itemArray=[_ObjC(submenu=_ObjC(itemArray=[other_item, quit_item]))]))
    appkit = types.ModuleType("AppKit")
    appkit.NSStatusBar = _ObjC(systemStatusBar=bar)
    appkit.NSVariableStatusItemLength = -1
    appkit.NSApplication = _ObjC(sharedApplication=nsapp)
    appkit.NSMenu = _ObjC(alloc=_ObjC(init=_ObjC()))
    appkit.NSMenuItem = _ObjC(alloc=_ObjC(initWithTitle_action_keyEquivalent_=lambda t, a, k: _ObjC(action=a)),
                              separatorItem=_ObjC())
    foundation = types.ModuleType("Foundation")
    foundation.NSObject = _FakeNSObject
    manager = _ObjC()
    foundation.NSAppleEventManager = _ObjC(sharedAppleEventManager=manager)
    modules = {"AppKit": appkit, "Foundation": foundation, "PyObjCTools": package, "PyObjCTools.AppHelper": apphelper}
    return types.SimpleNamespace(modules=modules, queue=queue, bar=bar, item=status_item, quit_item=quit_item,
                                 other_item=other_item, manager=manager)


class TrayTests(DesktopTestCase):
    def test_status_line_is_german_and_tolerant(self):
        from desktop import tray

        engine = types.SimpleNamespace(
            external=lambda: False,
            _manager=types.SimpleNamespace(snapshot=lambda: {"state": "ready", "model_id": "qwen"}))
        media = types.SimpleNamespace(WORKER=types.SimpleNamespace(current_job_id=lambda: 7))
        server = types.SimpleNamespace(loop=types.SimpleNamespace(status=lambda: {"active": True}))
        line = tray.status_line(server, {"backend.engine": engine, "backend.media": media})
        self.assertEqual(line, "Modell: bereit (qwen) · Aufgabe läuft · Bild/Video wird erstellt")
        broken = types.SimpleNamespace(external=lambda: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertEqual(tray.status_line(None, {"backend.engine": broken}), "Keine laufende Aufgabe")
        idle = types.SimpleNamespace(external=lambda: False,
                                     _manager=types.SimpleNamespace(snapshot=lambda: {"state": "stopped"}))
        self.assertEqual(tray.status_line(None, {"backend.engine": idle}), "Modell: aus · Keine laufende Aufgabe")

    def test_create_is_optional(self):
        from desktop import tray

        self.assertIsNone(tray.create(lambda: None, lambda: None, str, platform="linux"))
        self.assertIsNone(tray.create(lambda: None, lambda: None, str, platform="win32"))
        # macOS without pyobjc: logged, no exception
        with mock.patch.dict(sys.modules, {"PyObjCTools": None, "AppKit": None, "Foundation": None}):
            self.assertIsNone(tray.create(lambda: None, lambda: None, str, platform="darwin"))

    def test_tray_refresh_and_quit_off_the_gui_thread(self):
        from desktop import tray

        quit_threads = []
        quit_done = threading.Event()

        def on_quit():
            quit_threads.append(threading.current_thread().name)
            quit_done.set()

        t = tray.Tray(lambda: None, on_quit, lambda: "Modell: bereit", refresh_s=0.01)
        self.assertEqual(t.refresh(), "Modell: bereit")
        t.status = lambda: 1 / 0
        self.assertEqual(t.refresh(), "Status nicht verfügbar")
        t.quit()
        self.assertTrue(quit_done.wait(5))
        self.assertEqual(quit_threads, ["lowram-tray-quit"])
        t.start_refresher()
        t.close()
        t.close()  # idempotent

    def _window(self, calls):
        class FakeWindow:
            events = types.SimpleNamespace(closing=_Hook())

            def load_url(self, url):
                calls.append("loaded")

            def load_html(self, html):
                calls.append("error page")

            def hide(self):
                calls.append("hide")

            def show(self):
                calls.append("show")

            def restore(self):
                calls.append("restore")

            def destroy(self):
                calls.append("destroy")

        return FakeWindow()

    @mock.patch("desktop.launcher.keep_running_in_menu_bar", return_value=True)  # opt-in setting
    def test_close_hides_to_menu_bar_and_quit_shuts_down(self, _keep):
        port = free_port()
        calls, trays = [], []
        window = self._window(calls)

        def factory(**kw):
            trays.append(FakeTray(**kw))
            return trays[-1]

        def start(func, **kw):
            func()
            self.assertIn("Modell", trays[0].status())  # status line reads the in-process server
            (handler,) = window.events.closing.handlers
            self.assertIs(handler(), False)  # closing the window keeps the app running
            for _ in range(100):
                if "hide" in calls:
                    break
                threading.Event().wait(0.01)
            self.assertTrue(answers(port))
            trays[0].on_open()
            trays[0].on_quit()  # "Beenden"
            self.assertFalse(answers(port))
            self.assertIs(handler(), True)  # the final close is not intercepted

        fake = fake_webview(lambda title, **kw: window, start)
        with mock.patch.dict(sys.modules, {"webview": fake}), \
                mock.patch("desktop.launcher.default_tray_factory", factory), \
                mock.patch("desktop.launcher.webbrowser.open", side_effect=AssertionError("no browser")):
            starter = launcher.Starter([port], self.home)
            code = launcher.run_window(starter, self.home / "webview", None, tray_factory=factory)
            starter.close()
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["loaded", "hide", "show", "restore", "destroy"])
        self.assertTrue(trays[0].closed.is_set())
        self.assertEqual(self.events, ["startup", "shutdown"])

    def test_close_quits_when_setting_is_off_or_tray_missing(self):
        port = free_port()
        calls = []
        window = self._window(calls)
        trays = []

        def start(func, **kw):
            func()
            (handler,) = window.events.closing.handlers
            with mock.patch("desktop.launcher.keep_running_in_menu_bar", return_value=False):
                self.assertIs(handler(), True)

        fake = fake_webview(lambda title, **kw: window, start)
        with mock.patch.dict(sys.modules, {"webview": fake}):
            starter = launcher.Starter([port], self.home)
            code = launcher.run_window(starter, self.home / "webview", None,
                                       tray_factory=lambda **kw: trays.append(FakeTray(**kw)) or trays[-1])
            starter.close()
        self.assertEqual(code, 0)
        self.assertTrue(trays[0].closed.is_set())

        # a factory that fails or returns None: no handler, the app works as before
        for factory in (lambda **kw: None, lambda **kw: 1 / 0):
            window = self._window(calls)
            fake = fake_webview(lambda title, **kw: window, lambda func, **kw: func())
            with mock.patch.dict(sys.modules, {"webview": fake}):
                starter = launcher.Starter([free_port()], self.home)
                self.assertEqual(launcher.run_window(starter, self.home / "webview", None, tray_factory=factory), 0)
                starter.close()
            self.assertEqual(window.events.closing.handlers, [])

    def controller(self, calls, starter=None, clock=None):
        c = launcher.WindowController(self._window(calls), starter or FakeStarter(), None,
                                      clock=clock or (lambda: 100.0))
        c.tray = FakeTray(None, None, str)
        return c

    @mock.patch("desktop.launcher.keep_running_in_menu_bar", return_value=True)
    def test_close_while_hidden_or_twice_within_10s_really_quits(self, _keep):
        # Cmd+Q / Dock "Quit" reach pywebview's closing event like a close click; while the
        # window is hidden (or closed again within 10 s) that must quit, cleanly.
        calls, starter = [], FakeStarter()
        c = self.controller(calls, starter)
        tray = c.tray
        self.assertIs(c.on_closing(), False)
        self.assertEqual((calls, c.hidden, starter.closed), (["hide"], True, 0))
        self.assertIs(c.on_closing(), True)
        self.assertEqual((starter.closed, c.quitting), (1, True))
        self.assertTrue(tray.closed.is_set())
        self.assertIsNone(c.tray)

        now = [100.0]
        for later, quits in ((5.0, True), (11.0, False)):
            calls, starter = [], FakeStarter()
            now[0] = 100.0
            c = self.controller(calls, starter, clock=lambda: now[0])
            self.assertIs(c.on_closing(), False)
            c.show()  # reopened from the menu bar or the Dock
            self.assertFalse(c.hidden)
            now[0] += later
            self.assertIs(c.on_closing(), quits)
            self.assertEqual(starter.closed, 1 if quits else 0)
            self.assertEqual(calls, ["hide", "show", "restore"] + ([] if quits else ["hide"]))

    @mock.patch("desktop.launcher.keep_running_in_menu_bar", return_value=True)
    def test_never_hides_without_a_visible_icon(self, _keep):
        calls = []
        c = self.controller(calls)
        c.tray.visible = False  # e.g. the status item has no window (yet)
        self.assertIs(c.on_closing(), True)
        c.tray = None
        self.assertIs(c.on_closing(), True)
        self.assertEqual(calls, [])

        c = self.controller(calls)
        c.window.hide = mock.Mock(side_effect=RuntimeError("gone"))
        self.assertIs(c.on_closing(), True)  # could not hide: close (quit) instead
        self.assertFalse(c.hidden)

    def test_quit_always_removes_icon_and_closes_window(self):
        calls = []
        c = self.controller(calls, FakeStarter(RuntimeError("shutdown failed")))
        tray = c.tray
        with self.assertRaises(RuntimeError):
            c.quit()
        self.assertTrue(tray.closed.is_set())
        self.assertEqual(calls, ["destroy"])
        self.assertIs(c.on_closing(), True)

    def _mac(self, cocoa):
        from desktop import tray

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(sys.modules, cocoa.modules))
        stack.enter_context(mock.patch.object(tray, "_TARGET_CLASS", None))
        self.addCleanup(stack.close)
        return tray

    def test_mac_tray_routes_quit_and_reopen_and_confirms_visibility(self):
        cocoa = fake_cocoa()
        tray = self._mac(cocoa)
        opened, quit_done = threading.Event(), threading.Event()
        t = tray.create(opened.set, quit_done.set, lambda: "Modell: aus", platform="darwin", timeout=2)
        self.assertIsNotNone(t)
        self.addCleanup(t.close)
        self.assertIs(t.visible, True)
        target = t._target
        # Cmd+Q / app menu "Quit" -> clean quit instead of pywebview's closing event
        self.assertEqual(cocoa.quit_item.calls[1:], [("setTarget_", (target,)), ("setAction_", ("quitApp:",))])
        self.assertEqual(cocoa.other_item.calls, [("action", ())])
        self.assertEqual([(c[0], c[1][1:]) for c in cocoa.manager.calls], [
            ("setEventHandler_andSelector_forEventClass_andEventID_",
             ("handleQuit:withReplyEvent:", tray.AE_CORE_CLASS, int.from_bytes(b"quit", "big"))),
            ("setEventHandler_andSelector_forEventClass_andEventID_",
             ("handleReopen:withReplyEvent:", tray.AE_CORE_CLASS, int.from_bytes(b"rapp", "big"))),
        ])
        target.handleReopen_withReplyEvent_(None, None)  # click on the Dock icon
        self.assertTrue(opened.is_set())
        target.handleQuit_withReplyEvent_(None, None)  # Dock "Quit"
        self.assertTrue(quit_done.wait(5))
        t.close()
        self.assertIn(("removeStatusItem_", (cocoa.item,)), cocoa.bar.calls)
        self.assertIs(t.visible, False)

    def test_mac_tray_without_window_is_not_confirmed(self):
        tray = self._mac(fake_cocoa(button_window=False))
        t = tray.create(lambda: None, lambda: None, str, platform="darwin", timeout=2)
        self.addCleanup(t.close)
        self.assertIs(t.visible, False)

    def test_mac_tray_created_too_late_removes_itself(self):
        cocoa = fake_cocoa(mode="defer")  # the main thread never ran the build in time
        tray = self._mac(cocoa)
        self.assertIsNone(tray.create(lambda: None, lambda: None, str, platform="darwin", timeout=0.05))
        func, args = cocoa.queue.pop()
        func(*args)
        self.assertEqual(cocoa.bar.calls, [])  # nothing created after the caller gave up

        gate = threading.Event()  # the build is running while the caller gives up
        cocoa = fake_cocoa(mode="thread", item_gate=gate)
        tray = self._mac(cocoa)
        self.assertIsNone(tray.create(lambda: None, lambda: None, str, platform="darwin", timeout=0.05))
        gate.set()
        for _ in range(500):
            if ("removeStatusItem_", (cocoa.item,)) in cocoa.bar.calls:
                break
            threading.Event().wait(0.01)
        self.assertIn(("removeStatusItem_", (cocoa.item,)), cocoa.bar.calls)
        self.assertEqual(cocoa.manager.calls, [])  # no quit/reopen hooks for an abandoned icon

    def test_keep_running_setting_is_opt_in(self):
        self.assertFalse(launcher.keep_running_in_menu_bar())
        with mock.patch("backend.settings.load", return_value={"desktop": {"keep_running_in_menu_bar": True}}):
            self.assertTrue(launcher.keep_running_in_menu_bar())
        with mock.patch("backend.settings.load", side_effect=OSError("disk")):
            self.assertFalse(launcher.keep_running_in_menu_bar())

    def test_spec_bundles_tray_and_feature_modules(self):
        spec = (REPO / "desktop" / "LowRAM-AI.spec").read_text("utf-8")
        self.assertIn('"desktop.tray"', spec)
        for name in app.FEATURE_MODULES:
            self.assertIn(f'"{name}"', spec)


if __name__ == "__main__":
    unittest.main()
