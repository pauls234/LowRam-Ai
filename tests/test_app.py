import http.client
import json
import threading
import unittest
from pathlib import Path

from backend import app, db, runner
from tests.helpers import Gate, HttpError, LowRAMTestCase, Text, wait_until

INDEX = Path(app.STATIC_DIR) / "index.html"
TOKEN = "t"


class AppTestCase(LowRAMTestCase):
    def setUp(self):
        super().setUp()
        self.server = app.LowRAMServer(0, token=TOKEN)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.server.loop.stop()
        wait_until(lambda: not self.server.loop.is_active(), 15)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, body=None, *, token=TOKEN, content_type="application/json", headers=None, raw=None):
        hdrs = {"Connection": "close", **(headers or {})}
        if method == "POST":
            if content_type:
                hdrs.setdefault("Content-Type", content_type)
            if token is not None:
                hdrs.setdefault("X-LowRAM-Token", token)
        data = raw if raw is not None else (json.dumps(body if body is not None else {}).encode() if method == "POST" else None)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, dict(resp.getheaders()), payload
        finally:
            conn.close()

    def api(self, method, path, body=None, **kw):
        status, headers, payload = self.request(method, path, body, **kw)
        return status, json.loads(payload)

    def new_project(self, tasks="one\ntwo", name="proj"):
        status, data = self.api("POST", "/api/projects", {"name": name, "tasks": tasks})
        self.assertEqual(status, 201, data)
        return data["project"]["id"]

    def wait_loop_done(self, timeout=15):
        def done():
            return not self.api("GET", "/api/loop")[1]["loop"]["active"]

        self.assertTrue(wait_until(done, timeout), "loop did not finish")
        return self.api("GET", "/api/loop")[1]["loop"]


class PageTests(AppTestCase):
    @unittest.skipUnless(INDEX.exists(), "backend/static/index.html not built yet")
    def test_index_injects_token_and_sets_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertNotIn(app.TOKEN_PLACEHOLDER, text)
        self.assertIn(TOKEN, text)
        self.assertEqual(headers["Content-Security-Policy"], app.CSP)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(headers["Content-Type"].startswith("text/html"))

    def test_security_headers_on_api(self):
        status, headers, _ = self.request("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"], app.CSP)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_host_header_check(self):
        for host in ("evil.com", "evil.com:%d" % self.port, "127.0.0.1:1", "127.0.0.1.evil.com", ""):
            with self.subTest(host=host):
                status, _, _ = self.request("GET", "/api/projects", headers={"Host": host})
                self.assertEqual(status, 403)
        for host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}", f"[::1]:{self.port}"):
            with self.subTest(host=host):
                status, _, _ = self.request("GET", "/api/projects", headers={"Host": host})
                self.assertEqual(status, 200)

    def test_host_check_applies_to_post(self):
        status, _, _ = self.request("POST", "/api/recover", headers={"Host": "evil.com"})
        self.assertEqual(status, 403)

    def test_static_whitelist(self):
        for name in app.STATIC_FILES:
            if (app.STATIC_DIR / name).exists():
                status, headers, body = self.request("GET", f"/static/{name}")
                self.assertEqual(status, 200, name)
                self.assertEqual(headers["Content-Type"], app.STATIC_FILES[name])
                self.assertTrue(body)
        for path in ("/static/index.html", "/static/secret.txt", "/static/../app.py", "/static/%2e%2e/app.py", "/static/", "/static"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)


class SecurityTests(AppTestCase):
    def test_post_without_or_with_wrong_token(self):
        status, data = self.api("POST", "/api/recover", token=None)
        self.assertEqual(status, 403)
        status, data = self.api("POST", "/api/recover", token="wrong")
        self.assertEqual(status, 403)
        status, data = self.api("POST", "/api/projects", {"name": "x", "tasks": "y"}, token="wrong")
        self.assertEqual(status, 403)
        self.assertEqual(db.list_projects(), [])

    def test_wrong_content_type(self):
        for ct in ("text/plain", "application/x-www-form-urlencoded", None):
            with self.subTest(ct=ct):
                status, data = self.api("POST", "/api/recover", content_type=ct)
                self.assertEqual(status, 415)
        status, data = self.api("POST", "/api/recover", content_type="application/json; charset=utf-8")
        self.assertEqual(status, 200)

    def test_bad_bodies(self):
        status, _ = self.api("POST", "/api/projects", raw=b"{not json")
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/projects", raw=b"[1, 2]")
        self.assertEqual(status, 400)
        status, _ = self.api("POST", "/api/projects", raw=b"\xff\xfe")
        self.assertEqual(status, 400)
        status, _ = self.api(
            "POST", "/api/projects", raw=b"{}", headers={"Content-Length": str(app.MAX_BODY_BYTES + 1), "Connection": "close"}
        )
        self.assertEqual(status, 413)

    def test_unknown_routes_and_methods(self):
        self.assertEqual(self.api("GET", "/nope")[0], 404)
        self.assertEqual(self.api("POST", "/nope")[0], 404)
        self.assertEqual(self.api("GET", "/api/projects/abc")[0], 404)
        self.assertEqual(self.api("GET", "/api/recover")[0], 404)  # POST-only


