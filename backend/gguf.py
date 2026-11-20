"""A small, careful reader for the header of GGUF model files (the llama.cpp format).

Only the metadata at the start of a file is read – never the weights. Every length, every count and
the total amount read are limited, so a broken or deliberately crafted file cannot make LowRAM AI
allocate much memory or spin for long: :class:`GGUFError` is raised instead, and callers keep
their rough estimates.

:func:`kv_layout` gives the exact size of the KV cache: the memory one token of context needs
follows from the attention layout written in the file (layers × KV heads × (key + value length)).
Models with another cache design (MLA, recurrent or state-space layers) give ``None`` – the rough,
deliberately generous estimate stays in charge there.
"""

from __future__ import annotations

import logging
import os
import stat
import struct
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("lowram.gguf")

MAGIC = b"GGUF"
VERSIONS = (2, 3)  # version 1 (2023) used 32-bit lengths; llama.cpp no longer loads it either
MAX_HEADER_BYTES = 64 * 1024**2  # real models need a few MB, mostly for the tokenizer
MAX_TENSORS = 1_000_000
MAX_KEYS = 100_000
MAX_KEY_BYTES = 65_535  # the format's own limit
MAX_TEXT_BYTES = 16 * 1024**2
MAX_ITEMS = 32_000_000  # items of one array
MAX_STEPS = 4_000_000  # texts stepped over one by one (a vocabulary has ~150 000-260 000)
MAX_KEPT_ITEMS = 4096  # arrays up to this length are kept (per-layer values); longer ones are skipped
MAX_KEPT_TEXT = 1024**2
READ_BUFFER = 1024**2

UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL, STRING, ARRAY, UINT64, INT64, FLOAT64 = range(13)
_SCALARS = {kind: struct.Struct(fmt) for kind, fmt in (
    (UINT8, "<B"), (INT8, "<b"), (UINT16, "<H"), (INT16, "<h"), (UINT32, "<I"), (INT32, "<i"),
    (FLOAT32, "<f"), (BOOL, "<B"), (UINT64, "<Q"), (INT64, "<q"), (FLOAT64, "<d"))}
_U32, _U64 = _SCALARS[UINT32], _SCALARS[UINT64]

F16_BYTES_PER_VALUE = 2.0
Q8_0_BYTES_PER_VALUE = 34 / 32  # q8_0 stores blocks of 32 values in 34 bytes


class GGUFError(ValueError):
    """Not a GGUF header that can be read safely (broken, truncated, unknown version or beyond the limits)."""


class _Reader:
    def __init__(self, handle, size: int) -> None:
        self._handle = handle
        self._end = min(size, MAX_HEADER_BYTES)
        self._pos = 0
        self._steps = 0

    def _room(self, n: int) -> None:
        if n < 0 or self._pos + n > self._end:
            raise GGUFError("the header is truncated or larger than allowed")

    def read(self, n: int) -> bytes:
        self._room(n)
        data = self._handle.read(n)
        if len(data) != n:
            raise GGUFError("the file ends early")
        self._pos += n
        return data

    def skip(self, n: int) -> None:
        self._room(n)
        self._handle.seek(n, os.SEEK_CUR)
        self._pos += n

    def scalar(self, fmt: struct.Struct):
        return fmt.unpack(self.read(fmt.size))[0]

    def text(self, keep: bool = True, limit: int = MAX_TEXT_BYTES) -> str | None:
        """A length-prefixed UTF-8 text; ``None`` when stepped over (not wanted or very long)."""
        n = self.scalar(_U64)
        if n > limit:
            raise GGUFError("a text in the header is too long")
        if keep and n <= MAX_KEPT_TEXT:
            return self.read(n).decode("utf-8", "replace")
        self.skip(n)
        return None

    def value(self, kind: int, keep: bool):
        """One value of type ``kind``; ``None`` when stepped over (not wanted, a long array, texts)."""
        fmt = _SCALARS.get(kind)
        if fmt is not None:
            value = self.scalar(fmt)
            if not keep:
                return None
            return value != 0 if kind == BOOL else value
        if kind == STRING:
            return self.text(keep)
        if kind != ARRAY:
            raise GGUFError(f"unknown value type {kind} in the header")
        item_kind = self.scalar(_U32)
        count = self.scalar(_U64)
        if count > MAX_ITEMS:
            raise GGUFError("an array in the header is too long")
        fmt = _SCALARS.get(item_kind)
        if fmt is not None:
            if keep and count <= MAX_KEPT_ITEMS:
                values = [item for (item,) in fmt.iter_unpack(self.read(fmt.size * count))]
                return [item != 0 for item in values] if item_kind == BOOL else values
            self.skip(fmt.size * count)
            return None
        if item_kind == STRING:  # e.g. the vocabulary: stepped over text by text
            self._steps += count
            if self._steps > MAX_STEPS:
                raise GGUFError("too many texts in the header")
            for _ in range(count):
                self.text(keep=False)
            return None
        # nested arrays: llama.cpp refuses them as well
        raise GGUFError(f"unsupported array type {item_kind} in the header")


