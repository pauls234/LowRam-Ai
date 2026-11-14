"""RAM inspection and the adjustable 20-80 % planning budget.

The budget is a *scheduling hint*, not a hard memory cap: LowRAM AI does not
control how much memory the inference server allocates. The budget sizes the
prompt (carry-over context and ``max_tokens``), and a task only starts when
the machine has at least ``LOWRAM_MIN_FREE_MB`` of available RAM.

:func:`guard_wait` waits for RAM to recover instead of giving up, and a
background sampler (started by the dashboard, never on import) keeps
:func:`pressure` – ``ok`` / ``warn`` / ``critical`` – for the UI.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import psutil

from . import config, web

log = logging.getLogger("lowram.memory")

MIN_PERCENT = 20
MAX_PERCENT = 80
DEFAULT_PERCENT = 40

GIB = 1024**3
CHARS_PER_GIB = 2000
MIN_CONTEXT_CHARS = 2000
MAX_CONTEXT_CHARS = 24000
MIN_MAX_TOKENS = 256
MAX_MAX_TOKENS = 2048
TOKENS_PER_GIB = 96


def validate_percent(value: object) -> int:
    """Return ``value`` as an int in [20, 80] or raise ``ValueError``."""
    if isinstance(value, bool):
        raise ValueError("RAM budget must be a number between 20 and 80")
    if isinstance(value, float):
        if not value.is_integer():  # also rejects inf / nan
            raise ValueError("RAM budget must be a whole number")
        value = int(value)
    try:
        percent = int(value.strip()) if isinstance(value, str) else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"RAM budget must be a whole number between {MIN_PERCENT} and {MAX_PERCENT}") from exc
    if not MIN_PERCENT <= percent <= MAX_PERCENT:
        raise ValueError(f"RAM budget must be between {MIN_PERCENT} and {MAX_PERCENT} percent, got {percent}")
    return percent


@dataclass(frozen=True)
class MemoryPlan:
    percent: int
    total_bytes: int
    available_bytes: int
    used_percent: float
    budget_bytes: int
    min_free_bytes: int
    context_chars: int
    max_tokens: int
    can_run: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


def _mib(n: int) -> int:
    return int(n // (1024 * 1024))


def plan(percent: object = DEFAULT_PERCENT) -> MemoryPlan:
    """Measure available RAM and derive prompt limits for ``percent``."""
    pct = validate_percent(percent)
    vm = psutil.virtual_memory()
    budget = int(vm.total * pct / 100)
    budget_gib = budget / GIB
    min_free = config.min_free_bytes()
    can_run = vm.available >= min_free
    if can_run:
        reason = f"{_mib(vm.available)} MiB available (minimum {_mib(min_free)} MiB)."
    else:
        reason = (
            f"Paused: only {_mib(vm.available)} MiB RAM available, at least {_mib(min_free)} MiB "
            "required (LOWRAM_MIN_FREE_MB). Close other apps or use a smaller model."
        )
    return MemoryPlan(
        percent=pct,
        total_bytes=int(vm.total),
        available_bytes=int(vm.available),
        used_percent=float(vm.percent),
        budget_bytes=budget,
        min_free_bytes=min_free,
        context_chars=_clamp(budget_gib * CHARS_PER_GIB, MIN_CONTEXT_CHARS, MAX_CONTEXT_CHARS),
        max_tokens=_clamp(MIN_MAX_TOKENS + budget_gib * TOKENS_PER_GIB, MIN_MAX_TOKENS, MAX_MAX_TOKENS),
        can_run=can_run,
        reason=reason,
    )


# --------------------------------------------------------------------------- RAM guard

GUARD_INTERVAL_S = 5.0


def guard_wait(
    reason: str = "",
    max_wait_s: float = 0.0,
    stop_event: threading.Event | None = None,
    on_wait: Callable[[str], None] | None = None,
) -> bool:
    """Wait until a task may start (``plan().can_run``), checking every 5 s.

    Returns True as soon as enough RAM is free, False after ``max_wait_s``
    seconds or when ``stop_event`` is set (noticed at once). Before every
    pause ``on_wait(message)`` gets a status line such as
    ``"Warte auf freien Arbeitsspeicher (512 MB frei)…"``; ``reason`` names
    the waiting job in the log.
    """
    deadline = time.monotonic() + max(0.0, float(max_wait_s))
    logged = False
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        current = plan()
        if current.can_run:
            if logged:
                log.info("%s: enough RAM again (%s MiB available)", reason or "RAM guard", _mib(current.available_bytes))
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        message = f"Warte auf freien Arbeitsspeicher ({_mib(current.available_bytes)} MB frei)…"
        if not logged:
            logged = True
            log.info("%s: %s (needs %s MiB, waits up to %.0f s)", reason or "RAM guard", message,
                     _mib(current.min_free_bytes), remaining)
        if on_wait is not None:
            on_wait(message)
        pause = min(GUARD_INTERVAL_S, remaining)
        if stop_event is not None:
            stop_event.wait(pause)
        else:
            time.sleep(pause)


# --------------------------------------------------------------------------- memory pressure

PRESSURE_INTERVAL_S = 3.0
MAC_PRESSURE_CACHE_S = 3.0
SYSCTL_TIMEOUT_S = 2.0
SWAP_WINDOW_S = 60.0
SWAP_WARN_MB_PER_MIN = 64.0
SWAP_CRITICAL_MB_PER_MIN = 256.0
LEVELS = ("ok", "warn", "critical")
_MAC_LEVELS = {1: "ok", 2: "warn", 4: "critical"}  # kern.memorystatus_vm_pressure_level

_pressure_lock = threading.Lock()
_swap_history: deque[tuple[float, int]] = deque()
_latest: tuple[float, dict] | None = None
_mac_cache: tuple[float, str | None] | None = None
_mac_unsupported = False


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _sysctl() -> str:
    return "/usr/sbin/sysctl" if os.path.exists("/usr/sbin/sysctl") else "sysctl"


def mac_pressure_level() -> str | None:
    """macOS ``kern.memorystatus_vm_pressure_level`` as ok/warn/critical (cached 3 s); None elsewhere."""
    global _mac_cache, _mac_unsupported
    if _mac_unsupported or not _is_macos():
        return None
    with _pressure_lock:
        cached = _mac_cache
    if cached is not None and time.monotonic() - cached[0] < MAC_PRESSURE_CACHE_S:
        return cached[1]
    level = None
    try:
        proc = subprocess.run(
            [_sysctl(), "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=SYSCTL_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
        )
        if proc.returncode == 0:
            level = _MAC_LEVELS.get(int(proc.stdout.strip()))
    except FileNotFoundError:
        _mac_unsupported = True
    except (OSError, subprocess.SubprocessError, ValueError):
        level = None
    with _pressure_lock:
        _mac_cache = (time.monotonic(), level)
    return level


def _swap_growth(now: float, swap_used: int) -> float:
    """MB per minute the swap grew over the last ``SWAP_WINDOW_S`` (negative = shrinking)."""
    with _pressure_lock:
        _swap_history.append((now, swap_used))
        while len(_swap_history) > 2 and now - _swap_history[1][0] >= SWAP_WINDOW_S:
            _swap_history.popleft()
        then, before = _swap_history[0]
    elapsed = now - then
    if elapsed < 1.0:
        return 0.0
    return round((swap_used - before) / (1024 * 1024) / (elapsed / 60.0), 1)


def sample_pressure() -> dict:
    """Measure memory pressure now: ``{level, available_bytes, swap_used_bytes, swap_growth_mb_per_min, source}``.

    ``critical``: less than ``LOWRAM_MIN_FREE_MB`` available or swap growing
    fast; ``warn``: less than twice that or swap growing steadily. On macOS
    the kernel's own pressure level is used when available.
    """
    global _latest
    vm = psutil.virtual_memory()
    available = int(vm.available)
    try:
        swap_used = int(psutil.swap_memory().used)
    except Exception:  # noqa: BLE001 - some systems cannot report swap
        swap_used = 0
    now = time.monotonic()
    growth = _swap_growth(now, swap_used)
    try:
        min_free = config.min_free_bytes()
    except config.ConfigError:
        min_free = config.DEFAULT_MIN_FREE_MB * 1024 * 1024
    if available < min_free or growth >= SWAP_CRITICAL_MB_PER_MIN:
        level = "critical"
    elif available < 2 * min_free or growth >= SWAP_WARN_MB_PER_MIN:
        level = "warn"
    else:
        level = "ok"
    mac = mac_pressure_level()
    if mac is not None:  # the kernel knows best; below the task minimum stays critical
        level = mac if available >= min_free else "critical"
    result = {
        "level": level,
        "available_bytes": available,
        "swap_used_bytes": swap_used,
        "swap_growth_mb_per_min": growth,
        "source": "macos" if mac is not None else "psutil",
    }
    with _pressure_lock:
        _latest = (now, result)
    return dict(result)


def pressure() -> dict:
    """Current memory pressure for the UI (``GET /api/memory``).

    While the background sampler runs, its latest sample is returned (at most
    two intervals old); otherwise the pressure is measured now.
    """
    with _pressure_lock:
        latest = _latest
    if sampler_running() and latest is not None and time.monotonic() - latest[0] <= 2 * PRESSURE_INTERVAL_S:
        return dict(latest[1])
    return sample_pressure()


_sampler_lock = threading.Lock()
_sampler: tuple[threading.Thread, threading.Event] | None = None


def _sample_forever(stop: threading.Event, interval: float) -> None:
    while not stop.is_set():
        try:
            sample_pressure()
        except Exception:  # noqa: BLE001 - keep sampling; the UI falls back to a direct measurement
            log.debug("Memory pressure sample failed", exc_info=True)
        stop.wait(interval)


def sampler_running() -> bool:
    with _sampler_lock:
        return _sampler is not None and _sampler[0].is_alive()


def start_pressure_sampler(interval: float = PRESSURE_INTERVAL_S) -> threading.Thread:
    """Start the background sampler (idempotent: a running sampler is reused)."""
    global _sampler
    with _sampler_lock:
        if _sampler is not None and _sampler[0].is_alive():
            return _sampler[0]
        stop = threading.Event()
        thread = threading.Thread(target=_sample_forever, args=(stop, interval), name="lowram-memory-pressure",
                                  daemon=True)
        _sampler = (thread, stop)
        thread.start()
        return thread


def stop_pressure_sampler(timeout: float = 5.0) -> None:
    """Stop the background sampler (no-op when it is not running)."""
    global _sampler
    with _sampler_lock:
        current, _sampler = _sampler, None
    if current is None:
        return
    thread, stop = current
    stop.set()
    if thread is not threading.current_thread():
        thread.join(timeout)


@web.on_startup
def _start_sampler_on_startup(server: Any) -> None:
    start_pressure_sampler()


@web.on_shutdown
def _stop_sampler_on_shutdown(server: Any) -> None:
    stop_pressure_sampler()
