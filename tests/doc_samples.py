"""Small, valid sample documents in every supported format (built in memory for the tests)."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def zipped(parts: dict[str, str | bytes], *, stored_first: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if stored_first:
            zf.writestr(zipfile.ZipInfo(stored_first), parts[stored_first], compress_type=zipfile.ZIP_STORED)
        for name, content in parts.items():
            if name != stored_first:
                zf.writestr(name, content)
    return buf.getvalue()


def docx(paragraphs: list[tuple[str, str]] | None = None, *, table: list[list[str]] | None = None,
         extra_body: str = "", document_xml: str | None = None) -> bytes:
    """``paragraphs``: ``(style, text)`` – style "" for normal text, "list" for a list item, or e.g. "Heading1"."""
    body = []
    for style, text in paragraphs or []:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        if style == "list":
            ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        body.append(f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>")
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{escape(c)}</w:t></w:r></w:p></w:tc>" for c in row)
                       + "</w:tr>" for row in table)
        body.append(f"<w:tbl>{rows}</w:tbl>")
    xml = document_xml or (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                           f'<w:document xmlns:w="{W_NS}" xmlns:mc="http://schemas.openxmlformats.org/'
                           f'markup-compatibility/2006"><w:body>{"".join(body)}{extra_body}</w:body></w:document>')
    return zipped({
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                               'content-types"/>',
        "word/document.xml": xml,
    })


def odt(paragraphs: list[tuple[int, str]]) -> bytes:
    """``(outline level, text)`` – level 0 for a normal paragraph."""
    body = "".join(f'<text:h text:outline-level="{lvl}">{escape(t)}</text:h>' if lvl else f"<text:p>{escape(t)}</text:p>"
                   for lvl, t in paragraphs)
    content = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
               'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
               f"<office:body><office:text>{body}"
               "<table:table><table:table-row><table:table-cell><text:p>A1</text:p></table:table-cell>"
               "<table:table-cell><text:p>B<text:s text:c=\"2\"/>1</text:p></table:table-cell></table:table-row>"
               "</table:table></office:text></office:body></office:document-content>")
    return zipped({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content},
                  stored_first="mimetype")


def pptx(slides: list[list[str]]) -> bytes:
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    parts: dict[str, str] = {}
    ids, rels = [], []
    # file names in reverse order on purpose: the order comes from presentation.xml
    for index, texts in enumerate(slides, 1):
        number = len(slides) - index + 1
        paras = "".join(f"<a:p><a:r><a:t>{escape(t)}</a:t></a:r></a:p>" for t in texts)
        parts[f"ppt/slides/slide{number}.xml"] = (f'<p:sld xmlns:p="{p}" xmlns:a="{a}"><p:cSld><p:spTree><p:sp>'
                                                  f"<p:txBody>{paras}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
        ids.append(f'<p:sldId id="{255 + index}" r:id="rId{index}"/>')
        rels.append(f'<Relationship Id="rId{index}" Type="slide" Target="slides/slide{number}.xml"/>')
    parts["ppt/presentation.xml"] = (f'<p:presentation xmlns:p="{p}" xmlns:r="{R_NS}"><p:sldIdLst>{"".join(ids)}'
                                     "</p:sldIdLst></p:presentation>")
    parts["ppt/_rels/presentation.xml.rels"] = f'<Relationships xmlns="{PKG_NS}">{"".join(rels)}</Relationships>'
    return zipped(parts)


def xlsx(rows: list[list[str | int]], sheet: str = "Umsatz") -> bytes:
    s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    shared: list[str] = []
    xml_rows = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, int):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    return zipped({
        "xl/workbook.xml": f'<workbook xmlns="{s}" xmlns:r="{R_NS}"><sheets><sheet name="{escape(sheet)}" sheetId="1" '
                           'r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PKG_NS}"><Relationship Id="rId1" Type="worksheet" '
                                      'Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst xmlns="{s}">' + "".join(f"<si><t>{escape(x)}</t></si>" for x in shared) + "</sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{s}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
    })


def epub(chapters: list[str]) -> bytes:
    items = "".join(f'<item id="c{i}" href="text/ch{i}.xhtml" media-type="application/xhtml+xml"/>'
                    for i in range(len(chapters)))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    parts = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                                  '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        "OEBPS/content.opf": f'<package xmlns="http://www.idpf.org/2007/opf"><manifest>{items}</manifest>'
                             f"<spine>{spine}</spine></package>",
    }
    for i, html in enumerate(chapters):
        parts[f"OEBPS/text/ch{i}.xhtml"] = f"<html><body>{html}</body></html>"
    return zipped(parts, stored_first="mimetype")


def pdf(pages: list[str]) -> bytes:
    """A minimal valid PDF with one line of Helvetica text per page (ASCII)."""
    objects: list[bytes] = []
    kids = []
    font_id = 3 + 2 * len(pages)
    for index, text in enumerate(pages):
        page_id, content_id = 3 + 2 * index, 4 + 2 * index
        kids.append(f"{page_id} 0 R")
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                       f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    all_objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
                   f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()] + objects
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(all_objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(all_objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(all_objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


RTF = (rb"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0 Arial;}}{\colortbl;\red0\green0\blue0;}"
       rb"{\*\generator Test;}\f0 Gr\'fc\'dfe aus \u8364? Europa\par Zweite Zeile\tab mit Tab\par}")

HTML = (b"<html><head><title>T</title><style>p{color:red}</style><script>alert(1)</script></head><body>"
        b"<nav>Menu</nav><h1>Titel</h1><p>Erster &amp; <b>wichtiger</b> Absatz.</p><ul><li>Punkt A</li>"
        b"<li>Punkt B</li></ul><table><tr><td>x</td><td>y</td></tr></table></body></html>")
