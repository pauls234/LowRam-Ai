import json
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend import autosplit, cluster, db, runner
from tests.helpers import Gate, HttpError, Text, wait_until
from tests.test_app import AppTestCase

GOAL = "Schreibe einen Reiseführer für Rom"
PLAN = '```json\n{"tasks": ["Teil A", "Teil B", "teil a", "  3. Teil C\\u0007 "]}\n```'


class AutosplitApiTests(AppTestCase):
    def setUp(self):
        super().setUp()
        for module, name, value in ((cluster, "COORDINATOR_INTERVAL_S", 0.05), (autosplit, "WATCH_INTERVAL_S", 0.05)):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(cluster.reset)
        self.addCleanup(autosplit.stop_watcher)

    def split(self, **body):
        return self.api("POST", "/api/autosplit", {"goal": GOAL, **body})

    def final_ready(self, pid):
        return wait_until(lambda: self.api("GET", f"/api/cluster/projects/{pid}/final")[0] == 200, 20)

    def test_split_creates_project_merges_and_runs(self):
        self.llm.push(Text(PLAN))  # tasks and merge get the default answer
        status, data = self.split(max_tasks=3, distribute=True, percent=40)
        self.assertEqual(status, 202, data)
        self.assertEqual((data["merge"], data["distributed"], data["host_loop"]), (True, True, "started"))
        self.assertEqual([t["title"] for t in data["tasks"]], [f"Teil {x}\n\nGesamtziel: {GOAL}" for x in "ABC"])
        self.assertEqual((data["project"]["name"], data["project"]["kind"]), (GOAL, "general"))
        plan = self.llm.requests[0]["json"]
        self.assertEqual(plan["temperature"], 0.2)
        self.assertEqual(plan["messages"][0]["content"], autosplit.PLAN_SYSTEM_PROMPT)
        self.assertIn(GOAL, plan["messages"][1]["content"])
        pid = data["project"]["id"]
        self.assertTrue(self.final_ready(pid))
        self.assertEqual(db.get_project(pid)["counts"]["done"], 3)
        self.assertEqual(json.loads((Path(autosplit.artifacts.project_dir(pid)) / "autosplit.json").read_text())["goal"], GOAL)

    def test_without_distribute_helpers_never_see_the_tasks(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.llm.push(Text('{"tasks": ["Teil A", "Teil B"]}'), Gate(gate, "Ergebnis A"))
        status, data = self.split()
        self.assertEqual(status, 202, data)
        self.assertEqual((data["merge"], data["distributed"], data["host_loop"]), (True, False, "started"))
        pid = data["project"]["id"]
        self.assertTrue(wait_until(lambda: db.get_project(pid)["counts"]["running"] == 1, 10))
        self.assertIsNone(cluster._distributed_row(pid))
        self.assertIsNone(cluster.claim_for_device({"id": 7, "name": "helper"}, 2000, 128))
        self.assertEqual(autosplit.check_deferred(), [])
        gate.set()
        self.assertTrue(self.final_ready(pid))
        self.assertEqual(autosplit._deferred(), [])

    def test_without_devices_module_runs_locally_without_merge(self):
        self.llm.push(Text('{"tasks": ["Teil A", "Teil B"]}'))
        with mock.patch.object(autosplit, "_cluster", return_value=None):
            status, data = self.split(distribute=True)
        self.assertEqual(status, 202, data)
        self.assertEqual((data["merge"], data["distributed"], data["host_loop"]), (False, False, "started"))
        pid = data["project"]["id"]
        self.assertTrue(wait_until(lambda: db.get_project(pid)["counts"]["done"] == 2, 15))
        self.assertIsNone(cluster._distributed_row(pid))

    def test_unusable_output_is_502_with_raw_artifact(self):
        for reply in ("Das kann ich leider nicht.", '{"tasks": ["Nur eine", "nur  eine"]}'):
            self.llm.push(Text(reply))
            status, data = self.split()
            self.assertEqual(status, 502, data)
            path = Path(data["error"].rsplit("Raw output: ", 1)[1])
            self.assertEqual(path.read_text(encoding="utf-8"), reply)
        self.llm.push(HttpError(500, "boom"))
        self.assertEqual(self.split()[0], 502)
        self.assertEqual(db.list_projects(), [])
        self.assertIsNone(db.busy_reason())

    def test_validation_busy_and_no_model(self):
        for body in ({"goal": "  "}, {"goal": 5}, {"goal": "x" * 4001}, {"goal": "ok", "max_tasks": 1},
                     {"goal": "ok", "max_tasks": 13}, {"goal": "ok", "max_tasks": True},
                     {"goal": "ok", "distribute": "yes"}, {"goal": "ok", "percent": 5}):
            status, data = self.api("POST", "/api/autosplit", body)
            self.assertEqual(status, 400, (body, data))
        with db.inference_slot("other job"):
            self.assertEqual(self.split()[0], 409)
        with mock.patch.object(runner, "model_problem", return_value="No model is running."):
            self.assertEqual(self.split(), (409, {"error": "No model is running."}))
        self.assertEqual((self.llm.requests, db.list_projects()), ([], []))
        self.assertIsNone(db.busy_reason())


class ParseTests(unittest.TestCase):
    def test_reply_variants_are_cleaned_and_deduplicated(self):
        self.assertEqual(autosplit.parse_tasks('Hier: ["- Eins", "Zwei", "zwei"]', 8), ["Eins", "Zwei"])
        self.assertEqual(autosplit.parse_tasks('{"subtasks": [{"title": "A"}, {"task": "B"}, 3, ""]}', 8), ["A", "B"])
        tasks = autosplit.parse_tasks(json.dumps({"tasks": [f"Aufgabe {i} " + "x" * 500 for i in range(20)]}), 12)
        self.assertEqual(len(tasks), 12)
        self.assertTrue(all(len(t) <= autosplit.MAX_SUBTASK_CHARS for t in tasks))
        for raw in ('{"tasks": "A, B"}', "kein JSON", '{"tasks": ["nur eine"]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                autosplit.parse_tasks(raw, 8)

    def test_name_and_task_text_respect_limits(self):
        goal = "Ziel " + "y" * 3990
        self.assertEqual(len(autosplit.project_name(goal)), autosplit.MAX_NAME_CHARS)
        self.assertEqual(autosplit.project_name("Kurz\nund gut"), "Kurz und gut")
        text = autosplit.task_text("x" * 400, goal)
        self.assertLessEqual(len(text), runner.MAX_TASK_CHARS)
        self.assertTrue(text.startswith("x" * 400 + "\n\nGesamtziel: Ziel ") and text.endswith("…"))


if __name__ == "__main__":
    unittest.main()
