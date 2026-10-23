"""文件身份：区分「同一份文件被编辑 / 移动」与「另一份同内容文件」（契约 §7）。

为什么不能只靠路径 / 大小 / mtime / 哈希：

* 路径会变（移动、改名）—— 路径相同也不代表还是同一份文件（被另一份替换）；
* 大小、mtime、内容哈希都可能完全相同 —— 复制出来的另一份文件**不是**原文件；
  同一文件编辑后哈希变了，但**身份不变**。

唯一可靠判据是操作系统给的文件身份：

* Windows：卷序列号 + FILE_ID_128（GetFileInformationByHandleEx(FileIdInfo)）；
* POSIX：st_dev + st_ino。

本模块只做纯文件 I/O，不碰数据库、不依赖项目其它模块（可独立在 Windows 真机上取证，
见 scripts/verify_r9_file_identity.py）。

边界与诚实说明：

* Windows 的文件 id 在「删除后重建同名文件」「跨卷移动」后会变；跨卷移动得到的是新文件
  （身份不同），只有同卷 rename/移动才保持身份 —— 这正是契约要求的判据。
* 文件系统若不支持 file id（个别网络盘/虚拟盘），identity_of 返回 None：
  调用方必须如实按「无法确认」处理，不得退化成「同名/同大小即视为同一文件」。
* 身份只说明「是同一份文件」，不说明内容没被改；内容是否变化看 content_sha256。
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: 内容哈希的默认分块大小（1MB：与读取侧的分块口径一致，便于在事件循环里限时让出）
DEFAULT_CHUNK = 1024 * 1024

#: 有限范围查找的默认上限：移动修复只扫有限数量目录项，不做全盘监听/遍历。
DEFAULT_SCAN_MAX_FILES = 5000
DEFAULT_SCAN_MAX_DEPTH = 3

#: Windows 长路径前缀（用字符构造，避免源码里写转义序列）
_WIN_LONG_PREFIX = chr(92) * 2 + "?" + chr(92)


@dataclass(frozen=True)
class FileIdentity:
    """一份文件在操作系统里的身份事实（契约 §7 冻结形状）。

    * volume：Windows 卷序列号 / POSIX st_dev；
    * file_id：Windows FILE_ID_128 / POSIX st_ino；
    * path：本次观测时用的路径（**不是**身份的一部分）；
    * size / mtime_ns：观测到的元数据（会随编辑变化，不属于身份）。
    """

    volume: str
    file_id: str
    path: str
    size: int
    mtime_ns: int

    @property
    def key(self) -> str:
        """身份键（可直接落库到 attachments.file_id）：volume + file_id。"""
        return f"{self.volume}:{self.file_id}"


def _hex(value: int) -> str:
    return f"{int(value):016x}"


# ---------------------------------------------------------------------------
# Windows：GetFileInformationByHandleEx(FileIdInfo)
# ---------------------------------------------------------------------------

_FILE_ID_INFO_CLASS = 18  # FileIdInfo
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = -1

_win32 = None


def _load_kernel32():
    """延迟加载 kernel32（非 Windows 上不调用）。"""
    global _win32
    if _win32 is not None:
        return _win32
    import ctypes
    from ctypes import wintypes

    class FILE_ID_INFO(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", ctypes.c_ubyte * 16),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    _win32 = (ctypes, kernel32, FILE_ID_INFO)
    return _win32


def _long_path(path: str) -> str:
    """给 CreateFileW 补长路径前缀（>260 字符的 Windows 路径）。"""
    if path.startswith(_WIN_LONG_PREFIX):
        return path
    if os.name == "nt" and len(path) >= 240 and os.path.isabs(path):
        return _WIN_LONG_PREFIX + path
    return path


def _windows_identity(path: str) -> tuple[str, str] | None:
    """用 GetFileInformationByHandleEx(FileIdInfo) 取 (volume, file_id)；失败返回 None。"""
    try:
        ctypes, kernel32, FILE_ID_INFO = _load_kernel32()
    except Exception:  # noqa: BLE001 - 加载失败 = 无法确认身份（不是崩溃）
        return None
    handle = kernel32.CreateFileW(
        _long_path(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle is None or int(handle) == _INVALID_HANDLE_VALUE:
        return None
    try:
        info = FILE_ID_INFO()
        ok = kernel32.GetFileInformationByHandleEx(
            handle, _FILE_ID_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            return None
        # FILE_ID_INFO.VolumeSerialNumber 是 ULONGLONG 字段，但卷序列号本身只有
        # 32 位（GetVolumeInformationW 返回 DWORD）；高 32 位不是卷序列号的一部分，
        # 丢弃它才能与系统 API 的卷序列号对上、也避免不同 API 间比较失败。
        volume = str(int(info.VolumeSerialNumber) & 0xFFFFFFFF)
        file_id = "".join(f"{byte:02x}" for byte in bytes(info.FileId))
        return volume, file_id
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# POSIX：st_dev + st_ino
# ---------------------------------------------------------------------------


def _posix_identity(path: str) -> tuple[str, str] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return str(int(stat.st_dev)), _hex(int(stat.st_ino))


def raw_identity(path: str | Path) -> tuple[str, str] | None:
    """底层身份对 (volume, file_id)；无法确认时返回 None。"""
    raw = os.fspath(path)
    try:
        raw = os.path.abspath(raw)
    except OSError:
        return None
    if sys.platform == "win32":
        return _windows_identity(raw)
    return _posix_identity(raw)


def identity_of(path: Path | str) -> FileIdentity | None:
    """一份文件的身份事实；路径不存在 / 文件系统不支持时返回 None（不抛）。"""
    try:
        target = Path(os.path.abspath(os.fspath(path)))
        stat = target.stat()
    except OSError:
        return None
    pair = raw_identity(target)
    if pair is None:
        return None
    volume, file_id = pair
    return FileIdentity(
        volume=volume,
        file_id=file_id,
        path=str(target),
        size=int(stat.st_size),
        mtime_ns=int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    )


def identity_key_of(path: Path | str) -> str | None:
    """路径当前对象的统一身份键（volume:file_id）；读不到返回 None（不猜）。"""
    found = identity_of(path)
    return found.key if found is not None else None


def same_file(a: FileIdentity | None, b: FileIdentity | None) -> bool:
    """是否同一份文件：**只能**靠 volume + file_id（契约 §7）。

    任一为 None（无法确认）→ False：不把「可能」当成「是」。路径、大小、mtime、
    内容哈希都不参与这个判断。
    """
    if a is None or b is None:
        return False
    return (str(a.volume), str(a.file_id)) == (str(b.volume), str(b.file_id))


def identity_matches(recorded: str | None, current: FileIdentity | None) -> bool:
    """落库的身份键（attachments.file_id）与当前观测是否同一份文件。"""
    if not recorded or current is None:
        return False
    return str(recorded) == current.key


def content_sha256(path: Path | str, *, chunk: int = DEFAULT_CHUNK) -> str | None:
    """内容摘要；读不了返回 None（不抛）。

    内容摘要说明「字节是否还是那些字节」，**不参与** same_file 判定：
    同一文件编辑后哈希会变（身份不变），另一份同内容文件哈希相同（身份不同）。
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for piece in iter(lambda: handle.read(max(1, int(chunk))), b""):
                digest.update(piece)
    except OSError:
        return None
    return digest.hexdigest()


