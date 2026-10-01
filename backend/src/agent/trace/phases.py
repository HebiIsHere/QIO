"""Turn 阶段计时：把一轮的时间铺满成「阶段 + residual」。

为什么需要它（真实事故）：一次真实 turn 的 `duration_ms=51498`，其中模型调用只有
1688ms，其余 49.8 秒在 Trace 里**没有任何分区解释** —— 排查时只能靠猜。原因是
Trace 以前只记两种耗时：每次模型调用（`model_calls[].latency_ms`）与每次工具调用
（`tool_runs[].duration_ms`）。一轮里其它所有等待（凭据/能力探测、上下文装配与
检索、排队、审批等待、落库、收尾记忆处理、事件投递）都不可见。

本模块把时间轴**铺满**，而不是只打几个孤立的点：

* 顶层阶段（depth=1）按顺序铺时间轴；
* 两个顶层阶段之间的空档自动记成显式的 `other` 阶段（≥1ms 才记，微秒级碎屑留给
  `residual_ms`）；
* 嵌套阶段（depth>1）只作为父阶段的细分记录，**不重复计入合计** —— 这样并行工具
  之类的重叠时间不会把总账算多。

于是 `sum(顶层阶段) + residual ≈ 全程`：几十秒不可能再变成 unknown，它一定会落在
某个具名阶段或显式的 `other` 里。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

# 小于 1ms 的空档不单独记一段：它们会落进 residual_ms（仍然是显式的数字）。
MIN_SPAN_MS = 1
OTHER = "other"


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


@dataclass
class _OpenSpan:
    name: str
    detail: str | None
    depth: int
    started: float
    start_ms: int


@dataclass
class PhaseTimer:
    """一轮的线性阶段计时器（见模块 docstring）。

    不是线程安全的：一个 turn 的时间轴只由它自己推进（并行工具走 tool_wait 一个
    顶层外的阶段，不在并发分支里开阶段）。
    """

    clock: Callable[[], float] = time.perf_counter
    _t0: float = field(init=False)
    _cursor: float = field(init=False)
    _open: list[_OpenSpan] = field(default_factory=list, init=False)
    _spans: list[dict[str, Any]] = field(default_factory=list, init=False)
    _notes: dict[str, Any] = field(default_factory=dict, init=False)
    _after: list[dict[str, Any]] = field(default_factory=list, init=False)
    _stopped: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._t0 = self.clock()
        self._cursor = self._t0

    # -- recording --------------------------------------------------------

    @contextmanager
    def phase(self, name: str, detail: str | None = None) -> Iterator[None]:
        """记录一个阶段。可以嵌套；只有顶层阶段参与「铺满时间轴」的合计。"""
        if self._stopped:
            yield
            return
        started = self.clock()
        depth = len(self._open) + 1
        if depth == 1:
            self._record_gap(started)
        span = _OpenSpan(
            name=name,
            detail=detail,
            depth=depth,
            started=started,
            start_ms=_ms(started - self._t0),
        )
        self._open.append(span)
        try:
            yield
        finally:
            ended = self.clock()
            if self._open and self._open[-1] is span:
                self._open.pop()
            elif span in self._open:
                # 乱序退出（并发误用）：不把时间轴算坏，也不抛异常
                self._open.remove(span)
            else:  # pragma: no cover - stop() 已经收口过这一段
                ended = started
            self._spans.append(
                {
                    "name": span.name,
                    "ms": max(0, _ms(ended - span.started)),
                    "depth": span.depth,
                    "detail": span.detail,
                    "start_ms": span.start_ms,
                }
            )
            if depth == 1:
                self._cursor = ended

    def note(self, key: str, value: Any) -> None:
        """记一个与时间轴无关的事实（例如排队等待时长）。"""
        self._notes[key] = value

    def after_turn(self, name: str, ms: int, detail: str | None = None) -> None:
        """记一段**turn 结束之后**仍在跑的工作（派生摘要 / 知识抽取）。

        它刻意不计入 duration_ms 与顶层阶段合计：那些任务在后台跑，不阻塞这一轮
        的终态，把它们算进去反而会让「这一轮用了多久」说谎。单列出来是为了让
        「收尾还花了多少时间」看得见。
        """
        self._after.append({"name": name, "ms": max(0, int(ms)), "detail": detail})

    def stop(self) -> None:
        """收口：把还开着的阶段按「到此为止」结束，并补上最后一段空档。

        幂等：收口之后再 phase() 不再计时（只当空上下文）。
        """
        if self._stopped:
            return
        now = self.clock()
        had_open = bool(self._open)
        while self._open:
            span = self._open.pop()  # 内层先结束（异常/强制收口路径）
            self._spans.append(
                {
                    "name": span.name,
                    "ms": max(0, _ms(now - span.started)),
                    "depth": span.depth,
                    "detail": span.detail,
                    "start_ms": span.start_ms,
                }
            )
        if had_open:
            # 收口时还开着的顶层阶段已经覆盖了「游标 → now」这一段：
            # 再补一段 other 就会把它重复计一次（实测多算 17ms）。
            self._cursor = now
        else:
            self._record_gap(now)
        self._stopped = True

    # -- output -----------------------------------------------------------

    def payload(self, *, duration_ms: int | None = None) -> dict[str, Any]:
        """给存储层的阶段账本。

        `residual_ms` 优先按落库的 `duration_ms` 计算（它才是权威的「这一轮多久」），
        否则退回到计时器自己覆盖到的时间段。
        """
        if not self._stopped:
            self.stop()
        spans = sorted(self._spans, key=lambda s: (s["start_ms"], s["depth"]))
        sum_ms = sum(s["ms"] for s in spans if s["depth"] == 1)
        total_ms = max(0, _ms(self._cursor - self._t0))
        if duration_ms is None:
            residual = max(0, total_ms - sum_ms)
        else:
            residual = max(0, int(duration_ms) - sum_ms)
        return {
            "version": 1,
            "total_ms": total_ms,
            "sum_ms": sum_ms,
            "residual_ms": residual,
            "notes": dict(self._notes),
            "spans": spans,
            # turn 结束之后才发生的工作（明确排除在 duration_ms 之外）
            "after_turn": [dict(item) for item in self._after],
        }

    # -- internals --------------------------------------------------------

    def _record_gap(self, ts: float) -> None:
        """把游标到 ts 的空档记成显式的 other 阶段（不解释 = 说谎）。"""
        gap = _ms(ts - self._cursor)
        if gap >= MIN_SPAN_MS:
            self._spans.append(
                {
                    "name": OTHER,
                    "ms": gap,
                    "depth": 1,
                    "detail": None,
                    "start_ms": _ms(self._cursor - self._t0),
                }
            )
        self._cursor = ts
