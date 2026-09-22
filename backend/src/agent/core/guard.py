"""Runaway guard: detect repeated failing tool calls and escalate.

阈值（2026-09-22 按用户要求放宽，并改成"询问"而不是"直接终止"）：

* **同一个工具累计失败 15 次** → WARN（提醒模型换方法，执行照常）；
* **同一个工具累计失败 30 次** → HALT：拦下这次调用的结论并**询问用户是否继续**
  （由 AgentLoop 发起审批；批准＝该工具计数清零、本轮继续，拒绝＝本轮结束）。

为什么口径从"同一次调用（工具＋参数完全相同）"改成"同一个工具累计"：
模型反复用**略不同的参数**重试同一个坏工具时，按原口径永远攒不到阈值，反而
按参数分桶把计数摊薄了。按工具累计才是"这个工具在当前这轮里就是不行"的真实信号。

调用级阈值保留字段（call_warn / call_block）是为了兼容既有构造方式，取值与工具级一致，
因此实际生效的是工具级那一条线。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class GuardVerdict(str, Enum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


@dataclass
class RunawayGuard:
    call_warn: int = 15
    call_block: int = 30
    tool_warn: int = 15
    tool_halt: int = 30
    _call_fails: dict[str, int] = field(default_factory=dict)
    _tool_fails: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def _key(tool: str, arguments: dict) -> str:
        return tool + "::" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)

    def observe(self, tool: str, arguments: dict, ok: bool) -> GuardVerdict:
        if ok:
            return GuardVerdict.OK
        key = self._key(tool, arguments)
        self._call_fails[key] = self._call_fails.get(key, 0) + 1
        self._tool_fails[tool] = self._tool_fails.get(tool, 0) + 1
        if self._tool_fails[tool] >= self.tool_halt:
            return GuardVerdict.HALT
        if self._call_fails[key] >= self.call_block:
            return GuardVerdict.BLOCK
        if self._tool_fails[tool] >= self.tool_warn:
            return GuardVerdict.WARN
        if self._call_fails[key] >= self.call_warn:
            return GuardVerdict.WARN
        return GuardVerdict.OK

    # -- 询问之后的收敛 ------------------------------------------------

    def reset_tool(self, tool: str) -> None:
        """用户选择"继续"之后：把这个工具的累计失败清零，并清掉它的调用级计数。

        不清零的话，下一次失败立刻又触发同一个阈值，等于"继续"毫无意义。
        """
        self._tool_fails.pop(tool, None)
        prefix = tool + "::"
        for key in [k for k in self._call_fails if k.startswith(prefix)]:
            self._call_fails.pop(key, None)

    def failures_for(self, tool: str) -> int:
        """该工具当前累计失败次数（用于告警与审批载荷，避免文案里出现猜测的数字）。"""
        return int(self._tool_fails.get(tool, 0))
