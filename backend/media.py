"""Local image and video generation with stable-diffusion.cpp (``sd-cli`` / ``sd``).

Slow, but private and free: jobs wait in a SQLite queue and one background
worker thread runs them one after another. Every job

* holds the global inference slot (``db.inference_slot``) so chat and image
  generation never compete for RAM – while the slot is taken the job politely
  stays ``queued`` with the message "Waiting for the model to finish…";
* optionally pauses the managed llama-server (``settings.media.unload_llm``);
* runs the binary with an argument list (never a shell) in its own process
  group, in a private temp dir, with a finite timeout;
* reports progress parsed from the tool's output and ends as ``done``,
  ``failed`` (with a reason) or ``cancelled``.

All stable-diffusion.cpp flags live in :func:`build_image_args` and
:func:`build_video_args`. They were checked against the upstream sources
(``examples/cli/main.cpp``, ``examples/common/common.cpp``, ``docs/wan.md`` and
``examples/common/media_io.cpp`` on ``master``, November 2026):

* image: ``sd-cli -m MODEL [--vae VAE] [--type T] -p PROMPT [-n NEG] -W W -H H
  --steps N --cfg-scale C --sampling-method S -s SEED [--vae-tiling] -o out.png``
* video (Wan 2.1): ``sd-cli -M vid_gen --diffusion-model DM --vae VAE --t5xxl TE
  -p PROMPT -n NEG -W W -H H --video-frames F --fps R --steps N --cfg-scale C
  --sampling-method euler --flow-shift 3 -s SEED --vae-tiling -o out.webm``

The video container follows the ``-o`` extension (``.webm`` = VP8 when the
build has WebM support); builds without it – and older builds – write an MJPEG
AVI whatever the name, and a 1-frame video is written as PNG. The produced
file is therefore identified by its magic bytes and, when ``ffmpeg`` is
available, converted to H.264 MP4 (``-movflags +faststart``) for playback in
every browser.
"""

from __future__ import annotations

import codecs
import contextlib
import importlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any

import psutil

from . import config, db, power, settings
from .web import FileResponse, HTTPError, Request, Response, on_shutdown, on_startup, route

log = logging.getLogger("lowram.media")

# --------------------------------------------------------------------------- limits

KINDS = ("image", "video")
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")
ACTIVE_DOWNLOAD_STATES = ("queued", "downloading", "verifying", "extracting")
CATALOG_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

MAX_PROMPT_CHARS = 2000
MIN_SIZE = 256
MAX_IMAGE_SIZE = 1536
MAX_VIDEO_SIZE = 832
IMAGE_SIZE_MULTIPLE = 64
# Wan needs multiples of 16 and is trained on 832x480 (480 is not a multiple of 64).
VIDEO_SIZE_MULTIPLE = 16
MAX_STEPS = 150
MAX_FRAMES = 161
MAX_FPS = 60
MAX_CFG_SCALE = 30.0
MAX_SEED = 2**32 - 1
MAX_QUEUED_JOBS = 50
MAX_LISTED_JOBS = 200
MAX_JOB_ID = 2**62

IMAGE_TIMEOUT_S = 30 * 60
VIDEO_TIMEOUT_S = 6 * 3600
TRANSCODE_TIMEOUT_S = 30 * 60
PROBE_TIMEOUT_S = 15.0
SLOT_RETRY_S = 2.0
BATTERY_RETRY_S = 15.0  # how often a job waiting for power re-checks the battery
IDLE_POLL_S = 5.0
PROGRESS_WRITE_INTERVAL_S = 0.5
CANCEL_WAIT_S = 5.0
KILL_GRACE_S = 1.0
METADATA_TIMEOUT_S = 20.0
METADATA_MAX_BYTES = 8 * 1024 * 1024

WAITING_MESSAGE = "Waiting for the model to finish…"
SAMPLERS = frozenset(
    {"euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "dpm++2mv2", "ipndm", "ipndm_v", "lcm",
     "ddim_trailing", "tcd"}
)
WEIGHT_TYPES = frozenset({"f32", "f16", "q8_0", "q5_0", "q5_1", "q4_0", "q4_1", "q4_K", "q3_K", "q2_K"})

OUTPUT_TYPES = {
    "png": "image/png",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "webp": "image/webp",
    "avi": "video/x-msvideo",
}
OUTPUT_NAME_RE = re.compile(r"(\d+)\.(png|mp4|webm|webp|avi)")

# --------------------------------------------------------------------------- runtime constants

RUNTIME_NAMES = ("sd-cli", "sd")  # sd-cli is the current name, sd the older one
RUNTIME_DIR_NAME = "stable-diffusion.cpp"
HOMEBREW_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
RELEASES_URL = "https://api.github.com/repos/leejet/stable-diffusion.cpp/releases/latest"
RELEASE_DOWNLOAD_PREFIX = "https://github.com/leejet/stable-diffusion.cpp/releases/download/"
# Asset names come from the upstream release workflow (.github/workflows/build.yml):
# sd-<branch>-<sha>-bin-<uname -s>-<OS name>-<OS version>-<arch>.zip and sd-…-bin-win-<build>-x64.zip
RUNTIME_ASSET_PATTERNS: dict[tuple[str, str], tuple[str, ...]] = {
    ("darwin", "arm64"): (r"^sd-.+-bin-Darwin-macOS-[0-9.]+-arm64\.zip$",),
    ("linux", "x86_64"): (r"^sd-.+-bin-Linux-Ubuntu-[0-9.]+-x86_64\.zip$",),
    ("windows", "x86_64"): (r"^sd-.+-bin-win-avx2-x64\.zip$", r"^sd-.+-bin-win-cpu-x64\.zip$"),
}
HF_API_MODELS = "https://huggingface.co/api/models/"
HF_BASE = "https://huggingface.co/"
METADATA_HOSTS = frozenset({"api.github.com", "huggingface.co"})
HF_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
HF_PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SECRET_ENV_RE = re.compile(r"(?i)(api[_-]?key|token|secret|passw)")

# --------------------------------------------------------------------------- catalog

GIB = 1024**3