def _open(path: str | os.PathLike):
    """Open a regular file for reading (a FIFO or device would block or never end)."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(os.fspath(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise GGUFError("not a regular file")
        return os.fdopen(fd, "rb", buffering=READ_BUFFER), info.st_size
    except BaseException:
        os.close(fd)
        raise


def read_metadata(path: str | os.PathLike, want: Callable[[str], bool] | None = None) -> dict[str, object]:
    """The key/value pairs of a GGUF v2/v3 header; ``want(key)`` picks the keys to keep (default: all).

    Long arrays (more than :data:`MAX_KEPT_ITEMS` items), arrays of texts and texts longer than
    :data:`MAX_KEPT_TEXT` are stepped over and kept as ``None``. Raises :class:`GGUFError` for
    anything that is not a sound header, ``OSError`` when the file cannot be read.
    """
    handle, size = _open(path)
    with handle:
        reader = _Reader(handle, size)
        if reader.read(4) != MAGIC:
            raise GGUFError("not a GGUF file")
        version = reader.scalar(_U32)
        if version not in VERSIONS:
            raise GGUFError(f"GGUF version {version} is not supported")
        tensors, count = reader.scalar(_U64), reader.scalar(_U64)
        if tensors > MAX_TENSORS or count > MAX_KEYS:
            raise GGUFError("the header lists implausibly many entries")
        meta: dict[str, object] = {}
        for _ in range(count):
            key = reader.text(limit=MAX_KEY_BYTES) or ""
            keep = want is None or want(key)
            value = reader.value(reader.scalar(_U32), keep)
            if keep:
                meta[key] = value
    return meta


# --------------------------------------------------------------------------- KV cache layout

@dataclass(frozen=True)
class KVLayout:
    """What one token of context costs in the KV cache of llama.cpp."""

    architecture: str
    layers: int
    values_per_token: int  # Σ over layers: KV heads × (key length + value length)
    context_length: int | None  # the context the model was trained for

    def bytes_per_token(self, *, kv_q8: bool) -> float:
        return self.values_per_token * (Q8_0_BYTES_PER_VALUE if kv_q8 else F16_BYTES_PER_VALUE)


_SUFFIXES = ("block_count", "context_length", "embedding_length", "attention.head_count",
             "attention.head_count_kv", "attention.key_length", "attention.value_length")
# A different cache design: the per-token formula below does not describe it (or not completely).
_OTHER_DESIGN_PARTS = (".ssm.", ".wkv.", ".shortconv.", ".attention.kv_lora_rank", ".time_mix", ".token_shift")
_OTHER_DESIGN_ARCHS = ("mamba", "rwkv", "arwkv")
MAX_LAYERS = 1024
MAX_VALUES_PER_TOKEN = 4_000_000  # far above any real model (a 405B model: ~260 000)


def _wanted(key: str) -> bool:
    if key == "general.architecture" or any(part in key for part in _OTHER_DESIGN_PARTS):
        return True
    _, dot, rest = key.partition(".")
    return bool(dot) and rest in _SUFFIXES


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _per_layer(value: object, layers: int) -> list[int] | None:
    """One number for every layer: a single value for all of them, or a list (some models vary it)."""
    if isinstance(value, list):
        numbers = [_count(item) for item in value]
        return numbers if len(numbers) == layers and None not in numbers else None  # type: ignore[return-value]
    number = _count(value)
    return None if number is None else [number] * layers


def layout_from_metadata(meta: dict[str, object]) -> KVLayout | None:
    """The KV-cache layout llama.cpp uses for these header values, or ``None`` (missing, odd, other design)."""
    arch = meta.get("general.architecture")
    if not isinstance(arch, str) or not arch or len(arch) > 64:
        return None
    if arch.startswith(_OTHER_DESIGN_ARCHS) or any(part in key for key in meta for part in _OTHER_DESIGN_PARTS):
        return None
    prefix = arch + "."
    layers = _count(meta.get(prefix + "block_count"))
    if not layers or layers > MAX_LAYERS:
        return None
    heads = _per_layer(meta.get(prefix + "attention.head_count"), layers)
    if heads is None or not heads[0]:
        return None
    raw_kv = meta.get(prefix + "attention.head_count_kv")
    kv_heads = heads if raw_kv is None else _per_layer(raw_kv, layers)
    if kv_heads is None:
        return None
    # like llama.cpp: without explicit lengths a head is embedding_length / head_count (of layer 0)
    embedding = _count(meta.get(prefix + "embedding_length"))
    default = embedding // heads[0] if embedding else None
    key_length = _count(meta.get(prefix + "attention.key_length")) or default
    value_length = _count(meta.get(prefix + "attention.value_length")) or default
    if not key_length or not value_length:
        return None
    values = sum(kv_heads) * (key_length + value_length)  # layers without KV heads hold no cache
    if not 0 < values <= MAX_VALUES_PER_TOKEN:
        return None
    trained = _count(meta.get(prefix + "context_length"))
    return KVLayout(arch, layers, values, trained if trained and trained <= 1 << 24 else None)


_cache_lock = threading.Lock()
_cache: OrderedDict[tuple, KVLayout | None] = OrderedDict()
CACHE_SIZE = 32


def kv_layout(path: str | os.PathLike) -> KVLayout | None:
    """The exact KV-cache layout of a GGUF model file, or ``None`` (unreadable, other design, limits).

    Cached per file (path, size, modification time); never raises.
    """
    try:
        info = os.stat(path)
        key = (os.path.realpath(path), info.st_size, info.st_mtime_ns)
    except (OSError, ValueError):
        return None
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    try:
        layout = layout_from_metadata(read_metadata(path, _wanted))
    except (OSError, GGUFError) as exc:
        log.info("Model header not readable (%s): %s", os.fspath(path), exc)
        layout = None
    except Exception:  # never let a header break the start of a model
        log.warning("Model header check failed (%s)", os.fspath(path), exc_info=True)
        layout = None
    with _cache_lock:
        _cache[key] = layout
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return layout
