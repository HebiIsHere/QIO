"""实例归属（契约 C1）：判断一条记录的主人是不是还活着。

缺陷背景
--------

启动恢复以前只有一句话：把库里所有 `pending` / `queued` / `running` 标成
`interrupted`，把卡在 `running` 的派生任务放回 `pending`。那句话隐含了一个
假设：**库只有一个写入者**。实际不是：

* 用户开了第二个后端实例（或多开窗口），新实例一启动就把**还在跑**的那个
  实例的任务和待确认事项全标成中断；
* 重启期间旧进程可能还没退干净，恢复逻辑会重复认领同一批派生任务。

修法不是「加一个 pid 列」，而是给**每条记录一个归属**，再用一个**多判据**的
存活判定决定「这个归属者现在算不算活着」：

======================  ============================================
显式 `exited_at` 非空    死（进程自己登记了干净退出）
心跳新鲜（<= TTL）       活
心跳过期 **且** pid 不存在 死
其余                     unknown（未知）
======================  ============================================

**unknown 一律不改状态**：判不出来就什么都不做，等下一次维护重新判定。
只有「确认已退出」的归属者的记录才允许被标 interrupted / 重新认领。

为什么不能只看一种判据
----------------------

* 只看 PID：pid 会被复用；容器/多机场景 pid 根本不在本机。
* 只看时间：长任务（一次摘要可能跑几分钟）心跳不刷新就会被误杀；
  而崩溃进程的 pid 立刻就不存在了，时间阈值只是兜底。
* 所以是「显式退出 > 心跳 > pid > unknown」的组合，判不出来就承认判不出来。

归属表 `record_owners` 一张表覆盖所有记录类型（turn / approval / 派生任务），
避免每种记录各自长一列 owner 而彼此口径不一。写入侧（谁创建这条记录）
调 `claim()`；恢复侧（谁该动这条记录）调 `owner_alive()`。
"""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from agent.storage.db import transaction

logger = logging.getLogger(__name__)

# 心跳有效期：超过它且 pid 也确认不存在，才算死。90 秒是「维护间隔的数倍」，
# 不是「任务时长」—— 长任务不需要刷心跳，因为它的 pid 还在。
HEARTBEAT_TTL_SECONDS = 90.0

# 记录类型（record_owners.record_type）——写在这里只写一次，避免各处拼字符串。
RECORD_TURN = "turn"
RECORD_APPROVAL = "approval"
RECORD_DERIVED_TASK = "derived_task"

DEFAULT_HOST = "127.0.0.1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _pid_is_clearly_absent(exc: OSError) -> bool:
    """「这个 pid 不存在」的平台判定（跨平台，不靠猜）。

    * POSIX：`ProcessLookupError`；
    * Windows：pid 不存在时 `OpenProcess` 返回「参数错误」（WinError 87 / errno 22）。
      这条在 Windows 上**必须**认，否则崩溃进程的 pid 永远判不出来，
      「心跳过期 + pid 不存在」这条判据会退化成永远 unknown。
    """
    if isinstance(exc, ProcessLookupError):
        return True
    if getattr(exc, "winerror", None) == 87:
        return True
    return getattr(exc, "errno", None) in (22,)  # EINVAL


def _windows_pid_alive(pid: int) -> bool | None:
    """Windows：用 OpenProcess 只读查询，**不调用 os.kill**。

    为什么不用 `os.kill(pid, 0)`：在 Windows 上它不是一个纯查询 —— 对系统 pid
    可能返回 `SystemError` 甚至让解释器异常退出（本仓库实测把 pytest 进程带走）。
    存活探测是维护路径上的旁路判断，绝不能有这种副作用。

    返回：pid 不存在 → False；存在但拒绝访问 → True（进程在，只是不属于我们）；
    其它意外 → None。
    """
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    ERROR_ACCESS_DENIED = 5
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        kernel32.CloseHandle(handle)
        return True
    error = int(kernel32.GetLastError())
    if error == ERROR_INVALID_PARAMETER:
        return False  # 没有这个进程
    if error == ERROR_ACCESS_DENIED:
        return True  # 进程存在，只是不允许我们查询
    return None


