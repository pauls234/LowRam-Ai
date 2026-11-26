"""Text recognition (OCR) on this computer: scanned PDFs and photos of documents become text.

* **On the Mac, built in** – Apple's Vision framework (the on-device text recognition the Photos
  app uses) through pyobjc: no download, nothing leaves the computer; German, English, French,
  Italian, Spanish and Portuguese. Elsewhere the page says that it is only available on the Mac.
* **Safe** – the file is read in a separate process (CoreGraphics/ImageIO parse an untrusted file)
  with lower priority, a time limit per page and a memory limit (stopped above 2.5 GB); PDF pages
  are drawn one at a time in grey at ≤ 200 dpi and ≤ 2400 px (a few MB each); at most 300 pages;
  images of at most 100 megapixels, decoded at most 4096 px large.
* **Progress** – "Seite 3 von 12 · noch 10 Seiten · 25 % · noch etwa 30 s": the time left comes from
  the seconds per page measured in this job (it corrects itself with every page), before the first
  page from earlier jobs on this computer.
* **Gentle** – one job at a time; a scan of more than :data:`LONG_PAGES` pages waits for battery,
  heat and the global inference slot like other long jobs and keeps the Mac awake.
"""

from __future__ import annotations

import collections
import importlib.util
import logging
import multiprocessing
import re
import secrets
import statistics
import sys
import threading
import time
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import psutil

from . import db, power, progress
from .web import HTTPError, Request, Response, on_shutdown, route

log = logging.getLogger("lowram.ocr")

LANGUAGES = ["de-DE", "en-US", "fr-FR", "it-IT", "es-ES", "pt-BR"]
MAX_PAGES = 300
MAX_MEGAPIXELS = 100
IMAGE_MAX_PX = 4096  # photos are decoded at most this large (a phone photo is about 4000 px): plenty for text
RENDER_DPI = 200
RENDER_MAX_PX = 2400
PAGE_TIMEOUT_S = 120.0
CHILD_MAX_BYTES = 2560 * 1024 * 1024  # a crafted file must not fill the RAM: the process is stopped above this
MIB = 1024 * 1024
DEFAULT_PAGE_S = 3.0  # rough seconds per page before the first one here is measured (Apple Silicon)
LONG_PAGES = 10  # longer scans wait for the inference slot, battery and heat like a long job
RAM_NEEDED_BYTES = 700 * 1024 * 1024
RAM_WAIT_SHORT_S = 60.0
RAM_WAIT_LONG_S = 600.0
WAIT_STEP_S = 5.0
KEEP_JOBS = 20
RESULT_TTL_S = 3600.0


class OcrError(RuntimeError):
    """Text recognition failed; the message is for the user."""


class _Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- availability

_available: bool | None = None


def available() -> bool:
    """macOS with Vision and Quartz (pyobjc): the app ships both; from source: pyobjc-framework-Vision."""
    global _available
    if _available is None:
        try:
            _available = sys.platform == "darwin" and all(
                importlib.util.find_spec(name) is not None for name in ("Vision", "Quartz"))
        except (ImportError, ValueError):
            _available = False
    return _available


def unavailable_reason() -> str | None:
    if available():
        return None
    if sys.platform != "darwin":
        return "Fotos und eingescannte Seiten kann LowRAM AI bisher nur auf dem Mac lesen (Texterkennung)."
    return "Für die Texterkennung fehlt pyobjc-framework-Vision (pip install pyobjc-framework-Vision)."


def status() -> dict:
    return {"available": available(), "reason": unavailable_reason()}


# --------------------------------------------------------------------------- the child process

def layout(lines: list[tuple[float, float, float, str]]) -> str:
    """Recognised lines ``(top, left, height, text)`` – Vision's coordinates, 0…1 from the bottom – as
    text in reading order, with an empty line where the gap is larger than a line (a new paragraph)."""
    rows = sorted(lines, key=lambda line: (-round(line[0], 2), line[1]))
    out: list[str] = []
    previous: tuple[float, float] | None = None
    for top, _left, height, text in rows:
        if previous is not None and previous[0] - previous[1] - top > max(height, previous[1]):
            out.append("")
        out.append(text)
        previous = (top, height)
    return "\n".join(out)


