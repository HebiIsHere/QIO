"""可见范围、有效改动、提交前后状态（子智能体 B 负责实现）。

契约：docs/interactive-mode-contract.md §1.4 / §1.5 / §3。

硬约束（不许绕过）：

1. 保存不调用 QIO；**只有 submit_board 会让 QIO 拿到表达**，而第一阶段没有接入模型调用，
   所以 delivery.delivered 恒为 false，reason 必须说清「没有接入」，不能假装 QIO 懂了。
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

import hashlib
import json
import sqlite3
from typing import Any

from agent.interactive import board_store, models

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
    if kind in ("file", "image"):
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
) -> dict:
    return {
        "id": models.new_id("x"),
        "kind": kind,
        "intentBearing": kind not in models.NON_INTENT_EXPRESSIONS,
        "summary": summary,
        "cardIds": [str(cid) for cid in card_ids],
        "groupId": group_id,
        "linkId": link_id,
    }


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
    live = {card["id"]: card for card in state.get("cards", []) if models.is_live(card)}
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
            # 已被删除：保留下来表达「撤回」（内容此前已经提交过，不是新的泄露）
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
    """内容指纹：忽略 id、位置与时间，只保留「QIO 能看到的意思」。

    用于 duplicate 判定（与上次成功提交内容一致）：例如撤回后又加回同样的材料，
    内容没变，就不该重复调用 QIO。
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

    groups = sorted(
        json.dumps(
            {
                "name": str(group.get("name") or ""),
                "ordered": bool(group.get("ordered")),
                "members": [
                    card_fp(cards[m]) for m in group.get("members", []) if m in cards
                ],
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
                "src": card_fp(cards[str(link.get("src"))]),
                "dst": card_fp(cards[str(link.get("dst"))]),
                "direction": bool(link.get("direction")),
                "meaning": str(link.get("meaning") or ""),
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for link in snapshot.get("links", [])
        if str(link.get("src")) in cards and str(link.get("dst")) in cards
    )
    selection = sorted(
        card_fp(cards[cid]) for cid in snapshot.get("selection", []) if cid in cards
    )
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
    """empty / duplicate / succeeded 的一致判定（前两种都不更新基准、不调用 QIO）。"""
    content = [e for e in expressions if e.get("kind") not in DISPLAY_ONLY_EXPRESSIONS]
    if not content:
        return (
            "empty",
            "没有可提交内容：本次没有允许查看的内容，或只有普通移动 / 缩放这类不构成表达的变化",
        )
    if baseline and baseline.get("contentHash") and baseline["contentHash"] == content_fingerprint(after):
        return "duplicate", "与上次成功提交内容一致，未重复提交、未更新基准"
    return "succeeded", None


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
) -> dict:
    stamp = models.now_iso()
    conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state,"
        " visible, expressions, baseline, delivery, content_hash, error, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


async def submit_board(
    conn: sqlite3.Connection,
    board_id: str,
    *,
    requested_visible: list[str] | None = None,
    note: str = "",
) -> dict:
    """提交：QIO 取得表达的唯一入口。

    - 可见范围**由服务端从已保存状态推导**；客户端传来的 requestedVisible 只用来
      报告「哪些请求项不可见而被忽略」，绝不接受未勾选卡片进入载荷；
    - 成功才更新基准，并自动取消勾选（不是删除或撤回）；
    - 失败保留改动与本次注释选择，不更新基准、不调用 QIO；
    - empty / duplicate 不更新基准、不调用 QIO；
    - 第一阶段没有接入模型调用：delivery.delivered 恒为 false。
    """
    loaded = board_store.load_board(conn, board_id)
    bid = loaded["board"]["id"]
    state = loaded["state"]
    baseline = last_success_baseline(conn, bid)
    first_submission = baseline is None
    submission_seq = _next_submission_seq(conn, bid)
    submission_id = models.new_id("sub")
    note_text = " ".join(str(note or "").split())[:200]

    # 任何一步出问题都走 failed：不更新基准、不清勾选，但要如实返回一份可读的载荷。
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

    # QIO 投递位置：第一阶段没有接入模型调用，delivered 必须是 false 且说清原因。
    if status == "succeeded":
        delivery: dict[str, Any] = {
            "delivered": False,
            "reason": (
                "第一阶段没有接入 QIO 模型调用：本次表达已记录在本地数据库并标记受影响意图，"
                "但 QIO 尚未真正读取或理解这些内容"
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
        delivery = {
            "delivered": False,
            "reason": "提交未成功：没有调用 QIO，基准未更新，改动与本次注释选择已保留，可以再试一次",
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
    if ignored_requested:
        delivery["detail"] = (
            f"{delivery.get('detail', '')}；客户端请求的 {len(ignored_requested)} 项不在本次允许查看范围内，"
            "已忽略（可见范围由服务端从已保存状态推导）"
        ).strip("；")
    if note_text:
        delivery["detail"] = f"{delivery.get('detail', '')}；用户备注：{note_text}".strip("；")

    baseline_info = {
        "updated": status == "succeeded",
        "firstSubmission": first_submission,
        "previousSeq": int(baseline["seq"]) if baseline else None,
        "seq": submission_seq if status == "succeeded" else (int(baseline["seq"]) if baseline else None),
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
    )

    # 提交成功后自动取消勾选：不是删除或撤回，用户仍可再次勾选提交。
    checked_cleared: list[str] = []
    if status == "succeeded":
        checked_cleared = [card["id"] for card in visible["cards"] if card.get("checked")]
        if checked_cleared:
            try:
                cleared = json.loads(models.dumps(state))
                cleared_ids = set(checked_cleared)
                for card in cleared.get("cards", []):
                    if card.get("id") in cleared_ids:
                        card["checked"] = False
                        card["updatedAt"] = models.now_iso()
                board_store.save_board(conn, bid, cleared, reason="submitted")
            except Exception as exc:  # noqa: BLE001 - 提交已成功；勾选清理失败只记进返回值
                checked_cleared = []
                delivery["detail"] = (
                    f"{delivery.get('detail', '')}；勾选清理失败（提交本身已成功）：{exc}"
                ).strip("；")
                conn.execute(
                    "UPDATE board_submissions SET delivery = ? WHERE id = ?",
                    (models.dumps(delivery), submission_id),
                )
                record["delivery"] = delivery

    return {
        "status": status,
        "submission": record,
        "before": before,
        "after": after,
        "expressions": expressions,
        "baseline": baseline_info,
        "delivery": delivery,
        "visibleRange": visible,
        "checkedCleared": checked_cleared,
    }
