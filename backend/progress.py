"""Rough time estimates ("Pi mal Daumen") from what this computer actually measured.

How long repeated work took – loading a model, planning a big job, … – is remembered per kind and
key in ``<home>/durations.json`` (the last :data:`KEEP` values each). :func:`estimate` turns the
time spent so far and the typical duration into a percentage and the time that is probably left.

Estimates are honest rather than precise: they never claim more than :data:`MAX_RUNNING_PERCENT`
before the work is really done, and once something takes clearly longer than usual they say so
(``overdue``) instead of counting down below zero.
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
from pathlib import Path

from . import artifacts, config

log = logging.getLogger("lowram.progress")

FILE_NAME = "durations.json"
KEEP = 8  # remembered durations per kind/key
MAX_KEYS = 200
MAX_SECONDS = 7 * 24 * 3600
MAX_RUNNING_PERCENT = 95  # 100 % only when it is really finished
OVERDUE_FACTOR = 1.15  # this much longer than usual: "takes longer than usual" instead of a countdown

_lock = threading.Lock()


def _path() -> Path:
    return config.home_dir() / FILE_NAME


def _name(kind: str, key: str) -> str:
    return f"{kind}:{key}"[:200]


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _values(raw: object) -> list[float]:
    if not isinstance(raw, list):
        return []
    return [float(v) for v in raw if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= MAX_SECONDS]


def remember(kind: str, key: str, seconds: float) -> None:
    """Record one measured duration (e.g. ``remember("model_load", model_id, 21.4)``)."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= MAX_SECONDS:
        return
    name = _name(kind, key)
    with _lock:
        data = _load()
        values = _values(data.pop(name, None))[-(KEEP - 1):] + [round(float(seconds), 2)]
        data[name] = values  # most recently used last
        while len(data) > MAX_KEYS:
            data.pop(next(iter(data)))
        try:
            artifacts.write_json_atomic(_path(), data)
        except OSError as exc:  # an estimate is a nice-to-have; never fail the real work for it
            log.debug("Could not save durations: %s", exc)


def typical(kind: str, key: str) -> float | None:
    """The median of the remembered durations, or ``None`` if nothing was measured yet."""
    with _lock:
        values = _values(_load().get(_name(kind, key)))
    return float(statistics.median(values)) if values else None


def estimate(elapsed_s: float, expected_s: float | None) -> dict:
    """``{"percent", "eta_s", "overdue"}`` for work that usually takes ``expected_s`` seconds.

    Without an expectation both numbers are ``None``. Past :data:`OVERDUE_FACTOR` × the usual time,
    ``percent`` stays at :data:`MAX_RUNNING_PERCENT`, ``eta_s`` is ``None`` and ``overdue`` is set.
    """
    if not isinstance(expected_s, (int, float)) or expected_s <= 0:
        return {"percent": None, "eta_s": None, "overdue": False}
    elapsed = max(0.0, float(elapsed_s))
    if elapsed > expected_s * OVERDUE_FACTOR:
        return {"percent": MAX_RUNNING_PERCENT, "eta_s": None, "overdue": True}
    percent = min(MAX_RUNNING_PERCENT, int(100 * elapsed / expected_s))
    return {"percent": percent, "eta_s": round(max(1.0, expected_s - elapsed), 1), "overdue": False}