def _recognise(vision: Any, handler: Any) -> str:
    request = vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    try:
        request.setRecognitionLanguages_(LANGUAGES)
    except Exception:  # noqa: BLE001 - an older macOS: its default languages
        pass
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        detail = error.localizedDescription() if error is not None else "unbekannt"
        raise OcrError(f"Die Texterkennung ist fehlgeschlagen ({detail}).")
    lines = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        text = str(candidates[0].string()).strip()
        if text:
            box = observation.boundingBox()
            lines.append((box.origin.y + box.size.height, box.origin.x, box.size.height, text))
    return layout(lines)


def _render_page(quartz: Any, page: Any) -> Any:
    """One PDF page as a grey image (≤ 200 dpi, ≤ 2400 px), the page's rotation applied."""
    box = quartz.CGPDFPageGetBoxRect(page, quartz.kCGPDFCropBox)
    width, height = float(box.size.width), float(box.size.height)
    if quartz.CGPDFPageGetRotationAngle(page) % 180 == 90:
        width, height = height, width
    scale = min(RENDER_DPI / 72.0, RENDER_MAX_PX / max(width, height, 1.0))
    pixels_w, pixels_h = max(1, int(width * scale)), max(1, int(height * scale))
    gray = quartz.CGColorSpaceCreateDeviceGray()
    context = quartz.CGBitmapContextCreate(None, pixels_w, pixels_h, 8, 0, gray, quartz.kCGImageAlphaNone)
    if context is None:
        raise OcrError("Eine Seite ließ sich nicht zeichnen (zu groß?).")
    quartz.CGContextSetGrayFillColor(context, 1.0, 1.0)
    quartz.CGContextFillRect(context, quartz.CGRectMake(0, 0, pixels_w, pixels_h))
    quartz.CGContextScaleCTM(context, scale, scale)
    quartz.CGContextConcatCTM(context, quartz.CGPDFPageGetDrawingTransform(
        page, quartz.kCGPDFCropBox, quartz.CGRectMake(0, 0, width, height), 0, True))
    quartz.CGContextDrawPDFPage(context, page)
    return quartz.CGBitmapContextCreateImage(context)


def _child(source: str, kind: str, pages: list[int] | None, conn: Any) -> None:
    """Runs in its own process: draw and recognise page by page, never more than one page in memory."""
    try:
        import Quartz  # noqa: N813 - the framework's name
        import Vision
        from Foundation import NSURL
    except Exception as exc:  # noqa: BLE001 - reported to the user
        conn.send(("error", f"Die Texterkennung ist auf diesem Computer nicht verfügbar ({exc})."))
        conn.close()
        return
    try:
        url = NSURL.fileURLWithPath_(source)
        if kind == "pdf":
            document = Quartz.CGPDFDocumentCreateWithURL(url)
            if document is None:
                raise OcrError("Das PDF ließ sich für die Texterkennung nicht öffnen.")
            if not Quartz.CGPDFDocumentIsUnlocked(document):
                raise OcrError("Das PDF ist mit einem Passwort geschützt – entferne den Schutz und lade es erneut hoch.")
            count = int(Quartz.CGPDFDocumentGetNumberOfPages(document))
            wanted = [n for n in (pages or range(1, count + 1)) if 1 <= n <= count]
            if len(wanted) > MAX_PAGES:
                raise OcrError(f"Das PDF hat mehr als {MAX_PAGES} eingescannte Seiten – teile es bitte auf.")
            conn.send(("pages", len(wanted)))
            for number in wanted:
                image = _render_page(Quartz, Quartz.CGPDFDocumentGetPage(document, number))
                handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
                conn.send(("page", number, _recognise(Vision, handler)))
                del image, handler
        else:
            image_source = Quartz.CGImageSourceCreateWithURL(url, None)
            if image_source is None or Quartz.CGImageSourceGetCount(image_source) < 1:
                raise OcrError("Das Bild ließ sich nicht öffnen (beschädigt?).")
            props = dict(Quartz.CGImageSourceCopyPropertiesAtIndex(image_source, 0, None) or {})
            width = int(props.get(Quartz.kCGImagePropertyPixelWidth) or 0)
            height = int(props.get(Quartz.kCGImagePropertyPixelHeight) or 0)
            if width * height > MAX_MEGAPIXELS * 1_000_000:
                raise OcrError(f"Das Bild ist größer als {MAX_MEGAPIXELS} Megapixel – verkleinere es bitte.")
            # decoded at most IMAGE_MAX_PX large, turned upright (the photo's orientation applied)
            image = Quartz.CGImageSourceCreateThumbnailAtIndex(image_source, 0, {
                Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
                Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
                Quartz.kCGImageSourceThumbnailMaxPixelSize: IMAGE_MAX_PX,
            })
            if image is None:
                raise OcrError("Das Bild ließ sich nicht öffnen (beschädigt?).")
            conn.send(("pages", 1))
            handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
            conn.send(("page", 1, _recognise(Vision, handler)))
        conn.send(("done",))
    except OcrError as exc:
        conn.send(("error", str(exc)))
    except Exception as exc:  # noqa: BLE001 - a damaged file is the user's information
        conn.send(("error", f"Die Texterkennung ist fehlgeschlagen ({type(exc).__name__}: {exc})"[:300]))
    finally:
        conn.close()


