"""Rough time estimates from durations measured on this computer (backend/progress.py)."""

import json
import os
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from backend import app, chat, cluster, config, llm, progress
from tests.helpers import LowRAMTestCase


class EstimateTests(unittest.TestCase):
    def test_without_expectation_nothing_is_guessed(self):
        for expected in (None, 0, -3):
            self.assertEqual(progress.estimate(5, expected), {"percent": None, "eta_s": None, "overdue": False})

    def test_percent_and_time_left(self):
        self.assertEqual(progress.estimate(0, 40), {"percent": 0, "eta_s": 40.0, "overdue": False})
        self.assertEqual(progress.estimate(10, 40), {"percent": 25, "eta_s": 30.0, "overdue": False})
        self.assertEqual(progress.estimate(39.5, 40)["eta_s"], 1.0)  # never "0 s left" while it still runs

    def test_never_claims_done_and_says_when_it_takes_longer(self):
        self.assertEqual(progress.estimate(40, 40)["percent"], progress.MAX_RUNNING_PERCENT)
        self.assertEqual(progress.estimate(60, 40), {"percent": progress.MAX_RUNNING_PERCENT, "eta_s": None,
                                                     "overdue": True})


class MemoryTests(LowRAMTestCase):
    def test_typical_is_the_median_of_the_last_runs(self):
        self.assertIsNone(progress.typical("model_load", "m"))
        for seconds in (30, 10, 20):
            progress.remember("model_load", "m", seconds)
        self.assertEqual(progress.typical("model_load", "m"), 20.0)
        self.assertIsNone(progress.typical("model_load", "other"))
        for _ in range(progress.KEEP + 5):
            progress.remember("model_load", "m", 50)
        self.assertEqual(progress.typical("model_load", "m"), 50.0)  # only the last KEEP values count
        stored = json.loads((config.home_dir() / progress.FILE_NAME).read_text())
        self.assertEqual(len(stored["model_load:m"]), progress.KEEP)

    def test_nonsense_is_ignored_and_a_broken_file_is_survived(self):
        for seconds in (0, -1, float("nan"), True, "12", progress.MAX_SECONDS + 1):
            progress.remember("plan", "x", seconds)
        self.assertIsNone(progress.typical("plan", "x"))
        (config.home_dir() / progress.FILE_NAME).write_text("{broken")
        self.assertIsNone(progress.typical("plan", "x"))
        progress.remember("plan", "x", 12)
        self.assertEqual(progress.typical("plan", "x"), 12.0)

    def test_the_file_stays_small(self):
        for i in range(progress.MAX_KEYS + 20):
            progress.remember("k", str(i), 1)
        stored = json.loads((config.home_dir() / progress.FILE_NAME).read_text())
        self.assertEqual(len(stored), progress.MAX_KEYS)
        self.assertIn(f"k:{progress.MAX_KEYS + 19}", stored)  # the newest survive


class CombiningEstimateTests(unittest.TestCase):
    """A big job is "komplett fertig" only after its results are combined: the loop counts that in."""

    def state(self, **extra):
        return {"active": True, "project_id": 7, "eta_s": 120, "per_task_s": 60.0, "planned": 2,
                "progress_percent": 50, **extra}

    def test_time_left_and_percent_include_combining(self):
        state = self.state()
        with mock.patch.object(cluster, "merge_expected_seconds", return_value=60.0) as expected:
            app.LoopController._include_merge(state)
        expected.assert_called_once_with(7)
        # half of the steps done = 50 % × 120 s / (120 s + 60 s) of everything
        self.assertEqual((state["merge_expected_s"], state["eta_s"], state["progress_percent"]), (60, 180, 33))

    def test_nothing_changes_without_combining(self):
        for merge in (None, 0.0, RuntimeError("db gone")):
            state = self.state()
            kwargs = {"side_effect": merge} if isinstance(merge, Exception) else {"return_value": merge}
            with mock.patch.object(cluster, "merge_expected_seconds", **kwargs):
                app.LoopController._include_merge(state)
            self.assertEqual(state, self.state())
        state = self.state(active=False)
        with mock.patch.object(cluster, "merge_expected_seconds", return_value=60.0) as expected:
            app.LoopController._include_merge(state)
        expected.assert_not_called()
        state = self.state(eta_s=None, per_task_s=None)  # no estimate yet: only the expectation is shown
        with mock.patch.object(cluster, "merge_expected_seconds", return_value=60.0):
            app.LoopController._include_merge(state)
        self.assertEqual((state["merge_expected_s"], state["eta_s"], state["progress_percent"]), (60, None, 50))


