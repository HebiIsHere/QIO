"""放弃终态的**可靠保存**（契约 v2 A 章）。

缺口：`_write_state()` 吞掉 OSError，而 `abandon()` 改完内存就返回成功 ——
任务当场消失、重启后又出现。这里锁住四件事：

1. 严格路径 `_write_state_strict` / `_write_long_term_strict`：同目录临时文件 →
   flush+fsync → os.replace → **回读校验**；任何一步失败都抛 `StatePersistError`；
2. `abandon()` 事务式：失败时内存回滚到与磁盘一致（绝不允许「内存已放弃、
   磁盘没放弃」），`revoked` 只报**已经落盘**的那部分；
3. 原子写的临时文件（`state.json.tmp`）不进文件视图，不会把测试证据变成假 stale；
4. 进程在三个中断点被强杀后，重启看到的是「旧内容」或「新内容」，不是半截。

注入失败一律用 monkeypatch，**不在产品代码里加测试开关**。
"""

from __future__ import annotations

import builtins
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.tools import dev_workspace
from agent.tools.dev_workspace import DevWorkspace, StatePersistError


# ---------------------------------------------------------------------------
# 工具与注入器
# ---------------------------------------------------------------------------


def _ws(tmp_path) -> DevWorkspace:
    return DevWorkspace(tmp_path / "ws")


def _grant(ws: DevWorkspace, task_id: str, *, lifetime: str = "task") -> None:
    ws.grant_test_authorization(
        task_id, policy_fingerprint="p1", executor="subprocess", lifetime=lifetime
    )


def _state_bytes(task) -> bytes:
    return (task.dir / "state.json").read_bytes()


def _state_json(task) -> dict:
    return json.loads((task.dir / "state.json").read_text(encoding="utf-8"))


def _fail_temp_write(monkeypatch) -> None:
    """临时文件写不进去（例如磁盘写失败）。"""
    real_open = builtins.open

    def _exploding_open(file, mode="r", *args, **kwargs):
        if "w" in str(mode) or "a" in str(mode):
            raise OSError("模拟：临时文件写不进去")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(dev_workspace, "open", _exploding_open, raising=False)


def _fail_replace_for(monkeypatch, *, name: str) -> None:
    """`os.replace` 对指定文件名失败（其它文件照常替换）。"""
    real_replace = os.replace

    def _exploding_replace(src, dst, *args, **kwargs):
        if Path(dst).name == name:
            raise OSError(f"模拟：替换 {name} 失败")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(dev_workspace.os, "replace", _exploding_replace)


def _tamper_written_payload(monkeypatch, *, abandoned: bool = False) -> None:
    """写入成功，但写进去的内容不是这次要写的（回读校验必须发现）。"""
    real_atomic = dev_workspace._atomic_write_json

    def _tampered(path, payload):
        broken = dict(payload)
        broken["abandoned"] = abandoned
        real_atomic(path, broken)

    monkeypatch.setattr(dev_workspace, "_atomic_write_json", _tampered)


def _write_corrupt_payload(monkeypatch) -> None:
    """写入「半截 JSON」：回读校验必须发现（不能把坏文件当成写成功）。"""
    real_atomic = dev_workspace._atomic_write_json

    def _corrupt(path, payload):
        real_atomic(path, payload)
        path.write_text('{"schema": 3, "source": "qio.dev_workspace", "id": "ws_', encoding="utf-8")

    monkeypatch.setattr(dev_workspace, "_atomic_write_json", _corrupt)


# ---------------------------------------------------------------------------
# 1. 原子写与文件视图
# ---------------------------------------------------------------------------


def test_successful_abandon_leaves_a_complete_state_and_no_temp_file(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("x")

    result = ws.abandon(task.id)

    assert result["ok"] is True
    assert result["status"] == "abandoned"
    assert result["persisted"] is True
    data = _state_json(task)
    assert data["id"] == task.id
    assert data["abandoned"] is True
    assert data["abandoned_at"]
    assert data["phase"] == "abandoned"
    assert not (task.dir / "state.json.tmp").exists(), "成功路径不该留下临时文件"


def test_strict_write_verifies_by_reading_back(tmp_path):
    """`_write_state_strict` 的回读校验：写对了才通过。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    task.abandoned = True
    task.abandoned_at = "2026-10-04T00:00:00+00:00"
    task.phase = "abandoned"

    dev_workspace._write_state_strict(task)  # 不抛就是通过

    assert _state_json(task)["abandoned"] is True


def test_strict_write_rejects_a_mismatched_payload(tmp_path, monkeypatch):
    """写进去的不是「已放弃」→ 回读校验失败 → StatePersistError。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    task.abandoned = True
    task.phase = "abandoned"

    with monkeypatch.context() as m:
        _tamper_written_payload(m, abandoned=False)
        with pytest.raises(StatePersistError) as excinfo:
            dev_workspace._write_state_strict(task)

    assert "回读校验" in str(excinfo.value)
    assert excinfo.value.task_id == task.id


def test_leftover_temp_file_does_not_change_the_content_digest(tmp_path):
    """残留 `state.json.tmp` 不能改变内容摘要 —— 否则测试证据会变成假 stale。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 1\n")
    ws.record_test(task.id, True, "1/1 tests passed")
    digest_before = ws.content_digest(task.id)
    files_before = ws.list_files(task.id)

    # 崩溃残留：临时文件（顶层 + 子目录各一个）
    (task.dir / "state.json.tmp").write_text('{"half', encoding="utf-8")
    (task.dir / "pkg").mkdir(exist_ok=True)
    (task.dir / "pkg" / "state.json.tmp").write_text("半截", encoding="utf-8")

    assert ws.content_digest(task.id) == digest_before
    assert ws.list_files(task.id) == files_before
    assert "state.json.tmp" not in ws.project_files(task.id)
    assert "pkg/state.json.tmp" not in ws.project_files(task.id)
    state = ws.status(task.id)
    assert state["evidence_state"] == "current"
    assert state["test_evidence_current"] is True

    # 重启后同样不算数：证据仍然是「对应当前内容」
    reborn = DevWorkspace(tmp_path / "ws")
    restored = reborn.status(task.id)
    assert restored["evidence_state"] == "current"
    assert restored["test_evidence_current"] is True


def test_file_tools_cannot_write_state_temp_files(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("x")

    for name in ("state.json.tmp", "pkg/state.json.tmp", "state.json.bak"):
        with pytest.raises(ValueError):
            ws.write_file(task.id, name, "伪造状态")
    assert not (task.dir / "state.json.tmp").exists()


# ---------------------------------------------------------------------------
# 2. 注入失败：不报成功、内存与磁盘一致、重试收敛
# ---------------------------------------------------------------------------


def test_temp_write_failure_is_reported_and_changes_nothing(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    task = ws.create("x")
    ws.write_file(task.id, "tool.py", "print(1)\n")
    _grant(ws, task.id)
    before_state = _state_bytes(task)
    before_phase = ws.status(task.id)["phase"]

    with monkeypatch.context() as m:
        _fail_temp_write(m)
        result = ws.abandon(task.id)

    assert result["ok"] is False
    assert result["status"] == "persist_failed"
    assert result["persisted"] is False
    assert result["revoked"] is False, "授权没有被落盘收回，就不能报 revoked"
    assert result["message"]
    # 内存没有停在「已放弃」
    state = ws.status(task.id)
    assert state["abandoned"] is False
    assert state["phase"] == before_phase
    assert state["test_authorized"] is True, "失败时授权必须原样还在"
    # 磁盘一个字节都没动
    assert _state_bytes(task) == before_state
    # 重启后不复活（任务本来就没被放弃）
    assert DevWorkspace(tmp_path / "ws").status(task.id)["abandoned"] is False
    # 重试收敛
    retry = ws.abandon(task.id)
    assert retry["ok"] is True and retry["persisted"] is True and retry["revoked"] is True
    assert DevWorkspace(tmp_path / "ws").status(task.id)["abandoned"] is True


def test_replace_failure_keeps_the_old_file_and_cleans_the_temp(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    task = ws.create("x")
    before_state = _state_bytes(task)

    with monkeypatch.context() as m:
        _fail_replace_for(m, name="state.json")
        result = ws.abandon(task.id)

    assert result["ok"] is False and result["status"] == "persist_failed"
    # 正式文件保持旧内容：不会出现空文件 / 半截内容
    assert _state_bytes(task) == before_state
    assert _state_json(task)["abandoned"] is False
    assert not (task.dir / "state.json.tmp").exists(), "失败时要尽力清掉临时文件"
    assert ws.status(task.id)["abandoned"] is False
    assert DevWorkspace(tmp_path / "ws").status(task.id)["abandoned"] is False

    retry = ws.abandon(task.id)
    assert retry["ok"] is True and retry["persisted"] is True
    assert _state_json(task)["abandoned"] is True


def test_readback_mismatch_is_treated_as_failure(tmp_path, monkeypatch):
    """写成功了但内容不对：回读校验必须拦下，并按失败回滚。"""
    ws = _ws(tmp_path)
    task = ws.create("x")

    with monkeypatch.context() as m:
        _tamper_written_payload(m, abandoned=False)
        result = ws.abandon(task.id)

    assert result["ok"] is False and result["status"] == "persist_failed"
    assert result["persisted"] is False
    assert ws.status(task.id)["abandoned"] is False
    # 磁盘上的内容与内存一致（都是「没放弃」）
    assert _state_json(task)["abandoned"] is False
    assert DevWorkspace(tmp_path / "ws").status(task.id)["abandoned"] is False

    retry = ws.abandon(task.id)
    assert retry["ok"] is True
    assert DevWorkspace(tmp_path / "ws").status(task.id)["abandoned"] is True


def test_corrupt_payload_is_treated_as_failure(tmp_path, monkeypatch):
    """半截 JSON 也必须被发现：读不回来就不算写成功。"""
    ws = _ws(tmp_path)
    task = ws.create("x")

    with monkeypatch.context() as m:
        _write_corrupt_payload(m)
        result = ws.abandon(task.id)

    assert result["ok"] is False and result["status"] == "persist_failed"
    assert ws.status(task.id)["abandoned"] is False
    # 读不回来的状态按「未知」处理：绝不能推断成「已放弃」
    assert DevWorkspace(tmp_path / "ws").status(task.id)["abandoned"] is False


# ---------------------------------------------------------------------------
# 3. 长期授权：先落盘后失败 → 如实反馈部分完成
# ---------------------------------------------------------------------------


def test_long_term_lands_first_then_state_failure_reports_partial(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    task = ws.create("x")
    other = ws.create("另一个任务")
    _grant(ws, task.id)                              # 任务级
    _grant(ws, task.id, lifetime="long_term")        # 长期（跨任务）
    assert ws.test_authorized(other.id, policy_fingerprint="p1", executor="subprocess") is True

    with monkeypatch.context() as m:
        _fail_replace_for(m, name="state.json")
        result = ws.abandon(task.id)

    assert result["ok"] is False
    assert result["status"] == "persist_failed"
    assert result["persisted"] is False
    assert result["revoked"] is True, "长期授权已经落盘收回：必须如实反馈这部分"
    assert "长期授权已经收回" in result["message"]
    assert "重试" in result["message"]
    # 任务终态回滚：任务还在，任务级授权也还在
    state = ws.status(task.id)
    assert state["abandoned"] is False
    assert state["test_authorized"] is True
    # 长期授权已收回：内存与磁盘一致（重启后不会在别的任务上生效）
    assert ws.test_authorized(other.id, policy_fingerprint="p1", executor="subprocess") is False
    reborn = DevWorkspace(tmp_path / "ws")
    assert reborn.test_authorized(
        other.id, policy_fingerprint="p1", executor="subprocess"
    ) is False

    # 重试：把任务级授权与终态收干净
    retry = ws.abandon(task.id)
    assert retry["ok"] is True and retry["persisted"] is True and retry["revoked"] is True
    final = DevWorkspace(tmp_path / "ws")
    assert final.status(task.id)["abandoned"] is True
    assert final.status(task.id)["test_authorized"] is False


def test_long_term_write_failure_rolls_back_everything(tmp_path, monkeypatch):
    ws = _ws(tmp_path)
    task = ws.create("x")
    other = ws.create("另一个任务")
    _grant(ws, task.id, lifetime="long_term")
    before_state = _state_bytes(task)
    long_term_file = ws.root_dir / "long_term_authorizations.json"
    before_long_term = long_term_file.read_bytes()

    with monkeypatch.context() as m:
        _fail_replace_for(m, name="long_term_authorizations.json")
        result = ws.abandon(task.id)

    assert result["ok"] is False and result["status"] == "persist_failed"
    assert result["revoked"] is False
    assert "长期授权" in result["message"]
    # 内存与磁盘都没变：长期授权还在，任务也没被放弃
    assert ws.test_authorized(other.id, policy_fingerprint="p1", executor="subprocess") is True
    assert long_term_file.read_bytes() == before_long_term
    assert _state_bytes(task) == before_state
    assert DevWorkspace(tmp_path / "ws").status(task.id)["abandoned"] is False

    retry = ws.abandon(task.id)
    assert retry["ok"] is True and retry["revoked"] is True
    assert DevWorkspace(tmp_path / "ws").test_authorized(
        other.id, policy_fingerprint="p1", executor="subprocess"
    ) is False


# ---------------------------------------------------------------------------
# 4. 幂等与重试收敛
# ---------------------------------------------------------------------------


def test_repeated_abandon_is_an_idempotent_success(tmp_path):
    """保存成功但响应丢失 → 重复请求幂等成功，不再改任何东西。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    first = ws.abandon(task.id)
    assert first["ok"] is True and first["persisted"] is True
    before = _state_bytes(task)

    second = ws.abandon(task.id)

    assert second["ok"] is True
    assert second["status"] == "already_abandoned"
    assert second["persisted"] is True
    assert second["revoked"] is False
    assert _state_bytes(task) == before


def test_retry_cleans_up_authorizations_left_by_a_partial_state(tmp_path):
    """历史 / 异常状态可能在磁盘上留下「终态已写、授权没收」的残余：重试要收干净。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    other = ws.create("另一个任务")
    _grant(ws, task.id)
    _grant(ws, task.id, lifetime="long_term")
    # 模拟「上次放弃只写下了终态」：内存里已是终态，授权还挂着
    task.abandoned = True
    task.abandoned_at = "2026-10-04T00:00:00+00:00"
    task.phase = "abandoned"

    result = ws.abandon(task.id)

    assert result["ok"] is True
    assert result["status"] == "already_abandoned"
    assert result["persisted"] is True
    assert result["revoked"] is True, "重试必须把没收回的授权收干净"
    final = DevWorkspace(tmp_path / "ws")
    assert final.status(task.id)["abandoned"] is True
    assert final.status(task.id)["test_authorized"] is False
    assert final.test_authorized(
        other.id, policy_fingerprint="p1", executor="subprocess"
    ) is False
    assert final.authorizations() == []


def test_retry_failure_says_the_task_stays_abandoned_but_the_grant_is_not_revoked(
    tmp_path, monkeypatch
):
    ws = _ws(tmp_path)
    task = ws.create("x")
    _grant(ws, task.id)
    task.abandoned = True
    task.phase = "abandoned"

    with monkeypatch.context() as m:
        _fail_replace_for(m, name="state.json")
        result = ws.abandon(task.id)

    assert result["ok"] is False
    assert result["status"] == "persist_failed"
    assert result["persisted"] is False
    assert result["revoked"] is False
    assert "任务已经放弃" in result["message"]
    assert "还没能收回" in result["message"]
    # 授权没有被偷偷清掉（内存回滚）
    assert ws.status(task.id)["test_authorized"] is True

    retry = ws.abandon(task.id)
    assert retry["ok"] is True and retry["revoked"] is True
    assert ws.status(task.id)["test_authorized"] is False


def test_retry_also_repairs_a_state_file_that_never_says_abandoned(tmp_path):
    """内存已放弃、磁盘没放弃：重试必须把终态也写下去（否则重启就复活）。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    task.abandoned = True
    task.abandoned_at = "2026-10-04T00:00:00+00:00"
    task.phase = "abandoned"
    assert _state_json(task)["abandoned"] is False

    result = ws.abandon(task.id)

    assert result["ok"] is True and result["persisted"] is True
    assert _state_json(task)["abandoned"] is True
    assert DevWorkspace(tmp_path / "ws").status(task.id)["abandoned"] is True


# ---------------------------------------------------------------------------
# 5. 接口层（server.py 按契约 v2 C 章使用 persisted / persist_failed）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    monkeypatch.delenv("QIO_DATA_DIR", raising=False)


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_abandon_endpoint_reports_persist_failed_and_keeps_the_entry(client, monkeypatch):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")
    before = client.get("/api/dev/tasks").json()["tasks"][0]

    with monkeypatch.context() as m:
        _fail_replace_for(m, name="state.json")
        resp = client.post(f"/api/dev/tasks/{task.id}/abandon")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "persist_failed"
    assert body["persisted"] is False
    assert body["revoked"] is False
    assert body["invalidated_approvals"] == 0, "放弃没落盘时不许先动作废（不可逆）"
    assert body["task"]["abandoned"] is False, "任务行必须还是「未放弃」"
    assert client.get("/api/dev/tasks").json()["tasks"][0] == before

    # 重试：界面上再点一次就能成功
    retry = client.post(f"/api/dev/tasks/{task.id}/abandon").json()
    assert retry["ok"] is True
    assert retry["persisted"] is True
    assert retry["status"] == "abandoned"
    assert client.get("/api/dev/tasks").json()["tasks"][0]["abandoned"] is True


def test_abandon_endpoint_reports_persisted_on_success(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("x")

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()

    assert body["ok"] is True and body["persisted"] is True
    assert body["status"] == "abandoned"


# ---------------------------------------------------------------------------
# 6. 真实进程强杀：三个中断点
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = """
import os
import sys

src, root, task_id, mode = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, src)

import agent.tools.dev_workspace as dw  # noqa: E402

ws = dw.DevWorkspace(root)

if mode == "before_write":
    # 中断点 1：临时文件还没写
    real_open = open

    def _explode(file, mode="r", *args, **kwargs):
        if "w" in str(mode):
            os._exit(8)
        return real_open(file, mode, *args, **kwargs)

    dw.open = _explode
elif mode == "before_replace":
    # 中断点 2：临时文件写完（已 fsync），还没 replace
    def _explode(src_path, dst_path):
        os._exit(7)

    dw.os.replace = _explode
elif mode == "after_replace":
    # 中断点 3：replace 与回读校验都完成，但还没返回（响应丢失）
    real_strict = dw._write_state_strict

    def _strict_then_die(task):
        real_strict(task)
        os._exit(9)

    dw._write_state_strict = _strict_then_die

ws.abandon(task_id)
os._exit(0)
"""


def _kill_at(tmp_path, task_id: str, mode: str) -> int:
    src = str(Path(dev_workspace.__file__).resolve().parents[3])
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, src, str(tmp_path / "ws"), task_id, mode],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, f"子进程没有在中断点退出：{proc.stdout}{proc.stderr}"
    return proc.returncode


def test_kill_before_writing_the_temp_file_leaves_the_task_active(tmp_path):
    ws = _ws(tmp_path)
    task = ws.create("x")
    _grant(ws, task.id)
    before_state = _state_bytes(task)

    code = _kill_at(tmp_path, task.id, "before_write")

    assert code == 8
    assert _state_bytes(task) == before_state
    assert not (task.dir / "state.json.tmp").exists()
    reborn = DevWorkspace(tmp_path / "ws")
    assert reborn.status(task.id)["abandoned"] is False
    assert reborn.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess")


def test_kill_before_replace_keeps_the_old_state_and_ignores_the_temp(tmp_path):
    """临时文件写完、replace 前被强杀：磁盘是**旧内容**，不是半截。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 1\n")
    ws.record_test(task.id, True, "1/1 tests passed")
    _grant(ws, task.id)
    before_state = _state_bytes(task)
    digest_before = ws.content_digest(task.id)

    code = _kill_at(tmp_path, task.id, "before_replace")

    assert code == 7
    # 正式文件还是旧内容（能解析、没被写坏）
    assert _state_bytes(task) == before_state
    assert _state_json(task)["abandoned"] is False
    # 中断留下的临时文件不会影响任何事实
    assert (task.dir / "state.json.tmp").exists(), "这个中断点本来就会留下临时文件"
    reborn = DevWorkspace(tmp_path / "ws")
    assert reborn.content_digest(task.id) == digest_before
    assert reborn.status(task.id)["evidence_state"] == "current"
    assert reborn.status(task.id)["abandoned"] is False
    assert reborn.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess")


def test_kill_after_replace_leaves_the_task_abandoned(tmp_path):
    """replace 与回读校验都完成、响应前被强杀：重启后任务已经是「已放弃」。"""
    ws = _ws(tmp_path)
    task = ws.create("x")
    _grant(ws, task.id)

    code = _kill_at(tmp_path, task.id, "after_replace")

    assert code == 9
    reborn = DevWorkspace(tmp_path / "ws")
    state = reborn.status(task.id)
    assert state["abandoned"] is True
    assert state["abandoned_at"]
    assert state["phase"] == "abandoned"
    # 授权已经收回，重启后不会复活
    assert state["test_authorized"] is False
    assert reborn.test_authorized(task.id, policy_fingerprint="p1", executor="subprocess") is False
    assert not (task.dir / "state.json.tmp").exists()
