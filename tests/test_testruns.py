"""Test & fix loop: command validation, background runs, time limit, env scrubbing, fix step."""

import json
import os
import shutil
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import psutil

from backend import agent, config, db
from tests.helpers import LowRAMTestCase, Text, wait_until

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST_FILE = "from calc import add\n\nassert add(1, 2) == 3\n"
REPORT_SCRIPT = (
    "import os, sys\n"
    "print('Ran 1 test')\n"
    "print(f'  File \"{os.getcwd()}/calc.py\", line 2, in add')\n"
    "print('FAIL: tests/test_calc.py:3: AssertionError')\n"
    "print('also ../outside.py /etc/hosts .env missing.py node_modules/x.js')\n"
    "sys.exit(1)\n"
)
POSIX_PY = os.name == "posix" and shutil.which("python3") is not None


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


class TestRunCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(
            {"calc.py": BUGGY, "tests/test_calc.py": TEST_FILE, "report.py": REPORT_SCRIPT, ".env": "TOKEN=x\n"}
        )
        self.pid = agent.create_coding_project("Fix add", str(self.repo))
        agent.recover_test_runs()  # like app startup: clears the shutdown flag of earlier tests
        self.addCleanup(agent.stop_test_runs)

    def executable(self, rel: str, text: str = "#!/bin/sh\necho hi\n", mode: int = 0o755):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def start(self, command: str) -> dict:
        agent.set_test_command(self.pid, command)
        run = agent.start_test_run(self.pid)
        self.assertEqual(run["state"], "running")
        return run

    def finish(self, run: dict, timeout: float = 20) -> dict:
        self.assertTrue(
            wait_until(lambda: agent.get_test_run(self.pid, run["id"])["state"] != "running", timeout),
            "test run did not finish",
        )
        return agent.get_test_run(self.pid, run["id"])

    def run_command(self, command: str) -> dict:
        return self.finish(self.start(command))


@unittest.skipUnless(POSIX_PY, "needs python3 on PATH, symlinks and POSIX file modes")
class ValidationTests(TestRunCase):
    def test_valid_commands(self):
        args, exe = agent.validate_test_command(self.repo, "  python3 -m unittest -v 'a b'  ")
        self.assertEqual(args, ["python3", "-m", "unittest", "-v", "a b"])
        self.assertTrue(os.path.isabs(exe))
        script = self.executable("scripts/run_tests.sh")
        self.assertEqual(agent.validate_test_command(self.repo, "./scripts/run_tests.sh --fast")[1], str(script))
        self.assertEqual(agent.validate_test_command(self.repo, "scripts/run_tests.sh")[1], str(script))
        inner_link = self.repo / "run"
        inner_link.symlink_to(script)  # a symlink that stays inside the repository is fine
        self.assertEqual(agent.validate_test_command(self.repo, "./run")[1], str(script))
        self.assertEqual(len(agent.validate_test_command(self.repo, "python3" + " a" * 63)[0]), 64)

    def test_rejected_commands(self):
        outside = self.tmp / "x"
        outside.write_text("#!/bin/sh\necho outside\n")
        outside.chmod(0o755)
        (self.repo / "escape").symlink_to(outside)
        self.executable("not_exec.sh", mode=0o644)
        bad = {
            "absolute path outside the repo": "/bin/sh -c id",
            "absolute path into the repo": f"{self.repo}/report.py",
            "parent directory": "../x",
            "parent directory via ./": "./../x",
            "home path": "~/x",
            "symlink escape": "./escape",
            "missing script": "./missing.sh",
            "not executable": "./not_exec.sh",
            "unknown binary": "definitely-not-a-real-program-xyz --help",
            "newline": "python3 -c 1\nrm -rf ~",
            "carriage return": "python3 -c 1\rrm",
            "NUL": "python3\x00 -c 1",
            "hidden character": "python3 ‮-c 1",
            "too long": "python3 " + "a" * 300,
            "too many arguments": "python3" + " a" * 64,
            "unbalanced quote": "python3 -c \"print(1)",
            "empty program": '"" -c 1',
            "empty": "   ",
            "not text": 5,
            "missing": None,
        }
        for label, command in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                agent.validate_test_command(self.repo, command)
        with self.assertRaises(ValueError):
            agent.set_test_command(self.pid, "../x")
        self.assertIsNone(agent.get_test_command(self.pid))

    def test_command_is_stored_per_project(self):
        other = agent.create_coding_project("Other", str(self.repo))
        self.assertEqual(
            agent.set_test_command(self.pid, "  python3 -m unittest  "), {"command": "python3 -m unittest", "sandbox": True}
        )
        self.assertEqual(agent.set_test_command(self.pid, "python3 -m pytest", False)["sandbox"], False)
        self.assertEqual(agent.set_test_command(self.pid, "python3 -m pytest"), {"command": "python3 -m pytest", "sandbox": False})
        self.assertEqual(agent.get_test_command(self.pid), "python3 -m pytest")
        with self.assertRaises(ValueError):
            agent.set_test_command(self.pid, "python3 -m pytest", "yes")
        self.assertEqual(agent.get_test_settings(other), {"command": None, "sandbox": True})
        self.assertIsNone(agent.get_test_command(other))
        with self.assertRaises(agent.AgentError):
            agent.start_test_run(other)  # no command saved


