"""自动放行执行的事后核验：受保护范围快照比对（契约 §3.1）。

为什么需要它：低完整性（Low IL）能阻止脚本写 Medium 对象，但**不能阻止读取、不能
阻止网络**，也挡不住同账户权限下某些路径（例如脚本自己拉起的 Medium 进程、或用户
主动降低标签的目录）。所以对「自动放行」的执行再加一道事实核验：

1. 执行前记录受保护范围（工作区业务文件、QIO 内部运行数据等）里每个条目的
   size / mtime_ns；
2. 执行后重新记录并比对；
3. 发现新增/删除/修改 → 如实上报（不能回滚的说明不能回滚），并把本次执行标记为
   违规（调用方据此返回失败，而不是假装成功）。

诚实边界：这是**事后**核验，不是拦截。执行期间已经发生的修改无法撤销；它保证的是
「不会被静默放过」。范围由调用方提供（不要在这里猜用户的业务目录）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 50_000
# 这些目录只影响工具/构建缓存，不属于用户业务文件；跳过它们可以避免每次调用都
# 遍历几十万个文件（但仍会记录目录本身是否被新增/删除）。
_SKIP_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_ENTRY_DIR = -1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Snapshot:
    """一次受保护范围快照。entries: 路径 → (size, mtime_ns)；目录用 size=-1。"""

    roots: tuple[str, ...] = ()
    entries: dict[str, tuple[int, int]] = field(default_factory=dict)
    truncated: bool = False
    taken_at: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "roots": list(self.roots),
            "entries": len(self.entries),
            "truncated": self.truncated,
            "taken_at": self.taken_at,
            "error": self.error,
        }


def _resolve_roots(roots: Iterable[Any]) -> list[Path]:
    resolved: list[Path] = []
    for item in roots or []:
        try:
            path = Path(str(item)).resolve()
        except (OSError, ValueError):
            continue
        if path not in resolved:
            resolved.append(path)
    return resolved


def capture_snapshot(
    roots: Iterable[Any],
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> Snapshot:
    """记录受保护范围快照（只读、有界；不跟随符号链接）。"""
    resolved = _resolve_roots(roots)
    entries: dict[str, tuple[int, int]] = {}
    truncated = False
    error: str | None = None
    try:
        for root in resolved:
            if not root.exists():
                continue
            if root.is_file():
                st = root.stat()
                entries[str(root)] = (int(st.st_size), int(st.st_mtime_ns))
                continue
            for current, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [name for name in sorted(dirnames) if name not in _SKIP_DIR_NAMES]
                current_path = Path(current)
                try:
                    st = current_path.stat()
                    entries[str(current_path)] = (_ENTRY_DIR, int(st.st_mtime_ns))
                except OSError:
                    pass
                for name in sorted(filenames):
                    path = current_path / name
                    try:
                        st = path.stat()  # 不跟随：symlink 只记录链接本身
                    except OSError:
                        continue
                    entries[str(path)] = (int(st.st_size), int(st.st_mtime_ns))
                    if len(entries) >= max_entries:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
    except Exception as exc:  # noqa: BLE001 - 快照失败要如实标注，不能伪装成没变化
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("execution guard snapshot failed: %s", exc, exc_info=True)
    return Snapshot(
        roots=tuple(str(item) for item in resolved),
        entries=entries,
        truncated=truncated,
        taken_at=_now(),
        error=error,
    )


def compare_snapshots(before: Snapshot, after: Snapshot) -> dict[str, Any]:
    """比对两次快照，返回可序列化事实（violations 为空 = 没发现越界修改）。"""
    if before.error or after.error:
        return {
            "ok": False,
            "violations": [],
            "truncated": bool(before.truncated or after.truncated),
            "detail": "事后核验不可用：" + "；".join(
                item for item in (before.error, after.error) if item
            ),
        }
    violations: list[dict[str, str]] = []
    before_keys = set(before.entries)
    after_keys = set(after.entries)
    for path in sorted(before_keys - after_keys):
        violations.append({"path": path, "change": "删除"})
    for path in sorted(after_keys - before_keys):
        violations.append({"path": path, "change": "新增"})
    for path in sorted(before_keys & after_keys):
        old = before.entries[path]
        new = after.entries[path]
        if old != new and old[0] != _ENTRY_DIR:
            violations.append({"path": path, "change": "修改（大小或修改时间变化）"})
    truncated = bool(before.truncated or after.truncated)
    detail = ""
    if truncated:
        detail = "受保护范围超过快照上限，核验只覆盖了前一部分路径（如实标注，不声称完整）"
    return {
        "ok": not violations,
        "violations": violations,
        "checked": len(after.entries),
        "truncated": truncated,
        "detail": detail,
    }


class ExecutionGuard:
    """按调用方提供的保护范围做「执行前快照 → 执行后比对」。"""

    def __init__(
        self,
        scope_provider: Callable[[], Sequence[Any]],
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.scope_provider = scope_provider
        self.max_entries = max_entries

    def roots(self) -> list[Path]:
        try:
            raw = self.scope_provider() if callable(self.scope_provider) else self.scope_provider
        except Exception:  # noqa: BLE001 - 保护范围读不到 = 不核验（调用方会看到空）
            logger.warning("execution guard scope provider failed", exc_info=True)
            return []
        return _resolve_roots(raw or [])

    def capture(self) -> Snapshot:
        return capture_snapshot(self.roots(), max_entries=self.max_entries)

    def compare(self, before: Snapshot, after: Snapshot) -> dict[str, Any]:
        return compare_snapshots(before, after)
