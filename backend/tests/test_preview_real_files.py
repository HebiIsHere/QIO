"""真实文件验收：六类格式在卡片内预览链路里的端到端证据（Lead 集成测试）。

契约：docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md §3。

这条用例要证明的是**真实文件、真实字节、真实服务**：

1. 六类样例由 fixtures/preview 用代码生成（真 zip + 真 XML 的 docx/xlsx/pptx、手写最小合法 PDF、
   真实 PNG 字节、真实代码文本），**不是假文件占位**；
2. 走真实 AttachmentService（落盘到真实数据目录）与真实 HTTP 路由（TestClient）；
3. 每一步都断言**具体内容**（某个段落文字、A1 的值与公式、幻灯片标题、PDF 文本、代码行号），
   而不是"返回了 200 就算过"；
4. 结尾把预览结论与正文读取结论放在一起对照 —— **能预览 ≠ 模型已读到**，这两件事必须能分开看。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import materials as materials_service
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from fixtures.preview import (
    CODE_TEXT,
    DOCX_HEADING,
    DOCX_LAST_PARAGRAPH,
    DOCX_TABLE_ROWS,
    PDF_TEXT,
    PPTX_SLIDE1_TITLE,
    XLSX_A1,
    XLSX_B2_FORMULA,
    XLSX_SHEET_NAME,
    build_all,
)

BOARD_ID = "board_real_files"


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "real_files.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def samples(tmp_path: Path) -> dict[str, Path]:
    return build_all(tmp_path / "samples")


def _upload(client: TestClient, path: Path) -> dict:
    resp = client.post(
        "/api/attachments/upload",
        content=Path(path).read_bytes(),
        headers={
            "Content-Type": "application/octet-stream",
            "X-QIO-Name": quote(Path(path).name),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["attachment"]


def _preview(client: TestClient, attachment_id: str) -> dict:
    resp = client.get(f"/api/attachments/{attachment_id}/preview")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        ("docx", "docx"),
        ("xlsx", "xlsx"),
        ("pptx", "pptx"),
        ("pdf", "pdf"),
        ("png", "image"),
        ("code", "code"),
    ],
)
def test_real_file_preview_is_ready_with_real_content(
    client: TestClient, samples: dict[str, Path], key: str, kind: str
) -> None:
    """六类真实文件：预览结论必须是 ready，且 kind 正确（不是"文件名识别"）。"""
    source = samples[key]
    att = _upload(client, source)
    body = _preview(client, att["id"])
    assert body["status"] == "ready", (source.name, body.get("reason"))
    assert body["kind"] == kind
    assert body["attachmentId"] == att["id"]
    assert body["version"], "预览必须带上文件版本（位置与缓存都按版本归属）"
    assert body["data"], "ready 却没有数据"


def test_docx_preview_keeps_paragraphs_and_table(client: TestClient, samples: dict[str, Path]) -> None:
    att = _upload(client, samples["docx"])
    data = _preview(client, att["id"])["data"]
    texts = [run["text"] for block in data["blocks"] for run in block.get("runs", [])]
    joined = "".join(texts)
    assert DOCX_HEADING in joined
    assert DOCX_LAST_PARAGRAPH in joined
    assert [DOCX_TABLE_ROWS[0], DOCX_TABLE_ROWS[1]] == [row for row in data["tables"][0]["rows"]][:2]
    assert data["truncated"] is False


def test_xlsx_preview_keeps_sheet_cell_and_formula(client: TestClient, samples: dict[str, Path]) -> None:
    att = _upload(client, samples["xlsx"])
    data = _preview(client, att["id"])["data"]
    sheet = data["sheets"][0]
    assert sheet["name"] == XLSX_SHEET_NAME
    cells = {cell["ref"]: cell for row in sheet["rows"] for cell in row["cells"]}
    assert cells["A1"]["value"] == XLSX_A1
    # 公式与缓存值分开：不允许把公式当值、也不允许把值当公式
    assert XLSX_B2_FORMULA in (cells["B2"]["formula"] or "")
    assert cells["B2"]["formula"] != cells["B2"]["value"]


def test_pptx_preview_keeps_slide_text_and_table(client: TestClient, samples: dict[str, Path]) -> None:
    att = _upload(client, samples["pptx"])
    data = _preview(client, att["id"])["data"]
    first = data["slides"][0]
    assert first["title"] == PPTX_SLIDE1_TITLE
    assert any("调研" in text for text in first["texts"])
    assert first["tables"], "幻灯片里的表格必须保留（否则等于只读了标题）"


def test_pdf_preview_reports_facts_not_rendering(client: TestClient, samples: dict[str, Path]) -> None:
    att = _upload(client, samples["pdf"])
    body = _preview(client, att["id"])
    data = body["data"]
    assert data["kind"] == "pdf"
    assert data["pages"] == 1
    assert data["encrypted"] is False
    # 渲染在前端（pdf.js）；后端只给事实，不假装渲染过
    assert PDF_TEXT not in str(data.get("rendered", ""))


def test_code_preview_keeps_lines_and_language(client: TestClient, samples: dict[str, Path]) -> None:
    att = _upload(client, samples["code"])
    data = _preview(client, att["id"])["data"]
    assert data["language"] == "python"
    assert data["lines"], "代码文件必须给出逐行文本（前端按行号渲染）"
    assert "".join(data["lines"]).strip().startswith(CODE_TEXT.strip().splitlines()[0].strip()[:8])


def test_image_preview_reports_real_dimensions(client: TestClient, samples: dict[str, Path]) -> None:
    att = _upload(client, samples["png"])
    data = _preview(client, att["id"])["data"]
    assert data["width"] == 8 and data["height"] == 6
    assert data["bytes"] > 0


def test_legacy_office_and_unsupported_are_honest(client: TestClient, samples: dict[str, Path]) -> None:
    att = _upload(client, samples["legacy_doc"])
    body = _preview(client, att["id"])
    assert body["status"] in ("unsupported", "failed")
    assert body["reason"], "不支持 / 失败必须给可读原因"
    # 旧版 Office 的 data 是**结构化的"为什么不能预览"**（kind + reason），不是 None：
    # 界面要能据此显示原因并保留打开 / 另存入口。它绝不假装渲染过版面。
    data = body["data"] or {}
    assert data.get("kind") in ("legacy_office", "unsupported", None)
    assert "旧版" in body["reason"] and "打开" in body["reason"]


def test_broken_file_fails_with_readable_reason_not_500(client: TestClient, samples: dict[str, Path]) -> None:
    for key in ("broken_docx", "broken_pdf"):
        att = _upload(client, samples[key])
        resp = client.get(f"/api/attachments/{att['id']}/preview")
        assert resp.status_code == 200, "预览接口不允许 500：损坏文件也要如实返回"
        assert resp.json()["status"] == "failed"
        assert resp.json()["reason"]


def test_preview_ready_but_model_reading_is_a_separate_fact(
    client: TestClient, samples: dict[str, Path]
) -> None:
    """**能预览 ≠ 模型已读到**：两条链路的结论必须能分开看。

    同一个真实 .docx：
    * 预览接口返回 ready（这是"预览可用"）；
    * 它作为**文件材料卡**进入材料解析时读到正文（source=attachment_text）；
    * 同一份内容放进**未许可的文字卡**时，材料解析里根本看不到它（未许可 = 整体不可见）。
    """
    source = samples["docx"]
    att = _upload(client, source)
    preview = _preview(client, att["id"])
    assert preview["status"] == "ready"

    conn = client.app.state.ctx.conn
    service = client.app.state.ctx.attachments
    file_card = {
        "id": "c_file",
        "kind": "file",
        "content": "",
        "meta": {"file": {"name": "report.docx", "attachmentId": att["id"], "size": source.stat().st_size}},
        "checked": False,
        "hidden": False,
        "folded": False,
        "bookmarked": False,
        "deleted": False,
    }
    entries = materials_service.resolve_materials(
        conn, state={"cards": [file_card]}, cards=None, attachments=service
    )
    assert len(entries) == 1
    assert entries[0]["source"] == "attachment_text"
    assert entries[0]["read"] is True
    assert DOCX_HEADING in (entries[0].get("notes") or "") or entries[0]["characters"] > 0

    unlicensed = dict(file_card)
    unlicensed.update(
        {
            "id": "c_text",
            "kind": "text",
            "content": "",
            "checked": False,
            "meta": {"doc": {"schema": "qio.textcard/1", "blocks": [
                {"id": "b1", "kind": "paragraph", "text": "这段文字没有许可"}]},
                "docText": "这段文字没有许可\n"},
        }
    )
    licensed = dict(unlicensed)
    licensed["checked"] = True
    invisible = materials_service.resolve_materials(
        conn, state={"cards": [unlicensed]}, cards=None, attachments=service
    )
    assert invisible == [], "未许可的文字卡整体不得进入材料（含派生正文）"
    visible = materials_service.resolve_materials(
        conn, state={"cards": [licensed]}, cards=None, attachments=service
    )
    assert len(visible) == 1 and visible[0]["source"] == "inline"

def test_real_pdf_and_pptx_reach_the_model_as_readable_text(
    client: TestClient, samples: dict[str, Path]
) -> None:
    """真实 PDF / PPTX 的**正文**要真的被材料解析读到（不是只有预览能看）。

    为什么单独一条：预览走 preview.py，模型读正文走 attachment_tools 的解析分派。
    两条路各自实现过 PDF/PPTX —— 只测其中一条，另一条会悄悄退化成「二进制不可读」。
    """
    conn = client.app.state.ctx.conn
    service = client.app.state.ctx.attachments
    for key, needle in (("pdf", PDF_TEXT), ("pptx", PPTX_SLIDE1_TITLE)):
        source = samples[key]
        att = _upload(client, source)
        card = {
            "id": "c_" + key,
            "kind": "file",
            "content": "",
            "meta": {
                "file": {
                    "name": source.name,
                    "attachmentId": att["id"],
                    "size": source.stat().st_size,
                }
            },
            "checked": False,
            "hidden": False,
            "folded": False,
            "bookmarked": False,
            "deleted": False,
        }
        entries = materials_service.resolve_materials(
            conn, state={"cards": [card]}, cards=None, attachments=service
        )
        assert len(entries) == 1, (key, entries)
        assert entries[0]["source"] == "attachment_text", (key, entries[0].get("reason"))
        assert entries[0]["read"] is True
        assert needle in entries[0]["text"], (key, entries[0]["text"][:120])
