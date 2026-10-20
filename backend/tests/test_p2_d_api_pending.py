# -*- coding: utf-8 -*-
"""D 独立契约测试：API / 读取模型 / 来源标签（契约 §12.5 / §12.6）。

覆盖：
* `api/memory_pending_routes.py` 的 8 条路由在 `create_app` 里**挂载一次**；
* GET /api/memory/pending 返回冻结读取模型 PendingItemView 的字段，两类事项
  （knowledge_proposal / maintenance_question）按 kind / handler 区分；
* 回答 / 稍后处理 / 恢复 / 批次读取走 B 的 runner 与真实结果；
* `_knowledge_source_label` 把 maintenance.models 的来源键统一映射为「后台整理」，
  同时保留 provenance_source 原键（前端按 API 标签筛选，不自行推断）。

装置：真实 AppContext + ASGI（httpx.ASGITransport）；模型一律 fake。

基线说明：阶段二实现未合入时路由模块 / runner 不存在，本文件会**失败**（不是 skip）。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_api_pending.py -q
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest

CLAUSE = "契约 §12.5/§12.6"
ROOT = Path(__file__).resolve().parents[2]

ROUTES = (
    ("GET", "/api/memory/pending"),
    ("POST", "/api/memory/pending/{id}/answer"),
    ("POST", "/api/memory/pending/{id}/defer"),
    ("GET", "/api/memory/maintenance/batches"),
    ("GET", "/api/memory/maintenance/batches/{id}"),
    ("GET", "/api/memory/maintenance/restore/preview"),
    ("POST", "/api/memory/maintenance/restore"),
    ("GET", "/api/memory/maintenance/status"),
)


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
def app_ctx(db_conn, settings):
    _load("agent.api.memory_pending_routes")
    _require_tables(db_conn)
    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    if getattr(ctx, "maintenance_runner", None) is None:
        pytest.fail(
            f"{CLAUSE}：AppContext 未暴露 ctx.maintenance_runner；"
            "契约 §12.6 要求 Lead 在 services/app.py 实例化 MaintenanceRunner 并暴露。",
            pytrace=False,
        )
    return app, ctx


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _items(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("items", "pending", "data", "results", "batches"):
            value = body.get(key)
            if isinstance(value, list):
                return value
        for value in body.values():
            if isinstance(value, list):
                return value
    raise AssertionError(f"{CLAUSE}：无法从响应里找到事项列表：{str(body)[:300]}")


def _active(conn, content: str, *, provenance: dict | None = None):
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(conn)
    item = ks.create(category="general_fact", content=content, provenance=provenance)
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    return item.id


class FakeAdapter:
    def __init__(self, payloads) -> None:
        self._payloads = list(payloads) if not isinstance(payloads, str) else [payloads]
        self._default = self._payloads[-1] if self._payloads else "{}"

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        from agent.adapters.base import ChatMessage, Completion

        text = self._payloads.pop(0) if self._payloads else self._default
        return Completion(message=ChatMessage(role="assistant", content=text))


def _fake_build(adapter):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=adapter)


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


async def _ask_item(ctx, monkeypatch, kid: str, question: str) -> str:
    adapter = FakeAdapter(json.dumps({"candidates": [{
        "action": "ask",
        "knowledge_ids": [kid],
        "question": question,
        "reason": "需要用户确认",
    }]}, ensure_ascii=False))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await ctx.maintenance_runner.run_batch(_snapshot(ctx.conn, [kid]))
    assert outcome.asked >= 1, f"{CLAUSE}：ask 未登记事项（outcome={outcome}）"
    hits = _rows_containing(ctx.conn, "maintenance_items", question)
    assert hits, f"{CLAUSE}：找不到刚登记的维护事项"
    return _item_id(hits[0])


# ---------------------------------------------------------------------------
# 路由挂载
# ---------------------------------------------------------------------------


async def test_memory_pending_routes_are_mounted_once(app_ctx):
    """§12.5：8 条路由在 api/server.py 只挂载一次，且逐条真实可达。

    说明：本机 FastAPI 0.141 / starlette 1.6 的 include_router 在 app.routes 里只
    体现为子路由对象（path 为 None），不能按 app.routes 路径字符串判断「挂了几次」；
    因此「挂一次」用源码静态核对，「可达」用逐路由真实请求的语义响应核对。
    """
    app, _ctx = app_ctx
    server_file = ROOT / "backend" / "src" / "agent" / "api" / "server.py"
    server_text = server_file.read_text(encoding="utf-8")
    mount_calls = server_text.count("build_memory_pending_router(ctx)")
    assert mount_calls == 1, (
        f"{CLAUSE}：api/server.py 里 build_memory_pending_router(ctx) 出现 {mount_calls} 次"
        "（契约要求挂载一次）")

    responses = {}

    async def probe(client, name, method, path, *, json_body=None):
        if method == "GET":
            resp = await client.get(path)
        else:
            resp = await client.post(path, json=json_body if json_body is not None else {})
        responses[name] = (resp.status_code, resp.text[:200])
        return resp

    async with _client(app) as client:
        # 1) 待确认列表
        resp = await probe(client, "pending", "GET", "/api/memory/pending")
        assert resp.status_code == 200, responses["pending"]
        assert "items" in resp.json(), (
            f"{CLAUSE}：GET /api/memory/pending 必须返回 items 列表：{responses['pending']}")
        # 2) 回答动作（不存在的 id -> 路由可达、返回「不存在」而非路由缺失的 Not Found）
        resp = await probe(client, "answer", "POST", "/api/memory/pending/ghost/answer",
                           json_body={"answer": "x"})
        assert resp.status_code == 404 and "不存在" in resp.text, responses["answer"]
        # 3) 稍后处理动作
        resp = await probe(client, "defer", "POST", "/api/memory/pending/ghost/defer")
        assert resp.status_code == 404 and "不存在" in resp.text, responses["defer"]
        # 4) 批次列表
        resp = await probe(client, "batches", "GET", "/api/memory/maintenance/batches")
        assert resp.status_code == 200, responses["batches"]
        # 5) 批次详情
        resp = await probe(client, "batch", "GET", "/api/memory/maintenance/batches/ghost")
        assert resp.status_code == 404 and "不存在" in resp.text, responses["batch"]
        # 6) 恢复预览（参数校验错误证明路由存在）
        resp = await probe(client, "preview", "GET",
                           "/api/memory/maintenance/restore/preview")
        assert resp.status_code == 400 and "batch_id" in resp.text, responses["preview"]
        # 7) 恢复动作（必须显式 confirm）
        resp = await probe(client, "restore", "POST",
                           "/api/memory/maintenance/restore", json_body={})
        assert resp.status_code == 400 and "confirm" in resp.text, responses["restore"]
        # 8) 维护状态
        resp = await probe(client, "status", "GET", "/api/memory/maintenance/status")
        assert resp.status_code == 200 and isinstance(resp.json(), dict), responses["status"]


# ---------------------------------------------------------------------------
# 待确认列表：冻结读取模型 + 两类事项
# ---------------------------------------------------------------------------


async def test_pending_list_uses_frozen_view_and_two_handlers(app_ctx, monkeypatch):
    """§12.5：列表项按 PendingItemView 字段返回；两类事项 handler 不同。"""
    app, ctx = app_ctx
    conn = ctx.conn
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(conn)
    proposal = ks.create(category="general_fact", content="用户在学西班牙语")
    ks.submit(proposal.id)  # pending_review → knowledge_proposal 事项

    kid = _active(conn, "用户下周要参加马拉松")
    question = "用户下周确实要参加马拉松吗？"
    item_id = await _ask_item(ctx, monkeypatch, kid, question)

    models = _load("agent.maintenance.models")
    async with _client(app) as client:
        resp = await client.get("/api/memory/pending")
        assert resp.status_code == 200, f"{CLAUSE}：GET /api/memory/pending 失败 {resp.status_code}"
        items = _items(resp.json())
        assert items, f"{CLAUSE}：待确认列表为空（提案 / 问题都应出现）"

        views = []
        for raw in items:
            view = models.PendingItemView.model_validate(raw)  # extra=forbid：字段必须冻结
            views.append(view)
        kinds = {view.kind.value for view in views}
        assert "knowledge_proposal" in kinds, (
            f"{CLAUSE}：待审批知识没有出现在「记忆待确认」里（kinds={kinds}）")
        assert "maintenance_question" in kinds, (
            f"{CLAUSE}：后台维护问题没有出现在「记忆待确认」里（kinds={kinds}）")
        by_kind = {view.kind.value: view for view in views}
        assert by_kind["knowledge_proposal"].handler == "knowledge_proposal", (
            f"{CLAUSE}：知识提案的 handler 必须是 knowledge_proposal")
        question_views = [v for v in views if v.kind.value == "maintenance_question"]
        assert question_views and all(v.handler == "maintenance" for v in question_views), (
            f"{CLAUSE}：维护问题的 handler 必须是 maintenance")
        assert by_kind["knowledge_proposal"].status.value == "awaiting_approval", (
            f"{CLAUSE}：待审批知识状态应为 awaiting_approval")
        assert all(v.status.value == "awaiting_answer" for v in question_views), (
            f"{CLAUSE}：未回答的维护问题状态应为 awaiting_answer")
        assert any(str(v.id) == item_id for v in views), (
            f"{CLAUSE}：刚登记的维护事项没有出现在列表里")


# ---------------------------------------------------------------------------
# 动作路由
# ---------------------------------------------------------------------------


async def test_answer_route_processes_through_runner(app_ctx, monkeypatch):
    """§12.5：回答动作走 B 的 runner，结果体现在事项状态与知识版本上。"""
    app, ctx = app_ctx
    conn = ctx.conn
    kid = _active(conn, "用户偏好早上工作")
    item_id = await _ask_item(ctx, monkeypatch, kid, "用户还是偏好早上工作吗？")

    adapter = FakeAdapter(json.dumps({"resolution": "resolved", "changes": [{
        "action": "revise", "knowledge_id": kid,
        "new_content": "用户确定偏好早上工作",
    }]}, ensure_ascii=False))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))

    async with _client(app) as client:
        resp = await client.post(
            f"/api/memory/pending/{item_id}/answer", json={"answer": "对，确定"})
        assert resp.status_code == 200, (
            f"{CLAUSE}：POST answer 失败 {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        assert isinstance(body, dict), (
            f"{CLAUSE}：回答接口应返回结构化结果，实际 {type(body).__name__}")

    from agent.knowledge.lifecycle import KnowledgeService

    current = KnowledgeService(conn).resolve_current(kid)
    assert current is not None and current.content == "用户确定偏好早上工作", (
        f"{CLAUSE}：API 回答没有真正经过维护 runner 生效（当前 {getattr(current, 'content', None)!r}）")


async def test_defer_route_keeps_item_open(app_ctx, monkeypatch):
    """§12.5：稍后处理（defer）保留事项且不改知识。"""
    app, ctx = app_ctx
    conn = ctx.conn
    kid = _active(conn, "用户想学摄影")
    item_id = await _ask_item(ctx, monkeypatch, kid, "用户还想学摄影吗？")

    async with _client(app) as client:
        resp = await client.post(f"/api/memory/pending/{item_id}/defer", json={})
        assert resp.status_code == 200, (
            f"{CLAUSE}：POST defer 失败 {resp.status_code}: {resp.text[:300]}")

    row = conn.execute("SELECT state FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["state"] == "active", f"{CLAUSE}：defer 不得修改知识"
    hits = _rows_containing(conn, "maintenance_items", "用户还想学摄影吗？")
    assert hits, f"{CLAUSE}：defer 后事项应仍持久存在"
    row = hits[0]
    values = [str(row[key]) for key in row.keys() if isinstance(row[key], str)]
    assert "deferred" in values, (
        f"{CLAUSE}：defer 后事项状态应为 deferred（实际 {values}）")


async def test_restore_routes_go_through_runner(app_ctx):
    """§12.5：恢复预览 / 恢复动作必须走 ctx.maintenance_runner（不是路由自己实现）。"""
    app, ctx = app_ctx
    from unittest.mock import AsyncMock

    calls: list[tuple] = []

    async def fake_preview(**kwargs):
        calls.append(("preview", kwargs))
        return {"ok": True, "items": [], "batch_id": kwargs.get("batch_id")}

    async def fake_restore(**kwargs):
        calls.append(("restore", kwargs))
        return {"ok": True, "restored": 0, "batch_id": kwargs.get("batch_id")}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx.maintenance_runner, "preview_restore", fake_preview)
    monkeypatch.setattr(ctx.maintenance_runner, "restore", fake_restore)
    try:
        async with _client(app) as client:
            preview = await client.get(
                "/api/memory/maintenance/restore/preview", params={"batch_id": "batch_api_1"})
            assert preview.status_code == 200, (
                f"{CLAUSE}：恢复预览失败 {preview.status_code}: {preview.text[:300]}")
            # 契约 §12.5：恢复是高影响动作，API 要求显式 confirm（防误触）
            restore = await client.post(
                "/api/memory/maintenance/restore",
                json={"batch_id": "batch_api_1", "confirm": True})
            assert restore.status_code == 200, (
                f"{CLAUSE}：执行恢复失败 {restore.status_code}: {restore.text[:300]}")
    finally:
        monkeypatch.undo()
    assert any(kind == "preview" and payload.get("batch_id") == "batch_api_1"
               for kind, payload in calls), (
        f"{CLAUSE}：恢复预览没有经过 ctx.maintenance_runner.preview_restore；calls={calls}")
    assert any(kind == "restore" and payload.get("batch_id") == "batch_api_1"
               for kind, payload in calls), (
        f"{CLAUSE}：恢复没有经过 ctx.maintenance_runner.restore；calls={calls}")


# ---------------------------------------------------------------------------
# 来源标签
# ---------------------------------------------------------------------------


async def test_knowledge_source_labels_map_maintenance_keys(app_ctx):
    """§12.5：后台来源统一映射为「后台整理」，同时返回原始 provenance_source。"""
    app, ctx = app_ctx
    models = _load("agent.maintenance.models")
    conn = ctx.conn
    ids: dict[str, str] = {}
    for index, source in enumerate(models.MAINTENANCE_SOURCE_KEYS):
        ids[source] = _active(
            conn, f"后台整理来源测试 {index}",
            provenance={"source": source, "fragment_id": f"frag_{index}"})

    async with _client(app) as client:
        resp = await client.get("/api/knowledge")
        assert resp.status_code == 200, f"{CLAUSE}：GET /api/knowledge 失败"
        rows = resp.json()["knowledge"]
        by_id = {row["id"]: row for row in rows}
        for source, kid in ids.items():
            assert kid in by_id, f"{CLAUSE}：知识 {kid} 不在列表里"
            row = by_id[kid]
            assert row["source"] == "后台整理", (
                f"{CLAUSE}：来源 {source} 应映射为「后台整理」，实际 {row['source']!r}")
            assert row["provenance_source"] == source, (
                f"{CLAUSE}：必须同时返回原始 provenance_source={source!r}，"
                f"实际 {row.get('provenance_source')!r}")


async def test_maintenance_batches_and_status_routes(app_ctx, monkeypatch):
    """§12.5：维护批次列表 / 详情 / 状态读取可用（真实维护轮次之后）。"""
    app, ctx = app_ctx
    conn = ctx.conn
    first = _active(conn, "用户常用 VS Code")
    second = _active(conn, "用户写代码用 VS Code")
    adapter = FakeAdapter(json.dumps({"candidates": [{
        "action": "merge",
        "knowledge_ids": [first, second],
        "new_content": "用户写代码常用 VS Code",
        "reason": "语义重复",
    }]}, ensure_ascii=False))
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    outcome = await ctx.maintenance_runner.run_batch(_snapshot(conn, [first, second]))
    assert outcome.ok is True and outcome.batch_id, (
        f"{CLAUSE}：前置维护轮次未成功（outcome={outcome}）")

    async with _client(app) as client:
        listing = await client.get("/api/memory/maintenance/batches")
        assert listing.status_code == 200, f"{CLAUSE}：批次列表失败 {listing.status_code}"
        body = listing.json()
        items = _items(body)
        assert any(str(outcome.batch_id) in json.dumps(item, ensure_ascii=False)
                   for item in items), (
            f"{CLAUSE}：刚完成的批次没有出现在批次列表里")

        detail = await client.get(f"/api/memory/maintenance/batches/{outcome.batch_id}")
        assert detail.status_code == 200, (
            f"{CLAUSE}：批次详情失败 {detail.status_code}: {detail.text[:300]}")

        status = await client.get("/api/memory/maintenance/status")
        assert status.status_code == 200, f"{CLAUSE}：维护状态读取失败"
        assert isinstance(status.json(), (dict, list)), (
            f"{CLAUSE}：维护状态响应必须是结构化数据")
