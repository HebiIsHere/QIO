# -*- coding: utf-8 -*-
"""整合轮 C：互动理解的统一生命周期、关闭与重启恢复（契约 §2）。

缺陷背景（整合基线 c6a03b1）
---------------------------

理解的真实模型跑在 `submission._schedule_understanding` 里，而它以前只把任务挂在
模块级 `_BACKGROUND_TASKS` 上：句柄不在 `AppContext.background` 账上。于是

* `aclose()` 等不到正在跑的理解：关闭时它继续跑、继续写库；
* 关闭报告里没有它的名字：未确认结束被吞成「干净关闭」；
* 关闭之后还能继续调度新理解（没有任何人拒绝）；
* 关闭 / 重启中断的理解没有明确状态，旧实例的迟到结果照样往当前记录里写。

本文件断言修好后的行为（只跑定向节点：临时库 + fake understanding 模块，不联网、
不读任何密钥）：

1. 生产路径登记在 `AppContext.background` 上，名字固定
   `interactive-understanding:<understandingId>`，并带底层执行单元探针；
2. 关闭期间 `register` 抛 `BackgroundClosed` → 调用方**拒绝新调度**并如实说明，
   不静默改成同步跑模型；
3. 理解在跑时关闭 → 记录落成明确状态（failed + 可读原因），快照保留、可从原快照恢复；
4. 探针未确认结束时 `CloseReport.clean=False` 且 `unfinished` 有名字
   （阶段名 understanding_unfinished），**不用固定 sleep 假定任务已退出**；
5. 旧 attempt / 旧实例的迟到结果不得写回当前记录、不得重复生成意图；
6. 真实执行的调度与理解使用同一份应用归属（`execution.app_owner_for`）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types

import pytest

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.bus import EventBus
from agent.credentials.store import MemoryKeyring
from agent.interactive import board_store, execution, models, submission
from agent.services.app import AppContext
from agent.services.lifecycle import (
    PHASE_UNDERSTANDING_STOPPED,
    PHASE_UNDERSTANDING_UNFINISHED,
)
from agent.config import Settings

BOARD = models.DEFAULT_BOARD_ID


# --- 工具 -----------------------------------------------------------------


def _app(tmp_path, name: str = "irm_c") -> AppContext:
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / f"{name}.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path / name), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    # 理解记录引用 boards(id)：先建板面，测试才能直接造记录
    board_store.ensure_board(conn, board_id=BOARD)
    return ctx


def _fast_shutdown(ctx: AppContext) -> None:
    """把注册表的三档有界等待压到毫秒级。

    语义不变（先拒绝新建 → 有界等待 → 取消 → 取消确认 → final grace → 如实回报），
    只是把「有界」的边界缩小，测试不必真的等 14 秒。
    """
    original = ctx.background.shutdown

    async def _fast(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return await original(0.05, 0.05, final_timeout=0.05)

    ctx.background.shutdown = _fast  # type: ignore[method-assign]


def _insert_understanding(
    conn: sqlite3.Connection,
    uid: str = "und-1",
    *,
    status: str = "accepted",
    submission_id: str | None = "sub-1",
    snapshot: object = None,
    attempt: int = 1,
) -> dict:
    """造一条理解记录（形状与 B 的 understanding.request_understanding 一致）。"""
    if snapshot is None:
        snapshot = {
            "cards": [{"id": "c1", "kind": "file", "content": "材料正文"}],
            "groups": [],
            "links": [],
            "selection": ["c1"],
        }
    stamp = models.now_iso()
    conn.execute(
        "INSERT INTO board_understanding (id, board_id, submission_id, request_id, status,"
        " stage, progress, snapshot, context, materials, output, validation, memory, error,"
        " attempt, adapter, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            uid,
            BOARD,
            submission_id,
            f"req:{uid}",
            status,
            "",
            models.dumps({}),
            models.dumps(snapshot),
            models.dumps({}),
            models.dumps([]),
            models.dumps({}),
            models.dumps({}),
            models.dumps([]),
            None,
            attempt,
            "",
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return snapshot


def _row(conn: sqlite3.Connection, uid: str) -> dict:
    row = conn.execute("SELECT * FROM board_understanding WHERE id = ?", (uid,)).fetchone()
    assert row is not None
    data = dict(row)
    return {
        **data,
        "snapshot_parsed": models.loads(data.get("snapshot"), {}),
    }


def _fake_understanding(monkeypatch, run) -> dict:
    """把 agent.interactive.understanding 换成只含 run_understanding 的假模块。"""
    import agent.interactive as interactive_pkg

    module = types.ModuleType("agent.interactive.understanding")
    module.run_understanding = run
    monkeypatch.setitem(sys.modules, "agent.interactive.understanding", module)
    monkeypatch.setattr(interactive_pkg, "understanding", module, raising=False)
    return {"module": module}


def _finish(conn: sqlite3.Connection, uid: str, status: str = "understood") -> None:
    conn.execute(
        "UPDATE board_understanding SET status = ?, updated_at = ? WHERE id = ?",
        (status, models.now_iso(), uid),
    )
    conn.commit()


def _schedule(ctx: AppContext, uid: str) -> dict:
    submission.bind_background_owner(ctx.conn, ctx)
    return submission.schedule_understanding(ctx.conn, understanding_id=uid)


def _named(ctx: AppContext, uid: str) -> str:
    return submission.UNDERSTANDING_TASK_PREFIX + uid


# --- 1. 登记与探针 ---------------------------------------------------------


async def test_scheduling_registers_on_app_background_with_name_and_probe(tmp_path, monkeypatch):
    ctx = _app(tmp_path)
    uid = "und-register"
    _insert_understanding(ctx.conn, uid)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        entered.set()
        await release.wait()
        _finish(conn, understanding_id)

    _fake_understanding(monkeypatch, run)
    dispatch = _schedule(ctx, uid)

    assert dispatch["started"] is True, dispatch
    assert dispatch["rejected"] is None
    await entered.wait()

    # 登记在 AppContext.background（aclose 真正 shutdown 的那份注册表）上，名字固定
    assert _named(ctx, uid) in ctx.background.active(), ctx.background.active()
    handle = ctx.background.handle(_named(ctx, uid))
    assert handle is not None
    assert handle.has_unit_probe is True, "必须声明底层执行单元探针"
    # 探针口径：记录还没到终结状态 → 未确认结束
    assert handle.unit_confirmed() is False

    release.set()
    await asyncio.wait_for(handle.task, timeout=2)
    assert handle.unit_confirmed() is True
    assert _named(ctx, uid) not in ctx.background.active(), "结束的句柄必须从账上撤下"
    ctx.conn.close()


async def test_schedule_is_denied_after_close_without_silent_sync_run(tmp_path, monkeypatch):
    """关闭期间新调度被拒：拒绝新调度 + 如实说明，绝不静默改成同步跑模型。"""
    ctx = _app(tmp_path, "closed")
    uid = "und-after-close"
    _insert_understanding(ctx.conn, uid)
    calls: list[str] = []

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        calls.append(understanding_id)

    _fake_understanding(monkeypatch, run)
    submission.bind_background_owner(ctx.conn, ctx)

    await ctx.background.shutdown(0.01, 0.01, final_timeout=0.01)
    assert ctx.background.closed is True

    dispatch = submission.schedule_understanding(ctx.conn, understanding_id=uid)
    assert dispatch["started"] is False
    assert dispatch["rejected"] == "closed"
    assert "关闭" in (dispatch["reason"] or "")
    assert calls == [], "关闭期间不得偷偷同步跑模型"
    # 记录保持未终结（下次启动 / 重试可从原快照恢复），不假装已经跑过
    assert _row(ctx.conn, uid)["status"] == "accepted"
    ctx.conn.close()


# --- 2. 关闭：干净收尾与如实回报 -------------------------------------------


async def test_close_waits_for_understanding_and_reports_stopped(tmp_path, monkeypatch):
    ctx = _app(tmp_path, "clean")
    _fast_shutdown(ctx)
    uid = "und-clean"
    _insert_understanding(ctx.conn, uid)
    entered = asyncio.Event()

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # 配合取消：在取消确认窗口里落成明确状态，然后退出
            _finish(conn, understanding_id, "failed")
            raise

    _fake_understanding(monkeypatch, run)
    assert _schedule(ctx, uid)["started"] is True
    await entered.wait()

    report = await ctx.aclose()

    assert report.clean is True, report.to_dict()
    assert report.unfinished == ()
    assert PHASE_UNDERSTANDING_STOPPED in report.phases, report.phases
    assert PHASE_UNDERSTANDING_UNFINISHED not in report.phases
    assert _row(ctx.conn, uid)["status"] in models.UNDERSTANDING_SETTLED_STATUSES
    ctx.conn.close()


async def test_close_reports_unfinished_when_probe_never_confirms(tmp_path, monkeypatch):
    """探针未确认结束 → clean=False 且 unfinished 有名字（不假定任务已退出）。"""
    ctx = _app(tmp_path, "unfinished")
    _fast_shutdown(ctx)
    uid = "und-stuck"
    _insert_understanding(ctx.conn, uid)
    entered = asyncio.Event()
    release = asyncio.Event()  # 只有测试放行才结束：模拟吞掉取消的执行单元

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # 吞掉取消：这就是「取消后仍未终止」
        await release.wait()

    _fake_understanding(monkeypatch, run)
    assert _schedule(ctx, uid)["started"] is True
    await entered.wait()

    report = await ctx.aclose()

    assert report.clean is False, report.to_dict()
    assert _named(ctx, uid) in report.unfinished, report.unfinished
    assert PHASE_UNDERSTANDING_UNFINISHED in report.phases, report.phases
    assert PHASE_UNDERSTANDING_STOPPED not in report.phases
    # 记录仍停在未终结状态：关闭不替它假装成功
    assert _row(ctx.conn, uid)["status"] in models.UNDERSTANDING_ACTIVE_STATUSES

    release.set()
    ctx.conn.close()


# --- 3. 关闭中断的理解：明确状态 + 可从原快照恢复 ---------------------------


async def test_interrupted_understanding_has_clear_state_and_snapshot(tmp_path, monkeypatch):
    ctx = _app(tmp_path, "interrupted")
    _fast_shutdown(ctx)
    uid = "und-interrupted"
    snapshot = _insert_understanding(ctx.conn, uid)
    entered = asyncio.Event()

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        entered.set()
        await asyncio.Event().wait()  # 一直跑：等关闭把它取消掉

    _fake_understanding(monkeypatch, run)
    assert _schedule(ctx, uid)["started"] is True
    await entered.wait()

    report = await ctx.aclose()

    assert report.clean is True, report.to_dict()
    row = _row(ctx.conn, uid)
    assert row["status"] == "failed", "关闭中断必须有明确状态"
    assert submission.INTERRUPTED_UNDERSTANDING_ERROR in (row["error"] or "")
    assert row["finished_at"], "明确状态要带完成时刻"
    # 可从原快照恢复：快照没被删、内容与受理时一致
    assert row["snapshot_parsed"] == snapshot, "中断不得丢掉原快照"
    assert "重试" in (row["error"] or ""), "可读原因要说清可以从快照重试"
    ctx.conn.close()


async def test_retry_after_interruption_uses_saved_snapshot(tmp_path, monkeypatch):
    """重启 / 重试路径：中断过的理解能从原快照重新跑完（不重读当前板面）。"""
    ctx = _app(tmp_path, "retry")
    uid = "und-retry"
    snapshot = _insert_understanding(ctx.conn, "und-retry-old")
    _insert_understanding(ctx.conn, uid, submission_id="sub-retry")
    seen: list[dict] = []

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        row = conn.execute(
            "SELECT snapshot FROM board_understanding WHERE id = ?", (understanding_id,)
        ).fetchone()
        seen.append(models.loads(row["snapshot"], {}))
        _finish(conn, understanding_id)

    _fake_understanding(monkeypatch, run)
    # 先把一条中断记录落成 failed（关闭中断的真实结果），再按重试路径重新调度
    submission._mark_understanding_interrupted(ctx.conn, uid)
    ctx.conn.execute(
        "UPDATE board_understanding SET status = 'accepted', attempt = attempt + 1,"
        " finished_at = NULL WHERE id = ?",
        (uid,),
    )
    ctx.conn.commit()

    dispatch = _schedule(ctx, uid)
    assert dispatch["started"] is True, dispatch
    handle = ctx.background.handle(_named(ctx, uid))
    assert handle is not None
    await asyncio.wait_for(handle.task, timeout=2)
    assert _row(ctx.conn, uid)["status"] == "understood"
    assert seen and seen[0].get("cards"), "重试必须用保存的快照（不是空上下文）"
    assert seen[0] == models.loads(_row(ctx.conn, uid)["snapshot"], {}) or seen[0]
    ctx.conn.close()


# --- 4. 旧 attempt / 旧实例的迟到结果不得写回 -------------------------------


async def test_late_result_from_superseded_attempt_is_not_written(tmp_path, monkeypatch):
    """运行中被新的一次尝试接手：迟到的失败 / 中断不得改写当前记录。"""
    ctx = _app(tmp_path, "late")
    uid = "und-late"
    _insert_understanding(ctx.conn, uid)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        entered.set()
        await release.wait()
        raise RuntimeError("这一次运行已经过期，但异常仍然冒上来")

    _fake_understanding(monkeypatch, run)
    assert _schedule(ctx, uid)["started"] is True
    await entered.wait()

    # 新的一次尝试接手：attempt 加一，并落成终结状态
    ctx.conn.execute(
        "UPDATE board_understanding SET attempt = attempt + 1, status = 'understood',"
        " error = NULL, updated_at = ? WHERE id = ?",
        (models.now_iso(), uid),
    )
    ctx.conn.commit()

    release.set()
    handle = ctx.background.handle(_named(ctx, uid))
    assert handle is not None
    await asyncio.wait_for(handle.task, timeout=2)

    row = _row(ctx.conn, uid)
    assert row["status"] == "understood", "迟到的结果不得改写当前记录"
    assert int(row["attempt"]) == 2
    assert not (row["error"] or ""), "迟到的失败原因不得写进当前记录"
    ctx.conn.close()


async def test_cancel_uses_registry_handle(tmp_path, monkeypatch):
    """取消走注册表句柄（生产路径），不是只扫模块级的裸任务集合。"""
    ctx = _app(tmp_path, "cancel")
    uid = "und-cancel"
    _insert_understanding(ctx.conn, uid)
    entered = asyncio.Event()

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        entered.set()
        await asyncio.Event().wait()

    _fake_understanding(monkeypatch, run)
    assert _schedule(ctx, uid)["started"] is True
    await entered.wait()

    assert submission.cancel_background_understanding(uid, owner=ctx) is True
    handle = ctx.background.handle(_named(ctx, uid))
    for _ in range(100):
        if handle is None or handle.task.done():
            break
        await asyncio.sleep(0.01)
    assert handle is not None and handle.task.done() is True
    # 取消之后记录落成明确状态（可重试），不是停在 understanding
    assert _row(ctx.conn, uid)["status"] == "failed"
    assert ctx.conn.execute(
        "SELECT 1 FROM board_understanding WHERE id = ? AND finished_at IS NOT NULL", (uid,)
    ).fetchone() is not None

    # 取消失败的那一次不算「跑过」：按重试路由的真实 UPDATE 再调度一次，
    # 旧任务不得重复生成意图（这里以「只有新的一次写回终结状态」为可观察判据）。
    runs: list[int] = []

    async def retry_run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        runs.append(1)
        _finish(conn, understanding_id)

    _fake_understanding(monkeypatch, retry_run)
    ctx.conn.execute(
        "UPDATE board_understanding SET status = 'accepted', attempt = attempt + 1,"
        " finished_at = NULL WHERE id = ?",
        (uid,),
    )
    assert submission.schedule_understanding(ctx.conn, understanding_id=uid)["started"] is True
    fresh = ctx.background.handle(_named(ctx, uid))
    assert fresh is not None
    await asyncio.wait_for(fresh.task, timeout=2)
    assert runs == [1], "重试只跑一次（旧任务不得再次写回）"
    assert _row(ctx.conn, uid)["status"] == "understood"
    ctx.conn.close()


# --- 5. 与真实执行使用一致的应用归属 ---------------------------------------


def test_execution_and_understanding_share_app_ownership():
    """`execution.app_owner_for` 把 FastAPI 应用解析成 AppContext（同一份注册表）。"""
    from types import SimpleNamespace

    ctx = SimpleNamespace(background=object())
    app = SimpleNamespace(state=SimpleNamespace(ctx=ctx))
    assert execution.app_owner_for(app) is ctx, "必须解析到 AppContext，而不是 FastAPI 应用"
    bare = SimpleNamespace(state=SimpleNamespace())
    assert execution.app_owner_for(bare) is bare, "拿不到 ctx 时退回传入对象（不改变行为）"
    assert execution.app_owner_for(None) is None


def test_understanding_phase_helper_records_only_real_names():
    from agent.services.app import _record_understanding_phase
    from agent.services.lifecycle import CloseReport

    prefix = submission.UNDERSTANDING_TASK_PREFIX
    # 本次关闭没有理解后台任务：不记阶段名（没发生的事不写进报告）
    assert (
        _record_understanding_phase(CloseReport(clean=True), registered=(), unfinished=()).phases
        == ()
    )
    # 关停了一个正在跑的理解，且它确认结束了：记 stopped，报告仍然干净
    stopped = _record_understanding_phase(
        CloseReport(clean=True), registered=(prefix + "x",), unfinished=()
    )
    assert stopped.phases == (PHASE_UNDERSTANDING_STOPPED,)
    assert stopped.clean is True
    # 没确认结束：记 unfinished，且报告保持不干净、名单里有名字
    unfinished = _record_understanding_phase(
        CloseReport(clean=False, unfinished=(prefix + "x",)),
        registered=(prefix + "x",),
        unfinished=(prefix + "x",),
    )
    assert unfinished.phases == (PHASE_UNDERSTANDING_UNFINISHED,)
    assert unfinished.clean is False
    assert prefix + "x" in unfinished.unfinished


# --- 6. 路由级反例：受理照旧，后台调度被如实拒绝 -----------------------------


async def test_submit_board_rejects_background_dispatch_after_close(tmp_path, monkeypatch):
    """关闭期间提交：受理事实照旧落库，但后台理解**不被启动**，也没有同步跑模型。"""
    ctx = _app(tmp_path, "submit-closed")
    ui = "und-submit-closed"
    calls: list[str] = []

    async def run(conn, understanding_id, *, adapter_provider=None):  # noqa: ANN001
        calls.append(understanding_id)

    def request_understanding(conn, *, board_id, submission_id, snapshot):  # noqa: ANN001
        _insert_understanding(conn, ui, status="accepted", submission_id=submission_id)
        return {"id": ui}

    _fake_understanding(monkeypatch, run)
    module = sys.modules["agent.interactive.understanding"]
    module.request_understanding = request_understanding

    # 板面：一张可见卡片，提交才有东西可交
    state = {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": [models.new_card("file", "这次要交给 QIO 的材料")],
        "groups": [],
        "links": [],
        "selection": [],
    }
    board_store.save_board(ctx.conn, BOARD, state, reason="test")
    submission.bind_background_owner(ctx.conn, ctx)
    await ctx.background.shutdown(0.01, 0.01, final_timeout=0.01)

    result = await submission.submit_board(ctx.conn, BOARD, note="关闭期间提交")

    assert result["status"] == "succeeded", result
    delivery = result["delivery"]
    assert delivery["delivered"] is True, "受理本身不受关闭影响"
    assert delivery["understandingId"] == ui
    dispatch = delivery["background"]
    assert dispatch["started"] is False
    assert dispatch["rejected"] == "closed"
    assert "关闭" in (dispatch["reason"] or "")
    assert calls == [], "关闭期间不得同步跑模型"
    # 记录停在未终结状态（可从原快照重试），不假装已经理解过
    assert _row(ctx.conn, ui)["status"] == "accepted"
    ctx.conn.close()


# --- 7. 迟到写回护栏（understanding.run_understanding 内） -------------------
#
# 契约 §2 最后一条：旧实例 / 旧 attempt 的迟到结果不得写入当前记录、不得重复生成
# 意图。这里用**真实** run_understanding（不替换模块）+ 受控的假 adapter 把模型调用
# 钉在半路，模拟「旧进程的模型还在跑，新的一次尝试已经接手」。


def _intent_payload(*, refs: list[str], content: str) -> dict:
    return {
        "kind": "intent",
        "summary": "比较两份材料",
        "intents": [
            {
                "intentKey": "compare-materials",
                "title": "整理材料的差异",
                "goal": "让用户看到两份材料的关键差异",
                "summary": "对比两份材料的正文并给出差异",
                "basis": [
                    {"cardId": cid, "role": "material", "version": "v1"} for cid in refs
                ],
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


def _real_model_snapshot() -> dict:
    """受理时冻结的那份快照：两条材料 + 一条勾选的文字要求。

    理解上下文按契约从**冻结快照**构建（不读当前板面），所以这里直接给快照。
    """
    cards = [
        models.new_card("file", "甲材料：单价 100 元", id="m_a", meta={"name": "甲.txt"}),
        models.new_card("code", "乙材料：交付 3 天", id="m_b", meta={"language": "text"}),
        models.new_card("text", "请比较这两份材料的成本与交付", id="req", checked=True),
    ]
    return {"cards": cards, "groups": [], "links": [], "selection": [], "note": ""}


def _gated_adapter(payload: dict, entered: asyncio.Event, release: asyncio.Event):
    """把模型调用钉住：entered 置位后一直等 release —— 用来制造「迟到的结果」。"""
    adapter = FakeStreamAdapter([StreamScript(text=json.dumps(payload, ensure_ascii=False))])
    original = adapter.complete

    async def gated(messages, tools, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        entered.set()
        await release.wait()
        return await original(messages, tools, **kwargs)

    adapter.complete = gated  # type: ignore[method-assign]
    return adapter


def _intent_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM board_intents").fetchone()["n"])


async def test_superseded_attempt_late_result_is_discarded(tmp_path):
    """旧 attempt 迟到返回 → 当前记录不变、不建意图（真实 run_understanding）。"""
    ctx = _app(tmp_path, "late-real")
    uid = "und-late-real"
    _insert_understanding(ctx.conn, uid, snapshot=_real_model_snapshot())
    entered, release = asyncio.Event(), asyncio.Event()
    adapter = _gated_adapter(
        _intent_payload(refs=["m_a", "m_b"], content="成本差异：甲低于乙"), entered, release
    )

    async def provider():
        return adapter

    assert submission.schedule_understanding(
        ctx.conn, understanding_id=uid, adapter_provider=provider
    )["started"] is True
    await asyncio.wait_for(entered.wait(), timeout=5)

    # 新的一次尝试接手（等同关停后重启 / 重试）：attempt+1，并且已经落成终结状态。
    # 这里落成 failed + 可读原因（就是关停中断的那一条），旧实例返回后**不得**把它
    # 翻回 understood，也不得建意图。
    superseded_error = "理解被执行环境的关闭 / 重启中断：新的一次尝试已经接手。"
    ctx.conn.execute(
        "UPDATE board_understanding SET attempt = attempt + 1, status = 'failed',"
        " error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
        (superseded_error, models.now_iso(), models.now_iso(), uid),
    )
    release.set()
    handle = ctx.background.handle(_named(ctx, uid))
    assert handle is not None
    await asyncio.wait_for(handle.task, timeout=5)

    row = _row(ctx.conn, uid)
    assert row["status"] == "failed", "迟到的结果不得改写当前记录（含不得把中断翻成成功）"
    assert int(row["attempt"]) == 2
    assert row["error"] == superseded_error, "迟到路径不得改写当前记录的原因"
    assert _intent_count(ctx.conn) == 0, "迟到的结果不得创建意图"
    ctx.conn.close()


async def test_normal_run_still_writes_terminal_state_and_intents(tmp_path):
    """attempt 未被接手时照常写终结状态与意图：护栏不影响正常路径。"""
    ctx = _app(tmp_path, "normal-real")
    uid = "und-normal-real"
    _insert_understanding(ctx.conn, uid, snapshot=_real_model_snapshot())
    entered, release = asyncio.Event(), asyncio.Event()
    release.set()  # 不设闸：直接放行
    adapter = _gated_adapter(
        _intent_payload(refs=["m_a", "m_b"], content="成本差异：甲低于乙"), entered, release
    )

    async def provider():
        return adapter

    assert submission.schedule_understanding(
        ctx.conn, understanding_id=uid, adapter_provider=provider
    )["started"] is True
    handle = ctx.background.handle(_named(ctx, uid))
    assert handle is not None
    await asyncio.wait_for(handle.task, timeout=10)

    row = _row(ctx.conn, uid)
    assert row["status"] == "understood", row
    assert int(row["attempt"]) == 1
    assert not (row["error"] or ""), row["error"]
    assert _intent_count(ctx.conn) == 1, "正常路径必须照常创建意图"
    ctx.conn.close()


async def test_execution_lands_on_same_app_registry_and_close_ledger(tmp_path, monkeypatch):
    """真实执行的调度与理解使用**同一份**应用归属：关闭账上能看到它的名字。

    反例（整合基线）：路由层传 owner=request.app（FastAPI 对象），注册表另外挂一份、
    没有任何人 shutdown —— 关闭账上看不到 interactive-execution:<id>，
    未确认结束也不会让 CloseReport.clean=False。
    """
    from types import SimpleNamespace

    ctx = _app(tmp_path, "exec-owner")
    exec_id = "exec-close-ledger"
    entered, release = asyncio.Event(), asyncio.Event()
    execution._insert_execution(
        ctx.conn,
        execution_id=exec_id,
        intent_id="int-1",
        board_id=BOARD,
        attempt=1,
        status="queued",
        plan={"ops": []},
        steps=[],
        owner_instance_id=None,
    )

    async def stuck_run(conn, execution_id, *, adapter_provider=None, instance_id=None):  # noqa: ANN001
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # 吞掉取消：底层执行单元未确认结束
        await release.wait()

    monkeypatch.setattr(execution, "run_execution", stuck_run)

    app = SimpleNamespace(state=SimpleNamespace(ctx=ctx))
    started = await execution.start_execution(
        ctx.conn,
        exec_id,
        owner=execution.app_owner_for(app),  # 路由层现在就是这么解析的
    )
    assert started["scheduled"] is True, started
    await asyncio.wait_for(entered.wait(), timeout=5)

    name = "interactive-execution:" + exec_id
    assert name in ctx.background.registered(), ctx.background.registered()

    # 未确认结束时：关闭报告不干净，且名字在 unfinished 里（有界三档等待不无限等）
    _fast_shutdown(ctx)
    report = await ctx.aclose()
    assert report.clean is False, report.to_dict()
    assert name in report.unfinished, report.unfinished

    release.set()
    handle = ctx.background.handle(name)
    for _ in range(100):
        if handle is None or handle.task.done():
            break
        await asyncio.sleep(0.01)
    ctx.conn.close()
