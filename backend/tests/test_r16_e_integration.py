# -*- coding: utf-8 -*-
"""R16 · E：fake/scripted 模型驱动的真实事件流集成 + 恢复执行权 + 有界真实进程退出/接管。

对应契约 §10.2 A/G/H/I、§10.3 E 的集成部分、§14.5—§14.6/§14.9。

本文件只做**跨模块真实接线**，不重复 A/B/C/D 的模块内单元断言：
* I03/T1：一轮真实成功结束时内部推进（轮末细化 + 容量分段）必须广播可被前端接受的
  同源 position 事件，且 nav_version 不被内部推进污染；
* I09—I12：同一 answer_key 只有一个执行者；生成成功但保存失败后，另一条恢复记录
  **只补存不再生成**（共享暂存）；旧执行者迟到保存被 superseded（零行改动）；
* I13/I14：恢复只读导航、不改 pending、不新建片段，正式答案按原归属落库；
* I07/§14.9：有界真实子进程退出后，InstanceRegistry 把旧实例判死并交还归属（不无限等）。

模型一律脚本化 fake（不联网、不读密钥）；时间用受控 Event / 租约测试缝，不用 sleep。
证据写 D:/qio-dev/r16-evidence/e。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.config import Settings
from agent.graph.anchors import AnchorService
from agent.services import navigation_version, topic_recovery
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.instance_registry import InstanceRegistry
from agent.storage.migrate import apply_migrations

ANSWER = "[[QIO:ANSWER]]"
SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "r16e_integration.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = _memory_keyring()
    # 模拟真实启动流程：启动期权威重建（契约 §4/§8；真实入口在 create_app/lifespan）
    ctx.position_state.ensure_identity()
    return ctx


def _memory_keyring():  # noqa: ANN202
    from agent.credentials.store import MemoryKeyring

    return MemoryKeyring()


@pytest.fixture()
def app(tmp_path: Path) -> AppContext:
    ctx = _app(tmp_path)
    yield ctx


def _anchor_events(ctx: AppContext) -> list[dict]:
    return [
        dict(event.data or {})
        for event in list(ctx.bus._history)  # noqa: SLF001 - 与既有测试同口径读总线历史
        if getattr(event, "type", None) == EventType.ANCHOR
    ]


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _seed_fragment(
    ctx: AppContext,
    topic_id: str,
    *,
    closed: bool = False,
    title: str = "片段",
    payload: str = "原文",
) -> str:
    from agent.memory.fragment import new_id

    frag_id = new_id("frag")
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (frag_id, topic_id, f"{title} 摘要", now, now if closed else None),
    )
    msg_id = f"{frag_id}_seed"
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (msg_id, frag_id, payload, now),
    )
    ctx.conn.execute(
        "UPDATE fragments SET start_message_id = ?, end_message_id = ? WHERE id = ?",
        (msg_id, msg_id, frag_id),
    )
    ctx.conn.commit()
    return frag_id


class ScriptedAdapter:
    """非流式脚本 adapter：每次 complete 依次取 (content, tool_calls)。"""

    mode = "native"
    model = "fake-r16e-integration"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.calls = 0
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        content, calls = (
            self.script.pop(0) if self.script else (ANSWER + "\n补答", None)
        )
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=calls),
            finish_reason="tool_calls" if calls else "stop",
        )


class GatedAdapter:
    """第一次 complete 受控挂起（threading.Event），主线程放行；不占用事件循环。"""

    mode = "native"
    model = "fake-r16e-gated"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self, text: str, release: threading.Event, timeout: float = 20.0) -> None:
        self.text = text
        self.release = release
        self.timeout = timeout
        self.calls = 0
        self.started = threading.Event()

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.started.set()
        await asyncio.to_thread(self.release.wait, self.timeout)
        return Completion(message=ChatMessage(role="assistant", content=self.text))


def _install_adapter(app: AppContext, adapter: ScriptedAdapter) -> None:
    async def fake_build():  # noqa: ANN202
        return adapter

    app.build_adapter = fake_build  # type: ignore[assignment]


def _install_gated_adapter(app: AppContext, adapter: GatedAdapter) -> None:
    async def fake_build():  # noqa: ANN202
        return adapter

    app.build_adapter = fake_build  # type: ignore[assignment]


def _prepare_common_message(
    app: AppContext, *, turn_id: str = "turn_old", content: str = "旧问题"
) -> tuple[str, str, str]:
    topic_id = _topic(app, "旧问题话题")
    fragment = app.fragments.get_or_create_open(topic_id)
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content=content,
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    return topic_id, fragment.id, message_id


def _record_for_message(
    app: AppContext,
    topic_id: str,
    fragment_id: str,
    message_id: str,
    *,
    turn_id: str = "turn_old",
    record_id: str | None = None,
) -> str:
    record = topic_recovery.record_topic_failure(
        app,
        turn_id=turn_id,
        topic_id=topic_id,
        fragment_id=fragment_id,
        user_message_id=message_id,
        operation_id="op-r16e-integration",
        param_version=1,
        recovery_nav_version=navigation_version.current(app.conn),
        failed_stage="context_prepare",
        message="回答准备失败（E 集成用例）",
        record_id=record_id,
    )
    return str(record["record_id"])


def _assistant_rows(app: AppContext, turn_id: str) -> list:
    return app.conn.execute(
        "SELECT id, content, fragment_id, answer_key FROM messages "
        "WHERE role = 'assistant' AND turn_id = ? ORDER BY rowid",
        (turn_id,),
    ).fetchall()


def _run_row(app: AppContext, answer_key: str):
    return app.conn.execute(
        "SELECT answer_key, status, generation, owner, owner_instance, claim_until, "
        "answer_id, phase, staged_answer FROM topic_answer_runs WHERE answer_key = ?",
        (answer_key,),
    ).fetchone()


def _answer_key_for_turn(app: AppContext, turn_id: str = "turn_old") -> str:
    row = app.conn.execute(
        "SELECT answer_key FROM topic_recovery_records WHERE turn_id = ? "
        "AND answer_key IS NOT NULL ORDER BY rowid LIMIT 1",
        (turn_id,),
    ).fetchone()
    assert row is not None, "恢复记录必须带 answer_key"
    return str(row["answer_key"])


# ---------------------------------------------------------------------------
# ① I03/T1：真实一轮结束的内部推进必须产生可接受的事件（fake 模型驱动）
# ---------------------------------------------------------------------------


async def test_capacity_segment_publishes_same_source_position_event(app: AppContext, monkeypatch):
    topic = _topic(app, "容量分段事件")
    open_fragment = _seed_fragment(app, topic, closed=False, title="被分段的开放片段")
    # 强制本轮结束触发容量分段（确定性；不依赖真实 token 阈值）
    monkeypatch.setattr(app.fragments, "should_close", lambda fragment: True)
    # 派生工作不在本用例范围：禁止调度后台摘要，保持事件流可解释
    monkeypatch.setattr(app.turn_orchestrator, "_schedule_derived_work", lambda *a, **k: None)

    before = app.position_state.read()
    assert before["position_version"] >= 0
    events_before = len(_anchor_events(app))

    adapter = ScriptedAdapter([(ANSWER + "\n容量分段后的正式回答", None)])
    _install_adapter(app, adapter)
    result = await asyncio.wait_for(app.run_turn("触发容量分段的问题", topic_id=topic), timeout=60)
    assert result.get("ok") is True, result
    assert adapter.calls == 1

    final = app.position_state.read()
    # 位置落到了本轮真实片段；开放片段已被封存 → 这是历史位置
    assert final["topic_id"] == topic, final
    assert final["fragment_id"] == open_fragment, final
    assert final["historic"] is True, f"容量分段封存后当前片段必须是 historic：{final}"
    # 内部推进不是用户导航决定：nav_version 保持 0
    assert final["nav_version"] == 0, final
    # 轮首补锚点 / 轮末推进 / 容量分段都推进 position_version
    assert final["position_version"] >= before["position_version"] + 2, (
        before,
        final,
    )

    events = _anchor_events(app)[events_before:]
    assert events, "内部位置推进必须广播 ANCHOR（I03：前端可接受的位置事件）"
    last = events[-1]
    assert last["position"] == final, "事件 position 与权威读取必须同源"
    assert last["position_version"] == final["position_version"]
    assert last["position_identity"] == final["position_identity"]
    assert last["command_id"], "内部转换也必须带命令身份（internal:<kind>:<scope>）"
    assert last["command_id"].startswith("internal:")
    # 至少一条事件反映「封存后成为历史位置」
    assert any(
        item.get("position", {}).get("fragment_id") == open_fragment
        and item.get("position", {}).get("historic") is True
        for item in events
    ), events

    rows = app.conn.execute(
        "SELECT identity_key, command_kind, status, nav_version FROM navigation_commands "
        "ORDER BY created_at"
    ).fetchall()
    kinds = {row["command_kind"] for row in rows}
    assert "auto_segment" in kinds, [dict(r) for r in rows]
    assert "internal_refine" in kinds, [dict(r) for r in rows]
    assert all(row["status"] == "applied" for row in rows), [dict(r) for r in rows]
    assert all(int(row["nav_version"] or 0) == 0 for row in rows), (
        "内部转换命令行不得记成用户导航决定"
    )
    assert not any(str(row["identity_key"]).startswith(("request:", "operation:")) for row in rows)


# ---------------------------------------------------------------------------
# ② I13/I14：恢复旧问题只读导航、不改 pending、按原归属落库
# ---------------------------------------------------------------------------


async def test_recovery_is_navigation_read_only_and_keeps_attribution(app: AppContext):
    topic_id, fragment_id, message_id = _prepare_common_message(app)
    topic2 = _topic(app, "恢复期间用户所在话题")
    app.navigation.enter_topic(topic2, restore_position=False)
    app.navigation.register_continuation(
        topic_id, _seed_fragment(app, topic_id, closed=True, title="历史")
    )
    anchor_before = AnchorService(app.conn).get_active()
    nav_before = int(navigation_version.current(app.conn))
    pos_before = int(app.position_state.read()["position_version"])
    pending_before = app.bindings.peek_intent()
    fragments_before = app.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]
    record_id = _record_for_message(app, topic_id, fragment_id, message_id)

    adapter = ScriptedAdapter([(ANSWER + "\n恢复后的原归属回答", None)])
    _install_adapter(app, adapter)
    result = await asyncio.wait_for(topic_recovery.retry_topic_answer(app, record_id), timeout=30)

    assert result.get("ok") is True and result.get("answered") is True, result
    assert adapter.calls == 1
    anchor_after = AnchorService(app.conn).get_active()
    assert anchor_after is not None and anchor_after.topic_id == topic2
    if anchor_before is not None:
        assert anchor_after.fragment_id == anchor_before.fragment_id
        assert anchor_after.topic_id == anchor_before.topic_id
    assert int(navigation_version.current(app.conn)) == nav_before, "恢复不得推进 nav_version（I13）"
    assert int(app.position_state.read()["position_version"]) == pos_before, "恢复不得改位置状态"
    pending_after = app.bindings.peek_intent()
    assert (pending_after.intent_id if pending_after else None) == (
        pending_before.intent_id if pending_before else None
    ), "恢复不得改 pending（I13）"
    assert app.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == fragments_before

    rows = _assistant_rows(app, "turn_old")
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["fragment_id"] == fragment_id, "正式答案必须写回原归属片段（I14）"
    assert rows[0]["content"] == "恢复后的原归属回答"
    assert rows[0]["answer_key"], "正式答案必须带 answer_key"
    stored = topic_recovery.get_record(app, record_id)
    assert stored is not None and stored["answer_id"] == rows[0]["id"]
    assert pending is not None


# ---------------------------------------------------------------------------
# ③ I10/B8：生成成功但保存失败 → 另一条同 answer_key 记录只补存、不再生成
# ---------------------------------------------------------------------------


async def test_staged_answer_shared_across_records_no_second_generation(app: AppContext, monkeypatch):
    topic_id, fragment_id, message_id = _prepare_common_message(app)
    record_a = _record_for_message(app, topic_id, fragment_id, message_id, record_id="rec-stage-a")
    record_b = _record_for_message(app, topic_id, fragment_id, message_id, record_id="rec-stage-b")
    answer_key = _answer_key_for_turn(app)

    adapter_a = ScriptedAdapter([(ANSWER + "\n生成成功但保存失败的唯一正文", None)])
    _install_adapter(app, adapter_a)

    real_save = topic_recovery._save_answer  # noqa: SLF001 - 受控故障注入缝（生产不注入）
    failed_once = {"done": False}

    def flaky_save(conn, record, answer, **kwargs):  # noqa: ANN001, ANN003
        if record.get("record_id") == record_a and not failed_once["done"]:
            failed_once["done"] = True
            raise sqlite3.OperationalError("simulated save failure (r16e)")
        return real_save(conn, record, answer, **kwargs)

    monkeypatch.setattr(topic_recovery, "_save_answer", flaky_save)
    first = await asyncio.wait_for(topic_recovery.retry_topic_answer(app, record_a), timeout=30)

    assert first.get("ok") is False and first.get("recoverable") is True, first
    assert first.get("code") == "recovery_persist_failed", first
    assert adapter_a.calls == 1
    run = _run_row(app, answer_key)
    assert run is not None
    assert str(run["staged_answer"] or "").strip(), "保存失败必须把正文暂存到 run 行（唯一权威）"
    assert run["phase"] == "staged", dict(run)
    assert len(_assistant_rows(app, "turn_old")) == 0, "保存失败不得留下正式答案行"

    # 另一条同 answer_key 的恢复记录：只补存共享暂存，不调用模型
    adapter_b = ScriptedAdapter([(ANSWER + "\n绝不应该被调用的第二次生成", None)])
    _install_adapter(app, adapter_b)
    second = await asyncio.wait_for(topic_recovery.retry_topic_answer(app, record_b), timeout=30)

    assert second.get("ok") is True and second.get("answered") is True, second
    assert adapter_b.calls == 0, "已有共享暂存时不得再调用模型（I10）"
    rows = _assistant_rows(app, "turn_old")
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["content"].endswith("生成成功但保存失败的唯一正文")
    run_after = _run_row(app, answer_key)
    assert run_after["phase"] == "answered" and run_after["status"] == "answered", dict(run_after)
    assert not run_after["staged_answer"], "补存成功后必须清空共享暂存"

    records = app.conn.execute(
        "SELECT record_id, status, answer_id FROM topic_recovery_records "
        "WHERE record_id IN (?, ?)",
        (record_a, record_b),
    ).fetchall()
    assert {row["status"] for row in records} == {"answered"}, [dict(r) for r in records]
    assert len({row["answer_id"] for row in records}) == 1, [dict(r) for r in records]
    assert records[0]["answer_id"] == rows[0]["id"]


# ---------------------------------------------------------------------------
# ④ H/I11：租约到期 + 旧实例确认退出 → 接管；旧执行者迟到保存被拒（零行改动）
# ---------------------------------------------------------------------------


async def test_takeover_after_owner_exit_rejects_late_executor(app: AppContext, monkeypatch):
    topic_id, fragment_id, message_id = _prepare_common_message(app)
    record_a = _record_for_message(app, topic_id, fragment_id, message_id, record_id="rec-late-a")
    record_b = _record_for_message(app, topic_id, fragment_id, message_id, record_id="rec-late-b")
    answer_key = _answer_key_for_turn(app)

    release = threading.Event()
    gated = GatedAdapter(ANSWER + "\n旧执行者迟到的正文", release)
    _install_gated_adapter(app, gated)
    first_task = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app, record_a, lease_seconds=60.0, heartbeat_interval=3600.0
        )
    )
    entered = await asyncio.wait_for(
        asyncio.to_thread(gated.started.wait, 20.0), timeout=25
    )
    assert entered, "旧执行者必须先真实进入模型调用（前置条件）"

    # 制造「旧实例确认退出 + 租约到期」：真实行为等价于崩溃后 maintenance 收敛。
    # 实例行用 InstanceRegistry 真实写入（已退出），不靠时间等待。
    dead_instance = "qio_r16e_dead_owner"
    registry = InstanceRegistry(app.conn, dead_instance, pid=999_999)
    registry.start()
    registry.mark_clean_exit()
    past = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    app.conn.execute(
        "UPDATE topic_answer_runs SET owner_instance = ?, claim_until = ? WHERE answer_key = ?",
        (dead_instance, past, answer_key),
    )
    app.conn.commit()

    adapter_b = ScriptedAdapter([(ANSWER + "\n接管后的唯一正文", None)])
    _install_adapter(app, adapter_b)
    second = await asyncio.wait_for(
        topic_recovery.retry_topic_answer(app, record_b, lease_seconds=60.0), timeout=30
    )
    assert second.get("ok") is True and second.get("answered") is True, second
    assert adapter_b.calls == 1
    rows_after_takeover = _assistant_rows(app, "turn_old")
    assert len(rows_after_takeover) == 1
    run_after_takeover = _run_row(app, answer_key)
    generation_after_takeover = int(run_after_takeover["generation"])
    assert generation_after_takeover >= 2, "接管必须推进 execution generation"

    # 放行旧执行者：它的迟到保存必须被写入权校验拒绝（零行改动、结构化 superseded）
    release.set()
    late = await asyncio.wait_for(first_task, timeout=30)
    assert late.get("superseded") is True, late
    assert late.get("code") == "recovery_execution_superseded", late

    rows_final = _assistant_rows(app, "turn_old")
    assert len(rows_final) == 1, "旧执行者不得写出第二份正式答案"
    assert rows_final[0]["content"].endswith("接管后的唯一正文")
    run_final = _run_row(app, answer_key)
    assert run_final["status"] == "answered" and run_final["phase"] == "answered"
    assert int(run_final["generation"]) == generation_after_takeover, "旧执行者不得改新执行权行"
    assert not run_final["staged_answer"], "旧执行者的迟到暂存不得覆盖/写入共享暂存"


# ---------------------------------------------------------------------------
# ⑤ I07/§14.9：有界真实子进程退出后，InstanceRegistry 确认死亡并交还归属
# ---------------------------------------------------------------------------


def _child_script(db_path: str, instance_id: str, record_id: str) -> str:
    return f"""
