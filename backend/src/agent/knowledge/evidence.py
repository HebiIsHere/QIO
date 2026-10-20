"""轮级 / 后台证据目录（Phase 1 C：主对话与后台提炼共用的**程序拥有的**证据清单）。

边界（契约 §6）：

* 证据目录只由程序构造，**不来自模型正文**：主对话目录 = 本轮真实的用户 / 系统消息、
  实际附件、已经完成的工具调用、本轮注入过且 state=active 的知识与实体；
  后台目录 = 输入给模型的 fragment / 消息 / 实体 / 知识等真实 id。
* 模型只能引用目录里存在的 id；工具 / 派生提交都按目录校验，缺失即拒绝。
* `qio_observed` 必须至少有一条真实工具结果 / 附件 / 已读材料作为依据；
  `user_explicit` 必须至少有一条本轮消息作为依据（主对话里优先要求是用户消息）。
* 本模块是纯数据 + 校验，不调用模型、不写数据库（读取辅助函数只做 SELECT）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# -- 证据种类（契约里的稳定字符串，不做中文匹配） ---------------------------

KIND_MESSAGE = "message"
KIND_FRAGMENT = "fragment"
KIND_SUMMARY = "summary"
KIND_ENTITY = "entity"
KIND_KNOWLEDGE = "knowledge"
KIND_TOOL_RESULT = "tool_result"
KIND_ATTACHMENT = "attachment"
KIND_MATERIAL = "material"

EVIDENCE_KINDS = frozenset(
    {
        KIND_MESSAGE,
        KIND_FRAGMENT,
        KIND_SUMMARY,
        KIND_ENTITY,
        KIND_KNOWLEDGE,
        KIND_TOOL_RESULT,
        KIND_ATTACHMENT,
        KIND_MATERIAL,
    }
)

#: `qio_observed` 至少需要一条这一类依据（真实工具结果 / 附件 / 已读材料）。
OBSERVED_KINDS = frozenset({KIND_TOOL_RESULT, KIND_ATTACHMENT, KIND_MATERIAL})

#: `user_explicit` 的依据必须来自消息。
MESSAGE_KINDS = frozenset({KIND_MESSAGE})

ROLE_USER = "user"

# -- 认知状态（事实的确定程度，不是审批状态） --------------------------------

EPISTEMIC_USER_EXPLICIT = "user_explicit"
EPISTEMIC_SHARED = "shared_understanding"
EPISTEMIC_OBSERVED = "qio_observed"
EPISTEMIC_INFERENCE = "qio_inference"

EPISTEMIC_STATUSES = (
    EPISTEMIC_USER_EXPLICIT,
    EPISTEMIC_SHARED,
    EPISTEMIC_OBSERVED,
    EPISTEMIC_INFERENCE,
)

#: 缺省按「推测」处理（比默认当成用户明确表达更保守）。
DEFAULT_EPISTEMIC_STATUS = EPISTEMIC_INFERENCE

MAX_EVIDENCE_IDS = 12
MAX_EVIDENCE_LABEL = 160


@dataclass(frozen=True)
class EvidenceEntry:
    """目录里的一条证据：真实 id + 种类 + 简短说明（可带消息角色）。"""

    id: str
    kind: str
    label: str = ""
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
        }
        if self.role:
            payload["role"] = self.role
        return payload


class EvidenceCatalog:
    """一次处理实际拥有的证据清单；校验只认这里存在的 id。"""

    def __init__(self, entries: Iterable[Any] | None = None) -> None:
        self._entries: dict[str, EvidenceEntry] = {}
        for entry in entries or ():
            self.add(entry)

    # -- construction -----------------------------------------------------

    @classmethod
    def empty(cls) -> "EvidenceCatalog":
        return cls()

    @classmethod
    def from_entries(cls, entries: Iterable[Any] | None) -> "EvidenceCatalog":
        if isinstance(entries, EvidenceCatalog):
            return entries
        return cls(entries)

    def add(self, entry: Any) -> EvidenceEntry | None:
        """接受 EvidenceEntry / Mapping / 纯字符串 id；坏条目返回 None。"""
        if isinstance(entry, EvidenceEntry):
            item = entry
        elif isinstance(entry, Mapping):
            evidence_id = str(entry.get("id") or "").strip()
            if not evidence_id:
                return None
            kind = str(entry.get("kind") or "").strip()
            if kind not in EVIDENCE_KINDS:
                return None
            item = EvidenceEntry(
                id=evidence_id,
                kind=kind,
                label=str(entry.get("label") or "")[:MAX_EVIDENCE_LABEL],
                role=(str(entry.get("role") or "").strip() or None),
            )
        else:
            evidence_id = str(entry or "").strip()
            if not evidence_id:
                return None
            item = EvidenceEntry(id=evidence_id, kind="", label="")
        if not item.id:
            return None
        self._entries.setdefault(item.id, item)
        return item

    def merge(self, other: "EvidenceCatalog | Iterable[Any] | None") -> "EvidenceCatalog":
        merged = EvidenceCatalog(self._entries.values())
        if other is not None:
            merged.extend(other)
        return merged

    def extend(self, entries: Iterable[Any] | None) -> None:
        for entry in entries or ():
            self.add(entry)

    # -- reads ------------------------------------------------------------

    def get(self, evidence_id: str) -> EvidenceEntry | None:
        return self._entries.get(str(evidence_id or "").strip())

    def __contains__(self, evidence_id: object) -> bool:
        return str(evidence_id or "").strip() in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries.values())

    def entries(self) -> list[EvidenceEntry]:
        return list(self._entries.values())

    def ids(self) -> list[str]:
        return list(self._entries.keys())

    def prompt_entries(self) -> list[dict[str, Any]]:
        """给模型看的真实来源清单（只包含 id / 种类 / 简短说明）。"""
        return [entry.to_dict() for entry in self._entries.values()]

    # -- validation -------------------------------------------------------

    def missing(self, evidence_ids: Iterable[Any] | None) -> list[str]:
        missing: list[str] = []
        for raw in _clean_ids(evidence_ids):
            if raw not in self._entries:
                missing.append(raw)
        return missing

    def entries_for(self, evidence_ids: Iterable[Any] | None) -> list[EvidenceEntry]:
        found: list[EvidenceEntry] = []
        for evidence_id in _clean_ids(evidence_ids):
            entry = self._entries.get(evidence_id)
            if entry is not None:
                found.append(entry)
        return found

    def kinds_for(self, evidence_ids: Iterable[Any] | None) -> set[str]:
        return {entry.kind for entry in self.entries_for(evidence_ids) if entry.kind}

    def has_kind(self, evidence_ids: Iterable[Any] | None, kinds: Iterable[str]) -> bool:
        wanted = set(kinds)
        return any(entry.kind in wanted for entry in self.entries_for(evidence_ids))

    def validate_evidence_ids(
        self, evidence_ids: Iterable[Any] | None
    ) -> tuple[list[str], list[str]]:
        """清洗后的 id 列表 + 目录里不存在的 id 列表。"""
        ids = _clean_ids(evidence_ids)
        return ids, [evidence_id for evidence_id in ids if evidence_id not in self._entries]


def _clean_ids(values: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for raw in values or ():
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def coerce_catalog(value: Any) -> EvidenceCatalog:
    """把 None / EvidenceCatalog / 条目列表统一成目录（B 传 list[dict] 即可）。"""
    if isinstance(value, EvidenceCatalog):
        return value
    return EvidenceCatalog.from_entries(value)


def validate_epistemic_status(
    catalog: EvidenceCatalog,
    epistemic_status: str,
    evidence_ids: Iterable[Any] | None,
) -> str | None:
    """按认知状态校验依据；返回可读失败原因，None = 通过。

    只做程序能证明的检查（不靠中文句子匹配）：

    * `qio_observed`：至少一条 kind ∈ {tool_result, attachment, material}；
    * `user_explicit`：至少一条消息；消息带角色时必须是用户消息
      （目录没提供角色时按「消息存在」处理，不阻止后台提炼）。
    """
    status = str(epistemic_status or "").strip()
    if status not in EPISTEMIC_STATUSES:
        return f"epistemic_status 不在契约内（应为 {'|'.join(EPISTEMIC_STATUSES)}）"
    entries = catalog.entries_for(evidence_ids)
    if status == EPISTEMIC_OBSERVED:
        if not any(entry.kind in OBSERVED_KINDS for entry in entries):
            return (
                "qio_observed 必须至少引用一条真实工具结果 / 附件 / 已读材料作为依据，"
                "不能只依据自述"
            )
    if status == EPISTEMIC_USER_EXPLICIT:
        messages = [entry for entry in entries if entry.kind in MESSAGE_KINDS]
        if not messages:
            return "user_explicit 必须至少引用一条本轮消息作为依据"
        roles = {entry.role for entry in messages if entry.role}
        if roles and ROLE_USER not in roles:
            return "user_explicit 引用的消息不是用户消息"
    return None


# ---------------------------------------------------------------------------
# 读取辅助：主对话 / 后台的真实来源
# ---------------------------------------------------------------------------

_MESSAGE_PREVIEW = 60
_TOOL_PREVIEW = 80


def message_entries(conn: sqlite3.Connection, turn_id: str) -> list[EvidenceEntry]:
    """本轮已经落库的 user / system 消息（assistant 也列出，供区分角色）。"""
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE turn_id = ? "
        "ORDER BY created_at, id",
        (str(turn_id or ""),),
    ).fetchall()
    return [
        EvidenceEntry(
            id=str(row["id"]),
            kind=KIND_MESSAGE,
            label=f"{row['role']}: {str(row['content'] or '')[:_MESSAGE_PREVIEW]}",
            role=str(row["role"] or "") or None,
        )
        for row in rows
    ]


def attachment_entries(conn: sqlite3.Connection, turn_id: str) -> list[EvidenceEntry]:
    """本轮真实绑定的附件（只列程序记录，不推断正文）。"""
    rows = conn.execute(
        "SELECT id, original_name, kind, state FROM attachments WHERE turn_id = ? "
        "ORDER BY created_at, id",
        (str(turn_id or ""),),
    ).fetchall()
    return [
        EvidenceEntry(
            id=str(row["id"]),
            kind=KIND_ATTACHMENT,
            label=f"{row['original_name'] or row['id']}（{row['kind'] or 'file'}/{row['state'] or '?'}）",
        )
        for row in rows
    ]


def tool_result_entries(
    conn: sqlite3.Connection, turn_id: str, *, limit: int = 50
) -> list[EvidenceEntry]:
    """本轮**真实完成**（status=success）的工具调用；失败 / 取消不算依据。"""
    rows = conn.execute(
        "SELECT id, tool_name, output FROM tool_records "
        "WHERE turn_id = ? AND status = 'success' ORDER BY created_at, seq ASC LIMIT ?",
        (str(turn_id or ""), max(1, int(limit))),
    ).fetchall()
    return [
        EvidenceEntry(
            id=str(row["id"]),
            kind=KIND_TOOL_RESULT,
            label=f"{row['tool_name']}: {str(row['output'] or '')[:_TOOL_PREVIEW]}",
        )
        for row in rows
    ]


def knowledge_entries(snapshot: Iterable[Mapping[str, Any]] | None) -> list[EvidenceEntry]:
    """本轮注入过且 state=active 的知识（快照只来自程序）。"""
    out: list[EvidenceEntry] = []
    for item in snapshot or ():
        item_id = str(item.get("item_id") or item.get("id") or "").strip()
        if not item_id:
            continue
        out.append(
            EvidenceEntry(
                id=item_id,
                kind=KIND_KNOWLEDGE,
                label=str(item.get("content") or "")[:MAX_EVIDENCE_LABEL],
            )
        )
    return out


def entity_entries(cards: Iterable[Any] | None) -> list[EvidenceEntry]:
    """本轮注入 / 命中的实体（接受 EntityCard 对象或 {id, name} 字典）。"""
    out: list[EvidenceEntry] = []
    for card in cards or ():
        if isinstance(card, Mapping):
            card_id = str(card.get("id") or "").strip()
            name = str(card.get("name") or "")
        else:
            card_id = str(getattr(card, "id", "") or "").strip()
            name = str(getattr(card, "name", "") or "")
        if not card_id:
            continue
        out.append(EvidenceEntry(id=card_id, kind=KIND_ENTITY, label=name))
    return out


def build_turn_catalog(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    message: str | None = None,
    knowledge_snapshot: Iterable[Mapping[str, Any]] | None = None,
    entity_cards: Iterable[Any] | None = None,
) -> EvidenceCatalog:
    """主对话一轮的完整证据目录（消息 / 附件 / 已完成工具 / 注入知识 / 实体）。"""
    catalog = EvidenceCatalog()
    catalog.extend(message_entries(conn, turn_id))
    catalog.extend(attachment_entries(conn, turn_id))
    catalog.extend(tool_result_entries(conn, turn_id))
    catalog.extend(knowledge_entries(knowledge_snapshot))
    catalog.extend(entity_entries(entity_cards))
    return catalog


def build_background_catalog(
    *,
    fragment_id: str | None = None,
    messages: Iterable[Mapping[str, Any]] | None = None,
    summary: Any | None = None,
    entity_ids: Iterable[Any] | None = None,
    knowledge_ids: Iterable[Any] | None = None,
    extra: Iterable[Any] | None = None,
) -> EvidenceCatalog:
    """后台提炼的证据目录：输入给模型的真实 fragment / 消息 / 实体 / 知识 id。"""
    catalog = EvidenceCatalog()
    if fragment_id:
        catalog.add(EvidenceEntry(id=str(fragment_id), kind=KIND_FRAGMENT, label="片段"))
    for message in messages or ():
        message_id = str(message.get("id") or "").strip()
        if not message_id:
            continue
        catalog.add(
            EvidenceEntry(
                id=message_id,
                kind=KIND_MESSAGE,
                label=f"{message.get('role') or '?'}: "
                f"{str(message.get('content') or '')[:_MESSAGE_PREVIEW]}",
                role=(str(message.get("role") or "").strip() or None),
            )
        )
    if summary is not None:
        summary_id = str(getattr(summary, "id", "") or "")
        if summary_id:
            catalog.add(
                EvidenceEntry(id=summary_id, kind=KIND_SUMMARY, label="片段摘要")
            )
    for entity_id in _clean_ids(entity_ids):
        catalog.add(EvidenceEntry(id=entity_id, kind=KIND_ENTITY, label="实体"))
    for knowledge_id in _clean_ids(knowledge_ids):
        catalog.add(EvidenceEntry(id=knowledge_id, kind=KIND_KNOWLEDGE, label="知识"))
    catalog.extend(extra)
    return catalog
