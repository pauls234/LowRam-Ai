"""Local web dashboard and JSON API (stdlib ``http.server``, loopback only).

Start with ``python -m backend.app`` and open http://127.0.0.1:8765.

Protection (this is still a single-user prototype without authentication):
* binds to 127.0.0.1 only;
* rejects requests whose ``Host`` header is not a loopback name (DNS rebinding);
* every POST needs ``Content-Type: application/json`` and the per-process
  ``X-LowRAM-Token`` that is embedded in the served page (CSRF);
* strict Content-Security-Policy; model output is rendered as text only.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import secrets
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import api_core, artifacts, config, db, runner, web  # noqa: F401 - api_core registers routes
from .web import FileResponse, HTTPError, StreamResponse

# Feature modules register their routes/hooks on import. A module that does not
# exist yet (built in parallel) is skipped; any other import error is raised.
FEATURE_MODULES = ("api_coding", "chat", "engine", "media", "cluster")
LOADED_FEATURES: list[str] = []
for _name in FEATURE_MODULES:
    try:
        importlib.import_module(f"{__package__}.{_name}")
        LOADED_FEATURES.append(_name)
    except ModuleNotFoundError as _exc:
        if _exc.name != f"{__package__}.{_name}":
            raise

log = logging.getLogger("lowram.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}


def static_type(name: str) -> str | None:
    """Content type for a servable file directly inside ``static/`` (never index.html)."""
    suffix = Path(name).suffix.lower()
    if suffix not in STATIC_TYPES or "/" in name or name.startswith("."):
        return None
    candidate = STATIC_DIR / name
    return STATIC_TYPES[suffix] if candidate.is_file() and candidate.parent == STATIC_DIR else None


STATIC_FILES = {
    path.name: STATIC_TYPES[path.suffix.lower()]
    for path in sorted(STATIC_DIR.glob("*"))
    if path.is_file() and path.suffix.lower() in STATIC_TYPES
}
TOKEN_PLACEHOLDER = "__LOWRAM_TOKEN__"
MAX_BODY_BYTES = web.MAX_BODY_BYTES
CSP = (
    "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'"
)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")



class LoopController:
    """Runs one bounded background loop at a time and exposes its state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = self._idle_state()

    @staticmethod
    def _idle_state() -> dict:
        return {
            "active": False, "project_id": None, "percent": None, "max_tasks": None,
            "completed": 0, "failed": 0, "current_task_id": None, "stop_requested": False,
            "stopped_because": None, "started_at": None, "finished_at": None, "last_message": "",
        }

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def is_active(self) -> bool:
        with self._lock:
            return bool(self._state["active"])

    def start(self, project_id: int, percent: int, max_tasks: int) -> dict:
        reason = db.busy_reason()  # outside the lock: may wait on SQLite
        if reason:
            raise db.Busy(reason)
        with self._lock:
            if self._state["active"]:
                raise db.Busy("A loop is already running. Stop it first.")
            self._stop = threading.Event()
            self._state = {
                **self._idle_state(), "active": True, "project_id": project_id, "percent": percent,
                "max_tasks": max_tasks, "started_at": db.utcnow(), "last_message": "Loop started.",
            }
            self._thread = threading.Thread(
                target=self._run, args=(project_id, percent, max_tasks, self._stop),
                name=f"lowram-loop-{project_id}", daemon=True,
            )
            self._thread.start()
            return dict(self._state)

    def stop(self) -> dict:
        with self._lock:
            if self._state["active"]:
                self._stop.set()
                self._state["stop_requested"] = True
                self._state["last_message"] = "Stop requested; the loop ends after the current task."
            return dict(self._state)

    def _on_step(self, outcome: dict) -> None:
        with self._lock:
            task = outcome.get("task") or {}
            status = outcome["status"]
            if status == "done":
                self._state["completed"] += 1
            elif status == "failed":
                self._state["failed"] += 1
            self._state["current_task_id"] = None
            label = f"Task {task['position']} (id {task['id']}) {status}" if task else status
            detail = outcome.get("error") or outcome.get("reason") or ""
            self._state["last_message"] = f"{label}{': ' + detail if detail else ''}"[:500]

    def _run(self, project_id: int, percent: int, max_tasks: int, stop: threading.Event) -> None:
        stopped_because, message = "error", ""
        try:
            summary = runner.run_loop(project_id, percent, max_tasks, stop_event=stop, on_step=self._on_step)
            stopped_because, message = summary["stopped_because"], summary["message"]
        except Exception as exc:  # surfaced in the UI, also logged
            log.exception("Loop for project %s crashed", project_id)
            message = runner.describe_error(exc)
        finally:
            with self._lock:
                self._state.update(
                    active=False, current_task_id=None, stopped_because=stopped_because,
                    finished_at=db.utcnow(), last_message=(message or stopped_because)[:500],
                )



