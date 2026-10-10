"""D 独立验证：统一过程 / 阶段协议（契约 §1）。

契约来源：docs/plans/2026-10-06-unified-process-attachments-streaming.md §1。
验证方只依赖契约里写死的东西，不读实现方结论、不 import 实现方内部辅助函数
（除了契约 §5 明列的 core/stage.py 这个模块名）：

* 事件名 STAGE + 字段名（turn_id / stage_id / index / status / name / text / kind /
  op / narrative_id / call_id / call_ids / created_at）；
* stage_id 由系统生成、index 从 1 起单调；工具归属只看 stage_id；
* op 语义：start / next / update / 缺失 / 非法；
* name <= 40、text <= 120，都过 narrative 白名单与脱敏；
* TOOL_START / TOOL_END 带可选 stage_id；
* 阶段文案不参与任何判定：不能结束整轮、不能改真实结果。

基线（ee6bbff）现状：EventType 里没有 STAGE、core/stage.py 不存在、TOOL_* 没有
stage_id —— 因此本文件在实现合并前应当是**红的**。红就是「现状不满足契约」的证据，
不是脚本错误。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_stage_protocol_verify.py -q
"""

from __future__ import annotations

import re

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

# 契约 §1.3 的字段表
STAGE_FIELDS = (
    "turn_id",
    "stage_id",
    "index",
    "status",
    "name",
    "text",
    "kind",
    "op",
    "narrative_id",
    "call_id",
    "call_ids",
    "created_at",
)
STAGE_OPS = {"start", "update", "next", "end"}


# ---- 夹具：真 AppContext + 假 adapter，跑完整一轮 --------------------------------


def _app_ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "stage-verify.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


class _PlanAdapter:
    """按脚本返回的假 adapter：不做任何网络调用，不需要真实 Key。"""

    mode = "native"
    model = "fake-stage-verify"

    def __init__(self, steps: list[Completion]) -> None:
        self.steps = list(steps)

    async def complete(self, messages, tools, **kwargs):
        if self.steps:
            return self.steps.pop(0)
        return _final("（脚本用尽）")


def _tool_step(call_id: str, narrative: dict | None, text: str = "hi") -> Completion:
    return Completion(
        message=ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="echo",
                    arguments={"text": text},
                    narrative=narrative,
                )
            ],
        )
    )


def _final(text: str) -> Completion:
    return Completion(message=ChatMessage(role="assistant", content=text))


async def _run(ctx: AppContext, topic: str, adapter: _PlanAdapter, message: str = "跑一轮"):
    async def _fake_build(*args, **kwargs):
        return adapter

    ctx.build_adapter = _fake_build  # type: ignore[method-assign]
    return await ctx.run_turn(message, topic_id=topic)


def _events(ctx: AppContext, type_name: str) -> list:
    return [event for event in ctx.bus._history if event.type.value == type_name]


def _stage_events(ctx: AppContext) -> list:
    return _events(ctx, "STAGE")


def _turn_id(ctx: AppContext) -> str:
    starts = _events(ctx, "TURN_START")
    assert starts, "这一轮没有 TURN_START"
    return str(starts[0].data["turn_id"])


# ---- 1. 事件注册（契约 §1.3） ---------------------------------------------------


def test_stage_event_type_is_registered_and_critical():
    from agent.api.bus import CRITICAL_EVENTS
    from agent.api.events import EventType, make_event, sse_format

    assert EventType.STAGE.value == "STAGE"
    assert EventType.STAGE in CRITICAL_EVENTS, "STAGE 必须进关键事件集合（丢了前端无法对齐阶段）"
    wire = sse_format(make_event(EventType.STAGE, {"stage_id": "st_x_1", "op": "start"}))
    assert "event: STAGE" in wire


def test_stage_module_exists():
    """契约 §5 把 core/stage.py 列为新增模块：阶段协议只能有一个事实源。"""
    import importlib

    importlib.import_module("agent.core.stage")


# ---- 2. op 语义：start / next（契约 §1.2 规则 2、3） ----------------------------


