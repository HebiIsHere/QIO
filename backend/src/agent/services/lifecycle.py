"""关闭结果与「已停止」的**真实**判定（契约 A05）。

为什么单独一个模块
------------------

`services/background.py` 负责「把后台执行单元停下来并如实回报」；
但**关闭结果要传到哪里、由谁据此决定写不写干净退出、关不关数据库、
能不能对用户说「后端已停止」**，是另一件事。以前这些决定散落在调用点，
每处各写一遍 `if report.clean:`，很容易漏一处 —— 漏掉的那一处就会
「没确认结束也报成功」。

本模块把三件事收成一处、可被一行挂上：

1. :class:`CloseReport` —— `AppContext.aclose()` 的返回值（Lead 接线见下方备注）。
   `clean` 表示**所有后台执行单元都已确认结束**；`unfinished` 是「谁还没结束」，
   含「asyncio 句柄取消了但底层执行单元还在跑」这一类（它们不算干净）。
2. :func:`close_decision` —— 由 `clean` 推出的唯一决定：能不能写干净退出标记、
   能不能释放适配器、能不能关数据库。`clean=False` 时**三者全否**：
   * 不写干净退出：否则下次启动会以为「上次是干净退出的」，把仍在 running 的
     认领当成「进程崩了」以外的另一套语义（契约 C1 的判据会被污染）；
   * 不释放适配器：残留协程还会去调模型/HTTP，client 先关了只会制造二次故障；
   * 不关数据库：SQLite 的 WAL 崩溃安全，进程随退出走；下次启动按**归属**恢复。
   `app.py` / `api/server.py` 的接线就是照这三条做（W3 在回报里给逐行指令）。
3. :class:`StopVerdict` / :func:`backend_stop_verdict` —— 「更新前停止后端」这条
   路径的判据：**只有在真实探测确认不再响应（且后端自报没有未结束的后台工作）
   之后**才允许报告「后端已停止」；否则必须如实说「没确认」。

诚实边界（写在这里免得被误读）
------------------------------

* `clean=True` 只证明**本进程登记在册的后台执行单元**确认结束了，不证明
  外部世界（本地模型进程、被派生的子进程）已经没有残留；
* `clean=False` 时本模块**不做**任何补救：不能去 kill 协程、不能替调用方
  「等更久」。它只负责把真相说清楚，剩下的是上层选择（进程退出 / 下次启动恢复）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

from agent.services.background import (
    DEFAULT_CANCEL_TIMEOUT,
    DEFAULT_FINAL_TIMEOUT,
    DEFAULT_SHUTDOWN_TIMEOUT,
    BackgroundTasks,
    ShutdownReport,
)

logger = logging.getLogger(__name__)

# app.aclose() 级收尾阶段名。与 background 的阶段名分开命名空间：
# `phases` 里既能看出后台停在哪一档，也能看出上层据此做了什么决定。
PHASE_BACKGROUND_STOPPED = "background_stopped"
PHASE_BACKGROUND_UNFINISHED = "background_unfinished"
PHASE_CLEAN_EXIT_RECORDED = "clean_exit_recorded"
PHASE_CLEAN_EXIT_SKIPPED = "clean_exit_skipped"
PHASE_ADAPTERS_RELEASED = "adapters_released"
PHASE_ADAPTERS_KEPT = "adapters_kept"
PHASE_DB_CLOSED = "db_closed"
PHASE_DB_KEPT = "db_kept"

# 「更新前停止后端」的真实探测预算（秒）：发过结束动作之后等这么久。
DEFAULT_STOP_VERIFY_BUDGET_S = 5.0
# 两次探测之间的间隔（秒）。
DEFAULT_STOP_POLL_INTERVAL_S = 0.1

# detail 里列名字的条数上限（名单再长也不把一行日志写成几百个 id）。
_MAX_NAMES_IN_DETAIL = 8


def _redact(text: str) -> str:
    """detail 出口统一打码（与 trace 一个口径；打码不可用也不能让关闭流程崩）。"""
    try:
        from agent.trace.redact import redact_text

        return redact_text(text)
    except Exception:  # noqa: BLE001
        return text


def _names(names: Sequence[str]) -> str:
    shown = list(names[:_MAX_NAMES_IN_DETAIL])
    text = "、".join(shown)
    if len(names) > len(shown):
        text += f" 等共 {len(names)} 个"
    return text


@dataclass(frozen=True)
class CloseReport:
    """一次关闭的**真实**结果（`AppContext.aclose()` 的返回值）。

    * `clean`：所有后台执行单元是否都已**确认**结束；
    * `unfinished`：取消后仍未确认结束的名单（句柄未结束，或底层执行单元
      未确认结束 —— 「句柄取消了」从来不算「结束了」）；
    * `phases`：实际执行到的收尾阶段（便于日志与验收断言）；
    * `detail`：一句话原因（给用户/日志，不含密钥）。
    """

    clean: bool
    unfinished: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "unfinished": list(self.unfinished),
            "phases": list(self.phases),
            "detail": self.detail,
        }

    def with_phase(self, *phases: str) -> "CloseReport":
        """追加阶段名（返回新对象：frozen，改的是副本）。"""
        extra = tuple(str(phase) for phase in phases if str(phase))
        if not extra:
            return self
        return replace(self, phases=self.phases + extra)

    def with_detail(self, detail: str) -> "CloseReport":
        return replace(self, detail=_redact(str(detail)))


def close_report_from_shutdown(
    report: ShutdownReport | None,
    *,
    phases: Sequence[str] = (),
    detail: str = "",
) -> CloseReport:
    """把后台关闭报告翻译成 :class:`CloseReport`。

    `unfinished` 取**两个名单的并集**：句柄仍未结束的 + 底层执行单元未确认结束的。
    只报前者会把「句柄取消了、线程还在跑」说成干净 —— 那正是 A05 要修的谎。
    """
    extra = tuple(str(phase) for phase in phases if str(phase))
    if report is None:
        base_detail = detail or "没有后台注册表：本次关闭不涉及后台执行单元"
        return CloseReport(clean=True, unfinished=(), phases=extra, detail=_redact(base_detail))
    unfinished = tuple(sorted(set(report.unfinished) | set(report.still_running)))
    clean = bool(report.clean) and not unfinished
    if clean:
        base_detail = detail or report.detail or "所有后台执行单元均已确认结束"
    else:
        base_detail = detail or report.detail or (
            f"关闭时仍有后台工作未确认结束：{_names(unfinished)}"
        )
    phases_out = tuple(report.phases) + extra
    return CloseReport(clean=clean, unfinished=unfinished, phases=phases_out, detail=_redact(base_detail))


async def close_background(
    registry: BackgroundTasks | None,
    *,
    timeout: float | None = None,
    cancel_timeout: float | None = None,
    final_timeout: float | None = None,
) -> CloseReport:
    """关掉后台注册表并返回 :class:`CloseReport`（`app.aclose()` 的第一行）。

    没有注册表（老对象 / 测试替身）时返回 `clean=True` 的空报告：
    「没有后台工作」本身就是一个可以确认的结论，不是「判不出来」。
    """
    if registry is None:
        return close_report_from_shutdown(None)
    report = await registry.shutdown(
        DEFAULT_SHUTDOWN_TIMEOUT if timeout is None else timeout,
        DEFAULT_CANCEL_TIMEOUT if cancel_timeout is None else cancel_timeout,
        final_timeout=DEFAULT_FINAL_TIMEOUT if final_timeout is None else final_timeout,
    )
    return close_report_from_shutdown(report)


@dataclass(frozen=True)
class CloseDecision:
    """`clean` 之后**允许**做什么（唯一口径，aclose 与 lifespan 都问它）。"""

    record_clean_exit: bool
    release_adapters: bool
    close_database: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_clean_exit": self.record_clean_exit,
            "release_adapters": self.release_adapters,
            "close_database": self.close_database,
            "reason": self.reason,
        }


def close_decision(report: CloseReport) -> CloseDecision:
    """由关闭结果推出「写不写干净退出 / 释不释放适配器 / 关不关数据库」。

    `clean=False` 时三项全否，理由只有一个：**没确认结束就不能假装结束**。
    """
    if report.clean:
        return CloseDecision(
            record_clean_exit=True,
            release_adapters=True,
            close_database=True,
            reason="后台执行单元已全部确认结束",
        )
    return CloseDecision(
        record_clean_exit=False,
        release_adapters=False,
        close_database=False,
        reason=(
            "仍有后台执行单元未确认结束，本次关闭不写干净退出、不释放适配器、不关数据库"
            f"（{_names(report.unfinished)}）"
        ),
    )


def log_close_report(report: CloseReport, *, log: logging.Logger | None = None) -> None:
    """把关闭结果写进日志：干净是 info，不干净是**可行动的错误**。"""
    target = log or logger
    if report.clean:
        target.info("关闭已完成：%s（阶段 %s）", report.detail, "→".join(report.phases))
        return
    target.error(
        "关闭未确认完成：%s；未结束的后台执行单元：%s。"
        "数据库保持打开（SQLite WAL 崩溃安全），下次启动按归属恢复。",
        report.detail,
        _names(report.unfinished),
    )


@dataclass(frozen=True)
class StopVerdict:
    """「后端已停止」这一句话的**真实**依据。"""

    stopped: bool
    survivors: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped": self.stopped,
            "survivors": list(self.survivors),
            "detail": self.detail,
        }


def backend_stop_verdict(
    *,
    probe: Callable[[], bool],
    close_report: CloseReport | None = None,
    budget_s: float = DEFAULT_STOP_VERIFY_BUDGET_S,
    interval_s: float = DEFAULT_STOP_POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StopVerdict:
    """发过结束动作之后，**用真实探测**判断后端到底停没停。

    `probe()` 返回 True 表示**仍然可探测到**（端口还在响应 / 进程还在）。

    * 后端自报 `clean=False`（有未结束的后台执行单元）→ 直接判「未确认停止」，
      不再往下报成功；
    * 预算内探测不到 → `stopped=True`；
    * 预算用尽仍然可探测到 → `stopped=False` + `survivors`，
      调用方**不得**报告「后端已停止」（更新前停止后端的路径就靠这条）。

    `clock` / `sleep` 可注入：测试用假时钟，不真的等。
    """
    if close_report is not None and not close_report.clean:
        names = _names(close_report.unfinished)
        return StopVerdict(
            stopped=False,
            survivors=tuple(close_report.unfinished),
            detail=_redact(
                f"后端自报仍有未结束的后台执行单元（{names}），不能报告「后端已停止」"
            ),
        )
    budget = max(0.0, float(budget_s))
    interval = max(0.0, float(interval_s))
    deadline = clock() + budget
    while True:
        if not probe():
            return StopVerdict(
                stopped=True, detail=_redact("已确认后端停止：真实探测在预算内不再响应")
            )
        left = deadline - clock()
        if left <= 0:
            return StopVerdict(
                stopped=False,
                survivors=("backend",),
                detail=_redact(
                    "结束动作已发出，但真实探测显示后端仍在响应；未确认停止，"
                    "不得报告「后端已停止」"
                ),
            )
        sleep(min(interval, left) if interval > 0 else left)
