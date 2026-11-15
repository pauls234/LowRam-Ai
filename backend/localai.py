"""Local AI that is already on this computer: model files of other apps and running AI servers.

* ``GET /api/localai`` -> ``{"models": [...], "servers": [...], "local_server": {...}|null,
  "truncated": bool, "scanned_at": iso}``.
  ``models``: GGUF files that Ollama, LM Studio, Jan, GPT4All, the llama.cpp download cache or the
  Hugging Face cache already downloaded – usable in place (never copied, changed or deleted).
  ``servers``: AI servers already running on 127.0.0.1 (Ollama, LM Studio, Jan, Docker Model
  Runner, GPT4All, llama.cpp/llamafile, KoboldCpp) with their chat models and what they hold in RAM.
  ``?refresh=1`` scans the folders again (they are cached for :data:`CACHE_S`).
* ``POST /api/localai/use`` ``{"id"}`` -> ``{"model"}``: register a found file like "use a file you
  already have" (only ids of the last scan, never a path from the request).
* ``POST /api/localai/server`` ``{"id", "model"}`` -> ``{"local_server"}``: chat through a running
  server instead of the built-in engine (only servers found right now, only 127.0.0.1); the
  built-in engine is stopped so the model is not held in RAM twice. ``{"id": null}`` switches back.

Read-only and bounded: only the known folders of these apps are looked at (depth, entry and time
limits, folder symlinks are not followed), only the first four bytes of a candidate are read (the
GGUF signature), servers are only asked on 127.0.0.1 with a short timeout and without following
redirects, and nothing is sent to the internet.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import config, db, engine, settings
from .web import HTTPError, Request, Response, route

log = logging.getLogger("lowram.localai")

MIN_MODEL_BYTES = 50 * 1024 * 1024  # smaller .gguf files are vocabularies, adapters or test files
MAX_DEPTH = 6
MAX_ENTRIES = 20_000  # directory entries looked at per scan
MAX_FOUND = 200
SCAN_BUDGET_S = 3.0
CACHE_S = 30.0
MAX_MANIFEST_BYTES = 256 * 1024
PROBE_TIMEOUT_S = 0.6
MAX_PROBE_BYTES = 512 * 1024
MAX_SERVER_MODELS = 100
OLLAMA_MODEL_MEDIA = "application/vnd.ollama.image.model"
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")
SPLIT_RE = re.compile(r"-\d{5}-of-\d{5}\.gguf$", re.IGNORECASE)  # a part of a split model
SERVER_MODEL_RE = re.compile(r"[A-Za-z0-9._:/@+-]{1,200}")
EMBEDDING_RE = re.compile(r"embed|rerank|bge-|e5-", re.IGNORECASE)  # not chat models

# (name, port, path that lists the models, OpenAI-compatible chat path)
SERVERS: tuple[tuple[str, int, str, str], ...] = (
    ("Ollama", 11434, "/api/tags", "/v1/chat/completions"),
    ("LM Studio", 1234, "/v1/models", "/v1/chat/completions"),
    ("Jan", 1337, "/v1/models", "/v1/chat/completions"),
    ("Docker Model Runner", 12434, "/engines/v1/models", "/engines/v1/chat/completions"),
    ("GPT4All", 4891, "/v1/models", "/v1/chat/completions"),
    ("llama.cpp", 8080, "/v1/models", "/v1/chat/completions"),
    ("KoboldCpp", 5001, "/v1/models", "/v1/chat/completions"),
)


@dataclass(frozen=True)
class Found:
    id: str
    name: str
    source: str
    path: str
    size_bytes: int

    @property
    def purpose(self) -> str:
        return "code" if re.search(r"coder|code", self.name, re.IGNORECASE) else "chat"


_lock = threading.Lock()
_scan_lock = threading.Lock()  # one scan at a time (wizard and settings may ask together)
_cache: dict[str, Any] = {"at": 0.0, "found": [], "truncated": False, "scanned_at": None}


# --------------------------------------------------------------------------- where other apps keep models


def model_roots() -> list[tuple[str, Path, str]]:
    """``(app, folder, kind)`` to look in; kind ``ollama`` (manifests + blobs) or ``files`` (*.gguf)."""
    home = Path.home()
    roots: list[tuple[str, Path, str]] = []
    ollama = os.environ.get("OLLAMA_MODELS", "").strip()
    roots.append(("Ollama", Path(ollama).expanduser() if ollama else home / ".ollama" / "models", "ollama"))
    if sys.platform.startswith("linux"):
        roots.append(("Ollama", Path("/usr/share/ollama/.ollama/models"), "ollama"))  # the Linux service install
    roots += [("LM Studio", home / ".lmstudio" / "models", "files"),
              ("LM Studio", home / ".cache" / "lm-studio" / "models", "files")]
    hub = os.environ.get("HF_HUB_CACHE", "").strip()
    hf_home = os.environ.get("HF_HOME", "").strip()
    hub_path = Path(hub) if hub else (Path(hf_home) / "hub" if hf_home else home / ".cache" / "huggingface" / "hub")
    roots.append(("Hugging Face", hub_path.expanduser(), "files"))
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        roots += [("Jan", support / "Jan" / "data" / "llamacpp" / "models", "files"),
                  ("GPT4All", support / "nomic.ai" / "GPT4All", "files"),
                  ("llama.cpp", home / "Library" / "Caches" / "llama.cpp", "files")]
    elif os.name == "nt":
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roots += [("Jan", roaming / "Jan" / "data" / "llamacpp" / "models", "files"),
                  ("GPT4All", local / "nomic.ai" / "GPT4All", "files"),
                  ("llama.cpp", local / "llama.cpp", "files")]
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
        roots += [("Jan", data / "Jan" / "data" / "llamacpp" / "models", "files"),
                  ("GPT4All", data / "nomic.ai" / "GPT4All", "files"),
                  ("llama.cpp", cache / "llama.cpp", "files")]
    roots.append(("Jan", home / "jan" / "models", "files"))  # older Jan versions
    return roots


class _Budget:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + SCAN_BUDGET_S
        self.entries = 0
        self.truncated = False

    def spend(self) -> bool:
        """Count one directory entry; ``False`` once the scan has to stop."""
        self.entries += 1
        if self.entries > MAX_ENTRIES or time.monotonic() > self.deadline:
            self.truncated = True
            return False
        return True


def _is_gguf(path: Path) -> int | None:
    """Size of ``path`` if it is a regular GGUF file big enough to be a model, else ``None``."""
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size < MIN_MODEL_BYTES:
            return None
        with open(path, "rb") as handle:
            return size if handle.read(4) == b"GGUF" else None
    except OSError:
        return None


def _walk(root: Path, budget: _Budget):
    """Yield files below ``root`` (depth <= :data:`MAX_DEPTH`); folder symlinks and dot folders are skipped."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not budget.spend():
                        return
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < MAX_DEPTH and not entry.name.startswith("."):
                                stack.append((Path(entry.path), depth + 1))
                        elif entry.is_file():
                            yield Path(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue


def _found(source: str, name: str, path: Path, size: int) -> Found:
    resolved = str(path)
    return Found(hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16], name[:120], source, resolved, size)


