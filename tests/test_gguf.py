"""gguf.py: the model file header – exact KV-cache size, and safe against broken or crafted files."""

import os
import random
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend import gguf


def text(value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return struct.pack("<Q", len(data)) + data


def encode(kind: int, value) -> bytes:
    if kind == gguf.STRING:
        return text(value)
    if kind == gguf.ARRAY:
        item_kind, items = value
        return struct.pack("<IQ", item_kind, len(items)) + b"".join(encode(item_kind, item) for item in items)
    return gguf._SCALARS[kind].pack(value)


def header(pairs, *, version: int = 3, tensors: int = 1, magic: bytes = b"GGUF", count: int | None = None) -> bytes:
    body = b"".join(text(key) + struct.pack("<I", kind) + encode(kind, value) for key, kind, value in pairs)
    return magic + struct.pack("<IQQ", version, tensors, len(pairs) if count is None else count) + body


def model(arch: str, *, layers: int, heads, kv_heads=None, embedding: int, context: int | None = 32768,
          key_length: int | None = None, value_length: int | None = None, extra=(), vocab: int = 5000) -> list:
    """Header pairs like the ones llama.cpp's converter writes (plus a small vocabulary)."""
    def number(value):
        return (gguf.ARRAY, (gguf.INT32, value)) if isinstance(value, list) else (gguf.UINT32, value)

    pairs = [("general.architecture", gguf.STRING, arch), ("general.name", gguf.STRING, "Test model"),
             (f"{arch}.block_count", gguf.UINT32, layers), (f"{arch}.embedding_length", gguf.UINT32, embedding),
             (f"{arch}.attention.head_count", *number(heads))]
    if context is not None:
        pairs.append((f"{arch}.context_length", gguf.UINT32, context))
    if kv_heads is not None:
        pairs.append((f"{arch}.attention.head_count_kv", *number(kv_heads)))
    if key_length is not None:
        pairs.append((f"{arch}.attention.key_length", gguf.UINT32, key_length))
    if value_length is not None:
        pairs.append((f"{arch}.attention.value_length", gguf.UINT32, value_length))
    pairs += list(extra)
    pairs += [("tokenizer.ggml.model", gguf.STRING, "gpt2"),
              ("tokenizer.ggml.tokens", gguf.ARRAY, (gguf.STRING, [f"t{i}" for i in range(vocab)])),
              ("tokenizer.ggml.token_type", gguf.ARRAY, (gguf.INT32, [1] * vocab)),
              ("tokenizer.chat_template", gguf.STRING, "{{ messages }}")]
    return pairs


QWEN25_7B = model("qwen2", layers=28, heads=28, kv_heads=4, embedding=3584)


class GGUFTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        gguf._cache.clear()
        self.addCleanup(gguf._cache.clear)

    def write(self, data: bytes, name: str = "model.gguf", weights: int = 1024) -> Path:
        path = self.dir / name
        path.write_bytes(data + b"\0" * weights)
        return path


class LayoutTests(GGUFTestCase):
    def test_qwen25_7b_matches_what_llamacpp_reports(self):
        layout = gguf.kv_layout(self.write(header(QWEN25_7B)))
        self.assertEqual(layout, gguf.KVLayout("qwen2", 28, 28 * 4 * (128 + 128), 32768))
        # llama.cpp: "KV cache size = 1792.00 MiB (32768 cells, 28 layers)" (f16), 952 MiB with q8_0
        self.assertEqual(layout.bytes_per_token(kv_q8=False) * 32768 / 1024**2, 1792)
        self.assertEqual(layout.bytes_per_token(kv_q8=True) * 32768 / 1024**2, 952)

    def test_explicit_head_lengths_and_full_attention(self):
        gemma = model("gemma3", layers=34, heads=8, kv_heads=4, embedding=2560, key_length=256, value_length=256,
                      context=131072)
        self.assertEqual(gguf.layout_from_metadata(gguf.read_metadata(self.write(header(gemma)))),
                         gguf.KVLayout("gemma3", 34, 34 * 4 * 512, 131072))
        phi = model("phi3", layers=32, heads=32, embedding=3072, context=4096)  # no head_count_kv: every head
        self.assertEqual(gguf.kv_layout(self.write(header(phi), "phi.gguf")).values_per_token, 32 * 32 * (96 + 96))

    def test_per_layer_heads_and_layers_without_cache(self):
        mixed = model("deci", layers=4, heads=[32, 32, 32, 32], kv_heads=[8, 0, 4, 8], embedding=4096)
        self.assertEqual(gguf.kv_layout(self.write(header(mixed))).values_per_token, (8 + 0 + 4 + 8) * 256)
        wrong = model("deci", layers=4, heads=32, kv_heads=[8, 8], embedding=4096)  # not one value per layer
        self.assertIsNone(gguf.kv_layout(self.write(header(wrong), "wrong.gguf")))

    def test_other_cache_designs_keep_the_estimate(self):
        cases = {
            "mla": model("deepseek2", layers=27, heads=16, kv_heads=16, embedding=2048,
                         extra=[("deepseek2.attention.kv_lora_rank", gguf.UINT32, 512)]),
            "ssm": model("granitehybrid", layers=40, heads=32, kv_heads=8, embedding=4096,
                         extra=[("granitehybrid.ssm.state_size", gguf.UINT32, 128)]),
            "rwkv": model("rwkv7", layers=24, heads=32, embedding=2048),
            "mamba": model("mamba", layers=64, heads=1, embedding=2560),
        }
        for name, pairs in cases.items():
            with self.subTest(name):
                self.assertIsNone(gguf.kv_layout(self.write(header(pairs), f"{name}.gguf")))

    def test_missing_or_odd_values(self):
        def without(key):
            return [pair for pair in QWEN25_7B if pair[0] != key]

        for pairs in (without("general.architecture"), without("qwen2.block_count"),
                      without("qwen2.attention.head_count"), without("qwen2.embedding_length"),
                      model("qwen2", layers=0, heads=28, kv_heads=4, embedding=3584),
                      model("qwen2", layers=28, heads=0, kv_heads=4, embedding=3584),
                      model("qwen2", layers=28, heads=28, kv_heads=0, embedding=3584),
                      model("qwen2", layers=5000, heads=28, kv_heads=4, embedding=3584),
                      [("general.architecture", gguf.UINT32, 7)] + QWEN25_7B[1:],
                      [("general.architecture", gguf.STRING, "x" * 100)] + QWEN25_7B[1:]):
            with self.subTest(pairs[0]):
                self.assertIsNone(gguf.layout_from_metadata(gguf.read_metadata(self.write(header(pairs)))))
        # no trained context in the file: the layout still counts, the context is just unknown
        layout = gguf.kv_layout(self.write(header(model("qwen2", layers=2, heads=4, embedding=512, context=None)),
                                           "nocontext.gguf"))
        self.assertEqual((layout.values_per_token, layout.context_length), (2 * 4 * 256, None))

    def test_only_the_needed_values_are_kept(self):
        path = self.write(header(QWEN25_7B))
        meta = gguf.read_metadata(path)
        self.assertEqual(meta["general.name"], "Test model")
        self.assertIsNone(meta["tokenizer.ggml.tokens"])  # texts of the vocabulary: stepped over
        self.assertIsNone(meta["tokenizer.ggml.token_type"])  # longer than MAX_KEPT_ITEMS: stepped over
        self.assertEqual(set(gguf.read_metadata(path, lambda key: key.endswith("block_count"))),
                         {"qwen2.block_count"})
        values = [("flag", gguf.BOOL, 1), ("flags", gguf.ARRAY, (gguf.BOOL, [0, 2])), ("x", gguf.FLOAT64, 0.5),
                  ("big", gguf.UINT64, 2**63), ("neg", gguf.INT8, -3)]
        self.assertEqual(gguf.read_metadata(self.write(header(values), "v.gguf")),
                         {"flag": True, "flags": [False, True], "x": 0.5, "big": 2**63, "neg": -3})

    def test_cached_per_file_version_and_never_raises(self):
        path = self.write(header(QWEN25_7B))
        first = gguf.kv_layout(path)
        with mock.patch.object(gguf, "read_metadata", side_effect=AssertionError("read again")):
            self.assertEqual(gguf.kv_layout(path), first)
        path.write_bytes(header(model("qwen2", layers=2, heads=4, embedding=512)))  # replaced: other size
        self.assertEqual(gguf.kv_layout(path).layers, 2)
        self.assertIsNone(gguf.kv_layout(self.dir / "missing.gguf"))
        with mock.patch.object(gguf, "read_metadata", side_effect=RuntimeError("bug")), \
                self.assertLogs("lowram.gguf", "WARNING"):
            self.assertIsNone(gguf.kv_layout(self.write(b"GGUF", "other.gguf")))

    def test_a_real_sized_vocabulary_is_quick(self):
        pairs = QWEN25_7B[:-3] + [("tokenizer.ggml.tokens", gguf.ARRAY, (gguf.STRING, ["x"] * 152064)),
                                  ("tokenizer.ggml.merges", gguf.ARRAY, (gguf.STRING, ["a b"] * 151387))]
        path = self.write(header(pairs))
        started = time.monotonic()
        self.assertIsNotNone(gguf.kv_layout(path))
        self.assertLess(time.monotonic() - started, 5)


class HostileFileTests(GGUFTestCase):
    """A broken or crafted file must end in GGUFError – quickly, without big allocations."""

    def assertRefused(self, data: bytes, name: str = "bad.gguf"):
        path = self.write(data, name, weights=0)
        with self.assertRaises(gguf.GGUFError):
            gguf.read_metadata(path)
        self.assertIsNone(gguf.kv_layout(path))

    def test_not_gguf_or_unknown_version(self):
        self.assertRefused(b"")
        self.assertRefused(b"PK\x03\x04" + b"\0" * 100)
        self.assertRefused(header(QWEN25_7B, magic=b"FUGG"))
        for version in (0, 1, 4, 2**32 - 1):
            self.assertRefused(header(QWEN25_7B, version=version), f"v{version}.gguf")
        self.assertIsNotNone(gguf.read_metadata(self.write(header(QWEN25_7B, version=2), "v2.gguf")))

    def test_every_truncation_is_refused(self):
        data = header(model("llama", layers=2, heads=4, embedding=256, vocab=3))
        for cut in range(len(data)):
            with self.subTest(cut=cut):
                self.assertRefused(data[:cut], "cut.gguf")

    def test_huge_lengths_and_counts(self):
        def raw(key: bytes, kind: int, payload: bytes, count: int = 1) -> bytes:
            return b"GGUF" + struct.pack("<IQQ", 3, 0, count) + key + struct.pack("<I", kind) + payload

        key = text("k")
        self.assertRefused(b"GGUF" + struct.pack("<IQQ", 3, 0, 2**40))  # implausibly many keys
        self.assertRefused(b"GGUF" + struct.pack("<IQQ", 3, 2**60, 0))  # implausibly many tensors
        self.assertRefused(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2**63))  # key length
        self.assertRefused(raw(key, gguf.STRING, struct.pack("<Q", 2**64 - 1)))  # text length
        self.assertRefused(raw(key, gguf.STRING, struct.pack("<Q", 10**6) + b"x"))  # text beyond the end
        self.assertRefused(raw(key, gguf.ARRAY, struct.pack("<IQ", gguf.UINT64, 2**62)))  # array length
        self.assertRefused(raw(key, gguf.ARRAY, struct.pack("<IQ", gguf.UINT8, 10**7)))  # array beyond the end
        self.assertRefused(raw(key, gguf.ARRAY, struct.pack("<IQ", gguf.ARRAY, 1) + struct.pack("<IQ", 0, 0)))
        self.assertRefused(raw(key, 13, b"\0" * 8))  # unknown value type
        self.assertRefused(raw(key, gguf.ARRAY, struct.pack("<IQ", 99, 1) + b"\0" * 8))  # unknown item type
        with mock.patch.object(gguf, "MAX_STEPS", 100):  # vocabulary bombs: many texts are counted
            self.assertRefused(raw(key, gguf.ARRAY, struct.pack("<IQ", gguf.STRING, 101) + text("") * 101))
        with mock.patch.object(gguf, "MAX_HEADER_BYTES", 64):  # never more than this is read
            self.assertRefused(header(QWEN25_7B))

    def test_random_damage_never_escapes(self):
        data = bytearray(header(model("llama", layers=2, heads=4, kv_heads=2, embedding=256, vocab=20)))
        rng = random.Random(1234)
        started = time.monotonic()
        for _ in range(400):
            damaged = bytearray(data)
            for _ in range(rng.randint(1, 6)):
                damaged[rng.randrange(len(damaged))] = rng.randrange(256)
            path = self.write(bytes(damaged), "damaged.gguf", weights=rng.choice((0, 64)))
            try:
                gguf.read_metadata(path)
            except gguf.GGUFError:
                pass
            gguf._cache.clear()
            result = gguf.kv_layout(path)
            self.assertTrue(result is None or isinstance(result, gguf.KVLayout))
        self.assertLess(time.monotonic() - started, 20)

    @unittest.skipIf(sys.platform == "win32", "POSIX only")
    def test_only_regular_files_are_read(self):
        with self.assertRaises(gguf.GGUFError):
            gguf.read_metadata(self.dir)
        fifo = self.dir / "pipe.gguf"
        os.mkfifo(fifo)
        started = time.monotonic()
        with self.assertRaises(gguf.GGUFError):  # would block forever without O_NONBLOCK
            gguf.read_metadata(fifo)
        self.assertIsNone(gguf.kv_layout(fifo))
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
