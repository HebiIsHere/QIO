"""持续许可（契约 §2）与统一文字卡的材料 / 提交边界。

契约：docs/superpowers/plans/2026-10-12-unified-cards-media-preview-contract.md 第 2 节。

锁死的性质：

- 未许可文字卡**整体不交付**：卡内文字、链接、代码都不进材料与提交载荷；
- 遗留 code / url 卡在迁移完成前整体不可见（不回退成「材料默认可见」）；
- 许可持续有效：提交成功 / 失败 / 重复 / 重放 / 重启后 checked 仍为真；
- checkedCleared 在直连路径与 replay 路径上都**恒为空数组**，手动取消不会被旧回执复活；
- 已许可文字卡的多块合并成**一条** inline 材料（含链接地址与代码正文）；
- 多媒体诚实事实（audio / video）的来源值不被理解链路静默降级。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.interactive import (
    board_store,
    materials,
    models,
    submission,
    text_card,
    understanding,
)
from agent.services.attachments import AttachmentService
from agent.storage.db import connect

BOARD = "board_license"
CRID = "creq_license0001"


def _state(cards=(), groups=(), links=(), selection=()) -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups),
        "links": list(links),
        "selection": list(selection),
    }


def _save(conn: sqlite3.Connection, state: dict, reason: str = "op") -> dict:
    return board_store.save_board(conn, BOARD, state, reason=reason)


class _TurnAcceptor:
    """主循环受理器替身（阶段三起提交 = 受理一个主循环 turn）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs) -> dict:
        self.calls += 1
        return {"ok": True, "topicId": "topic-original", "turnId": f"turn_fake_{self.calls}"}


_ACCEPTOR = _TurnAcceptor()


def _submit(conn: sqlite3.Connection, **kwargs) -> dict:
    return asyncio.run(
        submission.submit_board(conn, BOARD, turn_acceptor=_ACCEPTOR, **kwargs)
    )


def _text_card(content: str = "", *, checked: bool = False, **fields) -> dict:
    return text_card.new_text_card(content, checked=checked, **fields)


def _rich_card() -> dict:
    text = (
        "把这三件事排个顺序\n"
        "\n"
        "```python\n  def f():\n      return 1\n```\n"
        "\n"
        "[QIO 仓库](https://github.com/HebiIsHere/QIO)\n"
    )
    return _text_card(text, checked=True)


