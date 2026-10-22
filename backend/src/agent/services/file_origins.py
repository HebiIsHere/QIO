"""文件来源（file origin）持久化：谁创建/接收了这份文件，后端自己记账。

契约：docs/r9f-contracts.md §3.4（冻结接口）。
R9 修复问题 C：工作区里的文件来源不可达，模型只能靠路径/关键词猜"这是不是 QIO 生成的"，
于是自然语言"整理报告"时既可能不敢改自己刚生成的文件，也可能误删/误覆盖用户文件。

本模块只做**持久化事实**，不做授权决策：
* 三来源：user_original / user_saved_material / qio_generated；
* 记录与 OS 文件身份（volume + file_id，复用 services/file_identity.py）绑定，
  路径只作辅助检索 —— 同卷移动/改名后身份不变，来源仍可查；
* 身份已知但与记录不一致（外部替换）→ **不继承**旧记录（绝不让外部文件冒充 QIO 生成物）；
* 持久化到迁移 55 的 file_origins 表，进程重启后同一 DB 仍可查；
* 所有方法 fail-soft：表不存在/连接不可用/DB 错误 → 返回 None/False，绝不打断真实文件操作。

fs_write/fs_patch/fs_delete、附件写回/原子替换统一通过本模块查询，并把
**后端核实过的** file_origin 放进 effect.detail（契约 §3.4）。模型传入的 origin 字段
不被信任，也不参与判定。

写入线程纪律：FileOriginStore 只应在**事件循环线程**调用（与 R9 的 sqlite 单线程纪律
一致）；文件 I/O 的调用方若在工作线程完成替换，应在回到事件循环后再记账。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent.services import file_identity

__all__ = [
    "ORIGIN_USER_ORIGINAL",
    "ORIGIN_USER_SAVED_MATERIAL",
    "ORIGIN_QIO_GENERATED",
    "ORIGINS",
    "USER_ORIGINS",
    "FileOriginRecord",
    "FileOriginStore",
    "set_default_store",
    "default_store",
    "reset_default_store",
    "set_attachment_origin_provider",
    "origin_for_path",
    "record_for_path",
    "record_created_path",
    "record_existing_path",
    "mark_missing_path",
    "note_replace_path",
    # 给 approved_operations（W2 文件）的接线 helper：W3 不直接改该文件。
    "snapshot_before_write",
    "note_authorized_write_path",
    "note_recycled_path",
]

ORIGIN_USER_ORIGINAL = "user_original"
ORIGIN_USER_SAVED_MATERIAL = "user_saved_material"
ORIGIN_QIO_GENERATED = "qio_generated"
ORIGINS = (
    ORIGIN_USER_ORIGINAL,
    ORIGIN_USER_SAVED_MATERIAL,
    ORIGIN_QIO_GENERATED,
)
#: 用户侧来源：任何"观测/创建"动作都不得把它们改写成 qio_generated。
USER_ORIGINS = (ORIGIN_USER_ORIGINAL, ORIGIN_USER_SAVED_MATERIAL)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _abspath(path: Path | str) -> str:
    """规范化绝对路径（只做归一化，不解析符号链接 —— 身份才是权威）。"""
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def _norm_key(path: Path | str) -> str:
    return os.path.normcase(_abspath(path))


@dataclass(frozen=True)
class FileOriginRecord:
    """file_origins 的一行（契约 §3.4 冻结形状）。"""

    path: str
    volume: str
    file_id: str
    origin: str
    source: str
    attachment_id: str | None
    created_at: str
    updated_at: str
    missing_at: str | None

    @property
    def identity_key(self) -> str:
        return f"{self.volume}:{self.file_id}"

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "volume": self.volume,
            "file_id": self.file_id,
            "identity": self.identity_key,
            "origin": self.origin,
            "source": self.source,
            "attachment_id": self.attachment_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "missing_at": self.missing_at,
        }


class FileOriginStore:
    """file_origins 持久化（迁移 55）。构造参数与契约 §3.4 一致。"""

    def __init__(self, conn: Any, workspace_root: Path | str) -> None:
        self.conn = conn
        self.workspace_root = Path(workspace_root)
        self._lock = threading.RLock()
        self._ready_cache: bool | None = None

    # -- 基础设施 ---------------------------------------------------------

    def _ready(self) -> bool:
        """表是否可用（按 sqlite_master 检查并缓存；不可用绝不抛给调用方）。"""
        if self._ready_cache is True:
            return True
        if self.conn is None:
            self._ready_cache = False
            return False
        try:
            row = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='file_origins'"
            ).fetchone()
        except Exception:  # noqa: BLE001 - 老库/连接不可用：按没有这张表处理
            self._ready_cache = False
            return False
        self._ready_cache = row is not None
        return bool(self._ready_cache)

    @staticmethod
    def _value(row: Any, name: str, index: int) -> Any:
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return row[index]

    def _record(self, row: Any) -> FileOriginRecord:
        return FileOriginRecord(
            path=str(self._value(row, "path", 2)),
            volume=str(self._value(row, "volume", 0)),
            file_id=str(self._value(row, "file_id", 1)),
            origin=str(self._value(row, "origin", 3)),
            source=str(self._value(row, "source", 4) or ""),
            attachment_id=self._value(row, "attachment_id", 5),
            created_at=str(self._value(row, "created_at", 6)),
            updated_at=str(self._value(row, "updated_at", 7)),
            missing_at=self._value(row, "missing_at", 8),
        )

    def _select(
        self,
        where: str,
        params: tuple,
        *,
        order: str = "",
        limit: int | None = None,
    ) -> list[FileOriginRecord]:
        if not self._ready():
            return []
        sql = (
            "SELECT volume, file_id, path, origin, source, attachment_id, "
            "created_at, updated_at, missing_at FROM file_origins WHERE " + where
        )
        if order:
            sql += " ORDER BY " + order
        if limit:
            sql += " LIMIT %d" % int(limit)
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001 - DB 问题不改变真实文件操作
            return []
        return [self._record(row) for row in rows]

    def _by_identity(self, ident: Any) -> FileOriginRecord | None:
        rows = self._select(
            "volume = ? AND file_id = ?", (str(ident.volume), str(ident.file_id)), limit=1
        )
        return rows[0] if rows else None

    def _by_path(self, path: str) -> FileOriginRecord | None:
        rows = self._select(
            "path = ?", (_abspath(path),), order="updated_at DESC", limit=1
        )
        return rows[0] if rows else None

    def _insert(
        self,
        ident: Any,
        path: str,
        origin: str,
        source: str,
        attachment_id: str | None,
        *,
        created_at: str | None = None,
    ) -> FileOriginRecord | None:
        if not self._ready():
            return None
        now = _now()
        try:
            with self._lock:
                # 同一路径上的旧身份记录先标 missing（被删除/被替换的旧文件）
                self.conn.execute(
                    "UPDATE file_origins SET missing_at = COALESCE(missing_at, ?), updated_at = ? "
                    "WHERE path = ? AND missing_at IS NULL "
                    "AND NOT (volume = ? AND file_id = ?)",
                    (now, now, path, str(ident.volume), str(ident.file_id)),
                )
                self.conn.execute(
                    "INSERT INTO file_origins "
                    "(volume, file_id, path, origin, source, attachment_id, created_at, updated_at, missing_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                    "ON CONFLICT(volume, file_id) DO UPDATE SET "
                    "path = excluded.path, origin = excluded.origin, source = excluded.source, "
                    "attachment_id = COALESCE(excluded.attachment_id, file_origins.attachment_id), "
                    "updated_at = excluded.updated_at, missing_at = NULL",
                    (
                        str(ident.volume),
                        str(ident.file_id),
                        path,
                        origin,
                        str(source or ""),
                        attachment_id,
                        created_at or now,
                        now,
                    ),
                )
                self.conn.commit()
        except Exception:  # noqa: BLE001 - 记账失败不得改变文件操作结果
            return None
        return self._by_identity(ident)

    # -- 契约 §3.4 冻结方法 ----------------------------------------------

    def lookup(self, path: Path) -> FileOriginRecord | None:
        """身份优先；身份读不到时退回同路径（文件被移走/暂时缺失的历史查询）。"""
        target = Path(path)
        ident = file_identity.identity_of(target)
        if ident is not None:
            return self._by_identity(ident)
        return self._by_path(_abspath(target))

    def is_generated(self, path: Path) -> bool:
        record = self.lookup(Path(path))
        return bool(record is not None and record.origin == ORIGIN_QIO_GENERATED)

    def record_created(
        self,
        path: Path,
        *,
        origin: str,
        source: str,
        attachment_id: str | None = None,
    ) -> FileOriginRecord | None:
        """**本次受控创建成功后**调用（契约 §3.4）。

        创建前已存在的路径不得冒充 generated：调用方必须在创建前确认路径不存在；
        本方法再做一层保守检查 —— 同路径上已有用户来源记录且文件身份未变时，
        拒绝把用户文件改写成 qio_generated。
        """
        if origin not in ORIGINS:
            return None
        target = Path(path)
        ident = file_identity.identity_of(target)
        if ident is None:
            return None
        now = _now()
        previous = self._by_identity(ident)
        if previous is not None:
            if previous.origin in USER_ORIGINS and origin == ORIGIN_QIO_GENERATED:
                return previous
            generated_at = previous.created_at
        else:
            # 同一路径的旧记录：文件身份已变（删除后重新创建）才算新文件
            stale = self._by_path(_abspath(target))
            generated_at = None
            if stale is not None and stale.identity_key == ident.key:
                return stale
            if stale is not None and stale.origin in USER_ORIGINS and origin == ORIGIN_QIO_GENERATED:
                # 保守：文件身份已变，说明确实是新文件；允许记录 generated（旧记录先标 missing）
                pass
        return self._insert(
            ident,
            _abspath(target),
            origin,
            source,
            attachment_id,
            created_at=generated_at,
        )

    def record_existing_file(
        self,
        path: Path,
        *,
        origin: str,
        source: str,
        attachment_id: str | None = None,
    ) -> FileOriginRecord | None:
        """观测/接收一个已存在的用户文件（不声称"本次创建"）。

        * 已有同身份记录：保留原来源（用户文件不会被降级/改写）；
        * 同路径记录身份已变（外部替换）：旧记录保持原来源并标 missing，
          新文件按传入 origin 记录 —— **外部替换不继承旧生成身份**。
        """
        if origin not in ORIGINS:
            return None
        target = Path(path)
        ident = file_identity.identity_of(target)
        if ident is None:
            return None
        previous = self._by_identity(ident)
        if previous is not None:
            try:
                with self._lock:
                    self.conn.execute(
                        "UPDATE file_origins SET path = ?, updated_at = ?, missing_at = NULL, "
                        "attachment_id = COALESCE(?, attachment_id) "
                        "WHERE volume = ? AND file_id = ?",
                        (
                            _abspath(target),
                            _now(),
                            attachment_id,
                            str(ident.volume),
                            str(ident.file_id),
                        ),
                    )
                    self.conn.commit()
            except Exception:  # noqa: BLE001
                pass
            return self._by_identity(ident)
        return self._insert(ident, _abspath(target), origin, source, attachment_id)

    def update_after_replace(
        self,
        old: FileOriginRecord,
        new_path: Path,
        *,
        require_identity_match: bool = True,
    ) -> FileOriginRecord | None:
        """原子替换/写回后更新身份；来源保持不变（替换不是"另一次创建"）。

        先按 old 的身份查行（require_identity_match=True 时必须存在），再读取
        new_path 的新身份：新身份与旧不同 → 迁移该行的身份键与路径；
        外部替换（调用方没有 old 记录/身份对不上）→ 返回 None，不继承旧来源。
        """
        if old is None:
            return None
        target = Path(new_path)
        new_ident = file_identity.identity_of(target)
        if new_ident is None:
            return None
        with self._lock:
            current = self._by_identity(old)
            if current is None:
                if require_identity_match:
                    return None
                current = old
            if (
                new_ident.volume == current.volume
                and new_ident.file_id == current.file_id
            ):
                try:
                    self.conn.execute(
                        "UPDATE file_origins SET path = ?, updated_at = ?, missing_at = NULL "
                        "WHERE volume = ? AND file_id = ?",
                        (
                            _abspath(target),
                            _now(),
                            current.volume,
                            current.file_id,
                        ),
                    )
                    self.conn.commit()
                except Exception:  # noqa: BLE001
                    return None
                return self._by_identity(new_ident)
            # 身份变化：删除旧身份键，按新身份重放同一来源（created_at 保留）
            try:
                self.conn.execute(
                    "DELETE FROM file_origins WHERE volume = ? AND file_id = ?",
                    (current.volume, current.file_id),
                )
                self.conn.commit()
            except Exception:  # noqa: BLE001
                return None
            return self._insert(
                new_ident,
                _abspath(target),
                current.origin,
                current.source,
                current.attachment_id,
                created_at=current.created_at,
            )

    def mark_missing(self, path: Path) -> None:
        """文件已删除/移入回收站：保留来源事实，只标记 missing_at。"""
        target = Path(path)
        now = _now()
        ident = file_identity.identity_of(target)
        try:
            with self._lock:
                if ident is not None:
                    self.conn.execute(
                        "UPDATE file_origins SET missing_at = COALESCE(missing_at, ?), updated_at = ? "
                        "WHERE volume = ? AND file_id = ?",
                        (now, now, str(ident.volume), str(ident.file_id)),
                    )
                self.conn.execute(
                    "UPDATE file_origins SET missing_at = COALESCE(missing_at, ?), updated_at = ? "
                    "WHERE path = ? AND missing_at IS NULL",
                    (now, now, _abspath(target)),
                )
                self.conn.commit()
        except Exception:  # noqa: BLE001 - 标记失败不影响删除结果
            return

    def update_path(self, record: FileOriginRecord, new_path: Path) -> FileOriginRecord | None:
        """同身份文件移动到新路径（重定位/采纳）：路径更新，来源不变。"""
        if record is None:
            return None
        target = Path(new_path)
        ident = file_identity.identity_of(target)
        if ident is None or ident.key != record.identity_key:
            return None
        try:
            with self._lock:
                self.conn.execute(
                    "UPDATE file_origins SET path = ?, updated_at = ?, missing_at = NULL "
                    "WHERE volume = ? AND file_id = ?",
                    (_abspath(target), _now(), record.volume, record.file_id),
                )
                self.conn.commit()
        except Exception:  # noqa: BLE001
            return None
        return self._by_identity(ident)


# ---------------------------------------------------------------------------
# 进程级查询入口：fs_tools / 附件服务共用同一个 store（app 装配时设置）
# ---------------------------------------------------------------------------

_DEFAULT_STORE: FileOriginStore | None = None
_DEFAULT_LOCK = threading.RLock()
_ATTACHMENT_ORIGIN_PROVIDER: Callable[[Path], str | None] | None = None


def set_default_store(store: FileOriginStore | None) -> None:
    """由应用装配（AttachmentService 构造时）设置进程默认 store。

    引擎/Gateway 也可注入自己的 store（契约 §3.5 origin_provider）；本函数只是
    在 app.py 尚未改动前的单一接线点，见报告"Lead 接线示例"。
    """
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = store


def default_store() -> FileOriginStore | None:
    with _DEFAULT_LOCK:
        return _DEFAULT_STORE


def reset_default_store() -> None:
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = None


def set_attachment_origin_provider(provider: Callable[[Path], str | None] | None) -> None:
    """附件行回退查询（统一查询 fs 与附件路径；老附件行可能早于本模块）。

    provider 必须返回后端核实过的来源或 None；不得接受模型传入值。
    """
    global _ATTACHMENT_ORIGIN_PROVIDER
    with _DEFAULT_LOCK:
        _ATTACHMENT_ORIGIN_PROVIDER = provider


def attachment_origin_provider() -> Callable[[Path], str | None] | None:
    with _DEFAULT_LOCK:
        return _ATTACHMENT_ORIGIN_PROVIDER


def availability() -> dict:
    """诊断用：当前进程是否已接上持久化 store / 附件回退查询。"""
    store = default_store()
    return {
        "store": store is not None,
        "store_ready": bool(store is not None and store._ready()),
        "attachment_provider": attachment_origin_provider() is not None,
    }


def record_for_path(path: Path | str) -> FileOriginRecord | None:
    """统一查询：先查持久化 store，再回退附件行（并落库固化，重启后可查）。"""
    store = default_store()
    if store is None:
        return None
    record = store.lookup(Path(path))
    if record is not None:
        return record
    provider = attachment_origin_provider()
    if provider is None:
        return None
    try:
        origin = provider(Path(path))
    except Exception:  # noqa: BLE001 - 回退查询失败按未知来源处理
        return None
    if origin not in ORIGINS:
        return None
    return store.record_existing_file(
        Path(path), origin=origin, source="attachment.row"
    )


def origin_for_path(path: Path | str) -> str | None:
    """后端核实过的来源；未知 → None（调用方按"用户文件/未知"保守处理）。"""
    record = record_for_path(path)
    return record.origin if record is not None else None


def record_created_path(
    path: Path | str, *, origin: str = ORIGIN_QIO_GENERATED, source: str = ""
) -> FileOriginRecord | None:
    store = default_store()
    if store is None:
        return None
    return store.record_created(Path(path), origin=origin, source=source)


def record_existing_path(
    path: Path | str, *, origin: str, source: str = ""
) -> FileOriginRecord | None:
    store = default_store()
    if store is None:
        return None
    return store.record_existing_file(Path(path), origin=origin, source=source)


def mark_missing_path(path: Path | str) -> None:
    store = default_store()
    if store is not None:
        store.mark_missing(Path(path))


def note_replace_path(old_record: FileOriginRecord | None, path: Path | str) -> FileOriginRecord | None:
    store = default_store()
    if store is None or old_record is None:
        return None
    return store.update_after_replace(old_record, Path(path))


# ---------------------------------------------------------------------------
# 给 approved_operations（W2 负责的文件）的接线 helper —— W3 不直接改它
# ---------------------------------------------------------------------------


def snapshot_before_write(path: Path | str) -> FileOriginRecord | None:
    """批准后的写入执行**之前**调用：抓取旧来源记录，供写后更新身份使用。

    写回/替换会改变 OS 文件身份；写前抓不到记录就无从判断"来源是否保留"，
    也挡不住外部替换继承旧生成身份。
    """
    return record_for_path(path)


def note_authorized_write_path(
    path: Path | str,
    *,
    existed_before: bool,
    pre_record: FileOriginRecord | None = None,
    source: str = "approved.write",
) -> FileOriginRecord | None:
    """批准后的 fs.write / fs.patch 真实执行**成功后**调用（一次记账）。

    规则与 fs_tools 一致：
    * pre_record 存在：身份更新、来源保留（编辑/替换不是重新创建）；
    * 写前已存在但没有记录：记 user_original（不得冒充生成物）；
    * 写前不存在（本次创建）：记 qio_generated —— 必须在真实创建成功后调用。
    """
    store = default_store()
    if store is None:
        return None
    target = Path(path)
    try:
        if pre_record is not None:
            return store.update_after_replace(pre_record, target)
        if existed_before:
            return store.record_existing_file(
                target,
                origin=ORIGIN_USER_ORIGINAL,
                source=source + ".observed_existing",
            )
        return store.record_created(
            target, origin=ORIGIN_QIO_GENERATED, source=source
        )
    except Exception:  # noqa: BLE001 - 记账失败不改变写入结果
        return None


def note_recycled_path(path: Path | str) -> None:
    """批准后的 fs.delete / attachment.recycle 成功移入回收站后调用。"""
    mark_missing_path(path)
