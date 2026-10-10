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

本轮收紧（F01 / F03 / F04 / F05）
--------------------------------

* **F01：没有归属 ≠ 已证明执行者停止。** 升级前的旧版本**不写任何实例 / 心跳 /
  归属记录**（`6e073e9` 的 schema 里根本没有 `instances` / `record_owners`，
  `turn_journal` 也没有归属列），所以新版本**没有**任何库内证据能证明「写下这条
  开放行的旧进程已经停了」。因此无归属的开放行（`legacy_unowned` / `derived_legacy`）
  一律**只列出、不提供会改变运行状态的动作**，并给出原因；只有用户显式确认
  「旧执行者已停止」（`confirm_stopped`，写在 settings 里的持久确认）之后才允许
  继续 / 忽略 / 重排。**不假装旧版本会遵守新版本新加的锁或字段**。
* **F03：派发失败不能把后继丢在没人管的地方。** `claim_for_resend` 成功、`submit`
  失败时，后继行会被改成显式的 `dispatch_failed` 状态：同一个后继（同一个 turn_id）
  能在当前运行中重试，不需要重启、也不会产生第二个后继。
* **F04：一致的 owner 判定 + 条件写入。** repair / continue / ignore / requeue 共用
  同一套 owner 规则；写入本身带 owner 条件（`IFNULL(owner_instance_id,'')`），
  活 owner 或判不出来时一行都不改。
* **F05：读失败不是空清单。** turn / derived 任一来源读不动就抛
  `RecoveryReadError`（路由回 503），绝不返回「200 + 空清单」。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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
# F03：后继已经落库、但派发没有做成。它不是「正在执行」，也不是「未知归属」——
# 它是本实例自己制造出来的、可原地重试的明确失败状态（同一个 turn_id 重试）。
STATE_DISPATCH_FAILED = "dispatch_failed"

ALL_STATES: tuple[str, ...] = (
    STATE_READY,
    STATE_ORPHAN,
    STATE_LEGACY_UNOWNED,
    STATE_OWNER_UNKNOWN,
    STATE_DERIVED_STALE,
    STATE_DERIVED_LEGACY,
    STATE_DISPATCH_FAILED,
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
# F01：用户显式确认「写下这条无归属记录的旧执行者已经停止」。
# 它是唯一能把 legacy_unowned / derived_legacy 从「只可见」变成「可操作」的入口。
ACTION_CONFIRM_STOPPED = "confirm_stopped"

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
    # F01：旧版本不写归属，所以「没有归属」只说明**没有证据**，不说明执行者停了。
    OWNER_NONE: "这条记录来自没有实例归属的旧版本，无法确认它的执行者是否已经停止",
}

_OWNER_NOTE_CONFIRMED = "你已确认这条记录的旧执行者已经停止"

_TAKEOVER_BLOCKED = "无法确认上次的写入者是否已停止，暂时不能接管这条记录"
# F01：无归属开放行的统一说法（continue / ignore / repair / requeue 共用一句）。
_LEGACY_UNPROVEN = (
    "这条记录来自没有实例归属的旧版本，无法确认它的执行者是否已经停止；"
    "确认旧执行者已停止后才能操作（现在动它有重复执行的风险）"
)
_DERIVED_LEGACY_UNPROVEN = (
    "这条派生任务来自没有实例归属的旧版本，无法确认它的执行者是否已经停止；"
    "确认旧执行者已停止后才能重排（现在重排有重复执行的风险）"
)

# F03：派发失败时写进 reason 的前缀（同时是「这个后继可以原地重试」的判据）。
DISPATCH_FAILED_PREFIX = "dispatch_failed"

# F01：用户确认「旧执行者已停止」的持久键前缀（见 LegacyStopConfirmations）。
LEGACY_STOP_KEY_PREFIX = "recovery.legacy_stop."


