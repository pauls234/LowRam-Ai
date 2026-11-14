import json
import os
import unittest
from pathlib import Path
from unittest import mock

from backend import agent, db
from tests.helpers import HttpError, LowRAMTestCase, Text

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def proposal_reply(changes, summary="fix it", fenced=True):
    body = json.dumps({"summary": summary, "changes": changes})
    return Text(f"Sure! Here you go:\n```json\n{body}\n```\nHope that helps." if fenced else body)


class ExtractJsonTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(agent.extract_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(agent.extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(agent.extract_json_object('```\n{"a": 2}\n```'), {"a": 2})
        self.assertEqual(agent.extract_json_object('Here: {"a": {"b": [1, 2]}} bye'), {"a": {"b": [1, 2]}})
        self.assertEqual(agent.extract_json_object('<think>{"a": 0}</think>{"a": 3}'), {"a": 3})
        self.assertEqual(agent.extract_json_object('<think>hmm {"x": 1} </think>\n```json\n{"a": 4}\n```'), {"a": 4})
        self.assertEqual(agent.extract_json_object('{broken {"a": 5}'), {"a": 5})

    def test_no_json(self):
        for bad in ("", "no json here", "[1, 2, 3]", "<think>{\"a\":1}</think>", "{unclosed"):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                agent.extract_json_object(bad)


class ParsePlanTests(unittest.TestCase):
    def test_parse(self):
        raw = json.dumps(
            {
                "steps": [
                    {"title": "  Do   thing ", "files": ["a.py", "", 5, " b.py "], "detail": " why "},
                    "junk",
                    {"title": ""},
                    {"title": "second"},
                ]
            }
        )
        steps = agent.parse_plan(raw)
        self.assertEqual(steps[0], {"title": "Do thing", "files": ["a.py", "b.py"], "detail": "why"})
        self.assertEqual(steps[1], {"title": "second", "files": [], "detail": ""})
        self.assertEqual(len(steps), 2)

    def test_caps_steps(self):
        raw = json.dumps({"steps": [{"title": f"s{i}"} for i in range(20)]})
        self.assertEqual(len(agent.parse_plan(raw)), agent.MAX_PLAN_STEPS)

    def test_invalid(self):
        for raw in ('{"steps": []}', '{"steps": "x"}', '{"nope": 1}', '{"steps": [{"title": 3}]}', "prose"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                agent.parse_plan(raw)


class CheckSyntaxTests(unittest.TestCase):
    def test_python(self):
        self.assertIsNone(agent.check_syntax("a.py", "x = 1\n"))
        self.assertIn("syntax error", agent.check_syntax("a.py", "def (:\n"))

    def test_json(self):
        self.assertIsNone(agent.check_syntax("a.json", '{"a": 1}'))
        self.assertIn("invalid JSON", agent.check_syntax("a.json", "{a: 1}"))

    @unittest.skipIf(agent.tomllib is None, "TOML checking needs tomllib (Python 3.11+)")
    def test_toml(self):
        self.assertIsNone(agent.check_syntax("a.toml", 'a = 1\n[t]\nb = "x"\n'))
        self.assertIn("a.toml", agent.check_syntax("a.toml", "a = = 1\n"))

    def test_other_types_unchecked(self):
        self.assertIsNone(agent.check_syntax("a.md", "def (:"))
        self.assertIsNone(agent.check_syntax("a.js", "function ("))


class CreateProjectTests(LowRAMTestCase):
    def test_success(self):
        repo = self.make_repo({"a.py": "x=1\n"})
        pid = agent.create_coding_project("  Fix the bug ", str(repo))
        project = db.get_project(pid)
        self.assertEqual(project["kind"], "coding")
        self.assertEqual(project["goal"], "Fix the bug")
        self.assertEqual(project["repo_path"], str(repo))
        self.assertTrue(project["name"].startswith("repo: Fix the bug"))

    def test_validation(self):
        repo = self.make_repo()
        a_file = repo / "f.txt"
        a_file.write_text("x")
        cases = [
            ("goal", "relative/path"),
            ("goal", str(self.tmp / "missing")),
            ("goal", str(a_file)),
            ("goal", ""),
            ("goal", None),
            ("goal", "/"),
            ("", str(repo)),
            ("   ", str(repo)),
            (None, str(repo)),
            ("g" * 2001, str(repo)),
        ]
        for goal, path in cases:
            with self.subTest(goal=str(goal)[:10], path=path), self.assertRaises(ValueError):
                agent.create_coding_project(goal, path)
        self.assertEqual(db.list_projects(), [])

    def test_home_directory_refused(self):
        home = Path(os.environ["HOME"])
        home.mkdir(parents=True, exist_ok=True)
        with self.assertRaisesRegex(ValueError, "home directory"):
            agent.create_coding_project("goal", str(home))
        with self.assertRaisesRegex(ValueError, "home directory"):
            agent.create_coding_project("goal", "~")

    def test_subdirectory_of_home_ok(self):
        home = Path(os.environ["HOME"])
        sub = home / "proj"
        sub.mkdir(parents=True)
        agent.create_coding_project("goal", str(sub))

    def test_goal_boundary_and_long_name(self):
        repo = self.make_repo()
        pid = agent.create_coding_project("g" * 2000, str(repo))
        self.assertLess(len(db.get_project(pid)["name"]), 100)

    def test_general_project_is_not_coding(self):
        pid = db.create_project("g", "general", tasks=[("t", "")])
        with self.assertRaises(ValueError):
            agent.plan(pid)
        with self.assertRaises(ValueError):
            agent.execute_coding_step(pid)


class IndexTests(LowRAMTestCase):
    def test_skips_unsafe_and_irrelevant(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "leak.py").write_text("x = 1\n")
        repo = self.make_repo(
            {
                "ok.py": "def f():\n    pass\n\nclass K:\n    pass\n",
                "sub/readme.md": "# Title here\ntext\n",
                ".github/workflows/ci.yml": "on: push\n",
                ".git/config": "[core]\n",
                "node_modules/pkg/index.js": "x\n",
                ".hidden/secret.py": "x = 1\n",
                "__pycache__/m.py": "x = 1\n",
                ".env": "KEY=1\n",
                ".env.local": "KEY=1\n",
                "key.pem": "-----BEGIN-----\n",
                "image.png": "binary",
                "noext": "text",
                "Makefile": "all:\n",
                "huge.py": "#" * (agent.MAX_FILE_BYTES + 1),
            }
        )
        os.symlink(outside / "leak.py", repo / "link.py")
        os.symlink(outside, repo / "linkdir")
        index = {e["path"]: e for e in agent.index_repository(repo)}
        self.assertEqual(
            set(index), {"ok.py", "sub/readme.md", ".github/workflows/ci.yml", "Makefile"}
        )
        self.assertIn("defines f, K", index["ok.py"]["summary"])
        self.assertIn("Title here", index["sub/readme.md"]["summary"])

    def test_max_files(self):
        repo = self.make_repo({f"f{i}.py": "x = 1\n" for i in range(10)})
        self.assertEqual(len(agent.index_repository(repo, max_files=3)), 3)

    def test_render_index_budget(self):
        entries = [{"path": f"f{i}.py", "summary": "1 lines"} for i in range(100)]
        text = agent.render_index(entries, 100)
        self.assertIn("more file(s) not shown", text)
        self.assertLess(len(text), 200)
        self.assertEqual(agent.render_index([], 100), "(no text files found)")


class ResolvePathTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "x.py").write_text("x = 1\n")
        self.repo = self.make_repo({"src/a.py": "x = 1\n", "real/ok.py": "y\n"})
        os.symlink(self.outside, self.repo / "esc")
        os.symlink(self.outside / "x.py", self.repo / "flink.py")
        (self.repo / "adir.py").mkdir()

    def test_accepts_normal_paths(self):
        self.assertEqual(agent.resolve_repo_path(self.repo, "src/a.py"), self.repo / "src" / "a.py")
        self.assertEqual(agent.resolve_repo_path(self.repo, "new/dir/file.md"), self.repo / "new/dir/file.md")
        agent.resolve_repo_path(self.repo, ".github/workflows/ci.yml")
        agent.resolve_repo_path(self.repo, "Makefile")
        agent.resolve_repo_path(self.repo, ".gitignore")

    def test_rejects(self):
        bad = [
            "/etc/passwd.py",
            "../outside/x.py",
            "src/../../outside/x.py",
            "src/../a.py",
            ".git/config.txt",
            ".git",
            "sub/.git/hooks.py",
            ".hidden/a.py",
            "node_modules/a.js",
            "esc/x.py",
            "esc/new.py",
            "flink.py",
            ".env",
            "src/.env",
            ".env.production",
            "server.pem",
            "id_rsa",
            "credentials.json",
            "pic.png",
            "noext",
            "~/x.py",
            "C:\\x.py",
            "src\\a.py",
            "",
            "   ",
            None,
            5,
            "a\x00.py",
            "a/" * 200 + "b.py",
            "adir.py",
        ]
        for rel in bad:
            with self.subTest(path=rel), self.assertRaises(agent.ProposalError):
                agent.resolve_repo_path(self.repo, rel)

    def test_env_example_allowed_by_sensitivity_but_text_check_applies(self):
        self.assertFalse(agent.is_sensitive(".env.example"))
        self.assertTrue(agent.is_sensitive(".ENV"))
        self.assertTrue(agent.is_sensitive("Server.PEM"))


class PlanTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo({"calc.py": BUGGY, "README.md": "# Calc\n"})
        self.pid = agent.create_coding_project("Fix add", str(self.repo))

    def test_plan_stores_steps(self):
        self.llm.push(
            Text(
                "<think>planning</think>"
                + json.dumps({"steps": [{"title": "Fix add", "files": ["calc.py"], "detail": "use +"}, {"title": "Docs"}]})
            )
        )
        tasks = agent.plan(self.pid)
        self.assertEqual([t["title"] for t in tasks], ["Fix add", "Docs"])
        self.assertEqual({t["status"] for t in tasks}, {"pending"})
        self.assertEqual(json.loads(tasks[0]["detail"]), {"files": ["calc.py"], "detail": "use +"})
        prompt = self.llm.user_prompt()
        self.assertIn("Fix add", prompt)
        self.assertIn("calc.py", prompt)
        self.assertIn("README.md", prompt)
        folder = self.home / "artifacts" / f"project_{self.pid}"
        self.assertTrue((folder / "plan.json").exists())
        self.assertTrue((folder / "index.json").exists())
        self.assertIsNone(db.busy_reason())  # slot released

    def test_plan_refuses_when_open_steps_exist(self):
        self.llm.push(Text(json.dumps({"steps": [{"title": "one"}]})))
        agent.plan(self.pid)
        with self.assertRaisesRegex(agent.AgentError, "open step"):
            agent.plan(self.pid)
        self.assertEqual(len(self.llm.requests), 1)

    def test_plan_allowed_again_when_steps_finished(self):
        self.llm.push(Text(json.dumps({"steps": [{"title": "one"}]})), Text(json.dumps({"steps": [{"title": "two"}]})))
        agent.plan(self.pid)
        task = db.claim_next_task(self.pid)
        db.finish_task(task["id"], "failed", error="x")
        tasks = agent.plan(self.pid)
        self.assertEqual([t["title"] for t in tasks], ["one", "two"])

    def test_unusable_plan_raises_and_adds_nothing(self):
        self.llm.push(Text("I cannot do that."))
        with self.assertRaisesRegex(agent.AgentError, "usable plan"):
            agent.plan(self.pid)
        self.assertEqual(db.list_tasks(self.pid), [])
        self.assertIsNone(db.busy_reason())

    def test_plan_low_ram(self):
        self.set_memory(total_gib=8, available_mib=10)
        with self.assertRaisesRegex(agent.AgentError, "Paused"):
            agent.plan(self.pid)
        self.assertEqual(self.llm.requests, [])

    def test_plan_llm_error_releases_slot(self):
        self.llm.push(HttpError(500, "x"))
        with self.assertRaises(Exception):
            agent.plan(self.pid)
        self.assertIsNone(db.busy_reason())

    def test_plan_busy(self):
        with db.inference_slot("other"):
            with self.assertRaises(db.Busy):
                agent.plan(self.pid)

    def test_add_step_validates_paths(self):
        tid = agent.add_step(self.pid, "  Fix   it ", ["calc.py"], " detail ")
        task = db.get_task(tid)
        self.assertEqual(task["title"], "Fix it")
        self.assertEqual(json.loads(task["detail"]), {"files": ["calc.py"], "detail": "detail"})
        with self.assertRaises(agent.ProposalError):
            agent.add_step(self.pid, "x", ["../evil.py"])
        with self.assertRaises(ValueError):
            agent.add_step(self.pid, "  ")


class StepTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo({"calc.py": BUGGY, "notes.txt": "keep\n"})
        self.pid = agent.create_coding_project("Fix add", str(self.repo))
        self.tid = agent.add_step(self.pid, "Fix add", ["calc.py"], "use plus")
        self.before = self.snapshot(self.repo)

    def folder(self) -> Path:
        return self.home / "artifacts" / f"project_{self.pid}"

    def propose(self, changes=None, **kw):
        changes = changes or [{"path": "calc.py", "content": FIXED}, {"path": "new/util.py", "content": "X = 1\n"}]
        self.llm.push(proposal_reply(changes, **kw))
        return agent.execute_coding_step(self.pid)

    def test_proposed_leaves_repo_untouched(self):
        out = self.propose()
        self.assertEqual(out["status"], "proposed")
        self.assertEqual(out["task_id"], self.tid)
        self.assertEqual(out["summary"], "fix it")
        self.assertEqual(
            out["files"],
            [
                {"path": "calc.py", "action": "modify", "attention": False},
                {"path": "new/util.py", "action": "create", "attention": False},
            ],
        )
        self.assertEqual(self.snapshot(self.repo), self.before)
        proposal = json.loads(Path(out["proposal_path"]).read_text())
        self.assertEqual([c["path"] for c in proposal["changes"]], ["calc.py", "new/util.py"])
        self.assertEqual(proposal["changes"][0]["content"], FIXED)
        self.assertIsNotNone(proposal["changes"][0]["base_sha256"])
        self.assertIsNone(proposal["changes"][1]["base_sha256"])
        diff = Path(out["diff_path"]).read_text()
        self.assertIn("-    return a - b", diff)
        self.assertIn("+    return a + b", diff)
        self.assertIn("--- /dev/null", diff)
        task = db.get_task(self.tid)
        self.assertEqual(task["status"], "proposed")
        self.assertTrue(task["proposal_sha256"])
        self.assertIn(BUGGY.strip().splitlines()[1], self.llm.user_prompt())  # full file was shown

    def test_review_pending_blocks_next_step(self):
        agent.add_step(self.pid, "Second", ["notes.txt"])
        self.propose()
        requests = len(self.llm.requests)
        with self.assertRaises(agent.ReviewPending):
            agent.execute_coding_step(self.pid)
        self.assertEqual(len(self.llm.requests), requests)
        self.assertEqual(len(db.tasks_with_status(self.pid, ["pending"])), 1)

    def test_empty_and_paused(self):
        db.transition(self.tid, ["pending"], "rejected")
        self.assertEqual(agent.execute_coding_step(self.pid)["status"], "empty")
        db.transition(self.tid, ["rejected"], "pending")
        self.set_memory(total_gib=8, available_mib=10)
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "paused")
        self.assertEqual(db.get_task(self.tid)["status"], "pending")
        self.assertEqual(self.llm.requests, [])

    def assert_failed(self, out, needle):
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any(needle in e for e in out["errors"]), out["errors"])
        task = db.get_task(self.tid)
        self.assertEqual(task["status"], "failed")
        self.assertIn(needle, task["error"])
        self.assertEqual(self.snapshot(self.repo), self.before)
        self.assertTrue((self.folder() / f"task_{self.tid}.rejected.json").exists())
        self.assertFalse((self.folder() / f"task_{self.tid}.proposal.json").exists())

    def test_invalid_python_syntax_fails(self):
        out = self.propose([{"path": "calc.py", "content": "def add(a, b)\n    return\n"}])
        self.assert_failed(out, "syntax error")

    def test_invalid_json_and_toml_fail(self):
        out = self.propose([{"path": "cfg.json", "content": "{nope"}])
        self.assert_failed(out, "invalid JSON")

    def test_path_escape_fails(self):
        for path in ("../evil.py", "/tmp/evil.py", ".git/hooks.py", ".env"):
            with self.subTest(path=path):
                db.retry_task(self.tid) if db.get_task(self.tid)["status"] == "failed" else None
                out = self.propose([{"path": path, "content": "x = 1\n"}])
                self.assertEqual(out["status"], "failed")
                self.assertEqual(self.snapshot(self.repo), self.before)
                self.assertFalse((self.tmp / "evil.py").exists())

    def test_blind_overwrite_refused(self):
        # notes.txt exists but was not listed for this step, so the model never saw it
        out = self.propose([{"path": "notes.txt", "content": "clobbered\n"}])
        self.assert_failed(out, "blind overwrite")

    def test_blind_overwrite_when_file_too_big_for_context(self):
        self.set_memory(total_gib=1, available_mib=800)
        big = "x = 1\n" * 1000  # 6000 chars > 2000 context budget
        (self.repo / "calc.py").write_text(big)
        self.before = self.snapshot(self.repo)
        self.llm.push(proposal_reply([{"path": "calc.py", "content": FIXED}]))
        out = agent.execute_coding_step(self.pid, 20)
        self.assert_failed(out, "blind overwrite")
        self.assertIn("Do NOT modify", self.llm.user_prompt())

    def test_no_change_and_bad_shapes_fail(self):
        for reply in (
            proposal_reply([{"path": "calc.py", "content": BUGGY}]),
            proposal_reply([]),
            proposal_reply([{"path": "a.py"}]),
            proposal_reply(["nope"]),
            proposal_reply([{"path": "a.py", "content": "x\x00y"}]),
            proposal_reply([{"path": "a.py", "content": "x = 1\n"}, {"path": "a.py", "content": "x = 2\n"}]),
            proposal_reply([{"path": f"f{i}.py", "content": "x = 1\n"} for i in range(11)]),
            Text("no json at all"),
        ):
            with self.subTest(reply=reply.text[:50]):
                if db.get_task(self.tid)["status"] == "failed":
                    db.retry_task(self.tid)
                self.llm.push(reply)
                out = agent.execute_coding_step(self.pid)
                self.assertEqual(out["status"], "failed")
                self.assertEqual(self.snapshot(self.repo), self.before)

    def test_llm_error_fails_step(self):
        self.llm.push(HttpError(500, "down"))
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "failed")
        self.assertIn("500", out["errors"][0])
        self.assertEqual(db.get_task(self.tid)["status"], "failed")

    def test_unterminated_content_gets_newline(self):
        out = self.propose([{"path": "calc.py", "content": FIXED.rstrip("\n")}])
        self.assertEqual(out["status"], "proposed")
        proposal = json.loads(Path(out["proposal_path"]).read_text())
        self.assertTrue(proposal["changes"][0]["content"].endswith("\n"))

    # ------------------------------------------------------------------ review

    def test_approve_applies_files_and_backs_up(self):
        out = self.propose()
        mode_before = (self.repo / "calc.py").stat().st_mode
        res = agent.approve(self.pid, out["task_id"])
        self.assertEqual(res["status"], "approved")
        self.assertEqual(sorted(res["written"]), ["calc.py", "new/util.py"])
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)
        self.assertEqual((self.repo / "calc.py").stat().st_mode, mode_before)
        self.assertEqual((self.repo / "new" / "util.py").read_text(), "X = 1\n")
        self.assertEqual((self.repo / "notes.txt").read_text(), "keep\n")
        backup = Path(res["backup_dir"])
        self.assertEqual((backup / "calc.py").read_text(), BUGGY)
        self.assertFalse((backup / "new" / "util.py").exists())
        self.assertEqual(db.get_task(self.tid)["status"], "approved")
        self.assertEqual([p.name for p in self.repo.rglob("*.tmp")], [])
        self.assertIsNone(db.busy_reason())

    def test_approve_create_only_has_no_backup_dir(self):
        self.llm.push(proposal_reply([{"path": "brand_new.py", "content": "Z = 1\n"}]))
        out = agent.execute_coding_step(self.pid)
        res = agent.approve(self.pid, out["task_id"])
        self.assertIsNone(res["backup_dir"])
        self.assertTrue((self.repo / "brand_new.py").exists())

    def test_approve_twice_refused(self):
        out = self.propose()
        agent.approve(self.pid, out["task_id"])
        with self.assertRaisesRegex(agent.AgentError, "only proposed"):
            agent.approve(self.pid, out["task_id"])

    def test_approve_refuses_tampered_proposal(self):
        out = self.propose()
        path = Path(out["proposal_path"])
        data = json.loads(path.read_text())
        data["changes"][0]["content"] = "import os\nos.system('evil')\n"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(agent.AgentError, "changed after it was generated"):
            agent.approve(self.pid, out["task_id"])
        with self.assertRaises(agent.AgentError):
            agent.load_proposal(self.pid, out["task_id"])
        self.assertEqual(self.snapshot(self.repo), self.before)
        self.assertEqual(db.get_task(self.tid)["status"], "proposed")
        self.assertIsNone(db.busy_reason())

    def test_approve_refuses_missing_proposal(self):
        out = self.propose()
        Path(out["proposal_path"]).unlink()
        with self.assertRaisesRegex(agent.AgentError, "missing"):
            agent.approve(self.pid, out["task_id"])

    def test_approve_refuses_when_target_changed(self):
        out = self.propose()
        (self.repo / "calc.py").write_text(BUGGY + "# user edit\n")
        edited = self.snapshot(self.repo)
        with self.assertRaisesRegex(agent.AgentError, "changed since"):
            agent.approve(self.pid, out["task_id"])
        self.assertEqual(self.snapshot(self.repo), edited)
        self.assertFalse((self.repo / "new").exists())
        self.assertEqual(db.get_task(self.tid)["status"], "proposed")

    def test_approve_refuses_when_target_already_has_new_content(self):
        out = self.propose([{"path": "calc.py", "content": FIXED}])
        (self.repo / "calc.py").write_text(FIXED)
        with self.assertRaises(agent.AgentError):
            agent.approve(self.pid, out["task_id"])

    def test_approve_refuses_when_new_file_appeared(self):
        out = self.propose()
        (self.repo / "new").mkdir()
        (self.repo / "new" / "util.py").write_text("OTHER = 1\n")
        with self.assertRaises(agent.AgentError):
            agent.approve(self.pid, out["task_id"])
        self.assertEqual((self.repo / "new" / "util.py").read_text(), "OTHER = 1\n")
        self.assertEqual((self.repo / "calc.py").read_text(), BUGGY)

    def test_approve_wrong_project_or_status(self):
        other = agent.create_coding_project("other", str(self.repo))
        out = self.propose()
        with self.assertRaisesRegex(agent.AgentError, "does not belong"):
            agent.approve(other, out["task_id"])
        with self.assertRaises(db.NotFound):
            agent.approve(self.pid, 999)

    def test_approve_rolls_back_on_write_failure(self):
        out = self.propose()
        real_replace = os.replace
        calls = []

        def flaky(src, dst, *a, **k):
            calls.append(dst)
            if str(dst).endswith("util.py"):
                raise OSError("disk full")
            return real_replace(src, dst, *a, **k)

        with mock.patch("backend.agent.os.replace", side_effect=flaky):
            with self.assertRaises(OSError):
                agent.approve(self.pid, out["task_id"])
        self.assertEqual((self.repo / "calc.py").read_text(), BUGGY)
        self.assertFalse((self.repo / "new" / "util.py").exists())
        self.assertEqual(db.get_task(self.tid)["status"], "proposed")
        self.assertEqual([p.name for p in self.repo.rglob("*.tmp")], [])

    def test_reject_leaves_repo_untouched(self):
        out = self.propose()
        task = agent.reject(self.pid, out["task_id"], "  not good ")
        self.assertEqual(task["status"], "rejected")
        self.assertEqual(task["error"], "Rejected by user: not good")
        self.assertEqual(self.snapshot(self.repo), self.before)
        with self.assertRaises(agent.AgentError):
            agent.approve(self.pid, out["task_id"])
        with self.assertRaises(agent.AgentError):
            agent.reject(self.pid, out["task_id"])

    def test_reject_without_reason(self):
        out = self.propose()
        self.assertEqual(agent.reject(self.pid, out["task_id"])["error"], "Rejected by user")

    def test_reject_pending_task_refused(self):
        with self.assertRaises(agent.AgentError):
            agent.reject(self.pid, self.tid)

    def test_next_step_after_approval_mentions_history(self):
        agent.add_step(self.pid, "Second step", [])
        out = self.propose()
        agent.approve(self.pid, out["task_id"])
        self.llm.push(proposal_reply([{"path": "second.py", "content": "S = 1\n"}]))
        out2 = agent.execute_coding_step(self.pid)
        self.assertEqual(out2["status"], "proposed")
        self.assertIn("Fix add", self.llm.user_prompt().split("Previously approved steps:")[1].split("\n")[0])


