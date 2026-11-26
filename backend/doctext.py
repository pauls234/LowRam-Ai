"""Text from documents: PDF, Word, OpenDocument, PowerPoint, Excel, EPUB, RTF, HTML and plain text.

Only the text is read – never macros, links, scripts or embedded objects. Every format has hard
limits, so a broken or crafted file cannot hang or bloat LowRAM AI:

* the file size (:data:`MAX_FILE_BYTES`) and the length of the text (:data:`MAX_TEXT_CHARS`);
* ZIP-based documents (Word, PowerPoint, Excel, OpenDocument, EPUB): the number of parts and
  the unpacked size of every part and of all parts together are capped while unpacking – a
  "zip bomb" stops at the limit instead of filling the RAM;
* XML is parsed as a stream with expat; a DOCTYPE or entity declaration is refused (no entity
  tricks, nothing is ever fetched);
* PDFs are read by pypdf in a separate process that is ended after :data:`PDF_TIMEOUT_S` – a
  crafted PDF can neither hang the app nor keep memory in it.

Errors are :class:`DocumentError` with a German message for the UI.
"""

from __future__ import annotations

import codecs
import io
import multiprocessing
import posixpath
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.parsers import expat

MIB = 1024 * 1024
MAX_FILE_BYTES = 40 * MIB
MAX_TEXT_CHARS = 3_000_000  # ~ 1000 book pages
MAX_ZIP_MEMBERS = 10_000
MAX_PART_BYTES = 64 * MIB  # one unpacked part (e.g. word/document.xml)
MAX_UNPACKED_BYTES = 200 * MIB  # all parts read from one document
MAX_SHEET_CELLS = 500_000
MAX_EPUB_CHAPTERS = 2000
PDF_MAX_PAGES = 3000
PDF_TIMEOUT_S = 180.0
SCANNED_CHARS_PER_PAGE = 25  # less text than this per page: probably scanned images

TEXT_SUFFIXES = frozenset((
    ".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".log", ".xml", ".srt", ".vtt", ".tex", ".bib", ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".sh", ".sql", ".css",
))
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".gif", ".bmp", ".webp")
SUFFIX_KIND = {
    ".pdf": "pdf", ".docx": "docx", ".odt": "odt", ".pptx": "pptx", ".xlsx": "xlsx", ".epub": "epub",
    ".rtf": "rtf", ".html": "html", ".htm": "html", ".xhtml": "html",
    **{suffix: "text" for suffix in TEXT_SUFFIXES},
    **{suffix: "image" for suffix in IMAGE_SUFFIXES},  # photos and scans: text recognition (backend.ocr)
}
LOW_TEXT_CHARS = 25  # a PDF page with fewer characters is probably a scanned image
OLD_OFFICE = {".doc": "Word", ".xls": "Excel", ".ppt": "PowerPoint"}
KIND_LABEL = {"pdf": "PDF", "docx": "Word", "odt": "OpenDocument-Text", "pptx": "PowerPoint", "xlsx": "Excel",
              "epub": "E-Book (EPUB)", "rtf": "RTF", "html": "Webseite (HTML)", "text": "Text",
              "audio": "Verschriftlichung (Audio)", "image": "Foto/Scan"}

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "http://schemas.openxmlformats.org/presentationml/2006/main}"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships}"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006}"
TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
OPF = "http://www.idpf.org/2007/opf}"
CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container}"


class DocumentError(ValueError):
    """The document cannot be read (format, damage, password, limits); the message is for the user."""


@dataclass
class Extracted:
    text: str
    kind: str
    pages: int | None = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    page_texts: list[str] | None = None  # PDF: the text of every page (to merge recognised pages in later)
    low_text_pages: list[int] = field(default_factory=list)  # PDF: pages (1-based) with almost no text

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    @property
    def needs_ocr(self) -> bool:
        """A photo, or a PDF that is mostly scanned pages: only text recognition can read it."""
        if self.kind == "image":
            return True
        if self.kind != "pdf" or not self.page_texts:
            return False
        return len(self.low_text_pages) >= max(1, 0.3 * len(self.page_texts))


