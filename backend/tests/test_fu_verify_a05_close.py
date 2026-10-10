"""V 组独立验证 · A05：关闭结果必须真实。

原缺陷（基线 `da0436b`，见 `_contracts` §4）

    `BackgroundTasks.shutdown()` 本身已经会如实回报 `unfinished`（上一轮修的），
    但**调用方拿到的东西并不真实**：

    * `AppContext.aclose()` 的返回类型是 `None` —— 调用方无法知道「后台执行单元
      到底有没有全部结束」；
    * 于是它**无条件** `instances.mark_clean_exit()`：即使有后台协程吞掉取消、
      根本没结束，也照样写下「干净退出」——下次启动会据此认为这个实例是干净退出的，
      「心跳 + pid」的兜底判据被这一条假证据覆盖；
    * 它也**无条件**释放 adapter / HTTP client：残留协程之后会打到已经关掉的
      client 上；
    * `api/server.py` 的 lifespan 在 `close_db_on_shutdown=True` 时**无条件**关掉
      数据库 —— 没有结束的后台协程接着写库，就是「数据库已关闭」类错误或半截状态。

    没有 `CloseReport`（`services/lifecycle.py` 在基线不存在），也没有
    `ShutdownReport.still_running`；「干净退出标记 / 依赖释放顺序 / 重启恢复 /
    无关闭后写库」四件事都无法从调用方观察。

验证手段（受控闸门 + 缩短超时，不随机长等待）

    * 四种关闭状态：正常结束 / 配合取消 / 延迟取消 / 取消后仍未结束；
    * 断言调用方拿到的 report（`clean` / `unfinished` / `phases` / `detail`）；
    * 断言台账里的干净退出标记（`instances.exited_at`）；
    * 断言依赖释放顺序（后台 → 维护 → turn → 独立任务 → 重活 → 干净退出 → adapter）；
    * 断言 unclean 时**不**释放 adapter、**不**关数据库；clean 时照旧；
    * 断言重启后仍能按归属恢复（clean 的实例记录被标 interrupted 并进入入口）。

    基线在 CloseReport / 干净退出标记 / DB 关闭三处全红；修复后仍绿。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.services.background import (
    DEFAULT_CANCEL_TIMEOUT,
    DEFAULT_SHUTDOWN_TIMEOUT,
    BackgroundTasks,
)
from agent.storage.db import connect
from agent.storage.instance_registry import InstanceRegistry
from agent.storage.migrate import apply_migrations
from agent.storage.turn_journal import TurnJournal

NOW = "2026-10-10T00:00:00+00:00"


# --------------------------------------------------------------------------
# 四种关闭状态的受控执行单元
# --------------------------------------------------------------------------


async def _normal():
    await asyncio.sleep(0)


async def _cooperative():
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        return


async def _delayed():
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.sleep(0.05)  # 有界清理：在 grace 窗口内结束
        return


async def _stuck(release: asyncio.Event):
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass  # 吞掉取消：这就是「取消后仍未结束」
    await release.wait()


class _FakeAdapter:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _CountingConnection(sqlite3.Connection):
    """记账连接：把写语句记在实例的 `writes` 上。

    为什么要子类：`sqlite3.Connection` 是 C 类型，**实例属性不可赋值**
    （`conn.execute = f` → `AttributeError: ... attribute 'execute' is read-only`），
    所以只能在**子类**上覆写 `execute`，再用 `sqlite3.connect(..., factory=...)`
    把子类实例交给 `AppContext`。参数与 `agent.storage.db.connect()` 保持一致
    （见 `_connect_counting`），这样观察到的写与真实运行路径同源。
    """

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.writes: list[str] = []

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
        head = str(sql).strip().split(" ", 1)[0].upper()
        if head in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            self.writes.append(str(sql))
        return super().execute(sql, *args, **kwargs)


def _connect_counting(db_path: Path) -> _CountingConnection:
    """`agent.storage.db.connect()` 的同参版本，只换成记账子类。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, factory=_CountingConnection)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn  # type: ignore[return-value]


