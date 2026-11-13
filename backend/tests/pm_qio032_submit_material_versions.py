# -*- coding: utf-8 -*-
"""PM-QIO032 #28 接线：提交受理时冻结材料内容版本（同大小内容替换可识别）。

背景：提交后的材料读取只按「附件缺失 / 非 ready / size 不一致」判断变化，
源文件被**同大小内容替换**时识别不到，会把新内容静默当成提交当时的材料。
i-material 在 materials.py 提供 stamp_snapshot_material_versions（只给提交投影加
顶层 materialVersions，不改 cards / 不参与求差与指纹）；本文件验证 i-submit 在
submission.py 受理成功路径把它接到 visible / after 两个快照上。

断言：
1. 正常提交仍成功，after_state / visible 顶层带 materialVersions（含 sha256），
   且 content_fingerprint / diff 语义不变（重复提交照旧 duplicate）；
2. 同大小内容替换后：submission_materials / submission_attachment_scope 返回
   stale（含「重新提交」且拒绝交付新内容），materials 条目 read=false、正文不含新内容。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.interactive import board, board_store, materials, models, submission
from agent.services.attachments import AttachmentService

BOARD = "board_pm032_matver"


class RecordingAcceptor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        return {
            "ok": True,
            "turnId": f"turn_matver_{len(self.calls)}",
            "topicId": "topic_matver",
        }


@pytest.fixture()
def env(db_conn, tmp_path):
    from agent.storage import migration_p3_a

    migration_p3_a.apply(db_conn)
    service = AttachmentService(db_conn, tmp_path / "data")
    owner = SimpleNamespace(_attachment_service=lambda: service)
    # 生产路径由 AppContext.__init__ 绑定；这里用最小宿主让 submission.py 的打标
    # 拿到同一份附件服务（不传时它会按 DB 位置自行推导，测试里显式给更稳）。
    submission.bind_background_owner(db_conn, owner)
    try:
        yield db_conn, service, tmp_path
    finally:
        submission.unbind_background_owner(db_conn)


def _ready(service: AttachmentService, tmp_path: Path, name: str, body: bytes):
    source = tmp_path / ("src-" + name)
    source.write_bytes(body)
    att = service.run_prepare(service.prepare(str(source), topic_id="t1").id)
    assert att.state == "ready", (att.state, att.error)
    return att


def _seed_file_card(conn, att, name: str) -> str:
    board_store.ensure_board(conn, board_id=BOARD)
    state = board_store.current_state(conn, BOARD)
    card = models.new_card(
        "file", "", meta={"name": name, "file": {"name": name, "attachmentId": att.id, "size": int(att.size_bytes)}}
    )
    state = board.add_card(state, "file", "", meta=card["meta"])
    card_id = str(state["cards"][-1]["id"])
    state = board.update_card(
        state, card_id, {"participation": models.PARTICIPATION_INCLUDED}
    )
    board_store.save_board(conn, BOARD, state, reason="seed-material-version")
    return card_id


def _submit(conn) -> dict:
    acceptor = RecordingAcceptor()
    result = asyncio.run(submission.submit_board(conn, BOARD, turn_acceptor=acceptor))
    return result


def _row(conn, submission_id: str):
    return conn.execute(
        "SELECT after_state, visible FROM board_submissions WHERE id = ?",
        (submission_id,),
    ).fetchone()


def test_successful_submission_freezes_material_versions_without_changing_semantics(env):
    conn, service, tmp_path = env
    att = _ready(service, tmp_path, "笔记.txt", b"AAAA")
    card_id = _seed_file_card(conn, att, "笔记.txt")

    result = _submit(conn)
    assert result["status"] == "succeeded", result
    submission_id = result["submission"]["id"]

    row = _row(conn, submission_id)
    after = models.loads(row["after_state"], {})
    visible = models.loads(row["visible"], {})
    assert isinstance(after.get("materialVersions"), dict), "受理成功必须冻结材料内容版本"
    assert card_id in after["materialVersions"], after["materialVersions"]
    fact = after["materialVersions"][card_id]
    assert fact["attachmentId"] == att.id
    assert len(str(fact.get("sha256") or "")) == 64, "必须冻结内容哈希（同大小替换识别依据）"
    assert isinstance(visible.get("materialVersions"), dict)
    assert visible["materialVersions"][card_id]["sha256"] == fact["sha256"]
    # 只加一个顶层键：卡片 / 组 / 关系 / 选择不改，指纹与求差语义不变。
    without = {key: value for key, value in after.items() if key != "materialVersions"}
    assert submission.content_fingerprint(without) == submission.content_fingerprint(after)

    # 同内容再提交仍判 duplicate（指纹不受新顶层键影响，基准/新快照都可比）。
    again = _submit(conn)
    assert again["status"] == "duplicate", again
    assert again["baseline"]["updated"] is False


def test_same_size_content_replacement_is_stale_and_not_delivered(env):
    conn, service, tmp_path = env
    att = _ready(service, tmp_path, "同大小.txt", b"AAAA")
    card_id = _seed_file_card(conn, att, "同大小.txt")

    result = _submit(conn)
    assert result["status"] == "succeeded", result
    submission_id = result["submission"]["id"]

    # 用户后来在同一路径写回**同样大小**的不同内容（只按 size 核对是看不出来的）。
    target = service.content_target(att.id)
    stored = Path(target[0]) if isinstance(target, tuple) else Path(target)
    stored.write_bytes(b"BBBB")

    resolved = materials.submission_materials(
        conn, board_id=BOARD, submission_id=submission_id, attachments=service
    )
    assert resolved["available"] is True
    stale = {item["cardId"]: item for item in resolved["stale"]}
    assert card_id in stale, ("同大小内容替换必须被识别为 stale", resolved.get("stale"))
    reason = stale[card_id]["reason"]
    assert "重新提交" in reason
    assert "内容" in reason and ("哈希" in reason or "不一致" in reason)
    entry = next(item for item in resolved["materials"] if item["cardId"] == card_id)
    assert entry["read"] is False
    assert "BBBB" not in str(entry.get("text") or ""), "不能把替换后的新内容当成旧材料交付"
    assert entry["source"] == "unreadable"

    scope = materials.submission_attachment_scope(
        conn, board_id=BOARD, submission_id=submission_id, attachments=service
    )
    assert att.id in scope["stale"], "读授权范围也必须按内容版本核对后报 stale"
    assert "重新提交" in scope["stale"][att.id]
