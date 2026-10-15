"""互动模式：批准后的真实执行 / 暂停取消恢复 / 允许操作集合（子智能体 C）。

契约：docs/interactive-mode-contract.md §14（14.1 执行状态 / 14.3 run_execution /
14.4 HTTP / 14.5 允许的板面操作集合）。

全部用例只用 fake / stub provider：不联网、不依赖真实 API Key、不产生模型费用。
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.interactive import board, board_store, execution, intents, models

BOARD = "board_exec_test"
UNDERSTANDING = "u_exec_test"


# --- 脚手架 -----------------------------------------------------------------


def _seed(conn: sqlite3.Connection) -> dict:
    """两份材料 + 一条注释：真实执行的材料依据都引用它们。"""
    file_a = models.new_card("file", "材料 A", meta={"file": {"name": "a.pdf"}}, x=20.0, y=20.0)
    file_b = models.new_card("file", "材料 B", meta={"file": {"name": "b.pdf"}}, x=20.0, y=180.0)
    note = models.new_card("text", "只勾选这一条注释", checked=True, x=20.0, y=340.0)
    state = models.empty_state(BOARD)
    state["cards"] = [file_a, file_b, note]
    board_store.save_board(conn, BOARD, state, reason="test-seed")
    return {"a": file_a, "b": file_b, "note": note}


def _state(conn: sqlite3.Connection) -> dict:
    return board_store.load_board(conn, BOARD)["state"]


def _cards(conn: sqlite3.Connection) -> dict:
    return {card["id"]: card for card in _state(conn)["cards"]}


def _live_cards(conn: sqlite3.Connection) -> dict:
    return {cid: card for cid, card in _cards(conn).items() if not card["deleted"]}


def _listed(conn: sqlite3.Connection) -> dict:
    return {item["id"]: item for item in intents.list_intents(conn, BOARD)["intents"]}


def _reply_cards(conn: sqlite3.Connection) -> list[dict]:
    return [
        card
        for card in _live_cards(conn).values()
        if isinstance(card.get("meta"), dict) and card["meta"].get("reply")
    ]


def _create(conn: sqlite3.Connection, operations: list[dict], **over) -> dict:
    """按真实入口建立一条意图（understanding_id + 已校验的操作集合）。"""
    payload = {
        "intentKey": over.pop("intentKey", "k_exec"),
        "title": over.pop("title", "整理两份材料"),
        "summary": over.pop("summary", ""),
        "goal": over.pop("goal", "把两份材料整理成一张对比摘要"),
        "expected": over.pop("expected", "板面上出现一张对比摘要结果卡片"),
        "operations": operations,
    }
    payload.update(over)
    created = intents.create_intents_from_understanding(
        conn,
        board_id=BOARD,
        understanding_id=str(over.pop("understanding", UNDERSTANDING)),
        payloads=[payload],
    )
    assert created, "这条 payload 应当被落成意图"
    return created[0]


def _approve(conn: sqlite3.Connection, intent_id: str, *, instance_id: str = "proc-1") -> dict:
    result = intents.approve_intent(conn, intent_id, instance_id=instance_id)
    assert result["ok"] is True, result
    assert result.get("execution"), "批准后必须实际建立执行记录"
    return result["execution"]


async def _run(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    provider=None,
    instance_id: str = "proc-1",
) -> dict:
    return await execution.run_execution(
        conn, execution_id, adapter_provider=provider, instance_id=instance_id
    )


class _TextProvider:
    """脚本化的 stub 模型：返回固定正文，并记下收到的请求（不联网）。"""

    def __init__(self, text: str = "生成的正文") -> None:
        self.text = text
        self.requests: list[list] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        self.requests.append(list(messages))
        return Completion(message=ChatMessage(role="assistant", content=self.text))

    def __call__(self):
        return self


class _TransactionProbeProvider(_TextProvider):
    """在模型调用那一刻检查：调用方**没有**持有数据库事务。"""

    def __init__(self, conn: sqlite3.Connection, text: str = "探针正文") -> None:
        super().__init__(text)
        self.conn = conn
        self.in_transaction_at_call: bool | None = None

    async def complete(self, messages, tools, **kwargs) -> Completion:
        self.in_transaction_at_call = bool(self.conn.in_transaction)
        return await super().complete(messages, tools, **kwargs)


class _PausingProvider(_TextProvider):
    """在模型返回**之后**暂停真实执行：用来验证迟到结果不会被写回。"""

    def __init__(self, conn: sqlite3.Connection, intent_id: str, *, instance_id="proc-1"):
        super().__init__("迟到的正文")
        self.conn = conn
        self.intent_id = intent_id
        self.instance_id = instance_id

    async def complete(self, messages, tools, **kwargs) -> Completion:
        execution.pause_intent(self.conn, self.intent_id, instance_id=self.instance_id)
        return await super().complete(messages, tools, **kwargs)


# --- 允许的操作集合：每条的正例 ---------------------------------------------


async def test_add_reply_card_lands_with_reply_meta(db_conn):
    """add_reply_card：结果卡片落地，meta 写 reply{intentId, understandingId} 可定位。"""
    seed = _seed(db_conn)
    intent = _create(
        db_conn,
        [{"op": "add_reply_card", "content": "对比摘要：两份材料的共同点是……", "x": 40.0, "y": 200.0}],
        materialRefs=[seed["a"]["id"], seed["b"]["id"]],
    )
    approved = _approve(db_conn, intent["id"])
    assert approved["status"] == "running"
    assert [step["op"] for step in approved["steps"]] == ["add_reply_card"]
    assert approved["steps"][0]["status"] == "pending"

    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True, result
    assert result["execution"]["status"] == "done"
    assert result["execution"]["result"]["cardIds"] == [
        result["execution"]["steps"][0]["cardId"]
    ]

    card_id = result["execution"]["steps"][0]["cardId"]
    card = _cards(db_conn)[card_id]
    assert card["kind"] == "reply" and card["checked"] is False and card["deleted"] is False
    assert card["meta"]["reply"]["intentId"] == intent["id"]
    assert card["meta"]["reply"]["understandingId"] == UNDERSTANDING
    assert card["meta"]["reply"]["executionId"] == approved["id"]
    assert card["meta"]["reply"]["stepIndex"] == 0

    listed = _listed(db_conn)
    assert listed[intent["id"]]["status"] == "done"
    assert listed[intent["id"]]["applied"]["cardIds"] == [card_id]
    assert listed[intent["id"]]["operations"][0]["op"] == "add_reply_card"

    # 逐步 checkpoint 落库（写前 / 写后各一条）
    row = execution.get_execution(db_conn, approved["id"])
    checkpoints = json.loads(row["checkpoints"])
    assert [item["phase"] for item in checkpoints] == ["before", "done"]
    assert json.loads(row["steps"])[0]["status"] == "done"

    # 审批绑定：方案版本 / 材料版本 / 目标对象版本都记在计划里
    plan = json.loads(row["plan"])
    assert plan["approval"]["operationsHash"]
    assert plan["approval"]["operationCount"] == 1
    assert plan["approval"]["materials"], "审批必须绑定材料版本"
    assert plan["approval"]["boardSeq"] is not None

    # 执行记录可查（GET /boards/{id}/executions 的数据来源）
    listed_executions = execution.list_executions(db_conn, BOARD)["executions"]
    assert [item["id"] for item in listed_executions] == [approved["id"]]


async def test_add_text_card_lands_as_text_card(db_conn):
    """add_text_card：文字卡片落地（不是结果卡片），不参与注释勾选。"""
    _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_text_card", "content": "整理要点：1）……"}])
    approved = _approve(db_conn, intent["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True
    card = _cards(db_conn)[result["execution"]["steps"][0]["cardId"]]
    assert card["kind"] == "text"
    assert card["content"] == "整理要点：1）……"
    assert card["checked"] is False and card["deleted"] is False


async def test_move_card_moves_only_position(db_conn):
    """move_card：只改位置，不改正文（正文是用户的内容）。"""
    seed = _seed(db_conn)
    intent = _create(
        db_conn,
        [{"op": "move_card", "cardId": seed["a"]["id"], "x": 500.0, "y": 640.0}],
        materialRefs=[seed["a"]["id"]],
    )
    approved = _approve(db_conn, intent["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True
    card = _cards(db_conn)[seed["a"]["id"]]
    assert (card["x"], card["y"]) == (500.0, 640.0)
    assert card["content"] == seed["a"]["content"]
    assert card["checked"] is False


async def test_group_cards_creates_ordered_group_with_new_card(db_conn):
    """group_cards：先落结果卡片再成组，成员与顺序按批准内容；旧组被显式调整。"""
    seed = _seed(db_conn)
    # 先让材料 A/B 属于一个旧组，验证「成员会被显式移出」
    state = board.create_group(_state(db_conn), [seed["a"]["id"], seed["b"]["id"]], "旧组")
    board_store.save_board(db_conn, BOARD, state, reason="seed-group")

    intent = _create(
        db_conn,
        [
            {"op": "add_reply_card", "content": "组内摘要", "cardId": "r1"},
            {"op": "group_cards", "cardIds": [seed["a"]["id"], "r1"], "ordered": True, "name": "对比组"},
        ],
        materialRefs=[seed["a"]["id"]],
    )
    approved = _approve(db_conn, intent["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True
    steps = result["execution"]["steps"]
    new_card_id = steps[0]["cardId"]
    group_id = steps[1]["groupId"]

    groups = {item["id"]: item for item in _state(db_conn)["groups"] if not item["deleted"]}
    group = groups[group_id]
    assert group["name"] == "对比组"
    assert group["ordered"] is True
    assert group["members"] == [seed["a"]["id"], new_card_id]
    # 材料 B 留在旧组（它没有被批准进入新组）
    old_groups = [item for item in groups.values() if seed["b"]["id"] in item["members"]]
    assert old_groups and seed["a"]["id"] not in old_groups[0]["members"]


async def test_order_group_reorders_members(db_conn):
    """order_group：按批准的成员顺序重排，不新增 / 不删除成员。"""
    seed = _seed(db_conn)
    state = board.create_group(
        _state(db_conn), [seed["a"]["id"], seed["b"]["id"], seed["note"]["id"]], "清单"
    )
    board_store.save_board(db_conn, BOARD, state, reason="seed-group")
    group_id = [g for g in _state(db_conn)["groups"] if not g["deleted"]][0]["id"]

    intent = _create(
        db_conn,
        [{"op": "order_group", "groupId": group_id, "memberOrder": [seed["note"]["id"], seed["a"]["id"]]}],
    )
    approved = _approve(db_conn, intent["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True
    group = [g for g in _state(db_conn)["groups"] if g["id"] == group_id][0]
    assert group["members"] == [seed["note"]["id"], seed["a"]["id"], seed["b"]["id"]]
    assert group["ordered"] is True
    assert set(group["members"]) == {seed["a"]["id"], seed["b"]["id"], seed["note"]["id"]}


async def test_add_link_creates_relation_and_never_overwrites_existing(db_conn):
    """add_link：建立关系；同一对端点已有关系时不动用户已有那条。"""
    seed = _seed(db_conn)
    first = _create(
        db_conn,
        [{"op": "add_link", "src": seed["a"]["id"], "dst": seed["b"]["id"], "meaning": "对比依据"}],
        intentKey="k_link",
        materialRefs=[seed["a"]["id"], seed["b"]["id"]],
    )
    approved = _approve(db_conn, first["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True
    link_id = result["execution"]["steps"][0]["linkId"]
    links = {item["id"]: item for item in _state(db_conn)["links"] if not item["deleted"]}
    assert links[link_id]["meaning"] == "对比依据"
    assert {links[link_id]["src"], links[link_id]["dst"]} == {seed["a"]["id"], seed["b"]["id"]}

    # 第二项任务对同一对端点写一个不同的含义：不能覆盖已有关系
    second = _create(
        db_conn,
        [{"op": "add_link", "src": seed["a"]["id"], "dst": seed["b"]["id"], "meaning": "另一种含义"}],
        intentKey="k_link2",
        understanding="u_exec_test_2",
    )
    approved2 = _approve(db_conn, second["id"])
    result2 = await _run(db_conn, approved2["id"])
    assert result2["ok"] is True
    assert "未改动已有关系" in result2["execution"]["steps"][0]["detail"]
    links = {item["id"]: item for item in _state(db_conn)["links"] if not item["deleted"]}
    assert len([item for item in links.values() if {item["src"], item["dst"]} == {seed["a"]["id"], seed["b"]["id"]}]) == 1
    assert links[link_id]["meaning"] == "对比依据"


async def test_generated_reply_text_uses_the_model_link(db_conn):
    """结果卡片正文缺省时走同一条模型链路；输出只成为那张卡片的正文。"""
    _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_reply_card"}])
    approved = _approve(db_conn, intent["id"])
    provider = _TextProvider("模型生成的摘要正文")
    result = await _run(db_conn, approved["id"], provider=provider)
    assert result["ok"] is True
    assert len(provider.requests) == 1, "只调用一次模型"
    card = _cards(db_conn)[result["execution"]["steps"][0]["cardId"]]
    assert card["content"] == "模型生成的摘要正文"
    # 模型输出没有被解析成任何新的操作：步骤数与批准时一致
    assert len(result["execution"]["steps"]) == 1


async def test_missing_adapter_gives_readable_failure(db_conn):
    """没有可用模型链路：给出可读失败原因，不写入半截内容。"""
    _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_reply_card"}])
    approved = _approve(db_conn, intent["id"])
    before = _state(db_conn)
    result = await _run(db_conn, approved["id"], provider=None)
    assert result["ok"] is False and result["reason"] == "no_adapter"
    assert "模型链路" in result["detail"]
    assert _state(db_conn) == before
    assert _listed(db_conn)[intent["id"]]["status"] == "failed"


async def test_model_call_never_holds_a_database_transaction(db_conn):
    """耗时工作不得阻塞服务请求：模型调用期间不持有数据库事务或全局锁。"""
    _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_reply_card"}])
    approved = _approve(db_conn, intent["id"])
    provider = _TransactionProbeProvider(db_conn)
    result = await _run(db_conn, approved["id"], provider=provider)
    assert result["ok"] is True
    assert provider.in_transaction_at_call is False, "模型调用期间不得持有数据库事务"


# --- 越权操作：不落成可执行任务、也不硬做 -----------------------------------


def test_forbidden_operations_are_never_created(db_conn):
    """删除 / 隐藏 / 勾选等越权操作不得落成可执行任务（同批合法项仍要保留）。"""
    seed = _seed(db_conn)
    created = intents.create_intents_from_understanding(
        db_conn,
        board_id=BOARD,
        understanding_id="u_forbidden",
        payloads=[
            {
                "intentKey": "k_delete",
                "title": "删掉材料",
                "operations": [{"op": "delete_card", "cardId": seed["a"]["id"]}],
            },
            {
                "intentKey": "k_hide",
                "title": "隐藏材料",
                "operations": [{"op": "hide_card", "cardId": seed["a"]["id"]}],
            },
            {
                "intentKey": "k_none",
                "title": "没有任何板面操作",
                "operations": [],
            },
            {
                "intentKey": "k_ok",
                "title": "写一张摘要",
                "operations": [{"op": "add_reply_card", "content": "合法的一步"}],
            },
        ],
    )
    assert [item["intentKey"] for item in created] == ["k_ok"]
    rows = db_conn.execute(
        "SELECT COUNT(*) AS c FROM board_intents WHERE intent_key IN ('k_delete', 'k_hide')"
    ).fetchone()
    assert rows["c"] == 0


async def test_forbidden_operation_in_plan_is_refused_before_any_write(db_conn):
    """批准之后计划里出现越权操作：写入之前拒绝，退回「需要重新提出方案与审批」。"""
    seed = _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_reply_card", "content": "原本批准的内容"}])
    approved = _approve(db_conn, intent["id"])
    before = _state(db_conn)

    row = execution.get_execution(db_conn, approved["id"])
    plan = json.loads(row["plan"])
    plan["ops"].append({"op": "set_checked", "cardId": seed["a"]["id"], "checked": True})
    db_conn.execute(
        "UPDATE board_executions SET plan = ? WHERE id = ?", (models.dumps(plan), approved["id"])
    )

    result = await _run(db_conn, approved["id"])
    assert result["ok"] is False and result["reason"] == "needs_new_plan"
    assert result["requiresUpdate"] is True
    assert _state(db_conn) == before, "越权操作不得写入任何改动"
    assert _listed(db_conn)[intent["id"]]["status"] == "needs_update"


async def test_operations_change_after_approval_needs_new_plan(db_conn):
    """审批绑定方案版本：批准之后操作集合被改过，执行前退回重新审批，不硬做。"""
    _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_reply_card", "content": "原来批准的内容"}])
    approved = _approve(db_conn, intent["id"])
    before = _state(db_conn)
    db_conn.execute(
        "UPDATE board_intents SET operations = ? WHERE id = ?",
        (models.dumps([{"op": "add_reply_card", "content": "偷偷换掉的内容"}]), intent["id"]),
    )
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is False and result["reason"] == "needs_new_plan"
    assert "方案版本" in result["detail"]
    assert _state(db_conn) == before
    assert _listed(db_conn)[intent["id"]]["status"] == "needs_update"


# --- 执行前重核：材料与对象版本 ---------------------------------------------


def test_material_change_before_approval_blocks_execution(db_conn):
    """批准之前材料已经变过：标记 needs_update，不建立执行。"""
    seed = _seed(db_conn)
    intent = _create(
        db_conn, [{"op": "add_reply_card", "content": "摘要"}], materialRefs=[seed["a"]["id"]]
    )
    state = _state(db_conn)
    for card in state["cards"]:
        if card["id"] == seed["a"]["id"]:
            card["content"] = "材料 A（用户改过）"
    board_store.save_board(db_conn, BOARD, state, reason="user-edit")

    result = intents.approve_intent(db_conn, intent["id"], instance_id="proc-1")
    assert result["ok"] is False and result["reason"] == "needs_update"
    assert result["requiresUpdate"] is True
    assert execution.active_execution(db_conn, intent["id"]) is None


async def test_material_change_after_approval_pauses_real_execution(db_conn):
    """批准之后材料变了：真实执行停下并保留进度，不按旧快照写回。"""
    seed = _seed(db_conn)
    intent = _create(
        db_conn, [{"op": "add_reply_card", "content": "摘要"}], materialRefs=[seed["a"]["id"]]
    )
    approved = _approve(db_conn, intent["id"])

    state = _state(db_conn)
    for card in state["cards"]:
        if card["id"] == seed["a"]["id"]:
            card["content"] = "材料 A（批准之后被改）"
    board_store.save_board(db_conn, BOARD, state, reason="user-edit")
    before = _state(db_conn)

    result = await _run(db_conn, approved["id"])
    assert result["ok"] is False and result["paused"] is True
    assert result["reason"] == "materials_changed"
    assert "材料" in result["detail"]
    assert _state(db_conn) == before, "暂停时不得写入板面"
    assert result["execution"]["status"] == "paused"
    assert _listed(db_conn)[intent["id"]]["status"] == "paused"


async def test_user_edit_of_target_is_not_overwritten(db_conn):
    """对象版本保护：用户改过要改动的对象时，不按旧快照覆盖，任务暂停。"""
    seed = _seed(db_conn)
    # 注意：这里刻意不把这张卡写进 materialRefs —— 验证的是**对象版本**保护
    # （材料版本保护由另一条用例覆盖）。
    intent = _create(
        db_conn, [{"op": "move_card", "cardId": seed["a"]["id"], "x": 500.0, "y": 640.0}]
    )
    approved = _approve(db_conn, intent["id"])
    state = _state(db_conn)
    for card in state["cards"]:
        if card["id"] == seed["a"]["id"]:
            card["content"] = "用户自己改过的正文"
    board_store.save_board(db_conn, BOARD, state, reason="user-edit")

    result = await _run(db_conn, approved["id"])
    assert result["ok"] is False and result["paused"] is True
    assert result["reason"] == "objects_changed"
    card = _cards(db_conn)[seed["a"]["id"]]
    assert card["content"] == "用户自己改过的正文"
    assert (card["x"], card["y"]) == (seed["a"]["x"], seed["a"]["y"]), "不得按旧快照移动"


# --- intent_key：沿用身份，不复制新任务 -------------------------------------


def test_create_intents_is_idempotent_per_understanding(db_conn):
    """同一 understanding_id + intent_key 重复调用不产生重复意图。"""
    _seed(db_conn)
    first = _create(db_conn, [{"op": "add_reply_card", "content": "第一版"}], intentKey="k_same")
    again = intents.create_intents_from_understanding(
        db_conn,
        board_id=BOARD,
        understanding_id=UNDERSTANDING,
        payloads=[
            {
                "intentKey": "k_same",
                "title": "整理两份材料",
                "operations": [{"op": "add_reply_card", "content": "第一版"}],
            }
        ],
    )
    assert [item["id"] for item in again] == [first["id"]]
    rows = db_conn.execute(
        "SELECT COUNT(*) AS c FROM board_intents WHERE board_id = ?", (BOARD,)
    ).fetchone()
    assert rows["c"] == 1


def test_intent_key_updates_pending_intent_instead_of_new_task(db_conn):
    """旧意图还在等待审批：按 intent_key 更新它，保留 id 与历史。"""
    _seed(db_conn)
    first = _create(
        db_conn, [{"op": "add_reply_card", "content": "第一版"}], intentKey="k_work"
    )
    second = _create(
        db_conn,
        [{"op": "add_reply_card", "content": "第二版"}],
        intentKey="k_work",
        understanding="u_exec_test_2",
        goal="按新要求整理",
    )
    assert second["id"] == first["id"]
    assert second["operations"][0]["content"] == "第二版"
    assert second["understandingId"] == "u_exec_test_2"
    assert second["goal"] == "按新要求整理"
    rows = db_conn.execute(
        "SELECT COUNT(*) AS c FROM board_intents WHERE board_id = ?", (BOARD,)
    ).fetchone()
    assert rows["c"] == 1


async def test_intent_key_after_done_creates_new_identity(db_conn):
    """旧意图已经执行完成：新一轮工作用新的身份建行，不覆盖已完成的成果。"""
    _seed(db_conn)
    first = _create(
        db_conn, [{"op": "add_reply_card", "content": "第一轮"}], intentKey="k_again"
    )
    approved = _approve(db_conn, first["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True
    first_card = result["execution"]["steps"][0]["cardId"]

    second = _create(
        db_conn,
        [{"op": "add_reply_card", "content": "第二轮"}],
        intentKey="k_again",
        understanding="u_exec_test_2",
    )
    assert second["id"] != first["id"]
    assert second["intentKey"] == "k_again#2"
    assert _listed(db_conn)[first["id"]]["status"] == "done"
    assert first_card in _live_cards(db_conn)


def test_dependencies_and_conflicts_land_by_intent_key(db_conn):
    """依赖与冲突按 payload 的 intentKey 落库（映射成真实 id）。"""
    _seed(db_conn)
    created = intents.create_intents_from_understanding(
        db_conn,
        board_id=BOARD,
        understanding_id="u_deps",
        payloads=[
            {
                "intentKey": "k_one",
                "title": "第一项",
                "operations": [{"op": "add_reply_card", "content": "一"}],
                "conflictsWith": ["k_two"],
                "conflictKey": "pair",
            },
            {
                "intentKey": "k_two",
                "title": "第二项",
                "operations": [{"op": "add_reply_card", "content": "二"}],
                "dependsOn": ["k_one"],
                "conflictKey": "pair",
            },
        ],
    )
    by_key = {item["intentKey"]: item for item in created}
    assert by_key["k_one"]["conflictsWith"] == [by_key["k_two"]["id"]]
    assert by_key["k_two"]["dependsOn"] == [by_key["k_one"]["id"]]
    assert by_key["k_two"]["conflictKey"] == "pair"


# --- 暂停 / 取消 / 恢复：作用于真实执行 -------------------------------------


async def test_pause_stops_real_execution_and_late_result_is_dropped(db_conn):
    """暂停作用于真实执行：模型已经返回的迟到正文不再写回。"""
    _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_reply_card"}])
    approved = _approve(db_conn, intent["id"])
    provider = _PausingProvider(db_conn, intent["id"])

    result = await _run(db_conn, approved["id"], provider=provider)
    assert result["discarded"] is True
    assert result["reason"] == "paused"
    assert not _reply_cards(db_conn), "迟到的模型结果不得写回板面"
    row = execution.get_execution(db_conn, approved["id"])
    assert row["status"] == "paused"
    assert json.loads(row["steps"])[0]["status"] == "running"
    assert _listed(db_conn)[intent["id"]]["status"] == "paused"


async def test_resume_does_not_repeat_completed_steps(db_conn):
    """恢复不重复已完成步骤：第一步的结果不会被重复新增。"""
    _seed(db_conn)
    intent = _create(
        db_conn,
        [
            {"op": "add_reply_card", "content": "第一步（已在批准内容里）"},
            {"op": "add_reply_card"},
        ],
    )
    approved = _approve(db_conn, intent["id"])
    plan = json.loads(execution.get_execution(db_conn, approved["id"])["plan"])
    first_card_id = plan["ops"][0]["id"]

    provider = _PausingProvider(db_conn, intent["id"])
    paused = await _run(db_conn, approved["id"], provider=provider)
    assert paused["discarded"] is True
    assert first_card_id in _live_cards(db_conn)
    assert len(_reply_cards(db_conn)) == 1

    resumed = execution.resume_intent(db_conn, intent["id"], instance_id="proc-2")
    assert resumed["ok"] is True
    assert resumed["execution"]["attempt"] == 2
    steps = json.loads(execution.get_execution(db_conn, approved["id"])["steps"])
    assert [step["status"] for step in steps] == ["done", "pending"]

    result = await _run(
        db_conn, approved["id"], provider=_TextProvider("第二条正文"), instance_id="proc-2"
    )
    assert result["ok"] is True
    steps = result["execution"]["steps"]
    assert [step["status"] for step in steps] == ["done", "done"]
    assert steps[0]["cardId"] == first_card_id
    replies = _reply_cards(db_conn)
    assert len(replies) == 2, "第一步只落一次，不重复新增"
    assert first_card_id in {card["id"] for card in replies}
    assert {card["content"] for card in replies} == {"第一步（已在批准内容里）", "第二条正文"}


async def test_board_save_pauses_real_execution_and_drops_late_result(db_conn):
    """保存改动材料时真实执行同步暂停：正在等待的模型结果不再写回。"""
    seed = _seed(db_conn)
    intent = _create(
        db_conn, [{"op": "add_reply_card"}], materialRefs=[seed["a"]["id"]]
    )
    approved = _approve(db_conn, intent["id"])

    state = _state(db_conn)
    for card in state["cards"]:
        if card["id"] == seed["a"]["id"]:
            card["content"] = "保存时改过的材料"
    board_store.save_board(db_conn, BOARD, state, reason="user-edit")

    impacted = intents.on_board_saved(db_conn, board_id=BOARD, state=_state(db_conn))
    assert intent["id"] in impacted["affected"]
    assert impacted["paused"], "执行中的任务在材料变化时必须被暂停"
    assert execution.get_execution(db_conn, approved["id"])["status"] == "paused"

    provider = _TextProvider("迟到的正文")
    result = await _run(db_conn, approved["id"], provider=provider)
    assert result.get("discarded") is True
    assert provider.requests == [], "暂停之后不得再调用模型"
    assert not _reply_cards(db_conn)


def test_restart_downgrades_running_task_and_execution_to_paused(db_conn):
    """重启恢复：别的实例遗留的 running 降级为 paused；真实执行记录同步暂停。"""
    _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_reply_card"}])
    approved = _approve(db_conn, intent["id"], instance_id="proc-1")

    assert intents.recover_running_intents(db_conn, BOARD, instance_id="proc-1") == {"paused": []}
    assert execution.get_execution(db_conn, approved["id"])["status"] == "running"

    recovered = intents.recover_running_intents(db_conn, BOARD, instance_id="proc-2")
    assert recovered["paused"] == [intent["id"]]
    assert _listed(db_conn)[intent["id"]]["status"] == "paused"
    row = execution.get_execution(db_conn, approved["id"])
    assert row["status"] == "paused" and row["owner_instance_id"] is None
    assert "重启" in str(row["error"])

    # 继续（同一进程）之后可以真的跑完
    resumed = execution.resume_intent(db_conn, intent["id"], instance_id="proc-2")
    assert resumed["ok"] is True and resumed["execution"]["status"] == "running"
    assert resumed["execution"]["attempt"] == 2


async def test_cancel_reverts_only_this_task_and_keeps_other_work(db_conn):
    """取消：撤回本任务造成的改动，保留用户后续编辑与其他任务的成果。"""
    seed = _seed(db_conn)
    other = _create(
        db_conn, [{"op": "add_reply_card", "content": "别的任务的结果"}], intentKey="k_other"
    )
    other_approved = _approve(db_conn, other["id"])
    other_result = await _run(db_conn, other_approved["id"])
    other_card = other_result["execution"]["steps"][0]["cardId"]

    target = _create(
        db_conn,
        [
            {"op": "add_reply_card", "content": "要取消的结果"},
            {"op": "add_reply_card"},
        ],
        intentKey="k_cancel",
    )
    approved = _approve(db_conn, target["id"])
    # 先落地第一步，再在第二步（模型调用）之后暂停：取消要撤回**已经造成**的改动
    paused = await _run(
        db_conn, approved["id"], provider=_PausingProvider(db_conn, target["id"])
    )
    assert paused["discarded"] is True
    steps = json.loads(execution.get_execution(db_conn, approved["id"])["steps"])
    assert steps[0]["status"] == "done" and steps[1]["status"] == "running"
    target_card = steps[0]["cardId"]
    assert target_card in _live_cards(db_conn)

    cancelled = execution.cancel_intent(db_conn, target["id"], instance_id="proc-1")
    assert cancelled["ok"] is True
    assert cancelled["execution"]["status"] == "cancelled"
    assert cancelled["intent"]["status"] == "cancelled"
    live = _live_cards(db_conn)
    assert target_card not in live, "被取消任务的结果卡片应当被撤回"
    assert other_card in live, "别的任务的成果不得被撤回"
    assert live[seed["a"]["id"]]["content"] == seed["a"]["content"]


async def test_failure_reverts_only_its_own_partial_changes(db_conn):
    """失败撤回只处理该任务实际造成且仍可安全撤回的改动。"""
    seed = _seed(db_conn)
    first = _create(
        db_conn, [{"op": "add_reply_card", "content": "第一项的结果"}], intentKey="k_first"
    )
    first_approved = _approve(db_conn, first["id"])
    first_result = await _run(db_conn, first_approved["id"])
    first_card = first_result["execution"]["steps"][0]["cardId"]

    second = _create(
        db_conn,
        [
            {"op": "add_reply_card", "content": "第二项的结果"},
            {"op": "move_card", "cardId": "c_已经不在板面上", "x": 1.0, "y": 2.0},
        ],
        intentKey="k_second",
    )
    approved = _approve(db_conn, second["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is False and result["reason"] == "step_failed"
    assert "已经不在板面上" in result["detail"]

    steps = json.loads(execution.get_execution(db_conn, approved["id"])["steps"])
    assert [step["status"] for step in steps] == ["done", "failed"]
    second_card = steps[0]["cardId"]
    live = _live_cards(db_conn)
    assert second_card not in live, "本任务造成的改动已被撤回"
    assert first_card in live, "别的任务的成果不得被撤回"
    assert live[seed["a"]["id"]]["content"] == seed["a"]["content"]
    assert result["revert"]["reverted"], "撤回报告必须说明撤回了什么"
    assert _listed(db_conn)[second["id"]]["status"] == "failed"
    # 不自动重试：再跑一次不会又执行一遍
    again = await _run(db_conn, approved["id"])
    assert again.get("discarded") is True
    assert second_card not in _live_cards(db_conn)


# --- 演示推进不得驱动真实任务 -----------------------------------------------


async def test_demo_advance_does_not_drive_real_execution(db_conn):
    """演示推进不得驱动真实任务：有活跃执行时被拒绝，执行与板面都不变。"""
    _seed(db_conn)
    intent = _create(db_conn, [{"op": "add_reply_card", "content": "真实结果"}])
    approved = _approve(db_conn, intent["id"])
    before = _state(db_conn)

    advance = intents.advance_intent(db_conn, intent["id"], outcome="done")
    assert advance["ok"] is False and advance["reason"] == "real_execution"
    assert advance["execution"]["id"] == approved["id"]
    assert _state(db_conn) == before
    assert execution.get_execution(db_conn, approved["id"])["status"] == "running"

    # 真实执行仍然照常完成
    result = await _run(db_conn, approved["id"], provider=_TextProvider("真实正文"))
    assert result["ok"] is True
    assert _listed(db_conn)[intent["id"]]["status"] == "done"


def test_demo_intents_do_not_create_real_executions(db_conn):
    """演示意图没有已批准的操作集合：批准只走可控状态机，不建立真实执行。"""
    _seed(db_conn)
    demo = intents.create_demo_intents(db_conn, board_id=BOARD)
    target = demo[0]
    approved = intents.approve_intent(db_conn, target["id"], instance_id="proc-1")
    assert approved["ok"] is True
    assert approved["execution"] is None
    assert execution.active_execution(db_conn, target["id"]) is None
    rows = db_conn.execute(
        "SELECT COUNT(*) AS c FROM board_executions WHERE board_id = ?", (BOARD,)
    ).fetchone()
    assert rows["c"] == 0


# --- F5 / F7 / F8（本轮新增）：类型化引用 / 落点 / 排序 ----------------------


async def test_preview_and_execution_share_placement_and_ref_map(db_conn):
    """F7：审批预览与真实执行用同一份落点/尺寸；F5：符号 → 正式对象映射稳定。"""
    _seed(db_conn)
    ops = [
        {"op": "add_reply_card", "content": "结论", "ref": "a"},
        {
            "op": "add_text_card",
            "content": "补充说明",
            "ref": "b",
            "place": {"relativeTo": "card:a", "side": "right", "gap": 24},
        },
    ]
    cards = _state(db_conn)["cards"]
    preview = execution.preview_from_operations(ops, cards=cards)
    preview_by_id = {card["id"]: card for card in preview["cards"]}
    assert preview["refs"]["a"] == "a" and preview["refs"]["b"] == "b"

    intent = _create(db_conn, ops)
    # 审批预览里存的卡片坐标就是 preview_from_operations 的坐标（同一函数）
    stored = _listed(db_conn)[intent["id"]]["preview"]
    stored_by_id = {card["id"]: card for card in stored["cards"]}
    for symbol in ("a", "b"):
        for key in ("x", "y", "w", "h"):
            assert stored_by_id[symbol][key] == preview_by_id[symbol][key]
    # 符号与其类型化写法都指向同一个预览对象 id
    assert stored["refs"]["a"] == stored["refs"]["card:a"] == "a"
    assert stored["refs"]["b"] == stored["refs"]["card:b"] == "b"

    approved = _approve(db_conn, intent["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True, result
    steps = result["execution"]["steps"]
    card_a = _cards(db_conn)[steps[0]["cardId"]]
    card_b = _cards(db_conn)[steps[1]["cardId"]]

    # 默认尺寸：reply 320×240、text 260×170（不是 240×120，也不是 1×1）
    assert (card_a["kind"], card_a["w"], card_a["h"]) == ("reply", 320.0, 240.0)
    assert (card_b["kind"], card_b["w"], card_b["h"]) == ("text", 260.0, 170.0)
    # place 由程序算出：b 在 a 右边、间距 24
    assert card_b["x"] == card_a["x"] + card_a["w"] + 24
    assert card_b["y"] == card_a["y"]
    # 默认落点绝不是 (0,0)
    assert (card_a["x"], card_a["y"]) != (0.0, 0.0)

    # 预览坐标 == 落地坐标
    for cid, card in ((steps[0]["cardId"], card_a), (steps[1]["cardId"], card_b)):
        assert cid not in preview_by_id  # 预览用符号 id，执行用正式 id
    assert (card_a["x"], card_a["y"]) == (
        preview_by_id["a"]["x"],
        preview_by_id["a"]["y"],
    )
    assert (card_b["x"], card_b["y"]) == (
        preview_by_id["b"]["x"],
        preview_by_id["b"]["y"],
    )
    refs = result["execution"]["objectRefs"]
    assert refs["a"] == card_a["id"] and refs["b"] == card_b["id"]


async def test_typed_refs_link_two_new_cards_and_do_not_duplicate(db_conn):
    """F5：同批声明的两张新卡可以直接建关系；重复执行 / 恢复不重复创建。"""
    _seed(db_conn)
    ops = [
        {"op": "add_reply_card", "content": "左边", "ref": "l"},
        {
            "op": "add_reply_card",
            "content": "右边",
            "ref": "r",
            "place": {"relativeTo": "card:l", "side": "right", "gap": 24},
        },
        {"op": "add_link", "src": "card:l", "dst": "card:r", "direction": True, "meaning": "先后"},
    ]
    intent = _create(db_conn, ops)
    approved = _approve(db_conn, intent["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True, result
    steps = result["execution"]["steps"]
    left_id, right_id = steps[0]["cardId"], steps[1]["cardId"]
    link = {item["id"]: item for item in _state(db_conn)["links"]}[steps[2]["linkId"]]
    assert (link["src"], link["dst"], link["meaning"], link["direction"]) == (
        left_id,
        right_id,
        "先后",
        True,
    )

    cards_before = len(_live_cards(db_conn))
    links_before = len(_state(db_conn)["links"])
    again = await _run(db_conn, approved["id"])
    assert again.get("discarded") is True, "已结束的执行不会重复落地"
    assert len(_live_cards(db_conn)) == cards_before
    assert len(_state(db_conn)["links"]) == links_before
    # 再点一次批准：已完成的任务不会被复活，也不会再建执行记录
    repeated = intents.approve_intent(db_conn, intent["id"], instance_id="proc-1")
    assert repeated["ok"] is False
    assert execution.active_execution(db_conn, intent["id"]) is None


def _arrange_seed(conn: sqlite3.Connection) -> dict:
    """三张文字卡 + 一个组（成员顺序 a, b, c）。"""
    cards = [
        models.new_card("text", "A", checked=True, x=20.0, y=20.0),
        models.new_card("text", "B", checked=True, x=300.0, y=20.0),
        models.new_card("text", "C", checked=True, x=600.0, y=20.0),
    ]
    state = models.empty_state(BOARD)
    state["cards"] = cards
    group = models.new_group(
        "一组", members=[cards[0]["id"], cards[1]["id"], cards[2]["id"]], default_name=False
    )
    group["id"] = "g_arrange"
    state["groups"] = [group]
    board_store.save_board(conn, BOARD, state, reason="seed-arrange")
    return {"a": cards[0], "b": cards[1], "c": cards[2], "group": "g_arrange"}


async def test_order_group_arrange_really_writes_positions(db_conn):
    """F8：arrange="row" 在调整顺序后**真实写入成员 x/y**，并统一更新组框。"""
    seed = _arrange_seed(db_conn)
    order = [seed["c"]["id"], seed["a"]["id"], seed["b"]["id"]]
    intent = _create(
        db_conn,
        [{"op": "order_group", "groupId": seed["group"], "memberOrder": order, "arrange": "row"}],
    )
    approved = _approve(db_conn, intent["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True, result
    cards = _cards(db_conn)
    xs = [cards[cid]["x"] for cid in order]
    ys = [cards[cid]["y"] for cid in order]
    assert xs == sorted(xs) and len(set(xs)) == 3, "row：从左到右、互不重叠"
    assert len(set(ys)) == 1, "row：同一行"
    assert "真实重排" in result["execution"]["steps"][0]["detail"]
    group = [g for g in _state(db_conn)["groups"] if g["id"] == seed["group"]][0]
    assert group["members"] == order
    left = min(card["x"] for card in cards.values() if not card["deleted"])
    top = min(card["y"] for card in cards.values() if not card["deleted"])
    assert group["x"] <= left and group["y"] <= top, "组框按既有口径跟着成员更新"


async def test_order_group_without_arrange_is_logic_only(db_conn):
    """F8：没有 arrange 时是**纯逻辑排序** —— 位置一个都不动，文案也如实说明。"""
    seed = _arrange_seed(db_conn)
    before = {cid: (card["x"], card["y"]) for cid, card in _cards(db_conn).items()}
    order = [seed["c"]["id"], seed["a"]["id"], seed["b"]["id"]]
    intent = _create(
        db_conn,
        [{"op": "order_group", "groupId": seed["group"], "memberOrder": order}],
    )
    approved = _approve(db_conn, intent["id"])
    result = await _run(db_conn, approved["id"])
    assert result["ok"] is True, result
    after = {cid: (card["x"], card["y"]) for cid, card in _cards(db_conn).items()}
    assert after == before, "纯逻辑排序不得移动任何卡片"
    group = [g for g in _state(db_conn)["groups"] if g["id"] == seed["group"]][0]
    assert group["members"] == order, "逻辑顺序确实更新了"
    detail = result["execution"]["steps"][0]["detail"]
    assert "只调整组内成员顺序" in detail and "没有移动" in detail