@unittest.skipUnless(POSIX_PY, "needs python3 on PATH and POSIX process groups")
class RunTests(TestRunCase):
    def test_passing_and_failing_runs(self):
        run = self.run_command("python3 -c \"import os, sys; print('hello', os.getcwd()); sys.stderr.write('err\\n')\"")
        self.assertEqual((run["state"], run["exit_code"], run["error"]), ("passed", 0, None))
        self.assertIn(f"hello {self.repo}", run["output_tail"])  # cwd = repository
        self.assertIn("err", run["output_tail"])  # stderr is merged
        self.assertTrue(run["started_at"] and run["finished_at"])
        run = self.run_command("python3 -c \"import sys; print('boom'); sys.exit(3)\"")
        self.assertEqual((run["state"], run["exit_code"]), ("failed", 3))
        self.assertIn("boom", run["output_tail"])
        self.assertEqual(agent.latest_test_run(self.pid)["id"], run["id"])

    def test_output_tail_is_capped_and_cleaned(self):
        run = self.run_command(
            "python3 -c \"print('\\x1b[31mRED\\x1b[0m'); [print('line', i) for i in range(5000)]; print('END')\""
        )
        tail = run["output_tail"]
        self.assertLessEqual(len(tail), agent.TEST_OUTPUT_TAIL_CHARS)
        self.assertTrue(tail.startswith("line "), tail[:40])  # starts at a line boundary
        self.assertTrue(tail.endswith("line 4999\nEND\n"))
        self.assertNotIn("RED", tail)  # was at the start, cut off
        run = self.run_command("python3 -c \"print('\\x1b[31mRED\\x1b[0m done')\"")
        self.assertEqual(run["output_tail"], "RED done\n")

    def test_environment_without_secrets(self):
        secrets = {"MY_API_KEY": "s3cret-1", "GITHUB_TOKEN": "s3cret-2", "DB_PASSWORD": "s3cret-3",
                   "aws_secret_access_key": "s3cret-4", "LOWRAM_VISIBLE": "visible", "CI": "0"}
        with mock.patch.dict(os.environ, secrets):
            run = self.run_command(
                "python3 -c \"import os; print(sorted(os.environ.items()));"
                " print('|'.join(os.environ.get(k, '-') for k in ('MY_API_KEY', 'LOWRAM_VISIBLE', 'CI')))\""
            )
        self.assertEqual(run["state"], "passed")
        self.assertIn("-|visible|1", run["output_tail"])
        self.assertNotIn("s3cret", run["output_tail"])

    def test_timeout_kills_the_process_group(self):
        script = (
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
            "print(child.pid, flush=True); time.sleep(30)"
        )
        with mock.patch.object(agent, "TEST_TIMEOUT_S", 0.8):
            run = self.start(f'python3 -c "{script}"')
        run = self.finish(run, timeout=20)
        self.assertEqual(run["state"], "timeout")
        self.assertIn("time limit", run["error"])
        self.assertLess(run["exit_code"], 0)  # killed by a signal
        grandchild = int(run["output_tail"].split()[0])
        self.assertTrue(wait_until(lambda: not alive(grandchild), 10), "child of the test command survived")

    def test_one_run_at_a_time_and_cancel(self):
        run = self.start("python3 -c \"import time; print('started', flush=True); time.sleep(30)\"")
        with self.assertRaises(db.Busy):
            agent.start_test_run(self.pid)
        self.assertTrue(wait_until(lambda: "started" in agent.get_test_run(self.pid, run["id"])["output_tail"]))
        run = agent.cancel_test_run(self.pid, run["id"])
        self.assertEqual((run["state"], run["error"]), ("error", agent.CANCELLED_RUN))
        with self.assertRaises(agent.AgentError):
            agent.cancel_test_run(self.pid, run["id"])  # already finished
        self.assertEqual(self.run_command("python3 -c pass")["state"], "passed")  # free again

    def test_start_revalidates_and_reports_missing_runs(self):
        script = self.executable("run.sh", "#!/bin/sh\necho script-ran\n")
        self.assertEqual(self.run_command("./run.sh")["output_tail"], "script-ran\n")
        script.chmod(0o644)
        with self.assertRaises(ValueError):
            agent.start_test_run(self.pid)
        with self.assertRaises(db.NotFound):
            agent.get_test_run(self.pid, 9999)
        other = agent.create_coding_project("Other", str(self.repo))
        run_id = agent.latest_test_run(self.pid)["id"]
        with self.assertRaises(db.NotFound):
            agent.get_test_run(other, run_id)
        self.assertIsNone(agent.latest_test_run(other))

    def test_recover_marks_leftover_runs_but_not_active_ones(self):
        old_project = agent.create_coding_project("Old", str(self.repo))
        with db.connect() as conn, db.transaction(conn):
            leftover = conn.execute(
                "INSERT INTO coding_test_runs (project_id, command, state, started_at) "
                "VALUES (?, 'python3 -m pytest', 'running', ?)",
                (old_project, db.utcnow()),
            ).lastrowid
        active = self.start("python3 -c \"import time; time.sleep(30)\"")
        with self.assertLogs("lowram.agent", "WARNING"):
            self.assertEqual(agent.recover_test_runs(), 1)
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM coding_test_runs WHERE id = ?", (leftover,)).fetchone()
        self.assertEqual((row["state"], row["error"]), ("error", agent.INTERRUPTED_RUN))
        self.assertIn("interrupted", row["error"])
        self.assertEqual(agent.get_test_run(self.pid, active["id"])["state"], "running")
        agent.stop_test_runs()
        self.assertEqual(agent.get_test_run(self.pid, active["id"])["error"], agent.INTERRUPTED_RUN)


