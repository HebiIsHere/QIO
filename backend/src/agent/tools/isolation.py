"""工具子进程的真实强制隔离（Windows 内核能力）——以及**做不到什么**的诚实清单。

这个模块只做「操作系统真的会拦」的事，不做「策略上声明」的事。本机实测（2026-10-02，
非提权普通账户 admin\\zxy）确认可用的两件事：

1. **Job Object**（工具子进程创建后指派）：
   * 进程内存上限：工具试图分配超过上限时拿到 MemoryError，而不是把整机吃光；
   * 活动进程数上限：超出后 CreateProcess 返回 WinError 1816（配额不足），fork 炸弹失效；
   * KILL_ON_JOB_CLOSE：QIO 进程退出（或被强杀）时，句柄关闭 → **整棵进程树由内核收掉**，
     不依赖 taskkill 有没有跑成功。
2. **低完整性级别（Low IL，Windows MIC）——默认关闭，`QIO_TOOL_LOW_INTEGRITY=1` 打开**：
   把工具子进程的令牌完整性级别降到 Low 之后，
   它对**标记为 Medium 及以上的对象没有写权限** —— 用户文件、QIO 数据目录（由正常权限的
   QIO 进程创建）都写不进去；副作用是**读仍然可以**（MIC 只管写向上）。本机实测：
   同一段代码在降级前 user_files/qio_data 写入成功，降级后 PermissionError。

**没有做到的事（必须一起读，不要只看上面）**：

* **网络没有任何强制隔离**：Low IL 不影响出网；本机（非提权）也无法装 WFP 过滤器 —— 未实现。
* **读没有隔离**：低完整性的工具仍然能读用户文件、读 QIO 数据目录（只要 ACL 允许）。
* **路径猜不到 ≠ 访问不到**：环境变量 / cwd / sys.path 里不会出现 QIO 数据目录，
  但工具可以按常见路径去猜。指针没泄露不等于边界存在。
* **AppContainer 未接**：本机实测 CreateAppContainerProfile 直接返回 0x80070005（拒绝访问），
  需要提权/额外的清单与 ACL 授权；受限令牌可以创建，但用它启动新进程会改变 sandbox 的
  启动路径（超出本阶段的追加式改动边界），见 docs/security/tool-execution-isolation.md。
* **POSIX 上本模块不提供任何隔离**（容器路径由 executor=docker 负责）：返回 unsupported，
  **行为与改动前完全一致**。

设计约束（写在代码里免得后面被误解）：

* 钩子**永远不抛异常**：平台不支持 / 调用失败 → 返回一条带原因的 outcome，调用方行为不变，
  outcome 会跟着 SandboxResult 一起交出去（可诊断，不静默）。
* 只做**追加式**集成：sandbox.py 在子进程创建之后调用 harden(process)，结束时调用 release(process)。
* 工具声明了文件系统能力（policy.filesystem 非空）时**不做低完整性降级**：它已经被批准
  在工作区外写文件，降级只会让它以权限错误失败。Job Object 限制照常生效。
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
from ctypes import wintypes  # 顶层导入：模块级还有平台无关的类型定义要用到它
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"

# 低完整性降级的开关。**默认关闭**：CI（windows-latest）实测证明降级一旦生效，
# 工具的 scratch 目录与 mock 夹具目录都写不进去 —— 标签没有得到可核实的落地。
# 默认只保留 Job Object 这一项真实强制（它不受写权限影响）。
# 置为 1/true/on/yes 才打开；打开时也会**先核实标签真的打上**，核实不了就不降级。
LOW_INTEGRITY_ENV = "QIO_TOOL_LOW_INTEGRITY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Job Object 上限。取「远高于正常工具、远低于吃光整机」的值：
# 单个工具进程最多 1 GiB 内存、最多同时 32 个进程（含它自己拉起的子进程）。
JOB_PROCESS_MEMORY_BYTES = 1024 * 1024 * 1024
JOB_ACTIVE_PROCESS_LIMIT = 32

LOW_INTEGRITY_SID = "S-1-16-4096"
MEDIUM_INTEGRITY_SID = "S-1-16-8192"

_MECHANISM_JOB = "job_object"
_MECHANISM_LOW_IL = "low_integrity"
_MECHANISM_NONE = "none"


@dataclass(frozen=True)
class IsolationOutcome:
    """一次工具执行实际拿到（或没拿到）的强制隔离。"""

    applied: bool
    mechanisms: tuple[str, ...] = ()
    detail: str = ""
    limits: dict[str, Any] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "mechanisms": list(self.mechanisms),
            "detail": self.detail,
            "limits": dict(self.limits),
            "problems": list(self.problems),
        }


UNSUPPORTED = IsolationOutcome(
    applied=False,
    detail=(
        "POSIX：本模块不提供强制隔离（需要容器执行器 executor=docker）；"
        "受限子进程与改动前完全一致"
    ),
)


# --------------------------------------------------------------------------
# Windows 绑定
# --------------------------------------------------------------------------

if WINDOWS:  # pragma: no cover - 平台分支
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _EXT_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", _SID_AND_ATTRIBUTES)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.IsProcessInJob.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)
    ]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    _advapi.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)
    ]
    _advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
    ]
    _advapi.SetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    _advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD),
    ]
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.LocalFree.restype = ctypes.c_void_p

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _LIMIT_ACTIVE_PROCESS = 0x00000008
    _LIMIT_PROCESS_MEMORY = 0x00000100
    _LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TOKEN_ADJUST_DEFAULT = 0x0080
    _TOKEN_QUERY = 0x0008
    _TOKEN_INTEGRITY_LEVEL = 25
    _SE_GROUP_INTEGRITY = 0x00000020
    _SE_FILE_OBJECT = 1
    _LABEL_SECURITY_INFORMATION = 0x00000010
    _SDDL_REVISION_1 = 1
    # 低完整性标签 + 继承给子对象；SDDL 里 LW = Low integrity level SID。
    _LOW_LABEL_SDDL = "S:(ML;OICI;NW;;;LW)"

    _jobs_lock = threading.Lock()
    _jobs: dict[int, int] = {}          # pid -> job handle


def supported() -> bool:
    """当前平台能不能给出**强制**隔离（不是声明）。Job Object 是默认生效的那一项。"""
    return WINDOWS


def low_integrity_enabled() -> bool:
    """低完整性降级是否被显式打开（默认关；见 LOW_INTEGRITY_ENV）。"""
    value = (os.environ.get(LOW_INTEGRITY_ENV) or "").strip().lower()
    return value in _TRUTHY


def _job_limits() -> dict[str, Any]:
    return {
        "process_memory_bytes": JOB_PROCESS_MEMORY_BYTES,
        "active_processes": JOB_ACTIVE_PROCESS_LIMIT,
        "kill_on_close": True,
    }


def _create_job() -> int:
    handle = _k32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
    info = _EXT_LIMIT()
    info.BasicLimitInformation.LimitFlags = (
        _LIMIT_PROCESS_MEMORY | _LIMIT_ACTIVE_PROCESS | _LIMIT_KILL_ON_JOB_CLOSE
    )
    info.BasicLimitInformation.ActiveProcessLimit = JOB_ACTIVE_PROCESS_LIMIT
    info.ProcessMemoryLimit = JOB_PROCESS_MEMORY_BYTES
    if not _k32.SetInformationJobObject(
        handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        _k32.CloseHandle(handle)
        raise OSError(f"SetInformationJobObject failed: {error}")
    return int(handle)


def _assign_job(job: int, pid: int) -> None:
    """把进程放进 job，并**读回核实**它真的在里面（不是只看调用返回）。"""
    # 核实成员资格需要查询权限：少了它 IsProcessInJob 会以 err=5 失败（实测踩过）。
    process = _k32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
        False, pid,
    )
    if not process:
        raise OSError(f"OpenProcess failed: {ctypes.get_last_error()}")
    try:
        if not _k32.AssignProcessToJobObject(job, process):
            raise OSError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")
        in_job = wintypes.BOOL()
        if not _k32.IsProcessInJob(process, job, ctypes.byref(in_job)) or not in_job.value:
            raise OSError(
                f"IsProcessInJob 核实失败：err={ctypes.get_last_error()} in_job={bool(in_job.value)}"
            )
    finally:
        _k32.CloseHandle(process)


def _sid(sid_text: str) -> int:
    sid = ctypes.c_void_p()
    if not _advapi.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)):
        raise OSError(f"ConvertStringSidToSidW({sid_text}) failed: {ctypes.get_last_error()}")
    return int(sid.value)


def integrity_of_token(token: int) -> str:
    """读一个令牌的完整性级别 SID 字符串（诊断/测试用）。"""
    size = wintypes.DWORD(0)
    _advapi.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
    if not size.value:
        raise OSError("GetTokenInformation(size) failed")
    buffer = ctypes.create_string_buffer(size.value)
    if not _advapi.GetTokenInformation(
        token, _TOKEN_INTEGRITY_LEVEL, buffer, size.value, ctypes.byref(size)
    ):
        raise OSError(f"GetTokenInformation failed: {ctypes.get_last_error()}")
    label = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
    text = wintypes.LPWSTR()
    _advapi.ConvertSidToStringSidW(label.Label.Sid, ctypes.byref(text))
    return str(text.value)


def integrity_of_process(pid: int) -> str:
    """读某个进程的完整性级别（测试用它证明降级真的发生了）。"""
    process = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
    if not process:
        raise OSError(f"OpenProcess(query) failed: {ctypes.get_last_error()}")
    token = wintypes.HANDLE()
    try:
        if not _advapi.OpenProcessToken(
            process, _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise OSError(f"OpenProcessToken failed: {ctypes.get_last_error()}")
        return integrity_of_token(int(token.value))
    finally:
        _k32.CloseHandle(process)
        if token:
            _k32.CloseHandle(token)


def _set_low_integrity(pid: int) -> None:
    """把子进程的令牌降到 Low 完整性（内核按此做强制访问检查）。"""
    process = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
    if not process:
        raise OSError(f"OpenProcess failed: {ctypes.get_last_error()}")
    token = wintypes.HANDLE()
    try:
        if not _advapi.OpenProcessToken(
            process, _TOKEN_ADJUST_DEFAULT | _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise OSError(f"OpenProcessToken failed: {ctypes.get_last_error()}")
        label = _TOKEN_MANDATORY_LABEL()
        label.Label.Sid = _sid(LOW_INTEGRITY_SID)
        label.Label.Attributes = _SE_GROUP_INTEGRITY
        if not _advapi.SetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, ctypes.byref(label), ctypes.sizeof(label)
        ):
            raise OSError(f"SetTokenInformation failed: {ctypes.get_last_error()}")
    finally:
        _k32.CloseHandle(process)
        if token:
            _k32.CloseHandle(token)


def integrity_label_of(path: str) -> str:
    """读回一个路径的强制标签（SDDL 片段）。读不到就回空串 —— 这是核实用的。

    「调用返回 0」不等于「标签真的落上了」（CI 上就这么翻过一次车：以为打上了，
    结果工具连自己的 scratch 都写不进去）。所以这里读回来自己看。
    """
    sd = ctypes.c_void_p()
    code = _advapi.GetNamedSecurityInfoW(
        str(path), _SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION,
        None, None, None, None, ctypes.byref(sd),
    )
    if code != 0 or not sd.value:
        return ""
    try:
        text = wintypes.LPWSTR()
        length = wintypes.DWORD(0)
        ok = _advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            sd, _SDDL_REVISION_1, _LABEL_SECURITY_INFORMATION,
            ctypes.byref(text), ctypes.byref(length),
        )
        return str(text.value) if ok and text.value else ""
    finally:
        _k32.LocalFree(sd)


def label_is_low(sddl_text: str) -> bool:
    """读回来的 SDDL 里是不是**真的**有一条低完整性标签 ACE。

    必须同时看到强制标签 ACE 与 Low：只有 `LW` 字样不算 —— 实测过一次
    `SetNamedSecurityInfoW` 返回 0、读回却是 `S:AINO_ACCESS_CONTROL`（根本没有标签），
    拿它当成功就会把工具自己的 scratch 写死。
    """
    text = sddl_text or ""
    has_label_ace = "(ML;" in text
    has_low = "S-1-16-4096" in text or ";LW" in text or ";;LW)" in text
    return has_label_ace and has_low


_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', wintypes.WCHAR * 260),
    ]


def _child_processes(parents: set) -> set:
    snapshot = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return set()
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        found = set()
        ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ParentProcessID in parents:
                found.add(int(entry.th32ProcessID))
            ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
        return found
    finally:
        _k32.CloseHandle(snapshot)


def _low_process_can_write(path: str) -> tuple:
    # 用一个**一次性 Low 子进程**真的往目录写一个文件：这是唯一可信的判据。
    # 「标签读回 Low」不等于「Low 进程写得进去」：CI（High 完整性的 runner）实测
    # 标签读回 S:AI(ML;OICI;NW;;;LW)，Low 子进程写同一个目录依然 PermissionError。
    # 探针不过就不降级（fail-safe），并把原因写进 problems。
    probe_path = os.path.join(str(path), '.qio-low-write-probe')
    quote = chr(34)
    command = 'pause >nul & echo x> ' + quote + probe_path + quote
    process = None
    try:
        process = subprocess.Popen(
            ['cmd', '/c', command],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _set_low_integrity(int(process.pid))
        actual = integrity_of_process(int(process.pid))
        if actual != LOW_INTEGRITY_SID:
            return False, 'probe-not-low: ' + str(actual)
        process.stdin.write(b'x')
        process.stdin.flush()
        code = process.wait(timeout=15)
    except Exception as exc:
        return False, 'probe-error: ' + str(exc)
    finally:
        if process is not None:
            with contextlib.suppress(Exception):
                process.kill()
    wrote = os.path.exists(probe_path)
    with contextlib.suppress(OSError):
        os.remove(probe_path)
    if wrote and code == 0:
        return True, 'ok'
    return False, 'write-denied exit=' + str(code)

def _harden_tree(root_pid: int, job: int, problems: list[str]) -> tuple:
    """把 worker **以及整棵后代**降级 + 指派进 job + 读回核实，直到收敛。

    为什么：Windows 上 `sys.executable` 可能是 uv 的 trampoline（.venv\\Scripts\\python.exe
    先起一个真解释器再转发参数），**工具代码跑在子进程里**；只处理被 spawn 的那一个 pid
    等于两层强制全部落空（CI 实测：工具以普通完整性跑完，能写用户目录）。

    递归：每轮把新发现的后代加入已知集合，直到连续若干轮没有新进程（trampoline 可能不止一层）；
    时间上仍然安全 —— sandbox 把请求写进 stdin 之后工具代码才会执行，这一步远早于它。

    返回 (处理过的 pid 列表, 核实失败的 pid 列表)。
    """
    handled: list[int] = []
    failed: list[int] = []
    known = {int(root_pid)}
    idle = 0
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        fresh = _child_processes(known) - known
        if fresh:
            idle = 0
        else:
            idle += 1
            if idle >= 5:  # 连续 5 轮（约 100ms）没有新进程：认为收敛
                break
        for pid in fresh:
            known.add(pid)
            try:
                _set_low_integrity(pid)
                if job:
                    _assign_job(job, pid)
                handled.append(pid)
            except Exception as exc:  # noqa: BLE001 - 单个后代失败不放弃整棵树
                problems.append(f"后代进程 {pid} 未能纳入隔离：{exc}")
                failed.append(pid)
        time.sleep(0.02)

    for pid in handled:
        actual = integrity_of_process(pid)
        if actual != LOW_INTEGRITY_SID:
            problems.append(
                f"后代进程 {pid} 降级后读回令牌是 {actual or '读不到'}，按未降级处理"
            )
            failed.append(pid)
    return handled, failed


def label_low(path: str) -> str:
    """给目录打低完整性标签（含继承）并**读回核实**；成功返回 "icacls"，否则回空串。

    为什么只用 icacls（2026-10-03 最小实验，Medium 父进程 + Low 子进程）：

        SDDL 路径  : SetNamedSecurityInfoW rc=0，读回 "S:AINO_ACCESS_CONTROL"（没有标签 ACE）
                     → 子进程 scratch=WRITE-DENIED（标签没落上，目录仍是 Medium）
        icacls 路径: exit=0，读回 "S:AI(ML;OICI;NW;;;LW)"，icacls 也显示
                     Mandatory Label\\Low Mandatory Level:(OI)(CI)(NW)
                     → 子进程 scratch=WRITE-OK，user/qio=WRITE-DENIED

    也就是说「调用返回 0」完全不可信；只有**读回看到 (ML;…;LW)** 才算打上。
    icacls 同时处理已存在的子文件（/T），SDDL 那次连目录本身都没改对。
    """
    try:
        done = subprocess.run(
            ["icacls", str(path), "/setintegritylevel", "(OI)(CI)L", "/T"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return "icacls" if label_is_low(integrity_label_of(path)) else ""


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def harden(
    process,
    *,
    scratch_dir: str | None = None,
    policy: Any = None,
    extra_writable_dirs: "list[str] | tuple[str, ...] | None" = None,
) -> IsolationOutcome:
    """给刚创建的工具子进程加真实强制隔离。**永远不抛异常**。

    * Job Object：内存 / 活动进程数上限 + 关句柄即收整棵树（**默认生效**）；
    * 低完整性降级（**默认关闭**，`QIO_TOOL_LOW_INTEGRITY=1` 打开）：工具写不进用户文件
      与 QIO 数据目录（读不受影响）。打开时：
      - 先把 `scratch_dir` 与 `extra_writable_dirs`（调用方声明的「工具合法需要写」的目录，
        例如 mock 夹具目录）都打上低标签**并读回核实**；
      - 任何一个核实不了 → **跳过降级**（fail-safe）：宁可少一层写边界，
        也不能让工具连自己的 scratch 都写不进去；
      - 工具显式声明了文件系统能力（policy.filesystem 非空）时同样跳过。

    返回的 outcome 会跟 SandboxResult 一起交出去，失败/跳过都带原因。
    """
    if not WINDOWS:
        return UNSUPPORTED
    pid = getattr(process, "pid", None)
    if not pid:
        return IsolationOutcome(applied=False, detail="拿不到子进程 pid，未加任何强制隔离")

    mechanisms: list[str] = []
    problems: list[str] = []
    limits: dict[str, Any] = {}

    job = None
    try:
        job = _create_job()
        _assign_job(job, int(pid))
        with _jobs_lock:
            previous = _jobs.pop(int(pid), None)
            _jobs[int(pid)] = job
        if previous:
            _k32.CloseHandle(previous)
        mechanisms.append(_MECHANISM_JOB)
        limits = _job_limits()
    except Exception as exc:  # noqa: BLE001 - 隔离失败不能让工具调用失败
        if job:
            _k32.CloseHandle(job)
        problems.append(f"job_object: {exc}")
        logger.debug("job object hardening failed for pid=%s: %s", pid, exc)

    declared_filesystem = bool(getattr(policy, "filesystem", None)) if policy else False
    writable = [str(item) for item in (extra_writable_dirs or []) if item]
    if scratch_dir:
        writable.insert(0, str(scratch_dir))
    if not low_integrity_enabled():
        # 默认关闭：CI 上降级一旦生效，工具连自己的 scratch 与 mock 夹具都写不进去。
        problems.append(
            f"低完整性降级默认关闭（置 {LOW_INTEGRITY_ENV}=1 打开；打开前会先核实标签）"
        )
    elif declared_filesystem:
        # 工具被批准在工作区外写文件：降级只会让它以权限错误失败，不做。
        problems.append("工具声明了文件系统能力，未做低完整性降级（job limits 仍然生效）")
    else:
        # 只有「所有需要可写的目录都**核实**为低标签」才降级：
        # 核实不了就不降级（fail-safe）—— 宁可少一层写边界，也不能让工具连 scratch 都写不了。
        unlabelled: list[str] = []
        for path in writable:
            if not label_low(path):
                unlabelled.append(path)
        # 光有标签还不够：CI（High 完整性的 runner）实测标签读回 Low、Low 子进程照样写不进。
        # 所以再用一个一次性 Low 进程真的写一次；写不进去就不降级（否则工具直接罢工）。
        unwritable: list[str] = []
        for path in writable:
            ok, why = _low_process_can_write(path)
            if not ok:
                unwritable.append(path + "（" + why + "）")
        if unlabelled:
            problems.append(
                "低完整性降级已跳过：以下目录没能核实为低标签，降级会让工具写不进去 "
                + "，".join(unlabelled)
            )
        elif unwritable:
            problems.append(
                "低完整性降级已跳过：Low 进程实测写不了以下目录（降级会让工具连 scratch 都用不了）"
                + "，".join(unwritable)
            )
        else:
            try:
                _set_low_integrity(int(pid))
            except Exception as exc:  # noqa: BLE001 - 同上
                problems.append(f"low_integrity: {exc}")
                logger.debug("low integrity downgrade failed for pid=%s: %s", pid, exc)
            else:
                # 关键：真正跑工具代码的进程可能**不是**我们 spawn 的那个（uv trampoline 会再起
                # 一个真解释器）。整棵后代都要降级 + 进 job，并且逐个读回核实。
                _handled, failed = _harden_tree(int(pid), job, problems)
                # C5：只有当 worker 与**所有**已发现的后代都核实为 Low 时，才允许声称低完整性。
                actual = integrity_of_process(int(pid))
                if actual != LOW_INTEGRITY_SID:
                    problems.append(
                        f"low_integrity: 降级后读回令牌仍是 {actual or '读不到'}，"
                        "按未降级处理（不声称低完整性）"
                    )
                elif failed:
                    problems.append(
                        "low_integrity: 有后代进程未能核实为低完整性（见上面的原因），"
                        "按未降级处理（不声称低完整性）"
                    )
                else:
                    mechanisms.append(_MECHANISM_LOW_IL)

    if not mechanisms:
        return IsolationOutcome(applied=False, detail="没有可用的强制隔离机制", problems=tuple(problems))
    # C5：detail 只描述**真的核实过**的机制（哪一层没成，就说哪一层没成）。
    parts: list[str] = []
    if _MECHANISM_JOB in mechanisms:
        parts.append("Job Object 已核实：进程内存/活动进程数上限 + 关句柄即收整棵树")
    if _MECHANISM_LOW_IL in mechanisms:
        parts.append("工具进程令牌已读回核实为低完整性（写不进用户文件与 QIO 数据目录，读不受限）")
    return IsolationOutcome(
        applied=True,
        mechanisms=tuple(mechanisms),
        detail="；".join(parts),
        limits=limits,
        problems=tuple(problems),
    )


def release(process) -> None:
    """工具调用结束后关掉 job 句柄（KILL_ON_JOB_CLOSE：不留任何进程）。"""
    if not WINDOWS:
        return
    pid = getattr(process, "pid", None)
    if not pid:
        return
    with _jobs_lock:
        job = _jobs.pop(int(pid), None)
    if job:
        try:
            _k32.CloseHandle(job)
        except Exception:  # noqa: BLE001 - 清理失败只记日志
            logger.debug("closing job handle failed for pid=%s", pid, exc_info=True)


def active_jobs() -> int:
    """还没有被释放的 job 数量（诊断 / 泄漏检查用）。"""
    with _jobs_lock:
        return len(_jobs)
