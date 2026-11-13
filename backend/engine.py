"""Managed llama.cpp runtime: install, model catalog and the local llama-server.

* Runtime discovery: ``llama-server`` in PATH, Homebrew folders, then the
  managed install below ``<LOWRAM_HOME>/bin/llama.cpp/<tag>/``.
* Runtime install: latest ggml-org/llama.cpp GitHub release, asset picked for
  this platform, verified with the asset's SHA-256 ``digest``.
* Models: a small curated catalog of ungated, permissively licensed GGUF
  models; size and SHA-256 come from the Hugging Face API at install time and
  an install without that metadata is refused.
* Server: one ``llama-server`` bound to 127.0.0.1, state machine
  stopped -> starting -> ready/error, crash detection with the last log lines.
  Never started when ``LOWRAM_LLM_URL`` points at an external server.
"""

from __future__ import annotations

import atexit
import functools
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psutil

from . import artifacts, config, db, downloads, llm, settings, web
from .web import HTTPError, Request, Response, route

log = logging.getLogger("lowram.engine")

GIB = 1024**3
MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
TAG_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
RAM_HEADROOM = 0.40  # leave 40 % of the RAM to the OS and other apps

RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
HF_TREE_URL = "https://huggingface.co/api/models/{repo}/tree/main"
HF_RESOLVE_URL = "https://huggingface.co/{repo}/resolve/main/{filename}"
RUNTIME_JOB_ID = "runtime-llama.cpp"
RUNTIME_NAME = "llama-server.exe" if os.name == "nt" else "llama-server"
HOMEBREW_DIRS: tuple[Path, ...] = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))

VERSION_TIMEOUT_S = 10.0
HEALTH_TIMEOUT_S = 900.0  # big models on slow disks can take minutes to load
HEALTH_POLL_S = 0.5
HEALTH_REQUEST_TIMEOUT_S = 2.0
STOP_GRACE_S = 10.0
LOG_TAIL_LINES = 20
MAX_HF_PAGES = 5

# Platform key -> regexes for the release asset names (first match wins; .tar.gz preferred).
ASSET_PATTERNS: dict[str, tuple[str, ...]] = {
    "macos-arm64": (r"llama-[A-Za-z0-9._-]+-bin-macos-arm64\.(?:tar\.gz|zip)",),
    "macos-x64": (r"llama-[A-Za-z0-9._-]+-bin-macos-x64\.(?:tar\.gz|zip)",),
    "linux-x64": (r"llama-[A-Za-z0-9._-]+-bin-ubuntu-x64\.(?:tar\.gz|zip)",),
    "linux-arm64": (r"llama-[A-Za-z0-9._-]+-bin-ubuntu-arm64\.(?:tar\.gz|zip)",),
    "windows-x64": (
        r"llama-[A-Za-z0-9._-]+-bin-win-cpu-x64\.zip",
        r"llama-[A-Za-z0-9._-]+-bin-win-avx2-x64\.zip",
    ),
}


