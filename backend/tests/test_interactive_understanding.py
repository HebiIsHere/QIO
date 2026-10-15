"""互动理解（契约 §14.1 / §14.3 / §14.5）的真实模型路径测试。

一律使用 fake / mock provider（不联网、不读任何密钥）；材料解析用按冻结签名构造的
假模块替换（A 的 materials.py 与 B 并行开发，签名已冻结）。

覆盖：
1) 真实理解：两条材料 + 一条获准要求 → intent → 校验通过 → 调用 create_intents_from_understanding；
2) 换材料 / 换要求 → 上下文与产出相应变化（证明不是固定模板）；
3) 未勾选注释、hidden 对象、间接链接不进上下文；
4) clarification / no_action 都不创建意图；
5) 无效输出（越权操作 / 范围外对象 / 不存在的材料 id / 结构损坏）不落成任务 + 可读原因；
   自动修复**只调用一次**；
6) 受理幂等；重试只从冻结快照恢复（不读当前板面）；
7) 材料口径诚实：未读取 / 截断都如实记录，禁止声称已完整阅读。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import asyncio
import contextlib
import types

import pytest

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.interactive import board_store, models, submission as submission_module
from agent.interactive import text_card
from agent.interactive import understanding as u

BOARD = models.DEFAULT_BOARD_ID
NOWHERE = "这段文字不应该出现在上下文里"


# --- 工具 -----------------------------------------------------------------


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _group(gid: str, name: str, members: list[str], *, ordered: bool = False) -> dict:
    group = models.new_group(name, ordered=ordered, default_name=False, members=list(members))
    group["id"] = gid
    return group


def _link(lid: str, src: str, dst: str, *, meaning: str = "") -> dict:
    link = models.new_link(src, dst, meaning=meaning)
    link["id"] = lid
    return link


async def _save_and_submit(
    conn: sqlite3.Connection,
    cards: list[dict],
    *,
    groups: list[dict] | None = None,
    links: list[dict] | None = None,
    selection: list[str] | None = None,
    note: str = "",
) -> dict:
    board_store.ensure_board(conn, board_id=BOARD)
    state = {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups or []),
        "links": list(links or []),
        "selection": list(selection or []),
    }
    board_store.save_board(conn, BOARD, state, reason="test")
    result = await submission_module.submit_board(conn, BOARD, note=note)
    # A 的提交路径在受理成功后会排一个后台理解任务。这里我们**自己**用 fake adapter 驱动
    # run_understanding，所以先把那个默认（没有 adapter）的后台任务取消掉，避免两边抢同一行；
    # 受理本身照旧（记录已建立、delivery.understandingId 已给出）。
    understanding_id = (result.get("delivery") or {}).get("understandingId")
    if understanding_id:
        await _stop_background_understanding(str(understanding_id))
    return result


async def _stop_background_understanding(understanding_id: str) -> None:
    target = "interactive-understanding:" + str(understanding_id)
    for task in asyncio.all_tasks():
        if task.get_name() != target:
            continue
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


def _provider(adapter):
    async def _get():
        return adapter

    return _get


def _request_context(request: dict) -> dict:
    for message in request["messages"]:
        content = message.content or ""
        if "=== 上下文开始 ===" in content:
            payload = content.split("=== 上下文开始 ===", 1)[1]
            payload = payload.split("=== 上下文结束 ===", 1)[0].strip()
            return json.loads(payload)
    raise AssertionError("请求里没有结构化上下文")


def _adapter(*payloads: dict | str) -> FakeStreamAdapter:
    scripts = []
    for payload in payloads:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        scripts.append(StreamScript(text=text))
    return FakeStreamAdapter(scripts)


# --- 假材料解析模块（A 的 materials.resolve_materials 冻结签名） ------------


def _material_kinds() -> tuple:
    return models.MATERIAL_KINDS


#: 文件卡里会被当成图片交付的扩展名（与 materials.DELIVERABLE_IMAGE_TYPES 同一口径）
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _is_image_attachment(meta: dict) -> bool:
    """这张**文件卡**的附件是不是图片（图片没有独立 kind 了，扩展名判定）。"""
    block = meta.get("file") if isinstance(meta.get("file"), dict) else {}
    name = str(block.get("name") or meta.get("name") or "").strip().lower()
    return name.endswith(_IMAGE_SUFFIXES)


@pytest.fixture()
def fake_materials(monkeypatch):
    """按冻结签名实现的假材料解析：只读传进来的 state，不碰当前板面。"""
    seen: list[dict] = []
    module = types.ModuleType("agent.interactive.materials")

    def resolve_materials(conn, *, state, cards=None, max_chars=40000):
        seen.append(state)
        items = []
        for card in state.get("cards", []):
            kind = card.get("kind")
            meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
            # 测试用模拟：这张可见卡**没有**材料条目（材料解析模块降级 / 解析失败）。
            # 契约 §2.2 之后「可见卡」与「材料」在正常路径上重合，所以这个分支只能这样构造，
            # 用来验证理解链路仍然拒绝「把非材料卡当材料依据」。
            if meta.get("materialUnavailable"):
                continue
            # 2026-10-12 起（契约 §2.2）：材料通道 = **文字卡（整卡许可）** + 文件卡；
            # 链接与代码没有默认可见通道，旧 code / url 卡不再出现在材料里。
            # 图片现在是**文件卡的附件**（kind 里没有 image）：没有视觉能力时如实记 image_unsupported。
            if kind not in ("text",) + _material_kinds():
                continue
            name = meta.get("name") or meta.get("title") or card.get("content") or card.get("id")
            if kind == "file" and _is_image_attachment(meta):
                items.append({
                    "id": card["id"],
                    "cardId": card["id"],
                    "kind": kind,
                    "name": name,
                    "source": "image_unsupported",
                    "text": "",
                    "chars": 0,
                })
                continue
            if kind == "text":
                # 文字卡：整卡许可为真时，正文 = 块序列纯文本（含链接地址与代码正文）
                text = text_card.blocks_to_text(text_card.doc_from_card(card))
            else:
                text = str(card.get("content") or "")
            items.append({
                "id": card["id"],
                "cardId": card["id"],
                "kind": kind,
                "name": name,
                "source": "inline",
                "text": text,
                "chars": len(text),
            })
        return items

    module.resolve_materials = resolve_materials
    monkeypatch.setitem(sys.modules, "agent.interactive.materials", module)
    return {"module": module, "seen": seen}


@pytest.fixture()
def intents_stub(monkeypatch):
    """记录 create_intents_from_understanding 的调用（C 的实现与 B 并行）。"""
    from agent.interactive import intents as intents_module

    calls: list[dict] = []

    def fake_create(conn, *, board_id, understanding_id, payloads):
        calls.append({
            "board_id": board_id,
            "understanding_id": understanding_id,
            "payloads": payloads,
        })
        return [{"id": "i_fake_" + str(len(calls))}]

    monkeypatch.setattr(
        intents_module, "create_intents_from_understanding", fake_create, raising=False
    )
    return calls


def _accept(conn, result) -> dict:
    """复用提交路径**已经受理**的那条理解请求（A 的 submit_board 里建立的）。

    「同一 request_id 重复受理是幂等的」由专门的用例断言，这里不重复受理。
    """
    understanding_id = (result.get("delivery") or {}).get("understandingId")
    if understanding_id:
        record = u.get_understanding(conn, str(understanding_id))
        if record is not None:
            return record
    return u.request_understanding(
        conn,
        board_id=BOARD,
        submission_id=result["submission"]["id"],
        snapshot=result["after"],
    )


def _intent_payload(operations, *, ref: str = "c_file", **overrides) -> dict:
    intent = {
        "intentKey": "compare-materials",
        "title": "整理两份材料的差异",
        "goal": "让用户看到两份材料的关键差异",
        "summary": "对比两份材料的正文并给出差异",
        "basis": [{"cardId": ref, "role": "material", "version": "v1"}],
        "expected": "一张列出差异的结果卡片",
        "operations": operations,
        "impact": {"cardIds": [ref], "scope": "只新增 QIO 结果卡片"},
        "dependsOn": [],
        "conflictsWith": [],
        "conflictKey": "",
    }
    intent.update(overrides)
    return {"kind": "intent", "summary": "用户要求比较两份材料", "intents": [intent]}


# --- 1) 真实理解路径 -------------------------------------------------------


async def test_intent_understanding_creates_intents(db_conn, fake_materials, intents_stub):
    cards = [
        _card("c_file", "file", "甲材料正文：方案 A 的成本更高", meta={"name": "甲.txt"}),
        # 2026-10-12 起：第二份材料是**文字卡**（整卡许可），链接与代码没有默认可见通道
        _card("c_code", "text", "乙材料正文：方案 B 的工期更长", checked=True),
        _card("c_note", "text", "请比较这两份材料并给出差异", checked=True),
        _card("c_unchecked", "text", NOWHERE, checked=False),
        _card("c_hidden", "file", NOWHERE, hidden=True, meta={"name": "隐藏.txt"}),
    ]
    groups = [_group("g1", "两份材料", ["c_file", "c_code"])]
    links = [_link("l1", "c_file", "c_unchecked", meaning="越界关系")]
    result = await _save_and_submit(
        db_conn,
        cards,
        groups=groups,
        links=links,
        selection=["c_file", "c_code"],
        note="请特别关注成本一项",
    )
    accepted = _accept(db_conn, result)
    assert accepted["status"] == "accepted"
    # 提交路径已受理：复用的就是同一条理解请求（幂等由专门的用例断言）
    assert accepted["id"] == result["delivery"]["understandingId"]

    payload = _intent_payload([
        {"op": "add_reply_card", "content": "两份材料的差异如下……", "title": "差异"},
        {"op": "group_cards", "cardIds": ["c_file", "c_code"], "ordered": True},
    ])
    adapter = _adapter(payload)
    done = await u.run_understanding(
        db_conn, accepted["id"], adapter_provider=_provider(adapter)
    )

    assert done["status"] == "understood"
    assert done["validation"]["ok"] is True
    assert done["stage"] == "persist"
    assert done["output"]["kind"] == "intent"
    assert len(intents_stub) == 1
    assert intents_stub[0]["understanding_id"] == accepted["id"]
    assert [i["intentKey"] for i in intents_stub[0]["payloads"]] == ["compare-materials"]
    ops = intents_stub[0]["payloads"][0]["operations"]
    assert [op["op"] for op in ops] == ["add_reply_card", "group_cards"]

    ctx = _request_context(adapter.requests[0])
    assert len(adapter.requests) == 1
    assert "请比较这两份材料并给出差异" in ctx["requirement"]["text"]
    assert ctx["requirement"]["expressionSource"] == "submission"
    assert ctx["requirement"]["note"] == "请特别关注成本一项"
    assert ctx["honesty"]["expressionsSource"] == "submission"
    assert set(ctx["visibleCardIds"]) == {"c_file", "c_code", "c_note"}
    # 已许可的文字卡（含那条获准要求本身）都是材料：文字卡是唯一的字符内容材料通道
    assert {m["cardId"] for m in ctx["materials"]} == {"c_file", "c_code", "c_note"}
    assert any("甲材料正文" in str(m.get("text")) for m in ctx["materials"])
    assert any("乙材料正文" in str(m.get("text")) for m in ctx["materials"])


# --- 2) 不是固定模板：换材料 / 换要求 → 上下文与产出都变 --------------------


async def test_context_and_output_change_with_submission(db_conn, fake_materials, intents_stub):
    first = await _save_and_submit(
        db_conn,
        [
            _card("c_a1", "file", "第一版材料：预算 10 万", meta={"name": "预算.txt"}),
            _card("c_a2", "file", "第一版材料：工期 3 个月", meta={"name": "工期.txt"}),
            _card("c_a3", "text", "请比较这两份材料的风险", checked=True),
        ],
    )
    first_accepted = _accept(db_conn, first)
    first_adapter = _adapter(
        _intent_payload(
            [{"op": "add_reply_card", "content": "风险对比", "title": "风险"}],
            ref="c_a1",
        )
    )
    first_done = await u.run_understanding(
        db_conn, first_accepted["id"], adapter_provider=_provider(first_adapter)
    )
    assert first_done["status"] == "understood"

    second = await _save_and_submit(
        db_conn,
        [
            # 2026-10-12 起：链接与代码没有默认可见通道，两份材料改用文字卡（整卡许可）
            _card("c_b1", "text", "def check(): return True", checked=True),
            _card("c_b2", "text", "规格说明：https://example.invalid/spec", checked=True),
            _card("c_b3", "text", "这两份内容不需要现在处理，先放着", checked=True),
        ],
    )
    second_accepted = _accept(db_conn, second)
    second_adapter = _adapter({"kind": "no_action", "summary": "用户明确说先放着，不需要开展任务"})
    second_done = await u.run_understanding(
        db_conn, second_accepted["id"], adapter_provider=_provider(second_adapter)
    )

    assert second_done["status"] == "no_action"
    assert not intents_stub or len(intents_stub) == 1  # 只有第一次落了意图
    first_ctx = _request_context(first_adapter.requests[0])
    second_ctx = _request_context(second_adapter.requests[0])
    assert set(first_ctx["visibleCardIds"]) == {"c_a1", "c_a2", "c_a3"}
    assert set(second_ctx["visibleCardIds"]) == {"c_b1", "c_b2", "c_b3"}
    assert "请比较这两份材料的风险" in first_ctx["requirement"]["text"]
    assert "先放着" in second_ctx["requirement"]["text"]
    assert {m["cardId"] for m in first_ctx["materials"]} == {"c_a1", "c_a2", "c_a3"}
    assert {m["cardId"] for m in second_ctx["materials"]} == {"c_b1", "c_b2", "c_b3"}
    assert first_ctx["materials"] != second_ctx["materials"]
    # 2026-10-12 起：链接与代码没有默认可见通道，材料只有 inline（文字卡）这一种字符来源
    assert {m["source"] for m in second_ctx["materials"]} == {"inline"}
    # 上下文不同，产出也必须不同：不是套模板
    assert first_done["output"]["kind"] == "intent"
    assert second_done["output"]["kind"] == "no_action"


# --- 3) 未勾选注释 / hidden 对象 / 间接链接不进上下文 -----------------------


async def test_invisible_objects_never_reach_context(db_conn, fake_materials, intents_stub):
    cards = [
        _card("c_ok", "file", "可见材料正文", meta={"name": "可见.txt"}),
        _card("c_note", "text", "已勾选的要求：整理可见材料", checked=True),
        _card("c_off", "text", NOWHERE, checked=False),
        _card("c_hide", "file", NOWHERE + "隐藏", hidden=True, meta={"name": "隐藏.txt"}),
        _card("c_gone", "file", NOWHERE + "删除", deleted=True, meta={"name": "删除.txt"}),
    ]
    groups = [
        _group("g_ok", "可见组", ["c_ok", "c_off"]),
        _group("g_off", "只有未勾选的组", ["c_off"]),
    ]
    links = [
        _link("l_indirect", "c_ok", "c_off", meaning=NOWHERE),
        _link("l_hidden", "c_ok", "c_hide", meaning=NOWHERE),
    ]
    result = await _save_and_submit(
        db_conn, cards, groups=groups, links=links, selection=["c_ok", "c_off"]
    )
    accepted = _accept(db_conn, result)
    context = u.build_context(db_conn, board_id=BOARD, understanding_id=accepted["id"])
    rendered = json.dumps(context, ensure_ascii=False)

    assert context["visibleCardIds"] == ["c_ok", "c_note"]
    for hidden_id in ("c_off", "c_hide", "c_gone"):
        assert hidden_id not in rendered
    assert NOWHERE not in rendered
    assert "隐藏" not in rendered
    assert [g["id"] for g in context["groups"]] == ["g_ok"]
    assert context["groups"][0]["members"] == ["c_ok"]
    assert context["relationships"] == []
    assert context["focus"]["cardIds"] == ["c_ok"]


# --- 4) clarification / no_action 都不创建意图 -----------------------------


async def test_clarification_and_no_action_create_no_intents(
    db_conn, fake_materials, intents_stub
):
    result = await _save_and_submit(
        db_conn,
        [
            _card("c_file", "file", "只有一份材料", meta={"name": "材料.txt"}),
            _card("c_note", "text", "看一下这个", checked=True),
        ],
    )
    accepted = _accept(db_conn, result)

    clarify_adapter = _adapter({
        "kind": "clarification",
        "summary": "不知道要对这份材料做什么",
        "clarification": {
            "question": "你想让我对这份材料做什么？",
            "missing": ["具体动作", "期望结果"],
        },
    })
    clarify_done = await u.run_understanding(
        db_conn, accepted["id"], adapter_provider=_provider(clarify_adapter)
    )
    assert clarify_done["status"] == "needs_clarification"
    assert clarify_done["output"]["clarification"]["question"].startswith("你想让我")
    assert clarify_done["output"]["intentCreation"]["skipped"] is True
    assert intents_stub == []

    again = await u.run_understanding(
        db_conn, accepted["id"], adapter_provider=_provider(_adapter({"kind": "no_action", "summary": "x"}))
    )
    assert again["status"] == "needs_clarification"
    assert again.get("skipped") is True

    second = await _save_and_submit(
        db_conn, [_card("c_only", "file", "仅添加材料", meta={"name": "another.txt"})]
    )
    second_accepted = _accept(db_conn, second)
    no_action_adapter = _adapter({
        "kind": "no_action",
        "summary": "只是添加材料，当前无需开展任务",
    })
    no_action_done = await u.run_understanding(
        db_conn, second_accepted["id"], adapter_provider=_provider(no_action_adapter)
    )
    assert no_action_done["status"] == "no_action"
    assert no_action_done["output"]["intentCreation"]["skipped"] is True
    assert intents_stub == []

# --- 5) 无效输出不落成任务；自动修复只调用一次 -----------------------------


async def _board_with_scope(conn) -> dict:
    cards = [
        _card("c_file", "file", "甲材料正文", meta={"name": "甲.txt"}),
        # 可见但**没有**材料条目的卡（模拟材料解析未覆盖它）：验证「材料 id 不在解析结果里」
        _card(
            "c_nomat",
            "text",
            "这张卡没有对应的材料条目",
            checked=True,
            meta={"materialUnavailable": True},
        ),
        _card("c_note", "text", "请处理这两份材料", checked=True),
        _card("c_hidden", "file", NOWHERE, hidden=True, meta={"name": "隐藏.txt"}),
    ]
    return await _save_and_submit(conn, cards)


INVALID_CASES = [
    (
        "越权操作",
        _intent_payload([{"op": "delete_card", "cardId": "c_file"}]),
        "不允许的操作",
    ),
    (
        "引用范围外对象",
        _intent_payload([{"op": "group_cards", "cardIds": ["c_file", "c_hidden"]}]),
        "不在本次可见范围",
    ),
    (
        "材料 id 不在解析结果里",
        _intent_payload(
            [{"op": "add_reply_card", "content": "结论"}],
            basis=[{"cardId": "c_nomat", "role": "material", "version": "v1"}],
        ),
        "不在本次解析出的材料里",
    ),
    (
        "结构损坏",
        "这是一段普通话，不是 JSON 对象。",
        "没有 JSON 对象",
    ),
]


@pytest.mark.parametrize("name,payload,fragment", INVALID_CASES)
async def test_invalid_output_never_becomes_tasks(
    db_conn, fake_materials, intents_stub, name, payload, fragment
):
    result = await _board_with_scope(db_conn)
    accepted = _accept(db_conn, result)
    adapter = _adapter(payload, payload)  # 修复调用同样给无效输出
    done = await u.run_understanding(
        db_conn, accepted["id"], adapter_provider=_provider(adapter)
    )

    assert done["status"] == "failed", name
    assert done["validation"]["ok"] is False
    assert done["validation"]["reasons"], name
    assert fragment in "；".join(done["validation"]["reasons"]), name
    assert done["error"] and len(done["error"]) > 0, name
    assert intents_stub == [], name
    # 自动修复**只调用一次**：一共两次模型调用，不会无限重试
    assert len(adapter.requests) == 2, name
    assert done["finishedAt"] is not None


async def test_repair_runs_at_most_once_and_can_succeed(
    db_conn, fake_materials, intents_stub
):
    result = await _board_with_scope(db_conn)
    accepted = _accept(db_conn, result)
    invalid = _intent_payload([{"op": "move_card", "cardId": "c_file", "x": "左边", "y": 10}])
    valid = _intent_payload([{"op": "add_reply_card", "content": "结论", "title": "结论"}])
    adapter = _adapter(invalid, valid)
    done = await u.run_understanding(
        db_conn, accepted["id"], adapter_provider=_provider(adapter)
    )

    assert done["status"] == "understood"
    assert done["validation"]["attempts"] == 2
    assert len(adapter.requests) == 2
    assert len(intents_stub) == 1
    repair_messages = adapter.requests[1]["messages"]
    assert any("没有通过程序校验" in (m.content or "") for m in repair_messages)


async def test_missing_adapter_settles_failed_without_raising(db_conn, fake_materials, intents_stub):
    result = await _board_with_scope(db_conn)
    accepted = _accept(db_conn, result)

    async def _none():
        return None

    done = await u.run_understanding(db_conn, accepted["id"], adapter_provider=_none)
    assert done["status"] == "failed"
    assert "没有可用的模型适配器" in done["error"]
    assert done["validation"]["reasons"]

    async def _boom():
        raise RuntimeError("凭据读取失败：key=sk-should-be-redacted-1234567890")

    failed = await u.run_understanding(db_conn, accepted["id"], adapter_provider=_boom)
    assert failed["status"] == "failed"
    assert failed["validation"]["reasons"]
    assert "sk-should-be-redacted-1234567890" not in json.dumps(failed, ensure_ascii=False)
    assert intents_stub == []


# --- 6) 受理幂等；重试只从冻结快照恢复 -------------------------------------


async def test_request_idempotent_and_retry_uses_frozen_snapshot(
    db_conn, fake_materials, intents_stub
):
    result = await _save_and_submit(
        db_conn,
        [
            _card("c_old", "file", "第一版材料：预算 10 万", meta={"name": "旧.txt"}),
            _card("c_note", "text", "请总结第一版材料", checked=True),
        ],
    )
    submission_id = result["submission"]["id"]
    # 提交路径已经受理过这一次提交：同 request_id 再受理两次都必须复用同一条
    established = result["delivery"]["understandingId"]
    first = u.request_understanding(
        db_conn, board_id=BOARD, submission_id=submission_id, snapshot=result["after"]
    )
    second = u.request_understanding(
        db_conn, board_id=BOARD, submission_id=submission_id, snapshot=result["after"]
    )
    assert established
    assert first["id"] == established
    assert first["reused"] is True
    assert second["reused"] is True
    assert second["id"] == first["id"]
    rows = db_conn.execute("SELECT COUNT(*) AS n FROM board_understanding").fetchone()
    assert int(rows["n"]) == 1

    # 第一次理解失败（模型报错）→ 只从保存的快照重试
    broken = FakeStreamAdapter([StreamScript(error=RuntimeError("provider down"))])
    failed = await u.run_understanding(
        db_conn, first["id"], adapter_provider=_provider(broken)
    )
    assert failed["status"] == "failed"
    assert "provider down" in failed["error"]

    # 把**当前板面**改掉：重试必须仍然用旧快照，读不到新内容
    board_store.save_board(
        db_conn,
        BOARD,
        {
            "boardId": BOARD,
            "cards": [
                _card("c_new", "file", "第二版材料：预算 99 万", meta={"name": "新.txt"}),
                _card("c_new_note", "text", "看第二版", checked=True),
            ],
            "groups": [],
            "links": [],
            "selection": [],
        },
        reason="board changed after submit",
    )

    payload = _intent_payload(
        [{"op": "add_reply_card", "content": "第一版材料的总结", "title": "总结"}],
        ref="c_old",
    )
    adapter = _adapter(payload)
    done = await u.run_understanding(
        db_conn, first["id"], adapter_provider=_provider(adapter)
    )

    assert done["status"] == "understood"
    assert done["attempt"] == 2
    ctx = _request_context(adapter.requests[0])
    rendered = json.dumps(ctx, ensure_ascii=False)
    assert "第一版材料" in rendered
    assert "第二版材料" not in rendered
    assert "c_new" not in rendered
    assert set(ctx["visibleCardIds"]) == {"c_old", "c_note"}

    rebuilt = u.build_context(db_conn, board_id=BOARD, understanding_id=first["id"])
    assert set(rebuilt["visibleCardIds"]) == {"c_old", "c_note"}
    assert "第二版材料" not in json.dumps(rebuilt, ensure_ascii=False)


# --- 7) 材料口径诚实：未读取 / 截断必须如实记录 -----------------------------


async def test_material_honesty_records_truncation_and_unread(db_conn, fake_materials):
    long_text = "正文段落。" * 2000
    result = await _save_and_submit(
        db_conn,
        [
            # 2026-10-12 起：长正文用**文字卡**（整卡许可）；图片是「可见但没有读到内容」的诚实来源
            # （现在是**文件卡里的图片附件**：文件卡 + meta.file 的图片名，没有视觉能力 → image_unsupported）；
            # 旧 url 卡留在板面上，但没有默认可见通道，不会进材料。
            _card("c_long", "text", long_text, checked=True, meta={"name": "长文本"}),
            _card(
                "c_img",
                "file",
                "",
                meta={"name": "示意图.png", "file": {"name": "示意图.png", "attachmentId": "att_x"}},
            ),
            _card("c_url", "url", "https://example.invalid/page", meta={"title": "网页地址"}),
            _card("c_note", "text", "请整理这些材料", checked=True),
        ],
    )
    accepted = _accept(db_conn, result)
    context = u.build_context(db_conn, board_id=BOARD, understanding_id=accepted["id"])

    assert context["limits"]["truncated"] is True
    assert context["limits"]["unreadRanges"]
    material_ids = {m["cardId"] for m in context["materials"]}
    assert "c_url" in material_ids, "旧 url 卡迁移成文字卡后随整卡许可一起交付（契约 §2.1）"
    migrated_url = [m for m in context["materials"] if m["cardId"] == "c_url"][0]
    assert "https://example.invalid/page" in migrated_url["text"], "链接地址在整卡材料里"
    truncated = [m for m in context["materials"] if m["cardId"] == "c_long"][0]
    assert truncated["truncated"] is True
    assert len(truncated["text"]) <= u.MAX_MATERIAL_CHARS
    assert context["honesty"]["complete"] is False
    assert context["honesty"]["unreadSources"]["image_unsupported"] == 1
    assert "未" in context["honesty"]["claim"]
    notes = "；".join(context["honesty"]["notes"])
    assert "没有视觉能力" in notes
    assert "其余未读取" in notes
    assert context["limits"]["usedChars"] <= u.MAX_CONTEXT_CHARS

    done = await u.run_understanding(
        db_conn,
        accepted["id"],
        adapter_provider=_provider(_adapter({"kind": "no_action", "summary": "先不动"})),
    )
    assert done["status"] == "no_action"
    # 诚实口径也随上下文一起进模型：模型不能假装读过没读到的东西
    assert done["hasContext"] is True


async def test_memory_only_records_cooperation_relevant_entries(db_conn, fake_materials, intents_stub):
    result = await _save_and_submit(
        db_conn,
        [
            _card("c_m", "file", "材料甲", meta={"name": "甲.txt"}),
            _card("c_note", "text", "请比较材料并给出结论", checked=True),
        ],
    )
    accepted = _accept(db_conn, result)
    payload = _intent_payload(
        [{"op": "add_reply_card", "content": "结论", "title": "结论"}], ref="c_m"
    )
    payload["memory"] = [
        {"text": "用户倾向用对比得出结论", "kind": "conclusion"},
        {"text": json.dumps({"cards": [{"id": "c_m"}]}, ensure_ascii=False), "kind": "basis"},
    ]
    done = await u.run_understanding(
        db_conn, accepted["id"], adapter_provider=_provider(_adapter(payload))
    )
    assert done["status"] == "understood"
    memory = done["memory"]
    kinds = {record.get("provenance") for record in memory}
    assert "user_statement" in kinds
    assert "pending_suggestion" in kinds
    dumped = "；".join(str(record.get("reason") or "") for record in memory)
    assert "整块板面" in dumped
    for record in memory:
        if record.get("stored"):
            assert record["state"] in ("pending_review", "active")
    # 未获准的内容不会被自动激活
    assert all(record.get("state") != "active" for record in memory if record.get("stored"))
