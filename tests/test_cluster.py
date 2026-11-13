"""Devices: pairing, TLS pinning, HMAC auth, distributed claims, lease expiry, merge, UI routes."""

import argparse
import base64
import io
import json
import os
import secrets
import socket
import stat
import threading
import time
import unittest
from collections import namedtuple
from contextlib import redirect_stderr
from unittest import mock

from backend import artifacts, cluster, db, settings, worker
from tests.cluster_helpers import ClusterTestCase, free_udp_port, null_slot, requires_openssl, result_meta
from tests.helpers import Gate, HttpError, LowRAMTestCase, Text, closed_port_url, wait_until

FP = bytes(range(32))
SECRET = bytes(range(100, 116))


class PairingCodeTests(unittest.TestCase):
    def test_roundtrip_and_tolerant_input(self):
        code = cluster.make_pairing_code(["192.168.1.5", "10.0.0.2"], 8766, FP, SECRET)
        self.assertTrue(code.startswith("LOWRAM1-"))
        for text in (code, code.lower(), "  " + code.replace("-", " ") + "\n", code.replace("-", "")):
            parsed = cluster.parse_pairing_code(text)
            self.assertEqual(parsed.addresses, ("192.168.1.5", "10.0.0.2"))
            self.assertEqual((parsed.port, parsed.fingerprint_prefix, parsed.secret), (8766, FP[:16], SECRET))
        prefix, body = code[:8], code[8:]
        confusable = prefix + body.replace("O", "0").replace("I", "1").replace("B", "8")
        self.assertEqual(cluster.parse_pairing_code(confusable), cluster.parse_pairing_code(code))

    def test_typos_fail_clearly(self):
        code = cluster.make_pairing_code(["192.168.1.5"], 8766, FP, SECRET)
        pos = 12
        wrong = "A" if code[pos] != "A" else "C"
        with self.assertRaisesRegex(cluster.PairingCodeError, "typo"):
            cluster.parse_pairing_code(code[:pos] + wrong + code[pos + 1:])
        for bad in ("", "hello", "LOWRAM1-", code[:-7], code + "AAAAAAAA", 42):
            with self.subTest(bad=bad), self.assertRaises(cluster.PairingCodeError):
                cluster.parse_pairing_code(bad)

    def test_signature_and_nonce_cache(self):
        sig = cluster.sign(b"k" * 32, "post", "/x", "1", "n" * 16, b"{}")
        self.assertEqual(sig, cluster.sign(b"k" * 32, "POST", "/x", "1", "n" * 16, b"{}"))
        self.assertNotEqual(sig, cluster.sign(b"k" * 32, "POST", "/x", "1", "n" * 16, b"{ }"))
        cache = cluster.NonceCache(capacity=3, per_device=2)
        self.assertTrue(cache.add("1", "a", 100, 0))
        self.assertFalse(cache.add("1", "a", 100, 1))  # replay
        self.assertTrue(cache.add("1", "b", 100, 1))
        self.assertFalse(cache.add("1", "c", 100, 2))  # device 1 used its share: refuse, never forget
        self.assertTrue(cache.add("2", "a", 100, 2))  # … but device 2 is not locked out
        self.assertFalse(cache.add("3", "a", 100, 2))  # whole cache full of live entries
        self.assertTrue(cache.add("1", "c", 300, 150))  # the old ones expired
        self.assertEqual(len(cache), 1)

    def test_lan_addresses_skip_loopback_and_link_local(self):
        Addr = namedtuple("Addr", "family address netmask broadcast ptp")
        fake = {
            "lo": [Addr(cluster.socket.AF_INET, "127.0.0.1", None, None, None)],
            "eth0": [Addr(cluster.socket.AF_INET, "169.254.3.4", None, None, None),
                     Addr(cluster.socket.AF_INET, "93.184.216.34", None, None, None),
                     Addr(cluster.socket.AF_INET6, "fe80::1", None, None, None)],
            "wlan0": [Addr(cluster.socket.AF_INET, "192.168.178.20", None, None, None)],
        }
        with mock.patch.object(cluster.psutil, "net_if_addrs", return_value=fake):
            self.assertEqual(cluster.lan_addresses(), ["192.168.178.20", "93.184.216.34"])


@requires_openssl
class CertificateTests(LowRAMTestCase):
    def test_generated_once_with_private_key_0600(self):
        cert, key = cluster.ensure_certificate()
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(cert.parent).st_mode), 0o700)
        first = cluster.certificate_fingerprint()
        self.assertEqual(len(first), 32)
        cluster.ensure_certificate()
        self.assertEqual(cluster.certificate_fingerprint(), first)
        self.assertEqual(sorted(p.name for p in cert.parent.iterdir()), ["server.crt", "server.key"])

    def test_missing_openssl_is_a_clear_error(self):
        with mock.patch.object(cluster.shutil, "which", return_value=None):
            with self.assertRaisesRegex(cluster.ClusterError, "openssl"):
                cluster.ensure_certificate()