def _default_pid_alive(pid: int) -> bool | None:
    """这个 pid 在本机还在吗？返回 None = 判断不了（绝不当成「死」）。

    pid 不存在 → False；没有权限说明进程存在 → True；意外错误 → None。
    **判不出来就承认判不出来**：unknown 不改任何状态。

    B02：**先分平台，再判特殊 PID**。以前是无条件 `pid <= 4 → None`，于是
    POSIX 上 1 / 2 / 3 / 4（init、kthreadd、ksoftirqd、kworker —— 都是合法 PID）
    永远判不出来：一个归属者在容器里恰好是 1 号进程时，「心跳过期 + pid 不存在」
    这条判据永远退化成 unknown，它的记录也就永远无法被恢复。
    只有 Windows 上 0/4 才是内核伪 pid，那条 `<= 4 → None` 只属于 Windows。
    """
    try:
        ivalue = int(pid)
    except (TypeError, ValueError):
        return None
    if ivalue <= 0:
        # 0 / 负数在任何平台上都没有意义。
        return None
    if os.name == "nt":
        if ivalue <= 4:
            # Windows：0/4 是内核伪 pid，查询行为不可靠（且绝不能用 os.kill）。
            return None
        try:
            return _windows_pid_alive(ivalue)
        except Exception:  # noqa: BLE001 - 探测失败一律 unknown
            return None
    try:
        os.kill(ivalue, 0)  # POSIX：信号 0 是纯存在性检查
    except OSError as exc:
        if _pid_is_clearly_absent(exc):
            return False
        if isinstance(exc, PermissionError):
            return True
        return None
    except Exception:  # noqa: BLE001
        return None
    return True


