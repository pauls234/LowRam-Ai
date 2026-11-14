"""LowRAM AI Studio desktop launcher: double-click, and everything starts.

``main(argv)`` starts the local server (``backend.app.start_background``) on
127.0.0.1 - or reuses a LowRAM AI that already answers there - and shows the
dashboard in a native window (pywebview). Without pywebview, with
``--no-window``, or when no native web view is available, it opens the default
browser and keeps serving until Ctrl+C / SIGTERM. Closing the window or
stopping the process runs ``backend.app.shutdown`` (shutdown hooks also stop a
managed llama-server).

Logs: ``<LOWRAM_HOME>/logs/app.log`` (rotating, 5 x 1 MB).

Run from source: ``python -m desktop.launcher [--no-window] [--port N]``.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import contextlib
import errno
import hashlib
import hmac
import http.client
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from html import escape
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

if __package__ in (None, "") and not getattr(sys, "frozen", False):
    # ``python desktop/launcher.py``: make the repository root importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP_NAME = "LowRAM AI"
LOOPBACK = "127.0.0.1"
LOOPBACK_NAMES = ("127.0.0.1", "::1", "localhost")
DEFAULT_PORT = 8765
LAST_PORT = 8785
READY_TIMEOUT_S = 20.0
PROBE_TIMEOUT_S = 1.0
WINDOW_SIZE = (1280, 820)
MIN_WINDOW_SIZE = (900, 600)
WINDOW_BACKGROUND = "#f5f5f4"
LOG_FILE_BYTES = 1024 * 1024
LOG_BACKUPS = 5
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
INSTANCE_FILE = "instance.json"
_TOKEN_META = re.compile(r'<meta\s+name="lowram-token"\s+content="([^"<>]{8,512})"')
_FILE_PATTERN = re.compile(r"\*\.[A-Za-z0-9]{1,16}")
# Windows reports "port in use" / "access denied" for bind() with these codes.
_WIN_BIND_ERRORS = (10013, 10048)

log = logging.getLogger("lowram.desktop")


class LauncherError(RuntimeError):
    """A startup problem whose message is shown to the user."""


# --------------------------------------------------------------------------- frozen app


def is_frozen() -> bool:
    """True inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Directory that holds the ``backend`` and ``desktop`` data files.

    From source this is the repository root. In a PyInstaller bundle it is
    ``sys._MEIPASS`` (onedir: ``<dist>/LowRAM AI/_internal``; macOS app:
    ``Contents/Frameworks``, whose data entries link into ``Contents/Resources``).
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_static_dir() -> Path:
    return bundle_root() / "backend" / "static"


def ensure_static_dir(app_module: ModuleType) -> Path:
    """Make sure the server finds the dashboard files; returns the directory used.

    ``backend.app.STATIC_DIR`` is ``Path(__file__).resolve().parent / "static"``.
    PyInstaller sets a bundled module's ``__file__`` to
    ``<_MEIPASS>/backend/app.pyc`` and the spec copies the files to
    ``<_MEIPASS>/backend/static``, so normally nothing changes here. Should a
    bundle layout ever differ, the server is pointed at the bundled copy (and
    the mismatch is logged) instead of answering every page with 404.
    """
    current = Path(app_module.STATIC_DIR)
    if (current / "index.html").is_file():
        return current
    fallback = bundled_static_dir()
    if (fallback / "index.html").is_file():
        log.warning("Dashboard files not found in %s; serving %s instead", current, fallback)
        types = app_module.STATIC_TYPES
        app_module.STATIC_DIR = fallback
        app_module.STATIC_FILES = {
            path.name: types[path.suffix.lower()]
            for path in sorted(fallback.glob("*"))
            if path.is_file() and path.suffix.lower() in types
        }
        return fallback
    raise LauncherError(
        f"The dashboard files are missing (looked in {current} and {fallback}). Please reinstall LowRAM AI."
    )


