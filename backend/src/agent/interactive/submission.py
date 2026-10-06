"""可见范围、有效改动、提交前后状态（子智能体 B 负责实现）。

契约：docs/interactive-mode-contract.md §1.4 / §1.5 / §3。

硬约束（不许绕过）：

1. 保存不调用 QIO；**只有 `submit_board` 会让 QIO 拿到表达**。
2. 未勾选 / 明确隐藏的注释，其文字与链接都不得进入 before / after 状态；
   链接必须两端都可见才出现；一名可见成员都没有的组整体不出现。
3. 提交成功才更新「上次成功提交」基准；失败保留改动与本次注释选择。
4. 空提交 / 重复点击必须幂等：不更新基准、不调用 QIO。

占位实现只保证「能 import」，B 负责替换为完整实现。
"""

from __future__ import annotations

import sqlite3


def visible_range(state: dict) -> dict:
    raise NotImplementedError("submission.visible_range 由 B 实现")


def project_snapshot(state: dict) -> dict:
    raise NotImplementedError("submission.project_snapshot 由 B 实现")


def diff_states(before: dict, after: dict) -> list[dict]:
    raise NotImplementedError("submission.diff_states 由 B 实现")


def last_success_baseline(conn: sqlite3.Connection, board_id: str) -> dict | None:
    raise NotImplementedError("submission.last_success_baseline 由 B 实现")


async def submit_board(
    conn: sqlite3.Connection,
    board_id: str,
    *,
    requested_visible: list[str] | None = None,
    note: str = "",
) -> dict:
    raise NotImplementedError("submission.submit_board 由 B 实现")
