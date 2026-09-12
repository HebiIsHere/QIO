"""Application context: wires storage, credentials, adapters, tools, loop.

Built once per process; the HTTP layer pulls what it needs from it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from agent.adapters.anthropic import AnthropicAdapter, is_anthropic_endpoint, probe_anthropic
from agent.adapters.base import AdapterMode, BaseAdapter
from agent.adapters.model_context import ContextLengthRegistry
from agent.adapters.native import NativeAdapter
from agent.adapters.probe import ProbeCache, probe_adapter
from agent.adapters.text import TextAdapter
from agent.config import Settings
from agent.core.guard import RunawayGuard
from agent.credentials.policy import CredentialPolicy, CredentialRef
from agent.credentials.store import CredentialStore
from agent.graph.anchors import AnchorService
from agent.graph.topics import TopicService
from agent.memory.fragment import FragmentManager
from agent.memory.index import IndexBuilder
from agent.memory.ingest import MemoryWriter
from agent.selector.selector import Selector
from agent.storage.settings import SettingsStore
from agent.prompts import (
    NOTIFY_SUBTASK_DONE,
)
from agent.services.injection import InjectionPayload
from agent.services.retrieval import Retriever
from agent.tools.builtin import EchoTool, NowTool

if TYPE_CHECKING:  # pragma: no cover - avoids api->app cycle at import time
    from agent.api.bus import EventBus
from agent.tools.memory_search import MemorySearchTool
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAIN_LOOP_TAG = "main-loop"
MAIN_LOOP_USAGE_TAGS = ["main-loop", "chat", "code", "vision", "research"]
BUDGET_RATIO = 0.25
DEFAULT_FRAGMENT_MAX_MESSAGES = 10


class AppContext:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bus: EventBus) -> None:
        self.settings = settings
        self.conn = conn
        self.bus = bus
        self.credentials = CredentialStore(conn)
        self.settings_store = SettingsStore(conn)
        from agent.trace.store import TraceStore

        self.trace_store = TraceStore(
            conn, enabled=self.settings_store.get("trace.enabled", "true") != "false"
        )
        self.policy = CredentialPolicy(self.credentials)
        self.probe_cache = ProbeCache()
        self.context_registry = ContextLengthRegistry()
        self.fragments = FragmentManager(conn)
        self.memory = MemoryWriter(conn, self.fragments)
        self.index_builder = IndexBuilder(conn)
        self.topics = TopicService(conn)
        from agent.selector.bm25 import BM25Backend
        from agent.services.predict import TopicPredictor

        self.embedding = self._build_embedding_backend()
        self.selector = Selector(
            recall=(
                self.embedding
                if self.embedding is not None and self.embedding.available()
                else None
            ),
            fallback_recall=BM25Backend(),
        )
        self.retriever = Retriever(self.selector, self.topics, conn=conn)
        from agent.services.context import ContextAssembler

        self.context_assembler = ContextAssembler(
            conn,
            fragments=self.fragments,
            topics=self.topics,
            retriever=self.retriever,
            context_registry=self.context_registry,
            budget_ratio=BUDGET_RATIO,
        )
        from agent.services.memory_lifecycle import MemoryLifecycle

        self.memory_lifecycle = MemoryLifecycle(
            conn,
            fragments=self.fragments,
            topics=self.topics,
            index_builder=self.index_builder,
            embedding=self.embedding,
            user_root_id=self._user_root_id,
            refresh_selector=self._refresh_selector,
        )
        from agent.services.turn_orchestrator import TurnOrchestrator

        self.turn_orchestrator = TurnOrchestrator(self)
        self.predictor = TopicPredictor(conn, self.embedding, self.topics)
        from agent.tools.approval import ApprovalService

        self.approvals = ApprovalService(bus)
        from agent.tools.services import ServiceRegistry

        self.services = ServiceRegistry()
        self.services.register("retriever", self.retriever)
        self.services.register("predictor", self.predictor)
        self.services.register("approvals", self.approvals)
        self.services.register("embedding", self.embedding)
        from agent.services.search import SearchService

        self.search_service = SearchService(
            searxng_url=self.settings_store.get("search.searxng_url") or None,
            bocha_api_key=self.settings_store.get("search.bocha_api_key") or None,
        )
        self.services.register("search_service", self.search_service)
        self.registry = ToolRegistry(approvals=self.approvals, services=self.services)
        # agent 切换/创建话题后，实时把新锚点广播给前端（ANCHOR SSE 事件）
        self.registry.register_policy("tool/result", self._on_tool_anchor_result)
        self.registry.register(EchoTool())
        self.registry.register(NowTool())
        self.registry.register(MemorySearchTool(self.retriever))
        from agent.tools.web_search import WebSearchTool
        from agent.tools.web_fetch import WebFetchTool

        self.registry.register(WebSearchTool(self.search_service))
        self.registry.register(WebFetchTool())
        # 电脑操控：共享沙箱 + 文件系统/命令/进程工具（分级授权，走 ComputerSandbox）
        from agent.services.computer import ComputerSandbox, DEFAULT_MODE
        from agent.tools.fs_tools import (
            FsReadTool,
            FsWriteTool,
            FsPatchTool,
            FsListTool,
            FsFindTool,
            FsInfoTool,
        )
        from agent.tools.cmd_tools import RunCmdTool, SysInfoTool, ProcListTool, ProcKillTool

        def _computer_root() -> str:
            return self.settings_store.get("computer.root_dir", "") or str(
                self.settings.data_dir / "workspace"
            )

        def _computer_mode() -> str:
            return self.settings_store.get("computer.permission_mode", DEFAULT_MODE) or DEFAULT_MODE

        self.computer = ComputerSandbox(resolve_root=_computer_root, permission_mode=_computer_mode)
        self.services.register("computer", self.computer)
        for _tool in (
            FsReadTool(),
            FsWriteTool(),
            FsPatchTool(),
            FsListTool(),
            FsFindTool(),
            FsInfoTool(),
            RunCmdTool(),
            SysInfoTool(),
            ProcListTool(),
            ProcKillTool(),
        ):
            self.registry.register(_tool)
        from agent.tools.topic_tools import CreateTopicTool, SwitchTopicTool

        self.registry.register(SwitchTopicTool(conn))
        self.registry.register(
            CreateTopicTool(conn, approvals=self.approvals, predictor=self.predictor)
        )
        from agent.tools.subagent_tool import AwaitTaskTool, ReadTaskResultTool
        from agent.tools.task_manager import TaskManager

        self.task_manager = TaskManager(bus)
        self.services.register("task_manager", self.task_manager)
        self.registry.register(
            AwaitTaskTool(self.task_manager, notify_handler=self._handle_subagent_notify)
        )
        self.registry.register(ReadTaskResultTool(self.task_manager))
        from agent.tools.approval import ApprovalService
        from agent.tools.dev_tools import (
            CreateToolTool,
            DevListFilesTool,
            DevReadFileTool,
            DevRunTestsTool,
            DevSubmitTool,
            DevWriteFileTool,
        )
        from agent.tools.dev_workspace import DevWorkspace

        self.dev_workspaces = DevWorkspace(settings.data_dir / "dev-workspaces")
        self.registry.register(CreateToolTool(self.dev_workspaces))
        self.registry.register(DevListFilesTool(self.dev_workspaces))
        self.registry.register(DevWriteFileTool(self.dev_workspaces))
        self.registry.register(DevReadFileTool(self.dev_workspaces))
        self.registry.register(DevRunTestsTool(self.dev_workspaces))
        self.registry.register(
            DevSubmitTool(
                self.dev_workspaces,
                lifecycle_builder=self._build_tool_lifecycle,
            )
        )
        from agent.tools.entity_tools import CorrectEntityTool

        self.registry.register(CorrectEntityTool(conn))
        from agent.tools.knowledge_correction import CorrectKnowledgeTool

        # turn runtime boundary：进程级服务在此，单轮状态在 TurnContext
        from agent.core.turn import TurnManager

        self.turns = TurnManager()
        self.turns.set_runner(self._execute_turn)
        self.turns.set_publisher(self._publish_turn_queue)
        self.registry.register(
            CorrectKnowledgeTool(conn, snapshot_provider=self._knowledge_snapshot_provider)
        )
        from agent.storage.tool_store import ToolStore

        self.tool_store = ToolStore(conn)
        self.services.register("tool_store", self.tool_store)
        self._restore_tools()
        self._notify_turn = False
        from agent.services.maintenance import MaintenanceScheduler
        from agent.services.tool_router import ToolRouter

        self.tool_router = ToolRouter(embedding=self.embedding)
        self.maintenance = MaintenanceScheduler(self)
        self._refresh_selector()

    # -- anchor 事件广播 --------------------------------------------------

    async def _on_tool_anchor_result(self, data: dict) -> None:
        """switch_topic / create_topic 成功后广播新锚点（tool/result 策略）。"""
        tool_name = data.get("tool")
        result = data.get("result")
        if tool_name not in ("switch_topic", "create_topic"):
            return
        if result is None or not getattr(result, "ok", False):
            return
        await self._publish_anchor_event()

    async def _publish_anchor_event(self) -> None:
        """把当前 active 锚点（话题 + 片段）以 ANCHOR 事件推给前端。"""
        from agent.api.events import EventType, make_event

        anchor = AnchorService(self.conn).get_active()
        if anchor is None or not anchor.topic_id:
            return
        node = self.topics.nodes.get_topic(anchor.topic_id)
        topic_name = node.name if node is not None else anchor.topic_id
        fragment_title = None
        if anchor.fragment_id:
            frag = self.fragments.get(anchor.fragment_id)
            fragment_title = frag.title if frag is not None else None
        await self.bus.publish(
            make_event(
                EventType.ANCHOR,
                {
                    "topic_id": anchor.topic_id,
                    "topic_name": topic_name,
                    "fragment_id": anchor.fragment_id,
                    "fragment_title": fragment_title,
                },
            )
        )

    def _build_embedding_backend(self):
        """Pick the local embedding backend (ONNX model or None). Vector recall
        never uses a remote/cloud credential."""
        from agent.selector.onnx import OnnxEmbeddingBackend

        models_dir = Path(os.environ.get("QIO_MODELS_DIR") or (self.settings.data_dir / "models"))
        onnx = OnnxEmbeddingBackend(
            self.conn, model_dir=models_dir / "bge-small-zh-v1.5"
        )
        if onnx.available():
            return onnx
        return None

    def _restore_tools(self) -> None:
        """Restore agent-created tools from the persistent store at startup."""
        from agent.tools.runtime_tools import CodeTool
        from agent.tools.sandbox import SandboxExecutor
        from agent.tools.subagent_tool import SubagentTool

        sandbox = SandboxExecutor()
        for definition in self.tool_store.load_all():
            try:
                if definition.tool_type == "subagent":
                    tool = SubagentTool(
                        definition,
                        credentials=self.credentials,
                        task_manager=self.task_manager,
                        retriever=self.retriever,
                        adapter_factory=self.build_adapter_for_credential,
                        bus=self.bus,
                        trace_store=self.trace_store,
                    )
                else:
                    tool = CodeTool(definition, sandbox, credentials=self.credentials)
                self.registry.register(tool)
                logger.info("restored tool: %s", definition.name)
            except Exception:  # noqa: BLE001 - a broken tool must not block startup
                logger.warning("failed to restore tool: %s", definition.name, exc_info=True)

    # -- adapter ----------------------------------------------------------

    def resolve_main_ref(self) -> CredentialRef | None:
        refs = self.policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS)
        return refs[0] if refs else None

    def _loop_max_iterations(self) -> int:
        """用户配置的迭代上限；0 表示未配置（用模式默认值）。"""
        return self.settings_store.get_int("loop.max_iterations", 0)

    def _loop_token_budget(self) -> int:
        """用户配置的输出 token 预算；0 表示未配置（用模式默认值）。"""
        return self.settings_store.get_int("loop.output_token_budget", 0)

    async def build_adapter_for_credential(
        self, key_id: str, model: str | None = None
    ) -> BaseAdapter | None:
        """Build an adapter for an explicit credential (shared by main/subagent)."""
        secret = self.credentials.get_secret(key_id)
        if secret is None:
            return None
        meta = self.credentials.get_metadata(key_id)
        base_url = (meta.get("endpoint") if meta else None) or "https://api.openai.com/v1"
        model = model or (meta.get("default_model") if meta else None) or "gpt-4o-mini"
        if is_anthropic_endpoint(base_url):
            await probe_anthropic(secret, model, base_url)
            return AnthropicAdapter(api_key=secret, model=model, endpoint=base_url)
        client = AsyncOpenAI(api_key=secret, base_url=base_url)
        probe = await probe_adapter(
            client, model, endpoint=base_url, cache=self.probe_cache,
        )
        if probe.mode == AdapterMode.NATIVE:
            return NativeAdapter(client, model, endpoint=base_url)
        if probe.mode == AdapterMode.TEXT:
            return TextAdapter(client, model, endpoint=base_url)
        return None

    async def build_adapter(self) -> BaseAdapter | None:
        ref = self.resolve_main_ref()
        if ref is None:
            return None
        return await self.build_adapter_for_credential(ref.key_id, ref.default_model)

    async def _build_tool_lifecycle(self):
        """Build a ToolLifecycle bound to the current main adapter (dev workflow submit)."""
        from agent.tools.lifecycle import ToolLifecycle
        from agent.tools.sandbox import SandboxExecutor

        adapter = await self.build_adapter()
        if adapter is None:
            raise RuntimeError("no main-loop credential available for tool development")
        return ToolLifecycle(
            adapter=adapter,
            approvals=self.approvals,
            sandbox=SandboxExecutor(),
            registry=self.registry,
            credentials=self.credentials,
            task_manager=self.task_manager,
            retriever=self.retriever,
            adapter_factory=self.build_adapter_for_credential,
            bus=self.bus,
            tool_store=self.tool_store,
            trace_store=self.trace_store,
        )

    # -- selector refresh -------------------------------------------------

    def _refresh_selector(self) -> None:
        rows = self.conn.execute(
            "SELECT mi.id AS index_id, mi.fragment_id, mi.topic_id, mi.entity_ids, "
            "mi.keywords, mi.title, mi.token_estimate, mi.created_at, f.summary "
            "FROM memory_index mi LEFT JOIN fragments f ON f.id = mi.fragment_id"
        ).fetchall()
        docs = []
        titles: dict[str, str] = {}
        tokens: dict[str, int] = {}
        for row in rows:
            doc_id = row["index_id"]
            text = " ".join(
                [row["summary"] or "", row["title"] or ""]
            ).strip() or doc_id
            docs.append(
                {
                    "doc_id": doc_id,
                    "text": text,
                    "topic_id": row["topic_id"],
                    "entity_ids": json.loads(row["entity_ids"] or "[]"),
                    "keywords": json.loads(row["keywords"] or "[]"),
                    "created_at": row["created_at"],
                }
            )
            titles[doc_id] = row["title"] or ""
            tokens[doc_id] = row["token_estimate"] or 0
        from agent.selector.base import IndexedDoc

        self.selector.load(
            [IndexedDoc(**d) for d in docs], titles=titles, token_estimates=tokens
        )

    # -- graph helpers ----------------------------------------------------

    def _user_root_id(self) -> str:
        return self.topics.nodes.get_or_create_user_root().id

    def _topic_entity_ids(self, topic_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT e.dst FROM edges e WHERE e.src = ? AND e.type = 'mention' "
            "ORDER BY e.weight DESC LIMIT 10",
            (topic_id,),
        ).fetchall()
        return [r["dst"] for r in rows]

    def _record_tool_call(self, trace: dict) -> None:
        """Persist one tool call for trajectory analysis (truncated summaries)."""
        import json as _json

        from agent.memory.fragment import new_id

        active = AnchorService(self.conn).get_active()
        try:
            args = _json.dumps(trace.get("arguments") or {}, ensure_ascii=False)[:500]
        except Exception:
            args = "{}"
        result = str(trace.get("result") or "")[:200]
        self.conn.execute(
            "INSERT INTO tool_calls (id, topic_id, tool_name, arguments, result, ok, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("tc"),
                active.topic_id if active else None,
                trace.get("tool_name", "?"),
                args,
                result,
                1 if trace.get("ok") else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def _route_tools(self, query: str):
        """Route the tool set for one PLANNING step (core + ranked subset)."""
        specs = self.registry.specs()
        # 工具定义变化 → 失效 embedding 缓存
        sig = tuple((s.name, s.description) for s in specs)
        if sig != getattr(self, "_router_sig", None):
            self.tool_router.invalidate()
            self._router_sig = sig
        pending = False
        try:
            pending = any(not r.done for r in self.task_manager._records.values())
        except Exception:  # noqa: BLE001 - routing must never break planning
            pending = False
        return self.tool_router.route(
            query, specs, pending_tasks=pending, web_allowed=True
        )

    # -- subagent notify (strategy 3: completion wakes the main agent) -----

    async def _publish_turn_queue(self, snapshot: dict) -> None:
        """广播当前 turn 队列快照（运行中 + 排队中），供前端折叠气泡展示。"""
        from agent.api.events import EventType, make_event

        await self.bus.publish(make_event(EventType.TURN_QUEUE, snapshot))

    def _format_notice(self, task_id: str, record) -> str:
        result = record.result
        preview = (result.content or "")[:300] if result else ""
        status = "完成" if (result and result.ok) else "失败"
        return NOTIFY_SUBTASK_DONE.format(
            tool=record.tool, task_id=task_id, status=status, preview=preview
        )

    async def _handle_subagent_notify(self, task_id: str, record) -> None:
        """Notify callback: inject into a running loop, or start a notify turn."""
        if self._notify_turn:
            return  # no recursive notify turns
        notice = self._format_notice(task_id, record)
        # single-flight：唯一 active turn 就是正确归属；找不到才排队一个系统 turn
        if self.turns.push_notice(notice):
            return
        self.turns.submit(notice, None, notify=True)

    async def _execute_notify_turn(self, ctx) -> None:
        """System-driven turn: main agent reacts to a finished subagent task."""
        from agent.core.loop import AgentLoop

        self._notify_turn = True
        try:
            adapter = await self.build_adapter()
            if adapter is None:
                ctx.result = {"ok": False, "reason": "no_credential"}
                return
            topic = self.current_topic()
            ctx.current_topic = topic
            from agent.trace.recorder import TurnTracer

            tracer = TurnTracer(self.trace_store, ctx.turn_id)
            ctx.trace = tracer
            self.trace_store.begin(ctx.turn_id, initial_topic=topic)
            notice = ctx.message
            prediction = self.predictor.predict(notice, current_topic_id=topic)
            payload = self.build_injection(
                notice,
                topic_id=topic,
                aux_topic_ids=prediction.aux_topic_ids,
                entity_ids=self._topic_entity_ids(topic),
                user_node_id=self._user_root_id(),
                model=adapter.model,
            )
            tracer.injection(
                items=[
                    {
                        "surface": it.surface,
                        "item_id": it.item_id,
                        "tokens": it.tokens,
                        "score": round(it.score, 4),
                        "preview": (it.text or "")[:160],
                    }
                    for it in payload.plan.all_items
                ],
                total_tokens=payload.plan.total_tokens,
                budget={
                    "hard_cap": payload.plan.hard_cap,
                    "truncated": payload.plan.truncated,
                    "needs_consolidation": payload.plan.needs_consolidation,
                },
                dropped=[],
            )
            prompt = notice
            if payload.text:
                prompt = f"{payload.text}\n\n【系统通知】\n{notice}"
            loop = AgentLoop(
                adapter, self.registry, self.bus,
                tool_trace=self._record_tool_call,
                tool_selector=self._route_tools,
                max_iterations=self._loop_max_iterations() or None,
                token_budget=self._loop_token_budget() or None,
                approvals=self.approvals,
                guard=RunawayGuard(),
                turn_id=ctx.turn_id,
                trace=tracer,
            )
            ctx.loop = loop
            try:
                result = await loop.run(prompt)
            finally:
                ctx.loop = None
            # feedback enters memory (assistant message; no user message)
            notify_msg_id, _ = self.memory.append_message(
                topic_id=topic,
                role="assistant",
                content=result.final_content or "",
                content_type="text",
                model=adapter.model,
            )
            tracer.write("messages", notify_msg_id)
            ctx.final_content = result.final_content
            ctx.result = {"ok": True, "turn": result.__dict__}
            fragment = self.fragments.get_or_create_open(topic)
            if self.fragments.should_close(fragment):
                closed = await self._close_fragment(topic, adapter, tracer=tracer)
                if closed is not None:
                    self._refresh_selector()
                    self.predictor.refresh_topic_vector(topic)
            self.trace_store.finish(
                ctx.turn_id, "done", final_topic=topic, final_preview=result.final_content or ""
            )
        except Exception as exc:  # noqa: BLE001 - notify turn must not crash
            logger.warning("notify turn failed: %s", exc)
            self.trace_store.finish(ctx.turn_id, "failed", error=str(exc)[:200])
            ctx.result = {"ok": False, "reason": "notify_failed"}
        finally:
            self._notify_turn = False

    # -- short-term memory & topic helpers ---------------------------------

    def _focus_block(self, topic_id: str, fragment_id: str | None) -> str:
        """委派给 ContextAssembler（见 agent/services/context.py）。"""
        return self.context_assembler.focus_block(topic_id, fragment_id)

    def anchor_fragment_info(self) -> dict | None:
        """委派给 ContextAssembler。"""
        return self.context_assembler.anchor_fragment_info()

    def _short_term_items(
        self, topic_id: str, exclude_message_id: str | None = None
    ) -> list:
        """委派给 ContextAssembler。"""
        return self.context_assembler.short_term_items(
            topic_id, exclude_message_id=exclude_message_id
        )

    def _entity_card_topics(self, message: str) -> list[str]:
        """委派给 ContextAssembler。"""
        return self.context_assembler.entity_card_topics(message)

    def _topic_note(self, topic_id: str, prediction) -> str:
        """委派给 ContextAssembler。"""
        return self.context_assembler.topic_note(topic_id, prediction)

    def _move_message(self, message_id: str, old_topic: str, new_topic: str) -> None:
        """Move the user message to the new topic's open fragment after a switch."""
        target = self.fragments.get_or_create_open(new_topic)
        self.conn.execute(
            "UPDATE messages SET fragment_id = ? WHERE id = ?", (target.id, message_id)
        )
        old_frag = self.fragments.get_or_create_open(old_topic)
        remaining = self.fragments.messages(old_frag.id)
        if not remaining:
            self.conn.execute(
                "UPDATE fragments SET start_message_id = NULL, end_message_id = NULL WHERE id = ?",
                (old_frag.id,),
            )
        else:
            self.conn.execute(
                "UPDATE fragments SET end_message_id = ? WHERE id = ?",
                (remaining[-1]["id"], old_frag.id),
            )

    # -- injection --------------------------------------------------------

    def build_injection(
        self,
        query: str,
        *,
        topic_id: str | None = None,
        aux_topic_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        user_node_id: str | None = None,
        model: str | None = None,
        short_term: list | None = None,
        new_topic_candidate: bool = False,
        new_topic_reason: str = "",
        topic_note: str = "",
        focus_block: str = "",
        entity_cards: list[str] | None = None,
        system_prompt_tokens: int = 0,
        adapter_overhead_tokens: int = 0,
        tool_definitions_tokens: int = 0,
        completion_reserve: int | None = None,
    ) -> InjectionPayload:
        """委派给 ContextAssembler。"""
        return self.context_assembler.build_injection(
            query,
            topic_id=topic_id,
            aux_topic_ids=aux_topic_ids,
            entity_ids=entity_ids,
            user_node_id=user_node_id,
            model=model,
            short_term=short_term,
            new_topic_candidate=new_topic_candidate,
            new_topic_reason=new_topic_reason,
            topic_note=topic_note,
            focus_block=focus_block,
            entity_cards=entity_cards,
            system_prompt_tokens=system_prompt_tokens,
            adapter_overhead_tokens=adapter_overhead_tokens,
            tool_definitions_tokens=tool_definitions_tokens,
            completion_reserve=completion_reserve,
        )

    # -- turn -------------------------------------------------------------

    def _knowledge_snapshot_provider(self) -> list[dict]:
        """当前 active turn 的知识快照（供 correct_knowledge 使用）。"""
        active = self.turns.active
        return active.knowledge_snapshot if active is not None else []

    async def run_turn(self, message: str, topic_id: str | None = None) -> dict:
        """提交一个 turn 并等待结果（单飞由 TurnManager 保证）。"""
        ctx = self.turns.submit(message, topic_id)
        result = await self.turns.wait(ctx.turn_id)
        return result or {"ok": False, "reason": "no_result"}

    async def _execute_turn(self, ctx) -> None:
        """委派给 TurnOrchestrator（见 agent/services/turn_orchestrator.py）。"""
        await self.turn_orchestrator.execute(ctx)

    # -- fragment close: summary + entities + knowledge extraction --------

    async def _close_fragment(
        self, topic_id: str, adapter: BaseAdapter, tracer=None
    ) -> Any | None:
        """委派给 MemoryLifecycle。"""
        return await self.memory_lifecycle.close_fragment(
            topic_id, adapter, tracer=tracer
        )

    async def _extract_knowledge(
        self,
        adapter: BaseAdapter,
        summary: Any,
        topic_id: str,
        entity_ids: list[str],
        fragment_id: str,
        tracer=None,
    ) -> None:
        """委派给 MemoryLifecycle。"""
        await self.memory_lifecycle.extract_knowledge(
            adapter, summary, topic_id, entity_ids, fragment_id, tracer=tracer
        )

    # -- budget-pressure consolidation ------------------------------------

    async def consolidate(self, topic_id: str, adapter: BaseAdapter) -> bool:
        """委派给 MemoryLifecycle。"""
        return await self.memory_lifecycle.consolidate(topic_id, adapter)

    # -- topics -----------------------------------------------------------

    def current_topic(self) -> str:
        """Current anchor topic (active cursor), falling back to the default topic."""
        row = self.conn.execute(
            "SELECT topic_id FROM cursor WHERE anchor_type = 'active'"
        ).fetchone()
        if row is not None and row["topic_id"]:
            return row["topic_id"]
        return self._ensure_default_topic()

    def session_messages(self, topic_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, role, content, content_type, created_at FROM messages "
            "WHERE fragment_id IN (SELECT id FROM fragments WHERE topic_id = ?) "
            "ORDER BY created_at",
            (topic_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _ensure_default_topic(self) -> str:
        if getattr(self, "_default_topic_id", None):
            return self._default_topic_id
        topics = self.topics.nodes.list_topics()
        if topics:
            self._default_topic_id = topics[0].id
        else:
            node = self.topics.nodes.create_topic("默认话题")
            self._default_topic_id = node.id
        return self._default_topic_id


def make_warning(message: str):
    from agent.api.events import EventType, make_event

    return make_event(EventType.WARNING, {"code": "app", "message": message})


def make_error(code: str, message: str, recoverable: bool):
    from agent.api.events import EventType, make_event

    return make_event(EventType.ERROR, {"code": code, "message": message, "recoverable": recoverable})
