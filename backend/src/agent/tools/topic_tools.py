"""Topic management tools: switch / create (execution side of topic prediction).

Judgement comes from the main model; these tools stay the **only** executors of
topic navigation. 话题申请流程（services/topic_application.py）会在
ToolRegistry.execute 之前拦截同名工具：用户确认之后才以（可能被 edits
校正过的）参数走到这里。因此：

* 本模块保持既有执行语义（进入话题 / 创建话题 + 登记工具导航归属）；
* P1-04：工具的真实导航写入带**起点快照 CAS** —— 申请绑定的 base_nav_version
  与实际写入时的导航版本不一致就抛 NavigationVersionConflict，整个写事务回滚，
  Anchor 与话题都不会被改（不覆盖用户后来的手动选择）；
* P1-05：create 的查重与相似冲突二次审批保留，但**冲突身份**从「名称集合」改为
  (operation_id, param_version)：同操作同参数版本的合法重试会再次进入工具内部
  的 create_topic 二次审批（用户最终确认的名称就是实际创建的名称），不同参数
  版本不会继承旧确认；
* 真正的归属搬迁与上下文重建不在这里做（由 orchestrator 注入 bridge 的闭包
  负责，避免出现第二套切换执行器）。
"""

from __future__ import annotations

import asyncio
import sqlite3

from agent.graph.nodes import NodeService
from agent.prompts import TOOL_DESCRIPTIONS
from agent.services.navigation import (
    NavigationVersionConflict,
    TopicNavigationService,
    note_tool_navigation,
)
from agent.services.topic_operation import (
    INTENT_PARAM,
    current_operation,
    normalize_topic_name,
)
from agent.tools.base import Tool, ToolResult

#: intent 只允许出现在两个话题工具的 schema 里（契约 P1-01 §2.4）。
#: schema 只留真实类型与约束：语义说明唯一来源是 prompts.py 的 TOOL_DESCRIPTIONS
#: （单源约束，见 tests/test_p1_lead_continue_context.py）。
_INTENT_SCHEMA: dict = {
    "type": "string",
    "enum": ["user_request", "suggestion"],
}


def _nav_conflict_result(exc: NavigationVersionConflict) -> ToolResult:
    """工具真实失败：起点版本已变，本次导航写入整体回滚（一个字段都没写）。"""
    facts = exc.as_dict()
    facts["topic_application"] = "stale"
    return ToolResult(
        ok=False,
        content=(
            "导航位置已经改变（申请绑定的版本与实际位置不一致），这次话题操作没有执行，"
            "也没有改动当前对话位置。请直接在当前话题回答用户原来的问题，不要重复调用话题工具。"
        ),
        error=str(exc),
        category="unknown",
        recoverable=False,
        facts=facts,
        code="nav_version_conflict",
    )


def _operation_nav_binding() -> tuple[int | None, str | None]:
    """本次工具执行绑定的申请起点：(base_nav_version, turn_id)。

    没有操作绑定（子任务 / 直接调用 / 旧路径）时两者都是 None：保持既有
    「无约束推进」语义，不因缺少申请而拒绝执行。
    """
    operation = current_operation()
    if operation is None:
        return None, None
    return operation.base_nav_version, str(operation.turn_id or "") or None


