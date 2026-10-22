"""有生命周期的临时文件（契约 §1、§6.5）。

只读模式允许应用写「工作区 temp/ 下有生命周期的解析临时文件」——这是应用受控
行为，不是模型任意写文件的能力。本模块统一管理这些文件：退出/完成即清理，
失败/取消也清理，绝不隐藏成永久副本。
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


class TempFileManager:
    def __init__(self, root: Path | Callable[[], Path], *, max_age_seconds: float = 24 * 3600) -> None:
        self._root = root
        self.max_age_seconds = float(max_age_seconds)

    def root(self) -> Path:
        raw = self._root() if callable(self._root) else self._root
        return Path(raw)

    def ensure(self) -> Path:
        root = self.root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - 目录不可用时由调用方如实报错
            pass
        return root

    def create(self, *, prefix: str = "qio-", suffix: str = "") -> Path:
        self.ensure()
        name = f"{prefix}{uuid.uuid4().hex}{suffix}"
        return self.root() / name

    def cleanup(self, path: Path | None) -> bool:
        if path is None:
            return False
        try:
            if path.is_file():
                path.unlink()
                return True
        except OSError:  # pragma: no cover - 清理失败不影响业务结果
            return False
        return False

    @contextmanager
    def managed(self, *, prefix: str = "qio-", suffix: str = "") -> Iterator[Path]:
        path = self.create(prefix=prefix, suffix=suffix)
        try:
            yield path
        finally:
            self.cleanup(path)

    def sweep(self, *, now: float | None = None) -> int:
        """清理超过 max_age_seconds 的残留临时文件（启动时调用，幂等）。"""
        root = self.root()
        if not root.is_dir():
            return 0
        removed = 0
        current = time.time() if now is None else float(now)
        try:
            entries = list(root.iterdir())
        except OSError:  # pragma: no cover
            return 0
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if current - entry.stat().st_mtime < self.max_age_seconds:
                    continue
                entry.unlink()
                removed += 1
            except OSError:  # pragma: no cover - 单个文件失败不影响其它
                continue
        return removed
