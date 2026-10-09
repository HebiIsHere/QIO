"""Agent loop state machine.

States: PLANNING -> (TOOL_EXEC -> OBSERVING -> PLANNING) | DONE
Termination: no tool calls requested, or budget exhausted (STOPPED unless
force_continue). Tool failures are isolated per call (WARNING events).
Memory hooks are placeholders until M6/M7.

工具执行走 ToolRegistry 的执行管线（tool/start → pre/execute/post → tool/result
→ tool/end）：本循环在构造时把内部事件转发为 SSE 的 TOOL_START/TOOL_END，
并在 tool/result 监听器里执行 tool_trace 审计；turn 结束时卸载监听器。

本循环**不**发 TURN_START / TURN_END：turn 的生命周期由 core/turn.py 的
TurnManager 单独负责（子 agent、维护任务也复用本循环，它们不是 turn，
以前会把主 turn 的界面状态提前结束掉）。

取消语义：`is_cancelled` 在每个模型调用 / 工具调用 / 迭代边界被检查；
一旦取消，本循环立刻停止，不再发起任何新的模型或工具调用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from agent.adapters.errors import UnsupportedCapability
from agent.adapters.base import (
    STREAM_DONE,
    STREAM_TEXT,
    STREAM_TOOL_CALL,
    AdapterMode,
    BaseAdapter,
    ChatMessage,
    Completion,
    StreamDelta,
    ToolSpec,
)
from agent.api.events import EventType, make_event
from agent.api.bus import EventBus
from agent.core.budget import IterationBudget, default_iterations
from agent.core.guard import GuardVerdict, RunawayGuard
from agent.core.narrative import parse_narrative
from agent.core.turn import short_turn_id
from agent.core import tool_feedback
from agent.core.tool_state import CANCELLED, FAILED, SUCCESS, ToolExecutionState
from agent.core.turn_facts import TurnFacts
from agent.core.answer_buffer import AnswerBuffer
from agent.core.progress import ProgressTracker
from agent.prompts import ANSWER_FALLBACK_HINT, ANSWER_MARKER, CONTENT_ROLE_PROTOCOL
from agent.tools.registry import ToolRegistry
from agent.tools.base import ToolResult

logger = logging.getLogger(__name__)


def _terminal_tool_status(data: dict) -> str:
    """管线结束事件 → 工具终态语义。

    取消是独立语义（`cancelled`），不能因为 `ok=False` 就并进 `failed`。
    """
    if data.get("cancelled"):
        return CANCELLED
    return SUCCESS if data.get("ok") else FAILED


# 发布节奏（plan §2.1）：累计快照最多攒这么久 / 这么多字符就发一次（先到者为准）。
# 这里**没有**分类窗口：正文一到达就进过程区，角色只在「这次调用结束」这个
# 系统事实上定论（见 _AssistantStream 与 plan §1.1，2026-10-06 审计修复）。
PUBLISH_MS = 40
PUBLISH_CHARS = 24

# 内容角色协议（第五轮契约 §1.1）：**角色只由模型在正文开头的声明决定**
# （prompts.ANSWER_MARKER），不由调用类型、延迟、字数、是否含代码块猜。
# 说明文字集中在 agent/prompts.py，由这一个常量注入三档（text 档进 system prompt，
# native / anthropic 由 _call_hint() 每次调用作为最后一条 system 消息发出）。
#
# 控制前缀（判定声明所需的全部字符）：声明本身 + 其后可能的 LF / CRLF。
# **只有这些字符参与角色判定**，任何超出部分都是正文（契约 §1.2）—— 旧实现把
# 「累计收到的正文长度」当成「声明是否有效」的证据（len(probe) > 32 → 未声明），
# 于是「合法声明 + 长正文同一大分块」被整段判成未声明，连声明一起泄漏。
PROBE_LIMIT = len(ANSWER_MARKER) + 2


def _split_declared_answer(text: str) -> tuple[bool, str]:
    """整段正文按内容角色协议解析：返回 (是否声明了回答, 去掉声明后的正文)。

    契约 §1.1 第 1、5 条：声明必须**在开头**（大小写不敏感，其后可跟一个换行）；
    不在开头 / 被拆坏 / 重复出现 → 非法，按「未声明」处理（原文照实保留，
    不猜测、不改写、不丢字）。
    """
    if not text:
        return False, text
    marker = ANSWER_MARKER.lower()
    if text[: len(ANSWER_MARKER)].lower() != marker:
        return False, text
    rest = text[len(ANSWER_MARKER):]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n") or rest.startswith("\r"):
        rest = rest[1:]
    if rest[: len(ANSWER_MARKER)].lower() == marker:
        return False, text  # 重复声明 = 非法
    return True, rest


def _redact_published(text: str, *, holdback: bool) -> str:
    """对外发布的正文统一脱敏（冻结契约 C3）。

    holdback=True（流式增量）时，先扣留「可能是某个已登记敏感值开头」的最长
    尾部，再走 redact_text；等后续分块打破对齐（或收尾）再放行。没有登记敏感值
    时扣留长度为 0 —— 正常流式完全不受影响，不退化为整段生成后显示。
    """
    if not text:
        return text
    from agent.trace.redact import redact_text, undecided_tail_length

    if holdback:
        tail = undecided_tail_length(text)
        if tail:
            text = text[: len(text) - tail]
    return redact_text(text)


# 结束语义（冻结契约 C2）里由模型调用本身产生的 reason_code：只有最后一次调用
# 的结束事实才算数，一次后续的正常结束（例如兜底回答）会把它们清掉。
_TERMINATION_CODES = frozenset({"incomplete_stream", "length_limit", "content_filter"})


class _StreamEnd:
    """流结束哨兵（消费侧据此收口）。"""

    __slots__ = ()


class _StreamError:
    """adapter 在流中抛出的异常（原样交给消费侧，不在这里吞掉）。"""

    __slots__ = ("error",)

    def __init__(self, error: Exception) -> None:
        self.error = error


class _UnsupportedStream:
    """adapter 声明支持流式、实际抛了 NotImplementedError（走整段降级）。"""

    __slots__ = ("error",)

    def __init__(self, error: Exception) -> None:
        self.error = error


_STREAM_END = _StreamEnd()


class _AssistantStream:
    """一条模型调用（一个 delta_id）对应的流式发布器（第四轮契约 §1.1）。

    角色规则：**只看这次调用带不带工具** —— 发起之前就知道，不含任何启发式：

    角色**只由模型在正文开头的声明决定**（第五轮契约 §1.1），按最长可能前缀
    流式判定（最多 MARKER_PROBE_CHARS 字节即可判定）：

    * 缓冲与声明**完全匹配** → role = "answer"：声明之后的正文**实时**进正式回答区
      （``interim=false, streaming=true``）；调用结束时同一 delta_id 再发一条
      ``streaming=false`` 的累计快照做**收尾校准**（校准不是首次展示来源）。
    * 缓冲与声明**前缀不再匹配** → **按「未声明」处理**（契约 §1.2：不得因为前缀
      不匹配就立刻把正文放进过程区）：role = "undeclared"，正文进有界缓冲
      （内存 ≤ UNDECLARED_MEMORY_LIMIT 个 UTF-8 字节，超出部分落临时暂存 ——
      上限**只管理资源、不决定角色**，超限不改判、也不构成「有工具调用」的证据）。
      只有**出现工具调用**才会让缓冲中的正文提前按工作调用实时放行到过程区；
      这一批工具的阶段就位后由 flush_interim 用同一个 delta_id 补上
      ``stage_id`` / ``call_ids``（同一份文字，不产生第二个气泡）。
    * 未声明正文在调用结束时：有工具调用 → 过程说明，放行到过程区（不丢字）；
      无工具调用 → 正式回答，**一次性**交付到正式回答区（``interim=false,
      streaming=false``、``role_evidence="undeclared_answer"``，协议未遵守的
      降级路径，不冒充流式、不重新生成、过程区不留副本）。
    * 非法（不在开头 / 被拆坏 / 重复）→ 按「未声明」处理。
    * 取消 / 断流 / 失败（completion=None）→ 已收到的文字放行到过程区（不丢字、
      不猜角色）。

    判据里没有：经过多少时间、文案像不像答案、暂未收到工具增量、某 kind 变化，
    也**没有**「这次调用带不带工具」——带工具同样可以给出正式回答。
    已经进入正式回答区的文字**永不**移动。

    发布节奏：按 ≥PUBLISH_MS 或 ≥PUBLISH_CHARS 合并一次，不逐字符发。
    """

    def __init__(
        self,
        emit: Callable[[dict], Any],
        *,
        delta_id: str,
        publish_ms: int = PUBLISH_MS,
        publish_chars: int = PUBLISH_CHARS,
        clock: Callable[[], float] = time.monotonic,
        answer_expected: bool = False,
        spill_dir: Path | str | None = None,
        memory_limit: int | None = None,
        spill_limit: int | None = None,
    ) -> None:
        self._emit = emit
        self.delta_id = delta_id
        self._publish_ms = publish_ms
        self._publish_chars = publish_chars
        self._clock = clock
        # 未声明正文的退路（契约 §1.3）：有界内存 + 溢出暂存。上限只管理资源，
        # 不决定角色；暂存目录缺省时按 <data_dir>/tmp 惰性解析。
        self._spill_dir = spill_dir
        self._memory_limit = memory_limit
        self._spill_limit = spill_limit
        # 这次调用**是否被要求给出回答**（tools=[]：兜底调用）。它不决定角色
        # （角色只看正文声明），只决定「未声明正文」在调用结束时去哪：被要求回答的
        # 调用不可能产出过程说明，未声明也按正式回答一次性交付。
        self._answer_expected = answer_expected
        # None（还没判定）/ "interim"（过程区）/ "answer"（正式回答）/
        # "undeclared"（未声明：有界缓冲，调用结束时再决定去哪）
        self.role: str | None = None
        # 控制前缀缓冲（≤ PROBE_LIMIT 个字符；只用于判定声明，不承载正文）
        self._probe = ""
        # 未声明正文的缓冲（有界内存 + 溢出暂存；上限不改角色）
        self._undeclared: AnswerBuffer | None = None
        # 取走缓冲时的结构化结果（含 limit / spill_* 故障事实）——**collect 之后、
        # 清理之前**记在这里，之后由 AgentLoop 写成可见事件 + 轮次警告（契约 §1.4）。
        self._buffer_outcome: BufferOutcome | None = None
        # 这条流交付到正式回答区的正文（声明本身已经去掉）；循环用它做 final_content
        self.answer_text = ""
        # 是否走了「未声明 → 一次性交付」的降级路径（如实记录，不冒充流式）
        self.undeclared_answer_used = False
        # 角色判据的**证据**：只用于取证与测试断言，不参与任何判定。
        self.role_evidence: str | None = None
        self._pending = ""
        self._pending_since: float | None = None
        self._confirmed = ""
        self._seq = 0
        # 是否已经向前端发过任何 ASSISTANT 事件（决定是否需要一次性降级）。
        self.published = False
        # 工具轮的 stage_id / call_ids：阶段就位后由 flush_interim 补上。
        self._stage_id: str | None = None
        self._call_ids: list[str] = []
        # 有没有收到过**正文**增量（整段返回 vs 真流式的判据）。
        self._saw_text = False
        # 这条流有没有真的收到过增量。整段返回（供应商忽略 stream）时为 False ——
        # 上层据此如实提示「该模型路径不支持实时生成」。
        self.saw_delta = False

    # -- 输入 -------------------------------------------------------------

    async def note_text(self, text: str) -> None:
        """一段正文增量：先按内容角色协议判定角色，再按发布节奏实时发出去。

        角色由**模型在正文开头的声明**决定（第五轮契约 §1.1）：声明匹配 → 回答
        调用（正式回答区）；前缀不再匹配 → 工作调用（过程区）；还判不出来（缓冲
        仍是声明的可能前缀）→ 先不展示。未声明的正文先有界缓冲，调用结束时再决定
        去过程区还是回答区。
        """
        if not text:
            return
        self.saw_delta = True
        self._saw_text = True
        if self.role is None:
            await self._feed(self._consume_prefix(text))
            return
        await self._feed(text)

    def _consume_prefix(self, text: str) -> str:
        """只把**控制前缀**放进 _probe；返回这一块里要交付的正文（可能为空串）。

        契约 §1.2：判定只依赖控制前缀（≤ PROBE_LIMIT 个字符），任何超出部分都是
        正文；匹配成功后**同一分块里剩下的正文立即交给回答流**（实时发布）。
        """
        need = PROBE_LIMIT - len(self._probe)
        head, tail = text[:need], text[need:]
        self._probe += head
        feed = self._resolve_probe()
        if feed is None:
            # 还没判定 ⟺ 这一块整体没超出控制前缀（判定所需长度是 PROBE_LIMIT，
            # 吃满就一定有结论）→ 没有正文被丢掉。
            if tail:  # pragma: no cover - 防御：真到了这里也绝不丢字、绝不提前展示
                pending = self._probe
                self._start_undeclared()
                return pending + tail
            return ""
        return feed + tail

    async def note_tool_call(self) -> None:
        """出现工具调用增量：未声明的正文按工作调用**实时放行**。

        角色只由正文声明决定，但工具调用是「这条响应是工具轮」的直接证据：还在
        缓冲的正文（未声明）此刻按进度说明实时进过程区（不丢字、不猜角色）。
        声明之后的迟到工具调用不改任何已发布文字的角色 —— 由 AgentLoop 发可见
        警告并按已声明回答收尾（绝不隐藏真实工具调用、绝不把回答移回过程区）。
        """
        self.saw_delta = True
        if self.role in (None, "undeclared"):
            self.role = "interim"
            self.role_evidence = None
            # 工具调用是「这条响应是工具轮」的直接证据：缓冲正文按过程规则
            # **按序完整**放行到过程区（内存 + 暂存一起），不丢字。
            text = self._probe + await self._take_buffered()
            self._probe = ""
            self._pending += text
            if self._pending_since is None:
                self._pending_since = self._clock()
            await self._flush(force=True)

    # -- 角色判定（内容角色协议）------------------------------------------

    def _resolve_probe(self) -> str | None:
        """判定内容角色：None = 还不能判定（继续等）；否则返回要交付的正文。

        契约 §1.2 规则：完全匹配声明 → "answer"（声明本身吃掉，剩下的正文立刻交付）；
        前缀不匹配 / 重复声明 → "undeclared"（进缓冲，**不是**立刻当工作调用）；
        仍是声明的可能前缀 → None（不展示、不丢字）。
        """
        probe = self._probe
        marker = ANSWER_MARKER.lower()
        lowered = probe.lower()
        if len(probe) < len(ANSWER_MARKER):
            if marker.startswith(lowered):
                return None  # 还可能是声明：等更多字符
            self._start_undeclared()
            return probe
        if not lowered.startswith(marker):
            self._start_undeclared()
            return probe
        rest = probe[len(ANSWER_MARKER):]
        if rest == "" or rest == "\r":
            # 声明后可能跟一个换行：再等一个字符（收尾时按「没有换行」处理）
            return None
        if rest.startswith("\r\n"):
            rest = rest[2:]
        elif rest.startswith("\n") or rest.startswith("\r"):
            rest = rest[1:]
        if rest[: len(ANSWER_MARKER)].lower() == marker:
            # 重复声明 = 非法：按未声明处理（原文照实保留，不猜测、不改写）
            self._start_undeclared()
            return probe
        self.role = "answer"
        self.role_evidence = "declared_answer"
        self._probe = ""
        return rest

    def _start_undeclared(self) -> None:
        """未声明（无标记 / 非法）：正文进有界缓冲，调用结束时再决定去哪。"""
        self.role = "undeclared"
        self.role_evidence = None
        self._probe = ""

    def _probe_is_complete_declaration(self) -> bool:
        """_probe 是否已经是一个完整合法声明（只是还没等到正文）。

        只有两种「未判定」形态可能是完整声明：声明本身、或声明后跟一个孤立的
        CR（LF / CRLF 在 _resolve_probe 里已经判成 answer）。完整声明必须被吃掉，
        绝不能当正文发出去（审计 F19）。
        """
        marker = ANSWER_MARKER.lower()
        lowered = self._probe.lower()
        return lowered == marker or lowered == marker + "\r"

    async def _flush_unresolved_probe(self) -> None:
        """收尾时处理还没走完判定的控制前缀（审计 F19）。

        完整声明 → 按回答调用收尾（正文为空）；否则 → 按「未声明」处理，并把
        已收到的前缀字符原样喂进缓冲。旧实现先清空 _probe 再喂它，等于把这段
        已收到的文字静默丢掉。
        """
        if self.role is not None:
            return
        if self._probe_is_complete_declaration():
            self.role = "answer"
            self.role_evidence = "declared_answer"
            self._probe = ""
            return
        pending = self._probe
        self._start_undeclared()
        await self._feed(pending)

    async def _feed(self, text: str) -> None:
        """按已判定的角色分发正文：实时发布 / 有界缓冲（内存 + 暂存）。"""
        if not text:
            return
        if self.role == "undeclared":
            # 上限只管理资源（内存 + 溢出暂存），**不决定角色**（契约 §1.3）。
            await self._buffer().append(text)
            return
        self._pending += text
        if self._pending_since is None:
            self._pending_since = self._clock()
        if len(self._pending) >= self._publish_chars:
            await self._flush(force=True)

    def _buffer(self) -> AnswerBuffer:
        """未声明正文的缓冲（惰性创建：只走未声明路径时才分配）。"""
        if self._undeclared is None:
            self._undeclared = AnswerBuffer(
                self.delta_id,
                self._spill_dir,
                memory_limit=self._memory_limit,
                spill_limit=self._spill_limit,
            )
        return self._undeclared

    async def _take_buffered(self) -> str:
        """取走缓冲里的全部正文（内存 + 暂存），并清理暂存文件。"""
        buffer = self._undeclared
        if buffer is None:
            return ""
        # 事实传递（契约 §1.4）：**collect() 之后、discard()/清理之前**先把结果记在
        # 流上 —— 之后由 AgentLoop 写成可见事件 + 轮次警告。只写日志不算交付，
        # 所以这里绝不把故障事实丢掉。
        outcome = await buffer.collect()
        if not outcome.complete:
            self._buffer_outcome = outcome
        await buffer.discard()
        self._undeclared = None
        return outcome.text

    @property
    def buffer_outcome(self) -> BufferOutcome | None:
        """这条流取缓冲的结构化结果（complete / limit / spill_*）；正常时为 None。"""
        return self._buffer_outcome

    async def _absorb_whole_text(self, text: str) -> None:
        """整段正文（一个增量都没收到）也走同一套内容角色判定。"""
        if self.role is not None:
            await self._feed(text)
            return
        feed = self._consume_prefix(text)
        if self.role is None:
            # 整段就到这里：仍是声明的可能前缀（如「[[QIO」）→ 未声明。
            # 先取出 _probe 再清空，绝不吞掉已收到的字符（审计 F19）。
            await self._flush_unresolved_probe()
            return
        await self._feed(feed)

    async def _settle_undeclared(self, *, tool_calls: bool, interrupted: bool) -> None:
        """未声明的缓冲正文在调用结束时去哪（契约 §1.3）。

        * 取消 / 断流 / 失败 → 已收到的文字放行到过程区（不猜角色、不丢字）；
        * 有工具调用（且不是被要求回答的调用）→ 按过程规则**按序完整**放行到过程区；
        * 其余（无工具调用 / 被要求回答的调用）→ 该正文是正式回答：**一次性**
          交付到正式回答区（降级路径，如实记录，不冒充流式、不重新生成）。
        """
        text = await self._take_buffered()
        if interrupted or (tool_calls and not self._answer_expected):
            self.role = "interim"
            self.role_evidence = None
            self._pending += text
            return
        self.role = "answer"
        self.role_evidence = "undeclared_answer"
        self.undeclared_answer_used = True
        self._pending += text

    # -- 时间 -------------------------------------------------------------

    def next_deadline(self) -> float | None:
        """下一个必须处理的发布时刻；None = 可以一直等下一段增量。"""
        if self._pending and self._pending_since is not None:
            return self._pending_since + self._publish_ms / 1000.0
        return None

    async def on_deadline(self) -> None:
        """到发布节奏：把攒下的文字按节奏发出去。"""
        await self._flush(force=False)

    @property
    def emitted(self) -> bool:
        """这次调用的正文是否**已经有人负责发**。

        三种情况都算：已经实时发过（published）、正等 flush_interim 交付（工具轮）、
        或者收尾会一次性交付（_pending 里的文字）。上层据此决定要不要走一次性补发 ——
        只要有正文，就绝不能补发第二次（同一段字会出现两次）。
        """
        buffered = self._undeclared.total_bytes if self._undeclared is not None else 0
        return (
            bool(self._confirmed)
            or bool(self._pending)
            or buffered > 0
            or self.published
        )

    # -- 收尾 -------------------------------------------------------------

    async def finish(self, completion: Completion | None) -> None:
        """流结束 / 中断：收尾，并把没到节奏的文字补齐。

        角色由正文声明在流式过程中判定（见 note_text），这里只做收尾：

        * 已声明回答 → 正文是正式回答：先保证它已经以 streaming=true 的增量出现过
          （没到发布阈值的真实增量在这里补发一次），再补一条 interim=false、
          streaming=false 的累计快照做**收尾校准** —— 首次展示永远不是校准快照。
        * 已判为工作调用 / 有工具调用 → 正文是进度说明：留在过程区；这一批带工具时
          等 flush_interim 补 stage_id / call_ids，其余情况直接收尾。
        * 未判定（未声明）→ 按契约 §1.3：有工具调用 → 按序完整放行到过程区；
          无工具调用 → 内存 + 暂存里的正文**一次性**交付到正式回答区（降级路径）。
        * completion=None（取消 / 失败 / 断流）→ 已收到的文字放行到过程区，只收尾
          （streaming=false：不再增长）。

        没有任何正文时什么都不发（空气泡是无意义噪声）。
        """
        if not self._saw_text and completion is not None:
            # 一段正文增量都没收到、但整段结果里有正文（供应商一次性给出）：
            # 走同一套内容角色判定，不假装是一帧一帧来的。
            text = completion.message.content or ""
            if text:
                await self._absorb_whole_text(text)
        tool_calls = bool(completion.tool_calls) if completion is not None else False
        if self.role is None:
            # 判定没走完（缓冲仍是声明的可能前缀）：完整声明按回答收尾（正文为空），
            # 否则按「未声明」处理；前缀字符原样保留（审计 F19）。
            await self._flush_unresolved_probe()
        if self.role == "undeclared":
            await self._settle_undeclared(tool_calls=tool_calls, interrupted=completion is None)
            if self.role == "interim" and tool_calls:
                return  # 工具轮：等 flush_interim 补阶段信息
            await self._settle()
            if self.role == "answer":
                # 最终交付正文同样脱敏（契约 C3：最终校准 / 历史输出）
                self.answer_text = _redact_published(self._confirmed, holdback=False)
            return
        if self.role == "interim" and tool_calls:
            return  # 工具轮：文字已经实时发过，等 flush_interim 补阶段信息
        if (
            self.role == "answer"
            and self._pending
            and self._saw_text
            and not self.undeclared_answer_used
        ):
            # 声明回答收尾前还有**没到发布阈值**的真实增量：先按累计快照发一条
            # streaming=true，再发 streaming=false 的收尾校准 —— 保证「首次展示
            # 永远不是校准快照」（契约 §1.1）。未声明的降级路径不在此列：
            # 那是一次性交付，不冒充流式。
            await self._flush(force=True)
        await self._settle()
        if self.role == "answer":
            # 这条流交付到正式回答区的正文（声明本身已经去掉），脱敏后交付
            self.answer_text = _redact_published(self._confirmed, holdback=False)

    async def _settle(self) -> None:
        """一次性交出全部已确认文字（同一 delta_id 的累计快照，streaming=false）。"""
        if not self._pending and not self.published:
            return
        self._confirmed += self._pending
        self._pending = ""
        self._pending_since = None
        self._seq += 1
        self.published = True
        await self._emit(self._payload(streaming=False))

    # -- 内部 -------------------------------------------------------------

    async def _flush(self, *, force: bool) -> None:
        if not self._pending:
            return
        if not force:
            elapsed = (
                (self._clock() - self._pending_since) if self._pending_since is not None else 0.0
            )
            if len(self._pending) < self._publish_chars and elapsed * 1000 < self._publish_ms:
                return
        self._confirmed += self._pending
        self._pending = ""
        self._pending_since = None
        self._seq += 1
        self.published = True
        await self._emit(self._payload(streaming=True))

    async def flush_interim(self, *, stage_id: str | None, call_ids: list[str]) -> None:
        """工具轮的已确认正文 → 过程区（**阶段就位之后**才调用）。

        这段文字可能已经随增量实时发过：这里用**同一个 delta_id** 再交一条累计
        快照，把 stage_id / call_ids 补上 —— 前端就地更新成「该阶段的历次说明」，
        而不是两个并列的过程气泡（Lead 裁决 2026-10-06）。
        没有任何正文时不发（空气泡是无意义噪声）。
        """
        if self.role != "interim":
            return
        if not self._pending and not self.published:
            return
        self._stage_id = stage_id
        self._call_ids = list(call_ids or [])
        self._confirmed += self._pending
        self._pending = ""
        self._pending_since = None
        self._seq += 1
        self.published = True
        await self._emit(self._payload(streaming=False))

    def _payload(self, *, streaming: bool) -> dict:
        """累计快照（与既有 ASSISTANT 语义一致）：content = 该 delta_id 全部已确认文字。

        工具轮的增量额外带 stage_id / call_ids：过程区据此把它归到**同一个阶段**
        （plan §1.1「工具归属只看 stage_id」的同一条口径）。正式回答的增量两者为空。
        role_evidence 只在角色有**可靠判据**时有值（见类文档），仅用于取证与断言。
        """
        interim = self.role == "interim"
        return {
            # 先脱敏再发布：累计快照里的完整敏感值在这里被替换；流式增量额外扣留
            # 可能是敏感值开头的尾部（契约 C3），收尾快照不再扣留（不会有后续分块）。
            "content": _redact_published(self._confirmed, holdback=streaming),
            "interim": interim,
            "streaming": streaming,
            "delta_id": self.delta_id,
            "seq": self._seq,
            "stage_id": self._stage_id if interim else None,
            "call_ids": list(self._call_ids) if interim else [],
            "role_evidence": self.role_evidence,
        }


class LoopPhase(str, Enum):
    PLANNING = "planning"
    TOOL_EXEC = "tool_exec"
    OBSERVING = "observing"
    DONE = "done"
    STOPPED = "stopped"


@dataclass
class TurnResult:
    final_content: str | None
    phase: LoopPhase
    iterations_used: int
    tokens_used: int
    tool_calls_made: int
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    # 没来得及读的系统通知（见 AgentLoop.push_notice）：宣布的时候还有下一轮
    # planning 可读，等这一轮结束时却已经没有了。它们必须由上层变成自己的一轮，
    # 否则子任务结果会无声消失。
    unread_notices: list[str] = field(default_factory=list)
    # 本轮被后端核对通过的完成结论（见 core/turn_facts.py）：落进 assistant 消息的
    # raw，并随 TURN_END 发给前端渲染「后端已核对」那一行；没有就是 None。
    verification: dict | None = None
    # 本轮**为什么停下来**的系统事实（plan §1.2）：TURN_END 的
    # reason_code / reason / stopped_by 直接取这里，前端据此显示原因与可用操作。
    # 正常跑完 = "none"；护栏 / 预算 / 无进展 / 用户停止由循环如实记录。
    stop_reason_code: str = "none"
    stop_reason: str | None = None
    stopped_by: str | None = None
    # 系统核对注释（后端事实，见 core/turn_facts.py）：独立字段，绝不拼进
    # final_content（审计 F11：正文与注释分层，前端不会因「全文不等」重复整段回答）。
    final_annotation: str | None = None


class AgentLoop:
    def __init__(
        self,
        adapter: BaseAdapter,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        max_iterations: int | None = None,
        token_budget: int | None = None,
        force_continue: bool = False,
        approvals=None,
        guard: RunawayGuard | None = None,
        continue_batch_iterations: int = 32,
        continue_batch_tokens: int = 12800,
        turn_id: str | None = None,
        trace=None,
        tool_trace=None,
        tool_selector=None,
        max_parallel_tools: int = 4,
        is_cancelled: Callable[[], bool] | None = None,
        tool_state: ToolExecutionState | None = None,
        narrative_sink: Callable[..., Any] | None = None,
        narrative_settler: Callable[..., Any] | None = None,
        stage_id_provider: Callable[[], str | None] | None = None,
        usage_sink: Callable[[int, int], None] | None = None,
        # 未声明长正文的退路（第六轮契约 §1.3）：暂存目录缺省 = <data_dir>/tmp
        # （惰性解析，正常轮次不碰磁盘）；两个上限只管理资源，不决定角色。
        spill_dir: Path | str | None = None,
        undeclared_memory_limit: int | None = None,
        undeclared_spill_limit: int | None = None,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.bus = bus
        mode = AdapterMode(adapter.mode)
        # token_budget 缺省 = 0 = 不限（见 core/budget.py 的产品决定）。
        # 迭代上限 / guard / provider 自身的上限仍然生效 —— 那是防异常循环，
        # 不是用来控制正常回答长度的。
        self.budget = IterationBudget(
            max_iterations=max_iterations or default_iterations(mode),
            token_budget=token_budget or 0,
        )
        self.force_continue = force_continue
        self.approvals = approvals
        self.guard = guard
        self.continue_batch_iterations = continue_batch_iterations
        self.continue_batch_tokens = continue_batch_tokens
        self.turn_id = turn_id
        self.trace = trace
        self._model_seq = 0
        self._halted = False
        # 无进展暂停的原因（同样的调用拿到同样的结果）：由 ProgressTracker 给出，
        # 在下一轮开始时结束本轮 —— 不靠「失败次数」，也不靠「可重试」标签。
        self._no_progress: str | None = None
        # 为什么停下来的人话说明：护栏终止 / 预算停止时记下来，收尾时若一个字
        # 都没产生就把它当回答写出去（空回答等于静默失败）。
        self._stop_note: str | None = None
        # 停下来的**原因代码 / 谁停的**（plan §1.2）：随 TurnResult 交给上层，
        # 由 TURN_END 如实带给前端；没有停下来就保持 None（对外是 "none"）。
        self._stop_code: str | None = None
        self._stop_reason: str | None = None
        self._stop_by: str | None = None
        # 当前这次调用是不是**被要求给出回答**（tools=[]：兜底调用）。它**不是**
        # 角色判据（第五轮契约 §1.1：角色只看正文声明），只决定「未声明的正文」
        # 在调用结束时去哪 —— 被要求回答的调用不可能产出过程说明。
        self._answer_expected = False
        # 当前这次调用的**内容角色**（由 _AssistantStream / _emit_one_shot_assistant
        # 按正文声明判定）："answer" / "interim" / None。
        self._call_role: str | None = None
        # 这次调用交付到正式回答区的正文（声明本身已去掉）；循环用它做 final_content。
        self._call_answer_text = ""
        # 这次调用是否走了「未声明 → 一次性交付」的降级路径（如实记录）。
        self._call_undeclared = False
        # 这次调用「交付不完整」的结构化事实（limit / spill_create / spill_write /
        # spill_read）；有值时如实报告：可见 WARNING + 事实写进交付内容（契约 §1.4）。
        self._call_buffer_outcome: BufferOutcome | None = None
        self._spill_dir = spill_dir
        self._undeclared_memory_limit = undeclared_memory_limit
        self._undeclared_spill_limit = undeclared_spill_limit
        # 每轮最多一次兜底调用；可见警告每轮最多一条。
        self._fallback_used = False
        self._late_tool_warned = False
        self._undeclared_warned = False
        self._truncation_warned = False
        self._warnings: list[str] = []
        self._notices: list[str] = []
        # 统一用量累计（输入 / 输出 / 总量）：供应商差异已经在 Adapter 层消掉
        self._usage_input = 0
        self._usage_output = 0
        self._usage_total = 0
        # 本 loop 派发的工具调用 id；用于过滤 registry 上的跨 loop 事件
        self._dispatched_call_ids: set[str] = set()
        # 每次工具调用的开始时刻：TOOL_END 用它给出耗时（前端卡片显示「1.2s」）。
        # 放在 loop 上而不是 registry 上：registry 是跨 loop 共享的，计时必须按调用归属。
        self._tool_started_at: dict[str, float] = {}
        # 每次工具调用的**系统事实**（终态 + 耗时）：工具卡与叙事摘要共用同一份，
        # 批次结束交给 narrative_settler 写进叙事记录。
        self._tool_facts: dict[str, dict] = {}
        # 工具调用历史：本轮内第几次调用（历史卡片按它排序）+ 待结算的调用/结果
        self._call_seq: dict[str, int] = {}
        self._call_seq_next = 0
        self._pending_tool_io: dict[str, dict] = {}
        # 本轮的「后端事实」台账（见 core/turn_facts.py）：工具终态 + 开发任务状态。
        # 每轮在 `_run` 里新建，收尾时用它决定要不要给最终答复补事实说明。
        self.turn_facts = TurnFacts()
        # 进展判断：每轮一份新的（上一轮的重复不该算到这一轮头上）。
        self.progress = ProgressTracker()
        # 工具执行的**权威事实**（active + recent terminal）。主 Turn 由 AppContext 注入
        # 进程级实例（这样「刚结束的 Turn」的工具结果在重连后仍查得到）；
        # 单独构造 loop（子任务 / 测试）时自建一份私有的，行为一致但不外泄。
        self.tool_state = tool_state or ToolExecutionState()
        self.tool_trace = tool_trace
        self.tool_selector = tool_selector
        # 叙事出口（可选）：主 turn 由 AppContext 注入落库 + 广播；子 agent / 维护
        # 循环不注入，因此不会往主对话里写过程说明。
        self.narrative_sink = narrative_sink
        self.narrative_settler = narrative_settler
        # 当前阶段 id（plan §1.3）：TOOL_START / TOOL_END 带上它；工具归属只看 stage_id，
        # 不靠消息相邻位置。没有阶段（子任务 / 无叙事的主轮）时如实为 None。
        self.stage_id_provider = stage_id_provider
        # 本次模型调用的稳定流式标识，以及「这一次调用的正文是否已经**有人负责发**」：
        # 真流式已发，或工具轮的正文正在等阶段就位（延后发）都算 —— 两种情况都
        # 不能再走一次性的整段补发，否则同一段文字会出现两次（见 _run）。
        self._call_delta_id: str | None = None
        self._stream_emitted = False
        # 「这条路径不支持实时生成」每轮只广播一次，避免降级后每次调用都刷屏。
        self._stream_degraded_warned = False
        # 结束语义警告（incomplete_stream / length_limit / content_filter）每轮一次。
        self._termination_warned = False
        # 当前这条流式响应的发布器：工具轮的文字要等阶段确定后再由它发出。
        self._active_stream: _AssistantStream | None = None
        # 用量归因：每次模型调用把 (进, 出) 报给调用方（由它记到对应凭据上）。
        # 放在每次调用之后而不是整轮结束 —— 中途失败/取消的那部分也已经计费。
        self.usage_sink = usage_sink
        self.max_parallel_tools = max(1, max_parallel_tools)
        self.is_cancelled = is_cancelled or (lambda: False)
        self._active_tool_tasks: set[asyncio.Task] = set()
        # 「停止」要能掐掉正在等待的模型请求，而不是等它自然返回（见 cancel/_await_completion）。
        self._cancel_event = asyncio.Event()
        self._request_aborted = False
        self._disposers: list[Callable[[], None]] = []
        self._bind_pipeline()

    # -- pipeline wiring ---------------------------------------------------

    def _bind_pipeline(self) -> None:
        """把内部工具事件转发到 SSE，并在 tool/result 上做审计。"""
        register = getattr(self.registry, "register_policy", None)
        if register is None:
            return
        self._disposers.append(register("tool/start", self._on_pipeline_start))
        self._disposers.append(register("tool/end", self._on_pipeline_end))
        if self.tool_trace is not None:
            self._disposers.append(register("tool/result", self._on_pipeline_result))

    def dispose(self) -> None:
        """卸载本轮注册的管线监听器。"""
        for disposer in self._disposers:
            try:
                disposer()
            except Exception:  # noqa: BLE001 - cleanup must never break the turn
                logger.warning("pipeline disposer failed", exc_info=True)
        self._disposers.clear()

    async def _on_pipeline_start(self, data: dict) -> None:
        call_id = data.get("call_id")
        if call_id not in self._dispatched_call_ids:
            return  # 非本 loop 的调用，忽略（跨 loop 隔离）
        import time as _time

        if call_id:
            self._tool_started_at[call_id] = _time.perf_counter()
        # 先写权威状态，再发实时事件：通知丢了也不影响最终状态可恢复。
        self.tool_state.start(self.turn_id, str(call_id or ""), str(data.get("tool") or ""))
        # 呈现（present_call）在这里也给：工具「开始执行」的卡片要有中文标题，
        # 而不是等结束才补上（否则运行中的卡显示的是原始工具名）。
        presentation = None
        tool = self.registry.get(str(data.get("tool") or ""))
        if tool is not None:
            try:
                call_present = tool.present_call(dict(data.get("arguments") or {}))
                if call_present:
                    from agent.tools.display import tool_label

                    presentation = {"title": tool_label(tool.name), "tool": tool.name}
                    presentation.update(call_present)
            except Exception:  # noqa: BLE001 - 呈现失败不能影响执行
                presentation = None
        await self._emit(
            EventType.TOOL_START,
            {
                "tool": data.get("tool"),
                # call_id 是前端「原地更新同一张卡」的匹配键：缺了就会被当成两次调用
                "call_id": call_id,
                "arguments": data.get("arguments", {}),
                "presentation": presentation,
                # 工具归属只看阶段标识（plan §1.1）；没有阶段时如实为 null，
                # 前端归入「整轮」，而不是就近猜一个阶段。
                "stage_id": self._current_stage_id(),
            },
        )

    async def _on_pipeline_end(self, data: dict) -> None:
        call_id = data.get("call_id")
        if call_id not in self._dispatched_call_ids:
            return
        duration_ms = None
        tool_name = str(data.get("tool") or "")
        status = _terminal_tool_status(data)
        if call_id:
            import time as _time

            started = self._tool_started_at.pop(call_id, None)
            if started is not None:
                duration_ms = int((_time.perf_counter() - started) * 1000)
        self.tool_state.finish(
            self.turn_id,
            str(call_id or ""),
            status,
            tool_name=tool_name,
            error=data.get("error"),
        )
        pending = self._pending_tool_io.pop(str(call_id or ""), None)
        # 结果事实（类别 / 可重试）随 TOOL_END 一起给：界面不必从 ok 反推失败类型。
        result_facts = (
            tool_feedback.facts(pending["result"]) if pending and pending.get("result") else {}
        )
        record_id = self._record_tool_call(tool_name, data, status, duration_ms, pending)
        if call_id:
            # 合并写入：`record_id` 由上面刚写下的历史记录给出，整条覆盖会把它丢掉
            self._tool_facts[str(call_id)] = {
                **(self._tool_facts.get(str(call_id)) or {}),
                "status": status,
                "duration_ms": duration_ms,
                "error": data.get("error"),
                "record_id": record_id,
            }
        await self._emit(
            EventType.TOOL_END,
            {
                "tool": data.get("tool"),
                "call_id": call_id,
                "ok": data.get("ok"),
                # 结局的语义（success / failed / cancelled）随事件一起给，
                # 前端不必从 ok 反推「取消」还是「失败」。
                "status": status,
                "error": data.get("error"),
                "category": result_facts.get("category"),
                "category_label": result_facts.get("category_label"),
                "recoverable": result_facts.get("recoverable"),
                "content_preview": data.get("content_preview", ""),
                "duration_ms": duration_ms,
                "presentation": data.get("presentation"),
                # 工具调用历史的记录 id：实时卡片靠它取全文（与历史卡片同一条路径）
                "record_id": record_id,
                "stage_id": self._current_stage_id(),
            },
        )

    async def _on_pipeline_result(self, data: dict) -> None:
        result = data.get("result")
        if result is None:
            return
        call = data.get("call")
        if call is None or getattr(call, "id", None) not in self._dispatched_call_ids:
            return
        # 终态（含「取消」）在 tool/end 才权威，而完整输出只有这里拿得到 → 先暂存
        self._pending_tool_io[str(call.id)] = {"call": call, "result": result}

    def _record_tool_call(self, tool_name: str, data: dict, status: str,
                          duration_ms: int | None, pending: dict | None) -> str | None:
        """把这次调用交给审计 / 历史回调；返回记录 id（没有回调或失败则 None）。

        审计与历史都不得影响工具结果，也不得让这一轮失败。
        """
        if self.tool_trace is None or pending is None:
            return None
        call = pending["call"]
        result = pending["result"]
        try:
            record_id = self.tool_trace({
                "tool_name": tool_name,
                "arguments": getattr(call, "arguments", {}),
                "ok": result.ok,
                "result": result.content,
                # 失败原因也要进轨迹：只落输出正文时，失败的调用落下来是空白
                "error": result.error,
                "call_id": str(data.get("call_id") or ""),
                "turn_id": self.turn_id,
                "seq": self._call_seq.get(str(data.get("call_id") or ""), 0),
                "duration_ms": duration_ms,
                "status": status,
            })
            return str(record_id) if record_id else None
        except Exception:  # noqa: BLE001 - tracing must not break the loop
            logger.warning("tool trace failed for %s", tool_name, exc_info=True)
            return None

    def _record_turn_facts(self, call, result: ToolResult) -> None:
        """把一次调用的终态与工具上报的事实记进本轮台账（只记账，不改执行）。

        见 core/turn_facts.py：台账是「最终答复事实校正」的唯一依据，
        它只认后端已经知道的事实（工具终态 + 开发工具上报的任务状态）。
        """
        self.turn_facts.record_tool(
            call_id=str(getattr(call, "id", "") or ""),
            tool_name=str(getattr(call, "name", "") or ""),
            ok=bool(result.ok),
            status=tool_feedback.status_of(result),
            category=result.category,
            error=result.error,
        )
        self.turn_facts.record_facts(result.facts)

    # -- parallel dispatch & cancellation -----------------------------------

    async def _emit_batch_narrative(self, calls) -> str | None:
        """一次工具批次最多输出一条叙事（批内第一条有效叙事胜出）。

        叙事只描述"这一批要做什么"：连续的低价值读取要么不写，要么合并成一句 ——
        这正是"不要求每次工具调用都提示"的落点。
        """
        if self.narrative_sink is None:
            return None
        for call in calls:
            narrative = parse_narrative(getattr(call, "narrative", None))
            if narrative is None:
                continue
            try:
                return await self.narrative_sink(
                    self.turn_id, narrative, call, [c.id for c in calls]
                )
            except Exception:  # noqa: BLE001 - 表达失败不得影响工具执行
                logger.warning("narrative emission failed", exc_info=True)
                return None
        return None

    async def _flush_active_stream(self, calls=None) -> None:
        """把这条流延后的过程区文字交出去（阶段就位之后，或不再有后续批次时）。

        ``calls`` 给出这一批工具时，带上它们的 id；没有后续批次（取消）时两者都为空。
        失败只记日志：过程区文字不该影响工具执行。
        """
        stream = self._active_stream
        if stream is None:
            return
        self._active_stream = None
        try:
            await stream.flush_interim(
                stage_id=self._current_stage_id() if calls else None,
                call_ids=[c.id for c in calls] if calls else [],
            )
        except Exception:  # noqa: BLE001 - 过程区失败不得影响执行
            logger.warning("interim flush failed", exc_info=True)

    async def _dispatch_tool_calls(self, calls) -> dict[str, Any]:
        """并发安全工具并行（受 max_parallel_tools 限制），其余串行。

        返回 {call.id: ToolResult}，顺序无关（调用方按原始顺序回填）。
        """
        for c in calls:
            self._dispatched_call_ids.add(c.id)
            self._call_seq_next += 1
            self._call_seq[c.id] = self._call_seq_next
        # 先说明、再执行：叙事事件必须排在本次工具事件之前。
        with self._phase("narrative"):
            narrative_id = await self._emit_batch_narrative(calls)
        # 流式工具轮的正文在**阶段就位之后**才交给过程区：它与刚才这条 STAGE 说明
        # 带同一个 stage_id / call_ids，表现为「同一阶段的历次说明」，
        # 而不是两个并列的过程气泡（Lead 裁决 2026-10-06）。
        await self._flush_active_stream(calls)
        from agent.tools.policy import Concurrency, effective_concurrency

        safe_calls = []
        for c in calls:
            tool = self.registry.get(c.name)
            if tool is not None and effective_concurrency(tool) == Concurrency.PARALLEL:
                safe_calls.append(c)
        safe_ids = {c.id for c in safe_calls}
        unsafe_calls = [c for c in calls if c.id not in safe_ids]
        results: dict[str, Any] = {}

        # 工具等待是一个批次一条顶层细分：并行的多次调用只记批次墙钟，
        # 每次调用自己的耗时仍然在 tool_runs 里（不重复计入合计）。
        with self._phase("tool_wait", f"calls={len(calls)}"):
            # 非安全：严格串行（保持顺序）
            for call in unsafe_calls:
                results[call.id] = await self._guarded_execute(call)

            if safe_calls:
                semaphore = asyncio.Semaphore(self.max_parallel_tools)

                async def _run_safe(call):
                    async with semaphore:
                        return call.id, await self._guarded_execute(call)

                gathered = await asyncio.gather(*(_run_safe(c) for c in safe_calls))
                for call_id, result in gathered:
                    results[call_id] = result
        # 批次结束：把系统知道的真实调用结果补写进叙事记录（失败只记日志）。
        if narrative_id and self.narrative_settler is not None:
            try:
                facts = {c.id: dict(self._tool_facts.get(c.id) or {}) for c in calls}
                await self.narrative_settler(narrative_id, results, calls, facts)
            except Exception:  # noqa: BLE001 - 结算失败不影响工具结果
                logger.warning("narrative settle failed", exc_info=True)
        # 进展判断：按调用顺序看这一批有没有产生新信息（同样的调用 + 同样的结果）。
        # 顺序固定，避免并发批次让判定随调度而变。
        for call in calls:
            result = results.get(call.id)
            if result is None:
                continue
            reason = self.progress.observe(
                tool=call.name,
                arguments=dict(call.arguments or {}),
                ok=bool(getattr(result, "ok", False)),
                result_text=(
                    getattr(result, "content", None) or getattr(result, "error", None) or ""
                ),
            )
            if reason and self._no_progress is None:
                self._no_progress = reason
        return results

    async def _guarded_execute(self, call) -> Any:
        """在独立 task 中执行，登记到 _active_tool_tasks 以便 cancel()。"""
        import time as _time

        # 取消检查点：已取消就不再启动这次工具调用
        if self.is_cancelled():
            return ToolResult(ok=False, error="turn cancelled")
        task = asyncio.current_task()
        if task is not None:
            self._active_tool_tasks.add(task)
        _t0 = _time.perf_counter()
        policy: str | None = None
        try:
            result = await self.registry.execute(call)
            if self.guard is not None:
                verdict = self.guard.observe(call.name, dict(call.arguments), result.ok)
                if verdict == GuardVerdict.BLOCK:
                    policy = "block"
                    result = ToolResult(ok=False, error="guard: 重复失败已被拦截")
                elif verdict == GuardVerdict.HALT:
                    # 阈值到了不再直接终止本轮：先问用户要不要继续（2026-09-22 起）。
                    policy = "halt"
                    failures = self.guard.failures_for(call.name)
                    if self.approvals is None:
                        # 没有审批通道（子任务 / 单元测试）：退回旧的终止行为，不静默继续
                        self._halted = True
                        self._stop_note = self._halt_stop_note(call.name, failures, result.error)
                        self._stop_code = "guard_halt"
                        self._stop_by = "system"
                        self._stop_reason = f"工具 {call.name} 连续失败 {failures} 次"
                        self._warn(
                            f"guard: {call.name} 累计失败 {failures} 次，已终止本轮（无审批通道）"
                        )
                    else:
                        with self._phase("approval_wait", "tool_failures"):
                            decision = await self.approvals.request(
                                "continue",
                                {
                                    "reason": "tool_failures",
                                    "tool": call.name,
                                    "failures": failures,
                                    # 复用预算那条「继续/停止」的通道：把当前预算一并给出，
                                    # 界面不必为"为什么问"单独做一套 UI。
                                    "used_iterations": self.budget.used_iterations,
                                    "max_iterations": self.budget.max_iterations,
                                    "used_tokens": self.budget.used_tokens,
                                    "token_budget": self.budget.token_budget,
                                },
                            )
                        if decision.decision == "approved":
                            self.guard.reset_tool(call.name)
                            self._warn(
                                f"guard: {call.name} 累计失败 {failures} 次，用户选择继续（计数已清零）"
                            )
                        else:
                            self._halted = True
                            self._stop_note = self._halt_stop_note(call.name, failures, result.error)
                            self._stop_code = "guard_halt"
                            self._stop_by = "user"
                            self._stop_reason = f"工具 {call.name} 连续失败 {failures} 次"
                            self._warn(
                                f"guard: {call.name} 累计失败 {failures} 次，用户选择停止本轮"
                            )
                elif verdict == GuardVerdict.WARN:
                    policy = "warn"
                    self._warn(
                        f"guard: {call.name} 已累计失败 {self.guard.failures_for(call.name)} 次，建议换方法"
                    )
            if self.trace is not None:
                self.trace.tool_run(
                    call_id=call.id,
                    tool=call.name,
                    arguments=dict(call.arguments),
                    ok=result.ok,
                    error=result.error,
                    duration_ms=int((_time.perf_counter() - _t0) * 1000),
                    policy=policy,
                    result=result.content,
                )
            return result
        finally:
            if task is not None:
                self._active_tool_tasks.discard(task)

    def cancel(self) -> None:
        """取消本轮：在途工具调用 + 正在等待的模型请求。

        工具那条走 registry（它会把 CancelledError 转成 aborted）；
        模型请求那条由 `_await_completion` 的竞速负责 —— 只设标志位的话，
        请求还是会跑完，用户按了停止却要一直等。
        """
        self._cancel_event.set()
        for task in list(self._active_tool_tasks):
            task.cancel()

    def push_notice(self, text: str) -> None:
        """Queue a system notice; injected before the next PLANNING step."""
        self._notices.append(text)

    def active_tools(self) -> list[dict]:
        """此刻真正在执行中的工具（TOOL_START 到了、TOOL_END 还没到）。

        runtime snapshot 用它回答「现在在跑哪些工具」：客户端在 RESYNC 之后
        把不在这个列表里的「运行中」卡片收口，避免 TOOL_END 丢失后永久转圈。
        """
        return [
            record
            for record in self.tool_state.snapshot(
                active_turn_id=self.turn_id, turn_id=self.turn_id
            )
            if record["status"] == "running"
        ]

    # -- event helpers ----------------------------------------------------

    async def _emit(self, event_type: EventType, data: dict) -> None:
        if self.turn_id:
            data = {**data, "turn_id": self.turn_id}
        await self.bus.publish(make_event(event_type, data))

    def _phase(self, name: str, detail: str | None = None):
        """阶段计时上下文（见 trace/phases.py）；没有 trace 时是空上下文。

        「这一轮的时间去哪了」必须由阶段账本回答，而不是只看模型/工具耗时：
        真实事故是 51.5 秒的一轮只有 1.7 秒模型调用被记录，其余全是 unknown。
        """
        tracer = self.trace
        phase = getattr(tracer, "phase", None)
        if phase is None:
            return nullcontext()
        return phase(name, detail)

    def _warn(self, message: str) -> None:
        from agent.trace.redact import redact_text

        # 警告文本可能引用工具 / 厂商回显：新增可见输出路径必须过脱敏（契约 C3）。
        message = redact_text(str(message))
        self._warnings.append(message)
        logger.warning(message)
        if self.trace is not None:
            self.trace.warning("loop", message)

    @staticmethod
    def _halt_stop_note(tool: str, failures: int, error: str | None) -> str:
        """护栏终止时的收尾文本：哪个工具、失败几次、最后一次为什么失败。"""
        return (
            f"本轮没有产生回答：工具 {tool} 连续失败 {failures} 次后已停止。"
            f"最后一次失败原因：{error or '（没有更多说明）'}。"
        )

    # -- main entry -------------------------------------------------------

    async def run(self, user_message: str) -> TurnResult:
        try:
            return await self._run(user_message)
        finally:
            self.dispose()

    async def _run(self, user_message: str) -> TurnResult:
        messages: list[ChatMessage] = [ChatMessage(role="user", content=user_message)]
        self._warnings = []
        self._stream_degraded_warned = False
        self._termination_warned = False
        # 每轮一份新台账：上一轮的失败不能算到这一轮头上。
        self.turn_facts = TurnFacts()
        # 结束事实是**每轮**的量（同一个 loop 实例可以被复用）。
        self._stop_code = None
        self._stop_reason = None
        self._stop_by = None

        phase = LoopPhase.PLANNING
        tool_calls_made = 0
        final_content: str | None = None

        while True:
            # 取消检查点：下一次模型调用之前
            if self.is_cancelled():
                phase = LoopPhase.STOPPED
                break

            if self._halted:
                phase = LoopPhase.STOPPED
                self._warn("护栏终止：同一工具反复失败，已终止本轮")
                await self._emit(
                    EventType.WARNING,
                    {"code": "guard_halt", "message": "同一工具反复失败，已终止本轮", "recoverable": True},
                )
                break

            if self._no_progress:
                # 有证据的暂停：这几次调用拿到的东西完全一样，继续只会重复。
                reason = self._no_progress
                self._no_progress = None
                self._warn(f"本轮提前暂停：{reason}")
                await self._emit(
                    EventType.WARNING,
                    {
                        "code": "no_progress",
                        "message": reason,
                        "recoverable": True,
                    },
                )
                if self.approvals is None:
                    phase = LoopPhase.STOPPED
                    self._stop_note = (
                        f"本轮暂停：{reason}。"
                        "继续下去只会重复同样的事情，需要换一个做法或给一个新的方向。"
                    )
                    self._stop_code = "no_progress"
                    self._stop_reason = reason
                    self._stop_by = "system"
                    break
                # 有审批通道就交给用户决定（与预算 / 护栏走同一条「继续/停止」通道）。
                # 这段时间以前完全不可见：模型只跑了 1.7 秒、turn 却 51.5 秒，
                # 差的那 50 秒就可能是「等人点确认」。
                with self._phase("approval_wait", "no_progress"):
                    decision = await self.approvals.request(
                        "continue",
                        {
                            "reason": "no_progress",
                            "message": reason,
                            "used_iterations": self.budget.used_iterations,
                            "max_iterations": self.budget.max_iterations,
                            "used_tokens": self.budget.used_tokens,
                            "token_budget": self.budget.token_budget,
                        },
                    )
                if decision.decision == "approved":
                    # 用户让继续：重新开始计数，否则下一次同样的重复立刻又触发。
                    self.progress.reset()
                    self._warn(f"无进展暂停：用户选择继续（{reason}）")
                    continue
                phase = LoopPhase.STOPPED
                self._stop_note = (
                    f"本轮暂停：{reason}，按你的选择停下来了。"
                    "需要换一个做法或给一个新的方向再继续。"
                )
                self._stop_code = "no_progress"
                self._stop_reason = reason
                self._stop_by = "user"
                break

            if self.budget.exhausted and not self.force_continue:
                reason = (
                    f"迭代次数达到上限（{self.budget.used_iterations}/{self.budget.max_iterations}）"
                    if self.budget.used_iterations >= self.budget.max_iterations
                    else f"输出 token 预算耗尽（{self.budget.used_tokens}/{self.budget.token_budget}）"
                )
                if self.approvals is None:
                    # 无审批服务：旧的静默停止行为
                    phase = LoopPhase.STOPPED
                    self._stop_note = f"本轮没有产生回答：{reason}，已停止。"
                    self._stop_code = "budget"
                    self._stop_reason = reason
                    self._stop_by = "system"
                    self._warn(f"本轮提前结束：{reason}")
                    await self._emit(
                        EventType.WARNING,
                        {"code": "budget_exhausted", "message": reason, "recoverable": True},
                    )
                    break
                # 有审批服务：挂起等用户决定「继续/停止」
                # 「继续/停止」这条通道服务两种暂停（无进展 / 预算耗尽）。
                # 只给 used/max 时界面只能说「已达迭代上限」，而 token 预算耗尽会因此显示一句
                # 与事实不符的话 —— 用户拿它做决定。这里把**准确原因**一起交出去，
                # 界面不再替后端猜（对应 core/loop.py 上面 no_progress 分支的同一口径）。
                budget_kind = (
                    "iterations"
                    if self.budget.used_iterations >= self.budget.max_iterations
                    else "tokens"
                )
                with self._phase("approval_wait", "budget"):
                    decision = await self.approvals.request(
                        "continue",
                        {
                            "reason": "budget",
                            "budget_kind": budget_kind,
                            "message": reason,
                            "used_iterations": self.budget.used_iterations,
                            "max_iterations": self.budget.max_iterations,
                            "used_tokens": self.budget.used_tokens,
                            "token_budget": self.budget.token_budget,
                        },
                    )
                if decision.decision == "approved":
                    self.budget.raise_limits(
                        self.continue_batch_iterations, self.continue_batch_tokens
                    )
                    continue
                phase = LoopPhase.STOPPED
                self._stop_note = f"本轮没有产生回答：{reason}，按你的选择停下来了。"
                self._stop_code = "budget"
                self._stop_reason = reason
                self._stop_by = "user"
                self._warn("预算耗尽，用户选择停止")
                break

            if self._notices:
                messages.append(
                    ChatMessage(role="system", content="\n".join(self._notices))
                )
                self._notices.clear()

            # ── 模型调用（带全套工具）：角色由**正文开头的声明**决定（第五轮契约 §1.1）
            # —— 带工具同样可以给出正式回答；不再「工作调用正文一律进过程区 + 结束
            # 无 tool_calls 就再发一次 tools=[] 重写一遍」（那会让同一段答案出现两份）。
            with self._phase("tool_routing"):
                work_tools = self._routed_tools(messages)
            completion = await self._plan(messages, tools=work_tools, hint=self._call_hint())
            if completion is None:
                # 这一轮在等待模型时被用户停掉：请求已经中断，没有结果可用。
                phase = LoopPhase.STOPPED
                break
            self._account_usage(completion)
            self.budget.consume_iteration()

            # 取消检查点：模型调用之后（无法物理中断已发出的 HTTP 请求，
            # 但返回结果必须被丢弃，绝不重新激活本 turn）
            if self.is_cancelled():
                # 已经不打算执行工具：工作阶段的已确认文字立刻交出去（没有阶段可归），
                # 宁可把它放在「整轮」里，也不能丢掉用户已经看到的模型说明。
                await self._flush_active_stream(None)
                phase = LoopPhase.STOPPED
                break

            # 真流式路径已经在收到增量时边收边发。这里只处理「这一次调用的正文还
            # 没有任何人负责发」的情况（text 兼容档 / 未实现的降级），一次性给出
            # {streaming: false} —— 不假装流式。
            if not self._stream_emitted:
                await self._emit_one_shot_assistant(completion)

            if self._call_undeclared:
                # 协议未遵守（模型没有声明回答角色）：未声明的正文按一次性回答交付，
                # 如实记录这条降级路径，不冒充流式（契约 §1.2/§1.3）。
                self._note_undeclared_answer()
            if self._call_buffer_outcome is not None:
                # 交付不完整（硬上限截断 / 暂存创建·写入·读取故障）：如实报告 ——
                # 可见 WARNING + 事实进交付内容（读取故障绝不说成「超过上限」）
                await self._note_incomplete_answer(self._call_buffer_outcome)

            answer_text = self._deliverable_answer_text()
            if self._call_role == "answer":
                # 模型用正文声明了「这是最终回答」：它就是正式回答（带工具也一样）。
                if completion.tool_calls:
                    # 声明之后的迟到工具调用：不执行 + 可见警告，按已声明回答收尾。
                    await self._warn_late_tool_calls(completion.tool_calls)
                if answer_text.strip():
                    phase = LoopPhase.DONE
                    final_content = answer_text
                    break
                # 声明了回答却一个字都没有 → 与「整轮没有回答内容」同路（兜底一次）
                phase = LoopPhase.DONE
                final_content = await self._maybe_fallback(messages, completion)
                break

            if not completion.tool_calls:
                # 没有请求工具 → 这一段正文（如果有）就是本轮的回答：声明过 → 已经
                # 实时进回答区；未声明 → 已经一次性交付（降级路径）。
                if answer_text.strip():
                    phase = LoopPhase.DONE
                    final_content = answer_text
                    break
                # 整轮**完全没有回答内容** → 唯一允许的额外调用：兜底要一次最终回答
                phase = LoopPhase.DONE
                final_content = await self._maybe_fallback(messages, completion)
                break

            # TOOL_EXEC + OBSERVING（执行走 registry 管线，事件由监听器转发；
            # 并发安全工具分组并行，其余串行，结果按原始顺序回填）
            phase = LoopPhase.TOOL_EXEC
            messages.append(completion.message)
            # 取消检查点：不启动新的工具
            if self.is_cancelled():
                # 已经不打算执行工具：延后的过程区文字立刻交出去（没有阶段可归），
                # 宁可把它放在「整轮」里，也不能丢掉用户已经看到的模型说明。
                await self._flush_active_stream(None)
                phase = LoopPhase.STOPPED
                break
            results = await self._dispatch_tool_calls(completion.tool_calls)
            # 取消检查点：工具返回之后不再进入推理循环
            if self.is_cancelled():
                phase = LoopPhase.STOPPED
                break
            for call in completion.tool_calls:
                tool_calls_made += 1
                result = results[call.id]
                if not result.ok:
                    self._warn(f"tool {call.name} failed: {result.error}")
                # 台账记的是**权威事实**：这一次调用最终是成功还是失败，以及工具
                # 上报的「操作之后」的开发任务状态（见 core/turn_facts.py）。
                self._record_turn_facts(call, result)
                messages.append(
                    # 统一反馈：失败也要把类别/原因/是否可重试交给模型，
                    # 不能只回 content —— 失败且 content 为空时模型会收到空正文。
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        content=tool_feedback.message(
                            result, tool_name=call.name, call_id=call.id
                        ),
                    )
                )
            phase = LoopPhase.OBSERVING

        cancelled = self.is_cancelled()
        if self._request_aborted:
            # 请求被用户中断：这一轮的语义就是「被取消」，不能落成「模型没说话」。
            cancelled = True
        if cancelled:
            phase = LoopPhase.STOPPED
        # 结束事实（plan §1.2）：取消是本轮已知的系统事实；其它情况用循环记下的
        # 停止原因（护栏 / 预算 / 无进展）。正常跑完就是 "none"。
        if cancelled:
            stop_code, stop_reason, stop_by = "user_stopped", None, "user"
        else:
            stop_code = self._stop_code or "none"
            stop_reason = self._stop_reason
            stop_by = self._stop_by
        if not cancelled and not (final_content or "").strip():
            # 静默失败收口：护栏终止 / 预算停止 / 模型什么都没说，都必须留下人话。
            # 取消是用户自己的动作，界面已有「已停止」状态行，这里不补文本。
            # 停止说明可能引用工具 / 厂商回显，过一遍脱敏（契约 C3：错误与最终输出）。
            final_content = _redact_published(
                self._stop_note, holdback=False
            ) or "本轮没有产生回答，也没有给出原因。"
        final_annotation: str | None = None
        if not cancelled:
            # 后端事实校正（审计 F11）：系统核对注释走独立字段，绝不拼进 final_content。
            # final_content 保持纯正文；前端按 annotation 渲染「系统核对」那一行，
            # 不会因为「全文不等」再补一条包含完整正文的重复回答。
            final_annotation = self.turn_facts.annotation()
        usage = {
            "iterations": self.budget.used_iterations,
            # 向后兼容字段：`tokens` 一直是「输出 token」（而不是总量）
            "tokens": self.budget.used_tokens,
            "tool_calls": tool_calls_made,
            "input_tokens": self._usage_input,
            "output_tokens": self._usage_output,
            "total_tokens": self._usage_total,
        }
        await self._emit(EventType.USAGE, usage)
        # 迟到的系统通知：这一轮已经不会再 planning 了，留在手上的必须交还给上层
        # （否则「子任务完成了」这句话会被静默丢掉，用户永远等不到结果）。
        unread_notices = list(self._notices)
        self._notices.clear()
        return TurnResult(
            final_content=final_content,
            phase=phase,
            iterations_used=self.budget.used_iterations,
            tokens_used=self.budget.used_tokens,
            tool_calls_made=tool_calls_made,
            warnings=list(self._warnings),
            cancelled=cancelled,
            unread_notices=unread_notices,
            verification=self.turn_facts.declaration,
            stop_reason_code=stop_code,
            stop_reason=stop_reason,
            stopped_by=stop_by,
            final_annotation=final_annotation,
        )

    # -- steps ------------------------------------------------------------

    def _routed_tools(self, messages: list[ChatMessage]) -> list[ToolSpec]:
        """按当前查询上下文路由工具集（原 _plan 的前半段，行为不变）。"""
        tools = self.registry.specs()
        if self.tool_selector is None:
            return tools
        # route tools by the current query context (last user + tool message)
        query_parts: list[str] = []
        for m in reversed(messages):
            if m.role == "user" and m.content:
                query_parts.append(m.content)
                break
        for m in reversed(messages):
            if m.role == "tool" and m.content:
                query_parts.append(m.content[:200])
                break
        query = "\n".join(reversed(query_parts))[:500]
        try:
            return self.tool_selector(query)
        except Exception:  # noqa: BLE001 - routing must never break planning
            logger.warning("tool routing failed; falling back to full set", exc_info=True)
            return tools

    # -- 流式（plan §2.1）--------------------------------------------------

    def _current_stage_id(self) -> str | None:
        """当前阶段 id（服务层注入）；拿不到就 None，绝不猜一个。"""
        provider = self.stage_id_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # noqa: BLE001 - 阶段标识失败不能影响工具执行
            return None

    async def _emit_assistant(self, payload: dict) -> None:
        await self._emit(EventType.ASSISTANT, payload)

    def _absorb_stream_role(self, stream: _AssistantStream) -> None:
        """把这条流的内容角色与交付正文收进本次调用的状态（_run 据此收口）。"""
        self._call_role = stream.role
        self._call_answer_text = stream.answer_text
        self._call_undeclared = stream.undeclared_answer_used
        self._call_buffer_outcome = stream.buffer_outcome

    async def _note_model_termination(self, completion: Completion) -> None:
        """把这次模型调用的结束事实记成本轮的停止原因（冻结契约 C2）。

        * stream_incomplete（adapter 按协议判定：无 finish_reason / 无 message_stop）
          → incomplete_stream；
        * finish_reason = length / max_tokens → length_limit；
        * content_filter / refusal → content_filter；
        * 其余（stop / tool_calls / end_turn / tool_use / 旧路径的 None）不设停止原因；
          一次后续的正常结束会把**先前**的临时结束码清掉（兜底回答成功 = 已恢复）。
        """
        reason = (completion.finish_reason or "").strip().lower()
        if getattr(completion, "stream_incomplete", False):
            code = "incomplete_stream"
            text = "模型流在给出结束标记之前就结束了，回答可能不完整。"
        elif reason in ("length", "max_tokens"):
            code = "length_limit"
            text = "模型因长度上限提前结束，回答可能被截断。"
        elif reason in ("content_filter", "refusal"):
            code = "content_filter"
            text = "模型因内容策略中断了这次生成。"
        else:
            if self._stop_code in _TERMINATION_CODES:
                self._stop_code = None
                self._stop_reason = None
                self._stop_by = None
            return
        self._stop_code = code
        self._stop_reason = text
        self._stop_by = "system"
        if self._termination_warned:
            return
        self._termination_warned = True
        self._warn(text)
        await self._emit(
            EventType.WARNING,
            {"code": code, "message": text, "recoverable": True},
        )

    def _call_hint(self) -> str | None:
        """每次调用附带的系统提示：内容角色协议（第五轮契约 §1.1）。

        text 兼容档已经把它拼进自己的 system prompt（adapter.protocol_in_prompt），
        这里就不再重复一遍；native / anthropic 本来不自己拼 system prompt，由这里
        作为**最后一条 system 消息**发出去 —— 三档看到的是同一份措辞（同一个常量）。
        """
        if getattr(self.adapter, "protocol_in_prompt", False):
            return None
        return CONTENT_ROLE_PROTOCOL

    async def _emit_one_shot_assistant(self, completion: Completion) -> None:
        """一次性正文事件（不支持流式的路径**不假装流式**）。

        角色同样只由**正文开头的声明**决定（第五轮契约 §1.1），这里对整段解析：

        * 声明在开头 → 正式回答：interim=false、streaming=false（声明不展示）；
        * 没有声明、但有工具调用 → 工作轮：正文是进度说明（interim=true，带阶段归属）；
          这一批工具带了 `_qio` 叙事时不再推 interim（同一阶段只保留一种过程表达）；
        * 没有声明、也没有工具调用 → **未声明的正式回答**：一次性交付到正式回答区
          （interim=false、streaming=false，降级路径，如实记录）；
        * text 兼容档的 content 是 JSON 协议块，任何情况下都不当作正文展示。
        """
        content = completion.message.content or ""
        calls = completion.tool_calls or []
        declared, body = _split_declared_answer(content)
        if declared:
            self._call_role = "answer"
            # 对外发布与最终交付的正文统一脱敏（契约 C3）；整段已知，无需扣留尾部。
            body = _redact_published(body, holdback=False)
            self._call_answer_text = body
            if not body.strip():
                return
            await self._emit_assistant(
                self._one_shot_payload(body, interim=False, evidence="declared_answer")
            )
            return
        if calls:
            self._call_role = "interim"
            if not content.strip() or self.adapter.mode != AdapterMode.NATIVE:
                return  # text 档的 content 是 JSON 协议块：不当作正文展示
            batch_has_narrative = any(
                parse_narrative(getattr(c, "narrative", None)) is not None for c in calls
            )
            if batch_has_narrative:
                return
            await self._emit_assistant(
                self._one_shot_payload(
                    _redact_published(content, holdback=False),
                    interim=True,
                    evidence=None,
                    calls=calls,
                )
            )
            return
        # 没有声明、也没有工具调用：未声明的正文就是正式回答（降级路径，一次性交付）
        self._call_role = "answer"
        self._call_answer_text = _redact_published(content, holdback=False)
        self._call_undeclared = bool(content.strip())
        if not content.strip():
            return
        await self._emit_assistant(
            self._one_shot_payload(
                self._call_answer_text, interim=False, evidence="undeclared_answer"
            )
        )

    def _one_shot_payload(
        self,
        content: str,
        *,
        interim: bool,
        evidence: str | None,
        calls: list | None = None,
    ) -> dict:
        """整段交付的 ASSISTANT 载荷（与流式路径同一套字段语义）。"""
        calls = calls or []
        return {
            "content": content,
            "interim": interim,
            "streaming": False,
            "delta_id": self._call_delta_id or "",
            "seq": 1,
            # 工作调用的正文与这一批工具同属一个阶段（plan §1.1）；没有工具调用
            # 的工作说明归入「整轮」，正式回答不属于任何阶段。
            "stage_id": self._current_stage_id() if interim and calls else None,
            "call_ids": [c.id for c in calls] if interim and calls else [],
            "role_evidence": evidence,
        }

    async def _maybe_fallback(
        self, messages: list[ChatMessage], completion: Completion
    ) -> str | None:
        """整轮完全没有回答内容 → 最多补一次 tools=[] 的调用要求最终回答。

        触发条件（契约 §1.1 第 6 条，唯一允许的额外回答调用，每轮最多一次）：
        走到这里时本轮既没有声明过的回答、也没有可交付的正文 —— 模型只调用过工具、
        或者一个字都没说。合规模型下直接问答只需 1 次调用；这里只补一次。
        """
        if self._fallback_used or self.is_cancelled():
            return None
        if self._call_buffer_outcome is not None and not self._call_buffer_outcome.complete:
            # 已经生成过内容（只是没能完整保存/读取）：**不得**再让模型重写一遍
            # 来「找回原文」（契约 §1.4：不重新调用模型伪造找回原文）。
            return None
        self._fallback_used = True
        # 把模型已经说过的话交给它自己（空内容不追加空消息），否则兜底调用会
        # 看不到自己刚写过什么。
        if (completion.message.content or "").strip():
            messages.append(completion.message)
        return await self._fallback_answer_call(messages)

    async def _warn_late_tool_calls(self, calls: list) -> None:
        """声明回答之后仍请求工具：不执行 + 可见警告（契约 §1.1 第 4 条）。

        绝不静默忽略真实工具调用：警告是**可见事件**（WARNING），本轮按已声明的
        回答收尾，工具一次都不执行。
        """
        names = ", ".join(str(getattr(c, "name", "?")) for c in calls)
        message = (
            f"模型在声明最终回答之后仍请求了工具（{names}）：本轮不执行这些调用，"
            "按已声明的回答收尾。"
        )
        self._warn(message)
        if self._late_tool_warned:
            return
        self._late_tool_warned = True
        await self._emit(
            EventType.WARNING,
            {"code": "answer_declared_with_tool_calls", "message": message, "recoverable": True},
        )

    def _note_undeclared_answer(self) -> None:
        """协议未遵守：未声明的正文按一次性回答交付（降级路径，不冒充流式）。

        如实记录的方式是**事件本身的字段**（`role_evidence="undeclared_answer"` +
        `streaming=false`），外加一条服务端日志。**不**发 WARNING 事件、也不进
        TurnResult.warnings：契约 §1.1 只对「声明之后的迟到工具调用」要求可见警告；
        未声明是模型侧尚未遵守新协议，每轮一条可见警告会变成噪声，而
        「干净的一轮没有警告」是这个项目已有的不变量（trace 用它判断异常）。
        """
        if self._undeclared_warned:
            return
        self._undeclared_warned = True
        logger.info(
            "answer role undeclared (turn=%s): 正文按一次性回答交付，未实时生成",
            self.turn_id,
        )

    # 交付不完整时给用户看的一句话（按 kind 区分：读取故障**绝不**说成「超过上限」）
    _INCOMPLETE_TITLES = {
        "limit": "这段正文太长，已按保存上限如实截断",
        "spill_create": "暂存文件创建失败，这段回答没能完整保存",
        "spill_write": "暂存写入失败，这段回答没能完整保存",
        "spill_read": "暂存读取失败，这段回答没能完整读取",
    }

    def _incomplete_title(self, outcome: BufferOutcome) -> str:
        return self._INCOMPLETE_TITLES.get(outcome.kind, "这段回答没能完整保存")

    def _incomplete_detail(self, outcome: BufferOutcome) -> str:
        """用户可见说明里的**三个字节事实**（名称必须真实，不得混用）。

        契约 §1.4：原生成字节数 / 成功保存字节数 / 实际交付字节数分开报 ——
        绝不把「成功保存量」说成「完整生成量」（硬上限 / 写失败时两者本来就不同）。
        """
        return (
            f"；原生成 {outcome.generated_bytes} 字节，"
            f"成功保存 {outcome.saved_bytes} 字节，"
            f"实际交付 {outcome.delivered_bytes} 字节"
        )

    def _deliverable_answer_text(self) -> str:
        """本次调用交付到正式回答区的正文 = **模型已生成、且已确认可交付**的那部分。

        交付内容**原样**返回：不追加说明文字、不改写模型的字（交付字节数因此如实可核）。
        交付不完整这个事实按契约 §1.4 的两条通道传递：①可见 WARNING 事件
        ②轮次结果/警告（见 _note_incomplete_answer），**不靠改正文来表达**。
        """
        return self._call_answer_text or ""

    async def _note_incomplete_answer(self, outcome: BufferOutcome) -> None:
        """交付不完整：发**可见** WARNING + 轮次警告（只写日志不算交付，契约 §1.4）。"""
        if self._truncation_warned:
            return
        self._truncation_warned = True
        title = self._incomplete_title(outcome)
        message = f"{title}：{outcome.reason}" if outcome.reason else title
        message = f"{message}{self._incomplete_detail(outcome)}。"
        message = _redact_published(message, holdback=False)
        self._warn(message)
        await self._emit(
            EventType.WARNING,
            {
                # limit（资源上限）与 spill_*（存储故障）用不同的 code：
                # 读取故障不得被描述成「正文超过上限」。
                "code": "answer_truncated" if outcome.kind == "limit" else "answer_incomplete",
                "kind": outcome.kind,
                "message": message,
                "recoverable": False,
                # 三个字节事实可机读（与 message 里报的数一致，名称不混用）
                "generated_bytes": outcome.generated_bytes,
                "saved_bytes": outcome.saved_bytes,
                "delivered_bytes": outcome.delivered_bytes,
            },
        )

    async def _fallback_answer_call(self, messages: list[ChatMessage]) -> str | None:
        """兜底调用（第五轮契约 §1.1 第 6 条）：tools=[]，**每轮最多一次**。

        触发条件见 _maybe_fallback：整轮结束时完全没有回答内容（没有声明、也没有
        可交付的正文）。这是唯一允许的额外回答调用 —— 合规模型下直接问答只需
        1 次调用，不再「每轮固定 +1」。这次调用的正文仍按同一套内容角色协议解析：
        声明 → 实时进回答区；未声明 → 一次性交付（降级路径，不冒充流式）。
        每次调用都计入迭代与用量，不隐藏。
        """
        completion = await self._plan(messages, tools=[], hint=ANSWER_FALLBACK_HINT)
        if completion is None:
            return None  # 被取消：这次调用没有结果
        self._account_usage(completion)
        self.budget.consume_iteration()
        if not self._stream_emitted:
            await self._emit_one_shot_assistant(completion)
        if self._call_undeclared:
            self._note_undeclared_answer()
        if self._call_buffer_outcome is not None:
            await self._note_incomplete_answer(self._call_buffer_outcome)
        if completion.tool_calls:
            # 这次调用没有提供任何工具定义：即使模型仍返回工具调用也不执行
            # （它没有被授予这些工具），只把正文当正式回答，并如实记一条可见警告。
            await self._warn_late_tool_calls(completion.tool_calls)
        return self._deliverable_answer_text() or None

    async def _plan(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        *,
        hint: str | None = None,
    ) -> Completion | None:
        # tools=None 时才做工具路由（含 query 嵌入）：它发生在模型计时**之前**，
        # 既不算模型耗时，也没有任何分区 —— 慢的路由曾经是完全不可见的等待。
        # 回答调用传 tools=[]：这次调用**不带工具**，不做路由。
        if tools is None:
            with self._phase("tool_routing"):
                tools = self._routed_tools(messages)
        # 这次调用**是不是被要求给出回答**（tools=[] 的兜底调用）。角色判据不在这里
        # —— 第五轮契约 §1.1：角色只由正文开头的声明决定（_AssistantStream 判定）。
        self._answer_expected = not tools
        # 每次调用都重置内容角色状态：上一次调用的角色绝不泄漏到这一次。
        self._call_role = None
        self._call_answer_text = ""
        self._call_undeclared = False
        self._call_buffer_outcome = None
        # 系统提示只发给这一次调用（不写回对话记录）：内容角色协议 / 兜底要求。
        request_messages = messages
        if hint:
            request_messages = [*messages, ChatMessage(role="system", content=hint)]
        import time as _time

        self._model_seq += 1
        # 一次模型调用 = 一条流式消息：稳定标识 dl_<turn8>_<call_seq>（plan §2.1）。
        self._call_delta_id = f"dl_{short_turn_id(self.turn_id)}_{self._model_seq}"
        self._stream_emitted = False
        _t0 = _time.perf_counter()
        try:
            with self._phase("model_wait", f"call#{self._model_seq}"):
                completion = await self._completion_step(request_messages, tools)
            if completion is None:
                # 被用户取消：这次调用没有结果，也不再记一条「假成功」的 trace
                return None
            if self.trace is not None:
                usage = completion.usage or {}
                self.trace.model_call(
                    seq=self._model_seq,
                    adapter_mode=str(getattr(self.adapter, "mode", "")),
                    model=getattr(self.adapter, "model", None),
                    input_tokens=self._input_tokens_of(completion),
                    output_tokens=self._output_tokens_of(completion),
                    latency_ms=int((_time.perf_counter() - _t0) * 1000),
                    tool_calls=len(completion.tool_calls or []),
                )
            # 这次调用的结束事实（冻结契约 C2）：记成本轮停止原因，供 TURN_END 用。
            await self._note_model_termination(completion)
            return completion
        except Exception as exc:  # adapter-level failure ends the turn
            if self.trace is not None:
                self.trace.model_call(
                    seq=self._model_seq,
                    adapter_mode=str(getattr(self.adapter, "mode", "")),
                    model=getattr(self.adapter, "model", None),
                    latency_ms=int((_time.perf_counter() - _t0) * 1000),
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )
            self._warn(f"planning failed: {exc}")
            from agent.trace.redact import redact_text

            await self._emit(
                EventType.ERROR,
                {
                    "code": "planning_failed",
                    # 错误信息同样是对外可观输出：过脱敏（契约 C3）
                    "message": redact_text(str(exc))[:200],
                    "recoverable": False,
                },
            )
            # **原样上抛**（Lead 裁决 2026-10-06）：适配器归一化过的异常自己带着
            # 「这是厂商/传输路径失败」的事实；没有归一化的异常就是 QIO 内部的意外
            # 故障，绝不能包装成 ProviderError —— provider_error 的含义是「厂商故障」，
            # 把内部 bug 报成厂商故障是对用户撒谎。
            # 上层（core/turn.py::_failure_facts）按类名如实分类：
            # ProviderError 家族 → provider_error；其它 → internal_error（reason 带类名）。
            raise

    async def _await_completion(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> Completion | None:
        """跑一次模型调用；被用户取消时**中断这次请求**并返回 None。

        以前取消只是一个检查点：请求已经发出去，就只能等它回来再把结果丢掉 ——
        provider 慢的时候要白白等几十秒，而 turn 队列是单飞的，排在后面的消息
        也跟着一起等。这里把请求放进自己的 task，与取消事件竞速；取消时立刻
        取消它，底层 HTTP 连接随之中断。

        诚实边界：断开的是**客户端的等待**，服务端是否立刻停止生成由供应商决定；
        这条改动保证的是「不再占用等待时间、不再堵住下一条消息」。
        """
        if self._cancel_event.is_set():
            return None
        request = asyncio.ensure_future(self.adapter.complete(messages, tools))
        waiter = asyncio.ensure_future(self._cancel_event.wait())
        try:
            await asyncio.wait({request, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if request.done() and not request.cancelled():
            # 正常返回（异常由 _plan 的 except 分支如实处理）
            return request.result()
        request.cancel()
        try:
            await request
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 结果已被取消，丢弃
            pass
        self._request_aborted = True
        return None

    async def _completion_step(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
    ) -> Completion | None:
        """一次模型调用：能真流式就真流式，否则整段降级（plan §2.2）。

        降级发生在「声明支持流式、实际用不了」的两种证据上：adapter 抛
        NotImplementedError（没实现），或抛 UnsupportedCapability（端点不接受
        stream / 客户端不返回异步流）。前提是**还没有任何正文被发出去** ——
        否则整段重来会重复展示，只能如实失败。降级后由 _run 发一条
        {streaming:false}，前端如实提示「该模型路径不支持实时生成」。
        """
        if not getattr(self.adapter, "supports_stream", False):
            return await self._await_completion(messages, tools)
        try:
            return await self._await_stream(messages, tools)
        except (NotImplementedError, UnsupportedCapability) as exc:
            if self._stream_emitted:
                # 已经透出正文就不能整段重来：那会重复展示同一段文字。
                # 归类成「能力不可用」而不是普通运行时错误：这是**供应商路径**的
                # 事实（声明支持流式、实际用不了），TURN_END 的原因代码据此归到
                # provider_error，前端不会把它显示成内部故障。
                raise UnsupportedCapability(
                    "模型路径不支持实时生成，但已经显示了部分正文"
                ) from exc
            # 声明支持流式、实际用不了（未实现 / 端点不接受 stream / 端点忽略
            # stream=true 回了整段 JSON）：整段降级，由 _run 发一条
            # {streaming:false}，**不假装流式**。
            await self._note_stream_unsupported()
            return await self._await_completion(messages, tools)

    async def _note_stream_unsupported(self) -> None:
        """如实告知：这条模型路径这次没有真流式（整段返回）。一轮只广播一次。

        TurnResult.warnings 不外发，所以按既有 WARNING 事件口径广播 —— 前端据此
        显示「该模型路径不支持实时生成」，而不是让用户以为字是慢慢打出来的。
        """
        if self._stream_degraded_warned:
            return
        self._stream_degraded_warned = True
        self._warn("这条模型路径不支持实时生成，本次整段返回")
        await self._emit(
            EventType.WARNING,
            {
                "code": "streaming_unsupported",
                "message": "这条模型路径不支持实时生成，本次整段返回",
                "recoverable": True,
            },
        )

    async def _await_stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
    ) -> Completion | None:
        """真流式模型调用：边收边发（plan §2.1）。

        返回 None = 被用户取消（已确认文本保留在界面上，状态由 TURN_END 给出）。
        adapter 抛的异常原样上抛给 _plan 的错误路径 —— 取消/失败/断线都不吞。

        与 complete() 的差异（有意为之）：这里**没有**「工具参数 JSON 解析失败 →
        把错误喂回模型重试」这条恢复路径 —— 流可能已经展示了正文，整段重来会
        重复展示。碎片攒不成合法 JSON 时这一轮如实失败（不执行任何未完成的调用），
        见 plan §2.1 第 4 条；complete() 路径的重试逻辑保持不变。
        """
        if self._cancel_event.is_set():
            return None  # 还没发出请求就被取消：不产生任何新的模型调用
        delta_id = self._call_delta_id or f"dl_{short_turn_id(self.turn_id)}_1"
        stream = _AssistantStream(
            self._emit_assistant,
            delta_id=delta_id,
            answer_expected=self._answer_expected,
            spill_dir=self._spill_dir,
            memory_limit=self._undeclared_memory_limit,
            spill_limit=self._undeclared_spill_limit,
        )
        # 工具轮的正文要等阶段就位后再补 stage_id / call_ids（见 _dispatch_tool_calls）。
        self._active_stream = stream
        queue: asyncio.Queue = asyncio.Queue()

        async def _pump() -> None:
            try:
                async for delta in self.adapter.stream(messages, tools):
                    await queue.put(delta)
            except asyncio.CancelledError:
                raise
            except NotImplementedError as exc:
                await queue.put(_UnsupportedStream(exc))
            except Exception as exc:  # noqa: BLE001 - 交给消费侧如实处理
                await queue.put(_StreamError(exc))
            finally:
                await queue.put(_STREAM_END)

        pump = asyncio.ensure_future(_pump())
        cancel_waiter = asyncio.ensure_future(self._cancel_event.wait())
        getter: asyncio.Future | None = None
        completion: Completion | None = None
        failure: Exception | None = None
        unsupported: Exception | None = None
        aborted = False
        try:
            while True:
                if getter is None:
                    # 取事件用同一个 future 反复等：超时（守卫/节奏）不会丢掉已经
                    # 到达的那一段 —— asyncio.Queue.get 被取消时可能吞掉一条增量。
                    getter = asyncio.ensure_future(queue.get())
                deadline = stream.next_deadline()
                timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
                done, _ = await asyncio.wait(
                    {getter, cancel_waiter},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if getter in done:
                    item = getter.result()
                    getter = None
                    if item is _STREAM_END:
                        break
                    if isinstance(item, _StreamError):
                        failure = item.error
                        break
                    if isinstance(item, _UnsupportedStream):
                        unsupported = item.error
                        break
                    if item.kind == STREAM_TEXT:
                        await stream.note_text(item.text)
                    elif item.kind == STREAM_TOOL_CALL:
                        # 工具参数碎片不在这里处理：只有 adapter 组装完成的合法
                        # JSON 才允许执行（见 StreamDelta 的契约）。
                        await stream.note_tool_call()
                    elif item.kind == STREAM_DONE:
                        completion = item.completion
                    continue
                if cancel_waiter in done:
                    aborted = True
                    break
                await stream.on_deadline()
        finally:
            cancel_waiter.cancel()
            if getter is not None:
                getter.cancel()
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 中断后不再需要它
                pass

        if aborted:
            self._request_aborted = True
            await stream.finish(None)
            self._stream_emitted = stream.emitted
            self._absorb_stream_role(stream)
            return None
        if unsupported is not None:
            await stream.finish(None)
            self._stream_emitted = stream.emitted
            self._absorb_stream_role(stream)
            # 交给 _completion_step 定夺：没透出正文就整段降级，透出了就如实失败。
            raise unsupported
        if failure is not None:
            await stream.finish(None)
            self._stream_emitted = stream.emitted
            self._absorb_stream_role(stream)
            raise failure
        if completion is None:
            await stream.finish(None)
            self._stream_emitted = stream.emitted
            self._absorb_stream_role(stream)
            raise RuntimeError("模型流在给出完整结果之前就结束了")
        # 收尾：角色由正文声明在流式过程中判定（契约 §1.1）；有工具调用时过程区
        # 文字留在原地，等 flush_interim 补 stage_id / call_ids。
        await stream.finish(completion)
        # 收尾之后才算：finish 可能刚刚把正文一次性交付（整段返回 / 未声明的路径），
        # 这时上层**不能**再补发第二条（同一段字会出现两次）。
        self._stream_emitted = stream.emitted
        self._absorb_stream_role(stream)
        if not stream.saw_delta and (completion.message.content or completion.tool_calls):
            # 一个增量都没收到、却拿到了完整结果：这条服务是整段回的
            # （忽略 stream=true，application/json）。如实告知，不假装流式；
            # _run 会因此走一次性 {streaming:false} 交付。
            await self._note_stream_unsupported()
        return completion

    def _tokens_of(self, completion: Completion) -> int:
        """该次模型调用的**输出** token 数。

        只认统一语义里的 output_tokens：以前回退到 total_tokens 会把输入也算进去，
        导致 Anthropic（只给 input/output）被过早限流。
        """
        return self._output_tokens_of(completion)

    @staticmethod
    def _input_tokens_of(completion: Completion) -> int:
        usage = completion.usage
        return int(usage.input_tokens) if usage is not None else 0

    @staticmethod
    def _output_tokens_of(completion: Completion) -> int:
        usage = completion.usage
        return int(usage.output_tokens) if usage is not None else 0

    def _account_usage(self, completion: Completion) -> None:
        """累计本轮用量：输出闸与 UI/Trace 统计共用同一份归一化数据。"""
        usage = completion.usage
        if usage is not None:
            self._usage_input += int(usage.input_tokens)
            self._usage_output += int(usage.output_tokens)
            self._usage_total += int(usage.total_tokens)
        self.budget.consume_output_tokens(self._tokens_of(completion))
        if self.usage_sink is not None:
            # 归因给「这把钥匙」：进 / 出分开报，由调用方决定记到哪条凭据上。
            input_tokens = self._input_tokens_of(completion)
            output_tokens = self._output_tokens_of(completion)
            if input_tokens or output_tokens:
                try:
                    self.usage_sink(input_tokens, output_tokens)
                except Exception:  # noqa: BLE001 - 记账失败不得打断回答
                    logger.warning("usage sink failed", exc_info=True)
