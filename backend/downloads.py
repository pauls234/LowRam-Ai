"""Thread-based download manager for runtimes and models.

Every download runs in its own daemon thread (at most :data:`MAX_PARALLEL` at
a time; the others wait in state ``queued``) and is visible to the UI as a
``Download`` dict (see docs/ARCHITECTURE.md).

Security model
--------------
* Only ``https://`` URLs on :data:`ALLOWED_HOSTS` / :data:`ALLOWED_HOST_SUFFIXES`.
  Redirects are followed manually (at most :data:`MAX_REDIRECTS`) and every hop
  is checked against the same allow-list, so a compromised or misconfigured
  server cannot bounce us to plain HTTP or to an arbitrary host.
* Data is written to ``<dest>.part`` and only renamed into place after the size
  and SHA-256 (when known) were verified. A failed checksum deletes the file.
* Archives are extracted into a private staging directory with zip-slip,
  absolute-path, device-file and symlink protection, then moved into place.
* URLs are logged and reported without query strings (signed CDN URLs carry
  credentials there), see :func:`redact`.
* Destinations must lie below ``LOWRAM_HOME``.

The network layer is injectable (:func:`set_opener`) so tests can serve the
files from a local plain-HTTP server; the URL policy above is applied before
the opener is called, so it is exercised by the tests as well.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urljoin, urlparse

from . import __version__, config, db
from .web import HTTPError, Request, Response, route

log = logging.getLogger("lowram.downloads")

ALLOWED_HOSTS = frozenset(
    {
        "huggingface.co",
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
ALLOWED_HOST_SUFFIXES = (".huggingface.co", ".hf.co")
REDIRECT_CODES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 10

JOB_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
KINDS = ("runtime", "model", "media-runtime", "media-model")
EXTRACT_FORMATS = ("zip", "tar.gz")
ACTIVE_STATES = ("queued", "downloading", "verifying", "extracting")
FINAL_STATES = ("done", "failed", "cancelled")

GIB = 1024**3
DISK_HEADROOM_BYTES = 1 * GIB
CHUNK_BYTES = 256 * 1024
PROGRESS_INTERVAL_S = 0.25  # at most ~4 progress updates per second
DISK_CHECK_EVERY_BYTES = 256 * 1024 * 1024
TIMEOUT_S = 30.0  # connect / per-read stall timeout, not a total limit
MAX_ATTEMPTS = 4  # transient network errors are retried with HTTP Range resume
RETRY_DELAYS_S = (1.0, 3.0, 8.0)
MAX_PARALLEL = 2
MAX_FINISHED_JOBS = 50
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_UNKNOWN_SIZE_BYTES = 128 * GIB
MAX_EXTRACT_BYTES = 8 * GIB
MAX_EXTRACT_MEMBERS = 20_000
MAX_LINK_TARGET_CHARS = 255
CANCEL_WAIT_S = 2.0
USER_AGENT = f"LowRAM-AI/{__version__} (+local download manager)"


class DownloadError(RuntimeError):
    """A download failed. ``transient`` errors are retried; ``discard_part``
    means the partial file is useless (e.g. checksum mismatch) and is deleted."""

    def __init__(self, message: str, *, transient: bool = False, discard_part: bool = False):
        super().__init__(message)
        self.transient = transient
        self.discard_part = discard_part


class HTTPStatusError(DownloadError):
    """The server answered with a non-success HTTP status."""

    def __init__(self, code: int, message: str):
        super().__init__(message, transient=code >= 500 or code in (408, 429))
        self.code = code


class UnsafeArchiveError(DownloadError):
    """An archive member would escape the destination or is not a plain file/dir."""

    def __init__(self, message: str):
        super().__init__(message, discard_part=True)


class _Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- URL policy


def redact(url: str) -> str:
    """``scheme://host[:port]/path`` only: drops credentials, query and fragment."""
    try:
        parsed = urlparse(str(url))
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return "<invalid url>"
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


