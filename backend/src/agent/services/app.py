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
import uuid
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
from agent.credentials.policy import (
    MAIN_LOOP_USAGE_TAGS as _POLICY_MAIN_LOOP_USAGE_TAGS,
    CredentialPolicy,
    CredentialRef,
)
from agent.credentials.providers import (
    resolve_endpoint as resolve_credential_endpoint,
    resolve_model as resolve_credential_model,
    uses_anthropic as credential_uses_anthropic,
)
from agent.credentials.store import CredentialStore
from agent.graph.anchors import AnchorService
from agent.graph.topics import TopicService
from agent.memory.fragment import FragmentManager
from agent.memory.index import IndexBuilder
from agent.memory.ingest import MemoryWriter
from agent.security import internal_data
from agent.selector.selector import Selector
from agent.services.heavy import HeavyWork
from agent.services.lifecycle import (
    PHASE_ADAPTERS_KEPT,
    PHASE_ADAPTERS_RELEASED,
    PHASE_CLEAN_EXIT_RECORDED,
    PHASE_CLEAN_EXIT_SKIPPED,
    PHASE_UNDERSTANDING_STOPPED,
    PHASE_UNDERSTANDING_UNFINISHED,
    UNDERSTANDING_TASK_PREFIX,
    CloseReport,
    close_background,
    close_decision,
    log_close_report,
    merge_close_reports,
)
from agent.credentials.usages import legacy_tags_for_usage as _legacy_tags_for_usage
from agent.services.usage_target import (
    REASON_ADAPTER_UNAVAILABLE,
    REASON_NO_CREDENTIAL,
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    SOURCE_POLICY,
    UsageTarget,
    UsageUnavailable,
    is_explicit_assignment as _is_explicit_usage_assignment,
    read_usage_assignments as _read_usage_assignments,
    resolve_shared_target as _resolve_shared_usage_target,
    resolve_usage_target as _resolve_usage_target,
)
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
# 主对话候选标签的唯一来源在 credentials/policy.py（用途解析也用同一批）。
# 这里保留同名常量：既有导入点（onboarding / 测试）不因这次重构而断。
MAIN_LOOP_USAGE_TAGS = list(_POLICY_MAIN_LOOP_USAGE_TAGS)

#: 后台三类用途 → 派生任务 kind（derived_tasks.KIND_*）。
#: 三者**各自独立**解析用途并各自建 adapter：以前三条都复用主 Turn 的同一个 adapter，
#: 于是「换掉记忆整理用的模型」在界面上看起来生效、实际毫无作用。
DERIVED_USAGE_BY_KIND = {
    "summary": "memory-organize",
    "knowledge": "knowledge-distill",
    "entities": "entity-extract",
}
BUDGET_RATIO = 0.25

# 用户忽略过某类高影响知识后，这段时间内不再把同类候选弹到对话里。
# 为什么按类别而不是按句子：模型每次的说法都不一样（「用户偏好中文回复」/「用户更喜欢
# 简洁的解释」），只比字符串会让用户刚说完「忽略」又被问一遍近乎同一件事。
# 被压下的候选仍然留在知识面板的待确认列表里（不丢数据，只是不再打扰）。
KNOWLEDGE_IGNORE_COOLDOWN_MINUTES = 30
KNOWLEDGE_IGNORE_KEY_PREFIX = "knowledge.ignored_at."

# Anthropic 能力探测的缓存时长：能力档位不会频繁变化，没必要每个 Turn 实测一次。
ANTHROPIC_PROBE_TTL_SECONDS = 3600.0

# adapter 缓存的条数上限（按「凭据 + 模型」槽位算）。超出时淘汰最久没用的槽位 ——
# 淘汰只是**松开引用**，不是关闭连接：仍被后台工作持有的 adapter 不能被关掉。
ADAPTER_CACHE_LIMIT = 16
# 被新配置顶替下来的旧 adapter 暂留条数上限。留一点是为了「换 Key 那一刻已经开始
# 的工作」还能用自己的连接收尾，同时不让历史连接无限堆积。
ADAPTER_RETIRED_LIMIT = 16

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


def _record_understanding_phase(
    report: CloseReport,
    *,
    registered: tuple[str, ...],
    unfinished: tuple[str, ...],
) -> CloseReport:
    """把互动理解的收尾结果记进关闭报告（整合轮契约 §2，与 maintenance_* 同构）。

    * 有理解后台任务未确认结束 → understanding_unfinished（名字已经在
      report.unfinished 里，clean=False 由 CloseReport 自己保证）；
    * 有理解后台任务且都确认结束 → understanding_stopped；
    * 本次关闭根本没有理解后台任务 → 阶段名一个都不记（没发生的事不写进报告）。

    「有没有」按 registered（关闭**之前**的注册表快照）判定，不是按关闭报告的名单：
    干净收尾的名字会被撤下账，只看报告会把「关停了一个正在跑的理解」说成
    「这次关闭跟理解无关」。
    """
    if not registered:
        return report
    if unfinished:
        return report.with_phase(PHASE_UNDERSTANDING_UNFINISHED)
    return report.with_phase(PHASE_UNDERSTANDING_STOPPED)


#: 子任务通知轮的确定性 request_id 前缀（TurnManager 幂等索引按它查重）。
SUBTASK_NOTIFICATION_REQUEST_ID_PREFIX = "subtask-notify:"