class RecoveryReadError(RuntimeError):
    """恢复清单**读失败**：调用方必须回明确失败，绝不能当成「一条记录都没有」（F05）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_dispatch_failed(reason: Any) -> bool:
    return str(reason or "").startswith(DISPATCH_FAILED_PREFIX)


class LegacyStopConfirmations:
    """F01：把用户「旧执行者已经停止」的确认持久化。

    为什么复用 `settings` 表而不新加一张表：迁移号在并行修复分支之间是**命名空间**
    （见 docs/architecture.md §13.8），本轮基线停在 30；再占一个号既可能与并行分支
    撞号（撞号 = 后合入的那条被整段跳过），也会给补偿迁移（30）塞一个它建不出来的
    「必需对象」。`settings` 是现成的 key/value 表，界面侧只按固定前缀逐项读取，
    写在这里的运行期记录不会出现在设置页。

    读不到（表不存在 / 库坏了）一律当**未确认**：拿不到证据就不放行。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def key(kind: str, record_id: str) -> str:
        return f"{LEGACY_STOP_KEY_PREFIX}{kind}.{record_id}"

    def is_confirmed(self, kind: str, record_id: str) -> bool:
        try:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (self.key(kind, record_id),)
            ).fetchone()
        except sqlite3.Error as exc:  # noqa: BLE001 - 读不到 = 未确认（保守）
            logger.warning("legacy stop confirmation read failed: %s", exc)
            return False
        return row is not None

    def confirm(
        self,
        kind: str,
        record_id: str,
        *,
        instance_id: str | None = None,
        note: str = "",
    ) -> str:
        """记下一次确认，返回确认时刻（幂等：重复确认只覆盖记录，不产生第二条）。"""
        moment = _now_iso()
        payload = json.dumps(
            {"at": moment, "instance_id": instance_id, "note": str(note or "")[:200]},
            ensure_ascii=False,
        )
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (self.key(kind, record_id), payload, moment),
        )
        return moment


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
    # F01：这条记录的「旧执行者已停止」是不是已经被用户显式确认过。
    confirmed_stopped: bool = False
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
            "confirmed_stopped": bool(self.confirmed_stopped),
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
    status: Any,
    recovered_at: Any,
    recovered_by: Any,
    owner_state: str,
    reason: Any = None,
) -> str | None:
    """一条 turn_journal 行的分类；None = 不进清单。"""
    if str(status) == INTERRUPTED:
        if _is_dispatch_failed(reason) and not recovered_by:
            # F03：这条是「后继已落库、派发没做成」的行。它没有在跑（submit 失败
            # 意味着它没有进内存队列），所以它是一个明确、可操作的状态。
            return STATE_DISPATCH_FAILED
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


def _can_touch(owner_state: str, confirmed: bool) -> bool:
    """这条记录的归属是否**足以证明**执行者不会再动它（F01）。

    * `dead`：实例表里明确写了退出 / 心跳过期且 pid 不存在 —— 证明成立；
    * `none`：旧版本不写归属，库里**没有任何证据**；只有用户显式确认
      「旧执行者已停止」之后才算成立（`confirmed=True`）；
    * `alive` / `unknown`：一律不成立（一个在跑，一个判不出来）。
    """
    if owner_state == OWNER_DEAD:
        return True
    if owner_state == OWNER_NONE:
        return bool(confirmed)
    return False


def _confirm_action() -> RecoveryAction:
    """F01 的唯一解锁入口（只在「无归属且未确认」时出现）。"""
    return RecoveryAction(ACTION_CONFIRM_STOPPED, "确认旧执行者已停止", True)