class PairingTests(ClusterTestCase):
    def test_pairing_needs_the_device_server(self):
        status, data = self.api("POST", "/api/cluster/pairing")
        self.assertEqual(status, 409)

    def test_pair_lists_device_and_never_returns_the_key_again(self):
        self.enable()
        code = self.new_code()
        creds = worker.pair(cluster.parse_pairing_code(code), "Laptop  One")
        self.assertEqual(len(base64.b64decode(creds["device_key"])), 32)
        self.assertEqual(bytes.fromhex(creds["fingerprint"]), cluster.certificate_fingerprint())
        status, data = self.api("GET", "/api/cluster")
        self.assertEqual(status, 200)
        self.assertEqual([d["name"] for d in data["devices"]], ["Laptop One"])
        self.assertEqual(data["devices"][0]["status"], "online")
        self.assertIsNone(data["pairing"])  # one-time
        self.assertNotIn(creds["device_key"], json.dumps(data))
        with self.assertRaisesRegex(worker.WorkerError, "expired or was already used"):
            worker.pair(cluster.parse_pairing_code(code), "again")

    def test_expired_code(self):
        self.enable()
        code = cluster.parse_pairing_code(self.new_code())
        later = time.time() + cluster.PAIRING_TTL_S + 1
        with mock.patch.object(cluster, "_now", return_value=later):
            with self.assertRaises(worker.WorkerError):
                worker.pair(code, "late")
        self.assertEqual(self.api("GET", "/api/cluster")[1]["devices"], [])

    def test_five_wrong_secrets_invalidate_the_code(self):
        self.enable()
        good = cluster.parse_pairing_code(self.new_code())
        bad = cluster.PairingCode(good.addresses, good.port, good.fingerprint_prefix, secrets.token_bytes(16))
        for _ in range(cluster.PAIRING_MAX_FAILURES):
            with self.assertRaises(worker.WorkerError):
                worker.pair(bad, "attacker")
        with self.assertRaises(worker.WorkerError):
            worker.pair(good, "too late")
        data = self.api("GET", "/api/cluster")[1]
        self.assertIsNone(data["pairing"])
        self.assertIn("invalidated", data["pairing_error"])
        self.assertEqual(data["devices"], [])

    def test_fingerprint_mismatch_aborts_before_sending(self):
        self.enable()
        good = cluster.parse_pairing_code(self.new_code())
        wrong_fp = bytes(b ^ 0xFF for b in good.fingerprint_prefix)
        bad = cluster.PairingCode(good.addresses, good.port, wrong_fp, good.secret)
        before = dict(cluster._STATE.stats)
        with self.assertRaisesRegex(worker.PinMismatch, "not the device you paired with"):
            worker.pair(bad, "victim")
        self.assertEqual(cluster._STATE.stats, before)  # no HTTP request reached the host
        self.assertEqual(cluster._STATE.pairing["failures"], 0)
        worker.pair(good, "real")  # the code is still valid

    def test_typo_in_code_fails_before_any_connection(self):
        self.enable()
        code = self.new_code()
        broken = code[:15] + ("A" if code[15] != "A" else "C") + code[16:]
        with self.assertRaisesRegex(ValueError, "typo"):
            self.make_worker(broken, "typo")
        with redirect_stderr(io.StringIO()) as err:
            rc = worker.run_cli(argparse.Namespace(join=broken, name=None, ram=40))
        self.assertEqual(rc, 2)
        self.assertIn("typo", err.getvalue())


