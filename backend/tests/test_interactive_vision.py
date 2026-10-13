"""视觉能力（多模态交付）的诚实口径与形状（2026-10-12 新增）。

锁定这些边界：

- 只有「真的读了字节 + 适配器声明支持多模态」才是 `image_delivered`；
- 没有附件 / 附件不在 / 格式不支持 / 超过字节上限 → 仍然 `image_unsupported` + 可读原因；
- 纯文本消息的 OpenAI 形状**逐字不变**（字符串），带图才变成多模态数组；
- 上下文 JSON 里不含 base64（图片走内容块，不进正文），但交付事实随上下文落库；
- 未勾选注释 / 隐藏 / 已删除的图片卡一律不交付。
"""

from __future__ import annotations

import base64
import io
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, ImagePart
from agent.adapters.native import _openai_content
from agent.interactive import materials, models, prompts, understanding
from agent.services.attachments import AttachmentService

BOARD = "board_vision"


def _png(width: int = 2, height: int = 2) -> bytes:
    """最小合法 PNG（真字节，不是假头）：2x2 全黑。"""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture()
def svc(db_conn: sqlite3.Connection, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _ready(svc: AttachmentService, tmp_path: Path, name: str, body: bytes):
    source = tmp_path / f"v-src-{name}"
    source.write_bytes(body)
    att = svc.run_prepare(svc.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready", (att.state, att.error)
    return att


def _state(*cards) -> dict:
    state = models.empty_state(BOARD)
    state["cards"] = list(cards)
    return state


def _image_card(att) -> dict:
    return models.new_card(
        "image",
        "",
        meta={"name": att.original_name, "image": {"name": att.original_name, "attachmentId": att.id}},
    )


# -- 1. 交付：真的读了字节，并且适配器声明支持多模态 -------------------------


def test_delivered_image_carries_real_bytes(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "pic.png", _png())
    card = _image_card(att)
    state = _state(card)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[card], attachments=svc, vision=True
    )
    entry = [e for e in entries if e["cardId"] == card["id"]][0]
    assert entry["source"] == "image_delivered"
    assert entry["read"] is True
    assert entry["bytes"] == len(_png())

    images = materials.resolve_images(db_conn, state=state, cards=[card], attachments=svc)
    assert len(images) == 1
    image = images[0]
    assert image["cardId"] == card["id"]
    assert image["attachmentId"] == att.id
    assert image["mediaType"] == "image/png"
    assert image["bytes"] == len(_png())
    head, payload = image["dataUrl"].split(",", 1)
    assert head == "data:image/png;base64"
    assert base64.b64decode(payload) == _png(), "交付的必须是原字节，不是占位"


def test_no_vision_route_keeps_the_honest_unsupported_result(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "pic.png", _png())
    card = _image_card(att)
    state = _state(card)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[card], attachments=svc, vision=False
    )
    entry = [e for e in entries if e["cardId"] == card["id"]][0]
    assert entry["source"] == "image_unsupported"
    assert entry["read"] is False
    assert "没有视觉能力" in entry["reason"]
    # resolve_images 是**能力无关**的读取层（它只回答「字节读得出来吗」）；
    # 「这次要不要交付」的闸门在 build_context 的 vision 上 —— 见下一条用例。
    assert len(materials.resolve_images(db_conn, state=state, cards=[card], attachments=svc)) == 1


def test_unlinked_or_missing_attachment_is_still_unsupported(db_conn, svc, tmp_path):
    linked_only_name = models.new_card("image", "", meta={"name": "没关联的图.png"})
    ghost = models.new_card(
        "image",
        "",
        meta={"image": {"name": "x.png", "attachmentId": "att_not_here"}},
    )
    state = _state(linked_only_name, ghost)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[linked_only_name, ghost], attachments=svc, vision=True
    )
    by_id = {e["cardId"]: e for e in entries}
    assert by_id[linked_only_name["id"]]["source"] == "image_unsupported"
    assert "没有视觉能力" in by_id[linked_only_name["id"]]["reason"]
    assert any("attachmentId 缺失" in str(n) for n in by_id[linked_only_name["id"]]["notes"])
    assert "没有视觉能力" in by_id[ghost["id"]]["reason"]
    assert any("取不到" in str(n) for n in by_id[ghost["id"]]["notes"])
    assert materials.resolve_images(
        db_conn, state=state, cards=[linked_only_name, ghost], attachments=svc
    ) == []


def test_oversized_image_is_refused_with_a_readable_reason(db_conn, svc, tmp_path):
    big = _png() + b"\x00" * (materials.MAX_IMAGE_BYTES + 1)
    att = _ready(svc, tmp_path, "big.png", big)
    card = _image_card(att)
    state = _state(card)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[card], attachments=svc, vision=True
    )
    entry = [e for e in entries if e["cardId"] == card["id"]][0]
    assert entry["source"] == "image_unsupported"
    assert "超过交付上限" in entry["reason"]
    assert materials.resolve_images(db_conn, state=state, cards=[card], attachments=svc) == []


