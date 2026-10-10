"""后台任务的统一登记与关闭（契约 C7 / M06）。

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
句柄由本模块持有。关闭走固定四步：

1. **先拒绝新建**（`register` 抛 `BackgroundClosed`）—— 关闭开始之后不允许再产生
   新的后台工作，调用方把「这一份工作还没做」如实留给持久层（派生任务仍在队列里，
   下次启动由恢复路径接手），而不是偷偷跑完；
2. **有界等待**：给在跑的任务 `timeout` 秒自然收尾（长任务不该无限拖住关闭）；
3. **取消**：超时仍未结束的一律取消，再给一个有界的取消确认窗口；
4. **确认结束**：返回报告（自然结束 / 取消 / 仍未结束的名单）；已结束的句柄移除，
   防重复调度与无主任务。

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

# 挂在 app 上的属性名（Lead 的 aclose 接线按这个名字取注册表）。
ATTRIBUTE_NAME = "background"

# 无法在 app 对象上写属性时的兜底（按对象身份存）。正常情况下不会用到。
_FALLBACK_REGISTRIES: dict[int, "BackgroundTasks"] = {}


class BackgroundClosed(RuntimeError):
    """关闭流程已开始：不再接受新的后台工作。"""


@dataclass(frozen=True)
class BackgroundHandle:
    """一个已登记的后台任务的句柄。"""

    name: str
    task: asyncio.Task

    @property
    def done(self) -> bool:
        return self.task.done()

    def cancel(self) -> bool:
        return self.task.cancel()


@dataclass
class ShutdownReport:
    """关闭结果（如实回报，不把「超时未结束」说成「已收尾」）。"""

    waited: int = 0  # 有界等待内自然结束的条数
    cancelled: int = 0  # 被取消的条数
    unfinished: list[str] = field(default_factory=list)  # 取消后仍未确认结束的名单

    @property
    def clean(self) -> bool:
        """是否确认所有后台任务都已结束。"""
        return not self.unfinished

    def as_dict(self) -> dict[str, Any]:
        return {
            "waited": self.waited,
            "cancelled": self.cancelled,
            "unfinished": list(self.unfinished),
            "clean": self.clean,
        }


class BackgroundTasks:
    """进程内后台协程的注册表（登记 / 查询 / 关闭）。"""

    def __init__(self, *, name: str = "background", loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.name = name
        self._loop = loop
        self._handles: dict[str, BackgroundHandle] = {}
        self._closed = False

    # -- 状态 -------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def active(self) -> list[str]:
        """还没结束的后台任务名（已结束的句柄会被移除，所以这也就是在跑的名单）。"""
        return sorted(name for name, handle in self._handles.items() if not handle.done)

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
    ) -> BackgroundHandle:
        """登记一个后台协程。

        `factory` 是零参可调用（返回 awaitable）或直接一个 awaitable。
        同名且**仍在跑**时不重复调度：返回已有句柄（`replace=True` 时替换）。

        关闭之后抛 :class:`BackgroundClosed` —— 这是「先拒绝新建」的落点，
        调用方不得在这里静默改成同步执行：派生工作必须留在持久队列里等恢复。
        """
        if self._closed:
            raise BackgroundClosed(f"{self.name}: 已进入关闭流程，拒绝新建后台任务 {name!r}")
        if not name:
            raise ValueError("后台任务必须有名字：关闭与去重都按名字判定")
        if not replace:
            existing = self.handle(name)
            if existing is not None:
                return existing

        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError as exc:  # 没有事件循环就没有「后台」
                raise RuntimeError("后台任务需要运行中的事件循环") from exc

        task = loop.create_task(self._invoke(factory), name=f"{self.name}:{name}")
        handle = BackgroundHandle(name=name, task=task)
        self._handles[name] = handle
        task.add_done_callback(lambda finished, key=name, owned=handle: self._retire(key, owned))
        return handle

    async def _invoke(self, factory: Callable[[], Awaitable[Any]] | Awaitable[Any]) -> Any:
        result = factory() if callable(factory) else factory
        if inspect.isawaitable(result):
            return await result
        return result

    def _retire(self, name: str, handle: BackgroundHandle) -> None:
        """任务结束：移除句柄，并消费异常（否则 asyncio 会报「异常从未取回」）。"""
        if self._handles.get(name) is not handle:
            return
        self._handles.pop(name, None)
        if handle.task.cancelled():
            return
        exc = handle.task.exception()
        if exc is not None:
            logger.warning("%s: 后台任务 %s 以异常结束：%s", self.name, name, type(exc).__name__)

    # -- 关闭 -------------------------------------------------------------

    async def shutdown(
        self,
        timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        *,
        cancel_timeout: float = DEFAULT_CANCEL_TIMEOUT,
    ) -> ShutdownReport:
        """停止受理 → 有界等待 → 取消 → 确认结束。

        幂等：可以重复调用；第二次调用不再等待/取消任何东西（句柄已清空）。
        """
        self._closed = True
        report = ShutdownReport()

        pending = [(name, handle) for name, handle in self._handles.items() if not handle.done]
        if pending:
            done, _ = await asyncio.wait(
                [handle.task for _, handle in pending], timeout=max(0.0, float(timeout))
            )
            report.waited = len(done)

        remaining = [(name, handle) for name, handle in pending if not handle.done]
        for _, handle in remaining:
            if handle.task.cancel():
                report.cancelled += 1
        if remaining:
            await asyncio.wait(
                [handle.task for _, handle in remaining],
                timeout=max(0.0, float(cancel_timeout)),
            )

        for name, handle in list(self._handles.items()):
            if handle.done:
                self._handles.pop(name, None)
        report.unfinished = self.active()
        if report.unfinished:
            # 不把「取消后还没结束」当成正常收尾：如实告警，交给上层决定（例如
            # 数据库关闭前的最后确认）。这种情况只可能来自吞掉取消的协程。
            logger.warning(
                "%s: 关闭后仍有 %d 个后台任务未确认结束：%s",
                self.name,
                len(report.unfinished),
                ", ".join(report.unfinished),
            )
        return report


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
