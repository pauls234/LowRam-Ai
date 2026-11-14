"""Fake OpenAI-compatible *streaming* endpoint and an SSE client for the chat tests."""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Stream:
    """SSE answer: optional ``reasoning_content`` deltas, then content deltas, finish + [DONE]."""

    def __init__(self, chunks=(), *, reasoning=(), delay=0.0, finish=True, done=True, chunked=True,
                 crlf=False, comments=False, finish_reason="stop", usage=None):
        self.chunks, self.reasoning, self.delay = list(chunks), list(reasoning), delay
        self.finish, self.done, self.chunked, self.crlf, self.comments = finish, done, chunked, crlf, comments
        self.finish_reason, self.usage = finish_reason, usage  # usage: completion_tokens to report


class Disconnect(Stream):
    """Send the chunks, then drop the connection (no finish_reason, no [DONE])."""

    def __init__(self, chunks=(), **kw):
        super().__init__(chunks, finish=False, done=False, **kw)


class Hold:
    """Send ``first`` chunks, then keep the request open (keep-alive comments) until
    ``release`` is set; then send ``rest``. Records a client disconnect on the fake."""

    def __init__(self, release: threading.Event, first=(), rest=("!",), timeout=15.0):
        self.release, self.first, self.rest, self.timeout = release, list(first), list(rest), timeout


class HttpError:
    def __init__(self, code=500, body="boom"):
        self.code, self.body = code, body


class RawBody:
    """HTTP 200 with exactly these bytes."""

    def __init__(self, data: bytes, content_type="text/event-stream", chunked=False):
        self.data, self.content_type, self.chunked = data, content_type, chunked


class JsonAnswer:
    """A server that ignores ``stream: true`` and answers with plain JSON."""

    def __init__(self, text: str):
        self.text = text


class Redirect:
    def __init__(self, location: str):
        self.location = location


def sse_chunk(delta: dict, finish=None) -> bytes:
    event = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return b"data: " + json.dumps(event).encode() + b"\n\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_head(self, status=200, content_type="text/event-stream", chunked=True, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _write(self, data: bytes, chunked: bool) -> None:
        self.wfile.write((b"%x\r\n" % len(data)) + data + b"\r\n" if chunked else data)
        self.wfile.flush()

    def _end(self, chunked: bool) -> None:
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        fake: FakeStreamLLM = self.server.fake  # type: ignore[attr-defined]
        with fake.lock:
            fake.requests.append({"headers": dict(self.headers), "json": json.loads(body or b"null")})
            item = fake.script.pop(0) if fake.script else Stream([fake.default_text])
        try:
            self._serve(item, fake)
        except (BrokenPipeError, ConnectionResetError):
            with fake.lock:
                fake.disconnects += 1

    def _serve(self, item, fake: "FakeStreamLLM") -> None:
        if isinstance(item, HttpError):
            data = item.body.encode()
            self.send_response(item.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if isinstance(item, Redirect):
            self.send_response(302)
            self.send_header("Location", item.location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if isinstance(item, JsonAnswer):
            data = json.dumps({"choices": [{"message": {"content": item.text}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if isinstance(item, RawBody):
            self._send_head(content_type=item.content_type, chunked=item.chunked)
            self._write(item.data, item.chunked)
            self._end(item.chunked)
            return
        if isinstance(item, Hold):
            self._send_head()
            for chunk in item.first:
                self._write(sse_chunk({"content": chunk}), True)
            end = time.monotonic() + item.timeout
            while not item.release.is_set() and time.monotonic() < end:
                self._write(b": keep-alive\n\n", True)  # raises once the client is gone
                time.sleep(0.03)
            for chunk in item.rest:
                self._write(sse_chunk({"content": chunk}), True)
            self._write(sse_chunk({}, "stop") + b"data: [DONE]\n\n", True)
            self._end(True)
            return
        self._send_head(chunked=item.chunked)
        newline = b"\r\n" if item.crlf else b"\n"
        if item.comments:
            self._write(b": ping" + newline + newline, item.chunked)
        pieces = [sse_chunk({"reasoning_content": r}) for r in item.reasoning]
        pieces += [sse_chunk({"content": c}) for c in item.chunks]
        if item.finish:
            pieces.append(sse_chunk({}, item.finish_reason))
        if item.usage is not None:  # like stream_options.include_usage: a final chunk without choices
            pieces.append(b"data: " + json.dumps({"choices": [], "usage": {"completion_tokens": item.usage}}).encode() + b"\n\n")
        if item.done:
            pieces.append(b"data: [DONE]\n\n")
        for piece in pieces:
            if item.delay:
                time.sleep(item.delay)
            self._write(piece.replace(b"\n", newline), item.chunked)
        if item.done or item.finish:
            self._end(item.chunked)


class FakeStreamLLM:
    """Streaming OpenAI-compatible endpoint on 127.0.0.1 with scripted replies."""

    def __init__(self, default_text="streamed default"):
        self.default_text = default_text
        self.script: list = []
        self.requests: list[dict] = []
        self.disconnects = 0
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.fake = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"

    def push(self, *items) -> None:
        with self.lock:
            self.script.extend(items)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class SSEClient:
    """POST a chat message and read the Server-Sent Events incrementally."""

    def __init__(self, port: int, path: str, body: dict, token: str = "t"):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        data = json.dumps(body).encode()
        self.conn.request(
            "POST", path, body=data,
            headers={"Content-Type": "application/json", "X-LowRAM-Token": token, "Connection": "close"},
        )
        self.resp = self.conn.getresponse()
        self.status = self.resp.status
        self.headers = dict(self.resp.getheaders())
        self.json = json.loads(self.resp.read()) if self.status != 200 else None

    def next(self) -> dict | None:
        while True:
            line = self.resp.readline()
            if not line:
                return None
            line = line.strip()
            if line.startswith(b"data: "):
                return json.loads(line[6:])

    def until(self, kind: str) -> list[dict]:
        """Events up to and including the first one of type ``kind``."""
        events = []
        while True:
            event = self.next()
            if event is None:
                raise AssertionError(f"stream ended before a {kind!r} event: {events}")
            events.append(event)
            if event["type"] == kind:
                return events

    def rest(self) -> list[dict]:
        events = []
        while (event := self.next()) is not None:
            events.append(event)
        return events

    def close(self) -> None:
        self.resp.close()
        self.conn.close()
