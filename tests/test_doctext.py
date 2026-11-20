"""doctext.py: text from PDF, Word, OpenDocument, PowerPoint, Excel, EPUB, RTF, HTML – and safe against crafted files."""

import io
import time
import unittest
import zipfile
from unittest import mock

from backend import doctext
from tests import doc_samples as ds


def slow_pdf_child(data, conn):  # stands in for a PDF that makes the reader hang (runs in the child process)
    time.sleep(30)


class FormatTests(unittest.TestCase):
    def test_word(self):
        data = ds.docx([("Heading1", "Einleitung"), ("", "Hallo Welt."), ("list", "Erster Punkt"),
                        ("berschrift2", "Teil zwei")], table=[["Name", "Preis"], ["Apfel", "2"]],
                       extra_body="<w:p><w:r><mc:AlternateContent><mc:Choice><w:t>NUR EINMAL</w:t></mc:Choice>"
                                  "<mc:Fallback><w:t>NUR EINMAL</w:t></mc:Fallback></mc:AlternateContent></w:r></w:p>"
                                  "<w:p><w:r><w:delText>gelöscht</w:delText></w:r></w:p>")
        result = doctext.extract("Bericht.docx", data)
        self.assertEqual((result.kind, result.label, result.truncated), ("docx", "Word", False))
        self.assertEqual(result.text, "# Einleitung\n\nHallo Welt.\n\n- Erster Punkt\n\n## Teil zwei\n\n"
                                      "Name | Preis\nApfel | 2\n\nNUR EINMAL")

    def test_opendocument_powerpoint_excel_epub(self):
        self.assertEqual(doctext.extract("a.odt", ds.odt([(1, "Kapitel"), (0, "Text hier.")])).text,
                         "# Kapitel\n\nText hier.\n\nA1 | B  1")
        slides = doctext.extract("a.pptx", ds.pptx([["Titel A", "Punkt 1"], ["Titel B"]]))
        self.assertEqual((slides.text, slides.pages), ("## Folie 1\nTitel A\nPunkt 1\n\n## Folie 2\nTitel B", 2))
        sheet = doctext.extract("a.xlsx", ds.xlsx([["Monat", "Umsatz"], ["Jan", 100], ["Feb", 250]]))
        self.assertEqual(sheet.text, "## Tabelle: Umsatz\nMonat | Umsatz\nJan | 100\nFeb | 250")
        book = doctext.extract("a.epub", ds.epub(["<h1>Kapitel 1</h1><p>Es war einmal.</p>", "<p>Ende.</p>"]))
        self.assertEqual((book.text, book.pages), ("# Kapitel 1\n\nEs war einmal.\n\nEnde.", 2))

    def test_rtf_html_and_text(self):
        self.assertEqual(doctext.extract("a.rtf", ds.RTF).text, "Grüße aus € Europa\nZweite Zeile\tmit Tab")
        html = doctext.extract("a.html", ds.HTML).text
        self.assertEqual(html, "# Titel\n\nErster & wichtiger Absatz.\n\n- Punkt A\n- Punkt B\n\n| x | y")
        self.assertNotIn("alert", html)
        legacy = doctext.extract("brief.txt", "Grüße\r\naus Köln".encode("cp1252"))
        self.assertEqual(legacy.text, "Grüße\naus Köln")
        self.assertTrue(legacy.warnings)  # says it was read as Windows text
        self.assertEqual(doctext.extract("x.md", "﻿# Hi".encode("utf-8")).text, "# Hi")

    def test_kind_comes_from_the_content_too(self):
        self.assertEqual(doctext.detect_kind("download", ds.docx([("", "x")])), "docx")
        self.assertEqual(doctext.detect_kind("download", ds.pdf(["x"])), "pdf")
        self.assertEqual(doctext.detect_kind("notes", b"just text"), "text")
        self.assertIsNone(doctext.detect_kind("image", b"\x89PNG\r\n\x1a\n\x00\x00\x00"))

    def test_pdf_in_a_separate_process(self):
        result = doctext.extract("Bericht.pdf", ds.pdf(["Das ist die erste Seite mit genug Text darauf.",
                                                        "Und hier steht die zweite (mit Klammern) Seite."]))
        self.assertEqual(result.pages, 2)
        self.assertEqual(result.text, "[Seite 1]\nDas ist die erste Seite mit genug Text darauf.\n\n"
                                      "[Seite 2]\nUnd hier steht die zweite (mit Klammern) Seite.")
        self.assertEqual(result.warnings, [])
        scanned = doctext.extract("scan.pdf", ds.pdf(["", ""]))
        self.assertEqual(scanned.text, "")
        self.assertIn("eingescannte", scanned.warnings[0])


