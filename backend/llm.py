"""Minimal client for a local OpenAI-compatible ``/v1/chat/completions`` endpoint.

Uses only the standard library. Loopback endpoints bypass any HTTP(S) proxy
configured in the environment, so a corporate proxy never sees your prompts.

:func:`chat` streams internally whenever the server supports it, so a slow
machine never hits a fixed total timeout: it waits up to
``settings.resilience.first_token_timeout_s`` for the first token (reading a
long prompt can take minutes) and afterwards up to ``idle_token_timeout_s``
between tokens. Every failure is an :class:`LLMError`, classified as
``transient`` (engine restarting/loading, connection refused/reset/closed or
stalled) or ``context_overflow`` (the prompt does not fit the model's context).
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import math
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from urllib.parse import urlparse

from . import config, settings

log = logging.getLogger("lowram.llm")

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_STREAM_LINE_BYTES = 1024 * 1024
STOP_POLL_S = 0.1
DEFAULT_FIRST_TOKEN_TIMEOUT_S = 1800.0
DEFAULT_IDLE_TOKEN_TIMEOUT_S = 300.0
MAX_TIMEOUT_S = 24 * 3600.0
MIN_TIMEOUT_S = 0.1
MAX_FIRST_TOKEN_TIMEOUT_S = 3600.0  # larger settings values are clamped (a stuck request must end)
MAX_REQUEST_S = 3 * 3600.0  # wall-clock cap per request, whatever the server sends
REQUEST_LIMIT_MESSAGE = "Die Anfrage hat das Zeitlimit von 3 Stunden überschritten"
ENGINE_TOUCH_INTERVAL_S = 10.0
TRANSIENT_HTTP_STATUS = frozenset({502, 503})  # engine restarting / still loading the model
_STREAM_REJECT_STATUS = frozenset({400, 404, 405, 415, 422, 501})
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CONTEXT_RE = re.compile(
    r"context[\s_-]*(?:size|length|window|limit)|exceed\w*\W+(?:\w+\W+){0,3}?context|n_ctx|kv[\s_-]*(?:cache|size)",
    re.IGNORECASE,
)
_NOT_OPENAI_STREAM = "The model endpoint sent streaming data that is not OpenAI chat-completions JSON"


class LLMError(RuntimeError):
    """The model endpoint could not be reached or returned something unusable.

    Classification (set where the error is raised):

    * ``transient`` – connection refused/reset/closed, no token within the
      timeouts, or HTTP 502/503 (engine restarting or still loading): worth
      one more try once the engine is ready again.
    * ``context_overflow`` – HTTP 400 whose body mentions the context size:
      retry with less carry-over context.
    * ``status`` – the HTTP status code, when the server answered with one.
    * ``partial`` – visible answer text received before the failure (set by
      :func:`chat`; reasoning removed).
    """

    transient: bool = False
    context_overflow: bool = False
    status: int | None = None
    partial: str = ""
    stream_rejected: bool = False

    def __init__(self, message: str = "", *, transient: bool = False, context_overflow: bool = False,
                 status: int | None = None, stream_rejected: bool = False) -> None:
        super().__init__(message)
        self.transient = bool(transient)
        self.context_overflow = bool(context_overflow)
        self.status = status
        self.stream_rejected = bool(stream_rejected)
        self.partial = ""


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


def visible_text(text: str) -> str:
    """The answer as the user sees it while it streams.

    Closed ``<think>`` blocks and an unclosed trailing one are removed and
    leading whitespace is dropped; trailing whitespace is kept so a partial
    answer can be continued seamlessly.
    """
    text = _THINK_RE.sub("", text)
    open_at = text.lower().find("<think>")
    return (text[:open_at] if open_at >= 0 else text).lstrip()


def _without_reasoning(text: str) -> str:
    """Visible answer: closed ``<think>`` blocks and an unclosed trailing one removed."""
    return visible_text(text).strip()


# --------------------------------------------------------------------------- settings / engine


def _positive_seconds(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return default
    return float(min(max(value, MIN_TIMEOUT_S), MAX_TIMEOUT_S))


def token_timeouts() -> tuple[float, float]:
    """``(first_token_timeout_s, idle_token_timeout_s)`` from ``settings.resilience``.

    Missing or invalid values fall back to 1800 s / 300 s.
    """
    try:
        values = settings.get("resilience")
    except Exception:  # noqa: BLE001 - a broken settings file must not break inference
        log.warning("Could not read the resilience settings; using default timeouts", exc_info=True)
        values = {}
    if not isinstance(values, dict):
        values = {}
    global _first_token_clamp_logged
    first = _positive_seconds(values.get("first_token_timeout_s"), DEFAULT_FIRST_TOKEN_TIMEOUT_S)
    if first > MAX_FIRST_TOKEN_TIMEOUT_S:
        if not _first_token_clamp_logged:
            _first_token_clamp_logged = True
            log.warning("first_token_timeout_s=%g is above the maximum; using %g s", first, MAX_FIRST_TOKEN_TIMEOUT_S)
        first = MAX_FIRST_TOKEN_TIMEOUT_S
    return first, _positive_seconds(values.get("idle_token_timeout_s"), DEFAULT_IDLE_TOKEN_TIMEOUT_S)


_first_token_clamp_logged = False


def _limit_error() -> LLMError:
    return LLMError(REQUEST_LIMIT_MESSAGE, transient=False)


def _past_limit(state: dict) -> bool:
    """The request reached ``MAX_REQUEST_S``: flagged by the watchdog, or measured from ``state["started"]``.

    The socket timeout (at most ``MAX_REQUEST_S``) and the watchdog can fire at the same moment. When the
    read fails first, the watchdog has not set its flag yet – the elapsed time still tells the truth.
    """
    if state.get("over_limit"):
        return True
    started = state.get("started")
    return started is not None and time.monotonic() - started >= MAX_REQUEST_S


def _start_watchdog(response, deadline: float, state: dict, lock: threading.Lock | None = None,
                    finished: threading.Event | None = None) -> threading.Timer:
    """Close ``response`` at ``deadline`` (``MAX_REQUEST_S`` after the request started) and mark ``state``."""

    def fire() -> None:
        if lock is None:
            state["over_limit"] = True
            _interrupt(response)
            return
        with lock:
            if not (finished is not None and finished.is_set()):
                state["over_limit"] = True
                _interrupt(response)

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), fire)
    timer.daemon = True
    timer.start()
    return timer


_touch_lock = threading.Lock()
_last_touch = 0.0


def _touch_engine(force: bool = False) -> None:
    """Tell the managed engine (only if already loaded) that the model is in use, so idle unloading waits."""
    global _last_touch
    touch = getattr(sys.modules.get(f"{__package__}.engine"), "touch", None)
    if not callable(touch):
        return
    now = time.monotonic()
    with _touch_lock:
        if not force and now - _last_touch < ENGINE_TOUCH_INTERVAL_S:
            return
        _last_touch = now
    try:
        touch()
    except Exception:  # noqa: BLE001 - bookkeeping only, never fails a request
        log.debug("engine.touch() failed", exc_info=True)


# --------------------------------------------------------------------------- request helpers


def _check_url(url: str):
    """Parsed ``url`` or ``LLMError`` when it is not a usable http(s) URL (credentials never shown)."""
    try:
        parsed = urlparse(url)
        ok = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        if ok:
            parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        ok = False
    if not ok:
        try:
            shown = redact_url(url)
        except ValueError:
            shown = "<invalid URL>"
        raise LLMError(f"LOWRAM_LLM_URL must be an http(s) URL, got {shown!r}")
    return parsed


def _headers(parsed, accept: str) -> tuple[dict, str | None]:
    headers = {"Content-Type": "application/json", "Accept": accept}
    api_key = config.llm_api_key()
    if api_key:
        if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
            raise LLMError("Refusing to send LOWRAM_LLM_API_KEY over plain http to a non-loopback host; use https")
        headers["Authorization"] = f"Bearer {api_key}"
    return headers, api_key


def _transient_reason(reason: object) -> bool:
    return isinstance(reason, (ConnectionError, TimeoutError, socket.timeout))


def _http_error(code: int, detail: str, reason: object = "") -> LLMError:
    overflow = code == 400 and bool(_CONTEXT_RE.search(detail))
    return LLMError(
        f"Model endpoint returned HTTP {code}: {detail or reason}",
        status=code,
        transient=code in TRANSIENT_HTTP_STATUS,
        context_overflow=overflow,
        stream_rejected=not overflow and code in _STREAM_REJECT_STATUS and "stream" in detail.lower(),
    )


def _request_error(exc: BaseException, shown_url: str, timeout: float, api_key: str | None) -> LLMError:
    """Classified ``LLMError`` for a failure while sending a request or waiting for the response headers."""
    if isinstance(exc, urllib.error.HTTPError):
        if 300 <= exc.code < 400:
            return LLMError(
                f"Model endpoint answered with a redirect (HTTP {exc.code}); redirects are not followed",
                status=exc.code,
            )
        try:
            detail = _scrub(exc.read(500).decode("utf-8", "replace").strip(), api_key)
        except (OSError, http.client.HTTPException, ValueError):
            detail = ""
        finally:
            exc.close()
        return _http_error(exc.code, detail, exc.reason)
    if isinstance(exc, urllib.error.URLError):
        return LLMError(
            f"Could not reach the model endpoint at {shown_url} ({exc.reason}). "
            "Is llama-server (or another OpenAI-compatible server) running?",
            transient=_transient_reason(exc.reason),
        )
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return LLMError(f"The model endpoint did not answer within {timeout:g} s", transient=True)
    if isinstance(exc, OSError):
        return LLMError(f"Connection to the model endpoint failed: {exc}", transient=isinstance(exc, ConnectionError))
    if isinstance(exc, ValueError):  # e.g. http.client.InvalidURL; its text may contain credentials
        return LLMError(f"The model endpoint URL {shown_url!r} is not valid")
    return LLMError(f"The model endpoint sent an invalid HTTP response ({type(exc).__name__})")


def _parse_completion(raw: bytes) -> tuple[dict, dict]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise LLMError("The model response exceeded 8 MiB and was discarded")
    try:
        data = json.loads(raw.decode("utf-8"))
        choice = data["choices"][0]
        choice["message"]["content"]  # noqa: B018 - shape check
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMError("The model endpoint returned a response that is not OpenAI chat-completions JSON") from exc
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise LLMError("The model endpoint returned a response that is not OpenAI chat-completions JSON")
    return data, choice


# --------------------------------------------------------------------------- chat (blocking)

_plain_only: set[str] = set()  # endpoints (redacted URLs) that refused "stream": true
_plain_lock = threading.Lock()


def chat(
    messages: list[dict],
    *,
    max_tokens: int = 512,
    temperature: float = 0.2,
    url: str | None = None,
    timeout: float | None = None,
    on_text: Callable[[str], None] | None = None,
) -> str:
    """Send a chat completion request and return the assistant's text.

    Streams internally (accumulating :func:`stream_chat`) so a long answer
    keeps the connection alive token by token: up to
    ``first_token_timeout_s`` until the first token, then
    ``idle_token_timeout_s`` between tokens (``settings.resilience``) – never
    a fixed total. An explicit ``timeout`` replaces both. Servers that refuse
    ``"stream": true`` are asked again without streaming (remembered per
    endpoint).

    ``on_text(text_so_far)`` (optional) is called after every received piece
    with the visible answer so far (reasoning removed), e.g. for checkpoints.

    Raises ``LLMError`` for every failure mode (unreachable, HTTP error,
    malformed data, stalled or interrupted stream, empty answer) so callers
    can record an explicit failure; see :class:`LLMError` for the
    ``transient`` / ``context_overflow`` classification and ``partial``.
    """
    url = url or config.llm_url()
    _check_url(url)
    if timeout is None:
        first_timeout, idle_timeout = token_timeouts()
    else:
        first_timeout = idle_timeout = float(timeout)
    key = redact_url(url)
    with _plain_lock:
        plain = key in _plain_only
    if not plain:
        pieces: list[str] = []
        try:
            for piece in stream_chat(
                messages, max_tokens=max_tokens, temperature=temperature, url=url,
                timeout=first_timeout, idle_timeout=idle_timeout,
            ):
                pieces.append(piece)
                if on_text is not None:
                    on_text(visible_text("".join(pieces)))
            return _without_reasoning("".join(pieces))
        except LLMError as exc:
            if not exc.stream_rejected:
                exc.partial = visible_text("".join(pieces))
                raise
            with _plain_lock:
                _plain_only.add(key)
            log.info("The model endpoint %s refused streaming (%s); using plain requests", key, exc)
    text = _chat_plain(messages, max_tokens=max_tokens, temperature=temperature, url=url, timeout=first_timeout)
    if on_text is not None:
        on_text(text)
    return text


def _chat_plain(messages: list[dict], *, max_tokens: int, temperature: float, url: str, timeout: float) -> str:
    """Non-streaming request (fallback for servers that refuse ``"stream": true``)."""
    parsed = _check_url(url)
    shown_url = redact_url(url)
    payload = {
        "model": config.llm_model(),
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": False,
    }
    headers, api_key = _headers(parsed, "application/json")
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    _touch_engine(force=True)
    started = time.monotonic()
    state: dict = {"started": started}
    try:
        with _opener(parsed.hostname).open(request, timeout=min(timeout, MAX_REQUEST_S)) as response:
            watchdog = _start_watchdog(response, started + MAX_REQUEST_S, state)
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                watchdog.cancel()
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if _past_limit(state):
            raise _limit_error() from exc
        raise _request_error(exc, shown_url, timeout, api_key) from exc
    finally:
        _touch_engine(force=True)
    if state.get("over_limit"):
        raise _limit_error()
    _, choice = _parse_completion(raw)
    content = choice["message"]["content"]
    if not isinstance(content, str):
        raise LLMError("The model returned an empty answer")
    content = _without_reasoning(content)
    if not content:
        raise LLMError("The model returned an empty answer")
    return content


def endpoint_ready(url: str | None = None, timeout: float = 5.0) -> bool:
    """Does the server behind ``url`` answer and is it not (re)loading? (``GET /health``)

    llama-server answers 503 while it loads a model; any other HTTP answer
    (200, 401, 404, …) counts as up. Same proxy/redirect rules as
    :func:`chat`; neither the prompt nor the API key is sent.
    """
    try:
        parsed = _check_url(url or config.llm_url())
        host = parsed.hostname or ""
        netloc = f"[{host}]" if ":" in host else host
        if parsed.port:
            netloc += f":{parsed.port}"
        request = urllib.request.Request(
            f"{parsed.scheme}://{netloc}/health", headers={"Accept": "application/json"}, method="GET"
        )
        with _opener(host).open(request, timeout=timeout) as response:
            response.read(4096)
        return True
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code not in (502, 503, 504)
    except (LLMError, OSError, http.client.HTTPException, ValueError):
        return False


# --------------------------------------------------------------------------- streaming


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


def _reported_error(value: object, secret: str | None) -> LLMError:
    """Classified ``LLMError`` for an error the server reported inside the stream."""
    payload = value
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
    inner = payload.get("error", payload) if isinstance(payload, dict) else None
    code = inner.get("code") if isinstance(inner, dict) else None
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    kind = str(inner.get("type") or "") if isinstance(inner, dict) else ""
    text = _error_text(value, secret)
    overflow = code in (400, None) and bool(_CONTEXT_RE.search(f"{kind} {text}"))
    return LLMError(
        f"The model endpoint reported an error: {text}",
        status=code, transient=code in TRANSIENT_HTTP_STATUS, context_overflow=overflow,
    )


def _open_stream(request: urllib.request.Request, host: str | None, shown_url: str, timeout: float,
                 api_key: str | None):
    """Open the request with the same proxy/redirect/error rules as :func:`chat`."""
    try:
        return _opener(host).open(request, timeout=timeout)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise _request_error(exc, shown_url, timeout, api_key) from exc


def _response_socket(response):
    return getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)


def _set_read_timeout(response, seconds: float) -> None:
    sock = _response_socket(response)
    if sock is not None:
        try:
            sock.settimeout(seconds)
        except (OSError, ValueError):
            pass


def _interrupt(response) -> None:
    """Wake a read that is blocked on the response socket (stop button)."""
    sock = _response_socket(response)
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


def _sse_fields(response, stop_event: threading.Event | None, outcome: dict) -> Iterator[tuple[str, str]]:
    """``(field, value)`` for every ``data:``/``error:`` line of an SSE body until EOF or stop."""
    total = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            line = response.readline(MAX_STREAM_LINE_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if _past_limit(outcome):
                raise _limit_error() from exc
            if stop_event is not None and stop_event.is_set():
                return
            if isinstance(exc, (TimeoutError, socket.timeout)):
                waited = outcome["timeout"]
                hint = "" if "first_at" in outcome else " (still no first token)"
                raise LLMError(f"The model endpoint sent nothing for {waited:g} s{hint}; giving up",
                               transient=True) from exc
            raise LLMError(f"The connection to the model endpoint was interrupted ({exc})", transient=True) from exc
        if outcome.get("over_limit"):
            raise _limit_error()
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
    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        if _past_limit(outcome):
            raise _limit_error() from exc
        if isinstance(exc, (TimeoutError, socket.timeout)):
            raise LLMError(f"The model endpoint did not answer within {outcome['timeout']:g} s",
                           transient=True) from exc
        raise LLMError(f"The connection to the model endpoint was interrupted ({exc})", transient=True) from exc
    data, choice = _parse_completion(raw)
    content = choice["message"]["content"]
    if isinstance(choice.get("finish_reason"), str):
        outcome["finish_reason"] = choice["finish_reason"]
    _usage_tokens(data, outcome)
    return content if isinstance(content, str) else ""


def _first_token(response, outcome: dict) -> None:
    """After the first token the (shorter) idle timeout applies between tokens."""
    outcome["first_at"] = time.monotonic()
    idle = outcome.get("idle_timeout")
    if idle is not None and idle != outcome["timeout"]:
        _set_read_timeout(response, idle)
        outcome["timeout"] = idle


def _stream_deltas(response, stop_event: threading.Event | None, api_key: str | None,
                   outcome: dict) -> Iterator[str]:
    """Text deltas of an OpenAI-style SSE stream; ``reasoning_content`` is wrapped in ``<think>``."""
    in_reasoning = False
    for name, value in _sse_fields(response, stop_event, outcome):
        if name == "error":
            raise _reported_error(value, api_key)
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
            raise _reported_error(chunk, api_key)
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
            if "first_at" not in outcome:
                _first_token(response, outcome)
            _touch_engine()
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
    idle_timeout: float | None = None,
) -> Iterator[str]:
    """Stream a chat completion and yield the text deltas as they arrive.

    Same endpoint rules as :func:`chat` (http(s) only, loopback bypasses
    proxies, no redirects, API key only over https or loopback, secrets
    scrubbed from errors). Reasoning sent as ``reasoning_content`` is yielded
    wrapped in ``<think>…</think>``; ``<think>`` tags inside the content are
    passed through unchanged. Raises ``LLMError`` (classified like
    :func:`chat`) on HTTP errors, malformed data, a connection that ends
    before the answer is complete, or an empty answer. When ``stop_event`` is
    set the connection is closed promptly and the generator simply ends (no
    error).

    ``timeout`` (default ``LOWRAM_LLM_TIMEOUT``) applies until the first
    token; ``idle_timeout`` (default: the same) between later tokens. No
    request runs longer than ``MAX_REQUEST_S`` (3 h) in total: then the
    connection is closed and a non-transient ``LLMError`` is raised.

    ``stats`` (optional dict) is filled when the generator ends, also on
    stop or error: ``tokens`` (server-reported ``usage.completion_tokens`` if
    sent, else the number of text deltas), ``tokens_per_s`` (first delta to
    end) and ``finish_reason`` (e.g. ``"stop"``/``"length"``, else ``None``).
    """
    url = url or config.llm_url()
    parsed = _check_url(url)
    shown_url = redact_url(url)
    payload = {
        "model": config.llm_model(),
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers, api_key = _headers(parsed, "text/event-stream, application/json")
    if stop_event is not None and stop_event.is_set():
        return
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    timeout = config.llm_timeout() if timeout is None else timeout
    _touch_engine(force=True)
    started = time.monotonic()
    try:
        response = _open_stream(request, parsed.hostname, shown_url, min(timeout, MAX_REQUEST_S), api_key)
    except LLMError as exc:
        if time.monotonic() - started >= MAX_REQUEST_S:
            raise _limit_error() from exc
        raise
    finished, lock = threading.Event(), threading.Lock()
    if stop_event is not None:
        threading.Thread(
            target=_watch_stop, args=(stop_event, finished, lock, response), name="lowram-llm-stop", daemon=True
        ).start()
    outcome: dict = {"complete": False, "timeout": timeout, "idle_timeout": idle_timeout, "started": started}
    watchdog = _start_watchdog(response, started + MAX_REQUEST_S, outcome, lock, finished)
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
            for piece in _stream_deltas(response, stop_event, api_key, outcome):
                visible.append(piece)
                yield piece
        if not outcome["complete"] and _past_limit(outcome):
            raise _limit_error()
        if stop_event is not None and stop_event.is_set():
            return
        if not outcome["complete"]:
            raise LLMError("The model endpoint closed the connection before the answer was complete", transient=True)
        if not _without_reasoning("".join(visible)):
            raise LLMError("The model returned an empty answer")
    finally:
        watchdog.cancel()
        with lock:
            finished.set()
            try:
                response.close()
            except (OSError, ValueError):
                pass
        _touch_engine(force=True)
        if stats is not None:
            tokens = outcome.get("usage_tokens", outcome.get("deltas", 0))
            elapsed = time.monotonic() - outcome["first_at"] if "first_at" in outcome else 0.0
            stats.update(
                tokens=tokens,
                tokens_per_s=round(tokens / elapsed, 2) if tokens and elapsed >= 0.05 else None,
                finish_reason=outcome.get("finish_reason"),
            )