async def test_start_then_next_creates_two_ordered_stages(tmp_path):
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("阶段验证").id
    adapter = _PlanAdapter(
        [
            _tool_step(
                "c1",
                {
                    "kind": "progress",
                    "text": "先看仓库结构",
                    "stage": {"op": "start", "name": "读取仓库结构"},
                },
            ),
            _tool_step(
                "c2",
                {
                    "kind": "progress",
                    "text": "进入下一步",
                    "stage": {"op": "next", "name": "核对实现"},
                },
            ),
            _final("完成"),
        ]
    )
    await _run(ctx, topic, adapter)
    turn_id = _turn_id(ctx)

    stage_events = _stage_events(ctx)
    assert stage_events, "契约 §1.3：一轮里出现阶段就必须发 STAGE 事件"

    ops = [str(event.data.get("op") or "") for event in stage_events]
    assert "start" in ops, ops
    assert "next" in ops, f"op=next 必须开新阶段：{ops}"
    assert set(ops) <= STAGE_OPS, f"出现了契约白名单外的 op：{ops}"

    started = [e for e in stage_events if e.data.get("op") == "start"]
    nexted = [e for e in stage_events if e.data.get("op") == "next"]
    first_id = str(started[0].data["stage_id"])
    second_id = str(nexted[0].data["stage_id"])
    assert first_id.startswith("st_"), first_id
    assert second_id.startswith("st_"), second_id
    assert first_id != second_id, "op=next 必须换一个 stage_id"
    assert int(nexted[0].data["index"]) == int(started[0].data["index"]) + 1

    # index 从 1 起、单调不减
    indexes = [int(e.data["index"]) for e in stage_events]
    assert indexes[0] == 1, indexes
    assert indexes == sorted(indexes), indexes

    # 字段完整 + 类型正确（契约 §1.3）
    for event in stage_events:
        missing = [key for key in STAGE_FIELDS if key not in event.data]
        assert not missing, (missing, event.data)
        assert event.data["turn_id"] == turn_id
        assert int(event.data["index"]) >= 1
        assert event.data["status"] in {"running", "done"}, event.data
        assert isinstance(event.data["call_ids"], list)
        assert re.match(r"^st_.+_\d+$", str(event.data["stage_id"])), event.data["stage_id"]
        # 模型叙事带出来的阶段事件必须带时间戳（历史排序靠它）。
        # 系统收口事件（op=end，narrative_id=None）单独在下面那条用例里核对。
        if event.data.get("narrative_id"):
            assert str(event.data["created_at"] or ""), event.data

    # 工具归属只看 stage_id（契约 §1.1）
    by_call: dict[str, str | None] = {}
    for event in _events(ctx, "TOOL_START") + _events(ctx, "TOOL_END"):
        by_call[str(event.data.get("call_id"))] = event.data.get("stage_id")
    assert by_call.get("c1") == first_id, by_call
    assert by_call.get("c2") == second_id, by_call


async def test_stage_events_always_carry_created_at(tmp_path):
    """契约 §1.3 的字段表把 created_at 写成时间字符串：每个 STAGE 事件都该带。

    实测（2026-10-06，集成分支 8d52634）：一轮的收口事件
    {op: "end", status: "done", narrative_id: null, text: "", created_at: null}
    的 created_at 是 null。前端排序有兜底，所以影响小；但这是字段级偏差，如实记录。
    """
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("阶段时间戳").id
    adapter = _PlanAdapter(
        [
            _tool_step(
                "c1",
                {"kind": "progress", "text": "先看仓库结构", "stage": {"op": "start", "name": "读取仓库结构"}},
            ),
            _final("完成"),
        ]
    )
    await _run(ctx, topic, adapter)
    stage_events = _stage_events(ctx)
    assert stage_events, "有阶段就必须有 STAGE 事件"
    missing = [event.data for event in stage_events if not str(event.data.get("created_at") or "")]
    assert not missing, ("契约 §1.3：每个 STAGE 事件都要带 created_at", missing)


async def test_stage_start_does_not_open_second_stage(tmp_path):
    """契约 §1.2 规则 2：已有阶段时再给 op=start 等价于 update，不新开。"""
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("阶段不新开").id
    adapter = _PlanAdapter(
        [
            _tool_step(
                "c1",
                {"kind": "progress", "text": "第一步", "stage": {"op": "start", "name": "阶段甲"}},
            ),
            _tool_step(
                "c2",
                {"kind": "progress", "text": "还是第一步", "stage": {"op": "start", "name": "阶段乙"}},
            ),
            _final("完成"),
        ]
    )
    await _run(ctx, topic, adapter)
    stage_events = _stage_events(ctx)
    stage_ids = {str(e.data["stage_id"]) for e in stage_events}
    assert len(stage_ids) == 1, f"op=start 不得新开阶段：{[e.data for e in stage_events]}"


# ---- 3. 非法与缺失：只更新说明，不改变阶段集合（契约 §1.2 规则 1、4） -------------


async def test_illegal_stage_op_is_ignored(tmp_path):
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("非法 op").id
    adapter = _PlanAdapter(
        [
            _tool_step(
                "c1",
                {
                    "kind": "progress",
                    "text": "先说明一句",
                    "stage": {"op": "jump", "name": "伪造阶段"},
                },
            ),
            _final("完成"),
        ]
    )
    await _run(ctx, topic, adapter)
    stage_events = _stage_events(ctx)
    examined = [str(e.data.get("op") or "") for e in stage_events]
    assert stage_events, "有说明就必须有阶段落点（隐式阶段也算），否则过程区没有归属"
    assert "jump" not in examined, examined
    assert set(examined) <= STAGE_OPS, examined
    stage_ids = {str(e.data["stage_id"]) for e in stage_events}
    assert len(stage_ids) <= 1, f"非法 op 不得开阶段：{[e.data for e in stage_events]}"
    names = {str(e.data.get("name") or "") for e in stage_events}
    assert "伪造阶段" not in names, names


