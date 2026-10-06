"""阶段协议（plan §1.2）：把模型的过程说明组织成有标识、有顺序、有状态的阶段。

边界（必须严格遵守）：

* 阶段标识与顺序由 QIO 生成（``st_<turn8>_<n>``），模型只能给 ``op`` 与 ``name``；
* 解析是白名单的：``op`` 不在 {start, next, update}、``name`` 非法 → 一律当成
  「没有阶段操作」安全降级；
* **不自动开阶段**：没有合法 stage 操作的说明只更新「当前阶段说明」；当前没有阶段
  时才建立一个系统兜底的隐式阶段（name 取首条说明，截断）；
* 阶段名与说明都过 ``core/narrative.py`` 的白名单与脱敏，并做长度截断
  （name ≤ 40，text ≤ 120）；
* 阶段只是**展示**：它不改变状态、参数、审批权限与真实结果，也不参与任何判定 ——
  整轮的真实状态只认 TURN_START / TURN_END / TOOL_*（见 plan §1.3）。

一处口径（Lead 裁决 2026-10-06）：``op=update`` 且**当前没有阶段**时，与「没有
stage 操作」走同一条安全降级路径 —— 建立隐式阶段并把这条说明发出去（op=start），
而不是把模型的说明丢掉。「不自动开阶段」约束的是「已有阶段时不要因为新文本
另开一个」，不是「第一个阶段都不给开」。``op=update`` 也**不改阶段名**（名字只在
开阶段时确定，start / next / 隐式兜底）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from agent.core.narrative import MAX_TEXT_CHARS, Narrative, clean_text
from agent.core.turn import short_turn_id

# 模型可以给的操作（plan §1.2）。
STAGE_OPS = ("start", "next", "update")
# 事件里的 op 多一个 end：它是阶段收尾（turn 结束 / 被 next 结束）由系统产生的。
STAGE_EVENT_OPS = ("start", "next", "update", "end")

MAX_STAGE_NAME = 40

STAGE_RUNNING = "running"
STAGE_DONE = "done"


def stage_id(turn_id: str | None, index: int) -> str:
    """系统生成的阶段标识：模型永远不能自己指定 stage_id。"""
    return f"st_{short_turn_id(turn_id)}_{int(index)}"


@dataclass(frozen=True)
class StageOp:
    """一次合法的阶段操作（白名单解析的产物）。"""

    op: str
    name: str


def parse_stage(raw: object) -> StageOp | None:
    """白名单解析 ``_qio.stage``；任何不合法输入都返回 None（= 不改变阶段集合）。

    ``name`` 允许为空：op=next 时可以用本条说明的文本作为新阶段名；
    op 非法一律 None（不会退化成某种「默认操作」）。
    """
    if not isinstance(raw, dict):
        return None
    op = raw.get("op")
    if not isinstance(op, str) or op not in STAGE_OPS:
        return None
    return StageOp(op=op, name=clean_text(raw.get("name"), MAX_STAGE_NAME))


@dataclass(frozen=True)
class StageState:
    """一个阶段的系统事实（不含说明文本）。"""

    stage_id: str
    index: int
    name: str
    status: str = STAGE_RUNNING


@dataclass(frozen=True)
class StageTransition:
    """一次说明对阶段集合造成的**确定性**变化。"""

    action: str  # open | update | none | end
    op: str  # 事件的 op：start | next | update | end
    stage: StageState | None = None
    previous: StageState | None = None  # op=next 时被结束的旧阶段
    text: str = ""
    kind: str = ""


class StageTracker:
    """一轮的阶段状态机（纯内存、无 IO；落库与广播由服务层负责）。"""

    def __init__(self, turn_id: str | None) -> None:
        self.turn_id = turn_id
        self._current: StageState | None = None
        self._index = 0
        # 收集阶段事件时用来去重（同一 stage_id 的同一次说明只发一次）。
        self.emitted: int = 0

    @property
    def current(self) -> StageState | None:
        return self._current

    def _open(self, name: str) -> StageState:
        self._index += 1
        self._current = StageState(
            stage_id=stage_id(self.turn_id, self._index),
            index=self._index,
            name=name[:MAX_STAGE_NAME],
        )
        return self._current

    def observe(self, narrative: Narrative, op: StageOp | None) -> StageTransition:
        """喂一条已解析的说明，得到阶段变化。

        规则与 plan §1.2 一一对应；任何非法/缺失都**不改变阶段集合**。
        """
        text = narrative.text
        kind = narrative.kind
        if not text:
            # 规则 4：text 为空（只有 explanation 的静默说明）→ 不改变阶段集合。
            return StageTransition(action="none", op="update", stage=self._current, kind=kind)
        if op is None:
            # 规则 1：没有合法 stage 操作 → 只更新当前说明；
            # 当前没有阶段时才建立系统兜底的隐式阶段。
            if self._current is None:
                stage = self._open(text)
                return StageTransition(
                    action="open", op="start", stage=stage, text=text, kind=kind
                )
            return StageTransition(
                action="update", op="update", stage=self._current, text=text, kind=kind
            )
        if op.op == "next":
            # 规则 3：结束当前阶段，开一个新阶段。
            # 名字：模型给了就用模型的，否则用本条说明（text 非空：上面的早退保证了）。
            previous = self._current
            name = op.name or text
            stage = self._open(name)
            return StageTransition(
                action="open",
                op="next",
                stage=stage,
                previous=previous,
                text=text,
                kind=kind,
            )
        if op.op == "start":
            # 规则 2：当前无阶段时才开；已有阶段**不新开**（等价 update）。
            if self._current is not None:
                return StageTransition(
                    action="update", op="update", stage=self._current, text=text, kind=kind
                )
            stage = self._open(op.name or text)
            return StageTransition(
                action="open", op="start", stage=stage, text=text, kind=kind
            )
        # 规则 4：op=update。当前有阶段 → 只更新当前说明；当前没有阶段 →
        # 安全降级为隐式开阶段（与「没有 stage 操作」同一条路径），不丢模型说明。
        if self._current is None:
            stage = self._open(text)
            return StageTransition(
                action="open", op="start", stage=stage, text=text, kind=kind
            )
        return StageTransition(
            action="update", op="update", stage=self._current, text=text, kind=kind
        )

    def close(self) -> StageTransition | None:
        """结束当前阶段（turn 收尾）：没有阶段时返回 None。"""
        current = self._current
        if current is None:
            return None
        self._current = None
        done = replace(current, status=STAGE_DONE)
        return StageTransition(action="end", op="end", stage=done, previous=done)


class StageRegistry:
    """turn_id → StageTracker（进程级；一轮一份，结束即取走）。"""

    def __init__(self) -> None:
        self._trackers: dict[str, StageTracker] = {}

    def tracker(self, turn_id: str | None) -> StageTracker:
        key = str(turn_id or "")
        tracker = self._trackers.get(key)
        if tracker is None:
            tracker = StageTracker(turn_id)
            self._trackers[key] = tracker
        return tracker

    def get(self, turn_id: str | None) -> StageTracker | None:
        return self._trackers.get(str(turn_id or ""))

    def current_stage_id(self, turn_id: str | None) -> str | None:
        tracker = self.get(turn_id)
        return tracker.current.stage_id if tracker is not None and tracker.current else None

    def pop(self, turn_id: str | None) -> StageTracker | None:
        return self._trackers.pop(str(turn_id or ""), None)


def stage_raw(state: StageState | None, op: str) -> dict[str, Any] | None:
    """落库形状（plan §1.4）：messages.raw.stage。"""
    if state is None:
        return None
    return {
        "stage_id": state.stage_id,
        "index": state.index,
        "name": state.name,
        "op": op if op in STAGE_EVENT_OPS else "update",
        "status": state.status,
    }


def stage_event_payload(
    turn_id: str | None,
    transition: StageTransition,
    *,
    narrative_id: str | None = None,
    call_id: str | None = None,
    call_ids: list[str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """SSE STAGE 载荷（plan §1.3）。标识与状态全部来自系统，模型无法伪造。

    ``created_at`` 一定可用（ISO8601）：有落库行时由调用方给行的 created_at；
    系统合成的边界事件（op=end，以及任何没有落库行的 start/next）在这里补事件
    时刻 —— 与其它事件一样是 UTC ISO8601，绝不给 null。
    """
    state = transition.stage
    return {
        "turn_id": turn_id,
        "stage_id": state.stage_id if state is not None else None,
        "index": state.index if state is not None else 0,
        "status": state.status if state is not None else STAGE_DONE,
        "name": state.name if state is not None else "",
        "text": clean_text(transition.text, MAX_TEXT_CHARS),
        "kind": transition.kind,
        "op": transition.op,
        "narrative_id": narrative_id,
        "call_id": call_id,
        "call_ids": list(call_ids or []),
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }