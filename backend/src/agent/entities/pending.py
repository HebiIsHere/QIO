# -*- coding: utf-8 -*-
"""A04：被保护而没落的自动候选，必须能被用户真正管理（读 / 采纳 / 丢弃）。

背景（与 `entities/cards.py` 的 M04/A02 规则配套）：自动提炼想改一个受保护的值
（用户明确设定过 / 来源未知的旧数据 / 用户删除过的属性 / 迟到结果 / 已归档卡片）时，
候选不会落库，而是进 `entity_cards.field_meta.pending`。本模块把这份 pending
变成**用户可管理**的东西：

* `list_candidates`：跨卡片 + 单卡片清单，只读；`total/truncated` 如实（未显示的
  候选不会永久消失，调用方带 `limit` 继续取）；
* `adopt`：**本次用户的明确决定** —— 写入该字段/属性、记 `source="user"` 与新
  `revision`、移除该候选；`expected_revision` 条件校验（不符 → `CandidateConflict`
  / HTTP 409，且**一个字节都不改**）；归档卡不给「点了没效果」的按钮
  （`adoptable=False` + `blocked_reason="card_archived"`）；重复点击幂等
  （`already_resolved=True`）；不支持的候选类型 `adoptable=False` + 原因；
* `dismiss`：只解决这一个候选，**不改**当前实体值、不影响其他候选，重复/并发幂等。

界面（W6）只需按 `CandidateView` 渲染，并在 409 时就地提示「这条实体已被改动，
请刷新后重试」——机器原因码不用它翻译，`reason_label` 已经是给用户看的一句话。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from agent.entities.cards import (
    PENDING_CARD_ARCHIVED,
    PENDING_STALE,
    PENDING_UNKNOWN_SOURCE,
    PENDING_USER_DELETED,
    PENDING_USER_VALUE,
    SOURCE_USER,
    EntityCard,
    EntityCardService,
    field_label,
    normalize_meta,
    pending_field_kind,
)

# 原因码 → 一句话中文（给用户看，不暴露 revision / 内部字段名）。
REASON_LABELS: dict[str, str] = {
    PENDING_USER_VALUE: "你已经明确设置过这项内容，自动提炼的修改没有生效",
    PENDING_UNKNOWN_SOURCE: "这项内容来自升级前的旧数据，没有来源记录，已按你的内容保护",
    PENDING_STALE: "这条候选产生之后实体已被改动，需要你确认这一次修改",
    PENDING_USER_DELETED: "你删除过这项内容，自动提炼不能把它加回来",
    PENDING_CARD_ARCHIVED: "该实体已归档；恢复实体是另一个动作",
}
DEFAULT_REASON_LABEL = "自动提炼想改动这项内容，与当前内容冲突"

# 不支持的候选类型（例如归档卡上的「整张卡片」候选）的原因码。
BLOCKED_UNSUPPORTED = "unsupported_candidate_type"


class CandidateError(Exception):
    """候选管理的领域错误基类（路由层据此映射 HTTP 状态码）。"""


class CandidateNotFound(CandidateError):
    """实体或候选不存在（HTTP 404）。"""

    def __init__(self, detail: str, *, entity_id: str = "", candidate_id: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.entity_id = entity_id
        self.candidate_id = candidate_id

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "reason": "not_found",
            "detail": self.detail,
            "entity_id": self.entity_id,
            "candidate_id": self.candidate_id,
        }


class CandidateConflict(CandidateError):
    """条件校验失败（HTTP 409）：**调用方拿到的状态就是它**，不做任何改动。"""

    def __init__(
        self, reason: str, detail: str = "", *, current_revision: int | None = None
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.current_revision = current_revision

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "conflict": True,
            "current_revision": self.current_revision,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CandidateView:
    """一条待处理候选（界面直接可用，不含内部 id 之外的实现细节）。"""

    candidate_id: str
    entity_id: str
    entity_name: str
    card_state: str
    field: str
    field_label: str
    kind: str
    current_value: object
    candidate_value: object
    reason: str
    reason_label: str
    created_at: str
    card_revision: int
    adoptable: bool
    blocked_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "card_state": self.card_state,
            "field": self.field,
            "field_label": self.field_label,
            "kind": self.kind,
            "current_value": self.current_value,
            "candidate_value": self.candidate_value,
            "reason": self.reason,
            "reason_label": self.reason_label,
            "created_at": self.created_at,
            "card_revision": self.card_revision,
            "adoptable": self.adoptable,
            "blocked_reason": self.blocked_reason,
        }


@dataclass(frozen=True)
class CandidateListing:
    candidates: tuple[CandidateView, ...]
    total: int
    shown: int
    truncated: bool

    def to_dict(self) -> dict:
        return {
            "candidates": [item.to_dict() for item in self.candidates],
            "total": self.total,
            "shown": self.shown,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class AdoptOutcome:
    ok: bool
    entity_id: str
    candidate_id: str
    adopted: dict | None = None
    entity: dict | None = None
    already_resolved: bool = False
    blocked_reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        payload = {
            "ok": self.ok,
            "entity_id": self.entity_id,
            "candidate_id": self.candidate_id,
            "entity": self.entity,
            "adopted": self.adopted,
            "already_resolved": self.already_resolved,
        }
        if self.blocked_reason:
            payload["blocked_reason"] = self.blocked_reason
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class DismissOutcome:
    ok: bool
    entity_id: str
    candidate_id: str
    dismissed: bool
    entity: dict | None = None
    already_resolved: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "entity_id": self.entity_id,
            "candidate_id": self.candidate_id,
            "dismissed": self.dismissed,
            "already_resolved": self.already_resolved,
            "entity": self.entity,
        }


def list_candidates(
    conn: sqlite3.Connection,
    *,
    entity_id: str | None = None,
    include_archived: bool = True,
    limit: int | None = 200,
) -> CandidateListing:
    """待处理候选清单（只读）。`entity_id=None` 是跨卡片视图。

    `total` 是匹配总数（不受 `limit` 影响），`truncated` 如实反映「还有没显示的」；
    最近的候选排在前面，这样截断不会先把新候选丢掉。
    """
    svc = EntityCardService(conn)
    sql = "SELECT * FROM entity_cards"
    params: list[object] = []
    if entity_id is not None:
        sql += " WHERE id = ?"
        params.append(entity_id)
    sql += " ORDER BY updated_at DESC"
    views: list[CandidateView] = []
    for row in conn.execute(sql, params).fetchall():
        card = svc.row_to_card(row)
        if not include_archived and str(card.state or "") != "active":
            continue
        for item in card.field_meta.get("pending", []):
            if isinstance(item, dict):
                views.append(_view_for(card, item))
    views.sort(key=lambda v: (v.created_at, v.candidate_id), reverse=True)
    total = len(views)
    shown = total if limit is None else max(0, min(int(limit), total))
    return CandidateListing(
        candidates=tuple(views[:shown]),
        total=total,
        shown=shown,
        truncated=total > shown,
    )


def adopt(
    conn: sqlite3.Connection,
    entity_id: str,
    candidate_id: str,
    *,
    expected_revision: int | None = None,
    actor: str = SOURCE_USER,
) -> AdoptOutcome:
    """采纳一条候选（用户的明确决定）。

    * `expected_revision` 不符 → `CandidateConflict`（409），**不改任何东西**；
    * 归档卡 → `ok=False, blocked_reason="card_archived"`（先恢复实体，是另一个动作）；
    * 不支持的候选类型 → `ok=False, blocked_reason="unsupported_candidate_type"`；
    * 候选已被处理过 → `already_resolved=True`，不报 500、不重复改值。
    """
    svc = EntityCardService(conn)
    card = svc.get(entity_id)
    if card is None:
        raise CandidateNotFound("entity not found", entity_id=entity_id, candidate_id=candidate_id)
    if expected_revision is not None and int(card.revision) != int(expected_revision):
        raise CandidateConflict(
            PENDING_STALE, "实体版本已变化，请刷新后重试", current_revision=card.revision
        )

    meta = normalize_meta(card.field_meta)
    target = _find_pending(meta, candidate_id)
    if target is None:
        if _is_resolved(meta, candidate_id):
            return AdoptOutcome(
                ok=True,
                entity_id=entity_id,
                candidate_id=candidate_id,
                already_resolved=True,
                entity=svc.to_dict(card),
            )
        raise CandidateNotFound(
            "candidate not found", entity_id=entity_id, candidate_id=candidate_id
        )

    if str(card.state or "") != "active":
        return AdoptOutcome(
            ok=False,
            entity_id=entity_id,
            candidate_id=candidate_id,
            blocked_reason=PENDING_CARD_ARCHIVED,
            detail="该实体已归档；需要先恢复该实体（独立动作）",
            entity=svc.to_dict(card),
        )

    field = str(target.get("field") or "")
    if pending_field_kind(field) == "unknown":
        return AdoptOutcome(
            ok=False,
            entity_id=entity_id,
            candidate_id=candidate_id,
            blocked_reason=BLOCKED_UNSUPPORTED,
            detail="这条候选不支持一键采纳，请手动修改该实体",
            entity=svc.to_dict(card),
        )

    updated = svc.resolve_pending(
        entity_id,
        candidate_id,
        accept=True,
        expected_revision=card.revision,
        actor=actor,
    )
    if updated is None:
        current = svc.get(entity_id)
        raise CandidateConflict(
            PENDING_STALE,
            "提交时实体已被改动，请刷新后重试",
            current_revision=current.revision if current is not None else None,
        )
    return AdoptOutcome(
        ok=True,
        entity_id=entity_id,
        candidate_id=candidate_id,
        adopted={"field": field, "value": target.get("value")},
        entity=svc.to_dict(updated),
    )


def dismiss(
    conn: sqlite3.Connection,
    entity_id: str,
    candidate_id: str,
    *,
    expected_revision: int | None = None,
) -> DismissOutcome:
    """丢弃一条候选：只解决这一个，不改当前值、不影响其他候选（重复点击幂等）。"""
    svc = EntityCardService(conn)
    card = svc.get(entity_id)
    if card is None:
        raise CandidateNotFound("entity not found", entity_id=entity_id, candidate_id=candidate_id)
    if expected_revision is not None and int(card.revision) != int(expected_revision):
        raise CandidateConflict(
            PENDING_STALE, "实体版本已变化，请刷新后重试", current_revision=card.revision
        )

    meta = normalize_meta(card.field_meta)
    target = _find_pending(meta, candidate_id)
    if target is None:
        if _is_resolved(meta, candidate_id):
            return DismissOutcome(
                ok=True,
                entity_id=entity_id,
                candidate_id=candidate_id,
                dismissed=False,
                already_resolved=True,
                entity=svc.to_dict(card),
            )
        raise CandidateNotFound(
            "candidate not found", entity_id=entity_id, candidate_id=candidate_id
        )

    updated = svc.resolve_pending(
        entity_id, candidate_id, accept=False, expected_revision=card.revision
    )
    if updated is None:
        current = svc.get(entity_id)
        raise CandidateConflict(
            PENDING_STALE,
            "提交时实体已被改动，请刷新后重试",
            current_revision=current.revision if current is not None else None,
        )
    return DismissOutcome(
        ok=True,
        entity_id=entity_id,
        candidate_id=candidate_id,
        dismissed=True,
        entity=svc.to_dict(updated),
    )


# -- 内部小工具 ---------------------------------------------------------------


def _view_for(card: EntityCard, item: dict) -> CandidateView:
    field = str(item.get("field") or "")
    kind = pending_field_kind(field)
    reason = str(item.get("reason") or "")
    state = str(card.state or "")
    if state != "active":
        blocked = PENDING_CARD_ARCHIVED
    elif kind == "unknown":
        blocked = BLOCKED_UNSUPPORTED
    else:
        blocked = ""
    return CandidateView(
        candidate_id=str(item.get("id") or ""),
        entity_id=card.id,
        entity_name=card.name,
        card_state=state,
        field=field,
        field_label=field_label(field),
        kind=kind,
        current_value=_current_value(card, field),
        candidate_value=item.get("value"),
        reason=reason,
        reason_label=REASON_LABELS.get(reason, DEFAULT_REASON_LABEL),
        created_at=str(item.get("at") or card.updated_at or ""),
        card_revision=card.revision,
        adoptable=blocked == "",
        blocked_reason=blocked,
    )


def _current_value(card: EntityCard, field: str) -> object:
    """当前值的可读形态（缺失 → None，别名 → 列表）。"""
    if field == "summary":
        return card.summary
    if field == "kind":
        return card.kind
    if field == "aliases":
        return list(card.aliases)
    if field.startswith("attributes."):
        key = field.split(".", 1)[1]
        hit = next((a for a in card.attributes if str(a.get("key")) == key), None)
        return None if hit is None else hit.get("value")
    return None


def _find_pending(meta: dict, candidate_id: str) -> dict | None:
    return next(
        (
            item
            for item in meta.get("pending", [])
            if str(item.get("id") or "") == str(candidate_id)
        ),
        None,
    )


def _is_resolved(meta: dict, candidate_id: str) -> bool:
    return any(
        str(row.get("id") or "") == str(candidate_id) for row in meta.get("resolved", [])
    )
