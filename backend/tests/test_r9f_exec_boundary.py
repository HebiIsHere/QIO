"""R9-F W2：统一受控启动入口 / 执行边界（契约 §3.3、问题 A）。

覆盖：
* spec_digest 事前核对：程序/argv/cwd/脚本内容变化 → 许可失效，不启动；
* 只读模式不能靠许可绕过；无 execute 授权的 permit 不执行；
* 内部运行数据未经三次确认 → launcher 拒绝；
* 网络白名单无法强制 → 明确「不可用」（不静默降级）；
* 执行前后 guard 事后核验：授权写范围外的修改 → boundary_violation；
  授权写范围内的修改 → authorized_changes（并如实标注 post_checked_only）；
* Windows 真机：enable_write_scope=True 时 Low IL 阻止范围外写入、范围内写入可用；
* 输出上限 / 超时收尾 / 取消语义不回退。
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from agent.security.launch import (
    ControlledLauncher,
    LaunchSpec,
    build_permit,
    compute_spec_digest,
)
from agent.tools import isolation

PY = sys.executable
WINDOWS = sys.platform == "win32"
WINDOWS_ONLY = pytest.mark.skipif(not WINDOWS, reason="Windows 内核能力（Job Object / MIC）")


def _program_spec(code: str, **kwargs) -> LaunchSpec:
    return LaunchSpec(kind="program", program=PY, argv=("-c", code), **kwargs)


def _script_spec(code: str, **kwargs) -> LaunchSpec:
    return LaunchSpec(
        kind="script", program=PY, script_source=code, script_name="r9f_check.py", **kwargs
    )


async def _no_spawn(*args, **kwargs):
    raise AssertionError("这次启动必须在创建进程之前被拒绝")


# --------------------------------------------------------------------------
# 事前约束：spec 与 permit 不一致 → 不启动
# --------------------------------------------------------------------------


async def test_program_launch_runs_and_reports_capability_boundaries():
    spec = _program_spec("print('R9F_EXEC_OK')")
    permit = build_permit(spec, mode="unrestricted")

    result = await ControlledLauncher().launch(spec, permit)

    assert result.ok is True, result.error
    assert result.returncode == 0
    assert "R9F_EXEC_OK" in result.stdout
    assert result.spec_digest == permit.spec_digest
    boundaries = (result.isolation or {}).get("capability_boundaries") or []
    assert any("network_not_isolated" in item for item in boundaries)
    assert any("read_not_isolated" in item for item in boundaries)


async def test_launch_rejects_permit_whose_digest_is_for_another_spec(monkeypatch):
    spec = _program_spec("print('A')")
    permit = build_permit(spec, mode="unrestricted")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    mutations = [
        _program_spec("print('B')"),                       # argv 内容变了
        LaunchSpec(kind="program", program=PY, argv=("-c", "print('A')"), cwd=os.getcwd()),  # cwd 变了
        _program_spec("print('A')", script_source=None),   # 等价但 kind 相同，digest 不变（对照）
    ]
    for mutated in mutations[:2]:
        result = await ControlledLauncher().launch(mutated, permit)
        assert result.ok is False
        assert result.category == "permit_mismatch", result.category
        assert "许可失效" in (result.error or "")
        assert "spec_digest" in (result.error or "")
    # 对照：原 spec 未被篡改时 digest 必然一致（这里不启动，只核对摘要）
    assert compute_spec_digest(spec) == permit.spec_digest


async def test_script_content_change_invalidates_permit(monkeypatch):
    spec = _script_spec("print('SAFE')")
    permit = build_permit(spec, mode="unrestricted")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    changed = _script_spec("print('EVIL')")
    result = await ControlledLauncher().launch(changed, permit)

    assert result.ok is False
    assert result.category == "permit_mismatch"
    assert "脚本" in (result.error or "") or "spec_digest" in (result.error or "")


async def test_program_identity_change_invalidates_permit(monkeypatch):
    """同一路径的程序内容变化（换程序）也必须让许可失效。"""
    import tempfile
    from pathlib import Path

    holder = Path(tempfile.mkdtemp(prefix="r9f-prog-"))
    binary = holder / ("prog.exe" if WINDOWS else "prog")
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    spec = LaunchSpec(kind="program", program=str(binary))
    permit = build_permit(spec, mode="unrestricted")
    # 审批之后程序文件被替换（同路径、新内容）
    binary.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    result = await ControlledLauncher().launch(spec, permit)

    assert result.ok is False
    assert result.category == "permit_mismatch"
    assert "许可失效" in (result.error or "")


async def test_permit_without_execute_operation_is_refused(monkeypatch):
    spec = _program_spec("print('never')")
    permit = build_permit(
        spec, mode="restricted", allowed_operations=("write",), allowed_roots=()
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    result = await ControlledLauncher().launch(spec, permit)

    assert result.ok is False
    assert result.category == "permission"
    assert "execute" in (result.error or "")


async def test_unauthorized_permit_is_refused(monkeypatch):
    spec = _program_spec("print('never')")
    good = build_permit(spec, mode="unrestricted")
    from dataclasses import replace

    permit = replace(good, authorized=False, reason="测试：未授权")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    result = await ControlledLauncher().launch(spec, permit)

    assert result.ok is False
    assert result.category == "permission"
    assert "没有获得授权" in (result.error or "")


async def test_read_only_mode_requires_readonly_marker(monkeypatch):
    spec = _program_spec("print('never')")
    # 普通 execute 标记在只读模式下不能启动
    permit = build_permit(spec, mode="read_only", allowed_operations=("execute",))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    result = await ControlledLauncher().launch(spec, permit)

    assert result.ok is False
    assert result.category == "permission"
    assert "只读" in (result.error or "")


async def test_mode_switch_to_read_only_invalidates_previous_permit():
    spec = _program_spec("print('never')")
    permit = build_permit(spec, mode="restricted", allowed_roots=())
    launcher = ControlledLauncher(mode_provider=lambda: "read_only")

    result = await launcher.launch(spec, permit)

    assert result.ok is False
    assert result.category == "permission"
    assert "只读" in (result.error or "")
    assert "许可" in (result.error or "") or "审批" in (result.error or "")


async def test_internal_data_without_three_confirmations_is_refused(tmp_path, monkeypatch):
    data_dir = tmp_path / "qio-data"
    data_dir.mkdir()
    target = data_dir / "qio.db"
    target.write_text("x", encoding="utf-8")
    launcher = ControlledLauncher(internal_data_paths=lambda: [data_dir])
    spec = _program_spec("print(r'" + str(target) + "')")
    permit = build_permit(spec, mode="unrestricted")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    refused = await launcher.launch(spec, permit)

    assert refused.ok is False
    assert refused.category == "permission"
    assert "三次确认" in (refused.error or "")
    assert "qio-data" in (refused.error or "")


async def test_internal_data_with_confirmation_executes(tmp_path):
    data_dir = tmp_path / "qio-data"
    data_dir.mkdir()
    target = data_dir / "qio.db"
    target.write_text("x", encoding="utf-8")
    launcher = ControlledLauncher(internal_data_paths=lambda: [data_dir])
    spec = _program_spec("print(r'" + str(target) + "')")
    permit = build_permit(spec, mode="unrestricted", internal_data_confirmed=True)

    result = await launcher.launch(spec, permit)

    assert result.ok is True, result.error
    assert "qio.db" in result.stdout


async def test_declared_network_hosts_is_explicitly_unavailable(monkeypatch):
    spec = _program_spec("print('never')")
    permit = build_permit(
        spec, mode="unrestricted", network_hosts=("example.com",)
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_spawn)

    result = await ControlledLauncher().launch(spec, permit)

    assert result.ok is False
    assert result.category == "unsupported"
    assert "网络" in (result.error or "")
    assert "不可用" in (result.error or "") or "无法" in (result.error or "")


# --------------------------------------------------------------------------
# 事后核验（补充审计）：授权写范围之外/之内的修改
# --------------------------------------------------------------------------


async def test_guard_detects_write_outside_allowed_roots(tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    victim = protected / "business.txt"
    victim.write_text("original", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    launcher = ControlledLauncher(
        protected_scope_provider=lambda: [protected],
        mode_provider=lambda: "restricted",
    )
    spec = _script_spec(
        "from pathlib import Path\n"
        "Path(r'" + str(victim) + "').write_text('tampered', encoding='utf-8')\n"
        "print('done')\n"
    )
    permit = build_permit(
        spec, mode="restricted", allowed_roots=(str(allowed),)
    )

    result = await launcher.launch(spec, permit)

    assert result.ok is False
    assert result.category == "boundary_violation", result.error
    assert "执行越界" in (result.error or "")
    guard = result.guard or {}
    assert guard.get("boundary") == "post_checked_only"
    assert any(str(victim) in str(item.get("path")) for item in guard["violations"])
    assert victim.read_text(encoding="utf-8") == "tampered", "事后核验不能回滚，只能如实上报"


async def test_guard_classifies_writes_inside_allowed_roots(tmp_path):
    protected = tmp_path / "workspace"
    protected.mkdir()
    allowed = protected / "build"
    allowed.mkdir()
    launcher = ControlledLauncher(
        protected_scope_provider=lambda: [protected],
        mode_provider=lambda: "restricted",
    )
    spec = _script_spec(
        "from pathlib import Path\n"
        "Path(r'" + str(allowed / "out.txt") + "').write_text('ok', encoding='utf-8')\n"
        "print('done')\n"
    )
    permit = build_permit(spec, mode="restricted", allowed_roots=(str(allowed),))

    result = await launcher.launch(spec, permit)

    assert result.ok is True, result.error
    guard = result.guard or {}
    assert guard.get("verified") is True
    assert guard.get("violations") == []
    assert any(str(allowed) in str(item.get("path")) for item in guard["authorized_changes"])


async def test_guard_classifies_qio_own_runtime_writes_with_evidence(tmp_path):
    """QIO 自身写库/日志：按启动前快照里的确切条目识别并留证据，不整类排除。"""
    protected = tmp_path / "data"
    protected.mkdir()
    db = protected / "qio.db"
    db.write_text("db-before", encoding="utf-8")

    launcher = ControlledLauncher(
        protected_scope_provider=lambda: [protected],
        internal_data_paths=lambda: [protected],
        mode_provider=lambda: "restricted",
    )
    spec = _script_spec(
        "from pathlib import Path\n"
        "Path(r'" + str(db) + "').write_text('db-after', encoding='utf-8')\n"
        "print('done')\n"
    )
    # 内部数据写入需要三次确认（permit 已确认）才能执行；这里验证「事后核验」如何分类
    permit = build_permit(
        spec, mode="restricted", allowed_roots=(), internal_data_confirmed=True
    )

    result = await launcher.launch(spec, permit)

    guard = result.guard or {}
    # qio.db 是启动前快照里的确切条目：归为自身写候选（有证据），不静默放过也不误判越界
    assert guard.get("violations") == []
    assert any(str(db) in str(item.get("path")) for item in guard["self_writes"])
    evidence = guard["self_writes"][0]["evidence"]
    assert evidence.get("evidence") == "pre_execution_snapshot_of_qio_runtime_file"


# --------------------------------------------------------------------------
# 超时 / 取消 / 输出上限（收尾语义不回退）
# --------------------------------------------------------------------------


async def test_timeout_terminates_tree_and_reports_termination():
    spec = LaunchSpec(
        kind="program", program=PY, argv=("-c", "import time; time.sleep(30)"), timeout=0.8
    )
    permit = build_permit(spec, mode="unrestricted")

    result = await ControlledLauncher().launch(spec, permit)

    assert result.ok is False
    assert result.category == "timeout"
    assert "超时" in (result.error or "")
    assert "已终止" in (result.error or "")
    assert "清理未确认" not in (result.error or "")


async def test_cancel_propagates_after_cleanup():
    spec = LaunchSpec(
        kind="program", program=PY, argv=("-c", "import time; time.sleep(30)"), timeout=30
    )
    permit = build_permit(spec, mode="unrestricted")
    task = asyncio.ensure_future(ControlledLauncher().launch(spec, permit))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_output_limit_terminates_and_reports():
    code = "print('x' * 200000)"
    spec = _program_spec(code)
    permit = build_permit(spec, mode="unrestricted")

    result = await ControlledLauncher(max_stdout_bytes=4096).launch(spec, permit)

    assert result.ok is False
    assert result.category == "output_limit"
    assert "输出超过上限" in (result.error or "")


# --------------------------------------------------------------------------
# Windows 真机：Low IL 事前写范围（enforce_write_scope=True）
# --------------------------------------------------------------------------


@WINDOWS_ONLY
async def test_enforced_write_scope_allows_inside_and_blocks_outside(tmp_path):
    """范围内必要写入可用；范围外写入被 Low IL 内核拒绝（真机证据）。"""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    victim = protected / "medium.txt"
    victim.write_text("original", encoding="utf-8")

    launcher = ControlledLauncher(mode_provider=lambda: "restricted", guard_enabled=False)
    code = (
        "import os\n"
        "print('CHILD_INTEGRITY', __import__('subprocess').run("
        "['whoami','/groups'], capture_output=True, text=True).stdout[:0])\n"
        "try:\n"
        "    open(r'" + str(victim) + "', 'w', encoding='utf-8').write('hacked')\n"
        "    print('OUTSIDE_WRITE_OK')\n"
        "except PermissionError as exc:\n"
        "    print('OUTSIDE_WRITE_DENIED', type(exc).__name__)\n"
        "open(r'" + str(allowed / "inside.txt") + "', 'w', encoding='utf-8').write('ok')\n"
        "print('INSIDE_WRITE_OK')\n"
    )
    spec = _script_spec(code, timeout=60)
    permit = build_permit(
        spec, mode="restricted", allowed_roots=(str(allowed),), allowed_operations=("execute",)
    )

    result = await launcher.launch(spec, permit, enforce_write_scope=True)

    assert result.ok is True, (result.error, result.stderr)
    assert "OUTSIDE_WRITE_DENIED" in result.stdout, result.stdout
    assert "INSIDE_WRITE_OK" in result.stdout
    assert victim.read_text(encoding="utf-8") == "original", "范围外写入必须被内核拒绝"
    assert (allowed / "inside.txt").read_text(encoding="utf-8") == "ok", "范围内写入必须可用"
    info = result.isolation or {}
    assert "low_integrity" in info.get("mechanisms", []), info
    assert info.get("write_scope", {}).get("pre_enforced") is True
    assert isolation.integrity_of_process(os.getpid())  # 父进程完整性可读（诊断）


@WINDOWS_ONLY
async def test_enforced_write_scope_blocks_delete_of_outside_file(tmp_path):
    """解释器内部删除不能绕过写边界：Low 进程删除 Medium 文件被内核拒绝。"""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    victim = protected / "keep.txt"
    victim.write_text("keep", encoding="utf-8")
    launcher = ControlledLauncher(mode_provider=lambda: "restricted", guard_enabled=False)
    code = (
        "import os\n"
        "try:\n"
        "    os.remove(r'" + str(victim) + "')\n"
        "    print('DELETE_OK')\n"
        "except PermissionError as exc:\n"
        "    print('DELETE_DENIED', type(exc).__name__)\n"
    )
    spec = _script_spec(code)
    permit = build_permit(
        spec, mode="restricted", allowed_roots=(str(allowed),), allowed_operations=("execute",)
    )

    result = await launcher.launch(spec, permit, enforce_write_scope=True)

    assert result.ok is True, (result.error, result.stderr)
    assert "DELETE_DENIED" in result.stdout
    assert victim.exists() and victim.read_text(encoding="utf-8") == "keep"


def test_script_file_content_check_detects_replacement(tmp_path):
    from agent.security.launch import ControlledLauncher as Launcher

    spec = _script_spec("print('A')")
    path = tmp_path / "r9f_check.py"
    path.write_text("print('B')", encoding="utf-8")
    checked = Launcher()._check_script_file(path, spec)
    assert checked["checked"] is True
    assert checked["changed"] is True
