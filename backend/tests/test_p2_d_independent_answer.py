# -*- coding: utf-8 -*-
"""D 独立契约测试：独立回答真正执行规则（契约 §12.4）。

覆盖：revise / revoke / needs_clarification / deferred / no_change 的语义、
版本冲突保护（CAS，冲突不改并如实记录）、只作用于事项允许的对象、
后台问答不进入主对话 messages / 摘要 / 用户轮数。

装置：真实 AppContext + fake adapter。事项先用 run_batch 的 ask 产生（真实路径），
再用 `MaintenanceRunner(ctx).answer_question(item_id, answer)` 处理。

基线说明：阶段二实现未合入时 runner / 迁移 47 不存在，本文件会**失败**（不是 skip）。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_independent_answer.py -q
"""

from __future__ import annotations

import importlib
import json

import pytest

CLAUSE = "契约 §12.4"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段二实现尚未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


def _require_tables(conn):
    have = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in ("knowledge_change_log", "maintenance_batches",
                           "maintenance_items") if t not in have]
    if missing:
        from agent.storage.migrate import current_version

        pytest.fail(
            f"{CLAUSE}：缺少表 {missing}（schema 版本 {current_version(conn)}）；"
            "迁移 46/47 尚未登记到 storage/schema.py。",
            pytrace=False,
        )


@pytest.fixture()
def ctx(tmp_path, db_conn):
    _load("agent.maintenance.runner")
    _require_tables(db_conn)
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext

    return AppContext(Settings(data_dir=tmp_path), db_conn, EventBus())


def _active(conn, content: str, *, category: str = "general_fact") -> str:
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(conn)
    item = ks.create(category=category, content=content)
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    return item.id


def _snapshot(conn, knowledge_ids):
    models = _load("agent.maintenance.models")
    rows = list(conn.execute("SELECT seq FROM knowledge_change_log ORDER BY seq"))
    seqs = [int(r["seq"]) for r in rows]
    return models.MaintenanceSnapshot(
        batch_seq=1,
        trigger=models.MaintenanceTrigger.CHANGE_COUNT,
        change_seq_max=max(seqs) if seqs else 0,
        pending_change_count=len(seqs),
        waiting_turns=0,
        change_ids=tuple(seqs),
        knowledge_ids=tuple(knowledge_ids),
        limits=dict(models.MAINTENANCE_LIMITS),
    )


def _runner(ctx):
    return _load("agent.maintenance.runner").MaintenanceRunner(ctx)


class FakeAdapter:
    def __init__(self, payloads) -> None:
        self._payloads = list(payloads) if not isinstance(payloads, str) else [payloads]
        self._default = self._payloads[-1] if self._payloads else "{}"
        self.calls = 0

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        from agent.adapters.base import ChatMessage, Completion

        self.calls += 1
        text = self._payloads.pop(0) if self._payloads else self._default
        return Completion(message=ChatMessage(role="assistant", content=text))


def _fake_build(adapter):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=adapter)


def _payload(candidates) -> str:
    return json.dumps({"candidates": candidates}, ensure_ascii=False)


def _resolution(resolution: str, *, changes=None, question=None) -> str:
    body: dict = {"resolution": resolution, "changes": changes or []}
    if question is not None:
        body["question"] = question
    return json.dumps(body, ensure_ascii=False)


def _rows_containing(conn, table: str, needle: str) -> list:
    hits = []
    for row in conn.execute(f"SELECT * FROM {table}"):
        for key in row.keys():
            value = row[key]
            if isinstance(value, str) and needle in value:
                hits.append(row)
                break
    return hits


def _item_key(row) -> str:
    keys = set(row.keys())
    for name in ("id", "item_id", "maintenance_item_id"):
        if name in keys and row[name]:
            return name
    raise AssertionError(f"maintenance_items 行缺少可识别的 id 列：{sorted(keys)}")


