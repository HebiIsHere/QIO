# -*- coding: utf-8 -*-
"""D 独立契约测试：维护产物安全恢复（契约 §12.4 恢复能力）。

覆盖：执行前影响预览、整批撤回、逐条恢复、保护用户后来的修改、历史不删除、
恢复通过新的版本记录（source=maintenance_restore / restore_of / batch_id）实现。

装置：真实 AppContext + fake adapter；先用一次合法 merge 造出可恢复的维护产物，
再调用 `MaintenanceRunner(ctx).preview_restore(...)` / `restore(...)`。

基线说明：阶段二实现未合入时 runner / 迁移 47 不存在，本文件会**失败**（不是 skip）。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_restore.py -q
"""

from __future__ import annotations

import importlib
import json

import pytest

CLAUSE = "契约 §12.4（恢复）"


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
                           "maintenance_items", "maintenance_operations")
               if t not in have]
    if missing:
        from agent.storage.migrate import current_version

        pytest.fail(
            f"{CLAUSE}：缺少表 {missing}（schema 版本 {current_version(conn)}）；"
            "迁移 47 尚未登记到 storage/schema.py。",
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


def _prov(row) -> dict:
    try:
        data = json.loads(row["provenance"] or "{}")
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _all_rows(conn) -> list:
    return list(conn.execute(
        "SELECT id, content, state, chain_id, version, provenance FROM knowledge ORDER BY id"))


def _active_restores(conn) -> list[dict]:
    rows = []
    for row in conn.execute(
            "SELECT id, content, state, chain_id, version, provenance "
            "FROM knowledge WHERE state = 'active'"):
        prov = _prov(row)
        if prov.get("source") == "maintenance_restore" or prov.get("restore_of"):
            rows.append({"row": row, "prov": prov})
    return rows


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
    raise AssertionError(f"maintenance_items 行缺少可识别 id 列：{sorted(keys)}")


def _batch_row(conn, batch_id: str):
    for row in conn.execute("SELECT * FROM maintenance_batches"):
        for key in row.keys():
            if key in ("id", "batch_id") and str(row[key]) == batch_id:
                return row
    return None


def _batch_status(conn, batch_id: str) -> str:
    row = _batch_row(conn, batch_id)
    assert row is not None, f"{CLAUSE}：找不到维护批次 {batch_id}"
    for name in ("status", "state"):
        if name in set(row.keys()):
            return str(row[name])
    raise AssertionError(f"maintenance_batches 行没有可读状态：{sorted(row.keys())}")


async def _merge_once(ctx, monkeypatch, *, first_content="用户喜欢喝拿铁",
                      second_content="用户常点拿铁咖啡"):
    conn = ctx.conn
    first = _active(conn, first_content)
    second = _active(conn, second_content)
    adapter = FakeAdapter(json.dumps({"candidates": [{
        "action": "merge",
        "knowledge_ids": [first, second],
        "new_content": "用户喜欢喝拿铁咖啡",
        "reason": "语义重复",
    }]}, ensure_ascii=False))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    runner = _runner(ctx)
    outcome = await runner.run_batch(_snapshot(conn, [first, second]))
    assert outcome.ok is True and outcome.applied >= 1, (
        f"{CLAUSE}：前置 merge 未成功（outcome={outcome}）")
    assert outcome.batch_id, f"{CLAUSE}：维护轮次必须有 batch_id（恢复按批次定位）"
    return runner, outcome.batch_id, first, second


# ---------------------------------------------------------------------------
# 整批撤回
# ---------------------------------------------------------------------------


async def test_preview_restore_describes_impact(ctx, monkeypatch):
    """§12.4：执行前影响预览（批次 / 涉及对象）。"""
    runner, batch_id, first, second = await _merge_once(ctx, monkeypatch)
    preview = await runner.preview_restore(batch_id=batch_id)
    assert isinstance(preview, dict) and preview, (
        f"{CLAUSE}：preview_restore 必须返回非空 dict（影响预览）")
    text = json.dumps(preview, ensure_ascii=False, default=str)
    assert batch_id in text or first in text or second in text, (
        f"{CLAUSE}：影响预览里应能看到批次或涉及对象：{text[:500]}")


async def test_restore_batch_rolls_back_and_keeps_history(ctx, monkeypatch):
    """§12.4：整批撤回通过新版本实现（maintenance_restore / restore_of / batch_id），
    不删除历史；来源知识恢复为 active。"""
    conn = ctx.conn
    runner, batch_id, first, second = await _merge_once(ctx, monkeypatch)
    before_rows = {(r["id"], r["state"]) for r in _all_rows(conn)}
    merged = [r for r in _all_rows(conn)
              if r["state"] == "active" and r["content"] == "用户喜欢喝拿铁咖啡"]
    assert len(merged) == 1
    merged_id = merged[0]["id"]

    result = await runner.restore(batch_id=batch_id)
    assert isinstance(result, dict) and result, f"{CLAUSE}：restore 必须返回结果 dict"

    # 1) 维护合并产物被撤销；2) 来源通过新的恢复版本重新 active 且带来源关系
    merged_after = conn.execute(
        "SELECT state FROM knowledge WHERE id = ?", (merged_id,)).fetchone()
    assert merged_after["state"] == "revoked", (
        f"{CLAUSE}：整批撤回后维护合并版本应被撤销（保留历史），实际 {merged_after['state']}")
    restores = _active_restores(conn)
    restore_of = {str(item["prov"].get("restore_of")) for item in restores}
    assert restore_of & {first, second}, (
        f"{CLAUSE}：恢复必须以新版本记录实现并带 restore_of（实际 {restore_of}）")
    for item in restores:
        prov = item["prov"]
        assert prov.get("source") == "maintenance_restore", (
            f"{CLAUSE}：恢复版本来源必须是 maintenance_restore，实际 {prov.get('source')!r}")
        if "batch_id" in prov:
            assert str(prov["batch_id"]) == str(batch_id), (
                f"{CLAUSE}：恢复版本 provenance 里的 batch_id 必须指向被撤回批次")
    # 3) 历史完整：原来所有行都还在（状态可变化，行数不减少）
    after_rows = {(r["id"], r["state"]) for r in _all_rows(conn)}
    assert {rid for rid, _ in before_rows} <= {rid for rid, _ in after_rows}, (
        f"{CLAUSE}：恢复不得删除任何历史版本")
    assert len(_all_rows(conn)) >= len(before_rows)
    assert _batch_status(conn, batch_id) in ("rolled_back", "rollback_partial"), (
        f"{CLAUSE}：批次状态应标为已撤回 / 部分撤回，实际 {_batch_status(conn, batch_id)!r}")


async def test_restore_protects_later_user_modification(ctx, monkeypatch):
    """§12.4：维护产物已不是当前链版本（用户改过）时该项拒绝恢复，并明确说明。"""
    from agent.knowledge.lifecycle import KnowledgeService

    conn = ctx.conn
    runner, batch_id, first, second = await _merge_once(ctx, monkeypatch)
    merged = [r for r in _all_rows(conn)
              if r["state"] == "active" and r["content"] == "用户喜欢喝拿铁咖啡"]
    assert len(merged) == 1
    merged_id = merged[0]["id"]

    # 用户在维护之后自己改了合并结果
    user_version = KnowledgeService(conn).revise_atomic(
        merged_id, fields={"content": "用户其实更喜欢美式咖啡"})
    assert user_version.version == 2

    preview = await runner.preview_restore(batch_id=batch_id)
    preview_text = json.dumps(preview, ensure_ascii=False, default=str)
    unsafe_tokens = ("无法安全恢复", "冲突", "conflict", "unsafe", "跳过", "无法恢复",
                     "拒绝恢复", "已被后续", "blocked")
    assert any(token in preview_text for token in unsafe_tokens), (
        f"{CLAUSE}：预览必须明确说明无法安全恢复的部分：{preview_text[:500]}")

    result = await runner.restore(batch_id=batch_id)
    result_text = json.dumps(result, ensure_ascii=False, default=str)
    current = KnowledgeService(conn).resolve_current(merged_id)
    assert current is not None and current.content == "用户其实更喜欢美式咖啡", (
        f"{CLAUSE}：恢复不得覆盖用户后来的修改（当前内容 {getattr(current, 'content', None)!r}）")
    assert current.version == 2, (
        f"{CLAUSE}：恢复保护后续修改时不应产生新版本（当前 v{current.version}）")
    assert any(token in result_text for token in unsafe_tokens), (
        f"{CLAUSE}：恢复结果必须明确说明无法安全恢复的部分：{result_text[:500]}")
    assert _batch_status(conn, batch_id) in ("rollback_partial", "rolled_back"), (
        f"{CLAUSE}：部分无法恢复时批次状态应如实反映，实际 {_batch_status(conn, batch_id)!r}")


# ---------------------------------------------------------------------------
# 逐条恢复
# ---------------------------------------------------------------------------


async def test_restore_single_item(ctx, monkeypatch):
    """§12.4：逐条恢复（item_id 定位）。"""
    conn = ctx.conn
    runner, batch_id, first, second = await _merge_once(ctx, monkeypatch)
    hits = _rows_containing(conn, "maintenance_items", "用户喜欢喝拿铁咖啡")
    assert hits, f"{CLAUSE}：找不到合并候选对应的维护事项（无法逐条恢复）"
    item_id = _item_id(hits[0])

    preview = await runner.preview_restore(item_id=item_id)
    assert isinstance(preview, dict) and preview, (
        f"{CLAUSE}：逐条恢复也要有影响预览")
    result = await runner.restore(item_id=item_id)
    assert isinstance(result, dict) and result, f"{CLAUSE}：逐条恢复必须返回结果 dict"
    restores = _active_restores(conn)
    assert restores, f"{CLAUSE}：逐条恢复应以新的恢复版本实现（maintenance_restore）"
    assert any(str(item["prov"].get("restore_of")) in {first, second} for item in restores), (
        f"{CLAUSE}：逐条恢复的 restore_of 应指向被恢复来源")


async def test_restore_unknown_target_is_reported(ctx, monkeypatch):
    """§12.4：未知批次 / 事项必须如实报错，不得静默成功。"""
    await _merge_once(ctx, monkeypatch)
    runner = _runner(ctx)
    for kwargs in ({"batch_id": "batch_does_not_exist"}, {"item_id": "item_does_not_exist"}):
        preview = await runner.preview_restore(**kwargs)
        assert isinstance(preview, dict)
        text = json.dumps(preview, ensure_ascii=False, default=str)
        assert any(token in text for token in ("不存在", "未找到", "not found", "error", "ok")), (
            f"{CLAUSE}：未知目标的影响预览应明确说明：{text[:300]}")
        result = await runner.restore(**kwargs)
        text = json.dumps(result, ensure_ascii=False, default=str)
        assert any(token in text for token in ("不存在", "未找到", "not found", "error", "ok")), (
            f"{CLAUSE}：未知目标的恢复结果应明确说明：{text[:300]}")