# Official Wan 2.1 negative prompt (from the Wan repository / sd.cpp docs/wan.md).
WAN_NEGATIVE_PROMPT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，"
    "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，"
    "手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "sd-turbo",
        "name": "SD-Turbo",
        "family": "Stable Diffusion 2.1 Turbo",
        "kind": "image",
        "files": [{"role": "model", "repo": "stabilityai/sd-turbo", "filename": "sd_turbo.safetensors"}],
        "size_bytes": 5_210_000_000,
        "min_ram_gb": 6,
        "recommended": True,
        "license": "Stability AI Community License (free for personal and small-business use)",
        "description": (
            "Fastest option: decent 512×512 images in 1–4 steps, often under a minute on a laptop CPU. "
            "Loaded as 16-bit to halve its RAM use. Good first choice for 8 GB machines."
        ),
        "weight_type": "f16",
        "defaults": {"width": 512, "height": 512, "steps": 4, "cfg_scale": 1.0, "sampler": "euler_a"},
    },
    {
        "id": "sd15",
        "name": "Stable Diffusion 1.5",
        "family": "Stable Diffusion 1.5",
        "kind": "image",
        "files": [
            {
                "role": "model",
                "repo": "stable-diffusion-v1-5/stable-diffusion-v1-5",
                "filename": "v1-5-pruned-emaonly-fp16.safetensors",
            }
        ],
        "size_bytes": 2_130_000_000,
        "min_ram_gb": 6,
        "recommended": False,
        "license": "CreativeML OpenRAIL-M",
        "description": (
            "The classic all-rounder (16-bit, 2 GB). Slower than the Turbo models (20+ steps) but flexible "
            "and it honours negative prompts. Best at 512×512."
        ),
        "defaults": {"width": 512, "height": 512, "steps": 20, "cfg_scale": 7.0, "sampler": "euler_a"},
    },
    {
        "id": "sdxl-turbo",
        "name": "SDXL-Turbo",
        "family": "Stable Diffusion XL Turbo",
        "kind": "image",
        "files": [
            {"role": "model", "repo": "stabilityai/sdxl-turbo", "filename": "sd_xl_turbo_1.0_fp16.safetensors"},
            {"role": "vae", "repo": "madebyollin/sdxl-vae-fp16-fix", "filename": "sdxl_vae.safetensors"},
        ],
        "size_bytes": 7_280_000_000,
        "min_ram_gb": 10,
        "recommended": False,
        "license": "Stability AI Community License (model); MIT (VAE fix)",
        "description": (
            "Sharper, more detailed images than SD-Turbo in 1–4 steps at 512×512. Loaded as 8-bit to save "
            "RAM, but still needs about 10 GB."
        ),
        "weight_type": "q8_0",
        "defaults": {"width": 512, "height": 512, "steps": 4, "cfg_scale": 1.0, "sampler": "euler_a"},
    },
    {
        "id": "wan2.1-t2v-1.3b",
        "name": "Wan 2.1 text-to-video 1.3B",
        "family": "Wan 2.1",
        "kind": "video",
        "files": [
            {
                "role": "diffusion_model",
                "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
                "filename": "split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors",
            },
            {
                "role": "vae",
                "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
                "filename": "split_files/vae/wan_2.1_vae.safetensors",
            },
            {"role": "text_encoder", "repo": "city96/umt5-xxl-encoder-gguf", "filename": "umt5-xxl-encoder-Q8_0.gguf"},
        ],
        "size_bytes": 9_140_000_000,
        "min_ram_gb": 16,
        "recommended": True,
        "license": "Apache-2.0",
        "warning": "Very slow, needs ≥16 GB RAM",
        "description": (
            "Short text-to-video clips (2 s at 16 fps by default). Very slow, needs ≥16 GB RAM: on a CPU "
            "one clip can take an hour or more. Downloads about 9 GB (video model, VAE and text encoder)."
        ),
        "frames_rule": "4n+1",
        "default_negative_prompt": WAN_NEGATIVE_PROMPT,
        "defaults": {
            "width": 832, "height": 480, "steps": 20, "cfg_scale": 6.0, "sampler": "euler",
            "frames": 33, "fps": 16, "flow_shift": 3.0,
        },
    },
)
CATALOG_BY_ID = {model["id"]: model for model in CATALOG}
IMAGE_ROLES = {"model", "vae"}
VIDEO_ROLES = {"diffusion_model", "vae", "text_encoder"}


class MediaJobError(RuntimeError):
    """A job failed; the message is shown to the user."""


class MediaSourceError(RuntimeError):
    """GitHub / Hugging Face metadata could not be fetched or is not trustworthy."""


class _Cancelled(Exception):
    pass


class _Stopped(Exception):
    pass


# --------------------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'video')),
    prompt TEXT NOT NULL,
    negative_prompt TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    output_file TEXT,
    output_type TEXT,
    owner TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_jobs_status ON media_jobs(status, id);
"""
db.register_schema(SCHEMA)

MAX_MESSAGE_CHARS = 500
MAX_ERROR_CHARS = 2000


def _duration(started: str | None, finished: str | None) -> float | None:
    if not started:
        return None
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(finished) if finished else datetime.now(timezone.utc)
    except ValueError:
        return None
    return round(max(0.0, (end - start).total_seconds()), 1)


def _job_dict(row: Any) -> dict:
    data = dict(row)
    try:
        params = json.loads(data.pop("params") or "{}")
    except ValueError:
        params = {}
    output_file = data.pop("output_file", None)
    data.pop("owner", None)
    data["params"] = params if isinstance(params, dict) else {}
    data["progress"] = float(data.get("progress") or 0.0)
    done = data["status"] == "done" and bool(output_file)
    data["output_url"] = f"/api/media/jobs/{data['id']}/file" if done else None
    if not done:
        data["output_type"] = None
    data["duration_s"] = _duration(data.get("started_at"), data.get("finished_at"))
    return data


def _check_job_id(job_id: int) -> int:
    job_id = int(job_id)
    if not 0 < job_id <= MAX_JOB_ID:
        raise db.NotFound(f"Media job #{job_id} not found")
    return job_id


def _get_row(conn: Any, job_id: int) -> Any:
    row = conn.execute("SELECT * FROM media_jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise db.NotFound(f"Media job #{job_id} not found")
    return row


def get_job(job_id: int) -> dict:
    job_id = _check_job_id(job_id)
    with db.connect() as conn:
        return _job_dict(_get_row(conn, job_id))


def list_jobs(limit: int = MAX_LISTED_JOBS) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM media_jobs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [_job_dict(row) for row in rows]


def _next_queued_job() -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM media_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
    return _job_dict(row) if row is not None else None


def _running_job_id() -> int | None:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM media_jobs WHERE status = 'running' ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row is not None else None


def _set_queued_message(job_id: int, message: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE media_jobs SET message = ? WHERE id = ? AND status = 'queued' AND message != ?",
            (message, job_id, message),
        )


def _mark_running(job_id: int) -> bool:
    with db.connect() as conn, db.transaction(conn):
        cur = conn.execute(
            "UPDATE media_jobs SET status = 'running', owner = ?, started_at = ?, finished_at = NULL, "
            "progress = 0, message = 'Starting…', error = NULL WHERE id = ? AND status = 'queued'",
            (db.owner_id(), db.utcnow(), job_id),
        )
        return cur.rowcount == 1


def _update_running(job_id: int, progress: float, message: str) -> None:
    progress = max(0.0, min(1.0, float(progress)))
    with db.connect() as conn:
        conn.execute(
            "UPDATE media_jobs SET progress = MAX(progress, ?), message = ? WHERE id = ? AND status = 'running'",
            (progress, message[:MAX_MESSAGE_CHARS], job_id),
        )


def _finish(
    job_id: int,
    status: str,
    *,
    message: str,
    error: str | None = None,
    output_file: str | None = None,
    output_type: str | None = None,
) -> None:
    """Record the outcome of a running job (retries a transiently locked database)."""
    for attempt in range(3):
        try:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE media_jobs SET status = ?, message = ?, error = ?, output_file = ?, output_type = ?, "
                    "progress = CASE WHEN ? = 'done' THEN 1.0 ELSE progress END, finished_at = ?, owner = NULL "
                    "WHERE id = ? AND status = 'running'",
                    (
                        status, message[:MAX_MESSAGE_CHARS], error[:MAX_ERROR_CHARS] if error else None,
                        output_file, output_type, status, db.utcnow(), job_id,
                    ),
                )
            return
        except Exception:  # sqlite3.OperationalError ("database is locked")
            if attempt == 2:
                log.exception("Could not record the outcome of media job #%s", job_id)
                raise
            time.sleep(0.5 * (attempt + 1))


def _owner_alive(owner: str | None) -> bool:
    """Is the process (``host:pid:start-time``) that owns a running job still alive?"""
    if not owner:
        return False
    parts = owner.rsplit(":", 2)
    try:
        pid = int(parts[1])
    except (IndexError, ValueError):
        return False
    try:
        started = psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True
    if len(parts) == 3:
        try:
            return abs(started - float(parts[2])) < 1.0
        except ValueError:
            return False
    return True


def recover_interrupted() -> list[int]:
    """Mark ``running`` jobs nobody is working on as failed and drop their temp dirs."""
    me = db.owner_id()
    current = WORKER.current_job_id()
    recovered: list[int] = []
    with db.connect() as conn, db.transaction(conn):
        for row in conn.execute("SELECT id, owner FROM media_jobs WHERE status = 'running'").fetchall():
            if row["id"] == current:
                continue
            if row["owner"] == me or not _owner_alive(row["owner"]):
                conn.execute(
                    "UPDATE media_jobs SET status = 'failed', message = 'Interrupted', error = ?, finished_at = ?, "
                    "owner = NULL WHERE id = ? AND status = 'running'",
                    (
                        "Interrupted: LowRAM AI stopped while this job was running. Submit it again.",
                        db.utcnow(), row["id"],
                    ),
                )
                recovered.append(int(row["id"]))
    if recovered:
        log.warning("Marked interrupted media job(s) %s as failed", recovered)
    _cleanup_tmp(keep=current)
    return recovered


# --------------------------------------------------------------------------- files and directories


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def media_dir() -> Path:
    """Generated images/videos: ``<LOWRAM_HOME>/media/<job id>.<ext>``."""
    return _private_dir(config.home_dir() / "media")


def media_tmp_dir() -> Path:
    return _private_dir(media_dir() / "tmp")


DRIVE_MISSING_MESSAGE = "The drive with this model is not connected"


def models_available() -> bool:
    """False when the (possibly external) models folder is missing, e.g. an unplugged drive."""
    try:
        return config.models_dir().is_dir()
    except Exception:  # unreadable settings / bad path
        return False


def media_models_dir() -> Path:
    """``<models_dir>/media`` – never created here: the models folder may be an unplugged drive."""
    return config.models_dir() / "media"


def _require_models_dir() -> Path:
    """The media models folder for writing; 409 when the models drive is not connected."""
    if not models_available():
        raise HTTPError(HTTPStatus.CONFLICT, f"{DRIVE_MISSING_MESSAGE} ({config.models_dir()}).")
    return _private_dir(media_models_dir())


def _job_tmp_dir(job_id: int) -> Path:
    path = media_tmp_dir() / f"job_{int(job_id)}"
    if path.is_symlink():
        path.unlink()
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    return _private_dir(path)


def _cleanup_tmp(keep: int | None = None) -> None:
    try:
        entries = list(media_tmp_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        if keep is not None and entry.name == f"job_{keep}":
            continue
        try:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            log.warning("Could not remove leftover media temp entry %s", entry)


def output_path(job_id: int, output_file: str | None) -> Path | None:
    """Where a job's file lives; ``None`` for anything that is not ``<id>.<known ext>``."""
    if not output_file:
        return None
    found = OUTPUT_NAME_RE.fullmatch(output_file)
    if not found or int(found.group(1)) != int(job_id):
        return None
    return media_dir() / output_file


