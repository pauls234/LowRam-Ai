"""Tiny routing layer shared by the dashboard server and the API modules.

API modules register handlers with ``@route("GET", r"/api/things/(\\d+)")``.
A handler receives a :class:`Request` plus the regex groups (digit-only
groups are converted to ``int``) and returns one of:

* :class:`Response` – JSON body (default status 200);
* :class:`StreamResponse` – Server-Sent Events, one JSON object per event;
* :class:`FileResponse` – a file from disk (e.g. a generated image).

Errors are raised as :class:`HTTPError` or as the domain exceptions mapped in
``backend.app``.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:  # pragma: no cover
    from http.server import BaseHTTPRequestHandler

MAX_BODY_BYTES = 1024 * 1024


class QuickBindMixin:
    """Skip ``socket.getfqdn()`` in ``HTTPServer.server_bind``.

    The stdlib does a reverse-DNS lookup of the bind address there, which can
    hang for many seconds on macOS (mDNS). We only listen on known addresses,
    so the fully-qualified name is never needed. Use as the FIRST base class.
    """

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)  # type: ignore[arg-type]
        host, port = self.server_address[:2]  # type: ignore[attr-defined]
        self.server_name = str(host)
        self.server_port = port


class HTTPError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Response:
    data: dict
    status: HTTPStatus = HTTPStatus.OK


@dataclass
class StreamResponse:
    """Server-Sent Events. Each yielded dict is sent as one ``data:`` line.

    The generator should yield a final event with ``"type": "done"`` or
    ``"type": "error"``; the connection is closed afterwards.
    """

    events: Iterator[dict]


@dataclass
class FileResponse:
    path: Path
    content_type: str
    download_name: str | None = None


@dataclass(frozen=True)
class Route:
    method: str
    regex: re.Pattern[str]
    func: Callable[..., Any]
    scope: str = "ui"


ROUTES: list[Route] = []
STARTUP_HOOKS: list[Callable[[Any], None]] = []
SHUTDOWN_HOOKS: list[Callable[[Any], None]] = []


def on_startup(func: Callable[[Any], None]) -> Callable[[Any], None]:
    """Run ``func(server)`` after the dashboard server is created (e.g. auto-start the engine)."""
    STARTUP_HOOKS.append(func)
    return func


def on_shutdown(func: Callable[[Any], None]) -> Callable[[Any], None]:
    """Run ``func(server)`` when the dashboard stops (e.g. stop llama-server).

    Shutdown hooks run in reverse registration order (last registered first), so
    features that use the engine (media, cluster) stop before the engine itself.
    """
    SHUTDOWN_HOOKS.append(func)
    return func


def run_hooks(hooks: list[Callable[[Any], None]], server: Any) -> list[str]:
    """Run hooks; one failing hook never prevents the others. Returns error messages."""
    import logging

    errors = []
    for hook in hooks:
        try:
            hook(server)
        except Exception as exc:  # reported, not swallowed
            logging.getLogger("lowram.web").exception("Hook %s failed", getattr(hook, "__name__", hook))
            errors.append(f"{getattr(hook, '__name__', 'hook')}: {exc}")
    return errors


def route(method: str, pattern: str, *, scope: str = "ui") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a handler. ``scope`` separates the loopback UI API ("ui") from
    the LAN device API ("cluster"), which is served by a different server."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        ROUTES.append(Route(method.upper(), re.compile(pattern), func, scope))
        return func

    return decorator


def match(method: str, path: str, scope: str = "ui") -> tuple[Route, list[Any]] | None:
    for candidate in ROUTES:
        if candidate.scope != scope or candidate.method != method:
            continue
        found = candidate.regex.fullmatch(path)
        if found:
            return candidate, [int(g) if g.isdigit() else g for g in found.groups()]
    return None


def path_exists(path: str, scope: str = "ui") -> bool:
    return any(r.scope == scope and r.regex.fullmatch(path) for r in ROUTES)


@dataclass
class Request:
    """What a route handler sees of the incoming HTTP request."""

    handler: "BaseHTTPRequestHandler"
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    _body: bytes | None = None

    @classmethod
    def from_handler(cls, handler: "BaseHTTPRequestHandler") -> "Request":
        parsed = urlparse(handler.path)
        return cls(handler=handler, method=handler.command, path=parsed.path, query=parse_qs(parsed.query))

    @property
    def server(self) -> Any:
        return self.handler.server

    @property
    def headers(self) -> Any:
        return self.handler.headers

    def arg(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def body(self, max_bytes: int = MAX_BODY_BYTES) -> bytes:
        """Read the raw request body once (Content-Length only, no chunking)."""
        if self._body is not None:
            return self._body
        if self.headers.get("Transfer-Encoding"):
            raise HTTPError(HTTPStatus.LENGTH_REQUIRED, "Chunked request bodies are not supported")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length") from exc
        if length < 0 or length > max_bytes:
            raise HTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
        self._body = self.handler.rfile.read(length) if length else b""
        return self._body

    def check_csrf(self) -> None:
        """UI POSTs must carry the per-process token embedded in the page."""
        expected = getattr(self.server, "csrf_token", None)
        token = self.headers.get("X-LowRAM-Token") or ""
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            raise HTTPError(HTTPStatus.FORBIDDEN, "Missing or invalid X-LowRAM-Token; reload the page")

    def json(self, *, csrf: bool = True, max_bytes: int = MAX_BODY_BYTES) -> dict:
        """Parse a JSON object body (UI requests also need the CSRF token)."""
        if self.headers.get("Transfer-Encoding"):
            raise HTTPError(HTTPStatus.LENGTH_REQUIRED, "Chunked request bodies are not supported")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise HTTPError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
        if csrf:
            self.check_csrf()
        raw = self.body(max_bytes) or b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPError(HTTPStatus.BAD_REQUEST, "Body must be valid JSON") from exc
        if not isinstance(data, dict):
            raise HTTPError(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
        return data