def _scan_files(source: str, root: Path, budget: _Budget, skip: set[Path]) -> list[Found]:
    out: list[Found] = []
    for path in _walk(root, budget):
        name = path.name
        if not name.lower().endswith(".gguf") or "mmproj" in name.lower() or SPLIT_RE.search(name):
            continue  # vision projectors are no models; split models are left to the file picker
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if any(resolved == own or own in resolved.parents for own in skip):
            continue  # LowRAM AI's own models folder
        size = _is_gguf(resolved)
        if size is not None:
            out.append(_found(source, engine._stem(name), resolved, size))
    return out


def _ollama_name(parts: tuple[str, ...]) -> str:
    """``registry.ollama.ai/library/llama3.2/3b`` -> ``llama3.2:3b``; other registries keep their path."""
    *repo, tag = parts
    if len(repo) >= 2 and repo[0] == "registry.ollama.ai" and repo[1] == "library":
        return f"{'/'.join(repo[2:])}:{tag}"
    return f"{'/'.join(repo[1:] if repo and '.' in repo[0] else repo)}:{tag}"


def _scan_ollama(root: Path, budget: _Budget) -> list[Found]:
    manifests, blobs = root / "manifests", root / "blobs"
    by_blob: dict[Path, Found] = {}
    for manifest in _walk(manifests, budget):
        try:
            parts = manifest.relative_to(manifests).parts
            if len(parts) < 2 or manifest.stat().st_size > MAX_MANIFEST_BYTES:
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        layers = data.get("layers") if isinstance(data, dict) else None
        digest = next((layer.get("digest") for layer in layers if isinstance(layer, dict)
                       and layer.get("mediaType") == OLLAMA_MODEL_MEDIA), None) if isinstance(layers, list) else None
        match = DIGEST_RE.fullmatch(digest) if isinstance(digest, str) else None
        if match is None:
            continue
        blob = next((b for b in (blobs / f"sha256-{match.group(1)}", blobs / f"sha256:{match.group(1)}")
                     if b.exists()), None)
        if blob is None:
            continue
        try:
            blob = blob.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        size = _is_gguf(blob)
        if size is None:
            continue
        name = _ollama_name(parts)
        known = by_blob.get(blob)
        if known is None or known.name.endswith(":latest"):  # "llama3.2:3b" says more than ":latest"
            by_blob[blob] = _found("Ollama", name, blob, size)
    return list(by_blob.values())


