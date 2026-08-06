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
from agent.tools.base import Tool, ToolResult


def _relate(conn: sqlite3.Connection, a: str, b: str) -> None:
    """Related edge is undirected: normalize direction so weights accumulate."""
    if a == b:
        return
    src, dst = sorted([a, b])
    EdgeService(conn).add(src, dst, "related")


class SwitchTopicTool(Tool):
    name = "switch_topic"
    description = (
        "把当前对话的主话题切换到指定话题。topic_id 必须是已存在的话题。"
        "切换后后续对话将记录到新话题，并在新旧话题间建立相关关系。"
    )
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
        anchors.set_active(topic_id)
        if old is not None and old.topic_id != topic_id:
            _relate(self.conn, old.topic_id, topic_id)
        return ToolResult(ok=True, content=f"已切换到话题「{node.name}」（{topic_id}）")

    async def run(self, **kwargs) -> ToolResult:
        return self.run_sync(**kwargs)


class CreateTopicTool(Tool):
    name = "create_topic"
    description = (
        "创建新话题并切换为当前主话题。适用于当前消息与所有现有话题都不匹配时。"
        "新话题与当前话题自动建立相关关系。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "新话题名称，简短明确"},
            "reason": {"type": "string", "description": "创建原因（可选）"},
        },
        "required": ["name"],
    }

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def run_sync(self, **kwargs) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="name 必填")
        nodes = NodeService(self.conn)
        node = nodes.create_topic(name)
        anchors = AnchorService(self.conn)
        old = anchors.get_active()
        anchors.set_active(node.id)
        if old is not None and old.topic_id != node.id:
            _relate(self.conn, old.topic_id, node.id)
        return ToolResult(ok=True, content=f"已创建并切换到话题「{name}」（{node.id}）")

    async def run(self, **kwargs) -> ToolResult:
        return self.run_sync(**kwargs)