def test_invisible_images_are_never_delivered(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "pic.png", _png())
    hidden = _image_card(att)
    hidden["hidden"] = True
    deleted = _image_card(att)
    deleted["deleted"] = True
    state = _state(hidden, deleted)

    assert materials.resolve_images(
        db_conn, state=state, cards=[hidden, deleted], attachments=svc
    ) == []


# -- 2. OpenAI 消息形状：纯文本逐字不变，带图才是多模态 ---------------------


def test_text_only_message_shape_is_unchanged():
    msg = ChatMessage(role="user", content="只有文字")
    assert _openai_content(msg) == "只有文字"


def test_image_message_becomes_a_content_array_with_real_data_url():
    part = ImagePart(data_url="data:image/png;base64,AAAA", label="图片（卡片 c1）")
    msg = ChatMessage(role="user", content="上下文", images=[part])
    content = _openai_content(msg)
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "上下文"}
    labels = [c for c in content if c["type"] == "text" and c["text"].startswith("[图片]")]
    assert labels and "c1" in labels[0]["text"], "标签必须带上卡片身份，模型引用时才不用猜"
    urls = [c["image_url"]["url"] for c in content if c["type"] == "image_url"]
    assert urls == ["data:image/png;base64,AAAA"]


# -- 3. 提示词：图片挂在同一条 user 消息上，且不进 JSON 正文 ----------------


def test_prompt_attaches_images_to_the_user_message_not_the_json():
    part = ImagePart(data_url="data:image/png;base64,AAAA", label="图片（卡片 c1）")
    context = {"boardId": BOARD, "materials": [], "imageParts": [{"dataUrl": "data:image/png;base64,AAAA"}]}
    messages = prompts.build_understanding_messages(context, images=[part])
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    assert messages[1].images == [part]
    assert "base64" not in (messages[1].content or ""), "上下文 JSON 不该夹带 base64"


def test_image_parts_rebuild_from_saved_context():
    context = {
        "materials": [
            {"cardId": "c1", "source": "image_delivered", "attachmentId": "att_1"},
            {"cardId": "c2", "source": "image_unsupported", "attachmentId": "att_2"},
        ],
        "imageParts": [
            {"dataUrl": "data:image/png;base64,AAAA", "label": "图片（卡片 c1）", "mediaType": "image/png", "bytes": 3}
        ],
    }
    parts = understanding.image_parts_from_context(context)
    assert len(parts) == 1
    assert parts[0].label == "图片（卡片 c1）"
    assert parts[0].media_type == "image/png"
    assert understanding.image_parts_from_context({}) == []


def test_delivered_source_counts_as_read_in_honesty_projection():
    assert "image_delivered" in understanding.READABLE_SOURCES

# -- 4. 交付闸门在 build_context 上：没有视觉能力就不带任何图片内容块 ----------


def _seed_submission(db_conn, state: dict) -> str:
    """落一条最小提交 + 理解请求，供 build_context 使用（只读快照，不调模型）。"""
    from agent.interactive import board_store

    saved = board_store.save_board(db_conn, BOARD, state, reason="vision test")
    sub_id = "sub_vision_1"
    import json as _json
    import uuid as _uuid

    db_conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state,"
        " visible, created_at, client_request_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            sub_id,
            BOARD,
            int(saved["seq"]),
            "succeeded",
            _json.dumps({"cards": [], "groups": [], "links": [], "selection": []}),
            _json.dumps({"cards": state["cards"], "groups": [], "links": [], "selection": []}),
            _json.dumps({"cards": state["cards"], "groups": [], "links": [], "selection": []}),
            "2026-10-12T00:00:00+00:00",
            "creq_vision_" + _uuid.uuid4().hex[:8],
        ),
    )
    uid = "u_vision_1"
    db_conn.execute(
        "INSERT INTO board_understanding (id, board_id, submission_id, request_id, status, stage,"
        " snapshot, context, materials, output, validation, memory, attempt, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            uid,
            BOARD,
            sub_id,
            "sub:" + BOARD + ":" + sub_id,
            "accepted",
            "queued",
            _json.dumps({"cards": state["cards"], "groups": [], "links": [], "selection": []}),
            "{}",
            "[]",
            "{}",
            "{}",
            "[]",
            1,
            "2026-10-12T00:00:01+00:00",
            "2026-10-12T00:00:01+00:00",
        ),
    )
    db_conn.commit()
    return uid


