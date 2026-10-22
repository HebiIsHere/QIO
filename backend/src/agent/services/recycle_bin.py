"""用户业务文件删除：一律进系统回收站；失败保留文件并如实报告（契约 §0.8）。

产品规则（不是建议）：

* 删除用户文件**不允许**用 os.unlink / 覆盖写 / 移进临时目录来"等效删除"；
* Windows 用系统回收站（SHFileOperationW + FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT，
  pFrom 必须 double-NUL 结尾）；
* POSIX 没有系统级回收站：按 freedesktop 规范移入 $XDG_DATA_HOME/Trash（同卷才移；
  跨卷无法保证可恢复 → 如实失败），并写 .trashinfo 记录原始路径与删除时间；
* 任何失败都**保留文件**并返回 reason；绝不退化成永久删除。
* 本模块只回答"文件有没有按可恢复语义移走"，不负责 DB 记账（调用方负责）。

真机取证：scripts/verify_r9_recycle_bin.py（Windows 上验证真实进系统回收站、可恢复）。
"""

from __future__ import annotations

import errno
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

# SHFileOperationW 的 FILEOP_FLAGS（契约要求的三个 + 自动化必需的 NOERRORUI）
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004
FOF_NOERRORUI = 0x0400
FO_DELETE = 3

#: 回收站机制的诚实标识（写进结果与审计，不把 POSIX 移动说成 Windows 回收站）
MECHANISM_WINDOWS = "windows_recycle_bin"
MECHANISM_FREEDESKTOP = "freedesktop_trash"
MECHANISM_NONE = "none"


def _failure(path: str, reason: str, *, mechanism: str = MECHANISM_NONE) -> dict:
    return {
        "ok": False,
        "path": path,
        "mechanism": mechanism,
        "reason": reason,
        "deleted_permanently": False,
    }


def _success(path: str, *, mechanism: str, trashed_path: str | None = None) -> dict:
    return {
        "ok": True,
        "path": path,
        "mechanism": mechanism,
        "reason": "已移入系统回收站" if mechanism == MECHANISM_WINDOWS else "已移入回收站",
        "trashed_path": trashed_path,
        "deleted_permanently": False,
    }


# ---------------------------------------------------------------------------
# Windows：SHFileOperationW
# ---------------------------------------------------------------------------


def _windows_recycle(path: str) -> dict:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # noqa: BLE001 - 无法调用系统 API：如实失败，文件保留
        return _failure(
            path, "无法加载 Windows 回收站 API：" + str(exc), mechanism=MECHANISM_WINDOWS
        )

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return _failure(
            path, "无法加载 shell32：" + str(exc), mechanism=MECHANISM_WINDOWS
        )
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int

    # pFrom 必须 double-NUL 结尾：给 create_unicode_buffer 多留 2 个字符，
    # 缓冲区里就是 <path>\0\0（字符串结尾的 NUL + 补的那个零）。
    source = ctypes.create_unicode_buffer(path, len(path) + 2)
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.cast(source, wintypes.LPCWSTR)
    operation.pTo = None
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None
    try:
        code = int(shell32.SHFileOperationW(ctypes.byref(operation)))
    except Exception as exc:  # noqa: BLE001
        return _failure(
            path, "调用系统回收站失败：" + str(exc), mechanism=MECHANISM_WINDOWS
        )
    if operation.fAnyOperationsAborted:
        return _failure(path, "用户/系统取消了回收站操作；文件保留", mechanism=MECHANISM_WINDOWS)
    if code != 0:
        return _failure(
            path,
            f"系统回收站返回错误码 0x{code:X}；文件保留（未做永久删除）",
            mechanism=MECHANISM_WINDOWS,
        )
    return _success(path, mechanism=MECHANISM_WINDOWS)


# ---------------------------------------------------------------------------
# POSIX：freedesktop Trash（没有系统级回收站；跨卷如实失败）
# ---------------------------------------------------------------------------


def _posix_trash_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "Trash"


