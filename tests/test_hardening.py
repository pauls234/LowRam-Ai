"""Regression tests for the hardening round (review fixes)."""

import builtins
import json
import os
import re
import sqlite3
import subprocess
import sys
import socket
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend import api_core, agent, db, llm, runner
from tests.helpers import (
    EchoAuthError, Gate, HttpError, LowRAMTestCase, Redirect, Text, closed_port_url, wait_until,
)
from tests.test_agent import BUGGY, FIXED, proposal_reply
from tests.test_app import AppTestCase
from tests.test_cli import run

MSGS = [{"role": "user", "content": "hi"}]


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# --------------------------------------------------------------------------- db


class OwnerTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.p = db.create_project("A", "general", tasks=[("t1", "")])
        self.host = socket.gethostname()

    def claim(self, owner):
        return db.claim_next_task(self.p, owner=owner)

    def test_owner_id_format(self):
        host, pid, started = db.owner_id().rsplit(":", 2)
        self.assertEqual(host, self.host)
        self.assertEqual(int(pid), os.getpid())
        self.assertRegex(started, r"^\d+\.\d\d$")
        self.assertTrue(db._owner_alive(db.owner_id()))

    def test_claim_uses_three_part_owner(self):
        task = self.claim(None)
        self.assertEqual(task["lease_owner"].count(":"), 2)
        self.assertEqual(db.recover_stale(), [])

    def test_pid_reuse_is_stale(self):
        task = self.claim(f"{self.host}:{os.getpid()}:1.00")  # live pid, wrong start time
        self.assertEqual(db.recover_stale(), [task["id"]])

    def test_different_hostname_dead_pid_is_recovered(self):
        task = self.claim(f"renamed-host.local:{dead_pid()}:1.00")
        self.assertEqual(db.recover_stale(), [task["id"]])

    def test_different_hostname_live_pid_with_matching_start_is_alive(self):
        _, pid, started = db.owner_id().rsplit(":", 2)
        task = self.claim(f"other-host:{pid}:{started}")
        self.assertEqual(db.recover_stale(), [])
        self.assertEqual(db.get_task(task["id"])["status"], "running")

    def test_legacy_two_part_owner(self):
        self.assertTrue(db._owner_alive(f"{self.host}:{os.getpid()}"))
        self.assertFalse(db._owner_alive(f"{self.host}:{dead_pid()}"))
        task = self.claim(f"{self.host}:{dead_pid()}")
        self.assertEqual(db.recover_stale(), [task["id"]])

    def test_garbage_owners_are_dead(self):
        for owner in (None, "", "nocolon", "h:abc", "h:abc:1.0"):
            with self.subTest(owner=owner):
                self.assertFalse(db._owner_alive(owner))
        self.assertFalse(db._owner_alive(f"{self.host}:{os.getpid()}:notafloat"))

    def test_stale_lock_with_dead_foreign_host(self):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO locks (name, owner, label, acquired_at) VALUES (?, ?, ?, ?)",
                (db.INFERENCE_LOCK, f"elsewhere:{dead_pid()}:1.00", "ghost", db.utcnow()),
            )
        self.assertIsNone(db.busy_reason())


class RetryFinishTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.p = db.create_project("A", "general", tasks=[("t1", "")])
        self.tid = db.list_tasks(self.p)[0]["id"]

    def test_retry_accepts_rejected(self):
        db.transition(self.tid, ["pending"], "rejected", error="no")
        task = db.retry_task(self.tid)
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["error"])

    def test_retry_still_refuses_other_states(self):
        db.claim_next_task(self.p)
        db.finish_task(self.tid, "done", result_path="/r")
        with self.assertRaises(db.InvalidTransition):
            db.retry_task(self.tid)

    def test_finish_retries_operational_error(self):
        db.claim_next_task(self.p)
        real = db._finish_task_once
        boom = sqlite3.OperationalError("database is locked")
        with mock.patch("backend.db.time.sleep") as sleep, mock.patch(
            "backend.db._finish_task_once", side_effect=[boom, boom, None]
        ) as once:
            once.side_effect = [boom, boom, real(self.tid, "done", "/r", None, None)]
            result = db.finish_task(self.tid, "done", result_path="/r")
        self.assertEqual(once.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["status"], "done")

    def test_finish_gives_up_after_three_attempts(self):
        db.claim_next_task(self.p)
        boom = sqlite3.OperationalError("database is locked")
        with mock.patch("backend.db.time.sleep"), mock.patch("backend.db._finish_task_once", side_effect=boom) as once:
            with self.assertRaises(sqlite3.OperationalError):
                db.finish_task(self.tid, "failed", error="x")
        self.assertEqual(once.call_count, 3)

    def test_finish_does_not_retry_invalid_transition(self):
        with mock.patch("backend.db.time.sleep") as sleep:
            with self.assertRaises(db.InvalidTransition):
                db.finish_task(self.tid, "done")  # pending, not running
        sleep.assert_not_called()


