# -*- coding: utf-8 -*-
"""D 独立契约测试：一轮后台维护真正执行规则（契约 §12.4）。

覆盖：merge（>=2 来源 / 保存 merged_from / 撤销来源保留历史）、normalize（恰好 1 个）、
ask（不改知识 / 不重复催问）、无效候选拒绝且不撤销原知识、不同类别 / 归属 / 确定程度
不自动合并、非法旧动作（expire / revoke）不作为回退、输出修复最多一次。

装置：真实 AppContext + fake adapter（模型一律 fake，不联网、不用真实 Key）。契约对
B 的公开入口是 `MaintenanceRunner(ctx).run_batch(snapshot)`；快照由 A 的冻结数据结构构造。

基线说明：阶段二实现未合入时 runner / 迁移 47 不存在，本文件会**失败**（不是 skip）。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_maintenance_run.py -q
"""

from __future__ import annotations

import importlib
import json

import pytest

CLAUSE = "契约 §12.4"


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段二实现尚未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


def _require_tables(conn, *tables: str):
    have = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in tables if t not in have]
    if missing:
        from agent.storage.migrate import current_version as _ver

        pytest.fail(
            f"{CLAUSE}：缺少表 {missing}（schema 版本 {_ver(conn)}）；"
            "迁移 46/47 尚未登记到 storage/schema.py。",
            pytrace=False,
        )


@pytest.fixture()
def ctx(tmp_path, db_conn):
    _load("agent.maintenance.runner")
    _require_tables(db_conn, "knowledge_change_log", "maintenance_batches",
                    "maintenance_items", "maintenance_operations")
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext

    return AppContext(Settings(data_dir=tmp_path), db_conn, EventBus())


def _active(conn, content: str, *, category: str = "general_fact",
            provenance: dict | None = None, node_ids=None) -> str:
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(conn)
    item = ks.create(category=category, content=content, provenance=provenance,
                     node_ids=node_ids)
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    return item.id


def _snapshot(conn, knowledge_ids, *, trigger_name: str = "change_count"):
    models = _load("agent.maintenance.models")
    rows = list(conn.execute(
        "SELECT seq FROM knowledge_change_log ORDER BY seq"))
    seqs = [int(r["seq"]) for r in rows]
    return models.MaintenanceSnapshot(
        batch_seq=1,
        trigger=models.MaintenanceTrigger(trigger_name),
        change_seq_max=max(seqs) if seqs else 0,
        pending_change_count=len(seqs),
        waiting_turns=0,
        change_ids=tuple(seqs),
        knowledge_ids=tuple(knowledge_ids),
        limits=dict(models.MAINTENANCE_LIMITS),
    )


def _runner(ctx):
    module = _load("agent.maintenance.runner")
    return module.MaintenanceRunner(ctx)


class FakeAdapter:
    """按队列返回模型输出；记录每次调用收到的 messages（供修复依据断言）。"""

    def __init__(self, payloads, *, default=None) -> None:
        if isinstance(payloads, str):
            payloads = [payloads]
        self._payloads = list(payloads)
        self._default = default if default is not None else (
            self._payloads[-1] if self._payloads else "{}")
        self.calls = 0
        self.message_texts: list[str] = []

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        from agent.adapters.base import ChatMessage, Completion

        self.calls += 1
        text = self._payloads.pop(0) if self._payloads else self._default
        parts: list[str] = []
        for message in messages or []:
            content = getattr(message, "content", None)
            parts.append(str(content or ""))
        self.message_texts.append("\n".join(parts))
        return Completion(message=ChatMessage(role="assistant", content=text))


def _fake_build(adapter):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=adapter)


def _payload(candidates) -> str:
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def _active_rows(conn, content: str | None = None):
    if content is None:
        return list(conn.execute(
            "SELECT id, content, state, provenance FROM knowledge WHERE state = 'active'"))
    return list(conn.execute(
        "SELECT id, content, state, provenance FROM knowledge "
        "WHERE state = 'active' AND content = ?", (content,)))


