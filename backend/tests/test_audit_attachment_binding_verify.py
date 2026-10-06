"""D 独立验证：附件显式绑定（审计问题 3 / plan §1.4）。

契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 3 条 + §1.4。
只依据产品规则：

1. 请求体里 attachment_ids 的**存在性**即语义：出现（含空列表）= 显式，只绑这些；
   空列表 = 没有附件；**缺字段**才走旧客户端兜底；
2. 前端一律发送该字段（即使为空）—— 后端必须把「空列表」与「缺字段」区分开；
3. 用户看到的附件 == 发送的附件；绑定前校验归属与状态；已被别的 turn 绑定的 id 不再重复绑定。

基线（e428bb9）现状：api/server.py:1233 用 body.get("attachment_ids") or [] 取值，
把显式空列表变成 falsy，bind_for_turn 于是走兜底把「该话题下所有未绑定附件」绑上
（services/attachments.py:779-806）；显式 id 还会被无条件改绑到新 turn ——
因此本文件在修复前应当是**红的**。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_audit_attachment_binding_verify.py -q
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.config import Settings  # noqa: F401  （夹具来自 conftest）
from agent.credentials.store import MemoryKeyring

TERMINAL_STATES = {"ready", "failed", "missing", "changed"}


class _CapturingTextAdapter:
    """text 兼容档的假模型：只记录它看到的上下文，不联网。"""

    mode = "text"
    model = "fake-capture-audit"
    supports_stream = False

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, messages, tools, **kwargs):
        self.prompts.append("\n".join(str(getattr(m, "content", "") or "") for m in messages))
        return Completion(message=ChatMessage(role="assistant", content="收到"))


@pytest.fixture()
def app_client(db_conn, settings):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        yield client, app


def _unwrap(body: dict) -> dict:
    nested = body.get("attachment")
    return nested if isinstance(nested, dict) else body


def _post_source(client, path, **extra) -> dict:
    resp = client.post("/api/attachments", json={"source_path": str(path), **extra})
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    return _unwrap(resp.json())


def _wait_terminal(client, attachment_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        resp = client.get(f"/api/attachments/{attachment_id}")
        assert resp.status_code == 200, (resp.status_code, resp.text)
        last = _unwrap(resp.json())
        if str(last.get("state")) in TERMINAL_STATES:
            return last
        time.sleep(0.05)
    pytest.fail(f"附件长时间没有进入终态：{last}")


def _make_ready_attachment(client, tmp_path, name: str = "待发草稿.txt") -> dict:
    source = tmp_path / name
    source.write_text("这份内容没有被发送过。", encoding="utf-8")
    created = _post_source(client, source)
    meta = _wait_terminal(client, str(created["id"]))
    assert meta["state"] == "ready", meta
    assert not meta.get("turn_id"), ("刚登记的附件不该有轮次归属", meta)
    return meta


def _turn_end(client, turn_id: str, timeout: float = 30.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in client.app.state.ctx.bus._history:
            if event.type.value == "TURN_END" and str(event.data.get("turn_id")) == turn_id:
                return event.data
        time.sleep(0.02)
    return None


def _submit(client, body: dict) -> dict:
    resp = client.post("/api/turns", json=body)
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    return resp.json()


# ---- 1. 显式空列表 = 没有附件（核心红点） -----------------------------------------


def test_explicit_empty_list_binds_nothing(app_client, tmp_path):
    client, _app = app_client
    pending = _make_ready_attachment(client, tmp_path)

    body = _submit(client, {"message": "只发纯文字，不带附件", "attachment_ids": []})
    bound = body.get("attachments") or []
    assert bound == [], (
        "attachment_ids 出现且为空 = 显式声明「没有附件」，不得兜底绑定话题下未绑定附件",
        [item.get("id") for item in bound],
    )
    turn_id = str(body["turn_id"])
    client.post("/api/turns/cancel")

    after = _unwrap(client.get(f"/api/attachments/{pending['id']}").json())
    assert not after.get("turn_id"), (
        "这一轮没有带附件，附件必须仍然无归属（用户看到的 == 发送的）",
        after.get("turn_id"),
    )
    assert str(after.get("turn_id") or "") != turn_id, after


def test_explicit_empty_list_keeps_attachment_out_of_model_context(app_client, tmp_path):
    """模型上下文 / 历史消息里都不能出现这个附件（plan §3 验收第 3 条）。"""
    client, app = app_client
    pending = _make_ready_attachment(client, tmp_path, "模型不该看见.txt")
    ctx = app.state.ctx

    adapter = _CapturingTextAdapter()
    ctx.build_adapter = AsyncMock(return_value=adapter)

    body = _submit(client, {"message": "这条消息没有附件", "attachment_ids": []})
    turn_id = str(body["turn_id"])
    end = _turn_end(client, turn_id)
    assert end is not None, "turn 没有在超时内结束"
    assert end.get("status") in ("completed", "failed", "unavailable", "cancelled"), end

    assert ctx.attachments.list(turn_id=turn_id) == [], "后端不得把附件绑到这一轮"
    note = ctx.attachment_turn_note(turn_id)
    assert note in (None, ""), ("本轮附件说明必须为空（模型上下文里没有这个文件）", note)
    assert adapter.prompts, "这一轮没有走到模型调用"
    joined = "\n".join(adapter.prompts)
    assert "模型不该看见" not in joined, (
        "附件名不该出现在模型上下文里（这条消息没有附件）",
        joined[:400],
    )

    messages = client.get("/api/session/messages").json()
    rows = messages.get("messages") if isinstance(messages, dict) else messages
    assert isinstance(rows, list), messages
    user_rows = [r for r in rows if str(r.get("role")) == "user"]
    assert user_rows, ("历史里必须有这条用户消息", rows)
    latest = user_rows[-1]
    raw = latest.get("raw") if isinstance(latest.get("raw"), dict) else latest
    ids = raw.get("attachment_ids") or latest.get("attachment_ids") or []
    assert [str(x) for x in ids] == [], ("历史消息不得带上未发送的附件", raw)
    dumped = str(raw)
    assert str(pending["id"]) not in dumped, ("历史消息里出现了这个附件的 id", dumped[:400])


# ---- 2. 缺字段才走旧客户端兜底（保持向后兼容） -------------------------------------


def test_missing_field_still_falls_back_for_old_clients(app_client, tmp_path):
    """旧客户端不带 attachment_ids 字段 → 兜底绑定该话题下未绑定附件（这条不能丢）。"""
    client, _app = app_client
    pending = _make_ready_attachment(client, tmp_path, "旧客户端.txt")

    body = _submit(client, {"message": "旧客户端不带这个字段"})
    bound = [str(item.get("id")) for item in (body.get("attachments") or [])]
    assert bound == [str(pending["id"])], (
        "缺字段时必须保留旧客户端兜底（把未绑定附件绑到这一轮）",
        bound,
    )
    client.post("/api/turns/cancel")


def test_explicit_ids_only_bind_those_ids(app_client, tmp_path):
    """显式给一个 id 时，不得顺手把其他未绑定附件也绑上。"""
    client, _app = app_client
    chosen = _make_ready_attachment(client, tmp_path, "被选中的.txt")
    other = _make_ready_attachment(client, tmp_path, "没被选中的.txt")

    body = _submit(client, {"message": "只带一个附件", "attachment_ids": [chosen["id"]]})
    bound = [str(item.get("id")) for item in (body.get("attachments") or [])]
    assert bound == [str(chosen["id"])], ("显式列表以显式为准", bound)

    after = _unwrap(client.get(f"/api/attachments/{other['id']}").json())
    assert not after.get("turn_id"), (
        "没有被显式指定的附件不得被顺手绑上",
        after.get("turn_id"),
    )
    client.post("/api/turns/cancel")


def test_unknown_id_does_not_silently_bind_everything(app_client, tmp_path):
    """显式给了一个不存在的 id：不能因此退回兜底（那正是老 bug 的形状）。"""
    client, _app = app_client
    pending = _make_ready_attachment(client, tmp_path, "存在但没被指定.txt")

    body = _submit(client, {"message": "指定了一个不存在的附件", "attachment_ids": ["att_nope_0001"]})
    bound = [str(item.get("id")) for item in (body.get("attachments") or [])]
    assert bound == [], ("显式列表里没有可绑对象时不得绑定别的附件", bound)
    client.post("/api/turns/cancel")
    after = _unwrap(client.get(f"/api/attachments/{pending['id']}").json())
    assert not after.get("turn_id"), after


# ---- 3. 已归属的 id 不再重复绑定 ---------------------------------------------------


def test_already_bound_attachment_is_not_rebound_to_another_turn(app_client, tmp_path):
    client, _app = app_client
    att = _make_ready_attachment(client, tmp_path, "已经属于第一轮.txt")

    first = _submit(client, {"message": "第一轮带它", "attachment_ids": [att["id"]]})
    assert [str(i.get("id")) for i in (first.get("attachments") or [])] == [str(att["id"])]
    first_turn = str(first["turn_id"])
    client.post("/api/turns/cancel")
    assert str(_unwrap(client.get(f"/api/attachments/{att['id']}").json()).get("turn_id")) == first_turn

    second = _submit(client, {"message": "第二轮又带它", "attachment_ids": [att["id"]]})
    second_turn = str(second["turn_id"])
    client.post("/api/turns/cancel")
    now = _unwrap(client.get(f"/api/attachments/{att['id']}").json())
    assert str(now.get("turn_id") or "") == first_turn, (
        "已被别的 turn 绑定的附件不得重复绑定到新轮（归属只能有一个）",
        {"first": first_turn, "second": second_turn, "now": now.get("turn_id")},
    )
    assert second.get("attachments") in (None, [],), (
        "第二轮不该拿到已经归属别人的附件",
        second.get("attachments"),
    )
