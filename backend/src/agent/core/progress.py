"""进展判断：这一轮有没有真的往前走（按证据），没有就停下来。

为什么单独做这一层：

* `RunawayGuard` 只看**失败次数**：一个「成功但毫无信息增量」的调用
  （反复读同一个文件、反复查同一个状态）永远不会被它拦下；
* 它的阈值（15/30 次）是按「异常循环」设的，等它触发时轮次预算与 token 早就
  烧光了，用户最后看到的只是一句「预算用完」，不知道其实什么都没发生。

判定依据是**证据**，不是标签、也不是「可重试」这种自述：

* 指纹 = 工具名 + 规范化参数 + 结果（成功/失败 + 结果正文的摘要）；
* 连续 `repeat_limit` 次拿到完全相同的指纹 → 这一轮没有新信息，判为「无进展」；
* 只要出现新信息（换了调用，或同一调用换了结果），计数清零。

只说事实：暂停的原因是「这几次调用拿到的东西完全一样」，不臆测模型在想什么。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

DEFAULT_REPEAT_LIMIT = 3


def _digest(value: Any) -> str:
    """把任意结构压成稳定摘要（不保留原文，也不怕参数里有敏感值）。"""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ProgressTracker:
    """连续重复检测。每个 turn 一份，跨轮不共享。"""

    repeat_limit: int = DEFAULT_REPEAT_LIMIT
    repeats: int = field(default=0, init=False)
    _last: str | None = field(default=None, init=False)
    _last_tool: str = field(default="", init=False)

    def observe(
        self,
        *,
        tool: str,
        arguments: Mapping[str, Any] | None,
        ok: bool,
        result_text: str | None,
    ) -> str | None:
        """记一次工具调用；返回暂停原因（没有进展），否则 None。"""
        fingerprint = _digest(
            [
                str(tool or ""),
                dict(arguments or {}),
                bool(ok),
                _digest(str(result_text or "")),
            ]
        )
        if fingerprint == self._last:
            self.repeats += 1
        else:
            self._last = fingerprint
            self._last_tool = str(tool or "")
            self.repeats = 1
        if self.repeats >= self.repeat_limit:
            return (
                f"`{self._last_tool}` 连续 {self.repeats} 次给出完全相同的结果，"
                "这一轮没有新的进展"
            )
        return None

    def reset(self) -> None:
        """用户选择「继续」之后重新开始计数。

        不清零的话，下一次同样的重复会立刻又触发暂停 —— 那「继续」就毫无意义。
        """
        self.repeats = 0
        self._last = None
        self._last_tool = ""
