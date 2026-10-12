"""卡片内预览的**真实样例**（契约 3.1 的格式矩阵逐条给真文件）。

这里不写"假文件占位"：每个样例都是**真的**——真 zip（含真 XML）、真 PNG 字节、
真 PDF 对象表与 xref、真代码文件。测试据此断言**具体内容**（某一段文字、A1 的值与公式、
某一页的备注），而不是"有个 dict 就行"。

只用标准库生成（zipfile + zlib + struct），所以生成器本身不引入任何依赖：
生成的 .docx 能被 Word 打开、.xlsx 能被 Excel 打开、PDF 能被 pypdf/pdf.js 读。
"""

from __future__ import annotations

import struct
import zlib
import zipfile
from pathlib import Path

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

#: 样例内容常量：测试断言用的字符串只在这里写一次
DOCX_HEADING = "季度报告"
DOCX_BOLD = "重点："
DOCX_ITALIC = "增长"
DOCX_TAIL = "与成本"
DOCX_ORDERED_ITEM = "第一条"
DOCX_BULLET_ITEM = "要点甲"
DOCX_TABLE_ROWS = [["项目", "金额"], ["收入", "120"]]
DOCX_LAST_PARAGRAPH = "结尾段落"

XLSX_SHEET_NAME = "销售"
XLSX_A1 = "季度"
XLSX_A2 = "收入"
XLSX_B1 = "12"
XLSX_B2_FORMULA = "SUM(B1:B1)"
XLSX_C1_INLINE = "备注文本"

PPTX_SLIDE1_TITLE = "产品路线"
PPTX_SLIDE1_TEXTS = ["第一步：调研", "第二步：原型"]
PPTX_SLIDE1_TABLE = [["阶段", "负责人"], ["调研", "小林"]]
PPTX_SLIDE1_NOTES = "备注内容：确认预算"
PPTX_SLIDE2_TITLE = "下一步"
PPTX_SLIDE2_TEXT = "评审"
PPTX_IMAGE_NAME = "image1.png"

PDF_TITLE = "QIO preview sample"
PDF_TEXT = "Hello QIO preview"
CODE_TEXT = (
    '"""样例：真实代码文件（缩进与行数都要保真）。"""\n'
    "\n"
    "\n"
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "if __name__ == \"__main__\":\n"
    "    print(add(1, 2))\n"
)


# ---------------------------------------------------------------------------
# PNG：真的 IHDR/IDAT/IEND（宽高来自头部，测试按真实字节断言）
# ---------------------------------------------------------------------------


