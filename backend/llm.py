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
