"""数据库身份自检：判断「这次打开的，是不是原来那个数据库」。

为什么需要它：数据目录可能被外部软件（杀软沙箱、容器、同步/搬家工具）在路径上
做"影子替换"，此时应用看到的是一个空库，表现成"聊天记录凭空消失"，而真实数据
其实完好。光看路径发现不了（路径没变），所以这里用两层身份来判断：

1. **数据库自身身份证**：`settings` 表里的 `db.instance_id`（随机 UUID，首次创建
   时生成）。它跟着数据走——拷贝、搬家都不会变，是「是不是同一个库」的权威答案。
2. **文件身份**：`<卷>:<文件号>:<大小>`。记在数据库之外（Windows 放注册表
   `HKCU\\Software\\qio\\QIO`，其它平台落一个 JSON 文件），用来发现「同一个库被
   换成了另一份文件」。

比对结果：

* `first_run`：没有基线 → 记基线，不告警；
* `ok`：身份证与文件都对得上；
* `replaced_same_database`：身份证一样、文件不同（复制/还原/迁移）→ 轻提示，
  并自动把基线更新到新位置；
* `different_database`：身份证不一样（例如被换成了另一个空库）→ 强告警，
  **基线保持不动**，这样下次启动还会继续报警，直到用户确认。
"""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

DB_INSTANCE_KEY = "db.instance_id"
BASELINE_ENV = "QIO_DB_BASELINE"
REGISTRY_KEY = r"Software\qio\QIO"
REGISTRY_VALUE = "DbBaseline"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BaselineStore(Protocol):
    """基线（上次看到的身份）的存放处：必须在数据目录之外。"""

    def read(self) -> dict[str, Any] | None: ...

    def write(self, payload: dict[str, Any]) -> None: ...

    def clear(self) -> None: ...


class FileBaselineStore:
    """JSON 文件实现：非 Windows 环境、以及需要隔离基线的场景（测试）。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class RegistryBaselineStore:
    """Windows 注册表实现。

    刻意放在 `HKCU\\Software\\qio`：和安装信息同一个键，且**不在数据目录里**——
    否则数据目录被整层掉包时，连"上次是谁"这句话也会跟着一起被换掉。
    """

    def read(self) -> dict[str, Any] | None:
        try:
            import winreg
        except ImportError:  # pragma: no cover - 非 Windows
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
                raw, _kind = winreg.QueryValueEx(key, REGISTRY_VALUE)
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, payload: dict[str, Any]) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            winreg.SetValueEx(
                key,
                REGISTRY_VALUE,
                0,
                winreg.REG_SZ,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            )

    def clear(self) -> None:
        try:
            import winreg
        except ImportError:  # pragma: no cover - 非 Windows
            return
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, REGISTRY_VALUE)
        except OSError:
            pass


def default_baseline_store() -> BaselineStore:
    override = os.environ.get(BASELINE_ENV)
    if override:
        return FileBaselineStore(override)
    if os.name == "nt":
        return RegistryBaselineStore()
    return FileBaselineStore(Path.home() / ".qio" / "db-baseline.json")


def check_enabled() -> bool:
    """自检总开关：测试/隔离环境可用 `QIO_DISABLE_DB_CHECK=1` 关掉。

    需要它是因为基线存在"用户级"的位置（注册表）——开发实例用的是临时数据目录，
    不能让它们去改写正式库的基线。
    """
    raw = os.environ.get("QIO_DISABLE_DB_CHECK", "").strip().lower()
    return raw not in ("1", "true", "yes", "on")


def connection_db_path(conn: sqlite3.Connection) -> str | None:
    """这条连接真正打开的数据库文件路径（`PRAGMA database_list` 的 main 行）。

    用它而不是配置里的路径：身份校验要看的正是"我现在到底打开了哪个文件"。
    """
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        name = row["name"] if "name" in row.keys() else row[1]
        file = row["file"] if "file" in row.keys() else row[2]
        if name == "main" and file:
            return str(file)
    return None


def disabled_report(db_path: str | os.PathLike[str]) -> IntegrityReport:
    return IntegrityReport(
        "disabled",
        "数据库身份自检已关闭（QIO_DISABLE_DB_CHECK=1）",
        str(db_path),
        None,
        None,
        None,
        None,
        _now(),
    )


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("dwFileAttributes", ctypes.c_ulong),
        ("ftCreationTime", ctypes.c_ulonglong),
        ("ftLastAccessTime", ctypes.c_ulonglong),
        ("ftLastWriteTime", ctypes.c_ulonglong),
        ("dwVolumeSerialNumber", ctypes.c_ulong),
        ("nFileSizeHigh", ctypes.c_ulong),
        ("nFileSizeLow", ctypes.c_ulong),
        ("nNumberOfLinks", ctypes.c_ulong),
        ("nFileIndexHigh", ctypes.c_ulong),
        ("nFileIndexLow", ctypes.c_ulong),
    ]


def file_identity(path: str | os.PathLike[str]) -> str | None:
    """文件身份：`<卷>:<文件号>:<字节数>`；取不到时返回 None（不阻塞主流程）。

    用文件号而不是路径来认文件：这样"同一个库被换成另一份拷贝"能看出来，
    而"同一个文件被搬到别处"（改名）在 Windows 上文件号不变，也不会误报。
    """
    target = Path(path)
    if os.name != "nt":
        try:
            st = target.stat()
        except OSError:
            return None
        return f"{st.st_dev}:{st.st_ino}:{st.st_size}"

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(str(target), 0x80000000, 7, None, 3, 0, None)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        info = _BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(
            ctypes.c_void_p(handle), ctypes.byref(info)
        ):
            return None
        file_id = (info.nFileIndexHigh << 32) | info.nFileIndexLow
        return f"{info.dwVolumeSerialNumber}:{file_id}:{info.nFileSizeLow}"
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def ensure_instance_id(conn: sqlite3.Connection) -> str:
    """读取数据库自身的身份证；没有就生成并写入（首次创建）。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (DB_INSTANCE_KEY,)
    ).fetchone()
    if row is not None:
        existing = str(row["value"]).strip()
        if existing:
            return existing
    instance_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (DB_INSTANCE_KEY, instance_id, _now()),
    )
    return instance_id