class EngineError(RuntimeError):
    """An engine operation failed; ``status`` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.CONFLICT):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- catalog


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    family: str
    purpose: str  # "chat" | "code"
    params: str
    quant: str
    repo: str
    filename: str
    size_bytes: int  # approximate; the real size comes from Hugging Face at install time
    min_ram_gb: float  # total RAM (GiB) needed for a comfortable experience
    license: str
    description: str
    rank: int  # higher = preferred when it fits


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        "qwen2.5-0.5b-instruct", "Qwen2.5 0.5B Instruct", "Qwen2.5", "chat", "0.5B", "Q4_K_M",
        "bartowski/Qwen2.5-0.5B-Instruct-GGUF", "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf", 398_000_000, 2, "Apache-2.0",
        "Tiny and very fast. Good for short answers and simple rewrites on machines with little RAM; "
        "makes mistakes on harder questions.", 10,
    ),
    CatalogEntry(
        "qwen2.5-1.5b-instruct", "Qwen2.5 1.5B Instruct", "Qwen2.5", "chat", "1.5B", "Q4_K_M",
        "bartowski/Qwen2.5-1.5B-Instruct-GGUF", "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf", 986_000_000, 3, "Apache-2.0",
        "Small multilingual assistant (including German). Fast on almost any computer.", 20,
    ),
    CatalogEntry(
        "qwen2.5-coder-1.5b-instruct", "Qwen2.5 Coder 1.5B Instruct", "Qwen2.5-Coder", "code", "1.5B", "Q4_K_M",
        "bartowski/Qwen2.5-Coder-1.5B-Instruct-GGUF", "Qwen2.5-Coder-1.5B-Instruct-Q4_K_M.gguf", 986_000_000, 3,
        "Apache-2.0", "Small coding model for quick code questions and snippets.", 20,
    ),
    CatalogEntry(
        "phi-3.5-mini-instruct", "Phi-3.5 mini Instruct", "Phi-3.5", "chat", "3.8B", "Q4_K_M",
        "bartowski/Phi-3.5-mini-instruct-GGUF", "Phi-3.5-mini-instruct-Q4_K_M.gguf", 2_390_000_000, 6, "MIT",
        "Microsoft's compact model with strong reasoning for its size; best in English.", 30,
    ),
    CatalogEntry(
        "qwen3-4b", "Qwen3 4B", "Qwen3", "chat", "4B", "Q4_K_M",
        "Qwen/Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf", 2_500_000_000, 6, "Apache-2.0",
        "Capable multilingual model for 8 GB machines. Thinks step by step before answering "
        "(better reasoning, slower answers).", 40,
    ),
    CatalogEntry(
        "qwen2.5-7b-instruct", "Qwen2.5 7B Instruct", "Qwen2.5", "chat", "7B", "Q4_K_M",
        "bartowski/Qwen2.5-7B-Instruct-GGUF", "Qwen2.5-7B-Instruct-Q4_K_M.gguf", 4_680_000_000, 10, "Apache-2.0",
        "Well-rounded multilingual assistant (including German) for writing, summaries and questions.", 60,
    ),
    CatalogEntry(
        "qwen2.5-coder-7b-instruct", "Qwen2.5 Coder 7B Instruct", "Qwen2.5-Coder", "code", "7B", "Q4_K_M",
        "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF", "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf", 4_680_000_000, 10,
        "Apache-2.0", "Strong coding model for explaining, writing and fixing code.", 60,
    ),
    CatalogEntry(
        "qwen3-8b", "Qwen3 8B", "Qwen3", "chat", "8B", "Q4_K_M",
        "Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", 5_030_000_000, 12, "Apache-2.0",
        "Stronger reasoning model. Thinks step by step before answering, so replies take longer.", 55,
    ),
    CatalogEntry(
        "qwen2.5-14b-instruct", "Qwen2.5 14B Instruct", "Qwen2.5", "chat", "14B", "Q4_K_M",
        "bartowski/Qwen2.5-14B-Instruct-GGUF", "Qwen2.5-14B-Instruct-Q4_K_M.gguf", 8_990_000_000, 20, "Apache-2.0",
        "High-quality assistant for machines with 24 GB RAM or more.", 80,
    ),
    CatalogEntry(
        "qwen2.5-coder-14b-instruct", "Qwen2.5 Coder 14B Instruct", "Qwen2.5-Coder", "code", "14B", "Q4_K_M",
        "bartowski/Qwen2.5-Coder-14B-Instruct-GGUF", "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf", 8_990_000_000, 20,
        "Apache-2.0", "High-quality coding model for machines with 24 GB RAM or more.", 80,
    ),
)
_BY_ID = {entry.id: entry for entry in CATALOG}


def get_entry(model_id: object) -> CatalogEntry:
    if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id) or model_id not in _BY_ID:
        raise EngineError(f"Unknown model {str(model_id)[:64]!r}", HTTPStatus.NOT_FOUND)
    return _BY_ID[model_id]


def total_ram() -> int:
    return int(psutil.virtual_memory().total)


def model_fits(entry: CatalogEntry, total_bytes: int) -> bool:
    return entry.min_ram_gb * GIB <= total_bytes and entry.size_bytes <= total_bytes * (1 - RAM_HEADROOM)


def recommended_model_id(total_bytes: int | None = None) -> str:
    """Best chat model that fits this machine's RAM with ~40 % headroom."""
    total = total_ram() if total_bytes is None else total_bytes
    chat = [entry for entry in CATALOG if entry.purpose == "chat"]
    fits = [entry for entry in chat if model_fits(entry, total)]
    if not fits:
        return min(chat, key=lambda e: e.size_bytes).id
    return max(fits, key=lambda e: (e.rank, -e.size_bytes)).id


def model_path(entry: CatalogEntry) -> Path:
    return config.models_dir() / entry.filename


def is_installed(entry: CatalogEntry) -> bool:
    path = model_path(entry)
    return path.is_file() and not path.is_symlink()


