"""桌面壳退出收尾（R9 契约 §11 / §12：W5）。

一句话：**退出是一个有边界、可重复、能验证的过程**，不是「杀进程」。

为什么单独成模块
----------------

壳（Tauri）在真正退出前会调用 `POST /api/lifecycle/shutdown`。后端要在**有界**时间里：

1. 停止受理新工作（之后的写请求一律 503，见 `api/server.py` 的 session_guard）；
2. 保存状态（`AppContext.aclose()` 的 clean-exit 记录 / 数据库关闭由 lifespan 按同一份
   报告决定）；
3. 结束本实例管理的执行 / 子智能体 / 子进程（委托给 aclose 的既有收尾链：
   background → maintenance → turns → task_manager → heavy → 阻塞执行器；工具侧子进程
   由各工具的取消收尾 + Job Object「关句柄即收整棵树」兜底）；
4. **不永久取消有效待审批**：等待中的审批要留给重启恢复（契约 §5.1 / §11）。

这些决定不能散落在路由里 —— 它们必须只有一份、可被 lifespan 复用，并且**幂等**：
壳的退出流程允许重复调用（第二次不能把已经收尾的东西再收一遍，也不能谎报成功）。

诚实边界
--------

* `SHUTDOWN_GRACE_SECONDS` 是**路由等待**的预算，不是「后端一定在这么短时间内死掉」的
  承诺：超时后路由如实返回 `timeout: true`，后端继续跑完 `aclose()`，壳按自己的预算
  等 sidecar 退出，仍不退出才 `taskkill /PID <pid> /T /F` 兜底并核验 pid 消失。
* 本模块不按进程名杀任何东西，也不直接结束别的实例的进程；进程树清理在壳侧按 pid 做。
* 有效待审批的恢复是**兜底**：W1 的审批 v2 接管后，其 shutdown 语义应直接保留 waiting；
  这里只防止「关闭时取消在跑的 turn」把等待中的审批连带结算成 cancelled（base 行为）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

#: 退出收尾时路由等待 `aclose()` 的预算（秒）。契约初值 8.0：到点如实返回 timeout，
#: 由壳继续等 sidecar 退出；这个值不允许被「等更久」的调用方悄悄绕过。
SHUTDOWN_GRACE_SECONDS = 8.0

#: 关闭完成之后、请求退出进程之前给 HTTP 响应留的余量（秒）。
EXIT_AFTER_RESPONSE_DELAY_SECONDS = 0.25

#: 响应里名单最多列几个名字（再长也不把一行日志写成几百个 id）。
_MAX_NAMES = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _names(names: list[str]) -> str:
    shown = [str(name) for name in names[:_MAX_NAMES]]
    text = "、".join(shown)
    if len(names) > len(shown):
        text += f" 等共 {len(names)} 个"
    return text


def _report_payload(report: Any) -> dict[str, Any]:
    """把 `CloseReport`（或测试替身）翻译成响应里的可读事实。"""
    if report is None:
        return {
            "clean": False,
            "unfinished": [],
            "phases": [],
            "detail": "关闭没有返回报告：不能确认后台执行单元已经结束",
        }
    to_dict = getattr(report, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 - 报告翻译失败也要给出可读结论
            logger.warning("关闭报告翻译失败", exc_info=True)
    return {
        "clean": bool(getattr(report, "clean", False)),
        "unfinished": list(getattr(report, "unfinished", ()) or ()),
        "phases": list(getattr(report, "phases", ()) or ()),
        "detail": str(getattr(report, "detail", "") or ""),
    }


def _snapshot_waiting_approvals(ctx: Any) -> list[dict[str, str]]:
    """关闭前快照「真的有人在等」的审批（契约 §11：waiting 不取消，供重启恢复）。

    只记录最小事实（id / 到期时间 / 创建时间）：不复制 payload、不写日志内容，
    避免把审批里的业务描述再散落一份。
    """
    service = getattr(ctx, "approvals", None)
    pending = getattr(service, "pending", None)
    if not callable(pending):
        return []
    try:
        items = pending()
    except Exception:  # noqa: BLE001 - 快照失败不能挡住退出
        logger.warning("读取待审批快照失败（退出继续）", exc_info=True)
        return []
    out: list[dict[str, str]] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        approval_id = str(item.get("approval_id") or "")
        if not approval_id:
            continue
        out.append(
            {
                "approval_id": approval_id,
                "expires_at": str(item.get("expires_at") or ""),
                "created_at": str(item.get("created_at") or ""),
            }
        )
    return out


def _restore_waiting_approvals(
    ctx: Any,
    snapshot: list[dict[str, str]],
    *,
    shutdown_started_at: str,
) -> int:
    """把关闭期间被「取消 turn」连带结算成 cancelled 的有效审批恢复为 pending。

    只动三种行：关闭前**确实在等**、**还没过期**、且在关闭开始之后被结算成 cancelled 的。
    已批准 / 已拒绝 / 自然过期 / 用户在此前取消的一律不碰（不伪造授权，也不复活旧结局）。

    这是对 base 审批实现的兜底（`ApprovalService.request` 在 CancelledError 时
    `_settle(cancelled)`）。W1 的审批 v2 接管「不因退出取消 waiting」之后，这里通常
    一行都不会更新。字段只写既有的 `status` / `resolved_at`，不碰 W1 新增的状态列。
    """
    conn = getattr(ctx, "conn", None)
    if conn is None or not snapshot:
        return 0
    now = _now_iso()
    restored: list[str] = []
    try:
        from agent.storage.db import transaction

        with transaction(conn):
            for item in snapshot:
                approval_id = item.get("approval_id") or ""
                expires_at = item.get("expires_at") or ""
                if not approval_id or not expires_at or expires_at <= now:
                    continue  # 已经过期：恢复等待没有意义，也不该复活
                cur = conn.execute(
                    "UPDATE pending_approvals SET status = 'pending', resolved_at = NULL "
                    "WHERE approval_id = ? AND status = 'cancelled' "
                    "AND (resolved_at IS NULL OR resolved_at >= ?)",
                    (approval_id, shutdown_started_at),
                )
                if cur.rowcount == 1:
                    restored.append(approval_id)
    except Exception:  # noqa: BLE001 - 持久化失败不能挡住退出，但必须留证据
        logger.warning("恢复待审批状态失败（退出继续）", exc_info=True)
        return 0
    if restored:
        logger.info(
            "退出收尾：保留 %s 条有效待审批供重启恢复（%s）",
            len(restored),
            _names(restored),
        )
    return len(restored)


class ShutdownCoordinator:
    """退出闸门 + 一次性 aclose + 幂等响应。

    * :meth:`mark_requested` 必须**同步**执行（在路由的第一行），这样 middleware
      从这一刻起就能拒绝新工作；
    * :meth:`aclose_for_lifespan` 与路由共用同一个 `aclose()` 任务：谁先到谁建立，
      另一个等同一份结果 —— 后端生命周期收尾在整个进程里只执行一次；
    * 退出进程的动作通过 :meth:`set_exit_hook` 注入（入口在 `agent/main.py`）：
      驱动层知道怎么让 uvicorn 优雅停下；测试/工厂路径没有注入时只报告事实，不假装。
    """

    def __init__(self, *, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
        self.grace_seconds = float(grace_seconds)
        self._requested_at: float | None = None
        self._requested_iso: str | None = None
        self._close_task: asyncio.Task[Any] | None = None
        self._report: Any = None
        self._done = False
        self._error: str | None = None
        self._approvals_restored = 0
        self._exit_hook: Callable[[], None] | None = None
        self._exit_armed = False

    # -- 状态 --------------------------------------------------------------

    def is_shutting_down(self) -> bool:
        return self._requested_at is not None

    def mark_requested(self) -> bool:
        """第一次请求返回 True；重复请求返回 False（幂等，但不假装是新请求）。"""
        if self._requested_at is not None:
            return False
        self._requested_at = time.monotonic()
        self._requested_iso = _now_iso()
        logger.info("收到退出请求：停止受理新工作，开始有界收尾")
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "shutting_down": self.is_shutting_down(),
            "done": self._done,
            "error": self._error,
            "approvals_restored": self._approvals_restored,
            "grace_seconds": self.grace_seconds,
            "report": _report_payload(self._report) if self._done else None,
        }

    def set_exit_hook(self, hook: Callable[[], None] | None) -> None:
        self._exit_hook = hook

    # -- 一次性关闭 ---------------------------------------------------------

    def _ensure_close_task(self, ctx: Any) -> "asyncio.Task[Any]":
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._aclose_once(ctx))
        return self._close_task

    async def _aclose_once(self, ctx: Any) -> Any:
        snapshot = _snapshot_waiting_approvals(ctx)
        started_at = self._requested_iso or _now_iso()
        report = None
        try:
            report = await ctx.aclose()
        finally:
            # aclose 会取消在跑的 turn；base 审批会在取消时把等待项结算成 cancelled。
            # 退出不是「用户取消」：有效 waiting 必须留给重启恢复（契约 §11）。
            self._approvals_restored = _restore_waiting_approvals(
                ctx, snapshot, shutdown_started_at=started_at
            )
        self._report = report
        return report

    async def aclose_for_lifespan(self, ctx: Any) -> Any:
        """lifespan 的唯一关闭入口：与退出路由共享同一个任务（绝不重复收尾）。"""
        task = self._ensure_close_task(ctx)
        try:
            report = await task
        except Exception as exc:  # noqa: BLE001 - 交给 lifespan 按既有策略处理
            self._error = f"{type(exc).__name__}: {exc}"
            raise
        self._done = True
        return report

    # -- 退出动作 -----------------------------------------------------------

    def arm_exit(self, *, delay: float = EXIT_AFTER_RESPONSE_DELAY_SECONDS) -> None:
        """关闭完成后请求进程退出：**先让响应写完**，再让 uvicorn 优雅停下。"""
        if self._exit_armed:
            return
        self._exit_armed = True
        hook = self._exit_hook
        if hook is None:
            # 工厂/测试路径（没有注入 uvicorn Server）：不假装已经安排退出。
            logger.info("退出收尾完成：当前入口没有注入进程退出钩子（测试/工厂路径）")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            hook()
            return
        loop.call_later(max(0.0, float(delay)), hook)

    async def request_shutdown(self, ctx: Any) -> dict[str, Any]:
        """路由入口：标记闸门 → 有界等待 aclose → 返回可读事实（可重复调用）。"""
        first = self.mark_requested()
        task = self._ensure_close_task(ctx)
        try:
            done, _pending = await asyncio.wait({task}, timeout=self.grace_seconds)
        except Exception as exc:  # noqa: BLE001 - 等待本身失败也要如实报告
            self._error = f"{type(exc).__name__}: {exc}"
            done = set()
        if task in done:
            self._done = True
            if task.cancelled():
                self._error = self._error or "关闭任务被取消"
            elif task.exception() is not None:
                self._error = self._error or f"{type(task.exception()).__name__}: {task.exception()}"
            report = _report_payload(self._report)
            self.arm_exit()
            logger.info(
                "退出收尾完成：clean=%s（阶段 %s）", report.get("clean"), report.get("phases")
            )
            return {
                "done": self._error is None,
                "already": not first,
                "timeout": False,
                "grace_seconds": self.grace_seconds,
                "approvals_restored": self._approvals_restored,
                "report": report,
            }
        # 超时：acknowledge 事实，不取消 aclose（它还要把收尾做完，进程才能干净退出）。
        self.arm_exit()
        detail = (
            f"有界收尾在 {self.grace_seconds:g} 秒内没有完成：后端会继续收尾，"
            "壳按自己的预算等待 sidecar 退出，仍不退出才按 pid 结束进程树并核验"
        )
        logger.warning(detail)
        return {
            "done": False,
            "already": not first,
            "timeout": True,
            "grace_seconds": self.grace_seconds,
            "approvals_restored": self._approvals_restored,
            "report": {
                "clean": False,
                "unfinished": [],
                "phases": [],
                "detail": detail,
            },
        }


def build_lifecycle_router(ctx: Any, coordinator: ShutdownCoordinator) -> APIRouter:
    """退出 / 状态接口。

    * `POST /api/lifecycle/shutdown`：真正退出前的唯一收尾入口（幂等）；
    * `GET /api/lifecycle/state`：只读事实，给壳/诊断看「退出进行到哪一步」。
    """
    router = APIRouter()

    @router.get("/api/lifecycle/state")
    async def lifecycle_state() -> dict:
        return coordinator.snapshot()

    @router.post("/api/lifecycle/shutdown")
    async def lifecycle_shutdown(request: Request) -> JSONResponse:
        payload = await coordinator.request_shutdown(ctx)
        # 退出前最后一条响应：显式让连接关闭，uvicorn 才能在有界时间内完成 shutdown
        # （keep-alive 连接会让优雅退出一直等到客户端超时）。
        return JSONResponse(payload, headers={"Connection": "close"})

    return router
