"""阶段二 A：知识维护调度器（触发判定 / 快照 / 单轮互斥 / 失败退避）。

契约：docs/qio-refactor-contract.md §12.3（A 的调度契约）、§12.6（Lead 接线）。

职责只有这一条链：

1. note_user_turn_completed：真实成功用户 turn 完成后累计等待轮次（按 turn_id 去重；
   没有待处理变化时**不**累计）；通知轮 / 失败 / 取消轮由接线的 Lead 保证不进来。
2. pending_state：给状态展示 / 测试 / 周期循环读取的只读视图（含当前是否满足触发）。
3. maybe_run：按阈值触发一轮——原子 claim（running）→ 取本轮快照（MaintenanceSnapshot）
   → await 注入的 runner.run_batch(snapshot) → 成功推进处理位置 / 失败保留位置并退避；
   任何情况最后都释放 running。

显式边界（不做的事）：

* 不读写工具输出清理 / 工具记录保留 / 自动化建议的任何状态——那些仍由既有维护
  生命周期独立调度（契约 §12.6：三类调度分开）；
* 不 import B 的 runner 实现，只依赖 MaintenanceSnapshot / MaintenanceRunOutcome 与
  run_batch（测试注入 fake async runner）；
* 不调用模型、不做知识写入——只记录「哪儿处理到哪了」。

持久化语义（迁移 46）：

* knowledge_change_log.seq 是处理位置；last_processed_seq 只前进到本轮快照上界
  change_seq_max，因此快照期间新增的变化留给后续批次，不会被跳过；
* running / run_started_at 落库：进程重启后仍能判断「是否有一轮在跑」；run_started_at
  超过 RUN_STALE_SECONDS 视为上一轮被强杀 / 崩溃，回收 running 并计一次失败，但
  **不动**处理位置（下一轮重做这批变化）；
* 失败时 last_processed_seq 不动，failed_attempts + 1，retry_blocked_until_turn =
  当前 turn_seq + retry_cooldown_turns —— 退避期间不再调用 runner / 模型。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from agent.maintenance.models import (
    MAINTENANCE_LIMITS,
    MaintenanceSnapshot,
    MaintenanceTrigger,
)
from agent.storage.db import transaction
from agent.trace.redact import sanitize_error_text

logger = logging.getLogger(__name__)

#: 调度状态是单行表（迁移 46 里的 id = 1）。
STATE_ID = 1

# 配置键前缀与默认值（契约 §12.3 的六个键，全部有默认值；settings 可覆盖）。
_KEY_PREFIX = "knowledge_maintenance."
DEFAULT_ENABLED = True
DEFAULT_CHANGE_THRESHOLD = 20
DEFAULT_WAITING_TURNS = 30
DEFAULT_MAX_CHANGES_PER_RUN = 50
DEFAULT_RETRY_COOLDOWN_TURNS = 5
DEFAULT_CHECK_INTERVAL_SECONDS = 60

#: running 标记超过这个时长没有释放 = 上一轮维护已中断（进程崩溃 / 被强杀）。
#: 一轮维护最多含一次模型调用 + 一次独立回答，30 分钟是足够宽的上限；测试用注入
#: 时钟推进，不真实等待。
RUN_STALE_SECONDS = 30 * 60

_STALE_ERROR = "上一轮维护执行中断（running 标记超时回收），处理位置保持不变"

#: 状态表全部列（显式列出来，读路径不依赖 SELECT * 的列顺序）。
_STATE_COLUMNS = (
    "id",
    "turn_seq",
    "last_processed_seq",
    "batch_seq",
    "running",
    "run_started_at",
    "failed_attempts",
    "retry_blocked_until_turn",
    "last_error",
    "last_run_at",
    "updated_at",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_error(error: Any) -> str:
    """错误出口统一语义：先打码、再截断（复用 trace.redact 的出口约定）。"""
    if error is None:
        text = "维护执行失败（未提供原因）"
    elif isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error) or "维护执行失败（未提供原因）"
    cleaned = sanitize_error_text(text, limit=500).strip()
    return cleaned or "维护执行失败"


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _outcome_field(outcome: Any, name: str, default: Any = None) -> Any:
    """读维护结果字段：兼容 MaintenanceRunOutcome 与测试替身（dict / SimpleNamespace）。"""
    if isinstance(outcome, Mapping):
        return outcome.get(name, default)
    return getattr(outcome, name, default)


class KnowledgeMaintenanceScheduler:
    """独立知识维护的调度器（A → B 的唯一入口）。

    构造参数（冻结）：

    * conn：SQLite 连接（迁移 46 的三张表必须已应用）；
    * runner：维护执行器（B 的 MaintenanceRunner 或测试假件），唯一依赖是
      await runner.run_batch(snapshot) 返回带 ok 的结果；
    * settings：可选设置源（有 get / get_int 的对象，例如 SettingsStore）；
      缺省时全部用契约默认值；
    * clock：可选受控时钟（返回 datetime 或 ISO 字符串），测试用它制造超时 / 退避，
      生产缺省用系统 UTC 时钟。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        runner: Any,
        settings: Any = None,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.conn = conn
        self.runner = runner
        self.settings = settings
        self.clock = clock

    # ------------------------------------------------------------------
    # 配置（契约 §12.3 的 settings 键；全部有默认值）
    # ------------------------------------------------------------------

    def _raw_setting(self, key: str) -> Any:
        settings = self.settings
        if settings is None:
            return None
        getter = getattr(settings, "get", None)
        if callable(getter):
            try:
                return getter(key)
            except TypeError:  # 不接受单参数的 get
                return None
        if isinstance(settings, Mapping):
            return settings.get(key)
        return None

    def _setting_int(self, name: str, default: int) -> int:
        key = _KEY_PREFIX + name
        settings = self.settings
        if settings is None:
            return default
        get_int = getattr(settings, "get_int", None)
        if callable(get_int):
            try:
                return int(get_int(key, default))
            except (TypeError, ValueError):
                return default
        raw = self._raw_setting(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    @property
    def enabled(self) -> bool:
        """是否允许触发（knowledge_maintenance.enabled，默认 true）。"""
        raw = self._raw_setting(_KEY_PREFIX + "enabled")
        if raw is None:
            return DEFAULT_ENABLED
        return _as_bool(raw)

    @property
    def change_threshold(self) -> int:
        """待处理变化数达到多少条触发（默认 20）。"""
        return self._setting_int("change_threshold", DEFAULT_CHANGE_THRESHOLD)

    @property
    def waiting_turns_threshold(self) -> int:
        """最老待处理变化等待多少用户轮触发（默认 30）。"""
        return self._setting_int("waiting_turns", DEFAULT_WAITING_TURNS)

    @property
    def max_changes_per_run(self) -> int:
        """单次快照最多取多少条变化（默认 50；非法值退回默认）。"""
        value = self._setting_int("max_changes_per_run", DEFAULT_MAX_CHANGES_PER_RUN)
        return max(1, value)

    @property
    def retry_cooldown_turns(self) -> int:
        """失败后至少等待多少个用户轮再重试（默认 5）。"""
        value = self._setting_int("retry_cooldown_turns", DEFAULT_RETRY_COOLDOWN_TURNS)
        return max(0, value)

    @property
    def check_interval_seconds(self) -> int:
        """Lead 的周期检查循环间隔（默认 60，最小 1 秒）。"""
        value = self._setting_int("check_interval_seconds", DEFAULT_CHECK_INTERVAL_SECONDS)
        return max(1, value)

    # ------------------------------------------------------------------
    # 时钟 / 状态读写
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        if self.clock is None:
            return datetime.now(timezone.utc)
        moment = self.clock()
        if isinstance(moment, datetime):
            resolved = moment
        else:
            resolved = datetime.fromisoformat(str(moment))
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=timezone.utc)
        return resolved

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _ensure_state_row(self, conn: sqlite3.Connection) -> sqlite3.Row:
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_maintenance_state (id, updated_at) VALUES (?, ?)",
            (STATE_ID, _now_iso()),
        )
        row = conn.execute(
            "SELECT " + ", ".join(_STATE_COLUMNS) + " FROM knowledge_maintenance_state WHERE id = ?",
            (STATE_ID,),
        ).fetchone()
        if row is None:  # pragma: no cover - 表在但单行被手删才会到这里
            raise RuntimeError("knowledge_maintenance_state 缺少单行状态（id = 1）")
        return row

    @staticmethod
    def _state_view(row: sqlite3.Row) -> dict:
        return {
            "turn_seq": int(row["turn_seq"] or 0),
            "last_processed_seq": int(row["last_processed_seq"] or 0),
            "batch_seq": int(row["batch_seq"] or 0),
            "running": bool(row["running"]),
            "run_started_at": row["run_started_at"],
            "failed_attempts": int(row["failed_attempts"] or 0),
            "retry_blocked_until_turn": int(row["retry_blocked_until_turn"] or 0),
            "last_error": row["last_error"],
            "last_run_at": row["last_run_at"],
            "updated_at": row["updated_at"],
        }

    def _pending_summary(self, conn: sqlite3.Connection, last_processed_seq: int) -> dict:
        row = conn.execute(
            "SELECT COUNT(*) AS pending_count,"
            " MIN(created_turn_seq) AS oldest_turn,"
            " MIN(seq) AS oldest_seq,"
            " MAX(seq) AS latest_seq"
            " FROM knowledge_change_log WHERE seq > ?",
            (int(last_processed_seq),),
        ).fetchone()
        return {
            "pending_count": int(row["pending_count"] or 0),
            "oldest_turn": row["oldest_turn"],
            "oldest_seq": row["oldest_seq"],
            "latest_seq": row["latest_seq"],
        }

    def _pending_rows(
        self, conn: sqlite3.Connection, last_processed_seq: int, limit: int
    ) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT seq, knowledge_id, chain_id, version, source, occurred_at, created_turn_seq"
            " FROM knowledge_change_log WHERE seq > ? ORDER BY seq LIMIT ?",
            (int(last_processed_seq), int(limit)),
        ).fetchall()

    # ------------------------------------------------------------------
    # 触发条件
    # ------------------------------------------------------------------

    def _waiting_turns(self, turn_seq: int, summary: dict) -> int:
        """最老待处理变化已经等待的用户轮数（没有待处理变化就是 0）。"""
        if int(summary["pending_count"] or 0) <= 0 or summary["oldest_turn"] is None:
            return 0
        return max(0, int(turn_seq) - int(summary["oldest_turn"]))

    def _trigger_for(self, pending_count: int, waiting_turns: int) -> MaintenanceTrigger | None:
        # 两个条件任一达到即可；同时达到时优先报「变化条数」（更直接的原因）。
        if pending_count > 0 and pending_count >= self.change_threshold:
            return MaintenanceTrigger.CHANGE_COUNT
        if pending_count > 0 and waiting_turns >= self.waiting_turns_threshold:
            return MaintenanceTrigger.WAITING_TURNS
        return None

    # ------------------------------------------------------------------
    # 公开接口（冻结）
    # ------------------------------------------------------------------

    def note_user_turn_completed(self, turn_id: str) -> None:
        """真实成功用户 turn 完成后调用；同一 turn_id 重复调用只算一次。

        只有**存在待处理变化**时才累计 turn_seq（没有待处理变化时不增长等待轮次）；
        没有待处理变化时也照样记 turn_log，保证这个 turn 之后再被重复通知不会补算。
        """
        identifier = str(turn_id or "").strip()
        if not identifier:
            return
        now = self._now_iso()
        with transaction(self.conn):
            self._ensure_state_row(self.conn)
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO knowledge_maintenance_turn_log (turn_id, created_at)"
                " VALUES (?, ?)",
                (identifier, now),
            )
            if cursor.rowcount != 1:
                return  # 同一个真实用户 turn 的重复通知：不计第二次
            state = self.conn.execute(
                "SELECT last_processed_seq FROM knowledge_maintenance_state WHERE id = ?",
                (STATE_ID,),
            ).fetchone()
            has_pending = self.conn.execute(
                "SELECT 1 FROM knowledge_change_log WHERE seq > ? LIMIT 1",
                (int(state["last_processed_seq"] or 0),),
            ).fetchone()
            if has_pending is None:
                return  # 没有待处理变化：不累计等待轮次
            self.conn.execute(
                "UPDATE knowledge_maintenance_state SET turn_seq = turn_seq + 1,"
                " updated_at = ? WHERE id = ?",
                (now, STATE_ID),
            )

    def pending_state(self) -> dict:
        """调度状态的只读视图（状态展示 / 测试 / 周期循环判断用）。"""
        with transaction(self.conn):
            view = self._state_view(self._ensure_state_row(self.conn))
            summary = self._pending_summary(self.conn, view["last_processed_seq"])
        pending_count = int(summary["pending_count"] or 0)
        waiting_turns = self._waiting_turns(view["turn_seq"], summary)
        trigger = self._trigger_for(pending_count, waiting_turns)
        return {
            **view,
            "enabled": self.enabled,
            "pending_change_count": pending_count,
            "waiting_turns": waiting_turns,
            "oldest_pending_seq": (
                int(summary["oldest_seq"])
                if pending_count and summary["oldest_seq"] is not None
                else None
            ),
            "change_seq_max": (
                int(summary["latest_seq"])
                if pending_count and summary["latest_seq"] is not None
                else None
            ),
            "retry_blocked": view["turn_seq"] < view["retry_blocked_until_turn"],
            "change_threshold": self.change_threshold,
            "waiting_turns_threshold": self.waiting_turns_threshold,
            "max_changes_per_run": self.max_changes_per_run,
            "retry_cooldown_turns": self.retry_cooldown_turns,
            "check_interval_seconds": self.check_interval_seconds,
            "trigger": trigger.value if trigger is not None else None,
            "in_flight": view["running"],
        }

    async def maybe_run(self, *, busy: bool = False) -> dict:
        """触发检查 + 单轮快照执行（冻结接口，返回可读结果字典）。

        * busy=True（主任务繁忙 / 关闭中）→ 不启动；
        * 关闭状态 / 已有一轮在跑 / 重试退避未到 → 不启动；
        * 未达触发条件 → 不启动、不调用 runner / 模型，也不增长等待轮次；
        * 达到条件 → 原子 claim + 快照 → await runner.run_batch(snapshot)；
          成功按快照上界推进处理位置，失败保留位置并进入退避；
        * 任何路径（成功 / 失败 / 异常 / 取消）最后都释放 running。
        """
        if busy:
            return {"ran": False, "reason": "busy", "state": self.pending_state()}
        if not self.enabled:
            return {"ran": False, "reason": "disabled", "state": self.pending_state()}

        kind, snapshot, info = self._claim()
        if kind != "claimed" or snapshot is None:
            return {"ran": False, "reason": kind, **info, "state": self.pending_state()}

        try:
            outcome = await self.runner.run_batch(snapshot)
        except asyncio.CancelledError:
            # 取消不是「成功」：保留处理位置、计一次失败（避免每轮重复调用），
            # 然后**原样抛出**让上层据此判定「本轮未确认完成」。
            self._finish_failure(snapshot, "维护执行被取消（未确认完成）")
            raise
        except Exception as exc:  # noqa: BLE001 - 维护失败必须被隔离成可读结果
            return self._finish_failure(snapshot, exc)

        if bool(_outcome_field(outcome, "ok", False)):
            return self._finish_success(snapshot, outcome)
        error = _outcome_field(outcome, "error", None) or "维护执行未成功（runner 未给出错误原因）"
        return self._finish_failure(snapshot, error, outcome=outcome)

    # ------------------------------------------------------------------
    # claim / 快照（同一事务内完成：running 与快照上界一致）
    # ------------------------------------------------------------------

    def _claim(self) -> tuple[str, MaintenanceSnapshot | None, dict]:
        now = self._now_iso()
        now_moment = self._now()
        with transaction(self.conn):
            state = self._ensure_state_row(self.conn)
            view = self._state_view(state)
            if view["running"]:
                if not self._is_stale(now_moment, view["run_started_at"]):
                    return (
                        "already_running",
                        None,
                        {"batch_seq": view["batch_seq"], "run_started_at": view["run_started_at"]},
                    )
                # 过期回收：上一轮被崩溃 / 强杀，清 running 并计一次失败（位置不动）。
                attempts = view["failed_attempts"] + 1
                blocked_until = view["turn_seq"] + self.retry_cooldown_turns
                self.conn.execute(
                    "UPDATE knowledge_maintenance_state SET running = 0, failed_attempts = ?,"
                    " retry_blocked_until_turn = ?, last_error = ?, last_run_at = ?,"
                    " updated_at = ? WHERE id = ?",
                    (attempts, blocked_until, _STALE_ERROR, now, now, STATE_ID),
                )
                logger.warning(
                    "维护 running 标记过期回收：failed_attempts=%s retry_blocked_until_turn=%s",
                    attempts,
                    blocked_until,
                )
                return (
                    "stale_recovered",
                    None,
                    {
                        "failed_attempts": attempts,
                        "retry_blocked_until_turn": blocked_until,
                        "last_error": _STALE_ERROR,
                    },
                )

            summary = self._pending_summary(self.conn, view["last_processed_seq"])
            pending_count = int(summary["pending_count"] or 0)
            waiting_turns = self._waiting_turns(view["turn_seq"], summary)
            if pending_count <= 0:
                return (
                    "no_trigger",
                    None,
                    {"pending_change_count": 0, "waiting_turns": 0},
                )
            if view["turn_seq"] < view["retry_blocked_until_turn"]:
                # 失败退避未到：不调用 runner / 模型，也不动处理位置。
                return (
                    "retry_cooldown",
                    None,
                    {
                        "turn_seq": view["turn_seq"],
                        "retry_blocked_until_turn": view["retry_blocked_until_turn"],
                        "failed_attempts": view["failed_attempts"],
                        "last_error": view["last_error"],
                        "pending_change_count": pending_count,
                    },
                )
            trigger = self._trigger_for(pending_count, waiting_turns)
            if trigger is None:
                return (
                    "no_trigger",
                    None,
                    {
                        "pending_change_count": pending_count,
                        "waiting_turns": waiting_turns,
                        "change_threshold": self.change_threshold,
                        "waiting_turns_threshold": self.waiting_turns_threshold,
                    },
                )

            limit = self.max_changes_per_run
            rows = self._pending_rows(self.conn, view["last_processed_seq"], limit)
            change_ids = tuple(int(row["seq"]) for row in rows)
            if not change_ids:
                return (
                    "no_trigger",
                    None,
                    {"pending_change_count": pending_count, "waiting_turns": waiting_turns},
                )
            change_seq_max = change_ids[-1]
            knowledge_ids = tuple(dict.fromkeys(str(row["knowledge_id"]) for row in rows))
            batch_seq = view["batch_seq"] + 1
            self.conn.execute(
                "UPDATE knowledge_maintenance_state SET running = 1, run_started_at = ?,"
                " batch_seq = ?, last_run_at = ?, updated_at = ? WHERE id = ?",
                (now, batch_seq, now, now, STATE_ID),
            )
            limits = dict(MAINTENANCE_LIMITS)
            # 配置可覆盖单次处理量：B 按这个上限裁剪输入与候选取样。
            limits["max_batch_changes"] = limit
            snapshot = MaintenanceSnapshot(
                batch_seq=batch_seq,
                trigger=trigger,
                change_seq_max=change_seq_max,
                pending_change_count=pending_count,
                waiting_turns=waiting_turns,
                change_ids=change_ids,
                knowledge_ids=knowledge_ids,
                limits=limits,
            )
            logger.info(
                "维护触发：batch=%s trigger=%s changes=%s pending=%s waiting_turns=%s",
                batch_seq,
                trigger.value,
                len(change_ids),
                pending_count,
                waiting_turns,
            )
            return ("claimed", snapshot, {})

    def _is_stale(self, now: datetime, started_at: Any) -> bool:
        """run_started_at 超过 RUN_STALE_SECONDS（或无法解析）视为上一轮已中断。"""
        if not started_at:
            return True
        try:
            started = datetime.fromisoformat(str(started_at))
        except (TypeError, ValueError):
            return True
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (now - started).total_seconds() > RUN_STALE_SECONDS

    # ------------------------------------------------------------------
    # 收尾：成功推进位置 / 失败保留位置并退避
    # ------------------------------------------------------------------

    def _finish_success(self, snapshot: MaintenanceSnapshot, outcome: Any) -> dict:
        processed = _outcome_field(outcome, "processed_change_seq", None)
        try:
            processed_number = int(processed) if processed is not None else None
        except (TypeError, ValueError):
            processed_number = None
        if processed_number is None:
            processed_number = snapshot.change_seq_max
        # 处理位置只前进、且不越过本轮快照上界：快照期间新增的变化留给下一批。
        processed_number = max(0, min(processed_number, snapshot.change_seq_max))
        now = self._now_iso()
        write_ok = True
        try:
            with transaction(self.conn):
                state = self._ensure_state_row(self.conn)
                processed_number = max(int(state["last_processed_seq"] or 0), processed_number)
                self.conn.execute(
                    "UPDATE knowledge_maintenance_state SET last_processed_seq = ?,"
                    " failed_attempts = 0, retry_blocked_until_turn = 0, last_error = NULL,"
                    " last_run_at = ?, updated_at = ?, running = 0 WHERE id = ?",
                    (processed_number, now, now, STATE_ID),
                )
                # 审计字段：只回填本批消费掉的变化（待处理判定仍只用处理位置）。
                self.conn.execute(
                    "UPDATE knowledge_change_log SET consumed_batch_seq = ?"
                    " WHERE seq <= ? AND consumed_batch_seq IS NULL",
                    (snapshot.batch_seq, processed_number),
                )
        except Exception as exc:  # noqa: BLE001 - 写状态失败不能掩盖本轮结果
            write_ok = False
            logger.error("维护成功后写入调度状态失败：%s", _format_error(exc), exc_info=True)
        finally:
            self._release_running()

        return {
            "ran": True,
            "ok": True,
            "reason": None,
            "batch_seq": snapshot.batch_seq,
            "trigger": snapshot.trigger.value,
            "change_seq_max": snapshot.change_seq_max,
            "change_ids": list(snapshot.change_ids),
            "knowledge_ids": list(snapshot.knowledge_ids),
            "pending_change_count": snapshot.pending_change_count,
            "waiting_turns": snapshot.waiting_turns,
            "processed_change_seq": processed_number,
            "status": str(_outcome_field(outcome, "status", "") or ""),
            "batch_id": _outcome_field(outcome, "batch_id", None),
            "applied": int(_outcome_field(outcome, "applied", 0) or 0),
            "asked": int(_outcome_field(outcome, "asked", 0) or 0),
            "rejected": int(_outcome_field(outcome, "rejected", 0) or 0),
            "state_written": write_ok,
            "state": self.pending_state(),
        }

    def _finish_failure(
        self, snapshot: MaintenanceSnapshot, error: Any, *, outcome: Any = None
    ) -> dict:
        message = _format_error(error)
        now = self._now_iso()
        attempts = 0
        blocked_until = 0
        position = snapshot.change_seq_max
        try:
            with transaction(self.conn):
                state = self._ensure_state_row(self.conn)
                attempts = int(state["failed_attempts"] or 0) + 1
                blocked_until = int(state["turn_seq"] or 0) + self.retry_cooldown_turns
                position = int(state["last_processed_seq"] or 0)
                self.conn.execute(
                    "UPDATE knowledge_maintenance_state SET failed_attempts = ?,"
                    " retry_blocked_until_turn = ?, last_error = ?, last_run_at = ?,"
                    " updated_at = ?, running = 0 WHERE id = ?",
                    (attempts, blocked_until, message, now, now, STATE_ID),
                )
        except Exception as exc:  # noqa: BLE001 - 失败路径本身不能再抛
            logger.error("维护失败后写入调度状态失败：%s", _format_error(exc), exc_info=True)
        finally:
            self._release_running()
        logger.warning(
            "维护失败（位置保持 %s）：%s；failed_attempts=%s retry_blocked_until_turn=%s",
            position,
            message,
            attempts,
            blocked_until,
        )
        return {
            "ran": True,
            "ok": False,
            "reason": "failed",
            "error": message,
            "batch_seq": snapshot.batch_seq,
            "trigger": snapshot.trigger.value,
            "change_seq_max": snapshot.change_seq_max,
            "pending_change_count": snapshot.pending_change_count,
            "waiting_turns": snapshot.waiting_turns,
            "processed_change_seq": position,
            "failed_attempts": attempts,
            "retry_blocked_until_turn": blocked_until,
            "status": str(_outcome_field(outcome, "status", "") or "") or None,
            "batch_id": _outcome_field(outcome, "batch_id", None),
            "state": self.pending_state(),
        }

    def _release_running(self) -> None:
        """幂等释放 running（成功 / 失败路径已各自置 0；这里是最后的兜底）。"""
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE knowledge_maintenance_state SET running = 0"
                    " WHERE id = ? AND running != 0",
                    (STATE_ID,),
                )
        except Exception as exc:  # noqa: BLE001 - 关闭 / 数据库已释放时只能记录
            logger.warning("释放维护 running 标记失败：%s", _format_error(exc))
