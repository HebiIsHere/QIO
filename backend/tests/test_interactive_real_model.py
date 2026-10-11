"""互动模式「真实模型理解 + 真实执行」的端到端验收（Lead 维护，契约 §14）。

只用 HTTP 接口 + fake provider（不联网、不读密钥、不产生真实模型费用），
验收的是**用户实际能走通的路径**：提交 → 可靠受理 → 材料读取 → 模型理解 →
意图 → 审批 → 真实执行 → 结果卡片与来源引用。

这里只测「跨模块才能证明」的部分；各模块内部的边界由各模块自己的用例覆盖。
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.server import create_app
from agent.config import Settings
from agent.interactive import models

BOARD = models.DEFAULT_BOARD_ID
NOWHERE = "这段不该出现在上下文里的文字"


# --- 夹具与工具 -------------------------------------------------------------


@pytest.fixture()
def app_state(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    return app


@pytest.fixture()
def client(app_state):
    with TestClient(app_state) as c:
        yield c


class Provider:
    """一条 provider：所有模型调用共用一个脚本化 adapter，按调用顺序取脚本。

    complete() 与 stream() 取同一批脚本 —— 理解走哪条分支都成立。
    """

    def __init__(self, *texts: str) -> None:
        self.adapter = FakeStreamAdapter([StreamScript(text=t) for t in texts])
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return self.adapter

    @property
    def requests(self) -> list[dict]:
        return self.adapter.requests


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _state(cards, groups=None, links=None, selection=None) -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups or []),
        "links": list(links or []),
        "selection": list(selection or []),
    }


def _meta(client: TestClient) -> dict:
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def save(client: TestClient, cards, groups=None, links=None, selection=None) -> dict:
    state = _state(cards, groups, links, selection)
    state["seq"] = int(_meta(client)["seq"])
    resp = client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "test"})
    if resp.status_code == 409:
        detail = resp.json().get("detail")
        if isinstance(detail, dict) and detail.get("error") == "impact_confirmation_required":
            meta = _meta(client)
            state["seq"] = int(meta["seq"])
            check = client.post(
                f"/api/interactive/boards/{BOARD}/impact-check",
                json={"stateVersion": meta["seq"], "changeSet": {"state": state}},
            ).json()
            assert check.get("checkId"), f"影响预判没有给出可确认的句柄：{check}"
            resp = client.put(
                f"/api/interactive/boards/{BOARD}/state",
                json={
                    "state": state,
                    "reason": "test",
                    "confirm": {"checkId": check["checkId"], "stateVersion": check.get("stateVersion")},
                },
            )
    assert resp.status_code == 200, resp.text
    return resp.json()["state"]


def submit(client: TestClient, **body) -> dict:
    resp = client.post(f"/api/interactive/boards/{BOARD}/submissions", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def understanding_list(client: TestClient) -> list[dict]:
    resp = client.get(f"/api/interactive/boards/{BOARD}/understanding")
    assert resp.status_code == 200, resp.text
    return resp.json()["understandings"]


def wait_understanding(client: TestClient, uid: str, *, timeout: float = 15.0) -> dict:
    settled = {"understood", "needs_clarification", "no_action", "failed"}
    deadline = time.time() + timeout
    seen: dict | None = None
    while time.time() < deadline:
        for item in understanding_list(client):
            if item["id"] == uid:
                seen = item
                if item["status"] in settled:
                    return item
        time.sleep(0.05)
    raise AssertionError(f"理解没有在 {timeout}s 内结束：{seen}")


def executions(client: TestClient) -> list[dict]:
    resp = client.get(f"/api/interactive/boards/{BOARD}/executions")
    assert resp.status_code == 200, resp.text
    return resp.json()["executions"]


def wait_execution(client: TestClient, intent_id: str, *, timeout: float = 15.0) -> dict:
    terminal = {"done", "failed", "cancelled", "paused"}
    deadline = time.time() + timeout
    seen: dict | None = None
    while time.time() < deadline:
        for item in executions(client):
            if item["intentId"] == intent_id:
                seen = item
                if item["status"] in terminal:
                    return item
        time.sleep(0.05)
    raise AssertionError(f"执行没有在 {timeout}s 内结束：{seen}")


def create_intents(client: TestClient, submission_id: str) -> dict:
    resp = client.post(
        f"/api/interactive/boards/{BOARD}/intents", json={"submissionId": submission_id}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def intent_payload(*, key: str, refs: list[str], content: str, summary: str = "比较两份材料") -> dict:
    return {
        "kind": "intent",
        "summary": summary,
        "intents": [
            {
                "intentKey": key,
                "title": "整理材料的差异",
                "goal": "让用户看到两份材料的关键差异",
                "summary": "对比两份材料的正文并给出差异",
                "basis": [{"cardId": cid, "role": "material", "version": "v1"} for cid in refs],
                "expected": "一张列出差异的结果卡片",
                "operations": [
                    {"op": "add_reply_card", "content": content, "title": "差异"},
                    {"op": "group_cards", "cardIds": list(refs), "ordered": True},
                ],
                "impact": {"cardIds": list(refs), "scope": "只新增 QIO 结果卡片并成组"},
                "dependsOn": [],
                "conflictsWith": [],
                "conflictKey": "",
            }
        ],
    }


def context_of(provider: Provider, index: int = 0) -> dict:
    """从第 index 次请求里取出结构化上下文（理解请求的固定分隔标记）。"""
    request = provider.requests[index]
    for message in request["messages"]:
        payload = message.content or ""
        if "=== 上下文开始 ===" in payload:
            body = payload.split("=== 上下文开始 ===", 1)[1]
            body = body.split("=== 上下文结束 ===", 1)[0].strip()
            return json.loads(body)
    raise AssertionError("这次请求里没有结构化上下文（把板面 JSON 当聊天文字发出去了？）")


def cards_of(client: TestClient) -> dict[str, dict]:
    return {card["id"]: card for card in _meta(client)["state"]["cards"]}


# --- 1. 真实闭环 -----------------------------------------------------------


def test_real_path_submission_understanding_approval_execution(client: TestClient, app_state) -> None:
    """两条材料 + 一条获准比较要求：真实理解 → 批准 → 真实执行 → 结果卡保留材料引用。"""
    provider = Provider(
        json.dumps(
            intent_payload(key="compare-cost", refs=["m_a", "m_b"], content="成本差异：甲低于乙"),
            ensure_ascii=False,
        ),
        "甲与乙的成本差异：甲的单价更低，乙的交付更快。",
    )
    app_state.state.interactive_adapter_provider = provider

    save(
        client,
        [
            _card("m_a", "file", "甲材料：单价 100 元", meta={"name": "甲.txt"}),
            _card("m_b", "code", "乙材料：交付 3 天", meta={"language": "text"}),
            _card("req", "text", "请比较这两份材料的成本与交付", checked=True),
        ],
    )
    result = submit(client)

    # 提交被可靠受理（不是「模型已经理解完成」）
    assert result["status"] == "succeeded"
    assert result["delivery"]["delivered"] is True
    assert result["delivery"]["status"] == "accepted"
    uid = result["delivery"]["understandingId"]
    assert uid, "受理成功必须给出理解请求 id"

    done = wait_understanding(client, uid)
    assert done["status"] == "understood", done
    assert provider.calls >= 1, "受理之后必须真的调用模型理解"

    # 模型看到的是结构化上下文，且含用户要求与两条材料
    ctx = context_of(provider, 0)
    assert "请比较这两份材料的成本与交付" in str(ctx["requirement"]["text"])
    assert {"m_a", "m_b"} <= set(ctx["visibleCardIds"])
    assert {"m_a", "m_b"} <= {m["cardId"] for m in ctx["materials"]}

    # 真实入口按 submissionId 取意图；不重新调用模型
    created = create_intents(client, result["submission"]["id"])
    assert created["demo"] is False
    assert created["created"], created
    intent = created["created"][0]
    calls_after_intents = provider.calls

    approved = client.post(f"/api/interactive/intents/{intent['id']}/approve", json={})
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["ok"] is True, body
    # 批准即真的启动执行（不是停在「已批准」）
    assert body.get("execution"), "批准必须启动真实执行并同步返回执行记录"

    execution = wait_execution(client, intent["id"])
    assert execution["status"] == "done", execution
    assert calls_after_intents == provider.calls, "取意图这一步不得再调用模型"

    # 正式板面上真的出现了结果卡片，且能定位到意图 / 理解请求 / 材料。
    # 正文就是模型在这次理解中给出、并在预览里被用户批准的那段内容。
    after = cards_of(client)
    replies = [c for c in after.values() if c["kind"] == "reply" and not c["deleted"]]
    assert len(replies) == 1, replies
    reply = replies[0]
    assert reply["content"] == "成本差异：甲低于乙"
    assert reply["meta"]["reply"]["intentId"] == intent["id"]
    assert reply["meta"]["reply"]["understandingId"] == uid
    assert set(intent["materialRefs"]) >= {"m_a", "m_b"}


# --- 2. 不是固定演示：换材料 / 换要求 → 结果相应变化 -------------------------


def test_result_changes_with_material_and_requirement(client: TestClient, app_state) -> None:
    provider = Provider(
        json.dumps(intent_payload(key="k1", refs=["x1", "x2"], content="第一版结论"), ensure_ascii=False),
        "第一版结果：预算 10 万。",
        json.dumps(intent_payload(key="k1", refs=["y1", "y2"], content="第二版结论"), ensure_ascii=False),
        "第二版结果：预算 30 万，工期更短。",
    )
    app_state.state.interactive_adapter_provider = provider

    save(
        client,
        [
            _card("x1", "file", "预算方案一：10 万", meta={"name": "一.txt"}),
            _card("x2", "code", "方案一：工期 6 个月"),
            _card("r1", "text", "请比较这两份材料的风险", checked=True),
        ],
    )
    first = submit(client)
    wait_understanding(client, first["delivery"]["understandingId"])
    first_intent = create_intents(client, first["submission"]["id"])["created"][0]
    client.post(f"/api/interactive/intents/{first_intent['id']}/approve", json={})
    assert wait_execution(client, first_intent["id"])["status"] == "done"
    first_replies = [c for c in cards_of(client).values() if c["kind"] == "reply" and not c["deleted"]]
    assert len(first_replies) == 1, first_replies
    # 第一版结果真的落在正式板面上（换材料前先确认）
    assert "第一版结论" in first_replies[0]["content"]

    # 换材料与要求后再走一遍：把第一版结果留在板面上，证明两版结果都真实存在
    save(
        client,
        [
            first_replies[0],
            _card("y1", "file", "预算方案二：30 万", meta={"name": "二.txt"}),
            _card("y2", "code", "方案二：工期 3 个月"),
            _card("r2", "text", "请比较这两份材料的时间风险", checked=True),
        ],
    )
    second = submit(client)
    wait_understanding(client, second["delivery"]["understandingId"])
    second_intent = create_intents(client, second["submission"]["id"])["created"][0]
    client.post(f"/api/interactive/intents/{second_intent['id']}/approve", json={})
    wait_execution(client, second_intent["id"])

    texts = {c["content"] for c in cards_of(client).values() if c["kind"] == "reply"}
    assert "第一版结论" in texts, texts
    assert "第二版结论" in texts, texts
    # 两次理解看到的上下文不同：不是套模板
    assert context_of(provider, 0)["visibleCardIds"] != context_of(provider, 2)["visibleCardIds"]


# --- 3. 未勾选 / 隐藏 / 间接链接不进模型上下文 ------------------------------


def test_invisible_never_reaches_model_context(client: TestClient, app_state) -> None:
    provider = Provider(json.dumps({"kind": "no_action", "summary": "没有需要开展的任务"}, ensure_ascii=False))
    app_state.state.interactive_adapter_provider = provider

    save(
        client,
        [
            _card("v1", "file", "可见材料正文", meta={"name": "可见.txt"}),
            _card("v2", "code", "可见代码"),
            _card("n_ok", "text", "已勾选的要求：整理可见材料", checked=True),
            # 注意：id 不能取成会被别的字符串包含的形式（例如 n_no 是 submission_notes 的子串），
            # 否则断言会把「正常字段」当成泄露。
            _card("note_private", "text", NOWHERE, checked=False),
            _card("note_secret", "text", NOWHERE + "（隐藏）", hidden=True),
        ],
        links=[models.new_link("n_ok", "note_private", meaning="间接链接")],
    )
    result = submit(client)
    uid = result["delivery"]["understandingId"]
    wait_understanding(client, uid)

    ctx = context_of(provider, 0)
    blob = json.dumps(ctx, ensure_ascii=False)
    assert NOWHERE not in blob, "未勾选 / 隐藏注释的正文不得进入模型上下文"
    assert "n_ok" in blob
    assert "note_private" not in ctx["visibleCardIds"]
    assert "note_secret" not in ctx["visibleCardIds"]
    assert "note_private" not in blob, "未勾选注释不得以任何形式（含间接链接）进入上下文"
    assert "note_secret" not in blob
    # 链接只有两端都可见时才允许出现
    for link in ctx.get("relationships") or []:
        assert link.get("src") in ctx["visibleCardIds"] and link.get("dst") in ctx["visibleCardIds"]


# --- 4. 纯位置变化 / 重复提交：不额外调用模型、不产生重复任务 ---------------


def test_duplicate_submission_does_not_call_model_or_create_tasks(client: TestClient, app_state) -> None:
    provider = Provider(
        json.dumps(intent_payload(key="k", refs=["m1", "m2"], content="结论"), ensure_ascii=False),
        "结果正文。",
    )
    app_state.state.interactive_adapter_provider = provider

    notes = [
        _card("m1", "file", "材料一", meta={"name": "一.txt"}),
        _card("m2", "file", "材料二", meta={"name": "二.txt"}),
        _card("n1", "text", "请比较这两份材料", checked=True),
    ]
    save(client, notes)
    first = submit(client)
    wait_understanding(client, first["delivery"]["understandingId"])
    create_intents(client, first["submission"]["id"])
    calls = provider.calls
    rows = len(understanding_list(client))

    # 只移动位置：内容与上次成功提交一致 → duplicate，且不建立新的理解请求
    moved = [
        _card("m1", "file", "材料一", meta={"name": "一.txt"}, x=120.0, y=80.0),
        _card("m2", "file", "材料二", meta={"name": "二.txt"}, x=380.0, y=80.0),
        _card("n1", "text", "请比较这两份材料", checked=True, x=120.0, y=300.0),
    ]
    save(client, moved)
    again = submit(client)
    assert again["status"] == "duplicate", again["status"]
    assert again["delivery"]["delivered"] is False
    assert provider.calls == calls, "重复提交不得额外调用模型"
    assert len(understanding_list(client)) == rows, "重复提交不得新建理解请求"

    # 再点一次也一样
    third = submit(client)
    assert third["status"] == "duplicate"
    assert provider.calls == calls


# --- 5. 三种理解结果必须区分：需要补充 / 无需行动 ---------------------------


def test_clarification_and_no_action_do_not_create_tasks(client: TestClient, app_state) -> None:
    provider = Provider(
        json.dumps(
            {"kind": "clarification", "summary": "要求不明确", "clarification": {"question": "要比较哪个方面？", "missing": ["比较的维度"]}},
            ensure_ascii=False,
        ),
        json.dumps({"kind": "no_action", "summary": "当前无需开展任务"}, ensure_ascii=False),
    )
    app_state.state.interactive_adapter_provider = provider

    save(client, [_card("m1", "file", "材料一", meta={"name": "一.txt"}), _card("n1", "text", "你看看", checked=True)])
    first = submit(client)
    first_done = wait_understanding(client, first["delivery"]["understandingId"])
    assert first_done["status"] == "needs_clarification", first_done
    assert not first_done["intentIds"]
    assert create_intents(client, first["submission"]["id"])["created"] == []

    save(client, [_card("m2", "file", "材料二", meta={"name": "二.txt"}), _card("n2", "text", "先放着，不用处理", checked=True)])
    second = submit(client)
    second_done = wait_understanding(client, second["delivery"]["understandingId"])
    assert second_done["status"] == "no_action", second_done
    assert not second_done["intentIds"]
    assert create_intents(client, second["submission"]["id"])["created"] == []


# --- 6. 重复批准不重复执行、结果卡不重复 -----------------------------------


def test_repeated_approval_does_not_duplicate_execution_or_result(client: TestClient, app_state) -> None:
    provider = Provider(
        json.dumps(intent_payload(key="k", refs=["m1", "m2"], content="结论"), ensure_ascii=False),
        "只应该出现一次的结果正文。",
    )
    app_state.state.interactive_adapter_provider = provider

    save(
        client,
        [
            _card("m1", "file", "材料一", meta={"name": "一.txt"}),
            _card("m2", "file", "材料二", meta={"name": "二.txt"}),
            _card("n1", "text", "请比较这两份材料", checked=True),
        ],
    )
    result = submit(client)
    wait_understanding(client, result["delivery"]["understandingId"])
    intent = create_intents(client, result["submission"]["id"])["created"][0]

    first = client.post(f"/api/interactive/intents/{intent['id']}/approve", json={}).json()
    second = client.post(f"/api/interactive/intents/{intent['id']}/approve", json={}).json()
    assert first["ok"] is True, first
    # 第二次批准不得再启动一次执行：要么复用同一次，要么明确回答「已经完成」
    assert second.get("ok") is True or second.get("reason") in {
        "done",
        "already_running",
        "already_done",
        "running",
    }, second

    execution = wait_execution(client, intent["id"])
    assert execution["status"] == "done", execution
    ours = [e for e in executions(client) if e["intentId"] == intent["id"]]
    assert len(ours) == 1, ours

    replies = [c for c in cards_of(client).values() if c["kind"] == "reply" and not c["deleted"]]
    assert len(replies) == 1, replies
