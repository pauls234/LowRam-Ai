"""Fixtures for the media tests: fake stable-diffusion.cpp / ffmpeg executables and
fake ``backend.engine`` / ``backend.downloads`` modules (built in parallel elsewhere)."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import sys
import threading
import types
from pathlib import Path
from unittest import mock

from backend import app, config, media
from tests.helpers import LowRAMTestCase, wait_until

TOKEN = "t"

FAKE_SD = r'''#!/usr/bin/env python3
"""Fake stable-diffusion.cpp: progress output like the real one, writes -o.

Modes (env FAKE_SD_MODE or a keyword in the prompt): FAIL, HANG, SLOW, NOFILE, AVI.
"""
import json, os, struct, sys, time, zlib

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("stable-diffusion.cpp version master-999-fake, commit abc1234def")
    sys.exit(0)
if args[:1] == ["-h"]:
    print("usage: sd-cli [options]\n  --sampling-method {euler, euler_a}")
    sys.exit(0)

def val(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default

def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")

if os.environ.get("FAKE_SD_ARGV_LOG"):
    with open(os.environ["FAKE_SD_ARGV_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
if os.environ.get("FAKE_SD_PID_FILE"):
    with open(os.environ["FAKE_SD_PID_FILE"], "w") as fh:
        fh.write(str(os.getpid()))
prompt = val("-p", "")
mode = os.environ.get("FAKE_SD_MODE", "").upper()
for word in ("FAIL", "HANG", "SLOW", "NOFILE", "AVI"):
    if word in prompt:
        mode = word
steps = int(val("--steps", "4"))
out = val("-o", "output.png")
w = sys.stdout.write
w("[INFO ] stable-diffusion.cpp:200 - loading model\n")
for i in range(1, 5):
    w("\r  |%s| %d/4 - 120.00MB/s\033[K" % ("#" * (i * 12) + " " * (50 - i * 12), i))
    sys.stdout.flush()
w("\n")
if mode == "FAIL":
    print("[ERROR] model.cpp:1234 - load from file failed: bad magic", flush=True)
    sys.exit(3)
for i in range(1, steps + 1):
    if mode == "HANG" and i == 2:
        time.sleep(600)
    if mode == "SLOW":
        time.sleep(0.15)
    w("\r  |%s>| %d/%d - 1.50s/it - ETA 00:%02d\033[K" % ("=" * (49 * i // steps), i, steps, steps - i))
    sys.stdout.flush()
w("\n[INFO ] stable-diffusion.cpp:300 - decoding 1 latents\n")
if mode == "NOFILE":
    sys.exit(0)
base, ext = os.path.splitext(out)
if "vid_gen" in args:
    if int(val("--video-frames", "1")) == 1:
        path, data = base + ".png", png()
    elif mode == "AVI" or ext not in (".webm", ".avi"):  # old builds: MJPEG AVI whatever the name
        path, data = out, b"RIFF" + struct.pack("<I", 120) + b"AVI LIST" + b"\0" * 120
    else:
        path, data = out, b"\x1a\x45\xdf\xa3" + b"\0" * 120
else:
    path, data = out, png()
with open(path, "wb") as fh:
    fh.write(data)
print("[INFO ] main.cpp:508 - save result image 0 to '%s' (success)" % path, flush=True)
'''

FAKE_FFMPEG = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if os.environ.get("FAKE_FFMPEG_LOG"):
    with open(os.environ["FAKE_FFMPEG_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_FFMPEG_FAIL"):
    print("Unknown encoder 'libx264'", file=sys.stderr)
    sys.exit(1)
out = args[-1]
data = b"\x00\x00\x00\x18ftypisom" + b"\0" * 64 if out.endswith(".mp4") else b"\x1a\x45\xdf\xa3" + b"\0" * 64
with open(out, "wb") as fh:
    fh.write(data)
'''


def write_script(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


class FakeEngine(types.ModuleType):
    """Stand-in for backend.engine with a recording pause_for_heavy_job()."""

    def __init__(self) -> None:
        super().__init__("backend.engine")
        self.events: list[str] = []

    @contextlib.contextmanager
    def pause_for_heavy_job(self):
        self.events.append("pause")
        try:
            yield
        finally:
            self.events.append("resume")

    def start(self, *args, **kwargs):  # media must never (re)start the engine itself
        self.events.append("start")

    def ensure_running(self, *args, **kwargs):
        self.events.append("ensure_running")


class FakeDownloads(types.ModuleType):
    """Stand-in for backend.downloads that records start() calls."""

    def __init__(self) -> None:
        super().__init__("backend.downloads")
        self.started: list[dict] = []
        self.jobs: dict[str, dict] = {}

    def start(self, job_id, url, dest, *, sha256=None, size=None, label, kind, extract=None, on_complete=None):
        self.started.append(
            {"id": job_id, "url": url, "dest": Path(dest), "sha256": sha256, "size": size, "label": label,
             "kind": kind, "extract": extract, "on_complete": on_complete}
        )
        job = {"id": job_id, "label": label, "kind": kind, "state": "queued", "bytes_done": 0,
               "bytes_total": size, "error": None, "started_at": None, "finished_at": None}
        self.jobs[job_id] = job
        return dict(job)

    def get(self, job_id):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return dict(self.jobs[job_id])

    def list_jobs(self):
        return list(self.jobs.values())


class MediaTestCase(LowRAMTestCase):
    """Isolated home with a fake sd-cli installed (managed) and fake engine/downloads modules."""

    def setUp(self) -> None:
        super().setUp()
        for name, value in {"SLOT_RETRY_S": 0.1, "IDLE_POLL_S": 0.2, "PROGRESS_WRITE_INTERVAL_S": 0.0,
                            "KILL_GRACE_S": 0.5}.items():
            patcher = mock.patch.object(media, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        media.invalidate_runtime_cache()
        self.addCleanup(media.invalidate_runtime_cache)
        self.engine = FakeEngine()
        self.downloads = FakeDownloads()
        self.inject_module("backend.engine", self.engine)
        self.inject_module("backend.downloads", self.downloads)
        self.ffmpeg_patch = mock.patch.object(media, "find_ffmpeg", return_value=None)
        self.ffmpeg_patch.start()
        self.addCleanup(self.ffmpeg_patch.stop)
        self.sd = write_script(config.bin_dir() / "stable-diffusion.cpp" / "fake" / "sd-cli", FAKE_SD)
        self.argv_log = self.tmp / "argv.jsonl"
        os.environ["FAKE_SD_ARGV_LOG"] = str(self.argv_log)
        os.environ.pop("FAKE_SD_MODE", None)
        self.install_model_files("sd-turbo")
        self.install_model_files("wan2.1-t2v-1.3b")
        self.addCleanup(media.WORKER.stop)  # runs before the environment is restored

    def inject_module(self, name: str, module: types.ModuleType) -> None:
        missing = object()
        old = sys.modules.get(name, missing)
        sys.modules[name] = module

        def restore() -> None:
            if old is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

        self.addCleanup(restore)

    def install_model_files(self, model_id: str) -> None:
        for path in media.model_file_paths(media.CATALOG_BY_ID[model_id]).values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"weights")

    def argv_calls(self) -> list[dict]:
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines()]

    def wait_status(self, job_id: int, statuses=("done", "failed", "cancelled"), timeout: float = 20) -> dict:
        self.assertTrue(
            wait_until(lambda: media.get_job(job_id)["status"] in statuses, timeout),
            f"job {job_id} stuck in {media.get_job(job_id)}",
        )
        return media.get_job(job_id)


class MediaAppTestCase(MediaTestCase):
    """Adds a real LowRAMServer on an ephemeral port."""

    def setUp(self) -> None:
        super().setUp()
        self.server = app.LowRAMServer(0, token=TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method: str, path: str, body=None, *, token=TOKEN):
        headers = {"Connection": "close"}
        data = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            if token is not None:
                headers["X-LowRAM-Token"] = token
            data = json.dumps(body if body is not None else {}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def api(self, method: str, path: str, body=None, **kw):
        status, _, payload = self.request(method, path, body, **kw)
        return status, json.loads(payload)