class ProjectApiTests(AppTestCase):
    def test_create_list_get(self):
        pid = self.new_project("a\nb\nc", name="  My  proj ")
        status, data = self.api("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(data["projects"][0]["name"], "My proj")
        self.assertEqual(data["projects"][0]["counts"]["pending"], 3)
        status, data = self.api("GET", f"/api/projects/{pid}")
        self.assertEqual(status, 200)
        self.assertEqual([t["title"] for t in data["tasks"]], ["a", "b", "c"])

    def test_create_validation(self):
        for body in ({}, {"name": "x"}, {"name": "", "tasks": "a"}, {"name": "x", "tasks": ""}, {"name": "x", "tasks": 5}):
            with self.subTest(body=body):
                status, data = self.api("POST", "/api/projects", body)
                self.assertEqual(status, 400)
                self.assertIn("error", data)

    def test_404s(self):
        self.assertEqual(self.api("GET", "/api/projects/999")[0], 404)
        self.assertEqual(self.api("POST", "/api/projects/999/run")[0], 404)
        self.assertEqual(self.api("POST", "/api/projects/999/loop")[0], 404)
        self.assertEqual(self.api("POST", "/api/tasks/999/retry")[0], 404)
        self.assertEqual(self.api("GET", "/api/tasks/999/result")[0], 404)

    def test_memory_endpoint(self):
        status, data = self.api("GET", "/api/memory")
        self.assertEqual(status, 200)
        self.assertEqual(data["percent"], 40)
        self.assertTrue(data["can_run"])
        status, data = self.api("GET", "/api/memory?percent=60")
        self.assertEqual(data["percent"], 60)
        for bad in ("19", "81", "abc", "40.5"):
            self.assertEqual(self.api("GET", f"/api/memory?percent={bad}")[0], 400, bad)

    def test_bad_env_is_reported_as_server_config_error(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"LOWRAM_MIN_FREE_MB": "junk"}):
            status, data = self.api("GET", "/api/memory")
        self.assertEqual(status, 500)  # misconfigured server, not a bad request
        self.assertIn("LOWRAM_MIN_FREE_MB", data["error"])


class RunApiTests(AppTestCase):
    def test_run_done_and_result(self):
        pid = self.new_project("say hi")
        self.llm.push(Text("Hello from fake"))
        status, data = self.api("POST", f"/api/projects/{pid}/run", {"percent": 40})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "done")
        tid = data["task"]["id"]
        status, res = self.api("GET", f"/api/tasks/{tid}/result")
        self.assertEqual(status, 200)
        self.assertEqual(res["content"].strip(), "Hello from fake")
        self.assertIn(str(self.home), res["path"])
        status, empty = self.api("POST", f"/api/projects/{pid}/run")
        self.assertEqual(empty["status"], "empty")

    def test_result_of_pending_task_is_409(self):
        pid = self.new_project("x")
        tid = db.list_tasks(pid)[0]["id"]
        self.assertEqual(self.api("GET", f"/api/tasks/{tid}/result")[0], 409)

    def test_result_outside_artifacts_refused(self):
        pid = self.new_project("x")
        tid = db.list_tasks(pid)[0]["id"]
        secret = self.tmp / "secret.txt"
        secret.write_text("TOP SECRET")
        db.claim_next_task(pid)
        db.finish_task(tid, "done", result_path=str(secret))
        status, data = self.api("GET", f"/api/tasks/{tid}/result")
        self.assertEqual(status, 409)
        self.assertNotIn("TOP SECRET", json.dumps(data))

    def test_run_failure_and_retry(self):
        pid = self.new_project("x")
        self.llm.push(HttpError(500, "model exploded"))
        status, data = self.api("POST", f"/api/projects/{pid}/run")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "failed")
        self.assertIn("500", data["error"])
        tid = data["task"]["id"]
        status, data = self.api("POST", f"/api/tasks/{tid}/retry")
        self.assertEqual(status, 200)
        self.assertEqual(data["task"]["status"], "pending")
        status, data = self.api("POST", f"/api/tasks/{tid}/retry")
        self.assertEqual(status, 409)
        status, data = self.api("POST", f"/api/projects/{pid}/run")
        self.assertEqual(data["status"], "done")

    def test_run_paused_on_low_ram(self):
        self.set_memory(total_gib=8, available_mib=5)
        pid = self.new_project("x")
        status, data = self.api("POST", f"/api/projects/{pid}/run")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "paused")
        self.assertEqual(self.llm.requests, [])

    def test_run_invalid_percent(self):
        pid = self.new_project("x")
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/run", {"percent": 19})[0], 400)
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/run", {"percent": True})[0], 400)

    def test_run_busy_returns_409(self):
        pid = self.new_project("x")
        with db.inference_slot("other work"):
            status, data = self.api("POST", f"/api/projects/{pid}/run")
        self.assertEqual(status, 409)
        self.assertIn("other work", data["error"])

    def test_run_coding_project_is_400(self):
        pid = db.create_project("c", "coding", goal="g", repo_path=str(self.tmp))
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/run")[0], 400)
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/loop")[0], 400)

    def test_recover(self):
        status, data = self.api("POST", "/api/recover")
        self.assertEqual((status, data), (200, {"recovered": []}))


