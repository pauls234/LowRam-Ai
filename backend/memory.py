"""RAM inspection and the adjustable 20-80 % planning budget.

The budget is a *scheduling hint*, not a hard memory cap: LowRAM AI does not
control how much memory the inference server allocates. The budget sizes the
prompt (carry-over context and ``max_tokens``), and a task only starts when
the machine has at least ``LOWRAM_MIN_FREE_MB`` of available RAM.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import psutil

from . import config

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
