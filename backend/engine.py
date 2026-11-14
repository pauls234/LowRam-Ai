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
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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
from urllib.parse import quote, unquote, urlparse

import psutil

from . import artifacts, config, db, downloads, llm, settings, web
from .web import HTTPError, Request, Response, route

log = logging.getLogger("lowram.engine")

GIB = 1024**3
MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
TAG_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
RAM_HEADROOM = 0.40  # leave 40 % of the RAM to the OS and other apps

RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
RELEASE_DOWNLOAD_PREFIX = "https://github.com/ggml-org/llama.cpp/releases/download/"
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
    source: str = "catalog"  # "catalog" | "custom" (own Hugging Face download) | "local" (imported file)
    files: tuple[str, ...] = ()  # all parts of a split model, the first is loaded; () = (filename,)
    local_path: str | None = None  # imported models: where the file lives (never copied or deleted)
    revision: str = "main"

    @property
    def all_files(self) -> tuple[str, ...]:
        return self.files or (self.filename,)


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


def all_entries() -> list[CatalogEntry]:
    return [*CATALOG, *custom_entries()]


def find_entry(model_id: object) -> CatalogEntry | None:
    if not isinstance(model_id, str) or not MODEL_ID_RE.fullmatch(model_id):
        return None
    if model_id in _BY_ID:
        return _BY_ID[model_id]
    return next((entry for entry in custom_entries() if entry.id == model_id), None)


def get_entry(model_id: object) -> CatalogEntry:
    entry = find_entry(model_id)
    if entry is None:
        raise EngineError(f"Unknown model {str(model_id)[:64]!r}", HTTPStatus.NOT_FOUND)
    return entry


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


def model_files(entry: CatalogEntry) -> list[Path]:
    if entry.local_path:
        return [Path(entry.local_path)]
    folder = Path(config.models_dir())
    return [folder / Path(name).name for name in entry.all_files]


def model_path(entry: CatalogEntry) -> Path:
    """The file llama-server loads (the first part of a split model)."""
    return model_files(entry)[0]


def _file_ok(path: Path, *, allow_symlink: bool = False) -> bool:
    try:
        if path.is_symlink() and not allow_symlink:
            return False
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def is_available(entry: CatalogEntry) -> bool:
    """All files are there right now (false e.g. while the external drive is unplugged)."""
    if entry.source == "local":
        return _file_ok(Path(entry.local_path or ""), allow_symlink=True)
    return all(_file_ok(path) for path in model_files(entry))


def is_installed(entry: CatalogEntry) -> bool:
    """Catalog models: downloaded. Own models: registered (even while their drive is unplugged)."""
    return entry.source != "catalog" or is_available(entry)


def _unavailable_error(entry: CatalogEntry) -> EngineError:
    if entry.source != "catalog" or not Path(config.models_dir()).is_dir():
        return EngineError(f"The drive with this model is not connected ({model_path(entry)} is missing)")
    return EngineError(f"{entry.name} is not installed yet")


def _sidecar(entry: CatalogEntry) -> Path:
    return config.models_dir() / f"{entry.id}.json"


def model_dict(entry: CatalogEntry, total: int | None = None, recommended: str | None = None) -> dict:
    total = total_ram() if total is None else total
    available = is_available(entry)
    size = entry.size_bytes
    if available:
        try:
            size = sum(path.stat().st_size for path in model_files(entry))
        except OSError:
            pass
    return {
        "id": entry.id, "name": entry.name, "family": entry.family, "purpose": entry.purpose,
        "params": entry.params, "quant": entry.quant, "size_bytes": size, "min_ram_gb": entry.min_ram_gb,
        "recommended": entry.id == recommended, "installed": entry.source != "catalog" or available,
        "license": entry.license, "description": entry.description, "source": entry.source,
        "path": str(model_path(entry)) if entry.source != "catalog" or available else None,
        "available": available, "fits_ram": model_fits(entry, total),
    }


def catalog() -> list[dict]:
    """Built-in catalog plus the user's own models. Never fails because a drive is unplugged."""
    total = total_ram()
    recommended = recommended_model_id(total)
    return [model_dict(entry, total, recommended) for entry in all_entries()]


# --------------------------------------------------------------------------- runtime


def external() -> bool:
    """``LOWRAM_LLM_URL`` is set: the user runs their own server; never start ours."""
    return bool(os.environ.get("LOWRAM_LLM_URL", "").strip())


def runtime_root() -> Path:
    path = config.bin_dir() / "llama.cpp"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _runtime_root_path() -> Path:
    """The managed runtime folder, without creating anything (status polling)."""
    return config.home_dir() / "bin" / "llama.cpp"