class _Enough(Exception):
    """Raised inside a parser callback once the text limit is reached."""


# --------------------------------------------------------------------------- entry point

def detect_kind(name: str, data: bytes) -> str | None:
    """``pdf``, ``docx``, … or ``text`` from the file name, checked against the file's own signature."""
    suffix = posixpath.splitext((name or "").lower())[1]
    head = data[:8]
    if suffix in OLD_OFFICE or head.startswith(b"\xd0\xcf\x11\xe0"):
        what = OLD_OFFICE.get(suffix, "Office")
        raise DocumentError(f"Das ist eine ältere {what}-Datei oder eine mit Passwort geschützte Office-Datei. "
                            "Speichere sie bitte als PDF oder im neuen Format (.docx, .xlsx, .pptx).")
    if image_kind(data):
        return "image"
    kind = SUFFIX_KIND.get(suffix)
    if kind == "image":
        raise DocumentError("Die Datei heißt wie ein Bild, ist aber keins (beschädigt?).")
    is_zip = head.startswith(b"PK\x03\x04")
    if kind == "pdf" or (kind is None and b"%PDF-" in data[:1024]):
        if b"%PDF-" not in data[:1024]:
            raise DocumentError("Die Datei heißt .pdf, ist aber kein PDF.")
        return "pdf"
    if kind in ("docx", "odt", "pptx", "xlsx", "epub"):
        if not is_zip:
            raise DocumentError(f"Die Datei ist kein gültiges {KIND_LABEL[kind]}-Dokument (beschädigt?).")
        return kind
    if kind is None and is_zip:
        return _sniff_zip(data)
    if kind is None and data.lstrip()[:5] == b"{\\rtf":
        return "rtf"
    if kind is None:
        if b"\x00" in data[:4096] and not data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return None  # some binary format
        return "text"
    return kind


