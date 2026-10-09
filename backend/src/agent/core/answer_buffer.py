"""未声明正文的有界缓冲 + 溢出暂存（第六轮契约 §1.3）。

角色由正文声明决定（§1.2）：未声明的正文在角色落地之前不能展示，所以需要一块
**有界**的暂存 —— 内存里最多 `UNDECLARED_MEMORY_LIMIT` 个 UTF-8 字节，超出部分
追加写到 `<data_dir>/tmp/<delta_id>.spill`（写在**工作线程**里，事件循环只记账）。

上限**只管理资源，不决定角色**：超过内存上限不改判 `interim`，也不构成
「有工具调用」或「整轮没有回答内容」的证据。硬上限 `UNDECLARED_SPILL_LIMIT`
达到时**如实报告**（可见 WARNING + 截断事实写进交付内容），绝不无界增长、
偷偷丢字、擅自换角色或重写答案。

交付结果是**结构化**的（第七轮契约 §1.4）：`collect()` 返回
:class:`BufferOutcome`，区分「完整交付 / 硬上限截断 / 暂存创建·写入·读取故障」——
**读取故障不得被描述成「正文超过上限」**。事实由 AgentLoop 写成可见事件 + 轮次警告
（只写日志不算交付）。

计量单位统一为 **UTF-8 字节**（旧注释里的「256 KB」按字符数算，中文下差 3 倍）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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


@dataclass(frozen=True)
class BufferOutcome:
    """未声明正文的交付结果（第七轮契约 §1.4，冻结形状）。

    ``kind`` 只有五种：
    * `complete`：完整交付（内存 + 暂存按序拼回）；
    * `limit`：达到暂存硬上限，如实截断（后面的内容没有保存）；
    * `spill_create` / `spill_write` / `spill_read`：暂存创建 / 写入 / 读取故障。

    ``reason`` 是人话原因（过 redact），**读取故障必须与「超过上限」区分开**。
    """

    text: str
    complete: bool
    kind: str
    reason: str | None = None
    # 三个数字分开（第八轮契约 §1.4，字段名冻结）：
    #   generated_bytes = 模型总共生成、交给缓冲的字节数（含没能保存的部分）
    #   saved_bytes     = 成功保存的字节数（内存 + 成功写入暂存的量；写失败/硬上限时 < generated）
    #   delivered_bytes = 实际交付给用户的字节数（= len(text) 的 UTF-8 字节数；只含能确认的内容）
    generated_bytes: int = 0
    saved_bytes: int = 0
    delivered_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        """旧名字（第七轮）：等价于 saved_bytes，仅为兼容保留。"""
        return self.saved_bytes


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
        # 三个字节事实（契约 §1.4）：
        #   生成量：模型交给缓冲的全部字节（含后来没能保存的部分）
        self.generated_bytes = 0
        #   保存量：成功保存的字节（内存 + 成功写入暂存）——「整轮有没有回答内容」看它
        self.saved_bytes = 0
        # 如实记录「交付不完整」的**种类与原因**（绝不偷偷丢字、绝不改角色）：
        # limit（硬上限截断）/ spill_create / spill_write / spill_read。
        # 事实由 AgentLoop 写成可见事件 + 轮次警告（只写日志不算交付）。
        self.failure_kind: str | None = None
        self.failure_reason: str | None = None

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
        # 生成量在**任何**取舍之前记账：硬上限截断 / 写失败时它仍然如实包含丢掉的字节。
        self.generated_bytes += size
        room = self._memory_limit - self._memory_bytes
        if size <= room:
            self._memory.append(text)
            self._memory_bytes += size
            self.saved_bytes += size
            return
        # 切分是 CPU 活：放到线程里，别堵事件循环（大分块时尤其明显）。
        head, tail = await asyncio.to_thread(_split_by_bytes, text, room)
        if head:
            used = len(head.encode("utf-8"))
            self._memory.append(head)
            self._memory_bytes += used
            self.saved_bytes += used
        if tail:
            await self._append_spill(tail)

    async def _append_spill(self, text: str) -> None:
        room = self._spill_limit - self._spill_bytes
        if room <= 0:
            self._fail("limit", f"达到暂存硬上限 {self._spill_limit} 字节")
            return
        keep, dropped = await asyncio.to_thread(_split_by_bytes, text, room)
        if dropped:
            self._fail("limit", f"达到暂存硬上限 {self._spill_limit} 字节")
        if not keep:
            return
        data = keep.encode("utf-8")
        try:
            await self._ensure_spill_file()
        except OSError as exc:  # 建不了暂存文件：如实报告（内存部分照常交付）
            self._fail("spill_create", f"暂存文件创建失败（{type(exc).__name__}: {exc}）")
            return
        try:
            await asyncio.to_thread(self._write_sync, data)
        except OSError as exc:  # 写不进暂存：如实报告（绝不无界增长、偷偷丢字）
            self._fail("spill_write", f"暂存写入失败（{type(exc).__name__}: {exc}）")
            return
        self._spill_bytes += len(data)
        self.saved_bytes += len(data)

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

    @property
    def truncated(self) -> bool:
        """交付是否不完整（硬上限截断或暂存故障）。"""
        return self.failure_kind is not None

    # 交付不完整时的种类优先级：存储故障比资源上限更严重（读取故障绝不能被
    # 「超过上限」这个说法盖住），所以保留**最严重**的那一条事实。
    _FAILURE_RANK = {"limit": 0, "spill_create": 1, "spill_write": 2, "spill_read": 3}

    def _fail(self, kind: str, reason: str) -> None:
        """记下「交付不完整」的事实（同类只记第一次，更严重的覆盖较轻的）。"""
        current = self.failure_kind
        if current is not None and self._FAILURE_RANK.get(current, 0) >= self._FAILURE_RANK.get(
            kind, 0
        ):
            return
        # 原因会进可见事件 / 轮次警告：按项目约定过脱敏（异常文本可能带路径/密钥形态）。
        from agent.trace.redact import redact_text

        reason = redact_text(reason)
        self.failure_kind = kind
        self.failure_reason = reason
        logger.warning(
            "undeclared answer incomplete (%s): %s (delta=%s)", kind, reason, self._delta_id
        )

    # -- 读取与清理 -------------------------------------------------------

    async def collect(self) -> BufferOutcome:
        """按序拼回全部正文（内存 + 暂存），返回**结构化**交付结果（契约 §1.4）。

        **按字节事实核对**：暂存里成功写入多少字节（self._spill_bytes）就有权期望
        读回多少字节。缺失 / 截短 / 异常增长 / 不是合法 UTF-8（含中文末字被截断的
        多字节边界）都**不算**完整交付：

        * 只交付**能确认**的内容（内存部分 + 校验通过的那段暂存前缀），
          **绝不用 replacement 字符掩盖损坏**，也不多交付异常增长出来的字节；
        * 事实写进 kind/reason（**存储损坏不得被描述成「正文超过上限」**），
          由 AgentLoop 写成可见事件 + 轮次警告；不完整事实不靠改正文来表达；
        * 内存里的部分永远保留、**原样**返回（交付内容就是已确认可交付的正文本身，
          字节数如实可核）；
        * 句柄关闭，文件留给 discard 清理（事实已经在结果里，不会被清理吞掉）。
        """
        parts = list(self._memory)
        await self._close_spill()
        path = self._spill_path
        if path is not None:
            try:
                data = await asyncio.to_thread(path.read_bytes)
                parts.append(self._confirm_spill_bytes(data))
            except OSError as exc:  # 读不回来就如实少这部分，不编造
                self._fail("spill_read", f"暂存读取失败（{type(exc).__name__}: {exc}）")
        text = "".join(parts)
        delivered = len(text.encode("utf-8"))
        return BufferOutcome(
            text=text,
            complete=self.failure_kind is None,
            kind=self.failure_kind or "complete",
            reason=self.failure_reason,
            generated_bytes=self.generated_bytes,
            saved_bytes=self.saved_bytes,
            delivered_bytes=delivered,
        )

    def _confirm_spill_bytes(self, data: bytes) -> str:
        """核对读回的暂存字节：只返回**能确认**的那部分（长度 + UTF-8 边界都核对）。

        成功写入的字节数是 self._spill_bytes（工作线程里写成功后累加的**真实事实**）：
        * 读回更少 → 截短 / 清空；
        * 读回更多 → 异常增长（只认前 self._spill_bytes 字节）；
        * 不是合法 UTF-8（含末尾多字节字符被截断）→ 截到合法边界，丢弃坏字节并如实说明。
        """
        expected = self._spill_bytes
        actual = len(data)
        if actual < expected:
            self._fail(
                "spill_read",
                f"暂存文件被截短：成功写入暂存 {expected} 字节，实际只读回 {actual} 字节",
            )
        elif actual > expected:
            self._fail(
                "spill_read",
                f"暂存文件异常增长：成功写入暂存 {expected} 字节，实际读到 {actual} 字节；"
                "只交付已确认的前一段",
            )
            data = data[:expected]
        if not data:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # 不用 replacement 字符掩盖：截到最后一个合法边界（exc.start 之前的字节
            # 一定是合法 UTF-8），丢弃其后的坏字节并如实报告数量。
            valid = data[: exc.start].decode("utf-8")
            self._fail(
                "spill_read",
                f"暂存内容不是合法 UTF-8（第 {exc.start} 字节起损坏，"
                f"已丢弃其后 {len(data) - exc.start} 字节）",
            )
            return valid

    async def discard(self) -> None:
        """删掉暂存文件（收尾 / 取消 / 断流后），不留待决任务。

        句柄已经关过（collect() 读过）就**不再关一次**：关闭/破坏类的外部观测
        （例如验证装置在 _close_spill 上挂的断言）只应该看到「这一次真正的收尾」。
        """
        if self._spill_handle is not None:
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