async def test_narrative_without_stage_gets_implicit_system_stage(tmp_path):
    """契约 §1.2 规则 1：没有阶段时的第一条说明建立系统兜底的隐式阶段。"""
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("隐式阶段").id
    adapter = _PlanAdapter(
        [
            _tool_step("c1", {"kind": "announce", "text": "我先看一下仓库结构再动手"}),
            _final("完成"),
        ]
    )
    await _run(ctx, topic, adapter)
    stage_events = _stage_events(ctx)
    assert stage_events, "有说明但没阶段时，系统必须兜底建立一个隐式阶段（否则过程区没有落点）"
    first = stage_events[0].data
    assert str(first.get("stage_id") or "").startswith("st_")
    assert str(first.get("name") or "").strip(), first
    # 关键事件：必须能落库（narrative_id 指向 messages 行）
    assert first.get("narrative_id"), first


# ---- 4. 长度与脱敏（契约 §1.2 规则 5） ------------------------------------------


async def test_stage_name_and_text_are_capped_and_redacted(tmp_path):
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("截断脱敏").id
    long_name = "阶段名" * 40
    long_text = "说明" * 200
    adapter = _PlanAdapter(
        [
            _tool_step(
                "c1",
                {
                    "kind": "progress",
                    "text": long_text,
                    "stage": {"op": "start", "name": long_name},
                },
            ),
            _tool_step(
                "c2",
                {"kind": "warning", "text": "api_key=sk-verify-stage-secret-0001"},
            ),
            _final("完成"),
        ]
    )
    await _run(ctx, topic, adapter)
    stage_events = _stage_events(ctx)
    assert stage_events, "阶段名/说明必须出现在 STAGE 事件里"
    for event in stage_events:
        assert len(str(event.data.get("name") or "")) <= 40, event.data
        assert len(str(event.data.get("text") or "")) <= 120, event.data
        assert "sk-verify-stage-secret-0001" not in str(event.data.get("text") or ""), event.data


# ---- 5. 模型不能伪造状态、不能结束整轮（契约 §1.2 规则 6、§1.3 末条） ------------


async def test_stage_text_cannot_end_turn_or_change_result(tmp_path):
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("伪造状态").id
    adapter = _PlanAdapter(
        [
            _tool_step(
                "c1",
                {
                    "kind": "result",
                    "text": "整轮已经完成",
                    "stage": {"op": "start", "name": "假装结束", "status": "done"},
                },
            ),
            # 契约 §1.1（第五轮）：角色由正文声明决定 —— 工具轮之后声明回答
            _final("[[QIO:ANSWER]]\n这是系统给出的真实最终回答。"),
        ]
    )
    await _run(ctx, topic, adapter)

    stage_events = _stage_events(ctx)
    assert stage_events, "阶段文案必须走 STAGE 事件（否则模型的话没有过程区落点）"
    ends = _events(ctx, "TURN_END")
    assert len(ends) == 1, f"一个 turn 只能有一个 TURN_END：{[e.data for e in ends]}"
    assert ends[0].data["status"] == "completed", ends[0].data
    assert "这是系统给出的真实最终回答。" in str(ends[0].data.get("final_content") or "")

    for event in stage_events:
        assert event.data.get("status") in {"running", "done"}, event.data
        assert event.data.get("status") != "hacked", event.data


# ---- 6. 持久化与历史回放（契约 §1.4） ------------------------------------------


async def test_stage_is_persisted_in_message_raw_and_replayable(tmp_path):
    import json

    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("阶段持久化").id
    adapter = _PlanAdapter(
        [
            _tool_step(
                "c1",
                {
                    "kind": "progress",
                    "text": "先看仓库结构",
                    "stage": {"op": "start", "name": "读取仓库结构"},
                },
            ),
            _final("完成"),
        ]
    )
    await _run(ctx, topic, adapter)
    turn_id = _turn_id(ctx)

    rows = ctx.conn.execute(
        "SELECT id, content, raw FROM messages WHERE content_type = 'narrative' AND turn_id = ?",
        (turn_id,),
    ).fetchall()
    assert rows, "阶段说明必须落库（content_type=narrative）"
    raws = [json.loads(row["raw"]) for row in rows]
    staged = [raw for raw in raws if isinstance(raw.get("stage"), dict)]
    assert staged, f"契约 §1.4：raw.stage 必须存在：{raws}"
    stage = staged[0]["stage"]
    for key in ("stage_id", "index", "name", "op", "status"):
        assert key in stage, (key, stage)
    assert str(stage["stage_id"]).startswith("st_")
    assert stage["op"] == "start"
    # 关联工具事实仍在同一行（历史回放要能还原「阶段内历次说明 + 关联工具记录」）
    assert isinstance(raws[0].get("calls"), list)

    view = ctx.active_turn_narratives(turn_id)
    assert view, "运行态视图必须能拿到这一轮的阶段说明"
    assert view[0]["text"] == "先看仓库结构"
