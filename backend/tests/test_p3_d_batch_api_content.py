# -*- coding: utf-8 -*-
"""D 独立契约测试：批量确认门槛、读取模型与长正文 / Markdown（契约 §13.5）。

覆盖：
* 「同批批量确认」门槛前后端统一为两条（同一常量口径）：2 条可批量、1 条走单条；
* 意图读取返回真实状态 / 版本 / 授权范围，不把已受理显示为已完成；
* 长正文（含 Markdown 表格 / 代码块）完整保存并从 API 完整返回，不静默截断成四千字；
* 空正文被参数模型拒绝；编辑 / 保存不调用模型。

基线说明：本阶段实现未合入时（B 的 intent_service / 工具、C 的 API 扩展缺失）本文件失败是预期。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_batch_api_content.py -q
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CLAUSE = "契约 §13.5"
BOARD = "board_p3_d_batch"
SUBMISSION = "sub_p3_d_batch"
ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"{CLAUSE}：契约要求的 {name} 不可导入；阶段三实现未合入时属于「未验证」：{exc!r}",
            pytrace=False,
        )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 批量确认门槛：前后端同口径 = 2
# ---------------------------------------------------------------------------


def test_batch_confirm_threshold_is_two_on_both_sides():
    """§13.5：前后端「同批批量确认」门槛统一为两条（同一常量口径）。"""
    backend_files = list((ROOT / "backend" / "src" / "agent").rglob("*.py"))
    frontend_files = [p for p in (ROOT / "frontend" / "src").rglob("*.ts")]
    backend_hits = [p for p in backend_files
                    if "BATCH_CONFIRM_MIN = 2" in _read(p) or "BATCH_MIN = 2" in _read(p)]
    frontend_hits = [p for p in frontend_files
                     if "BATCH_CONFIRM_MIN = 2" in _read(p) or "BATCH_MIN = 2" in _read(p)]
    assert backend_hits, f"{CLAUSE}：后端未把批量确认门槛统一为 2"
    assert frontend_hits, f"{CLAUSE}：前端未把批量确认门槛统一为 2"
    # 旧门槛 4 不得继续作为「同批批量确认」的判据
    for path in backend_hits + frontend_hits:
        text = _read(path)
        assert "BATCH_MIN = 4" not in text, (
            f"{CLAUSE}：{path} 仍保留旧门槛 4")


def test_batch_api_accepts_two_and_rejects_single(client, db_conn):
    """§13.5：2 条意图可批量确认；单条走单条确认（批量接口拒绝 1 条）。"""
    service = _load("agent.interactive.intent_service")
    from agent.interactive import board_store, models

    board_store.save_board(db_conn, BOARD, models.empty_state(BOARD), reason="seed")
    specs = [
        {"client_ref": f"a{i}", "title": f"意图 {i}", "goal": "目标",
         "expected": "结果",
         "scope": {"allowed_actions": ["create_card"], "max_new_cards": 1}}
        for i in range(2)
    ]
    proposed = asyncio.run(service.propose_intents(
        db_conn, board_id=BOARD, submission_id=SUBMISSION, intents=specs,
        resolved_question_ids=()))
    ids = [str(item.get("id")) for item in proposed.get("intents") or []]
    assert len(ids) == 2, f"{CLAUSE}：前置提案失败：{str(proposed)[:300]}"

    client_obj, _ctx = client
    batch = client_obj.post("/api/interactive/intents/batch",
                            json={"intentIds": ids})
    assert batch.status_code == 200, (
        f"{CLAUSE}：两条意图应可批量确认（实际 {batch.status_code}: {batch.text[:300]}）")

    # 单条请求：批量接口不能替代单条确认
    single = client_obj.post("/api/interactive/intents/batch",
                             json={"intentIds": ids[:1]})
    assert single.status_code in (400, 409), (
        f"{CLAUSE}：单条意图不得走批量接口（实际 {single.status_code}: {single.text[:300]}）")


def test_intent_list_reports_real_state_not_completed(client, db_conn):
    """§13.5：读取返回真实状态；未确认不等于已完成。"""
    service = _load("agent.interactive.intent_service")
    from agent.interactive import board_store, models

    board_store.save_board(db_conn, BOARD, models.empty_state(BOARD), reason="seed")
    spec = {"client_ref": "a1", "title": "待确认意图", "goal": "目标", "expected": "结果",
            "scope": {"allowed_actions": ["create_card"], "max_new_cards": 1}}
    proposed = asyncio.run(service.propose_intents(
        db_conn, board_id=BOARD, submission_id=SUBMISSION, intents=[spec],
        resolved_question_ids=()))
    intent_id = str((proposed.get("intents") or [{}])[0].get("id"))

    client_obj, _ctx = client
    listing = client_obj.get(f"/api/interactive/boards/{BOARD}/intents")
    assert listing.status_code == 200, (
        f"{CLAUSE}：意图读取失败（{listing.status_code}: {listing.text[:300]}）")
    body = listing.json()
    items = body.get("intents") or body.get("items") or []
    target = next((item for item in items if str(item.get("id")) == intent_id), None)
    assert target is not None, f"{CLAUSE}：读取结果缺少刚提出的意图：{str(body)[:400]}"
    status = str(target.get("status") or "")
    assert status != "completed", (
        f"{CLAUSE}：已受理（待确认）不得显示为已完成：{target}")
    assert "version" in target, f"{CLAUSE}：读取必须给出意图版本：{target}"
    text = str(target)
    assert any(token in text for token in
               ("scope", "范围", "authorization", "授权", "goal", "目标")), (
        f"{CLAUSE}：读取必须给出目标 / 范围 / 授权信息：{text[:400]}")


# ---------------------------------------------------------------------------
# 长正文 / Markdown 完整保存
# ---------------------------------------------------------------------------


class FakeAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        from agent.adapters.base import ChatMessage, Completion

        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content="模型不应被调用"))


@pytest.fixture()
def env(tmp_path, db_conn, monkeypatch):
    _load("agent.interactive.board_tools")
    service = _load("agent.interactive.intent_service")
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.interactive import board_store, models
    from agent.interactive.runtime import BoardTurnContext, reset_board_turn, set_board_turn
    from agent.services.app import AppContext

    app = AppContext(Settings(data_dir=tmp_path), db_conn, EventBus())
    adapter = FakeAdapter()
    monkeypatch.setattr(app, "build_adapter", lambda: _async_value(adapter))
    board_store.save_board(db_conn, BOARD, models.empty_state(BOARD), reason="seed")
    token = set_board_turn(BoardTurnContext(
        turn_id="turn_content", mode="interactive", board_id=BOARD,
        submission_id=SUBMISSION))
    try:
        yield {"app": app, "conn": db_conn, "service": service, "adapter": adapter}
    finally:
        reset_board_turn(token)


async def _async_value(value):
    return value


def _exec(env, name: str, arguments: dict):
    from agent.adapters.base import ToolCall

    async def _run_tool():
        return await env["app"].registry.execute(
            ToolCall(id=f"call_{name}", name=name, arguments=arguments))

    return asyncio.run(_run_tool())


def _confirmed_intent(env, *, max_new_cards: int = 5) -> str:
    spec = {"client_ref": "a1", "title": "产出长文", "goal": "整理", "expected": "长文卡",
            "scope": {"allowed_actions": ["create_card", "update_card"],
                      "max_new_cards": max_new_cards}}
    proposed = asyncio.run(env["service"].propose_intents(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION, intents=[spec],
        resolved_question_ids=()))
    intent_id = str((proposed.get("intents") or [{}])[0].get("id"))
    asyncio.run(env["service"].confirm_intents(
        env["conn"], board_id=BOARD, intent_ids=[intent_id]))
    return intent_id


def _card(env, card_id: str) -> dict:
    from agent.interactive import board_store

    state = board_store.load_board(env["conn"], BOARD)["state"]
    for card in state.get("cards") or []:
        if str(card.get("id")) == card_id:
            return card
    raise AssertionError(f"{CLAUSE}：卡片 {card_id} 不存在")


def test_long_markdown_content_is_saved_completely(env):
    """§13.5：长正文完整保存（表格 / 代码块不截断），上限由冻结常量执行。"""
    intent_id = _confirmed_intent(env)
    long_markdown = (
        "# 长文标题\n\n"
        "## 表格\n\n| 列 A | 列 B |\n|---|---|\n| 1 | 2 |\n\n"
        "## 代码块\n\n\x60\x60\x60python\nprint('hello')\n\x60\x60\x60\n\n"
        + "这是一段很长的正文。" * 500
    )
    assert len(long_markdown) > 5000, "前提：正文必须超过旧的四千字截断线"
    result = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_long_1",
        "content": long_markdown, "title": "长文卡"})
    assert result.ok is True, f"{CLAUSE}：长正文创建应成功（实际 {result}）"
    result_id = str((result.data or {}).get("id") or (result.data or {}).get("card_id") or "")
    assert result_id, f"{CLAUSE}：创建结果必须给出真实卡片 id：{result.data}"
    saved = _card(env, result_id)
    assert saved.get("content") == long_markdown, (
        f"{CLAUSE}：正文必须逐字完整保存（实际 {len(str(saved.get('content') or ''))} 字符）")


def test_long_content_is_returned_by_read_and_api_completely(env):
    """§13.5：读取 / API 返回完整正文，不静默截断。"""
    intent_id = _confirmed_intent(env)
    content = "# 标题\n\n" + ("正文内容。" * 800)
    created = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_read_long", "content": content})
    assert created.ok is True, f"{CLAUSE}：前置创建失败（实际 {created}）"
    card_id = str((created.data or {}).get("id") or (created.data or {}).get("card_id"))
    read = _exec(env, "board_read_cards", {"card_ids": [card_id]})
    assert read.ok is True, f"{CLAUSE}：读取应成功（实际 {read}）"
    assert content in str(read.data), (
        f"{CLAUSE}：读取结果必须包含完整正文（不允许截断）")
    assert len(content) > 4000


def test_empty_content_is_rejected_at_registry(env):
    """§13.5：空正文在参数层被拒绝，不产生假卡。"""
    intent_id = _confirmed_intent(env)
    result = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_empty", "content": "   "})
    assert result.ok is False, f"{CLAUSE}：空正文不得创建卡片（实际 {result}）"
    code = str(result.code or (result.data or {}).get("code") or "")
    assert "invalid_arguments" in code or "参数" in str(result.error or ""), (
        f"{CLAUSE}：空正文应报参数校验失败：{result}")


def test_edit_save_does_not_call_model(env):
    """§13.5：编辑 / 保存本身不调用模型（模型调用次数必须为 0）。"""
    intent_id = _confirmed_intent(env)
    created = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_edit_1", "content": "原始正文"})
    card_id = str((created.data or {}).get("id") or (created.data or {}).get("card_id"))
    before = env["adapter"].calls
    result = _exec(env, "board_update_card", {
        "intent_id": intent_id, "operation_key": "op_edit_2", "card_id": card_id,
        "expected_version": int(_card(env, card_id).get("version") or 1),
        "patch": {"content": "# 编辑后的正文\n\n" + "内容。" * 600}})
    assert result.ok is True, f"{CLAUSE}：编辑保存应成功（实际 {result}）"
    assert env["adapter"].calls == before, (
        f"{CLAUSE}：编辑 / 保存不得调用模型（调用次数 {before} -> {env['adapter'].calls}）")
    assert "编辑后的正文" in str(_card(env, card_id).get("content"))


def test_edit_stale_version_does_not_overwrite_user_edit(env):
    """§13.5：编辑前读取的版本已过期时不得覆盖用户后来的修改。"""
    intent_id = _confirmed_intent(env)
    created = _exec(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_stale_1", "content": "原始正文"})
    card_id = str((created.data or {}).get("id") or (created.data or {}).get("card_id"))
    version = int(_card(env, card_id).get("version") or 1)
    # 用户后来在板面上改了一次（版本 +1）
    from agent.interactive import board_store

    state = board_store.load_board(env["conn"], BOARD)["state"]
    for card in state["cards"]:
        if str(card.get("id")) == card_id:
            card["content"] = "用户后来的修改"
            card["version"] = version + 1
    board_store.save_board(env["conn"], BOARD, state, reason="user-edit")
    result = _exec(env, "board_update_card", {
        "intent_id": intent_id, "operation_key": "op_stale_2", "card_id": card_id,
        "expected_version": version, "patch": {"content": "模型想覆盖的正文"}})
    assert result.ok is False, (
        f"{CLAUSE}：旧版本写入必须冲突拒绝（实际 {result}）")
    assert _card(env, card_id).get("content") == "用户后来的修改", (
        f"{CLAUSE}：不得覆盖用户后来的修改")


# ---------------------------------------------------------------------------
# 测试夹具：TestClient（批量 / 读取 API 用）
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db_conn, settings, monkeypatch):
    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    adapter = FakeAdapter()
    monkeypatch.setattr(ctx, "build_adapter", lambda: _async_value(adapter))
    with TestClient(app) as c:
        yield c, ctx
