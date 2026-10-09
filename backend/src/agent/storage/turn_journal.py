"""Turn 队列的持久化台账：被 API 接受过的消息不能在重启后静默消失。

产品语义（**不自动重放**）：

* `queued`    —— API 已经明确接受，但还没开始执行；
* `running`   —— 正在执行；
* `completed` / `cancelled` / `failed` / `unavailable` / `incomplete` —— 终态
  （`incomplete` = 流没有合法结束标记就 EOF，回答可能不完整；见 plan §C2）；
* `interrupted` —— 进程结束（含崩溃）时还没走到终态的 turn。

重启后的规则（明确、可测）：

1. 台账里还写着 `queued` / `running` 的行一律标成 `interrupted`
   （原因分别记 `queued_at_restart` / `running_at_restart`）；
2. **不自动执行任何一条**：进程退出可能正是用户的意思，自动重放会重复花
   模型钱、重复产生回答，甚至在用户没看着的时候改动数据；
3. 消息原文留在台账里，通过 `/api/runtime/state` 的 `interrupted_turns`
   如实告诉用户「这条没有被执行」，由用户明确决定「重发」或「知道了」；
4. 只有 `interrupted` 的行能被重发；`completed` 等终态永远不可重发 ——
   这就是「不得重复执行已经完成的 turn」的落地方式；
5. 重发是**一次性**的：`claim()` 用一条带条件的 UPDATE 抢占，抢占成功才提交
   新 turn，所以同一条记录不可能被重发两次。

为什么队列里的消息会消失（缺陷）：排队中的 turn 只活在进程内存的
`asyncio.Queue` 里，用户消息甚至还没写进 `messages` 表 —— 进程一退，
它就没有任何痕迹。台账是这条消息唯一的落点。

保留策略：终态行保留 `DEFAULT_RETENTION_DAYS` 天（只为「不重复执行」作证），
到期清理；`interrupted` 行在用户处理（重发 / 知道了）之前**不清理**。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from agent.trace.redact import redact_text

logger = logging.getLogger(__name__)

# 台账状态（至少覆盖 queued / running / completed / cancelled / interrupted）
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
UNAVAILABLE = "unavailable"
# 冻结契约 C2：协议没有给出结束标记就 EOF = 不完整结束，不是完成。
# 它必须进终态集合，否则 terminal() 会把它兜底成 failed —— 台账就会说谎。
INCOMPLETE = "incomplete"
INTERRUPTED = "interrupted"

TERMINAL_STATUSES = (COMPLETED, CANCELLED, FAILED, UNAVAILABLE, INCOMPLETE)
# 进程结束时会「没走到终态」的状态：重启后一律变成 interrupted
OPEN_STATUSES = (QUEUED, RUNNING)

DEFAULT_RETENTION_DAYS = 7

# 权威定义一：「用户的消息被进程掐断」的行 —— 系统通知轮（notify = 1）不是用户的消息，
# 永远不在这个集合里。notify 这个条件**只在下面这一行写一次**。
_USER_INTERRUPTED_CLAUSE = "status = ? AND notify = 0"
_USER_INTERRUPTED_PARAMS: tuple = (INTERRUPTED,)

# 权威定义二：「用户还没处理的 interrupted 行」= 定义一 + 还没被处理过。
# 用它的人：unfinished()（界面入口）、recoverable()（resend / dismiss 的判断，
# dismiss 也走它）、claim()（重发的一次性抢占）。
#
# 两者确实不同（写清差异，不混用）：区别就是 recovered_at IS NULL。
# * 定义一给「已经进入恢复流程」的行用：claim 抢占时会把 recovered_at 写上，
#   mark_recovered / release_claim 处理的正是这种「已经抢占过」的行 —— 它们用定义一；
# * 定义二给「还没被处理过才算可恢复」的入口与判断用。
#
# 为什么必须收敛：以前 unfinished() 带了 notify = 0，recoverable() / claim() 没带，
# 于是对系统通知轮直接调 resend 会真的再提交一条系统消息（实测 200 + accepted），
# dismiss 也会被放行。
_RECOVERABLE_CLAUSE = f"{_USER_INTERRUPTED_CLAUSE} AND recovered_at IS NULL"
_RECOVERABLE_PARAMS = _USER_INTERRUPTED_PARAMS

# 用户看到的文案（不含任何内部标识之外的东西）
REASON_TEXT = {
    "queued_at_restart": "这条消息当时还在排队，进程退出后没有开始执行",
    "running_at_restart": "这条消息执行到一半，进程退出后没有完成",
    "shutdown": "应用关闭时这条消息还没有执行",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redacted_or_none(value: str | None) -> str | None:
    """落库前过脱敏：台账里不得出现密钥原文；空串按「没有」处理。"""
    text = redact_text(value) if value is not None else ""
    text = (text or "").strip()
    return text or None


def _human_reason(value: Any) -> str | None:
    """台账里的 reason：历史遗留码（queued_at_restart…）翻成人话，其余原样。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    return REASON_TEXT.get(text, text)


