"""Knowledge lifecycle state machine.

States: draft -> pending_review -> verified -> active -> expired | revoked.
Only active entries enter the injection surface.

版本链（契约 C4/C5/C6；R02/R03）——本模块是**唯一入口**：

- 身份：`knowledge_id`（版本行）、`chain_id`（链）、`version`（链内序号）、
  `supersedes_id`（上一版）；「当前有效」= 该链内唯一 `status='active'`；
- 所有会改变「谁是当前版本」的写入都在**单一真实事务**里完成：
  版本核对 → 撤销旧行 → 插入并激活新行 → 写历史关联；
  连接是 autocommit（`isolation_level=None`），所以事务用 `BEGIN IMMEDIATE`
  显式开启（已在事务中时退化为 SAVEPOINT）——`with conn:` 在这里是空操作，不可用；
- 任一步失败整体回滚，旧版本保持 active、新版本不会「误标成功」；
- 版本不符抛 `VersionConflict`（HTTP 409 语义），**绝不新增第二个 active**；
- 历史库已有同链重复 active 时保守处理：原文与历史都保留，
  只读地把冲突列出来给管理侧看（`list_duplicate_active_chains`），不擅自丢弃。

列适配：迁移 24 会给 knowledge 补 `chain_id` / `version`。列存在时以列为准
（读写都用列）；列还没落地（例如尚未合并迁移的 worktree）时，用 `supersedes_id`
链派生同样的语义，保证两套环境行为一致。
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator

from agent.knowledge import scope as scope_mod

CATEGORIES = {
    "user_profile",
    "agent_self",
    "goal",
    "general_fact",
    "tool_experience",
}
HIGH_IMPACT_CATEGORIES = {"user_profile", "agent_self", "goal"}
LOW_IMPACT_CATEGORIES = CATEGORIES - HIGH_IMPACT_CATEGORIES

_CHAIN_COLUMNS = ("chain_id", "version")


class KnowledgeState(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


_TERMINAL_STATES = {KnowledgeState.EXPIRED, KnowledgeState.REVOKED}

_TRANSITIONS: dict[KnowledgeState, set[KnowledgeState]] = {
    KnowledgeState.DRAFT: {KnowledgeState.PENDING_REVIEW, KnowledgeState.REVOKED},
    KnowledgeState.PENDING_REVIEW: {KnowledgeState.VERIFIED, KnowledgeState.DRAFT, KnowledgeState.REVOKED},
    KnowledgeState.VERIFIED: {KnowledgeState.ACTIVE, KnowledgeState.REVOKED},
    KnowledgeState.ACTIVE: {KnowledgeState.EXPIRED, KnowledgeState.REVOKED},
    KnowledgeState.EXPIRED: set(),
    KnowledgeState.REVOKED: set(),
}


def impact_of(category: str) -> str:
    return "high" if category in HIGH_IMPACT_CATEGORIES else "low"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return f"kn_{uuid.uuid4().hex[:12]}"


# -- 错误 -------------------------------------------------------------------


class KnowledgeNotFound(KeyError):
    """条目不存在（HTTP 404 语义）。"""

    def __init__(self, knowledge_id: str) -> None:
        self.knowledge_id = knowledge_id
        super().__init__(knowledge_id)


class VersionConflict(ValueError):
    """版本不符 / 链上已有更新的当前版本（HTTP 409 语义）。

    调用方（管理 API）据此返回 409，并可直接用 `current_id` / `current_version`
    组装响应体；绝不因为冲突而丢弃原文或历史。
    """

    status_code = 409

    def __init__(
        self,
        knowledge_id: str,
        *,
        expected_version: int | None = None,
        current_id: str | None = None,
        current_version: int | None = None,
        reason: str = "",
    ) -> None:
        self.knowledge_id = knowledge_id
        self.expected_version = expected_version
        self.current_id = current_id
        self.current_version = current_version
        self.reason = reason or "版本冲突"
        super().__init__(self.reason)

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "conflict": True,
            "knowledge_id": self.knowledge_id,
            "expected_version": self.expected_version,
            "current_id": self.current_id,
            "current_version": self.current_version,
            "error": self.reason,
        }


@dataclass(frozen=True)
class DeactivateOutcome:
    """删除/停用结果：包含「到底删没删」的准确反馈。"""

    item: KnowledgeItem
    deleted: bool
    already_inactive: bool
    current_id: str | None
    current_version: int | None

    def to_dict(self) -> dict:
        return {
            "id": self.item.id if self.item else None,
            "state": self.item.state.value if self.item else None,
            "deleted": self.deleted,
            "already_inactive": self.already_inactive,
            "current_id": self.current_id,
            "current_version": self.current_version,
        }


@dataclass(frozen=True)
class DuplicateActiveChain:
    """同链多 active 的历史冲突（只读展示，不自动修）。"""

    chain_id: str
    active_ids: list[str]
    versions: list[int]
    contents: list[str]

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "active_ids": list(self.active_ids),
            "versions": list(self.versions),
            "contents": list(self.contents),
        }


# -- 事务 -------------------------------------------------------------------


def _is_locked(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


_SAVEPOINT_SEQ = itertools.count(1)


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """真实事务边界（autocommit 连接上必须显式 BEGIN）。

    - 不在事务中：`BEGIN IMMEDIATE` 先拿写锁，两条连接并发纠正时只会有一个
      拿到锁并成功，另一个在锁释放后看到新版本 → 明确冲突；
    - 已在事务中：退化为 SAVEPOINT，出错只回滚本段，外层事务与调用方的
      「全成功或全失败」语义都不被破坏。
    """
    if conn.in_transaction:
        name = f"rm_b_sp_{next(_SAVEPOINT_SEQ)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _is_locked(exc):
            raise VersionConflict("", reason="数据库忙：并发修改未取得写锁，请重试") from exc
        raise
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# -- 身份与链 ---------------------------------------------------------------


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def knowledge_columns(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(knowledge)").fetchall()}
    except sqlite3.Error:
        return set()


def _has_chain_columns(conn: sqlite3.Connection) -> bool:
    # 迁移 24 落地后列一定存在；这里每次真实探测，避免「缓存住旧结论」后
    # 明明有列还走派生分支（两套语义）。
    return set(_CHAIN_COLUMNS) <= knowledge_columns(conn)


def derive_chain(conn: sqlite3.Connection, knowledge_id: str) -> tuple[str, int]:
    """沿 supersedes_id 上溯，返回 `(链根 id, 该行版本号)`。只读、环形安全。"""
    version = 1
    current = knowledge_id
    seen = {knowledge_id}
    for _ in range(10_000):
        row = conn.execute(
            "SELECT supersedes_id FROM knowledge WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            break
        parent = row["supersedes_id"]
        if not parent or parent in seen:
            break
        if conn.execute("SELECT 1 FROM knowledge WHERE id = ?", (parent,)).fetchone() is None:
            break
        seen.add(parent)
        current = parent
        version += 1
    return current, version


def chain_of(conn: sqlite3.Connection, knowledge_id: str) -> list[KnowledgeItem]:
    """该条目所在版本链的全部成员（不含 order 保证）。"""
    svc = KnowledgeService(conn)
    start = svc.get(knowledge_id)
    if start is None:
        return []
    if _has_chain_columns(conn) and start.chain_id:
        ids: set[str] = set()
        for row in conn.execute(
            "SELECT id FROM knowledge WHERE chain_id = ?", (start.chain_id,)
        ).fetchall():
            ids.add(str(row["id"]))
        # 迁移 26 只加列、不回填：历史行的 chain_id 仍可能是空的，旧写入路径也可能
        # 留下 chain_id = NULL 的行。它们属于同一条链，绝不能被列查询漏掉——一旦漏掉，
        # 同链的第二个 active 就看不见，R02「同一版本链不能有两个当前版本」会失效。
        for row in conn.execute(
            "SELECT id FROM knowledge WHERE chain_id IS NULL OR chain_id = ''"
        ).fetchall():
            orphan_id = str(row["id"])
            if orphan_id in ids:
                continue
            if derive_chain(conn, orphan_id)[0] == start.chain_id:
                ids.add(orphan_id)
        if ids:
            members = [svc.get(i) for i in ids]
            return [m for m in members if m is not None]
    ids: set[str] = {knowledge_id}
    # 祖先：先找到链根
    root = knowledge_id
    for _ in range(10_000):
        row = conn.execute(
            "SELECT supersedes_id FROM knowledge WHERE id = ?", (root,)
        ).fetchone()
        if row is None:
            break
        parent = row["supersedes_id"]
        if not parent or parent in ids:
            break
        if conn.execute("SELECT 1 FROM knowledge WHERE id = ?", (parent,)).fetchone() is None:
            break
        ids.add(str(parent))
        root = str(parent)
    # 从链根向下 BFS：覆盖**兄弟分叉**（同链两个 v2 也算同一条链，
    # 否则派生分支会漏掉「另一条同号 active」，正是「新增第二个 active」的漏洞）
    frontier = [root]
    while frontier:
        nxt: list[str] = []
        for node_id in frontier:
            for row in conn.execute(
                "SELECT id FROM knowledge WHERE supersedes_id = ?", (node_id,)
            ).fetchall():
                child = str(row["id"])
                if child not in ids:
                    ids.add(child)
                    nxt.append(child)
        frontier = nxt
    members = [svc.get(i) for i in ids]
    return [m for m in members if m is not None]


def resolve_current(conn: sqlite3.Connection, knowledge_id: str) -> KnowledgeItem | None:
    """该条目所在链的「当前版本」。

    优先唯一 active；历史异常（无 active）时退回链上版本号最大的行。
    """
    svc = KnowledgeService(conn)
    item = svc.get(knowledge_id)
    if item is None:
        return None
    members = chain_of(conn, knowledge_id)
    if not members:
        return item
    actives = [m for m in members if m.state is KnowledgeState.ACTIVE]
    pool = actives or members
    return max(pool, key=lambda m: (int(m.version), m.created_at or ""))


def list_duplicate_active_chains(conn: sqlite3.Connection) -> list[DuplicateActiveChain]:
    """只读列出「同一条链有多个 active」的历史冲突，供管理侧看见。

    不删内容、不改历史、不自动修；新写入由 `activate` / `revise_atomic`
    的条件校验保证不再产生新的重复。
    """
    rows = conn.execute(
        "SELECT * FROM knowledge WHERE state = 'active' ORDER BY id"
    ).fetchall()
    buckets: dict[str, list[str]] = {}
    for row in rows:
        chain_id = _row_get(row, "chain_id") or derive_chain(conn, str(row["id"]))[0]
        buckets.setdefault(str(chain_id), []).append(str(row["id"]))
    svc = KnowledgeService(conn)
    out: list[DuplicateActiveChain] = []
    for chain_id, ids in buckets.items():
        if len(ids) < 2:
            continue
        items = [svc.get(i) for i in ids]
        present = [i for i in items if i is not None]
        out.append(
            DuplicateActiveChain(
                chain_id=chain_id,
                active_ids=[i.id for i in present],
                versions=[int(i.version) for i in present],
                contents=[i.content for i in present],
            )
        )
    return sorted(out, key=lambda d: (-len(d.active_ids), d.chain_id))


# -- 行写入 -----------------------------------------------------------------


def _revoke_row(
    conn: sqlite3.Connection, knowledge_id: str, *, superseded_by: str | None = None
) -> None:
    """撤销一行（保留原文与历史）；`superseded_by` 记下被谁取代。"""
    row = conn.execute(
        "SELECT provenance FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    if row is None:
        return
    try:
        prov = json.loads(row["provenance"] or "{}")
    except (TypeError, ValueError):
        prov = {}
    if not isinstance(prov, dict):
        prov = {}
    now = _now()
    if superseded_by:
        prov["superseded_by"] = superseded_by
        prov["superseded_at"] = now
    conn.execute(
        "UPDATE knowledge SET state = 'revoked', provenance = ?, updated_at = ? WHERE id = ?",
        (json.dumps(prov, ensure_ascii=False), now, knowledge_id),
    )


def _topic_like(node_ids: list[str]) -> str | None:
    for node_id in node_ids:
        if str(node_id).startswith(("topic_", "topic:")):
            return str(node_id)
    return None


def _insert_active(
    conn: sqlite3.Connection,
    *,
    knowledge_id: str,
    category: str,
    content: str,
    supersedes_id: str | None,
    chain_id: str,
    version: int,
    node_ids: list[str],
    entity_ids: list[str],
    provenance: dict,
    confidence: float | None,
    export: bool,
    now: str,
) -> None:
    """插入一行并直接置为 active（调用方必须已在事务内）。"""
    columns = [
        "id",
        "category",
        "state",
        "content",
        "supersedes_id",
        "topic_id",
        "node_ids",
        "entity_ids",
        "provenance",
        "confidence",
        "created_at",
        "updated_at",
        "activated_at",
        "export",
    ]
    values: list[Any] = [
        knowledge_id,
        category,
        KnowledgeState.ACTIVE.value,
        content,
        supersedes_id,
        _topic_like(node_ids),
        json.dumps(node_ids, ensure_ascii=False),
        json.dumps(entity_ids, ensure_ascii=False),
        json.dumps(provenance, ensure_ascii=False),
        confidence,
        now,
        now,
        now,
        1 if export else 0,
    ]
    if _has_chain_columns(conn):
        columns[5:5] = ["chain_id", "version"]
        values[5:5] = [chain_id, int(version)]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO knowledge ({', '.join(columns)}) VALUES ({placeholders})", values
    )


@dataclass(frozen=True)
class KnowledgeItem:
    id: str
    category: str
    state: KnowledgeState
    content: str
    supersedes_id: str | None
    topic_id: str | None
    node_ids: list[str]
    entity_ids: list[str]
    provenance: dict
    confidence: float | None
    created_at: str
    updated_at: str
    activated_at: str | None
    expired_at: str | None
    export: bool
    #: 版本链身份（契约 C4）。列未落地时由 supersedes 链派生。
    chain_id: str = ""
    version: int = 1
    #: M02：旧无归属条目（保持 active 但不参与全局注入，管理界面可见可改）
    scope_unresolved: bool = False
    #: 是否用户全局（「你」）
    scope_global: bool = False


# -- 服务 -------------------------------------------------------------------


class KnowledgeService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        *,
        category: str,
        content: str,
        topic_id: str | None = None,
        node_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        provenance: dict | None = None,
        export: bool = False,
        supersedes_id: str | None = None,
    ) -> KnowledgeItem:
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        if not content.strip():
            raise ValueError("knowledge content must not be empty")
        # M02：默认（未关联话题、也没给节点）= 用户全局节点；topic_id 只是兼容字段。
        nodes, global_scope = scope_mod.attribution_for_new(
            self.conn, node_ids=node_ids, topic_id=topic_id
        )
        prov = dict(provenance or {})
        prov.setdefault(scope_mod.SCOPE_VERSION_KEY, scope_mod.SCOPE_VERSION)
        # attribution_for_new 一定给出真实归属（显式节点 / 兼容话题 / 用户全局），
        # 所以新条目不会被判成「旧无归属」。
        prov.pop(scope_mod.UNRESOLVED_KEY, None)
        if global_scope:
            prov.setdefault(scope_mod.GLOBAL_SCOPE_KEY, scope_mod.GLOBAL_SCOPE_VALUE)
        else:
            prov.pop(scope_mod.GLOBAL_SCOPE_KEY, None)
        knowledge_id = new_id()
        now = _now()
        chain_id = knowledge_id
        version = 1
        if supersedes_id is not None:
            target = self.get(supersedes_id)
            if target is None:
                raise ValueError(f"supersedes target not found: {supersedes_id}")
            chain_id = target.chain_id or target.id
            version = int(target.version) + 1
        columns = [
            "id",
            "category",
            "state",
            "content",
            "supersedes_id",
            "topic_id",
            "node_ids",
            "entity_ids",
            "provenance",
            "created_at",
            "updated_at",
            "export",
        ]
        values: list[Any] = [
            knowledge_id,
            category,
            KnowledgeState.DRAFT.value,
            content,
            supersedes_id,
            _topic_like(nodes) or (str(topic_id) if topic_id else None),
            json.dumps(nodes, ensure_ascii=False),
            json.dumps(entity_ids or [], ensure_ascii=False),
            json.dumps(prov, ensure_ascii=False),
            now,
            now,
            1 if export else 0,
        ]
        if _has_chain_columns(self.conn):
            columns[5:5] = ["chain_id", "version"]
            values[5:5] = [chain_id, int(version)]
        placeholders = ", ".join("?" for _ in columns)
        self.conn.execute(
            f"INSERT INTO knowledge ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        item = self.get(knowledge_id)
        assert item is not None
        return item

    def submit(self, knowledge_id: str) -> KnowledgeItem:
        return self._transition(knowledge_id, KnowledgeState.PENDING_REVIEW)

    def reject(self, knowledge_id: str) -> KnowledgeItem:
        """pending_review -> draft (sent back for revision)."""
        return self._transition(knowledge_id, KnowledgeState.DRAFT)

    def verify(
        self, knowledge_id: str, *, verified_by: str = "system"
    ) -> KnowledgeItem:
        """pending_review -> verified.

        High-impact categories (user_profile / agent_self / goal) require an
        explicit user confirmation; low-impact categories may be
        auto-verified by the system.
        """
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        if item.state != KnowledgeState.PENDING_REVIEW:
            raise ValueError(f"cannot verify from state {item.state.value}")
        if impact_of(item.category) == "high" and verified_by != "user":
            raise PermissionError(
                f"high-impact category '{item.category}' requires user confirmation"
            )
        return self._transition(
            knowledge_id, KnowledgeState.VERIFIED, extra={"confidence": 0.9}
        )

    def activate(
        self, knowledge_id: str, *, expected_version: int | None = None
    ) -> KnowledgeItem:
        """verified -> active；同一事务内撤销同链旧版本，保证链内唯一 active。

        版本不符 / 链上已有同号或更新的版本 → `VersionConflict`（409 语义），
        绝不新增第二个 active，也不把旧版本悄悄激活。
        """
        with atomic(self.conn):
            item = self.get(knowledge_id)
            if item is None:
                raise KnowledgeNotFound(knowledge_id)
            if item.state is KnowledgeState.ACTIVE:
                return item  # 幂等：已经是当前有效版本
            if item.state not in _TRANSITIONS or KnowledgeState.ACTIVE not in _TRANSITIONS[item.state]:
                raise ValueError(f"cannot activate from state {item.state.value}")
            if expected_version is not None and int(expected_version) != int(item.version):
                raise VersionConflict(
                    knowledge_id,
                    expected_version=int(expected_version),
                    current_id=item.id,
                    current_version=int(item.version),
                    reason=f"版本不符：期望 v{expected_version}，该行是 v{item.version}",
                )
            members = chain_of(self.conn, knowledge_id)
            conflicts = [
                m
                for m in members
                if m.id != item.id
                and (int(m.version) > int(item.version)
                     or (int(m.version) == int(item.version) and m.state is KnowledgeState.ACTIVE))
            ]
            if conflicts:
                current = max(conflicts, key=lambda m: int(m.version))
                raise VersionConflict(
                    knowledge_id,
                    expected_version=expected_version,
                    current_id=current.id,
                    current_version=int(current.version),
                    reason="该链已有更新的当前版本，激活被拒绝",
                )
            for member in members:
                if member.id != item.id and member.state is KnowledgeState.ACTIVE:
                    _revoke_row(self.conn, member.id, superseded_by=item.id)
            self._set_state(item.id, KnowledgeState.ACTIVE, extra={"activated_at": _now()})
        return self.get(knowledge_id)  # type: ignore[return-value]

    def revoke(
        self, knowledge_id: str, *, expected_version: int | None = None
    ) -> KnowledgeItem:
        """撤销：active 行必须核对「它仍是当前版本」，否则明确冲突。

        非 active 行（草稿/待审/已验证）只是归档，不涉及版本链。
        """
        return deactivate_atomic(
            self.conn, knowledge_id, expected_version=expected_version
        ).item

    def expire(self, knowledge_id: str) -> KnowledgeItem:
        """active -> expired."""
        return self._transition(
            knowledge_id, KnowledgeState.EXPIRED, extra={"expired_at": _now()}
        )

    def mark_ended(self, knowledge_id: str, *, reason: str = "user_confirmed") -> KnowledgeItem:
        """把一条知识标成「已结束」：**不改状态机**，只降低它的使用权重。

        和 `expire` 的区别：过期与撤销是"不再算数"，从注入面彻底消失；结束是
        "不再是当前状态"，主注入不再常驻，只有在和本轮内容相关时才作为参考出现
        （权重见 `services/injection.knowledge_score`）。
        """
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        provenance = dict(item.provenance or {})
        provenance["ended_at"] = _now()
        provenance["ended_reason"] = reason
        self.conn.execute(
            "UPDATE knowledge SET provenance = ?, updated_at = ? WHERE id = ?",
            (json.dumps(provenance, ensure_ascii=False), _now(), knowledge_id),
        )
        marked = self.get(knowledge_id)
        assert marked is not None
        return marked

    def resume(self, knowledge_id: str) -> KnowledgeItem:
        """撤销「已结束」：重新当作当前状态使用。"""
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        provenance = dict(item.provenance or {})
        provenance.pop("ended_at", None)
        provenance.pop("ended_reason", None)
        self.conn.execute(
            "UPDATE knowledge SET provenance = ?, updated_at = ? WHERE id = ?",
            (json.dumps(provenance, ensure_ascii=False), _now(), knowledge_id),
        )
        resumed = self.get(knowledge_id)
        assert resumed is not None
        return resumed

    def set_scope(
        self, knowledge_id: str, *, node_ids: list[str], topic_id: str | None = None
    ) -> KnowledgeItem:
        """改「这条知识管多大范围」：挂到「你」= 全局，挂到话题 = 只在该话题生效。

        - 传入的节点就是**真实归属**（不再用 topic_id 判断范围）；
        - 调用即视为用户确认过范围 → 清掉 unresolved 标记；
        - 传空列表 = 保持/标记无归属，绝不静默改成全局。
        """
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        nodes = [str(n) for n in (node_ids or []) if str(n).strip()]
        prov = dict(item.provenance or {})
        if nodes:
            prov.pop(scope_mod.UNRESOLVED_KEY, None)
            prov.pop(scope_mod.UNRESOLVED_REASON_KEY, None)
            prov.pop("scope_unresolved_at", None)
            prov[scope_mod.SCOPE_VERSION_KEY] = scope_mod.SCOPE_VERSION
            if any(scope_mod.is_global_node_id(n) for n in nodes):
                prov[scope_mod.GLOBAL_SCOPE_KEY] = scope_mod.GLOBAL_SCOPE_VALUE
            else:
                prov.pop(scope_mod.GLOBAL_SCOPE_KEY, None)
        else:
            prov[scope_mod.UNRESOLVED_KEY] = True
            prov[scope_mod.UNRESOLVED_REASON_KEY] = "scope_cleared"
        compat_topic = str(topic_id) if topic_id else _topic_like(nodes)
        with atomic(self.conn):
            self.conn.execute(
                "UPDATE knowledge SET node_ids = ?, topic_id = ?, provenance = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    json.dumps(nodes, ensure_ascii=False),
                    compat_topic,
                    json.dumps(prov, ensure_ascii=False),
                    _now(),
                    knowledge_id,
                ),
            )
        updated = self.get(knowledge_id)
        assert updated is not None
        return updated

    # -- 版本链原子写（契约 C4；管理 API / 纠正工具共用）----------------------

    def resolve_current(self, knowledge_id: str) -> KnowledgeItem | None:
        return resolve_current(self.conn, knowledge_id)

    def revise_atomic(
        self,
        target_id: str,
        expected_version: int | None = None,
        fields: dict | None = None,
        *,
        source: str | None = None,
        actor: str = "user",
    ) -> KnowledgeItem:
        return revise_atomic(
            self.conn, target_id, expected_version, fields, source=source, actor=actor
        )

    def deactivate_atomic(
        self, knowledge_id: str, *, expected_version: int | None = None
    ) -> DeactivateOutcome:
        return deactivate_atomic(
            self.conn, knowledge_id, expected_version=expected_version
        )

    def duplicate_active_chains(self) -> list[DuplicateActiveChain]:
        return list_duplicate_active_chains(self.conn)

    # -- reading ----------------------------------------------------------

    def get(self, knowledge_id: str) -> KnowledgeItem | None:
        row = self.conn.execute(
            "SELECT * FROM knowledge WHERE id = ?", (knowledge_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def history(self, knowledge_id: str) -> list[KnowledgeItem]:
        """整条版本链（oldest first）——从任一版本都能看到替换历史。"""
        members = chain_of(self.conn, knowledge_id)
        if not members:
            return []
        return sorted(members, key=lambda m: (int(m.version), m.created_at or ""))

    def list_items(
        self,
        category: str | None = None,
        state: str | None = None,
        q: str | None = None,
    ) -> list[KnowledgeItem]:
        """列出知识条目，支持分类/状态/关键词过滤，按 updated_at 倒序。"""
        sql = "SELECT * FROM knowledge WHERE 1=1"
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if state:
            sql += " AND state = ?"
            params.append(state)
        if q:
            sql += " AND content LIKE ?"
            params.append(f"%{q}%")
        sql += " ORDER BY updated_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._from_row(r) for r in rows]

    # -- internals --------------------------------------------------------

    def _transition(
        self,
        knowledge_id: str,
        target: KnowledgeState,
        extra: dict[str, Any] | None = None,
    ) -> KnowledgeItem:
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        allowed = _TRANSITIONS[item.state]
        if target not in allowed:
            raise ValueError(
                f"invalid transition {item.state.value} -> {target.value}"
            )
        self._set_state(knowledge_id, target, extra=extra)
        updated = self.get(knowledge_id)
        assert updated is not None
        return updated

    def _set_state(
        self,
        knowledge_id: str,
        state: KnowledgeState,
        confidence: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        sets = ["state = ?", "updated_at = ?"]
        params: list[Any] = [state.value, _now()]
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(confidence)
        for column, value in (extra or {}).items():
            sets.append(f"{column} = ?")
            params.append(value)
        params.append(knowledge_id)
        self.conn.execute(
            f"UPDATE knowledge SET {', '.join(sets)} WHERE id = ?", params
        )

    def _from_row(self, row: sqlite3.Row) -> KnowledgeItem:
        try:
            node_ids = json.loads(row["node_ids"] or "[]")
        except (TypeError, ValueError):
            node_ids = []
        try:
            provenance = json.loads(row["provenance"] or "{}")
        except (TypeError, ValueError):
            provenance = {}
        if not isinstance(provenance, dict):
            provenance = {}
        try:
            entity_ids = json.loads(_row_get(row, "entity_ids") or "[]")
        except (TypeError, ValueError):
            entity_ids = []
        stored_chain = _row_get(row, "chain_id")
        stored_version = _row_get(row, "version")
        if stored_chain and stored_version:
            chain_id, version = str(stored_chain), int(stored_version)
        else:
            chain_id, version = derive_chain(self.conn, str(row["id"]))
        return KnowledgeItem(
            id=row["id"],
            category=row["category"],
            state=KnowledgeState(row["state"]),
            content=row["content"],
            supersedes_id=row["supersedes_id"],
            topic_id=_row_get(row, "topic_id"),
            node_ids=list(node_ids),
            entity_ids=list(entity_ids),
            provenance=provenance,
            confidence=_row_get(row, "confidence"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            activated_at=_row_get(row, "activated_at"),
            expired_at=_row_get(row, "expired_at"),
            export=bool(_row_get(row, "export") or 0),
            chain_id=chain_id,
            version=version,
            scope_unresolved=scope_mod.is_unresolved(
                node_ids=node_ids, topic_id=_row_get(row, "topic_id"), provenance=provenance
            ),
            scope_global=any(scope_mod.is_global_node_id(n) for n in node_ids),
        )


# -- 模块级原子操作（契约 C4 的精确签名）------------------------------------


def revise_atomic(
    conn: sqlite3.Connection,
    target_id: str,
    expected_version: int | None = None,
    fields: dict | None = None,
    *,
    source: str | None = None,
    actor: str = "user",
) -> KnowledgeItem:
    """原子纠正：版本核对 → 撤销旧行 → 插入并激活新行 → 写历史关联。

    `fields` 可含 `content` / `category` / `node_ids` / `entity_ids` /
    `provenance` / `confidence` / `export`；缺省沿用目标行。

    冲突（目标已被取代、不是 active、期望版本不符、同链已有同号/更新版本）
    抛 `VersionConflict`；不存在抛 `KnowledgeNotFound`。任一步失败整体回滚。
    """
    payload = dict(fields or {})
    svc = KnowledgeService(conn)
    with atomic(conn):
        target = svc.get(target_id)
        if target is None:
            raise KnowledgeNotFound(target_id)
        current = resolve_current(conn, target_id)
        if current is None:
            raise KnowledgeNotFound(target_id)
        if current.id != target.id:
            raise VersionConflict(
                target_id,
                expected_version=expected_version,
                current_id=current.id,
                current_version=int(current.version),
                reason="目标已不是该链当前版本（已被取代），未做修改",
            )
        if current.state is not KnowledgeState.ACTIVE:
            raise VersionConflict(
                target_id,
                expected_version=expected_version,
                current_id=current.id,
                current_version=int(current.version),
                reason=f"目标状态为 {current.state.value}，不是当前有效版本",
            )
        if expected_version is not None and int(expected_version) != int(current.version):
            raise VersionConflict(
                target_id,
                expected_version=int(expected_version),
                current_id=current.id,
                current_version=int(current.version),
                reason=f"版本不符：期望 v{expected_version}，当前 v{current.version}",
            )
        content = str(payload.get("content", current.content) or "").strip()
        if not content:
            raise ValueError("knowledge content must not be empty")
        category = str(payload.get("category") or current.category)
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        node_ids = payload.get("node_ids")
        if node_ids is None:
            node_ids = list(current.node_ids)
        node_ids = [str(n) for n in node_ids if str(n).strip()]
        entity_ids = payload.get("entity_ids")
        if entity_ids is None:
            entity_ids = list(current.entity_ids)
        provenance = dict(current.provenance or {})
        provenance.update(dict(payload.get("provenance") or {}))
        provenance["corrected_from"] = current.id
        provenance["revised_by"] = actor
        if source:
            provenance["source"] = source
        if not node_ids:
            # 旧无归属条目：纠正后的新版本同样保持 unresolved，不擅自变成全局
            provenance[scope_mod.UNRESOLVED_KEY] = True
        confidence = payload.get("confidence", current.confidence)
        export = bool(payload.get("export", current.export))
        new_knowledge_id = new_id()
        now = _now()
        members = chain_of(conn, current.id)
        conflicts = [
            m
            for m in members
            if m.id != current.id and int(m.version) >= int(current.version) + 1
        ]
        if conflicts:
            newest = max(conflicts, key=lambda m: int(m.version))
            raise VersionConflict(
                target_id,
                expected_version=expected_version,
                current_id=newest.id,
                current_version=int(newest.version),
                reason="该链已有更新的版本，纠正被拒绝",
            )
        # 1) 撤销旧行（含同链历史重复 active），保留原文与历史关联
        for member in members:
            if member.state is KnowledgeState.ACTIVE:
                _revoke_row(conn, member.id, superseded_by=new_knowledge_id)
        # 2) 插入并激活新行（此步失败 → 整体回滚，旧版本恢复 active）
        _insert_active(
            conn,
            knowledge_id=new_knowledge_id,
            category=category,
            content=content,
            supersedes_id=current.id,
            chain_id=current.chain_id or current.id,
            version=int(current.version) + 1,
            node_ids=node_ids,
            entity_ids=list(entity_ids),
            provenance=provenance,
            confidence=confidence,
            export=export,
            now=now,
        )
    created = svc.get(new_knowledge_id)
    assert created is not None
    return created


def deactivate_atomic(
    conn: sqlite3.Connection,
    knowledge_id: str,
    *,
    expected_version: int | None = None,
) -> DeactivateOutcome:
    """原子删除/停用。

    - active 行：核对「它仍是当前版本」后再撤销；已被新版本取代时抛
      `VersionConflict`（不能删旧条目却声称当前已删）；
    - 已经是 revoked/expired：幂等返回 `already_inactive=True`，准确反馈；
    - 非 active 行（草稿等）：直接归档，不涉及版本链。
    """
    svc = KnowledgeService(conn)
    with atomic(conn):
        item = svc.get(knowledge_id)
        if item is None:
            raise KnowledgeNotFound(knowledge_id)
        current = resolve_current(conn, knowledge_id)
        if item.state in _TERMINAL_STATES:
            return DeactivateOutcome(
                item=item,
                deleted=False,
                already_inactive=True,
                current_id=current.id if current is not None else None,
                current_version=int(current.version) if current is not None else None,
            )
        if expected_version is not None and int(expected_version) != int(item.version):
            raise VersionConflict(
                knowledge_id,
                expected_version=int(expected_version),
                current_id=item.id,
                current_version=int(item.version),
                reason=f"版本不符：期望 v{expected_version}，该行是 v{item.version}",
            )
        if item.state is KnowledgeState.ACTIVE:
            if current is not None and current.id != item.id:
                raise VersionConflict(
                    knowledge_id,
                    expected_version=expected_version,
                    current_id=current.id,
                    current_version=int(current.version),
                    reason="该条目已被新版本取代，未删除；当前有效版本仍保留",
                )
        _revoke_row(conn, item.id)
        removed = svc.get(knowledge_id)
        assert removed is not None
        return DeactivateOutcome(
            item=removed,
            deleted=True,
            already_inactive=False,
            current_id=item.id,
            current_version=int(item.version),
        )
