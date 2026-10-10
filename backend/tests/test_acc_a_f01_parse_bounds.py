"""A 组反例：F01 DOCX/XLSX 只限制压缩文件大小，解压/解析仍可大量分配内存。

契约来源：docs/plans/2026-10-09-process-attachment-audit-consolidation.md C7。
基线现状：PARSE_MAX_BYTES 只看**压缩文件大小**；_docx_lines / _xlsx_lines 用
ElementTree.parse 整份构建 XML 树、共享字符串整表进内存。约 30 KB 的 DOCX 可以
含约 30 MB XML，一次读取就分配数十 MB；输出截断限制不了解析前的展开与对象分配。

反例（基线应当红）：
* 小压缩文件含大 XML → 基线「读取成功」，峰值与展开大小同量级；
* 多成员累计超限 / 共享字符串过大 / 成员数量过多 → 必须有明确限制原因；
* 损坏归档受控退出；正常 DOCX / XLSX 仍可读。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from agent.services.attachments import AttachmentService
from agent.tools import attachment_tools as tool_mod
from agent.tools.attachment_tools import ReadAttachmentTool

from _acc_a_probe import run_probe

MB = 1_000_000

DOCX_HEADER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
)
DOCX_FOOTER = "</w:body></w:document>"
PARA_CHUNK = b"<w:p><w:r><w:t>abcdefghijklmnopqrstuvwxyz</w:t></w:r></w:p>" * 2000


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _tool(svc: AttachmentService) -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: "t")


def _prepare(svc: AttachmentService, path: Path):
    att = svc.prepare(str(path))
    return svc.run_prepare(att.id)


def _make_docx_bomb(path: Path, target_bytes: int) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        with archive.open("word/document.xml", "w") as handle:
            handle.write(DOCX_HEADER.encode("utf-8"))
            written = 0
            while written < target_bytes:
                handle.write(PARA_CHUNK)
                written += len(PARA_CHUNK)
            handle.write(DOCX_FOOTER.encode("utf-8"))
    return path


def _make_docx(path: Path, paragraphs: list[str]) -> Path:
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p for p in paragraphs)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", DOCX_HEADER + body + DOCX_FOOTER)
    return path


def _make_xlsx(path: Path, *, shared: bytes | None = None, sheets: dict[str, bytes] | None = None) -> Path:
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
    default_sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        if shared is not None:
            archive.writestr("xl/sharedStrings.xml", shared)
        for name, data in (sheets or {"xl/worksheets/sheet1.xml": default_sheet.encode("utf-8")}).items():
            archive.writestr(name, data)
    return path


def _payload(result) -> dict:
    assert result.ok is True, result.error
    return json.loads(result.content)


# ---- 1. 小压缩文件大展开：受控退出，且峰值与展开大小脱钩（子进程探针） ----------


def test_docx_small_zip_large_xml_is_controlled(tmp_path: Path):
    source = _make_docx_bomb(tmp_path / "bomb.docx", 48 * MB)
    assert source.stat().st_size < 1 * MB, "装置前提：压缩后很小"
    result = run_probe(tmp_path, source, "bomb.docx", offset=0, limit=5)
    assert result["ok"] is True, result["error"]
    payload = json.loads(result["content"])
    assert payload["readable"] is False, "大展开不能伪装成完整读取成功"
    assert "当前不可读取" in payload["reason"]
    assert "超过" in payload["reason"] or "上限" in payload["reason"]
    assert result["peak"] < 96 * MB, ("展开上限必须真的限制分配峰值", result["peak"])


# ---- 2. 成员数量 / 累计展开 / 共享字符串 -------------------------------------------------


async def test_member_count_limit(svc: AttachmentService, tmp_path: Path):
    source = _make_docx(tmp_path / "many_members.docx", ["正文"])
    with zipfile.ZipFile(source, "a") as archive:
        for index in range(tool_mod.ZIP_MAX_MEMBERS + 5):
            archive.writestr("word/media/f%04d.bin" % index, b"x")
    att = _prepare(svc, source)
    payload = _payload(await _tool(svc).run(attachment_id=att.id, limit=3))
    assert payload["readable"] is False
    assert "成员" in payload["reason"]


async def test_cumulative_expansion_limit(svc: AttachmentService, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tool_mod, "ZIP_TOTAL_MAX_BYTES", 4_000)
    monkeypatch.setattr(tool_mod, "ZIP_MEMBER_MAX_BYTES", 3_000)
    shared = (
        '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join("<si><t>%s</t></si>" % ("s" * 50) for _ in range(40))
        + "</sst>"
    ).encode("utf-8")
    sheet = (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join('<row r="%d"><c r="A%d" t="s"><v>0</v></c></row>' % (i, i) for i in range(1, 60))
        + "</sheetData></worksheet>"
    ).encode("utf-8")
    source = _make_xlsx(tmp_path / "cumulative.xlsx", shared=shared, sheets={"xl/worksheets/sheet1.xml": sheet})
    att = _prepare(svc, source)
    payload = _payload(await _tool(svc).run(attachment_id=att.id, limit=3))
    assert payload["readable"] is False
    assert "累计" in payload["reason"] or "上限" in payload["reason"]
    assert "实际读取" in payload["reason"], "必须是按实际读出的字节计数，不是 ZIP 声明大小"


async def test_shared_strings_too_large(svc: AttachmentService, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tool_mod, "ZIP_MEMBER_MAX_BYTES", 2_000)
    shared = (
        '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join("<si><t>%s</t></si>" % ("t" * 200) for _ in range(40))
        + "</sst>"
    ).encode("utf-8")
    source = _make_xlsx(tmp_path / "shared.xlsx", shared=shared)
    att = _prepare(svc, source)
    payload = _payload(await _tool(svc).run(attachment_id=att.id, limit=3))
    assert payload["readable"] is False
    assert "sharedStrings" in payload["reason"] or "单成员" in payload["reason"]
    assert "实际读取" in payload["reason"]


# ---- 3. 损坏归档与正常文件 ---------------------------------------------------------------


async def test_corrupt_archive_is_controlled(svc: AttachmentService, tmp_path: Path):
    source = tmp_path / "broken.docx"
    source.write_bytes(b"this is not a zip file at all" * 100)
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id, limit=3)
    assert result.ok is False
    assert result.category == "invalid_data"
    assert "损坏" in (result.error or "") or "不是有效的" in (result.error or "")


async def test_normal_docx_and_xlsx_are_still_readable(svc: AttachmentService, tmp_path: Path):
    docx = _make_docx(tmp_path / "ok.docx", ["第一段", "第二段"])
    docx_att = _prepare(svc, docx)
    payload = _payload(await _tool(svc).run(attachment_id=docx_att.id, limit=5))
    assert payload["readable"] is True
    assert "第一段" in payload["content"] and "第二段" in payload["content"]

    xlsx = _make_xlsx(tmp_path / "ok.xlsx")
    xlsx_att = _prepare(svc, xlsx)
    payload = _payload(await _tool(svc).run(attachment_id=xlsx_att.id, limit=5))
    assert payload["readable"] is True
    assert "数据" in payload["content"]
