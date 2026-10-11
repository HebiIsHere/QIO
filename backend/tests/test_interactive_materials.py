"""材料解析的诚实口径（契约 docs/interactive-mode-contract.md §14.1 / §14.3）。

锁定这些边界：

- 只有真的读到正文才是 attachment_text / url_fetched / inline，且 read=true；
- 文件名、文件说明、网址本身**都不是正文**；
- 图片 → image_unsupported（当前模型链路没有视觉能力）；
- 不可读格式 / 读取失败 → unreadable + 可读原因（不假装读过）；
- max_chars 截断如实置 truncated=true，characters 记截断前的原始字数；
- 未勾选注释 / 隐藏 / 已删除的卡片绝不进材料。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.interactive import materials, models
from agent.services.attachments import AttachmentService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

BOARD = "board_materials"


@pytest.fixture()
def svc(db_conn: sqlite3.Connection, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _state(*cards) -> dict:
    state = models.empty_state(BOARD)
    state["cards"] = list(cards)
    return state


def _ready(svc: AttachmentService, tmp_path: Path, name: str, body: bytes):
    source = tmp_path / f"m-src-{name}"
    source.write_bytes(body)
    att = svc.run_prepare(svc.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready", (att.state, att.error)
    return att


def _by_card(entries: list[dict], card_id: str) -> dict:
    found = [entry for entry in entries if entry["cardId"] == card_id]
    assert found, [entry["cardId"] for entry in entries]
    return found[0]


def _assert_contract_fields(entry: dict) -> None:
    for key in (
        "cardId",
        "kind",
        "label",
        "source",
        "version",
        "read",
        "truncated",
        "bytes",
        "characters",
        "reason",
        "attachmentId",
    ):
        assert key in entry, (key, entry)
    assert entry["source"] in models.MATERIAL_SOURCES


# -- inline：正文就在卡片里 -------------------------------------------------


def test_inline_text_and_code_are_the_body(db_conn, svc, tmp_path):
    note = models.new_card("text", "把这三个文件按时间排序", checked=True)
    code = models.new_card("code", "print('hi')", meta={"language": "python"})
    state = _state(note, code)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[note, code], attachments=svc
    )
    assert [entry["cardId"] for entry in entries] == [note["id"], code["id"]]

    text_entry = _by_card(entries, note["id"])
    _assert_contract_fields(text_entry)
    assert text_entry["source"] == "inline"
    assert text_entry["read"] is True and text_entry["truncated"] is False
    assert text_entry["text"] == "把这三个文件按时间排序"
    assert text_entry["characters"] == len("把这三个文件按时间排序")
    assert text_entry["bytes"] == len("把这三个文件按时间排序".encode("utf-8"))

    code_entry = _by_card(entries, code["id"])
    assert code_entry["source"] == "inline" and code_entry["text"] == "print('hi')"
    assert any("python" in note for note in code_entry["notes"])


def test_cards_can_be_given_as_ids_and_unchecked_note_is_skipped(db_conn, svc):
    note = models.new_card("text", "没有勾选的注释", checked=False)
    hidden = models.new_card("text", "明确隐藏的注释", checked=True, hidden=True)
    state = _state(note, hidden)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[note["id"], hidden["id"]], attachments=svc
    )
    assert entries == [], "未勾选 / 隐藏的注释绝不进材料"


def test_reply_card_is_not_material(db_conn, svc):
    reply = models.new_card("reply", "QIO 自己的结果卡片")
    state = _state(reply)
    assert materials.resolve_materials(
        db_conn, state=state, cards=[reply], attachments=svc
    ) == []


def test_empty_attachment_body_is_unreadable_not_attachment_text(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "空.txt", b"")
    card = models.new_card("file", "", meta={"file": {"name": "空.txt", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "unreadable", "没有正文就不能说读到了附件正文"
    assert entry["read"] is False and entry["text"] == ""
    assert entry["reason"]


def test_empty_inline_body_is_not_claimed_as_read(db_conn, svc):
    note = models.new_card("text", "", checked=True)
    state = _state(note)
    entry = materials.resolve_materials(
        db_conn, state=state, cards=[note], attachments=svc
    )[0]
    assert entry["read"] is False and entry["text"] == ""
    assert entry["reason"]


# -- 文件卡：只认真实读到的附件正文 -----------------------------------------


def test_file_card_reads_real_saved_attachment(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "说明.txt", "第一行\n第二行".encode("utf-8"))
    card = models.new_card("file", "文件说明不是正文", meta={"file": {"name": "说明.txt", "attachmentId": att.id}})
    state = _state(card)

    entry = materials.resolve_materials(db_conn, state=state, cards=[card], attachments=svc)[0]
    _assert_contract_fields(entry)
    assert entry["source"] == "attachment_text"
    assert entry["read"] is True and entry["truncated"] is False
    assert "第一行" in entry["text"] and "第二行" in entry["text"]
    assert entry["attachmentId"] == att.id
    assert entry["bytes"] == Path(att.stored_path).stat().st_size
    assert entry["characters"] == len(entry["text"])
    assert entry["version"].startswith("sha256:")
    assert "文件说明不是正文" not in entry["text"]


def test_file_card_without_attachment_id_never_uses_the_name(db_conn, svc):
    card = models.new_card(
        "file", "这是文件说明，不是正文", meta={"file": {"name": "合同.pdf"}}
    )
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "unreadable"
    assert entry["read"] is False and entry["text"] == ""
    assert "文件名不是正文" in entry["reason"]
    assert "合同.pdf" in entry["reason"]


def test_unknown_attachment_id_is_unreadable(db_conn, svc):
    card = models.new_card("file", "", meta={"file": {"name": "x.txt", "attachmentId": "att_missing"}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "unreadable" and entry["read"] is False
    assert "att_missing" in entry["reason"]


def test_unsupported_format_is_unreadable_with_reason(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "扫描件.pdf", b"%PDF-1.4 not really")
    card = models.new_card("file", "", meta={"file": {"name": "扫描件.pdf", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "unreadable" and entry["read"] is False
    assert entry["text"] == ""
    assert "PDF 文本解析未实现" in entry["reason"]
    assert entry["attachmentId"] == att.id


def test_missing_copy_falls_back_to_original_with_a_note(db_conn, svc, tmp_path):
    # 副本丢了但用户原文件还在：按既有工具口径退回原文件读，并如实说明读的是哪一个
    att = _ready(svc, tmp_path, "a.txt", "原文件正文".encode("utf-8"))
    Path(att.stored_path).unlink()
    card = models.new_card("file", "", meta={"file": {"name": "a.txt", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "attachment_text" and entry["read"] is True
    assert "原文件正文" in entry["text"]
    assert any("原文件" in note for note in entry["notes"]), entry["notes"]


def test_attachment_without_copy_or_source_is_unreadable(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "b.txt", b"gone")
    Path(att.stored_path).unlink()
    Path(att.source_path).unlink()
    card = models.new_card("file", "", meta={"file": {"name": "b.txt", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "unreadable"
    assert entry["read"] is False and entry["text"] == ""
    assert entry["reason"]


def test_no_service_nor_derivable_attachment_is_unreadable(db_conn, tmp_path):
    # attachments=None：按连接位置推导服务；这里把副本与来源都弄丢，仍必须如实说不可读
    att = _ready(AttachmentService(db_conn, tmp_path / "data"), tmp_path, "c.txt", b"hello")
    Path(att.stored_path).unlink()
    Path(att.source_path).unlink()
    card = models.new_card("file", "", meta={"file": {"name": "c.txt", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=None
    )[0]
    assert entry["read"] is False and entry["text"] == ""
    assert entry["source"] == "unreadable"
    assert entry["reason"]


def test_csv_and_html_reuse_existing_parsing(db_conn, svc, tmp_path):
    csv_att = _ready(svc, tmp_path, "名单.csv", "姓名,年龄\n张三,30\n".encode("utf-8"))
    html_att = _ready(
        svc, tmp_path, "页面.html", "<html><body><h1>标题</h1><script>bad()</script></body></html>".encode("utf-8")
    )
    csv_card = models.new_card("file", "", meta={"file": {"name": "名单.csv", "attachmentId": csv_att.id}})
    html_card = models.new_card("file", "", meta={"file": {"name": "页面.html", "attachmentId": html_att.id}})

    entries = materials.resolve_materials(
        db_conn, state=_state(csv_card, html_card), cards=[csv_card, html_card], attachments=svc
    )
    csv_entry = _by_card(entries, csv_card["id"])
    html_entry = _by_card(entries, html_card["id"])
    assert csv_entry["source"] == "attachment_text" and "张三" in csv_entry["text"]
    assert html_entry["source"] == "attachment_text" and "标题" in html_entry["text"]
    assert "bad()" not in html_entry["text"], "脚本内容不得当成正文"


# -- 图片：没有视觉能力 -----------------------------------------------------


def test_image_card_is_image_unsupported(db_conn, svc, tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    att = _ready(svc, tmp_path, "截图.png", png)
    card = models.new_card("image", "", meta={"image": {"name": "截图.png", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "image_unsupported"
    assert entry["read"] is False and entry["text"] == ""
    assert entry["characters"] == 0
    assert "视觉能力" in entry["reason"]
    assert entry["attachmentId"] == att.id


def test_file_card_with_image_extension_is_image_unsupported(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "图.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    card = models.new_card("file", "", meta={"file": {"name": "图.png", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "image_unsupported" and entry["read"] is False


# -- URL：地址不等于正文 ---------------------------------------------------


def test_url_card_without_body_is_address_only(db_conn, svc):
    card = models.new_card("url", "https://example.com/a", meta={"url": {"href": "https://example.com/a"}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "url_address_only"
    assert entry["read"] is False and entry["text"] == ""
    assert entry["characters"] == 0
    assert "没有抓到网页正文" in entry["reason"]


def test_url_card_with_registered_body_is_fetched(db_conn, svc):
    card = models.new_card(
        "url",
        "https://example.com/a",
        meta={"url": {"href": "https://example.com/a", "content": "页面正文第一段"}},
    )
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["source"] == "url_fetched"
    assert entry["read"] is True
    assert entry["text"] == "页面正文第一段"
    assert entry["characters"] == len("页面正文第一段")


# -- 截断要如实 -------------------------------------------------------------


def test_max_chars_truncation_is_honest(db_conn, svc, tmp_path):
    body = ("行内容" * 20 + "\n") * 50  # 每行 60 字，共 3000 字
    att = _ready(svc, tmp_path, "长文.txt", body.encode("utf-8"))
    card = models.new_card("file", "", meta={"file": {"name": "长文.txt", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc, max_chars=100
    )[0]
    assert entry["source"] == "attachment_text" and entry["read"] is True
    assert entry["truncated"] is True
    assert len(entry["text"]) == 100
    assert entry["deliveredCharacters"] == 100
    assert entry["characters"] >= 3000, "characters 记截断前的原始字数"
    assert entry["characters"] > entry["deliveredCharacters"]
    assert any("截断" in note for note in entry["notes"])


def test_inline_truncation_is_honest(db_conn, svc):
    note = models.new_card("text", "字" * 500, checked=True)
    entry = materials.resolve_materials(
        db_conn, state=_state(note), cards=[note], attachments=svc, max_chars=80
    )[0]
    assert entry["truncated"] is True
    assert len(entry["text"]) == 80
    assert entry["characters"] == 500
    assert entry["deliveredCharacters"] == 80


def test_parser_window_limit_is_marked_truncated(db_conn, svc, tmp_path):
    # 超过解析器单次 40000 字符预算：即使 max_chars 用默认值，也必须说「没有读全」
    line = "x" * 200
    body = ("\n".join(line for _ in range(400))).encode("utf-8")
    att = _ready(svc, tmp_path, "很大.txt", body)
    card = models.new_card("file", "", meta={"file": {"name": "很大.txt", "attachmentId": att.id}})
    entry = materials.resolve_materials(
        db_conn, state=_state(card), cards=[card], attachments=svc
    )[0]
    assert entry["truncated"] is True
    assert entry["deliveredCharacters"] == materials.PARSER_CHAR_BUDGET
    assert any("没有读全" in note or "截断" in note for note in entry["notes"])


# -- 服务推导与参数校验 -----------------------------------------------------


def test_service_is_derived_from_connection_when_not_passed(tmp_path):
    conn = connect(tmp_path / "data" / "app.db")
    apply_migrations(conn)
    try:
        svc = AttachmentService(conn, tmp_path / "data")
        att = _ready(svc, tmp_path, "推导.txt", "推导出来的正文".encode("utf-8"))
        card = models.new_card("file", "", meta={"file": {"name": "推导.txt", "attachmentId": att.id}})
        entry = materials.resolve_materials(conn, state=_state(card), cards=[card])[0]
        assert entry["source"] == "attachment_text"
        assert entry["text"].strip() == "推导出来的正文"
    finally:
        conn.close()


def test_max_chars_must_be_a_number(db_conn, svc):
    with pytest.raises(ValueError):
        materials.resolve_materials(
            db_conn, state=_state(), cards=[], attachments=svc, max_chars=-1
        )
    with pytest.raises(ValueError):
        materials.resolve_materials(
            db_conn, state=_state(), cards=[], attachments=svc, max_chars="多"
        )