def host_allowed(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower()
    if host in ALLOWED_HOSTS:
        return True
    return any(host.endswith(suffix) and len(host) > len(suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def check_url(url: str) -> str:
    """Return ``url`` if it is an https URL on an allow-listed host, else raise ``DownloadError``."""
    if not isinstance(url, str) or not url or len(url) > 8192 or any(ord(c) < 33 for c in url):
        raise DownloadError("Download URL is missing or malformed")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise DownloadError(f"Download URL is malformed: {redact(url)}") from exc
    if parsed.scheme != "https":
        raise DownloadError(f"Only https downloads are allowed, refused {redact(url)}")
    if parsed.username is not None or parsed.password is not None:
        raise DownloadError("Download URLs must not contain credentials")
    if port not in (None, 443):
        raise DownloadError(f"Downloads must use the standard https port, refused {redact(url)}")
    if not host_allowed(parsed.hostname):
        raise DownloadError(
            f"Downloads from {parsed.hostname or 'this address'} are not allowed "
            "(only Hugging Face and GitHub release hosts are)"
        )
    return url


# --------------------------------------------------------------------------- network layer

Opener = Callable[[urllib.request.Request, float], Any]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects surface as ``HTTPError`` so we can check each hop ourselves."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib API
        return None


_opener_override: Opener | None = None
_default_opener: Opener | None = None
_opener_lock = threading.Lock()


def set_opener(opener: Opener | None) -> None:
    """Replace the network layer: ``opener(request, timeout) -> response``.

    The response must behave like ``http.client.HTTPResponse`` and non-2xx
    answers (including redirects) must raise ``urllib.error.HTTPError``. Used by
    tests; ``None`` restores the real HTTPS opener. The URL allow-list is
    enforced before the opener is called either way.
    """
    global _opener_override
    _opener_override = opener


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    try:  # optional: bundled CA store for frozen apps / python.org builds on macOS
        import certifi  # type: ignore[import-not-found]

        context.load_verify_locations(certifi.where())
    except Exception:  # noqa: BLE001 - certifi is optional
        pass
    return context


def _get_opener() -> Opener:
    global _default_opener
    if _opener_override is not None:
        return _opener_override
    with _opener_lock:
        if _default_opener is None:
            director = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_tls_context()), NoRedirect)

            def _open(request: urllib.request.Request, timeout: float) -> Any:
                return director.open(request, timeout=timeout)

            _default_opener = _open
        return _default_opener


def _describe_network_error(exc: BaseException, url: str) -> str:
    reason = getattr(exc, "reason", exc)
    host = urlparse(url).hostname or "the server"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            f"TLS certificate check for {host} failed ({getattr(reason, 'verify_message', None) or reason}). "
            "Your Python has no trusted CA certificates; on macOS run 'Install Certificates.command' "
            "from the Python folder."
        )
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return f"{host} did not answer within {TIMEOUT_S:g} s"
    if isinstance(reason, socket.gaierror):
        return f"Could not resolve {host}; are you online?"
    text = str(reason).replace(url, redact(url))
    return f"Could not reach {host}: {text}"


def _close_quietly(obj: Any) -> None:
    try:
        obj.close()
    except Exception:  # noqa: BLE001 - best effort
        pass


def open_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = TIMEOUT_S,
    opener: Opener | None = None,
) -> tuple[Any, str]:
    """GET ``url`` following redirects only to allow-listed https hosts.

    Returns ``(response, final_url)``; the caller closes the response. Raises
    :class:`DownloadError` (or :class:`HTTPStatusError`) with a redacted message.
    """
    current = check_url(url)
    open_fn = opener or _get_opener()
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    for _hop in range(MAX_REDIRECTS + 1):
        request = urllib.request.Request(current, headers=request_headers, method="GET")
        try:
            response = open_fn(request, timeout)
        except urllib.error.HTTPError as exc:
            code, location = exc.code, (exc.headers.get("Location") if exc.headers else None)
            _close_quietly(exc)
            if code in REDIRECT_CODES:
                if not location:
                    raise DownloadError(f"{redact(current)} redirected without a target") from None
                current = check_url(urljoin(current, location.strip()))
                continue
            raise HTTPStatusError(code, f"{redact(current)} answered HTTP {code}") from None
        except urllib.error.URLError as exc:
            transient = not isinstance(exc.reason, ssl.SSLCertVerificationError)
            raise DownloadError(_describe_network_error(exc, current), transient=transient) from None
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadError(_describe_network_error(exc, current), transient=True) from None
        status = int(getattr(response, "status", 200))
        if status in REDIRECT_CODES:  # an opener that does not raise for 3xx
            location = response.headers.get("Location")
            _close_quietly(response)
            if not location:
                raise DownloadError(f"{redact(current)} redirected without a target")
            current = check_url(urljoin(current, location.strip()))
            continue
        if not 200 <= status < 300:
            _close_quietly(response)
            raise HTTPStatusError(status, f"{redact(current)} answered HTTP {status}")
        return response, current
    raise DownloadError(f"Too many redirects (more than {MAX_REDIRECTS}) for {redact(url)}")


