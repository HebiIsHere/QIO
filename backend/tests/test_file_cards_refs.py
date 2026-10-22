"""文件卡片与源引用（契约 §6.4 / §8）：快照必须暴露引用事实 + 用户说明。

覆盖：
* _card_snapshot 的 file_ref 含 §6.4 全部字段（attachment_id/filename/media_kind/
  origin/source_kind/availability/size/user_note/material_enabled/missing_reason/reference_id）；
* 卡片没有关联附件 / 附件记录不存在 / 附件服务不可用时，如实用 missing_reason，不伪造路径；
* 复制卡片（同一 attachmentId 两张卡）共享同一源引用；删除引用行不删源文件，
  两张卡的快照随后都如实变成「找不到附件记录」；
* 互动材料条目同步暴露 file_ref + 用户说明（materials._attach_file_ref 口径）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.interactive.board_tools import _card_snapshot
from agent.interactive import materials
from agent.services.attachments import AttachmentService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def service(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _file_card(card_id: str, attachment_id: str | None, *, name: str = "材料.txt") -> dict:
    meta: dict = {"title": "文件卡"}
    if attachment_id:
        meta["file"] = {"attachmentId": attachment_id, "name": name}
    return {
        "id": card_id,
        "kind": "file",
        "version": 1,
        "content": "",
        "meta": meta,
        "x": 0.0,
        "y": 0.0,
        "w": 200.0,
        "h": 120.0,
        "checked": False,
        "hidden": False,
    }


FIELDS = (
    "attachment_id",
    "filename",
    "media_kind",
    "origin",
    "source_kind",
    "availability",
    "size",
    "user_note",
    "material_enabled",
    "missing_reason",
    "reference_id",
)


def test_snapshot_exposes_full_file_ref(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "卡片材料.txt"
    source.write_text("正文内容", encoding="utf-8")
    att = service.register_source(
        path=str(source),
        source_kind="desktop_path",
        user_note="这是用户写的说明",
        topic_id="t1",
    )

    snapshot = _card_snapshot(_file_card("c1", att.id), attachment_service=service)
    ref = snapshot["file_ref"]
    for key in FIELDS:
        assert key in ref, f"卡片快照缺 §6.4 字段：{key}"
    assert ref["attachment_id"] == att.id
    assert ref["reference_id"] == att.id
    assert ref["filename"] == "卡片材料.txt"
    assert ref["origin"] == "user_original"
    assert ref["source_kind"] == "desktop_path"
    assert ref["user_note"] == "这是用户写的说明"
    assert ref["material_enabled"] is True
    assert ref["availability"]["state"] == "ready"
    assert ref["missing_reason"] is None


def test_snapshot_material_switch_and_missing_are_honest(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "开关.txt"
    source.write_text("内容", encoding="utf-8")
    att = service.register_source(path=str(source), source_kind="desktop_path")
    service.set_material_enabled(att.id, False)
    off = _card_snapshot(_file_card("c1", att.id), attachment_service=service)
    assert off["file_ref"]["material_enabled"] is False

    # 卡片有 id 但附件记录不存在：如实说找不到，不伪造路径
    ghost = _card_snapshot(_file_card("c2", "att_missing"), attachment_service=service)
    assert ghost["file_ref"]["missing_reason"]
    assert "att_missing" in ghost["file_ref"]["missing_reason"]
    assert ghost["file_ref"]["availability"] is None

    # 卡片没有关联附件
    empty = _card_snapshot(_file_card("c3", None), attachment_service=service)
    assert empty["file_ref"]["attachment_id"] is None
    assert "还没有关联" in empty["file_ref"]["missing_reason"]

    # 附件服务不可用：不假装拿到了引用事实
    no_service = _card_snapshot(_file_card("c4", att.id), attachment_service=None)
    assert no_service["file_ref"]["missing_reason"]


def test_copied_cards_share_reference_and_delete_keeps_source(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "共享源.txt"
    source.write_bytes("共享内容".encode("utf-8"))
    att = service.register_source(path=str(source), source_kind="desktop_path")

    card_a = _file_card("card_a", att.id)
    card_b = _file_card("card_b", att.id)  # 复制卡片：同一 attachmentId
    snap_a = _card_snapshot(card_a, attachment_service=service)
    snap_b = _card_snapshot(card_b, attachment_service=service)
    assert snap_a["file_ref"]["attachment_id"] == snap_a["file_ref"]["attachment_id"] == att.id
    assert snap_a["file_ref"]["reference_id"] == snap_b["file_ref"]["reference_id"] == att.id

    removed = service.remove_reference(att.id)
    assert removed["removed"] is True
    assert source.read_bytes() == "共享内容".encode("utf-8"), "删除引用绝不删除源文件"

    after = _card_snapshot(card_a, attachment_service=service)
    assert after["file_ref"]["missing_reason"], "引用行删除后卡片必须如实说找不到附件记录"


def test_materials_entry_exposes_file_ref_and_note(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "互动材料.txt"
    source.write_text("材料正文", encoding="utf-8")
    att = service.register_source(
        path=str(source), source_kind="desktop_path", user_note="就是这份"
    )
    card = _file_card("card_mat", att.id)
    entry = materials._file_entry(
        card,
        "材料.txt",
        card["meta"],
        service=service,
        tool=None,
        max_chars=1000,
    )
    assert entry["file_ref"]["attachment_id"] == att.id
    assert entry["file_ref"]["user_note"] == "就是这份"
    assert entry["user_note"] == "就是这份"
    assert entry["material_enabled"] is True
    assert entry["source_kind"] == "desktop_path"
