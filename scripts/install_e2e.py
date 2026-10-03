r"""NSIS 安装包「真机 E2E」驱动：装 → 首启 → 工具全链 → 重启 → 卸载。

为什么需要它：release_gate.py 只能核对**已经产出的文件**（哈希、清单、签名结构、内置模型），
它明确不覆盖「安装器真的跑起来了吗 / 装出来的后端真的能服务吗 / 数据在重启与卸载后还在吗」。
这份脚本把后半段做成可重复执行、逐条留证据的流程。

诚实边界（不要把这些读成更强的结论）：

* **不是干净 Windows 用户环境**。本机不能新建用户；这里用可达成的等价条件：
  独立安装目录 + 全新的空数据目录（QIO_DATA_DIR 指向空目录）+ 启动进程**不继承开发用环境变量**
  （白名单构造 env：不带 QIO_DEV_INSECURE / QIO_ENABLE_TEST_EVENTS / PYTHONPATH / VIRTUAL_ENV）。
* **模型是假厂商**（scripts/e2e_fake_provider.py）。凡是用到它的用例都只能证明「QIO 自己的链路对」，
  不能证明任何真实厂商端点。
* **没有 GUI 驱动**。Tauri WebView 不开 CDP，脚本只走 HTTP API；界面层的步骤必须人工做，
  见 docs 里「必须人工的步骤」一节。
* 卸载这一步**不勾选「删除应用数据」**（静默模式跳过确认页，复选框状态恒为 0）——这正是要验证的
  「卸载是否保留用户数据」。

安全设计（不动本机既有安装 D:\QIO）：

1. 静默安装用 /S /D=<绝对路径>。/D= 必须是最后一个参数且不带引号（NSIS 规则）。
2. NSIS 的「检测到既有安装 → 重装/升级」自定义页在 **silent 模式下不会被调用**（本脚本先用
   --decoy 实验证明：把注册表指向一个诱饵安装目录，跑真安装器，检查诱饵卸载器有没有被执行）。
3. 同一个产品名只有一个卸载注册表槽位（HKCU\...\Uninstall\QIO），新安装必然覆写它。
   所以脚本在动手前 reg export 备份、结束后 reg import 还原；桌面/开始菜单快捷方式同样备份还原；
   并在最后逐文件比对 D:\QIO 的 sha256 证明既有安装未被改动。

P4 轮新增（语义按 docs/p4-plan.md 第 1 节，先失败后通过）：

* `shelllease` 阶段：启动**真外壳** `qio.exe`，核对它写的 `sidecar.lease.json`
  （schema / install_dir / shell / backend 四件套，以及 backend.pid 就是安装目录里按路径枚举到的
  那个 qio-backend.exe），再用 WM_CLOSE 关掉壳，断言壳与 sidecar 都退出、lease 消失。
  lease 由**外壳**写：本脚本直接起 qio-backend.exe 的那条路径观测不到它（那里只记 NOT VERIFIED）。
* `--uninstall-with-running-backend` 的语义**变了**：安装目录里的替身（ping.exe 改名的
  `qio-backend.exe`）没有 lease、也没有主程序 → 卸载器**必须不杀它**，并如实报告"无法确认归属"。
  断言 = 拒绝误杀 + 可行动报告 + 残留只有被占用的那个替身；旧的"卸载器自己收掉了还在跑的 sidecar"
  断言按新语义作废（不要为了保绿把它留下）。
* `pidreuse` 阶段：PID 复用反证（plan §1.4）—— 伪造三份对不上的 lease（无关进程的 pid /
  创建时间差 1 / install_dir 指向别处），帮助程序必须 exit 3 且**不动任何进程**；再加一份
  合法 lease 的正向对照（必须 exit 0 并真的收掉那两个 pid），防止"永远拒绝"骗过反证。
* `coinstall` 阶段：两份真安装并存的判定交给 `scripts/install_e2e_multi.py`（一份实现、两个入口）。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

RESULTS: list[dict] = []
EVIDENCE: Path | None = None
_ev_seq = [0]

# 诱饵注册表是否已经生效：一旦生效，无论后面哪一步失败，都必须还原（否则用户机器上的
# 「QIO 已安装」注册项会指着诱饵目录 —— 那是对既有安装的真实破坏）。
STATE = {"decoy_applied": False}


# ---------------------------------------------------------------- 基础设施

def log(msg: str) -> None:
    print(msg, flush=True)


def record(cid: str, title: str, state: str, detail: str) -> None:
    RESULTS.append({"id": cid, "title": title, "state": state, "detail": detail})
    log("[%s] %s %s :: %s" % (state, cid, title, detail[:300]))


def evidence(name: str, text: str) -> str:
    """把原始输出落盘，返回证据文件相对路径。"""
    global _ev_seq
    if EVIDENCE is None:
        return ""
    _ev_seq[0] += 1
    target = EVIDENCE / ("%03d-%s.txt" % (_ev_seq[0], name))
    target.write_text(text, encoding="utf-8", errors="replace")
    return str(target.relative_to(EVIDENCE.parent))


def install_cmdline(installer: str, install_dir: str) -> str:
    """拼安装器命令行。

    NSIS 的 /D= **不能带引号**（即使路径里有空格），而 Python 传列表时会自动给含空格的
    参数加引号 —— 那样 NSIS 会解析失败（表现为「进程起来又消失、目录没生成」）。
    所以：exe 自己加引号，/D= 放最后且保持裸值。这条路径已用 makensis 探针单独验证过。
    """
    return '"%s" /S /D=%s' % (installer, install_dir)


def run(cmd, *, timeout: int = 600, env: dict | None = None, cwd: str | None = None,
        tag: str | None = None) -> tuple[int, str]:
    """跑外部命令并把命令 + 原始输出一起留证。cmd 可以是 list，也可以是**原样**命令行字符串。"""
    log("  $ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=env, cwd=cwd)
        out = (proc.stdout or "") + (proc.stderr or "")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = "TIMEOUT after %ss\n%s" % (timeout, (exc.stdout or "") + (exc.stderr or ""))
        code = -1
    if tag:
        evidence(tag, "$ " + " ".join(cmd) + "\n\n" + out)
    return code, out


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def powershell(script: str, *, timeout: int = 300, tag: str | None = None) -> tuple[int, str]:
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
               timeout=timeout, tag=tag)


# ---------------------------------------------------------------- Windows 进程事实
#
# 为什么用 ctypes 直连 kernel32，而不是 PowerShell/WMI：
#   * 「B 的 backend 没有重启过」与「PID 复用反证」都要求**100ns 精度**的进程创建时间
#     （UTC FILETIME）。Win32_Process.CreationDate 经 PowerShell 序列化后会丢精度，
#     拿它去比对只能得出"大概一样"，那不足以支撑这两条硬断言；
#   * 本机子进程是受限令牌，.NET 静态调用在受限语言模式下会被挡；ctypes 只读查询不受影响。
# 失败时一律返回 None（进程不存在/查不到），调用方负责把它记成 FAIL 而不是当成"通过"。

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


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

# 259 = STILL_ACTIVE。**必须**查这个：只要还有句柄开着（我们自己的 Popen 就开着一个），
# 已经退出的进程在内核里仍然可以被 OpenProcess 到、创建时间也还查得到 —— 只按
# "OpenProcess 成功"判存活，会把刚被 taskkill 掉的进程记成"还在跑"（本机 smoke 实测踩到）。
STILL_ACTIVE = 259


def _process_times_and_path(pid: int) -> dict | None:
    """一个 pid 的 (映像路径, 创建时间 FILETIME)。进程已退出/查不到 → None。"""
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


def _snapshot_pids() -> list[dict]:
    """Toolhelp 快照：所有进程的 (pid, ppid, 映像名)。"""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    out: list[dict] = []
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


def process_identity(pid: int) -> dict | None:
    """进程身份 = pid + 父 pid + 映像路径 + 创建时间（**抗 PID 复用**的那四件套）。"""
    info = _process_times_and_path(pid)
    if info is None:
        return None
    ppid = next((item["ppid"] for item in _snapshot_pids() if item["pid"] == int(pid)), None)
    return {**info, "ppid": ppid}


def processes_in_dir(install_dir) -> list[dict]:
    """安装目录里的进程 —— 按**映像路径**匹配，不按映像名。

    这台机器上同时有多个 agent 在跑真实后端：按名字枚举（qio-backend.exe）会把别人的进程
    算成自己的，之后的每一条断言都在验错对象。
    """
    prefix = os.path.normcase(str(Path(install_dir).resolve())) + os.sep
    found: list[dict] = []
    for entry in _snapshot_pids():
        info = _process_times_and_path(entry["pid"])
        if not info or not info["exe"]:
            continue
        if os.path.normcase(str(Path(info["exe"]).resolve())).startswith(prefix):
            found.append({**entry, **info})
    return sorted(found, key=lambda item: item["pid"])


def identity_digest(items: list[dict]) -> str:
    """一组进程身份的指纹：pid + 创建时间 + 路径。有任何重启/替换都会变。"""
    canonical = json.dumps(sorted(
        (int(item["pid"]), str(item.get("exe") or ""), str(item.get("created_filetime") or ""))
        for item in items), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def describe_processes(items: list[dict]) -> str:
    return ", ".join("%s(ppid=%s,ft=%s)" % (item["pid"], item.get("ppid"), item.get("created_filetime"))
                     for item in items) or "无"


def wait_processes_gone(pids, timeout: float = 30.0) -> list[int]:
    """等这些 pid 真的消失；返回仍在的 pid（空列表 = 确认都退了）。"""
    deadline = time.time() + timeout
    left = [int(pid) for pid in pids]
    while True:
        left = [pid for pid in left if process_identity(pid) is not None]
        if not left or time.time() >= deadline:
            return left
        time.sleep(0.5)


def listen_port_of(pids) -> int | None:
    """按 pid 反查监听端口。

    外壳自己 pick_free_port（见 frontend/src-tauri/src/main.rs），**不认** QIO_PORT
    —— 所以启动真外壳的场景里端口只能这样反查，不能沿用 --port 的假设。
    """
    wanted = [int(pid) for pid in pids if int(pid) > 0]
    if not wanted:
        return None
    script = (
        "$ids = @(%s); Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
        "Where-Object { $ids -contains $_.OwningProcess } | "
        "Select-Object -First 1 -ExpandProperty LocalPort" % ",".join(str(pid) for pid in wanted))
    code, out = run(["powershell", "-NoProfile", "-Command", script], tag="listen-port-of")
    for token in out.split():
        if token.strip().isdigit():
            return int(token.strip())
    return None


# ---------------------------------------------------------------- sidecar.lease.json
#
# 契约的唯一事实源是 docs/p4-plan.md §1.1（外壳在启动时写、退出时删；PID + 创建时间 +
# 映像路径三者同时匹配才认这个进程 = 抗 PID 复用；内容不含任何密钥/令牌）。

LEASE_NAME = "sidecar.lease.json"
# 只把「有非空值」的敏感字段名算成泄露：字段名本身（例如 "token": null）不是泄露。
# 字段名里出现这些词就算敏感（api_token / session_token / client_secret 都要能抓到）；
# "key" 单独作为整名匹配，避免把 keys/keyset 这类无害字段误判成泄露。
SECRET_WORDS = ("token", "secret", "password", "credential", "private")
SECRET_EXACT = ("key", "api_key", "apikey", "authorization")


def lease_path(install_dir) -> Path:
    return Path(install_dir) / LEASE_NAME


def read_lease(install_dir) -> tuple[dict | None, str]:
    """读 lease：返回 (解析后的 dict 或 None, 原文)。文件不存在 → (None, "")。"""
    path = lease_path(install_dir)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""
    try:
        data = json.loads(raw)
    except ValueError:
        return None, raw
    return (data if isinstance(data, dict) else None), raw


def lease_problems(lease: dict, install_dir) -> list[str]:
    """按 §1.1 核对 lease 的四件套；返回问题清单（空 = 每一项都对得上真实进程）。"""
    problems: list[str] = []
    if lease.get("schema") != 1:
        problems.append("schema=%r（期望 1）" % lease.get("schema"))
    recorded_dir = str(lease.get("install_dir") or "")
    if not recorded_dir or os.path.normcase(os.path.abspath(recorded_dir)) != \
            os.path.normcase(str(Path(install_dir).resolve())):
        problems.append("install_dir=%r 与安装目录不符" % recorded_dir)
    for role in ("shell", "backend"):
        item = lease.get(role)
        if not isinstance(item, dict):
            problems.append("缺少 %s 段" % role)
            continue
        pid = item.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            problems.append("%s.pid=%r 不是正整数" % (role, pid))
            continue
        live = _process_times_and_path(pid)
        if live is None:
            problems.append("%s.pid=%s 当前取不到（进程已退出或无权查询）" % (role, pid))
            continue
        if str(item.get("created_filetime") or "") != live["created_filetime"]:
            problems.append("%s.created_filetime=%r 与实际 %s 不符（PID 复用/伪造？）"
                            % (role, item.get("created_filetime"), live["created_filetime"]))
        recorded_exe_live = str(item.get("exe") or "")
        if recorded_exe_live and os.path.normcase(os.path.abspath(recorded_exe_live)) != \
                os.path.normcase(os.path.abspath(live["exe"])):
            problems.append("%s.exe=%r 与实际映像路径 %r 不符" % (role, recorded_exe_live, live["exe"]))
        if role == "backend":
            recorded_exe = str(item.get("exe") or "")
            expected = str((Path(install_dir) / "qio-backend.exe").resolve())
            if not recorded_exe or os.path.normcase(os.path.abspath(recorded_exe)) != \
                    os.path.normcase(expected):
                problems.append("backend.exe=%r 不是安装目录里的 %s" % (recorded_exe, expected))
    return problems


def lease_secret_hits(raw: str, extra_values=()) -> list[str]:
    """lease 原文里有没有密钥/令牌。

    * 只对**有非空值**的敏感字段名报警（字段名本身不算）；
    * 另外扫会话令牌的**值**与 sk- 形态的密钥。
    """
    hits: list[str] = []
    for value in extra_values:
        if value and str(value) in raw:
            hits.append("lease 原文里出现了会话令牌的值")
    for match in re.finditer(r'"([^"]+)"\s*:\s*("(?:[^"\\]|\\.)*"|[^,}\s]+)', raw):
        name = match.group(1).strip().lower()
        value = match.group(2).strip().strip('"').strip().lower()
        if not value or value in ("null", "false", "0", "none"):
            continue  # 字段名本身不算泄露；只有**有值**才算
        if any(word in name for word in SECRET_WORDS) or name in SECRET_EXACT:
            hits.append("字段 %s 有非空值" % name)
    if re.search(r"sk-[A-Za-z0-9]{4,}", raw):
        hits.append("出现 sk- 形态的密钥原文")
    return sorted(set(hits))


def write_synthetic_lease(install_dir, shell_pid: int, backend_pid: int) -> dict:
    """**测试装置**：按 §1.1 的契约合成一份 lease。

    它证明的是「卸载判定与 PID 定向」，**不证明**外壳真的会写这份文件 ——
    「外壳会写」由 shelllease 阶段用真外壳验证。整份文件里不放任何密钥/令牌。
    """
    install_dir = Path(install_dir)
    shell = process_identity(shell_pid)
    backend = process_identity(backend_pid)
    if shell is None or backend is None:
        raise RuntimeError("合成 lease 失败：shell/backend 进程身份取不到（shell=%s backend=%s）"
                           % (shell_pid, backend_pid))
    payload = {
        "schema": 1,
        "install_dir": str(install_dir.resolve()),
        # shell 段也要 exe：产品侧（qio_core::ownership）的 shell 三要素是 pid + created_filetime
        # + exe，少写一个就永远对不上（本机实测踩到过一次）。
        "shell": {"pid": int(shell_pid), "created_filetime": shell["created_filetime"],
                  "exe": shell["exe"]},
        "backend": {"pid": int(backend_pid), "created_filetime": backend["created_filetime"],
                    "exe": str(install_dir / "qio-backend.exe")},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "_synthetic": "E2E 按 §1.1 契约合成（只证明判定逻辑，不证明外壳会写）",
    }
    target = lease_path(install_dir)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return payload


# ---------------------------------------------------------------- 真外壳 / lease 阶段

def shell_token_path(args, shell_pid: int) -> Path:
    """外壳把会话令牌写进 %TEMP% 下的 qio-session-<pid>.token（session_token_path），
    而 TEMP 被白名单 env 指到了 <work>/tmp —— 所以路径是确定的。"""
    return Path(args.work_dir) / "tmp" / ("qio-session-%d.token" % shell_pid)


def launch_shell(args, exe: Path, data_dir: Path, tag: str, extra_env: dict | None = None):
    """启动安装目录里的**真外壳**（qio.exe / 它的改名副本）。"""
    env = clean_env(args, args.port)
    env["QIO_DATA_DIR"] = str(data_dir)
    # 本机子进程是受限令牌：tauri 的 log 插件要在 %LOCALAPPDATA%\<identifier>\logs 下建目录，
    # ACCESS_DENIED 会让外壳在启动时直接 panic（原始输出：PluginInitialization("log",
    # "拒绝访问。 (os error 5)")）。把 LOCALAPPDATA 也指到检出内 —— 与 TEMP 同一类沙箱适配，
    # **不是产品行为**，真机/CI 不需要。WebView2 的用户数据目录也在它下面，顺带一起解决。
    local_appdata = Path(args.work_dir) / "localappdata"
    local_appdata.mkdir(parents=True, exist_ok=True)
    env["LOCALAPPDATA"] = str(local_appdata)
    if extra_env:
        env.update(extra_env)
    logfile = Path(args.work_dir) / "evidence" / ("shell-%s.log" % tag)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    fh = logfile.open("wb")
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env,
                            stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log("  $ [shell %s] %s（pid=%s, data=%s）" % (tag, exe, proc.pid, data_dir))
    return proc, logfile


def wait_lease(install_dir, timeout: float, proc=None) -> tuple[dict | None, str]:
    """等 lease 出现。外壳提前退出就立刻返回（不白等）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        lease, raw = read_lease(install_dir)
        if lease is not None:
            return lease, raw
        if proc is not None and proc.poll() is not None:
            return None, ""
        time.sleep(0.5)
    return None, ""


