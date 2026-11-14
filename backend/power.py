"""MacBook-friendly long jobs: keep the computer awake, pause on low battery, notifications.

* ``keep_awake(reason)`` – context manager, reference-counted across threads.
  macOS: one ``caffeinate -i -w <our pid>`` while any holder exists (it also
  exits by itself if LowRAM AI dies); Windows: ``SetThreadExecutionState`` on a
  dedicated keeper thread (the state belongs to the calling thread); Linux:
  no-op (logged once). Respects ``settings.power.keep_awake``.
* ``battery()`` / ``battery_block_reason()`` – psutil; long jobs pause on
  battery below ``settings.power.battery_min_percent`` (0 = never pause).
* ``notify(title, body)`` – system notification (macOS: ``osascript`` with the
  texts as *arguments*, never string-built AppleScript), rate-limited to one
  per title every 5 s, respects ``settings.power.notifications``.
* ``GET /api/power``.

Power management must never break a job: these helpers log failures and
never raise.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import psutil

from . import settings, web
from .web import Request, Response, route

log = logging.getLogger("lowram.power")

NOTIFY_INTERVAL_S = 5.0
MAX_TITLE_CHARS = 80
MAX_BODY_CHARS = 240
MAX_REASON_CHARS = 120
DEFAULT_MIN_PERCENT = 20
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
# Control characters, zero-width and bidi overrides: never shown in a notification.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]+")


def _platform() -> str:
    return sys.platform


def _setting(key: str, default: Any) -> Any:
    try:
        return settings.get("power", key)
    except Exception:  # missing section or unreadable file: fall back to the default
        log.debug("power.%s unavailable, using %r", key, default, exc_info=True)
        return default


def clean_text(value: object, limit: int) -> str:
    """Single-line, printable, length-capped text."""
    text = _UNSAFE.sub(" ", str(value if value is not None else ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- keep awake


class _Caffeinate:
    """macOS: prevent idle sleep while this process lives (``-w`` ties it to our pid)."""

    def __init__(self) -> None:
        exe = "/usr/bin/caffeinate" if os.path.exists("/usr/bin/caffeinate") else "caffeinate"
        self.proc = subprocess.Popen(
            [exe, "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        )

    def stop(self) -> None:
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)


def _win_set_state(flags: int) -> None:
    import ctypes

    func = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
    func.argtypes = [ctypes.c_uint32]
    func.restype = ctypes.c_uint32
    if func(flags) == 0:
        raise OSError("SetThreadExecutionState failed")


class _WindowsKeeper:
    """Windows: the execution state belongs to a thread, so one thread holds it."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lowram-keep-awake", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            _win_set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            log.warning("Could not keep Windows awake", exc_info=True)
            return
        self._stop.wait()
        try:
            _win_set_state(ES_CONTINUOUS)
        except Exception:
            log.warning("Could not release the Windows keep-awake state", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)


class _AwakeManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders: dict[int, str] = {}
        self._seq = 0
        self._inhibitor: Any = None
        self._linux_noted = False

    def acquire(self, reason: str) -> int | None:
        if not _setting("keep_awake", True):
            return None
        with self._lock:
            self._seq += 1
            token = self._seq
            self._holders[token] = reason
            if self._inhibitor is None:
                self._start(reason)
            return token

    def release(self, token: int | None) -> None:
        if token is None:
            return
        with self._lock:
            if self._holders.pop(token, None) is None or self._holders:
                return
            self._stop()

    def release_all(self) -> None:
        with self._lock:
            self._holders.clear()
            self._stop()

    def active(self) -> list[str]:
        with self._lock:
            return list(self._holders.values())

    def _start(self, reason: str) -> None:
        platform = _platform()
        try:
            if platform == "darwin":
                self._inhibitor = _Caffeinate()
            elif platform == "win32":
                self._inhibitor = _WindowsKeeper()
            else:
                if not self._linux_noted:
                    self._linux_noted = True
                    log.info("Keeping the computer awake is not supported on %s; long jobs may pause "
                             "if it sleeps", platform)
                return
            log.info("Keeping the computer awake (%s)", reason)
        except Exception:
            log.warning("Could not keep the computer awake", exc_info=True)
            self._inhibitor = None

    def _stop(self) -> None:
        inhibitor, self._inhibitor = self._inhibitor, None
        if inhibitor is None:
            return
        try:
            inhibitor.stop()
            log.info("The computer may sleep again")
        except Exception:
            log.warning("Could not stop keeping the computer awake", exc_info=True)


