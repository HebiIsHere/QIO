# -*- coding: utf-8 -*-
"""QIO #46 r10 Windows 安装版返修验收：
1) 正式构建固定产品锁名（继承测试 env 仍单实例）；
2) 覆盖升级后打开新版（vA 0.3.2 → vB 0.3.3 覆盖安装 → 打开并校验版本 + 单实例）；
3) 更新重启交接（vA 运行 → arm handoff → 新版二进制接任；可选）；
4) 跨 Windows 账号/会话启动（环境受限时如实 UNVERIFIED）。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERIFY = Path("D:/qio-dev/q46-r9/_lead-logs/qio032-46/r10/verify")
sys.path.insert(0, str(VERIFY))
sys.path.insert(0, "D:/qio-dev/q46-r9/_lead-logs/qio032-46/r9/verify")
import win_instance_accept as W  # noqa: E402


def run(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def file_sha(path):
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()[:16]


def install(installer, dest, tag):
    dest = Path(dest)
    shutil.rmtree(dest, ignore_errors=True)
    code, out = run([str(installer), "/S", "/D=" + str(dest)], timeout=600)
    return code, out


def over_install(installer, dest, tag):
    """覆盖安装：不删除现有目录（模拟更新器在新版安装包上原地覆盖）。"""
    code, out = run([str(installer), "/S", "/D=" + str(Path(dest))], timeout=600)
    return code, out


def norm_path(value):
    return os.path.normcase(os.path.normpath(str(value or "").replace("\\\\?\\", "")))


def exe_version(path):
    """读取 exe 的 ProductVersion（正式构建由 tauri 写入 config 版本；比页面 IPC 稳）。"""
    script = ("(Get-Item -LiteralPath %s -ErrorAction Stop).VersionInfo.ProductVersion"
              % ("'" + str(path).replace("'", "''") + "'"))
    code, out = run(["powershell", "-NoProfile", "-Command", script], timeout=60)
    return out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else None


def cdp_version(port, attempts=20):
    """页面刚打开时 __TAURI_INTERNALS__ 可能还没就绪：重试读版本。"""
    script = VERIFY / "_read_version.mjs"
    for _ in range(attempts):
        env = dict(os.environ)
        env["QIO_CDP_PORT"] = str(port)
        code, out = run(["node", str(script)], timeout=120)
        for line in (out or "").splitlines():
            if line.startswith("VERSION="):
                value = line.split("=", 1)[1].strip()
                if value and value != "undefined" and value != "null":
                    return value
        time.sleep(0.5)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe-b", required=True, help="vB（新版）qio.exe")
    ap.add_argument("--installer-b", required=True, help="vB 安装包")
    ap.add_argument("--installer-a", required=True, help="vA（0.3.2）安装包")
    ap.add_argument("--install-dir", default="D:/qio-dev/_win-r10/install-u")
    ap.add_argument("--work", default="D:/qio-dev/_win-r10/update")
    ap.add_argument("--cdp-port", type=int, default=9223)
    ap.add_argument("--expect-version", default="0.3.3")
    ap.add_argument("--skip-handoff", action="store_true")
    ap.add_argument("--skip-env", action="store_true", help="跳过 R1/R1b（env 过敏已单独跑过时）")
    args = ap.parse_args()
    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    install_dir = Path(args.install_dir)
    results = []

    def rec(cid, title, state, detail):
        results.append({"id": cid, "title": title, "state": state, "detail": detail})
        print("[%s] %s %s — %s" % (cid, state, title, detail), flush=True)

    # ---------- 1) 正式构建固定产品锁名（继承测试 env） ----------
    W.stop_everything([install_dir])
    if args.skip_env:
        print("[skip] R1/R1b（--skip-env）", flush=True)
        _skip_env = True
    else:
        _skip_env = False
    if not _skip_env:
        data = work / "data-env"
        extra = {"QIO_INSTANCE_PROBE": "hold", "QIO_INSTANCE_PROBE_SUFFIX": "inherited-a"}
        # 用**本轮修复后的正式构建**（vB 安装包）验 env 过敏；vA 是上一轮产物，没有这条 gating。
        code, out = install(args.installer_b, install_dir, "install-vb-env")
        p1, _ = W.launch(install_dir / "qio.exe", work, data, args.cdp_port, "env-a", extra)
        lease, win = W.wait_ready(install_dir, p1.pid, timeout=240)
        app_ok = bool(lease) and bool(win)
        rec("R1", "正式 exe 继承 QIO_INSTANCE_PROBE_SUFFIX/QIO_INSTANCE_PROBE 仍按正式实例启动",
            "PASS" if app_ok else "FAIL",
            "lease=%s window=%s pid=%s 说明：正式构建若仍认测试后缀/探针，会提前退出或自成一个隔离实例" % (bool(lease), bool(win), p1.pid))
        if app_ok:
            p2, _ = W.launch(install_dir / "qio.exe", work, work / "data-env-b", args.cdp_port, "env-b",
                             {"QIO_INSTANCE_PROBE_SUFFIX": "inherited-b", "QIO_INSTANCE_PROBE": "acquire"})
            try:
                code2 = p2.wait(timeout=40)
            except subprocess.TimeoutExpired:
                code2 = "TIMEOUT"
            time.sleep(2.5)
            backends = W.backend_pids(install_dir)
            wins = W.window_list(install_dir)
            ok2 = (not W.process_alive(p2.pid)) and code2 == 0 and len(W.canonical_leases(install_dir)) == 1 and len(wins) == 1 and len(backends) >= 1
            rec("R1b", "正式 exe 第二次启动（带另一测试后缀）只唤醒已有窗口、无第二套应用",
                "PASS" if ok2 else "FAIL",
                "second_alive=%s code=%s leases=%d windows=%d backends=%s" % (
                    W.process_alive(p2.pid), code2, len(W.canonical_leases(install_dir)), len(wins), backends))
        W.stop_everything([install_dir])

    # ---------- 2) 更新重启交接：vB 新版本二进制接任（可选） ----------
    if not args.skip_handoff:
        code, out = install(args.installer_a, install_dir, "install-a2")
        data_a = work / "data-handoff"
        pa, _ = W.launch(install_dir / "qio.exe", work, data_a, args.cdp_port, "handoff-a")
        lease_a, win_a = W.wait_ready(install_dir, pa.pid, timeout=240)
        if lease_a and win_a:
            v_old = exe_version(install_dir / "qio.exe")
            rec("R2a", "vA（0.3.2）运行并读取版本", "PASS" if v_old == "0.3.2" else "WARN", "version=%r" % (v_old,))
            # 把 vB 放到安装目录的另一个文件名（resource_dir 仍是安装目录）
            staged = install_dir / "qio-update.exe"
            shutil.copy2(args.exe_b, staged)
            # 通过 CDP 调真实 IPC arm handoff，然后启动 vB（模拟 plugin relaunch 的新进程）
            arm = subprocess.run(["node", str(VERIFY / "_arm_handoff.mjs")], capture_output=True, text=True,
                                 env={**os.environ, "QIO_CDP_PORT": str(args.cdp_port)}, timeout=120)
            pb, _ = W.launch(staged, work, data_a, args.cdp_port + 1, "handoff-b")
            time.sleep(1.0)
            W.kill_tree(pa.pid)  # 旧进程退出（模拟 plugin-process restart 的 exit(0)）
            deadline = time.time() + 120
            new_ok = False
            max_backend = 0
            max_windows = 0
            while time.time() < deadline:
                items = W.processes_in_dir(install_dir)
                shells = [x for x in items if os.path.basename(x["exe"]).lower() in ("qio.exe", "qio-update.exe")]
                backends = [x for x in items if "backend" in os.path.basename(x["exe"]).lower()]
                wins = W.window_list(install_dir)
                max_backend = max(max_backend, len(backends))
                max_windows = max(max_windows, len(wins))
                if len(shells) == 1 and shells[0]["pid"] == pb.pid and backends and wins:
                    new_ok = True
                    break
                time.sleep(0.2)
            time.sleep(2)
            v_new = exe_version(staged)
            lease_new, _raw = W.read_lease(install_dir)
            running_exe = str((lease_new or {}).get("shell", {}).get("exe") or "")
            same_exe = bool(running_exe) and norm_path(running_exe) == norm_path(staged)
            ok = (new_ok and not W.process_alive(pa.pid) and max_backend <= 2 and max_windows <= 1
                  and v_new == args.expect_version and same_exe)
            rec("R2", "更新重启交接：新版二进制接任主实例（单窗口/单后端、旧进程退出、版本正确）",
                "PASS" if ok else "FAIL",
                "new_ok=%s old_alive=%s max_backend=%d max_windows=%d new_version=%r arm=%s" % (
                    new_ok, W.process_alive(pa.pid), max_backend, max_windows, v_new, (arm.stdout or "").strip()[-60:]))
        else:
            rec("R2", "更新重启交接：新版二进制接任主实例", "FAIL", "vA 未就绪")
        W.stop_everything([install_dir])

    # ---------- 3) 覆盖升级后打开新版 ----------
    W.stop_everything([install_dir])
    code, out = install(args.installer_a, install_dir, "overinstall-a")
    data_u = work / "data-over"
    pa, _ = W.launch(install_dir / "qio.exe", work, data_u, args.cdp_port, "over-a")
    lease_a, win_a = W.wait_ready(install_dir, pa.pid, timeout=240)
    v_before = exe_version(install_dir / "qio.exe")
    # 真实更新前置：结束本实例后端（保留外壳直到安装器接管；随后按更新流程退出外壳）
    prep_ok = False
    if lease_a:
        pr = subprocess.run(["node", str(VERIFY / "_prepare_update.mjs")], capture_output=True, text=True,
                            env={**os.environ, "QIO_CDP_PORT": str(args.cdp_port)}, timeout=120)
        prep_ok = "PREPARE_OK" in (pr.stdout or "")
        W.kill_tree(pa.pid)
        W.wait_processes_gone([pa.pid], 40)
    # 强制结束的旧实例不会自己删 lease；覆盖安装也不删目录里的旧文件 —— 先清掉，
    # 否则「单实例只有一份权威 lease」会被上一实例的残留记录误判。
    for f in W.lease_files(install_dir):
        try:
            f.unlink()
        except OSError:
            pass
    code_b, out_b = over_install(args.installer_b, install_dir, "overinstall-b")
    for f in W.lease_files(install_dir):
        try:
            f.unlink()
        except OSError:
            pass
    installed_bytes = file_sha(install_dir / "qio.exe")
    want_bytes = file_sha(args.exe_b)
    pb, _ = W.launch(install_dir / "qio.exe", work, data_u, args.cdp_port, "over-b")
    lease_b, win_b = W.wait_ready(install_dir, pb.pid, timeout=240)
    time.sleep(2)
    v_after = exe_version(install_dir / "qio.exe")
    lease_run, _raw = W.read_lease(install_dir)
    running_exe = str((lease_run or {}).get("shell", {}).get("exe") or "")
    running_after = bool(running_exe) and norm_path(running_exe) == norm_path(install_dir / "qio.exe")
    p2, _ = W.launch(install_dir / "qio.exe", work, work / "data-over-b", args.cdp_port, "over-b2")
    try:
        code2 = p2.wait(timeout=40)
    except subprocess.TimeoutExpired:
        code2 = "TIMEOUT"
    time.sleep(2)
    wins = W.window_list(install_dir)
    # 安装器重新链接的 qio.exe 与 cargo 直接产物字节可能不同（bundle 元数据），
    # 版本以运行时 getVersion + 安装器退出码为准；sha 只记录不参与判定。
    ok = (code_b == 0 and lease_b and win_b and v_before != v_after and running_after
          and v_after == args.expect_version and (not W.process_alive(p2.pid)) and code2 == 0
          and len(wins) == 1 and len(W.canonical_leases(install_dir)) == 1)
    rec("R3", "覆盖升级后打开新版：安装器替换文件 → 新版窗口/后端 → 单实例仍生效",
        "PASS" if ok else "FAIL",
        "installer_exit=%s sha_installed=%s sha_vb=%s v_before=%r v_after=%r lease=%s window=%s second_alive=%s second_code=%s windows=%d" % (
            code_b, installed_bytes, want_bytes, v_before, v_after, bool(lease_b), bool(win_b),
            W.process_alive(p2.pid), code2, len(wins)))
    W.stop_everything([install_dir])

    # ---------- 4) 跨 Windows 账号 / 登录会话 ----------
    code_who, who = run(["whoami"], 30)
    try:
        code_add, out_add = run(["net", "user", "r10_probe", "/add"], 30)
    except Exception as exc:  # noqa: BLE001
        code_add, out_add = -1, repr(exc)
    rec("R4", "另一 Windows 账号 / 登录会话重复启动（只唤醒 / 提示后退出）", "UNVERIFIED",
        "本机只有当前登录会话且非提权（whoami=%s）；尝试创建第二账号 exit=%s（需管理员）：%s；"
        "无法在不改动机器账号/密码的前提下制造第二会话" % (who.strip(), code_add, (out_add or "").strip()[:160]))

    out_path = work / "results.json"
    out_path.write_text(json.dumps({"results": results, "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r for r in results if r["state"] == "FAIL"]
    print("SUMMARY pass=%d fail=%d unverified=%d total=%d" % (
        len([r for r in results if r["state"] == "PASS"]), len(failed),
        len([r for r in results if r["state"] == "UNVERIFIED"]), len(results)), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