def _prov(row) -> dict:
    try:
        data = json.loads(row["provenance"] or "{}")
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _rows_containing(conn, table: str, needle: str) -> list:
    hits = []
    for row in conn.execute(f"SELECT * FROM {table}"):
        for key in row.keys():
            value = row[key]
            if isinstance(value, str) and needle in value:
                hits.append(row)
                break
    return hits


def _item_id(row) -> str:
    keys = set(row.keys())
    for name in ("id", "item_id", "maintenance_item_id"):
        if name in keys and row[name]:
            return str(row[name])
    raise AssertionError(f"maintenance_items 行缺少可识别的 id 列：{sorted(keys)}")


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


async def test_merge_creates_new_version_and_keeps_sources_history(ctx, monkeypatch):
    """§12.4：merge >=2 个不同 ID；新版本 provenance 保存 merged_from（id/version/chain_id）；
    撤销来源但保留完整历史。"""
    from agent.knowledge.lifecycle import KnowledgeService

    conn = ctx.conn
    first = _active(conn, "用户喜欢喝咖啡")
    second = _active(conn, "用户平时喝咖啡")
    adapter = FakeAdapter(_payload([{
        "action": "merge",
        "knowledge_ids": [first, second],
        "new_content": "用户平时喜欢喝咖啡",
        "reason": "语义重复",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))

    outcome = await _runner(ctx).run_batch(
        _snapshot(conn, [first, second], trigger_name="change_count"))
    models = _load("agent.maintenance.models")
    assert isinstance(outcome, models.MaintenanceRunOutcome), (
        f"{CLAUSE}：run_batch 必须返回 MaintenanceRunOutcome")
    assert outcome.ok is True and outcome.applied >= 1, (
        f"{CLAUSE}：合法 merge 应执行成功（outcome={outcome}）")

    merged_rows = _active_rows(conn, "用户平时喜欢喝咖啡")
    assert len(merged_rows) == 1, f"{CLAUSE}：merge 后应有一条新的 active 知识：{merged_rows}"
    merged = merged_rows[0]
    prov = _prov(merged)
    assert prov.get("source") == models.SOURCE_MAINTENANCE_MERGE, (
        f"{CLAUSE}：merge 新版本来源必须标记为 maintenance_merge，实际 {prov.get('source')!r}")
    merged_from = prov.get("merged_from")
    assert isinstance(merged_from, list) and len(merged_from) == 2, (
        f"{CLAUSE}：新版本 provenance 必须保存 merged_from（两个来源），实际 {merged_from!r}")
    for source in merged_from:
        assert isinstance(source, dict) and {"id", "version", "chain_id"} <= set(source), (
            f"{CLAUSE}：merged_from 每个来源都要有 id / version / chain_id，实际 {source!r}")
    assert {str(s["id"]) for s in merged_from} == {first, second}

    ks = KnowledgeService(conn)
    for kid in (first, second):
        item = ks.get(kid)
        assert item is not None, f"{CLAUSE}：撤销来源不得删除历史行（{kid} 消失了）"
        assert item.state.value == "revoked", (
            f"{CLAUSE}：merge 后来源应为 revoked（保留历史），实际 {item.state.value}")


async def test_maintenance_itself_does_not_create_change_records(ctx, monkeypatch):
    """§12.3/§12.8：维护自身产生的版本不记录为待处理变化（不形成循环触发）。"""
    conn = ctx.conn
    first = _active(conn, "用户常用命令行")
    second = _active(conn, "用户习惯用命令行")
    before = int(conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_change_log").fetchone()["n"])
    adapter = FakeAdapter(_payload([{
        "action": "merge",
        "knowledge_ids": [first, second],
        "new_content": "用户常用命令行工作",
        "reason": "语义重复",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).run_batch(_snapshot(conn, [first, second]))
    assert outcome.ok is True and outcome.applied >= 1
    maintenance_rows = list(conn.execute(
        "SELECT source FROM knowledge_change_log WHERE source LIKE 'maintenance%'"))
    assert maintenance_rows == [], (
        f"{CLAUSE}：维护自身版本不得进入变化计数（实际 {maintenance_rows}）")
    after = int(conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge_change_log").fetchone()["n"])
    assert after == before, (
        f"{CLAUSE}：维护产生的 active 版本不应新增待处理变化（{before} -> {after}）")


async def test_merge_rejects_unknown_id_and_keeps_originals(ctx, monkeypatch):
    """§12.4：无效候选（未知 ID）必须拒绝该项并保留原记录，不得回落旧 revoke / expire。"""
    conn = ctx.conn
    kid = _active(conn, "用户每周游泳两次")
    adapter = FakeAdapter(_payload([{
        "action": "merge",
        "knowledge_ids": [kid, "kn_ghost_does_not_exist"],
        "new_content": "用户每周游泳两次（被合并）",
        "reason": "测试无效候选",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).run_batch(_snapshot(conn, [kid]))

    row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["state"] == "active", (
        f"{CLAUSE}：无效候选不得撤销原知识（原记录必须保持 active）")
    active = _active_rows(conn)
    assert len(active) == 1 and active[0]["id"] == kid, (
        f"{CLAUSE}：无效候选不得产生新版本 / 不得撤销原知识：{active}")
    assert outcome.rejected >= 1 or outcome.ok is False, (
        f"{CLAUSE}：无效候选应体现在结果里（rejected>=1 或 ok=False），实际 {outcome}")


async def test_cross_category_merge_is_rejected(ctx, monkeypatch):
    """§12.4：不同类别不得自动合成更强更宽的认识。"""
    conn = ctx.conn
    fact = _active(conn, "用户住在杭州", category="general_fact")
    profile = _active(conn, "用户是软件工程师", category="user_profile")
    adapter = FakeAdapter(_payload([{
        "action": "merge",
        "knowledge_ids": [fact, profile],
        "new_content": "用户住在杭州并且是软件工程师",
        "reason": "跨类别合并",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    await _runner(ctx).run_batch(_snapshot(conn, [fact, profile]))

    for kid in (fact, profile):
        row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
        assert row["state"] == "active", (
            f"{CLAUSE}：跨类别 merge 必须被拒绝，原知识保持 active（{kid}）")
    assert _active_rows(conn, "用户住在杭州并且是软件工程师") == []


async def test_merge_with_different_scope_is_rejected(ctx, monkeypatch):
    """§12.4：不同归属 / 范围不得自动合并。"""
    conn = ctx.conn
    from agent.graph.nodes import NodeService

    topic_node = NodeService(conn).create_topic("阶段二 D 范围测试话题")
    global_id = _active(conn, "用户偏好安静的环境")
    scoped_id = _active(conn, "用户偏好安静的环境（某话题内）", node_ids=[topic_node.id])
    adapter = FakeAdapter(_payload([{
        "action": "merge",
        "knowledge_ids": [global_id, scoped_id],
        "new_content": "用户偏好安静的环境（合并后）",
        "reason": "跨归属合并",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    await _runner(ctx).run_batch(_snapshot(conn, [global_id, scoped_id]))

    for kid in (global_id, scoped_id):
        row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
        assert row["state"] == "active", (
            f"{CLAUSE}：不同归属的 merge 必须被拒绝，原知识保持 active（{kid}）")
    assert _active_rows(conn, "用户偏好安静的环境（合并后）") == []


async def test_merge_with_different_epistemic_status_is_rejected(ctx, monkeypatch):
    """§12.4：不同确定程度不得自动合并成更强认识。"""
    from agent.maintenance.models import SOURCE_MAIN_CHAT

    conn = ctx.conn
    explicit = _active(
        conn, "用户对花生过敏",
        provenance={"source": SOURCE_MAIN_CHAT, "epistemic_status": "user_explicit"})
    inferred = _active(
        conn, "用户可能对花生过敏",
        provenance={"source": SOURCE_MAIN_CHAT, "epistemic_status": "qio_inference"})
    adapter = FakeAdapter(_payload([{
        "action": "merge",
        "knowledge_ids": [explicit, inferred],
        "new_content": "用户对花生过敏",
        "reason": "确定程度不同",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    await _runner(ctx).run_batch(_snapshot(conn, [explicit, inferred]))

    for kid in (explicit, inferred):
        row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
        assert row["state"] == "active", (
            f"{CLAUSE}：不同确定程度的 merge 必须被拒绝，原知识保持 active（{kid}）")
    # 合并内容与 explicit 原文相同：只能存在原知识本身，不得出现第二条同内容 active 版本
    same_content = _active_rows(conn, "用户对花生过敏")
    assert [row["id"] for row in same_content] == [explicit], (
        f"{CLAUSE}：不同确定程度的 merge 不得产生新的合并版本：{[(r['id'], r['content']) for r in same_content]}")


# ---------------------------------------------------------------------------
# normalize / ask
# ---------------------------------------------------------------------------


async def test_normalize_revises_one_entry_in_same_chain(ctx, monkeypatch):
    """§12.4：normalize 恰好一个 ID，只做不改变原意的格式整理；版本链保留。"""
    conn = ctx.conn
    kid = _active(conn, "用户喜欢喝：手冲咖啡")
    adapter = FakeAdapter(_payload([{
        "action": "normalize",
        "knowledge_ids": [kid],
        "new_content": "用户喜欢喝手冲咖啡。",
        "reason": "标点与空白整理",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).run_batch(_snapshot(conn, [kid]))

    assert outcome.ok is True and outcome.applied >= 1, (
        f"{CLAUSE}：合法 normalize 应执行（outcome={outcome}）")
    from agent.knowledge.lifecycle import KnowledgeService

    revised = KnowledgeService(conn).resolve_current(kid)
    assert revised is not None
    assert revised.content == "用户喜欢喝手冲咖啡。", (
        f"{CLAUSE}：normalize 后当前版本内容应更新，实际 {revised.content!r}")
    assert revised.id != kid, f"{CLAUSE}：normalize 必须通过新版本实现，不能原地覆盖"
    assert revised.chain_id == KnowledgeService(conn).get(kid).chain_id, (
        f"{CLAUSE}：normalize 新版本必须留在同一版本链")
    old = KnowledgeService(conn).get(kid)
    assert old.state.value == "revoked" and old.content == "用户喜欢喝：手冲咖啡", (
        f"{CLAUSE}：旧版本要被保留（历史不删除）")


async def test_ask_persists_pending_item_without_touching_knowledge(ctx, monkeypatch):
    """§12.4：ask 提供具体问题、不直接改知识；事项持久保存为待回答。"""
    conn = ctx.conn
    kid = _active(conn, "用户可能下个月搬家")
    question = "用户下个月确实要搬家吗？"
    adapter = FakeAdapter(_payload([{
        "action": "ask",
        "knowledge_ids": [kid],
        "question": question,
        "reason": "涉及有效性变化，需要用户确认",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).run_batch(_snapshot(conn, [kid]))
    assert outcome.ok is True and outcome.asked >= 1, (
        f"{CLAUSE}：ask 应登记为一个待回答事项（outcome={outcome}）")

    row = conn.execute("SELECT state, content FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["state"] == "active" and row["content"] == "用户可能下个月搬家", (
        f"{CLAUSE}：ask 不得直接修改知识")
    hits = _rows_containing(conn, "maintenance_items", question)
    assert hits, f"{CLAUSE}：ask 的问题必须持久保存（maintenance_items 里找不到问题原文）"
    models = _load("agent.maintenance.models")
    # 持久化行的内部状态名与读取模型枚举分层：C 的 PendingItemView 把 kind=ask 映射为
    # PendingKind.MAINTENANCE_QUESTION；这里接受「内部 ask」或「读取模型枚举」两种表示，
    # 真正面向用户的 kind/handler 由 API 契约测试（test_p2_d_api_pending）核对。
    kinds = {str(value) for hit in hits for value in hit}
    assert (models.PendingKind.MAINTENANCE_QUESTION.value in kinds
            or "ask" in kinds), (
        f"{CLAUSE}：ask 事项的类型必须可被识别为后台维护问题（实际 {kinds}）")

    # 同一事项已经等待回答：不重复催问
    before = len(list(conn.execute("SELECT * FROM maintenance_items")))
    adapter2 = FakeAdapter(_payload([{
        "action": "ask",
        "knowledge_ids": [kid],
        "question": question,
        "reason": "重复提出",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter2))
    await _runner(ctx).run_batch(_snapshot(conn, [kid]))
    after = len(list(conn.execute("SELECT * FROM maintenance_items")))
    assert after == before, (
        f"{CLAUSE}：已等待回答的同一事项不得重复提出（{before} -> {after}）")


# ---------------------------------------------------------------------------
# 输出修复 / 旧动作回退
# ---------------------------------------------------------------------------


async def test_invalid_output_repairs_at_most_once_and_keeps_data(ctx, monkeypatch):
    """§12.4：输出修复最多一次且有字段错误依据；失败保留原数据。"""
    conn = ctx.conn
    kid = _active(conn, "用户每天早上跑步")
    bad = _payload([{"action": "merge", "knowledge_ids": [kid], "new_content": "x"}])
    adapter = FakeAdapter([bad, bad])
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).run_batch(_snapshot(conn, [kid]))
    assert adapter.calls <= 2, (
        f"{CLAUSE}：输出修复最多一次（模型调用应 <= 2），实际 {adapter.calls} 次")
    assert outcome.ok is False or outcome.rejected >= 1, (
        f"{CLAUSE}：非法输出必须如实体现在结果里，实际 {outcome}")
    row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["state"] == "active", f"{CLAUSE}：失败保留原数据（原知识不得被撤销）"
    if len(adapter.message_texts) >= 2:
        repair_input = adapter.message_texts[1]
        assert any(token in repair_input for token in ("未通过校验", "修复", "至少", "new_content")), (
            f"{CLAUSE}：第二次调用必须带上明确的字段错误依据（实际输入未含修复依据）")


async def test_legacy_expire_or_revoke_output_is_not_a_fallback(ctx, monkeypatch):
    """§12.4：旧 revoke / expire 不得作为新维护动作的回退；出现即拒绝且不动原知识。"""
    conn = ctx.conn
    kid = _active(conn, "用户喜欢用键盘工作")
    for legacy_action in ("expire", "revoke", "delete"):
        adapter = FakeAdapter(_payload([{
            "action": legacy_action,
            "knowledge_ids": [kid],
            "reason": "旧动作",
        }]))
        monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
        outcome = await _runner(ctx).run_batch(_snapshot(conn, [kid]))
        row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
        assert row["state"] == "active", (
            f"{CLAUSE}：旧动作 {legacy_action!r} 不得执行（原知识必须保持 active）")
        assert outcome.ok is False or outcome.rejected >= 1, (
            f"{CLAUSE}：旧动作 {legacy_action!r} 应被拒绝并如实记入结果")


async def test_invalid_candidate_does_not_revoke_original(ctx, monkeypatch):
    """§12.4：非法候选（缺新内容 / 未知动作）拒绝该项，原记录保持不动。"""
    conn = ctx.conn
    kid = _active(conn, "用户每周末看电影")
    adapter = FakeAdapter(_payload([{
        "action": "normalize",
        "knowledge_ids": [kid],
        "new_content": "",
        "reason": "缺新内容",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).run_batch(_snapshot(conn, [kid]))
    row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["state"] == "active", f"{CLAUSE}：非法候选不得撤销原知识"
    assert outcome.ok is False or outcome.rejected >= 1