def test_context_only_carries_image_parts_when_vision_is_on(db_conn, svc, tmp_path, monkeypatch):
    att = _ready(svc, tmp_path, "pic.png", _png())
    card = _image_card(att)
    state = _state(card)
    uid = _seed_submission(db_conn, state)

    # 材料模块要用真附件服务（否则读不到字节）
    monkeypatch.setattr(understanding, "_derive_materials_service", lambda conn: svc, raising=False)
    monkeypatch.setattr(
        understanding, "_resolve_deliverable_images",
        lambda conn, st: materials.resolve_images(conn, state=st, cards=st["cards"], attachments=svc),
    )

    with_vision = understanding.build_context(db_conn, board_id=BOARD, understanding_id=uid, vision=True)
    without = understanding.build_context(db_conn, board_id=BOARD, understanding_id=uid, vision=False)

    assert with_vision["imageParts"], "视觉开启时必须把可交付图片读进上下文"
    assert with_vision["imageParts"][0]["dataUrl"].startswith("data:image/png;base64,")
    assert understanding.image_parts_from_context(with_vision), "上下文里的交付事实能重建内容块"
    assert without["imageParts"] == [], "没有视觉能力时不得带任何图片内容块"
    assert understanding.image_parts_from_context(without) == []
    sources = {m["source"] for m in without["materials"]}
    assert "image_delivered" not in sources


# -- 「文件」入口加的图片：与图片卡同一份交付事实（2026-10-13 复审指出的缺口） ------


def _file_card(att) -> dict:
    """用户在板面用「文件」入口加的卡片（扩展名是图片时也应该拿到画面）。"""
    return models.new_card(
        "file",
        "",
        meta={"name": att.original_name, "file": {"name": att.original_name, "attachmentId": att.id}},
    )


def test_file_card_holding_an_image_is_delivered_when_vision_is_on(db_conn, svc, tmp_path):
    """链路有视觉能力时，「文件」卡里的 .png 必须交付**真实字节**。

    旧实现把这一支硬编码成「当前模型链路没有视觉能力」且从不读字节 —— 理由假、字节也永远拿不到。
    """
    att = _ready(svc, tmp_path, "shot-file.png", _png())
    card = _file_card(att)
    state = _state(card)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[card], attachments=svc, vision=True
    )
    entry = [e for e in entries if e["cardId"] == card["id"]][0]
    assert entry["source"] == "image_delivered", entry
    assert entry["read"] is True
    assert entry["bytes"] == len(_png())
    assert "没有视觉能力" not in entry["reason"], entry["reason"]

    images = materials.resolve_images(db_conn, state=state, cards=[card], attachments=svc)
    assert len(images) == 1, images
    head, payload = images[0]["dataUrl"].split(",", 1)
    assert head == "data:image/png;base64"
    assert base64.b64decode(payload) == _png(), "交付的必须是原字节"


def test_file_card_holding_an_image_without_vision_keeps_the_honest_reason(db_conn, svc, tmp_path):
    att = _ready(svc, tmp_path, "shot-file2.png", _png())
    card = _file_card(att)
    state = _state(card)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[card], attachments=svc, vision=False
    )
    entry = [e for e in entries if e["cardId"] == card["id"]][0]
    assert entry["source"] == "image_unsupported"
    assert entry["read"] is False
    assert "没有视觉能力" in entry["reason"]
    # resolve_images 是**能力无关**的读取层（只回答「字节读得出来吗」）：
    # 「这次要不要交付」的闸门在 build_context 的 vision 上（见下一条用例）。
    assert len(materials.resolve_images(db_conn, state=state, cards=[card], attachments=svc)) == 1


def test_non_image_file_card_is_not_delivered_as_an_image(db_conn, svc, tmp_path):
    """反向守卫：普通文件卡（.txt）不能被当成图片交付，也不许标成 image_* 的事实。"""
    att = _ready(svc, tmp_path, "note.txt", "这是一份文本".encode("utf-8"))
    card = _file_card(att)
    state = _state(card)

    entries = materials.resolve_materials(
        db_conn, state=state, cards=[card], attachments=svc, vision=True
    )
    entry = [e for e in entries if e["cardId"] == card["id"]][0]
    assert not str(entry["source"]).startswith("image"), entry["source"]
    assert materials.resolve_images(db_conn, state=state, cards=[card], attachments=svc) == []


def test_context_includes_a_file_card_image_when_vision_is_on(
    db_conn, svc, tmp_path, monkeypatch
):
    """端到端：上下文里真的带上这份图片，且能重建内容块。"""
    att = _ready(svc, tmp_path, "shot-file3.png", _png())
    card = _file_card(att)
    state = _state(card)
    uid = _seed_submission(db_conn, state)

    monkeypatch.setattr(understanding, "_derive_materials_service", lambda conn: svc, raising=False)
    monkeypatch.setattr(
        understanding,
        "_resolve_deliverable_images",
        lambda conn, st: materials.resolve_images(conn, state=st, cards=st["cards"], attachments=svc),
    )

    with_vision = understanding.build_context(db_conn, board_id=BOARD, understanding_id=uid, vision=True)
    without = understanding.build_context(db_conn, board_id=BOARD, understanding_id=uid, vision=False)

    assert with_vision["imageParts"], "视觉开启时文件卡里的图片也要进上下文"
    assert with_vision["imageParts"][0]["dataUrl"].startswith("data:image/png;base64,")
    assert understanding.image_parts_from_context(with_vision)
    assert without["imageParts"] == []