def _turn_actions(
    state_class: str, owner_state: str, confirmed: bool = False
) -> tuple[RecoveryAction, ...]:
    if state_class == STATE_DISPATCH_FAILED:
        # 派发失败的后继：原文、父子关联、唯一后继语义都还在，重试的是**同一个**
        # turn_id（见 RecoveryInbox.take_over_and_continue）。
        return (
            RecoveryAction(ACTION_CONTINUE, "重新执行", True),
            RecoveryAction(ACTION_IGNORE, "知道了", True),
        )
    can_touch = _can_touch(owner_state, confirmed)
    unproven = owner_state == OWNER_NONE and not confirmed
    blocked_reason = _LEGACY_UNPROVEN if unproven else _TAKEOVER_BLOCKED
    if state_class == STATE_ORPHAN:
        actions = [
            RecoveryAction(
                ACTION_CONTINUE,
                "继续",
                False,
                "这条记录的重发关联不完整，先修复再继续",
            ),
            RecoveryAction(
                ACTION_REPAIR, "修复", can_touch, "" if can_touch else blocked_reason
            ),
            RecoveryAction(ACTION_IGNORE, "知道了", can_touch, "" if can_touch else blocked_reason),
        ]
        if unproven:
            actions.append(_confirm_action())
        return tuple(actions)
    if not can_touch:
        actions = [
            RecoveryAction(ACTION_CONTINUE, "继续", False, blocked_reason),
            RecoveryAction(ACTION_IGNORE, "知道了", False, blocked_reason),
        ]
        if unproven:
            actions.append(_confirm_action())
        return tuple(actions)
    return (
        RecoveryAction(ACTION_CONTINUE, "继续", True),
        RecoveryAction(ACTION_IGNORE, "知道了", True),
    )


