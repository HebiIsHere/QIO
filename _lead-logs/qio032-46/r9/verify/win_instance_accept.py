# -*- coding: utf-8 -*-
"""QIO #46 r9 Windows 安装版单实例验收（只写 _lead-logs/qio032-46/r9）。

断言全部来自 OS 事实：进程树（按安装目录映像路径）、主窗口 HWND（EnumWindows）、
lease 文件（安装目录里的 sidecar.lease*.json）、后端监听端口；不以日志文案作为通过依据。
重复启动的短命进程允许正常退出，判定依据是它有没有启动第二套应用（第二窗口 / 第二后端 / 第二份记录）。
"""
import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path("D:/qio-dev/q46-r9")
RESULTS = []


def log(msg):
    print(msg, flush=True)


def record(cid, title, state, detail):
    RESULTS.append({"id": cid, "title": title, "state": state, "detail": detail})
    log("[%s] %s %s — %s" % (cid, state, title, detail))


# ---------------------------------------------------------------- 进程 / 窗口
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _snapshot():
    snap = []
    h = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if h == -1:
        return snap
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = kernel32.Process32FirstW(h, ctypes.byref(e))
    while ok:
        snap.append({"pid": int(e.th32ProcessID), "ppid": int(e.th32ParentProcessID), "name": e.szExeFile})
        ok = kernel32.Process32NextW(h, ctypes.byref(e))
    kernel32.CloseHandle(h)
    return snap


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_path(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(32768)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def processes_in_dir(install_dir):
    prefix = os.path.normcase(str(Path(install_dir).resolve())) + os.sep
    out = []
    for item in _snapshot():
        exe = process_path(item["pid"])
        if exe and os.path.normcase(str(Path(exe).resolve())).startswith(prefix):
            out.append({**item, "exe": exe})
    return out


def process_alive(pid):
    h = kernel32.OpenProcess(0x1000, 0, int(pid))
    if not h:
        return False
    code = wintypes.DWORD()
    ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    kernel32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def wait_processes_gone(pids, timeout=30.0):
    deadline = time.time() + timeout
    left = [int(p) for p in pids]
    while True:
        left = [p for p in left if process_alive(p)]
        if not left or time.time() >= deadline:
            return left
        time.sleep(0.3)


def kill_tree(pid):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True, timeout=60)


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows_of_pid(pid):
    found = []

    def cb(hwnd, _lparam):
        win_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if win_pid.value == int(pid):
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            found.append({
                "hwnd": int(hwnd),
                "title": buf.value,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "iconic": bool(user32.IsIconic(hwnd)),
            })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def main_window(pid):
    wins = [w for w in windows_of_pid(pid) if w["title"].strip()]
    wins.sort(key=lambda w: (not w["visible"], w["iconic"]))
    return wins[0] if wins else None


def wait_main_window(pid, timeout=90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(pid):
            return None
        win = main_window(pid)
        if win and win["visible"]:
            return win
        time.sleep(0.3)
    return main_window(pid)


def post_wm_close(hwnd):
    user32.PostMessageW(wintypes.HWND(hwnd), 0x0010, 0, 0)


def minimize(hwnd):
    return bool(user32.ShowWindow(wintypes.HWND(hwnd), 6))  # SW_MINIMIZE


# ---------------------------------------------------------------- lease / 启动
def lease_files(install_dir):
    return sorted(Path(install_dir).glob("sidecar.lease*.json"))


def read_lease(install_dir):
    for path in reversed(lease_files(install_dir)):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("shell"), dict):
            return data, path
    return None, None


def wait_ready(install_dir, shell_pid, timeout=180.0):
    """等到「外壳存活 + lease 是本进程 + 后端已监听 + 主窗口可见」四件事都成立。

    后端是 launcher + onefile child 两段：只等 lease 会在 child 还没起监听时冻结到不完整的
    进程树基线，后面任何一次比较都会假失败。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_alive(shell_pid):
            return None, None
        lease, path = read_lease(install_dir)
        if lease and int(lease["shell"]["pid"]) == int(shell_pid) and backend_port(install_dir):
            win = wait_main_window(shell_pid, timeout=max(5.0, deadline - time.time()))
            if win:
                return lease, win
        time.sleep(0.5)
    return None, None


def backend_pids(install_dir):
    """安装目录里的后端进程树（launcher + onefile child）；监听端口的可能是 child。"""
    return [item["pid"] for item in processes_in_dir(install_dir)
            if "backend" in os.path.basename(item["exe"]).lower()]


def backend_port(install_dir):
    pids = backend_pids(install_dir)
    if not pids:
        return None
    script = ("$ids=@(%s); Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
              "Where-Object { $ids -contains $_.OwningProcess } | Select-Object -First 1 -ExpandProperty LocalPort"
              % ",".join(str(pid) for pid in pids))
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60).stdout
    for token in out.split():
        if token.strip().isdigit():
            return int(token.strip())
    return None


def clean_env(work, data_dir, cdp_port, extra=None):
    keep = ["SystemRoot", "windir", "SystemDrive", "COMSPEC", "PATHEXT", "PATH",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
            "USERPROFILE", "USERNAME", "USERDOMAIN", "APPDATA", "ProgramData",
            "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles", "COMPUTERNAME",
            "HOMEDRIVE", "HOMEPATH", "PUBLIC", "SESSIONNAME", "TZ"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    tmp = Path(work) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    local = Path(work) / "localappdata"
    local.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = env["TMP"] = str(tmp)
    env["LOCALAPPDATA"] = str(local)
    env["QIO_DATA_DIR"] = str(data_dir)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=%d" % int(cdp_port)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def launch(exe, work, data_dir, cdp_port, tag, extra_env=None):
    logdir = Path(work) / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / ("shell-%s.log" % tag)
    fh = logfile.open("wb")
    proc = subprocess.Popen([str(exe)], cwd=str(Path(exe).parent),
                            env=clean_env(work, data_dir, cdp_port, extra_env),
                            stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log("  $ launch[%s] %s pid=%s" % (tag, exe, proc.pid))
    return proc, logfile


def stop_everything(install_dirs):
    pids = set()
    for d in install_dirs:
        for item in processes_in_dir(d):
            pids.add(item["pid"])
    for pid in sorted(pids, reverse=True):
        kill_tree(pid)
    if pids:
        wait_processes_gone(pids, 30)
    for d in install_dirs:
        for f in lease_files(d):
            try:
                f.unlink()
            except OSError:
                pass
    return sorted(pids)


# ---------------------------------------------------------------- 场景
def summarize_state(install_dir):
    items = processes_in_dir(install_dir)
    wins = []
    for item in items:
        wins.extend(windows_of_pid(item["pid"]))
    return {"processes": items, "windows": wins, "leases": [str(p) for p in lease_files(install_dir)]}


def window_list(install_dir):
    """**可见**的主窗口（隐藏的辅助/工具窗口不算第二套应用）。"""
    wins = []
    for item in processes_in_dir(install_dir):
        wins.extend([w for w in windows_of_pid(item["pid"]) if w["title"].strip() and w["visible"]])
    return wins


def canonical_leases(install_dir):
    """按实例区分的权威 lease（sidecar.lease.<pid>.json）；旧版兼容镜像不算第二份。"""
    return [p for p in lease_files(install_dir) if re.match(r"sidecar\.lease\.\d+\.json$", p.name)]


def scenario_duplicate(exe, install_dir, work, cdp_ports):
    stop_everything([install_dir])
    data = Path(work) / "data-dup"
    shutil.rmtree(data, ignore_errors=True)
    p1, _ = launch(exe, work, data, cdp_ports[0], "dup-primary")
    lease, win = wait_ready(install_dir, p1.pid)
    if not lease or not win:
        record("S1", "普通重复启动：首个实例就绪", "FAIL", "首实例未就绪（进程存活=%s）" % process_alive(p1.pid))
        return None
    base = summarize_state(install_dir)
    backends_before = backend_pids(install_dir)
    p2, _ = launch(exe, work, data, cdp_ports[0], "dup-second")
    try:
        code2 = p2.wait(timeout=30)
    except subprocess.TimeoutExpired:
        code2 = "TIMEOUT"
    time.sleep(3)
    after = summarize_state(install_dir)
    alive2 = process_alive(p2.pid)
    after_backends = backend_pids(install_dir)
    windows_now = window_list(install_dir)
    ok = (not alive2) and code2 == 0 and after_backends == backends_before and len(canonical_leases(install_dir)) == 1 and len(windows_now) == 1
    record("S1", "普通重复启动：第二进程退出、无第二套应用", "PASS" if ok else "FAIL",
           "second_alive=%s code=%s backends_before=%s after=%s leases=%d windows=%d" % (
               alive2, code2, backends_before, after_backends, len(canonical_leases(install_dir)), len(windows_now)))
    port = backend_port(install_dir)
    alive_first = process_alive(p1.pid) and main_window(p1.pid) is not None
    record("S1b", "重复启动后首实例主窗口与后端仍正常", "PASS" if (alive_first and port) else "FAIL",
           "shell_alive=%s window=%s backend_port=%s" % (alive_first, main_window(p1.pid) is not None, port))
    return p1


def scenario_rapid(exe, install_dir, work, cdp_ports):
    stop_everything([install_dir])
    data = Path(work) / "data-rapid"
    shutil.rmtree(data, ignore_errors=True)
    p1, _ = launch(exe, work, data, cdp_ports[0], "rapid-1")
    p2, _ = launch(exe, work, data, cdp_ports[0], "rapid-2")
    deadline = time.time() + 40
    while time.time() < deadline:
        alive = [p for p in (p1, p2) if process_alive(p.pid)]
        if len(alive) <= 1:
            break
        time.sleep(0.2)
    time.sleep(8)
    survivors = [p for p in (p1, p2) if process_alive(p.pid)]
    exit_codes = {p.pid: p.poll() for p in (p1, p2)}
    items = processes_in_dir(install_dir)
    backends = [x for x in items if "backend" in os.path.basename(x["exe"]).lower()]
    wins = window_list(install_dir)
    ok = len(survivors) == 1 and 1 <= len(backends) <= 2 and len(wins) == 1
    record("S2", "快速同时启动：只留下一套应用", "PASS" if ok else "FAIL",
           "alive=%s exits=%s backends=%s windows=%d" % ([p.pid for p in survivors], exit_codes, [b["pid"] for b in backends], len(wins)))
    return survivors[0] if survivors else None


def scenario_not_ready(exe, install_dir, work, cdp_ports):
    stop_everything([install_dir])
    data = Path(work) / "data-notready"
    shutil.rmtree(data, ignore_errors=True)
    p1, _ = launch(exe, work, data, cdp_ports[0], "notready-primary")
    time.sleep(0.25)
    p2, _ = launch(exe, work, data, cdp_ports[0], "notready-second")
    try:
        code2 = p2.wait(timeout=40)
    except subprocess.TimeoutExpired:
        code2 = "TIMEOUT"
    lease, win = wait_ready(install_dir, p1.pid, timeout=180)
    time.sleep(1)
    wins = window_list(install_dir)
    ok = (not process_alive(p2.pid)) and code2 == 0 and lease is not None and len(wins) == 1
    record("S3", "首实例尚未就绪时第二次启动：登记激活、只有一套应用", "PASS" if ok else "FAIL",
           "second_alive=%s code=%s lease=%s windows=%d" % (process_alive(p2.pid), code2, bool(lease), len(wins)))
    return p1, lease


def scenario_minimize_tray(exe, install_dir, work, cdp_ports, p1):
    win = main_window(p1.pid)
    if not win:
        record("S4", "最小化后再次启动：恢复并聚焦已有窗口", "FAIL", "找不到主窗口")
        return
    minimize(win["hwnd"])
    time.sleep(1.0)
    minimized = [w for w in windows_of_pid(p1.pid) if w["hwnd"] == win["hwnd"]][0]
    p2, _ = launch(exe, work, Path(work) / "data-min", cdp_ports[0], "min-second")
    try:
        code2 = p2.wait(timeout=30)
    except subprocess.TimeoutExpired:
        code2 = "TIMEOUT"
    time.sleep(2.5)
    restored = [w for w in windows_of_pid(p1.pid) if w["hwnd"] == win["hwnd"]][0]
    ok = minimized["iconic"] and (not restored["iconic"]) and restored["visible"] and code2 == 0
    record("S4", "最小化后再次启动：恢复并聚焦已有窗口", "PASS" if ok else "FAIL",
           "was_iconic=%s now_iconic=%s visible=%s second_code=%s" % (
               minimized["iconic"], restored["iconic"], restored["visible"], code2))


def scenario_tray(exe, install_dir, work, cdp_ports, p1):
    win = main_window(p1.pid)
    if not win:
        record("S5", "隐藏到托盘后再次启动：恢复已有窗口", "FAIL", "找不到主窗口")
        return
    post_wm_close(win["hwnd"])
    time.sleep(2.0)
    hidden = [w for w in windows_of_pid(p1.pid) if w["hwnd"] == win["hwnd"]][0]
    p2, _ = launch(exe, work, Path(work) / "data-tray", cdp_ports[0], "tray-second")
    try:
        code2 = p2.wait(timeout=30)
    except subprocess.TimeoutExpired:
        code2 = "TIMEOUT"
    time.sleep(2.5)
    shown = [w for w in windows_of_pid(p1.pid) if w["hwnd"] == win["hwnd"]][0]
    ok = (not hidden["visible"]) and shown["visible"] and code2 == 0 and process_alive(p1.pid)
    record("S5", "隐藏到托盘后再次启动：恢复已有窗口", "PASS" if ok else "FAIL",
           "hidden_before=%s visible_after=%s second_code=%s" % (not hidden["visible"], shown["visible"], code2))


def scenario_cross_dir(exe_a, exe_b, install_a, install_b, work, cdp_ports):
    stop_everything([install_a, install_b])
    data = Path(work) / "data-cross"
    shutil.rmtree(data, ignore_errors=True)
    p1, _ = launch(exe_a, work, data, cdp_ports[0], "cross-a")
    lease, win = wait_ready(install_a, p1.pid)
    if not lease:
        record("S6", "不同安装目录重复启动：第二份安装被拦截", "FAIL", "A 实例未就绪")
        return None, None
    p2, _ = launch(exe_b, work, Path(work) / "data-cross-b", cdp_ports[0], "cross-b")
    try:
        code2 = p2.wait(timeout=40)
    except subprocess.TimeoutExpired:
        code2 = "TIMEOUT"
    time.sleep(2)
    b_items = processes_in_dir(install_b)
    b_wins = window_list(install_b)
    b_leases = lease_files(install_b)
    ok = (not process_alive(p2.pid)) and code2 == 0 and len(b_items) == 0 and not b_leases and not b_wins
    record("S6", "不同安装目录重复启动：第二份安装被拦截（无第二后端/记录/窗口）", "PASS" if ok else "FAIL",
           "second_alive=%s code=%s b_processes=%s b_leases=%d b_windows=%d" % (
               process_alive(p2.pid), code2, [x["pid"] for x in b_items], len(b_leases), len(b_wins)))
    return p1, lease


def scenario_force_kill(exe, install_dir, work, cdp_ports):
    stop_everything([install_dir])
    data = Path(work) / "data-kill"
    shutil.rmtree(data, ignore_errors=True)
    p1, _ = launch(exe, work, data, cdp_ports[0], "kill-first")
    lease, win = wait_ready(install_dir, p1.pid)
    if not lease:
        record("S7", "强制结束后重开：锁被内核释放、可以重新启动", "FAIL", "首实例未就绪")
        return None, None
    kill_tree(p1.pid)
    gone = wait_processes_gone([p1.pid] + [x["pid"] for x in processes_in_dir(install_dir)], 40)
    time.sleep(1.0)
    p2, _ = launch(exe, work, data, cdp_ports[0], "kill-second")
    lease2, win2 = wait_ready(install_dir, p2.pid, timeout=180)
    ok = not gone and lease2 is not None and win2 is not None
    record("S7", "强制结束后重开：锁被内核释放、可以重新启动", "PASS" if ok else "FAIL",
           "old_left=%s new_ready=%s new_window=%s" % (gone, bool(lease2), bool(win2)))
    return p2, lease2


CDP_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL("D:/qio-dev/q46-r9/_lead-logs/qio032-46/r9/verify/lib-r9.mjs").href);
const port = Number(process.env.QIO_CDP_PORT || 9223);
const page = await mod.connectApp({ port });
const invoke = (cmd) => page.eval("window.__TAURI_INTERNALS__.invoke(" + JSON.stringify(cmd) + ")");
const prep = await invoke("qio_prepare_for_update");
console.log("PREPARE_RESULT", JSON.stringify(prep));
if (prep && prep.stopped === true && prep.verified === true) console.log("PREPARE_OK");
const restore = await invoke("qio_restore_backend");
console.log("RESTORE_RESULT", JSON.stringify(restore));
if (restore && restore.restored === true) console.log("RESTORE_OK");
process.exit(0);
"""

