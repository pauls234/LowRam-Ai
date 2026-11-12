import json
import threading
import unittest
from pathlib import Path

from backend import db, memory, runner
from tests.helpers import Empty, HttpError, LowRAMTestCase, Text


class CreateProjectTests(LowRAMTestCase):
    def test_creates_tasks_from_lines(self):
        pid = runner.create_project("  My   project ", "one\n\n  two  \nthree")
        project = db.get_project(pid)
        self.assertEqual(project["name"], "My project")
        self.assertEqual(project["kind"], "general")
        self.assertEqual([t["title"] for t in db.list_tasks(pid)], ["one", "two", "three"])

    def test_validation(self):
        bad_names = [None, "", "   ", 5, "x" * 121]
        for name in bad_names:
            with self.subTest(name=name), self.assertRaises(ValueError):
                runner.create_project(name, "task")
        bad_tasks = [None, "", " \n \n", 3, "x" * 2001, "\n".join("t" for _ in range(201))]
        for tasks in bad_tasks:
            with self.subTest(tasks=str(tasks)[:20]), self.assertRaises(ValueError):
                runner.create_project("ok", tasks)
        self.assertEqual(db.list_projects(), [])

    def test_boundaries_accepted(self):
        runner.create_project("n" * 120, "\n".join("t" for _ in range(200)))
        runner.create_project("ok", "x" * 2000)

    def test_add_tasks_respects_limit(self):
        pid = runner.create_project("p", "\n".join("t" for _ in range(199)))
        runner.add_tasks(pid, "one more")
        with self.assertRaises(ValueError):
            runner.add_tasks(pid, "too many")

    def test_validate_max_tasks(self):
        self.assertEqual(runner.validate_max_tasks("5"), 5)
        self.assertEqual(runner.validate_max_tasks(100), 100)
        for bad in (0, 101, True, "x", None, 2.5, -1):
            with self.subTest(v=bad), self.assertRaises(ValueError):
                runner.validate_max_tasks(bad)


