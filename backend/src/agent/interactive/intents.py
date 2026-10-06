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
            progress=_progress_shape({"done": 0, "text": "材料已变化：等待更新预览"}, preview),
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
            progress=_progress_shape({"done": 0, "text": "前项已完成：等待你的再次确认"}, preview),
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
    conn: sqlite3.Connection, intent_id: str, *, confirm_dependency: bool = False
) -> dict:
    """批准一项意图。

    - pending 且无未完成前项 → running；有未完成前项 → waiting_dependency（不自动开始）；
    - 前项全部完成后状态变为 waiting_confirm，必须带 confirmDependency=true 才会开始；
    - 互不相容的另一项已被批准（等待 / 执行 / 已完成）时不能批准；
    - needs_update 的预览禁止批准，必须先更新。
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
        return {
            **_fail("paused", "这项任务处于暂停：需要你确认继续，不会自动重试。"),
            "intent": payload,
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
                progress=_progress_shape({"done": 0, "text": "等待前项成功完成"}, preview),
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
        running_progress = dict(_progress_shape({"done": 0, "text": "执行中"}, preview))
        # 开始执行时记下材料的指纹：之后用户改动这些材料就能被保护住（§1.6）
        watch = _watch_for(conn, row)
        if watch:
            running_progress["__materialWatch"] = watch
        _update(
            conn,
            row["id"],
            status="running",
            reason="已确认开始：前项已完成，任务开始执行。",
            progress=running_progress,
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
            progress=_progress_shape({"done": 0, "text": "等待前项成功完成"}, preview),
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
    pending_progress = dict(_progress_shape({"done": 0, "text": "执行中"}, preview))
    watch = _watch_for(conn, row)
    if watch:
        pending_progress["__materialWatch"] = watch
    _update(
        conn,
        row["id"],
        status="running",
        reason="已批准：任务开始执行。",
        progress=pending_progress,
    )
    fresh = _get_row(conn, intent_id)
    assert fresh is not None
    return {
        "ok": True,
        "intent": _intent_payload(fresh, rows_by_id),
        "reason": "approved",
        "detail": "已批准，任务开始执行。",
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
        progress=_progress_shape({"done": 0, "text": "已拒绝"}, {}),
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
        progress=_progress_shape({"done": 0, "text": "预览已变化：等待更新"}, next_preview),
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


def batch_decide(conn: sqlite3.Connection, *, approve: list[str], reject: list[str]) -> dict:
    """批量审批：选择部分或全部。互不相容的两项在同一批里也不能同时批准。"""
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
        outcome = approve_intent(conn, intent_id)
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


def _apply_preview(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    """把预览变成正式内容：只用用户同样具备的板面操作（新增结果卡片 / 组 / 链接）。"""
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

    applied_groups: list[str] = []
    for group in preview["groups"]:
        if group.get("deleted"):
            continue
        members: list[str] = []
        for member in group.get("members") or []:
            real = mapping.get(str(member), str(member))
            if real in live_ids and real not in members:
                members.append(real)
        if not members:
            continue
        name = str(group.get("name") or models.default_group_name(len(state["groups"]) + 1))
        created_group = models.new_group(
            name,
            ordered=bool(group.get("ordered", False)),
            default_name=bool(group.get("defaultName", True)),
        )
        created_group.update(
            {
                "members": members,
                "x": _num(group.get("x"), created_group["x"]),
                "y": _num(group.get("y"), created_group["y"]),
                "w": _num(group.get("w"), created_group["w"]),
                "h": _num(group.get("h"), created_group["h"]),
            }
        )
        state["groups"].append(created_group)
        mapping[str(group.get("id"))] = created_group["id"]
        applied_groups.append(created_group["id"])

    applied_links: list[str] = []
    pairs = {
        (str(link.get("src")), str(link.get("dst")))
        for link in state["links"]
        if not link.get("deleted")
    }
    for link in preview["links"]:
        if link.get("deleted"):
            continue
        src = mapping.get(str(link.get("src")), str(link.get("src")))
        dst = mapping.get(str(link.get("dst")), str(link.get("dst")))
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
        pairs.add((src, dst))

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
    return {
        "cardIds": applied_cards,
        "groupIds": applied_groups,
        "linkIds": applied_links,
        "signatures": signatures,
        "previewIdMap": mapping,
        "appliedAt": _now(),
    }


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
            progress=_progress_shape({"done": 0, "text": "已暂停（演示）"}, preview),
        )
        fresh = _get_row(conn, intent_id)
        assert fresh is not None
        return {"ok": True, "intent": _intent_payload(fresh, rows_by_id), "demo": True}

    if outcome == "done":
        applied = _apply_preview(conn, row)
        _update(
            conn,
            row["id"],
            status="done",
            applied=applied,
            reason=(
                "演示：任务已完成，预览已经成为正式内容（新增结果卡片 / 组 / 链接与用户手动操作一致）。"
                "第一阶段没有真实模型执行，所以这里的结果是可控演示，不代表真实 QIO 判断。"
            ),
            progress=_progress_shape({"done": total, "total": total, "text": "已完成（演示）"}, preview),
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
        progress=_progress_shape(
            {
                "done": 0,
                "total": total,
                "text": "失败（演示）：已撤回" if outcome == "failed" else "已取消（演示）",
            },
            preview,
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


def recover_running_intents(conn: sqlite3.Connection, board_id: str) -> dict:
    """重启恢复：上次进程遗留的 running 降级为 paused（重新打开不自动继续）。"""
    paused: list[str] = []
    for row in _board_rows(conn, board_id):
        if row["status"] != "running":
            continue
        preview = _preview_shape(models.loads(row["preview"], {}))
        _update(
            conn,
            row["id"],
            status="paused",
            reason="上次没有正常结束（进程重启）：任务已暂停，重新打开不会自动继续，需要你确认。",
            progress=_progress_shape({"done": 0, "text": "已暂停：等待你确认继续"}, preview),
        )
        paused.append(row["id"])
    return {"paused": paused}


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


def _progress_with_watch(row: sqlite3.Row, signatures: dict[str, str]) -> dict:
    payload = dict(_progress_shape(models.loads(row["progress"], {})))
    if signatures:
        payload["__materialWatch"] = signatures
    return payload


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
    """
    affected: list[dict] = []
    for row in _board_rows(conn, board_id):
        if row["status"] != "running" or not _material_watch(row):
            continue
        changed = _changed_materials(row, state)
        if not changed:
            continue
        affected.append(
            {
                "intentId": row["id"],
                "title": row["title"],
                "materials": [_material_label(state, card_id) for card_id in changed],
                "consequence": (
                    "继续保存会让这项任务暂停并保留当前进度；取消则不改动板面，任务继续。"
                    "暂停后不会自动继续，需要你确认。"
                ),
            }
        )
    return {"affected": affected}


def on_board_saved(
    conn: sqlite3.Connection, *, board_id: str, state: dict, reason: str = "op"
) -> dict:
    """保存板面之后被调用（保存本身仍然不调用 QIO）。

    服务端**自己再判定一次**，不依赖前端的预判：凡是 materialRefs 与本次改动相交的
    running 意图，置为 paused 并保留进度。第一次观察到的任务只记基线，不算改动。
    """
    rows = _board_rows(conn, board_id)
    if not rows:
        return {"paused": [], "affected": []}
    paused: list[dict] = []
    affected: list[str] = []
    for row in rows:
        if row["status"] != "running":
            continue
        refs = [str(x) for x in models.loads(row["material_refs"], [])]
        if not refs:
            continue
        if not _material_watch(row):
            # 还没有基线（例如任务在这套保护生效之前就开始）：记下指纹，本次不误伤
            _update(conn, row["id"], progress=_progress_with_watch(row, _material_signatures(state, refs)))
            continue
        changed = _changed_materials(row, state)
        if not changed:
            continue
        labels = "、".join(_material_label(state, card_id) for card_id in changed)
        text = (
            f"你修改了这项任务依赖的材料（{labels}）：保存已经生效，任务因此暂停并保留进度。"
            "需要你确认后才会继续；不会自动重试。"
        )
        base = _progress_shape(models.loads(row["progress"], {}))
        api_progress = _progress_shape({"done": base["done"], "total": base["total"], "text": text})
        stored = dict(api_progress)
        stored["__materialWatch"] = _material_signatures(state, refs)
        _update(conn, row["id"], status="paused", reason=text, progress=stored)
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