def _sidecar(entry: CatalogEntry) -> Path:
    return config.models_dir() / f"{entry.id}.json"


def catalog() -> list[dict]:
    total = total_ram()
    recommended = recommended_model_id(total)
    models = []
    for entry in CATALOG:
        installed = is_installed(entry)
        size = model_path(entry).stat().st_size if installed else entry.size_bytes
        models.append(
            {
                "id": entry.id, "name": entry.name, "family": entry.family, "purpose": entry.purpose,
                "params": entry.params, "quant": entry.quant, "size_bytes": size, "min_ram_gb": entry.min_ram_gb,
                "recommended": entry.id == recommended, "installed": installed, "license": entry.license,
                "description": entry.description,
            }
        )
    return models


# --------------------------------------------------------------------------- runtime


def external() -> bool:
    """``LOWRAM_LLM_URL`` is set: the user runs their own server; never start ours."""
    return bool(os.environ.get("LOWRAM_LLM_URL", "").strip())


def runtime_root() -> Path:
    path = config.bin_dir() / "llama.cpp"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_server_binary(folder: Path, max_depth: int = 4) -> Path | None:
    """Shallowest regular (non-symlink) ``llama-server`` file below ``folder``."""
    best: tuple[int, Path] | None = None
    base_depth = len(folder.parts)
    for current, dirs, files in os.walk(folder, followlinks=False):
        depth = len(Path(current).parts) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        if RUNTIME_NAME in files:
            candidate = Path(current) / RUNTIME_NAME
            if not candidate.is_symlink() and candidate.is_file() and (best is None or depth < best[0]):
                best = (depth, candidate)
    return best[1] if best else None


def _managed_runtime() -> Path | None:
    root = runtime_root()
    pointer = root / "current.json"
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        tag, rel = data["tag"], data["path"]
        if isinstance(tag, str) and TAG_RE.fullmatch(tag) and isinstance(rel, str) and ".." not in Path(rel).parts:
            candidate = root / tag / rel
            resolved, root_resolved = candidate.resolve(), root.resolve()
            if root_resolved in resolved.parents and _is_executable(candidate):
                return candidate
    except (OSError, ValueError, KeyError, TypeError):
        pass
    folders = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and TAG_RE.fullmatch(p.name)]
    for folder in sorted(folders, key=lambda p: p.stat().st_mtime, reverse=True):
        found = find_server_binary(folder)
        if found and _is_executable(found):
            return found
    return None


def discover_runtime() -> tuple[Path | None, str | None]:
    """``(path, source)`` with source ``path``/``homebrew``/``managed``, or ``(None, None)``."""
    found = shutil.which("llama-server")
    if found:
        path = Path(found).absolute()
        return path, ("homebrew" if path.parent in HOMEBREW_DIRS else "path")
    for folder in HOMEBREW_DIRS:
        candidate = folder / RUNTIME_NAME
        if _is_executable(candidate):
            return candidate, "homebrew"
    managed = _managed_runtime()
    if managed is not None:
        return managed, "managed"
    return None, None


def _child_env(binary: Path) -> dict[str, str]:
    env = dict(os.environ)
    if sys.platform.startswith("linux"):  # release builds ship their shared libraries next to the binary
        env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [str(binary.parent), env.get("LD_LIBRARY_PATH")]))
    return env


_version_cache: dict[tuple[str, int, int], str | None] = {}
_version_lock = threading.Lock()


