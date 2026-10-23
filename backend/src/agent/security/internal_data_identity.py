"""内部运行数据的真实对象身份登记与生命周期（R9-H2 §5 / R9-H3 §4 / R9-H4 §8）。

为什么需要：

* R9-H 之后内部数据只按 AppContext 真实注册路径识别（不再用名称/后缀判断），
  但硬链接、改名、移动会改变路径而对象还是同一个 —— 只按路径判断会让同一真实
  对象失去保护；
* R9-H4 起还必须区分**生命周期**：文件真正删除后，卷序列号+file_id 可能被新文件
  复用。只凭 identity 会把新对象误认成旧内部对象。因此登记同时保存
  `birth_key`（Windows 创建时间 / POSIX birthtime，取不到时用 ctime 并标注弱依据），
  匹配时 identity + birth 同时成立才算同一对象。

生命周期规则（契约 §8）：

* 原路径替换**不足以**证明旧对象不存在：旧登记不因「路径上换了一个对象」就失去
  历史生命周期保护 —— 旧生命周期 token 继续被识别（旧对象经硬链接/移动仍存在时
  保持保护）；
* 新登记失败时保住已有保护（撤销旧登记 + 建立新登记在同一事务里完成）；
* 真正删除 + 编号复用：identity 相同但 birth 不同的新对象**不得**误认成旧内部对象；
  匹配返回 different；不永久保护全部历史编号（保护键是 identity+birth，不是编号）；
* 无法可靠区分（birth 缺失、弱依据不一致、读不到生命周期）时明确返回 uncertain，
  按路径/历史保守处理，不声称同一文件；
* 不做全盘扫描：只处理 AppContext 给你的路径 + 已登记历史路径 + 有界目录枚举；
* 可信提交后自动更新身份（R9-H3 接线保留），显式 refresh 只作补充。

持久化到迁移 61 的 internal_data_identities 表（迁移 65 追加 birth_key 列）；
重启后从同一 DB 重建。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agent.services import file_identity
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

#: 同步上限（契约 §5.1：有界枚举，不做全盘扫描）。
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_DEPTH = 3

KIND_FILE = "file"
KIND_DIRECTORY = "directory"

#: 被替换/顶替：旧生命周期对象**可能**仍经其它路径存在 → 保留生命周期 token 保护。
RETIRED_REPLACED = "replaced"
#: 已确认对象不存在且无引用 → 不再保护。
RETIRED_DELETED = "deleted"

#: 身份匹配状态。
STATE_SAME = "same"
STATE_DIFFERENT = "different"
STATE_UNCERTAIN = "uncertain"
STATE_UNKNOWN = "unknown"
STATE_RETIRED = "retired"

#: 旧表没有 birth_key 列（迁移 65 之前的库）时的列探测缓存标记。
_BIRTH_COLUMN_MISSING = ""


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


def _identity_parts(identity: Any) -> tuple[str, str | None, int | None]:
    """从 FileIdentity / dict / (key,birth) 取 (identity_key, birth_key, link_count)。"""
    if identity is None:
        return "", None, None
    if isinstance(identity, dict):
        volume = str(identity.get("volume") or "")
        file_id = str(identity.get("file_id") or "")
        key = str(identity.get("identity_key") or identity.get("key") or "")
        if not key and volume and file_id:
            key = volume + ":" + file_id
        birth = identity.get("birth_key")
        link = identity.get("link_count")
        return key, (str(birth) if birth else None), link
    key = str(getattr(identity, "key", "") or getattr(identity, "identity_key", "") or "")
    birth = getattr(identity, "birth_key", None)
    link = getattr(identity, "link_count", None)
    return key, (str(birth) if birth else None), link


class InternalDataIdentityStore:
    """可信应用维护的内部数据身份登记（迁移 61 的 internal_data_identities）。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self._ready_cache: bool | None = None
        self._birth_column: bool | None = None
        #: 本进程观测到的生命周期证据（identity_key → {birth_key, link_count, path}）。
        #: 不落库：重启后按「未知」保守处理（不直接退休旧登记）。
        self._observed: dict[str, dict] = {}
        #: 编号复用事件（诊断/测试用；不包含敏感内容）。
        self.reuse_events: list[dict] = []
        #: 最近一次失败原因（identity 读取/写库失败必须可见，不静默降级）。
        self.last_error: str | None = None

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
        except Exception as exc:  # noqa: BLE001 - 老库/连接不可用：按没有表处理
            self.last_error = type(exc).__name__ + ": " + str(exc)
            self._ready_cache = False
            return False
        self._ready_cache = row is not None
        return bool(self._ready_cache)

    def _has_birth_column(self) -> bool:
        if self._birth_column is not None:
            return bool(self._birth_column)
        if not self._ready():
            self._birth_column = False
            return False
        try:
            rows = self.conn.execute("PRAGMA table_info(internal_data_identities)").fetchall()
        except Exception:  # noqa: BLE001
            self._birth_column = False
            return False
        names = {str(self._value(row, "name", 1) or "") for row in rows}
        self._birth_column = "birth_key" in names
        return bool(self._birth_column)

    def _row(self, identity_key: str) -> Any:
        if not self._ready():
            return None
        try:
            return self.conn.execute(
                "SELECT * FROM internal_data_identities WHERE identity_key = ?",
                (str(identity_key),),
            ).fetchone()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__ + ": " + str(exc)
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

    @classmethod
    def _birth_of(cls, row: Any) -> str | None:
        if row is None:
            return None
        try:
            raw = row["birth_key"]
        except (KeyError, IndexError, TypeError):
            return None
        return str(raw) if raw else None

    @staticmethod
    def _active_of(row: Any, index: int = 3) -> int:
        try:
            return int(row["active"])
        except (KeyError, IndexError, TypeError, ValueError):
            try:
                return int(row[index])
            except (KeyError, IndexError, TypeError, ValueError):
                return 0

    def _observe(self, identity: Any) -> dict:
        key, birth, link = _identity_parts(identity)
        info = {
            "identity_key": key,
            "birth_key": birth,
            "link_count": link,
            "path": str(getattr(identity, "path", "") or ""),
        }
        if key:
            self._observed[key] = info
        return info

    # -- 登记（生命周期感知） ---------------------------------------------

    def _active_rows_at_path(self, path: str, *, excluding_key: str) -> list[Any]:
        if not self._ready():
            return []
        try:
            rows = self.conn.execute(
                "SELECT * FROM internal_data_identities WHERE path = ? AND active = 1",
                (str(path),),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__ + ": " + str(exc)
            return []
        return [
            row
            for row in rows
            if str(self._value(row, "identity_key", 0) or "") != str(excluding_key)
        ]

    def _known_path_lists_identity(self, identity_key: str, birth_key: str | None) -> bool:
        """有限核对：历史登记路径里是否仍有对象匹配这条登记（不做全盘扫描）。"""
        if not self._ready():
            return False
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT path FROM internal_data_identities"
                " WHERE path IS NOT NULL AND path != ''"
            ).fetchall()
        except Exception:  # noqa: BLE001
            return False
        for row in rows:
            raw = str(self._value(row, "path", 0) or "").strip()
            if not raw:
                continue
            current = file_identity.identity_of(raw)
            if current is None:
                continue
            if current.key != str(identity_key):
                continue
            if birth_key and current.birth_key and current.birth_key == birth_key:
                return True
            if not birth_key:
                return True
        return False

    def _replacement_disposition(self, row: Any) -> tuple[str, str]:
        """路径被新对象顶替时，旧登记怎么处理：retire / keep。

        只有能证明旧对象已不可能存在时才退休（契约 §8 第一条）。证明依据：
        * 旧登记有强生命周期依据；
        * 本进程观测过旧对象且硬链接数 <= 1（替换掉最后一个链接 → 旧对象不存在）；
        * 历史登记路径里也找不到仍匹配旧生命周期的对象。
        任一条件不满足 → keep（保守保留保护），并如实记录原因。
        """
        old_key = str(self._value(row, "identity_key", 0) or "")
        old_birth = self._birth_of(row)
        if not old_key or not old_birth:
            return "keep", "birth_unknown"
        if not file_identity.is_strong_birth(old_birth):
            return "keep", "birth_weak"
        observed = self._observed.get(old_key)
        if observed is None:
            return "keep", "no_lifecycle_observation"
        if observed.get("birth_key") and observed.get("birth_key") != old_birth:
            return "keep", "observed_other_lifecycle"
        link_count = observed.get("link_count")
        if link_count is None:
            return "keep", "link_count_unknown"
        if int(link_count) > 1:
            return "keep", "linked_elsewhere"
        if self._known_path_lists_identity(old_key, old_birth):
            return "keep", "known_path_still_exists"
        return "retire", "verified_gone"

    def _insert_row(self, key: str, path: str, kind: str, birth: str | None, source: str, now: str) -> None:
        if self._has_birth_column():
            self.conn.execute(
                "INSERT INTO internal_data_identities"
                " (identity_key, path, kind, active, source, created_at, updated_at,"
                "  retired_reason, birth_key)"
                " VALUES (?, ?, ?, 1, ?, ?, ?, NULL, ?)",
                (key, path, kind, str(source or "")[:200], now, now, birth),
            )
        else:  # pragma: no cover - 迁移 65 之前的旧库
            self.conn.execute(
                "INSERT INTO internal_data_identities"
                " (identity_key, path, kind, active, source, created_at, updated_at,"
                "  retired_reason)"
                " VALUES (?, ?, ?, 1, ?, ?, ?, NULL)",
                (key, path, kind, str(source or "")[:200], now, now),
            )

    def _update_row(
        self,
        key: str,
        path: str,
        kind: str,
        *,
        birth: str | None,
        source: str,
        now: str,
        counts: dict,
        observed_new_birth: bool,
    ) -> None:
        existing_birth = self._birth_of(self._row(key))
        if observed_new_birth and existing_birth and birth and birth != existing_birth:
            if file_identity.is_strong_birth(existing_birth) and file_identity.is_strong_birth(birth):
                # 编号复用：同一 identity 上出现了另一个生命周期对象。
                self.conn.execute(
                    "UPDATE internal_data_identities"
                    " SET path = ?, kind = ?, active = 1, source = ?, updated_at = ?,"
                    "  retired_reason = NULL, birth_key = ?"
                    " WHERE identity_key = ?",
                    (path, kind, str(source or "")[:200], now, birth, key),
                )
                counts["reused"] = int(counts.get("reused", 0)) + 1
                self.reuse_events.append(
                    {
                        "identity_key": key,
                        "old_birth_key": existing_birth,
                        "new_birth_key": birth,
                        "path": path,
                        "at": now,
                    }
                )
                return
            # 弱依据不一致或一强一弱：不能断定复用 → 保留旧登记（不确定）
            counts["uncertain"] = int(counts.get("uncertain", 0)) + 1
            logger.warning("内部数据身份生命周期依据不一致（按不确定保留旧登记）")
            return
        if self._has_birth_column():
            self.conn.execute(
                "UPDATE internal_data_identities"
                " SET path = ?, kind = ?, active = 1, source = ?, updated_at = ?,"
                "  retired_reason = NULL, birth_key = COALESCE(birth_key, ?)"
                " WHERE identity_key = ?",
                (path, kind, str(source or "")[:200], now, birth, key),
            )
        else:  # pragma: no cover
            self.conn.execute(
                "UPDATE internal_data_identities"
                " SET path = ?, kind = ?, active = 1, source = ?, updated_at = ?,"
                "  retired_reason = NULL WHERE identity_key = ?",
                (path, kind, str(source or "")[:200], now, key),
            )
        if birth and not existing_birth:
            counts["birth_backfilled"] = int(counts.get("birth_backfilled", 0)) + 1

    def _apply_identity(
        self,
        identity: Any,
        path: str,
        kind: str,
        *,
        source: str,
        now: str,
        counts: dict,
    ) -> bool:
        """登记一条 identity（须在事务内调用）；返回是否写库成功。"""
        key, birth, _link = _identity_parts(identity)
        if not key:
            counts["errors"] = int(counts.get("errors", 0)) + 1
            return False
        self._observe(identity)
        existing = self._row(key)
        try:
            if existing is None:
                self._insert_row(key, path, kind, birth, source, now)
                counts["added"] = int(counts.get("added", 0)) + 1
            else:
                was_active = self._active_of(existing) == 1
                self._update_row(
                    key,
                    path,
                    kind,
                    birth=birth,
                    source=source,
                    now=now,
                    counts=counts,
                    observed_new_birth=bool(birth),
                )
                if not was_active and counts.get("uncertain", 0) == 0:
                    counts["reactivated"] = int(counts.get("reactivated", 0)) + 1
            counts["active"] = int(counts.get("active", 0)) + 1
            return True
        except Exception as exc:  # noqa: BLE001 - 写库失败必须如实上报
            counts["errors"] = int(counts.get("errors", 0)) + 1
            self.last_error = "identity_update_failed: " + type(exc).__name__ + ": " + str(exc)
            logger.warning("内部数据身份登记写入失败（事务将回滚）", exc_info=True)
            return False

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
        """登记路径上的当前对象：替换旧登记 + 建立新登记在**同一事务**里完成。"""
        key, _birth, _link = _identity_parts(identity)
        if not key:
            counts["errors"] = int(counts.get("errors", 0)) + 1
            return
        path_text = str(path)
        self._observe(identity)
        errors_before = int(counts.get("errors", 0))
        replacements: list[tuple[Any, str, str]] = []
        for row in self._active_rows_at_path(path_text, excluding_key=key):
            disposition, reason = self._replacement_disposition(row)
            old_key = str(self._value(row, "identity_key", 0) or "")
            if disposition == "retire":
                replacements.append((row, RETIRED_REPLACED, reason))
            else:
                counts["retained"] = int(counts.get("retained", 0)) + 1
                retained = counts.setdefault("retained_keys", [])
                if isinstance(retained, list) and old_key:
                    retained.append({"identity_key": old_key, "reason": reason})
        try:
            with transaction(self.conn):
                for row, retired_reason, _reason in replacements:
                    old_key = str(self._value(row, "identity_key", 0) or "")
                    if self._has_birth_column():
                        self.conn.execute(
                            "UPDATE internal_data_identities"
                            " SET active = 0, retired_reason = ?, updated_at = ?, source = ?"
                            " WHERE identity_key = ? AND active = 1",
                            (retired_reason, now, str(source or "")[:200], old_key),
                        )
                    else:  # pragma: no cover
                        self.conn.execute(
                            "UPDATE internal_data_identities"
                            " SET active = 0, retired_reason = ?, updated_at = ?, source = ?"
                            " WHERE identity_key = ? AND active = 1",
                            (retired_reason, now, str(source or "")[:200], old_key),
                        )
                    counts["retired"] = int(counts.get("retired", 0)) + 1
                if not self._apply_identity(
                    identity, path_text, kind, source=source, now=now, counts=counts
                ):
                    # 本次事务内写失败：主动抛出回滚，避免「退休了旧登记却没登记新对象」。
                    raise RuntimeError("identity registration failed")
        except Exception as exc:  # noqa: BLE001 - 失败必须与真实状态一致
            counts["errors"] = int(counts.get("errors", 0)) + 1
            self.last_error = type(exc).__name__ + ": " + str(exc)
            logger.warning("内部数据身份登记事务失败（已回滚，旧保护保留）", exc_info=True)

    # -- 同步 -------------------------------------------------------------

    def sync(
        self,
        protected_paths: Iterable[object],
        *,
        source: str = "app_startup",
        max_files: int = DEFAULT_MAX_FILES,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> dict:
        """同步真实注册路径的身份登记（口径见契约 §5.1 / §8）。

        * 文件：登记其身份 + 生命周期依据；
        * 目录：登记目录自身 + iter_files_limited 有界枚举的文件；
        * 不存在/身份读失败：保留已有登记，只计数 errors（不降级）；
        * 同一路径身份/生命周期变化：见 _register_path（替换与新登记同一事务）；
        * 返回 counts(active/added/retired/retained/reused/uncertain/errors/scanned)。
        """
        counts = {
            "active": 0,
            "added": 0,
            "retired": 0,
            "retained": 0,
            "reused": 0,
            "uncertain": 0,
            "errors": 0,
            "scanned": 0,
        }
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

    # -- 可信写入提交后的身份更新（R9-H3 §4 / R9-H4 §8） ---------------------

    def note_committed_path(
        self,
        path: object,
        *,
        source: str = "trusted_write",
        previous_identity: object = None,
        kind: str | None = None,
    ) -> dict:
        """可信应用写入/原子替换**提交成功后**立即登记新对象身份。

        * 读取当前真实身份 + 生命周期依据；读取失败返回明确错误（不删除旧登记、
          不静默降级）；
        * 旧对象仍可能经其它路径存在时保留其生命周期保护（见 _replacement_disposition）；
        * 撤销旧登记 + 建立新登记同一事务；失败回滚并如实报告；
        * 只更新这一个路径，不做扫描；普通工具没有调用入口。
        返回 {ok, code?, reason?, path?, identity_key?, birth_key?, retired?, retained?}。
        """
        target = _normalize(path)
        if target is None:
            return {
                "ok": False,
                "code": "invalid_path",
                "reason": "路径为空或无法规范化",
                "path": str(path or ""),
            }
        identity = file_identity.identity_of(target)
        if identity is None:
            return {
                "ok": False,
                "code": "identity_unavailable",
                "reason": "当前对象身份读不到（文件系统不支持/路径暂不可用）；已保留旧登记",
                "path": str(target),
            }
        now = _now_iso()
        counts: dict = {
            "active": 0,
            "added": 0,
            "retired": 0,
            "retained": 0,
            "reused": 0,
            "uncertain": 0,
            "errors": 0,
        }
        try:
            actual_kind = KIND_DIRECTORY if target.is_dir() else KIND_FILE
        except OSError:
            actual_kind = kind or KIND_FILE
        self._register_path(
            target,
            actual_kind,
            identity=identity,
            source=source,
            now=now,
            counts=counts,
        )
        old_key = str(previous_identity or "").strip()
        if old_key and old_key != str(identity.key) and self._ready():
            try:
                with transaction(self.conn):
                    cursor = self.conn.execute(
                        "UPDATE internal_data_identities SET active = 0,"
                        " retired_reason = ?, updated_at = ?, source = ?"
                        " WHERE identity_key = ? AND active = 1",
                        (RETIRED_REPLACED, now, str(source or "")[:200], old_key),
                    )
                counts["retired"] += int(cursor.rowcount or 0)
            except Exception:  # noqa: BLE001 - 旧身份退休失败如实报错
                counts["errors"] += 1
        if counts["errors"]:
            return {
                "ok": False,
                "code": "identity_update_failed",
                "reason": "身份登记写入数据库失败（事务已回滚；旧登记保留）",
                "path": str(target),
                "identity_key": str(identity.key),
                "birth_key": identity.birth_key,
                "retired": int(counts.get("retired", 0)),
                "retained": int(counts.get("retained", 0)),
            }
        return {
            "ok": True,
            "path": str(target),
            "identity_key": str(identity.key),
            "birth_key": identity.birth_key,
            "retired": int(counts.get("retired", 0)),
            "retained": int(counts.get("retained", 0)),
            "reused": int(counts.get("reused", 0)),
        }

    def note_committed_paths(
        self,
        paths: Iterable[object],
        *,
        source: str = "trusted_write",
        limit: int = 50,
    ) -> list[dict]:
        """有界批量入口（一次可信提交最多更新 limit 个明确变更对象）。"""
        results: list[dict] = []
        seen: set[str] = set()
        for raw in paths or ():
            if len(results) >= max(1, int(limit or 1)):
                break
            target = _normalize(raw)
            if target is None:
                continue
            key = str(target)
            if key in seen:
                continue
            seen.add(key)
            results.append(self.note_committed_path(target, source=source))
        return results

    def note_committed_delete(
        self, paths: Iterable[object], *, source: str = "trusted_delete", limit: int = 50
    ) -> list[dict]:
        """可信删除提交后：只在能证明对象已不存在时退休登记（契约 §8）。

        证明条件：强生命周期依据 + 观测过硬链接数 <= 1 + 历史登记路径里找不到
        仍匹配旧生命周期的对象。任一不满足 → 保留（如实报告 retained/uncertain），
        不因「路径没了」就直接解除保护。
        """
        results: list[dict] = []
        now = _now_iso()
        seen: set[str] = set()
        for raw in paths or ():
            if len(results) >= max(1, int(limit or 1)):
                break
            target = _normalize(raw)
            if target is None:
                continue
            key = str(target)
            if key in seen:
                continue
            seen.add(key)
            rows = self._active_rows_at_path(key, excluding_key="")
            if not rows:
                results.append(
                    {"ok": True, "path": key, "retired": 0, "retained": 0, "reason": "not_registered"}
                )
                continue
            retired = 0
            retained = 0
            uncertain = 0
            for row in rows:
                old_key = str(self._value(row, "identity_key", 0) or "")
                old_birth = self._birth_of(row)
                observed = self._observed.get(old_key) or {}
                link_count = observed.get("link_count")
                strong = bool(old_birth) and file_identity.is_strong_birth(old_birth)
                same_lifecycle_observed = bool(
                    old_birth and observed.get("birth_key") == old_birth
                )
                exists = self._known_path_lists_identity(old_key, old_birth)
                if exists:
                    retained += 1
                    continue
                if link_count is not None and int(link_count) > 1:
                    # 观测到仍有其它硬链接引用：旧对象确定还存在，保持保护。
                    retained += 1
                    continue
                if strong and same_lifecycle_observed and link_count is not None and int(link_count) <= 1:
                    try:
                        with transaction(self.conn):
                            cursor = self.conn.execute(
                                "UPDATE internal_data_identities SET active = 0,"
                                " retired_reason = ?, updated_at = ?, source = ?"
                                " WHERE identity_key = ? AND active = 1",
                                (RETIRED_DELETED, now, str(source or "")[:200], old_key),
                            )
                        retired += int(cursor.rowcount or 0)
                    except Exception as exc:  # noqa: BLE001
                        self.last_error = type(exc).__name__ + ": " + str(exc)
                        return [
                            {
                                "ok": False,
                                "code": "identity_update_failed",
                                "path": key,
                                "reason": "身份登记写入失败（保留原登记）",
                            }
                        ]
                else:
                    uncertain += 1
            results.append(
                {
                    "ok": True,
                    "path": key,
                    "retired": retired,
                    "retained": retained,
                    "uncertain": uncertain,
                }
            )
        return results

    def retire_path(
        self, path: object, *, reason: str = "deleted", source: str = "trusted_delete"
    ) -> dict:
        """把某路径上的 active 身份标记退休（保留历史行，不删除任何登记）。

        说明：删除路径上的对象若仍被硬链接引用，身份本身仍然存在；一般删除不应调用
        本方法（本方法只用于明确「对象已不存在且不再被引用」的场景，调用方负责确认）。
        """
        target = _normalize(path)
        if target is None:
            return {"ok": False, "code": "invalid_path", "retired": 0}
        if not self._ready():
            return {"ok": False, "code": "store_unavailable", "retired": 0}
        try:
            with transaction(self.conn):
                cursor = self.conn.execute(
                    "UPDATE internal_data_identities SET active = 0, retired_reason = ?,"
                    " updated_at = ?, source = ?"
                    " WHERE path = ? AND active = 1",
                    (
                        str(reason or RETIRED_DELETED)[:100],
                        _now_iso(),
                        str(source or "")[:200],
                        str(target),
                    ),
                )
            return {"ok": True, "path": str(target), "retired": int(cursor.rowcount or 0)}
        except Exception as exc:  # noqa: BLE001 - 失败保留登记并如实报告
            self.last_error = type(exc).__name__ + ": " + str(exc)
            return {"ok": False, "code": "identity_update_failed", "retired": 0}

    # -- 查询 -------------------------------------------------------------

    def active_keys(self) -> frozenset[str]:
        """active 身份键集合（兼容旧调用方；生命周期匹配见 protected_tokens）。"""
        if not self._ready():
            return frozenset()
        try:
            rows = self.conn.execute(
                "SELECT identity_key FROM internal_data_identities WHERE active = 1"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__ + ": " + str(exc)
            return frozenset()
        return frozenset(
            str(self._value(row, "identity_key", 0) or "") for row in rows
        ) - {""}

    def active_tokens(self) -> frozenset[str]:
        """active 行的匹配键：强生命周期 → token；弱/未知 → plain key（保守匹配）。"""
        if not self._ready():
            return frozenset()
        try:
            rows = self.conn.execute(
                "SELECT identity_key, birth_key, active FROM internal_data_identities"
                " WHERE active = 1"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__ + ": " + str(exc)
            return frozenset()
        tokens: set[str] = set()
        for row in rows:
            key = str(self._value(row, "identity_key", 0) or "")
            if not key:
                continue
            birth = self._birth_of(row)
            if birth:
                # 有生命周期依据（强或弱）才用 token 匹配；弱依据不一致时按不确定处理，
                # 但不再用裸 identity 键声称同一文件（契约 §8：不声称同一文件）。
                tokens.add(key + "#" + birth)
            # 旧行 birth NULL：身份键不足以证明同一对象 → 只按路径/历史保守，
            # 不加入识别集合（启动 sync 会用当前对象的 birth 回填）。
        return frozenset(tokens)

    def protected_tokens(self) -> frozenset[str]:
        """识别内部对象用的匹配键（契约 §8）：

        * active 行按 active_tokens；
        * retired_replaced（被顶替但旧对象可能仍存在）的强生命周期 token 继续保留保护；
        * retired_deleted（已证明不存在）不再保留。
        """
        tokens = set(self.active_tokens())
        if not self._ready():
            return frozenset(tokens)
        try:
            rows = self.conn.execute(
                "SELECT identity_key, birth_key, retired_reason FROM internal_data_identities"
                " WHERE active = 0 AND retired_reason = ?",
                (RETIRED_REPLACED,),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__ + ": " + str(exc)
            return frozenset(tokens)
        for row in rows:
            key = str(self._value(row, "identity_key", 0) or "")
            birth = self._birth_of(row)
            if key and birth and file_identity.is_strong_birth(birth):
                tokens.add(key + "#" + birth)
        return frozenset(tokens)

    def known_paths(self) -> frozenset[str]:
        """active + 历史登记路径（身份读失败时不静默降级的依据）。"""
        if not self._ready():
            return frozenset()
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT path FROM internal_data_identities"
                " WHERE path IS NOT NULL AND path != ''"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__ + ": " + str(exc)
            return frozenset()
        return frozenset(
            str(self._value(row, "path", 0) or "") for row in rows
        ) - {""}

    def lookup(self, identity_key: object) -> bool:
        """该身份是否仍是受保护的 active 内部数据对象（兼容旧调用方）。"""
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

    def match_identity(self, identity: Any, birth_key: str | None = None) -> dict:
        """身份 + 生命周期一起核对（契约 §8 核心查询）。

        返回 {state, identity_key, birth_key, active, retired_reason, reason}；
        state ∈ same / different / uncertain / unknown / retired。
        * 旧行 birth NULL、当前 birth 未知、弱依据不一致 → uncertain（不声称同一文件）；
        * identity 相同但两侧强 birth 不同 → different（编号复用，不是旧对象）；
        * 被顶替但生命周期 token 仍受保护（retired_replaced）→ same + retained。
        """
        key, birth, _link = _identity_parts(identity)
        if not key and birth_key:
            key, birth = "", str(birth_key)
        if not key:
            return {
                "state": STATE_UNKNOWN,
                "identity_key": "",
                "birth_key": birth,
                "active": 0,
                "retired_reason": None,
                "reason": "没有可用身份",
            }
        row = self._row(key)
        if row is None:
            return {
                "state": STATE_UNKNOWN,
                "identity_key": key,
                "birth_key": birth,
                "active": 0,
                "retired_reason": None,
                "reason": "未登记过这个身份",
            }
        stored_birth = self._birth_of(row)
        active = int(self._value(row, "active", 3, 0) or 0)
        retired_reason = self._value(row, "retired_reason", 7)
        result = {
            "identity_key": key,
            "birth_key": stored_birth,
            "active": active,
            "retired_reason": retired_reason,
            "retained": False,
        }
        if active != 1:
            if (
                retired_reason == RETIRED_REPLACED
                and stored_birth
                and birth
                and stored_birth == birth
                and file_identity.is_strong_birth(stored_birth)
            ):
                result.update(
                    {
                        "state": STATE_SAME,
                        "retained": True,
                        "reason": "旧生命周期仍受保护（对象可能仍经其它路径存在）",
                    }
                )
                return result
            result.update({"state": STATE_RETIRED, "reason": "这个身份已被退休"})
            return result
        if not stored_birth or not birth:
            result.update(
                {
                    "state": STATE_UNCERTAIN,
                    "reason": "生命周期依据缺失（旧登记/平台不支持），按路径与历史保守处理",
                }
            )
            return result
        if stored_birth == birth:
            result.update({"state": STATE_SAME, "reason": "身份与生命周期依据一致"})
            return result
        if file_identity.is_strong_birth(stored_birth) and file_identity.is_strong_birth(birth):
            result.update(
                {"state": STATE_DIFFERENT, "reason": "同一编号上的另一个生命周期对象（编号复用）"}
            )
            return result
        result.update(
            {"state": STATE_UNCERTAIN, "reason": "生命周期依据不一致但来源不可靠，按不确定处理"}
        )
        return result

    def lookup_lifecycle(self, identity_key: object, birth_key: object = None) -> str:
        """便捷入口：返回 same/different/uncertain/unknown/retired。"""
        key = str(identity_key or "").strip()
        birth = str(birth_key or "").strip() or None
        return str(self.match_identity({"identity_key": key, "birth_key": birth}).get("state"))

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
        counts: dict = {"active": 0, "added": 0, "retired": 0, "retained": 0, "errors": 0}
        try:
            kind = KIND_DIRECTORY if target.is_dir() else KIND_FILE
        except OSError:
            kind = KIND_FILE
        self._register_path(
            target, kind, identity=identity, source=source, now=now, counts=counts
        )
        return str(identity.key)

    # -- 诊断（测试/排查用；不含密钥） -------------------------------------

    def health(self) -> dict:
        """登记可用性与最近一次错误（读失败不得静默降级）。"""
        return {
            "ready": self._ready(),
            "birth_column": self._has_birth_column(),
            "last_error": self.last_error,
            "observed": len(self._observed),
            "reuse_events": len(self.reuse_events),
        }

    def observed(self, identity_key: str) -> dict | None:
        info = self._observed.get(str(identity_key or ""))
        return dict(info) if info else None

    def snapshot(self) -> list[dict]:
        if not self._ready():
            return []
        try:
            rows = self.conn.execute(
                "SELECT * FROM internal_data_identities ORDER BY created_at, identity_key"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            self.last_error = type(exc).__name__ + ": " + str(exc)
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
                    "birth_key": self._birth_of(row),
                }
            )
        return result


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FILES",
    "InternalDataIdentityStore",
    "KIND_DIRECTORY",
    "KIND_FILE",
    "RETIRED_DELETED",
    "RETIRED_REPLACED",
    "STATE_DIFFERENT",
    "STATE_RETIRED",
    "STATE_SAME",
    "STATE_UNCERTAIN",
    "STATE_UNKNOWN",
]
