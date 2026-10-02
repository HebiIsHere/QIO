"""验证 PyInstaller onefile backend 的真实进程模型与退出行为（Windows）。

为什么需要它：onefile 产物是「launcher + 真正提供服务的 child」两个进程。壳退出时只杀
launcher 会留下 child 锁住 qio-backend.exe，安装器因此报 Can't write。这个脚本用**真实冻结
产物**逐个跑 6 个生命周期 case，并把每个进程的 PID / 父子关系 / 可执行文件 / 命令行 / 创建时间
记录下来 —— 结论必须来自这些原始数据，不能靠推断。

用法（Windows，注意先设置可写的 TEMP）：
    python scripts/verify_backend_process_model.py --exe frontend/src-tauri/binaries/qio-backend-x86_64-pc-windows-msvc.exe
    python scripts/verify_backend_process_model.py --exe <exe> --case 2        # 只跑某个 case
    python scripts/verify_backend_process_model.py --exe <exe> --json out.json

只做只读探测 + 启停自己拉起的进程；不碰别的 QIO 实例（所有操作都以「本次拉起的 launcher pid
为根的进程树」为界）。
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# 本脚本创建过的实例 TEMP 目录：退出时统一删（onefile 的 _MEI 残留不删会堆到 GB 级）
_TEMP_DIRS: list[Path] = []


def cleanup_temp_dirs() -> None:
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)
    _TEMP_DIRS.clear()


atexit.register(cleanup_temp_dirs)

# ---------------------------------------------------------------- Win32 ----

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
JobObjectBasicProcessIdList = 3
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    _fields_ = [
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", ctypes.c_size_t * 1024),
    ]


def job_create(*, kill_on_close: bool = True):
    """建一个 job；默认带 KILL_ON_JOB_CLOSE（与壳里的 backend_job::create 同参数）。"""
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE if kill_on_close else 0
    )
    ok = kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(job)
        raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
    return job


def job_assign(job, pid: int) -> bool:
    """与壳里的 BackendJob::assign 完全相同的调用。"""
    handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(job, handle))
    finally:
        kernel32.CloseHandle(handle)


def job_pids(job) -> list[int]:
    """job 里当前有哪些 pid（JobObjectBasicProcessIdList）—— 判断 child 有没有被收容。"""
    info = _JOBOBJECT_BASIC_PROCESS_ID_LIST()
    ok = kernel32.QueryInformationJobObject(
        job, JobObjectBasicProcessIdList, ctypes.byref(info), ctypes.sizeof(info), None,
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "QueryInformationJobObject")
    return [int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)]


def current_process_is_in_a_job() -> bool:
    """本进程是否已经在某个 job 里（决定 AssignProcessToJobObject 会不会失败）。"""
    result = wintypes.BOOL()
    ok = kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(result))
    return bool(ok and result.value)


def send_ctrl_c_to_console_of(pid: int) -> bool:
    """在**独立进程**里 attach 到目标控制台并发 CTRL_C（等价于用户按 Ctrl+C）。

    必须另起进程：AttachConsole 会抢走调用者自己的控制台，直接在主进程里做会
    影响运行本脚本的终端。目标必须有自己的控制台（spawn 时用 CREATE_NEW_CONSOLE）。
    """
    code = (
        "import ctypes, sys;",
        "k = ctypes.WinDLL('kernel32');",
        "k.FreeConsole();",
        "ok = k.AttachConsole(int(sys.argv[1]));",
        "k.SetConsoleCtrlHandler(None, True);",
        "sent = k.GenerateConsoleCtrlEvent(0, 0) if ok else 0;",
        "print('attach=', bool(ok), 'sent=', bool(sent));",
        "k.FreeConsole()",
    )
    proc = subprocess.run(
        [sys.executable, "-c", " ".join(code), str(pid)],
        capture_output=True, text=True, timeout=30,
    )
    print(f"    ctrl-c helper: rc={proc.returncode} out={proc.stdout.strip()} err={proc.stderr.strip()[:200]}")
    return proc.returncode == 0 and "sent= True" in proc.stdout

# ------------------------------------------------------------ 进程快照 ----

PS_LIST = (
    "Get-CimInstance Win32_Process | "
    "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine,CreationDate | "
    "ConvertTo-Json -Compress"
)


def list_processes() -> list[dict]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", PS_LIST],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


def snapshot(exe: Path, since: float, launcher_pid: int | None = None) -> dict:
    """与本次实验有关的进程：同 exe 路径且创建时间不早于 since。

    同 exe 路径的判定是关键：onefile 的 launcher 与 child 是**同一个可执行文件**，
    只按名字/路径分不出父子，必须靠 ParentProcessId 与创建时间。
    """
    exe_norm = str(exe).lower()
    procs = []
    for item in list_processes():
        path = (item.get("ExecutablePath") or "").lower()
        if path != exe_norm:
            continue
        created = item.get("CreationDate")
        ts = _cim_time_to_epoch(created)
        if ts is not None and ts + 5 < since:
            continue
        procs.append({
            "pid": item.get("ProcessId"),
            "ppid": item.get("ParentProcessId"),
            "name": item.get("Name"),
            "exe": item.get("ExecutablePath"),
            "cmdline": (item.get("CommandLine") or "")[:200],
            "created": str(created),
            "created_ts": ts,
        })
    by_pid = {p["pid"]: p for p in procs}
    roots = [p for p in procs if p["ppid"] not in by_pid]
    return {"processes": procs, "roots": roots, "count": len(procs)}


def _cim_time_to_epoch(value) -> float | None:
    """CIM 的 CreationDate 可能是 /Date(1699999999999)/ 或 ISO 串。"""
    if value is None:
        return None
    text = str(value)
    if text.startswith("/Date("):
        digits = text.split("(")[1].split(")")[0].split("+")[0].split("-")[0]
        try:
            return int(digits) / 1000.0
        except ValueError:
            return None
    return None


def listener_pid(port: int) -> int | None:
    """哪个 pid 在监听这个端口 —— 「真正提供服务的进程」的直接证据。"""
    cmd = (
        f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -First 1 -ExpandProperty OwningProcess)"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    text = (proc.stdout or "").strip()
    return int(text) if text.isdigit() else None


def full_tree(launcher_pid: int) -> list[dict]:
    """从 launcher 出发**递归**枚举整棵进程树（不限可执行文件名）。

    为什么要递归枚举而不是只看同名进程：onefile 是 launcher + child，但中间/旁边还可能有
    别的进程（控制台宿主、trampoline、子进程）；「你以为在管那个进程，其实干活的不是它」
    是这类结构最容易踩的坑（uv 的 venv python 也是同样的 trampoline 结构）。
    """
    all_procs = list_processes()
    by_parent: dict[int, list[dict]] = {}
    for item in all_procs:
        by_parent.setdefault(int(item.get("ParentProcessId") or 0), []).append(item)
    out: list[dict] = []
    stack = [launcher_pid]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        for item in by_parent.get(pid, []):
            row = {
                "pid": int(item.get("ProcessId")),
                "ppid": int(item.get("ParentProcessId") or 0),
                "name": item.get("Name"),
                "exe": item.get("ExecutablePath"),
                "cmdline": (item.get("CommandLine") or "")[:160],
                "created": str(item.get("CreationDate")),
            }
            out.append(row)
            stack.append(row["pid"])
    return sorted(out, key=lambda r: r["pid"])


def print_tree(launcher_pid: int, listener: int | None) -> str:
    rows = full_tree(launcher_pid)
    lines = []
    for row in rows:
        mark = "  <= 监听端口" if listener is not None and row["pid"] == listener else ""
        lines.append(
            f"    pid={row['pid']} ppid={row['ppid']} {row['name']} "
            f"created={row['created']} cmd={row['cmdline'][:70]!r}{mark}"
        )
    return "\n".join(lines) if lines else "    （没有后代）"


def case_tree(exe: Path, work: Path, report: dict) -> None:
    """完整进程树 + 谁在监听端口 + 监听者是否在 job 里（一次跑全）。"""
    print("== 进程树实验：递归枚举 + 监听端口归属 + 监听者是否在 job 里 ==")
    port = free_port()
    since = time.time()
    job = job_create()
    proc, token = spawn_backend(exe, work, port)
    assigned = job_assign(job, proc.pid)
    ready = wait_ready(port, token, exe, since)
    listener = listener_pid(port)
    tree = full_tree(proc.pid)
    pids = job_pids(job)
    print(f"    spawn 返回的 launcher pid={proc.pid} assign={assigned} ready={ready['ready']} port={port}")
    print(f"    监听 {port} 的 pid={listener}")
    print("    完整进程树（递归）：")
    print(print_tree(proc.pid, listener))
    print(f"    job 内 pid={pids}  监听者在 job 里={listener in pids if listener else None}")
    kernel32.CloseHandle(job)
    time.sleep(3.0)
    after = snapshot(exe, since)
    print(f"    关闭 job 后：端口仍开={port_open(port)} 同名残留={after['count']} 监听 pid 还活着={_alive(listener) if listener else None}")
    report["tree"] = {
        "launcher_pid": proc.pid, "assign_ok": assigned, "ready": ready,
        "listener_pid": listener, "tree": tree, "job_pids": pids,
        "listener_in_job": (listener in pids) if listener else None,
        "after_close": after, "port_open_after": port_open(port),
    }
    for pid in [p["pid"] for p in after["processes"]]:
        kill_tree(pid)


def job_stability_experiment(exe: Path, work: Path, report: dict, runs: int = 10) -> None:
    """同一时序重复 N 次：child 到底稳不稳定地落进 job（竞态检查）。"""
    print(f"== Job Object 稳定性：立即 assign 重复 {runs} 次 ==")
    rows = []
    for i in range(1, runs + 1):
        port = free_port()
        since = time.time()
        job = job_create()
        proc, token = spawn_backend(exe, work / f"run{i}", port)
        assigned = job_assign(job, proc.pid)
        ready = wait_ready(port, token, exe, since)
        snap = wait_for_two(exe, since)
        pids = job_pids(job)
        child = next((p for p in snap["processes"] if p["pid"] != proc.pid), None)
        listener = listener_pid(port)
        delta_ms = None
        if child is not None and child.get("created_ts") and snap["processes"]:
            launcher_ts = next((p["created_ts"] for p in snap["processes"] if p["pid"] == proc.pid), None)
            if launcher_ts:
                delta_ms = round((child["created_ts"] - launcher_ts) * 1000)
        row = {
            "run": i, "launcher": proc.pid, "assign_ok": assigned,
            "child": child["pid"] if child else None,
            "child_in_job": bool(child and child["pid"] in pids),
            "listener": listener, "listener_in_job": (listener in pids) if listener else None,
            "launcher_to_child_ms": delta_ms,
            "job_pids": pids, "ready": ready["ready"],
        }
        rows.append(row)
        print(
            f"    run {i:2d}: assign={assigned} child={row['child']} child_in_job={row['child_in_job']} "
            f"listener={listener} listener_in_job={row['listener_in_job']} "
            f"launcher->child={delta_ms}ms job_pids={pids}"
        )
        kernel32.CloseHandle(job)
        time.sleep(2.0)
        leftovers = snapshot(exe, since)
        row["leftovers_after_close"] = leftovers["count"]
        row["port_open_after"] = port_open(port)
        for p in leftovers["processes"]:
            kill_tree(p["pid"])
        cleanup_temp_dirs()  # 每轮清一次，别让 10 轮的 _MEI 堆起来
    stable = all(r["child_in_job"] and r["listener_in_job"] for r in rows)
    deltas = [r["launcher_to_child_ms"] for r in rows if r["launcher_to_child_ms"] is not None]
    print(
        f"    汇总：{runs} 次里 child_in_job={sum(1 for r in rows if r['child_in_job'])}、"
        f"listener_in_job={sum(1 for r in rows if r['listener_in_job'])}、"
        f"launcher->child 间隔 {min(deltas) if deltas else None}~{max(deltas) if deltas else None}ms、"
        f"关 job 后残留={sum(r['leftovers_after_close'] for r in rows)}"
    )
    report["job_stability"] = {"runs": rows, "stable": stable, "deltas_ms": deltas}


def pid_names(pids: list[int]) -> dict:
    """把 pid 解析成 (名字, 可执行文件) —— 报告里要说清 job 里除了后端还有谁。"""
    if not pids:
        return {}
    joined = ",".join(str(pid) for pid in pids)
    cmd = (
        f"Get-CimInstance Win32_Process | Where-Object {{ {joined} -contains $_.ProcessId }} | "
        "Select-Object ProcessId,Name,ExecutablePath | ConvertTo-Json -Compress"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    rows = data if isinstance(data, list) else [data]
    return {
        int(row["ProcessId"]): {"name": row.get("Name"), "exe": row.get("ExecutablePath")}
        for row in rows
    }


def port_open(port: int, timeout: float = 0.4) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# 本机同时有多个 agent 在起真实后端，固定端口会互相踩。默认给每个实例要一个空闲端口；
# 需要固定时用 --port 指定（Agent A 的分配是 8891，连续实例往后顺延）。
_PINNED_PORT: int | None = None
_PINNED_USED = 0


def free_port() -> int:
    global _PINNED_USED
    if _PINNED_PORT is not None:
        _PINNED_USED += 1
        return _PINNED_PORT + _PINNED_USED - 1
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])

# --------------------------------------------------------------- 拉起/等待 ----


def spawn_backend(exe: Path, work: Path, port: int, *, new_console: bool = True):
    data_dir = work / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    token = work / "session-token.txt"
    if token.exists():
        token.unlink()
    # 每个实例用自己的 TEMP：onefile 会把压缩包解到这里（约 130MB），而进程被 job 杀掉时
    # 不会自己清理 —— 集中放一个目录，实验结束（或每轮结束）一起删，免得把磁盘塞满。
    run_tmp = work / "tmp"
    run_tmp.mkdir(parents=True, exist_ok=True)
    _TEMP_DIRS.append(run_tmp)
    env = {
        **os.environ,
        "TEMP": str(run_tmp),
        "TMP": str(run_tmp),
        "QIO_HOST": "127.0.0.1",
        "QIO_PORT": str(port),
        "QIO_SESSION_TOKEN_FILE": str(token),
        "QIO_DATA_DIR": str(data_dir),
        # 数据库身份基线的默认位置是**用户级注册表**；本会话的进程是 Low 完整性，
        # 写 HKCU 会被拒绝（Access denied），后端会因此在启动自检里崩掉。
        # QIO_DB_BASELINE 是仓库自带的「隔离环境」开关：把基线指到本次实验目录，
        # 自检仍然开着，只是不碰用户级状态。
        "QIO_DB_BASELINE": str(work / "db-baseline.json"),
    }
    flags = 0
    if new_console:
        flags |= CREATE_NEW_CONSOLE
    else:
        flags |= CREATE_NEW_PROCESS_GROUP
    out = (work / "backend.out.log").open("ab")
    err = (work / "backend.err.log").open("ab")
    proc = subprocess.Popen(
        [str(exe)], env=env, stdout=out, stderr=err, cwd=str(work),
        creationflags=flags, close_fds=True,
    )
    return proc, token


def wait_ready(port: int, token: Path, exe: Path, since: float, timeout: float = 90.0) -> dict:
    """等后端真的开始服务（端口可连 + token 文件出现），并记录进程树演化。"""
    deadline = time.time() + timeout
    stages: list[dict] = []
    last_count = -1
    while time.time() < deadline:
        snap = snapshot(exe, since)
        if snap["count"] != last_count:
            stages.append({
                "t": round(time.time() - since, 3),
                "count": snap["count"],
                "pids": [p["pid"] for p in snap["processes"]],
                "ppids": [p["ppid"] for p in snap["processes"]],
            })
            last_count = snap["count"]
        if port_open(port) and token.exists():
            return {"ready": True, "stages": stages, "seconds": round(time.time() - since, 3)}
        time.sleep(0.15)
    return {"ready": False, "stages": stages, "seconds": round(time.time() - since, 3)}


def kill_pid(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30)


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)


def wait_gone(pids: list[int], timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [pid for pid in pids if _alive(pid)]
        if not alive:
            return True
        time.sleep(0.2)
    return False


def _alive(pid: int) -> bool:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
        capture_output=True, text=True, timeout=30,
    )
    return bool((proc.stdout or "").strip())


def describe(snap: dict) -> str:
    if not snap["processes"]:
        return "    （无相关进程）"
    lines = []
    for p in sorted(snap["processes"], key=lambda x: x["created_ts"] or 0):
        lines.append(
            f"    pid={p['pid']} ppid={p['ppid']} created={p['created']} "
            f"exe={Path(p['exe']).name} cmd={p['cmdline'][:80]!r}"
        )
    return "\n".join(lines)


def wait_for_two(exe: Path, since: float, timeout: float = 60.0) -> dict:
    """等 onefile 的 child 出现（同一个 exe 路径的第二个进程）。"""
    deadline = time.time() + timeout
    snap = snapshot(exe, since)
    while time.time() < deadline and snap["count"] < 2:
        time.sleep(0.1)
        snap = snapshot(exe, since)
    return snap

# ----------------------------------------------------------------- cases ----


def case_1_normal_exit(exe: Path, work: Path, report: dict) -> None:
    print("== Case 1：正常退出（对后端控制台发 Ctrl+C）==")
    port = free_port()
    since = time.time()
    proc, token = spawn_backend(exe, work, port)
    ready = wait_ready(port, token, exe, since)
    snap = snapshot(exe, since)
    print(f"    ready={ready['ready']} in {ready['seconds']}s  port={port} open={port_open(port)}")
    print(describe(snap))
    pids = [p["pid"] for p in snap["processes"]]
    launcher = proc.pid
    sent = send_ctrl_c_to_console_of(launcher)
    gone = wait_gone(pids, timeout=45)
    time.sleep(1.0)
    after = snapshot(exe, since)
    print(f"    ctrl-c sent={sent}  全部退出={gone}  端口仍开={port_open(port)}")
    print(describe(after))
    report["case1"] = {
        "ready": ready, "before": snap, "ctrl_c_sent": sent,
        "all_gone": gone, "after": after, "port_open_after": port_open(port),
        "launcher_returncode": proc.poll(),
    }
    for pid in [p["pid"] for p in after["processes"]]:
        kill_tree(pid)


def case_2_kill_launcher(exe: Path, work: Path, report: dict) -> None:
    print("== Case 2：只结束 launcher（等价于壳退出只杀父进程）==")
    port = free_port()
    since = time.time()
    proc, token = spawn_backend(exe, work, port)
    ready = wait_ready(port, token, exe, since)
    snap = snapshot(exe, since)
    print(f"    ready={ready['ready']} in {ready['seconds']}s  port={port} open={port_open(port)}")
    print(describe(snap))
    child_pids = [p["pid"] for p in snap["processes"] if p["pid"] != proc.pid]
    kill_pid(proc.pid)
    time.sleep(2.0)
    after = snapshot(exe, since)
    print("    结束 launcher 后：")
    print(describe(after))
    print(f"    child 仍在={[pid for pid in child_pids if _alive(pid)]} 端口仍开={port_open(port)}")
    report["case2"] = {
        "ready": ready, "before": snap, "killed_pid": proc.pid,
        "after": after, "child_alive": [pid for pid in child_pids if _alive(pid)],
        "port_open_after": port_open(port),
    }
    for pid in [p["pid"] for p in after["processes"]]:
        kill_tree(pid)


def case_3_kill_child(exe: Path, work: Path, report: dict) -> None:
    print("== Case 3：结束 child（真正的服务进程）==")
    port = free_port()
    since = time.time()
    proc, token = spawn_backend(exe, work, port)
    ready = wait_ready(port, token, exe, since)
    snap = snapshot(exe, since)
    print(f"    ready={ready['ready']} in {ready['seconds']}s  port={port} open={port_open(port)}")
    print(describe(snap))
    child = next((p for p in snap["processes"] if p["pid"] != proc.pid), None)
    if child is None:
        print("    没有观察到 child —— 记录事实并结束本 case")
        report["case3"] = {"ready": ready, "before": snap, "child": None}
        kill_tree(proc.pid)
        return
    kill_pid(child["pid"])
    time.sleep(2.0)
    after = snapshot(exe, since)
    print("    结束 child 后：")
    print(describe(after))
    print(f"    launcher 还在={_alive(proc.pid)} launcher returncode={proc.poll()} 端口仍开={port_open(port)}")
    report["case3"] = {
        "ready": ready, "before": snap, "killed_pid": child["pid"],
        "after": after, "launcher_alive": _alive(proc.pid),
        "launcher_returncode": proc.poll(), "port_open_after": port_open(port),
    }
    for pid in [p["pid"] for p in after["processes"]]:
        kill_tree(pid)


def case_4_crash_child(exe: Path, work: Path, report: dict) -> None:
    print("== Case 4：backend crash（硬杀 child，模拟服务进程崩溃）==")
    port = free_port()
    since = time.time()
    proc, token = spawn_backend(exe, work, port)
    ready = wait_ready(port, token, exe, since)
    snap = snapshot(exe, since)
    print(f"    ready={ready['ready']} in {ready['seconds']}s  port={port} open={port_open(port)}")
    print(describe(snap))
    child = next((p for p in snap["processes"] if p["pid"] != proc.pid), None)
    if child is None:
        report["case4"] = {"ready": ready, "before": snap, "child": None}
        kill_tree(proc.pid)
        return
    kill_pid(child["pid"])
    time.sleep(3.0)
    after = snapshot(exe, since)
    print("    child 硬崩后：")
    print(describe(after))
    print(f"    launcher 还在={_alive(proc.pid)} returncode={proc.poll()} 端口仍开={port_open(port)}")
    report["case4"] = {
        "ready": ready, "before": snap, "killed_pid": child["pid"],
        "after": after, "launcher_alive": _alive(proc.pid),
        "launcher_returncode": proc.poll(), "port_open_after": port_open(port),
    }
    for pid in [p["pid"] for p in after["processes"]]:
        kill_tree(pid)


def case_5_rapid_restart(exe: Path, work: Path, report: dict) -> None:
    print("== Case 5：快速重复启动（两个不同端口 + 一个撞同一端口）==")
    since = time.time()
    port_a, port_b = free_port(), free_port()
    proc_a, token_a = spawn_backend(exe, work / "a", port_a)
    proc_b, token_b = spawn_backend(exe, work / "b", port_b)
    ready_a = wait_ready(port_a, token_a, exe, since)
    ready_b = wait_ready(port_b, token_b, exe, since)
    snap = snapshot(exe, since)
    print(f"    a ready={ready_a['ready']} port={port_a} open={port_open(port_a)}")
    print(f"    b ready={ready_b['ready']} port={port_b} open={port_open(port_b)}")
    print(describe(snap))
    proc_c, token_c = spawn_backend(exe, work / "c", port_a)
    ready_c = wait_ready(port_a, token_c, exe, since, timeout=25)
    time.sleep(2.0)
    snap2 = snapshot(exe, since)
    print(f"    c（撞端口 {port_a}）ready={ready_c['ready']} 进程数={snap2['count']}")
    print(describe(snap2))
    report["case5"] = {
        "a": {"ready": ready_a, "port": port_a}, "b": {"ready": ready_b, "port": port_b},
        "c_same_port": {"ready": ready_c, "port": port_a},
        "snapshot": snap, "snapshot_after_c": snap2,
        "port_a_open": port_open(port_a), "port_b_open": port_open(port_b),
    }
    for proc in (proc_a, proc_b, proc_c):
        kill_tree(proc.pid)
    for pid in [p["pid"] for p in snapshot(exe, since)["processes"]]:
        kill_tree(pid)


def _try(label: str, fn) -> str:
    """执行一个文件操作，返回 ""（成功）或异常描述。"""
    try:
        fn()
        return ""
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def case_6_replace_executable(exe: Path, work: Path, report: dict) -> None:
    """运行中/结束后各试三种操作：改名、删除、**原地写入**。

    安装器的 Can't write 来自第三种：Windows 的映像文件是以 FILE_SHARE_DELETE
    打开的（改名/删除可以），但**不允许再以写方式打开**（原地覆盖会被拒）。
    所以必须分三种操作分别验证，不能只测一种就说「文件没被锁」。
    """
    print("== Case 6：运行中替换/删除 executable（决定更新与卸载能不能成功）==")
    lock_dir = work / "lock-test"
    lock_dir.mkdir(parents=True, exist_ok=True)
    results: dict = {}
    for label, op in (("rename", "rename"), ("delete", "delete"), ("overwrite", "overwrite")):
        target = lock_dir / f"{label}-{exe.name}"
        shutil.copy2(exe, target)
        port = free_port()
        since = time.time()
        proc, token = spawn_backend(target, work / f"lock-{label}", port)
        ready = wait_ready(port, token, target, since)
        snap = snapshot(target, since)
        print(f"  [{label}] ready={ready['ready']} in {ready['seconds']}s 进程数={snap['count']} port={port}")
        print(describe(snap))
        if op == "rename":
            dest = lock_dir / (target.name + ".moved")
            running_err = _try(label, lambda: os.replace(target, dest))
        elif op == "delete":
            running_err = _try(label, lambda: os.remove(target))
        else:
            running_err = _try(label, lambda: shutil.copyfile(exe, target))
        print(f"    运行中 {op} -> {running_err or '成功'}")
        kill_tree(proc.pid)
        wait_gone([p["pid"] for p in snap["processes"]], timeout=30)
        time.sleep(0.8)
        # 结束后再试同一个操作（换一个干净副本，保证这一测只反映「文件是否还被锁」）
        target2 = lock_dir / f"after-{label}-{exe.name}"
        shutil.copy2(exe, target2)
        if op == "rename":
            after_err = _try(label, lambda: os.replace(target2, lock_dir / (target2.name + ".moved")))
        elif op == "delete":
            after_err = _try(label, lambda: os.remove(target2))
        else:
            after_err = _try(label, lambda: shutil.copyfile(exe, target2))
        print(f"    结束后 {op} -> {after_err or '成功'}")
        results[label] = {
            "ready": ready, "processes": snap, "running": running_err or "success",
            "after_kill": after_err or "success",
            "target_exists_after": target.exists(),
        }
    report["case6"] = results

# ------------------------------------------------------- job object 实验 ----


def job_experiment(exe: Path, work: Path, report: dict, *, assign_immediately: bool) -> None:
    label = "立即 assign（与壳同序）" if assign_immediately else "等 child 出现后再 assign（危险顺序）"
    print(f"== Job Object 实验：{label} ==")
    port = free_port()
    since = time.time()
    job = job_create()
    proc, token = spawn_backend(exe, work, port)
    assigned = job_assign(job, proc.pid) if assign_immediately else None
    if not assign_immediately:
        snap0 = wait_for_two(exe, since)
        print(f"    child 已出现（进程数={snap0['count']}），现在才 assign")
        assigned = job_assign(job, proc.pid)
    ready = wait_ready(port, token, exe, since)
    snap = wait_for_two(exe, since)
    pids = job_pids(job)
    print(f"    assign 成功={assigned} ready={ready['ready']} 进程数={snap['count']} job 内 pid={pids}")
    print(f"    job 内的进程明细：{json.dumps(pid_names(pids), ensure_ascii=False)}")
    print(describe(snap))
    child = next((p for p in snap["processes"] if p["pid"] != proc.pid), None)
    child_in_job = bool(child and child["pid"] in pids)
    print(f"    child pid={child['pid'] if child else None} 在 job 里={child_in_job}")
    kernel32.CloseHandle(job)
    time.sleep(3.0)
    after = snapshot(exe, since)
    print("    关闭 job 句柄后：")
    print(describe(after))
    print(f"    launcher 还在={_alive(proc.pid)} 端口仍开={port_open(port)}")
    key = "job_immediate" if assign_immediately else "job_late"
    report[key] = {
        "assign_ok": assigned, "ready": ready, "before": snap, "job_pids": pids,
        "child_pid": child["pid"] if child else None, "child_in_job": child_in_job,
        "after_close": after, "launcher_alive": _alive(proc.pid),
        "port_open_after": port_open(port),
    }
    for pid in [p["pid"] for p in after["processes"]]:
        kill_tree(pid)


def nested_job_experiment(exe: Path, work: Path, report: dict) -> None:
    """宿主已经把壳放进一个 job 时（CI runner / 某些启动器），再收后端还成不成立。

    模拟方式：先用 J1 收住 launcher（等价于「壳继承了宿主的 job」），再用 J2 收它
    —— 这正是 AssignProcessToJobObject 的经典坑（嵌套 job）。J1 不带 KILL_ON_JOB_CLOSE，
    免得实验结束把本脚本自己带走。
    """
    print("== Job Object 实验：宿主已有 job（嵌套）==")
    host_job = job_create(kill_on_close=False)
    port = free_port()
    since = time.time()
    proc, token = spawn_backend(exe, work, port)
    in_host = job_assign(host_job, proc.pid)
    own_job = job_create()
    nested = job_assign(own_job, proc.pid)
    ready = wait_ready(port, token, exe, since)
    snap = wait_for_two(exe, since)
    pids = job_pids(own_job)
    child = next((p for p in snap["processes"] if p["pid"] != proc.pid), None)
    child_in_job = bool(child and child["pid"] in pids)
    print(f"    先放进宿主 job={in_host} 再放进自己的 job={nested} ready={ready['ready']}")
    print(f"    进程数={snap['count']} 自己 job 内 pid={pids} child 在 job 里={child_in_job}")
    print(describe(snap))
    kernel32.CloseHandle(own_job)
    time.sleep(3.0)
    after = snapshot(exe, since)
    print("    关闭自己的 job 句柄后：")
    print(describe(after))
    print(f"    launcher 还在={_alive(proc.pid)} 端口仍开={port_open(port)}")
    kernel32.CloseHandle(host_job)
    report["job_nested"] = {
        "assigned_to_host_job": in_host, "assigned_to_own_job": nested,
        "ready": ready, "before": snap, "job_pids": pids,
        "child_pid": child["pid"] if child else None, "child_in_job": child_in_job,
        "after_close": after, "launcher_alive": _alive(proc.pid),
        "port_open_after": port_open(port),
    }
    for pid in [p["pid"] for p in after["processes"]]:
        kill_tree(pid)


def shell_death_experiment(exe: Path, work: Path, report: dict) -> None:
    """壳被强杀（没有 RunEvent 清理）时，后端应随 job 句柄消失被系统带走。

    做法：另起一个 helper 进程，由它建 job、拉后端、assign，然后**立刻退出**
    （句柄随进程关闭）—— 等价于壳被 TerminateProcess。本进程只负责看结果。
    """
    print("== Job Object 实验：壳被强杀（句柄随进程消失）==")
    port = free_port()
    since = time.time()
    helper = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--spawn-and-die",
         "--exe", str(exe), "--work", str(work), "--port", str(port)],
        capture_output=True, text=True, timeout=180,
    )
    print(f"    helper rc={helper.returncode} out={helper.stdout.strip()[:300]}")
    if helper.stderr.strip():
        print(f"    helper err={helper.stderr.strip()[:300]}")
    time.sleep(4.0)
    after = snapshot(exe, since)
    print("    helper 退出后：")
    print(describe(after))
    print(f"    端口仍开={port_open(port)} 残留进程数={after['count']}")
    report["shell_death"] = {
        "helper_rc": helper.returncode,
        "helper_stdout": helper.stdout.strip()[:500],
        "after": after, "port_open_after": port_open(port), "leftovers": after["count"],
    }
    for pid in [p["pid"] for p in after["processes"]]:
        kill_tree(pid)


def spawn_and_die(exe: Path, work: Path, port: int) -> int:
    """helper：建 job → 拉后端 → assign → 立刻退出（不清理）。"""
    work.mkdir(parents=True, exist_ok=True)
    job = job_create()
    proc, _token = spawn_backend(exe, work, port)
    assigned = job_assign(job, proc.pid)
    print(json.dumps({"launcher": proc.pid, "assigned": assigned}))
    sys.stdout.flush()
    # 故意不 kill、不 CloseHandle：进程退出时句柄由系统关闭 = 壳被强杀
    os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 onefile backend 的进程模型与退出行为（Windows）")
    parser.add_argument("--exe", required=True, help="冻结后的 qio-backend.exe")
    parser.add_argument("--work", default="", help="实验工作目录（默认放在 exe 同级）")
    parser.add_argument("--case", default="all", help="all / 1 / 2 / 3 / 4 / 5 / 6 / job")
    parser.add_argument("--json", default="", help="把结构化结果写到这个文件")
    parser.add_argument("--spawn-and-die", action="store_true", help="内部用：拉起后立刻退出（模拟壳被强杀）")
    parser.add_argument("--port", type=int, default=0, help="内部用：指定端口")
    args = parser.parse_args()
    if not IS_WINDOWS:
        print("这个验证只在 Windows 上有意义")
        return 2
    exe = Path(args.exe).resolve()
    if not exe.is_file():
        print(f"找不到冻结产物：{exe}")
        return 2
    if args.port:
        global _PINNED_PORT
        _PINNED_PORT = args.port
    if args.spawn_and_die:
        return spawn_and_die(exe, Path(args.work).resolve(), args.port)
    work = Path(args.work).resolve() if args.work else exe.parent / "p3a-process-model"
    work.mkdir(parents=True, exist_ok=True)
    report = {
        "exe": str(exe),
        "exe_size": exe.stat().st_size,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "harness_in_a_job": current_process_is_in_a_job(),
    }
    print(f"# 冻结产物：{exe}（{exe.stat().st_size / 1024 / 1024:.1f} MB）")
    print(f"# 本脚本进程是否已在某个 job 里：{report['harness_in_a_job']}")
    print()

    def want(name: str) -> bool:
        return args.case in ("all", name)

    if want("1"):
        case_1_normal_exit(exe, work / "case1", report)
        print()
    if want("2"):
        case_2_kill_launcher(exe, work / "case2", report)
        print()
    if want("3"):
        case_3_kill_child(exe, work / "case3", report)
        print()
    if want("4"):
        case_4_crash_child(exe, work / "case4", report)
        print()
    if want("5"):
        case_5_rapid_restart(exe, work / "case5", report)
        print()
    if want("6"):
        case_6_replace_executable(exe, work / "case6", report)
        print()
    if want("job"):
        job_experiment(exe, work / "job-immediate", report, assign_immediately=True)
        print()
        job_experiment(exe, work / "job-late", report, assign_immediately=False)
        print()
    if args.case in ("all", "job", "nested"):
        nested_job_experiment(exe, work / "job-nested", report)
        print()
    if args.case in ("all", "tree"):
        case_tree(exe, work / "tree", report)
        print()
    if args.case in ("all", "stability"):
        job_stability_experiment(exe, work / "stability", report, runs=10)
        print()
    if args.case in ("all", "job", "die"):
        shell_death_experiment(exe, work / "job-die", report)
        print()

    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结构化结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())