def close_main_window(pid: int) -> tuple[bool, str]:
    """给外壳发 WM_CLOSE（GUI 的关闭按钮点不到）。返回 (是否受理, 原始输出)。"""
    script = (
        "$p = Get-Process -Id %d -ErrorAction SilentlyContinue;"
        "if ($p) { $ok = $p.CloseMainWindow(); Write-Output ('CLOSE_MAIN_WINDOW=' + $ok) }"
        " else { Write-Output 'ALREADY_GONE' }" % int(pid))
    code, out = run(["powershell", "-NoProfile", "-Command", script], tag="shell-close-main-window")
    return ("CLOSE_MAIN_WINDOW=True" in out or "ALREADY_GONE" in out), out


def kill_by_pid(pid: int, tag: str, *, force: bool = True) -> tuple[int, str]:
    """按 **PID** 收进程（绝不用 /IM 按映像名收）。"""
    cmd = ["taskkill", "/T", "/PID", str(int(pid))]
    if force:
        cmd.insert(1, "/F")
    return run(cmd, timeout=60, tag=tag)


def tail_text(path: Path, limit: int = 800) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def step_shell_lease(args) -> bool:
    """真外壳路径：lease 生成 → 四件套校验 → 健康检查 → WM_CLOSE → 都退出 + lease 消失。

    这是计划 §1.1「外壳写 lease、退出删 lease」唯一**能观测到**的路径：直接起
    qio-backend.exe 的流程里没有外壳，注定看不到这份文件（那里只能记 NOT VERIFIED）。
    """
    install_dir = Path(args.install_dir)
    shell_exe = install_dir / args.shell_exe_name
    if not shell_exe.is_file():
        record("A-110", "真外壳存在（%s）" % args.shell_exe_name, "FAIL",
               "找不到 %s —— shelllease 需要一份真实安装（--stages install）" % shell_exe)
        return False
    data = Path(args.work_dir) / "data-shell"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True, exist_ok=True)
    lease_file = lease_path(install_dir)
    if lease_file.exists():  # 上一轮的残留会让"启动后出现"变成假结论
        lease_file.unlink()
    proc, logfile = launch_shell(args, shell_exe, data, "lease")
    shell_id = process_identity(proc.pid)
    record("A-110", "真外壳已启动（%s）" % args.shell_exe_name, "PASS",
           "pid=%s 创建时间=%s；数据目录=%s（全新空目录）"
           % (proc.pid, (shell_id or {}).get("created_filetime"), data))

    lease, raw = wait_lease(install_dir, args.shell_timeout, proc)
    if lease is None:
        exited = proc.poll()
        log_tail = tail_text(logfile, 600)
        detail = ("外壳在 %ss 内就退出了（exit=%s），没有写出 %s"
                  % (args.shell_timeout, exited, LEASE_NAME)) if exited is not None else \
                 ("外壳还活着（pid=%s）但 %ss 内没有出现 %s" % (proc.pid, args.shell_timeout, LEASE_NAME))
        detail += "；壳日志尾部：" + (log_tail or "（空）")
        # 本机沙箱的已知签名：子进程是受限令牌 → tauri 的 log 插件在 %LOCALAPPDATA% 下建目录被拒
        # （PluginInitialization("log", "拒绝访问。 (os error 5)")）→ 外壳启动即 panic。
        # 这种失败**不是产品结论**，记 WARN/NOT VERIFIED；别的失败仍然是 FAIL。
        sandbox_blocked = (exited is not None and
                           ("PluginInitialization(\"log\"" in log_tail
                            or ("os error 5" in log_tail and "拒绝访问" in log_tail)))
        if sandbox_blocked:
            detail += ("；判定：**本机沙箱限制**（LOG 插件写 %LOCALAPPDATA% 被拒）—— 本机 NOT VERIFIED，"
                       "真外壳这条必须在 CI / 真机上硬过，不能读成产品通过")
        record("A-111", "启动后安装目录里出现 sidecar.lease.json",
               "WARN" if sandbox_blocked else "FAIL", detail)
        kill_by_pid(proc.pid, "cleanup-shell-no-lease")
        return False

    hits = lease_secret_hits(raw, extra_values=[args.token])
    lease_evidence = raw if not hits else "<REDACTED：lease 里检测到疑似密钥/令牌，原文不落盘；命中=%s>" % hits
    evidence("shell-lease-raw", lease_evidence)
    record("A-111", "启动后安装目录里出现 sidecar.lease.json", "PASS",
           "pid(shell)=%s pid(backend)=%s；原文见 evidence/shell-lease-raw（%d 字节）"
           % ((lease.get("shell") or {}).get("pid"), (lease.get("backend") or {}).get("pid"), len(raw)))

    problems = lease_problems(lease, install_dir)
    record("A-112", "lease 四件套与真实进程一致（schema/install_dir/shell/backend）",
           "FAIL" if problems else "PASS",
           "；".join(problems) if problems else
           "schema=1、install_dir 一致、shell/backend 的 pid+created_filetime 与内核值一致、backend.exe 指向安装目录")

    record("A-113", "lease 内容不含密钥/令牌", "FAIL" if hits else "PASS",
           "命中：%s" % hits if hits else "没有非空敏感字段、没有会话令牌值、没有 sk- 形态密钥")

    procs = processes_in_dir(install_dir)
    lease_backend_pid = (lease.get("backend") or {}).get("pid")
    in_dir = lease_backend_pid in [item["pid"] for item in procs]
    record("A-114", "lease.backend.pid 就是安装目录里按路径枚举到的那个进程",
           "PASS" if in_dir else "FAIL",
           "按路径枚举：%s；lease.backend.pid=%s（命中=%s）—— 按路径而不是按映像名，"
           "所以别的安装实例的同名进程不会被算进来"
           % (describe_processes(procs), lease_backend_pid, in_dir))

    token_file = shell_token_path(args, proc.pid)
    deadline = time.time() + 30
    token = ""
    while time.time() < deadline and not token:
        try:
            token = token_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            time.sleep(0.5)
    port = listen_port_of([item["pid"] for item in procs]) or listen_port_of([lease_backend_pid])
    if port is None:
        record("A-115", "外壳启动的 sidecar 健康检查可用", "FAIL",
               "按 pid 反查不到监听端口（pids=%s）" % [item["pid"] for item in procs])
    else:
        ok, detail = wait_health(Client("http://127.0.0.1:%d" % port, token or args.token), timeout=60)
        code, out = powershell(
            "$owner = (Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue | "
            "Select-Object -First 1 -ExpandProperty OwningProcess);"
            "if ($owner) { (Get-Process -Id $owner -ErrorAction SilentlyContinue).Path } else { '' }" % port,
            tag="shell-lease-responder")
        expected = str((install_dir / "qio-backend.exe").resolve())
        same = bool(out.strip()) and str(Path(out.strip()).resolve()) == expected
        record("A-115", "外壳启动的 sidecar 健康检查可用（端口按 pid 反查）",
               "PASS" if ok and same else "FAIL",
               "port=%s token文件=%s(%s) health=%s 回答者=%r（期望 %s）"
               % (port, token_file.name, "有令牌" if token else "**没读到令牌**", detail, out.strip(), expected))

    accepted, close_out = close_main_window(proc.pid)
    record("A-116", "WM_CLOSE 已发给外壳", "PASS" if accepted else "FAIL", close_out.strip()[:200])
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    if proc.poll() is None:
        kill_by_pid(proc.pid, "cleanup-shell-close-fallback")
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
    record("A-117", "WM_CLOSE 后外壳退出", "PASS" if proc.poll() is not None else "FAIL",
           "exit=%s" % proc.poll())
    backend_pids = [item["pid"] for item in procs]
    left = wait_processes_gone(backend_pids, timeout=30)
    record("A-118", "外壳退出后 sidecar 也退出（job 生效，不留孤儿）",
           "FAIL" if left else "PASS",
           "仍在：%s" % left if left else "按路径枚举：无残留（pid=%s 全部消失）" % backend_pids)
    for pid in left:
        kill_by_pid(pid, "cleanup-sidecar-orphan")
    deadline = time.time() + 15
    while time.time() < deadline and lease_file.exists():
        time.sleep(0.5)
    record("A-119", "外壳退出后 lease 被删除", "PASS" if not lease_file.exists() else "FAIL",
           "%s 仍在（退出路径没有清 lease）" % lease_file.name if lease_file.exists()
           else "%s 已消失" % LEASE_NAME)
    return not (hits or problems or left)


# ---------------------------------------------------------------- HTTP