def runtime_version(path: Path) -> str | None:
    """First ``version:`` line of ``llama-server --version`` (cached per binary; failures -> None)."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _version_lock:
        if key in _version_cache:
            return _version_cache[key]
    version = None
    try:
        proc = subprocess.run(
            [str(path), "--version"], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=VERSION_TIMEOUT_S, env=_child_env(path), check=False,
        )
        text = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", "replace")
        match = re.search(r"version:?\s*([^\r\n]+)", text, re.IGNORECASE)
        if match:
            version = re.sub(r"[\x00-\x1f\x7f]", "", match.group(1)).strip()[:120] or None
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Could not read the llama-server version: %s", exc)
    with _version_lock:
        _version_cache[key] = version
    return version


def runtime_info() -> dict:
    path, source = discover_runtime()
    return {
        "installed": path is not None,
        "path": str(path) if path else None,
        "version": runtime_version(path) if path else None,
        "source": source,
    }


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    system = system or sys.platform
    machine = (machine or platform.machine() or "").lower()
    arm = machine in ("arm64", "aarch64")
    x64 = machine in ("x86_64", "amd64", "x64")
    if system == "darwin" and (arm or x64):
        return "macos-arm64" if arm else "macos-x64"
    if system.startswith("linux") and (arm or x64):
        return "linux-arm64" if arm else "linux-x64"
    if system in ("win32", "cygwin") and x64:
        return "windows-x64"
    raise EngineError(
        f"There is no prebuilt llama.cpp for this computer ({system}/{machine or 'unknown'}). "
        "Install llama-server yourself (for example by building llama.cpp) and put it in your PATH."
    )


def select_asset(release: Any, key: str) -> tuple[str, dict]:
    """Pick ``(tag, asset)`` for ``key`` from a GitHub release JSON object."""
    if not isinstance(release, dict):
        raise EngineError("GitHub returned an unexpected answer for the llama.cpp release", HTTPStatus.BAD_GATEWAY)
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise EngineError("The llama.cpp release has an invalid tag name", HTTPStatus.BAD_GATEWAY)
    assets = [a for a in release.get("assets") or [] if isinstance(a, dict) and isinstance(a.get("name"), str)]
    for pattern in ASSET_PATTERNS.get(key, ()):
        matches = [a for a in assets if re.fullmatch(pattern, a["name"])]
        if matches:
            matches.sort(key=lambda a: 0 if a["name"].endswith(".tar.gz") else 1)
            return tag, matches[0]
    raise EngineError(f"The llama.cpp release {tag} has no download for this computer ({key}).")


def _runtime_installed(tag: str) -> Callable[[Path], None]:
    def finish(folder: Path) -> None:
        binary = find_server_binary(folder)
        if binary is None:
            raise EngineError(f"The llama.cpp {tag} download does not contain {RUNTIME_NAME}")
        os.chmod(binary, 0o755)
        artifacts.write_json_atomic(
            runtime_root() / "current.json", {"tag": tag, "path": binary.relative_to(folder).as_posix()}
        )
        log.info("llama.cpp %s installed at %s", tag, binary)

    return finish


def install_runtime() -> dict:
    """Download the latest llama.cpp release for this platform (202 + Download)."""
    current = downloads.get(RUNTIME_JOB_ID)
    if current and current["state"] in downloads.ACTIVE_STATES:
        return current
    key = platform_key()
    try:
        release = downloads.fetch_json(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
    except downloads.DownloadError as exc:
        raise EngineError(f"Could not read the latest llama.cpp release from GitHub: {exc}", HTTPStatus.BAD_GATEWAY) from None
    tag, asset = select_asset(release, key)
    dest = runtime_root() / tag
    if find_server_binary(dest) if dest.is_dir() else None:
        raise EngineError(f"llama.cpp {tag} is already installed")
    url = asset.get("browser_download_url")
    size = asset.get("size")
    digest = asset.get("digest")
    sha256 = None
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        sha256 = digest.split(":", 1)[1]
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        size = None
    try:
        return downloads.start(
            RUNTIME_JOB_ID, url, dest, sha256=sha256, size=size, label=f"llama.cpp {tag} ({key})", kind="runtime",
            extract="zip" if asset["name"].endswith(".zip") else "tar.gz", on_complete=_runtime_installed(tag),
        )
    except ValueError as exc:
        raise EngineError(f"The llama.cpp release cannot be downloaded safely: {exc}", HTTPStatus.BAD_GATEWAY) from None


# --------------------------------------------------------------------------- model install / delete


def _hf_file_meta(entry: CatalogEntry) -> tuple[str, int]:
    """``(sha256, size)`` of the catalog file from the Hugging Face API."""
    url: str | None = HF_TREE_URL.format(repo=entry.repo)
    try:
        for _page in range(MAX_HF_PAGES):
            if url is None:
                break
            items, url = downloads.fetch_json_with_next(url)
            if not isinstance(items, list):
                raise EngineError("Hugging Face returned an unexpected file list", HTTPStatus.BAD_GATEWAY)
            for item in items:
                if isinstance(item, dict) and item.get("path") == entry.filename:
                    lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
                    sha, size = lfs.get("oid"), lfs.get("size")
                    if (
                        isinstance(sha, str) and downloads.SHA256_RE.fullmatch(sha)
                        and isinstance(size, int) and not isinstance(size, bool) and size > 0
                    ):
                        return sha.lower(), size
                    raise EngineError(
                        f"Hugging Face reports no SHA-256 for {entry.filename}; refusing an unverified download",
                        HTTPStatus.BAD_GATEWAY,
                    )
    except downloads.DownloadError as exc:
        raise EngineError(f"Could not read the model details from Hugging Face: {exc}", HTTPStatus.BAD_GATEWAY) from None
    raise EngineError(f"{entry.filename} was not found in {entry.repo} on Hugging Face", HTTPStatus.BAD_GATEWAY)


def _model_installed(entry: CatalogEntry, sha256: str, size: int) -> Callable[[Path], None]:
    def finish(path: Path) -> None:
        artifacts.write_json_atomic(
            _sidecar(entry),
            {"id": entry.id, "repo": entry.repo, "filename": entry.filename, "sha256": sha256,
             "size_bytes": size, "installed_at": db.utcnow()},
        )
        if entry.purpose == "chat" and not settings.get("engine", "model_id"):
            settings.update("engine", {"model_id": entry.id})

    return finish


def install_model(model_id: str) -> dict:
    entry = get_entry(model_id)
    job_id = f"model-{entry.id}"
    current = downloads.get(job_id)
    if current and current["state"] in downloads.ACTIVE_STATES:
        return current
    if is_installed(entry):
        raise EngineError(f"{entry.name} is already installed")
    sha256, size = _hf_file_meta(entry)
    url = HF_RESOLVE_URL.format(repo=entry.repo, filename=quote(entry.filename))
    return downloads.start(
        job_id, url, model_path(entry), sha256=sha256, size=size, label=entry.name, kind="model",
        on_complete=_model_installed(entry, sha256, size),
    )


def delete_model(model_id: str) -> None:
    entry = get_entry(model_id)
    server = _manager.snapshot()
    if server["model_id"] == entry.id and server["state"] in ("starting", "ready"):
        raise EngineError(f"{entry.name} is loaded; stop the engine before deleting it")
    current = downloads.get(f"model-{entry.id}")
    if current and current["state"] in downloads.ACTIVE_STATES:
        raise EngineError(f"{entry.name} is being downloaded; cancel the download first")
    path = model_path(entry)
    leftovers = [p for p in (path, Path(str(path) + ".part"), _sidecar(entry)) if p.exists() or p.is_symlink()]
    if not leftovers:
        raise EngineError(f"{entry.name} is not installed", HTTPStatus.NOT_FOUND)
    for item in leftovers:
        item.unlink()
    if settings.get("engine", "model_id") == entry.id:
        settings.update("engine", {"model_id": None})


# --------------------------------------------------------------------------- server


def default_ctx_size(total_bytes: int | None = None) -> int:
    total_gib = (total_ram() if total_bytes is None else total_bytes) / GIB
    return 4096 if total_gib < 12 else 8192 if total_gib < 24 else 16384


def ctx_size() -> int:
    configured = settings.get("engine", "ctx_size")
    if isinstance(configured, int) and not isinstance(configured, bool) and 512 <= configured <= 131072:
        return configured
    return default_ctx_size()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_port() -> int:
    configured = settings.get("engine", "port")
    if configured is not None:
        if isinstance(configured, bool) or not isinstance(configured, int) or not 1024 <= configured <= 65535:
            raise EngineError(f"The engine port setting must be a number from 1024 to 65535, not {configured!r}")
        if not _port_free(configured):
            raise EngineError(f"Port {configured} is already in use; pick another engine port in Settings or leave it empty")
        return configured
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def logs_dir() -> Path:
    path = config.home_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def log_path() -> Path:
    return logs_dir() / "llama-server.log"


def _pidfile() -> Path:
    return logs_dir() / "llama-server.pid"


def log_tail(lines: int = LOG_TAIL_LINES, path: Path | None = None) -> str:
    path = path or log_path()
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 64 * 1024))
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", data)
    return "\n".join([line for line in text.splitlines() if line.strip()][-lines:])


def _probe_health(port: int) -> int | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=HEALTH_REQUEST_TIMEOUT_S) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return int(exc.code)
    except (OSError, ValueError):
        return None


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill() if sig == getattr(signal, "SIGKILL", None) else proc.terminate()
        else:
            os.killpg(proc.pid, sig)  # start_new_session=True: pgid == pid
    except ProcessLookupError:
        pass
    except PermissionError:
        proc.send_signal(sig)


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        _signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            log.error("llama-server (pid %s) did not exit after SIGKILL", proc.pid)


def _exit_reason(code: int | None) -> str:
    if code is None:
        return "stopped"
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = f"signal {-code}"
        hint = " (often: the computer ran out of memory)" if name == "SIGKILL" else ""
        return f"was killed by {name}{hint}"
    return f"exited with code {code}"


def _reap_orphan() -> None:
    """Stop a llama-server left behind by a LowRAM AI process that crashed."""
    path = _pidfile()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid, created, owner = int(data["pid"]), float(data["create_time"]), str(data["owner"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    if db._owner_alive(owner):
        return  # this process or another live LowRAM AI instance manages it
    try:
        proc = psutil.Process(pid)
        names = [Path(arg).name for arg in proc.cmdline()[:3]]  # [python3, script] for wrapper scripts
        if abs(proc.create_time() - created) < 1.0 and any(n.startswith("llama-server") for n in names):
            log.warning("Stopping orphaned llama-server (pid %s) from an earlier run", pid)
            proc.terminate()
            try:
                proc.wait(STOP_GRACE_S)
            except psutil.TimeoutExpired:
                proc.kill()
    except (psutil.Error, OSError):
        pass
    try:
        path.unlink()
    except OSError:
        pass


class EngineManager:
    """Owns the one managed llama-server process. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._op = threading.RLock()  # serialises start/stop/pause
        self._proc: subprocess.Popen | None = None
        self._gen = 0
        self._paused = 0
        self._resume_model: str | None = None
        self._url: str | None = None
        self._info = self._idle()

    @staticmethod
    def _idle() -> dict:
        return {"state": "stopped", "model_id": None, "port": None, "pid": None, "ctx_size": None,
                "error": None, "started_at": None}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._info)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused > 0

    def _pick_model(self, model_id: str | None) -> CatalogEntry:
        if model_id is not None:
            entry = get_entry(model_id)
            if not is_installed(entry):
                raise EngineError(f"{entry.name} is not installed yet")
            return entry
        selected = settings.get("engine", "model_id")
        if isinstance(selected, str) and selected in _BY_ID and is_installed(_BY_ID[selected]):
            return _BY_ID[selected]
        installed = [e for e in CATALOG if is_installed(e)]
        if not installed:
            raise EngineError("No model is installed yet. Install a model under Models & Settings first.")
        best = recommended_model_id()
        installed.sort(key=lambda e: (e.id != best, e.purpose != "chat", -e.rank))
        return installed[0]

    def start(self, model_id: str | None = None, *, persist: bool = True) -> dict:
        with self._op:
            if external():
                raise EngineError("LOWRAM_LLM_URL is set, so LowRAM AI uses that server and does not start its own")
            if self.paused:
                raise EngineError("The engine is paused while an image or video job uses the memory; try again afterwards")
            entry = self._pick_model(model_id)
            binary, _source = discover_runtime()
            if binary is None:
                raise EngineError("The llama.cpp runtime is not installed yet. Install it under Models & Settings.")
            with self._lock:
                if self._info["state"] in ("starting", "ready") and self._info["model_id"] == entry.id:
                    return dict(self._info)
                running = self._proc is not None
            if running:
                self._stop_locked()
            _reap_orphan()
            port, ctx = choose_port(), ctx_size()
            path = log_path()
            if path.exists():
                os.replace(path, path.with_name(path.name + ".1"))
            args = [str(binary), "-m", str(model_path(entry)), "--host", "127.0.0.1", "--port", str(port), "-c", str(ctx)]
            log_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                kwargs: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {
                    "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                proc = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                    cwd=str(config.home_dir()), env=_child_env(binary), close_fds=True, **kwargs,
                )
            except OSError as exc:
                with self._lock:
                    self._info = {**self._idle(), "state": "error", "model_id": entry.id,
                                  "error": f"Could not start llama-server: {exc}"}
                    self._cond.notify_all()
                raise EngineError(f"Could not start llama-server: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR) from None
            finally:
                os.close(log_fd)
            with self._lock:
                self._gen += 1
                gen = self._gen
                self._proc = proc
                self._info = {"state": "starting", "model_id": entry.id, "port": port, "pid": proc.pid,
                              "ctx_size": ctx, "error": None, "started_at": db.utcnow()}
                self._cond.notify_all()
            try:
                create_time = psutil.Process(proc.pid).create_time()
                artifacts.write_json_atomic(
                    _pidfile(), {"pid": proc.pid, "create_time": create_time, "owner": db.owner_id()})
            except (psutil.Error, OSError):
                pass
            log.info("Starting llama-server (pid %s) with %s on port %s, context %s", proc.pid, entry.id, port, ctx)
            if persist and model_id is not None:
                settings.update("engine", {"model_id": entry.id})
            threading.Thread(target=self._watch, args=(proc, gen, port, path), name="lowram-engine-watch",
                             daemon=True).start()
            return self.snapshot()

    def _watch(self, proc: subprocess.Popen, gen: int, port: int, path: Path) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while True:
            if proc.poll() is not None:
                return self._on_exit(proc, gen, path)
            with self._lock:
                if gen != self._gen:
                    return
            if _probe_health(port) == 200 and proc.poll() is None:
                break
            if time.monotonic() > deadline:
                with self._lock:
                    if gen != self._gen:
                        return
                    self._gen += 1
                    self._proc = None
                    self._info.update(state="error", pid=None, error=(
                        f"llama-server did not finish loading within {HEALTH_TIMEOUT_S:g} s.\n{log_tail(path=path)}"
                    ).strip())
                    self._cond.notify_all()
                _terminate(proc)
                return
            try:
                proc.wait(HEALTH_POLL_S)
            except subprocess.TimeoutExpired:
                pass
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        with self._lock:
            if gen != self._gen or self._info["state"] != "starting":
                return
            self._info["state"] = "ready"
            self._url = url
            config.set_runtime_llm_url(url)
            self._cond.notify_all()
        log.info("llama-server is ready on port %s", port)
        proc.wait()
        self._on_exit(proc, gen, path)

    def _on_exit(self, proc: subprocess.Popen, gen: int, path: Path) -> None:
        with self._lock:
            if gen != self._gen:
                return  # an intentional stop/restart
            self._gen += 1
            self._proc = None
            self._clear_url()
            tail = log_tail(path=path)
            self._info.update(state="error", pid=None, error=(
                f"llama-server {_exit_reason(proc.returncode)}." + (f" Last log lines:\n{tail}" if tail else "")
            )[:4000])
            self._cond.notify_all()
        log.warning("llama-server %s", _exit_reason(proc.returncode))
        try:
            _pidfile().unlink()
        except OSError:
            pass

    def _clear_url(self) -> None:
        if self._url is not None:
            config.set_runtime_llm_url(None)
            self._url = None

    def _stop_locked(self) -> None:
        """Stop the process (caller holds ``_op``)."""
        with self._lock:
            proc, self._proc = self._proc, None
            self._gen += 1
            self._clear_url()
        if proc is not None:
            _terminate(proc)
            try:
                _pidfile().unlink()
            except OSError:
                pass
            log.info("llama-server stopped")
        with self._lock:
            self._info = self._idle()
            self._cond.notify_all()

    def stop(self) -> dict:
        with self._op:
            self._stop_locked()
            return self.snapshot()

    def ensure_running(self, timeout: float = HEALTH_TIMEOUT_S, model_id: str | None = None) -> str:
        if external():
            return config.llm_url()
        with self._lock:
            info = dict(self._info)
        if (model_id is not None and info["model_id"] != model_id) or info["state"] not in ("starting", "ready"):
            info = self.start(model_id, persist=False)
        deadline = time.monotonic() + timeout
        with self._cond:
            gen = self._gen
            while True:
                state = self._info["state"]
                if state == "ready" and self._url:
                    return self._url
                if state == "error":
                    raise EngineError(self._info["error"] or "llama-server failed to start", HTTPStatus.SERVICE_UNAVAILABLE)
                if state == "stopped" or self._gen != gen:
                    raise EngineError("llama-server was stopped while loading", HTTPStatus.SERVICE_UNAVAILABLE)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EngineError(f"llama-server is still loading after {timeout:g} s", HTTPStatus.SERVICE_UNAVAILABLE)
                self._cond.wait(min(remaining, 1.0))

    @contextmanager
    def pause_for_heavy_job(self) -> Iterator[None]:
        if external():
            yield
            return
        with self._op:
            with self._lock:
                self._paused += 1
                outer = self._paused == 1
                if outer:
                    running = self._info["state"] in ("starting", "ready")
                    self._resume_model = self._info["model_id"] if running else None
                stop_now = outer and self._resume_model is not None
            if stop_now:
                log.info("Pausing llama-server for a heavy job")
                self._stop_locked()
        try:
            yield
        finally:
            with self._lock:
                self._paused -= 1
                resume = self._resume_model if self._paused == 0 else None
                if self._paused == 0:
                    self._resume_model = None
            if resume:
                threading.Thread(target=self._restart_quietly, args=(resume,), name="lowram-engine-resume",
                                 daemon=True).start()

    def _restart_quietly(self, model_id: str) -> None:
        try:
            self.start(model_id, persist=False)
        except EngineError as exc:
            log.warning("Could not restart llama-server after the heavy job: %s", exc)

    def kill_now(self) -> None:
        """atexit: never leave an orphaned llama-server behind."""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            _signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                _signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))