def need_files(*paths):
    return Text("<think>let me look</think>" + json.dumps({"need_files": list(paths)}))


class ExplorationTests(LowRAMTestCase):
    """The model may ask for more files (need_files) before it proposes."""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(
            {"calc.py": BUGGY, "helpers.py": "def helper():\n    return 'HELPER-CONTENT'\n", "notes.txt": "NOTES\n"}
        )
        self.pid = agent.create_coding_project("Fix add", str(self.repo))
        self.tid = agent.add_step(self.pid, "Fix add", ["calc.py"], "use plus")
        self.before = self.snapshot(self.repo)

    def messages(self, index):
        return self.llm.requests[index]["json"]["messages"]

    def test_need_files_then_proposal(self):
        self.llm.push(
            need_files("helpers.py", "missing.py", "calc.py"),
            proposal_reply([{"path": "helpers.py", "content": "def helper():\n    return 1\n"},
                            {"path": "calc.py", "content": FIXED}]),
        )
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "proposed", out)
        self.assertEqual(out["explored_files"], ["helpers.py"])
        self.assertEqual([f["path"] for f in out["files"]], ["helpers.py", "calc.py"])  # helpers.py was shown in full
        self.assertEqual(len(self.llm.requests), 2)
        first, second = self.messages(0), self.messages(1)
        self.assertIn('"need_files"', first[-1]["content"])  # the option is explained
        self.assertNotIn("HELPER-CONTENT", first[-1]["content"])
        self.assertEqual(second[:2], first)  # same prefix: the engine can reuse its prompt cache
        self.assertEqual(
            second[2], {"role": "assistant", "content": json.dumps({"need_files": ["helpers.py", "missing.py", "calc.py"]})}
        )
        follow_up = second[3]["content"]
        self.assertIn("### helpers.py\n```\ndef helper():\n    return 'HELPER-CONTENT'\n", follow_up)
        self.assertIn("### missing.py\n(does not exist)", follow_up)
        self.assertIn("### calc.py\n(already shown above)", follow_up)
        raw = (self.home / "artifacts" / f"project_{self.pid}" / f"task_{self.tid}.raw.txt").read_text()
        self.assertIn("===== model answer 2 =====", raw)
        self.assertEqual(self.snapshot(self.repo), self.before)

    def test_two_extra_rounds_and_bad_requests(self):
        self.llm.push(
            need_files(*[f"f{i}.py" for i in range(8)]),
            Text(json.dumps({"need_files": "notes.txt"})),
            proposal_reply([{"path": "calc.py", "content": FIXED}]),
        )
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "proposed", out)
        self.assertEqual(out["explored_files"], [])
        round1, round2 = self.llm.user_prompt(1), self.llm.user_prompt(2)
        self.assertIn("### f5.py", round1)
        self.assertNotIn("f6.py", round1)
        self.assertIn(f"only the first {agent.MAX_NEED_FILES} requested files", round1)
        self.assertIn("1 request(s) left", round1)
        self.assertIn("need_files must be a list", round2)
        self.assertIn("You cannot request more files", round2)

    def test_too_many_rounds_fail(self):
        self.llm.push(need_files("helpers.py"), need_files("notes.txt"), need_files("calc.py"))
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "failed")
        self.assertEqual(len(self.llm.requests), 3)
        self.assertIn("asked for more files again after 2 extra rounds", out["errors"][0])
        self.assertEqual(out["explored_files"], ["helpers.py", "notes.txt"])
        task = db.get_task(self.tid)
        self.assertEqual(task["status"], "failed")
        self.assertIn("asked for more files", task["error"])
        self.assertIn("===== model answer 3 =====", Path(out["raw_output"]).read_text())
        self.assertEqual(self.snapshot(self.repo), self.before)

    def test_path_escape_in_need_files_is_reported_not_loaded(self):
        (self.tmp / "outside.py").write_text("OUTSIDE = 'TOP-SECRET'\n")
        (self.repo / ".env").write_text("TOKEN=ENV-SECRET\n")
        (self.repo / "link.py").symlink_to(self.tmp / "outside.py")
        bad = ["../outside.py", str(self.tmp / "outside.py"), ".env", "link.py", "~/outside.py", 7]
        self.llm.push(need_files(*bad), proposal_reply([{"path": "calc.py", "content": FIXED}]))
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "proposed", out)
        self.assertEqual(out["explored_files"], [])
        self.assertEqual(self.llm.user_prompt(1).count("(not available:"), len(bad))
        for request in self.llm.requests:
            text = json.dumps(request["json"])
            self.assertNotIn("TOP-SECRET", text)
            self.assertNotIn("ENV-SECRET", text)

    def test_requested_file_too_large_is_not_shown_and_not_writable(self):
        self.set_memory(total_gib=1, available_mib=800)  # 20 % -> 2000 characters for files
        (self.repo / "big.py").write_text("x = 1\n" * 1000)
        self.before = self.snapshot(self.repo)
        self.llm.push(need_files("big.py"), proposal_reply([{"path": "big.py", "content": "x = 2\n"}]))
        out = agent.execute_coding_step(self.pid, 20)
        self.assertEqual(out["status"], "failed")
        self.assertIn("not shown – too large", self.llm.user_prompt(1))
        self.assertNotIn("x = 1\nx = 1", self.llm.user_prompt(1))
        self.assertEqual(out["explored_files"], [])
        self.assertTrue(any("blind overwrite" in e for e in out["errors"]), out["errors"])
        self.assertEqual(self.snapshot(self.repo), self.before)


if __name__ == "__main__":
    unittest.main()
