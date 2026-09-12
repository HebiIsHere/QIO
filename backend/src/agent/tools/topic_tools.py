"""Topic management tools: switch / create (execution side of topic prediction).

Judgement comes from the embedding predictor; the main model confirms via
these tools. Switching updates the anchor (old topic -> history) and
accumulates a related edge between the old and new topic.
"""

from __future__ import annotations

import sqlite3

from agent.graph.anchors import AnchorService
from agent.graph.edges import EdgeService
from agent.graph.nodes import NodeService
from agent.prompts import TOOL_CREATE_TOPIC_DESC, TOOL_SWITCH_TOPIC_DESC
from agent.tools.base import Tool, ToolResult


def _relate(conn: sqlite3.Connection, a: str, b: str) -> None:
    """Related edge is undirected: normalize direction so weights accumulate."""
    if a == b:
        return
    src, dst = sorted([a, b])
    EdgeService(conn).add(src, dst, "related")


class SwitchTopicTool(Tool):
    name = "switch_topic"
    description = TOOL_SWITCH_TOPIC_DESC
    parameters = {
        "type": "object",
        "properties": {
            "topic_id": {"type": "string", "description": "目标话题 id"},
            "reason": {"type": "string", "description": "切换原因（可选）"},
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
        anchors = AnchorService(self.conn)
        old = anchors.get_active()
        # 切回已有话题时恢复它保存的位置（不覆盖用户明确选择：用户选择本身
        # 就是 active，因此在同一话题内它永远优先）
        restored = anchors.restore_position(topic_id)
        if old is not None and old.topic_id != topic_id:
            _relate(self.conn, old.topic_id, topic_id)
        if restored.fragment_id:
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
    description = TOOL_CREATE_TOPIC_DESC
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "新话题名称，简短明确"},
            "reason": {"type": "string", "description": "创建原因（可选）"},
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
        self._blocked: set[str] = set()  # 被去重拦截过的话题名

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

    async def run(self, **kwargs) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="name 必填")

        # 二次申请（已被去重拦截过）：跳过重复检查，直接人工审批
        if name in self._blocked:
            if self.approvals is None:
                return ToolResult(ok=False, error="重复话题需人工审批，但审批服务不可用")
            result = await self.approvals.request("create_topic", {"name": name})
            if result.decision != "approved":
                return ToolResult(ok=False, content="话题创建未获批准，未创建")
            self._blocked.discard(name)
            return self._create(name)

        # 首次申请：去重硬限制
        dup = self._duplicate(name)
        if dup is not None:
            dup_id, score, _why = dup
            self._blocked.add(name)
            from agent.graph.topics import TopicService

            fp = TopicService(self.conn).fingerprint(dup_id)
            return ToolResult(
                ok=False,
                content=(
                    f"检测到相似话题「{fp.title}」（{dup_id}，摘要：{fp.summary_preview or ''}），"
                    f"相似度 {score:.2f}。可调用 memory_search 查阅确认是否重复；"
                    f"如确属不同话题，请再次调用 create_topic（将进入人工审批）。"
                ),
            )
        return self._create(name)

    def _create(self, name: str) -> ToolResult:
        nodes = NodeService(self.conn)
        node = nodes.create_topic(name)
        anchors = AnchorService(self.conn)
        old = anchors.get_active()
        anchors.set_active(node.id)
        if old is not None and old.topic_id != node.id:
            _relate(self.conn, old.topic_id, node.id)
        return ToolResult(ok=True, content=f"已创建并切换到话题「{name}」（{node.id}）")
