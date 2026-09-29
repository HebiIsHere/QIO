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
    # -- 记忆候选的可解释量（知识条目为 None） ---------------------------
    #: 生效的排序策略（relevance / weighted / entity_card / rules-only）
    strategy: str | None = None
    #: 原始检索相关分（未被任何业务奖励修改）
    relevance: float | None = None
    #: 各附加因素的实际贡献（权重 × 因素值）
    factors: dict[str, float] = Field(default_factory=dict)
    #: 在排序入口里的名次（1 起）
    rank: int | None = None


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
    #: 本次生效的排序配置（策略 / 权重 / 候选池 / 返回上限）
    ranking: dict[str, Any] = Field(default_factory=dict)


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