def image_kind(data: bytes) -> str | None:
    """The image format from the first bytes (``jpeg``, ``png``, ``heic`` …) or None."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:2] == b"BM" and len(data) > 26:
        return "bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif"):
        return "heic"
    return None


def _sniff_zip(data: bytes) -> str | None:
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist()[:MAX_ZIP_MEMBERS])
    except (zipfile.BadZipFile, ValueError, NotImplementedError):
        return None
    for kind, marker in (("docx", "word/document.xml"), ("pptx", "ppt/presentation.xml"),
                         ("xlsx", "xl/workbook.xml"), ("epub", "META-INF/container.xml"), ("odt", "content.xml")):
        if marker in names:
            return kind
    return None


def extract(name: str, data: bytes) -> Extracted:
    """The text of one document (see the module docstring for the limits)."""
    if len(data) > MAX_FILE_BYTES:
        raise DocumentError(f"Die Datei ist größer als {MAX_FILE_BYTES // MIB} MB.")
    if not data:
        raise DocumentError("Die Datei ist leer.")
    kind = detect_kind(name, data)
    if kind is None:
        raise DocumentError("Dieses Dateiformat wird nicht unterstützt. Möglich sind PDF, Word (.docx), "
                            "OpenDocument (.odt), PowerPoint (.pptx), Excel (.xlsx), EPUB, RTF, HTML und Text.")
    result = _READERS[kind](data)
    return _finish(result)


def _finish(result: Extracted) -> Extracted:
    text = result.text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if len(text) > MAX_TEXT_CHARS:
        cut = text.rfind("\n", 0, MAX_TEXT_CHARS)
        text = text[: cut if cut > MAX_TEXT_CHARS // 2 else MAX_TEXT_CHARS].rstrip()
        result.truncated = True
    if result.truncated:
        result.warnings.append(f"Das Dokument ist sehr lang – gelesen wurden die ersten {len(text):,} Zeichen."
                               .replace(",", "."))
    result.text = text
    return result


# --------------------------------------------------------------------------- plain text

def decode_text(data: bytes) -> tuple[str, list[str]]:
    """UTF-8 (with or without BOM), UTF-16 with BOM, else Windows-1252 (old German Windows files)."""
    for bom, codec in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            return data.decode(codec, errors="replace"), []
    try:
        return data.decode("utf-8"), []
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), ["Die Datei war nicht in UTF-8 gespeichert und wurde als "
                                                         "Windows-Text (cp1252) gelesen."]


def _read_text(data: bytes) -> Extracted:
    text, warnings = decode_text(data)
    if text.count("\ufffd") > max(20, len(text) // 50):
        raise DocumentError("Die Datei enthält kaum lesbaren Text – ist es wirklich eine Textdatei?")
    return Extracted(text, "text", warnings=warnings)


# --------------------------------------------------------------------------- ZIP and XML helpers

class _Zip:
    def __init__(self, data: bytes, kind: str) -> None:
        self.kind = kind
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zf.infolist()
        except (zipfile.BadZipFile, ValueError, NotImplementedError, OSError) as exc:
            raise DocumentError(f"Das {KIND_LABEL[kind]}-Dokument ist beschädigt.") from exc
        if len(infos) > MAX_ZIP_MEMBERS:
            raise DocumentError("Das Dokument besteht aus ungewöhnlich vielen Teilen und wird nicht gelesen.")
        self.names = {info.filename for info in infos}
        self.unpacked = 0

    def has(self, name: str) -> bool:
        return name in self.names

    def read(self, name: str) -> bytes:
        try:
            info = self.zf.getinfo(name)
        except KeyError as exc:
            raise DocumentError(f"Im {KIND_LABEL[self.kind]}-Dokument fehlt ein Teil ({name}).") from exc
        if info.flag_bits & 0x1:
            raise DocumentError("Das Dokument ist mit einem Passwort geschützt.")
        try:
            with self.zf.open(info) as handle:
                data = handle.read(MAX_PART_BYTES + 1)  # unpacks only up to the limit
        except Exception as exc:  # zlib/lzma/bz2 errors, bad CRC, truncated data …: all mean "damaged"
            raise DocumentError(f"Das {KIND_LABEL[self.kind]}-Dokument ist beschädigt.") from exc
        self.unpacked += len(data)
        if len(data) > MAX_PART_BYTES or self.unpacked > MAX_UNPACKED_BYTES:
            raise DocumentError("Das Dokument ist entpackt zu groß und wird nicht gelesen (Schutz vor ZIP-Bomben).")
        return data


def _parse_xml(data: bytes, start: Callable | None = None, end: Callable | None = None,
               chars: Callable | None = None) -> None:
    """Stream ``data`` through expat; names arrive as ``"{namespace}local"`` without the brace."""
    parser = expat.ParserCreate(namespace_separator="}")

    def refuse(*_args) -> None:
        raise DocumentError("Das Dokument enthält eine DTD oder Entity-Definition und wird nicht gelesen.")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.buffer_text = True
    if start:
        parser.StartElementHandler = start
    if end:
        parser.EndElementHandler = end
    if chars:
        parser.CharacterDataHandler = chars
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise DocumentError("Das Dokument ist beschädigt (fehlerhaftes XML).") from exc


class _Collector:
    """Paragraphs with a running length check (stops parsing once the text limit is reached)."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False

    def add(self, text: str) -> None:
        self.parts.append(text)
        self.length += len(text) + 1
        if self.length > MAX_TEXT_CHARS:
            self.truncated = True
            raise _Enough

    def text(self, sep: str = "\n") -> str:
        return sep.join(self.parts)


def _relationships(zipped: _Zip, rels_path: str, base: str) -> dict[str, str]:
    """``{rId: part path}`` from an OOXML ``.rels`` file (targets resolved, never outside the package)."""
    if not zipped.has(rels_path):
        return {}
    found: dict[str, str] = {}

    def start(name, attrs):
        if name == PKG_REL + "Relationship" and attrs.get("TargetMode") != "External":
            target = attrs.get("Target", "")
            path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))
            if not path.startswith(("../", "/")) and path != "..":
                found[attrs.get("Id", "")] = path

    _parse_xml(zipped.read(rels_path), start=start)
    return found


# --------------------------------------------------------------------------- Word (.docx)

