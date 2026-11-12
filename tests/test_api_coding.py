import http.client
import json
import os
import threading
import types
import unittest
from unittest import mock

from backend import app, chat, config, db
from tests.helpers import HttpError, LowRAMTestCase, Text

TOKEN = "t"
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"


def plan_reply(*steps):
    return Text("<think>planning</think>" + json.dumps({"steps": list(steps)}))


def proposal_reply(path="calc.py", content=FIXED, summary="use plus"):
    body = json.dumps({"summary": summary, "changes": [{"path": path, "content": content}]})
    return Text(f"Here you go:\n```json\n{body}\n```")


class CodingApiTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.server = app.LowRAMServer(0, token=TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.repo = self.make_repo({"calc.py": BUGGY, "README.md": "# Calc\n"})

    def _shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def api(self, method, path, body=None, token=TOKEN):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Connection": "close"}
        data = None
        if method == "POST":
            headers.update({"Content-Type": "application/json", "X-LowRAM-Token": token})
            data = json.dumps(body or {}).encode()
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def new_project(self, goal="Fix add"):
        status, data = self.api("POST", "/api/coding/projects", {"goal": goal, "repo_path": str(self.repo)})
        self.assertEqual(status, 201, data)
        return data["project"]["id"]


class ProjectTests(CodingApiTestCase):
    def test_create_list_get(self):
        pid = self.new_project()
        db.create_project("general", "general", tasks=[("t", "")])
        status, data = self.api("GET", "/api/coding/projects")
        self.assertEqual(status, 200)
        self.assertEqual([p["id"] for p in data["projects"]], [pid])
        self.assertEqual(data["projects"][0]["repo_path"], str(self.repo))
        status, data = self.api("GET", f"/api/coding/projects/{pid}")
        self.assertEqual((status, data["project"]["kind"], data["tasks"]), (200, "coding", []))

    def test_create_validation(self):
        a_file = self.repo / "calc.py"
        home = self.tmp / "fakeuserhome"
        home.mkdir(exist_ok=True)
        cases = [
            {"goal": "g", "repo_path": "relative/path"},
            {"goal": "g", "repo_path": str(self.tmp / "missing")},
            {"goal": "g", "repo_path": str(a_file)},
            {"goal": "g", "repo_path": str(home)},
            {"goal": "g", "repo_path": "/"},
            {"goal": "g", "repo_path": 5},
            {"goal": "g"},
            {"goal": "", "repo_path": str(self.repo)},
            {"goal": "g" * 2001, "repo_path": str(self.repo)},
            {"goal": "g", "repo_path": str(self.home)},  # the LowRAM AI data folder itself
            {"goal": "g", "repo_path": str(self.tmp)},  # contains the data folder
        ]
        (self.home / "inside").mkdir(parents=True)
        cases.append({"goal": "g", "repo_path": str(self.home / "inside")})
        for body in cases:
            with self.subTest(body=body):
                status, data = self.api("POST", "/api/coding/projects", body)
                self.assertEqual(status, 400, data)
                self.assertIn("error", data)
        self.assertEqual(self.api("POST", "/api/coding/projects", {"goal": "g", "repo_path": str(self.repo)}, token="x")[0], 403)
        self.assertEqual(db.list_projects(), [])

    def test_not_found(self):
        general = db.create_project("general", "general", tasks=[("t", "")])
        for path in (f"/api/coding/projects/{general}", "/api/coding/projects/999",
                     "/api/coding/projects/999/tasks/1/proposal"):
            self.assertEqual(self.api("GET", path)[0], 404, path)
        for path in (f"/api/coding/projects/{general}/plan", f"/api/coding/projects/{general}/step"):
            self.assertEqual(self.api("POST", path)[0], 404, path)
        self.assertEqual(self.llm.requests, [])


class WorkflowTests(CodingApiTestCase):
    def test_plan_step_approve_writes_file(self):
        pid = self.new_project()
        self.llm.push(plan_reply({"title": "Fix add", "files": ["calc.py"], "detail": "use +"}), proposal_reply())
        status, data = self.api("POST", f"/api/coding/projects/{pid}/plan", {"percent": 40})
        self.assertEqual(status, 200, data)
        self.assertEqual([t["title"] for t in data["tasks"]], ["Fix add"])
        self.assertEqual(data["tasks"][0]["step"], {"files": ["calc.py"], "detail": "use +"})
        self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/plan")[0], 409)  # open steps

        status, result = self.api("POST", f"/api/coding/projects/{pid}/step", {})
        self.assertEqual((status, result["status"]), (200, "proposed"), result)
        tid = result["task_id"]
        self.assertIn("+    return a + b", result["diff"])
        self.assertEqual((self.repo / "calc.py").read_text(), BUGGY)  # nothing written yet

        status, data = self.api("GET", f"/api/coding/projects/{pid}/tasks/{tid}/proposal")
        self.assertEqual(status, 200)
        self.assertEqual(data["proposal"]["changes"][0]["content"], FIXED)
        self.assertEqual(data["proposal"]["summary"], "use plus")

        status, data = self.api("POST", f"/api/coding/projects/{pid}/tasks/{tid}/approve")
        self.assertEqual((status, data["status"], data["written"]), (200, "approved", ["calc.py"]))
        self.assertEqual((self.repo / "calc.py").read_text(), FIXED)
        self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/tasks/{tid}/approve")[0], 409)
        tasks = self.api("GET", f"/api/coding/projects/{pid}")[1]["tasks"]
        self.assertEqual(tasks[0]["status"], "approved")
        self.assertIsNone(db.busy_reason())

    def test_manual_step_reject_leaves_repo_unchanged(self):
        pid = self.new_project()
        before = self.snapshot(self.repo)
        status, data = self.api("POST", f"/api/coding/projects/{pid}/steps",
                                {"title": "Add docs", "files": ["README.md", "docs/new.md"], "detail": "explain"})
        self.assertEqual(status, 201, data)
        tid = data["task_id"]
        self.llm.push(proposal_reply("docs/new.md", "# New\n"))
        result = self.api("POST", f"/api/coding/projects/{pid}/step")[1]
        self.assertEqual((result["status"], result["task_id"]), ("proposed", tid))
        self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/step")[0], 409)  # review pending
        status, data = self.api("POST", f"/api/coding/projects/{pid}/tasks/{tid}/reject", {"reason": "not now"})
        self.assertEqual((status, data["task"]["status"]), (200, "rejected"))
        self.assertIn("not now", data["task"]["error"])
        self.assertEqual(self.snapshot(self.repo), before)
        self.assertEqual(self.api("GET", f"/api/coding/projects/{pid}/tasks/{tid}/proposal")[0], 409)

    def test_step_validation(self):
        pid = self.new_project()
        bad = [{}, {"title": "  "}, {"title": 5}, {"title": "x", "files": "calc.py"}, {"title": "x", "files": [1]},
               {"title": "x", "files": ["../evil.py"]}, {"title": "x", "files": [".env"]},
               {"title": "x", "detail": 5}, {"title": "x", "files": [f"f{i}.py" for i in range(11)]}]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/steps", body)[0], 400)
        self.assertEqual(db.list_tasks(pid), [])
        self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/step", {"percent": 5})[0], 400)
        self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/step")[1]["status"], "empty")

    def test_task_of_other_project_is_404(self):
        pid = self.new_project()
        other = self.new_project("Other goal")
        tid = self.api("POST", f"/api/coding/projects/{other}/steps", {"title": "x"})[1]["task_id"]
        self.assertEqual(self.api("GET", f"/api/coding/projects/{pid}/tasks/{tid}/proposal")[0], 404)
        self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/tasks/{tid}/approve")[0], 404)
        self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/tasks/{tid}/reject")[0], 404)
        self.assertEqual(self.api("POST", f"/api/coding/projects/{other}/tasks/{tid}/reject", {"reason": 5})[0], 400)

    def test_busy_and_model_errors(self):
        pid = self.new_project()
        with db.inference_slot("chat #1"):
            status, data = self.api("POST", f"/api/coding/projects/{pid}/plan")
            self.assertEqual(status, 409)
            self.assertIn("chat #1", data["error"])
            self.assertEqual(self.api("POST", f"/api/coding/projects/{pid}/step")[0], 409)
        self.llm.push(HttpError(500, "model exploded"))
        status, data = self.api("POST", f"/api/coding/projects/{pid}/plan")
        self.assertEqual(status, 502)
        self.assertIn("HTTP 500", data["error"])
        self.llm.push(Text("I cannot plan this."))
        status, data = self.api("POST", f"/api/coding/projects/{pid}/plan")
        self.assertEqual(status, 409)
        self.assertIn("usable plan", data["error"])
        self.set_memory(total_gib=8, available_mib=10)
        status, data = self.api("POST", f"/api/coding/projects/{pid}/plan")
        self.assertEqual(status, 409)
        self.assertIn("Paused", data["error"])
        self.assertIsNone(db.busy_reason())

    def test_managed_engine_is_started_or_409(self):
        pid = self.new_project()
        os.environ.pop("LOWRAM_LLM_URL")  # restored by the base class' patch.dict

        class EngineError(RuntimeError):
            pass

        def ensure_running(timeout):
            raise EngineError("no model installed")

        engine = types.SimpleNamespace(EngineError=EngineError, ensure_running=ensure_running, status=lambda: {})
        with mock.patch.object(chat, "_engine_module", return_value=engine):
            status, data = self.api("POST", f"/api/coding/projects/{pid}/plan")
        self.assertEqual(status, 409)
        self.assertTrue(data["error"].startswith(chat.NO_MODEL_MESSAGE))
        self.assertEqual(self.llm.requests, [])

        started = []

        def start_engine(timeout):  # like the real engine: publish the runtime URL
            started.append(timeout)
            config.set_runtime_llm_url(self.llm.url)
            return self.llm.url

        self.addCleanup(config.set_runtime_llm_url, None)
        engine = types.SimpleNamespace(EngineError=EngineError, status=lambda: {}, ensure_running=start_engine)
        self.llm.push(plan_reply({"title": "one"}))
        with mock.patch.object(chat, "_engine_module", return_value=engine):
            status, data = self.api("POST", f"/api/coding/projects/{pid}/plan")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(started), 1)


if __name__ == "__main__":
    unittest.main()
