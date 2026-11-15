"""Real-model check of a running LowRAM AI app (the release workflow runs it on a macOS runner).

It talks to the app's HTTP API exactly like the Studio window does: installs the llama.cpp engine
and the smallest catalog model (both downloads verified with SHA-256), resolves a Hugging Face repo
(the "own model" path), starts the model, streams a real chat answer, and writes the llama-server
pid so the workflow can check that quitting the app also ends the model process.

    python desktop/real_model_smoke.py http://127.0.0.1:8798 /path/to/llama.pid
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MODEL_ID = "qwen2.5-0.5b-instruct"  # smallest catalog model (~0.4 GB)
HF_REPO = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
PROMPT = "Antworte mit einem kurzen Satz: Wie heißt die Hauptstadt von Frankreich?"
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the app on 127.0.0.1, never via a proxy


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    log(f"FAILED: {message}")
    sys.exit(1)


class App:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token = ""
        page = self._open("GET", "/", None, 30.0).read().decode("utf-8")
        match = re.search(r'name="lowram-token" content="([^"]+)"', page)
        if not match:
            fail("the served page has no CSRF token")
        self.token = match.group(1)

    def _open(self, method: str, path: str, body: dict | None, timeout: float):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if method != "GET":
            request.add_header("X-LowRAM-Token", self.token)
        try:
            return LOCAL.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:3000].decode("utf-8", "replace")
            fail(f"{method} {path} -> HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, OSError) as exc:
            fail(f"{method} {path}: the app does not answer ({exc})")

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 120.0) -> dict:
        with self._open(method, path, body, timeout) as response:
            return json.loads(response.read() or b"{}")

    def wait_download(self, job: dict, limit_s: float = 1800.0) -> None:
        job_id, last = job["id"], None
        deadline = time.monotonic() + limit_s
        while time.monotonic() < deadline:
            current = next((j for j in self.call("GET", "/api/downloads")["downloads"] if j["id"] == job_id), None)
            if current is None:
                fail(f"download {job_id} disappeared")
            if current["state"] != last:
                last = current["state"]
                size = f" ({current['bytes_total'] / 2**20:.0f} MiB)" if current.get("bytes_total") else ""
                log(f"   {current['label']}: {last}{size}")
            if last == "done":
                return
            if last in ("failed", "cancelled"):
                fail(f"download {job_id} {last}: {current.get('error')}")
            time.sleep(2)
        fail(f"download {job_id} did not finish within {limit_s:g} s")

    def wait_ready(self, limit_s: float = 600.0) -> dict:
        last = None
        deadline = time.monotonic() + limit_s
        while time.monotonic() < deadline:
            server = self.call("GET", "/api/engine/status")["server"]
            if server["state"] != last:
                last = server["state"]
                log(f"   engine: {last}")
            if last == "ready":
                return server
            if last in ("error", "stopped"):
                fail(f"the engine is {last}: {server.get('error')}")
            time.sleep(1)
        fail(f"the engine was not ready within {limit_s:g} s")

    def stream_answer(self, path: str, body: dict, timeout: float = 900.0) -> tuple[str, dict]:
        pieces: list[str] = []
        with self._open("POST", path, body, timeout) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:])
                kind = event.get("type")
                if kind == "token":
                    pieces.append(str(event.get("text") or ""))
                elif kind == "done":
                    return "".join(pieces), event
                elif kind == "error":
                    fail(f"the chat answered with an error: {event.get('error')}")
        fail("the answer stream ended without a 'done' event")


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8798"
    pid_file = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    started = time.monotonic()
    app = App(base)

    log("1. llama.cpp engine (official GitHub release, SHA-256 verified)")
    if not app.call("GET", "/api/engine/status")["runtime"]["installed"]:
        app.wait_download(app.call("POST", "/api/engine/runtime/install", {})["download"])
    runtime = app.call("GET", "/api/engine/status")["runtime"]
    if not runtime["installed"]:
        fail(f"no engine after the download: {runtime}")
    log(f"   installed: {runtime['version']}")

    log(f"2. Own model: resolve {HF_REPO} on Hugging Face")
    files = app.call("POST", "/api/engine/custom/resolve", {"source": HF_REPO})["files"]
    picked = next((f for f in files if f["recommended"]), None)
    if picked is None or not all(f["verified"] for f in files):
        fail(f"unexpected answer for {HF_REPO}: {files}")
    log(f"   {len(files)} GGUF variants, recommended: {picked['filename']} ({picked['size_bytes'] / 2**20:.0f} MiB)")

    log(f"3. Catalog model {MODEL_ID} (SHA-256 verified)")
    models = app.call("GET", "/api/engine/catalog")["models"]
    model = next((m for m in models if m["id"] == MODEL_ID), None)
    if model is None:
        fail(f"{MODEL_ID} is not in the catalog")
    if not model["installed"]:
        app.wait_download(app.call("POST", f"/api/engine/models/{MODEL_ID}/install", {})["download"])

    log("4. Start the model")
    app.call("POST", "/api/engine/start", {"model_id": MODEL_ID})
    server = app.wait_ready()
    log(f"   ready on port {server['port']}, tuning: {json.dumps(server.get('tuning'), sort_keys=True)}")

    log("5. Chat: a real, streamed answer")
    chat = app.call("POST", "/api/chats", {"title": "Echt-Modell-Test", "mode": "chat"})["chat"]
    asked = time.monotonic()
    text, done = app.stream_answer(f"/api/chats/{chat['id']}/messages", {"content": PROMPT})
    if not text.strip():
        fail(f"empty answer: {done}")
    log(f"   answer after {time.monotonic() - asked:.1f} s: {text.strip()[:300]!r}")
    message = done.get("message") or {}
    stats = {k: message[k] for k in ("tokens", "tokens_per_s", "duration_s", "stats") if k in message}
    if stats:
        log(f"   stats: {json.dumps(stats, sort_keys=True)}")
    if "paris" not in text.lower():
        log("   note: the answer does not mention Paris (a 0.5B model may ramble – the engine works)")

    server = app.call("GET", "/api/engine/status")["server"]
    if server["state"] != "ready" or not server.get("pid"):
        fail(f"the engine is not ready after the answer: {server}")
    if pid_file is not None:
        pid_file.write_text(str(server["pid"]))
    log(f"OK – everything worked with a real model in {time.monotonic() - started:.0f} s")


if __name__ == "__main__":
    main()
