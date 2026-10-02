"""工具子进程的真实强制隔离（Windows 内核能力）——以及**做不到什么**的诚实清单。

这个模块只做「操作系统真的会拦」的事，不做「策略上声明」的事。本机实测（2026-10-02，
非提权普通账户 admin\\zxy）确认可用的两件事：

1. **Job Object**（工具子进程创建后指派）：
   * 进程内存上限：工具试图分配超过上限时拿到 MemoryError，而不是把整机吃光；
   * 活动进程数上限：超出后 CreateProcess 返回 WinError 1816（配额不足），fork 炸弹失效；
   * KILL_ON_JOB_CLOSE：QIO 进程退出（或被强杀）时，句柄关闭 → **整棵进程树由内核收掉**，
     不依赖 taskkill 有没有跑成功。
2. **低完整性级别（Low IL，Windows MIC）**：把工具子进程的令牌完整性级别降到 Low 之后，
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

import ctypes
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"

# 关掉低完整性降级的逃生开关（支持/回滚用；默认开启）。
DISABLE_ENV = "QIO_TOOL_LOW_INTEGRITY"

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

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _LIMIT_ACTIVE_PROCESS = 0x00000008
    _LIMIT_PROCESS_MEMORY = 0x00000100
    _LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_INFORMATION = 0x0400
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
    """当前平台能不能给出**强制**隔离（不是声明）。"""
    if not WINDOWS:
        return False
    return _env_disabled() is False


def _env_disabled() -> bool:
    value = (os.environ.get(DISABLE_ENV) or "").strip().lower()
    return value in {"0", "false", "no", "off"}


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
    process = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not process:
        raise OSError(f"OpenProcess failed: {ctypes.get_last_error()}")
    try:
        if not _k32.AssignProcessToJobObject(job, process):
            raise OSError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")
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


def label_low(path: str) -> str:
    """给目录打低完整性标签（含继承），返回使用的办法：sddl / icacls / 空。"""
    descriptor = ctypes.c_void_p()
    if _advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _LOW_LABEL_SDDL, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        try:
            code = _advapi.SetNamedSecurityInfoW(
                str(path), _SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION,
                None, None, None, descriptor,
            )
            if code == 0:
                return "sddl"
        finally:
            _k32.LocalFree(descriptor)
    # 回退：icacls 同时把**已存在**的子文件一起打上标签（/T）。
    try:
        done = subprocess.run(
            ["icacls", str(path), "/setintegritylevel", "(OI)(CI)L", "/T"],
            capture_output=True, timeout=30,
        )
        if done.returncode == 0:
            return "icacls"
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def harden(process, *, scratch_dir: str | None = None, policy: Any = None) -> IsolationOutcome:
    """给刚创建的工具子进程加真实强制隔离。**永远不抛异常**。

    * Job Object：内存 / 活动进程数上限 + 关句柄即收整棵树；
    * 低完整性降级：工具写不进用户文件与 QIO 数据目录（读不受影响）；
      当工具显式声明了文件系统能力时跳过（它被批准在工作区外写文件）。

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
    if _env_disabled():
        problems.append(
            f"低完整性降级被 {DISABLE_ENV} 关掉（显式逃生开关，保护面缩小）"
        )
    elif declared_filesystem:
        # 工具被批准在工作区外写文件：降级只会让它以权限错误失败，不做。
        problems.append("工具声明了文件系统能力，未做低完整性降级（job limits 仍然生效）")
    else:
        if scratch_dir:
            how = label_low(scratch_dir)
            if not how:
                # 标不上标签仍然降级：安全属性优先；工具自己的目录写不了会在结果里显形。
                problems.append("临时目录打低完整性标签失败（工具可能写不了自己的工作目录）")
        try:
            _set_low_integrity(int(pid))
            mechanisms.append(_MECHANISM_LOW_IL)
        except Exception as exc:  # noqa: BLE001 - 同上
            problems.append(f"low_integrity: {exc}")
            logger.debug("low integrity downgrade failed for pid=%s: %s", pid, exc)

    if not mechanisms:
        return IsolationOutcome(applied=False, detail="没有可用的强制隔离机制", problems=tuple(problems))
    return IsolationOutcome(
        applied=True,
        mechanisms=tuple(mechanisms),
        detail=(
            "已强制：进程内存/活动进程上限 + 关句柄即收整棵树"
            + ("；工具进程降为低完整性（写不进用户文件与 QIO 数据目录，读不受限）"
               if _MECHANISM_LOW_IL in mechanisms else "")
        ),
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