# --------------------------------------------------------------------------- llm


class LLMHardeningTests(LowRAMTestCase):
    def test_think_blocks_stripped(self):
        self.llm.push(Text("<think>secret reasoning</think>\nThe answer."))
        self.assertEqual(llm.chat(MSGS), "The answer.")

    def test_only_think_block_is_empty_answer(self):
        self.llm.push(Text("<think>just thinking...</think>"), Text("<think>a</think>  <THINK>b</THINK>"))
        for _ in range(2):
            with self.assertRaisesRegex(llm.LLMError, "empty answer"):
                llm.chat(MSGS)

    def test_redirects_not_followed(self):
        target = closed_port_url()
        for code in (301, 302, 307):
            self.llm.push(Redirect(target, code))
            with self.assertRaisesRegex(llm.LLMError, "redirect"):
                llm.chat(MSGS)
        self.assertEqual(len(self.llm.requests), 3)

    def test_redirect_target_never_contacted(self):
        from tests.helpers import FakeLLM

        other = FakeLLM()
        self.addCleanup(other.close)
        self.llm.push(Redirect(other.url))
        with self.assertRaises(llm.LLMError):
            llm.chat(MSGS)
        self.assertEqual(other.requests, [])

    def test_redact_url(self):
        self.assertEqual(llm.redact_url("http://user:pw@host.example:8080/v1/x?key=SECRET#frag"), "http://host.example:8080/v1/x")
        self.assertEqual(llm.redact_url("https://host.example/a?b=1"), "https://host.example/a")
        self.assertEqual(llm.redact_url("http://[::1]:9/x"), "http://[::1]:9/x")
        self.assertEqual(llm.redact_url("http://127.0.0.1:8080/v1/chat/completions"), "http://127.0.0.1:8080/v1/chat/completions")

    def test_unreachable_message_hides_credentials(self):
        url = closed_port_url().replace("http://", "http://user:hunter2@") + "?token=abc123"
        with self.assertRaises(llm.LLMError) as ctx:
            llm.chat(MSGS, url=url, timeout=3)
        text = str(ctx.exception)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc123", text)

    def test_bad_scheme_message_hides_credentials(self):
        with self.assertRaises(llm.LLMError) as ctx:
            llm.chat(MSGS, url="ftp://user:hunter2@host/x?k=abc123")
        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertNotIn("abc123", str(ctx.exception))

    def test_api_key_refused_over_plain_http_non_loopback(self):
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_API_KEY": "sekret"}), mock.patch(
            "backend.llm._opener", side_effect=AssertionError("must not open a connection")
        ):
            with self.assertRaisesRegex(llm.LLMError, "plain http"):
                llm.chat(MSGS, url="http://10.255.255.1/v1/chat/completions", timeout=1)

    def test_api_key_allowed_over_loopback_http(self):
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_API_KEY": "sekret"}):
            self.assertEqual(llm.chat(MSGS), "default answer")

    def test_no_key_plain_http_non_loopback_is_not_refused_early(self):
        with mock.patch("backend.llm._opener", side_effect=RuntimeError("opener reached")):
            with self.assertRaisesRegex(RuntimeError, "opener reached"):
                llm.chat(MSGS, url="http://10.255.255.1/v1/chat/completions", timeout=1)

    def test_api_key_scrubbed_from_error_body(self):
        self.llm.push(EchoAuthError())
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_API_KEY": "sk-very-secret"}):
            with self.assertRaises(llm.LLMError) as ctx:
                llm.chat(MSGS)
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertNotIn("sk-very-secret", str(ctx.exception))
        self.assertIn("***", str(ctx.exception))
        self.assertEqual(self.llm.requests[0]["headers"]["Authorization"], "Bearer sk-very-secret")


# --------------------------------------------------------------------------- agent


class ChangeValidationTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo({"a.py": "x = 1\n"})

    def validate(self, path, content):
        return agent.validate_changes(self.repo, [{"path": path, "content": content}])

    def test_invisible_characters_rejected(self):
        for name, ch in {
            "escape": "\x1b[2K", "rlo": "‮", "zwsp": "​", "formfeed": "\x0c",
            "line sep": " ", "para sep": " ", "bell": "\x07", "lone CR": "a\rb", "bom": "﻿",
        }.items():
            with self.subTest(name=name):
                changes, errors = self.validate("b.py", f"y = 1  # {ch}\n")
                self.assertEqual(changes, [])
                self.assertTrue(any("invisible/control" in e for e in errors), errors)

    def test_tabs_newlines_and_unicode_text_allowed(self):
        changes, errors = self.validate("b.py", 'def f():\n\treturn "héllo ✓ 日本"\n')
        self.assertEqual(errors, [])
        self.assertEqual(len(changes), 1)

    def test_crlf_normalised_for_new_file(self):
        changes, errors = self.validate("b.py", "a = 1\r\nb = 2\r\n")
        self.assertEqual(errors, [])
        self.assertNotIn("\r", changes[0]["content"])
        self.assertEqual(changes[0]["newline"], "lf")

    def test_blocked_write_locations(self):
        for path in (
            ".github/workflows/ci.yml", ".github/dependabot.yml", ".pre-commit-config.yaml",
            "sub/.pre-commit-config.yml", "sitecustomize.py", "pkg/usercustomize.py", "SiteCustomize.py",
        ):
            with self.subTest(path=path):
                changes, errors = self.validate(path, "x = 1\n" if path.endswith(".py") else "a: 1\n")
                self.assertEqual(changes, [])
                self.assertTrue(errors)

    def test_blocked_paths_only_for_write(self):
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "ci.yml").write_text("on: push\n")
        agent.resolve_repo_path(self.repo, ".github/ci.yml")  # reading is fine
        with self.assertRaises(agent.ProposalError):
            agent.resolve_repo_path(self.repo, ".github/ci.yml", write=True)
        self.assertIn(".github/ci.yml", [e["path"] for e in agent.index_repository(self.repo)])

    def test_file_used_as_directory(self):
        (self.repo / "README").write_text("hi\n")
        with self.assertRaisesRegex(agent.ProposalError, "not a directory"):
            agent.resolve_repo_path(self.repo, "README/x.md")
        changes, errors = self.validate("README/x.md", "text\n")
        self.assertEqual(changes, [])
        self.assertTrue(errors)

    def test_needs_attention(self):
        for name in ("Makefile", "setup.py", "conftest.py", "package.json", "scripts/run.sh", "sub/Makefile", "pyproject.toml"):
            with self.subTest(name=name):
                self.assertTrue(agent.needs_attention(name))
        for name in ("a.py", "README.md", "src/main.js"):
            with self.subTest(name=name):
                self.assertFalse(agent.needs_attention(name))
        changes, _ = agent.validate_changes(
            self.repo, [{"path": "Makefile", "content": "all:\n"}, {"path": "ok.py", "content": "x = 1\n"}]
        )
        self.assertEqual({c["path"]: c["attention"] for c in changes}, {"Makefile": True, "ok.py": False})

    def test_clean_text_and_plan_titles(self):
        self.assertEqual(agent.clean_text("a\x1b[2Kb‮c​d\te\nf"), "a[2Kbcd\te\nf")
        raw = json.dumps({"steps": [{"title": "Fix\x1b[2K it‮ now", "files": ["a​.py"], "detail": "d\x07etail"}]})
        step = agent.parse_plan(raw)[0]
        self.assertEqual(step["title"], "Fix[2K it now")
        self.assertEqual(step["files"], ["a.py"])
        self.assertEqual(step["detail"], "detail")
        self.assertIsNone(agent._hidden_char("plain\ttext\n"))
        self.assertEqual(agent._hidden_char("a‮b"), "U+202E")

    def test_title_of_only_control_chars_is_dropped(self):
        with self.assertRaises(ValueError):
            agent.parse_plan(json.dumps({"steps": [{"title": "\x1b​"}]}))

    def test_deeply_nested_json_does_not_raise(self):
        for suffix, content in ((".json", "[" * 100000), (".json", "[" * 100000 + "]" * 100000), (".py", "(" * 100000)):
            with self.subTest(suffix=suffix):
                result = agent.check_syntax(f"deep{suffix}", content)
                self.assertIsInstance(result, str)
        changes, errors = self.validate("deep.json", "[" * 100000)
        self.assertEqual(changes, [])
        self.assertTrue(errors)


class LineEndingTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()

    def project(self, filename, raw: bytes):
        (self.repo / filename).write_bytes(raw)
        pid = agent.create_coding_project("goal", str(self.repo))
        agent.add_step(pid, "edit", [filename])
        return pid

    def test_crlf_file_keeps_crlf_and_diff_is_minimal(self):
        pid = self.project("c.py", b"a = 1\r\nb = 2\r\nc = 3\r\n")
        self.llm.push(proposal_reply([{"path": "c.py", "content": "a = 1\nb = 20\nc = 3\n"}]))
        out = agent.execute_coding_step(pid)
        self.assertEqual(out["status"], "proposed", out)
        self.assertIn("-b = 2", out["diff"])
        self.assertIn("+b = 20", out["diff"])
        self.assertNotIn("-a = 1", out["diff"])
        self.assertNotIn("-c = 3", out["diff"])
        self.assertNotIn("\r", out["diff"])
        agent.approve(pid, out["task_id"])
        self.assertEqual((self.repo / "c.py").read_bytes(), b"a = 1\r\nb = 20\r\nc = 3\r\n")

    def test_crlf_in_model_output_for_crlf_file(self):
        pid = self.project("c.py", b"a = 1\r\nb = 2\r\n")
        self.llm.push(proposal_reply([{"path": "c.py", "content": "a = 1\r\nb = 3\r\n"}]))
        out = agent.execute_coding_step(pid)
        agent.approve(pid, out["task_id"])
        self.assertEqual((self.repo / "c.py").read_bytes(), b"a = 1\r\nb = 3\r\n")

    def test_lf_file_stays_lf_and_new_file_is_lf(self):
        pid = self.project("l.py", b"a = 1\n")
        self.llm.push(
            proposal_reply(
                [{"path": "l.py", "content": "a = 2\r\n"}, {"path": "n.py", "content": "z = 1\r\n"}]
            )
        )
        out = agent.execute_coding_step(pid)
        agent.approve(pid, out["task_id"])
        self.assertEqual((self.repo / "l.py").read_bytes(), b"a = 2\n")
        self.assertEqual((self.repo / "n.py").read_bytes(), b"z = 1\n")

    def test_crlf_unchanged_content_is_no_change(self):
        pid = self.project("c.py", b"a = 1\r\n")
        self.llm.push(proposal_reply([{"path": "c.py", "content": "a = 1\n"}]))
        out = agent.execute_coding_step(pid)
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("does not change anything" in e for e in out["errors"]))

    def test_missing_trailing_newline_shows_marker(self):
        (self.repo / "m.py").write_bytes(b"x = 1")
        changes, errors = agent.validate_changes(self.repo, [{"path": "m.py", "content": "x = 2\n"}])
        self.assertEqual(errors, [])
        self.assertIn("\\ No newline at end of file", changes[0]["diff"])
        self.assertIn("-x = 1", changes[0]["diff"])

    def test_only_newline_added_is_a_visible_change(self):
        (self.repo / "m.py").write_bytes(b"x = 1")
        changes, errors = agent.validate_changes(self.repo, [{"path": "m.py", "content": "x = 1"}])
        self.assertEqual(errors, [])
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0]["diff"].strip())
        self.assertIn("\\ No newline at end of file", changes[0]["diff"])


class StepResultTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo({"calc.py": BUGGY, "setup.py": "x = 1\n"})
        self.pid = agent.create_coding_project("goal", str(self.repo))

    def test_attention_list(self):
        agent.add_step(self.pid, "touch setup", ["setup.py", "calc.py"])
        self.llm.push(
            proposal_reply(
                [{"path": "setup.py", "content": "x = 2\n"}, {"path": "calc.py", "content": FIXED}, {"path": "run.sh", "content": "echo hi\n"}]
            )
        )
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "proposed")
        self.assertEqual(out["attention"], ["setup.py", "run.sh"])
        flags = {f["path"]: f["attention"] for f in out["files"]}
        self.assertEqual(flags, {"setup.py": True, "calc.py": False, "run.sh": True})
        self.assertEqual(out["warnings"], [])

    def test_warning_after_failed_step(self):
        agent.add_step(self.pid, "first", ["calc.py"])
        agent.add_step(self.pid, "second", ["calc.py"])
        self.llm.push(HttpError(500, "down"), proposal_reply([{"path": "calc.py", "content": FIXED}]))
        self.assertEqual(agent.execute_coding_step(self.pid)["status"], "failed")
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "proposed")
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("first", out["warnings"][0])
        self.assertIn("failed", out["warnings"][0])

    def test_warning_after_rejected_step(self):
        agent.add_step(self.pid, "first", ["calc.py"])
        agent.add_step(self.pid, "second", ["calc.py"])
        self.llm.push(proposal_reply([{"path": "calc.py", "content": FIXED}]), proposal_reply([{"path": "calc.py", "content": FIXED + "# c\n"}]))
        first = agent.execute_coding_step(self.pid)
        agent.reject(self.pid, first["task_id"])
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "proposed")
        self.assertIn("rejected", out["warnings"][0])
        self.assertIn("first", out["warnings"][0])

    def test_reject_busy_while_slot_held(self):
        agent.add_step(self.pid, "first", ["calc.py"])
        self.llm.push(proposal_reply([{"path": "calc.py", "content": FIXED}]))
        out = agent.execute_coding_step(self.pid)
        with db.inference_slot("someone else"):
            with self.assertRaises(db.Busy):
                agent.reject(self.pid, out["task_id"])
        self.assertEqual(db.get_task(out["task_id"])["status"], "proposed")
        self.assertEqual(agent.reject(self.pid, out["task_id"])["status"], "rejected")


# --------------------------------------------------------------------------- app