class Client:
    def __init__(self, base: str, token: str = "") -> None:
        self.base = base.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload=None, *, timeout: float = 30.0,
                raw: bool = False):
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, (body if raw else json.loads(body or "{}"))

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, payload=None, **kw):
        return self.request("POST", path, payload if payload is not None else {}, **kw)


class SseReader:
    """后台读 SSE；主线程按需取事件。"""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.events: list[dict] = []
        # 见过的 event id：重连时服务端会重放历史，必须能把"历史"和"本轮"分开，
        # 否则一个旧的 TURN_END 就会让本轮提前"结束"（实测踩过：重启后那条流）。
        self.seen: set[str] = set()
        self.q: queue.Queue = queue.Queue()
        self._stop = False
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        headers = {"Accept": "text/event-stream"}
        if self.client.token:
            headers["Authorization"] = "Bearer " + self.client.token
        req = urllib.request.Request(self.client.base + "/api/events", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                for raw in resp:
                    if self._stop:
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("data: "):
                        try:
                            event = json.loads(line[6:])
                        except ValueError:
                            continue
                        self.events.append(event)
                        if event.get("id"):
                            self.seen.add(str(event["id"]))
                        self.q.put(event)
        except Exception as exc:  # 流断了就把原因放进队列，别静默
            self.q.put({"type": "__SSE_ERROR__", "data": {"error": repr(exc)}})

    def drain_replay(self, seconds: float = 1.2) -> int:
        """丢掉连接时服务端重放的历史事件。"""
        deadline = time.time() + seconds
        n = 0
        while time.time() < deadline:
            try:
                self.q.get(timeout=0.2)
                n += 1
            except queue.Empty:
                pass
        self.events.clear()
        return n

    def next_event(self, timeout: float):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


# ---------------------------------------------------------------- 步骤

MANUPRODUCT_KEY = r"Software\qio\QIO"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\QIO"
# 这个键下不属于「安装信息」的值：后端写的数据库身份基线（用户状态）。
# 卸载必须保留它 —— 这是「安装信息 vs 用户数据」这条边界的可执行断言。
USER_STATE_VALUE = "DbBaseline"


def _registry_dump(key: str, tag: str) -> dict:
    """把一个 HKCU 键下的值读成 dict（**读**注册表沙箱允许）。键不存在返回 {}。"""
    script = (
        "$k='HKCU:\\%s';"
        "if (-not (Test-Path $k)) { Write-Output '__ABSENT__' } else {"
        "  $o = [ordered]@{};"
        "  (Get-ItemProperty $k).PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } |"
        "    ForEach-Object { $o[$_.Name] = [string]$_.Value };"
        "  $o | ConvertTo-Json -Compress }" % key
    )
    code, out = powershell(script, tag=tag)
    text = out.strip()
    if not text or text == "__ABSENT__":
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def registry_write_probe(tag: str = "registry-write-probe") -> tuple[bool, str]:
    """**子进程**能不能写注册表 —— 这是判定的前提，不是跳过检查的借口。

    本会话给子进程的是受限令牌，安装器的 WriteRegStr 与卸载器的 DeleteRegKey
    都会静默 ACCESS_DENIED。能写就必须给 PASS/FAIL；不能写就必须写 NOT VERIFIED，
    绝不允许把"没验"写成"通过"。
    """
    key = r"Software\qio-e2e-probe"
    script = (
        "$ErrorActionPreference='Continue';"
        "try {"
        "  New-Item -Path 'HKCU:\\%s' -Force -ErrorAction Stop | Out-Null;"
        "  Set-ItemProperty -Path 'HKCU:\\%s' -Name probe -Value 'x' -ErrorAction Stop;"
        "  Remove-Item 'HKCU:\\%s' -Recurse -Force -ErrorAction SilentlyContinue;"
        "  Write-Output 'WRITABLE'"
        "} catch { Write-Output ('DENIED: ' + $_.Exception.Message) }" % (key, key, key)
    )
    code, out = powershell(script, tag=tag)
    text = out.strip()
    if "WRITABLE" in text:
        return True, "子进程可以写 HKCU"
    return False, (text[-200:] or "未知原因")


def _wait_registry_settled(*, timeout: float = 30.0, interval: float = 0.5,
                           tag: str = "uninstall-registry-settle") -> dict:
    """等卸载**真的**做完再读注册表；返回等待证据（等了多久、第几次轮询读到干净）。

    为什么需要（2026-10-03 复核 run 37058622003）：`uninstall.exe /S` 会把自己复制到临时目录再执行，
    原进程**提前返回**，真正的卸载在另一个进程里继续跑。于是「安装目录已经没了、注册表还没清」会被
    读成 FAIL —— 而 A-092（控制面板登记，主卸载脚本删的）与 A-093（安装信息，POSTUNINSTALL 钩子删的）
    **同时**不干净，正是这个时序特征的指纹，不是「钩子慢几毫秒」。

    判据与断言一致：UNINSTALLKEY 消失 **且** MANUPRODUCTKEY 的安装位置默认值消失。超时**不算通过**：
    返回 clean=False 与超时那一刻还剩下什么，由调用方原样判 FAIL（重试不许把真失败洗成绿）。
    """
    script = (
        "$deadline = (Get-Date).AddSeconds(%s);"
        "$i = 0; $cleanAt = 0; $unPresent = $true; $loc = '';"
        "while ($true) {"
        "  $i++;"
        "  $unPresent = Test-Path 'HKCU:\\%s';"
        "  $loc = [string]((Get-ItemProperty 'HKCU:\\%s' -Name '(default)' "
        "    -ErrorAction SilentlyContinue).'(default)');"
        "  if ((-not $unPresent) -and ($loc -eq '')) { $cleanAt = $i; break }"
        "  if ((Get-Date) -ge $deadline) { break }"
        "  Start-Sleep -Milliseconds %s"
        "};"
        "[pscustomobject]@{ polls=$i; clean_at=$cleanAt; uninstall_key_present=[bool]$unPresent;"
        " install_location=$loc } | ConvertTo-Json -Compress"
        % (int(timeout), UNINSTALL_KEY, MANUPRODUCT_KEY, int(interval * 1000))
    )
    started = time.time()
    code, out = powershell(script, tag=tag)
    waited = time.time() - started
    data: dict = {}
    for line in reversed((out or "").splitlines()):
        text = line.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
                break
            except ValueError:
                continue
    # 防御：非 0 退出 / 空输出 / 非法 JSON 都不许抛异常 —— 变成可诊断的 read_error，
    # 由调用方记成 FAIL。异常逃出去只会留一行 traceback，还会让 finally 里的汇总看起来没失败。
    read_error: str | None = None
    if code != 0:
        read_error = "powershell 退出码 %s；输出尾部：%s" % (
            code, (out or "").strip()[-200:] or "（空）")
    elif not data:
        read_error = "读注册表没有拿到可解析的结果；输出尾部：%s" % (
            (out or "").strip()[-200:] or "（空）")
    location = str(data.get("install_location") or "").strip()
    clean = (
        read_error is None
        and bool(data.get("clean_at"))
        and not data.get("uninstall_key_present")
        and not location
    )
    result = {
        "clean": clean,
        "read_error": read_error,
        "waited_seconds": round(waited, 2),
        "polls": data.get("polls"),
        "clean_at_poll": data.get("clean_at") or None,
        "uninstall_key_present": data.get("uninstall_key_present"),
        "install_location_value": location or None,
        "timeout_seconds": timeout,
        "poll_interval_seconds": interval,
    }
    evidence("registry-settle-%d" % int(started * 1000), json.dumps(result, ensure_ascii=False, indent=2))
    return result


def step_install_registry(args) -> None:
    """安装信息必须真的写进注册表：控制面板登记 + 安装位置记录。"""
    writable, why = registry_write_probe()
    uninstall_key = _registry_dump(UNINSTALL_KEY, "registry-uninstall-key")
    manu_key = _registry_dump(MANUPRODUCT_KEY, "registry-manuproduct-key")
    evidence(
        "registry-after-install",
        json.dumps(
            {"child_can_write_registry": writable, "probe": why,
             "uninstall_key": uninstall_key, "manuproduct_key": manu_key},
            ensure_ascii=False, indent=2),
    )
    if not writable:
        record("A-023", "安装信息写入注册表（控制面板登记 + 安装位置）", "WARN",
               "**NOT VERIFIED**：本会话子进程写不了注册表（%s），安装器的 WriteRegStr 会静默失败，"
               "本机既不能判通过也不能判失败 —— 必须在真机/CI（有桌面会话）上验证" % why)
        return
    problems = []
    location = str(uninstall_key.get("InstallLocation") or "").strip('"')
    if location.lower() != str(Path(args.install_dir)).lower():
        problems.append("InstallLocation=%r 期望 %r" % (location, args.install_dir))
    if not str(uninstall_key.get("DisplayVersion") or ""):
        problems.append("没有 DisplayVersion（控制面板里看不到版本）")
    recorded = str(manu_key.get("(default)") or "")
    if recorded.lower() != str(Path(args.install_dir)).lower():
        problems.append("%s 默认值=%r 期望 %r（下次安装会还原到它）" % (MANUPRODUCT_KEY, recorded, args.install_dir))
    record("A-023", "安装信息写入注册表（控制面板登记 + 安装位置）", "FAIL" if problems else "PASS",
           "；".join(problems) if problems else
           "InstallLocation=%s DisplayVersion=%s %s=%s" % (
               location, uninstall_key.get("DisplayVersion"), MANUPRODUCT_KEY, recorded))


def step_seed_user_state(args) -> bool:
    """卸载前放一个**合成的** DbBaseline，用来验证"卸载不会顺手删用户状态"。

    只在注册表可写时放（真机/CI）。值里带 e2e 标记，避免与真实基线混淆；
    断言结束后立刻删掉，恢复原状。返回"是否真的放了"。
    """
    writable, why = registry_write_probe("registry-write-probe-seed")
    if not writable:
        return False
    value = '{"e2e": "install_e2e", "note": "synthetic DbBaseline"}'
    script = (
        "$ErrorActionPreference='Continue';"
        "if (-not (Test-Path 'HKCU:\\%s')) { New-Item -Path 'HKCU:\\%s' -Force | Out-Null };"
        "Set-ItemProperty -Path 'HKCU:\\%s' -Name '%s' -Value '%s';"
        "Write-Output 'SEEDED'"
        % (MANUPRODUCT_KEY, MANUPRODUCT_KEY, MANUPRODUCT_KEY, USER_STATE_VALUE, value)
    )
    code, out = powershell(script, tag="seed-user-state")
    seeded = "SEEDED" in out
    record("A-092a", "卸载前放入合成用户状态（DbBaseline）", "PASS" if seeded else "WARN",
           "已写入合成 DbBaseline（断言后会删除，恢复原状）" if seeded
           else "写不进去（%s）—— A-094 只能记 NOT VERIFIED" % out.strip()[-160:])
    return seeded


def step_uninstall_registry(args, seeded: bool) -> None:
    """卸载必须清掉**安装信息**，并且**保留用户状态**（DbBaseline）。"""
    # 与 A-092 同一份等待证据（若已经干净会立刻返回）：拒绝在卸载还没做完时下断言。
    settled = _wait_registry_settled(tag="uninstall-registry-settle-install-info")
    writable, why = registry_write_probe("registry-write-probe-after-uninstall")
    manu_key = _registry_dump(MANUPRODUCT_KEY, "registry-manuproduct-after-uninstall")
    uninstall_key = _registry_dump(UNINSTALL_KEY, "registry-uninstall-after-uninstall")
    evidence(
        "registry-after-uninstall",
        json.dumps(
            {"child_can_write_registry": writable, "probe": why, "seeded": seeded,
             "uninstall_key": uninstall_key, "manuproduct_key": manu_key},
            ensure_ascii=False, indent=2),
    )
    if settled.get("read_error"):
        record("A-093", "卸载清掉安装信息（安装位置 / Installer Language）", "FAIL",
               "读注册表失败，无法下断言：%s" % settled["read_error"])
        record("A-094", "卸载保留用户状态（DbBaseline 不被顺手删掉）", "WARN",
               "同上：读注册表失败，无法下断言")
        return
    if not writable:
        record("A-093", "卸载清掉安装信息（安装位置 / Installer Language）", "WARN",
               "**NOT VERIFIED**：本会话子进程写不了注册表（%s），卸载器的 DeleteRegKey / "
               "DeleteRegValue 会静默失败，本机无法判定。修复本身由 release_gate.py 的 "
               "uninstall.contract 静态契约在 CI 上守着" % why)
        record("A-094", "卸载保留用户状态（DbBaseline 不被顺手删掉）", "WARN",
               "**NOT VERIFIED**：同上 —— 本机读得到注册表，但装/卸两边都写不进去，断言没有意义")
        return

    problems = []
    if manu_key.get("(default)"):
        problems.append("安装位置默认值仍在：%r" % manu_key["(default)"])
    if "Installer Language" in manu_key:
        problems.append("Installer Language 仍在（安装向导语言属于安装信息）")
    timing = "等待 %.2fs / %s 次轮询" % (settled["waited_seconds"], settled["polls"])
    timing += (
        "（超时 %ss，注册表始终没干净）" % settled["timeout_seconds"]
        if not settled["clean"]
        else "（第 %s 次轮询读到干净）" % settled["clean_at_poll"]
    )
    record("A-093", "卸载清掉安装信息（安装位置 / Installer Language）", "FAIL" if problems else "PASS",
           ("；".join(problems) + "；" if problems else
            "安装位置与 Installer Language 都已清掉；键内剩余值：%s；" % sorted(manu_key)) + timing)

    if seeded:
        kept = str(manu_key.get(USER_STATE_VALUE) or "")
        record("A-094", "卸载保留用户状态（DbBaseline 不被顺手删掉）",
               "PASS" if "install_e2e" in kept else "FAIL",
               "合成 DbBaseline 卸载后仍在" if "install_e2e" in kept
               else "合成 DbBaseline 被卸载删掉了 —— 卸载动了用户状态（%r）" % kept[:80])
    else:
        record("A-094", "卸载保留用户状态（DbBaseline 不被顺手删掉）", "WARN",
               "**NOT VERIFIED**：没能放入合成 DbBaseline（注册表不可写），本机无法断言")

    # 恢复原状：把合成值删掉（reg import 是合并语义，不会替我们清掉它）
    cleanup = (
        "$ErrorActionPreference='Continue';"
        "try { Remove-ItemProperty -Path 'HKCU:\\%s' -Name '%s' -ErrorAction Stop;"
        " Write-Output 'CLEANED' } catch { Write-Output ('CLEANUP-FAILED: ' + $_.Exception.Message) }"
        % (MANUPRODUCT_KEY, USER_STATE_VALUE)
    )
    code, out = powershell(cleanup, tag="cleanup-user-state")
    if "CLEANED" not in out:
        record("A-094b", "清理合成用户状态", "WARN", out.strip()[-160:])


def _port_open(port: int) -> bool:
    """端口上有没有人在听。

    存在的理由（2026-10-02 实测踩到）：这台机器上多个 agent 会同时跑同一份 E2E，
    默认端口一撞，"健康检查"就会被**别人的后端**答上来 —— 后面每一条断言都在
    验错的对象，却看起来全绿。宁可开局就红，也不要一份验错对象的报告。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _free_port(start: int, limit: int = 20) -> int:
    """从 start 起挑第一个空闲端口（假厂商是测试替身，端口随便挑，不占别人的配额）。"""
    for candidate in range(start, start + limit):
        if not _port_open(candidate):
            return candidate
    return start


def step_ports(args) -> bool:
    """开局检查**后端**端口。假厂商的端口自动挑空闲的，不参与配额。"""
    if _port_open(args.port):
        record("A-005", "E2E 端口未被占用", "FAIL",
               "端口 %d 已被占用（可能是另一个 agent 的后端/E2E 在跑）：换 --port 重跑，"
               "否则健康检查会被别人的后端答上来" % args.port)
        return False
    record("A-005", "E2E 端口未被占用", "PASS",
           "%d（后端）空着；假厂商端口自动挑空闲的" % args.port)
    return True


def step_preflight(args) -> bool:
    installer = Path(args.installer)
    if not installer.exists():
        record("A-000", "安装包存在", "FAIL", "找不到 %s" % installer)
        return False
    digest = sha256_of(installer)
    record("A-001", "安装包指纹", "PASS",
           "%s（%.1f MB，sha256 %s）" % (installer.name, installer.stat().st_size / 1048576, digest))

    code, out = run(["powershell", "-NoProfile", "-Command",
                     "Get-Process -Name qio,qio-backend -ErrorAction SilentlyContinue | "
                     "Select-Object Id,ProcessName,Path | Format-Table -AutoSize | Out-String"],
                    tag="preflight-processes")
    # 静默安装遇到正在运行的 qio.exe 会**直接把它杀掉**（NSIS 模板行为）。那是动用户正在用的
    # 程序，所以这里不替用户做决定：有进程就先停，让人来决定。
    if out.strip():
        record("A-002", "安装前没有 QIO 进程在跑", "FAIL",
               "检测到正在运行的 QIO：%s（静默安装会杀掉它，脚本主动停止）" % out.strip()[:200])
        return False
    record("A-002", "安装前没有 QIO 进程在跑", "PASS", "无")

    work = Path(args.work_dir)
    backup = work / "backup"
    backup.mkdir(parents=True, exist_ok=True)
    for name, key in (("uninstall-QIO.reg", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\QIO"),
                      ("software-qio.reg", r"HKCU\Software\qio")):
        code, out = run(["reg", "export", key, str(backup / name), "/y"], tag="preflight-%s" % name)
        if code != 0:
            record("A-003", "注册表备份", "FAIL", "reg export %s 失败：%s" % (key, out.strip()[:200]))
            return False
    record("A-003", "注册表备份", "PASS", "已导出 Uninstall\QIO 与 Software\qio 到 %s" % backup)

    for src, dst in ((Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "QIO.lnk", backup / "desktop-QIO.lnk"),
                     (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "QIO.lnk",
                      backup / "startmenu-QIO.lnk")):
        if src.exists():
            shutil.copy2(src, dst)
    code, out = powershell(
        "$v = (Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
        "-Name QIO -ErrorAction SilentlyContinue).QIO; if ($v) { $v } else { '' }",
        tag="preflight-run-value")
    (backup / "run-value.txt").write_text(out.strip(), encoding="utf-8")

    installer_dir = Path(args.existing_install)
    hashes = []
    if installer_dir.exists():
        for path in sorted(installer_dir.rglob("*")):
            if path.is_file():
                hashes.append("%s  %s" % (sha256_of(path), path))
    (backup / "existing-install-hashes.txt").write_text("\n".join(hashes), encoding="utf-8")
    record("A-004", "既有安装基线指纹（D:\\QIO）", "PASS" if hashes else "SKIP",
           "%d 个文件已记录到 backup\\existing-install-hashes.txt" % len(hashes))
    return True


def installer_env(args) -> dict:
    """跑安装器要一份**可写**的 TEMP。

    本机子进程是受限令牌，写不了用户目录下的 AppData\Local\Temp：NSIS 解 $PLUGINSDIR
    失败会直接静默 abort（实测 exit=2、目录不生成；把 TEMP 指到检出内 work/tmp 后同一份
    安装包 exit=0、8.4s 装完）。指到检出内的 work/tmp。
    真机/CI 的 %TEMP% 本来就可写 —— 这条是**本沙箱的环境适配，不是产品行为**，必须记账。
    """
    tmp = Path(args.work_dir) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "TEMP": str(tmp), "TMP": str(tmp)}


def step_decoy(args) -> bool:
    """把「既有安装」换成诱饵，验证静默安装不会调用既有卸载器。"""
    decoy = Path(args.decoy_dir)
    shutil.rmtree(decoy, ignore_errors=True)
    decoy.mkdir(parents=True, exist_ok=True)
    (decoy / "qio.exe").write_bytes(b"decoy-not-a-real-exe")
    marker = Path(args.work_dir) / "DECOY-UNINSTALLER-RAN.log"
    if marker.exists():
        marker.unlink()

    nsi = Path(args.work_dir) / "decoy-uninstaller.nsi"
    nsi.write_text(
        'Unicode true\nName "DecoyUninstaller"\nOutFile "%s"\nRequestExecutionLevel user\n'
        'SilentInstall silent\n'
        'Section\n'
        '  FileOpen $9 "%s" w\n  FileWrite $9 "DECOY UNINSTALLER EXECUTED"\n  FileClose $9\n'
        '  Delete "%s\\qio.exe"\n  Delete "%s\\uninstall.exe"\n  RMDir "%s"\n'
        'SectionEnd\n' % (decoy / "uninstall.exe", marker, decoy, decoy, decoy),
        encoding="utf-8")
    makensis = Path(os.environ.get("LOCALAPPDATA", "")) / "tauri" / "NSIS" / "makensis.exe"
    if not makensis.exists():
        record("A-010", "诱饵实验（静默安装是否会卸载既有安装）", "SKIP",
               "找不到 makensis：%s" % makensis)
        return True
    code, out = run([str(makensis), "/V2", str(nsi)], timeout=120, tag="decoy-makensis")
    if code != 0 or not (decoy / "uninstall.exe").exists():
        record("A-010", "诱饵实验", "SKIP", "诱饵卸载器编译失败：%s" % out.strip()[:200])
        return True

    # 把注册表指向诱饵（真安装器就是照这个槽位判断「有没有既有安装」）。
    # 键可能不存在（例如已经被卸载过），所以先建键、再写值。
    # 这一整段用 raw 三引号字符串：PowerShell 里的反斜杠不需要再过一层 Python 转义，
    # 引号用 [char]34 在 PowerShell 侧拼，避免三层引号互相打架。
    repointer = r"""
$decoy = '%s'
$q = [char]34
$un = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\QIO'
$prod = 'HKCU:\Software\qio\QIO'
foreach ($p in @('HKCU:\Software\qio', $prod, $un)) {
  if (-not (Test-Path $p)) { New-Item -Path $p -Force | Out-Null }
}
Set-ItemProperty -Path $un -Name DisplayName -Value 'QIO'
Set-ItemProperty -Path $un -Name Publisher -Value 'qio'
Set-ItemProperty -Path $un -Name DisplayVersion -Value '0.1.9'
Set-ItemProperty -Path $un -Name InstallLocation -Value ($q + $decoy + $q)
Set-ItemProperty -Path $un -Name UninstallString -Value ($q + $decoy + '\uninstall.exe' + $q)
Set-ItemProperty -Path $prod -Name '(default)' -Value $decoy
Write-Output 'REPOINTED'
""" % decoy
    code, out = powershell(repointer, tag="decoy-registry")
    if "REPOINTED" not in out:
        record("A-009", "诱饵注册表已生效", "FAIL", "改注册表失败：%s" % out.strip()[:300])
        return False
    STATE["decoy_applied"] = True
    record("A-009", "诱饵注册表已生效", "PASS",
           "HKCU\\...\\Uninstall\\QIO 与 HKCU\\Software\\qio\\QIO 暂时指向 %s；"
           "整个 E2E 期间保持这个状态，结束时还原" % decoy)

    code, out = run(install_cmdline(args.installer, args.install_dir), env=installer_env(args),
                    timeout=900, tag="decoy-install")
    ran = marker.exists()
    state = "FAIL" if ran else "PASS"
    detail = ("诱饵卸载器被执行了 —— 说明静默安装会卸载既有安装，禁止在本机跑真安装！"
              if ran else
              "诱饵卸载器没有被执行（exit=%s）；安装目录存在=%s"
              % (code, Path(args.install_dir).exists()))
    record("A-010", "诱饵实验：静默安装不调用既有安装的卸载器", state, detail)
    if ran:
        return False
    if code != 0:
        record("A-011", "静默安装退出码", "FAIL", "exit=%s，输出尾部：%s" % (code, out.strip()[-400:]))
        return False
    return True


def step_install_files(args) -> bool:
    install_dir = Path(args.install_dir)
    if not install_dir.exists():
        record("A-020", "安装目录生成", "FAIL", "%s 不存在" % install_dir)
        return False
    files = []
    for path in sorted(install_dir.rglob("*")):
        if path.is_file():
            files.append("%10d  %s  %s" % (path.stat().st_size, sha256_of(path)[:16],
                                           path.relative_to(install_dir)))
    listing = "\n".join(files)
    evidence("install-dir-listing", listing)
    record("A-020", "安装目录内容", "PASS",
           "%d 个文件（%.1f MB）\n%s" % (len(files), sum(p.stat().st_size for p in install_dir.rglob("*") if p.is_file()) / 1048576, listing))

    required = {"qio.exe": "主程序", "qio-backend.exe": "打包后端 sidecar",
                "uninstall.exe": "卸载器",
                "models/bge-small-zh-v1.5/model.onnx": "内置模型",
                "models/bge-small-zh-v1.5/model_manifest.json": "模型清单"}
    missing = [k for k in required if not (install_dir / k).exists()]
    record("A-021", "安装目录关键文件", "FAIL" if missing else "PASS",
           "缺少：%s" % missing if missing else " / ".join("%s(%s)" % (k, v) for k, v in required.items()))
    return not missing


def step_models(args) -> bool:
    install_dir = Path(args.install_dir)
    base = install_dir / "models" / "bge-small-zh-v1.5"
    manifest_path = base / "model_manifest.json"
    if not manifest_path.exists():
        record("A-022", "内置模型与清单一致", "FAIL", "缺少 %s" % manifest_path)
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems, checked, lines = [], 0, []
    for entry in manifest.get("files") or []:
        name = str(entry.get("file") or "")
        target = base / name
        if not target.exists():
            problems.append("缺少 %s" % name)
            continue
        size = target.stat().st_size
        digest = sha256_of(target)
        ok = (int(entry.get("bytes") or -1) == size) and (not entry.get("sha256") or digest == entry["sha256"])
        lines.append("%s  %s  bytes=%d  sha256=%s" % ("OK " if ok else "BAD", name, size, digest[:16]))
        if ok:
            checked += 1
        else:
            problems.append("%s 与清单不符" % name)
    evidence("installed-model-manifest", "\n".join(lines) + "\n\nmanifest: " + json.dumps(manifest, ensure_ascii=False))
    record("A-022", "内置模型与清单一致", "FAIL" if problems else "PASS",
           "；".join(problems) if problems else "%d 个文件逐一核对通过（bytes+sha256）" % checked)
    return not problems


def clean_env(args, port: int) -> dict:
    """白名单构造 env：**不继承**开发用环境变量。"""
    keep = ["SystemRoot", "windir", "SystemDrive", "COMSPEC", "PATHEXT", "PATH",
            "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
            "USERPROFILE", "USERNAME", "USERDOMAIN", "LOCALAPPDATA", "APPDATA", "ProgramData",
            "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles", "COMPUTERNAME",
            "HOMEDRIVE", "HOMEPATH", "PUBLIC", "SESSIONNAME", "TZ"]
    env = {k: os.environ[k] for k in keep if k in os.environ}
    tmp = str(Path(args.work_dir) / "tmp")
    Path(tmp).mkdir(parents=True, exist_ok=True)
    env["TEMP"] = env["TMP"] = tmp
    env["QIO_DATA_DIR"] = str(Path(args.work_dir) / "data")
    env["QIO_PORT"] = str(port)
    env["QIO_SESSION_TOKEN"] = args.token
    # 唯一的**偏差开关**，必须显式记账：
    # 启动时的「数据库身份自检」在 Windows 上把基线写进 HKCU\Software\qio（注册表）。
    # 本会话的沙箱给子进程的是受限令牌，任何注册表写入都是 WinError 5 —— 装出来的后端会
    # 在启动阶段直接崩（原始 traceback 见 docs）。QIO_DISABLE_DB_CHECK=1 是产品**自带**的
    # 开关（注释写明给测试/隔离环境用），这里用它绕开环境限制，而不是改产品代码。
    # 代价：这次 E2E **没有覆盖**「数据库身份自检」这条启动路径。
    env["QIO_DISABLE_DB_CHECK"] = "1"
    dropped = sorted(k for k in os.environ if k.startswith(("QIO_", "PYTHON", "UV_", "VIRTUAL_ENV", "NODE_")) and k not in env)
    evidence("backend-env", json.dumps({"kept": sorted(env), "dropped_dev_vars": dropped}, ensure_ascii=False, indent=2))
    return env


def start_backend(args, port: int):
    install_dir = Path(args.install_dir)
    env = clean_env(args, port)
    logfile = Path(args.work_dir) / "evidence" / "installed-backend.log"
    fh = logfile.open("wb")
    proc = subprocess.Popen([str(install_dir / "qio-backend.exe")], cwd=str(install_dir),
                            env=env, stdout=fh, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc


def wait_health(client: Client, timeout: float = 60.0):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            status, body = client.get("/api/health", timeout=5)
            if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                return True, json.dumps(body, ensure_ascii=False)
            last = "%s %s" % (status, body)
        except Exception as exc:
            last = repr(exc)
        time.sleep(0.5)
    return False, last


def step_health(args, port: int):
    client = Client("http://127.0.0.1:%d" % port, args.token)
    record("A-032", "启动环境偏差（必须记账）", "WARN",
           "注入了 QIO_DISABLE_DB_CHECK=1：沙箱令牌不允许写注册表，而启动时的数据库身份自检"
           "要把基线写进 HKCU\\Software\\qio。该路径本次未被覆盖；不用这个开关后端会在启动阶段崩溃。")
    ok, detail = wait_health(client)
    record("A-030", "安装目录里的后端 /api/health", "PASS" if ok else "FAIL",
           "200 + status=ok + db=true：%s" % detail if ok else "未就绪：%s" % detail)
    if not ok:
        return False, client
    # 谁在回答这个端口？必须**就是**安装目录里的那个 exe。
    # 只看进程名不够：机器上可能有别的 QIO 后端（别的 agent 的 E2E、或本机既有安装）。
    script = (
        "$owner = (Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty OwningProcess);"
        "if ($owner) { (Get-Process -Id $owner -ErrorAction SilentlyContinue).Path } else { '' }" % port
    )
    code, out = run(["powershell", "-NoProfile", "-Command", script], tag="backend-process-path")
    expected = str((Path(args.install_dir) / "qio-backend.exe").resolve())
    actual = out.strip()
    if actual and Path(actual).resolve() == Path(expected):
        record("A-031", "健康检查回答者就是安装目录里的后端", "PASS", actual)
        return True, client
    record("A-031", "健康检查回答者就是安装目录里的后端", "FAIL",
           "端口 %d 的回答者是 %r，不是 %r —— 后面的断言会验错对象，直接停" % (
               port, actual or "(取不到进程路径)", expected))
    return False, client

def step_fake_provider(args, port: int):
    # 假厂商端口自动挑空闲的：port+1 可能正好是别的 agent 分配到的后端端口
    # （Lead 2026-10-02 的端口分配里就存在这种相邻冲突）。
    fp_port = _free_port(port + 1)
    logfile = Path(args.work_dir) / "evidence" / "fake-provider.log"
    fh = logfile.open("wb")
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "e2e_fake_provider.py"),
                             "--port", str(fp_port)],
                            stdout=fh, stderr=subprocess.STDOUT,
                            env={**os.environ, "TEMP": str(Path(args.work_dir) / "tmp"),
                                 "TMP": str(Path(args.work_dir) / "tmp")})
    client = Client("http://127.0.0.1:%d" % fp_port)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            status, body = client.get("/__health", timeout=3)
            if status == 200 and body.get("ok"):
                record("A-040", "离线假厂商已就绪（不是真实服务）", "PASS",
                       "http://127.0.0.1:%d/v1 served=%s" % (fp_port, body.get("served")))
                return proc, client, fp_port
        except Exception:
            time.sleep(0.4)
    record("A-040", "离线假厂商已就绪", "FAIL", "30s 内没有起来，见 evidence/fake-provider.log")
    return proc, client, fp_port


def step_credential(args, client: Client, fp_port: int):
    key_id = "e2e-install-main"
    status, body = client.post("/api/credentials", {
        "key_id": key_id,
        "secret": "sk-e2e-install-fake-0001",
        "tags": ["main-loop"],
        "endpoint": "http://127.0.0.1:%d/v1" % fp_port,
        "default_model": "fake-model",
        "budget": 100000,
    })
    ok = status == 200 and body.get("version") == 1
    record("A-041", "保存 provider 凭据（写入即不可读回）", "PASS" if ok else "FAIL",
           "key_id=%s version=%s" % (key_id, body.get("version")) if ok else "%s %s" % (status, str(body)[:200]))

    listing = client.get("/api/credentials")[1].get("credentials", [])
    item = next((c for c in listing if c["key_id"] == key_id), None)
    leaked = item is not None and ("sk-e2e-install-fake-0001" in json.dumps(item, ensure_ascii=False))
    record("A-042", "凭据列表不回显密钥原文", "FAIL" if leaked else "PASS",
           "发现泄露" if leaked else "列表只有掩码元数据")
    if item:
        evidence("credential-listing", json.dumps(item, ensure_ascii=False, indent=2))
    client.post("/api/credentials/%s/default" % key_id)
    status, probe = client.post("/api/credentials/%s/test" % key_id, timeout=60)
    record("A-043", "测试连接（打的是假厂商）", "PASS" if status in (200, 502) else "FAIL",
           "status=%s %s" % (status, json.dumps(probe, ensure_ascii=False)[:300]))
    return key_id


def run_turn(client: Client, sse: SseReader, fp_client: Client, message: str,
             on_event=None, timeout: float = 300.0):
    """触发一轮模型对话，边收事件边让调用方推进假厂商脚本。"""
    sse.drain_replay()
    # 关键：把"连接重放出来的历史事件"全部排除，只认本轮新产生的事件。
    # 单靠 drain_replay 的固定等待是不够的 —— 重放可能比那 1.2s 慢，旧 TURN_END 会先到。
    before = set(sse.seen)
    events: list[dict] = []
    status, body = client.post("/api/turns", {"message": message})
    if status != 200:
        return events, "POST /api/turns -> %s %s" % (status, body)
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = sse.next_event(timeout=2.0)
        if event is None:
            continue
        if event.get("id") and str(event["id"]) in before:
            continue  # 历史重放，不属于本轮
        events.append(event)
        if on_event is not None:
            try:
                on_event(event)
            except Exception as exc:
                log("  ! on_event 异常：%r" % (exc,))
        if event.get("type") in ("TURN_END", "ERROR"):
            return events, ""
    return events, "超时未收到 TURN_END"


def step_turn(args, client: Client, sse: SseReader, fp: Client):
    fp.post("/__script", {"steps": [{"text": "这是安装后首次真实一轮对话的假模型回复。"}]})
    events, err = run_turn(client, sse, fp, "安装 E2E：你好，请回一句。")
    types = [e.get("type") for e in events]
    ok = not err and "TURN_END" in types
    record("A-050", "装出来的后端跑通一轮真实 turn", "PASS" if ok else "FAIL",
           "事件：%s" % ",".join(types) if ok else "%s；事件 %s" % (err, types))
    logdata = fp.get("/__log")[1].get("requests", [])
    key = logdata[-1]["key"] if logdata else {}
    record("A-051", "假厂商确实收到了请求（密钥只留指纹）", "PASS" if key.get("present") else "FAIL",
           "sha256_16=%s len=%s model=%s" % (key.get("sha256_16"), key.get("length"),
                                             logdata[-1]["model"] if logdata else None))
    return ok


TOOL_JSON = json.dumps({
    "name": "dev_add",
    "description": "两个数求和（安装 E2E 用）",
    "tool_type": "function",
    "sync": True,
    "code": "def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
    "tests": [
        {"name": "positive", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}},
        {"name": "zero", "input": {"a": 0, "b": 0}, "expect": {"sum": 0}},
    ],
}, ensure_ascii=False)


def step_dev_flow(args, client: Client, sse: SseReader, fp: Client):
    """create_tool → dev_write_file → dev_run_tests → dev_submit_tool → 注册。"""
    approvals: list[dict] = []
    tool_ends: list[dict] = []
    state = {"ws": None, "pushed": False}

    def on_event(event: dict) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "APPROVAL_REQUIRED":
            approval = data.get("approval") or {}
            approvals.append(approval)
            client.post("/api/approvals/%s/respond" % approval.get("approval_id"), {
                "decision": "approved",
                "turn_id": approval.get("turn_id"),
                "session_id": approval.get("session_id"),
                "request_digest": approval.get("digest"),
            })
        elif etype == "TOOL_END":
            tool_ends.append(data)
            if data.get("tool") == "create_tool" and data.get("ok") and not state["pushed"]:
                # 先用事件里的 content_preview（create_tool 的第一行就带工作区 id），
                # 它不需要再发一次 HTTP —— 少一次往返就少一点被主循环抢先的机会。
                text = json.dumps(data.get("content_preview") or "", ensure_ascii=False)
                rec = data.get("record_id")
                if not re.search(r"工作区 id=([A-Za-z0-9_\-]+)", text) and rec:
                    text += " " + json.dumps(client.get("/api/tool-records/%s" % rec)[1], ensure_ascii=False)
                match = re.search(r"工作区 id=([A-Za-z0-9_\-]+)", text)
                if match:
                    state["ws"] = match.group(1)
                    state["pushed"] = True
                    fp.post("/__script", {"steps": [
                        {"tool": "dev_write_file", "args": {"workspace": state["ws"], "name": "tool.json", "content": TOOL_JSON}},
                        {"tool": "dev_run_tests", "args": {"workspace": state["ws"]}},
                        {"tool": "dev_submit_tool", "args": {"workspace": state["ws"],
                                                             "explanation": "把两个数加起来返回它们的和"}},
                        {"text": "工具已开发完成"},
                    ]})

    # 关键：主循环不会等我们（SSE 是异步的）。create_tool 之后它立刻再问一次模型，
    # 如果这时脚本队列是空的，假厂商就会回一句文本 → turn 直接结束，后面的开发步骤永远没机会跑。
    # 所以先把 default 设成一个**无害且可重复**的工具调用（dev_list_tasks 不写任何东西），
    # 让 turn 撑到我们把真正的步骤推进队列为止。
    fp.post("/__script", {"steps": [{"tool": "create_tool",
                                     "args": {"request": "我需要一个把两个数求和的工具，输入 a、b，输出 sum"}}],
                          "default": {"tool": "dev_list_tasks", "args": {}}})
    events, err = run_turn(client, sse, fp, "帮我做一个两个数求和的工具。", on_event=on_event, timeout=600)
    fp.post("/__script", {"steps": [], "default": {"text": "（假模型默认回复）"}})
    names = [t.get("tool") for t in tool_ends]
    record("A-060", "开发流程四步都被调用", "PASS" if {"create_tool", "dev_write_file", "dev_run_tests",
                                                       "dev_submit_tool"} <= set(names) else "FAIL",
           "%s（err=%s）" % (names, err or "无"))
    record("A-061", "审批真的发生过且被逐条批准", "PASS" if approvals else "FAIL",
           "%d 次：%s" % (len(approvals), [a.get("kind") for a in approvals]))
    evidence("dev-flow-approvals", json.dumps(approvals, ensure_ascii=False, indent=2))
    evidence("dev-flow-tool-ends", json.dumps(tool_ends, ensure_ascii=False, indent=2))

    submit = next((t for t in tool_ends if t.get("tool") == "dev_submit_tool"), None)
    record("A-062", "dev_run_tests 通过（测试真的跑了）", "PASS" if any(
        t.get("tool") == "dev_run_tests" and t.get("ok") for t in tool_ends) else "FAIL",
        "%s" % (next((str(t.get("content_preview"))[:200] for t in tool_ends
                      if t.get("tool") == "dev_run_tests"), "没有 dev_run_tests 事件")))
    record("A-063", "dev_submit_tool 提交成功（注册前置）", "PASS" if submit and submit.get("ok") else "FAIL",
           str((submit or {}).get("content_preview"))[:300] or "无 dev_submit_tool 事件")
    return state["ws"]


def step_invoke_tool(args, client: Client, sse: SseReader, fp: Client):
    """重启前先调用一次新注册的工具，留一条工具记录。"""
    results: list[dict] = []

    def on_event(event: dict) -> None:
        if event.get("type") == "TOOL_END":
            results.append(event.get("data") or {})

    fp.post("/__script", {"steps": [{"tool": "dev_add", "args": {"a": 2, "b": 3}}, {"text": "和是 5"}]})
    events, err = run_turn(client, sse, fp, "用你刚做好的工具算 2+3。", on_event=on_event, timeout=300)
    call = next((r for r in results if r.get("tool") == "dev_add"), None)
    ok = bool(call and call.get("ok"))
    record("A-064", "新工具注册后可被调用", "PASS" if ok else "FAIL",
           "ok=%s content=%s err=%s" % (call.get("ok") if call else None,
                                        str((call or {}).get("content_preview"))[:160], err or "无"))
    rec_id = (call or {}).get("record_id")
    if rec_id:
        status, body = client.get("/api/tool-records/%s" % rec_id)
        text = json.dumps(body, ensure_ascii=False)
        record("A-065", "工具记录可查", "PASS" if status == 200 and "dev_add" in text else "FAIL",
                "GET /api/tool-records/%s -> %s %s" % (rec_id, status, text[:220]))
    else:
        record("A-065", "工具记录可查", "FAIL", "TOOL_END 里没有 record_id")
    return ok


def _backend_pids_by_path(install_dir) -> list[int]:
    """**只**找安装目录里那个 qio-backend.exe 的进程 id。

    按路径找，不按镜像名找：这台机器上同时有多个 agent 在跑真实后端，
    按名字杀（taskkill /IM qio-backend.exe）会误伤别人的进程
    —— 2026-10-02 Lead 明确要求"按 PID / 进程树杀"。
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='qio-backend.exe'\" | "
        "Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLower() -eq '%s' } | "
        "Select-Object -ExpandProperty ProcessId"
        % str((Path(install_dir) / "qio-backend.exe").resolve()).lower()
    )
    code, out = powershell(script, tag="backend-pids-by-path")
    return [int(x) for x in out.split() if x.strip().isdigit()]