def _store_output(job_id: int, source: Path, ext: str) -> Path:
    if ext not in OUTPUT_TYPES:
        raise MediaJobError(f"Unsupported output type .{ext}")
    dest = media_dir() / f"{int(job_id)}.{ext}"
    if dest.is_symlink():
        dest.unlink()
    os.chmod(source, 0o600)
    os.replace(source, dest)  # same file system: the temp dir lives below media/
    return dest


def sniff_media(path: Path) -> tuple[str, str] | None:
    """Identify an output file by its magic bytes: ``(ext, content type)`` or ``None``."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    ext = None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        ext = "avi"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        ext = "webp"
    elif head[:4] == b"\x1a\x45\xdf\xa3":
        ext = "webm"
    elif head[4:8] == b"ftyp":
        ext = "mp4"
    return (ext, OUTPUT_TYPES[ext]) if ext else None


def _find_output(tmp: Path, kind: str) -> tuple[Path, str, str] | None:
    names = ("output.png",) if kind == "image" else (
        "output.webm", "output.avi", "output.webp", "output.mp4", "output.png",
    )
    for name in names:
        path = tmp / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            continue
        found = sniff_media(path)
        if found is None:
            continue
        ext, content_type = found
        if kind == "image" and ext != "png":
            continue
        return path, ext, content_type
    return None


# --------------------------------------------------------------------------- catalog helpers


def get_catalog_model(model_id: object) -> dict:
    model = CATALOG_BY_ID.get(str(model_id)) if model_id is not None else None
    if model is None:
        raise db.NotFound(f"Unknown media model {str(model_id)[:64]!r}")
    return model


def model_file_paths(model: dict) -> dict[str, Path]:
    folder = media_models_dir() / model["id"]
    return {item["role"]: folder / Path(item["filename"]).name for item in model["files"]}


def _file_ready(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def model_installed(model: dict) -> bool:
    return all(_file_ready(path) for path in model_file_paths(model).values())


def _download_id(model: dict, role: str) -> str:
    return f"media-model-{model['id']}-{role}".replace("_", "-")


def _download_state(download_id: str) -> dict | None:
    try:
        downloads = importlib.import_module(f"{__package__}.downloads")
    except ImportError:
        return None
    try:
        current = downloads.get(download_id)
    except Exception:  # unknown id (NotFound / KeyError) or module trouble: no active download
        return None
    return current if isinstance(current, dict) else None


def _download_active(download_id: str) -> bool:
    state = _download_state(download_id)
    return bool(state) and state.get("state") in ACTIVE_DOWNLOAD_STATES


def public_model(model: dict, ram_total: int | None = None) -> dict:
    """A CatalogModel-like dict for the UI."""
    available = models_available()
    installed = available and model_installed(model)
    downloading = (not installed) and any(_download_active(_download_id(model, f["role"])) for f in model["files"])
    data = {
        key: model[key]
        for key in ("id", "name", "family", "kind", "size_bytes", "min_ram_gb", "recommended", "license", "description")
    }
    data.update(
        installed=installed,
        available=available,
        unavailable_reason=None if available else DRIVE_MISSING_MESSAGE,
        downloading=downloading,
        fits_ram=None if ram_total is None else ram_total >= model["min_ram_gb"] * GIB * 0.95,
        warning=model.get("warning"),
        defaults=dict(model["defaults"]),
        files=[{"role": f["role"], "repo": f["repo"], "filename": f["filename"]} for f in model["files"]],
        download_ids=[_download_id(model, f["role"]) for f in model["files"]],
    )
    return data


# --------------------------------------------------------------------------- runtime discovery

_probe_lock = threading.Lock()
_probe_cache: dict[tuple[str, int, int], dict | None] = {}
_VERSION_RE = re.compile(r"stable-diffusion\.cpp version\s+([^\s,]+),?\s+commit\s+([0-9A-Za-z]+)")


def _exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def _managed_root() -> Path:
    return config.bin_dir() / RUNTIME_DIR_NAME


def _is_managed(path: Path) -> bool:
    try:
        root = _managed_root().resolve()
        return root in path.resolve().parents
    except OSError:
        return False


def _runtime_candidates() -> list[tuple[Path, str]]:
    """Managed install first (newest), then PATH, then the Homebrew dirs (Finder apps lack them on PATH)."""
    candidates: list[tuple[Path, str]] = []
    root = _managed_root()
    if root.is_dir() and not root.is_symlink():
        found = []
        for index, name in enumerate(RUNTIME_NAMES):
            for path in root.rglob(_exe(name)):
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    if os.name != "nt" and not os.access(path, os.X_OK):
                        continue  # made executable once after extraction (_runtime_installed)
                    found.append((-path.stat().st_mtime, index, str(path), path))
                except OSError:
                    continue
        candidates.extend((item[3], "managed") for item in sorted(found))
    for name in RUNTIME_NAMES:
        which = shutil.which(name)
        if which:
            path = Path(which)
            source = "homebrew" if str(path.parent) in HOMEBREW_DIRS else "path"
            candidates.append((path, source))
    for folder in HOMEBREW_DIRS:
        for name in RUNTIME_NAMES:
            path = Path(folder) / _exe(name)
            if path.is_file() and os.access(path, os.X_OK):
                candidates.append((path, "homebrew"))
    return candidates


def _child_env(binary: Path | None = None) -> dict[str, str]:
    """The environment for child processes, without secrets (API keys, tokens…)."""
    env = {key: value for key, value in os.environ.items() if not _SECRET_ENV_RE.search(key)}
    if binary is not None and sys.platform.startswith("linux") and _is_managed(binary):
        env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [str(binary.parent), env.get("LD_LIBRARY_PATH")]))
    return env


def _identify(output: str) -> dict | None:
    found = _VERSION_RE.search(output)
    if found:
        return {"version": f"{found.group(1)} ({found.group(2)[:12]})"}
    if "stable-diffusion.cpp" in output or "--sampling-method" in output or "img_gen" in output:
        return {"version": None}  # an older build without --version
    return None


def _probe_binary(path: Path) -> dict | None:
    """Run ``--version`` (then ``-h``) once per file version and make sure it is stable-diffusion.cpp.

    ``sd`` is also the name of an unrelated find-and-replace tool; it must
    never be run with generation arguments.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]
    info = None
    for flag in ("--version", "-h"):
        try:
            result = subprocess.run(
                [str(path), flag], cwd=str(media_tmp_dir()), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=PROBE_TIMEOUT_S, env=_child_env(path), check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("Could not run %s %s: %s", path, flag, exc)
            break
        info = _identify(result.stdout[:65536].decode("utf-8", "replace"))
        if info is not None:
            break
    if info is None:
        log.warning("Ignoring %s: it does not identify as stable-diffusion.cpp", path)
    with _probe_lock:
        _probe_cache[key] = info
    return info


RUNTIME_CACHE_TTL_S = 30.0
_runtime_cache: tuple[float, dict] | None = None


def invalidate_runtime_cache() -> None:
    """Forget discovery results (after an install, or in tests)."""
    global _runtime_cache
    with _probe_lock:
        _runtime_cache = None
        _probe_cache.clear()


def _runtime_installed(path: Path) -> None:
    """downloads ``on_complete`` for the runtime archive: make the binaries executable once.

    Raising makes the download fail (and removes the extracted files).
    """
    root = Path(path)
    binaries = [
        candidate
        for name in (*RUNTIME_NAMES, "sd-server")
        for candidate in root.rglob(_exe(name))
        if candidate.is_file() and not candidate.is_symlink()
    ]
    if not any(Path(b).name in {_exe(n) for n in RUNTIME_NAMES} for b in binaries):
        raise MediaJobError("The downloaded archive contains no sd-cli / sd executable")
    if os.name != "nt":
        for binary in binaries:
            binary.chmod(0o755)  # zip extraction may drop the executable bit
    invalidate_runtime_cache()


def find_runtime(refresh: bool = False) -> dict:
    """``{installed, path, version, source}`` of the stable-diffusion.cpp binary to use.

    Cached for RUNTIME_CACHE_TTL_S (status polling never re-scans or re-probes);
    an install invalidates the cache, the worker always refreshes.
    """
    global _runtime_cache
    now = time.monotonic()
    with _probe_lock:
        cached = _runtime_cache
    if not refresh and cached is not None and now - cached[0] < RUNTIME_CACHE_TTL_S:
        return dict(cached[1])
    result = _discover_runtime()
    with _probe_lock:
        _runtime_cache = (now, result)
    return dict(result)


def _discover_runtime() -> dict:
    seen: set[str] = set()
    for path, source in _runtime_candidates():
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        info = _probe_binary(path)
        if info is not None:
            return {"installed": True, "path": str(path), "version": info["version"], "source": source}
    return {"installed": False, "path": None, "version": None, "source": None}


def find_ffmpeg() -> Path | None:
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    for folder in HOMEBREW_DIRS:
        path = Path(folder) / _exe("ffmpeg")
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


# --------------------------------------------------------------------------- command lines (ALL sd flags live here)


def _num(value: float) -> str:
    return f"{float(value):g}"


def build_image_args(binary: Path | str, model: dict, files: dict[str, Path], job: dict, output: Path) -> list[str]:
    """``sd-cli`` txt2img command (default mode ``img_gen``, so ``-M`` is omitted for old builds)."""
    params = job["params"]
    args = [str(binary), "-m", str(files["model"])]
    if "vae" in files:
        args += ["--vae", str(files["vae"])]
    if model.get("weight_type"):
        args += ["--type", model["weight_type"]]  # convert weights while loading: less RAM
    args += ["-p", job["prompt"]]  # one argv element, whatever it contains
    if job.get("negative_prompt"):
        args += ["-n", job["negative_prompt"]]
    args += [
        "-W", str(params["width"]),
        "-H", str(params["height"]),
        "--steps", str(params["steps"]),
        "--cfg-scale", _num(params["cfg_scale"]),
        "--sampling-method", params["sampler"],
        "-s", str(params["seed"]),
    ]
    if params["width"] * params["height"] > 768 * 768:
        args.append("--vae-tiling")  # decode in tiles: much less RAM for large images
    args += ["-o", str(output)]
    return args


def build_video_args(binary: Path | str, model: dict, files: dict[str, Path], job: dict, output: Path) -> list[str]:
    """``sd-cli -M vid_gen`` command for Wan 2.1 (diffusion model + VAE + umt5 text encoder)."""
    params = job["params"]
    args = [
        str(binary), "-M", "vid_gen",
        "--diffusion-model", str(files["diffusion_model"]),
        "--vae", str(files["vae"]),
        "--t5xxl", str(files["text_encoder"]),
        "-p", job["prompt"],
    ]
    if job.get("negative_prompt"):
        args += ["-n", job["negative_prompt"]]
    args += [
        "-W", str(params["width"]),
        "-H", str(params["height"]),
        "--video-frames", str(params["frames"]),
        "--fps", str(params["fps"]),
        "--steps", str(params["steps"]),
        "--cfg-scale", _num(params["cfg_scale"]),
        "--sampling-method", params["sampler"],
    ]
    if params.get("flow_shift") is not None:
        args += ["--flow-shift", _num(params["flow_shift"])]
    args += ["-s", str(params["seed"]), "--vae-tiling", "-o", str(output)]
    return args


def build_transcode_args(ffmpeg: Path | str, source: Path, dest: Path, codec: str = "h264") -> list[str]:
    """ffmpeg command converting the native output to a browser-friendly file."""
    args = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-an"]
    if codec == "h264":
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart"]
    else:
        args += ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-row-mt", "1"]
    return args + [str(dest)]


# --------------------------------------------------------------------------- progress parsing

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BAR_RE = re.compile(r"\|(?P<bar>[=>#\- ]*)\|\s*(?P<step>\d+)\s*/\s*(?P<total>\d+)(?P<rest>.*)")
_STEP_RE = re.compile(r"\bstep\s*[:#]?\s*(?P<step>\d+)\s*(?:/|of)\s*(?P<total>\d+)", re.IGNORECASE)
_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(s/it|it/s)")
_ETA_RE = re.compile(r"ETA\s*((?:\d+:)?\d{1,2}:\d{2})")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ProgressParser:
    """Turns stable-diffusion.cpp output lines into ``progress`` (0..1) and a message.

    Phases: loading ``#`` bars → 0.02–0.10, the sampling bar (``|===>  | 12/20 -
    3.52s/it - ETA 00:28`` or ``step 12/20``) → 0.10–0.90, later bars (VAE tiles)
    and "decoding" → 0.90–0.98, "save result" → 0.98.
    """

    def __init__(self, kind: str, steps: int | None = None):
        self.kind = kind
        self.steps = steps
        self.progress = 0.0
        self.message = "Loading the model…"
        self.phase = "start"
        self.tail: deque[str] = deque(maxlen=30)

    def _set(self, progress: float, message: str) -> None:
        self.progress = max(self.progress, min(0.99, progress))
        self.message = message

    def _sampling(self, step: int, total: int, rest: str) -> None:
        self.phase = "sampling"
        noun = "Generating video" if self.kind == "video" else "Generating image"
        message = f"{noun}: step {step}/{total}"
        extras = []
        speed = _SPEED_RE.search(rest)
        if speed:
            value, unit = float(speed.group(1)), speed.group(2)
            extras.append(f"{value:.1f} s/step" if unit == "s/it" else f"{value:.1f} steps/s")
        eta = _ETA_RE.search(rest)
        if eta:
            extras.append(f"about {eta.group(1)} left")
        if extras:
            message += " (" + ", ".join(extras) + ")"
        self._set(0.10 + 0.80 * step / total, message)
        if step >= total:
            self.phase = "sampled"

    def feed(self, line: str) -> bool:
        """Parse one output line; returns True when progress or message changed."""
        before = (self.progress, self.message)
        text = _CONTROL_CHARS_RE.sub("", _ANSI_RE.sub("", line)).strip()
        if not text:
            return False
        bar = _BAR_RE.search(text)
        step_match = None if bar else _STEP_RE.search(text)
        if bar or step_match:
            found = bar or step_match
            step, total = int(found.group("step")), int(found.group("total"))
            if total <= 0:
                return False
            step = min(step, total)
            rest = bar.group("rest") if bar else text[found.end():]
            if bar and "#" in bar.group("bar"):
                if self.phase == "start":
                    self._set(0.02 + 0.08 * step / total, f"Loading the model… {100 * step // total}%")
            elif self.phase in ("start", "sampling") and (
                step_match or self.steps is None or total == self.steps or self.phase == "sampling"
            ):
                self._sampling(step, total, rest)
            elif self.phase in ("sampled", "decoding"):
                self.phase = "decoding"
                self._set(0.90 + 0.08 * step / total, f"Decoding: {step}/{total}")
            else:
                self._set(self.progress, f"Preparing: {step}/{total}")
            return (self.progress, self.message) != before
        self.tail.append(text[:500])
        lowered = text.lower()
        if "save result" in lowered or "saving" in lowered:
            self._set(0.98, "Saving…")
        elif "decod" in lowered and self.phase in ("sampling", "sampled", "decoding"):
            self.phase = "decoding"
            self._set(0.90, "Decoding the result…")
        return (self.progress, self.message) != before

    def failure_summary(self, limit: int = 1500) -> str:
        lines = list(self.tail)
        interesting = [line for line in lines if re.search(r"error|fail|unknown|invalid|not found|abort", line, re.I)]
        chosen = (interesting or lines)[-6:]
        return " | ".join(chosen)[-limit:]


class _ProgressReporter:
    """Feeds output lines to a parser and writes changes to the job row (throttled)."""

    def __init__(self, job_id: int, parser: ProgressParser, base: float = 0.0, scale: float = 1.0):
        self.job_id = job_id
        self.parser = parser
        self.base, self.scale = base, scale
        self._last_write = 0.0
        self._dirty = False
        self._lock = threading.Lock()

    def feed(self, line: str) -> None:
        with self._lock:
            if not self.parser.feed(line):
                return
            self._dirty = True
            now = time.monotonic()
            if now - self._last_write >= PROGRESS_WRITE_INTERVAL_S:
                self._write(now)

    def _write(self, now: float) -> None:
        self._last_write = now
        self._dirty = False
        try:
            _update_running(self.job_id, self.base + self.scale * self.parser.progress, self.parser.message)
        except Exception:  # progress is best effort; never kill the reader thread
            log.exception("Could not store progress of media job #%s", self.job_id)

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._write(time.monotonic())


def _pump_output(stream: Any, on_line: Callable[[str], None]) -> None:
    """Read a child's output; progress bars end with ``\\r``, log lines with ``\\n``."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    read = getattr(stream, "read1", stream.read)
    try:
        while True:
            chunk = read(65536)
            if not chunk:
                break
            buffer += decoder.decode(chunk)
            parts = re.split(r"[\r\n]", buffer)
            buffer = parts.pop()
            # A progress bar ends with "\x1b[K" and no newline (the next one starts with "\r"):
            # report it now instead of one step late.
            if len(buffer) > 65536 or buffer.endswith("\x1b[K"):
                parts.append(buffer)
                buffer = ""
            for part in parts:
                try:
                    on_line(part)
                except Exception:
                    log.exception("Progress callback failed")
        buffer += decoder.decode(b"", final=True)
        if buffer:
            on_line(buffer)
    except (OSError, ValueError):  # stream closed while killing the process
        pass


def _kill_process(proc: subprocess.Popen) -> None:
    """Terminate the whole process group, escalate to SIGKILL after KILL_GRACE_S."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(KILL_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        log.error("Process %s did not exit after SIGKILL", proc.pid)


def _format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


# --------------------------------------------------------------------------- the LLM pause


def _llm_pause_context() -> Any:
    """``engine.pause_for_heavy_job()`` when ``settings.media.unload_llm`` is on, else ``None``."""
    try:
        unload = bool(settings.get("media", "unload_llm"))
    except Exception:
        unload = True
    if not unload:
        return None
    try:
        engine = importlib.import_module(f"{__package__}.engine")
    except ImportError:
        log.info("backend.engine is not available; not pausing the chat model")
        return None
    pause = getattr(engine, "pause_for_heavy_job", None)
    return pause() if callable(pause) else None


# --------------------------------------------------------------------------- the worker


class MediaWorker:
    """One background thread that processes queued media jobs sequentially."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: int | None = None
        self._proc: subprocess.Popen | None = None
        self._cancel: set[int] = set()

    # ---------------------------------------------------------------- control

    def is_alive(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def current_job_id(self) -> int | None:
        with self._lock:
            return self._current

    def wake(self) -> None:
        self._wake.set()

    def ensure_started(self) -> None:
        """Start the thread (after crash recovery) unless it is already running."""
        if not self.is_alive():
            recover_interrupted()
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._stop = threading.Event()
                    self._thread = threading.Thread(
                        target=self._loop, args=(self._stop,), name="lowram-media", daemon=True
                    )
                    self._thread.start()
        self._wake.set()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the thread; a running job is killed and recorded as interrupted."""
        with self._lock:
            thread = self._thread
            self._stop.set()
            proc = self._proc
        self._wake.set()
        if proc is not None:
            _kill_process(proc)
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def cancel(self, job_id: int) -> bool:
        """Cancel the job this worker is running; False if it is not the current job."""
        with self._lock:
            if self._current != job_id:
                return False
            self._cancel.add(job_id)
            proc = self._proc
        self._wake.set()
        if proc is not None:
            _kill_process(proc)
        return True

    def _cancel_requested(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._cancel

    def _check_cancel(self, job_id: int, stop: threading.Event) -> None:
        if self._cancel_requested(job_id):
            raise _Cancelled()
        if stop.is_set():
            raise _Stopped()

    # ---------------------------------------------------------------- loop

    def _loop(self, stop: threading.Event) -> None:
        log.info("Media worker started")
        while not stop.is_set():
            self._wake.clear()
            try:
                delay = self._step(stop)
            except Exception:
                log.exception("Media worker step failed")
                delay = SLOT_RETRY_S
            if delay > 0 and not stop.is_set():
                self._wake.wait(delay)
        log.info("Media worker stopped")

    def _step(self, stop: threading.Event) -> float:
        job = _next_queued_job()
        if job is None:
            return IDLE_POLL_S
        on_battery = power.battery_block_reason()
        if on_battery:  # the job stays queued and shows why
            _set_queued_message(job["id"], on_battery)
            return BATTERY_RETRY_S
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(db.inference_slot(f"{job['kind']} generation #{job['id']}"))
            except db.Busy:
                _set_queued_message(job["id"], WAITING_MESSAGE)
                return SLOT_RETRY_S
            if not _mark_running(job["id"]):
                return 0.0  # cancelled while waiting
            stack.enter_context(power.keep_awake(f"{job['kind']} generation #{job['id']}"))
            with self._lock:
                self._current = job["id"]
            try:
                self._execute(job, stop, stack)
            finally:
                with self._lock:
                    self._current = None
                    self._proc = None
                    self._cancel.discard(job["id"])
        # leaving the stack restarts the chat model (if paused) and frees the slot
        return 0.0

    def _execute(self, job: dict, stop: threading.Event, stack: contextlib.ExitStack) -> None:
        job_id = job["id"]
        tmp: Path | None = None
        started = time.monotonic()
        try:
            runtime = find_runtime(refresh=True)
            if not runtime["installed"]:
                raise MediaJobError("stable-diffusion.cpp is not installed. Install it under Images & Video first.")
            if not models_available():
                raise MediaJobError(f"{DRIVE_MISSING_MESSAGE}. Reconnect it and submit the job again.")
            model = get_catalog_model(job["model_id"])
            files = model_file_paths(model)
            missing = [path.name for path in files.values() if not _file_ready(path)]
            if missing:
                raise MediaJobError(f"Model files are missing: {', '.join(missing)}. Install the model again.")
            self._check_cancel(job_id, stop)
            pause = _llm_pause_context()
            if pause is not None:
                _update_running(job_id, 0.0, "Pausing the chat model to free RAM…")
                try:
                    stack.enter_context(pause)
                except Exception as exc:  # generation can still work, just with less free RAM
                    log.warning("Could not pause the chat model for media job #%s: %s", job_id, exc)
            self._check_cancel(job_id, stop)
            tmp = _job_tmp_dir(job_id)
            binary = Path(runtime["path"])
            if job["kind"] == "image":
                argv = build_image_args(binary, model, files, job, tmp / "output.png")
                timeout = IMAGE_TIMEOUT_S
            else:
                argv = build_video_args(binary, model, files, job, tmp / "output.webm")
                timeout = VIDEO_TIMEOUT_S
            parser = ProgressParser(job["kind"], job["params"].get("steps"))
            _update_running(job_id, 0.01, "Loading the model… (this can take a minute)")
            code = self._run_process(job_id, argv, tmp, timeout, stop, _ProgressReporter(job_id, parser),
                                     _child_env(binary))
            if code != 0:
                summary = parser.failure_summary()
                raise MediaJobError(
                    f"stable-diffusion.cpp failed (exit code {code})" + (f": {summary}" if summary else "")
                )
            produced = _find_output(tmp, job["kind"])
            if produced is None:
                summary = parser.failure_summary()
                raise MediaJobError(
                    "stable-diffusion.cpp finished but wrote no output file" + (f": {summary}" if summary else "")
                )
            source, ext, content_type = produced
            if job["kind"] == "video" and ext in ("avi", "webm"):
                source, ext, content_type = self._convert_video(job_id, source, ext, content_type, tmp, stop)
            dest = _store_output(job_id, source, ext)
            note = " (AVI: download it if your browser cannot play it)" if ext == "avi" else ""
            _finish(
                job_id, "done", message=f"Done in {_format_seconds(time.monotonic() - started)}{note}",
                output_file=dest.name, output_type=content_type,
            )
            log.info("Media job #%s done (%s)", job_id, content_type)
            power.notify(f"{job['kind'].capitalize()} ready",
                         f"Done in {_format_seconds(time.monotonic() - started)}: {job.get('prompt') or ''}")
        except _Cancelled:
            _finish(job_id, "cancelled", message="Cancelled.")
        except _Stopped:
            _finish(
                job_id, "failed", message="Interrupted",
                error="Interrupted: LowRAM AI was closed while this job was running. Submit it again.",
            )
        except MediaJobError as exc:
            _finish(job_id, "failed", message="Failed", error=str(exc))
            power.notify(f"{job['kind'].capitalize()} failed", str(exc))
        except Exception as exc:  # recorded as an explicit failure, never as success
            log.exception("Media job #%s crashed", job_id)
            _finish(job_id, "failed", message="Failed", error=f"Unexpected error: {exc}")
            power.notify(f"{job['kind'].capitalize()} failed", f"Unexpected error: {exc}")
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def _convert_video(
        self, job_id: int, source: Path, ext: str, content_type: str, tmp: Path, stop: threading.Event
    ) -> tuple[Path, str, str]:
        """MP4 (H.264) via ffmpeg when available; keep the native WebM/AVI otherwise."""
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            return source, ext, content_type
        attempts = [("h264", tmp / "converted.mp4")]
        if ext == "avi":  # WebM is already playable; only AVI needs a second chance
            attempts.append(("vp9", tmp / "converted.webm"))
        for codec, dest in attempts:
            _update_running(job_id, 0.985, "Converting the video for the browser…")
            parser = ProgressParser("video")
            code = self._run_process(job_id, build_transcode_args(ffmpeg, source, dest, codec), tmp,
                                     TRANSCODE_TIMEOUT_S, stop, _ProgressReporter(job_id, parser), _child_env())
            found = sniff_media(dest) if code == 0 and _file_ready(dest) else None
            if found and found[0] in ("mp4", "webm"):
                return dest, found[0], found[1]
            log.warning("ffmpeg (%s) could not convert media job #%s: %s", codec, job_id, parser.failure_summary())
        return source, ext, content_type

    def _run_process(
        self,
        job_id: int,
        argv: list[str],
        cwd: Path,
        timeout: float,
        stop: threading.Event,
        reporter: _ProgressReporter,
        env: dict[str, str],
    ) -> int:
        """Run ``argv`` (no shell) in its own process group; cancellable, with a timeout."""
        self._check_cancel(job_id, stop)
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
                argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, **kwargs,
            )
        except OSError as exc:
            raise MediaJobError(f"Could not start {Path(argv[0]).name}: {exc}") from exc
        with self._lock:
            self._proc = proc
            cancelled_early = job_id in self._cancel
        if cancelled_early:
            _kill_process(proc)
        reader = threading.Thread(target=_pump_output, args=(proc.stdout, reporter.feed), daemon=True,
                                  name=f"lowram-media-output-{job_id}")
        reader.start()
        deadline = time.monotonic() + timeout
        outcome = None
        code = None
        try:
            while code is None:
                try:
                    code = proc.wait(0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self._cancel_requested(job_id):
                    outcome = "cancelled"
                elif stop.is_set():
                    outcome = "stopped"
                elif time.monotonic() > deadline:
                    outcome = "timeout"
                if outcome:
                    _kill_process(proc)
                    code = proc.poll()
                    break
        finally:
            if proc.poll() is None:
                _kill_process(proc)
            reader.join(5)
            if proc.stdout is not None:
                proc.stdout.close()
            with self._lock:
                self._proc = None
            reporter.flush()
        if outcome == "cancelled" or self._cancel_requested(job_id):
            raise _Cancelled()
        if outcome == "stopped" or stop.is_set():
            raise _Stopped()
        if outcome == "timeout":
            raise MediaJobError(
                f"{Path(argv[0]).name} did not finish within {_format_seconds(timeout)} and was stopped. "
                "Try fewer steps, a smaller size or fewer frames."
            )
        return int(code if code is not None else -1)


WORKER = MediaWorker()


# --------------------------------------------------------------------------- validation and job API

ALLOWED_JOB_FIELDS = frozenset(
    {"kind", "prompt", "negative_prompt", "width", "height", "steps", "seed", "frames", "fps", "cfg_scale",
     "model_id"}
)
_PROMPT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value: object, name: str, *, required: bool) -> str:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > MAX_PROMPT_CHARS:
        raise ValueError(f"{name} must be at most {MAX_PROMPT_CHARS} characters")
    if _PROMPT_CONTROL_RE.search(value):
        raise ValueError(f"{name} contains control characters")
    return value


def _int_field(data: dict, name: str, default: int, low: int, high: int) -> int:
    value = data.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a whole number")
    if isinstance(value, float):
        if not value.is_integer():  # also rejects inf / nan
            raise ValueError(f"{name} must be a whole number")
        value = int(value)
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
    return int(value)


def _float_field(data: dict, name: str, default: float, low: float, high: float) -> float:
    value = data.get(name)
    if value is None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:  # nan
        raise ValueError(f"{name} must be a number")
    if not low <= float(value) <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return float(value)


def _size_field(data: dict, name: str, default: int, multiple: int, high: int) -> int:
    value = _int_field(data, name, default, MIN_SIZE, high)
    if value % multiple:
        raise ValueError(f"{name} must be a multiple of {multiple} (between {MIN_SIZE} and {high})")
    return value


def installed_models(kind: str) -> list[dict]:
    return [model for model in CATALOG if model["kind"] == kind and model_installed(model)]


def _resolve_model(kind: str, model_id: object) -> dict:
    if not models_available():
        raise HTTPError(HTTPStatus.CONFLICT, DRIVE_MISSING_MESSAGE)
    if model_id is None or model_id == "":
        try:
            preferred = settings.get("media", f"{kind}_model_id")
        except Exception:
            preferred = None
        candidates = installed_models(kind)
        for model in candidates:
            if model["id"] == preferred:
                return model
        if not candidates:
            raise ValueError(f"No {kind} model is installed yet. Install one under Images & Video first.")
        return sorted(candidates, key=lambda m: not m["recommended"])[0]
    if not isinstance(model_id, str) or not CATALOG_ID_RE.fullmatch(model_id):
        raise ValueError("model_id must be a catalog id")
    model = CATALOG_BY_ID.get(model_id)
    if model is None:
        raise ValueError(f"Unknown model {model_id!r}")
    if model["kind"] != kind:
        raise ValueError(f"{model['name']} is a {model['kind']} model, not a {kind} model")
    if not model_installed(model):
        raise ValueError(f"{model['name']} is not installed yet. Install it first.")
    return model


def validate_job_request(data: object) -> dict:
    """Strictly validate a ``POST /api/media/jobs`` body; returns the job spec to store."""
    if not isinstance(data, dict):
        raise ValueError("Body must be a JSON object")
    unknown = set(data) - ALLOWED_JOB_FIELDS
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(sorted(map(str, unknown)))[:200]}")
    kind = data.get("kind")
    if kind not in KINDS:
        raise ValueError("kind must be 'image' or 'video'")
    prompt = _text(data.get("prompt"), "prompt", required=True)
    model = _resolve_model(kind, data.get("model_id"))
    defaults = model["defaults"]
    if data.get("negative_prompt") is None:
        negative = model.get("default_negative_prompt", "")
    else:
        negative = _text(data.get("negative_prompt"), "negative_prompt", required=False)
    if kind == "image":
        multiple, max_size = IMAGE_SIZE_MULTIPLE, MAX_IMAGE_SIZE
    else:
        multiple, max_size = VIDEO_SIZE_MULTIPLE, MAX_VIDEO_SIZE
    params: dict[str, Any] = {
        "width": _size_field(data, "width", defaults["width"], multiple, max_size),
        "height": _size_field(data, "height", defaults["height"], multiple, max_size),
        "steps": _int_field(data, "steps", defaults["steps"], 1, MAX_STEPS),
        "cfg_scale": _float_field(data, "cfg_scale", defaults["cfg_scale"], 0.0, MAX_CFG_SCALE),
        "sampler": defaults["sampler"],
    }
    seed = _int_field(data, "seed", -1, -1, MAX_SEED)
    params["seed"] = secrets.randbelow(2**31) if seed == -1 else seed  # stored, so results are reproducible
    if kind == "video":
        frames = _int_field(data, "frames", defaults["frames"], 1, MAX_FRAMES)
        if model.get("frames_rule") == "4n+1" and (frames - 1) % 4:
            raise ValueError(f"{model['name']} needs 4n+1 frames (e.g. 17, 33, 49, 81), got {frames}")
        params["frames"] = frames
        params["fps"] = _int_field(data, "fps", defaults["fps"], 1, MAX_FPS)
        if defaults.get("flow_shift") is not None:
            params["flow_shift"] = float(defaults["flow_shift"])
    elif data.get("frames") is not None or data.get("fps") is not None:
        raise ValueError("frames and fps only apply to videos")
    return {"kind": kind, "prompt": prompt, "negative_prompt": negative, "model_id": model["id"], "params": params}


