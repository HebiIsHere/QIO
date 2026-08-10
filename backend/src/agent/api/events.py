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
    MEMORY_INJECT = "MEMORY_INJECT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_RESULT = "APPROVAL_RESULT"
    CREDENTIAL_STATUS = "CREDENTIAL_STATUS"
    SUBAGENT_STATUS = "SUBAGENT_STATUS"
    USAGE = "USAGE"
    TURN_START = "TURN_START"
    TURN_END = "TURN_END"
    TOOL_START = "TOOL_START"
    TOOL_END = "TOOL_END"
    ASSISTANT = "ASSISTANT"
    WARNING = "WARNING"
    ERROR = "ERROR"


class AgentEvent(BaseModel):
    type: EventType
    id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict = Field(default_factory=dict)


def make_event(event_type: EventType, data: dict | None = None) -> AgentEvent:
    return AgentEvent(type=event_type, data=data or {})


def sse_format(event: AgentEvent) -> str:
    return f"event: {event.type.value}\ndata: {event.model_dump_json()}\n\n"