class RunNextTests(LowRAMTestCase):
    def test_done_writes_artifacts(self):
        pid = runner.create_project("p", "Say hi")
        self.llm.push(Text("Hello!"))
        out = runner.run_next(pid)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["result"], "Hello!")
        task = db.list_tasks(pid)[0]
        self.assertEqual(task["status"], "done")
        md = Path(task["result_path"])
        self.assertEqual(md.suffix, ".md")
        self.assertEqual(md.read_text(), "Hello!\n")
        meta = json.loads(md.with_suffix(".json").read_text())
        self.assertEqual(meta["task_id"], task["id"])
        self.assertEqual(meta["title"], "Say hi")
        self.assertEqual(meta["percent"], 40)
        self.assertIn(str(self.home), str(md))
        prompt = self.llm.user_prompt()
        self.assertIn("Say hi", prompt)
        self.assertEqual(self.llm.requests[0]["json"]["max_tokens"], memory.plan(40).max_tokens)

    def test_failures_are_recorded_never_done(self):
        cases = [HttpError(500, "kaboom"), Empty()]
        for item in cases:
            with self.subTest(item=type(item).__name__):
                pid = runner.create_project("p", "task")
                self.llm.push(item)
                out = runner.run_next(pid)
                self.assertEqual(out["status"], "failed")
                self.assertTrue(out["error"])
                task = db.list_tasks(pid)[0]
                self.assertEqual(task["status"], "failed")
                self.assertTrue(task["error"])
                self.assertFalse(task["has_result"])

    def test_unreachable_model_fails_task(self):
        import os
        from unittest import mock
        from tests.helpers import closed_port_url

        pid = runner.create_project("p", "task")
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_URL": closed_port_url()}):
            out = runner.run_next(pid)
        self.assertEqual(out["status"], "failed")
        self.assertIn("Could not reach", out["error"])

    def test_paused_on_low_ram_claims_nothing(self):
        self.set_memory(total_gib=8, available_mib=100)
        pid = runner.create_project("p", "task")
        out = runner.run_next(pid)
        self.assertEqual(out["status"], "paused")
        self.assertIn("Paused", out["reason"])
        self.assertEqual(db.list_tasks(pid)[0]["status"], "pending")
        self.assertEqual(db.list_tasks(pid)[0]["attempts"], 0)
        self.assertEqual(self.llm.requests, [])

    def test_empty_queue(self):
        pid = runner.create_project("p", "task")
        runner.run_next(pid)
        out = runner.run_next(pid)
        self.assertEqual(out["status"], "empty")
        self.assertEqual(len(self.llm.requests), 1)

    def test_invalid_percent_and_unknown_project(self):
        pid = runner.create_project("p", "task")
        with self.assertRaises(ValueError):
            runner.run_next(pid, 10)
        with self.assertRaises(db.NotFound):
            runner.run_next(999)

    def test_busy_when_another_task_running(self):
        a = runner.create_project("a", "t1\nt2")
        db.claim_next_task(a)
        with self.assertRaises(db.Busy):
            runner.run_next(a)

    def test_refuses_coding_projects(self):
        pid = db.create_project("c", "coding", goal="g", repo_path=str(self.tmp))
        with self.assertRaisesRegex(ValueError, "coding project"):
            runner.run_next(pid)
        with self.assertRaises(ValueError):
            runner.run_loop(pid)
        with self.assertRaises(ValueError):
            runner.add_tasks(pid, "x")
        self.assertEqual(self.llm.requests, [])

    def test_carry_over_context(self):
        pid = runner.create_project("p", "first\nsecond\nthird")
        self.llm.push(Text("ALPHA-RESULT"), Text("BETA-RESULT"), Text("done"))
        runner.run_next(pid)
        self.assertNotIn("Results of earlier tasks", self.llm.user_prompt(0))
        runner.run_next(pid)
        second = self.llm.user_prompt(1)
        self.assertIn("ALPHA-RESULT", second)
        self.assertIn("Task 1: first", second)
        self.assertIn("Current task (2): second", second)
        runner.run_next(pid)
        third = self.llm.user_prompt(2)
        self.assertIn("ALPHA-RESULT", third)
        self.assertIn("BETA-RESULT", third)
        self.assertLess(third.index("ALPHA-RESULT"), third.index("BETA-RESULT"))

    def test_failed_tasks_are_not_carried_over(self):
        pid = runner.create_project("p", "first\nsecond")
        self.llm.push(HttpError(500, "no"), Text("fine"))
        runner.run_next(pid)
        runner.run_next(pid)
        self.assertNotIn("Results of earlier tasks", self.llm.user_prompt(1))

    def test_carry_over_truncated_to_budget(self):
        self.set_memory(total_gib=1, available_mib=800)
        budget = memory.plan(20).context_chars
        self.assertEqual(budget, memory.MIN_CONTEXT_CHARS)
        pid = runner.create_project("p", "first\nsecond")
        self.llm.push(Text("X" * 50000), Text("ok"))
        runner.run_next(pid, 20)
        runner.run_next(pid, 20)
        second = self.llm.user_prompt(1)
        self.assertIn("[…truncated]", second)
        self.assertLessEqual(second.count("X"), budget)
        self.assertLess(len(second), budget + 300)

    def test_newest_results_win_when_budget_is_small(self):
        self.set_memory(total_gib=1, available_mib=800)
        pid = runner.create_project("p", "a\nb\nc")
        self.llm.push(Text("OLD" * 1000), Text("NEW" * 1000), Text("ok"))
        for _ in range(3):
            runner.run_next(pid, 20)
        third = self.llm.user_prompt(2)
        self.assertIn("NEW", third)
        self.assertLessEqual(len(third), memory.MIN_CONTEXT_CHARS + 300)

    def test_context_skips_missing_artifact(self):
        pid = runner.create_project("p", "first\nsecond")
        runner.run_next(pid)
        Path(db.list_tasks(pid)[0]["result_path"]).unlink()
        out = runner.run_next(pid)
        self.assertEqual(out["status"], "done")
        self.assertNotIn("Results of earlier tasks", self.llm.user_prompt(1))


