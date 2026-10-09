"""知识归属范围（M02 / 契约 C5）。

规则（**唯一入口**，管理 API 与注入都按这里判定）：

- `node_ids` 非空 → 这就是它的范围（用户全局 / 话题 / 实体卡）；
- `node_ids` 为空 → **不再等于全局**：
  * 新代码写下的条目由 `attribution_for_new` 显式挂到用户根节点（全局）；
  * 旧的无归属条目无法可靠判定，保持 `active` 但标 `unresolved`，
    由管理界面显示、允许用户改，**不得一律改成全局**；
- `topic_id` 只是兼容字段，不参与范围判定；但它能作为「旧数据到底属于哪个话题」
  的最后一条可靠线索（迁移 3 已把 topic_id 折进 node_ids，这里只做兜底）。

`unresolved` 标记写在 `provenance.scope_unresolved`，不新增表结构；
`scope_version` 标记「这条的归属由新规则判定过」，两类标记都由本模块维护。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from agent.graph.nodes import NodeService

#: 新规则的版本号：>= 它表示归属已被显式判定（不再当旧数据猜测）。
SCOPE_VERSION = 2
SCOPE_VERSION_KEY = "scope_version"
UNRESOLVED_KEY = "scope_unresolved"
UNRESOLVED_REASON_KEY = "scope_unresolved_reason"
GLOBAL_SCOPE_KEY = "scope"
GLOBAL_SCOPE_VALUE = "global"

#: 契约 C5 里用户全局节点的概念 id；本仓库实际用 `user_<hex>`（NodeService.new_id("user")）。
LEGACY_GLOBAL_IDS = {"user", "user:local", "user:self"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_global_node_id(node_id: str | None) -> bool:
    """用户根节点 = 全局范围。

    本仓库的用户根节点 id 由 `NodeService.new_id("user")` 生成（`user_<hex>`），
    契约里写的 `user:<user_id|local>` 也一并认，避免两套写法。
    """
    if not node_id:
        return False
    text = str(node_id)
    return text in LEGACY_GLOBAL_IDS or text.startswith(("user_", "user:"))


def _clean_nodes(node_ids: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    for raw in node_ids or []:
        text = str(raw).strip()
        if text and text not in out:
            out.append(text)
    return out


def _global_intent(provenance: dict | None) -> bool:
    prov = provenance or {}
    try:
        version = int(prov.get(SCOPE_VERSION_KEY) or 0)
    except (TypeError, ValueError):
        version = 0
    if version < SCOPE_VERSION:
        return False
    return str(prov.get(GLOBAL_SCOPE_KEY) or "") == GLOBAL_SCOPE_VALUE


@dataclass(frozen=True)
class ScopeResolution:
    """一条知识的范围判定结果。"""

    #: 真实归属节点（全局 = 用户根节点）；unresolved 时为空
    nodes: list[str] = field(default_factory=list)
    #: 是否用户全局（「你」）
    global_scope: bool = False
    #: 归属是从别的字段派生出来的（存储里没有直接记录），需要写回
    migrated: bool = False
    #: 旧无归属条目：保持不变但仍需用户确认，不参与全局注入
    unresolved: bool = False
    #: 兼容字段原值（仅用于展示/诊断）
    legacy_topic_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.nodes),
            "global": self.global_scope,
            "migrated": self.migrated,
            "unresolved": self.unresolved,
            "legacy_topic_id": self.legacy_topic_id,
        }


def existing_user_global_node_id(conn: sqlite3.Connection) -> str | None:
    """只读地取用户根节点：注入这种读路径不得顺手建节点。"""
    row = conn.execute(
        "SELECT id FROM nodes WHERE type = 'user' ORDER BY created_at LIMIT 1"
    ).fetchone()
    return str(row["id"]) if row is not None else None


def user_global_node_id(conn: sqlite3.Connection) -> str:
    """取（必要时建）用户根节点：写路径用它落地「全局（你）」。"""
    return NodeService(conn).get_or_create_user_root().id


def attribution_for_new(
    conn: sqlite3.Connection, *, node_ids: Iterable[Any] | None = None, topic_id: str | None = None
) -> tuple[list[str], bool]:
    """新建知识的归属：返回 `(nodes, global_scope)`。

    - 显式给了 node_ids → 原样使用；
    - 只给了兼容字段 topic_id → 挂到该话题（旧调用方的语义保持不变）；
    - 都没给 → 用户全局节点（M02 默认表单路径），而不是「无归属」。
    """
    nodes = _clean_nodes(node_ids)
    if nodes:
        return nodes, any(is_global_node_id(n) for n in nodes)
    if topic_id:
        text = str(topic_id).strip()
        return ([text] if text else []), is_global_node_id(text)
    return [user_global_node_id(conn)], True


def _decide(
    node_ids: Iterable[Any] | None, topic_id: str | None, provenance: dict | None
) -> str:
    """三处判定共用的决策：nodes | topic_fallback | global_intent | unresolved。"""
    if _clean_nodes(node_ids):
        return "nodes"
    prov = provenance or {}
    if prov.get(UNRESOLVED_KEY):
        return "unresolved"
    if topic_id:
        return "topic_fallback"
    if _global_intent(prov):
        return "global_intent"
    return "unresolved"


def is_unresolved(
    *, node_ids: Iterable[Any] | None = None, topic_id: str | None = None, provenance: dict | None = None
) -> bool:
    """纯判定：不看数据库，`_from_row` 等只读路径可以安全调用。"""
    return _decide(node_ids, topic_id, provenance) == "unresolved"


def resolve_scope(
    conn: sqlite3.Connection,
    *,
    node_ids: Iterable[Any] | None = None,
    topic_id: str | None = None,
    provenance: dict | None = None,
) -> ScopeResolution:
    """判定一条知识的真实范围。管理 API、注入、纠正工具共用。"""
    mode = _decide(node_ids, topic_id, provenance)
    if mode == "nodes":
        nodes = _clean_nodes(node_ids)
        return ScopeResolution(
            nodes=nodes,
            global_scope=any(is_global_node_id(n) for n in nodes),
            migrated=False,
            unresolved=False,
            legacy_topic_id=topic_id or None,
        )
    if mode == "unresolved":
        return ScopeResolution(
            nodes=[], global_scope=False, migrated=False, unresolved=True, legacy_topic_id=topic_id or None
        )
    if mode == "topic_fallback":
        text = str(topic_id).strip()
        nodes = [text] if text else []
        return ScopeResolution(
            nodes=nodes,
            global_scope=is_global_node_id(text),
            migrated=True,
            unresolved=not nodes,
            legacy_topic_id=text or None,
        )
    # global_intent：新规则判过、但节点没写进存储的条目（异常/半迁移），补齐到全局
    return ScopeResolution(
        nodes=[user_global_node_id(conn)],
        global_scope=True,
        migrated=True,
        unresolved=False,
        legacy_topic_id=topic_id or None,
    )


def resolve_for_item(conn: sqlite3.Connection, item: Any) -> ScopeResolution:
    """`KnowledgeItem`（或任意带 node_ids/topic_id/provenance 的对象）的范围判定。"""
    return resolve_scope(
        conn,
        node_ids=getattr(item, "node_ids", None),
        topic_id=getattr(item, "topic_id", None),
        provenance=getattr(item, "provenance", None),
    )


def is_injectable(conn: sqlite3.Connection, item: Any) -> bool:
    """能否进入注入面：必须有真实归属；unresolved 条目不得当成全局注入。"""
    resolution = resolve_for_item(conn, item)
    return bool(resolution.nodes) and not resolution.unresolved


# -- 归属标记的读写（管理 API 用）-------------------------------------------


def _load_provenance(conn: sqlite3.Connection, knowledge_id: str) -> dict | None:
    row = conn.execute(
        "SELECT provenance FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["provenance"] or "{}")
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _store_provenance(conn: sqlite3.Connection, knowledge_id: str, provenance: dict) -> None:
    conn.execute(
        "UPDATE knowledge SET provenance = ?, updated_at = ? WHERE id = ?",
        (json.dumps(provenance, ensure_ascii=False), _now(), knowledge_id),
    )


def mark_unresolved(
    conn: sqlite3.Connection, knowledge_id: str, *, reason: str = "legacy_unattributed"
) -> dict | None:
    """把旧无归属条目标成 unresolved（保持 active、保留原文）。"""
    prov = _load_provenance(conn, knowledge_id)
    if prov is None:
        return None
    prov[UNRESOLVED_KEY] = True
    prov[UNRESOLVED_REASON_KEY] = reason
    prov["scope_unresolved_at"] = _now()
    _store_provenance(conn, knowledge_id, prov)
    return prov


def clear_unresolved(conn: sqlite3.Connection, knowledge_id: str) -> dict | None:
    """用户改过范围：清掉 unresolved 标记并记下新规则版本。"""
    prov = _load_provenance(conn, knowledge_id)
    if prov is None:
        return None
    prov.pop(UNRESOLVED_KEY, None)
    prov.pop(UNRESOLVED_REASON_KEY, None)
    prov.pop("scope_unresolved_at", None)
    prov[SCOPE_VERSION_KEY] = SCOPE_VERSION
    _store_provenance(conn, knowledge_id, prov)
    return prov