_HEADING = re.compile(r"(?i)^(?:heading|berschrift|überschrift)\s*([1-6])$")


def _read_docx(data: bytes) -> Extracted:
    zipped = _Zip(data, "docx")
    out = _Collector()
    state = {"skip": 0, "in_t": False, "style": "", "list": False, "depth": 0}
    para: list[str] = []
    cell: list[str] = []
    row: list[str] = []
    skip_tags = {MC + "Fallback", W + "del", W + "instrText", W + "delText"}

    def start(name, attrs):
        if name in skip_tags:
            state["skip"] += 1
        elif state["skip"]:
            return
        elif name == W + "p":
            para.clear()
            state["style"], state["list"] = "", False
        elif name == W + "pStyle":
            state["style"] = attrs.get(W + "val", "")
        elif name == W + "numPr":
            state["list"] = True
        elif name == W + "t":
            state["in_t"] = True
        elif name == W + "tab":
            para.append("\t")
        elif name in (W + "br", W + "cr"):
            para.append("\n")
        elif name == W + "tbl":
            state["depth"] += 1

    def end(name):
        if name in skip_tags:
            state["skip"] -= 1
            return
        if state["skip"]:
            return
        if name == W + "t":
            state["in_t"] = False
        elif name == W + "p":
            text = "".join(para).strip()
            match = _HEADING.match(state["style"])
            if text and match:
                text = "#" * int(match.group(1)) + " " + text
            elif text and state["style"].lower() == "title":
                text = "# " + text
            elif text and state["list"]:
                text = "- " + text
            if state["depth"]:
                if text:
                    cell.append(text)
            elif text:
                out.add(text + "\n")
        elif name == W + "tc":
            row.append(" ".join(cell))
            cell.clear()
        elif name == W + "tr":
            if any(row):
                out.add(" | ".join(row))
            row.clear()
        elif name == W + "tbl":
            state["depth"] = max(0, state["depth"] - 1)
            if not state["depth"]:
                out.add("")

    def chars(text):
        if state["in_t"] and not state["skip"]:
            para.append(text)

    try:
        _parse_xml(zipped.read("word/document.xml"), start, end, chars)
    except _Enough:
        pass
    return Extracted(out.text(), "docx", truncated=out.truncated)


# --------------------------------------------------------------------------- OpenDocument text (.odt)

def _read_odt(data: bytes) -> Extracted:
    zipped = _Zip(data, "odt")
    out = _Collector()
    state = {"in_p": 0, "skip": 0, "level": 0, "list": 0, "table": 0}
    para: list[str] = []
    cell: list[str] = []
    row: list[str] = []
    skip_tags = {OFFICE_NS + "annotation", TEXT_NS + "tracked-changes", TEXT_NS + "note-citation"}

    def start(name, attrs):
        if name in skip_tags:
            state["skip"] += 1
        elif state["skip"]:
            return
        elif name in (TEXT_NS + "p", TEXT_NS + "h"):
            state["in_p"] += 1
            if state["in_p"] == 1:
                para.clear()
                level = attrs.get(TEXT_NS + "outline-level", "1") if name == TEXT_NS + "h" else ""
                state["level"] = int(level) if level.isdigit() else 0
        elif name == TEXT_NS + "s":
            count = attrs.get(TEXT_NS + "c", "1")
            para.append(" " * min(int(count) if count.isdigit() else 1, 100))
        elif name == TEXT_NS + "tab":
            para.append("\t")
        elif name == TEXT_NS + "line-break":
            para.append("\n")
        elif name == TEXT_NS + "list-item":
            state["list"] += 1
        elif name == TABLE_NS + "table":
            state["table"] += 1

    def end(name):
        if name in skip_tags:
            state["skip"] -= 1
            return
        if state["skip"]:
            return
        if name in (TEXT_NS + "p", TEXT_NS + "h"):
            state["in_p"] -= 1
            if state["in_p"]:
                return
            text = "".join(para).strip()
            if text and state["level"]:
                text = "#" * min(state["level"], 6) + " " + text
            elif text and state["list"]:
                text = "- " + text
            if state["table"]:
                if text:
                    cell.append(text)
            elif text:
                out.add(text + "\n")
        elif name == TEXT_NS + "list-item":
            state["list"] -= 1
        elif name == TABLE_NS + "table-cell":
            row.append(" ".join(cell))
            cell.clear()
        elif name == TABLE_NS + "table-row":
            if any(row):
                out.add(" | ".join(row))
            row.clear()
        elif name == TABLE_NS + "table":
            state["table"] = max(0, state["table"] - 1)
            if not state["table"]:
                out.add("")

    def chars(text):
        if state["in_p"] and not state["skip"]:
            para.append(text)

    try:
        _parse_xml(zipped.read("content.xml"), start, end, chars)
    except _Enough:
        pass
    return Extracted(out.text(), "odt", truncated=out.truncated)