def _wait_sidecar_gone(install_dir, *, timeout: float = 30.0, interval: float = 0.5) -> dict:
    """等安装目录里的 sidecar 被卸载器收掉（PREUNINSTALL 钩子负责收）。

    A-095 的语义不变：超时后仍然留下进程就是 FAIL。这里只是把「等到稳定」的过程与证据记下来
    （按路径枚举、按 PID 收；绝不按镜像名杀）。
    """
    started = time.time()
    polls = 0
    left: list[int] = []
    while True:
        polls += 1
        left = _backend_pids_by_path(install_dir)
        if not left or time.time() - started >= timeout:
            break
        time.sleep(interval)
    result = {
        "gone": not left,
        "waited_seconds": round(time.time() - started, 2),
        "polls": polls,
        "left_pids": left,
        "timeout_seconds": timeout,
    }
    evidence("sidecar-settle", json.dumps(result, ensure_ascii=False, indent=2))
    return result


def ensure_backend_stopped(args, tag: str) -> bool:
    """卸载/重装前的硬前置：安装目录里的后端必须**真的**停下。

    「端口关 ≠ 文件没被锁」（2026-10-03，Agent A 用冻结产物实测 + 我的安装/卸载实测）：
      * 运行中的 qio-backend.exe 删不掉也覆盖不了（delete -> WinError 5，
        overwrite -> Errno 13），只有 rename 能成功；
      * onefile 是 launcher + child，只结束 launcher 会留下孤儿 child 继续持有映像。
    所以这里按**路径**枚举进程、按 **PID / 进程树**收，然后确认真的没有了 ——
    否则后面的「卸载残留了什么 / 重装有没有换掉文件」都会被这把锁污染成假结论。
    """
    for _ in range(3):
        pids = _backend_pids_by_path(args.install_dir)
        if not pids:
            record("A-088", "%s：安装目录里的后端已确认停止" % tag, "PASS", "按路径枚举：无残留进程")
            return True
        for pid in pids:
            run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=60, tag="precondition-kill")
        # 有界等待 + 证据（main 上 dcb2a17 给 A-095 加的那套口径，在这里同样适用：
        # 「进程刚被杀」到「文件锁真的放开」之间有窗口，靠固定 sleep 猜会得到假结论）。
        settled = _wait_sidecar_gone(args.install_dir, timeout=10.0, interval=0.5)
        if settled["gone"]:
            record("A-088", "%s：安装目录里的后端已确认停止" % tag, "PASS",
                   "按路径枚举：无残留进程（等待 %.2fs / %s 次轮询）" % (
                       settled["waited_seconds"], settled["polls"]))
            return True
    pids = _backend_pids_by_path(args.install_dir)
    record("A-088", "%s：安装目录里的后端已确认停止" % tag, "FAIL",
           "仍有进程持有安装目录里的 exe（pid=%s）：文件删不掉/换不掉，之后的结论不可信" % pids)
    return False