class RunLoopTests(LowRAMTestCase):
    def test_queue_empty(self):
        pid = runner.create_project("p", "a\nb")
        s = runner.run_loop(pid)
        self.assertEqual(s["stopped_because"], "queue_empty")
        self.assertEqual((s["completed"], s["failed"]), (2, 0))
        self.assertEqual([x["status"] for x in s["steps"]], ["done", "done"])

    def test_max_tasks(self):
        pid = runner.create_project("p", "a\nb\nc")
        s = runner.run_loop(pid, max_tasks=2)
        self.assertEqual(s["stopped_because"], "max_tasks")
        self.assertEqual(s["completed"], 2)
        self.assertEqual(db.list_tasks(pid)[2]["status"], "pending")

    def test_max_tasks_validated(self):
        pid = runner.create_project("p", "a")
        with self.assertRaises(ValueError):
            runner.run_loop(pid, max_tasks=0)
        with self.assertRaises(ValueError):
            runner.run_loop(pid, percent=5)

    def test_failure_stops(self):
        pid = runner.create_project("p", "a\nb\nc")
        self.llm.push(Text("ok"), HttpError(500, "bad"))
        s = runner.run_loop(pid)
        self.assertEqual(s["stopped_because"], "failure")
        self.assertEqual((s["completed"], s["failed"]), (1, 1))
        self.assertIn("500", s["message"])
        self.assertEqual(db.list_tasks(pid)[2]["status"], "pending")
        self.assertEqual(len(self.llm.requests), 2)

    def test_keep_going(self):
        pid = runner.create_project("p", "a\nb\nc")
        self.llm.push(HttpError(500, "bad"), Text("ok"), Text("ok"))
        s = runner.run_loop(pid, stop_on_failure=False)
        self.assertEqual(s["stopped_because"], "queue_empty")
        self.assertEqual((s["completed"], s["failed"]), (2, 1))
        self.assertEqual([t["status"] for t in db.list_tasks(pid)], ["failed", "done", "done"])

    def test_paused_low_ram(self):
        self.set_memory(total_gib=8, available_mib=10)
        pid = runner.create_project("p", "a")
        s = runner.run_loop(pid)
        self.assertEqual(s["stopped_because"], "paused_low_ram")
        self.assertEqual(s["steps"], [])
        self.assertEqual(db.list_tasks(pid)[0]["status"], "pending")
        self.assertEqual(self.llm.requests, [])

    def test_stop_requested_before_start(self):
        pid = runner.create_project("p", "a")
        ev = threading.Event()
        ev.set()
        s = runner.run_loop(pid, stop_event=ev)
        self.assertEqual(s["stopped_because"], "stop_requested")
        self.assertEqual(self.llm.requests, [])

    def test_stop_requested_between_tasks(self):
        pid = runner.create_project("p", "a\nb\nc")
        ev = threading.Event()
        s = runner.run_loop(pid, stop_event=ev, on_step=lambda outcome: ev.set())
        self.assertEqual(s["stopped_because"], "stop_requested")
        self.assertEqual(s["completed"], 1)
        self.assertEqual([t["status"] for t in db.list_tasks(pid)], ["done", "pending", "pending"])

    def test_busy_stop_reason(self):
        a = runner.create_project("a", "t1")
        with db.inference_slot("other"):
            s = runner.run_loop(a)
        self.assertEqual(s["stopped_because"], "busy")

    def test_on_step_receives_each_outcome(self):
        pid = runner.create_project("p", "a\nb")
        seen = []
        runner.run_loop(pid, on_step=lambda o: seen.append(o["status"]))
        self.assertEqual(seen, ["done", "done", "empty"])


if __name__ == "__main__":
    unittest.main()
