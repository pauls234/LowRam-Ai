"""Minimal client for a local OpenAI-compatible ``/v1/chat/completions`` endpoint.

Uses only the standard library. Loopback endpoints bypass any HTTP(S) proxy
configured in the environment, so a corporate proxy never sees your prompts.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import config

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """The model endpoint could not be reached or returned something unusable."""


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: they could forward prompts and the API key elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        return None


def _opener(host: str | None) -> urllib.request.OpenerDirector:
    if _is_loopback(host):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    return urllib.request.build_opener(_NoRedirect)


def redact_url(url: str) -> str:
    """Drop credentials and query strings before a URL is shown or stored."""
    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc, query="", fragment="").geturl()


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def strip_reasoning(text: str) -> str:
    """Remove ``<think>…</think>`` blocks some local reasoning models emit."""
    return _THINK_RE.sub("", text).strip()


def chat(
    messages: list[dict],
    *,
    max_tokens: int = 512,
    temperature: float = 0.2,
    url: str | None = None,
    timeout: float | None = None,
) -> str:
    """Send a chat completion request and return the assistant's text.

    Raises ``LLMError`` for every failure mode (unreachable, HTTP error,
    malformed JSON, empty answer) so callers can record an explicit failure.
    """
    url = url or config.llm_url()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LLMError(f"LOWRAM_LLM_URL must be an http(s) URL, got {redact_url(url)!r}")
    shown_url = redact_url(url)
    payload = {
        "model": config.llm_model(),
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = config.llm_api_key()
    if api_key:
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise LLMError("Refusing to send LOWRAM_LLM_API_KEY over plain http to a non-loopback host; use https")
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    timeout = config.llm_timeout() if timeout is None else timeout
    try:
        with _opener(parsed.hostname).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise LLMError(f"Model endpoint answered with a redirect (HTTP {exc.code}); redirects are not followed") from exc
        detail = _scrub(exc.read(500).decode("utf-8", "replace").strip(), api_key)
        raise LLMError(f"Model endpoint returned HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(
            f"Could not reach the model endpoint at {shown_url} ({exc.reason}). "
            "Is llama-server (or another OpenAI-compatible server) running?"
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise LLMError(f"The model endpoint did not answer within {timeout:g} s") from exc
    except OSError as exc:
        raise LLMError(f"Connection to the model endpoint failed: {exc}") from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        raise LLMError("The model response exceeded 8 MiB and was discarded")
    try:
        data = json.loads(raw.decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMError("The model endpoint returned a response that is not OpenAI chat-completions JSON") from exc
    if not isinstance(content, str):
        raise LLMError("The model returned an empty answer")
    content = strip_reasoning(content)
    if not content:
        raise LLMError("The model returned an empty answer")
    return content


# --------------------------------------------------------------------------- streaming
# Appended for v0.3 (chat). Imports live here so the code above stays untouched.

import http.client  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections.abc import Iterator  # noqa: E402

MAX_STREAM_LINE_BYTES = 1024 * 1024
STOP_POLL_S = 0.1
_NOT_OPENAI_STREAM = "The model endpoint sent streaming data that is not OpenAI chat-completions JSON"


def _without_reasoning(text: str) -> str:
    """Visible answer: closed ``<think>`` blocks and an unclosed trailing one removed."""
    text = _THINK_RE.sub("", text)
    open_at = text.lower().find("<think>")
    return (text[:open_at] if open_at >= 0 else text).strip()


def _error_text(value: object, secret: str | None) -> str:
    """Human-readable message from an error payload (JSON text, dict or plain text)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if isinstance(value, dict):
        inner = value.get("error", value)
        if isinstance(inner, dict):
            inner = inner.get("message") or inner.get("type") or json.dumps(inner)
        value = inner
    text = value if isinstance(value, str) else json.dumps(value)
    return _scrub(text.strip(), secret)[:500] or "unknown error"


