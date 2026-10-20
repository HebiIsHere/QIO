# -*- coding: utf-8 -*-
"""阶段四 C：自动化建议的结构化契约（schema / 可空 / 不催问 / 不伪造 / 不注入回答标记）。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.maintenance import models as maintenance_models
from agent.services import automation
from agent.services.automation import (
    AUTOMATION_LIMITS,
    AutomationSuggestion,
    basis_fingerprint,
    filter_suggestions,
    load_rejections,
    record_rejection,
    suggestion_signature,
    suggest_automations,
    validate_automation_output,
)


class _StubAdapter:
    """只回一段固定内容的假模型（不联网）；记录实际收到的消息。"""

    model = "stub-model"

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(messages)
        return SimpleNamespace(message=SimpleNamespace(content=self.payload))


def _suggestion(name: str = "export_report", description: str = "批量导出报表") -> dict:
    return {
        "name": name,
        "description": description,
        "reason": "用户多次要求导出同类报表",
        "parameters": {"type": "object", "properties": {}},
        "evidence_ids": ["m1", "m2"],
    }


def test_validate_accepts_schema_and_allows_empty():
    value, errors = validate_automation_output(
        json.dumps({"suggestions": [_suggestion()]})
    )
    assert errors == []
    assert value is not None and len(value.suggestions) == 1
    empty, empty_errors = validate_automation_output('{"suggestions": []}')
    assert empty_errors == []
    assert empty is not None and empty.suggestions == []


def test_validate_rejects_unknown_fields_and_broken_json():
    bad, errors = validate_automation_output(
        json.dumps({"suggestions": [dict(_suggestion(), extra="x")]})
    )
    assert bad is None and errors
    broken, broken_errors = validate_automation_output("我觉得可以做一个导出工具")
    assert broken is None and broken_errors


def test_signature_and_filter_do_not_nag_after_rejection():
    suggestion = AutomationSuggestion.model_validate(_suggestion())
    basis = basis_fingerprint(["帮我导出报表", "把上次的报表也导出"])
    signature = suggestion_signature(suggestion.model_dump())
    rejections = {signature: {"basis": basis, "reason": "rejected"}}
    kept, skipped = filter_suggestions(
        [suggestion],
        existing_tool_names=["web_search"],
        rejections=rejections,
        basis=basis,
    )
    assert kept == []
    assert skipped[0]["reason"] == "rejected_no_new_basis"

    # 新依据（新的完整请求）允许再次建议，而不是永久封死。
    new_basis = basis_fingerprint(["帮我导出报表", "把上次的报表也导出", "再导出一份季度表"])
    kept_new, _ = filter_suggestions(
        [suggestion],
        existing_tool_names=["web_search"],
        rejections=rejections,
        basis=new_basis,
    )
    assert kept_new == [suggestion]


def test_filter_skips_existing_tools_and_duplicates():
    existing = AutomationSuggestion.model_validate(_suggestion("web_search", "搜索"))
    duplicate_a = AutomationSuggestion.model_validate(_suggestion("export_report", "导出"))
    duplicate_b = AutomationSuggestion.model_validate(_suggestion("export_report", "导出"))
    kept, skipped = filter_suggestions(
        [existing, duplicate_a, duplicate_b],
        existing_tool_names=["web_search"],
        rejections={},
        basis="b",
    )
    assert kept == [duplicate_a]
    reasons = {item["reason"] for item in skipped}
    assert reasons == {"existing_tool", "duplicate"}


def test_rejection_record_roundtrip_and_bounded(db_conn):
    assertion = AutomationSuggestion.model_validate(_suggestion())
    record_rejection(
        db_conn, suggestion=assertion.model_dump(), reason="rejected", basis="b1"
    )
    stored = load_rejections(db_conn)
    signature = suggestion_signature(assertion.model_dump())
    assert signature in stored
    assert stored[signature]["basis"] == "b1"
    # 有界：不会无限增长（这里只验证可重复写入且仍可读）。
    record_rejection(
        db_conn, suggestion=assertion.model_dump(), reason="rejected", basis="b2"
    )
    assert load_rejections(db_conn)[signature]["basis"] == "b2"


@pytest.mark.asyncio
async def test_suggest_automations_uses_structured_task_without_answer_marker():
    adapter = _StubAdapter(json.dumps({"suggestions": [_suggestion()]}))
    suggestions, error = await suggest_automations(
        adapter,
        requests=[{"message_id": "m1", "content": "帮我导出上个月的报表"}],
        actual_results=[{"message_id": "a1", "content": "已经导出 3 张报表"}],
        existing_tools=[{"name": "web_search", "description": "搜索"}],
    )
    assert error is None
    assert [item.name for item in suggestions] == ["export_report"]
    system_text = adapter.calls[0][0].content
    user_text = adapter.calls[0][1].content
    assert "自动化工具建议" in system_text
    assert '"suggestions"' in system_text
    assert "[[QIO:ANSWER]]" not in system_text
    data = json.loads(user_text)  # user 必须是独立 JSON 数据，不是聊天拼接
    assert data["requests"][0]["content"] == "帮我导出上个月的报表"
    assert data["actual_results"][0]["content"] == "已经导出 3 张报表"
    assert data["existing_tools"][0]["name"] == "web_search"


@pytest.mark.asyncio
async def test_suggest_automations_is_nullable_and_never_fabricates():
    assert await suggest_automations(None, requests=[{"content": "x"}]) == ([], "no adapter")
    empty = _StubAdapter('{"suggestions": []}')
    suggestions, error = await suggest_automations(
        empty, requests=[{"message_id": "m1", "content": "帮我做点事"}]
    )
    assert suggestions == [] and error is None
    broken = _StubAdapter("不是 JSON")
    suggestions, error = await suggest_automations(
        broken, requests=[{"message_id": "m1", "content": "帮我做点事"}]
    )
    assert suggestions == [] and error

    class _Boom:
        async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("model down")

    suggestions, error = await suggest_automations(
        _Boom(), requests=[{"message_id": "m1", "content": "帮我做点事"}]
    )
    assert suggestions == [] and "model down" in (error or "")


def _seed_requests(db_conn) -> None:
    for i in range(3):
        db_conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at)"
            " VALUES (?, NULL, 'user', ?, 'text', '2026-10-21T00:00:00+00:00')",
            (f"m{i}", f"帮我导出第 {i} 个月的报表，最好一次导出全部"),
        )
    db_conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at)"
        " VALUES ('a1', NULL, 'assistant', '已导出 3 张报表，但每次都要手工重复', 'text',"
        " '2026-10-21T00:01:00+00:00')"
    )
    db_conn.commit()


def _maintenance_ctx(db_conn, adapter):
    async def build_adapter():
        return adapter

    return SimpleNamespace(
        conn=db_conn,
        turns=SimpleNamespace(active=None),
        embedding=None,
        build_adapter=build_adapter,
    )


@pytest.mark.asyncio
async def test_maintenance_creates_one_approval_per_suggestion(db_conn, monkeypatch):
    from agent.services import maintenance

    _seed_requests(db_conn)
    adapter = _StubAdapter(json.dumps({"suggestions": [_suggestion()]}))
    ctx = _maintenance_ctx(db_conn, adapter)
    requested: list[tuple] = []
    monkeypatch.setattr(
        maintenance,
        "_request_approval",
        lambda c, kind, payload: requested.append((kind, payload)),
    )
    plan = maintenance.plan_tool_candidates(ctx)
    assert plan is not None
    clusters = maintenance.cluster_texts(plan.texts, plan.embedding, plan.threshold)
    created = await maintenance.commit_tool_candidates(ctx, plan, clusters)
    assert created == 1
    assert requested and requested[0][0] == "tool_candidate"
    payload = requested[0][1]
    assert payload["name"] == "export_report"
    assert payload["basis"]
    assert payload["source_count"] == 3
    # 完整请求进入判断依据，不是 200 字前缀。
    assert adapter.calls
    user_data = json.loads(adapter.calls[0][1].content)
    assert all(item["content"].endswith("全部") for item in user_data["requests"])
    assert user_data["actual_results"][0]["content"].endswith("重复")


@pytest.mark.asyncio
async def test_maintenance_without_adapter_or_with_invalid_output_is_empty(db_conn, monkeypatch):
    from agent.services import maintenance

    _seed_requests(db_conn)
    ctx = _maintenance_ctx(db_conn, None)
    plan = maintenance.plan_tool_candidates(ctx)
    clusters = maintenance.cluster_texts(plan.texts, plan.embedding, plan.threshold)
    monkeypatch.setattr(maintenance, "_request_approval", lambda *a, **k: None)
    assert await maintenance.commit_tool_candidates(ctx, plan, clusters) == 0

    ctx = _maintenance_ctx(db_conn, _StubAdapter("不是 JSON"))
    plan = maintenance.plan_tool_candidates(ctx)
    clusters = maintenance.cluster_texts(plan.texts, plan.embedding, plan.threshold)
    assert await maintenance.commit_tool_candidates(ctx, plan, clusters) == 0


@pytest.mark.asyncio
async def test_rejected_suggestion_is_not_asked_again_without_new_basis(db_conn, monkeypatch):
    from agent.services import maintenance

    _seed_requests(db_conn)
    adapter = _StubAdapter(json.dumps({"suggestions": [_suggestion()]}))
    ctx = _maintenance_ctx(db_conn, adapter)
    monkeypatch.setattr(maintenance, "_request_approval", lambda *a, **k: None)
    plan = maintenance.plan_tool_candidates(ctx)
    clusters = maintenance.cluster_texts(plan.texts, plan.embedding, plan.threshold)
    assert await maintenance.commit_tool_candidates(ctx, plan, clusters) == 1
    basis = basis_fingerprint(plan.texts)
    record_rejection(
        db_conn,
        suggestion=_suggestion(),
        reason="rejected",
        basis=basis,
    )
    # 同一批完整请求：拒绝记录命中，不再催问。
    assert await maintenance.commit_tool_candidates(ctx, plan, clusters) == 0

    # 新请求出现（判断依据变化）：允许带着新依据再提一次。
    db_conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at)"
        " VALUES ('m9', NULL, 'user', '帮我导出第 9 个月的报表，最好一次导出全部', 'text',"
        " '2026-10-21T00:02:00+00:00')"
    )
    db_conn.commit()
    new_plan = maintenance.plan_tool_candidates(ctx)
    new_clusters = maintenance.cluster_texts(new_plan.texts, new_plan.embedding, new_plan.threshold)
    assert await maintenance.commit_tool_candidates(ctx, new_plan, new_clusters) == 1


def test_limits_are_shared_with_schema():
    schema = automation.automation_output_schema()
    assert schema["properties"]["suggestions"]["maxItems"] == AUTOMATION_LIMITS["max_suggestions"]
    assert maintenance_models.MAX_EVIDENCE_IDS >= 1  # 公共上限模块仍可用（不复制其实现）

@pytest.mark.asyncio
async def test_approval_path_records_rejection_and_approval_in_unified_flow(db_conn):
    """用户拒绝 → 记录（不催问）；用户同意 → 进入统一开发工作区。"""
    from types import SimpleNamespace

    from agent.services import maintenance

    base_payload = {
        "name": "export_report",
        "description": "批量导出报表",
        "reason": "重复请求",
        "parameters": {},
        "evidence_ids": ["m1"],
        "basis": "basis-1",
        "signature": suggestion_signature(_suggestion()),
    }

    class _Approvals:
        def __init__(self, decision: str) -> None:
            self.decision = decision
            self.calls: list[tuple] = []

        async def request(self, kind: str, payload: dict):
            self.calls.append((kind, payload))
            return SimpleNamespace(decision=self.decision)

    class _Workspaces:
        def __init__(self) -> None:
            self.created: list[str] = []

        def create(self, request: str):
            self.created.append(request)
            return SimpleNamespace(id="dev_1")

    # 拒绝：记录指纹 + 依据；不创建开发工作区。
    approvals = _Approvals("rejected")
    workspaces = _Workspaces()
    ctx = SimpleNamespace(
        conn=db_conn, approvals=approvals, dev_workspaces=workspaces, maintenance=None
    )
    maintenance._request_approval(ctx, "tool_candidate", dict(base_payload))
    await asyncio.sleep(0.05)
    stored = load_rejections(db_conn)
    assert suggestion_signature(base_payload) in stored
    assert workspaces.created == []

    # 同意：进入统一开发流程（dev_workspaces.create），不是旧 ToolCreator 路径。
    approvals_ok = _Approvals("approved")
    ctx_ok = SimpleNamespace(
        conn=db_conn, approvals=approvals_ok, dev_workspaces=workspaces, maintenance=None
    )
    maintenance._request_approval(ctx_ok, "tool_candidate", dict(base_payload))
    await asyncio.sleep(0.05)
    assert workspaces.created and "批量导出报表" in workspaces.created[0]


def test_long_requests_keep_full_text_beyond_first_200_chars():
    """判断依据是完整需求：200 字之后的关键信息也必须进入建议输入。"""
    from agent.services import maintenance

    tail = "尾部标记：需要同时导出 PDF 与 Excel 两种格式"
    long_text = ("我每周都要重复整理一次销售报表并导出。" * 15) + tail
    plan = maintenance.ToolClusterPlan(
        texts=[long_text, long_text, long_text],
        embedding=None,
        threshold=0.6,
        generation=None,
        message_ids=["m1", "m2", "m3"],
    )
    requests = maintenance._suggestion_requests(plan, [[0, 1, 2]])
    assert requests
    assert tail in requests[0]["content"]
    assert requests[0]["truncated"] is False
