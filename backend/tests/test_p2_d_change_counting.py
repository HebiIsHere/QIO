# -*- coding: utf-8 -*-
"""D 独立契约测试：变化计数只记录正式生效的新增 / 实质修改（契约 §12.3）。

被验证的公开接口（契约冻结）：

    agent.maintenance.changes.record_effective_change(
        conn, *, knowledge_id, chain_id, version, source, occurred_at=None) -> bool

要点：
* 只在知识正式生效（active）的新增 / 实质修改时由生命周期写入；
* 唯一键 (chain_id, version) 幂等；
* 未审批（draft / pending_review）、后台通知、失败重试、本轮维护自身产生的版本都不计数；
* maintenance_* 来源（MAINTENANCE_ORIGINS）不计数。

基线说明：阶段二实现未合入时模块 / 迁移 46 不存在，本文件会**失败**（不是 skip），
失败信息写明契约条款。运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_change_counting.py -q
"""

from __future__ import annotations

import importlib

import pytest

CLAUSE = "契约 §12.3"


def _changes_module():
    try:
        return importlib.import_module("agent.maintenance.changes")
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 agent.maintenance.changes 不可导入；"
            f"阶段二 A 的变化计数实现尚未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


def _require_log(conn):
    have = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "knowledge_change_log" not in have:
        from agent.storage.migrate import current_version

        pytest.fail(
            f"{CLAUSE}：缺少 knowledge_change_log 表（schema 版本 "
            f"{current_version(conn)}）；迁移 46 尚未登记到 storage/schema.py。",
            pytrace=False,
        )


def _rows(conn) -> list:
    return list(conn.execute(
        "SELECT seq, knowledge_id, chain_id, version, source "
        "FROM knowledge_change_log ORDER BY seq"
    ))


def _active(conn, content: str, *, category: str = "general_fact", provenance: dict | None = None):
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(conn)
    item = ks.create(category=category, content=content, provenance=provenance)
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    return item, ks


# ---------------------------------------------------------------------------
# 接口本身的幂等与来源
# ---------------------------------------------------------------------------


def test_record_effective_change_is_idempotent_per_chain_version(db_conn):
    """§12.3：唯一键 (chain_id, version) 幂等（同一版本重复写只留一行）。"""
    changes = _changes_module()
    _require_log(db_conn)

    first = changes.record_effective_change(
        db_conn, knowledge_id="kn_a", chain_id="chain_x", version=1, source="main_chat"
    )
    second = changes.record_effective_change(
        db_conn, knowledge_id="kn_a", chain_id="chain_x", version=1, source="main_chat"
    )
    assert first is True, f"{CLAUSE}：首次记录应返回 True"
    assert second is False, f"{CLAUSE}：同一 (chain_id, version) 重复记录必须幂等返回 False"
    rows = _rows(db_conn)
    assert len(rows) == 1, f"{CLAUSE}：幂等失效，落了两行：{rows}"
    assert rows[0]["chain_id"] == "chain_x" and rows[0]["version"] == 1


def test_record_keeps_occurred_at_optional(db_conn):
    """§12.5/§12.3：occurred_at 可省略；不同版本各自一行。"""
    changes = _changes_module()
    _require_log(db_conn)
    assert changes.record_effective_change(
        db_conn, knowledge_id="kn_v1", chain_id="chain_y", version=1, source="main_chat",
        occurred_at="2026-10-21T00:00:00+00:00",
    ) is True
    assert changes.record_effective_change(
        db_conn, knowledge_id="kn_v2", chain_id="chain_y", version=2, source="main_chat",
    ) is True
    rows = _rows(db_conn)
    assert [int(r["version"]) for r in rows] == [1, 2]


# ---------------------------------------------------------------------------
# 生命周期钩子：只在正式生效时记录
# ---------------------------------------------------------------------------


def test_draft_and_pending_review_are_not_counted(db_conn):
    """§12.3：未审批提议（draft / pending_review）不计数。"""
    _changes_module()
    _require_log(db_conn)
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(db_conn)
    item = ks.create(category="general_fact", content="还未获批的知识")
    assert _rows(db_conn) == [], f"{CLAUSE}：draft 阶段不应产生变化记录"
    ks.submit(item.id)
    assert _rows(db_conn) == [], f"{CLAUSE}：pending_review 阶段不应产生变化记录"
    ks.verify(item.id, verified_by="user")
    assert _rows(db_conn) == [], f"{CLAUSE}：verified 但未激活不构成「正式生效」"
    ks.activate(item.id)
    rows = _rows(db_conn)
    assert len(rows) == 1, f"{CLAUSE}：activate 成功后必须记录一条变化，实际 {rows}"
    assert rows[0]["knowledge_id"] == item.id
    assert int(rows[0]["version"]) == 1