class AuthTests(ClusterTestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.client = self.paired_client()

    def signed(self, path="/cluster/v1/heartbeat", body=b"{}", *, key=None, ts=None, device=None, nonce=None):
        headers = cluster.signed_headers(device or self.client.device_id, key or self.client.key, "POST", path, body,
                                         int(time.time()) if ts is None else ts)
        if nonce:
            headers[cluster.HEADER_NONCE] = nonce
            headers[cluster.HEADER_SIGNATURE] = cluster.sign(
                key or self.client.key, "POST", path, headers[cluster.HEADER_TIMESTAMP], nonce, body)
        return headers

    def test_valid_request(self):
        status, data = self.lan_request("POST", "/cluster/v1/heartbeat", b"{}", self.signed())
        self.assertEqual((status, data["ok"]), (200, True))

    def test_rejections_are_all_the_same_401(self):
        cases = {
            "no headers": {},
            "bad signature": {**self.signed(), cluster.HEADER_SIGNATURE: "0" * 64},
            "wrong key": self.signed(key=b"x" * 32),
            "clock skew": self.signed(ts=int(time.time()) - cluster.MAX_SKEW_S - 30),
            "future": self.signed(ts=int(time.time()) + cluster.MAX_SKEW_S + 30),
            "unknown device": self.signed(device=999),
        }
        bodies = set()
        for label, headers in cases.items():
            with self.subTest(label):
                status, data = self.lan_request("POST", "/cluster/v1/heartbeat", b"{}", headers)
                self.assertEqual(status, 401)
                bodies.add(json.dumps(data))
        self.assertEqual(bodies, {json.dumps({"error": "Unauthorized"})})
        headers = self.signed()
        status, _ = self.lan_request("POST", "/cluster/v1/heartbeat", b'{"x": 1}', headers)  # body tampered
        self.assertEqual(status, 401)

    def test_replay_is_rejected(self):
        headers = self.signed(nonce="fixed-nonce-0123456789")
        self.assertEqual(self.lan_request("POST", "/cluster/v1/heartbeat", b"{}", headers)[0], 200)
        self.assertEqual(self.lan_request("POST", "/cluster/v1/heartbeat", b"{}", headers)[0], 401)

    def test_revoked_device(self):
        status, data = self.api("POST", f"/api/cluster/devices/{self.client.device_id}/revoke")
        self.assertEqual((status, data), (200, {"revoked": True}))
        self.assertEqual(self.lan_request("POST", "/cluster/v1/heartbeat", b"{}", self.signed())[0], 401)
        with self.assertRaises(worker.AuthRejected):
            self.client.call("POST", "/cluster/v1/heartbeat", {})
        self.assertEqual(self.api("POST", "/api/cluster/devices/999/revoke")[0], 404)

    def test_body_over_one_mib(self):
        headers = {**self.signed(), "Content-Length": str(cluster.MAX_BODY_BYTES + 1)}
        status, _ = self.lan_request("POST", "/cluster/v1/heartbeat", b"{}", headers)
        self.assertEqual(status, 413)

    def test_worker_corrects_a_wrong_clock(self):
        self.client.clock_offset = -600  # this computer's clock is 10 minutes behind
        status, data = self.client.call("POST", "/cluster/v1/heartbeat", {})
        self.assertEqual(status, 200)
        self.assertLess(abs(self.client.clock_offset), 5)

    def test_scopes_are_separated(self):
        # The LAN server never serves the UI (no page, no token, no /api) – even when signed.
        for path in ("/", "/static/app.js", "/api/status"):
            self.assertEqual(self.lan_request("GET", path)[0], 401, path)
        headers = cluster.signed_headers(self.client.device_id, self.client.key, "GET", "/api/cluster", b"")
        status, data = self.lan_request("GET", "/api/cluster", b"", headers)
        self.assertEqual(status, 404)
        # The UI server never serves device routes.
        for path in ("/cluster/v1/claim", "/cluster/v1/pair"):
            self.assertEqual(self.api("POST", path, {})[0], 404, path)
        self.assertEqual(self.api("GET", "/cluster/v1/pair")[0], 404)


class FlowTests(ClusterTestCase):
    def setUp(self):
        super().setUp()
        self.enable()

    def claim(self, client):
        status, data = client.call("POST", "/cluster/v1/claim", {"context_chars": 4000, "max_tokens": 300})
        self.assertEqual(status, 200, data)
        return data["task"]

    def test_claim_heartbeat_result_fail_release(self):
        client = self.paired_client("pc")
        self.assertIsNone(self.claim(client))  # nothing distributed yet
        pid = self.new_project(3)
        cluster.distribute(pid, 40, merge=False)
        task = self.claim(client)
        self.assertEqual((task["title"], task["max_tokens"]), ("task 1", 300))
        self.assertEqual(task["messages"][0]["role"], "system")
        self.assertEqual(db.get_task(task["task_id"])["lease_owner"], f"device:{client.device_id}")
        status, hb = client.call("POST", "/cluster/v1/heartbeat", {"task_id": task["task_id"]})
        self.assertTrue(hb["lease_valid"])
        status, data = client.call("POST", f"/cluster/v1/tasks/{task['task_id']}/result",
                                   {"content": "Answer one", "duration_s": 1.5, "model": "tiny"})
        self.assertEqual((status, data["status"]), (200, "done"))
        done = db.get_task(task["task_id"])
        self.assertEqual(done["attempts"], 1)  # counted when the work was reported, not on claim
        self.assertIn(f"_device{client.device_id}_", done["result_path"])  # a file per attempt
        meta = result_meta(done)
        self.assertEqual((meta["device"]["name"], meta["model"], meta["duration_s"]), ("pc", "tiny", 1.5))
        self.assertEqual(self.api("GET", f"/api/tasks/{task['task_id']}/result")[1]["content"].strip(), "Answer one")

        task2 = self.claim(client)
        self.assertIn("Answer one", task2["context"])  # earlier results become context
        client.call("POST", f"/cluster/v1/tasks/{task2['task_id']}/fail", {"error": "model said no"})
        failed = db.get_task(task2["task_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Failed on device 'pc': model said no", failed["error"])

        task3 = self.claim(client)
        client.call("POST", f"/cluster/v1/tasks/{task3['task_id']}/fail", {"error": "worker stopped", "release": True})
        released = db.get_task(task3["task_id"])
        self.assertEqual((released["status"], released["attempts"]), ("pending", 0))
        self.assertIn("worker stopped", released["error"])
        device = self.api("GET", "/api/cluster")[1]["devices"][0]
        self.assertEqual((device["tasks_done"], device["model"]), (1, "tiny"))

    def test_only_the_lease_holder_may_report(self):
        a, b = self.paired_client("a"), self.paired_client("b")
        pid = self.new_project(1)
        cluster.distribute(pid, 40, merge=False)
        task = self.claim(a)
        status, data = b.call("POST", f"/cluster/v1/tasks/{task['task_id']}/result", {"content": "stolen"})
        self.assertEqual(status, 409)
        self.assertEqual(b.call("POST", f"/cluster/v1/tasks/{task['task_id']}/fail", {"error": "x"})[0], 409)
        self.assertEqual(db.get_task(task["task_id"])["status"], "running")
        status, data = a.call("POST", f"/cluster/v1/tasks/{task['task_id']}/result", {"content": ""})
        self.assertEqual(status, 400)

    def test_concurrent_claims_never_hand_out_a_task_twice(self):
        clients = [self.paired_client(f"d{i}") for i in range(4)]
        pid = self.new_project(20)
        cluster.distribute(pid, 40, merge=False)
        got: list[list[int]] = [[] for _ in clients]

        def drain(index, client):
            while True:
                task = self.claim(client)
                if task is None:
                    return
                got[index].append(task["task_id"])
                client.call("POST", f"/cluster/v1/tasks/{task['task_id']}/result", {"content": f"r{task['task_id']}"})

        threads = [threading.Thread(target=drain, args=(i, c)) for i, c in enumerate(clients)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        ids = [tid for chunk in got for tid in chunk]
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)
        self.assertEqual(db.get_project(pid)["counts"]["done"], 20)

    def test_worker_end_to_end(self):
        pid = self.new_project(3)
        cluster.distribute(pid, 40, merge=False)
        w = self.make_worker(self.new_code(), "helper")
        self.start_worker(w)
        self.wait(lambda: db.get_project(pid)["counts"]["done"] == 3, message="worker did not finish")
        self.assertEqual(len(self.llm.requests), 3)
        state_file = self.tmp / "worker-helper.json"
        self.assertEqual(stat.S_IMODE(os.stat(state_file).st_mode), 0o600)
        self.wait(lambda: w.status()["tasks_done"] == 3)
        self.assertEqual(w.status()["host"], cluster.host_display_name())
        self.assertEqual(result_meta(db.list_tasks(pid)[0])["device"]["name"], "helper")
        w.stop()
        self.assertEqual(w.status()["state"], "stopped")
        # A second run reuses the stored credentials (the one-time code is used up already).
        again = self.make_worker(self.new_code(), "helper")  # same state file → no new pairing
        self.start_worker(again)
        self.wait(lambda: again.status()["state"] == "idle" and again.status()["host"])
        self.assertEqual(len(self.api("GET", "/api/cluster")[1]["devices"]), 1)

    def test_two_workers_and_host_work_in_parallel(self):
        pid = self.new_project(6)
        workers = []
        for i in range(2):  # one pairing code at a time: pair, then make the next code
            w = self.make_worker(self.new_code(), f"w{i}")
            workers.append(w)
            self.start_worker(w)
            self.wait(lambda: w.status()["device_id"] is not None, message="worker did not pair")
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.llm.push(Gate(gate, "g1"), Gate(gate, "g2"), Gate(gate, "g3"))
        status, data = self.api("POST", f"/api/cluster/projects/{pid}/distribute", {"merge": False})
        self.assertEqual((status, data["host_loop"]), (202, "started"))
        self.wait(lambda: len(self.llm.requests) >= 3, message="three parties did not start")
        owners = {t["lease_owner"] for t in db.tasks_with_status(pid, ["running"])}
        self.assertEqual(len(owners), 3, owners)
        self.assertEqual(sum(1 for o in owners if o.startswith("device:")), 2)
        gate.set()
        self.wait(lambda: db.get_project(pid)["counts"]["done"] == 6, message="tasks did not finish")
        self.assertEqual(len(self.llm.requests), 6)  # every task ran exactly once
        endpoints = {result_meta(t)["endpoint"] for t in db.list_tasks(pid)}
        self.assertGreaterEqual(len(endpoints), 2)

    def test_worker_respects_low_ram_and_dead_model(self):
        pid = self.new_project(1)
        cluster.distribute(pid, 40, merge=False)
        self.set_memory(total_gib=8, available_mib=5)
        w = self.make_worker(self.new_code(), "small")
        self.start_worker(w)
        self.wait(lambda: "Paused" in (w.status().get("message") or ""))
        time.sleep(0.2)
        self.assertEqual(db.list_tasks(pid)[0]["status"], "pending")
        w.stop()
        self.set_memory()
        with mock.patch.dict(os.environ, {"LOWRAM_LLM_URL": closed_port_url()}):
            w2 = self.make_worker(self.new_code(), "nomodel")
            self.start_worker(w2)
            self.wait(lambda: "not reachable" in (w2.status().get("error") or ""))
            time.sleep(0.2)
        task = db.list_tasks(pid)[0]
        self.assertEqual((task["status"], task["attempts"]), ("pending", 0))

    def test_ctrl_c_hands_the_task_back(self):
        pid = self.new_project(1)
        cluster.distribute(pid, 40, merge=False)
        w = self.make_worker(self.new_code(), "cli")
        with mock.patch.object(worker.llm, "chat", side_effect=KeyboardInterrupt):
            self.assertEqual(w.run(), 0)
        task = db.list_tasks(pid)[0]
        self.assertEqual((task["status"], task["attempts"]), ("pending", 0))
        self.assertIn("Ctrl+C", task["error"])
        self.assertEqual(w.status()["state"], "stopped")


class LeaseTests(ClusterTestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.client = self.paired_client("flaky")
        self.pid = self.new_project(1)
        cluster.distribute(self.pid, 40, merge=False)
        self.pause_coordinator()  # these tests drive sweep_leases() themselves

    def claim(self):
        return self.client.call("POST", "/cluster/v1/claim", {})[1]["task"]

    def age_device(self, seconds):
        with db.connect() as conn:
            conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time() - seconds, self.client.device_id))

    def test_dead_device_requeues_then_fails_after_three_attempts(self):
        for attempt in (1, 2):
            task = self.claim()
            self.age_device(cluster.LEASE_REQUEUE_S + 10)  # still "alive" for db.recover_stale (< 120 s)
            cluster.sweep_leases()
            row = db.get_task(task["task_id"])
            self.assertEqual((row["status"], row["attempts"]), ("pending", attempt))
            self.assertIn("stopped responding", row["error"])
        status, _ = self.client.call("POST", f"/cluster/v1/tasks/{task['task_id']}/result", {"content": "late"})
        self.assertEqual(status, 409)  # the requeued task no longer belongs to this device
        task = self.claim()
        self.age_device(cluster.LEASE_REQUEUE_S + 10)
        cluster.sweep_leases()
        row = db.get_task(task["task_id"])
        self.assertEqual(row["status"], "failed")
        self.assertIn("Gave up after 3 attempts", row["error"])

    def test_heartbeat_keeps_the_lease(self):
        task = self.claim()
        self.age_device(cluster.LEASE_REQUEUE_S + 10)
        self.client.call("POST", "/cluster/v1/heartbeat", {"task_id": task["task_id"]})
        cluster.sweep_leases()
        self.assertEqual(db.get_task(task["task_id"])["status"], "running")
        self.assertIsNone(db.busy_reason())  # device leases never block this machine

    def test_disable_and_revoke_release_tasks(self):
        task = self.claim()
        self.api("POST", "/api/cluster/disable")
        self.assertEqual(db.get_task(task["task_id"])["status"], "pending")
        self.enable()
        task = self.claim()
        self.api("POST", f"/api/cluster/devices/{self.client.device_id}/revoke")
        self.assertEqual(db.get_task(task["task_id"])["status"], "pending")


class OwnerCheckTests(LowRAMTestCase):
    def test_recover_stale_backstop_uses_device_heartbeat(self):
        pid = db.create_project("p", "general", tasks=[("t", "")])
        with db.connect() as conn:
            dev = conn.execute(
                "INSERT INTO devices (name, key_b64, paired_at, last_seen) VALUES ('d', 'AA==', 'x', ?)", (time.time(),)
            ).lastrowid
        task = db.claim_next_task(pid, owner=f"device:{dev}")
        self.assertEqual(db.recover_stale(), [])
        with db.connect() as conn:
            conn.execute("UPDATE devices SET last_seen = ?", (time.time() - cluster.LEASE_DEAD_S - 5,))
        self.assertEqual(db.recover_stale(), [task["id"]])


class MergeTests(ClusterTestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.client = self.paired_client("m")

    def finish_all(self, pid):
        while True:
            task = self.client.call("POST", "/cluster/v1/claim", {})[1]["task"]
            if task is None:
                return
            self.client.call("POST", f"/cluster/v1/tasks/{task['task_id']}/result",
                             {"content": f"Result of {task['title']}"})

    def final(self, pid):
        return self.api("GET", f"/api/cluster/projects/{pid}/final")

    def test_merge_writes_final_md(self):
        pid = self.new_project(2)
        # Without the host loop only this test's device works on the tasks, so nothing can be
        # merged before both results arrive – independent of thread scheduling.
        cluster.distribute(pid, 40, merge=True)
        self.assertEqual(self.final(pid)[0], 404)
        self.llm.push(Text("THE COMBINED ANSWER"))
        self.finish_all(pid)
        self.wait(lambda: self.final(pid)[0] == 200, message="merge did not run")
        status, data = self.final(pid)
        self.assertEqual(data["content"].strip(), "THE COMBINED ANSWER")
        self.assertTrue(data["path"].endswith(f"project_{pid}/final.md"))
        prompt = self.llm.user_prompt()
        self.assertIn("task 1", prompt)
        self.assertIn("task 2", prompt)
        project = self.api("GET", "/api/cluster")[1]["projects"][0]
        self.assertEqual((project["merge_state"], project["final_available"]), ("done", True))

    def test_merge_failure_is_reported_and_can_be_retried(self):
        pid = self.new_project(1)
        cluster.distribute(pid, 40, merge=True)
        self.llm.push(HttpError(500, "merge broke"))
        self.finish_all(pid)
        self.wait(lambda: self.api("GET", "/api/cluster")[1]["projects"][0]["merge_state"] == "failed")
        status, data = self.final(pid)
        self.assertEqual(status, 404)
        self.assertIn("merge broke", data["error"])
        self.llm.push(Text("second try"))
        self.assertEqual(self.api("POST", f"/api/cluster/projects/{pid}/distribute", {"merge": True})[0], 202)
        self.wait(lambda: self.final(pid)[0] == 200)
        self.assertEqual(self.final(pid)[1]["content"].strip(), "second try")


class UiTests(ClusterTestCase):
    def test_status_enable_disable(self):
        status, data = self.api("GET", "/api/cluster")
        self.assertEqual(status, 200)
        self.assertEqual((data["enabled"], data["listening"], data["devices"], data["worker"]), (False, None, [], None))
        data = self.enable()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["listening"]["port"], settings.get("cluster", "port"))
        self.assertEqual(data["listening"]["addresses"], ["127.0.0.1"])
        self.assertRegex(data["fingerprint"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertTrue(settings.get("cluster", "enabled"))
        status, data = self.api("POST", "/api/cluster/disable")
        self.assertEqual((status, data["cluster"]["listening"], data["cluster"]["enabled"]), (200, None, False))

    def test_enable_failure_is_reported(self):
        for name in ("server.crt", "server.key"):
            (cluster.cluster_dir() / name).unlink()
        with mock.patch.object(cluster.shutil, "which", return_value=None):
            status, data = self.api("POST", "/api/cluster/enable")
        self.assertEqual(status, 409)
        self.assertIn("openssl", data["error"])
        self.assertFalse(settings.get("cluster", "enabled"))
        self.assertIn("openssl", self.api("GET", "/api/cluster")[1]["error"])

    def test_startup_hook_restarts_the_server(self):
        settings.update("cluster", {"enabled": True})
        cluster.on_app_startup(self.server)
        self.assertIsNotNone(self.api("GET", "/api/cluster")[1]["listening"])
        cluster.on_app_shutdown(self.server)
        self.assertIsNone(self.api("GET", "/api/cluster")[1]["listening"])

    def test_distribute_validation_and_csrf(self):
        pid = self.new_project(1)
        self.assertEqual(self.api("POST", f"/api/cluster/projects/{pid}/distribute", {"merge": True})[0], 409)
        self.enable()
        coding = db.create_project("c", "coding", goal="g", repo_path=str(self.tmp))
        self.assertEqual(self.api("POST", f"/api/cluster/projects/{coding}/distribute", {})[0], 400)
        self.assertEqual(self.api("POST", "/api/cluster/projects/999/distribute", {})[0], 404)
        self.assertEqual(self.api("POST", f"/api/cluster/projects/{pid}/distribute", {"merge": "yes"})[0], 400)
        self.assertEqual(self.api("POST", f"/api/cluster/projects/{pid}/distribute", {"percent": 5})[0], 400)
        self.assertEqual(self.api("POST", f"/api/cluster/projects/{pid}/distribute", {}, token="bad")[0], 403)
        self.assertEqual(self.api("POST", "/api/cluster/enable", {}, token=None)[0], 403)
        self.assertEqual(self.api("GET", f"/api/cluster/projects/{pid}/final")[0], 404)

    def test_join_and_leave_in_app(self):
        self.enable()
        pid = self.new_project(1)
        cluster.distribute(pid, 40, merge=False)
        code = self.new_code()
        self.assertEqual(self.api("POST", "/api/cluster/join", {"code": "LOWRAM1-XXXX"})[0], 400)
        status, data = self.api("POST", "/api/cluster/join", {"code": code, "name": "This PC"})
        self.assertEqual(status, 202, data)
        self.assertEqual(self.api("POST", "/api/cluster/join", {"code": code})[0], 409)
        self.wait(lambda: db.get_project(pid)["counts"]["done"] == 1, message="in-app worker did not work")
        info = self.api("GET", "/api/cluster")[1]["worker"]
        self.assertEqual(info["host"], cluster.host_display_name())
        self.assertIn(info["state"], ("idle", "working"))
        status, data = self.api("POST", "/api/cluster/leave")
        self.assertEqual((status, data["worker"]["state"]), (200, "stopped"))
        self.assertEqual(stat.S_IMODE(os.stat(worker.default_state_path()).st_mode), 0o600)


def _send_udp(port: int, payload: bytes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(payload, ("127.0.0.1", port))


class DiscoveryUnitTests(unittest.TestCase):
    def test_beacon_roundtrip_and_validation(self):
        raw = cluster.encode_beacon("0123456789abcdef", "Büro-PC", 8766, FP)
        self.assertEqual(cluster.decode_beacon(raw),
                         {"id": "0123456789abcdef", "name": "Büro-PC", "port": 8766, "fp": FP.hex()[:16]})
        self.assertLessEqual(len(cluster.encode_beacon("0123456789abcdef", "ü" * 500, 8766, FP)), 512)
        good = json.loads(raw)
        for bad in ({**good, "app": "x"}, {**good, "v": 2}, {**good, "id": "XYZ"}, {**good, "fp": "00"},
                    {**good, "port": 80}, {**good, "port": True}, {**good, "port": "8766"}):
            self.assertIsNone(cluster.decode_beacon(json.dumps(bad).encode()), bad)
        for junk in (b"", b"\xff\xfe", b"[1]", b"{" * 600, json.dumps({**good, "name": "x" * 600}).encode()):
            self.assertIsNone(cluster.decode_beacon(junk))
        self.assertEqual(cluster.decode_beacon(json.dumps({**good, "name": "\x1b[31mevil\n"}).encode())["name"],
                         "[31mevil")

    def test_listener_collects_ignores_and_expires(self):
        port = free_udp_port()
        listener = cluster.DiscoveryListener(port, "127.0.0.1", own_id="aaaaaaaaaaaaaaaa")
        self.assertTrue(listener.start())
        self.addCleanup(listener.stop)
        with mock.patch.object(cluster, "BEACON_MIN_GAP_S", 0.0):  # rate limit tested separately below
            _send_udp(port, b"garbage")
            _send_udp(port, cluster.encode_beacon("aaaaaaaaaaaaaaaa", "me", 8766, FP))  # own beacon
            _send_udp(port, cluster.encode_beacon("bbbbbbbbbbbbbbbb", "Laptop", 9000, FP))
            self.assertTrue(wait_until(lambda: listener.hosts(), 10))
        [host] = listener.hosts()
        self.assertEqual((host["id"], host["name"], host["port"], host["addresses"], host["fingerprint_short"]),
                         ("bbbbbbbbbbbbbbbb", "Laptop", 9000, ["127.0.0.1"], FP.hex()[:16]))
        with mock.patch.object(cluster, "_now", return_value=time.time() + cluster.NEARBY_TTL_S + 1):
            self.assertEqual(listener.hosts(), [])

    def test_listener_rate_limits_per_address(self):
        listener = cluster.DiscoveryListener(0, own_id="aaaaaaaaaaaaaaaa")  # not started: handle() directly
        with mock.patch.object(cluster, "BEACON_MIN_GAP_S", 60.0):
            self.assertTrue(listener.handle(cluster.encode_beacon("bbbbbbbbbbbbbbbb", "b", 9000, FP), "10.0.0.2"))
            self.assertFalse(listener.handle(cluster.encode_beacon("cccccccccccccccc", "c", 9000, FP), "10.0.0.2"))
            self.assertTrue(listener.handle(cluster.encode_beacon("cccccccccccccccc", "c", 9000, FP), "10.0.0.3"))
        self.assertEqual(sorted(h["id"] for h in listener.hosts()), ["bbbbbbbbbbbbbbbb", "cccccccccccccccc"])

    def test_listener_never_raises_when_it_cannot_bind(self):
        listener = cluster.DiscoveryListener(free_udp_port(), "203.0.113.1")  # not a local address
        self.assertFalse(listener.start())
        self.assertIn("Cannot listen", listener.error)
        self.assertEqual(listener.hosts(), [])

    def test_verification_code_binds_the_certificate(self):
        nonce_c, nonce_h = secrets.token_bytes(16), secrets.token_bytes(16)
        code = cluster.verification_code(FP, nonce_c, nonce_h)
        self.assertRegex(code, r"^\d{3} \d{3}$")
        self.assertEqual(code, cluster.verification_code(FP, nonce_c, nonce_h))
        self.assertNotEqual(code, cluster.verification_code(bytes(32), nonce_c, nonce_h))  # MITM certificate

    def test_worker_clamps_max_tokens_to_its_own_plan(self):
        plan = namedtuple("Plan", "max_tokens")(400)
        self.assertEqual(worker.effective_max_tokens({"max_tokens": 2048}, plan), 400)
        self.assertEqual(worker.effective_max_tokens({"max_tokens": 300}, plan), 300)
        self.assertEqual(worker.effective_max_tokens({"max_tokens": "x"}, plan), 400)


class EasyPairingTests(ClusterTestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.assertTrue(wait_until(lambda: self.nearby(), 5), "beacon not received")

    def nearby(self):
        status, data = self.api("GET", "/api/cluster/nearby")
        self.assertEqual(status, 200)
        return data["hosts"]

    def raw_client(self, full_pin=True):
        fp = cluster.certificate_fingerprint()
        return worker.HostClient(["127.0.0.1"], cluster.listening_port(), fp if full_pin else fp[:8])

    def pair_request(self, client, nonce, ip_name="x"):
        commit = cluster._b64url(cluster.hashlib.sha256(nonce).digest())
        return client.call("POST", "/cluster/v1/pair-request", {"name": ip_name, "client_commit": commit}, signed=False)

    def poll(self, client, rid, nonce):
        return client.call("POST", "/cluster/v1/pair-status",
                           {"request_id": rid, "client_nonce": cluster._b64url(nonce)}, signed=False)

    def test_nearby_shows_the_beacon(self):
        [host] = self.nearby()
        self.assertEqual(host["id"], cluster.host_id())
        self.assertEqual(host["port"], cluster.listening_port())
        self.assertEqual(host["fingerprint_short"], cluster.certificate_fingerprint().hex()[:16])
        self.assertEqual(host["addresses"], ["127.0.0.1"])

    def test_join_request_approve_then_work(self):
        pid = self.new_project(1)
        cluster.distribute(pid, 40, merge=False)
        host_id = self.nearby()[0]["id"]
        self.assertEqual(self.api("POST", "/api/cluster/join", {"host_id": "ffffffffffffffff"})[0], 404)
        status, data = self.api("POST", "/api/cluster/join", {"host_id": host_id, "name": "Helper"})
        self.assertEqual(status, 202, data)
        self.wait(lambda: self.api("GET", "/api/cluster")[1]["worker"]["state"] == "awaiting_approval")

        def shown():
            reqs = self.api("GET", "/api/cluster")[1]["requests"]
            return reqs and reqs[0]["verification_code"]

        self.wait(shown)
        cluster_state = self.api("GET", "/api/cluster")[1]
        [req] = cluster_state["requests"]
        self.assertEqual(req["verification_code"], cluster_state["worker"]["verification_code"])  # same number
        self.assertEqual((req["name"], req["address"]), ("Helper", "127.0.0.1"))
        self.assertEqual(self.api("POST", f"/api/cluster/requests/{req['id']}/approve")[1], {"approved": True})
        self.wait(lambda: db.get_project(pid)["counts"]["done"] == 1, message="paired helper did not work")
        state = self.api("GET", "/api/cluster")[1]
        self.assertEqual([d["name"] for d in state["devices"]], ["Helper"])
        self.assertEqual(state["requests"], [])
        creds = worker.load_credentials(worker.default_state_path())
        self.assertEqual(bytes.fromhex(creds["fingerprint"]), cluster.certificate_fingerprint())
        self.assertEqual(self.api("POST", f"/api/cluster/requests/{req['id']}/approve")[0], 404)

    def test_deny(self):
        status, _ = self.api("POST", "/api/cluster/join", {"host_id": self.nearby()[0]["id"]})
        self.assertEqual(status, 202)
        self.wait(lambda: (self.api("GET", "/api/cluster")[1]["requests"] or [{}])[0].get("verification_code"))
        rid = self.api("GET", "/api/cluster")[1]["requests"][0]["id"]
        self.assertEqual(self.api("POST", f"/api/cluster/requests/{rid}/deny")[1], {"denied": True})
        self.wait(lambda: "declined" in (self.api("GET", "/api/cluster")[1]["worker"]["error"] or ""))
        self.assertEqual(self.api("GET", "/api/cluster")[1]["devices"], [])

    def test_key_only_once_and_only_for_the_nonce_owner(self):
        client = self.raw_client(full_pin=False)
        nonce, other = secrets.token_bytes(16), secrets.token_bytes(16)
        status, data = self.pair_request(client, nonce)
        self.assertEqual(status, 200, data)
        rid = data["request_id"]
        self.assertEqual(self.poll(client, rid, other)[0], 404)
        rid_status = self.poll(client, rid, nonce)
        self.assertEqual(rid_status, (200, {"state": "pending"}))
        self.assertEqual(self.api("POST", f"/api/cluster/requests/{rid}/approve")[0], 200)
        self.assertEqual(self.poll(client, rid, other)[0], 404)  # a stranger cannot collect the key
        status, data = self.poll(client, rid, nonce)
        self.assertEqual(data["state"], "approved")
        self.assertEqual(len(base64.b64decode(data["device_key"])), 32)
        status, again = self.poll(client, rid, nonce)
        self.assertNotIn("device_key", again)  # handed out exactly once
        self.assertEqual(again["device_id"], data["device_id"])

    def test_numbers_differ_for_another_certificate(self):
        client = self.raw_client(full_pin=False)
        nonce = secrets.token_bytes(16)
        status, data = self.pair_request(client, nonce)
        rid = data["request_id"]
        self.assertEqual(self.api("POST", f"/api/cluster/requests/{rid}/approve")[0], 409)  # nonce not revealed
        self.poll(client, rid, nonce)
        shown = self.api("GET", "/api/cluster")[1]["requests"][0]["verification_code"]
        host_nonce = cluster._from_b64url(data["host_nonce"], 16)
        self.assertEqual(shown, cluster.verification_code(client.peer_fingerprint, nonce, host_nonce))
        self.assertNotEqual(shown, cluster.verification_code(bytes(32), nonce, host_nonce))

    def test_expiry_limits_and_hidden_mode(self):
        client = self.raw_client(full_pin=False)
        nonce = secrets.token_bytes(16)
        rid = self.pair_request(client, nonce)[1]["request_id"]
        status, data = self.pair_request(client, secrets.token_bytes(16))
        self.assertEqual(status, 429)  # rate limit per address
        later = time.time() + cluster.REQUEST_TTL_S + 1
        with mock.patch.object(cluster, "_now", return_value=later):
            self.assertEqual(self.poll(client, rid, nonce)[1], {"state": "expired"})
            self.assertEqual(self.api("GET", "/api/cluster")[1]["requests"], [])
            self.assertEqual(self.api("POST", f"/api/cluster/requests/{rid}/approve")[0], 404)
        commit = cluster._b64url(cluster.hashlib.sha256(b"n" * 16).digest())
        for i in range(cluster.MAX_PENDING_REQUESTS - 1):
            cluster._pair_request({"name": f"d{i}", "client_commit": commit}, f"10.0.0.{i + 1}")
        cluster._pair_request({"name": "last", "client_commit": commit}, "10.0.0.50")
        with self.assertRaises(cluster.HTTPError) as ctx:
            cluster._pair_request({"name": "too many", "client_commit": commit}, "10.0.0.99")
        self.assertEqual(ctx.exception.status, 429)
        settings.update("cluster", {"discoverable": False})
        with mock.patch.object(cluster, "REQUEST_MIN_INTERVAL_S", 0):
            status, data = self.pair_request(self.raw_client(False), nonce)
        self.assertEqual(status, 403)
        self.assertIn("pairing code", data["error"])


class HardeningTests(ClusterTestCase):
    def setUp(self):
        super().setUp()
        self.enable()

    def tls_socket(self):
        raw = socket.create_connection(("127.0.0.1", cluster.listening_port()), timeout=5)
        return worker._client_context().wrap_socket(raw)

    def test_claim_cycles_do_not_burn_attempts_and_are_rate_limited(self):
        client = self.paired_client("greedy")
        pid = self.new_project(1)
        cluster.distribute(pid, 40, merge=False)
        for _ in range(5):  # claim → (re-)claim releases the held task without counting an attempt
            self.assertIsNotNone(client.call("POST", "/cluster/v1/claim", {})[1]["task"])
        task = db.list_tasks(pid)[0]
        self.assertEqual((task["status"], task["attempts"]), ("running", 0))
        with mock.patch.object(cluster, "CLAIM_MIN_INTERVAL_S", 5.0):
            client.call("POST", "/cluster/v1/claim", {})
            status, data = client.call("POST", "/cluster/v1/claim", {})
        self.assertEqual(status, 429)
        self.assertGreater(data["retry_after"], 0)

    def test_slow_request_is_cut_off(self):
        with mock.patch.object(cluster, "REQUEST_DEADLINE_S", 0.5):
            sock = self.tls_socket()
            self.addCleanup(sock.close)
            for ch in b"POST /cluster/v1/heartbeat HTTP/1.1\r\n":  # one byte every 50 ms (each read is "fast")
                try:
                    sock.sendall(bytes([ch]))
                except OSError:
                    break
                time.sleep(0.05)
            sock.settimeout(3)
            try:
                data = sock.recv(100)
            except socket.timeout:
                self.fail("the server kept the trickling connection open")
            except OSError:
                data = b""
            self.assertEqual(data, b"")

    def test_connections_per_address_are_capped(self):
        with mock.patch.object(cluster, "MAX_CONNECTIONS_PER_IP", 2):
            held = [self.tls_socket(), self.tls_socket()]
            with self.assertRaises(OSError):
                extra = self.tls_socket()
                extra.settimeout(3)
                if not extra.recv(1):
                    raise ConnectionResetError("closed")
            for sock in held:
                sock.close()
            self.wait(lambda: self.lan_request("POST", "/cluster/v1/heartbeat")[0] == 401)

    def test_worker_reconnects_after_the_host_comes_back(self):
        w = self.make_worker(self.new_code(), "roamer")
        self.start_worker(w)
        self.wait(lambda: w.status()["device_id"] is not None and w.status()["state"] == "idle")
        self.api("POST", "/api/cluster/disable")
        self.wait(lambda: w.status()["state"] == "error" and "turned off there" in (w.status()["error"] or ""),
                  message="no clear 'host off' message")
        self.enable()
        self.wait(lambda: w.status()["state"] == "idle" and w.status()["error"] is None, message="did not reconnect")
        self.wait(lambda: self.api("GET", "/api/cluster")[1]["devices"][0]["status"] == "online")

    def test_worker_hands_back_when_busy_and_waits_for_a_model(self):
        pid = self.new_project(1)
        cluster.distribute(pid, 40, merge=False)

        def busy_slot(label):
            raise db.Busy("Busy: an image is being generated")

        w = self.make_worker(self.new_code(), "busy", slot=busy_slot)
        thread = self.start_worker(w)
        self.wait(lambda: "Handed task" in (w.status()["message"] or ""))
        # The worker may already have claimed the task again; stopping (and its /leave) hands it back.
        w.stop()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.wait(lambda: db.list_tasks(pid)[0]["status"] == "pending", message="task was not handed back")
        self.assertEqual(db.list_tasks(pid)[0]["attempts"], 0)  # hand-backs never use an attempt
        self.assertEqual(self.llm.requests, [])

        calls = []

        def flaky_engine(timeout=None):
            calls.append(timeout)
            if len(calls) < 3:
                raise RuntimeError("No chat model is installed")
            return self.llm.url

        with mock.patch.object(cluster, "local_llm_url", side_effect=flaky_engine):
            w2 = self.make_worker(self.new_code(), "patient", slot=null_slot)
            self.start_worker(w2)
            self.wait(lambda: w2.status()["message"] == "Waiting for a local model…" or len(calls) >= 3)
            self.wait(lambda: db.get_project(pid)["counts"]["done"] == 1, message="worker gave up on the engine")
        self.assertGreaterEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