_manager = EngineManager()
atexit.register(lambda: _manager.kill_now())


# --------------------------------------------------------------------------- public Python API


def status() -> dict:
    return {
        "runtime": runtime_info(),
        "server": _manager.snapshot(),
        "external": external(),
        "llm_url": llm.redact_url(config.llm_url()),
        "paused": _manager.paused,
    }


def start(model_id: str | None = None, *, persist: bool = True) -> dict:
    return _manager.start(model_id, persist=persist)


def stop() -> dict:
    return _manager.stop()


def ensure_running(timeout: float = HEALTH_TIMEOUT_S, model_id: str | None = None) -> str:
    """URL of a ready chat endpoint (external, or the managed server – started if needed)."""
    return _manager.ensure_running(timeout, model_id)


@contextmanager
def pause_for_heavy_job() -> Iterator[None]:
    """Stop the managed llama-server for a RAM-heavy job and restart it afterwards."""
    with _manager.pause_for_heavy_job():
        yield


# --------------------------------------------------------------------------- hooks


def _auto_start() -> None:
    try:
        _reap_orphan()
        if external() or not settings.get("engine", "auto_start"):
            return
        selected = settings.get("engine", "model_id")
        entry = _BY_ID.get(selected) if isinstance(selected, str) else None
        if entry is None or not is_installed(entry) or discover_runtime()[0] is None:
            return
        start(entry.id, persist=False)
    except EngineError as exc:
        log.warning("Engine auto-start skipped: %s", exc)
    except Exception:  # noqa: BLE001 - never break app startup
        log.exception("Engine auto-start failed")


