"""Devices: other computers of the user on the same local network help with a project.

The host (this machine) splits a *distributed* general project between itself
and paired helper devices. Every device runs its own local model, claims one
pending task at a time, sends the answer back, and the host stores it exactly
like a local result. When all tasks are finished, the host can run one local
"combine the results" inference that writes ``final.md``.

Network design
--------------
* The UI server stays on 127.0.0.1. Only when the user turns Devices on does a
  second server listen on the LAN (``0.0.0.0:<cluster.port>``). It serves only
  routes registered with ``scope="cluster"`` (never the UI page, static files
  or ``/api/*``) and the UI server never serves ``/cluster/*`` routes.
* TLS with a self-signed EC certificate made by the ``openssl`` CLI on first
  use (key file 0600 under ``<LOWRAM_HOME>/cluster/``). Workers pin the
  certificate's SHA-256 fingerprint, which travels inside the pairing code, and
  check it right after the handshake, before sending a single byte.
* Pairing: a one-time code (10 minutes, at most 5 wrong attempts) carries the
  host's addresses, port, fingerprint prefix and a 16-byte secret. A successful
  ``POST /cluster/v1/pair`` returns a random 32-byte device key once.
* Easy pairing: while ``cluster.discoverable`` is on, the host sends a UDP
  beacon (port 8767, broadcast + multicast) with its id, name, port and the
  first 8 bytes of its certificate fingerprint. A helper sends a pairing
  *request* (commitment to a client nonce), gets a host nonce, then reveals
  its nonce; both screens show a 6-digit number derived from the full
  certificate fingerprint and both nonces, and the host user clicks Allow.
  The key is handed out once, to the poller that knows the client nonce.
* Every other LAN request is signed: ``X-LowRAM-Signature`` =
  HMAC-SHA256(key, method\\npath\\ntimestamp\\nnonce\\nsha256(body)). Requests
  with clock skew > 60 s, a reused nonce, an unknown/revoked device or a bad
  signature all get the same ``401 Unauthorized``. Bodies are limited to 1 MiB.

Threat model (summary)
----------------------
Protected: passive sniffing and active man-in-the-middle on the LAN (TLS +
pinning), other LAN hosts calling the API (HMAC device keys), replay (nonce +
timestamp), guessing the pairing secret (128 bit, 5 attempts, 10 minutes),
leaking the device key (never logged, returned once, DB file is 0600).
Easy pairing: beacons are unauthenticated (anyone can announce a fake host or
spoof names); they only offer a target. Safety comes from the number the user
compares: a man-in-the-middle presents another certificate and gets another
number, and the commitment keeps it from choosing nonces until the numbers
match (1 in 10^6 per attempt; requests are rate-limited, at most 3 pending,
2 minutes each). A user who clicks Allow without comparing can pair an
attacker's device – it then gets tasks of shared projects, nothing else.

Not protected: a paired device is trusted to return honest answers (its text is
stored as a task result and becomes context for later prompts and the merge);
anyone who can read ``LOWRAM_HOME`` can read the keys; a LAN attacker can burn a
pairing code with 5 wrong guesses (denial of service, not access); a code that
is photographed/copied while valid can be used by someone else once.

The one exception to "nothing is retried automatically": a task whose device
stops sending heartbeats for :data:`LEASE_REQUEUE_S` seconds goes back to
``pending`` with a note in its error field; that counts as an attempt, and
after :data:`MAX_ATTEMPTS` attempts it is marked ``failed``. Claims never count
as attempts, and tasks a device hands back (it stops, leaves, re-claims, is
revoked or Devices is switched off) return to ``pending`` without using one.
Claims are rate-limited per device, nonces are capped per device, and the LAN
server enforces a total request deadline, an idle timeout and a per-address
connection cap (slowloris).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psutil

from . import artifacts, config, db, llm, memory, power, runner, settings, web
from .web import HTTPError, Request, Response, route

log = logging.getLogger("lowram.cluster")

# --------------------------------------------------------------------------- constants

LAN_SCOPE = "cluster"
LAN_BIND_HOST = "0.0.0.0"  # tests bind 127.0.0.1
CODE_PREFIX = "LOWRAM1-"
CODE_VERSION = 1
MAX_CODE_ADDRESSES = 4
PAIRING_TTL_S = 600
PAIRING_MAX_FAILURES = 5
MAX_SKEW_S = 60
NONCE_CACHE_SIZE = 20_000
LEASE_REQUEUE_S = 90  # the coordinator gives the task back after this much silence
LEASE_DEAD_S = 120  # db.recover_stale backstop (marks the task failed) if nobody requeued it
MAX_ATTEMPTS = 3
ONLINE_WINDOW_S = 45  # workers heartbeat every 15 s
MAX_DEVICES = 32
MAX_LAN_CONNECTIONS = 32
HANDSHAKE_TIMEOUT_S = 10
COORDINATOR_INTERVAL_S = 5.0
MAX_BODY_BYTES = web.MAX_BODY_BYTES
MAX_RESULT_CHARS = 2_000_000
MAX_ERROR_TEXT = 2000
MERGE_MIN_SHARE = 300
OPENSSL_TIMEOUT_S = 60
LLM_START_TIMEOUT_S = 300
CLAIM_MIN_INTERVAL_S = 1.0  # per device; faster claims get 429 + retry_after
NONCES_PER_DEVICE = 300  # one device cannot fill the shared nonce cache
REQUEST_DEADLINE_S = 15.0  # headers + body must arrive within this time (slowloris)
IDLE_TIMEOUT_S = 5  # per socket read / keep-alive idle time
MAX_CONNECTIONS_PER_IP = 4
DISCOVERY_PORT = 8767
DISCOVERY_BIND = ""  # all interfaces; tests use 127.0.0.1
DISCOVERY_GROUP = "239.255.76.82"
BEACON_INTERVAL_S = 2.0
NEARBY_TTL_S = 10.0
MAX_BEACON_BYTES = 512
MAX_NEARBY = 64
BEACON_MIN_GAP_S = 0.2  # per source address
REQUEST_TTL_S = 120
MAX_PENDING_REQUESTS = 3
REQUEST_MIN_INTERVAL_S = 5.0  # per address
PUBLIC_LAN_PATHS = frozenset({"/cluster/v1/pair", "/cluster/v1/pair-request", "/cluster/v1/pair-status"})
REMOTE = db.REMOTE_OWNER_PREFIX  # "device:"

HEADER_DEVICE = "X-LowRAM-Device"
HEADER_TIMESTAMP = "X-LowRAM-Timestamp"
HEADER_NONCE = "X-LowRAM-Nonce"
HEADER_SIGNATURE = "X-LowRAM-Signature"

_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_SIG_RE = re.compile(r"[0-9a-fA-F]{64}")
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{22}")
_PAIR_FAILED = (
    "Pairing failed: the code is wrong, expired or was already used. "
    "Create a new pairing code on the host (Devices) and try again."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_b64 TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    ram_total_bytes INTEGER,
    model TEXT,
    paired_at TEXT NOT NULL,
    last_seen REAL,
    tasks_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS distributed_projects (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    percent INTEGER NOT NULL,
    merge INTEGER NOT NULL DEFAULT 0,
    merge_state TEXT NOT NULL DEFAULT 'off',
    merge_error TEXT,
    final_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_claims (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL,
    claimed_at REAL NOT NULL,
    max_tokens INTEGER NOT NULL,
    context_chars INTEGER NOT NULL,
    skipped TEXT NOT NULL DEFAULT '[]'
);
"""
db.register_schema(SCHEMA)

MERGE_SYSTEM_PROMPT = (
    "You are LowRAM AI. Several computers worked on the tasks of one project in parallel. "
    "Combine their results into one coherent final answer for the whole project. Keep every "
    "important fact, remove repetition, follow the order of the tasks, and point out "
    "contradictions instead of silently choosing one side. Do not invent content that is not "
    "in the results; if something is missing, say so."
)


class ClusterError(RuntimeError):
    """Devices cannot be used right now (no openssl, port in use, …)."""


class PairingCodeError(ValueError):
    """The pairing code is malformed or has a typo."""


class _Unauthorized(Exception):
    """Any authentication failure; the client only ever sees a generic 401."""


def _now() -> float:
    return time.time()


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def clean_text(value: object, limit: int) -> str:
    """Printable single-line text, whitespace collapsed, cut to ``limit``."""
    text = "".join(ch for ch in str(value) if ch.isprintable() or ch in " \t\n")
    return " ".join(text.split())[:limit]


def clean_device_name(value: object, default: str = "Device") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("The device name must be text")
    return clean_text(value, 64) or default


def host_display_name() -> str:
    try:
        configured = settings.get("cluster", "device_name")
    except Exception:  # unreadable settings must not break the LAN API
        configured = None
    if isinstance(configured, str) and configured.strip():
        return clean_text(configured, 64)
    return clean_text(socket.gethostname(), 64) or "LowRAM AI"


# --------------------------------------------------------------------------- protocol (shared with worker.py)


@dataclass(frozen=True)
class PairingCode:
    addresses: tuple[str, ...]
    port: int
    fingerprint_prefix: bytes  # first 16 bytes of the certificate's SHA-256
    secret: bytes  # 16 random bytes, one-time


def _code_checksum(payload: bytes) -> bytes:
    return hashlib.sha256(b"LOWRAM1 pairing code\n" + payload).digest()[:4]


def make_pairing_code(addresses: list[str], port: int, fingerprint: bytes, secret: bytes) -> str:
    ips: list[ipaddress.IPv4Address] = []
    for text in addresses:
        ip = ipaddress.ip_address(text)
        if ip.version == 4 and ip not in ips:
            ips.append(ip)  # type: ignore[arg-type]
    if not ips:
        raise ValueError("No IPv4 address to put into the pairing code")
    if not 1 <= port <= 65535 or len(fingerprint) < 16 or len(secret) != 16:
        raise ValueError("Invalid pairing code parts")
    ips = ips[:MAX_CODE_ADDRESSES]
    payload = (
        bytes([CODE_VERSION, len(ips)]) + b"".join(ip.packed for ip in ips)
        + port.to_bytes(2, "big") + fingerprint[:16] + secret
    )
    text = base64.b32encode(payload + _code_checksum(payload)).decode("ascii").rstrip("=")
    return CODE_PREFIX + "-".join(text[i:i + 5] for i in range(0, len(text), 5))