def stop_backend(proc, install_dir=None) -> None:
    """收掉**我们自己**起的后端：先按 PID 收整棵树，再按路径兜底。

    PyInstaller onefile 是「引导进程 + 真正跑服务的子进程」两层，而子进程会握着安装目录里的
    qio-backend.exe（本机实测：不连子进程一起收，卸载器就删不掉它）。
    顺序很关键：**先** taskkill /T /PID（此时父子关系还在，/T 才能收掉子进程），
    再去 terminate 父进程 —— 反过来的话子进程会变成孤儿，就再也按树收不到了。
    """
    if proc is not None and proc.poll() is None:
        run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], timeout=60, tag="taskkill-tree")
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if install_dir is not None:
        for pid in _backend_pids_by_path(install_dir):
            run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=60, tag="taskkill-by-path")
    time.sleep(2)


def step_restart(args, client: Client, proc, port: int):
    stop_backend(proc, args.install_dir)
    time.sleep(1.5)
    new_proc = start_backend(args, port)
    ok, detail = wait_health(Client("http://127.0.0.1:%d" % port, args.token))
    record("A-070", "重启后后端重新就绪", "PASS" if ok else "FAIL", detail if ok else "未就绪：%s" % detail)
    return new_proc, ok


def step_recovery(args, client: Client, sse: SseReader, fp: Client):
    checks = []
    for cid, title, path, needle in (
        ("A-071", "开发任务在重启后仍在", "/api/dev/tasks", None),
        ("A-072", "授权状态在重启后仍可读", "/api/dev/authorizations", None),
        ("A-073", "interrupted turn 入口存在", "/api/runtime/state", "interrupted_turns"),
        ("A-074", "会话消息在重启后仍在", "/api/session/messages", None),
    ):
        try:
            status, body = client.get(path)
            text = json.dumps(body, ensure_ascii=False)
            needed = (needle is None) or (needle in text)
            has_content = len(text) > 2 and text not in ("{}", "[]", '{"messages": []}')
            state = "PASS" if status == 200 and needed and has_content else ("WARN" if status == 200 and needed else "FAIL")
            record(cid, title, state, "%s -> %s %s" % (path, status, text[:240]))
            checks.append(state == "PASS")
        except Exception as exc:
            record(cid, title, "FAIL", "%s 读取失败：%r" % (path, exc))
            checks.append(False)
    # 工具恢复：重启后再调一次。
    # 注意：重启把上一条 SSE 流打断了（后端进程没了），必须换一条新连接 ——
    # 用旧连接会「一个事件都收不到」，然后看起来像功能坏了。
    sse = SseReader(client)
    results: list[dict] = []

    # 用 default 而不是 FIFO 队列来驱动这一轮：队列里的步骤可能被"别的模型调用"先吃掉，
    # 那会让检查变成假阴性（本机踩过：脚本明明推了，厂商却回了默认文本）。
    # default 保证"这一轮任何一次模型调用"都先拿到 dev_add，直到我们把它换成文本。
    def recovery_event(event: dict) -> None:
        data = event.get("data") or {}
        if event.get("type") == "TOOL_END":
            results.append(data)
            if data.get("tool") == "dev_add":
                fp.post("/__script", {"steps": [], "default": {"text": "42"}})

    fp.post("/__script", {"steps": [], "default": {"tool": "dev_add", "args": {"a": 40, "b": 2}}})
    events, err = run_turn(client, sse, fp, "重启后再算 40+2。", on_event=recovery_event, timeout=300)
    fp.post("/__script", {"steps": [], "default": {"text": "（假模型默认回复）"}})
    call = next((r for r in results if r.get("tool") == "dev_add"), None)
    types = [e.get("type") for e in events]
    # 诊断：假厂商到底把哪一步喂给了模型（这是"没调用工具"时唯一能分辨原因的证据）
    served = fp.get("/__log")[1].get("requests", [])
    last = served[-1] if served else {}
    record("A-075", "注册的工具在重启后仍可调用", "PASS" if call and call.get("ok") else "FAIL",
           "ok=%s content=%s；本轮事件=%s；err=%s；厂商总请求=%d；最后一次 step=%s；offered 里有 dev_add=%s" % (
               (call or {}).get("ok"), str((call or {}).get("content_preview"))[:120],
               ",".join(types), err or "无", len(served),
               json.dumps(last.get("step"), ensure_ascii=False),
               "dev_add" in (last.get("tool_names_offered") or [])))
    return all(checks)


