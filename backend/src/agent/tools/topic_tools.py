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
_INTENT_SCHEMA: dict = {
    "type": "string",
    "enum": ["user_request", "suggestion"],
    "description": (
        "这次话题操作的来源（受控语义通道，只能是本轮真实 tool_call 里声明的值）："
        "user_request = 用户本轮明确要求现在执行这次具体变更（校验后直接执行）；"
        "suggestion = QIO 主动建议（缺省 / 未知值都按它处理，需用户确认后才执行）。"
    ),
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
        if result.fragment_id:
            return ToolResult(
                ok=True,
                content=(
                    f"已切换到话题「{node.name}」（{topic_id}）；"
                    "已回到该话题上次讨论的位置，下一轮会带上那段历史。"
                ),
            )
        return ToolResult(ok=True, content=f"已切换到话题「{node.name}」（{topic_id}）")

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
        # 冲突身份 = (operation_id, param_version)；没有操作绑定时退回空操作 + 规范化名称
        # （旧路径兼容），**不再**只按名称建批准 key。
        self._conflicted: dict[tuple[str, int], str] = {}
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
        """冲突身份：优先稳定操作身份；没有绑定才退回规范化名称（旧路径）。"""
        if operation is not None:
            return (str(operation.operation_id), int(operation.param_version))
        # 旧路径（工具被直接调用、没有操作绑定）：只能退回规范化名称作为冲突身份；
        # 有操作绑定时永远用 (operation_id, param_version)，不按名称建批准 key。
        return ("name:" + normalize_topic_name(name), 0)

    def _conflict_payload(self, name: str, kwargs: dict) -> dict:
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
        return payload

    async def run(self, **kwargs) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="name 必填")
        operation = current_operation()
        identity = self._conflict_identity(operation, name)
        expected_nav_version, turn_id = _operation_nav_binding()

        # 同操作同参数版本的合法重试（含用户改名后最终确认的版本）：进入二次审批。
        if identity in self._conflicted:
            return await self._resolve_conflict(name, identity, kwargs)

        # 首次申请：去重硬限制
        dup = self._duplicate(name)
        if dup is not None:
            dup_id, score, _why = dup
            self._conflicted[identity] = name
            from agent.graph.topics import TopicService

            fp = TopicService(self.conn).fingerprint(dup_id)
            return ToolResult(
                ok=False,
                content=(
                    f"检测到相似话题「{fp.title}」（{dup_id}，摘要：{fp.summary_preview or ''}），"
                    f"相似度 {score:.2f}。可调用 memory_search 查阅确认是否重复；"
                    f"如确属不同话题，请再次调用 create_topic（将进入人工审批）。"
                ),
                error="similar topic exists",
                facts={
                    "topic_application": "conflict",
                    "conflict": "conflict_pending",
                    "similar_topic_id": dup_id,
                    "score": score,
                    "operation_id": (
                        str(operation.operation_id) if operation is not None else None
                    ),
                    "param_version": (
                        int(operation.param_version) if operation is not None else None
                    ),
                },
                code="topic_create_conflict",
            )
        return await self._create(name, expected_nav_version, turn_id)

    async def _resolve_conflict(
        self, name: str, identity: tuple[str, int], kwargs: dict
    ) -> ToolResult:
        """用户处理查重冲突：同一操作同一参数版本走工具内部的二次人工审批。"""
        if identity in self._declined:
            return ToolResult(
                ok=False,
                content=(
                    "用户没有确认这次查重冲突，话题没有创建。"
                    "请直接在当前话题回答用户原来的问题，不要重复申请。"
                ),
                error="topic creation declined",
                recoverable=False,
                facts={
                    "topic_application": "conflict",
                    "conflict": "conflict_declined",
                },
                code="topic_create_conflict_declined",
            )
        if self.approvals is None:
            return ToolResult(ok=False, error="重复话题需人工审批，但审批服务不可用")
        payload = self._conflict_payload(name, kwargs)
        try:
            result = await self.approvals.request("create_topic", payload)
        except Exception as exc:  # noqa: BLE001 - 审批失败如实失败，不假装已创建
            return ToolResult(
                ok=False,
                error="查重冲突的二次审批失败：" + str(exc)[:160],
                recoverable=False,
            )
        decision = str(getattr(result, "decision", "") or "")
        if decision != "approved":
            self._declined.add(identity)
            return ToolResult(
                ok=False,
                content=(
                    "用户没有确认这次查重冲突，话题没有创建。"
                    "请直接在当前话题回答用户原来的问题，不要重复申请。"
                ),
                error="topic creation declined",
                recoverable=False,
                facts={
                    "topic_application": "conflict",
                    "conflict": "conflict_declined",
                },
                code="topic_create_conflict_declined",
            )
        self._conflicted.pop(identity, None)
        self._declined.discard(identity)
        expected_nav_version, turn_id = _operation_nav_binding()
        return await self._create(name, expected_nav_version, turn_id)

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
        return ToolResult(ok=True, content=f"已创建并切换到话题「{name}」（{result.topic_id}）")