def _parse_link_next(header: str | None) -> str | None:
    for part in (header or "").split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if match:
            return match.group(1)
    return None


def fetch_json_with_next(
    url: str, *, headers: dict[str, str] | None = None, timeout: float = 20.0, max_bytes: int = MAX_JSON_BYTES
) -> tuple[Any, str | None]:
    """Fetch a JSON document; also return the ``Link: rel="next"`` URL (pagination)."""
    response, final_url = open_url(url, headers={"Accept": "application/json", **(headers or {})}, timeout=timeout)
    try:
        raw = response.read(max_bytes + 1)
        link = response.headers.get("Link")
    except (OSError, http.client.HTTPException) as exc:
        raise DownloadError(_describe_network_error(exc, final_url), transient=True) from None
    finally:
        _close_quietly(response)
    if len(raw) > max_bytes:
        raise DownloadError(f"The answer from {redact(final_url)} is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DownloadError(f"{redact(final_url)} did not return valid JSON") from exc
    next_url = _parse_link_next(link)
    return data, (urljoin(final_url, next_url) if next_url else None)


def fetch_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> Any:
    """GET a small JSON document from an allow-listed host (same rules as downloads)."""
    return fetch_json_with_next(url, headers=headers, timeout=timeout)[0]


# --------------------------------------------------------------------------- jobs


class _Job:
    def __init__(
        self,
        job_id: str,
        url: str,
        dest: Path,
        *,
        label: str,
        kind: str,
        sha256: str | None,
        size: int | None,
        extract: str | None,
        extract_dir: Path | None,
        on_complete: Callable[[Path], None] | None,
        opener: Opener | None,
    ):
        self.id = job_id
        self.url = url
        self.dest = dest
        self.label = label
        self.kind = kind
        self.sha256 = sha256
        self.size = size
        self.extract = extract  # "zip" | "tar.gz" | "auto" (detect) | None
        self.extract_dir = extract_dir  # set when ``extract`` was given as a directory
        self.on_complete = on_complete
        self.opener = opener
        self.state = "queued"
        self.bytes_done = 0
        self.bytes_total: int | None = size
        self.error: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.cancel_event = threading.Event()
        self.done_event = threading.Event()
        self.response: Any = None
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "label": self.label,
                "kind": self.kind,
                "state": self.state,
                "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    def update(self, **values: Any) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    @property
    def active(self) -> bool:
        with self.lock:
            return self.state in ACTIVE_STATES

    def finish(self, state: str, error: str | None = None) -> None:
        with self.lock:
            self.state = state
            self.error = error[:2000] if error else None
            self.finished_at = db.utcnow()
            self.response = None
        self.done_event.set()


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_PARALLEL)


def _clean_label(label: object) -> str:
    if not isinstance(label, str) or not label.strip():
        raise ValueError("A download needs a label")
    return re.sub(r"[\x00-\x1f\x7f]", " ", label).strip()[:200]


def _validate_dest(dest: Path | str) -> Path:
    """``dest`` must be below LOWRAM_HOME and must not be a symlink."""
    path = Path(dest).expanduser()
    if not path.is_absolute():
        raise ValueError("Download destination must be an absolute path")
    if path.name in ("", ".", "..") or ".." in path.parts:
        raise ValueError("Download destination is not a valid file name")
    home = config.home_dir().resolve()
    parent = path.parent.resolve()
    if parent != home and home not in parent.parents:
        raise ValueError("Downloads may only be saved inside the LowRAM AI data folder")
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / path.name
    for candidate in (final, Path(str(final) + ".part")):
        if candidate.is_symlink():
            raise ValueError(f"Refusing to write through a symbolic link: {candidate.name}")
    return final