def _valid_tag_dir(name: str) -> bool:
    return bool(TAG_RE.fullmatch(name)) and not name.startswith(".")  # ".x.extract-…" = leftover staging


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
    root = _runtime_root_path()
    if not root.is_dir():
        return None
    pointer = root / "current.json"
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        tag, rel = data["tag"], data["path"]
        if isinstance(tag, str) and _valid_tag_dir(tag) and isinstance(rel, str) and ".." not in Path(rel).parts:
            candidate = root / tag / rel
            resolved, root_resolved = candidate.resolve(), root.resolve()
            if root_resolved in resolved.parents and _is_executable(candidate):
                return candidate
    except (OSError, ValueError, KeyError, TypeError):
        pass
    folders = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and _valid_tag_dir(p.name)]
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


_SECRET_ENV_RE = re.compile(r"(?i)(key|token|secret|passw)")


def _is_managed(binary: Path) -> bool:
    try:
        return _runtime_root_path().resolve() in binary.resolve().parents
    except OSError:
        return False


def _child_env(binary: Path) -> dict[str, str]:
    """Environment for llama-server: no secrets; managed Linux builds find their own libraries."""
    env = {key: value for key, value in os.environ.items() if not _SECRET_ENV_RE.search(key)}
    if sys.platform.startswith("linux"):
        if getattr(sys, "frozen", False):  # PyInstaller points LD_LIBRARY_PATH at its bundle
            original = env.pop("LD_LIBRARY_PATH_ORIG", None)
            if original:
                env["LD_LIBRARY_PATH"] = original
            else:
                env.pop("LD_LIBRARY_PATH", None)
        if _is_managed(binary):  # release builds ship their shared libraries next to the binary
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
    if sha256 is None or not downloads.SHA256_RE.fullmatch(sha256):
        raise EngineError(
            f"The llama.cpp release {tag} has no checksum – refusing to install an unverified program",
            HTTPStatus.BAD_GATEWAY,
        )
    if not isinstance(url, str) or not url.startswith(RELEASE_DOWNLOAD_PREFIX):
        raise EngineError("The llama.cpp release points to an unexpected download address", HTTPStatus.BAD_GATEWAY)
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
    if entry.source != "catalog":
        raise EngineError(f"{entry.name} is already one of your models")
    job_id = f"model-{entry.id}"
    current = downloads.get(job_id)
    if current and current["state"] in downloads.ACTIVE_STATES:
        return current
    if is_installed(entry):
        raise EngineError(f"{entry.name} is already installed")
    _not_moving()
    _require_models_dir()
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
    prefix = f"model-{entry.id}"
    if any(d["id"] == prefix or d["id"].startswith(prefix + "-p") for d in downloads.list_jobs()
           if d["state"] in downloads.ACTIVE_STATES):
        raise EngineError(f"{entry.name} is being downloaded; cancel the download first")
    _not_moving()
    if entry.source == "local":
        _unregister(entry.id)  # an imported file is never deleted, only forgotten
    else:
        candidates = [*(q for p in model_files(entry) for q in (p, Path(str(p) + ".part"))), _sidecar(entry)]
        leftovers = [p for p in candidates if p.exists() or p.is_symlink()]
        if entry.source == "catalog" and not leftovers:
            raise EngineError(f"{entry.name} is not installed", HTTPStatus.NOT_FOUND)
        for item in leftovers:
            item.unlink()
        if entry.source == "custom":
            _unregister(entry.id)
    for key in ("model_id", "code_model_id"):
        if settings.get("engine", key) == entry.id:
            settings.update("engine", {key: None})


# --------------------------------------------------------------------------- own models (custom / local)

HF_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
HF_REVISION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
HF_PATH_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}")
HF_RESERVED = frozenset({"api", "datasets", "spaces", "docs", "models", "collections", "papers", "settings",
                         "organizations", "login", "join", "blog", "posts"})
HF_TREE_REV_URL = "https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true"
HF_RESOLVE_REV_URL = "https://huggingface.co/{repo}/resolve/{revision}/{path}"
MAX_HF_TREE_PAGES = 20
SPLIT_RE = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)
QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])(I?Q[1-8](?:_[0-9A-Za-z]{1,3})*|BF16|F16|F32|MXFP4|TQ[12]_0)(?![A-Za-z0-9])", re.IGNORECASE
)
PARAMS_RE = re.compile(r"(?<![A-Za-z0-9.])(\d+x)?(\d+(?:\.\d+)?)[Bb](?![A-Za-z0-9])")
PREFERRED_QUANTS = ("Q5_K_M", "Q4_K_M")
REGISTRY_FILE = "custom_models.json"
MAX_NAME_CHARS = 80
_registry_lock = threading.RLock()


