"""ocr.py: photos and scanned PDFs become text – with a stand-in for Apple's Vision, and the real one on a Mac."""

import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from backend import documents, ocr, progress
from tests import doc_samples as ds
from tests import ocr_children
from tests.helpers import wait_until
from tests.test_app import AppTestCase

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
TEXT_PAGE = "Echter Text auf dieser Seite, lang genug, um als Textseite zu gelten."


def fake_messages(texts: dict, delay: float = 0.0, fail: str | None = None):
    """Stands in for the child process: page by page, as Vision would answer."""
    def messages(source, kind, pages, cancel):
        numbers = pages or [1]
        yield ("pages", len(numbers))
        for number in numbers:
            if delay and cancel.wait(delay):
                raise ocr._Cancelled
            if fail:
                yield ("error", fail)
                return
            yield ("page", number, texts.get(number, f"Text von Seite {number}"))
        yield ("done",)
    return messages


class LayoutTests(unittest.TestCase):
    def test_reading_order_and_paragraphs(self):
        lines = [(0.50, 0.1, 0.03, "zweiter Absatz"), (0.90, 0.1, 0.03, "Rechnung"), (0.86, 0.1, 0.03, "Nr. 4711"),
                 (0.90, 0.6, 0.03, "rechts oben")]
        self.assertEqual(ocr.layout(lines), "Rechnung\nrechts oben\nNr. 4711\n\nzweiter Absatz")
        self.assertEqual(ocr.layout([]), "")


class ChildProcessTests(unittest.TestCase):
    """The real separate process (with stand-ins for Vision): answers, time limit, crash, memory, cancel."""

    def collect(self, child, **limits):
        for patcher in [mock.patch.object(ocr, "_child", child)] + [mock.patch.object(ocr, k, v) for k, v in limits.items()]:
            patcher.start()
            self.addCleanup(patcher.stop)
        return list(ocr._messages(Path("egal.pdf"), "pdf", [1, 2], threading.Event()))

    def assertNoChildLeft(self):
        self.assertTrue(wait_until(lambda: not multiprocessing.active_children(), 10))

    def test_the_answers_arrive_in_order(self):
        self.assertEqual(self.collect(ocr_children.talker),
                         [("pages", 2), ("page", 1, "eins"), ("page", 2, "zwei"), ("done",)])
        self.assertNoChildLeft()

    def test_a_hanging_page_is_stopped(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ocr.OcrError, "zu lange"):
            self.collect(ocr_children.sleeper, PAGE_TIMEOUT_S=1.0)
        self.assertLess(time.monotonic() - started, 20)
        self.assertNoChildLeft()

    def test_a_crash_is_reported(self):
        with self.assertRaisesRegex(ocr.OcrError, "unerwartet beendet"):
            self.collect(ocr_children.crasher)
        self.assertNoChildLeft()

    def test_a_file_that_needs_too_much_memory_is_stopped(self):
        with self.assertRaisesRegex(ocr.OcrError, "zu viel Arbeitsspeicher"):
            self.collect(ocr_children.hog, CHILD_MAX_BYTES=150 * 1024 * 1024)
        self.assertNoChildLeft()

    def test_cancel_stops_the_process(self):
        cancel = threading.Event()
        messages = []
        with mock.patch.object(ocr, "_child", ocr_children.sleeper), self.assertRaises(ocr._Cancelled):
            for message in ocr._messages(Path("egal.pdf"), "pdf", [1], cancel):
                messages.append(message)
                cancel.set()  # the user cancels while the first page is read
        self.assertEqual(messages, [("pages", 1)])
        self.assertNoChildLeft()


class OcrApiTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.worker = ocr.OcrWorker()
        for patcher in (mock.patch.object(ocr, "WORKER", self.worker),
                        mock.patch.object(ocr, "available", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.worker.stop)
        self.addCleanup(documents.WORKER.stop)  # no document job of this test continues in the next one

    def upload(self, name, data):
        status, _, payload = self.request("POST", "/api/documents/uploads", raw=data,
                                          content_type="application/octet-stream", headers={"X-Filename": quote(name)})
        return status, json.loads(payload)

    def job(self, job_id):
        status, data = self.api("GET", f"/api/ocr/jobs/{job_id}")
        self.assertEqual(status, 200, data)
        return data["job"]

    def run_ocr(self, upload_id):
        status, data = self.api("POST", f"/api/ocr/uploads/{upload_id}", {})
        self.assertEqual(status, 202, data)
        job_id = data["job"]["id"]
        self.assertTrue(wait_until(lambda: self.job(job_id)["status"] in ("done", "failed", "cancelled"), 10))
        return self.job(job_id)

    def test_a_photo_becomes_text(self):
        status, data = self.upload("Rechnung.jpg", JPEG)
        self.assertEqual(status, 201, data)
        upload = data["upload"]
        self.assertEqual((upload["kind"], upload["kind_label"], upload["needs_ocr"], upload["ocr_pages"]),
                         ("image", "Foto/Scan", True, [1]))
        source = documents._uploads() / f"{upload['id']}.src"
        self.assertTrue(source.exists())
        job_request = {"upload_id": upload["id"], "action": "summarize"}
        self.assertEqual(self.api("POST", "/api/documents/jobs", job_request)[0], 409)  # text not recognised yet
        with mock.patch.object(ocr, "_messages", fake_messages({1: "Rechnung Nr. 4711\nBetrag: 99,00 €"}, delay=0.05)):
            job = self.run_ocr(upload["id"])
        self.assertEqual(job["status"], "done", job)
        self.assertEqual((job["percent"], job["upload"]["needs_ocr"]), (100, False))
        meta, text = documents.load_upload(upload["id"])
        self.assertEqual(text, "Rechnung Nr. 4711\nBetrag: 99,00 €")
        self.assertTrue(meta["ocr"])
        self.assertIn("Texterkennung", " ".join(meta["warnings"]))
        self.assertFalse(source.exists())  # the original goes once its text is there
        with mock.patch.object(documents.WORKER, "ensure_started"):  # allowed now – created, not processed
            self.assertEqual(self.api("POST", "/api/documents/jobs", job_request)[0], 201)
        self.assertIsNotNone(progress.typical("ocr_page", "vision"))  # the next estimate learns from this one

    def test_scanned_pages_are_read_and_text_pages_kept(self):
        status, data = self.upload("Vertrag.pdf", ds.pdf([TEXT_PAGE, "", ""]))
        self.assertEqual(status, 201, data)
        upload = data["upload"]
        self.assertEqual((upload["needs_ocr"], upload["ocr_pages"]), (True, [2, 3]))
        with mock.patch.object(ocr, "_messages", fake_messages({2: "Seite zwei gescannt", 3: "Seite drei gescannt"})):
            job = self.run_ocr(upload["id"])
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(documents.load_upload(upload["id"])[1],
                         f"[Seite 1]\n{TEXT_PAGE}\n\n[Seite 2]\nSeite zwei gescannt\n\n[Seite 3]\nSeite drei gescannt")

    def test_pages_left_time_left_and_cancel(self):
        status, data = self.upload("Scan.pdf", ds.pdf(["", "", "", ""]))
        upload = data["upload"]
        with mock.patch.object(ocr, "_messages", fake_messages({}, delay=0.3)):
            job_id = self.api("POST", f"/api/ocr/uploads/{upload['id']}", {})[1]["job"]["id"]
            self.assertTrue(wait_until(lambda: self.job(job_id)["done"] >= 1, 10))
            job = self.job(job_id)
            self.assertEqual((job["status"], job["pages"], job["basis"]), ("running", 4, "this_job"))
            self.assertGreaterEqual(job["page"], 2)
            self.assertIsNotNone(job["eta_s"])  # from the seconds the first page took
            self.assertGreater(job["percent"], 0)
            again = self.api("POST", f"/api/ocr/uploads/{upload['id']}", {})[1]["job"]
            self.assertEqual(again["id"], job_id)  # the same file twice: the running job
            self.assertEqual(self.api("POST", f"/api/ocr/jobs/{job_id}/cancel", {})[0], 200)
            self.assertTrue(wait_until(lambda: self.job(job_id)["status"] == "cancelled", 10))
        self.assertTrue(documents.load_upload(upload["id"])[0]["needs_ocr"])  # still waiting for its text

    def test_a_failure_is_reported(self):
        status, data = self.upload("kaputt.png", PNG)
        with mock.patch.object(ocr, "_messages", fake_messages({}, fail="Das Bild ließ sich nicht öffnen (beschädigt?).")):
            job = self.run_ocr(data["upload"]["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("beschädigt", job["error"])
        with mock.patch.object(ocr, "_messages", fake_messages({1: "   "})):
            job = self.run_ocr(data["upload"]["id"])
        self.assertIn("kein Text erkannt", job["error"])

    def test_without_text_recognition(self):
        reason = "Fotos und eingescannte Seiten kann LowRAM AI bisher nur auf dem Mac lesen (Texterkennung)."
        with mock.patch.object(ocr, "available", return_value=False), \
                mock.patch.object(ocr, "unavailable_reason", return_value=reason):
            status, data = self.upload("Foto.jpg", JPEG)
            self.assertEqual(status, 422, data)
            self.assertIn("nur auf dem Mac", data["error"])
            formats = self.api("GET", "/api/documents/formats")[1]
            self.assertFalse(formats["ocr"]["available"])
            self.assertNotIn(".jpg", formats["suffixes"])
            self.assertIn(".pdf", formats["suffixes"])
            status, data = self.upload("Scan.pdf", ds.pdf([TEXT_PAGE, "", ""]))
            self.assertEqual(status, 201, data)  # the text pages are still there, with a note
            self.assertFalse(data["upload"]["needs_ocr"])
            self.assertIn("nur auf dem Mac", " ".join(data["upload"]["warnings"]))
            self.assertEqual(self.api("POST", f"/api/ocr/uploads/{data['upload']['id']}", {})[0], 409)
        self.assertEqual(self.api("GET", "/api/ocr/jobs/zz")[0], 404)


@unittest.skipUnless(ocr.available(), "Apple's Vision (macOS with pyobjc-framework-Vision)")
class RealVisionTests(unittest.TestCase):
    def read(self, path, kind, pages=None):
        texts = {}
        for message in ocr._messages(path, kind, pages, threading.Event()):
            if message[0] == "error":
                self.fail(message[1])
            if message[0] == "page":
                texts[message[1]] = message[2]
        return texts

    def test_a_photo_of_text_is_read(self):
        from tests import ocr_samples

        with tempfile.TemporaryDirectory() as tmp:
            png = ocr_samples.text_png(Path(tmp) / "brief.png", ["Rechnung Nummer 4711", "Betrag 99 Euro"])
            text = self.read(png, "image")[1]
        self.assertIn("4711", text)
        self.assertIn("Rechnung", text)


if __name__ == "__main__":
    unittest.main()
