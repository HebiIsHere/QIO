"""阶段协议（plan §1.2~§1.4）：白名单解析、状态机、落库与广播。

全部用 fake provider / 内存 SQLite，不联网、不需要真实密钥。
覆盖的规则：

* 解析白名单（op / name / 长度 / 脱敏）；
* 不自动开阶段 + 系统兜底隐式阶段；
* start 不新开、next 结束再开、update / 非法 / 空文本不改变阶段集合；
* stage_id 与 index 由系统生成，模型不能伪造；
* 落库形状 raw.stage 与 STAGE 事件载荷（先落库、再广播）；
* turn 收尾时阶段状态回写 done 并发 op=end。
"""

from __future__ import annotations

import json

from agent.core.narrative import parse_narrative
from agent.core.stage import (
    MAX_STAGE_NAME,
    StageOp,
    StageTracker,
    parse_stage,
    stage_event_payload,
    stage_id,
    stage_raw,
)
from agent.core.turn import short_turn_id


def _narrative(text: str = "正在读取仓库结构", kind: str = "progress", **extra):
    data = {"kind": kind, "text": text}
    data.update(extra)
    out = parse_narrative(data)
    assert out is not None
    return out


# ---- 标识与解析 -------------------------------------------------------------


def test_stage_id_is_system_generated():
    assert short_turn_id("turn_ab12cd34ef56") == "ab12cd34"
    assert short_turn_id(None) == "local"
    assert stage_id("turn_ab12cd34ef56", 3) == "st_ab12cd34_3"


def test_parse_stage_whitelist():
    assert parse_stage({"op": "start", "name": "读取仓库结构"}) == StageOp(
        op="start", name="读取仓库结构"
    )
    assert parse_stage({"op": "next"}) == StageOp(op="next", name="")
    assert parse_stage({"op": "update", "name": None}) == StageOp(op="update", name="")
    # 非法 op / 不是 dict / 缺 op：一律没有阶段操作（安全降级）
    for bad in ({"op": "end"}, {"op": "reset"}, {"op": 1}, {}, "start", None, []):
        assert parse_stage(bad) is None


def test_parse_stage_truncates_and_redacts_name():
    long = "阶" * 100
    out = parse_stage({"op": "start", "name": long})
    assert out is not None and len(out.name) == MAX_STAGE_NAME
    secret = parse_stage({"op": "start", "name": "api_key=sk-abcdef123456"})
    assert secret is not None and "sk-abcdef123456" not in secret.name


def test_narrative_carries_raw_stage_envelope():
    narrative = _narrative(stage={"op": "next", "name": "第二阶段"})
    assert narrative.stage == {"op": "next", "name": "第二阶段"}
    assert parse_stage(narrative.stage) == StageOp(op="next", name="第二阶段")
    # 形状不对：原样留成 None，解析层给出「没有阶段操作」
    assert _narrative(stage="next").stage is None
    assert parse_stage(_narrative(stage={"op": "boom"}).stage) is None


# ---- 状态机 -----------------------------------------------------------------


def test_first_narrative_without_stage_op_opens_implicit_stage():
    tracker = StageTracker("turn_ab12cd34ef56")
    first = tracker.observe(_narrative("我先看仓库结构"), None)
    assert first.action == "open" and first.op == "start"
    assert first.stage is not None
    assert first.stage.stage_id == "st_ab12cd34_1" and first.stage.index == 1
    assert first.stage.name == "我先看仓库结构" and first.stage.status == "running"
    # 第二条说明不新开阶段，只更新当前说明
    second = tracker.observe(_narrative("正在读 src"), None)
    assert second.action == "update" and second.op == "update"
    assert second.stage is not None and second.stage.stage_id == first.stage.stage_id
    assert tracker.current is not None and tracker.current.index == 1


def test_implicit_stage_name_is_truncated():
    tracker = StageTracker("turn_ab12cd34ef56")
    out = tracker.observe(_narrative("正" * 90), None)
    assert out.stage is not None and len(out.stage.name) == MAX_STAGE_NAME


def test_start_does_not_open_second_stage():
    tracker = StageTracker("turn_ab12cd34ef56")
    first = tracker.observe(_narrative("先看结构"), parse_stage({"op": "start", "name": "读取仓库"}))
    assert first.action == "open" and first.stage is not None
    assert first.stage.name == "读取仓库"
    again = tracker.observe(
        _narrative("换个说法"), parse_stage({"op": "start", "name": "另外一个名字"})
    )
    # 已有阶段时不新开（等价 update）：标识不变、序号不变、名字不变
    assert again.action == "update"
    assert again.stage is not None
    assert again.stage.stage_id == first.stage.stage_id
    assert again.stage.index == 1 and again.stage.name == "读取仓库"
    assert tracker.current is not None and tracker.current.index == 1


