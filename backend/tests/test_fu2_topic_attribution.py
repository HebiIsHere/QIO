# -*- coding: utf-8 -*-
"""FU2-A 回归：确认 / 恢复 / 重试启动续作 Turn 的话题归属。

问题（基线 373fda40）：`api/interactive_intents.py::_start_intent_turn` 把
**当前选中话题**传给 `submission.start_interactive_turn`，而后者只在话题为空时
才回退到原提交 binding.resumeState.topicId。用户在确认前切换话题，续作就会被
写进新话题。

契约（COMMON §4.1 / BRIEF-a）：
* 续作 Turn 的话题只认原提交保存的 binding.resumeState.topicId，当前话题不得覆盖；
* 无法确认归属（无 submission 绑定 / topicId 为空 / 绑定缺失）→ 明确失败
  code=topic_unresolved，不启动 Turn、不写绑定、不回退当前话题；
* 已在执行的意图复用既有 turnId（既有行为）；已完成 / 已取消不得再次执行。

定向：真实临时库 + fake 主循环（不联网、不调用模型、不出现密钥）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module
from agent.interactive import intent_service, models, submission

from test_p3_b_support import add_submission, intent_scope, intent_spec, make_board, prepare

BOARD = "board_fu2a"
SUBMISSION = "sub_fu2a"
SAVED_TOPIC = "topic-saved"
CURRENT_TOPIC = "topic-current"


def _seed_submission(conn, *, topic: str | None = SAVED_TOPIC) -> None:
    """模拟原互动轮已保存的提交事实：resumeState.topicId = 原话题。"""
    resume: dict[str, object] = {"kind": "submission"}
    if topic:
        resume["topicId"] = topic
    conn.execute(
        "UPDATE board_submissions SET resume_state = ? WHERE id = ?",
        (models.dumps(resume), SUBMISSION),
    )


def _approved_intent(conn, *, title: str = "整理材料") -> str:
    proposed = intent_service.propose_intents(
        conn,
        board_id=BOARD,
        submission_id=SUBMISSION,
        intents=[intent_spec(title=title, scope=intent_scope(allowed_actions=["create_card"]))],
    )
    assert proposed["ok"], proposed
    intent_id = proposed["intents"][0]["intent_id"]
    confirmed = intent_service.confirm_intents(conn, board_id=BOARD, intent_ids=[intent_id])
    assert confirmed["ok"], confirmed
    return intent_id


class FakeTurn:
    def __init__(self, turn_id: str, message: str = "", topic_id: str | None = None) -> None:
        self.turn_id = turn_id
        self.message = message
        self.topic_id = topic_id


class FakeTurns:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def submit(self, message: str, topic_id: str | None = None, **kwargs):
        turn = FakeTurn(f"turn_fake_{len(self.calls) + 1}", message, topic_id)
        self.calls.append({"message": message, "topicId": topic_id, "turn": turn})
        return turn


def _client(conn, turns, *, current_topic: str = CURRENT_TOPIC) -> TestClient:
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        conn=conn, turns=turns, current_topic=lambda: current_topic
    )
    app.include_router(api_module.router)
    return TestClient(app)


def _count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


@pytest.fixture()
def board_env(db_conn):
    prepare(db_conn)
    make_board(db_conn, board_id=BOARD)
    add_submission(db_conn, board_id=BOARD, submission_id=SUBMISSION)
    _seed_submission(db_conn)
    return db_conn


# --- 红：当前选中话题覆盖了原提交话题 ---------------------------------------


def test_confirm_start_uses_saved_submission_topic(board_env):
    """确认续作：原提交已保存 topic-saved，当前话题 topic-current 不得覆盖。"""
    conn = board_env
    intent_id = _approved_intent(conn)
    turns = FakeTurns()
    client = _client(conn, turns)

    response = client.post(
        f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["started"] is True, body
    assert turns.calls, "应当真实提交一次续作 Turn"
    assert turns.calls[0]["topicId"] == SAVED_TOPIC, "当前话题不得覆盖原提交话题"
    assert turns.calls[0]["turn"].board_topic_id == SAVED_TOPIC
    binding = submission.submission_binding(conn, SUBMISSION)
    assert binding is not None
    assert binding["resumeState"]["topicId"] == SAVED_TOPIC, "写回绑定也不得改成当前话题"


def test_batch_confirm_start_uses_saved_submission_topic(board_env):
    """同批确认同样按原提交话题执行。"""
    conn = board_env
    first_id = _approved_intent(conn, title="第一条")
    second_id = _approved_intent(conn, title="第二条")
    turns = FakeTurns()
    client = _client(conn, turns)

    response = client.post(
        "/api/interactive/intents/batch-confirm",
        json={"boardId": BOARD, "intentIds": [first_id, second_id]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["started"] is True, body
    assert turns.calls[0]["topicId"] == SAVED_TOPIC


def test_recover_start_uses_saved_submission_topic(board_env):
    """恢复续作：原提交已保存 topic-saved，当前话题不得覆盖。"""
    conn = board_env
    intent_id = _approved_intent(conn)
    conn.execute("UPDATE board_intents SET status = 'failed' WHERE id = ?", (intent_id,))
    version = int(
        conn.execute("SELECT version FROM board_intents WHERE id = ?", (intent_id,)).fetchone()[0]
    )
    turns = FakeTurns()
    client = _client(conn, turns, current_topic="topic-switched")

    response = client.post(
        f"/api/interactive/intents/{intent_id}/recover",
        json={"boardId": BOARD, "expectedVersion": version},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["started"] is True, body
    assert turns.calls[0]["topicId"] == SAVED_TOPIC, "恢复也不得改用当前话题"


def test_manual_retry_start_uses_saved_submission_topic(board_env):
    """手动重试续作：原提交已保存 topic-saved，当前话题不得覆盖。"""
    conn = board_env
    intent_id = _approved_intent(conn)
    conn.execute("UPDATE board_intents SET status = 'failed' WHERE id = ?", (intent_id,))
    turns = FakeTurns()
    client = _client(conn, turns, current_topic="topic-switched")

    response = client.post(
        f"/api/interactive/intents/{intent_id}/retry", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True, body
    assert turns.calls[0]["topicId"] == SAVED_TOPIC, "重试也不得改用当前话题"


def test_start_interactive_turn_ignores_caller_topic_when_binding_has_topic(board_env):
    """库函数层：调用方永远无法用当前话题覆盖原提交话题。"""
    conn = board_env
    intent_id = _approved_intent(conn)
    turns = FakeTurns()

    started = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
        topic_id=CURRENT_TOPIC,
    )
    assert started["ok"] is True, started
    assert started["topicId"] == SAVED_TOPIC
    assert turns.calls[0]["topicId"] == SAVED_TOPIC
    assert turns.calls[0]["turn"].board_topic_id == SAVED_TOPIC


# --- 红：无法确认归属时必须明确失败，不启动 / 不写绑定 ----------------------


def test_confirm_without_saved_topic_fails_explicitly_and_starts_nothing(board_env):
    conn = board_env
    _seed_submission(conn, topic=None)  # 原提交没有保存话题
    intent_id = _approved_intent(conn)
    turns = FakeTurns()
    client = _client(conn, turns)

    response = client.post(
        f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False, "无法确认原提交话题时不得假装已开始执行"
    assert body["started"] is False
    assert body["turnId"] is None
    assert body["resume"] is False
    assert body["turnStart"]["code"] == "topic_unresolved", body
    assert "原提交话题" in body["startError"], body
    assert turns.calls == [], "不得回退到当前话题开始 Turn"
    assert submission.active_intent_turn(conn, intent_id) is None
    assert _count(conn, "board_intent_turns") == 0, "不得把意图标记为已续作"
    assert _count(conn, "board_submission_turns") == 0, "不得写提交绑定"
    assert submission.submission_binding(conn, SUBMISSION)["turnId"] == ""
    row = conn.execute("SELECT status FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    assert str(row["status"]) == "approved", "确认事实保留；执行没有开始"
    assert body.get("confirmationSaved") is True


def test_recover_without_saved_topic_fails_explicitly_and_starts_nothing(board_env):
    conn = board_env
    _seed_submission(conn, topic=None)
    intent_id = _approved_intent(conn)
    conn.execute("UPDATE board_intents SET status = 'failed' WHERE id = ?", (intent_id,))
    version = int(
        conn.execute("SELECT version FROM board_intents WHERE id = ?", (intent_id,)).fetchone()[0]
    )
    turns = FakeTurns()
    client = _client(conn, turns)

    response = client.post(
        f"/api/interactive/intents/{intent_id}/recover",
        json={"boardId": BOARD, "expectedVersion": version},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["started"] is False
    assert body["turnStart"]["code"] == "topic_unresolved"
    assert turns.calls == []
    assert _count(conn, "board_intent_turns") == 0
    assert _count(conn, "board_submission_turns") == 0


def test_manual_retry_without_saved_topic_reports_visible_reason(board_env):
    conn = board_env
    _seed_submission(conn, topic=None)
    intent_id = _approved_intent(conn)
    conn.execute("UPDATE board_intents SET status = 'failed' WHERE id = ?", (intent_id,))
    turns = FakeTurns()
    client = _client(conn, turns)

    response = client.post(
        f"/api/interactive/intents/{intent_id}/retry", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert "原提交话题" in str(body.get("reason") or ""), body
    assert body["turnStart"]["code"] == "topic_unresolved"
    assert turns.calls == []


def test_start_without_submission_binding_fails_unresolved(board_env):
    """意图没有提交绑定：不得用当前话题兜底。"""
    conn = board_env
    intent_id = _approved_intent(conn)
    conn.execute("UPDATE board_intents SET submission_id = '' WHERE id = ?", (intent_id,))
    turns = FakeTurns()

    started = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
        topic_id=CURRENT_TOPIC,
    )
    assert started["ok"] is False, started
    assert started["code"] == "topic_unresolved"
    assert started["turnId"] is None
    assert turns.calls == []
    assert submission.active_intent_turn(conn, intent_id) is None
    assert _count(conn, "board_intent_turns") == 0


def test_start_with_missing_submission_row_fails_unresolved(board_env):
    """绑定读取不到（提交记录缺失）：明确失败，不用当前话题兜底。"""
    conn = board_env
    intent_id = _approved_intent(conn)
    conn.execute("DELETE FROM board_submissions WHERE id = ?", (SUBMISSION,))
    turns = FakeTurns()

    started = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
        topic_id=CURRENT_TOPIC,
    )
    assert started["ok"] is False, started
    assert started["code"] == "topic_unresolved"
    assert turns.calls == []


def test_start_with_empty_saved_topic_fails_unresolved(board_env):
    """原提交 binding 存在但 topicId 为空：明确失败，不启动 Turn。"""
    conn = board_env
    _seed_submission(conn, topic=None)
    intent_id = _approved_intent(conn)
    turns = FakeTurns()

    started = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
        topic_id=CURRENT_TOPIC,
    )
    assert started["ok"] is False, started
    assert started["code"] == "topic_unresolved"
    assert started["turnId"] is None
    assert turns.calls == []


# --- 保护：已在执行复用；终态不得重复执行 -----------------------------------


def test_already_running_turn_is_reused_not_started_twice(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    turns = FakeTurns()
    first = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
    )
    assert first["ok"] is True and first["turnId"] == "turn_fake_1"
    second = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
        topic_id=CURRENT_TOPIC,
    )
    assert second["ok"] is True and second["reused"] is True
    assert second["turnId"] == "turn_fake_1"
    assert len(turns.calls) == 1, "已在执行的意图不得并行开第二个 Turn"


def test_completed_intent_is_not_executed_again(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    conn.execute("UPDATE board_intents SET status = 'completed' WHERE id = ?", (intent_id,))
    turns = FakeTurns()
    started = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
        topic_id=CURRENT_TOPIC,
    )
    assert started["ok"] is False
    assert started["code"] == "invalid_status"
    assert turns.calls == []
    assert _count(conn, "board_intent_turns") == 0

# --- 生产调用点：启动恢复自动续作 / 轮次编排自动重试 -----------------------


def _pending_retry(conn, intent_id: str) -> dict:
    """造一个可自动重试的待办系列（真实失败事实，不伪造次数）。"""
    recorded = intent_service.record_retry_failure(
        conn, board_id=BOARD, intent_ids=[intent_id], reason="temporary timeout"
    )
    assert recorded, "应当记录一次可自动重试的失败"
    state = intent_service.retry_state(conn, intent_id)
    assert state is not None and state["status"] == "pending", state
    return state


def test_startup_recovery_uses_saved_topic(board_env):
    """启动恢复自动续作：用原提交的话题，不用启动时的当前话题。"""
    from agent.interactive import startup_recovery

    conn = board_env
    intent_id = _approved_intent(conn)
    _pending_retry(conn, intent_id)
    turns = FakeTurns()

    recovered = startup_recovery.recover_startup(conn, turns.submit)
    assert recovered["started"] == 1, recovered
    assert turns.calls, "有待重试系列且话题可确认：应当恢复执行"
    assert turns.calls[0]["topicId"] == SAVED_TOPIC
    assert submission.active_intent_turn(conn, intent_id)["turnId"] == "turn_fake_1"


def test_startup_recovery_without_saved_topic_skips_without_starting(board_env):
    """启动恢复无法确认话题：记录失败并跳过，不按当前话题起 Turn、不崩溃。"""
    from agent.interactive import startup_recovery

    conn = board_env
    _seed_submission(conn, topic=None)
    intent_id = _approved_intent(conn)
    _pending_retry(conn, intent_id)
    turns = FakeTurns()

    recovered = startup_recovery.recover_startup(conn, turns.submit)
    assert recovered == {"released": 0, "started": 0, "skipped": 1}, recovered
    assert turns.calls == [], "无法确认原提交话题时不得启动"
    assert submission.active_intent_turn(conn, intent_id) is None
    assert _count(conn, "board_intent_turns") == 0
    state = intent_service.retry_state(conn, intent_id)
    assert state["status"] == "pending", "启动恢复失败只放回待重试，不假装成功"
    assert state["attempts"] == 1, "一次认领计入次数（有界：3 次后 exhausted），不重复执行"


def test_orchestrator_auto_retry_uses_saved_topic(board_env):
    """轮次编排自动重试：用原提交的话题。"""
    from agent.services.turn_orchestrator import TurnOrchestrator

    conn = board_env
    intent_id = _approved_intent(conn)
    _pending_retry(conn, intent_id)
    turns = FakeTurns()
    orchestrator = TurnOrchestrator(SimpleNamespace(conn=conn, turns=turns))
    ctx = SimpleNamespace(
        mode="interactive",
        notify=False,
        board_id=BOARD,
        board_intent_ids=(intent_id,),
        status="failed",
        error="temporary timeout",
        cancelled=False,
        turn_id="turn_old",
    )

    orchestrator._finish_interactive_turn(ctx, failed=True)
    assert len(turns.calls) == 1, "话题可确认时服务端应安排一次自动重试"
    assert turns.calls[0]["topicId"] == SAVED_TOPIC
    assert intent_service.retry_state(conn, intent_id)["turn_id"] == "turn_fake_1"


def test_orchestrator_auto_retry_without_saved_topic_stops_without_starting(board_env):
    """轮次编排无法确认话题：停止自动重试，不按当前话题起 Turn、不重复执行。"""
    from agent.services.turn_orchestrator import TurnOrchestrator

    conn = board_env
    _seed_submission(conn, topic=None)
    intent_id = _approved_intent(conn)
    _pending_retry(conn, intent_id)
    turns = FakeTurns()
    orchestrator = TurnOrchestrator(SimpleNamespace(conn=conn, turns=turns))
    ctx = SimpleNamespace(
        mode="interactive",
        notify=False,
        board_id=BOARD,
        board_intent_ids=(intent_id,),
        status="failed",
        error="temporary timeout",
        cancelled=False,
        turn_id="turn_old",
    )

    orchestrator._finish_interactive_turn(ctx, failed=True)
    assert turns.calls == [], "无法确认原提交话题时不得改用当前话题启动"
    state = intent_service.retry_state(conn, intent_id)
    assert state is not None and state["status"] == "stopped", "无法确认话题是确定性失败，停止自动重试"
    assert _count(conn, "board_intent_turns") == 0
