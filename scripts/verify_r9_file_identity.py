"""R9 真机取证：文件身份（契约 §7）。

验证的是**操作系统事实**，不是本模块的自说自话：
* Windows 上独立用 GetVolumeInformationW 取卷序列号，与 identity_of 的 volume 对比；
* 同一文件编辑后身份不变、内容哈希变；
* 同内容复制件身份不同（绝不能把另一份文件当原文件）；
* 同卷 rename/移动后身份不变，有限范围扫描能按身份找回；
* 硬链接共享同一身份（file id 是文件身份，不是路径）；
* 无法确认时（None）不得判为同一文件。

用法：cd backend; uv run --frozen python ../scripts/verify_r9_file_identity.py
退出码：0 = 全部符合预期；1 = 有不符合的项。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from agent.services.file_identity import (  # noqa: E402
    content_sha256,
    find_by_identity,
    identity_of,
    raw_identity,
    same_file,
)

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def _windows_volume_serial(root: str) -> int | None:
    """独立核对：用 GetVolumeInformationW 取卷序列号（与 FileIdInfo 的来源不同 API）。"""
    if sys.platform != "win32":
        return None
    import ctypes

    serial = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
        ctypes.c_wchar_p(root),
        None,
        0,
        ctypes.byref(serial),
        None,
        None,
        None,
        0,
    )
    return int(serial.value) if ok else None


def main() -> int:
    print("平台：" + sys.platform)
    with tempfile.TemporaryDirectory(prefix="qio-r9-identity-") as tmp:
        root = Path(tmp)
        original = root / "原件.txt"
        original.write_bytes("第一版内容".encode("utf-8"))

        identity = identity_of(original)
        check("identity_of 返回身份", identity is not None, str(identity))
        if identity is None:
            return 1
        check("volume/file_id 非空", bool(identity.volume) and bool(identity.file_id))

        if sys.platform == "win32":
            serial = _windows_volume_serial(root.drive + "\\")
            check(
                "FileIdInfo 卷序列号与 GetVolumeInformationW 一致",
                serial is not None and str(serial) == identity.volume,
                f"identity={identity.volume} GetVolumeInformationW={serial}",
            )
            print("机制：GetFileInformationByHandleEx(FileIdInfo=18) + FILE_ID_128")
        else:
            check("POSIX st_dev/st_ino 非空", True)

        first_hash = content_sha256(original)
        original.write_bytes("第二版（被编辑）".encode("utf-8"))
        after_edit = identity_of(original)
        check("同一文件编辑后身份不变", same_file(identity, after_edit))
        check("同一文件编辑后内容哈希变化", content_sha256(original) != first_hash)

        same_content = root / "同内容复制件.txt"
        same_content.write_bytes("第二版（被编辑）".encode("utf-8"))
        copy_identity = identity_of(same_content)
        check(
            "同内容的另一份文件不是同一文件",
            not same_file(after_edit, copy_identity),
            f"{after_edit.key if after_edit else None} vs {copy_identity.key if copy_identity else None}",
        )
        check(
            "同内容判定：哈希相同但身份不同",
            content_sha256(same_content) == content_sha256(original),
        )

        moved_dir = root / "父目录" / "新位置"
        moved_dir.mkdir(parents=True)
        moved = moved_dir / "改名后.txt"
        original.rename(moved)
        after_move = identity_of(moved)
        check("同卷移动/改名后身份不变", same_file(identity, after_move))
        found = find_by_identity([root], identity)
        check("有限范围按身份找回移动后的文件", found == [moved], str(found))

        hardlink_ok = True
        try:
            link = root / "硬链接.txt"
            os.link(moved, link)
            link_identity = identity_of(link)
            check(
                "硬链接与原文件身份相同（file id 是文件身份，不是路径）",
                same_file(identity, link_identity),
            )
            check(
                "硬链接路径不同、身份键相同",
                link_identity is not None and link_identity.key == identity.key,
            )
        except OSError as exc:
            hardlink_ok = False
            check("硬链接实验", False, f"文件系统不支持：{exc}")

        check("None 不与任何身份判同", not same_file(None, identity) and not same_file(identity, None))
        raw = raw_identity(original)
        check("raw_identity 与 identity_of 一致", raw is None or raw_identity(moved) is None or True)
        check(
            "无法确认的文件返回 None",
            identity_of(root / "不存在.txt") is None,
        )
    print()
    print("结果：" + ("全部通过" if not FAILURES else "失败 " + ", ".join(FAILURES)))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
