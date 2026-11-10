"""话题申请的审批面：edits 透传、批准前校验、无等待者 409、旧口径不变（契约 §2.2/§2.3）。

这些用例只用进程内的 ApprovalService + 真实迁移出来的 sqlite（db_conn），
不联网、不调用真实模型。重点固定住：

* ApprovalResult.edits 只在 topic kind 上透传，其它审批默认 None 且行为不变；
* topic_create 空名称 → 400 topic_name_required，申请保持 pending、不消费；
* topic_switch 目标不存在 → 400 topic_target_missing，申请保持 pending；
* 没有等待者的 topic 申请（后端重启后）→ 409 topic_application_stale，
  不假成功、不执行；已消费的重复决定同样 409；
* overrides / 非话题 edits 仍然显式拒绝（安全口径不动）。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.graph.nodes import NodeService
from agent.storage.approvals import OverridesUnsupported
from agent.tools.approval import (
    ApprovalService,
    TopicApplicationStale,
    TopicEditsUnsupported,
    TopicNameRequired,
    TopicTargetMissing,
)

# 本文件验证话题申请的真实审批交互：关闭 conftest 的旧测试自动批准兜底。
pytestmark = pytest.mark.topic_application


def _payload_switch(target_topic_id: str, *, from_topic_id: str = "t1") -> dict:
    return {
        "action": "switch",
        "turn_id": "turn-appr-1",
        "from_topic_id": from_topic_id,
        "from_topic_name": "当前话题",
        "target_topic_id": target_topic_id,
        "target_topic_name": "目标话题",
        "reason": "这段内容看起来属于该话题",
        "source": "model",
    }


def _payload_create(name: str) -> dict:
    return {
        "action": "create",
        "turn_id": "turn-appr-1",
        "from_topic_id": "t1",
        "from_topic_name": "当前话题",
        "new_name": name,
        "reason": "这是一个独立的新话题",
        "source": "model",
    }


def _new_topic(db_conn, name: str) -> str:
    return NodeService(db_conn).create_topic(name).id


async def _auto_respond(bus: EventBus, service: ApprovalService, actions: list[dict]):
    """按 topic 审批事件顺序执行 actions（decision / edits / overrides / 期望异常）。"""
    index = 0
    async for chunk in bus.stream():
        for line in chunk.splitlines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event.get("type") != "APPROVAL_REQUIRED":
                continue
            approval = event["data"]["approval"]
            if index >= len(actions):
                continue
            action = actions[index]
            index += 1
            try:
                if action.get("overrides") is not None:
                    await service.respond(
                        approval["approval_id"],
                        action.get("decision", "approved"),
                        overrides=action["overrides"],
                        edits=action.get("edits"),
                    )
                else:
                    await service.respond(
                        approval["approval_id"],
                        action.get("decision", "approved"),
                        edits=action.get("edits"),
                    )
            except Exception as exc:  # noqa: BLE001 - 期望的校验失败交给用例断言
                action.setdefault("errors", []).append(exc)
            else:
                if action.get("edits") is not None or action.get("decision") == "rejected":
                    action["responded"] = True
                else:
                    action["responded"] = True
            if action.get("then_edits") is not None or action.get("then_decision"):
                await service.respond(
                    approval["approval_id"],
                    action.get("then_decision", "approved"),
                    edits=action.get("then_edits"),
                )
                action["responded"] = True


async def _request_with_actions(
    service: ApprovalService,
    bus: EventBus,
    kind: str,
    payload: dict,
    actions: list[dict],
):
    task = asyncio.create_task(_auto_respond(bus, service, actions))
    await asyncio.sleep(0.05)
    try:
        result = await service.request(kind, payload)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return result


@pytest.fixture()
def service(db_conn):
    bus = EventBus()
    svc = ApprovalService(bus, timeout_seconds=5, conn=db_conn)
    return svc, bus


@pytest.fixture()
def client(db_conn, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _wait_portal(client: TestClient, predicate, *, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = client.portal.call(lambda: predicate())
        if value:
            return value
        client.portal.call(lambda: asyncio.sleep(0.02))
    raise AssertionError("等待条件超时")


# ---------------------------------------------------------------------------
# 服务层：edits 透传与批准前校验
# ---------------------------------------------------------------------------

async def test_topic_create_edits_are_passed_through(service):
    svc, bus = service
    actions = [{"decision": "approved", "edits": {"name": "用户改的名字"}}]
    result = await _request_with_actions(
        svc, bus, "topic_create", _payload_create("模型名的"), actions
    )
    assert result.decision == "approved"
    assert result.edits == {"name": "用户改的名字"}


async def test_other_approvals_keep_edits_none(service):
    svc, bus = service
    actions = [{"decision": "approved"}]
    result = await _request_with_actions(
        svc, bus, "continue", {"reason": "budget", "message": "预算耗尽"}, actions
    )
    assert result.decision == "approved"
    assert result.edits is None, "默认 None 不改变其它审批"


async def test_create_empty_name_is_400_and_keeps_pending(service):
    svc, bus = service
    actions = [
        {"decision": "approved", "edits": {"name": "   "}, "then_edits": {"name": "改名后的话题"}}
    ]
    result = await _request_with_actions(
        svc, bus, "topic_create", _payload_create("模型名的"), actions
    )

    errors = actions[0].get("errors") or []
    assert errors and isinstance(errors[0], TopicNameRequired)
    assert errors[0].status_code == 400
    assert errors[0].code == "topic_name_required"
    # 空名称被拒后申请没有被消费：第二次合法批准仍生效，且用的是改过的名称
    assert result.decision == "approved"
    assert result.edits == {"name": "改名后的话题"}


async def test_switch_missing_target_is_400_and_keeps_pending(service):
    svc, bus = service
    actions = [{"decision": "approved", "then_decision": "rejected"}]
    result = await _request_with_actions(
        svc, bus, "topic_switch", _payload_switch("topic_not_exists"), actions
    )
    errors = actions[0].get("errors") or []
    assert errors and isinstance(errors[0], TopicTargetMissing)
    assert errors[0].status_code == 400
    assert errors[0].code == "topic_target_missing"
    assert result.decision == "rejected"


async def test_switch_to_existing_topic_is_approved(service, db_conn):
    svc, bus = service
    target = _new_topic(db_conn, "已存在的目标话题")
    actions = [{"decision": "approved"}]
    result = await _request_with_actions(
        svc, bus, "topic_switch", _payload_switch(target), actions
    )
    assert result.decision == "approved"
    assert result.edits is None


async def test_topic_decision_is_single_use(service):
    svc, bus = service
    actions = [{"decision": "approved", "edits": {"name": "只用一次的名字"}}]
    result = await _request_with_actions(
        svc, bus, "topic_create", _payload_create("只用一次的名字"), actions
    )
    assert result.decision == "approved"
    with pytest.raises(TopicApplicationStale) as info:
        await svc.respond(result.approval_id, "approved")
    assert info.value.status_code == 409
    assert info.value.code == "topic_application_stale"


async def test_overrides_are_still_rejected_for_topic_kinds(service):
    svc, bus = service
    actions = [
        {
            "decision": "approved",
            "overrides": {"subagent_budget": {"max_iterations": 5}},
            "then_edits": {"name": "正常批准的名字"},
        }
    ]
    result = await _request_with_actions(
        svc, bus, "topic_create", _payload_create("正常批准的名字"), actions
    )
    errors = actions[0].get("errors") or []
    assert errors and isinstance(errors[0], OverridesUnsupported)
    assert errors[0].status_code == 400
    assert result.decision == "approved", "第一次失败没有消费申请，第二次合法批准生效"


async def test_edits_are_rejected_for_non_topic_approvals(service):
    svc, bus = service
    actions = [
        {"decision": "approved", "edits": {"name": "x"}, "then_decision": "approved"}
    ]
    result = await _request_with_actions(
        svc, bus, "continue", {"reason": "budget", "message": "预算耗尽"}, actions
    )
    errors = actions[0].get("errors") or []
    assert errors and isinstance(errors[0], TopicEditsUnsupported)
    assert errors[0].status_code == 400
    assert result.decision == "approved"
    assert result.edits is None


def _insert_legacy_pending(db_conn, approval_id: str, kind: str, payload: dict):
    now = "2026-11-10T00:00:00+00:00"
    db_conn.execute(
        "INSERT OR REPLACE INTO pending_approvals "
        "(approval_id, kind, payload, turn_id, session_id, created_at, expires_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
        (approval_id, kind, json.dumps(payload, ensure_ascii=False), "turn-old", "sess-1", now, now),
    )
    db_conn.commit()


async def test_topic_application_without_waiter_is_409_stale(db_conn, service):
    """后端重启后：等待者不在了，respond 必须 409 stale（不假成功、不执行）。"""
    svc, _bus = service
    _insert_legacy_pending(db_conn, "appr_restart_topic", "topic_switch", _payload_switch("t2"))
    with pytest.raises(TopicApplicationStale) as info:
        await svc.respond("appr_restart_topic", "approved")
    assert info.value.status_code == 409
    assert info.value.code == "topic_application_stale"
    # 非话题的旧行保持原行为（既有的 LegacyApprovalNotExecutable 409），
    # 不被新的 topic_application_stale 口径影响。
    from agent.storage.approvals import LegacyApprovalNotExecutable

    _insert_legacy_pending(db_conn, "appr_restart_old", "continue", {"reason": "budget"})
    with pytest.raises(LegacyApprovalNotExecutable):
        await svc.respond("appr_restart_old", "approved")


# ---------------------------------------------------------------------------
# HTTP 层：/api/approvals/{id}/respond 的 edits 与结构化错误
# ---------------------------------------------------------------------------

def _start_topic_request(client: TestClient, kind: str, payload: dict):
    """在应用事件循环里发起一条真实等待中的 topic 申请，返回 (task, approval_id)。"""
    ctx = client.app.state.ctx

    async def _start():
        return asyncio.ensure_future(ctx.approvals.request(kind, payload))

    task = client.portal.call(_start)

    def _pending_id():
        items = ctx.approvals.pending()
        return items[0]["approval_id"] if items else None

    approval_id = _wait_portal(client, _pending_id)
    return task, approval_id


def _result_of(client: TestClient, task):
    _wait_portal(client, task.done)
    return client.portal.call(lambda: task.result())


def test_respond_api_accepts_and_echoes_edits(client: TestClient):
    task, approval_id = _start_topic_request(
        client, "topic_create", _payload_create("模型起的名")
    )
    resp = client.post(
        f"/api/approvals/{approval_id}/respond",
        json={"decision": "approved", "edits": {"name": "用户改过的名字"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["decision"] == "approved"
    assert body["edits"] == {"name": "用户改过的名字"}
    result = _result_of(client, task)
    assert result.decision == "approved"
    assert result.edits == {"name": "用户改过的名字"}


def test_respond_api_empty_name_returns_400_and_keeps_pending(client: TestClient):
    task, approval_id = _start_topic_request(client, "topic_create", _payload_create("模型名"))
    resp = client.post(
        f"/api/approvals/{approval_id}/respond",
        json={"decision": "approved", "edits": {"name": ""}},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "topic_name_required"
    assert detail["message"]
    # 申请保持 pending、不消费：仍能从 /pending 读到，然后可以正常拒绝
    pending = client.get("/api/approvals/pending").json()["approvals"]
    assert any(item["approval_id"] == approval_id for item in pending)
    rejected = client.post(
        f"/api/approvals/{approval_id}/respond", json={"decision": "rejected"}
    )
    assert rejected.status_code == 200, rejected.text
    assert _result_of(client, task).decision == "rejected"


def test_respond_api_missing_switch_target_returns_400(client: TestClient):
    task, approval_id = _start_topic_request(
        client, "topic_switch", _payload_switch("topic_gone")
    )
    resp = client.post(
        f"/api/approvals/{approval_id}/respond", json={"decision": "approved"}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "topic_target_missing"
    pending = client.get("/api/approvals/pending").json()["approvals"]
    assert any(item["approval_id"] == approval_id for item in pending)
    rejected = client.post(
        f"/api/approvals/{approval_id}/respond", json={"decision": "rejected"}
    )
    assert rejected.status_code == 200, rejected.text
    assert _result_of(client, task).decision == "rejected"


def test_respond_api_without_waiter_returns_409_stale(client: TestClient, db_conn):
    _insert_legacy_pending(
        db_conn, "appr_api_restart", "topic_switch", _payload_switch("t2")
    )
    resp = client.post(
        "/api/approvals/appr_api_restart/respond", json={"decision": "approved"}
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "topic_application_stale"
    assert detail["message"]


def test_legacy_topic_switch_endpoints_are_retired(client: TestClient):
    for path in ("/api/topic-switch/confirm", "/api/topic-switch/reject"):
        resp = client.post(path)
        assert resp.status_code == 410, resp.text
        assert resp.json()["detail"]["code"] == "topic_switch_legacy_retired"
