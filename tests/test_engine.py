import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import unittest
from unittest import mock

import psutil

from backend import app, config, db, downloads, engine, llm, settings
from tests.engine_helpers import FakeHub, fake_path_env, make_tar_gz, quiet_logger, write_fake_llama_server
from tests.helpers import GIB, LowRAMTestCase, wait_until

POSIX = os.name != "nt"
SMALL = "qwen2.5-1.5b-instruct"
RELEASE_KEY = "api.github.com/repos/ggml-org/llama.cpp/releases/latest"


def gone(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


class EngineTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.hub = FakeHub()
        self.addCleanup(self.hub.close)
        downloads.set_opener(self.hub.opener)
        self.addCleanup(downloads.set_opener, None)
        self.addCleanup(downloads._reset_for_tests)
        quiet_logger(self, "lowram.engine")
        quiet_logger(self, "lowram.downloads")
        self.fake_dir = self.tmp / "fakebin"
        self.fake_dir.mkdir()
        patches = [
            mock.patch.object(engine, "_manager", engine.EngineManager()),
            mock.patch.object(engine, "HOMEBREW_DIRS", (self.tmp / "brew",)),
            mock.patch.object(engine, "HEALTH_POLL_S", 0.05),
            mock.patch.object(engine, "STOP_GRACE_S", 3.0),
            mock.patch.object(downloads, "RETRY_DELAYS_S", (0.01,)),
            mock.patch.dict(os.environ, {"PATH": fake_path_env(self.fake_dir)}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(config.set_runtime_llm_url, None)
        self.addCleanup(lambda: engine._manager.stop())  # runs before the manager patch is undone
        if shutil.which("llama-server"):
            self.skipTest("a real llama-server is on this PATH")

    def managed(self):
        """Behave as if LOWRAM_LLM_URL is not set (the app manages llama-server)."""
        os.environ.pop("LOWRAM_LLM_URL", None)

    def add_fake_runtime(self):
        return write_fake_llama_server(self.fake_dir)

    def add_model(self, model_id=SMALL, data=b"GGUF fake weights"):
        path = engine.model_path(engine.get_entry(model_id))
        path.write_bytes(data)
        return path

    def wait_state(self, *states, timeout=15):
        ok = wait_until(lambda: engine._manager.snapshot()["state"] in states, timeout)
        snap = engine._manager.snapshot()
        self.assertTrue(ok, snap)
        return snap


class CatalogTests(EngineTestCase):
    def test_catalog_entries_are_well_formed(self):
        self.assertTrue(6 <= len(engine.CATALOG) <= 10)
        ids = [e.id for e in engine.CATALOG]
        files = [e.filename for e in engine.CATALOG]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(len(set(files)), len(files))
        self.assertEqual({e.purpose for e in engine.CATALOG}, {"chat", "code"})
        for entry in engine.CATALOG:
            with self.subTest(entry=entry.id):
                self.assertRegex(entry.id, engine.MODEL_ID_RE)
                self.assertTrue(entry.filename.endswith("Q4_K_M.gguf"))
                self.assertEqual(entry.quant, "Q4_K_M")
                self.assertIn(entry.license, ("Apache-2.0", "MIT"))
                self.assertRegex(entry.repo, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+-GGUF$")
                self.assertLess(entry.size_bytes, entry.min_ram_gb * GIB)

    def test_catalog_api_shape_and_installed_flag(self):
        models = engine.catalog()
        keys = {"id", "name", "family", "purpose", "params", "quant", "size_bytes", "min_ram_gb", "recommended",
                "installed", "license", "description", "source", "path", "available", "fits_ram", "default_for"}
        self.assertTrue(all(set(m) == keys for m in models))
        self.assertEqual(sum(m["recommended"] for m in models), 1)
        self.assertFalse(any(m["installed"] for m in models))
        self.add_model(data=b"x" * 10)
        model = next(m for m in engine.catalog() if m["id"] == SMALL)
        self.assertEqual((model["installed"], model["size_bytes"]), (True, 10))

    def test_recommendation_by_ram(self):
        expected = {1: "qwen2.5-0.5b-instruct", 4: "qwen2.5-1.5b-instruct", 8: "qwen3-4b",
                    16: "qwen2.5-7b-instruct", 32: "qwen2.5-14b-instruct", 128: "qwen2.5-14b-instruct"}
        for gib, model_id in expected.items():
            with self.subTest(gib=gib):
                self.assertEqual(engine.recommended_model_id(gib * GIB), model_id)
        sizes = [engine.get_entry(engine.recommended_model_id(g * GIB)).size_bytes for g in range(1, 70)]
        self.assertEqual(sizes, sorted(sizes))
        for g in range(2, 70):
            entry = engine.get_entry(engine.recommended_model_id(g * GIB))
            self.assertEqual(entry.purpose, "chat")
            self.assertLessEqual(entry.size_bytes, g * GIB * 0.6)

    def test_ctx_size(self):
        self.assertEqual([engine.default_ctx_size(g * GIB) for g in (8, 16, 32)], [4096, 8192, 16384])
        self.assertEqual(engine.ctx_size(), 8192)  # LowRAMTestCase fakes 16 GiB
        settings.update("engine", {"ctx_size": 2048})
        self.assertEqual(engine.ctx_size(), 2048)
        settings.update("engine", {"ctx_size": "huge"})
        self.assertEqual(engine.ctx_size(), 8192)

    def test_unknown_model(self):
        for bad in ("nope", "../etc", "", None, 5):
            with self.subTest(bad=bad), self.assertRaises(engine.EngineError) as ctx:
                engine.get_entry(bad)
            self.assertEqual(ctx.exception.status, 404)


class RuntimeDiscoveryTests(EngineTestCase):
    def test_not_installed(self):
        self.assertEqual(engine.runtime_info(), {"installed": False, "path": None, "version": None, "source": None})

    def test_managed_runtime_and_version(self):
        folder = engine.runtime_root() / "b1" / "build" / "bin"
        write_fake_llama_server(folder)
        (engine.runtime_root() / "current.json").write_text(json.dumps({"tag": "b1", "path": "build/bin/llama-server"}))
        info = engine.runtime_info()
        self.assertEqual((info["installed"], info["source"]), (True, "managed"))
        self.assertEqual(info["version"], "4242 (fakebuild)")

    def test_managed_pointer_cannot_escape(self):
        write_fake_llama_server(self.tmp / "evil")
        (engine.runtime_root() / "current.json").write_text(json.dumps({"tag": "b1", "path": "../../../../evil/llama-server"}))
        self.assertEqual(engine.discover_runtime(), (None, None))

    def test_order_path_then_homebrew_then_managed(self):
        write_fake_llama_server(engine.runtime_root() / "b1")
        self.assertEqual(engine.discover_runtime()[1], "managed")
        write_fake_llama_server(self.tmp / "brew")
        self.assertEqual(engine.discover_runtime()[1], "homebrew")
        self.add_fake_runtime()
        self.assertEqual(engine.discover_runtime(), (self.fake_dir / "llama-server", "path"))

    @unittest.skipUnless(POSIX, "shebang scripts")
    def test_version_timeout_is_tolerated(self):
        self.add_fake_runtime()
        with mock.patch.dict(os.environ, {"FAKE_LLAMA_MODE": "hang_version"}), \
                mock.patch.object(engine, "VERSION_TIMEOUT_S", 0.5):
            info = engine.runtime_info()
        self.assertEqual((info["installed"], info["version"]), (True, None))

    def test_platform_keys(self):
        cases = {("darwin", "arm64"): "macos-arm64", ("darwin", "x86_64"): "macos-x64",
                 ("linux", "x86_64"): "linux-x64", ("linux", "aarch64"): "linux-arm64", ("win32", "AMD64"): "windows-x64"}
        for (system, machine), key in cases.items():
            self.assertEqual(engine.platform_key(system, machine), key)
        with self.assertRaises(engine.EngineError):
            engine.platform_key("freebsd", "x86_64")

    def test_select_asset(self):
        names = ["llama-b7000-bin-macos-arm64.zip", "llama-b7000-bin-macos-arm64.tar.gz", "llama-b7000-bin-macos-x64.tar.gz",
                 "llama-b7000-bin-ubuntu-vulkan-x64.tar.gz", "llama-b7000-bin-ubuntu-x64.tar.gz",
                 "llama-b7000-bin-win-cuda-12.4-x64.zip", "llama-b7000-bin-win-cpu-x64.zip", "llama-b7000-xcframework.zip"]
        release = {"tag_name": "b7000", "assets": [{"name": n} for n in names]}
        picks = {key: engine.select_asset(release, key)[1]["name"] for key in ("macos-arm64", "linux-x64", "windows-x64")}
        self.assertEqual(picks, {"macos-arm64": "llama-b7000-bin-macos-arm64.tar.gz",
                                 "linux-x64": "llama-b7000-bin-ubuntu-x64.tar.gz",
                                 "windows-x64": "llama-b7000-bin-win-cpu-x64.zip"})
        with self.assertRaises(engine.EngineError) as ctx:
            engine.select_asset(release, "linux-arm64")
        self.assertIn("no download for this computer", str(ctx.exception))
        with self.assertRaises(engine.EngineError):
            engine.select_asset({"tag_name": "../x", "assets": []}, "linux-x64")


class InstallTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(engine, "platform_key", return_value="linux-x64")
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish_release(self, archive, digest=None, name="llama-b9999-bin-ubuntu-x64.tar.gz"):
        url = f"https://github.com/ggml-org/llama.cpp/releases/download/b9999/{name}"
        cdn = "release-assets.githubusercontent.com/github-production-release-asset/1"
        self.hub.redirects[url[len("https://"):]] = f"https://{cdn}?X-Amz-Signature=secret"
        self.hub.files[cdn] = archive
        digest = digest or "sha256:" + hashlib.sha256(archive).hexdigest()
        assets = [{"name": "llama-b9999-bin-macos-arm64.tar.gz", "browser_download_url": "https://github.com/x", "size": 1},
                  {"name": name, "browser_download_url": url, "size": len(archive), "digest": digest}]
        self.hub.json[RELEASE_KEY] = ({"tag_name": "b9999", "assets": assets}, None)

    @unittest.skipUnless(POSIX, "shebang scripts")
    def test_runtime_install_end_to_end(self):
        script = (self.tmp / "src").joinpath
        write_fake_llama_server(self.tmp / "src")
        archive = make_tar_gz([("dir", "llama-b9999"), ("file", "llama-b9999/llama-server", script("llama-server").read_bytes(), 0o755),
                               ("file", "llama-b9999/libllama.so.0", b"lib"), ("symlink", "llama-b9999/libllama.so", "libllama.so.0")])
        self.publish_release(archive)
        job = engine.install_runtime()
        self.assertEqual((job["id"], job["kind"]), (engine.RUNTIME_JOB_ID, "runtime"))
        job = downloads.wait(job["id"], 15)
        self.assertEqual(job["state"], "done", job)
        info = engine.runtime_info()
        self.assertEqual((info["source"], info["version"]), ("managed", "4242 (fakebuild)"))
        self.assertTrue(info["path"].endswith(os.path.join("b9999", "llama-b9999", "llama-server")))
        with self.assertRaises(engine.EngineError):
            engine.install_runtime()  # same tag already installed

    def test_runtime_digest_mismatch(self):
        self.publish_release(make_tar_gz([("file", "llama-server", b"x", 0o755)]), digest="sha256:" + "0" * 64)
        job = downloads.wait(engine.install_runtime()["id"], 15)
        self.assertEqual(job["state"], "failed")
        self.assertIn("SHA-256", job["error"])
        self.assertEqual(engine.discover_runtime(), (None, None))

    def test_runtime_archive_without_server(self):
        self.publish_release(make_tar_gz([("file", "llama-b9999/README.md", b"x")]))
        job = downloads.wait(engine.install_runtime()["id"], 15)
        self.assertEqual(job["state"], "failed")
        self.assertIn("does not contain", job["error"])
        self.assertFalse((engine.runtime_root() / "b9999").exists())

    def test_runtime_metadata_failures(self):
        with self.assertRaises(engine.EngineError) as ctx:
            engine.install_runtime()  # 404 from the fake GitHub API
        self.assertEqual(ctx.exception.status, 502)
        self.hub.json[RELEASE_KEY] = ({"tag_name": "b1", "assets": [{"name": "llama-b1-bin-win-cpu-x64.zip"}]}, None)
        with self.assertRaises(engine.EngineError) as ctx:
            engine.install_runtime()
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(downloads.list_jobs(), [])

    def publish_model(self, model_id=SMALL, data=b"GGUF" * 1000, lfs=True):
        entry = engine.get_entry(model_id)
        item = {"type": "file", "path": entry.filename, "size": len(data)}
        if lfs:
            item["lfs"] = {"oid": hashlib.sha256(data).hexdigest(), "size": len(data), "pointerSize": 135}
        tree = f"huggingface.co/api/models/{entry.repo}/tree/main"
        self.hub.json[tree] = ([{"type": "file", "path": "README.md", "size": 10}, item], None)
        resolve = f"huggingface.co/{entry.repo}/resolve/main/{entry.filename}"
        self.hub.redirects[resolve] = "https://cas-bridge.xethub.hf.co/xet-bridge/abc?X-Amz-Signature=secret"
        self.hub.files["cas-bridge.xethub.hf.co/xet-bridge/abc"] = data
        return entry, tree

    def test_model_install_end_to_end(self):
        entry, _ = self.publish_model()
        job = engine.install_model(SMALL)
        self.assertEqual((job["id"], job["kind"], job["label"]), (f"model-{SMALL}", "model", entry.name))
        job = downloads.wait(job["id"], 15)
        self.assertEqual(job["state"], "done", job)
        self.assertTrue(engine.is_installed(entry))
        meta = json.loads((config.models_dir() / f"{SMALL}.json").read_text())
        self.assertEqual(meta["size_bytes"], 4000)
        self.assertEqual(settings.get("engine", "model_id"), SMALL)
        with self.assertRaises(engine.EngineError):
            engine.install_model(SMALL)

    def test_model_install_follows_pagination(self):
        entry, tree = self.publish_model()
        page2 = tree + "/page2"
        self.hub.json[page2] = self.hub.json[tree]
        self.hub.json[tree] = ([], f'<https://{page2}>; rel="next"')
        self.assertEqual(downloads.wait(engine.install_model(SMALL)["id"], 15)["state"], "done")

    def test_model_install_requires_verified_metadata(self):
        with self.assertRaises(engine.EngineError) as ctx:
            engine.install_model(SMALL)  # tree API answers 404
        self.assertEqual(ctx.exception.status, 502)
        self.publish_model(lfs=False)
        with self.assertRaises(engine.EngineError) as ctx:
            engine.install_model(SMALL)
        self.assertIn("unverified", str(ctx.exception))
        self.assertEqual(downloads.list_jobs(), [])

    def test_delete_model(self):
        with self.assertRaises(engine.EngineError) as ctx:
            engine.delete_model(SMALL)
        self.assertEqual(ctx.exception.status, 404)
        self.add_model()
        settings.update("engine", {"model_id": SMALL})
        engine.delete_model(SMALL)
        self.assertFalse(engine.is_installed(engine.get_entry(SMALL)))
        self.assertIsNone(settings.get("engine", "model_id"))


@unittest.skipUnless(POSIX, "the fake llama-server is a shebang script")
class ServerTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.managed()
        self.add_fake_runtime()
        self.add_model()

    def test_start_ready_chat_stop(self):
        server = engine.start(SMALL)
        self.assertEqual((server["state"], server["model_id"], server["ctx_size"]), ("starting", SMALL, 8192))
        server = self.wait_state("ready")
        url = f"http://127.0.0.1:{server['port']}/v1/chat/completions"
        self.assertEqual(config.llm_url(), url)
        self.assertEqual(llm.chat([{"role": "user", "content": "hi"}]), "fake llama reply")
        self.assertEqual(settings.get("engine", "model_id"), SMALL)
        self.assertTrue(psutil.pid_exists(server["pid"]))
        self.assertIn(f"port={server['port']}", engine.log_path().read_text())
        self.assertEqual(engine.start(SMALL)["pid"], server["pid"])  # idempotent
        status = engine.status()
        self.assertEqual((status["external"], status["server"]["state"], status["runtime"]["source"]), (False, "ready", "path"))
        stopped = engine.stop()
        self.assertEqual(stopped["state"], "stopped")
        self.assertTrue(wait_until(lambda: gone(server["pid"]), 5))
        self.assertEqual(config.llm_url(), config.DEFAULT_LLM_URL)

    def test_ensure_running_starts_selected_model(self):
        settings.update("engine", {"model_id": SMALL})
        url = engine.ensure_running(timeout=15)
        self.assertEqual(engine._manager.snapshot()["state"], "ready")
        self.assertEqual(engine.ensure_running(timeout=1), url)

    def test_configured_port_and_busy_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            settings.update("engine", {"port": port})
            with self.assertRaises(engine.EngineError) as ctx:
                engine.start(SMALL)
            self.assertIn("in use", str(ctx.exception))
        self.assertEqual(engine.start(SMALL)["port"], port)
        settings.update("engine", {"port": "80"})
        engine.stop()
        with self.assertRaises(engine.EngineError):
            engine.start(SMALL)

    def test_crash_while_loading(self):
        with mock.patch.dict(os.environ, {"FAKE_LLAMA_MODE": "crash"}):
            engine.start(SMALL)
            server = self.wait_state("error")
            self.assertIn("exited with code 3", server["error"])
            self.assertIn("failed to load model", server["error"])
            with self.assertRaises(engine.EngineError):
                engine.ensure_running(timeout=10)

    def test_crash_after_ready_is_detected(self):
        with mock.patch.dict(os.environ, {"FAKE_LLAMA_MODE": "crash_after_ready"}):
            engine.start(SMALL)
            self.wait_state("ready")
            server = self.wait_state("error")
        self.assertIn("GGML_ASSERT", server["error"])
        self.assertEqual(config.llm_url(), config.DEFAULT_LLM_URL)

    def test_load_timeout(self):
        with mock.patch.dict(os.environ, {"FAKE_LLAMA_MODE": "never_ready"}), \
                mock.patch.object(engine, "HEALTH_TIMEOUT_S", 1.0):
            pid = engine.start(SMALL)["pid"]
            server = self.wait_state("error")
        self.assertIn("did not finish loading", server["error"])
        self.assertTrue(wait_until(lambda: gone(pid), 5))

    def test_stop_escalates_to_kill(self):
        with mock.patch.dict(os.environ, {"FAKE_LLAMA_MODE": "ignore_term"}), \
                mock.patch.object(engine, "STOP_GRACE_S", 0.5):
            engine.start(SMALL)
            self.wait_state("ready")
            self.assertEqual(engine.stop()["state"], "stopped")

    def test_pause_for_heavy_job(self):
        engine.start(SMALL)
        first = self.wait_state("ready")
        with engine.pause_for_heavy_job():
            self.assertEqual(engine._manager.snapshot()["state"], "stopped")
            self.assertTrue(engine.status()["paused"])
            with self.assertRaises(engine.EngineError):
                engine.start(SMALL)
        second = self.wait_state("ready")
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertFalse(engine.status()["paused"])

    def test_pause_when_stopped_does_not_start(self):
        with engine.pause_for_heavy_job():
            pass
        self.assertFalse(wait_until(lambda: engine._manager.snapshot()["state"] != "stopped", 0.3))

    def test_preconditions(self):
        with self.assertRaises(engine.EngineError):
            engine.start("qwen2.5-7b-instruct")  # not installed
        os.unlink(self.fake_dir / "llama-server")
        with self.assertRaises(engine.EngineError) as ctx:
            engine.start(SMALL)
        self.assertIn("runtime is not installed", str(ctx.exception))
        engine.delete_model(SMALL)
        with self.assertRaises(engine.EngineError) as ctx:
            engine.start()
        self.assertIn("No model", str(ctx.exception))

    def test_delete_refused_while_loaded(self):
        engine.start(SMALL)
        with self.assertRaises(engine.EngineError) as ctx:
            engine.delete_model(SMALL)
        self.assertEqual(ctx.exception.status, 409)

    def test_startup_hook_auto_start_and_shutdown_hook(self):
        settings.update("engine", {"auto_start": False, "model_id": SMALL})
        engine._on_startup(None)
        self.assertFalse(wait_until(lambda: engine._manager.snapshot()["state"] != "stopped", 0.5))
        settings.update("engine", {"auto_start": True})
        engine._on_startup(None)
        self.wait_state("ready")
        engine._on_shutdown(None)
        self.assertEqual(engine._manager.snapshot()["state"], "stopped")

    def test_orphan_from_crashed_app_is_reaped(self):
        port = engine.choose_port()
        proc = subprocess.Popen([str(self.fake_dir / "llama-server"), "-m", "x", "--host", "127.0.0.1", "--port", str(port)],
                                stdout=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(wait_until(lambda: engine._probe_health(port) is not None, 10))
        record = {"pid": proc.pid, "create_time": psutil.Process(proc.pid).create_time()}
        engine._pidfile().write_text(json.dumps({**record, "owner": db.owner_id()}))
        engine._reap_orphan()  # the owner (this process) is alive: leave it alone
        self.assertIsNone(proc.poll())
        engine._pidfile().write_text(json.dumps({**record, "owner": "otherhost:999999:1.00"}))
        with mock.patch("backend.db._owner_alive", return_value=False):
            engine._reap_orphan()
        self.assertIsNotNone(proc.wait(5))
        self.assertFalse(engine._pidfile().exists())


class ExternalModeTests(EngineTestCase):
    def test_external_server_is_never_started(self):
        self.add_fake_runtime()
        self.add_model()
        with self.assertRaises(engine.EngineError) as ctx:
            engine.start(SMALL)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(engine.ensure_running(timeout=1), self.llm.url)
        with engine.pause_for_heavy_job():
            pass
        status = engine.status()
        self.assertEqual((status["external"], status["server"]["state"], status["llm_url"]), (True, "stopped", self.llm.url))
        engine._on_startup(None)
        self.assertEqual(engine._manager.snapshot()["state"], "stopped")

    def test_llm_url_is_redacted(self):
        os.environ["LOWRAM_LLM_URL"] = "https://user:pw@example.com/v1/chat/completions?key=secret"
        self.assertEqual(engine.status()["llm_url"], "https://example.com/v1/chat/completions")


class EngineRouteTests(EngineTestCase):
    def setUp(self):
        EngineTestCase.setUp(self)  # explicit: OwnModelRouteTests borrows this setUp
        self.server = app.LowRAMServer(0, token="t")
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(5)))

    def api(self, method, path, body=None, token="t"):
        headers = {"Connection": "close"}
        data = None
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-LowRAM-Token": token})
            data = json.dumps(body or {}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_status_and_catalog(self):
        status, data = self.api("GET", "/api/engine/status")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"runtime", "server", "external", "llm_url", "paused"})
        self.assertEqual(set(data["server"]), {"state", "model_id", "port", "pid", "ctx_size", "error", "started_at"})
        self.assertEqual(set(data["runtime"]), {"installed", "path", "version", "source"})
        status, data = self.api("GET", "/api/engine/catalog")
        self.assertEqual((status, data["ram_total_bytes"]), (200, 16 * GIB))
        self.assertEqual(len(data["models"]), len(engine.CATALOG))

    def test_errors_and_csrf(self):
        self.assertEqual(self.api("POST", "/api/engine/stop", token="bad")[0], 403)
        self.assertEqual(self.api("POST", "/api/engine/models/nope/install")[0], 404)
        self.assertEqual(self.api("POST", "/api/engine/models/nope/delete")[0], 404)
        self.assertEqual(self.api("POST", "/api/engine/models/Bad!/install")[0], 404)
        self.assertEqual(self.api("POST", "/api/engine/start", {"model_id": 5})[0], 400)
        status, data = self.api("POST", "/api/engine/start", {})
        self.assertEqual(status, 409)  # external mode (LOWRAM_LLM_URL set by the test base)
        self.assertIn("LOWRAM_LLM_URL", data["error"])
        self.managed()
        self.assertEqual(self.api("POST", "/api/engine/start", {"model_id": "nope"})[0], 404)
        status, data = self.api("POST", "/api/engine/start", {})
        self.assertEqual(status, 409)
        self.assertIn("No model", data["error"])
        self.assertEqual(self.api("POST", "/api/engine/stop"), (200, {"server": engine._manager._idle()}))

    def test_install_routes(self):
        with mock.patch.object(engine, "platform_key", return_value="linux-x64"):
            self.assertEqual(self.api("POST", "/api/engine/runtime/install")[0], 502)
        entry = engine.get_entry(SMALL)
        data = b"GGUF" * 100
        self.hub.json[f"huggingface.co/api/models/{entry.repo}/tree/main"] = (
            [{"type": "file", "path": entry.filename, "lfs": {"oid": hashlib.sha256(data).hexdigest(), "size": len(data)}}], None)
        self.hub.files[f"huggingface.co/{entry.repo}/resolve/main/{entry.filename}"] = data
        status, body = self.api("POST", f"/api/engine/models/{SMALL}/install")
        self.assertEqual(status, 202)
        self.assertEqual(downloads.wait(body["download"]["id"], 15)["state"], "done")
        self.assertEqual([d["id"] for d in self.api("GET", "/api/downloads")[1]["downloads"]], [f"model-{SMALL}"])
        self.assertEqual(self.api("POST", f"/api/engine/models/{SMALL}/install")[0], 409)
        self.assertEqual(self.api("POST", f"/api/engine/models/{SMALL}/delete"), (200, {"deleted": True}))

    @unittest.skipUnless(POSIX, "shebang scripts")
    def test_start_and_stop_routes(self):
        self.managed()
        self.add_fake_runtime()
        self.add_model()
        status, data = self.api("POST", "/api/engine/start", {"model_id": SMALL})
        self.assertEqual((status, data["server"]["state"]), (202, "starting"))
        self.wait_state("ready")
        self.assertEqual(self.api("POST", f"/api/engine/models/{SMALL}/delete")[0], 409)
        status, data = self.api("POST", "/api/engine/stop")
        self.assertEqual((status, data["server"]["state"]), (200, "stopped"))


# --------------------------------------------------------------------------- review round 1


class HardeningTests(EngineTestCase):
    def test_runtime_release_needs_checksum_and_official_url(self):
        with mock.patch.object(engine, "platform_key", return_value="linux-x64"):
            url = "https://github.com/ggml-org/llama.cpp/releases/download/b1/llama-b1-bin-ubuntu-x64.tar.gz"
            asset = {"name": "llama-b1-bin-ubuntu-x64.tar.gz", "browser_download_url": url, "size": 10}
            self.hub.json[RELEASE_KEY] = ({"tag_name": "b1", "assets": [asset]}, None)
            with self.assertRaises(engine.EngineError) as ctx:
                engine.install_runtime()
            self.assertIn("unverified program", str(ctx.exception))
            asset.update(digest="sha256:" + "a" * 64,
                         browser_download_url="https://github.com/evil/llama.cpp/releases/download/b1/x.tar.gz")
            with self.assertRaises(engine.EngineError) as ctx:
                engine.install_runtime()
            self.assertIn("unexpected download address", str(ctx.exception))
        self.assertEqual(downloads.list_jobs(), [])

    def test_staging_dirs_are_ignored_and_status_creates_nothing(self):
        write_fake_llama_server(engine.runtime_root() / ".b1.extract-1234")
        self.assertEqual(engine.discover_runtime(), (None, None))
        shutil.rmtree(engine.runtime_root())
        engine.status()
        self.assertFalse((self.home / "bin" / "llama.cpp").exists())

    def test_child_env_scrubs_secrets_and_restores_frozen_library_path(self):
        binary = engine.runtime_root() / "b1" / "llama-server"
        env = {"PATH": "/bin", "MY_API_KEY": "k", "GH_TOKEN": "t", "DB_PASSWORD": "p", "X_SECRET": "s",
               "LD_LIBRARY_PATH": "/bundle", "LD_LIBRARY_PATH_ORIG": "/orig", "LOWRAM_HOME": str(self.home)}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(engine.sys, "platform", "linux"), \
                mock.patch.object(engine.sys, "frozen", True, create=True):
            child = engine._child_env(binary)
        self.assertEqual(child["PATH"], "/bin")
        self.assertFalse({"MY_API_KEY", "GH_TOKEN", "DB_PASSWORD", "X_SECRET", "LD_LIBRARY_PATH_ORIG"} & set(child))
        self.assertEqual(child["LD_LIBRARY_PATH"], os.pathsep.join([str(binary.parent), "/orig"]))
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/x", "LOWRAM_HOME": str(self.home)}, clear=True), \
                mock.patch.object(engine.sys, "platform", "linux"):
            self.assertEqual(engine._child_env(self.tmp / "llama-server")["LD_LIBRARY_PATH"], "/x")  # not managed


@unittest.skipUnless(POSIX, "the fake llama-server is a shebang script")
class ServerHardeningTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.managed()
        self.add_fake_runtime()
        self.add_model()

    def test_api_key_parallel_and_clean_env(self):
        with mock.patch.dict(os.environ, {"SOME_SECRET": "x", "MY_TOKEN": "y"}):
            engine.start(SMALL)
            server = self.wait_state("ready")
        key = config.llm_api_key()
        self.assertTrue(key and len(key) >= 32)
        self.assertEqual(llm.chat([{"role": "user", "content": "hi"}]), "fake llama reply")
        conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
        conn.request("POST", "/v1/chat/completions", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 401)  # without the key llama-server does not answer
        conn.close()
        logged = engine.log_path().read_text()
        self.assertIn("parallel=1", logged)
        self.assertIn("secret_env=[]", logged)
        self.assertNotIn(key, logged)
        engine.stop()
        self.assertIsNone(config.llm_api_key())
        self.assertEqual(config.llm_url(), config.DEFAULT_LLM_URL)

    def test_nothing_starts_after_shutdown(self):
        engine.start(SMALL)
        self.wait_state("ready")
        with engine.pause_for_heavy_job():
            engine._on_shutdown(None)
        self.assertFalse(wait_until(lambda: engine._manager.snapshot()["state"] != "stopped", 0.5))
        with self.assertRaises(engine.EngineError) as ctx:
            engine.start(SMALL)
        self.assertEqual(ctx.exception.status, 503)
        engine._on_startup(None)
        self.assertFalse(wait_until(lambda: engine._manager.snapshot()["state"] != "stopped", 0.3))


# --------------------------------------------------------------------------- own models and storage

REPO = "someone/Cool-Model-7B-GGUF"
TREE = f"huggingface.co/api/models/{REPO}/tree/main"


def lfs_item(path, size=None, data=None, sha=True):
    size = len(data) if data is not None else size
    item = {"type": "file", "path": path, "size": size}
    if sha:
        item["lfs"] = {"oid": hashlib.sha256(data if data is not None else path.encode()).hexdigest(), "size": size}
    return item


class SourceParsingTests(unittest.TestCase):
    def test_accepted_sources(self):
        cases = {
            "Qwen/Qwen3-8B-GGUF": ("Qwen/Qwen3-8B-GGUF", "main", None),
            "https://huggingface.co/Qwen/Qwen3-8B-GGUF": ("Qwen/Qwen3-8B-GGUF", "main", None),
            "https://huggingface.co/Qwen/Qwen3-8B-GGUF/tree/v1.0": ("Qwen/Qwen3-8B-GGUF", "v1.0", None),
            "https://huggingface.co/a/b/resolve/main/Q8_0/b-Q8_0.gguf?download=true": ("a/b", "main", "Q8_0/b-Q8_0.gguf"),
            "https://huggingface.co/a/b/blob/abc123/b-Q4_K_M.gguf": ("a/b", "abc123", "b-Q4_K_M.gguf"),
        }
        for source, expected in cases.items():
            self.assertEqual(engine.parse_hf_source(source), expected, source)

    def test_refused_sources(self):
        for source in ("", "justaname", "a/b/c", "../x", "datasets/x", "http://huggingface.co/a/b",
                       "https://evil.com/a/b", "https://huggingface.co.evil.com/a/b", "https://u:p@huggingface.co/a/b",
                       "https://huggingface.co/datasets/a/b", "https://huggingface.co/a/b/resolve/main/x.bin",
                       "https://huggingface.co/a/b/resolve/main/../x.gguf", "https://huggingface.co/a/b/raw/main/x.gguf",
                       "https://huggingface.co/a", None, 5, "a/\nb"):
            with self.subTest(source=source), self.assertRaises(engine.EngineError) as ctx:
                engine.parse_hf_source(source)
            self.assertEqual(ctx.exception.status, 400)

    def test_quant_and_params(self):
        for name, quant in {"x-Q4_K_M.gguf": "Q4_K_M", "qwen2.5-7b-instruct-q5_k_m-00001-of-00002.gguf": "Q5_K_M",
                            "m-IQ4_XS.gguf": "IQ4_XS", "m-f16.gguf": "F16", "Model-UD-Q4_K_XL.gguf": "Q4_K_XL",
                            "m.Q8_0.gguf": "Q8_0", "model.gguf": None, "Qwen2-7B-bf16.gguf": "BF16"}.items():
            self.assertEqual(engine.parse_quant(name), quant, name)
        self.assertEqual(engine.parse_params("Qwen2.5-0.5B-Instruct-Q4_K_M.gguf"), "0.5B")
        self.assertEqual(engine.parse_params("Mixtral-8x7B-Q4_K_M.gguf"), "8x7B")
        self.assertEqual(engine.parse_params("Llama-3.2-3B-Q4_K_M.gguf"), "3B")


class ResolveTests(EngineTestCase):
    def publish_tree(self):
        g = 10**9
        page1 = [lfs_item("README.md", 10), lfs_item("Cool-7B-Q2_K.gguf", 3 * g), lfs_item("Cool-7B-Q4_K_M.gguf", 4.4 * g // 1),
                 lfs_item("mmproj-Cool-f16.gguf", g)]
        page2 = [lfs_item("Cool-7B-Q5_K_M.gguf", 5.1 * g // 1), lfs_item("Cool-7B-Q8_0.gguf", 8.1 * g // 1),
                 lfs_item("F16/Cool-7B-F16-00001-of-00002.gguf", 7.6 * g // 1),
                 lfs_item("F16/Cool-7B-F16-00002-of-00002.gguf", 7.6 * g // 1),
                 lfs_item("broken-00001-of-00003.gguf", g)]
        for item in page1 + page2:
            item["size"] = int(item["size"])
            item.get("lfs", {})["size"] = int(item["size"])
        self.hub.json[TREE] = (page1, f'<https://{TREE}/p2>; rel="next"')
        self.hub.json[TREE + "/p2"] = (page2, None)

    def test_resolve_groups_splits_and_recommends(self):
        self.publish_tree()
        result = engine.resolve_custom(REPO)
        self.assertEqual((result["repo"], result["revision"]), (REPO, "main"))
        files = {f["filename"]: f for f in result["files"]}
        self.assertEqual(list(files), ["Cool-7B-Q2_K.gguf", "Cool-7B-Q4_K_M.gguf", "Cool-7B-Q5_K_M.gguf",
                                       "Cool-7B-Q8_0.gguf", "F16/Cool-7B-F16-00001-of-00002.gguf"])
        split = files["F16/Cool-7B-F16-00001-of-00002.gguf"]
        self.assertEqual((split["parts"], split["size_bytes"], split["quant"], split["fits_ram"]), (2, 15_200_000_000, "F16", False))
        self.assertEqual([f["filename"] for f in result["files"] if f["recommended"]], ["Cool-7B-Q5_K_M.gguf"])
        self.assertEqual(files["Cool-7B-Q8_0.gguf"]["fits_ram"], True)  # 16 GiB * 60 % > 8.1 GB

    def test_recommendation_on_small_machine_and_direct_links(self):
        self.publish_tree()
        self.set_memory(total_gib=6)
        result = engine.resolve_custom(f"https://huggingface.co/{REPO}")
        self.assertEqual([f["filename"] for f in result["files"] if f["recommended"]], ["Cool-7B-Q2_K.gguf"])
        one = engine.resolve_custom(f"https://huggingface.co/{REPO}/resolve/main/F16/Cool-7B-F16-00002-of-00002.gguf")
        self.assertEqual([f["filename"] for f in one["files"]], ["F16/Cool-7B-F16-00001-of-00002.gguf"])
        with self.assertRaises(engine.EngineError) as ctx:
            engine.resolve_custom(f"https://huggingface.co/{REPO}/resolve/main/missing.gguf")
        self.assertEqual(ctx.exception.status, 404)

    def test_resolve_errors(self):
        with self.assertRaises(engine.EngineError) as ctx:
            engine.resolve_custom(REPO)  # tree answers 404
        self.assertEqual(ctx.exception.status, 404)
        self.hub.json[TREE] = ([lfs_item("README.md", 10)], None)
        with self.assertRaises(engine.EngineError) as ctx:
            engine.resolve_custom(REPO)
        self.assertIn("no GGUF", str(ctx.exception))


class CustomInstallTests(EngineTestCase):
    def test_install_single_and_split_models(self):
        single, part1, part2 = b"GGUF" + b"a" * 3000, b"GGUF" + b"b" * 2000, b"c" * 1500
        names = ("Cool-7B-Q4_K_M.gguf", "Big/Cool-7B-Q8_0-00001-of-00002.gguf", "Big/Cool-7B-Q8_0-00002-of-00002.gguf")
        self.hub.json[TREE] = ([lfs_item(n, data=d) for n, d in zip(names, (single, part1, part2), strict=True)], None)
        for name, data in zip(names, (single, part1, part2), strict=True):
            self.hub.files[f"huggingface.co/{REPO}/resolve/main/{name}"] = data
        download, model_id = engine.install_custom(REPO, names[0])
        self.assertEqual(downloads.wait(download["id"], 15)["state"], "done")
        model = next(m for m in engine.catalog() if m["id"] == model_id)
        self.assertEqual((model["source"], model["installed"], model["available"], model["quant"]), ("custom", True, True, "Q4_K_M"))
        self.assertEqual(model["path"], str(config.models_dir() / names[0]))
        self.assertEqual(settings.get("engine", "model_id"), model_id)
        with self.assertRaises(engine.EngineError):
            engine.install_custom(REPO, names[0])
        download, split_id = engine.install_custom(REPO, names[1], name="Big one", purpose="code")
        self.assertTrue(download["id"].endswith("-p01"))
        for job in downloads.list_jobs():
            self.assertEqual(downloads.wait(job["id"], 15)["state"], "done", job)
        entry = engine.get_entry(split_id)
        self.assertEqual((entry.name, entry.purpose, entry.files),
                         ("Big one", "code", ("Cool-7B-Q8_0-00001-of-00002.gguf", "Cool-7B-Q8_0-00002-of-00002.gguf")))
        self.assertEqual(engine.model_path(entry).name, "Cool-7B-Q8_0-00001-of-00002.gguf")
        registry = json.loads((self.home / "custom_models.json").read_text())
        self.assertEqual(sorted(m["id"] for m in registry["models"]), sorted([model_id, split_id]))
        engine.delete_model(split_id)
        self.assertFalse((config.models_dir() / entry.files[1]).exists())
        self.assertIsNone(engine.find_entry(split_id))

    def test_install_refuses_unverified_and_collisions(self):
        self.hub.json[TREE] = ([lfs_item("a-Q4_K_M.gguf", 100, sha=False), lfs_item("b-Q4_K_M.gguf", data=b"x")], None)
        with self.assertRaises(engine.EngineError) as ctx:
            engine.install_custom(REPO, "a-Q4_K_M.gguf")
        self.assertIn("unverified", str(ctx.exception))
        (config.models_dir() / "b-Q4_K_M.gguf").write_bytes(b"old")
        with self.assertRaises(engine.EngineError) as ctx:
            engine.install_custom(REPO, "b-Q4_K_M.gguf")
        self.assertIn("already exists", str(ctx.exception))
        for bad in (("../x", "a.gguf"), (REPO, "../a.gguf"), (REPO, "a.bin"), (REPO, None)):
            with self.subTest(bad=bad), self.assertRaises(engine.EngineError) as ctx:
                engine.install_custom(*bad)
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(downloads.list_jobs(), [])


class LocalImportTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.drive = self.tmp / "ExternalSSD" / "models"
        self.drive.mkdir(parents=True)
        self.gguf = self.drive / "My-Model-3B-Q4_K_M.gguf"
        self.gguf.write_bytes(b"GGUF" + b"\0" * 100)

    def test_import_validation(self):
        (self.drive / "fake.gguf").write_bytes(b"NOPE1234")
        (self.drive / "model.bin").write_bytes(b"GGUF1234")
        for bad in (None, "", "relative/x.gguf", str(self.drive / "missing.gguf"), str(self.drive),
                    str(self.drive / "fake.gguf"), str(self.drive / "model.bin")):
            with self.subTest(bad=bad), self.assertRaises(engine.EngineError) as ctx:
                engine.import_local(bad)
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(engine.custom_entries(), [])

    def test_import_unplug_and_unregister(self):
        link = self.tmp / "shortcut.gguf"
        os.symlink(self.gguf, link)
        model = engine.import_local(str(link), name="Mine")
        self.assertEqual((model["source"], model["available"], model["installed"], model["name"]), ("local", True, True, "Mine"))
        self.assertEqual(model["path"], str(self.gguf.resolve()))
        self.assertEqual(engine.import_local(str(self.gguf))["id"], model["id"])  # same file, same entry
        shutil.rmtree(self.tmp / "ExternalSSD")  # unplug the drive
        listed = next(m for m in engine.catalog() if m["id"] == model["id"])
        self.assertEqual((listed["available"], listed["installed"]), (False, True))
        self.managed()
        with self.assertRaises(engine.EngineError) as ctx:
            engine.start(model["id"])
        self.assertIn("drive with this model is not connected", str(ctx.exception))
        self.assertEqual(len(engine.custom_entries()), 1)  # never dropped automatically
        engine.delete_model(model["id"])
        self.assertEqual(engine.custom_entries(), [])

    def test_delete_never_removes_imported_file(self):
        model = engine.import_local(str(self.gguf))
        engine.delete_model(model["id"])
        self.assertTrue(self.gguf.exists())

    @unittest.skipUnless(POSIX, "shebang scripts")
    def test_start_imported_model(self):
        self.managed()
        self.add_fake_runtime()
        model = engine.import_local(str(self.gguf))
        engine.start(model["id"])
        self.wait_state("ready")
        self.assertIn(f"model={self.gguf.resolve()}", engine.log_path().read_text())

    def test_corrupt_registry_is_kept_aside(self):
        (config.home_dir() / "custom_models.json").write_text("{broken")
        self.assertEqual(engine.custom_entries(), [])
        self.assertTrue((self.home / "custom_models.json.corrupt").exists())
        self.assertEqual(len(engine.catalog()), len(engine.CATALOG))


class StorageTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.drive = self.tmp / "SSD"
        self.drive.mkdir()

    def test_default_storage_info(self):
        info = engine.storage_info()
        self.assertEqual((info["custom"], info["available"], info["writable"], info["moving"]), (False, True, True, None))
        self.assertEqual(info["models_dir"], info["default_dir"])
        self.assertGreater(info["total_bytes"], 0)

    def test_choose_folder_and_unplug(self):
        info = engine.set_storage(str(self.drive))
        self.assertEqual((info["models_dir"], info["custom"]), (str(self.drive.resolve()), True))
        self.assertEqual(settings.get("engine", "models_dir"), str(self.drive.resolve()))
        shutil.rmtree(self.drive)  # unplug
        info = engine.storage_info()
        self.assertEqual((info["available"], info["free_bytes"]), (False, None))
        self.assertFalse(any(m["installed"] for m in engine.catalog()))
        with self.assertRaises(engine.EngineError) as ctx:
            engine.install_model(SMALL)
        self.assertIn("not available", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            downloads.start("x", "https://huggingface.co/a/b", self.drive / "m.gguf", label="x", kind="model")
        self.assertIn("external drive not connected", str(ctx.exception))
        self.assertFalse(self.drive.exists())  # never re-created on the boot disk
        self.assertFalse(engine.set_storage(None)["custom"])

    def test_invalid_folders(self):
        (self.drive / "file.txt").write_text("x")
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(engine.__file__)))
        for bad in ("relative", str(self.tmp / "missing"), str(self.drive / "file.txt"), "/", "/usr", "/etc",
                    "/usr/local/models", repo_root, str(self.home), 5):
            with self.subTest(bad=bad), self.assertRaises(engine.EngineError) as ctx:
                engine.set_storage(bad)
            self.assertEqual(ctx.exception.status, 400)
        with mock.patch.dict(os.environ, {"LOWRAM_MODELS_DIR": str(self.drive)}), self.assertRaises(engine.EngineError):
            engine.set_storage(str(self.drive))

    def test_downloads_may_write_into_custom_models_folder(self):
        engine.set_storage(str(self.drive))
        self.hub.files["huggingface.co/a/b/resolve/main/m.gguf"] = b"GGUF data"
        downloads.start("model-x", "https://huggingface.co/a/b/resolve/main/m.gguf", self.drive / "m.gguf",
                        label="x", kind="model")
        self.assertEqual(downloads.wait("model-x", 15)["state"], "done")
        self.assertEqual((self.drive / "m.gguf").read_bytes(), b"GGUF data")
        with self.assertRaises(ValueError):
            downloads.start("y", "https://huggingface.co/a/b", self.tmp / "elsewhere.gguf", label="y", kind="model")

    def put_models(self):
        catalog_file = self.add_model(data=b"GGUF" * 5000)
        (config.models_dir() / f"{SMALL}.json").write_text("{}")
        local = self.tmp / "local.gguf"
        local.write_bytes(b"GGUF local")
        engine.import_local(str(local))
        return catalog_file, local

    def test_move_existing_models(self):
        catalog_file, local = self.put_models()
        info = engine.set_storage(str(self.drive), move_existing=True)
        self.assertEqual(info["moving"]["state"], "copying")
        self.assertTrue(wait_until(lambda: engine.storage_info()["moving"]["state"] != "copying", 15))
        info = engine.storage_info()
        self.assertEqual((info["moving"]["state"], info["moving"]["bytes_done"]), ("done", info["moving"]["bytes_total"]))
        self.assertEqual(info["models_dir"], str(self.drive.resolve()))
        self.assertFalse(catalog_file.exists())
        self.assertEqual((self.drive / catalog_file.name).read_bytes(), b"GGUF" * 5000)
        self.assertTrue((self.drive / f"{SMALL}.json").exists())
        self.assertTrue(local.exists())  # imported files are never moved
        model = next(m for m in engine.catalog() if m["id"] == SMALL)
        self.assertEqual((model["installed"], model["path"]), (True, str(self.drive / catalog_file.name)))

    def test_failed_move_keeps_everything(self):
        catalog_file, _ = self.put_models()
        real_copy = engine._StorageMover._copy
        calls = []

        def flaky(mover, src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("disk full")
            real_copy(mover, src, dst)

        with mock.patch.object(engine._StorageMover, "_copy", flaky):
            engine.set_storage(str(self.drive), move_existing=True)
            self.assertTrue(wait_until(lambda: engine.storage_info()["moving"]["state"] != "copying", 15))
        info = engine.storage_info()
        self.assertEqual(info["moving"]["state"], "failed")
        self.assertIn("disk full", info["moving"]["error"])
        self.assertFalse(info["custom"])  # setting unchanged
        self.assertTrue(catalog_file.exists())
        self.assertEqual(list(self.drive.iterdir()), [])

    def test_move_refused_while_downloading_or_running(self):
        self.hub.files["huggingface.co/a/b/resolve/main/slow.gguf"] = b"x" * 400_000
        self.hub.chunk, self.hub.delay = 8192, 0.05
        downloads.start("model-slow", "https://huggingface.co/a/b/resolve/main/slow.gguf",
                        config.models_dir() / "slow.gguf", label="slow", kind="model")
        with self.assertRaises(engine.EngineError) as ctx:
            engine.set_storage(str(self.drive), move_existing=True)
        self.assertIn("downloads", str(ctx.exception))
        downloads.cancel("model-slow")
        self.add_model()
        with mock.patch.object(engine._manager, "snapshot", return_value={"state": "ready"}), \
                self.assertRaises(engine.EngineError) as ctx:
            engine.set_storage(str(self.drive), move_existing=True)
        self.assertIn("Stop the engine", str(ctx.exception))


class OwnModelRouteTests(EngineTestCase):
    setUp = EngineRouteTests.setUp
    api = EngineRouteTests.api

    def test_custom_and_storage_routes(self):
        data = b"GGUF" * 50
        self.hub.json[TREE] = ([lfs_item("Cool-Q4_K_M.gguf", data=data)], None)
        self.hub.files[f"huggingface.co/{REPO}/resolve/main/Cool-Q4_K_M.gguf"] = data
        status, body = self.api("POST", "/api/engine/custom/resolve", {"source": REPO})
        self.assertEqual((status, body["files"][0]["recommended"]), (200, True))
        self.assertEqual(self.api("POST", "/api/engine/custom/resolve", {"source": "http://x"})[0], 400)
        status, body = self.api("POST", "/api/engine/custom/install", {"repo": REPO, "filename": "Cool-Q4_K_M.gguf"})
        self.assertEqual(status, 202)
        self.assertEqual(downloads.wait(body["download"]["id"], 15)["state"], "done")
        self.assertIn(body["model_id"], [m["id"] for m in self.api("GET", "/api/engine/catalog")[1]["models"]])
        gguf = self.tmp / "x.gguf"
        gguf.write_bytes(b"GGUF....")
        status, body = self.api("POST", "/api/engine/custom/import", {"path": str(gguf)})
        self.assertEqual((status, body["model"]["source"]), (201, "local"))
        self.assertEqual(self.api("POST", "/api/engine/custom/import", {"path": "x.gguf"})[0], 400)
        status, body = self.api("GET", "/api/engine/storage")
        self.assertEqual(set(body), {"models_dir", "default_dir", "custom", "available", "writable", "free_bytes",
                                     "total_bytes", "moving"})
        drive = self.tmp / "drive"
        drive.mkdir()
        status, body = self.api("POST", "/api/engine/storage", {"models_dir": str(drive), "move_existing": False})
        self.assertEqual((status, body["storage"]["custom"]), (202, True))
        self.assertEqual(self.api("POST", "/api/engine/storage", {})[0], 400)
        self.assertEqual(self.api("POST", "/api/engine/storage", {"models_dir": "/"})[0], 400)
        self.assertEqual(self.api("POST", "/api/engine/storage", {"models_dir": None, "move_existing": "yes"})[0], 400)


# --------------------------------------------------------------------------- polish v0.3.1: purposes + self-healing


def tree_item(path, data):
    return {"type": "file", "path": path, "size": len(data),
            "lfs": {"oid": hashlib.sha256(data).hexdigest(), "size": len(data)}}


class PurposeTests(EngineTestCase):
    def test_default_for_hints(self):
        allowed = {"chat", "code", "general"}
        for entry in engine.CATALOG:
            self.assertTrue(entry.default_for, entry.id)
            self.assertTrue(set(entry.default_for) <= allowed, entry.id)
            if entry.purpose == "code":
                self.assertIn("code", entry.default_for)
        self.assertEqual(engine.catalog()[0]["default_for"], list(engine.CATALOG[0].default_for))

    def test_code_recommendation(self):
        for gib, model_id in ((4, "qwen2.5-coder-1.5b-instruct"), (16, "qwen2.5-coder-7b-instruct"),
                              (32, "qwen2.5-coder-14b-instruct"), (1, "qwen2.5-coder-1.5b-instruct")):
            for purpose in ("code", "coding"):
                self.assertEqual(engine.recommended_model_id(gib * GIB, purpose), model_id)
        self.assertEqual(engine.recommended_model_id(16 * GIB, "chat"), engine.recommended_model_id(16 * GIB))
        self.assertEqual(engine.get_entry(engine.recommended_model_id(16 * GIB, None)).purpose, "chat")
        rec = [m["id"] for m in engine.catalog("code") if m["recommended"]]
        self.assertEqual(len(rec), 1)
        self.assertEqual(engine.get_entry(rec[0]).purpose, "code")


class SelfHealingInstallTests(EngineTestCase):
    setUp = EngineRouteTests.setUp
    api = EngineRouteTests.api

    def tree(self, entry, items):
        self.hub.json[f"huggingface.co/api/models/{entry.repo}/tree/main"] = (items, None)

    def test_missing_file_suggests_q4_k_m(self):
        entry = engine.get_entry(SMALL)
        good, other = b"GGUF" * 300, b"GGUF" * 400
        self.tree(entry, [
            {"type": "file", "path": "README.md", "size": 5},
            tree_item("Qwen2.5-1.5B-Instruct-Q8_0.gguf", other),
            tree_item("qwen2.5-1.5b-instruct-q4_k_m.gguf", good),
            tree_item("Qwen2.5-1.5B-Instruct-Q4_K_M-00001-of-00002.gguf", good),
        ])
        with self.assertRaises(engine.ModelFileMissing) as ctx:
            engine.install_model(SMALL)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.suggestion, {"filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "size_bytes": 1200})
        status, body = self.api("POST", f"/api/engine/models/{SMALL}/install", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["suggestion"]["filename"], "qwen2.5-1.5b-instruct-q4_k_m.gguf")
        self.assertIn("error", body)
        self.assertEqual(downloads.list_jobs(), [])
        # one-click retry with the suggested file: verified and stored under the catalog name
        self.hub.files[f"huggingface.co/{entry.repo}/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf"] = good
        status, body = self.api("POST", f"/api/engine/models/{SMALL}/install",
                                {"filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf"})
        self.assertEqual(status, 202, body)
        self.assertEqual(downloads.wait(body["download"]["id"], 15)["state"], "done")
        self.assertTrue(engine.is_installed(entry))
        self.assertEqual(engine.model_path(entry).read_bytes(), good)
        meta = json.loads((config.models_dir() / f"{SMALL}.json").read_text())
        self.assertEqual(meta["filename"], "qwen2.5-1.5b-instruct-q4_k_m.gguf")

    def test_suggestion_falls_back_to_closest_quant_that_fits(self):
        entry = engine.get_entry(SMALL)
        items = [tree_item("m-Q8_0.gguf", b"a" * 50), tree_item("m-Q5_K_M.gguf", b"b" * 40),
                 tree_item("m-Q4_K_M.gguf", b"c" * 900)]
        self.assertEqual(engine.suggest_file(entry, items, total_bytes=100)["filename"], "m-Q5_K_M.gguf")
        self.assertIsNone(engine.suggest_file(entry, items, total_bytes=10))
        unverified = [{"type": "file", "path": "m-Q4_K_M.gguf", "size": 5}]
        self.assertIsNone(engine.suggest_file(entry, unverified))
        self.tree(entry, unverified)
        with self.assertRaises(engine.EngineError) as ctx:
            engine.install_model(SMALL)
        self.assertEqual(ctx.exception.status, 502)
        self.assertNotIsInstance(ctx.exception, engine.ModelFileMissing)

    def test_filename_must_exist_and_be_safe(self):
        entry = engine.get_entry(SMALL)
        self.tree(entry, [tree_item("ok-Q4_K_M.gguf", b"x" * 10)])
        for bad in ("../x.gguf", "/abs.gguf", "x.bin", 5, "a-00001-of-00002.gguf", "missing-Q4_K_M.gguf"):
            status, body = self.api("POST", f"/api/engine/models/{SMALL}/install", {"filename": bad})
            self.assertEqual(status, 400, (bad, body))
        self.assertEqual(downloads.list_jobs(), [])

    def test_catalog_route_recommends_per_purpose(self):
        status, body = self.api("GET", "/api/engine/catalog?purpose=code")
        self.assertEqual(status, 200)
        self.assertEqual(engine.get_entry(body["recommended"]["code"]).purpose, "code")
        self.assertEqual(engine.get_entry(body["recommended"]["chat"]).purpose, "chat")
        rec = [m for m in body["models"] if m["recommended"]]
        self.assertEqual([m["id"] for m in rec], [body["recommended"]["code"]])


if __name__ == "__main__":
    unittest.main()