def _min_ram_gb(size_bytes: int) -> float:
    return round(-(-size_bytes * 10 // int(GIB * (1 - RAM_HEADROOM))) / 10, 1)  # ceil to 0.1 GiB


def parse_quant(filename: str) -> str | None:
    name = Path(filename).name
    found = QUANT_RE.findall(name[: -len(".gguf")] if name.lower().endswith(".gguf") else name)
    return found[-1].upper() if found else None


def parse_params(filename: str) -> str:
    found = PARAMS_RE.findall(Path(filename).name)
    return f"{found[-1][0]}{found[-1][1]}B" if found else ""


def _stem(filename: str) -> str:
    return re.sub(r"(-\d{5}-of-\d{5})?\.gguf$", "", Path(filename).name, flags=re.IGNORECASE)


def _custom_id(prefix: str, stem: str, key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40].strip("-") or "model"
    return f"{prefix}-{slug}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"


def _clean_name(name: object, default: str) -> str:
    if name is None:
        return default[:MAX_NAME_CHARS]
    if not isinstance(name, str):
        raise EngineError("name must be text", HTTPStatus.BAD_REQUEST)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", name).strip()[:MAX_NAME_CHARS]
    return cleaned or default[:MAX_NAME_CHARS]


def _clean_purpose(purpose: object, filename: str) -> str:
    if purpose is None:
        return "code" if re.search(r"coder|code", filename, re.IGNORECASE) else "chat"
    if purpose not in ("chat", "code"):
        raise EngineError("purpose must be 'chat' or 'code'", HTTPStatus.BAD_REQUEST)
    return str(purpose)


def _registry_path() -> Path:
    return config.home_dir() / REGISTRY_FILE


def _load_registry() -> list[dict]:
    path = _registry_path()
    with _registry_lock:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            models = data.get("models") if isinstance(data, dict) else None
            if not isinstance(models, list):
                raise ValueError("no model list")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            backup = path.with_suffix(".json.corrupt")
            log.warning("%s is unreadable (%s); moved to %s", path.name, exc, backup.name)
            try:
                path.replace(backup)
            except OSError:
                pass
            return []
        return [m for m in models if isinstance(m, dict)]


def _save_registry(models: list[dict]) -> None:
    with _registry_lock:
        artifacts.write_json_atomic(_registry_path(), {"version": 1, "models": models})


def _entry_from_record(rec: dict) -> CatalogEntry | None:
    try:
        model_id, source = rec["id"], rec["source"]
        if not (isinstance(model_id, str) and MODEL_ID_RE.fullmatch(model_id) and model_id not in _BY_ID):
            return None
        if source not in ("custom", "local"):
            return None
        files = tuple(str(f) for f in (rec.get("files") or [rec["filename"]]))
        local = rec.get("path") if source == "local" else None
        if source == "custom" and not all(HF_PATH_PART_RE.fullmatch(f) for f in files):
            return None  # stored names are plain file names inside the models folder
        if source == "local" and not (isinstance(local, str) and os.path.isabs(local)):
            return None
        size = int(rec.get("size_bytes") or 0)
        purpose = rec.get("purpose") if rec.get("purpose") in ("chat", "code") else "chat"
        return CatalogEntry(
            model_id, str(rec.get("name") or model_id)[:MAX_NAME_CHARS], str(rec.get("family") or ""), purpose,
            str(rec.get("params") or ""), str(rec.get("quant") or ""), str(rec.get("repo") or ""), files[0], size,
            _min_ram_gb(size), str(rec.get("license") or "see the model card"), str(rec.get("description") or ""),
            0, source=source, files=files, local_path=local, revision=str(rec.get("revision") or "main"),
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def custom_entries() -> list[CatalogEntry]:
    entries = (_entry_from_record(rec) for rec in _load_registry())
    return [entry for entry in entries if entry is not None]


def _register(entry: CatalogEntry) -> None:
    record = {
        "id": entry.id, "source": entry.source, "name": entry.name, "family": entry.family, "purpose": entry.purpose,
        "params": entry.params, "quant": entry.quant, "repo": entry.repo, "revision": entry.revision,
        "filename": entry.filename, "files": list(entry.all_files), "path": entry.local_path,
        "size_bytes": entry.size_bytes, "license": entry.license, "description": entry.description,
        "added_at": db.utcnow(),
    }
    with _registry_lock:
        models = [m for m in _load_registry() if m.get("id") != entry.id]
        _save_registry([*models, record])


def _unregister(model_id: str) -> None:
    with _registry_lock:
        _save_registry([m for m in _load_registry() if m.get("id") != model_id])


def parse_hf_source(source: object) -> tuple[str, str, str | None]:
    """``owner/repo``, a huggingface.co repo/tree URL or a ``…/resolve/<rev>/<file>.gguf`` URL
    -> ``(repo, revision, file path or None)``."""
    bad = EngineError(
        "Enter a Hugging Face model as owner/name or a https://huggingface.co/… link", HTTPStatus.BAD_REQUEST
    )
    if not isinstance(source, str):
        raise bad
    text = source.strip()
    if not text or len(text) > 2048 or any(ord(c) < 32 for c in text):
        raise bad
    if "://" not in text:
        if not HF_REPO_RE.fullmatch(text) or text.split("/")[0].lower() in HF_RESERVED:
            raise bad
        return text, "main", None
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError:
        raise bad from None
    if (parsed.scheme != "https" or (parsed.hostname or "").lower() != "huggingface.co" or port not in (None, 443)
            or parsed.username is not None or parsed.password is not None):
        raise EngineError("Only https://huggingface.co links are supported", HTTPStatus.BAD_REQUEST)
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise bad
    repo, rest = f"{parts[0]}/{parts[1]}", parts[2:]
    if not HF_REPO_RE.fullmatch(repo) or parts[0].lower() in HF_RESERVED:
        raise bad
    if not rest:
        return repo, "main", None
    if len(rest) < 2 or rest[0] not in ("tree", "resolve", "blob") or not HF_REVISION_RE.fullmatch(rest[1]):
        raise bad
    if rest[0] == "tree":
        return repo, rest[1], None
    file_parts = rest[2:]
    if not file_parts or not all(HF_PATH_PART_RE.fullmatch(p) for p in file_parts):
        raise bad
    path = "/".join(file_parts)
    if not path.lower().endswith(".gguf"):
        raise EngineError("Only .gguf model files can be used with llama.cpp", HTTPStatus.BAD_REQUEST)
    return repo, rest[1], path


def _hf_tree(repo: str, revision: str) -> list[dict]:
    url: str | None = HF_TREE_REV_URL.format(repo=repo, revision=quote(revision, safe=""))
    items: list[dict] = []
    try:
        for _page in range(MAX_HF_TREE_PAGES):
            if url is None:
                break
            page, url = downloads.fetch_json_with_next(url)
            if not isinstance(page, list):
                raise EngineError("Hugging Face returned an unexpected file list", HTTPStatus.BAD_GATEWAY)
            items.extend(item for item in page if isinstance(item, dict))
    except downloads.HTTPStatusError as exc:
        if exc.code in (401, 403, 404):
            raise EngineError(f"{repo} was not found on Hugging Face (or it is private or gated)",
                              HTTPStatus.NOT_FOUND) from None
        raise EngineError(f"Could not list the files of {repo}: {exc}", HTTPStatus.BAD_GATEWAY) from None
    except downloads.DownloadError as exc:
        raise EngineError(f"Could not list the files of {repo}: {exc}", HTTPStatus.BAD_GATEWAY) from None
    return items


def gguf_groups(items: list[dict]) -> list[dict]:
    """GGUF files of a repo; split files (``-00001-of-0000N``) become one entry with all parts."""
    files: dict[str, dict] = {}
    for item in items:
        path = item.get("path")
        if item.get("type") != "file" or not isinstance(path, str) or not path.lower().endswith(".gguf"):
            continue
        parts = path.split("/")
        if not all(HF_PATH_PART_RE.fullmatch(p) for p in parts) or parts[-1].lower().startswith("mmproj"):
            continue  # odd names / vision projectors (not loadable as a model on their own)
        lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
        size = lfs.get("size", item.get("size"))
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            continue
        oid = lfs.get("oid")
        sha = oid.lower() if isinstance(oid, str) and downloads.SHA256_RE.fullmatch(oid) else None
        files[path] = {"path": path, "size": size, "sha256": sha}
    groups: list[dict] = []
    splits: dict[tuple[str, int], dict[int, dict]] = {}
    for path, info in files.items():
        match = SPLIT_RE.match(path)
        if match:
            splits.setdefault((match["stem"], int(match["total"])), {})[int(match["index"])] = info
        else:
            groups.append({"filename": path, "parts": [info]})
    for (_stem_name, total), parts in splits.items():
        if sorted(parts) == list(range(1, total + 1)):  # incomplete split sets are skipped
            groups.append({"filename": parts[1]["path"], "parts": [parts[i] for i in range(1, total + 1)]})
    for group in groups:
        group["size_bytes"] = sum(p["size"] for p in group["parts"])
        group["quant"] = parse_quant(group["filename"])
    groups.sort(key=lambda g: (g["size_bytes"], g["filename"]))
    return groups


def resolve_custom(source: object) -> dict:
    repo, revision, wanted = parse_hf_source(source)
    groups = gguf_groups(_hf_tree(repo, revision))
    if wanted is not None:
        groups = [g for g in groups if any(p["path"] == wanted for p in g["parts"])]
        if not groups:
            raise EngineError(f"{wanted} was not found in {repo}", HTTPStatus.NOT_FOUND)
    if not groups:
        raise EngineError(f"{repo} has no GGUF model files (llama.cpp needs GGUF)", HTTPStatus.NOT_FOUND)
    budget = total_ram() * (1 - RAM_HEADROOM)
    fits = [g for g in groups if g["size_bytes"] <= budget]
    preferred = [g for g in fits if g["quant"] in PREFERRED_QUANTS]
    pick = max(preferred or fits, key=lambda g: g["size_bytes"]) if fits else None
    return {
        "repo": repo,
        "revision": revision,
        "files": [
            {"filename": g["filename"], "size_bytes": g["size_bytes"], "quant": g["quant"],
             "fits_ram": g["size_bytes"] <= budget, "recommended": g is pick, "parts": len(g["parts"]),
             "verified": all(p["sha256"] for p in g["parts"])}
            for g in groups
        ],
    }


def _require_models_dir() -> Path:
    folder = Path(config.models_dir())
    if not folder.is_dir():
        raise EngineError(f"The models folder {folder} is not available (external drive not connected?)")
    return folder


def _not_moving() -> None:
    if _mover.active:
        raise EngineError("Models are being moved to the new folder; try again when that has finished")


def _check_free_space(folder: Path, needed: int) -> None:
    free = shutil.disk_usage(folder).free
    if free < needed + downloads.DISK_HEADROOM_BYTES:
        raise EngineError(
            f"Not enough disk space in {folder}: {needed / GIB:.1f} GiB needed plus 1 GiB reserve, "
            f"{free / GIB:.1f} GiB free"
        )


def install_custom(repo: object, filename: object, name: object = None, purpose: object = None,
                   revision: object = "main") -> tuple[dict, str]:
    """Download any GGUF model from Hugging Face (verified with its SHA-256). Returns (download, model_id)."""
    if not isinstance(repo, str) or not HF_REPO_RE.fullmatch(repo) or repo.split("/")[0].lower() in HF_RESERVED:
        raise EngineError("repo must look like owner/name", HTTPStatus.BAD_REQUEST)
    if not isinstance(revision, str) or not HF_REVISION_RE.fullmatch(revision):
        raise EngineError("Invalid revision", HTTPStatus.BAD_REQUEST)
    if (not isinstance(filename, str) or not filename.lower().endswith(".gguf")
            or not all(HF_PATH_PART_RE.fullmatch(p) for p in filename.split("/"))):
        raise EngineError("filename must be a .gguf file of the repository", HTTPStatus.BAD_REQUEST)
    _not_moving()
    folder = _require_models_dir()
    group = next((g for g in gguf_groups(_hf_tree(repo, revision))
                  if any(p["path"] == filename for p in g["parts"])), None)
    if group is None:
        raise EngineError(f"{filename} was not found in {repo}", HTTPStatus.NOT_FOUND)
    if not all(p["sha256"] for p in group["parts"]):
        raise EngineError(f"Hugging Face reports no SHA-256 for {filename}; refusing an unverified download",
                          HTTPStatus.BAD_GATEWAY)
    basenames = [Path(p["path"]).name for p in group["parts"]]
    model_id = _custom_id("custom", _stem(basenames[0]), f"{repo}@{revision}/{group['filename']}")
    count = len(basenames)
    job_ids = [f"model-{model_id}"] if count == 1 else [f"model-{model_id}-p{i:02d}" for i in range(1, count + 1)]
    running = [d for d in (downloads.get(j) for j in job_ids) if d and d["state"] in downloads.ACTIVE_STATES]
    if running:
        return running[0], model_id
    if find_entry(model_id) is not None:
        raise EngineError("This model is already installed")
    for base in basenames:
        if (folder / base).exists():
            raise EngineError(f"A file named {base} already exists in the models folder")
    _check_free_space(folder, group["size_bytes"])
    family = repo.split("/")[1]
    family = family[: -len("-GGUF")] if family.upper().endswith("-GGUF") else family
    entry = CatalogEntry(
        model_id, _clean_name(name, _stem(basenames[0])), family, _clean_purpose(purpose, filename),
        parse_params(basenames[0]), group["quant"] or "", repo, basenames[0], group["size_bytes"],
        _min_ram_gb(group["size_bytes"]), "see the model card", f"From huggingface.co/{repo}", 0,
        source="custom", files=tuple(basenames), revision=revision,
    )
    finish_lock = threading.Lock()

    def finish(_path: Path) -> None:
        with finish_lock:
            if all(_file_ok(folder / b) for b in basenames) and find_entry(model_id) is None:
                _register(entry)
                if entry.purpose == "chat" and not settings.get("engine", "model_id"):
                    settings.update("engine", {"model_id": model_id})

    first = None
    for index, (job_id, part, base) in enumerate(zip(job_ids, group["parts"], basenames, strict=True), start=1):
        url = HF_RESOLVE_REV_URL.format(repo=repo, revision=quote(revision, safe=""), path=quote(part["path"], safe="/"))
        label = entry.name if count == 1 else f"{entry.name} (part {index}/{count})"
        job = downloads.start(job_id, url, folder / base, sha256=part["sha256"], size=part["size"], label=label,
                              kind="model", on_complete=finish)
        first = first or job
    return first, model_id


def import_local(path: object, name: object = None, purpose: object = None) -> dict:
    """Register an existing .gguf file (e.g. on an external drive) without copying it."""
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or "\x00" in path:
        raise EngineError("path must be the full path of a .gguf file", HTTPStatus.BAD_REQUEST)
    raw = Path(path.strip())
    if not raw.is_absolute():
        raise EngineError("path must be an absolute path", HTTPStatus.BAD_REQUEST)
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError):
        raise EngineError(f"File not found: {raw}", HTTPStatus.BAD_REQUEST) from None
    if not resolved.is_file():
        raise EngineError(f"{resolved} is not a regular file", HTTPStatus.BAD_REQUEST)
    if resolved.suffix.lower() != ".gguf":
        raise EngineError("Only .gguf model files can be imported", HTTPStatus.BAD_REQUEST)
    try:
        with open(resolved, "rb") as handle:
            magic = handle.read(4)
        size = resolved.stat().st_size
    except OSError as exc:
        raise EngineError(f"{resolved} cannot be read: {exc.strerror or exc}", HTTPStatus.BAD_REQUEST) from None
    if magic != b"GGUF":
        raise EngineError(f"{resolved.name} is not a GGUF model (wrong file header)", HTTPStatus.BAD_REQUEST)
    model_id = _custom_id("local", _stem(resolved.name), str(resolved))
    existing = find_entry(model_id)
    if existing is not None:
        return model_dict(existing)
    entry = CatalogEntry(
        model_id, _clean_name(name, _stem(resolved.name)), "Local file", _clean_purpose(purpose, resolved.name),
        parse_params(resolved.name), parse_quant(resolved.name) or "", "", resolved.name, size, _min_ram_gb(size),
        "see the model's source", f"Imported from {resolved.parent}", 0, source="local",
        files=(resolved.name,), local_path=str(resolved),
    )
    _register(entry)
    return model_dict(entry)


# --------------------------------------------------------------------------- models folder (storage)

SYSTEM_DIRS = ("/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/dev", "/proc", "/sys", "/boot",
               "/lib", "/lib64", "/Library", "/Applications")
MOVE_CHUNK_BYTES = 4 * 1024 * 1024


def _app_dirs() -> list[Path]:
    dirs = [Path(__file__).resolve().parent.parent]
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
        if getattr(sys, "_MEIPASS", None):
            dirs.append(Path(sys._MEIPASS).resolve())  # type: ignore[attr-defined]
    for folder in list(dirs):
        bundle = next((p for p in (folder, *folder.parents) if p.suffix == ".app"), None)
        if bundle is not None:
            dirs.append(bundle)
    return dirs


def _system_location(path: Path) -> bool:
    if path == Path(path.anchor):
        return True
    candidates = [Path(d) for d in SYSTEM_DIRS]
    if os.name == "nt":
        candidates += [Path(v) for v in (os.environ.get("WINDIR"), os.environ.get("ProgramFiles"),
                                         os.environ.get("ProgramFiles(x86)"), r"C:\Windows") if v]
    candidates += _app_dirs()
    return any(path == c or c in path.parents for c in candidates)


def validate_models_folder(raw: object) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or len(raw) > 4096:
        raise EngineError("models_dir must be a folder path", HTTPStatus.BAD_REQUEST)
    path = Path(raw.strip())
    if not path.is_absolute():
        raise EngineError("models_dir must be an absolute path", HTTPStatus.BAD_REQUEST)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise EngineError(f"{path} does not exist (is the drive connected?)", HTTPStatus.BAD_REQUEST) from None
    if not resolved.is_dir():
        raise EngineError(f"{resolved} is not a folder", HTTPStatus.BAD_REQUEST)
    if _system_location(resolved) or _system_location(path):
        raise EngineError(f"{resolved} is a system or app folder; choose another one", HTTPStatus.BAD_REQUEST)
    if resolved == config.home_dir().resolve():
        raise EngineError("Choose a dedicated folder, not the LowRAM AI data folder itself", HTTPStatus.BAD_REQUEST)
    try:
        fd, probe = tempfile.mkstemp(prefix=".lowram-write-test-", dir=resolved)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise EngineError(f"Cannot write to {resolved}: {exc.strerror or exc}", HTTPStatus.BAD_REQUEST) from None
    return resolved


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


class _StorageMover:
    """Copies models to a new folder: copy -> fsync -> size check for all, switch, then delete sources."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state is not None and self._state["state"] == "copying"

    def snapshot(self) -> dict | None:
        with self._lock:
            return dict(self._state) if self._state else None

    def start(self, files: list[Path], target: Path, new_setting: str | None) -> None:
        conflicts = [f.name for f in files if (target / f.name).exists() or (target / f.name).is_symlink()]
        if conflicts:
            raise EngineError(f"The new folder already contains {conflicts[0]}; remove it or choose another folder")
        total = sum(f.stat().st_size for f in files)
        _check_free_space(target, total)
        with self._lock:
            if self._state is not None and self._state["state"] == "copying":
                raise EngineError("Models are already being moved")
            self._state = {"state": "copying", "bytes_done": 0, "bytes_total": total, "error": None}
        threading.Thread(target=self._run, args=(files, target, new_setting), name="lowram-models-move",
                         daemon=True).start()

    def _progress(self, n: int) -> None:
        with self._lock:
            if self._state is not None:
                self._state["bytes_done"] += n

    def _copy(self, src: Path, dst: Path) -> None:
        tmp = dst.with_name(f".{dst.name}.moving")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            with open(src, "rb") as source, os.fdopen(os.open(tmp, flags, 0o600), "wb") as out:
                while True:
                    chunk = source.read(MOVE_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    self._progress(len(chunk))
                out.flush()
                os.fsync(out.fileno())
            if tmp.stat().st_size != src.stat().st_size:
                raise OSError(f"size check failed for {src.name}")
            os.chmod(tmp, 0o644)
            os.replace(tmp, dst)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _run(self, files: list[Path], target: Path, new_setting: str | None) -> None:
        copied: list[tuple[Path, Path]] = []
        try:
            for src in files:
                dst = target / src.name
                self._copy(src, dst)
                copied.append((src, dst))
            settings.update("engine", {"models_dir": new_setting})  # switch only when every copy is verified
        except Exception as exc:  # noqa: BLE001 - reported in the storage status
            for _src, dst in copied:  # the sources are untouched; drop our copies
                try:
                    dst.unlink()
                except OSError:
                    pass
            log.warning("Moving the models failed: %s", exc)
            with self._lock:
                if self._state is not None:
                    self._state.update(state="failed", error=f"Moving the models failed: {exc}")
            return
        for src, _dst in copied:
            try:
                src.unlink()
            except OSError as exc:
                log.warning("Could not delete %s after moving it: %s", src, exc)
        with self._lock:
            if self._state is not None:
                self._state.update(state="done")
        log.info("Moved %d model file(s) to %s", len(copied), target)


_mover = _StorageMover()


def storage_info() -> dict:
    folder = Path(config.models_dir())
    default = config.default_models_dir()
    available = folder.is_dir()
    free = total = None
    if available:
        try:
            usage = shutil.disk_usage(folder)
            free, total = int(usage.free), int(usage.total)
        except OSError:
            pass
    return {
        "models_dir": str(folder), "default_dir": str(default), "custom": not _same_dir(folder, default),
        "available": available, "writable": available and os.access(folder, os.W_OK),
        "free_bytes": free, "total_bytes": total, "moving": _mover.snapshot(),
    }


def _model_downloads_active() -> bool:
    return any(d["kind"] == "model" and d["state"] in downloads.ACTIVE_STATES for d in downloads.list_jobs())


def set_storage(models_dir: object, move_existing: object = False) -> dict:
    """Change the models folder (``None`` = default), optionally moving downloaded models along."""
    if os.environ.get("LOWRAM_MODELS_DIR", "").strip():
        raise EngineError("LOWRAM_MODELS_DIR is set in the environment; change or remove it there")
    if not isinstance(move_existing, bool):
        raise EngineError("move_existing must be true or false", HTTPStatus.BAD_REQUEST)
    target = config.default_models_dir() if models_dir is None else validate_models_folder(models_dir)
    new_setting = None if models_dir is None else str(target)
    _not_moving()
    if _model_downloads_active():
        raise EngineError("Wait until the model downloads have finished (or cancel them) before changing the folder")
    current = Path(config.models_dir())
    if _same_dir(current, target):
        return storage_info()
    if move_existing:
        if not current.is_dir():
            raise EngineError(f"The current models folder {current} is not available (drive not connected?), "
                              "so nothing can be moved")
        if _manager.snapshot()["state"] in ("starting", "ready"):
            raise EngineError("Stop the engine before moving the models")
        files: list[Path] = []
        for entry in [*CATALOG, *(e for e in custom_entries() if e.source == "custom")]:
            for item in [*model_files(entry), _sidecar(entry)]:
                if _file_ok(item) and item not in files:
                    files.append(item)
        if files:
            _mover.start(files, target, new_setting)
            return storage_info()
    settings.update("engine", {"models_dir": new_setting})
    return storage_info()


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
        self._key: str | None = None
        self._shutting_down = False
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
            if not is_available(entry):
                raise _unavailable_error(entry)
            return entry
        selected = find_entry(settings.get("engine", "model_id"))
        if selected is not None and is_installed(selected):
            if not is_available(selected):
                raise _unavailable_error(selected)
            return selected
        usable = [e for e in all_entries() if is_available(e)]
        if not usable:
            raise EngineError("No model is installed yet. Install a model under Models & Settings first.")
        best = recommended_model_id()
        usable.sort(key=lambda e: (e.id != best, e.purpose != "chat", e.source != "catalog", -e.rank))
        return usable[0]

    def start(self, model_id: str | None = None, *, persist: bool = True) -> dict:
        with self._op:
            if self._shutting_down:
                raise EngineError("LowRAM AI is shutting down", HTTPStatus.SERVICE_UNAVAILABLE)
            if external():
                raise EngineError("LOWRAM_LLM_URL is set, so LowRAM AI uses that server and does not start its own")
            if _mover.active:
                raise EngineError("Models are being moved to the new folder; try again when that has finished")
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
            ctx = ctx_size()
            path = log_path()
            if path.exists():
                os.replace(path, path.with_name(path.name + ".1"))
            key = secrets.token_urlsafe(32)  # llama-server only answers API calls that carry it
            port = choose_port()  # as late as possible: another process could grab a free port
            args = [str(binary), "-m", str(model_path(entry)), "--host", "127.0.0.1", "--port", str(port),
                    "-c", str(ctx), "--parallel", "1", "--api-key", key]
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
                self._key = key
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
            if proc.poll() is not None:  # only our own, still running process counts as ready
                return
            self._info["state"] = "ready"
            self._url = url
            config.set_runtime_llm_url(url)
            config.set_runtime_llm_api_key(self._key)
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
            config.set_runtime_llm_api_key(None)
            self._url = None
        self._key = None

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

    def shutdown(self) -> dict:
        """Stop for good: nothing may spawn llama-server after shutdown began."""
        self._shutting_down = True
        return self.stop()

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
        if self._shutting_down:
            return
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
        if external() or _manager._shutting_down or not settings.get("engine", "auto_start"):
            return
        entry = find_entry(settings.get("engine", "model_id"))
        if entry is None or not is_available(entry) or discover_runtime()[0] is None:
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
    _manager.shutdown()


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


@route("POST", r"/api/engine/custom/resolve")
@_api
def route_custom_resolve(req: Request) -> Response:
    return Response(resolve_custom(req.json().get("source")))


@route("POST", r"/api/engine/custom/install")
@_api
def route_custom_install(req: Request) -> Response:
    data = req.json()
    download, model_id = install_custom(
        data.get("repo"), data.get("filename"), data.get("name"), data.get("purpose"), data.get("revision", "main")
    )
    return Response({"download": download, "model_id": model_id}, HTTPStatus.ACCEPTED)


@route("POST", r"/api/engine/custom/import")
@_api
def route_custom_import(req: Request) -> Response:
    data = req.json()
    return Response({"model": import_local(data.get("path"), data.get("name"), data.get("purpose"))},
                    HTTPStatus.CREATED)


@route("GET", r"/api/engine/storage")
@_api
def route_storage(req: Request) -> Response:
    return Response(storage_info())


@route("POST", r"/api/engine/storage")
@_api
def route_set_storage(req: Request) -> Response:
    data = req.json()
    if "models_dir" not in data:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "models_dir is required (null = default folder)")
    return Response({"storage": set_storage(data["models_dir"], data.get("move_existing", False))},
                    HTTPStatus.ACCEPTED)