def _derived_actions(owner_state: str, confirmed: bool = False) -> tuple[RecoveryAction, ...]:
    if owner_state == OWNER_DEAD:
        return (RecoveryAction(ACTION_REQUEUE, "重新排队", True),)
    if owner_state == OWNER_NONE:
        # F01：旧版本不写归属 = 没有证据，默认只可见。
        if confirmed:
            return (RecoveryAction(ACTION_REQUEUE, "重新排队", True),)
        return (
            RecoveryAction(ACTION_REQUEUE, "重新排队", False, _DERIVED_LEGACY_UNPROVEN),
            _confirm_action(),
        )
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
        # F01：「旧执行者已停止」的持久确认（读不到就按未确认处理）。
        self.confirmations = LegacyStopConfirmations(conn)
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
        failed: list[str] = []
        if kind_filter is None or KIND_USER_TURN in kind_filter:
            records.extend(self._read_source(KIND_USER_TURN, failed))
        if kind_filter is None or KIND_DERIVED_TASK in kind_filter:
            records.extend(self._read_source(KIND_DERIVED_TASK, failed))
        if failed:
            # F05：读失败绝不能伪装成「空清单」。清单是一个整体：少了任一来源，
            # `total` 就不再是「服务端匹配总量」，因此这里明确失败（路由回 503），
            # 前端保留上一次可见的内容并给出可重试原因。原因里写清**哪一个来源**
            # 没读出来（来源名 + 真实错误），别让用户自己猜。
            raise RecoveryReadError(
                "恢复清单读取失败（{}）：没有读出来，不代表没有记录".format(
                    "；".join(failed)
                )
            )
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

    def _read_source(self, kind: str, failed: list[str]) -> list[RecoveryRecord]:
        """读一个来源；失败时登记来源名并继续读另一个（由调用方决定整体失败）。"""
        try:
            if kind == KIND_USER_TURN:
                return self._turn_records()
            return self._derived_records()
        except RecoveryReadError as exc:
            logger.warning("recovery inbox %s read failed: %s", kind, exc)
            failed.append(f"{kind}：{exc}")
            return []

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
        except sqlite3.Error as exc:  # noqa: BLE001 - 读失败必须让调用方知道（F05）
            raise RecoveryReadError(f"turn_journal 读取失败：{exc}") from exc
        out: list[RecoveryRecord] = []
        for raw in rows:
            row = dict(raw)
            record_id = str(row.get("turn_id"))
            owner = _owner_of(
                self.registry, RECORD_TURN, record_id, row.get("owner_instance_id")
            )
            alive = _owner_alive(self.registry, owner)
            status = str(row.get("status") or "")
            dispatch_failed = str(status) == INTERRUPTED and _is_dispatch_failed(
                row.get("reason")
            )
            if alive is True and not dispatch_failed:
                # 活实例的记录不是恢复项：不进清单（也就永远不会被误操作）。
                # F03 的 dispatch_failed 后继是唯一例外：它**没有在跑**（派发失败 =
                # 没进内存队列），归属者就是刚刚失败的自己，必须可见、可重试。
                continue
            state = _owner_state(owner, alive)
            confirmed = state == OWNER_NONE and self.confirmations.is_confirmed(
                KIND_USER_TURN, record_id
            )
            state_class = _turn_class(
                status,
                row.get("recovered_at"),
                row.get("recovered_by"),
                state,
                row.get("reason"),
            )
            if state_class is None:
                continue
            note = _OWNER_NOTE_CONFIRMED if confirmed else _OWNER_NOTE.get(state, "")
            out.append(
                RecoveryRecord(
                    record_id=record_id,
                    kind=KIND_USER_TURN,
                    state_class=state_class,
                    status=status,
                    message=row.get("message"),
                    topic_id=row.get("topic_id"),
                    reason=row.get("reason"),
                    created_at=str(row.get("created_at") or ""),
                    updated_at=row.get("updated_at"),
                    owner_instance_id=owner,
                    owner_state=state,
                    owner_note=note,
                    confirmed_stopped=confirmed,
                    actions=_turn_actions(state_class, state, confirmed),
                )
            )
        return out

    def _derived_records(self) -> list[RecoveryRecord]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM derived_tasks WHERE state = ?",
                (derived_tasks.STATE_RUNNING,),
            ).fetchall()
        except sqlite3.Error as exc:  # noqa: BLE001 - 读失败必须让调用方知道（F05）
            raise RecoveryReadError(f"derived_tasks 读取失败：{exc}") from exc
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
            confirmed = state == OWNER_NONE and self.confirmations.is_confirmed(
                KIND_DERIVED_TASK, record_id
            )
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
                    owner_note=(
                        _OWNER_NOTE_CONFIRMED if confirmed else _OWNER_NOTE.get(state, "")
                    ),
                    claim_generation=(
                        int(row["claim_generation"]) if "claim_generation" in keys else 0
                    ),
                    attempts=int(row["attempts"]) if "attempts" in keys else None,
                    last_error=row.get("last_error"),
                    confirmed_stopped=confirmed,
                    actions=_derived_actions(state, confirmed),
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
        # F03：派发失败的后继 —— 原地重试**同一个** turn_id（不产生第二个后继）。
        # 这个分支必须在归属判定之前：它的归属者正是本实例（submit 失败的是我们），
        # 而它并没有在跑（submit 失败 = 没有进内存队列）。
        if str(status) == INTERRUPTED and _is_dispatch_failed(row.get("reason")):
            if expected_class and str(expected_class) != STATE_DISPATCH_FAILED:
                raise RecoveryConflict(
                    f"记录状态已经变化（现在是 {STATE_DISPATCH_FAILED}），请刷新后重试"
                )
            return self._retry_dispatch(row, topic_id)
        owner = _owner_of(
            self.registry, RECORD_TURN, str(record_id), row.get("owner_instance_id")
        )
        alive = _owner_alive(self.registry, owner)
        if alive is True:
            raise RecoveryConflict("上次的写入者现在还在运行，不能接管这条记录")
        state = _owner_state(owner, alive)
        confirmed = state == OWNER_NONE and self.confirmations.is_confirmed(
            KIND_USER_TURN, str(record_id)
        )
        state_class = _turn_class(
            status, row.get("recovered_at"), row.get("recovered_by"), state, row.get("reason")
        )
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
        if state == OWNER_NONE and not confirmed:
            # F01：旧版本不写归属，库里没有任何证据说明它的执行者已经停止。
            raise RecoveryConflict(_LEGACY_UNPROVEN)
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
        try:
            linked = self.journal.claim_for_resend(
                str(record_id), new_turn_id, self.instance_id
            )
        except BaseException:
            # F03：错误可能发生在**提交之前** —— 重发事务整体回滚了，但刚才那次接管
            # 是独立事务、已经落库。不退回去就会把这条记录藏起来（归属者还活着），
            # 用户在当前运行里再也重试不了。
            self._undo_takeover(record_id, row, status)
            raise
        if not linked:
            self._undo_takeover(record_id, row, status)
            raise RecoveryConflict(
                "已经是目标状态：这条记录已经被处理过（并发或重复点击）"
            )
        fresh = self._turn_row(record_id)
        message = str((fresh or row).get("message") or "")
        topic = topic_id if topic_id else (fresh or row).get("topic_id")
        try:
            turn = submitter(message, topic, new_turn_id)
        except Exception as exc:  # noqa: BLE001 - 派发失败必须可见（503）
            reason = self._failure_text(exc)
            # F03：后继已经落库（queued + 归属本实例）。如果不处理，它既不在内存
            # 队列里、又因为「归属者还活着」不进恢复清单 —— 用户只能等重启。
            # 把它改成显式的 dispatch_failed：同一个后继在当前运行中就能重试。
            self._mark_dispatch_failed(new_turn_id, reason)
            raise RecoveryDispatchError(reason) from exc
        return {
            "ok": True,
            "record_id": str(record_id),
            "turn_id": str(getattr(turn, "turn_id", new_turn_id) or new_turn_id),
            "status": str(getattr(turn, "status", "queued") or "queued"),
        }

    def _undo_takeover(self, record_id: str, row: dict[str, Any], status: str) -> None:
        """把一次已经落库、但后续步骤失败的接管退回原状（F03）。

        只有`queued`/`running`的开放行才被接管过；已经是 `interrupted` 的行不需要
        退回（它本来就在可恢复状态，没有任何东西被藏起来）。
        """
        if str(status) not in OPEN_STATUSES:
            return
        reverted = self.journal.release_takeover(
            str(record_id),
            status=str(status),
            reason=row.get("reason"),
            owner_instance_id=row.get("owner_instance_id"),
            instance_id=self.instance_id,
        )
        if not reverted:
            logger.warning(
                "takeover could not be rolled back after a failed resend: %s", record_id
            )

    # -- F03：派发失败的后继（原地重试同一个 turn_id） ----------------------

    def _failure_text(self, exc: BaseException) -> str:
        from agent.trace.redact import redact_text

        return redact_text(f"继续未被执行：{exc}")[:500]

    def _mark_dispatch_failed(self, turn_id: str, reason: str) -> None:
        """把「已落库但没有派发成功」的后继改成明确、可操作的失败状态。"""
        if not self.journal.mark_dispatch_failed(str(turn_id), reason):
            logger.warning(
                "dispatch failure could not be recorded on the successor: %s", turn_id
            )

    def _retry_dispatch(self, row: dict[str, Any], topic_id: str | None) -> dict[str, Any]:
        """重新派发**同一个**后继（F03：不创建第二个后继、也不静默宣称已执行）。"""
        record_id = str(row.get("turn_id"))
        submitter = self._resolve_submitter()
        if submitter is None:
            raise RecoveryDispatchError(
                "没有可用的执行通道，无法重新执行这条记录（记录没有被改动，可稍后重试）"
            )
        if not self.journal.requeue_dispatch_failed(record_id):
            fresh = self._turn_row(record_id)
            fresh_reason = (fresh or {}).get("reason")
            if fresh is None or not _is_dispatch_failed(fresh_reason):
                raise RecoveryConflict(
                    "已经是目标状态：这条记录已经被重新执行过（或状态已经变化），请刷新后重试"
                )
            raise RecoveryConflict("重新执行没有生效（状态已经变化），请刷新后重试")
        message = str(row.get("message") or "")
        topic = topic_id if topic_id else row.get("topic_id")
        try:
            turn = submitter(message, topic, record_id)
        except Exception as exc:  # noqa: BLE001 - 第二次派发失败同样必须可见
            reason = self._failure_text(exc)
            self._mark_dispatch_failed(record_id, reason)
            raise RecoveryDispatchError(reason) from exc
        return {
            "ok": True,
            "record_id": record_id,
            "turn_id": str(getattr(turn, "turn_id", record_id) or record_id),
            "status": str(getattr(turn, "status", "queued") or "queued"),
            "retried": True,
        }

    # -- F01：用户确认「旧执行者已停止」 ------------------------------------

    def confirm_stopped(
        self,
        record_id: str,
        *,
        note: str = "",
        expected_class: str | None = None,
    ) -> dict[str, Any]:
        """用户显式确认：写下这条无归属记录的旧执行者已经停止。

        这是`legacy_unowned` / `derived_legacy` 从「只可见」变成「可操作」的唯一
        入口（F01）。只对**当前确实无归属**的记录生效 —— 有归属（活着或判不出来）
        的记录不需要、也不允许靠它绕过 owner 判定。
        """
        row = self._turn_row(record_id)
        kind = KIND_USER_TURN
        if row is None:
            row = self._derived_row(record_id)
            kind = KIND_DERIVED_TASK
        if row is None:
            return _refuse("没有这条恢复记录")
        if kind == KIND_USER_TURN and bool(row.get("notify")):
            return _refuse("系统通知轮不是用户的消息，不能确认接管")
        owner = _owner_of(
            self.registry,
            RECORD_TURN if kind == KIND_USER_TURN else RECORD_DERIVED_TASK,
            str(record_id),
            row.get("owner_instance_id"),
        )
        alive = _owner_alive(self.registry, owner)
        if alive is True:
            return _refuse("这条记录现在有活着的归属者，不需要确认旧执行者已停止")
        state = _owner_state(owner, alive)
        if state != OWNER_NONE:
            return _refuse("这条记录有归属，确认「旧执行者已停止」不适用")
        if kind == KIND_USER_TURN:
            state_class = _turn_class(
                row.get("status"),
                row.get("recovered_at"),
                row.get("recovered_by"),
                state,
                row.get("reason"),
            )
            if state_class is None:
                return _refuse("已经是目标状态：这条记录已经不在可恢复状态")
            if expected_class and str(expected_class) != state_class:
                return _refuse(
                    f"记录状态已经变化（现在是 {state_class}），请刷新后重试"
                )
        elif str(row.get("state") or "") != derived_tasks.STATE_RUNNING:
            return _refuse("已经是目标状态：这条派生任务不在卡住状态")
        existing = self.confirmations.is_confirmed(kind, str(record_id))
        moment = self.confirmations.confirm(
            kind, str(record_id), instance_id=self.instance_id, note=note
        )
        return {
            "ok": True,
            "confirmed": True,
            "already_confirmed": bool(existing),
            "confirmed_at": moment,
            "record_id": str(record_id),
            "kind": kind,
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
        # F04：修复必须遵守 owner 状态（与 continue / ignore 同一套判定）。
        owner = _owner_of(
            self.registry, RECORD_TURN, str(record_id), row.get("owner_instance_id")
        )
        alive = _owner_alive(self.registry, owner)
        if alive is True:
            return _refuse("上次的写入者现在还在运行，不能修复这条记录", repaired=False)
        state = _owner_state(owner, alive)
        if state == OWNER_UNKNOWN:
            return _refuse(_TAKEOVER_BLOCKED, repaired=False)
        if state == OWNER_NONE and not self.confirmations.is_confirmed(
            KIND_USER_TURN, str(record_id)
        ):
            # F01：无归属 = 没有证据。修复会清掉抢占记录、把这条恢复成「可继续」，
            # 所以它同样属于「可能改变运行状态的动作」。
            return _refuse(_LEGACY_UNPROVEN, repaired=False)
        if not self.journal.repair_orphan(
            str(record_id), self.instance_id, owner_key=owner or ""
        ):
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
        # F03：派发失败的后继没有在跑（归属者是自己），直接按既有终态语义忽略。
        # 必须在归属判定之前处理，否则会被「归属者还活着」挡住。
        if str(row.get("status") or "") == INTERRUPTED and _is_dispatch_failed(
            row.get("reason")
        ):
            if expected_class and str(expected_class) != STATE_DISPATCH_FAILED:
                return _refuse(
                    f"记录状态已经变化（现在是 {STATE_DISPATCH_FAILED}），请刷新后重试",
                    ignored=False,
                )
            return self._dismiss_row(row)
        owner = _owner_of(
            self.registry, RECORD_TURN, str(record_id), row.get("owner_instance_id")
        )
        alive = _owner_alive(self.registry, owner)
        if alive is True:
            return _refuse("上次的写入者现在还在运行，不能标记为「知道了」", ignored=False)
        state = _owner_state(owner, alive)
        confirmed = state == OWNER_NONE and self.confirmations.is_confirmed(
            KIND_USER_TURN, str(record_id)
        )
        state_class = _turn_class(
            row.get("status"),
            row.get("recovered_at"),
            row.get("recovered_by"),
            state,
            row.get("reason"),
        )
        if state_class is None:
            return _refuse("已经是目标状态：这条记录不在可恢复状态", ignored=False)
        if expected_class and str(expected_class) != state_class:
            return _refuse(
                f"记录状态已经变化（现在是 {state_class}），请刷新后重试", ignored=False
            )
        if state == OWNER_UNKNOWN:
            return _refuse(_TAKEOVER_BLOCKED, ignored=False)
        if state == OWNER_NONE and not confirmed:
            # F01：忽略同样会让用户以为「它不会跑」，而旧执行者可能照跑不误。
            return _refuse(_LEGACY_UNPROVEN, ignored=False)

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
            # F04：这里与 repair 端点一样带 owner 条件（归属列与权威归属不一致时
            # 命中 0 行）。清不掉也不要紧：下面那次条件 dismiss 会如实拒绝。
            self.journal.repair_orphan(
                str(record_id), self.instance_id, owner_key=owner or ""
            )

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
        if owner:
            if alive is None:
                return _refuse("无法确认这条派生任务原本的执行者是否已停止，暂时不能重排")
            if alive is not False:  # pragma: no cover - _owner_alive 只返回三态
                return _refuse("无法确认这条派生任务原本的执行者是否已停止，暂时不能重排")
        else:
            # F01：旧版本不写归属 = 库里没有证据证明执行者停了（与 turn 一致）。
            if not self.confirmations.is_confirmed(KIND_DERIVED_TASK, str(record_id)):
                return _refuse(_DERIVED_LEGACY_UNPROVEN)
        generation = int(row.get("claim_generation") or 0) if "claim_generation" in keys else 0
        if expected_generation is not None and int(expected_generation) != generation:
            return _refuse("认领代次已经变化，请刷新后重试")
        if not derived_tasks.requeue_running(
            self.conn,
            str(record_id),
            expected_state=state,
            expected_generation=generation,
            instance_id=self.instance_id,
            owner_key=owner or "",
        ):
            return _refuse("重排没有生效（状态已经变化），请刷新后重试")
        return {
            "ok": True,
            "state": derived_tasks.STATE_PENDING,
            "record_id": str(record_id),
        }

    # -- internals --------------------------------------------------------

    def _dismiss_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """把一条**确定没有在跑**的行标成用户已忽略（F03 的 dispatch_failed 后继）。"""
        record_id = str(row.get("turn_id"))
        if not self.journal.dismiss(record_id):
            fresh = self._turn_row(record_id)
            if fresh is not None and str(fresh.get("status")) == DISMISSED:
                return _refuse("已经是目标状态：这条记录已经被忽略过了", ignored=False)
            return _refuse("标记「知道了」没有生效（状态已经变化），请刷新后重试", ignored=False)
        return {"ok": True, "ignored": True, "record_id": record_id}

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
