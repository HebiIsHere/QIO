"""卡片内预览 · 转换层（services/preview.py）定向测试。

范围（契约 §3.2/§3.4）：
* 分类矩阵与冻结常量；
* 真实样例的**具体内容**断言（DOCX 的某一段文字与表格单元格、XLSX 的 A1 值与 B2 公式、
  PPTX 的标题/正文/表格/图片清单/备注、PDF 的页数/标题/加密/可提取文本量、PNG 的真实宽高、
  代码文件的行与语言）；
* 截断 / 损坏 / 加密 / 旧版 Office / 未支持各自的 status 与可读 reason（**绝不抛 500**）；
* 缓存按 (attachment_id, version, recipe) 归属，版本变了重算、旧版本结果绝不写到新版本；
* 取消与「迟到结果不写缓存」。

样例全部由 fixtures.preview 用**真 zip + 真 XML + 真 PDF 对象表 + 真 PNG 字节**现场生成，
不是假文件占位。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent.interactive import materials
from agent.services import preview
from agent.services.attachments import AttachmentService
from agent.storage.db import transaction
from fixtures.preview import (
    CODE_TEXT,
    DOCX_BOLD,
    DOCX_BULLET_ITEM,
    DOCX_HEADING,
    DOCX_ITALIC,
    DOCX_LAST_PARAGRAPH,
    DOCX_ORDERED_ITEM,
    DOCX_TABLE_ROWS,
    PDF_TEXT,
    PDF_TITLE,
    PPTX_IMAGE_NAME,
    PPTX_SLIDE1_NOTES,
    PPTX_SLIDE1_TABLE,
    PPTX_SLIDE1_TEXTS,
    PPTX_SLIDE1_TITLE,
    PPTX_SLIDE2_TEXT,
    PPTX_SLIDE2_TITLE,
    XLSX_A1,
    XLSX_A2,
    XLSX_B1,
    XLSX_B2_FORMULA,
    XLSX_C1_INLINE,
    XLSX_SHEET_NAME,
    build_all,
    png_bytes,
)


@pytest.fixture()
def samples(tmp_path: Path) -> dict[str, Path]:
    return build_all(tmp_path / "samples")


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _attach(svc: AttachmentService, path: Path, name: str | None = None):
    """把一个真实样例登记成 ready 的 QIO 副本（走真实的上传链路）。"""
    return svc.register_upload(Path(path).read_bytes(), name=name or Path(path).name)


def _render(svc: AttachmentService, path: Path, name: str | None = None, **kwargs) -> dict:
    att = _attach(svc, path, name)
    return preview.render_for(svc, att.id, **kwargs)


# ---------------------------------------------------------------------------
# 冻结常量与分类
# ---------------------------------------------------------------------------


def test_frozen_constants_match_the_contract():
    assert preview.RECIPE_VERSION == 1
    assert preview.MAX_PREVIEW_BYTES == 64 * 1024 * 1024
    assert preview.MAX_PREVIEW_UNITS == 4000
    assert preview.MAX_ROWS_PER_SHEET == 500
    assert preview.MAX_COLS_PER_SHEET == 60


@pytest.mark.parametrize(
    "name,expected",
    [
        ("照片.png", "image"),
        ("照片.JPG", "image"),
        ("扫描.gif", "image"),
        ("说明.pdf", "pdf"),
        ("报告.docx", "docx"),
        ("表格.xlsx", "xlsx"),
        ("表格.xlsm", "xlsx"),
        ("演示.pptx", "pptx"),
        ("脚本.py", "code"),
        ("说明.md", "code"),
        ("数据.csv", "code"),
        ("页面.html", "code"),
        ("配置.xml", "code"),
        ("图标.svg", "code"),
        ("旧文档.doc", "legacy_office"),
        ("旧表格.xls", "legacy_office"),
        ("旧演示.ppt", "legacy_office"),
        ("归档.zip", "unsupported"),
        ("程序.exe", "unsupported"),
        ("没有扩展名", "unsupported"),
    ],
)
def test_classify_preview_matrix(name: str, expected: str):
    assert preview.classify_preview(name) == expected


def test_html_and_xml_are_text_not_inline_html():
    """契约 §3.1 ①：html/xml/svg 不内联（会执行脚本/外链），走「转换层返回文本」。"""
    for name in ("a.html", "b.xml", "c.svg"):
        assert preview.classify_preview(name) == "code"
        assert preview.language_for(name) == "xml"


def test_preview_cache_key_is_stable_and_sensitive():
    key = preview.preview_cache_key("att_1", "sha256:aa", "docx")
    assert key == preview.preview_cache_key("att_1", "sha256:aa", "docx")
    assert f"r{preview.RECIPE_VERSION}" in key  # 配方版本进键
    assert key != preview.preview_cache_key("att_1", "sha256:bb", "docx")
    assert key != preview.preview_cache_key("att_2", "sha256:aa", "docx")
    assert key != preview.preview_cache_key("att_1", "sha256:aa", "xlsx")


def test_attachment_version_matches_the_material_version(svc: AttachmentService, samples):
    """预览位置按 attachmentId+version 归属，必须与提交/审批用的材料版本同一口径。"""
    att = _attach(svc, samples["docx"])
    assert preview.attachment_version(att) == materials._attachment_version(att)
    assert preview.attachment_version(att).startswith("sha256:")

    bare = _attach(svc, samples["code"])
    with transaction(svc.conn):
        svc.conn.execute("UPDATE attachments SET sha256 = NULL WHERE id = ?", (bare.id,))
    current = svc.get(bare.id, check=False)
    assert current is not None
    assert preview.attachment_version(current) == materials._attachment_version(current)


# ---------------------------------------------------------------------------
# DOCX：真 XML 的具体内容
# ---------------------------------------------------------------------------


def test_docx_real_content(svc: AttachmentService, samples):
    att = _attach(svc, samples["docx"])
    data = preview.convert(att, samples["docx"])
    assert data["kind"] == "docx"
    assert data["truncated"] is False

    types = [block["type"] for block in data["blocks"]]
    assert types[0] == "heading"
    heading = data["blocks"][0]
    assert heading["level"] == 1
    assert "".join(run["text"] for run in heading["runs"]) == DOCX_HEADING

    paragraph = data["blocks"][1]
    assert paragraph["type"] == "paragraph"
    runs = {run["text"]: run for run in paragraph["runs"]}
    assert runs[DOCX_BOLD]["bold"] is True
    assert runs[DOCX_BOLD]["italic"] is False
    assert runs[DOCX_ITALIC]["italic"] is True
    assert runs[DOCX_ITALIC]["bold"] is False

    ordered = [b for b in data["blocks"] if b["type"] == "list" and b["ordered"] is True]
    bullets = [b for b in data["blocks"] if b["type"] == "list" and b["ordered"] is False]
    assert "".join(r["text"] for r in ordered[0]["runs"]) == DOCX_ORDERED_ITEM
    assert "".join(r["text"] for r in bullets[0]["runs"]) == DOCX_BULLET_ITEM

    assert len(data["tables"]) == 1
    assert data["tables"][0]["rows"] == DOCX_TABLE_ROWS
    assert data["blocks"][-1]["type"] == "paragraph"
    assert "".join(r["text"] for r in data["blocks"][-1]["runs"]) == DOCX_LAST_PARAGRAPH


def test_docx_goes_through_the_render_for_envelope(svc: AttachmentService, samples):
    env = _render(svc, samples["docx"])
    assert env["status"] == "ready"
    assert env["kind"] == "docx"
    assert env["recipe"] == preview.RECIPE_VERSION
    assert env["data"]["kind"] == "docx"
    assert env["stats"]["units"] > 0
    assert env["stats"]["bytes"] == samples["docx"].stat().st_size


# ---------------------------------------------------------------------------
# XLSX：单元格地址 / 值 / 公式 / 缓存值 / 内联字符串
# ---------------------------------------------------------------------------


def _cells(data: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sheet in data["sheets"]:
        for row in sheet["rows"]:
            for cell in row["cells"]:
                out[cell["ref"]] = cell
    return out


def test_xlsx_real_content(svc: AttachmentService, samples):
    att = _attach(svc, samples["xlsx"])
    data = preview.convert(att, samples["xlsx"])
    assert data["kind"] == "xlsx"
    assert [sheet["name"] for sheet in data["sheets"]] == [XLSX_SHEET_NAME]
    assert [row["index"] for row in data["sheets"][0]["rows"]] == [1, 2]

    cells = _cells(data)
    # A1：共享字符串
    assert cells["A1"]["value"] == XLSX_A1
    assert cells["A1"]["formula"] is None
    assert cells["A1"]["cached"] is None
    # B1：普通数字
    assert cells["B1"]["value"] == XLSX_B1
    # C1：内联字符串
    assert cells["C1"]["value"] == XLSX_C1_INLINE
    # B2：公式与缓存值**分开**（不计算公式）
    assert cells["B2"]["formula"] == XLSX_B2_FORMULA
    assert cells["B2"]["cached"] == XLSX_B1
    assert cells["B2"]["value"] is None
    assert cells["A2"]["value"] == XLSX_A2


def test_xlsm_uses_the_same_converter(svc: AttachmentService, samples):
    att = _attach(svc, samples["xlsm"])
    data = preview.convert(att, samples["xlsm"])
    assert data["kind"] == "xlsx"
    assert data["sheets"][0]["name"] == XLSX_SHEET_NAME


# ---------------------------------------------------------------------------
# PPTX：标题 / 正文 / 表格 / 图片清单 / 备注
# ---------------------------------------------------------------------------


def test_pptx_real_content(svc: AttachmentService, samples):
    att = _attach(svc, samples["pptx"])
    data = preview.convert(att, samples["pptx"])
    assert data["kind"] == "pptx"
    assert [slide["index"] for slide in data["slides"]] == [1, 2]

    first = data["slides"][0]
    assert first["title"] == PPTX_SLIDE1_TITLE
    for text in PPTX_SLIDE1_TEXTS:
        assert text in first["texts"]
    assert f"{preview._NOTES_PREFIX}{PPTX_SLIDE1_NOTES}" in first["texts"]
    assert first["tables"] == [PPTX_SLIDE1_TABLE]
    # 表格文字只出现一次：进了 tables 就不在 texts 里重复一遍
    assert "阶段" not in first["texts"]
    assert [image["name"] for image in first["images"]] == [PPTX_IMAGE_NAME]
    assert first["images"][0]["bytes"] == len(png_bytes())

    second = data["slides"][1]
    assert second["title"] == PPTX_SLIDE2_TITLE
    assert PPTX_SLIDE2_TEXT in second["texts"]


# ---------------------------------------------------------------------------
# PDF：只给事实
# ---------------------------------------------------------------------------


def test_pdf_real_facts(svc: AttachmentService, samples):
    att = _attach(svc, samples["pdf"])
    data = preview.convert(att, samples["pdf"])
    assert data["kind"] == "pdf"
    assert data["pages"] == 1
    assert data["title"] == PDF_TITLE
    assert data["encrypted"] is False
    assert data["textChars"] > 0
    assert data["textProbedPages"] == 1


def test_pdf_multi_page_count(svc: AttachmentService, tmp_path: Path):
    from fixtures.preview import write_pdf

    path = write_pdf(tmp_path / "三页.pdf", pages=3)
    att = _attach(svc, path)
    data = preview.convert(att, path)
    assert data["pages"] == 3
    assert data["textChars"] > len(PDF_TEXT)


def test_encrypted_pdf_is_unsupported_with_a_clear_reason(svc: AttachmentService, tmp_path: Path):
    from pypdf import PdfWriter

    path = tmp_path / "加密.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.encrypt("secret")
    with open(path, "wb") as handle:
        writer.write(handle)

    att = _attach(svc, path)
    env = preview.render_for(svc, att.id)
    assert env["status"] == "unsupported"
    assert env["data"]["encrypted"] is True
    assert env["data"]["pages"] is None
    assert "密码" in env["reason"]


def test_broken_pdf_is_failed_not_500(svc: AttachmentService, samples):
    env = _render(svc, samples["broken_pdf"])
    assert env["status"] == "failed"
    assert env["data"] is None
    assert "PDF" in env["reason"]
    assert env["truncated"] is False


# ---------------------------------------------------------------------------
# 代码 / 文本
# ---------------------------------------------------------------------------


def test_code_real_content(svc: AttachmentService, samples):
    att = _attach(svc, samples["code"])
    data = preview.convert(att, samples["code"])
    assert data["kind"] == "code"
    assert data["language"] == "python"
    assert data["encoding"] == "utf-8"
    assert data["lines"] == CODE_TEXT.splitlines()
    assert data["totalLines"] == len(CODE_TEXT.splitlines())
    assert data["truncated"] is False
    # 缩进保真（契约 §3.1 的「保留缩进」）
    assert data["lines"][4] == "    return a + b"


def test_gbk_text_is_decoded_and_declared(svc: AttachmentService, tmp_path: Path):
    path = tmp_path / "中文日志.log"
    path.write_bytes("第一行\n第二行\n".encode("gbk"))
    att = _attach(svc, path)
    data = preview.convert(att, path)
    assert data["language"] == "text"
    assert data["encoding"] == "gbk"
    assert data["lines"] == ["第一行", "第二行"]


def test_binary_bytes_with_text_suffix_are_refused_honestly(svc: AttachmentService, tmp_path: Path):
    path = tmp_path / "伪装.txt"
    path.write_bytes(b"\x00\x01\x02binary")
    att = _attach(svc, path)
    env = preview.render_for(svc, att.id)
    assert env["status"] == "failed"
    assert "二进制" in env["reason"]


# ---------------------------------------------------------------------------
# 图片
# ---------------------------------------------------------------------------


def test_image_dimensions_come_from_real_bytes(svc: AttachmentService, samples):
    att = _attach(svc, samples["png"])
    data = preview.convert(att, samples["png"])
    assert data == {
        "kind": "image",
        "width": 8,
        "height": 6,
        "bytes": len(png_bytes()),
    }


# ---------------------------------------------------------------------------
# 旧版 Office / 未支持：不同 status 与可读 reason
# ---------------------------------------------------------------------------


def test_legacy_office_is_unsupported_with_its_own_reason(svc: AttachmentService, samples):
    env = _render(svc, samples["legacy_doc"])
    assert env["status"] == "unsupported"
    assert env["kind"] == "legacy_office"
    assert "旧版" in env["reason"]
    assert "系统程序" in env["reason"]


def test_unsupported_extensions_say_why(svc: AttachmentService, tmp_path: Path):
    path = tmp_path / "归档.zip"
    path.write_bytes(b"PK\x03\x04zip")
    env = _render(svc, path)
    assert env["status"] == "unsupported"
    assert env["kind"] == "unsupported"
    assert "压缩包" in env["reason"]


def test_missing_name_without_extension_is_unsupported(svc: AttachmentService, tmp_path: Path):
    path = tmp_path / "README"
    path.write_text("some text", encoding="utf-8")
    env = _render(svc, path)
    assert env["status"] == "unsupported"
    assert "扩展名" in env["reason"]


# ---------------------------------------------------------------------------
# 损坏 / 超限 / 截断
# ---------------------------------------------------------------------------


def test_broken_docx_reports_damage(svc: AttachmentService, samples):
    env = _render(svc, samples["broken_docx"])
    assert env["status"] == "failed"
    assert "损坏" in env["reason"] or "不是有效" in env["reason"]


def test_byte_limit_is_reported_without_converting(svc: AttachmentService, samples, monkeypatch):
    monkeypatch.setattr(preview, "MAX_PREVIEW_BYTES", 16)
    env = _render(svc, samples["docx"])
    assert env["status"] == "failed"
    assert "超过预览转换上限" in env["reason"]


def test_code_truncates_at_the_unit_limit(svc: AttachmentService, samples, monkeypatch):
    monkeypatch.setattr(preview, "MAX_PREVIEW_UNITS", 3)
    att = _attach(svc, samples["code"])
    data = preview.convert(att, samples["code"])
    assert len(data["lines"]) == 3
    assert data["totalLines"] == len(CODE_TEXT.splitlines())
    assert data["truncated"] is True


def test_code_truncates_overlong_lines(svc: AttachmentService, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(preview, "MAX_LINE_CHARS", 5)
    path = tmp_path / "长行.txt"
    path.write_text("0123456789\nshort\n", encoding="utf-8")
    att = _attach(svc, path)
    data = preview.convert(att, path)
    assert data["lines"] == ["01234", "short"]
    assert data["truncated"] is True


def test_xlsx_truncates_at_the_row_cap(svc: AttachmentService, samples, monkeypatch):
    monkeypatch.setattr(preview, "MAX_ROWS_PER_SHEET", 1)
    att = _attach(svc, samples["xlsx"])
    data = preview.convert(att, samples["xlsx"])
    assert len(data["sheets"][0]["rows"]) == 1
    assert data["truncated"] is True


def test_xlsx_truncates_at_the_column_cap(svc: AttachmentService, samples, monkeypatch):
    monkeypatch.setattr(preview, "MAX_COLS_PER_SHEET", 2)
    att = _attach(svc, samples["xlsx"])
    data = preview.convert(att, samples["xlsx"])
    refs = [cell["ref"] for cell in data["sheets"][0]["rows"][0]["cells"]]
    assert refs == ["A1", "B1"]
    assert data["truncated"] is True


def test_zip_budget_is_the_shared_one_from_attachment_tools(svc: AttachmentService, samples, monkeypatch):
    """预算与取消语义直接沿用 tools/attachment_tools.py 的实现（契约 §3.2 硬约束）。"""
    from agent.tools import attachment_tools

    monkeypatch.setattr(attachment_tools, "ZIP_MEMBER_MAX_BYTES", 32)
    att = _attach(svc, samples["docx"])
    env = preview.render_for(svc, att.id)
    assert env["status"] == "failed"
    assert "预算" in env["reason"]


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_cancel_before_conversion_stops_it_and_caches_nothing(svc: AttachmentService, samples):
    att = _attach(svc, samples["docx"])
    cancel = threading.Event()
    cancel.set()
    env = preview.render_for(svc, att.id, cancel=cancel)
    assert env["status"] == "failed"
    assert "取消" in env["reason"]
    assert _cache_rows(svc) == []


def test_late_result_is_not_cached_when_cancelled_midway(svc: AttachmentService, samples, monkeypatch):
    """转换跑完才发现被取消：结果可以返回，但**绝不写缓存**。"""
    original = preview.convert
    cancel = threading.Event()

    def _convert_then_cancel(att, path, *, cancel=None):  # noqa: ANN001
        data = original(att, path, cancel=cancel)
        cancel.set()
        return data

    monkeypatch.setattr(preview, "convert", _convert_then_cancel)
    att = _attach(svc, samples["docx"])
    env = preview.render_for(svc, att.id, cancel=cancel)
    assert env["data"] is not None
    assert "取消" in env["reason"]
    assert _cache_rows(svc) == []


# ---------------------------------------------------------------------------
# 缓存与版本
# ---------------------------------------------------------------------------


def _cache_rows(svc: AttachmentService) -> list[dict]:
    rows = svc.conn.execute(
        "SELECT attachment_id, version, kind, recipe, status, truncated, payload, bytes"
        " FROM attachment_previews ORDER BY version"
    ).fetchall()
    result = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["payload"] = json.loads(item["payload"])
        result.append(item)
    return result


def test_cache_hit_and_refresh(svc: AttachmentService, samples):
    att = _attach(svc, samples["xlsx"])
    first = preview.render_for(svc, att.id)
    assert first["status"] == "ready"
    assert first["stats"]["cached"] is False

    rows = _cache_rows(svc)
    assert len(rows) == 1
    row = rows[0]
    assert row["attachment_id"] == att.id
    assert row["version"] == preview.attachment_version(att)
    assert row["kind"] == "xlsx"
    assert row["recipe"] == preview.RECIPE_VERSION
    assert row["status"] == "ready"
    assert row["bytes"] == len(json.dumps(row["payload"], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert row["payload"]["data"]["kind"] == "xlsx"
    assert row["payload"]["data"]["sheets"][0]["rows"][0]["cells"][0]["value"] == XLSX_A1

    second = preview.render_for(svc, att.id)
    assert second["stats"]["cached"] is True
    assert second["data"] == first["data"]

    refreshed = preview.render_for(svc, att.id, refresh=True)
    assert refreshed["stats"]["cached"] is False
    assert refreshed["data"] == first["data"]
    assert len(_cache_rows(svc)) == 1  # 同一版本同一配方：覆盖，不新增行


def test_failures_are_not_cached(svc: AttachmentService, samples):
    _render(svc, samples["broken_docx"])
    assert _cache_rows(svc) == []


def test_version_change_recomputes_and_never_reuses_the_old_row(svc: AttachmentService, samples):
    att = _attach(svc, samples["docx"])
    first = preview.render_for(svc, att.id)
    assert first["stats"]["cached"] is False
    old_version = first["version"]
    assert len(_cache_rows(svc)) == 1

    # 模拟「文件被改 / 重新关联」：版本变了
    with transaction(svc.conn):
        svc.conn.execute(
            "UPDATE attachments SET sha256 = ? WHERE id = ?", ("f" * 64, att.id)
        )
    second = preview.render_for(svc, att.id)
    assert second["version"] != old_version
    assert second["version"] == "sha256:" + "f" * 64
    assert second["stats"]["cached"] is False  # 版本变了 → 重新转换，不吃旧缓存
    assert len(_cache_rows(svc)) == 2

    third = preview.render_for(svc, att.id)
    assert third["version"] == second["version"]
    assert third["stats"]["cached"] is True


def test_late_result_is_not_written_onto_a_new_version(svc: AttachmentService, samples, monkeypatch):
    """转换期间附件版本变了：这次结果一份都不写进缓存（迟到结果不写缓存）。"""
    original = preview.convert

    def _convert_then_bump_version(att, path, *, cancel=None):  # noqa: ANN001
        data = original(att, path, cancel=cancel)
        with transaction(svc.conn):
            svc.conn.execute(
                "UPDATE attachments SET sha256 = ? WHERE id = ?", ("a" * 64, att.id)
            )
        return data

    monkeypatch.setattr(preview, "convert", _convert_then_bump_version)
    att = _attach(svc, samples["docx"])
    env = preview.render_for(svc, att.id)
    assert env["data"] is not None  # 这次请求仍然看得到内容
    assert _cache_rows(svc) == []  # 但没有留下任何一行
    assert "没有写入缓存" in env["reason"]


def test_unknown_attachment_is_failed_not_raised(svc: AttachmentService):
    env = preview.render_for(svc, "att_不存在")
    assert env["status"] == "failed"
    assert env["data"] is None
    assert "没有这个附件" in env["reason"]


# ---------------------------------------------------------------------------
# read_attachment 工具的新增分派（PPTX / PDF 正文提取）
# ---------------------------------------------------------------------------


def _tool_content(path: Path, name: str, *, offset: int = 0, limit: int = 50):
    """直接调用工具的提取函数（同步、纯计算）：不经过 async 包装，断言真实产出。"""
    from agent.services.attachments import Attachment
    from agent.tools import attachment_tools

    att = Attachment(
        id="att_tool",
        message_id=None,
        turn_id=None,
        topic_id=None,
        kind="copy",
        original_name=name,
        stored_path=str(path),
        source_path=None,
        size_bytes=path.stat().st_size,
        mtime=path.stat().st_mtime,
        sha256=None,
        state="ready",
        error=None,
        created_at="2026-10-12T00:00:00+00:00",
        updated_at="2026-10-12T00:00:00+00:00",
    )
    mode = attachment_tools.classify_readability(name)[0]
    extractor = {
        "pptx": attachment_tools._extract_pptx,
        "pdf": attachment_tools._extract_pdf,
    }[mode]
    return mode, extractor(att, path, offset, limit, 0, None)


def test_read_attachment_tool_extracts_pptx_per_slide(samples):
    mode, content = _tool_content(samples["pptx"], "样例演示.pptx")
    assert mode == "pptx"
    assert content.readable is True
    assert "—— 第 1 页幻灯片 ——" in content.content
    assert PPTX_SLIDE1_TITLE in content.content
    assert "阶段 | 负责人" in content.content
    assert "[图片] image1.png" in content.content
    assert "备注：备注内容：确认预算" in content.content
    assert "不还原版面" in " ".join(content.notes)


def test_read_attachment_tool_extracts_pdf_text_layer(samples):
    mode, content = _tool_content(samples["pdf"], "样例文档.pdf")
    assert mode == "pdf"
    assert content.readable is True
    assert "—— 第 1 页 ——" in content.content
    assert PDF_TEXT in content.content
    assert content.extra.get("pages") == 1


def test_read_attachment_tool_refuses_encrypted_pdf(tmp_path: Path):
    from pypdf import PdfWriter

    path = tmp_path / "加密.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.encrypt("secret")
    with open(path, "wb") as handle:
        writer.write(handle)
    mode, content = _tool_content(path, "加密.pdf")
    assert mode == "pdf"
    assert content.readable is False
    assert "加密" in (content.reason or "")


def test_reference_attachment_without_a_copy_is_failed(svc: AttachmentService, tmp_path: Path):
    source = tmp_path / "只记位置.py"  # 分类必须是受支持的类别，才走「取副本」这条路
    source.write_bytes(b"x" * 32)
    att = svc.prepare(str(source))
    # 不跑 run_prepare：这行还是「只登记、没有副本」
    env = preview.render_for(svc, att.id)
    assert env["status"] == "failed"
    assert env["data"] is None
    assert env["reason"]
    assert _cache_rows(svc) == []
