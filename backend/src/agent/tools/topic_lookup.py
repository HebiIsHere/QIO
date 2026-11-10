# -*- coding: utf-8 -*-
"""lookup_topic：只读解析真实话题对象（R14 / F11，契约 §3.8）。

为什么需要它：

* `services/topic_decision.build_candidates` 每轮最多给 5 个候选；
  `switch_topic` 的说明又限制 topic_id 只能来自本轮候选 —— 排名第 6 及更后的
  真实话题，模型没有可靠的解析路径；
* `memory_search` 只覆盖「封存并摘要成功」的片段与保存的原文；只有名称、
  没有消息/片段的**空话题**在它的两张表里都没有数据，检索不到；
* 于是模型可能编造 topic_id，或为重名/空话题/已结束话题重复创建。

本工具只做**只读解析**，是「名称 / topic_id → 真实话题节点」的唯一最小入口：

* 同名 / 归一化同名 / 包含匹配的**全部**真实候选都列出来，不擅自挑选；
* 目标不存在 / 已删除 → 明确 `not_found`：不编 ID、不自动新建；
* 空话题与已结束话题如实标注状态与区分信息；
* 是否可切换按现有产品规则如实返回（`switch_topic` 只校验话题存在，
  已结束话题仍按 ID 可达、记忆保留）；
* 零副作用：只读 SELECT；不改 Anchor、不建片段、不登记接续、不推进导航版本、
  不发审批、不执行任何切换或新建。

程序只解析对象，不做意图判断：查询本身不代表要切换，也不替主模型或用户做决定。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from agent import prompts
from agent.graph.anchors import AnchorService
from agent.graph.nodes import Node, NodeService
from agent.services.topic_operation import normalize_topic_name
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult, ToolStatus

#: 单次最多返回多少条真实候选。这不是「挑几个」：超过上限时如实报告 omitted，
#: 由调用方用更准确的名称 / topic_id 继续查询。
MAX_MATCHES = 20

#: 区分信息里的摘要预览长度（与 TopicService.fingerprint 的 80 字同口径）。
SUMMARY_PREVIEW_CHARS = 80

_MATCH_ID = "id"
_MATCH_EXACT = "exact"
_MATCH_NORMALIZED = "normalized"
_MATCH_CONTAINS = "contains"

#: 匹配层级：0 精确同名 → 1 归一化同名 → 2 包含匹配。
_TIER_EXACT = 0
_TIER_NORMALIZED = 1
_TIER_CONTAINS = 2

_TIER_LABELS = {
    _TIER_EXACT: _MATCH_EXACT,
    _TIER_NORMALIZED: _MATCH_NORMALIZED,
    _TIER_CONTAINS: _MATCH_CONTAINS,
}


def _flag(value: Any) -> bool:
    """include_ended 的宽松解析：只接受真实的布尔语义，不把 "false" 当 True。"""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _fold(value: Any) -> str:
    """名称比较口径：与查重归一化同一口径 + ASCII 大小写折叠。

    这只决定「哪些真实话题可以列出来作证据」，不改变任何真实名称，也不参与
    执行参数 / 批准身份（契约 §3.2：查重归一化只作证据）。
    """
    return normalize_topic_name(value).casefold()


def _ended_meta(meta: dict | None) -> tuple[bool, str | None, str | None]:
    """(是否已结束, ended_at, ended_reason)：只读 nodes.meta 的真实标记。"""
    data = meta if isinstance(meta, dict) else {}
    ended_at = str(data.get("ended_at") or "").strip() or None
    reason = str(data.get("ended_reason") or "").strip() or None
    return (ended_at is not None, ended_at, reason)


def _clip(text: Any, limit: int) -> str | None:
    value = " ".join(str(text or "").split())
    if not value:
        return None
    if limit > 0 and len(value) > limit:
        return value[:limit] + "…"
    return value


def _invalid(message: str) -> ToolResult:
    """参数不成立：结构化失败（工具没有被执行，也没有任何副作用）。"""
    return ToolResult(
        ok=False,
        error=message,
        status=ToolStatus.FAILED.value,
        code="invalid_arguments",
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        data={"query": {}, "found": False, "topics": []},
    )


class LookupTopicTool(Tool):
    """只读解析真实话题对象（name / topic_id → 真实节点）。"""

    name = "lookup_topic"
    description = prompts.TOOL_DESCRIPTIONS[name]
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": prompts.argument_description("lookup_topic", "name"),
            },
            "topic_id": {
                "type": "string",
                "description": prompts.argument_description("lookup_topic", "topic_id"),
            },
            "include_ended": {
                "type": "boolean",
                "description": prompts.argument_description(
                    "lookup_topic", "include_ended"
                ),
            },
        },
        "required": [],
    }
    # 只读查询：可与其他并发安全工具并行执行
    is_concurrency_safe = True

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- 入口 --------------------------------------------------------------

    async def run(self, **kwargs: Any) -> ToolResult:
        return self.run_sync(**kwargs)

    def run_sync(self, **kwargs: Any) -> ToolResult:
        name = str(kwargs.get("name") or "").strip()
        topic_id = str(kwargs.get("topic_id") or "").strip()
        include_ended = _flag(kwargs.get("include_ended"))
        if not name and not topic_id:
            return _invalid(
                "name 与 topic_id 至少提供一个：请传要查找的真实话题名称或话题 id。"
            )
        current_topic_id = self._current_topic_id()
        query = {
            "name": name or None,
            "topic_id": topic_id or None,
            "include_ended": include_ended,
        }
        # topic_id 是权威身份：给了就按 ID 精确解析（名称只作为原样回显的查询条件）。
        if topic_id:
            node = NodeService(self.conn).get_topic(topic_id)
            if node is None:
                return self._not_found(query, current_topic_id, ended_hidden=0)
            record = self._record(
                node,
                match=_MATCH_ID,
                current_topic_id=current_topic_id,
            )
            return self._found(
                query,
                [record],
                matched_total=1,
                omitted=0,
                ended_hidden=0,
                current_topic_id=current_topic_id,
            )
        return self._lookup_by_name(
            name, query, include_ended=include_ended, current_topic_id=current_topic_id
        )

    # -- 名称解析 ----------------------------------------------------------

    def _lookup_by_name(
        self,
        name: str,
        query: dict,
        *,
        include_ended: bool,
        current_topic_id: str | None,
    ) -> ToolResult:
        nodes = NodeService(self.conn).list_topics()
        folded_query = _fold(name)
        matched: list[tuple[int, Node]] = []
        for node in nodes:
            tier = self._match_tier(str(node.name or ""), name, folded_query)
            if tier is not None:
                matched.append((tier, node))
        # include_ended 只影响「包含匹配」：精确 / 归一化同名是权威身份事实，
        # 隐藏它会让模型误以为不存在（进而重复创建同名话题）。
        kept: list[tuple[int, Node]] = []
        ended_hidden = 0
        for tier, node in matched:
            ended, _ended_at, _reason = _ended_meta(node.meta)
            if tier == _TIER_CONTAINS and ended and not include_ended:
                ended_hidden += 1
                continue
            kept.append((tier, node))

        if not kept:
            return self._not_found(
                query, current_topic_id, ended_hidden=ended_hidden
            )

        # 稳定顺序：匹配层级优先（精确 > 归一化 > 包含），同层最近更新在前，
        # 同更新时间按 topic_id 稳定排序。排序不是「挑选」，只是确定性展示。
        ordered = sorted(kept, key=lambda pair: pair[1].id)
        ordered.sort(key=lambda pair: str(pair[1].updated_at or ""), reverse=True)
        ordered.sort(key=lambda pair: pair[0])
        matched_total = len(ordered)
        page = ordered[:MAX_MATCHES]
        records = [
            self._record(
                node,
                match=_TIER_LABELS[tier],
                current_topic_id=current_topic_id,
            )
            for tier, node in page
        ]
        return self._found(
            query,
            records,
            matched_total=matched_total,
            omitted=max(0, matched_total - len(records)),
            ended_hidden=ended_hidden,
            current_topic_id=current_topic_id,
        )

    @staticmethod
    def _match_tier(node_name: str, query: str, folded_query: str) -> int | None:
        raw_name = str(node_name or "").strip()
        if raw_name == query:
            return _TIER_EXACT
        if not folded_query:
            return None
        folded_name = _fold(raw_name)
        if not folded_name:
            return None
        if folded_name == folded_query:
            return _TIER_NORMALIZED
        if folded_query in folded_name or folded_name in folded_query:
            return _TIER_CONTAINS
        return None

    # -- 记录：真实区分信息 ------------------------------------------------

    def _current_topic_id(self) -> str | None:
        """真实 Anchor 的当前话题（只读；读不到按未知处理，不猜）。"""
        try:
            active = AnchorService(self.conn).get_active()
        except Exception:  # noqa: BLE001 - 只读辅助信息，读不到不影响解析
            return None
        return getattr(active, "topic_id", None) if active is not None else None

    def _stats(self, topic_id: str) -> dict[str, Any]:
        frag = self.conn.execute(
            "SELECT COUNT(*) AS c, MAX(created_at) AS m FROM fragments "
            "WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        messages = self.conn.execute(
            "SELECT COUNT(*) AS c, MAX(m.created_at) AS m FROM messages m "
            "JOIN fragments f ON f.id = m.fragment_id WHERE f.topic_id = ?",
            (topic_id,),
        ).fetchone()
        summary_row = self.conn.execute(
            "SELECT summary FROM fragments WHERE topic_id = ? "
            "AND summary IS NOT NULL AND summary <> '' "
            "ORDER BY created_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        fragment_count = int(frag["c"] if frag is not None else 0)
        message_count = int(messages["c"] if messages is not None else 0)
        timestamps = [
            str(value)
            for value in (
                frag["m"] if frag is not None else None,
                messages["m"] if messages is not None else None,
            )
            if value
        ]
        return {
            "fragment_count": fragment_count,
            "message_count": message_count,
            "last_activity": max(timestamps) if timestamps else None,
            "summary_preview": _clip(
                summary_row["summary"] if summary_row is not None else None,
                SUMMARY_PREVIEW_CHARS,
            ),
        }

    def _record(
        self,
        node: Node,
        *,
        match: str,
        current_topic_id: str | None,
    ) -> dict[str, Any]:
        stats = self._stats(node.id)
        ended, ended_at, ended_reason = _ended_meta(node.meta)
        is_current = bool(current_topic_id and current_topic_id == node.id)
        status = "ended" if ended else "active"
        return {
            "topic_id": node.id,
            "name": node.name,
            "type": "topic",
            "status": status,
            "ended": ended,
            "ended_at": ended_at,
            "ended_reason": ended_reason,
            "match": match,
            "empty": stats["fragment_count"] == 0 and stats["message_count"] == 0,
            "fragment_count": stats["fragment_count"],
            "message_count": stats["message_count"],
            "last_activity": stats["last_activity"],
            "summary_preview": stats["summary_preview"],
            "is_current": is_current,
            # 现有产品规则（唯一执行者 switch_topic 只校验话题是否存在；已结束话题
            # 仍按 ID 可达 = 真实可切换目标）。这里只陈述事实，不构成授权。
            "switchable": True,
            "switch_note": self._switch_note(status=status, is_current=is_current),
        }

    @staticmethod
    def _switch_note(*, status: str, is_current: bool) -> str:
        if is_current:
            return "这就是当前话题；不需要切换，可以直接继续当前讨论。"
        if status == "ended":
            return (
                "已结束话题：不会出现在星球主视图，但记忆保留；现有规则下仍可按该 "
                "topic_id 调用 switch_topic，是否切换由你按话题规则判断并由用户决定。"
            )
        return (
            "真实存在的话题；需要换题时仍按 switch_topic 的 intent 规则判断，"
            "程序会执行既有校验与确认。"
        )

    # -- 结果 --------------------------------------------------------------

    def _found(
        self,
        query: dict,
        records: list[dict[str, Any]],
        *,
        matched_total: int,
        omitted: int,
        ended_hidden: int,
        current_topic_id: str | None,
    ) -> ToolResult:
        notes = [
            "以上都是真实存在的话题节点（只读查询：没有改变当前讨论位置，"
            "也没有执行任何切换或新建）。",
            "同名/相似名可能同时存在：全部列出，不替你挑选；需要唯一目标时先向用户澄清。",
        ]
        shown = [record["topic_id"] for record in records]
        if matched_total > 1:
            notes.append(
                f"共有 {matched_total} 个匹配的真实话题；本次返回 {len(shown)} 个。"
            )
        if omitted > 0:
            notes.append(
                f"匹配超过单次返回上限 {MAX_MATCHES} 个，还有 {omitted} 个没有列出；"
                "请用更准确的名称或 topic_id 继续查询。"
            )
        if ended_hidden > 0:
            notes.append(
                f"另有 {ended_hidden} 个名称包含该词的已结束话题被 include_ended=false 隐藏；"
                "需要时用 include_ended=true 重新查询。"
            )
        data = {
            "query": dict(query),
            "found": True,
            "topics": records,
            "matched_total": int(matched_total),
            "omitted": int(omitted),
            "ended_hidden": int(ended_hidden),
            "current_topic_id": current_topic_id,
            "notes": notes,
        }
        names = "、".join(
            "「" + str(record["name"]) + "」（" + str(record["topic_id"]) + "）"
            for record in records[:3]
        )
        content = f"找到 {matched_total} 个真实话题：" + names
        return ToolResult(
            ok=True,
            content=content,
            status=ToolStatus.COMPLETED.value,
            code="ok",
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            data=data,
        )

    def _not_found(
        self,
        query: dict,
        current_topic_id: str | None,
        *,
        ended_hidden: int,
    ) -> ToolResult:
        notes = [
            "没有找到匹配的真实话题。不要编造 topic_id，也不要为了这次查询自动"
            "新建同名话题；如果确实需要新话题，按 create_topic 的规则处理。",
        ]
        if ended_hidden > 0:
            notes.append(
                f"另有 {ended_hidden} 个名称包含该词的已结束话题被 include_ended=false 隐藏；"
                "需要时用 include_ended=true 重新查询。"
            )
        data = {
            "query": dict(query),
            "found": False,
            "topics": [],
            "matched_total": 0,
            "omitted": 0,
            "ended_hidden": int(ended_hidden),
            "current_topic_id": current_topic_id,
            "notes": notes,
        }
        return ToolResult(
            ok=True,
            content="没有找到匹配的真实话题。",
            status=ToolStatus.COMPLETED.value,
            code="not_found",
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            data=data,
        )