@dataclass(frozen=True)
class IntegrityReport:
    status: str
    message: str
    db_path: str
    instance_id: str | None
    expected_instance_id: str | None
    file_identity: str | None
    expected_file_identity: str | None
    checked_at: str

    @property
    def alert(self) -> bool:
        """需要用户看见的强告警（"这不是你原来的数据库"）。"""
        return self.status == "different_database"

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "db_path": self.db_path,
            "instance_id": self.instance_id,
            "expected_instance_id": self.expected_instance_id,
            "file_identity": self.file_identity,
            "expected_file_identity": self.expected_file_identity,
            "checked_at": self.checked_at,
            "alert": self.alert,
        }


def _path_key(db_path: str) -> str:
    """按数据目录分别记基线：开发实例用临时目录时，不会污染正式库的基线。"""
    return os.path.normcase(os.path.abspath(db_path))


def _read_baseline(store: BaselineStore) -> dict[str, Any]:
    data = store.read() or {}
    entries = data.get("entries")
    last = data.get("last")
    return {
        "entries": dict(entries) if isinstance(entries, dict) else {},
        "last": dict(last) if isinstance(last, dict) else {},
    }


def _write_baseline(
    store: BaselineStore,
    baseline: dict[str, Any],
    key: str,
    entry: dict[str, Any],
    *,
    update_last: bool = True,
) -> None:
    entries = dict(baseline.get("entries") or {})
    entries[key] = entry
    last = dict(entry) if update_last else dict(baseline.get("last") or {})
    store.write({"entries": entries, "last": last})


def _entry_payload(
    instance_id: str, identity: str | None, db_path: str, checked_at: str
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "file_identity": identity,
        "db_path": db_path,
        "updated_at": checked_at,
    }


def _has_content(conn: sqlite3.Connection) -> bool:
    """这个库里有没有真实内容（用来区分「新的空库」和「另一个有数据的库」）。"""
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row["n"])


