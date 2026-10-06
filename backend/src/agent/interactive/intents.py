"""QIO 意图与审批生命周期（子智能体 C 负责实现）。

契约：docs/interactive-mode-contract.md §1.6 / §3。

要点：

- QIO 对正式板面的任何改动都必须经过审批；批准后才成为任务，拒绝后预览消失、原内容保留；
- 互不相容的结果不能同时批准；依赖任务即使提前批准，也要等前项成功并展示结果 + 用户再次确认；
- 相关材料变化 → 待审批预览标记 needs_update 并禁止批准；
- 失败 / 取消撤回该任务造成的改动，保留用户后续修改，并说明未撤回部分；
- 不自动重试；重启后 running → paused，由用户决定是否继续。

占位实现只保证「能 import、界面不报错」，C 负责替换为完整实现。
"""

from __future__ import annotations

import sqlite3


def on_new_submission(
    conn: sqlite3.Connection, *, board_id: str, submission_id: str, expressions: list[dict]
) -> list[str]:
    """把受本次提交影响、仍在等待审批的意图标记为 needs_update。"""
    return []


def create_demo_intents(conn: sqlite3.Connection, *, board_id: str) -> list[dict]:
    raise NotImplementedError("intents.create_demo_intents 由 C 实现")


def list_intents(conn: sqlite3.Connection, board_id: str) -> dict:
    return {"intents": [], "conflicts": [], "batchAvailable": False, "recovered": []}


def approve_intent(conn: sqlite3.Connection, intent_id: str, *, confirm_dependency: bool = False) -> dict:
    raise NotImplementedError("intents.approve_intent 由 C 实现")


def reject_intent(conn: sqlite3.Connection, intent_id: str) -> dict:
    raise NotImplementedError("intents.reject_intent 由 C 实现")


def update_preview(conn: sqlite3.Connection, intent_id: str, preview: dict) -> dict:
    raise NotImplementedError("intents.update_preview 由 C 实现")


def advance_intent(conn: sqlite3.Connection, intent_id: str, *, outcome: str) -> dict:
    raise NotImplementedError("intents.advance_intent 由 C 实现")


def batch_decide(conn: sqlite3.Connection, *, approve: list[str], reject: list[str]) -> dict:
    raise NotImplementedError("intents.batch_decide 由 C 实现")


def recover_running_intents(conn: sqlite3.Connection, board_id: str) -> dict:
    return {"paused": []}