class SwitchTopicTool(Tool):
    name = "switch_topic"
    description = TOOL_DESCRIPTIONS[name]
    parameters = {
        "type": "object",
        "properties": {
            "topic_id": {"type": "string"},
            "reason": {"type": "string"},
            INTENT_PARAM: _INTENT_SCHEMA,
        },
        "required": ["topic_id"],
    }

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def run_sync(self, **kwargs) -> ToolResult:
        topic_id = (kwargs.get("topic_id") or "").strip()
        if not topic_id:
            return ToolResult(ok=False, error="topic_id 必填")
        nodes = NodeService(self.conn)
        node = nodes.get_topic(topic_id)
        if node is None:
            return ToolResult(ok=False, error=f"话题不存在: {topic_id}")
        expected_nav_version, turn_id = _operation_nav_binding()
        try:
            # 切回已有话题时恢复它保存的位置（不覆盖用户明确选择：用户选择本身
            # 就是 active，因此在同一话题内它永远优先）。写 anchor 只走 Navigator，
            # 且 Anchor 写入与导航版本 CAS 在同一个事务里（P1-04）。
            result = TopicNavigationService(self.conn).enter_topic(
                topic_id,
                relate=True,
                expected_nav_version=expected_nav_version,
                turn_id=turn_id,
            )
        except NavigationVersionConflict as exc:
            return _nav_conflict_result(exc)
        # 本轮是模型自己换的话题：整轮归属跟着走（用户导航不会走到这里）。
        note_tool_navigation(topic_id)
        # 权威导航结果（F05/F12）：真实目标 + 写入后的导航版本。只报真实值，不猜。
        facts = {
            "topic_id": result.topic_id,
            "fragment_id": result.fragment_id,
            "nav_version": result.nav_version,
        }
        if result.fragment_id:
            return ToolResult(
                ok=True,
                content=(
                    f"已切换到话题「{node.name}」（{topic_id}）；"
                    "已回到该话题上次讨论的位置，下一轮会带上那段历史。"
                ),
                facts=facts,
            )
        return ToolResult(
            ok=True,
            content=f"已切换到话题「{node.name}」（{topic_id}）",
            facts=facts,
        )

    async def run(self, **kwargs) -> ToolResult:
        return self.run_sync(**kwargs)


class CreateTopicTool(Tool):
    name = "create_topic"
    description = TOOL_DESCRIPTIONS[name]
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "reason": {"type": "string"},
            INTENT_PARAM: _INTENT_SCHEMA,
        },
        "required": ["name"],
    }

    # 名称相似硬限制阈值；embedding 模糊带 [EMBED_LO, EMBED_HI)
    NAME_SIM_THRESHOLD = 0.8
    EMBED_LO = 0.5
    EMBED_HI = 0.7

    def __init__(self, conn: sqlite3.Connection, approvals=None, predictor=None) -> None:
        self.conn = conn
        self.approvals = approvals  # ApprovalService（二次申请人工审批）
        self.predictor = predictor  # TopicPredictor（embedding 模糊带判定）
        # 冲突身份 = (operation_id, param_version)；没有操作绑定时退回空操作 + 精确名称
        # （旧路径兼容），**不再**只按名称建批准 key，也**不再**用查重归一化当身份
        # （F07：宽松归一化只作证据）。
        self._conflicted: dict[tuple[str, int], dict] = {}
        # 用户拒绝过这次查重冲突的版本：同版本再重试不再重复弹窗。
        self._declined: set[tuple[str, int]] = set()

    @staticmethod
    def _name_jaccard(a: str, b: str) -> float:
        sa, sb = set(a), set(b)
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def _duplicate(self, name: str) -> tuple[str, float, str] | None:
        """返回 (similar_topic_id, score, reason) 或 None。"""
        from agent.graph.topics import TopicService

        topics = TopicService(self.conn)
        # 1) 名称相似（硬）
        for fp in topics.list_with_fingerprints():
            sim = self._name_jaccard(name, fp.title)
            if sim >= self.NAME_SIM_THRESHOLD:
                return (fp.topic_id, sim, "name")
        # 2) embedding 模糊带（硬）：新话题名与最相似现有话题 ∈ [0.5, 0.7)
        if self.predictor is not None:
            pred = self.predictor.predict(name, current_topic_id=None)
            scores = dict(getattr(pred, "scores", None) or {})
            if scores:
                top_id = max(scores, key=scores.get)
                top = scores[top_id]
                if self.EMBED_LO <= top < self.EMBED_HI:
                    return (top_id, top, "embedding")
        return None

    @staticmethod
    def _conflict_identity(operation, name: str) -> tuple[str, int]:
        """冲突身份：优先稳定操作身份；没有绑定才退回**精确名称**（旧路径）。

        有操作绑定时永远用 (operation_id, param_version)。旧路径没有操作身份，
        只能用精确名称兜底；**不用** normalize_topic_name —— 那是查重证据口径，
        不是批准身份（F07）。
        """
        if operation is not None:
            return (str(operation.operation_id), int(operation.param_version))
        return ("name:" + name, 0)

    def _conflict_payload(self, name: str, kwargs: dict, dup: dict | None = None) -> dict:
        operation = current_operation()
        payload: dict = {
            "name": name,
            "reason": str(kwargs.get("reason") or ""),
            "source": "model",
        }
        if operation is not None:
            payload["operation_id"] = str(operation.operation_id)
            payload["param_version"] = int(operation.param_version)
            payload["conflict"] = "conflict_pending"
            payload["target_name"] = name
        if dup:
            payload["similar_topic_id"] = dup.get("similar_topic_id")
            # 展示事实：前端查重冲突区块显示「已有相似话题「X」」；不参与任何身份判断。
            payload["similar_topic_name"] = dup.get("similar_topic_name")
            payload["score"] = dup.get("score")
        return payload

    def _conflict_facts(
        self, dup: dict | None, *, conflict: str
    ) -> dict:
        """结构化冲突事实（含**具体对象**：新名称 + 相似话题 id），不按文本猜。"""
        operation = current_operation()
        facts: dict = {
            "topic_application": (
                "stale" if conflict == "conflict_stale" else "conflict"
            ),
            "conflict": conflict,
        }
        if dup:
            facts["similar_topic_id"] = dup.get("similar_topic_id")
            facts["similar_topic_name"] = dup.get("similar_topic_name")
            facts["score"] = dup.get("score")
            facts["target_name"] = dup.get("name")
            # 查重归一化只作证据（F07），不参与身份。
            facts["dedup_name"] = normalize_topic_name(dup.get("name"))
        if operation is not None:
            facts["operation_id"] = str(operation.operation_id)
            facts["param_version"] = int(operation.param_version)
            facts["attempt"] = getattr(
                getattr(operation, "attempt", None), "value", None
            )
        return facts

    def _declined_result(self, dup: dict | None) -> ToolResult:
        """用户明确拒绝这次具体查重冲突：同一版本不再重复弹窗（F10）。"""
        return ToolResult(
            ok=False,
            content=(
                "用户没有确认这次查重冲突，话题没有创建。"
                "请直接在当前话题回答用户原来的问题，不要重复申请。"
            ),
            error="topic creation declined",
            recoverable=False,
            facts=self._conflict_facts(dup, conflict="conflict_declined"),
            code="topic_create_conflict_declined",
        )

    def _resolve_conflict_stale_result(self, dup: dict | None) -> ToolResult:
        """冲突确认期间申请失效：失效 ≠ 拒绝（不写拒绝证据、不重弹）。"""
        return ToolResult(
            ok=False,
            content=(
                "这次查重冲突确认已失效（等待期间话题位置已改变），话题没有创建。"
                "请直接在当前话题回答用户原来的问题，不要重复申请。"
            ),
            error="topic conflict application stale",
            recoverable=False,
            facts=self._conflict_facts(dup, conflict="conflict_stale"),
            code="topic_create_conflict_stale",
        )

    def _resolve_conflict_timeout_result(self, dup: dict | None) -> ToolResult:
        """冲突确认超时：不是主动拒绝，同一版本可再次进入具体冲突确认（F02/F10）。"""
        return ToolResult(
            ok=False,
            content=(
                "用户还没有处理这次查重冲突确认（等待超时），这不是拒绝，话题没有创建。"
                "如确属不同话题，可以再次调用 create_topic 重新发起确认；"
                "否则请直接在当前话题回答用户原来的问题。"
            ),
            error="topic conflict confirmation timed out",
            recoverable=True,
            facts=self._conflict_facts(dup, conflict="conflict_pending"),
            code="topic_create_conflict_timeout",
        )

    def _resolve_conflict_failed_result(
        self, dup: dict | None, detail: str
    ) -> ToolResult:
        """冲突确认的技术错误：与「用户拒绝」分开表示，如实说明可再试。"""
        return ToolResult(
            ok=False,
            content=(
                "查重冲突的二次审批没有完成（技术错误：" + detail[:120] + "），"
                "这次话题没有创建，也没有被当成用户拒绝。"
            ),
            error="topic conflict confirmation failed: " + detail[:160],
            recoverable=True,
            facts=self._conflict_facts(dup, conflict="conflict_pending"),
            code="topic_create_conflict_failed",
        )

    async def run(self, **kwargs) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="name 必填")
        operation = current_operation()
        identity = self._conflict_identity(operation, name)
        expected_nav_version, turn_id = _operation_nav_binding()

        # 同操作同参数版本的合法重试（含用户改名后最终确认的版本）：进入二次审批。
        if identity in self._conflicted:
            return await self._resolve_conflict(
                name, identity, kwargs, self._conflicted.get(identity)
            )

        # 首次申请：去重硬限制（查重归一化只是证据，不改变执行名称）
        dup = self._duplicate(name)
        if dup is not None:
            dup_id, score, _why = dup
            from agent.graph.topics import TopicService

            fp = TopicService(self.conn).fingerprint(dup_id)
            record = {
                "name": name,
                "similar_topic_id": dup_id,
                "similar_topic_name": (getattr(fp, "title", None) or ""),
                "score": score,
                "dedup_name": normalize_topic_name(name),
            }
            self._conflicted[identity] = record
            return ToolResult(
                ok=False,
                content=(
                    f"检测到相似话题「{fp.title}」（{dup_id}，摘要：{fp.summary_preview or ''}），"
                    f"相似度 {score:.2f}。可调用 memory_search 查阅确认是否重复；"
                    f"如确属不同话题，请再次调用 create_topic（将进入人工审批）。"
                ),
                error="similar topic exists",
                facts=self._conflict_facts(record, conflict="conflict_pending"),
                code="topic_create_conflict",
            )
        return await self._create(name, expected_nav_version, turn_id)

    async def _resolve_conflict(
        self,
        name: str,
        identity: tuple[str, int],
        kwargs: dict,
        dup: dict | None = None,
    ) -> ToolResult:
        """用户处理查重冲突：同一操作同一参数版本走工具内部的二次人工审批。

        F10：拒绝 / 超时 / 失效 / 技术错误**分别表示**，不把任何一种默认当成
        用户拒绝；超时与可重试技术错误允许同版本再次进入本确认（F02）。
        """
        if identity in self._declined:
            return self._declined_result(dup)
        if self.approvals is None:
            return ToolResult(
                ok=False,
                error="重复话题需人工审批，但审批服务不可用",
                recoverable=True,
                facts=self._conflict_facts(dup, conflict="conflict_pending"),
                code="topic_create_conflict_failed",
            )
        payload = self._conflict_payload(name, kwargs, dup)
        try:
            result = await self.approvals.request("create_topic", payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 审批失败如实失败，不假装已创建
            # 技术错误 ≠ 用户拒绝：不写拒绝证据、不阻断同版本重试（F10）。
            from agent.trace.redact import sanitize_error_text

            return self._resolve_conflict_failed_result(
                dup, sanitize_error_text(str(exc))
            )
        decision = str(getattr(result, "decision", "") or "")
        if decision == "approved":
            self._conflicted.pop(identity, None)
            self._declined.discard(identity)
            expected_nav_version, turn_id = _operation_nav_binding()
            return await self._create(name, expected_nav_version, turn_id)
        if decision == "stale":
            # 失效 ≠ 拒绝：不加入 _declined，不写拒绝证据。
            return self._resolve_conflict_stale_result(dup)
        if decision == "timeout":
            # 超时 ≠ 主动拒绝：不加入 _declined，同版本可再次进入确认。
            return self._resolve_conflict_timeout_result(dup)
        if decision == "cancelled":
            return ToolResult(
                ok=False,
                content="本轮已停止，查重冲突确认没有执行。",
                error="turn cancelled",
                category="cancelled",
                status="cancelled",
                facts=self._conflict_facts(dup, conflict="conflict_pending"),
                code="topic_create_conflict_cancelled",
            )
        # rejected / 其它显式否定：这是**具体对象**的拒绝，同版本不再重复弹。
        self._declined.add(identity)
        return self._declined_result(dup)

    async def _create(
        self,
        name: str,
        expected_nav_version: int | None,
        turn_id: str | None,
    ) -> ToolResult:
        try:
            result = TopicNavigationService(self.conn).create_topic(
                name,
                expected_nav_version=expected_nav_version,
                turn_id=turn_id,
            )
        except NavigationVersionConflict as exc:
            # 冲突版本的创建不执行：话题与 Anchor 都在同一个事务里回滚了。
            return _nav_conflict_result(exc)
        # 新话题此刻还没有片段，由本轮收尾时把整轮消息搬进它的开放片段。
        note_tool_navigation(result.topic_id)
        return ToolResult(
            ok=True,
            content=f"已创建并切换到话题「{name}」（{result.topic_id}）",
            facts={
                "topic_id": result.topic_id,
                "fragment_id": None,
                "nav_version": result.nav_version,
                "dedup_name": normalize_topic_name(name),
            },
        )
