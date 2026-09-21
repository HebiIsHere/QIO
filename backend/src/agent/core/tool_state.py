"""工具执行的**权威事实**（进程级、纯内存、有界）。

为什么需要这一层：`EventBus` 是**实时通知**渠道，允许在极端积压时丢弃过程事件
（`TOOL_START` / `TOOL_END` 都在其中）。但「这次工具最终是成功、失败还是取消」
是服务器**已经知道的事实** —— 它不能因为一条通知没送到就永久变成 `unknown`。

所以这里维护一份轻量、独立于事件流的权威状态：

    active tool executions + recent terminal tool executions

它不是事件日志、不是工具输出归档，也不落盘：

* 每条只留 `tool_call_id` / `tool_name` / `turn_id` / `status` / 起止时间 / 一行错误摘要；
* 身份是 `(turn_id, tool_call_id)`：同一次调用原位更新，跨 Turn 不会互相污染；
* 终态记录按 TTL + 最大条数回收，但 **active Turn 的记录永不提前回收**
  （Turn 还没结束就把它的工具状态 prune 掉，等于又把可恢复的事实变回 unknown）；
* 进程重启后这份状态本来就是空的 —— 那时服务器确实不知道结果，
  界面显示「结果未收到」是诚实答案（见 `docs/status.md` 的接受限制）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
CANCELLED = "cancelled"
# 服务器自己也无法确认结果时的投影（不是写进状态的取值）。
UNKNOWN = "unknown"

TERMINAL_STATUSES = (SUCCESS, FAILED, CANCELLED)

# 终态记录保留多久 / 最多多少条。目标只有一个：
# 「当前或刚结束的一轮，断线重连后工具卡还能恢复成真实结果」。
DEFAULT_TTL_SECONDS = 1800.0
DEFAULT_MAX_RECORDS = 200
# 错误摘要只给用户看一行：完整输出属于工具自己的呈现，不在这里重复存。
ERROR_SUMMARY_CHARS = 200


def _summarize(error: Any) -> str | None:
    text = " ".join(str(error or "").split())
    if not text:
        return None
    return text[:ERROR_SUMMARY_CHARS]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass
class ToolExecution:
    turn_id: str
    tool_call_id: str
    tool_name: str
    status: str = RUNNING
    started_at: str = ""
    ended_at: str | None = None
    error_summary: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def as_dict(self, *, status: str | None = None) -> dict:
        return {
            "turn_id": self.turn_id or None,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": status or self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error_summary": self.error_summary,
        }


class ToolExecutionState:
    """主 Turn 工具执行的当前事实（唯一来源，与事件流是否完整无关）。"""

    def __init__(
        self,
        *,
        ttl_seconds: float | None = DEFAULT_TTL_SECONDS,
        max_records: int = DEFAULT_MAX_RECORDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_records = max(1, int(max_records))
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )
        # dict 保序：快照按调用发生的顺序返回（前端只按 call_id 匹配，顺序只影响可读性）。
        self._records: dict[tuple[str, str], ToolExecution] = {}

    # -- writes -----------------------------------------------------------

    def start(
        self, turn_id: str | None, tool_call_id: str, tool_name: str = ""
    ) -> dict | None:
        """记录一次工具调用开始。没有稳定身份（call_id / tool_name 都缺）时不记录。"""
        key = self._key(turn_id, tool_call_id, tool_name)
        if key is None:
            return None
        record = ToolExecution(
            turn_id=str(turn_id or ""),
            tool_call_id=key[1],
            tool_name=str(tool_name or ""),
            status=RUNNING,
            started_at=self._now(),
        )
        self._records[key] = record
        self.prune(active_turn_id=turn_id)
        return record.as_dict()

    def finish(
        self,
        turn_id: str | None,
        tool_call_id: str,
        status: str,
        *,
        tool_name: str | None = None,
        error: Any = None,
    ) -> dict | None:
        """记录一次工具调用的**终态**（success / failed / cancelled）。

        终态本身就是权威事实：即使 `TOOL_START` 也丢了，这里也会补出一条记录，
        而不是因为「没有开始记录」把这个结果一起丢掉。
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"not a terminal tool status: {status!r}")
        key = self._key(turn_id, tool_call_id, tool_name or "")
        if key is None:
            return None
        record = self._records.get(key)
        if record is None:
            record = ToolExecution(
                turn_id=str(turn_id or ""),
                tool_call_id=key[1],
                tool_name=str(tool_name or ""),
                status=RUNNING,
                started_at=self._now(),
            )
            self._records[key] = record
        if tool_name:
            record.tool_name = str(tool_name)
        record.status = status
        record.ended_at = self._now()
        record.error_summary = _summarize(error)
        self.prune(active_turn_id=turn_id)
        return record.as_dict()

    def forget(self, turn_id: str) -> None:
        """丢掉某个 Turn 的全部记录（只用于测试与显式清理）。"""
        for key in [k for k in self._records if k[0] == str(turn_id or "")]:
            self._records.pop(key, None)

    # -- reads ------------------------------------------------------------

    def snapshot(
        self,
        *,
        active_turn_id: str | None = None,
        turn_id: str | None = None,
    ) -> list[dict]:
        """当前工具执行事实的投影（供 runtime snapshot 使用）。

        规则：

        * `running` 记录只有在它属于当前 active Turn 时才如实报 `running`；
          所属 Turn 已经不在了，服务器就无法替它保证「还在跑」→ 报 `unknown`；
        * 终态原样返回（`success` / `failed` / `cancelled`），
          这就是「TOOL_END 丢了也能恢复」的依据。
        """
        out: list[dict] = []
        for record in self._records.values():
            if turn_id is not None and record.turn_id != str(turn_id):
                continue
            status = record.status
            if status == RUNNING and record.turn_id != str(active_turn_id or ""):
                status = UNKNOWN
            out.append(record.as_dict(status=status))
        return out

    def count(self) -> int:
        return len(self._records)

    # -- retention --------------------------------------------------------

    def prune(self, *, active_turn_id: str | None = None) -> None:
        """回收记录：先 TTL，再最大条数；**active Turn 的记录一律保留**。"""
        now = self._clock()
        active = str(active_turn_id or "")
        if self._ttl is not None:
            for key, record in list(self._records.items()):
                if record.turn_id == active:
                    continue
                anchor = _parse_ts(record.ended_at if record.terminal else record.started_at)
                if anchor is None:
                    continue
                if (now - anchor).total_seconds() > self._ttl:
                    self._records.pop(key, None)
        while len(self._records) > self._max_records:
            victim = self._oldest_evictable(active)
            if victim is None:
                # 剩下的全是 active Turn 的活状态：宁可短暂超限，也不能丢当前事实
                break
            self._records.pop(victim, None)

    def _oldest_evictable(self, active_turn_id: str) -> tuple[str, str] | None:
        oldest: tuple[str, str] | None = None
        oldest_key = ""
        for key, record in self._records.items():
            if record.turn_id == active_turn_id:
                continue
            sort_key = record.ended_at or record.started_at
            if oldest is None or sort_key < oldest_key:
                oldest, oldest_key = key, sort_key
        return oldest

    # -- internals --------------------------------------------------------

    def _now(self) -> str:
        return self._clock().isoformat()

    @staticmethod
    def _key(
        turn_id: str | None, tool_call_id: str | None, tool_name: str | None
    ) -> tuple[str, str] | None:
        """身份 = `(turn_id, tool_call_id || tool_name)`。

        `tool_call_id` 是匹配工具卡的稳定身份；同一个 Turn 里 `read_file` 连续调用三次
        必须能被区分，所以只有在调用方完全没给 id 时才退回工具名。
        """
        identity = str(tool_call_id or tool_name or "").strip()
        if not identity:
            return None
        return (str(turn_id or ""), identity)
