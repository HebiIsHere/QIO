"""知识保存申请：原子批准 / 修改后保存 / 不保存 / 打回（定向测试）。

重点断言的是**行为**，不是文案：

* 「修改后保存」真正生效的是用户确认后的内容（一次事务，不先批准原文）；
* 提交前候选被改动 / 版本不符 / 已失效 / 已被别处处理 → 明确 409，绝不覆盖；
* 重复提交幂等：不会新增第二个 active 版本；
* 事务中途失败整体回滚（候选仍是 pending_review，没有半个 active）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent.api.knowledge_approval_routes import register_knowledge_approval_routes
from agent.api.server import create_app
from agent.config import Settings
from agent.knowledge.lifecycle import KnowledgeService, KnowledgeState
from agent.services import knowledge_approval as service
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def conn(tmp_path):
    connection = connect(tmp_path / "knowledge-approval.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture()
def client(conn, tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    register_knowledge_approval_routes(app, app.state.ctx)
    return TestClient(app)


def _pending(conn, *, category: str = "user_profile", content: str = "用户偏好安静的界面") -> str:
    ks = KnowledgeService(conn)
    item = ks.create(category=category, content=content)
    ks.submit(item.id)
    return item.id


def _states(conn) -> list[str]:
    rows = conn.execute("SELECT state FROM knowledge ORDER BY created_at, id").fetchall()
    return [str(row["state"]) for row in rows]


def _active_rows(conn) -> list:
    return conn.execute(
        "SELECT id, content, supersedes_id, version, chain_id FROM knowledge WHERE state = 'active'"
    ).fetchall()


def _approve(client, knowledge_id: str, **overrides):
    body = {"decision": "approve", **overrides}
    return client.post(f"/api/knowledge/{knowledge_id}/approval", json=body)


def test_approve_original_activates_in_one_request(client, conn):
    kid = _pending(conn)
    r = _approve(client, kid, expected_version=1, expected_content="用户偏好安静的界面")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["decision"] == "approve"
    assert body["knowledge"]["state"] == "active"
    assert [row["state"] for row in conn.execute("SELECT state FROM knowledge")] == ["active"]
    provenance = json.loads(
        conn.execute("SELECT provenance FROM knowledge WHERE id = ?", (kid,)).fetchone()["provenance"]
    )
    assert provenance["approved_by"] == "user"
    assert provenance["approval_decision"] == "approve"


def test_approve_modified_saves_user_content_and_keeps_history(client, conn):
    kid = _pending(conn)
    r = client.post(
        f"/api/knowledge/{kid}/approval",
        json={
            "decision": "approve_modified",
            "content": "用户偏好安静、留白充足的界面",
            "expected_version": 1,
            "expected_content": "用户偏好安静的界面",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already_handled"] is False
    new_item = body["knowledge"]
    assert new_item["content"] == "用户偏好安静、留白充足的界面"
    assert new_item["state"] == "active"
    assert new_item["supersedes_id"] == kid
    assert new_item["version"] == 2
    # 原文仍在历史里（revoked），只有一条 active，并且是用户确认后的内容
    states = dict(conn.execute("SELECT id, state FROM knowledge").fetchall())
    assert states[kid] == "revoked"
    actives = _active_rows(conn)
    assert len(actives) == 1
    assert actives[0]["content"] == "用户偏好安静、留白充足的界面"
    provenance = json.loads(
        conn.execute(
            "SELECT provenance FROM knowledge WHERE id = ?", (new_item["id"],)
        ).fetchone()["provenance"]
    )
    assert provenance["edited_by_user"] is True
    assert provenance["approved_from"] == kid
    assert provenance["original_content"] == "用户偏好安静的界面"


def test_modified_save_rejects_stale_expected_content_without_writing(client, conn):
    kid = _pending(conn)
    # 模拟用户在别处看过之后候选又被修改（这里直接改库，模拟「提交前变化」）
    conn.execute(
        "UPDATE knowledge SET content = ?, updated_at = ? WHERE id = ?",
        ("已经变了的内容", "2099-01-01T00:00:00+00:00", kid),
    )
    conn.commit()
    r = client.post(
        f"/api/knowledge/{kid}/approval",
        json={
            "decision": "approve_modified",
            "content": "我的编辑",
            "expected_version": 1,
            "expected_content": "用户偏好安静的界面",
        },
    )
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "content_changed"
    assert body["current"]["content"] == "已经变了的内容"
    # 什么都没写：没有新版本、候选仍是待审、内容仍是被别人改后的
    assert _active_rows(conn) == []
    assert [row["state"] for row in conn.execute("SELECT state FROM knowledge")] == ["pending_review"]


def test_modified_save_rejects_version_mismatch(client, conn):
    kid = _pending(conn)
    r = client.post(
        f"/api/knowledge/{kid}/approval",
        json={"decision": "approve_modified", "content": "x", "expected_version": 99},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "version_changed"
    assert [row["state"] for row in conn.execute("SELECT state FROM knowledge")] == ["pending_review"]


def test_approve_is_all_or_nothing_when_insert_fails(conn, monkeypatch):
    kid = _pending(conn)

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟写新版本失败")

    monkeypatch.setattr(service, "_insert_active", _boom)
    with pytest.raises(RuntimeError):
        service.approve_pending_knowledge(
            conn,
            kid,
            content="用户编辑后的内容",
            expected_version=1,
            expected_content="用户偏好安静的界面",
        )
    # 整体回滚：候选没被批准、没被撤销、没有半个 active 版本
    assert [row["state"] for row in conn.execute("SELECT state FROM knowledge")] == ["pending_review"]
    assert _active_rows(conn) == []
    item = KnowledgeService(conn).get(kid)
    assert item is not None and item.content == "用户偏好安静的界面"


def test_duplicate_modified_submit_is_idempotent_and_never_adds_second_active(client, conn):
    kid = _pending(conn)
    body = {
        "decision": "approve_modified",
        "content": "用户偏好安静、留白充足的界面",
        "expected_version": 1,
        "expected_content": "用户偏好安静的界面",
    }
    first = client.post(f"/api/knowledge/{kid}/approval", json=body)
    assert first.status_code == 200
    rows_after_first = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]

    second = client.post(f"/api/knowledge/{kid}/approval", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["already_handled"] is True
    assert conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0] == rows_after_first
    assert len(_active_rows(conn)) == 1

    # 同一候选换一个内容再审：不能覆盖已经生效的版本，必须明确冲突
    third = client.post(
        f"/api/knowledge/{kid}/approval",
        json={**body, "content": "另一个内容"},
    )
    assert third.status_code == 409
    assert third.json()["code"] == "already_handled"
    assert _active_rows(conn)[0]["content"] == "用户偏好安静、留白充足的界面"


def test_ignore_marks_not_saved_and_is_idempotent(client, conn):
    kid = _pending(conn)
    r = _approve(client, kid, decision="ignore", expected_version=1)
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "ignore"
    assert [row["state"] for row in conn.execute("SELECT state FROM knowledge")] == ["revoked"]
    provenance = json.loads(
        conn.execute("SELECT provenance FROM knowledge WHERE id = ?", (kid,)).fetchone()["provenance"]
    )
    assert provenance["ignored_at"]
    # 再点一次「不保存」：幂等，不报错、不重复写
    again = _approve(client, kid, decision="ignore")
    assert again.status_code == 200
    assert again.json()["already_handled"] is True
    # 已经「不保存」的候选不能被另一个入口再批准
    late = _approve(client, kid)
    assert late.status_code == 409
    assert _active_rows(conn) == []


def test_reject_returns_draft_and_records_rejection(client, conn):
    kid = _pending(conn)
    r = _approve(client, kid, decision="reject")
    assert r.status_code == 200, r.text
    assert r.json()["knowledge"]["state"] == "draft"
    provenance = json.loads(
        conn.execute("SELECT provenance FROM knowledge WHERE id = ?", (kid,)).fetchone()["provenance"]
    )
    assert provenance.get("rejected_at")
    again = _approve(client, kid, decision="reject")
    assert again.status_code == 200
    assert again.json()["already_handled"] is True


def test_unknown_decision_and_missing_item(client, conn):
    kid = _pending(conn)
    r = _approve(client, kid, decision="maybe")
    assert r.status_code == 400
    assert r.json()["code"] == "knowledge_approval_invalid"
    assert _approve(client, "kn_missing").status_code == 404


def test_approve_does_not_smuggle_edits_through_confirm(client, conn):
    kid = _pending(conn)
    r = _approve(client, kid, content="偷偷改的内容", expected_content="用户偏好安静的界面")
    assert r.status_code == 400
    assert [row["state"] for row in conn.execute("SELECT state FROM knowledge")] == ["pending_review"]


def test_background_high_impact_approval_kind_stays_on_approvals_channel(client, conn):
    """后台维护对既有知识的审批带 approval_id，不归本接口；这里只锁定路由边界。"""
    # 知识保存接口只认 knowledge id：拿 approval id 来会被当作不存在的知识
    r = _approve(client, "appr_1234567890ab")
    assert r.status_code == 404
    assert r.json()["code"] == "knowledge_not_found"
