"""read_attachment 工具：真实读入内容 + 诚实边界。

验证的是「读到的东西」而不是「卡片出现了」：文本按行分段、HTML 只取可见文本、
DOCX/XLSX 用标准库解析真实文件、图片只给元数据、PDF/二进制明确说不可读取。
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
from pathlib import Path

import pytest

from agent.services import attachments as attachments_mod
from agent.services.attachments import AttachmentService
from agent.tools.attachment_tools import ReadAttachmentTool


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _prepare(svc: AttachmentService, path: Path, *, topic_id: str = "t1", turn_id: str | None = None):
    att = svc.prepare(str(path), topic_id=topic_id, turn_id=turn_id)
    return svc.run_prepare(att.id)


def _tool(svc: AttachmentService, turn_id: str | None = "turn_1") -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: turn_id)


async def test_text_segments_are_real_and_paging_is_honest(svc: AttachmentService, tmp_path: Path):
    body = "\n".join(f"第{i}行内容" for i in range(1, 11))
    source = _write(tmp_path / "日志.log", body.encode("utf-8"))
    att = _prepare(svc, source)

    first = await _tool(svc).run(attachment_id=att.id, offset=0, limit=3)
    assert first.ok is True
    assert "第1行内容" in first.content and "第3行内容" in first.content
    assert "第4行内容" not in first.content
    assert '"total_units": 10' in first.content
    assert '"next_offset": 3' in first.content
    assert '"readable": true' in first.content
    assert "第3行内容" in first.content

    second = await _tool(svc).run(attachment_id=att.id, offset=3, limit=3)
    assert "第4行内容" in second.content and "第6行内容" in second.content
    assert "第3行内容" not in second.content

    tail = await _tool(svc).run(attachment_id=att.id, offset=8, limit=100)
    assert "第10行内容" in tail.content
    assert '"next_offset": null' in tail.content  # 已到末尾：不再给一个假的下一页

    beyond = await _tool(svc).run(attachment_id=att.id, offset=99, limit=10)
    assert beyond.ok is True
    assert '"returned": 0' in beyond.content
    assert "这一段是空的" in beyond.content


async def test_gbk_encoded_text_is_sniffed(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "国标.csv", "姓名,数量\n张三,3\n".encode("gbk"))
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=10)
    assert result.ok is True
    assert "张三" in result.content
    assert "gbk" in result.content


async def test_html_only_visible_text(svc: AttachmentService, tmp_path: Path):
    html = (
        "<html><head><title>标题</title><script>alert('不该出现')</script>"
        "<style>.x{color:red}</style></head>"
        "<body><h1>正文一</h1><p>正文二</p></body></html>"
    )
    source = _write(tmp_path / "page.html", html.encode("utf-8"))
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=10)
    assert result.ok is True
    assert "正文一" in result.content and "正文二" in result.content
    assert "不该出现" not in result.content  # 脚本不执行也不进正文
    assert '"title": "标题"' in result.content


async def test_docx_paragraphs_and_table(svc: AttachmentService, tmp_path: Path):
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>第一段正文</w:t></w:r></w:p>
    <w:p><w:r><w:t>第二段</w:t></w:r><w:r><w:t>连着写</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>单元格A</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>单元格B</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>"""
    path = tmp_path / "报告.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("[Content_Types].xml", "<Types/>")
    att = _prepare(svc, path)

    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=10)
    assert result.ok is True
    import json

    payload = json.loads(result.content)
    assert payload["read"]["unit"] == "段"
    assert payload["read"]["total_units"] == 3
    assert payload["content"].splitlines() == ["第一段正文", "第二段连着写", "单元格A	单元格B"]


async def test_xlsx_rows_with_shared_strings(svc: AttachmentService, tmp_path: Path):
    shared = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>名称</t></si><si><t>数量</t></si><si><t>螺丝</t></si>
</sst>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="库存" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
</Relationships>"""
    sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
    <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>42</v></c></row>
  </sheetData>
</worksheet>"""
    path = tmp_path / "库存.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    att = _prepare(svc, path)

    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=10)
    assert result.ok is True
    assert "库存" in result.content          # 工作表名
    assert "A1=名称" in result.content
    assert "B2=42" in result.content
    assert "螺丝" in result.content
    assert '"unit": "行"' in result.content


def _tiny_png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
    return signature + chunk + b"\x00\x00\x00\x00IEND\xaeB\x60\x82"


async def test_image_metadata_only_no_visual_claim(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "截图.png", _tiny_png(320, 240))
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is True
    assert '"readable": false' in result.content
    assert "320×240 像素" in result.content
    assert "没有视觉能力" in result.content
    assert "当前只能给元数据" in result.content