@pytest.fixture()
def svc(db_conn: sqlite3.Connection, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


# -- 材料类卡片与遗留字符卡 -------------------------------------------------


def test_material_kinds_exclude_legacy_code_and_url():
    assert models.MATERIAL_KINDS == ("file",)
    assert "code" not in models.MATERIAL_KINDS
    assert "url" not in models.MATERIAL_KINDS
    # F4：结果卡（reply）与文字卡同一套许可语义（默认未勾选，勾选后持续有效）。
    assert models.CHECKABLE_KINDS == ("text", "reply")


def test_material_sources_include_multimedia_facts():
    for source in (
        "audio_delivered",
        "audio_transcribed",
        "audio_unsupported",
        "video_delivered",
        "video_sampled",
        "video_unsupported",
    ):
        assert source in models.MATERIAL_SOURCES, source


def test_legacy_code_and_url_cards_are_never_visible():
    code = models.new_card("code", "print(1)", meta={"language": "python"}, checked=True)
    url = models.new_card("url", "https://example.com", meta={"url": {"href": "https://example.com"}}, checked=True)
    file_card = models.new_card("file", "报告.pdf", meta={"name": "报告.pdf"})
    state = _state([code, url, file_card])

    assert models.visible_except_deleted(code) is False
    assert models.visible_except_deleted(url) is False
    assert models.visible_except_deleted(file_card) is True
    visible = models.selectable_ids(state)
    assert code["id"] not in visible and url["id"] not in visible
    assert file_card["id"] in visible


def test_legacy_cards_never_reach_materials(db_conn, svc):
    code = models.new_card("code", "print('不该出现的代码')", meta={"language": "python"})
    url = models.new_card("url", "https://secret.example", meta={"url": {"href": "https://secret.example"}})
    entries = materials.resolve_materials(
        db_conn, state=_state([code, url]), cards=[code, url], attachments=svc
    )
    assert entries == []


def test_unchecked_text_card_never_reaches_materials(db_conn, svc):
    card = _text_card("未许可的文字\n\n[链接](https://secret.example)\n\n```py\nsecret = 1\n```\n", checked=False)
    entries = materials.resolve_materials(db_conn, state=_state([card]), cards=[card], attachments=svc)
    assert entries == [], "未许可文字卡根本不进材料列表"


def test_checked_text_card_is_one_inline_entry(db_conn, svc):
    card = _rich_card()
    entries = materials.resolve_materials(db_conn, state=_state([card]), cards=[card], attachments=svc)
    assert len(entries) == 1, "同一张卡的多个块合并为一条 inline 材料"
    entry = entries[0]
    assert entry["source"] == "inline" and entry["read"] is True
    assert "def f():" in entry["text"], "代码正文逐字在材料里"
    assert "https://github.com/HebiIsHere/QIO" in entry["text"], "链接地址在材料里"
    assert "QIO 仓库" in entry["text"]
    assert any("同一张文字卡的正文" in note for note in entry["notes"])
    assert any("代码（python）" in note for note in entry["notes"])


# -- 持续许可：提交不改动勾选 ------------------------------------------------


def test_success_keeps_checked_and_checked_cleared_is_empty(db_conn):
    note = _text_card("提交之后许可仍然有效", checked=True)
    _save(db_conn, _state([note]))

    result = _submit(db_conn)
    assert result["status"] == "succeeded"
    assert result["checkedCleared"] == [], "契约 §2.2：提交不再取消勾选"
    stored = board_store.load_board(db_conn, BOARD)["state"]
    assert stored["cards"][0]["checked"] is True
    assert [card["id"] for card in result["after"]["cards"]] == [note["id"]]


def test_failure_keeps_checked(db_conn, monkeypatch):
    note = _text_card("失败也保留许可", checked=True)
    _save(db_conn, _state([note]))

    def boom(before, after):  # noqa: ANN001
        raise RuntimeError("模拟求差失败")

    monkeypatch.setattr(submission, "diff_states", boom)
    result = _submit(db_conn)
    assert result["status"] == "failed"
    assert result["checkedCleared"] == []
    assert "勾选已取消" not in models.dumps(result["delivery"])
    assert board_store.load_board(db_conn, BOARD)["state"]["cards"][0]["checked"] is True


def test_duplicate_submission_keeps_checked(db_conn):
    note = _text_card("重复提交也不改许可", checked=True)
    _save(db_conn, _state([note]))
    assert _submit(db_conn)["status"] == "succeeded"
    again = _submit(db_conn)
    assert again["status"] in ("duplicate", "empty")
    assert again["checkedCleared"] == []
    assert board_store.load_board(db_conn, BOARD)["state"]["cards"][0]["checked"] is True


def test_replay_keeps_checked_and_checked_cleared_is_empty(db_conn):
    note = _text_card("重放路径也不改许可", checked=True)
    seq = _save(db_conn, _state([note]))["seq"]
    first = _submit(db_conn, client_request_id=CRID, base_state_version=seq)
    assert first["status"] == "succeeded"
    assert first["checkedCleared"] == []

    replay = _submit(db_conn, client_request_id=CRID, base_state_version=seq)
    assert replay["replayed"] is True
    assert replay["checkedCleared"] == [], "replay 路径同样恒为空数组"
    assert replay["delivery"] == first["delivery"]
    assert board_store.load_board(db_conn, BOARD)["state"]["cards"][0]["checked"] is True


def test_manual_uncheck_is_not_resurrected_by_old_receipt(db_conn):
    note = _text_card("手动取消后不能被旧回执复活", checked=True)
    seq = _save(db_conn, _state([note]))["seq"]
    first = _submit(db_conn, client_request_id=CRID, base_state_version=seq)
    assert first["status"] == "succeeded"

    # 用户更晚的手动取消：这是一次新的板面保存
    unchecked = dict(note)
    unchecked["checked"] = False
    _save(db_conn, _state([unchecked]), reason="uncheck")

    replay = _submit(db_conn, client_request_id=CRID, base_state_version=seq)
    assert replay["replayed"] is True
    assert replay["checkedCleared"] == []
    stored = board_store.load_board(db_conn, BOARD)["state"]
    assert stored["cards"][0]["checked"] is False, "旧回执不得把用户更晚的取消恢复为开启"


def test_checked_survives_restart(db_conn, tmp_path):
    note = _text_card("重启后许可仍在", checked=True)
    _save(db_conn, _state([note]))
    assert _submit(db_conn)["status"] == "succeeded"

    row = db_conn.execute("PRAGMA database_list").fetchone()
    db_path = Path(row[2]) if row is not None else tmp_path / "test.db"
    other = connect(db_path)
    try:
        stored = board_store.load_board(other, BOARD)["state"]
        assert stored["cards"][0]["checked"] is True
    finally:
        other.close()


# -- 未许可文字卡整体不交付 --------------------------------------------------


SECRET = "未许可的文字不许出现在任何载荷里"
SECRET_LINK = "https://secret.example/never"
SECRET_CODE = "secret_token = '不该出现'"


def _secret_card() -> dict:
    text = (
        SECRET + "\n"
        "\n"
        "[" + SECRET + "](" + SECRET_LINK + ")\n"
        "\n"
        "```python\n" + SECRET_CODE + "\n```\n"
    )
    return _text_card(text, checked=False)


def test_unchecked_text_card_is_absent_from_submission_payload(db_conn):
    card = _secret_card()
    _save(db_conn, _state([card]))
    result = _submit(db_conn)
    assert result["status"] == "empty"
    payload = models.dumps(result)
    for secret in (SECRET, SECRET_LINK, SECRET_CODE):
        assert secret not in payload, secret
    assert result["before"]["cards"] == []
    assert result["after"]["cards"] == []


def _put_legacy_state(conn, state: dict, *, seq: int = 1) -> None:
    """把「升级前就存在」的板面直接写进库（模拟历史数据，不走 save_board 的规范化）。"""
    board_store.ensure_board(conn, board_id=BOARD)
    stamp = models.now_iso()
    dumped = models.dumps({**state, "boardId": BOARD, "seq": seq})
    conn.execute(
        "UPDATE board_states SET seq = ?, state = ?, reason = 'op', updated_at = ?"
        " WHERE board_id = ?",
        (seq, dumped, stamp, BOARD),
    )
    conn.execute(
        "INSERT INTO board_state_snapshots (id, board_id, seq, state, reason, created_at)"
        " VALUES (?, ?, ?, ?, 'op', ?)",
        (models.new_id("snap"), BOARD, seq, dumped, stamp),
    )
    conn.commit()


def test_legacy_code_card_submits_successfully_with_consistent_seq(db_conn):
    """真实提交路径（Lead 要求）：板面里有旧 code 卡时，提交必须**成功**（不是 StaleState），
    并且提交后的 seq 与库里完全一致 —— 保存内部的规范化不能悄悄把版本推高。"""
    code = models.new_card("code", "print('legacy')", meta={"language": "python"})
    saved = _save(db_conn, _state([code]))
    assert saved["seq"] == 1

    loaded = board_store.load_board(db_conn, BOARD)
    assert loaded["seq"] == 1, "保存即规范化：加载不会再偷偷 +1"
    assert loaded["migratedFromSeq"] is None
    assert loaded["state"]["cards"][0]["kind"] == "text"

    result = _submit(db_conn, base_state_version=1)
    assert result["status"] == "succeeded", result.get("error")
    assert result["checkedCleared"] == []
    stored_seq = db_conn.execute(
        "SELECT seq FROM board_states WHERE board_id = ?", (BOARD,)
    ).fetchone()["seq"]
    assert stored_seq == board_store.load_board(db_conn, BOARD)["seq"] == 1
    # 迁移后的旧代码卡是文字卡（整卡许可为真）：代码正文照常在可见范围里
    assert result["after"]["cards"][0]["kind"] == "text"
    assert "print('legacy')" in result["after"]["cards"][0]["content"]


def test_load_migration_does_not_make_inflight_submit_stale(db_conn):
    """升级后的第一次加载会迁移历史卡片（seq+1）：拿着**迁移前版本**的提交不算过期。

    没有这条容错，用户升级后第一次点提交就会看到「板面已更新，请重新读取」——
    而实际上内容一个字都没变，只是系统把旧卡迁成了文字卡。
    """
    code = models.new_card("code", "print('legacy-on-disk')", meta={"language": "python"})
    _put_legacy_state(db_conn, _state([code]), seq=1)

    # 升级后的第一次加载就是这次提交：迁移在提交内部顺带发生（seq 1→2），
    # 但这次提交仍然基于迁移前那个版本 1。
    result = _submit(db_conn, base_state_version=1)
    assert result["status"] == "succeeded", result.get("error")
    assert result["baseline"]["baseStateVersion"] == 1, "这次提交基于迁移前那个版本"

    row = db_conn.execute(
        "SELECT seq, reason FROM board_states WHERE board_id = ?", (BOARD,)
    ).fetchone()
    assert row["seq"] == 2, "迁移确实保存了一次（seq+1）"
    assert row["reason"] == "text_card_migration"
    stored = board_store.load_board(db_conn, BOARD)["state"]
    assert stored["cards"][0]["kind"] == "text"


def test_unchecked_text_card_is_absent_from_materials(db_conn, svc):
    card = _secret_card()
    entries = materials.resolve_materials(db_conn, state=_state([card]), cards=[card], attachments=svc)
    assert entries == []


# -- 理解链路：多媒体来源不被静默降级 ---------------------------------------


def test_understanding_keeps_video_sampled_source():
    normalized = understanding._normalize_material(
        {"cardId": "c1", "kind": "video", "source": "video_sampled", "text": "第 0 秒的帧"}, 0
    )
    assert normalized["source"] == "video_sampled", "P3 的诚实事实不得被静默降级成 unsupported"
    assert normalized["text"] == "第 0 秒的帧"


def test_understanding_keeps_new_multimedia_sources():
    for source in ("audio_transcribed", "audio_unsupported", "video_delivered"):
        normalized = understanding._normalize_material({"source": source, "text": "x"}, 0)
        assert normalized["source"] == source, source


def test_understanding_keeps_unknown_source_value_verbatim():
    """清单里暂时没有的来源也按原值保留，并如实记下「不在已知清单里」。"""
    normalized = understanding._normalize_material({"source": "future_fact", "text": "x"}, 0)
    assert normalized["source"] == "future_fact"
    assert "future_fact" in (normalized["reason"] or "")


def test_understanding_degrades_only_missing_source_with_reason():
    normalized = understanding._normalize_material({"text": "没有来源"}, 0)
    assert normalized["source"] == "unsupported"
    assert "来源" in (normalized["reason"] or ""), "确实不认识时要如实记原因"


def test_understanding_keeps_original_reason_when_adding_source_note():
    normalized = understanding._normalize_material(
        {"source": "future_fact", "reason": "上游给的原始原因"}, 0
    )
    assert normalized["reason"].startswith("上游给的原始原因")
    assert "future_fact" in normalized["reason"]
