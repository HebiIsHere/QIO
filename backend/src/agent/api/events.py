"""SSE event protocol (agreed event set).

Envelope: { "type": str, "id": str, "ts": str, "data": object }
Wire format: "event: <TYPE>\\ndata: <json>\\n\\n"
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class EventType(str, Enum):
    CAPABILITY = "CAPABILITY"
    FALLBACK = "FALLBACK"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_RESULT = "APPROVAL_RESULT"
    CREDENTIAL_STATUS = "CREDENTIAL_STATUS"
    SUBAGENT_STATUS = "SUBAGENT_STATUS"
    # 工具创建是一条流程、一张卡：这些事件让同一张卡原地推进
    # （提案 / 构建 / 测试 / 等待确认 / 注册 / 已创建），phase 见 tools/dev_tools.py
    TOOL_CREATE_STATUS = "TOOL_CREATE_STATUS"
    # 高影响知识候选：回答完成后在对话里自然确认（保存 / 修改 / 忽略）
    KNOWLEDGE_CANDIDATE = "KNOWLEDGE_CANDIDATE"
    USAGE = "USAGE"
    TURN_START = "TURN_START"
    TURN_END = "TURN_END"
    TURN_QUEUE = "TURN_QUEUE"
    TOOL_START = "TOOL_START"
    TOOL_END = "TOOL_END"
    # 模型自主决定的过程说明（announce / progress / warning / result）。
    # 它只承载"模型怎么表达"，工具事实仍走 TOOL_START / TOOL_END。
    NARRATIVE = "NARRATIVE"
    ASSISTANT = "ASSISTANT"
    ANCHOR = "ANCHOR"
    # 推测切换：只表示「可能属于另一个话题，等你确认」，Anchor 没变（见 spec 第 29~30 条）
    TOPIC_SWITCH_SUGGESTED = "TOPIC_SWITCH_SUGGESTED"
    WARNING = "WARNING"
    ERROR = "ERROR"
    # 事件流完整性受损时由总线发出：客户端据此重新拉取权威快照，
    # 而不是继续拿一份可能不完整的事件序列当作最新状态。
    RESYNC = "RESYNC"


class AgentEvent(BaseModel):
    type: EventType
    id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict = Field(default_factory=dict)


def make_event(event_type: EventType, data: dict | None = None) -> AgentEvent:
    return AgentEvent(type=event_type, data=data or {})


def sse_format(event: AgentEvent) -> str:
    # `id:` 是标准 SSE 的 replay cursor：断线重连时客户端带上最后一个已处理的
    # event_id，服务端只补发之后的事件（见 api/bus.py::stream）。
    return f"id: {event.id}\nevent: {event.type.value}\ndata: {event.model_dump_json()}\n\n"