class InstanceRegistry:
    """本实例的身份 + 全库实例的存活判定 + 记录归属。

    `clock` 可注入：受控测试用假时钟制造「心跳过期」而不真的等待
    （时间阈值是判据之一，但测试不该真的 sleep 90 秒）。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        instance_id: str | None = None,
        *,
        pid: int | None = None,
        host: str | None = None,
        heartbeat_ttl: float = HEARTBEAT_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        pid_alive: Callable[[int], bool | None] | None = None,
    ) -> None:
        self.conn = conn
        self.instance_id = str(instance_id or f"qio_{uuid.uuid4().hex[:16]}")
        self.pid = int(os.getpid() if pid is None else pid)
        self.host = str(host if host is not None else socket.gethostname() or DEFAULT_HOST)
        self.heartbeat_ttl = float(heartbeat_ttl)
        self._clock = clock or _now
        self._pid_alive = pid_alive or _default_pid_alive

    def now(self) -> datetime:
        return self._clock()

    # -- 本实例的生命周期 --------------------------------------------------

    def start(self) -> str:
        """登记本实例（幂等：同一个 id 再 start 只刷新心跳，不重置 started_at）。"""
        moment = _iso(self.now())
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT instance_id FROM instances WHERE instance_id = ?",
                (self.instance_id,),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO instances "
                    "(instance_id, pid, host, started_at, last_heartbeat, exited_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL)",
                    (self.instance_id, self.pid, self.host, moment, moment),
                )
            else:
                self.conn.execute(
                    "UPDATE instances SET pid = ?, host = ?, last_heartbeat = ?, "
                    "exited_at = NULL WHERE instance_id = ?",
                    (self.pid, self.host, moment, self.instance_id),
                )
        return self.instance_id

    def heartbeat(self) -> None:
        """刷新心跳。维护循环定期调它 —— 它就是「我还活着」的证据。"""
        self.conn.execute(
            "UPDATE instances SET last_heartbeat = ?, pid = ?, host = ? WHERE instance_id = ?",
            (_iso(self.now()), self.pid, self.host, self.instance_id),
        )

    def mark_clean_exit(self) -> None:
        """干净退出：显式写 exited_at。这比任何时间/pid 推断都权威。

        幂等：已经写过就不改（首写时间才是退出时刻）。
        """
        self.conn.execute(
            "UPDATE instances SET exited_at = COALESCE(exited_at, ?) WHERE instance_id = ?",
            (_iso(self.now()), self.instance_id),
        )

    # -- 存活判定 ---------------------------------------------------------

    def instance_row(self, instance_id: str) -> sqlite3.Row | None:
        if not instance_id:
            return None
        return self.conn.execute(
            "SELECT * FROM instances WHERE instance_id = ?", (str(instance_id),)
        ).fetchone()

    def owner_alive(self, instance_id: str | None) -> bool | None:
        """归属者现在算不算活着。

        返回 False 只代表**确认已退出**；判不出来一律 None —— 调用方不得把
        None 当作死（契约 C1：unknown 不改状态）。
        """
        if not instance_id:
            return None
        row = self.instance_row(str(instance_id))
        if row is None:
            # 库里有它的记录、但实例表里没有它：说明记录来自「没有实例身份」的
            # 旧版本。判不出来，绝不当成已死。
            return None
        if row["exited_at"]:
            return False
        ttl = timedelta(seconds=self.heartbeat_ttl)
        try:
            last = datetime.fromisoformat(str(row["last_heartbeat"]))
        except (TypeError, ValueError):
            return None
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if self.now() - last <= ttl:
            return True
        # 心跳过期：还要 pid 确认不存在才算死。pid 为 NULL / 判不出来 → unknown。
        pid = row["pid"]
        if pid is None:
            return None
        if str(row["host"] or "") != self.host:
            # 别的机器上的 pid 与本机无关，不能拿本机探测结果下结论。
            return None
        return self._pid_alive(int(pid))

    def live_instance_ids(self) -> list[str]:
        """**确认活着**的实例 id（unknown 不在其中：它不是「活」，只是「判不出来」）。"""
        out: list[str] = []
        for row in self.conn.execute("SELECT instance_id FROM instances").fetchall():
            if self.owner_alive(str(row["instance_id"])) is True:
                out.append(str(row["instance_id"]))
        return out

    def classify_instances(self) -> dict[str, list[str]]:
        """当前全库实例的三分类（受控验收与维护日志共用同一口径）。"""
        report: dict[str, list[str]] = {"alive": [], "dead": [], "unknown": []}
        for row in self.conn.execute("SELECT instance_id FROM instances").fetchall():
            instance_id = str(row["instance_id"])
            state = self.owner_alive(instance_id)
            if state is True:
                report["alive"].append(instance_id)
            elif state is False:
                report["dead"].append(instance_id)
            else:
                report["unknown"].append(instance_id)
        return report

    # -- 记录归属 ---------------------------------------------------------

    def claim(self, record_type: str, record_id: str, instance_id: str | None = None) -> None:
        """登记「这条记录属于谁」。同一条记录重复登记以最后一次为准（幂等）。"""
        if record_id is None or str(record_id) == "":
            return
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO record_owners (record_type, record_id, instance_id) "
                "VALUES (?, ?, ?)",
                (str(record_type), str(record_id), str(instance_id or self.instance_id)),
            )

    def owner_instance_id(self, record_type: str, record_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT instance_id FROM record_owners WHERE record_type = ? AND record_id = ?",
            (str(record_type), str(record_id)),
        ).fetchone()
        return str(row["instance_id"]) if row is not None else None

    def release(self, record_type: str, record_id: str) -> None:
        """撤销归属（记录已经终结、不再需要判定时用）。"""
        self.conn.execute(
            "DELETE FROM record_owners WHERE record_type = ? AND record_id = ?",
            (str(record_type), str(record_id)),
        )

    def owned_record_ids(self, record_type: str, instance_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT record_id FROM record_owners WHERE record_type = ? AND instance_id = ?",
            (str(record_type), str(instance_id)),
        ).fetchall()
        return [str(row["record_id"]) for row in rows]

    # -- 启动恢复的判定入口 ------------------------------------------------

    def recover_confirmed_dead(
        self,
        *,
        types: tuple[str, ...],
        handler: Callable[[str, str], int],
    ) -> dict[str, object]:
        """只对**确认已退出**实例的记录调 handler（unknown 一律不动）。

        `handler(instance_id, record_type) -> int`：由调用方执行「把这些记录标成
        中断 / 放回可重试」，返回处理的条数。本模块不认识任何具体表的语义。

        返回报告（供启动日志与验收断言）：alive/dead/unknown 实例清单、
        逐实例处理的条数、以及**保守保留**（归属者未知、因此没动）的条数。
        """
        report = self.classify_instances()
        handled: dict[str, object] = {}
        deferred: dict[str, object] = {}
        for instance_id, state in [
            *[(i, "dead") for i in report["dead"]],
            *[(i, "unknown") for i in report["unknown"]],
        ]:
            if state == "dead":
                total = 0
                for record_type in types:
                    ids = self.owned_record_ids(record_type, instance_id)
                    if not ids:
                        continue
                    try:
                        total += int(handler(instance_id, record_type))
                    finally:
                        # 归属已经用掉了：清掉它，避免下次维护重复处理同一批记录。
                        # 处理失败也清（记一条 warning），否则会把它永远留成待办。
                        try:
                            with transaction(self.conn):
                                self.conn.execute(
                                    "DELETE FROM record_owners "
                                    "WHERE record_type = ? AND instance_id = ?",
                                    (str(record_type), str(instance_id)),
                                )
                        except sqlite3.Error:
                            logger.warning("record owner cleanup failed", exc_info=True)
                if total:
                    handled[instance_id] = total
            else:
                # unknown：保守保留，只记「有多少条等着以后重判」，不改任何状态。
                counts = sum(
                    len(self.owned_record_ids(record_type, instance_id)) for record_type in types
                )
                if counts:
                    deferred[instance_id] = counts
        report["handled"] = handled
        report["deferred"] = deferred
        return report