def scan(force: bool = False) -> tuple[list[Found], bool, str | None]:
    """Model files of other apps (cached for :data:`CACHE_S`) and whether the scan stopped early."""
    with _scan_lock:
        with _lock:
            if not force and _cache["scanned_at"] is not None and time.monotonic() - _cache["at"] < CACHE_S:
                return list(_cache["found"]), _cache["truncated"], _cache["scanned_at"]
        return _scan_now()


def _scan_now() -> tuple[list[Found], bool, str | None]:
    budget = _Budget()
    try:
        own = {Path(config.models_dir()).resolve()}
    except (OSError, RuntimeError):
        own = set()
    found: dict[str, Found] = {}
    for source, root, kind in model_roots():
        if budget.truncated or not root.is_dir():
            continue
        try:
            items = _scan_ollama(root, budget) if kind == "ollama" else _scan_files(source, root, budget, own)
        except Exception:  # noqa: BLE001 - one odd folder must not hide the others
            log.exception("Scanning %s for models failed", root)
            continue
        for item in items:
            found.setdefault(item.path, item)
    result = sorted(found.values(), key=lambda f: (f.source, f.name.lower()))[:MAX_FOUND]
    with _lock:
        _cache.update(at=time.monotonic(), found=result, truncated=budget.truncated, scanned_at=db.utcnow())
        return list(result), budget.truncated, _cache["scanned_at"]


def model_view(item: Found, registered: dict[str, str], total: int) -> dict:
    return {
        "id": item.id, "name": item.name, "source": item.source, "path": item.path, "size_bytes": item.size_bytes,
        "params": engine.parse_params(item.name), "quant": engine.parse_quant(item.name) or "",
        "purpose": item.purpose, "fits_ram": bool(total) and item.size_bytes <= total * (1 - engine.RAM_HEADROOM),
        "model_id": registered.get(item.path),
    }


def _registered_paths() -> dict[str, str]:
    """Resolved path of every imported model file -> its model id."""
    out: dict[str, str] = {}
    for entry in engine.custom_entries():
        if entry.local_path:
            try:
                out[str(Path(entry.local_path).resolve())] = entry.id
            except (OSError, RuntimeError):
                continue
    return out


# --------------------------------------------------------------------------- running AI servers (127.0.0.1)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a local server never sends LowRAM AI anywhere else


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _get_json(port: int, path: str) -> Any:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     headers={"Accept": "application/json", "User-Agent": "LowRAM-AI"})
    try:
        with _OPENER.open(request, timeout=PROBE_TIMEOUT_S) as response:
            if response.status != 200:
                return None
            raw = response.read(MAX_PROBE_BYTES + 1)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        return None
    if len(raw) > MAX_PROBE_BYTES:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _names(items: Any, key: str) -> list[str]:
    names = []
    for item in items if isinstance(items, list) else []:
        value = item.get(key) if isinstance(item, dict) else None
        if isinstance(value, str) and SERVER_MODEL_RE.fullmatch(value) and not EMBEDDING_RE.search(value):
            names.append(value)
    return list(dict.fromkeys(names))[:MAX_SERVER_MODELS]


