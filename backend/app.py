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

import hmac
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
from urllib.parse import parse_qs, urlparse

from . import artifacts, config, db, memory, runner

log = logging.getLogger("lowram.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
TOKEN_PLACEHOLDER = "__LOWRAM_TOKEN__"
MAX_BODY_BYTES = 1024 * 1024
MAX_RESULT_CHARS = 2_000_000
CSP = (
    "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'"
)
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class HTTPError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


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

    # ------------------------------------------------------------------ plumbing

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.info("%s %s", self.address_string(), format % args)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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

    def _read_json(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise HTTPError(HTTPStatus.LENGTH_REQUIRED, "Chunked request bodies are not supported")
        content_type =(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise HTTPError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
        token = self.headers.get("X-LowRAM-Token") or ""
        if not hmac.compare_digest(token.encode(), self.server.csrf_token.encode()):
            raise HTTPError(HTTPStatus.FORBIDDEN, "Missing or invalid X-LowRAM-Token; reload the page")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "Body must be valid JSON") from exc
        if not isinstance(data, dict):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
        return data

    def _dispatch(self, routes: list[tuple[str, object]]) -> None:
        try:
            self._check_host()
            path = urlparse(self.path).path
            for pattern, handler in routes:
                match = re.fullmatch(pattern, path)
                if match:
                    handler(*[int(g) if g.isdigit() else g for g in match.groups()])  # type: ignore[operator]
                    return
            raise HTTPError(HTTPStatus.NOT_FOUND, "Not found")
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

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch(
            [
                (r"/", self.get_index),
                (r"/static/([A-Za-z0-9_.-]+)", self.get_static),
                (r"/api/memory", self.get_memory),
                (r"/api/projects", self.get_projects),
                (r"/api/projects/(\d+)", self.get_project),
                (r"/api/tasks/(\d+)/result", self.get_result),
                (r"/api/loop", self.get_loop),
            ]
        )

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch(
            [
                (r"/api/projects", self.post_project),
                (r"/api/projects/(\d+)/run", self.post_run),
                (r"/api/projects/(\d+)/loop", self.post_loop),
                (r"/api/loop/stop", self.post_loop_stop),
                (r"/api/tasks/(\d+)/retry", self.post_retry),
                (r"/api/recover", self.post_recover),
            ]
        )

    def get_index(self) -> None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace(TOKEN_PLACEHOLDER, self.server.csrf_token)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

    def get_static(self, name: str) -> None:
        if name not in STATIC_FILES:
            raise HTTPError(HTTPStatus.NOT_FOUND, "Not found")
        self._send(HTTPStatus.OK, (STATIC_DIR / name).read_bytes(), STATIC_FILES[name])

    def get_memory(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        percent = query.get("percent", [str(memory.DEFAULT_PERCENT)])[0]
        self._json(HTTPStatus.OK, memory.plan(percent).to_dict())

    def get_projects(self) -> None:
        self._json(HTTPStatus.OK, {"projects": db.list_projects()})

    def get_project(self, project_id: int) -> None:
        self._json(HTTPStatus.OK, {"project": db.get_project(project_id), "tasks": db.list_tasks(project_id)})

    def get_result(self, task_id: int) -> None:
        task = db.get_task(task_id)
        path = artifacts.resolve_inside(task["result_path"])
        content = artifacts.read_result(str(path), max_chars=MAX_RESULT_CHARS + 1)
        truncated = len(content) > MAX_RESULT_CHARS
        self._json(
            HTTPStatus.OK,
            {"task_id": task_id, "path": str(path), "content": content[:MAX_RESULT_CHARS], "truncated": truncated},
        )

    def get_loop(self) -> None:
        self._json(HTTPStatus.OK, {"loop": self.server.loop.status()})

    def post_project(self) -> None:
        data = self._read_json()
        project_id = runner.create_project(data.get("name"), data.get("tasks"))
        self._json(HTTPStatus.CREATED, {"project": db.get_project(project_id)})

    def post_run(self, project_id: int) -> None:
        data = self._read_json()
        if self.server.loop.is_active():
            raise db.Busy("A loop is running. Stop it before running single tasks.")
        with self.server.track_run():
            outcome = runner.run_next(project_id, data.get("percent", memory.DEFAULT_PERCENT))
        self._json(HTTPStatus.OK, outcome)

    def post_loop(self, project_id: int) -> None:
        data = self._read_json()
        percent = memory.validate_percent(data.get("percent", memory.DEFAULT_PERCENT))
        max_tasks = runner.validate_max_tasks(data.get("max_tasks", runner.DEFAULT_LOOP_TASKS))
        runner.require_general(project_id)
        state = self.server.loop.start(project_id, percent, max_tasks)
        self._json(HTTPStatus.ACCEPTED, {"loop": state})

    def post_loop_stop(self) -> None:
        self._read_json()
        self._json(HTTPStatus.OK, {"loop": self.server.loop.stop()})

    def post_retry(self, task_id: int) -> None:
        self._read_json()
        self._json(HTTPStatus.OK, {"task": db.retry_task(task_id)})

    def post_recover(self) -> None:
        force = self._read_json().get("force") is True
        if force and (self.server.loop.is_active() or self.server.runs_in_flight()):
            raise db.Busy("A task is running in this dashboard; force recovery is only allowed when idle.")
        self._json(HTTPStatus.OK, {"recovered": db.recover_stale(force=force)})


class LowRAMServer(ThreadingHTTPServer):
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
    return LowRAMServer(config.port() if port is None else port)


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
    host, port = server.server_address[:2]
    print(f"LowRAM AI dashboard: http://{host}:{port}  (model endpoint: {config.llm_url()})")
    print("Local use only - do not expose this port to the network. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        server.loop.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