class LoopApiTests(AppTestCase):
    def test_loop_runs_to_completion(self):
        pid = self.new_project("a\nb\nc")
        status, data = self.api("POST", f"/api/projects/{pid}/loop", {"percent": 40, "max_tasks": 5})
        self.assertEqual(status, 202)
        self.assertTrue(data["loop"]["active"] or data["loop"]["stopped_because"])
        state = self.wait_loop_done()
        self.assertEqual(state["stopped_because"], "queue_empty")
        self.assertEqual(state["completed"], 3)
        self.assertEqual(state["failed"], 0)
        self.assertEqual(state["project_id"], pid)
        self.assertIsNotNone(state["finished_at"])
        self.assertEqual({t["status"] for t in db.list_tasks(pid)}, {"done"})

    def test_idle_status(self):
        status, data = self.api("GET", "/api/loop")
        self.assertEqual(status, 200)
        self.assertFalse(data["loop"]["active"])

    def test_loop_respects_max_tasks_and_validation(self):
        pid = self.new_project("a\nb\nc")
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/loop", {"max_tasks": 0})[0], 400)
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/loop", {"max_tasks": 101})[0], 400)
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/loop", {"percent": 5})[0], 400)
        self.assertFalse(self.api("GET", "/api/loop")[1]["loop"]["active"])
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/loop", {"max_tasks": 1})[0], 202)
        state = self.wait_loop_done()
        self.assertEqual(state["stopped_because"], "max_tasks")
        self.assertEqual(state["completed"], 1)

    def test_loop_failure_reported(self):
        pid = self.new_project("a\nb")
        self.llm.push(HttpError(500, "bad"))
        self.api("POST", f"/api/projects/{pid}/loop")
        state = self.wait_loop_done()
        self.assertEqual(state["stopped_because"], "failure")
        self.assertEqual(state["failed"], 1)
        self.assertIn("500", state["last_message"])

    def test_stop_while_running(self):
        pid = self.new_project("a\nb\nc")
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.llm.push(Gate(gate, "first answer"))
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/loop")[0], 202)
        self.assertTrue(wait_until(lambda: len(self.llm.requests) >= 1))
        # while the loop is active: second loop, single run and another stop-less start are refused
        status, _ = self.api("POST", f"/api/projects/{pid}/loop")
        self.assertEqual(status, 409)
        status, _ = self.api("POST", f"/api/projects/{pid}/run")
        self.assertEqual(status, 409)
        status, data = self.api("POST", "/api/loop/stop")
        self.assertEqual(status, 200)
        self.assertTrue(data["loop"]["stop_requested"])
        self.assertTrue(data["loop"]["active"])
        gate.set()
        state = self.wait_loop_done()
        self.assertEqual(state["stopped_because"], "stop_requested")
        self.assertEqual(state["completed"], 1)
        self.assertEqual([t["status"] for t in db.list_tasks(pid)], ["done", "pending", "pending"])
        self.assertEqual(len(self.llm.requests), 1)

    def test_stop_when_idle_is_harmless(self):
        status, data = self.api("POST", "/api/loop/stop")
        self.assertEqual(status, 200)
        self.assertFalse(data["loop"]["active"])
        self.assertFalse(data["loop"]["stop_requested"])

    def test_loop_busy_when_slot_held(self):
        pid = self.new_project("a")
        with db.inference_slot("elsewhere"):
            status, data = self.api("POST", f"/api/projects/{pid}/loop")
        self.assertEqual(status, 409)

    def test_loop_paused_low_ram(self):
        self.set_memory(total_gib=8, available_mib=5)
        pid = self.new_project("a")
        self.api("POST", f"/api/projects/{pid}/loop")
        state = self.wait_loop_done()
        self.assertEqual(state["stopped_because"], "paused_low_ram")
        self.assertEqual(db.list_tasks(pid)[0]["status"], "pending")

    def test_can_start_new_loop_after_finish(self):
        pid = self.new_project("a\nb")
        self.api("POST", f"/api/projects/{pid}/loop", {"max_tasks": 1})
        self.wait_loop_done()
        self.assertEqual(self.api("POST", f"/api/projects/{pid}/loop", {"max_tasks": 1})[0], 202)
        state = self.wait_loop_done()
        self.assertEqual(state["completed"], 1)
        self.assertEqual(db.get_project(pid)["counts"]["done"], 2)


if __name__ == "__main__":
    unittest.main()
