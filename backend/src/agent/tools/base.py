"""Tool interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    error: str | None = None


class Tool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]

    # -- 现代化扩展（均有默认值，向后兼容） --------------------------------
    # 单次执行超时（毫秒）；None 表示不超时。由执行管线以 asyncio.wait_for 实施。
    timeout_ms: int | None = None
    # 为 True 时，执行前会先发起用户审批（approval policy，pre 阶段短路）。
    requires_approval: bool = False

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError
