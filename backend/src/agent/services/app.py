"""Application context: wires storage, credentials, adapters, tools, loop.

Built once per process; the HTTP layer pulls what it needs from it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sqlite3
import time
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
from agent.core.tool_state import ToolExecutionState
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

# 用户忽略过某类高影响知识后，这段时间内不再把同类候选弹到对话里。
# 为什么按类别而不是按句子：模型每次的说法都不一样（「用户偏好中文回复」/「用户更喜欢
# 简洁的解释」），只比字符串会让用户刚说完「忽略」又被问一遍近乎同一件事。
# 被压下的候选仍然留在知识面板的待确认列表里（不丢数据，只是不再打扰）。
KNOWLEDGE_IGNORE_COOLDOWN_MINUTES = 30
KNOWLEDGE_IGNORE_KEY_PREFIX = "knowledge.ignored_at."

# Anthropic 能力探测的缓存时长：能力档位不会频繁变化，没必要每个 Turn 实测一次。
ANTHROPIC_PROBE_TTL_SECONDS = 3600.0

# 会话历史分页：首屏只取最近一页，其余按游标往前翻。
SESSION_PAGE_DEFAULT_LIMIT = 200
SESSION_PAGE_MAX_LIMIT = 500

# 内部循环的 turn_id 前缀。Subagent 面向用户的最小显示单位是「独立任务」，
# 内部工具的明细既不展示也不恢复（产品范围边界，不是待修 bug）。
INTERNAL_TURN_PREFIXES = ("subagent:",)


def _is_main_turn_id(turn_id: object) -> bool:
    """这条工具执行记录是否属于**主 Turn**（内部循环 / 无归属记录不算）。"""
    if not isinstance(turn_id, str) or not turn_id:
        return False
    return not turn_id.startswith(INTERNAL_TURN_PREFIXES)


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
        # Adapter / 底层 HTTP client 复用：每个 Turn 重新建连接池是没有必要的
        # （新 TCP/TLS 握手 + 新连接池），能力探测结果也不该每轮重测。
        # key = (key_id, credential version, endpoint, model)
        self._adapter_cache: dict[tuple, BaseAdapter] = {}
        # Anthropic 能力探测的缓存（key 里含凭据版本，换 Key 立即失效）
        self._anthropic_probe_at: dict[tuple, float] = {}
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
        from agent.services.planet import PlanetBrowseService

        # 星球浏览景观的数据层：总话题数无上限，可见容量由视觉层决定。
        self.planet = PlanetBrowseService(conn)
        from agent.services.navigation import TopicNavigationService

        # Topic 导航的唯一入口：Anchor 只由它写入（Planet 选中 / 检索 / 预测都不算导航）。
        self.navigation = TopicNavigationService(conn)
        from agent.services.binding import TurnBindingService

        # 阶段 1：轮次归属（Turn → Topic/Fragment）与用户的历史接续意图。
        self.bindings = TurnBindingService(conn)
        self.memory.bindings = self.bindings
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
            upsert_selector=self._upsert_selector,
            remove_selector=self._remove_selector,
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

        self.search_service = SearchService()
        self.apply_search_settings()
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
        from agent.tools.cmd_tools import (
            ProcKillTool,
            ProcListTool,
            RunCmdTool,
            RunProgramTool,
            SysInfoTool,
        )

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
            RunProgramTool(),
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
        from agent.tools.continue_tool import ContinueFromFragmentTool

        self.registry.register(ContinueFromFragmentTool(conn))
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
        # 建工具是一条流程、一张卡：这些工具把阶段事件发到同一个出口
        self.registry.register(
            CreateToolTool(
                self.dev_workspaces,
                bus=self.bus,
                turn_id_provider=self._active_turn_id,
            )
        )
        self.registry.register(DevListFilesTool(self.dev_workspaces))
        self.registry.register(
            DevWriteFileTool(
                self.dev_workspaces,
                bus=self.bus,
                turn_id_provider=self._active_turn_id,
            )
        )
        self.registry.register(DevReadFileTool(self.dev_workspaces))
        self.registry.register(
            DevRunTestsTool(
                self.dev_workspaces,
                bus=self.bus,
                turn_id_provider=self._active_turn_id,
            )
        )
        self.registry.register(
            DevSubmitTool(
                self.dev_workspaces,
                lifecycle_builder=self._build_tool_lifecycle,
                bus=self.bus,
                turn_id_provider=self._active_turn_id,
            )
        )
        from agent.tools.entity_tools import CorrectEntityTool

        self.registry.register(CorrectEntityTool(conn))
        from agent.tools.knowledge_correction import CorrectKnowledgeTool

        # turn runtime boundary：进程级服务在此，单轮状态在 TurnContext
        from agent.core.turn import TurnManager

        # 工具执行的权威事实（进程级、纯内存、有界）：活工具 + 最近结束的工具。
        # 为什么不是事件总线 history：history 会被裁剪 / 清空 / overflow，
        # 而「这次调用最终成功、失败还是取消」不能因为一条通知丢失就永久变成 unknown。
        self.tool_state = ToolExecutionState()
        self.turns = TurnManager()
        self.turns.set_runner(self._execute_turn)
        self.turns.set_publisher(self._publish_turn_queue)
        # TurnManager 是 turn 生命周期的唯一事实源：TURN_START / TURN_END 只由它发。
        self.turns.set_emitter(self._publish_turn_event)
        self.registry.register(
            CorrectKnowledgeTool(conn, snapshot_provider=self._knowledge_snapshot_provider)
        )
        from agent.storage.tool_store import ToolStore

        self.tool_store = ToolStore(conn)
        self.services.register("tool_store", self.tool_store)
        self._restore_tools()
        self._notify_turn = False
        # 上一次宣告过的适配档位：正常状态不制造噪声，只有档位变化才广播
        self._announced_mode: str | None = None
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
        if tool_name not in ("switch_topic", "create_topic", "continue_from_fragment"):
            return
        if result is None or not getattr(result, "ok", False):
            return
        await self._publish_anchor_event()

    async def _publish_anchor_event(self) -> None:
        """把当前 active 锚点（话题 + 片段 + 是否历史位置）以 ANCHOR 事件推给前端。

        前端据此更新话题行与「当前从历史位置继续」提示：位置推进到当前片段后
        historic=false，提示自然消失（不会几十轮后还显示旧片段）。
        """
        from agent.api.events import EventType, make_event

        anchors = AnchorService(self.conn)
        anchor = anchors.get_active()
        if anchor is None or not anchor.topic_id:
            return
        node = self.topics.nodes.get_topic(anchor.topic_id)
        topic_name = node.name if node is not None else anchor.topic_id
        historic = anchors.is_historic_position(anchor.topic_id)
        fragment_title = self._fragment_title(anchor.fragment_id)
        # 阶段 1：把「已登记但还没落实」的接续选择一并广播。
        # 界面据此显示「将从所选记录继续」；落实之后（consumed）这段字段消失。
        pending = self.bindings.peek_intent()
        await self.bus.publish(
            make_event(
                EventType.ANCHOR,
                {
                    "topic_id": anchor.topic_id,
                    "topic_name": topic_name,
                    "fragment_id": anchor.fragment_id,
                    "fragment_title": fragment_title,
                    "historic": historic,
                    "pending_intent_id": pending.intent_id if pending else None,
                    "pending_intent_version": pending.version if pending else None,
                    "pending_source_fragment_id": pending.source_fragment_id if pending else None,
                    "pending_source_title": (
                        self._fragment_title(pending.source_fragment_id) if pending else None
                    ),
                },
            )
        )

    def _fragment_title(self, fragment_id: str | None) -> str | None:
        """片段标题：优先 memory_index（已封块摘要的标题），否则用摘要首句。"""
        if not fragment_id:
            return None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
        if row is not None and row["title"]:
            return str(row["title"])
        frag = self.fragments.get(fragment_id)
        if frag is not None and frag.summary:
            return frag.summary.strip().splitlines()[0][:40]
        return None

    def apply_search_settings(self) -> None:
        """把搜索设置套用到运行中的 SearchService。

        保存设置必须立刻生效：此前只写 settings 表，运行中的 SearchService 仍是
        旧配置（改了 SearXNG/博查要重启后端才生效）。
        """
        self.search_service.set_config(
            searxng_url=self.settings_store.get("search.searxng_url") or None,
            bocha_api_key=self.settings_store.get("search.bocha_api_key") or None,
            keyless_fallback=self.settings_store.get_bool("search.keyless_fallback", True),
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
                # 能力指纹变化 → 不得沿用旧授权，需重新批准
                from agent.tools.policy import default_policy_for, policy_fingerprint

                current_fp = policy_fingerprint(default_policy_for(definition))
                if (
                    definition.approved_policy_fingerprint
                    and definition.approved_policy_fingerprint != current_fp
                ):
                    logger.warning(
                        "tool %s capability policy changed since approval; "
                        "skipping restore (needs re-approval)",
                        definition.name,
                    )
                    continue
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
        """Build (or reuse) an adapter for an explicit credential.

        正常 Turn 复用同一个 adapter（等价于复用底层 HTTP client / 连接池）；
        凭据被轮换、端点或模型变化时缓存键变化 → 自然重建；
        应用关闭时由 `aclose()` 统一释放。
        """
        secret = self.credentials.get_secret(key_id)
        if secret is None:
            return None
        meta = self.credentials.get_metadata(key_id)
        base_url = (meta.get("endpoint") if meta else None) or "https://api.openai.com/v1"
        model = model or (meta.get("default_model") if meta else None) or "gpt-4o-mini"
        cache_key = (
            key_id,
            (meta or {}).get("version"),
            (meta or {}).get("updated_at"),
            base_url,
            model,
        )
        cached = self._adapter_cache.get(cache_key)
        if cached is not None:
            return cached

        adapter = await self._create_adapter(secret, model, base_url, key_id, meta)
        if adapter is not None:
            self._remember_adapter(cache_key, adapter)
        return adapter

    async def _create_adapter(
        self,
        secret: str,
        model: str,
        base_url: str,
        key_id: str,
        meta: dict | None,
    ) -> BaseAdapter | None:
        if is_anthropic_endpoint(base_url):
            await self._ensure_anthropic_capability(secret, model, base_url, key_id, meta)
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

    async def _ensure_anthropic_capability(
        self,
        secret: str,
        model: str,
        base_url: str,
        key_id: str,
        meta: dict | None,
    ) -> None:
        """Anthropic 能力探测按 (credential, endpoint, model) 缓存。

        探测失败不写缓存（下一次仍会重试），保证 provider 变化或临时故障后
        仍能恢复到正确档位。
        """
        key = (key_id, (meta or {}).get("version"), (meta or {}).get("updated_at"), base_url, model)
        probed_at = self._anthropic_probe_at.get(key)
        if probed_at is not None and (time.time() - probed_at) < ANTHROPIC_PROBE_TTL_SECONDS:
            return
        result = await probe_anthropic(secret, model, base_url)
        self._anthropic_probe_at[key] = time.time()
        return result

    def _remember_adapter(self, cache_key: tuple, adapter: BaseAdapter) -> None:
        """记住 adapter，并淘汰同一凭据的旧版本（避免缓存随轮换无限增长）。"""
        key_id = cache_key[0]
        for old_key in [k for k in self._adapter_cache if k[0] == key_id and k != cache_key]:
            stale = self._adapter_cache.pop(old_key)
            _close_adapter_soon(stale)
        self._adapter_cache[cache_key] = adapter

    async def aclose(self) -> None:
        """应用关闭：按依赖顺序收尾，不留悬挂的任务与等待。

        顺序（后者都依赖前者已经停下来）：

        1. 停后台维护调度；
        2. 停 TurnManager（在跑的那一轮收尾、排队 turn 兑现终态、等待者全部结束）；
        3. 停 TaskManager（取消在跑的独立任务、兑现所有 waiter）；
        4. 释放 adapter / HTTP client。

        数据库连接的关闭由调用方决定（`create_app(..., close_db_on_shutdown=True)`
        时在 lifespan 的最后一步），保证不会出现「后台任务还在写，DB 已经关了」。
        """
        await self.maintenance.stop()
        await self.turns.shutdown()
        await self.task_manager.shutdown()

        adapters, self._adapter_cache = list(self._adapter_cache.values()), {}
        self._anthropic_probe_at.clear()
        for adapter in adapters:
            await _close_adapter(adapter)

    async def build_adapter(self) -> BaseAdapter | None:
        ref = self.resolve_main_ref()
        if ref is None:
            return None
        return await self.build_adapter_for_credential(ref.key_id, ref.default_model)

    # -- 能力与凭据状态广播 ------------------------------------------------

    async def announce_capability(
        self, adapter: BaseAdapter, turn_id: str | None = None
    ) -> None:
        """宣告本轮适配档位（native / text / unsupported）。

        档位没变就什么都不发——正常状态不应该在事件流里刷存在感；
        只有真的降级到兼容文本模式时，额外发一次 `FALLBACK` 说明原因。
        """
        from agent.api.events import EventType, make_event

        mode = getattr(adapter, "mode", None)
        mode_value = mode.value if isinstance(mode, AdapterMode) else str(mode or "")
        if mode_value == self._announced_mode:
            return
        self._announced_mode = mode_value
        data: dict = {"adapter": mode_value, "model": getattr(adapter, "model", None)}
        if turn_id:
            data["turn_id"] = turn_id
        await self.bus.publish(make_event(EventType.CAPABILITY, data))
        if mode_value == AdapterMode.TEXT.value:
            fallback: dict = {
                "from": AdapterMode.NATIVE.value,
                "to": AdapterMode.TEXT.value,
                "reason": "model_without_native_tool_calls",
                "message": "当前模型不支持原生工具调用，已使用兼容模式",
            }
            if turn_id:
                fallback["turn_id"] = turn_id
            await self.bus.publish(make_event(EventType.FALLBACK, fallback))

    async def announce_credential_unavailable(self, turn_id: str | None = None) -> None:
        """主循环没有可用凭据：只说明「现在用不了 + 去哪里加」，不带内部标识。"""
        from agent.api.events import EventType, make_event

        data: dict = {
            "status": "unavailable",
            "scope": "main_loop",
            "reason_code": "no_credential",
            "message": "当前没有可用的模型凭据：请在「设置 → 凭据」里添加一个 API Key",
        }
        if turn_id:
            data["turn_id"] = turn_id
        await self.bus.publish(make_event(EventType.CREDENTIAL_STATUS, data))

    # -- 高影响知识候选 ----------------------------------------------------

    def _knowledge_candidate_ignored(self, category: str, content: str) -> bool:
        """用户忽略过的同一条知识（同类别 + 同内容）不再提示。"""
        rows = self.conn.execute(
            "SELECT provenance FROM knowledge "
            "WHERE category = ? AND content = ? AND state = 'revoked'",
            (category, content),
        ).fetchall()
        for row in rows:
            try:
                provenance = json.loads(row["provenance"] or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(provenance, dict) and provenance.get("ignored_at"):
                return True
        return False

    def knowledge_category_cooling_down(self, category: str) -> bool:
        """这一类高影响知识是不是刚被忽略过（冷却期内不再弹到对话里）。

        忽略时间存在 settings 表里，所以重启后仍然生效 —— 「别再问」是用户的
        长期表态，不能因为一次重启就忘掉。
        """
        from datetime import datetime, timedelta, timezone

        raw = self.settings_store.get(f"{KNOWLEDGE_IGNORE_KEY_PREFIX}{category}")
        if not raw:
            return False
        try:
            ignored_at = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if ignored_at.tzinfo is None:
            ignored_at = ignored_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ignored_at < timedelta(
            minutes=KNOWLEDGE_IGNORE_COOLDOWN_MINUTES
        )

    async def emit_knowledge_candidates(self, ctx: Any = None) -> None:
        """回答完成后，把本轮新建的高影响知识候选发进对话里确认。

        `ctx` 既可以是 `TurnContext`（从中取 `turn_id`），也可以直接是 turn_id
        字符串。低影响候选不进这里；发完清空队列，被忽略过的内容不再重复提示。
        """
        from agent.api.events import EventType, make_event

        pending = self.memory_lifecycle.take_knowledge_candidates()
        if not pending:
            return
        turn_id = ctx if isinstance(ctx, str) else getattr(ctx, "turn_id", None)
        for cand in pending:
            if self._knowledge_candidate_ignored(cand["category"], cand["content"]):
                continue
            if self.knowledge_category_cooling_down(cand["category"]):
                # 刚被忽略过这一类：不再弹到对话里（候选仍留在知识面板待确认）
                continue
            data: dict = {
                "knowledge_id": cand["knowledge_id"],
                "category": cand["category"],
                "content": cand["content"],
                "impact": cand.get("impact", "high"),
                "reason": cand.get("reason"),
            }
            if turn_id:
                data["turn_id"] = turn_id
            await self.bus.publish(make_event(EventType.KNOWLEDGE_CANDIDATE, data))

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
            turn_id_provider=self._active_turn_id,
        )

    def _active_turn_id(self) -> str | None:
        """当前轮的 turn_id：工具创建事件靠它归属到某一轮（拿不到就省略）。"""
        active = self.turns.active
        return getattr(active, "turn_id", None) if active is not None else None

    def tool_executions(self) -> list[dict]:
        """主 Turn 中工具执行的权威事实（供 `/api/runtime/state` 恢复工具卡状态）。

        `TOOL_END` 可能丢在失真区间里，但**服务器仍然知道**这次调用最终是
        success / failed / cancelled —— 这个接口就是把那份事实交出来。

        范围边界（与 `docs/status.md` 记录的一致）：

        * 只报主 Turn 的调用：内部循环（`subagent:*` 等）不是主 Turn，
          产品上也不展示它们的工具明细，所以不返回、也不新增对应 UI；
        * 没有 turn 归属的记录同样不返回（无法证明属于当前主对话）；
        * 快照不能因为一次投影失败就整体失败。
        """
        active = self.turns.active
        active_turn_id = active.turn_id if active is not None else None
        try:
            records = self.tool_state.snapshot(active_turn_id=active_turn_id)
        except Exception:  # noqa: BLE001 - 快照必须始终有返回值
            return []
        return [r for r in records if _is_main_turn_id(r.get("turn_id"))]

    # -- selector refresh -------------------------------------------------

    @staticmethod
    def _indexed_doc_from_row(row) -> dict:
        """memory_index 一行 → Selector 文档（全量重建与增量更新共用同一套构造）。"""
        doc_id = row["index_id"]
        text = " ".join([row["summary"] or "", row["title"] or ""]).strip() or doc_id
        return {
            "doc_id": doc_id,
            "text": text,
            "topic_id": row["topic_id"],
            "entity_ids": json.loads(row["entity_ids"] or "[]"),
            "keywords": json.loads(row["keywords"] or "[]"),
            "created_at": row["created_at"],
        }

    _SELECTOR_DOC_SQL = (
        "SELECT mi.id AS index_id, mi.fragment_id, mi.topic_id, mi.entity_ids, "
        "mi.keywords, mi.title, mi.token_estimate, mi.created_at, f.summary "
        "FROM memory_index mi LEFT JOIN fragments f ON f.id = mi.fragment_id"
    )

    def _refresh_selector(self) -> None:
        """全量重建（启动、修复、检索语义校验时用）。

        **正常路径不再走这里**：每封一块就全量重建会让单次新增的成本随历史
        条数线性增长。正常增量入口是 `_upsert_selector` / `_remove_selector`。
        """
        rows = self.conn.execute(self._SELECTOR_DOC_SQL).fetchall()
        docs = []
        titles: dict[str, str] = {}
        tokens: dict[str, int] = {}
        for row in rows:
            doc_id = row["index_id"]
            docs.append(self._indexed_doc_from_row(row))
            titles[doc_id] = row["title"] or ""
            tokens[doc_id] = row["token_estimate"] or 0
        from agent.selector.base import IndexedDoc

        self.selector.load(
            [IndexedDoc(**d) for d in docs], titles=titles, token_estimates=tokens
        )

    def _upsert_selector(self, index_id: str) -> None:
        """只处理新写入的那一条 memory index 记录。"""
        from agent.selector.base import IndexedDoc

        row = self.conn.execute(
            f"{self._SELECTOR_DOC_SQL} WHERE mi.id = ?", (index_id,)
        ).fetchone()
        if row is None:
            return
        self.selector.upsert(
            IndexedDoc(**self._indexed_doc_from_row(row)),
            title=row["title"] or "",
            token_estimate=row["token_estimate"] or 0,
        )

    def _remove_selector(self, index_id: str) -> None:
        self.selector.remove(index_id)

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

    async def _publish_turn_event(self, name: str, data: dict) -> None:
        """TurnManager 的 TURN_START / TURN_END 出口（唯一的一处）。"""
        from agent.api.events import EventType, make_event

        await self.bus.publish(make_event(EventType(name), data))

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
            self.approvals.set_context(turn_id=ctx.turn_id)
            adapter = await self.build_adapter()
            if adapter is None:
                ctx.result = {"ok": False, "reason": "no_credential"}
                ctx.status = "unavailable"
                ctx.error = "no_credential"
                return
            topic = self.current_topic()
            ctx.current_topic = topic
            # 系统驱动的轮也要有自己的绑定（归属明确），并且标记成 system：
            # 它不占用户轮次容量，也不该被当成「用户开了新的一轮」。
            ctx.bound_topic = topic
            ctx.bound_fragment_id = None
            self.bindings.record_binding(ctx.turn_id, topic, system=True)
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
                # 系统驱动的轮也是主 Turn：它的工具结果同样要能从快照恢复
                tool_state=self.tool_state,
                is_cancelled=lambda: ctx.cancelled,
            )
            ctx.loop = loop
            try:
                result = await loop.run(prompt)
            finally:
                ctx.loop = None
            if ctx.cancelled or result.cancelled:
                ctx.cancelled = True
                self.trace_store.finish(ctx.turn_id, "cancelled")
                return
            # feedback enters memory (assistant message; no user message)
            notify_msg_id, _ = self.memory.append_message(
                topic_id=topic,
                role="assistant",
                content=result.final_content or "",
                content_type="text",
                model=adapter.model,
                turn_id=ctx.turn_id,
            )
            tracer.write("messages", notify_msg_id)
            ctx.final_content = result.final_content
            ctx.result = {"ok": True, "turn": result.__dict__}
            ctx.usage = {
                "iterations": result.iterations_used,
                "tokens": result.tokens_used,
                "tool_calls": result.tool_calls_made,
            }
            fragment = self.fragments.get_or_create_open(topic)
            if self.fragments.should_close(fragment):
                closed = await self._close_fragment(topic, adapter, tracer=tracer)
                if closed is not None:
                    # 索引在 close_fragment 内部已增量更新（不再全量重建）
                    self.predictor.refresh_topic_vector(topic)
            self.trace_store.finish(
                ctx.turn_id, "done", final_topic=topic, final_preview=result.final_content or ""
            )
        except Exception as exc:  # noqa: BLE001 - notify turn must not crash
            logger.warning("notify turn failed: %s", exc)
            self.trace_store.finish(ctx.turn_id, "failed", error=str(exc)[:200])
            ctx.result = {"ok": False, "reason": "notify_failed"}
            ctx.status = "failed"
            ctx.error = f"{type(exc).__name__}: {str(exc)[:180]}"
        finally:
            self._notify_turn = False
            self.approvals.set_context(turn_id=None)

    # -- short-term memory & topic helpers ---------------------------------

    def _focus_block(self, topic_id: str, fragment_id: str | None) -> str:
        """委派给 ContextAssembler（见 agent/services/context.py）。"""
        return self.context_assembler.focus_block(topic_id, fragment_id)

    def anchor_fragment_info(self) -> dict | None:
        """委派给 ContextAssembler。"""
        return self.context_assembler.anchor_fragment_info()

    def _short_term_items(
        self,
        topic_id: str,
        exclude_message_id: str | None = None,
        fragment_id: str | None = None,
    ) -> list:
        """委派给 ContextAssembler。"""
        return self.context_assembler.short_term_items(
            topic_id, exclude_message_id=exclude_message_id, fragment_id=fragment_id
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
        focus_item_id: str | None = None,
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
            focus_item_id=focus_item_id,
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
        # 提交时捕获「此刻待落实的接续选择」：之后用户再做的新选择只影响后续提交
        pending = self.bindings.peek_intent()
        ctx = self.turns.submit(
            message, topic_id, intent_id=pending.intent_id if pending else None
        )
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

    def session_messages_page(
        self, topic_id: str, *, limit: int = SESSION_PAGE_DEFAULT_LIMIT, before: str | None = None
    ) -> dict:
        """一页历史消息（从**最近**往前翻）。

        长度不再随机器增长：首次只给最近一页（默认 200 条），用户往上读时
        再用 `next_before` 游标取更早的一页。

        游标是 `created_at|id` 的复合键。只按 `created_at` 分页时，同一时刻写入的
        多条消息会被整段跳过或整段重复 —— 复合游标 + `id` 兜底排序才能保证
        「不丢、不重、顺序正确」。
        """
        limit = max(1, min(int(limit or SESSION_PAGE_DEFAULT_LIMIT), SESSION_PAGE_MAX_LIMIT))
        params: list[Any] = [topic_id]
        where = "fragment_id IN (SELECT id FROM fragments WHERE topic_id = ?)"
        cursor = _parse_history_cursor(before)
        if cursor is not None:
            created_at, message_id = cursor
            where += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params.extend([created_at, created_at, message_id])
        params.append(limit + 1)  # 多取一条判断是否还有更早的
        rows = self.conn.execute(
            "SELECT id, role, content, content_type, created_at FROM messages "
            f"WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        page = [dict(r) for r in rows]
        has_more = len(page) > limit
        page = page[:limit]
        page.reverse()  # 页面内部仍按时间升序
        next_before = (
            _format_history_cursor(page[0]["created_at"], page[0]["id"]) if has_more and page else None
        )
        return {"messages": page, "has_more": has_more, "next_before": next_before}

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


def make_warning(message: str, turn_id: str | None = None):
    from agent.api.events import EventType, make_event

    data = {"code": "app", "message": message}
    if turn_id:
        data["turn_id"] = turn_id
    return make_event(EventType.WARNING, data)


def make_error(code: str, message: str, recoverable: bool, turn_id: str | None = None):
    from agent.api.events import EventType, make_event

    data = {"code": code, "message": message, "recoverable": recoverable}
    if turn_id:
        data["turn_id"] = turn_id
    return make_event(EventType.ERROR, data)


# ---------------------------------------------------------------------------
# 历史分页游标
# ---------------------------------------------------------------------------


def _format_history_cursor(created_at: str | None, message_id: str) -> str:
    """`created_at|id`：同一时刻写入的多条消息也能稳定排序。"""
    return f"{created_at or ''}|{message_id}"


def _parse_history_cursor(cursor: str | None) -> tuple[str, str] | None:
    """解析历史游标；无法解析（旧格式 / 坏值）时返回 None = 从最新开始。"""
    if not cursor or "|" not in cursor:
        return None
    created_at, _, message_id = cursor.rpartition("|")
    if not message_id:
        return None
    return created_at, message_id


# ---------------------------------------------------------------------------
# Adapter / HTTP client 释放
# ---------------------------------------------------------------------------


async def _close_adapter(adapter: BaseAdapter) -> None:
    """释放 adapter 持有的网络资源；任何失败都只记日志，不影响关闭流程。"""
    closer = getattr(adapter, "close", None)
    if callable(closer):
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
            return
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.warning("failed to close adapter %r", type(adapter).__name__, exc_info=True)
            return
    client = getattr(adapter, "_client", None)
    client_close = getattr(client, "close", None)
    if not callable(client_close):
        return
    try:
        result = client_close()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.warning("failed to close model client", exc_info=True)


def _close_adapter_soon(adapter: BaseAdapter) -> None:
    """同步上下文里淘汰旧 adapter：交给事件循环异步关闭。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # 没有事件循环（例如测试构造阶段）：交给 GC
    asyncio.create_task(_close_adapter(adapter))
