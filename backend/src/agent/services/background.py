"""后台任务的统一登记与关闭（契约 C7 / M06 / A05）。

缺陷背景
--------

以前派生工作（摘要 / 知识 / 实体）是 `loop.create_task(_run())` 直接扔进事件循环的：
**没有任何人持有句柄**。于是：

* `app.aclose()` 不认识这些任务 —— 它按「维护 → turn → 独立任务 → 执行器 →
  适配器 → 数据库」收尾，后台派生任务不在任何一环里。关闭后它们可能还在跑：
  写库发生在「数据库已经关了」之后（要么抛「数据库已关闭」被当成正常收尾，
  要么把状态写成半截）；
* 也没有去重：同一个片段被重复调度时会产生多条并发的同一份工作；
* 进程被杀时认领状态留在 `running`，只能等时限兜底。

修法
----

一个进程内一份注册表：所有后台派生协程都经 `register(name, factory)` 登记，
句柄由本模块持有。关闭走固定阶段：

1. **先拒绝新建**（`register` 抛 `BackgroundClosed`）—— 关闭开始之后不允许再产生
   新的后台工作，调用方把「这一份工作还没做」如实留给持久层（派生任务仍在队列里，
   下次启动由恢复路径接手），而不是偷偷跑完；
2. **有界等待**（`timeout`）：给在跑的任务一个自然收尾的窗口（长任务不该无限拖住关闭）；
3. **取消**：超时仍未确认结束的一律取消；
4. **取消确认窗口**（`cancel_timeout`）：配合取消的协程在这里结束；
5. **最后一档 grace**（`final_timeout`）：吞掉取消、延迟退出的协程在这里结束；
6. **如实回报**：`ShutdownReport` 里 `unfinished` 是「asyncio 句柄仍未结束」的名字，
   `still_running` 是「**底层执行单元**未被确认结束」的名字。已结束的句柄移除，
   防重复调度与无主任务。

A05 的核心纠正：**「句柄被取消了」不等于「底层执行单元结束了」**。

* `asyncio.Task.cancel()` 只是投递一个取消请求。只有当协程真的退出（含 `finally`
  清理）之后 `task.done()` 才为真 —— 这一点 `asyncio.wait()` 已经保证了。
* 但如果协程把工作**交给了别的执行单元**（线程池里的阻塞调用、子进程、外部副作用），
  取消协程**不会**停掉那个执行单元：它可能仍然在跑、仍然会在关闭之后写库。
  这种句柄必须声明 `unit_finished` 探针（或传一个 `asyncio.Event`），
  关闭只在**探针确认结束**之后才把它算作收尾；否则如实进 `still_running`。
* 反过来也不许偷懒：句柄结束了就当作底层结束了，同样不是「确认」。
  拿不到探针的句柄只能按「句柄即执行单元」判定，这一口径写在
  :meth:`BackgroundHandle.unit_confirmed` 里，并且在 `detail` 里说明。

取消不是「失败」：被取消的派生协程由调用方（`MemoryLifecycle._drain_batch`）把
认领**过代次校验后**放回队列（`derived_tasks.release`），因此关闭不会留下永久
`running`，迟到结果也会因为代次不匹配被丢弃。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# 自然收尾的有界等待预算（秒）。这是**上限**：关闭不该被一个卡住的后台任务
# 无限拖住。真实等待/取消结果由 ShutdownReport 如实回报，不假装已经收尾。
DEFAULT_SHUTDOWN_TIMEOUT = 10.0

# 取消之后确认结束的有界窗口（秒）。
DEFAULT_CANCEL_TIMEOUT = 2.0

# 取消确认窗口之后、最后一档 grace（秒）：专门接「吞掉取消、延迟退出」的协程。
# 必须**有界**：三档加起来就是关闭耗时的硬上限，不会无限等下去。
DEFAULT_FINAL_TIMEOUT = 2.0

# 句柄已经结束、只剩底层探针未确认时，重新探测的最小间隔（秒）。
# 探针是同步布尔查询，不该把 CPU 打满，也不该把 grace 窗口睡过去。
_UNIT_PROBE_INTERVAL = 0.005

# 挂在 app 上的属性名（Lead 的 aclose 接线按这个名字取注册表）。
ATTRIBUTE_NAME = "background"

# 收尾阶段名（如实记录「实际执行到了哪一档」，便于日志与验收断言）。
PHASE_REJECT_NEW = "reject_new"
PHASE_WAIT = "wait"
PHASE_CANCEL = "cancel"
PHASE_CANCEL_GRACE = "cancel_grace"
PHASE_FINAL_GRACE = "final_grace"
PHASE_VERIFY = "verify"

# 无法在 app 对象上写属性时的兜底（按对象身份存）。正常情况下不会用到。
_FALLBACK_REGISTRIES: dict[int, "BackgroundTasks"] = {}

# 名字进 detail / 日志时的条数上限：名单再长也不把一行日志写成几百个 id。
_MAX_NAMES_IN_DETAIL = 8


class BackgroundClosed(RuntimeError):
    """关闭流程已开始：不再接受新的后台工作。"""


def _as_unit_probe(source: Any) -> Callable[[], bool] | None:
    """把 `unit_finished` 的几种写法统一成「零参 → 是否已确认结束」的探针。

    接受：`None`（没有独立执行单元）、`asyncio.Event` 一类有 `is_set()` 的对象、
    普通零参可调用。其它一律报错 —— 静默当成 `None` 会把「底层还在跑」谎报成
    「已结束」，这正是 A05 要修的东西。
    """
    if source is None:
        return None
    is_set = getattr(source, "is_set", None)
    if callable(is_set):
        return lambda: bool(is_set())
    if callable(source):
        return lambda: bool(source())
    raise TypeError("unit_finished 必须是 None、带 is_set() 的对象或零参可调用")


@dataclass(frozen=True)
class BackgroundHandle:
    """一个已登记的后台任务的句柄。

    `task` 只是 asyncio 那一层的句柄；`unit_finished` 描述**底层执行单元**
    （线程 / 子进程 / 外部副作用）是否已经确认结束。两者含义不同：
    ``task.done() and unit_confirmed`` 才算这一份工作真的收尾了。
    """

    name: str
    task: asyncio.Task
    unit_finished: Callable[[], bool] | None = None

    @property
    def done(self) -> bool:
        """asyncio 句柄是否已经结束（不含底层执行单元确认）。"""
        return self.task.done()

    def cancel(self) -> bool:
        """投递取消请求（返回是否真的投递成功；已结束的句柄返回 False）。"""
        return self.task.cancel()

    def unit_confirmed(self) -> bool:
        """底层执行单元是否**已确认**结束。

        * 没声明探针：协程自己就是执行单元 —— 句柄结束即确认结束；
        * 声明了探针：**只认探针**。句柄被取消、甚至句柄已经结束，都不代表
          底层（线程 / 子进程 / 外部副作用）停了下来。
        * 探针本身抛异常：算「未确认」（不能把探针故障当成收尾成功）。
        """
        if self.unit_finished is None:
            return self.task.done()
        try:
            return bool(self.unit_finished())
        except Exception:  # noqa: BLE001 - 探针故障 = 未确认，不是已结束
            logger.warning("%s: 后台任务 %s 的底层探针报错", "background", self.name, exc_info=True)
            return False

    @property
    def settled(self) -> bool:
        """这一份工作是否真的收尾：句柄结束 **且** 底层执行单元已确认结束。"""
        return self.task.done() and self.unit_confirmed()

    @property
    def has_unit_probe(self) -> bool:
        return self.unit_finished is not None


@dataclass
class ShutdownReport:
    """关闭结果（如实回报，不把「超时未结束」说成「已收尾」）。"""

    waited: int = 0  # 有界等待内确认收尾的条数
    cancelled: int = 0  # 投递过取消的条数
    unfinished: list[str] = field(default_factory=list)  # 取消后 asyncio 句柄仍未结束的名单
    still_running: list[str] = field(default_factory=list)  # 底层执行单元未确认结束的名单
    phases: list[str] = field(default_factory=list)  # 实际执行到的收尾阶段
    detail: str = ""  # 一句话说明（不含密钥）

    @property
    def clean(self) -> bool:
        """是否确认所有后台执行单元都已结束。

        两个名单都必须为空：句柄没了但底层还在跑（`still_running`）同样**不干净** ——
        「asyncio 句柄取消了」从来不是「底层执行单元结束了」。
        """
        return not self.unfinished and not self.still_running

    def as_dict(self) -> dict[str, Any]:
        return {
            "waited": self.waited,
            "cancelled": self.cancelled,
            "unfinished": list(self.unfinished),
            "still_running": list(self.still_running),
            "phases": list(self.phases),
            "detail": self.detail,
            "clean": self.clean,
        }


def _redacted_names(names: list[str]) -> str:
    """名单进 detail / 日志前的统一出口：打码 + 截断（不出现密钥原文）。"""
    shown = list(names[:_MAX_NAMES_IN_DETAIL])
    text = ", ".join(shown)
    if len(names) > len(shown):
        text += f" 等共 {len(names)} 个"
    return text


def _detail_for(report: ShutdownReport, *, name: str) -> str:
    if report.clean:
        return f"{name}: 后台执行单元已全部确认结束（有界等待内结束 {report.waited} 个，取消 {report.cancelled} 个）"
    parts: list[str] = []
    if report.unfinished:
        parts.append(f"asyncio 句柄仍未结束：{_redacted_names(report.unfinished)}")
    if report.still_running:
        parts.append(f"底层执行单元未确认结束：{_redacted_names(report.still_running)}")
    text = f"{name}: 关闭时仍有后台工作未确认结束（{'；'.join(parts)}）"
    try:  # 日志/返回值路径统一过打码，和 trace 一个口径
        from agent.trace.redact import redact_text

        return redact_text(text)
    except Exception:  # noqa: BLE001 - 打码不可用也不能让关闭流程崩
        return text


class BackgroundTasks:
    """进程内后台协程的注册表（登记 / 查询 / 关闭）。"""

    def __init__(self, *, name: str = "background", loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.name = name
        self._loop = loop
        self._handles: dict[str, BackgroundHandle] = {}
        # 句柄已结束、但底层执行单元**未确认结束**的登记（「幽灵」）。
        # 这些必须留在账上：它们是「关闭之后还在写库」的唯一线索，
        # 不能因为 asyncio 句柄撤销了就假装从未存在。
        self._ghosts: list[BackgroundHandle] = []
        self._closed = False

    # -- 状态 -------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def active(self) -> list[str]:
        """还没结束的后台任务名（按 asyncio 句柄判定）。"""
        return sorted(name for name, handle in self._handles.items() if not handle.done)

    def unsettled(self) -> list[str]:
        """**未确认收尾**的后台任务名（句柄未结束，或底层执行单元未确认结束）。"""
        return sorted(
            {handle.name for handle in self._tracked() if not handle.settled}
        )

    def still_running(self) -> list[str]:
        """底层执行单元未确认结束的名单（可能句柄已经结束了）。"""
        return sorted({handle.name for handle in self._tracked() if not handle.unit_confirmed()})

    def _tracked(self) -> list[BackgroundHandle]:
        """还挂在账上的执行单元：在跑的句柄 + 未确认结束的幽灵（按对象身份去重）。"""
        seen: dict[int, BackgroundHandle] = {}
        for handle in list(self._handles.values()) + list(self._ghosts):
            seen.setdefault(id(handle), handle)
        return list(seen.values())

    def _prune_ghosts(self) -> None:
        """幽灵的底层执行单元确认结束之后才撤销登记。"""
        self._ghosts = [handle for handle in self._ghosts if not handle.unit_confirmed()]

    def handle(self, name: str) -> BackgroundHandle | None:
        handle = self._handles.get(name)
        if handle is None or handle.done:
            return None
        return handle

    # -- 登记 -------------------------------------------------------------

    def register(
        self,
        name: str,
        factory: Callable[[], Awaitable[Any]] | Awaitable[Any],
        *,
        replace: bool = False,
        unit_finished: Callable[[], bool] | Any | None = None,
    ) -> BackgroundHandle:
        """登记一个后台协程。

        `factory` 是零参可调用（返回 awaitable）或直接一个 awaitable。
        同名且**仍在跑**时不重复调度：返回已有句柄（`replace=True` 时替换）。

        `unit_finished` 声明**底层执行单元**是否已结束的探针（线程池阻塞调用、
        子进程、外部副作用这类）：取消协程不会停掉它，所以关闭只在探针确认之后
        才把这一份算作收尾。可以传 `asyncio.Event` 或零参可调用。

        关闭之后抛 :class:`BackgroundClosed` —— 这是「先拒绝新建」的落点，
        调用方不得在这里静默改成同步执行：派生工作必须留在持久队列里等恢复。
        """
        if self._closed:
            raise BackgroundClosed(f"{self.name}: 已进入关闭流程，拒绝新建后台任务 {name!r}")
        if not name:
            raise ValueError("后台任务必须有名字：关闭与去重都按名字判定")
        probe = _as_unit_probe(unit_finished)
        if not replace:
            existing = self.handle(name)
            if existing is not None:
                return existing
        self._prune_ghosts()

        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:  # 没有事件循环就没有「后台」
                raise RuntimeError("后台任务需要运行中的事件循环") from exc

        task = loop.create_task(self._invoke(factory), name=f"{self.name}:{name}")
        handle = BackgroundHandle(name=name, task=task, unit_finished=probe)
        self._handles[name] = handle
        task.add_done_callback(lambda finished, key=name, owned=handle: self._retire(key, owned))
        return handle

    async def _invoke(self, factory: Callable[[], Awaitable[Any]] | Awaitable[Any]) -> Any:
        result = factory() if callable(factory) else factory
        if inspect.isawaitable(result):
            return await result
        return result

    def _retire(self, name: str, handle: BackgroundHandle) -> None:
        """任务结束：消费异常（否则 asyncio 会报「异常从未取回」）并更新登记。

        **句柄结束不等于收尾**：声明了底层执行单元的句柄如果探针还没确认结束，
        它会被移到「幽灵」账上继续被追踪 —— 关闭时必须还能如实报出
        「这份工作其实还在跑」。只有 `settled` 的句柄才撤销登记。
        """
        if self._handles.get(name) is not handle:
            return
        self._handles.pop(name, None)
        if not handle.settled:
            # 句柄结束了但底层执行单元未确认结束：移到「幽灵」账上继续追踪 ——
            # 关闭时必须还能如实报出「这份工作其实还在跑」。
            self._ghosts.append(handle)
        if handle.task.cancelled():
            return
        exc = handle.task.exception()
        if exc is not None:
            logger.warning("%s: 后台任务 %s 以异常结束：%s", self.name, name, type(exc).__name__)

    # -- 关闭 -------------------------------------------------------------

    async def shutdown(
        self,
        timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        cancel_timeout: float = DEFAULT_CANCEL_TIMEOUT,
        *,
        final_timeout: float = DEFAULT_FINAL_TIMEOUT,
    ) -> ShutdownReport:
        """拒绝新建 → 有界等待 → 取消 → 取消确认 → final grace → 如实回报。

        三档等待都有硬上限（`timeout` / `cancel_timeout` / `final_timeout`），
        所以关闭耗时是有界的，**绝不为一个卡住的后台任务无限等待**。

        幂等：可以重复调用；第二次调用不再等待/取消任何东西（句柄已清空）。
        """
        self._closed = True
        report = ShutdownReport(phases=[PHASE_REJECT_NEW])

        # 进入关闭流程这一刻的账目快照（在跑的句柄 + 未确认结束的幽灵）。
        # 每个阶段都**重新判定 settled**：句柄结束 ≠ 底层执行单元结束，
        # 底层探针可能在句柄取消之后仍然为 False。
        pending = [handle for handle in self._tracked() if not handle.settled]

        await self._spend(phase=PHASE_WAIT, pending=pending, budget=timeout, report=report)
        report.waited = sum(1 for handle in pending if handle.settled)

        remaining = [handle for handle in pending if not handle.settled]
        if remaining:
            report.phases.append(PHASE_CANCEL)
            for handle in remaining:
                # 只对「asyncio 句柄还没结束」的投递取消。句柄已经结束、底层还在跑的，
                # 取消句柄没有意义（task.cancel() 返回 False），只能等探针确认。
                if not handle.done and handle.cancel():
                    report.cancelled += 1
            await self._spend(
                phase=PHASE_CANCEL_GRACE, pending=remaining, budget=cancel_timeout, report=report
            )
            still = [handle for handle in remaining if not handle.settled]
            if still:
                # 最后一档：吞掉取消、延迟退出的协程在这里结束；再不动就如实回报。
                await self._spend(
                    phase=PHASE_FINAL_GRACE, pending=still, budget=final_timeout, report=report
                )

        report.phases.append(PHASE_VERIFY)
        self._prune_ghosts()
        report.unfinished = sorted(
            {handle.name for handle in self._tracked() if not handle.done}
        )
        report.still_running = sorted(
            {handle.name for handle in self._tracked() if not handle.unit_confirmed()}
        )

        # 已结束**且底层已确认**的句柄移除（防重复调度与无主任务）。未确认的一律留着：
        # 下一次 shutdown 还要能如实报出来，不能靠「句柄撤销登记」假装收尾。
        for name, handle in list(self._handles.items()):
            if handle.settled:
                self._handles.pop(name, None)

        report.detail = _detail_for(report, name=self.name)
        if not report.clean:
            logger.warning("%s", report.detail)
        return report

    async def _spend(
        self,
        *,
        phase: str,
        pending: list[BackgroundHandle],
        budget: float,
        report: ShutdownReport,
    ) -> None:
        """在一个**有界**窗口里等 `pending` 收尾；窗口用尽即返回（不抛、不无限等）。

        阶段名只在「确实有东西要等」时记进 `phases`，这样 report 能区分
        「正常结束（没走到取消）」与「取消/延迟取消（走满了后面的档）」。
        """
        budget = max(0.0, float(budget))
        waiting = [handle for handle in pending if not handle.settled]
        if not waiting:
            return
        report.phases.append(phase)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        while True:
            if all(handle.settled for handle in waiting):
                return
            left = deadline - loop.time()
            if left <= 0:
                return
            alive = [handle.task for handle in waiting if not handle.done]
            if alive:
                # asyncio.wait 不新建 Task：句柄里那条协程自己结束才算 done。
                await asyncio.wait(alive, timeout=left)
            else:
                # 句柄都结束了，只剩底层探针未确认：按间隔重探，别把 grace 睡过去。
                await asyncio.sleep(min(_UNIT_PROBE_INTERVAL, left))


def registry_for(owner: Any, *, loop: asyncio.AbstractEventLoop | None = None) -> BackgroundTasks:
    """取（必要时创建并挂上）`owner` 的后台注册表。

    派生工作由编排器在 post_turn 里调度，但**关闭接线在 app.aclose**：两边要拿到
    同一份注册表。正常路径是 app 上有一个 `background` 属性（Lead 在
    `AppContext.__init__` 里建、在 `aclose` 里 shutdown）；这里做惰性兜底，
    保证编排器第一次调度时它一定存在。
    """
    existing = getattr(owner, ATTRIBUTE_NAME, None)
    if isinstance(existing, BackgroundTasks):
        return existing
    registry = BackgroundTasks(loop=loop)
    try:
        setattr(owner, ATTRIBUTE_NAME, registry)
        return registry
    except Exception:  # noqa: BLE001 - 不许写属性的对象（slots / 代理）走兜底存储
        key = id(owner)
        fallback = _FALLBACK_REGISTRIES.get(key)
        if fallback is None:
            fallback = registry
            _FALLBACK_REGISTRIES[key] = fallback
        return fallback


def forget_registry_for(owner: Any) -> None:
    """清掉兜底注册表（测试收尾用；正常路径不需要）。"""
    _FALLBACK_REGISTRIES.pop(id(owner), None)
