"""Resilience on weak hardware: token timeouts, checkpoints + resume, overflow retry, RAM guard, pressure."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend import api_core, app, db, llm, memory, runner, settings
from tests.helpers import LowRAMTestCase, closed_port_url, fake_vm, wait_until

MSGS = [{"role": "user", "content": "hi"}]
OVERFLOW = ('{"error":{"code":400,"message":"the request exceeds the available context size, try increasing it",'
            '"type":"exceed_context_size_error"}}')
ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- fake streaming server


class Stream:
    """Scripted SSE answer. ``pieces``: str = content, ("think", str) = reasoning.

    ``first_delay``/``delay``: seconds before the first / each later piece; ``hold``: an event the
    server waits for after the pieces; ``stall``: seconds of silence after them; ``error``: an
    ``error:`` event; ``end=False``: the connection is closed mid-answer (engine crash).
    """

    def __init__(self, pieces, *, first_delay=0.0, delay=0.0, hold=None, stall=0.0, error=None, end=True):
        self.pieces, self.first_delay, self.delay = list(pieces), first_delay, delay
        self.hold, self.stall, self.error, self.end = hold, stall, error, end


class Fail:
    def __init__(self, status, body):
        self.status, self.body = status, body


class Plain:
    def __init__(self, text):
        self.text = text


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - health check
        fake = self.server.fake
        with fake.lock:
            fake.health_checks += 1
        self._send(fake.health_status, b'{"status":"ok"}')

    def do_POST(self):  # noqa: N802
        fake = self.server.fake
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        with fake.lock:
            fake.requests.append(payload)
            item = fake.script.pop(0) if fake.script else Stream(["default answer"])
        try:
            if isinstance(item, Fail):
                self._send(item.status, item.body.encode())
            elif isinstance(item, Plain):
                self._send(200, json.dumps({"choices": [{"message": {"content": item.text}}]}).encode())
            else:
                self._stream(item, fake.release)
        except OSError:  # the client gave up (timeout tests)
            pass

    def _event(self, data, field=b"data"):
        self.wfile.write(field + b": " + json.dumps(data).encode() + b"\n\n")

    def _stream(self, item, release):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.close_connection = True
        for index, piece in enumerate(item.pieces):
            pause = item.first_delay if index == 0 else item.delay
            if pause:
                release.wait(pause)
            delta = {"reasoning_content": piece[1]} if isinstance(piece, tuple) else {"content": piece}
            self._event({"choices": [{"index": 0, "delta": delta}]})
        if item.hold is not None:
            item.hold.wait(10)
        if item.stall:
            release.wait(item.stall)
        if item.error is not None:
            self._event(item.error, b"error")
            return
        if item.end:
            self._event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")


class StreamLLM:
    """OpenAI-compatible streaming endpoint on 127.0.0.1 with scripted answers and a /health route."""

    def __init__(self):
        self.script, self.requests, self.health_checks, self.health_status = [], [], 0, 200
        self.lock, self.release = threading.Lock(), threading.Event()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.fake = self
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"

    def push(self, *items):
        with self.lock:
            self.script.extend(items)

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class StreamCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.fake = StreamLLM()
        self.addCleanup(self.fake.close)
        env = mock.patch.dict(os.environ, {"LOWRAM_LLM_URL": self.fake.url})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: llm._plain_only.discard(llm.redact_url(self.fake.url)))
        for name, value in (("CHECKPOINT_INTERVAL_S", 0.0), ("ENGINE_POLL_S", 0.05)):
            patcher = mock.patch.object(runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def timeouts(self, first, idle):
        settings.update("resilience", {"first_token_timeout_s": first, "idle_token_timeout_s": idle})


# --------------------------------------------------------------------------- llm.chat


class TokenTimeoutTests(StreamCase):
    def test_slow_first_token_is_fine(self):
        self.timeouts(5, 0.5)
        self.fake.push(Stream(["Hallo ", "Welt"], first_delay=1.3, delay=0.1))
        seen = []
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_TIMEOUT": "1"}):  # the old fixed timeout gave up after 1 s
            self.assertEqual(llm.chat(MSGS, on_text=seen.append), "Hallo Welt")
        self.assertIs(self.fake.requests[0]["stream"], True)
        self.assertEqual(seen, ["Hallo ", "Hallo Welt"])

    def test_stall_after_first_token_is_transient_and_keeps_partial(self):
        self.timeouts(5, 0.4)
        self.fake.push(Stream(["Teil eins "], stall=8, end=False))
        started = time.monotonic()
        with self.assertRaisesRegex(llm.LLMError, r"sent nothing for 0\.4 s") as ctx:
            llm.chat(MSGS)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(ctx.exception.transient)
        self.assertEqual(ctx.exception.partial, "Teil eins ")

    def test_no_first_token_within_first_token_timeout(self):
        self.timeouts(0.4, 5)
        self.fake.push(Stream(["zu spät"], first_delay=8))
        with self.assertRaisesRegex(llm.LLMError, "still no first token") as ctx:
            llm.chat(MSGS)
        self.assertTrue(ctx.exception.transient)
        self.assertEqual(ctx.exception.partial, "")

    def test_first_token_timeout_is_capped_at_one_hour(self):
        self.timeouts(99999, 300)
        self.addCleanup(setattr, llm, "_first_token_clamp_logged", False)
        llm._first_token_clamp_logged = False
        with self.assertLogs("lowram.llm", "WARNING") as logs:
            self.assertEqual(llm.token_timeouts(), (3600.0, 300.0))
            self.assertEqual(llm.token_timeouts()[0], 3600.0)
        self.assertEqual(len(logs.output), 1)  # logged once

    def test_total_request_cap_while_tokens_flow(self):
        self.timeouts(5, 5)
        self.fake.push(Stream(["a "] * 80, delay=0.05))
        started = time.monotonic()
        with mock.patch.object(llm, "MAX_REQUEST_S", 0.6):
            with self.assertRaisesRegex(llm.LLMError, "Zeitlimit von 3 Stunden") as ctx:
                llm.chat(MSGS)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(ctx.exception.transient)
        self.assertTrue(ctx.exception.partial.startswith("a a"))

    def test_total_request_cap_before_first_token(self):
        self.timeouts(5, 5)
        self.fake.push(Stream(["zu spät"], first_delay=8))
        started = time.monotonic()
        with mock.patch.object(llm, "MAX_REQUEST_S", 0.5):
            with self.assertRaisesRegex(llm.LLMError, "Zeitlimit") as ctx:
                llm.chat(MSGS)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(ctx.exception.transient)

    def test_limit_is_reported_when_the_read_times_out_before_the_watchdog(self):
        """macOS: the socket timeout can beat the watchdog thread – the elapsed time decides, not the order."""
        self.timeouts(5, 5)
        self.fake.push(Stream(["zu spät"], first_delay=8))
        with mock.patch.object(llm, "MAX_REQUEST_S", 0.5), mock.patch.object(llm, "_start_watchdog") as watchdog:
            with self.assertRaisesRegex(llm.LLMError, "Zeitlimit") as ctx:
                llm.chat(MSGS)
        self.assertTrue(watchdog.called)  # it never fired: only the read timeout ended the request
        self.assertFalse(ctx.exception.transient)

    def test_past_limit(self):
        with mock.patch.object(llm, "MAX_REQUEST_S", 5.0):
            self.assertTrue(llm._past_limit({"over_limit": True}))
            self.assertTrue(llm._past_limit({"started": time.monotonic() - 6}))
            self.assertFalse(llm._past_limit({"started": time.monotonic()}))
            self.assertFalse(llm._past_limit({}))

    def test_connection_drop_is_transient(self):
        self.fake.push(Stream(["abc "], end=False))
        with self.assertRaisesRegex(llm.LLMError, "closed the connection") as ctx:
            llm.chat(MSGS)
        self.assertTrue(ctx.exception.transient)
        self.assertEqual(ctx.exception.partial, "abc ")

    def test_error_events_are_classified(self):
        self.fake.push(
            Stream(["x"], error=json.loads(OVERFLOW)["error"]),
            Stream([], error={"code": 503, "message": "Loading model"}),
        )
        with self.assertRaises(llm.LLMError) as ctx:
            llm.chat(MSGS)
        self.assertEqual((ctx.exception.context_overflow, ctx.exception.transient), (True, False))
        with self.assertRaises(llm.LLMError) as ctx:
            llm.chat(MSGS)
        self.assertEqual((ctx.exception.context_overflow, ctx.exception.transient), (False, True))

    def test_reasoning_is_stripped(self):
        self.fake.push(Stream([("think", "hmm, "), ("think", "überlegen"), "Antwort"]))
        seen = []
        self.assertEqual(llm.chat(MSGS, on_text=seen.append), "Antwort")
        self.assertFalse(any("hmm" in text for text in seen))

    def test_server_refusing_stream_falls_back_to_plain_requests(self):
        self.fake.push(Fail(400, '{"error":"stream mode is not supported"}'), Plain("plain answer"), Plain("again"))
        self.assertEqual(llm.chat(MSGS), "plain answer")
        self.assertEqual(llm.chat(MSGS), "again")  # remembered: no second streaming attempt
        self.assertEqual([r["stream"] for r in self.fake.requests], [True, False, False])

    def test_engine_is_touched(self):
        touch = mock.Mock()
        with mock.patch.dict(sys.modules, {"backend.engine": types.SimpleNamespace(touch=touch)}):
            llm.chat(MSGS)
        self.assertTrue(touch.called)

    def test_endpoint_ready(self):
        self.assertTrue(llm.endpoint_ready(self.fake.url))
        self.fake.health_status = 503  # llama-server while it loads the model
        self.assertFalse(llm.endpoint_ready(self.fake.url))
        self.assertFalse(llm.endpoint_ready(closed_port_url(), timeout=2))
        self.assertFalse(llm.endpoint_ready("ftp://x/y"))


# --------------------------------------------------------------------------- runner


class CheckpointResumeTests(StreamCase):
    def meta(self, outcome):
        return json.loads(Path(outcome["task"]["result_path"]).with_suffix(".json").read_text())

    def test_drop_mid_stream_resumes_from_checkpoint(self):
        pid = runner.create_project("p", "Schreibe etwas")
        partial = runner.partial_path(pid, db.list_tasks(pid)[0]["id"])
        hold = threading.Event()
        self.fake.push(Stream(["Erster ", "Teil."], hold=hold, end=False), Stream(["Zweiter Teil."]))
        waits, result = [], {}
        worker = threading.Thread(target=lambda: result.update(out=runner.run_next(pid, on_wait=waits.append)))
        worker.start()
        self.assertTrue(wait_until(lambda: partial.exists() and partial.read_text() == "Erster Teil."))
        hold.set()
        worker.join(15)
        out = result["out"]
        self.assertEqual(out["status"], "done", out["error"])
        self.assertEqual(out["result"], "Erster Teil. Zweiter Teil.")
        self.assertEqual(Path(out["task"]["result_path"]).read_text(), "Erster Teil. Zweiter Teil.\n")
        self.assertFalse(partial.exists())
        resume = self.fake.requests[1]["messages"]
        self.assertEqual(resume[:-2], self.fake.requests[0]["messages"])
        self.assertEqual(resume[-2], {"role": "assistant", "content": "Erster Teil."})
        self.assertIn("ohne Wiederholung", resume[-1]["content"])
        meta = self.meta(out)
        self.assertEqual((meta["attempts"], meta["resumed_from_checkpoint"], meta["transient_retries"]), (2, True, 1))
        self.assertEqual(waits, [runner.WAITING_FOR_MODEL])
        self.assertGreaterEqual(self.fake.health_checks, 1)

    def test_engine_restarted_on_a_new_port(self):
        """Old engine behaviour: right after the crash ensure_running still reports the dead port as ready."""
        pid = runner.create_project("p", "Schreibe etwas")
        partial = runner.partial_path(pid, db.list_tasks(pid)[0]["id"])
        hold = threading.Event()
        self.fake.push(Stream(["Erster ", "Teil."], hold=hold, end=False))
        restarted = StreamLLM()
        self.addCleanup(restarted.close)
        restarted.push(Stream(["Zweiter Teil."]))
        old_url, calls, result = self.fake.url, [], {}

        def ensure_running(timeout):
            calls.append(timeout)
            return old_url if len(calls) <= 2 else restarted.url

        engine = types.SimpleNamespace(ensure_running=ensure_running)
        with mock.patch.object(runner, "_managed_engine", return_value=engine), \
                mock.patch.object(runner, "ENGINE_READY_POLL_S", 0.05):
            worker = threading.Thread(target=lambda: result.update(out=runner.run_next(pid)))
            worker.start()
            self.assertTrue(wait_until(lambda: partial.exists() and partial.read_text() == "Erster Teil."))
            self.fake.close()  # the engine crashes: its port is gone …
            hold.set()  # … and the running answer breaks off
            worker.join(15)
        out = result["out"]
        self.assertEqual(out["status"], "done", out["error"])
        self.assertEqual(out["result"], "Erster Teil. Zweiter Teil.")
        meta = self.meta(out)
        self.assertEqual((meta["resumed_from_checkpoint"], meta["attempts"]), (True, 2))
        self.assertEqual(meta["endpoint"], restarted.url)
        self.assertGreaterEqual(len(calls), 3)  # the stale "ready" URL was not trusted
        self.assertEqual(len(restarted.requests), 1)
        self.assertEqual(restarted.requests[0]["messages"][-2], {"role": "assistant", "content": "Erster Teil."})
        self.assertFalse(partial.exists())

    def test_failed_resume_keeps_partial_and_says_so(self):
        pid = runner.create_project("p", "t")
        self.fake.push(Stream(["Anfang "], end=False), Stream(["mehr "], end=False))
        out = runner.run_next(pid)
        self.assertEqual(out["status"], "failed")
        task = db.list_tasks(pid)[0]
        partial = runner.partial_path(pid, task["id"])
        self.assertEqual(partial.read_text(), "Anfang mehr ")
        self.assertIn("partial answer", out["error"])
        self.assertIn(partial.name, task["error"])
        self.assertEqual(len(self.fake.requests), 2)  # resumed only once

    def test_request_cap_fails_task_without_retry_and_frees_the_slot(self):
        pid = runner.create_project("p", "t")
        self.fake.push(Stream(["a "] * 80, delay=0.05))
        with mock.patch.object(llm, "MAX_REQUEST_S", 0.6):
            out = runner.run_next(pid)
        self.assertEqual(out["status"], "failed")
        self.assertIn(llm.REQUEST_LIMIT_MESSAGE, out["error"])
        self.assertIn("partial answer", out["error"])
        self.assertEqual(len(self.fake.requests), 1)  # not transient: no resume
        self.assertIsNone(db.busy_reason())

    def test_checkpoint_every_15_s_and_on_error(self):
        settings.update("resilience", {"retry_transient": False})
        pid = runner.create_project("p", "t")
        partial = runner.partial_path(pid, db.list_tasks(pid)[0]["id"])
        hold, result = threading.Event(), {}
        self.fake.push(Stream(["Anfang ", "mehr"], hold=hold, end=False))
        with mock.patch.object(runner, "CHECKPOINT_INTERVAL_S", 15.0):
            worker = threading.Thread(target=lambda: result.update(out=runner.run_next(pid)))
            worker.start()
            self.assertTrue(wait_until(lambda: len(self.fake.requests) == 1))
            time.sleep(0.3)
            self.assertFalse(partial.exists())  # no write before 15 s
            hold.set()
            worker.join(15)
        self.assertEqual(result["out"]["status"], "failed")
        self.assertEqual(partial.read_text(), "Anfang mehr")  # final checkpoint on error

    def test_no_retry_when_disabled(self):
        settings.update("resilience", {"retry_transient": False})
        pid = runner.create_project("p", "t")
        self.fake.push(Stream(["Nur "], end=False))
        out = runner.run_next(pid)
        self.assertEqual(out["status"], "failed")
        self.assertIn("closed the connection", out["error"])
        self.assertIn("partial answer", out["error"])
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.fake.health_checks, 0)

    def test_transient_error_before_first_token_retries_plainly(self):
        pid = runner.create_project("p", "t")
        self.fake.push(Fail(503, '{"error":{"code":503,"message":"Loading model"}}'), Stream(["fertig"]))
        out = runner.run_next(pid)
        self.assertEqual(out["status"], "done", out["error"])
        self.assertEqual(out["result"], "fertig")
        self.assertEqual(self.fake.requests[1]["messages"], self.fake.requests[0]["messages"])
        meta = self.meta(out)
        self.assertEqual((meta["attempts"], meta["resumed_from_checkpoint"], meta["transient_retries"]), (2, False, 1))

    def test_model_not_back_in_time_fails_the_task(self):
        self.fake.health_status = 503
        pid = runner.create_project("p", "t")
        self.fake.push(Fail(503, "Loading model"))
        with mock.patch.object(runner, "ENGINE_WAIT_S", 0.3):
            out = runner.run_next(pid)
        self.assertEqual(out["status"], "failed")
        self.assertIn("not ready again", out["error"])
        self.assertEqual(len(self.fake.requests), 1)

    def test_wait_for_model_uses_the_managed_engine(self):
        engine = types.SimpleNamespace(
            ensure_running=mock.Mock(side_effect=[RuntimeError("loading"), closed_port_url(), self.fake.url])
        )
        with mock.patch.object(runner, "_managed_engine", return_value=engine), \
                mock.patch.object(runner, "ENGINE_READY_POLL_S", 0.05):
            self.assertEqual(runner.await_model(timeout=5), (self.fake.url, None))
        self.assertEqual(engine.ensure_running.call_count, 3)  # a URL that does not answer is not trusted
        self.assertLessEqual(engine.ensure_running.call_args.kwargs["timeout"], runner.ENGINE_STEP_S)

    def test_wait_for_model_stops_promptly(self):
        self.fake.health_status = 503
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        self.assertIn("stopped", runner.wait_for_model(timeout=30, stop_event=stop))
        self.assertLess(time.monotonic() - started, 3)

    def test_context_overflow_retries_with_half_the_context(self):
        self.set_memory(total_gib=8, available_mib=4096)
        budget = memory.plan(40).context_chars
        pid = runner.create_project("p", "erste\nzweite")
        self.fake.push(Stream(["X" * 30000]), Fail(400, OVERFLOW), Stream(["kurz"]))
        runner.run_next(pid)
        out = runner.run_next(pid)
        self.assertEqual(out["status"], "done", out["error"])
        first = self.fake.requests[1]["messages"][-1]["content"].count("X")
        second = self.fake.requests[2]["messages"][-1]["content"].count("X")
        self.assertGreater(first, budget // 2)
        self.assertLessEqual(second, budget // 2)
        meta = self.meta(out)
        self.assertEqual((meta["attempts"], meta["context_retry"]), (2, True))
        self.assertLessEqual(meta["context_chars"], budget // 2)

    def test_call_with_overflow_retry(self):
        budgets = []

        def build(budget):
            budgets.append(budget)
            return [{"role": "user", "content": "c" * budget}]

        self.fake.push(Fail(400, OVERFLOW), Stream(["ok"]))
        info = {}
        self.assertEqual(runner.call_with_overflow_retry(build, 1000, info=info, max_tokens=50), "ok")
        self.assertEqual(budgets, [1000, 500])
        self.assertEqual(info, {"attempts": 2, "context_retry": True, "context_chars": 500})
        self.fake.push(Fail(400, OVERFLOW), Fail(400, OVERFLOW))
        with self.assertRaises(llm.LLMError) as ctx:
            runner.call_with_overflow_retry(build, 1000, max_tokens=50)
        self.assertTrue(ctx.exception.context_overflow)
        budgets.clear()
        self.fake.push(Fail(500, "boom"))
        with self.assertRaises(llm.LLMError):
            runner.call_with_overflow_retry(build, 1000, max_tokens=50)
        self.assertEqual(budgets, [1000])  # other errors are not retried

    def test_join_continuation(self):
        self.assertEqual(runner.join_continuation("Hallo ", "Welt"), "Hallo Welt")
        self.assertEqual(runner.join_continuation("Satz.", "Weiter"), "Satz. Weiter")
        self.assertEqual(runner.join_continuation("Wo", "rt"), "Wort")
        self.assertEqual(runner.join_continuation("", "nur"), "nur")


# --------------------------------------------------------------------------- RAM guard


class RamGuardTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.ram_mib = 8192
        for target, kwargs in (
            ("backend.memory.psutil.virtual_memory", {"side_effect": lambda: fake_vm(16, self.ram_mib)}),
            ("backend.memory.GUARD_INTERVAL_S", {"new": 0.02}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def recover_after(self, seconds):
        timer = threading.Timer(seconds, lambda: setattr(self, "ram_mib", 8192))
        timer.daemon = True
        timer.start()

    def test_guard_wait_returns_when_ram_recovers(self):
        self.ram_mib = 100
        messages = []
        self.recover_after(0.2)
        self.assertTrue(memory.guard_wait("test", 10, None, messages.append))
        self.assertTrue(messages)
        self.assertEqual(messages[0], "Warte auf freien Arbeitsspeicher (100 MB frei)…")

    def test_guard_wait_times_out_and_stops(self):
        self.assertTrue(memory.guard_wait("test", 0))
        self.ram_mib = 100
        started = time.monotonic()
        self.assertFalse(memory.guard_wait("test", 0.2))
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        self.assertFalse(memory.guard_wait("test", 60, stop))
        self.assertLess(time.monotonic() - started, 3)

    def test_loop_waits_between_tasks_then_continues(self):
        pid = runner.create_project("p", "a\nb")
        seen = []

        def on_step(outcome):
            seen.append((outcome["status"], outcome["reason"]))
            if outcome["status"] == "done" and len(seen) == 1:
                self.ram_mib = 100
                self.recover_after(0.2)

        summary = runner.run_loop(pid, on_step=on_step)
        self.assertEqual((summary["stopped_because"], summary["completed"]), ("queue_empty", 2))
        waiting = [reason for status, reason in seen if status == "waiting"]
        self.assertTrue(waiting)
        self.assertIn("Warte auf freien Arbeitsspeicher", waiting[0])

    def test_loop_stops_after_waiting_too_long(self):
        settings.update("resilience", {"wait_for_ram_minutes": 0.005})
        pid = runner.create_project("p", "a\nb")

        def on_step(outcome):
            if outcome["status"] == "done":
                self.ram_mib = 100

        summary = runner.run_loop(pid, on_step=on_step)
        self.assertEqual((summary["stopped_because"], summary["completed"]), ("paused_low_ram", 1))
        self.assertIn("Waited", summary["message"])
        self.assertEqual(db.list_tasks(pid)[1]["status"], "pending")

    def test_stop_during_wait_is_prompt(self):
        pid = runner.create_project("p", "a\nb")
        stop = threading.Event()

        def on_step(outcome):
            if outcome["status"] == "done":
                self.ram_mib = 100
            elif outcome["status"] == "waiting":
                stop.set()

        started = time.monotonic()
        summary = runner.run_loop(pid, stop_event=stop, on_step=on_step)
        self.assertEqual(summary["stopped_because"], "stop_requested")
        self.assertLess(time.monotonic() - started, 5)

    def test_loop_controller_shows_the_wait(self):
        loop = app.LoopController()
        loop._on_step({"status": "waiting", "task": None, "reason": "Warte auf freien Arbeitsspeicher (100 MB frei)…"})
        state = loop.status()
        self.assertEqual(state["last_message"], "Warte auf freien Arbeitsspeicher (100 MB frei)…")
        self.assertEqual((state["completed"], state["failed"]), (0, 0))


# --------------------------------------------------------------------------- pressure


class PressureTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        memory.stop_pressure_sampler()
        memory._swap_history.clear()
        memory._mac_cache = None
        self.addCleanup(memory.stop_pressure_sampler)
        self.addCleanup(memory._swap_history.clear)
        self.addCleanup(setattr, memory, "_mac_cache", None)
        for target, kwargs in (
            ("backend.memory._is_macos", {"return_value": False}),
            ("backend.memory.psutil.swap_memory", {"return_value": mock.Mock(used=0)}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_levels(self):
        sample = memory.sample_pressure()
        self.assertEqual(set(sample), {"level", "available_bytes", "swap_used_bytes", "swap_growth_mb_per_min",
                                       "source"})
        self.assertEqual(sample["level"], "ok")
        self.set_memory(16, 400)  # minimum 256 MiB in tests: less than twice that is a warning
        self.assertEqual(memory.sample_pressure()["level"], "warn")
        self.set_memory(16, 100)
        self.assertEqual(memory.sample_pressure()["level"], "critical")
        self.set_memory()
        for growth, level in ((100.0, "warn"), (300.0, "critical")):
            with mock.patch.object(memory, "_swap_growth", return_value=growth):
                self.assertEqual(memory.sample_pressure()["level"], level)

    def test_swap_growth(self):
        mib = 1024 * 1024
        self.assertEqual(memory._swap_growth(100.0, 0), 0.0)
        self.assertEqual(memory._swap_growth(130.0, 300 * mib), 600.0)  # 300 MB in half a minute
        self.assertEqual(memory._swap_growth(1000.0, 300 * mib), 0.0)  # old samples age out

    def test_macos_pressure_level(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="4\n", stderr=""))
        with mock.patch.object(memory, "_is_macos", return_value=True), mock.patch.object(memory.subprocess, "run", run):
            self.assertEqual(memory.mac_pressure_level(), "critical")
            self.assertEqual(memory.sample_pressure()["source"], "macos")  # cached for 3 s
            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual(args[0][1:], ["-n", "kern.memorystatus_vm_pressure_level"])
            self.assertEqual(kwargs["timeout"], 2)
            self.assertNotIn("shell", kwargs)
            memory._mac_cache = None
            run.return_value = subprocess.CompletedProcess([], 0, stdout="1\n", stderr="")
            self.assertEqual(memory.sample_pressure()["level"], "ok")
            self.set_memory(16, 100)  # below the task minimum stays critical
            self.assertEqual(memory.sample_pressure()["level"], "critical")
        self.assertIsNone(memory.mac_pressure_level())  # not macOS

    def test_sampler_is_idempotent_and_stoppable(self):
        first = memory.start_pressure_sampler(interval=0.05)
        self.assertIs(memory.start_pressure_sampler(), first)
        self.assertTrue(wait_until(lambda: memory._latest is not None))
        self.assertIn(memory.pressure()["level"], memory.LEVELS)
        memory.stop_pressure_sampler()
        self.assertFalse(first.is_alive())
        self.assertFalse(memory.sampler_running())
        memory.stop_pressure_sampler()  # no-op

    def test_sampler_cadence_follows_activity(self):
        with mock.patch("backend.db.busy_reason", return_value="chat #1"):
            self.assertTrue(memory._something_active())
        with mock.patch("backend.db.busy_reason", return_value=None):
            self.assertFalse(memory._something_active())
        for active, expected in ((False, 0.5), (True, 0.05)):
            with mock.patch.object(memory, "_something_active", return_value=active):
                memory.start_pressure_sampler(interval=0.05, idle_interval=0.5)
                self.assertTrue(wait_until(lambda: memory._cadence == expected))
                memory.stop_pressure_sampler()
        self.assertEqual(memory._cadence, memory.PRESSURE_INTERVAL_S)  # reset when stopped
        self.assertEqual((memory.PRESSURE_INTERVAL_S, memory.PRESSURE_IDLE_INTERVAL_S), (3.0, 20.0))
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="1\n", stderr=""))
        with mock.patch.object(memory, "_is_macos", return_value=True), \
                mock.patch.object(memory.subprocess, "run", run), mock.patch.object(memory, "_cadence", 20.0):
            memory._mac_cache = (time.monotonic() - 5, "warn")
            self.assertEqual(memory.mac_pressure_level(), "warn")  # idle: sysctl only every 20 s
            run.assert_not_called()
            memory._cadence = 3.0
            self.assertEqual(memory.mac_pressure_level(), "ok")
            run.assert_called_once()

    def test_import_starts_no_thread(self):
        code = "import threading, backend.app; print(sorted(t.name for t in threading.enumerate()))"
        with tempfile.TemporaryDirectory() as home:
            out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                                 env={**os.environ, "LOWRAM_HOME": home})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("lowram-memory-pressure", out.stdout)

    def test_memory_endpoint_includes_pressure(self):
        req = mock.Mock()
        req.arg.return_value = "40"
        data = api_core.get_memory(req).data
        self.assertEqual(data["percent"], 40)
        self.assertIn(data["pressure"]["level"], memory.LEVELS)


if __name__ == "__main__":
    unittest.main()