# --------------------------------------------------------------------------- PowerPoint (.pptx)

def _slide_paths(zipped: _Zip) -> list[str]:
    rels = _relationships(zipped, "ppt/_rels/presentation.xml.rels", "ppt")
    ordered: list[str] = []
    if rels and zipped.has("ppt/presentation.xml"):
        def start(name, attrs):
            if name == P + "sldId":
                path = rels.get(attrs.get(R + "id", ""))
                if path and zipped.has(path):
                    ordered.append(path)

        _parse_xml(zipped.read("ppt/presentation.xml"), start=start)
    if ordered:
        return ordered
    numbered = [(int(m.group(1)), name) for name in zipped.names
                if (m := re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name))]
    return [name for _, name in sorted(numbered)]


def _read_pptx(data: bytes) -> Extracted:
    zipped = _Zip(data, "pptx")
    out = _Collector()
    slides = _slide_paths(zipped)
    try:
        for number, path in enumerate(slides, 1):
            paragraphs: list[str] = []
            para: list[str] = []
            state = {"in_t": False}

            def start(name, attrs, para=para, state=state):
                if name == A + "p":
                    para.clear()
                elif name == A + "t":
                    state["in_t"] = True
                elif name == A + "br":
                    para.append("\n")

            def end(name, para=para, state=state, paragraphs=paragraphs):
                if name == A + "t":
                    state["in_t"] = False
                elif name == A + "p":
                    text = "".join(para).strip()
                    if text:
                        paragraphs.append(text)

            def chars(text, para=para, state=state):
                if state["in_t"]:
                    para.append(text)

            _parse_xml(zipped.read(path), start, end, chars)
            if paragraphs:
                out.add(f"## Folie {number}\n" + "\n".join(paragraphs) + "\n")
    except _Enough:
        pass
    return Extracted(out.text(), "pptx", pages=len(slides), truncated=out.truncated)


# --------------------------------------------------------------------------- Excel (.xlsx)

def _shared_strings(zipped: _Zip) -> list[str]:
    if not zipped.has("xl/sharedStrings.xml"):
        return []
    strings: list[str] = []
    buf: list[str] = []
    state = {"in_t": False, "phonetic": 0}

    def start(name, attrs):
        if name == S + "si":
            buf.clear()
        elif name == S + "rPh":
            state["phonetic"] += 1
        elif name == S + "t" and not state["phonetic"]:
            state["in_t"] = True

    def end(name):
        if name == S + "t":
            state["in_t"] = False
        elif name == S + "rPh":
            state["phonetic"] -= 1
        elif name == S + "si":
            strings.append("".join(buf))
            if len(strings) > MAX_SHEET_CELLS:
                raise DocumentError("Die Excel-Datei enthält zu viele Texte und wird nicht gelesen.")

    def chars(text):
        if state["in_t"]:
            buf.append(text)

    _parse_xml(zipped.read("xl/sharedStrings.xml"), start, end, chars)
    return strings


