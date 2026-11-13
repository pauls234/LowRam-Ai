"""Helper device: work on another LowRAM AI's shared project with this machine's model.

``python3 lowram.py worker --join LOWRAM1-… [--name NAME] [--ram 40]`` or, inside
the app, ``POST /api/cluster/join``. The worker pairs once (credentials in
``<LOWRAM_HOME>/worker.json``, mode 0600), then loops: claim a task → run it on
the local model → send the answer. Heartbeats every 15 s keep its lease alive.

Transport security: TLS without CA validation, but the server certificate's
SHA-256 must match the fingerprint from the pairing code. The check runs right
after the handshake and before any request byte is sent; on a mismatch the
connection is closed ("This is not the device you paired with"). Every request
after pairing carries an HMAC signature (see ``cluster.signed_headers``).
"""

from __future__ import annotations

import base64
import binascii
import email.utils
import hashlib
import hmac
import http.client
import json
import logging
import platform
import socket
import ssl
import sys
import threading
import time
import urllib.error
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from . import artifacts, cluster, config, db, llm, memory, settings

log = logging.getLogger("lowram.worker")

HEARTBEAT_S = 15.0
IDLE_MIN_S = 2.0
IDLE_MAX_S = 30.0
REQUEST_TIMEOUT_S = 30.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_CONTENT_BYTES = 900_000  # stays below the host's 1 MiB body limit
RESULT_RETRIES = 4
SKEW_RETRY_S = 30


class WorkerError(RuntimeError):
    """The worker cannot continue (or a single host call failed)."""


class PinMismatch(WorkerError):
    """The TLS certificate is not the one from the pairing code."""


class HostUnreachable(WorkerError):
    """No connection to the host (temporary)."""


class AuthRejected(WorkerError):
    """The host answered 401: the device was removed, or the key is wrong."""


