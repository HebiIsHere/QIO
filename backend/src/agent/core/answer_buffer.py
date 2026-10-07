"""未声明正文的有界缓冲 + 溢出暂存（第六轮契约 §1.3）。

角色由正文声明决定（§1.2）：未声明的正文在角色落地之前不能展示，所以需要一块
**有界**的暂存 —— 内存里最多 `UNDECLARED_MEMORY_LIMIT` 个 UTF-8 字节，超出部分
追加写到 `<data_dir>/tmp/<delta_id>.spill`（写在**工作线程**里，事件循环只记账）。

上限**只管理资源，不决定角色**：超过内存上限不改判 `interim`，也不构成
「有工具调用」或「整轮没有回答内容」的证据。硬上限 `UNDECLARED_SPILL_LIMIT`
达到时**如实报告**（可见 WARNING + 截断事实写进交付内容），绝不无界增长、
偷偷丢字、擅自换角色或重写答案。

计量单位统一为 **UTF-8 字节**（旧注释里的「256 KB」按字符数算，中文下差 3 倍）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 内存里最多保留的未声明正文（**UTF-8 字节**，不是字符数）。
UNDECLARED_MEMORY_LIMIT = 256 * 1024
# 暂存文件硬上限（UTF-8 字节）：达到即如实截断并报告，绝不无界增长。
UNDECLARED_SPILL_LIMIT = 64 * 1024 * 1024
# 暂存文件名后缀（启动清理只删自己的东西）。
SPILL_SUFFIX = ".spill"

# 已经清理过的目录（进程内一次）：启动清理是惰性的，不需要 AppContext 接线。
_cleaned_dirs: set[str] = set()


def default_spill_dir() -> Path:
    """`<data_dir>/tmp`：与 AppContext 同一个 Settings 口径（env 驱动）。"""
    from agent.config import Settings

    return Path(Settings().data_dir) / "tmp"


def cleanup_stale_spills(spill_dir: Path, *, force: bool = False) -> int:
    """清理陈旧暂存文件：新进程不拥有任何暂存文件（重启即清理）。

    惰性调用（第一次真正要写暂存时）—— 生产接线由 Lead/B 决定，见回报。
    """
    key = str(spill_dir)
    if not force and key in _cleaned_dirs:
        return 0
    _cleaned_dirs.add(key)
    removed = 0
    try:
        for path in spill_dir.glob(f"*{SPILL_SUFFIX}"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning("stale spill cleanup failed: %s", path)
    except OSError:  # 目录不存在 / 不可读：没有可清理的东西
        return 0
    if removed:
        logger.info("cleaned %d stale spill file(s) in %s", removed, spill_dir)
    return removed


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        logger.warning("spill cleanup failed: %s", path)


def _split_by_bytes(text: str, limit: int) -> tuple[str, str]:
    """按 UTF-8 字节上限把文本切成两段（**不切坏多字节字符**）。"""
    if limit <= 0:
        return "", text
    used = 0
    for index, char in enumerate(text):
        size = len(char.encode("utf-8"))
        if used + size > limit:
            return text[:index], text[index:]
        used += size
    return text, ""


def _safe_name(delta_id: str) -> str:
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in delta_id]
    return ("".join(keep) or "delta")[:80]


class AnswerBuffer:
    """未声明正文的缓冲：有界内存 + 溢出暂存；上限只管理资源，不决定角色。"""

    def __init__(
        self,
        delta_id: str,
        spill_dir: Path | str | None = None,
        *,
        memory_limit: int | None = None,
        spill_limit: int | None = None,
    ) -> None:
        self._delta_id = delta_id
        self._spill_dir = Path(spill_dir) if spill_dir is not None else None
        self._memory_limit = UNDECLARED_MEMORY_LIMIT if memory_limit is None else memory_limit
        self._spill_limit = UNDECLARED_SPILL_LIMIT if spill_limit is None else spill_limit
        self._memory: list[str] = []
        self._memory_bytes = 0
        self._spill_path: Path | None = None
        self._spill_handle = None
        self._spill_bytes = 0
        # 已生成内容的总字节数（内存 + 暂存）——「整轮有没有回答内容」看它。
        self.total_bytes = 0
        # 如实记录截断（绝不偷偷丢字、绝不改角色）；截断事实由 AgentLoop 报给用户。
        self.truncated = False
        self.truncation_reason: str | None = None

    # -- 状态 -------------------------------------------------------------

    @property
    def spilled(self) -> bool:
        return self._spill_bytes > 0

    @property
    def memory_bytes(self) -> int:
        return self._memory_bytes

    @property
    def spill_path(self) -> Path | None:
        return self._spill_path

    # -- 写入 -------------------------------------------------------------

    async def append(self, text: str) -> None:
        """追加一段正文：先填内存，超出部分进暂存（工作线程里写）。"""
        if not text:
            return
        size = len(text.encode("utf-8"))
        room = self._memory_limit - self._memory_bytes
        if size <= room:
            self._memory.append(text)
            self._memory_bytes += size
            self.total_bytes += size
            return
        # 切分是 CPU 活：放到线程里，别堵事件循环（大分块时尤其明显）。
        head, tail = await asyncio.to_thread(_split_by_bytes, text, room)
        if head:
            used = len(head.encode("utf-8"))
            self._memory.append(head)
            self._memory_bytes += used
            self.total_bytes += used
        if tail:
            await self._append_spill(tail)

    async def _append_spill(self, text: str) -> None:
        room = self._spill_limit - self._spill_bytes
        if room <= 0:
            self._truncate(f"达到暂存硬上限 {self._spill_limit} 字节")
            return
        keep, dropped = await asyncio.to_thread(_split_by_bytes, text, room)
        if dropped:
            self._truncate(f"达到暂存硬上限 {self._spill_limit} 字节")
        if not keep:
            return
        data = keep.encode("utf-8")
        try:
            await self._ensure_spill_file()
            await asyncio.to_thread(self._write_sync, data)
        except OSError as exc:  # 暂存不可用：如实截断并报告（内存部分照常交付）
            self._truncate(f"暂存写入失败（{type(exc).__name__}: {exc}）")
            return
        self._spill_bytes += len(data)
        self.total_bytes += len(data)

    async def _ensure_spill_file(self) -> None:
        if self._spill_handle is not None:
            return
        spill_dir = self._spill_dir if self._spill_dir is not None else default_spill_dir()
        # 第一次真正要写暂存时清理陈旧文件（重启后的残留），进程内只做一次。
        await asyncio.to_thread(cleanup_stale_spills, spill_dir)
        await asyncio.to_thread(spill_dir.mkdir, parents=True, exist_ok=True)
        path = spill_dir / f"{_safe_name(self._delta_id)}{SPILL_SUFFIX}"
        handle = await asyncio.to_thread(open, path, "ab")
        self._spill_path = path
        self._spill_handle = handle
        self._spill_dir = spill_dir

    def _write_sync(self, data: bytes) -> None:
        handle = self._spill_handle
        if handle is None:  # pragma: no cover - 调用方保证已建文件
            raise OSError("spill handle closed")
        handle.write(data)
        handle.flush()

    def _truncate(self, reason: str) -> None:
        if not self.truncated:
            self.truncated = True
            self.truncation_reason = reason
            logger.warning("undeclared answer truncated: %s (delta=%s)", reason, self._delta_id)

    # -- 读取与清理 -------------------------------------------------------

    async def collect(self) -> str:
        """按序拼回全部正文（内存 + 暂存）；句柄关闭，文件留给 discard 清理。"""
        parts = list(self._memory)
        await self._close_spill()
        path = self._spill_path
        if path is not None:
            try:
                data = await asyncio.to_thread(path.read_bytes)
                parts.append(data.decode("utf-8", "replace"))
            except OSError as exc:  # 读不回来就如实少这部分，不编造
                logger.warning("spill read failed: %s", exc)
        return "".join(parts)

    async def discard(self) -> None:
        """删掉暂存文件（收尾 / 取消 / 断流后），不留待决任务。"""
        await self._close_spill()
        path, self._spill_path = self._spill_path, None
        if path is not None:
            await asyncio.to_thread(_unlink_quiet, path)

    async def _close_spill(self) -> None:
        handle, self._spill_handle = self._spill_handle, None
        if handle is not None:
            try:
                await asyncio.to_thread(handle.close)
            except OSError:  # pragma: no cover - 关不掉不影响交付
                logger.warning("spill close failed", exc_info=True)
