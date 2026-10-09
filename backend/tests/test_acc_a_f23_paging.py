"""A 组反例：F23 字符截断后分页声称返回更多行，后续无法取回被丢内容。

契约来源：docs/plans/2026-10-09-process-attachment-audit-consolidation.md C7。
基线现状：returned=截断前的行数，next_offset=offset+returned，正文却被 _cap_chars(40 KB)
从中间切断 —— 元数据说的行数与实际交付不符，按 next_offset 继续会**漏掉**被切断的尾部。

反例（基线应当红）：
* 40 行 × 3000 字符：returned 声称 40 行，正文只覆盖约 14 行；
* 按 next_offset 继续读取，重建的内容与原文不一致（漏段 / 重复）；
* 超长单行没有行内片段游标，尾部永远取不回。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from agent.services.attachments import AttachmentService
from agent.tools import attachment_tools as tool_mod
from agent.tools.attachment_tools import ReadAttachmentTool

MAX_CHARS = tool_mod.MAX_CHARS
MAX_LINE_CHARS = tool_mod.MAX_LINE_CHARS


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _tool(svc: AttachmentService) -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: "t")


def _prepare(svc: AttachmentService, path: Path):
    att = svc.prepare(str(path))
    return svc.run_prepare(att.id)


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="")
    return path


async def _reconstruct(svc: AttachmentService, att, *, unit_limit: int = 5, max_pages: int = 400):
    offset = 0
    frag = 0
    acc: list[str] = []
    total = None
    pages = 0
    while pages < max_pages:
        pages += 1
        res = await _tool(svc).run(
            attachment_id=att.id, offset=offset, limit=unit_limit, fragment_offset=frag
        )
        assert res.ok is True, res.error
        payload = json.loads(res.content)
        read = payload["read"]
        if total is None:
            total = read["total_units"]
        content = payload["content"]
        pieces = content.split("\n") if content != "" else []
        if frag > 0 and acc:
            if pieces:
                acc[-1] += pieces[0]
                acc.extend(pieces[1:])
        else:
            acc.extend(pieces)
        if read["next_offset"] is None:
            return "\n".join(acc), total, pages
        offset, frag = read["next_offset"], read.get("next_fragment_offset", 0)
    raise AssertionError("分页没有收敛")


# ---- 文本 --------------------------------------------------------------------------


async def test_returned_matches_actually_delivered_units(svc: AttachmentService, tmp_path: Path):
    lines = [("%03d" % i) + "x" * 3000 for i in range(40)]
    source = _write(tmp_path / "wide.txt", "\n".join(lines))
    att = _prepare(svc, source)

    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=40)
    assert result.ok is True, result.error
    payload = json.loads(result.content)
    read = payload["read"]
    content = payload["content"]
    delivered = content.split("\n") if content != "" else []
    assert len(content) <= MAX_CHARS
    assert read["returned"] == len(delivered), (
        "returned 必须等于正文里实际出现的单元数（不能按截断前的行数虚报）",
        read["returned"],
        len(delivered),
    )
    assert read["next_offset"] is not None, "还有内容没交付，必须给出可继续的位置"
    assert "\ufffd" not in content


async def test_multi_page_reconstruction_text(svc: AttachmentService, tmp_path: Path):
    lines = [("line-%02d-" % i) + "y" * 3000 for i in range(30)]
    source = _write(tmp_path / "many_wide.txt", "\n".join(lines))
    att = _prepare(svc, source)
    rebuilt, total, pages = await _reconstruct(svc, att, unit_limit=5)
    assert rebuilt == "\n".join(lines), ("多页重建必须与原文一致（不漏段、不重复）", pages)
    assert total == 30
    assert pages >= 2


async def test_fragment_cursor_reconstructs_single_long_line(svc: AttachmentService, tmp_path: Path):
    body = "abcdefghij" * 900  # 9000 字符单行
    source = _write(tmp_path / "oneline.txt", body)
    att = _prepare(svc, source)
    first = await _tool(svc).run(attachment_id=att.id, offset=0, limit=1)
    payload = json.loads(first.content)
    read = payload["read"]
    assert read["next_offset"] == 0
    assert read["next_fragment_offset"] == MAX_LINE_CHARS
    assert read["partial"] is True
    assert len(payload["content"]) == MAX_LINE_CHARS

    rebuilt, total, _pages = await _reconstruct(svc, att, unit_limit=1)
    assert rebuilt == body
    assert total == 1


async def test_exact_line_boundary_is_complete(svc: AttachmentService, tmp_path: Path):
    body = ("z" * MAX_LINE_CHARS) + "\nnext\n"
    source = _write(tmp_path / "exact.txt", body)
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=1)
    payload = json.loads(result.content)
    read = payload["read"]
    assert read["partial"] is False
    assert read["returned"] == 1
    assert len(payload["content"]) == MAX_LINE_CHARS
    assert read["next_offset"] == 1


async def test_empty_lines_crlf_and_last_page(svc: AttachmentService, tmp_path: Path):
    lines = ["甲", "", "乙", "丙"]
    source = _write(tmp_path / "crlf_empty.txt", "\r\n".join(lines) + "\r\n")
    att = _prepare(svc, source)
    rebuilt, total, _pages = await _reconstruct(svc, att, unit_limit=2)
    assert rebuilt == "\r\n".join(lines) + "\r\n" or rebuilt == "\n".join(lines)
    assert total == 4

    beyond = await _tool(svc).run(attachment_id=att.id, offset=99, limit=5)
    payload = json.loads(beyond.content)
    assert payload["read"]["returned"] == 0
    assert payload["read"]["next_offset"] is None


# ---- DOCX / XLSX / HTML ------------------------------------------------------------


def _make_docx(path: Path, paragraphs: list[str]) -> Path:
    body = "".join(
        "<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p for p in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>%s</w:body></w:document>" % body
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return path


def _make_xlsx(path: Path, rows: list[list[str]]) -> Path:
    sheet_rows = []
    for index, cells in enumerate(rows, start=1):
        row = "".join(
            '<c r="%s%d"><v>%s</v></c>' % (chr(65 + col), index, value)
            for col, value in enumerate(cells)
        )
        sheet_rows.append('<row r="%d">%s</row>' % (index, row))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>%s</sheetData></worksheet>" % "".join(sheet_rows)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


async def test_docx_paging_reconstructs_all_paragraphs(svc: AttachmentService, tmp_path: Path):
    paragraphs = [("段%02d " % i) + "p" * 2500 for i in range(8)]
    source = _make_docx(tmp_path / "wide.docx", paragraphs)
    att = _prepare(svc, source)
    rebuilt, total, pages = await _reconstruct(svc, att, unit_limit=3)
    assert rebuilt == "\n".join(paragraphs), pages
    assert total == 8
    assert pages >= 2


async def test_xlsx_paging_reaches_every_row_without_loss(svc: AttachmentService, tmp_path: Path):
    source = _make_xlsx(
        tmp_path / "many.xlsx",
        [["r%03d-%d" % (row, col) for col in range(3)] for row in range(120)],
    )
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=10)
    payload = json.loads(result.content)
    assert payload["read"]["returned"] == len(payload["content"].split("\n"))
    assert payload["read"]["total_units"] == 121  # 1 个表头 + 120 行

    offset = 0
    seen: list[str] = []
    while True:
        page = await _tool(svc).run(attachment_id=att.id, offset=offset, limit=25)
        body = json.loads(page.content)
        seen.extend(body["content"].split("\n"))
        if body["read"]["next_offset"] is None:
            break
        offset = body["read"]["next_offset"]
    assert len(seen) == 121
    assert seen[0].startswith("—— 工作表：")
    assert seen[1].startswith("r1: ")
    assert seen[-1].startswith("r120: ")


async def test_html_paging_reconstructs_paragraphs(svc: AttachmentService, tmp_path: Path):
    paragraphs = [("段落%02d " % i) + "h" * 2500 for i in range(6)]
    html = "<html><body>" + "".join("<p>%s</p>" % p for p in paragraphs) + "</body></html>"
    source = tmp_path / "many.html"
    source.write_text(html, encoding="utf-8")
    att = _prepare(svc, source)
    rebuilt, total, pages = await _reconstruct(svc, att, unit_limit=2)
    assert rebuilt == "\n".join(paragraphs), pages
    assert total == 6
    assert pages >= 2