CDP_RELAUNCH_SCRIPT = r"""
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL("D:/qio-dev/q46-r9/_lead-logs/qio032-46/r9/verify/lib-r9.mjs").href);
const port = Number(process.env.QIO_CDP_PORT || 9223);
const page = await mod.connectApp({ port });
await page.eval("window.__TAURI_INTERNALS__.invoke('qio_arm_relaunch')")
  .then((r) => console.log("ARM_RESULT", JSON.stringify(r)))
  .catch((e) => console.log("ARM_ERR", String(e)));
console.log("READY_TO_RELAUNCH");
await page.eval("window.__TAURI_INTERNALS__.invoke('plugin:process|restart')")
  .catch((e) => console.log("RESTART_ERR", String(e)));
await new Promise((r) => setTimeout(r, 1500));
process.exit(0);
"""


def run_node_script(work, name, code, cdp_port):
    node = Path(work) / name
    node.write_text(code, encoding="utf-8")
    env = dict(os.environ)
    env["QIO_CDP_PORT"] = str(cdp_port)
    run = subprocess.run(["node", str(node)], capture_output=True, text=True, timeout=180, env=env)
    return (run.stdout or "") + ("\n" + run.stderr if run.stderr else "")


def scenario_update_lock(exe, install_dir, work, cdp_ports, p1):
    detail = run_node_script(work, "update-lock-script.mjs", CDP_SCRIPT, cdp_ports[0])
    p2, _ = launch(exe, work, Path(work) / "data-upd", cdp_ports[0], "upd-second")
    try:
        code2 = p2.wait(timeout=30)
    except subprocess.TimeoutExpired:
        code2 = "TIMEOUT"
    ok = ("PREPARE_OK" in detail) and ("RESTORE_OK" in detail) and (not process_alive(p2.pid)) and code2 == 0
    record("S8", "更新期间停/恢复后端：锁持续持有、重复启动仍被拦截", "PASS" if ok else "FAIL",
           "prepare_ok=%s restore_ok=%s second_alive=%s code=%s" % (
               "PREPARE_OK" in detail, "RESTORE_OK" in detail, process_alive(p2.pid), code2))