def start(
    job_id: str,
    url: str,
    dest: Path | str,
    *,
    sha256: str | None = None,
    size: int | None = None,
    label: str,
    kind: str,
    extract: str | os.PathLike | None = None,
    on_complete: Callable[[Path], None] | None = None,
    opener: Opener | None = None,
) -> dict:
    """Queue a download and return its ``Download`` dict.

    ``extract`` may be

    * ``None``: ``dest`` is the final file;
    * ``"zip"`` / ``"tar.gz"``: ``dest`` is the *directory* the archive is
      unpacked into (the archive itself is deleted afterwards);
    * a directory path: the archive is unpacked into that directory (format
      detected from the file name or magic bytes) and then kept at ``dest``.

    Whatever is visible at ``dest`` / the extraction directory only appears
    after the download was completely received and verified (and unpacked).
    ``on_complete(path)`` runs in the worker before the job is marked ``done``
    (``path`` is the extraction directory, else ``dest``); if it raises, the
    job fails and the result is removed. Starting an id that is still active
    returns the running job unchanged.
    """
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("Download id must be 1-80 characters of A-Z, a-z, 0-9, '.', '_' or '-'")
    label = _clean_label(label)
    if kind not in KINDS:
        raise ValueError(f"Unknown download kind {kind!r}")
    extract_dir: Path | None = None
    if isinstance(extract, os.PathLike) or (isinstance(extract, str) and extract not in EXTRACT_FORMATS):
        if not os.path.isabs(os.fspath(extract)):
            raise ValueError(f"extract must be 'zip', 'tar.gz' or an absolute directory path, not {extract!r}")
        extract_dir, extract = _validate_dest(extract), "auto"
    elif extract is not None and extract not in EXTRACT_FORMATS:
        raise ValueError(f"Unsupported archive format {extract!r}")
    if sha256 is not None:
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise ValueError("sha256 must be 64 hexadecimal characters")
        sha256 = sha256.lower()
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size <= 0):
        raise ValueError("size must be a positive integer")
    try:
        check_url(url)
    except DownloadError as exc:
        raise ValueError(str(exc)) from None
    target = _validate_dest(dest)
    if extract_dir is not None and extract_dir == target:
        raise ValueError("The extraction directory must differ from the archive path")

    with _jobs_lock:
        existing = _jobs.get(job_id)
        if existing is not None and existing.active:
            return existing.snapshot()
        mine = {target, extract_dir} - {None}
        for other in _jobs.values():
            if other.active and mine & ({other.dest, other.extract_dir} - {None}):
                raise ValueError(f"Another download ({other.id}) is already writing {target.name}")
        job = _Job(
            job_id, url, target, label=label, kind=kind, sha256=sha256, size=size,
            extract=extract, extract_dir=extract_dir, on_complete=on_complete, opener=opener,
        )
        _jobs.pop(job_id, None)
        _jobs[job_id] = job
        _prune_locked()
        snapshot = job.snapshot()
        threading.Thread(target=_worker, args=(job,), name=f"lowram-download-{job_id}", daemon=True).start()
    log.info("Download %s queued: %s -> %s", job_id, redact(url), target)
    return snapshot


def _prune_locked() -> None:
    finished = [jid for jid, job in _jobs.items() if not job.active]
    for jid in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
        del _jobs[jid]


