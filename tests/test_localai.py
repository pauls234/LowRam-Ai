"""Local AI already on this computer: finding model files of other apps and running AI servers."""

import hashlib
import http.client
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend import app, chat, cluster, config, engine, llm, localai, runner, settings
from tests.helpers import LowRAMTestCase

MIN = 4096  # localai.MIN_MODEL_BYTES during the tests


def gguf(path: Path, size: int = MIN + 10, magic: bytes = b"GGUF") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic + b"\0" * (size - len(magic)))
    return path


def ollama_model(models: Path, name_parts: tuple, data: bytes = b"GGUF" + b"\0" * MIN) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    blob = models / "blobs" / f"sha256-{digest}"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(data)
    manifest = models.joinpath("manifests", *name_parts)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schemaVersion": 2, "layers": [
        {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:" + "1" * 64},
        {"mediaType": "application/vnd.ollama.image.model", "digest": f"sha256:{digest}", "size": len(data)},
    ]}))
    return blob


class LocalAICase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.user = Path(os.environ["HOME"])
        for patcher in (mock.patch.object(localai, "MIN_MODEL_BYTES", MIN),
                        mock.patch.object(localai, "SERVERS", ())):
            patcher.start()
            self.addCleanup(patcher.stop)
        localai._cache.update(at=0.0, found=[], truncated=False, scanned_at=None)
        self.addCleanup(localai._cache.update, at=0.0, found=[], truncated=False, scanned_at=None)

    def found(self, force=True):
        return {(f.source, f.name): f for f in localai.scan(force=force)[0]}


