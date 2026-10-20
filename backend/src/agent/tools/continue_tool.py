"""continue_from_fragment：Agent 显式从某段历史继续。

与 switch_topic / create_topic 同级：这是“改变历史位置”的显式动作，
不是检索。memory_search 只读，永远不会改 anchor（见 services/injection.py
与 graph/anchors.py 的语义说明）。

为什么要有它：Agent 需要和用户一样的能力 —— 找到历史 → 判断具体片段 →
明确选择“从这里继续”。仅靠 memory_search 无法表达“接下来持续讨论的位置”。

第二阶段语义（与用户在星球里点“从这里继续”完全一致）：
历史片段保持不变，**新建一个接续片段**并记下来源，Anchor 落在新片段上；
下一轮的 Focus 读的是来源片段（接续片段本身是空的）。
写 anchor 只走 TopicNavigationService。

Phase 1 新增：登记成功的**同一轮**就把该片段的相关上下文（摘要 + 头尾原文）
作为这次工具结果的真实内容交还给模型 —— 这一轮立即可用，不必等到下一轮；
但不改变本轮归属，也不改写任何已经排队的消息。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent.graph.nodes import NodeService
from agent.memory.fragment import FragmentManager
from agent.prompts import TOOL_DESCRIPTIONS, context_heading
from agent.services.navigation import (
    FragmentNotInTopic,
    TopicNavigationService,
    TopicNotFound,
)
from agent.services.params import FOCUS
from agent.tools.base import Tool, ToolResult


class ContinueFromFragmentTool(Tool):
    name = "continue_from_fragment"
    description = TOOL_DESCRIPTIONS[name]
    parameters = {
        "type": "object",
        "properties": {
            "fragment_id": {"type": "string"},
            "reason": {"type": "string"},
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
        messages = fragments.messages(fragment_id)
        if not (fragment.summary or "").strip() and not messages:
            return ToolResult(ok=False, error=f"片段内容不可用（无摘要也无消息）：{fragment_id}")

        try:
            # 阶段 1：Agent 在执行中调用接续工具时**只登记意图**，不改变当前轮绑定，
            # 也不改写已经提交的排队消息。接续片段在这之后提交的消息真正执行时才落实。
            result = TopicNavigationService(self.conn).register_continuation(
                fragment.topic_id, fragment_id
            )
        except TopicNotFound as exc:
            return ToolResult(ok=False, error=str(exc))
        except FragmentNotInTopic as exc:
            return ToolResult(ok=False, error=str(exc))

        title = self._title(fragment_id, fragment.summary)
        context = self._focus_context(
            fragment_id=fragment_id,
            topic_id=fragment.topic_id,
            topic_name=node.name,
            title=title,
            summary=fragment.summary or "",
            messages=messages,
        )
        context_text = context_heading("history_focus") + "\n" + json.dumps(
            context, ensure_ascii=False, sort_keys=True
        )
        data = {
            "fragment_id": fragment_id,
            "topic_id": fragment.topic_id,
            "topic_name": node.name,
            "title": title,
            "source_fragment_id": result.get("source_fragment_id") or fragment_id,
            "opens_current": bool(result.get("opens_current")),
            "context": context,
        }

        if result.get("opens_current"):
            return ToolResult(
                ok=True,
                content=(
                    f"「{title}」就是当前正在继续的片段，不需要新建接续片段。"
                    f"\n\n{context_text}"
                ),
                protocol_version=1,
                status="completed",
                code="continuation_already_current",
                data=data,
            )
        return ToolResult(
            ok=True,
            content=(
                f"已登记：你的下一条消息将从历史片段「{title}」"
                f"（fragment_id={fragment_id}，话题「{node.name}」）继续。"
                "这条登记只对之后提交的消息生效：本轮归属不变，已经排队但还没开始的消息也不改向。"
                "那段历史保持原样，接续片段会记下它的来源（source_fragment_id="
                f"{result.get('source_fragment_id') or fragment_id}）。"
                "本轮需要引用那段讨论的细节时，下面已给出该片段的相关上下文；"
                "仍需要更多细节时再用 memory_search 继续查。"
                f"\n\n{context_text}"
            ),
            protocol_version=1,
            status="completed",
            code="continuation_registered",
            data=data,
        )

    def _focus_context(
        self,
        *,
        fragment_id: str,
        topic_id: str,
        topic_name: str,
        title: str,
        summary: str,
        messages: list[Any],
    ) -> dict[str, Any]:
        """构造**本轮即可使用**的片段上下文（摘要 + 头尾原文摘录）。

        规模由 FocusPolicy 控制：单条消息按 max_message_chars 截断，整块按
        max_tokens 收敛（从最早的尾部消息开始丢，保证摘要与片段开头仍在）。
        数据先按完整条目收敛，再交给上层序列化；这里不做序列化后的截断。
        """
        from agent.memory.index import estimate_tokens

        rows = [m for m in messages if (m["content"] or "").strip()]
        head = rows[: FOCUS.head_messages]
        tail = rows[-FOCUS.tail_messages :] if len(rows) > len(head) else []
        head_ids = {m["id"] for m in head}
        tail = [m for m in tail if m["id"] not in head_ids]
        omitted = max(0, len(rows) - len(head) - len(tail))

        def render(message: Any) -> dict[str, Any]:
            body = str(message["content"] or "")
            if len(body) > FOCUS.max_message_chars:
                body = body[: FOCUS.max_message_chars] + "…"
            return {
                "message_id": message["id"],
                "role": message["role"],
                "content": body,
            }

        summary_text = summary.strip()
        if len(summary_text) > FOCUS.max_summary_chars:
            summary_text = summary_text[: FOCUS.max_summary_chars] + "…"

        head_items = [render(m) for m in head]
        tail_items = [render(m) for m in tail]

        def payload(head_items: list[dict], tail_items: list[dict]) -> dict[str, Any]:
            return {
                "fragment_id": fragment_id,
                "topic_id": topic_id,
                "topic_name": topic_name,
                "title": title,
                "summary": summary_text,
                "head": head_items,
                "tail": tail_items,
                "omitted_messages": omitted,
                "note": "该片段是用户选择继续的历史位置，以上是程序给出的摘要与真实原文摘录。",
            }

        context = payload(head_items, tail_items)
        while estimate_tokens(json.dumps(context, ensure_ascii=False)) > FOCUS.max_tokens and tail_items:
            tail_items = tail_items[1:]
            context = payload(head_items, tail_items)
        while (
            estimate_tokens(json.dumps(context, ensure_ascii=False)) > FOCUS.max_tokens
            and len(head_items) > 1
        ):
            head_items = head_items[1:]
            context = payload(head_items, tail_items)
        return context

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
