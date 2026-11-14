"""Persistent user settings in ``<LOWRAM_HOME>/settings.json``.

Sections and keys are fixed by :data:`DEFAULTS`; unknown keys are rejected so
a typo can never silently disable something. Writes are atomic.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from typing import Any

from . import artifacts, config

log = logging.getLogger("lowram.settings")

DEFAULTS: dict[str, Any] = {
    "budget_percent": 40,
    "engine": {
        "auto_start": True,  # start llama-server with the selected model when the app starts
        "model_id": None,  # catalog id of the chat model
        "code_model_id": None,  # optional separate model for the Code mode
        "port": None,  # None = pick a free port
        "ctx_size": None,  # None = derive from the RAM budget
        "models_dir": None,  # None = <home>/models; or an absolute folder, e.g. on an external SSD
    },
    "media": {
        "image_model_id": None,
        "video_model_id": None,
        "unload_llm": True,  # stop llama-server while an image/video job needs the RAM
    },
    "cluster": {
        "enabled": False,  # accept helper devices on the local network
        "port": 8766,
        "device_name": None,
        "discoverable": True,  # announce this host on the LAN so helpers can find it without a code
    },
    "ui": {
        "theme": "system",
        "check_updates": True,  # look for a newer release on GitHub (at most every 6 h)
        "language": "auto",  # "auto" (browser language, German fallback) | "de" | "en"
    },
    "desktop": {
        "keep_running_in_menu_bar": True,  # closing the window keeps LowRAM AI in the macOS menu bar
    },
    "chat": {
        "instructions": "",  # custom instructions added to every conversation (max 4000 chars)
    },
    "power": {
        "keep_awake": True,  # keep the Mac awake while long jobs run (caffeinate)
        "battery_min_percent": 20,  # on battery below this, long jobs pause (0 = never pause)
        "notifications": True,  # system notifications when long jobs finish / devices ask to pair
    },
}

_lock = threading.RLock()  # re-entrant: update() holds it across load() + write


def _path():
    return config.home_dir() / "settings.json"


def _merge(defaults: dict, stored: dict) -> dict:
    merged = copy.deepcopy(defaults)
    for key, value in stored.items():
        if key not in merged:
            continue
        if isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load() -> dict:
    """All settings with defaults filled in. A corrupt file is kept aside, not lost."""
    path = _path()
    with _lock:
        if not path.exists():
            return copy.deepcopy(DEFAULTS)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise ValueError("settings file is not a JSON object")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            backup = path.with_suffix(".json.corrupt")
            log.warning("Settings file unreadable (%s); moved to %s and using defaults", exc, backup)
            try:
                path.replace(backup)
            except OSError:
                pass
            return copy.deepcopy(DEFAULTS)
        return _merge(DEFAULTS, stored)


def get(section: str, key: str | None = None) -> Any:
    value = load()[section]
    return value if key is None else value[key]


def update(section: str, values: dict) -> Any:
    """Change keys of one section (or the scalar ``budget_percent``) and persist."""
    if section not in DEFAULTS:
        raise ValueError(f"Unknown settings section {section!r}")
    with _lock:  # read-modify-write as one step, so concurrent updates never lose changes
        current = load()
        if isinstance(DEFAULTS[section], dict):
            if not isinstance(values, dict):
                raise ValueError("Settings values must be an object")
            unknown = set(values) - set(DEFAULTS[section])
            if unknown:
                raise ValueError(f"Unknown setting(s) in {section}: {', '.join(sorted(unknown))}")
            current[section].update(values)
        else:
            current[section] = values
        artifacts.write_json_atomic(_path(), current)
        return current[section]