def _make_ctx(
    tmp_path: Path,
    name: str = "ctx",
    *,
    conn_factory=connect,  # noqa: ANN001
) -> tuple[AppContext, sqlite3.Connection]:
    conn = conn_factory(tmp_path / f"{name}.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path / name), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx, conn


def _shorten_shutdown(ctx: AppContext) -> None:
    """把关闭等待压到毫秒级（语义不变：等待 → 取消 → grace → 仍未结束）。"""
    original = ctx.background.shutdown

    async def _fast(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return await original(0.05, cancel_timeout=0.05)

    ctx.background.shutdown = _fast  # type: ignore[method-assign]


def _tap(ctx: AppContext, order: list[str]) -> _FakeAdapter:
    """记录收尾阶段的真实调用顺序（受控替身，不改实现），并返回播种的 adapter。

    返回引用是必须的：`aclose()` 在「干净释放」时执行
    `adapters, self._adapter_cache = list(...), {}`（释放过的不许再被发出去 ——
    这是正确行为），所以**关闭之后**再去读 `_adapter_cache` 只会读到空字典。
    断言必须落在「关闭前播种的那个对象」上。
    """

    def _wrap_async(name: str, target: object, attr: str) -> None:
        original = getattr(target, attr)

        async def _wrapped(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            order.append(name)
            return await original(*args, **kwargs)

        setattr(target, attr, _wrapped)

    _wrap_async("maintenance.stop", ctx.maintenance, "stop")
    _wrap_async("turns.shutdown", ctx.turns, "shutdown")
    _wrap_async("task_manager.shutdown", ctx.task_manager, "shutdown")
    _wrap_async("heavy.shutdown", ctx.heavy, "shutdown")

    background_original = ctx.background.shutdown

    async def _background(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        order.append("background.shutdown")
        return await background_original(*args, **kwargs)

    ctx.background.shutdown = _background  # type: ignore[method-assign]

    mark_original = ctx.instances.mark_clean_exit

    def _mark() -> None:
        order.append("mark_clean_exit")
        mark_original()

    ctx.instances.mark_clean_exit = _mark  # type: ignore[method-assign]

    adapter = _FakeAdapter()
    ctx._adapter_cache[("key_verify", 1, "https://api.example.com/v1", "gpt-x")] = adapter
    return adapter


def _exited_at(conn: sqlite3.Connection, instance_id: str) -> str | None:
    row = conn.execute(
        "SELECT exited_at FROM instances WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    assert row is not None, "本实例必须登记在 instances 表里"
    return row["exited_at"]


def _report_dict(report) -> dict:  # noqa: ANN001
    assert report is not None, "aclose() 必须返回关闭报告（基线返回 None）"
    to_dict = getattr(report, "to_dict", None)
    assert callable(to_dict), "关闭报告必须有 to_dict()"
    data = to_dict()
    assert {"clean", "unfinished", "phases", "detail"} <= set(data), (
        f"关闭报告字段不全：{sorted(data)}"
    )
    return data


# --------------------------------------------------------------------------
# 状态 1：正常结束
# --------------------------------------------------------------------------


async def test_a05_state_normal_reports_clean_and_persists_clean_exit(tmp_path):
    ctx, conn = _make_ctx(tmp_path, "normal")
    order: list[str] = []
    _shorten_shutdown(ctx)
    adapter = _tap(ctx, order)
    ctx.background.register("normal", _normal())

    report = await ctx.aclose()
    data = _report_dict(report)

    assert data["clean"] is True, f"正常结束必须是 clean：{data}"
    assert data["unfinished"] == []
    assert isinstance(data["phases"], (list, tuple)) and data["phases"], "必须记录收尾阶段"
    assert str(data["detail"])
    assert _exited_at(conn, ctx.instance_id), "clean=True 必须写干净退出标记"
    assert order[0] == "background.shutdown", f"后台必须先停：{order}"
    assert adapter.closed is True, "clean=True 必须释放关闭前播种的那个 adapter"
    conn.close()


async def test_a05_state_normal_writes_nothing_after_close(tmp_path):
    """无关闭后写库：所有执行单元确认结束后，台账/库不再被触碰。

    观察点是**记账连接**（`sqlite3.Connection` 子类，覆写 `execute` 记账）：
    C 类型不允许给实例赋 `execute`（原探针在本 Python 上从未可能通过），
    子类化是同一观察点的可行写法，断言强度不变（关闭后写语句条数不再增长），
    并且额外要求「关闭前确实观察到过写」—— 否则探针是空跑。
    """
    ctx, conn = _make_ctx(tmp_path, "normal-nw", conn_factory=_connect_counting)
    _shorten_shutdown(ctx)
    ctx.background.register("normal", _normal())

    writes = conn.writes  # type: ignore[attr-defined]
    report = await ctx.aclose()
    assert _report_dict(report)["clean"] is True
    baseline = len(writes)
    assert baseline > 0, "记账连接必须真的观察到关闭前的写（否则这条探针是空跑）"
    await asyncio.sleep(0.05)
    assert len(writes) == baseline, f"关闭完成后不得再写库：{writes[baseline:]}"
    conn.close()


# --------------------------------------------------------------------------
# 状态 2：配合取消
# --------------------------------------------------------------------------


async def test_a05_state_cooperative_cancel_is_clean(tmp_path):
    ctx, conn = _make_ctx(tmp_path, "coop")
    order: list[str] = []
    _shorten_shutdown(ctx)
    adapter = _tap(ctx, order)
    ctx.background.register("coop", _cooperative())

    report = await ctx.aclose()
    data = _report_dict(report)

    assert data["clean"] is True, f"配合取消必须收干净：{data}"
    assert data["unfinished"] == []
    assert _exited_at(conn, ctx.instance_id)
    assert adapter.closed is True
    conn.close()


# --------------------------------------------------------------------------
# 状态 3：延迟取消
# --------------------------------------------------------------------------


async def test_a05_state_delayed_cancel_is_clean(tmp_path):
    ctx, conn = _make_ctx(tmp_path, "delayed")
    order: list[str] = []
    _shorten_shutdown(ctx)
    adapter = _tap(ctx, order)
    ctx.background.register("delayed", _delayed())

    report = await ctx.aclose()
    data = _report_dict(report)

    assert data["clean"] is True, f"延迟取消在 grace 内结束也必须算干净：{data}"
    assert data["unfinished"] == []
    assert _exited_at(conn, ctx.instance_id), "确实收干净了就该写干净退出"
    assert adapter.closed is True
    conn.close()


# --------------------------------------------------------------------------
# 状态 4：取消后仍未结束
# --------------------------------------------------------------------------


async def test_a05_state_stuck_after_cancel_is_not_reported_clean(tmp_path):
    ctx, conn = _make_ctx(tmp_path, "stuck")
    order: list[str] = []
    _shorten_shutdown(ctx)
    adapter = _tap(ctx, order)
    release = asyncio.Event()
    handle = ctx.background.register("stuck", _stuck(release))

    report = await ctx.aclose()
    data = _report_dict(report)

    assert data["clean"] is False, f"有执行单元没结束就不能报 clean：{data}"
    assert "stuck" in list(data["unfinished"]), f"必须如实列出未结束的名单：{data}"
    assert handle.task.done() is False, "不得把「句柄取消了」当成「执行单元结束了」"
    assert _exited_at(conn, ctx.instance_id) is None, (
        "有后台协程没结束就不得写干净退出标记（那会让下次启动误判）"
    )
    assert adapter.closed is False, "不 clean 时不得释放 adapter（残留协程会打到已关的 client）"
    assert "mark_clean_exit" not in order, f"不 clean 时不得走干净退出的动作：{order}"
    # 收尾顺序：后台仍然排在最前，其余依赖照旧被停掉
    assert order[0] == "background.shutdown", f"{order}"
    assert "turns.shutdown" in order, f"不 clean 也不能跳过后面的收尾：{order}"
    assert order.index("background.shutdown") < order.index("turns.shutdown")

    release.set()
    await asyncio.sleep(0.05)
    assert handle.task.done() is True, "测试收尾：放行后该任务应当结束（不留悬挂任务）"
    conn.close()


async def test_a05_stuck_report_from_registry_exposes_still_running_or_unfinished():
    """注册表层：取消后仍未结束必须能被调用方判定（`still_running` 或 `unfinished`）。"""
    registry = BackgroundTasks()
    release = asyncio.Event()
    handle = registry.register("stuck", _stuck(release))

    report = await registry.shutdown(0.01, cancel_timeout=0.05)
    payload = report.as_dict()

    assert payload["clean"] is False
    assert payload["unfinished"] == ["stuck"]
    named = getattr(report, "still_running", None)
    if named is not None:
        assert list(named) == ["stuck"], "still_running 必须如实列出未确认结束的执行单元"
    assert handle.task.done() is False
    assert registry.active() == ["stuck"]

    release.set()
    await asyncio.sleep(0.05)


async def test_a05_delayed_cancel_gets_a_bounded_final_grace():
    """两阶段有界：取消之后再给一个 `final_timeout` grace，仍不结束才如实报未完成。"""
    registry = BackgroundTasks()
    handle = registry.register("delayed", _delayed())

    report = await registry.shutdown(0.01, cancel_timeout=0.01, final_timeout=0.8)

    assert report.unfinished == [], (
        f"final_timeout 内结束的执行单元不得被报成未完成：{report.as_dict()}"
    )
    assert report.clean is True
    assert handle.task.done() is True


async def test_a05_default_timeouts_are_bounded():
    """不许无限等待：默认预算必须是有限的正数。"""
    assert 0 < DEFAULT_SHUTDOWN_TIMEOUT < 120
    assert 0 <= DEFAULT_CANCEL_TIMEOUT < 60


async def test_a05_shutdown_is_idempotent(tmp_path):
    ctx, conn = _make_ctx(tmp_path, "idem")
    _shorten_shutdown(ctx)
    ctx.background.register("normal", _normal())

    first = await ctx.aclose()
    second = await ctx.aclose()

    assert _report_dict(first)["clean"] is True
    assert _report_dict(second)["clean"] is True
    conn.close()


# --------------------------------------------------------------------------
# 重启恢复：clean 的实例记录必须能在下一次启动被处理
# --------------------------------------------------------------------------


async def test_a05_clean_close_lets_next_instance_recover_queued_rows(tmp_path):
    ctx, conn = _make_ctx(tmp_path, "recover")
    _shorten_shutdown(ctx)
    ctx.turn_journal.accepted(turn_id="turn_left", message="上次没跑完的消息")

    report = await ctx.aclose()
    assert _report_dict(report)["clean"] is True

    next_ctx = AppContext(Settings(data_dir=tmp_path / "recover-next"), conn, EventBus())
    next_ctx.credentials._kr = MemoryKeyring()
    try:
        unfinished = {str(row["turn_id"]) for row in next_ctx.turn_journal.unfinished()}
        assert "turn_left" in unfinished, (
            "clean 退出的实例留下的 queued 行必须被下一个实例标成 interrupted 并进入入口"
        )
    finally:
        await next_ctx.aclose()
        conn.close()


async def test_a05_unclean_close_leaves_rows_for_ownership_based_recovery(tmp_path):
    """不 clean 时不写干净退出标记；记录仍然靠「心跳 + pid」判据恢复，不会丢。"""
    ctx, conn = _make_ctx(tmp_path, "unclean-recover")
    _shorten_shutdown(ctx)
    release = asyncio.Event()
    handle = ctx.background.register("stuck", _stuck(release))
    ctx.turn_journal.accepted(turn_id="turn_stuck", message="被卡住的那条消息")

    report = await ctx.aclose()
    assert _report_dict(report)["clean"] is False
    assert _exited_at(conn, ctx.instance_id) is None

    release.set()
    await asyncio.sleep(0.05)
    assert handle.task.done() is True

    # 受控判据：心跳过期 + pid 确认不存在 → 该实例算死，记录被恢复
    checker = InstanceRegistry(
        conn,
        "qio_verify_checker",
        pid=999999,
        heartbeat_ttl=0,
        pid_alive=lambda _pid: False,
    )
    recovered = TurnJournal(conn).interrupt_stale(checker)
    assert any(str(row["turn_id"]) == "turn_stuck" for row in recovered)

    row = conn.execute(
        "SELECT status, message FROM turn_journal WHERE turn_id = 'turn_stuck'"
    ).fetchone()
    assert row["status"] == "interrupted"
    assert row["message"] == "被卡住的那条消息", "恢复不得改消息原文"
    conn.close()


# --------------------------------------------------------------------------
# lifespan：只有 clean 才关数据库
# --------------------------------------------------------------------------


def _register_stuck_via_portal(client: TestClient, name: str = "stuck"):  # noqa: ANN202
    ctx = client.app.state.ctx
    _shorten_shutdown(ctx)
    release = asyncio.Event()

    async def _factory():
        return await _stuck(release)

    client.portal.call(lambda: ctx.background.register(name, _factory))
    return release


def test_a05_lifespan_keeps_db_open_when_close_is_unclean(tmp_path):
    conn = connect(tmp_path / "unclean.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn, close_db_on_shutdown=True)

    with TestClient(app) as client:
        release = _register_stuck_via_portal(client)

    release.set()

    try:
        conn.execute("SELECT 1").fetchone()
        still_open = True
    except sqlite3.ProgrammingError:
        still_open = False
    assert still_open, (
        "不 clean 的关闭不得关数据库：残留后台协程会打到已关的连接上"
        "（进程随退出走，下次启动按归属恢复）"
    )
    conn.close()


def test_a05_lifespan_closes_db_when_close_is_clean(tmp_path):
    conn = connect(tmp_path / "clean.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn, close_db_on_shutdown=True)

    with TestClient(app) as client:
        assert client.get("/api/runtime/state").status_code == 200

    try:
        conn.execute("SELECT 1").fetchone()
        still_open = True
    except sqlite3.ProgrammingError:
        still_open = False
    assert still_open is False, "clean 关闭时真实入口照旧关数据库（对照，基线也成立）"
