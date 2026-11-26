#!/usr/bin/env python3
"""Checks the frozen app (``--headless``) on the CI runner: documents, web pages, skills, text recognition.

Run against the built app: ``python desktop/feature_smoke.py http://127.0.0.1:8799``. It proves
what unit tests cannot: that the bundle contains pypdf and can start the separate PDF reading
process, that a real HTTPS page can be read through the checked connections (certificates in the
bundle), that skills work and – on the Mac – that photos and scanned PDFs become text with Apple's
Vision in a separate process (Windows must say that this only works on the Mac). A live web search is only reported (search engines sometimes block
data-center addresses) – everything else must pass.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import doc_samples  # noqa: E402 - sample documents built in memory


def log(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    log(f"FAILED: {message}")
    sys.exit(1)


class App:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        page = urllib.request.urlopen(self.base + "/", timeout=30).read().decode("utf-8")
        match = re.search(r'name="lowram-token" content="([^"]+)"', page)
        if not match:
            fail("the served page has no CSRF token")
        self.token = match.group(1)

    def call(self, method: str, path: str, body: dict | None = None, *, raw: bytes | None = None,
             name: str | None = None, timeout: float = 120) -> tuple[int, dict]:
        data = raw if raw is not None else (json.dumps(body or {}).encode() if method == "POST" else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if method == "POST":
            request.add_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
            request.add_header("X-LowRAM-Token", self.token)
        if name:
            request.add_header("X-Filename", quote(name))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


def recognise(app: App, name: str, data: bytes, pages: list[int]) -> dict:
    """Upload a photo or scan, let the app recognise its text and return the finished upload."""
    status, answer = app.call("POST", "/api/documents/uploads", raw=data, name=name)
    upload = answer.get("upload") or {}
    if status != 201 or not upload.get("needs_ocr") or upload.get("ocr_pages") != pages:
        fail(f"{name}: should wait for text recognition of pages {pages}: {status} {answer}")
    status, answer = app.call("POST", f"/api/ocr/uploads/{upload['id']}")
    if status != 202:
        fail(f"{name}: text recognition did not start: {status} {answer}")
    job, started, seen = answer["job"], time.monotonic(), []
    while job["status"] not in ("done", "failed", "cancelled"):
        if time.monotonic() - started > 300:
            fail(f"{name}: text recognition takes too long: {job}")
        time.sleep(0.5)
        job = app.call("GET", f"/api/ocr/jobs/{job['id']}")[1]["job"]
        line = f"{job['status']}: page {job['page']} of {job['pages']}, {job['done']} done, {job['percent']} %, eta {job['eta_s']} s"
        if not seen or seen[-1] != line:
            seen.append(line)
    for line in seen:
        log(f"   {line}")
    if job["status"] != "done":
        fail(f"{name}: {job}")
    status, done = app.call("GET", f"/api/documents/uploads/{upload['id']}")
    if status != 200 or done.get("needs_ocr") or not done.get("ocr"):
        fail(f"{name}: the recognised text is missing: {status} {done}")
    log(f"   ok in {time.monotonic() - started:.1f} s: {done['text'][:200]!r}")
    return done


def check_text_recognition(app: App) -> None:
    status, info = app.call("GET", "/api/ocr")
    if status != 200:
        fail(f"/api/ocr: {status} {info}")
    if sys.platform != "darwin":
        status, answer = app.call("POST", "/api/documents/uploads", raw=b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\0" * 64,
                                  name="foto.jpg")
        if info.get("available") or status != 422 or "nur auf dem Mac" not in answer.get("error", ""):
            fail(f"a photo must be refused with a reason outside the Mac: {info} {status} {answer}")
        log("   photos are refused with a reason (only on the Mac): ok")
        return
    if not info.get("available"):
        fail(f"text recognition is not available in the Mac app: {info}")
    from tests import ocr_samples  # AppKit and Quartz (pyobjc) draw the test pictures

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        photo = ocr_samples.text_png(folder / "brief.png", ["Rechnung Nummer 4711", "Betrag 99 Euro"])
        text = recognise(app, "brief.png", photo.read_bytes(), [1])["text"]
        if "4711" not in text or "Rechnung" not in text:
            fail(f"photo: {text!r}")
        pages = [ocr_samples.text_png(folder / f"seite{n}.png", [f"Vertrag Seite {word}", "LowRAM AI liest Scans"])
                 for n, word in ((1, "eins"), (2, "zwei"))]
        scan = ocr_samples.scan_pdf(folder / "scan.pdf", pages)
        text = recognise(app, "scan.pdf", scan.read_bytes(), [1, 2])["text"]
        if "[Seite 2]" not in text or "zwei" not in text or "eins" not in text:
            fail(f"scanned PDF: {text!r}")


def main() -> None:
    app = App(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799")

    log("1. PDF (read in a separate process inside the app)")
    started = time.monotonic()
    status, data = app.call("POST", "/api/documents/uploads?text=1", raw=doc_samples.pdf(
        ["Erste Seite des Testdokuments mit genug Text.", "Zweite Seite: LowRAM AI liest PDFs."]), name="test.pdf")
    if status != 201 or "Zweite Seite: LowRAM AI liest PDFs." not in data["upload"]["text"]:
        fail(f"PDF upload: {status} {data}")
    log(f"   ok in {time.monotonic() - started:.1f} s: {data['upload']['pages']} pages, {data['upload']['chars']} characters")

    log("2. Word document")
    status, data = app.call("POST", "/api/documents/uploads?text=1", raw=doc_samples.docx(
        [("Heading1", "Bericht"), ("", "Inhalt aus Word.")]), name="bericht.docx")
    if status != 201 or data["upload"]["text"] != "# Bericht\n\nInhalt aus Word.":
        fail(f"Word upload: {status} {data}")
    log("   ok")

    log("3. A real web page as a document (HTTPS, checked connection)")
    for attempt in range(3):
        status, data = app.call("POST", "/api/documents/uploads/url", {"url": "https://example.com/"})
        if status == 201:
            break
        log(f"   attempt {attempt + 1}: {status} {data.get('error')}")
        time.sleep(5)
    upload = (data.get("upload") or {}) if status == 201 else {}
    # the title becomes the document name; the body text of example.com changes now and then
    if status != 201 or "Example Domain" not in upload.get("name", "") or upload.get("chars", 0) < 50:
        fail(f"web page: {status} {data}")
    log(f"   ok: {upload['name']!r}, {upload['chars']} characters: {upload['preview'][:160]!r}")
    status, data = app.call("POST", "/api/documents/uploads/url", {"url": "http://127.0.0.1/"})
    if status != 422 or "eigenen Netz" not in data.get("error", ""):
        fail(f"a local address must be refused: {status} {data}")
    log("   a local address is refused: ok")

    log("4. Skills")
    status, data = app.call("POST", "/api/skills", {"title": "Smoke", "instructions": "Antworte kurz."})
    if status != 201 or not data["skill"]["usable"]:
        fail(f"skill: {status} {data}")
    log("   ok")

    log("5. Text recognition (photos and scanned PDFs)")
    check_text_recognition(app)

    log("6. Live web search and the raw page markup (only reported)")
    try:
        from backend import doctext, internet

        markup = internet.request("https://example.com/")["data"].decode("utf-8", "replace")
        log(f"   example.com markup: {markup[:1500]!r}")
        log(f"   read as: {doctext.html_to_text(markup)[0][:400]!r}")
        results = internet.search("Python Programmiersprache")
        log(f"   {len(results)} results, first: {results[0]['url'] if results else '-'}")
    except Exception as exc:  # noqa: BLE001 - a search engine may block CI addresses
        log(f"   note: {exc}")
    log("OK – documents, web pages, skills and text recognition work in the built app")


if __name__ == "__main__":
    main()
