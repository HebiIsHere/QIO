# -*- coding: utf-8 -*-
"""R16 F 独立验收：契约 §2 I01—I14、§3 完整位置状态与两类版本、§4/§5 命令身份、
§6 共享暂存、§8 迁移，以及 §10.2 A—I 回归。

本文件只从冻结契约（docs/topic-unify-20261111-contract.md v1 + §14 v2 delta）
推导预期，不复用 A—D 的实现细节/测试代码；基线红在 r15-baseline（44d92fb3）取得，
集成绿在 qio-r14 取得。所有 SQLite 都是 tmp_path 下的临时库。

诚实边界：
* 这里是服务级/DB 级独立验收，不是浏览器端到端；前端接受规则在
  frontend/src/__tests__/r16-f-independent.test.ts。
* 模型一律 fake/scripted adapter，不联网、不调真实模型。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.graph.nodes import NodeService
from agent.memory.fragment import FragmentManager
from agent.services import navigation_version as nav_version_module
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.binding import TurnBindingService
from agent.services.topic_operation import TopicStages
from agent.storage import db_identity
from agent.storage import migrate as migrate_module
from agent.storage.db import connect, transaction
from agent.storage.migrate import apply_migrations
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

#: 生产源码根以**实际导入的 agent 包**为准（避免测试文件被复制到别处时扫错树）。
import agent as _agent_package

SRC = Path(_agent_package.__file__).resolve().parent

ANSWER = "[[QIO:ANSWER]]"
#: 生产生成路径会剥离协议声明标记；正式正文/共享暂存断言只比较清理后的正文。
ANSWER_TEXT = "独立验收回答"


# ── 延迟导入 R16 新模块（基线没有；让每个用例独立红，而不是整文件 collection error）──


def _ps():
    from agent.services import position_state

    return position_state


def _service(conn: sqlite3.Connection):
    return _ps().PositionStateService(conn)


def _identity(**overrides):
    data = dict(
        kind="request",
        identity_key="request:test-req-1",
        request_id="test-req-1",
        operation_id=None,
        param_version=None,
        internal_scope=None,
    )
    data.update(overrides)
    return _ps().NavCommandIdentity(**data)


def _transition(**overrides):
    data = dict(
        kind="enter_topic",
        source="manual.enter_topic",
        target_topic_id=None,
        target_fragment_id=None,
        base_nav_version=None,
        expected_position_version=None,
        pending="keep",
        pending_topic_id=None,
        pending_fragment_id=None,
        pending_request_id=None,
        pending_intent_id=None,
        nav_decision=True,
        move_anchor=None,
        initiator="user",
        identity=_identity(),
        turn_id=None,
        approval_id=None,
        operation_id=None,
        param_version=None,
        causal={},
        result_extra={},
        new_topic_name=None,
    )
    data.update(overrides)
    return _ps().PositionTransition(**data)


# ── 通用设施 ─────────────────────────────────────────────────────────


def _new_db(tmp_path: Path, name: str = "r16_f.db") -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table)}


def _make_topic(conn: sqlite3.Connection, name: str = "话题") -> str:
    return NodeService(conn).create_topic(name).id


def _open_fragment(conn: sqlite3.Connection, topic_id: str) -> str:
    return FragmentManager(conn).get_or_create_open(topic_id).id


def _closed_fragment(conn: sqlite3.Connection, topic_id: str) -> str:
    """测试装置：直接插入一条已封存的历史片段（不触发「一话题一开放片段」约束）。"""
    fragment_id = "frag_f_" + __import__("uuid").uuid4().hex[:12]
    with transaction(conn):
        conn.execute(
            "INSERT INTO fragments (id, topic_id, created_at, closed_at, boundary_reason) "
            "VALUES (?, ?, ?, ?, 'capacity')",
            (
                fragment_id,
                topic_id,
                "2026-11-11T00:00:00+00:00",
                "2026-11-11T00:00:01+00:00",
            ),
        )
    return fragment_id


def _anchor(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT topic_id, fragment_id FROM cursor WHERE anchor_type = 'active'"
    ).fetchone()
    return (row["topic_id"], row["fragment_id"]) if row is not None else (None, None)


def _nav_version(conn: sqlite3.Connection) -> int:
    return int(nav_version_module.current(conn))


def _position_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT position_version FROM position_state WHERE id = 1"
    ).fetchone()
    assert row is not None, "position_state 单行不存在（迁移 83 未生效）"
    return int(row["position_version"])


def _intent_rows(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT intent_id, topic_id, source_fragment_id, version, state, "
            "resolved_fragment_id, request_id FROM continuation_intents "
            "ORDER BY created_at, intent_id"
        )
    ]


def _command_rows(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT command_id, identity_key, identity_kind, command_kind, "
            "param_digest, status, reason, position_version, nav_version, initiator "
            "FROM navigation_commands ORDER BY created_at, command_id"
        )
    ]


def _fingerprint(conn: sqlite3.Connection) -> dict:
    """用于断言「零写入/零副作用」的完整事实指纹（含 sqlite 写行计数）。"""
    return {
        "nav_version": _nav_version(conn),
        "anchor": _anchor(conn),
        "intents": _intent_rows(conn),
        "commands": _command_rows(conn),
        "total_changes": int(conn.total_changes),
    }


def _apply_or_exc(service, transition):
    try:
        return service.apply(transition), None
    except Exception as exc:  # noqa: BLE001 - 契约要求 CommandResult；异常单独判定
        return None, exc


# ── §8 迁移 83/84 与 REQUIRED_OBJECTS ────────────────────────────────


def test_migration_83_84_create_frozen_objects(tmp_path):
    assert SCHEMA_VERSION == 84, "SCHEMA_VERSION 应为 84（§14.9：补偿版本=max 迁移版本）"
    assert COMPENSATION_VERSION == 84, "COMPENSATION_VERSION 应为 84"
    conn = connect(tmp_path / "m83.db")
    version = apply_migrations(conn)
    assert version == 84
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert int(row["v"]) == 84
    assert _table_exists(conn, "position_state")
    assert _table_exists(conn, "navigation_commands")

    ps_cols = _columns(conn, "position_state")
    for column in (
        "id",
        "position_version",
        "db_identity",
        "last_source",
        "last_turn_id",
        "last_operation_id",
        "last_nav_version",
        "updated_at",
    ):
        assert column in ps_cols, "position_state 缺列 %s" % column
    seed = conn.execute(
        "SELECT position_version, db_identity FROM position_state WHERE id = 1"
    ).fetchone()
    assert seed is not None, "position_state 缺少 id=1 种子行"
    assert int(seed["position_version"]) == 0
    assert str(seed["db_identity"]) == ""

    command_cols = _columns(conn, "navigation_commands")
    expected_command_cols = {
        "command_id",
        "identity_key",
        "identity_kind",
        "command_kind",
        "param_digest",
        "request_id",
        "operation_id",
        "param_version",
        "status",
        "reason",
        "result_json",
        "position_json",
        "nav_version",
        "position_version",
        "initiator",
        "turn_id",
        "approval_id",
        "created_at",
        "updated_at",
    }
    assert expected_command_cols <= command_cols, (
        "navigation_commands 缺列：%s" % (expected_command_cols - command_cols)
    )
    index_names = {
        str(row[1]) for row in conn.execute("PRAGMA index_list(navigation_commands)")
    }
    assert "idx_navigation_commands_identity" in index_names
    assert "idx_navigation_commands_created" in index_names
    identity_unique = [
        row
        for row in conn.execute("PRAGMA index_list(navigation_commands)")
        if str(row[1]) == "idx_navigation_commands_identity"
    ]
    assert identity_unique and int(identity_unique[0][2]) == 1, "identity 索引必须 UNIQUE"

    run_cols = _columns(conn, "topic_answer_runs")
    for column in ("phase", "staged_answer", "staged_at", "staged_model"):
        assert column in run_cols, "topic_answer_runs 缺列 %s" % column


def test_migrate_required_objects_cover_r16(tmp_path):
    required = migrate_module.REQUIRED_OBJECTS
    tables = set(required["tables"])
    columns = set(required["columns"])
    indexes = set(required["indexes"])
    assert {"position_state", "navigation_commands"} <= tables
    assert {
        "topic_answer_runs.phase",
        "topic_answer_runs.staged_answer",
        "topic_answer_runs.staged_at",
        "topic_answer_runs.staged_model",
    } <= columns
    assert {"idx_navigation_commands_identity", "idx_navigation_commands_created"} <= indexes


def test_compensation_84_restores_missing_r16_objects(tmp_path):
    conn = connect(tmp_path / "compensation.db")
    apply_migrations(conn)
    assert _table_exists(conn, "position_state")
    assert _table_exists(conn, "navigation_commands")
    conn.execute("DROP TABLE navigation_commands")
    conn.execute("DROP TABLE position_state")
    apply_migrations(conn)  # 版本已是 83；缺失对象由补偿 84 重放
    assert _table_exists(conn, "position_state"), "补偿 84 没有重建 position_state"
    assert _table_exists(conn, "navigation_commands"), "补偿 84 没有重建 navigation_commands"
    service = _service(conn)
    service.ensure_identity()
    assert isinstance(service.read()["position_version"], int)


def _build_db_at_version(tmp_path: Path, target_version: int) -> sqlite3.Connection:
    """按真实迁移语句顺序把空库推进到指定版本（用于旧库升级验证）。"""
    conn = connect(tmp_path / ("old_%d.db" % target_version))
    migrate_module.current_version(conn)  # 建 schema_version 表
    for target, statements in MIGRATIONS:
        if int(target) > int(target_version):
            break
        with transaction(conn):
            migrate_module._apply_statements(conn, int(target), list(statements))
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (int(target), "2026-11-11T00:00:00+00:00"),
            )
    return conn


def test_migration_83_backfills_answer_run_phase(tmp_path):
    conn = _build_db_at_version(tmp_path, 82)
    assert int(migrate_module.current_version(conn)) == 82
    now = "2026-11-11T00:00:00+00:00"
    with transaction(conn):
        conn.execute(
            "INSERT INTO topic_answer_runs (answer_key, status, created_at, updated_at) "
            "VALUES ('key-answered', 'answered', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO topic_answer_runs (answer_key, status, created_at, updated_at) "
            "VALUES ('key-running', 'running', ?, ?)",
            (now, now),
        )
    apply_migrations(conn)
    phases = {
        str(row["answer_key"]): str(row["phase"])
        for row in conn.execute("SELECT answer_key, phase FROM topic_answer_runs")
    }
    assert phases.get("key-answered") == "answered", "旧 answered run 必须回填 phase=answered"
    assert phases.get("key-running") == "waiting", "旧 running run 按冻结规则回填 waiting"


# ── §3.1 read() 完整位置状态 / §3.2 两类版本与身份 ───────────────────


def test_read_returns_complete_frozen_position_wire(tmp_path):
    conn = _new_db(tmp_path)
    service = _service(conn)
    service.ensure_identity()

    topic_id = _make_topic(conn, "阅读话题")
    open_id = _open_fragment(conn, topic_id)
    closed_id = _closed_fragment(conn, topic_id)
    AnchorService(conn).set_active(topic_id, closed_id)
    intent = TurnBindingService(conn).register_intent_ex(
        topic_id, closed_id, request_id="req-read-pending"
    ).intent
    nav_version_module.advance(
        conn, source="test.setup", topic_id=topic_id, fragment_id=closed_id
    )

    state = service.read()
    required = {
        "topic_id",
        "topic_name",
        "fragment_id",
        "fragment_title",
        "historic",
        "pending_intent_id",
        "pending_intent_version",
        "pending_source_fragment_id",
        "pending_source_title",
        "nav_version",
        "position_version",
        "position_identity",
    }
    assert required <= set(state), "read() 缺 wire 字段：%s" % (required - set(state))
    assert state["topic_id"] == topic_id
    assert state["fragment_id"] == closed_id
    assert state["historic"] is True, "焦点在封存历史片段时 historic 必须为 True"
    assert state["nav_version"] == _nav_version(conn)
    assert state["position_version"] == _position_version(conn)
    assert state["position_identity"] == db_identity.ensure_instance_id(conn)
    assert state["pending_intent_id"] == intent.intent_id
    assert state["pending_intent_version"] == intent.version
    assert state["pending_source_fragment_id"] == closed_id
    assert state["topic_name"] == "阅读话题"
    assert service.read() == state, "read() 必须同源且稳定（不得每次拼接出不同事实）"
    assert open_id != closed_id


def test_ensure_identity_rebuilds_only_on_db_identity_change(tmp_path):
    conn = _new_db(tmp_path)
    service = _service(conn)
    service.ensure_identity()
    identity = service.read()["position_identity"]
    assert identity == db_identity.ensure_instance_id(conn)

    with transaction(conn):
        conn.execute("UPDATE position_state SET position_version = 7 WHERE id = 1")
    assert service.ensure_identity() is False, "身份未变时 ensure_identity 不得重建"
    assert service.read()["position_version"] == 7, "身份未变时不得清零 position_version"

    new_identity = "db-replaced-for-f-test"
    with transaction(conn):
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (new_identity, db_identity.DB_INSTANCE_KEY),
        )
    rebuilt = service.ensure_identity()
    assert rebuilt is True, "身份变化必须由权威重建流程返回 True"
    state = service.read()
    assert state["position_identity"] == new_identity
    assert state["position_version"] == 0, "数据库身份变化必须把 position_version 归零"


# ── §4/§5 统一变更入口：I04/I05/I01/I07 与 §3.3 版本职责 ─────────────


def _setup_two_topics(tmp_path):
    conn = _new_db(tmp_path)
    service = _service(conn)
    service.ensure_identity()
    topic_a = _make_topic(conn, "话题A")
    topic_b = _make_topic(conn, "话题B")
    frag_a = _open_fragment(conn, topic_a)
    frag_b = _open_fragment(conn, topic_b)
    AnchorService(conn).set_active(topic_a, frag_a)
    return conn, service, topic_a, topic_b, frag_a, frag_b


def test_i04_replay_same_identity_same_params_returns_existing_zero_writes(tmp_path):
    conn, service, topic_a, topic_b, _frag_a, _frag_b = _setup_two_topics(tmp_path)
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)
    identity = _identity(
        kind="request", identity_key="request:req-i04", request_id="req-i04"
    )
    transition = _transition(
        kind="enter_topic",
        target_topic_id=topic_b,
        target_fragment_id=None,
        base_nav_version=nav0,
        pending="cancel",
        nav_decision=True,
        initiator="user",
        identity=identity,
    )
    first = service.apply(transition)
    assert first.changed is True and first.replay is False
    assert first.status == "applied"
    assert first.command_id == "request:req-i04", "command_id 必须等于 identity_key"
    assert _nav_version(conn) == nav0 + 1
    assert _position_version(conn) == pos0 + 1
    assert _anchor(conn) == (topic_b, None)
    assert first.position["topic_id"] == topic_b

    digest = _ps().param_digest(transition)
    assert isinstance(digest, str) and digest, "param_digest 必须返回稳定非空摘要"
    before = _fingerprint(conn)

    # 新实例模拟进程重启后重放：同身份同参数必须返回既有结果，零位置写入。
    restarted = _service(conn)
    restarted.ensure_identity()
    again = restarted.apply(_transition(
        kind="enter_topic",
        target_topic_id=topic_b,
        target_fragment_id=None,
        base_nav_version=nav0,
        pending="cancel",
        nav_decision=True,
        initiator="user",
        identity=_identity(
            kind="request", identity_key="request:req-i04", request_id="req-i04"
        ),
    ))
    assert again.replay is True and again.changed is False
    assert again.command_id == first.command_id
    after = _fingerprint(conn)
    assert after == before, "I04：重放必须零位置写入、零版本推进、零重复命令行"
    assert _nav_version(conn) == nav0 + 1
    assert _position_version(conn) == pos0 + 1

    replay_result = restarted.replay("request:req-i04", digest)
    assert replay_result is not None, "replay() 必须能按 identity+digest 读回既有结果"
    assert replay_result.replay is True and replay_result.changed is False
    assert replay_result.position["topic_id"] == topic_b, "replay 返回当前只读 position"
    lookup = restarted.lookup("request:req-i04")
    assert lookup is not None, "lookup() 必须能读到命令行事实"
    assert lookup.get("identity_key") == "request:req-i04"
    assert lookup.get("command_kind") == "enter_topic"


def test_i05_same_identity_different_params_is_conflict_zero_write(tmp_path):
    conn, service, topic_a, topic_b, _frag_a, _frag_b = _setup_two_topics(tmp_path)
    identity_key = "request:req-i05"
    identity = _identity(kind="request", identity_key=identity_key, request_id="req-i05")
    service.apply(_transition(
        kind="enter_topic",
        target_topic_id=topic_b,
        base_nav_version=_nav_version(conn),
        pending="cancel",
        identity=identity,
    ))
    before = _fingerprint(conn)
    count_before = len([
        row for row in _command_rows(conn) if row["identity_key"] == identity_key
    ])
    assert count_before == 1

    conflict = service.apply(_transition(
        kind="enter_topic",
        target_topic_id=topic_a,  # 同一身份、不同参数
        base_nav_version=_nav_version(conn),
        pending="cancel",
        identity=_identity(kind="request", identity_key=identity_key, request_id="req-i05"),
    ))
    assert conflict.conflict is True or conflict.status == "conflict"
    assert conflict.changed is False
    assert conflict.command_id == identity_key
    assert conflict.position["topic_id"] == topic_b, "冲突响应返回当前只读 position"
    after = _fingerprint(conn)
    assert after == before, "I05：同身份不同参数必须零业务副作用、不新增行"
    assert len([
        row for row in _command_rows(conn) if row["identity_key"] == identity_key
    ]) == 1


def test_i01_stale_basis_rejected_zero_write_and_replay_stays_rejected(tmp_path):
    conn, service, topic_a, topic_b, _frag_a, _frag_b = _setup_two_topics(tmp_path)
    topic_c = _make_topic(conn, "话题C")
    _open_fragment(conn, topic_c)
    nav_stale = _nav_version(conn)
    service.apply(_transition(
        kind="enter_topic",
        target_topic_id=topic_b,
        base_nav_version=nav_stale,
        pending="cancel",
        identity=_identity(kind="request", identity_key="request:req-base"),
    ))
    assert _anchor(conn)[0] == topic_b

    before = _fingerprint(conn)
    stale_identity_key = "request:req-stale-i01"
    stale = service.apply(_transition(
        kind="enter_topic",
        target_topic_id=topic_c,
        base_nav_version=nav_stale,  # 依据早于用户后来的选择
        pending="cancel",
        identity=_identity(
            kind="request", identity_key=stale_identity_key, request_id="req-stale-i01"
        ),
    ))
    assert stale.changed is False, "I01：旧依据不得修改当前位置"
    assert stale.status in ("rejected", "superseded"), stale.status
    assert stale.code, "拒绝必须带结构化 code"
    assert _anchor(conn)[0] == topic_b, "旧请求不得覆盖用户后来的选择"
    after = _fingerprint(conn)
    assert after["nav_version"] == before["nav_version"]
    assert after["anchor"] == before["anchor"]
    assert after["intents"] == before["intents"]
    stale_rows = [
        row for row in _command_rows(conn) if row["identity_key"] == stale_identity_key
    ]
    assert len(stale_rows) == 1, "首次出现的被拒身份必须留一行拒绝事实"
    assert stale_rows[0]["status"] in ("rejected", "superseded")

    rejected_before = _fingerprint(conn)
    replay_rejected = service.apply(_transition(
        kind="enter_topic",
        target_topic_id=topic_c,
        base_nav_version=nav_stale,
        pending="cancel",
        identity=_identity(
            kind="request", identity_key=stale_identity_key, request_id="req-stale-i01"
        ),
    ))
    assert replay_rejected.changed is False
    assert replay_rejected.inactive is True or replay_rejected.replay is True
    assert _fingerprint(conn) == rejected_before, "重放既有拒绝必须零写入，且不得重新执行"


def test_apply_result_position_matches_read_same_source(tmp_path):
    conn, service, topic_a, topic_b, _frag_a, _frag_b = _setup_two_topics(tmp_path)
    result = service.apply(_transition(
        kind="enter_topic",
        target_topic_id=topic_b,
        base_nav_version=_nav_version(conn),
        pending="cancel",
        identity=_identity(kind="request", identity_key="request:req-wire"),
    ))
    current = service.read()
    for key in (
        "topic_id",
        "fragment_id",
        "historic",
        "pending_intent_id",
        "pending_intent_version",
        "pending_source_fragment_id",
        "nav_version",
        "position_version",
        "position_identity",
    ):
        assert result.position.get(key) == current.get(key), (
            "I02：命令结果 position 与统一读取必须同源（字段 %s）" % key
        )


# ── §3.3/§14.5 转换语义：两类版本严格分开 ────────────────────────────


def _setup_internal(tmp_path):
    conn = _new_db(tmp_path)
    service = _service(conn)
    service.ensure_identity()
    topic_id = _make_topic(conn, "内部推进话题")
    open_id = _open_fragment(conn, topic_id)
    closed_id = _closed_fragment(conn, topic_id)
    AnchorService(conn).set_active(topic_id, closed_id)
    nav_version_module.advance(conn, source="test.setup", topic_id=topic_id)
    return conn, service, topic_id, open_id, closed_id


def test_internal_refine_never_advances_nav_version_but_advances_position_version(tmp_path):
    conn, service, topic_id, open_id, _closed_id = _setup_internal(tmp_path)
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)
    transition = _transition(
        kind="internal_refine",
        source="turn.advance_anchor",
        target_topic_id=topic_id,
        target_fragment_id=open_id,
        expected_position_version=pos0,
        pending="keep",
        nav_decision=False,
        initiator="internal",
        identity=_identity(
            kind="internal",
            identity_key="internal:internal_refine:turn-f-1",
            request_id=None,
            internal_scope="turn-f-1",
        ),
        turn_id="turn-f-1",
    )
    result = service.apply(transition)
    assert result.changed is True
    assert _nav_version(conn) == nav0, "I03：内部细化不得伪装成用户导航决定"
    assert _position_version(conn) == pos0 + 1
    assert _anchor(conn) == (topic_id, open_id)

    # 内部转换必须带 expected_position_version（或等价事实前提），缺失要拒绝且零写入。
    before = _fingerprint(conn)
    missing_expected = _transition(
        kind="internal_refine",
        source="turn.advance_anchor",
        target_topic_id=topic_id,
        target_fragment_id=None,
        expected_position_version=None,
        pending="keep",
        nav_decision=False,
        initiator="internal",
        identity=_identity(
            kind="internal",
            identity_key="internal:internal_refine:turn-f-missing",
            request_id=None,
            internal_scope="turn-f-missing",
        ),
        turn_id="turn-f-missing",
    )
    rejected, exc = _apply_or_exc(service, missing_expected)
    assert exc is not None or rejected.changed is False, (
        "缺少 expected_position_version 的内部动作必须明确拒绝"
    )
    assert _fingerprint(conn) == before, "被拒绝的内部动作零写入"


def test_auto_segment_advances_only_position_version(tmp_path):
    conn, service, topic_id, open_id, _closed_id = _setup_internal(tmp_path)
    AnchorService(conn).set_active(topic_id, open_id)
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)
    FragmentManager(conn).seal(open_id, reason="capacity")
    result = service.apply(_transition(
        kind="auto_segment",
        source="capacity.segment",
        target_topic_id=topic_id,
        target_fragment_id=None,
        expected_position_version=pos0,
        pending="keep",
        nav_decision=False,
        initiator="internal",
        identity=_identity(
            kind="internal",
            identity_key="internal:auto_segment:turn-f-2",
            request_id=None,
            internal_scope="turn-f-2:%s" % open_id,
        ),
        turn_id="turn-f-2",
        causal={"sealed_fragment_id": open_id},
    ))
    assert result.changed is True
    assert _nav_version(conn) == nav0, "§14.5：auto_segment 不推进 nav_version"
    assert _position_version(conn) == pos0 + 1, "§14.5：封存开放片段一律 position_version +1"


def test_apply_continuation_resolves_pending_without_nav_advance(tmp_path):
    conn, service, topic_id, open_id, closed_id = _setup_internal(tmp_path)
    TurnBindingService(conn).register_intent_ex(
        topic_id, closed_id, request_id="req-apply-cont"
    )
    intent = TurnBindingService(conn).peek_intent()
    assert intent is not None and intent.state == "registered"
    AnchorService(conn).set_active(topic_id, open_id)
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)
    result = service.apply(_transition(
        kind="apply_continuation",
        source="turn.apply_continuation",
        target_topic_id=topic_id,
        target_fragment_id=open_id,
        expected_position_version=pos0,
        pending="resolve",
        pending_topic_id=topic_id,
        pending_fragment_id=open_id,
        pending_request_id="req-apply-cont",
        pending_intent_id=intent.intent_id,
        nav_decision=False,
        initiator="internal",
        identity=_identity(
            kind="internal",
            identity_key="internal:apply_continuation:turn-f-3",
            request_id=None,
            internal_scope="turn-f-3:%s" % intent.intent_id,
        ),
        turn_id="turn-f-3",
    ))
    assert result.changed is True
    assert _nav_version(conn) == nav0, "§3.3：落实接续的决定在登记时已计，nav_version 0"
    assert _position_version(conn) == pos0 + 1, "待接续 registered→resolved 是真实状态变化"
    assert TurnBindingService(conn).peek_intent() is None
    stored = TurnBindingService(conn).intent_by_id(intent.intent_id)
    assert stored is not None and stored.state == "resolved"
    assert stored.resolved_fragment_id == open_id


def test_select_history_tool_registration_keeps_anchor_and_click_moves_it(tmp_path):
    conn = _new_db(tmp_path)
    service = _service(conn)
    service.ensure_identity()
    topic_id = _make_topic(conn, "选历史话题")
    open_id = _open_fragment(conn, topic_id)
    closed_id = _closed_fragment(conn, topic_id)
    AnchorService(conn).set_active(topic_id, open_id)
    nav_version_module.advance(conn, source="test.setup", topic_id=topic_id)
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)

    # 14.5：工具登记路径只登记 pending，不动 Anchor、不广播位置事件；changed 时两版本各 +1。
    tool_result = service.apply(_transition(
        kind="select_history",
        source="tool.continue_from_fragment",
        target_topic_id=topic_id,
        target_fragment_id=closed_id,
        base_nav_version=nav0,
        pending="register",
        pending_topic_id=topic_id,
        pending_fragment_id=closed_id,
        pending_request_id="op-f-1",
        nav_decision=True,
        move_anchor=False,
        initiator="tool",
        operation_id="op-f-1",
        param_version=1,
        identity=_identity(
            kind="operation",
            identity_key="operation:op-f-1:1",
            request_id=None,
            operation_id="op-f-1",
            param_version=1,
        ),
    ))
    assert tool_result.changed is True
    assert _anchor(conn) == (topic_id, open_id), "工具登记路径不得移动 Anchor"
    assert _nav_version(conn) == nav0 + 1
    assert _position_version(conn) == pos0 + 1
    pending = TurnBindingService(conn).peek_intent()
    assert pending is not None and pending.source_fragment_id == closed_id

    # 点击路径：Anchor 落到历史片段并可见 historic=True。
    nav1 = _nav_version(conn)
    pos1 = _position_version(conn)
    click_result = service.apply(_transition(
        kind="select_history",
        source="manual.select_history",
        target_topic_id=topic_id,
        target_fragment_id=closed_id,
        base_nav_version=nav1,
        pending="register",
        pending_topic_id=topic_id,
        pending_fragment_id=closed_id,
        pending_request_id="req-click",
        nav_decision=True,
        move_anchor=True,
        initiator="user",
        identity=_identity(
            kind="request", identity_key="request:req-click", request_id="req-click"
        ),
    ))
    assert click_result.changed is True
    assert _anchor(conn) == (topic_id, closed_id)
    assert click_result.position["historic"] is True
    assert _nav_version(conn) == nav1 + 1
    assert _position_version(conn) == pos1 + 1


def test_open_fragment_cancels_registered_continuation(tmp_path):
    conn, service, topic_id, open_id, closed_id = _setup_internal(tmp_path)
    TurnBindingService(conn).register_intent_ex(
        topic_id, closed_id, request_id="req-old-cont"
    )
    assert TurnBindingService(conn).peek_intent() is not None
    AnchorService(conn).set_active(topic_id, open_id)
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)
    result = service.apply(_transition(
        kind="open_fragment",
        source="manual.open_fragment",
        target_topic_id=topic_id,
        target_fragment_id=open_id,
        base_nav_version=nav0,
        pending="cancel",
        nav_decision=True,
        initiator="user",
        identity=_identity(
            kind="request", identity_key="request:req-open", request_id="req-open"
        ),
    ))
    assert result.changed is True
    assert TurnBindingService(conn).peek_intent() is None, "E：返回开放片段不得残留旧接续选择"
    assert _anchor(conn) == (topic_id, open_id)
    assert _nav_version(conn) == nav0 + 1, "pending 被取消计入 nav_version"
    assert _position_version(conn) == pos0 + 1


def test_no_real_change_does_not_advance_either_version(tmp_path):
    """§3.2：同值/幂等命令不推进 position_version，也不推进 nav_version。"""
    conn, service, topic_a, _topic_b, frag_a, _frag_b = _setup_two_topics(tmp_path)
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)
    result = service.apply(_transition(
        kind="enter_topic",
        source="manual.enter_topic",
        target_topic_id=topic_a,
        target_fragment_id=frag_a,  # 与当前 Anchor 完全相同
        base_nav_version=nav0,
        pending="keep",
        nav_decision=True,
        initiator="user",
        identity=_identity(
            kind="request", identity_key="request:req-same", request_id="req-same"
        ),
    ))
    assert result.changed is False, "没有真实变化时 changed 必须为 False"
    assert _anchor(conn) == (topic_a, frag_a)
    assert _nav_version(conn) == nav0
    assert _position_version(conn) == pos0, "§3.2：无变化不推进 position_version"


def test_cancel_continuation_changes_pending_but_not_nav_version(tmp_path):
    """§14.9：取消接续不是新的导航选择 → nav_version 0；pending 变化 → position_version +1。"""
    conn, service, topic_id, _open_id, closed_id = _setup_internal(tmp_path)
    TurnBindingService(conn).register_intent_ex(
        topic_id, closed_id, request_id="req-cancel-cont"
    )
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)
    result = service.apply(_transition(
        kind="cancel_continuation",
        source="manual.cancel_continuation",
        target_topic_id=None,
        target_fragment_id=None,
        base_nav_version=nav0,
        pending="cancel",
        nav_decision=False,
        initiator="user",
        identity=_identity(
            kind="request", identity_key="request:req-cancel", request_id="req-cancel"
        ),
    ))
    assert result.changed is True
    assert TurnBindingService(conn).peek_intent() is None
    assert _nav_version(conn) == nav0, "§14.9：cancel_continuation 不推进 nav_version"
    assert _position_version(conn) == pos0 + 1, "pending 事实变化推进 position_version"


def test_enter_topic_none_fragment_stored_as_null(tmp_path):
    conn, service, _topic_a, topic_b, _frag_a, frag_b = _setup_two_topics(tmp_path)
    # 话题 B 也有开放片段，但 target_fragment_id=None 必须如实表示存储 NULL。
    result = service.apply(_transition(
        kind="enter_topic",
        source="tool.switch_topic",
        target_topic_id=topic_b,
        target_fragment_id=None,
        base_nav_version=_nav_version(conn),
        pending="cancel",
        nav_decision=True,
        initiator="tool",
        operation_id="op-enter",
        param_version=1,
        identity=_identity(
            kind="operation",
            identity_key="operation:op-enter:1",
            request_id=None,
            operation_id="op-enter",
            param_version=1,
        ),
    ))
    assert result.changed is True
    assert _anchor(conn) == (topic_b, None)
    assert result.position["fragment_id"] is None
    state = service.read()
    assert state["fragment_id"] is None, "读取端不得把 NULL 擅自解析成 open_fragment"
    assert frag_b  # 仅说明开放片段存在，且没有被当成本地位置


def _forged_identity(kind: str, identity_key: str):
    """绕过构造校验伪造一份 kind/前缀不一致的身份，专门验证 apply() 自身是否校验。"""
    identity = object.__new__(_ps().NavCommandIdentity)
    object.__setattr__(identity, "kind", kind)
    object.__setattr__(identity, "identity_key", identity_key)
    object.__setattr__(identity, "request_id", "forged-op")
    object.__setattr__(identity, "operation_id", None)
    object.__setattr__(identity, "param_version", None)
    object.__setattr__(identity, "internal_scope", None)
    return identity


def test_identity_kind_prefix_mismatch_rejected_zero_write(tmp_path):
    conn, service, _topic_a, topic_b, _frag_a, _frag_b = _setup_two_topics(tmp_path)
    before = _fingerprint(conn)
    result, exc = _apply_or_exc(service, _transition(
        kind="enter_topic",
        target_topic_id=topic_b,
        base_nav_version=_nav_version(conn),
        pending="cancel",
        identity=_forged_identity("request", "operation:forged-op:1"),
    ))
    assert exc is not None or result.changed is False, (
        "§14.8：apply() 必须校验 kind 与 identity_key 前缀一致"
    )
    assert _fingerprint(conn) == before, "身份前缀非法必须零写入"
    assert not any(
        row["identity_key"] == "operation:forged-op:1" for row in _command_rows(conn)
    )


# ── 生产写入口旁路静态清单（§4：所有生产位置写入必须且只能经 apply()）──


_ANCHOR_WRITE_ALLOW = {
    "services/position_state.py",
    "graph/anchors.py",
}
_INTENT_WRITE_ALLOW = {
    "services/position_state.py",
    "services/binding.py",
}
_SQL_ALLOW = {
    "services/position_state.py",
    "graph/anchors.py",
    "services/binding.py",
    "services/navigation_version.py",
    "storage/schema.py",
    "storage/migration_p1r_navigation.py",
}

_FORBIDDEN = [
    (re.compile(r"\.set_active\s*\("), _ANCHOR_WRITE_ALLOW),
    (
        re.compile(
            r"\.request_pending\s*\(|\.confirm_pending\s*\(|\.discard_pending\s*\(|\.restore_position\s*\("
        ),
        _ANCHOR_WRITE_ALLOW,
    ),
    (
        re.compile(
            r"bindings\.(?:register_intent_ex|register_intent|resolve_intent|cancel_intent|"
            r"consume_intent)\s*\(|TurnBindingService\([^)]*\)\s*\.(?:register_intent_ex|"
            r"register_intent|resolve_intent|cancel_intent|consume_intent)\s*\("
        ),
        _INTENT_WRITE_ALLOW,
    ),
    (
        re.compile(r"cas_or_advance\s*\(|navigation_version\.advance\s*\("),
        {"services/position_state.py", "services/navigation_version.py"},
    ),
    (
        re.compile(
            r"(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+cursor\b", re.IGNORECASE
        ),
        _SQL_ALLOW,
    ),
    (
        re.compile(
            r"(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+continuation_intents\b",
            re.IGNORECASE,
        ),
        _SQL_ALLOW,
    ),
    (
        re.compile(
            r"(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+navigation_version\b",
            re.IGNORECASE,
        ),
        _SQL_ALLOW | {"storage/migration_p1r_navigation.py"},
    ),
    (
        re.compile(
            r"(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+position_state\b", re.IGNORECASE
        ),
        {"services/position_state.py", "storage/migration_r16_position_state.py"},
    ),
]


def test_production_position_writes_only_through_unified_entry():
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("storage/migration") or relative.startswith("storage/schema.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, allowed in _FORBIDDEN:
            if relative in allowed:
                continue
            match = pattern.search(text)
            if match is not None:
                line = text[: match.start()].count("\n") + 1
                violations.append("%s:%d 直接写入（%s）" % (relative, line, pattern.pattern))
    assert not violations, (
        "生产位置写入必须且只能经 PositionStateService.apply()，发现旁路：\n"
        + "\n".join(violations)
    )


# ── §6 逻辑答案执行权 / 共享暂存 / 正式保存 ──────────────────────────


class CountingAdapter:
    mode = "native"
    model = "fake-r16-f"
    supports_stream = False

    def __init__(self, text: str = ANSWER + "\n" + ANSWER_TEXT, gates: int = 0) -> None:
        self.text = text
        self.calls = 0
        self.started = [asyncio.Event() for _ in range(gates)]
        self.release = [asyncio.Event() for _ in range(gates)]

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        index = self.calls
        self.calls += 1
        if index < len(self.started):
            self.started[index].set()
            await self.release[index].wait()
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class FakeOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
        self.calls.append(
            {"turn_id": turn_id, "topic_id": topic_id, "user_message_id": user_message_id}
        )
        return [
            ChatMessage(role="system", content="SYS_FOR:" + str(topic_id)),
            ChatMessage(role="user", content="原问题"),
        ]


def _wire_model(app: AppContext, adapter) -> None:
    app.turn_orchestrator = FakeOrchestrator()  # type: ignore[assignment]

    async def _build_adapter():
        return adapter

    app.build_adapter = _build_adapter  # type: ignore[method-assign]


@pytest.fixture()
def answer_app(tmp_path):
    conn = connect(tmp_path / "r16_f_answer.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


def _prepare_answer(app: AppContext, *, turn_id: str = "turn_f", name: str = "答案话题"):
    topic_id = app.topics.nodes.create_topic(name).id
    fragment = app.fragments.get_or_create_open(topic_id)
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="原问题（F 独立验收）",
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    return topic_id, fragment.id, message_id


def _make_record(app: AppContext, topic_id: str, message_id: str, *, record_id: str, **overrides):
    payload = {
        "turn_id": "turn_f",
        "topic_id": topic_id,
        "user_message_id": message_id,
        "operation_id": "op-r16-f",
        "param_version": 1,
        "base_nav_version": None,
        "recovery_nav_version": _nav_version(app.conn),
        "failed_stage": "context_prepare",
        "message": "话题已切换，回答准备失败",
        "stages": TopicStages(context_ready=True),
    }
    payload.update(overrides)
    payload["record_id"] = record_id
    return topic_recovery.record_topic_failure(app, **payload)


def _col(row, name: str, context: str):
    """读取新迁移列：列缺失时给出可读的契约失败，而不是裸 KeyError。"""
    if row is None:
        raise AssertionError("%s：行不存在" % context)
    try:
        return row[name]
    except (KeyError, IndexError):
        raise AssertionError(
            "%s：缺列 %s（迁移 83/84 未生效或实现未写该事实）" % (context, name)
        ) from None


def _answer_rows(app: AppContext) -> list:
    return app.conn.execute(
        "SELECT id, content, answer_key FROM messages WHERE role = 'assistant'"
    ).fetchall()


def _run_row(app: AppContext, answer_key: str):
    return app.conn.execute(
        "SELECT * FROM topic_answer_runs WHERE answer_key = ?", (answer_key,)
    ).fetchone()


def _install_failing_answer_insert(app: AppContext, *, enable: bool) -> None:
    """用真实 SQLite 触发器模拟「生成成功但保存失败」，不用 monkeypatch 内部函数。"""
    if enable:
        app.conn.execute(
            "CREATE TRIGGER IF NOT EXISTS f_fail_assistant_insert "
            "BEFORE INSERT ON messages WHEN NEW.role = 'assistant' BEGIN "
            "SELECT RAISE(ABORT, 'F simulated save failure'); END"
        )
    else:
        app.conn.execute("DROP TRIGGER IF EXISTS f_fail_assistant_insert")


def test_i10_staged_answer_shared_across_records_same_answer_key(answer_app):
    app = answer_app
    topic_id, _fragment_id, message_id = _prepare_answer(app)
    adapter = CountingAdapter()
    _wire_model(app, adapter)
    first = _make_record(app, topic_id, message_id, record_id="trec_f_a")
    second = _make_record(app, topic_id, message_id, record_id="trec_f_b")
    assert first["answer_key"] == second["answer_key"]

    _install_failing_answer_insert(app, enable=True)
    failed = asyncio.run(topic_recovery.retry_topic_answer(app, first["record_id"]))
    assert failed.get("answered") is not True
    assert adapter.calls == 1, "第一次执行只应调用一次模型"
    assert not _answer_rows(app), "保存失败时不得留下正式答案行"

    # 记录 A 已经拿到生成结果（共享暂存）；记录 B 同 answer_key 只补存、不再生成。
    _install_failing_answer_insert(app, enable=False)
    retried = asyncio.run(topic_recovery.retry_topic_answer(app, second["record_id"]))
    assert adapter.calls == 1, (
        "I10：同 answer_key 的另一条记录必须取得共享暂存结果，不得再调用模型；"
        "实际调用 %d 次" % adapter.calls
    )
    assert retried.get("answered") is True, retried
    rows = _answer_rows(app)
    assert len(rows) == 1, "I12：同一逻辑答案只能有一份正式持久化结果"
    assert rows[0]["content"] == ANSWER_TEXT, (
        "正式答案正文 = 剥离协议标记后的生成正文；实际=%r" % (rows[0]["content"],)
    )
    for record in (first, second):
        stored = topic_recovery.get_record(app, record["record_id"])
        assert stored["status"] == "answered"
        assert stored["answer_id"] == retried["answer_id"]
    run = _run_row(app, first["answer_key"])
    assert run is not None and str(run["status"]) == "answered"


def test_i10_run_phase_and_shared_staged_columns(answer_app):
    app = answer_app
    topic_id, _fragment_id, message_id = _prepare_answer(app)
    adapter = CountingAdapter()
    _wire_model(app, adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_f_phase")

    _install_failing_answer_insert(app, enable=True)
    failed = asyncio.run(topic_recovery.retry_topic_answer(app, record["record_id"]))
    assert failed.get("answered") is not True
    run = _run_row(app, record["answer_key"])
    assert run is not None
    assert str(_col(run, "status", "run 行")) == "pending"
    assert str(_col(run, "phase", "run 行")) == "staged", "保存失败后 run.phase 必须是 staged"
    assert str(_col(run, "staged_answer", "run 行") or "") == ANSWER_TEXT, (
        "I10：共享暂存正文唯一权威是 run.staged_answer（剥离协议标记后的正文）"
    )
    assert _col(run, "staged_at", "run 行"), "staged 正文必须记录 staged_at"

    _install_failing_answer_insert(app, enable=False)
    retried = asyncio.run(topic_recovery.retry_topic_answer(app, record["record_id"]))
    assert retried.get("answered") is True
    run = _run_row(app, record["answer_key"])
    assert str(_col(run, "phase", "run 行")) == "answered"
    assert str(_col(run, "status", "run 行")) == "answered"
    assert str(_col(run, "staged_answer", "run 行") or "") == "", (
        "补存成功后必须同事务清空 staged_answer"
    )
    assert _col(run, "staged_at", "run 行") is None


def test_i12_single_formal_answer_and_same_answer_id(answer_app):
    app = answer_app
    topic_id, _fragment_id, message_id = _prepare_answer(app)
    adapter = CountingAdapter()
    _wire_model(app, adapter)
    first = _make_record(app, topic_id, message_id, record_id="trec_f_12a")
    second = _make_record(app, topic_id, message_id, record_id="trec_f_12b")
    result = asyncio.run(topic_recovery.retry_topic_answer(app, first["record_id"]))
    assert result.get("answered") is True
    rows = _answer_rows(app)
    assert len(rows) == 1
    assert rows[0]["answer_key"] == first["answer_key"]
    assert adapter.calls == 1
    for record in (first, second):
        stored = topic_recovery.get_record(app, record["record_id"])
        assert stored["status"] == "answered"
        assert stored["answer_id"] == result["answer_id"], "I12：同 answer_key 返回相同 answer_id"
    retried = asyncio.run(topic_recovery.retry_topic_answer(app, second["record_id"]))
    assert retried.get("answered") is True
    assert retried.get("answer_id") == result["answer_id"]
    assert adapter.calls == 1, "已有正式答案时重试不得重新生成"


def test_run_phase_progresses_generating_then_answered(answer_app):
    app = answer_app
    topic_id, _fragment_id, message_id = _prepare_answer(app)
    adapter = CountingAdapter(gates=1)
    _wire_model(app, adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_f_lifecycle")

    async def _scenario():
        task = asyncio.create_task(
            topic_recovery.retry_topic_answer(app, record["record_id"])
        )
        await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
        run = _run_row(app, record["answer_key"])
        assert run is not None
        phase = str(_col(run, "phase", "run 行"))
        assert phase in ("preparing", "generating"), (
            "模型生成期间 phase 必须是 preparing/generating，不能提前 answered：%r"
            % (phase,)
        )
        adapter.release[0].set()
        return await asyncio.wait_for(task, timeout=10)

    result = asyncio.run(_scenario())
    assert result.get("answered") is True
    run = _run_row(app, record["answer_key"])
    assert str(_col(run, "phase", "run 行")) == "answered"


def test_i13_recovery_is_read_only_for_navigation(answer_app):
    app = answer_app
    topic_id, fragment_id, message_id = _prepare_answer(app)
    closed_id = _closed_fragment(app.conn, topic_id)
    intent = app.bindings.register_intent_ex(topic_id, closed_id, request_id="req-i13")
    intent = intent.intent
    nav_before = _nav_version(app.conn)
    anchor_before = _anchor(app.conn)
    intent_rows_before = _intent_rows(app.conn)
    position_version_before = _position_version(app.conn)
    adapter = CountingAdapter()
    _wire_model(app, adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_f_i13")
    result = asyncio.run(topic_recovery.retry_topic_answer(app, record["record_id"]))
    assert result.get("answered") is True
    assert _nav_version(app.conn) == nav_before, "I13：恢复不得推进导航决定版本"
    assert _anchor(app.conn) == anchor_before, "I13：恢复不得移动当前位置"
    assert _intent_rows(app.conn) == intent_rows_before, (
        "I13：恢复不得改 pending continuation（登记态保持）"
    )
    assert _position_version(app.conn) == position_version_before
    assert len(_answer_rows(app)) == 1
    assert intent.state == "registered"


def test_i14_attribution_mismatch_never_becomes_formal_answer(answer_app):
    app = answer_app
    topic_id, _fragment_id, message_id = _prepare_answer(app)
    other_topic = app.topics.nodes.create_topic("另一个话题").id
    app.fragments.get_or_create_open(other_topic)
    adapter = CountingAdapter()
    _wire_model(app, adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_f_i14")
    # 数据损坏/越权：记录声称属于另一个话题，但原用户消息仍属于第一个话题。
    with transaction(app.conn):
        app.conn.execute(
            "UPDATE topic_recovery_records SET topic_id = ? WHERE record_id = ?",
            (other_topic, record["record_id"]),
        )
    result = asyncio.run(topic_recovery.retry_topic_answer(app, record["record_id"]))
    assert result.get("answered") is not True, "I14：归属不一致不得作为正式回答落库"
    assert result.get("ok") is False
    assert result.get("code") in ("answer_attribution_mismatch", "answer_context_missing"), (
        "§14.6 冻结 I14 结构化拒绝码；实际=%r" % (result.get("code"),)
    )
    assert result.get("recoverable") is True
    assert result.get("status_code") == 503, "§14.6：I14 拒绝的 HTTP 映射为 503"
    assert not _answer_rows(app), "归属不一致时零正式答案行"
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] != "answered"


def test_i11_old_executor_late_write_cannot_touch_staged_state(answer_app, monkeypatch):
    app = answer_app
    topic_id, _fragment_id, message_id = _prepare_answer(app)
    app.instance_id = "inst_f_old"
    now = datetime.now(timezone.utc).isoformat()
    with transaction(app.conn):
        app.conn.execute(
            "INSERT OR REPLACE INTO instances "
            "(instance_id, pid, host, started_at, last_heartbeat, exited_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("inst_f_old", 999999, "test-host", now, now, now),
        )
    clock = {"now": datetime(2026, 11, 11, 0, 0, 0, tzinfo=timezone.utc)}

    monkeypatch.setattr(topic_recovery, "_now_utc", lambda: clock["now"])
    adapter = CountingAdapter(gates=2)
    _wire_model(app, adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_f_i11")

    async def _scenario():
        first = asyncio.create_task(
            topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=600)
        )
        await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
        clock["now"] = clock["now"] + timedelta(seconds=700)
        app.instance_id = "inst_f_new"
        second = asyncio.create_task(
            topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=600)
        )
        await asyncio.wait_for(adapter.started[1].wait(), timeout=10)
        run_before = dict(_run_row(app, record["answer_key"]))
        record_before = dict(
            app.conn.execute(
                "SELECT * FROM topic_recovery_records WHERE record_id = ?",
                (record["record_id"],),
            ).fetchone()
        )
        adapter.release[0].set()  # 旧执行者迟到成功
        old_done = await asyncio.wait_for(first, timeout=10)
        run_after = dict(_run_row(app, record["answer_key"]))
        record_after = dict(
            app.conn.execute(
                "SELECT * FROM topic_recovery_records WHERE record_id = ?",
                (record["record_id"],),
            ).fetchone()
        )
        adapter.release[1].set()
        new_done = await asyncio.wait_for(second, timeout=10)
        return run_before, record_before, old_done, run_after, record_after, new_done, dict(
            _run_row(app, record["answer_key"])
        )

    run_before, record_before, old_done, run_after, record_after, new_done, run_final = (
        asyncio.run(_scenario())
    )
    assert old_done.get("superseded") is True or old_done.get("ok") is False, old_done
    for key in (
        "status",
        "generation",
        "owner",
        "owner_instance",
        "claim_until",
        "record_id",
        "answer_id",
        "phase",
        "staged_answer",
        "staged_at",
        "staged_model",
    ):
        assert run_after.get(key) == run_before.get(key), (
            "I11：旧执行者迟到写不得改 run 行的 %s 事实" % key
        )
    for key in (
        "status",
        "execution_generation",
        "execution_owner",
        "claim_owner",
        "claim_until",
        "answer_text",
        "last_error",
        "answer_id",
        "topic_id",
        "user_message_id",
        "failed_stage",
    ):
        assert record_after.get(key) == record_before.get(key), (
            "I11：旧执行者迟到写不得改恢复记录的 %s" % key
        )
    assert new_done.get("answered") is True, new_done
    assert str(_col(run_final, "phase", "run 行")) == "answered"
    assert str(_col(run_final, "staged_answer", "run 行") or "") == ""
    assert len(_answer_rows(app)) == 1


# ── §10.2 A—I 既有回归（部分为基线即绿的守卫）─────────────────────────


def test_regression_e_open_fragment_via_navigation_service_cancels_pending(tmp_path):
    """E：真正返回开放片段时，不残留指向旧历史的接续选择（生产包装入口）。"""
    conn = _new_db(tmp_path)
    service = _service(conn)
    service.ensure_identity()
    from agent.services.navigation import TopicNavigationService

    navigation = TopicNavigationService(conn)
    topic_id = _make_topic(conn, "回归E话题")
    open_id = _open_fragment(conn, topic_id)
    closed_id = _closed_fragment(conn, topic_id)
    TurnBindingService(conn).register_intent_ex(topic_id, closed_id, request_id="req-e-old")
    assert TurnBindingService(conn).peek_intent() is not None
    nav0 = _nav_version(conn)
    pos0 = _position_version(conn)
    outcome = navigation.register_and_position_continuation(
        topic_id, open_id, request_id="req-e-open"
    )
    assert outcome.get("changed") is True
    assert TurnBindingService(conn).peek_intent() is None
    assert _nav_version(conn) == nav0 + 1
    assert _position_version(conn) == pos0 + 1, "§14.5：pending 取消是真实位置状态变化"


def test_regression_d_replay_history_request_keeps_later_position(tmp_path):
    """D：开放片段 A → 历史 B → 重放原请求 A，B 保持不变（命令重放零写入）。"""
    conn = _new_db(tmp_path)
    service = _service(conn)
    service.ensure_identity()
    from agent.services.navigation import TopicNavigationService

    navigation = TopicNavigationService(conn)
    topic_id = _make_topic(conn, "回归D话题")
    open_id = _open_fragment(conn, topic_id)
    closed_id = _closed_fragment(conn, topic_id)
    first = navigation.register_and_position_continuation(
        topic_id, closed_id, request_id="req-d-1"
    )
    assert first.get("changed") is True
    assert _anchor(conn) == (topic_id, closed_id)
    before = _fingerprint(conn)
    replay = navigation.register_and_position_continuation(
        topic_id, closed_id, request_id="req-d-1"
    )
    assert replay.get("replay") is True
    assert replay.get("changed") is False
    assert _fingerprint(conn) == before, "D：重放原请求必须零写入，B 位置保持不变"
    assert _anchor(conn) == (topic_id, closed_id)
    assert open_id != closed_id