def test_activation_records_real_chain_and_source(db_conn):
    """§12.3：source 取 provenance 的来源键；chain_id / version 与真实版本链一致。"""
    _changes_module()
    _require_log(db_conn)
    item, ks = _active(db_conn, "一条主对话确认的知识", provenance={"source": "main_chat"})
    rows = _rows(db_conn)
    assert len(rows) == 1, f"{CLAUSE}：正式生效的一条知识应只有一条变化记录：{rows}"
    row = rows[0]
    current = ks.get(item.id)
    assert row["chain_id"] == current.chain_id, f"{CLAUSE}：chain_id 与版本链不一致"
    assert int(row["version"]) == int(current.version)
    assert row["source"] == "main_chat", (
        f"{CLAUSE}：source 应取 provenance 的来源键（main_chat），实际 {row['source']!r}")


def test_reactivating_active_row_does_not_duplicate(db_conn):
    """§12.3：activate 幂等（已经是 active）不得重复计数。"""
    _changes_module()
    _require_log(db_conn)
    item, ks = _active(db_conn, "已经生效的知识")
    assert len(_rows(db_conn)) == 1
    ks.activate(item.id)  # 幂等调用
    assert len(_rows(db_conn)) == 1, f"{CLAUSE}：重复 activate 不得再记一次变化"


def test_failed_activation_leaves_no_change_row(db_conn):
    """§12.3：失败 / 非法状态转换不留下变化记录（不能把失败重试当变化）。"""
    _changes_module()
    _require_log(db_conn)
    item, ks = _active(db_conn, "很快会被撤销的知识")
    ks.revoke(item.id)
    before = _rows(db_conn)
    with pytest.raises((ValueError, KeyError)):
        ks.activate(item.id)
    assert _rows(db_conn) == before, f"{CLAUSE}：失败的 activate 不应产生变化记录"


def test_substantive_revision_is_counted_with_new_version(db_conn):
    """§12.3：实质修改（revise_atomic 产生新版本）要计数，且是新版本号。"""
    _changes_module()
    _require_log(db_conn)
    item, ks = _active(db_conn, "用户每天喝两杯咖啡")
    revised = ks.revise_atomic(item.id, fields={"content": "用户每天喝三杯咖啡"})
    rows = _rows(db_conn)
    assert len(rows) == 2, f"{CLAUSE}：新增 + 一次实质修改应各记一条：{rows}"
    assert int(rows[-1]["version"]) == int(revised.version) == 2
    assert rows[-1]["knowledge_id"] == revised.id
    assert rows[-1]["chain_id"] == revised.chain_id


def test_maintenance_origins_are_not_counted(db_conn):
    """§12.3/§12.4：维护自身产生的版本（maintenance_*）不参与变化计数。"""
    import importlib as _il

    models = _il.import_module("agent.maintenance.models")
    _changes_module()
    _require_log(db_conn)
    item, ks = _active(db_conn, "重复的知识一")
    before = len(_rows(db_conn))
    for origin in models.MAINTENANCE_ORIGINS:
        if origin != models.SOURCE_MAINTENANCE_NORMALIZE:
            continue
        revised = ks.revise_atomic(
            item.id,
            source=origin,
            fields={"content": "重复的知识一（格式整理）"},
        )
        assert revised.state.value == "active"
    after = _rows(db_conn)
    assert len(after) == before, (
        f"{CLAUSE}：maintenance_* 来源的版本不得计入变化计数；"
        f"before={before} after={len(after)} rows={after}")


def test_notification_and_retry_do_not_emit_change_records(db_conn):
    """§12.3：后台通知 / 失败重试不产生变化记录（它们不是知识生效）。"""
    _changes_module()
    _require_log(db_conn)
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(db_conn)
    # 失败重试路径：同一版本被重复提交/验证失败，不会变成 active
    item = ks.create(category="general_fact", content="一条会被拒绝的提议")
    ks.submit(item.id)
    ks.reject(item.id)
    assert _rows(db_conn) == [], f"{CLAUSE}：被打回的提议不计数（重试也不）"
