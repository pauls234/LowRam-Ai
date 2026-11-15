"""Diagnostics, a tiny model self-test and the update check.

* ``GET /api/diagnostics`` -> ``{checks: [...], report}``. Every check is
  guarded: a failing or missing component becomes a ``warn``/``fail`` entry,
  never an exception. Optional modules (engine, media, cluster) are imported
  lazily. The copyable ``report`` contains no secrets, no query strings and
  shows the home folder as ``~``.
* ``POST /api/diagnostics/selftest {}`` -> ``{ok, seconds, tokens_per_s, error}``.
* ``GET /api/update`` -> ``{current, latest, url, available, checked, error}``:
  asks the GitHub API (https, api.github.com only, 5 s timeout, cached 6 h,
  never with a token). A 404 (e.g. a private repo) is silent: ``checked: false``
  and no error.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from . import __version__, config, db, llm, settings
from .web import Request, Response, route

log = logging.getLogger("lowram.diagnostics")

GIB = 1024**3

# The one place the release repository is named.
UPDATE_REPO = "pauls234/LowRam-Ai"
UPDATE_HOST = "api.github.com"
UPDATE_URL = f"https://{UPDATE_HOST}/repos/{UPDATE_REPO}/releases/latest"
UPDATE_TIMEOUT_S = 5.0
UPDATE_CACHE_S = 6 * 3600
UPDATE_ERROR_CACHE_S = 30 * 60
RELEASE_URL_PREFIX = f"https://github.com/{UPDATE_REPO}/"
SILENT_STATUSES = (401, 403, 404)  # private repo / rate limit: nothing to tell the user

SELFTEST_PROMPT = "Antworte mit OK"
SELFTEST_MAX_TOKENS = 16
SELFTEST_TIMEOUT_S = 120.0

DISK_WARN_BYTES = 5 * GIB
DISK_FAIL_BYTES = 1 * GIB
RAM_WARN_BYTES = 2 * GIB
RAM_FAIL_BYTES = 512 * 1024**2

_SECRET_RE = re.compile(r"(?i)\b(key|token|secret|password|passwd|authorization)\b\s*[=:]\s*\S+")

# --------------------------------------------------------------------------- helpers


def _home_text() -> str:
    try:
        return str(Path.home())
    except Exception:  # noqa: BLE001 - no HOME at all
        return ""


def scrub(text: object) -> str:
    """Make text safe for the copyable report: home folder -> ``~``, no secrets, no URL queries."""
    out = str(text)
    home = _home_text()
    if home and len(home) > 1:
        out = out.replace(home, "~")
    out = re.sub(r"(https?://)[^\s/@]+@", r"\1", out)  # credentials in URLs
    out = re.sub(r"(https?://[^\s?#]+)[?#]\S*", r"\1", out)  # query strings / fragments
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", out)


def _check(cid: str, title: str, status: str, detail: str, hint: str = "") -> dict:
    return {"id": cid, "title": title, "status": status, "detail": scrub(detail), "hint": hint}


def _safe(cid: str, title: str, fn: Callable[[], dict]) -> dict:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail
        log.debug("Diagnostic %s failed", cid, exc_info=True)
        return _check(cid, title, "warn", f"Konnte nicht geprüft werden ({type(exc).__name__})",
                      "Starte die App neu und versuche es noch einmal.")


def _engine() -> Any | None:
    try:
        from . import engine  # lazy: optional feature module

        return engine
    except Exception:  # noqa: BLE001
        return None


def _gb(n: float) -> str:
    return f"{n / GIB:.1f} GB"


# --------------------------------------------------------------------------- checks


def check_app() -> dict:
    detail = (f"LowRAM AI {__version__} · Python {platform.python_version()} · "
              f"{platform.system()} {platform.release()} ({platform.machine()})")
    status, hint = "ok", ""
    if sys.version_info < (3, 10):
        status, hint = "fail", "LowRAM AI braucht Python 3.10 oder neuer."
    return _check("app", "App, Python und System", status, detail, hint)


def check_runtime() -> dict:
    title = "KI-Engine (llama.cpp)"
    engine = _engine()
    if engine is None:
        return _check("runtime", title, "fail", "Engine-Modul nicht verfügbar", "Installiere die App neu.")
    if engine.external():
        return _check("runtime", title, "ok", "Externer Server über LOWRAM_LLM_URL (eigene Engine wird nicht genutzt)")
    info = engine.runtime_info()
    if not info.get("installed"):
        return _check("runtime", title, "fail", "llama-server ist nicht installiert",
                      "Öffne „Modelle“ und installiere die Engine.")
    version = info.get("version") or "unbekannte Version"
    return _check("runtime", title, "ok", f"Installiert ({info.get('source') or 'unbekannt'}, {version})")


def check_model() -> dict:
    title = "Ausgewähltes Modell"
    engine = _engine()
    if engine is None:
        return _check("model", title, "warn", "Engine-Modul nicht verfügbar")
    if engine.external():
        return _check("model", title, "ok", "Wird vom externen Server bestimmt")
    model_id = settings.get("engine", "model_id")
    if not model_id:
        return _check("model", title, "warn", "Noch kein Modell ausgewählt", "Lade unter „Modelle“ ein Modell herunter.")
    entry = engine.find_entry(model_id)
    if entry is None:
        return _check("model", title, "fail", f"Das Modell {str(model_id)[:64]} gibt es nicht mehr",
                      "Wähle unter „Modelle“ ein anderes Modell.")
    if engine.is_available(entry):
        return _check("model", title, "ok", f"{entry.name} ist vorhanden")
    if entry.source != "catalog" or not Path(config.models_dir()).is_dir():
        return _check("model", title, "fail", f"{entry.name}: Die Datei ist nicht erreichbar",
                      "Ist die externe Festplatte mit den Modellen angeschlossen?")
    return _check("model", title, "fail", f"{entry.name} ist nicht heruntergeladen",
                  "Lade das Modell unter „Modelle“ herunter.")


def check_server() -> dict:
    title = "Modell-Server"
    engine = _engine()
    if engine is None:
        return _check("server", title, "warn", "Engine-Modul nicht verfügbar")
    if engine.external():
        return _check("server", title, "ok", f"Externer Server: {llm.redact_url(config.llm_url())}")
    server = engine.status()["server"]
    state = server.get("state")
    if state == "ready" and server.get("port"):
        code = engine._probe_health(int(server["port"]))
        if code == 200:
            return _check("server", title, "ok", f"Läuft und antwortet (Port {server['port']})")
        return _check("server", title, "fail", f"Läuft, antwortet aber nicht (Status {code})",
                      "Stoppe die Engine und starte sie neu.")
    if state == "starting":
        return _check("server", title, "warn", "Wird gerade gestartet (große Modelle brauchen etwas Zeit)")
    if state == "error":
        return _check("server", title, "fail", f"Fehler: {server.get('error') or 'unbekannt'}",
                      "Prüfe Modell und freien Arbeitsspeicher und starte die Engine neu.")
    return _check("server", title, "warn", "Gestoppt", "Starte die Engine unter „Modelle“.")


def check_ram() -> dict:
    vm = psutil.virtual_memory()
    detail = f"{_gb(vm.available)} frei von {_gb(vm.total)}"
    if vm.available < RAM_FAIL_BYTES:
        return _check("ram", "Arbeitsspeicher", "fail", detail, "Schließe andere Programme oder nutze ein kleineres Modell.")
    if vm.available < RAM_WARN_BYTES:
        return _check("ram", "Arbeitsspeicher", "warn", detail, "Wenig frei – schließe andere Programme.")
    return _check("ram", "Arbeitsspeicher", "ok", detail)


def _models_dir() -> Path:
    return Path(config.models_dir())


def check_disk() -> dict:
    title = "Freier Speicherplatz"
    folder = _models_dir()
    target = folder if folder.is_dir() else config.home_dir()
    usage = shutil.disk_usage(target)
    detail = f"{_gb(usage.free)} frei von {_gb(usage.total)}"
    if usage.free < DISK_FAIL_BYTES:
        return _check("disk", title, "fail", detail, "Mache Platz frei oder lege die Modelle auf ein externes Laufwerk.")
    if usage.free < DISK_WARN_BYTES:
        return _check("disk", title, "warn", detail, "Für weitere Modelle wird es knapp.")
    return _check("disk", title, "ok", detail)


def check_models_folder() -> dict:
    title = "Modell-Ordner"
    folder = _models_dir()
    if not folder.is_dir():
        return _check("models_folder", title, "fail", f"{folder} ist nicht erreichbar",
                      "Schließe das externe Laufwerk an oder setze den Ordner unter „Modelle“ zurück.")
    if not os.access(folder, os.W_OK | os.X_OK):
        return _check("models_folder", title, "fail", f"{folder} ist nicht beschreibbar",
                      "Wähle einen Ordner, in den du schreiben darfst.")
    return _check("models_folder", title, "ok", f"{folder} ist beschreibbar")


def check_media() -> dict:
    title = "Bild/Video-Engine"
    try:
        from . import media  # lazy: optional feature module
    except Exception:  # noqa: BLE001
        return _check("media_runtime", title, "warn", "Medien-Modul nicht verfügbar")
    info = media.find_runtime()
    if info.get("installed"):
        return _check("media_runtime", title, "ok", f"Installiert ({info.get('version') or 'unbekannte Version'})")
    return _check("media_runtime", title, "warn", "Nicht installiert (nur für Bilder und Videos nötig)",
                  "Installiere sie unter „Bilder & Video“, wenn du sie brauchst.")


def check_lan() -> dict:
    title = "Geräte im Netzwerk"
    openssl = shutil.which("openssl") is not None
    if not settings.get("cluster", "enabled"):
        detail = "Ausgeschaltet" + ("" if openssl else " · openssl fehlt")
        return _check("lan", title, "ok", detail)
    try:
        from . import cluster  # lazy: optional feature module
    except Exception:  # noqa: BLE001
        return _check("lan", title, "warn", "Geräte-Modul nicht verfügbar")
    port = cluster.listening_port()
    state = getattr(cluster, "_STATE", None)
    discovery = getattr(state, "discovery", None) is not None if state is not None else False
    discoverable = bool(settings.get("cluster", "discoverable"))
    parts = [f"Empfang auf Port {port}" if port else "Empfang nicht aktiv",
             ("Suche aktiv" if discovery else "Suche nicht aktiv") if discoverable else "Unsichtbar",
             "openssl vorhanden" if openssl else "openssl fehlt"]
    if not openssl:
        return _check("lan", title, "fail", " · ".join(parts), "Installiere openssl, damit Geräte sicher verbunden werden.")
    if not port:
        return _check("lan", title, "fail", " · ".join(parts),
                      "Der Port ist evtl. belegt – ändere ihn unter „Geräte“ oder starte die App neu.")
    if discoverable and not discovery:
        return _check("lan", title, "warn", " · ".join(parts),
                      "Der Such-Port ist evtl. belegt; verbinde Geräte mit dem Code.")
    return _check("lan", title, "ok", " · ".join(parts))


CHECKS: tuple[tuple[str, str, Callable[[], dict]], ...] = (
    ("app", "App, Python und System", check_app),
    ("runtime", "KI-Engine (llama.cpp)", check_runtime),
    ("model", "Ausgewähltes Modell", check_model),
    ("server", "Modell-Server", check_server),
    ("ram", "Arbeitsspeicher", check_ram),
    ("disk", "Freier Speicherplatz", check_disk),
    ("models_folder", "Modell-Ordner", check_models_folder),
    ("media_runtime", "Bild/Video-Engine", check_media),
    ("lan", "Geräte im Netzwerk", check_lan),
)

_LABEL = {"ok": "OK", "warn": "WARNUNG", "fail": "FEHLER"}


def run_checks() -> list[dict]:
    return [_safe(cid, title, fn) for cid, title, fn in CHECKS]


def build_report(checks: list[dict]) -> str:
    lines = [f"LowRAM AI {__version__} – Diagnose", f"Erstellt: {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for item in checks:
        lines.append(f"[{_LABEL.get(item['status'], item['status'])}] {item['title']}: {item['detail']}")
        if item.get("hint") and item["status"] != "ok":
            lines.append(f"    -> {item['hint']}")
    return scrub("\n".join(lines))


def diagnostics() -> dict:
    checks = run_checks()
    return {"checks": checks, "report": build_report(checks)}


# --------------------------------------------------------------------------- self-test


def selftest() -> dict:
    """One tiny completion against the running model. Never raises."""
    started = time.monotonic()

    def result(ok: bool, error: str | None = None, tokens: int | None = None) -> dict:
        seconds = round(time.monotonic() - started, 2)
        rate = round(tokens / seconds, 1) if ok and tokens and seconds > 0 else None
        return {"ok": ok, "seconds": seconds, "tokens_per_s": rate, "error": error}

    try:
        engine = _engine()
        if engine is not None and not engine.external():
            if engine.status()["server"].get("state") != "ready":
                return result(False, "Es läuft kein Modell. Starte zuerst die Engine unter „Modelle“.")
        with db.inference_slot("Selbsttest"):
            started = time.monotonic()
            text = llm.chat([{"role": "user", "content": SELFTEST_PROMPT}], max_tokens=SELFTEST_MAX_TOKENS,
                            temperature=0.0, timeout=SELFTEST_TIMEOUT_S)
        if not text.strip():
            return result(False, "Das Modell hat leer geantwortet.")
        tokens = max(1, round(len(text) / 4))  # rough estimate: ~4 characters per token
        return result(True, None, tokens)
    except db.Busy as exc:
        return result(False, f"Gerade beschäftigt: {exc}")
    except llm.LLMError as exc:
        return result(False, scrub(str(exc))[:300])
    except Exception as exc:  # noqa: BLE001 - the self-test reports, it never crashes
        log.warning("Self-test failed: %s", type(exc).__name__)
        return result(False, f"Selbsttest fehlgeschlagen ({type(exc).__name__})")


# --------------------------------------------------------------------------- update check

_update_lock = threading.Lock()
_update_cache: tuple[float, float, dict] | None = None  # (fetched_at, ttl, result)


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.match(r"^\s*v?(\d+(?:\.\d+){0,3})", text or "")
    return tuple(int(p) for p in match.group(1).split(".")) if match else None


def is_newer(latest: str, current: str) -> bool:
    a, b = _version_tuple(latest), _version_tuple(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _fetch_release() -> Any:
    """GET the latest release JSON (https, allow-listed host, no credentials)."""
    from . import downloads  # lazy

    parsed = urlparse(UPDATE_URL)
    if parsed.scheme != "https" or parsed.hostname != UPDATE_HOST:
        raise ValueError("update URL must be https://api.github.com")
    return downloads.fetch_json(UPDATE_URL, headers={"Accept": "application/vnd.github+json"},
                                timeout=UPDATE_TIMEOUT_S)


def _empty(error: str | None = None) -> dict:
    return {"current": __version__, "latest": None, "url": None, "available": False, "checked": False,
            "error": error}


def _check_update_now() -> tuple[dict, float]:
    try:
        data = _fetch_release()
    except Exception as exc:  # noqa: BLE001 - never raise to the UI
        code = getattr(exc, "code", None)
        if code in SILENT_STATUSES:
            return _empty(None), UPDATE_CACHE_S
        log.info("Update check failed: %s", type(exc).__name__)
        return _empty("Die Update-Prüfung ist gerade nicht möglich (keine Verbindung?)."), UPDATE_ERROR_CACHE_S
    if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
        return _empty("GitHub hat eine unerwartete Antwort geschickt."), UPDATE_ERROR_CACHE_S
    latest = data["tag_name"].strip()[:64]
    html = data.get("html_url")
    url = html if isinstance(html, str) and html.startswith(RELEASE_URL_PREFIX) and len(html) < 300 else None
    return {"current": __version__, "latest": latest, "url": url, "available": is_newer(latest, __version__),
            "checked": True, "error": None}, UPDATE_CACHE_S


def update_status(force: bool = False) -> dict:
    global _update_cache
    try:
        enabled = bool(settings.get("ui", "check_updates"))
    except Exception:  # noqa: BLE001 - settings key missing in an old build
        enabled = True
    if not enabled:
        return _empty(None)
    with _update_lock:
        now = time.monotonic()
        if not force and _update_cache is not None and now - _update_cache[0] < _update_cache[1]:
            return dict(_update_cache[2])
        result, ttl = _check_update_now()
        _update_cache = (now, ttl, result)
        return dict(result)


def _reset_for_tests() -> None:
    global _update_cache
    with _update_lock:
        _update_cache = None


# --------------------------------------------------------------------------- routes


@route("GET", r"/api/diagnostics")
def route_diagnostics(req: Request) -> Response:
    return Response(diagnostics())


@route("POST", r"/api/diagnostics/selftest")
def route_selftest(req: Request) -> Response:
    req.json()
    return Response(selftest())


@route("GET", r"/api/update")
def route_update(req: Request) -> Response:
    return Response(update_status())


__all__ = ["diagnostics", "selftest", "update_status", "UPDATE_URL"]