def parse_pairing_code(text: object) -> PairingCode:
    """Parse and verify a pairing code; typos fail with a clear message."""
    if not isinstance(text, str) or not text.strip():
        raise PairingCodeError("Enter the pairing code shown on the host under Devices")
    cleaned = re.sub(r"[\s\-_.]", "", text.upper())
    prefix = CODE_PREFIX.rstrip("-")
    if not cleaned.startswith(prefix):
        raise PairingCodeError(
            "This is not a LowRAM AI pairing code (it starts with LOWRAM1-). "
            "Copy the code from the host's Devices page."
        )
    body = cleaned[len(prefix):].translate(str.maketrans({"0": "O", "1": "I", "8": "B"}))
    if not body or len(body) > 200 or not re.fullmatch(r"[A-Z2-7]+", body):
        raise PairingCodeError("The pairing code contains characters that cannot be in a pairing code; check for typos")
    try:
        data = base64.b32decode(body + "=" * (-len(body) % 8))
    except (binascii.Error, ValueError) as exc:
        raise PairingCodeError("The pairing code is incomplete or has a typo; copy it again from the host") from exc
    if len(data) < 2 + 4 + 2 + 16 + 16 + 4:
        raise PairingCodeError("The pairing code is too short; copy all of it from the host")
    payload, checksum = data[:-4], data[-4:]
    if not hmac.compare_digest(checksum, _code_checksum(payload)):
        raise PairingCodeError(
            "The pairing code has a typo (its checksum does not match). Copy it again from the host's Devices page."
        )
    if payload[0] != CODE_VERSION:
        raise PairingCodeError("This pairing code comes from a different LowRAM AI version; update both computers")
    count = payload[1]
    if not 1 <= count <= 8 or len(payload) != 2 + 4 * count + 2 + 32:
        raise PairingCodeError("The pairing code is malformed")
    offset = 2 + 4 * count
    addresses = tuple(str(ipaddress.IPv4Address(payload[2 + 4 * i: 6 + 4 * i])) for i in range(count))
    port = int.from_bytes(payload[offset:offset + 2], "big")
    if port == 0:
        raise PairingCodeError("The pairing code is malformed (port 0)")
    return PairingCode(addresses, port, payload[offset + 2:offset + 18], payload[offset + 18:offset + 34])


