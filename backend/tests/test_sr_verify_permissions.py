"""契约 6 权限组反例（test_sr_verify_permissions）。

对应冻结契约 2026-10-09 的第 1 组：临时真实 git 仓库 + RunProgramTool
真实实例（approvals 桩直接拒绝并计数）。测试断言的是**修复后的期望**：
在未修复基线上应当红（这正是反例证据），Lead 集成后在集成 worktree
重跑同组测试做前后对照。

覆盖：
* 1a  default 模式 git tag 创建零审批执行 → 期望非 auto + repo 零变化
* 1b  工作区外 .env：fs_read 拒绝后 run_program cat 旁路 → 期望非 auto + 内容不泄漏
* 1c  plan 模式写命令（git tag 创建）零审批执行 → 期望非 auto；自由 shell 写命令保持 deny

证据文件：scripts/sr-verify/output/<label>.json（不含敏感原文）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parents[2] / "scripts" / "sr-verify"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import probes  # noqa: E402  (scripts/sr-verify/probes.py)


async def test_sr_verify_git_tag_create_default_mode():
    """1a：default 模式下 git tag 创建不得 auto；拒绝后 repo 零变化。"""
    ev = await probes.probe_git_tag_write(mode="default", label="permission_git_tag_default")
    assert ev.get("git_available"), "本机没有 git，无法做真实反例（环境问题）"
    assert ev["verdict_not_auto"], (
        "反例：git tag <名>（创建）实际 verdict=%s（risk=%s），期望非 auto —— 零审批的写操作"
        % (ev["verdict"], ev["risk"])
    )
    assert not ev["tag_created"], "repo 里真的多出了 tag：未修复基线上的真实副作用"


async def test_sr_verify_git_tag_create_plan_mode():
    """1c：plan 模式下写形态 git tag 必须 deny（零审批写命令不允许）。"""
    ev = await probes.probe_git_tag_write(mode="plan", label="permission_git_tag_plan")
    assert ev.get("git_available"), "本机没有 git，无法做真实反例（环境问题）"
    assert ev["verdict_not_auto"], (
        "反例：plan 模式下 git tag <名> 实际 verdict=%s，期望 deny —— plan 模式不允许零审批写命令"
        % ev["verdict"]
    )
    assert not ev["tag_created"], "plan 模式下 repo 出现了新 tag：写操作被真实执行"


async def test_sr_verify_out_of_root_env_no_command_bypass():
    """1b：fs_read 拒绝的工作区外 .env 不得被 run_program cat 旁路。"""
    ev = await probes.probe_env_read_bypass(label="permission_env_bypass")
    assert ev["fs_read_denied"], "FsReadTool 未拒绝工作区外 .env：" + ev["fs_read_error"][:80]
    assert ev["run_verdict_not_auto"], (
        "反例：FsReadTool 已拒绝的 .env 路径，run_program cat 实际 verdict="
        + ev["run_verdict"]
        + "（期望非 auto）—— 文件路径操作数一致性缺失"
    )
    assert not ev["run_marker_leaked"], (
        "反例：run_program 读出了 FsReadTool 已拒绝的 .env 内容（执行了=%s，系统有 cat=%s）"
        % (ev["run_executed"], ev["cat_available"])
    )
    assert ev["inside_file_auto_kept"], "根内普通文件 cat 的 auto 只读体验被误伤（期望保持 auto）"


async def test_sr_verify_plan_mode_shell_write_denied():
    """1c 护栏：plan 模式自由 shell 写命令保持 deny（既有正确行为，防回归）。"""
    ev = await probes.probe_plan_mode_shell_write_denied(label="permission_plan_shell_write")
    assert not ev["tool_ok"], "plan 模式下 run_shell 写命令被执行了（应当 deny）"
    assert ev["verdict"] == "deny", "plan 模式 run_shell verdict=" + ev["verdict"] + "，期望 deny"
