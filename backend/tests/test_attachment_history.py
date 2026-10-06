"""历史消息必须带附件元数据（问题 5：刷新 / 重进历史后附件仍能打开、重新定位）。

D 阶段二实机复现：带附件发一条消息 → 刷新/重进历史 → 附件行消失。
证据：GET /api/session/context 与 GET /api/session/messages 返回的每条消息只有
[id, role, content, content_type, created_at, raw, turn_id]，**没有 attachments**；
而前端 session.ts 明确读 m.attachments（用 toAttachmentRef 收敛），刷新后没有任何入口。

本文件先于实现落地：修复前上面两条路由都没有 attachments 字段 → 红。
实现要求（Lead 指派）：
1. 两条历史路由的每条消息附带 attachments，**形状与实时路径的 attachments.payload() 完全一致**；
2. 一次批量查询（按 message_id，必要时 turn_id 兜底），不做 N+1；
3. 状态是**现在的事实**（walk payload(..., check=True)），missing / changed / failed 如实呈现；
4. 没有附件的消息不带该字段；分页 / before 游标语义不变。
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import attachments as attachments_mod
from agent.services.app import AppContext

TERMINAL = ("ready", "failed", "changed", "missing", "cancelled")


def _pin_attachment_data_dir(app, tmp_path: Path) -> None:
    """把附件的真实落点钉在 tmp_path。

    已知陷阱（Lead 2026-10-07 确认的代码事实）：config.Settings.__post_init__ 会用环境变量
    QIO_DATA_DIR **覆盖**构造时显式传入的 data_dir。tests/conftest.py 会 pop 掉它，但把用例
    放在仓外跑（或 conftest 没被加载）时，Settings(data_dir=tmp_path) 就会写进用户真实数据目录。
    所以走 create_app 的附件测试必须再钉一次服务自己的 data_dir（root 由它派生）。
    """
    data_dir = tmp_path / "data"
    (data_dir / "attachments").mkdir(parents=True, exist_ok=True)
    app.state.ctx.attachments.data_dir = data_dir


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings, tmp_path: Path):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    _pin_attachment_data_dir(app, tmp_path)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def ctx(client) -> AppContext:
    return client.app.state.ctx


def _wait_terminal(client: TestClient, attachment_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last: dict | None = None
    while time.time() < deadline:
        last = client.get(f"/api/attachments/{attachment_id}").json()["attachment"]
        if last["state"] in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"附件没有在 {timeout}s 内进入终态：{last}")


def _create_ready(
    client: TestClient, tmp_path, name: str, topic_id: str, data: bytes = b"history\n"
) -> dict:
    source = tmp_path / name
    source.write_bytes(data)
    created = client.post(
        "/api/attachments", json={"source_path": str(source), "topic_id": topic_id}
    ).json()["attachment"]
    ready = _wait_terminal(client, created["id"])
    assert ready["state"] == "ready", ready
    return ready


def _bind_and_append(ctx: AppContext, topic_id: str, turn_id: str, attachment_ids: list[str], text: str):
    """走真实的绑定路径 + 写一条历史消息（与 test_tool_record_api.py 造历史的姿势一致）。"""
    bound = ctx.attachments.bind_for_turn(turn_id, attachment_ids, topic_id=topic_id)
    assert [a.id for a in bound] == attachment_ids
    message_id, _fragment = ctx.memory.append_message(
        topic_id=topic_id, role="user", content=text, turn_id=turn_id
    )
    return message_id


def test_history_context_messages_carry_the_same_attachment_shape(client, ctx, tmp_path):
    topic = ctx.topics.nodes.create_topic("历史附件A").id
    att = _create_ready(client, tmp_path, "历史纪要.txt", topic)
    message_id = _bind_and_append(ctx, topic, "turn_hist_a", [att["id"]], "带附件发一条")
    ctx.navigation.anchors.set_active(topic)

    body = client.get("/api/session/context").json()
    message = next(m for m in body["messages"] if m["id"] == message_id)

    assert "attachments" in message, "历史消息必须带附件（否则刷新后附件行消失）"
    items = message["attachments"]
    assert [item["id"] for item in items] == [att["id"]]

    # 形状必须与实时路径**完全一致**（前端直接用 toAttachmentRef 收敛，不做第二套解析）
    realtime = client.get(f"/api/attachments/{att['id']}").json()["attachment"]
    one = items[0]
    assert set(one) == set(realtime), (sorted(set(one) ^ set(realtime)),)
    for key in ("id", "name", "size_bytes", "kind", "display", "state", "error",
                "sha256", "stored_path", "source_path", "topic_id", "turn_id"):
        assert one[key] == realtime[key], key
    assert one["state"] == "ready"
    assert one["turn_id"] == "turn_hist_a"
    assert one["availability"]["readable_by_tool"] is True


def test_history_messages_route_carries_attachments_and_keeps_cursor_semantics(client, ctx, tmp_path):
    topic = ctx.topics.nodes.create_topic("历史附件B").id
    att = _create_ready(client, tmp_path, "第二个.txt", topic)
    _bind_and_append(ctx, topic, "turn_hist_b", [att["id"]], "第一条带附件")
    plain_id, _ = ctx.memory.append_message(topic_id=topic, role="user", content="第二条没有附件", turn_id=None)
    ctx.navigation.anchors.set_active(topic)

    body = client.get("/api/session/messages", params={"topic_id": topic, "limit": 50}).json()
    by_id = {m["id"]: m for m in body["messages"]}
    with_att = [m for m in body["messages"] if m.get("attachments")]
    assert len(with_att) == 1
    assert with_att[0]["attachments"][0]["id"] == att["id"]
    assert with_att[0]["attachments"][0]["name"] == "第二个.txt"
    # 没有附件的消息不带这个字段（与实时路径 ...(refs.length ? {attachments} : {}) 一致）
    assert "attachments" not in by_id[plain_id]
    # 分页字段语义不变
    assert body["has_more"] is False
    assert body["next_before"] is None

    # before 游标仍然能用：limit=1 翻到更早一页
    first = client.get("/api/session/messages", params={"topic_id": topic, "limit": 1}).json()
    assert first["has_more"] is True and first["next_before"]
    older = client.get(
        "/api/session/messages",
        params={"topic_id": topic, "limit": 5, "before": first["next_before"]},
    ).json()
    assert [m["id"] for m in older["messages"]] == [list(by_id)[0]]
    assert older["messages"][0]["attachments"][0]["id"] == att["id"]


def test_history_attachment_state_is_current_not_send_time(client, ctx, tmp_path, monkeypatch):
    """状态必须按**现在的事实**算：引用型源文件删掉之后，重新打开历史要如实显示 missing。"""
    monkeypatch.setattr(attachments_mod, "COPY_MAX_BYTES", 16)
    topic = ctx.topics.nodes.create_topic("历史附件C").id
    source = tmp_path / "会被删掉的引用.bin"
    source.write_bytes(b"v" * 32)
    created = client.post(
        "/api/attachments", json={"source_path": str(source), "topic_id": topic}
    ).json()["attachment"]
    ready = _wait_terminal(client, created["id"])
    assert ready["kind"] == "reference" and ready["state"] == "ready"

    _bind_and_append(ctx, topic, "turn_hist_c", [ready["id"]], "引用型附件")
    ctx.navigation.anchors.set_active(topic)

    # 发送之后源文件被删掉：历史里必须显示 missing（不能是发送时的旧状态 ready）
    source.unlink()
    body = client.get("/api/session/messages", params={"topic_id": topic, "limit": 50}).json()
    attached = [m for m in body["messages"] if m.get("attachments")]
    assert len(attached) == 1, body
    item = attached[0]["attachments"][0]
    assert item["kind"] == "reference"
    assert item["state"] == "missing", item
    assert item["availability"]["source_present"] is False
    assert item["error"], "missing 必须带一句人话原因"

    # 副本型同理：删掉 QIO 自己的副本 → 历史里 missing
    copy_att = _create_ready(client, tmp_path, "副本型.txt", topic)
    _bind_and_append(ctx, topic, "turn_hist_c2", [copy_att["id"]], "副本型附件")
    from pathlib import Path

    Path(copy_att["stored_path"]).unlink()
    body = client.get("/api/session/messages", params={"topic_id": topic, "limit": 50}).json()
    copy_item = next(
        m for m in body["messages"] if m.get("attachments") and m["attachments"][0]["id"] == copy_att["id"]
    )["attachments"][0]
    assert copy_item["state"] == "missing", copy_item


def test_history_page_uses_a_batch_query_not_n_plus_one(client, ctx, tmp_path, monkeypatch):
    """一页里有多少条带附件的消息，都只允许 1 次批量查询（message_id）+ 1 次兜底（turn_id）。"""
    topic = ctx.topics.nodes.create_topic("历史附件D").id
    for index in range(8):
        att = _create_ready(client, tmp_path, f"批量{index}.txt", topic)
        _bind_and_append(ctx, topic, f"turn_batch_{index}", [att["id"]], f"第 {index} 条")
    ctx.navigation.anchors.set_active(topic)

    service = ctx.attachments
    counter = {"attachments_select": 0}

    class CountingConn:
        def __init__(self, inner) -> None:
            self._inner = inner

        def execute(self, sql, *args, **kwargs):  # noqa: ANN002, ANN003
            text = " ".join(str(sql).split())
            if text.startswith("SELECT * FROM attachments"):
                counter["attachments_select"] += 1
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(self._inner, name)

        @property
        def in_transaction(self) -> bool:
            return self._inner.in_transaction

    monkeypatch.setattr(service, "conn", CountingConn(ctx.conn))

    body = client.get("/api/session/messages", params={"topic_id": topic, "limit": 50}).json()

    with_att = [m for m in body["messages"] if m.get("attachments")]
    assert len(with_att) == 8, "8 条消息都应该带上各自的附件"
    assert counter["attachments_select"] <= 2, (
        f"附件查询次数 {counter['attachments_select']} 随消息数增长（N+1）："
        "一页历史必须用一次批量查询取完"
    )