def png_bytes(width: int = 8, height: int = 6, rgb: tuple[int, int, int] = (200, 120, 60)) -> bytes:
    """生成一张真实可解码的 PNG（真 zlib 压缩、真 CRC）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_png(path: Path, *, width: int = 8, height: int = 6) -> Path:
    path.write_bytes(png_bytes(width, height))
    return path


# ---------------------------------------------------------------------------
# DOCX：真 zip + 真 WordprocessingML（标题/加粗斜体/有序与无序列表/表格）
# ---------------------------------------------------------------------------


def _docx_document_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        f'<w:r><w:t>{DOCX_HEADING}</w:t></w:r></w:p>'
        '<w:p>'
        f'<w:r><w:rPr><w:b/></w:rPr><w:t>{DOCX_BOLD}</w:t></w:r>'
        f'<w:r><w:rPr><w:i/></w:rPr><w:t>{DOCX_ITALIC}</w:t></w:r>'
        f'<w:r><w:t>{DOCX_TAIL}</w:t></w:r>'
        '</w:p>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        f'<w:r><w:t>{DOCX_ORDERED_ITEM}</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr>'
        f'<w:r><w:t>{DOCX_BULLET_ITEM}</w:t></w:r></w:p>'
        '<w:tbl>'
        + "".join(
            "<w:tr>"
            + "".join(f'<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>' for cell in row)
            + "</w:tr>"
            for row in DOCX_TABLE_ROWS
        )
        + '</w:tbl>'
        f'<w:p><w:r><w:t>{DOCX_LAST_PARAGRAPH}</w:t></w:r></w:p>'
        '</w:body></w:document>'
    )


def _docx_numbering_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:numbering xmlns:w="{W_NS}">'
        '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0">'
        '<w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0">'
        '<w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
        '</w:numbering>'
    )


def write_docx(path: Path) -> Path:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{R_NS}/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", _docx_document_xml())
        archive.writestr("word/numbering.xml", _docx_numbering_xml())
    return path


# ---------------------------------------------------------------------------
# XLSX：真 zip + 真 SpreadsheetML（共享字符串 / 数字 / 公式 + 缓存值 / 内联字符串）
# ---------------------------------------------------------------------------


def write_xlsx(path: Path) -> Path:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{R_NS}/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>'
        f'<sheet name="{XLSX_SHEET_NAME}" sheetId="1" r:id="rId1"/>'
        '</sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{R_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<sst xmlns="{S_NS}" count="2" uniqueCount="2">'
        f'<si><t>{XLSX_A1}</t></si><si><t>{XLSX_A2}</t></si>'
        '</sst>'
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<worksheet xmlns="{S_NS}"><sheetData>'
        f'<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>{XLSX_B1}</v></c>'
        f'<c r="C1" t="inlineStr"><is><t>{XLSX_C1_INLINE}</t></is></c></row>'
        f'<row r="2"><c r="A2" t="s"><v>1</v></c>'
        f'<c r="B2"><f>{XLSX_B2_FORMULA}</f><v>12</v></c></row>'
        '</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


# ---------------------------------------------------------------------------
# PPTX：真 zip + 真 PresentationML（两页、标题/正文/表格/图片清单/备注页）
# ---------------------------------------------------------------------------


def _pptx_sp(placeholder_type: str, texts: list[str], idx: str = "") -> str:
    idx_attr = f' idx="{idx}"' if idx else ""
    paragraphs = "".join(
        f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p>' for text in texts
    )
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Shape"/>'
        f'<p:nvPr><p:ph type="{placeholder_type}"{idx_attr}/></p:nvPr></p:nvSpPr>'
        f'<p:txBody><a:bodyPr/>{paragraphs}</p:txBody></p:sp>'
    )


def _pptx_table(rows: list[list[str]]) -> str:
    body = "".join(
        "<a:tr>"
        + "".join(
            f'<a:tc><a:txBody><a:p><a:r><a:t>{cell}</a:t></a:r></a:p></a:txBody></a:tc>'
            for cell in row
        )
        + "</a:tr>"
        for row in rows
    )
    return (
        '<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3" name="Table"/>'
        '<p:nvPr/></p:nvGraphicFramePr><a:graphic><a:graphicData>'
        f'<a:tbl>{body}</a:tbl>'
        '</a:graphicData></a:graphic></p:graphicFrame>'
    )


def write_pptx(path: Path) -> Path:
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{R_NS}/officeDocument" Target="ppt/presentation.xml"/>'
        '</Relationships>'
    )
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<p:presentation xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">'
        '<p:sldIdLst>'
        '<p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/>'
        '</p:sldIdLst></p:presentation>'
    )
    presentation_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{R_NS}/slide" Target="slides/slide1.xml"/>'
        f'<Relationship Id="rId2" Type="{R_NS}/slide" Target="slides/slide2.xml"/>'
        '</Relationships>'
    )
    slide1 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}"><p:cSld><p:spTree>'
        + _pptx_sp("title", [PPTX_SLIDE1_TITLE])
        + _pptx_sp("body", PPTX_SLIDE1_TEXTS, idx="1")
        + _pptx_table(PPTX_SLIDE1_TABLE)
        + '<p:pic><p:nvPicPr><p:cNvPr id="4" name="Picture"/>'
        '<p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="rId3"/></p:blipFill></p:pic>'
        '</p:spTree></p:cSld></p:sld>'
    )
    slide1_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId3" Type="{R_NS}/image" Target="../media/{PPTX_IMAGE_NAME}"/>'
        f'<Relationship Id="rId4" Type="{R_NS}/notesSlide" Target="../notesSlides/notesSlide1.xml"/>'
        '</Relationships>'
    )
    slide2 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}"><p:cSld><p:spTree>'
        + _pptx_sp("title", [PPTX_SLIDE2_TITLE])
        + _pptx_sp("body", [PPTX_SLIDE2_TEXT], idx="1")
        + '</p:spTree></p:cSld></p:sld>'
    )
    notes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<p:notes xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}"><p:cSld><p:spTree>'
        + _pptx_sp("body", [PPTX_SLIDE1_NOTES])
        + '</p:spTree></p:cSld></p:notes>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", presentation_rels)
        archive.writestr("ppt/slides/slide1.xml", slide1)
        archive.writestr("ppt/slides/slide2.xml", slide2)
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", slide1_rels)
        archive.writestr("ppt/notesSlides/notesSlide1.xml", notes)
        archive.writestr(f"ppt/media/{PPTX_IMAGE_NAME}", png_bytes())
    return path


# ---------------------------------------------------------------------------
# PDF：真对象表 + 真 xref 偏移（能被 pypdf/pdf.js 读；标题进 /Info）
# ---------------------------------------------------------------------------


def pdf_bytes(*, title: str = PDF_TITLE, text: str = PDF_TEXT, pages: int = 1) -> bytes:
    """生成一份最小但**合法**的 PDF（正确的 xref 偏移与 trailer）。"""
    objects: list[bytes] = []
    page_ids = [3 + index for index in range(pages)]
    content_ids = [3 + pages + index for index in range(pages)]
    font_id = 3 + 2 * pages
    info_id = font_id + 1

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode("latin-1"))
    for index in range(pages):
        stream = f"BT /F1 18 Tf 20 60 Td ({text} {index + 1}) Tj ET".encode("latin-1")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 120] "
                f"/Contents {content_ids[index]} 0 R "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            ).encode("latin-1")
        )
    for index in range(pages):
        stream = f"BT /F1 18 Tf 20 60 Td ({text} {index + 1}) Tj ET".encode("latin-1")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    escaped = title.replace("(", "\\(").replace(")", "\\)")
    objects.append(f"<< /Title ({escaped}) /Producer (qio preview fixtures) >>".encode("latin-1"))

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R /Info {info_id} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def write_pdf(path: Path, **kwargs) -> Path:
    path.write_bytes(pdf_bytes(**kwargs))
    return path


# ---------------------------------------------------------------------------
# 代码文件 / 二进制垃圾 / 旧版 Office
# ---------------------------------------------------------------------------


def write_code(path: Path, text: str = CODE_TEXT) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def write_broken_zip(path: Path) -> Path:
    """一份"看起来像 Office"但根本不是 zip 的文件（损坏路径的样例）。"""
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    return path


def write_broken_pdf(path: Path) -> Path:
    """一份"看起来像 PDF"但结构坏掉的文件。"""
    path.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog\nendobj\ntrailer\n<< /Root 9 0 R >>\n%%EOF\n")
    return path


def write_legacy_doc(path: Path, payload: bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy") -> Path:
    path.write_bytes(payload)
    return path


# ---------------------------------------------------------------------------
# 一次性生成全部样例
# ---------------------------------------------------------------------------


def build_all(dest: Path) -> dict[str, Path]:
    """在 dest 下生成全部样例，返回 {键: 路径}（键名与测试断言一一对应）。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    return {
        "docx": write_docx(dest / "样例文档.docx"),
        "xlsx": write_xlsx(dest / "样例表格.xlsx"),
        "xlsm": write_xlsx(dest / "样例表格.xlsm"),
        "pptx": write_pptx(dest / "样例演示.pptx"),
        "pdf": write_pdf(dest / "样例文档.pdf"),
        "png": write_png(dest / "样例图片.png", width=8, height=6),
        "code": write_code(dest / "样例代码.py"),
        "broken_docx": write_broken_zip(dest / "损坏文档.docx"),
        "broken_pdf": write_broken_pdf(dest / "损坏文档.pdf"),
        "legacy_doc": write_legacy_doc(dest / "旧版文档.doc"),
    }