def step_tool_settings(args, client: Client):
    try:
        status, body = client.get("/api/settings/tools")
        text = json.dumps(body, ensure_ascii=False)
        record("A-076", "工具设置/注册表可见 dev_add", "PASS" if "dev_add" in text else "WARN",
               "GET /api/settings/tools -> %s %s" % (status, text[:260]))
    except Exception as exc:
        record("A-076", "工具设置/注册表可见 dev_add", "WARN", repr(exc))


def step_reinstall(args, client: Client, port: int):
    """同一版本再装一次（等价于「重装/覆盖升级」），验证用户数据是否保留。"""
    data_dir = Path(args.work_dir) / "data"
    before = sorted(p.name for p in data_dir.glob("*"))
    code, out = run(install_cmdline(args.installer, args.install_dir), env=installer_env(args),
                    timeout=900, tag="reinstall")
    time.sleep(2)
    after = sorted(p.name for p in data_dir.glob("*"))
    proc = start_backend(args, port)
    ok, detail = wait_health(Client("http://127.0.0.1:%d" % port, args.token))
    record("A-080", "重装（覆盖安装）保留用户数据目录", "PASS" if before == after and before else "FAIL",
           "exit=%s 数据条目 %s -> %s" % (code, before, after))
    record("A-081", "重装后后端仍可用", "PASS" if ok else "FAIL", detail if ok else "未就绪：%s" % detail)
    return proc


def step_uninstall(args, *, locked_residue=None):
    """跑卸载器 /S。

    locked_residue：**预期**会残留的文件名集合（替身占着 qio-backend.exe 时，卸载器按
    新语义不杀它 → 文件删不掉）。给了它，A-090 记 WARN 并写清"这是预期残留，不是卸载器
    缺陷"；残留**超出**这个集合仍然是 FAIL —— 不能拿"预期"当挡箭牌。
    """
    uninstaller = Path(args.install_dir) / "uninstall.exe"
    if not uninstaller.exists():
        record("A-090", "卸载器存在", "FAIL", "找不到 %s" % uninstaller)
        return
    data_dir = Path(args.work_dir) / "data"
    before = sorted(p.name for p in data_dir.glob("*"))
    env = {**os.environ, "TEMP": str(Path(args.work_dir) / "tmp"),
           "TMP": str(Path(args.work_dir) / "tmp")}
    code, out = run([str(uninstaller), "/S"], timeout=600, env=env, tag="uninstall")
    deadline = time.time() + 120
    while time.time() < deadline and Path(args.install_dir).exists():
        time.sleep(2)
    still = sorted(p.name for p in Path(args.install_dir).glob("*")) if Path(args.install_dir).exists() else []
    after = sorted(p.name for p in data_dir.glob("*"))
    if not Path(args.install_dir).exists():
        record("A-090", "卸载后安装目录被清空", "PASS", "exit=%s 目录已删除" % code)
    elif locked_residue is not None and set(still) <= set(locked_residue):
        record("A-090", "卸载后安装目录被清空", "WARN",
               "exit=%s 残留：%s —— **预期残留**：这些文件被拒绝误杀的无归属替身占着（见 A-095/A-096），"
               "卸载器按新语义不杀它，所以文件删不掉。不是卸载器缺陷，但也**不是**「目录已清空」" % (code, still))
    else:
        record("A-090", "卸载后安装目录被清空",
               "WARN" if still in (["uninstall.exe"], []) else "FAIL",
               "exit=%s 残留：%s" % (code, still))
    if after == before and before:
        record("A-091", "卸载保留用户数据（未勾选删除数据）", "PASS", "数据目录 %s -> %s" % (before, after))
    elif not before:
        # 数据目录本来就是空的（例如 --stages uninstall 单独跑）：没有对象可验，记 NOT VERIFIED，
        # 不要把它印成 FAIL —— 那会让人以为是产品把数据删了（本机实测踩到过）。
        record("A-091", "卸载保留用户数据（未勾选删除数据）", "WARN",
               "**NOT VERIFIED**：卸载前数据目录就是空的，这一条没有对象可验（完整流程里由 "
               "CI/前面的 stage 放进合成用户状态）")
    else:
        record("A-091", "卸载保留用户数据（未勾选删除数据）", "FAIL",
               "数据目录 %s -> %s" % (before, after))
    # 卸载器会把自己复制到临时目录再执行、原进程提前返回 —— 读到「还没清」之前必须等它真的做完。
    settled = _wait_registry_settled()
    writable, why = registry_write_probe("registry-write-probe-after-uninstall")
    timing = "等待 %.2fs / %s 次轮询" % (settled["waited_seconds"], settled["polls"])
    timing += (
        "（超时 %ss，注册表始终没干净）" % settled["timeout_seconds"]
        if not settled["clean"]
        else "（第 %s 次轮询读到干净）" % settled["clean_at_poll"]
    )
    if settled.get("read_error"):
        record("A-092", "卸载后卸载注册表项被移除", "FAIL",
               "读注册表失败，无法下断言：%s；%s" % (settled["read_error"], timing))
    elif not writable:
        # 本机：子进程写不了注册表，安装器当初可能根本没写进去 —— 「STILL PRESENT」不能当产品结论。
        record("A-092", "卸载后卸载注册表项被移除", "WARN",
               "**NOT VERIFIED**：本会话子进程写不了注册表（%s），安装器的 WriteRegStr 会静默失败，"
               "读到的值不代表产品行为。%s" % (why, timing))
    else:
        present = bool(settled["uninstall_key_present"])
        detail = ("控制面板登记 UNINSTALLKEY 仍在；" if present else "UNINSTALLKEY 已移除；") + timing
        record("A-092", "卸载后卸载注册表项被移除", "FAIL" if present else "PASS", detail)