def submit_job(data: object) -> dict:
    """Validate, queue and wake the worker. Raises ValueError / HTTPError(409)."""
    spec = validate_job_request(data)
    if not find_runtime()["installed"]:
        raise HTTPError(HTTPStatus.CONFLICT, "stable-diffusion.cpp is not installed yet. Install the image runtime first.")
    with db.connect() as conn, db.transaction(conn):
        queued = conn.execute("SELECT COUNT(*) FROM media_jobs WHERE status = 'queued'").fetchone()[0]
        if queued >= MAX_QUEUED_JOBS:
            raise db.Busy(f"{queued} media jobs are already waiting; let some finish first.")
        cur = conn.execute(
            "INSERT INTO media_jobs (kind, prompt, negative_prompt, model_id, params, status, progress, message, "
            "created_at) VALUES (?, ?, ?, ?, ?, 'queued', 0, 'Queued', ?)",
            (spec["kind"], spec["prompt"], spec["negative_prompt"], spec["model_id"], json.dumps(spec["params"]),
             db.utcnow()),
        )
        job_id = int(cur.lastrowid)
    WORKER.ensure_started()
    return get_job(job_id)


def _wait_not_running(job_id: int, timeout: float) -> dict:
    end = time.monotonic() + timeout
    job = get_job(job_id)
    while job["status"] == "running" and time.monotonic() < end:
        time.sleep(0.05)
        job = get_job(job_id)
    return job


