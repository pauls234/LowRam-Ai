"""Fixtures for the Devices tests: a real TLS device server on 127.0.0.1."""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend import cluster, runner, settings, worker
from tests.helpers import wait_until
from tests.test_app import AppTestCase

HAS_OPENSSL = shutil.which("openssl") is not None
requires_openssl = unittest.skipUnless(HAS_OPENSSL, "the openssl CLI is not installed")

_CERT_CACHE: tempfile.TemporaryDirectory | None = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def null_slot(label: str):
    """Workers in the same process as the host must not share its inference slot."""
    return contextlib.nullcontext()


def install_certificate() -> None:
    """Generate the certificate once per test run and copy it into each test home."""
    global _CERT_CACHE
    target = cluster.cluster_dir()
    if _CERT_CACHE is None:
        cert, key = cluster.ensure_certificate()
        _CERT_CACHE = tempfile.TemporaryDirectory()
        shutil.copy2(cert, Path(_CERT_CACHE.name) / "server.crt")
        shutil.copy2(key, Path(_CERT_CACHE.name) / "server.key")
        return
    for name in ("server.crt", "server.key"):
        shutil.copy2(Path(_CERT_CACHE.name) / name, target / name)


class ClusterTestCase(AppTestCase):
    """UI server (``LowRAMServer(0, token="t")``) plus the TLS device server on 127.0.0.1."""

    def setUp(self):
        super().setUp()
        if not HAS_OPENSSL:
            self.skipTest("the openssl CLI is not installed")
        for target, value in (("LAN_BIND_HOST", "127.0.0.1"), ("COORDINATOR_INTERVAL_S", 0.05)):
            patcher = mock.patch.object(cluster, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cluster, "lan_addresses", lambda: ["127.0.0.1"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(cluster.reset)
        settings.update("cluster", {"port": free_port()})
        install_certificate()
        cluster.on_app_startup(self.server)

    # ---------------------------------------------------------------- helpers

    def enable(self) -> dict:
        status, data = self.api("POST", "/api/cluster/enable")
        self.assertEqual(status, 200, data)
        return data["cluster"]

    def new_code(self) -> str:
        status, data = self.api("POST", "/api/cluster/pairing")
        self.assertEqual(status, 200, data)
        return data["pairing"]["code"]

    def paired_client(self, name: str = "laptop") -> worker.HostClient:
        creds = worker.pair(cluster.parse_pairing_code(self.new_code()), name)
        return worker.client_from_credentials(creds)

    def new_project(self, tasks="one\ntwo", name="proj"):
        if isinstance(tasks, int):
            tasks = "\n".join(f"task {i}" for i in range(1, tasks + 1))
        return runner.create_project(name, tasks)

    def lan_request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        conn = worker.PinnedHTTPSConnection("127.0.0.1", cluster.listening_port(), cluster.certificate_fingerprint(), 10)
        try:
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/json", "Connection": "close", **(headers or {})})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
        finally:
            conn.close()

    def make_worker(self, code: str, name: str, **kw) -> worker.Worker:
        options = dict(state_path=self.tmp / f"worker-{name}.json", slot=null_slot,
                       heartbeat_s=0.2, idle_min_s=0.02, idle_max_s=0.1)
        options.update(kw)
        return worker.Worker(code, name, 40, **options)

    def start_worker(self, w: worker.Worker) -> threading.Thread:
        thread = threading.Thread(target=w.run, daemon=True)
        thread.start()

        def stop():
            w.stop("test finished")
            thread.join(10)

        self.addCleanup(stop)
        return thread

    def wait(self, predicate, timeout: float = 15.0, message: str = "condition not reached"):
        self.assertTrue(wait_until(predicate, timeout), message)