class ScanTests(LocalAICase):
    def test_finds_models_of_other_apps(self):
        ollama = self.user / ".ollama" / "models"
        blob = ollama_model(ollama, ("registry.ollama.ai", "library", "llama3.2", "3b"))
        ollama_model(ollama, ("registry.ollama.ai", "library", "llama3.2", "latest"))  # same blob, other tag
        ollama_model(ollama, ("hf.co", "bartowski", "Qwen2.5-7B-Instruct-GGUF", "Q4_K_M"),
                     b"GGUF" + b"\1" * MIN)
        ollama_model(ollama, ("registry.ollama.ai", "library", "broken", "latest"), b"NOPE" + b"\0" * MIN)
        manifest = ollama / "manifests" / "registry.ollama.ai" / "library" / "gone" / "latest"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"layers": [
            {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:" + "2" * 64}]}))
        (ollama / "manifests" / "registry.ollama.ai" / "library" / "junk").mkdir()
        (ollama / "manifests" / "registry.ollama.ai" / "library" / "junk" / "latest").write_text("{not json")

        lms = self.user / ".lmstudio" / "models" / "lmstudio-community" / "Qwen2.5-Coder-7B-Instruct-GGUF"
        gguf(lms / "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf")
        gguf(lms / "mmproj-model-f16.gguf")  # vision projector, no model
        gguf(lms / "big-00001-of-00002.gguf")  # split models are left to the file picker
        gguf(lms / "tiny.gguf", size=100)  # too small to be a chat model
        gguf(lms / "fake.gguf", magic=b"ABCD")  # not GGUF inside
        gguf(self.user / ".lmstudio" / "models" / ".trash" / "deleted.gguf")  # dot folders are skipped

        hub = self.user / ".cache" / "huggingface" / "hub" / "models--bartowski--Phi-3.5-mini-GGUF"
        target = gguf(hub / "blobs" / ("a" * 64))
        snap = hub / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "Phi-3.5-mini-instruct-Q4_K_M.gguf").symlink_to(target)

        found = self.found()
        self.assertEqual(set(found), {
            ("Ollama", "llama3.2:3b"), ("Ollama", "bartowski/Qwen2.5-7B-Instruct-GGUF:Q4_K_M"),
            ("LM Studio", "Qwen2.5-Coder-7B-Instruct-Q4_K_M"), ("Hugging Face", "Phi-3.5-mini-instruct-Q4_K_M"),
        })
        self.assertEqual(found[("Ollama", "llama3.2:3b")].path, str(blob.resolve()))
        self.assertEqual(found[("Hugging Face", "Phi-3.5-mini-instruct-Q4_K_M")].path, str(target.resolve()))
        self.assertEqual(found[("LM Studio", "Qwen2.5-Coder-7B-Instruct-Q4_K_M")].purpose, "code")
        self.assertEqual(found[("Ollama", "llama3.2:3b")].purpose, "chat")

    def test_own_models_folder_is_not_listed(self):
        own = self.user / ".lmstudio" / "models" / "lowram"
        gguf(own / "mine-Q4_K_M.gguf")
        with mock.patch.dict(os.environ, {"LOWRAM_MODELS_DIR": str(own)}):
            self.assertEqual(self.found(), {})

    def test_scan_is_bounded_and_cached(self):
        gguf(self.user / ".lmstudio" / "models" / "a" / "one-Q4_K_M.gguf")
        with mock.patch.object(localai, "MAX_ENTRIES", 1):
            found, truncated, _ = localai.scan(force=True)
        self.assertTrue(truncated)
        found, truncated, _ = localai.scan(force=True)
        self.assertEqual((len(found), truncated), (1, False))
        gguf(self.user / ".lmstudio" / "models" / "b" / "two-Q4_K_M.gguf")
        self.assertEqual(len(localai.scan()[0]), 1)  # cached
        self.assertEqual(len(localai.scan(force=True)[0]), 2)

    def test_missing_folders_are_fine(self):
        found, truncated, scanned_at = localai.scan(force=True)
        self.assertEqual((found, truncated), ([], False))
        self.assertTrue(scanned_at)


class LocalServerSettingTests(LowRAMTestCase):
    """Only http://127.0.0.1:<port>/…/chat/completions is ever used, whatever the settings file says."""

    def setUp(self):
        super().setUp()
        os.environ.pop("LOWRAM_LLM_URL", None)

    def choose(self, url, model="llama3.2:3b", name="Ollama"):
        settings.update("engine", {"local_server": {"name": name, "url": url, "model": model}})
        return config.local_server()

    def test_only_local_addresses_are_accepted(self):
        good = "http://127.0.0.1:11434/v1/chat/completions"
        self.assertEqual(self.choose(good), {"name": "Ollama", "url": good, "model": "llama3.2:3b"})
        self.assertEqual(self.choose("http://127.0.0.1:12434/engines/v1/chat/completions")["url"],
                         "http://127.0.0.1:12434/engines/v1/chat/completions")
        for url in ("https://evil.example/v1/chat/completions", "http://localhost:11434/v1/chat/completions",
                    "http://127.0.0.1.evil.example:80/v1/chat/completions", "http://10.0.0.2:11434/v1/chat/completions",
                    "http://127.0.0.1:99999/v1/chat/completions", "http://127.0.0.1:11434/v1/models",
                    "http://127.0.0.1:11434/../chat/completions", "http://user@127.0.0.1:1/chat/completions", 7, None):
            with self.subTest(url=url):
                self.assertIsNone(self.choose(url))
                self.assertNotEqual(config.llm_url(), url)
        for model in ("", "x" * 201, "bad\nmodel", 3):
            with self.subTest(model=model):
                self.assertIsNone(self.choose(good, model=model))

    def test_chosen_server_is_used_without_the_engine_key(self):
        url = "http://127.0.0.1:11434/v1/chat/completions"
        config.set_runtime_llm_url("http://127.0.0.1:5555/v1/chat/completions")
        config.set_runtime_llm_api_key("managed-secret")
        self.addCleanup(config.set_runtime_llm_url, None)
        self.addCleanup(config.set_runtime_llm_api_key, None)
        self.assertEqual(config.llm_api_key(), "managed-secret")
        self.choose(url)
        self.assertEqual((config.llm_url(), config.llm_model()), (url, "llama3.2:3b"))
        self.assertIsNone(config.llm_api_key())  # the managed engine's key never goes to another server
        self.assertTrue(engine.external())
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_URL": "http://127.0.0.1:9/v1/chat/completions"}):
            self.assertIsNone(config.local_server())  # the environment variable wins
        # chat, task queue and devices use the chosen server – they never try to start the built-in engine
        self.assertIsNone(chat._managed_engine())
        self.assertIsNone(runner._managed_engine())
        self.assertEqual(cluster.local_llm_url(), url)
        self.assertEqual(chat.ensure_model(), (url, "llama3.2:3b"))
        settings.update("engine", {"local_server": None})
        self.assertFalse(engine.external())
        self.assertEqual(config.llm_api_key(), "managed-secret")


# --------------------------------------------------------------------------- fake AI servers


class _FakeAI(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self.server.hits.append(("GET", self.path))
        route = self.server.routes.get(self.path)
        if route is None:
            return self._send(404, {"error": "not found"})
        code, body, headers = route if len(route) == 3 else (*route, None)
        return self._send(code, body, headers)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.hits.append(("POST", self.path, body, self.headers.get("Authorization")))
        if self.path.endswith("/chat/completions"):
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": "Hallo von Ollama"},
                                                 "finish_reason": "stop"}]})
        return self._send(404, {})