class AppContext:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bus: EventBus) -> None:
        self.settings = settings
        self.conn = conn
        self.bus = bus
        # 本进程实例身份 + 实例归属（契约 C1）。必须在任何「会写下带归属的记录」
        # 的服务（审批、turn 台账）之前建立并 start()：先有身份，才有归属可写。
        # 这里生成 instance_id（create_app 复用它），保证 HTTP 层、事件、台账
        # 看到的是**同一个**实例身份。
        from agent.storage.instance_registry import InstanceRegistry

        self.instance_id = f"qio_{uuid.uuid4().hex[:16]}"
        self.instances = InstanceRegistry(conn, self.instance_id, pid=os.getpid())
        self.instances.start()
        # R05：派生任务队列要认同一份实例身份与存活判据。不绑的话 owner 会写成
        # NULL，恢复只能退化成「超时兜底」，「刚认领就重启」就恢复不了。
        from agent.services.derived_tasks import bind_instance as _bind_derived_instance

        _bind_derived_instance(self.instance_id, self.instances)
        # M06：后台记忆任务的统一注册表。编排器调度（registry_for）与 aclose 收尾
        # 必须拿到同一份，否则关闭时等不到正在跑的提炼任务。
        from agent.services.background import BackgroundTasks as _BackgroundTasks

        self.background = _BackgroundTasks()
        # 整合轮 §2：互动理解的后台调度要登记进**同一份**注册表。连接是它唯一的
        # 输入，调度点上再没有别的应用入口，所以在建立注册表的这一刻把归属记下来
        # （submission.bind_background_owner）；aclose 收的是同一份注册表，于是
        # 「理解期间关闭」不再依赖调用方记得传 owner。
        from agent.interactive.submission import bind_background_owner as _bind_understanding

        _bind_understanding(conn, self)
        self.credentials = CredentialStore(conn)
        self.settings_store = SettingsStore(conn)
        from agent.trace.store import TraceStore

        self.trace_store = TraceStore(
            conn, enabled=self.settings_store.get("trace.enabled", "true") != "false"
        )
        self.policy = CredentialPolicy(self.credentials)
        # 用途解析的最近一次结论（**只含凭据 id 与模型名，不含密钥**）：
        # 界面 / 事件 / 后台任务据此说明「这个用途现在凭什么在用它」，以及
        # 「默认模型不可用，已回退到 X（原因）」。没有条目 = 本轮还没解析过。
        self.usage_targets: dict[str, UsageTarget] = {}
        self.usage_unavailable: dict[str, UsageUnavailable] = {}
        self.probe_cache = ProbeCache()
        self.context_registry = ContextLengthRegistry()
        # Adapter / 底层 HTTP client 复用：每个 Turn 重新建连接池是没有必要的
        # （新 TCP/TLS 握手 + 新连接池），能力探测结果也不该每轮重测。
        # key = (key_id, credential version, updated_at, endpoint, model)
        self._adapter_cache: dict[tuple, BaseAdapter] = {}
        # 槽位索引：(key_id, model) -> 当前缓存键。**同一个槽位**换配置才顶替旧的；
        # 同一凭据的不同模型是不同槽位，可以同时存在（A：以前会被互相关闭）。
        self._adapter_slots: dict[tuple, tuple] = {}
        # 被顶替 / 淘汰下来、暂时还留着强引用的旧 adapter（有界，见 _retire_adapter）。
        # 运行期**不关闭**它们：可能还有已经开始的工作在用，关闭会打断那次调用。
        self._adapter_retired: list[tuple[tuple, BaseAdapter]] = []
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

        # 本次进程的会话身份：审批创建时带上它，应答时客户端把它回传，
        # approval.respond 比对不一致就拒绝（见 tools/approval.py::set_context 里
        # 「这是绑定校验、不是访问控制」的说明）。以前这个字段永远是 NULL，
        # 那条比对因此从来没有生效过 —— 一个看起来在校验、实际不会触发的检查。
        self.session_id = f"sess_{uuid.uuid4().hex[:12]}"

        # 带上数据库连接：等待中的审批要落库，重启后才能说清「那次操作没有执行」
        # （见迁移 23 与 tools/approval.py 的 interrupted）。
        # registry 让「上一次进程留下的 pending」按**实例归属**判定（契约 C1）：
        # 归属者还活着的审批一行都不动。
        self.approvals = ApprovalService(bus, conn=conn, registry=self.instances)
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

        # 搜索参数（返回条数 / 正文预算）从设置实时读取：设置在界面上可改，
        # 运行期必须跟着变（见 tools/web_search.py / web_fetch.py 的同名注释）。
        self.registry.register(WebSearchTool(self.search_service, self.settings_store))
        self.registry.register(WebFetchTool(settings=self.settings_store))
        # 电脑操控：共享沙箱 + 文件系统/命令/进程工具（分级授权，走 ComputerSandbox）
        from agent.services.computer import ComputerSandbox, DEFAULT_MODE
        from agent.tools.fs_tools import (
            FsReadTool,
            FsWriteTool,
            FsPatchTool,
            FsListTool,
            FsFindTool,
            FsInfoTool,
            FsDeleteTool,
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
        # 启动即就位：工作区根目录不存在时，所有相对路径的文件工具都会直接失败
        self.computer.ensure_root()
        self.services.register("computer", self.computer)

        # ── R9 统一授权边界（契约 §1/§2/§5）──────────────────────────────
        # 权限判定、指令授权、可恢复审批、内部数据保护共用同一套实现：
        # 工具只能走 AuthorizationEngine；审批 v2 由 ApprovalService 持久化。
        from agent.security.engine import AuthorizationEngine
        from agent.security.instructions import InstructionRegistry
        from agent.security.operations import OperationRegistry
        from agent.security.tempfiles import TempFileManager

        self.instructions = InstructionRegistry()
        self.operations = OperationRegistry()
        self.tempfiles = TempFileManager(Path(self.settings.data_dir) / "tmp")
        # R9-F：受限模式模型授权审查 / 需求台账 / 有界授权记录 / 后端文件来源。
        # 这些模块由并行分支提供；未合入或初始化失败时明确降级为"没有模型审查"，
        # 受限模式退回普通审批（不默认放行，也不会自动切模式）。
        self.requirements = None
        self.authorizations = None
        self.review_audit = None
        self.reviewer = None
        self.requirement_resolver = None
        try:
            from agent.security.requirements import (
                AuthorizationLedger,
                RequirementLedger,
            )
            from agent.security.review import (
                ModelAuthorizationReviewer,
                ReviewAuditLedger,
            )

            self.requirements = RequirementLedger(conn)
            self.authorizations = AuthorizationLedger(conn)
            self.review_audit = ReviewAuditLedger(conn)
            self.reviewer = ModelAuthorizationReviewer(
                adapter_factory=self.build_adapter,
                audit=self.review_audit,
            )
            # R9-G：模型理解"这条用户消息是新要求/补充/修正/撤回"；不是授权审查，
            # 不产生许可；失败时后端按新要求继续并标注 unavailable（不关键词兜底）。
            try:
                from agent.security.resolution import ModelRequirementResolver
                import inspect as _inspect

                params = _inspect.signature(ModelRequirementResolver).parameters
                resolver_kwargs = {"adapter_factory": self.build_adapter}
                if "requirements" in params:
                    resolver_kwargs["requirements"] = self.requirements
                if "audit" in params and "audit" in _inspect.signature(
                    ModelRequirementResolver
                ).parameters:
                    resolver_kwargs["audit"] = self.requirements
                self.requirement_resolver = ModelRequirementResolver(**resolver_kwargs)
            except Exception as exc:  # noqa: BLE001 - 解析器不可用：按新要求继续
                logging.getLogger(__name__).warning(
                    "R9-G 要求解析器未启用（本次按新要求继续）：%s: %s",
                    type(exc).__name__,
                    exc,
                )
        except Exception as exc:  # noqa: BLE001 - 降级必须可见，不静默假装已启用
            logging.getLogger(__name__).warning(
                "R9-F 授权审查组件未启用（受限模式退回普通审批）：%s: %s",
                type(exc).__name__,
                exc,
            )
        # R9-H2 §5：内部运行数据真实对象身份登记（C 分支）。失败只告警，路径判定
        # 仍兜底（不能因为登记不可用就静默把已注册对象当普通文件）。
        self.internal_data_identity = None
        self.internal_data_identity_error: str | None = None
        try:
            from agent.security.internal_data_identity import InternalDataIdentityStore

            self.internal_data_identity = InternalDataIdentityStore(conn)
            counts = self.internal_data_identity.sync(
                self._internal_data_paths(), source="app_startup"
            )
            logging.getLogger(__name__).info(
                "内部数据身份登记完成：added=%s retired=%s active=%s",
                (counts or {}).get("added"),
                (counts or {}).get("retired"),
                (counts or {}).get("active"),
            )
            health = getattr(self.internal_data_identity, "health", None)
            if callable(health):
                last_error = (health() or {}).get("last_error")
                if last_error:
                    self.internal_data_identity_error = str(last_error)
            # R9-H4 升级兼容：旧行 birth NULL 且登记路径仍存在时，有界回填一次；
            # 只读已登记路径，不扫描全盘。已移动且原路径不存在的旧对象无法回填，
            # 保持历史路径/保守边界（不凭裸编号声称同一文件）。
            self._backfill_identity_births()
        except Exception as exc:  # noqa: BLE001 - 身份登记失败不静默降级；路径判定仍兜底
            self.internal_data_identity_error = type(exc).__name__ + ": " + str(exc)
            logging.getLogger(__name__).warning(
                "R9-H2 内部数据身份登记未启用（路径判定仍兜底）：%s: %s",
                type(exc).__name__,
                exc,
            )
        self.authorization = AuthorizationEngine(
            conn=conn,
            approvals=self.approvals,
            settings=self.settings_store,
            instance_id=self.instance_id,
            workspace_root=lambda: self.computer.root(),
            internal_data_paths=self._internal_data_paths,
            requirements=self.requirements,
            authorizations=self.authorizations,
            reviewer=self.reviewer,
            origin_provider=self._file_origin_for,
            # 引擎按真实对象身份判定内部数据；登记不可用时返回空集合，路径判定兜底。
            internal_data_identity_keys=lambda: self._internal_identity_keys(),
            internal_data_known_paths=lambda: self._internal_identity_known_paths(),
            # R9-H4 §8：identity + birth 生命周期查询（编号复用不误认）。
            internal_data_identity_lookup=lambda *args, **kwargs: (
                self._internal_identity_lookup(*args, **kwargs)
            ),
            internal_data_identity_tokens=lambda: self._internal_identity_tokens(),
        )
        # R9-G：统一执行前重核对服务（模式/要求/授权/审批/对象/内容/条件）；
        # 挂在 AppContext 与引擎上，执行器/审批恢复可按统一接口调用。
        self.recheck = None
        try:
            from agent.security.recheck import RecheckService

            self.recheck = RecheckService(
                engine=self.authorization,
                requirements=self.requirements,
                approvals=self.approvals,
                authorizations=self.authorizations,
            )
            self.authorization.recheck = self.recheck
        except Exception as exc:  # noqa: BLE001 - 重核对不可用不能静默放行
            logging.getLogger(__name__).warning(
                "R9-G 执行前重核对服务未启用：%s: %s", type(exc).__name__, exc
            )
        self.approvals.set_engine(self.authorization)
        self.approvals.set_operations(self.operations)
        self.approvals.set_instruction_registry(self.instructions)
        self.approvals.set_notifier(self._on_approval_executed)
        self.services.register("security", self.authorization)
        self.services.register("operations", self.operations)
        self.services.register("tempfiles", self.tempfiles)
        # 工具审批走 deferred：批准后由后台执行器按 kind 取出执行函数真正执行。
        self.operations.register("tool.invoke", self._execute_approved_tool)
        # 审批通过后的真实执行器（契约 §5.3）：fs.write/fs.patch/fs.delete、proc.*、
        # subagent.create；没有实现的动作显式注册「不可用」，不允许批准后静默丢单。
        from agent.tools.approved_operations import register_approved_operations

        self.approved_operation_kinds = register_approved_operations(
            self.operations,
            computer=self.computer,
            security=self.authorization,
            tool_lookup=self.registry.get,
            # AttachmentService 由 create_app 稍后注册到 services；用 getter 延迟取。
            attachments_getter=lambda: self._attachment_service(),
        )
        # R9-H3 §4：可信写入（审批通过后的真实执行器）提交成功后立即更新内部对象
        # 身份；失败记录明确错误，不静默降级，也不影响已成功的真实文件操作。
        self._install_trusted_write_identity_hook()
        for _tool in (
            FsReadTool(),
            FsWriteTool(),
            FsPatchTool(),
            FsListTool(),
            FsFindTool(),
            FsInfoTool(),
            FsDeleteTool(),
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
        from agent.tools.subagent_tool import (
            AwaitTaskTool,
            ReadTaskResultTool,
            set_origin_provider,
        )
        from agent.tools.subagent_tool import SUBTASK_CONTROL_KEY as _SUBTASK_CONTROL_KEY  # noqa: F401
        from agent.tools.subtask_notifications import SubtaskNotificationQueue
        from agent.tools.task_manager import TaskManager

        # 对话页任务栏：子任务生命周期同步写进 task_feed（展示事实，不替代执行）。
        self.task_manager = TaskManager(bus, on_change=self._on_subagent_task_change)
        self.services.register("task_manager", self.task_manager)
        # 阶段四 A / FU2-D：子任务完成通知先**持久化**入队（task_id 主键去重），空闲时
        # 各自成为一条通知轮。投递状态在库里（delivered_at 才是交付判据）：重启可恢复、
        # 失败保留待重试、超过告警阈值只告警不丢弃。入队 ≠ 交付。
        self.subtask_notifications = SubtaskNotificationQueue(self.task_manager, conn=conn)
        # 投递失败后的延迟重试（秒）；只在没有 active / queued 用户轮时才会真正提交。
        self.subtask_notification_retry_delay = 10.0
        self._notify_retry_task: asyncio.Task | None = None
        # 阶段四 A：程序绑定子任务 origin（当前真实用户轮 + 原话题），模型不能伪造。
        set_origin_provider(self._subtask_origin)
        from agent.tools.credential_catalog import ListAvailableCredentialsTool

        self.registry.register(
            ListAvailableCredentialsTool(
                self.credentials, settings_store=self.settings_store
            )
        )
        self.registry.register(
            AwaitTaskTool(self.task_manager, notify_handler=self._handle_subagent_notify)
        )
        self.registry.register(ReadTaskResultTool(self.task_manager))
        from agent.tools.approval import ApprovalService
        from agent.tools.dev_tools import (
            CreateToolTool,
            DevListFilesTool,
            DevListTasksTool,
            DevReadFileTool,
            DevRunTestsTool,
            DevSubmitTool,
            DevWriteFileTool,
        )
        from agent.tools.dev_workspace import DevWorkspace
        from agent.tools.sandbox import SandboxExecutor
        from agent.tools.tool_envs import ToolEnvManager

        self.dev_workspaces = DevWorkspace(settings.data_dir / "dev-workspaces")
        # 工具执行器只建一份：开发测试、提交复测与注册后的真实执行走同一套
        # 超时 / 环境 / 输出上限（见 tools/sandbox.py）。
        # R9 执行层约束（契约 §3.1）：执行器同时知道当前模式、统一授权网关与
        # 受保护范围 —— read_only/restricted 的脚本约束在应用内真正生效。
        self.tool_sandbox = self._make_tool_sandbox()
        # 项目级专用依赖环境（QIO 管理）：声明了第三方依赖的工具用它跑，
        # 没准备好就明确失败 —— 不静默回落到随包解释器。
        self.tool_envs = ToolEnvManager(Path(settings.data_dir) / "tool-envs")
        # 建工具是一条流程、一张卡：这些工具把阶段事件发到同一个出口
        self.registry.register(
            CreateToolTool(
                self.dev_workspaces,
                bus=self.bus,
                turn_id_provider=self._active_turn_id,
            )
        )
        self.registry.register(DevListFilesTool(self.dev_workspaces))
        self.registry.register(DevListTasksTool(self.dev_workspaces))
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
                sandbox=self.tool_sandbox,
                approvals=self.approvals,
                bus=self.bus,
                turn_id_provider=self._active_turn_id,
                envs=self.tool_envs,
            )
        )
        self.registry.register(
            DevSubmitTool(
                self.dev_workspaces,
                lifecycle_builder=self._build_tool_lifecycle,
                sandbox=self.tool_sandbox,
                approvals=self.approvals,
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
        # 阶段协议的状态机（plan §1.2）：turn_id → StageTracker，纯内存；
        # 落库 / 广播在 _on_narrative 与 TURN_END 出口完成。
        from agent.core.stage import StageRegistry

        self.stages = StageRegistry()
        self.turns = TurnManager()
        self.turns.set_runner(self._execute_turn)
        self.turns.set_publisher(self._publish_turn_queue)
        # TurnManager 是 turn 生命周期的唯一事实源：TURN_START / TURN_END 只由它发。
        self.turns.set_emitter(self._publish_turn_event)
        # turn 队列台账（迁移 25）：被 API 接受过的消息跨重启不丢。
        # 台账带上本实例身份（契约 C1）+ 归属表；`submit()` 里「先持久化成功、
        # 才入队返回」（契约 C2）就靠这个 journal。
        from agent.storage.turn_journal import TurnJournal

        self.turn_journal = TurnJournal(
            conn, instance_id=self.instance_id, registry=self.instances
        )
        self.turns.set_journal(self.turn_journal)
        # 启动恢复：只处理**确认已退出实例**留下的 queued / running —— 别的实例
        # 还在跑（或判不出来）时一行都不改，只留痕、**不自动重放**。
        self.recovered_turns = self.turn_journal.interrupt_stale(self.instances)
        if self.recovered_turns:
            logger.warning(
                "上一个进程留下了 %s 条没有执行的用户消息（不会自动重放，等用户决定）",
                len(self.recovered_turns),
            )
        self.turn_journal.prune_terminal()
        self.registry.register(
            CorrectKnowledgeTool(conn, snapshot_provider=self._knowledge_snapshot_provider)
        )
        # Phase 1 C：主对话新增知识一律提交审批；实体由 QIO 自主创建（证据必须在本轮目录）。
        from agent.tools.entity_create import CreateEntityTool
        from agent.tools.knowledge_propose import ProposeKnowledgeTool

        self.registry.register(
            ProposeKnowledgeTool(
                conn,
                evidence_provider=self._turn_evidence_catalog,
                turn_id_provider=self._active_turn_id,
            )
        )
        self.registry.register(
            CreateEntityTool(conn, evidence_provider=self._turn_evidence_catalog)
        )
        from agent.storage.tool_store import ToolStore

        self.tool_store = ToolStore(conn)
        self.services.register("tool_store", self.tool_store)
        self._restore_tools()
        from agent.tools.declare_completion import DeclareCompletionTool

        # 「测试通过 / 已注册 / 现在可以使用」这类结论必须过一遍后端事实
        # （见 core/turn_facts.py 与 docs/superpowers/specs/2026-09-30-final-answer-fact-check-design.md）。
        # 它只读：不改任务状态、不触发审批、不注册任何东西。
        self.registry.register(
            DeclareCompletionTool(self.dev_workspaces, self.registry, self.tool_store)
        )
        # 阶段三契约 §13.6：互动板面专属工具。它们始终注册在注册表里（便于服务端
        # 拒绝越权调用），但普通模式的模型消息里不出现（turn_orchestrator 过滤 specs）；
        # 工具自身还要求 interactive/runtime.py 里程序验证过的板面绑定。
        from agent.interactive.board_tools import build_board_tools

        for board_tool in build_board_tools(self):
            self.registry.register(board_tool)
        self._notify_turn = False
        # 上一次宣告过的适配档位：正常状态不制造噪声，只有档位变化才广播
        self._announced_mode: str | None = None
        from agent.services.maintenance import MaintenanceScheduler
        from agent.services.tool_router import ToolRouter

        self.tool_router = ToolRouter(embedding=self.embedding)
        self.maintenance = MaintenanceScheduler(self)
        # 阶段二：独立知识维护（调度器 + 执行器）。生命周期由 MaintenanceScheduler
        # 的 start()/stop() 统一托管（见 services/maintenance.py），与工具清理 / 自动化建议分开。
        from agent.maintenance.runner import MaintenanceRunner
        from agent.maintenance.scheduler import KnowledgeMaintenanceScheduler

        self.maintenance_runner = MaintenanceRunner(self)
        self.knowledge_maintenance = KnowledgeMaintenanceScheduler(
            conn, runner=self.maintenance_runner, settings=self.settings_store
        )
        # 重活执行器（契约 WS3 §3）：把纯计算（ONNX 推理等）搬出事件循环，
        # 有并发上限。输入读取与结果提交仍留在事件循环一侧（见 services/heavy.py）。
        self.heavy = HeavyWork()
        self._refresh_selector()
        # 启动清理一次工具输出：把保留天数调小之后，重启也立刻生效。
        # 失败只记日志 —— 清理是维护动作，不能挡住启动。
        self.prune_tool_outputs()
        # 「整条记录保留天数」默认 0（永久保留，行为与以前一致）；设了天数才清理。
        self.prune_tool_records()
        # 启动恢复（归属判定）最后做一次收口：只处理确认已退出实例留下的记录，
        # unknown / 旧记录一律保留原状态。见下面方法的长注释。
        try:
            self.recover_instance_owned_records()
        except Exception:  # noqa: BLE001 - 恢复失败不该让应用起不来
            logger.warning("instance owned recovery failed", exc_info=True)
        # FU2-D：上一次进程没送达的通知还在库里 —— 启动后继续投递（不靠用户先说话）。
        self._prime_subtask_notification_delivery()

    # -- 实例归属的启动恢复（契约 C1）--------------------------------------

    def _recover_owned_derived_tasks(self, instance_id: str, record_type: str) -> int:
        """把「确认已退出实例」的派生任务重新交给后续 drain（**不在这里执行**）。

        这里只改状态：把该实例名下仍卡在 running 的任务放回可重试，并把原因
        写成一条可读的标记（不是错误、是「上次那个实例退出了」）。
        `claim_due` 之后会照常取走它们 —— 恢复清单可见，但**不自动执行**任何副作用。
        """
        from agent.services.derived_tasks import STATE_PENDING
        from agent.storage.instance_registry import RECORD_DERIVED_TASK

        owner_column = self._derived_owner_column()
        if owner_column is None:
            # 列不存在（降级路径）：用归属表给出的 id 逐条更新。
            ids = self.instances.owned_record_ids(RECORD_DERIVED_TASK, instance_id)
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            cur = self.conn.execute(
                f"UPDATE derived_tasks SET state = ?, updated_at = ? "
                f"WHERE id IN ({placeholders}) AND state = 'running'",
                (STATE_PENDING, _now_iso(), *ids),
            )
            return int(cur.rowcount or 0)
        cur = self.conn.execute(
            "UPDATE derived_tasks SET state = ?, updated_at = ?, owner_instance_id = NULL, "
            "last_error = COALESCE(last_error, '上次执行的实例已退出，这次改写没有完成') "
            "WHERE owner_instance_id = ? AND state = 'running'",
            (STATE_PENDING, _now_iso(), str(instance_id)),
        )
        return int(cur.rowcount or 0)

    def _derived_owner_column(self) -> str | None:
        """派生任务表有没有 owner_instance_id 列（没有就降级，绝不假设它存在）。"""
        try:
            cols = [
                str(row[1])
                for row in self.conn.execute("PRAGMA table_info(derived_tasks)").fetchall()
            ]
        except sqlite3.Error:
            return None
        return "owner_instance_id" if "owner_instance_id" in cols else None

    def recover_instance_owned_records(self) -> dict:
        """启动恢复的**归属判定**入口（契约 C1）。

        队列台账与待确认事项在各自的构造阶段已经按归属判过（见
        `turn_journal.interrupt_stale(registry)` 与 `ApprovalService._mark_interrupted`）；
        这里补派生任务，并把「有多少条因为归属未知而没动」如实记进日志。

        规则（三条，逐条对应契约）：
        1. 归属者确认已退出 → 记录标成可恢复（起因为「那个实例退出了」）；
        2. 归属者还活着 → 一行不动（别抢正在工作的实例的任务）；
        3. 归属未知（unknown / 没有归属的旧记录）→ 保守保留原状态，只计数。
        """
        from agent.storage.instance_registry import RECORD_DERIVED_TASK

        report = self.instances.recover_confirmed_dead(
            types=(RECORD_DERIVED_TASK,),
            handler=self._recover_owned_derived_tasks,
        )
        self.instance_recovery_report = report
        legacy = self._legacy_unowned_counts()
        report["legacy_unowned"] = legacy
        if report["deferred"] or any(legacy.values()):
            logger.info(
                "实例归属未知，保守保留原状态：%s；无归属的旧记录：%s",
                report["deferred"],
                legacy,
            )
        return report

    def _legacy_unowned_counts(self) -> dict[str, int]:
        """没有归属的旧记录条数（保守保留、不自动中断的证据）。

        这些行来自「还没有实例身份」的旧版本：无法判断它们的写入者还在不在，
        所以只统计、不改状态 —— 界面上它们仍然按原状态呈现（interrupted 的
        照旧可重发）。以后由维护重新判定。
        """
        counts: dict[str, int] = {}
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM turn_journal WHERE owner_instance_id IS NULL "
                "AND status IN ('queued','running')"
            ).fetchone()
            counts["turn_journal"] = int(row["c"] if row is not None else 0)
        except sqlite3.Error:  # pragma: no cover - 表不存在时降级
            counts["turn_journal"] = 0
        if self._derived_owner_column() is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM derived_tasks "
                "WHERE owner_instance_id IS NULL AND state = 'running'"
            ).fetchone()
            counts["derived_tasks"] = int(row["c"] if row is not None else 0)
        else:
            counts["derived_tasks"] = 0
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) c FROM pending_approvals "
                "WHERE owner_instance_id IS NULL AND status = 'pending'"
            ).fetchone()
            counts["pending_approvals"] = int(row["c"] if row is not None else 0)
        except sqlite3.Error:  # pragma: no cover
            counts["pending_approvals"] = 0
        return counts

    # -- R9 授权 / 审批 v2 接线 --------------------------------------------

    def _internal_data_paths(self) -> list[Path]:
        """QIO 内部运行数据路径清单（契约 §4）：DB、内部配置、日志、凭据等。

        工作区 / 归档 / 临时目录不在此列（它们是用户工作与可清理数据）。
        """
        paths: list[Path] = []
        try:
            paths.append(Path(self.settings.db_path))
            paths.append(Path(self.settings.config_path))
        except Exception:  # noqa: BLE001 - 路径字段缺失不阻塞
            pass
        try:
            for row in self.conn.execute("PRAGMA database_list").fetchall():
                file = str(row["file"] or "")
                if file:
                    paths.append(Path(file))
        except Exception:  # noqa: BLE001
            pass
        try:
            data_dir = Path(self.settings.data_dir)
            for name in ("logs", "credentials", "instances", "cache", "tmp"):
                candidate = data_dir / name
                if candidate.exists():
                    paths.append(candidate)
            for child in data_dir.iterdir():
                if child.name in {"workspace", "archive"} or child.name.startswith("workspace"):
                    continue
                if child.is_file():
                    paths.append(child)
        except OSError:  # pragma: no cover - 数据目录不可读时按已收集路径
            pass
        return paths

    def _internal_identity_keys(self) -> frozenset[str]:
        """引擎读取的身份匹配键（R9-H4 §8：identity+birth，含仍可能存在的被替换旧对象）。"""
        store = getattr(self, "internal_data_identity", None)
        if store is None:
            return frozenset()
        try:
            tokens = getattr(store, "protected_tokens", None)
            if callable(tokens):
                return frozenset(tokens())
            return frozenset(store.active_keys())
        except Exception as exc:  # noqa: BLE001 - 读失败不能静默降级
            self._record_identity_update_error("identity_keys", exc)
            return frozenset()

    def _backfill_identity_births(self) -> None:
        store = getattr(self, "internal_data_identity", None)
        if store is None:
            return
        snapshot = getattr(store, "snapshot", None)
        refresh = getattr(store, "refresh_path", None)
        if not callable(snapshot) or not callable(refresh):
            return
        try:
            rows = list(snapshot() or ())
        except Exception as exc:  # noqa: BLE001 - 回填失败不阻塞启动，但记录事实
            self._record_identity_update_error("birth_backfill_snapshot", exc)
            return
        for row in rows[:100]:
            if not isinstance(row, dict) or not row.get("active"):
                continue
            if row.get("birth_key"):
                continue
            path = str(row.get("path") or "")
            if not path:
                continue
            try:
                if not Path(path).exists():
                    continue
                refresh(path, source="startup_birth_backfill")
            except Exception as exc:  # noqa: BLE001 - 单个失败不降级其它登记
                self._record_identity_update_error("birth_backfill", exc)

    def _internal_identity_lookup(self, identity: object, birth_key: object = None) -> dict:
        """身份+birth 三态查询（E 接口；失败按未知，不声称同一文件）。"""
        store = getattr(self, "internal_data_identity", None)
        if store is None:
            return {"state": "unknown", "reason": "身份登记不可用"}
        lookup = getattr(store, "match_identity", None)
        if not callable(lookup):
            return {"state": "unknown", "reason": "身份登记不支持生命周期查询"}
        try:
            return dict(lookup(identity, birth_key) or {})
        except Exception as exc:  # noqa: BLE001 - 查询失败按未知（路径/历史仍兜底）
            self._record_identity_update_error("identity_lookup", exc)
            return {"state": "unknown", "reason": type(exc).__name__ + ": " + str(exc)}

    def _internal_identity_tokens(self) -> frozenset[str]:
        """identity+birth token 集合（含 retired_replaced 中仍可能存在的旧对象）。"""
        return self._internal_identity_keys()

    def _internal_identity_known_paths(self) -> frozenset[str]:
        """历史登记路径（身份读取失败时不静默降级为普通文件）。"""
        store = getattr(self, "internal_data_identity", None)
        if store is None:
            return frozenset()
        try:
            return frozenset(store.known_paths())
        except Exception as exc:  # noqa: BLE001 - 读失败不能静默降级
            self._record_identity_update_error("identity_known_paths", exc)
            return frozenset()

    # -- R9-H3 §4：可信写入提交后的身份自动更新 ----------------------------

    def _install_trusted_write_identity_hook(self) -> None:
        """把身份更新挂到真实可信写入入口（审批通过后的执行器）里。

        不是模型/普通工具可调用的接口：这里包装的是 AppContext 自己的 operations
        执行器；只有在真实执行**成功提交**后才按实际目标路径更新登记。普通工具直接
        读取/声称自己的写入不经过这里，不能自报身份。
        """
        original = self.operations.execute

        async def execute_with_identity_update(kind, payload, ctx):  # noqa: ANN001
            result = await original(kind, payload, ctx)
            try:
                self._note_identity_after_trusted_commit(kind, payload, result)
            except Exception as exc:  # noqa: BLE001 - 身份更新失败不能伪装成功
                self._record_identity_update_error(
                    "trusted_commit", exc, extra={"kind": str(kind)}
                )
            return result

        self.operations.execute = execute_with_identity_update  # type: ignore[method-assign]

    @staticmethod
    def _trusted_commit_paths(kind: object, payload: dict) -> list[str]:
        """从真实执行器 payload 取**实际变更对象**路径（有界，不扫描）。"""
        kind_text = str(kind or "")
        data = payload if isinstance(payload, dict) else {}
        paths: list[str] = []
        if kind_text in ("fs.write", "fs.patch"):
            raw = str(data.get("path") or "").strip()
            if raw:
                paths.append(raw)
        elif kind_text == "fs.delete":
            raw_rows = data.get("paths")
            if isinstance(raw_rows, list):
                for item in raw_rows:
                    if isinstance(item, dict):
                        candidate = str(item.get("path") or "").strip()
                    else:
                        candidate = str(item or "").strip()
                    if candidate:
                        paths.append(candidate)
            single = str(data.get("path") or "").strip()
            if single:
                paths.append(single)
            for item in data.get("items") or []:
                if isinstance(item, dict):
                    candidate = str(item.get("path") or "").strip()
                    if candidate:
                        paths.append(candidate)
        # 去重 + 有界（一次审批最多 50 个明确对象）。
        seen: set[str] = set()
        bounded: list[str] = []
        for raw in paths:
            if raw in seen:
                continue
            seen.add(raw)
            bounded.append(raw)
            if len(bounded) >= 50:
                break
        return bounded

    def _note_identity_after_trusted_commit(
        self, kind: object, payload: dict, result: object
    ) -> None:
        """真实执行成功提交后立即登记新对象身份（旧对象 retired / 新对象 active）。"""
        if not getattr(result, "ok", False):
            return
        store = getattr(self, "internal_data_identity", None)
        if store is None:
            return
        committed = self._trusted_commit_paths(kind, payload)
        if not committed:
            return
        # R9-H4 §8：可信删除与写入分开处理。删除只能退休已被证明不存在的对象；
        # 不能复用“读新身份”的写入逻辑把删除误报成登记失败。
        if str(kind or "") == "fs.delete":
            completed = self._completed_delete_paths(payload, result)
            if not completed:
                return
            try:
                outcomes = store.note_committed_delete(
                    completed, source="approved:fs.delete"
                )
            except Exception as exc:  # noqa: BLE001 - 身份更新失败不能伪装成功
                self._record_identity_update_error("trusted_delete", exc)
                return
            failures = [item for item in outcomes if not item.get("ok")]
            # 目录删除：只按已登记路径做前缀有界退休，不做全盘扫描。
            try:
                known = set(store.known_paths())
            except Exception:  # noqa: BLE001
                known = set()
            descendants: list[str] = []
            for root_path in completed:
                prefix = str(root_path).rstrip("\\/") + os.sep
                for known_path in known:
                    if str(known_path).startswith(prefix) and str(known_path) != root_path:
                        descendants.append(str(known_path))
                        if len(descendants) >= 50:
                            break
                if len(descendants) >= 50:
                    break
            if descendants:
                try:
                    child_outcomes = store.note_committed_delete(
                        descendants, source="approved:fs.delete_tree"
                    )
                    failures.extend(
                        item for item in child_outcomes if not item.get("ok")
                    )
                except Exception as exc:  # noqa: BLE001
                    self._record_identity_update_error("trusted_delete_tree", exc)
            if failures:
                messages = [
                    str(item.get("reason") or item.get("code") or "identity_update_failed")
                    for item in failures
                ]
                detail = getattr(result, "detail", None)
                if isinstance(detail, dict):
                    detail["internal_identity_error"] = messages
                self._record_identity_update_error(
                    "trusted_delete",
                    RuntimeError("; ".join(messages)),
                    extra={"kind": str(kind), "paths": len(failures)},
                )
            return
        try:
            protected = self._internal_data_paths()
        except Exception:  # noqa: BLE001 - 读路径失败按空处理；store 历史路径仍可命中
            protected = []
        known: set[str] = set()
        try:
            known = set(store.known_paths())
        except Exception:  # noqa: BLE001
            known = set()
        updates: list[dict] = []
        failures: list[dict] = []
        for raw in committed:
            target = internal_data.normalize_path(raw)
            if target is None:
                continue
            in_scope = internal_data.is_internal_data_path(target, protected)
            if not in_scope and str(target) not in known:
                # 普通用户文件：不登记、不扫描（与内部数据无关）。
                continue
            outcome = store.note_committed_path(target, source="approved:" + str(kind))
            updates.append(outcome)
            if not outcome.get("ok"):
                failures.append(outcome)
        if not updates:
            return
        detail = getattr(result, "detail", None)
        if failures:
            messages = [
                str((item or {}).get("reason") or (item or {}).get("code") or "identity_update_failed")
                for item in failures
            ]
            if isinstance(detail, dict):
                detail["internal_identity_error"] = messages
            self._record_identity_update_error(
                "trusted_commit",
                RuntimeError("; ".join(messages)),
                extra={"kind": str(kind), "paths": len(failures)},
            )

    def _completed_delete_paths(self, payload: dict, result: object) -> list[str]:
        """只取真实执行结果里已完成的删除项；部分失败不得误退休未完成对象。"""
        detail = getattr(result, "detail", None)
        completed: list[str] = []
        if isinstance(detail, dict):
            for item in detail.get("results") or ():
                if isinstance(item, dict) and item.get("ok"):
                    path = str(item.get("path") or "").strip()
                    if path:
                        completed.append(path)
            if completed:
                return completed[:50]
        if getattr(result, "ok", False):
            return self._trusted_commit_paths("fs.delete", payload)
        return []

    def _record_identity_update_error(
        self, phase: str, exc: BaseException, *, extra: dict | None = None
    ) -> None:
        """身份更新失败：保留事实、明确记录；不静默降级、不把失败报成成功。"""
        message = type(exc).__name__ + ": " + str(exc)
        self.internal_data_identity_error = message
        logging.getLogger(__name__).warning(
            "内部数据身份自动更新失败（保留已有登记；路径判定仍兜底）"
            " phase=%s extra=%s: %s",
            phase,
            extra or {},
            message,
        )

    def note_trusted_internal_commit(
        self, paths: object, *, source: str = "trusted_write"
    ) -> dict:
        """其它可信写入入口（如设置保存）提交成功后调用：只刷新明确变更对象。"""
        store = getattr(self, "internal_data_identity", None)
        if store is None:
            return {
                "ok": False,
                "reason": self.internal_data_identity_error or "身份登记不可用",
                "updates": [],
            }
        try:
            raw_paths = paths if isinstance(paths, (list, tuple, set, frozenset)) else [paths]
            # 只允许真实注册范围/历史登记中的路径：普通工具/普通用户文件不能借
            # 这个入口自报为内部数据对象（不扩大保护范围、不登记无关文件）。
            try:
                protected = self._internal_data_paths()
            except Exception:  # noqa: BLE001 - 路径读取失败按空；历史登记仍可命中
                protected = []
            try:
                known = set(store.known_paths())
            except Exception:  # noqa: BLE001
                known = set()
            scoped: list[str] = []
            for raw in raw_paths:
                target = internal_data.normalize_path(raw)
                if target is None:
                    continue
                if internal_data.is_internal_data_path(target, protected) or (
                    str(target) in known
                ):
                    scoped.append(str(target))
            if not scoped:
                return {"ok": True, "updates": [], "skipped": len(list(raw_paths))}
            updates = store.note_committed_paths(scoped, source=source, limit=50)
            failures = [item for item in updates if not item.get("ok")]
            if failures:
                self._record_identity_update_error(
                    "trusted_commit",
                    RuntimeError(
                        "; ".join(
                            str((item or {}).get("reason") or (item or {}).get("code") or "")
                            for item in failures
                        )
                    ),
                    extra={"source": source, "paths": len(failures)},
                )
            return {"ok": not failures, "updates": updates}
        except Exception as exc:  # noqa: BLE001 - 记录明确错误，不影响已成功的业务提交
            self._record_identity_update_error("trusted_commit", exc, extra={"source": source})
            return {"ok": False, "reason": type(exc).__name__ + ": " + str(exc), "updates": []}

    def refresh_internal_data_identities(self, path: object | None = None) -> dict:
        """刷新内部数据身份登记（启动后新增/替换对象时调用；失败如实返回）。"""
        store = getattr(self, "internal_data_identity", None)
        if store is None:
            return {
                "ok": False,
                "reason": self.internal_data_identity_error or "身份登记不可用",
            }
        try:
            if path is not None:
                key = store.refresh_path(path, source="manual_refresh")
                return {"ok": True, "path": str(path), "identity_key": key}
            counts = store.sync(self._internal_data_paths(), source="manual_refresh")
            return {"ok": True, "counts": dict(counts or {})}
        except Exception as exc:  # noqa: BLE001 - 刷新失败保留已有登记
            logging.getLogger(__name__).warning(
                "内部数据身份刷新失败（保留已有登记）", exc_info=True
            )
            return {"ok": False, "reason": type(exc).__name__ + ": " + str(exc)}

    def _file_origin_for(self, path: Path) -> str | None:
        """后端持久化文件来源查询（供授权引擎核实 qio_generated / 用户材料）。

        模型/工具传入的 origin 字段不可信；只有这里查到的来源才参与判定。
        存储由 AttachmentService 构造时建立并设为进程级默认（W3，迁移 55）。
        """
        try:
            from agent.services.file_origins import origin_for_path

            return origin_for_path(path)
        except Exception:  # noqa: BLE001 - 模块未就绪/查不到按未记录（保守：需要确认）
            return None

    def _make_tool_sandbox(self):
        """工具子进程执行器：注入模式/网关/受保护范围（契约 §3.1，R9 W2b 接线）。"""
        from agent.tools.sandbox import SandboxExecutor

        return SandboxExecutor(
            mode_provider=lambda: self.authorization.mode().value,
            security=self.authorization,
            protected_scope_provider=lambda: [
                self.computer.root(),
                *self._internal_data_paths(),
            ],
        )

    async def _execute_approved_tool(self, payload: dict, ctx) -> object:
        """tool.invoke 执行器：用户批准后重新走工具管线真正执行（不重复弹审批）。"""
        from agent.adapters.base import ToolCall
        from agent.security.operations import ExecResult

        name = str(payload.get("tool") or "")
        arguments = dict(payload.get("arguments") or {})
        call_id = str(payload.get("call_id") or ("approved_" + str(ctx.approval_id)))
        if self.registry.get(name) is None:
            return ExecResult(
                ok=False,
                state="failed",
                summary="工具已不存在，本次未执行",
                error="tool_not_found: " + name,
            )
        call = ToolCall(id=call_id, name=name, arguments=arguments)
        result = await self.registry.execute(call, approval_id=ctx.approval_id)
        ok = bool(getattr(result, "ok", False))
        return ExecResult(
            ok=ok,
            state="completed" if ok else "failed",
            summary=(getattr(result, "content", "") or getattr(result, "error", "") or "")[:300],
            detail={
                "tool": name,
                "ok": ok,
                "content": (getattr(result, "content", "") or "")[:2000],
                "error": getattr(result, "error", None),
            },
        )

    def _on_approval_executed(self, data: dict) -> None:
        """审批执行结果通知：原 turn 若仍在，注入一条系统通知（不伪装成模型的话）。"""
        active = self.turns.active
        loop = getattr(active, "loop", None) if active is not None else None
        push = getattr(loop, "push_notice", None)
        if not callable(push):
            return
        label = str(data.get("label") or data.get("kind") or "操作")
        summary = str(data.get("summary") or "").strip()
        state_text = "已执行" if data.get("ok") else "未完成"
        push("审批「" + label + "」" + state_text + ("：" + summary if summary else ""))

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

    # -- 对话页任务栏：子任务展示记录 -------------------------------------

    def _on_subagent_task_change(self, record) -> None:
        """把子任务的真实生命周期写进展示记录（失败不影响任务本身）。"""
        from agent.services import task_feed

        try:
            task_feed.record_subagent(
                self.conn, record, instance_id=self.instance_id
            )
        except Exception:  # noqa: BLE001 - 展示记录写失败不能影响执行
            logger.warning(
                "task feed record failed: %s",
                getattr(record, "task_id", ""),
                exc_info=True,
            )

    def _restore_tools(self) -> None:
        """Restore agent-created tools from the persistent store at startup."""
        from agent.tools.runtime_tools import CodeTool
        from agent.tools.sandbox import SandboxExecutor
        from agent.tools.subagent_tool import SubagentTool

        # 恢复出来的自建工具同样受执行层约束（与 self.tool_sandbox 同一套注入）。
        sandbox = self._make_tool_sandbox()
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
                        usage_assignment=self.subagent_usage_assignment,
                    )
                else:
                    tool = CodeTool(
                        definition,
                        sandbox,
                        credentials=self.credentials,
                        envs=self.tool_envs,
                    )
                self.registry.register(tool)
                logger.info("restored tool: %s", definition.name)
            except Exception:  # noqa: BLE001 - a broken tool must not block startup
                logger.warning("failed to restore tool: %s", definition.name, exc_info=True)

    # -- 用途解析（设置页改版 §8）-----------------------------------------
    #
    # 一次 turn / 一个后台任务开始时**冻结** UsageTarget（见 turn_orchestrator.begin /
    # _schedule_derived_work）：用户中途改了用途分配，已经开始的调用仍按原配置归属，
    # 后续调用才用新保存值。解析本身是纯读（services/usage_target.py），不建连接。

    def resolve_usage(
        self, usage_key: str, *, policy_tags: list[str] | None = None
    ) -> UsageTarget:
        """按用途解析出 (凭据, 模型)。不可用时抛 UsageUnavailable（带原因码）。"""
        return _resolve_usage_target(
            self.credentials,
            self.settings_store,
            usage_key,
            policy=self.policy,
            policy_tags=policy_tags,
        )

    def resolve_main_target(self) -> UsageTarget:
        """主对话 / 通用对话：共用同一条调用路径，通用对话显式指定优先。"""
        return _resolve_shared_usage_target(
            self.credentials,
            self.settings_store,
            ["general-chat", "main-chat"],
            policy=self.policy,
            primary_key="main-chat",
        )

    def resolve_alongside_target(self, usage_key: str) -> UsageTarget | None:
        """「专项用途」：只有**显式指定**或基线专项标签才有目标，否则 None。

        看图就是这种：基线行为是「有 vision 凭据就用它，没有就如实回落主对话」。
        这里不继承默认模型（``vision`` 的 inherits_default=False）：把默认对话模型
        塞给看图，等于让一个可能读不了图的模型去读图。
        """
        usage_key = str(usage_key or "").strip()
        if not usage_key:
            return None
        assignment = _read_usage_assignments(self.credentials).get(usage_key)
        if _is_explicit_usage_assignment(assignment):
            # 显式指定损坏 → 硬失败（不静默换成主对话那把 Key）。
            return self.resolve_usage(usage_key)
        tags = list(_legacy_tags_for_usage(usage_key))
        refs = self.policy.resolve(usage_key, tags) if tags else []
        if not refs:
            return None
        ref = refs[0]
        return UsageTarget(
            usage_key=usage_key,
            credential_id=ref.key_id,
            model_id=ref.default_model,
            source=SOURCE_POLICY,
        )

    def _record_usage(self, target: UsageTarget) -> None:
        """记下解析结论（不记密钥）；降级要留痕，否则「静默回退」就真的静默了。

        解析成功时清掉这个用途上一次的「不可用」记录：用户修好凭据之后，界面/事件
        不能再拿旧原因说事（否则「主对话」明明恢复了，失败提示却还挂着旧理由）。
        """
        self.usage_targets[target.usage_key] = target
        self.usage_unavailable.pop(target.usage_key, None)
        if target.degraded:
            logger.warning(
                "用途 %s 的默认模型不可用（%s），已回退到 %s/%s",
                target.usage_key,
                target.degrade_reason,
                target.credential_id,
                target.model_id or "(凭据默认模型)",
            )

    def _record_usage_unavailable(self, exc: UsageUnavailable) -> None:
        """记下「这个用途现在不可用 + 原因」；文案里没有密钥，也没有请求内容。"""
        self.usage_unavailable[exc.usage_key] = exc
        logger.warning(
            "用途 %s 不可用：%s（%s）", exc.usage_key, exc.reason_code, exc.message
        )

    # -- adapter ----------------------------------------------------------

    def resolve_main_ref(self) -> CredentialRef | None:
        refs = self.policy.resolve("main-loop", MAIN_LOOP_USAGE_TAGS)
        return refs[0] if refs else None

    def resolve_vision_ref(self) -> CredentialRef | None:
        """专门打 `vision` 用途标签的凭据（看图用）。

        为什么要单独一条：主循环优先 `main-loop` 标签，用户很可能另配一把只能看图的
        Key；这时把图片发给主循环那把，等于让一个看不见的模型去读图。有 vision 凭据
        就优先它，没有就如实回落主循环（回落由调用方决定并记录）。
        与 `resolve_main_ref` 同一套 policy：停用 / 撤销 / 预算 / 验证状态照样拦。
        """
        refs = self.policy.resolve("vision", ["vision"])
        return refs[0] if refs else None

    async def build_vision_adapter(self) -> BaseAdapter | None:
        """看图用的适配器（用途 `vision`）。

        显式指定了看图模型却不可用 → **不换 Key**：记下原因并返回 None（调用方按
        「没有专用看图凭据」如实回落，图片能否交付由适配器能力决定）。
        没有显式指定 → 基线的 vision 标签凭据 → 没有就 None（由调用方回落主对话）。
        """
        try:
            target = self.resolve_alongside_target("vision")
        except UsageUnavailable as exc:
            self._record_usage_unavailable(exc)
            return None
        if target is None:
            # 没有显式指定、也没有 vision 专用凭据：基线行为是「回调方回落主对话」。
            # 顺手清掉上一次「显式指定不可用」的记录，免得用户清掉分配后还在报旧原因。
            self.usage_unavailable.pop("vision", None)
            return None
        self._record_usage(target)
        return await self.build_adapter_for_target(target)

    async def build_adapter_for_target(self, target: UsageTarget) -> BaseAdapter | None:
        """按冻结好的用途目标建（或复用）adapter —— 归因/预算仍走唯一那条路径。"""
        return await self.build_adapter_for_credential(
            target.credential_id, target.model_id
        )

    async def build_adapter_for_usage(
        self, usage_key: str, *, policy_tags: list[str] | None = None
    ) -> BaseAdapter | None:
        """后台路径用：按用途解析并建 adapter；不可用时记录原因并返回 None。

        这台机器的调用方（维护、后台分析）拿不到 adapter 就如实跳过并说明原因，
        绝不悄悄改用主对话那把 Key。
        """
        try:
            target = self.resolve_usage(usage_key, policy_tags=policy_tags)
        except UsageUnavailable as exc:
            self._record_usage_unavailable(exc)
            return None
        self._record_usage(target)
        return await self.build_adapter_for_target(target)

    def subagent_usage_assignment(self) -> tuple[str, str | None] | None:
        """「子任务」用途的显式 / 默认指定（给 tools/subagent_tool.py 用）。

        * 没有显式分配、也没有默认模型 → None：照旧走基线自主选择（``credential_selection``）；
        * 有显式分配 → 返回 (credential_ref, model)；**不可用时抛 UsageUnavailable**，
          由工具如实失败 —— 用户明确指定的凭据不能被静默替换；
        * 只有默认模型 → 返回默认模型目标；默认层不可用时回退基线选择，但降级事实已由
          `_record_usage` 记下（界面可显示「默认模型不可用，已回退到 X」）。
        """
        assignment = _read_usage_assignments(self.credentials).get("subagent")
        if _is_explicit_usage_assignment(assignment):
            target = self.resolve_usage("subagent")
            self._record_usage(target)
            return target.credential_id, target.model_id
        try:
            target = self.resolve_usage("subagent")
        except UsageUnavailable as exc:
            # 没有显式分配时，回退是基线自己的事；这里只记原因。
            self._record_usage_unavailable(exc)
            return None
        self._record_usage(target)
        if target.source in (SOURCE_EXPLICIT, SOURCE_DEFAULT):
            return target.credential_id, target.model_id
        return None

    async def build_background_adapter(self, usage_key: str) -> BaseAdapter | None:
        """后台维护路径（知识维护批次 / 维护问答）用的 adapter。

        * 用途能解析出目标 → 用它（用户改了用途分配就真的换）；
        * **数据库里一条凭据都没有**（首次启动 / 单测注入主 adapter 的离线环境）→
          保持基线：用主 adapter。这时库里没有任何别的服务可选，不存在「把内容发给
          用户没选过的服务」的风险；一旦真有凭据而用途目标不可用，就返回 None，
          由调用方如实失败（不悄悄换用主对话模型）。
        """
        adapter = await self.build_adapter_for_usage(usage_key)
        if adapter is not None:
            return adapter
        exc = self.usage_unavailable.get(usage_key)
        if exc is not None and exc.reason_code != REASON_NO_CREDENTIAL:
            return None
        return await self.build_adapter()

    async def build_derived_adapters(self) -> dict[str, Any]:
        """后台三类用途各自解析 + 建 adapter，返回 ``kind -> adapter | UsageUnavailable``。

        为什么不是「一个 adapter 传下去」：摘要（记忆整理）、知识提炼、实体提炼是三条
        独立派生任务，用户可以分别指定不同模型；共用主 Turn 的 adapter 会让「用途分配」
        在界面上看起来生效、实际毫无作用。

        解析/建 adapter 失败时返回**异常对象**而不是 None：派发器按 kind 把它写成该任务
        的可读失败原因（这条任务停了，别的任务照跑），不退回主对话模型。
        """
        result: dict[str, Any] = {}
        for kind, usage_key in DERIVED_USAGE_BY_KIND.items():
            try:
                target = self.resolve_usage(usage_key)
            except UsageUnavailable as exc:
                self._record_usage_unavailable(exc)
                result[kind] = exc
                continue
            self._record_usage(target)
            adapter = await self.build_adapter_for_target(target)
            if adapter is None:
                exc = UsageUnavailable(
                    usage_key,
                    REASON_ADAPTER_UNAVAILABLE,
                    f"「{usage_key}」用途解析到 {target.credential_id}，但它建不出可用适配器"
                    "（端点或模型缺失）",
                    credential_id=target.credential_id,
                    model_id=target.model_id,
                )
                self._record_usage_unavailable(exc)
                result[kind] = exc
                continue
            result[kind] = adapter
        return result

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
        缓存按 `(key_id, model)` 分槽位：同一凭据的不同模型可以同时使用，
        建后一个不会关闭/顶替前一个（A）；被顶替的旧 adapter 运行期不关闭，
        应用关闭时由 `aclose()` 统一释放。
        """
        secret = self.credentials.get_secret(key_id)
        if secret is None:
            return None
        meta = self.credentials.get_metadata(key_id)
        base_url = resolve_credential_endpoint(meta)
        model = model or resolve_credential_model(meta)
        if base_url is None or model is None:
            # 地址或模型缺失时**不猜**：以前会兜底成另一家的默认地址/模型
            # （openai.com + gpt-4o-mini），那等于把内容发给用户没选过的服务。
            logger.warning(
                "credential %s has no usable endpoint/model; refusing to build an adapter", key_id
            )
            return None
        cache_key = (
            key_id,
            (meta or {}).get("version"),
            (meta or {}).get("updated_at"),
            base_url,
            model,
        )
        cached = self._adapter_cache.get(cache_key)
        if cached is not None:
            # 命中即刷新最近使用顺序（dict 保序，容量淘汰从最旧的开始）。
            self._adapter_cache[cache_key] = self._adapter_cache.pop(cache_key)
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
        adapter: BaseAdapter | None
        if credential_uses_anthropic(meta, base_url):
            await self._ensure_anthropic_capability(secret, model, base_url, key_id, meta)
            adapter = AnthropicAdapter(api_key=secret, model=model, endpoint=base_url)
        else:
            client = AsyncOpenAI(api_key=secret, base_url=base_url)
            probe = await probe_adapter(
                client, model, endpoint=base_url, cache=self.probe_cache,
            )
            if probe.mode == AdapterMode.NATIVE:
                adapter = NativeAdapter(client, model, endpoint=base_url)
            elif probe.mode == AdapterMode.TEXT:
                adapter = TextAdapter(client, model, endpoint=base_url)
            else:
                return None
        # 用量归因：这条 adapter 发出的每次调用都算在这把凭据上。
        # 主循环、子 agent、后台维护共用这里建出来的 adapter，所以只在这一处打标。
        adapter.key_id = key_id
        # A06：显式绑定**本上下文**的账本。这里是新建 adapter 的唯一路径（缓存复用走不到
        # 这里），所以缓存复用的 adapter 天然保持原绑定；绑定函数本身也拒绝改写已有的显式
        # 归属，于是「哪个上下文最后登记了进程级默认库」不再影响记账与预算归属。
        from agent.credentials.usage import bind_request_accounting

        bind_request_accounting(adapter, self.credentials)
        return adapter

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
        result = await probe_anthropic(
            secret, model, base_url, key_id=key_id, accounting_store=self.credentials
        )
        self._anthropic_probe_at[key] = time.time()
        return result

    def _remember_adapter(self, cache_key: tuple, adapter: BaseAdapter) -> None:
        """记住 adapter；只有**同一个槽位**换配置才顶替旧的，绝不关闭别的模型。

        历史缺陷（A）：这里以前会把同一 key_id 的其它缓存项全部 pop 并
        `_close_adapter_soon`。于是同一把 Key 下换成另一个模型（记忆整理 / 知识提炼 /
        实体提炼各自指定不同模型，主对话与子任务用不同模型）时，前一个还在跑的任务
        的连接会被关掉。

        现在的规则：

        * 槽位 = `(key_id, model)`：不同模型各自独立，互不顶替、互不关闭；
        * 同一槽位换了配置（凭据版本 / updated_at / 端点）→ 新配置成为该槽位当前项，
          旧 adapter 进暂留名单（`_adapter_retired`），**运行期不关闭**：
          换配置那一刻已经开始的工作仍能用自己的连接收尾；
        * 两条名单都有条数上限（`_prune_adapters` 只松开引用、不关闭）：没人再引用的
          旧连接会立刻被引用计数回收，仍被持有的（正在用）保持可用 —— 这正是「不无限
          保留历史连接」与「不打断在用连接」可以同时成立的原因；
        * 唯一的关闭点是 `aclose()`（应用关闭）。
        """
        # 并发建同一个键（例如主对话与后台派生工作几乎同时开跑）：后到的成为当前项，
        # 先到的进暂留名单 —— 同样不关闭，谁手里还拿着它谁就能继续用完。
        replaced = self._adapter_cache.get(cache_key)
        if replaced is not None and replaced is not adapter:
            self._adapter_cache.pop(cache_key, None)
            self._retire_adapter(cache_key, replaced)
        # 缓存键的排布见 build_adapter_for_credential：末位是模型，首位是凭据 id。
        slot = (cache_key[0], cache_key[-1])
        previous_key = self._adapter_slots.get(slot)
        if previous_key is not None and previous_key != cache_key:
            previous = self._adapter_cache.pop(previous_key, None)
            if previous is not None:
                self._retire_adapter(previous_key, previous)
        # 插入到末尾 = 最近使用（dict 保序，容量淘汰按它做 LRU）。
        self._adapter_cache.pop(cache_key, None)
        self._adapter_cache[cache_key] = adapter
        self._adapter_slots[slot] = cache_key
        self._prune_adapters()

    def _retire_adapter(self, cache_key: tuple, adapter: BaseAdapter) -> None:
        """把被顶替 / 淘汰的旧 adapter 放进**有界**暂留名单（不关闭）。

        留一个强引用只为「应用退出时还能对最近的旧连接做一次显式 close」；
        超出上限的最旧条目只是松开引用 —— 引用计数会立刻回收没人再持有的底层
        client，仍被后台工作持有的条目由持有者决定生命周期。运行期这里不关闭
        任何连接（那会打断已经开始的调用）。
        """
        self._adapter_retired.append((cache_key, adapter))
        while len(self._adapter_retired) > ADAPTER_RETIRED_LIMIT:
            # 不调用 close()：可能还有已经开始的工作在用。松开引用即回收无人引用的。
            self._adapter_retired.pop(0)

    def _prune_adapters(self) -> None:
        """把当前缓存压到条数上限：淘汰最久没用的槽位（同样只松开引用、不关闭）。

        被淘汰的 adapter 进暂留名单，所以「容量淘汰」不会变成「关闭一个正在被用的
        连接」—— 运行期没有任何路径关闭缓存里的 adapter。
        """
        while len(self._adapter_cache) > ADAPTER_CACHE_LIMIT:
            stale_key = next(iter(self._adapter_cache))
            stale = self._adapter_cache.pop(stale_key)
            slot = (stale_key[0], stale_key[-1])
            if self._adapter_slots.get(slot) == stale_key:
                self._adapter_slots.pop(slot, None)
            self._retire_adapter(stale_key, stale)

    async def aclose(self) -> "CloseReport":
        """应用关闭：按依赖顺序收尾，不留悬挂的任务与等待。

        顺序（后者都依赖前者已经停下来）：

        0. 停后台记忆任务（先拒绝新建派生工作 → 有界等待 → 取消 → 确认结束）；
        1. 停后台维护调度；
        2. 停 TurnManager（在跑的那一轮收尾、排队 turn 兑现终态、等待者全部结束）；
        3. 停 TaskManager（取消在跑的独立任务、兑现所有 waiter）；
        4. 释放 adapter / HTTP client。

        返回 `CloseReport`：调用方（API lifespan / 更新前停后端）**必须**按它决定
        是否写干净退出、是否释放依赖、是否关数据库。不干净时三项全否——见
        `services/lifecycle.py::close_decision`。

        数据库连接的关闭由调用方决定（`create_app(..., close_db_on_shutdown=True)`
        时在 lifespan 的最后一步），保证不会出现「后台任务还在写，DB 已经关了」。
        """
        # FU2-D：先取消待执行的「通知投递延迟重试」句柄（库里的待投递记录保留，
        # 下次启动由 _prime_subtask_notification_delivery 继续投递）。
        try:
            self.cancel_notification_retry()
        except Exception:  # noqa: BLE001 - 取消失败不阻止其它收尾
            logging.getLogger(__name__).warning(
                "cancel notification retry failed", exc_info=True
            )
        # R9：先停审批后台执行器（取消在途执行任务；waiting 审批不取消，供重启恢复）。
        try:
            await self.approvals.aclose()
        except Exception:  # noqa: BLE001 - 取消失败不能阻止其它收尾
            logging.getLogger(__name__).warning("approvals aclose failed", exc_info=True)
        # A05：派生工作在 turn 收尾时调度。必须先关它，否则 turns.shutdown() 之后
        # 仍可能有提炼协程在写库。`close_background` 会等/取消并如实回报
        # 「句柄取消了」与「底层执行单元确认结束」的区别。
        background_registry = getattr(self, "background", None)
        # 整合轮 §2：**关闭之前**取理解任务的快照。关闭报告的名单只列未确认结束的任务，
        # 干净收尾的名字会被撤下账 —— 只看报告会把「关停了一个正在跑的理解」说成
        # 「这次关闭跟理解无关」。快照里同时含「句柄已结束、底层执行单元未确认结束」的幽灵。
        understanding_registered = tuple(
            name
            for name in (background_registry.registered() if background_registry is not None else ())
            if str(name).startswith(UNDERSTANDING_TASK_PREFIX)
        )
        report = await close_background(background_registry)
        # F06：维护有自己的执行单元（周期循环 + 手动触发的那一轮 + 维护发起的审批等待）。
        # stop() 先拒绝新维护，再有界等待/取消并**确认**执行单元终止，返回如实报告；
        # 未确认结束时合并进下面同一份 CloseReport ⇒ clean=False / unfinished 有名字，
        # 于是 adapter 不释放、数据库不关闭（它仍然依赖这些资源）。
        from agent.services.lifecycle import (
            PHASE_MAINTENANCE_STOPPED,
            PHASE_MAINTENANCE_UNFINISHED,
        )
        maintenance_report = await self.maintenance.stop()
        if isinstance(maintenance_report, dict):
            reported_phases = tuple(
                str(phase) for phase in maintenance_report.get("phases") or ()
            )
            if reported_phases:
                report = report.with_phase(*reported_phases)
            elif maintenance_report.get("clean"):
                report = report.with_phase(PHASE_MAINTENANCE_STOPPED)
            else:
                report = report.with_phase(PHASE_MAINTENANCE_UNFINISHED)
            if not maintenance_report.get("clean"):
                pending = tuple(str(name) for name in maintenance_report.get("pending") or ())
                if not pending:
                    # 报告说没结束却没给名字：不能当成「没有未结束的东西」。
                    pending = ("maintenance",)
                report = merge_close_reports(
                    report,
                    CloseReport(
                        clean=False,
                        unfinished=pending,
                        detail=str(maintenance_report.get("reason") or "维护未确认结束"),
                    ),
                )
        # 整合轮 §2：阶段名与 maintenance_* 同构，判定只看真实名单 ——
        # registered 是关闭前账上的理解任务，unfinished 是关闭报告里仍未确认结束的。
        # 不为理解单独做任何等待或固定 sleep：等待 / 取消 / 确认都由
        # background.shutdown 的有界三档完成，时钟不会被某个理解任务无限拖住。
        understanding_unfinished = tuple(
            name for name in report.unfinished if str(name).startswith(UNDERSTANDING_TASK_PREFIX)
        )

        await self.turns.shutdown()
        await self.task_manager.shutdown()
        # 重活执行器最后收：在跑的 turn 已经收尾，不会再有人往池里丢任务。
        await self.heavy.shutdown()
        # 契约 4：共享有界执行器（工具路由/文件 I/O）随应用关闭收口。
        from agent.tools.blocking import shutdown_blocking_executor

        shutdown_blocking_executor(wait=True)

        decision = close_decision(report)
        if decision.record_clean_exit:
            # 干净退出：显式写下退出时刻（契约 C1 的权威判据）。
            # 顺序放在 turn / task 都停完之后：此刻确实不再有本实例的写入了。
            # 失败也不能阻止关闭 —— 下次启动会用心跳 + pid 兜底判定。
            try:
                self.instances.mark_clean_exit()
            except Exception:  # noqa: BLE001 - 关闭阶段不能再抛
                logger.warning("failed to mark clean exit", exc_info=True)
            report = report.with_phase(PHASE_CLEAN_EXIT_RECORDED)
        else:
            # 还有后台执行单元没确认结束：**不写干净退出**，下次启动按归属恢复。
            report = report.with_phase(PHASE_CLEAN_EXIT_SKIPPED)
            logger.error(
                "后台执行单元未确认结束：不写干净退出（下次启动按归属恢复）：%s",
                list(report.unfinished),
            )

        if decision.release_adapters:
            # 当前缓存 + 被顶替仍暂留的旧 adapter 一起关：应用退出时不存在「还可以
            # 继续用」的连接，而关闭前后台执行单元已经确认停下（见 close_decision）。
            # 运行期从不关闭它们（见 _remember_adapter），所以这里是唯一的关闭点。
            adapters = list(self._adapter_cache.values())
            adapters.extend(adapter for _key, adapter in self._adapter_retired)
            self._adapter_cache = {}
            self._adapter_slots.clear()
            self._adapter_retired.clear()
            self._anthropic_probe_at.clear()
            closed: set[int] = set()
            for adapter in adapters:
                if id(adapter) in closed:
                    continue
                closed.add(id(adapter))
                await _close_adapter(adapter)
            report = report.with_phase(PHASE_ADAPTERS_RELEASED)
        else:
            # 残留协程还会用到 adapter：先不释放，也不清缓存（否则它们会打到已关的 client）。
            report = report.with_phase(PHASE_ADAPTERS_KEPT)
        report = _record_understanding_phase(
            report,
            registered=understanding_registered,
            unfinished=understanding_unfinished,
        )
        # 关闭完成：连接不再承载任何后台调度，解绑免得把宿主应用一直留在模块表里。
        try:
            from agent.interactive.submission import unbind_background_owner

            unbind_background_owner(self.conn)
        except Exception:  # noqa: BLE001 - 关闭阶段不能再抛
            logger.warning("failed to unbind interactive background owner", exc_info=True)

        log_close_report(report)
        return report

    async def build_adapter(self) -> BaseAdapter | None:
        """主对话适配器（用途 main-chat / general-chat）。

        解析优先：显式指定 → 默认模型 → 基线 policy。不可用时**不换 Key**，而是记下
        原因并返回 None —— 调用方（Turn 编排器）据此给出可读的终态，而不是把它当成
        「没有配置凭据」。这个返回值同时是这一轮的**冻结**结果（下一次调用才会用新保存值）。
        """
        try:
            target = self.resolve_main_target()
        except UsageUnavailable as exc:
            self._record_usage_unavailable(exc)
            return None
        self._record_usage(target)
        return await self.build_adapter_for_target(target)

    def usage_unavailable_detail(self, usage_key: str) -> dict[str, Any] | None:
        """这个用途最近一次「不可用」的完整事实（原因码 + 可读原因 + 凭据/模型引用）。

        给路由 / 理解模块 / 界面用：它们拿不到 adapter 时也要能说清**为什么**，
        而不是笼统报「没有可用模型」。没有任何记录返回 None（不编造）。
        """
        exc = self.usage_unavailable.get(str(usage_key or "").strip())
        return exc.to_dict() if exc is not None else None

    def main_adapter_unavailable_message(self) -> str:
        """主对话没有可用适配器时的可读原因（有原因就说原因，不笼统说「没配置」）。"""
        exc = self.usage_unavailable.get("main-chat")
        if exc is None:
            return "还没有配置可用的模型凭据：请在「设置 → 凭据」里添加一个 API Key 后再对话"
        return f"主对话当前不可用：{exc.message}"

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
        """主循环没有可用凭据：说明「现在用不了 + 为什么 + 去哪里处理」。

        以前一律报 reason_code=no_credential（「还没有配置凭据」）。用户在设置里显式
        指定了用途、但那把 Key 被停用 / 预算耗尽时，这句话是错的：凭据已经配好了，
        是它现在不能用。所以这里优先报真实原因码与可读原因（不含密钥、不含 endpoint）。
        """
        from agent.api.events import EventType, make_event

        exc = self.usage_unavailable.get("main-chat")
        data: dict = {
            "status": "unavailable",
            "scope": "main_loop",
            "reason_code": exc.reason_code if exc is not None else "no_credential",
            "message": self.main_adapter_unavailable_message(),
        }
        if exc is not None:
            # 只给定位用的引用（凭据 id / 模型名），不含任何密钥材料。
            data["credential_id"] = exc.credential_id
            data["model_id"] = exc.model_id
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

        adapter = await self.build_adapter()
        if adapter is None:
            raise RuntimeError("no main-loop credential available for tool development")
        return ToolLifecycle(
            adapter=adapter,
            approvals=self.approvals,
            sandbox=self.tool_sandbox,
            registry=self.registry,
            credentials=self.credentials,
            envs=self.tool_envs,
            task_manager=self.task_manager,
            retriever=self.retriever,
            adapter_factory=self.build_adapter_for_credential,
            bus=self.bus,
            tool_store=self.tool_store,
            trace_store=self.trace_store,
            turn_id_provider=self._active_turn_id,
            usage_assignment=self.subagent_usage_assignment,
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

    # -- 附件事实（plan §4；AttachmentService 由 C 提供） ------------------

    ATTACHMENT_NOTE_LIMIT = 600

    def _attachment_service(self):
        """附件服务（C 的 AttachmentService）：注册名/属性名都容忍缺失。"""
        resolver = getattr(self.services, "resolve", None)
        if resolver is not None:
            for name in ("attachments", "attachment_service"):
                service = resolver(name)
                if service is not None:
                    return service
        for attr in ("attachments", "attachment_service"):
            service = getattr(self, attr, None)
            if service is not None:
                return service
        return None

    def attachment_turn_note(self, turn_id: str | None = None) -> str | None:
        """本轮附件事实（喂给模型的一段系统说明）；没有附件 / 没有服务 → None。

        只回答「附加了哪些文件对象」：名字、保存方式、可读性、是否仍可访问。
        **不把文件内容塞进上下文**（模型必须按需 read_attachment），也不影响审批权限
        与工具参数。附件是旁路：任何异常都按「没有附件」处理。
        """
        service = self._attachment_service()
        if service is None or not turn_id:
            return None
        getter = getattr(service, "turn_note", None)
        if getter is None:
            return None
        try:
            note = getter(turn_id)
        except Exception:  # noqa: BLE001 - 附件事实是旁路
            logger.warning("attachment turn note failed", exc_info=True)
            return None
        if not note or not str(note).strip():
            return None
        from agent.prompts import ATTACHMENT_NOTE_PREFIX
        from agent.trace.redact import redact_text

        body = redact_text(str(note).strip())[: self.ATTACHMENT_NOTE_LIMIT]
        return f"{ATTACHMENT_NOTE_PREFIX}\n{body}"

    # -- 多媒体材料（契约 §4）：本轮真正交付的图片/音频/视频依据 -------------

    def media_materials_for_turn(self, turn_id: str | None, adapter: Any) -> dict:
        """本轮媒体材料：诚实事实 + 真正能交付的内容块（**同步**，见调用点说明）。

        能力协商只看**本次真实使用的适配器**（含凭据回落之后的那一个）的声明：
        `supports_images` / `supports_audio` / `supports_video` / `audio_transcription`。
        没有能力就如实降级成 `*_unsupported`，绝不静默换成用户没配置的外部服务。

        线程纪律：候选清单（附件 → 路径）在**事件循环线程**上收集（附件服务与数据库
        连接都不是线程安全的，见 services/attachments.py）；调用方把 `candidates`
        交给工作线程跑 ffmpeg（那里只有纯文件 I/O 与子进程）。

        返回 `{"parts": [...], "entries": [...], "work_dir": Path|None, "error": str}`；
        任何一步失败都按「没有媒体材料」处理并把原因记在 `error` 里 —— 媒体是旁路，
        绝不因为它挡住这一轮。
        """
        from agent.interactive import materials as materials_module

        result: dict = {"parts": [], "entries": [], "work_dir": None, "error": ""}
        if not turn_id:
            return result
        service = self._attachment_service()
        if service is None:
            result["error"] = "没有可用的附件服务：本轮没有读取任何媒体材料"
            return result
        try:
            candidates = materials_module.collected_media_candidates(service, [turn_id])
        except Exception as exc:  # noqa: BLE001 - 候选收集失败 = 没有媒体材料
            logger.warning("media candidates failed", exc_info=True)
            result["error"] = f"媒体候选收集失败：{type(exc).__name__}"
            return result
        if not candidates:
            return result
        result["candidates"] = candidates
        return result

    def resolve_media_materials(self, candidates: Any, *, turn_id: str | None, adapter: Any) -> dict:
        """**工作线程**入口：跑 ffmpeg 派生处理并按适配器能力如实交付。

        只碰候选里已经解析好的路径与适配器上的能力位：不读数据库、不碰附件服务。
        """
        from agent.interactive import materials as materials_module

        result: dict = {"parts": [], "entries": [], "work_dir": None, "error": ""}
        rows = list(candidates or [])
        if not rows:
            return result
        supports_images = bool(getattr(adapter, "supports_images", False))
        supports_audio = bool(getattr(adapter, "supports_audio", False))
        supports_video = bool(getattr(adapter, "supports_video", False))
        transcription = getattr(adapter, "audio_transcription", None)
        # 转写调用器**按本次运行解析**：有通道且适配器真的实现了入口才拿得到，
        # 否则返回 None（调用方据此如实降级成「没有转写通道」，不静默换别家服务）。
        transcriber = materials_module.transcriber_for_adapter(adapter) if transcription else None
        work_dir = None
        if turn_id:
            try:
                work_dir = Path(self.settings.data_dir) / "tmp" / f"media-{turn_id}"
            except Exception:  # noqa: BLE001 - 取不到数据目录就让材料层用系统临时目录
                work_dir = None
        try:
            resolved = materials_module.resolve_media(
                candidates=rows,
                supports_images=supports_images,
                supports_audio=supports_audio,
                supports_video=supports_video,
                audio_transcription=transcription,
                transcriber=transcriber,
                work_dir=work_dir,
            )
        except Exception as exc:  # noqa: BLE001 - 媒体是旁路：失败就是没有材料
            logger.warning("media resolve failed", exc_info=True)
            result["error"] = f"媒体材料解析失败：{type(exc).__name__}"
            return result
        result["parts"] = list(resolved.get("parts") or [])
        result["entries"] = list(resolved.get("entries") or [])
        result["work_dir"] = resolved.get("work_dir")
        return result

    # -- 附件读取授权 / 互动材料事实（R9 §3.1 / §6.4 / §8）--------------------

    async def authorize_attachment_reads_for_turn(
        self,
        turn_id: str | None,
        *,
        gateway: Any = None,
        purpose: str = "本轮互动材料读取",
    ) -> dict:
        """本轮附件的 READ effect 统一授权（异步边界）；同步材料构造只查结果表。

        * 只对 material_enabled 的附件产生读取授权（关掉 = 不主动纳入，不是 OS 权限）；
        * 权限判定全部来自 W1 的网关（经 services/attachment_access 适配层）；
        * 返回 {"allowed","denied","degraded"}；调用方把 allowed 放进
          attachment_access.current_read_grants，同步材料层据此查表，不复制判定。
        """
        from agent.services import attachment_access

        result: dict = {"allowed": [], "denied": [], "degraded": False}
        service = self._attachment_service()
        if service is None or not turn_id:
            return result
        gw = gateway if gateway is not None else getattr(self, "authorization", None)
        try:
            items = service.list(turn_id=str(turn_id), limit=100, check=False)
        except Exception:  # noqa: BLE001 - 列附件失败 = 本轮没有可授权材料
            return result
        for att in items:
            if not bool(getattr(att, "material_enabled", 1)):
                continue
            try:
                resolved = service.resolve_source(att.id)
            except Exception:  # noqa: BLE001 - 解析失败按不可读处理
                result["denied"].append(
                    {"attachment_id": att.id, "reason": "源文件解析失败", "verdict": "unavailable"}
                )
                continue
            if resolved.path is None:
                result["denied"].append(
                    {
                        "attachment_id": att.id,
                        "reason": resolved.reason,
                        "verdict": "unavailable",
                    }
                )
                continue
            identity = (
                resolved.identity.key
                if resolved.identity is not None
                else getattr(att, "file_id", None)
            )
            decision = await attachment_access.authorize_read(
                gw,
                path=resolved.path,
                identity=identity,
                purpose=purpose,
                tool="interactive.materials",
                detail={"attachment_id": att.id, "turn_id": str(turn_id)},
            )
            if decision.get("verdict") == "allow":
                result["allowed"].append(att.id)
            else:
                result["denied"].append(
                    {
                        "attachment_id": att.id,
                        "reason": decision.get("reason"),
                        "verdict": decision.get("verdict"),
                    }
                )
                result["degraded"] = result["degraded"] or bool(decision.get("degraded"))
        return result

    def attachment_materials_facts(self, turn_id: str | None) -> list[dict]:
        """本轮附件的 §6.4 引用事实 + 用户说明（互动上下文结构化块用）。"""
        service = self._attachment_service()
        if service is None or not turn_id:
            return []
        try:
            items = service.list(turn_id=str(turn_id), limit=100, check=False)
        except Exception:  # noqa: BLE001 - 附件事实是旁路
            return []
        facts: list[dict] = []
        for att in items:
            try:
                ref = service.file_ref(att, check=False)
            except Exception:  # noqa: BLE001 - 单条引用事实失败不影响其它材料
                continue
            ref["material_enabled"] = bool(getattr(att, "material_enabled", 1))
            ref["user_note"] = str(getattr(att, "user_note", "") or "")
            facts.append(ref)
        return facts

    # -- execution narrative（模型怎么表达，见 core/narrative.py） -------------

    def _append_narrative(self, binding, narrative, call, turn_id, raw) -> str | None:
        """叙事落库（阶段与说明在同一条消息里，见 plan §1.4）。

        落库失败只记日志：过程说明是旁路，不能挡住工具执行。
        """
        try:
            message_id, _ = self.memory.append_message(
                topic_id=binding.topic_id,
                role="assistant",
                content=narrative.text,
                content_type="narrative",
                fragment_id=binding.fragment_id,
                turn_id=turn_id,
                raw=raw,
            )
        except Exception:  # noqa: BLE001 - 叙事落库失败不得影响工具执行
            logger.warning("narrative persistence failed", exc_info=True)
            return None
        return message_id

    def _message_created_at(self, message_id: str | None) -> str | None:
        if not message_id:
            return None
        row = self.conn.execute(
            "SELECT created_at FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return row["created_at"] if row is not None else None

    async def _on_narrative(self, turn_id, narrative, call, call_ids) -> str | None:
        """主 Turn 的过程说明与阶段：**先落库、再广播**（plan §1.2~§1.4）。

        落库的意义有三个：

        * 页面刷新 / 分页能拿到同一条 ``narrative_id``（前端据此去重）；
        * 批次结束后可以把系统知道的真实调用结果补写进同一行的 ``raw.calls``；
        * 阶段标识 / 序号 / 状态与历次说明都在 ``raw.stage`` 与行序里，历史可回看。

        广播的是 **STAGE**（阶段边界 + 本次说明）：阶段标识、序号、状态全部由系统
        生成，模型只能给 op 与 name。NARRATIVE 是兼容事件，主轮不再发（plan §1.3）。
        """
        from agent.api.events import EventType, make_event
        from agent.core.stage import parse_stage, stage_event_payload, stage_raw

        binding = self.bindings.binding_for(turn_id)
        if binding is None:
            # 拿不到本轮归属（子任务 / 测试 / 异常时序）：不写脏数据，也不报错。
            return None
        tracker = self.stages.tracker(turn_id)
        transition = tracker.observe(
            narrative, parse_stage(getattr(narrative, "stage", None))
        )
        raw = {
            "narrative": {
                "kind": narrative.kind,
                "tool": call.name,
                "call_id": call.id,
                "silent": narrative.silent,
            },
            "calls": [],
        }
        if transition.action == "none":
            # 不改变阶段集合（没有合法 stage 操作但已有阶段、或 text 为空、
            # 或 op=update 而当前没有阶段）：只留下说明的痕迹，不发阶段事件。
            return self._append_narrative(binding, narrative, call, turn_id, raw)
        descriptor = stage_raw(transition.stage, transition.op)
        if descriptor is not None:
            raw["stage"] = descriptor
        message_id = self._append_narrative(binding, narrative, call, turn_id, raw)
        if message_id is None:
            return None
        # 被 next 结束的旧阶段：它的说明行要回写成 done，历史回看才不会
        # 把一个已经结束的阶段显示成仍在运行。
        previous = transition.previous
        if previous is not None and (
            transition.stage is None or previous.stage_id != transition.stage.stage_id
        ):
            self._close_stage_rows(turn_id, previous.stage_id)
        await self.bus.publish(
            make_event(
                EventType.STAGE,
                stage_event_payload(
                    turn_id,
                    transition,
                    narrative_id=message_id,
                    call_id=call.id,
                    call_ids=list(call_ids or []),
                    created_at=self._message_created_at(message_id),
                ),
            )
        )
        return message_id

    def _close_stage_rows(self, turn_id: str | None, stage_id: str | None) -> None:
        """把某个阶段已经落库的说明行标成 done（plan §1.4 的 status）。

        只动 ``messages.raw.stage.status``，不改内容、不改归属；写失败只记日志。
        """
        if not turn_id or not stage_id:
            return
        try:
            rows = self.conn.execute(
                "SELECT id, raw FROM messages WHERE content_type = 'narrative' "
                "AND turn_id = ?",
                (turn_id,),
            ).fetchall()
        except Exception:  # noqa: BLE001 - 阶段收尾不得影响任何执行结果
            logger.warning("stage close read failed", exc_info=True)
            return
        for row in rows:
            try:
                raw = json.loads(row["raw"] or "{}")
            except ValueError:
                continue
            stage = raw.get("stage")
            if not isinstance(stage, dict) or stage.get("stage_id") != stage_id:
                continue
            if stage.get("status") == "done":
                continue
            stage["status"] = "done"
            try:
                self.conn.execute(
                    "UPDATE messages SET raw = ? WHERE id = ?",
                    (json.dumps(raw, ensure_ascii=False), row["id"]),
                )
            except Exception:  # noqa: BLE001 - 同上
                logger.warning("stage close write failed", exc_info=True)

    def current_stage_id(self, turn_id: str | None = None) -> str | None:
        """当前阶段 id（给 AgentLoop 的 TOOL_START / TOOL_END 用，plan §1.1）。

        只有主 Turn 有阶段；拿不到（子任务 / 已收尾）时如实返回 None，前端归入
        「整轮」，而不是就近猜一个阶段。
        """
        target = turn_id if turn_id is not None else self._active_turn_id()
        if not target or not _is_main_turn_id(target):
            return None
        return self.stages.current_stage_id(target)

    async def _close_open_stage(self, turn_id: str | None) -> None:
        """turn 收尾：结束当前阶段并广播 op=end（plan §1.3）。

        没有阶段（本轮没有过程说明）时什么都不做 —— 过程区照样由 TURN_* / TOOL_*
        的真实状态驱动。
        """
        from agent.api.events import EventType, make_event
        from agent.core.stage import stage_event_payload

        tracker = self.stages.pop(turn_id)
        if tracker is None:
            return
        transition = tracker.close()
        if transition is None or transition.stage is None:
            return
        self._close_stage_rows(turn_id, transition.stage.stage_id)
        await self.bus.publish(
            make_event(
                EventType.STAGE,
                stage_event_payload(turn_id, transition),
            )
        )

    async def _settle_narrative(self, narrative_id, results, calls, facts=None) -> None:
        """批次结束：把**系统知道的**调用结果补写进叙事记录（raw.calls）。

        这份摘要只含系统事实（工具名 / 状态 / 耗时 / 失败原因），模型文案不进入它，
        模型也无法改写它。写失败只记日志 —— 它不影响工具结果与 turn 终态。
        """
        if not narrative_id:
            return
        row = self.conn.execute(
            "SELECT raw FROM messages WHERE id = ?", (narrative_id,)
        ).fetchone()
        if row is None:
            return
        from agent.tools.display import tool_label

        try:
            raw = json.loads(row["raw"] or "{}")
        except ValueError:
            raw = {}
        summaries: list[dict] = []
        for call in calls or []:
            fact = dict((facts or {}).get(call.id) or {})
            result = (results or {}).get(call.id)
            summaries.append(
                {
                    "call_id": call.id,
                    "tool": call.name,
                    "title": tool_label(call.name),
                    "status": str(fact.get("status") or ("success" if getattr(result, "ok", False) else "failed")),
                    "error": fact.get("error") or (getattr(result, "error", None) if result is not None else None),
                    "duration_ms": fact.get("duration_ms"),
                }
            )
        raw["calls"] = summaries
        try:
            self.conn.execute(
                "UPDATE messages SET raw = ? WHERE id = ?",
                (json.dumps(raw, ensure_ascii=False), narrative_id),
            )
        except Exception:  # noqa: BLE001 - 摘要补写失败不影响任何执行结果
            logger.warning("narrative settle write failed", exc_info=True)

    def active_turn_narratives(self, turn_id: str | None = None) -> list[dict]:
        """当前（或指定）主 Turn 的叙事，供 `/api/runtime/state` 恢复界面。"""
        target = turn_id if turn_id is not None else self._active_turn_id()
        if not target or not _is_main_turn_id(target):
            return []
        rows = self.conn.execute(
            "SELECT id, content, raw, created_at FROM messages "
            "WHERE content_type = 'narrative' AND turn_id = ? "
            "ORDER BY created_at, id",
            (target,),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            try:
                raw = json.loads(row["raw"] or "{}")
            except ValueError:
                raw = {}
            meta = dict(raw.get("narrative") or {})
            out.append(
                {
                    "narrative_id": row["id"],
                    "turn_id": target,
                    "kind": str(meta.get("kind") or "progress"),
                    "text": row["content"] or "",
                    "calls": list(raw.get("calls") or []),
                    # 阶段事实（plan §1.4）：重连/刷新时按行序重放即可恢复阶段顺序、
                    # 阶段内历次说明与关联工具记录；旧数据没有这个键 → None，
                    # 前端走 legacy 平铺渲染，不伪造阶段。
                    "stage": dict(raw.get("stage") or {}) or None,
                    "created_at": row["created_at"],
                }
            )
        return out

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

    def _record_tool_call(self, trace: dict) -> str | None:
        """Persist one tool call.

        两件事：轨迹审计（截断摘要，给分析用）与**工具调用历史**（打码后的参数与
        输出全文，给用户复盘用）。返回历史记录 id；实时卡片用它在展开时取全文。
        """
        import json as _json

        from agent.memory.fragment import new_id
        from agent.storage.tool_records import record_tool_call

        from agent.trace.redact import redact_any, redact_text

        active = AnchorService(self.conn).get_active()
        try:
            # 审计行同样不得出现密钥原文：这里的参数/结果/错误都是工具给的原始值，
            # 必须与 tool_records 走同一个打码入口（trace/redact.py）。
            args = _json.dumps(redact_any(trace.get("arguments") or {}), ensure_ascii=False)[:500]
        except Exception:
            args = "{}"
        result = redact_text(str(trace.get("result") or ""))[:200]
        # 失败原因单独一列：只落输出正文时，失败的调用在轨迹表里是一片空白
        error = redact_text(str(trace.get("error") or ""))[:200]
        self.conn.execute(
            "INSERT INTO tool_calls "
            "(id, topic_id, tool_name, arguments, result, error, ok, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("tc"),
                active.topic_id if active else None,
                trace.get("tool_name", "?"),
                args,
                result,
                error,
                1 if trace.get("ok") else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()
        return record_tool_call(
            self.conn,
            turn_id=str(trace.get("turn_id") or ""),
            topic_id=active.topic_id if active else None,
            call_id=str(trace.get("call_id") or ""),
            seq=int(trace.get("seq") or 0),
            tool_name=str(trace.get("tool_name") or "?"),
            arguments=trace.get("arguments") or {},
            output=str(trace.get("result") or ""),
            status=str(trace.get("status") or ("success" if trace.get("ok") else "failed")),
            error=str(trace.get("error") or ""),
            duration_ms=trace.get("duration_ms"),
            save_output=self.settings_store.get_bool("tools.record_outputs", True),
        )

    def prune_tool_outputs(self) -> int:
        """清掉超过保留期的工具输出全文（记录本身保留）。

        启动、后台维护、保存设置三处都会调；`tools.output_retention_days = 0`
        表示永久保留。清理失败只记日志：这是维护动作，不能影响对话与启动。
        """
        from agent.storage.tool_records import DEFAULT_RETENTION_DAYS, prune_outputs

        days = self.settings_store.get_int(
            "tools.output_retention_days", DEFAULT_RETENTION_DAYS
        )
        try:
            pruned = prune_outputs(self.conn, days)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻塞启动与维护
            logger.warning("tool output prune failed: %s", exc)
            return 0
        if pruned:
            logger.info("pruned %s tool outputs older than %s days", pruned, days)
        return pruned

    def prune_tool_records(self) -> int:
        """按「记录保留天数」删掉整条工具历史（参数、错误、状态一并删）。

        **默认 0 = 永久保留**：与既有行为完全一致 —— 以前只按天清输出全文，
        记录行本身不删。只有用户显式设置天数才会隐式清理，且这里的清理
        只动 `tool_records`（用户能回看的历史）；`tool_calls` / `turn_traces`
        是审计用途，是否记录由 `trace.enabled` 决定（见 storage/tool_records.py 顶部注释）。
        """
        from agent.storage.tool_records import (
            DEFAULT_RECORD_RETENTION_DAYS,
            prune_records,
        )

        days = self.settings_store.get_int(
            "tools.record_retention_days", DEFAULT_RECORD_RETENTION_DAYS
        )
        try:
            pruned = prune_records(self.conn, days)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻塞启动与维护
            logger.warning("tool record prune failed: %s", exc)
            return 0
        if pruned:
            logger.info("pruned %s tool records older than %s days", pruned, days)
        return pruned

    async def _route_tools(self, query: str):
        """Route the tool set for one PLANNING step（契约 4：纯计算经有界执行器）.

        结构判断留在事件循环；embed/余弦等耗时部分走 ToolRouter.route_async
        的有界线程池，事件循环不再被嵌入阻塞（审批撤回、取消、健康检查可推进）。
        """
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
        return await self.tool_router.route_async(
            query, specs, pending_tasks=pending, web_allowed=True
        )

    # -- subagent notify (strategy 3: completion wakes the main agent) -----

    async def _publish_turn_queue(self, snapshot: dict) -> None:
        """广播当前 turn 队列快照（运行中 + 排队中），供前端折叠气泡展示。"""
        from agent.api.events import EventType, make_event

        await self.bus.publish(make_event(EventType.TURN_QUEUE, snapshot))

    async def _publish_turn_event(self, name: str, data: dict) -> None:
        """TurnManager 的 TURN_START / TURN_END 出口（唯一的一处）。

        TURN_END 之前做两件事（plan §1.3 / §3）：

        * 结束当前阶段并先发一条 STAGE(op=end) —— 阶段边界必须先于整轮终态；
        * 补上耗时事实：duration_ms 以 trace 台账为权威（缺失时保留 core 用单调钟
          测得的执行窗口，缺失 ≠ 0）/ started_at / ended_at。
        """
        from agent.api.events import EventType, make_event

        if name == "TURN_END":
            await self._close_open_stage(data.get("turn_id"))
            data = {**data, **self._turn_timing_facts(data.get("turn_id"))}
            # 结束事实落台账（R4 S6）：这些字段以前只随事件发一次，刷新 / 换设备后就没了 ——
            # 用户看到一轮失败、刷新后「重试」入口消失。台账是旁路，写不进去不影响发布。
            self._record_turn_facts(data)
        await self.bus.publish(make_event(EventType(name), data))

    def _record_turn_facts(self, data: dict) -> None:
        """把 TURN_END 的结束事实写进台账（只写系统确实给的事实）。

        * 台账里没有这一行 → record_facts 静默不写（旁路，不凭空建假记录）；
        * 写入异常只记日志：用户仍然必须收到 TURN_END；
        * reason 的脱敏在 turn_journal.record_facts 内部完成（不在这里二次加工）。
        """
        turn_id = str(data.get("turn_id") or "").strip()
        if not turn_id:
            return
        try:
            self.turn_journal.record_facts(
                turn_id,
                reason_code=data.get("reason_code"),
                reason=data.get("reason"),
                stopped_by=data.get("stopped_by"),
                actions=list(data.get("actions") or []),
            )
        except Exception as exc:  # noqa: BLE001 - 台账写不进去不能挡住对话
            from agent.trace.redact import redact_text

            logger.warning(
                "结束事实落台账失败（不影响事件发布）：%s", redact_text(str(exc))
            )

    def _turn_timing_facts(self, turn_id: str | None) -> dict:
        """TURN_END 的耗时事实（plan §3）：以 trace 台账为准，缺什么就不补什么。"""
        if not turn_id:
            return {}
        try:
            row = self.trace_store.get(turn_id)
        except Exception:  # noqa: BLE001 - 台账读不出来不能影响 TURN_END
            return {}
        if not row:
            return {}
        facts: dict = {}
        if row.get("duration_ms") is not None:
            facts["duration_ms"] = max(0, int(row["duration_ms"]))
        if row.get("started_at"):
            facts["started_at"] = row["started_at"]
        if row.get("ended_at"):
            facts["ended_at"] = row["ended_at"]
        return facts

    def _subtask_origin(self) -> dict:
        """程序绑定子任务 origin：当前真实用户轮 + 原话题（模型不能伪造）。"""
        active = self.turns.active
        topic = ""
        if active is not None:
            topic = str(getattr(active, "current_topic", None) or "")
        if not topic:
            try:
                topic = str(self.current_topic() or "")
            except Exception:  # noqa: BLE001 - 没有活动话题时如实留空
                topic = ""
        return {
            "turn_id": str(getattr(active, "turn_id", "") or ""),
            "topic_id": topic,
            "goal_version": getattr(active, "bound_intent_version", None),
        }

    @staticmethod
    def _notification_request_id(item) -> str:
        """通知投递的确定性请求身份（同一 task_id 只有一条通知轮）。

        用途：TurnManager 的 request_id 幂等索引在本进程内可查重。若上一次 submit
        已受理但 delivered_at 落库失败（或两步之间被打断），再次投递时先查这个 id，
        已存在就不再重复提交，直接补记交付 —— 把「崩溃窗口内重复投递」压到仅剩
        跨进程重启这一条（诚实边界见 subtask_notifications 模块 docstring）。
        """
        return SUBTASK_NOTIFICATION_REQUEST_ID_PREFIX + str((item or {}).get("task_id") or "")

    def _format_notice(self, task_id: str, record) -> str:
        """兼容名：通知正文统一由 subtask_notifications 渲染（固定文案来自 prompts）。"""
        from agent.tools.subtask_notifications import build_notification, format_completion_notice

        return format_completion_notice(build_notification(task_id, record))

    async def _handle_subagent_notify(self, task_id: str, record=None) -> None:
        """子任务收口：只入队（去重），空闲时再取一条作为自己的通知轮。

        不打断当前交流：有 active 用户轮时留在队列里，轮末再取；处理另一条通知
        期间到达的通知也不会丢。取消 / 迟到结果不入队（不复活已取消任务）。
        """
        if self._notify_turn:
            # 当前通知轮正在跑：入队即可，它结束时统一 drain。
            try:
                self.subtask_notifications.enqueue(task_id, record)
            except Exception:  # noqa: BLE001 - 通知入队失败只记日志
                logging.getLogger(__name__).warning(
                    "subtask notification enqueue failed", exc_info=True
                )
            return
        try:
            enqueued = self.subtask_notifications.enqueue(task_id, record)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "subtask notification enqueue failed", exc_info=True
            )
            return
        if enqueued:
            self._schedule_subtask_notification_pump()

    def _schedule_subtask_notification_pump(self) -> None:
        """在当前 turn 真正收尾后再尝试取通知（TurnManager 清 active 在 runner 之后）。"""

        async def _later() -> None:
            await asyncio.sleep(0)
            await self._pump_subtask_notifications()

        try:
            asyncio.get_running_loop().create_task(_later())
        except RuntimeError:
            return

    async def _pump_subtask_notifications(self) -> None:
        """空闲时取一条未送达通知作为自己的通知轮；不在 active 用户轮里打断当前交流。

        FU2-D 的交付判据：只有 turns.submit **成功受理**后才写 delivered_at（唯一事实）；
        失败写 attempts / last_error 并保留待重试，安排一次延迟重试，绝不当成已交付。
        """
        if self._notify_turn or self.turns.active is not None:
            return
        # 用户消息还在排队：先让用户轮执行，通知轮排在其后（不抢占）。
        if self.turns.queued_count():
            return
        item = self.subtask_notifications.take()
        if item is None:
            return
        from agent.tools.subtask_notifications import format_completion_notice

        origin = item.get("origin") or {}
        topic = str(origin.get("topic_id") or "") or None
        # 确定性 request_id：本进程内已受理过同一条通知就不再重复提交（只补记交付）。
        request_id: str | None = None
        try:
            parameters = inspect.signature(self.turns.submit).parameters
        except (TypeError, ValueError):  # pragma: no cover - 内置/替身签名不可省
            parameters = {}
        if "request_id" in parameters:
            request_id = self._notification_request_id(item)
            lookup = getattr(self.turns, "lookup_request", None)
            if callable(lookup):
                try:
                    existing = lookup(request_id)
                except Exception:  # noqa: BLE001 - 查重失败按未受理处理（宁可重试一次）
                    existing = None
                if existing is not None:
                    logging.getLogger(__name__).info(
                        "subtask notification already accepted in this process: %s",
                        request_id,
                    )
                    try:
                        self.subtask_notifications.mark_delivered(item)
                    except Exception:  # noqa: BLE001 - 补记失败仍要可观测
                        logging.getLogger(__name__).warning(
                            "subtask notification backfill delivered failed", exc_info=True
                        )
                    return
        try:
            if request_id is None:
                turn = self.turns.submit(format_completion_notice(item), topic, notify=True)
            else:
                turn = self.turns.submit(
                    format_completion_notice(item), topic, notify=True, request_id=request_id
                )
        except Exception as exc:  # noqa: BLE001 - 提交失败 ≠ 交付：保留待重试
            logging.getLogger(__name__).warning(
                "subtask notification turn submit failed", exc_info=True
            )
            self.subtask_notifications.mark_failed(
                item, f"{type(exc).__name__}: {str(exc)[:200]}"
            )
            self._schedule_subtask_notification_retry()
            return
        # 被 TurnManager 接受 = 交付事实：立刻写 delivered_at（同一条永不重投）。
        try:
            self.subtask_notifications.mark_delivered(item)
        except Exception:  # noqa: BLE001 - 已交给 TurnManager，落库失败必须可观测
            logging.getLogger(__name__).warning(
                "subtask notification mark delivered failed", exc_info=True
            )
        # 通知轮携带结构化事实（身份 / 状态 / 结果引用 / origin），供轮末原话题续作。
        try:
            turn.subtask_notification = item
        except Exception:  # noqa: BLE001 - TurnContext 允许动态属性，失败不影响执行
            pass

    def _schedule_subtask_notification_retry(self) -> None:
        """投递失败后安排一次延迟重试（只重试，不伪造交付）。"""
        if self._notify_retry_task is not None and not self._notify_retry_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._notify_retry_task = loop.create_task(self._subtask_notification_retry_later())

    async def _subtask_notification_retry_later(self) -> None:
        try:
            await asyncio.sleep(max(0.0, float(self.subtask_notification_retry_delay)))
            await self._pump_subtask_notifications()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 重试失败只记日志，记录仍在库里
            logging.getLogger(__name__).warning(
                "subtask notification retry failed", exc_info=True
            )
        finally:
            self._notify_retry_task = None

    def cancel_notification_retry(self) -> None:
        """取消待执行的延迟重试（测试 / 关闭收尾用；不删库里的待投递记录）。"""
        task = self._notify_retry_task
        self._notify_retry_task = None
        if task is not None and not task.done():
            task.cancel()

    async def resume_subtask_notifications(self) -> int:
        """重启恢复：把库里未送达的通知继续投递；返回恢复前的未送达条数。"""
        try:
            pending = self.subtask_notifications.pending_count()
        except Exception:  # noqa: BLE001 - 读不到如实记日志，不假装没有待投递
            logging.getLogger(__name__).warning(
                "subtask notification pending count failed", exc_info=True
            )
            return 0
        if pending:
            logging.getLogger(__name__).info(
                "subtask notifications: 恢复 %s 条未送达通知，继续投递", pending
            )
            await self._pump_subtask_notifications()
        return pending

    def _prime_subtask_notification_delivery(self) -> None:
        """启动时加载未送达通知并继续投递（不依赖用户先说话）。"""
        try:
            pending = self.subtask_notifications.pending_count()
        except Exception:  # noqa: BLE001 - 队列不可用要可见，但不阻止启动
            logging.getLogger(__name__).warning(
                "subtask notification prime failed", exc_info=True
            )
            return
        if pending <= 0:
            return
        try:
            asyncio.get_running_loop().create_task(self.resume_subtask_notifications())
        except RuntimeError:
            # 没有事件循环（同步构造 / 单测）：记录仍在库里，等第一次轮末/空闲再投递。
            logging.getLogger(__name__).info(
                "subtask notifications: %s 条未送达（当前无事件循环，稍后投递）", pending
            )

    async def _execute_notify_turn(self, ctx) -> None:
        """System-driven turn: main agent reacts to a finished subagent task."""
        from agent.core.loop import AgentLoop
        from agent.security.context import approval_origin

        # 系统通知轮不是用户指令：不产生 grant，审批来源如实标为 background。
        origin_token = approval_origin.set("background")
        self._notify_turn = True
        # FU3-D：通知轮的话题**只解析一次**，并在本轮所有落点复用同一结果。
        # 优先「原任务话题」（turns.submit 已按 origin 把它写进 ctx.initial_topic），
        # 只有它缺失时才回退界面当前话题。通知轮是原任务的续作，不是用户此刻
        # 正在看的那个话题 —— 用 current_topic() 会把结果写进另一个话题。
        initial_topic = getattr(ctx, "initial_topic", None)
        initial_text = str(initial_topic).strip() if initial_topic is not None else ""
        # 两者都缺失时保持 None（与基线一致：不把「没有话题」伪造成空串话题）。
        resolved_topic = initial_text or self.current_topic()
        try:
            self.approvals.set_context(turn_id=ctx.turn_id, session_id=self.session_id)
            tracer = getattr(ctx, "trace", None)
            if tracer is None:
                from agent.trace.recorder import TurnTracer

                tracer = TurnTracer(self.trace_store, ctx.turn_id)
                ctx.trace = tracer
            self.trace_store.ensure_started(ctx.turn_id, initial_topic=resolved_topic)
            with tracer.phase("adapter_setup"):
                adapter = await self.build_adapter()
            if adapter is None:
                ctx.result = {"ok": False, "reason": "no_credential"}
                ctx.status = "unavailable"
                ctx.error = "no_credential"
                self.trace_store.finish(ctx.turn_id, "unavailable", error="no_credential")
                return
            # 同一解析结果贯穿：上下文 / 绑定 / trace / 后续保存都用它。
            topic = resolved_topic
            ctx.current_topic = topic
            # 系统驱动的轮也要有自己的绑定（归属明确），并且标记成 system：
            # 它不占用户轮次容量，也不该被当成「用户开了新的一轮」。
            ctx.bound_topic = topic
            ctx.bound_fragment_id = None
            self.bindings.record_binding(ctx.turn_id, topic, system=True)
            self.trace_store.set_initial_topic(ctx.turn_id, topic)
            notice = ctx.message
            with tracer.phase("context_assembly"):
                with tracer.phase("topic_prediction"):
                    prediction = self.predictor.predict(notice, current_topic_id=topic)
                with tracer.phase("retrieval"):
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
            # 统一消息组装（契约 §3）：系统通知作为本轮 user 原文，长期记忆等上下文
            # 走结构化 ContextBlock（固定标签由 prompts.CONTEXT_LABELS 渲染），
            # 不再用「【系统通知】」中文标题把两段拼成一条消息。
            from agent.core.message_assembly import (
                ContextBlock,
                MediaParts,
                build_initial_messages,
            )

            blocks = []
            if payload.text:
                blocks.append(ContextBlock("memory", {"legacy_text": payload.text}))
            mode_rules = ""
            mode_getter = getattr(adapter, "mode_rules_text", None)
            if callable(mode_getter):
                try:
                    mode_rules = str(mode_getter(self.registry.specs()) or "")
                except Exception:  # noqa: BLE001 - 模式规则失败不能挡住通知轮
                    logging.getLogger(__name__).warning("notify mode rules failed", exc_info=True)
                    mode_rules = ""
            initial_messages = build_initial_messages(
                user_text=notice,
                media=MediaParts(),
                mode_rules=mode_rules,
                blocks=blocks,
            )
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
                # 系统驱动的轮没有叙事出口，阶段通常为空；这里仍然给出同一个
                # 提供者，TOOL_START / TOOL_END 的 stage_id 才有统一口径（可为 null）。
                stage_id_provider=lambda: self.current_stage_id(ctx.turn_id),
                # 长正文暂存目录：显式用 AppContext 的 data_dir（不惰性解析 Settings()）
                spill_dir=Path(self.settings.data_dir) / "tmp",
            )
            ctx.loop = loop
            try:
                with tracer.phase("agent_loop"):
                    result = await loop.run(notice, initial_messages=initial_messages)
            finally:
                ctx.loop = None
            if ctx.cancelled or result.cancelled:
                ctx.cancelled = True
                self.trace_store.finish(ctx.turn_id, "cancelled")
                return
            # feedback enters memory (assistant message; no user message)
            from agent.services.turn_orchestrator import verification_raw

            with tracer.phase("persistence"):
                notify_msg_id, _ = self.memory.append_message(
                    topic_id=topic,
                    role="assistant",
                    content=result.final_content or "",
                    content_type="text",
                    model=adapter.model,
                    raw=verification_raw(result),
                    turn_id=ctx.turn_id,
                )
            tracer.write("messages", notify_msg_id)
            ctx.final_content = result.final_content
            ctx.final_verification = getattr(result, "verification", None)
            ctx.final_annotation = getattr(result, "final_annotation", None)
            ctx.result = {"ok": True, "turn": result.__dict__}
            ctx.usage = {
                "iterations": result.iterations_used,
                "tokens": result.tokens_used,
                "tool_calls": result.tool_calls_made,
            }
            with tracer.phase("memory_post"):
                fragment = self.fragments.get_or_create_open(topic)
                if self.fragments.should_close(fragment):
                    # 通知轮是真实运行时路径：三种派生各自走自己的用途 adapter。
                    closed = await self._close_fragment(
                        topic, adapter, tracer=tracer, usage_adapters=True
                    )
                    if closed is not None:
                        # 索引在 close_fragment 内部已增量更新（不再全量重建）；
                        # 话题向量刷新走工作线程，取消后不写回。
                        await self.refresh_topic_vector_offloaded(topic, ctx=ctx)
            with tracer.phase("finalize"):
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
            approval_origin.reset(origin_token)
            self.approvals.set_context(turn_id=None, session_id=self.session_id)
            self._schedule_subtask_notification_pump()

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

    async def refresh_topic_vector_offloaded(self, topic_id: str, ctx=None) -> bool:
        """封块之后刷新话题向量：读输入 → 线程里嵌入 → 回事件循环写回。

        契约 WS3 §3：嵌入（ONNX 推理）搬进有上限的工作线程；数据库写回留在
        事件循环一侧。这一轮已经被取消（`ctx.cancelled`）时**不写回** ——
        过时的计算结果不许提交。返回是否真的写回了。
        """
        predictor = self.predictor
        plan = predictor.plan_topic_refresh(topic_id)
        if plan is None:
            return False
        vector = await self.heavy.run(predictor.compute_topic_refresh, plan)
        if ctx is not None and getattr(ctx, "cancelled", False):
            return False
        return predictor.commit_topic_refresh(plan, vector)

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

    def _turn_entity_cards(self, ctx: Any) -> list[Any]:
        """本轮实际会看到的实体（消息命中的卡 + 当前话题挂着的卡），供证据目录使用。"""
        try:
            from agent.entities.cards import EntityCardService

            svc = EntityCardService(self.conn)
            cards: dict[str, Any] = {}
            for card in svc.match_cards(getattr(ctx, "message", "") or ""):
                cards.setdefault(card.id, card)
            topic_id = getattr(ctx, "current_topic", None)
            if topic_id:
                for node_id in self._topic_entity_ids(topic_id):
                    row = self.conn.execute(
                        "SELECT id FROM entity_cards WHERE node_id = ? AND state = 'active'",
                        (node_id,),
                    ).fetchone()
                    if row is not None:
                        card = svc.get(row["id"])
                        if card is not None:
                            cards.setdefault(card.id, card)
            return list(cards.values())
        except Exception:  # noqa: BLE001 - 证据目录是校验依据，但取实体失败不应打断一轮
            logger.warning("turn entity evidence failed", exc_info=True)
            return []

    def _turn_evidence_catalog(self):
        """本轮程序证据目录（Phase 1 C）。

        惰性构造：工具在校验 evidence_ids 时才调用，因此本轮已经落库的用户消息、
        附件与**已经真实完成**的工具调用都在目录里；回合结束不需要额外清理
        （没有任何跨轮缓存）。
        """
        from agent.knowledge.evidence import EvidenceCatalog, build_turn_catalog

        active = self.turns.active
        if active is None:
            return EvidenceCatalog.empty()
        return build_turn_catalog(
            self.conn,
            turn_id=active.turn_id,
            message=getattr(active, "message", "") or "",
            knowledge_snapshot=list(active.knowledge_snapshot or []),
            entity_cards=self._turn_entity_cards(active),
        )

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
        """委派给 TurnOrchestrator（见 agent/services/turn_orchestrator.py）。

        R9：每轮用户指令建立独立的 InstructionContext（授权依据），轮末即关闭
        全部 grant —— 绝不因为同话题/同目录/历史许可而延续到后续指令。
        """
        from agent.security.context import (
            approval_origin_context,
            instruction_context,
            requirement_context,
            requirement_limits_context,
            requirement_resolution_context,
        )

        instruction = None
        try:
            instruction = self.instructions.begin(
                turn_id=str(getattr(ctx, "turn_id", "") or ""),
                text=str(getattr(ctx, "message", "") or ""),
                topic_id=getattr(ctx, "initial_topic", None),
            )
        except Exception:  # noqa: BLE001 - 指令提取失败不能挡住一轮（保守=没有 grant）
            logging.getLogger(__name__).warning("instruction begin failed", exc_info=True)
        # R9-H（契约 §3）：先由模型理解这条用户消息是新要求/补充/修正/撤回，再由
        # 后端应用并形成**具体要求的版本绑定**；操作只使用这个绑定，不再取
        # "最近一条 active 要求"。解析器不可用/失败时按新要求继续并标注 unavailable
        # （不关键词兜底，也不静默扩大旧要求）。
        requirement_binding = None
        resolution_status = "unavailable"
        resolution_limits: tuple[dict, ...] = ()
        requirement_error: str | None = None
        turn_id = str(getattr(ctx, "turn_id", "") or "")
        if instruction is not None and self.requirements is not None:
            (
                requirement_binding,
                resolution_status,
                resolution_limits,
                requirement_error,
            ) = await self._prepare_requirement_binding(ctx, instruction)
        # 要求解析是本轮开始前的一次模型调用；如果用户在这期间点了停止，本轮
        # 可能还没进入编排器就结束。此时仍要留下「已取消」的轮绑定（不能因为
        # 提前取消就没有任何终态记录）。
        if getattr(ctx, "cancelled", False) and str(turn_id):
            try:
                if self.bindings.binding_for(turn_id) is None:
                    topic = getattr(ctx, "initial_topic", None) or ""
                    self.bindings.record_binding(turn_id, topic)
                    self.bindings.mark_status(turn_id, "cancelled")
            except Exception:  # noqa: BLE001 - 终态记录失败不挡住取消返回
                logging.getLogger(__name__).warning(
                    "取消轮绑定记录失败", exc_info=True
                )
        try:
            if instruction is None:
                await self.turn_orchestrator.execute(ctx)
            else:
                with instruction_context(instruction), approval_origin_context("user"), \
                        requirement_context(requirement_binding), \
                        requirement_resolution_context(resolution_status), \
                        requirement_limits_context(resolution_limits):
                    await self.turn_orchestrator.execute(ctx)
        finally:
            if instruction is not None:
                self.instructions.end(instruction)
            # 轮末尝试取一条子任务通知（不打断刚结束的交流；有排队用户轮时延后）。
            self._schedule_subtask_notification_pump()

    # -- R9-H：要求解析 → 有效版本绑定 → 历史限制重新理解 -------------------

    @staticmethod
    def _turn_message_id(ctx: Any) -> str | None:
        """本轮真实用户消息 id；拿不到时为 None，**不**用 turn_id/topic_id 冒充。

        解析发生在编排器写用户消息之前：新轮通常还没有 message_id；恢复/续作轮
        ctx.user_message_id 可能已由台账恢复，此时原样使用。两者互不串用。
        """
        value = getattr(ctx, "user_message_id", None)
        if value is None:
            value = getattr(ctx, "message_id", None)
        return None if value is None or str(value) == "" else str(value)

    @staticmethod
    def _applied_limits(applied: Any) -> tuple[dict, ...]:
        """取 AppliedResolution 的结构化限制（C 新增 structured_limits 时优先）。

        R9-H11 §7：structured 状态下以**台账已保存行**为准——AppliedResolution 里的
        快照可能形成于 program_binding 固化之前，本轮生效限制不能因此丢绑定
        （否则执行层会把已绑定对象当成未绑定/未知）。
        """
        from agent.security import requirement_limits

        requirement = getattr(applied, "requirement", None)
        if requirement is not None and (
            str(getattr(requirement, "structured_limits_state", "") or "")
            == requirement_limits.STATE_STRUCTURED
        ):
            saved = getattr(requirement, "structured_limits", None)
            if saved is not None:
                return tuple(dict(item) for item in saved if isinstance(item, dict))
        values = getattr(applied, "structured_limits", None)
        if values is None:
            values = getattr(applied, "limits", ())
        return tuple(dict(item) for item in (values or ()) if isinstance(item, dict))

    @staticmethod
    def _applied_relation(applied: Any) -> str:
        relation = getattr(applied, "relation", None)
        return str(getattr(relation, "value", relation) or "").strip().lower()

    def _binding_for_requirement(self, requirement: Any) -> Any:
        if requirement is None or self.requirements is None:
            return None
        binder = getattr(self.requirements, "binding_for", None)
        if not callable(binder):
            return None
        try:
            return binder(str(getattr(requirement, "requirement_id", "")))
        except Exception:  # noqa: BLE001 - 台账异常按"没有有效绑定"处理（需确认）
            logging.getLogger(__name__).warning("读取要求绑定失败", exc_info=True)
            return None

    def _note_requirement_issue(
        self,
        ctx: Any,
        *,
        code: str,
        error: Any,
        message_id: str | None,
        turn_id: str,
        relation: str | None = None,
        target_requirement_id: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        audit: bool = True,
    ) -> str:
        """记录"解析成功但绑定失败"等明确错误（日志 + 轮上下文 + 台账审计）。"""
        detail = str(code) + ": " + type(error).__name__ + ": " + str(error)[:300]
        logging.getLogger(__name__).warning("要求解析/绑定异常（%s）：%s", code, detail)
        notices = getattr(ctx, "notices", None)
        if isinstance(notices, list):
            notices.append("要求解析/绑定异常（" + str(code) + "），本轮相关操作需要确认")
        if audit and self.requirements is not None:
            recorder = getattr(self.requirements, "record_resolution_failure", None)
            if callable(recorder):
                try:
                    recorder(
                        error=detail,
                        message_id=message_id or None,
                        turn_id=turn_id or None,
                        relation=relation,
                        target_requirement_id=target_requirement_id,
                        model=model,
                        prompt_version=prompt_version,
                    )
                except Exception:  # noqa: BLE001 - 审计失败不挡住用户轮
                    logging.getLogger(__name__).warning(
                        "要求绑定失败审计写入失败", exc_info=True
                    )
        return detail

    async def _prepare_requirement_binding(
        self, ctx: Any, instruction: Any
    ) -> tuple[Any, str, tuple[dict, ...], str | None]:
        """把"这条用户消息"落实成真实要求 + 有效版本绑定（R9-H 契约 §3）。

        返回 (binding, resolution_status, resolution_limits, error)。
        * new/amend 必须用 AppliedResolution.requirement + binding_for 建立绑定；
          建立失败 → 记录明确错误、status=unavailable，**不**回退登记另一条要求；
        * withdraw 不绑定已撤回要求（binding=None，status=ok）；
        * 解析失败 → 按新要求登记并标 unavailable（不扩大旧授权）；
        * 取得绑定后按需重新理解历史结构化限制（legacy/unresolved）。
        """
        requirements = self.requirements
        message_id = self._turn_message_id(ctx)
        turn_id = str(getattr(ctx, "turn_id", "") or "")
        topic_id = getattr(ctx, "initial_topic", None)
        resolver = getattr(self, "requirement_resolver", None)
        resolution = None
        failure_class = "resolver_missing"
        if resolver is None:
            self._note_requirement_issue(
                ctx,
                code="resolution_unavailable",
                error=RuntimeError("要求解析器未启用"),
                message_id=message_id,
                turn_id=turn_id,
                model=None,
                prompt_version=None,
            )
        else:
            try:
                candidates = requirements.candidates(limit=12)
                resolution = await resolver.resolve(
                    message=instruction.text,
                    message_id=message_id,
                    turn_id=turn_id,
                    candidates=candidates,
                )
                if resolution is None:
                    # 解析器返回 None / 输出未通过结构校验：同样属于“未成功解析”。
                    failure_class = "invalid_output"
                    self._note_requirement_issue(
                        ctx,
                        code="resolution_unavailable",
                        error=RuntimeError("解析器输出非法或为空"),
                        message_id=message_id,
                        turn_id=turn_id,
                        model=getattr(resolver, "model", None),
                        prompt_version=getattr(resolver, "prompt_version", None),
                    )
            except Exception as exc:  # noqa: BLE001 - 解析失败按新要求继续
                failure_class = type(exc).__name__
                self._note_requirement_issue(
                    ctx,
                    code="resolution_unavailable",
                    error=exc,
                    message_id=message_id,
                    turn_id=turn_id,
                    model=getattr(resolver, "model", None),
                    prompt_version=getattr(resolver, "prompt_version", None),
                )
                resolution = None

        if resolution is not None:
            resolved_relation = str(
                getattr(getattr(resolution, "relation", None), "value", getattr(resolution, "relation", ""))
                or ""
            ).lower()
            try:
                applied = requirements.apply_resolution(
                    resolution,
                    message_id=message_id,
                    turn_id=turn_id,
                    topic_id=topic_id,
                    message=instruction.text,
                )
            except Exception as exc:  # noqa: BLE001 - apply 失败不得回退登记/静默成功
                error = self._note_requirement_issue(
                    ctx,
                    code="apply_resolution_failed",
                    error=exc,
                    message_id=message_id,
                    turn_id=turn_id,
                    relation=resolved_relation or None,
                    target_requirement_id=getattr(resolution, "target_requirement_id", None),
                    model=getattr(resolver, "model", None),
                    prompt_version=getattr(resolution, "prompt_version", None),
                    audit=False,  # apply_resolution 已写 rejected 审计，不重复
                )
                return None, "unavailable", (), error

            relation = self._applied_relation(applied)
            requirement = getattr(applied, "requirement", None)
            limits = self._applied_limits(applied)
            if relation in ("new", "amend"):
                problem: Exception | None = None
                if requirement is None:
                    problem = RuntimeError(
                        "AppliedResolution.requirement 为空：无法按真实要求建立绑定"
                    )
                else:
                    binding = self._binding_for_requirement(requirement)
                    if binding is None:
                        problem = RuntimeError(
                            "new/amend 后无法取得有效版本绑定（要求 id="
                            + str(getattr(requirement, "requirement_id", ""))
                            + "）"
                        )
                    else:
                        requirement_version = int(getattr(requirement, "version", -1) or -1)
                        binding_version = int(getattr(binding, "version", -1) or -1)
                        if requirement_version != binding_version:
                            problem = RuntimeError(
                                "要求版本与绑定版本不一致（要求=%r，绑定=%r）"
                                % (requirement_version, binding_version)
                            )
                if problem is not None:
                    error = self._note_requirement_issue(
                        ctx,
                        code="binding_unavailable",
                        error=problem,
                        message_id=message_id,
                        turn_id=turn_id,
                        relation=relation,
                        target_requirement_id=(
                            None
                            if requirement is None
                            else str(getattr(requirement, "requirement_id", "")) or None
                        ),
                        prompt_version=getattr(resolution, "prompt_version", None),
                    )
                    return None, "unavailable", limits, error
                recovered_limits = await self._ensure_legacy_structured_limits(
                    ctx, binding, message_id=message_id, turn_id=turn_id
                )
                if recovered_limits is not None:
                    limits = recovered_limits
                return binding, "ok", limits, None

            if relation == "withdraw":
                if requirement is not None and str(getattr(requirement, "status", "")) == "active":
                    problem = RuntimeError("withdraw 后要求仍为 active，台账状态异常")
                    error = self._note_requirement_issue(
                        ctx,
                        code="withdraw_state_invalid",
                        error=problem,
                        message_id=message_id,
                        turn_id=turn_id,
                        relation=relation,
                        target_requirement_id=str(
                            getattr(requirement, "requirement_id", "") or ""
                        )
                        or None,
                        prompt_version=getattr(resolution, "prompt_version", None),
                    )
                    return None, "unavailable", limits, error
                # 撤回成功：不得绑定已撤回要求；本轮后续需要确认，不沿用旧授权。
                return None, "ok", limits, None

            problem = RuntimeError("未知的要求关系：" + str(relation))
            error = self._note_requirement_issue(
                ctx,
                code="unknown_relation",
                error=problem,
                message_id=message_id,
                turn_id=turn_id,
                relation=relation or None,
                prompt_version=getattr(resolution, "prompt_version", None),
            )
            return None, "unavailable", limits, error

        # 解析失败/解析器缺失：按新要求登记并**显式**标 unresolved（R9-H2 §4）：
        # 不能把"未解析成功"落成 structured/[]（那会被当成"已确认无限制"）。
        try:
            recorded = await self._record_unresolved_requirement(
                text=instruction.text,
                message_id=message_id,
                turn_id=turn_id,
                topic_id=topic_id,
                failure_class=failure_class,
            )
        except Exception as exc:  # noqa: BLE001 - 台账失败不阻塞用户轮，但审查会退回审批
            error = self._note_requirement_issue(
                ctx,
                code="fallback_record_failed",
                error=exc,
                message_id=message_id,
                turn_id=turn_id,
            )
            return None, "unavailable", (), error
        binding = self._binding_for_requirement(recorded)
        if binding is None:
            problem = RuntimeError(
                "解析失败后登记的新要求无法取得有效绑定（要求 id="
                + str(getattr(recorded, "requirement_id", ""))
                + "）"
            )
            error = self._note_requirement_issue(
                ctx,
                code="fallback_binding_unavailable",
                error=problem,
                message_id=message_id,
                turn_id=turn_id,
                relation="new",
                target_requirement_id=str(getattr(recorded, "requirement_id", "")) or None,
            )
            return None, "unavailable", (), error
        # 解析失败的新消息本身不立即再送模型"重新理解成 structured"（R9-H2 §4.2）；
        # 历史恢复只针对已有 legacy/unresolved 且用户继续操作的要求，由下一次
        # 解析成功的 new/amend 路径触发 _ensure_legacy_structured_limits。
        return binding, "unavailable", (), None

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _setter_parameter_names(setter: Any) -> set[str]:
        try:
            return set(inspect.signature(setter).parameters)
        except (TypeError, ValueError):  # pragma: no cover - 内置/动态签名
            return set()

    async def _call_structured_setter(
        self,
        requirement_id: str,
        limits: Sequence[dict],
        *,
        source: str,
        state: str,
        expected: dict | None = None,
        attach_bindings: bool | None = None,
    ) -> Any:
        """调用台账 setter；A 分支的 expected_* 条件参数存在才传（兼容旧分支）。

        R9-H13 §3.A1：``attach_bindings=False`` 表示“原样回写已有 known limits”，
        不得对历史对象重新查找/补建绑定；仅当 setter 支持该参数时才传。
        """
        setter = getattr(self.requirements, "set_structured_limits", None)
        if not callable(setter):
            return None
        kwargs: dict[str, Any] = {"source": str(source)[:300], "state": str(state)}
        supported = self._setter_parameter_names(setter)
        for key, value in dict(expected or {}).items():
            if value is not None and key in supported:
                kwargs[key] = value
        if attach_bindings is not None and "attach_bindings" in supported:
            kwargs["attach_bindings"] = bool(attach_bindings)
        try:
            written = setter(requirement_id, tuple(limits), **kwargs)
        except TypeError:
            # 旧分支没有条件写参数：退回最小参数（调用方仍会重读核对）。
            written = setter(requirement_id, tuple(limits), source=str(source)[:300], state=str(state))
        return await self._maybe_await(written)

    async def _record_unresolved_requirement(
        self,
        *,
        text: str,
        message_id: str | None,
        turn_id: str,
        topic_id: str | None,
        failure_class: str,
    ) -> Any:
        """解析失败时登记新要求：显式 unresolved，绝不当成 structured/[]。

        A 分支的 record_user_message 支持 structured_state/structured_source；
        未合入时退回真实台账已有的 set_structured_limits（仍保证持久化状态一致）。
        """
        from agent.security import requirement_limits

        source = "resolution_unavailable:" + str(failure_class or "unknown")
        base = dict(
            text=text,
            message_id=message_id,
            turn_id=turn_id,
            topic_id=topic_id,
        )
        recorded = None
        try:
            recorded = self.requirements.record_user_message(
                **base,
                structured_state=requirement_limits.STATE_UNRESOLVED,
                structured_source=source,
            )
            recorded = await self._maybe_await(recorded)
        except TypeError:
            recorded = self.requirements.record_user_message(**base)
            recorded = await self._maybe_await(recorded)
        requirement_id = str(getattr(recorded, "requirement_id", "") or "")
        if not requirement_id:
            return recorded
        current = None
        try:
            current = self.requirements.by_id(requirement_id)
        except Exception:  # noqa: BLE001 - 读不到时直接用返回值判断
            current = recorded
        state = str(getattr(current, "structured_limits_state", "") or "")
        if state == requirement_limits.STATE_UNRESOLVED:
            return current or recorded
        try:
            written = await self._call_structured_setter(
                requirement_id,
                (),
                source=source,
                state=requirement_limits.STATE_UNRESOLVED,
            )
            if written is not None:
                return written
        except Exception:  # noqa: BLE001 - setter 不可用/失败：继续尝试兼容写
            logging.getLogger(__name__).warning(
                "解析失败后的 unresolved 状态写入失败", exc_info=True
            )
        # 最后的兼容兜底：直接更新台账状态（绝不留成 structured/[]）。
        conn = getattr(self.requirements, "conn", None)
        if conn is not None:
            try:
                from agent.storage.db import transaction

                with transaction(conn):
                    conn.execute(
                        "UPDATE user_requirements SET structured_limits_state = ?,"
                        " structured_limits_source = ? WHERE id = ?",
                        (
                            requirement_limits.STATE_UNRESOLVED,
                            source[:300],
                            requirement_id,
                        ),
                    )
            except Exception:  # noqa: BLE001 - 持久化失败本轮仍需继续（但状态未确认）
                logging.getLogger(__name__).warning(
                    "解析失败后的 unresolved 状态兼容写入失败", exc_info=True
                )
        return current or recorded

    @staticmethod
    def _limit_prompt_version(resolver: Any, *, chain: bool) -> str:
        names = (
            ("limit_extraction_chain_prompt_version", "chain_limit_extraction_prompt_version")
            if chain
            else ("limit_extraction_prompt_version",)
        )
        for name in names + ("prompt_version",):
            value = getattr(resolver, name, None)
            if value:
                return str(value)
        return "r9h2-limit-extraction-chain-v1" if chain else "r9h-limit-extraction-v1"

    @staticmethod
    def _requirement_key_state(requirement: Any) -> dict:
        return {
            "requirement_id": str(getattr(requirement, "requirement_id", "") or ""),
            "version": int(getattr(requirement, "version", 0) or 0),
            "status": str(getattr(requirement, "status", "") or ""),
            "parent_id": (
                None
                if getattr(requirement, "parent_id", None) is None
                else str(getattr(requirement, "parent_id"))
            ),
        }

    def _append_history_notice(self, ctx: Any, message: str) -> None:
        notices = getattr(ctx, "notices", None)
        if isinstance(notices, list):
            notices.append(str(message))

    async def _mark_limits_unresolved(
        self,
        ctx: Any,
        requirement: Any,
        *,
        code: str,
        version: int | None = None,
        chain_digest: str | None = None,
    ) -> None:
        """恢复失败：保留已有 known limits，写 unresolved，不覆盖为“无限制”。"""
        from agent.security import requirement_limits

        known_raw = getattr(requirement, "structured_limits", None)
        known = tuple(
            dict(item) for item in (known_raw or ()) if isinstance(item, dict)
        )
        source = str(code)
        if chain_digest:
            source += ";chain=" + str(chain_digest)
        if version is not None:
            source += ";version=" + str(version)
        requirement_id = str(getattr(requirement, "requirement_id", "") or "")
        try:
            await self._call_structured_setter(
                requirement_id,
                known,
                source=source,
                state=requirement_limits.STATE_UNRESOLVED,
                expected={
                    "expected_version": version,
                    "expected_status": "active",
                    "expected_chain_digest": chain_digest,
                },
                # 只是把已有 known limits 标为未解决：不得重新查找/绑定历史对象。
                attach_bindings=False,
            )
        except Exception:  # noqa: BLE001 - 失败保留原状态，不阻塞本轮
            logging.getLogger(__name__).warning(
                "历史要求限制 unresolved 状态写入失败", exc_info=True
            )
        self._append_history_notice(ctx, "历史用户限制暂时无法恢复，相关操作需要确认")

    @staticmethod
    def _recovery_context_matches(
        expected: Any, fresh: Any, requirement: Any, *, requirement_id: str, version: int
    ) -> bool:
        """恢复返回后重新读：id/version/status/parent/chain_digest 任一变化即丢弃。"""
        if fresh is None or requirement is None:
            return False
        if str(getattr(requirement, "status", "")) != "active":
            return False
        if str(getattr(requirement, "requirement_id", "")) != requirement_id:
            return False
        if int(getattr(requirement, "version", 0) or 0) != int(version):
            return False
        expected_parent = getattr(expected, "parent_id", None)
        fresh_parent = getattr(fresh, "parent_id", None)
        if str(expected_parent or "") != str(fresh_parent or ""):
            return False
        expected_digest = str(getattr(expected, "chain_digest", "") or "")
        fresh_digest = str(getattr(fresh, "chain_digest", "") or "")
        if expected_digest and fresh_digest and expected_digest != fresh_digest:
            return False
        if not bool(getattr(fresh, "complete", False)):
            return False
        return True

    async def _recover_limits_from_chain(
        self,
        ctx: Any,
        requirement: Any,
        *,
        recovery: Any,
        extractor: Any,
    ) -> tuple[dict, ...] | None:
        """A 分支：完整祖先链 + extract_limits_from_context → 条件写回。"""
        from agent.security import requirement_limits

        requirement_id = str(getattr(requirement, "requirement_id", "") or "")
        version = int(getattr(requirement, "version", 1) or 1)
        try:
            context = await self._maybe_await(recovery(requirement_id))
        except Exception as exc:  # noqa: BLE001 - 上下文读取失败按恢复失败
            await self._mark_limits_unresolved(
                ctx, requirement, code="recovery_context_failed:" + type(exc).__name__, version=version
            )
            return None
        if context is None or not bool(getattr(context, "complete", False)):
            reason = str(getattr(context, "reason", "") or "上下文不完整") if context else "上下文不存在"
            logging.getLogger(__name__).warning(
                "历史要求限制恢复上下文不完整：%s（%s）", requirement_id, reason[:200]
            )
            self._append_history_notice(ctx, "历史用户限制上下文不完整，相关操作需要确认")
            return None
        chain_digest = str(getattr(context, "chain_digest", "") or "")
        prompt_version = self._limit_prompt_version(self.requirement_resolver, chain=True)
        try:
            limits = await self._maybe_await(
                extractor(context, requirement_id=requirement_id, version=version)
            )
        except Exception as exc:  # noqa: BLE001 - 失败标 unresolved，保留 known
            await self._mark_limits_unresolved(
                ctx,
                requirement,
                code="extract_limits_failed:" + type(exc).__name__,
                version=version,
                chain_digest=chain_digest,
            )
            return None
        # 恢复结果返回后重新读 requirement/context，发生变化就丢弃。
        fresh_requirement = None
        fresh_context = None
        try:
            fresh_requirement = self.requirements.by_id(requirement_id)
            fresh_context = await self._maybe_await(recovery(requirement_id))
        except Exception:  # noqa: BLE001 - 读不到就不写
            fresh_requirement = None
            fresh_context = None
        if not self._recovery_context_matches(
            context, fresh_context, fresh_requirement,
            requirement_id=requirement_id, version=version,
        ):
            logging.getLogger(__name__).warning(
                "历史要求限制恢复结果已过期（要求/版本/链已变化），丢弃不写：%s",
                requirement_id,
            )
            self._append_history_notice(ctx, "历史用户限制恢复结果已过期，相关操作需要确认")
            return None
        source = prompt_version + ";chain=" + chain_digest + ";version=" + str(version)
        try:
            written = await self._call_structured_setter(
                requirement_id,
                tuple(dict(item) for item in (limits or ()) if isinstance(item, dict)),
                source=source,
                state=requirement_limits.STATE_STRUCTURED,
                expected={
                    "expected_version": version,
                    "expected_status": "active",
                    "expected_chain_digest": chain_digest,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 版本变化/写失败：保留 known，不覆盖
            await self._mark_limits_unresolved(
                ctx,
                fresh_requirement or requirement,
                code="set_structured_limits_failed:" + type(exc).__name__,
                version=version,
                chain_digest=chain_digest,
            )
            return None
        reloaded = None
        try:
            reloaded = self.requirements.by_id(requirement_id)
        except Exception:  # noqa: BLE001
            reloaded = None
        recovered = getattr(reloaded, "structured_limits", None) if reloaded is not None else None
        if recovered is None:
            # 写后读不到明确限制：不能声称成功恢复；保持 unresolved。
            await self._mark_limits_unresolved(
                ctx,
                fresh_requirement or requirement,
                code="structured_limits_reload_missing",
                version=version,
                chain_digest=chain_digest,
            )
            return None
        return tuple(dict(item) for item in recovered if isinstance(item, dict))

    async def _recover_limits_legacy(
        self, ctx: Any, requirement: Any, *, resolver: Any
    ) -> tuple[dict, ...] | None:
        """兼容路径（A 未合入）：只喂当前要求原话，行为与 R9-H 相同。"""
        from agent.security import requirement_limits

        requirement_id = str(getattr(requirement, "requirement_id", "") or "")
        version = int(getattr(requirement, "version", 1) or 1)
        extractor = getattr(resolver, "extract_limits", None) if resolver is not None else None
        if not callable(extractor):
            await self._mark_limits_unresolved(
                ctx, requirement, code="extract_limits_unavailable:missing", version=version
            )
            return None
        prompt_version = self._limit_prompt_version(resolver, chain=False)
        try:
            limits = await self._maybe_await(
                extractor(
                    str(getattr(requirement, "text", "") or ""),
                    requirement_id=requirement_id,
                    version=version,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 失败标 unresolved，不当无限制
            await self._mark_limits_unresolved(
                ctx,
                requirement,
                code="extract_limits_failed:" + type(exc).__name__,
                version=version,
            )
            return None
        try:
            written = await self._call_structured_setter(
                requirement_id,
                tuple(dict(item) for item in (limits or ()) if isinstance(item, dict)),
                source=prompt_version,
                state=requirement_limits.STATE_STRUCTURED,
                expected={"expected_version": version, "expected_status": "active"},
            )
        except Exception as exc:  # noqa: BLE001 - 持久化失败保持原状态（不得当无限制）
            logging.getLogger(__name__).warning(
                "历史要求结构化限制持久化失败", exc_info=True
            )
            await self._mark_limits_unresolved(
                ctx,
                requirement,
                code="set_structured_limits_failed:" + type(exc).__name__,
                version=version,
            )
            return None
        reloaded = None
        try:
            reloaded = self.requirements.by_id(requirement_id)
        except Exception:  # noqa: BLE001
            reloaded = None
        recovered = getattr(reloaded, "structured_limits", None) if reloaded is not None else None
        if recovered is None:
            await self._mark_limits_unresolved(
                ctx, requirement, code="structured_limits_reload_missing", version=version
            )
            return None
        return tuple(dict(item) for item in recovered if isinstance(item, dict))

    # -- R9-H11：structured-but-unbound 的专用绑定恢复 ------------------

    @staticmethod
    def _binding_recovery_snapshot(requirement: Any, context: Any) -> dict:
        """恢复开始时的要求/链快照（写回前核对 id/version/status/chain_digest）。"""
        return {
            "requirement_id": str(getattr(requirement, "requirement_id", "") or ""),
            "version": int(getattr(requirement, "version", 0) or 0),
            "status": str(getattr(requirement, "status", "") or ""),
            "parent_id": (
                None
                if getattr(requirement, "parent_id", None) is None
                else str(getattr(requirement, "parent_id"))
            ),
            "text": str(getattr(requirement, "text", "") or ""),
            "chain_digest": str(getattr(context, "chain_digest", "") or ""),
        }

    @staticmethod
    def _binding_recovery_snapshot_matches(
        expected: dict, requirement: Any, context: Any
    ) -> bool:
        """恢复结果返回后重新读：id/版本/状态/父链/原话/链摘要任一变化即丢弃。"""
        if requirement is None or not isinstance(expected, dict):
            return False
        if str(getattr(requirement, "status", "")) != "active":
            return False
        if str(getattr(requirement, "requirement_id", "")) != str(
            expected.get("requirement_id") or ""
        ):
            return False
        if int(getattr(requirement, "version", 0) or 0) != int(
            expected.get("version") or 0
        ):
            return False
        if str(getattr(requirement, "parent_id", None) or "") != str(
            expected.get("parent_id") or ""
        ):
            return False
        if str(getattr(requirement, "text", "") or "") != str(
            expected.get("text") or ""
        ):
            return False
        expected_digest = str(expected.get("chain_digest") or "")
        fresh_digest = str(getattr(context, "chain_digest", "") or "")
        if expected_digest and (not fresh_digest or fresh_digest != expected_digest):
            return False
        return True

    async def _recover_structured_binding_limits(
        self, ctx: Any, requirement: Any
    ) -> tuple[dict, ...] | None:
        """R9-H12 §4：structured-but-unbound 的**专用绑定恢复**（保存候选优先）。

        * 只理解和绑定限制：只用已保存限制 + **保存时记录的候选事实**，不生成普通
          授权、不调用普通独立授权审查、不改权限模式；
        * 无保存候选时**绝不**用当前 PATH 查找结果直接绑定（同名程序可能已换对象），
          当前候选只作为审计事实返回/记录；
        * 采纳对象必须后端核验（路径存在 + 身份可读 + path/identity 一致）；
        * 写回前重新核对 requirement id/version/status/chain_digest，并重新核验所采纳
          的候选事实仍有效；
        * 恢复失败 / 等待期间要求被修改或撤回：一行都不写，原限制、known、候选与来源
          保持原样（不会清空成"无限制"）。
        """
        from agent.security import requirement_limits

        requirement_id = str(getattr(requirement, "requirement_id", "") or "")
        version = int(getattr(requirement, "version", 0) or 0)
        status = str(getattr(requirement, "status", "") or "")
        raw_limits = getattr(requirement, "structured_limits", None)
        if not requirement_id or version < 1 or not isinstance(raw_limits, (list, tuple)):
            return None
        known = tuple(dict(item) for item in raw_limits if isinstance(item, dict))
        if not requirement_limits.needs_binding_recovery(known):
            return None
        context = None
        recovery = getattr(self.requirements, "recovery_context", None)
        if callable(recovery):
            try:
                context = await self._maybe_await(recovery(requirement_id))
            except Exception:  # noqa: BLE001 - 链快照读不到：仍可用版本/状态条件写入
                context = None
        snapshot = self._binding_recovery_snapshot(requirement, context)
        # 当前 PATH 只用于记录审计事实；恢复采纳只认保存候选（后端重新核验）。
        current_candidates: tuple[dict, ...] = ()
        try:
            current_candidates = requirement_limits.current_binding_candidates(known)
        except Exception:  # noqa: BLE001 - 审计事实采集失败不阻塞恢复
            current_candidates = ()
        try:
            outcome = requirement_limits.recover_program_bindings(
                known,
                requirement_id=requirement_id,
                version=version,
                status=status,
                now_candidates=current_candidates,
            )
        except Exception as exc:  # noqa: BLE001 - 恢复失败：保留原始记录
            logger.warning(
                "程序绑定专用恢复失败（%s）：%s", requirement_id, type(exc).__name__
            )
            self._append_history_notice(
                ctx, "历史用户限制中的程序对象暂时无法可靠绑定，相关操作需要确认"
            )
            return None
        if not outcome.complete or not outcome.changed:
            logger.warning(
                "程序绑定专用恢复未完成（%s）：reason=%s 未解决=%s 审计候选=%s",
                requirement_id,
                outcome.reason or "no_adoption",
                [
                    {
                        "value": item.get("value"),
                        "reason": item.get("reason"),
                        "failure": item.get("failure"),
                        "saved": item.get("saved_candidates"),
                    }
                    for item in outcome.unresolved
                ],
                [
                    {
                        "name": item.get("name"),
                        "path": item.get("path"),
                        "failure": item.get("failure"),
                    }
                    for item in outcome.current_candidates
                ],
            )
            missing_history = any(
                str(item.get("reason") or "")
                == requirement_limits.RECOVERY_REASON_HISTORICAL_IDENTITY_MISSING
                for item in outcome.unresolved
            )
            if missing_history:
                # R9-H13 §3.A2：历史依据不足不是“自动已验证”，需要用户澄清限制所指对象。
                self._append_history_notice(
                    ctx,
                    "历史用户限制缺少保存时的对象身份依据，无法确认限制所指对象，"
                    "需要澄清限制对应的对象",
                )
            else:
                self._append_history_notice(
                    ctx, "历史用户限制中的程序对象无法可靠绑定，相关操作需要确认"
                )
            return None
        # 恢复期间重新读要求/链：被修改、撤回或链变化即丢弃结果，不写回旧结果。
        fresh_requirement = None
        fresh_context = None
        try:
            fresh_requirement = self.requirements.by_id(requirement_id)
            if callable(recovery):
                fresh_context = await self._maybe_await(recovery(requirement_id))
        except Exception:  # noqa: BLE001 - 读不到就不写
            fresh_requirement = None
            fresh_context = None
        if not self._binding_recovery_snapshot_matches(
            snapshot, fresh_requirement, fresh_context
        ):
            logger.warning(
                "程序绑定专用恢复结果已过期（要求/版本/链已变化），丢弃不写：%s",
                requirement_id,
            )
            self._append_history_notice(
                ctx, "历史用户限制恢复结果已过期，相关操作需要确认"
            )
            return None
        # 写回前重新核验所采纳的保存候选：path 未变、identity/birth 一致。
        try:
            invalid = requirement_limits.verify_recovered_bindings(outcome.limits)
        except Exception as exc:  # noqa: BLE001 - 核验异常=不能确认，失败关闭
            logger.warning(
                "程序绑定专用恢复采纳对象复核失败（%s）：%s",
                requirement_id,
                type(exc).__name__,
            )
            invalid = ({"failure": type(exc).__name__},)
        if invalid:
            logger.warning(
                "程序绑定专用恢复采纳对象已变化（%s）：%s",
                requirement_id,
                [str(item.get("failure")) for item in invalid],
            )
            self._append_history_notice(
                ctx, "历史用户限制恢复所采纳的程序对象已变化，相关操作需要确认"
            )
            return None
        source = requirement_limits.build_binding_recovery_source(
            requirement_text=snapshot["text"],
            chain_digest=snapshot["chain_digest"] or None,
            version=version,
            sources=outcome.sources or ("saved_candidate_verified",),
            origin=getattr(requirement, "structured_limits_source", "") or "",
        )
        try:
            writer = getattr(self.requirements, "apply_binding_recovery", None)
            if callable(writer):
                written = writer(
                    requirement_id,
                    outcome.limits,
                    source=source,
                    expected_version=version,
                    expected_status="active",
                    expected_chain_digest=snapshot["chain_digest"] or None,
                )
                written = await self._maybe_await(written)
            else:  # 兼容旧装配：条件写参数由 _call_structured_setter 探测
                written = await self._call_structured_setter(
                    requirement_id,
                    outcome.limits,
                    source=source,
                    state=requirement_limits.STATE_STRUCTURED,
                    expected={
                        "expected_version": version,
                        "expected_status": "active",
                        "expected_chain_digest": snapshot["chain_digest"] or None,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - 版本/链变化或写失败：保持原记录
            logger.warning(
                "程序绑定专用恢复写回失败（%s）：%s",
                requirement_id,
                type(exc).__name__,
            )
            self._append_history_notice(
                ctx, "历史用户限制恢复写回失败，相关操作需要确认"
            )
            return None
        reloaded = None
        try:
            reloaded = self.requirements.by_id(requirement_id)
        except Exception:  # noqa: BLE001
            reloaded = None
        stored = (
            getattr(reloaded, "structured_limits", None)
            if reloaded is not None
            else None
        )
        if stored is None or not requirement_limits.binding_recovery_complete(stored):
            logger.warning("程序绑定专用恢复写入后读回不一致：%s", requirement_id)
            self._append_history_notice(
                ctx, "历史用户限制恢复结果无法确认，相关操作需要确认"
            )
            return None
        return tuple(dict(item) for item in stored if isinstance(item, dict))

    async def _ensure_legacy_structured_limits(
        self,
        ctx: Any,
        binding: Any,
        *,
        message_id: str | None,
        turn_id: str,
    ) -> tuple[dict, ...] | None:
        """恢复入口：legacy/unresolved 重新理解；structured-but-unbound 专用绑定。

        * A 分支接口存在：RequirementLedger.recovery_context +
          ModelRequirementResolver.extract_limits_from_context，走祖先链；
        * 未合入：退回只喂当前原话的兼容路径（不阻塞、不把失败当无限制）；
        * state=structured 且 program_name 无 program_binding（R9-H11 问题一）：
          进入**专用绑定恢复**，只采纳保存候选（后端重新核验），无保存候选时保持
          未解决而不是按当前 PATH 转移禁止对象；不是"无关"；
        * 成功返回本轮应使用的结构化限制；失败返回 None（已有 known limits 保留）。
        """
        from agent.security import requirement_limits

        requirement = None
        try:
            requirement = self.requirements.by_id(str(getattr(binding, "requirement_id", "")))
        except Exception:  # noqa: BLE001 - 读不到就按无历史限制处理（引擎仍按 unresolved 把关）
            logging.getLogger(__name__).warning("历史要求读取失败", exc_info=True)
            return None
        if requirement is None:
            return None
        state = str(getattr(requirement, "structured_limits_state", None) or "")
        if state == requirement_limits.STATE_STRUCTURED:
            return await self._recover_structured_binding_limits(ctx, requirement)
        if state not in (
            requirement_limits.STATE_LEGACY,
            requirement_limits.STATE_UNRESOLVED,
        ):
            return None
        resolver = getattr(self, "requirement_resolver", None)
        recovery = getattr(self.requirements, "recovery_context", None)
        chain_extractor = (
            getattr(resolver, "extract_limits_from_context", None)
            if resolver is not None
            else None
        )
        if callable(recovery) and callable(chain_extractor):
            return await self._recover_limits_from_chain(
                ctx, requirement, recovery=recovery, extractor=chain_extractor
            )
        return await self._recover_limits_legacy(ctx, requirement, resolver=resolver)

    # -- fragment close: summary + entities + knowledge extraction --------

    async def _close_fragment(
        self,
        topic_id: str,
        adapter: BaseAdapter,
        tracer=None,
        *,
        usage_adapters: bool = False,
    ) -> Any | None:
        """封块 + 立刻做完三种派生。

        `usage_adapters=False`（默认）是**兼容入口**：调用方已经给定 adapter
        （既有测试 / 离线维护），三种派生共用它，行为与基线完全一致。
        `usage_adapters=True` 用于运行时的通知轮：三种派生各自走自己的用途解析，
        与主对话解耦（用户改了用途分配就必须真的换）。
        """
        adapters = await self.build_derived_adapters() if usage_adapters else None
        return await self.memory_lifecycle.close_fragment(
            topic_id, adapter, tracer=tracer, adapters=adapters
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

    async def consolidate(
        self, topic_id: str, adapter: BaseAdapter, tracer=None
    ) -> bool:
        """委派给 MemoryLifecycle（tracer 只用于把派生修正/失败写进 trace）。"""
        return await self.memory_lifecycle.consolidate(topic_id, adapter, tracer=tracer)

    async def consolidate_for_usage(self, topic_id: str, tracer=None) -> bool:
        """预算压缩整理：走 `memory-organize` 用途。

        不可用（显式指定坏了 / 没有可用凭据）时**如实跳过**：这不是对话主路径，
        不能让整轮失败，也绝不能改用主对话模型偷偷跑完（那会绕过用户的用途分配）。
        返回是否真的做了一次整理。
        """
        try:
            target = self.resolve_usage("memory-organize")
        except UsageUnavailable as exc:
            self._record_usage_unavailable(exc)
            return False
        self._record_usage(target)
        adapter = await self.build_adapter_for_target(target)
        if adapter is None:
            self._record_usage_unavailable(
                UsageUnavailable(
                    "memory-organize",
                    REASON_ADAPTER_UNAVAILABLE,
                    f"记忆整理用途解析到 {target.credential_id}，但建不出可用适配器",
                    credential_id=target.credential_id,
                    model_id=target.model_id,
                )
            )
            return False
        return await self.memory_lifecycle.consolidate(topic_id, adapter, tracer=tracer)

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
            "SELECT id, role, content, content_type, created_at, raw, turn_id FROM messages "
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
        from agent.storage.tool_records import previews_for_turns

        # 工具记录按 turn_id 归属：工具换话题时整轮会跟着走，用话题过滤在那个瞬间不可靠；
        # 按时间窗口切又会把分页边界上的调用切丢。预览随消息一起回来，全文另有接口。
        turn_ids = sorted({str(m.get("turn_id") or "") for m in page} - {""})
        return {
            "messages": page,
            "tool_records": previews_for_turns(self.conn, turn_ids),
            "has_more": has_more,
            "next_before": next_before,
        }

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
