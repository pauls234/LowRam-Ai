import socket
import subprocess
import sys
import unittest

from backend import config, db
from tests.helpers import LowRAMTestCase


def dead_owner() -> str:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return f"{socket.gethostname()}:{proc.pid}"


class ProjectTests(LowRAMTestCase):
    def test_create_list_counts(self):
        a = db.create_project("A", "general", tasks=[("t1", ""), ("t2", "")])
        b = db.create_project("B", "coding", goal="g", repo_path="/x")
        projects = db.list_projects()
        self.assertEqual([p["id"] for p in projects], [b, a])  # newest first
        pa = db.get_project(a)
        self.assertEqual(pa["total"], 2)
        self.assertEqual(pa["counts"]["pending"], 2)
        self.assertEqual(pa["counts"]["done"], 0)
        self.assertEqual(db.get_project(b)["total"], 0)
        self.assertEqual(db.get_project(b)["repo_path"], "/x")

    def test_unknown_kind_and_not_found(self):
        with self.assertRaises(ValueError):
            db.create_project("x", "weird")
        with self.assertRaises(db.NotFound):
            db.get_project(99)
        with self.assertRaises(db.NotFound):
            db.list_tasks(99)
        with self.assertRaises(db.NotFound):
            db.get_task(99)
        with self.assertRaises(db.NotFound):
            db.add_tasks(99, [("t", "")])

    def test_add_tasks_appends_positions(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        db.add_tasks(p, [("t2", ""), ("t3", "")])
        self.assertEqual([t["position"] for t in db.list_tasks(p)], [1, 2, 3])
        self.assertEqual([t["title"] for t in db.list_tasks(p)], ["t1", "t2", "t3"])

    def test_empty_list(self):
        self.assertEqual(db.list_projects(), [])

    def test_tasks_with_status_ignores_unknown(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        self.assertEqual(db.tasks_with_status(p, ["bogus"]), [])
        self.assertEqual(len(db.tasks_with_status(p, ["pending", "bogus"])), 1)

    def test_newer_schema_refused(self):
        db.create_project("A", "general")
        with db.connect() as conn:
            conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            db.list_projects()

    def test_database_lives_in_isolated_home(self):
        db.create_project("A", "general")
        self.assertEqual(config.db_path(), self.home / "lowram.db")
        self.assertTrue((self.home / "lowram.db").exists())


class ClaimTests(LowRAMTestCase):
    def test_claim_ordering_and_attempts(self):
        p = db.create_project("A", "general", tasks=[("t1", ""), ("t2", "")])
        first = db.claim_next_task(p)
        self.assertEqual(first["title"], "t1")
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["lease_owner"], db.owner_id())
        db.finish_task(first["id"], "done", result_path="/r")
        self.assertEqual(db.claim_next_task(p)["title"], "t2")

    def test_claim_empty_returns_none(self):
        p = db.create_project("A", "general")
        self.assertIsNone(db.claim_next_task(p))

    def test_claim_unknown_project(self):
        with self.assertRaises(db.NotFound):
            db.claim_next_task(123)

    def test_busy_when_task_running_in_any_project(self):
        a = db.create_project("A", "general", tasks=[("t1", ""), ("t2", "")])
        b = db.create_project("B", "general", tasks=[("u1", "")])
        db.claim_next_task(a)
        with self.assertRaises(db.Busy):
            db.claim_next_task(a)
        with self.assertRaises(db.Busy):
            db.claim_next_task(b)
        self.assertIn("already running", db.busy_reason())

    def test_busy_when_inference_slot_held(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        with db.inference_slot("planning"):
            self.assertIn("planning", db.busy_reason())
            with self.assertRaises(db.Busy):
                db.claim_next_task(p)
            with self.assertRaises(db.Busy):
                with db.inference_slot("second"):
                    pass
        self.assertIsNone(db.busy_reason())
        self.assertIsNotNone(db.claim_next_task(p))

    def test_slot_released_on_exception(self):
        with self.assertRaises(KeyError):
            with db.inference_slot("x"):
                raise KeyError("boom")
        self.assertIsNone(db.busy_reason())

    def test_slot_refused_while_task_running(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        db.claim_next_task(p)
        with self.assertRaises(db.Busy):
            with db.inference_slot("x"):
                pass


class FinishTransitionTests(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.p = db.create_project("A", "general", tasks=[("t1", "")])
        self.task = db.get_task(db.list_tasks(self.p)[0]["id"])

    def test_finish_only_from_running(self):
        with self.assertRaises(db.InvalidTransition):
            db.finish_task(self.task["id"], "done")
        db.claim_next_task(self.p)
        done = db.finish_task(self.task["id"], "done", result_path="/r")
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["has_result"])
        self.assertIsNone(done["lease_owner"])
        with self.assertRaises(db.InvalidTransition):
            db.finish_task(self.task["id"], "failed", error="late")
        self.assertEqual(db.get_task(self.task["id"])["status"], "done")

    def test_finish_rejects_bad_status(self):
        db.claim_next_task(self.p)
        for bad in ("running", "pending", "approved", "nope"):
            with self.assertRaises(ValueError):
                db.finish_task(self.task["id"], bad)

    def test_error_is_truncated(self):
        db.claim_next_task(self.p)
        failed = db.finish_task(self.task["id"], "failed", error="e" * 10000)
        self.assertEqual(len(failed["error"]), db.MAX_ERROR_CHARS)

    def test_retry_failed_resets_task(self):
        db.claim_next_task(self.p)
        db.finish_task(self.task["id"], "failed", result_path="/r", error="bad")
        retried = db.retry_task(self.task["id"])
        self.assertEqual(retried["status"], "pending")
        self.assertIsNone(retried["error"])
        self.assertIsNone(retried["result_path"])
        self.assertIsNone(retried["started_at"])
        self.assertEqual(retried["attempts"], 1)
        self.assertEqual(db.claim_next_task(self.p)["attempts"], 2)

    def test_retry_only_failed(self):
        with self.assertRaises(db.InvalidTransition):
            db.retry_task(self.task["id"])  # pending
        db.claim_next_task(self.p)
        with self.assertRaises(db.InvalidTransition):
            db.retry_task(self.task["id"])  # running
        db.finish_task(self.task["id"], "done", result_path="/r")
        with self.assertRaises(db.InvalidTransition):
            db.retry_task(self.task["id"])  # done
        with self.assertRaises(db.NotFound):
            db.retry_task(999)

    def test_transition_validation(self):
        with self.assertRaises(ValueError):
            db.transition(self.task["id"], ["pending"], "bogus")
        with self.assertRaises(ValueError):
            db.transition(self.task["id"], ["bogus"], "done")
        moved = db.transition(self.task["id"], ["pending"], "rejected", error="no")
        self.assertEqual(moved["status"], "rejected")
        self.assertEqual(moved["error"], "no")


class RecoverTests(LowRAMTestCase):
    def test_dead_owner_is_recovered(self):
        p = db.create_project("A", "general", tasks=[("t1", ""), ("t2", "")])
        task = db.claim_next_task(p, owner=dead_owner())
        self.assertEqual(db.recover_stale(), [task["id"]])
        after = db.get_task(task["id"])
        self.assertEqual(after["status"], "failed")
        self.assertIn("Interrupted", after["error"])
        self.assertIsNone(after["lease_owner"])
        self.assertIsNotNone(db.claim_next_task(p))  # queue is unblocked

    def test_claim_recovers_dead_owner_automatically(self):
        p = db.create_project("A", "general", tasks=[("t1", ""), ("t2", "")])
        stale = db.claim_next_task(p, owner=dead_owner())
        nxt = db.claim_next_task(p)
        self.assertEqual(nxt["title"], "t2")
        self.assertEqual(db.get_task(stale["id"])["status"], "failed")

    def test_live_owner_not_recovered(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        task = db.claim_next_task(p)  # owner == this process
        self.assertEqual(db.recover_stale(), [])
        self.assertEqual(db.get_task(task["id"])["status"], "running")

    def test_other_host_assumed_alive(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        task = db.claim_next_task(p, owner="some-other-host.invalid:1")
        self.assertEqual(db.recover_stale(), [])
        self.assertEqual(db.get_task(task["id"])["status"], "running")

    def test_malformed_owner_is_dead(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        task = db.claim_next_task(p, owner=f"{socket.gethostname()}:notapid")
        self.assertEqual(db.recover_stale(), [task["id"]])

    def test_force_recovers_live_owner(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        task = db.claim_next_task(p)
        self.assertEqual(db.recover_stale(force=True), [task["id"]])
        self.assertEqual(db.get_task(task["id"])["status"], "failed")

    def test_stale_lock_is_cleaned(self):
        p = db.create_project("A", "general", tasks=[("t1", "")])
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO locks (name, owner, label, acquired_at) VALUES (?, ?, ?, ?)",
                (db.INFERENCE_LOCK, dead_owner(), "ghost", db.utcnow()),
            )
        self.assertIsNone(db.busy_reason())
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0], 0)
        self.assertIsNotNone(db.claim_next_task(p))

    def test_live_lock_is_kept(self):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO locks (name, owner, label, acquired_at) VALUES (?, ?, ?, ?)",
                (db.INFERENCE_LOCK, db.owner_id(), "me", db.utcnow()),
            )
        self.assertEqual(db.recover_stale(), [])
        self.assertIn("me", db.busy_reason())

    def test_nothing_to_recover(self):
        self.assertEqual(db.recover_stale(), [])


if __name__ == "__main__":
    unittest.main()
