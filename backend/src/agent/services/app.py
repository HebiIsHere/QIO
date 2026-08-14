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
    INJECT_FOCUS_SECTION,
    INJECT_SHORT_TERM_SECTION,
    NOTIFY_SUBTASK_DONE,
    TOPIC_NOTE_CURRENT,
    TOPIC_NOTE_NEW_REASON,
    TOPIC_NOTE_RELATED,
    TOPIC_NOTE_SWITCH,
)
from agent.services.injection import BudgetConfig, InjectionAssembler, InjectionBudget, InjectionPayload
from agent.services.retrieval import Retriever
from agent.tools.builtin import EchoTool, NowTool

if TYPE_CHECKING:  # pragma: no cover - avoids api->app cycle at import time
    from agent.api.bus import EventBus
from agent.tools.memory_search import MemorySearchTool
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAIN_LOOP_TAG = "main-loop"
BUDGET_RATIO = 0.25
DEFAULT_FRAGMENT_MAX_MESSAGES = 10
CONSOLIDATION_COOLDOWN_SECONDS = 600


class AppContext:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bus: EventBus) -> None:
        self.settings = settings
        self.conn = conn
        self.bus = bus
        self.credentials = CredentialStore(conn)
        self.settings_store = SettingsStore(conn)
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
        self.predictor = TopicPredictor(conn, self.embedding, self.topics)
        from agent.tools.approval import ApprovalService

        self.approvals = ApprovalService(bus)
        self.registry = ToolRegistry()
        self.registry.register(EchoTool())
        self.registry.register(NowTool())
        self.registry.register(MemorySearchTool(self.retriever))
        from agent.tools.topic_tools import CreateTopicTool, SwitchTopicTool

        self.registry.register(SwitchTopicTool(conn))
        self.registry.register(
            CreateTopicTool(conn, approvals=self.approvals, predictor=self.predictor)
        )
        from agent.tools.subagent_tool import AwaitTaskTool, ReadTaskResultTool
        from agent.tools.task_manager import TaskManager

        self.task_manager = TaskManager(bus)
        self.registry.register(
            AwaitTaskTool(self.task_manager, notify_handler=self._handle_subagent_notify)
        )
        self.registry.register(ReadTaskResultTool(self.task_manager))
        from agent.tools.approval import ApprovalService
        from agent.tools.dev_tools import (
            CreateToolTool,
            DevReadFileTool,
            DevRunTestsTool,
            DevSubmitTool,
            DevWriteFileTool,
        )
        from agent.tools.dev_workspace import DevWorkspace

        self.dev_workspaces = DevWorkspace(settings.data_dir / "dev-workspaces")
        self.registry.register(CreateToolTool(self.dev_workspaces))
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

        self._knowledge_snapshot: list[dict] = []
        self.registry.register(
            CorrectKnowledgeTool(conn, snapshot_provider=lambda: self._knowledge_snapshot)
        )
        from agent.storage.tool_store import ToolStore

        self.tool_store = ToolStore(conn)
        self._restore_tools()
        self._active_loop = None
        self._notify_turn = False
        from agent.services.maintenance import MaintenanceScheduler
        from agent.services.tool_router import ToolRouter

        self.tool_router = ToolRouter(embedding=self.embedding)
        self.maintenance = MaintenanceScheduler(self)
        self._refresh_selector()

    def _build_embedding_backend(self):
        """Pick the recall backend: remote embeddings (BYOK) > local ONNX > None."""
        from agent.selector.onnx import OnnxEmbeddingBackend
        from agent.selector.remote import DEFAULT_REMOTE_MODEL, RemoteEmbeddingBackend

        models_dir = Path(os.environ.get("QIO_MODELS_DIR") or (self.settings.data_dir / "models"))
        onnx = OnnxEmbeddingBackend(
            self.conn, model_dir=models_dir / "bge-small-zh-v1.5"
        )
        refs = self.policy.resolve("embedding", ["embedding"])
        if refs:
            secret = self.credentials.get_secret(refs[0].key_id)
            if secret is not None:
                model = (
                    self.settings_store.get("embedding.model")
                    or refs[0].default_model
                    or DEFAULT_REMOTE_MODEL
                )
                base_url = refs[0].endpoint or "https://api.openai.com/v1"
                remote = RemoteEmbeddingBackend(
                    self.conn, api_key=secret, base_url=base_url, model=model
                )
                if remote.available():
                    logger.info("embedding backend: remote (%s)", model)
                    return remote
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
                    )
                else:
                    tool = CodeTool(definition, sandbox, credentials=self.credentials)
                self.registry.register(tool)
                logger.info("restored tool: %s", definition.name)
            except Exception:  # noqa: BLE001 - a broken tool must not block startup
                logger.warning("failed to restore tool: %s", definition.name, exc_info=True)

    # -- adapter ----------------------------------------------------------

    def resolve_main_ref(self) -> CredentialRef | None:
        refs = self.policy.resolve("main-loop", [MAIN_LOOP_TAG])
        return refs[0] if refs else None

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
        return self.tool_router.route(query, self.registry.specs())

    # -- subagent notify (strategy 3: completion wakes the main agent) -----

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
        if self._active_loop is not None:
            self._active_loop.push_notice(notice)
            return
        asyncio.create_task(self._run_notify_turn(task_id, record))

    async def _run_notify_turn(self, task_id: str, record) -> None:
        """System-driven turn: main agent reacts to a finished subagent task."""
        from agent.core.loop import AgentLoop

        self._notify_turn = True
        try:
            adapter = await self.build_adapter()
            if adapter is None:
                return
            topic = self.current_topic()
            notice = self._format_notice(task_id, record)
            prediction = self.predictor.predict(notice, current_topic_id=topic)
            payload = self.build_injection(
                notice,
                topic_id=topic,
                aux_topic_ids=prediction.aux_topic_ids,
                entity_ids=self._topic_entity_ids(topic),
                user_node_id=self._user_root_id(),
                model=adapter.model,
            )
            prompt = notice
            if payload.text:
                prompt = f"{payload.text}\n\n{notice}"
            loop = AgentLoop(
                adapter, self.registry, self.bus,
                tool_trace=self._record_tool_call,
                tool_selector=self._route_tools,
            )
            self._active_loop = loop
            try:
                result = await loop.run(prompt)
            finally:
                self._active_loop = None
            # feedback enters memory (assistant message; no user message)
            self.memory.append_message(
                topic_id=topic,
                role="assistant",
                content=result.final_content or "",
                content_type="text",
                model=adapter.model,
            )
            fragment = self.fragments.get_or_create_open(topic)
            if self.fragments.should_close(fragment):
                closed = await self._close_fragment(topic, adapter)
                if closed is not None:
                    self._refresh_selector()
                    self.predictor.refresh_topic_vector(topic)
        except Exception as exc:  # noqa: BLE001 - notify turn must not crash
            logger.warning("notify turn failed: %s", exc)
        finally:
            self._notify_turn = False

    # -- short-term memory & topic helpers ---------------------------------

    def _focus_block(self, topic_id: str, fragment_id: str | None) -> str:
        """Build the focus block for the anchored fragment (empty when invalid)."""
        if not fragment_id:
            return ""
        frag = self.fragments.get(fragment_id)
        if frag is None or frag.topic_id != topic_id:
            return ""
        title = None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ?", (fragment_id,)
        ).fetchone()
        if row is not None and row["title"]:
            title = row["title"]
        if not title:
            if frag.closed_at is None:
                title = "当前片段"
            else:
                title = (frag.summary or "")[:20] or "历史片段"
        parts = [INJECT_FOCUS_SECTION.format(title=title)]
        if frag.summary:
            parts.append(frag.summary)
        for m in self.fragments.messages(fragment_id)[:3]:
            content = m["content"] or ""
            parts.append(f"[{m['role']}] {content}")
        return "\n".join(parts)

    def anchor_fragment_info(self) -> dict | None:
        """Anchor fragment metadata for the session context (id + title)."""
        active = AnchorService(self.conn).get_active()
        if active is None or not active.fragment_id:
            return None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ?",
            (active.fragment_id,),
        ).fetchone()
        return {"id": active.fragment_id, "title": row["title"] if row else None}

    def _short_term_items(self, topic_id: str) -> list:
        """Deterministic short-term memory: open fragment transcript + recent summaries."""
        from agent.knowledge.inject import InjectionSource
        from agent.memory.index import estimate_tokens
        from agent.services.injection import PlannedItem

        items: list = []
        frag = self.fragments.get_or_create_open(topic_id)
        if frag.start_message_id is not None:
            lines = [
                f"[{m['role']}] {m['content']}"
                for m in self.fragments.messages(frag.id)
                if m["content"]
            ]
            if lines:
                text = "\n".join(lines)
                items.append(
                    PlannedItem(
                        source="memory",
                        surface="topic_short",
                        item_id=frag.id,
                        text=INJECT_SHORT_TERM_SECTION.format(text=text),
                        tokens=estimate_tokens(text),
                    )
                )
        for s in InjectionSource(self.conn).recent_fragment_summaries(topic_id, limit=2):
            text = f"【短期摘要·{s['title']}】\n{s['summary']}"
            items.append(
                PlannedItem(
                    source="memory",
                    surface="topic_short",
                    item_id=s["id"],
                    text=text,
                    tokens=estimate_tokens(text),
                )
            )
        return items

    def _entity_card_topics(self, message: str) -> list[str]:
        """消息命中的实体卡，其关联话题（mention 边 topic→entity，软信号用）。"""
        from agent.entities.cards import EntityCardService

        svc = EntityCardService(self.conn)
        topics: set[str] = set()
        for card in svc.match_cards(message):
            if not card.node_id:
                continue
            rows = self.conn.execute(
                "SELECT src FROM edges WHERE dst = ? AND type = 'mention'",
                (card.node_id,),
            ).fetchall()
            for r in rows:
                topics.add(r["src"])
        return list(topics)

    def _topic_note(self, topic_id: str, prediction) -> str:
        """Human-readable topic context injected so the main model can act on it."""
        parts: list[str] = []
        node = self.topics.nodes.get_topic(topic_id)
        parts.append(
            TOPIC_NOTE_CURRENT.format(name=node.name if node else topic_id, topic_id=topic_id)
        )
        if prediction.aux_topic_ids:
            names = []
            for tid in prediction.aux_topic_ids:
                n = self.topics.nodes.get_topic(tid)
                names.append(f"「{n.name if n else tid}」（{tid}）")
            parts.append(TOPIC_NOTE_RELATED.format(names="、".join(names)))
        if prediction.suggested_switch and prediction.main_topic_id:
            n = self.topics.nodes.get_topic(prediction.main_topic_id)
            score = prediction.scores.get(prediction.main_topic_id, 0.0)
            parts.append(
                TOPIC_NOTE_SWITCH.format(
                    name=n.name if n else prediction.main_topic_id,
                    topic_id=prediction.main_topic_id,
                    score=score,
                )
            )
        return "；".join(parts)

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
    ) -> InjectionPayload:
        from agent.knowledge.inject import InjectionSource

        context_window = self.context_registry.get(None, model or "unknown")
        budget = InjectionBudget(
            BudgetConfig(context_window=context_window, budget_ratio=BUDGET_RATIO)
        )
        assembler = InjectionAssembler(
            budget,
            self.retriever,
            knowledge_source=InjectionSource(self.conn),
        )
        return assembler.build(
            query,
            topic_id=topic_id,
            aux_topic_ids=aux_topic_ids,
            entity_ids=entity_ids or [],
            user_node_id=user_node_id,
            top_k=6,
            short_term=short_term,
            new_topic_candidate=new_topic_candidate,
            new_topic_reason=new_topic_reason,
            topic_note=topic_note,
            focus_block=focus_block,
            entity_cards=entity_cards,
        )

    # -- turn -------------------------------------------------------------

    async def run_turn(self, message: str, topic_id: str | None = None) -> dict:
        from agent.core.loop import AgentLoop

        adapter = await self.build_adapter()
        if adapter is None:
            await self.bus.publish(
                make_warning("no main-loop credential configured; add one in Settings")
            )
            return {"ok": False, "reason": "no_credential"}

        self.fragments.max_messages = self.settings_store.get_int(
            "fragment.max_messages", DEFAULT_FRAGMENT_MAX_MESSAGES
        )
        topic = topic_id or self.current_topic()
        # speaking in a topic anchors it (if the anchor is absent or stale)
        active_anchor = AnchorService(self.conn).get_active()
        if active_anchor is None or active_anchor.topic_id != topic:
            AnchorService(self.conn).set_active(topic)

        # topic prediction (embedding judgement; execution stays with main model)
        prediction = self.predictor.predict(message, current_topic_id=topic)
        # 归属分类（联想激活/抑制 + 实体软信号）
        from agent.services.affinity import (
            NEW_TOPIC_STRICT,
            TopicMode,
            classify,
            related_topics,
        )

        decision = classify(message, prediction, topic, self._entity_card_topics(message))
        if decision.mode == TopicMode.IN_TOPIC:
            aux_topic_ids = related_topics(self.conn, topic, top_n=2)
        elif decision.mode == TopicMode.SWITCH and decision.switch_to:
            aux_topic_ids = [decision.switch_to]
        else:
            aux_topic_ids = []
        new_topic_candidate = False
        reason = ""
        if decision.mode == TopicMode.NEW_TOPIC:
            if decision.closest_score < NEW_TOPIC_STRICT or prediction.is_new_topic_candidate:
                new_topic_candidate = True
                reason = f"最高话题相似度 {decision.closest_score:.2f} 低于阈值，无匹配话题"
        # 软提示：最相似话题引导切换 + 实体关联话题
        extra_note = ""
        if decision.mode == TopicMode.NEW_TOPIC and decision.closest_topic and decision.closest_score >= NEW_TOPIC_STRICT:
            n = self.topics.nodes.get_topic(decision.closest_topic)
            extra_note += (
                f"；最相似话题「{n.name if n else decision.closest_topic}」"
                f"（相似度 {decision.closest_score:.2f}），如确属新话题需人工确认"
            )
        if decision.entity_hints:
            names = []
            for tid in decision.entity_hints:
                n = self.topics.nodes.get_topic(tid)
                names.append(n.name if n else tid)
            extra_note += "；提及实体关联话题：" + "、".join(names)

        # write user message into memory domain first
        msg_id, _ = self.memory.append_message(
            topic_id=topic,
            role="user",
            content=message,
            content_type="text",
            model=adapter.model,
        )
        # anchored fragment focus (从这里开始)
        active_anchor = AnchorService(self.conn).get_active()
        focus_block = ""
        if active_anchor is not None and active_anchor.fragment_id:
            focus_block = self._focus_block(topic, active_anchor.fragment_id)
        # short-term memory of the current topic (open fragment + recent summaries)
        short_term = self._short_term_items(topic)
        topic_note = self._topic_note(topic, prediction)
        if extra_note:
            topic_note = topic_note + extra_note
        # 实体卡命中（交流锚点）：消息提到经验性实体 → 高优注入卡文本
        from agent.entities.cards import EntityCardService

        card_svc = EntityCardService(self.conn)
        entity_cards = [card_svc.format_card(c) for c in card_svc.match_cards(message)]
        payload = self.build_injection(
            message,
            topic_id=topic,
            aux_topic_ids=aux_topic_ids,
            entity_ids=self._topic_entity_ids(topic),
            user_node_id=self._user_root_id(),
            model=adapter.model,
            short_term=short_term,
            new_topic_candidate=new_topic_candidate,
            new_topic_reason=reason,
            topic_note=topic_note,
            focus_block=focus_block,
            entity_cards=entity_cards,
        )
        self._knowledge_snapshot = [
            {
                "item_id": item.item_id,
                "content": (item.text.split("] ", 1)[-1] if "] " in item.text else item.text),
            }
            for item in payload.plan.knowledge
        ]
        prompt = message
        if payload.text:
            prompt = f"{payload.text}\n\n用户消息：{message}"

        loop = AgentLoop(
            adapter, self.registry, self.bus,
            tool_trace=self._record_tool_call,
            tool_selector=self._route_tools,
        )
        self._active_loop = loop
        try:
            result = await loop.run(prompt)
        except Exception as exc:
            logger.exception("turn failed")
            await self.bus.publish(
                make_error("turn_failed", str(exc)[:200], recoverable=True)
            )
            return {"ok": False, "reason": "turn_failed"}
        finally:
            self._active_loop = None

        # the main model may have switched/created a topic via tools this turn
        final_topic = topic
        active = AnchorService(self.conn).get_active()
        if active is not None and active.topic_id and active.topic_id != topic:
            final_topic = active.topic_id
            self._move_message(msg_id, topic, final_topic)

        self.memory.append_message(
            topic_id=final_topic,
            role="assistant",
            content=result.final_content or "",
            content_type="text",
            model=adapter.model,
        )
        # chunk close -> summary + extraction + indexing + topic vector refresh
        fragment = self.fragments.get_or_create_open(final_topic)
        if self.fragments.should_close(fragment):
            closed = await self._close_fragment(final_topic, adapter)
            if closed is not None:
                self._refresh_selector()
                self.predictor.refresh_topic_vector(final_topic)
        # budget-pressure memory consolidation (rolling summary)
        if payload.plan.needs_consolidation:
            await self.consolidate(final_topic, adapter)
        return {"ok": True, "turn": result.__dict__}

    # -- fragment close: summary + entities + knowledge extraction --------

    async def _close_fragment(self, topic_id: str, adapter: BaseAdapter) -> Any | None:
        from agent.knowledge.lifecycle import KnowledgeService
        from agent.knowledge.verify import VerificationService
        from agent.memory.summary import extract_knowledge_candidates, summarize_fragment

        fragment = self.fragments.get_or_create_open(topic_id)
        if fragment.start_message_id is None:
            return None
        messages = self.fragments.messages(fragment.id)
        summary, error = await summarize_fragment(
            adapter, [dict(m) for m in messages]
        )
        if summary is None:
            self.fragments.close(fragment.id, "", summary_model=None, summary_version=0)
            return None

        # entities: lazy-create nodes + mention edges (attachment surface)
        entity_ids: list[str] = []
        from agent.graph.edges import EdgeService
        from agent.graph.nodes import NodeService

        nodes = NodeService(self.conn)
        edges = EdgeService(self.conn)
        for name in summary.entities:
            entity, created = nodes.mention(name, topic_id, force=False)
            if entity is not None:
                entity_ids.append(entity.id)
                edges.add(topic_id, entity.id, "mention")

        # 共同实体 → 话题相关边（实体作为话题间桥梁）
        from agent.services.affinity import relate_shared_entities

        relate_shared_entities(self.conn, topic_id, entity_ids)

        # 实体卡提炼：主模型从对话提炼经验性实体卡（属性/关系/别名），失败静默降级
        from agent.entities.cards import EntityCardService
        from agent.entities.extract import extract_entity_cards

        try:
            candidates = await extract_entity_cards(adapter, [dict(m) for m in messages])
            card_svc = EntityCardService(self.conn)
            for cand in candidates:
                card = card_svc.upsert(cand)
                # 实体卡向量（供向量检索）；embedding 不可用时静默跳过
                try:
                    if self.embedding is not None and hasattr(self.embedding, "save_entity_card_vector"):
                        self.embedding.save_entity_card_vector(
                            card.id, f"{card.name}：{card.summary or ''}"
                        )
                except Exception:
                    pass
        except Exception:
            logger.warning("entity card extraction failed", exc_info=True)

        self.fragments.close(
            fragment.id,
            summary.summary,
            summary_model=adapter.model,
            summary_version=1,
        )
        self.index_builder.build(
            fragment_id=fragment.id,
            topic_id=topic_id,
            title=summary.title,
            summary_text=summary.summary,
            entities=summary.entities,
            keywords=summary.keywords,
            message_texts=[m["content"] for m in messages],
        )

        # knowledge extraction: draft -> tiered verification -> attach
        await self._extract_knowledge(adapter, summary, topic_id, entity_ids, fragment.id)
        return self.fragments.get(fragment.id)

    async def _extract_knowledge(
        self,
        adapter: BaseAdapter,
        summary: Any,
        topic_id: str,
        entity_ids: list[str],
        fragment_id: str,
    ) -> None:
        from agent.knowledge.lifecycle import HIGH_IMPACT_CATEGORIES, KnowledgeService
        from agent.knowledge.verify import VerificationService
        from agent.memory.summary import extract_knowledge_candidates

        extraction, error = await extract_knowledge_candidates(adapter, summary)
        if extraction is None:
            logger.info("knowledge extraction skipped: %s", error)
            return
        ks = KnowledgeService(self.conn)
        vs = VerificationService(self.conn, ks)
        for cand in extraction.candidates:
            node_ids: list[str] = []
            if cand.attach == "user":
                node_ids = [self._user_root_id()]
            elif cand.attach == "entity" and cand.entity:
                node, _ = self.topics.nodes.mention(cand.entity, topic_id, force=False)
                if node is not None:
                    node_ids = [node.id]
            else:
                node_ids = [topic_id]
            try:
                item = ks.create(
                    category=cand.category,
                    content=cand.content,
                    node_ids=node_ids,
                    provenance={"fragment_id": fragment_id},
                )
                ks.submit(item.id)
                if cand.category in HIGH_IMPACT_CATEGORIES:
                    # stays draft; user confirmation required before activation
                    continue
                result = vs.review(ks.get(item.id), verified_by="system")
                if result.accepted:
                    ks.activate(item.id)
            except Exception as exc:  # extraction must not break the fragment close
                logger.warning("knowledge candidate failed: %s", exc)

    # -- budget-pressure consolidation ------------------------------------

    async def consolidate(self, topic_id: str, adapter: BaseAdapter) -> bool:
        """Rolling summary of the open fragment under budget pressure."""
        from agent.memory.summary import summarize_rolling

        fragment = self.fragments.get_or_create_open(topic_id)
        if fragment.start_message_id is None:
            return False
        meta = dict(fragment.meta)
        last = meta.get("consolidated_at")
        if last:
            try:
                last_ts = datetime.fromisoformat(last)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - last_ts).total_seconds()
                if age < CONSOLIDATION_COOLDOWN_SECONDS:
                    return False
            except ValueError:
                pass
        messages = self.fragments.messages(fragment.id)
        summary, error = await summarize_rolling(
            adapter, fragment.summary, [dict(m) for m in messages]
        )
        if summary is None:
            logger.info("consolidation skipped: %s", error)
            return False
        now = datetime.now(timezone.utc).isoformat()
        meta["consolidated"] = True
        meta["consolidated_at"] = now
        meta["consolidation_count"] = int(meta.get("consolidation_count", 0)) + 1
        self.conn.execute(
            "UPDATE fragments SET summary = ?, summary_model = ?, summary_version = summary_version + 1, meta = ? "
            "WHERE id = ?",
            (summary.summary, adapter.model, json.dumps(meta, ensure_ascii=False), fragment.id),
        )
        self.index_builder.build(
            fragment_id=fragment.id,
            topic_id=topic_id,
            title=summary.title,
            summary_text=summary.summary,
            entities=summary.entities,
            keywords=summary.keywords,
            message_texts=[m["content"] for m in messages],
        )
        self._refresh_selector()
        return True

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