def _lower_priority(pid: int) -> None:
    try:
        from .engine import lower_priority

        lower_priority(pid, 10)  # never raises
    except ImportError:
        pass


def _messages(source: Path, kind: str, pages: list[int] | None, cancel: threading.Event) -> Iterator[tuple]:
    """The child's messages: ``("pages", n)``, ``("page", number, text)``, ``("done",)`` or ``("error", text)``."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(str(source), kind, pages, sender), daemon=True, name="lowram-ocr")
    process.start()
    sender.close()
    watched = None
    if process.pid:
        _lower_priority(process.pid)
        try:
            watched = psutil.Process(process.pid)
        except psutil.Error:
            pass
    peak = 0
    try:
        last = time.monotonic()
        while True:
            if cancel.is_set():
                raise _Cancelled
            if watched is not None:  # a damaged or crafted file must not fill the RAM
                try:
                    peak = max(peak, int(watched.memory_info().rss))
                except psutil.Error:
                    pass
                if peak > CHILD_MAX_BYTES:
                    raise OcrError("Die Datei braucht für die Texterkennung zu viel Arbeitsspeicher – ist sie beschädigt? "
                                   "Verkleinere die Bilder oder teile die Datei auf.")
            if receiver.poll(0.5):
                try:
                    message = receiver.recv()
                except (EOFError, OSError) as exc:
                    raise OcrError("Die Texterkennung wurde unerwartet beendet.") from exc
                last = time.monotonic()
                yield message
                if message[0] in ("done", "error"):
                    return
            elif time.monotonic() - last > PAGE_TIMEOUT_S:
                raise OcrError("Eine Seite braucht für die Texterkennung zu lange – ist die Datei beschädigt?")
            elif not process.is_alive() and not receiver.poll(0):
                raise OcrError("Die Texterkennung wurde unerwartet beendet.")
    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
        process.join(5)
        if peak:
            log.info("Text recognition process: at most %d MB of memory", peak // MIB)


# --------------------------------------------------------------------------- jobs

class _Job:
    def __init__(self, job_id: str, upload_id: str, name: str, source: Path, kind: str, pages: list[int] | None) -> None:
        self.id = job_id
        self.upload_id = upload_id
        self.name = name
        self.source = source
        self.kind = kind
        self.pages = pages
        self.total: int | None = len(pages) if pages else (1 if kind == "image" else None)
        self.done = 0
        self.status = "queued"
        self.message: str | None = None
        self.error: str | None = None
        self.durations: list[float] = []
        self.measured = progress.typical("ocr_page", "vision")  # seconds per page measured here before
        self.prior = self.measured or DEFAULT_PAGE_S
        self.page_started: float | None = None
        self.finished: float | None = None
        self.upload: dict | None = None
        self.cancel = threading.Event()

    @property
    def active(self) -> bool:
        return self.status in ("queued", "waiting", "running")

    def view(self) -> dict:
        percent = eta = None
        basis = "this_job" if self.durations else ("history" if self.measured else "rough")
        if self.status == "done":
            percent = 100
        elif self.total:
            per_page = statistics.median(self.durations) if self.durations else self.prior
            fraction = 0.0
            if self.status == "running" and per_page and self.page_started is not None:
                step = progress.estimate(time.monotonic() - self.page_started, per_page)
                fraction = (step["percent"] or 0) / 100
                current_left = step["eta_s"] if step["eta_s"] is not None else per_page * 0.15
                eta = round(current_left + max(0, self.total - self.done - 1) * per_page, 1)
            percent = min(99, int(100 * (self.done + fraction) / self.total))
        page = min(self.done + 1, self.total) if self.total and self.status == "running" else None
        return {"id": self.id, "upload_id": self.upload_id, "name": self.name, "status": self.status,
                "page": page, "pages": self.total, "done": self.done, "percent": percent, "eta_s": eta,
                "basis": basis, "message": self.message, "error": self.error,
                "upload": self.upload if self.status == "done" else None}


class OcrWorker:
    """One recognition at a time; jobs live in memory only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: collections.OrderedDict[str, _Job] = collections.OrderedDict()
        self._queue: collections.deque[_Job] = collections.deque()
        self._running = False
        self._stopped = False

    def submit(self, job: _Job) -> dict:
        with self._lock:
            if self._stopped:
                raise HTTPError(HTTPStatus.SERVICE_UNAVAILABLE, "LowRAM AI wird gerade beendet.")
            self._prune_locked()
            for other in self._jobs.values():  # the same file twice: the running job
                if other.active and other.upload_id == job.upload_id:
                    return other.view()
            if self._running or self._queue:
                job.message = "Wartet, bis die vorherige Texterkennung fertig ist."
            self._jobs[job.id] = job
            self._queue.append(job)
            if not self._running:
                self._running = True
                threading.Thread(target=self._loop, name="lowram-ocr", daemon=True).start()
            return job.view()

    def _prune_locked(self) -> None:
        now = time.time()
        for job_id, job in list(self._jobs.items()):
            if job.finished and now - job.finished > RESULT_TTL_S:
                del self._jobs[job_id]
        finished = [job_id for job_id, job in self._jobs.items() if not job.active]
        for job_id in finished[: max(0, len(self._jobs) - KEEP_JOBS + 1)]:
            del self._jobs[job_id]

    def get(self, job_id: str) -> dict:
        with self._lock:
            self._prune_locked()
            job = self._jobs.get(job_id)
        if job is None:
            raise db.NotFound("Diese Texterkennung gibt es nicht (mehr).")
        return job.view()

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise db.NotFound("Diese Texterkennung gibt es nicht (mehr).")
            job.cancel.set()
            if job in self._queue:
                self._queue.remove(job)
                job.status, job.message, job.finished = "cancelled", None, time.time()
        return job.view()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._queue.clear()
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._queue or self._stopped:
                    self._running = False
                    return
                job = self._queue.popleft()
            try:
                self._run(job)
            finally:
                job.finished = time.time()

    def _wait(self, job: _Job, message: str, seconds: float = WAIT_STEP_S) -> None:
        job.status, job.message = "waiting", message
        if job.cancel.wait(seconds):
            raise _Cancelled

    def _run(self, job: _Job) -> None:
        from . import doctext, documents  # the upload this job belongs to

        try:
            if job.cancel.is_set():
                raise _Cancelled
            long_scan = (job.total or 1) > LONG_PAGES
            self._wait_for_ram(job, RAM_WAIT_LONG_S if long_scan else RAM_WAIT_SHORT_S)
            if long_scan:
                while (reason := power.battery_block_reason() or power.heat_block_reason()):
                    self._wait(job, reason, 20.0)
                while True:
                    try:
                        with db.inference_slot(f"Texterkennung „{job.name}“"), power.keep_awake("Texterkennung"):
                            texts = self._recognise(job)
                        break
                    except db.Busy as exc:
                        self._wait(job, f"Wartet, bis die andere Arbeit fertig ist ({exc}).")
            else:
                texts = self._recognise(job)
            job.upload = documents.finish_ocr(job.upload_id, texts)
            job.status, job.message = "done", None
        except _Cancelled:
            job.status, job.message = "cancelled", None
        except (OcrError, doctext.DocumentError, db.NotFound) as exc:
            job.status, job.error, job.message = "failed", str(exc), None
        except Exception as exc:  # reported to the user, never swallowed
            log.exception("Text recognition %s failed", job.id)
            job.status, job.error, job.message = "failed", f"Die Texterkennung ist fehlgeschlagen: {exc}", None

    def _wait_for_ram(self, job: _Job, patience_s: float) -> None:
        deadline = time.monotonic() + patience_s
        while int(psutil.virtual_memory().available) < RAM_NEEDED_BYTES:
            if time.monotonic() > deadline:
                raise OcrError("Es ist nicht genug Arbeitsspeicher frei für die Texterkennung – schließe andere "
                               "Programme und versuche es erneut.")
            self._wait(job, "Wartet auf freien Arbeitsspeicher.")

    def _recognise(self, job: _Job) -> dict[int, str]:
        texts: dict[int, str] = {}
        job.status, job.message = "running", None
        job.page_started = time.monotonic()
        for message in _messages(job.source, job.kind, job.pages, job.cancel):
            if message[0] == "pages":
                job.total = max(1, int(message[1]))
            elif message[0] == "page":
                now = time.monotonic()
                job.durations.append(now - job.page_started)
                job.page_started = now
                texts[int(message[1])] = str(message[2])
                job.done += 1
            elif message[0] == "error":
                raise OcrError(str(message[1]))
        if job.durations:
            progress.remember("ocr_page", "vision", statistics.median(job.durations))
        return texts


