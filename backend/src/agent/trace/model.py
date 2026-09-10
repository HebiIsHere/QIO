"""Trace data model — what we record to answer "why did this turn happen?\""""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ModelCallInfo(BaseModel):
    seq: int = 0
    provider: str | None = None
    adapter_mode: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    tool_calls: int = 0
    fallback: bool = False
    error: str | None = None


class ToolRunInfo(BaseModel):
    call_id: str
    tool: str
    args_preview: str = ""
    ok: bool = True
    error: str | None = None
    duration_ms: int = 0
    policy: str | None = None  # auto | approve | block | deny | halt
    result_preview: str = ""


class InjectionItem(BaseModel):
    surface: str
    item_id: str | None = None
    tokens: int = 0
    preview: str = ""
    reason: str | None = None


class TopicTrace(BaseModel):
    initial: str | None = None
    predictor_backend: str | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    suspected_new: bool = False
    final: str | None = None
    operation: str = "none"  # none | switch | create


class WriteTrace(BaseModel):
    messages: list[str] = Field(default_factory=list)
    fragments_closed: list[str] = Field(default_factory=list)
    summaries: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)


class InjectionTrace(BaseModel):
    items: list[InjectionItem] = Field(default_factory=list)
    total_tokens: int = 0
    budget: dict[str, Any] = Field(default_factory=dict)
    dropped: list[dict[str, Any]] = Field(default_factory=list)


class TurnTrace(BaseModel):
    turn_id: str
    status: str = "running"
    started_at: str
    ended_at: str | None = None
    duration_ms: int | None = None
    topic: TopicTrace = Field(default_factory=TopicTrace)
    injection: InjectionTrace = Field(default_factory=InjectionTrace)
    model_calls: list[ModelCallInfo] = Field(default_factory=list)
    tool_runs: list[ToolRunInfo] = Field(default_factory=list)
    writes: WriteTrace = Field(default_factory=WriteTrace)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    final_preview: str = ""
