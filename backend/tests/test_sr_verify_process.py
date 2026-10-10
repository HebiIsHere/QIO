"""契约 6 进程组反例（test_sr_verify_process）。

对应冻结契约 2026-10-09 的第 2 组：受控真实 python 子进程（写 PID 文件 +
sleep + 孙进程；Windows 真机 tasklist / taskkill 取证）。

反例（未修复基线）：
* 2a  git show 不存在的引用，退出码非 0 却返回 ok=True
* 2b  工具超时/取消只放弃等待：本次启动的进程树继续存活（tasklist 证实）

判定层（permission verdicts）在权限组覆盖；这里用批准桩让真实子进程走完
完整的执行/超时/取消路径。诱饵进程（同程序名的其它实例）必须毫发无损；
收尾清理只杀探针自己记录到的 pid。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parents[2] / "scripts" / "sr-verify"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import probes  # noqa: E402  (scripts/sr-verify/probes.py)


async def test_sr_verify_git_show_missing_ref_reports_failure():
    """2a：非零退出码必须是失败（旧反例：git show 坏引用返回 ok=True）。"""
    ev = await probes.probe_git_show_missing_ref(label="process_git_show_missing_ref")
    assert ev.get("git_available"), "本机没有 git，无法做真实反例（环境问题）"
    assert not ev["ok"], (
        "反例：git show 不存在的引用返回 ok=True（合并输出可见 stderr="
        + str(ev["content_has_stderr_hint"])
        + "）—— 退出码语义缺失"
    )
    assert ev["error_mentions_exit_code"], "error 未包含退出码语义：" + ev["error"][:120]


async def test_sr_verify_timeout_kills_process_tree_and_spares_decoy():
    """2b：超时返回后进程树（子+孙）退出；同名诱饵进程存活。"""
    ev = await probes.probe_timeout_tree(label="process_timeout_tree")
    assert ev["tool_failed"], "超时的执行应当报失败：ok=" + str(ev["tool_ok"])
    assert ev["child_pid_started"], "受控子进程未启动（环境问题，不能作数）"
    # 直接子进程在工具返回的瞬间就必须已退出：cmd_tools 在超时清理里 await 了
    # proc.wait()（确定性证据，与机器负载无关）。
    assert not ev["child_alive_immediately"], (
        "反例：工具超时返回后本次启动的子进程仍存活（tasklist pid 取证）"
    )
    # 整树（孙进程经 taskkill /T 收割）用宽窗口断言：固定 1 秒墙钟是负载敏感
    # 的次要证据（taskkill 自身在并行负载下可花 >1s）；核心事实是「反例基线上
    # sleep 300 的树永远活着，修复后必定在宽窗口内收割」。
    assert ev["child_gone_within_1s"] or ev["child_gone_within_5s"], (
        "超时返回后 %ss 内子/孙进程仍未全部退出（tasklist 取证）" % ev.get("probe_wait_seconds_wide", 5.0)
    )
    assert ev["grandchild_gone"], "孙进程仍在（进程树未级联终止）"
    assert ev["decoy_alive"], "同名诱饵进程被误杀 —— 只允许清理本次启动的 pid"


async def test_sr_verify_cancel_kills_process_tree_and_propagates():
    """2b（取消）：取消 → 进程树退出 + CancelledError 重新抛出。"""
    ev = await probes.probe_cancel_tree(label="process_cancel_tree")
    assert ev["child_pid_started"], "受控子进程未启动（环境问题，不能作数）"
    assert ev["cancelled_error"], "取消后未重新抛出 CancelledError（loop 依赖它判定取消终态）"
    assert ev["child_gone_after_cancel"], "反例：取消后子进程仍存活（未在自己的 finally 里杀树）"
    assert ev["grandchild_gone_after_cancel"], "取消后孙进程仍在"
    assert ev["decoy_alive"], "同名诱饵进程被误杀（只允许清理本次启动的 pid）"
