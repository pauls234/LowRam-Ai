"""Shared fixtures: isolated LOWRAM_HOME, fake memory, fake OpenAI-compatible server."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

GIB = 1024**3
MIB = 1024**2

VM = namedtuple("VM", "total available percent")


def fake_vm(total_gib: float = 16, available_mib: float = 8192) -> VM:
    total = int(total_gib * GIB)
    available = int(available_mib * MIB)
    return VM(total, available, round(100 * (total - available) / total, 1))


# --------------------------------------------------------------------------- scripted replies


class Text:
    def __init__(self, text: str):
        self.text = text


class HttpError:
    def __init__(self, code: int = 500, body: str = "boom"):
        self.code, self.body = code, body


class Raw:
    """Send exactly these bytes with HTTP 200 (e.g. malformed JSON)."""

    def __init__(self, data: bytes):
        self.data = data


class Empty:
    """A well-formed completion with empty content."""


class Redirect:
    """Answer with a 302 pointing at ``location``."""

    def __init__(self, location: str, code: int = 302):
        self.location, self.code = location, code


class EchoAuthError:
    """HTTP 500 whose body echoes the Authorization header (to test secret scrubbing)."""


class Gate:
    """Wait for ``event`` before answering with ``text`` (to keep a request in flight)."""

    def __init__(self, event: threading.Event, text: str = "gated answer"):
        self.event, self.text = event, text


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            payload = None
        fake: FakeLLM = self.server.fake  # type: ignore[attr-defined]
        with fake.lock:
            fake.requests.append(
                {"path": self.path, "headers": dict(self.headers), "json": payload}
            )
            item = fake.script.pop(0) if fake.script else Text(fake.default_text)
        status, data = 200, b""
        if isinstance(item, Gate):
            item.event.wait(15)
            item = Text(item.text)
        if isinstance(item, Text):
            data = json.dumps({"choices": [{"message": {"content": item.text}}]}).encode()
        elif isinstance(item, Empty):
            data = json.dumps({"choices": [{"message": {"content": ""}}]}).encode()
        elif isinstance(item, HttpError):
            status, data = item.code, item.body.encode()
        elif isinstance(item, Raw):
            data = item.data
        elif isinstance(item, EchoAuthError):
            status, data = 500, f"bad credentials: {self.headers.get('Authorization')}".encode()
        extra = {}
        if isinstance(item, Redirect):
            status, extra = item.code, {"Location": item.location}
        self.send_response(status)
        for key, value in extra.items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeLLM:
    """OpenAI-compatible chat endpoint on 127.0.0.1 with a queue of scripted replies."""

    def __init__(self, default_text: str = "default answer"):
        self.default_text = default_text
        self.script: list = []
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.fake = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"

    def push(self, *items) -> None:
        with self.lock:
            self.script.extend(items)

    def user_prompt(self, index: int = -1) -> str:
        return self.requests[index]["json"]["messages"][-1]["content"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def closed_port_url() -> str:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1/chat/completions"


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# --------------------------------------------------------------------------- base class


class LowRAMTestCase(unittest.TestCase):
    """Fresh LOWRAM_HOME + fake LLM + plentiful fake RAM for every test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.llm = FakeLLM()
        self.addCleanup(self.llm.close)
        env = mock.patch.dict(
            os.environ,
            {
                "LOWRAM_HOME": str(self.home),
                "LOWRAM_LLM_URL": self.llm.url,
                "LOWRAM_MIN_FREE_MB": "256",
                "LOWRAM_LLM_TIMEOUT": "10",
                "HOME": str(self.tmp / "fakeuserhome"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        for key in ("LOWRAM_LLM_API_KEY", "LOWRAM_LLM_MODEL", "LOWRAM_PORT"):
            os.environ.pop(key, None)
        self.set_memory()

    def set_memory(self, total_gib: float = 16, available_mib: float = 8192) -> None:
        patcher = mock.patch("backend.memory.psutil.virtual_memory", return_value=fake_vm(total_gib, available_mib))
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_repo(self, files: dict[str, str] | None = None) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        for rel, content in (files or {}).items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return repo

    def snapshot(self, repo: Path) -> dict[str, bytes]:
        return {
            p.relative_to(repo).as_posix(): p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()
        }

    def rmtree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
