#!/usr/bin/env python
"""A 项的端到端验证：帮助程序只收「lease 记录的那个进程」，不碰同名的别的进程。

场景：
  1) 目录 A：真 qio-backend.exe 跑着 + 一份指向它的 lease（三要素正确）→ --check-only 必须 exit 0，
     --close-installation 必须收掉它、删 lease、exit 0；
  2) 目录 B：真 qio-backend.exe 跑着 + **指向 A 的 lease**（冒名/陈旧记录）→ 必须 exit 3 且 B 一个进程都不动；
  3) 目录 C：lease 指向一个已被复用/不存在的 pid → 必须 exit 3 且不动任何进程；
  4) 反证：整个过程不许按映像名杀 —— 结束时 B 必须原样活着。
"""
import json, os, shutil, socket, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".repro" / "helper-e2e"
SIDECAR = Path(r"C:\Users\zxy\Documents\Front agent\qio-wt-fixes\frontend\src-tauri\binaries\qio-backend-x86_64-pc-windows-msvc.exe")
HELPER = ROOT / "frontend" / "src-tauri" / "target" / "release" / "qio-uninstall-helper.exe"

def log(m): print(m, flush=True)

def ps(cmd, timeout=120):
    p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=timeout)
    return (p.stdout or "") + (p.stderr or "")

