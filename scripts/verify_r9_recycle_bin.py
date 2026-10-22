"""R9 真机取证：用户文件删除进系统回收站，失败保留文件（契约 §0.8 / §12）。

验证的是**Windows 系统事实**：
* SHFileOperationW(FOF_ALLOWUNDO|FOF_NOCONFIRMATION|FOF_SILENT, double-NUL pFrom)
  真的把文件移进了系统回收站（不是永久删除）：查 C:\\$Recycle.Bin 的 $I 元数据，
  原始路径对得上、$R 内容对得上；
* 可以从 $R 还原回原路径（内容一致）—— 证明"可恢复"；
* 文件被独占打开、系统无法回收时：调用如实失败，**文件仍在原位**（绝不永久删除）；
* 不存在的路径如实失败。

用法：cd backend; uv run --frozen python ../scripts/verify_r9_recycle_bin.py
退出码：0 = 全部符合预期；1 = 有不符合的项。
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from agent.services import recycle_bin  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def _recycle_bin_roots(drive: str) -> list[Path]:
    root = Path(drive + "\\$Recycle.Bin")
    roots: list[Path] = []
    try:
        if root.is_dir():
            roots.append(root)
            roots.extend(child for child in root.iterdir() if child.is_dir())
    except OSError:
        pass
    return roots


def _find_recycled_entry(original: Path) -> tuple[dict | None, str]:
    """按 $I 元数据找真实回收站条目；返回 (条目, 诊断)。"""
    target = os.path.normcase(os.path.abspath(str(original)))
    errors: list[str] = []
    for root in _recycle_bin_roots(original.drive + "\\"):
        try:
            entries = list(root.glob("$I*"))
        except OSError as exc:
            errors.append(f"{root}: {exc}")
            continue
        for info_file in entries:
            try:
                raw = info_file.read_bytes()
            except OSError as exc:
                errors.append(f"{info_file}: {exc}")
                continue
            if len(raw) < 24:
                continue
            version = struct.unpack_from("<Q", raw, 0)[0]
            size = struct.unpack_from("<Q", raw, 8)[0]
            # Windows 10/11 的 $I 版本 2：24 字节头后面还有一个 4 字节路径长度字段；
            # 版本 1（旧系统）路径直接从 24 字节开始。
            path_offset = 28 if version == 2 and len(raw) >= 28 else 24
            try:
                stored_path = (
                    raw[path_offset:]
                    .decode("utf-16-le", errors="ignore")
                    .split(chr(0))[0]
                )
            except Exception:  # noqa: BLE001
                continue
            if os.path.normcase(stored_path) != target:
                continue
            data_file = info_file.with_name("$R" + info_file.name[2:])
            return {
                "info": str(info_file),
                "data": str(data_file),
                "stored_path": stored_path,
                "size": int(size),
                "data_exists": data_file.is_file(),
            }, "; ".join(errors)
    return None, "; ".join(errors) or "没有找到匹配的 $I 条目"


def _hold_exclusive(path: Path):
    """用 dwShareMode=0 独占打开文件：模拟"文件正被占用，回收站无法处理"。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        ctypes.c_wchar_p(str(path)),
        0x80000000,  # GENERIC_READ
        0,           # 不共享：其它打开/删除都会失败
        None,
        3,           # OPEN_EXISTING
        0x80,        # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = int(ctypes.c_void_p(-1).value)
    if handle is None or int(handle) == invalid:
        return None
    return (ctypes, kernel32, handle)


def _release(held) -> None:
    if held is None:
        return
    ctypes, kernel32, handle = held
    kernel32.CloseHandle(handle)


def main() -> int:
    print("平台：" + sys.platform)
    print(
        "机制常量：SHFileOperationW + FOF_ALLOWUNDO=0x40 | FOF_NOCONFIRMATION=0x10 | "
        "FOF_SILENT=0x4（另有 FOF_NOERRORUI=0x400 防自动化弹窗）；pFrom double-NUL"
    )
    with tempfile.TemporaryDirectory(prefix="qio-r9-recycle-") as tmp:
        root = Path(tmp)
        target = root / "用户业务文件.txt"
        payload = "必须可恢复的用户内容".encode("utf-8")
        target.write_bytes(payload)

        result = recycle_bin.send_to_recycle_bin(target)
        print("删除结果：" + str(result))
        check("调用报告成功", bool(result.get("ok")), str(result.get("reason")))
        check("原路径已消失", not target.exists())
        check("机制是系统回收站", result.get("mechanism") == recycle_bin.MECHANISM_WINDOWS)
        check("没有永久删除标记", result.get("deleted_permanently") is False)

        entry, why = _find_recycled_entry(target)
        check("在系统回收站中找到真实 $I 元数据条目", entry is not None, why)
        if entry is not None:
            check("回收站记录的原始路径一致", os.path.normcase(entry["stored_path"]) == os.path.normcase(str(target)))
            check("$R 数据文件存在", bool(entry["data_exists"]), entry["data"])
            check("回收站记录的大小一致", entry["size"] == len(payload))
            # 还原：证明可恢复（不是不可逆删除）
            if entry["data_exists"]:
                restored = Path(entry["data"]).read_bytes()
                check("从回收站读回的内容一致", restored == payload)
                target.write_bytes(restored)
                check("还原到原路径后内容一致", target.read_bytes() == payload)
                # 归还到回收站（清理本次取证产生的测试文件；仍是可恢复语义）
                cleanup = recycle_bin.send_to_recycle_bin(target)
                check("取证文件还原后再次回收成功（清理）", bool(cleanup.get("ok")), str(cleanup.get("reason")))

        # 失败保留：文件被独占打开时系统回收应失败，文件必须还在
        locked = root / "被占用不能删.txt"
        locked.write_bytes("锁住的用户内容".encode("utf-8"))
        held = _hold_exclusive(locked)
        check("成功独占打开待删文件（构造失败场景）", held is not None)
        try:
            failed = recycle_bin.send_to_recycle_bin(locked)
            print("失败场景结果：" + str(failed))
            check("占用时回收如实失败", failed.get("ok") is False, str(failed.get("reason")))
            # 独占锁未释放时不能读内容：只核对文件仍在原位
            check(
                "失败后文件仍在原位（未永久删除）",
                locked.is_file() and locked.stat().st_size == len("锁住的用户内容".encode("utf-8")),
            )
        finally:
            _release(held)
        check(
            "释放独占锁后内容未被改动",
            locked.is_file() and locked.read_bytes() == "锁住的用户内容".encode("utf-8"),
        )
        # 释放后应能正常进回收站
        retry = recycle_bin.send_to_recycle_bin(locked)
        check("释放后回收成功", bool(retry.get("ok")), str(retry.get("reason")))

        missing = recycle_bin.send_to_recycle_bin(root / "不存在的文件.txt")
        check("不存在的路径如实失败", missing.get("ok") is False)
    print()
    print("结果：" + ("全部通过" if not FAILURES else "失败 " + ", ".join(FAILURES)))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