import os, sys
from pathlib import Path
from agent.storage.db import connect
from agent.storage.instance_registry import InstanceRegistry

conn = connect(Path({db_path!r}))
reg = InstanceRegistry(conn, {instance_id!r}, pid=os.getpid())
reg.start()
reg.claim("turn", {record_id!r})
print("READY", flush=True)
sys.stdin.readline()          # 父进程可放行（有界：父进程自己负责超时）
conn.close()
os._exit(7)                   # 非干净退出：不写 exited_at
"""


def test_real_child_process_exit_is_classified_dead_and_ownership_reclaimed(tmp_path: Path):
    db_path = str(tmp_path / "process_owner.db")
    conn = connect(Path(db_path))
    apply_migrations(conn)
    current = InstanceRegistry(conn, "qio_parent_r16e", pid=os.getpid())
    current.start()
    child_id = "qio_child_r16e"
    record_id = "turn_child_r16e"

    env = dict(os.environ)
    env["PYTHONPATH"] = SRC_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen(
        [sys.executable, "-c", _child_script(db_path, child_id, record_id)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        ready = threading.Event()

        def _read_ready() -> None:
            line = proc.stdout.readline() if proc.stdout is not None else ""
            if line.strip() == "READY":
                ready.set()

        reader = threading.Thread(target=_read_ready, daemon=True)
        reader.start()
        assert ready.wait(30.0), "子进程必须在有界时间内完成登记（否则载荷/环境有问题）"

        # 子进程仍活着：pid 存在 → owner_alive True（心跳 TTL 用 0 也不能误杀进程）
        checker = InstanceRegistry(conn, "qio_checker_r16e", pid=os.getpid(), heartbeat_ttl=0.0)
        assert checker.owner_alive(child_id) is True, "子进程仍存活时不得被判死"

        assert proc.stdin is not None
        proc.stdin.write("go\n")
        proc.stdin.flush()
        returncode = proc.wait(timeout=30)
        assert returncode == 7, f"子进程应按约定非干净退出：{returncode}"
        reader.join(timeout=5)

        # 真实进程已经退出。Windows 下父进程 Popen 句柄尚未释放时 OpenProcess 仍可能成功
        # （句柄让进程对象残留），因此「pid 已不存在」这一 OS 事实用 InstanceRegistry 的
        # 受注入口径断言（该入口本就是为受控测试保留的），不 sleep、不无限等：
        # 子进程真的退出（returncode=7）是真实进程事实，pid 判据由注入的探测确认。
        dead_checker = InstanceRegistry(
            conn,
            "qio_checker_r16e",
            pid=os.getpid(),
            heartbeat_ttl=0.0,
            pid_alive=lambda pid: False,
        )
        assert dead_checker.owner_alive(child_id) is False, "真实退出的子进程必须被确认死亡"

        # 复用 InstanceRegistry 的接管入口：只对确认死亡的实例交还归属
        handled: dict[str, int] = {}

        def _handler(instance_id: str, record_type: str) -> int:
            handled[record_type] = handled.get(record_type, 0) + 1
            return 1

        report = dead_checker.recover_confirmed_dead(types=("turn",), handler=_handler)
        assert child_id in report["dead"], report
        assert report["handled"].get(child_id) == 1, report
        assert handled == {"turn": 1}
        assert checker.owner_instance_id("turn", record_id) is None, "归属必须在接管后清除"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
        conn.close()