def _posix_recycle(path: str) -> dict:
    target = Path(path)
    trash = _posix_trash_dir()
    files_dir = trash / "files"
    info_dir = trash / "info"
    try:
        files_dir.mkdir(parents=True, exist_ok=True)
        info_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failure(
            path,
            "无法创建回收站目录（" + str(exc) + "）；文件保留",
            mechanism=MECHANISM_FREEDESKTOP,
        )
    name = target.name or "item"
    candidate = files_dir / name
    counter = 1
    while candidate.exists():
        candidate = files_dir / f"{name}.{counter}"
        counter += 1
        if counter > 10_000:
            return _failure(
                path, "回收站里同名文件过多，无法生成唯一名字；文件保留",
                mechanism=MECHANISM_FREEDESKTOP,
            )
    try:
        # os.replace 跨卷会 EXDEV：宁可失败，也不做"复制+删除"的永久删除
        os.replace(target, candidate)
    except OSError as exc:
        reason = (
            "目标与回收站不在同一文件系统，无法保证可恢复；文件保留"
            if exc.errno == errno.EXDEV
            else "移入回收站失败（" + str(exc) + "）；文件保留"
        )
        return _failure(path, reason, mechanism=MECHANISM_FREEDESKTOP)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    info_path = info_dir / (candidate.name + ".trashinfo")
    try:
        info_path.write_text(
            "[Trash Info]\nPath=" + quote(str(target.absolute()), safe="/") + "\nDeletionDate=" + stamp + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        # 元数据写不进去就无法按规范恢复：退回原位置并如实失败
        try:
            os.replace(candidate, target)
        except OSError:
            return _failure(
                path,
                "回收站元数据写入失败且无法把文件放回原位（" + str(exc) + "）",
                mechanism=MECHANISM_FREEDESKTOP,
            )
        return _failure(
            path,
            "回收站元数据写入失败；文件已放回原位（" + str(exc) + "）",
            mechanism=MECHANISM_FREEDESKTOP,
        )
    return _success(
        path, mechanism=MECHANISM_FREEDESKTOP, trashed_path=str(candidate)
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def send_to_recycle_bin(path: str | Path) -> dict:
    """把一个文件/目录移入系统回收站（**唯一**的用户文件删除入口）。

    返回事实字典；ok=False 时文件一定还在原位置（或调用方需要按 reason 人工处理），
    绝不出现"报告失败但文件已被永久删除"的情况。
    """
    raw = str(path or "").strip().strip('"')
    if not raw:
        return _failure("", "路径为空")
    target = Path(raw)
    try:
        if not target.exists():
            return _failure(str(target), "文件不存在，未做任何删除")
        absolute = target.absolute()
    except OSError as exc:
        return _failure(str(target), "无法定位这个路径（" + str(exc) + "）；未做任何删除")

    # Windows 上 SHFileOperationW 接受相对路径，但统一绝对路径便于审计与核验
    if sys.platform == "win32":
        result = _windows_recycle(str(absolute))
    else:
        result = _posix_recycle(str(absolute))

    try:
        still_there = absolute.exists()
    except OSError:
        still_there = True
    if result.get("ok") and still_there:
        return _failure(
            str(absolute),
            "系统报告已移入回收站，但原路径仍然存在；按失败处理（不冒充已删除）",
            mechanism=str(result.get("mechanism") or MECHANISM_NONE),
        )
    if not result.get("ok") and not still_there:
        # 罕见：调用报错但文件已不在原位 —— 如实标注，不声称进了回收站
        result = dict(result)
        result["verified_gone"] = True
        result["reason"] = str(result.get("reason") or "") + "；文件已不在原路径，但无法确认在回收站"
    return result


def recycle_many(paths: list[str | Path]) -> list[dict]:
    """批量入口：逐个如实报告，绝不因为一个失败就永久删除其余文件。"""
    return [send_to_recycle_bin(item) for item in paths]


def _self_test() -> int:
    """真实运行自测：把一个临时文件移入系统回收站，并确认原路径消失。"""
    import tempfile

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        suffix = (" :: " + detail) if detail else ""
        print(f"[{'PASS' if condition else 'FAIL'}] {name}{suffix}")
        if not condition:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="qio-recycle-") as tmp:
        target = Path(tmp) / "要删除的用户文件.txt"
        target.write_text("用户内容", encoding="utf-8")
        result = send_to_recycle_bin(target)
        print("结果：" + str(result))
        check("调用报告成功", bool(result.get("ok")), str(result.get("reason")))
        check("原路径已消失", not target.exists())
        check("机制如实", result.get("mechanism") in (MECHANISM_WINDOWS, MECHANISM_FREEDESKTOP))
        missing = send_to_recycle_bin(Path(tmp) / "不存在.txt")
        check("不存在的文件如实失败", missing.get("ok") is False)
    print()
    print("结果：" + ("全部通过" if not failures else "失败 " + ", ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(_self_test())
