"""Subagent runtime tool: standalone loop with own model/key.

sync=true waits for completion; sync=false submits a background task via
the TaskManager and returns a pending handle. Results are never truncated:
the subagent is prompt-constrained (output_limit_chars) and over-limit
results are served pointer-style via read_task_result(task_id, offset, limit).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable

from agent.prompts import (
    SUBAGENT_PROMPT,
    SUBAGENT_RESULT_LONG,
    SUBAGENT_STARTED,
    TOOL_AWAIT_TASK_DESC,
    TOOL_READ_TASK_RESULT_DESC,
)
from agent.tools.base import Tool, ToolResult
from agent.tools.memory_search import MemorySearchTool
from agent.tools.registry import ToolRegistry
from agent.tools.spec import SubagentBudget, ToolDefinition

DEFAULT_RESULT_PREVIEW_CHARS = 500
READ_CHUNK_LIMIT = 4000


class SubagentTool(Tool):
    def __init__(
        self,
        definition: ToolDefinition,
        *,
        credentials,
        task_manager,
        retriever=None,
        adapter_factory: Callable[[str, str | None], Awaitable[Any]] | None = None,
        bus=None,
        toolset: list[str] | None = None,
        trace_store=None,
    ) -> None:
        self.definition = definition
        self.name = definition.name
        self.description = definition.description
        self.parameters = definition.parameters
        self.credentials = credentials
        self.task_manager = task_manager
        self.retriever = retriever
        self.adapter_factory = adapter_factory
        self.bus = bus
        self.toolset = toolset or ["memory_search"]
        self.trace_store = trace_store

    async def run(self, **kwargs: Any) -> ToolResult:
        if self.adapter_factory is None:
            return ToolResult(ok=False, error="子任务没有可用的适配器工厂")

        # 主 agent 只能在 subagent tag 里选；不指定则自动挑；没有则静默用 main-loop。
        candidates = self.credentials.list_tagged("subagent")
        requested = str(kwargs.get("credential_key") or "").strip() or None
        if requested:
            if not any(c["id"] == requested for c in candidates):
                return ToolResult(ok=False, error="credential_key 不在 subagent 用途内")
            chosen = requested
        elif candidates:
            chosen = candidates[0]["id"]
        else:
            default = self.credentials.get_default_meta()
            if default is None:
                return ToolResult(
                    ok=False,
                    error="子任务没有专属凭据，也没有可回落的主循环凭据",
                )
            chosen = default["id"]

        meta = self.credentials.get_metadata(chosen)
        model = (meta.get("default_model") if meta else None) or self.definition.model
        adapter = await self.adapter_factory(chosen, model)
        if adapter is None:
            return ToolResult(ok=False, error="为子任务构建适配器失败")
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self.task_manager.submit(
            self.name,
            lambda: self._execute(task_id, adapter, kwargs),
            task_id=task_id,
        )
        if self.definition.sync:
            _, res = await self.task_manager.await_result(task_id, timeout=None)
            return res if res is not None else ToolResult(ok=False, error="子任务执行失败")
        return ToolResult(
            ok=True,
            content=SUBAGENT_STARTED.format(task_id=task_id),
        )

    async def _execute(self, task_id: str, adapter, kwargs: dict) -> ToolResult:
        tid = f"subagent:{task_id}"
        tracer = None
        if self.trace_store is not None:
            from agent.trace.recorder import TurnTracer

            tracer = TurnTracer(self.trace_store, tid)
            self.trace_store.begin(tid)
        try:
            result = await self._run_subagent(task_id, adapter, kwargs, tid, tracer)
        except Exception:
            if self.trace_store is not None:
                self.trace_store.finish(tid, "failed")
            raise
        if self.trace_store is not None:
            self.trace_store.finish(
                tid, "done", final_preview=result.content or ""
            )
        return result

    async def _run_subagent(self, task_id: str, adapter, kwargs: dict, tid: str, tracer):
        from agent.core.loop import AgentLoop

        budget = self.definition.subagent_budget or SubagentBudget()
        sub_registry = ToolRegistry()
        if self.retriever is not None and "memory_search" in self.toolset:
            sub_registry.register(MemorySearchTool(self.retriever))
        prompt = SUBAGENT_PROMPT.format(
            description=self.definition.description,
            kwargs=json.dumps(kwargs, ensure_ascii=False),
            limit=budget.output_limit_chars,
        )
        loop = AgentLoop(
            adapter,
            sub_registry,
            self.bus,
            max_iterations=budget.max_iterations,
            token_budget=budget.max_tokens,
            turn_id=tid,
            trace=tracer,
        )
        result = await loop.run(prompt)
        record = self.task_manager.record_info(task_id)
        if record is not None:
            record.iterations = result.iterations_used
            record.tokens = result.tokens_used
            record.tool_calls = result.tool_calls_made
        content = result.final_content or ""
        if len(content) <= budget.output_limit_chars:
            return ToolResult(ok=True, content=content)
        record = self.task_manager.record_info(task_id)
        if record is not None:
            record.full_content = content
        preview = content[:DEFAULT_RESULT_PREVIEW_CHARS]
        return ToolResult(
            ok=True,
            content=SUBAGENT_RESULT_LONG.format(
                length=len(content), preview=preview, task_id=task_id
            ),
        )


class AwaitTaskTool(Tool):
    name = "await_task"
    description = TOOL_AWAIT_TASK_DESC
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "子任务 id"},
            "notify": {"type": "boolean", "description": "是否注册完成唤起，默认 false"},
            "timeout": {"type": "integer", "description": "等待秒数，默认 120"},
        },
        "required": ["task_id"],
    }

    def __init__(self, task_manager, notify_handler=None) -> None:
        self.task_manager = task_manager
        self.notify_handler = notify_handler

    async def run(self, **kwargs: Any) -> ToolResult:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(ok=False, error="task_id 必填")
        notify = bool(kwargs.get("notify", False))
        timeout = int(kwargs.get("timeout", 120) or 120)
        if notify:
            if self.notify_handler is None:
                return ToolResult(ok=False, error="当前环境不支持完成通知")
            if self.task_manager.register_notify(task_id, self.notify_handler):
                return ToolResult(
                    ok=True,
                    content=f"已注册通知：任务 {task_id} 完成时将自动唤起主 agent。",
                )
            # 已完成的直接取回
        status, res = await self.task_manager.await_result(task_id, timeout=timeout)
        if status == "not_found":
            return ToolResult(ok=False, error=f"找不到子任务：{task_id}")
        if res is None:
            return ToolResult(
                ok=True,
                content=f"任务 {task_id} 仍在运行（{status}），可稍后再次 await_task 取回。",
            )
        if not res.ok:
            return ToolResult(ok=False, error=res.error or "子任务失败")
        return ToolResult(ok=True, content=res.content)


class ReadTaskResultTool(Tool):
    name = "read_task_result"
    description = TOOL_READ_TASK_RESULT_DESC
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "子任务 id"},
            "offset": {"type": "integer", "description": "起始位置，默认 0"},
            "limit": {"type": "integer", "description": "读取长度，默认 4000，上限 4000"},
        },
        "required": ["task_id"],
    }

    def __init__(self, task_manager) -> None:
        self.task_manager = task_manager

    async def run(self, **kwargs: Any) -> ToolResult:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(ok=False, error="task_id 必填")
        offset = max(0, int(kwargs.get("offset", 0) or 0))
        limit = max(1, min(int(kwargs.get("limit", READ_CHUNK_LIMIT) or READ_CHUNK_LIMIT), READ_CHUNK_LIMIT))
        record = self.task_manager.record_info(task_id)
        if record is None or record.result is None or not record.result.ok:
            return ToolResult(ok=False, error="子任务结果暂不可用")
        content = (
            record.full_content
            if record.full_content is not None
            else (record.result.content if record.result else "")
        ) or ""
        return ToolResult(ok=True, content=content[offset : offset + limit])
