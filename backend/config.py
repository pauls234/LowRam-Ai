"""Runtime configuration, resolved from environment variables on every call.

Values are read lazily (not at import time) so tests and long-running
processes always see the current environment.
"""

from __future__ import annotations

import os
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


def llm_url() -> str:
    """``LOWRAM_LLM_URL`` wins, then the managed engine, then the llama-server default."""
    return os.environ.get("LOWRAM_LLM_URL", "").strip() or _runtime_llm_url or DEFAULT_LLM_URL


def models_dir() -> Path:
    return _ensure_dir(home_dir() / "models")


def bin_dir() -> Path:
    return _ensure_dir(home_dir() / "bin")


def llm_model() -> str:
    return os.environ.get("LOWRAM_LLM_MODEL", "").strip() or DEFAULT_MODEL


def llm_api_key() -> str | None:
    """Optional bearer token for endpoints that require one (never logged)."""
    return os.environ.get("LOWRAM_LLM_API_KEY") or None


def llm_timeout() -> float:
    return _env_number("LOWRAM_LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT_S, 1, 24 * 3600)


def min_free_bytes() -> int:
    """Free RAM required before a task may start (``LOWRAM_MIN_FREE_MB``)."""
    mb = _env_number("LOWRAM_MIN_FREE_MB", DEFAULT_MIN_FREE_MB, 0, 1024 * 1024)
    return int(mb * 1024 * 1024)


def port() -> int:
    return int(_env_number("LOWRAM_PORT", DEFAULT_PORT, 1024, 65535))