@unittest.skipUnless(POSIX_PY, "needs python3 on PATH and POSIX process groups")
class FixTestsTests(TestRunCase):
    def test_fix_step_content(self):
        run = self.run_command("python3 report.py")
        self.assertEqual(run["state"], "failed")
        task_id = agent.add_fix_tests_step(self.pid, run["id"])
        task = db.get_task(task_id)
        self.assertEqual(task["title"], agent.FIX_TESTS_TITLE)
        self.assertEqual(task["title"], "Fehlschlagende Tests beheben")
        step = json.loads(task["detail"])
        # mentioned and existing inside the repo; ../outside.py, /etc/hosts, .env, missing files ignored
        self.assertEqual(step["files"], ["tests/test_calc.py", "calc.py"])
        self.assertIn("Testbefehl: python3 report.py", step["detail"])
        self.assertIn("Exit-Code: 1", step["detail"])
        self.assertIn("FAIL: tests/test_calc.py:3: AssertionError", step["detail"])

    def test_long_output_is_truncated_and_files_capped(self):
        names = [f"mod{i}.py" for i in range(8)]
        for name in names:
            (self.repo / name).write_text("X = 1\n")
        printer = "; ".join(
            ["import sys", "[print('noise', i) for i in range(2000)]", f"print({' '.join(names)!r})", "sys.exit(2)"]
        )
        run = self.run_command(f'python3 -c "{printer}"')
        task = db.get_task(agent.add_fix_tests_step(self.pid, run["id"]))
        step = json.loads(task["detail"])
        self.assertEqual(len(step["files"]), agent.FIX_TESTS_MAX_FILES)
        self.assertEqual(step["files"], list(reversed(names))[:6])
        output_part = step["detail"].split("Testausgabe (Ende):\n", 1)[1]
        self.assertLessEqual(len(output_part), agent.FIX_TESTS_OUTPUT_CHARS)
        self.assertTrue(output_part.startswith("noise "))
        self.assertNotIn("noise 0\n", output_part)
        self.assertIn("Exit-Code: 2", step["detail"])

    def test_only_failed_or_timed_out_runs(self):
        run = self.run_command("python3 -c pass")
        with self.assertRaises(agent.AgentError):
            agent.add_fix_tests_step(self.pid, run["id"])
        with self.assertRaises(db.NotFound):
            agent.add_fix_tests_step(self.pid, 9999)
        running = self.start("python3 -c \"import time; time.sleep(30)\"")
        with self.assertRaises(agent.AgentError):
            agent.add_fix_tests_step(self.pid, running["id"])
        self.assertEqual(db.list_tasks(self.pid), [])


