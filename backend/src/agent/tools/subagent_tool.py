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

    async def run(self, **kwargs: Any) -> ToolResult:
        ref = self.definition.credential_ref
        if not ref:
            return ToolResult(ok=False, error="subagent tool requires credential_ref")
        secret = self.credentials.get_secret(ref)
        if secret is None:
            return ToolResult(ok=False, error=f"referenced credential unavailable: {ref}")
        if self.adapter_factory is None:
            return ToolResult(ok=False, error="no adapter factory wired for subagent")
        adapter = await self.adapter_factory(ref, self.definition.model)
        if adapter is None:
            return ToolResult(ok=False, error="failed to build adapter for subagent")
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self.task_manager.submit(
            self.name,
            lambda: self._execute(task_id, adapter, kwargs),
            task_id=task_id,
        )
        if self.definition.sync:
            _, res = await self.task_manager.await_result(task_id, timeout=None)
            return res if res is not None else ToolResult(ok=False, error="subagent task failed")
        return ToolResult(
            ok=True,
            content=SUBAGENT_STARTED.format(task_id=task_id),
        )

    async def _execute(self, task_id: str, adapter, kwargs: dict) -> ToolResult:
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
            return ToolResult(ok=False, error="task_id required")
        notify = bool(kwargs.get("notify", False))
        timeout = int(kwargs.get("timeout", 120) or 120)
        if notify:
            if self.notify_handler is None:
                return ToolResult(ok=False, error="notify unsupported in this environment")
            if self.task_manager.register_notify(task_id, self.notify_handler):
                return ToolResult(
                    ok=True,
                    content=f"已注册通知：任务 {task_id} 完成时将自动唤起主 agent。",
                )
            # 已完成的直接取回
        status, res = await self.task_manager.await_result(task_id, timeout=timeout)
        if status == "not_found":
            return ToolResult(ok=False, error=f"task not found: {task_id}")
        if res is None:
            return ToolResult(
                ok=True,
                content=f"任务 {task_id} 仍在运行（{status}），可稍后再次 await_task 取回。",
            )
        if not res.ok:
            return ToolResult(ok=False, error=res.error or "task failed")
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
            return ToolResult(ok=False, error="task_id required")
        offset = max(0, int(kwargs.get("offset", 0) or 0))
        limit = max(1, min(int(kwargs.get("limit", READ_CHUNK_LIMIT) or READ_CHUNK_LIMIT), READ_CHUNK_LIMIT))
        record = self.task_manager.record_info(task_id)
        if record is None or record.result is None or not record.result.ok:
            return ToolResult(ok=False, error="task result unavailable")
        content = (
            record.full_content
            if record.full_content is not None
            else (record.result.content if record.result else "")
        ) or ""
        return ToolResult(ok=True, content=content[offset : offset + limit])