def fake_server(routes):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAI)
    server.routes, server.hits = routes, []
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return server


class ServerTests(LocalAICase):
    def setUp(self):
        super().setUp()
        os.environ.pop("LOWRAM_LLM_URL", None)
        self.ollama = fake_server({
            "/api/tags": (200, {"models": [{"name": "llama3.2:3b"}, {"name": "nomic-embed-text:latest"},
                                            {"name": "bad name\nx"}]}),
            "/api/ps": (200, {"models": [{"name": "llama3.2:3b", "size": 3 * 1024**3}]}),
        })
        self.lms = fake_server({"/v1/models": (200, {"data": [{"id": "qwen2.5-7b-instruct"}]}),
                                "/api/v0/models": (200, {"data": [{"id": "qwen2.5-7b-instruct", "state": "loaded"}]})})
        self.other = fake_server({"/v1/models": (302, {}, {"Location": f"http://127.0.0.1:{self.ollama.server_port}/x"})})
        for server in (self.ollama, self.lms, self.other):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        patcher = mock.patch.object(localai, "SERVERS", (
            ("Ollama", self.ollama.server_port, "/api/tags", "/v1/chat/completions"),
            ("LM Studio", self.lms.server_port, "/v1/models", "/v1/chat/completions"),
            ("llama.cpp", self.other.server_port, "/v1/models", "/v1/chat/completions"),
            ("Jan", 1, "/v1/models", "/v1/chat/completions"),  # nothing listens there
        ))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = app.LowRAMServer(0, token="t")
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), thread.join(5)))

    def api(self, method, path, body=None):
        headers = {"Connection": "close"}
        data = None
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-LowRAM-Token": "t"})
            data = json.dumps(body or {}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=15)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_running_servers_are_found(self):
        status, data = self.api("GET", "/api/localai")
        self.assertEqual(status, 200, data)
        servers = {s["name"]: s for s in data["servers"]}
        self.assertEqual(set(servers), {"Ollama", "LM Studio"})  # the redirecting one and the silent port are not
        ollama = servers["Ollama"]
        self.assertEqual(ollama["models"], ["llama3.2:3b"])  # embedding models and odd names are left out
        self.assertEqual(ollama["loaded"], [{"name": "llama3.2:3b", "size_bytes": 3 * 1024**3}])
        self.assertEqual(ollama["url"], f"http://127.0.0.1:{self.ollama.server_port}/v1/chat/completions")
        self.assertFalse(ollama["in_use"])
        self.assertEqual(servers["LM Studio"]["loaded"], [{"name": "qwen2.5-7b-instruct", "size_bytes": None}])
        self.assertNotIn(("GET", "/x"), self.ollama.hits)  # redirects are never followed

    def test_chat_through_a_found_server_and_back(self):
        server_id = f"ollama-{self.ollama.server_port}"
        with mock.patch.object(engine, "stop") as stop:
            status, data = self.api("POST", "/api/localai/server", {"id": server_id, "model": "llama3.2:3b"})
        self.assertEqual(status, 200, data)
        stop.assert_called_once()  # the built-in engine does not keep a second model in RAM
        self.assertEqual(data["local_server"]["model"], "llama3.2:3b")
        status, eng = self.api("GET", "/api/engine/status")
        self.assertTrue(eng["external"])
        self.assertEqual(eng["local_server"]["name"], "Ollama")
        self.assertEqual(llm.chat([{"role": "user", "content": "Hi"}]), "Hallo von Ollama")
        post = [hit for hit in self.ollama.hits if hit[0] == "POST"][-1]
        self.assertEqual((post[1], post[2]["model"], post[3]), ("/v1/chat/completions", "llama3.2:3b", None))
        status, data = self.api("GET", "/api/localai")
        self.assertTrue(next(s for s in data["servers"] if s["name"] == "Ollama")["in_use"])
        status, data = self.api("POST", "/api/localai/server", {"id": None})
        self.assertEqual((status, data), (200, {"local_server": None}))
        self.assertFalse(engine.external())

    def test_chat_api_answers_through_the_chosen_server(self):
        """The Studio's chat (SSE) with a chosen local server – found by the browser check: no engine start."""
        server_id = f"ollama-{self.ollama.server_port}"
        with mock.patch.object(engine, "stop"):
            self.assertEqual(self.api("POST", "/api/localai/server", {"id": server_id, "model": "llama3.2:3b"})[0], 200)
        status, data = self.api("POST", "/api/chats", {"title": "Ollama", "mode": "chat"})
        self.assertEqual(status, 201, data)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        try:
            conn.request("POST", f"/api/chats/{data['chat']['id']}/messages", body=json.dumps({"content": "Hi"}),
                         headers={"Content-Type": "application/json", "X-LowRAM-Token": "t", "Connection": "close"})
            resp = conn.getresponse()
            raw = resp.read().decode()
        finally:
            conn.close()
        self.assertEqual(resp.status, 200, raw)
        events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:")]
        self.assertEqual(events[-1]["type"], "done", events)
        self.assertIn("Hallo von Ollama", "".join(e.get("text", "") for e in events if e["type"] == "token"))
        post = [hit for hit in self.ollama.hits if hit[0] == "POST"][-1]
        self.assertEqual((post[1], post[2]["model"]), ("/v1/chat/completions", "llama3.2:3b"))

    def test_server_choice_is_validated(self):
        server_id = f"ollama-{self.ollama.server_port}"
        cases = [({"id": server_id, "model": "missing:1b"}, 400), ({"id": server_id}, 400),
                 ({"id": "jan-1", "model": "x"}, 404), ({"id": "../etc", "model": "x"}, 400),
                 ({"id": 5, "model": "x"}, 400)]
        for body, code in cases:
            with self.subTest(body=body):
                self.assertEqual(self.api("POST", "/api/localai/server", body)[0], code)
        self.assertIsNone(config.local_server())

    def test_use_a_found_file(self):
        blob = ollama_model(self.user / ".ollama" / "models", ("registry.ollama.ai", "library", "qwen2.5-coder", "7b"))
        status, data = self.api("GET", "/api/localai?refresh=1")
        item = next(m for m in data["models"] if m["source"] == "Ollama")
        self.assertEqual((item["name"], item["params"], item["purpose"], item["model_id"]),
                         ("qwen2.5-coder:7b", "7B", "code", None))
        settings.update("engine", {"local_server": {"name": "Ollama", "model": "llama3.2:3b",
                                                    "url": "http://127.0.0.1:11434/v1/chat/completions"}})
        status, data = self.api("POST", "/api/localai/use", {"id": item["id"]})
        self.assertEqual(status, 201, data)
        model = data["model"]
        self.assertEqual((model["family"], model["name"], model["source"], model["purpose"], model["params"]),
                         ("Ollama", "qwen2.5-coder:7b", "local", "code", "7B"))
        self.assertEqual(model["path"], str(blob.resolve()))
        self.assertIsNone(config.local_server())  # a file runs in the built-in engine again
        self.assertEqual(self.api("POST", "/api/localai/use", {"id": item["id"]})[1]["model"]["id"], model["id"])
        status, data = self.api("GET", "/api/localai")
        self.assertEqual(next(m for m in data["models"] if m["id"] == item["id"])["model_id"], model["id"])
        self.assertTrue(any(m["id"] == model["id"] and m["installed"] for m in engine.catalog()))
        # the found file is used in place: nothing copied, nothing changed
        self.assertEqual(blob.read_bytes()[:4], b"GGUF")
        self.assertFalse(any(config.models_dir().glob("*")))
        # a typed path still needs the .gguf ending
        status, data = self.api("POST", "/api/engine/custom/import", {"path": str(blob)})
        self.assertEqual(status, 400)

    def test_use_only_accepts_ids_of_found_files(self):
        for body, code in (({"id": "0" * 16}, 404), ({"id": "/etc/passwd"}, 400), ({}, 400), ({"id": "AB" * 8}, 400)):
            with self.subTest(body=body):
                self.assertEqual(self.api("POST", "/api/localai/use", body)[0], code)


if __name__ == "__main__":
    unittest.main()