def _probe(name: str, port: int, models_path: str, chat_path: str) -> dict | None:
    data = _get_json(port, models_path)
    if not isinstance(data, dict):
        return None
    loaded: list[dict] = []
    if name == "Ollama":
        models = _names(data.get("models"), "name")
        running = _get_json(port, "/api/ps")
        for item in (running.get("models") if isinstance(running, dict) else None) or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("size"), int):
                loaded.append({"name": item["name"][:200], "size_bytes": max(0, item["size"])})
    else:
        models = _names(data.get("data"), "id")
        if name == "LM Studio":  # its own API tells which models are loaded right now
            native = _get_json(port, "/api/v0/models")
            for item in (native.get("data") if isinstance(native, dict) else None) or []:
                if isinstance(item, dict) and item.get("state") == "loaded" and isinstance(item.get("id"), str):
                    loaded.append({"name": item["id"][:200], "size_bytes": None})
    if not models:
        return None
    return {"id": f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}-{port}", "name": name, "port": port,
            "models": models, "loaded": loaded, "url": f"http://127.0.0.1:{port}{chat_path}"}


def probe_servers() -> list[dict]:
    """AI servers answering on 127.0.0.1 right now (all asked at once, ~:data:`PROBE_TIMEOUT_S`)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SERVERS), thread_name_prefix="lowram-probe") as pool:
        results = list(pool.map(lambda server: _probe(*server), SERVERS))
    return [server for server in results if server is not None]


# --------------------------------------------------------------------------- HTTP API


@route("GET", r"/api/localai")
def route_scan(req: Request) -> Response:
    found, truncated, scanned_at = scan(force=req.arg("refresh") in ("1", "true"))
    registered, total = _registered_paths(), engine.total_ram()
    current = config.local_server()
    servers = probe_servers()
    for server in servers:
        server["in_use"] = bool(current and current["url"] == server["url"])
    return Response({
        "models": [model_view(item, registered, total) for item in found], "servers": servers,
        "local_server": current, "truncated": truncated, "scanned_at": scanned_at,
    })


@route("POST", r"/api/localai/use")
def route_use(req: Request) -> Response:
    data = req.json()
    found_id = data.get("id")
    if not isinstance(found_id, str) or not re.fullmatch(r"[0-9a-f]{16}", found_id):
        raise HTTPError(HTTPStatus.BAD_REQUEST, "id must come from the list of found models")
    item = next((f for f in scan()[0] if f.id == found_id), None)
    if item is None:  # the list may be older than the cache: look once more
        item = next((f for f in scan(force=True)[0] if f.id == found_id), None)
    if item is None:
        raise HTTPError(HTTPStatus.NOT_FOUND, "Dieses Modell ist nicht mehr da – bitte neu suchen.")
    try:
        model = engine.import_local(item.path, item.name, item.purpose, family=item.source, any_suffix=True)
    except engine.EngineError as exc:
        raise HTTPError(exc.status, str(exc)) from None
    if config.local_server() is not None:  # a file runs in the built-in engine again
        settings.update("engine", {"local_server": None})
    return Response({"model": model}, HTTPStatus.CREATED)


@route("POST", r"/api/localai/server")
def route_server(req: Request) -> Response:
    data = req.json()
    server_id = data.get("id")
    if server_id is None:
        settings.update("engine", {"local_server": None})
        return Response({"local_server": None})
    if not isinstance(server_id, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", server_id):
        raise HTTPError(HTTPStatus.BAD_REQUEST, "id must come from the list of found servers")
    server = next((s for s in probe_servers() if s["id"] == server_id), None)
    if server is None:
        raise HTTPError(HTTPStatus.NOT_FOUND, "Dieser KI-Server antwortet gerade nicht – läuft das Programm noch?")
    model = data.get("model")
    if not isinstance(model, str) or model not in server["models"]:
        raise HTTPError(HTTPStatus.BAD_REQUEST, "Wähle eines der Modelle dieses Servers.")
    settings.update("engine", {"local_server": {"name": server["name"], "url": server["url"], "model": model}})
    chosen = config.local_server()
    if chosen is None:  # cannot happen with a found server; never leave a half-valid setting behind
        settings.update("engine", {"local_server": None})
        raise HTTPError(HTTPStatus.INTERNAL_SERVER_ERROR, "Der Server konnte nicht übernommen werden.")
    try:
        engine.stop()  # the built-in engine must not hold a second model in RAM
    except Exception:  # noqa: BLE001 - it may not run at all
        log.debug("Built-in engine was not running", exc_info=True)
    log.info("Using the local AI server %s (%s) instead of the built-in engine", chosen["name"], chosen["model"])
    return Response({"local_server": chosen})