def signing_message(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    return "\n".join((method.upper(), path, timestamp, nonce, hashlib.sha256(body).hexdigest())).encode("utf-8")


def sign(key: bytes, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    return hmac.new(key, signing_message(method, path, timestamp, nonce, body), hashlib.sha256).hexdigest()


def signed_headers(device_id: int, key: bytes, method: str, path: str, body: bytes, timestamp: int | None = None) -> dict:
    ts = str(int(time.time() if timestamp is None else timestamp))
    nonce = secrets.token_urlsafe(18)
    return {
        HEADER_DEVICE: str(int(device_id)),
        HEADER_TIMESTAMP: ts,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: sign(key, method, path, ts, nonce, body),
    }


def format_fingerprint(digest: bytes) -> str:
    return ":".join(f"{b:02X}" for b in digest)


# --------------------------------------------------------------------------- state


class NonceCache:
    """Remembers (device, nonce) pairs until their timestamp can no longer pass the skew check.

    Full caches refuse new requests instead of forgetting live nonces; the
    per-device cap keeps one (compromised) device from locking out the others.
    """

    def __init__(self, capacity: int = NONCE_CACHE_SIZE, per_device: int = NONCES_PER_DEVICE):
        self.capacity = capacity
        self.per_device = per_device
        self._items: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def _drop(self, key: tuple[str, str]) -> None:
        self._items.pop(key, None)
        left = self._counts.get(key[0], 0) - 1
        if left > 0:
            self._counts[key[0]] = left
        else:
            self._counts.pop(key[0], None)

    def add(self, owner: str, nonce: str, expires: float, now: float) -> bool:
        """False if the nonce was seen before (replay) or there is no room for it."""
        with self._lock:
            while self._items:
                first_key, first_exp = next(iter(self._items.items()))
                if first_exp > now:
                    break
                self._drop(first_key)
            key = (owner, nonce)
            if key in self._items:
                return False
            if self._counts.get(owner, 0) >= self.per_device or len(self._items) >= self.capacity:
                for old in [k for k, e in self._items.items() if e <= now]:
                    self._drop(old)
                if self._counts.get(owner, 0) >= self.per_device or len(self._items) >= self.capacity:
                    log.warning("Nonce cache full for %s; refusing requests until entries expire", owner)
                    return False
            self._items[key] = expires
            self._counts[owner] = self._counts.get(owner, 0) + 1
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.server: LanServer | None = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.pairing: dict | None = None
        self.pairing_error: str | None = None
        self.nonces = NonceCache()
        self.app_server: Any = None
        self.coordinator: threading.Thread | None = None
        self.coordinator_stop = threading.Event()
        self.wake = threading.Event()
        self.merging: set[int] = set()
        self.auto_loop: set[int] = set()
        self.stats = {"requests": 0, "unauthorized": 0}
        self.last_claim: dict[int, float] = {}
        self.requests: dict[int, dict] = {}
        self.request_seq = 0
        self.request_last_by_ip: dict[str, float] = {}
        self.discovery: DiscoveryListener | None = None
        self.beacon: threading.Thread | None = None
        self.beacon_stop = threading.Event()


_STATE = _State()


# --------------------------------------------------------------------------- certificate


def cluster_dir() -> Path:
    path = config.home_dir() / "cluster"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _cert_paths() -> tuple[Path, Path]:
    folder = cluster_dir()
    return folder / "server.crt", folder / "server.key"


def _run_openssl(args: list[str]) -> bytes:
    try:
        proc = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, timeout=OPENSSL_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClusterError(f"Could not run openssl to create the device certificate: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ClusterError(f"openssl failed to create the device certificate: {detail[-1] if detail else proc.returncode}")
    return proc.stdout


def ensure_certificate() -> tuple[Path, Path]:
    """Create the self-signed EC certificate on first use; returns ``(cert, key)``."""
    cert, key = _cert_paths()
    if cert.is_file() and key.is_file() and not cert.is_symlink() and not key.is_symlink():
        return cert, key
    openssl = shutil.which("openssl")
    if not openssl:
        raise ClusterError(
            "Devices need the 'openssl' command to create their encryption certificate, but it was not found. "
            "On Windows install OpenSSL (for example the one that comes with Git for Windows) and add it to PATH, "
            "then turn Devices on again."
        )
    folder = cert.parent
    key_pem = _run_openssl([openssl, "ecparam", "-name", "prime256v1", "-genkey", "-noout"])
    if b"PRIVATE KEY" not in key_pem:
        raise ClusterError("openssl did not return a private key")
    key_tmp = artifacts.write_bytes_atomic(folder / "server.key.new", key_pem, 0o600)
    conf = artifacts.write_text_atomic(
        folder / "openssl-req.cnf",
        "[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = LowRAM AI device server\n",
    )
    try:
        cert_pem = _run_openssl([
            openssl, "req", "-new", "-x509", "-key", str(key_tmp), "-days", "3650", "-sha256", "-config", str(conf),
        ])
        if b"BEGIN CERTIFICATE" not in cert_pem:
            raise ClusterError("openssl did not return a certificate")
        artifacts.write_bytes_atomic(cert, cert_pem, 0o600)
        os.replace(key_tmp, key)
        os.chmod(key, 0o600)
    finally:
        for leftover in (key_tmp, conf):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
    log.info("Created the device certificate in %s", folder)
    return cert, key


def certificate_fingerprint() -> bytes | None:
    cert, _ = _cert_paths()
    try:
        pem = cert.read_text(encoding="ascii")
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _server_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(str(cert), str(key))
    except (ssl.SSLError, OSError) as exc:
        raise ClusterError(
            f"The device certificate cannot be loaded ({exc}). Delete the folder {cert.parent} to create a new one "
            "(all devices then have to pair again)."
        ) from exc
    return ctx


# --------------------------------------------------------------------------- network helpers


def lan_addresses() -> list[str]:
    """This machine's IPv4 addresses that other devices can use (no loopback/link-local)."""
    found: list[ipaddress.IPv4Address] = []
    try:
        interfaces = psutil.net_if_addrs()
    except Exception:  # psutil can fail in sandboxes; then we simply show none
        log.warning("Could not list network interfaces", exc_info=True)
        return []
    for _name, addrs in sorted(interfaces.items()):
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            try:
                ip = ipaddress.IPv4Address(addr.address)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                continue
            if ip not in found:
                found.append(ip)
    found.sort(key=lambda ip: not ip.is_private)  # stable: private LAN addresses first
    return [str(ip) for ip in found]


def _configured_port() -> int:
    port = settings.get("cluster", "port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ClusterError(f"Settings → cluster.port must be a number between 1024 and 65535, got {port!r}")
    return port


# --------------------------------------------------------------------------- LAN server


class LanHandler(BaseHTTPRequestHandler):
    """Serves only ``scope="cluster"`` routes; every call except pairing is HMAC-signed."""

    server: "LanServer"
    server_version = "LowRAM-AI-Devices"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = IDLE_TIMEOUT_S  # every socket read (and keep-alive idling) times out quickly
    lan_device: dict | None = None
    _deadline: threading.Timer | None = None

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.debug("LAN %s %s", self.address_string(), format % args)

    def handle_one_request(self) -> None:
        # Total deadline for request line + headers + body: a client that trickles
        # bytes (slowloris) is cut off even though each single read is fast enough.
        timer = threading.Timer(REQUEST_DEADLINE_S, self._deadline_hit)
        timer.daemon = True
        self._deadline = timer
        timer.start()
        try:
            super().handle_one_request()
        finally:
            timer.cancel()

    def _deadline_hit(self) -> None:
        log.info("Device request from %s did not arrive in time; closing it", self.client_address[0])
        self.close_connection = True
        try:  # plain socket shutdown: wakes the blocked read without touching the TLS object
            socket.socket.shutdown(self.connection, socket.SHUT_RDWR)
        except OSError:
            pass

    def _json(self, status: HTTPStatus, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _drain(self, request: Request | None) -> None:
        """Read an unread request body before an error answer, so closing the
        connection does not reset it (the client would then miss the answer)."""
        if request is None or request._body is not None:
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return
        if length <= 0:
            return
        try:
            self.connection.settimeout(0.5)
            remaining = min(length, MAX_BODY_BYTES)
            while remaining > 0:
                chunk = self.rfile.read1(min(remaining, 65536)) if hasattr(self.rfile, "read1") else b""
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _error(self, request: Request | None, status: HTTPStatus, data: dict) -> None:
        self._drain(request)
        self._json(status, data)

    def _handle(self) -> None:
        with _STATE.lock:
            _STATE.stats["requests"] += 1
        request: Request | None = None
        try:
            request = web.Request.from_handler(self)
            request.body(MAX_BODY_BYTES)  # whole request received (413 above 1 MiB) …
            if self._deadline is not None:
                self._deadline.cancel()  # … so the deadline no longer applies to the handler
            found = web.match(self.command, request.path, LAN_SCOPE)
            if not (found is not None and request.path in PUBLIC_LAN_PATHS):
                self.lan_device = _authenticate(request)
            if found is None:
                if web.path_exists(request.path, LAN_SCOPE):
                    raise HTTPError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
                raise HTTPError(HTTPStatus.NOT_FOUND, "Not found")
            route_, args = found
            result = route_.func(request, *args)
            if not isinstance(result, web.Response):  # pragma: no cover - programming error
                raise TypeError(f"LAN route {route_.func.__name__} must return a Response")
            self._json(result.status, result.data)
        except _Unauthorized:
            with _STATE.lock:
                _STATE.stats["unauthorized"] += 1
            log.warning("Rejected an unauthenticated device request from %s", self.client_address[0])
            self._error(request, HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
        except HTTPError as exc:
            self._error(request, exc.status, {"error": exc.message})
        except db.NotFound as exc:
            self._error(request, HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except db.Busy as exc:
            self._error(request, HTTPStatus.CONFLICT, {"error": str(exc)})
        except (artifacts.ArtifactError, db.InvalidTransition) as exc:
            self._error(request, HTTPStatus.CONFLICT, {"error": str(exc)})
        except ValueError as exc:
            self._error(request, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            log.exception("Unhandled error for LAN %s %s", self.command, self.path)
            self._error(request, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal error on the host; see its log"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle  # noqa: N815


class LanServer(web.QuickBindMixin, ThreadingHTTPServer):
    """TLS server for devices. The handshake runs in the per-connection thread
    (with a timeout), so a silent client cannot block ``accept``."""

    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], ssl_context: ssl.SSLContext):
        self.ssl_context = ssl_context
        self._slots = threading.BoundedSemaphore(MAX_LAN_CONNECTIONS)
        self._per_ip: dict[str, int] = {}
        self._ip_lock = threading.Lock()
        super().__init__(address, LanHandler)

    def _release(self, ip: str) -> None:
        with self._ip_lock:
            left = self._per_ip.get(ip, 1) - 1
            if left > 0:
                self._per_ip[ip] = left
            else:
                self._per_ip.pop(ip, None)
        self._slots.release()

    def process_request(self, request, client_address) -> None:  # type: ignore[override]
        ip = client_address[0]
        with self._ip_lock:
            refused = self._per_ip.get(ip, 0) >= MAX_CONNECTIONS_PER_IP or not self._slots.acquire(blocking=False)
            if not refused:
                self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
        if refused:
            log.warning("Too many device connections from %s; refusing", ip)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(ip)
            raise

    def process_request_thread(self, request, client_address) -> None:  # type: ignore[override]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def finish_request(self, request, client_address) -> None:  # type: ignore[override]
        request.settimeout(HANDSHAKE_TIMEOUT_S)
        try:
            tls = self.ssl_context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log.info("TLS handshake with %s failed: %s", client_address[0], exc)
            return
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def handle_error(self, request, client_address) -> None:  # type: ignore[override]
        log.debug("Device connection from %s ended with an error", client_address[0], exc_info=True)


def is_listening() -> bool:
    with _STATE.lock:
        return _STATE.server is not None


def listening_port() -> int | None:
    with _STATE.lock:
        return _STATE.server.server_address[1] if _STATE.server else None


def start_lan_server() -> None:
    """Start the TLS device server (idempotent). Raises :class:`ClusterError`."""
    with _STATE.lock:
        if _STATE.server is not None:
            return
        try:
            port = _configured_port()
            cert, key = ensure_certificate()
            context = _server_context(cert, key)
            try:
                server = LanServer((LAN_BIND_HOST, port), context)
            except OSError as exc:
                raise ClusterError(
                    f"Could not listen for devices on port {port}: {exc.strerror or exc}. "
                    "Another program may use it; choose another port in Settings (cluster.port)."
                ) from exc
        except ClusterError as exc:
            _STATE.error = str(exc)
            raise
        _STATE.server, _STATE.error = server, None
        _STATE.thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="lowram-devices", daemon=True,
        )
        _STATE.thread.start()
    log.info("Device server listening on %s:%s", LAN_BIND_HOST, server.server_address[1])
    _ensure_coordinator()
    _start_beacon()


def stop_lan_server() -> None:
    _stop_beacon()
    with _STATE.lock:
        server, thread = _STATE.server, _STATE.thread
        _STATE.server, _STATE.thread, _STATE.pairing = None, None, None
    if server is not None:
        server.shutdown()
        server.server_close()
    if thread is not None:
        thread.join(5)


# --------------------------------------------------------------------------- authentication


def _load_device(device_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def _authenticate(request: Request) -> dict:
    headers = request.headers
    device_text = (headers.get(HEADER_DEVICE) or "").strip()
    timestamp = (headers.get(HEADER_TIMESTAMP) or "").strip()
    nonce = (headers.get(HEADER_NONCE) or "").strip()
    signature = (headers.get(HEADER_SIGNATURE) or "").strip()
    if not (
        device_text.isdigit() and len(device_text) <= 12 and timestamp.isdigit() and len(timestamp) <= 12
        and _NONCE_RE.fullmatch(nonce) and _SIG_RE.fullmatch(signature)
    ):
        raise _Unauthorized
    body = request.body(MAX_BODY_BYTES)  # 413 above 1 MiB
    now = _now()
    if abs(now - int(timestamp)) > MAX_SKEW_S:
        raise _Unauthorized
    device = _load_device(int(device_text))
    if device is None:
        raise _Unauthorized
    try:
        key = base64.b64decode(device["key_b64"], validate=True)
    except (binascii.Error, ValueError):
        raise _Unauthorized from None
    expected = sign(key, request.method, request.handler.path, timestamp, nonce, body)
    if not hmac.compare_digest(expected, signature.lower()):
        raise _Unauthorized
    if not _STATE.nonces.add(str(device["id"]), nonce, int(timestamp) + MAX_SKEW_S + 1, now):
        raise _Unauthorized
    with db.connect() as conn:
        conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, device["id"]))
    device["last_seen"] = now
    return device


def _device(req: Request) -> dict:
    device = getattr(req.handler, "lan_device", None)
    if not device:  # pragma: no cover - the handler authenticates before dispatch
        raise _Unauthorized
    return device


def _device_lease_alive(owner: str) -> bool:
    """Owner check for ``device:<id>`` leases (backstop for db.recover_stale)."""
    try:
        device_id = int(owner[len(REMOTE):])
    except ValueError:
        return False
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT last_seen FROM devices WHERE id = ?", (device_id,)).fetchone()
    except sqlite3.Error:
        log.warning("Could not check the lease of %s; keeping it", owner, exc_info=True)
        return True
    if row is None:
        return False
    return _now() - (row["last_seen"] or 0) < LEASE_DEAD_S


db.register_owner_check(REMOTE, _device_lease_alive)


# --------------------------------------------------------------------------- task leases


def _owner(device_id: int) -> str:
    return f"{REMOTE}{int(device_id)}"


def _release_locked(conn: sqlite3.Connection, task_id: int, attempts: int, note: str, *, count: bool = False) -> str:
    """Inside a transaction: give a device-leased task back.

    Claims do not count as attempts (a device cannot burn tasks by claiming and
    releasing). ``count=True`` is used when a device held the task and went
    silent (lease expiry): that counts, and after MAX_ATTEMPTS the task fails.
    """
    conn.execute("DELETE FROM device_claims WHERE task_id = ?", (task_id,))
    if count:
        attempts += 1
        conn.execute("UPDATE tasks SET attempts = ? WHERE id = ?", (attempts, task_id))
    if count and attempts >= MAX_ATTEMPTS:
        conn.execute(
            "UPDATE tasks SET status = 'failed', lease_owner = NULL, finished_at = ?, error = ? "
            "WHERE id = ? AND status = 'running'",
            (db.utcnow(), f"{note}. Gave up after {attempts} attempts; use retry to queue it again."[:db.MAX_ERROR_CHARS], task_id),
        )
        return "failed"
    conn.execute(
        "UPDATE tasks SET status = 'pending', lease_owner = NULL, started_at = NULL, finished_at = NULL, "
        "result_path = NULL, error = ? WHERE id = ? AND status = 'running'",
        (f"{note}; queued again automatically ({attempts} of {MAX_ATTEMPTS} attempts used)."[:db.MAX_ERROR_CHARS], task_id),
    )
    return "pending"


def release_device_tasks(device_id: int | None, note: str) -> list[int]:
    """Give back every task leased to one device (or to all devices when ``device_id`` is None)."""
    released = []
    with db.connect() as conn, db.transaction(conn):
        if device_id is None:
            rows = conn.execute(
                "SELECT id, attempts FROM tasks WHERE status = 'running' AND lease_owner LIKE ?", (REMOTE + "%",)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, attempts FROM tasks WHERE status = 'running' AND lease_owner = ?", (_owner(device_id),)
            ).fetchall()
        for row in rows:
            _release_locked(conn, row["id"], row["attempts"], note)
            released.append(int(row["id"]))
    if released:
        _STATE.wake.set()
    return released


def sweep_leases() -> list[int]:
    """Requeue tasks of devices that stopped sending heartbeats (or were removed)."""
    cutoff = _now() - LEASE_REQUEUE_S
    changed = []
    with db.connect() as conn, db.transaction(conn):
        rows = conn.execute(
            "SELECT t.id, t.attempts, t.lease_owner, d.id AS device_id, d.name, d.last_seen FROM tasks t "
            "LEFT JOIN devices d ON t.lease_owner = 'device:' || d.id "
            "WHERE t.status = 'running' AND t.lease_owner LIKE ?",
            (REMOTE + "%",),
        ).fetchall()
        for row in rows:
            if row["device_id"] is not None and (row["last_seen"] or 0) >= cutoff:
                continue
            if row["device_id"] is None:
                note = "The device working on this task was removed"
            else:
                note = f"Device {row['name']!r} stopped responding for more than {LEASE_REQUEUE_S} s"
            _release_locked(conn, row["id"], row["attempts"], note, count=True)
            changed.append(int(row["id"]))
    if changed:
        log.warning("Device lease(s) expired for task(s) %s", changed)
    return changed


def _clamp_int(value: object, low: int, high: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(max(low, min(high, int(value))))
    except (OverflowError, ValueError):
        return default


def claim_for_device(device: dict, context_chars: int, max_tokens: int) -> dict | None:
    """Atomically lease the next pending task of a distributed project to ``device``."""
    owner = _owner(device["id"])
    with db.connect() as conn:
        with db.transaction(conn):
            # A worker runs one task at a time; anything it still holds was abandoned
            # (e.g. it restarted or lost our previous answer).
            for row in conn.execute(
                "SELECT id, attempts FROM tasks WHERE status = 'running' AND lease_owner = ?", (owner,)
            ).fetchall():
                _release_locked(conn, row["id"], row["attempts"], f"Device {device['name']!r} asked for new work without finishing this task")
            row = conn.execute(
                "SELECT t.id FROM tasks t JOIN distributed_projects dp ON dp.project_id = t.project_id "
                "JOIN projects p ON p.id = t.project_id "
                "WHERE t.status = 'pending' AND p.kind = 'general' "
                "ORDER BY dp.created_at, dp.project_id, t.position, t.id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE tasks SET status = 'running', lease_owner = ?, started_at = ?, finished_at = NULL, "
                "error = NULL WHERE id = ? AND status = 'pending'",
                (owner, db.utcnow(), row["id"]),
            )
        task = db.task_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone())
    skipped: list[int] = []
    context = runner.build_context(task["project_id"], task, context_chars, skipped)
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO device_claims (task_id, device_id, claimed_at, max_tokens, context_chars, skipped) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task["id"], device["id"], _now(), max_tokens, len(context), json.dumps(skipped)),
        )
    log.info("Task #%s leased to device %r", task["id"], device["name"])
    return {
        "task_id": task["id"],
        "project_id": task["project_id"],
        "position": task["position"],
        "title": task["title"],
        "context": context,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": runner._messages(task, context),
    }


def _held_task(conn: sqlite3.Connection, task_id: int, device_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise db.NotFound(f"Task #{task_id} not found")
    if row["status"] != "running" or row["lease_owner"] != _owner(device_id):
        raise HTTPError(
            HTTPStatus.CONFLICT,
            f"This device no longer holds task #{task_id} (it was given to another device or finished); "
            "the answer was discarded.",
        )
    return row


def submit_result(device: dict, task_id: int, content: str, duration_s: float | None, model: str | None) -> dict:
    """Store a device's answer exactly like a local result (artifact + sidecar) and finish the task."""
    with db.connect() as conn:
        task = db.task_dict(_held_task(conn, task_id, device["id"]))
        claim = conn.execute("SELECT * FROM device_claims WHERE task_id = ?", (task_id,)).fetchone()
        dist = conn.execute(
            "SELECT percent FROM distributed_projects WHERE project_id = ?", (task["project_id"],)
        ).fetchone()
    metadata = {
        "project_id": task["project_id"],
        "task_id": task_id,
        "position": task["position"],
        "title": task["title"],
        "model": model,
        "endpoint": f"device #{device['id']} ({device['name']})",
        "percent": dist["percent"] if dist else None,
        "max_tokens": claim["max_tokens"] if claim else None,
        "context_chars": claim["context_chars"] if claim else None,
        "context_skipped_task_ids": json.loads(claim["skipped"]) if claim else [],
        "duration_s": duration_s,
        "created_at": db.utcnow(),
        "device": {"id": device["id"], "name": device["name"], "platform": device["platform"]},
    }
    # A name per attempt: a late answer can never overwrite the result of whoever holds the task now.
    folder = artifacts.project_dir(task["project_id"])
    stem = f"task_{int(task_id)}_device{int(device['id'])}_{secrets.token_hex(4)}"
    path = artifacts.write_text_atomic(folder / f"{stem}.md", content.rstrip() + "\n")
    sidecar = artifacts.write_json_atomic(folder / f"{stem}.json", metadata)
    with db.connect() as conn:
        with db.transaction(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'done', result_path = ?, error = NULL, finished_at = ?, lease_owner = NULL, "
                "attempts = attempts + 1 WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (str(path), db.utcnow(), task_id, _owner(device["id"])),
            )
            if cur.rowcount != 1:
                for leftover in (path, sidecar):
                    leftover.unlink(missing_ok=True)
                raise HTTPError(HTTPStatus.CONFLICT, f"This device no longer holds task #{task_id}; the answer was discarded.")
            conn.execute(
                "UPDATE devices SET tasks_done = tasks_done + 1, model = COALESCE(?, model) WHERE id = ?",
                (model, device["id"]),
            )
            conn.execute("DELETE FROM device_claims WHERE task_id = ?", (task_id,))
        done = db.task_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    log.info("Device %r finished task #%s", device["name"], task_id)
    _STATE.wake.set()
    return done


def submit_failure(device: dict, task_id: int, error: str, release: bool) -> dict:
    with db.connect() as conn:
        with db.transaction(conn):
            row = _held_task(conn, task_id, device["id"])
            if release:
                _release_locked(conn, task_id, row["attempts"], f"Device {device['name']!r} stopped before finishing ({error})")
            else:
                conn.execute("DELETE FROM device_claims WHERE task_id = ?", (task_id,))
                conn.execute(
                    "UPDATE tasks SET status = 'failed', lease_owner = NULL, finished_at = ?, error = ?, "
                    "attempts = attempts + 1 WHERE id = ? AND status = 'running'",
                    (db.utcnow(), f"Failed on device {device['name']!r}: {error}"[:db.MAX_ERROR_CHARS], task_id),
                )
        task = db.task_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    _STATE.wake.set()
    return task


# --------------------------------------------------------------------------- devices


def _running_by_device() -> dict[int, int]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, lease_owner FROM tasks WHERE status = 'running' AND lease_owner LIKE ? ORDER BY id",
            (REMOTE + "%",),
        ).fetchall()
    held: dict[int, int] = {}
    for row in rows:
        try:
            held.setdefault(int(row["lease_owner"][len(REMOTE):]), int(row["id"]))
        except ValueError:
            continue
    return held


def list_devices() -> list[dict]:
    """Public view of paired devices (never includes the key)."""
    held = _running_by_device()
    now = _now()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
    devices = []
    for row in rows:
        current = held.get(row["id"])
        recent = bool(row["last_seen"]) and now - row["last_seen"] < ONLINE_WINDOW_S
        devices.append({
            "id": row["id"],
            "name": row["name"],
            "status": "busy" if current is not None else ("online" if recent else "offline"),
            "platform": row["platform"],
            "ram_total_bytes": row["ram_total_bytes"],
            "model": row["model"],
            "last_seen": _iso(row["last_seen"]),
            "tasks_done": row["tasks_done"],
            "current_task_id": current,
            "paired_at": row["paired_at"],
        })
    return devices


def revoke_device(device_id: int) -> None:
    with db.connect() as conn, db.transaction(conn):
        device = conn.execute("SELECT id, name FROM devices WHERE id = ?", (device_id,)).fetchone()
        if device is None:
            raise db.NotFound(f"Device #{device_id} not found")
        for row in conn.execute(
            "SELECT id, attempts FROM tasks WHERE status = 'running' AND lease_owner = ?", (_owner(device_id),)
        ).fetchall():
            _release_locked(conn, row["id"], row["attempts"], f"Device {device['name']!r} was removed")
        conn.execute("DELETE FROM device_claims WHERE device_id = ?", (device_id,))
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))  # the key is gone with the row
    log.info("Device #%s removed", device_id)
    _STATE.wake.set()


# --------------------------------------------------------------------------- pairing


def create_pairing() -> dict:
    """New one-time pairing code (replaces an older one). Needs the device server running."""
    port = listening_port()
    fingerprint = certificate_fingerprint()
    if port is None or fingerprint is None:
        raise HTTPError(HTTPStatus.CONFLICT, "Turn on Devices first; the pairing code needs the device server.")
    addresses = lan_addresses() or ["127.0.0.1"]
    secret = secrets.token_bytes(16)
    code = make_pairing_code(addresses, port, fingerprint, secret)
    expires = _now() + PAIRING_TTL_S
    with _STATE.lock:
        _STATE.pairing = {"secret": secret, "code": code, "expires": expires, "failures": 0}
        _STATE.pairing_error = None
    return {"code": code, "expires_at": _iso(expires)}


def _active_pairing() -> dict | None:
    with _STATE.lock:
        pairing = _STATE.pairing
        if pairing is not None and pairing["expires"] <= _now():
            _STATE.pairing = pairing = None
        return pairing


def _decode_secret(value: object) -> bytes | None:
    if not isinstance(value, str) or not _SECRET_RE.fullmatch(value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "==")
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == 16 else None


def encode_secret(secret: bytes) -> str:
    return base64.urlsafe_b64encode(secret).decode("ascii").rstrip("=")


def _pair(data: dict, client_ip: str) -> dict:
    name, platform_text, ram = _validated_device_info(data)
    secret = _decode_secret(data.get("secret"))
    with _STATE.lock:
        pairing = _active_pairing()
        if pairing is None or secret is None or not hmac.compare_digest(secret, pairing["secret"]):
            if pairing is not None:
                pairing["failures"] += 1
                log.warning(
                    "Wrong pairing code from %s (%d of %d attempts)", client_ip, pairing["failures"], PAIRING_MAX_FAILURES
                )
                if pairing["failures"] >= PAIRING_MAX_FAILURES:
                    _STATE.pairing = None
                    _STATE.pairing_error = (
                        f"The pairing code was invalidated after {PAIRING_MAX_FAILURES} wrong attempts. If that was "
                        "not you, another computer on your network tried to join. Create a new code when ready."
                    )
            raise HTTPError(HTTPStatus.UNAUTHORIZED, _PAIR_FAILED)
        with db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        if count >= MAX_DEVICES:
            raise HTTPError(HTTPStatus.CONFLICT, f"At most {MAX_DEVICES} devices can be paired; remove one first.")
        _STATE.pairing = None  # one-time
    device_id, key = _insert_device(name, platform_text, ram)
    log.info("Paired device #%s (%s) from %s", device_id, name, client_ip)
    return {
        "device_id": device_id,
        "device_key": base64.b64encode(key).decode("ascii"),
        "host_name": host_display_name(),
        "server_time": int(_now()),
    }


# --------------------------------------------------------------------------- discovery (UDP beacons)


@dataclass(frozen=True)
class NearbyHost:
    id: str
    name: str
    addresses: tuple[str, ...]
    port: int
    fp_prefix: bytes  # first 8 bytes of the certificate SHA-256 (from the beacon; unauthenticated)


_HOST_IDS: dict[str, str] = {}
_ID_RE = re.compile(r"[0-9a-f]{8,32}")
_FP16_RE = re.compile(r"[0-9a-f]{16}")


def host_id() -> str:
    """Stable, non-secret id of this installation (``<home>/cluster/host_id``)."""
    path = cluster_dir() / "host_id"
    cached = _HOST_IDS.get(str(path))
    if cached:
        return cached
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    if not re.fullmatch(r"[0-9a-f]{16}", text):
        text = secrets.token_hex(8)
        artifacts.write_text_atomic(path, text + "\n")
    _HOST_IDS[str(path)] = text
    return text


def encode_beacon(hid: str, name: str, port: int, fingerprint: bytes) -> bytes:
    name = clean_text(name, 64)
    while True:
        raw = json.dumps(
            {"app": "lowram", "v": 1, "id": hid, "name": name, "port": port, "fp": fingerprint.hex()[:16]},
            separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
        if len(raw) <= MAX_BEACON_BYTES or not name:
            return raw
        name = name[:-8]


def decode_beacon(raw: bytes) -> dict | None:
    """Validated beacon fields, or None for anything that is not a well-formed LowRAM beacon."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BEACON_BYTES:
        return None
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("app") != "lowram" or data.get("v") != 1:
        return None
    hid, port, fp, name = data.get("id"), data.get("port"), data.get("fp"), data.get("name")
    if not isinstance(hid, str) or not _ID_RE.fullmatch(hid):
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        return None
    if not isinstance(fp, str) or not _FP16_RE.fullmatch(fp):
        return None
    return {"id": hid, "name": clean_device_name(name if isinstance(name, str) else None, "LowRAM AI"),
            "port": port, "fp": fp}


def _listener_own_id() -> str:
    return host_id()


class DiscoveryListener:
    """Collects beacons of hosts seen in the last NEARBY_TTL_S seconds. Never raises."""

    def __init__(self, port: int, bind_host: str = "", own_id: str | None = None):
        self.port, self.bind_host, self.own_id = port, bind_host, own_id
        self.error: str | None = None
        self._hosts: dict[str, dict] = {}
        self._last_packet: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind((self.bind_host, self.port))
        except OSError as exc:
            sock.close()
            self.error = f"Cannot listen for nearby devices on UDP port {self.port}: {exc.strerror or exc}"
            log.warning("%s", self.error)
            return False
        try:
            mreq = socket.inet_aton(DISCOVERY_GROUP) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass  # multicast is optional; broadcast still works
        sock.settimeout(0.5)
        self._sock = sock
        self._thread = threading.Thread(target=self._run, name="lowram-discovery", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(3)
        if self._sock is not None:
            self._sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.5)
                continue
            try:
                self.handle(data, addr[0])
            except Exception:  # a hostile packet must never stop discovery
                log.debug("Ignoring a bad discovery packet from %s", addr[0], exc_info=True)

    def handle(self, data: bytes, ip: str) -> bool:
        mono = time.monotonic()
        with self._lock:
            last = self._last_packet.get(ip)
            if last is not None and mono - last < BEACON_MIN_GAP_S:
                return False  # rate limit per source address
            if len(self._last_packet) > 1024:
                self._last_packet.clear()
            self._last_packet[ip] = mono
        beacon = decode_beacon(data)
        if beacon is None or beacon["id"] == (self.own_id or _listener_own_id()):
            return False
        now = _now()
        with self._lock:
            entry = self._hosts.get(beacon["id"])
            if entry is None or entry["fp"] != beacon["fp"]:
                self._prune(now)
                if len(self._hosts) >= MAX_NEARBY:
                    return False
                entry = self._hosts[beacon["id"]] = {"id": beacon["id"], "addresses": {}}
            entry.update(name=beacon["name"], port=beacon["port"], fp=beacon["fp"], last_seen=now)
            entry["addresses"][ip] = now
            if len(entry["addresses"]) > 8:
                oldest = min(entry["addresses"], key=entry["addresses"].get)
                entry["addresses"].pop(oldest)
        return True

    def _prune(self, now: float) -> None:
        for key in [k for k, e in self._hosts.items() if now - e["last_seen"] > NEARBY_TTL_S]:
            self._hosts.pop(key)

    def hosts(self) -> list[dict]:
        now = _now()
        with self._lock:
            self._prune(now)
            result = []
            for entry in sorted(self._hosts.values(), key=lambda e: e["name"].lower()):
                fresh = [ip for ip, seen in sorted(entry["addresses"].items(), key=lambda kv: -kv[1])
                         if now - seen <= NEARBY_TTL_S]
                result.append({"id": entry["id"], "name": entry["name"], "addresses": fresh, "port": entry["port"],
                               "fingerprint_short": entry["fp"], "last_seen": _iso(entry["last_seen"])})
            return result


def start_discovery_listener() -> None:
    with _STATE.lock:
        if _STATE.discovery is not None:
            return
        listener = DiscoveryListener(DISCOVERY_PORT, DISCOVERY_BIND)
        listener.start()
        _STATE.discovery = listener


def stop_discovery_listener() -> None:
    with _STATE.lock:
        listener, _STATE.discovery = _STATE.discovery, None
    if listener is not None:
        listener.stop()


def nearby_hosts() -> list[dict]:
    with _STATE.lock:
        listener = _STATE.discovery
    return listener.hosts() if listener is not None else []


def find_nearby(host_id_value: object) -> NearbyHost:
    if not isinstance(host_id_value, str) or not _ID_RE.fullmatch(host_id_value):
        raise ValueError("host_id must be the id of a nearby device")
    for host in nearby_hosts():
        if host["id"] == host_id_value and host["addresses"]:
            return NearbyHost(host["id"], host["name"], tuple(host["addresses"]), host["port"],
                              bytes.fromhex(host["fingerprint_short"]))
    raise HTTPError(
        HTTPStatus.NOT_FOUND,
        "That device is not visible any more. Make sure both computers are on the same Wi-Fi (some routers "
        "isolate Wi-Fi clients) and Devices is on there – or use the pairing code instead.",
    )


def _beacon_targets() -> list[tuple[str, int]]:
    """Broadcast address of every LAN interface, the global broadcast and the multicast group."""
    targets: list[tuple[str, int]] = []
    try:
        interfaces = psutil.net_if_addrs()
    except Exception:
        interfaces = {}
    for _name, addrs in sorted(interfaces.items()):
        for addr in addrs:
            if addr.family != socket.AF_INET or not addr.broadcast:
                continue
            try:
                ip = ipaddress.IPv4Address(addr.address)
                ipaddress.IPv4Address(addr.broadcast)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local:
                continue
            if (addr.broadcast, DISCOVERY_PORT) not in targets:
                targets.append((addr.broadcast, DISCOVERY_PORT))
    targets += [("255.255.255.255", DISCOVERY_PORT), (DISCOVERY_GROUP, DISCOVERY_PORT)]
    return targets


def _beacon_main(stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        except OSError:
            pass
        while not stop.is_set():
            try:
                port, fingerprint = listening_port(), certificate_fingerprint()
                if port is not None and fingerprint is not None and settings.get("cluster", "discoverable"):
                    payload = encode_beacon(host_id(), host_display_name(), port, fingerprint)
                    for target in _beacon_targets():
                        try:
                            sock.sendto(payload, target)
                        except OSError:
                            pass  # e.g. no route for one interface; the others still work
            except Exception:
                log.debug("Beacon round failed", exc_info=True)
            stop.wait(BEACON_INTERVAL_S)
    finally:
        sock.close()


def _start_beacon() -> None:
    with _STATE.lock:
        if _STATE.beacon is not None and _STATE.beacon.is_alive():
            return
        _STATE.beacon_stop = threading.Event()
        _STATE.beacon = threading.Thread(target=_beacon_main, args=(_STATE.beacon_stop,),
                                         name="lowram-beacon", daemon=True)
        _STATE.beacon.start()


def _stop_beacon() -> None:
    with _STATE.lock:
        thread, _STATE.beacon = _STATE.beacon, None
        _STATE.beacon_stop.set()
    if thread is not None:
        thread.join(3)


# --------------------------------------------------------------------------- pairing requests (numeric comparison)
#
# 1. helper → pair-request {…, client_commit = sha256(client_nonce)}   → {request_id, host_nonce}
# 2. helper → pair-status {request_id, client_nonce}   (reveals the nonce; polled every 2 s)
# Both sides show verification_code(full certificate fingerprint, client_nonce, host_nonce).
# The commitment stops a man-in-the-middle from picking nonces until the two numbers match.


def verification_code(fingerprint: bytes, client_nonce: bytes, host_nonce: bytes) -> str:
    number = int.from_bytes(hashlib.sha256(fingerprint + client_nonce + host_nonce).digest()[:8], "big") % 1_000_000
    text = f"{number:06d}"
    return f"{text[:3]} {text[3:]}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _from_b64url(value: object, length: int) -> bytes | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == length else None


def _purge_requests(now: float) -> None:
    """Inside _STATE.lock: expire old requests; forget them 2 minutes later."""
    for rid, item in list(_STATE.requests.items()):
        if item["state"] in ("pending", "approved") and not item["delivered"] and now > item["expires"]:
            item["state"] = "expired"
        if now > item["expires"] + REQUEST_TTL_S:
            _STATE.requests.pop(rid)


def _validated_device_info(data: dict) -> tuple[str, str, int | None]:
    name = clean_device_name(data.get("name"))
    platform_text = clean_text(data.get("platform") or "", 64)
    ram = data.get("ram_total_bytes")
    if ram is not None and (isinstance(ram, bool) or not isinstance(ram, int) or not 0 <= ram <= 2**50):
        raise ValueError("ram_total_bytes must be a whole number of bytes")
    return name, platform_text, ram


def _device_count() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0])


def _insert_device(name: str, platform_text: str, ram: int | None) -> tuple[int, bytes]:
    key = secrets.token_bytes(32)
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO devices (name, key_b64, platform, ram_total_bytes, paired_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (name, base64.b64encode(key).decode("ascii"), platform_text, ram, db.utcnow(), _now()),
        )
        return int(cur.lastrowid), key


def _too_many(message: str, retry_after: float) -> HTTPError:
    return HTTPError(HTTPStatus.TOO_MANY_REQUESTS, f"{message} (try again in {max(1, round(retry_after))} s)")


def _pair_request(data: dict, ip: str) -> dict:
    if not settings.get("cluster", "discoverable"):
        raise HTTPError(HTTPStatus.FORBIDDEN, "This computer does not accept pairing requests (it is hidden). "
                                              "Use the pairing code from its Devices page instead.")
    name, platform_text, ram = _validated_device_info(data)
    commit = _from_b64url(data.get("client_commit"), 32)
    if commit is None:
        raise ValueError("client_commit must be the base64url SHA-256 of the client nonce")
    if _device_count() >= MAX_DEVICES:
        raise HTTPError(HTTPStatus.CONFLICT, f"At most {MAX_DEVICES} devices can be paired; remove one first.")
    now, mono = _now(), time.monotonic()
    with _STATE.lock:
        _purge_requests(now)
        last = _STATE.request_last_by_ip.get(ip)
        if last is not None and mono - last < REQUEST_MIN_INTERVAL_S:
            raise _too_many("Too many pairing requests from this computer", REQUEST_MIN_INTERVAL_S - (mono - last))
        for item in _STATE.requests.values():  # one open request per address: a new one replaces it
            if item["address"] == ip and item["state"] == "pending":
                item["state"], item["expires"] = "expired", now
        pending = [r for r in _STATE.requests.values() if r["state"] == "pending"]
        if len(pending) >= MAX_PENDING_REQUESTS:
            raise _too_many("Too many pairing requests are waiting on the host", min(r["expires"] for r in pending) - now)
        if len(_STATE.request_last_by_ip) > 256:
            _STATE.request_last_by_ip.clear()
        _STATE.request_last_by_ip[ip] = mono
        _STATE.request_seq += 1
        rid = _STATE.request_seq
        host_nonce = secrets.token_bytes(16)
        _STATE.requests[rid] = {
            "id": rid, "name": name, "platform": platform_text, "ram_total_bytes": ram, "address": ip,
            "commit": commit, "client_nonce": None, "host_nonce": host_nonce, "code": None,
            "state": "pending", "created": now, "expires": now + REQUEST_TTL_S, "delivered": False, "device_id": None,
        }
    log.info("Pairing request #%s from %s (%s)", rid, ip, name)
    return {"request_id": rid, "host_nonce": _b64url(host_nonce), "expires_in": REQUEST_TTL_S}


def _pair_status(data: dict) -> dict:
    rid = data.get("request_id")
    nonce = _from_b64url(data.get("client_nonce"), 16)
    unknown = HTTPError(HTTPStatus.NOT_FOUND, "Unknown or expired pairing request; start pairing again.")
    if isinstance(rid, bool) or not isinstance(rid, int) or nonce is None:
        raise unknown
    now = _now()
    with _STATE.lock:
        _purge_requests(now)
        item = _STATE.requests.get(rid)
        if item is None or not hmac.compare_digest(hashlib.sha256(nonce).digest(), item["commit"]):
            raise unknown  # only the helper that made the request knows the nonce
        if item["client_nonce"] is None and item["state"] == "pending":
            item["client_nonce"] = nonce
            item["code"] = verification_code(certificate_fingerprint() or b"", nonce, item["host_nonce"])
            power.notify("Device wants to join", f"{item['name']} wants to join – check the number {item['code']}")
        if item["state"] != "approved":
            return {"state": item["state"]}
        if item["delivered"]:
            return {"state": "approved", "device_id": item["device_id"]}  # the key is handed out once
        if _device_count() >= MAX_DEVICES:
            item["state"] = "denied"
            return {"state": "denied"}
        device_id, key = _insert_device(item["name"], item["platform"], item["ram_total_bytes"])
        item.update(delivered=True, device_id=device_id)
    log.info("Paired device #%s (%s) via request #%s", device_id, item["name"], rid)
    return {"state": "approved", "device_id": device_id, "device_key": base64.b64encode(key).decode("ascii"),
            "host_name": host_display_name(), "server_time": int(now)}


def pending_requests() -> list[dict]:
    now = _now()
    with _STATE.lock:
        _purge_requests(now)
        return [
            {"id": r["id"], "name": r["name"], "platform": r["platform"], "ram_total_bytes": r["ram_total_bytes"],
             "address": r["address"], "verification_code": r["code"], "expires_at": _iso(r["expires"])}
            for r in sorted(_STATE.requests.values(), key=lambda r: r["id"]) if r["state"] == "pending"
        ]


def answer_request(request_id: int, approve: bool) -> None:
    now = _now()
    with _STATE.lock:
        _purge_requests(now)
        item = _STATE.requests.get(request_id)
        if item is None or item["state"] != "pending":
            raise db.NotFound(f"Pairing request #{request_id} no longer exists (it expired or was answered)")
        if approve and item["code"] is None:
            raise HTTPError(HTTPStatus.CONFLICT, "The helper has not finished its request yet; wait a moment.")
        if approve:
            item["state"], item["expires"] = "approved", max(item["expires"], now + 30)
        else:
            item["state"] = "denied"
    log.info("Pairing request #%s %s", request_id, "approved" if approve else "denied")


# --------------------------------------------------------------------------- distributed projects and merge


def _distributed_row(project_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM distributed_projects WHERE project_id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def _set_merge_state(project_id: int, state: str, *, error: str | None = None, final_path: str | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE distributed_projects SET merge_state = ?, merge_error = ?, final_path = COALESCE(?, final_path), "
            "updated_at = ? WHERE project_id = ?",
            (state, error[:db.MAX_ERROR_CHARS] if error else None, final_path, db.utcnow(), project_id),
        )


def distribute(project_id: int, percent: int, merge: bool) -> dict:
    """Mark a general project as shared with devices (idempotent; resets the merge state)."""
    runner.require_general(project_id)
    pct = memory.validate_percent(percent)
    current = _distributed_row(project_id)
    if current and current["merge_state"] == "running":
        raise db.Busy(f"The results of project #{project_id} are being combined right now; try again afterwards.")
    now = db.utcnow()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO distributed_projects (project_id, percent, merge, merge_state, merge_error, final_path, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET percent = excluded.percent, merge = excluded.merge, "
            "merge_state = excluded.merge_state, merge_error = NULL, final_path = NULL, updated_at = excluded.updated_at",
            (project_id, pct, int(bool(merge)), "waiting" if merge else "off", now, now),
        )
    _ensure_coordinator()
    _STATE.wake.set()
    return _distributed_row(project_id) or {}


def distributed_projects() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT dp.*, p.name FROM distributed_projects dp JOIN projects p ON p.id = dp.project_id "
            "ORDER BY dp.created_at, dp.project_id"
        ).fetchall()
    result = []
    for row in rows:
        project = db.get_project(row["project_id"])
        result.append({
            "project_id": row["project_id"],
            "name": row["name"],
            "percent": row["percent"],
            "merge": bool(row["merge"]),
            "merge_state": row["merge_state"],
            "merge_error": row["merge_error"],
            "final_available": row["merge_state"] == "done" and bool(row["final_path"]),
            "counts": project["counts"],
            "total": project["total"],
        })
    return result


def local_llm_url(timeout: float = LLM_START_TIMEOUT_S) -> str:
    """URL of this machine's model: LOWRAM_LLM_URL, else the managed engine, else the default."""
    if os.environ.get("LOWRAM_LLM_URL", "").strip():
        return config.llm_url()
    try:
        from . import engine  # built in parallel; optional
    except ImportError:
        return config.llm_url()
    return engine.ensure_running(timeout=timeout)


def _merge_prompt(project: dict, done: list[dict], failed: list[dict], budget: int) -> tuple[str, dict]:
    texts = []
    unreadable = []
    for task in done:
        try:
            texts.append((task, artifacts.read_result(task["result_path"]).strip()))
        except artifacts.ArtifactError as exc:
            log.warning("Merge of project #%s skips task #%s: %s", project["id"], task["id"], exc)
            unreadable.append(task["id"])
    if not texts:
        raise artifacts.ArtifactError("None of the finished results could be read; nothing to combine.")
    overhead = 200 + sum(len(t["title"]) + 40 for t, _ in texts)
    room = max(budget - overhead, MERGE_MIN_SHARE)
    fit = max(1, min(len(texts), room // MERGE_MIN_SHARE))
    share = room // fit
    pieces, truncated, omitted = [], [], []
    for index, (task, text) in enumerate(texts):
        if index >= fit:
            omitted.append(task["id"])
            continue
        if len(text) > share:
            text = text[: max(share - 16, 0)].rstrip() + "\n[…truncated]"
            truncated.append(task["id"])
        pieces.append(f"### Task {task['position']}: {task['title']}\n{text}")
    parts = [f"Project: {project['name']}", "Results of the individual tasks:\n\n" + "\n\n".join(pieces)]
    if failed:
        parts.append("These tasks failed and have no result: " + ", ".join(f"{t['position']}. {t['title']}" for t in failed))
    if omitted or unreadable:
        parts.append(f"{len(omitted) + len(unreadable)} further result(s) did not fit into the memory budget or could not be read.")
    parts.append("Write the combined final result now.")
    info = {"truncated_task_ids": truncated, "omitted_task_ids": omitted, "unreadable_task_ids": unreadable}
    return "\n\n---\n\n".join(parts), info


def run_merge(project_id: int) -> dict:
    """Combine all task results of a finished distributed project into ``final.md``.

    Returns ``{"status": "done"|"waiting"|"failed", ...}``; never raises for
    model/file problems (they are stored as the merge error).
    """
    dist = _distributed_row(project_id)
    if dist is None:
        raise db.NotFound(f"Project #{project_id} is not shared with devices")
    project = db.get_project(project_id)
    counts = project["counts"]
    if counts["pending"] or counts["running"]:
        return {"status": "waiting", "reason": f"{counts['pending'] + counts['running']} task(s) are not finished yet"}
    done = db.tasks_with_status(project_id, ["done"])
    failed = db.tasks_with_status(project_id, ["failed"])
    if not done:
        message = "No task finished successfully, so there is nothing to combine. Retry the failed tasks first."
        _set_merge_state(project_id, "failed", error=message)
        return {"status": "failed", "error": message}
    mem = memory.plan(dist["percent"])
    if not mem.can_run:
        _set_merge_state(project_id, "waiting", error=mem.reason)
        return {"status": "waiting", "reason": mem.reason}
    started = time.monotonic()
    try:
        with db.inference_slot(f"combining the results of project #{project_id}"):
            _set_merge_state(project_id, "running")
            try:
                prompt, info = _merge_prompt(project, done, failed, mem.context_chars)
                url = local_llm_url()
                answer = llm.chat(
                    [{"role": "system", "content": MERGE_SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                    max_tokens=mem.max_tokens, url=url,
                )
                notes = []
                if failed:
                    notes.append(f"{len(failed)} task(s) failed and are not included")
                skipped = info["truncated_task_ids"] + info["omitted_task_ids"] + info["unreadable_task_ids"]
                if skipped:
                    notes.append(f"the results of task(s) {sorted(skipped)} were shortened or left out to fit the RAM budget")
                text = answer.rstrip() + "\n"
                if notes:
                    text += "\n---\n_Note: " + "; ".join(notes) + ". See the individual task results._\n"
                folder = artifacts.project_dir(project_id)
                final = artifacts.write_text_atomic(folder / "final.md", text)
                artifacts.write_json_atomic(folder / "final.json", {
                    "project_id": project_id,
                    "task_ids": [t["id"] for t in done],
                    "failed_task_ids": [t["id"] for t in failed],
                    **info,
                    "model": config.llm_model(),
                    "endpoint": llm.redact_url(url),
                    "percent": dist["percent"],
                    "max_tokens": mem.max_tokens,
                    "prompt_chars": len(prompt),
                    "duration_s": round(time.monotonic() - started, 2),
                    "created_at": db.utcnow(),
                })
            except Exception as exc:  # recorded, shown in the UI, never swallowed
                message = runner.describe_error(exc)
                log.warning("Merging project #%s failed: %s", project_id, message)
                _set_merge_state(project_id, "failed", error=message)
                return {"status": "failed", "error": message}
    except db.Busy as exc:
        _set_merge_state(project_id, "waiting", error=None)
        return {"status": "waiting", "reason": str(exc)}
    _set_merge_state(project_id, "done", final_path=str(final))
    log.info("Project #%s merged into %s", project_id, final)
    power.notify("Combined result ready", f"{project['name']}: all devices finished; the merged result is ready.")
    return {"status": "done", "path": str(final)}


def _start_merge(project_id: int) -> bool:
    with _STATE.lock:
        if project_id in _STATE.merging:
            return False
        _STATE.merging.add(project_id)

    def work() -> None:
        try:
            run_merge(project_id)
        except Exception:
            log.exception("Merge of project #%s crashed", project_id)
        finally:
            with _STATE.lock:
                _STATE.merging.discard(project_id)

    threading.Thread(target=work, name=f"lowram-merge-{project_id}", daemon=True).start()
    return True


_RESTARTABLE = (None, "queue_empty", "max_tasks", "busy")


def _start_host_loop(server: Any, project_id: int, percent: int) -> str:
    """Let this machine work on the project too (``req.server.loop``), if its loop is idle."""
    loop = getattr(server, "loop", None)
    if loop is None:
        return "not started: no local loop available"
    with _STATE.lock:
        _STATE.app_server = server
    try:
        if not db.tasks_with_status(project_id, ["pending"]):
            return "not started: no pending tasks"
        try:
            loop.start(project_id, percent, runner.MAX_LOOP_TASKS)
        except db.Busy as exc:
            status = loop.status()
            if status["active"] and status["project_id"] == project_id:
                return "already running"
            return f"not started: {exc}"
        return "started"
    finally:
        # Only now may the coordinator resume the loop later (requeued tasks).
        with _STATE.lock:
            _STATE.auto_loop.add(project_id)


def _maybe_restart_host_loop(project_id: int, percent: int) -> None:
    """Pick requeued tasks up locally – but never override a stop/failure/low-RAM pause."""
    server = _STATE.app_server
    loop = getattr(server, "loop", None)
    if loop is None:
        return
    status = loop.status()
    if status["active"]:
        return
    if status["project_id"] == project_id and status["stopped_because"] not in _RESTARTABLE:
        with _STATE.lock:
            _STATE.auto_loop.discard(project_id)
        return
    runs = getattr(server, "runs_in_flight", None)
    if runs is not None and runs():
        return
    try:
        loop.start(project_id, percent, runner.MAX_LOOP_TASKS)
    except db.Busy:
        pass
    except (ValueError, db.NotFound):
        with _STATE.lock:
            _STATE.auto_loop.discard(project_id)


def coordinate_once() -> dict:
    """One coordinator step: expire dead device leases, start merges, resume the host loop."""
    requeued = sweep_leases()
    merges = []
    for item in distributed_projects():
        pid, counts = item["project_id"], item["counts"]
        unfinished = counts["pending"] + counts["running"]
        if item["merge"] and item["merge_state"] == "waiting" and unfinished == 0:
            if _start_merge(pid):
                merges.append(pid)
        elif counts["pending"] and pid in _STATE.auto_loop:
            _maybe_restart_host_loop(pid, item["percent"])
    return {"requeued": requeued, "merges_started": merges}


def _coordinator_main(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            coordinate_once()
        except Exception:
            log.exception("Device coordinator step failed")
        _STATE.wake.wait(COORDINATOR_INTERVAL_S)
        _STATE.wake.clear()


def _ensure_coordinator() -> None:
    with _STATE.lock:
        if _STATE.coordinator is not None and _STATE.coordinator.is_alive():
            return
        _STATE.coordinator_stop = threading.Event()
        _STATE.coordinator = threading.Thread(
            target=_coordinator_main, args=(_STATE.coordinator_stop,), name="lowram-devices-coordinator", daemon=True,
        )
        _STATE.coordinator.start()


def _stop_coordinator() -> None:
    with _STATE.lock:
        thread, stop = _STATE.coordinator, _STATE.coordinator_stop
        _STATE.coordinator = None
    stop.set()
    _STATE.wake.set()
    if thread is not None:
        thread.join(5)


# --------------------------------------------------------------------------- lifecycle


def _in_app_worker_status() -> dict | None:
    from . import worker  # lazy: worker imports this module

    return worker.app_worker_status()


def status() -> dict:
    enabled = bool(settings.get("cluster", "enabled"))
    port = listening_port()
    fingerprint = certificate_fingerprint()
    pairing = _active_pairing()
    with _STATE.lock:
        error, pairing_error = _STATE.error, _STATE.pairing_error
    return {
        "enabled": enabled,
        "listening": {"addresses": lan_addresses(), "port": port} if port is not None else None,
        "fingerprint": format_fingerprint(fingerprint) if fingerprint else None,
        "devices": list_devices(),
        "pairing": {"code": pairing["code"], "expires_at": _iso(pairing["expires"])} if pairing else None,
        "pairing_error": pairing_error,
        "requests": pending_requests(),
        "discoverable": bool(settings.get("cluster", "discoverable")),
        "error": error,
        "host_name": host_display_name(),
        "projects": distributed_projects(),
        "worker": _in_app_worker_status(),
    }


def enable() -> dict:
    try:
        start_lan_server()
    except ClusterError as exc:
        raise HTTPError(HTTPStatus.CONFLICT, str(exc)) from exc
    settings.update("cluster", {"enabled": True})
    return status()


def disable() -> dict:
    settings.update("cluster", {"enabled": False})
    stop_lan_server()
    with _STATE.lock:
        _STATE.error = None
    release_device_tasks(None, "Devices were turned off on the host")
    return status()


def reset() -> None:
    """Stop everything and forget in-memory state (shutdown and tests)."""
    from . import worker

    worker.stop_app_worker(forget=True)
    _stop_coordinator()
    stop_lan_server()
    stop_discovery_listener()
    global _STATE
    _STATE = _State()


@web.on_startup
def on_app_startup(server: Any) -> None:
    with _STATE.lock:
        _STATE.app_server = server
    start_discovery_listener()  # never raises; a taken port only means "no nearby devices"
    if settings.get("cluster", "enabled"):
        _ensure_coordinator()
        start_lan_server()  # a ClusterError becomes a startup error shown in /api/status
    else:
        # Devices cannot reach us; do not let their tasks wait for a timeout.
        release_device_tasks(None, "Devices are turned off on the host")
        if distributed_projects():
            _ensure_coordinator()


@web.on_shutdown
def on_app_shutdown(server: Any) -> None:
    from . import worker

    worker.stop_app_worker()
    _stop_coordinator()
    stop_lan_server()
    stop_discovery_listener()


# --------------------------------------------------------------------------- UI routes (127.0.0.1)


@route("GET", r"/api/cluster")
def get_cluster(req: Request) -> Response:
    return Response(status())


@route("POST", r"/api/cluster/enable")
def post_enable(req: Request) -> Response:
    req.json()
    with _STATE.lock:
        _STATE.app_server = req.server
    return Response({"cluster": enable()})


@route("POST", r"/api/cluster/disable")
def post_disable(req: Request) -> Response:
    req.json()
    return Response({"cluster": disable()})


@route("POST", r"/api/cluster/pairing")
def post_pairing(req: Request) -> Response:
    req.json()
    return Response({"pairing": create_pairing()})


@route("POST", r"/api/cluster/devices/(\d+)/revoke")
def post_revoke(req: Request, device_id: int) -> Response:
    req.json()
    revoke_device(device_id)
    return Response({"revoked": True})


@route("POST", r"/api/cluster/projects/(\d+)/distribute")
def post_distribute(req: Request, project_id: int) -> Response:
    data = req.json()
    percent = memory.validate_percent(data.get("percent", settings.get("budget_percent")))
    merge = data.get("merge", False)
    if not isinstance(merge, bool):
        raise ValueError("'merge' must be true or false")
    runner.require_general(project_id)
    if not is_listening():
        raise HTTPError(HTTPStatus.CONFLICT, "Turn on Devices first, so helper devices can join this project.")
    distribute(project_id, percent, merge)
    host_loop = _start_host_loop(req.server, project_id, percent)
    return Response(
        {"project_id": project_id, "distributed": True, "merge": merge, "host_loop": host_loop}, HTTPStatus.ACCEPTED
    )


@route("GET", r"/api/cluster/projects/(\d+)/final")
def get_final(req: Request, project_id: int) -> Response:
    dist = _distributed_row(project_id)
    if dist is None:
        raise db.NotFound(f"Project #{project_id} is not shared with devices")
    if dist["merge_state"] != "done" or not dist["final_path"]:
        detail = {
            "off": "combining was not requested for this project",
            "waiting": "waiting for all tasks to finish",
            "running": "the results are being combined right now",
            "failed": f"combining failed: {dist['merge_error']}",
        }.get(dist["merge_state"], dist["merge_state"])
        raise HTTPError(HTTPStatus.NOT_FOUND, f"No combined result yet ({detail}).")
    path = artifacts.resolve_inside(dist["final_path"])
    content = artifacts.read_result(str(path), max_chars=MAX_RESULT_CHARS)
    return Response({"content": content, "path": str(path)})


@route("POST", r"/api/cluster/join")
def post_join(req: Request) -> Response:
    from . import worker

    data = req.json()
    percent = memory.validate_percent(data.get("percent", settings.get("budget_percent")))
    if data.get("host_id") is not None:
        host = find_nearby(data.get("host_id"))
        return Response(
            {"worker": worker.start_app_worker(None, data.get("name"), percent, host=host)}, HTTPStatus.ACCEPTED
        )
    return Response(
        {"worker": worker.start_app_worker(data.get("code"), data.get("name"), percent)}, HTTPStatus.ACCEPTED
    )


@route("GET", r"/api/cluster/nearby")
def get_nearby(req: Request) -> Response:
    with _STATE.lock:
        listener = _STATE.discovery
    error = listener.error if listener is not None else None
    return Response({"hosts": nearby_hosts(), "error": error})


@route("POST", r"/api/cluster/requests/(\d+)/approve")
def post_approve_request(req: Request, request_id: int) -> Response:
    req.json()
    answer_request(request_id, True)
    return Response({"approved": True})


@route("POST", r"/api/cluster/requests/(\d+)/deny")
def post_deny_request(req: Request, request_id: int) -> Response:
    req.json()
    answer_request(request_id, False)
    return Response({"denied": True})


@route("POST", r"/api/cluster/leave")
def post_leave(req: Request) -> Response:
    from . import worker

    req.json()
    return Response({"worker": worker.stop_app_worker()})


# --------------------------------------------------------------------------- LAN routes (TLS device server)


def _lan_json(req: Request) -> dict:
    return req.json(csrf=False, max_bytes=MAX_BODY_BYTES)


def _note_device(device_id: int, data: dict) -> None:
    model = data.get("model")
    ram = data.get("ram_total_bytes")
    with db.connect() as conn:
        if isinstance(model, str) and model.strip():
            conn.execute("UPDATE devices SET model = ? WHERE id = ?", (clean_text(model, 120), device_id))
        if isinstance(ram, int) and not isinstance(ram, bool) and 0 <= ram <= 2**50:
            conn.execute("UPDATE devices SET ram_total_bytes = ? WHERE id = ?", (ram, device_id))


@route("POST", r"/cluster/v1/pair", scope=LAN_SCOPE)
def lan_pair(req: Request) -> Response:
    return Response(_pair(_lan_json(req), req.handler.client_address[0]))


@route("POST", r"/cluster/v1/pair-request", scope=LAN_SCOPE)
def lan_pair_request(req: Request) -> Response:
    return Response(_pair_request(_lan_json(req), req.handler.client_address[0]))


@route("POST", r"/cluster/v1/pair-status", scope=LAN_SCOPE)
def lan_pair_status(req: Request) -> Response:
    return Response(_pair_status(_lan_json(req)))


@route("POST", r"/cluster/v1/heartbeat", scope=LAN_SCOPE)
def lan_heartbeat(req: Request) -> Response:
    device = _device(req)
    data = _lan_json(req)
    _note_device(device["id"], data)
    task_id = data.get("task_id")
    lease_valid = None
    if task_id is not None:
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            raise ValueError("task_id must be a task id")
        with db.connect() as conn:
            row = conn.execute("SELECT status, lease_owner FROM tasks WHERE id = ?", (task_id,)).fetchone()
        lease_valid = bool(row) and row["status"] == "running" and row["lease_owner"] == _owner(device["id"])
    return Response({"ok": True, "lease_valid": lease_valid, "server_time": int(_now())})


@route("POST", r"/cluster/v1/claim", scope=LAN_SCOPE)
def lan_claim(req: Request) -> Response:
    device = _device(req)
    data = _lan_json(req)
    _note_device(device["id"], data)
    context_chars = _clamp_int(data.get("context_chars"), memory.MIN_CONTEXT_CHARS, memory.MAX_CONTEXT_CHARS,
                               memory.MIN_CONTEXT_CHARS)
    max_tokens = _clamp_int(data.get("max_tokens"), memory.MIN_MAX_TOKENS, memory.MAX_MAX_TOKENS, memory.MIN_MAX_TOKENS)
    now = time.monotonic()
    with _STATE.lock:
        wait = CLAIM_MIN_INTERVAL_S - (now - _STATE.last_claim.get(device["id"], -1e9))
        if wait > 0:
            return Response({"error": "Too many claims; slow down.", "task": None, "retry_after": round(wait, 2)},
                            HTTPStatus.TOO_MANY_REQUESTS)
        _STATE.last_claim[device["id"]] = now
    task = claim_for_device(device, context_chars, max_tokens)
    return Response({"task": task, "retry_after": None if task else 5})


@route("POST", r"/cluster/v1/tasks/(\d+)/result", scope=LAN_SCOPE)
def lan_result(req: Request, task_id: int) -> Response:
    device = _device(req)
    data = _lan_json(req)
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be non-empty text")
    duration = data.get("duration_s")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                                 or not 0 <= duration <= 7 * 24 * 3600):
        raise ValueError("duration_s must be a number of seconds")
    model = data.get("model")
    model_text = clean_text(model, 120) if isinstance(model, str) else None
    task = submit_result(device, task_id, content, round(float(duration), 2) if duration is not None else None, model_text)
    return Response({"task_id": task["id"], "status": task["status"]})


@route("POST", r"/cluster/v1/tasks/(\d+)/fail", scope=LAN_SCOPE)
def lan_fail(req: Request, task_id: int) -> Response:
    device = _device(req)
    data = _lan_json(req)
    error = data.get("error")
    if not isinstance(error, str) or not error.strip():
        raise ValueError("error must be non-empty text")
    release = data.get("release", False)
    if not isinstance(release, bool):
        raise ValueError("release must be true or false")
    task = submit_failure(device, task_id, clean_text(error, MAX_ERROR_TEXT), release)
    return Response({"task_id": task["id"], "status": task["status"]})


@route("POST", r"/cluster/v1/leave", scope=LAN_SCOPE)
def lan_leave(req: Request) -> Response:
    device = _device(req)
    _lan_json(req)
    released = release_device_tasks(device["id"], f"Device {device['name']!r} left")
    with db.connect() as conn:
        conn.execute("UPDATE devices SET last_seen = 0 WHERE id = ?", (device["id"],))
    return Response({"ok": True, "released": released})