def cancel_job(job_id: int) -> dict:
    job_id = _check_job_id(job_id)
    for _ in range(3):
        job = get_job(job_id)
        if job["status"] == "queued":
            with db.connect() as conn:
                cur = conn.execute(
                    "UPDATE media_jobs SET status = 'cancelled', message = 'Cancelled.', finished_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (db.utcnow(), job_id),
                )
            if cur.rowcount == 1:
                WORKER.wake()
                return get_job(job_id)
            continue  # the worker just picked it up: cancel the running job instead
        if job["status"] == "running":
            if WORKER.cancel(job_id):
                return _wait_not_running(job_id, CANCEL_WAIT_S)
            if WORKER.current_job_id() is None:  # stale row (its process is gone)
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE media_jobs SET status = 'cancelled', message = 'Cancelled.', finished_at = ?, "
                        "owner = NULL WHERE id = ? AND status = 'running'",
                        (db.utcnow(), job_id),
                    )
                return get_job(job_id)
            time.sleep(0.05)  # between "running" and the worker's bookkeeping
            continue
        raise db.InvalidTransition(f"Media job #{job_id} is already {job['status']}")
    return get_job(job_id)


def delete_job(job_id: int) -> None:
    job_id = _check_job_id(job_id)
    job = get_job(job_id)
    if job["status"] in ("queued", "running"):
        job = cancel_job(job_id)
        if job["status"] == "running":
            raise db.Busy(f"Media job #{job_id} is still stopping; try again in a moment.")
    with db.connect() as conn, db.transaction(conn):
        row = _get_row(conn, job_id)
        if row["status"] == "running":
            raise db.Busy(f"Media job #{job_id} is running; cancel it first.")
        path = output_path(job_id, row["output_file"])
        conn.execute("DELETE FROM media_jobs WHERE id = ?", (job_id,))
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def job_file(job_id: int) -> tuple[Path, str, str]:
    """``(path, content type, ext)`` of a finished job's output; ``db.NotFound`` until then."""
    job_id = _check_job_id(job_id)
    with db.connect() as conn:
        row = _get_row(conn, job_id)
    if row["status"] != "done":
        raise db.NotFound(f"Media job #{job_id} has no file yet ({row['status']})")
    path = output_path(job_id, row["output_file"])
    if path is None or path.is_symlink() or not path.is_file():
        raise db.NotFound(f"The output file of media job #{job_id} is missing on disk")
    ext = path.suffix.lstrip(".")
    return path, OUTPUT_TYPES[ext], ext