# --------------------------------------------------------------------------- reading the prompt (llama-server)

SSE_BODY = b"".join(b"data: " + line + b"\n\n" for line in (
    b'{"choices":[],"prompt_progress":{"total":1000,"cache":200,"processed":600,"time_ms":2000}}',
    b'{"prompt_progress":{"total":1000,"cache":200,"processed":1000,"time_ms":4000}}',  # no "choices" at all
    b'{"choices":[{"delta":{"content":"Hallo "}}]}',
    b'{"choices":[{"delta":{"content":"Welt"},"finish_reason":"stop"}]}',
    b"[DONE]",
))


class _SSE(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.server.bodies.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(SSE_BODY)))
        self.end_headers()
        self.wfile.write(SSE_BODY)


class PromptProgressTests(LowRAMTestCase):
    def test_parsing(self):
        self.assertEqual(llm.prompt_progress({"total": 1000, "cache": 200, "processed": 600, "time_ms": 2000}),
                         {"total": 1000, "processed": 600, "percent": 60, "eta_s": 2.0})
        self.assertEqual(llm.prompt_progress({"total": 10, "processed": 10})["percent"], 100)
        self.assertIsNone(llm.prompt_progress({"total": 10, "processed": 3})["eta_s"])  # no time yet
        for bad in (None, [], {"total": 0, "processed": 0}, {"total": "9", "processed": 1}, {"processed": 3}):
            self.assertIsNone(llm.prompt_progress(bad))

    def test_reading_progress_is_reported_and_never_breaks_the_answer(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SSE)
        server.bodies = []
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        seen = []
        text = "".join(llm.stream_chat([{"role": "user", "content": "hi"}], max_tokens=20, url=url,
                                       on_progress=seen.append))
        self.assertEqual(text, "Hallo Welt")
        self.assertEqual(seen, [{"total": 1000, "processed": 600, "percent": 60, "eta_s": 2.0},
                                {"total": 1000, "processed": 1000, "percent": 100, "eta_s": None}])
        self.assertTrue(server.bodies[0]["return_progress"])
        # without a listener nothing is asked for – and progress chunks of a server are still skipped
        text = "".join(llm.stream_chat([{"role": "user", "content": "hi"}], max_tokens=20, url=url))
        self.assertEqual(text, "Hallo Welt")
        self.assertNotIn("return_progress", server.bodies[1])

        def broken(info):
            raise RuntimeError("display failed")

        text = "".join(llm.stream_chat([{"role": "user", "content": "hi"}], max_tokens=20, url=url,
                                       on_progress=broken))
        self.assertEqual(text, "Hallo Welt")

    def test_loading_status_carries_percent_and_time_left(self):
        os.environ.pop("LOWRAM_LLM_URL", None)
        load = {"percent": 40, "eta_s": 12.0, "overdue": False, "measured": True, "elapsed_s": 8.0}
        fake = types.SimpleNamespace(load_progress=lambda: load)
        with mock.patch.object(chat, "_engine_module", return_value=fake):
            self.assertEqual(chat._loading_event(), {"type": "status", "text": chat.LOADING_TEXT, "phase": "load",
                                                     "percent": 40, "eta_s": 12.0, "overdue": False,
                                                     "measured": True})
        with mock.patch.object(chat, "_engine_module", return_value=types.SimpleNamespace()):
            self.assertEqual(chat._loading_event(), {"type": "status", "text": chat.LOADING_TEXT, "phase": "load"})


if __name__ == "__main__":
    unittest.main()