def _item_id(row) -> str:
    return str(row[_item_key(row)])


def _item_row(conn, item_id: str):
    for row in conn.execute("SELECT * FROM maintenance_items"):
        key = _item_key(row)
        if str(row[key]) == item_id:
            return row
    return None


def _item_status(conn, item_id: str) -> str:
    row = _item_row(conn, item_id)
    assert row is not None, f"{CLAUSE}：找不到维护事项 {item_id}"
    for name in ("status", "state"):
        if name in set(row.keys()):
            return str(row[name])
    for key in row.keys():
        value = row[key]
        if isinstance(value, str) and value in {
            "awaiting_answer", "awaiting_approval", "processing", "processed",
            "deferred", "rejected", "failed",
        }:
            return value
    raise AssertionError(f"maintenance_items 行没有可读状态：{sorted(row.keys())}")


async def _ask(ctx, monkeypatch, kid: str, question: str) -> str:
    adapter = FakeAdapter(_payload([{
        "action": "ask",
        "knowledge_ids": [kid],
        "question": question,
        "reason": "需要用户确认",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).run_batch(_snapshot(ctx.conn, [kid]))
    assert outcome.asked >= 1, f"{CLAUSE}：ask 未登记事项（outcome={outcome}）"
    hits = _rows_containing(ctx.conn, "maintenance_items", question)
    assert hits, f"{CLAUSE}：找不到刚登记的维护事项（问题原文未持久化）"
    return _item_id(hits[0])


def _counts(conn) -> dict[str, int]:
    def count(table: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    counts = {"messages": count("messages"), "fragments": count("fragments")}
    try:
        counts["turn_journal"] = count("turn_journal")
    except Exception:  # noqa: BLE001 - 既有库必有；没有就不比较
        pass
    try:
        row = conn.execute(
            "SELECT turn_seq FROM knowledge_maintenance_state LIMIT 1").fetchone()
        counts["turn_seq"] = int(row["turn_seq"]) if row is not None else 0
    except Exception:  # noqa: BLE001
        pass
    return counts


# ---------------------------------------------------------------------------
# 五种 resolution 语义
# ---------------------------------------------------------------------------


async def test_answer_revise_applies_new_version(ctx, monkeypatch):
    """§12.4：revise 要求非空 new_content；产生新版本并保存修改依据。"""
    from agent.knowledge.lifecycle import KnowledgeService

    conn = ctx.conn
    kid = _active(conn, "用户下周去上海出差")
    item_id = await _ask(ctx, monkeypatch, kid, "用户下周真的去上海出差吗？")

    adapter = FakeAdapter(_resolution("resolved", changes=[{
        "action": "revise", "knowledge_id": kid,
        "new_content": "用户下周二去上海出差三天",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).answer_question(item_id, "对，下周二去，出差三天")

    models = _load("agent.maintenance.models")
    assert isinstance(outcome, models.MaintenanceAnswerOutcome), (
        f"{CLAUSE}：answer_question 必须返回 MaintenanceAnswerOutcome")
    assert outcome.ok is True and outcome.applied_changes >= 1, (
        f"{CLAUSE}：revise 应执行成功（outcome={outcome}）")
    current = KnowledgeService(conn).resolve_current(kid)
    assert current is not None and current.content == "用户下周二去上海出差三天", (
        f"{CLAUSE}：回答修订未生效，当前内容 {getattr(current, 'content', None)!r}")
    assert _item_status(conn, item_id) == "processed", (
        f"{CLAUSE}：回答处理后事项状态应为 processed，实际 {_item_status(conn, item_id)!r}")


async def test_answer_revoke_keeps_history(ctx, monkeypatch):
    """§12.4：revoke 要求 new_content 为空；撤销但不删除历史。"""
    from agent.knowledge.lifecycle import KnowledgeService

    conn = ctx.conn
    kid = _active(conn, "用户计划养一只猫")
    item_id = await _ask(ctx, monkeypatch, kid, "用户还计划养猫吗？")

    adapter = FakeAdapter(_resolution("resolved", changes=[{
        "action": "revoke", "knowledge_id": kid,
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).answer_question(item_id, "不养了，计划取消")
    assert outcome.ok is True and outcome.applied_changes >= 1, (
        f"{CLAUSE}：revoke 应执行成功（outcome={outcome}）")
    item = KnowledgeService(conn).get(kid)
    assert item is not None, f"{CLAUSE}：revoke 不得删除历史行"
    assert item.state.value == "revoked", (
        f"{CLAUSE}：revoke 后原知识应为 revoked，实际 {item.state.value}")


async def test_answer_needs_clarification_keeps_item_and_changes_nothing(ctx, monkeypatch):
    """§12.4：needs_clarification 必须有问题且不同时应用 changes。"""
    conn = ctx.conn
    kid = _active(conn, "用户在公司负责后端")
    item_id = await _ask(ctx, monkeypatch, kid, "用户现在还负责后端吗？")

    adapter = FakeAdapter(_resolution(
        "needs_clarification", question="指的是工作职责还是项目分工？"))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).answer_question(item_id, "嗯……看情况吧")

    assert outcome.ok is True, f"{CLAUSE}：needs_clarification 是正常结果（outcome={outcome}）"
    row = conn.execute("SELECT state, content, version FROM knowledge WHERE id = ?",
                       (kid,)).fetchone()
    assert row["state"] == "active" and row["version"] == 1, (
        f"{CLAUSE}：needs_clarification 不得应用修改")
    assert _item_status(conn, item_id) in ("awaiting_answer", "processing"), (
        f"{CLAUSE}：提问后事项应保持待回答，实际 {_item_status(conn, item_id)!r}")
    hits = _rows_containing(conn, "maintenance_items", "指的是工作职责还是项目分工？")
    assert hits, f"{CLAUSE}：needs_clarification 的后续问题必须持久保存"


async def test_answer_deferred_keeps_item_open(ctx, monkeypatch):
    """§12.4：deferred 不应用 changes、保留待处理。"""
    conn = ctx.conn
    kid = _active(conn, "用户计划今年读完 20 本书")
    item_id = await _ask(ctx, monkeypatch, kid, "今年的读书计划还作数吗？")

    adapter = FakeAdapter(_resolution("deferred"))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).answer_question(item_id, "先放一放，回头再说")
    assert outcome.ok is True
    row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["state"] == "active", f"{CLAUSE}：deferred 不得应用修改"
    assert _item_status(conn, item_id) == "deferred", (
        f"{CLAUSE}：deferred 应保留事项并标记稍后处理，实际 {_item_status(conn, item_id)!r}")


async def test_answer_no_change_marks_processed(ctx, monkeypatch):
    """§12.4：no_change 不应用 changes（用户明确保留原认识）。"""
    conn = ctx.conn
    kid = _active(conn, "用户喜欢喝美式咖啡")
    item_id = await _ask(ctx, monkeypatch, kid, "用户还喜欢喝美式咖啡吗？")

    adapter = FakeAdapter(_resolution("no_change"))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).answer_question(item_id, "是的，没变")
    assert outcome.ok is True and outcome.applied_changes == 0, (
        f"{CLAUSE}：no_change 不应应用修改（outcome={outcome}）")
    row = conn.execute("SELECT state, content FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["state"] == "active" and row["content"] == "用户喜欢喝美式咖啡"
    assert _item_status(conn, item_id) == "processed", (
        f"{CLAUSE}：no_change 表示问题已解决，事项应标 processed，"
        f"实际 {_item_status(conn, item_id)!r}")


# ---------------------------------------------------------------------------
# 版本冲突保护 / 作用范围
# ---------------------------------------------------------------------------


async def test_answer_respects_version_conflict(ctx, monkeypatch):
    """§12.4：写前重新检查版本（CAS）；用户后来改过则不改、如实记录冲突。"""
    from agent.knowledge.lifecycle import KnowledgeService

    conn = ctx.conn
    kid = _active(conn, "用户住在北京")
    item_id = await _ask(ctx, monkeypatch, kid, "用户还住在北京吗？")

    # 事项登记后，用户在主对话里直接改了这条知识（v1 -> v2）
    ks = KnowledgeService(conn)
    user_version = ks.revise_atomic(kid, fields={"content": "用户搬到了上海"})
    assert user_version.version == 2

    adapter = FakeAdapter(_resolution("resolved", changes=[{
        "action": "revise", "knowledge_id": kid,
        "new_content": "用户仍然住在北京",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).answer_question(item_id, "对，还住北京")

    current = ks.resolve_current(kid)
    assert current is not None and current.content == "用户搬到了上海", (
        f"{CLAUSE}：版本冲突时不得覆盖用户后来的修改（当前内容 {current.content!r}）")
    assert current.version == 2, (
        f"{CLAUSE}：冲突时不得悄悄产生新版本（当前版本 {current.version}）")
    assert outcome.ok is False or outcome.applied_changes == 0, (
        f"{CLAUSE}：版本冲突必须如实反映（outcome={outcome}）")
    assert outcome.error or outcome.detail, (
        f"{CLAUSE}：冲突需要在结果里明确说明，不能静默失败（outcome={outcome}）")


async def test_answer_only_touches_allowed_objects(ctx, monkeypatch):
    """§12.4：只作用于当前事项允许的对象；不得借回答改无关知识。"""
    conn = ctx.conn
    asked = _active(conn, "用户养了一只狗叫豆豆")
    unrelated = _active(conn, "用户的生日是三月五日")
    item_id = await _ask(ctx, monkeypatch, asked, "用户养的狗还叫豆豆吗？")

    adapter = FakeAdapter(_resolution("resolved", changes=[{
        "action": "revise", "knowledge_id": unrelated,
        "new_content": "用户的生日是随便哪天",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).answer_question(item_id, "对，还叫豆豆")
    row = conn.execute("SELECT state, content, version FROM knowledge WHERE id = ?",
                       (unrelated,)).fetchone()
    assert row["state"] == "active" and row["content"] == "用户的生日是三月五日", (
        f"{CLAUSE}：独立回答不得修改事项之外的无关知识")
    assert row["version"] == 1
    assert outcome.ok is False or outcome.applied_changes == 0, (
        f"{CLAUSE}：越界修改应被拒绝并如实反映（outcome={outcome}）")


# ---------------------------------------------------------------------------
# 后台问答隔离
# ---------------------------------------------------------------------------


async def test_background_qa_does_not_touch_main_conversation(ctx, monkeypatch):
    """§12.4：后台问题与回答不进入主对话 messages / 摘要、不计入用户对话轮数。"""
    conn = ctx.conn
    kid = _active(conn, "用户在用 QIO 整理自己的知识")
    item_id = await _ask(ctx, monkeypatch, kid, "这条知识还需要保留吗？")
    before = _counts(conn)

    adapter = FakeAdapter(_resolution("resolved", changes=[{
        "action": "revise", "knowledge_id": kid,
        "new_content": "用户在用 QIO 整理自己的知识（确认保留）",
    }]))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await _runner(ctx).answer_question(item_id, "保留，谢谢")
    assert outcome.ok is True
    after = _counts(conn)

    for name in ("messages", "fragments", "turn_journal"):
        if name in before and name in after:
            assert after[name] == before[name], (
                f"{CLAUSE}：后台问答不得写入主对话 {name}（{before[name]} -> {after[name]}）")
    if "turn_seq" in before and "turn_seq" in after:
        assert after["turn_seq"] == before["turn_seq"], (
            f"{CLAUSE}：后台问答不得计入用户对话轮数（turn_seq 变化）")
