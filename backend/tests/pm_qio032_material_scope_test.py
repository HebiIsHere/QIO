# -*- coding: utf-8 -*-
"""PM-QIO032 后端材料 / 三态授权 / 提交版本读取的定向用例（i-material 分支）。

覆盖任务书条目：

* #01 正式提交入口：TXT 正文与 PNG 图片在同一份提交材料里交付（不只看卡面）；
* #02 普通聊天收紧：板面工具只认程序绑定，未提交改动不进材料范围；
* #08/#54 三态（显示并参与 / 显示但不参与 / 隐藏且不参与）：提交、关系范围、
  工具读卡、附件读取、材料解析共用同一授权判断；迁移不扩大范围，恢复隐藏默认
  只回到「显示但不参与」；选择 / 框选 / 提交投影不改变三态；
* #28 提交的一轮只读提交当时的卡片 / 关系 / 材料版本（A 版），后保存 B 版不影响；
  源文件变化且旧内容不可得时提示重新提交，不悄悄替换。

全部用例只用本地 SQLite 与 fake 附件服务，不联网、不调用真实模型。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent.interactive import (
    board,
    board_store,
    materials,
    models,
    runtime,
    submission,
)
from agent.services.attachments import AttachmentService

BOARD = "board_qio032"
SUB = "sub_qio032_a"
TURN = "turn_qio032"

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# 共用帮助
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc(db_conn: sqlite3.Connection, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _ensure(conn: sqlite3.Connection) -> None:
    board_store.ensure_board(conn, board_id=BOARD)


def _saved_state(conn: sqlite3.Connection) -> dict:
    return board_store.current_state(conn, BOARD)


def _save(conn: sqlite3.Connection, state: dict) -> dict:
    board_store.save_board(conn, BOARD, state, reason="test")
    return _saved_state(conn)


def _ready(svc: AttachmentService, tmp_path: Path, name: str, body: bytes):
    source = tmp_path / ("src-" + name)
    source.write_bytes(body)
    att = svc.run_prepare(svc.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready", (att.state, att.error)
    return att


def _add_file_card(
    state: dict,
    *,
    att=None,
    name: str = "",
    size: int | None = None,
    included: bool = True,
) -> tuple[dict, str]:
    """加一张文件卡（meta.file 是卡片真实形状：name / attachmentId / size）。"""
    shown = name or (att.original_name if att is not None else "无名文件")
    att_id = "" if att is None else att.id
    block = {"name": shown, "attachmentId": att_id}
    if att is not None:
        block["size"] = int(size if size is not None else att.size_bytes)
    state = board.add_card(state, "file", "", meta={"name": shown, "file": block})
    card_id = str(state["cards"][-1]["id"])
    if not included:
        state = board.update_card(
            state, card_id, {"participation": models.PARTICIPATION_EXCLUDED}
        )
    return state, card_id


def _add_text_card(state: dict, content: str, *, included: bool = True) -> tuple[dict, str]:
    state = board.add_card(state, "text", content)
    card_id = str(state["cards"][-1]["id"])
    state = board.update_card(
        state,
        card_id,
        {
            "participation": (
                models.PARTICIPATION_INCLUDED if included else models.PARTICIPATION_EXCLUDED
            )
        },
    )
    return state, card_id


def _record_submission(
    conn: sqlite3.Connection,
    state: dict,
    submission_id: str = SUB,
    *,
    snapshot: dict | None = None,
    svc: "AttachmentService | None" = None,
) -> dict:
    """落一条真实形状的提交记录：visible / after_state 都是当时允许查看的投影。

    真实受理路径（submission.py）会在成功时把可见文件卡的内容版本（sha256）冻结进
    快照顶层 materialVersions；这里默认照做（svc 给定时），测试快照才与生产同形。
    snapshot 给定时直接用它（用于精确控制 frozen 版本）；svc=None 时保持旧记录形状
    （没有 materialVersions，用来验证「旧记录无法核对版本」的失败路径）。
    """
    if isinstance(snapshot, dict):
        snapshot = dict(snapshot)
    else:
        snapshot = submission.project_snapshot(state)
        if svc is not None:
            snapshot = materials.stamp_snapshot_material_versions(
                conn, snapshot, attachments=svc
            )
    conn.execute(
        "INSERT OR REPLACE INTO board_submissions (id, board_id, seq, status,"
        " before_state, after_state, visible, expressions, baseline, delivery,"
        " content_hash, error, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            submission_id,
            BOARD,
            1,
            "succeeded",
            models.dumps(submission._empty_projection(first_submission=True)),
            models.dumps(snapshot),
            models.dumps(snapshot),
            "[]",
            "{}",
            "{}",
            "",
            None,
            models.now_iso(),
        ),
    )
    return snapshot


@contextmanager
def _bound(submission_id: str = SUB):
    token = runtime.set_board_turn(
        runtime.BoardTurnContext(
            turn_id=TURN,
            mode=runtime.MODE_INTERACTIVE,
            board_id=BOARD,
            submission_id=submission_id,
            topic_id="topic_qio032",
            attempt=1,
        )
    )
    try:
        yield
    finally:
        runtime.clear_delivered_questions()
        runtime.reset_board_turn(token)


class _FakeApp:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn


def _tools(conn: sqlite3.Connection) -> dict:
    from agent.interactive.board_tools import build_board_tools

    return {tool.name: tool for tool in build_board_tools(_FakeApp(conn))}


# ---------------------------------------------------------------------------
# #01 / #28：提交材料只从提交当时的快照（A 版）交付
# ---------------------------------------------------------------------------


def test_submission_materials_deliver_txt_and_png_from_submission_snapshot(
    db_conn, svc, tmp_path
):
    """TXT 正文与 PNG 图片走同一份提交材料；提交后保存的 B 版不掺进来。"""
    _ensure(db_conn)
    txt = _ready(svc, tmp_path, "材料.txt", "第一行正文\n第二行正文".encode("utf-8"))
    png = _ready(svc, tmp_path, "图.png", _PNG_BYTES)
    excluded_att = _ready(svc, tmp_path, "排除材料.txt", b"excluded body")

    state = _saved_state(db_conn)
    state, text_id = _add_text_card(state, "把这两个文件按时间排序")
    state, txt_id = _add_file_card(state, att=txt)
    state, png_id = _add_file_card(state, att=png)
    state, excluded_id = _add_file_card(state, att=excluded_att, included=False)
    _save(db_conn, state)
    _record_submission(db_conn, state, svc=svc)

    # 提交之后保存 B 版：改正文、加新卡、把 A 版里的一张卡改成不参与。
    later = board.update_card(state, text_id, {"content": "B 版正文"})
    later, later_id = _add_text_card(later, "提交后才加的卡片")
    later = board.update_card(
        later, txt_id, {"participation": models.PARTICIPATION_EXCLUDED}
    )
    _save(db_conn, later)

    resolved = materials.submission_materials(
        db_conn, board_id=BOARD, submission_id=SUB, attachments=svc, vision=True
    )
    assert resolved["available"] is True, resolved["reason"]
    entries = {entry["cardId"]: entry for entry in resolved["materials"]}

    assert entries[text_id]["read"] is True
    assert entries[text_id]["text"] == "把这两个文件按时间排序", "必须交付 A 版正文"
    assert entries[txt_id]["read"] is True
    assert "第一行正文" in entries[txt_id]["text"], "TXT 正文必须交付"
    assert entries[png_id]["source"] == "image_delivered", entries[png_id]
    assert excluded_id not in entries, "提交时就不参与的材料不进范围，也不交付"
    assert excluded_id not in {item["cardId"] for item in resolved["images"]}
    assert later_id not in entries, "提交后新增的卡片不在 A 版材料范围"
    images = {item["cardId"]: item for item in resolved["images"]}
    assert images[png_id]["mediaType"] == "image/png"
    assert images[png_id]["dataUrl"].startswith("data:image/png;base64,")
    # 两路给的都是 A 版：PNG 在 materials 与 images 里都出现，TXT 只在正文里出现。
    assert {entry["cardId"] for entry in resolved["materials"]} == {
        text_id,
        txt_id,
        png_id,
    }


def test_submission_materials_mark_changed_source_as_needs_resubmit(db_conn, svc, tmp_path):
    """源文件在提交后变化、旧内容不可得时明确要求重新提交，绝不悄悄换成新内容。"""
    _ensure(db_conn)
    att = _ready(svc, tmp_path, "报告.txt", b"old body")
    state = _saved_state(db_conn)
    state, card_id = _add_file_card(state, att=att)
    _save(db_conn, state)
    _record_submission(db_conn, state, svc=svc)

    # 真实改动已保存的 QIO 副本（用户后来改了源文件再同步/写回）：长度变化。
    target = svc.content_target(att.id)
    path = Path(target[0]) if isinstance(target, tuple) else Path(target)
    path.write_bytes(b"new body that is definitely longer than the old one")

    resolved = materials.submission_materials(
        db_conn, board_id=BOARD, submission_id=SUB, attachments=svc
    )
    assert resolved["available"] is True
    stale = {item["cardId"]: item for item in resolved["stale"]}
    assert card_id in stale, resolved
    assert "重新提交" in stale[card_id]["reason"]
    entries = {entry["cardId"]: entry for entry in resolved["materials"]}
    assert entries[card_id]["read"] is False
    assert "new body" not in entries[card_id]["text"], "不能把变化后的新内容当旧材料悄悄交付"
    assert resolved["images"] == []


async def test_board_inspect_and_read_cards_only_see_submission_version(db_conn):
    """板面查看与读卡都返回 A 版；后续保存的 B 版不出现在本轮依据里。"""
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, a_id = _add_text_card(state, "A 版正文")
    state, a2_id = _add_text_card(state, "A 版第二张")
    state = board.add_link(state, a_id, a2_id, False, "A 版关系")
    link_id = str(state["links"][-1]["id"])
    _save(db_conn, state)
    _record_submission(db_conn, state)

    later = board.update_card(state, a_id, {"content": "B 版正文"})
    later = board.update_link(later, link_id, {"meaning": "B 版关系"})
    later, b_only_id = _add_text_card(later, "B 版新增")
    _save(db_conn, later)

    tools = _tools(db_conn)
    with _bound():
        inspected = await tools["board_inspect"].run()
        assert inspected.ok is True, inspected
        assert inspected.data["basis"]["source"] == "submission"
        cards = {item["id"]: item for item in inspected.data["cards"]}
        assert cards[a_id]["content_preview"] == "A 版正文"
        assert b_only_id not in cards
        assert [item["meaning"] for item in inspected.data["links"]] == ["A 版关系"], (
            "关系也必须是提交当时的版本"
        )

        read = await tools["board_read_cards"].run()  # 默认 latest
        assert read.ok is True, read
        assert read.data["basis"]["source"] == "submission"
        by_id = {item["id"]: item for item in read.data["cards"]}
        assert by_id[a_id]["content"] == "A 版正文"
        assert b_only_id not in by_id

        denied = await tools["board_read_cards"].run(card_ids=[b_only_id])
        assert denied.ok is False
        assert denied.code == "not_readable"


async def test_bound_turn_with_missing_snapshot_fails_and_asks_resubmit(db_conn):
    """#28：绑定提交但快照读不到 → 失败 + 建议重新提交，绝不返回当前板面（正文 / 关系）。

    这是本文件原来那条「快照缺失仍成功读取当前板面」用例的反转：旧行为把提交快照故障
    显示成一次正常读取（还带上了提交之后才保存的 B 版），正是 #28 要修的问题。
    """
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, card_id = _add_text_card(state, "当前板面正文")
    state, other_id = _add_text_card(state, "当前板面第二张")
    state = board.add_link(state, card_id, other_id, False, "当前板面关系")
    _save(db_conn, state)
    # 旧记录：提交行存在，但没有保存当时的可见范围快照（无法确认 A 版）。
    db_conn.execute(
        "INSERT OR IGNORE INTO board_submissions (id, board_id, seq, status, created_at)"
        " VALUES (?, ?, 1, 'accepted', ?)",
        (SUB, BOARD, models.now_iso()),
    )

    tools = _tools(db_conn)
    with _bound():
        results = {
            "board_inspect": await tools["board_inspect"].run(),
            "board_read_cards(latest)": await tools["board_read_cards"].run(),
            "board_read_cards(submission)": await tools["board_read_cards"].run(
                source="submission"
            ),
        }

    for label, result in results.items():
        assert result.ok is False, (label, result)
        assert result.status == "failed", (label, result.status)
        assert result.recoverable is False, (label, result.recoverable)
        assert result.error == "读不到这次提交的内容，请重新提交。", (label, result.error)
        assert result.data["next_step"] == "resubmit", label
        # 失败状态可识别：机器码 + basis 明确说「快照不可用」，诊断原因进详情。
        assert result.code in {
            "submission_snapshot_unavailable",
            "submission_not_found",
        }, (label, result.code)
        assert result.data["basis"]["available"] is False, (label, result.data["basis"])
        assert result.data["basis"]["source"] == "submission", (label, result.data["basis"])
        assert result.data["diagnostics"]["snapshot"], label
        # 当前板面的正文与关系一个字都不能出现（失败不等于「当前板面 / 零张卡」）。
        dumped = json.dumps(
            {"content": result.content, "error": result.error, "data": result.data},
            ensure_ascii=False,
        )
        assert "当前板面正文" not in dumped, (label, dumped)
        assert "当前板面第二张" not in dumped, (label, dumped)
        assert "当前板面关系" not in dumped, (label, dumped)
        assert "cards" not in result.data, "失败结果不能带板面卡片"
        assert "links" not in result.data, "失败结果不能带板面关系"
        assert "counts" not in result.data, "失败结果不能伪装成「零张卡」的正常结果"


async def test_bound_turn_without_submission_row_never_reads_current_board(db_conn):
    """提交行都不存在时同样失败：板面读取不给当前数据，显式读提交给出「找不到提交」。"""
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, card_id = _add_text_card(state, "当前板面正文")
    state, other_id = _add_text_card(state, "当前板面第二张")
    state = board.add_link(state, card_id, other_id, False, "当前板面关系")
    _save(db_conn, state)

    tools = _tools(db_conn)
    with _bound():
        inspected = await tools["board_inspect"].run()
        latest = await tools["board_read_cards"].run()
        explicit = await tools["board_read_cards"].run(source="submission")

    for result in (inspected, latest):
        assert result.ok is False, result
        assert result.code == "submission_snapshot_unavailable", result.code
        assert result.data["basis"]["available"] is False
    assert explicit.ok is False
    assert explicit.code == "submission_not_found"
    for result in (inspected, latest, explicit):
        dumped = json.dumps(
            {"content": result.content, "error": result.error, "data": result.data},
            ensure_ascii=False,
        )
        assert result.error == "读不到这次提交的内容，请重新提交。", result.error
        assert "当前板面正文" not in dumped
        assert "当前板面关系" not in dumped


def _insert_raw_submission(
    conn: sqlite3.Connection,
    *,
    submission_id: str,
    board_id: str,
    visible: object,
    after_state: object | None = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO board_submissions (id, board_id, seq, status,"
        " before_state, after_state, visible, expressions, baseline, delivery,"
        " content_hash, error, created_at)"
        " VALUES (?, ?, 1, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            submission_id,
            board_id,
            models.dumps(submission._empty_projection(first_submission=True)),
            models.dumps(after_state if after_state is not None else visible),
            models.dumps(visible),
            "[]",
            "{}",
            "{}",
            "",
            None,
            models.now_iso(),
        ),
    )


async def test_damaged_snapshot_is_failure_not_empty_board(db_conn):
    """提交快照损坏（形状不对）：按失败处理，绝不当成「正常空板面 / 零张卡」。"""
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, _card_id = _add_text_card(state, "当前板面正文")
    _save(db_conn, state)
    # cards 不是列表：JSON 还在但形状损坏，不能假装这是当时的可见范围。
    _insert_raw_submission(
        db_conn,
        submission_id=SUB,
        board_id=BOARD,
        visible={"cards": "broken", "groups": [], "links": []},
    )

    tools = _tools(db_conn)
    with _bound():
        inspected = await tools["board_inspect"].run()
        read = await tools["board_read_cards"].run()
        explicit = await tools["board_read_cards"].run(source="submission")
    for result in (inspected, read, explicit):
        assert result.ok is False, result
        assert result.code in {
            "submission_snapshot_unavailable",
            "submission_not_found",
        }, result.code
        assert "读不到这次提交的内容，请重新提交。" in (result.error or "")
        assert "当前板面正文" not in json.dumps(result.data, ensure_ascii=False)
        assert "cards" not in result.data, "损坏的快照不能被当成空板面"


async def test_snapshot_from_other_board_is_recognizable_failure(db_conn):
    """提交归属不匹配：读不到这次提交的内容 → 失败，不给当前板面数据。"""
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, _card_id = _add_text_card(state, "当前板面正文")
    _save(db_conn, state)
    # 另一块真实存在的板面（FK 要求板面行存在）：提交属于它，不属于本轮绑定板面。
    board_store.ensure_board(db_conn, board_id="board_other_28")
    _insert_raw_submission(
        db_conn,
        submission_id=SUB,
        board_id="board_other_28",
        visible={"cards": [], "groups": [], "links": [], "selection": []},
    )

    tools = _tools(db_conn)
    with _bound():
        inspected = await tools["board_inspect"].run()
        explicit = await tools["board_read_cards"].run(source="submission")
    assert inspected.ok is False, inspected
    assert inspected.code == "submission_snapshot_unavailable"
    assert "不属于" in inspected.data["diagnostics"]["snapshot"], inspected.data
    assert "当前板面正文" not in json.dumps(inspected.data, ensure_ascii=False)
    assert explicit.ok is False, explicit
    assert explicit.code == "submission_not_found"
    assert "当前板面正文" not in json.dumps(explicit.data, ensure_ascii=False)


async def test_legacy_snapshot_still_reads_available_history_cards(db_conn):
    """旧提交（没有 materialVersions）仍能查看当时可见的卡片；文件材料单独提示重新提交。"""
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, text_id = _add_text_card(state, "A 版正文")
    _save(db_conn, state)
    # 旧记录：有当时的可见范围投影，但没有 materialVersions（无法核对文件内容版本）。
    _insert_raw_submission(
        db_conn,
        submission_id=SUB,
        board_id=BOARD,
        visible=submission.project_snapshot(state),
    )
    later = board.update_card(state, text_id, {"content": "B 版正文"})
    _save(db_conn, later)

    tools = _tools(db_conn)
    with _bound():
        read = await tools["board_read_cards"].run()
        explicit = await tools["board_read_cards"].run(source="submission")
    assert read.ok is True, read
    assert read.data["basis"]["source"] == "submission"
    by_id = {item["id"]: item for item in read.data["cards"]}
    assert by_id[text_id]["content"] == "A 版正文", "旧提交可用的历史卡片仍要能查看"
    assert explicit.ok is True, explicit
    assert explicit.data["source"] == "submission"


# ---------------------------------------------------------------------------
# #01 / #02 / #08 / #28：附件读取与提交范围同一授权判断
# ---------------------------------------------------------------------------


async def test_read_attachment_scope_matches_submission_included_materials(db_conn, svc, tmp_path):
    """已参与材料的附件可读；提交后新增的附件不在 A 版范围，读取被拒绝。"""
    from agent.tools.attachment_tools import ReadAttachmentTool

    _ensure(db_conn)
    old_att = _ready(svc, tmp_path, "旧材料.txt", b"old material body")
    later_att = _ready(svc, tmp_path, "新材料.txt", b"later material body")

    state = _saved_state(db_conn)
    state, _old_id = _add_file_card(state, att=old_att)
    _save(db_conn, state)
    _record_submission(db_conn, state, svc=svc)
    later = board.add_card(_saved_state(db_conn), "file", "", meta={"name": "新材料.txt", "file": {"name": "新材料.txt", "attachmentId": later_att.id, "size": later_att.size_bytes}})
    _save(db_conn, later)

    tool = ReadAttachmentTool(svc)
    with _bound():
        allowed = await tool.run(attachment_id=old_att.id)
        assert allowed.ok is True, allowed
        assert "old material body" in allowed.content
        denied = await tool.run(attachment_id=later_att.id)
        assert denied.ok is False
        assert denied.code == "not_in_submission_scope"
        assert "提交" in denied.error


async def test_board_tools_reject_unbound_normal_mode(db_conn):
    """普通聊天没有程序绑定：板面工具一律拒绝（未提交改动读不到）。"""
    _ensure(db_conn)
    tools = _tools(db_conn)
    inspected = await tools["board_inspect"].run()
    assert inspected.ok is False
    assert inspected.code == "interactive_unavailable"
    read = await tools["board_read_cards"].run()
    assert read.ok is False


# ---------------------------------------------------------------------------
# #08 / #54：三态语义与迁移
# ---------------------------------------------------------------------------


def test_three_state_roundtrip_restore_defaults_to_displayed_but_not_included(db_conn):
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, file_id = _add_file_card(state, name="材料.txt")
    assert models.card_participation(
        models.card_by_id(state, file_id)
    ) == models.PARTICIPATION_INCLUDED, "新文件材料默认参与"

    state = board.set_hidden(state, file_id, True)
    card = models.card_by_id(state, file_id)
    assert card["hidden"] is True
    assert models.card_participation(card) == models.PARTICIPATION_EXCLUDED, "隐藏 = 不参与"

    restored = board.set_hidden(state, file_id, False)
    card = models.card_by_id(restored, file_id)
    assert card["hidden"] is False
    assert models.card_participation(card) == models.PARTICIPATION_EXCLUDED, (
        "恢复隐藏默认只恢复为「显示但不参与」，不能自动回到参与"
    )

    state = board.set_participation(restored, file_id, True)
    card = models.card_by_id(state, file_id)
    assert models.card_participation(card) == models.PARTICIPATION_INCLUDED
    assert card["hidden"] is False

    state = board.set_participation(state, file_id, False)
    assert models.card_participation(
        models.card_by_id(state, file_id)
    ) == models.PARTICIPATION_EXCLUDED
    assert models.card_by_id(state, file_id)["hidden"] is False, "不参与不是隐藏"


def test_legacy_conflicts_never_expand_reading_scope():
    """旧配置冲突取不会扩大读取范围的状态；旧卡迁移保留排除意图。"""
    raw = models.empty_state("board_legacy")
    raw["cards"] = [
        # 冲突：旧数据一边隐藏一边还写着参与 → 隐藏优先（不扩大读取）。
        models.new_card("text", "隐藏且旧勾选", checked=True, hidden=True),
        # 显式 excluded 与旧 checked=true 冲突 → 显式不参与优先。
        {**models.new_card("text", "显式排除但旧勾选", checked=True), "participation": "excluded"},
        # 旧 code / url 卡：迁移完成前一律不可交付。
        models.new_card("code", "print(1)"),
        models.new_card("url", "https://example.com"),
        # 旧文字卡未勾选 → 排除（保留排除意图）。
        models.new_card("text", "未勾选注释", checked=False),
        # 旧文件材料默认参与（旧规则默认可见）。
        models.new_card("file", "", meta={"file": {"name": "旧材料.txt"}}),
        # 显式参与 → 参与。
        {**models.new_card("text", "明确参与", checked=True), "participation": "included"},
    ]
    state = board.normalize_state(raw)
    cards = state["cards"]
    assert len(cards) == 7
    by_participation = [card["participation"] for card in cards]
    assert by_participation == [
        "excluded",  # 隐藏优先
        "excluded",  # 显式排除优先
        "excluded",  # 旧 code 卡
        "excluded",  # 旧 url 卡
        "excluded",  # 旧未勾选文字卡
        "included",  # 旧文件材料默认参与
        "included",  # 显式参与
    ], by_participation
    assert cards[0]["hidden"] is True
    visible = models.selectable_ids(state)
    assert visible == {cards[5]["id"], cards[6]["id"]}


async def test_three_state_consistent_across_submission_links_tools_and_materials(db_conn, svc):
    """提交投影、关系范围、工具读卡、材料解析用的是同一套授权判断。"""
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, included_id = _add_text_card(state, "参与的注释")
    state, excluded_id = _add_text_card(state, "不参与的注释", included=False)
    state, hidden_id = _add_text_card(state, "隐藏的注释")
    state = board.set_hidden(state, hidden_id, True)
    # 关系：参与↔参与可见；参与↔排除 / 参与↔隐藏不可见。
    state = board.add_link(state, included_id, excluded_id, False, "无效关系")
    state = board.add_link(state, included_id, hidden_id, False, "隐藏关系")
    _save(db_conn, state)
    state = _saved_state(db_conn)

    expected = models.selectable_ids(state)
    assert included_id in expected and excluded_id not in expected and hidden_id not in expected

    # 1) 提交投影
    projected = submission.project_snapshot(state)
    assert {card["id"] for card in projected["cards"]} == expected
    # 2) 关系范围
    visible_links = models.visible_links(state)
    for link in visible_links:
        assert link["src"] in expected and link["dst"] in expected
    assert all(link["meaning"] != "隐藏关系" for link in visible_links)
    # 3) 工具读卡
    tools = _tools(db_conn)
    with _bound(submission_id=""):
        inspected = await tools["board_inspect"].run()
        assert inspected.ok is True, inspected
        assert {item["id"] for item in inspected.data["cards"]} == expected
        denied = await tools["board_read_cards"].run(card_ids=[excluded_id])
        assert denied.ok is False and denied.code == "not_readable"
    # 4) 材料解析
    entries = materials.resolve_materials(
        db_conn, state=state, cards=None, attachments=svc
    )
    assert {entry["cardId"] for entry in entries} == expected


def test_selection_and_submission_projection_do_not_change_participation(db_conn):
    """预览 / 选择 / 框选 / 提交投影都不改变三态。"""
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, a_id = _add_text_card(state, "参与")
    state, b_id = _add_text_card(state, "不参与", included=False)

    def snapshot_participation() -> dict:
        return {
            card["id"]: models.card_participation(card) for card in state["cards"]
        }

    before = snapshot_participation()
    state = board.set_selection(state, [a_id, b_id])
    assert snapshot_participation() == before
    state = board.select_in_rect(
        state, {"x": -10.0, "y": -10.0, "w": 5000.0, "h": 5000.0}, False
    )
    assert snapshot_participation() == before
    submission.project_snapshot(state)
    submission.visible_range(state)
    assert snapshot_participation() == before
    assert models.card_participation(
        models.card_by_id(state, b_id)
    ) == models.PARTICIPATION_EXCLUDED

# ---------------------------------------------------------------------------
# #08/#54：三态入口（界面标签与后端授权同一映射）
# ---------------------------------------------------------------------------


def test_card_access_state_maps_three_states_and_restore_defaults_to_displayed(db_conn):
    _ensure(db_conn)
    state = _saved_state(db_conn)
    state, card_id = _add_text_card(state, "三态用例")

    assert board.card_access_state(models.card_by_id(state, card_id)) == board.ACCESS_INCLUDED

    state = board.set_card_access_state(state, card_id, board.ACCESS_DISPLAYED)
    card = models.card_by_id(state, card_id)
    assert board.card_access_state(card) == board.ACCESS_DISPLAYED
    assert card["hidden"] is False
    assert models.card_participation(card) == models.PARTICIPATION_EXCLUDED

    state = board.set_card_access_state(state, card_id, board.ACCESS_HIDDEN)
    card = models.card_by_id(state, card_id)
    assert board.card_access_state(card) == board.ACCESS_HIDDEN
    assert card["hidden"] is True
    assert models.card_participation(card) == models.PARTICIPATION_EXCLUDED

    # 恢复隐藏只回到「显示但不参与」：无论经由哪个入口。
    restored = board.set_card_access_state(state, card_id, board.ACCESS_DISPLAYED)
    card = models.card_by_id(restored, card_id)
    assert board.card_access_state(card) == board.ACCESS_DISPLAYED
    assert card["hidden"] is False
    assert models.card_participation(card) == models.PARTICIPATION_EXCLUDED

    legacy_restore = board.set_hidden(state, card_id, False)
    assert board.card_access_state(
        models.card_by_id(legacy_restore, card_id)
    ) == board.ACCESS_DISPLAYED

    back = board.set_card_access_state(restored, card_id, board.ACCESS_INCLUDED)
    card = models.card_by_id(back, card_id)
    assert board.card_access_state(card) == board.ACCESS_INCLUDED
    assert card["hidden"] is False

    # 非法取值不改状态（不猜）。
    assert board.set_card_access_state(back, card_id, "unknown") == board.normalize_state(back)


def test_conflicting_raw_card_is_hidden_never_included():
    card = {**models.new_card("text", "冲突"), "hidden": True, "participation": "included"}
    state = board.normalize_state({"cards": [card], "groups": [], "links": [], "selection": []})
    normalized = state["cards"][0]
    assert board.card_access_state(normalized) == board.ACCESS_HIDDEN
    assert models.card_participation(normalized) == models.PARTICIPATION_EXCLUDED


# ---------------------------------------------------------------------------
# #26 事件旁路：intent_service.publish_intents_changed 缺失 / 失败都不影响工具结果
# ---------------------------------------------------------------------------


def _turn_for_events() -> runtime.BoardTurnContext:
    return runtime.BoardTurnContext(
        turn_id=TURN,
        mode=runtime.MODE_INTERACTIVE,
        board_id=BOARD,
        submission_id=SUB,
    )


async def test_propose_intents_publishes_changed_event(monkeypatch, db_conn):
    from types import SimpleNamespace

    from agent.interactive import intent_service

    _ensure(db_conn)
    calls: list[dict] = []

    async def fake_publish(conn, **kwargs):
        calls.append(kwargs)

    def fake_propose(conn, **kwargs):
        return {"ok": True, "intents": [{"intent_id": "i_1", "title": "t"}]}

    monkeypatch.setattr(intent_service, "publish_intents_changed", fake_publish, raising=False)
    monkeypatch.setattr(intent_service, "propose_intents", fake_propose)
    tools = _tools(db_conn)
    args = SimpleNamespace(submission_id=SUB, intents=[{"client_ref": "a"}], resolved_question_ids=[])
    result = await tools["board_propose_intents"]._run(_turn_for_events(), args)
    assert result.ok is True, result
    assert calls == [
        {
            "board_id": BOARD,
            "submission_id": SUB,
            "intent_ids": ["i_1"],
            "reason": "proposed",
        }
    ], calls


async def test_intent_event_failure_never_changes_tool_result(monkeypatch, db_conn):
    from types import SimpleNamespace

    from agent.interactive import intent_service

    _ensure(db_conn)

    async def broken_publish(conn, **kwargs):
        raise RuntimeError("bus unavailable")

    def fake_update(conn, **kwargs):
        return {
            "ok": True,
            "requires_confirmation": True,
            "intent": {"id": kwargs.get("intent_id"), "submission_id": SUB},
        }

    monkeypatch.setattr(intent_service, "publish_intents_changed", broken_publish, raising=False)
    monkeypatch.setattr(intent_service, "update_intent", fake_update)
    tools = _tools(db_conn)
    args = SimpleNamespace(
        intent_id="i_1", expected_version=1, changes={"title": "改"}, resolved_question_ids=[]
    )
    result = await tools["board_update_intent"]._run(_turn_for_events(), args)
    assert result.ok is True, result
    assert result.code == "awaiting_confirmation"


async def test_intent_event_hook_tolerates_missing_helper(monkeypatch, db_conn):
    """helper 没合入时工具照常工作（事件不是工具成功的条件）。"""
    from types import SimpleNamespace

    from agent.interactive import intent_service

    _ensure(db_conn)
    monkeypatch.delattr(intent_service, "publish_intents_changed", raising=False)

    def fake_propose(conn, **kwargs):
        return {"ok": True, "intents": [{"intent_id": "i_1"}]}

    monkeypatch.setattr(intent_service, "propose_intents", fake_propose)
    tools = _tools(db_conn)
    args = SimpleNamespace(submission_id=SUB, intents=[{"client_ref": "a"}], resolved_question_ids=[])
    result = await tools["board_propose_intents"]._run(_turn_for_events(), args)
    assert result.ok is True, result

# ---------------------------------------------------------------------------
# #01 素材数据源细化 / #28 同大小内容替换（提交受理冻结 materialVersions）
# ---------------------------------------------------------------------------


def test_txt_file_card_body_is_delivered_as_attachment_text(db_conn, svc, tmp_path):
    """参与项 TXT 文件卡：source=attachment_text、read=true、text 是真实正文。"""
    _ensure(db_conn)
    txt = _ready(svc, tmp_path, "材料.txt", "第一段正文：预算\n第二段正文：排期".encode("utf-8"))
    state = _saved_state(db_conn)
    state, card_id = _add_file_card(state, att=txt, name="材料.txt")
    _save(db_conn, state)
    _record_submission(db_conn, state, svc=svc)

    resolved = materials.submission_materials(
        db_conn, board_id=BOARD, submission_id=SUB, attachments=svc
    )
    entry = {item["cardId"]: item for item in resolved["materials"]}[card_id]
    assert entry["source"] == "attachment_text", entry
    assert entry["read"] is True
    assert "第一段正文：预算" in entry["text"] and "第二段正文：排期" in entry["text"]
    assert entry["attachmentId"] == txt.id
    assert resolved["stale"] == []


def test_hidden_and_post_submission_files_are_not_delivered(db_conn, svc, tmp_path):
    """隐藏的文件卡、提交后新增的文件卡都不进首轮材料范围（不泄漏）。"""
    _ensure(db_conn)
    included_att = _ready(svc, tmp_path, "参与材料.txt", b"included body")
    hidden_att = _ready(svc, tmp_path, "隐藏材料.txt", b"hidden body")
    later_att = _ready(svc, tmp_path, "提交后新增.txt", b"later body")

    state = _saved_state(db_conn)
    state, included_id = _add_file_card(state, att=included_att, name="参与材料.txt")
    state, hidden_id = _add_file_card(state, att=hidden_att, name="隐藏材料.txt")
    state = board.set_hidden(state, hidden_id, True)
    _save(db_conn, state)
    _record_submission(db_conn, state, svc=svc)

    later = board.add_card(
        _saved_state(db_conn),
        "file",
        "",
        meta={"name": "提交后新增.txt", "file": {"name": "提交后新增.txt", "attachmentId": later_att.id, "size": later_att.size_bytes}},
    )
    _save(db_conn, later)

    resolved = materials.submission_materials(
        db_conn, board_id=BOARD, submission_id=SUB, attachments=svc
    )
    entry_ids = {item["cardId"] for item in resolved["materials"]}
    assert included_id in entry_ids
    assert hidden_id not in entry_ids
    assert set(resolved["attachmentIds"]) == {included_att.id}
    scope = materials.submission_attachment_scope(
        db_conn, board_id=BOARD, submission_id=SUB, attachments=svc
    )
    assert scope["available"] is True
    assert set(scope["allowed"]) == {included_att.id}
    assert later_att.id not in scope["allowed"] and hidden_att.id not in scope["allowed"]


def test_not_ready_attachment_is_stale_never_delivered(db_conn, svc, tmp_path):
    """附件未就绪：如实标 stale（提示重新提交），不交付任何正文。"""
    _ensure(db_conn)
    source = tmp_path / "src-准备中.txt"
    source.write_bytes(b"pending body")
    pending = svc.prepare(str(source), topic_id="t1")
    assert pending.state == "prepared"

    state = _saved_state(db_conn)
    state, card_id = _add_file_card(
        state, att=pending, name="准备中.txt", size=len(b"pending body")
    )
    _save(db_conn, state)
    _record_submission(db_conn, state, svc=svc)

    resolved = materials.submission_materials(
        db_conn, board_id=BOARD, submission_id=SUB, attachments=svc
    )
    stale = {item["cardId"]: item for item in resolved["stale"]}
    assert card_id in stale
    assert "重新提交" in stale[card_id]["reason"]
    entry = {item["cardId"]: item for item in resolved["materials"]}[card_id]
    assert entry["read"] is False and entry["text"] == ""
    assert "pending body" not in entry["text"]


def test_same_size_content_replacement_is_detected_with_stamped_versions(db_conn, svc, tmp_path):
    """同大小内容替换：受理时冻结的 sha256 能识别，提示重新提交而不是悄悄换内容。"""
    _ensure(db_conn)
    old_body = b"AAAA-old-body"
    new_body = b"BBBB-new-body"  # 与 old_body 等长
    assert len(old_body) == len(new_body)
    att = _ready(svc, tmp_path, "同大小.txt", old_body)

    state = _saved_state(db_conn)
    state, card_id = _add_file_card(state, att=att, name="同大小.txt")
    _save(db_conn, state)
    snapshot = submission.project_snapshot(state)
    stamped = materials.stamp_snapshot_material_versions(
        db_conn, snapshot, attachments=svc
    )
    version = stamped["materialVersions"][card_id]
    assert version["attachmentId"] == att.id
    assert version["sha256"], "受理时必须能冻结真实内容哈希"
    _record_submission(db_conn, state, snapshot=stamped)

    # 真实内容被同大小替换（旧实现按 size 看不出来）。
    target = svc.content_target(att.id)
    path = Path(target[0]) if isinstance(target, tuple) else Path(target)
    path.write_bytes(new_body)

    resolved = materials.submission_materials(
        db_conn, board_id=BOARD, submission_id=SUB, attachments=svc
    )
    stale = {item["cardId"]: item for item in resolved["stale"]}
    assert card_id in stale, resolved["stale"]
    assert "内容哈希" in stale[card_id]["reason"]
    assert "重新提交" in stale[card_id]["reason"]
    entry = {item["cardId"]: item for item in resolved["materials"]}[card_id]
    assert entry["read"] is False
    assert "BBBB" not in entry["text"], "不能把替换后的新内容当旧材料交付"
    assert resolved["materialVersions"][card_id]["sha256"] == version["sha256"]


def test_stamp_snapshot_material_versions_only_adds_a_key(db_conn, svc, tmp_path):
    """冻结版本只加顶层键，不改卡片 / 不参与前后求差。"""
    _ensure(db_conn)
    att = _ready(svc, tmp_path, "版本材料.txt", b"versioned body")
    state = _saved_state(db_conn)
    state, _card_id = _add_file_card(state, att=att, name="版本材料.txt")
    _save(db_conn, state)
    snapshot = submission.project_snapshot(state)
    stamped = materials.stamp_snapshot_material_versions(db_conn, snapshot, attachments=svc)
    assert stamped["cards"] == snapshot["cards"]
    assert stamped["groups"] == snapshot["groups"]
    assert stamped["links"] == snapshot["links"]
    assert stamped["selection"] == snapshot["selection"]
    assert set(stamped) - set(snapshot) == {"materialVersions"}
    # 指纹 / 求差不因为多了这个键而把「没改」判成「改过」。
    assert submission.content_fingerprint(stamped) == submission.content_fingerprint(snapshot)


# ---------------------------------------------------------------------------
# #08/#54 最终形态：旧卡迁移后恢复隐藏仍是「显示但不参与」
# ---------------------------------------------------------------------------


def test_legacy_hidden_card_restore_stays_displayed_not_included(db_conn):
    _ensure(db_conn)
    state = _saved_state(db_conn)
    legacy = {
        **models.new_card("text", "旧卡隐藏且勾选", checked=True, hidden=True),
        "participation": "included",  # 旧配置冲突：一边隐藏一边写着参与
    }
    state = board.normalize_state(
        {**state, "cards": [legacy], "groups": [], "links": [], "selection": []}
    )
    card_id = str(state["cards"][0]["id"])
    assert board.card_access_state(state["cards"][0]) == board.ACCESS_HIDDEN
    assert models.card_participation(state["cards"][0]) == models.PARTICIPATION_EXCLUDED

    # 用户恢复显示：只回到「显示但不参与」，不会借迁移把参与意图扩大回来。
    restored = board.set_hidden(state, card_id, False)
    card = models.card_by_id(restored, card_id)
    assert board.card_access_state(card) == board.ACCESS_DISPLAYED
    assert models.card_participation(card) == models.PARTICIPATION_EXCLUDED
    _save(db_conn, restored)
    assert models.selectable_ids(_saved_state(db_conn)) == set()

    # 旧文件材料（旧规则默认可见）迁移后仍是参与，不能被「旧卡一律排除」误伤。
    legacy_file = models.new_card(
        "file", "", meta={"name": "旧材料.txt", "file": {"name": "旧材料.txt"}}
    )
    other = board.normalize_state(
        {**state, "cards": [legacy_file], "groups": [], "links": [], "selection": []}
    )
    assert models.card_participation(
        other["cards"][0]
    ) == models.PARTICIPATION_INCLUDED
    assert board.card_access_state(other["cards"][0]) == board.ACCESS_INCLUDED