def _read_xlsx(data: bytes) -> Extracted:
    zipped = _Zip(data, "xlsx")
    rels = _relationships(zipped, "xl/_rels/workbook.xml.rels", "xl")
    sheets: list[tuple[str, str]] = []

    def sheet_start(name, attrs):
        if name == S + "sheet":
            path = rels.get(attrs.get(R + "id", ""))
            if path and zipped.has(path):
                sheets.append((attrs.get("name", "Tabelle"), path))

    _parse_xml(zipped.read("xl/workbook.xml"), start=sheet_start)
    strings = _shared_strings(zipped)
    out = _Collector()
    cells = 0
    try:
        for title, path in sheets:
            rows: list[str] = []
            row: list[str] = []
            value: list[str] = []
            state = {"type": "", "in_v": False}

            def start(name, attrs, row=row, value=value, state=state):
                if name == S + "row":
                    row.clear()
                elif name == S + "c":
                    state["type"] = attrs.get("t", "")
                    value.clear()
                elif name in (S + "v", S + "t"):
                    state["in_v"] = True

            def end(name, rows=rows, row=row, value=value, state=state):
                nonlocal cells
                if name in (S + "v", S + "t"):
                    state["in_v"] = False
                elif name == S + "c":
                    raw = "".join(value).strip()
                    if state["type"] == "s":
                        raw = strings[int(raw)] if raw.isdigit() and int(raw) < len(strings) else ""
                    elif state["type"] == "b":
                        raw = "WAHR" if raw == "1" else "FALSCH" if raw == "0" else raw
                    row.append(raw.replace("\n", " ").strip())
                    cells += 1
                    if cells > MAX_SHEET_CELLS:
                        out.truncated = True
                        raise _Enough
                elif name == S + "row":
                    while row and not row[-1]:
                        row.pop()
                    if row:
                        rows.append(" | ".join(row))

            def chars(text, value=value, state=state):
                if state["in_v"]:
                    value.append(text)

            try:
                _parse_xml(zipped.read(path), start, end, chars)
            finally:
                if rows:
                    out.add(f"## Tabelle: {title}\n" + "\n".join(rows) + "\n")
    except _Enough:
        out.truncated = True
    return Extracted(out.text(), "xlsx", pages=len(sheets), truncated=out.truncated)


# --------------------------------------------------------------------------- HTML and EPUB

