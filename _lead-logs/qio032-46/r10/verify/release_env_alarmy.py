# -*- coding: utf-8 -*-
"""QIO #46 r10 Task1 验收：**正式构建（release/安装版）固定产品锁名**的 env 过敏检查。

背景：r9 的单实例实现里，QIO_INSTANCE_PROBE / QIO_INSTANCE_PROBE_SUFFIX 无条件生效，
因此 release qio.exe 一旦继承了测试环境变量（CI、真机脚本、安装器残留、父进程 env），
就会「提前走探针打印 JSON 后退出」或「按另一个后缀自成一套隔离实例」——正式构建必须
始终使用同一产品锁名。r10 用 cfg!(debug_assertions) 把两个 env 限制在测试构建内。

用法（Lead 在 release 构建 + 安装/暂存目录上跑）：
    python D:/qio-dev/q46-r9/_lead-logs/qio032-46/r10/verify/release_env_alarmy.py \
        --exe "D:/qio-dev/_win-r10/install-u/qio.exe" \
        --work "D:/qio-dev/_win-r10/env-alarmy"

前提：--exe 所在目录必须是可运行的安装/暂存目录（同目录有 qio-backend.exe 等 sidecar，
前端产物已内嵌）；qio.exe 必须是 release 构建（debug 构建按设计仍认这些 env，属预期）。

断言全部来自 OS 事实（进程树、主窗口 HWND、sidecar.lease、后端监听端口）+ 进程退出码，
不以日志文案作为通过依据；辅助函数复用 r9 的 win_instance_accept.py。

场景：
  R1  第一个进程带 QIO_INSTANCE_PROBE=hold + QIO_INSTANCE_PROBE_SUFFIX=inherited-a 启动：
      必须按正式实例启动 —— 进程存活、写出本进程 lease、主窗口可见、后端已监听；
      不允许「走探针后退出」，也不允许「按 inherited-a 自成隔离实例」。
  R1b 同会话再启动一个带 QIO_INSTANCE_PROBE_SUFFIX=inherited-b（+ PROBE=hold）的进程：
      必须 exit 0（被产品锁拦截 → 只有一处 lease/窗口/后端），不产生第二套应用。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

R9_VERIFY = Path("D:/qio-dev/q46-r9/_lead-logs/qio032-46/r9/verify")
sys.path.insert(0, str(R9_VERIFY))
import win_instance_accept as W  # noqa: E402


def _log_tail(path, limit=1200):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(日志不可读)"
    return text.strip()[-limit:]


def main():
    ap = argparse.ArgumentParser(description="release qio.exe 的 QIO_INSTANCE_PROBE(_SUFFIX) env 过敏验收")
    ap.add_argument("--exe", required=True, help="release 构建的 qio.exe（在安装/暂存目录内，旁边有 qio-backend.exe）")
    ap.add_argument("--work", default="D:/qio-dev/_win-r10/env-alarmy")
    ap.add_argument("--cdp-port", type=int, default=9225)
    ap.add_argument("--timeout", type=float, default=240.0, help="首个实例就绪等待上限（秒）")
    args = ap.parse_args()

    exe = Path(args.exe).resolve()
    install_dir = exe.parent
    if not exe.is_file():
        raise SystemExit("[FAIL] --exe 不存在：%s" % exe)
    if not (install_dir / "qio-backend.exe").is_file():
        raise SystemExit("[FAIL] %s 下没有 qio-backend.exe：请把 --exe 指向完整的安装/暂存目录" % install_dir)

    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    data = work / "data-a"
    results = []

    def rec(cid, title, state, detail):
        results.append({"id": cid, "title": title, "state": state, "detail": detail})
        print("[%s] %s %s — %s" % (cid, state, title, detail), flush=True)

    def finish(code=0):
        out = work / "results.json"
        out.write_text(json.dumps({"results": results, "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        failed = [x for x in results if x["state"] == "FAIL"]
        print("SUMMARY pass=%d fail=%d unverified=%d total=%d" % (
            len([x for x in results if x["state"] == "PASS"]), len(failed),
            len([x for x in results if x["state"] == "UNVERIFIED"]), len(results)), flush=True)
        return 1 if failed else code

    # 清理安装目录里可能残留的上一轮进程/lease（只按安装目录映像路径收）。
    W.stop_everything([install_dir])

    p1 = p2 = None
    try:
        # ---------- R1：首个进程继承测试 env，仍必须按正式实例启动 ----------
        env_a = {"QIO_INSTANCE_PROBE": "hold", "QIO_INSTANCE_PROBE_SUFFIX": "inherited-a"}
        p1, log_a = W.launch(exe, work, data, args.cdp_port, "env-a", env_a)
        lease, win = W.wait_ready(install_dir, p1.pid, timeout=args.timeout)
        backends = W.backend_pids(install_dir)
        exit1 = p1.poll()
        ok1 = (lease is not None) and (win is not None) and (exit1 is None) \
            and W.process_alive(p1.pid) and len(backends) >= 1
        detail1 = "alive=%s exit=%r lease=%s lease_shell_pid=%r window=%s backends=%s" % (
            W.process_alive(p1.pid), exit1, bool(lease),
            (lease or {}).get("shell", {}).get("pid") if lease else None,
            bool(win), backends)
        if not ok1:
            detail1 += "；进程输出尾部=%r（正式构建若执行探针会在打印 JSON 后 exit 0：见 red 证据）" % _log_tail(log_a)
        rec("R1", "release exe 继承 QIO_INSTANCE_PROBE=hold + SUFFIX=inherited-a 仍按正式实例启动（lease/窗口/后端）",
            "PASS" if ok1 else "FAIL", detail1)

        if ok1:
            before_backends = set(W.backend_pids(install_dir))
            before_windows = len(W.window_list(install_dir))
            before_leases = len(W.canonical_leases(install_dir))
            # ---------- R1b：同会话第二次启动（另一后缀）只唤醒已有窗口 ----------
            env_b = {"QIO_INSTANCE_PROBE": "hold", "QIO_INSTANCE_PROBE_SUFFIX": "inherited-b"}
            p2, _log_b = W.launch(exe, work, work / "data-b", args.cdp_port, "env-b", env_b)
            try:
                code2 = p2.wait(timeout=45)
            except subprocess.TimeoutExpired:
                code2 = "TIMEOUT"
            alive2 = W.process_alive(p2.pid)
            time.sleep(2.5)
            after_backends = set(W.backend_pids(install_dir))
            windows_after = W.window_list(install_dir)
            leases_after = W.canonical_leases(install_dir)
            lease_after, _ = W.read_lease(install_dir)
            ok2 = ((not alive2) and code2 == 0
                   and len(windows_after) == 1
                   and len(leases_after) == 1
                   and after_backends == before_backends
                   and lease_after is not None
                   and int(lease_after.get("shell", {}).get("pid", -1)) == p1.pid)
            rec("R1b", "同会话第二次启动带 SUFFIX=inherited-b：exit 0、只唤醒已有窗口（无第二窗口/后端/lease）",
                "PASS" if ok2 else "FAIL",
                "second_alive=%s code=%r windows(before=%d after=%d) leases(before=%d after=%d) "
                "backends(before=%s after=%s) lease_shell_pid=%r" % (
                    alive2, code2, before_windows, len(windows_after),
                    before_leases, len(leases_after), sorted(before_backends), sorted(after_backends),
                    (lease_after or {}).get("shell", {}).get("pid") if lease_after else None))
        else:
            rec("R1b", "同会话第二次启动带 SUFFIX=inherited-b：只唤醒已有窗口", "UNVERIFIED",
                "R1 未通过，首实例未作为正式实例就绪，无法验证重复启动")
    finally:
        W.stop_everything([install_dir])

    return finish()


if __name__ == "__main__":
    sys.exit(main())
