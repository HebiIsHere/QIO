"""QIO 意图与审批生命周期（子智能体 C 负责实现）。

契约：docs/interactive-mode-contract.md §1.6 / §3。

要点：

- QIO 对正式板面的任何改动都必须经过审批；批准后才成为任务，拒绝后预览消失、原内容保留；
- 互不相容的结果不能同时批准；依赖任务即使提前批准，也要等前项成功并展示结果 + 用户再次确认；
- 相关材料变化 → 待审批预览标记 needs_update 并禁止批准；
- 失败 / 取消撤回该任务造成的改动，保留用户后续修改，并说明未撤回部分；
- 不自动重试；重启后 running → paused，由用户决定是否继续。

边界都在服务端判定（冲突、依赖、材料变化、恢复），前端只显示结果。
第一阶段没有真实的模型执行：演示意图的推进只走 /demo/advance，标注为演示。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from typing import Any, Iterable

from agent.interactive import models

# --- 常量 ---------------------------------------------------------------

#: 仍然「等待审批」的状态：材料变化时要标记 needs_update
OPEN_STATUSES = models.OPEN_INTENT_STATUSES
#: 可以进入审批列表（含批量列表）的状态
DECIDABLE_STATUSES = ("pending", "needs_update", "waiting_dependency", "waiting_confirm")
#: 冲突判定仍然有意义的意图状态：已拒绝 / 失败 / 取消的不再互相阻挡
CONFLICT_ACTIVE_STATUSES = (
    "pending",
    "needs_update",
    "waiting_dependency",
    "waiting_confirm",
    "running",
    "paused",
    "done",
)
#: 已经批准、还没被撤回的状态：这些会阻挡互不相容的另一项被批准
CONFLICT_BLOCKING_STATUSES = ("waiting_dependency", "waiting_confirm", "running", "paused", "done")
#: 同一批达到这个数量时，界面提供简洁列表（批量选择部分或全部）
BATCH_MIN = 4
#: 演示执行推进允许的结果
ADVANCE_OUTCOMES = ("done", "failed", "paused", "cancelled")

#: 演示场景：四项可控意图（含一对冲突、一项依赖前项、一项会失败）
DEMO_TITLES = {
    "combine": "［演示］把材料归为一组并给出对比摘要",
    "separate": "［演示］保持材料分开，分别给出说明",
    "followup": "［演示］根据前一项的结论再整理一份清单",
    "failing": "［演示］把材料整理成可执行清单",
}

_JSON_COLUMNS = (
    "preview",
    "impact",
    "depends_on",
    "conflicts_with",
    "material_refs",
    "progress",
    "applied",
    "revert",
)

_KIND_LABELS = {
    "text": "注释",
    "file": "文件",
    "image": "图片",
    "code": "代码",
    "url": "链接",
    "reply": "QIO 结果",
}


# --- 基础工具 -----------------------------------------------------------


def _now() -> str:
    return models.now_iso()


def _fail(reason: str, detail: str) -> dict:
    return {"ok": False, "reason": reason, "detail": detail}


def _get_row(conn: sqlite3.Connection, intent_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM board_intents WHERE id = ?", (intent_id,)).fetchone()


def _board_rows(conn: sqlite3.Connection, board_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM board_intents WHERE board_id = ? ORDER BY rowid", (board_id,)
        ).fetchall()
    )


def _update(conn: sqlite3.Connection, intent_id: str, **fields: Any) -> None:
    """按列名更新一条意图；JSON 列自动序列化，updated_at 自动刷新。"""
    fields["updated_at"] = _now()
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(models.dumps(value) if key in _JSON_COLUMNS else value)
    values.append(intent_id)
    conn.execute(f"UPDATE board_intents SET {', '.join(assignments)} WHERE id = ?", values)


def _insert_intent(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    title: str,
    summary: str = "",
    status: str = "pending",
    preview: dict | None = None,
    impact: dict | None = None,
    depends_on: Iterable[str] = (),
    conflicts_with: Iterable[str] = (),
    conflict_key: str = "",
    material_refs: Iterable[str] = (),
    progress: dict | None = None,
    reason: str = "",
    demo: bool = False,
    submission_id: str | None = None,
    intent_id: str | None = None,
) -> dict:
    intent_id = intent_id or models.new_id("i")
    stamp = _now()
    preview = _preview_shape(preview)
    impact = _impact_shape(impact)
    progress = _progress_shape(progress, preview)
    conn.execute(
        "INSERT INTO board_intents ("
        " id, board_id, submission_id, title, summary, status, preview, impact,"
        " depends_on, conflicts_with, conflict_key, material_refs, progress, applied,"
        " revert, reason, demo, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            intent_id,
            board_id,
            submission_id,
            title,
            summary,
            status,
            models.dumps(preview),
            models.dumps(impact),
            models.dumps(list(depends_on)),
            models.dumps(list(conflicts_with)),
            conflict_key,
            models.dumps(list(material_refs)),
            models.dumps(progress),
            models.dumps({}),
            models.dumps({}),
            reason,
            1 if demo else 0,
            stamp,
            stamp,
        ),
    )
    row = _get_row(conn, intent_id)
    assert row is not None
    # 16：意图创建时记录审批依据（材料语义 + 检查时的板面版本），供审批时复核
    _record_basis(conn, row)
    return _intent_payload(row)


def _card_label(card: dict) -> str:
    content = str(card.get("content") or "").strip().replace("\n", " ")
    if len(content) > 18:
        content = content[:18] + "…"
    kind = _KIND_LABELS.get(str(card.get("kind")), str(card.get("kind")))
    return f"{kind}「{content}」" if content else f"{kind} {card.get('id')}"


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


# --- 预览的形状与语义比较（只改位置 vs 改语义） ---------------------------


def _preview_shape(preview: Any) -> dict:
    data = preview if isinstance(preview, dict) else {}
    return {
        "cards": [dict(c) for c in data.get("cards") or [] if isinstance(c, dict)],
        "groups": [dict(g) for g in data.get("groups") or [] if isinstance(g, dict)],
        "links": [dict(l) for l in data.get("links") or [] if isinstance(l, dict)],
        "note": str(data.get("note") or ""),
    }


def _impact_shape(impact: Any) -> dict:
    data = impact if isinstance(impact, dict) else {}
    return {
        "objects": [str(x) for x in data.get("objects") or []],
        "tasks": [str(x) for x in data.get("tasks") or []],
        "consequences": [str(x) for x in data.get("consequences") or []],
    }


def _progress_shape(progress: Any, preview: dict | None = None) -> dict:
    data = progress if isinstance(progress, dict) else {}
    total = int(data.get("total") or len((preview or {}).get("cards") or []) or 1)
    total = max(1, total)
    done = max(0, min(int(data.get("done") or 0), total))
    return {"done": done, "total": total, "text": str(data.get("text") or "")}


#: progress 里的私有键：材料指纹与执行者身份。它们不进接口负载（_progress_shape 会剥离），
#: 但必须跨状态保留——恢复流程一旦重建 progress，材料保护就会失效。
#: __basisWatch 是审批依据指纹（收尾轮 16）：意图创建时相关材料的语义指纹 + 检查时的板面
#: 版本；审批与保存时服务端用它重新校验「材料还是预览所依据的那一份」。
_PRIVATE_PROGRESS_KEYS = ("__materialWatch", "__ownerInstance", "__basisWatch")


def _progress_private(row: sqlite3.Row) -> dict:
    progress = models.loads(row["progress"], {})
    if not isinstance(progress, dict):
        return {}
    return {key: progress[key] for key in _PRIVATE_PROGRESS_KEYS if key in progress}


def _owner_instance(row: sqlite3.Row) -> str | None:
    """这项 running 任务属于哪个进程实例（没有记录时返回 None）。"""
    owner = _progress_private(row).get("__ownerInstance")
    return str(owner) if owner else None


def _stored_progress(
    row: sqlite3.Row,
    *,
    text: str | None = None,
    done: int | None = None,
    total: int | None = None,
    watch: dict | None = None,
    owner: str | None = None,
    drop_owner: bool = False,
    basis: dict | None = None,
) -> dict:
    """构造写回数据库的 progress（保留私有键）；接口负载仍由 _progress_shape 剥离私有键。"""
    base = _progress_shape(models.loads(row["progress"], {}))
    payload = {
        "done": base["done"] if done is None else done,
        "total": base["total"] if total is None else total,
        "text": base["text"] if text is None else text,
    }
    private = _progress_private(row)
    if watch is not None:
        private["__materialWatch"] = watch
    if owner is not None:
        private["__ownerInstance"] = owner
    if drop_owner:
        private.pop("__ownerInstance", None)
    if basis is not None:
        private["__basisWatch"] = basis
    payload.update(private)
    return payload


def _card_semantic(card: dict) -> dict:
    """卡片语义：位置、大小、折叠、书签都不算语义（只影响显示）。"""
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    return {
        "kind": card.get("kind"),
        "content": card.get("content") or "",
        "meta": dict(sorted(meta.items())),
        "checked": bool(card.get("checked", False)),
        "hidden": bool(card.get("hidden", False)),
        "deleted": bool(card.get("deleted", False)),
    }


def _card_geometry(card: dict) -> dict:
    return {
        "x": round(_num(card.get("x")), 3),
        "y": round(_num(card.get("y")), 3),
        "w": round(_num(card.get("w")), 3),
        "h": round(_num(card.get("h")), 3),
    }


def _group_semantic(group: dict) -> dict:
    return {
        "name": group.get("name") or "",
        "defaultName": bool(group.get("defaultName", True)),
        "ordered": bool(group.get("ordered", False)),
        "members": [str(m) for m in group.get("members") or []],
        "deleted": bool(group.get("deleted", False)),
    }


def _link_semantic(link: dict) -> dict:
    return {
        "src": str(link.get("src") or ""),
        "dst": str(link.get("dst") or ""),
        "direction": bool(link.get("direction", False)),
        "meaning": link.get("meaning") or "",
        "deleted": bool(link.get("deleted", False)),
    }


def _preview_keys(preview: dict) -> tuple[dict, dict]:
    """返回（完整键, 语义键）。两者的差别只有位置与大小。"""
    cards = {str(c.get("id")): c for c in preview.get("cards") or []}
    groups = {str(g.get("id")): g for g in preview.get("groups") or []}
    links = {str(l.get("id")): l for l in preview.get("links") or []}

    full = {
        "cards": {cid: {**_card_semantic(c), **_card_geometry(c)} for cid, c in cards.items()},
        "groups": {
            gid: {**_group_semantic(g), **_card_geometry(g)} for gid, g in groups.items()
        },
        "links": {lid: _link_semantic(l) for lid, l in links.items()},
        "note": preview.get("note") or "",
    }
    semantic = {
        "cards": {cid: _card_semantic(c) for cid, c in cards.items()},
        "groups": {gid: _group_semantic(g) for gid, g in groups.items()},
        "links": {lid: _link_semantic(l) for lid, l in links.items()},
        "note": preview.get("note") or "",
    }
    return full, semantic


def _preview_changes(current: dict, next_preview: dict) -> list[str]:
    """人能读懂的改动清单：用于「需要更新」的原因说明。"""
    changes: list[str] = []
    cur_cards = {str(c.get("id")): c for c in current.get("cards") or []}
    nxt_cards = {str(c.get("id")): c for c in next_preview.get("cards") or []}
    for cid, card in nxt_cards.items():
        if cid not in cur_cards:
            changes.append(f"新增结果卡片：{_card_label(card)}")
        elif _card_semantic(card) != _card_semantic(cur_cards[cid]):
            changes.append(f"修改结果卡片：{_card_label(card)}")
    for cid, card in cur_cards.items():
        if cid not in nxt_cards:
            changes.append(f"移除结果卡片：{_card_label(card)}")

    cur_groups = {str(g.get("id")): g for g in current.get("groups") or []}
    nxt_groups = {str(g.get("id")): g for g in next_preview.get("groups") or []}
    for gid, group in nxt_groups.items():
        if gid not in cur_groups:
            changes.append(f"新增组「{group.get('name')}」")
        elif _group_semantic(group) != _group_semantic(cur_groups[gid]):
            changes.append(f"修改组「{group.get('name')}」的成员或名称")
    for gid, group in cur_groups.items():
        if gid not in nxt_groups:
            changes.append(f"移除组「{group.get('name')}」")

    cur_links = {str(l.get("id")): l for l in current.get("links") or []}
    nxt_links = {str(l.get("id")): l for l in next_preview.get("links") or []}
    for lid, link in nxt_links.items():
        if lid not in cur_links:
            changes.append(f"新增关系「{link.get('meaning') or lid}」")
        elif _link_semantic(link) != _link_semantic(cur_links[lid]):
            changes.append(f"修改关系「{link.get('meaning') or lid}」")
    for lid, link in cur_links.items():
        if lid not in nxt_links:
            changes.append(f"移除关系「{link.get('meaning') or lid}」")

    if (current.get("note") or "") != (next_preview.get("note") or ""):
        changes.append("修改了预览说明")
    return changes


def compare_preview(current: Any, next_preview: Any) -> dict:
    """比较两份预览：same（没有变化）/ layout_only（只改位置）/ semantic（改了语义）。"""
    cur = _preview_shape(current)
    nxt = _preview_shape(next_preview)
    cur_full, cur_semantic = _preview_keys(cur)
    nxt_full, nxt_semantic = _preview_keys(nxt)
    if cur_full == nxt_full:
        level = "same"
    elif cur_semantic == nxt_semantic:
        level = "layout_only"
    else:
        level = "semantic"
    return {"level": level, "changes": _preview_changes(cur, nxt)}


# --- 负载转换 -----------------------------------------------------------


def _unfinished_deps(rows_by_id: dict[str, sqlite3.Row], row: sqlite3.Row) -> list[str]:
    deps = [str(d) for d in models.loads(row["depends_on"], [])]
    return [d for d in deps if d not in rows_by_id or rows_by_id[d]["status"] != "done"]


def _intent_payload(row: sqlite3.Row, rows_by_id: dict[str, sqlite3.Row] | None = None) -> dict:
    rows_by_id = rows_by_id or {}
    status = row["status"]
    payload: dict[str, Any] = {
        "id": row["id"],
        "boardId": row["board_id"],
        "submissionId": row["submission_id"],
        "title": row["title"],
        "summary": row["summary"] or "",
        "status": status,
        "preview": _preview_shape(models.loads(row["preview"], {})),
        "impact": _impact_shape(models.loads(row["impact"], {})),
        "dependsOn": [str(d) for d in models.loads(row["depends_on"], [])],
        "conflictsWith": [str(c) for c in models.loads(row["conflicts_with"], [])],
        "conflictKey": row["conflict_key"] or "",
        "materialRefs": [str(m) for m in models.loads(row["material_refs"], [])],
        "progress": _progress_shape(models.loads(row["progress"], {}), {}),
        "reason": row["reason"] or "",
        "demo": bool(row["demo"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if status in ("waiting_dependency", "waiting_confirm"):
        payload["waitingFor"] = _unfinished_deps(rows_by_id, row)
    applied = models.loads(row["applied"], {})
    if applied:
        payload["applied"] = applied
    revert = models.loads(row["revert"], {})
    if revert:
        payload["revert"] = revert
    return payload


# --- 材料变化：待审批预览标记 needs_update -------------------------------


def _preview_ref_ids(preview: dict) -> set[str]:
    ids: set[str] = set()
    for card in preview.get("cards") or []:
        ids.add(str(card.get("id")))
    for group in preview.get("groups") or []:
        ids.add(str(group.get("id")))
    for link in preview.get("links") or []:
        ids.add(str(link.get("id")))
    return ids


def _affected_ids(expressions: list[dict]) -> tuple[set[str], list[dict]]:
    """把提交里的表达式拆成「被触及的 id」与「构成表达的项」。"""
    touched: set[str] = set()
    bearing: list[dict] = []
    for expr in expressions or []:
        if not isinstance(expr, dict):
            continue
        kind = str(expr.get("kind") or "")
        # 位置 / 大小只影响显示，不作为「材料变化」的依据
        if kind != "layout_only":
            for card_id in expr.get("cardIds") or []:
                touched.add(str(card_id))
            if expr.get("groupId"):
                touched.add(str(expr["groupId"]))
            if expr.get("linkId"):
                touched.add(str(expr["linkId"]))
        if bool(expr.get("intentBearing", kind not in models.NON_INTENT_EXPRESSIONS)):
            bearing.append(expr)
    return touched, bearing


def on_new_submission(
    conn: sqlite3.Connection, *, board_id: str, submission_id: str, expressions: list[dict]
) -> list[str]:
    """把受本次提交影响、仍在等待审批的意图标记为 needs_update，返回被标记的 id。

    - 只处理仍然等待审批的意图（pending / waiting_dependency / waiting_confirm）；
    - 依据是意图的 materialRefs 与预览引用是否被本次提交触及；位置调整除外；
    - 没有明确材料依据的意图：任何「构成表达」的变化都让它需要更新；
    - 已经是 needs_update 的不重复标记。
    """
    rows = _board_rows(conn, board_id)
    if not rows:
        return []
    touched, bearing = _affected_ids(expressions)
    marked: list[str] = []
    for row in rows:
        status = row["status"]
        if status not in OPEN_STATUSES or status == "needs_update":
            continue
        refs = {str(x) for x in models.loads(row["material_refs"], [])}
        refs |= _preview_ref_ids(_preview_shape(models.loads(row["preview"], {})))
        affected = bool(refs & touched)
        if not refs and bearing:
            affected = True
        if not affected:
            continue
        preview = _preview_shape(models.loads(row["preview"], {}))
        _update(
            conn,
            row["id"],
            status="needs_update",
            reason=(
                f"相关材料在提交 {submission_id} 后发生了变化：这份预览的依据已经过期，"
                "需要提交并由 QIO 更新预览后才能批准。"
            ),
            progress=_stored_progress(row, text="材料已变化：等待更新预览"),
        )
        marked.append(row["id"])
    return marked


# --- 依赖与冲突（都在服务端判定） ---------------------------------------


def _refresh_dependency_states(conn: sqlite3.Connection, board_id: str) -> None:
    """前项全部完成时，把 waiting_dependency 提升为 waiting_confirm（不自动开始）。"""
    rows = _board_rows(conn, board_id)
    rows_by_id = {row["id"]: row for row in rows}
    for row in rows:
        if row["status"] != "waiting_dependency":
            continue
        pending = _unfinished_deps(rows_by_id, row)
        if pending:
            continue
        preview = _preview_shape(models.loads(row["preview"], {}))
        _update(
            conn,
            row["id"],
            status="waiting_confirm",
            reason="前项已经成功完成：请先确认，任务不会自动开始。",
            progress=_stored_progress(row, text="前项已完成：等待你的再次确认"),
        )


def _conflicts(a: dict, b: dict) -> bool:
    if b["id"] in (a.get("conflictsWith") or []) or a["id"] in (b.get("conflictsWith") or []):
        return True
    key = a.get("conflictKey") or ""
    return bool(key) and key == (b.get("conflictKey") or "")


def _conflict_groups(intents: list[dict]) -> list[list[str]]:
    active = [i for i in intents if i["status"] in CONFLICT_ACTIVE_STATUSES]
    parent = {i["id"]: i["id"] for i in active}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for index, first in enumerate(active):
        for second in active[index + 1 :]:
            if _conflicts(first, second):
                union(first["id"], second["id"])
    buckets: dict[str, list[str]] = {}
    for item in active:
        buckets.setdefault(find(item["id"]), []).append(item["id"])
    return [sorted(ids) for ids in buckets.values() if len(ids) > 1]


def _conflict_blockers(conn: sqlite3.Connection, row: sqlite3.Row) -> list[dict]:
    target = _intent_payload(row, {})
    blockers = []
    for other in _board_rows(conn, row["board_id"]):
        if other["id"] == row["id"] or other["status"] not in CONFLICT_BLOCKING_STATUSES:
            continue
        if _conflicts(target, _intent_payload(other, {})):
            blockers.append({"id": other["id"], "title": other["title"], "status": other["status"]})
    return blockers


# --- 查询 ---------------------------------------------------------------


def list_intents(conn: sqlite3.Connection, board_id: str) -> dict:
    """意图列表 + 冲突分组 + 批量可用性（恢复信息由路由层补充）。"""
    _refresh_dependency_states(conn, board_id)
    rows = _board_rows(conn, board_id)
    rows_by_id = {row["id"]: row for row in rows}
    intents = [_intent_payload(row, rows_by_id) for row in rows]
    open_count = sum(1 for item in intents if item["status"] in DECIDABLE_STATUSES)
    return {
        "intents": intents,
        "conflicts": _conflict_groups(intents),
        "batchAvailable": open_count >= BATCH_MIN,
        "recovery": {"paused": []},
    }


# --- 审批 ---------------------------------------------------------------


def approve_intent(
    conn: sqlite3.Connection,
    intent_id: str,
    *,
    confirm_dependency: bool = False,
    instance_id: str | None = None,
) -> dict:
    """批准一项意图。

    - pending 且无未完成前项 → running；有未完成前项 → waiting_dependency（不自动开始）；
    - 前项全部完成后状态变为 waiting_confirm，必须带 confirmDependency=true 才会开始；
    - paused 的任务带 confirmDependency=true 表示「按当前材料继续」：重新 running 并刷新材料指纹，
      不带确认则返回 confirm_required（不会自动继续，也不会自动重试）；
    - 互不相容的另一项已被批准（等待 / 执行 / 已完成）时不能批准；
    - needs_update 的预览禁止批准，必须先更新；
    - 已拒绝 / 已完成 / 已失败 / 已取消的任务不能通过批准复活。
    """
    row = _get_row(conn, intent_id)
    if row is None:
        return _fail("not_found", "找不到这项意图。")
    rows_by_id = {r["id"]: r for r in _board_rows(conn, row["board_id"])}
    payload = _intent_payload(row, rows_by_id)
    status = row["status"]

    if status == "needs_update":
        return {
            **_fail(
                "needs_update",
                payload["reason"] or "相关材料已变化：需要提交并由 QIO 更新预览后才能批准。",
            ),
            "intent": payload,
            "requiresUpdate": True,
        }
    if status in ("rejected", "failed", "cancelled"):
        return {**_fail("closed", "这项意图已经结束，不能再批准。"), "intent": payload}
    if status == "running":
        return {**_fail("running", "这项任务已经在执行中。"), "intent": payload}
    if status == "done":
        return {**_fail("done", "这项任务已经完成。"), "intent": payload}
    if status == "paused":
        if not confirm_dependency:
            return {
                **_fail(
                    "confirm_required",
                    "这项任务暂停后材料已经变过，继续之前需要你确认按当前材料继续"
                    "（不会自动重试，也不会重新执行已完成的部分）。",
                ),
                "intent": payload,
            }
        return _resume_paused_intent(conn, row, instance_id=instance_id, rows_by_id=rows_by_id)

    # 16：审批时服务端重新校验材料与预览依据；单项、批量、依赖等待后的确认共用这条路径。
    if status in ("pending", "waiting_dependency", "waiting_confirm"):
        stale = _stale_basis_materials(conn, row)
        if stale:
            state = _load_state(conn, row["board_id"])
            labels = "、".join(_material_label(state, card_id) for card_id in stale[:3])
            preview = _preview_shape(models.loads(row["preview"], {}))
            text = (
                f"相关材料在批准之前发生了变化（{labels}）：这份预览的依据已经过期，"
                "需要提交并由 QIO 更新预览后才能批准。"
            )
            _update(
                conn,
                row["id"],
                status="needs_update",
                reason=text,
                progress=_stored_progress(row, text="材料已变化：等待更新预览"),
            )
            fresh = _get_row(conn, intent_id)
            assert fresh is not None
            return {
                **_fail("needs_update", text),
                "intent": _intent_payload(fresh, rows_by_id),
                "requiresUpdate": True,
            }

    blockers = _conflict_blockers(conn, row)
    if blockers:
        names = "、".join(f"「{b['title']}」" for b in blockers[:3])
        return {
            **_fail("conflict", f"与已批准且尚未撤回的 {names} 互不相容：不能同时批准。"),
            "intent": payload,
            "conflictsWith": [b["id"] for b in blockers],
        }

    preview = _preview_shape(models.loads(row["preview"], {}))
    pending_deps = _unfinished_deps(rows_by_id, row)

    if status in ("waiting_dependency", "waiting_confirm"):
        if pending_deps:
            _update(
                conn,
                row["id"],
                status="waiting_dependency",
                reason="前项还没有成功完成：会一直等待，不会自动开始。",
                progress=_stored_progress(row, text="等待前项成功完成"),
            )
            fresh = _get_row(conn, intent_id)
            return {
                **_fail("dependency_waiting", "前项还没有成功完成：会一直等待，不会自动开始。"),
                "intent": _intent_payload(fresh, rows_by_id),
                "waitingFor": pending_deps,
            }
        if not confirm_dependency:
            return {
                **_fail(
                    "confirm_required",
                    "前项已经完成：开始之前需要你再次确认（不会自动开始）。",
                ),
                "intent": payload,
                "waitingFor": [],
            }
        # 开始执行时记下材料指纹与执行者身份（§1.6）：材料保护靠指纹，
        # 「重启才降级为暂停」靠身份，否则刷一次列表就会把自己正在执行的任务暂停掉。
        _update(
            conn,
            row["id"],
            status="running",
            reason="已确认开始：前项已完成，任务开始执行。",
            progress=_stored_progress(
                row,
                text="执行中",
                done=0,
                watch=_watch_for(conn, row) or None,
                owner=instance_id,
            ),
        )
        fresh = _get_row(conn, intent_id)
        assert fresh is not None
        return {
            "ok": True,
            "intent": _intent_payload(fresh, rows_by_id),
            "reason": "approved",
            "detail": "已确认开始。",
        }

    # pending：首次批准
    if pending_deps:
        _update(
            conn,
            row["id"],
            status="waiting_dependency",
            reason="已批准；但前项还没有成功完成：不会自动开始，前项完成并再次确认后才开始。",
            progress=_stored_progress(row, text="等待前项成功完成"),
        )
        fresh = _get_row(conn, intent_id)
        assert fresh is not None
        return {
            "ok": True,
            "intent": _intent_payload(fresh, rows_by_id),
            "reason": "waiting_dependency",
            "detail": "已批准；前项成功完成后还需要你再次确认才会开始。",
            "waitingFor": pending_deps,
        }
    _update(
        conn,
        row["id"],
        status="running",
        reason="已批准：任务开始执行。",
        progress=_stored_progress(
            row, text="执行中", done=0, watch=_watch_for(conn, row) or None, owner=instance_id
        ),
    )
    fresh = _get_row(conn, intent_id)
    assert fresh is not None
    return {
        "ok": True,
        "intent": _intent_payload(fresh, rows_by_id),
        "reason": "approved",
        "detail": "已批准，任务开始执行。",
    }


def _resume_paused_intent(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    instance_id: str | None,
    rows_by_id: dict[str, sqlite3.Row],
) -> dict:
    """暂停 → 继续：按**当前材料**重新记录指纹，保留进度，不自动重试、不重跑已完成的部分。"""
    refs = [str(x) for x in models.loads(row["material_refs"], [])]
    watch = _material_signatures(_load_state(conn, row["board_id"]), refs) if refs else {}
    _update(
        conn,
        row["id"],
        status="running",
        reason="已按当前材料继续；这次继续不会自动重试，也不会重新执行已完成的部分。",
        progress=_stored_progress(
            row,
            watch=watch or None,
            owner=instance_id,
            drop_owner=not instance_id,
        ),
    )
    fresh = _get_row(conn, row["id"])
    assert fresh is not None
    return {
        "ok": True,
        "intent": _intent_payload(fresh, rows_by_id),
        "reason": "resumed",
        "detail": "已按当前材料继续；不会自动重试，也不会重新执行已完成的部分。",
    }


def reject_intent(conn: sqlite3.Connection, intent_id: str) -> dict:
    """拒绝：预览消失，板面原内容保持不变。"""
    row = _get_row(conn, intent_id)
    if row is None:
        return _fail("not_found", "找不到这项意图。")
    status = row["status"]
    rows_by_id = {r["id"]: r for r in _board_rows(conn, row["board_id"])}
    if status not in DECIDABLE_STATUSES:
        detail = (
            "执行中的任务请先暂停或取消；拒绝只作用于还在等待审批的预览。"
            if status in ("running", "paused")
            else "这项意图已经结束。"
        )
        return {**_fail("not_rejectable", detail), "intent": _intent_payload(row, rows_by_id)}
    _update(
        conn,
        row["id"],
        status="rejected",
        preview={"cards": [], "groups": [], "links": [], "note": ""},
        reason="已拒绝：预览消失，板面原内容保持不变（没有对正式内容做任何改动）。",
        progress=_stored_progress(row, text="已拒绝"),
    )
    fresh = _get_row(conn, intent_id)
    assert fresh is not None
    return {
        "ok": True,
        "intent": _intent_payload(fresh, rows_by_id),
        "reason": "rejected",
        "detail": "已拒绝：预览已消失，板面原内容保持不变。",
    }


def update_preview(conn: sqlite3.Connection, intent_id: str, preview: dict) -> dict:
    """调整预览：只改位置仍可直接批准；改语义 → needs_update（禁止批准）。"""
    row = _get_row(conn, intent_id)
    if row is None:
        return {**_fail("not_found", "找不到这项意图。"), "requiresUpdate": False}
    status = row["status"]
    rows_by_id = {r["id"]: r for r in _board_rows(conn, row["board_id"])}
    if status not in DECIDABLE_STATUSES:
        return {
            **_fail("closed", "只有还在等待审批的预览可以调整。"),
            "intent": _intent_payload(row, rows_by_id),
            "requiresUpdate": False,
        }
    current = _preview_shape(models.loads(row["preview"], {}))
    next_preview = _preview_shape(preview)
    result = compare_preview(current, next_preview)
    level = result["level"]
    if level == "same":
        return {
            "ok": True,
            "intent": _intent_payload(row, rows_by_id),
            "requiresUpdate": status == "needs_update",
            "canApprove": status != "needs_update",
            "reason": "unchanged",
            "detail": "预览没有变化。",
        }
    if level == "layout_only":
        _update(
            conn,
            row["id"],
            preview=next_preview,
            reason="只调整了预览的位置：工作内容、材料范围与结果关系都没有变，可以直接批准。",
        )
        fresh = _get_row(conn, intent_id)
        assert fresh is not None
        return {
            "ok": True,
            "intent": _intent_payload(fresh, rows_by_id),
            "requiresUpdate": status == "needs_update",
            "canApprove": status != "needs_update",
            "reason": "layout_only",
            "detail": "只改了位置：工作内容、材料范围与结果关系不变，可以直接批准。",
        }
    changes = result["changes"]
    detail = "；".join(changes[:4]) or "预览的工作内容 / 材料范围 / 结果关系发生了变化"
    _update(
        conn,
        row["id"],
        preview=next_preview,
        status="needs_update",
        reason=f"预览发生了变化（{detail}）：需要先提交，由 QIO 更新预览后才能批准。",
        progress=_stored_progress(row, text="预览已变化：等待更新"),
    )
    fresh = _get_row(conn, intent_id)
    assert fresh is not None
    return {
        "ok": True,
        "intent": _intent_payload(fresh, rows_by_id),
        "requiresUpdate": True,
        "canApprove": False,
        "reason": "semantic_change",
        "detail": f"工作要求或关系含义发生了变化（{detail}）：需要提交并由 QIO 更新预览后才能批准。",
    }


def _payloads(conn: sqlite3.Connection, board_id: str) -> list[dict]:
    rows = _board_rows(conn, board_id)
    rows_by_id = {row["id"]: row for row in rows}
    return [_intent_payload(row, rows_by_id) for row in rows]


def _conflicts_pair(first: sqlite3.Row | None, second: sqlite3.Row | None) -> bool:
    if first is None or second is None:
        return False
    return _conflicts(_intent_payload(first, {}), _intent_payload(second, {}))


def batch_decide(
    conn: sqlite3.Connection,
    *,
    approve: list[str],
    reject: list[str],
    instance_id: str | None = None,
) -> dict:
    """批量审批：选择部分或全部。互不相容的两项在同一批里也不能同时批准。

    instance_id 必须一路透传到 approve_intent：批量批准同样会把任务置为 running，
    没有执行者身份的话，紧接着的一次列表读取就会把它当成「别的进程遗留」暂停掉。
    """
    approve_ids = [str(x) for x in approve or []]
    reject_ids = [str(x) for x in reject or []]
    rows: dict[str, sqlite3.Row] = {}
    for intent_id in approve_ids + reject_ids:
        row = _get_row(conn, intent_id)
        if row is not None:
            rows[intent_id] = row

    # 同批内的冲突：两项都要求批准，但彼此互不相容 → 两项都不批准
    batch_conflicts: set[str] = set()
    for index, first in enumerate(approve_ids):
        first_row = rows.get(first)
        if first_row is None:
            continue
        for second in approve_ids[index + 1 :]:
            second_row = rows.get(second)
            if second_row is None:
                continue
            if _conflicts(_intent_payload(first_row, {}), _intent_payload(second_row, {})):
                batch_conflicts.add(first)
                batch_conflicts.add(second)

    results: list[dict] = []
    approved: list[str] = []
    rejected: list[str] = []
    for intent_id in approve_ids:
        if intent_id in batch_conflicts:
            others = [
                x
                for x in sorted(batch_conflicts)
                if x != intent_id and _conflicts_pair(rows.get(x), rows.get(intent_id))
            ]
            results.append(
                {
                    **_fail("conflict", "与同一批里另一项互不相容：不能同时批准，两项都没有批准。"),
                    "intentId": intent_id,
                    "conflictsWith": others,
                }
            )
            continue
        outcome = approve_intent(conn, intent_id, instance_id=instance_id)
        outcome["intentId"] = intent_id
        results.append(outcome)
        if outcome.get("ok"):
            approved.append(intent_id)
    for intent_id in reject_ids:
        outcome = reject_intent(conn, intent_id)
        outcome["intentId"] = intent_id
        results.append(outcome)
        if outcome.get("ok"):
            rejected.append(intent_id)

    board_ids = {row["board_id"] for row in rows.values()}
    conflicts: list[list[str]] = []
    for board_id in sorted(board_ids):
        conflicts.extend(_conflict_groups(_payloads(conn, board_id)))
    return {
        "ok": True,
        "results": results,
        "approved": approved,
        "rejected": rejected,
        "conflicts": conflicts,
    }


# --- 演示执行：完成 / 失败 / 暂停 / 取消 ---------------------------------


def _board_store():
    # 延迟导入：C → B 的调用，避免任何 import 环
    from agent.interactive import board_store

    return board_store


def _signature(value: Any) -> str:
    return hashlib.sha1(models.dumps(value).encode("utf-8")).hexdigest()[:12]


def _card_signature(card: dict) -> str:
    """用户后续修改的判定：只比较有含义的字段（位置 / 折叠 / 书签不算）。"""
    return _signature(_card_semantic(card))


def _group_signature(group: dict) -> str:
    return _signature(_group_semantic(group))


def _link_signature(link: dict) -> str:
    return _signature(_link_semantic(link))


def _load_state(conn: sqlite3.Connection, board_id: str) -> dict:
    return _board_store().load_board(conn, board_id)["state"]


def _save_state(conn: sqlite3.Connection, board_id: str, state: dict, reason: str) -> None:
    _board_store().save_board(conn, board_id, state, reason=reason)


def _apply_preview(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict, str | None]:
    """把预览变成正式内容：只用用户同样具备的板面操作（新增结果卡片 / 组 / 链接 / 成员调整）。

    成组落地按「先做成员调整、再建立新组」实现（契约 M5 / 反例 18）：

    - 预览组引用的既有成员若已在别的组里，先从原组移出（成员调整，是用户可以做的操作）；
      一张卡仍然只属于一个组（G1），靠显式调整，而不是靠 normalize_state 静默裁剪；
    - 落库前先在归一化结果上核对「每个批准成员、批准顺序、批准关系」都真实落地；
      任何不一致都在**生效前**明确拒绝：返回失败原因、不写入任何改动；
    - 拒绝时意图保持可处理状态（不会被悄悄标成完成）。
    """
    board_id = row["board_id"]
    preview = _preview_shape(models.loads(row["preview"], {}))
    state = _load_state(conn, board_id)
    state.setdefault("cards", [])
    state.setdefault("groups", [])
    state.setdefault("links", [])

    mapping: dict[str, str] = {}
    applied_cards: list[str] = []
    live_ids = {str(c.get("id")) for c in state["cards"] if models.is_live(c)}

    for card in preview["cards"]:
        if card.get("deleted"):
            continue
        # QIO 新增的正式内容一律是结果卡片：它不参与注释勾选语义（G7）
        created = models.new_card(models.REPLY_KIND, str(card.get("content") or ""))
        meta = dict(card.get("meta") or {})
        meta["intentId"] = row["id"]
        created.update(
            {
                "meta": meta,
                "x": _num(card.get("x"), created["x"]),
                "y": _num(card.get("y"), created["y"]),
                "w": _num(card.get("w"), created["w"]),
                "h": _num(card.get("h"), created["h"]),
                "checked": False,
                "hidden": False,
                "folded": bool(card.get("folded", False)),
                "bookmarked": bool(card.get("bookmarked", False)),
            }
        )
        state["cards"].append(created)
        mapping[str(card.get("id"))] = created["id"]
        applied_cards.append(created["id"])
        live_ids.add(created["id"])

    # --- 预飞检查：预览组引用的成员必须都能落地（新生成的或仍在板面上的活卡） ---
    planned_groups: list[tuple[str, list[str], bool]] = []  # (预览组 id, 落地成员顺序, 是否有序)
    planned_group_ids: set[str] = set()
    for group in preview["groups"]:
        if group.get("deleted"):
            continue
        resolved: list[str] = []
        missing: list[str] = []
        for member in group.get("members") or []:
            real = mapping.get(str(member), str(member))
            if real in live_ids and real not in resolved:
                resolved.append(real)
            else:
                missing.append(str(member))
        if missing:
            return (
                {},
                (
                    "预览引用的成员（"
                    + "、".join(missing[:3])
                    + "）已经不在板面上，批准的内容无法按原样落地；"
                    "没有写入任何改动。请重新提交，由 QIO 更新预览后再处理。"
                ),
            )
        if not resolved:
            continue
        gid = str(group.get("id") or "")
        planned_group_ids.add(gid)
        planned_groups.append((gid, resolved, bool(group.get("ordered", False))))

    # --- 成员调整：既有成员从原组移出（保留原组其余成员；空组随之消失） ----------
    expected_members: set[str] = set()
    for _gid, members, _ordered in planned_groups:
        expected_members.update(members)
    for group in state["groups"]:
        if group.get("deleted") or str(group.get("id")) in planned_group_ids:
            continue
        members = [str(m) for m in group.get("members") or []]
        remaining = [m for m in members if m not in expected_members]
        if remaining != members:
            group["members"] = remaining
            group["updatedAt"] = _now()
            # 成员全部移出的组消失：与用户手动移空一个组是同一种操作（G3）
            group["deleted"] = not remaining

    # --- 建立批准的组（成员 / 顺序 / 缺省名与预览一致） ---------------------------
    applied_groups: list[str] = []
    for gid, members, _ordered in planned_groups:
        source = next(
            (item for item in preview["groups"] if str(item.get("id") or "") == gid), {}
        )
        name = str(source.get("name") or models.default_group_name(len(state["groups"]) + 1))
        created_group = models.new_group(
            name,
            ordered=source.get("ordered") is True,
            default_name=bool(source.get("defaultName", True)),
        )
        created_group.update(
            {
                "members": members,
                "x": _num(source.get("x"), created_group["x"]),
                "y": _num(source.get("y"), created_group["y"]),
                "w": _num(source.get("w"), created_group["w"]),
                "h": _num(source.get("h"), created_group["h"]),
            }
        )
        state["groups"].append(created_group)
        if gid:
            mapping[gid] = created_group["id"]
        applied_groups.append(created_group["id"])

    applied_links: list[str] = []
    expected_links: list[tuple[str, str]] = []
    pairs = {
        (str(link.get("src")), str(link.get("dst")))
        for link in state["links"]
        if not link.get("deleted")
    }
    for link in preview["links"]:
        if link.get("deleted"):
            continue
        src_ref, dst_ref = str(link.get("src") or ""), str(link.get("dst") or "")
        src = mapping.get(src_ref, src_ref)
        dst = mapping.get(dst_ref, dst_ref)
        if src_ref and src_ref not in mapping and src_ref not in live_ids:
            return (
                {},
                (
                    f"预览引用的关系端点（{src_ref}）已经不在板面上，批准的内容无法按原样落地；"
                    "没有写入任何改动。请重新提交，由 QIO 更新预览后再处理。"
                ),
            )
        if dst_ref and dst_ref not in mapping and dst_ref not in live_ids:
            return (
                {},
                (
                    f"预览引用的关系端点（{dst_ref}）已经不在板面上，批准的内容无法按原样落地；"
                    "没有写入任何改动。请重新提交，由 QIO 更新预览后再处理。"
                ),
            )
        if src == dst or src not in live_ids or dst not in live_ids or (src, dst) in pairs:
            continue
        created_link = models.new_link(
            src,
            dst,
            direction=bool(link.get("direction", False)),
            meaning=str(link.get("meaning") or ""),
        )
        state["links"].append(created_link)
        mapping[str(link.get("id"))] = created_link["id"]
        applied_links.append(created_link["id"])
        expected_links.append((src, dst))
        pairs.add((src, dst))

    # --- 落库前核对：归一化后的板面必须完整包含批准的成员 / 顺序 / 关系 -----------
    from agent.interactive import board as board_module  # 延迟 import，避免任何 import 环

    normalized = board_module.normalize_state({**state, "boardId": board_id})
    normalized_groups = {str(g.get("id")): g for g in normalized.get("groups") or []}
    member_groups: dict[str, str] = {}
    for group in normalized.get("groups") or []:
        for member in group.get("members") or []:
            member_groups[str(member)] = str(group.get("id"))
    for gid, members, _ordered in planned_groups:
        landed = normalized_groups.get(str(mapping.get(gid) or ""))
        if landed is None:
            return (
                {},
                (
                    "批准的组在落地核对时消失了（无法按批准内容完整落地）；"
                    "生效前拒绝，没有写入任何改动。"
                ),
            )
        actual = [str(m) for m in landed.get("members") or []]
        if actual != members:
            return (
                {},
                (
                    "批准的组没有按确认的成员落地（期望 "
                    + str(len(members))
                    + " 项，实际 "
                    + "、".join(actual[:3])
                    + "）：生效前拒绝，没有写入任何改动。"
                ),
            )
        for member in members:
            if member_groups.get(member) != str(mapping.get(gid) or ""):
                return (
                    {},
                    (
                        "批准的成员没有都进入批准的组（一张卡只属于一个组，但没有全部用"
                        "成员调整完成）：生效前拒绝，没有写入任何改动。"
                    ),
                )
    existing_link_pairs = {
        (str(link.get("src")), str(link.get("dst")))
        for link in normalized.get("links") or []
        if not link.get("deleted")
    }
    for src, dst in expected_links:
        if (src, dst) not in existing_link_pairs:
            return (
                {},
                (
                    "批准的关系在落地核对时消失了（无法按批准内容完整落地）；"
                    "生效前拒绝，没有写入任何改动。"
                ),
            )

    _save_state(conn, board_id, state, f"intent:{row['id']}:apply")

    fresh = _load_state(conn, board_id)
    cards = {str(c.get("id")): c for c in fresh.get("cards") or []}
    groups = {str(g.get("id")): g for g in fresh.get("groups") or []}
    links = {str(l.get("id")): l for l in fresh.get("links") or []}
    signatures: dict[str, str] = {}
    for card_id in applied_cards:
        if card_id in cards:
            signatures[card_id] = _card_signature(cards[card_id])
    for group_id in applied_groups:
        if group_id in groups:
            signatures[group_id] = _group_signature(groups[group_id])
    for link_id in applied_links:
        if link_id in links:
            signatures[link_id] = _link_signature(links[link_id])
    return (
        {
            "cardIds": applied_cards,
            "groupIds": applied_groups,
            "linkIds": applied_links,
            "signatures": signatures,
            "previewIdMap": mapping,
            "appliedAt": _now(),
        },
        None,
    )


def _other_work_refs(conn: sqlite3.Connection, row: sqlite3.Row) -> set[str]:
    """其他仍在进行（或已完成）的工作引用的对象：撤到它们会影响别的任务。"""
    refs: set[str] = set()
    for other in _board_rows(conn, row["board_id"]):
        if other["id"] == row["id"] or other["status"] in ("rejected", "failed", "cancelled"):
            continue
        refs |= {str(x) for x in models.loads(other["material_refs"], [])}
        refs |= _preview_ref_ids(_preview_shape(models.loads(other["preview"], {})))
    return refs


def _card_impacts(
    card_id: str,
    state: dict,
    *,
    task_groups: set[str],
    task_links: set[str],
    other_refs: set[str],
) -> list[str]:
    impacts: list[str] = []
    group = models.group_of(state, card_id)
    if group is not None and str(group.get("id")) not in task_groups:
        impacts.append(f"它现在属于组「{group.get('name')}」，而这个组不是这次任务建立的")
    for link in state.get("links") or []:
        if link.get("deleted") or str(link.get("id")) in task_links:
            continue
        if card_id in (str(link.get("src")), str(link.get("dst"))):
            impacts.append(f"它是你后来建立的关系「{link.get('meaning') or link.get('id')}」的一端")
    if card_id in other_refs:
        impacts.append("另一个仍在进行的工作以它为材料依据")
    return list(dict.fromkeys(impacts))


def _revert_applied(conn: sqlite3.Connection, row: sqlite3.Row, outcome: str) -> dict:
    """撤回本任务造成的改动：先撤回不受影响的部分，再列出其余部分与具体影响。"""
    board_id = row["board_id"]
    applied = models.loads(row["applied"], {})
    signatures = applied.get("signatures") or {}
    card_ids = [str(x) for x in applied.get("cardIds", [])]
    group_ids = [str(x) for x in applied.get("groupIds", [])]
    link_ids = [str(x) for x in applied.get("linkIds", [])]
    state = _load_state(conn, board_id)
    cards = {str(c.get("id")): c for c in state.get("cards") or []}
    groups = {str(g.get("id")): g for g in state.get("groups") or []}
    links = {str(l.get("id")): l for l in state.get("links") or []}
    task_groups = set(group_ids)
    task_links = set(link_ids)
    other_refs = _other_work_refs(conn, row)

    reverted: list[str] = []
    kept: list[str] = []
    pending: list[dict] = []
    revert_cards: set[str] = set()
    keep_cards: set[str] = set()
    pending_cards: set[str] = set()

    for card_id in card_ids:
        card = cards.get(card_id)
        if card is None or card.get("deleted"):
            continue
        if _card_signature(card) != signatures.get(card_id):
            kept.append(f"卡片 {card_id}（{_card_label(card)}）：你在任务之后修改过它，已保留")
            keep_cards.add(card_id)
            continue
        impacts = _card_impacts(
            card_id, state, task_groups=task_groups, task_links=task_links, other_refs=other_refs
        )
        if impacts:
            pending.append(
                {
                    "id": card_id,
                    "reason": f"撤回卡片 {card_id}（{_card_label(card)}）会影响你已经做过的其他工作",
                    "impact": "；".join(impacts),
                }
            )
            pending_cards.add(card_id)
            continue
        revert_cards.add(card_id)
        reverted.append(f"卡片 {card_id}（{_card_label(card)}）：本任务新增的结果卡片，已撤回")

    revert_links: set[str] = set()
    for link_id in link_ids:
        link = links.get(link_id)
        if link is None or link.get("deleted"):
            continue
        if _link_signature(link) != signatures.get(link_id):
            kept.append(f"关系 {link_id}：你后来修改过它，已保留")
            continue
        if str(link.get("src")) in pending_cards or str(link.get("dst")) in pending_cards:
            pending.append(
                {
                    "id": link_id,
                    "reason": f"关系 {link_id} 的一端要等你决定是否撤回",
                    "impact": "撤回卡片会让这条关系失去一端，因此需要一起决定",
                }
            )
            continue
        revert_links.add(link_id)
        if str(link.get("src")) in keep_cards or str(link.get("dst")) in keep_cards:
            reverted.append(f"关系 {link_id}：本任务建立的关系，已撤回（保留的卡片仍在板面上）")
        else:
            reverted.append(f"关系 {link_id}：本任务建立的关系，已撤回")

    revert_groups: set[str] = set()
    for group_id in group_ids:
        group = groups.get(group_id)
        if group is None or group.get("deleted"):
            continue
        if _group_signature(group) != signatures.get(group_id):
            kept.append(f"组 {group_id}：「{group.get('name')}」被你后来改过，已保留")
            continue
        members = [str(m) for m in group.get("members") or []]
        remaining = [m for m in members if m not in revert_cards]
        if not remaining:
            revert_groups.add(group_id)
            reverted.append(f"组 {group_id}：「{group.get('name')}」的成员都来自本任务，已撤回")
        else:
            group["members"] = remaining
            group["updatedAt"] = _now()
            reverted.append(
                f"组 {group_id}：「{group.get('name')}」已移除本任务新增的成员，保留其余 "
                f"{len(remaining)} 名成员"
            )

    changed = False
    for card_id in revert_cards:
        cards[card_id]["deleted"] = True
        cards[card_id]["updatedAt"] = _now()
        changed = True
    for link_id in revert_links:
        links[link_id]["deleted"] = True
        links[link_id]["updatedAt"] = _now()
        changed = True
    for group_id in revert_groups:
        groups[group_id]["deleted"] = True
        groups[group_id]["updatedAt"] = _now()
        changed = True
    if changed:
        _save_state(conn, board_id, state, f"intent:{row['id']}:revert")

    verb = "失败" if outcome == "failed" else "取消"
    parts = [f"任务{verb}，已撤回本任务造成的改动 {len(reverted)} 项"]
    if kept:
        parts.append(f"保留你后来的修改 {len(kept)} 项")
    if pending:
        parts.append(f"还有 {len(pending)} 项需要你决定是否撤回（撤回会影响你别的 工作）")
    return {
        "reverted": reverted,
        "kept": kept,
        "pendingDecision": pending,
        "reasonText": "；".join(parts) + "。",
    }


def _revert_pending_rest(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """用户决定撤回「等待决定」的其余部分（决策路径：演示推进 outcome=revert_rest）。"""
    applied = models.loads(row["applied"], {})
    report = models.loads(row["revert"], {})
    pending = [p for p in report.get("pendingDecision") or [] if isinstance(p, dict)]
    if not pending:
        return _fail("nothing_pending", "没有等待决定的撤回项。")
    state = _load_state(conn, row["board_id"])
    cards = {str(c.get("id")): c for c in state.get("cards") or []}
    groups = {str(g.get("id")): g for g in state.get("groups") or []}
    links = {str(l.get("id")): l for l in state.get("links") or []}
    applied_groups = {str(x) for x in applied.get("groupIds", [])}
    reverted = list(report.get("reverted") or [])
    kept = list(report.get("kept") or [])
    changed = False
    for item in pending:
        item_id = str(item.get("id") or "")
        if item_id in cards and not cards[item_id].get("deleted"):
            cards[item_id]["deleted"] = True
            cards[item_id]["updatedAt"] = _now()
            reverted.append(f"卡片 {item_id}（{_card_label(cards[item_id])}）：按你的决定，已撤回")
            changed = True
            group = models.group_of(state, item_id)
            if group is not None:
                group["members"] = [m for m in group.get("members") or [] if str(m) != item_id]
                if not group["members"] and str(group.get("id")) in applied_groups:
                    groups[str(group["id"])]["deleted"] = True
                changed = True
        elif item_id in links and not links[item_id].get("deleted"):
            links[item_id]["deleted"] = True
            links[item_id]["updatedAt"] = _now()
            reverted.append(f"关系 {item_id}：按你的决定，已撤回")
            changed = True
        elif item_id in groups and not groups[item_id].get("deleted"):
            groups[item_id]["deleted"] = True
            groups[item_id]["updatedAt"] = _now()
            reverted.append(f"组 {item_id}：按你的决定，已撤回")
            changed = True
        else:
            kept.append(f"{item_id}：已经不在板面上，无需撤回")
    if changed:
        _save_state(conn, row["board_id"], state, f"intent:{row['id']}:revert-rest")
    new_report = {
        "reverted": reverted,
        "kept": kept,
        "pendingDecision": [],
        "reasonText": "已按你的决定撤回其余部分；保留的部分不再变动。",
    }
    _update(conn, row["id"], revert=new_report, reason=f"{row['reason']}（其余部分已按你的决定撤回）")
    fresh = _get_row(conn, row["id"])
    assert fresh is not None
    return {
        "ok": True,
        "intent": _intent_payload(fresh, {}),
        "revert": new_report,
        "demo": bool(row["demo"]),
    }


def advance_intent(conn: sqlite3.Connection, intent_id: str, *, outcome: str) -> dict:
    """演示执行推进（明确标注为演示）。

    第一阶段没有真实的模型执行：这里用可控结果走通状态机与撤回保护。
    outcome=revert_rest 用于执行「等待用户决定」的其余撤回项。
    """
    outcome = str(outcome or "").strip()
    row = _get_row(conn, intent_id)
    if row is None:
        return _fail("not_found", "找不到这项意图。")
    if outcome == "revert_rest":
        if row["status"] not in ("failed", "cancelled"):
            return _fail("not_applicable", "只有失败或取消、且还有待决定项的任务才需要这个决定。")
        return _revert_pending_rest(conn, row)
    if outcome not in ADVANCE_OUTCOMES:
        return _fail("bad_outcome", f"不认识的结果：{outcome}。")

    status = row["status"]
    rows_by_id = {r["id"]: r for r in _board_rows(conn, row["board_id"])}
    preview = _preview_shape(models.loads(row["preview"], {}))
    total = max(1, len(preview["cards"]))

    if status == "paused" and outcome == "paused":
        return {
            "ok": True,
            "intent": _intent_payload(row, rows_by_id),
            "demo": True,
            "detail": "任务已经处于暂停。",
        }
    if status == "waiting_confirm":
        return {
            **_fail("confirm_required", "前项已经完成：需要你先确认，任务不会自动开始。"),
            "intent": _intent_payload(row, rows_by_id),
        }
    if status == "waiting_dependency":
        return {
            **_fail("dependency_waiting", "前项还没有成功完成：会一直等待，不会自动开始。"),
            "intent": _intent_payload(row, rows_by_id),
            "waitingFor": _unfinished_deps(rows_by_id, row),
        }
    # 演示：已经完成的任务也可以让它失败 / 取消，用来演示「失败 / 取消撤回该任务造成的改动」。
    # 真实执行接入后，这条路径由真实的执行结果触发，而不是由演示入口手动给定。
    done_can_revert = bool(row["demo"]) and outcome in ("failed", "cancelled")
    if status not in ("running", "paused") and not (status == "done" and done_can_revert):
        return {
            **_fail("not_running", "只有执行中或已暂停的任务可以推进；先批准它。"),
            "intent": _intent_payload(row, rows_by_id),
        }

    if outcome == "paused":
        _update(
            conn,
            row["id"],
            status="paused",
            reason="演示：任务已暂停并保留进度；重新打开不会自动继续，需要你确认。",
            progress=_stored_progress(row, text="已暂停（演示）"),
        )
        fresh = _get_row(conn, intent_id)
        assert fresh is not None
        return {"ok": True, "intent": _intent_payload(fresh, rows_by_id), "demo": True}

    if outcome == "done":
        applied, apply_failure = _apply_preview(conn, row)
        if apply_failure is not None:
            # 18：无法按批准内容落地 → 生效前明确拒绝；意图保持当前状态，板面没有写入
            return {
                **_fail("cannot_apply", apply_failure),
                "intent": _intent_payload(row, rows_by_id),
                "demo": bool(row["demo"]),
            }
        _update(
            conn,
            row["id"],
            status="done",
            applied=applied,
            reason=(
                "演示：任务已完成，预览已经成为正式内容（新增结果卡片 / 组 / 链接与用户手动操作一致）。"
                "第一阶段没有真实模型执行，所以这里的结果是可控演示，不代表真实 QIO 判断。"
            ),
            progress=_stored_progress(row, text="已完成（演示）", done=total, total=total),
        )
        _refresh_dependency_states(conn, row["board_id"])
        fresh = _get_row(conn, intent_id)
        assert fresh is not None
        return {
            "ok": True,
            "intent": _intent_payload(fresh, rows_by_id),
            "applied": applied,
            "demo": True,
            "detail": "演示任务已完成，结果已作为正式内容落到板面。",
        }

    report = _revert_applied(conn, row, outcome)
    failure_text = (
        "演示：执行未能完成（第一阶段没有真实执行，失败由演示入口给出）。"
        if outcome == "failed"
        else "演示：任务被取消。"
    )
    _update(
        conn,
        row["id"],
        status=outcome,
        revert=report,
        reason=f"{failure_text}{report['reasonText']}不会自动重试。",
        progress=_stored_progress(
            row,
            done=0,
            total=total,
            text="失败（演示）：已撤回" if outcome == "failed" else "已取消（演示）",
        ),
    )
    fresh = _get_row(conn, intent_id)
    assert fresh is not None
    return {
        "ok": True,
        "intent": _intent_payload(fresh, rows_by_id),
        "revert": report,
        "demo": True,
        "detail": report["reasonText"],
    }


def recover_running_intents(
    conn: sqlite3.Connection, board_id: str, *, instance_id: str | None = None
) -> dict:
    """重启恢复：把**不属于当前进程**的 running 降级为 paused（重新打开不自动继续）。

    - owner（progress 私有键 __ownerInstance）就是当前 instance_id 的，是本进程自己的任务，
      什么都不改——否则每读一次意图列表都会把自己正在执行的任务暂停掉；
    - instance_id 为 None（拿不到进程身份）时不暂停任何东西，只返回空结果；
    - 降级时**保留 __materialWatch**：材料保护与撤回记录不能被恢复流程清掉。
    """
    if not instance_id:
        return {"paused": []}
    paused: list[str] = []
    for row in _board_rows(conn, board_id):
        if row["status"] != "running":
            continue
        if _owner_instance(row) == instance_id:
            continue
        _update(
            conn,
            row["id"],
            status="paused",
            reason="上次没有正常结束（进程重启）：任务已暂停，重新打开不会自动继续，需要你确认。",
            progress=_stored_progress(row, text="已暂停：等待你确认继续", drop_owner=True),
        )
        paused.append(row["id"])
    return {"paused": paused}


# --- M4 影响确认协议（08）：checkId 绑定版本、范围与受影响任务 ---------------


#: 进程内的影响确认记录：checkId → 绑定内容。服务重启后记录不存在，确认会被
#: 如实拒绝并提示重新预判——这是有意的失败方式，不做无法证明的自动延续。
_CONFIRM_CHECKS: dict[str, dict] = {}
_CONFIRM_CHECK_LOCK = threading.Lock()
_CONFIRM_CHECK_KEEP = 64


def state_semantic_signature(state: dict) -> str:
    """板面语义签名：内容与关系结构一致；位置 / 大小 / 折叠 / 书签都不算。

    影响确认的 checkId 绑定到它：等待期间普通的位置调整不要求重新预判，
    但任何内容 / 关系 / 组成员变化都会让确认失效（stale_check）——
    确认不能顺带放行预判时没有说明的改动。
    """
    current = state if isinstance(state, dict) else {}
    cards: dict[str, dict] = {}
    for card in current.get("cards") or []:
        if isinstance(card, dict) and card.get("id"):
            cards[str(card["id"])] = _card_semantic(card)
    groups: dict[str, dict] = {}
    for group in current.get("groups") or []:
        if isinstance(group, dict) and group.get("id"):
            groups[str(group["id"])] = _group_semantic(group)
    links: dict[str, dict] = {}
    for link in current.get("links") or []:
        if isinstance(link, dict) and link.get("id"):
            links[str(link["id"])] = _link_semantic(link)
    selection = sorted(str(cid) for cid in current.get("selection") or [])
    return _signature({"cards": cards, "groups": groups, "links": links, "selection": selection})


def _affected_materials_for_state(
    conn: sqlite3.Connection, *, board_id: str, state: dict
) -> list[dict]:
    """对候选状态做影响判断：哪些任务依赖的材料被改变（running / paused 都报告）。"""
    affected: list[dict] = []
    for row in _board_rows(conn, board_id):
        status = row["status"]
        if status not in ("running", "paused") or not _material_watch(row):
            continue
        changed = _changed_materials(row, state)
        if not changed:
            continue
        if status == "running":
            consequence = (
                "继续保存会让这项任务暂停并保留当前进度；取消则不改动板面，任务继续。"
                "暂停后不会自动继续，需要你确认。"
            )
        else:
            # 已经暂停的任务：不再重复暂停，但它的材料依据仍然和当前板面不一致，要如实说明
            consequence = (
                "这项任务已经因为材料变化暂停：继续保存不会自动继续它，需要你确认；"
                "它的材料依据与当前板面仍然不一致。"
            )
        affected.append(
            {
                "intentId": row["id"],
                "title": row["title"],
                "status": status,
                "materials": [_material_label(state, card_id) for card_id in changed],
                "consequence": consequence,
            }
        )
    return affected


def _prune_confirm_checks() -> None:
    """把影响确认记录的数量限制在有界范围（先进先出；进程内存，重启即清空）。"""
    if len(_CONFIRM_CHECKS) <= _CONFIRM_CHECK_KEEP:
        return
    items = sorted(_CONFIRM_CHECKS.items(), key=lambda kv: str(kv[1].get("createdAt") or ""))
    for key, _ in items[: len(items) - _CONFIRM_CHECK_KEEP]:
        _CONFIRM_CHECKS.pop(key, None)


def impact_check(
    conn: sqlite3.Connection, *, board_id: str, state_version: Any, state: dict
) -> dict:
    """M4 预判入口：判断这次候选改动会不会影响执行中 / 已暂停的任务。

    - checkId 绑定（板面版本、候选内容签名、受影响任务）；之后任何让版本变化的
      保存、或与确认内容不一致的保存，确认时都会被拒绝（stale_check）；
    - 预判失败返回 {"ok": False, "reason"}，并**不改动任何状态**；
    - 预判本身只读：不落库、不暂停任何任务。
    """
    try:
        requested = int(state_version)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "stateVersion 必须是板面版本号（整数）"}
    try:
        loaded = _board_store().load_board(conn, board_id)
    except Exception as exc:  # noqa: BLE001 - 预判失败必须真实说明
        return {"ok": False, "reason": f"读取板面失败：{exc}"}
    if requested != loaded["seq"]:
        return {
            "ok": False,
            "reason": (
                f"板面已经更新到版本 {loaded['seq']}，这次预判基于的版本 {requested} 已过期；"
                "请先重新读取板面，再对最新的内容做影响预判。"
            ),
            "currentSeq": loaded["seq"],
        }
    try:
        affected = _affected_materials_for_state(conn, board_id=board_id, state=state)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"影响预判失败：{exc}"}

    running = [item for item in affected if item["status"] == "running"]
    if affected and running:
        names = "、".join(item["title"] for item in running[:3])
        summary = (
            f"这次改动会修改执行中任务（{names}）依赖的材料：需要确认后才会保存生效；"
            "取消则这次改动不生效、任务继续。"
        )
    elif affected:
        summary = (
            "这次改动涉及已暂停任务的依赖材料：不需要额外确认，"
            "但那些任务的材料依据与当前板面仍不一致。"
        )
    else:
        summary = "这次改动不影响任何执行中的任务，可以直接保存。"

    check_id = models.new_id("chk")
    with _CONFIRM_CHECK_LOCK:
        _prune_confirm_checks()
        _CONFIRM_CHECKS[check_id] = {
            "boardId": board_id,
            "stateVersion": int(loaded["seq"]),
            "signature": state_semantic_signature(state),
            "taskIds": [item["intentId"] for item in affected],
            "createdAt": _now(),
        }
    return {
        "ok": True,
        "checkId": check_id,
        "stateVersion": int(loaded["seq"]),
        "affectedTasks": affected,
        "summary": summary,
        "impactConfirmationRequired": bool(running),
    }


def confirm_binding_version(check_id: str) -> int | None:
    """这次影响确认绑定的板面版本（记录不存在时返回 None）。

    保存接口用它核对 confirm.stateVersion：确认必须针对它当时预判的那一版，
    版本对不上就按 stale_check 拒绝（不落库）。
    """
    entry = _CONFIRM_CHECKS.get(str(check_id))
    if entry is None:
        return None
    try:
        return int(entry.get("stateVersion"))
    except (TypeError, ValueError):
        return None


def impact_gate(conn: sqlite3.Connection, *, board_id: str, state: dict) -> dict:
    """保存前的服务端门（常规校验入口：不只依赖前端禁用按钮）。"""
    affected = _affected_materials_for_state(conn, board_id=board_id, state=state)
    running_ids = [item["intentId"] for item in affected if item["status"] == "running"]
    return {"affectedTasks": affected, "runningIds": running_ids}


def validate_save_confirmation(
    conn: sqlite3.Connection, *, board_id: str, check_id: str, candidate_signature: str
) -> str | None:
    """确认校验（M4）：通过返回 None；否则返回失败原因（保存不得落库）。"""
    entry = _CONFIRM_CHECKS.get(str(check_id))
    if entry is None:
        return "没有找到对应的影响确认记录（可能已过期，或服务重新启动过）：请重新预判并确认。"
    if entry.get("boardId") != board_id:
        return "这份影响确认记录属于另一个板面：请重新预判并确认。"
    try:
        current_seq = _board_store().load_board(conn, board_id)["seq"]
    except Exception as exc:  # noqa: BLE001
        return f"读取板面失败：{exc}"
    if entry.get("stateVersion") != current_seq:
        _CONFIRM_CHECKS.pop(str(check_id), None)
        return (
            f"确认之后板面又保存过（现在是版本 {current_seq}）：这份确认已经过期，"
            "请重新预判并确认。"
        )
    if entry.get("signature") != candidate_signature:
        return (
            "待保存的内容已经不是预判时确认的范围（等待期间又改了别的内容）："
            "请重新预判并确认，确认不会顺带放行没有说明的改动。"
        )
    return None


def check_for_submission(
    conn: sqlite3.Connection, *, board_id: str, check_id: str, state: dict
) -> str | None:
    """提交时校验当时确认过的影响范围与当前已保存板面一致（M4 第三条路径）。"""
    entry = _CONFIRM_CHECKS.get(str(check_id))
    if entry is None:
        return "没有找到对应的影响确认记录（可能已过期，或服务重新启动过）：请重新预判并确认后再提交。"
    if entry.get("boardId") != board_id:
        return "这份影响确认记录属于另一个板面：请重新预判并确认后再提交。"
    if entry.get("signature") != state_semantic_signature(state):
        return (
            "提交的板面与当时确认过的内容不一致（之后板面又发生了变化）："
            "请重新预判并确认后再提交。"
        )
    return None


# --- 审批依据指纹（16：材料变了，旧待审批预览不得批准） ---------------------


def _basis_from_state(state: dict, refs: Iterable[str], preview: dict | None = None) -> dict:
    """依据对象在当前板面上的语义指纹（+ 检查时的板面版本由调用方补充）。"""
    live_cards = {str(c.get("id")) for c in state.get("cards") or [] if models.is_live(c)}
    ids: list[str] = []
    for ref in refs or []:
        label = str(ref)
        if label not in ids:
            ids.append(label)
    for card_id in _preview_ref_ids(_preview_shape(preview or {})):
        if card_id in live_cards and card_id not in ids:
            ids.append(card_id)
    return {"signatures": _material_signatures(state, ids)}


def _record_basis(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """第一次见到一条待审批意图时记录它的审批依据。

    旧格式数据缺 __basisWatch 也走这里：按「第一次观察记基线」如实处理——
    无法证明更早的变化，之后的任何语义变化都会被抓到（M9 兼容规则）。
    """
    state = _load_state(conn, row["board_id"])
    refs = [str(x) for x in models.loads(row["material_refs"], [])]
    preview = _preview_shape(models.loads(row["preview"], {}))
    basis = _basis_from_state(state, refs, preview)
    basis["seq"] = int(_board_store().load_board(conn, row["board_id"])["seq"])
    _update(conn, row["id"], progress=_stored_progress(row, basis=basis))


def _stale_basis_materials(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    """已保存板面上已经与预览依据不一致的材料（只看语义，位置不算）。"""
    refs = [str(x) for x in models.loads(row["material_refs"], [])]
    if not refs:
        return []
    progress = models.loads(row["progress"], {})
    basis = progress.get("__basisWatch") if isinstance(progress, dict) else None
    state = _load_state(conn, row["board_id"])
    current = _material_signatures(state, refs)
    if not isinstance(basis, dict) or not isinstance(basis.get("signatures"), dict):
        # 旧数据没有依据指纹：按第一次观察记基线，不做无法证明的判断
        _update(conn, row["id"], progress=_stored_progress(row, basis=_basis_from_state(state, refs)))
        return []
    saved = {str(k): str(v) for k, v in basis["signatures"].items()}
    return [card_id for card_id in refs if saved.get(card_id) != current.get(card_id)]


# --- 保存后的材料保护（执行中任务） ---------------------------------------


def _material_signatures(state: dict, ids: Iterable[str]) -> dict[str, str]:
    """材料在**有含义的字段**上的指纹：位置 / 折叠 / 书签不算改动。"""
    cards = {str(c.get("id")): c for c in state.get("cards") or []}
    signatures: dict[str, str] = {}
    for card_id in ids:
        key = str(card_id)
        card = cards.get(key)
        signatures[key] = _signature(_card_semantic(card)) if isinstance(card, dict) else "missing"
    return signatures


def _material_watch(row: sqlite3.Row) -> dict[str, str]:
    """任务开始执行时记下的材料指纹（存在 progress 的私有键里，不进接口负载）。"""
    progress = models.loads(row["progress"], {})
    watch = progress.get("__materialWatch") if isinstance(progress, dict) else None
    if not isinstance(watch, dict):
        return {}
    return {str(key): str(value) for key, value in watch.items()}


def _watch_for(conn: sqlite3.Connection, row: sqlite3.Row, state: dict | None = None) -> dict:
    refs = [str(x) for x in models.loads(row["material_refs"], [])]
    if not refs:
        return {}
    if state is None:
        state = _load_state(conn, row["board_id"])
    return _material_signatures(state, refs)


def _material_label(state: dict, card_id: str) -> str:
    for card in state.get("cards") or []:
        if str(card.get("id")) == str(card_id):
            return _card_label(card)
    return f"材料 {card_id}（已经不在板面上）"


def _changed_materials(row: sqlite3.Row, state: dict) -> list[str]:
    watch = _material_watch(row)
    if not watch:
        return []
    refs = [str(x) for x in models.loads(row["material_refs"], [])]
    current = _material_signatures(state, refs)
    return [card_id for card_id in refs if watch.get(card_id) != current.get(card_id)]


def preview_material_impact(conn: sqlite3.Connection, *, board_id: str, state: dict) -> dict:
    """保存前的只读预判：这次改动会不会影响执行中的任务。

    契约 §1.6：改动执行中任务依赖的材料前，先说明受影响的任务与后果，再让用户选择
    继续（改动生效、相关任务暂停并保留进度）或取消（不改动、任务继续）。
    这里**不改任何状态**；状态判定以 on_board_saved 为准。
    每项含 status（running / paused），供 M4 的确认门区分哪些任务需要确认。
    """
    return {"affected": _affected_materials_for_state(conn, board_id=board_id, state=state)}


def on_board_saved(
    conn: sqlite3.Connection, *, board_id: str, state: dict, reason: str = "op"
) -> dict:
    """保存板面之后被调用（保存本身仍然不调用 QIO）。

    服务端**自己再判定一次**，不依赖前端的预判：凡是 materialRefs 与本次改动相交的
    running 意图，置为 paused 并保留进度。已经因为材料变化暂停的意图只报告、不重复暂停
    （它的材料依据在被确认更新之前一直是不一致的）。第一次观察到的任务只记基线，不算改动。
    """
    rows = _board_rows(conn, board_id)
    if not rows:
        return {"paused": [], "affected": []}
    paused: list[dict] = []
    affected: list[str] = []
    for row in rows:
        status = row["status"]
        if status in OPEN_STATUSES and status != "needs_update":
            # 16 的补充标记：预览依据在保存后失效（旧格式缺依据指纹时按首次观察记基线）
            refs = [str(x) for x in models.loads(row["material_refs"], [])]
            if not refs:
                continue
            progress = models.loads(row["progress"], {})
            basis = progress.get("__basisWatch") if isinstance(progress, dict) else None
            if not isinstance(basis, dict) or not isinstance(basis.get("signatures"), dict):
                _update(
                    conn,
                    row["id"],
                    progress=_stored_progress(row, basis=_basis_from_state(state, refs)),
                )
                continue
            saved_sigs = {str(k): str(v) for k, v in basis["signatures"].items()}
            current = _material_signatures(state, refs)
            changed = [cid for cid in refs if saved_sigs.get(cid) != current.get(cid)]
            if not changed:
                continue
            preview = _preview_shape(models.loads(row["preview"], {}))
            labels = "、".join(_material_label(state, card_id) for card_id in changed[:3])
            text = (
                f"相关材料在保存后发生了变化（{labels}）：这份预览的依据已经过期，"
                "需要提交并由 QIO 更新预览后才能批准。"
            )
            _update(
                conn,
                row["id"],
                status="needs_update",
                reason=text,
                progress=_stored_progress(row, text="材料已变化：等待更新预览"),
            )
            affected.append(row["id"])
            continue
        if status not in ("running", "paused"):
            continue
        refs = [str(x) for x in models.loads(row["material_refs"], [])]
        if not refs:
            continue
        if not _material_watch(row):
            # 还没有基线（例如任务在这套保护生效之前就开始）：记下指纹，本次不误伤
            if status == "running":
                _update(
                    conn,
                    row["id"],
                    progress=_stored_progress(row, watch=_material_signatures(state, refs)),
                )
            continue
        changed = _changed_materials(row, state)
        if not changed:
            continue
        if status == "paused":
            # 已经暂停：只报告，不再改状态（重复保存不该产生新的暂停动作）
            affected.append(row["id"])
            continue
        labels = "、".join(_material_label(state, card_id) for card_id in changed)
        text = (
            f"你修改了这项任务依赖的材料（{labels}）：保存已经生效，任务因此暂停并保留进度。"
            "需要你确认后才会继续；不会自动重试。"
        )
        base = _progress_shape(models.loads(row["progress"], {}))
        api_progress = _progress_shape({"done": base["done"], "total": base["total"], "text": text})
        _update(
            conn,
            row["id"],
            status="paused",
            reason=text,
            # 保留原始材料基线（不刷新）：在被确认更新之前，这项任务的依据一直与板面不一致，
            # 重复保存会继续报告它受影响，但不会重复暂停
            progress=_stored_progress(row, text=text, drop_owner=True),
        )
        affected.append(row["id"])
        paused.append(
            {
                "intentId": row["id"],
                "title": row["title"],
                "reason": text,
                "progress": api_progress,
            }
        )
    return {"paused": paused, "affected": affected}


# --- 演示意图 -----------------------------------------------------------


def _demo_reply(text: str, x: float, y: float) -> dict:
    card = models.new_card(models.REPLY_KIND, text)
    card.update({"x": x, "y": y, "w": 280.0, "h": 96.0})
    return card


def _make_demo_plans(state: dict) -> dict[str, dict]:
    """四项可控演示意图的预览 / 影响说明（只使用用户同样具备的板面操作）。"""
    live = [c for c in state.get("cards") or [] if models.is_live(c)]
    materials = [c for c in live if c.get("kind") in models.MATERIAL_KINDS][:2]
    material_ids = [str(c.get("id")) for c in materials]
    material_labels = [_card_label(c) for c in materials] or [
        "板面上还没有材料（演示预览会只新增结果卡片）"
    ]

    combine_reply = _demo_reply(
        "对比摘要（演示预览）：两份材料的共同点与差异会写在这里；"
        "这一项只有在批准并执行后才会成为正式内容。",
        40.0,
        200.0,
    )
    combine_group = models.new_group(
        models.default_group_name(1),
        ordered=False,
        default_name=True,
        members=(material_ids + [combine_reply["id"]]),
        x=24.0,
        y=24.0,
        w=340.0,
        h=320.0,
    )
    combine_link = models.new_link(
        combine_reply["id"],
        material_ids[0] if material_ids else combine_reply["id"],
        direction=False,
        meaning="对比依据",
    )

    separate_first = _demo_reply("材料一说明（演示预览）：分别说明，不合并成组。", 40.0, 60.0)
    separate_second = _demo_reply("材料二说明（演示预览）：分别说明，不合并成组。", 40.0, 200.0)

    followup_reply = _demo_reply(
        "整理清单（演示预览）：以前一项的结论为依据继续整理；前项没有完成之前，这一项会一直等待。",
        40.0,
        60.0,
    )

    failing_reply = _demo_reply(
        "可执行清单（演示预览）：这一项会以失败结束，用来演示失败时的撤回保护。",
        40.0,
        60.0,
    )

    return {
        "combine": {
            "title": DEMO_TITLES["combine"],
            "summary": "把两份材料放进同一组，并新增一张对比摘要结果卡片。",
            "preview": {
                "cards": [combine_reply],
                "groups": [combine_group],
                "links": [combine_link] if material_ids else [],
                "note": "虚线预览（演示）：批准并执行后才会成为正式内容；第一阶段没有真实的 QIO 执行。",
            },
            "impact": {
                "objects": material_labels + ["新增结果卡片 1 张"],
                "tasks": ["把两份材料归为一组", "新增一张对比摘要结果卡片"],
                "consequences": [
                    "正式板面会新增 1 张 QIO 结果卡片（实线）",
                    "两份材料会被放进同一个组（组名默认，可自行改名）",
                    "这一项与「保持材料分开」那一项互不相容，不能同时批准",
                ],
            },
            "conflict_key": "demo-group-vs-separate",
            "material_refs": material_ids,
            "reason": "",
        },
        "separate": {
            "title": DEMO_TITLES["separate"],
            "summary": "保持两份材料分开，分别新增一张说明卡片。",
            "preview": {
                "cards": [separate_first, separate_second],
                "groups": [],
                "links": [],
                "note": "虚线预览（演示）：批准并执行后才会成为正式内容；第一阶段没有真实的 QIO 执行。",
            },
            "impact": {
                "objects": material_labels + ["新增结果卡片 2 张"],
                "tasks": ["不合并材料", "分别新增两张说明卡片"],
                "consequences": [
                    "正式板面会新增 2 张 QIO 结果卡片（实线）",
                    "材料保持现状，不会被归组",
                    "这一项与「归为一组」那一项互不相容，不能同时批准",
                ],
            },
            "conflict_key": "demo-group-vs-separate",
            "material_refs": material_ids,
            "reason": "",
        },
        "followup": {
            "title": DEMO_TITLES["followup"],
            "summary": "等前一项完成后再整理一份清单。",
            "preview": {
                "cards": [followup_reply],
                "groups": [],
                "links": [],
                "note": "虚线预览（演示）：前项没有成功完成之前，这一项会一直等待；阶段内没有真实执行。",
            },
            "impact": {
                "objects": material_labels + ["新增结果卡片 1 张"],
                "tasks": ["等待前一项完成", "整理一份清单"],
                "consequences": [
                    "前项没有成功完成之前，这一项不会开始（即使提前批准）",
                    "前项完成后还需要你再次确认才会开始",
                ],
            },
            "conflict_key": "",
            "material_refs": material_ids,
            "reason": "",
        },
        "failing": {
            "title": DEMO_TITLES["failing"],
            "summary": "整理一份可执行清单；这一项会失败，用来演示撤回保护。",
            "preview": {
                "cards": [failing_reply],
                "groups": [],
                "links": [],
                "note": "虚线预览（演示）：这一项会以失败结束，失败后本任务造成的改动会被撤回。",
            },
            "impact": {
                "objects": material_labels + ["新增结果卡片 1 张"],
                "tasks": ["整理一份可执行清单（演示：会失败）"],
                "consequences": [
                    "失败后本任务新增的内容会被撤回",
                    "你自己后来的修改会被保留",
                    "撤回会影响你其他工作时，会先撤回不受影响的部分，其余等待你决定",
                ],
            },
            "conflict_key": "",
            "material_refs": material_ids,
            "reason": "演示场景：这一项会失败，用来演示失败时的撤回保护（不会自动重试）。",
        },
    }


def create_demo_intents(conn: sqlite3.Connection, *, board_id: str) -> list[dict]:
    """生成 4 项可控演示意图：一对冲突、一项依赖前项、一项会失败。

    演示意图全部 demo=true，界面上必须写明「演示」：第一阶段没有真实的模型执行，
    这些结果不代表 QIO 的真实判断。已经存在且尚未结束的同名演示项会被复用，不重复创建。
    """
    board_store = _board_store()
    board_store.ensure_board(conn, board_id=board_id)
    state = board_store.load_board(conn, board_id)["state"]
    plans = _make_demo_plans(state)

    rows = _board_rows(conn, board_id)
    existing = {
        row["title"]: row for row in rows if row["demo"] and row["status"] in OPEN_STATUSES
    }
    ids = {
        key: (existing[plan["title"]]["id"] if plan["title"] in existing else models.new_id("i"))
        for key, plan in plans.items()
    }

    order = ("combine", "separate", "followup", "failing")
    created: list[dict] = []
    for key in order:
        plan = plans[key]
        if plan["title"] in existing:
            created.append(_intent_payload(existing[plan["title"]], {}))
            continue
        conflicts_with: list[str] = []
        if key == "combine":
            conflicts_with = [ids["separate"]]
        elif key == "separate":
            conflicts_with = [ids["combine"]]
        depends_on = [ids["combine"]] if key == "followup" else []
        payload = _insert_intent(
            conn,
            board_id=board_id,
            intent_id=ids[key],
            title=plan["title"],
            summary=plan["summary"],
            status="pending",
            preview=plan["preview"],
            impact=plan["impact"],
            depends_on=depends_on,
            conflicts_with=conflicts_with,
            conflict_key=plan["conflict_key"],
            material_refs=plan["material_refs"],
            progress=_progress_shape(
                {
                    "done": 0,
                    "text": (
                        "演示：等待审批（这一项会失败，用于演示撤回保护）"
                        if key == "failing"
                        else "演示：等待审批"
                    ),
                },
                plan["preview"],
            ),
            reason=plan["reason"],
            demo=True,
        )
        created.append(payload)
    return created


__all__ = [
    "ADVANCE_OUTCOMES",
    "BATCH_MIN",
    "check_for_submission",
    "impact_check",
    "impact_gate",
    "state_semantic_signature",
    "validate_save_confirmation",
    "approve_intent",
    "batch_decide",
    "create_demo_intents",
    "list_intents",
    "on_board_saved",
    "on_new_submission",
    "preview_material_impact",
    "recover_running_intents",
    "reject_intent",
    "update_preview",
    "advance_intent",
]
