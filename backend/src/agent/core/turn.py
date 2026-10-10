"""Turn runtime boundary: per-turn state + single-flight scheduler.

Rule of thumb:
    process-level services live on AppContext / RuntimeServices;
    per-turn state lives on TurnContext.

TurnManager guarantees `active_main_turn <= 1` and keeps the rest in a FIFO
queue, so overlapping HTTP requests cannot clobber each other's active loop,
pipeline listeners, notices or cancellation target.

TurnManager is also the **single source of truth for the turn lifecycle**:

    accepted ──▶ queued ──▶ running ──┬──▶ completed
          └──────────────────────────▶├──▶ failed
                                      ├──▶ cancelled
                                      ├──▶ unavailable
                                      └──▶ incomplete

契约（前端与后端共同遵守）：

* `accepted` 只表示「后端已经受理」——它**不是** active；
* 必须等待前一个主 turn 结束时是 `queued`；
* 只有真的开始执行才是 `running`，也只有 worker 在真正开跑时才会发
  `TURN_START`；
* 终态（`TERMINAL_STATUSES`）单向：进入之后不再变化，更不允许回到 `running`。

被取消的 queued turn 会变成 **tombstone**：它仍然躺在底层 `asyncio.Queue`
里（`asyncio.Queue` 不支持安全删除），但 worker 取到它时会直接跳过 ——
既不会被设成 `_active`，也不会发出 `TURN_START`。

**但 tombstone 仍然欠用户一条 `TURN_END`**（冻结契约 C1：一个 accepted turn 恰好
一次 TURN_END，含排队期被取消）。worker 既然跳过，这条结束事件就必须由 `cancel()`
自己调度发出（见 `_schedule_turn_end`）—— 否则前端拿不到结束事实，刷新后也恢复不了。

`incomplete` 是冻结契约（plan §C2）本轮扩进来的终态：**只在「不完整 EOF」时使用**
（native 无 finish_reason、anthropic 无 message_stop、仅 usage / 空分块、未结束的工具
调用，即 `reason_code == incomplete_stream`）。厂商合法终止 `length_limit` /
`content_filter` 仍然落 `completed`，只用 reason_code 区分 —— 「被截断」不等于
「协议没说结束」。

Every accepted turn gets exactly one `TURN_START` and exactly one `TURN_END`
(the latter in a `finally`), whatever happens inside the runner. Nested loops
(subagents, maintenance, tool development) must never emit turn events — they
are not turns, and a subagent's `TURN_END` used to end the user's turn.

TURN_END 除了终态与权威最终回答，还带**结束事实**（plan §1.2）：
`reason_code` / `reason`（已过 redact）/ `stopped_by` / `actions`。
全部来自系统事实；没有事实（含旧记录）就是 `none`，不伪造原因。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def short_turn_id(turn_id: str | None) -> str:
    """turn_id 的 8 位短标识：阶段 id / 流式 delta id 的前缀（plan §1.1、§2.1）。

    ``turn_ab12cd34ef56`` → ``ab12cd34``。拿不到 turn_id 时返回 ``local`` ——
    标识仍然稳定可达，但不伪造一个看起来像 turn 的假 id。
    """
    raw = str(turn_id or "").strip()
    if not raw:
        return "local"
    core = raw[5:] if raw.startswith("turn_") else raw
    return core[:8] or "local"


TurnRunner = Callable[["TurnContext"], Awaitable[None]]
EventEmitter = Callable[[str, dict], Awaitable[None]]

TURN_START = "TURN_START"
TURN_END = "TURN_END"

# 终态：进入其中之一后不再变化。
#
# incomplete（冻结契约 C2，本轮扩入）：协议没有给出结束标记就 EOF —— 回答可能不完整，
# 所以**不是** completed（前端不得显示「正常完成」），但它也不是 failed / cancelled。
# 语义边界由 `_completion_status` 钉死：只有 reason_code == incomplete_stream 才用，
# length_limit / content_filter 等厂商合法终止仍然是 completed。
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "unavailable", "incomplete")

#: 不完整结束的状态码 / 人话原因（core/loop.py 按协议判定后随 TurnResult 传来；
#: 这里是拿不到循环事实时的兜底，保证 TURN_END 绝不会退回「无原因的正常完成」）。
INCOMPLETE_STATUS = "incomplete"
INCOMPLETE_STREAM_CODE = "incomplete_stream"
INCOMPLETE_REASON = "模型流在给出结束标记之前就结束了，回答可能不完整。"

logger = logging.getLogger(__name__)

#: 预留顺序的**有界等待**上界（秒）：队首预留迟迟不放行（例如它的请求异常退出、
#: 没来得及 abandon）时，后面已经就绪的预留不会被永远挡住 —— 到点按就绪顺序放行并记警告。
ACTIVATION_ORDER_TIMEOUT = 60.0

#: 准备标识历史（已取消 / 已开始）的容量：重复取消要幂等回「已取消」，
#: 放行之后取消要如实回「已开始」，所以这两类事实都要在有界历史里留一段时间。
PREPARE_HISTORY = 64

#: 准备标识历史（已取消 / 已开始）的容量：重复取消要幂等回「已取消」，
#: 放行之后取消要如实回「已开始」，所以这两类事实都要在有界历史里留一段时间。
PREPARE_HISTORY = 64

# 适配器（供应商）异常的类名：见 adapters/errors.py 的归一化分类。
# 只按**类名**分类，不解析错误正文去猜厂商内容（与 services/verify.py 同一口径）。
# tests/test_turn_timing_facts.py 有一条防漂移断言：这张表必须覆盖 ProviderError 家族。
PROVIDER_ERROR_NAMES = frozenset(
    {
        "ProviderError",
        "AuthenticationError",
        "RateLimitError",
        "NetworkError",
        "UnsupportedCapability",
        "InvalidToolCall",
        "ProviderInternalError",
    }
)

# reason_code → 当前**确实可用**的操作（plan §1.2「只列当前确实可用的操作」）。
# * retry：前端用「重发这条用户消息」实现（走现有发送接口）；
# * resend：后端既有 /api/turns/{id}/resend（只认 journal 记成 interrupted 的行）；
# * 预算 / 无进展 / 护栏 / 缺凭据：重发同样的请求会再次停下，或真正的入口不在对话里
#   （设置 → 凭据）—— 一律不给按钮，原因写在 reason 里。
ACTIONS_BY_REASON: dict[str, tuple[str, ...]] = {
    "provider_error": ("retry",),
    "internal_error": ("retry",),
    "tool_failed": ("retry",),
    # 冻结契约 K3：user_stopped 的 journal 是 cancelled（不是 interrupted），
    # /api/turns/{id}/resend 只认 interrupted → 给 resend 等于给一个必然 409 的死按钮。
    # 用户停止后真正可用的恢复是 retry（前端用既有发送接口创建新 turn）。
    "user_stopped": ("retry",),
    "interrupted": ("resend",),
    # 流式结束语义（冻结契约 C2）：不完整结束 / 长度截断 / 内容策略中断
    # 都可以用「重发这条消息」恢复，所以给 retry。
    "incomplete_stream": ("retry",),
    "length_limit": ("retry",),
    "content_filter": ("retry",),
    "budget": (),
    "no_progress": (),
    "guard_halt": (),
    "credential_unavailable": (),
    "none": (),
}

# 没有「更具体原因」可用时的**系统事实**文案（不猜原因、不重复状态词）。
# 用户停止 / 程序中断同样必须有人话原因：plan §1.2 要求除 none 之外的每个
# reason_code 都能回答「为什么停下来」，否则前端只能显示一个状态词。
USER_STOPPED_REASON = "你按下了停止，这一轮不再继续生成。"
INTERRUPTED_REASON = "程序在这次回答结束前中断了。"
NO_CREDENTIAL_REASON = "还没有配置可用的模型凭据（设置 → 凭据），这一轮没有开始。"

# 人话原因的上限（plan §1.2：≤200 字）。
REASON_MAX_CHARS = 200


class TurnAcceptError(RuntimeError):
    """这一轮**没有被接受**。

    受理 = 队列台账已经持久化成功。台账写不进去（`JournalWriteError`）时
    不能返回一个「已接受」的内存 turn：那条消息在重启后没有任何痕迹，
    用户却以为发出去了。调用方（HTTP 层）必须据此明确拒绝
    （`POST /api/turns` → 503 + accepted=false）。
    """


def _journal_accept_failed(exc: BaseException) -> bool:
    """这个异常是不是「台账写失败」（storage 抛的 JournalWriteError）。

    用类名而不是 import：core/ 不认识 storage（见模块 docstring 的边界约定），
    而 `JournalWriteError` 是 RuntimeError 的子类，按名字识别足够精确。
    """
    return type(exc).__name__ == "JournalWriteError"


def _terminal(ctx: "TurnContext") -> bool:
    return ctx.status in TERMINAL_STATUSES


@dataclass
class TurnContext:
    """All state that belongs to exactly one turn."""

    turn_id: str
    message: str
    created_at: str = field(default_factory=_now)
    # 受理时刻（单调钟）：排队等待 = started_perf - accepted_perf。
    # 用 perf_counter 而不是墙钟：它测的是「等了多久」，不受系统时间调整影响。
    accepted_perf: float = field(default_factory=time.perf_counter)
    started_perf: float | None = None
    # 真正开始执行的墙钟时刻（TURN_END 的 started_at；台账里也有同一时刻，
    # 台账拿不到时用它兜底，不让前端拿到 null）。
    started_at: str | None = None
    initial_topic: str | None = None
    current_topic: str | None = None
    # accepted | queued | running | completed | failed | cancelled | unavailable
    status: str = "accepted"
    user_message_id: str | None = None
    prediction: Any = None
    loop: Any = None
    cancelled: bool = False
    notices: list[str] = field(default_factory=list)
    knowledge_snapshot: list[dict] = field(default_factory=list)
    final_content: str | None = None
    # 系统核对注释（见 core/turn_facts.py）：**独立字段**，不拼进 final_content
    # （审计 F11）。随 TURN_END 的 annotation 字段发出，前端单独渲染。
    final_annotation: str | None = None
    # 本轮被后端核对通过的完成结论（见 core/turn_facts.py）：随 TURN_END 发出去，
    # 前端据此在回答下方显示「后端已核对」那一行。
    final_verification: dict | None = None
    error: str | None = None
    usage: dict | None = None
    result: dict | None = None
    notify: bool = False  # system-driven (e.g. subagent completion) turn
    # 发送请求身份（前端可传）：幂等受理与查证端点用它定位同一个「用户意图」。
    # 一次发送动作只对应一个 request_id；重试必须复用同一个 id（契约 5）。
    request_id: str | None = None
    turn_start_emitted: bool = False
    turn_end_emitted: bool = False
    # 结束操作（actions）的**显式覆盖**，None = 按 reason_code 走 ACTIONS_BY_REASON。
    # 为什么需要：排队轮被取消时 journal 是 cancelled（不是 interrupted），
    # /api/turns/{id}/resend 必然拒绝，列 resend 就是一个点不通的死按钮；而
    # 「重发这条用户消息」（retry）是真实可用的入口。这条路径单独给 ("retry",)，
    # 不改 user_stopped 的全局映射（active 取消仍然是 resend，既有测试精确断言它）。
    end_actions: tuple[str, ...] | None = None
    # 阶段 1：这一轮的归属在**提交时**就捕获接续意图，在**开始执行时**落实成绑定。
    # 提交之后用户再做的新选择只影响后续提交（排队消息不被追溯改向）。
    intent_id: str | None = None
    bound_topic: str | None = None
    bound_fragment_id: str | None = None
    bound_intent_version: int | None = None
    explicit_target: str | None = None


class TurnManager:
    """Single-flight main-turn scheduler with a FIFO queue."""

    def __init__(
        self,
        runner: TurnRunner | None = None,
        publisher: Callable[[dict], Awaitable[None]] | None = None,
        emitter: EventEmitter | None = None,
        journal: Any = None,
    ) -> None:
        self._runner = runner
        self._publisher = publisher
        self._emitter = emitter
        self._queue: asyncio.Queue[TurnContext] = asyncio.Queue()
        self._active: TurnContext | None = None
        self._pending: list[TurnContext] = []
        self._cancelled: list[dict] = []  # 最近被取消的 turn（有界）
        # 发送请求身份索引（有界）：client_request_id → 最近一次绑定的 TurnContext。
        # 幂等受理与查证端点只按它关联同一意图；进程重启后为空（查证返回 unknown）。
        self._request_index: dict[str, TurnContext] = {}
        self._REQUEST_INDEX_LIMIT = 300
        # 预留（附件准备中）的轮次：**不入队**，等准备完成后按**预留顺序**放行（R6 §1.1）。
        # 顺序即 FIFO：后预留的即使先就绪，也要等前面的先放行（有界兜底见 ACTIVATION_ORDER_TIMEOUT）。
        self._reserved: list[TurnContext] = []
        self._ready: set[str] = set()
        self._ready_since: dict[str, float] = {}
        self._order_timer: Any = None
        # 准备标识（plan §1.1）：prepare_id → turn_id。预留期间有效；放行/放弃时按
        # 「已开始」「已取消」两个**有界历史**记住它 —— 重复取消必须幂等回「已取消」，
        # 放行之后取消必须如实回「已开始」（前端据此走既有停止流程）。
        self._prepares: dict[str, str] = {}
        self._prepare_of_turn: dict[str, str] = {}
        self._prepare_signals: dict[str, asyncio.Future] = {}
        self._cancelled_prepares: OrderedDict[str, str] = OrderedDict()
        self._started_prepares: OrderedDict[str, str] = OrderedDict()
        self._futures: dict[str, asyncio.Future] = {}
        self._worker: asyncio.Task | None = None
        # 补发的 TURN_END 任务（排队轮被取消，不走 worker）：持句柄避免被 GC，
        # shutdown 时会等它们跑完（见 _schedule_turn_end）。
        self._emit_tasks: set[asyncio.Task] = set()
        self._closed = False
        # 可选的持久化台账（见 storage/turn_journal.py）：被 API 接受过的消息
        # 从此有痕迹，进程退出后不会静默消失。core/ 不认识 storage，只按协议调用。
        #
        # 契约 C2：**受理必须先持久化成功**。所以 accepted 的失败不是「只记日志」，
        # 它会变成 TurnAcceptError（见 submit）。其它台账写入（running / terminal /
        # 用户消息 id）仍然是旁路：失败只记日志，不打断已经在跑的对话。
        self._journal: Any = journal
        # 队列快照的版本号：每一次影响快照的状态变化都 +1。
        # 前端据此丢弃「比已知状态更旧」的快照 —— 快照是权威的，
        # 但**旧**的权威快照不能覆盖更新的事件（例如 TURN_START 之后晚到的 running=null）。
        self._revision = 0
        # 后端实例标识（由 create_app 注入）：进程重启后 revision 会从头计数，
        # 前端必须靠它判断「基准已经换了」，而不是把新实例的低 revision 当成旧状态。
        self.instance_id: str | None = None

    # -- wiring -----------------------------------------------------------

    def _bump_revision(self) -> int:
        self._revision += 1
        return self._revision

    def set_runner(self, runner: TurnRunner) -> None:
        self._runner = runner

    def set_publisher(self, publisher: Callable[[dict], Awaitable[None]]) -> None:
        self._publisher = publisher

    def set_emitter(self, emitter: EventEmitter) -> None:
        """Wire the SSE emitter; kept as a callable so core/ never imports api/."""
        self._emitter = emitter

    def set_journal(self, journal: Any) -> None:
        """接上 turn 持久化台账（duck-typed：accepted / running / terminal）。"""
        self._journal = journal

    # -- journal（跨重启的痕迹；写入失败只忽略，绝不影响 turn）------------

    def note_user_message(self, turn_id: str, message_id: str | None) -> None:
        """把「这一轮的用户消息已经写进历史」记进台账。

        重启后就能如实区分「消息连历史都没进」和「消息已保存、只是没生成回答」。
        """
        self._journal_call("note_user_message", turn_id, message_id)

    def _journal_call(self, method: str, *args, **kwargs) -> bool:
        """旁路台账调用（running / terminal / note_user_message）：失败只记日志。

        **受理那一次不走这里**：它必须让失败可见（`submit` 里直接调 `accepted`
        并抛 `TurnAcceptError`）。其余写入发生在 turn 已经在跑之后，写不进去
        也不该把对话打断 —— 但会返回 False，调用方需要时可以自己记一笔。
        """
        journal = self._journal
        if journal is None:
            return False
        fn = getattr(journal, method, None)
        if fn is None:
            return False
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - 台账是旁路，不能挡住对话
            return False
        return True

    def _persist_accept(self, ctx: "TurnContext", *, status: str) -> None:
        """受理的**唯一**持久化入口（契约 C2）：写不进去 = 这条消息没有被接受。

        为什么单独抽出来：`submit()` 与 `reserve()`（附件准备路径）都是「已经受理」
        的入口，必须对同一个故障给出同一个结果 —— 否则附件路径会出现「API 回了
        accepted、库里却一行都没有」的消息，重启后静默消失（正是 C2 要根除的缺陷）。
        旁路写入（running / terminal / note_user_message）仍走 `_journal_call`：
        那些发生在 turn 已经跑起来之后，写不进去不该把对话打断。
        """
        journal = self._journal
        if journal is None:
            return
        try:
            journal.accepted(
                turn_id=ctx.turn_id,
                message=ctx.message,
                topic_id=ctx.initial_topic,
                notify=ctx.notify,
                status=status,
            )
        except Exception as exc:  # noqa: BLE001 - 任何台账失败都等于「没接受」
            raise TurnAcceptError(f"turn not accepted: {exc}") from exc

    # -- queue snapshot ---------------------------------------------------

    def snapshot(self) -> dict:
        active = self._active
        return {
            "instance_id": self.instance_id,
            "revision": self._revision,
            "running": (
                {"turn_id": active.turn_id, "message": active.message[:120]}
                if active is not None
                else None
            ),
            "queued": [
                {"turn_id": c.turn_id, "message": c.message[:120]}
                for c in self._pending
            ],
            "cancelled": list(self._cancelled),
        }

    def _record_cancelled(self, ctx: TurnContext) -> None:
        self._cancelled.append({"turn_id": ctx.turn_id, "message": ctx.message[:120]})
        del self._cancelled[:-5]  # 只保留最近 5 条

    def _schedule_emit(self) -> None:
        if self._publisher is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(self._emit_queue())

    async def _emit_queue(self) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(self.snapshot())
        except Exception:  # noqa: BLE001 - queue broadcast must not break turns
            pass

    # -- submission -------------------------------------------------------

    # -- 预留 → 准备 → 放行（R6 §1.1）--------------------------------------

    def reserve(
        self,
        message: str,
        topic_id: str | None = None,
        *,
        notify: bool = False,
        intent_id: str | None = None,
        prepare_id: str | None = None,
        request_id: str | None = None,
    ) -> TurnContext:
        """预留一轮：分配 turn_id、落台账行 —— **不入队、不发 TURN_START**。

        用在「附件还没准备好就不能开始执行」的路径上：路由先 reserve 拿到 turn_id，
        用它去准备/克隆附件；成功才 activate（入队 + 发 TURN_START），失败就 abandon。
        预留期间 worker 完全看不到这一轮，所以模型不可能被提前调用。
        """
        if self._closed:
            raise RuntimeError("TurnManager is closed; it no longer accepts new turns")
        ctx = TurnContext(
            turn_id=f"turn_{uuid.uuid4().hex[:12]}",
            message=message,
            initial_topic=topic_id,
            current_topic=topic_id,
            notify=notify,
            intent_id=intent_id,
            # 内部状态：既不是 accepted（已入队）也不是 running（在跑）——
            # 它不会出现在队列快照里，前端沿用请求进行中的「发送中」。
            status="preparing",
        )
        # 1) persist：受理的唯一持久化入口（契约 C2）—— 写不进去 = 没有被接受，
        # 绝不留下「API 回了 accepted、库里没有痕迹」的预留。
        # 预留同样算已经受理：准备期间进程退出不能让这条消息静默消失。
        # 状态写 queued（台账没有 preparing 这一档），abandon 时会如实收尾。
        self._persist_accept(ctx, status="queued")
        # 2) 记住这一轮（不入队、不发 TURN_START）
        try:
            loop = asyncio.get_running_loop()
            self._futures[ctx.turn_id] = loop.create_future()
        except RuntimeError:
            pass  # no running loop: reserve without an awaitable result
        self._reserved.append(ctx)
        if prepare_id:
            self._register_prepare(prepare_id, ctx.turn_id)
        if request_id:
            # 契约 5（幂等受理）：预留同样算「已经受理」—— 同一个 client_request_id
            # 重试必须命中同一轮，绝不产生第二条 turn。索引有界，进程重启后为空
            # （按请求查证如实返回 unknown）。
            ctx.request_id = request_id
            self._request_index[request_id] = ctx
            while len(self._request_index) > self._REQUEST_INDEX_LIMIT:
                oldest = next(iter(self._request_index))
                self._request_index.pop(oldest, None)
        return ctx

    # -- 准备标识与可确认取消（plan §1.1）----------------------------------

    def _register_prepare(self, prepare_id: str, turn_id: str) -> None:
        self._cancelled_prepares.pop(prepare_id, None)
        self._started_prepares.pop(prepare_id, None)
        self._prepares[prepare_id] = turn_id
        self._prepare_of_turn[turn_id] = prepare_id
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if prepare_id not in self._prepare_signals:
            self._prepare_signals[prepare_id] = loop.create_future()

    def prepare_signal(self, prepare_id: str | None):
        """本次准备的**取消信号**：显式取消端点与断连监测都会兑现它。

        事件驱动（等待方 await 这个 future），不轮询、不靠固定延时判断。
        """
        if not prepare_id:
            return None
        fut = self._prepare_signals.get(prepare_id)
        if fut is not None and not fut.done():
            return fut
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        done = loop.create_future()
        done.set_result("cancelled" if self.prepare_was_cancelled(prepare_id) else "finished")
        return done

    def prepare_was_cancelled(self, prepare_id: str | None) -> bool:
        """这个准备标识是否已被取消 —— **activate 之前必须复核**（迟到复制成功不得重启本轮）。"""
        return bool(prepare_id) and prepare_id in self._cancelled_prepares

    def cancel_prepare(self, prepare_id: str) -> tuple[str, str | None]:
        """按取消契约处理一个准备标识（幂等）。

        返回 (state, turn_id)：

        * `("cancelled", turn_id)`：这一轮已被放弃 —— 不入队、不调用模型、不执行工具；
        * `("already_started", turn_id)`：已经放行/开始 —— 调用方必须走**既有停止流程**
          （不得假装「没有发送」）；
        * `("unknown", None)`：未知 / 已过期标识（幂等，不报错）。
        """
        if not prepare_id:
            return "unknown", None
        known = self._prepares.get(prepare_id)
        if known is None:
            if prepare_id in self._cancelled_prepares:
                return "cancelled", self._cancelled_prepares[prepare_id]
            if prepare_id in self._started_prepares:
                return "already_started", self._started_prepares[prepare_id]
            return "unknown", None
        ctx = self._reserved_by_id(known)
        if ctx is None or _terminal(ctx):
            # 已经放行（或已经收尾）：如实回「已开始」，由前端走停止流程
            self._mark_started_prepare(known)
            return "already_started", known
        self._cancel_reserved(ctx, reason="cancelled_during_prepare")
        return "cancelled", known

    def _reserved_by_id(self, turn_id: str) -> TurnContext | None:
        for ctx in self._reserved:
            if ctx.turn_id == turn_id:
                return ctx
        return None

    def _wake_prepare(self, turn_id: str) -> None:
        """兑现该轮准备标识的取消信号：正在等它的请求立刻走取消分支。"""
        prepare_id = self._prepare_of_turn.get(turn_id)
        if not prepare_id:
            return
        fut = self._prepare_signals.get(prepare_id)
        if fut is not None and not fut.done():
            fut.set_result("cancelled")

    def _forget_prepare(self, turn_id: str) -> None:
        """放弃/收尾：这个标识不再是「准备中」（历史里也不会变成「已开始」）。"""
        prepare_id = self._prepare_of_turn.pop(turn_id, None)
        if not prepare_id:
            return
        self._prepares.pop(prepare_id, None)
        fut = self._prepare_signals.pop(prepare_id, None)
        if fut is not None and not fut.done():
            fut.set_result("finished")

    def _mark_started_prepare(self, turn_id: str) -> None:
        """放行：标识从「准备中」转为「已开始」（之后取消要如实回 already_started）。"""
        prepare_id = self._prepare_of_turn.pop(turn_id, None)
        if not prepare_id:
            return
        self._prepares.pop(prepare_id, None)
        fut = self._prepare_signals.pop(prepare_id, None)
        if fut is not None and not fut.done():
            fut.set_result("finished")
        self._started_prepares[prepare_id] = turn_id
        while len(self._started_prepares) > PREPARE_HISTORY:
            self._started_prepares.popitem(last=False)

    def _remember_cancelled_prepare(self, prepare_id: str, turn_id: str) -> None:
        self._cancelled_prepares[prepare_id] = turn_id
        while len(self._cancelled_prepares) > PREPARE_HISTORY:
            self._cancelled_prepares.popitem(last=False)

    def _remember_cancelled(self, turn_id: str) -> None:
        prepare_id = self._prepare_of_turn.get(turn_id)
        if prepare_id:
            self._remember_cancelled_prepare(prepare_id, turn_id)

    def _cancel_reserved(self, ctx: TurnContext, *, reason: str) -> bool:
        """取消一个**准备中**的预留（幂等）：丢弃 + 兑现等待者 + FIFO 链继续。"""
        if ctx not in self._reserved:
            return False
        self._reserved.remove(ctx)
        self._ready.discard(ctx.turn_id)
        self._ready_since.pop(ctx.turn_id, None)
        ctx.cancelled = True
        if not _terminal(ctx):
            ctx.status = "cancelled"
            self._journal_call("terminal", ctx.turn_id, "cancelled", reason=reason)
        self._record_cancelled(ctx)
        self._remember_cancelled(ctx.turn_id)  # 必须早于 _forget_prepare
        self._wake_prepare(ctx.turn_id)  # 等信号的请求立刻走取消分支（不靠轮询）
        self._forget_prepare(ctx.turn_id)
        self._resolve(ctx, {"ok": False, "reason": reason})
        self._flush_ready()
        return True

    def activate(self, ctx: TurnContext) -> None:
        """附件就绪后放行：**按预留顺序**入队（此刻起 worker 才可能开始执行）。"""
        if ctx not in self._reserved:
            return  # 已经被取消 / 放弃 / 已经放行
        self._ready.add(ctx.turn_id)
        self._ready_since.setdefault(ctx.turn_id, self._monotonic())
        self._flush_ready()
        self._schedule_order_deadline()

    def abandon(self, ctx: TurnContext, *, reason: str = "prepare_failed") -> None:
        """准备失败 / 准备期间被取消：丢弃预留 —— 不入队、不留可执行队列项。

        **顺序是契约的一部分（plan §1.2）**：先兑现等待者，再清结果表。
        反过来（先 pop _futures 再 _resolve，而 _resolve 内部也 pop）会让已经开始的
        await wait(turn_id) **永远不返回** —— 等待者既拿不到结果，也等不到超时。

        台账如实收尾成 cancelled + 具体 reason（**不是** shutdown 的 interrupted）：
        这一轮从未开始执行，不能事后看起来像「被中断的一轮」。
        """
        if ctx in self._reserved:
            self._reserved.remove(ctx)
        self._ready.discard(ctx.turn_id)
        self._ready_since.pop(ctx.turn_id, None)
        self._forget_prepare(ctx.turn_id)  # 放弃之后它不再是「准备中」
        # 已终态的轮次（刚跑完 / 已被取消）：迟到的 abandon 不改写它，也不重复落终态。
        if not _terminal(ctx):
            ctx.status = "cancelled"
            self._journal_call("terminal", ctx.turn_id, "cancelled", reason=reason)
        # 兑现等待者：_resolve 自己负责清表，且幂等（重复调用不会重复设置）。
        self._resolve(ctx, {"ok": False, "reason": reason})
        # FIFO 链不能断：前面放弃了，后面已经就绪的预留要立刻能走
        self._flush_ready()

    def _flush_ready(self) -> None:
        """按预留顺序放行：队首没就绪就停下等它（有界兜底见 _force_expired_ready）。"""
        while self._reserved:
            head = self._reserved[0]
            if head.turn_id not in self._ready:
                break
            self._reserved.pop(0)
            self._ready.discard(head.turn_id)
            self._ready_since.pop(head.turn_id, None)
            self._enqueue_reserved(head)

    def _enqueue_reserved(self, ctx: TurnContext) -> None:
        """真正入队（原 submit 的尾段）：从这里开始 worker 才可能取到它并发 TURN_START。"""
        if ctx.cancelled or _terminal(ctx):
            # 竞态兜底：放行之前这一刻已经被取消（例如取消正好落在准备完成边界）
            self._forget_prepare(ctx.turn_id)
            self._resolve(ctx, {"ok": False, "reason": "cancelled"})
            return
        self._mark_started_prepare(ctx.turn_id)  # 之后取消要如实回 already_started
        ctx.status = "queued" if (self._active is not None or bool(self._pending)) else "accepted"
        self._pending.append(ctx)
        self._queue.put_nowait(ctx)
        self._bump_revision()
        self._ensure_worker()
        self._schedule_emit()

    @staticmethod
    def _monotonic() -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return time.monotonic()

    def _schedule_order_deadline(self) -> None:
        """有界等待：到点后把「等太久」的就绪预留放行（记警告），不无限期挡住后面的。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._order_timer is not None and not self._order_timer.cancelled():
            return
        self._order_timer = loop.call_later(ACTIVATION_ORDER_TIMEOUT, self._force_expired_ready)

    def _force_expired_ready(self) -> None:
        self._order_timer = None
        now = self._monotonic()
        for ctx in list(self._reserved):
            if ctx.turn_id not in self._ready:
                continue
            since = self._ready_since.get(ctx.turn_id)
            if since is None or now - since < ACTIVATION_ORDER_TIMEOUT:
                continue
            logger.warning(
                "预留顺序等待超过 %.0fs，按就绪顺序放行（前面的预留没有按时放行）：%s",
                ACTIVATION_ORDER_TIMEOUT,
                ctx.turn_id,
            )
            self._reserved.remove(ctx)
            self._ready.discard(ctx.turn_id)
            self._ready_since.pop(ctx.turn_id, None)
            self._enqueue_reserved(ctx)
        if self._ready:
            self._schedule_order_deadline()

    def submit(
        self,
        message: str,
        topic_id: str | None = None,
        *,
        notify: bool = False,
        intent_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
    ) -> TurnContext:
        """受理一条 turn：**先持久化，成功之后才入队并返回**（契约 C2）。

        顺序是固定的 persist → dispatch，不能反：

        * 先入队再持久化：台账写失败时会留下一个「内存里有、库里没有」的 turn，
          API 已经回 200，用户以为发出去了，重启后这条消息没有任何痕迹；
        * 先持久化再入队：写失败就抛 `TurnAcceptError`，调用方明确拒绝这条消息
          （HTTP 503 + accepted=false），内存与磁盘都不存在它。

        提交之后、worker 真正派发之前进程退出的窗口，由台账里那条 `queued` 行
        覆盖：重启后它是可见的「没有执行的消息」，但**不会**被自动执行。
        """
        if self._closed:
            # worker 已经停了：再收下这个 turn，它只会躺在队列里永远不被执行
            # （调用方还会一直 await 一个永远不会兑现的 future）。
            raise RuntimeError("TurnManager is closed; it no longer accepts new turns")
        # 提交成功 ≠ 开始执行：前面还有主 turn 或已经排着队时，它就是 queued。
        waits = self._active is not None or bool(self._pending)
        ctx = TurnContext(
            turn_id=turn_id or f"turn_{uuid.uuid4().hex[:12]}",
            message=message,
            initial_topic=topic_id,
            current_topic=topic_id,
            notify=notify,
            intent_id=intent_id,
            status="queued" if waits else "accepted",
        )
        # 1) persist：台账写失败 → 这条消息没有被接受，绝不入队（契约 C2）。
        self._persist_accept(ctx, status=ctx.status)
        # 2) dispatch：到这里台账已经有了这一行，入队才是安全的。
        try:
            loop = asyncio.get_running_loop()
            self._futures[ctx.turn_id] = loop.create_future()
        except RuntimeError:
            pass  # no running loop: enqueue without an awaitable result
        if request_id:
            ctx.request_id = request_id
            self._request_index[request_id] = ctx
            # 有界：超出上限丢弃最老的映射（查证对很早的请求返回 unknown 是诚实行为）
            while len(self._request_index) > self._REQUEST_INDEX_LIMIT:
                oldest = next(iter(self._request_index))
                self._request_index.pop(oldest, None)
        self._pending.append(ctx)
        self._queue.put_nowait(ctx)
        self._bump_revision()
        self._ensure_worker()
        self._schedule_emit()
        return ctx

    async def wait(self, turn_id: str, timeout: float | None = None) -> dict | None:
        """等待某个 turn 的结果。

        `wait` 是一次**等待操作**，和 turn 本身的生命周期是两件事：

        * 超时只结束这一次等待 —— 不删除、也不取消 turn 的 completion future；
        * 调用方被取消（任务取消）同样只结束这一次等待；
        * completion future 只在 turn 进入终态、被兑现之后清理（见 `_resolve`）。

        所以这里必须用 `asyncio.shield`：`wait_for` / 任务取消只会取消 shield 的外层，
        不会把内层 future 一起取消掉（否则 turn 结束时结果就没有地方落地了）。
        """
        fut = self._futures.get(turn_id)
        if fut is None:
            return None
        try:
            if timeout is None:
                return await asyncio.shield(fut)
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None

    def lookup_request(self, request_id: str) -> TurnContext | None:
        """按发送请求身份查证：这个意图在本进程里对应哪一轮（可能已结束）。返回 None
        表示本进程没有该请求的记录（包括进程重启后），查证端点必须如实呈现 unknown。
        """
        if not request_id:
            return None
        return self._request_index.get(str(request_id).strip())

    def _resolve(self, ctx: TurnContext, payload: dict) -> None:
        """兑现某个 turn 的等待者（幂等：已经兑现过的不再重复设置）。"""
        fut = self._futures.pop(ctx.turn_id, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

    # -- worker: single-flight FIFO --------------------------------------

    async def _work(self) -> None:
        while not self._closed:
            ctx = await self._queue.get()
            if ctx in self._pending:
                self._pending.remove(ctx)
            # tombstone：排队期间被取消（或已进入终态）的 turn 直接跳过。
            # 它从未开始执行，因此既不能占用 active，也不能发 TURN_START。
            if ctx.cancelled or _terminal(ctx):
                self._resolve(ctx, {"ok": False, "reason": "cancelled"})
                self._schedule_emit()
                continue
            self._active = ctx
            ctx.status = "running"
            ctx.started_perf = time.perf_counter()
            ctx.started_at = _now()
            self._journal_call("running", ctx.turn_id)
            self._bump_revision()
            self._schedule_emit()
            await self._emit_turn_start(ctx)
            try:
                if ctx.cancelled:
                    ctx.status = "cancelled"
                else:
                    await self._runner(ctx)
                    if ctx.cancelled:
                        ctx.status = "cancelled"
                    elif ctx.status in ("running", "accepted"):
                        # 权威终态：不完整 EOF 不是完成（冻结契约 C2）。
                        ctx.status = self._completion_status(ctx)
            except asyncio.CancelledError:
                ctx.status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - turn isolation boundary
                ctx.status = "failed"
                ctx.error = f"{type(exc).__name__}: {exc}"
            finally:
                self._flush_trace_phases(ctx)
                # 终态落台账；关闭中的 cancelled 记成 interrupted（那是进程掐断的，
                # 不是用户取消的 —— 重启后应当给用户重发的机会）。
                self._journal_call(
                    "terminal",
                    ctx.turn_id,
                    ctx.status,
                    reason="shutdown" if self._closed else None,
                )
                await self._emit_turn_end(ctx)
                if self._active is ctx:
                    self._active = None
                    self._bump_revision()
                self._resolve(
                    ctx,
                    ctx.result if ctx.result is not None else {"ok": False, "reason": ctx.status},
                )
                self._schedule_emit()

    def _completion_status(self, ctx: TurnContext) -> str:
        """runner 正常跑完后的权威终态：completed 或 incomplete。

        唯一依据是循环给出的**协议事实**（core/loop.py::_note_model_termination）：
        `reason_code == incomplete_stream` 表示流在给出结束标记之前就结束了 —— 已经
        确认的正文保留，但整轮如实标成 incomplete，绝不显示成「正常完成」。

        length_limit / content_filter 不在这里：它们是厂商合法终止，状态仍是 completed。
        """
        code = str(self._turn_result(ctx).get("stop_reason_code") or "")
        return INCOMPLETE_STATUS if code == INCOMPLETE_STREAM_CODE else "completed"

    def _flush_trace_phases(self, ctx: TurnContext) -> None:
        """把这一轮的阶段时间落库（幂等）。

        收口放在 turn 生命周期的 finally 里：不论 runner 是正常结束、抛异常，
        还是根本没走到 trace_store.finish（例如凭据不可用提前返回），
        「这一轮的时间去哪了」都不会丢。计时句柄由 ctx.trace 提供（鸭子类型，
        core/ 不认识 trace 的具体实现）。
        """
        flush = getattr(getattr(ctx, "trace", None), "flush_phases", None)
        if flush is None:
            return
        try:
            flush()
        except Exception:  # noqa: BLE001 - 阶段时间写不进去不能影响 turn 收尾
            pass

    # -- lifecycle events -------------------------------------------------

    async def _emit_turn_start(self, ctx: TurnContext) -> None:
        if ctx.turn_start_emitted:
            return
        ctx.turn_start_emitted = True
        await self._emit_event(
            TURN_START,
            {
                "turn_id": ctx.turn_id,
                "instance_id": self.instance_id,
                # revision 让前端能把「新的 turn 状态」和「旧的队列快照」比较：
                # 晚到的旧快照不得把这一轮清掉。
                "revision": self._revision,
                "message": ctx.message[:200],
                # 发送请求身份：前端可用它把 TURN_START 关联回自己的发送动作。
                "request_id": ctx.request_id,
            },
        )

    def _schedule_turn_end(self, ctx: TurnContext) -> None:
        """为「不经过 worker 的终态」补发 TURN_END（当前用于排队轮被取消）。

        worker 取到 tombstone 只会跳过，所以这一条结束事件必须在这里显式调度；
        任务句柄被持有（`_emit_tasks`），shutdown 会等它跑完 —— 不留半截事实，
        也不产生 asyncio 的「任务被 GC」警告。幂等由 `turn_end_emitted` 保证。
        """
        if ctx.turn_end_emitted:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环就没法发异步事件；调用方在异步上下文之外
        task = asyncio.create_task(self._emit_turn_end(ctx))
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)

    async def _emit_turn_end(self, ctx: TurnContext) -> None:
        """一个 accepted turn 必须且只能有一个 TURN_END（含异常路径）。

        `incomplete` 与其它四个终态一样只在这里定稿：不是终态才兜底成 completed。
        """
        if ctx.status not in TERMINAL_STATUSES:
            ctx.status = "completed"
        if ctx.turn_end_emitted:
            return
        ctx.turn_end_emitted = True
        payload: dict[str, Any] = {
            "turn_id": ctx.turn_id,
            "instance_id": self.instance_id,
            "revision": self._revision,
            "status": ctx.status,
            "final_content": ctx.final_content,
            "error": ctx.error,
        }
        # 耗时事实（plan §3）：duration_ms 权威来自 trace 台账；台账还没收口时
        # 退回进程内单调钟测得的执行时长 —— 缺失 ≠ 0，也不给一个假的 0。
        payload.update(self._turn_timing(ctx))
        # 结束事实（plan §1.2）：为什么停下来 + 当前确实可用的操作。
        payload.update(self._end_facts(ctx))
        if ctx.final_verification:
            payload["verification"] = ctx.final_verification
        # 系统核对注释（审计 F11：只追加字段，不改 final_content 的旧语义）。
        # ctx 上没显式给时，从服务层放进 result["turn"] 的 TurnResult 里取。
        annotation = ctx.final_annotation
        if annotation is None:
            raw_annotation = self._turn_result(ctx).get("final_annotation")
            annotation = raw_annotation if isinstance(raw_annotation, str) and raw_annotation else None
        if annotation:
            payload["annotation"] = annotation
        # 回答身份（冻结契约 K2）：本次最终内容所校准的正式回答的 delta_id；
        # 没有可校准回答时为 null —— 前端据此定位目标回答，不再靠文字相似度猜。
        answer_id = self._turn_result(ctx).get("final_answer_id")
        payload["answer_id"] = answer_id if isinstance(answer_id, str) and answer_id else None
        if ctx.usage:
            payload.update(ctx.usage)
        await self._emit_event(TURN_END, payload)

    # -- 结束事实（plan §1.2）-----------------------------------------------

    def _end_facts(self, ctx: TurnContext) -> dict[str, Any]:
        """TURN_END 的 reason_code / reason / stopped_by / actions。

        只写系统**确实知道**的事实，不猜、不伪造：

        * 循环记下的停止原因（预算 / 无进展 / 护栏）随 TurnResult 到达
          （服务层把它放进 ctx.result["turn"]）；
        * 取消分两种：用户按的停止是 user_stopped，进程收尾掐断的是 interrupted；
        * 失败按**异常类名**分类：适配器错误 → provider_error，其余 → internal_error；
        * 不完整结束（incomplete）→ incomplete_stream + system + 标准人话原因；
        * 没有事实 / 旧记录 → "none"，不编一个理由；旧记录也不会被补写。

        actions 默认按 reason_code 走 ACTIONS_BY_REASON；ctx.end_actions 显式给了就
        以它为准（当前只有「排队轮被取消」这一条路径给 ("retry",)，见 TurnContext）。
        """
        status = ctx.status
        turn = self._turn_result(ctx)
        code = str(turn.get("stop_reason_code") or "")
        reason: str | None = turn.get("stop_reason")
        stopped_by: str | None = turn.get("stopped_by")
        if status == "cancelled":
            # 谁停的：用户按的停止优先（进程恰好也在收尾不影响这个事实）；
            # 只有「没有任何人按停止、进程自己掐断」才是 interrupted。
            if ctx.cancelled:
                code, stopped_by, reason = (
                    "user_stopped",
                    "user",
                    USER_STOPPED_REASON,
                )
            else:
                code, stopped_by, reason = "interrupted", "system", INTERRUPTED_REASON
        elif status == "unavailable":
            code, stopped_by, reason = (
                "credential_unavailable",
                "system",
                NO_CREDENTIAL_REASON,
            )
        elif status == "failed":
            code, stopped_by, reason = self._failure_facts(ctx.error)
        elif status == INCOMPLETE_STATUS:
            # 不完整 EOF（冻结契约 C2）：正文保留，但整轮如实标「未完成 + 原因 + retry」。
            # 循环正常会给全三个字段；拿不到时兜底，绝不退回「无原因的正常完成」。
            code = code or INCOMPLETE_STREAM_CODE
            stopped_by = stopped_by or "system"
            reason = reason or INCOMPLETE_REASON
        elif status == "completed" and not code:
            code, stopped_by, reason = "none", None, None
        if not code:
            code = "none"
        actions = (
            list(ctx.end_actions)
            if ctx.end_actions is not None
            else list(ACTIONS_BY_REASON.get(code, ()))
        )
        return {
            "reason_code": code,
            "reason": self._clean_reason(reason),
            "stopped_by": stopped_by,
            "actions": actions,
        }

    @staticmethod
    def _turn_result(ctx: TurnContext) -> dict:
        """本轮循环的 TurnResult 事实：服务层以 {"ok": ..., "turn": {...}} 放在 ctx.result。"""
        result = ctx.result if isinstance(ctx.result, dict) else {}
        turn = result.get("turn")
        return turn if isinstance(turn, dict) else {}

    @staticmethod
    def _failure_facts(error: str | None) -> tuple[str, str, str | None]:
        """失败 → (reason_code, stopped_by, reason)。依据是异常**类名**前缀。"""
        text = (error or "").strip()
        name = text.split(":", 1)[0].strip()
        code = "provider_error" if name in PROVIDER_ERROR_NAMES else "internal_error"
        return code, "system", text or None

    @staticmethod
    def _clean_reason(reason: str | None) -> str | None:
        """人话原因：过 redact、截断到 REASON_MAX_CHARS；没有原因就是 None。

        这是**新增输出路径**，所以必须过 agent/trace/redact.py：打码器自己出问题时
        宁可给一句「无法安全显示」，也绝不把原文发出去。
        """
        if not reason:
            return None
        from agent.trace.redact import redact_text

        try:
            text = redact_text(str(reason))
        except Exception:  # noqa: BLE001 - 打码失败也不能把未打码的原文发出去
            return "（原因包含无法安全显示的内容）"
        text = text.strip()
        if not text:
            return None
        if len(text) > REASON_MAX_CHARS:
            text = text[: REASON_MAX_CHARS - 1] + "…"
        return text

    # -- 耗时事实（plan §3）-------------------------------------------------

    @staticmethod
    def _trace_ledger(ctx: TurnContext) -> dict:
        """trace 台账里的这一行（duck-typed，core/ 不认识 trace 的具体实现）。

        拿不到就返回空 dict —— 台账是旁路，不能挡住 TURN_END。
        """
        tracer = getattr(ctx, "trace", None)
        store = getattr(tracer, "store", None)
        getter = getattr(store, "get", None)
        if getter is None:
            return {}
        try:
            row = getter(ctx.turn_id)
        except Exception:  # noqa: BLE001 - 台账异常不得影响 turn 收尾
            return {}
        return dict(row) if isinstance(row, dict) else {}

    def _turn_timing(self, ctx: TurnContext) -> dict[str, Any]:
        """TURN_END 的耗时字段：duration_ms / queue_ms / started_at / ended_at。

        * queue_ms = 受理到真正开跑（用户等的时间，不是执行时间）；
        * duration_ms 优先取台账（trace/store.py 已有 duration_ms，与「时间去哪了」
          同一口径）；台账没写（例如 runner 抛异常前就结束了）时退回 perf_counter
          测得的执行窗口；
        * started_at / ended_at 用台账的墙钟；台账没有就先用开跑时刻 / 现在。
        """
        facts: dict[str, Any] = {}
        started_perf = ctx.started_perf
        if started_perf is not None:
            facts["duration_ms"] = max(0, int((time.perf_counter() - started_perf) * 1000))
            facts["queue_ms"] = max(0, int((started_perf - ctx.accepted_perf) * 1000))
        else:
            # 从未开始执行（排队中被取消）：执行时长是 0（不是「缺失」），排队时长照实给。
            # 契约 C8 的总耗时 = 排队 + 执行，这里两个数字都必须存在且非负。
            facts["duration_ms"] = 0
            facts["queue_ms"] = max(0, int((time.perf_counter() - ctx.accepted_perf) * 1000))
        ledger = self._trace_ledger(ctx)
        if ledger.get("duration_ms") is not None:
            facts["duration_ms"] = max(0, int(ledger["duration_ms"]))
        facts["started_at"] = ledger.get("started_at") or ctx.started_at or ctx.created_at
        facts["ended_at"] = ledger.get("ended_at") or _now()
        return facts

    async def _emit_event(self, name: str, data: dict) -> None:
        if self._emitter is None:
            return
        try:
            await self._emitter(name, data)
        except Exception:  # noqa: BLE001 - 广播失败不能影响 turn 收尾
            pass

    # -- introspection / control -----------------------------------------

    @property
    def active(self) -> TurnContext | None:
        return self._active

    def active_loop(self) -> Any:
        return self._active.loop if self._active is not None else None

    def push_notice(self, text: str) -> bool:
        """Route a notice to the active turn's loop; False when none."""
        loop = self.active_loop()
        if loop is None:
            return False
        loop.push_notice(text)
        return True

    def cancel_active(self) -> bool:
        """Cancel the active turn's in-flight work; False when idle."""
        ctx = self._active
        if ctx is None:
            return False
        ctx.cancelled = True
        if ctx.loop is not None:
            ctx.loop.cancel()
        self._record_cancelled(ctx)
        self._bump_revision()
        self._schedule_emit()
        return True

    def cancel(self, turn_id: str) -> bool:
        """Cancel a specific turn — active or still queued."""
        active = self._active
        if active is not None and active.turn_id == turn_id:
            return self.cancel_active()
        for c in list(self._reserved):
            if c.turn_id == turn_id:
                if _terminal(c):
                    return False
                # 准备中的预留：直接丢弃，绝不让它之后还被放行（客户端断开 / 用户取消）
                return self._cancel_reserved(c, reason="user")
        for i, c in enumerate(self._pending):
            if c.turn_id == turn_id:
                if _terminal(c):
                    return False
                c.cancelled = True
                c.status = "cancelled"
                # 排队取消的可用操作是 retry（前端重发这条用户消息）。journal 记成
                # cancelled（不是 interrupted），/api/turns/{id}/resend 必然拒绝 ——
                # 列 resend 就是死按钮。active 取消路径不受影响，仍是 resend。
                c.end_actions = ("retry",)
                self._pending.pop(i)
                self._journal_call("terminal", c.turn_id, "cancelled", reason="user")
                self._record_cancelled(c)
                # 排队项的结局不依赖 worker：立刻兑现等待者，
                # 之后 worker 取到这个 tombstone 只会跳过。
                self._resolve(c, {"ok": False, "reason": "cancelled"})
                self._bump_revision()
                self._schedule_emit()
                # worker 跳过 tombstone，所以这一条 TURN_END 必须由这里补发
                # （冻结契约 C1：accepted turn 恰好一次 TURN_END，含排队期被取消）。
                # 立刻发出，不等 active turn 结束；幂等由 turn_end_emitted 保证。
                self._schedule_turn_end(c)
                return True
        return False

    def queued_count(self) -> int:
        return len(self._pending)

    async def shutdown(self) -> None:
        """应用关闭：不留下任何永远等不到结果的 wait。

        顺序：停止受理新任务 → 处理排队 turn（置终态 + 兑现等待者）
        → 取消 worker（active turn 的收尾在 worker 的 finally 里完成）
        → 清理仍然挂着的等待者与队列对象。
        """
        self._closed = True
        self._bump_revision()

        # 准备中的预留：从未开始执行，也不该留成「可重发的被中断轮」——
        # 如实记成 cancelled + reason=shutdown_during_prepare（不是 interrupted）。
        reserved, self._reserved = list(self._reserved), []
        self._ready.clear()
        self._ready_since.clear()
        for ctx in reserved:
            ctx.cancelled = True
            if not _terminal(ctx):
                ctx.status = "cancelled"
            self._journal_call(
                "terminal", ctx.turn_id, "cancelled", reason="shutdown_during_prepare"
            )
            self._remember_cancelled(ctx.turn_id)
            self._wake_prepare(ctx.turn_id)
            self._forget_prepare(ctx.turn_id)
            self._resolve(ctx, {"ok": False, "reason": "shutdown"})
        pending, self._pending = list(self._pending), []
        for ctx in pending:
            ctx.cancelled = True
            if not _terminal(ctx):
                ctx.status = "cancelled"
            # 排队中还没执行的消息：进程关闭不等于用户取消 —— 记成 interrupted，
            # 下次启动会作为「没有执行的消息」提示用户（不自动重放）。
            self._journal_call("terminal", ctx.turn_id, "cancelled", reason="shutdown")
            self._resolve(ctx, {"ok": False, "reason": "shutdown"})

        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass

        # 补发的 TURN_END（排队轮取消）也要跑完：关停不得留下半截结束事实。
        for task in list(self._emit_tasks):
            try:
                await task
            except Exception:  # noqa: BLE001 - 关停不得抛
                pass

        # 兜底：worker 已经不在，任何还挂着的等待者都必须以终态结束，而不是永远等待。
        for turn_id, fut in list(self._futures.items()):
            if not fut.done():
                fut.set_result({"ok": False, "reason": "cancelled"})
            self._futures.pop(turn_id, None)

        # 丢弃底层队列里剩下的 tombstone / 未执行对象，避免 shutdown 之后仍被引用。
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                break
        self._active = None
