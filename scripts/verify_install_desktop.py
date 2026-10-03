#!/usr/bin/env python
"""第 1 步真机桌面验收：安装 → 启动 → lease 独立核对 → 正常退出 → 保持开着直接卸载 → lease 写失败。

用户要求的四步（task-5）：
  1. 安装后启动 QIO → sidecar.lease.json 出现，且内容对应**本次安装与实际运行的进程**：
     install_dir = 本次安装目录；shell/backend 的 pid + 创建时间 FILETIME + exe 路径
     由本脚本用**独立手段**核对（Toolhelp 快照 + OpenProcess / GetProcessTimes /
     QueryFullProcessImageNameW，直连 kernel32），不读 lease 自证。
  2. 正常退出（给外壳主窗口 PostMessage(WM_CLOSE)，等价于点窗口的关闭按钮）→ lease 被删。
  3. 保持 QIO 开着直接卸载（uninstall.exe /S）→ 本次安装的后台进程被结束、安装目录被清理、
     用户数据目录内容不变；诱饵进程与第二份安装的进程按 **pid + 创建时间** 核对，一个都没被动过。
  4. lease 写失败（把 <install_dir>\sidecar.lease.json 预建成**目录**，让真实 write_lease 的
     原子替换必然失败）→ 原因可读 + 已启动的后台被收掉；不允许「后台还在跑但无法确认归属」。

执行顺序与编号不同，是有意的（每一步的前提不能被上一步破坏）：
    preflight → install → lease(S1) → exit(S2) → leasefail(S4) → uninstall(S3)
  * S4 自己要装第三份，而安装段的 NSIS 模板仍有一处按**映像名**查 qio.exe 的检查
    （构建期补丁只替换了卸载段那一处），所以 S4 必须在「没有别的 QIO 在跑」时装；
  * S3 需要第二份安装 + 诱饵常驻，放最后跑，它的断言不会被后续安装动作干扰。

用法：
  # 完整四步（需要 Lead 给的安装包）
  python scripts/verify_install_desktop.py --installer <QIO_x.y.z_x64-setup.exe> \
      --sha256 <期望 sha256> [--commit <被测 commit>]

  # 骨架自测（不装包，直接跑 release 外壳；只跑第 1/2 步）
  python scripts/verify_install_desktop.py \
      --shell-exe frontend/src-tauri/target/release/qio.exe --stages preflight,lease,exit

纪律（脚本自身保证）：
  * 清理进程只按 pid（taskkill /F /T /PID，一条一条来）；不接受按映像名结束进程的写法。
  * QIO_DATA_DIR 一律指到 work-dir，绝不写 %APPDATA%\qio 里的真实用户数据。
  * 静默安装前先检查有没有**别人的** QIO 在跑：安装段的模板检查会按名字杀 qio.exe，
    有就拒绝继续（记 FAIL），不拿别人的进程冒险。
  * 每一个断言都带原始证据（work-dir/evidence/ 下的文件）；summary.json 每条带 id/state/detail。
  * 没验到的记 NOT VERIFIED / SKIP，不把「脚本跑通」说成「产品验过」。

退出码：0 = 没有 FAIL；1 = 有 FAIL；2 = 用法错误。
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

PASS, FAIL, WARN, SKIP, NOT_VERIFIED = "PASS", "FAIL", "WARN", "SKIP", "NOT VERIFIED"

ALL_STAGES = ["preflight", "install", "lease", "exit", "leasefail", "windowfail", "uninstall"]
EXEC_ORDER = ["preflight", "install", "lease", "exit", "leasefail", "windowfail", "uninstall"]
STANDALONE_STAGES = ["preflight", "lease", "exit"]

LEASE_NAME = "sidecar.lease.json"
QIO_IMAGE_NAMES = {"qio.exe", "qio-backend.exe", "qio-uninstall-helper.exe"}
SECRET_WORDS = ("token", "secret", "password", "credential", "private")
SECRET_EXACT = ("key", "api_key", "apikey", "authorization")
WM_CLOSE = 0x0010


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 记录与证据

class Recorder:
    """每条断言一个 id / state / detail，原始输出落盘到 work-dir/evidence/。"""

    def __init__(self, work: Path):
        self.work = work
        self.ev_dir = work / "evidence"
        self.ev_dir.mkdir(parents=True, exist_ok=True)
        self.rows: list[dict] = []
        self.seq = 0
        self.started = time.time()
        self.meta: dict = {}

    def evidence(self, name: str, text: str) -> str:
        self.seq += 1
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name))[:70].strip("-") or "evidence"
        target = self.ev_dir / ("%03d-%s.txt" % (self.seq, safe))
        target.write_text(text if text else "(空)\n", encoding="utf-8", errors="replace")
        return str(target.relative_to(self.work))

    def copy_evidence(self, name: str, source: Path) -> str:
        self.seq += 1
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name))[:70].strip("-") or "copy"
        target = self.ev_dir / ("%03d-%s" % (self.seq, safe))
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            target.write_text("复制失败：%r\n" % (exc,), encoding="utf-8")
        return str(target.relative_to(self.work))

    def add(self, rid: str, title: str, state: str, detail, evidence=None, extra=None) -> dict:
        row = {"id": rid, "title": title, "state": state, "detail": str(detail)}
        if evidence:
            row["evidence"] = evidence if isinstance(evidence, list) else [evidence]
        if extra:
            row["extra"] = extra
        self.rows.append(row)
        log("[%s] %-7s %s :: %s" % (state, rid, title, str(detail)[:300]))
        return row

    def counts(self) -> dict:
        out = {state: 0 for state in (PASS, FAIL, WARN, SKIP, NOT_VERIFIED)}
        for row in self.rows:
            out[row["state"]] = out.get(row["state"], 0) + 1
        return out


# ---------------------------------------------------------------- Windows 进程事实（独立枚举）
#
# 为什么直连 kernel32 而不是 PowerShell/WMI：
#   * 比对要求 **100ns 精度**的进程创建时间（UTC FILETIME）。Win32_Process.CreationDate
#     经 PowerShell 序列化会丢精度，拿它只能得出"大概一样"，不足以支撑这条硬断言；
#   * GetProcessTimes 的 FILETIME 与产品侧 ownership.rs 读的是同一个来源（同一把尺子）。
# 失败一律返回 None（进程不存在 / 查不到），调用方必须把它记成 FAIL，不能当"通过"。

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
STILL_ACTIVE = 259


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.GetProcessTimes.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong * 2), ctypes.POINTER(ctypes.c_ulong * 2),
    ctypes.POINTER(ctypes.c_ulong * 2), ctypes.POINTER(ctypes.c_ulong * 2)]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
_kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
_kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
_kernel32.GetCurrentProcess.restype = ctypes.c_void_p
_kernel32.ProcessIdToSessionId.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]


def _process_times_and_path(pid: int):
    """一个 pid 的 (映像路径, 创建时间 FILETIME)。已退出/查不到 → None。

    **必须**查 GetExitCodeProcess：只要还有句柄开着（我们自己的 Popen 就开着一个），
    已退出的进程在内核里仍可 OpenProcess、创建时间也还查得到 —— 只按"OpenProcess 成功"
    判存活，会把刚退出的进程记成"还在跑"。
    """
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not handle:
        return None
    try:
        exit_code = ctypes.c_ulong(0)
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != STILL_ACTIVE:
            return None
        creation = (ctypes.c_ulong * 2)()
        exit_time = (ctypes.c_ulong * 2)()
        kernel = (ctypes.c_ulong * 2)()
        user = (ctypes.c_ulong * 2)()
        if not _kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_time),
                ctypes.byref(kernel), ctypes.byref(user)):
            return None
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))
        path = ""
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            path = buffer.value
        filetime = (int(creation[1]) << 32) | int(creation[0])
        return {"pid": int(pid), "exe": path, "created_filetime": str(filetime)}
    finally:
        _kernel32.CloseHandle(handle)


def snapshot_pids() -> list:
    """Toolhelp 快照：所有进程的 (pid, ppid, 映像名)。"""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out = []
    try:
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            out.append({"pid": int(entry.th32ProcessID), "ppid": int(entry.th32ParentProcessID),
                        "name": entry.szExeFile})
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)
    return out


def sample_process_identity(pid: int, timeout: float = 6.0, interval: float = 0.25):
    """等一小会再读进程身份，返回 (identity 或 None, 实际观察秒数)。

    为什么给宽限：lease 是外壳**刚 spawn 完**就写的，onefile 的 sidecar 先起 launcher、再解包出
    child；刚写完的一瞬间读不到不代表没在跑。宽限只影响"给它一点时间被看见"，
    **不会**把"一直没出现"记成通过。
    """
    started = time.time()
    while True:
        info = _process_times_and_path(pid)
        if info is not None:
            return info, round(time.time() - started, 2)
        if time.time() - started >= timeout:
            return None, round(time.time() - started, 2)
        time.sleep(interval)


def process_identity(pid: int):
    """进程身份 = pid + ppid + 映像路径 + 创建时间（抗 PID 复用的四件套）。"""
    info = _process_times_and_path(pid)
    if info is None:
        return None
    ppid = next((item["ppid"] for item in snapshot_pids() if item["pid"] == int(pid)), None)
    return dict(info, ppid=ppid)


def describe_processes(items) -> str:
    return ", ".join("%s(%s, ft=%s, ppid=%s)" % (item["pid"], item.get("exe") or "?", item.get("created_filetime"), item.get("ppid"))
                     for item in items) or "无"


def processes_in_dir(path) -> list:
    """目录里的活动进程 —— 按**映像路径**匹配，不按映像名（同机可能有多份安装/别人的进程）。"""
    prefix = os.path.normcase(str(Path(path).resolve())) + os.sep
    found = []
    for entry in snapshot_pids():
        info = _process_times_and_path(entry["pid"])
        if not info or not info["exe"]:
            continue
        if os.path.normcase(str(Path(info["exe"]).resolve())).startswith(prefix):
            found.append(dict(entry, **info))
    return sorted(found, key=lambda item: item["pid"])


def processes_by_exe(exe) -> list:
    want = os.path.normcase(str(Path(exe).resolve()))
    return [info for info in (dict(_process_times_and_path(e["pid"]) or {}, name=e["name"])
                              for e in snapshot_pids())
            if info.get("exe") and os.path.normcase(str(Path(info["exe"]).resolve())) == want]


def qio_named_processes() -> list:
    """当前会话里映像名是 qio.exe / qio-backend.exe / qio-uninstall-helper.exe 的进程。

    **只用来做观察与安全检查，绝不用来结束进程**：拿它去核对"谁死了"，不拿它当收进程的依据。
    """
    out = []
    for entry in snapshot_pids():
        if entry["name"].lower() not in QIO_IMAGE_NAMES:
            continue
        info = _process_times_and_path(entry["pid"])
        if info:
            out.append(dict(entry, **info))
    return sorted(out, key=lambda item: item["pid"])


def parent_chain(pid: int, limit: int = 12) -> list:
    chain = []
    current = int(pid)
    snapshot = {item["pid"]: item["ppid"] for item in snapshot_pids()}
    for _ in range(limit):
        parent = snapshot.get(current)
        if not parent or parent == current:
            break
        chain.append(parent)
        current = parent
    return chain


def wait_identity_gone(pids, timeout: float = 30.0) -> list:
    """等这些 pid 真的消失；返回仍在的 pid（空列表 = 确认都退了）。"""
    wanted = [int(pid) for pid in pids]
    deadline = time.time() + timeout
    while True:
        left = [pid for pid in wanted if process_identity(pid) is not None]
        if not left or time.time() >= deadline:
            return left
        time.sleep(0.5)


def is_under(path, directory) -> bool:
    try:
        prefix = os.path.normcase(str(Path(directory).resolve())) + os.sep
        return os.path.normcase(str(Path(path).resolve())).startswith(prefix)
    except OSError:
        return False


_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
_user32.EnumWindows.argtypes = [_EnumWindowsProc, ctypes.c_void_p]
_user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
_user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
_user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
_user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_long * 4)]
_user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]


def top_level_windows(pid: int) -> list:
    """pid 拥有的顶层窗口（标题 / 类名 / 可见性 / 尺寸）—— 证明 lease 里的壳就是那个窗口进程。"""
    found = []

    def callback(hwnd, _lparam):
        owner = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) != int(pid):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 2)
        _user32.GetWindowTextW(hwnd, title, length + 2)
        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        rect = (ctypes.c_long * 4)()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append({
            "hwnd": int(hwnd) if hwnd else 0,
            "title": title.value,
            "class": cls.value,
            "visible": bool(_user32.IsWindowVisible(hwnd)),
            "rect": [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])],
        })
        return True

    _user32.EnumWindows(_EnumWindowsProc(callback), None)
    return found


def close_main_window(pid: int):
    """给外壳主窗口发 WM_CLOSE（等价于点关闭按钮）。返回 (是否发出, 原始证据文本)。"""
    windows = top_level_windows(pid)
    lines = ["pid=%s 的顶层窗口：" % pid]
    for item in windows:
        lines.append("  hwnd=%s class=%r title=%r visible=%s rect=%s"
                     % (item["hwnd"], item["class"], item["title"], item["visible"], item["rect"]))
    if not windows:
        lines.append("  （一个顶层窗口都没有）")
        return False, "\n".join(lines)
    # **只有带标题的可见顶层窗口才是应用主窗口**：tao 的 "Tao Thread Event Target"、IME 窗口
    # 也是可见顶层窗口，但给它们发 WM_CLOSE 不会让应用退出（实测：会得出"发了 WM_CLOSE 却
    # 没退出"的假结论）。找不到主窗口就如实说"没有正常退出路径"，不硬发。
    titled = [item for item in windows if item["visible"] and item["title"]]
    if not titled:
        lines.append("  没有『可见 + 有标题』的顶层窗口 → 不发 WM_CLOSE（没有正常退出路径）")
        return False, "\n".join(lines)
    target = titled[0]
    posted = bool(_user32.PostMessageW(target["hwnd"], WM_CLOSE, None, None))
    lines.append("PostMessage(WM_CLOSE) → hwnd=%s posted=%s" % (target["hwnd"], posted))
    return posted, "\n".join(lines)


class WindowWatch(threading.Thread):
    """从**进程一启动**就采样顶层窗口，而不是等 lease 出现之后才看。

    本机实测（2026-10-03）：外壳的窗口只活 ~8s（后端起不来时前端会把窗口关掉），
    而 lease 要 8.2s 才写出来 —— 等 lease 再查窗口，正好错过。所以窗口历史必须
    从 CreateProcess 那一刻开始记。
    """

    def __init__(self, pid: int, interval: float = 0.25):
        super().__init__(daemon=True)
        self.pid = int(pid)
        self.interval = interval
        self.samples: list = []
        self.first_seen = None
        self.last_seen = None
        self.last_windows: list = []
        # **不能**叫 self._stop —— threading.Thread 自己有个 _stop() 方法，覆盖它会让 join()
        # 抛 TypeError: 'Event' object is not callable（实测踩到）。
        self._stop_event = threading.Event()

    def run(self):
        t0 = time.time()
        while not self._stop_event.is_set():
            try:
                wins = [w for w in top_level_windows(self.pid) if w["visible"] and w["title"]]
            except OSError:
                wins = []
            now = round(time.time() - t0, 2)
            self.samples.append((now, len(wins), wins[0]["title"] if wins else ""))
            self.last_windows = wins
            if wins:
                if self.first_seen is None:
                    self.first_seen = now
                self.last_seen = now
            self._stop_event.wait(self.interval)

    def stop(self) -> dict:
        self._stop_event.set()
        self.join(timeout=5)
        return {"pid": self.pid, "first_seen": self.first_seen, "last_seen": self.last_seen,
                "alive_at_stop": bool(self.last_windows), "last_windows": self.last_windows,
                "samples": self.samples}


class EarlyCloseWatch(threading.Thread):
    """窗口还活着的时候就把 WM_CLOSE 发出去（等价于用户点关闭按钮）。

    为什么不能等第 1 步的断言跑完再发：本机窗口只活 ~9s，而 S1 的深度检查要 6s+ ——
    等完窗口已经没了（2026-10-03 实测）。这个线程与 S1 **并行**：一旦
    「窗口可见且有标题」且「本实例的 lease 已写出（shell.pid = 我们启动的 pid）」
    同时成立，立刻 PostMessage(WM_CLOSE)，并记录发出时刻与当时的窗口 ——
    「第 2 步是不是在窗口存活期内发的」本身成为一条可核对的证据。
    """

    def __init__(self, pid: int, install_dir, interval: float = 0.1):
        super().__init__(daemon=True)
        self.pid = int(pid)
        self.install_dir = Path(install_dir)
        self.interval = interval
        self.window_seen_at = None
        self.lease_seen_at = None
        self.posted_at = None
        self.posted = False
        self.posted_hwnd = None
        self.posted_windows = []
        self._stop_event = threading.Event()

    def run(self):
        t0 = time.time()
        while not self._stop_event.is_set():
            try:
                wins = [w for w in top_level_windows(self.pid) if w["visible"] and w["title"]]
            except OSError:
                wins = []
            now = round(time.time() - t0, 2)
            if wins and self.window_seen_at is None:
                self.window_seen_at = now
            lease, _raw, _err = read_lease(self.install_dir)
            lease_ok = bool(lease) and int((lease.get("shell") or {}).get("pid") or 0) == self.pid
            if lease_ok and self.lease_seen_at is None:
                self.lease_seen_at = now
            if lease_ok and wins and self.posted_at is None:
                self.posted = bool(_user32.PostMessageW(wins[0]["hwnd"], WM_CLOSE, None, None))
                self.posted_at = now
                self.posted_hwnd = wins[0]["hwnd"]
                self.posted_windows = wins
            self._stop_event.wait(self.interval)

    def stop(self) -> dict:
        self._stop_event.set()
        self.join(timeout=5)
        return {"pid": self.pid, "window_seen_at": self.window_seen_at, "lease_seen_at": self.lease_seen_at,
                "posted_at": self.posted_at, "posted": self.posted, "posted_hwnd": self.posted_hwnd,
                "posted_windows": self.posted_windows}


def kill_pid_tree(pid: int) -> tuple:
    """按 **pid** 结束进程树（绝不用 /IM，绝不按映像名）。"""
    proc = subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                          capture_output=True, text=True, errors="replace", timeout=60)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------- 文件事实

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def dir_manifest(path: Path, hash_limit: int = 32 * 1024 * 1024) -> dict:
    """目录内容的指纹：每个文件的 (相对路径, 大小, sha256)。卸载前后必须逐字节一致。"""
    path = Path(path)
    if not path.exists():
        return {"exists": False, "count": 0, "digest": None, "files": {}}
    files = {}
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        try:
            size = item.stat().st_size
        except OSError:
            continue
        rel = str(item.relative_to(path)).replace("\\", "/")
        files[rel] = {"size": size,
                      "sha256": sha256_of(item) if size <= hash_limit else None}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"exists": True, "count": len(files), "digest": digest, "files": files}


def read_lease(install_dir) -> tuple:
    """读 lease 原文。返回 (解析后的 dict 或 None, 原文, 读取错误或 "")。"""
    path = Path(install_dir) / LEASE_NAME
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, "", repr(exc)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, raw, "JSON 解析失败：%r" % (exc,)
    return (data if isinstance(data, dict) else None), raw, ""


def lease_secret_hits(raw: str, extra_values=()) -> list:
    """lease 原文里有没有密钥/令牌（只看**有非空值**的敏感字段名 + 令牌值 + sk- 形态）。"""
    hits = []
    for value in extra_values:
        if value and str(value) in raw:
            hits.append("lease 原文里出现了会话令牌的值")
    for match in re.finditer(r'"([^"]+)"\s*:\s*("(?:[^"\\]|\\.)*"|[^,}\s]+)', raw):
        name = match.group(1).strip().lower()
        value = match.group(2).strip().strip('"').strip().lower()
        if not value or value in ("null", "false", "0", "none"):
            continue
        if any(word in name for word in SECRET_WORDS) or name in SECRET_EXACT:
            hits.append("字段 %s 有非空值" % name)
    if re.search(r"sk-[A-Za-z0-9]{4,}", raw):
        hits.append("出现 sk- 形态的密钥原文")
    return sorted(set(hits))


# ---------------------------------------------------------------- 环境事实

def host_facts() -> dict:
    facts = {
        "python": sys.version.split()[0],
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
        "temp": os.environ.get("TEMP"),
        "appdata": os.environ.get("APPDATA"),
        "localappdata": os.environ.get("LOCALAPPDATA"),
        "session_id": None,
        "integrity": "unknown",
        "temp_writable": False,
    }
    try:
        session = ctypes.c_ulong(0)
        if _kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            facts["session_id"] = int(session.value)
    except OSError:
        pass
    try:
        probe = Path(os.environ.get("TEMP") or ".") / ("qio-verify-probe-%d.txt" % os.getpid())
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        facts["temp_writable"] = True
    except OSError:
        facts["temp_writable"] = False
    try:
        facts["integrity"] = _integrity_level()
    except OSError:
        pass
    return facts


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


def _integrity_level() -> str:
    TOKEN_QUERY = 0x0008
    TOKEN_INTEGRITY_LEVEL = 25
    _advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                              ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    _advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    _advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    _advapi32.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_ulong)
    _advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    token = ctypes.c_void_p()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return "unknown"
    try:
        size = ctypes.c_ulong(0)
        _advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        if not size.value:
            return "unknown"
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer, size.value, ctypes.byref(size)):
            return "unknown"
        label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        count = _advapi32.GetSidSubAuthorityCount(label.Label.Sid).contents.value
        rid = _advapi32.GetSidSubAuthority(label.Label.Sid, count - 1).contents.value
        table = {0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
                 0x2100: "Medium Plus", 0x3000: "High", 0x4000: "System"}
        return table.get(int(rid), hex(int(rid)))
    finally:
        _kernel32.CloseHandle(token)


# ---------------------------------------------------------------- 运行时上下文

class Ctx:
    def __init__(self, args, rec: Recorder):
        self.args = args
        self.rec = rec
        self.work = Path(args.work_dir).resolve()
        self.shells: dict = {}
        self.window_watches: dict = {}
        self.tracked_pids: list = []
        self.data1 = (self.work / "data").resolve()
        self.data2 = (self.work / "data2").resolve()
        self.data3 = (self.work / "data3").resolve()
        self.data4 = (self.work / "data4").resolve()
        self.decoy_dir = (self.work / "decoy").resolve()
        self.installer = Path(args.installer).resolve() if args.installer else None
        self.standalone = bool(args.shell_exe) and not args.installer
        if self.standalone:
            self.shell_exe = Path(args.shell_exe).resolve()
            self.install1 = Path(args.install_dir).resolve() if args.install_dir else self.shell_exe.parent
        else:
            self.shell_exe = None
            self.install1 = Path(args.install_dir).resolve() if args.install_dir else (self.work / "install")
        self.install2 = Path(args.install_dir2).resolve() if args.install_dir2 else (self.work / "install2")
        self.install3 = Path(args.install_dir3).resolve() if args.install_dir3 else (self.work / "install3")

    # -- 命令执行（留原始证据） -------------------------------------------------
    def run(self, cmd, *, tag: str, timeout: int = 600, env=None, cwd=None) -> tuple:
        shown = cmd if isinstance(cmd, str) else subprocess.list2cmdline(cmd)
        log("  $ " + shown)
        started = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                                  timeout=timeout, env=env, cwd=cwd)
            out = (proc.stdout or "") + (proc.stderr or "")
            code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            out = "TIMEOUT after %ss\n%s" % (timeout, (exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
            code = -1
        except OSError as exc:
            out, code = "OSError: %r" % (exc,), -2
        self.rec.evidence(tag, "命令: %s\n用时: %.2fs\nexit=%s\n\n%s" % (shown, time.time() - started, code, out))
        return code, out

    # -- 外壳启动 -------------------------------------------------------------
    def launch_shell(self, exe: Path, data_dir: Path, tag: str, extra_env=None):
        data_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["QIO_DATA_DIR"] = str(data_dir)
        # 无人值守开关（task-4 契约）：启动失败时不弹模态错误框 —— 模态框会把进程钉住，
        # 自动化就会卡在那里等一个没人点的按钮。成功路径不受它影响。
        env["QIO_STARTUP_ERROR_DIALOG"] = "0"
        for pair in getattr(self.args, "shell_env", []) or []:
            if "=" in pair:
                key, value = pair.split("=", 1)
                env[key.strip()] = value
        if extra_env:
            env.update(extra_env)
        logfile = self.rec.ev_dir / ("shell-%s.log" % tag)
        fh = logfile.open("wb")
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                                stdout=fh, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.shells[tag] = proc
        self.tracked_pids.append((proc.pid, "shell:%s" % tag))
        watch = WindowWatch(proc.pid)
        watch.start()
        self.window_watches[tag] = watch
        log("  $ [shell %s] %s（pid=%s，QIO_DATA_DIR=%s）" % (tag, exe, proc.pid, data_dir))
        return proc, logfile

    def tail(self, path: Path, limit: int = 2000) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def wait_lease_for_pid(self, install_dir, pid: int, timeout: float, proc=None) -> tuple:
        """等**我们自己启动的那个 pid** 的 lease 出现，返回 (lease, 原文, 等待秒数)。

        判据必须是 lease.shell.pid == 我们启动的 pid：安装目录里可能躺着上一次运行留下的 lease
        （外壳被强杀时按设计不删），只判「文件存在」会把上一份记录当成自己的记录 ——
        本机骨架自测第一次就踩到了：读到了 5.7 分钟前上一轮留下的 lease。
        """
        deadline = time.time() + timeout
        started = time.time()
        last = ""
        while True:
            lease, raw, _err = read_lease(install_dir)
            if raw:
                last = raw
            if lease is not None and int((lease.get("shell") or {}).get("pid") or 0) == int(pid):
                return lease, raw, round(time.time() - started, 2)
            if proc is not None and proc.poll() is not None:
                return None, last, round(time.time() - started, 2)
            if time.time() >= deadline:
                return None, last, round(time.time() - started, 2)
            time.sleep(0.5)

    def installer_env(self) -> dict:
        """安装/卸载器都拿 work-dir 里的 TEMP（避免往用户真实 TEMP 里塞安装器副本）。"""
        tmp = self.work / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["TEMP"] = str(tmp)
        env["TMP"] = str(tmp)
        return env

    # -- 安全护栏 -------------------------------------------------------------
    def ours(self) -> set:
        pids = {pid for pid, _tag in self.tracked_pids}
        for directory in (self.install1, self.install2, self.install3, self.decoy_dir):
            for item in processes_in_dir(directory):
                pids.add(item["pid"])
        for proc in self.shells.values():
            if proc.poll() is None:
                pids.add(proc.pid)
        return pids

    def foreign_qio_processes(self) -> list:
        """不属于本次验证装置的 QIO 命名进程 —— 静默安装前必须为空。

        安装段的 NSIS 模板检查按**映像名**杀 qio.exe（构建期补丁只换了卸载段那一处），
        有别人的 QIO 在跑时继续安装就是在拿别人的进程冒险。
        """
        mine = self.ours()
        out = []
        for item in qio_named_processes():
            if item["pid"] in mine:
                continue
            if is_under(item["exe"], self.work):
                continue
            out.append(item)
        return out

    def stop_by_pid(self, pid: int, tag: str):
        item = process_identity(pid)
        if item is None:
            return
        code, out = kill_pid_tree(pid)
        self.rec.evidence("kill-%s-pid%s" % (tag, pid),
                          "按 pid 收进程：pid=%s exe=%s ft=%s\n$ taskkill /F /T /PID %s\nexit=%s\n%s"
                          % (pid, item["exe"], item["created_filetime"], pid, code, out))
        left = wait_identity_gone([pid], timeout=10)
        if left:
            log("  !! pid %s 仍在（taskkill 后复核）" % pid)

    def stop_all_tracked(self, reason: str):
        log("== 清理：%s" % reason)
        for proc in list(self.shells.values()):
            if proc.poll() is None:
                self.stop_by_pid(proc.pid, "cleanup")
        for directory, label in ((self.install1, "install1"), (self.install2, "install2"),
                                 (self.install3, "install3"), (self.decoy_dir, "decoy")):
            for item in processes_in_dir(directory):
                self.stop_by_pid(item["pid"], label)


# ---------------------------------------------------------------- 断言小工具

def all_true(pairs) -> tuple:
    """pairs = [(是否成立, 说明)] → (全成立?, 拼接说明)。"""
    bad = [text for ok, text in pairs if not ok]
    return (not bad), ("；".join(text for _ok, text in pairs) if not bad else "问题：" + "；".join(bad))


# ---------------------------------------------------------------- 第 0 步：环境与护栏

def scan_commands_for_im(source_path: Path) -> list:
    """AST 扫一遍：有没有哪条命令的实参里出现 /IM（按映像名结束进程）。

    只扫**调用点实参**里的字符串常量，不扫文档/注释 —— 上一版用正则扫全文，把自己的说明文字
    也当成了命中（误报）。真正的清理路径是 kill_pid_tree()：taskkill /F /T /PID <pid>。
    """
    found = []
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return ["AST 解析失败：%r" % (exc,)]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for literal in ast.walk(arg):
                if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                    if re.search(r"(^|\s)/IM(\s|$)", literal.value, re.IGNORECASE):
                        found.append("line %s: %r" % (getattr(literal, "lineno", "?"), literal.value[:80]))
    return sorted(set(found))

def stage_preflight(ctx: Ctx):
    rec = ctx.rec
    facts = host_facts()
    rec.meta["host"] = facts
    ctx.work.mkdir(parents=True, exist_ok=True)
    rec.add("S0-001", "跑在真实 Windows 桌面会话里（不是服务/无窗口会话）", PASS,
            "session_id=%s（1 = 交互桌面会话）；integrity=%s；user=%s；TEMP=%s（可写=%s）"
            % (facts["session_id"], facts["integrity"], facts["user"], facts["temp"], facts["temp_writable"]),
            rec.evidence("host-facts", json.dumps(facts, ensure_ascii=False, indent=2)))
    real_appdata_qio = Path(facts["appdata"] or "") / "qio"
    rec.add("S0-002", "用户数据目录隔离：QIO_DATA_DIR 指向 work-dir，不碰 %APPDATA%\\qio",
            PASS if str(ctx.data1).startswith(str(ctx.work)) else FAIL,
            "QIO_DATA_DIR=%s；真实 %s %s" % (ctx.data1, real_appdata_qio,
                                         "存在（本脚本不写入）" if real_appdata_qio.exists() else "不存在"))
    # 反证：本脚本拼出来的命令里没有一条带 /IM（按映像名结束进程）。
    # 用 AST 扫**调用点的实参**，不扫源码文本 —— 文档/注释里出现 "/IM" 不算数（上一版正则扫全文误报过）。
    hits = scan_commands_for_im(Path(__file__))
    rec.add("S0-003", "清进程只用 pid：脚本拼出来的命令里没有按映像名（/IM）的写法", PASS if not hits else FAIL,
            "命中=%s；清理路径 = taskkill /F /T /PID <pid>" % (hits or "无"),
            rec.evidence("self-scan-no-im", "AST 扫描 subprocess 调用实参里的 /IM：%s\n" % (hits or "无")))
    shell_env = list(getattr(ctx.args, "shell_env", []) or [])
    if shell_env:
        rec.add("S0-005", "装置偏差：给外壳/后端追加了环境变量（结论里必须显式说明）", WARN,
                "shell-env=%s。本机两条已知环境限制（docs/e2e-install-2026-10-02.md §4）："
                "真实 %%TEMP%% 下 PyInstaller onefile 建不出临时目录（后端秒退）；"
                "受限令牌下 db_identity 写基线 WinError 5（产品自带 QIO_DISABLE_DB_CHECK=1 绕开）。"
                "这两条必须在结论里写明，不能当成产品行为。" % shell_env)
    # 静默安装的安全护栏
    foreign = ctx.foreign_qio_processes()
    main_foreign = [item for item in foreign if Path(item["exe"]).name.lower() == "qio.exe"]
    other_foreign = [item for item in foreign if Path(item["exe"]).name.lower() != "qio.exe"]
    if ctx.standalone:
        # standalone 不跑安装器；这里只记录（别的 QIO 实例可能干扰 lease/窗口判定，但不是本次的判据）
        rec.add("S0-004", "当前没有别的 QIO 主程序在跑（standalone 模式：仅记录）",
                WARN if main_foreign else PASS,
                "别的 qio.exe：%s；别的 QIO 命名进程：%s" % (describe_processes(main_foreign) if main_foreign else "无",
                                                     describe_processes(other_foreign) if other_foreign else "无"))
        return True
    if main_foreign:
        # 安装段的 NSIS 模板检查是**按映像名**杀 qio.exe 的（构建期补丁只换了卸载段那一处）。
        # 有别人的主程序在跑时继续安装，就是拿别人的进程冒险 —— 拒绝继续。
        rec.add("S0-004", "静默安装前没有别的 QIO 主程序在跑（安装段会按名字杀 qio.exe）", FAIL,
                "发现不属于本次装置的 qio.exe：%s —— 拒绝继续安装，请先关掉它们" % describe_processes(main_foreign),
                rec.evidence("foreign-qio", describe_processes(foreign)))
        return False
    if other_foreign:
        rec.add("S0-004", "静默安装前没有别的 QIO 主程序在跑", WARN,
                "没有别的 qio.exe；但存在其它 QIO 命名进程（安装段的检查只针对主程序，它们不会被按名字杀）：%s"
                % describe_processes(other_foreign), rec.evidence("foreign-qio-others", describe_processes(other_foreign)))
    else:
        rec.add("S0-004", "静默安装前没有别的 QIO 主程序在跑", PASS, "QIO 命名进程：无（不属于本次装置的）")
    return True


# ---------------------------------------------------------------- 机器状态：QIO 卸载登记项
#
# 为什么必须管这个：机器上可能还登记着**上一次测试安装**（本机实测：
# HKCU\...\Uninstall\QIO 指向 .repro\QIO-D，0.1.11）。Tauri 的 NSIS 模板检测到已有安装，
# 静默安装会先去执行旧卸载器（实测静默安装直接 exit=2、一个字节都不写）。
# 干净安装的前提是「没有旧登记」：先导出留档 → 删掉 → 装 → 收尾时**原样导回**。
# 只动 Uninstall 那一项；HKCU\Software\qio\QIO 里的 DbBaseline 是用户状态，绝不碰。

REG_UNINSTALL_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\QIO"


def stage_registry_snapshot(ctx: Ctx):
    rec = ctx.rec
    outdir = ctx.work / "registry"
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / "uninstall-QIO.reg"
    code, out = ctx.run(["reg", "export", REG_UNINSTALL_KEY, str(target), "/y"],
                        tag="reg-export-uninstall-qio", timeout=60)
    ctx.registry_backup = target if code == 0 and target.is_file() else None
    rec.add("S1-000a", "安装前把机器上遗留的 QIO 卸载登记项留档（有就导出，没有就记着）",
            PASS, "reg export exit=%s；备份=%s" % (code, ctx.registry_backup or "(没有该项，无需备份)"),
            rec.evidence("reg-export", "reg export %s\nexit=%s\n%s" % (REG_UNINSTALL_KEY, code, out)))
    return ctx.registry_backup


def clear_legacy_install_registration(ctx: Ctx):
    """把旧的卸载登记摘掉，让这次静默安装走「全新安装」而不是「先卸载上一份」。"""
    rec = ctx.rec
    code, out = ctx.run(["reg", "delete", REG_UNINSTALL_KEY, "/f"], tag="reg-delete-uninstall-qio", timeout=60)
    rec.add("S1-000b", "摘掉遗留卸载登记（避免静默安装先去卸载上一次测试安装）",
            PASS if code in (0, 1) else FAIL,  # 1 = 本来就不存在
            "reg delete exit=%s（1 = 本来就没有）" % code,
            rec.evidence("reg-delete", "reg delete %s /f\nexit=%s\n%s" % (REG_UNINSTALL_KEY, code, out)))


def restore_registry(ctx: Ctx):
    rec = ctx.rec
    backup = getattr(ctx, "registry_backup", None)
    if not backup or not Path(backup).is_file():
        rec.add("S9-004", "收尾：还原安装前留档的卸载登记（本次没有旧登记）", PASS, "没有可还原的备份")
        return
    code, out = ctx.run(["reg", "import", str(backup)], tag="reg-import-uninstall-qio", timeout=60)
    rec.add("S9-004", "收尾：把卸载登记还原成跑之前的样子", PASS if code == 0 else WARN,
            "reg import exit=%s (%s)" % (code, backup),
            rec.evidence("reg-import", "reg import %s\nexit=%s\n%s" % (backup, code, out)))


# ---------------------------------------------------------------- 第 1 步前置：静默安装

def stage_install(ctx: Ctx):
    rec = ctx.rec
    installer = ctx.installer
    if installer is None or not installer.is_file():
        rec.add("S1-001", "安装包存在", FAIL, "找不到 %s" % installer)
        return False
    digest = sha256_of(installer)
    ctx.installer_sha256 = digest
    rec.meta["installer"] = {"path": str(installer), "sha256": digest,
                             "bytes": installer.stat().st_size,
                             "expected_sha256": ctx.args.sha256 or None}
    ev = rec.evidence("installer-fingerprint",
                      "path=%s\nbytes=%s\nsha256=%s\nexpected=%s\n" % (installer, installer.stat().st_size,
                                                                     digest, ctx.args.sha256 or "(未提供)"))
    if ctx.args.sha256:
        rec.add("S1-001", "被测安装包 sha256 与 Lead 给的一致",
                PASS if digest.lower() == ctx.args.sha256.lower() else FAIL,
                "实际 %s / 期望 %s" % (digest, ctx.args.sha256), ev)
    else:
        rec.add("S1-001", "安装包 sha256（未提供期望值，只记录指纹）", WARN,
                "%s（NOT VERIFIED：没有可比对的期望值）" % digest, ev)
    if not ctx.args.i_know_this_installs_outside_work_dir and not str(ctx.install1).lower().startswith(str(ctx.work).lower()):
        rec.add("S1-002", "安装目录在 work-dir 内（防止误装/误卸真实安装）", FAIL,
                "install_dir=%s 不在 work_dir=%s 内；如确实要这样跑，加 --i-know-this-installs-outside-work-dir"
                % (ctx.install1, ctx.work))
        return False
    stage_registry_snapshot(ctx)
    clear_legacy_install_registration(ctx)
    shutil.rmtree(ctx.install1, ignore_errors=True)
    cmdline = '"%s" /S /D=%s' % (installer, ctx.install1)
    code, out = ctx.run(cmdline, tag="install1-silent", timeout=900, env=ctx.installer_env())
    ok, detail = wait_for(lambda: (ctx.install1 / "qio.exe").is_file(), timeout=300)
    files = sorted(p.name for p in ctx.install1.glob("*")) if ctx.install1.exists() else []
    rec.add("S1-002", "静默安装成功（/S /D=<install_dir>）", PASS if code == 0 and ok else FAIL,
            "exit=%s 目录出现 qio.exe=%s；顶层文件 %d 个" % (code, ok, len(files)),
            rec.evidence("install1-output", "$ %s\nexit=%s\n\n%s" % (cmdline, code, out)))
    need = [("qio.exe", True), ("qio-backend.exe", True), ("uninstall.exe", True),
            ("qio-uninstall-helper.exe", True)]
    missing = [name for name, _required in need if not (ctx.install1 / name).is_file()]
    rec.add("S1-003", "安装目录里该有的东西都在（外壳/后端/卸载器/帮助程序）",
            PASS if not missing else FAIL,
            "缺：%s" % missing if missing else "qio.exe / qio-backend.exe / uninstall.exe / qio-uninstall-helper.exe 全在",
            rec.evidence("install1-files", "\n".join(files)))
    return code == 0 and ok


def wait_for(pred, timeout: float, interval: float = 0.5) -> tuple:
    deadline = time.time() + timeout
    while True:
        try:
            if pred():
                return True, time.time()
        except OSError:
            pass
        if time.time() >= deadline:
            return False, time.time()
        time.sleep(interval)


def clear_stale_lease(ctx: Ctx, install_dir: Path, rid: str) -> bool:
    """启动前的卫生检查：目录里不能有**活着的**进程；上一轮留下的旧 lease 留档后删掉。

    为什么必须做：旧 lease 的存在会让「启动后出现 lease」变成假阳性（实测踩到过 ——
    读到了 5.7 分钟前上一轮被强杀时留下的记录）。返回 False = 环境不干净，拒绝启动，
    脚本不会替你结束不属于本次装置的进程。
    """
    rec = ctx.rec
    install_dir = Path(install_dir)
    live = processes_in_dir(install_dir)
    if live:
        rec.add(rid, "启动前安装目录里没有别的 QIO 进程（否则记录归属不到本实例）", FAIL,
                "发现活动进程：%s —— 请先关掉它再跑；脚本不结束不属于本次装置的进程" % describe_processes(live),
                rec.evidence("%s-preexisting" % rid, describe_processes(live)))
        return False
    lease_file = install_dir / LEASE_NAME
    if lease_file.exists():
        raw = read_lease(install_dir)[1] or "(读不出来)"
        ev = rec.copy_evidence("%s-stale-lease" % rid, lease_file)
        try:
            lease_file.unlink()
            removed, why = True, ""
        except OSError as exc:
            removed, why = False, repr(exc)
        rec.add(rid + "-stale", "上一轮留下的旧 lease 已留档并清掉（避免把旧记录当成新记录）",
                PASS if removed else FAIL,
                "旧 lease：%s%s" % (raw.replace("\n", " ")[:200], ("；删除失败：" + why) if why else ""), ev)
        if not removed:
            return False
    return True


# ---------------------------------------------------------------- 第 1 步：lease 出现且内容对得上

def check_lease_against_processes(ctx: Ctx, prefix: str, install_dir: Path, lease: dict, raw: str,
                                  shell_pid_expected=None) -> bool:
    """一条 lease 的完整核对：静态字段 + **独立枚举**的真实进程三要素。返回是否全对。"""
    rec = ctx.rec
    results = []
    install_dir = Path(install_dir)
    ev_raw = rec.evidence("%s-lease-raw" % prefix, raw or "(读不到)")

    def add(rid, title, ok, detail, evidence=None):
        results.append(bool(ok))
        rec.add("%s-%s" % (prefix, rid), title, PASS if ok else FAIL, detail, evidence)

    add("010", "lease 是合法 JSON 且 schema=1",
        isinstance(lease, dict) and lease.get("schema") == 1,
        "schema=%r" % (lease.get("schema") if isinstance(lease, dict) else None), ev_raw)
    recorded_dir = str((lease or {}).get("install_dir") or "")
    same_dir = os.path.normcase(os.path.abspath(recorded_dir)) == os.path.normcase(str(install_dir))
    add("011", "lease.install_dir = 本次安装目录", same_dir,
        "lease 记的 %r / 实际 %s" % (recorded_dir, install_dir))
    # shell 段：三要素与真实进程逐项比对
    shell = (lease or {}).get("shell") or {}
    live_shell = _process_times_and_path(shell.get("pid")) if isinstance(shell.get("pid"), int) else None
    shell_pairs = []
    if live_shell is None:
        shell_pairs.append((False, "shell.pid=%r 当前取不到活动进程（进程已退或无权查询）" % shell.get("pid")))
    else:
        shell_pairs.append((str(shell.get("created_filetime") or "") == live_shell["created_filetime"],
                            "created_filetime：lease=%s 实际=%s" % (shell.get("created_filetime"), live_shell["created_filetime"])))
        shell_pairs.append((os.path.normcase(os.path.abspath(str(shell.get("exe") or ""))) ==
                            os.path.normcase(os.path.abspath(live_shell["exe"])),
                            "exe：lease=%s 实际=%s" % (shell.get("exe"), live_shell["exe"])))
        expected_shell_exe = ctx.shell_exe if ctx.standalone else (install_dir / "qio.exe")
        shell_pairs.append((os.path.normcase(os.path.abspath(str(shell.get("exe") or ""))) ==
                            os.path.normcase(str(Path(expected_shell_exe).resolve())),
                            "exe 是本次启动的外壳 %s" % expected_shell_exe))
        if shell_pid_expected is not None:
            shell_pairs.append((int(shell.get("pid") or 0) == int(shell_pid_expected),
                                "shell.pid=%s 与脚本启动的进程 pid=%s 一致" % (shell.get("pid"), shell_pid_expected)))
    ok, detail = all_true(shell_pairs)
    add("012", "shell 三要素（pid/创建时间/exe）与**独立枚举**的真实进程逐项一致", ok, detail,
        rec.evidence("%s-shell-identity" % prefix,
                     "lease.shell=%s\n独立枚举=%s" % (json.dumps(shell, ensure_ascii=False),
                                                  json.dumps(live_shell, ensure_ascii=False))))
    # backend 段
    backend = (lease or {}).get("backend") or {}
    live_backend, backend_waited = (sample_process_identity(backend["pid"]) if isinstance(backend.get("pid"), int)
                                    else (None, 0.0))
    backend_pairs = []
    if live_backend is None:
        backend_pairs.append((False, "backend.pid=%r 观察了 %.1fs 也没有活动进程（后台在 lease 写出后很快就没了）"
                              % (backend.get("pid"), backend_waited)))
    else:
        backend_pairs.append((str(backend.get("created_filetime") or "") == live_backend["created_filetime"],
                              "created_filetime：lease=%s 实际=%s" % (backend.get("created_filetime"), live_backend["created_filetime"])))
        backend_pairs.append((os.path.normcase(os.path.abspath(str(backend.get("exe") or ""))) ==
                              os.path.normcase(os.path.abspath(live_backend["exe"])),
                              "exe：lease=%s 实际=%s" % (backend.get("exe"), live_backend["exe"])))
    ok, detail = all_true(backend_pairs)
    add("013", "backend 三要素（pid/创建时间/exe）与**独立枚举**的真实进程逐项一致", ok, detail,
        rec.evidence("%s-backend-identity" % prefix,
                     "lease.backend=%s\n独立枚举=%s" % (json.dumps(backend, ensure_ascii=False),
                                                       json.dumps(live_backend, ensure_ascii=False))))
    expected_backend_exe = Path(str(backend.get("exe") or ""))
    add("014", "backend.exe 就是本安装目录里的 qio-backend.exe",
        os.path.normcase(os.path.abspath(str(expected_backend_exe))) ==
        os.path.normcase(str((install_dir / "qio-backend.exe").resolve())),
        "lease.backend.exe=%s" % expected_backend_exe)
    # 安装目录内的活动进程都能被这份 lease 解释
    live = processes_in_dir(install_dir)
    shell_pid = int(shell.get("pid") or 0)
    backend_pid = int(backend.get("pid") or 0)
    unexplained = []
    for item in live:
        name = Path(item["exe"]).name.lower()
        if name == "qio.exe":
            if item["pid"] != shell_pid:
                unexplained.append("qio.exe pid=%s 不在这份 lease 里（shell 记的是 %s）" % (item["pid"], shell_pid))
        elif name == "qio-backend.exe":
            if item["pid"] != backend_pid and backend_pid not in parent_chain(item["pid"]):
                unexplained.append("qio-backend.exe pid=%s 既不是 lease 里的 backend，也不是它的后代" % item["pid"])
        else:
            unexplained.append("%s pid=%s" % (item["exe"], item["pid"]))
    add("015", "安装目录内所有活动进程都能被这份 lease 解释（没有无法确认归属的后台）",
        not unexplained,
        ("；".join(unexplained) if unexplained else "枚举到 %d 个，全部对上" % len(live)),
        rec.evidence("%s-live-processes" % prefix,
                     "安装目录 %s 内的活动进程：\n%s\n\nparent_chain(backend)=%s"
                     % (install_dir, describe_processes(live), parent_chain(backend_pid) if backend_pid else [])))
    # 会话令牌不能出现在 lease 原文里
    token_path = Path(os.environ.get("TEMP") or ".") / ("qio-session-%d.token" % shell_pid)
    token_value = ""
    try:
        token_value = token_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    hits = lease_secret_hits(raw, extra_values=[token_value] if token_value else ())
    add("016", "lease 原文不含会话令牌/密钥形态", not hits,
        "命中：%s" % hits if hits else ("未命中（会话令牌文件 %s）" % ("已比对" if token_value else "本次没读到")),
        ev_raw)
    return all(results)


def stage_lease(ctx: Ctx):
    rec = ctx.rec
    shell_exe = ctx.shell_exe if ctx.standalone else (ctx.install1 / "qio.exe")
    install_dir = ctx.install1
    if not clear_stale_lease(ctx, install_dir, "S1-019"):
        return False
    proc, logfile = ctx.launch_shell(shell_exe, ctx.data1, "install1")
    early = EarlyCloseWatch(proc.pid, install_dir)
    early.start()
    ctx.early_close = early
    ok_shell, _ = wait_for(lambda: process_identity(proc.pid) is not None, timeout=20)
    rec.add("S1-020", "外壳进程起来了", PASS if ok_shell else FAIL,
            "pid=%s exe=%s" % (proc.pid, shell_exe),
            rec.evidence("shell-live", json.dumps(process_identity(proc.pid), ensure_ascii=False)))
    lease, raw, took = ctx.wait_lease_for_pid(install_dir, proc.pid, ctx.args.lease_timeout, proc=proc)
    ev = rec.evidence("lease-install1-raw",
                      "path=%s\nexists=%s\n等待用时=%ss\n\n%s" % (install_dir / LEASE_NAME,
                                                              (install_dir / LEASE_NAME).exists(), took,
                                                              raw or "(没有文件)"))
    if lease is None:
        rec.add("S1-021", "启动后出现本实例（pid=%s）的 sidecar.lease.json" % proc.pid, FAIL,
                "等了 %ss 没有等到 shell.pid=%s 的 lease（读到的可能是上一轮/别人的记录）；外壳 exit=%s；日志尾部=%s"
                % (took, proc.pid, proc.poll(), ctx.tail(logfile)[-600:] or "（空）"), ev)
        return False
    rec.add("S1-021", "启动后出现本实例（pid=%s）的 sidecar.lease.json" % proc.pid, PASS,
            "%s（%d 字节，等待 %.1fs）" % (install_dir / LEASE_NAME, len(raw), took), ev)
    watch = ctx.window_watches.get("install1")
    info = watch.stop() if watch else {}
    ev_win = rec.evidence("shell-windows",
                          "窗口采样（从进程启动开始，0.25s 一次）：\n" +
                          "\n".join("t=%ss 可见带标题窗口=%s%s" % (t, n, ("  title=%r" % title) if title else "")
                                    for t, n, title in (info.get("samples") or [])) +
                          "\n\nfirst_seen=%s last_seen=%s alive_at_stop=%s last_windows=%s"
                          % (info.get("first_seen"), info.get("last_seen"), info.get("alive_at_stop"),
                             json.dumps(info.get("last_windows") or [], ensure_ascii=False)))
    rec.add("S1-022", "lease 里的 shell pid 就是脚本启动的外壳，且它拥有真实顶层窗口（GUI 桌面外壳）",
            PASS if info.get("first_seen") is not None else FAIL,
            ("窗口首次出现 t=%ss，最后一次可见 t=%ss%s"
             % (info.get("first_seen"), info.get("last_seen"),
                "；**本步结束时窗口已经没了**（建出来又消失）" if not info.get("alive_at_stop") else "；本步结束时仍在"))
            if info.get("first_seen") is not None
            else "从启动开始采样，45s 内没有出现任何可见带标题的顶层窗口", ev_win)
    if info.get("first_seen") is not None and not info.get("alive_at_stop"):
        rec.add("S1-022b", "窗口不能「建出来又消失」：本步结束时它必须还在",
                FAIL, "首次 t=%ss → 最后 t=%ss 就没了；之后进程还活着（这正是 2026-10-03 那个坏 profile / 后端起不来的形状）"
                % (info.get("first_seen"), info.get("last_seen")), ev_win)
    ok = check_lease_against_processes(ctx, "S1", install_dir, lease, raw, shell_pid_expected=proc.pid)
    # 后台存活时间线：lease 写下的 backend pid 到底活了多久（本机实测：几秒内就没了）
    backend_pid = int((lease.get("backend") or {}).get("pid") or 0)
    timeline, seen = [], False
    t0 = time.time()
    for _ in range(12):
        live_pids = [item["pid"] for item in processes_in_dir(install_dir)]
        alive_now = backend_pid in live_pids if backend_pid else False
        seen = seen or alive_now
        timeline.append("t=%+.1fs backend_alive=%s 目录内活动 pid=%s" % (time.time() - t0, alive_now, live_pids))
        time.sleep(0.5)
    rec.add("S1-024", "后台（lease 里的 backend pid）的存活时间线",
            PASS if seen else FAIL,
            ("观察 %d 次都见到 pid=%s" % (len(timeline), backend_pid)) if seen
            else "从 lease 写出后观察 6s，pid=%s 一次都没见到（后台在写出后很快退出）" % backend_pid,
            rec.evidence("backend-timeline", "\n".join(timeline)))
    log_path = ctx.data1 / "logs" / "qio.log"
    log_text = ctx.tail(log_path, 4000)
    has_line = "已写 sidecar lease" in log_text and str(proc.pid) in log_text
    rec.add("S1-023", "日志通道有内容，且记录了本次 lease 写入（task-4 的日志修复）",
            PASS if has_line else FAIL,
            "%s：%s" % (log_path, (log_text.strip()[-300:] or "（空）")),
            rec.evidence("lease-log", log_text or "(空)"))
    ctx.lease1 = lease
    rec.meta["lease_install1"] = lease
    return ok


# ---------------------------------------------------------------- 第 2 步：正常退出 → lease 被删

def stage_exit(ctx: Ctx):
    rec = ctx.rec
    proc = ctx.shells.get("install1")
    install_dir = ctx.install1
    if proc is None:
        rec.add("S2-001", "存在一个正在跑的本次安装外壳（第 2 步的前提）", FAIL, "没有已启动的外壳")
        return False
    lease, raw, _err = read_lease(install_dir)
    backend_pid = int(((lease or {}).get("backend") or {}).get("pid") or 0)
    early = getattr(ctx, "early_close", None)
    einfo = early.stop() if early else {}
    ev_close = rec.evidence("early-close-watch", json.dumps(einfo, ensure_ascii=False, indent=2))
    rec.add("S2-000", "窗口在 lease 写出后仍然可见（走正常退出路径的前提）",
            PASS if einfo.get("posted_at") is not None else FAIL,
            "窗口首见 t=%ss；lease（shell.pid=本进程）首见 t=%ss；WM_CLOSE 发出 t=%ss"
            % (einfo.get("window_seen_at"), einfo.get("lease_seen_at"), einfo.get("posted_at")),
            ev_close)
    posted = bool(einfo.get("posted"))
    rec.add("S2-001", "在窗口存活期内给主窗口发 WM_CLOSE（等价于点关闭按钮，不是 taskkill）",
            PASS if posted else FAIL,
            ("posted=True hwnd=%s；当时的窗口：%s" % (einfo.get("posted_hwnd"),
                                                  [{"class": w["class"], "title": w["title"], "visible": w["visible"]}
                                                   for w in (einfo.get("posted_windows") or [])]))
            if posted else
            ("没能在「窗口可见 + lease 已写出」同时成立时发出 WM_CLOSE"
             "（窗口首见=%s / lease 首见=%s / 发出=%s）—— 窗口寿命问题见 §17.5"
             % (einfo.get("window_seen_at"), einfo.get("lease_seen_at"), einfo.get("posted_at"))),
            ev_close)
    if not posted:
        posted2, ev_text = close_main_window(proc.pid)
        rec.add("S2-001b", "退而求其次：此刻直接找主窗口再发一次", PASS if posted2 else FAIL,
                "posted=%s" % posted2, rec.evidence("close-main-window", ev_text))
        if not posted2:
            return False
    exited, _ = wait_for(lambda: proc.poll() is not None, timeout=ctx.args.exit_timeout)
    code = proc.poll()
    rec.add("S2-002", "外壳自己退出（未被杀）：退出码 0",
            PASS if exited and code == 0 else FAIL,
            "exited=%s exit_code=%s；日志尾部=%s" % (exited, code, ctx.tail(ctx.rec.ev_dir / "shell-install1.log")[-500:] or "（空）"))
    gone, _ = wait_for(lambda: process_identity(proc.pid) is None, timeout=30)
    rec.add("S2-003", "外壳进程真的消失（独立枚举复核）", PASS if gone else FAIL,
            "pid=%s 仍可枚举=%s" % (proc.pid, not gone))
    lease_gone, _ = wait_for(lambda: not (install_dir / LEASE_NAME).exists(), timeout=30)
    rec.add("S2-004", "正常退出后 lease 被删（按设计清理）", PASS if lease_gone else FAIL,
            "lease 存在=%s" % (install_dir / LEASE_NAME).exists(),
            rec.evidence("lease-after-exit",
                         "path=%s\nexists=%s\n" % (install_dir / LEASE_NAME, (install_dir / LEASE_NAME).exists())))
    if backend_pid:
        backend_gone, _ = wait_for(lambda: process_identity(backend_pid) is None, timeout=30)
        rec.add("S2-005", "本次安装的后台进程随外壳退出被收掉（job/pid 兜底）",
                PASS if backend_gone else FAIL,
                "backend pid=%s 仍可枚举=%s" % (backend_pid, not backend_gone))
    leftovers = processes_in_dir(install_dir)
    rec.add("S2-006", "退出后安装目录里没有残留活动进程", PASS if not leftovers else FAIL,
            describe_processes(leftovers),
            rec.evidence("leftover-after-exit", describe_processes(leftovers)))
    tmps = sorted(p.name for p in install_dir.glob(LEASE_NAME + ".tmp*")) if install_dir.exists() else []
    rec.add("S2-007", "没有留下 lease 临时文件", PASS if not tmps else FAIL, "残留=%s" % (tmps or "无"))
    return lease_gone and gone


# ---------------------------------------------------------------- 第 4 步：lease 写失败

def stage_leasefail(ctx: Ctx):
    """lease 写失败（真实失败路径）：把 <install_dir>\\sidecar.lease.json 预建成目录，
    让 WriteFile 之后的原子替换必然失败 —— 不依赖任何测试钩子，走的是产品真实的 Err 分支。"""
    rec = ctx.rec
    if ctx.installer is None:
        rec.add("S4-001", "lease 写失败场景需要一份安装（standalone 模式无法构造）", SKIP,
                "本次用 --shell-exe 自测骨架：第 4 步 NOT VERIFIED")
        return None
    install_dir = ctx.install3
    shutil.rmtree(install_dir, ignore_errors=True)
    ctx.stop_all_tracked("lease 写失败场景前：按 pid 收掉本次装置还在跑的外壳（别让安装段的按名字检查动手）")
    code, out = ctx.run('"%s" /S /D=%s' % (ctx.installer, install_dir), tag="install3-silent", timeout=900,
                        env=ctx.installer_env())
    ready, _ = wait_for(lambda: (install_dir / "qio.exe").is_file(), timeout=300)
    rec.add("S4-001", "第三份安装就位（lease 写失败场景的载体）",
            PASS if code == 0 and ready else FAIL, "exit=%s qio.exe=%s" % (code, ready))
    if not ready:
        return False
    trigger = install_dir / LEASE_NAME
    if trigger.exists() and not trigger.is_dir():
        trigger.unlink()
    trigger.mkdir(parents=True, exist_ok=True)
    (trigger / "BLOCKED-BY-VERIFIER.txt").write_text(
        "本目录由 scripts/verify_install_desktop.py 故意创建：占据 lease 路径，让外壳的原子替换失败。\n",
        encoding="utf-8")
    rec.add("S4-002", "构造真实的 lease 写失败：lease 路径被一个目录占着",
            PASS if trigger.is_dir() else FAIL,
            "trigger=%s is_dir=%s（外壳 write_lease 的 rename 必然失败）" % (trigger, trigger.is_dir()))
    proc, logfile = ctx.launch_shell(install_dir / "qio.exe", ctx.data3, "install3")
    exited, _ = wait_for(lambda: proc.poll() is not None, timeout=ctx.args.lease_timeout)
    exit_code = proc.poll()
    rec.add("S4-003", "lease 写失败时外壳自己退出（task-4 契约：exit=1，不带着无法归属的后台继续跑）",
            PASS if exited else FAIL,
            ("exit=%s（pid=%s）" % (exit_code, proc.pid)) if exited
            else "等了 %ss 外壳仍在运行（pid=%s）" % (ctx.args.lease_timeout, proc.pid),
            rec.evidence("leasefail-shell-log", ctx.tail(logfile, 4000) or "(空)"))
    if exited and exit_code != 1:
        rec.add("S4-003b", "退出码与 task-4 契约一致（1）", WARN, "实际 exit=%s，契约写的是 1" % exit_code)
    # 原因必须可读：契约给的两个落点 + 日志里的 [ERROR] 行
    reason_files = [("QIO_DATA_DIR/qio-startup-error.txt", ctx.data3 / "qio-startup-error.txt"),
                    ("install_dir/qio-startup-error.txt", install_dir / "qio-startup-error.txt")]
    chunks, texts = [], {}
    for label, path in reason_files:
        text = ""
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                text = "读取失败：%r" % (exc,)
        texts[label] = text
        chunks.append("== %s ==\n%s" % (label, text or "(没有这个文件)"))
    log_text = ctx.tail(ctx.data3 / "logs" / "qio.log", 6000)
    chunks.append("== QIO_DATA_DIR/logs/qio.log ==\n%s" % (log_text or "(空)"))
    chunks.append("== shell stdout/stderr ==\n%s" % (ctx.tail(logfile, 4000) or "(空)"))
    ev = rec.evidence("leasefail-reason", "\n".join(chunks))
    marker = "QIO-LEASE-WRITE-FAILED"
    marker_hits = [label for label, text in texts.items() if marker in text]
    if marker in log_text:
        marker_hits.append("QIO_DATA_DIR/logs/qio.log")
    rec.add("S4-004", "原因可读：稳定标记 %s 出现在原因文件 / 日志里" % marker,
            PASS if marker_hits else FAIL,
            ("命中：%s" % "；".join(marker_hits)) if marker_hits else
            "两个原因文件与 qio.log 里都没有 %s" % marker, ev)
    reason_bits = []
    for label, text in list(texts.items()) + [("qio.log", log_text)]:
        for line in (text or "").splitlines():
            low = line.lower()
            if "lease" in low and any(word in low for word in ("失败", "failed", "拒绝", "denied", "os error", "error")):
                reason_bits.append("[%s] %s" % (label, line.strip()))
    rec.add("S4-004b", "原因文本里有具体的失败说明（人类可读，不是只有标记）",
            PASS if reason_bits else FAIL,
            "；".join(reason_bits[:3]) if reason_bits else "没有任何带原因的 lease 失败文本", ev)
    # 契约核心：后台被收掉，不允许「后台还在跑但无法确认归属」
    wait_for(lambda: not processes_in_dir(install_dir), timeout=15)
    live = processes_in_dir(install_dir)
    backends = [item for item in live if Path(item["exe"]).name.lower() == "qio-backend.exe"]
    rec.add("S4-005", "lease 写失败时已启动的后台被收掉（不留无法确认归属的 qio-backend）",
            PASS if not backends else FAIL,
            ("残留后台：%s" % describe_processes(backends)) if backends
            else ("安装目录内其它活动进程：%s" % describe_processes(live) if live else "安装目录内没有残留活动进程"),
            rec.evidence("leasefail-live-processes", describe_processes(live)))
    rec.add("S4-005b", "安装目录里没有残留活动进程（外壳退出 + 后台被收掉）",
            PASS if not live else FAIL, describe_processes(live))
    rec.add("S4-006", "lease 确实没被写出来（失败是真的）", PASS if not (install_dir / LEASE_NAME).is_file() else FAIL,
            "lease 路径仍是被目录占着=%s；存在 lease 文件=%s"
            % ((install_dir / LEASE_NAME).is_dir(), (install_dir / LEASE_NAME).is_file()))
    # 收尾：按 pid 收掉这一份，删触发目录
    for item in processes_in_dir(install_dir):
        ctx.stop_by_pid(item["pid"], "install3")
    if trigger.is_dir():
        shutil.rmtree(trigger, ignore_errors=True)
    return (not live) and bool(marker_hits)




def stage_windowfail(ctx: Ctx):
    """窗口看门狗 + 坏 WebView2 用户数据目录的失败模式（Lead 2026-10-03 定位）。

    用 --broken-profile-src 给的目录的**副本**（不动原件）当 WEBVIEW2_USER_DATA_FOLDER。
    期望：外壳起得来（窗口自检日志能看到 config 里的窗口被创建），但窗口很快被销毁 →
    看门狗写 QIO-WINDOW-GONE（数据目录 → 安装目录 → stderr）并**自己退出（exit=1）**，
    而不是留下一个"进程活着、界面没了、用户点不到"的僵尸外壳。
    """
    rec = ctx.rec
    src = Path(ctx.args.broken_profile_src).resolve() if ctx.args.broken_profile_src else None
    if ctx.installer is None or src is None or not src.is_dir():
        rec.add("S5-001", "窗口看门狗场景：需要 --installer + --broken-profile-src", SKIP,
                "NOT VERIFIED：%s" % ("没有提供坏 profile 源目录" if src is None else "源目录不存在：%s" % src))
        return None
    install_dir = ctx.install3
    if not (install_dir / "qio.exe").is_file():
        rec.add("S5-001", "窗口看门狗场景需要一个装好的外壳", FAIL, "%s 里没有 qio.exe" % install_dir)
        return False
    copy_dir = ctx.work / "broken-profile-copy"
    if not copy_dir.exists():
        shutil.copytree(src, copy_dir, dirs_exist_ok=True)
    rec.add("S5-001", "坏 WebView2 profile 的副本就位（原件不动）", PASS,
            "%s → %s" % (src, copy_dir))
    rec.meta["broken_profile"] = {"src": str(src), "copy": str(copy_dir)}
    ctx.stop_all_tracked("窗口看门狗场景前：按 pid 收掉还在跑的外壳")
    proc, logfile = ctx.launch_shell(install_dir / "qio.exe", ctx.data4, "install3-windowfail",
                                     extra_env={"WEBVIEW2_USER_DATA_FOLDER": str(copy_dir)})
    exited, _ = wait_for(lambda: proc.poll() is not None, timeout=ctx.args.window_timeout)
    code = proc.poll()
    rec.add("S5-002", "窗口丢了而进程还活着时，外壳自己退出（看门狗，exit=1）",
            PASS if exited else FAIL,
            ("exit=%s（pid=%s）" % (code, proc.pid)) if exited
            else "等了 %ss 外壳仍在运行（pid=%s）" % (ctx.args.window_timeout, proc.pid),
            rec.evidence("windowfail-shell-log", ctx.tail(logfile, 4000) or "(空)"))
    if exited and code != 1:
        rec.add("S5-002b", "退出码与契约一致（1）", WARN, "实际 exit=%s" % code)
    chunks, hits = [], []
    for label, path in (("QIO_DATA_DIR/qio-startup-error.txt", ctx.data4 / "qio-startup-error.txt"),
                        ("install_dir/qio-startup-error.txt", install_dir / "qio-startup-error.txt")):
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        chunks.append("== %s ==\n%s" % (label, text or "(没有这个文件)"))
        if "QIO-WINDOW-GONE" in text:
            hits.append(label)
    log_text = ctx.tail(ctx.data4 / "logs" / "qio.log", 4000)
    chunks.append("== QIO_DATA_DIR/logs/qio.log ==\n%s" % (log_text or "(空)"))
    shell_text = ctx.tail(logfile, 4000)
    chunks.append("== shell stdout/stderr ==\n%s" % (shell_text or "(空)"))
    if "QIO-WINDOW-GONE" in log_text:
        hits.append("QIO_DATA_DIR/logs/qio.log")
    ev = rec.evidence("windowfail-reason", "\n".join(chunks))
    rec.add("S5-003", "原因可读：QIO-WINDOW-GONE 出现在原因文件 / 日志里", PASS if hits else FAIL,
            ("命中：%s" % "；".join(hits)) if hits else "没有找到 QIO-WINDOW-GONE", ev)
    selfcheck = [line for line in (log_text + "\n" + shell_text).splitlines() if "窗口自检" in line]
    rec.add("S5-004", "窗口自检日志在（能区分『config 里没窗口』与『建出来又没了』）",
            PASS if selfcheck else WARN,
            "；".join(selfcheck[:2]) if selfcheck else "日志里没有『窗口自检』行（旧包没有这条）", ev)
    for item in processes_in_dir(install_dir):
        ctx.stop_by_pid(item["pid"], "windowfail")
    return bool(hits) and exited


# ---------------------------------------------------------------- 第 3 步：保持开着直接卸载

def prepare_second_install(ctx: Ctx) -> bool:
    rec = ctx.rec
    ctx.stop_all_tracked("第二份安装前：按 pid 收掉本次装置还在跑的外壳")
    shutil.rmtree(ctx.install2, ignore_errors=True)
    code, out = ctx.run('"%s" /S /D=%s' % (ctx.installer, ctx.install2), tag="install2-silent", timeout=900,
                        env=ctx.installer_env())
    ready, _ = wait_for(lambda: (ctx.install2 / "qio.exe").is_file(), timeout=300)
    rec.add("S3-001", "第二份静默安装就位（误伤对照：它必须毫发无损）",
            PASS if code == 0 and ready else FAIL, "exit=%s qio.exe=%s" % (code, ready))
    return code == 0 and ready


def start_decoys(ctx: Ctx) -> list:
    """诱饵：把 ping.exe 复制成 qio.exe / qio-backend.exe 长驻。

    名字与真进程**完全一样**、路径不同 —— 这是「按映像名杀」最容易被误伤的形状。
    只按 pid 收进程的实现必须一个都不碰。
    """
    rec = ctx.rec
    ctx.decoy_dir.mkdir(parents=True, exist_ok=True)
    ping = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
    started = []
    for name in ("qio.exe", "qio-backend.exe"):
        target = ctx.decoy_dir / name
        shutil.copy2(ping, target)
        proc = subprocess.Popen([str(target), "-t", "127.0.0.1"], cwd=str(ctx.decoy_dir),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ctx.tracked_pids.append((proc.pid, "decoy:%s" % name))
        started.append((name, proc))
    for name, proc in started:
        wait_for(lambda p=proc: process_identity(p.pid) is not None, timeout=10)
    rec.add("S3-002", "诱饵进程就位（映像名与真进程相同、路径不同）", PASS if all(
        process_identity(p.pid) is not None for _n, p in started) else FAIL,
        describe_processes([process_identity(p.pid) for _n, p in started if process_identity(p.pid)]),
        rec.evidence("decoys", "\n".join("%s -> %s" % (n, json.dumps(process_identity(p.pid), ensure_ascii=False))
                                         for n, p in started)))
    return [p for _n, p in started]


def stage_uninstall(ctx: Ctx):
    rec = ctx.rec
    if ctx.installer is None:
        rec.add("S3-001", "保持开着直接卸载需要一份安装（standalone 模式无法构造）", SKIP,
                "本次用 --shell-exe 自测骨架：第 3 步 NOT VERIFIED")
        return None
    if not prepare_second_install(ctx):
        return False
    decoys = start_decoys(ctx)
    if not clear_stale_lease(ctx, ctx.install1, "S3-005a") or not clear_stale_lease(ctx, ctx.install2, "S3-005b"):
        return False
    # 本次安装的外壳重新起来（第 2 步已经正常退出过一次）
    proc1, log1 = ctx.launch_shell(ctx.install1 / "qio.exe", ctx.data1, "install1b")
    lease1, raw1, took1 = ctx.wait_lease_for_pid(ctx.install1, proc1.pid, ctx.args.lease_timeout, proc=proc1)
    proc2, log2 = ctx.launch_shell(ctx.install2 / "qio.exe", ctx.data2, "install2")
    lease2, raw2, took2 = ctx.wait_lease_for_pid(ctx.install2, proc2.pid, ctx.args.lease_timeout, proc=proc2)
    rec.add("S3-003", "两份安装的外壳都在跑，各自的 lease 都在（卸载前的前提）",
            PASS if lease1 is not None and lease2 is not None else FAIL,
            "install1 lease=%s（等 %.1fs，pid=%s）；install2 lease=%s（等 %.1fs，pid=%s）"
            % (lease1 is not None, took1, proc1.pid, lease2 is not None, took2, proc2.pid),
            rec.evidence("s3-leases",
                         "install1:\n%s\n\ninstall2:\n%s\n" % (raw1 or "(无)", raw2 or "(无)")))
    if lease1 is None or lease2 is None:
        return False
    # 独立核对第二份的 lease（它也是真外壳写的）
    check_lease_against_processes(ctx, "S3B", ctx.install2, lease2, raw2, shell_pid_expected=proc2.pid)
    # 基线：身份 + 数据目录指纹 + 全局 QIO 命名进程
    baseline = {
        "decoy": [process_identity(p.pid) for p in decoys],
        "install1_shell": process_identity(proc1.pid),
        "install1_backend": _process_times_and_path(int((lease1.get("backend") or {}).get("pid") or 0)),
        "install2_shell": process_identity(proc2.pid),
        "install2_backend": _process_times_and_path(int((lease2.get("backend") or {}).get("pid") or 0)),
        "install2_lease_raw": raw2,
        "all_qio_named": qio_named_processes(),
        "data1": dir_manifest(ctx.data1),
    }
    ctx.baseline = baseline
    rec.add("S3-004", "基线快照（诱饵 / 两份安装的进程 / 第二份 lease 原文 / 用户数据目录指纹 / 全局 QIO 命名进程）",
            PASS, "诱饵=%s；install2 壳=%s；数据目录文件数=%s 指纹=%s"
            % (describe_processes(baseline["decoy"]), baseline["install2_shell"] and baseline["install2_shell"]["pid"],
               baseline["data1"]["count"], (baseline["data1"]["digest"] or "?")[:16]),
            rec.evidence("s3-baseline", json.dumps(baseline, ensure_ascii=False, indent=2)))
    # 卸载：保持 install1 开着，跑它自己的卸载器
    uninstaller = ctx.install1 / "uninstall.exe"
    code, out = ctx.run([str(uninstaller), "/S"], tag="uninstall1-silent", timeout=900, env=ctx.installer_env())
    gone, _ = wait_for(lambda: not ctx.install1.exists(), timeout=ctx.args.uninstall_timeout)
    still = sorted(p.name for p in ctx.install1.glob("*")) if ctx.install1.exists() else []
    rec.add("S3-010", "卸载器跑完且安装目录被清理", PASS if gone and not still else FAIL,
            "exit=%s；目录存在=%s；残留=%s" % (code, ctx.install1.exists(), still or "无"),
            rec.evidence("uninstall1-output", "$ %s /S\nexit=%s\n\n%s" % (uninstaller, code, out)))
    # 断言 1：本次安装的壳与后台都被结束（按 pid + 创建时间）
    for role in ("install1_shell", "install1_backend"):
        item = baseline[role]
        if not item:
            rec.add("S3-011-%s" % role, "本次安装的 %s 被结束" % role, FAIL, "基线里没有 %s 的身份" % role)
            continue
        left = wait_identity_gone([item["pid"]], timeout=30)
        rec.add("S3-011-%s" % role, "本次安装的 %s 被结束（pid=%s + 创建时间核对）" % (role, item["pid"]),
                PASS if not left else FAIL,
                "exe=%s ft=%s 仍在=%s" % (item["exe"], item["created_filetime"], bool(left)))
    leftovers = processes_in_dir(ctx.install1) if ctx.install1.exists() else []
    rec.add("S3-012", "卸载后安装目录里没有残留活动进程", PASS if not leftovers else FAIL,
            describe_processes(leftovers))
    # 断言 2：用户数据目录内容不变
    after_data = dir_manifest(ctx.data1)
    same = after_data["digest"] == baseline["data1"]["digest"]
    rec.add("S3-013", "卸载保留用户数据：数据目录内容前后逐字节一致", PASS if same else FAIL,
            "before=%s(%d 文件) after=%s(%d 文件)"
            % ((baseline["data1"]["digest"] or "?")[:16], baseline["data1"]["count"],
               (after_data["digest"] or "?")[:16], after_data["count"]),
            rec.evidence("s3-data-manifest",
                         "before:\n%s\n\nafter:\n%s\n" % (json.dumps(baseline["data1"], ensure_ascii=False, indent=2)[:4000],
                                                          json.dumps(after_data, ensure_ascii=False, indent=2)[:4000])))
    # 断言 3：诱饵一个都没被动过
    decoy_now = [process_identity(p.pid) for p in decoys]
    decoy_ok = all(item is not None for item in decoy_now)
    rec.add("S3-014", "诱饵进程（映像名 qio.exe / qio-backend.exe，另一路径）一个都没被动过",
            PASS if decoy_ok else FAIL,
            "before=%s；after=%s" % (describe_processes(baseline["decoy"]), describe_processes([d for d in decoy_now if d])),
            rec.evidence("s3-decoy-compare", "before=%s\nafter=%s\n"
                         % (json.dumps(baseline["decoy"], ensure_ascii=False, indent=2),
                            json.dumps(decoy_now, ensure_ascii=False, indent=2))))
    # 断言 4：第二份安装的进程与 lease 都没被动过
    shell2_now = process_identity(proc2.pid)
    backend2_now = _process_times_and_path(int((lease2.get("backend") or {}).get("pid") or 0))
    sample_ok = (shell2_now == baseline["install2_shell"]) and (backend2_now == baseline["install2_backend"])
    rec.add("S3-015", "第二份安装的壳与后台一个都没被动过（pid + 创建时间 + exe 逐项一致）",
            PASS if sample_ok else FAIL,
            "install2 壳 before=%s after=%s；后台 before=%s after=%s"
            % (baseline["install2_shell"], shell2_now, baseline["install2_backend"], backend2_now))
    lease2_now = read_lease(ctx.install2)[1]
    rec.add("S3-016", "第二份安装的 lease 原文没被动过", PASS if lease2_now == baseline["install2_lease_raw"] else FAIL,
            "lease 仍存在=%s；原文一致=%s" % ((ctx.install2 / LEASE_NAME).is_file(),
                                          lease2_now == baseline["install2_lease_raw"]),
            rec.evidence("s3-install2-lease-compare",
                         "before:\n%s\n\nafter:\n%s\n" % (baseline["install2_lease_raw"], lease2_now)))
    # 断言 5：整个会话里没有别的 QIO 命名进程消失
    now_named = {item["pid"]: item for item in qio_named_processes()}
    vanished = []
    # 本安装实例自己的进程（含 onefile 的第二个进程）**本来就该在这次卸载里消失** ——
    # 它们不算"被误伤的别的实例"。判据用「进程映像在 install1 目录下」，不是 pid 名单。
    for item in baseline["all_qio_named"]:
        if is_under(item["exe"], ctx.install1):
            continue
        if item["pid"] not in now_named:
            vanished.append("pid=%s %s" % (item["pid"], item["exe"]))
    foreign_vanished = [text for text in vanished if not is_under(text.split(" ", 1)[1], ctx.work)]
    own_vanished = [text for text in vanished if is_under(text.split(" ", 1)[1], ctx.work)]
    state_017 = FAIL if own_vanished else (WARN if foreign_vanished else PASS)
    rec.add("S3-017", "卸载只影响本安装实例：本次装置自己的 QIO 命名进程一个都没少",
            state_017,
            ("本次装置内的：%s；" % own_vanished if own_vanished else "") +
            ("装置外（不在本脚本管理范围，可能是别的 agent 自己结束的，**无法归因**）：%s" % foreign_vanished
             if foreign_vanished else
             "基线 %d 个（除本实例外）全部还在" % len(baseline["all_qio_named"])),
            rec.evidence("s3-named-processes",
                         "baseline=%s\n\nnow=%s" % (json.dumps(baseline["all_qio_named"], ensure_ascii=False, indent=2),
                                                    json.dumps(list(now_named.values()), ensure_ascii=False, indent=2))))
    return all(bool(row["state"] == PASS) for row in rec.rows if row["id"].startswith("S3-01"))


# ---------------------------------------------------------------- 清理与汇总

def cleanup(ctx: Ctx):
    rec = ctx.rec
    # 先按 pid 收掉本次装置起的所有进程
    ctx.stop_all_tracked("按 pid 收掉本次装置启动的进程（外壳 / 诱饵 / 各安装目录内进程）")
    # 再把本次装置装的第二、三份卸载掉（第一份已经被第 3 步的卸载器清掉了）
    for path, tag in ((ctx.install2, "install2"), (ctx.install3, "install3")):
        if ctx.installer and (path / "uninstall.exe").is_file():
            code, out = ctx.run([str(path / "uninstall.exe"), "/S"], tag="cleanup-uninstall-%s" % tag,
                                timeout=900, env=ctx.installer_env())
            gone, _ = wait_for(lambda p=path: not p.exists(), timeout=ctx.args.uninstall_timeout)
            rec.add("S9-001-%s" % tag, "收尾：本次装置的%s被卸载（不留残骸）" % tag,
                    PASS if gone else WARN, "exit=%s 目录仍存在=%s" % (code, path.exists()),
                    rec.evidence("cleanup-uninstall-%s-output" % tag, "exit=%s\n%s" % (code, out)))
            for item in processes_in_dir(path):
                ctx.stop_by_pid(item["pid"], "cleanup-%s" % tag)
    restore_registry(ctx)
    if ctx.args.keep_installs:
        rec.add("S9-002", "按 --keep-installs 保留安装目录（不删）", WARN,
                "install1=%s install2=%s install3=%s" % (ctx.install1, ctx.install2, ctx.install3))
    # 本次装置在安装目录里留下的 lease（standalone 模式下安装目录就是构建目录）：
    # 只删「记录里的壳已经不在」的那些 —— 活着的实例的 lease 绝不碰。
    for directory in (ctx.install1,):
        lease_file = Path(directory) / LEASE_NAME
        if not lease_file.exists():
            continue
        lease = read_lease(directory)[0]
        owner = int(((lease or {}).get("shell") or {}).get("pid") or 0)
        if owner and process_identity(owner) is not None:
            continue
        try:
            ev = rec.copy_evidence("leftover-lease", lease_file)
            lease_file.unlink()
            rec.add("S9-003", "收尾：清掉本次/上一轮留下的 lease（外壳被强杀时按设计不删）",
                    PASS, "已删 %s（原文留档 %s）" % (lease_file, ev))
        except OSError as exc:
            rec.add("S9-003", "收尾：清掉残留 lease", WARN, "删 %s 失败：%r" % (lease_file, exc))


def write_summary(ctx: Ctx) -> int:
    rec = ctx.rec
    counts = rec.counts()
    failures = [row for row in rec.rows if row["state"] == FAIL]
    summary = {
        "script": str(Path(__file__).resolve()),
        "mode": "standalone-shell" if ctx.standalone else "installer",
        "installer": rec.meta.get("installer"),
        "lease_install1": rec.meta.get("lease_install1"),
        "host": rec.meta.get("host"),
        "work_dir": str(ctx.work),
        "install_dirs": {"install1": str(ctx.install1), "install2": str(ctx.install2), "install3": str(ctx.install3)},
        "data_dirs": {"data1": str(ctx.data1), "data2": str(ctx.data2), "data3": str(ctx.data3)},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(rec.started)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "elapsed_seconds": round(time.time() - rec.started, 1),
        "counts": counts,
        "results": rec.rows,
        "failed": len(failures),
    }
    out = ctx.work / "summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_lines = ["# 第 1 步真机桌面验收汇总", "",
                     "开始 %s，用时 %.1fs" % (summary["started_at"], summary["elapsed_seconds"]),
                     "安装包：%s" % ((rec.meta.get("installer") or {}).get("path") or "(standalone 模式，未用安装包)"),
                     "sha256：%s" % ((rec.meta.get("installer") or {}).get("sha256") or "-"),
                     "结果：%s" % counts, ""]
    for row in rec.rows:
        summary_lines.append("[%s] %-9s %s" % (row["state"], row["id"], row["title"]))
        summary_lines.append("        %s" % row["detail"])
    (ctx.work / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    log("")
    log("== 汇总（%s） ==" % ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items())))
    log("   证据目录：%s" % (ctx.work / "evidence"))
    log("   summary.json / summary.txt：%s" % ctx.work)
    if failures:
        log("   FAIL %d 条：" % len(failures))
        for row in failures:
            log("     - %s %s :: %s" % (row["id"], row["title"], row["detail"][:200]))
    return 1 if failures else 0


# ---------------------------------------------------------------- main

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="第 1 步真机桌面验收（安装 → lease → 正常退出 → 带壳卸载 → lease 写失败）")
    parser.add_argument("--installer", default="", help="NSIS 安装包（完整四步需要）")
    parser.add_argument("--shell-exe", default="", help="直接跑 release 外壳（骨架自测；与 --installer 二选一）")
    parser.add_argument("--sha256", default="", help="期望的安装包 sha256（比不上记 FAIL）")
    parser.add_argument("--commit", default="", help="期望的被测 commit（比不上记 FAIL；只记录实际值也可）")
    parser.add_argument("--work-dir", default=str(ROOT / ".e2e-desktop"), help="证据与安装目录的根（默认 repo/.e2e-desktop）")
    parser.add_argument("--install-dir", default="", help="install1 目录（默认 <work>/install）")
    parser.add_argument("--install-dir2", default="", help="第二份安装目录（默认 <work>/install2）")
    parser.add_argument("--install-dir3", default="", help="第三份安装目录（lease 写失败场景，默认 <work>/install3）")
    parser.add_argument("--stages", default="", help="逗号分隔；默认：装包模式全部；standalone 模式 preflight,lease,exit")
    parser.add_argument("--lease-timeout", type=float, default=90.0, help="等 lease 出现的秒数（默认 90）")
    parser.add_argument("--exit-timeout", type=float, default=60.0, help="等外壳正常退出的秒数（默认 60）")
    parser.add_argument("--window-timeout", type=float, default=45.0, help="等外壳主窗口出现的秒数（默认 45）")
    parser.add_argument("--uninstall-timeout", type=float, default=240.0, help="等安装目录被清掉的秒数（默认 240）")
    parser.add_argument("--broken-profile-src", default="",
                        help="坏掉的 WebView2 用户数据目录（会**复制**一份到 work-dir 里做 "
                             "WEBVIEW2_USER_DATA_FOLDER，不动原件）：验证窗口看门狗退出码 1 + QIO-WINDOW-GONE")
    parser.add_argument("--keep-installs", action="store_true", help="收尾不卸载本次装置装的第二/三份")
    parser.add_argument("--shell-env", action="append", default=[],
                        help="给外壳（及其 sidecar）追加环境变量 KEY=VALUE，可重复。"
                             "本机必须用它的两处：TEMP/TMP 指到 work-dir（真实 %%TEMP%% 下 PyInstaller 建不出临时目录）、"
                             "QIO_DISABLE_DB_CHECK=1（产品自带的隔离环境开关，见 docs/e2e-install-2026-10-02.md §4）")
    parser.add_argument("--i-know-this-installs-outside-work-dir", action="store_true",
                        help="允许安装目录在 work-dir 之外（默认拒绝，防止误装/误卸真实安装）")
    parser.add_argument("--json-only", action="store_true", help="只输出 summary.json（不打印每条断言）")
    return parser.parse_args(argv)


def artifact_commit_of(installer) -> tuple:
    """产物对应的 commit：**优先读构建清单** <installer>.build.json（构建时写死的那个），
    拿不到再退回产物所在 git 工作树的 HEAD。

    为什么不能只看工作树 HEAD：构建之后工作树还会继续提交（实测：包是 aacabd9 编的，
    而验证时那棵树已经走到 6d920f2）—— 拿 HEAD 去对被测 commit 会得出错误的 FAIL。
    """
    if installer is None:
        return "", ""
    manifest = Path(str(installer) + ".build.json")
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("commit"):
                return str(data["commit"]), "%s（构建清单）" % manifest.name
        except (OSError, ValueError):
            pass
    return git_commit(Path(installer).parent), "git HEAD(%s)" % Path(installer).parent


def git_commit(path=None) -> str:
    """某个路径所在 git 工作树 / 仓库的 HEAD。

    path 给了就用它（安装包可能来自**另一个 worktree**，例如 qio-wt-fixes）——
    拿本检出的 HEAD 去对被测 commit 会得出错误的结论。
    """
    target = str(path) if path else str(ROOT)
    try:
        proc = subprocess.run(["git", "-C", target, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30)
        return (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.installer and not args.shell_exe:
        args.shell_exe = str(ROOT / "frontend" / "src-tauri" / "target" / "release" / "qio.exe")
        log("（没有 --installer 也没有 --shell-exe：按骨架自测模式用 %s）" % args.shell_exe)
    work = Path(args.work_dir).resolve()
    rec = Recorder(work)
    ctx = Ctx(args, rec)
    stages = [s.strip() for s in (args.stages or "").split(",") if s.strip()]
    if not stages:
        stages = list(STANDALONE_STAGES if ctx.standalone else ALL_STAGES)
    unknown = [s for s in stages if s not in ALL_STAGES]
    if unknown:
        log("未知 stage：%s（可用：%s）" % (unknown, ALL_STAGES))
        return 2
    stages = [s for s in EXEC_ORDER if s in stages]
    for stage in ALL_STAGES:
        if stage not in stages:
            rec.add("S0-010-%s" % stage, "stage %s 本次没有执行" % stage, SKIP,
                    "NOT VERIFIED：不在本次 --stages（%s）里" % ",".join(stages))
    commit = git_commit(ROOT)
    artifact_commit, artifact_src = artifact_commit_of(ctx.installer)
    rec.meta["commit"] = commit
    rec.meta["artifact_commit"] = artifact_commit
    rec.meta["artifact_commit_source"] = artifact_src
    rec.meta["stages"] = stages
    log("== 第 1 步真机桌面验收 ==")
    log("   work-dir : %s" % work)
    log("   mode     : %s" % ("standalone-shell" if ctx.standalone else "installer"))
    log("   stages   : %s" % ", ".join(stages))
    log("   commit   : 检出 %s / 产物所在树 %s" % (commit or "?", artifact_commit or "?"))
    if args.commit:
        # 被测 commit 以**产物所在的树**为准（安装包可能来自另一个 worktree）
        actual = artifact_commit or commit
        rec.add("S0-000", "被测 commit 与 Lead 给的一致", PASS if actual.startswith(args.commit) else FAIL,
                "产物 commit %s（来源：%s）/ 检出 HEAD %s / 期望 %s"
                % (artifact_commit or "(取不到)", artifact_src or "?", commit or "(取不到)", args.commit))
    else:
        rec.add("S0-000", "被测 commit（未提供期望值，只记录实际值）", WARN,
                "产物所在树 %s / 检出 %s（NOT VERIFIED：没有可比对的期望值）"
                % (artifact_commit or "(取不到)", commit or "(取不到)"))
    # stage 之间只保留**真依赖**：装不上 → lease/卸载无从谈起；lease 没出现 → 退出路径无从谈起。
    # 反过来，第 2 步失败（例如没有主窗口）不该把**独立的**第 3/4 步一起跳过 —— 那会把
    # "没验" 混成 "没跑"。
    ready = {"install": True, "lease": True}
    try:
        if "preflight" in stages:
            ready["install"] = stage_preflight(ctx)
        if "install" in stages and ready["install"]:
            ready["install"] = stage_install(ctx)
        if "lease" in stages and ready["install"]:
            ready["lease"] = stage_lease(ctx)
        if "exit" in stages and ready["lease"]:
            stage_exit(ctx)
        if "leasefail" in stages and ready["install"]:
            stage_leasefail(ctx)
        if "windowfail" in stages and ready["install"]:
            stage_windowfail(ctx)
        if "uninstall" in stages and ready["install"]:
            stage_uninstall(ctx)
    finally:
        try:
            cleanup(ctx)
        except Exception as exc:  # noqa: BLE001 - 收尾失败必须留痕，但不能吞掉上面的结论
            rec.add("S9-999", "收尾时异常（按 pid 清理可能没做完）", FAIL, "%r" % (exc,))
        rc = write_summary(ctx)
    return rc


if __name__ == "__main__":
    sys.exit(main())