@web.on_startup
def _on_startup(server: Any) -> None:
    if external():
        return
    threading.Thread(target=_auto_start, name="lowram-engine-autostart", daemon=True).start()


@web.on_shutdown
def _on_shutdown(server: Any) -> None:
    stop()


# --------------------------------------------------------------------------- routes


def _api(func: Callable[..., Response]) -> Callable[..., Response]:
    @functools.wraps(func)
    def wrapper(req: Request, *args: Any) -> Response:
        try:
            return func(req, *args)
        except EngineError as exc:
            raise HTTPError(exc.status, str(exc)) from None

    return wrapper


def _path_id(req: Request) -> str:
    return req.path.split("/")[4]  # /api/engine/models/<id>/...; raw string, not int-converted


@route("GET", r"/api/engine/status")
@_api
def route_status(req: Request) -> Response:
    return Response(status())


@route("GET", r"/api/engine/catalog")
@_api
def route_catalog(req: Request) -> Response:
    return Response({"models": catalog(), "ram_total_bytes": total_ram()})


@route("POST", r"/api/engine/runtime/install")
@_api
def route_install_runtime(req: Request) -> Response:
    req.json()
    return Response({"download": install_runtime()}, HTTPStatus.ACCEPTED)


@route("POST", r"/api/engine/models/([a-z0-9][a-z0-9._-]{0,63})/install")
@_api
def route_install_model(req: Request, _model_id: object) -> Response:
    req.json()
    return Response({"download": install_model(_path_id(req))}, HTTPStatus.ACCEPTED)


@route("POST", r"/api/engine/models/([a-z0-9][a-z0-9._-]{0,63})/delete")
@_api
def route_delete_model(req: Request, _model_id: object) -> Response:
    req.json()
    delete_model(_path_id(req))
    return Response({"deleted": True})


@route("POST", r"/api/engine/start")
@_api
def route_start(req: Request) -> Response:
    model_id = req.json().get("model_id")
    if model_id is not None and (not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id)):
        raise HTTPError(HTTPStatus.BAD_REQUEST, "model_id must be a catalog id")
    return Response({"server": start(model_id)}, HTTPStatus.ACCEPTED)


@route("POST", r"/api/engine/stop")
@_api
def route_stop(req: Request) -> Response:
    req.json()
    return Response({"server": stop()})
