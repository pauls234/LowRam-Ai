"""Optional menu-bar icon (macOS) for the desktop app: open the window, status, quit.

Everything here is best effort: a missing or broken GUI binding, an unsupported
platform or any error while building the icon only logs and returns ``None`` -
the app then works exactly as without a tray.

macOS: an ``NSStatusItem`` built with pyobjc (installed with pywebview). It is
created on the main thread through ``PyObjCTools.AppHelper.callAfter`` while
pywebview's Cocoa loop runs, so it coexists with the window. Windows/Linux:
no tray (it would need an extra dependency such as pystray).

The status line is read in-process (engine state, running task/job) every few
seconds by a background thread; it never changes anything.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Callable

log = logging.getLogger("lowram.desktop.tray")

OPEN_LABEL = "LowRAM AI öffnen"
QUIT_LABEL = "Beenden"
REFRESH_S = 3.0
_ENGINE_STATES = {"ready": "bereit", "starting": "startet …", "stopped": "aus", "error": "Fehler"}


def status_line(server: Any = None, modules: dict | None = None) -> str:
    """One German line such as ``Modell: bereit · Aufgabe läuft`` (read-only, never raises)."""
    modules = sys.modules if modules is None else modules
    parts: list[str] = []
    engine = modules.get("backend.engine")
    try:
        if engine is not None and engine.external():
            parts.append("Modell: externer Server")
        elif engine is not None:
            info = engine._manager.snapshot()
            state = _ENGINE_STATES.get(info.get("state"), str(info.get("state") or "unbekannt"))
            model = info.get("model_id")
            parts.append(f"Modell: {state}" + (f" ({model})" if model and info.get("state") != "stopped" else ""))
    except Exception:
        log.debug("Engine status unavailable", exc_info=True)
    jobs: list[str] = []
    try:
        if server is not None and server.loop.status().get("active"):
            jobs.append("Aufgabe läuft")
    except Exception:
        log.debug("Loop status unavailable", exc_info=True)
    media = modules.get("backend.media")
    try:
        if media is not None and media.WORKER.current_job_id() is not None:
            jobs.append("Bild/Video wird erstellt")
    except Exception:
        log.debug("Media status unavailable", exc_info=True)
    parts.append(" · ".join(jobs) if jobs else "Keine laufende Aufgabe")
    return " · ".join(parts)


class Tray:
    """Platform-independent part: menu callbacks and the status refresher thread."""

    def __init__(self, on_open: Callable[[], None], on_quit: Callable[[], None],
                 status: Callable[[], str], refresh_s: float = REFRESH_S):
        self.on_open = on_open
        self.on_quit = on_quit
        self.status = status
        self.refresh_s = refresh_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_status = ""

    # backends override these two
    def set_status(self, text: str) -> None:
        self.last_status = text

    def _remove(self) -> None:
        pass

    def open(self) -> None:
        try:
            self.on_open()
        except Exception:
            log.exception("Could not open the window")

    def quit(self) -> None:
        # The shutdown waits for the server and llama-server: never on the GUI thread.
        threading.Thread(target=self._quit, name="lowram-tray-quit", daemon=True).start()

    def _quit(self) -> None:
        try:
            self.on_quit()
        except Exception:
            log.exception("Quit from the menu bar failed")

    def refresh(self) -> str:
        try:
            text = self.status()
        except Exception:
            log.debug("Status line failed", exc_info=True)
            text = "Status nicht verfügbar"
        self.set_status(text)
        return text

    def start_refresher(self) -> None:
        def run() -> None:
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(self.refresh_s)

        self._thread = threading.Thread(target=run, name="lowram-tray-status", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop refreshing and remove the icon (idempotent, never raises)."""
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self._remove()
        except Exception:
            log.debug("Could not remove the tray icon", exc_info=True)


# --------------------------------------------------------------------------- macOS

_TARGET_CLASS: Any = None


def _target_class() -> Any:
    """The Objective-C menu target class (an ObjC class can be defined only once per process)."""
    global _TARGET_CLASS
    if _TARGET_CLASS is None:
        from Foundation import NSObject

        class LowRAMTrayTarget(NSObject):
            def openWindow_(self, sender):  # noqa: N802 - Objective-C selector
                self.tray.open()

            def quitApp_(self, sender):  # noqa: N802 - Objective-C selector
                self.tray.quit()

        _TARGET_CLASS = LowRAMTrayTarget
    return _TARGET_CLASS


class MacTray(Tray):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        from PyObjCTools import AppHelper

        self._call_after = AppHelper.callAfter
        self._item: Any = None
        self._status_item: Any = None
        self._target: Any = None

    def build(self) -> None:
        """Create the status item; must run on the main thread."""
        import AppKit

        bar = AppKit.NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        item.button().setTitle_("LowRAM")
        item.button().setToolTip_("LowRAM AI")
        target = _target_class().alloc().init()
        target.tray = self
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        opener = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(OPEN_LABEL, "openWindow:", "")
        opener.setTarget_(target)
        menu.addItem_(opener)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        status = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Status wird geladen …", None, "")
        status.setEnabled_(False)
        menu.addItem_(status)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        quitter = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(QUIT_LABEL, "quitApp:", "q")
        quitter.setTarget_(target)
        menu.addItem_(quitter)
        item.setMenu_(menu)
        self._status_item, self._item, self._target = item, status, target

    def open(self) -> None:
        try:
            import AppKit

            AppKit.NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            log.debug("Could not activate the app", exc_info=True)
        super().open()

    def set_status(self, text: str) -> None:
        super().set_status(text)
        if self._item is not None:
            self._call_after(self._item.setTitle_, text)

    def _remove(self) -> None:
        item = self._status_item
        if item is None:
            return
        self._status_item = None

        def remove() -> None:
            import AppKit

            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(item)

        self._call_after(remove)


def _create_mac(on_open, on_quit, status, timeout: float) -> Tray | None:
    tray = MacTray(on_open, on_quit, status)
    done = threading.Event()
    errors: list[BaseException] = []

    def build() -> None:
        try:
            tray.build()
        except BaseException as exc:  # noqa: BLE001 - reported below, never raised into Cocoa
            errors.append(exc)
        finally:
            done.set()

    tray._call_after(build)
    if not done.wait(timeout):
        log.info("The menu-bar icon was not created in time; continuing without it")
        return None
    if errors:
        log.info("No menu-bar icon: %s", errors[0])
        return None
    tray.start_refresher()
    return tray


def create(on_open: Callable[[], None], on_quit: Callable[[], None], status: Callable[[], str],
           *, platform: str | None = None, timeout: float = 5.0) -> Tray | None:
    """The menu-bar icon, or ``None`` (unsupported platform, missing library, any error)."""
    platform = sys.platform if platform is None else platform
    if platform != "darwin":
        return None
    try:
        return _create_mac(on_open, on_quit, status, timeout)
    except Exception as exc:  # ImportError (no pyobjc) or a broken binding
        log.info("No menu-bar icon (%s)", exc)
        return None
