"""A 组回归：任务栏的「失败归属」与「需要处理」计数（问题 1 / 4 / 5）。

本文件锁住三条新的产品口径（2026-11-09 修复轮）：

* **问题 1**：只有「同一轮 + 同一工具 + 同一操作身份」（run_shell 的 cmd+cwd、
  web_search 的 query、web_fetch 的 url）的后续执行，才能收口前一次失败。
  不同操作（复制文件 vs 查看目录、不同查询、不同 cwd、不同轮次）互不覆盖；
  没有可追踪操作身份时按独立记录处理，不靠工具相同 / 时间接近 / 同一轮猜关联。
* **问题 4**：「需要处理」只统计当前仍需用户处理的事项。点过「知道了」的失败
  移入历史后保留真实结果，但不再计入 counts 的 failed / waiting 数量（单项与多项）。
* **问题 5**：最终失败的派生整理 / 记忆维护任务即使没有结束时间，用户已知晓后也
  按真实终态进历史（不补造成功或结束时间）；自动重试中的任务保持「排队中」，
  不会因为一次确认动作被误归档。

所有断言只用程序字段与真实 DB 事实；不联网、不调模型、不依赖错误文本关键词。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.storage import tool_records


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


# ── 装置：真实写入 tool_records / turn_journal / 派生任务 ────────────────────


def _end_turn(conn: sqlite3.Connection, turn_id: str, status: str = "completed") -> None:
    moment = _now()
    conn.execute(
        "INSERT OR REPLACE INTO turn_journal "
        "(turn_id, message, topic_id, notify, status, created_at, started_at, ended_at, updated_at) "
        "VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?)",
        (turn_id, "测试消息", status, moment, moment, moment, moment),
    )
    conn.commit()


def _record(
    conn: sqlite3.Connection,
    turn_id: str,
    tool_name: str,
    *,
    seq: int = 1,
    status: str = "failed",
    error: str = "",
    code: str | None = None,
    category: str | None = None,
    recoverable: bool | None = None,
    arguments: object | None = None,
    output: str = "",
) -> str:
    record_id = tool_records.record_tool_call(
        conn,
        turn_id=turn_id,
        tool_name=tool_name,
        arguments=arguments if arguments is not None else {},
        output=output,
        status=status,
        error=error,
        duration_ms=1200,
        seq=seq,
        code=code,
        category=category,
        recoverable=recoverable,
        result_status=status,
    )
    assert record_id, "工具记录没有写进去（装置失效）"
    return record_id


def _feed(client, view: str = "current", limit: int = 100) -> dict:
    response = client.get(f"/api/tasks?view={view}&limit={limit}")
    assert response.status_code == 200, response.text
    return response.json()


def _ids(client, view: str) -> set[str]:
    return {item["id"] for item in _feed(client, view)["items"]}


def _find(client, task_id: str, view: str) -> dict | None:
    for item in _feed(client, view)["items"]:
        if item["id"] == task_id:
            return item
    return None


def _derived(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    state: str,
    run_after: str | None,
    kind: str = "summary",
) -> str:
    now = _now()
    conn.execute(
        "INSERT INTO derived_tasks "
        "(id, kind, fragment_id, content_version, state, attempts, last_error, run_after, "
        " created_at, updated_at) "
        "VALUES (?, ?, 'frag_regression', 1, ?, 2, '模型调用失败', ?, ?, ?)",
        (task_id, kind, state, run_after, now, now),
    )
    conn.commit()
    return f"derived:{task_id}"


def _maintenance(conn: sqlite3.Connection, batch_id: str) -> str:
    conn.execute(
        "INSERT INTO maintenance_batches "
        "(id, trigger_reason, snapshot, status, model, created_at, finished_at, stats, error) "
        "VALUES (?, 'manual', '{}', 'failed', 'fake-model', ?, NULL, '{}', '整理失败')",
        (batch_id, _now()),
    )
    conn.commit()
    return f"maintenance:{batch_id}"


# ── 问题 1：不同操作不能被当成同一项工作的重试 ─────────────────────────────


def test_different_command_success_does_not_hide_timeout_verification(client):
    """「复制文件」超时后「查看当前目录」成功：不能认定复制完成，待核查提醒必须留着。"""
    ctx = client.app.state.ctx
    turn = "turn_copy_then_dir"
    _end_turn(ctx.conn, turn)
    timed_out = _record(
        ctx.conn, turn, "run_shell", seq=1,
        error="命令执行超时", code="timeout", category="timeout", recoverable=True,
        arguments={"cmd": "copy report.csv D:\\backup\\", "cwd": "D:\\work"},
    )
    _record(
        ctx.conn, turn, "run_shell", seq=2, status="success", output="目录列表",
        arguments={"cmd": "dir", "cwd": "D:\\work"},
    )

    item = _find(client, f"tool:{timed_out}", "current")
    assert item is not None, "不同命令的成功不能把待核查的超时提醒撤掉"
    assert item["requiresAttention"] is True
    assert item["status"] == "failed"
    assert _feed(client)["counts"]["failed"] == 1

    # 执行记录本身仍是 failed + 超时事实（过滤/展示不删除、不改写记录）。
    record = client.get(f"/api/tool-records/{timed_out}").json()
    assert record["status"] == "failed" and record["code"] == "timeout"


def test_same_operation_retry_success_retires_previous_timeout(client):
    """同一操作（同 cmd + 同 cwd）重试成功：旧超时提醒可以被收起。"""
    ctx = client.app.state.ctx
    turn = "turn_copy_retry"
    _end_turn(ctx.conn, turn)
    timed_out = _record(
        ctx.conn, turn, "run_shell", seq=1,
        error="命令执行超时", code="timeout", category="timeout", recoverable=True,
        arguments={"cmd": "copy report.csv D:\\backup\\", "cwd": "D:\\work"},
    )
    retried = _record(
        ctx.conn, turn, "run_shell", seq=2, status="success", output="已复制",
        # 重试只调整了超时参数：操作身份（cmd+cwd）不变，仍是同一项工作的重试。
        arguments={"cmd": "copy report.csv D:\\backup\\", "cwd": "D:\\work", "timeout": 120},
    )

    assert f"tool:{timed_out}" not in _ids(client, "current")
    assert f"tool:{timed_out}" not in _ids(client, "history")
    assert f"tool:{retried}" in _ids(client, "current"), "成功执行仍按既有规则展示"
    assert _feed(client)["counts"]["failed"] == 0


def test_same_operation_failed_retry_only_latest_is_shown(client):
    """同一操作连续失败：重试链只保留最新一次（前一次是被重试取代，不是被其它操作覆盖）。"""
    ctx = client.app.state.ctx
    turn = "turn_same_cmd_twice"
    _end_turn(ctx.conn, turn)
    first = _record(
        ctx.conn, turn, "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "python migrate.py", "cwd": "D:\\work"},
    )
    second = _record(
        ctx.conn, turn, "run_shell", seq=2, error="缺少依赖 pandas",
        code="missing_dependency", category="missing_dependency", recoverable=False,
        arguments={"cmd": "python migrate.py", "cwd": "D:\\work"},
    )

    ids = _ids(client, "current")
    assert f"tool:{first}" not in ids
    assert f"tool:{second}" in ids
    assert _feed(client)["counts"]["failed"] == 1


def test_same_command_with_different_cwd_is_a_different_operation(client):
    """命令文本相同但工作目录不同：是不同的操作，成功不能收口前一次超时。"""
    ctx = client.app.state.ctx
    turn = "turn_different_cwd"
    _end_turn(ctx.conn, turn)
    timed_out = _record(
        ctx.conn, turn, "run_shell", seq=1,
        error="命令执行超时", code="timeout", category="timeout", recoverable=True,
        arguments={"cmd": "npm run build", "cwd": "D:\\project-a"},
    )
    _record(
        ctx.conn, turn, "run_shell", seq=2, status="success", output="构建完成",
        arguments={"cmd": "npm run build", "cwd": "D:\\project-b"},
    )

    item = _find(client, f"tool:{timed_out}", "current")
    assert item is not None, "不同工作目录的构建不是同一项工作的重试"
    assert item["requiresAttention"] is True


def test_same_command_success_in_later_turn_does_not_hide_previous_timeout(client):
    """跨轮次没有可追踪的重试关系（可能是之后另一次独立操作），不能推定解决。"""
    ctx = client.app.state.ctx
    _end_turn(ctx.conn, "turn_copy_first")
    _end_turn(ctx.conn, "turn_copy_second")
    timed_out = _record(
        ctx.conn, "turn_copy_first", "run_shell", seq=1,
        error="命令执行超时", code="timeout", category="timeout", recoverable=True,
        arguments={"cmd": "copy report.csv D:\\backup\\", "cwd": "D:\\work"},
    )
    _record(
        ctx.conn, "turn_copy_second", "run_shell", seq=1, status="success", output="已复制",
        arguments={"cmd": "copy report.csv D:\\backup\\", "cwd": "D:\\work"},
    )

    item = _find(client, f"tool:{timed_out}", "current")
    assert item is not None, "下一轮的同名命令不能自动证明上一轮超时命令的结果"


def test_untrackable_arguments_are_not_merged_with_other_calls(client):
    """参数无法解析 = 没有可追踪操作身份：两条失败各自独立，不靠同工具/同轮合并。"""
    ctx = client.app.state.ctx
    turn = "turn_untrackable"
    _end_turn(ctx.conn, turn)
    first = _record(
        ctx.conn, turn, "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments="不是 JSON 对象",
    )
    second = _record(
        ctx.conn, turn, "run_shell", seq=2, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments="也不是 JSON 对象",
    )

    ids = _ids(client, "current")
    assert {f"tool:{first}", f"tool:{second}"} <= ids, (
        "没有可追踪关联时两条失败都要保留，不能推定后者已解决前者"
    )
    assert _feed(client)["counts"]["failed"] == 2


# ── 问题 4：已知晓的历史失败不再计入「需要处理」──────────────────────────────


def test_acknowledged_failure_moves_to_history_and_drops_from_counts(client):
    ctx = client.app.state.ctx
    turn = "turn_ack_single"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "rm -rf build", "cwd": "D:\\work"},
    )
    task_id = f"tool:{failed}"

    current = _find(client, task_id, "current")
    assert current is not None and current["requiresAttention"] is True
    assert current["acknowledged"] is False
    assert _feed(client)["counts"]["failed"] == 1

    ack = client.post(f"/api/tasks/{task_id}/ack")
    assert ack.status_code == 200

    assert _find(client, task_id, "current") is None
    history = _find(client, task_id, "history")
    assert history is not None, "已知晓的失败保留真实记录并进入历史"
    assert history["status"] == "failed" and history["rawStatus"] == "failed"
    assert history["acknowledged"] is True
    assert _feed(client)["counts"]["failed"] == 0, "已知晓失败不再占需要处理数量"
    assert _feed(client, "history")["counts"]["failed"] == 1, (
        "历史视图的计数如实统计它自己包含的失败记录（数字与可见事项一致）"
    )

    detail = client.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200, "历史记录仍可查看原始结果"
    assert detail.json()["item"]["acknowledged"] is True


def test_acknowledging_one_of_two_failures_only_removes_one_count(client):
    ctx = client.app.state.ctx
    _end_turn(ctx.conn, "turn_ack_multi")
    first = _record(
        ctx.conn, "turn_ack_multi", "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "rm -rf build", "cwd": "D:\\work"},
    )
    second = _record(
        ctx.conn, "turn_ack_multi", "run_shell", seq=2,
        error="命令执行超时", code="timeout", category="timeout", recoverable=True,
        arguments={"cmd": "python migrate.py", "cwd": "D:\\work"},
    )
    assert _feed(client)["counts"]["failed"] == 2

    assert client.post(f"/api/tasks/tool:{first}/ack").status_code == 200
    counts = _feed(client)["counts"]
    assert counts["failed"] == 1, "多项确认只减掉已确认的那一项"
    assert _find(client, f"tool:{first}", "history") is not None
    assert _find(client, f"tool:{second}", "current") is not None

    assert client.post(f"/api/tasks/tool:{second}/ack").status_code == 200
    assert _feed(client)["counts"]["failed"] == 0
    assert _find(client, f"tool:{second}", "history") is not None


# ── 问题 5：最终失败的后台整理任务，已知晓后不依赖结束时间也要进历史 ────────────


@pytest.mark.parametrize("kind", ["summary", "knowledge", "entities"])
def test_acknowledged_final_failed_derived_task_moves_to_history_without_finished_at(client, kind):
    ctx = client.app.state.ctx
    task_id = _derived(
        ctx.conn, f"derived_final_failed_{kind}", state="failed", run_after=None, kind=kind
    )

    current = _find(client, task_id, "current")
    assert current is not None
    assert current["status"] == "failed" and current["requiresAttention"] is True
    assert current["finishedAt"] is None, "最终失败行本来就没有结束时间（不伪造）"
    assert _feed(client)["counts"]["failed"] == 1

    assert client.post(f"/api/tasks/{task_id}/ack").status_code == 200

    assert _find(client, task_id, "current") is None, (
        "已知晓的最终失败不能因为缺少结束时间继续留在当前任务"
    )
    history = _find(client, task_id, "history")
    assert history is not None
    assert history["status"] == "failed" and history["acknowledged"] is True
    assert history["finishedAt"] is None, "归档不能补造任意结束时间"
    assert _feed(client)["counts"]["failed"] == 0
    assert _feed(client, "history")["counts"]["failed"] == 1


def test_retrying_derived_task_stays_queued_and_is_not_archived(client):
    """自动重试中的任务显示排队中；确认动作不会把它误归档成历史失败。"""
    ctx = client.app.state.ctx
    run_after = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    task_id = _derived(ctx.conn, "derived_retrying", state="failed", run_after=run_after)

    current = _find(client, task_id, "current")
    assert current is not None
    assert current["status"] == "queued", "自动重试中是排队，不是最终失败"
    assert current["requiresAttention"] is False
    assert "自动重试" in current["rawStatus"]
    assert _feed(client)["counts"]["failed"] == 0

    assert client.post(f"/api/tasks/{task_id}/ack").status_code == 200
    assert _find(client, task_id, "current") is not None, "重试中的任务不能因为确认动作被归档"
    assert _find(client, task_id, "history") is None


def test_acknowledged_maintenance_failure_without_finished_at_moves_to_history(client):
    ctx = client.app.state.ctx
    task_id = _maintenance(ctx.conn, "mb_failed_no_end")

    current = _find(client, task_id, "current")
    assert current is not None
    assert current["status"] == "failed" and current["requiresAttention"] is True
    assert current["finishedAt"] is None
    assert _feed(client)["counts"]["failed"] == 1

    assert client.post(f"/api/tasks/{task_id}/ack").status_code == 200

    assert _find(client, task_id, "current") is None
    history = _find(client, task_id, "history")
    assert history is not None
    assert history["status"] == "failed" and history["acknowledged"] is True
    assert history["finishedAt"] is None
    assert _feed(client)["counts"]["failed"] == 0
