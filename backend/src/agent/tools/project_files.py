"""多文件项目：路径规则与落盘（工作区与沙箱共用同一份）。

规则只有一份的原因很直接：工作区写入、沙箱执行两处都要判断「这个路径能不
能用」，两份实现一定会漂移 —— 一边放行、另一边拦下，或者两边都漏。

边界（都取最小值，够做一个像样的工具）：

* 单段最长 `MAX_SEGMENT_CHARS`、最多 `MAX_PATH_DEPTH` 层；
* 单文件最长 `MAX_FILE_BYTES`、项目总体积不超过 `MAX_TOTAL_BYTES`、文件数不超过 `MAX_FILES`。

拒绝的写法：绝对路径（含 `C:` 盘符）、`..` / `.`、空段、以及不在白名单字符集里的段。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 400_000
MAX_FILES = 40
MAX_SEGMENT_CHARS = 64
MAX_PATH_DEPTH = 8

_SAFE_SEGMENT = re.compile(r"^[a-zA-Z0-9_.-]+$")


def safe_rel_path(name: object) -> str:
    """把工具给的路径规范化成相对 posix 路径；不合法就抛 ValueError。"""
    raw = str(name or "").replace("\\", "/").strip()
    if not raw:
        raise ValueError("文件路径不能为空")
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:", raw):
        raise ValueError(f"不能使用绝对路径：{name!r}")
    segments = raw.split("/")
    if len(segments) > MAX_PATH_DEPTH:
        raise ValueError(f"目录层级过深（最多 {MAX_PATH_DEPTH} 层）：{name!r}")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ValueError(f"项目文件路径不合法：{name!r}")
        if len(segment) > MAX_SEGMENT_CHARS or not _SAFE_SEGMENT.match(segment):
            raise ValueError(f"项目文件路径片段不合法：{segment!r}")
    return "/".join(segments)


def check_project_size(files: Mapping[str, str]) -> None:
    """体积与数量上限：一个失控的项目不该把定义、日志和内存一起撑爆。"""
    if len(files) > MAX_FILES:
        raise ValueError(f"项目文件数量超过上限（最多 {MAX_FILES} 个）")
    total = 0
    for name, content in files.items():
        size = len(str(content or "").encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError(
                f"单个文件超过上限（{name}：{size} 字节，最多 {MAX_FILE_BYTES} 字节）"
            )
        total += size
    if total > MAX_TOTAL_BYTES:
        raise ValueError(
            f"项目总体积超过上限（{total} 字节，最多 {MAX_TOTAL_BYTES} 字节）"
        )


def materialize(files: Mapping[str, str], root: Path) -> list[str]:
    """把项目文件写进 `root`，返回写入的相对路径（已排序）。

    路径不合法或超限时抛 ValueError —— 调用方负责把它变成一条如实的失败，
    而不是让文件落到半个位置。
    """
    check_project_size(files)
    written: list[str] = []
    for name, content in files.items():
        rel = safe_rel_path(name)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content or ""), encoding="utf-8")
        written.append(rel)
    return sorted(written)
