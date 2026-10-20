"""Tool interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolStatus(str, Enum):
    """工具结果状态（Phase 1 冻结枚举；字段由程序填写，不靠中文措辞推断）。

    * queued / running：已受理、尚未完成；
    * completed / partially_completed：本次动作完成 / 部分完成（后者不代表整个用户目标完成）；
    * awaiting_approval / awaiting_input：仍有等待；
    * conflict / failed / cancelled / unknown：不得表述为成功。
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    CONFLICT = "conflict"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# 结构化工具结果协议版本。None = 旧正文兼容结果；新工具/关键改造路径显式填 1。
TOOL_RESULT_PROTOCOL_VERSION = 1


@dataclass
class ToolResult:
    ok: bool
    content: str = ""
    error: str | None = None
    # 统一的失败语义（见 core/tool_feedback.py）：错误类别与「是否可重试」是给模型
    # 与界面共用的**事实字段**，不是提示词。缺省 None = 交给反馈层按文本推断。
    category: str | None = None
    recoverable: bool | None = None
    # 机器可读的事实（见 core/turn_facts.py）：工具把自己操作之后的真实状态报给主
    # 循环，用于「最终结论的事实校正」。它不是给用户看的正文，也不进模型消息。
    facts: dict[str, Any] | None = None
    # ---- Phase 1 结构化结果契约（docs/qio-refactor-contract.md §4）--------
    # 程序自有类型标识：protocol_version=1 表示结构化结果；None 表示旧正文兼容。
    # 结构化结果由 core/tool_feedback.py 统一序列化成 ToolEnvelope（JSON），
    # 其中 data 必须真正进入模型消息；不得只放进 facts。
    protocol_version: int | None = None
    # ToolStatus 的值；None 时由 ok / category 派生（旧工具兼容）。
    status: str | None = None
    # 稳定机器代码（例如 "ok" / "not_found" / "version_conflict"），不做中文匹配。
    code: str | None = None
    # 证据 / 结果载荷（JSON 可序列化）。结构化结果必须通过它把完整证据交给模型。
    data: dict[str, Any] | None = None
    # 显式的模型载荷：给出时优先序列化它（必须符合 ToolEnvelope 形状）。
    model_payload: dict[str, Any] | None = None


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