def _open_stream(request: urllib.request.Request, host: str | None, shown_url: str, timeout: float,
                 api_key: str | None):
    """Open the request with the same proxy/redirect/error rules as :func:`chat`."""
    try:
        return _opener(host).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise LLMError(f"Model endpoint answered with a redirect (HTTP {exc.code}); redirects are not followed") from exc
        detail = _scrub(exc.read(500).decode("utf-8", "replace").strip(), api_key)
        exc.close()
        raise LLMError(f"Model endpoint returned HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(
            f"Could not reach the model endpoint at {shown_url} ({exc.reason}). "
            "Is llama-server (or another OpenAI-compatible server) running?"
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise LLMError(f"The model endpoint did not answer within {timeout:g} s") from exc
    except OSError as exc:
        raise LLMError(f"Connection to the model endpoint failed: {exc}") from exc


def _interrupt(response) -> None:
    """Wake a read that is blocked on the response socket (stop button)."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    try:
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
        else:
            response.close()
    except (OSError, ValueError):
        pass


def _watch_stop(stop_event: threading.Event, finished: threading.Event, lock: threading.Lock, response) -> None:
    while not finished.is_set():
        if stop_event.wait(STOP_POLL_S):
            with lock:
                if not finished.is_set():
                    _interrupt(response)
            return


def _sse_fields(response, stop_event: threading.Event | None, timeout: float) -> Iterator[tuple[str, str]]:
    """``(field, value)`` for every ``data:``/``error:`` line of an SSE body until EOF or stop."""
    total = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            line = response.readline(MAX_STREAM_LINE_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if stop_event is not None and stop_event.is_set():
                return
            if isinstance(exc, (TimeoutError, socket.timeout)):
                raise LLMError(f"The model endpoint sent nothing for {timeout:g} s; giving up") from exc
            raise LLMError(f"The connection to the model endpoint was interrupted ({exc})") from exc
        if not line:
            return
        total += len(line)
        if total > MAX_RESPONSE_BYTES:
            raise LLMError("The model response exceeded 8 MiB and was discarded")
        if len(line) > MAX_STREAM_LINE_BYTES:
            raise LLMError("The model endpoint sent an oversized streaming event")
        try:
            text = line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise LLMError(_NOT_OPENAI_STREAM) from exc
        if not text or text.startswith(":"):
            continue  # event separator or keep-alive comment
        name, _, value = text.partition(":")
        if name in ("data", "error"):  # llama.cpp reports failures as "error: {...}"
            yield name, value[1:] if value.startswith(" ") else value


def _usage_tokens(data: dict, outcome: dict) -> None:
    usage = data.get("usage")
    tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        outcome["usage_tokens"] = tokens


def _read_json_answer(response, outcome: dict) -> str:
    """Fallback for servers that ignore ``"stream": true`` and answer with plain JSON."""
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LLMError("The model response exceeded 8 MiB and was discarded")
    try:
        data = json.loads(raw.decode("utf-8"))
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMError("The model endpoint returned a response that is not OpenAI chat-completions JSON") from exc
    if isinstance(choice.get("finish_reason"), str):
        outcome["finish_reason"] = choice["finish_reason"]
    _usage_tokens(data, outcome)
    return content if isinstance(content, str) else ""


def _stream_deltas(response, stop_event: threading.Event | None, timeout: float,
                   api_key: str | None, outcome: dict) -> Iterator[str]:
    """Text deltas of an OpenAI-style SSE stream; ``reasoning_content`` is wrapped in ``<think>``."""
    in_reasoning = False
    for name, value in _sse_fields(response, stop_event, timeout):
        if name == "error":
            raise LLMError(f"The model endpoint reported an error: {_error_text(value, api_key)}")
        if value.strip() == "[DONE]":
            outcome["complete"] = True
            break
        try:
            chunk = json.loads(value)
        except json.JSONDecodeError as exc:
            raise LLMError(_NOT_OPENAI_STREAM) from exc
        if not isinstance(chunk, dict):
            raise LLMError(_NOT_OPENAI_STREAM)
        if chunk.get("error"):
            raise LLMError(f"The model endpoint reported an error: {_error_text(chunk['error'], api_key)}")
        _usage_tokens(chunk, outcome)
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise LLMError(_NOT_OPENAI_STREAM)
        if not choices:
            continue  # e.g. a final usage-only chunk
        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict):
            raise LLMError(_NOT_OPENAI_STREAM)
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise LLMError(_NOT_OPENAI_STREAM)
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if (isinstance(reasoning, str) and reasoning) or content:
            outcome["deltas"] = outcome.get("deltas", 0) + 1
            outcome.setdefault("first_at", time.monotonic())
        if isinstance(reasoning, str) and reasoning:
            if not in_reasoning:
                in_reasoning = True
                yield "<think>"
            yield reasoning
        if content:
            if in_reasoning:
                in_reasoning = False
                yield "</think>"
            yield content
        if choice.get("finish_reason"):
            outcome["complete"] = True
            if isinstance(choice["finish_reason"], str):
                outcome["finish_reason"] = choice["finish_reason"]
    if in_reasoning:
        yield "</think>"


def stream_chat(
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.4,
    url: str | None = None,
    timeout: float | None = None,
    stop_event: threading.Event | None = None,
    stats: dict | None = None,
) -> Iterator[str]:
    """Stream a chat completion and yield the text deltas as they arrive.

    Same endpoint rules as :func:`chat` (http(s) only, loopback bypasses
    proxies, no redirects, API key only over https or loopback, secrets
    scrubbed from errors). Reasoning sent as ``reasoning_content`` is yielded
    wrapped in ``<think>…</think>``; ``<think>`` tags inside the content are
    passed through unchanged. Raises ``LLMError`` on HTTP errors, malformed
    data, a connection that ends before the answer is complete, or an empty
    answer. When ``stop_event`` is set the connection is closed promptly and
    the generator simply ends (no error).

    ``stats`` (optional dict) is filled when the generator ends, also on
    stop or error: ``tokens`` (server-reported ``usage.completion_tokens`` if
    sent, else the number of text deltas), ``tokens_per_s`` (first delta to
    end) and ``finish_reason`` (e.g. ``"stop"``/``"length"``, else ``None``).
    """
    url = url or config.llm_url()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LLMError(f"LOWRAM_LLM_URL must be an http(s) URL, got {redact_url(url)!r}")
    shown_url = redact_url(url)
    payload = {
        "model": config.llm_model(),
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
    api_key = config.llm_api_key()
    if api_key:
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise LLMError("Refusing to send LOWRAM_LLM_API_KEY over plain http to a non-loopback host; use https")
        headers["Authorization"] = f"Bearer {api_key}"
    if stop_event is not None and stop_event.is_set():
        return
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    timeout = config.llm_timeout() if timeout is None else timeout
    response = _open_stream(request, parsed.hostname, shown_url, timeout, api_key)
    finished, lock = threading.Event(), threading.Lock()
    if stop_event is not None:
        threading.Thread(
            target=_watch_stop, args=(stop_event, finished, lock, response), name="lowram-llm-stop", daemon=True
        ).start()
    outcome: dict = {"complete": False}
    try:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        visible: list[str] = []
        if content_type == "application/json":
            outcome["first_at"] = time.monotonic()
            text = _read_json_answer(response, outcome)
            outcome["complete"] = True
            if text:
                outcome["deltas"] = 1
                visible.append(text)
                yield text
        else:
            for piece in _stream_deltas(response, stop_event, timeout, api_key, outcome):
                visible.append(piece)
                yield piece
        if stop_event is not None and stop_event.is_set():
            return
        if not outcome["complete"]:
            raise LLMError("The model endpoint closed the connection before the answer was complete")
        if not _without_reasoning("".join(visible)):
            raise LLMError("The model returned an empty answer")
    finally:
        with lock:
            finished.set()
            try:
                response.close()
            except (OSError, ValueError):
                pass
        if stats is not None:
            tokens = outcome.get("usage_tokens", outcome.get("deltas", 0))
            elapsed = time.monotonic() - outcome["first_at"] if "first_at" in outcome else 0.0
            stats.update(
                tokens=tokens,
                tokens_per_s=round(tokens / elapsed, 2) if tokens and elapsed >= 0.05 else None,
                finish_reason=outcome.get("finish_reason"),
            )
