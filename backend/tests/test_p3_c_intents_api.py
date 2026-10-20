# -*- coding: utf-8 -*-
"""阶段三 C 定向测试：意图视图 / 批量确认门槛 / 等待 / 账本 / 结果 / 恢复 API（契约 §13.5）。

只依赖冻结契约与注入的 fake intent_service（B 尚未合入）：
* 读取模型字段、真实状态不冒充完成、批量确认门槛两条（单条走单条确认）；
* 等待 / 账本 / 结果 / 恢复在服务未接入时如实 503 / available=false，不伪造数据；
* 旧 intents.list_intents 只读兼容映射（source=legacy）如实标注。
不联网、不调用真实模型。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module


BOARD = "board_p3_c"


# ---------------------------------------------------------------------------
# 假意图服务（B 的 intent_service 冻结接口形状）
# ---------------------------------------------------------------------------


class FakeIntentService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.views = [
            _view("in_1", "proposed", version=2, title="整理采访记录"),
            _view("in_2", "awaiting_confirmation", version=1, title="归类材料"),
            _view("in_3", "running", version=3, title="生成摘要"),
            _view("in_4", "partially_completed", version=2, title="合并表格"),
        ]
        self.confirm_result = {
            "ok": True,
            "results": [],
            "confirmed": [],
            "conflicts": [],
            "gate": "program",
        }

    def list_intents_view(self, conn, board_id):
        self.calls.append(("list_intents_view", board_id))
        return {
            "board_id": board_id,
            "seq": 7,
            "intents": [dict(view) for view in self.views],
            "operations": {"available": True, "counts": {"applied": 2}},
        }

    def get_intent(self, conn, board_id, intent_id):
        self.calls.append(("get_intent", board_id, intent_id))
        for view in self.views:
            if view["intent_id"] == intent_id:
                return dict(view)
        return None

    def confirm_intents(self, conn, *, board_id, intent_ids):
        self.calls.append(("confirm_intents", board_id, tuple(intent_ids)))
        result = dict(self.confirm_result)
        result["results"] = [
            {"intent_id": intent_id, "ok": True, "status": "approved", "reason": None}
            for intent_id in intent_ids
        ]
        result["confirmed"] = list(intent_ids)
        return result

    def ledger_summary(self, conn, board_id):
        self.calls.append(("ledger_summary", board_id))
        return {
            "available": True,
            "counts": {"applied": 2, "running": 1, "pending": 0, "failed": 0, "conflict": 0},
            "operations": [
                {
                    "operation_id": "op_1",
                    "intent_id": "in_1",
                    "operation_key": "k1",
                    "action": "create_card",
                    "status": "applied",
                }
            ],
        }

    def list_results(self, conn, board_id):
        self.calls.append(("list_results", board_id))
        return {
            "available": True,
            "results": [
                {
                    "result_id": "res_1",
                    "card_id": "card_1",
                    "intent_id": "in_3",
                    "kind": "text",
                    "title": "整理后的采访记录",
                    "content": "## 摘要\n\n| 项 | 值 |\n| --- | --- |\n| 主题 | 访谈 |\n\n    print(1)\n",
                    "source": "board_operations",
                    "created_at": "2026-10-21T00:00:00+00:00",
                }
            ],
        }

    def preview_recovery(self, conn, intent_id):
        self.calls.append(("preview_recovery", intent_id))
        return {
            "intent_id": intent_id,
            "status": "partially_completed",
            "can_resume": True,
            "reasons": [],
            "unfinished_operations": ["op_3"],
            "unfinished_dependencies": [],
        }

    def recover_intent(self, conn, *, board_id, intent_id, expected_version):
        self.calls.append(("recover_intent", board_id, intent_id, expected_version))
        return {"ok": True, "status": "approved", "reason": ""}


def _view(intent_id: str, status: str, *, version: int = 1, title: str = "") -> dict:
    return {
        "intent_id": intent_id,
        "board_id": BOARD,
        "client_ref": "c_" + intent_id,
        "submission_id": "sub_1",
        "version": version,
        "status": status,
        "title": title or intent_id,
        "goal": "目标：" + intent_id,
        "expected": "预期：" + intent_id,
        "basis": [{"card_id": "card_1", "version": 1, "role": "source"}],
        "scope": {
            "read_card_ids": ["card_1"],
            "write_card_ids": [],
            "group_ids": [],
            "allowed_actions": ["create_card"],
            "max_new_cards": 2,
            "max_new_groups": 0,
            "max_new_links": 0,
        },
        "depends_on": [],
        "conflicts_with": [],
        "conflicts": [],
        "authorization": {"status": "valid"},
        "authorization_valid": True,
        "outcome": "",
        "summary": "",
        "attempt": 1,
        "operations_summary": {
            "applied": 0,
            "running": 0,
            "pending": 0,
            "failed": 0,
            "conflict": 0,
        },
        "created_at": "2026-10-21T00:00:00+00:00",
        "updated_at": "2026-10-21T00:00:00+00:00",
        "finished_at": None,
    }


def _client(conn, service=None, *, waiting_provider=None) -> TestClient:
    app = FastAPI()
    app.state.ctx = SimpleNamespace(conn=conn)
    if service is not None:
        app.state.p3_intent_service = service
    if waiting_provider is not None:
        app.state.p3_waiting_provider = waiting_provider
    app.include_router(api_module.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# 读取：意图视图 / 详情
# ---------------------------------------------------------------------------


def test_batch_confirm_threshold_constant_is_two():
    assert api_module.BATCH_CONFIRM_MIN == 2


def test_intent_views_keep_real_status_and_batch_gate(db_conn):
    service = FakeIntentService()
    client = _client(db_conn, service)

    response = client.get(f"/api/interactive/boards/{BOARD}/intent-views")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["source"] == "service"
    assert body["seq"] == 7
    statuses = {view["intent_id"]: view["status"] for view in body["intents"]}
    assert statuses == {
        "in_1": "proposed",
        "in_2": "awaiting_confirmation",
        "in_3": "running",
        "in_4": "partially_completed",
    }
    # 视图字段完整（目标 / 预期 / 依据 / 范围 / 授权 / 账本摘要 / 版本）
    first = body["intents"][0]
    for key in (
        "intent_id",
        "client_ref",
        "version",
        "status",
        "title",
        "goal",
        "expected",
        "basis",
        "scope",
        "depends_on",
        "conflicts_with",
        "authorization",
        "authorization_valid",
        "operations_summary",
    ):
        assert key in first
    # 只有 proposed / awaiting_confirmation 可确认；两条才可批量
    assert body["batch_confirm"]["min"] == 2
    assert body["batch_confirm"]["eligible_ids"] == ["in_1", "in_2"]
    assert body["batch_confirm"]["available"] is True


def test_intent_view_detail_and_404(db_conn):
    service = FakeIntentService()
    client = _client(db_conn, service)
    ok = client.get(f"/api/interactive/boards/{BOARD}/intent-views/in_1")
    assert ok.status_code == 200
    assert ok.json()["intent"]["intent_id"] == "in_1"
    missing = client.get(f"/api/interactive/boards/{BOARD}/intent-views/ghost")
    assert missing.status_code == 404
    assert ("get_intent", BOARD, "ghost") in service.calls


# ---------------------------------------------------------------------------
# 确认：批量门槛两条 / 单条走单条
# ---------------------------------------------------------------------------


def test_batch_confirm_requires_two_intents(db_conn):
    service = FakeIntentService()
    client = _client(db_conn, service)
    response = client.post(
        "/api/interactive/intents/batch-confirm",
        json={"boardId": BOARD, "intentIds": ["in_1"]},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "batch_confirm_min"
    assert detail["min"] == 2
    assert detail["count"] == 1
    assert service.calls == []


def test_batch_confirm_two_intents_calls_service_and_never_fakes_completion(db_conn):
    service = FakeIntentService()
    client = _client(db_conn, service)
    response = client.post(
        "/api/interactive/intents/batch-confirm",
        json={"boardId": BOARD, "intentIds": ["in_1", "in_2"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["batch"] is True
    assert body["min"] == 2
    assert [item["intent_id"] for item in body["results"]] == ["in_1", "in_2"]
    # 服务返回 approved 就如实显示 approved，不冒充 completed
    assert {item["status"] for item in body["results"]} == {"approved"}
    assert ("confirm_intents", BOARD, ("in_1", "in_2")) in service.calls


def test_single_confirm_uses_single_path(db_conn):
    service = FakeIntentService()
    client = _client(db_conn, service)
    response = client.post(
        "/api/interactive/intents/in_1/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["batch"] is False
    assert body["min"] == 2
    assert body["results"][0]["intent_id"] == "in_1"
    assert ("confirm_intents", BOARD, ("in_1",)) in service.calls

    missing_board = client.post("/api/interactive/intents/in_1/confirm", json={})
    assert missing_board.status_code == 400


# ---------------------------------------------------------------------------
# 服务未接入：接近真实的前置失败，绝不伪造
# ---------------------------------------------------------------------------


def test_confirm_requires_service(db_conn):
    client = _client(db_conn)
    batch = client.post(
        "/api/interactive/intents/batch-confirm",
        json={"boardId": BOARD, "intentIds": ["in_1", "in_2"]},
    )
    assert batch.status_code == 503
    assert "未执行" in batch.json()["detail"]
    single = client.post("/api/interactive/intents/in_1/confirm", json={"boardId": BOARD})
    assert single.status_code == 503


def test_legacy_fallback_marks_source_and_maps_statuses(db_conn, monkeypatch):
    from agent.api import interactive_intents as module

    legacy_payload = {
        "intents": [
            {
                "id": "old_1",
                "boardId": BOARD,
                "title": "旧任务",
                "status": "pending",
                "summary": "旧摘要",
            },
            {
                "id": "old_2",
                "boardId": BOARD,
                "title": "旧任务 2",
                "status": "done",
                "summary": "已完成",
            },
        ]
    }
    monkeypatch.setattr(
        module.intents, "list_intents", lambda conn, board_id: legacy_payload
    )
    client = _client(db_conn)
    response = client.get(f"/api/interactive/boards/{BOARD}/intent-views")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "legacy"
    assert body["operations"]["available"] is False
    mapped = {view["intent_id"]: view for view in body["intents"]}
    assert mapped["old_1"]["status"] == "proposed"
    assert mapped["old_1"]["raw_legacy_status"] == "pending"
    assert mapped["old_2"]["status"] == "completed"
    assert body["batch_confirm"]["min"] == 2


# ---------------------------------------------------------------------------
# 等待 / 账本 / 结果 / 恢复
# ---------------------------------------------------------------------------


def test_waiting_uses_injected_provider(db_conn):
    client = _client(
        db_conn,
        waiting_provider=lambda board_id: {
            "waiting": {
                "board_id": board_id,
                "submission_id": "sub_1",
                "question": "你希望整理成表格还是清单？",
                "question_ids": ["q_1"],
            }
        },
    )
    body = client.get(f"/api/interactive/boards/{BOARD}/waiting").json()
    assert body["available"] is True
    assert body["waiting"]["question"].startswith("你希望整理")


def test_waiting_unavailable_is_honest(db_conn, monkeypatch):
    from agent.api import interactive_intents as module

    monkeypatch.setattr(module, "_waiting_for", lambda request, board_id: {
        "available": False,
        "waiting": None,
        "reason": "等待持久化尚未接入",
    })
    client = _client(db_conn)
    body = client.get(f"/api/interactive/boards/{BOARD}/waiting").json()
    assert body["available"] is False
    assert body["waiting"] is None


def test_ledger_and_results_from_service(db_conn):
    service = FakeIntentService()
    client = _client(db_conn, service)
    ledger = client.get(f"/api/interactive/boards/{BOARD}/ledger").json()
    assert ledger["available"] is True
    assert ledger["counts"]["applied"] == 2
    results = client.get(f"/api/interactive/boards/{BOARD}/results").json()
    assert results["available"] is True
    assert results["results"][0]["result_id"] == "res_1"
    # 结果正文完整（含 Markdown 表格 / 代码块），不在这里截断
    assert "| 项 | 值 |" in results["results"][0]["content"]


def test_ledger_and_results_unavailable_without_service_or_table(db_conn):
    client = _client(db_conn)
    ledger = client.get(f"/api/interactive/boards/{BOARD}/ledger").json()
    assert ledger["available"] is False
    assert ledger["operations"] == []
    results = client.get(f"/api/interactive/boards/{BOARD}/results").json()
    assert results["available"] is False
    assert results["results"] == []


def test_recovery_preview_and_recover(db_conn):
    service = FakeIntentService()
    client = _client(db_conn, service)
    preview = client.get("/api/interactive/intents/in_4/recovery-preview")
    assert preview.status_code == 200
    body = preview.json()["preview"]
    assert body["can_resume"] is True
    assert body["unfinished_operations"] == ["op_3"]

    missing_version = client.post(
        "/api/interactive/intents/in_4/recover", json={"boardId": BOARD}
    )
    assert missing_version.status_code == 400
    recovered = client.post(
        "/api/interactive/intents/in_4/recover",
        json={"boardId": BOARD, "expectedVersion": 2},
    )
    assert recovered.status_code == 200
    assert recovered.json()["ok"] is True
    assert recovered.json()["status"] == "approved"
    assert ("recover_intent", BOARD, "in_4", 2) in service.calls


def test_recovery_requires_service(db_conn):
    client = _client(db_conn)
    assert client.get("/api/interactive/intents/in_4/recovery-preview").status_code == 503
    assert (
        client.post(
            "/api/interactive/intents/in_4/recover",
            json={"boardId": BOARD, "expectedVersion": 1},
        ).status_code
        == 503
    )
