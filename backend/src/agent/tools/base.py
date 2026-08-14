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
    # 输出 JSON Schema（轻量子集：object/string/number/integer/boolean/array/
    # required/properties/items）。执行后校验失败会返回失败结果。
    output_schema: dict[str, Any] | None = None
    # 为 True 时可在同一轮 tool_calls 中与其他并发安全工具并行执行
    # （AgentLoop 以 max_parallel_tools 分组 asyncio.gather）；默认 False 串行。
    is_concurrency_safe: bool = False
    # 声明的服务依赖名列表；ToolRegistry.register 时经 ServiceRegistry.attach
    # 自动注入到实例属性（缺省忽略，不报错）。
    inject: list[str] = []

    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError

    # -- 呈现（可选）------------------------------------------------------
    # 返回 {"title"?, "status"?, "summary"?}；None = 使用默认模板。
    def present_call(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def present_result(self, result: ToolResult) -> dict[str, Any] | None:
        return None
