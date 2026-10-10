"""Turn 队列的持久化台账：被 API 接受过的消息不能在重启后静默消失。

产品语义（**不自动重放**）：

* `queued`    —— API 已经明确接受，但还没开始执行；
* `running`   —— 正在执行；
* `completed` / `cancelled` / `failed` / `unavailable` —— 终态；
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
5. 重发是**一次性**的：`claim_for_resend()` 在**一个事务**里同时完成
   「老记录 recovered_by/recovered_at」+「新记录」+「关联」，条件 UPDATE 抢不到
   就直接失败，所以同一条记录不可能被重发两次，也不可能只写一半。

第二轮补充（契约 C1 / C2 / C3）：

* **实例归属**：每一条台账行都记住是哪个实例接受的（`owner_instance_id`，
  同时登记进 `record_owners`）。启动恢复只处理「确认已退出实例」的行；
  归属者活着（新实例看到旧实例还在跑）→ 一行都不动；判不出来 → 一样不动。
  老记录没有归属 → 保守保留 + 计数（不静默改状态）。
* **持久接受**：`accepted()` 写失败**必须抛** `JournalWriteError`（以前是
  「写失败只记日志」—— 于是 API 返回 200 accepted，消息却没进库，重启即丢）。
  `TurnManager.submit()` 据此先持久化成功、才入队返回（契约 C2）。
* **重发关联**：`claim_for_resend()` 单事务；`orphaned_claims()` / `repair_orphan()`
  处理「抢占了但后继没写成」的遗留，孤立即记录不得永久隐藏。

补充修复轮（A01 / A03；收件箱实现见 `services/recovery.py`）：

* **接管**：`take_over()` 用条件 UPDATE 把一条**无归属**（或归属者已确认退出）的
  `queued` / `running` 历史行精确接管成 `interrupted`，命中 0 行（状态已变 /
  有归属且不是确认退出）时一行都不改 —— 这是「升级前的历史消息」唯一的可操作入口；
* **忽略是终态**：`dismiss()` 现在写成 `status = 'dismissed'`，与「抢占过但没有后继」
  的孤儿从库里就分得开，既不会被收件箱当成孤儿重列，也不能被 `repair_orphan()`
  复活成可重发。

为什么队列里的消息会消失（缺陷）：排队中的 turn 只活在进程内存的
`asyncio.Queue` 里，用户消息甚至还没写进 `messages` 表 —— 进程一退，
它就没有任何痕迹。台账是这条消息唯一的落点。

保留策略：终态行保留 `DEFAULT_RETENTION_DAYS` 天（只为「不重复执行」作证），
到期清理；`interrupted` 行在用户处理（重发 / 知道了）之前**不清理**。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from agent.storage.db import transaction
from agent.storage.instance_registry import RECORD_TURN

logger = logging.getLogger(__name__)

# 台账状态（至少覆盖 queued / running / completed / cancelled / interrupted）
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
CANCELLED = "cancelled"
FAILED = "failed"
UNAVAILABLE = "unavailable"
INTERRUPTED = "interrupted"
# 用户明确「知道了」（忽略）之后的终态：原文保留、不再提示、不可重发、不可修复。
# 为什么需要它是独立状态（A01/A03）：以前 dismiss 只写 recovered_at，于是
# 「用户已忽略」和「抢占成功但没收尾的孤儿」在库里长得**一模一样**
# （recovered_at 非空 + recovered_by 空），收件箱会把用户刚忽略的记录当成孤儿
# 再列出来，repair_orphan 甚至能把它复活成可重发。
DISMISSED = "dismissed"

TERMINAL_STATUSES = (COMPLETED, CANCELLED, FAILED, UNAVAILABLE)
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
    "user_confirmed_takeover": "你确认接管了这条当时没有归属的消息",
}

# 用户明确接管一条历史记录时写进 reason 的标记（契约 §2.1 冻结字面量）。
TAKEOVER_REASON = "user_confirmed_takeover"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JournalWriteError(RuntimeError):
    """台账写入**该成功却没写成**。

    只用在「被 API 受理 = 必须落库」的那一次写入上（`accepted`）。
    其它旁路写入（running/terminal/note_user_message）继续保持「失败只记日志」：
    消息已经被受理，收尾信息写不进去不该把对话打断。

    为什么要有这个异常：以前 `_execute` 把所有 sqlite3.Error 吞成一条 warning，
    于是 `POST /api/turns` 在库写不进去时仍然返回 200 accepted —— 内存里有一条
    永远执行不完的 turn，库里什么都没有，重启后这条消息**没有任何痕迹**。
    用户以为发出去了。现在它必须变成一次明确的拒绝（HTTP 503）。
    """


class TurnJournal:
    """`turn_journal` 表的读写。

    旁路写入（running / terminal / 提示）失败只记日志 —— 台账不能挡住对话；
    但「受理」这一次写入失败必须抛 `JournalWriteError`（见上面的说明）。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        instance_id: str | None = None,
        registry: Any = None,
    ) -> None:
        self.conn = conn
        self.retention_days = int(retention_days)
        # 本实例身份：新写的行带上它，别的实例重启时才知道「这行是谁写的」。
        # 没有它时行为与以前完全一致（owner_instance_id 留 NULL）。
        self.instance_id = instance_id
        # 可选的实例归属表（duck-typed）：claim / owner_alive。
        # 没有它时全部退化回「只看状态」的旧行为，不影响任何既有调用方。
        self.registry = registry

    def _owner_id(self) -> str | None:
        return self.instance_id

    # -- 写入：生命周期 ---------------------------------------------------

    def accepted(
        self,
        *,
        turn_id: str,
        message: str,
        topic_id: str | None = None,
        notify: bool = False,
        status: str = QUEUED,
        instance_id: str | None = None,
    ) -> None:
        """把一个刚被接受的 turn 落一行（排队中的消息从此有了痕迹）。

        **写失败抛 `JournalWriteError`**（契约 C2）：这是「受理」的持久化，
        它是 API 返回 200 的前提 —— 写不进去就必须让调用方拒绝这条消息，
        而不是返回一个库里不存在的「已接受」。
        """
        owner = instance_id if instance_id is not None else self._owner_id()
        moment = _now()
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT OR IGNORE INTO turn_journal "
                    "(turn_id, message, topic_id, notify, status, created_at, updated_at, "
                    " owner_instance_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(turn_id),
                        str(message or ""),
                        topic_id,
                        1 if notify else 0,
                        status if status in OPEN_STATUSES else QUEUED,
                        moment,
                        moment,
                        owner,
                    ),
                )
        except sqlite3.Error as exc:
            logger.error("turn journal accept failed: %s", exc)
            raise JournalWriteError(f"turn journal accept failed: {exc}") from exc
        if owner and self.registry is not None:
            try:
                self.registry.claim(RECORD_TURN, str(turn_id), owner)
            except Exception:  # noqa: BLE001 - 归属登记失败不撤销已经落库的受理
                logger.warning("turn journal owner claim failed", exc_info=True)

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
        """
        if status not in TERMINAL_STATUSES:
            status = FAILED
        journal_status = INTERRUPTED if (status == CANCELLED and reason == "shutdown") else status
        self._execute(
            "UPDATE turn_journal SET status = ?, ended_at = ?, updated_at = ?, "
            "reason = COALESCE(?, reason) WHERE turn_id = ? AND status IN (?, ?)",
            (journal_status, _now(), _now(), reason, str(turn_id), QUEUED, RUNNING),
        )

    def note_user_message(self, turn_id: str, message_id: str | None) -> None:
        """记下这一轮已经写进历史的用户消息 id（重启后能如实告诉用户）。"""
        if not message_id:
            return
        self._execute(
            "UPDATE turn_journal SET user_message_id = ?, updated_at = ? WHERE turn_id = ?",
            (str(message_id), _now(), str(turn_id)),
        )

    # -- 重启恢复 ---------------------------------------------------------

    def interrupt_stale(self, registry: Any = None) -> list[dict[str, Any]]:
        """启动时把上一个进程留下的 queued / running 标成 interrupted。

        只改状态、不动消息原文；返回被动过的行（供启动日志与界面提示）。

        `registry` 给定时加一层**实例归属**判定（契约 C1）：

        * 行的归属者确认已退出 → 照常标 interrupted；
        * 行的归属者**还活着**（另一个实例仍在跑）→ 一行都不动：
          新实例启动不能把正在工作的实例的任务标成中断；
        * 归属者判不出来（unknown）/ 没有归属（旧记录）→ 保守保留原状态，
          只计数不改状态（等下一次维护重判）。

        `registry` 省略时保持原来的行为：库只有一个写入者的老路径不变。
        """
        if registry is not None:
            res = self.conn
            res.execute("BEGIN IMMEDIATE")
            try:
                report = self._interrupt_owned(registry)
                res.execute("COMMIT")
            except BaseException:
                res.execute("ROLLBACK")
                raise
            return report
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

    def _interrupt_owned(self, registry: Any) -> list[dict[str, Any]]:
        """按归属判定中断（调用方持有事务；unknown 一律不改状态）。"""
        rows = self.conn.execute(
            "SELECT * FROM turn_journal WHERE status IN (?, ?) ORDER BY created_at ASC",
            (QUEUED, RUNNING),
        ).fetchall()
        recovered: list[dict[str, Any]] = []
        deferred = 0
        for raw in rows:
            row = dict(raw)
            owner = self._row_owner(dict(raw), registry)
            state = registry.owner_alive(owner) if owner else None
            if state is True:
                # 别的主人还在跑：不改它的状态，也不清它的归属。
                continue
            if state is None:
                # unknown（或旧记录没有归属）：保守保留 + 待处理，绝不自动中断。
                deferred += 1
                continue
            reason = (
                "running_at_restart" if row["status"] == RUNNING else "queued_at_restart"
            )
            self.conn.execute(
                "UPDATE turn_journal SET status = ?, reason = ?, updated_at = ? "
                "WHERE turn_id = ? AND status IN (?, ?)",
                (INTERRUPTED, reason, _now(), row["turn_id"], QUEUED, RUNNING),
            )
            recovered.append(
                self._view({**row, "status": INTERRUPTED, "reason": reason})
            )
        if deferred:
            # 如实说清「有多少条因为判不出归属而没动」：这不是错误，是保守。
            logger.info("turn journal: %s 条待处理记录因归属未知而保留原状态", deferred)
        return recovered

    @staticmethod
    def _row_owner(row: dict[str, Any], registry: Any) -> str | None:
        """这一行的归属者：优先 record_owners（权威），退回行上的归属列。"""
        try:
            owner = registry.owner_instance_id(RECORD_TURN, str(row.get("turn_id")))
        except Exception:  # noqa: BLE001 - 归属表读不到就退回列值，不因此中断恢复
            owner = None
        if owner:
            return str(owner)
        column = row.get("owner_instance_id")
        return str(column) if column else None

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

        **这是给「一个进程独占库」的旧路径用的**：新代码走
        `claim_for_resend()`（单事务，连新记录一起写）。两者都会把
        `recovered_at` 写上，因此不会互相绕过。
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

    # -- 重发关联（契约 C3）：一个事务里完成「老记录 + 新记录 + 关联」---------

    def claim_for_resend(
        self,
        old_id: str,
        new_id: str,
        instance_id: str | None = None,
        *,
        prepare: Callable[[], Any] | None = None,
    ) -> bool:
        """把一条未执行的记录接管给一个新 turn：**单事务**、只能成功一次。

        一个事务里做三件事（契约 C3）：

        1. 带条件 UPDATE 老记录：必须是「用户没处理过的 interrupted 行」
           （与 `recoverable()` 同一个权威谓词），写 `recovered_at`；
        2. 插入新记录（新 turn 立刻就有跨重启痕迹，restart 也能找回它）；
        3. 写关联 `recovered_by = new_id`。

        任何一步失败 → 整个事务回滚：**不存在**「老记录被标成已重发、新记录却没写成」
        的半截状态（那正是孤儿记录的来源）。并发两次重发时，`BEGIN IMMEDIATE`
        先取写锁 + 条件 UPDATE 的 rowcount 判定，保证只有一个后继生效。

        `prepare()` 在事务内、老记录抢到之后调用；它抛异常 = 这次重发失败整体回滚。
        调用方注意：`claim_for_resend` 已经持有 `BEGIN IMMEDIATE`（`conn.in_transaction`
        为真），所以 `prepare` 里再调 `transaction(conn)` 是安全的（会复用外层事务），
        但**不要**在里面自己 BEGIN/COMMIT —— 那会破坏「整体回滚」的前提。

        返回 False 表示这条记录不在可重发状态（或已经被别的请求抢走）。
        """
        owner = instance_id if instance_id is not None else self._owner_id()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "UPDATE turn_journal SET recovered_at = ?, updated_at = ? "
                f"WHERE turn_id = ? AND {_RECOVERABLE_CLAUSE}",
                (_now(), _now(), str(old_id), *_RECOVERABLE_PARAMS),
            )
            if int(cur.rowcount or 0) != 1:
                self.conn.execute("ROLLBACK")
                return False
            if prepare is not None:
                prepare()  # 抛异常则由下面统一回滚
            row = self.conn.execute(
                "SELECT message, topic_id, notify FROM turn_journal WHERE turn_id = ?",
                (str(old_id),),
            ).fetchone()
            message = str(row["message"]) if row is not None else ""
            topic_id = row["topic_id"] if row is not None else None
            notify = bool(row["notify"]) if row is not None else False
            moment = _now()
            self.conn.execute(
                "INSERT INTO turn_journal "
                "(turn_id, message, topic_id, notify, status, created_at, updated_at, "
                " owner_instance_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(new_id), message, topic_id, 1 if notify else 0, QUEUED, moment, moment, owner),
            )
            self.conn.execute(
                "UPDATE turn_journal SET recovered_by = ?, updated_at = ? WHERE turn_id = ?",
                (str(new_id), moment, str(old_id)),
            )
            self.conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 回滚本身失败时保留原异常
                pass
            logger.error("turn journal claim_for_resend failed: %s", exc)
            raise JournalWriteError(f"turn journal claim_for_resend failed: {exc}") from exc
        except BaseException:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            raise
        if owner and self.registry is not None:
            try:
                self.registry.claim(RECORD_TURN, str(new_id), owner)
            except Exception:  # noqa: BLE001 - 归属登记失败不影响已经落库的关联
                logger.warning("turn journal owner claim for resend failed", exc_info=True)
        return True

    def orphaned_claims(self, limit: int = 50) -> list[dict[str, Any]]:
        """孤儿重发：`recovered_at` 非空、但 `recovered_by` 为空的遗留记录。

        它们是最坏的一种状态 —— 已经被「处理过」，界面上不再提示（`unfinished()`
        要求 `recovered_at IS NULL`），可实际上没有任何后继 turn。用户看不到、
        也点不到，消息就这样永久消失了。所以必须有一个明确的出口把它们列出来
        （`/api/runtime/state` 与维护日志用它），并允许 `repair_orphan()` 修复。
        """
        rows = self._query(
            f"SELECT * FROM turn_journal WHERE {_USER_INTERRUPTED_CLAUSE} "
            "AND recovered_at IS NOT NULL AND (recovered_by IS NULL OR recovered_by = '') "
            "ORDER BY recovered_at ASC LIMIT ?",
            (*_USER_INTERRUPTED_PARAMS, max(1, int(limit))),
        )
        return [self._view(dict(r)) for r in rows]

    def repair_orphan(self, record_id: str, instance_id: str | None = None) -> bool:
        """让一条孤儿记录**重新可重发**（清掉那次失败的抢占），返回是否改到了行。

        只作用于「孤儿」这一种精确状态：`interrupted` + 用户行 + `recovered_at`
        非空 + `recovered_by` 空。已经真正重发过的行（`recovered_by` 非空）不动 ——
        否则就会出现「一条消息被重发两次」。

        修复后的记录**不归任何实例**：归属表里那一行会被清掉（修复前的归属是
        「抢占的人」，它已经失败了）。这一点很关键：如果修复后把归属记到当前实例
        名下，这条记录就会因为「主人还活着」而重新从恢复入口消失 —— 刚修好的东西
        又看不见了。`instance_id` 只用于日志审计（保留参数以兼容既有调用方）。
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "UPDATE turn_journal SET recovered_at = NULL, updated_at = ? "
                f"WHERE turn_id = ? AND {_USER_INTERRUPTED_CLAUSE} "
                "AND recovered_at IS NOT NULL AND (recovered_by IS NULL OR recovered_by = '')",
                (_now(), str(record_id), *_USER_INTERRUPTED_PARAMS),
            )
            changed = int(cur.rowcount or 0) == 1
            if changed:
                self._release_owner(str(record_id))
            self.conn.execute("COMMIT" if changed else "ROLLBACK")
        except sqlite3.Error as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            logger.warning("turn journal repair_orphan failed: %s", exc)
            return False
        if changed:
            logger.info(
                "turn journal orphan repaired: record=%s by=%s", record_id, instance_id
            )
        return changed

    def _release_owner(self, record_id: str) -> None:
        """清掉一条记录在归属表里的登记（它不再属于任何实例）。"""
        if self.registry is not None:
            try:
                self.registry.release(RECORD_TURN, record_id)
                return
            except Exception:  # noqa: BLE001 - 归属清理失败不撤销已经落库的修复
                logger.warning("turn journal owner release failed", exc_info=True)
        try:
            self.conn.execute(
                "DELETE FROM record_owners WHERE record_type = ? AND record_id = ?",
                (RECORD_TURN, record_id),
            )
        except sqlite3.Error:
            logger.warning("turn journal owner release mirror failed", exc_info=True)

    def take_over(self, record_id: str, *, expected_status: str, instance_id: str | None = None,
                  dead_owner: str | None = None) -> bool:
        """把一条**开放状态**（queued / running）的历史记录接管成本实例的 interrupted。

        A01 的关键动作：升级前的历史行没有任何归属，`interrupt_stale()` 对它们
        一律「保守保留」（`_interrupt_owned` 里 owner 为空的行走 unknown 分支），
        于是它们既不在 `unfinished()` 里（那要求 `interrupted`），也无法重发 ——
        消息在库里有原文，但用户在界面上永远点不到。用户明确点「继续」时，
        才由这里把这一条精确地接管过来。

        条件（契约 §2.1 冻结）：

        1. `status = expected_status`：客户端看到的状态必须还是当前状态
           （并发 / 状态已变化 → 命中 0 行，返回 False，**一行都不改**）；
        2. `recovered_at IS NULL`：已经进入过重发流程的行不走这条路；
        3. 归属条件：`owner_instance_id IS NULL`（无归属）**或**
           `owner_instance_id = dead_owner`（有归属、但归属者已确认退出）。
           有归属且不是「已完成退出」的一律拒绝 —— 活实例 / 判不出来的记录
           绝不能被抢（unknown 不改状态）。

        `dead_owner` 只由调用方在 `registry.owner_alive(owner) is False` 时传入：
        归属者已经确认退出时，它留下的 running/queued 行才是可安全接管的。
        """
        owner = instance_id if instance_id is not None else self._owner_id()
        moment = _now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "UPDATE turn_journal SET status = ?, reason = ?, owner_instance_id = ?, "
                "updated_at = ? WHERE turn_id = ? AND status = ? AND recovered_at IS NULL "
                "AND owner_instance_id IS NULL",
                (INTERRUPTED, TAKEOVER_REASON, owner, moment, str(record_id),
                 str(expected_status)),
            )
            changed = int(cur.rowcount or 0) == 1
            if not changed and dead_owner:
                cur = self.conn.execute(
                    "UPDATE turn_journal SET status = ?, reason = ?, owner_instance_id = ?, "
                    "updated_at = ? WHERE turn_id = ? AND status = ? AND recovered_at IS NULL "
                    "AND owner_instance_id = ?",
                    (INTERRUPTED, TAKEOVER_REASON, owner, moment, str(record_id),
                     str(expected_status), str(dead_owner)),
                )
                changed = int(cur.rowcount or 0) == 1
            self.conn.execute("COMMIT" if changed else "ROLLBACK")
        except sqlite3.Error as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 回滚本身失败时保留原异常
                pass
            logger.warning("turn journal take_over failed: %s", exc)
            return False
        if changed and owner:
            self._claim_owner(str(record_id), str(owner))
        return changed

    def _claim_owner(self, record_id: str, instance_id: str) -> None:
        """把归属写进 `record_owners`（契约 §2.1：接管后归属必须可追踪）。

        有 registry 走它（生产路径）；没有时直接写同一张表、同一个主键 ——
        归属登记失败不撤销已经落库的接管，只记一条 warning。
        """
        if self.registry is not None:
            try:
                self.registry.claim(RECORD_TURN, record_id, instance_id)
                return
            except Exception:  # noqa: BLE001 - 归属登记失败不撤销已经落库的接管
                logger.warning("turn journal ownership claim failed", exc_info=True)
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO record_owners "
                "(record_type, record_id, instance_id) VALUES (?, ?, ?)",
                (RECORD_TURN, record_id, instance_id),
            )
        except sqlite3.Error:
            logger.warning("turn journal ownership mirror failed", exc_info=True)

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
        """用户选择「知道了」：不再提示，但仍然保留记录（不删用户消息）。

        一次带条件的 UPDATE（与 `recoverable()` 同一个权威谓词），写成
        `status = 'dismissed'` 的终态：

        * 它不再是 `interrupted`，所以既不会被 `orphaned_claims()` 当成孤儿，
          也不能被 `repair_orphan()` 复活成「可重发」（否则用户说过的
          「知道了」会被一个修复动作推翻，那条消息会被再执行一次）；
        * 原文、topic、recovered_at 全部保留（不删用户数据）；
        * 重复点击返回 False（第二次不再是可恢复状态）→ HTTP 409。
        """
        try:
            cur = self.conn.execute(
                "UPDATE turn_journal SET status = ?, recovered_at = ?, updated_at = ? "
                f"WHERE turn_id = ? AND {_RECOVERABLE_CLAUSE}",
                (DISMISSED, _now(), _now(), str(turn_id), *_RECOVERABLE_PARAMS),
            )
        except sqlite3.Error as exc:  # noqa: BLE001 - 台账失败不得影响接口可用性
            logger.warning("turn journal dismiss failed: %s", exc)
            return False
        return int(cur.rowcount or 0) == 1

    # -- 清理 -------------------------------------------------------------

    def prune_terminal(self, days: int | None = None) -> int:
        """清掉过期终态行（只为「不重复执行已完成 turn」作证，不需要永久保留）。

        `interrupted` 行不会被清：它承载用户还没看到的消息原文。
        """
        retention = self.retention_days if days is None else int(days)
        if retention <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention)).isoformat()
        try:
            cur = self.conn.execute(
                "DELETE FROM turn_journal WHERE status IN (?, ?, ?, ?) AND ended_at IS NOT NULL "
                "AND ended_at < ?",
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
