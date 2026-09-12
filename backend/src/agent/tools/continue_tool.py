"""continue_from_fragment：Agent 显式把讨论位置移到某个历史片段。

与 switch_topic / create_topic 同级：这是「改变历史位置」的显式动作，
不是检索。memory_search 只读，永远不会改 anchor（见 services/injection.py
与 graph/anchors.py 的语义说明）。

为什么要有它：Agent 需要和用户一样的能力 —— 找到历史 → 判断具体片段 →
明确选择「从这里继续」。仅靠 memory_search 无法表达「接下来持续讨论的位置」。
"""

from __future__ import annotations

import sqlite3

from agent.graph.anchors import AnchorService
from agent.graph.nodes import NodeService
from agent.memory.fragment import FragmentManager
from agent.prompts import TOOL_CONTINUE_FROM_FRAGMENT_DESC
from agent.tools.base import Tool, ToolResult
from agent.tools.topic_tools import _relate


class ContinueFromFragmentTool(Tool):
    name = "continue_from_fragment"
    description = TOOL_CONTINUE_FROM_FRAGMENT_DESC
    parameters = {
        "type": "object",
        "properties": {
            "fragment_id": {
                "type": "string",
                "description": "要接着往下聊的历史片段 id（用 memory_search 得到）",
            },
            "reason": {"type": "string", "description": "为什么从这里继续（可选）"},
        },
        "required": ["fragment_id"],
    }
    # 写状态（anchor）：不要与其他工具并行，避免与其它写动作交叉
    is_concurrency_safe = False

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def run_sync(self, **kwargs) -> ToolResult:
        fragment_id = str(kwargs.get("fragment_id") or "").strip()
        if not fragment_id:
            return ToolResult(ok=False, error="fragment_id 必填（用 memory_search 取得片段 id）")

        fragments = FragmentManager(self.conn)
        fragment = fragments.get(fragment_id)
        if fragment is None:
            return ToolResult(
                ok=False,
                error=f"片段不存在：{fragment_id}。请先用 memory_search 确认要接续的片段。",
            )
        node = NodeService(self.conn).get_topic(fragment.topic_id)
        if node is None:
            return ToolResult(ok=False, error=f"片段所属话题不存在：{fragment.topic_id}")
        # 数据完整性：既没有摘要也没有消息的片段不可用（损坏/被清理）
        if not (fragment.summary or "").strip() and not fragments.messages(fragment_id):
            return ToolResult(ok=False, error=f"片段内容不可用（无摘要也无消息）：{fragment_id}")

        anchors = AnchorService(self.conn)
        old = anchors.get_active()
        anchors.set_active(fragment.topic_id, fragment_id)
        if old is not None and old.topic_id and old.topic_id != fragment.topic_id:
            _relate(self.conn, old.topic_id, fragment.topic_id)

        return ToolResult(
            ok=True,
            content=(
                f"已把讨论位置移到历史片段「{self._title(fragment_id, fragment.summary)}」"
                f"（fragment_id={fragment_id}，话题「{node.name}」）。"
                "本轮上下文不会重建；从下一轮开始会带上这段历史。"
                "如果本轮还需要那段讨论的细节，用 memory_search 继续查。"
            ),
        )

    def _title(self, fragment_id: str, summary: str | None) -> str:
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
        if row is not None and row["title"]:
            return str(row["title"])
        if summary:
            return summary.strip().splitlines()[0][:40]
        return fragment_id

    async def run(self, **kwargs) -> ToolResult:
        return self.run_sync(**kwargs)