class Handler(BaseHTTPRequestHandler):
    server: "LowRAMServer"
    server_version = "LowRAM-AI"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # idle keep-alive / slow clients must not hold a thread forever
    scope = "ui"

    # ------------------------------------------------------------------ plumbing

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.info("%s %s", self.address_string(), format % args)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if status >= 400:
            # The request body may be unread; never reuse such a connection.
            self.close_connection = True
            self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, data: dict) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _check_host(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        allowed = {f"{name}:{port}" for name in LOOPBACK_HOSTS}
        if port == 80:
            allowed.update(LOOPBACK_HOSTS)
        if host not in allowed:
            raise HTTPError(HTTPStatus.FORBIDDEN, "Forbidden host header")

    def _send_index(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace(TOKEN_PLACEHOLDER, self.server.csrf_token)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

    def _send_static(self, name: str) -> None:
        content_type = STATIC_FILES.get(name) or static_type(name)
        if not content_type:
            raise HTTPError(HTTPStatus.NOT_FOUND, "Not found")
        self._send(HTTPStatus.OK, (STATIC_DIR / name).read_bytes(), content_type)

    def _send_file(self, resp: FileResponse) -> None:
        extra = {}
        if resp.download_name:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", resp.download_name)[:120]
            extra["Content-Disposition"] = f'attachment; filename="{safe}"'
        self._send(HTTPStatus.OK, resp.path.read_bytes(), resp.content_type, extra)

    def _send_stream(self, resp: StreamResponse) -> None:
        events = resp.events
        try:
            # Headers inside the try: if the client is already gone, close() still runs
            # and the producer releases its inference slot.
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "close")
            self._security_headers()
            self.end_headers()
            for event in events:
                self.wfile.write(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log.info("Client disconnected from stream %s", self.path)
        except Exception as exc:  # the stream already started: report the error in-band
            log.exception("Stream %s failed", self.path)
            try:
                payload = {"type": "error", "error": runner.describe_error(exc)}
                self.wfile.write(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
            except OSError:
                pass
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                close()  # lets the producer cancel work (GeneratorExit)

    # ------------------------------------------------------------------ dispatch

    def _handle(self) -> None:
        try:
            self._check_host()
            request = web.Request.from_handler(self)
            method = "GET" if self.command == "HEAD" else self.command
            if method == "GET" and request.path == "/":
                self._send_index()
                return
            static = re.fullmatch(r"/static/([A-Za-z0-9_-][A-Za-z0-9_.-]*)", request.path)
            if method == "GET" and static:
                self._send_static(static.group(1))
                return
            found = web.match(method, request.path, self.scope)
            if found is None:
                if web.path_exists(request.path, self.scope):
                    raise HTTPError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
                raise HTTPError(HTTPStatus.NOT_FOUND, "Not found")
            route, args = found
            result = route.func(request, *args)
            if isinstance(result, web.StreamResponse):
                self._send_stream(result)
            elif isinstance(result, web.FileResponse):
                self._send_file(result)
            elif isinstance(result, web.Response):
                self._json(result.status, result.data)
            else:  # pragma: no cover - programming error in a route
                raise TypeError(f"Route {route.func.__name__} returned {type(result).__name__}")
        except HTTPError as exc:
            self._json(exc.status, {"error": exc.message})
        except db.NotFound as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except db.Busy as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (artifacts.ArtifactError, db.InvalidTransition) as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except config.ConfigError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Server configuration error: {exc}"})
        except (UnicodeError, json.JSONDecodeError):
            log.exception("Corrupt data while handling %s %s", self.command, self.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Stored data could not be decoded; see the server log"})
        except ValueError as exc:  # input validation
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:  # logged with traceback, generic message to the client
            log.exception("Unhandled error for %s %s", self.command, self.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal error; see the server log"})

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle  # noqa: N815


class LowRAMServer(web.QuickBindMixin, ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, token: str | None = None):
        super().__init__((config.HOST, port), Handler)
        self.csrf_token = token or secrets.token_urlsafe(32)
        self.loop = LoopController()
        self._runs = 0
        self._runs_lock = threading.Lock()

    @contextmanager
    def track_run(self) -> Iterator[None]:
        with self._runs_lock:
            self._runs += 1
        try:
            yield
        finally:
            with self._runs_lock:
                self._runs -= 1

    def runs_in_flight(self) -> bool:
        with self._runs_lock:
            return self._runs > 0


def make_server(port: int | None = None) -> LowRAMServer:
    recovered = db.recover_stale()
    if recovered:
        log.warning("Marked interrupted task(s) %s as failed (crash recovery)", recovered)
    server = LowRAMServer(config.port() if port is None else port)
    server.startup_errors = []
    return server


def start_background(port: int | None = None) -> LowRAMServer:
    """Create the server, run startup hooks and serve in a daemon thread (desktop app, tests)."""
    server = make_server(port)
    server.startup_errors = web.run_hooks(web.STARTUP_HOOKS, server)
    threading.Thread(target=server.serve_forever, name="lowram-http", daemon=True).start()
    return server


def shutdown(server: LowRAMServer) -> None:
    """Stop the loop, run shutdown hooks (e.g. stop llama-server) and close the socket."""
    server.loop.stop()
    web.run_hooks(list(reversed(web.SHUTDOWN_HOOKS)), server)
    server.shutdown()
    server.server_close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        server = make_server()
    except OSError as exc:
        print(f"Could not start the dashboard on {config.HOST}:{config.port()}: {exc}", file=sys.stderr)
        return 1
    except config.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    server.startup_errors = web.run_hooks(web.STARTUP_HOOKS, server)
    for error in server.startup_errors:
        print(f"Warning: {error}", file=sys.stderr)
    host, port = server.server_address[:2]
    print(f"LowRAM AI: http://{host}:{port}  (model endpoint: {config.llm_url()})")
    print("Local use only - do not expose this port to the network. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        server.loop.stop()
        web.run_hooks(list(reversed(web.SHUTDOWN_HOOKS)), server)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
