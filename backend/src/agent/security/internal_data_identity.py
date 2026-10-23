"""内部运行数据的真实对象身份登记（R9-H2 契约 §5）。

为什么需要：

* R9-H 之后内部数据只按 AppContext 真实注册路径识别（不再用名称/后缀判断），
  但硬链接、改名、移动会改变路径而对象还是同一个 —— 只按路径判断会让同一真实
  对象失去保护；
* 本模块维护"可信应用登记的内部数据对象身份"（volume + file_id，复用
  services/file_identity.py），由应用启动/路径维护时同步：
  - 真实注册文件/目录取身份；目录用 iter_files_limited **有界枚举**（禁止全盘扫描）；
  - 同一路径身份变化（原子替换）→ 旧 identity retired（active=0, replaced），新 identity 登记；
  - 注册路径暂时不存在/身份读失败 → **保留**已有 active 登记（不降级、不删除）；
  - 同内容复制件身份不同 → 不登记为原对象（只认 identity，不看内容/大小/mtime）；
  - 模型/普通工具没有写入/解除登记的入口：只信任应用维护路径。

持久化到迁移 61 的 internal_data_identities 表；重启后从同一 DB 重建。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agent.services import file_identity

logger = logging.getLogger(__name__)

#: 同步上限（契约 §5.1：有界枚举，不做全盘扫描）。
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_DEPTH = 3

KIND_FILE = "file"
KIND_DIRECTORY = "directory"

RETIRED_REPLACED = "replaced"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(path: object) -> Path | None:
    text = str(path or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, ValueError):  # pragma: no cover - 极端输入
        return None


class InternalDataIdentityStore:
    """可信应用维护的内部数据身份登记（迁移 61 的 internal_data_identities）。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._ready_cache: bool | None = None

    # -- 基础设施 ---------------------------------------------------------

    def _ready(self) -> bool:
        if self._ready_cache is True:
            return True
        if self.conn is None:
            self._ready_cache = False
            return False
        try:
            row = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='internal_data_identities'"
            ).fetchone()
        except Exception:  # noqa: BLE001 - 老库/连接不可用：按没有表处理
            self._ready_cache = False
            return False
        self._ready_cache = row is not None
        return bool(self._ready_cache)

    def _row(self, identity_key: str) -> Any:
        if not self._ready():
            return None
        try:
            return self.conn.execute(
                "SELECT * FROM internal_data_identities WHERE identity_key = ?",
                (str(identity_key),),
            ).fetchone()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _value(row: Any, name: str, index: int, default: Any = None) -> Any:
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            try:
                return row[index]
            except (KeyError, IndexError, TypeError):
                return default

    # -- 登记 -------------------------------------------------------------

    def _retire_for_path(
        self, path: str, new_identity_key: str, *, source: str, now: str
    ) -> int:
        """同一路径上的旧身份（原子替换/被新对象顶替）→ retired（保留历史行）。"""
        if not self._ready():
            return 0
        try:
            cursor = self.conn.execute(
                "UPDATE internal_data_identities"
                " SET active = 0, retired_reason = ?, updated_at = ?, source = ?"
                " WHERE path = ? AND active = 1 AND identity_key != ?",
                (RETIRED_REPLACED, now, str(source or "")[:200], path, new_identity_key),
            )
            return int(cursor.rowcount or 0)
        except Exception:  # noqa: BLE001 - 登记失败不改变真实文件操作
            return 0

    def _upsert_identity(
        self,
        identity: Any,
        path: str,
        kind: str,
        *,
        source: str,
        now: str,
        counts: dict,
    ) -> None:
        if not self._ready():
            return
        key = str(getattr(identity, "key", "") or "")
        if not key:
            return
        existing = self._row(key)
        try:
            if existing is None:
                self.conn.execute(
                    "INSERT INTO internal_data_identities"
                    " (identity_key, path, kind, active, source, created_at, updated_at,"
                    "  retired_reason)"
                    " VALUES (?, ?, ?, 1, ?, ?, ?, NULL)",
                    (key, path, kind, str(source or "")[:200], now, now),
                )
                counts["added"] = int(counts.get("added", 0)) + 1
            else:
                self.conn.execute(
                    "UPDATE internal_data_identities"
                    " SET path = ?, kind = ?, active = 1, source = ?, updated_at = ?,"
                    "  retired_reason = NULL"
                    " WHERE identity_key = ?",
                    (path, kind, str(source or "")[:200], now, key),
                )
            self.conn.commit()
        except Exception:  # noqa: BLE001 - 登记失败只计数，不影响真实文件
            counts["errors"] = int(counts.get("errors", 0)) + 1
            logger.warning("内部数据身份登记失败（已忽略，不影响真实文件操作）")

    def _register_path(
        self,
        path: Path,
        kind: str,
        *,
        identity: Any,
        source: str,
        now: str,
        counts: dict,
    ) -> None:
        key = str(getattr(identity, "key", "") or "")
        if not key:
            counts["errors"] = int(counts.get("errors", 0)) + 1
            return
        retired = self._retire_for_path(str(path), key, source=source, now=now)
        counts["retired"] = int(counts.get("retired", 0)) + retired
        self._upsert_identity(identity, str(path), kind, source=source, now=now, counts=counts)
        counts["active"] = int(counts.get("active", 0)) + 1

    # -- 同步 -------------------------------------------------------------

    def sync(
        self,
        protected_paths: Iterable[object],
        *,
        source: str = "app_startup",
        max_files: int = DEFAULT_MAX_FILES,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> dict:
        """同步真实注册路径的身份登记（口径见契约 §5.1）。

        * 文件：登记其身份；
        * 目录：登记目录自身 + iter_files_limited 有界枚举的文件；
        * 不存在/身份读失败：保留已有登记，只计数 errors；
        * 同一路径身份变化：旧 identity retired，新 identity active；
        * 返回 counts(active/added/retired/errors/scanned)。
        """
        counts = {"active": 0, "added": 0, "retired": 0, "errors": 0, "scanned": 0}
        remaining = max(0, int(max_files or 0))
        depth = max(0, int(max_depth or 0))
        now = _now_iso()
        for raw in list(protected_paths or ()):
            path = _normalize(raw)
            if path is None:
                counts["errors"] += 1
                continue
            try:
                is_dir = path.is_dir()
            except OSError:
                counts["errors"] += 1
                continue
            if is_dir:
                directory_identity = file_identity.identity_of(path)
                if directory_identity is None:
                    counts["errors"] += 1
                else:
                    counts["scanned"] += 1
                    self._register_path(
                        path,
                        KIND_DIRECTORY,
                        identity=directory_identity,
                        source=source,
                        now=now,
                        counts=counts,
                    )
                files = file_identity.iter_files_limited(
                    path, max_files=remaining, max_depth=depth
                )
                for child in files:
                    if remaining <= 0:
                        break
                    remaining -= 1
                    child_path = Path(child)
                    identity = file_identity.identity_of(child_path)
                    if identity is None:
                        counts["errors"] += 1
                        continue
                    counts["scanned"] += 1
                    try:
                        child_kind = KIND_FILE if child_path.is_file() else KIND_DIRECTORY
                    except OSError:
                        child_kind = KIND_FILE
                    self._register_path(
                        child_path,
                        child_kind,
                        identity=identity,
                        source=source,
                        now=now,
                        counts=counts,
                    )
                continue
            identity = file_identity.identity_of(path)
            if identity is None:
                # 路径暂时不存在/身份读失败：保留已有 active 登记，不降级不删除。
                counts["errors"] += 1
                continue
            counts["scanned"] += 1
            self._register_path(
                path,
                KIND_FILE,
                identity=identity,
                source=source,
                now=now,
                counts=counts,
            )
        return counts

    # -- 查询 -------------------------------------------------------------

    def active_keys(self) -> frozenset[str]:
        if not self._ready():
            return frozenset()
        try:
            rows = self.conn.execute(
                "SELECT identity_key FROM internal_data_identities WHERE active = 1"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return frozenset()
        return frozenset(
            str(self._value(row, "identity_key", 0) or "") for row in rows
        ) - {""}

    def known_paths(self) -> frozenset[str]:
        """active + 历史登记路径（身份读失败时不静默降级的依据）。"""
        if not self._ready():
            return frozenset()
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT path FROM internal_data_identities"
                " WHERE path IS NOT NULL AND path != ''"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return frozenset()
        return frozenset(
            str(self._value(row, "path", 0) or "") for row in rows
        ) - {""}

    def lookup(self, identity_key: object) -> bool:
        """该身份是否仍是受保护的 active 内部数据对象。"""
        key = str(identity_key or "").strip()
        if not key:
            return False
        row = self._row(key)
        if row is None:
            return False
        try:
            return int(self._value(row, "active", 3, 0) or 0) == 1
        except (TypeError, ValueError):
            return False

    def refresh_path(self, path: object, *, source: str = "path_refresh") -> str | None:
        """重新读取某个路径的当前身份并更新登记；返回当前 identity_key（读不到 None）。

        读不到时不删除/不降级已登记身份（移动/短暂不可用不能解除保护）。
        """
        target = _normalize(path)
        if target is None:
            return None
        identity = file_identity.identity_of(target)
        if identity is None:
            return None
        now = _now_iso()
        counts = {"active": 0, "added": 0, "retired": 0, "errors": 0}
        try:
            kind = KIND_DIRECTORY if target.is_dir() else KIND_FILE
        except OSError:
            kind = KIND_FILE
        self._register_path(
            target, kind, identity=identity, source=source, now=now, counts=counts
        )
        return str(identity.key)

    # -- 诊断（测试/排查用；不含密钥） -------------------------------------

    def snapshot(self) -> list[dict]:
        if not self._ready():
            return []
        try:
            rows = self.conn.execute(
                "SELECT * FROM internal_data_identities ORDER BY created_at, identity_key"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []
        result: list[dict] = []
        for row in rows:
            result.append(
                {
                    "identity_key": str(self._value(row, "identity_key", 0) or ""),
                    "path": self._value(row, "path", 1),
                    "kind": str(self._value(row, "kind", 2) or KIND_FILE),
                    "active": int(self._value(row, "active", 3, 0) or 0),
                    "source": self._value(row, "source", 4),
                    "created_at": str(self._value(row, "created_at", 5) or ""),
                    "updated_at": str(self._value(row, "updated_at", 6) or ""),
                    "retired_reason": self._value(row, "retired_reason", 7),
                }
            )
        return result


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FILES",
    "InternalDataIdentityStore",
    "KIND_DIRECTORY",
    "KIND_FILE",
    "RETIRED_REPLACED",
]