class HostRejected(WorkerError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ModelUnavailable(WorkerError):
    """The local model server cannot be reached (temporary)."""


# --------------------------------------------------------------------------- transport


def _client_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Trust does not come from a CA but from the pinned fingerprint (checked in connect()).
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that verifies the certificate fingerprint before sending anything."""

    def __init__(self, host: str, port: int, pin: bytes, timeout: float):
        super().__init__(host, port, timeout=timeout, context=_client_context())
        self._pin = pin
        self.peer_fingerprint: bytes | None = None

    def connect(self) -> None:
        super().connect()
        der = self.sock.getpeercert(binary_form=True) if self.sock else None
        digest = hashlib.sha256(der or b"").digest()
        if not der or not self._pin or not hmac.compare_digest(digest[: len(self._pin)], self._pin):
            self.sock.close()
            self.sock = None
            raise PinMismatch(
                f"This is not the device you paired with: the computer at {self.host}:{self.port} presented a "
                "different certificate. Nothing was sent. If the host was reinstalled, create a new pairing code there."
            )
        self.peer_fingerprint = digest


class HostClient:
    """Talks to one host; tries every address from the pairing code."""

    def __init__(self, addresses: list[str] | tuple[str, ...], port: int, pin: bytes, *,
                 device_id: int | None = None, key: bytes | None = None, timeout: float = REQUEST_TIMEOUT_S,
                 clock_offset: float = 0.0):
        self.addresses = list(dict.fromkeys(addresses))
        self.port = port
        self.pin = pin
        self.device_id = device_id
        self.key = key
        self.timeout = timeout
        self.clock_offset = clock_offset
        self.address: str | None = None
        self.peer_fingerprint: bytes | None = None
        self._lock = threading.Lock()

    def _order(self) -> list[str]:
        if self.address in self.addresses:
            return [self.address] + [a for a in self.addresses if a != self.address]  # type: ignore[list-item]
        return list(self.addresses)

    def _send(self, method: str, path: str, body: bytes, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
        mismatch: PinMismatch | None = None
        last: Exception | None = None
        for address in self._order():
            conn = PinnedHTTPSConnection(address, self.port, self.pin, timeout)
            try:
                conn.connect()
            except PinMismatch as exc:
                mismatch = exc
                continue
            except OSError as exc:  # includes ssl.SSLError and timeouts
                last = exc
                conn.close()
                continue
            with self._lock:
                self.address = address
                self.peer_fingerprint = conn.peer_fingerprint
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                return resp.status, resp_headers, raw
            except (OSError, http.client.HTTPException) as exc:
                # The request may have reached the host: never resend it elsewhere.
                raise HostUnreachable(f"Lost the connection to the host at {address}:{self.port}: {exc}") from exc
            finally:
                conn.close()
        if mismatch is not None:
            raise mismatch
        shown = ", ".join(self.addresses)
        raise HostUnreachable(
            f"Could not reach the host at {shown} (port {self.port}): {last}. Is LowRAM AI running there with "
            "Devices turned on, and are both computers on the same network?"
        )

    def _learn_clock(self, date_header: str | None) -> float:
        if not date_header:
            return self.clock_offset
        try:
            server = email.utils.parsedate_to_datetime(date_header).timestamp()
        except (TypeError, ValueError, IndexError):
            return self.clock_offset
        offset = server - time.time()
        return offset if abs(offset) > 2 else 0.0

    def call(self, method: str, path: str, payload: dict | None = None, *, signed: bool = True,
             timeout: float | None = None) -> tuple[int, dict]:
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        for attempt in (0, 1):
            headers = {"Content-Type": "application/json", "Accept": "application/json", "Connection": "close"}
            if signed:
                if self.device_id is None or self.key is None:
                    raise WorkerError("This device is not paired yet")
                headers.update(cluster.signed_headers(
                    self.device_id, self.key, method, path, body, int(time.time() + self.clock_offset)))
            status, resp_headers, raw = self._send(method, path, body, headers, timeout or self.timeout)
            learned = self._learn_clock(resp_headers.get("date"))
            if status == 401 and signed and attempt == 0 and abs(learned - self.clock_offset) > SKEW_RETRY_S:
                self.clock_offset = learned  # our clock is off: sign with the host's time and retry once
                continue
            self.clock_offset = learned if abs(learned - self.clock_offset) > SKEW_RETRY_S else self.clock_offset
            if len(raw) > MAX_RESPONSE_BYTES:
                raise HostRejected(status, "The host sent an oversized response")
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HostRejected(status, f"The host sent an unexpected response (HTTP {status})") from exc
            if not isinstance(data, dict):
                raise HostRejected(status, f"The host sent an unexpected response (HTTP {status})")
            if status == 401 and signed:
                raise AuthRejected(
                    "The host does not accept this device any more (it was removed there, or the clocks of the two "
                    "computers differ a lot). Create a new pairing code on the host and join again."
                )
            return status, data
        raise AssertionError("unreachable")


# --------------------------------------------------------------------------- credentials


def default_state_path() -> Path:
    return config.home_dir() / "worker.json"


def load_credentials(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.warning("Ignoring unreadable worker credentials in %s", path)
        return None
    if not isinstance(data, dict):
        return None
    try:
        int(data["device_id"])
        base64.b64decode(data["device_key"], validate=True)
        bytes.fromhex(data["fingerprint"])
        int(data["port"])
    except (KeyError, TypeError, ValueError, binascii.Error):
        return None
    return data


def save_credentials(path: Path, creds: dict) -> None:
    artifacts.write_json_atomic(path, creds)  # 0600, atomic


def _matches(creds: dict, code: cluster.PairingCode) -> bool:
    return int(creds["port"]) == code.port and bytes.fromhex(creds["fingerprint"])[:16] == code.fingerprint_prefix


def client_from_credentials(creds: dict, extra_addresses: tuple[str, ...] = ()) -> HostClient:
    return HostClient(
        list(extra_addresses) + list(creds.get("addresses") or []), int(creds["port"]),
        bytes.fromhex(creds["fingerprint"]), device_id=int(creds["device_id"]),
        key=base64.b64decode(creds["device_key"]),
    )


def device_platform() -> str:
    return cluster.clean_text(f"{platform.system() or 'unknown'} {platform.machine()}", 64)


def default_device_name() -> str:
    try:
        configured = settings.get("cluster", "device_name")
    except Exception:
        configured = None
    if isinstance(configured, str) and configured.strip():
        return cluster.clean_device_name(configured)
    return cluster.clean_device_name(socket.gethostname(), "Helper device")


def pair(code: cluster.PairingCode, name: str, timeout: float = REQUEST_TIMEOUT_S) -> dict:
    """Pair with the host from ``code``; returns credentials (not yet saved)."""
    client = HostClient(code.addresses, code.port, code.fingerprint_prefix, timeout=timeout)
    status, data = client.call("POST", "/cluster/v1/pair", {
        "secret": cluster.encode_secret(code.secret),
        "name": name,
        "platform": device_platform(),
        "ram_total_bytes": int(psutil.virtual_memory().total),
    }, signed=False)
    if status != 200:
        raise WorkerError(data.get("error") or f"Pairing failed (HTTP {status})")
    try:
        device_id = int(data["device_id"])
        key = base64.b64decode(data["device_key"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise WorkerError("The host sent an invalid pairing answer") from exc
    if len(key) != 32 or client.peer_fingerprint is None:
        raise WorkerError("The host sent an invalid pairing answer")
    return {
        "version": 1,
        "host_name": cluster.clean_text(data.get("host_name") or "", 64) or client.address,
        "addresses": list(code.addresses),
        "port": code.port,
        "fingerprint": client.peer_fingerprint.hex(),  # full SHA-256 from now on
        "device_id": device_id,
        "device_key": base64.b64encode(key).decode("ascii"),
        "name": name,
        "paired_at": db.utcnow(),
    }


def _model_unreachable(exc: BaseException) -> bool:
    cause = exc.__cause__
    if isinstance(cause, urllib.error.HTTPError):
        return False
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return False
    return isinstance(cause, (urllib.error.URLError, ConnectionError))


def _model_reachable(url: str) -> bool:
    """Cheap TCP check so a worker with a dead model never claims (and burns) tasks."""
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        socket.create_connection((parsed.hostname, port), timeout=3).close()
    except OSError:
        return False
    return True


def _local_model_name() -> str:
    try:
        from . import engine  # optional, built in parallel

        server = engine.status().get("server") or {}
        if server.get("model_id"):
            return str(server["model_id"])[:120]
    except Exception:
        pass
    return config.llm_model()


# --------------------------------------------------------------------------- the worker


class Worker:
    """Claim → run → report loop. ``run()`` blocks; ``stop()`` may be called from any thread."""

    def __init__(self, code: object, name: object = None, percent: object = memory.DEFAULT_PERCENT, *,
                 state_path: Path | None = None,
                 slot: Callable[[str], AbstractContextManager[Any]] = db.inference_slot,
                 heartbeat_s: float = HEARTBEAT_S, idle_min_s: float = IDLE_MIN_S, idle_max_s: float = IDLE_MAX_S,
                 say: Callable[[str], None] | None = None):
        self.code = cluster.parse_pairing_code(code)
        self.name = cluster.clean_device_name(name, default_device_name())
        self.percent = memory.validate_percent(percent)
        self.state_path = state_path or default_state_path()
        self.slot = slot
        self.heartbeat_s = heartbeat_s
        self.idle_min_s = idle_min_s
        self.idle_max_s = idle_max_s
        self.say = say or (lambda text: log.info("%s", text))
        self.client: HostClient | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current: dict | None = None
        self._abandoned: set[int] = set()
        self._model = config.llm_model()
        self._status: dict = {
            "state": "idle", "host": None, "device_id": None, "tasks_done": 0, "current_task": None,
            "error": None, "message": "",
        }

    # ---------------------------------------------------------------- status

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._status.update(changes)
        if changes.get("message"):
            self.say(changes["message"])

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self, reason: str = "Worker stopped") -> None:
        """Stop claiming; give an in-flight task back to the host (its answer is discarded)."""
        self._stop.set()
        with self._lock:
            task = self._current
            if task is not None:
                self._abandoned.add(task["task_id"])
            self._status.update(state="stopped", current_task=None, message="Stopped.")
        if task is not None and self.client is not None:
            try:
                self.client.call("POST", f"/cluster/v1/tasks/{task['task_id']}/fail",
                                 {"error": reason, "release": True}, timeout=10)
            except WorkerError as exc:
                log.warning("Could not hand task #%s back to the host: %s", task["task_id"], exc)

    # ---------------------------------------------------------------- connect

    def _connect(self) -> None:
        self._set(state="pairing", message="Connecting to the host…")
        creds = load_credentials(self.state_path)
        if creds is not None and _matches(creds, self.code):
            self.client = client_from_credentials(creds, self.code.addresses)
            try:
                self._heartbeat()
            except AuthRejected:
                self.say("The host no longer knows this device; pairing again with the code…")
                creds = None
            else:
                creds["addresses"] = list(dict.fromkeys(list(self.code.addresses) + list(creds.get("addresses") or [])))
                save_credentials(self.state_path, creds)
        if creds is None or not _matches(creds, self.code):
            creds = pair(self.code, self.name)
            save_credentials(self.state_path, creds)
            self.client = client_from_credentials(creds)
            self.say(f"Paired with {creds['host_name']} as device #{creds['device_id']}.")
        self._set(host=creds.get("host_name"), device_id=int(creds["device_id"]))

    def _heartbeat(self) -> dict:
        assert self.client is not None
        with self._lock:
            task = self._current
        status, data = self.client.call("POST", "/cluster/v1/heartbeat", {
            "task_id": task["task_id"] if task else None, "model": self._model,
            "ram_total_bytes": int(psutil.virtual_memory().total),
        })
        if status != 200:
            raise HostRejected(status, data.get("error") or f"Heartbeat failed (HTTP {status})")
        if task is not None and data.get("lease_valid") is False:
            with self._lock:
                self._abandoned.add(task["task_id"])
            self.say(f"The host gave task #{task['task_id']} to someone else; its answer will be discarded.")
        return data

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            try:
                self._heartbeat()
            except AuthRejected as exc:
                self._set(error=str(exc))
            except WorkerError as exc:
                log.info("Heartbeat failed: %s", exc)

    # ---------------------------------------------------------------- work

    def _wait(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _claim(self, plan: memory.MemoryPlan) -> dict | None:
        assert self.client is not None
        status, data = self.client.call("POST", "/cluster/v1/claim", {
            "context_chars": plan.context_chars, "max_tokens": plan.max_tokens, "model": self._model,
        })
        if status != 200:
            raise HostRejected(status, data.get("error") or f"Claim failed (HTTP {status})")
        task = data.get("task")
        if task is None:
            return None
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), int):
            raise HostRejected(status, "The host sent an invalid task")
        return task

    def _report(self, path: str, payload: dict) -> None:
        """Send a result/failure; retry while the host is unreachable (the answer is valuable)."""
        assert self.client is not None
        delay = 1.0
        for attempt in range(RESULT_RETRIES):
            try:
                status, data = self.client.call("POST", path, payload)
            except HostUnreachable as exc:
                if attempt == RESULT_RETRIES - 1 or self._stop.is_set():
                    raise
                self._set(error=str(exc))
                self._wait(delay)
                delay *= 2
                continue
            if status == 409:
                self.say(f"The host did not take this answer: {data.get('error')}")
                return
            if status != 200:
                raise HostRejected(status, data.get("error") or f"HTTP {status}")
            return

    def _messages(self, task: dict) -> list[dict]:
        messages = task.get("messages")
        if isinstance(messages, list) and all(isinstance(m, dict) for m in messages):
            return messages
        from . import runner

        return runner._messages({"position": task.get("position", "?"), "title": task.get("title", "")},
                                task.get("context") or "")

    def _clear_current(self) -> None:
        with self._lock:
            self._current = None
            self._status["current_task"] = None

    def _process(self, task: dict, url: str) -> None:
        with self._lock:
            self._current = task
        try:
            self._process_inner(task, url)
        except Exception:
            self._clear_current()
            raise
        # KeyboardInterrupt/SystemExit keep _current so stop() can hand the task back.
        self._clear_current()

    def _process_inner(self, task: dict, url: str) -> None:
        tid = task["task_id"]
        self._set(state="working", error=None, current_task={"id": tid, "title": task.get("title"),
                                                             "project_id": task.get("project_id")},
                  message=f"Working on task #{tid}: {cluster.clean_text(task.get('title', ''), 80)}")
        started = time.monotonic()
        try:
            answer = llm.chat(self._messages(task), max_tokens=int(task.get("max_tokens") or 512),
                              temperature=float(task.get("temperature", 0.2)), url=url)
        except llm.LLMError as exc:
            if self._is_abandoned(tid):
                return
            if _model_unreachable(exc):
                self._report(f"/cluster/v1/tasks/{tid}/fail", {"error": f"Local model unreachable: {exc}", "release": True})
                raise ModelUnavailable(str(exc)) from exc
            self._report(f"/cluster/v1/tasks/{tid}/fail", {"error": str(exc)})
            self._set(message=f"Task #{tid} failed: {exc}")
            return
        if self._is_abandoned(tid):
            return
        data = answer.encode("utf-8")
        if len(data) > MAX_CONTENT_BYTES:
            answer = data[:MAX_CONTENT_BYTES].decode("utf-8", "ignore") + "\n[…truncated by the worker]"
        duration = round(time.monotonic() - started, 2)
        self._report(f"/cluster/v1/tasks/{tid}/result", {"content": answer, "duration_s": duration, "model": self._model})
        with self._lock:
            self._status["tasks_done"] += 1
        self._set(message=f"Task #{tid} done in {duration:g} s.")

    def _is_abandoned(self, task_id: int) -> bool:
        with self._lock:
            return task_id in self._abandoned or self._stop.is_set()

    def _work_loop(self) -> None:
        url = cluster.local_llm_url()
        delay = self.idle_min_s
        while not self._stop.is_set():
            plan = memory.plan(self.percent)
            if not plan.can_run:
                self._set(state="idle", message=plan.reason)
                self._wait(self.idle_max_s)
                continue
            if not _model_reachable(url):
                self._set(state="error", error=f"The local model at {llm.redact_url(url)} is not reachable; "
                                               "start it (Models & Settings) – retrying.")
                self._wait(delay)
                delay = min(delay * 2, self.idle_max_s)
                try:
                    url = cluster.local_llm_url()
                except Exception as start_exc:  # engine errors are reported, then retried
                    self._set(error=f"Could not start the local model: {start_exc}")
                continue
            task = None
            try:
                with self.slot(f"helping {self._status.get('host') or 'another device'} (worker)"):
                    if self._stop.is_set():
                        break
                    task = self._claim(plan)
                    if task is not None:
                        self._process(task, url)
            except db.Busy as exc:
                self._set(state="idle", message=f"Waiting: {exc}")
            except ModelUnavailable as exc:
                self._set(state="error", error=f"Local model not reachable: {exc}")
                self._wait(delay)
                delay = min(delay * 2, self.idle_max_s)
                try:
                    url = cluster.local_llm_url()
                except Exception as start_exc:  # engine errors are reported, then retried
                    self._set(error=f"Could not start the local model: {start_exc}")
                continue
            except (HostUnreachable, HostRejected) as exc:
                self._set(state="error", error=str(exc))
            if task is None:
                if self._status.get("state") != "error":
                    self._set(state="idle", current_task=None)
                self._wait(delay)
                delay = min(delay * 2, self.idle_max_s)
            else:
                delay = self.idle_min_s
                with self._lock:
                    if self._status["state"] == "error":
                        self._status["state"] = "idle"

    def run(self) -> int:
        """Blocks until stopped. 0 = stopped cleanly, 1 = fatal error (see status()['error'])."""
        heartbeat: threading.Thread | None = None
        try:
            self._connect()
            self._set(state="idle", error=None, message=f"Connected to {self._status['host']}. Waiting for tasks…")
            self._model = _local_model_name()
            heartbeat = threading.Thread(target=self._heartbeat_loop, name="lowram-worker-heartbeat", daemon=True)
            heartbeat.start()
            self._work_loop()
            return 0
        except KeyboardInterrupt:
            self.stop("Worker stopped (Ctrl+C) before finishing this task")
            return 0
        except (WorkerError, ValueError) as exc:
            if self._stop.is_set():
                return 0
            self._set(state="error", error=str(exc), current_task=None)
            self.say(f"Error: {exc}")
            return 1
        except Exception as exc:  # e.g. engine.EngineError while starting the local model
            if self._stop.is_set():
                return 0
            log.exception("Worker crashed")
            self._set(state="error", error=f"{type(exc).__name__}: {exc}", current_task=None)
            self.say(f"Error: {exc}")
            return 1
        finally:
            already = self._stop.is_set()
            self._stop.set()
            if heartbeat is not None:
                heartbeat.join(2)
            if already:
                with self._lock:
                    self._status["state"] = "stopped"
            if self.client is not None and self.client.device_id is not None:
                try:
                    self.client.call("POST", "/cluster/v1/leave", {}, timeout=5)
                except WorkerError:
                    pass


# --------------------------------------------------------------------------- CLI and in-app helper


def run_cli(args: Any) -> int:
    """``lowram.py worker --join CODE [--name NAME] [--ram PERCENT]``."""
    def say(text: str) -> None:
        print(cluster.clean_text(text, 500), flush=True)

    try:
        worker = Worker(args.join, getattr(args, "name", None), getattr(args, "ram", memory.DEFAULT_PERCENT), say=say)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    say(f"Joining as '{worker.name}' (RAM budget {worker.percent}%). Press Ctrl+C to stop.")
    return worker.run()


_APP_LOCK = threading.Lock()
_APP_WORKER: Worker | None = None
_APP_THREAD: threading.Thread | None = None


def start_app_worker(code: object, name: object = None, percent: object = memory.DEFAULT_PERCENT) -> dict:
    """Run a worker in a background thread of the app (``POST /api/cluster/join``)."""
    global _APP_WORKER, _APP_THREAD
    with _APP_LOCK:
        if _APP_THREAD is not None and _APP_THREAD.is_alive() and _APP_WORKER and not _APP_WORKER.stopped:
            raise db.Busy("This computer is already helping another device. Leave first.")
        worker = Worker(code, name, percent)
        thread = threading.Thread(target=worker.run, name="lowram-app-worker", daemon=True)
        _APP_WORKER, _APP_THREAD = worker, thread
        thread.start()
        return worker.status()


def stop_app_worker(forget: bool = False) -> dict | None:
    global _APP_WORKER, _APP_THREAD
    with _APP_LOCK:
        worker, thread = _APP_WORKER, _APP_THREAD
        if forget:
            _APP_WORKER = _APP_THREAD = None
    if worker is None:
        return None
    if not worker.stopped:
        worker.stop("Stopped from the app")
    if forget and thread is not None:
        thread.join(5)
    return worker.status()


def app_worker_status() -> dict | None:
    with _APP_LOCK:
        worker = _APP_WORKER
    return worker.status() if worker else None