def iter_files_limited(
    root: Path | str,
    *,
    max_files: int = DEFAULT_SCAN_MAX_FILES,
    max_depth: int = DEFAULT_SCAN_MAX_DEPTH,
    include_hidden: bool = True,
) -> list[Path]:
    """在有限范围里列出候选文件（只用于移动修复的有限查找，不做全盘监听）。

    范围 = 给定目录及其 max_depth 层子目录；超过 max_files 立即停止。
    不跟随目录符号链接（避免环）。
    """
    out: list[Path] = []
    base = Path(root)
    try:
        base_is_dir = base.is_dir()
    except OSError:
        return out
    if not base_is_dir:
        return out
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack and len(out) < max_files:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if len(out) >= max_files:
                break
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                if depth + 1 <= max_depth:
                    stack.append((Path(entry.path), depth + 1))
                continue
            if not include_hidden and entry.name.startswith("."):
                continue
            out.append(Path(entry.path))
    return out


def find_by_identity(
    roots: list[Path | str] | tuple[Path | str, ...],
    identity: FileIdentity | None,
    *,
    max_files: int = DEFAULT_SCAN_MAX_FILES,
    max_depth: int = DEFAULT_SCAN_MAX_DEPTH,
) -> list[Path]:
    """在有限范围里找 **volume+file_id 完全相同** 的文件（自动修复的唯一依据）。"""
    if identity is None:
        return []
    matched: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for candidate in iter_files_limited(root, max_files=max_files, max_depth=max_depth):
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            current = identity_of(candidate)
            if same_file(identity, current):
                matched.append(candidate)
                if len(matched) > 1:
                    return matched  # 多个同身份路径（硬链接）：调用方必须当 ambiguous
    return matched


def _self_test() -> int:
    """真实运行自测（不进 pytest 收集；Windows 真机脚本与人工排查共用）。"""
    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        suffix = (" :: " + detail) if detail else ""
        print(f"[{'PASS' if condition else 'FAIL'}] {name}{suffix}")
        if not condition:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="qio-file-identity-") as tmp:
        root = Path(tmp)
        original = root / "原件.txt"
        original.write_text("第一版内容", encoding="utf-8")
        first = identity_of(original)
        check("identity_of 读到身份", first is not None, str(first))
        if first is None:
            return 1
        check(
            "content_sha256 与 hashlib 一致",
            content_sha256(original)
            == hashlib.sha256(original.read_bytes()).hexdigest(),
        )
        # 编辑后：身份不变、哈希变
        original.write_text("第二版内容（被编辑）", encoding="utf-8")
        after_edit = identity_of(original)
        check("同一文件编辑后仍是同一文件", same_file(first, after_edit))
        check(
            "编辑后内容哈希变化",
            content_sha256(original) != content_sha256(root / "不存在.txt"),
        )
        # 移动/改名后：身份不变
        moved = root / "改了名.txt"
        original.rename(moved)
        after_move = identity_of(moved)
        check("同卷移动/改名后身份不变", same_file(first, after_move), str(after_move))
        # 另一份同内容文件：身份不同
        copy_path = root / "复制件.txt"
        copy_path.write_text("第二版内容（被编辑）", encoding="utf-8")
        copy_identity = identity_of(copy_path)
        check("同内容的另一份文件不是同一文件", not same_file(after_move, copy_identity))
        # 有限范围查找：按身份找回移动后的文件
        found = find_by_identity([root], first)
        check("find_by_identity 找到同身份文件", found == [moved], str(found))
        # 无法确认时不得误判为同一文件
        check(
            "None 不与任何身份判同",
            not same_file(None, first) and not same_file(first, None),
        )
    print()
    print("结果：" + ("全部通过" if not failures else "失败 " + ", ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(_self_test())