class SandboxProfileTests(LowRAMTestCase):
    HOME = Path("/Users/me")

    def profile(self, repo="/Users/me/code/app", home=HOME):
        return agent.sandbox_profile(Path(repo), home=Path(home), data_dir=Path("/Users/me/.lowram-ai"))

    def test_profile(self):
        profile = self.profile()
        self.assertTrue(profile.startswith("(version 1)\n(allow default)\n"))
        for path in ("/Users/me/code/app", "/private/tmp", "/private/var/folders", "/dev"):
            self.assertIn(f'    (subpath "{path}")', profile.split("(allow file-write*")[1].split(")\n(deny")[0])
        secrets = profile.split("(deny file-read* file-write*")[1]
        for rel in (".ssh", ".aws", ".gnupg", ".config/gh", ".netrc", "Library/Keychains", ".lowram-ai"):
            self.assertIn(f'(subpath "/Users/me/{rel}")', secrets)
        self.assertEqual(profile.count('"/Users/me/.lowram-ai"'), 1)  # data folder listed once
        self.assertIn("(deny network-outbound)", profile)
        self.assertIn('(allow network-outbound (remote ip "localhost:*"))', profile)
        self.assertIn("(allow network-outbound (remote unix-socket))", profile)
        # later rules win: write-deny, then the writable folders, then the secret folders
        order = [profile.index(rule) for rule in ("(deny file-write*)", "(allow file-write*", "(deny file-read*")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(profile.count("("), profile.count(")"))

    def test_paths_needing_escapes_are_refused(self):
        for repo in ('/Users/me/co"de', "/Users/me/back\\slash", "/Users/me/new\nline", "relative/app"):
            with self.subTest(repo=repo), self.assertRaises(agent.AgentError):
                self.profile(repo)
        with self.assertRaises(agent.AgentError):
            self.profile(home='/Users/"me')

    def test_sandbox_command(self):
        repo = self.make_repo()
        args = ["python3", "-m", "pytest", "-q"]
        with mock.patch.object(agent, "_sandbox_state", return_value="ok"):
            popen_args, exe, sandboxed, warning = agent.sandbox_command(repo, args, "/usr/bin/python3", True)
            self.assertEqual(popen_args[:2], [agent.SANDBOX_EXEC, "-p"])
            self.assertIn(f'(subpath "{repo}")', popen_args[2])
            self.assertEqual(popen_args[3:], ["--", "/usr/bin/python3", "-m", "pytest", "-q"])
            self.assertEqual((exe, sandboxed, warning), (agent.SANDBOX_EXEC, True, None))
            self.assertEqual(
                agent.sandbox_command(repo, args, "/x", False), (args, "/x", False, agent.SANDBOX_OFF_WARNING)
            )
        with mock.patch.object(agent, "_sandbox_state", return_value="unsupported"):
            self.assertEqual(
                agent.sandbox_command(repo, args, "/x", True), (args, "/x", False, agent.SANDBOX_UNAVAILABLE_WARNING)
            )
        with mock.patch.object(agent, "_sandbox_state", return_value="missing"):
            self.assertEqual(agent.sandbox_command(repo, args, "/x", True)[2:], (False, agent.SANDBOX_MISSING_WARNING))

    @unittest.skipIf(sys.platform == "darwin", "macOS has sandbox-exec")
    def test_no_sandbox_outside_macos(self):
        self.assertEqual(agent._sandbox_state(), "unsupported")


SANDBOX_PROBE = """
import json, socket, sys

def attempt(action):
    try:
        action()
        return "ok"
    except OSError as exc:
        return f"{type(exc).__name__}:{exc.errno}"

def write(path):
    with open(path, "w") as handle:
        handle.write("x")

def read(path):
    with open(path) as handle:
        handle.read()

def connect(host, port):
    socket.create_connection((host, port), timeout=5).close()

repo, outside, secret, data, port = sys.argv[1:6]
print(json.dumps({
    "repo": attempt(lambda: write(repo + "/written.txt")),
    "outside": attempt(lambda: write(outside)),
    "secret": attempt(lambda: read(secret)),
    "lowram_data": attempt(lambda: read(data)),
    "internet": attempt(lambda: connect("1.1.1.1", 443)),
    "localhost": attempt(lambda: connect("127.0.0.1", int(port))),
}))
"""


@unittest.skipUnless(sys.platform == "darwin" and os.access(agent.SANDBOX_EXEC, os.X_OK), "needs macOS sandbox-exec")
class RealSandboxTests(LowRAMTestCase):
    """On a real Mac (the macOS CI runners): the test-run sandbox blocks what the README promises."""

    def test_sandbox_blocks_home_secrets_app_data_and_internet(self):
        import pwd
        import socket
        import subprocess
        import tempfile

        base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        repo, home, data = base / "repo", base / "home", base / "lowram-data"
        repo.mkdir()
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_test").write_text("secret key")
        data.mkdir()
        (data / "settings.json").write_text("{}")
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()  # the tests point HOME at a temp folder
        outside = real_home / f".lowram-sandbox-probe-{os.getpid()}"
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        profile = agent.sandbox_profile(repo, home=home, data_dir=data)
        result = subprocess.run(
            [agent.SANDBOX_EXEC, "-p", profile, "--", sys.executable, "-I", "-c", SANDBOX_PROBE, str(repo), str(outside),
             str(home / ".ssh" / "id_test"), str(data / "settings.json"), str(server.getsockname()[1])],
            capture_output=True, text=True, timeout=60, cwd=repo,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        denied = "PermissionError:1"  # EPERM: "Operation not permitted" from the sandbox
        self.assertEqual(seen, {"repo": "ok", "outside": denied, "secret": denied, "lowram_data": denied,
                                "internet": denied, "localhost": "ok"})
        self.assertEqual((repo / "written.txt").read_text(), "x")
        self.assertFalse(outside.exists())


@unittest.skipUnless(POSIX_PY, "needs python3 on PATH and POSIX process groups")
class RunRecordTests(TestRunCase):
    @unittest.skipIf(sys.platform == "darwin", "runs are sandboxed on macOS")
    def test_runs_outside_macos_are_not_sandboxed_and_say_so(self):
        run = self.run_command("python3 -c pass")
        self.assertEqual((run["sandboxed"], run["warning"]), (False, agent.SANDBOX_UNAVAILABLE_WARNING))
        self.assertNotIn("ai_writes_seen", run)
        agent.set_test_command(self.pid, "python3 -c pass", False)
        run = self.finish(agent.start_test_run(self.pid))
        self.assertEqual((run["sandboxed"], run["warning"]), (False, agent.SANDBOX_OFF_WARNING))

    def test_only_the_newest_runs_are_kept(self):
        other = agent.create_coding_project("Other", str(self.repo))
        with db.connect() as conn, db.transaction(conn):
            for project in (self.pid, other):
                for _ in range(25):
                    conn.execute(
                        "INSERT INTO coding_test_runs (project_id, command, state, started_at, finished_at) "
                        "VALUES (?, 'x', 'passed', ?, ?)",
                        (project, db.utcnow(), db.utcnow()),
                    )
        run = self.run_command("python3 -c pass")
        with db.connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM coding_test_runs WHERE project_id = ? ORDER BY id", (self.pid,))]
            others = conn.execute("SELECT COUNT(*) FROM coding_test_runs WHERE project_id = ?", (other,)).fetchone()[0]
        self.assertEqual(len(ids), agent.MAX_TEST_RUNS_KEPT)
        self.assertEqual(ids[-1], run["id"])
        self.assertEqual(others, 25)  # other projects are pruned only when their own runs finish

    def test_no_new_runs_after_shutdown(self):
        agent.set_test_command(self.pid, "python3 -c pass")
        agent.stop_test_runs()
        with self.assertRaises(db.Busy) as ctx:
            agent.start_test_run(self.pid)
        self.assertEqual(str(ctx.exception), "LowRAM AI wird beendet")
        self.assertIsNone(agent.latest_test_run(self.pid))
        agent.recover_test_runs()  # next startup
        self.assertEqual(self.finish(agent.start_test_run(self.pid))["state"], "passed")


@unittest.skipUnless(POSIX_PY, "needs python3 on PATH and POSIX process groups")
class ConfirmationTests(TestRunCase):
    def approve(self, changes):
        existing = [c["path"] for c in changes if (self.repo / c["path"]).exists()]
        agent.add_step(self.pid, "AI change", existing)
        self.llm.push(Text(json.dumps({"summary": "change", "changes": changes})))
        out = agent.execute_coding_step(self.pid)
        self.assertEqual(out["status"], "proposed", out)
        agent.approve(self.pid, out["task_id"])
        return out

    def test_ai_written_test_files_need_confirmation(self):
        agent.set_test_command(self.pid, "python3 -c pass")
        self.finish(agent.start_test_run(self.pid))  # nothing written by the AI yet
        out = self.approve([{"path": "calc.py", "content": FIXED}])
        self.assertEqual(out["attention"], [])
        self.finish(agent.start_test_run(self.pid))  # calc.py is not run directly by the test runner
        out = self.approve(
            [
                {"path": "tests/test_calc.py", "content": TEST_FILE + "assert add(2, 2) == 4\n"},
                {"path": "run_all.sh", "content": "echo hi\n"},
                {"path": "calc.py", "content": FIXED + "# checked\n"},
            ]
        )
        self.assertEqual(sorted(out["attention"]), ["run_all.sh", "tests/test_calc.py"])
        last = agent.latest_test_run(self.pid)["id"]
        with self.assertRaises(agent.ConfirmationRequired) as ctx:
            agent.start_test_run(self.pid)
        self.assertEqual(ctx.exception.files, ["run_all.sh", "tests/test_calc.py"])
        self.assertIn("Die KI hat diese Dateien geschrieben", str(ctx.exception))
        self.assertEqual(agent.latest_test_run(self.pid)["id"], last)  # nothing started
        self.assertEqual(self.finish(agent.start_test_run(self.pid, confirm=True))["state"], "passed")
        self.assertEqual(agent.unconfirmed_ai_files(self.pid)[0], [])
        self.finish(agent.start_test_run(self.pid))  # confirmed once: no new AI files since


OLD_SCHEMA = """
CREATE TABLE coding_test_commands (project_id INTEGER PRIMARY KEY, command TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE coding_test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL, command TEXT NOT NULL, state TEXT NOT NULL,
    exit_code INTEGER, output_tail TEXT NOT NULL DEFAULT '', error TEXT, started_at TEXT NOT NULL, finished_at TEXT
);
INSERT INTO coding_test_commands VALUES (1, 'python3 -m pytest', 'then');
INSERT INTO coding_test_runs (project_id, command, state, started_at) VALUES (1, 'python3 -m pytest', 'passed', 'then');
"""


class MigrationTests(LowRAMTestCase):
    def test_tables_of_earlier_builds_get_the_new_columns(self):
        with closing(sqlite3.connect(config.db_path())) as raw:
            raw.executescript(OLD_SCHEMA)
        pid = agent.create_coding_project("Old", str(self.make_repo({"a.py": "x = 1\n"})))
        self.assertEqual(pid, 1)
        self.assertEqual(agent.get_test_settings(pid), {"command": "python3 -m pytest", "sandbox": True})
        run = agent.latest_test_run(pid)
        self.assertEqual((run["state"], run["sandboxed"], run["warning"]), ("passed", False, None))
        self.assertEqual(agent.unconfirmed_ai_files(pid), ([], 0))


if __name__ == "__main__":
    unittest.main()
