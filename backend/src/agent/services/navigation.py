"""统一 Topic 导航入口（TopicNavigationService）。

第二阶段的核心逻辑修复之一：**Anchor 只能有一个写入者**。

必须分开的概念：

* **当前话题 / Anchor**：用户当前对话真正继续发生的位置。只有本模块能改它；
* **选中话题**：用户在星球上正在浏览谁（纯前端状态，不改 Anchor）；
* **引用话题**：为了回答当前问题临时读取另一个话题（检索 / Focus，只读）；
* **待确认切换**：Predictor 觉得「可能属于另一个话题」时给出的建议，等用户点头才生效。

因此禁止这些隐式联动（第二阶段明确要求删除）：

    选中 Topic → currentTopic = selectedTopic
    Memory Retrieval 命中 Topic → currentTopic = result.topic
    Predictor 判断属于别的话题 → 直接改 Anchor

所有真正的 Topic Switch 都从这里走：进入话题 / 创建话题 / 确认切换 / 从历史继续。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.graph.anchors import AnchorService
from agent.graph.edges import EdgeService
from agent.graph.nodes import NodeService
from agent.memory.fragment import FragmentManager, new_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TopicNotFound(ValueError):
    """目标话题不存在。"""


class FragmentNotInTopic(ValueError):
    """片段不存在，或者不属于这个话题（绝不能把别的历史注入进来）。"""


@dataclass(frozen=True)
class NavigationResult:
    topic_id: str
    fragment_id: str | None
    fragment_title: str | None
    historic: bool
    created_fragment_id: str | None = None
    source_fragment_id: str | None = None


def relate_topics(conn: sqlite3.Connection, a: str, b: str) -> None:
    """两个话题之间的 related 边（无向，方向归一化后权重才能累加）。"""
    if not a or not b or a == b:
        return
    src, dst = sorted([a, b])
    EdgeService(conn).add(src, dst, "related")


# 明确的导航动词。只有「动词 + 已知话题名」同时命中才算明确切换：
# 单纯提到某个话题名（「顺丁橡胶降解的数据不错」）不是导航意图。
_NAV_VERBS = (
    "切换到",
    "切到",
    "转到",
    "换到",
    "回到",
    "返回",
    "继续之前的",
    "接着之前的",
    "继续之前",
)


def detect_explicit_navigation(
    message: str, topics: list[tuple[str, str]] | tuple[tuple[str, str], ...]
) -> str | None:
    """识别「用户明确要求切换话题」的指令，返回目标 topic_id。

    `topics` 是 [(topic_id, title)]。命中条件：句子里出现导航动词，
    动词之后的部分能匹配到一个**已知话题名**（最长匹配优先）。
    """
    if not message:
        return None
    for verb in _NAV_VERBS:
        idx = message.find(verb)
        if idx < 0:
            continue
        tail = message[idx + len(verb):].strip(" 　「」《》\"'：:，,。.?!~～、")
        if not tail:
            continue
        best: tuple[str, int] | None = None
        for topic_id, title in topics:
            if not title:
                continue
            matched = title in tail or (len(tail) >= 2 and tail in title)
            if matched and (best is None or len(title) > best[1]):
                best = (topic_id, len(title))
        if best is not None:
            return best[0]
    return None


class TopicNavigationService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.nodes = NodeService(conn)
        self.anchors = AnchorService(conn)
        self.fragments = FragmentManager(conn)
        self._pending_reason: str = ""

    # -- 真正改变对话位置的动作 ------------------------------------------

    def enter_topic(
        self,
        topic_id: str,
        *,
        fragment_id: str | None = None,
        relate: bool = False,
        restore_position: bool = True,
    ) -> NavigationResult:
        """进入一个话题：Anchor 移到该话题（有历史位置就恢复它）。

        `fragment_id=None` 表示「进入这个话题的最新位置」，与
        「从某段历史继续」是两个明确不同的动作（见 continue_from_history）。

        `restore_position=False` 用于「用户明确选择从最新位置开始」：
        此时不恢复该话题保存过的旧位置，而是把位置直接落到最新（None）。
        """
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        if fragment_id is not None:
            fragment = self.fragments.get(fragment_id)
            if fragment is None or fragment.topic_id != topic_id:
                raise FragmentNotInTopic(f"片段不属于该话题：{fragment_id}")
        elif restore_position:
            # 切回话题时恢复它保存的位置（没有 / 已失效则为 None）
            fragment_id = self.anchors.position_fragment(topic_id)
        previous = self.anchors.get_active()
        self.anchors.set_active(topic_id, fragment_id)
        if relate and previous is not None and previous.topic_id:
            relate_topics(self.conn, previous.topic_id, topic_id)
        return self._result(topic_id, fragment_id)

    def continue_from_history(
        self, topic_id: str, fragment_id: str, *, relate: bool = True
    ) -> NavigationResult:
        """从一段历史接续：**创建新片段**，并记下来源。

        旧片段（已封块的历史）永远保持不变：关闭的 Fragment 不得重新修改。
        新片段在数据层记 `source_fragment_id`，未来可以做分支历史 UI
        （A → B → C 与 A → D 并存），本阶段不要求树状界面。
        """
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        source = self.fragments.get(fragment_id)
        if source is None or source.topic_id != topic_id:
            raise FragmentNotInTopic(f"片段不属于该话题：{fragment_id}")
        if source.closed_at is None:
            # 来源就是当前开放片段：它本来就是对话的位置，不需要再开一段
            return self.enter_topic(topic_id, fragment_id=fragment_id, relate=relate)

        new_fragment_id = new_id("frag")
        self.conn.execute(
            "INSERT INTO fragments (id, topic_id, created_at, summary_version, meta, source_fragment_id) "
            "VALUES (?, ?, ?, 0, '{}', ?)",
            (new_fragment_id, topic_id, _now(), fragment_id),
        )
        previous = self.anchors.get_active()
        self.anchors.set_active(topic_id, new_fragment_id)
        if relate and previous is not None and previous.topic_id:
            relate_topics(self.conn, previous.topic_id, topic_id)
        return self._result(
            topic_id,
            new_fragment_id,
            created_fragment_id=new_fragment_id,
            source_fragment_id=fragment_id,
        )

    def create_topic(self, name: str, *, relate: bool = True) -> NavigationResult:
        node = self.nodes.create_topic(name)
        previous = self.anchors.get_active()
        self.anchors.set_active(node.id)
        if relate and previous is not None and previous.topic_id:
            relate_topics(self.conn, previous.topic_id, node.id)
        return self._result(node.id, None)

    # -- 待确认切换 -------------------------------------------------------

    def request_switch(self, topic_id: str, *, reason: str = "") -> dict:
        """登记「推测切换」：只写 pending，不碰 active。"""
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        self.anchors.request_pending(topic_id)
        self._pending_reason = reason
        return {"topic_id": topic_id, "topic_name": node.name, "reason": reason}

    def pending_switch(self) -> dict | None:
        pending = self.anchors.get_pending()
        if pending is None or not pending.topic_id:
            return None
        node = self.nodes.get_topic(pending.topic_id)
        if node is None:
            return None
        return {
            "topic_id": pending.topic_id,
            "topic_name": node.name,
            "fragment_id": pending.fragment_id,
            "reason": self._pending_reason,
        }

    def confirm_switch(self) -> NavigationResult | None:
        pending = self.pending_switch()
        if pending is None:
            return None
        result = self.enter_topic(pending["topic_id"], fragment_id=pending["fragment_id"])
        self.reject_switch()
        return result

    def reject_switch(self) -> None:
        """用户说「保留当前」：待确认清空，Anchor 一动不动。"""
        self.anchors.discard_pending()
        self._pending_reason = ""

    # -- 只读辅助 ---------------------------------------------------------

    def fragment_title(self, fragment_id: str | None) -> str | None:
        if not fragment_id:
            return None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? ORDER BY created_at DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
        if row is not None and row["title"]:
            return str(row["title"])
        fragment = self.fragments.get(fragment_id)
        if fragment is not None and fragment.summary:
            return fragment.summary.strip().splitlines()[0][:40]
        return None

    def _result(
        self,
        topic_id: str,
        fragment_id: str | None,
        *,
        created_fragment_id: str | None = None,
        source_fragment_id: str | None = None,
    ) -> NavigationResult:
        return NavigationResult(
            topic_id=topic_id,
            fragment_id=fragment_id,
            fragment_title=self.fragment_title(fragment_id),
            historic=self.anchors.is_historic_position(topic_id),
            created_fragment_id=created_fragment_id,
            source_fragment_id=source_fragment_id,
        )