# --------------------------------------------------------------------------- metadata (GitHub / Hugging Face)


def _check_metadata_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in METADATA_HOSTS or parsed.port not in (
        None, 443,
    ):
        raise MediaSourceError(f"Refusing to fetch metadata from {parsed.scheme}://{parsed.hostname}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - stdlib signature
        _check_metadata_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_json(url: str) -> Any:
    """GET a small JSON document over HTTPS from an allow-listed host."""
    _check_metadata_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "LowRAM-AI"})
    try:
        with opener.open(request, timeout=METADATA_TIMEOUT_S) as response:
            data = response.read(METADATA_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        host = urllib.parse.urlsplit(url).hostname
        if exc.code in (401, 403):
            raise MediaSourceError(
                f"{host} refused access (HTTP {exc.code}). The file may need a login or licence acceptance; "
                "LowRAM AI only downloads openly available files."
            ) from exc
        if exc.code == 404:
            raise MediaSourceError(f"{host} answered 404 (not found) for {urllib.parse.urlsplit(url).path}") from exc
        if exc.code == 429:
            raise MediaSourceError(f"{host} is rate limiting requests; try again later.") from exc
        raise MediaSourceError(f"{host} answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise MediaSourceError(f"Could not reach {urllib.parse.urlsplit(url).hostname}: {reason}") from exc
    if len(data) > METADATA_MAX_BYTES:
        raise MediaSourceError("Metadata answer is unexpectedly large")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MediaSourceError("Metadata answer is not valid JSON") from exc


def hf_file_metadata(repo: str, filename: str) -> dict:
    """Size and SHA-256 of a Hugging Face file; refuses files without a SHA-256 (LFS oid)."""
    if not HF_REPO_RE.fullmatch(repo) or not HF_PATH_RE.fullmatch(filename) or ".." in filename:
        raise MediaSourceError(f"Invalid Hugging Face file reference {repo}/{filename}")
    folder = filename.rpartition("/")[0]
    url = f"{HF_API_MODELS}{repo}/tree/main" + (f"/{urllib.parse.quote(folder)}" if folder else "")
    entries = fetch_json(url)
    if not isinstance(entries, list):
        raise MediaSourceError("Unexpected answer from huggingface.co")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "file" or entry.get("path") != filename:
            continue
        lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else {}
        sha256 = lfs.get("oid") or lfs.get("sha256")
        size = lfs.get("size", entry.get("size"))
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise MediaSourceError(f"huggingface.co lists no SHA-256 for {filename}; refusing an unverified download.")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise MediaSourceError(f"huggingface.co lists no size for {filename}; refusing the download.")
        return {
            "sha256": sha256,
            "size": size,
            "url": f"{HF_BASE}{repo}/resolve/main/{urllib.parse.quote(filename)}",
        }
    raise MediaSourceError(f"{filename} was not found in the Hugging Face repository {repo}.")


def _platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(machine, machine)
    return system, machine


def pick_runtime_asset(assets: object, system: str, machine: str) -> dict:
    """The release asset for this platform (first matching pattern wins)."""
    patterns = RUNTIME_ASSET_PATTERNS.get((system, machine))
    if not patterns:
        raise MediaSourceError(
            f"There is no prebuilt stable-diffusion.cpp for {system} {machine}. Build it from source "
            "(github.com/leejet/stable-diffusion.cpp) and put sd-cli on your PATH."
        )
    usable = [a for a in assets if isinstance(a, dict) and isinstance(a.get("name"), str)] if isinstance(
        assets, list
    ) else []
    for pattern in patterns:
        for asset in usable:
            if re.fullmatch(pattern, asset["name"]):
                return asset
    raise MediaSourceError(f"The latest stable-diffusion.cpp release has no download for {system} {machine}.")


def _parse_digest(asset: dict) -> str | None:
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        value = digest.split(":", 1)[1].lower()
        if SHA256_RE.fullmatch(value):
            return value
    return None


# --------------------------------------------------------------------------- installs


def _downloads_module() -> Any:
    try:
        return importlib.import_module(f"{__package__}.downloads")
    except ImportError as exc:
        log.warning("backend.downloads is not available: %s", exc)
        raise HTTPError(
            HTTPStatus.SERVICE_UNAVAILABLE, "Downloads are not available in this build (backend.downloads is missing)."
        ) from exc


def install_runtime() -> dict:
    """Start downloading the latest stable-diffusion.cpp release for this platform."""
    downloads = _downloads_module()
    current = _download_state("media-runtime")
    if current and current.get("state") in ACTIVE_DOWNLOAD_STATES:
        return current
    release = fetch_json(RELEASES_URL)
    if not isinstance(release, dict):
        raise MediaSourceError("Unexpected answer from api.github.com")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", tag):
        raise MediaSourceError("The latest release has an unexpected tag name")
    asset = pick_runtime_asset(release.get("assets"), *_platform_key())
    name, url = asset["name"], asset.get("browser_download_url")
    if not isinstance(url, str) or not url.startswith(RELEASE_DOWNLOAD_PREFIX) or "/" in name or ".." in name:
        raise MediaSourceError("The release asset has an unexpected download URL")
    size = asset.get("size")
    size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None
    sha256 = _parse_digest(asset)
    if sha256 is None:
        raise MediaSourceError(
            f"GitHub lists no SHA-256 digest for {name}; refusing an unverified download. "
            "Install stable-diffusion.cpp manually (sd-cli on your PATH) or try again later."
        )
    root = _private_dir(_managed_root())
    return downloads.start(
        "media-runtime", url, root / name, sha256=sha256, size=size,
        label=f"stable-diffusion.cpp {tag}", kind="media-runtime", extract=root / tag,
        on_complete=_runtime_installed,
    )


def install_model(model_id: str) -> list[dict]:
    """Start (verified) downloads for every missing file of a catalog model."""
    model = get_catalog_model(model_id)
    downloads = _downloads_module()
    _require_models_dir()  # 409 instead of writing to the boot disk when the drive is unplugged
    paths = model_file_paths(model)
    planned = []
    for item in model["files"]:
        dest = paths[item["role"]]
        download_id = _download_id(model, item["role"])
        if _file_ready(dest):
            continue
        current = _download_state(download_id)
        if current and current.get("state") in ACTIVE_DOWNLOAD_STATES:
            planned.append((item, dest, download_id, None, current))
            continue
        planned.append((item, dest, download_id, hf_file_metadata(item["repo"], item["filename"]), None))
    started = []
    for item, dest, download_id, meta, current in planned:  # all metadata fetched first: fail before starting
        if current is not None:
            started.append(current)
            continue
        _private_dir(dest.parent)
        started.append(
            downloads.start(
                download_id, meta["url"], dest, sha256=meta["sha256"], size=meta["size"],
                label=f"{model['name']}: {Path(item['filename']).name}", kind="media-model",
            )
        )
    return started


def status() -> dict:
    """Everything the Images & Video page needs."""
    try:
        ram_total = int(psutil.virtual_memory().total)
    except Exception:
        ram_total = None
    models = [public_model(model, ram_total) for model in CATALOG]
    active = WORKER.current_job_id()
    if active is None:
        active = _running_job_id()
    runtime = find_runtime()
    runtime["downloading"] = _download_active("media-runtime")
    return {
        "runtime": runtime,
        "image_models": [m for m in models if m["kind"] == "image"],
        "video_models": [m for m in models if m["kind"] == "video"],
        "active_job_id": active,
        "ffmpeg": find_ffmpeg() is not None,
        "ram_total_bytes": ram_total,
        "models_available": models_available(),
        "models_message": None if models_available() else f"{DRIVE_MISSING_MESSAGE} ({config.models_dir()}).",
        "settings": settings.get("media"),
    }


# --------------------------------------------------------------------------- routes


def _source_error(exc: MediaSourceError) -> HTTPError:
    return HTTPError(HTTPStatus.BAD_GATEWAY, str(exc))


@route("GET", r"/api/media/status")
def get_media_status(req: Request) -> Response:
    return Response(status())


@route("POST", r"/api/media/runtime/install")
def post_runtime_install(req: Request) -> Response:
    req.json()
    try:
        download = install_runtime()
    except MediaSourceError as exc:
        raise _source_error(exc) from exc
    return Response({"download": download}, HTTPStatus.ACCEPTED)


@route("POST", r"/api/media/models/([a-z0-9][a-z0-9._-]{0,63})/install")
def post_model_install(req: Request, model_id: Any) -> Response:
    req.json()
    try:
        started = install_model(str(model_id))
    except MediaSourceError as exc:
        raise _source_error(exc) from exc
    return Response(
        {"downloads": started, "installed": model_installed(get_catalog_model(str(model_id)))}, HTTPStatus.ACCEPTED
    )


@route("GET", r"/api/media/jobs")
def get_media_jobs(req: Request) -> Response:
    return Response({"jobs": list_jobs()})


@route("POST", r"/api/media/jobs")
def post_media_job(req: Request) -> Response:
    return Response({"job": submit_job(req.json())}, HTTPStatus.ACCEPTED)


@route("GET", r"/api/media/jobs/(\d+)")
def get_media_job(req: Request, job_id: int) -> Response:
    return Response({"job": get_job(job_id)})


@route("POST", r"/api/media/jobs/(\d+)/cancel")
def post_media_cancel(req: Request, job_id: int) -> Response:
    req.json()
    return Response({"job": cancel_job(job_id)})


@route("POST", r"/api/media/jobs/(\d+)/delete")
def post_media_delete(req: Request, job_id: int) -> Response:
    req.json()
    delete_job(job_id)
    return Response({"deleted": True})


@route("GET", r"/api/media/jobs/(\d+)/file")
def get_media_file(req: Request, job_id: int) -> FileResponse:
    try:
        path, content_type, ext = job_file(job_id)
    except db.NotFound as exc:
        raise HTTPError(HTTPStatus.NOT_FOUND, str(exc)) from exc
    download = req.arg("download") in ("1", "true")
    kind = "video" if content_type.startswith("video/") else "image"
    return FileResponse(path, content_type, f"lowram-{kind}-{int(job_id)}.{ext}" if download else None)


@on_startup
def _start_worker(server: Any) -> None:
    """Crash recovery (interrupted jobs → failed) and resume processing queued jobs."""
    WORKER.ensure_started()


@on_shutdown
def _stop_worker(server: Any) -> None:
    WORKER.stop()