def pids_by_path(exe):
    target = str(Path(exe).resolve()).lower().replace("\\", "\\\\").replace("'", "''")
    out = ps("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLower() -eq '" + target + "' } | ForEach-Object { $_.ProcessId }")
    return [int(x) for x in out.split() if x.strip().isdigit()]

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def start_backend(d):
    data = d / "data"; tmp = d / "tmp"
    data.mkdir(parents=True, exist_ok=True); tmp.mkdir(parents=True, exist_ok=True)
    token = d / "session.token"
    port = free_port()
    env = {k: os.environ.get(k, "") for k in ("PATH","PATHEXT","SYSTEMROOT","SYSTEMDRIVE","WINDIR","COMSPEC","USERPROFILE","HOMEDRIVE","HOMEPATH","APPDATA","LOCALAPPDATA","PROGRAMDATA")}
    env.update({"TEMP": str(tmp), "TMP": str(tmp), "QIO_DISABLE_DB_CHECK": "1", "QIO_HOST": "127.0.0.1",
                "QIO_PORT": str(port), "QIO_DATA_DIR": str(data), "QIO_SESSION_TOKEN_FILE": str(token),
                "PYTHONIOENCODING": "utf-8"})
    fh = open(d / "backend.log", "wb")
    proc = subprocess.Popen([str(d / "qio-backend.exe")], cwd=str(d), env=env, stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc, port, token

def wait_health(port, token, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/api/health" % port)
            tok = token.read_text(encoding="utf-8", errors="replace").strip() if token.exists() else ""
            if tok: req.add_header("Authorization", "Bearer " + tok)
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200: return True
        except Exception: pass
        time.sleep(1)
    return False

# 帮助程序读的是 GetProcessTimes 的 FILETIME（纪元 1601-01-01，100ns 单位）。
# PowerShell 的 StartTime 是 .NET DateTime（纪元 0001-01-01），它的 Ticks **更大**：
#   ToFileTimeUtc() = Ticks - 504911232000000000
# 第一版把符号写反了（+），于是"身份对不上"被误读成实现坏了 —— 这是夹具 bug，不是产品 bug。
# 直接让 .NET 做换算（不要自己加减常量），避免再踩同一个坑。


def filetime(pid):
    out = ps(
        "$p = Get-Process -Id %d -ErrorAction SilentlyContinue; " % pid
        + "if ($p) { $p.StartTime.ToUniversalTime().ToFileTimeUtc() }"
    ).strip()
    return out if out.isdigit() else ""

def process_path(pid):
    """进程的真实映像路径（不要自己拼 C:\\Windows\\System32\\notepad.exe：
    WOW64 重定向/大小写会让拼出来的字符串跟进程实际路径不一致 —— 第一版就踩了这个，
    shell_identity_mismatch 被误读成实现坏了）。"""
    out = ps("(Get-Process -Id %d -ErrorAction SilentlyContinue).Path" % pid).strip()
    return out


def run_helper(*args):
    p = subprocess.run([str(HELPER)] + list(args), capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

def main():
    shutil.rmtree(WORK, ignore_errors=True)
    results = []
    def record(name, ok, detail):
        results.append({"check": name, "ok": bool(ok), "detail": detail})
        log("[%s] %s :: %s" % ("PASS" if ok else "FAIL", name, detail))

    # 三个安装目录，各一份真 backend
    dirs = {}
    for name in ("A", "B", "C"):
        d = WORK / name
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SIDECAR, d / "qio-backend.exe")
        proc, port, token = start_backend(d)
        ok = wait_health(port, token)
        dirs[name] = {"dir": d, "proc": proc, "port": port, "token": token}
        record("precondition: %s 后端就绪" % name, ok, "pid=%s port=%s" % (proc.pid, port))
    if not all(v["proc"].poll() is None for v in dirs.values()):
        log("!! 有后端没起来，停"); print(json.dumps(results, ensure_ascii=False, indent=2)); return 1

    # 「壳」用**独立的替身进程**，不是测试脚本自己：
    #   * 收壳动作（WM_CLOSE / taskkill）会真的把目标结束掉 —— 拿自己当壳等于测试自杀；
    #   * 而且"壳"有窗口时才能验证优雅关闭那条路（notepad 就是有窗口的真进程）。
    # 壳用**长驻的独立进程**，不要用 notepad：这台机器上的 Notepad 是 Store 应用，
    # 启动器会立刻退出、真正的窗口进程是另一个 pid（实测 shell_identity_mismatch）。
    # powershell -NoExit 是个稳定长驻、有控制台窗口的替身。
    def spawn_shell():
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-NoExit", "-Command", "Start-Sleep -Seconds 600"],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        for _ in range(40):
            time.sleep(0.25)
            if p.poll() is not None:
                return p
            if process_path(p.pid):
                break
        return p

    shells = {name: spawn_shell() for name in ("A", "B", "C")}
    for name, p in shells.items():
        record("precondition: %s 替身壳进程" % name, p.poll() is None, "pid=%s" % p.pid)

    # lease：A 指向自己（正确）；B 指向自己（正确，用来验"收 A 不动 B"）；C 指向不存在的 pid
    def lease_for(install_dir, backend_pid, shell_pid, shell_exe, backend_exe):
        return {
            "schema": 1,
            "install_dir": str(install_dir),
            "shell": {"pid": shell_pid, "created_filetime": filetime(shell_pid), "exe": str(shell_exe)},
            "backend": {"pid": backend_pid, "created_filetime": filetime(backend_pid), "exe": str(backend_exe)},
            "started_at": "2026-10-03T00:00:00Z",
        }

    shell_exes = {name: process_path(p.pid) for name, p in shells.items()}
    for name, path in shell_exes.items():
        record("precondition: %s 替身壳路径" % name, bool(path), path or "(取不到)")

    lease_a = lease_for(dirs["A"]["dir"], dirs["A"]["proc"].pid, shells["A"].pid,
                       shell_exes["A"], dirs["A"]["dir"] / "qio-backend.exe")
    (dirs["A"]["dir"] / "sidecar.lease.json").write_text(json.dumps(lease_a, ensure_ascii=False, indent=2), encoding="utf-8")
    lease_b = lease_for(dirs["B"]["dir"], dirs["B"]["proc"].pid, shells["B"].pid,
                       shell_exes["B"], dirs["B"]["dir"] / "qio-backend.exe")
    (dirs["B"]["dir"] / "sidecar.lease.json").write_text(json.dumps(lease_b, ensure_ascii=False, indent=2), encoding="utf-8")
    # 冒名：内容指向 A 的进程（含 A 的安装目录），但放在 B 的目录里 ——
    # 契约要求"backend.exe 必须在 install_dir 下"，所以这必须被拒。
    lease_imp = dict(lease_a)
    lease_imp["install_dir"] = str(dirs["B"]["dir"])
    imp_dir = WORK / "IMP"
    imp_dir.mkdir(parents=True, exist_ok=True)
    (imp_dir / "sidecar.lease.json").write_text(json.dumps(lease_imp, ensure_ascii=False, indent=2), encoding="utf-8")
    lease_c = lease_for(dirs["C"]["dir"], 999999, 999999, "C:\\nope.exe", dirs["C"]["dir"] / "qio-backend.exe")
    lease_c["shell"] = {"pid": 999999, "created_filetime": "1", "exe": "C:\\nope.exe"}
    (dirs["C"]["dir"] / "sidecar.lease.json").write_text(json.dumps(lease_c, ensure_ascii=False, indent=2), encoding="utf-8")

    # 1) --check-only：A/B 可收、冒名与陈旧记录不可确认
    for name in ("A", "B"):
        code, out, err = run_helper("--check-only", "--install-dir", str(dirs[name]["dir"]), "--json")
        record("check-only %s（正确 lease）exit=0" % name, code == 0, "exit=%s out=%s" % (code, out[:280]))
    code, out, err = run_helper("--check-only", "--install-dir", str(imp_dir), "--json")
    record("check-only 冒名 lease（exe 不在 install_dir 下）exit=3", code == 3, "exit=%s out=%s" % (code, out[:280]))
    code, out, err = run_helper("--check-only", "--install-dir", str(dirs["C"]["dir"]), "--json")
    record("check-only C（陈旧 pid）exit=3", code == 3, "exit=%s out=%s" % (code, out[:280]))

    # 2) 关键：对**冒名**目录跑 --close-installation —— 必须拒绝，B 一个进程都不能动
    code, out, err = run_helper("--close-installation", "--install-dir", str(imp_dir), "--json", "--timeout-ms", "2000")
    time.sleep(2)
    b_alive = len(pids_by_path(dirs["B"]["dir"] / "qio-backend.exe")) > 0
    record("close 冒名 lease exit=3 且 B 仍活着", code == 3 and b_alive, "exit=%s alive=%s out=%s" % (code, b_alive, out[:280]))

    # 3) 对 A 跑 --close-installation：必须收掉 A 自己的树、删 lease
    code, out, err = run_helper("--close-installation", "--install-dir", str(dirs["A"]["dir"]), "--json", "--timeout-ms", "3000")
    time.sleep(3)
    a_tree = pids_by_path(dirs["A"]["dir"] / "qio-backend.exe")
    lease_gone = not (dirs["A"]["dir"] / "sidecar.lease.json").exists()
    record("close A（正确 lease）exit=0", code == 0, "exit=%s out=%s" % (code, out[:300]))
    record("A 的 backend 进程树已结束", not a_tree, "残留=%s" % a_tree)
    record("A 的 lease 已删除", lease_gone, "exists=%s" % (not lease_gone))
    # 用**进程树**判定存活，不要用 launcher 的 poll()：onefile 的 launcher 可能已经退出、
    # 真正的 child 还在跑（这正是"按名字杀会误伤"的另一面）。
    b_tree_after = pids_by_path(dirs["B"]["dir"] / "qio-backend.exe")
    record("B 仍然活着（没有被 A 的关闭动作误伤）",
           bool(b_tree_after) and dirs["B"]["proc"].poll() is None,
           "B pid=%s tree=%s" % (dirs["B"]["proc"].pid, b_tree_after))

    # 收尾：只按 pid 收（含替身壳）。
    # onefile 是 launcher + child **两个**进程，taskkill /T 有时只收掉父进程（实测），
    # 所以收完必须复核；还有残留就再收一轮，并且要等文件锁真的放开
    # （否则下一次运行会因为 qio-backend.exe 被占用而 PermissionError）。
    for p in shells.values():
        if p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, text=True)
    for _ in range(5):
        leftover = []
        for name, v in dirs.items():
            leftover += pids_by_path(v["dir"] / "qio-backend.exe")
        if not leftover:
            break
        for pid in leftover:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, text=True)
        time.sleep(2)
    (WORK / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r for r in results if not r["ok"]]
    log("== %d/%d 通过 ==" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())