def _parse_actions(raw: Any) -> list[str]:
    """actions 列是 JSON 数组字符串；坏数据返回 []（绝不抛、也不猜内容）。"""
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


class TurnJournal:
    """`turn_journal` 表的读写。所有写入失败都只记日志 —— 台账不能挡住对话。"""

    def __init__(self, conn: sqlite3.Connection, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.conn = conn
        self.retention_days = int(retention_days)

    # -- 写入：生命周期 ---------------------------------------------------

    def accepted(
        self,
        *,
        turn_id: str,
        message: str,
        topic_id: str | None = None,
        notify: bool = False,
        status: str = QUEUED,
    ) -> None:
        """把一个刚被接受的 turn 落一行（排队中的消息从此有了痕迹）。"""
        self._execute(
            "INSERT OR IGNORE INTO turn_journal "
            "(turn_id, message, topic_id, notify, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(turn_id),
                str(message or ""),
                topic_id,
                1 if notify else 0,
                status if status in OPEN_STATUSES else QUEUED,
                _now(),
                _now(),
            ),
        )

    def running(self, turn_id: str) -> None:
        self._execute(
            "UPDATE turn_journal SET status = ?, started_at = COALESCE(started_at, ?), "
            "updated_at = ? WHERE turn_id = ? AND status = ?",
            (RUNNING, _now(), _now(), str(turn_id), QUEUED),
        )

    def terminal(self, turn_id: str, status: str, *, reason: str | None = None) -> None:
        """记终态。

        `cancelled` + `reason='shutdown'` 记成 `interrupted`：那是进程把这一轮
        掐断的，不是用户取消的 —— 重启后应该给用户一个「重发」的机会。

        `incomplete` 是正常终态之一（契约 C2）：它照原样落库，不折算成 failed / completed。
        """
        if status not in TERMINAL_STATUSES:
            status = FAILED
        journal_status = INTERRUPTED if (status == CANCELLED and reason == "shutdown") else status
        self._execute(
            "UPDATE turn_journal SET status = ?, ended_at = ?, updated_at = ?, "
            "reason = COALESCE(?, reason) WHERE turn_id = ? AND status IN (?, ?)",
            (journal_status, _now(), _now(), reason, str(turn_id), QUEUED, RUNNING),
        )

    # -- 结束事实（R4 S6：刷新 / 换设备后失败轮仍能「重试」）----------------

    def record_facts(
        self,
        turn_id: str,
        *,
        reason_code: str | None,
        reason: str | None,
        stopped_by: str | None,
        actions: list[str],
    ) -> None:
        """把 TURN_END 的结束事实落进台账。

        为什么要落库：这些字段以前只随事件发一次，刷新或换设备后就没了 ——
        用户看到一轮失败、刷新之后「重试」入口消失，只能重写整段需求。

        * 只写事实本身：reason_code / reason（**落库前过脱敏**）/ stopped_by /
          actions（JSON 数组字符串）；
        * 没有这一行时静默不写 —— 台账是旁路，绝不凭空建一条假记录；
        * 写入失败只记日志（与台账其它写入一致），不能挡住对话。
        """
        if not str(turn_id or "").strip():
            return
        payload = json.dumps(
            [str(item).strip() for item in (actions or []) if str(item).strip()],
            ensure_ascii=False,
        )
        self._execute(
            "UPDATE turn_journal SET reason_code = ?, reason = ?, stopped_by = ?,"
            " actions = ?, updated_at = ? WHERE turn_id = ?",
            (
                _redacted_or_none(reason_code),
                _redacted_or_none(reason),
                _redacted_or_none(stopped_by),
                payload,
                _now(),
                str(turn_id),
            ),
        )

    def facts(self, turn_ids: Iterable[str]) -> dict[str, dict]:
        """批量取每轮的结束事实（**一次查询**，不 N+1）。

        返回 ``{turn_id: {turn_id, status, reason_code, reason, stopped_by, actions}}``：

        * 没有这一行 → 不出现（调用方按旧行为显示状态词，绝不伪造原因）；
        * 旧行（迁移前写入、三列为 NULL）→ 状态与既有 reason 照给，actions 为 []；
        * reason 是历史遗留码时翻成人话，内部码不上界面；
        * actions 反序列化失败 → []，绝不抛。
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for item in turn_ids or []:
            value = str(item).strip()
            if value and value not in seen:
                seen.add(value)
                wanted.append(value)
        if not wanted:
            return {}
        out: dict[str, dict] = {}
        # 与附件批量查询同样的分块：SQLite 变量上限以内，一次取回一页。
        for start in range(0, len(wanted), 400):
            chunk = wanted[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._query(
                "SELECT turn_id, status, reason, reason_code, stopped_by, actions"
                f" FROM turn_journal WHERE turn_id IN ({placeholders})",
                tuple(chunk),
            )
            for row in rows:
                turn_id = str(row["turn_id"])
                out[turn_id] = {
                    "turn_id": turn_id,
                    "status": str(row["status"] or ""),
                    "reason_code": str(row["reason_code"]) if row["reason_code"] else None,
                    "reason": _human_reason(row["reason"]),
                    "stopped_by": str(row["stopped_by"]) if row["stopped_by"] else None,
                    "actions": _parse_actions(row["actions"]),
                }
        return out

    def note_user_message(self, turn_id: str, message_id: str | None) -> None:
        """记下这一轮已经写进历史的用户消息 id（重启后能如实告诉用户）。"""
        if not message_id:
            return
        self._execute(
            "UPDATE turn_journal SET user_message_id = ?, updated_at = ? WHERE turn_id = ?",
            (str(message_id), _now(), str(turn_id)),
        )

    # -- 重启恢复 ---------------------------------------------------------

    def interrupt_stale(self) -> list[dict[str, Any]]:
        """启动时把上一个进程留下的 queued / running 标成 interrupted。

        只改状态、不动消息原文；返回被动过的行（供启动日志与界面提示）。
        """
        rows = self._query(
            "SELECT * FROM turn_journal WHERE status IN (?, ?) ORDER BY created_at ASC",
            (QUEUED, RUNNING),
        )
        recovered: list[dict[str, Any]] = []
        for row in rows:
            reason = (
                "running_at_restart" if row["status"] == RUNNING else "queued_at_restart"
            )
            self._execute(
                "UPDATE turn_journal SET status = ?, reason = ?, updated_at = ? "
                "WHERE turn_id = ? AND status IN (?, ?)",
                (INTERRUPTED, reason, _now(), row["turn_id"], QUEUED, RUNNING),
            )
            recovered.append(
                self._view({**dict(row), "status": INTERRUPTED, "reason": reason})
            )
        return recovered

    def unfinished(self) -> list[dict[str, Any]]:
        """还没被用户处理的 interrupted 行（系统通知轮不算：它不是用户的消息）。

        与 recoverable() 共用同一个权威谓词（见 _RECOVERABLE_CLAUSE）。
        """
        rows = self._query(
            f"SELECT * FROM turn_journal WHERE {_RECOVERABLE_CLAUSE} ORDER BY created_at ASC",
            _RECOVERABLE_PARAMS,
        )
        return [self._view(dict(r)) for r in rows]

    def recoverable(self, turn_id: str) -> dict[str, Any] | None:
        """这条记录还能不能重发 / 知道了：只有「用户还没处理的 interrupted 行」才算。

        与 unfinished() 同一个权威谓词（见 _RECOVERABLE_CLAUSE）——系统通知轮
        （notify = 1）不在其中：它既不在入口里，也不允许被重发 / 知道了。
        """
        row = self._query_one(
            f"SELECT * FROM turn_journal WHERE turn_id = ? AND {_RECOVERABLE_CLAUSE}",
            (str(turn_id), *_RECOVERABLE_PARAMS),
        )
        return dict(row) if row is not None else None

    def claim(self, turn_id: str) -> bool:
        """抢占一条记录的重发权（原子、一次性）：抢到了才允许提交新 turn。

        抢占条件与 recoverable() 完全一致（同一个权威谓词）：系统通知轮抢不到，
        所以即便有人绕过接口判断，也提交不出新的系统消息。
        """
        try:
            cur = self.conn.execute(
                "UPDATE turn_journal SET recovered_at = ?, updated_at = ? "
                f"WHERE turn_id = ? AND {_RECOVERABLE_CLAUSE}",
                (_now(), _now(), str(turn_id), *_RECOVERABLE_PARAMS),
            )
        except sqlite3.Error as exc:  # noqa: BLE001 - 台账失败不得影响接口可用性
            logger.warning("turn journal claim failed: %s", exc)
            return False
        return int(cur.rowcount or 0) == 1

    def release_claim(self, turn_id: str) -> None:
        """提交失败时把抢占退回去（否则用户就再也重发不了这条消息了）。

        回滚作用于「用户被掐断的行」（权威定义一）：抢不到的行本来就没被改过，
        不该在这里被动到；通知轮也不在其中。
        """
        self._execute(
            "UPDATE turn_journal SET recovered_at = NULL, updated_at = ? "
            f"WHERE turn_id = ? AND {_USER_INTERRUPTED_CLAUSE}",
            (_now(), str(turn_id), *_USER_INTERRUPTED_PARAMS),
        )

    def mark_recovered(self, turn_id: str, *, new_turn_id: str | None = None) -> bool:
        """标记已处理；返回是否真的改到了行（与 claim 一样是带条件的更新）。

        这里用权威定义一（不含 recovered_at IS NULL）：调用它的时机是 claim 已经
        抢占成功之后，行上的 recovered_at 已经写上了。通知轮仍然不在集合里。
        """
        try:
            cur = self.conn.execute(
                "UPDATE turn_journal SET recovered_at = ?, recovered_by = ?, updated_at = ? "
                f"WHERE turn_id = ? AND {_USER_INTERRUPTED_CLAUSE}",
                (_now(), new_turn_id, _now(), str(turn_id), *_USER_INTERRUPTED_PARAMS),
            )
        except sqlite3.Error as exc:  # noqa: BLE001 - 台账失败不得影响接口可用性
            logger.warning("turn journal mark_recovered failed: %s", exc)
            return False
        return int(cur.rowcount or 0) == 1

    def dismiss(self, turn_id: str) -> bool:
        """用户选择「知道了」：不再提示，但仍然保留记录（不删用户消息）。"""
        if self.recoverable(turn_id) is None:
            return False
        self._execute(
            "UPDATE turn_journal SET recovered_at = ?, updated_at = ? WHERE turn_id = ?",
            (_now(), _now(), str(turn_id)),
        )
        return True

    # -- 清理 -------------------------------------------------------------

    def prune_terminal(self, days: int | None = None) -> int:
        """清掉过期终态行（只为「不重复执行已完成 turn」作证，不需要永久保留）。

        `interrupted` 行不会被清：它承载用户还没看到的消息原文。
        """
        retention = self.retention_days if days is None else int(days)
        if retention <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat()
        # 占位符按终态集合长度生成：以前写死 4 个，加入 incomplete 后会变成
        # 「6 个绑定，5 个占位符」——清理静默失败（只记 warning），终态行永不清理。
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        try:
            cur = self.conn.execute(
                f"DELETE FROM turn_journal WHERE status IN ({placeholders}) "
                "AND ended_at IS NOT NULL AND ended_at < ?",
                (*TERMINAL_STATUSES, cutoff),
            )
        except sqlite3.Error as exc:  # noqa: BLE001 - 清理是维护动作
            logger.warning("turn journal prune failed: %s", exc)
            return 0
        return int(cur.rowcount or 0)

    # -- internals --------------------------------------------------------

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        """给界面的形状：消息原文照给（它就是用户自己发的那句话）。"""
        row.pop("notify", None)
        row["reason_text"] = REASON_TEXT.get(str(row.get("reason") or ""), "")
        return row

    def _execute(self, sql: str, params: tuple) -> None:
        try:
            self.conn.execute(sql, params)
        except sqlite3.Error as exc:  # noqa: BLE001 - 台账失败不得影响对话
            logger.warning("turn journal write failed: %s", exc)

    def _query(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        try:
            return list(self.conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:  # noqa: BLE001 - 读不到就当没有
            logger.warning("turn journal read failed: %s", exc)
            return []

    def _query_one(self, sql: str, params: tuple) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None