def check_integrity(
    conn: sqlite3.Connection,
    db_path: str | os.PathLike[str],
    *,
    store: BaselineStore | None = None,
    now: str | None = None,
) -> IntegrityReport:
    """启动自检：回答"这次打开的，是不是原来那个数据库"。"""
    baseline_store = store or default_baseline_store()
    path_text = str(Path(db_path))
    checked_at = now or _now()
    instance_id = ensure_instance_id(conn)
    identity = file_identity(db_path)
    baseline = _read_baseline(baseline_store)
    key = _path_key(path_text)
    entry = baseline["entries"].get(key)
    last = baseline["last"]
    fresh = _entry_payload(instance_id, identity, path_text, checked_at)

    if isinstance(entry, dict):
        expected_id = str(entry.get("instance_id") or "") or None
        expected_identity = entry.get("file_identity")
        if expected_id and expected_id != instance_id:
            return IntegrityReport(
                "different_database",
                (
                    "这次打开的不是原先那个数据库："
                    f"当前库身份证 {instance_id[:8]}…，原先 {expected_id[:8]}…"
                    f"（文件 {path_text}）。真实数据可能仍在原位置，请先不要在里面继续写。"
                ),
                path_text,
                instance_id,
                expected_id,
                identity,
                expected_identity,
                checked_at,
            )
        if expected_identity and identity and expected_identity != identity:
            _write_baseline(baseline_store, baseline, key, fresh)
            return IntegrityReport(
                "replaced_same_database",
                "数据库文件换成了同一份数据的另一个拷贝（可能是复制/还原/迁移），已更新位置记录",
                path_text,
                instance_id,
                expected_id,
                identity,
                expected_identity,
                checked_at,
            )
        _write_baseline(baseline_store, baseline, key, fresh)
        return IntegrityReport(
            "ok",
            "数据库身份校验通过",
            path_text,
            instance_id,
            expected_id,
            identity,
            expected_identity,
            checked_at,
        )

    # 这个目录第一次见：如果和上次用的是同一个库（拷贝/迁移），只给轻提示；
    # 如果换成了另一个有内容的库，才算强告警；全新的空库（例如开发实例）不打扰。
    last_id = str(last.get("instance_id") or "") or None
    last_identity = last.get("file_identity")
    if last_id and last_id == instance_id:
        _write_baseline(baseline_store, baseline, key, fresh)
        return IntegrityReport(
            "replaced_same_database",
            f"数据库换到了新位置（{path_text}），内容仍是同一个库，已更新位置记录",
            path_text,
            instance_id,
            last_id,
            identity,
            last_identity,
            checked_at,
        )
    if last_id and _has_content(conn):
        return IntegrityReport(
            "different_database",
            (
                "这次打开的不是上次那个数据库："
                f"当前库身份证 {instance_id[:8]}…（{path_text}），"
                f"上次是 {last_id[:8]}…（{last.get('db_path') or '未知位置'}）。"
            ),
            path_text,
            instance_id,
            last_id,
            identity,
            last_identity,
            checked_at,
        )
    _write_baseline(baseline_store, baseline, key, fresh)
    return IntegrityReport(
        "first_run",
        "首次运行：已记录当前数据库的身份",
        path_text,
        instance_id,
        last_id,
        identity,
        last_identity,
        checked_at,
    )


def accept_current(
    conn: sqlite3.Connection,
    db_path: str | os.PathLike[str],
    *,
    store: BaselineStore | None = None,
    now: str | None = None,
) -> IntegrityReport:
    """用户确认"以当前这个库为准"：把基线重新记到当前状态。"""
    baseline_store = store or default_baseline_store()
    path_text = str(Path(db_path))
    checked_at = now or _now()
    instance_id = ensure_instance_id(conn)
    identity = file_identity(db_path)
    baseline = _read_baseline(baseline_store)
    entry = _entry_payload(instance_id, identity, path_text, checked_at)
    _write_baseline(baseline_store, baseline, _path_key(path_text), entry)
    return IntegrityReport(
        "ok",
        "已以当前数据库为准，重新记录身份",
        path_text,
        instance_id,
        instance_id,
        identity,
        identity,
        checked_at,
    )
