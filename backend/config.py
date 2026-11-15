"""Runtime configuration, resolved from environment variables on every call.

Values are read lazily (not at import time) so tests and long-running
processes always see the current environment.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_MODEL = "local-model"
DEFAULT_PORT = 8765
DEFAULT_MIN_FREE_MB = 1024
DEFAULT_LLM_TIMEOUT_S = 600.0

# The web UI never binds to anything but loopback.
HOST = "127.0.0.1"


class ConfigError(ValueError):
    """Raised when an environment variable holds an unusable value."""


def _env_number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g}, got {raw!r}")
    return value


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)  # mkdir's mode is ignored for directories that already exist
    except OSError:
        pass
    return path


def home_dir() -> Path:
    """Data directory (default ``~/.lowram-ai``; override with ``LOWRAM_HOME``)."""
    raw = os.environ.get("LOWRAM_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".lowram-ai"
    return _ensure_dir(path)


def db_path() -> Path:
    return home_dir() / "lowram.db"


def artifacts_dir() -> Path:
    return _ensure_dir(home_dir() / "artifacts")


def skills_dir() -> Path:
    return _ensure_dir(home_dir() / "skills")


_runtime_llm_url: str | None = None


def set_runtime_llm_url(url: str | None) -> None:
    """Called by the engine manager when it starts/stops its own llama-server."""
    global _runtime_llm_url
    _runtime_llm_url = url


# path segments of letters, digits and ._~- – but never "." or ".."
LOCAL_SERVER_URL_RE = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})(?:/(?!\.{1,2}/)[A-Za-z0-9._~-]+)*/chat/completions")
MAX_LOCAL_MODEL_CHARS = 200


def local_server() -> dict | None:
    """The AI server already running on this computer that the user chose in the Studio, or ``None``.

    Stored as ``settings.engine.local_server`` = ``{"name", "url", "model"}`` (e.g. Ollama). Whatever
    the settings file says, only ``http://127.0.0.1:<port>/…/chat/completions`` is accepted, so this
    can never send anything to another computer. ``LOWRAM_LLM_URL`` wins over it.
    """
    if os.environ.get("LOWRAM_LLM_URL", "").strip():
        return None
    try:
        from . import settings  # lazy: settings imports this module

        value = settings.get("engine", "local_server")
    except Exception:  # settings unreadable or an old file: the built-in engine
        return None
    if not isinstance(value, dict):
        return None
    url, model, name = value.get("url"), value.get("model"), value.get("name")
    match = LOCAL_SERVER_URL_RE.fullmatch(url) if isinstance(url, str) else None
    if match is None or not 1 <= int(match.group(1)) <= 65535:
        return None
    if not isinstance(model, str) or not 0 < len(model) <= MAX_LOCAL_MODEL_CHARS or not model.isprintable():
        return None
    label = name.strip()[:60] if isinstance(name, str) and name.strip().isprintable() and name.strip() else "Local AI"
    return {"name": label, "url": url, "model": model}


def llm_url() -> str:
    """``LOWRAM_LLM_URL`` wins, then a chosen local AI server, the managed engine, the llama-server default."""
    env = os.environ.get("LOWRAM_LLM_URL", "").strip()
    if env:
        return env
    server = local_server()
    return server["url"] if server else _runtime_llm_url or DEFAULT_LLM_URL


def default_models_dir() -> Path:
    return _ensure_dir(home_dir() / "models")


def models_dir() -> Path:
    """Where models live: ``LOWRAM_MODELS_DIR`` > ``settings.engine.models_dir`` > ``<home>/models``.

    A custom folder (e.g. on an external SSD) is NOT created automatically: if
    the drive is unplugged the path simply does not exist and callers must
    report "drive not connected" instead of silently writing to the boot disk.
    """
    env = os.environ.get("LOWRAM_MODELS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from . import settings  # lazy: settings imports this module

        custom = settings.get("engine", "models_dir")
    except Exception:  # settings unreadable → fall back to the default folder
        custom = None
    if isinstance(custom, str) and custom.strip():
        return Path(custom).expanduser()
    return default_models_dir()


def bin_dir() -> Path:
    return _ensure_dir(home_dir() / "bin")


def llm_model() -> str:
    env = os.environ.get("LOWRAM_LLM_MODEL", "").strip()
    if env:
        return env
    server = local_server()
    return server["model"] if server else DEFAULT_MODEL


_runtime_llm_api_key: str | None = None


def set_runtime_llm_api_key(key: str | None) -> None:
    """Called by the engine manager: the random key its own llama-server requires."""
    global _runtime_llm_api_key
    _runtime_llm_api_key = key


def llm_api_key() -> str | None:
    """Bearer token for the model endpoint (never logged).

    ``LOWRAM_LLM_API_KEY`` wins. The managed engine's random key is only sent
    to the managed engine, never to an external ``LOWRAM_LLM_URL`` or a chosen local server.
    """
    env_key = os.environ.get("LOWRAM_LLM_API_KEY")
    if env_key:
        return env_key
    if not os.environ.get("LOWRAM_LLM_URL", "").strip() and _runtime_llm_url and local_server() is None:
        return _runtime_llm_api_key
    return None


def llm_timeout() -> float:
    return _env_number("LOWRAM_LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT_S, 1, 24 * 3600)


def min_free_bytes() -> int:
    """Free RAM required before a task may start (``LOWRAM_MIN_FREE_MB``)."""
    mb = _env_number("LOWRAM_MIN_FREE_MB", DEFAULT_MIN_FREE_MB, 0, 1024 * 1024)
    return int(mb * 1024 * 1024)


def port() -> int:
    return int(_env_number("LOWRAM_PORT", DEFAULT_PORT, 1024, 65535))