class HostileFileTests(unittest.TestCase):
    """Broken or crafted files end in a clear DocumentError – quickly and without filling the RAM."""

    def assertRefused(self, name, data, words):
        with self.assertRaises(doctext.DocumentError) as caught:
            doctext.extract(name, data)
        self.assertIn(words, str(caught.exception))

    def test_unsupported_and_mislabelled(self):
        self.assertRefused("alt.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, "ältere Word-Datei")
        self.assertRefused("x.pdf", b"not a pdf", "kein PDF")
        self.assertRefused("x.docx", b"plain text", "kein gültiges Word-Dokument")
        self.assertRefused("x.docx", b"PK\x03\x04broken zip", "beschädigt")
        self.assertRefused("photo.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", "nicht unterstützt")
        self.assertRefused("leer.txt", b"", "leer")
        self.assertRefused("x.pdf", b"%PDF-1.4\n" + b"garbage " * 50, "beschädigt")

    def test_zip_bombs_and_too_many_parts(self):
        bomb = ds.zipped({"word/document.xml": "<w:document>" + " " * 3_000_000 + "</w:document>"})
        self.assertLess(len(bomb), 50_000)  # tiny on disk …
        with mock.patch.object(doctext, "MAX_PART_BYTES", 1_000_000):  # … but far bigger unpacked
            self.assertRefused("bomb.docx", bomb, "ZIP-Bomben")
        parts = ds.zipped({f"p{i}.xml": "x" for i in range(30)} | {"word/document.xml": "<a/>"})
        with mock.patch.object(doctext, "MAX_ZIP_MEMBERS", 20):
            self.assertRefused("many.docx", parts, "ungewöhnlich vielen Teilen")

    def test_no_xml_entity_tricks(self):
        laughs = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
                  '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p>'
                  "<w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>")
        self.assertRefused("lol.docx", ds.docx(document_xml=laughs), "DTD")
        external = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]>'
                    "<w:document><w:t>&ext;</w:t></w:document>")
        self.assertRefused("ext.docx", ds.docx(document_xml=external), "DTD")
        self.assertRefused("bad.docx", ds.docx(document_xml="<w:document><unclosed>"), "fehlerhaftes XML")

    def test_paths_never_leave_the_package(self):
        evil = ds.zipped({"META-INF/container.xml": '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                                                    '<rootfiles><rootfile full-path="../../etc/passwd"/></rootfiles>'
                                                    "</container>"})
        self.assertRefused("evil.epub", evil, "beschädigt")

    def test_long_documents_are_cut_with_a_note(self):
        long_doc = ds.docx([("", f"Absatz {i} " + "x" * 200) for i in range(50)])
        with mock.patch.object(doctext, "MAX_TEXT_CHARS", 3000):
            result = doctext.extract("lang.docx", long_doc)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.text), 3000)
        self.assertIn("sehr lang", result.warnings[-1])

    def test_rtf_nesting_and_unicode_edge_cases(self):
        self.assertRefused("deep.rtf", b"{\\rtf1" + b"{" * 1100 + b"x" + b"}" * 1100 + b"}", "verschachtelt")
        text = doctext.extract("u.rtf", b"{\\rtf1\\uc1 \\u-3?\\u55357?\\u252? ok}").text
        self.assertIn("ok", text)  # negative and surrogate code points never crash

    def test_a_hanging_pdf_is_stopped(self):
        started = time.monotonic()
        with mock.patch.object(doctext, "_pdf_child", slow_pdf_child), \
                mock.patch.object(doctext, "PDF_TIMEOUT_S", 1.0):
            self.assertRefused("slow.pdf", ds.pdf(["x"]), "abgebrochen")
        self.assertLess(time.monotonic() - started, 15)

    def test_encrypted_parts_are_refused(self):
        data = bytearray(ds.docx([("", "geheim")]))
        for marker, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):  # the "encrypted" flag in both headers
            start = 0
            while (start := data.find(marker, start)) != -1:
                data[start + offset] |= 0x1
                start += 4
        with self.assertRaises(doctext.DocumentError):
            doctext.extract("geheim.docx", bytes(data))

    def test_damaged_zip_part(self):
        good = ds.docx([("", "Hallo " * 200)])
        with zipfile.ZipFile(io.BytesIO(good)) as zf:
            offset = zf.getinfo("word/document.xml").header_offset
        broken = bytearray(good)
        broken[offset + 60: offset + 90] = b"\xff" * 30  # corrupt the compressed bytes
        with self.assertRaises(doctext.DocumentError):
            doctext.extract("kaputt.docx", bytes(broken))


if __name__ == "__main__":
    unittest.main()