_MANAGER = _AwakeManager()


@contextlib.contextmanager
def keep_awake(reason: str = "long job") -> Iterator[None]:
    """Keep the computer from idle-sleeping while the block runs (never raises itself)."""
    token = None
    try:
        token = _MANAGER.acquire(clean_text(reason, MAX_REASON_CHARS) or "long job")
    except Exception:
        log.warning("keep_awake failed", exc_info=True)
    try:
        yield
    finally:
        try:
            _MANAGER.release(token)
        except Exception:
            log.warning("keep_awake release failed", exc_info=True)


def active_reasons() -> list[str]:
    return _MANAGER.active()


@web.on_shutdown
def _release_on_shutdown(server: Any) -> None:
    _MANAGER.release_all()


# --------------------------------------------------------------------------- battery


def battery() -> dict:
    """``{available, percent, plugged}``; ``available`` is False on desktops (or if unknown)."""
    try:
        info = psutil.sensors_battery()
    except Exception:  # not implemented on this platform, or a sensor error
        info = None
    if info is None or info.percent is None:
        return {"available": False, "percent": None, "plugged": None}
    plugged = info.power_plugged
    return {"available": True, "percent": round(float(info.percent)),
            "plugged": None if plugged is None else bool(plugged)}


def min_battery_percent() -> int:
    value = _setting("battery_min_percent", DEFAULT_MIN_PERCENT)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        return DEFAULT_MIN_PERCENT
    return int(value)


def battery_block_reason() -> str | None:
    """Why long jobs should wait right now (on battery below the minimum), else None."""
    try:
        minimum = min_battery_percent()
        if minimum <= 0:
            return None
        state = battery()
        if not state["available"] or state["plugged"] is not False or state["percent"] >= minimum:
            return None
        return (f"On battery at {state['percent']} % – long jobs pause below {minimum} %. "
                "Plug in to continue.")
    except Exception:
        log.warning("Battery check failed", exc_info=True)
        return None


# --------------------------------------------------------------------------- notifications

_notify_lock = threading.Lock()
_last_notified: dict[str, float] = {}


def _no_option(text: str) -> str:
    # Arguments after the script must never look like options (e.g. "-e").
    return " " + text if text.startswith("-") else text


def notification_command(title: str, body: str, platform: str | None = None) -> list[str] | None:
    """Argument list that shows a notification on this platform, or None (unsupported)."""
    platform = platform or _platform()
    if platform == "darwin":
        exe = "/usr/bin/osascript" if os.path.exists("/usr/bin/osascript") else "osascript"
        return [exe, "-e", "on run argv",
                "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run", _no_option(title), _no_option(body)]
    if platform.startswith("linux"):
        exe = shutil.which("notify-send")
        return [exe, "--app-name=LowRAM AI", "--", title, body] if exe else None
    return None  # Windows: no notification without extra dependencies


def _deliver(title: str, body: str) -> None:
    try:
        argv = notification_command(title, body)
        if argv is None:
            log.debug("No system notifications on %s: %s – %s", _platform(), title, body)
            return
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, check=False)
    except Exception:
        log.warning("Could not show a notification", exc_info=True)


def notify(title: object, body: object = "") -> bool:
    """Show a system notification in the background. True if one was sent; never raises."""
    try:
        if not _setting("notifications", True):
            return False
        clean_title = clean_text(title, MAX_TITLE_CHARS) or "LowRAM AI"
        clean_body = clean_text(body, MAX_BODY_CHARS)
        now = time.monotonic()
        with _notify_lock:
            last = _last_notified.get(clean_title)
            if last is not None and now - last < NOTIFY_INTERVAL_S:
                return False
            if len(_last_notified) > 256:
                _last_notified.clear()
            _last_notified[clean_title] = now
        threading.Thread(target=_deliver, args=(clean_title, clean_body), name="lowram-notify", daemon=True).start()
        return True
    except Exception:
        log.warning("notify failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- API


def status() -> dict:
    return {
        "battery": battery(),
        "keep_awake": {"enabled": bool(_setting("keep_awake", True)), "active": active_reasons()},
        "settings": _setting_section(),
    }


def _setting_section() -> dict:
    try:
        return dict(settings.get("power"))
    except Exception:
        return dict(settings.DEFAULTS.get("power", {}))


@route("GET", r"/api/power")
def get_power(req: Request) -> Response:
    return Response(status())