def test_next_closes_previous_and_opens_next():
    tracker = StageTracker("turn_ab12cd34ef56")
    first = tracker.observe(_narrative("第一步"), None)
    nxt = tracker.observe(_narrative("第二步开始"), parse_stage({"op": "next", "name": "写入修改"}))
    assert nxt.action == "open" and nxt.op == "next"
    assert nxt.previous is not None and first.stage is not None
    assert nxt.previous.stage_id == first.stage.stage_id
    assert nxt.stage is not None and nxt.stage.index == 2
    assert nxt.stage.stage_id == "st_ab12cd34_2"
    assert nxt.stage.name == "写入修改"


def test_next_without_name_uses_text():
    tracker = StageTracker("turn_ab12cd34ef56")
    tracker.observe(_narrative("第一步"), None)
    by_text = tracker.observe(_narrative("用这段说明当名字"), parse_stage({"op": "next"}))
    assert by_text.stage is not None and by_text.stage.name == "用这段说明当名字"
    # 文本为空（只有 explanation）：不改变阶段集合 —— 不会产生一个空名字的阶段
    silent = parse_narrative({"explanation": "只有一个说明"})
    assert silent is not None
    by_silent = tracker.observe(silent, parse_stage({"op": "next"}))
    assert by_silent.action == "none"
    assert tracker.current is not None and tracker.current.index == 2


def test_update_without_stage_does_nothing():
    tracker = StageTracker("turn_ab12cd34ef56")
    out = tracker.observe(_narrative("我在做事"), parse_stage({"op": "update", "name": "x"}))
    assert out.action == "none" and out.stage is None
    assert tracker.current is None  # 不自动开阶段


def test_empty_text_never_changes_the_stage_set():
    tracker = StageTracker("turn_ab12cd34ef56")
    silent = parse_narrative({"explanation": "只是解释，不产生说明行"})
    assert silent is not None
    assert tracker.observe(silent, parse_stage({"op": "start", "name": "阶段"})).action == "none"
    assert tracker.current is None


def test_model_cannot_forge_stage_id_index_or_status():
    tracker = StageTracker("turn_ab12cd34ef56")
    forged = _narrative(
        "我在做事",
        stage={"op": "start", "name": "伪装", "stage_id": "st_forged_9", "index": 99, "status": "done"},
    )
    out = tracker.observe(forged, parse_stage(forged.stage))
    assert out.stage is not None
    assert out.stage.stage_id == "st_ab12cd34_1"  # 系统生成，忽略模型给的
    assert out.stage.index == 1
    assert out.stage.status == "running"
    raw = stage_raw(out.stage, out.op)
    assert set(raw) == {"stage_id", "index", "name", "op", "status"}


def test_close_marks_done_and_payload_matches_contract():
    tracker = StageTracker("turn_ab12cd34ef56")
    opened = tracker.observe(_narrative("第一步"), None)
    assert opened.stage is not None
    closed = tracker.close()
    assert closed is not None and closed.op == "end" and closed.action == "end"
    assert closed.stage is not None and closed.stage.status == "done"
    assert closed.stage.stage_id == opened.stage.stage_id
    assert tracker.close() is None  # 幂等：没有阶段就没有事件

    payload = stage_event_payload(
        "turn_ab12cd34ef56",
        closed,
        narrative_id="msg_1",
        call_id="call_1",
        call_ids=["call_1"],
        created_at="2026-10-06T08:00:00+00:00",
    )
    assert set(payload) == {
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
    }
    assert payload["stage_id"] == opened.stage.stage_id
    assert payload["status"] == "done" and payload["op"] == "end"
    assert payload["narrative_id"] == "msg_1" and payload["call_ids"] == ["call_1"]


# ---- 服务层：先落库、再广播 -------------------------------------------------


def _app_ctx(tmp_path):
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "stage.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    topic = ctx.topics.nodes.create_topic("阶段协议").id
    fragment = ctx.fragments.get_or_create_open(topic).id
    ctx.bindings.record_binding("turn_1", topic, fragment_id=fragment)
    return ctx


def _stage_events(ctx) -> list[dict]:
    return [e.data for e in ctx.bus._history if e.type.value == "STAGE"]


