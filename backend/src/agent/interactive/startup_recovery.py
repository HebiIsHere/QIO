"""互动执行的重启恢复（Phase 3 收口）。

启动时做两件程序事实：
1. 上一个进程留下的 board_intent_turns claiming/active 不再是可信活跃执行，统一释放；
2. 持久化的 pending 自动重试重新交给主循环受理（每系列一次），不重置次数。
不扫描模型、不猜状态；所有失败只记录并保持可重试。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from agent.interactive import intent_service, models, submission

logger = logging.getLogger(__name__)

MAX_STARTUP_RETRIES = 10


def release_stale_intent_turns(conn: sqlite3.Connection) -> int:
    """上一个进程的 claiming/active 都失去执行归属；启动时释放，避免永久挡路。"""
    try:
        with intent_service.transaction(conn):
            cursor = conn.execute(
                "UPDATE board_intent_turns SET status = 'failed', reason = ?, updated_at = ?"
                " WHERE status IN ('claiming', 'active')",
                ("startup_recovered", models.now_iso()),
            )
            return int(cursor.rowcount or 0)
    except sqlite3.Error:
        logger.warning("release stale interactive intent turns failed", exc_info=True)
        return 0


def resume_pending_retries(
    conn: sqlite3.Connection,
    turn_submitter: Callable[[str, str | None], Any],
    *,
    limit: int = MAX_STARTUP_RETRIES,
) -> dict[str, int]:
    """启动时恢复 pending 自动重试；不重置 attempts，不并行。"""
    started = 0
    skipped = 0
    try:
        rows = conn.execute(
            "SELECT series_id, intent_id, board_id FROM board_intent_retries"
            " WHERE retryable = 1 AND status = 'pending' AND attempts < retry_limit"
            " AND next_action = 'auto' ORDER BY updated_at, created_at LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
    except sqlite3.Error:
        logger.warning("list startup retries failed", exc_info=True)
        return {"started": 0, "skipped": 0}
    for row in rows:
        series_id = str(row["series_id"])
        claim = intent_service.claim_auto_retry(conn, series_id)
        if not claim.get("ok"):
            skipped += 1
            continue
        result = submission.start_interactive_turn(
            conn,
            turn_submitter=turn_submitter,
            board_id=str(claim.get("board_id") or ""),
            intent_ids=[str(claim.get("intent_id") or "")],
            submission_id=str(claim.get("submission_id") or ""),
            source="retry",
            resume=True,
            retry_series_id=series_id,
            retry_attempt=int(claim.get("attempt") or 1),
            retry_limit=int(claim.get("limit") or 3),
        )
        if result.get("ok") and result.get("turnId"):
            intent_service.set_retry_turn(conn, series_id, str(result["turnId"]))
            started += 1
        else:
            # 临时不可用不消耗自动次数：放回 pending，等下一轮主循环收尾再试。
            intent_service.release_retry(
                conn,
                series_id,
                status="pending",
                reason=str(result.get("reason") or "startup_retry_unavailable"),
            )
            skipped += 1
    return {"started": started, "skipped": skipped}


def recover_startup(
    conn: sqlite3.Connection,
    turn_submitter: Callable[[str, str | None], Any],
) -> dict[str, int]:
    released = release_stale_intent_turns(conn)
    resumed = resume_pending_retries(conn, turn_submitter)
    if released or resumed.get("started"):
        logger.info("interactive startup recovery released=%s resumed=%s", released, resumed)
    return {"released": released, **resumed}
