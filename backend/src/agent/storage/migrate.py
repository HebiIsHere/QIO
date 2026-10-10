"""Lightweight ordered migrations with a schema_version table.

**每条迁移是一个真实事务**：它的全部语句 + 写 schema_version 要么一起生效、要么一起不生效。
这一点在本项目里极易被改坏，所以写清楚：

* `storage/db.py` 把连接设成 autocommit（`isolation_level = None`）；
* autocommit 下 `with conn:` **不会隐式开事务** —— 上下文管理器没有东西可提交/回滚，
  每条语句各自落盘。旧写法正是这样：一次中途失败（或两步之间进程死掉）会留下**半截 DDL**，
  而 schema_version 还停在旧版本。migration 24 是单条 `ALTER TABLE ... ADD COLUMN phases`，
  ALTER 生效、版本没写 → 下次启动重放它 → "duplicate column name: phases" → 应用再也起不来。

所以要显式 BEGIN/COMMIT（`agent.storage.db.transaction()` 就是它）。

另外对**存量库**的半截迁移做窄口径自愈：只有「对象已经存在」（duplicate column / already
exists）才当作「这条已经应用过」，其它任何错误照旧抛 —— 不能拿自愈掩盖真正的迁移错误。
`ALTER TABLE ... ADD COLUMN` 还会先查一次 `PRAGMA table_info`：列已存在就不发这条语句
（与「发出去、报 duplicate column、再跳过」等价，只是少一条误导性的告警）。
补偿迁移（`schema.COMPENSATION_VERSION`）里的列**本来就是允许冗余重放**的，
它的跳过按 debug 记，不算「半截迁移自愈」的告警。

**只看版本号是不够的**（B01）：schema_version 到 29 的存量库照样可能缺
`instances` / `record_owners` / 归属列 / 版本链列。所以本模块还负责：

* `REQUIRED_OBJECTS`：本轮必需的表 / 列 / 索引清单；
* `missing_objects(conn)`：**按对象校验**（不只看版本号），返回缺什么；
* `apply_migrations()` 结束前调用 `verify_required_objects()`：缺了就重放补偿迁移，
  仍缺就抛可读的 `SchemaIncompleteError` —— **不再静默放过**。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from agent.storage.db import transaction
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)

# 只有「对象已经存在」这一类信号才当作「已经应用过」：
# 半截迁移的存量库靠它自愈；其它错误（no such table / 约束失败 / 语法错）一律照旧抛。
_ALREADY_APPLIED_PATTERNS = (
    re.compile(r"duplicate column name", re.IGNORECASE),
    re.compile(r"already exists", re.IGNORECASE),
)

# `ALTER TABLE <t> ADD COLUMN <c> ...`：用来在发语句之前先确认这一列在不在。
_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# 合法标识符（对象名只会来自本仓库的常量清单；仍然校验一次，杜绝拼接注入）。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 本轮（A05/B01 补充修复轮）**必需**的 schema 对象。校验按对象做，不看版本号：
# 版本号到 29 但对象缺失正是本轮要修的主场景。
#
# 诚实边界：这是**按名字**的校验 —— 同名的对象存在但形状不同（例如某个兄弟分支
# 用同一个列名存了别的东西）它判不出来。清单本身要与补偿迁移 30 一一对应。
REQUIRED_OBJECTS: dict[str, tuple[str, ...]] = {
    "tables": ("instances", "record_owners"),
    "columns": (
        "turn_journal.owner_instance_id",
        "derived_tasks.owner_instance_id",
        "derived_tasks.claim_generation",
        "pending_approvals.owner_instance_id",
        "knowledge.chain_id",
        "knowledge.version",
        "entity_cards.revision",
        "entity_cards.field_meta",
    ),
    "indexes": (
        "idx_instances_heartbeat",
        "idx_record_owners_instance",
        "idx_turn_journal_owner",
        "idx_knowledge_chain",
    ),
}


class SchemaIncompleteError(RuntimeError):
    """迁移跑完之后必需的 schema 对象仍然缺失（带上是缺什么，不再静默放过）。"""

    def __init__(self, missing: dict[str, list[str]]) -> None:
        self.missing = {kind: list(names) for kind, names in missing.items()}
        detail = "；".join(
            f"{_KIND_LABELS.get(kind, kind)}缺 {', '.join(names)}"
            for kind, names in self.missing.items()
            if names
        )
        super().__init__(
            f"schema 不完整：{detail}。补偿迁移 {COMPENSATION_VERSION} 已重放仍缺 —— "
            "请检查这个存量库的真实形状（对象也许被手改或半截迁移弄丢了）"
        )


_KIND_LABELS = {"tables": "表", "columns": "列", "indexes": "索引"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _already_applied(exc: sqlite3.Error) -> bool:
    """这条语句要建的对象是不是已经存在（= 这条已经应用过）。"""
    return any(pattern.search(str(exc)) for pattern in _ALREADY_APPLIED_PATTERNS)


def _check_ident(name: str) -> str:
    if not _IDENT_RE.match(str(name)):
        raise ValueError(f"非法的 schema 对象名：{name!r}")
    return str(name)


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    return int(row["v"])


# ---------------------------------------------------------------------------
# 按对象校验（不只看版本号）
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_check_ident(table),)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """表不存在 → 列也不存在（不抛：校验路径要能把「整张表没了」如实报出来）。"""
    name = _check_ident(table)
    if not _table_exists(conn, name):
        return False
    wanted = _check_ident(column)
    return any(str(row[1]) == wanted for row in conn.execute(f"PRAGMA table_info({name})"))


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (_check_ident(index),)
    ).fetchone()
    return row is not None


def missing_objects(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """按**对象**列出缺失的本轮必需对象（三个键总是存在，缺的就是空列表）。"""
    missing: dict[str, list[str]] = {"tables": [], "columns": [], "indexes": []}
    for table in REQUIRED_OBJECTS["tables"]:
        if not _table_exists(conn, table):
            missing["tables"].append(table)
    for spec in REQUIRED_OBJECTS["columns"]:
        table, _, column = str(spec).partition(".")
        if not table or not column:
            raise ValueError(f"REQUIRED_OBJECTS['columns'] 必须是 'table.column' 形式：{spec!r}")
        if not _column_exists(conn, table, column):
            missing["columns"].append(str(spec))
    for index in REQUIRED_OBJECTS["indexes"]:
        if not _index_exists(conn, index):
            missing["indexes"].append(index)
    return missing


def _compensation_statements() -> list[str]:
    for target, statements in MIGRATIONS:
        if target == COMPENSATION_VERSION:
            return list(statements)
    raise RuntimeError(
        f"补偿迁移 {COMPENSATION_VERSION} 不在 MIGRATIONS 里：REQUIRED_OBJECTS 缺失时无处补偿"
    )


def _is_compensation(version: int) -> bool:
    return int(version) == int(COMPENSATION_VERSION)


def _apply_statements(conn: sqlite3.Connection, version: int, statements: list[str]) -> None:
    """在**当前事务里**执行一条迁移的全部语句（宽口径：对象已存在就跳过）。

    调用方负责 BEGIN/COMMIT —— 迁移的原子性靠它，不靠这里。
    """
    for stmt in statements:
        add_column = _ADD_COLUMN_RE.match(str(stmt))
        if add_column is not None and _column_exists(conn, add_column.group(1), add_column.group(2)):
            # 列已经在：这条语句的**效果**已经达成了。补偿迁移的重放属于预期，
            # 按 debug 记；其余情形是「半截迁移自愈」，必须留下 warning（可观测）。
            reason = f"column {add_column.group(2)} already exists"
            if _is_compensation(version):
                logger.debug("迁移 %s：语句已应用过，跳过（%s）", version, reason)
            else:
                logger.warning("迁移 %s：语句已应用过，跳过（%s）", version, reason)
            continue
        try:
            conn.execute(stmt)
        except sqlite3.Error as exc:
            if not _already_applied(exc):
                raise
            # 上一次运行（旧版本代码）已经把这条语句生效过、只是版本号没来得及写：
            # 跳过它，剩下的语句与版本行在同一个事务里补齐 —— 库因此能自愈。
            logger.warning("迁移 %s：语句已应用过，跳过（%s）", version, exc)


def _run_compensation(conn: sqlite3.Connection) -> None:
    """重放补偿迁移（迁移 30）：只建对象、不动数据。

    这里是**尽力而为**：单条语句失败只记日志，最后由按对象的复核决定是否抛
    `SchemaIncompleteError`。所以不能因为它抛异常就放弃复核。
    """
    with transaction(conn):
        for stmt in _compensation_statements():
            try:
                _apply_statements(conn, COMPENSATION_VERSION, [stmt])
            except sqlite3.Error as exc:  # noqa: BLE001 - 补偿失败由复核兜底
                logger.warning("补偿迁移 %s：语句失败（%s）", COMPENSATION_VERSION, exc)
    logger.info("补偿迁移 %s：已重放（缺少本轮必需对象）", COMPENSATION_VERSION)


def verify_required_objects(
    conn: sqlite3.Connection, *, compensate: bool = True
) -> dict[str, list[str]]:
    """迁移序列跑完之后按对象复核；缺了就重放补偿迁移，返回**最终**仍缺的对象。

    返回值就是 :func:`missing_objects` 的形状（三个键总是存在）。
    """
    missing = missing_objects(conn)
    if not any(missing.values()):
        return missing
    logger.warning(
        "schema 缺少本轮必需对象，尝试补偿（迁移 %s）：%s",
        COMPENSATION_VERSION,
        _describe(missing),
    )
    if compensate:
        _run_compensation(conn)
        missing = missing_objects(conn)
    return missing


def _describe(missing: dict[str, list[str]]) -> str:
    return "；".join(
        f"{_KIND_LABELS.get(kind, kind)} {', '.join(names)}"
        for kind, names in missing.items()
        if names
    ) or "（无）"


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the new schema version.

    每条迁移一个事务：语句 + 版本行原子落盘（见模块 docstring 里为什么不能只写 `with conn:`）。
    遇到「对象已存在」的语句时跳过并记一条 warning（存量库的半截迁移自愈），其它错误直接抛。

    最后**按对象**复核本轮必需对象（不只看版本号）：缺了就重放补偿迁移，
    仍缺就抛 :class:`SchemaIncompleteError`（带上缺什么）—— 不再静默放过。
    """
    version = current_version(conn)
    for target, statements in MIGRATIONS:
        if target <= version:
            continue
        with transaction(conn):
            _apply_statements(conn, target, statements)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (target, _now()),
            )
        version = target

    still_missing = verify_required_objects(conn)
    if any(still_missing.values()):
        raise SchemaIncompleteError(still_missing)
    return version


def schema_objects_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """给日志 / 诊断用的只读快照（版本 + 缺什么）。**不改库**。"""
    missing = missing_objects(conn)
    return {
        "version": current_version(conn),
        "complete": not any(missing.values()),
        "missing": missing,
    }
