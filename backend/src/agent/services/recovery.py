"""A01 + A03：可恢复记录收件箱（后端服务层）。

要解决的问题
------------

进程退出（崩溃、被强杀、升级重启）会留下一批「被接受过、但没有走到终态」的
用户消息与派生任务。它们分散在三个地方，口径各不相同：

* `turn_journal` 里 `queued` / `running`：**升级前**写入的历史行没有任何归属，
  `interrupt_stale()` 对无归属的行一律保守保留（不改状态）—— 于是它们既不在
  `unfinished()` 里（那要求 `interrupted`），也不能重发：消息原文在库里，
  用户在界面上永远点不到（A01 的缺陷，基线已用受控证据复现）；
* `turn_journal` 里 `recovered_at` 非空、`recovered_by` 空：抢占过但没有后继的
  孤儿重发记录，`unfinished()` 同样看不到（A03 的缺陷）；
* `derived_tasks` 里卡在 `running`、归属者已确认退出（或根本没有归属）的派生任务。

本模块把这些状态**分类表达**成一份只读清单（`list_records`），并给出严格的、
用户明确触发的动作（继续 / 修复 / 知道了 / 重排）。三条纪律贯穿全文：

1. **只读清单**：`list_records()` 不改任何状态，unknown 绝不被当成「死」或
   批量改成 `interrupted`；
2. **动不了就如实说**：判不出来的记录照旧列出，但动作 `enabled=False` +
   一句给用户看的原因（前端不必猜为什么按钮点不动）；
3. **复用既有可靠路径**：继续/修复都走 `TurnJournal.claim_for_resend()` /
   `repair_orphan()`，不另写一套派发；重复与并发只允许一个后继。

状态分类（`state_class`，与契约 §2.1 冻结一致）
----------------------------------------------

============================  ==================================================
`ready`                       已确认可恢复：`interrupted` 且 `recovered_at` 为空，
                              或 `queued`/`running` 但归属者已确认退出
`orphaned_claim`              `interrupted` + `recovered_at` 非空 + `recovered_by` 空
`legacy_unowned`              `queued`/`running` 且没有任何归属（升级前的历史行）
`owner_unknown`               有归属但存活判不出来（unknown 不改状态）
`derived_stale`               `running` 派生任务且归属者确认已退出
`derived_legacy`              `running` 派生任务但无归属
============================  ==================================================

**活实例的记录不进清单**：归属者确认还活着的 `queued`/`running` 行是「正在做的
事」，不是待用户处理的恢复项；把它们列出来只会诱导用户去抢一个正在执行的 turn。
已真正重发（`recovered_by` 指向后继）、已完成、系统通知轮（`notify = 1`）、
用户已忽略（`dismissed`）的记录也一律不进清单。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from agent.services import derived_tasks
from agent.storage.instance_registry import RECORD_DERIVED_TASK, RECORD_TURN
from agent.storage.turn_journal import (
    DISMISSED,
    INTERRUPTED,
    OPEN_STATUSES,
    TurnJournal,
)

logger = logging.getLogger(__name__)

# -- 状态分类（冻结字面量：前端按它决定显示与动作） -------------------------

STATE_READY = "ready"
STATE_ORPHAN = "orphaned_claim"
STATE_LEGACY_UNOWNED = "legacy_unowned"
STATE_OWNER_UNKNOWN = "owner_unknown"
STATE_DERIVED_STALE = "derived_stale"
STATE_DERIVED_LEGACY = "derived_legacy"

ALL_STATES: tuple[str, ...] = (
    STATE_READY,
    STATE_ORPHAN,
    STATE_LEGACY_UNOWNED,
    STATE_OWNER_UNKNOWN,
    STATE_DERIVED_STALE,
    STATE_DERIVED_LEGACY,
)

# -- 记录种类 ---------------------------------------------------------------

KIND_USER_TURN = "user_turn"
KIND_DERIVED_TASK = "derived_task"
ALL_KINDS: tuple[str, ...] = (KIND_USER_TURN, KIND_DERIVED_TASK)

# -- 动作（`RecoveryAction.id` 的取值） -------------------------------------

ACTION_CONTINUE = "continue"
ACTION_REPAIR = "repair"
ACTION_IGNORE = "ignore"
ACTION_REQUEUE = "requeue"

# 归属者状态（`RecoveryRecord.owner_state`）
OWNER_ALIVE = "alive"
OWNER_DEAD = "dead"
OWNER_UNKNOWN = "unknown"
OWNER_NONE = "none"

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

_OWNER_NOTE = {
    OWNER_ALIVE: "上次的写入者现在还在运行",
    OWNER_DEAD: "上次的写入者已确认退出",
    OWNER_UNKNOWN: "无法确认上次的写入者是否已停止",
    OWNER_NONE: "这条记录来自升级前的版本，没有写入者归属",
}

_TAKEOVER_BLOCKED = "无法确认上次的写入者是否已停止，暂时不能接管这条记录"


class RecoveryConflict(RuntimeError):
    """这次操作与记录的当前状态冲突：**一行都不改**，调用方应回 409。"""


class RecoveryDispatchError(RuntimeError):
    """继续这个动作没有做成（派发通道不可用 / 新 turn 没有被接受）。

    和 server.py 的重发接口同一口径：宁可明确失败，也不返回一个假的 200 ——
    上层据此回 503，并如实告诉用户「这条消息没有执行」。
    """


@dataclass(frozen=True)
class RecoveryAction:
    """一个可用的动作：`enabled=False` 时 `reason` 必须能直接给用户看。"""

    id: str
    label: str
    enabled: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "enabled": bool(self.enabled),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RecoveryRecord:
    record_id: str
    kind: str
    state_class: str
    status: str
    message: str | None
    topic_id: str | None
    reason: str | None
    created_at: str
    updated_at: str | None
    owner_instance_id: str | None
    owner_state: str
    owner_note: str
    claim_generation: int | None = None
    attempts: int | None = None
    last_error: str | None = None
    actions: tuple[RecoveryAction, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "state_class": self.state_class,
            "status": self.status,
            "message": self.message,
            "topic_id": self.topic_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "owner_instance_id": self.owner_instance_id,
            "owner_state": self.owner_state,
            "owner_note": self.owner_note,
            "claim_generation": self.claim_generation,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True)
class RecoveryListing:
    records: tuple[RecoveryRecord, ...]
    total: int
    shown: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [record.to_dict() for record in self.records],
            "total": int(self.total),
            "shown": int(self.shown),
            "truncated": bool(self.truncated),
        }


# ---------------------------------------------------------------------------
# 归属判定（duck-typed：registry 只要有 owner_alive / owner_instance_id 即可）
# ---------------------------------------------------------------------------


def _owner_alive(registry: Any, instance_id: str | None) -> bool | None:
    """True / False / None(unknown)。None 绝不能被调用方当成「死」。"""
    if not instance_id or registry is None:
        return None
    checker = getattr(registry, "owner_alive", None)
    if checker is None:
        if callable(registry):
            checker = registry
        else:
            return None
    try:
        result = checker(str(instance_id))
    except Exception:  # noqa: BLE001 - 判不出来就是 unknown
        return None
    if result is True:
        return True
    if result is False:
        return False
    return None


def _owner_of(
    registry: Any, record_type: str, record_id: str, column_value: Any
) -> str | None:
    """这条记录的归属者：优先归属表（权威），退回行上的归属列。"""
    if registry is not None:
        getter = getattr(registry, "owner_instance_id", None)
        if getter is not None:
            try:
                found = getter(str(record_type), str(record_id))
            except Exception:  # noqa: BLE001 - 归属表读不到就退回列值
                found = None
            if found:
                return str(found)
    return str(column_value) if column_value else None


def _owner_state(owner: str | None, alive: bool | None) -> str:
    if not owner:
        return OWNER_NONE
    if alive is True:
        return OWNER_ALIVE
    if alive is False:
        return OWNER_DEAD
    return OWNER_UNKNOWN


def _turn_class(
    status: Any, recovered_at: Any, recovered_by: Any, owner_state: str
) -> str | None:
    """一条 turn_journal 行的分类；None = 不进清单。"""
    if str(status) == INTERRUPTED:
        if recovered_at is None:
            return STATE_READY
        if not recovered_by:
            return STATE_ORPHAN
        return None  # 已经真正重发过：不需要用户再处理
    if str(status) in OPEN_STATUSES:
        if owner_state == OWNER_NONE:
            return STATE_LEGACY_UNOWNED
        if owner_state == OWNER_DEAD:
            # 归属者确认退出 = 已确认可恢复（只是还没被恢复循环轮到）。
            return STATE_READY
        return STATE_OWNER_UNKNOWN
    return None


def _turn_actions(state_class: str, owner_state: str) -> tuple[RecoveryAction, ...]:
    can_touch = owner_state in (OWNER_NONE, OWNER_DEAD)
    if state_class == STATE_ORPHAN:
        return (
            RecoveryAction(
                ACTION_CONTINUE,
                "继续",
                False,
                "这条记录的重发关联不完整，先修复再继续",
            ),
            RecoveryAction(ACTION_REPAIR, "修复", True),
            RecoveryAction(
                ACTION_IGNORE,
                "知道了",
                can_touch,
                "" if can_touch else _TAKEOVER_BLOCKED,
            ),
        )
    if not can_touch:
        return (
            RecoveryAction(ACTION_CONTINUE, "继续", False, _TAKEOVER_BLOCKED),
            RecoveryAction(ACTION_IGNORE, "知道了", False, _TAKEOVER_BLOCKED),
        )
    return (
        RecoveryAction(ACTION_CONTINUE, "继续", True),
        RecoveryAction(ACTION_IGNORE, "知道了", True),
    )


def _derived_actions(owner_state: str) -> tuple[RecoveryAction, ...]:
    if owner_state in (OWNER_NONE, OWNER_DEAD):
        return (RecoveryAction(ACTION_REQUEUE, "重新排队", True),)
    return (
        RecoveryAction(
            ACTION_REQUEUE,
            "重新排队",
            False,
            "无法确认这条派生任务原本的执行者是否已停止",
        ),
    )


def _normalise(values: Iterable[str] | str | None, allowed: Sequence[str]) -> set[str] | None:
    """把 `kinds` / `classes` 参数规整成集合；未知取值被忽略。"""
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    chosen = {str(value).strip() for value in values if str(value).strip()}
    if not chosen:
        return None
    return chosen & set(allowed)


class RecoveryInbox:
    """可恢复记录的只读清单 + 用户明确触发的动作。

    `conn` 是库连接；`registry` 是实例归属判定（`InstanceRegistry`，也接受任何
    提供 `owner_alive` / `owner_instance_id` 的对象，测试可用最小替身）。
    `turns` / `submitter` 只在「继续」时需要：没有派发通道时继续会明确失败
    （`RecoveryDispatchError`），绝不假装成功。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: Any = None,
        *,
        turns: Any = None,
        instance_id: str | None = None,
        submitter: Callable[[str, str | None, str], Any] | None = None,
        journal: Any = None,
    ) -> None:
        self.conn = conn
        self.registry = registry
        self.turns = turns
        self.instance_id = instance_id or getattr(registry, "instance_id", None)
        self._submitter = submitter
        self.journal = journal or TurnJournal(
            conn, instance_id=self.instance_id, registry=registry
        )

    # -- 清单（只读） ------------------------------------------------------

    def list_records(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
        kinds: Iterable[str] | str | None = None,
        classes: Iterable[str] | str | None = None,
    ) -> RecoveryListing:
        """当前所有需要用户处理的记录（只读；不改任何状态、不批量改 unknown）。"""
        kind_filter = _normalise(kinds, ALL_KINDS)
        class_filter = _normalise(classes, ALL_STATES)
        records: list[RecoveryRecord] = []
        if kind_filter is None or KIND_USER_TURN in kind_filter:
            records.extend(self._turn_records())
        if kind_filter is None or KIND_DERIVED_TASK in kind_filter:
            records.extend(self._derived_records())
        if class_filter is not None:
            records = [record for record in records if record.state_class in class_filter]
        # 新的在前：用户最关心「最近那次没跑完的」。
        records.sort(key=lambda item: (str(item.created_at or ""), item.record_id), reverse=True)
        total = len(records)
        try:
            limit_value = int(limit)
        except (TypeError, ValueError):
            limit_value = DEFAULT_LIMIT
        limit_value = max(1, min(limit_value, MAX_LIMIT))
        shown = records[:limit_value]
        return RecoveryListing(
            records=tuple(shown),
            total=total,
            shown=len(shown),
            truncated=total > len(shown),
        )

    def _turn_records(self) -> list[RecoveryRecord]:
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        sql = (
            "SELECT * FROM turn_journal WHERE notify = 0 AND ("
            " (status = ? AND recovered_at IS NULL)"
            " OR (status = ? AND recovered_at IS NOT NULL"
            "     AND (recovered_by IS NULL OR recovered_by = ''))"
            f" OR status IN ({placeholders}))"
        )
        params: list[Any] = [INTERRUPTED, INTERRUPTED, *OPEN_STATUSES]
        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:  # noqa: BLE001 - 读不到就当没有（不猜状态）
            logger.warning("recovery inbox turn read failed: %s", exc)
            return []
        out: list[RecoveryRecord] = []
        for raw in rows:
            row = dict(raw)
            record_id = str(row.get("turn_id"))
            owner = _owner_of(
                self.registry, RECORD_TURN, record_id, row.get("owner_instance_id")
            )
            alive = _owner_alive(self.registry, owner)
            if alive is True:
                # 活实例正在做的记录不是恢复项：不进清单（也就永远不会被误操作）。
                continue
            state = _owner_state(owner, alive)
            state_class = _turn_class(
                row.get("status"), row.get("recovered_at"), row.get("recovered_by"), state
            )
            if state_class is None:
                continue
            out.append(
                RecoveryRecord(
                    record_id=record_id,
                    kind=KIND_USER_TURN,
                    state_class=state_class,
                    status=str(row.get("status") or ""),
                    message=row.get("message"),
                    topic_id=row.get("topic_id"),
                    reason=row.get("reason"),
                    created_at=str(row.get("created_at") or ""),
                    updated_at=row.get("updated_at"),
                    owner_instance_id=owner,
                    owner_state=state,
                    owner_note=_OWNER_NOTE.get(state, ""),
                    actions=_turn_actions(state_class, state),
                )
            )
        return out

    def _derived_records(self) -> list[RecoveryRecord]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM derived_tasks WHERE state = ?",
                (derived_tasks.STATE_RUNNING,),
            ).fetchall()
        except sqlite3.Error as exc:  # noqa: BLE001 - 读不到就当没有
            logger.warning("recovery inbox derived read failed: %s", exc)
            return []
        out: list[RecoveryRecord] = []
        for raw in rows:
            row = dict(raw)
            keys = set(row.keys())
            record_id = str(row.get("id"))
            owner = _owner_of(
                self.registry,
                RECORD_DERIVED_TASK,
                record_id,
                row.get("owner_instance_id") if "owner_instance_id" in keys else None,
            )
            alive = _owner_alive(self.registry, owner)
            if alive is True:
                continue
            state = _owner_state(owner, alive)
            state_class = (
                STATE_DERIVED_STALE if state == OWNER_DEAD else STATE_DERIVED_LEGACY
            )
            out.append(
                RecoveryRecord(
                    record_id=record_id,
                    kind=KIND_DERIVED_TASK,
                    state_class=state_class,
                    status=str(row.get("state") or ""),
                    message=None,
                    topic_id=None,
                    reason=None,
                    created_at=str(row.get("created_at") or ""),
                    updated_at=row.get("updated_at"),
                    owner_instance_id=owner,
                    owner_state=state,
                    owner_note=_OWNER_NOTE.get(state, ""),
                    claim_generation=(
                        int(row["claim_generation"]) if "claim_generation" in keys else 0
                    ),
                    attempts=int(row["attempts"]) if "attempts" in keys else None,
                    last_error=row.get("last_error"),
                    actions=_derived_actions(state),
                )
            )
        return out

    # -- 继续（接管 + 既有可靠重发） ---------------------------------------

    def take_over_and_continue(
        self,
        record_id: str,
        *,
        expected_class: str | None = None,
        expected_status: str | None = None,
        topic_id: str | None = None,
    ) -> dict[str, Any]:
        """用户确认「继续」这条没有执行完的消息。

        顺序固定（契约 §2.1）：

        1. 校验客户端看到的状态（`expected_status` / `expected_class`）还是当前状态；
        2. `queued` / `running` 的开放行先做**接管**（`TurnJournal.take_over`：
           `BEGIN IMMEDIATE` + 条件 UPDATE，只认「无归属」，或归属者已确认退出）；
           已经是 `interrupted` 的行本来就是接管后的目标状态，直接进入第 3 步
           （不重写 `reason`，否则会覆盖「为什么这条没有执行」的原因）；
        3. 沿用 `TurnJournal.claim_for_resend()`：老记录 + 新记录 + 关联在一个事务里，
           抢不到就冲突 —— 所以重复点击 / 并发只会产生一个后继；
        4. 最后才 submit（失败则 `RecoveryDispatchError` → HTTP 503，绝不说「已继续」）。
        """
        row = self._turn_row(record_id)
        if row is None or bool(row.get("notify")):
            raise RecoveryConflict("没有这条用户消息记录（或它不是用户的消息）")
        status = str(row.get("status") or "")
        if expected_status and str(expected_status) != status:
            raise RecoveryConflict(
                f"已经是目标状态：这条记录现在是 {status}，不是 {expected_status}，请刷新后重试"
            )
        owner = _owner_of(
            self.registry, RECORD_TURN, str(record_id), row.get("owner_instance_id")
        )
        alive = _owner_alive(self.registry, owner)
        if alive is True:
            raise RecoveryConflict("上次的写入者现在还在运行，不能接管这条记录")
        state = _owner_state(owner, alive)
        state_class = _turn_class(status, row.get("recovered_at"), row.get("recovered_by"), state)
        if state_class is None:
            raise RecoveryConflict(
                "已经是目标状态：这条记录已经不在可恢复状态"
                "（可能已经执行完成、已经被处理过，或已经被忽略）"
            )
        if expected_class and str(expected_class) != state_class:
            raise RecoveryConflict(
                f"记录状态已经变化（现在是 {state_class}），请刷新后重试"
            )
        if state == OWNER_UNKNOWN:
            raise RecoveryConflict(_TAKEOVER_BLOCKED)
        if state_class == STATE_ORPHAN:
            raise RecoveryConflict("这条记录的重发关联不完整，请先修复再继续")

        # 派发通道先确认（在任何写入之前）：没有通道就不要动这条记录 —— 否则
        # 「已抢占」却没地方执行，记录反而会从清单里消失。
        submitter = self._resolve_submitter()
        if submitter is None:
            raise RecoveryDispatchError(
                "没有可用的执行通道，无法继续这条记录（记录没有被改动，可稍后重试）"
            )

        if status in OPEN_STATUSES:
            if not self.journal.take_over(
                str(record_id),
                expected_status=status,
                instance_id=self.instance_id,
                dead_owner=owner if state == OWNER_DEAD else None,
            ):
                fresh = self._turn_row(record_id)
                now_status = str(fresh.get("status")) if fresh else "未知"
                raise RecoveryConflict(
                    f"已经是目标状态：接管没有命中（这条记录现在是 {now_status}），请刷新后重试"
                )

        new_turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        linked = self.journal.claim_for_resend(str(record_id), new_turn_id, self.instance_id)
        if not linked:
            raise RecoveryConflict(
                "已经是目标状态：这条记录已经被处理过（并发或重复点击）"
            )
        fresh = self._turn_row(record_id)
        message = str((fresh or row).get("message") or "")
        topic = topic_id if topic_id else (fresh or row).get("topic_id")
        try:
            turn = submitter(message, topic, new_turn_id)
        except Exception as exc:  # noqa: BLE001 - 派发失败必须可见（503）
            from agent.trace.redact import redact_text

            raise RecoveryDispatchError(
                redact_text(f"继续未被执行：{exc}")[:500]
            ) from exc
        return {
            "ok": True,
            "record_id": str(record_id),
            "turn_id": str(getattr(turn, "turn_id", new_turn_id) or new_turn_id),
            "status": str(getattr(turn, "status", "queued") or "queued"),
        }

    # -- 修复孤立重发（A03） ----------------------------------------------

    def repair_orphan(
        self, record_id: str, *, expected_class: str | None = None
    ) -> dict[str, Any]:
        """让「抢占过但没有后继」的记录重新可继续 / 可忽略。

        只作用于这一种精确状态（`TurnJournal.repair_orphan` 的条件 UPDATE 与
        这里的前置判定一致）。已经真正重发过的行必须拒绝：否则一条消息会被执行两次。
        修复本身不改消息原文、不新建 turn。
        """
        if expected_class and str(expected_class) != STATE_ORPHAN:
            return _refuse("只有孤立重发记录（orphaned_claim）需要修复", repaired=False)
        row = self._turn_row(record_id)
        if row is None or bool(row.get("notify")):
            return _refuse("没有这条用户消息记录（或它不是用户的消息）", repaired=False)
        if row.get("recovered_by"):
            return _refuse(
                "这条记录已经真正重发过（有关联的后继），不能再修复", repaired=False
            )
        if str(row.get("status") or "") != INTERRUPTED:
            return _refuse(
                f"这条记录现在不是 interrupted（{row.get('status')}），不需要修复",
                repaired=False,
            )
        if row.get("recovered_at") is None:
            return _refuse("这条记录没有被抢占过，不需要修复", repaired=False)
        if not self.journal.repair_orphan(str(record_id), self.instance_id):
            return _refuse("修复没有生效（状态已经变化），请刷新后重试", repaired=False)
        return {"ok": True, "repaired": True, "record_id": str(record_id)}

    # -- 忽略（用户已知晓） ------------------------------------------------

    def ignore(self, record_id: str, *, expected_class: str | None = None) -> dict[str, Any]:
        """用户确认「知道了」：不再提示，但原文保留、不产生后继。

        开放状态（`queued` / `running`）的行先按接管规则接管（**只为无归属 /
        归属者已确认退出**的行），孤儿行先清掉那次没收尾的抢占，最后走既有
        `TurnJournal.dismiss` 语义（写成 `dismissed` 终态）。

        判不出来归属（unknown）的行拒绝：它可能正在被执行，标成「知道了」
        会让用户以为它不会跑，而实际上它可能照跑不误。
        """
        row = self._turn_row(record_id)
        if row is None or bool(row.get("notify")):
            return _refuse("没有这条用户消息记录（或它不是用户的消息）", ignored=False)
        if str(row.get("status") or "") == DISMISSED:
            return _refuse("已经是目标状态：这条记录已经被忽略过了", ignored=False)
        owner = _owner_of(
            self.registry, RECORD_TURN, str(record_id), row.get("owner_instance_id")
        )
        alive = _owner_alive(self.registry, owner)
        if alive is True:
            return _refuse("上次的写入者现在还在运行，不能标记为「知道了」", ignored=False)
        state = _owner_state(owner, alive)
        state_class = _turn_class(
            row.get("status"), row.get("recovered_at"), row.get("recovered_by"), state
        )
        if state_class is None:
            return _refuse("已经是目标状态：这条记录不在可恢复状态", ignored=False)
        if expected_class and str(expected_class) != state_class:
            return _refuse(
                f"记录状态已经变化（现在是 {state_class}），请刷新后重试", ignored=False
            )
        if state == OWNER_UNKNOWN:
            return _refuse(_TAKEOVER_BLOCKED, ignored=False)

        if str(row.get("status") or "") in OPEN_STATUSES:
            if not self.journal.take_over(
                str(record_id),
                expected_status=str(row.get("status")),
                instance_id=self.instance_id,
                dead_owner=owner if state == OWNER_DEAD else None,
            ):
                fresh = self._turn_row(record_id)
                if fresh is not None and str(fresh.get("status")) == DISMISSED:
                    return _refuse("已经是目标状态：这条记录已经被忽略过了", ignored=False)
                return _refuse("接管没有命中（状态已经变化），请刷新后重试", ignored=False)
        elif state_class == STATE_ORPHAN:
            # 孤儿：先清掉那次没收尾的抢占，才可能走到 dismiss。
            self.journal.repair_orphan(str(record_id), self.instance_id)

        if not self.journal.dismiss(str(record_id)):
            fresh = self._turn_row(record_id)
            if fresh is not None and str(fresh.get("status")) == DISMISSED:
                return _refuse("已经是目标状态：这条记录已经被忽略过了", ignored=False)
            return _refuse(
                "标记「知道了」没有生效（状态已经变化），请刷新后重试", ignored=False
            )
        return {"ok": True, "ignored": True, "record_id": str(record_id)}

    # -- 重排派生任务 ------------------------------------------------------

    def requeue_derived(
        self,
        record_id: str,
        *,
        expected_state: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        """把一条卡住的 `running` 派生任务交回队列（用户明确触发）。

        条件更新 + `claim_generation + 1`（见 `derived_tasks.requeue_running`）：
        执行者还活着时拒绝；归属判不出来时也拒绝（它可能正在跑，重排会让同一件
        事做两遍 —— 代次只能挡住迟到写回，挡不住「同时做两遍」）。
        `attempts` / `last_error` 原样保留。
        """
        row = self._derived_row(record_id)
        if row is None:
            return _refuse("没有这条派生任务")
        keys = set(row.keys())
        state = str(row.get("state") or "")
        if state == derived_tasks.STATE_COMPLETED:
            return _refuse("这条派生任务已经完成，不能重排")
        if state == derived_tasks.STATE_PENDING:
            return _refuse("已经是目标状态：这条派生任务已经在队列里了")
        if expected_state and str(expected_state) != state:
            return _refuse(f"任务状态已经变化（现在是 {state}），请刷新后重试")
        owner = _owner_of(
            self.registry,
            RECORD_DERIVED_TASK,
            str(record_id),
            row.get("owner_instance_id") if "owner_instance_id" in keys else None,
        )
        alive = _owner_alive(self.registry, owner)
        if alive is True:
            return _refuse("这条派生任务的执行者现在还在运行，不能重排")
        if owner and alive is None:
            return _refuse("无法确认这条派生任务原本的执行者是否已停止，暂时不能重排")
        generation = int(row.get("claim_generation") or 0) if "claim_generation" in keys else 0
        if expected_generation is not None and int(expected_generation) != generation:
            return _refuse("认领代次已经变化，请刷新后重试")
        if not derived_tasks.requeue_running(
            self.conn,
            str(record_id),
            expected_state=state,
            expected_generation=generation,
            instance_id=self.instance_id,
        ):
            return _refuse("重排没有生效（状态已经变化），请刷新后重试")
        return {
            "ok": True,
            "state": derived_tasks.STATE_PENDING,
            "record_id": str(record_id),
        }

    # -- internals --------------------------------------------------------

    def _resolve_submitter(self) -> Callable[[str, str | None, str], Any] | None:
        if self._submitter is not None:
            return self._submitter
        turns = self.turns
        if turns is None or not hasattr(turns, "submit"):
            return None

        def _submit(message: str, topic: str | None, turn_id: str) -> Any:
            return turns.submit(message, topic, turn_id=turn_id)

        return _submit

    def _turn_row(self, record_id: str) -> dict[str, Any] | None:
        try:
            row = self.conn.execute(
                "SELECT * FROM turn_journal WHERE turn_id = ?", (str(record_id),)
            ).fetchone()
        except sqlite3.Error as exc:  # noqa: BLE001 - 读不到就当没有这条记录
            logger.warning("recovery inbox record read failed: %s", exc)
            return None
        return dict(row) if row is not None else None

    def _derived_row(self, record_id: str) -> dict[str, Any] | None:
        try:
            row = self.conn.execute(
                "SELECT * FROM derived_tasks WHERE id = ?", (str(record_id),)
            ).fetchone()
        except sqlite3.Error as exc:  # noqa: BLE001
            logger.warning("recovery inbox derived record read failed: %s", exc)
            return None
        return dict(row) if row is not None else None


def _refuse(reason: str, **extra: Any) -> dict[str, Any]:
    """动作被拒绝的统一返回形状（上层据此回 409，且**一行都没改**）。"""
    payload: dict[str, Any] = {"ok": False, "conflict": True, "reason": reason}
    payload.update(extra)
    return payload
