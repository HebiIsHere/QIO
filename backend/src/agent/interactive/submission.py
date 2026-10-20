"""可见范围、有效改动、提交前后状态（子智能体 B 负责实现）。

契约：docs/interactive-mode-contract.md §1.4 / §1.5 / §3。

硬约束（不许绕过）：

1. 保存不调用 QIO；**只有 submit_board 会让 QIO 拿到表达**。提交成功（succeeded）时
   **受理 = 主循环受理**（阶段三契约 §13.3）：本函数通过注入的 turn_acceptor 把这次提交
   作为主循环的一个 Turn 受理（同一 TurnManager / 同一模型与工具循环），不再创建独立
   理解请求、不再启动理解模型调用。受理成功 → delivery.delivered=true、delivery.turnId、
   delivery.status='accepted'，响应带 turnId / waiting，并写「提交—turn 绑定」（迁移 49）；
   没有受理器 / 受理失败 → 这次提交按 failed 处理：不更新基准、不改动勾选、如实返回原因，
   绝不假装 QIO 受理了，也绝不回退旧理解路径。旧 board_understanding 记录保持只读兼容。
2. 未勾选的注释、明确隐藏的卡片：文字与链接都不得进入 before / after；
   链接必须两端都在范围内才出现；一名可见成员都没有的组整体不出现。
3. 提交成功才更新「上次成功提交」基准；失败保留改动与本次注释选择。
4. 空提交 / 重复点击必须幂等：不更新基准、不调用 QIO。

两个投影：

- project_snapshot(state)：**本次允许查看的范围**（= models.selectable_cards 的唯一实现）；
- project_baseline(baseline, state)：把上次成功提交时交给 QIO 的内容，投影到**本次**
  可见范围，作为提交的 before。仍在范围内的保留原文（这样才能求差）；现在已删除、
  但当时已提交的保留下来表达「撤回」；现在未勾选 / 被隐藏的整条丢弃（不得重新进入载荷）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agent.interactive import board_store, models
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

class StaleState(Exception):
    """提交被拒（M4 路径 3）：候选版本 / 确认范围与当前服务端不一致。

    触发它的提交**不落库**：不产生提交记录、不更新基准、不调用 QIO。
    """

class RequestIdentityConflict(Exception):
    """提交被拒（契约 §1.1 第 3 条）：同一个提交请求身份，内容却不一致。

    一次提交请求有稳定身份（client_request_id）。同身份同内容 = 重试（返回原事实）；
    同身份**不同内容**（base_state_version 或 note 不一致）= 冲突 —— 不得把另一个窗口 /
    另一次内容当成同一次提交的受理结果。

    触发它的提交**不落库**：不产生提交记录、不新建理解请求、不改基准、不调用模型。
    """


#: 提交请求身份的上限（与 understanding.request_id_for 拼接后仍在合理长度内）。
MAX_CLIENT_REQUEST_ID = 200


#: 只影响显示、不构成提交内容的表达式：普通移动 / 缩放。
#: 注意它与 models.NON_INTENT_EXPRESSIONS 不是一回事：材料增删、注释撤回、关系移除
#: 虽然 intentBearing=false（不代表用户提出的工作），但它们仍然是**板面内容变化**，
#: 需要交给 QIO 了解，所以不算「空提交」。
DISPLAY_ONLY_EXPRESSIONS = ("layout_only",)

_SNIPPET_LIMIT = 24


# --- 小工具 -------------------------------------------------------------


def _snippet(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "（无文字）"
    return text if len(text) <= _SNIPPET_LIMIT else text[:_SNIPPET_LIMIT] + "…"


def _meta_key(meta: Any) -> str:
    return json.dumps(meta or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def card_label(card: dict) -> str:
    """给表达式摘要用的可读标签（只用于可见范围内的卡片）。"""
    kind = card.get("kind")
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    if kind == "text":
        return _snippet(card.get("content"))
    if kind == "file":
        return _snippet(meta.get("name") or meta.get("title") or card.get("content"))
    if kind == "code":
        language = str(meta.get("language") or "").strip()
        return f"{language} 代码" if language else _snippet(card.get("content"))
    if kind == "url":
        return _snippet(meta.get("title") or meta.get("href") or card.get("content"))
    return _snippet(card.get("content"))


def _card_meaning(card: dict) -> tuple:
    """卡片的意思（不含位置 / 大小 / 折叠 / 书签）。"""
    return (card.get("kind"), str(card.get("content") or ""), _meta_key(card.get("meta")))


def _reply_source(card: dict) -> dict:
    """结果卡的来源（F4）：写进 result_added / result_updated 的载荷，而不是只给卡片编号。

    来源取 `meta.reply`（执行时写入的 intentId / understandingId / executionId / stepIndex）；
    没有来源时返回空对象 —— 不编造。
    """
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    reply = meta.get("reply") if isinstance(meta.get("reply"), dict) else {}
    return {
        key: reply[key]
        for key in ("intentId", "understandingId", "executionId", "stepIndex")
        if reply.get(key) is not None
    }


def _card_layout(card: dict) -> tuple:
    def num(value: Any) -> float:
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return 0.0

    return (num(card.get("x")), num(card.get("y")), num(card.get("w")), num(card.get("h")))


def _expr(
    kind: str,
    *,
    summary: str,
    card_ids: list[str] | tuple[str, ...] = (),
    group_id: str | None = None,
    link_id: str | None = None,
    content: str | None = None,
    source: dict | None = None,
    updated_at: Any = None,
) -> dict:
    item = {
        "id": models.new_id("x"),
        "kind": kind,
        "intentBearing": kind not in models.NON_INTENT_EXPRESSIONS,
        "summary": summary,
        "cardIds": [str(cid) for cid in card_ids],
        "groupId": group_id,
        "linkId": link_id,
    }
    # F4：结果卡的表达必须带**正文与来源**（不能只给卡片编号或任务标题）。
    if content is not None:
        item["content"] = str(content)
    if isinstance(source, dict) and source:
        item["source"] = dict(source)
    if updated_at:
        item["updatedAt"] = str(updated_at)
    return item


def _empty_projection(*, first_submission: bool = False) -> dict:
    snapshot = {"cards": [], "groups": [], "links": [], "selection": [], "empty": True}
    if first_submission:
        # 契约 §6.7：首次提交无历史基准时，before 是「空快照 + firstSubmission」
        snapshot["firstSubmission"] = True
    return snapshot


# --- 可见范围投影 -------------------------------------------------------


def project_snapshot(state: dict) -> dict:
    """把板面状态投影到**本次允许查看的范围**（提交载荷与前后状态都用它）。

    - 卡片：models.selectable_cards（唯一实现）；
    - 组：至少有一名可见成员才出现，且只列出可见成员；
    - 链接：两端都可见才出现；
    - selection：只保留仍然可见的卡片（取消掉的选择不产生表达）。
    """
    visible_cards = models.selectable_cards(state)
    visible_ids = {card["id"] for card in visible_cards if card.get("id")}
    groups: list[dict] = []
    for group in state.get("groups", []):
        if group.get("deleted"):
            continue
        members = [cid for cid in group.get("members", []) if cid in visible_ids]
        if not members:
            # 一名可见成员都没有的组整体不出现：组名会间接暴露被隐藏注释的存在
            continue
        item = dict(group)
        item["members"] = members
        groups.append(item)
    links: list[dict] = []
    for link in state.get("links", []):
        if link.get("deleted"):
            continue
        if link.get("src") in visible_ids and link.get("dst") in visible_ids:
            links.append(dict(link))
    selection = [cid for cid in state.get("selection", []) if cid in visible_ids]
    snapshot = {
        "cards": [dict(card) for card in visible_cards],
        "groups": groups,
        "links": links,
        "selection": selection,
    }
    snapshot["empty"] = not (snapshot["cards"] or snapshot["groups"] or snapshot["links"])
    return snapshot


def visible_range(state: dict) -> dict:
    """提交前预览：本次允许查看的范围 + 只给用户看的「不在其中」数量。"""
    snapshot = project_snapshot(state)
    visible_ids = {card["id"] for card in snapshot["cards"]}
    live = [card for card in state.get("cards", []) if models.is_live(card)]
    snapshot["notVisibleCount"] = len([c for c in live if c.get("id") not in visible_ids])
    return snapshot


def project_baseline(baseline: dict | None, state: dict) -> dict:
    """把上次成功提交的内容投影成本次的 before（见模块 docstring）。"""
    if not baseline:
        return _empty_projection(first_submission=True)
    stored = baseline.get("snapshot")
    if not isinstance(stored, dict):
        return _empty_projection(first_submission=True)
    current_cards = {
        card["id"]: card for card in state.get("cards", []) if card.get("id")
    }
    live = {cid: card for cid, card in current_cards.items() if models.is_live(card)}
    visible_now = models.selectable_ids(state)
    keep: dict[str, dict] = {}
    for card in stored.get("cards", []):
        cid = card.get("id")
        if not cid:
            continue
        if cid in visible_now:
            # 仍然允许查看：保留上次交给 QIO 的原文，才能求出「改了什么」
            keep[cid] = dict(card)
        elif cid not in live:
            # 已被删除：判据必须用**当前**卡片，而且**忽略 deleted 也仍然可见**才保留
            # （契约 §1.4 硬边界：未勾选 / 明确隐藏的注释，连「撤回」也不能把它带回
            #  before —— 否则删掉一条未勾选注释就会让它的文字、id 与链接重新进入载荷）。
            current = current_cards.get(cid)
            if current is not None and models.visible_except_deleted(current):
                keep[cid] = dict(card)
        # 未勾选 / 被隐藏：整条丢弃，文字与链接都不得重新进入载荷
    groups: list[dict] = []
    for group in stored.get("groups", []):
        members = [cid for cid in group.get("members", []) if cid in keep]
        if not members:
            continue
        item = dict(group)
        item["members"] = members
        groups.append(item)
    links = [
        dict(link)
        for link in stored.get("links", [])
        if link.get("src") in keep and link.get("dst") in keep
    ]
    selection = [cid for cid in stored.get("selection", []) if cid in keep]
    snapshot = {
        "cards": [dict(card) for card in keep.values()],
        "groups": groups,
        "links": links,
        "selection": selection,
    }
    snapshot["empty"] = not (snapshot["cards"] or snapshot["groups"] or snapshot["links"])
    return snapshot


# --- 有效改动（由前后两份状态求差，不是操作流水） -------------------------


def _is_reply(card: dict) -> bool:
    return card.get("kind") == models.REPLY_KIND


def _link_key(link: dict) -> tuple:
    src, dst = str(link.get("src") or ""), str(link.get("dst") or "")
    return (src, dst) if link.get("direction") else tuple(sorted((src, dst)))


def _link_text(link: dict, cards: dict[str, dict]) -> str:
    src = card_label(cards.get(str(link.get("src")), {}))
    dst = card_label(cards.get(str(link.get("dst")), {}))
    arrow = "→" if link.get("direction") else "—"
    meaning = str(link.get("meaning") or "").strip()
    text = f"{src} {arrow} {dst}"
    return f"{text}（含义：{_snippet(meaning)}）" if meaning else text


def diff_states(before: dict, after: dict) -> list[dict]:
    """由前后两份（已投影到可见范围的）状态求差，得到本次有效改动。

    不是操作流水：提交前撤销掉的中间操作不会出现，普通移动 / 缩放标记为 layout_only
    且 intentBearing=false，取消掉的选择不产生表达，仍然有效的多选 / 区域选择作为关注范围。
    """
    b_cards = {c["id"]: c for c in before.get("cards", []) if c.get("id")}
    a_cards = {c["id"]: c for c in after.get("cards", []) if c.get("id")}
    b_groups = {g["id"]: g for g in before.get("groups", []) if g.get("id")}
    a_groups = {g["id"]: g for g in after.get("groups", []) if g.get("id")}
    expressions: list[dict] = []

    # 1) 卡片：新增 / 撤回 / 内容修改 / 仅位置变化
    for cid in sorted(a_cards.keys() - b_cards.keys()):
        card = a_cards[cid]
        if _is_reply(card):
            # F4：用户明确许可查看的结果卡进入范围 → 结果新增（载荷带正文与来源）。
            expressions.append(
                _expr(
                    "result_added",
                    card_ids=[cid],
                    summary=f"新增可见结果卡：{card_label(card)}",
                    content=str(card.get("content") or ""),
                    source=_reply_source(card),
                    updated_at=card.get("updatedAt"),
                )
            )
            continue
        is_note = card.get("kind") == "text"
        expressions.append(
            _expr(
                "note_added" if is_note else "material_added",
                card_ids=[cid],
                summary=(
                    f"新增注释：{card_label(card)}"
                    if is_note
                    else f"新增材料（{card.get('kind')}）：{card_label(card)}"
                ),
            )
        )
    for cid in sorted(b_cards.keys() - a_cards.keys()):
        card = b_cards[cid]
        if _is_reply(card):
            # 撤回一张结果卡不是「用户提出的新工作」，也不是材料撤回：
            # 这里不产出表达（F4 只要求「新增」与「已可见内容变化」两种），
            # 但它仍然立刻退出可见范围（after 里没有它）。
            continue
        is_note = card.get("kind") == "text"
        expressions.append(
            _expr(
                "note_deleted" if is_note else "material_removed",
                card_ids=[cid],
                summary=(
                    f"撤回注释：{card_label(card)}"
                    if is_note
                    else f"移除材料（{card.get('kind')}）：{card_label(card)}"
                ),
            )
        )
    for cid in sorted(a_cards.keys() & b_cards.keys()):
        old, new = b_cards[cid], a_cards[cid]
        if _is_reply(new):
            if _card_meaning(old) != _card_meaning(new):
                # F4：已可见结果卡的正文 / 来源变化 → result_updated（同样带正文与来源）。
                expressions.append(
                    _expr(
                        "result_updated",
                        card_ids=[cid],
                        summary=f"可见结果卡内容变化：{card_label(new)}",
                        content=str(new.get("content") or ""),
                        source=_reply_source(new),
                        updated_at=new.get("updatedAt"),
                    )
                )
            elif _card_layout(old) != _card_layout(new):
                expressions.append(
                    _expr(
                        "layout_only",
                        card_ids=[cid],
                        summary=f"普通移动 / 缩放（只影响显示）：{card_label(new)}",
                    )
                )
            continue
        if _card_meaning(old) != _card_meaning(new):
            if old.get("kind") == "text" and new.get("kind") == "text":
                expressions.append(
                    _expr("note_edited", card_ids=[cid], summary=f"修改注释：{card_label(new)}")
                )
            else:
                # 材料没有「编辑」语义：旧内容撤回 + 新内容添加，别把它说成改了同一份材料
                expressions.append(
                    _expr(
                        "material_removed",
                        card_ids=[cid],
                        summary=f"材料内容变化，撤回原内容（{new.get('kind')}）：{card_label(old)}",
                    )
                )
                expressions.append(
                    _expr(
                        "material_added",
                        card_ids=[cid],
                        summary=f"材料内容变化，添加新内容（{new.get('kind')}）：{card_label(new)}",
                    )
                )
        elif _card_layout(old) != _card_layout(new):
            expressions.append(
                _expr(
                    "layout_only",
                    card_ids=[cid],
                    summary=f"普通移动 / 缩放（只影响显示）：{card_label(new)}",
                )
            )

    # 2) 关系链接：按两端配对，避免「删了再加」被说成含义变化
    b_links: dict[tuple, dict] = {}
    for link in before.get("links", []):
        b_links.setdefault(_link_key(link), link)
    a_links: dict[tuple, dict] = {}
    for link in after.get("links", []):
        a_links.setdefault(_link_key(link), link)
    for key in sorted(a_links.keys() - b_links.keys()):
        link = a_links[key]
        expressions.append(
            _expr(
                "link_added",
                card_ids=[str(link.get("src")), str(link.get("dst"))],
                link_id=link.get("id"),
                summary=f"新增关系：{_link_text(link, a_cards)}",
            )
        )
    for key in sorted(b_links.keys() - a_links.keys()):
        link = b_links[key]
        expressions.append(
            _expr(
                "link_removed",
                card_ids=[str(link.get("src")), str(link.get("dst"))],
                link_id=link.get("id"),
                summary=f"移除关系：{_link_text(link, b_cards)}",
            )
        )
    for key in sorted(a_links.keys() & b_links.keys()):
        old, new = b_links[key], a_links[key]
        same = (str(old.get("meaning") or ""), bool(old.get("direction"))) == (
            str(new.get("meaning") or ""),
            bool(new.get("direction")),
        )
        if not same:
            expressions.append(
                _expr(
                    "link_meaning_changed",
                    card_ids=[str(new.get("src")), str(new.get("dst"))],
                    link_id=new.get("id"),
                    summary=(
                        f"修改关系含义：{_link_text(old, b_cards)} → {_link_text(new, a_cards)}"
                    ),
                )
            )

    # 3) 组：成形 / 合并 / 解散 / 改名 / 有序 / 成员 / 顺序
    merged_from: set[str] = set()
    handled_old: set[str] = set()
    for gid in sorted(a_groups.keys() - b_groups.keys()):
        group = a_groups[gid]
        members = [str(m) for m in group.get("members", [])]
        prior = {
            old_gid
            for old_gid, old_group in b_groups.items()
            if any(m in old_group.get("members", []) for m in members)
        }
        if len(prior) >= 2 and not (prior & set(a_groups)):
            merged_from.add(gid)
            handled_old |= prior
            expressions.append(
                _expr(
                    "group_merged",
                    card_ids=members,
                    group_id=gid,
                    summary=f"两个组合并为「{group.get('name')}」（{len(members)} 项）",
                )
            )
        else:
            expressions.append(
                _expr(
                    "group_formed",
                    card_ids=members,
                    group_id=gid,
                    summary=f"形成组「{group.get('name')}」（{len(members)} 项）",
                )
            )
    for gid in sorted(b_groups.keys() - a_groups.keys()):
        if gid in handled_old:
            continue
        group = b_groups[gid]
        members = [str(m) for m in group.get("members", [])]
        kept = [m for m in members if m in a_cards]
        expressions.append(
            _expr(
                "group_membership_changed",
                card_ids=members,
                summary=(
                    f"组「{group.get('name')}」已解散"
                    f"（{len(kept)} 项仍在板面，{len(members) - len(kept)} 项已撤回）"
                ),
            )
        )
    for gid in sorted(a_groups.keys() & b_groups.keys()):
        old, new = b_groups[gid], a_groups[gid]
        old_name = str(old.get("name") or "")
        new_name = str(new.get("name") or "")
        old_members = [str(m) for m in old.get("members", [])]
        new_members = [str(m) for m in new.get("members", [])]
        if old_name != new_name:
            expressions.append(
                _expr(
                    "group_renamed",
                    card_ids=new_members,
                    group_id=gid,
                    summary=f"组改名：「{old_name}」→「{new_name}」",
                )
            )
        if bool(old.get("ordered")) != bool(new.get("ordered")):
            expressions.append(
                _expr(
                    "ordered_changed",
                    card_ids=new_members,
                    group_id=gid,
                    summary=(
                        f"组「{new_name}」设为有序（按序号表达顺序）"
                        if new.get("ordered")
                        else f"组「{new_name}」设为普通（摆放不表示先后）"
                    ),
                )
            )
        if old_members != new_members:
            if set(old_members) == set(new_members) and new.get("ordered"):
                expressions.append(
                    _expr(
                        "order_changed",
                        card_ids=new_members,
                        group_id=gid,
                        summary=f"调整有序组「{new_name}」顺序（{len(new_members)} 项）",
                    )
                )
            else:
                expressions.append(
                    _expr(
                        "group_membership_changed",
                        card_ids=new_members,
                        group_id=gid,
                        summary=f"组「{new_name}」成员变化（现 {len(new_members)} 项）",
                    )
                )

    # 4) 关注范围：仍然有效的多选 / 区域选择；取消掉的选择不产生表达
    sel_before = [cid for cid in before.get("selection", []) if cid in b_cards]
    sel_after = [cid for cid in after.get("selection", []) if cid in a_cards]
    if sel_after and set(sel_after) != set(sel_before):
        expressions.append(
            _expr(
                "focus_selection",
                card_ids=sel_after,
                summary=f"本次关注范围：{len(sel_after)} 项（多选 / 区域选择）",
            )
        )
    return expressions


def content_fingerprint(snapshot: dict) -> str:
    """去重指纹：以**身份结构**为准（契约 M5 / 反例 17）。

    - 卡片沿用内容键（kind + content + meta）：「撤回后重新加回同样内容」的现状
      重复规则得以维持——同内容即重复，不因重新建了一张卡就再调用 QIO；
    - 链接端点、组成员（含顺序）与 selection 按**卡片 id** 的对象身份记录：
      正文不再替换端点身份。A、B 内容相同但身份不同时，A→C 换成 B→C
      是真实的新关系（新增 + 移除），不是重复提交；
    - 同身份同结构同正文 = duplicate；同正文不同身份 ≠ duplicate。
    """
    cards = {c["id"]: c for c in snapshot.get("cards", []) if c.get("id")}

    def card_fp(card: dict) -> str:
        return json.dumps(
            {
                "kind": card.get("kind"),
                "content": str(card.get("content") or ""),
                "meta": card.get("meta") or {},
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def endpoint(cid: Any) -> str:
        """链接端点 / 组成员的身份：**卡片 id（对象身份）**，不是正文。

        修正（Lead 集成复核）：这里原来返回 card_fp(card)（kind + content + meta），
        于是 A、B 两份**内容相同、身份不同**的材料被当成同一个端点 ——
        A→C 换成 B→C 被判 duplicate（17 的核心反例）。身份必须按 id 记录；
        正文不参与端点身份，卡片本身仍然按内容键参与（「撤回再加回同样内容」照旧去重）。
        快照里看不见的 id 也按身份如实记录，不伪装成已消失。
        """
        return f"id:{str(cid)}"

    groups = sorted(
        json.dumps(
            {
                "name": str(group.get("name") or ""),
                "ordered": bool(group.get("ordered")),
                "members": [endpoint(m) for m in group.get("members", [])],
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for group in snapshot.get("groups", [])
    )
    links = sorted(
        json.dumps(
            {
                "src": endpoint(link.get("src")),
                "dst": endpoint(link.get("dst")),
                "direction": bool(link.get("direction")),
                "meaning": str(link.get("meaning") or ""),
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for link in snapshot.get("links", [])
    )
    selection = sorted(endpoint(cid) for cid in snapshot.get("selection", []))
    payload = json.dumps(
        {
            "cards": sorted(card_fp(card) for card in cards.values()),
            "groups": groups,
            "links": links,
            "selection": selection,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- 基准与未提交改动 ---------------------------------------------------


def last_success_baseline(conn: sqlite3.Connection, board_id: str) -> dict | None:
    """「上次成功提交」基准：只有 succeeded 的提交会更新它。

    返回 {"submissionId", "seq", "submittedAt", "contentHash", "snapshot"}；
    没有成功提交过时返回 None（首次提交会拿到空快照 + firstSubmission）。
    """
    info = board_store.ensure_board(conn, board_id=board_id)
    row = conn.execute(
        "SELECT id, seq, after_state, content_hash, created_at FROM board_submissions"
        " WHERE board_id = ? AND status = 'succeeded' ORDER BY seq DESC LIMIT 1",
        (info["id"],),
    ).fetchone()
    if row is None:
        return None
    snapshot = models.loads(row["after_state"], None)
    if not isinstance(snapshot, dict):
        return None
    return {
        "submissionId": row["id"],
        "seq": int(row["seq"]),
        "submittedAt": row["created_at"],
        "contentHash": row["content_hash"] or "",
        "snapshot": snapshot,
    }


def refresh_pending(conn: sqlite3.Connection, board_id: str) -> dict:
    """重算并落库「未提交的有效改动」（保存后调用；纯本地求差，不调用 QIO）。"""
    loaded = board_store.load_board(conn, board_id)
    state = loaded["state"]
    baseline = last_success_baseline(conn, loaded["board"]["id"])
    expressions = diff_states(project_baseline(baseline, state), project_snapshot(state))
    stamp = models.now_iso()
    conn.execute(
        "INSERT INTO board_pending (board_id, baseline_seq, expressions, updated_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT(board_id) DO UPDATE SET"
        " baseline_seq = excluded.baseline_seq, expressions = excluded.expressions,"
        " updated_at = excluded.updated_at",
        (
            loaded["board"]["id"],
            int(baseline["seq"]) if baseline else None,
            models.dumps(expressions),
            stamp,
        ),
    )
    return {
        "baselineSeq": int(baseline["seq"]) if baseline else None,
        "expressions": expressions,
        "updatedAt": stamp,
    }


# --- 提交 ---------------------------------------------------------------


def _decide_status(baseline: dict | None, expressions: list[dict], after: dict) -> tuple[str, str | None]:
    """empty / duplicate / succeeded 的一致判定（前两种都不更新基准、不调用 QIO）。

    判定顺序按契约 §1.5：**先看内容是否与上次成功提交一致（duplicate），再看有没有
    可提交内容（empty）**。纯位置变化（layout_only）后内容与上次成功提交一致，必须
    判 duplicate 而不是 empty —— 两种状态的安全语义相同（不更新基准、不调用模型），
    但前端文案与状态机含义必须准确。
    """
    fingerprint = content_fingerprint(after)
    if baseline:
        # 与「上次成功提交内容」比较时对两边都重算身份指纹：
        # 基准行里的 content_hash 可能是旧方案（内容指纹），旧基准无缝兼容
        snapshot = baseline.get("snapshot")
        if isinstance(snapshot, dict) and snapshot:
            if fingerprint == content_fingerprint(snapshot):
                return "duplicate", "与上次成功提交内容一致，未重复提交、未更新基准"
        elif baseline.get("contentHash") and baseline["contentHash"] == fingerprint:
            return "duplicate", "与上次成功提交内容一致，未重复提交、未更新基准"
    # 首次提交（baseline=None）不得判成 duplicate；没有内容变化才是 empty
    content = [e for e in expressions if e.get("kind") not in DISPLAY_ONLY_EXPRESSIONS]
    if not content:
        return (
            "empty",
            "没有可提交内容：本次没有允许查看的内容，或只有普通移动 / 缩放这类不构成表达的变化",
        )
    return "succeeded", None


# --- 提交请求身份（契约 §1.1 / §1.2） ------------------------------------


def normalize_client_request_id(value: Any) -> str:
    """把客户端传来的提交请求身份规范成可落库的字符串；非法直接拒绝。

    - None / 缺失 / 空白 → ""（旧客户端：不参与幂等，行为与今天一致）；
    - 必须是字符串（数字 / 布尔一律 400 级错误，不悄悄转成字符串）；
    - 去掉首尾空白，长度 1..MAX_CLIENT_REQUEST_ID。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("clientRequestId 必须是字符串")
    if not value.strip():
        # 空 / 全空白 = 旧客户端：不参与幂等（也不落身份）
        return ""
    if len(value) > MAX_CLIENT_REQUEST_ID:
        # 只拒绝，不截断：身份必须**原样**保存与回显（前端逐字比较）
        raise ValueError(f"clientRequestId 过长（最多 {MAX_CLIENT_REQUEST_ID} 字符）")
    return value


#: 客户端冻结视图（F6）的字段：板面坐标下可见区域的左上角 + 尺寸 + 缩放。
VIEW_FIELDS = ("x", "y", "w", "h", "scale")


def normalize_view(value: Any) -> dict | None:
    """校验客户端的冻结视图（F6）。非法 / 缺失一律返回 None —— 不编造。

    口径（与 Lead 冻结约定一致）：x/y/w/h/scale 必须都是**有限数**，且 w>0、h>0、scale>0；
    任何一项不满足就整块丢弃。单位是板面坐标（board units），原点在板面左上。
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for key in VIEW_FIELDS:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        number = float(raw)
        if not math.isfinite(number):
            return None
        out[key] = number
    if out["w"] <= 0 or out["h"] <= 0 or out["scale"] <= 0:
        return None
    return out


def _raw_note(note: Any) -> str:
    """提交请求里的 note，**原样**（不改写、不截断、不规范化）。"""
    return note if isinstance(note, str) else ""


def _display_note(note: Any) -> str:
    """给用户看的备注摘要（只进 delivery.detail 的展示文本，不作为身份依据）。"""
    return " ".join(str(note or "").split())[:200]


def _note_from_delivery(delivery: Any) -> str:
    """从提交记录的 delivery 里取回本次提交的备注（**原样**）。

    新记录显式带 delivery["note"]（提交时原样落库）：直接原样返回 —— 前端逐字比较，
    不许规范化 / 截断 / 改写。旧记录没有它时才从 detail 的「用户备注：」段取回，
    取不到返回 ""（不猜、不编）。
    """
    data = delivery if isinstance(delivery, dict) else {}
    note = data.get("note")
    if isinstance(note, str):
        return note
    detail = data.get("detail")
    if isinstance(detail, str) and "用户备注：" in detail:
        tail = detail.rsplit("用户备注：", 1)[1]
        return tail.split("；", 1)[0].strip()[:200]
    return ""


def _delivery_facts(delivery: Any, status: str) -> dict:
    """把记录里的 delivery 规范成契约 §1.2 的冻结形状（缺的字段如实补 None，不编造）。

    既有的 delivered / status / understandingId / reason / detail 原样透出；背景调度
    这类额外事实（background / marking）也一并保留（前端按需读取，只增不改）。
    duplicate / empty 这类没有投递状态的历史记录如实表成 not_sent，不伪装成 accepted。
    """
    data = dict(delivery) if isinstance(delivery, dict) else {}
    fallback_status = "accepted" if status == "succeeded" else "not_sent"
    normalized = {
        "delivered": bool(data.get("delivered", status == "succeeded")),
        "status": str(data.get("status") or fallback_status),
        "understandingId": data.get("understandingId"),
        "reason": data.get("reason"),
        "detail": data.get("detail"),
    }
    for key, value in data.items():
        normalized.setdefault(key, value)
    return normalized


def _understanding_facts(
    conn: sqlite3.Connection, *, submission_id: str
) -> tuple[str | None, str | None]:
    """这次提交的理解请求事实 (understandingId, understandingStatus)。

    身份口径与 understanding.request_id_for 一致（"sub:<boardId>:<submissionId>" 的
    submission_id 部分）：按 submission_id 取一条，读不到就如实返回 (None, None) ——
    不推断、不拿「最近一条理解记录」冒充受理证据（契约 §1.1 第 4 条）。
    """
    try:
        row = conn.execute(
            "SELECT id, status FROM board_understanding WHERE submission_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (str(submission_id),),
        ).fetchone()
    except sqlite3.Error:
        return None, None
    if row is None:
        return None, None
    return str(row["id"]), str(row["status"])


def _load_base_seq(loaded: dict) -> int:
    """这次操作**基于的**服务端板面版本。

    load_board 在加载时可能顺手做一次**表示层**迁移（旧 code / url → 文字卡，契约 §2.2）：
    迁移只改内容的表示、不改用户的语义内容，所以这次操作仍然基于迁移前的那个版本。
    否则「刚保存完就来提交」会被系统自己的迁移判成过期（会误导用户以为版本冲突）。
    """
    migrated_from = loaded.get("migratedFromSeq")
    if isinstance(migrated_from, int) and not isinstance(migrated_from, bool):
        return migrated_from
    return int(loaded.get("seq") or 0)


def _base_state_version(row) -> int | None:
    """这条提交记录**基于的板面版本**（契约 §1.2 acceptance.baseStateVersion）。

    真相在提交记录里，不需要新列：

    - `baseline.seq` 是这次提交写下的板面版本（成功提交 = 本次基于的版本；
      empty / duplicate / failed 也带上它，便于事后查证）；
    - 缺它时（本列之前写下的旧记录）退回读 after_state 里的 seq；再没有就如实 None ——
      不拿「最近一次成功提交的版本」冒充（那会把另一次提交的版本安到这一条上）。
    """
    baseline = models.loads(row["baseline"], {})
    if isinstance(baseline, dict):
        raw = baseline.get("baseStateVersion")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
        # 旧记录：只有成功提交的 baseline.seq 与「本次基于的版本」同义
        if str(row["status"]) == "succeeded":
            legacy = baseline.get("seq")
            if isinstance(legacy, int) and not isinstance(legacy, bool):
                return legacy
    return board_store.parse_state_seq(models.loads(row["after_state"], {}))


def _client_request_id_row(conn: sqlite3.Connection, board_id: str, client_request_id: str):
    """按**精确身份**取原始提交记录；查不到返回 None。

    只按 (board_id, client_request_id) 精确匹配：时间接近、最近一条、板面出现新任务
    一律不作为证据。空身份直接 None（旧客户端不参与幂等）。
    """
    if not client_request_id:
        return None
    try:
        return conn.execute(
            "SELECT * FROM board_submissions WHERE board_id = ? AND client_request_id = ?"
            " ORDER BY seq LIMIT 1",
            (board_id, client_request_id),
        ).fetchone()
    except sqlite3.Error:
        return None


def acceptance_lookup(
    conn: sqlite3.Connection, board_id: str, client_request_id: str
) -> dict | None:
    """按**精确身份**查这次提交请求的事实（契约 §1.2）；查不到返回 None。

    冻结形状（D/A 都按它写断言）：

        {clientRequestId, submissionId, status, delivery{delivered, status,
         understandingId, reason, detail}, understandingId, understandingStatus,
         baseStateVersion, note, createdAt, replayed}

    **只认精确匹配**：时间接近、最近一条理解记录、板面出现新任务都不能单独作为受理
    证据；查不到就返回 None，由调用方如实回「结果未知」，绝不推导成功。
    本函数只读：不写库、不落新行、不调用模型。
    """
    try:
        bid = board_store.ensure_board(conn, board_id=board_id)["id"]
    except Exception:  # noqa: BLE001 - 板面 id 非法 / 库不可用都按「查不到」处理
        return None
    wanted = normalize_client_request_id(client_request_id)
    if not wanted:
        return None
    row = _client_request_id_row(conn, bid, wanted)
    if row is None:
        return None
    delivery = _delivery_facts(models.loads(row["delivery"], {}), str(row["status"]))
    understanding_id, understanding_status = _understanding_facts(conn, submission_id=row["id"])
    return {
        # 原样回显落库的身份（提交时就是原样保存的；前端逐字比较，绝不规范化 / 截断）
        "clientRequestId": str(row["client_request_id"]),
        "submissionId": row["id"],
        "status": str(row["status"]),
        "delivery": delivery,
        "understandingId": understanding_id,
        "understandingStatus": understanding_status,
        "baseStateVersion": _base_state_version(row),
        "note": _note_from_delivery(delivery),
        "createdAt": row["created_at"],
        # 查证动作本身不重放提交：这里永远是 False（重放事实由提交响应里的 replayed 表达）
        "replayed": False,
    }


def _submission_facts(conn: sqlite3.Connection, row) -> dict:
    """把一条提交记录转成「原记录事实」（重放响应的唯一读侧实现）。"""

    def _json(value: Any, fallback: Any) -> Any:
        loaded = models.loads(value, fallback)
        return loaded if loaded is not None else fallback

    status = str(row["status"])
    delivery = _json(row["delivery"], {})
    understanding_id, understanding_status = _understanding_facts(conn, submission_id=row["id"])
    return {
        "submissionId": row["id"],
        "seq": int(row["seq"]),
        "status": status,
        "before": _json(row["before_state"], {}),
        "after": _json(row["after_state"], {}),
        "visible": _json(row["visible"], {}),
        "expressions": _json(row["expressions"], []),
        "baseline": _json(row["baseline"], {}),
        "delivery": _delivery_facts(delivery, status),
        "error": row["error"],
        "createdAt": row["created_at"],
        "baseStateVersion": _base_state_version(row),
        "note": _note_from_delivery(delivery),
        "understandingId": understanding_id,
        "understandingStatus": understanding_status,
    }


def _checked_cleared_at(conn: sqlite3.Connection, board_id: str, seq: int) -> list[str]:
    """历史字段：现在**恒为空数组**（契约 §2.2）。

    旧行为是「提交成功后自动取消勾选」；持续许可生效后，提交不再改动任何勾选 ——
    这里保留函数与参数只是为了不改调用点与旧回执形状，绝不再从快照里重算「被清理的勾选」。
    """
    return []


def replay_response(conn: sqlite3.Connection, row, *, board_id: str) -> dict:
    """同身份重试的响应：只返回**原记录事实**，不落新行、不新建理解请求、不调模型。

    与原提交响应同形状（status / submission / before / after / expressions / baseline /
    delivery / visibleRange / checkedCleared），但 status、delivery、submission 全部来自
    落库的原记录，并带 `replayed: true` 让调用方（与前端）区分「重放」与「新受理」。
    重放**不重放副作用**，也不再改动勾选：许可持续有效（契约 §2），
    所以 checkedCleared 在直连与重放两条路径上都**恒为空数组**。
    """
    facts = _submission_facts(conn, row)
    status = str(facts["status"])
    submission_seq = int(facts["seq"])
    baseline_info = facts["baseline"] if isinstance(facts["baseline"], dict) else {}
    previous_seq = baseline_info.get("previousSeq")
    if previous_seq is None:
        prior = conn.execute(
            "SELECT seq FROM board_submissions WHERE board_id = ? AND status = 'succeeded'"
            " AND seq < ? ORDER BY seq DESC LIMIT 1",
            (board_id, submission_seq),
        ).fetchone()
        previous_seq = int(prior["seq"]) if prior is not None else None
    before = facts["before"]
    if status == "succeeded":
        baseline_info = {
            "updated": True,
            "firstSubmission": previous_seq is None,
            "previousSeq": previous_seq,
            "seq": submission_seq,
        }
        # 前后状态复用同一份求差实现（与 understanding 侧「按 submission_id 补读」同口径）：
        # 重放时板面可能已经前进，所以 before 按**当时那次提交**的基准还原。
        try:
            snapshot = conn.execute(
                "SELECT state FROM board_state_snapshots WHERE board_id = ? AND seq = ? LIMIT 1",
                (board_id, submission_seq - 1),
            ).fetchone()
            if snapshot is not None:
                state = models.loads(snapshot["state"], {})
                if isinstance(state, dict):
                    before = project_baseline(last_success_baseline(conn, board_id), state)
        except Exception:  # noqa: BLE001 - 还原不出来时仍旧返回原记录里的 before
            before = facts["before"]
    baseline_seq = facts["baseStateVersion"]
    return {
        "status": status,
        "submission": {
            "id": str(facts["submissionId"]),
            "seq": submission_seq,
            "status": status,
            "visible": facts["visible"],
            "expressions": facts["expressions"],
            "delivery": facts["delivery"],
            "error": facts["error"],
            "createdAt": facts["createdAt"],
            "clientRequestId": str(row["client_request_id"]),
        },
        "before": before,
        "after": facts["after"],
        "expressions": facts["expressions"],
        "baseline": baseline_info,
        "delivery": facts["delivery"],
        "visibleRange": facts["visible"],
        # 阶段三：重放也如实回原记录的主循环 turnId 与当前等待快照（不重新受理）
        "turnId": (
            facts["delivery"].get("turnId")
            if isinstance(facts["delivery"], dict)
            else None
        ),
        "waiting": _waiting_state_safe(conn, board_id),
        # 契约 §2.2：提交不再取消勾选，重放也绝不复活 / 清除任何许可 —— 恒为空数组
        "checkedCleared": _checked_cleared_at(conn, board_id, baseline_seq)
        if isinstance(baseline_seq, int)
        else [],
        "replayed": True,
    }


def _conflict_reason(conn: sqlite3.Connection, row, *, note: str, base_state_version: Any) -> str | None:
    """同身份但内容不一致的可读原因；一致返回 None（= 重试，可以重放原事实）。

    只比契约写明的两件事：base_state_version 与 note。身份内出现这两种偏差都**不能**
    当成同一次提交的受理结果（不得把另一个窗口的内容混进来）。
    """
    stored_base = _base_state_version(row)
    wanted_base = None
    if base_state_version is not None:
        try:
            wanted_base = int(base_state_version)
        except (TypeError, ValueError):
            wanted_base = None
    stored_note = _note_from_delivery(models.loads(row["delivery"], {}))
    identity = str(row["client_request_id"])
    if wanted_base != stored_base:
        return (
            f"这个提交请求身份（{identity}）已经对应一次基于板面版本 {stored_base} 的提交，"
            f"这次却声明基于版本 {wanted_base}：内容不一致，已拒绝"
            "（没有落新提交、没有改基准、没有调用模型）。"
        )
    if _note_from_delivery({"note": note}) != stored_note:
        return (
            f"这个提交请求身份（{identity}）已经对应一次备注为「{stored_note}」的提交，"
            f"这次却是「{note}」：内容不一致，已拒绝"
            "（没有落新提交、没有改基准、没有调用模型）。"
        )
    return None


def _next_submission_seq(conn: sqlite3.Connection, board_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS top FROM board_submissions WHERE board_id = ?",
        (board_id,),
    ).fetchone()
    return int(row["top"]) + 1


def _record_submission(
    conn: sqlite3.Connection,
    *,
    submission_id: str,
    board_id: str,
    seq: int,
    status: str,
    before: dict,
    after: dict,
    visible: dict,
    expressions: list[dict],
    baseline: dict,
    delivery: dict,
    content_hash: str,
    error: str | None,
    client_request_id: str = "",
) -> dict:
    stamp = models.now_iso()
    conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state,"
        " visible, expressions, baseline, delivery, content_hash, error, created_at,"
        " client_request_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            submission_id,
            board_id,
            seq,
            status,
            models.dumps(before),
            models.dumps(after),
            models.dumps(visible),
            models.dumps(expressions),
            models.dumps(baseline),
            models.dumps(delivery),
            content_hash,
            error,
            stamp,
            client_request_id,
        ),
    )
    return {
        "id": submission_id,
        "seq": seq,
        "status": status,
        "visible": visible,
        "expressions": expressions,
        "delivery": delivery,
        "error": error,
        "createdAt": stamp,
    }


# --- 等待 / 续作持久化（阶段三契约 §13.3；迁移 49） ------------------------
#
# 互动提交现在是主循环的一轮：QIO 在对话里问出的问题（board_wait_for_input 的参数）
# 必须**持久保存**，用户下一次发给该板面的聊天消息据此自动续作原提交。
# 这些函数是 Lead 的轮末接线、B 的 board_finish_intent 完成核对、C 的只读 API
# 共用的稳定入口；读函数只读，写函数复用外层事务（storage.db.transaction 语义）。

MODE_NORMAL = "normal"
MODE_INTERACTIVE = "interactive"

WAITING_STATUSES: tuple[str, ...] = ("waiting", "resolved", "cancelled")

#: 一个板面最多展示 / 读取的等待事项（有界；防止异常数据把响应撑爆）。
MAX_WAITING_ITEMS = 50

#: 自动续作时视为「已确认、需要继续执行」的意图状态（与 tool_models.INTENT_STATUSES 一致）。
_RESUME_INTENT_STATUSES: tuple[str, ...] = (
    "approved",
    "running",
    "paused",
    "partially_completed",
)

_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _json_list(value: Any) -> list[str]:
    loaded = models.loads(value, [])
    if not isinstance(loaded, (list, tuple)):
        return []
    return [str(item) for item in loaded if str(item).strip()]


def _question_key(question: Any) -> str:
    return " ".join(str(question or "").split())


def _waiting_row_dict(row) -> dict:
    return {
        "id": str(row["id"]),
        "boardId": str(row["board_id"]),
        "submissionId": str(row["submission_id"]),
        "turnId": str(row["turn_id"] or ""),
        "question": str(row["question"]),
        "missingFields": _json_list(row["missing_fields"]),
        "status": str(row["status"]),
        "resolvedQuestionIds": _json_list(row["resolved_question_ids"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _waiting_rows(
    conn: sqlite3.Connection,
    board_id: str,
    *,
    submission_id: str | None = None,
    status: str | None = "waiting",
) -> list:
    sql = "SELECT * FROM board_waiting WHERE board_id = ?"
    params: list[Any] = [board_id]
    if submission_id:
        sql += " AND submission_id = ?"
        params.append(submission_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at, rowid LIMIT ?"
    params.append(MAX_WAITING_ITEMS)
    return conn.execute(sql, params).fetchall()


def _submission_row(conn: sqlite3.Connection, submission_id: str):
    return conn.execute(
        "SELECT id, board_id, turn_id, resume_state, created_at FROM board_submissions"
        " WHERE id = ?",
        (submission_id,),
    ).fetchone()


def record_waiting(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    submission_id: str,
    question: str,
    missing_fields: Sequence[str] = (),
    turn_id: str = "",
) -> dict:
    """持久记录一条 QIO 在对话里提出的等待问题（同一事项不重复记录）。

    * 同一 submission + 同一问题正文且仍在 waiting → 幂等返回已有项（created=False），
      「已等待回答的同一事项不重复提出」；
    * 空 board / 空 submission / 空问题 → ValueError（调用方是程序接线，不猜）；
    * 提交必须属于这个板面（不接受伪造关联）。

    返回冻结形状：{id, boardId, submissionId, turnId, question, missingFields,
    status, resolvedQuestionIds, createdAt, updatedAt, created}。
    """
    bid = str(board_id or "").strip()
    sid = str(submission_id or "").strip()
    text = str(question or "").strip()
    if not bid or not sid or not text:
        raise ValueError("record_waiting 需要非空的 board_id / submission_id / question")
    row = _submission_row(conn, sid)
    if row is None:
        raise ValueError(f"没有这条提交记录：{sid}")
    if str(row["board_id"]) != bid:
        raise ValueError(f"提交 {sid} 不属于板面 {bid}")
    fields = [str(item).strip() for item in (missing_fields or []) if str(item).strip()]
    stamp = models.now_iso()
    with transaction(conn):
        existing_rows = _waiting_rows(conn, bid, submission_id=sid)
        wanted = _question_key(text)
        for existing in existing_rows:
            if _question_key(existing["question"]) == wanted:
                item = _waiting_row_dict(existing)
                item["created"] = False
                return item
        item_id = models.new_id("wait")
        conn.execute(
            "INSERT INTO board_waiting (id, board_id, submission_id, turn_id, question,"
            " missing_fields, status, resolved_question_ids, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'waiting', '[]', ?, ?)",
            (
                item_id,
                bid,
                sid,
                str(turn_id or ""),
                text,
                models.dumps(fields),
                stamp,
                stamp,
            ),
        )
        created = conn.execute("SELECT * FROM board_waiting WHERE id = ?", (item_id,)).fetchone()
    item = _waiting_row_dict(created)
    item["created"] = True
    return item


def resolve_waiting(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    submission_id: str,
    question_ids: Sequence[str] = (),
) -> int:
    """按 QIO 明确给出的 question id 解除等待；返回实际解除的条数。

    空 question_ids = 不解除任何事项（回答不足 / 属于别的事项时必须保留等待）；
    重复调用幂等（已 resolved 的行不再计数）。question id 就是 board_waiting.id。
    """
    bid = str(board_id or "").strip()
    sid = str(submission_id or "").strip()
    wanted = [str(item).strip() for item in (question_ids or []) if str(item).strip()]
    if not bid or not sid or not wanted:
        return 0
    unique = list(dict.fromkeys(wanted))
    resolved = 0
    stamp = models.now_iso()
    with transaction(conn):
        rows = conn.execute(
            "SELECT id, resolved_question_ids FROM board_waiting"
            " WHERE board_id = ? AND submission_id = ? AND status = 'waiting'"
            " AND id IN (" + ", ".join("?" for _ in unique) + ")",
            [bid, sid, *unique],
        ).fetchall()
        for row in rows:
            merged = _json_list(row["resolved_question_ids"])
            for item_id in unique:
                if str(row["id"]) == item_id and item_id not in merged:
                    merged.append(item_id)
            cursor = conn.execute(
                "UPDATE board_waiting SET status = 'resolved', resolved_question_ids = ?,"
                " updated_at = ? WHERE id = ? AND status = 'waiting'",
                (models.dumps(merged), stamp, str(row["id"])),
            )
            resolved += int(cursor.rowcount or 0)
    return resolved


def get_waiting(
    conn: sqlite3.Connection, board_id: str, *, submission_id: str | None = None
) -> dict | None:
    """只读查询未解除的等待事项；没有等待返回 None。

    B 的 board_finish_intent 用 get_waiting(conn, board_id) is None 判「无等待」；
    数据库 / 表不可用时**如实抛出 sqlite3.Error**（绝不伪装成「没有等待」）。
    返回 {boardId, submissionId(最新一条), submissionIds, items, questionIds, count,
    updatedAt}；items 按 created_at 升序，每项字段同 record_waiting。
    """
    bid = str(board_id or "").strip()
    if not bid:
        return None
    wanted_sid = str(submission_id or "").strip() or None
    rows = _waiting_rows(conn, bid, submission_id=wanted_sid)
    if not rows:
        return None
    items = [_waiting_row_dict(row) for row in rows]
    submission_ids: list[str] = []
    for item in items:
        if item["submissionId"] not in submission_ids:
            submission_ids.append(item["submissionId"])
    latest = items[-1]["submissionId"]
    return {
        "boardId": bid,
        "submissionId": latest,
        "submissionIds": submission_ids,
        "items": items,
        "questionIds": [item["id"] for item in items],
        "count": len(items),
        "updatedAt": max(str(item["updatedAt"] or "") for item in items),
    }


def waiting_state(conn: sqlite3.Connection, board_id: str) -> dict:
    """等待状态的稳定只读视图（永远返回 dict，供 C 的 API / 状态展示读取）。

    字段：{boardId, waiting, count, items, questionIds, submissionIds,
    latestSubmissionId, updatedAt}。数据库错误如实抛出（由调用方按自身 API 语义处理）。
    """
    bid = str(board_id or "").strip()
    empty = {
        "boardId": bid,
        "waiting": False,
        "count": 0,
        "items": [],
        "questionIds": [],
        "submissionIds": [],
        "latestSubmissionId": None,
        "updatedAt": None,
    }
    if not bid:
        return empty
    state = get_waiting(conn, bid)
    if state is None:
        return empty
    return {
        "boardId": bid,
        "waiting": True,
        "count": state["count"],
        "items": state["items"],
        "questionIds": state["questionIds"],
        "submissionIds": state["submissionIds"],
        "latestSubmissionId": state["submissionId"],
        "updatedAt": state["updatedAt"],
    }


def bind_submission_turn(
    conn: sqlite3.Connection,
    *,
    submission_id: str,
    turn_id: str,
    resume_state: Mapping[str, Any] | None = None,
) -> dict:
    """把一条提交绑定到主循环 turn（幂等；保留历次绑定历史）。

    resume_state 会与已有状态合并（只增 / 覆盖同名键），典型键：topicId（原话题）、
    kind（submission | resume）、attempt（第几次续作）。返回 {submissionId, boardId,
    turnId, resumeState, createdAt, updatedAt}。
    """
    sid = str(submission_id or "").strip()
    tid = str(turn_id or "").strip()
    if not sid or not tid:
        raise ValueError("bind_submission_turn 需要非空的 submission_id / turn_id")
    row = _submission_row(conn, sid)
    if row is None:
        raise ValueError(f"没有这条提交记录：{sid}")
    stored = models.loads(row["resume_state"], {})
    merged: dict[str, Any] = dict(stored) if isinstance(stored, dict) else {}
    if resume_state is not None:
        if not isinstance(resume_state, Mapping):
            raise ValueError("resume_state 必须是对象")
        merged.update({str(key): value for key, value in resume_state.items()})
    stamp = models.now_iso()
    with transaction(conn):
        conn.execute(
            "UPDATE board_submissions SET turn_id = ?, resume_state = ? WHERE id = ?",
            (tid, models.dumps(merged), sid),
        )
        conn.execute(
            "INSERT INTO board_submission_turns (submission_id, turn_id, resume_state, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(submission_id, turn_id) DO UPDATE SET"
            " resume_state = excluded.resume_state",
            (sid, tid, models.dumps(merged), stamp),
        )
        first = conn.execute(
            "SELECT MIN(created_at) AS first FROM board_submission_turns WHERE submission_id = ?",
            (sid,),
        ).fetchone()
    return {
        "submissionId": sid,
        "boardId": str(row["board_id"]),
        "turnId": tid,
        "resumeState": merged,
        "createdAt": first["first"] if first is not None else stamp,
        "updatedAt": stamp,
    }


def submission_binding(conn: sqlite3.Connection, submission_id: str) -> dict | None:
    """只读：一条提交当前的绑定事实（最近 turn / 续作状态 / 历次轮次）。"""
    sid = str(submission_id or "").strip()
    if not sid:
        return None
    row = _submission_row(conn, sid)
    if row is None:
        return None
    stored = models.loads(row["resume_state"], {})
    resume_state = dict(stored) if isinstance(stored, dict) else {}
    turns = conn.execute(
        "SELECT turn_id, resume_state, created_at FROM board_submission_turns"
        " WHERE submission_id = ? ORDER BY created_at, rowid",
        (sid,),
    ).fetchall()
    history = []
    for item in turns:
        state = models.loads(item["resume_state"], {})
        history.append(
            {
                "turnId": str(item["turn_id"]),
                "resumeState": dict(state) if isinstance(state, dict) else {},
                "createdAt": item["created_at"],
            }
        )
    return {
        "submissionId": sid,
        "boardId": str(row["board_id"]),
        "turnId": str(row["turn_id"] or ""),
        "resumeState": resume_state,
        "turns": history,
        "createdAt": row["created_at"],
        "updatedAt": history[-1]["createdAt"] if history else row["created_at"],
    }


def resolve_chat_binding(
    conn: sqlite3.Connection,
    *,
    mode: Any,
    board_id: Any = None,
    submission_id: Any = None,
    current_topic: str | None = None,
) -> dict:
    """校验聊天发送请求里的互动字段，解析出可信的 TurnContext 绑定事实。

    * normal（或缺省）：不带 boardId / submissionId；带了就拒绝（不许伪造身份）；
    * interactive：boardId 必须存在；submissionId 可选且必须属于该板面；
      没有 submissionId 但板面有等待中的提交 → 自动续作该提交（resume=True），
      resolved_question_ids = 该提交当前等待的 question id（程序从 waiting 恢复）；
    * 模型不能通过这里传入别的板面：所有身份都从服务端库核对。
    非法输入抛 ValueError（路由映射 400）；数据库错误如实抛出。
    """
    raw_mode = MODE_NORMAL if mode is None or str(mode).strip() == "" else str(mode).strip()
    if raw_mode not in (MODE_NORMAL, MODE_INTERACTIVE):
        raise ValueError("mode 只能是 normal 或 interactive")
    bid = "" if board_id is None else str(board_id).strip()
    wanted_sid = "" if submission_id is None else str(submission_id).strip()
    if raw_mode == MODE_NORMAL:
        if bid or wanted_sid:
            raise ValueError("普通模式不能携带 boardId / submissionId（板面身份只由互动模式使用）")
        return {
            "mode": MODE_NORMAL,
            "boardId": "",
            "submissionId": "",
            "topicId": "",
            "resume": False,
            "attempt": 1,
            "resolvedQuestionIds": (),
            "intentIds": (),
            "waiting": None,
        }
    if not bid:
        raise ValueError("互动模式必须提供 boardId")
    if not _BOARD_ID_RE.match(bid):
        raise ValueError("boardId 只能包含字母、数字与 _.-: 且长度 1..64")
    if conn.execute("SELECT 1 FROM boards WHERE id = ?", (bid,)).fetchone() is None:
        raise ValueError(f"没有这个板面：{bid}")
    sid = wanted_sid
    resume = False
    waiting: dict | None
    if sid:
        row = _submission_row(conn, sid)
        if row is None:
            raise ValueError(f"没有这条提交记录：{sid}")
        if str(row["board_id"]) != bid:
            raise ValueError(f"提交 {sid} 不属于板面 {bid}")
        resume = True
        waiting = get_waiting(conn, bid, submission_id=sid)
    else:
        waiting = get_waiting(conn, bid)
        if waiting is not None:
            sid = waiting["submissionId"]
            resume = True
    resolved: tuple[str, ...] = ()
    if sid and waiting is not None:
        resolved = tuple(
            item["id"] for item in waiting["items"] if item["submissionId"] == sid
        )
    topic = ""
    if sid:
        binding = submission_binding(conn, sid)
        if binding is not None:
            topic = str(binding["resumeState"].get("topicId") or "")
    if not topic:
        topic = str(current_topic or "")
    attempt = 1
    intents: tuple[str, ...] = ()
    if sid:
        count_row = conn.execute(
            "SELECT COUNT(*) AS n FROM board_submission_turns WHERE submission_id = ?",
            (sid,),
        ).fetchone()
        attempt = int(count_row["n"] or 0) + 1
        placeholders = ", ".join("?" for _ in _RESUME_INTENT_STATUSES)
        intent_rows = conn.execute(
            "SELECT id FROM board_intents WHERE submission_id = ? AND status IN ("
            + placeholders
            + ") ORDER BY created_at, rowid LIMIT 64",
            (sid, *_RESUME_INTENT_STATUSES),
        ).fetchall()
        intents = tuple(str(row["id"]) for row in intent_rows)
    return {
        "mode": MODE_INTERACTIVE,
        "boardId": bid,
        "submissionId": sid,
        "topicId": topic,
        "resume": resume,
        "attempt": attempt,
        "resolvedQuestionIds": resolved,
        "intentIds": intents,
        "waiting": waiting_state(conn, bid),
    }


def _waiting_state_safe(conn: sqlite3.Connection, board_id: str) -> dict:
    """提交响应里的等待快照：表未就绪时如实标 available=False，不假装「没有等待」。"""
    try:
        return waiting_state(conn, board_id)
    except sqlite3.Error as exc:  # 迁移 49 未应用 / 库不可用：响应继续，事实如实标注
        logger.warning("读取等待状态失败（等待/续作表不可用）：%s", exc)
        return {
            "boardId": str(board_id or ""),
            "waiting": None,
            "count": 0,
            "items": [],
            "questionIds": [],
            "submissionIds": [],
            "latestSubmissionId": None,
            "updatedAt": None,
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }


def submission_turn_message(
    *,
    board_id: str,
    submission_id: str,
    after: dict,
    expressions: Sequence[dict],
    note: str = "",
) -> str:
    """程序写清「这次提交是什么」的用户消息正文（不要求模型猜，也不塞整份 JSON）。"""
    cards = after.get("cards") or []
    groups = after.get("groups") or []
    links = after.get("links") or []
    kinds: dict[str, int] = {}
    for item in expressions:
        kind = str(item.get("kind") or "")
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
    summary = "、".join(f"{kind}×{count}" for kind, count in sorted(kinds.items()))
    lines = [
        f"【互动板面提交】board_id={board_id} submission_id={submission_id}",
        (
            f"本次提交范围：卡片 {len(cards)} 项、组 {len(groups)} 项、关系 {len(links)} 项；"
            f"有效改动 {len(expressions)} 项"
            + (f"（{summary}）" if summary else "")
            + "。"
        ),
    ]
    text_note = str(note or "").strip()
    if text_note:
        lines.append(f"用户备注：{text_note[:500]}")
    lines.append(
        "这是用户的一次正式互动提交（不是普通闲聊）：请用板面工具读取所需对象后处理，"
        "提出意图或向用户提出具体问题；不要猜测未提供的板面内容，也不要重复提交。"
    )
    return "\n".join(lines)


# --- 受理与后台理解（契约 §14.1 / §14.3 / §14.4） -------------------------
#
# 统一生命周期（整合轮契约 §2）：理解的后台调度必须经 AppContext.background
# （services/background.BackgroundTasks）登记，名字固定
# interactive-understanding:<understandingId>，并声明底层执行单元探针
# ——「理解记录是否已到终结状态」。关闭开始后 register 抛 BackgroundClosed，
# 调用方据此拒绝新调度并如实说明（绝不静默改成同步跑模型）。

#: 后台理解任务的强引用：任务在跑的时候必须有人持有它（否则可能被 GC 吞掉）。
#: 仅用于没有绑定应用上下文的测试兜底（裸 create_task）路径；生产路径的句柄
#: 由 AppContext.background 持有，关闭时由它统一收尾。
_BACKGROUND_TASKS: set[asyncio.Task] = set()

#: 后台任务名前缀（与 execution.py 的 interactive-execution: 同一命名空间约定）。
UNDERSTANDING_TASK_PREFIX = "interactive-understanding:"

#: 关闭 / 重启中断一次理解时写进 error 的可读原因（记录仍可从原快照重试）。
INTERRUPTED_UNDERSTANDING_ERROR = (
    "理解被执行环境的关闭 / 重启中断（没有产出任何意图）：记录仍可从保存的快照重试，"
    "不需要重读当前板面。"
)

#: 连接 → 宿主应用（AppContext / 任何持有 background 注册表的对象）。
#: 值里同时持有 conn 与 owner 的强引用：id() 复用会导致把任务登记到错误的注册表上。
_CONNECTION_OWNERS: dict[int, tuple[sqlite3.Connection, Any]] = {}


def bind_background_owner(conn: sqlite3.Connection, owner: Any) -> None:
    """把连接绑到宿主应用：之后这条连接上的理解调度都登记进 owner.background。

    由 AppContext.__init__ 调用（同一份 conn 也就是关闭时 shutdown 的那份注册表）。
    不绑（老对象 / 单元测试裸连接）时走测试兜底路径；生产路径必须绑。
    """
    _CONNECTION_OWNERS[id(conn)] = (conn, owner)


def background_owner_for(conn: sqlite3.Connection) -> Any | None:
    """取这条连接绑定的宿主应用；没绑（或连接已变）时返回 None。"""
    entry = _CONNECTION_OWNERS.get(id(conn))
    if entry is None or entry[0] is not conn:
        return None
    return entry[1]


def unbind_background_owner(conn: sqlite3.Connection) -> None:
    """解绑（关闭收尾 / 测试收尾用；正常路径不需要）。"""
    entry = _CONNECTION_OWNERS.get(id(conn))
    if entry is not None and entry[0] is conn:
        _CONNECTION_OWNERS.pop(id(conn), None)


def _understanding_is_settled(conn: sqlite3.Connection, understanding_id: str) -> bool:
    """底层执行单元探针：这条理解记录是否已经到达终结状态。

    读到终结状态（understood / needs_clarification / no_action / failed）才算这一份
    后台工作真的收尾；仍停在 accepted / understanding 时如实返回 False ——
    「asyncio 句柄被取消了」不是「底层执行单元结束了」（A05 口径）。
    读不到记录（数据库不可用 / 记录不存在）时同样返回 False：不能靠读失败假装收尾。
    """
    try:
        row = conn.execute(
            "SELECT status FROM board_understanding WHERE id = ?", (understanding_id,)
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    return str(row["status"]) in models.UNDERSTANDING_SETTLED_STATUSES


async def _no_adapter():
    """没有传入 adapter_provider 时的兜底：如实返回「当前没有可用模型」。

    understanding.run_understanding 拿到 None 必须给出可读失败原因（不抛异常）；
    这里只保证形状正确 —— A 侧绝不自己拿凭据、绝不绕过 QIO 既有的用量 / 预算链路。
    """
    return None


def _load_understanding():
    """延迟 import B 的理解模块：不存在时返回 (None, 可读原因)，绝不抛给调用方。

    只管「模块在不在」；具体有没有 request_understanding / run_understanding 由各自的
    调用点判定（受理与后台执行是两条不同的路径）。
    """
    try:
        from agent.interactive import understanding  # 延迟 import：B 的模块
    except Exception as exc:  # noqa: BLE001 - 模块缺失 / 加载失败都是「受理不可用」
        return None, f"理解模块不可用（agent.interactive.understanding）：{type(exc).__name__}: {exc}"
    return understanding, None


def _current_attempt(conn: sqlite3.Connection, understanding_id: str) -> int | None:
    """读这条理解当前的 attempt（迟到结果护栏的基线）；读不到返回 None。"""
    try:
        row = conn.execute(
            "SELECT attempt FROM board_understanding WHERE id = ?", (understanding_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return int(row["attempt"] or 1)


def _is_current_attempt(
    conn: sqlite3.Connection, understanding_id: str, baseline: int | None
) -> bool:
    """这条理解记录是否仍是「这一次运行」的那一份（attempt 没被别人接手）。

    baseline 读不到（记录不可用）时返回 False：宁可什么都不写，也不能把迟到的
    结果写进一条来路不明的记录。旧实例 / 旧 attempt 的迟到结果因此不得改写当前
    记录；意图生成发生在理解模块内部，那一边由同一份归属核对保证。
    """
    if baseline is None:
        return False
    return _current_attempt(conn, understanding_id) == baseline


def _log_discarded(understanding_id: str, why: str) -> None:
    logger.warning("互动理解的结果被丢弃（%s）：%s", why, understanding_id)


def _schedule_understanding(
    conn: sqlite3.Connection,
    *,
    understanding_id: str,
    adapter_provider: Any,
    owner: Any = None,
) -> dict:
    """把真实理解放到后台跑：不阻塞请求，也不持有任何事务（模型调用在任务里）。

    生产路径（契约 §2）：

    * 经 owner 的 background 注册表（AppContext.background / BackgroundTasks）登记，
      名字固定 interactive-understanding:<understandingId>；
    * 声明底层执行单元探针（理解记录是否已到终结状态）：关闭时只在探针确认之后才把
      这一份算作收尾，「句柄被取消了」不算「结束了」；
    * 关闭已开始 → register 抛 BackgroundClosed → 这里拒绝新调度并如实说明，
      绝不静默改成同步跑模型（记录停在未终结状态，可从保存的快照重试）。

    没有绑定宿主应用（裸连接的单元测试）时退回裸 create_task 兜底：句柄由
    _BACKGROUND_TASKS 持有，行为与今天一致。

    返回 {"started": bool, "reason": str | None, "rejected": str | None}；
    受理之后的失败只记在理解记录上，不回改提交状态。
    """
    understanding, reason = _load_understanding()
    if understanding is None:
        return {"started": False, "reason": reason, "rejected": "unavailable"}
    runner = getattr(understanding, "run_understanding", None)
    if runner is None:
        return {
            "started": False,
            "reason": "理解模块没有 run_understanding：无法启动真实理解",
            "rejected": "unavailable",
        }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return {
            "started": False,
            "reason": "当前没有运行中的事件循环：本次没有启动后台理解（可以从保存的快照重试）",
            "rejected": "no_loop",
        }
    provider = adapter_provider if callable(adapter_provider) else _no_adapter
    if owner is None:
        # submit_board 内部直接调这个函数（那条路径没有 request 可用）：归属统一按
        # 「这条连接绑定的宿主应用」解析，生产路径（AppContext）因此一定走注册表。
        owner = background_owner_for(conn)

    # 迟到结果护栏的基线：这次运行开始时的 attempt。运行结束前被新的一次重试 /
    # 重启接手（attempt 变了）时，这一次的结果不得再写回当前记录。
    baseline_attempt = _current_attempt(conn, understanding_id)

    async def _run() -> None:
        try:
            await runner(conn, understanding_id, adapter_provider=provider)
        except asyncio.CancelledError:
            # 关闭 / 取消把协程停在这里：明确记下中断状态（可读原因 + 可重试），
            # 让底层执行单元探针随后确认为已终结；探针读不到终结状态时关闭照样报
            # 「未确认结束」，不假装收尾。写完再原样抛出，保持取消语义。
            if _is_current_attempt(conn, understanding_id, baseline_attempt):
                _mark_understanding_interrupted(conn, understanding_id)
            else:
                _log_discarded(understanding_id, "被新的一次尝试取代")
            raise
        except Exception as exc:  # noqa: BLE001 - 提交已受理：后台失败绝不改提交状态
            logger.warning("互动理解后台任务失败（提交已受理，状态不改）", exc_info=True)
            if _is_current_attempt(conn, understanding_id, baseline_attempt):
                _mark_understanding_failed(conn, understanding_id, exc)
            else:
                _log_discarded(understanding_id, "被新的一次尝试取代")

    name = UNDERSTANDING_TASK_PREFIX + str(understanding_id)
    if owner is not None:
        from agent.services.background import BackgroundClosed, registry_for

        try:
            registry = registry_for(owner, loop=loop)
            registry.register(
                name,
                _run,
                # 底层执行单元探针：理解记录是否已到终结状态（同步、零参）。
                unit_finished=lambda: _understanding_is_settled(conn, understanding_id),
            )
        except BackgroundClosed as exc:
            detail = (
                "服务正在关闭：本次没有启动后台理解，也没有在关闭流程里偷偷同步跑模型"
                f"（{exc}）"
            )
            logger.warning("互动理解调度被拒（关闭流程已开始）：%s", understanding_id)
            return {"started": False, "reason": detail, "rejected": "closed"}
        except RuntimeError as exc:
            detail = f"后台登记不可用：{exc}；本次没有启动后台理解（可以从保存的快照重试）"
            logger.warning("互动理解调度失败（后台登记不可用）：%s", understanding_id)
            return {"started": False, "reason": detail, "rejected": "no_background"}
        logger.info("互动理解已登记为后台任务：%s", name)
        return {"started": True, "reason": None, "rejected": None}

    # 测试兜底：没有绑定宿主应用时用裸 create_task（生产路径不走这条）。
    task = loop.create_task(_run(), name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"started": True, "reason": None, "rejected": None}


def schedule_understanding(
    conn: sqlite3.Connection,
    *,
    understanding_id: str,
    adapter_provider: Any = None,
    owner: Any = None,
) -> dict:
    """把一条已受理 / 待重试的理解放到后台跑（提交路径与重试路由共用同一条路径）。

    owner 省略时用这条连接绑定的宿主应用（bind_background_owner，由
    AppContext.__init__ 建立）；都没有时走测试兜底路径。
    """
    if owner is None:
        owner = background_owner_for(conn)
    return _schedule_understanding(
        conn,
        understanding_id=understanding_id,
        adapter_provider=adapter_provider,
        owner=owner,
    )


def _registered_owners() -> list[Any]:
    """所有绑定过的宿主应用（按对象身份去重，保序）。"""
    owners: list[Any] = []
    seen: set[int] = set()
    for _conn, candidate in list(_CONNECTION_OWNERS.values()):
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        owners.append(candidate)
    return owners


def cancel_background_understanding(understanding_id: str, owner: Any = None) -> bool:
    """取消正在跑的后台理解任务（已读的材料不再产出；记录状态由调用方如实落库）。

    生产路径按注册表里的句柄取消（同名任务只可能有一份：register 会去重）；
    owner 省略时在所有绑定过的宿主应用里找。测试兜底的裸任务一并处理。
    找不到正在跑的任务时如实返回 False（不假装取消成功）。
    """
    target = UNDERSTANDING_TASK_PREFIX + str(understanding_id)
    stopped = False
    candidates = [owner] if owner is not None else _registered_owners()
    for candidate in candidates:
        registry = getattr(candidate, "background", None)
        handle = registry.handle(target) if registry is not None else None
        if handle is not None and handle.cancel():
            stopped = True
    for task in list(_BACKGROUND_TASKS):
        if task.done() or task.get_name() != target:
            continue
        task.cancel()
        stopped = True
    return stopped


def _mark_understanding_interrupted(conn: sqlite3.Connection, understanding_id: str) -> bool:
    """关闭 / 重启中断了一次理解：把仍在跑的理解记录落成明确状态。

    冻结的状态集合里没有 interrupted：中断落成 failed + 可读原因（关闭 / 重启中断），
    而且绝不删快照 —— 记录于是可以从原快照重试，不需要重读当前板面。
    重复调用幂等（只动 accepted / understanding 的行）。
    """
    stamp = models.now_iso()
    try:
        cursor = conn.execute(
            "UPDATE board_understanding SET status = 'failed', error = ?, updated_at = ?,"
            " finished_at = ? WHERE id = ? AND status IN ('accepted', 'understanding')",
            (INTERRUPTED_UNDERSTANDING_ERROR, stamp, stamp, understanding_id),
        )
    except sqlite3.Error:
        logger.warning("标记理解中断时写库失败（理解记录不可用）", exc_info=True)
        return False
    if cursor.rowcount:
        logger.warning(
            "互动理解因关闭 / 重启中断，已落成 failed（可从快照重试）：%s", understanding_id
        )
        return True
    return False


def _mark_understanding_failed(
    conn: sqlite3.Connection, understanding_id: str, exc: BaseException
) -> bool:
    """兜底：理解模块抛异常时，把仍在 accepted / understanding 的理解记录标成 failed。

    只写理解记录自己的状态（失败可从保存的快照重试），绝不回改提交记录；
    理解模块自己已经落了终结状态时这里什么都不做（不抢 B 的写入）。
    """
    reason = (f"理解失败（可重试，从保存的快照恢复）：{type(exc).__name__}: {exc}")[:500]
    try:
        cursor = conn.execute(
            "UPDATE board_understanding SET status = 'failed', error = ?, updated_at = ?"
            " WHERE id = ? AND status IN ('accepted', 'understanding')",
            (reason, models.now_iso(), understanding_id),
        )
    except sqlite3.Error:
        logger.warning("标记理解失败时写库失败（理解记录不可用）", exc_info=True)
        return False
    if cursor.rowcount:
        logger.warning("互动理解已标记为 failed（%s）：%s", understanding_id, reason)
        return True
    return False

async def submit_board(
    conn: sqlite3.Connection,
    board_id: str,
    *,
    requested_visible: list[str] | None = None,
    note: str = "",
    base_state_version: Any = None,
    confirmed_check_id: str | None = None,
    adapter_provider: Any = None,
    client_request_id: str = "",
    view: Any = None,
    turn_acceptor: Callable[..., dict] | None = None,
) -> dict:
    """提交：QIO 取得表达的唯一入口（阶段三起作为主循环的一个 Turn 受理）。

    - 可见范围**由服务端从已保存状态推导**；客户端传来的 requestedVisible 只用来
      报告「哪些请求项不可见而被忽略」，绝不接受未勾选卡片进入载荷；
    - 成功才更新基准；**不改动任何勾选**（整体许可持续有效，契约 §2.2）；
    - 失败保留改动与本次注释选择，不更新基准、不调用 QIO；
    - empty / duplicate 不更新基准、不调用 QIO、**不创建主循环 turn**；
    - M4 路径 3：请求可携带 baseStateVersion（本次候选所基于的服务器 seq）与
      confirmedCheckId（影响确认记录）；与服务端不一致 → StaleState，
      **不落库**（不产生提交记录、不更新基准、不调用 QIO）；
    - **受理 = 主循环受理**（§13.3）：succeeded 时由注入的 turn_acceptor 把这次提交
      作为主循环的一个 Turn 受理（同一 TurnManager / 同一模型与工具循环）；不再创建
      独立理解请求、不再启动理解模型调用。turn_acceptor 省略 / 抛错 / 未返回 turnId
      → 按 failed 处理（不更新基准、不改动勾选、如实返回原因），绝不回退旧理解路径；
    - 受理成功后写「提交—turn 绑定」（迁移 49），响应带 turnId 与等待快照；
    - **稳定提交请求身份**（契约 §1.1）：client_request_id 非空时按
      (board_id, client_request_id) 精确认这一次提交请求 ——
      同身份同内容 = 重试，直接返回原记录事实（replayed=True，不落新行、不创建 turn、
      不调用模型）；同身份但 base_state_version 或 note 不一致 →
      RequestIdentityConflict（HTTP 409，不落新提交、不改基准、不调模型）；
      空串 = 旧客户端，行为与今天一致（不参与幂等）。
    - adapter_provider 参数保留只为旧调用点兼容；新流程不再用它启动任何独立模型调用。
    """
    loaded = board_store.load_board(conn, board_id)
    bid = loaded["board"]["id"]
    state = loaded["state"]
    base_seq = _load_base_seq(loaded)
    requested_note = _raw_note(note)
    display_note = _display_note(note)
    # F6：客户端冻结视图只影响「交给 QIO 的几何上下文」，不参与内容 / 求差 / 指纹。
    frozen_view = normalize_view(view)

    # --- 稳定提交请求身份（契约 §1.1 / §1.2）：先按精确身份认领，再做任何写入与模型调用 ---
    identity = normalize_client_request_id(client_request_id)
    if identity:
        existing = _client_request_id_row(conn, bid, identity)
        if existing is not None:
            conflict = _conflict_reason(
                conn, existing, note=requested_note, base_state_version=base_state_version
            )
            if conflict is not None:
                # 同身份不同内容：绝不落新提交、不改基准、不调模型（契约 §1.1 第 3 条）
                raise RequestIdentityConflict(conflict)
            # 同身份同内容：返回**原记录事实**，不重复落行、不新建理解请求、不启动后台理解
            return replay_response(conn, existing, board_id=bid)

    if base_state_version is not None:
        try:
            requested_version = int(base_state_version)
        except (TypeError, ValueError) as exc:
            raise StaleState("baseStateVersion 必须是板面版本号（整数）。") from exc
        if requested_version != base_seq:
            raise StaleState(
                f"板面已经在服务端更新到版本 {base_seq}，这次提交基于的版本 "
                f"{requested_version} 已过期：请重新读取板面后再提交；本次没有记录任何提交。"
            )
    if confirmed_check_id:
        from agent.interactive import intents as intents_module  # 延迟 import

        reason = intents_module.check_for_submission(
            conn, board_id=bid, check_id=str(confirmed_check_id), state=loaded["state"]
        )
        if reason:
            raise StaleState(reason)
    baseline = last_success_baseline(conn, bid)
    first_submission = baseline is None
    submission_seq = _next_submission_seq(conn, bid)
    submission_id = models.new_id("sub")
    note_text = display_note

    # 任何一步出问题都走 failed：不更新基准、不改动勾选，但要如实返回一份可读的载荷。
    visible = _empty_projection()
    before = _empty_projection(first_submission=first_submission)
    after = _empty_projection()
    expressions: list[dict] = []
    content_hash = ""
    status = "failed"
    error: str | None = None
    try:
        visible = visible_range(state)
        before = project_baseline(baseline, state)
        after = project_snapshot(state)
        expressions = diff_states(before, after)
        content_hash = content_fingerprint(after)
        status, error = _decide_status(baseline, expressions, after)
    except Exception as exc:  # noqa: BLE001 - 提交失败要如实返回，保留改动与勾选
        status = "failed"
        error = f"提交处理失败（未调用 QIO，基准未更新，改动与勾选保留）：{exc}"
        expressions = []

    requested = {str(cid) for cid in (requested_visible or [])}
    ignored_requested = sorted(requested - {card["id"] for card in visible["cards"]})

    # 受理（§13.3）：只有 succeeded 才把这次提交作为主循环的一个 Turn 受理。
    # 由注入的 turn_acceptor 完成（生产路由用 app.turns 的公开受理接口）；这里绝不
    # 回退旧独立理解模型调用。没有注入 / 受理失败 → 按 failed 处理（不更新基准）。
    acceptance: dict[str, Any] = {"ok": False, "turnId": None, "topicId": "", "reason": ""}
    if status == "succeeded":
        if turn_acceptor is None:
            status = "failed"
            error = (
                "受理失败：主循环受理器不可用（没有注入 turn_acceptor）。"
                "本次没有更新基准、没有创建独立理解请求；改动与本次注释选择已保留，可以再试一次。"
            )
            acceptance = {
                "ok": False,
                "turnId": None,
                "topicId": "",
                "reason": "主循环受理器不可用（没有注入 turn_acceptor）",
            }
        else:
            message = submission_turn_message(
                board_id=bid,
                submission_id=submission_id,
                after=after,
                expressions=expressions,
                note=requested_note,
            )
            try:
                created = turn_acceptor(
                    board_id=bid,
                    submission_id=submission_id,
                    message=message,
                    topic_id=None,
                    resume=False,
                    resolved_question_ids=(),
                    intent_ids=(),
                    attempt=1,
                    view=frozen_view,
                )
            except Exception as exc:  # noqa: BLE001 - 受理失败如实返回，不能把路由打崩
                logger.warning("互动提交受理失败（不更新基准、不改动勾选）", exc_info=True)
                created = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
            reason = created.get("reason") if isinstance(created, dict) else None
            turn_id = (
                str(created.get("turnId") or created.get("turn_id") or "").strip()
                if isinstance(created, dict)
                else ""
            )
            if not isinstance(created, dict) or not created.get("ok") or not turn_id:
                status = "failed"
                detail = (
                    str(reason)
                    if reason
                    else (
                        "受理器没有返回 turnId"
                        if isinstance(created, dict) and created.get("ok")
                        else "未知原因"
                    )
                )
                error = f"受理失败：没有创建主循环 turn。原因：{detail}"
                acceptance = {
                    "ok": False,
                    "turnId": None,
                    "topicId": "",
                    "reason": detail,
                }
            else:
                acceptance = {
                    "ok": True,
                    "turnId": turn_id,
                    "topicId": str(
                        created.get("topicId") or created.get("topic_id") or ""
                    ),
                    "reason": None,
                }

    # QIO 投递位置：主循环 turn 已受理才 delivered；没有独立理解模型失败这回事了。
    if status == "succeeded":
        delivery: dict[str, Any] = {
            "delivered": True,
            "status": "accepted",
            "understandingId": None,
            "turnId": acceptance.get("turnId"),
            "boardId": bid,
            "submissionId": submission_id,
            "waiting": _waiting_state_safe(conn, bid),
            "reason": (
                "提交已可靠受理：本次互动提交已作为主循环的一轮受理"
                "（同一 TurnManager / 同一模型与工具循环），QIO 会在对话中处理；"
                "不再创建独立理解记录，旧理解记录只读保留"
            ),
            "detail": (
                f"本次交给 QIO 的范围：卡片 {len(after['cards'])} 项、组 {len(after['groups'])} 项、"
                f"关系 {len(after['links'])} 项；有效改动 {len(expressions)} 项"
            ),
        }
        marking_updated: list[str] = []
        try:
            from agent.interactive import intents  # 延迟 import：避免与 C 的模块互相牵制

            marking_updated = list(
                intents.on_new_submission(
                    conn,
                    board_id=bid,
                    submission_id=submission_id,
                    expressions=expressions,
                )
                or []
            )
            delivery["marking"] = {"updated": marking_updated}
        except Exception as exc:  # noqa: BLE001 - 标记失败不能让提交失败
            delivery["marking"] = {
                "updated": [],
                "error": f"受影响意图标记失败（提交本身已成功）：{exc}",
            }
    elif status == "failed":
        acceptance_failed = bool(acceptance["reason"]) and not acceptance["ok"]
        delivery = {
            "delivered": False,
            "status": "failed",
            "understandingId": None,
            "turnId": None,
            "boardId": bid,
            "submissionId": submission_id,
            "waiting": _waiting_state_safe(conn, bid),
            "reason": (
                f"受理失败：{acceptance['reason']}。本次没有更新基准、没有清除勾选，"
                "改动与本次注释选择已保留，可以再试一次"
                if acceptance_failed
                else "提交未成功：没有调用 QIO，基准未更新，改动与本次注释选择已保留，可以再试一次"
            ),
            "detail": error or "",
            "marking": {"updated": [], "error": "提交未成功，未标记任何意图"},
        }
    else:
        delivery = {
            "delivered": False,
            "reason": f"没有调用 QIO（{status}）：{error or ''}".strip(),
            "detail": "未更新基准；已保存的板面与勾选保持不变",
            "marking": {"updated": []},
        }
    # 备注**原样**落进 delivery：查证（acceptance.note）与身份冲突比对都按它逐字比较，
    # 前端提交时恒为空串，这里就回显空串（绝不把别的说明塞进这个字段）。
    delivery["note"] = requested_note
    # F6：本次冻结视图一并落进投递事实（没给 / 非法 → null，不编造）
    delivery["view"] = frozen_view
    if ignored_requested:
        delivery["detail"] = (
            f"{delivery.get('detail', '')}；客户端请求的 {len(ignored_requested)} 项不在本次允许查看范围内，"
            "已忽略（可见范围由服务端从已保存状态推导）"
        ).strip("；")
    if note_text:
        delivery["detail"] = f"{delivery.get('detail', '')}；用户备注：{note_text}".strip("；")

    baseline_info = {
        # baseline.seq 保持既有语义（成功 = 提交后新的基准版本；其余 = 当前成功基准的版本）。
        "updated": status == "succeeded",
        "firstSubmission": first_submission,
        "previousSeq": int(baseline["seq"]) if baseline else None,
        "seq": submission_seq if status == "succeeded" else (int(baseline["seq"]) if baseline else None),
        # 新增（只增字段）：这次提交**基于**的板面版本 = 请求里的 baseStateVersion。
        # 契约 §1.2 的 acceptance.baseStateVersion 就取它（四种状态都写上，便于事后查证）。
        "baseStateVersion": base_seq,
    }
    record = _record_submission(
        conn,
        submission_id=submission_id,
        board_id=bid,
        seq=submission_seq,
        status=status,
        before=before,
        after=after,
        visible=visible,
        expressions=expressions,
        baseline=baseline_info,
        delivery=delivery,
        content_hash=content_hash if status == "succeeded" else "",
        error=error,
        client_request_id=identity,
    )

    # 契约 §2.2：提交**不再**清空勾选（整体许可持续有效，只有用户手动取消 / 明确隐藏才失效）。
    # checkedCleared 恒为空数组：字段名保留只为兼容旧前端，绝不因为一次回执把许可改掉。
    checked_cleared: list[str] = []

    # 提交—主循环 turn 绑定（迁移 49）：受理成功后写最近绑定与历史；绑定失败只告警，
    # 不影响已经成立的受理事实（响应里仍如实带 turnId / waiting）。
    turn_binding: dict | None = None
    if status == "succeeded" and acceptance.get("turnId"):
        try:
            turn_binding = bind_submission_turn(
                conn,
                submission_id=submission_id,
                turn_id=str(acceptance["turnId"]),
                resume_state={
                    "topicId": str(acceptance.get("topicId") or ""),
                    "kind": "submission",
                    "attempt": 1,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 绑定失败不能让已受理的提交失败
            logger.warning(
                "记录提交—主循环 turn 绑定失败（提交已受理）：%s", exc, exc_info=True
            )
            turn_binding = {"error": str(exc)[:300]}

    record["clientRequestId"] = identity
    return {
        "status": status,
        "submission": record,
        "turnId": acceptance.get("turnId"),
        "turnBinding": turn_binding,
        "waiting": _waiting_state_safe(conn, bid),
        "before": before,
        "after": after,
        "expressions": expressions,
        "baseline": baseline_info,
        "delivery": delivery,
        "visibleRange": visible,
        "checkedCleared": checked_cleared,
    }