def configure_certificates() -> str | None:
    """Give HTTPS downloads a CA bundle when the bundled Python has none.

    A frozen app cannot use the build machine's OpenSSL paths, and python.org's
    macOS Python ships without certificates until "Install Certificates" runs.
    ``SSL_CERT_FILE`` (honoured by ``ssl.create_default_context``) then points
    at certifi's bundle. An explicit ``SSL_CERT_FILE`` is never overridden.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return None
    paths = ssl.get_default_verify_paths()
    system_ok = bool(paths.cafile and os.path.isfile(paths.cafile)) or bool(
        paths.capath and os.path.isdir(paths.capath) and any(os.scandir(paths.capath))
    )
    if (system_ok or sys.platform == "win32") and not is_frozen():
        return None  # Windows uses the system certificate store
    try:
        import certifi
    except ImportError:
        return None
    cafile = certifi.where()
    if not os.path.isfile(cafile):
        return None
    os.environ["SSL_CERT_FILE"] = cafile
    return cafile


def configure_environment() -> None:
    """Environment fixes for an app started by Finder/Explorer instead of a shell."""
    if is_frozen() and sys.platform == "darwin":
        # Finder starts apps with PATH=/usr/bin:/bin:/usr/sbin:/sbin; append
        # Homebrew so an installed llama-server can be found (system tools keep priority).
        parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        extra = [p for p in ("/opt/homebrew/bin", "/usr/local/bin") if os.path.isdir(p) and p not in parts]
        if extra:
            os.environ["PATH"] = os.pathsep.join(parts + extra)
    cafile = configure_certificates()
    if cafile:
        log.info("Using CA certificates from %s", cafile)


# --------------------------------------------------------------------------- ports and instances


def candidate_ports(explicit: int | None = None) -> list[int]:
    """An explicit port is used as is; otherwise 8765 or the next free one up to 8785."""
    if explicit is not None:
        return [explicit]
    return list(range(DEFAULT_PORT, LAST_PORT + 1))


def port_is_free(port: int) -> bool:
    """True if nothing listens on 127.0.0.1:``port`` and the server may bind it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # The server sets SO_REUSEADDR, which on Windows can steal a port in
            # use; an exclusive test bind fails whenever anything holds it.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((LOOPBACK, port))
        return True
    except OSError:
        pass
    finally:
        sock.close()
    if os.name == "nt":
        return False
    # POSIX: bind() without SO_REUSEADDR also fails while old connections sit
    # in TIME_WAIT (e.g. right after a restart); the server's SO_REUSEADDR
    # ignores those. Only a live listener makes the port busy - this keeps the
    # port (and the window's saved settings, which are per origin) stable.
    try:
        with socket.create_connection((LOOPBACK, port), timeout=0.5):
            return False
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def probe_instance(port: int, timeout: float = PROBE_TIMEOUT_S) -> dict | None:
    """LowRAM AI's ``/api/status`` JSON if it answers on ``port``, else None."""
    conn = http.client.HTTPConnection(LOOPBACK, port, timeout=timeout)
    try:
        conn.request("GET", "/api/status", headers={"Accept": "application/json", "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read(256 * 1024)
        if resp.status != 200:
            return None
        data = json.loads(body.decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()
    if isinstance(data, dict) and isinstance(data.get("version"), str) and isinstance(data.get("features"), list):
        return data
    return None


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _process_start_time(pid: int) -> float | None:
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, ValueError, OverflowError):
        return None


def write_instance_file(home: Path, port: int, token: str) -> tuple[Path, str]:
    """Record this process as the running instance: ``<home>/instance.json``, 0600, atomic.

    Stores the SHA-256 of the server's per-process page token (never the token).
    Returns the path and a random nonce that marks the file as ours.
    """
    path = home / INSTANCE_FILE
    nonce = secrets.token_hex(16)
    data = {
        "port": port, "pid": os.getpid(), "create_time": _process_start_time(os.getpid()),
        "token_sha256": _token_digest(token), "nonce": nonce,
    }
    tmp = home / f".{INSTANCE_FILE}.{nonce}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path, nonce


def remove_instance_file(path: Path, nonce: str) -> None:
    """Delete the instance file if it is still ours (a newer instance may have replaced it)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("nonce") == nonce:
        with contextlib.suppress(OSError):
            path.unlink()


def _read_page_token(port: int) -> str | None:
    conn = http.client.HTTPConnection(LOOPBACK, port, timeout=PROBE_TIMEOUT_S)
    try:
        conn.request("GET", "/", headers={"Connection": "close"})
        resp = conn.getresponse()
        body = resp.read(2 * 1024 * 1024)
        if resp.status != 200:
            return None
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    match = _TOKEN_META.search(body.decode("utf-8", "replace"))
    return match.group(1) if match else None


def find_verified_instance(home: Path) -> int | None:
    """Port of *this user's* running LowRAM AI (proven by ``<home>/instance.json``), else None.

    Anything else answering on 127.0.0.1 - another user's LowRAM AI, an old
    process, an impostor - is never opened in our window (it would share the
    window's storage). Reused only if the file is a regular file owned by this
    user and not writable by others, its process is alive with the recorded
    start time, and the server on the recorded port serves the page token
    whose SHA-256 the file holds.
    """
    path = home / INSTANCE_FILE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Cannot read %s: %s", path, exc)
        return None
    if not stat.S_ISREG(info.st_mode):  # lstat: a symlink is not a regular file
        log.warning("Ignoring %s: not a regular file", path)
        return None
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        log.warning("Ignoring %s: not owned by this user or writable by others", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        port, pid, started, digest = data["port"], data["pid"], data["create_time"], data["token_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("Ignoring unreadable %s", path)
        return None
    if not (isinstance(port, int) and 1024 <= port <= 65535 and isinstance(pid, int) and pid > 0
            and isinstance(started, (int, float)) and isinstance(digest, str)):
        log.warning("Ignoring malformed %s", path)
        return None
    actual = _process_start_time(pid)
    if actual is None or abs(actual - started) > 1.0:
        log.info("The previous LowRAM AI (pid %s) is no longer running", pid)
        return None
    token = _read_page_token(port) if probe_instance(port) is not None else None
    if token is None or not hmac.compare_digest(_token_digest(token), digest):
        log.warning("The server on port %s could not prove it is this LowRAM AI; not reusing it", port)
        return None
    return port


def wait_until_ready(port: int, timeout: float = READY_TIMEOUT_S) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status = probe_instance(port)
        if status is not None:
            return status
        if time.monotonic() >= deadline:
            raise LauncherError(f"The local server did not answer within {timeout:g} seconds.")
        time.sleep(0.1)


def start_server(app_module: ModuleType, ports: Sequence[int]) -> Any:
    """``app.start_background`` on the first free port (127.0.0.1 only); sets ``LOWRAM_PORT``."""
    previous = os.environ.get("LOWRAM_PORT")
    for port in ports:
        if not port_is_free(port):
            log.info("Port %s is in use; trying the next one", port)
            continue
        os.environ["LOWRAM_PORT"] = str(port)  # config.port() must agree with the real port
        try:
            return app_module.start_background(port)
        except OSError as exc:  # lost a race for the port
            if exc.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(exc, "winerror", None) in _WIN_BIND_ERRORS:
                log.info("Port %s became busy (%s); trying the next one", port, exc)
                continue
            raise
    if previous is None:
        os.environ.pop("LOWRAM_PORT", None)
    else:
        os.environ["LOWRAM_PORT"] = previous
    if len(ports) == 1:
        raise LauncherError(f"Port {ports[0]} on {LOOPBACK} is used by another program. Choose another port.")
    raise LauncherError(
        f"No free port between {ports[0]} and {ports[-1]} on {LOOPBACK}. Close other programs and try again."
    )


class Backend:
    """The server this process started (``owned``) or found already running."""

    def __init__(self, port: int, server: Any = None, app_module: ModuleType | None = None):
        self.port = port
        self.server = server
        self._app = app_module
        self._lock = threading.Lock()
        self._stopped = False
        self.instance: tuple[Path, str] | None = None  # (instance.json, nonce) while we own it

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}/"

    @property
    def owned(self) -> bool:
        return self.server is not None

    def stop(self) -> None:
        """Clean shutdown of an owned server (idempotent; never stops another process's server)."""
        with self._lock:
            if self._stopped or self.server is None:
                self._stopped = True
                return
            self._stopped = True
        log.info("Stopping LowRAM AI on port %s", self.port)
        try:
            self._app.shutdown(self.server)
        except Exception:
            log.exception("Clean shutdown failed")
        else:
            log.info("LowRAM AI stopped")
        finally:
            if self.instance is not None:
                remove_instance_file(*self.instance)


def start_or_reuse(ports: Sequence[int], home: Path, ready_timeout: float = READY_TIMEOUT_S) -> Backend:
    existing = find_verified_instance(home)
    if existing is not None:
        log.info("LowRAM AI already runs on port %s; opening it instead of starting a second server", existing)
        os.environ["LOWRAM_PORT"] = str(existing)
        return Backend(existing)
    from backend import __version__, app, config  # imported late: logging is set up by now

    if config.HOST not in LOOPBACK_NAMES:
        raise LauncherError(f"Refusing to serve on non-loopback address {config.HOST!r}.")
    ensure_static_dir(app)
    server = start_server(app, ports)
    backend = Backend(server.server_address[1], server, app)
    for problem in getattr(server, "startup_errors", None) or []:
        log.warning("Startup problem: %s", problem)
    try:
        wait_until_ready(backend.port, ready_timeout)
    except BaseException:
        backend.stop()
        raise
    try:
        backend.instance = write_instance_file(home, backend.port, server.csrf_token)
    except OSError as exc:  # single-instance detection only; not fatal
        log.warning("Could not write %s: %s", home / INSTANCE_FILE, exc)
    log.info("LowRAM AI %s is running at %s", __version__, backend.url)
    return backend


class Starter:
    """Starts (or finds) the backend exactly once; safe to call from the window thread."""

    def __init__(self, ports: Sequence[int], home: Path, ready_timeout: float = READY_TIMEOUT_S):
        self.ports = list(ports)
        self.home = home
        self.ready_timeout = ready_timeout
        self.backend: Backend | None = None
        self.error: str | None = None
        self._closed = False
        self._lock = threading.Lock()

    def get(self) -> Backend:
        with self._lock:
            if self.backend is not None:
                return self.backend
            if self._closed:
                raise LauncherError("LowRAM AI is shutting down.")
            try:
                self.backend = start_or_reuse(self.ports, self.home, self.ready_timeout)
            except Exception as exc:
                self.error = describe(exc)
                raise
            return self.backend

    def close(self) -> None:
        """Stop the owned server (waits for a start in progress). Idempotent."""
        with self._lock:
            self._closed = True
            backend = self.backend
        if backend is not None:
            backend.stop()


def describe(exc: BaseException) -> str:
    if isinstance(exc, LauncherError):
        return str(exc)
    if isinstance(exc, OSError):
        return f"System error: {exc.strerror or exc}"
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- logging


class QuietAccessLog(logging.Filter):
    """Keep app.log useful: drop successful GET/HEAD access lines (UI polling)."""

    _OK_READ = re.compile(r'"(?:GET|HEAD) [^"]*" [23]\d\d\b')

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "lowram.app" and record.levelno == logging.INFO and record.msg == "%s %s":
            return not self._OK_READ.search(record.getMessage())
        return True


class _LogStream:
    """Stand-in for sys.stdout/sys.stderr in a windowed app (they are None there)."""

    def __init__(self, level: int):
        self.level = level

    def write(self, text: str) -> int:
        for line in str(text).splitlines():
            if line.strip():
                logging.getLogger("lowram.console").log(self.level, "%s", line)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def ensure_std_streams() -> None:
    if sys.stdout is None:
        sys.stdout = _LogStream(logging.INFO)
    if sys.stderr is None:
        sys.stderr = _LogStream(logging.WARNING)


def setup_logging(home: Path) -> tuple[logging.Handler, Path]:
    """Attach the rotating ``<home>/logs/app.log`` handler to the root logger."""
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = log_dir / "app.log"
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_FILE_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    handler.addFilter(QuietAccessLog())
    logging.getLogger().addHandler(handler)
    return handler, path


def install_excepthooks() -> Callable[[], None]:
    """Log uncaught exceptions (a windowed app has no console). Returns a restore function."""
    old_sys, old_thread = sys.excepthook, threading.excepthook

    def sys_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            old_sys(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.critical("Uncaught exception in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook, threading.excepthook = sys_hook, thread_hook

    def restore() -> None:
        sys.excepthook, threading.excepthook = old_sys, old_thread

    return restore


def say(message: str, *, err: bool = False) -> None:
    """Print to the terminal when there is one (no-op in a windowed app)."""
    stream = sys.stderr if err else sys.stdout
    if stream is None or isinstance(stream, _LogStream):
        return
    try:
        print(message, file=stream, flush=True)
    except (OSError, ValueError):
        pass


def alert(message: str) -> None:
    """Native error dialog for a frozen app that has neither console nor window."""
    try:
        if sys.platform == "darwin":
            script = 'on run argv\ndisplay alert "LowRAM AI could not start" message (item 1 of argv) as critical\nend run'
            subprocess.run(["osascript", "-e", script, message], timeout=600, check=False)
        elif os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "LowRAM AI could not start", 0x10)
    except Exception:
        log.exception("Could not show the error dialog")


# --------------------------------------------------------------------------- window / browser


def _icon_data_uri() -> str:
    for path in (bundle_root() / "desktop" / "icon.svg", Path(__file__).resolve().parent / "icon.svg"):
        try:
            return "data:image/svg+xml;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
    return ""


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LowRAM AI</title>
<style>
:root {{ color-scheme: light dark; --bg: #f5f5f4; --text: #1c1c1e; --muted: #6b6b70; --track: #e2e2e0; --bar: #6b6b70; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #1b1b1d; --text: #ececee; --muted: #9a9aa0; --track: #2e2e31; --bar: #b4b4ba; }}
}}
html, body {{ height: 100%; margin: 0; }}
body {{ display: flex; align-items: center; justify-content: center; background: var(--bg); color: var(--text);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  -webkit-user-select: none; user-select: none; cursor: default; }}
main {{ text-align: center; max-width: 34rem; padding: 2rem; }}
img {{ width: 104px; height: 104px; }}
h1 {{ font-size: 1.45rem; font-weight: 600; letter-spacing: -0.01em; margin: 1rem 0 0.3rem; }}
p {{ margin: 0.3rem 0; color: var(--muted); }}
.detail {{ color: var(--text); -webkit-user-select: text; user-select: text; cursor: text; overflow-wrap: anywhere; }}
.bar {{ position: relative; width: 168px; height: 3px; margin: 1.6rem auto 0; border-radius: 3px;
  background: var(--track); overflow: hidden; }}
.bar::after {{ content: ""; position: absolute; top: 0; bottom: 0; left: 0; width: 38%; border-radius: 3px;
  background: var(--bar); animation: slide 1.5s ease-in-out infinite; }}
@keyframes slide {{ from {{ transform: translateX(-100%); }} to {{ transform: translateX(270%); }} }}
@media (prefers-reduced-motion: reduce) {{ .bar::after {{ animation: none; width: 100%; opacity: 0.4; }} }}
</style></head>
<body><main>{icon}{body}</main></body></html>
"""


def splash_html() -> str:
    icon = _icon_data_uri()
    return _PAGE.format(
        icon=f'<img src="{icon}" alt="">' if icon else "",
        body='<h1>LowRAM AI</h1><p>Starting your private AI studio&hellip;</p>'
        '<div class="bar" role="progressbar" aria-label="Starting"></div>',
    )


def error_html(message: str, log_path: Path | None) -> str:
    icon = _icon_data_uri()
    where = f'<p>Details are in the log file:<br><span class="detail">{escape(str(log_path))}</span></p>' if log_path else ""
    return _PAGE.format(
        icon=f'<img src="{icon}" alt="">' if icon else "",
        body=f'<h1>LowRAM AI could not start</h1><p class="detail">{escape(message)}</p>{where}'
        "<p>Close this window and open LowRAM AI again.</p>",
    )


def _call_window(func: Callable[[str], Any], arg: str) -> None:
    try:
        func(arg)
    except Exception as exc:  # e.g. the window was closed while starting
        log.info("Could not update the window: %s", exc)


class DesktopApi:
    """The only Python functions the window's JavaScript can call (``window.pywebview.api``).

    pywebview exposes every public attribute of this object, so everything
    except the two dialog methods is private. Titles are accepted for the UI's
    convenience but not shown (pywebview's dialogs have no title option).
    """

    __slots__ = ("_window", "_open", "_folder")

    def __init__(self, webview_module: Any):
        dialogs = getattr(webview_module, "FileDialog", None)
        self._open = dialogs.OPEN if dialogs is not None else webview_module.OPEN_DIALOG
        self._folder = dialogs.FOLDER if dialogs is not None else webview_module.FOLDER_DIALOG
        self._window: Any = None

    def pick_folder(self, title: str | None = None) -> str | None:
        """Native folder dialog -> absolute path or None (e.g. a models folder on an external SSD)."""
        return self._pick(self._folder, ())

    def pick_file(self, title: str | None = None, patterns: list | None = None) -> str | None:
        """Native open dialog -> absolute path or None; ``patterns`` like ``["*.gguf"]``."""
        allowed = [p for p in (patterns if isinstance(patterns, list) else []) if isinstance(p, str)
                   and _FILE_PATTERN.fullmatch(p)][:8] or ["*.gguf"]
        return self._pick(self._open, (f"Supported files ({';'.join(allowed)})",))

    def _pick(self, dialog_type: Any, file_types: tuple) -> str | None:
        if self._window is None:
            return None
        try:
            result = self._window.create_file_dialog(
                dialog_type, directory=str(Path.home()), allow_multiple=False, file_types=file_types
            )
        except Exception:
            log.exception("File dialog failed")
            return None
        path = result[0] if isinstance(result, (list, tuple)) and result else result
        return path if isinstance(path, str) and os.path.isabs(path) else None


class SignalWatcher:
    """Run ``callback(signum)`` in a thread as soon as SIGTERM/SIGHUP/SIGINT arrives.

    While a GUI loop (Cocoa, GTK, WinForms) owns the main thread, Python-level
    signal handlers may not run for a long time. The C-level wakeup fd still
    receives the signal number immediately, so this thread can shut the server
    down (stopping llama-server) and close the window. Signal masks are not
    touched, so child processes such as llama-server stay killable.
    """

    def __init__(self, callback: Callable[[int], None]):
        self._callback = callback
        self._signals = {getattr(signal, n) for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n)}
        self._previous: dict[int, Any] = {}
        self._closed = threading.Event()
        self._reader, self._writer = socket.socketpair()
        self._writer.setblocking(False)
        self._old_fd: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if threading.current_thread() is not threading.main_thread():
            return False
        try:
            self._old_fd = signal.set_wakeup_fd(self._writer.fileno(), warn_on_full_buffer=False)
        except (ValueError, OSError) as exc:
            log.info("Signal watcher unavailable: %s", exc)
            return False
        for signum in self._signals:
            with contextlib.suppress(OSError, ValueError):
                self._previous[signum] = signal.signal(signum, self._python_handler)
        self._thread = threading.Thread(target=self._run, name="lowram-signals", daemon=True)
        self._thread.start()
        return True

    @staticmethod
    def _python_handler(signum, frame):  # noqa: ARG004 - the watcher thread does the work
        pass

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                data = self._reader.recv(64)
            except OSError:
                return
            if self._closed.is_set() or not data:
                return
            for signum in data:
                if signum in self._signals:
                    log.info("Received signal %s; closing LowRAM AI", signum)
                    try:
                        self._callback(signum)
                    except Exception:
                        log.exception("Shutdown after signal %s failed", signum)
                    return

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.set_wakeup_fd(self._old_fd if self._old_fd is not None else -1)
            for signum, old in self._previous.items():
                with contextlib.suppress(OSError, ValueError, TypeError):
                    signal.signal(signum, old)
            with contextlib.suppress(OSError):
                self._writer.send(b"\0")  # wake the thread
            self._thread.join(2)
        self._reader.close()
        self._writer.close()


def run_window(starter: Starter, storage_dir: Path, log_path: Path | None) -> int | None:
    """Show the native window until it is closed. None = no native web view (use the browser)."""
    try:
        import webview
    except Exception as exc:  # ImportError or a broken GUI binding
        log.info("pywebview is not available (%s); using the browser", exc)
        return None
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True  # "save image/video" in the dashboard
        webview.settings["ALLOW_FILE_URLS"] = False
    except Exception:
        log.debug("Could not change pywebview settings", exc_info=True)

    try:
        storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        api = DesktopApi(webview)
        window = webview.create_window(
            APP_NAME,
            html=splash_html(),
            js_api=api,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=MIN_WINDOW_SIZE,
            text_select=True,
            background_color=WINDOW_BACKGROUND,
        )
        api._window = window
    except Exception as exc:
        log.warning("Could not create the window (%s); using the browser", exc, exc_info=True)
        return None

    def boot() -> None:
        try:
            backend = starter.get()
        except Exception as exc:
            log.exception("LowRAM AI could not start")
            _call_window(window.load_html, error_html(describe(exc), log_path))
            return
        _call_window(window.load_url, backend.url)

    gui_done = threading.Event()

    def on_signal(signum: int) -> None:
        starter.close()  # stop the server and llama-server first, whatever the GUI does
        try:
            window.destroy()
        except Exception:
            log.exception("Could not close the window")
        if not gui_done.wait(10):
            log.warning("The window did not close after signal %s; exiting", signum)
            logging.shutdown()
            os._exit(EXIT_OK)

    watcher = SignalWatcher(on_signal)
    watcher.start()
    try:
        # Not private: the dashboard keeps theme and drafts in localStorage.
        webview.start(boot, private_mode=False, storage_path=str(storage_dir))
    except Exception as exc:
        log.warning("The native window is not available (%s); using the browser", exc, exc_info=True)
        return None
    finally:
        gui_done.set()
        watcher.close()
    log.info("Window closed")
    return EXIT_FAILED if starter.error else EXIT_OK


def open_in_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url, new=2)
    except Exception:
        log.exception("Could not open the browser")
        opened = False
    if not opened:
        say(f"Open {url} in your browser.")


def wait_for_stop(stop: threading.Event | None = None) -> None:
    """Block until Ctrl+C, SIGTERM, SIGHUP (terminal closed) or ``stop`` is set."""
    stop = stop or threading.Event()
    previous: dict[int, Any] = {}

    def handler(signum, frame):  # noqa: ARG001 - signal signature
        log.info("Received signal %s; stopping", signum)
        stop.set()

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous[signum] = signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        log.info("Interrupted; stopping")
    finally:
        for signum, old in previous.items():
            try:
                signal.signal(signum, old)
            except (OSError, ValueError, TypeError):
                pass


def run_browser(starter: Starter, *, open_browser: bool = True) -> int:
    backend = starter.get()
    if open_browser:
        open_in_browser(backend.url)
    if not backend.owned:
        say(f"LowRAM AI is already running: {backend.url}")
        return EXIT_OK
    say(f"LowRAM AI is running at {backend.url} - press Ctrl+C to stop.")
    wait_for_stop()
    return EXIT_OK


# --------------------------------------------------------------------------- entry point


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1024 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lowram-ai", description="Start LowRAM AI Studio.")
    parser.add_argument("--no-window", action="store_true", help="open the default browser instead of a window")
    parser.add_argument("--port", type=_port, help=f"exact port on {LOOPBACK} (default: {DEFAULT_PORT}-{LAST_PORT})")
    parser.add_argument("--headless", action="store_true",
                        help="only run the local server (no window, no browser), e.g. for smoke tests")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # parse_known_args: old macOS versions pass "-psn_0_12345" to apps started by Finder.
    args, unknown = build_parser().parse_known_args(None if argv is None else list(argv))
    ensure_std_streams()
    try:
        from backend import config

        home = config.home_dir()
        explicit = args.port
        if explicit is None and os.environ.get("LOWRAM_PORT", "").strip():
            explicit = config.port()
    except (ValueError, OSError) as exc:  # ConfigError is a ValueError
        say(f"LowRAM AI: {exc}", err=True)
        if is_frozen():
            alert(str(exc))
        return EXIT_USAGE

    handler, log_path = setup_logging(home)
    root = logging.getLogger()
    old_level = root.level
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    restore_hooks = install_excepthooks()
    starter = Starter(candidate_ports(explicit), home)
    atexit.register(starter.close)
    try:
        configure_environment()
        log.info("Launcher started (frozen=%s, python %s, %s)", is_frozen(), sys.version.split()[0], sys.platform)
        if unknown:
            log.info("Ignoring unknown arguments: %s", unknown)
        say(f"Starting LowRAM AI (log: {log_path})")
        if args.headless:
            return run_browser(starter, open_browser=False)
        if not args.no_window:
            result = run_window(starter, home / "webview", log_path)
            if result is not None:
                return result
        return run_browser(starter)
    except Exception as exc:
        if isinstance(exc, LauncherError):
            log.error("LowRAM AI could not start: %s", exc)
        else:
            log.exception("LowRAM AI could not start")
        message = describe(exc)
        say(f"LowRAM AI could not start: {message}\nDetails: {log_path}", err=True)
        if is_frozen():
            alert(f"{message}\n\nDetails: {log_path}")
        return EXIT_FAILED
    finally:
        starter.close()
        atexit.unregister(starter.close)
        restore_hooks()
        root.removeHandler(handler)
        handler.close()
        root.setLevel(old_level)


if __name__ == "__main__":
    raise SystemExit(main())
