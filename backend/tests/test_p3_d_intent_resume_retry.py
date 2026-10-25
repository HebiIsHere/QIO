# -*- coding: utf-8 -*-
"""P3-D：确认/恢复真实续作 Turn、单意图有效执行、持久化自动重试、固定文案（§6.1–§6.5）。

定向：真实 venv + fake 主循环（不联网、不调用模型）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module
from agent.interactive import board_store, intent_service, submission, tool_models
from agent.storage.migrate import apply_migrations, current_version, missing_objects
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

from test_p3_b_support import add_submission, intent_scope, intent_spec, make_board, prepare

BOARD = "board_p3d"
SUBMISSION = "sub_p3d"


def _approved_intent(conn, *, title: str = "整理材料", actions=("create_card",)) -> str:
    proposed = intent_service.propose_intents(
        conn,
        board_id=BOARD,
        submission_id=SUBMISSION,
        intents=[intent_spec(scope=intent_scope(allowed_actions=list(actions)))],
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


class ExplodingTurns:
    def submit(self, message: str, topic_id: str | None = None, **kwargs):
        raise RuntimeError("main loop unavailable")


@pytest.fixture()
def board_env(db_conn):
    prepare(db_conn)
    make_board(db_conn, board_id=BOARD)
    add_submission(db_conn, board_id=BOARD, submission_id=SUBMISSION)
    return db_conn


# --- §6.1 迁移与视图 --------------------------------------------------------


def test_migration_69_adds_display_retry_and_intent_turn_tables(db_conn):
    """P3-D 的登记号：原页面线 56 → 三分支整合后 69（与 R9 的 55/57 不再同号/相邻冲突）。

    断言"最新版本 = 补偿版本"仍保留（整合后是 70）；P3-D 本身必须登记为 69。
    """
    assert SCHEMA_VERSION == COMPENSATION_VERSION
    assert SCHEMA_VERSION >= 69
    assert current_version(db_conn) == SCHEMA_VERSION
    assert missing_objects(db_conn) == {"tables": [], "columns": [], "indexes": []}
    tables = {
        str(row["name"])
        for row in db_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"board_intent_turns", "board_intent_retries"} <= tables
    columns = {str(row["name"]) for row in db_conn.execute("PRAGMA table_info(board_intents)")}
    assert "display" in columns
    # 原页面线 56 已于三分支整合改号到 69（56 保持空号，避免与 R9 冻结号段相邻冲突）。
    assert any(int(number) == 69 for number, _ in MIGRATIONS), (
        "P3-D 迁移必须登记为 69（原页面线 56 → 整合后 69）")
    assert not any(int(number) == 56 for number, _ in MIGRATIONS), (
        "56 必须保持空号：P3-D 已改号到 69")


def test_intent_view_includes_display_and_retry(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    conn.execute(
        "UPDATE board_intents SET display = ? WHERE id = ?",
        (models_dumps({"x": 12.5, "y": 40}), intent_id),
    )
    view = intent_service.get_intent(conn, BOARD, intent_id)
    assert view["display"] == {"x": 12.5, "y": 40.0}
    assert view["retry"] is None


def test_set_intent_display_does_not_bump_semantic_version(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    before = intent_service.get_intent(conn, BOARD, intent_id)
    result = intent_service.set_intent_display(
        conn, board_id=BOARD, intent_id=intent_id, x=17.25, y=-3.5
    )
    assert result["ok"] is True
    assert result["display"] == {"x": 17.25, "y": -3.5}
    after = intent_service.get_intent(conn, BOARD, intent_id)
    assert after["display"] == {"x": 17.25, "y": -3.5}
    assert after["version"] == before["version"]
    assert intent_service.list_retry_series(conn, board_id=BOARD) == []


def models_dumps(value) -> str:
    from agent.interactive import models

    return models.dumps(value)


# --- §6.2 真实续作 Turn -----------------------------------------------------


def test_claim_rejects_second_claim_during_claiming_window(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    first = submission.claim_intent_turns(conn, board_id=BOARD, intent_ids=[intent_id])
    assert first["ok"] is True
    row = conn.execute(
        "SELECT status FROM board_intent_turns WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    assert str(row["status"]) == "claiming"
    second = submission.claim_intent_turns(conn, board_id=BOARD, intent_ids=[intent_id])
    assert second["ok"] is False
    assert second["code"] == "intent_turn_active"



def test_start_turn_claims_intent_and_duplicate_confirm_does_not_parallel(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    turns = FakeTurns()
    first = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
        topic_id="topic-1",
    )
    assert first["ok"] is True, first
    assert first["turnId"] == "turn_fake_1"
    assert first["resume"] is True
    turn = turns.calls[0]["turn"]
    assert turn.board_intent_ids == (intent_id,)
    assert turn.board_submission_id == SUBMISSION
    assert turn.board_topic_id == "topic-1"
    assert turn.mode == "interactive"
    assert turn.board_resume is True
    assert turn.board_attempt >= 1
    assert submission.active_intent_turn(conn, intent_id)["turnId"] == "turn_fake_1"

    second = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
    )
    assert second["ok"] is True
    assert second["reused"] is True
    assert second["turnId"] == "turn_fake_1"
    assert len(turns.calls) == 1, "重复确认不得并行开第二个 Turn"

    submission.release_intent_turns(conn, intent_ids=[intent_id], turn_id="turn_fake_1", status="done")
    third = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
    )
    assert third["ok"] is True
    assert third["turnId"] == "turn_fake_2"
    assert len(turns.calls) == 2


def test_start_turn_unavailable_or_submit_failure_is_honest(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    unavailable = submission.start_interactive_turn(
        conn,
        turn_submitter=None,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
    )
    assert unavailable["ok"] is False
    assert unavailable["availability"] == "unavailable"
    assert unavailable["turnId"] is None
    assert submission.active_intent_turn(conn, intent_id) is None

    exploded = submission.start_interactive_turn(
        conn,
        turn_submitter=ExplodingTurns().submit,
        board_id=BOARD,
        intent_ids=[intent_id],
        source="confirm",
    )
    assert exploded["ok"] is False
    assert exploded["code"] == "turn_submit_failed"
    assert exploded["turnId"] is None
    assert submission.active_intent_turn(conn, intent_id) is None


def test_confirm_api_returns_real_turn_id_and_binds_facts(board_env):
    conn = board_env
    intent_id = _approved_intent(conn, title="准备确认")
    turns = FakeTurns()
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        conn=conn,
        turns=turns,
        current_topic=lambda: "topic-api",
    )
    app.include_router(api_module.router)
    client = TestClient(app)

    response = client.post(
        f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["started"] is True
    assert body["turnId"] == "turn_fake_1"
    assert body["resume"] is True
    assert turns.calls and turns.calls[0]["turn"].board_intent_ids == (intent_id,)
    assert submission.active_intent_turn(conn, intent_id)["turnId"] == "turn_fake_1"


def test_recover_api_starts_turn_and_counts_one_manual_attempt(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    # 造一次真实失败：状态 failed + 授权有效 + 重试系列 exhausted。
    conn.execute(
        "UPDATE board_intents SET status = 'failed', finished_at = ? WHERE id = ?",
        ("2026-10-24T00:00:00+00:00", intent_id),
    )
    intent_service.record_retry_failure(
        conn,
        board_id=BOARD,
        intent_ids=[intent_id],
        reason="temporary network failure",
        turn_id="turn_old",
    )
    for _ in range(3):
        claimed = intent_service.claim_auto_retry(
            conn,
            intent_service.retry_state(conn, intent_id)["series_id"],
        )
        assert claimed["ok"] is True
        intent_service.record_retry_failure(
            conn,
            board_id=BOARD,
            intent_ids=[intent_id],
            reason="temporary network failure",
        )
    exhausted = intent_service.retry_state(conn, intent_id)
    assert exhausted["status"] == "exhausted"
    assert exhausted["attempts"] == 3

    turns = FakeTurns()
    app = FastAPI()
    app.state.ctx = SimpleNamespace(conn=conn, turns=turns, current_topic=lambda: "topic-r")
    app.include_router(api_module.router)
    client = TestClient(app)
    version = int(
        conn.execute("SELECT version FROM board_intents WHERE id = ?", (intent_id,)).fetchone()[0]
    )
    response = client.post(
        f"/api/interactive/intents/{intent_id}/recover",
        json={"boardId": BOARD, "expectedVersion": version},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["started"] is True
    assert body["turnId"] == "turn_fake_1"
    retry = intent_service.retry_state(conn, intent_id)
    assert retry["manual_attempts"] == 1
    assert retry["attempts"] == 3, "手动重试不重置自动次数"


# --- §6.3 重试系列 ----------------------------------------------------------


def test_retry_first_failure_does_not_count_and_three_auto_attempts_then_exhausted(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    recorded = intent_service.record_retry_failure(
        conn, board_id=BOARD, intent_ids=[intent_id], reason="temporary timeout"
    )
    assert len(recorded) == 1
    state = it = intent_service.retry_state(conn, intent_id)
    assert it["attempts"] == 0, "首次失败不占自动次数"
    assert it["limit"] == 3 and it["status"] == "pending"
    assert it["final_notified_at"] is None

    for attempt in (1, 2, 3):
        claimed = intent_service.claim_auto_retry(conn, state["series_id"])
        assert claimed["ok"] is True
        assert claimed["attempt"] == attempt
        intent_service.record_retry_failure(
            conn, board_id=BOARD, intent_ids=[intent_id], reason="temporary timeout"
        )
    exhausted = intent_service.retry_state(conn, intent_id)
    assert exhausted["attempts"] == 3
    assert exhausted["status"] == "exhausted"
    assert exhausted["final_notified_at"], "失败收尾只记录一次最终说明"
    assert intent_service.claim_auto_retry(conn, state["series_id"])["ok"] is False


def test_non_retryable_failure_never_auto_retries(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    intent_service.record_retry_failure(
        conn,
        board_id=BOARD,
        intent_ids=[intent_id],
        reason="authorization stale: 授权已失效",
    )
    state = intent_service.retry_state(conn, intent_id)
    assert state["status"] == "stopped"
    assert state["retryable"] is False
    assert intent_service.pending_auto_retries(conn, board_id=BOARD, intent_ids=[intent_id]) == []


def test_retry_persists_across_new_connection(tmp_path):
    from agent.storage.db import connect

    path = tmp_path / "retry.db"
    conn = connect(path)
    try:
        assert apply_migrations(conn) == SCHEMA_VERSION
        conn.execute(
            "INSERT INTO boards (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("b", "板", "2026-10-24T00:00:00+00:00", "2026-10-24T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO board_intents (id, board_id, title, status, version, created_at, updated_at)"
            " VALUES (?, ?, ?, 'failed', 1, ?, ?)",
            ("i1", "b", "任务", "2026-10-24T00:00:00+00:00", "2026-10-24T00:00:00+00:00"),
        )
        intent_service.record_retry_failure(conn, board_id="b", intent_ids=["i1"], reason="timeout")
        series = intent_service.retry_state(conn, "i1")
        intent_service.claim_auto_retry(conn, series["series_id"])
    finally:
        conn.close()
    conn2 = connect(path)
    try:
        reopened = intent_service.retry_state(conn2, "i1")
        assert reopened is not None
        assert reopened["attempts"] == 1
        assert reopened["status"] == "running"
    finally:
        conn2.close()


def test_success_turn_does_not_leave_retry_series_running(board_env):
    from agent.services.turn_orchestrator import TurnOrchestrator

    conn = board_env
    intent_id = _approved_intent(conn)
    intent_service.record_retry_failure(
        conn, board_id=BOARD, intent_ids=[intent_id], reason="temporary timeout"
    )
    series = intent_service.retry_state(conn, intent_id)
    claimed = intent_service.claim_auto_retry(conn, series["series_id"])
    assert claimed["ok"] is True
    conn.execute("UPDATE board_intents SET status = 'paused' WHERE id = ?", (intent_id,))
    turns = FakeTurns()
    app = SimpleNamespace(conn=conn, turns=turns)
    orchestrator = TurnOrchestrator(app)
    ctx = SimpleNamespace(
        mode="interactive",
        notify=False,
        board_id=BOARD,
        board_intent_ids=(intent_id,),
        status="completed",
        error="",
        cancelled=False,
        turn_id="turn_success_without_finish",
    )
    orchestrator._finish_interactive_turn(ctx)
    after = intent_service.retry_state(conn, intent_id)
    assert after is not None
    assert after["status"] == "stopped"


def test_startup_recovery_releases_stale_claim_and_resumes_pending_retry(board_env):
    from agent.interactive import startup_recovery

    conn = board_env
    intent_id = _approved_intent(conn)
    intent_service.record_retry_failure(
        conn, board_id=BOARD, intent_ids=[intent_id], reason="temporary timeout"
    )
    conn.execute(
        "INSERT INTO board_intent_turns"
        " (intent_id, board_id, submission_id, turn_id, attempt, source, status, reason, created_at, updated_at)"
        " VALUES (?, ?, ?, '', 1, 'confirm', 'claiming', '', ?, ?)",
        (intent_id, BOARD, SUBMISSION, "old", "old"),
    )
    turns = FakeTurns()
    recovered = startup_recovery.recover_startup(conn, turns.submit)
    assert recovered["released"] == 1
    assert recovered["started"] == 1
    assert len(turns.calls) == 1
    state = intent_service.retry_state(conn, intent_id)
    assert state["status"] == "running"
    assert state["turn_id"] == "turn_fake_1"


def test_orchestrator_schedules_automatic_retry_after_failure(board_env):
    from agent.services.turn_orchestrator import TurnOrchestrator

    conn = board_env
    intent_id = _approved_intent(conn)
    intent_service.record_retry_failure(
        conn, board_id=BOARD, intent_ids=[intent_id], reason="temporary timeout"
    )
    turns = FakeTurns()
    app = SimpleNamespace(conn=conn, turns=turns)
    orchestrator = TurnOrchestrator(app)
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
    assert len(turns.calls) == 1, "首次失败后服务端应排一次自动重试"
    state = intent_service.retry_state(conn, intent_id)
    assert state["attempts"] == 1
    assert state["turn_id"] == "turn_fake_1"
    assert submission.active_intent_turn(conn, intent_id)["turnId"] == "turn_fake_1"



def test_display_and_retry_series_api(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        conn=conn,
        turns=FakeTurns(),
        current_topic=lambda: "topic-api",
    )
    app.include_router(api_module.router)
    client = TestClient(app)
    display = client.post(
        f"/api/interactive/intents/{intent_id}/display",
        json={"boardId": BOARD, "x": 21.5, "y": 33},
    )
    assert display.status_code == 200
    assert display.json()["display"] == {"x": 21.5, "y": 33.0}
    view = intent_service.get_intent(conn, BOARD, intent_id)
    assert view["display"] == {"x": 21.5, "y": 33.0}
    series = client.get(f"/api/interactive/boards/{BOARD}/retry-series")
    assert series.status_code == 200
    assert series.json()["series"] == []
    single = client.get(f"/api/interactive/intents/{intent_id}/retry-series")
    assert single.status_code == 200
    assert single.json()["series"] == []


def test_confirm_api_reports_turn_failure_when_main_loop_rejects(board_env):
    conn = board_env
    intent_id = _approved_intent(conn)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        conn=conn,
        turns=ExplodingTurns(),
        current_topic=lambda: "topic-api",
    )
    app.include_router(api_module.router)
    client = TestClient(app)
    response = client.post(
        f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False, "主循环可用但受理失败时不能假装确认已开始"
    assert body["started"] is False
    assert body["turnId"] is None
    assert body["confirmationSaved"] is True
    assert submission.active_intent_turn(conn, intent_id) is None


def test_finish_failed_records_retry_and_orchestrator_exhausts_three_attempts(board_env):
    from agent.services.turn_orchestrator import TurnOrchestrator

    conn = board_env
    intent_id = _approved_intent(conn)
    row = conn.execute("SELECT version FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    finished = intent_service.finish_intent(
        conn,
        board_id=BOARD,
        intent_id=intent_id,
        expected_version=int(row["version"]),
        outcome="failed",
        summary="temporary timeout",
        operation_keys=[],
    )
    assert finished["ok"] is True, finished
    state = intent_service.retry_state(conn, intent_id)
    assert state["attempts"] == 0 and state["status"] == "pending"

    turns = FakeTurns()
    orchestrator = TurnOrchestrator(SimpleNamespace(conn=conn, turns=turns))
    turn_id = "turn_initial"
    for index in range(3):
        ctx = SimpleNamespace(
            mode="interactive",
            notify=False,
            board_id=BOARD,
            board_intent_ids=(intent_id,),
            status="failed",
            error="temporary timeout",
            cancelled=False,
            turn_id=turn_id,
        )
        orchestrator._finish_interactive_turn(ctx)
        state = intent_service.retry_state(conn, intent_id)
        assert state["attempts"] == index + 1
        turn_id = str(state["turn_id"] or "")
    assert len(turns.calls) == 3, "最多三次自动重试"
    assert state["attempts"] == 3 and state["status"] == "running"
    orchestrator._finish_interactive_turn(
        SimpleNamespace(
            mode="interactive",
            notify=False,
            board_id=BOARD,
            board_intent_ids=(intent_id,),
            status="failed",
            error="temporary timeout",
            cancelled=False,
            turn_id=str(state["turn_id"] or "turn_final"),
        )
    )
    assert len(turns.calls) == 3, "耗尽后不再自动重试"
    assert intent_service.retry_state(conn, intent_id)["status"] == "exhausted"
    assert submission.active_intent_turn(conn, intent_id) is None



def test_batch_start_only_claims_not_yet_running_intents(board_env):
    conn = board_env
    first_id = _approved_intent(conn, title="第一条")
    second_id = _approved_intent(conn, title="第二条")
    turns = FakeTurns()
    first = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[first_id],
        source="confirm",
    )
    assert first["ok"] is True
    batch = submission.start_interactive_turn(
        conn,
        turn_submitter=turns.submit,
        board_id=BOARD,
        intent_ids=[first_id, second_id],
        source="batch_confirm",
    )
    assert batch["ok"] is True
    assert len(turns.calls) == 2
    assert turns.calls[1]["turn"].board_intent_ids == (second_id,)
    assert batch["alreadyRunning"][0]["intentId"] == first_id


# --- §6.5 固定文案 ----------------------------------------------------------


def test_fixed_prompts_are_present_and_tool_descriptions_point_to_tools():
    from agent import prompts

    policy = prompts.PROMPTS["INTERACTIVE_POLICY"]
    retry = prompts.PROMPTS["INTERACTIVE_RETRY_POLICY"]
    assert policy.startswith("当前运行于互动模式")
    assert "意图是待确认的工作目标" in policy
    assert retry.startswith("当前正在处理一项已有互动工作的失败恢复")
    assert "最多安排三次自动重试" in retry
    assert prompts.TOOL_DESCRIPTIONS["board_inspect"].startswith("查看当前互动板面中获准访问的卡片")
    assert "operation_key" in prompts.TOOL_DESCRIPTIONS["board_create_card"]
    assert "不能通过单独修改宽或高拉伸图片" in prompts.TOOL_DESCRIPTIONS["board_update_card"]
    assert "端点可以是程序允许且获准访问的卡片或分组" in prompts.TOOL_DESCRIPTIONS["board_add_link"]
    assert "外部新成员追加到末尾" in prompts.TOOL_DESCRIPTIONS["board_group_cards"]