class AppHardeningTests(AppTestCase):
    KEEPALIVE = {"Connection": "keep-alive"}

    def raw(self, method, path, **kw):
        kw["headers"] = {**self.KEEPALIVE, **kw.get("headers", {})}
        return self.request(method, path, **kw)

    def test_error_responses_close_connection(self):
        cases = {
            "bad token": self.raw("POST", "/api/recover", token="wrong"),
            "415": self.raw("POST", "/api/recover", content_type="text/plain"),
            "404": self.raw("GET", "/api/nope"),
            "413": self.raw("POST", "/api/projects", raw=b"{}", headers={"Content-Length": str(10**7)}),
            "403 host": self.raw("GET", "/api/projects", headers={"Host": "evil.com"}),
        }
        expected = {"bad token": 403, "415": 415, "404": 404, "413": 413, "403 host": 403}
        for name, (status, headers, _) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(status, expected[name])
                self.assertEqual(headers.get("Connection"), "close")

    def test_success_does_not_force_close(self):
        status, headers, _ = self.raw("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("Connection"), "close")

    def test_chunked_post_is_411(self):
        status, headers, body = self.request(
            "POST", "/api/projects", raw=iter([b'{"name": "x", "tasks": "y"}']),
            headers={"Transfer-Encoding": "chunked"},
        )
        self.assertEqual(status, 411)
        self.assertEqual(headers.get("Connection"), "close")
        self.assertEqual(db.list_projects(), [])

    def test_result_not_truncated(self):
        pid = self.new_project("x")
        self.llm.push(Text("hello"))
        _, data = self.api("POST", f"/api/projects/{pid}/run")
        status, res = self.api("GET", f"/api/tasks/{data['task']['id']}/result")
        self.assertEqual(status, 200)
        self.assertIs(res["truncated"], False)

    def test_result_truncated_flag(self):
        from backend import app

        pid = self.new_project("x")
        self.llm.push(Text("y" * 50))
        _, data = self.api("POST", f"/api/projects/{pid}/run")
        with mock.patch.object(api_core, "MAX_RESULT_CHARS", 10):
            status, res = self.api("GET", f"/api/tasks/{data['task']['id']}/result")
        self.assertEqual(status, 200)
        self.assertIs(res["truncated"], True)
        self.assertEqual(len(res["content"]), 10)

    def test_recover_force_when_idle(self):
        status, data = self.api("POST", "/api/recover", {"force": True})
        self.assertEqual((status, data), (200, {"recovered": []}))
        pid = self.new_project("x")
        task = db.claim_next_task(pid)  # running under a live owner, but no dashboard run
        status, data = self.api("POST", "/api/recover", {"force": True})
        self.assertEqual((status, data), (200, {"recovered": [task["id"]]}))
        self.assertEqual(db.get_task(task["id"])["status"], "failed")

    def test_recover_without_force_keeps_live_owner(self):
        pid = self.new_project("x")
        task = db.claim_next_task(pid)
        status, data = self.api("POST", "/api/recover", {"force": False})
        self.assertEqual(data, {"recovered": []})
        self.assertEqual(db.get_task(task["id"])["status"], "running")

    def test_force_recover_refused_while_loop_active(self):
        pid = self.new_project("a\nb")
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.llm.push(Gate(gate))
        self.api("POST", f"/api/projects/{pid}/loop")
        self.assertTrue(wait_until(lambda: len(self.llm.requests) >= 1))
        status, data = self.api("POST", "/api/recover", {"force": True})
        self.assertEqual(status, 409)
        self.assertEqual(db.list_tasks(pid)[0]["status"], "running")
        gate.set()
        self.wait_loop_done()

    def test_loop_last_message_uses_position_and_id(self):
        pid = self.new_project("a\nb")
        tasks = db.list_tasks(pid)
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.llm.push(Text("one"), Gate(gate, "two"))
        self.api("POST", f"/api/projects/{pid}/loop")
        expected = f"Task 1 (id {tasks[0]['id']}) done"
        self.assertTrue(
            wait_until(lambda: self.api("GET", "/api/loop")[1]["loop"]["last_message"] == expected),
            self.api("GET", "/api/loop")[1]["loop"]["last_message"],
        )
        gate.set()
        self.wait_loop_done()


# --------------------------------------------------------------------------- cli


class CliHardeningTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo({"calc.py": BUGGY})
        self.assertEqual(run("create", self.repo, "Fix add")[0], 0)
        run("add-step", 1, "Fix it", "--file", "calc.py")

    @staticmethod
    def stdin(tty: bool):
        return mock.patch("backend.cli.sys.stdin", mock.Mock(isatty=lambda: tty))

    def test_max_steps_bounds(self):
        with self.stdin(True):
            for n in (0, -1, 51):
                with self.subTest(n=n):
                    code, _, err = run("loop", 1, "--max-steps", n)
                    self.assertEqual(code, 2)
                    self.assertIn("--max-steps", err)
        self.assertEqual(self.llm.requests, [])

    def test_not_a_tty(self):
        with self.stdin(False):
            code, _, err = run("loop", 1)
        self.assertEqual(code, 2)
        self.assertIn("interactive terminal", err)
        self.assertEqual(self.llm.requests, [])
        self.assertEqual(db.get_task(1)["status"], "pending")

    def test_eof_at_prompt_is_quit(self):
        self.llm.push(proposal_reply([{"path": "calc.py", "content": FIXED}]))
        before = self.snapshot(self.repo)
        with self.stdin(True), mock.patch.object(builtins, "input", side_effect=EOFError):
            code, out, _ = run("loop", 1)
        self.assertEqual(code, 0)
        self.assertIn("Left task #1 for review", out)
        self.assertEqual(db.get_task(1)["status"], "proposed")
        self.assertEqual(self.snapshot(self.repo), before)

    def test_loop_answers(self):
        run("add-step", 1, "Second", "--file", "calc.py")
        self.llm.push(
            proposal_reply([{"path": "calc.py", "content": FIXED}]),
            proposal_reply([{"path": "calc.py", "content": FIXED + "# more\n"}]),
        )
        with self.stdin(True), mock.patch.object(builtins, "input", side_effect=["maybe", "y", "n"]):
            code, out, _ = run("loop", 1)
        self.assertEqual(code, 0)
        self.assertEqual(db.get_task(1)["status"], "approved")
        self.assertEqual(db.get_task(2)["status"], "rejected")
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)

    def test_escape_sequences_stripped_from_output(self):
        self.llm.push(
            proposal_reply([{"path": "calc.py", "content": FIXED}], summary="\x1b[2J\x1b]0;pwned\x07Looks‮ fine")
        )
        code, out, _ = run("step", 1)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("‮", out)
        self.assertIn("Looks", out)
        code, out, _ = run("show", 1, 1)
        self.assertEqual(code, 0)
        for bad in ("\x1b", "\x07", "‮"):
            self.assertNotIn(bad, out)
        self.assertIn("Looks", out)

    def test_step_output_lists_attention(self):
        run("add-step", 1, "Edit makefile", "--file", "Makefile")
        db.transition(1, ["pending"], "rejected")
        self.llm.push(proposal_reply([{"path": "Makefile", "content": "all:\n\techo hi\n"}]))
        code, out, _ = run("step", 1)
        self.assertEqual(code, 0)
        self.assertIn("Makefile", out)
        self.assertIn("WARNING", out)  # earlier rejected step and/or attention note


if __name__ == "__main__":
    unittest.main()
