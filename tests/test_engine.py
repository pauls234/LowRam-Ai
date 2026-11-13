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
                "installed", "license", "description"}
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
        super().setUp()
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


if __name__ == "__main__":
    unittest.main()
