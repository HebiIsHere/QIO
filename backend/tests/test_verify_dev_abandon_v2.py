"""独立验证 v2：放弃终态的**可靠保存**（失败注入 + 三个中断点强杀）。

依据：`_ABANDON-CONTRACT.md`「冻结契约 v2」A 章（可靠保存）与 D1/D2（验收）。
这份文件由**验证方**维护，不覆盖实现方的用例。断言只看契约，不看实现内部结构。

三个失败注入点（D1）：
  1. 临时文件写失败；
  2. `os.replace` 失败；
  3. `os.replace` 成功但**回读校验**对不上。

三个中断点（D2，**真子进程 + os._exit(9)**，不是模拟）：
  1. 写临时文件前；2. 临时文件写完、replace 前；3. replace 后、响应前。

注入器是**自适应**的：实现落地后按契约名字（`_atomic_write_json` / `_write_state_strict` /
`_write_long_term_strict` / `StatePersistError`）注入；实现还没落地时退回到 v1 的直接写
（`Path.write_text(state.json)` / `_write_long_term()`），这样「实现前是红的」这条证据
是**行为性**的（v1 会吞掉错误并报成功），而不是「找不到某个名字」这种空洞的红。
"""

from __future__ import annotations

import importlib
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
from agent.tools.dev_workspace import DevWorkspace