def _narrative_rows(ctx) -> list[dict]:
    rows = ctx.conn.execute(
        "SELECT id, content, raw FROM messages WHERE content_type = 'narrative' ORDER BY rowid"
    ).fetchall()
    out = []
    for row in rows:
        out.append({"id": row["id"], "content": row["content"], "raw": json.loads(row["raw"] or "{}")})
    return out


async def test_stage_is_persisted_then_broadcast(tmp_path):
    from agent.adapters.base import ToolCall

    ctx = _app_ctx(tmp_path)
    call = ToolCall(id="c1", name="echo", arguments={})
    narrative = _narrative("正在读取仓库结构", stage={"op": "start", "name": "读取仓库结构"})
    message_id = await ctx._on_narrative("turn_1", narrative, call, ["c1"])
    assert message_id

    rows = _narrative_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["content"] == "正在读取仓库结构"
    assert rows[0]["raw"]["stage"] == {
        "stage_id": "st_1_1",
        "index": 1,
        "name": "读取仓库结构",
        "op": "start",
        "status": "running",
    }
    events = _stage_events(ctx)
    assert len(events) == 1
    assert events[0]["narrative_id"] == message_id
    assert events[0]["stage_id"] == "st_1_1" and events[0]["index"] == 1
    assert events[0]["op"] == "start" and events[0]["status"] == "running"
    assert events[0]["text"] == "正在读取仓库结构" and events[0]["call_id"] == "c1"


async def test_next_closes_previous_rows_and_opens_new_stage(tmp_path):
    from agent.adapters.base import ToolCall

    ctx = _app_ctx(tmp_path)
    call = ToolCall(id="c1", name="echo", arguments={})
    await ctx._on_narrative("turn_1", _narrative("第一步"), call, ["c1"])
    second = _narrative("第二步", stage={"op": "next", "name": "写入修改"})
    await ctx._on_narrative("turn_1", second, call, ["c2"])

    rows = _narrative_rows(ctx)
    assert [r["raw"]["stage"]["op"] for r in rows] == ["start", "next"]
    assert [r["raw"]["stage"]["status"] for r in rows] == ["done", "running"]
    assert [r["raw"]["stage"]["index"] for r in rows] == [1, 2]
    events = _stage_events(ctx)
    assert [e["op"] for e in events] == ["start", "next"]
    assert "previous" not in events[1]  # 事件只按契约带阶段事实，不带内部字段
    assert events[1]["stage_id"] == "st_1_2" and events[1]["status"] == "running"


async def test_turn_end_closes_open_stage_before_turn_end(tmp_path):
    from agent.adapters.base import ToolCall

    ctx = _app_ctx(tmp_path)
    await ctx._on_narrative("turn_1", _narrative("第一步"), ToolCall(id="c1", name="echo", arguments={}), ["c1"])
    await ctx._publish_turn_event("TURN_END", {"turn_id": "turn_1", "status": "completed"})

    types = [e.type.value for e in ctx.bus._history]
    assert types[-2:] == ["STAGE", "TURN_END"]  # 阶段边界必须先于整轮终态
    end_event = _stage_events(ctx)[-1]
    assert end_event["op"] == "end" and end_event["status"] == "done"
    assert end_event["text"] == ""  # 纯阶段边界，不带说明
    rows = _narrative_rows(ctx)
    assert rows[0]["raw"]["stage"]["status"] == "done"
    # 这一轮已经收尾：tracker 被取走，重复收尾不会重复发事件
    await ctx._publish_turn_event("TURN_END", {"turn_id": "turn_1", "status": "completed"})
    assert len(_stage_events(ctx)) == 2


async def test_turn_end_without_stage_publishes_nothing_extra(tmp_path):
    ctx = _app_ctx(tmp_path)
    await ctx._publish_turn_event("TURN_END", {"turn_id": "turn_2", "status": "completed"})
    assert _stage_events(ctx) == []
    assert [e.type.value for e in ctx.bus._history] == ["TURN_END"]


async def test_current_stage_id_is_null_without_stage(tmp_path):
    from agent.adapters.base import ToolCall

    ctx = _app_ctx(tmp_path)
    assert ctx.current_stage_id("turn_1") is None
    await ctx._on_narrative("turn_1", _narrative("第一步"), ToolCall(id="c1", name="echo", arguments={}), ["c1"])
    assert ctx.current_stage_id("turn_1") == "st_1_1"
    # 子任务不是主 Turn：没有阶段归属
    assert ctx.current_stage_id("subagent:task_x") is None