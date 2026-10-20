"""阶段二 A：知识变化记录（正式生效知识的新增 / 实质修改）。

契约：docs/qio-refactor-contract.md §12.3。

调用方（B）在 KnowledgeService.activate() / revise_atomic() 真正提交成功之后调用
record_effective_change(...)；本模块只写变化日志，不改知识本身、不做审批、也不触发
维护。记录规则（与契约逐条对应）：

* 只记录「正式生效（active）的新增 / 实质修改」——什么时候调用由 B 的领域写入
  路径保证，本模块不读 knowledge 表、不自行判断状态；
* 同一 (chain_id, version) 只记一次（UNIQUE + INSERT OR IGNORE），重复调用返回 False；
* source 属于 MAINTENANCE_ORIGINS（维护自身写入）时不记录，避免维护形成循环触发；
* 空 knowledge_id / 空 chain_id / version <= 0 不记录；
* created_turn_seq 取记录时调度状态里的 turn_seq —— 等待轮数按**最老一条**待处理
  变化计算（新变化加入不重置较早内容的等待），所以每条记录必须带自己创建时的轮次。

本模块可以被 B 的领域写事务包裹调用：storage.db.transaction() 在已有事务里复用
外层事务，变化记录与领域写入因此同生共死（不会出现「知识生效了但日志没落」）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from agent.maintenance.models import MAINTENANCE_ORIGINS
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

#: 调度状态是单行表（迁移 46 里的 id = 1）。
STATE_ID = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_state_row(conn: sqlite3.Connection) -> sqlite3.Row:
    """确保调度状态单行存在并返回它。

    迁移 46 已插入这一行；这里再兜一次是为了「半截迁移 / 单独拿本模块用的测试库」
    也能记录变化（幂等 INSERT OR IGNORE，不覆盖已有计数）。
    """
    conn.execute(
        "INSERT OR IGNORE INTO knowledge_maintenance_state (id, updated_at) VALUES (?, ?)",
        (STATE_ID, _now_iso()),
    )
    row = conn.execute(
        "SELECT turn_seq FROM knowledge_maintenance_state WHERE id = ?", (STATE_ID,)
    ).fetchone()
    if row is None:  # pragma: no cover - 表在但单行被手删才会到这里
        raise RuntimeError("knowledge_maintenance_state 缺少单行状态（id = 1）")
    return row


def record_effective_change(
    conn: sqlite3.Connection,
    *,
    knowledge_id: str,
    chain_id: str,
    version: int,
    source: str,
    occurred_at: str | None = None,
) -> bool:
    """记录一条正式生效的知识变化；返回是否**新记录**（重复 / 无效返回 False）。

    参数：
    * knowledge_id：版本行 id（knowledge 表主键）；
    * chain_id / version：该版本在链内的稳定身份（唯一键，幂等）；
    * source：provenance 来源键（例如 background_extraction；维护自身的
      maintenance_* 不计数）；
    * occurred_at：变化发生时间（UTC ISO 字符串；缺省用当前时间）。
    """
    knowledge = str(knowledge_id or "").strip()
    chain = str(chain_id or "").strip()
    origin = str(source or "").strip()
    try:
        version_number = int(version)
    except (TypeError, ValueError):
        return False
    if not knowledge or not chain or version_number <= 0:
        return False
    if origin in MAINTENANCE_ORIGINS:
        # 维护自身写入不参与变化计数：既不触发自己，也不污染等待轮次。
        logger.debug(
            "不记录维护自身来源的变化：knowledge=%s chain=%s version=%s source=%s",
            knowledge,
            chain,
            version_number,
            origin,
        )
        return False
    if isinstance(occurred_at, str) and occurred_at.strip():
        stamp = occurred_at.strip()
    elif occurred_at is None:
        stamp = _now_iso()
    else:  # 容忍传入 datetime 等其它时间对象（契约是 str | None）
        stamp = str(occurred_at)

    with transaction(conn):
        state = _ensure_state_row(conn)
        created_turn_seq = int(state["turn_seq"] or 0)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO knowledge_change_log"
            " (knowledge_id, chain_id, version, source, occurred_at, created_turn_seq)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (knowledge, chain, version_number, origin, stamp, created_turn_seq),
        )
        recorded = cursor.rowcount == 1

    if recorded:
        logger.debug(
            "记录知识变化：knowledge=%s chain=%s version=%s source=%s turn_seq=%s",
            knowledge,
            chain,
            version_number,
            origin,
            created_turn_seq,
        )
    else:
        logger.debug(
            "知识变化已记录过（幂等跳过）：chain=%s version=%s", chain, version_number
        )
    return recorded
