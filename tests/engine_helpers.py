"""Fixtures for engine/download tests: a fake HTTPS "hub" and a fake llama-server."""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from backend import downloads


def quiet_logger(testcase, name: str) -> None:
    """Keep expected warnings out of the test output (assertLogs still sees them)."""
    import logging
    from unittest import mock

    logger = logging.getLogger(name)
    for patcher in (mock.patch.object(logger, "propagate", False), mock.patch.object(logger, "handlers", [logging.NullHandler()])):
        patcher.start()
        testcase.addCleanup(patcher.stop)


class _HubServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # silence client disconnects
        pass


class _HubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        hub: FakeHub = self.server.hub  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        host = self.headers.get("X-Fake-Host", "")
        key = host + parsed.path
        with hub.lock:
            hub.requests.append({"key": key, "query": parsed.query, "range": self.headers.get("Range")})
            status = hub.status.get(key)
            redirect = hub.redirects.get(key)
            doc = hub.json.get(key)
            data = hub.files.get(key)
            drops = hub.drops.get(key)
            drop_after = drops.pop(0) if drops else None
        if status:
            return self._send(status, b"error")
        if redirect:
            self.send_response(302)
            self.send_header("Location", redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if doc is not None:
            body, link = doc
            return self._send(200, json.dumps(body).encode(), {"Link": link} if link else None)
        if data is None:
            return self._send(404, b"not found")
        start = 0
        rng = self.headers.get("Range")
        if rng and not hub.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                return self._send(416, b"")
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        sent, pos = 0, start
        try:
            while pos < len(data):
                chunk = data[pos : pos + hub.chunk]
                if drop_after is not None and sent + len(chunk) > drop_after:
                    self.wfile.write(chunk[: max(0, drop_after - sent)])
                    self.wfile.flush()
                    self.close_connection = True
                    return None
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
                pos += len(chunk)
                if hub.delay:
                    time.sleep(hub.delay)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def _send(self, status, body, headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeHub:
    """Local plain-HTTP server that pretends to be every allow-listed HTTPS host.

    Keys are ``host + path`` (e.g. ``"huggingface.co/a/b"``). :meth:`opener` is
    installed with :func:`downloads.set_opener`; the download manager still
    applies its https/allow-list policy before the opener sees a URL.
    """

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.json: dict[str, tuple[object, str | None]] = {}
        self.redirects: dict[str, str] = {}
        self.status: dict[str, int] = {}
        self.drops: dict[str, list[int]] = {}
        self.requests: list[dict] = []
        self.opened: list[str] = []
        self.ignore_range = False
        self.chunk = 64 * 1024
        self.delay = 0.0
        self.lock = threading.Lock()
        self.server = _HubServer(("127.0.0.1", 0), _HubHandler)
        self.server.hub = self  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self._director = urllib.request.build_opener(urllib.request.ProxyHandler({}), downloads.NoRedirect)

    def opener(self, request, timeout):
        parsed = urlparse(request.full_url)
        assert parsed.scheme == "https", request.full_url  # the policy must have refused anything else
        with self.lock:
            self.opened.append(request.full_url)
        local = urlunparse(("http", f"127.0.0.1:{self.port}", parsed.path, "", parsed.query, ""))
        headers = dict(request.header_items())
        headers["X-Fake-Host"] = parsed.hostname
        return self._director.open(urllib.request.Request(local, headers=headers), timeout=timeout)

    def ranges(self, key):
        return [r["range"] for r in self.requests if r["key"] == key]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


# --------------------------------------------------------------------------- archives


def make_tar_gz(entries: list[tuple]) -> bytes:
    """entries: ("file", name, bytes, mode) | ("dir", name) | ("symlink", name, target) | ("hardlink", name, target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for entry in entries:
            info = tarfile.TarInfo(entry[1])
            if entry[0] == "file":
                info.size, info.mode = len(entry[2]), entry[3] if len(entry) > 3 else 0o644
                tf.addfile(info, io.BytesIO(entry[2]))
                continue
            if entry[0] == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
            elif entry[0] == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, entry[2]
            elif entry[0] == "hardlink":
                info.type, info.linkname = tarfile.LNKTYPE, entry[2]
            elif entry[0] == "fifo":
                info.type = tarfile.FIFOTYPE
            tf.addfile(info)
    return buf.getvalue()


def make_zip(entries: list[tuple]) -> bytes:
    """entries: ("file", name, bytes, mode) | ("symlink", name, target)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for entry in entries:
            info = zipfile.ZipInfo(entry[1])
            info.create_system = 3
            if entry[0] == "symlink":
                info.external_attr = (0o120777 << 16)
                zf.writestr(info, entry[2])
            else:
                info.external_attr = ((0o100000 | (entry[3] if len(entry) > 3 else 0o644)) << 16)
                zf.writestr(info, entry[2])
    return buf.getvalue()


# --------------------------------------------------------------------------- fake llama-server

FAKE_LLAMA_SERVER = r'''#!/usr/bin/env python3
import json, os, signal, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
mode = os.environ.get("FAKE_LLAMA_MODE", "ok")
if "--version" in args:
    if mode == "hang_version":
        time.sleep(60)
    print("version: 4242 (fakebuild)")
    print("built with fake compiler for testing", file=sys.stderr)
    sys.exit(0)

def opt(name):
    return args[args.index(name) + 1] if name in args else None

host, port = opt("--host"), int(opt("--port"))
print(f"fake llama-server model={opt('-m')} host={host} port={port} ctx={opt('-c')}", flush=True)
if mode == "crash":
    print("error: failed to load model: fake corrupt file", flush=True)
    sys.exit(3)
if mode == "ignore_term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
loading = int(os.environ.get("FAKE_LLAMA_LOADING_POLLS", "2"))
state = {"health": 0}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            state["health"] += 1
            if mode == "never_ready" or state["health"] <= loading:
                return self._send(503, {"error": {"code": 503, "message": "Loading model"}})
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": "fake llama reply"}}]})

srv = HTTPServer((host, port), H)
if mode == "crash_after_ready":
    def die():
        while state["health"] <= loading:
            time.sleep(0.02)
        time.sleep(0.3)
        print("GGML_ASSERT: fake fatal error", flush=True)
        os._exit(5)
    threading.Thread(target=die, daemon=True).start()
srv.serve_forever()
'''


def write_fake_llama_server(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "llama-server"
    path.write_text(FAKE_LLAMA_SERVER, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def fake_path_env(fake_dir: Path) -> str:
    """PATH with the fake binary first and a python3 for its shebang."""
    parts = [str(fake_dir), os.path.dirname(sys.executable), "/usr/bin", "/bin"]
    return os.pathsep.join(parts)