def scenario_relaunch(exe, install_dir, work, cdp_ports, p1):
    before_pid = p1.pid
    detail = run_node_script(work, "relaunch-script.mjs", CDP_RELAUNCH_SCRIPT, cdp_ports[0])
    deadline = time.time() + 120
    new_shell = None
    max_backend = 0
    max_windows = 0
    while time.time() < deadline:
        items = processes_in_dir(install_dir)
        shells = [x for x in items if os.path.basename(x["exe"]).lower() == "qio.exe"]
        backends = [x for x in items if "backend" in os.path.basename(x["exe"]).lower()]
        wins = window_list(install_dir)
        max_backend = max(max_backend, len(backends))
        max_windows = max(max_windows, len(wins))
        if len(shells) == 1 and shells[0]["pid"] != before_pid and backends and wins:
            new_shell = shells[0]
            break
        time.sleep(0.2)
    time.sleep(2)
    ok = new_shell is not None and max_backend <= 2 and max_windows <= 1 and not process_alive(before_pid)
    record("S9", "更新重启：交接后只有新实例（单窗口/单后端），旧进程退出", "PASS" if ok else "FAIL",
           "new_pid=%s old_alive=%s max_backend=%d max_windows=%d detail=%s" % (
               new_shell["pid"] if new_shell else None, process_alive(before_pid), max_backend, max_windows, detail[-300:]))
    return new_shell