WORKER = OcrWorker()


@on_shutdown
def _stop_worker(server: Any) -> None:
    WORKER.stop()


# --------------------------------------------------------------------------- routes

_JOB_ID = re.compile(r"[0-9a-f]{12}")


def _job_id(raw: str) -> str:
    if not _JOB_ID.fullmatch(str(raw)):
        raise db.NotFound("Diese Texterkennung gibt es nicht.")
    return str(raw)


@route("GET", r"/api/ocr")
def get_status(req: Request) -> Response:
    return Response(status())


@route("POST", r"/api/ocr/uploads/([0-9a-f]{16})")
def post_start(req: Request, upload_id: str) -> Response:
    """Recognise the text of an uploaded photo or scanned PDF (the original is kept until then)."""
    req.json()
    if not available():
        raise HTTPError(HTTPStatus.CONFLICT, unavailable_reason())
    from . import documents

    meta, source, pages = documents.ocr_source(str(upload_id))
    job = _Job(secrets.token_hex(6), str(upload_id), meta["name"], source, meta["kind"], pages)
    return Response({"job": WORKER.submit(job)}, HTTPStatus.ACCEPTED)


@route("GET", r"/api/ocr/jobs/([0-9a-f]+)")
def get_job(req: Request, job_id: str) -> Response:
    return Response({"job": WORKER.get(_job_id(job_id))})


@route("POST", r"/api/ocr/jobs/([0-9a-f]+)/cancel")
def post_cancel(req: Request, job_id: str) -> Response:
    req.json()
    return Response({"job": WORKER.cancel(_job_id(job_id))})