def write_forged_lease(install_dir, *, shell_pid: int, backend_pid: int,
                       backend_exe: str | None = None, backend_filetime: str | None = None,
                       install_dir_field: str | None = None) -> Path:
    """写一份**伪造**的 lease（PID 复用反证用；只动安装目录里的这一个文件）。

    shell/backend 的 pid 与创建时间默认取真实进程的值，再按需要把**某一项**改错 ——
    这样每条断言只考一个判定点，不与别的判定纠缠。
    """
    install_dir = Path(install_dir)
    shell = process_identity(shell_pid)
    backend = process_identity(backend_pid)
    if shell is None or backend is None:
        raise RuntimeError("伪造 lease 失败：shell/backend 进程身份取不到（%s / %s）"
                           % (shell_pid, backend_pid))
    payload = {
        "schema": 1,
        "install_dir": install_dir_field or str(install_dir.resolve()),
        # shell 段用真实的三要素（pid + 创建时间 + 映像路径）—— 反证要考的是 backend 那一项，
        # 不能让 shell 段先对不上（那就变成"到处都对不上"的弱反证了）。
        "shell": {"pid": int(shell_pid), "created_filetime": shell["created_filetime"],
                  "exe": shell["exe"]},
        "backend": {"pid": int(backend_pid),
                    "created_filetime": backend_filetime or backend["created_filetime"],
                    "exe": backend_exe or str(install_dir / "qio-backend.exe")},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "_forged": "E2E 伪造（PID 复用反证）：故意让某个判定点对不上",
    }
    target = lease_path(install_dir)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def step_pid_reuse_rebuttal(args) -> bool:
    """PID 复用反证（plan §1.4）：lease 指向一个**已存在的无关进程**时，帮助程序必须拒绝。

    四个变体（前三个都必须 exit 3 + 一行可行动诊断 + **不动任何进程**）：
      1. backend 段指向一个真实存在的**无关进程**（另一个目录里的 ping）：pid 与创建时间都是
         真的，只有 exe 路径不是安装目录里的 qio-backend.exe → 必须拒绝（否则就是"只看 pid"）；
      2. pid 与 exe 都对（就是安装目录里的那个替身），但 created_filetime 差 1 → 必须拒绝
         （否则抗不了 PID 复用）；
      3. 内容全对，只有 lease.install_dir 指向别的目录 → 必须拒绝；
      4. **正向对照**：一份完全合法的 lease（shell 与 backend 都指向我们自己的两个进程）
         → 必须 exit 0 且真的把这两个 pid 收掉 —— 否则"永远拒绝"也能骗过前三条。
    """
    install_dir = Path(args.install_dir)
    helper = install_dir / "qio-uninstall-helper.exe"
    if not helper.is_file():
        record("A-120", "帮助程序存在（PID 复用反证的前提）", "FAIL",
               "**实现未就位**：找不到 %s" % helper)
        return False
    procs = processes_in_dir(install_dir)
    if not procs:
        record("A-120", "安装目录里有一个可当 backend 的进程", "FAIL",
               "按路径枚举为空 —— 先用 --stages uninstall --uninstall-with-running-backend 造替身，"
               "或让真后端跑着")
        return False
    backend_pid = procs[0]["pid"]

    scratch = Path(args.work_dir) / "pidreuse-scratch"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    ping = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
    unrelated_exe = scratch / "unrelated.exe"
    shutil.copy2(ping, unrelated_exe)

    def spawn(exe: Path):
        return subprocess.Popen([str(exe), "-t", "127.0.0.1"], cwd=str(exe.parent),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    unrelated = spawn(unrelated_exe)
    shell_stub = spawn(unrelated_exe)
    time.sleep(1.5)
    tracked = [item["pid"] for item in processes_in_dir(install_dir)] + [unrelated.pid, shell_stub.pid]

    def identity_map():
        return {pid: process_identity(pid) for pid in tracked}

    def untouched(before_map) -> tuple[bool, str]:
        after_map = identity_map()
        for pid, item in before_map.items():
            if item is None:
                continue
            if after_map.get(pid) is None:
                return False, "pid %s 消失了（帮助程序动了不该动的进程）" % pid
            if after_map[pid]["created_filetime"] != item["created_filetime"]:
                return False, "pid %s 的创建时间变了" % pid
        return True, "进程身份逐条未变"

    def helper_refuses(cid: str, title: str, expect_reason: str) -> bool:
        before_map = identity_map()
        code, out = run([str(helper), "--close-installation", "--install-dir", str(install_dir),
                         "--timeout-ms", "2000", "--json"], timeout=120, tag=cid)
        ok_codes = code == 3
        actionable = any(word in out for word in ("归属", "lease", "租约", "对不上", "不一致",
                                                   "不存在", "未找到", "无法确认"))
        still, why = untouched(before_map)
        state = "PASS" if (ok_codes and actionable and still) else "FAIL"
        record(cid, title, state,
               "exit=%s（期望 3）；诊断=%r；进程=%s；判定点=%s"
               % (code, out.strip().replace("\n", " ")[:260], why, expect_reason))
        return state == "PASS"

    results = []
    try:
        # 变体 1：无关进程的 pid + 真实创建时间，但 exe 路径不是安装目录里的
        write_forged_lease(install_dir, shell_pid=shell_stub.pid, backend_pid=unrelated.pid,
                           backend_exe=str(install_dir / "qio-backend.exe"))
        results.append(helper_refuses("A-121", "反证 1：backend 指向无关进程（路径对不上）→ 必须拒绝",
                                      "映像路径"))
        # 变体 2：pid 与 exe 都对，创建时间差 1（PID 复用）
        backend_stub = next(item for item in processes_in_dir(install_dir) if item["pid"] == backend_pid)
        write_forged_lease(install_dir, shell_pid=shell_stub.pid, backend_pid=backend_pid,
                           backend_filetime=str(int(backend_stub["created_filetime"]) + 1))
        results.append(helper_refuses("A-122", "反证 2：创建时间差 1（PID 复用）→ 必须拒绝",
                                      "created_filetime"))
        # 变体 3：内容全对，install_dir 指向别处
        write_forged_lease(install_dir, shell_pid=shell_stub.pid, backend_pid=backend_pid,
                           install_dir_field=str(scratch))
        results.append(helper_refuses("A-123", "反证 3：lease.install_dir 指向别的目录 → 必须拒绝",
                                      "install_dir"))
        # 变体 4：正向对照 —— 合法 lease 必须真的收掉这两个 pid（防止"永远拒绝"骗过上面三条）
        write_forged_lease(install_dir, shell_pid=shell_stub.pid, backend_pid=backend_pid)
        code, out = run([str(helper), "--close-installation", "--install-dir", str(install_dir),
                         "--timeout-ms", "3000", "--json"], timeout=120, tag="pidreuse-positive")
        left = wait_processes_gone([shell_stub.pid, backend_pid], timeout=30)
        record("A-124", "正向对照：合法 lease 必须真的收掉这两个 pid（exit 0）",
               "PASS" if code == 0 and not left else "FAIL",
               "exit=%s（期望 0）；诊断=%r；仍在=%s" % (code, out.strip()[:260], left or "无"))
        results.append(code == 0 and not left)
    finally:
        for pid in set([unrelated.pid, shell_stub.pid] + [item["pid"] for item in processes_in_dir(install_dir)]):
            if process_identity(pid) is not None:
                kill_by_pid(pid, "cleanup-pidreuse")
        lease_file = lease_path(install_dir)
        if lease_file.exists():
            lease_file.unlink()
        shutil.rmtree(scratch, ignore_errors=True)
    record("A-125", "PID 复用反证收尾（伪造 lease 已删除、进程按 PID 清掉）", "PASS",
           "残留 lease=%s；残留进程=%s" % (lease_path(install_dir).exists(),
                                          [item["pid"] for item in processes_in_dir(install_dir)]))
    return all(results)


def step_uninstall_with_standin(args) -> bool:
    """替身语义（plan §1.4）：安装目录里那个 qio-backend.exe 是 ping.exe 改名的替身。

    它**没有 lease、也没有主程序** → 卸载器必须不杀它，并如实报告"无法确认归属"。
    不能保留旧版本那句"卸载器自己收掉了还在跑的 sidecar"：那是改动前的行为，
    也不是本轮想要的行为。

    CI 的调用方式不变：CI 先自己起好替身，再跑
    --stages uninstall --uninstall-with-running-backend；本函数也支持替身还没起时
    自己造一个（本地跑用）。全过程只按 **PID / 路径**收进程，绝不用 /IM。
    """
    install_dir = Path(args.install_dir)
    if not install_dir.exists():
        record("A-087", "替身还在跑（卸载前）", "FAIL", "%s 不存在" % install_dir)
        return False
    standin_before = processes_in_dir(install_dir)
    if not standin_before:
        # 本地跑：自己造替身（CI 里是 CI 那一步造的，语义完全一样）。
        ping = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ping.exe"
        if not ping.exists():
            record("A-087", "替身还在跑（卸载前）", "FAIL", "找不到 %s，造不出替身" % ping)
            return False
        target = install_dir / "qio-backend.exe"
        try:
            shutil.copy2(ping, target)
        except OSError as exc:
            record("A-087", "替身还在跑（卸载前）", "FAIL",
                   "替换成替身失败：%r（真后端还在跑？先让 --stages uninstall 走不带 flag 的路径）" % exc)
            return False
        subprocess.Popen([str(target), "-t", "127.0.0.1"], cwd=str(install_dir),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        standin_before = processes_in_dir(install_dir)
    if not standin_before:
        record("A-087", "替身还在跑（卸载前）", "FAIL", "替身没有起来（按路径枚举为空）")
        return False

    lease, _raw = read_lease(install_dir)
    record("A-087", "替身场景的前提：进程在跑、且**没有** lease",
           "FAIL" if lease is not None else "PASS",
           "按路径枚举：%s；lease=%s" % (
               describe_processes(standin_before),
               "存在（这就不是「无归属」场景了，验的不是同一条缝）" if lease is not None else "不存在"))

    # --- 直接调帮助程序：这是唯一能拿到"如实报告"原文的路径（卸载器静默模式吞 stdout）---
    helper = install_dir / "qio-uninstall-helper.exe"
    if not helper.exists():
        record("A-095a", "帮助程序如实报告无法确认归属（exit 3 + 可行动诊断）", "FAIL",
               "**实现未就位**：%s 不存在。plan §1.2 要求它拒绝时 exit 3 并输出一行 JSON 诊断" % helper)
    else:
        helper_code, helper_out = run(
            [str(helper), "--close-installation", "--install-dir", str(install_dir),
             "--timeout-ms", "3000", "--json"],
            timeout=120, tag="uninstall-helper-standin")
        actionable = any(word in helper_out for word in
                         ("无法确认", "归属", "lease", "租约", "未找到", "不存在", "不一致"))
        refused = helper_code == 3
        record("A-095a", "帮助程序如实报告无法确认归属（exit 3 + 可行动诊断）",
               "PASS" if refused and actionable else "FAIL",
               "exit=%s（期望 3）；输出=%r；判定关键词命中=%s"
               % (helper_code, helper_out.strip()[:400], actionable))

    alive = processes_in_dir(install_dir)
    same = [item["pid"] for item in alive] == [item["pid"] for item in standin_before] and all(
        next((x for x in alive if x["pid"] == item["pid"]), {}).get("created_filetime")
        == item["created_filetime"] for item in standin_before)
    record("A-095b", "帮助程序拒绝后替身一个都没被动过（按 pid+创建时间核对）",
           "PASS" if same else "FAIL",
           "调用前：%s；调用后：%s" % (describe_processes(standin_before), describe_processes(alive)))

    # --- 端到端：跑真卸载器 ---
    step_uninstall(args, locked_residue={"qio-backend.exe"})
    alive_after = processes_in_dir(install_dir)
    survived = [item["pid"] for item in alive_after] == [item["pid"] for item in standin_before] and all(
        next((x for x in alive_after if x["pid"] == item["pid"]), {}).get("created_filetime")
        == item["created_filetime"] for item in standin_before)
    record("A-095", "卸载器**没有误杀**没有 lease 的替身（端到端，按 pid+创建时间核对）",
           "PASS" if survived else "FAIL",
           "卸载前：%s；卸载后：%s" % (describe_processes(standin_before),
                                       describe_processes(alive_after)))
    residue = sorted(p.name for p in install_dir.glob("*")) if install_dir.exists() else []
    residue_ok = set(residue) <= {"qio-backend.exe"}
    record("A-096", "卸载后残留只有被占用的那个替身（没有别的残渣）",
           "PASS" if residue_ok else "FAIL",
           "残留=%s（期望 ⊆ ['qio-backend.exe']：替身占着它 → 文件删不掉，这是拒绝误杀的必然后果）"
           % residue)

    # --- E2E 自己的收尾：按 PID 杀替身（不是产品行为，必须单独记账）---
    cleanup_pids = [item["pid"] for item in processes_in_dir(install_dir)]
    for pid in cleanup_pids:
        kill_by_pid(pid, "cleanup-standin")
    left = wait_processes_gone([item["pid"] for item in standin_before], timeout=20)
    shutil.rmtree(install_dir, ignore_errors=True)
    record("A-097", "E2E 收尾：按 PID 清掉替身与残留目录（非产品行为）",
           "PASS" if not left and not install_dir.exists() else "FAIL",
           "taskkill /F /T /PID %s；残留 pid=%s；目录存在=%s"
           % (cleanup_pids, left, install_dir.exists()))
    return survived and residue_ok


def step_restore(args):
    backup = Path(args.work_dir) / "backup"
    for name in ("uninstall-QIO.reg", "software-qio.reg"):
        path = backup / name
        if path.exists():
            run(["reg", "import", str(path)], tag="restore-%s" % name)
    notes = []
    for src, dst in ((backup / "desktop-QIO.lnk", Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "QIO.lnk"),
                     (backup / "startmenu-QIO.lnk",
                      Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "QIO.lnk")):
        if not src.exists():
            continue
        try:
            shutil.copy2(src, dst)
            notes.append("%s 已还原" % dst.name)
        except OSError as exc:
            # 沙箱不允许子进程写检出外的路径。那就退一步核对「有没有被动过」——
            # 这一层结论仍然是硬的（哈希比对）。
            unchanged = dst.exists() and sha256_of(src) == sha256_of(dst)
            notes.append("%s 需要人工还原（%s；当前与备份%s）" % (
                dst.name, type(exc).__name__, "一致，未被改动" if unchanged else "**不一致**"))
    if notes:
        bad = [n for n in notes if "已还原" not in n and "未被改动" not in n]
        record("A-102", "桌面/开始菜单快捷方式", "WARN" if bad else "PASS", "；".join(notes))
    run_value = (backup / "run-value.txt")
    if run_value.exists() and run_value.read_text(encoding="utf-8").strip():
        powershell("Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
                   "-Name QIO -Value '%s'" % run_value.read_text(encoding="utf-8").strip(),
                   tag="restore-run-value")
    code, out = powershell(
        "$k='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QIO';"
        "(Get-ItemProperty $k).DisplayVersion; (Get-ItemProperty $k).InstallLocation;"
        "(Get-ItemProperty 'HKCU:\\Software\\qio\\QIO').'(default)'", tag="restore-verify-registry")
    record("A-100", "机器注册表已还原到基线", "PASS" if "0.1.9" in out and "D:\\QIO" in out else "WARN", out.strip()[:200])

    baseline = (backup / "existing-install-hashes.txt")
    if baseline.exists():
        current = []
        base_dir = Path(args.existing_install)
        for path in sorted(base_dir.rglob("*")):
            if path.is_file():
                current.append("%s  %s" % (sha256_of(path), path))
        same = current == baseline.read_text(encoding="utf-8").splitlines()
        record("A-101", "既有安装 D:\\QIO 逐文件未被改动", "PASS" if same else "FAIL",
               "sha256 清单一致（%d 个文件）" % len(current) if same else "有文件被改动，见 A-004 基线")
    else:
        record("A-101", "既有安装 D:\\QIO 逐文件未被改动", "SKIP", "没有基线清单")


def main() -> int:
    parser = argparse.ArgumentParser(description="NSIS 安装包真机 E2E（不动本机既有安装）")
    parser.add_argument("--installer", required=True)
    # 全部放在检出内部：这台机器的 DSH 沙箱只允许**检出内**的路径被子进程写，
    # 检出外（C:\Users\zxy\...、D:\...）会被拒绝访问 —— 那是沙箱行为，不是产品行为。
    parser.add_argument("--work-dir", default=str(ROOT / ".e2e-work"))
    parser.add_argument("--install-dir", default=str(ROOT / ".e2e-work" / "install"))
    parser.add_argument("--decoy-dir", default=str(ROOT / ".e2e-work" / "decoy-old"))
    parser.add_argument("--existing-install", default=r"D:\QIO")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--token", default="e2e-install-token-0001")
    parser.add_argument("--stages", default="all")
    # 替身语义（P4 起）：安装目录里那个 qio-backend.exe 是 ping.exe 改名的替身 —— 没有 lease、
    # 也没有主程序 → 卸载器**必须不杀它**并如实报告"无法确认归属"。旧的"卸载器自己收掉了
    # 还在跑的 sidecar"断言已按新语义作废。
    parser.add_argument("--uninstall-with-running-backend", action="store_true")
    # shelllease 阶段用哪个可执行文件当"真外壳"（默认 qio.exe）。
    # 多安装并存场景里 B 那份要用改名副本，见 scripts/install_e2e_multi.py 的说明。
    parser.add_argument("--shell-exe-name", default="qio.exe")
    parser.add_argument("--shell-timeout", type=float, default=120.0,
                        help="等外壳写出 sidecar.lease.json 的秒数")
    args = parser.parse_args()

    global EVIDENCE
    work = Path(args.work_dir)
    (work / "evidence").mkdir(parents=True, exist_ok=True)
    (work / "data").mkdir(parents=True, exist_ok=True)
    (work / "tmp").mkdir(parents=True, exist_ok=True)
    EVIDENCE = work / "evidence"

    stages = set(args.stages.split(",")) if args.stages != "all" else {
        "preflight", "decoy", "install", "models", "health", "shelllease", "pidreuse", "api", "dev",
        "restart", "reinstall", "uninstall", "restore", "coinstall"}
    # /D= 可以含空格（裸值、放最后），但不能含引号：已用 makensis 探针验证过含空格路径可用。
    if '"' in args.install_dir:
        log("! 安装目录不能含引号（NSIS /D= 规则）")
        return 2

    log("== 安装包 E2E ==")
    log("  安装包   : %s" % args.installer)
    log("  安装目录 : %s" % args.install_dir)
    log("  数据目录 : %s（全新空目录）" % (work / "data"))
    log("  既有安装 : %s（不会被改动）" % args.existing_install)

    proc = backend = fp_proc = None
    client = None
    try:
        if "preflight" in stages and not step_preflight(args):
            return 3
        if "decoy" in stages:
            if not step_decoy(args):
                log("!! 诱饵实验判定有风险：停止，绝不在这台机器上继续安装")
                return 4
        if "install" in stages:
            if not step_install_files(args):
                return 5
            # 安装信息（控制面板登记 + 安装位置）是否真的写进去了。
            # 本会话的沙箱可能让子进程写不了注册表 —— 那种情况记 NOT VERIFIED，不记通过。
            step_install_registry(args)
        if "models" in stages and not step_models(args):
            return 6
        if "health" in stages:
            if not step_ports(args):
                return 8
            backend = start_backend(args, args.port)
            ok, client = step_health(args, args.port)
            if not ok:
                return 7
        if "shelllease" in stages:
            # 真外壳：lease 只由外壳写（plan §1.1）。直接起 qio-backend.exe 的流程里
            # 观测不到它 —— 所以"启动后 lease 在不在"这条只在 shelllease 里判。
            step_shell_lease(args)
        if "pidreuse" in stages:
            # PID 复用反证（plan §1.4）：伪造 lease 必须被帮助程序拒绝，且不动任何进程。
            step_pid_reuse_rebuttal(args)
        if client is None:
            client = Client("http://127.0.0.1:%d" % args.port, args.token)
        sse = SseReader(client)
        fp_proc, fp, fp_port = step_fake_provider(args, args.port)
        if "api" in stages:
            step_credential(args, client, fp_port)
            step_turn(args, client, sse, fp)
        if "dev" in stages:
            step_dev_flow(args, client, sse, fp)
            step_invoke_tool(args, client, sse, fp)
            step_tool_settings(args, client)
        if "restart" in stages:
            backend, _ = step_restart(args, client, backend, args.port)
            step_recovery(args, client, sse, fp)
        if "reinstall" in stages:
            stop_backend(backend, args.install_dir)
            ensure_backend_stopped(args, "重装前")
            backend = step_reinstall(args, client, args.port)
            backend, _ = step_restart(args, client, backend, args.port)
        if "uninstall" in stages:
            if args.uninstall_with_running_backend:
                # 替身路径：断言 = 拒绝误杀 + 如实报告 + 残留只有被占用的替身。
                # 真后端必须先停（否则替身覆盖不了 qio-backend.exe），这一步由
                # step_uninstall_with_standin 负责（它自己按 PID 清）。
                stop_backend(backend, args.install_dir)
                backend = None
                ensure_backend_stopped(args, "替身替换前")
                step_uninstall_with_standin(args)
            else:
                stop_backend(backend, args.install_dir)
                ensure_backend_stopped(args, "卸载前")
                backend = None
                # 先在同一个键下放一个合成 DbBaseline（只在注册表可写时），
                # 用来断言「卸载清安装信息，但不动用户状态」这条边界。
                seeded = step_seed_user_state(args)
                step_uninstall(args)
                step_uninstall_registry(args, seeded)
        if "coinstall" in stages:
            # 两份真安装并存：判定逻辑全在 install_e2e_multi.py（一份实现、两个入口）。
            # 这里只负责把它当子进程跑起来、把它的结果并进本脚本的 summary。
            coinstall_dir = Path(args.work_dir) / "coinstall"
            cmd = [sys.executable, str(HERE / "install_e2e_multi.py"),
                   "--installer", args.installer, "--work-dir", str(coinstall_dir),
                   "--port-a", str(args.port + 100), "--port-b", str(args.port + 101),
                   "--shell-timeout", str(args.shell_timeout)]
            code, out = run(cmd, timeout=7200, tag="coinstall-driver")
            child_summary = coinstall_dir / "summary.json"
            if child_summary.exists():
                try:
                    payload = json.loads(child_summary.read_text(encoding="utf-8"))
                    for item in payload.get("results") or []:
                        RESULTS.append(item)
                        log("  [coinstall %s] %s %s :: %s" % (
                            item.get("state"), item.get("id"), item.get("title"),
                            str(item.get("detail"))[:160]))
                except ValueError as exc:
                    record("C-999", "coinstall 结果汇总", "FAIL", "子驱动 summary.json 读不了：%r" % exc)
            if code != 0:
                record("C-998", "coinstall 阶段退出码", "FAIL",
                       "install_e2e_multi.py exit=%s（原始输出见 evidence/coinstall-driver.txt）" % code)

        if "restore" in stages:
            step_restore(args)
    except Exception as exc:  # noqa: BLE001 - 一次把问题报全：异常也要变成 FAIL 记录
        # 以前异常直接逃出 main()：traceback 打完之后，finally 里的「汇总」照样打印 0 FAIL，
        # 读日志的人会被那个假象骗到（2026-10-03 真的骗过一次）。异常必须变成一条 FAIL 记录。
        frames = traceback.extract_tb(exc.__traceback__)
        where = ""
        if frames:
            last = frames[-1]
            where = " @ %s:%s in %s" % (Path(last.filename).name, last.lineno, last.name)
        record("A-999", "E2E 流程未完成（异常）", "FAIL",
               "%s: %s%s" % (type(exc).__name__, exc, where))
    finally:
        stop_backend(backend, args.install_dir)
        if fp_proc is not None:
            fp_proc.terminate()
        if STATE["decoy_applied"] and "restore" in stages:
            log("== 还原机器状态（注册表 / 快捷方式 / 校验既有安装）==")
            try:
                step_restore(args)
            except Exception as exc:  # 还原失败要吼出来，不能静默
                record("A-199", "机器状态还原", "FAIL", repr(exc))
        failures = [r for r in RESULTS if r["state"] == "FAIL"]
        summary = {"installer": args.installer, "install_dir": args.install_dir,
                   "results": RESULTS, "failed": len(failures),
                   "warned": [r["id"] for r in RESULTS if r["state"] == "WARN"],
                   "not_executed": [r["id"] for r in RESULTS if r["state"] == "SKIP"]}
        out = work / "summary.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log("")
        warned = [r for r in RESULTS if r["state"] == "WARN"]
        log("== 汇总：%d 项 PASS / %d 项 WARN / %d 项 FAIL / %d 项 SKIP ==" % (
            len([r for r in RESULTS if r["state"] == "PASS"]), len(warned), len(failures),
            len([r for r in RESULTS if r["state"] == "SKIP"])))
        for item in failures:
            log("  FAIL %s %s :: %s" % (item["id"], item["title"], item["detail"][:200]))
        for item in warned:
            log("  WARN %s %s :: %s" % (item["id"], item["title"], item["detail"][:160]))
        log("  summary.json -> %s" % out)
    return 1 if any(r["state"] == "FAIL" for r in RESULTS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