def main():
    parser = argparse.ArgumentParser(description="QIO r9 单实例 Windows 安装版验收")
    parser.add_argument("--exe", required=True, help="新的 qio.exe（会复制进安装目录 A/B）")
    parser.add_argument("--install-a", default="D:/qio-dev/win-install-a")
    parser.add_argument("--install-b", default="D:/qio-dev/win-install-b")
    parser.add_argument("--work", default="D:/qio-dev/_win-r9/run1")
    parser.add_argument("--cdp-port", type=int, default=9223)
    parser.add_argument("--skip-copy", action="store_true")
    args = parser.parse_args()
    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    install_a = Path(args.install_a)
    install_b = Path(args.install_b)
    exe = Path(args.exe)
    # 先收掉上一轮/旧版本仍在跑的进程，否则安装目录里的 qio.exe 被占用，拷不进去。
    stop_everything([install_a, install_b])
    if not args.skip_copy:
        shutil.copy2(exe, install_a / "qio.exe")
        shutil.copy2(exe, install_b / "qio.exe")
    exe_a = install_a / "qio.exe"
    exe_b = install_b / "qio.exe"
    ports = [args.cdp_port, args.cdp_port + 1, args.cdp_port + 2]
    stop_everything([install_a, install_b])
    record("ENV", "验收环境：安装 A/B + 隔离数据目录 + WebView2 CDP", "INFO",
           "install_a=%s install_b=%s work=%s" % (install_a, install_b, work))
    p1 = scenario_duplicate(exe_a, install_a, work, ports)
    if p1:
        # 托盘场景要求首实例以 QIO_CLOSE_BEHAVIOR=tray 启动：重启一遍
        stop_everything([install_a])
        data = Path(work) / "data-tray"
        shutil.rmtree(data, ignore_errors=True)
        pt, _ = launch(exe_a, work, data, ports[0], "tray-primary", {"QIO_CLOSE_BEHAVIOR": "tray"})
        lease_t, win_t = wait_ready(install_a, pt.pid)
        if lease_t and win_t:
            scenario_tray(exe_a, install_a, work, ports, pt)
        else:
            record("S5", "隐藏到托盘后再次启动：恢复已有窗口", "FAIL", "tray 首实例未就绪")
        stop_everything([install_a])
    p = scenario_rapid(exe_a, install_a, work, ports)
    if p:
        stop_everything([install_a])
    p, lease = scenario_not_ready(exe_a, install_a, work, ports)
    if p:
        scenario_minimize_tray(exe_a, install_a, work, ports, p)
        stop_everything([install_a])
    pa, _ = scenario_cross_dir(exe_a, exe_b, install_a, install_b, work, ports)
    if pa:
        stop_everything([install_a, install_b])
    p, lease = scenario_force_kill(exe_a, install_a, work, ports)
    if p:
        scenario_update_lock(exe_a, install_a, work, ports, p)
        stop_everything([install_a, install_b])
    p, lease = scenario_force_kill(exe_a, install_a, work, ports)
    if p:
        scenario_relaunch(exe_a, install_a, work, ports, p)
        stop_everything([install_a, install_b])
    record("S10", "另一 Windows 账号/登录会话重复启动（激活 / 提示后退出）", "UNVERIFIED",
           "本机只有当前登录会话；创建第二个 Windows 用户/会话需要管理员权限且会改动机器账号，未执行")
    out = Path(work) / "results.json"
    out.write_text(json.dumps({"results": RESULTS, "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [r for r in RESULTS if r["state"] == "FAIL"]
    log("")
    log("SUMMARY pass=%d fail=%d unverified=%d total=%d" % (
        len([r for r in RESULTS if r["state"] == "PASS"]), len(failed),
        len([r for r in RESULTS if r["state"] == "UNVERIFIED"]), len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