_STATE = "state.json"
_LONG_TERM = "long_term_authorizations.json"
# 崩溃用例的子进程默认用**同一份**源码（当前工作树）。跑「实现前是红的」证据时可以
# 用 VERIFY_AGENT_SRC 指到旧版本的源码副本，父进程与子进程必须一致，否则证据不自洽。
_SRC = Path(os.environ.get("VERIFY_AGENT_SRC") or (Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    monkeypatch.delenv("QIO_DATA_DIR", raising=False)


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    """真实应用（与实现方的用例同一种起法）：HTTP 层的行为要在真接口上看。"""
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 工具：注入器（自适应）+ 观察
# ---------------------------------------------------------------------------


def _module():
    return importlib.import_module("agent.tools.dev_workspace")


def _has_strict_api() -> bool:
    """契约 v2 的严格路径是否已经落地（名字固定，见 A2）。"""
    module = _module()
    return all(
        hasattr(module, name)
        for name in ("_atomic_write_json", "_write_state_strict", "_write_long_term_strict")
    )


def _inject_temp_write_failure(monkeypatch) -> str:
    """临时文件写失败：严格路径在原子写这一步失败；v1 退回「直接写 state.json 失败」。"""
    module = _module()
    if hasattr(module, "_atomic_write_json"):
        def boom(path, payload):  # noqa: ANN001, ARG001
            raise OSError(28, "No space left on device (injected: temp write)")

        monkeypatch.setattr(module, "_atomic_write_json", boom)
        return "module._atomic_write_json（严格路径的原子写）"

    real_write = Path.write_text

    def patched(self, *args, **kwargs):  # noqa: ANN001
        if self.name.startswith(_STATE):
            raise OSError(28, "No space left on device (injected: temp write)")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", patched)
    return "Path.write_text(state.json)（v1 直接写：严格路径未落地）"


def _inject_state_write_failure(monkeypatch) -> str:
    """只让**任务状态**这一步写失败（第 4 步）。

    用来验证 A4 的「部分完成」反馈：第 3 步（长期授权收回）已经落盘，第 4 步失败时
    必须保留第 3 步的结果并如实报 `revoked=True`，同时把任务终态回滚。
    """
    module = _module()
    if hasattr(module, "_atomic_write_json"):
        real = module._atomic_write_json

        def patched(path, payload):  # noqa: ANN001
            if Path(path).name.startswith(_STATE):
                raise OSError(28, "No space left on device (injected: state write)")
            return real(path, payload)

        monkeypatch.setattr(module, "_atomic_write_json", patched)
        return "module._atomic_write_json（只拦 state.json）"

    real_write = Path.write_text

    def patched_write(self, *args, **kwargs):  # noqa: ANN001
        if self.name.startswith(_STATE):
            raise OSError(28, "No space left on device (injected: state write)")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", patched_write)
    return "Path.write_text（只拦 state.json）"


def _inject_replace_failure(monkeypatch) -> str:
    """os.replace 失败：严格路径在原子替换这一步失败。"""
    module = _module()

    def boom(src, dst, *args, **kwargs):  # noqa: ANN001, ARG001
        raise OSError(5, "Access is denied (injected: os.replace)")

    monkeypatch.setattr(module.os, "replace", boom)
    return "os.replace（严格路径的原子替换）"


def _inject_readback_mismatch(monkeypatch, task_id: str) -> str:
    """回读校验对不上：让替换真的成功，但落盘内容与「刚写的那一份」不一致。

    这样无论回读是用 `_read_state()` 还是直接 `json.loads(path.read_text())`，
    都会读到一份「id 不对 / abandoned 不是 True」的内容 —— 严格路径必须据此判失败。
    v1 没有回读这一步，注入是空操作（它会照常报成功 → 用例变红）。
    """
    module = _module()
    real_replace = module.os.replace

    def replace_then_corrupt(src, dst, *args, **kwargs):  # noqa: ANN001
        real_replace(src, dst, *args, **kwargs)
        try:
            Path(dst).write_text(
                json.dumps(
                    {"schema": 3, "source": "qio.dev_workspace", "id": "ws_ffffffffffff",
                     "abandoned": False, "phase": "created"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    monkeypatch.setattr(module.os, "replace", replace_then_corrupt)
    return "os.replace → 替换成功但内容被改成回读对不上（校验这一步）"


def _task(ws: DevWorkspace, request: str = "验证：可靠保存") -> object:
    return ws.create(request)


def _disk_state(task) -> dict:
    return json.loads((task.dir / _STATE).read_text(encoding="utf-8"))


def _disk_abandoned(task) -> bool:
    return bool(_disk_state(task).get("abandoned"))


def _long_term_rows(ws: DevWorkspace) -> list[dict]:
    path = ws.root_dir / _LONG_TERM
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _with_long_term(ws: DevWorkspace, task) -> None:
    ws.grant_test_authorization(
        task.id, policy_fingerprint="fp-verify", executor="subprocess", lifetime="long_term"
    )


# ---------------------------------------------------------------------------
# D1：三种失败注入 —— 接口不报成功、内存与磁盘一致、重试能成功
# ---------------------------------------------------------------------------

_INJECTIONS = [
    ("temp_write", "临时文件写失败", _inject_temp_write_failure),
    ("replace", "os.replace 失败", _inject_replace_failure),
]


@pytest.mark.parametrize("mode,label,injector", _INJECTIONS, ids=["temp-write", "replace"])
def test_persist_failure_never_reports_success(tmp_path, monkeypatch, mode, label, injector):
    """D1：注入失败后 —— 不报成功、内存未放弃、磁盘未放弃、仍可重试。"""
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = _task(ws)
    _with_long_term(ws, task)
    where = injector(monkeypatch)

    result = ws.abandon(task.id)

    assert result.get("ok") is False, f"[{label}] 保存失败却报了成功：{result}（注入点：{where}）"
    assert result.get("status") == "persist_failed", f"[{label}] status={result.get('status')}（注入点：{where}）"
    # 内存与磁盘一致：都没有变成已放弃
    assert ws.status(task.id).get("abandoned") is False, f"[{label}] 内存里已经放弃（磁盘没放弃）"
    assert _disk_abandoned(task) is False, f"[{label}] 磁盘上被写成了已放弃"
    assert ws.abandon_readiness(task.id)["status"] == "ok", f"[{label}] 失败后任务不再可放弃，无法重试"

    monkeypatch.undo()
    retry = ws.abandon(task.id)
    assert retry.get("ok") is True, f"[{label}] 去掉注入后重试仍失败：{retry}"
    assert ws.status(task.id)["abandoned"] is True
    assert _disk_abandoned(task) is True, f"[{label}] 重试成功但磁盘没落盘"
    # 重启后仍然是已放弃（重试确实持久化了）
    assert DevWorkspace(tmp_path / "dev-workspaces").status(task.id)["abandoned"] is True


def test_readback_mismatch_never_reports_success(tmp_path, monkeypatch):
    """D1：回读校验对不上 —— 必须判失败（这是「接口返回成功」的证据）。"""
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = _task(ws)
    where = _inject_readback_mismatch(monkeypatch, task.id)

    result = ws.abandon(task.id)

    assert result.get("ok") is False, f"回读对不上却报了成功：{result}（注入点：{where}）"
    assert result.get("status") == "persist_failed", f"status={result.get('status')}"
    assert ws.status(task.id).get("abandoned") is False, "回读失败后内存停在「已放弃」"
    assert ws.abandon_readiness(task.id)["status"] == "ok", "回读失败后不能重试"


def test_long_term_revocation_is_reported_truthfully_on_partial_failure(tmp_path, monkeypatch):
    """A4 失败路径：第 3 步（长期授权收回）已落盘、第 4 步（任务终态）失败时，
    必须保留已落盘的收回、如实报 revoked=True，并把任务终态回滚成「没放弃」。"""
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = _task(ws)
    _with_long_term(ws, task)
    assert _long_term_rows(ws), "夹具没建立长期授权"

    where = _inject_state_write_failure(monkeypatch)
    result = ws.abandon(task.id)

    assert result.get("ok") is False, f"保存失败却报了成功：{result}（注入点：{where}）"
    assert result.get("status") == "persist_failed"
    assert result.get("persisted") is False
    # 第 3 步已经落盘：内存与磁盘必须一致地「已收回」
    assert _long_term_rows(ws) == [], f"长期授权没被收回，或只改了内存没落盘（注入点：{where}）"
    assert result.get("revoked") is True, f"长期授权已收回，revoked 却是 {result.get('revoked')!r}"
    assert ws.status(task.id)["abandoned"] is False, "任务终态没有回滚"
    assert _disk_abandoned(task) is False, "磁盘上留下了已放弃标记"
    message = str(result.get("message") or "")
    assert "授权" in message and "重试" in message, f"文案没如实说明部分完成与可重试：{message!r}"

    # 重试收敛：解除注入后应当成功，且终态与授权都到位
    monkeypatch.undo()
    retry = ws.abandon(task.id)
    assert retry.get("ok") is True, f"重试没有成功：{retry}"
    assert _disk_abandoned(task) is True and _long_term_rows(ws) == []


def test_already_abandoned_retry_finishes_the_unrevoked_authorization(tmp_path, monkeypatch):
    """A5：幂等重试要把上次没收回的授权收干净（成功 → ok=True/revoked=True）。"""
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = _task(ws)
    _with_long_term(ws, task)
    assert _long_term_rows(ws), "夹具没建立长期授权"

    # 造出「内存与磁盘都已放弃、长期授权还在」的中间态：模拟上次中断在收回之前。
    task.abandoned = True
    task.abandoned_at = "2026-10-04T00:00:00+00:00"
    task.phase = "abandoned"
    _module()._write_state(task)  # A1：尽力而为的写入器仍然保留

    retry = ws.abandon(task.id)
    assert retry.get("status") == "already_abandoned", f"重试路径不对：{retry}"
    assert retry.get("ok") is True, f"幂等重试应当成功：{retry}"
    assert retry.get("revoked") is True, "幂等重试没有把长期授权收干净"
    assert _long_term_rows(ws) == [], "长期授权文件里还留着记录"


def test_already_abandoned_retry_reports_failure_when_revocation_cannot_persist(tmp_path, monkeypatch):
    """A5 后半：授权还是收不回来 → ok=False/status=persist_failed，并说清「任务已放弃但授权没收回」。"""
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = _task(ws)
    _with_long_term(ws, task)
    task.abandoned = True
    task.abandoned_at = "2026-10-04T00:00:00+00:00"
    task.phase = "abandoned"
    _module()._write_state(task)

    _inject_temp_write_failure(monkeypatch)
    retry = ws.abandon(task.id)

    # 契约 A5：再次失败时 status 就是 persist_failed（不是 already_abandoned），
    # 但原因必须说清「任务已经放弃、授权还没收回」——否则用户会以为任务又活过来了。
    assert retry.get("status") == "persist_failed", f"契约 A5 要求 status=persist_failed：{retry}"
    assert retry.get("ok") is False, f"授权没收回却报了成功：{retry}"
    assert retry.get("persisted") is False, f"persisted 应当是 False：{retry}"
    assert retry.get("revoked") is False, f"授权没落盘，revoked 应当是 False：{retry}"
    message = str(retry.get("message") or "")
    assert "放弃" in message, f"文案没说明任务已经放弃：{message!r}"
    assert "授权" in message, f"文案没说明授权没收回：{message!r}"


# ---------------------------------------------------------------------------
# D1 + C4：HTTP 层 —— 失败时一个审批都不许作废，任务行原样
# ---------------------------------------------------------------------------


def test_http_persist_failure_reports_failure_and_touches_no_approval(client, monkeypatch):
    """D1/C4：接口返回 ok=false / persist_failed / invalidated_approvals=0，任务行仍是未放弃。"""
    ctx = client.app.state.ctx
    ws = ctx.dev_workspaces
    task = ws.create("验证：HTTP 保存失败")

    # 一条真实等待中的审批（用来证明「失败这一支不许作废任何审批」）
    import asyncio

    async def _hold():
        return await ctx.approvals.request("tool_execution", {"workspace": task.id})

    loop = asyncio.new_event_loop()
    holder = loop.create_task(_hold())
    for _ in range(200):
        if ctx.approvals.pending():
            break
        loop.run_until_complete(asyncio.sleep(0.02))
    assert ctx.approvals.pending(), "审批没有进入等待表"

    _inject_temp_write_failure(monkeypatch)
    resp = client.post(f"/api/dev/tasks/{task.id}/abandon")
    body = resp.json()

    assert body["ok"] is False, f"保存失败却报成功：{body}"
    assert body["status"] == "persist_failed", f"status={body['status']}"
    assert body["persisted"] is False, f"persisted={body['persisted']}"
    assert body["invalidated_approvals"] == 0, "失败这一支作废了审批（作废不可逆）"
    assert ctx.approvals.pending(), "失败这一支把未决审批作废掉了"
    assert body["task"]["abandoned"] is False, "返回的任务行被标成已放弃"

    listed = client.get("/api/dev/tasks").json()["tasks"]
    row = [item for item in listed if item["id"] == task.id]
    assert row and row[0]["abandoned"] is False, "列表里任务被错误地标成已放弃（界面会错误移除条目）"

    # 重试：解除注入后才应当成功，并且**这时才**作废那条审批
    monkeypatch.undo()
    retry = client.post(f"/api/dev/tasks/{task.id}/abandon").json()
    assert retry["ok"] is True and retry["persisted"] is True, f"重试没有成功：{retry}"
    assert retry["invalidated_approvals"] == 1, f"成功后应作废那条未决审批：{retry}"

    loop.run_until_complete(asyncio.wait_for(holder, timeout=10))
    loop.close()


# ---------------------------------------------------------------------------
# D2：三个中断点强杀（真子进程 + os._exit(9)）
# ---------------------------------------------------------------------------

_CHILD = r'''
import importlib, json, os, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
root, task_id, point = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
STATE = "state.json"
module = importlib.import_module("agent.tools.dev_workspace")
ws = module.DevWorkspace(root)


def _is_state(path) -> bool:
    return Path(path).name.startswith(STATE)


if point == "before_temp":
    # 只在「写 state.json 的临时文件」之前强杀；长期授权那一步（第 3 步）已经正常完成。
    atomic = getattr(module, "_atomic_write_json", None)
    if atomic is None:
        real_write = Path.write_text
        def patched_write(self, *a, **k):
            if _is_state(self):
                os._exit(9)
            return real_write(self, *a, **k)
        Path.write_text = patched_write
    else:
        def patched_atomic(path, payload):
            if _is_state(path):
                os._exit(9)
            return atomic(path, payload)
        module._atomic_write_json = patched_atomic
elif point == "before_replace":
    real_replace = module.os.replace
    def patched_before(src, dst, *a, **k):
        if Path(dst).name == STATE:
            os._exit(9)
        return real_replace(src, dst, *a, **k)
    module.os.replace = patched_before
elif point == "after_replace":
    real_replace = module.os.replace
    def patched_after(src, dst, *a, **k):
        real_replace(src, dst, *a, **k)
        if Path(dst).name == STATE:
            os._exit(9)
    module.os.replace = patched_after
else:
    raise SystemExit("unknown point: " + point)

result = ws.abandon(task_id)
print(json.dumps(result, ensure_ascii=False))
os._exit(0)
'''


def _crash_abandon(root: Path, task_id: str, point: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _CHILD, str(_SRC), str(root), task_id, point],
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("point", ["before_temp", "before_replace"])
def test_crash_before_the_state_lands_leaves_the_task_unabandoned(tmp_path, point):
    """D2 前两个中断点：重启后仍是未放弃，磁盘没有半截文件，也没有残留临时文件。

    覆盖位置说明：这两个点都在**第 4 步（任务终态落盘）**内部；第 3 步（长期授权收回）
    已经正常完成 —— 所以这里同时断言「长期授权已收回、任务未放弃」这一**部分完成**状态，
    它正是契约 A4 失败路径允许并要求如实反馈的形态。
    """
    root = tmp_path / "dev-workspaces"
    ws = DevWorkspace(root)
    task = _task(ws)
    _with_long_term(ws, task)
    before = (task.dir / _STATE).read_text(encoding="utf-8")

    proc = _crash_abandon(root, task.id, point)
    assert proc.returncode == 9, (
        f"[{point}] 子进程没有在中断点被强杀（rc={proc.returncode}）——"
        f"说明这个中断点在当前实现里不存在：stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r}"
    )

    reopened = DevWorkspace(root)
    assert reopened.status(task.id)["abandoned"] is False, f"[{point}] 重启后任务复活成了已放弃"
    assert (task.dir / _STATE).exists(), f"[{point}] state.json 不见了"
    payload = json.loads((task.dir / _STATE).read_text(encoding="utf-8"))
    assert payload.get("id") == task.id, f"[{point}] 落盘内容不是完整的一份（半截文件）"
    # 中断点在「任务终态」之前：磁盘上那一份必须还是中断前的内容
    assert payload.get("abandoned") is False, f"[{point}] 磁盘上出现了不该有的已放弃标记"
    assert (task.dir / _STATE).read_text(encoding="utf-8") == before, f"[{point}] 中断点之前磁盘就被改了"
    # 第 3 步已经完成：长期授权已收回（部分完成，如实）
    assert _long_term_rows(reopened) == [], f"[{point}] 第 3 步应当已经完成，长期授权却没收回"
    # 残留临时文件不许出现在工作区文件视图里
    assert not [name for name in reopened.list_files(task.id) if name.startswith(_STATE) and name != _STATE], (
        f"[{point}] 残留临时文件出现在文件视图：{reopened.list_files(task.id)}"
    )
    # 重启后仍能放弃（可恢复，不是死局）
    retry = reopened.abandon(task.id)
    assert retry.get("ok") is True and reopened.status(task.id)["abandoned"] is True, (
        f"[{point}] 重启后放弃失败：{retry}"
    )


def test_crash_after_replace_keeps_the_abandoned_state(tmp_path):
    """D2 第三个中断点：state.json 的 replace 之后被强杀 → 重启后已放弃、授权已收回。"""
    root = tmp_path / "dev-workspaces"
    ws = DevWorkspace(root)
    task = _task(ws)
    _with_long_term(ws, task)

    proc = _crash_abandon(root, task.id, "after_replace")
    assert proc.returncode == 9, (
        f"子进程没有在 replace 之后被强杀（rc={proc.returncode}）——"
        f"说明这个中断点在当前实现里不存在：stdout={proc.stdout!r} stderr={proc.stderr[-400:]!r}"
    )

    reopened = DevWorkspace(root)
    assert reopened.status(task.id)["abandoned"] is True, "replace 已经完成，重启后却不是已放弃"
    assert reopened.status(task.id)["phase"] == "abandoned"
    assert _long_term_rows(reopened) == [], "已放弃任务的长期授权没有收回"
    assert not [name for name in reopened.list_files(task.id) if name.startswith(_STATE) and name != _STATE]
    # 「保存成功但响应丢失」的重复请求必须是幂等成功（A5）
    again = reopened.abandon(task.id)
    assert again.get("status") == "already_abandoned" and again.get("ok") is True, f"重复请求不幂等：{again}"


def test_strict_path_uses_same_directory_temp_file_and_readback(tmp_path, monkeypatch):
    """A3：临时文件必须在同一个目录（同卷 replace），且 replace 之后要有回读校验。

    这条只看契约点名的可观察行为：临时文件名以 state.json 开头、位于任务目录内。
    """
    module = _module()
    if not hasattr(module, "_atomic_write_json"):
        pytest.fail(
            "严格持久化还没落地：缺少 module._atomic_write_json（契约 v2 A2）——"
            "本用例在实现前必然红，这正是它要证明的判别力"
        )
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = _task(ws)
    seen: list[tuple[str, str]] = []
    real_replace = module.os.replace

    def spy(src, dst, *args, **kwargs):  # noqa: ANN001
        seen.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", spy)
    ws.abandon(task.id)

    assert seen, "严格路径没有调用 os.replace（不是原子替换）"
    src, dst = seen[-1]
    assert Path(src).parent == Path(dst).parent, f"临时文件不在同一目录：{src} → {dst}"
    assert Path(src).name.startswith(_STATE), f"临时文件命名不符合契约：{Path(src).name}"
    assert Path(dst).name == _STATE