async def test_pdf_is_explicitly_unreadable(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "合同.pdf", b"%PDF-1.7\n" + b"\x00" * 200 + b"\n%%EOF")
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is True
    assert '"readable": false' in result.content
    assert "当前不可读取" in result.content
    assert "PDF 文本解析未实现" in result.content


async def test_binary_without_extension_is_unreadable(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "blob", bytes(range(256)) * 4)
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is True
    assert '"readable": false' in result.content
    assert "二进制" in result.content


async def test_unknown_extension_with_text_is_read(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "notes.custom", "自定义扩展名里的文本".encode("utf-8"))
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id, limit=5)
    assert result.ok is True
    assert "自定义扩展名里的文本" in result.content
    assert '"readable": true' in result.content


async def test_long_line_is_capped_and_reported(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "oneline.json", b"x" * 9000)
    att = _prepare(svc, source)
    result = await _tool(svc).run(attachment_id=att.id, limit=5)
    assert result.ok is True
    assert "本行已截断" in result.content
    assert '"truncated": true' in result.content


async def test_reference_reads_user_file_and_says_so(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(attachments_mod, "COPY_MAX_BYTES", 8)
    source = _write(tmp_path / "大表格.csv", b"a,b\n1,2\n3,4\n")
    att = _prepare(svc, source)
    assert att.kind == "reference"

    result = await _tool(svc).run(attachment_id=att.id, offset=1, limit=2)
    assert result.ok is True
    assert "1,2" in result.content and "3,4" in result.content
    assert "引用本地文件" in result.content
    assert "不保证内容仍然存在" in result.content
    assert "用户本地原文件" in result.content


async def test_missing_copy_falls_back_to_source_and_says_so(svc: AttachmentService, tmp_path: Path):
    """QIO 的副本丢了、原文件还在：读原文件，但必须说清楚读的是哪一个。"""
    source = _write(tmp_path / "gone.txt", "会被删掉".encode("utf-8"))
    att = _prepare(svc, source)
    Path(att.stored_path).unlink()
    assert svc.get(att.id).state == "missing"

    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is True
    assert "会被删掉" in result.content
    assert "副本已经不在了" in result.content
    assert "用户本地原文件" in result.content


async def test_missing_copy_and_missing_source_fail_honestly(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "gone2.txt", "都没了".encode("utf-8"))
    att = _prepare(svc, source)
    Path(att.stored_path).unlink()
    source.unlink()

    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is False
    assert "读不到附件" in (result.error or "")
    assert result.recoverable is True


async def test_prepared_attachment_cannot_be_read_yet(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "waiting.txt", b"wait")
    att = svc.prepare(str(source))  # 只登记，没准备
    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is False
    assert "还在准备中" in (result.error or "")


async def test_unknown_attachment_id_says_how_to_discover(svc: AttachmentService):
    result = await _tool(svc).run(attachment_id="att_nope")
    assert result.ok is False
    assert "没有这个附件 id" in (result.error or "")
    assert "不传 attachment_id" in (result.error or "")


async def test_listing_turn_attachments(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "a.txt", b"a")
    att = _prepare(svc, source, turn_id="turn_x")
    listing = await _tool(svc, "turn_x").run()
    assert listing.ok is True
    assert att.id in listing.content
    assert "a.txt" in listing.content
    assert '"count": 1' in listing.content

    other = await _tool(svc, "turn_other").run()
    assert other.ok is False
    assert "本轮没有附件" in (other.error or "")


async def test_listing_without_active_turn_is_honest(svc: AttachmentService):
    result = await _tool(svc, None).run()
    assert result.ok is False
    assert "没有正在执行的一轮" in (result.error or "")


async def test_read_never_modifies_the_source_file(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "read-only.txt", "不许改我\n".encode("utf-8"))
    before = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime)
    att = _prepare(svc, source)
    await _tool(svc).run(attachment_id=att.id)
    assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime) == before
    assert Path(att.stored_path).read_text("utf-8") == "不许改我\n"


async def test_large_file_skips_total_count_and_says_so(svc, tmp_path, monkeypatch):
    source = _write(tmp_path / "big.log", ("行\n" * 100).encode("utf-8"))
    att = _prepare(svc, source)
    from agent.tools import attachment_tools as tool_mod

    monkeypatch.setattr(tool_mod, "COUNT_TOTAL_MAX_BYTES", 10)
    result = await _tool(svc).run(attachment_id=att.id, offset=0, limit=3)
    assert result.ok is True
    assert '"total_units": null' in result.content
    assert "没有统计总行数" in result.content
    assert "行" in result.content