def get(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(str(job_id))
    return job.snapshot() if job else None


def list_jobs() -> list[dict]:
    """All known downloads, newest first."""
    with _jobs_lock:
        jobs = list(_jobs.values())
    return [job.snapshot() for job in reversed(jobs)]


def cancel(job_id: str, wait_s: float = CANCEL_WAIT_S) -> dict | None:
    """Request cancellation; waits briefly so the answer usually says ``cancelled``.

    Returns ``None`` for an unknown id; a finished job is returned unchanged.
    """
    with _jobs_lock:
        job = _jobs.get(str(job_id))
    if job is None:
        return None
    if job.active:
        job.cancel_event.set()
        with job.lock:
            response = job.response
        _abort_response(response)
        job.done_event.wait(wait_s)
    return job.snapshot()


def wait(job_id: str, timeout: float | None = None) -> dict | None:
    """Block until the job finished (or ``timeout``); returns its current dict."""
    with _jobs_lock:
        job = _jobs.get(str(job_id))
    if job is None:
        return None
    job.done_event.wait(timeout)
    return job.snapshot()


def _abort_response(response: Any) -> None:
    """Unblock a worker stuck in ``read()`` by shutting the socket down (best effort)."""
    if response is None:
        return
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _reset_for_tests() -> None:
    """Cancel and forget all jobs (test isolation)."""
    with _jobs_lock:
        jobs = list(_jobs.values())
        _jobs.clear()
    for job in jobs:
        job.cancel_event.set()
        _abort_response(job.response)
    for job in jobs:
        job.done_event.wait(5)


# --------------------------------------------------------------------------- worker


def _part_path(dest: Path) -> Path:
    return Path(str(dest) + ".part")


def _remove(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Could not remove %s: %s", path, exc)


def _check_disk(folder: Path, needed: int) -> None:
    free = shutil.disk_usage(folder).free
    if free < needed + DISK_HEADROOM_BYTES:
        raise DownloadError(
            f"Not enough disk space: {needed / GIB:.1f} GiB needed plus {DISK_HEADROOM_BYTES / GIB:.0f} GiB "
            f"reserve, only {free / GIB:.1f} GiB free. Free up space and try again."
        )


def _acquire_slot(job: _Job) -> bool:
    while not job.cancel_event.is_set():
        if _slots.acquire(timeout=0.2):
            return True
    return False


def _worker(job: _Job) -> None:
    part = _part_path(job.dest)
    if not _acquire_slot(job):
        job.finish("cancelled")
        return
    try:
        job.update(state="downloading", started_at=db.utcnow())
        hasher = _download_with_retries(job, part)
        if job.cancel_event.is_set():
            raise _Cancelled
        job.update(state="verifying")
        _verify(job, part, hasher)
        if job.extract:
            job.update(state="extracting")
            final = _extract(job, part)
        else:
            os.chmod(part, 0o644)
            os.replace(part, job.dest)
            final = job.dest
        if job.on_complete is not None:
            try:
                job.on_complete(final)
            except Exception as exc:  # noqa: BLE001 - reported as the job's failure
                log.warning("Post-install step for %s failed: %s", job.id, exc)
                _remove(final)
                if job.extract_dir is not None:
                    _remove(job.dest)
                raise DownloadError(str(exc) or type(exc).__name__) from exc
        job.finish("done")
        log.info("Download %s finished (%s bytes)", job.id, job.bytes_done)
    except _Cancelled:
        _remove(part)
        job.finish("cancelled")
        log.info("Download %s cancelled", job.id)
    except DownloadError as exc:
        if exc.discard_part:
            _remove(part)
        job.finish("failed", str(exc))
        log.warning("Download %s failed: %s", job.id, exc)
    except OSError as exc:
        job.finish("failed", f"File system error: {exc.strerror or exc}")
        log.warning("Download %s failed: %s", job.id, exc)
    except Exception:  # noqa: BLE001 - never leave a job hanging in an active state
        log.exception("Download %s crashed", job.id)
        job.finish("failed", "Unexpected error; see the server log")
    finally:
        _slots.release()


def _download_with_retries(job: _Job, part: Path) -> Any:
    attempt = 1
    while True:
        try:
            return _download_once(job, part)
        except DownloadError as exc:
            if job.cancel_event.is_set():
                raise _Cancelled from None
            if not exc.transient or attempt >= MAX_ATTEMPTS:
                raise
            delay = RETRY_DELAYS_S[min(attempt - 1, len(RETRY_DELAYS_S) - 1)]
            log.warning("Download %s: %s; retrying in %.0f s (attempt %d/%d)", job.id, exc, delay, attempt + 1, MAX_ATTEMPTS)
            attempt += 1
            if job.cancel_event.wait(delay):
                raise _Cancelled from None


def _hash_existing(part: Path, hasher: Any, job: _Job) -> None:
    with open(part, "rb") as handle:
        while True:
            if job.cancel_event.is_set():
                raise _Cancelled
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return
            hasher.update(chunk)


def _parse_content_range(value: str | None) -> tuple[int, int | None]:
    match = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", value or "")
    if not match:
        raise DownloadError("The server sent an invalid Content-Range header", transient=True)
    total = None if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), total


def _content_length(response: Any) -> int | None:
    raw = response.headers.get("Content-Length")
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


def _read_chunk(response: Any) -> bytes:
    read1 = getattr(response, "read1", None)
    return read1(CHUNK_BYTES) if read1 is not None else response.read(CHUNK_BYTES)


def _open_part(part: Path, append: bool) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    flags |= os.O_APPEND if append else os.O_TRUNC
    return os.fdopen(os.open(part, flags, 0o600), "ab" if append else "wb")


def _download_once(job: _Job, part: Path) -> Any:
    """One HTTP attempt; resumes from an existing ``.part`` with a Range request."""
    if part.is_symlink():
        part.unlink()
    existing = part.stat().st_size if part.exists() else 0
    if job.size is not None and existing > job.size:
        _remove(part)
        existing = 0
    hasher = hashlib.sha256()
    if job.size is not None and existing == job.size:  # complete from an earlier run: just verify
        _hash_existing(part, hasher, job)
        job.update(bytes_done=existing, bytes_total=job.size)
        return hasher
    _check_disk(part.parent, (job.size - existing) if job.size else 0)

    headers = {"Accept-Encoding": "identity"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    try:
        response, final_url = open_url(job.url, headers=headers, opener=job.opener)
    except HTTPStatusError as exc:
        if exc.code == 416 and existing:  # our partial file no longer matches: start over
            _remove(part)
            raise DownloadError("The partial download did not match the server's file; restarting", transient=True) from None
        raise
    job.update(response=response)
    try:
        status = int(getattr(response, "status", 200))
        if existing and status == 206:
            offset, total = _parse_content_range(response.headers.get("Content-Range"))
            if offset != existing:
                raise DownloadError("The server resumed at the wrong position", transient=True, discard_part=True)
            _hash_existing(part, hasher, job)
            append = True
        elif status == 200:
            existing, append = 0, False
            total = _content_length(response)
        else:
            raise DownloadError(f"Unexpected HTTP {status} from {redact(final_url)}")
        if total is None and append:
            length = _content_length(response)
            total = existing + length if length is not None else None
        if job.size is not None and total is not None and total != job.size:
            raise DownloadError(
                f"The server offers {total} bytes but {job.size} were expected; refusing the file", discard_part=True
            )
        total = total if total is not None else job.size
        if total is not None:
            _check_disk(part.parent, total - existing)
        limit = job.size if job.size is not None else (total if total is not None else MAX_UNKNOWN_SIZE_BYTES)
        job.update(bytes_done=existing, bytes_total=total)

        done, last_report, next_disk_check = existing, time.monotonic(), existing + DISK_CHECK_EVERY_BYTES
        with _open_part(part, append) as handle:
            while True:
                if job.cancel_event.is_set():
                    raise _Cancelled
                try:
                    chunk = _read_chunk(response)
                except (OSError, http.client.HTTPException) as exc:
                    if job.cancel_event.is_set():
                        raise _Cancelled from None
                    raise DownloadError(
                        f"Connection lost after {done} bytes ({_describe_network_error(exc, final_url)})", transient=True
                    ) from None
                if not chunk:
                    break
                if done + len(chunk) > limit:
                    raise DownloadError("The server sent more data than expected; refusing the file", discard_part=True)
                handle.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last_report >= PROGRESS_INTERVAL_S:
                    job.update(bytes_done=done)
                    last_report = now
                if done >= next_disk_check:
                    handle.flush()
                    _check_disk(part.parent, (total - done) if total else 0)
                    next_disk_check = done + DISK_CHECK_EVERY_BYTES
            handle.flush()
            os.fsync(handle.fileno())
        job.update(bytes_done=done)
        if job.cancel_event.is_set():
            raise _Cancelled
        if total is not None and done < total:
            raise DownloadError(f"The connection closed early ({done} of {total} bytes)", transient=True)
        if total is None:
            job.update(bytes_total=done)
        return hasher
    finally:
        job.update(response=None)
        _close_quietly(response)


def _verify(job: _Job, part: Path, hasher: Any) -> None:
    actual = part.stat().st_size
    if job.size is not None and actual != job.size:
        raise DownloadError(
            f"Size check failed: got {actual} bytes, expected {job.size}; the file was deleted", discard_part=True
        )
    if job.sha256 is not None and hasher.hexdigest() != job.sha256:
        raise DownloadError(
            "SHA-256 check failed: the file is corrupt or was tampered with; it was deleted", discard_part=True
        )


# --------------------------------------------------------------------------- safe extraction

_LINK_TARGET_RE = re.compile(r"[A-Za-z0-9._+-]{1,255}")


class _SafeExtractor:
    """Writes archive members below ``root`` and refuses anything that could escape it.

    * no absolute paths, drive letters, ``..`` components or NUL bytes;
    * never writes through an existing symlink (each parent must be a real dir,
      files are created with ``O_EXCL``/``O_NOFOLLOW``);
    * symlinks only to a plain sibling name in the same folder (e.g.
      ``libggml.so -> libggml.so.0``), hard links only to files already extracted;
    * no device files or FIFOs; size and member-count limits against bombs.
    """

    def __init__(self, root: Path, cancel: threading.Event):
        self.root = root
        self.cancel = cancel
        self.written = 0
        self.members = 0
        self.files: set[tuple[str, ...]] = set()

    def parts(self, name: str) -> tuple[str, ...]:
        if "\x00" in name:
            raise UnsafeArchiveError("Archive member name contains a NUL byte")
        clean = name.replace("\\", "/")
        if clean.startswith("/") or re.match(r"^[A-Za-z]:", clean):
            raise UnsafeArchiveError(f"Archive contains an absolute path: {name[:200]!r}")
        parts = tuple(p for p in clean.split("/") if p not in ("", "."))
        if any(p == ".." for p in parts):
            raise UnsafeArchiveError(f"Archive contains a path that leaves its folder: {name[:200]!r}")
        if any(":" in p for p in parts) and os.name == "nt":
            raise UnsafeArchiveError(f"Archive member name is not allowed: {name[:200]!r}")
        return parts

    def _tick(self) -> None:
        if self.cancel.is_set():
            raise _Cancelled
        self.members += 1
        if self.members > MAX_EXTRACT_MEMBERS:
            raise UnsafeArchiveError(f"Archive has more than {MAX_EXTRACT_MEMBERS} entries")

    def _dir_for(self, parts: tuple[str, ...]) -> Path:
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise UnsafeArchiveError(f"Archive writes through a symbolic link: {'/'.join(parts)[:200]!r}")
            if current.exists():
                if not current.is_dir():
                    raise UnsafeArchiveError(f"Archive uses a file as a folder: {'/'.join(parts)[:200]!r}")
            else:
                current.mkdir(mode=0o755)
        return current

    def _clear(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            raise UnsafeArchiveError(f"Archive replaces a folder with a file: {path.name!r}")

    def directory(self, name: str) -> None:
        self._tick()
        parts = self.parts(name)
        if parts:
            self._dir_for(parts)

    def file(self, name: str, source: BinaryIO, mode: int, declared: int | None) -> None:
        self._tick()
        parts = self.parts(name)
        if not parts:
            return
        target = self._dir_for(parts[:-1]) / parts[-1]
        self._clear(target)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        written = 0
        with os.fdopen(os.open(target, flags, 0o600), "wb") as out:
            while True:
                if self.cancel.is_set():
                    raise _Cancelled
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                self.written += len(chunk)
                if declared is not None and written > declared:
                    raise UnsafeArchiveError(f"Archive member is larger than declared: {name[:200]!r}")
                if self.written > MAX_EXTRACT_BYTES:
                    raise UnsafeArchiveError(f"Archive unpacks to more than {MAX_EXTRACT_BYTES / GIB:.0f} GiB")
                out.write(chunk)
        os.chmod(target, 0o755 if mode & 0o111 else 0o644)
        self.files.add(parts)

    def symlink(self, name: str, link_target: str) -> None:
        self._tick()
        parts = self.parts(name)
        if not parts:
            raise UnsafeArchiveError("Archive contains a nameless link")
        if (
            len(link_target) > MAX_LINK_TARGET_CHARS
            or not _LINK_TARGET_RE.fullmatch(link_target)
            or link_target in (".", "..")
        ):
            raise UnsafeArchiveError(
                f"Archive contains a link that points outside its folder: {name[:200]!r} -> {link_target[:200]!r}"
            )
        folder = self._dir_for(parts[:-1])
        target = folder / parts[-1]
        self._clear(target)
        try:
            os.symlink(link_target, target)
        except (OSError, NotImplementedError):  # e.g. Windows without privileges: copy instead
            sibling = folder / link_target
            if sibling.is_file() and not sibling.is_symlink():
                shutil.copyfile(sibling, target)
                os.chmod(target, stat.S_IMODE(sibling.stat().st_mode))
            else:
                raise UnsafeArchiveError(f"Could not create link {name[:200]!r}") from None

    def hardlink(self, name: str, link_target: str) -> None:
        self._tick()
        parts, source_parts = self.parts(name), self.parts(link_target)
        if not parts or source_parts not in self.files:
            raise UnsafeArchiveError(f"Archive contains a hard link to an unknown file: {name[:200]!r}")
        source = self.root.joinpath(*source_parts)
        target = self._dir_for(parts[:-1]) / parts[-1]
        self._clear(target)
        shutil.copyfile(source, target, follow_symlinks=False)
        os.chmod(target, stat.S_IMODE(source.stat().st_mode))
        self.files.add(parts)


def _extract_zip(archive: Path, writer: _SafeExtractor, folder: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_EXTRACT_MEMBERS:
            raise UnsafeArchiveError(f"Archive has more than {MAX_EXTRACT_MEMBERS} entries")
        total = sum(info.file_size for info in infos)
        if total > MAX_EXTRACT_BYTES:
            raise UnsafeArchiveError(f"Archive unpacks to more than {MAX_EXTRACT_BYTES / GIB:.0f} GiB")
        _check_disk(folder, total)
        for info in infos:
            unix_mode = (info.external_attr >> 16) if info.create_system == 3 else 0
            if info.is_dir():
                writer.directory(info.filename)
            elif stat.S_ISLNK(unix_mode):
                with zf.open(info) as handle:
                    link_target = handle.read(MAX_LINK_TARGET_CHARS + 1).decode("utf-8", "replace")
                writer.symlink(info.filename, link_target)
            elif unix_mode and not stat.S_ISREG(unix_mode):
                raise UnsafeArchiveError(f"Archive contains a special file: {info.filename[:200]!r}")
            else:
                with zf.open(info) as handle:
                    writer.file(info.filename, handle, unix_mode, info.file_size)


def _extract_tar(archive: Path, writer: _SafeExtractor, folder: Path) -> None:
    with tarfile.open(archive, mode="r:gz") as tf:
        members = []
        total = 0
        for member in tf:  # streams the index; bounded by the member limit
            members.append(member)
            if len(members) > MAX_EXTRACT_MEMBERS:
                raise UnsafeArchiveError(f"Archive has more than {MAX_EXTRACT_MEMBERS} entries")
            if member.isreg():
                total += member.size
        if total > MAX_EXTRACT_BYTES:
            raise UnsafeArchiveError(f"Archive unpacks to more than {MAX_EXTRACT_BYTES / GIB:.0f} GiB")
        _check_disk(folder, total)
        for member in members:
            if member.isdir():
                writer.directory(member.name)
            elif member.isreg():
                handle = tf.extractfile(member)
                if handle is None:
                    raise UnsafeArchiveError(f"Archive member cannot be read: {member.name[:200]!r}")
                with handle:
                    writer.file(member.name, handle, member.mode, member.size)
            elif member.issym():
                writer.symlink(member.name, member.linkname)
            elif member.islnk():
                writer.hardlink(member.name, member.linkname)
            else:
                raise UnsafeArchiveError(f"Archive contains a special file: {member.name[:200]!r}")


def _detect_format(archive: Path, *names: str) -> str:
    for name in names:
        lower = name.lower()
        if lower.endswith(".zip"):
            return "zip"
        if lower.endswith((".tar.gz", ".tgz")):
            return "tar.gz"
    with open(archive, "rb") as handle:
        magic = handle.read(4)
    if magic.startswith(b"PK\x03\x04") or magic.startswith(b"PK\x05\x06"):
        return "zip"
    if magic.startswith(b"\x1f\x8b"):
        return "tar.gz"
    raise DownloadError("The downloaded file is neither a .zip nor a .tar.gz archive", discard_part=True)


def _extract(job: _Job, archive: Path) -> Path:
    dest = job.extract_dir or job.dest
    fmt = job.extract
    if fmt == "auto":
        fmt = _detect_format(archive, job.dest.name, urlparse(job.url).path)
    staging = dest.parent / f".{dest.name}.extract-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o700)
    try:
        writer = _SafeExtractor(staging, job.cancel_event)
        try:
            if fmt == "zip":
                _extract_zip(archive, writer, dest.parent)
            else:
                _extract_tar(archive, writer, dest.parent)
        except (zipfile.BadZipFile, tarfile.TarError, EOFError, RuntimeError) as exc:
            if isinstance(exc, DownloadError):
                raise
            raise DownloadError(f"The downloaded archive is damaged or unsupported: {exc}", discard_part=True) from None
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.exists():
            old = dest.parent / f".{dest.name}.old-{secrets.token_hex(4)}"
            os.replace(dest, old)
            shutil.rmtree(old, ignore_errors=True)
        os.replace(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if job.extract_dir is not None:  # keep the verified archive at its final path, last
        os.chmod(archive, 0o644)
        os.replace(archive, job.dest)
    else:
        _remove(archive)
    return dest


# --------------------------------------------------------------------------- routes


@route("GET", r"/api/downloads")
def route_list(req: Request) -> Response:
    return Response({"downloads": list_jobs()})


@route("POST", r"/api/downloads/([A-Za-z0-9._-]{1,80})/cancel")
def route_cancel(req: Request, _job_id: object) -> Response:
    req.json()
    job_id = req.path.split("/")[3]  # the raw string: the router turns digit-only ids into ints
    job = cancel(job_id)
    if job is None:
        raise HTTPError(HTTPStatus.NOT_FOUND, "Download not found")
    return Response({"download": job})
