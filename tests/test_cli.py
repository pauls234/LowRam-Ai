import contextlib
import io
import json
import unittest
from pathlib import Path

from backend import cli, db
from tests.helpers import HttpError, LowRAMTestCase, Text
from tests.test_agent import BUGGY, FIXED, proposal_reply
from tests.test_skills import URL, FakeOpener


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as exc:  # argparse errors
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class GeneralCliTests(LowRAMTestCase):
    def test_memory(self):
        self.set_memory(total_gib=8, available_mib=4096)
        code, out, _ = run("memory")
        self.assertEqual(code, 0)
        self.assertIn("ready", out)
        self.assertIn("8.0 GiB", out)
        code, out, _ = run("--json", "memory", "--ram", 60)
        data = json.loads(out)
        self.assertEqual((code, data["percent"], data["can_run"]), (0, 60, True))

    def test_memory_paused_exit_code(self):
        self.set_memory(total_gib=8, available_mib=10)
        code, out, _ = run("memory")
        self.assertEqual(code, 3)
        self.assertIn("PAUSED", out)

    def test_memory_invalid_ram(self):
        code, out, err = run("memory", "--ram", 19)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Error:"))
        self.assertEqual(out, "")

    def test_new_tasks_projects(self):
        code, out, _ = run("new", "Demo", "--task", "first", "--task", "second")
        self.assertEqual(code, 0)
        self.assertIn("Created project #1 with 2 task(s)", out)
        taskfile = self.tmp / "tasks.txt"
        taskfile.write_text("third\n\nfourth\n")
        code, out, _ = run("--json", "new", "From file", "--file", taskfile, "--task", "zero")
        self.assertEqual(json.loads(out)["total"], 3)
        code, out, _ = run("tasks", 1)
        self.assertEqual(code, 0)
        self.assertIn("[pending ]", out)
        self.assertIn("first", out)
        code, out, _ = run("--json", "tasks", 1)
        self.assertEqual([t["title"] for t in json.loads(out)], ["first", "second"])
        code, out, _ = run("projects")
        self.assertIn("Demo", out)
        self.assertIn("From file", out)
        self.assertEqual(len(json.loads(run("--json", "projects")[1])), 2)

    def test_empty_listings(self):
        self.assertIn("No projects yet", run("projects")[1])
        self.assertEqual(json.loads(run("--json", "projects")[1]), [])

    def test_new_errors(self):
        code, out, err = run("new", "Empty")
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        code, _, err = run("new", "X", "--file", self.tmp / "missing.txt")
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)

    def test_argparse_errors_exit_2(self):
        self.assertEqual(run()[0], 2)
        self.assertEqual(run("bogus")[0], 2)
        self.assertEqual(run("tasks", "abc")[0], 2)

    def test_run_result_retry(self):
        run("new", "Demo", "--task", "a", "--task", "b")
        self.llm.push(Text("answer A"), HttpError(500, "nope"))
        code, out, err = run("run", 1)
        self.assertEqual(code, 1)
        self.assertIn("[done]", out)
        self.assertIn("[failed]", out)
        self.assertIn("1 done, 1 failed", out)
        self.assertEqual(err, "")
        code, out, _ = run("result", 1)
        self.assertEqual((code, out.strip()), (0, "answer A"))
        self.assertEqual(json.loads(run("--json", "result", 1)[1])["content"].strip(), "answer A")
        code, out, _ = run("tasks", 1)
        self.assertIn("! ", out)
        code, out, _ = run("retry", 2)
        self.assertEqual(code, 0)
        self.assertIn("pending again", out)
        code, out, _ = run("--json", "run", 1)
        summary = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(summary["stopped_because"], "queue_empty")
        self.assertEqual(summary["completed"], 1)

    def test_run_keep_going_and_max_tasks(self):
        run("new", "Demo", "--task", "a", "--task", "b", "--task", "c")
        self.llm.push(HttpError(500, "x"))
        code, out, _ = run("--json", "run", 1, "--keep-going")
        s = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual((s["completed"], s["failed"]), (2, 1))
        run("new", "Two", "--task", "a", "--task", "b")
        s = json.loads(run("--json", "run", 2, "--max-tasks", 1)[1])
        self.assertEqual(s["stopped_because"], "max_tasks")

    def test_run_low_ram(self):
        run("new", "Demo", "--task", "a")
        self.set_memory(total_gib=8, available_mib=10)
        code, out, _ = run("run", 1)
        self.assertEqual(code, 3)  # nothing ran: distinguishable from success
        self.assertIn("paused_low_ram", out)
        self.assertEqual(self.llm.requests, [])

    def test_error_exit_codes(self):
        for argv in (
            ("run", 99),
            ("tasks", 99),
            ("result", 99),
            ("retry", 99),
            ("show", 99, 1),
            ("run", 1, "--max-tasks", 0),
        ):
            with self.subTest(argv=argv):
                code, out, err = run(*argv)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("Error:"), err)
        run("new", "Demo", "--task", "a")
        code, _, err = run("retry", 1)  # pending, not failed
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        code, _, err = run("result", 1)  # no result yet
        self.assertEqual(code, 1)

    def test_busy_error(self):
        run("new", "Demo", "--task", "a", "--task", "b")
        db.claim_next_task(1)
        code, out, _ = run("--json", "run", 1)
        self.assertEqual(code, 3)  # reported as stopped_because=busy, not a crash
        self.assertEqual(json.loads(out)["stopped_because"], "busy")

    def test_recover(self):
        self.assertIn("Nothing to recover", run("recover")[1])
        run("new", "Demo", "--task", "a")
        db.claim_next_task(1)
        code, out, _ = run("recover")
        self.assertIn("Nothing to recover", out)  # live owner
        code, out, _ = run("--json", "recover", "--force")
        self.assertEqual(json.loads(out), {"recovered": [1]})
        self.assertEqual(db.get_task(1)["status"], "failed")

    def test_skill_list_and_json(self):
        self.assertIn("No skills installed", run("skill", "list")[1])
        from backend import skills

        skills.install_skill(URL, "demo", opener=FakeOpener(b"# hi\n"))
        code, out, _ = run("skill", "list")
        self.assertEqual(code, 0)
        self.assertIn("demo", out)
        self.assertIn("ok", out)
        items = json.loads(run("--json", "skill", "list")[1])
        self.assertEqual([i["name"] for i in items], ["demo"])
        self.assertTrue(items[0]["checksum_ok"])
        (self.home / "skills" / "demo.md").write_text("changed")
        self.assertIn("CHECKSUM MISMATCH", run("skill", "list")[1])

    def test_skill_install_rejects_bad_url_without_network(self):
        code, _, err = run("skill", "install", "https://evil.com/x.md", "demo")
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        self.assertIn("not approved", err)

    def test_version(self):
        code, out, _ = run("--version")
        self.assertEqual(code, 0)
        self.assertIn("LowRAM AI", out)

    def test_loop_needs_tty(self):
        code, _, err = run("loop", 1)
        self.assertEqual(code, 2)
        self.assertIn("interactive", err)


class CodingCliTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo({"calc.py": BUGGY})
        self.before = self.snapshot(self.repo)

    def create(self):
        code, out, _ = run("create", self.repo, "Fix add")
        self.assertEqual(code, 0)
        self.assertIn("Created coding project #1", out)

    def test_create_errors(self):
        code, _, err = run("create", "relative/dir", "goal")
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        code, _, err = run("create", self.tmp / "nope", "goal")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(run("--json", "projects")[1]), [])

    def test_plan_step_show_approve(self):
        self.create()
        plan = {"steps": [{"title": "Fix add", "files": ["calc.py"], "detail": "use plus"}]}
        self.llm.push(Text(json.dumps(plan)))
        code, out, _ = run("plan", 1)
        self.assertEqual(code, 0)
        self.assertIn("Fix add", out)
        code, _, err = run("plan", 1)  # open steps exist
        self.assertEqual(code, 1)
        self.assertIn("open step", err)

        self.llm.push(proposal_reply([{"path": "calc.py", "content": FIXED}]))
        code, out, _ = run("step", 1)
        self.assertEqual(code, 0)
        self.assertIn("Proposal for task #1", out)
        self.assertIn("modify calc.py", out)
        self.assertEqual(self.snapshot(self.repo), self.before)

        code, out, _ = run("show", 1, 1)
        self.assertEqual(code, 0)
        self.assertIn("+    return a + b", out)
        shown = json.loads(run("--json", "show", 1, 1)[1])
        self.assertEqual(shown["changes"][0]["path"], "calc.py")

        code, out, _ = run("approve", 1, 1)
        self.assertEqual(code, 0)
        self.assertIn("Applied task #1: calc.py", out)
        self.assertIn("Backups", out)
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)
        code, _, err = run("approve", 1, 1)
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        code, out, _ = run("step", 1)
        self.assertEqual(code, 0)
        self.assertIn("empty", out)

    def test_add_step_and_reject(self):
        self.create()
        code, out, _ = run("add-step", 1, "Fix it", "--file", "calc.py", "--detail", "plus")
        self.assertEqual(code, 0)
        self.assertIn("Added step #1", out)
        code, _, err = run("add-step", 1, "Bad", "--file", "../x.py")
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)
        self.llm.push(proposal_reply([{"path": "calc.py", "content": FIXED}]))
        data = json.loads(run("--json", "step", 1)[1])
        self.assertEqual(data["status"], "proposed")
        code, _, err = run("step", 1)  # a proposal is awaiting review
        self.assertEqual(code, 1)
        self.assertIn("awaiting review", err)
        code, out, _ = run("reject", 1, 1, "--reason", "meh")
        self.assertEqual(code, 0)
        self.assertIn("repository was not changed", out)
        self.assertEqual(self.snapshot(self.repo), self.before)
        self.assertEqual(db.get_task(1)["error"], "Rejected by user: meh")

    def test_step_failure_exit_code(self):
        self.create()
        run("add-step", 1, "Fix it", "--file", "calc.py")
        self.llm.push(proposal_reply([{"path": "calc.py", "content": "def broken(:\n"}]))
        code, out, err = run("step", 1)
        self.assertEqual(code, 1)
        self.assertIn("failed", out)
        self.assertIn("syntax error", out)
        self.assertEqual(self.snapshot(self.repo), self.before)

    def test_step_review_pending_is_error(self):
        self.create()
        run("add-step", 1, "One", "--file", "calc.py")
        run("add-step", 1, "Two")
        self.llm.push(proposal_reply([{"path": "calc.py", "content": FIXED}]))
        self.assertEqual(run("step", 1)[0], 0)
        code, _, err = run("step", 1)
        self.assertEqual(code, 1)
        self.assertIn("Error:", err)

    def test_general_run_refuses_coding_project(self):
        self.create()
        code, _, err = run("run", 1)
        self.assertEqual(code, 1)
        self.assertIn("coding project", err)

    def test_step_paused(self):
        self.create()
        run("add-step", 1, "One")
        self.set_memory(total_gib=8, available_mib=10)
        code, out, _ = run("step", 1)
        self.assertEqual(code, 1)
        self.assertIn("paused", out)


if __name__ == "__main__":
    unittest.main()
