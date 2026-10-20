# -*- coding: utf-8 -*-
"""D 独立契约测试：批量确认门槛、读取模型与长正文 / Markdown（契约 §13.5）。

覆盖：
* 「同批批量确认」门槛前后端统一为两条（同一常量口径）：2 条可批量、1 条走单条；
* 意图读取返回真实状态 / 版本 / 授权范围，不把已受理显示为已完成；
* 长正文（含 Markdown 表格 / 代码块）完整保存并从 API 完整返回，不静默截断成四千字；
* 空正文被拒绝；编辑 / 保存不调用模型（旧空正文二次生成路径不被调用）。

装置：真实 DB + B 的 board_tools / intent_service（内容用例）；C 的 API（TestClient）。

基线说明：本阶段实现未合入时（B/C 缺失）本文件失败是预期。
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


class FakeApp:
    def __init__(self, conn) -> None:
        self.conn = conn


# ---------------------------------------------------------------------------
# 批量确认门槛：前后端同口径 = 2
# ---------------------------------------------------------------------------


def test_batch_confirm_threshold_is_two_on_both_sides():
    """§13.5：前后端「同批批量确认」门槛统一为两条（同一常量口径）。"""
    backend_files = list((ROOT / "backend" / "src" / "agent").rglob("*.py"))
    frontend_files = [p for p in (ROOT / "frontend" / "src").rglob("*.ts")]
    backend_hits = [p for p in backend_files if "BATCH_CONFIRM_MIN = 2" in _read(p)]
    frontend_hits = [p for p in frontend_files if "BATCH_CONFIRM_MIN = 2" in _read(p)]
    assert backend_hits, f"{CLAUSE}：后端未把批量确认门槛统一为 2"
    assert frontend_hits, f"{CLAUSE}：前端未把批量确认门槛统一为 2"
    for path in backend_hits + frontend_hits:
        assert "BATCH_MIN = 4" not in _read(path), (
            f"{CLAUSE}：{path} 仍保留旧门槛 4")
    # 批量确认必须走独立接口（单条走单项确认，不共用同一入口）
    joined = "\n".join(_read(path) for path in backend_hits)
    assert "batch-confirm" in joined or "batch_confirm" in joined, (
        f"{CLAUSE}：后端缺少批量确认接口")


@pytest.fixture()
def client(db_conn, settings, monkeypatch):
    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    from unittest.mock import AsyncMock

    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=None))
    with TestClient(app) as c:
        yield c, ctx


def _ensure_board(conn, board_id: str = BOARD) -> None:
    from agent.interactive import board_store

    board_store.ensure_board(conn, board_id=board_id)


def _add_submission(conn, board_id: str = BOARD, submission_id: str = SUBMISSION) -> str:
    from agent.interactive import models

    conn.execute(
        "INSERT OR IGNORE INTO board_submissions (id, board_id, seq, status, created_at)"
        " VALUES (?, ?, 1, 'accepted', ?)",
        (submission_id, board_id, models.now_iso()))
    conn.commit()
    return submission_id


def _spec(client_ref: str, **overrides) -> dict:
    base = {
        "client_ref": client_ref,
        "title": f"意图 {client_ref}",
        "goal": "目标",
        "expected": "结果",
        "basis": [],
        "scope": {"allowed_actions": ["create_card"], "max_new_cards": 2},
        "depends_on": [],
        "conflicts_with": [],
    }
    base.update(overrides)
    return base


def _propose(conn, service, intents: list[dict]) -> dict:
    return service.propose_intents(
        conn, board_id=BOARD, submission_id=SUBMISSION, intents=intents,
        resolved_question_ids=())


def test_batch_api_accepts_two_and_rejects_single(client, db_conn):
    """§13.5：2 条意图可批量确认；单条走单条确认（批量接口拒绝 1 条）。"""
    service = _load("agent.interactive.intent_service")
    _ensure_board(db_conn)
    _add_submission(db_conn)
    proposed = _propose(db_conn, service, [_spec("a1"), _spec("a2")])
    assert proposed.get("ok") is True, f"{CLAUSE}：前置提案失败：{str(proposed)[:300]}"
    ids = [str(item) for item in (proposed.get("client_refs") or {}).values()]

    client_obj, _ctx = client
    batch = client_obj.post("/api/interactive/intents/batch-confirm",
                            json={"intentIds": ids})
    assert batch.status_code == 200, (
        f"{CLAUSE}：两条意图应可批量确认（实际 {batch.status_code}: {batch.text[:300]}）")
    body = batch.json()
    assert int(body.get("min") or 2) == 2, f"{CLAUSE}：批量确认门槛应为 2：{body}"

    single = client_obj.post("/api/interactive/intents/batch-confirm",
                             json={"intentIds": ids[:1]})
    assert single.status_code in (400, 409), (
        f"{CLAUSE}：单条意图不得走批量接口（实际 {single.status_code}: {single.text[:300]}）")


def test_intent_list_reports_real_state_not_completed(client, db_conn):
    """§13.5：读取返回真实状态；未确认不等于已完成。"""
    service = _load("agent.interactive.intent_service")
    _ensure_board(db_conn)
    _add_submission(db_conn)
    proposed = _propose(db_conn, service, [_spec("a1")])
    intent_id = str((proposed.get("client_refs") or {}).get("a1"))

    client_obj, _ctx = client
    listing = client_obj.get(f"/api/interactive/boards/{BOARD}/intent-views")
    assert listing.status_code == 200, (
        f"{CLAUSE}：意图读取失败（{listing.status_code}: {listing.text[:300]}）")
    body = listing.json()
    items = body.get("intents") or body.get("items") or []
    target = next(
        (item for item in items
         if str(item.get("intent_id") or item.get("id")) == intent_id), None)
    assert target is not None, f"{CLAUSE}：读取结果缺少刚提出的意图：{str(body)[:400]}"
    assert str(target.get("status")) != "completed", (
        f"{CLAUSE}：已受理（待确认）不得显示为已完成：{target}")
    assert "version" in target, f"{CLAUSE}：读取必须给出意图版本：{target}"
    assert "scope" in target, f"{CLAUSE}：读取必须给出结构化范围：{target}"


# ---------------------------------------------------------------------------
# 长正文 / Markdown 完整保存（B 工具 + 真实板面状态）
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(db_conn):
    _load("agent.interactive.board_tools")
    service = _load("agent.interactive.intent_service")
    from agent.interactive import board_store, models, runtime

    conn = db_conn
    board_store.ensure_board(conn, board_id=BOARD)
    conn.execute(
        "INSERT OR IGNORE INTO board_submissions (id, board_id, seq, status, created_at)"
        " VALUES (?, ?, 1, 'accepted', ?)",
        (SUBMISSION, BOARD, models.now_iso()))
    conn.commit()
    tools_mod = _load("agent.interactive.board_tools")
    tools = {tool.name: tool for tool in tools_mod.build_board_tools(FakeApp(conn))}
    token = runtime.set_board_turn(runtime.BoardTurnContext(
        turn_id="turn_content", mode=runtime.MODE_INTERACTIVE, board_id=BOARD,
        submission_id=SUBMISSION, topic_id="topic_content", attempt=1))
    try:
        yield {"conn": conn, "service": service, "tools": tools,
               "board_store": board_store, "models": models}
    finally:
        runtime.clear_delivered_questions()
        runtime.reset_board_turn(token)


def _run(value):
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _tool(env, name: str, arguments: dict):
    return _run(env["tools"][name].run(**arguments))


def _confirmed_intent(env, *, max_new_cards: int = 5) -> str:
    proposed = env["service"].propose_intents(
        env["conn"], board_id=BOARD, submission_id=SUBMISSION,
        intents=[_spec("a1", scope={
            "allowed_actions": ["create_card", "update_card"],
            "max_new_cards": max_new_cards})],
        resolved_question_ids=())
    assert proposed.get("ok") is True, f"{CLAUSE}：前置提案失败：{str(proposed)[:300]}"
    intent_id = str((proposed.get("client_refs") or {})["a1"])
    confirmed = env["service"].confirm_intents(
        env["conn"], board_id=BOARD, intent_ids=[intent_id])
    assert confirmed.get("ok") is True, f"{CLAUSE}：前置确认失败：{str(confirmed)[:300]}"
    return intent_id


def _result_card_id(result) -> str:
    data = result.data or {}
    nested = data.get("result") if isinstance(data.get("result"), dict) else {}
    card_id = nested.get("card_id") or data.get("card_id") or nested.get("id")
    assert card_id, f"{CLAUSE}：工具结果缺少真实卡片 id：{str(data)[:400]}"
    return str(card_id)


def _cards(env) -> dict:
    state = env["board_store"].current_state(env["conn"], BOARD)
    return {str(card.get("id")): card for card in state.get("cards") or []}


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
    result = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_long_1",
        "content": long_markdown, "title": "长文卡"})
    assert result.ok is True, f"{CLAUSE}：长正文创建应成功（实际 {result}）"
    card_id = _result_card_id(result)
    saved = _cards(env)[card_id]
    assert saved.get("content") == long_markdown, (
        f"{CLAUSE}：正文必须逐字完整保存（实际 {len(str(saved.get('content') or ''))} 字符）")


def test_long_content_is_returned_by_board_state_api_completely(client, db_conn):
    """§13.5：API 返回完整正文，不静默截断。"""
    from agent.interactive import board_store, models

    _ensure_board(db_conn)
    content = "# 标题\n\n" + ("正文内容。" * 800)
    state = models.empty_state(BOARD)
    card = models.new_card("text", content, checked=True)
    state["cards"] = [card]
    board_store.save_board(db_conn, BOARD, state, reason="p3-d-long")

    client_obj, _ctx = client
    response = client_obj.get(f"/api/interactive/boards/{BOARD}/state")
    assert response.status_code == 200, (
        f"{CLAUSE}：板面读取失败（{response.status_code}: {response.text[:300]}）")
    assert content in response.text, (
        f"{CLAUSE}：API 必须返回完整正文（不允许静默截断）：{len(response.text)} 字节")


def test_empty_content_is_rejected_at_registry(env):
    """§13.5：空正文在参数层被拒绝，不产生假卡。"""
    intent_id = _confirmed_intent(env)
    result = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_empty", "content": ""})
    assert result.ok is False, f"{CLAUSE}：空正文不得创建卡片（实际 {result}）"
    text = str(result.code or "") + str(result.error or "") + str(result.data or "")
    assert "invalid_arguments" in text or "参数" in text, (
        f"{CLAUSE}：空正文应报参数校验失败：{text[:300]}")


def test_edit_save_does_not_call_model(env, monkeypatch):
    """§13.5：编辑 / 保存本身不调用模型（旧空正文二次生成路径也不得触发）。"""
    execution = _load("agent.interactive.execution")

    def _bomb(*args, **kwargs):
        raise AssertionError("契约 §13.5：编辑 / 保存不得调用模型或旧二次生成路径")

    monkeypatch.setattr(execution, "_generate_text", _bomb)
    intent_id = _confirmed_intent(env)
    created = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_edit_1", "content": "原始正文"})
    card_id = _result_card_id(created)
    new_content = "# 编辑后的正文\n\n" + "内容。" * 600
    version = int(_cards(env)[card_id].get("version") or 1)
    result = _tool(env, "board_update_card", {
        "intent_id": intent_id, "operation_key": "op_edit_2", "card_id": card_id,
        "expected_version": version, "patch": {"content": new_content}})
    assert result.ok is True, f"{CLAUSE}：编辑保存应成功（实际 {result}）"
    assert _cards(env)[card_id].get("content") == new_content, (
        f"{CLAUSE}：编辑后的正文必须完整保存")


def test_edit_stale_version_does_not_overwrite_user_edit(env):
    """§13.5：编辑前读取的版本已过期时不得覆盖用户后来的修改。"""
    intent_id = _confirmed_intent(env)
    created = _tool(env, "board_create_card", {
        "intent_id": intent_id, "operation_key": "op_stale_1", "content": "原始正文"})
    card_id = _result_card_id(created)
    version = int(_cards(env)[card_id].get("version") or 1)
    state = env["board_store"].current_state(env["conn"], BOARD)
    for card in state["cards"]:
        if str(card.get("id")) == card_id:
            card["content"] = "用户后来的修改"
            card["version"] = version + 1
    env["board_store"].save_board(env["conn"], BOARD, state, reason="user-edit")
    result = _tool(env, "board_update_card", {
        "intent_id": intent_id, "operation_key": "op_stale_2", "card_id": card_id,
        "expected_version": version, "patch": {"content": "模型想覆盖的正文"}})
    assert result.ok is False, f"{CLAUSE}：旧版本写入必须冲突拒绝（实际 {result}）"
    assert _cards(env)[card_id].get("content") == "用户后来的修改", (
        f"{CLAUSE}：不得覆盖用户后来的修改")