class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe", "object", "canvas", "nav", "button",
            "select", "form"}
    BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "aside", "ul", "ol", "table", "tr",
             "blockquote", "pre", "hr", "figure", "figcaption", "dl", "dt", "dd", "address"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.pre = 0
        self.length = 0
        self.truncated = False

    def _put(self, text: str) -> None:
        if self.truncated:
            return
        self.parts.append(text)
        self.length += len(text)
        if self.length > MAX_TEXT_CHARS:
            self.truncated = True

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif re.fullmatch(r"h[1-6]", tag):
            self._put("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._put("\n- ")
        elif tag == "br":
            self._put("\n")
        elif tag in ("td", "th"):
            self._put(" | ")
        elif tag in self.BLOCK:
            self._put("\n\n" if tag in ("p", "table", "blockquote", "pre") else "\n")
            if tag == "pre":
                self.pre += 1

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.skip:
            self._put("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in self.BLOCK or re.fullmatch(r"h[1-6]", tag):
            self._put("\n")
            if tag == "pre":
                self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if self.skip:
            return
        self._put(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        lines = [line.strip() for line in "".join(self.parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> tuple[str, bool]:
    """Readable text of an HTML page (headings as ``#``, lists as ``-``); ``(text, truncated)``."""
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError):  # very broken markup: keep what was read
        pass
    return parser.text(), parser.truncated


def _decode_html(data: bytes) -> str:
    match = re.search(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_-]{2,40})""", data[:4096], re.I)
    if match:
        try:
            name = match.group(1).decode("ascii")
            codecs.lookup(name)
            return data.decode(name, errors="replace")
        except LookupError:
            pass
    return decode_text(data)[0]


def _read_html(data: bytes) -> Extracted:
    text, truncated = html_to_text(_decode_html(data))
    return Extracted(text, "html", truncated=truncated)


def _read_epub(data: bytes) -> Extracted:
    zipped = _Zip(data, "epub")
    root: list[str] = []

    def container_start(name, attrs):
        if name == CONTAINER + "rootfile" and not root:
            root.append(posixpath.normpath(unquote(attrs.get("full-path", ""))))

    _parse_xml(zipped.read("META-INF/container.xml"), start=container_start)
    if not root or root[0].startswith(("..", "/")):
        raise DocumentError("Das E-Book ist beschädigt (kein Inhaltsverzeichnis).")
    base = posixpath.dirname(root[0])
    manifest: dict[str, str] = {}
    spine: list[str] = []

    def opf_start(name, attrs):
        if name == OPF + "item":
            href = posixpath.normpath(posixpath.join(base, unquote(attrs.get("href", "").split("#")[0])))
            if not href.startswith(("..", "/")):
                manifest[attrs.get("id", "")] = href
        elif name == OPF + "itemref":
            spine.append(attrs.get("idref", ""))

    _parse_xml(zipped.read(root[0]), start=opf_start)
    out = _Collector()
    chapters = [manifest[idref] for idref in spine if manifest.get(idref) and zipped.has(manifest[idref])]
    try:
        for path in chapters[:MAX_EPUB_CHAPTERS]:
            text, cut = html_to_text(decode_text(zipped.read(path))[0])
            if text:
                out.add(text + "\n")
            if cut:
                out.truncated = True
                raise _Enough
    except _Enough:
        out.truncated = True
    return Extracted(out.text(), "epub", pages=len(chapters),
                     truncated=out.truncated or len(chapters) > MAX_EPUB_CHAPTERS)


# --------------------------------------------------------------------------- RTF

_RTF_TOKEN = re.compile(r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|[\r\n]+"
                        r"|([^\\{}\r\n]+)")
_RTF_SKIP = frozenset(("fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer", "headerl",
                       "headerr", "footerl", "footerr", "themedata", "datastore", "latentstyles", "listtable",
                       "listoverridetable", "rsidtbl", "generator", "xmlnstbl", "mmathPr", "fldinst", "filetbl",
                       "revtbl", "pgdsctbl", "colorschememapping", "nonshppict", "shppict", "bkmkstart", "bkmkend"))
_RTF_CHARS = {"par": "\n", "line": "\n", "sect": "\n\n", "page": "\n\n", "tab": "\t", "emdash": "—", "endash": "–",
              "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”", "cell": " | ",
              "row": "\n"}


def _read_rtf(data: bytes) -> Extracted:
    source = data.decode("latin-1")  # RTF itself is 7-bit; \'hh escapes carry code page bytes
    codepage = "cp1252"
    stack: list[tuple[bool, int]] = []
    skip, uc, pending = False, 1, 0
    out: list[str] = []
    length = 0
    truncated = False
    for match in _RTF_TOKEN.finditer(source):
        word, arg, hexbyte, symbol, brace, text = match.groups()
        if brace == "{":
            if len(stack) > 1000:
                raise DocumentError("Die RTF-Datei ist zu tief verschachtelt und wird nicht gelesen.")
            stack.append((skip, uc))
            continue
        if brace == "}":
            skip, uc = stack.pop() if stack else (False, 1)
            continue
        if symbol is not None:
            if symbol == "*":
                skip = True
            elif symbol in "\\{}" and not skip:
                out.append(symbol)
            elif symbol == "~" and not skip:
                out.append("\u00a0")
            continue
        if word is not None:
            if word in _RTF_SKIP:
                skip = True
            elif word == "ansicpg" and arg:
                try:
                    codecs.lookup(f"cp{int(arg)}")
                    codepage = f"cp{int(arg)}"
                except (LookupError, ValueError):
                    pass
            elif word == "uc" and arg:
                uc = max(0, min(int(arg), 4))
            elif word == "u" and arg and not skip:
                value = int(arg) % 65536 if int(arg) < 0 else int(arg)
                out.append(chr(value) if value < 0x110000 and not 0xD800 <= value <= 0xDFFF else "")
                pending = uc
            elif not skip and word in _RTF_CHARS:
                out.append(_RTF_CHARS[word])
            continue
        if pending:  # the replacement characters after \uN are skipped
            if hexbyte is not None:
                pending -= 1
                continue
            if text:
                take = min(pending, len(text))
                text = text[take:]
                pending -= take
        if skip:
            continue
        if hexbyte is not None:
            out.append(bytes([int(hexbyte, 16)]).decode(codepage, errors="replace"))
        elif text:
            out.append(text)
        else:
            continue
        length += len(out[-1])
        if length > MAX_TEXT_CHARS:
            truncated = True
            break
    return Extracted("".join(out), "rtf", truncated=truncated)


# --------------------------------------------------------------------------- PDF (separate process)

def _read_pdf_pages(data: bytes) -> dict:
    """Runs in the child process: the text of each page (pypdf)."""
    import logging

    from pypdf import PdfReader  # imported here: only the child process needs it

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # damaged files are reported to the user instead
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")
        except Exception:  # e.g. AES without the optional crypto library
            opened = 0
        if not opened:
            raise DocumentError("Das PDF ist mit einem Passwort geschützt. Entferne den Schutz und versuche es erneut.")
    total_pages = len(reader.pages)
    texts: list[str] = []
    failed = length = 0
    truncated = False
    for index in range(min(total_pages, PDF_MAX_PAGES)):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:  # one broken page must not lose the others
            text, failed = "", failed + 1
        texts.append(text)
        length += len(text)
        if length > MAX_TEXT_CHARS:
            truncated = True
            break
    return {"pages": total_pages, "texts": texts, "failed": failed,
            "truncated": truncated or total_pages > PDF_MAX_PAGES}


def _pdf_child(data: bytes, conn) -> None:
    try:
        conn.send(("ok", _read_pdf_pages(data)))
    except DocumentError as exc:
        conn.send(("error", str(exc)))
    except Exception as exc:  # noqa: BLE001 - everything else is a damaged PDF for the user
        conn.send(("error", f"Das PDF konnte nicht gelesen werden ({type(exc).__name__}). Ist es beschädigt?"))
    finally:
        conn.close()


def _read_pdf(data: bytes) -> Extracted:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_pdf_child, args=(data, sender), daemon=True, name="lowram-pdf")
    process.start()
    sender.close()
    try:
        if not receiver.poll(PDF_TIMEOUT_S):
            raise DocumentError(f"Das PDF braucht zum Lesen länger als {PDF_TIMEOUT_S / 60:g} Minuten und wurde "
                                "abgebrochen. Ist es sehr groß oder beschädigt?")
        status, payload = receiver.recv()
    except (EOFError, OSError) as exc:
        raise DocumentError("Das PDF konnte nicht gelesen werden (der Leseprozess wurde beendet).") from exc
    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
        process.join(5)
    if status != "ok":
        raise DocumentError(str(payload))
    texts = payload["texts"]
    result = Extracted(join_pages(texts), "pdf", pages=payload["pages"], truncated=payload["truncated"],
                       page_texts=list(texts),
                       low_text_pages=[n for n, text in enumerate(texts, 1) if len(text.strip()) < LOW_TEXT_CHARS])
    chars = sum(len(text.strip()) for text in texts)
    if payload["pages"] and chars < SCANNED_CHARS_PER_PAGE * max(1, len(texts)):
        result.warnings.append("Das PDF enthält kaum Text – vermutlich eingescannte Seiten (Bilder).")
    if payload["failed"]:
        result.warnings.append(f"{payload['failed']} Seite(n) konnten nicht gelesen werden.")
    return result


def join_pages(texts: list[str]) -> str:
    """The text of a PDF from its pages: ``[Seite N]`` before every page that has text."""
    return "\n\n".join(f"[Seite {number}]\n{text.strip()}" for number, text in enumerate(texts, 1) if text.strip())


def _read_image(data: bytes) -> Extracted:
    """A photo or scan: no text of its own – text recognition (backend.ocr) reads it."""
    return Extracted("", "image", pages=1)


_READERS: dict[str, Callable[[bytes], Extracted]] = {
    "text": _read_text, "pdf": _read_pdf, "docx": _read_docx, "odt": _read_odt, "pptx": _read_pptx,
    "xlsx": _read_xlsx, "epub": _read_epub, "rtf": _read_rtf, "html": _read_html, "image": _read_image,
}
