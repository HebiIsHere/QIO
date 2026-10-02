"""C1 最小实验：Low Integrity 下「工具自己的 scratch 目录能不能写」到底取决于什么。

为什么单写一个脚本：上一阶段在 CI（windows-latest，普通完整性）上看到 scratch == WRITE-DENIED，
而本机全绿 —— 因为本机的 uv 把 python 降到 Low 完整性，降级成了 no-op。这个脚本不依赖 QIO 工具系统，
只用 stdlib + ctypes，把每一步的事实打出来：环境 / 目录 / ACL / 标签（设置前后都读回）/ 子进程令牌 / 写入结果。

用法（关键：要用**普通完整性**的解释器当父进程才复现得了 CI 场景）：
    & "C:\...\Python312\python.exe" scripts\low_integrity_probe.py      # Medium 父进程（CI 场景）
    uv run --frozen python scripts\low_integrity_probe.py                  # Low 父进程（本机默认）
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from ctypes import wintypes

adv = ctypes.WinDLL("advapi32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

TOKEN_ADJUST_DEFAULT, TOKEN_QUERY = 0x0080, 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
TOKEN_INTEGRITY_LEVEL = 25
SE_FILE_OBJECT = 1
LABEL_SECURITY_INFORMATION = 0x00000010
SDDL_REVISION_1 = 1
LOW_SDDL = "S:(ML;OICI;NW;;;LW)"


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
adv.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
adv.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.LocalFree.argtypes = [ctypes.c_void_p]
k32.LocalFree.restype = ctypes.c_void_p
k32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]


def _sid_text(sid_ptr: int) -> str:
    text = wintypes.LPWSTR()
    if not adv.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(text)):
        return ""
    return str(text.value or "")


def token_integrity_of_process(pid: int) -> str:
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, int(pid))
    if not handle:
        return f"OpenProcess failed err={ctypes.get_last_error()}"
    token = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return f"OpenProcessToken failed err={ctypes.get_last_error()}"
        size = wintypes.DWORD(0)
        adv.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf, size.value, ctypes.byref(size)):
            return f"GetTokenInformation failed err={ctypes.get_last_error()}"
        label = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        return _sid_text(label.Label.Sid)
    finally:
        k32.CloseHandle(handle)
        if token:
            k32.CloseHandle(token)


def current_integrity() -> str:
    return token_integrity_of_process(os.getpid())


def downgrade_to_low(pid: int) -> str:
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, int(pid))
    if not handle:
        return f"OpenProcess failed err={ctypes.get_last_error()}"
    token = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(handle, TOKEN_ADJUST_DEFAULT | TOKEN_QUERY, ctypes.byref(token)):
            return f"OpenProcessToken failed err={ctypes.get_last_error()}"
        sid = ctypes.c_void_p()
        if not adv.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(sid)):
            return f"ConvertStringSidToSidW failed err={ctypes.get_last_error()}"
        label = TOKEN_MANDATORY_LABEL()
        label.Label.Sid = sid
        label.Label.Attributes = 0x20
        if not adv.SetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, ctypes.byref(label), ctypes.sizeof(label)):
            return f"SetTokenInformation failed err={ctypes.get_last_error()}"
        return "ok"
    finally:
        k32.CloseHandle(handle)
        if token:
            k32.CloseHandle(token)


def label_via_sddl(path: str) -> str:
    descriptor = ctypes.c_void_p()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(LOW_SDDL, SDDL_REVISION_1, ctypes.byref(descriptor), None):
        return f"ConvertStringSecurityDescriptorToSecurityDescriptorW err={ctypes.get_last_error()}"
    try:
        code = adv.SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None, descriptor)
        return f"SetNamedSecurityInfoW rc={code}"
    finally:
        k32.LocalFree(descriptor)


def label_via_icacls(path: str) -> str:
    done = subprocess.run(["icacls", str(path), "/setintegritylevel", "(OI)(CI)L", "/T"], capture_output=True, text=True, encoding="gbk", errors="replace")
    return f"icacls exit={done.returncode}"


def read_label(path: str) -> str:
    sd = ctypes.c_void_p()
    code = adv.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, None, None, None, None, ctypes.byref(sd))
    if code != 0 or not sd.value:
        return f"GetNamedSecurityInfoW rc={code}"
    try:
        text = wintypes.LPWSTR()
        length = wintypes.DWORD(0)
        ok = adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, SDDL_REVISION_1, LABEL_SECURITY_INFORMATION, ctypes.byref(text), ctypes.byref(length))
        return str(text.value) if ok and text.value else "（没有标签 ACE）"
    finally:
        k32.LocalFree(sd)


def icacls_lines(path: str) -> list[str]:
    done = subprocess.run(["icacls", str(path)], capture_output=True, text=True, encoding="gbk", errors="replace")
    return [ln.strip() for ln in done.stdout.splitlines() if "Mandatory" in ln or "强制" in ln]


def fs_of(path: str) -> str:
    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    name = ctypes.create_unicode_buffer(64)
    fsname = ctypes.create_unicode_buffer(64)
    serial = wintypes.DWORD(); maxlen = wintypes.DWORD(); flags = wintypes.DWORD()
    k32.GetVolumeInformationW(root, name, 64, ctypes.byref(serial), ctypes.byref(maxlen), ctypes.byref(flags), fsname, 64)
    return fsname.value or "?"


CHILD = r"""
import os, sys
def touch(path):
    try:
        with open(os.path.join(path, "probe.txt"), "w") as fh:
            fh.write("x")
        return "WRITE-OK"
    except OSError as exc:
        return "WRITE-DENIED " + type(exc).__name__
sys.stdout.write("READY\n"); sys.stdout.flush()
sys.stdin.readline()
print("scratch=" + touch(sys.argv[1]))
print("user=" + touch(sys.argv[2]))
print("qio=" + touch(sys.argv[3]))
print("DONE")
"""


def run_child(interpreter: str, targets: dict, *, downgrade: bool) -> dict:
    proc = subprocess.Popen(
        [interpreter, "-c", CHILD, targets["scratch"], targets["user"], targets["qio"]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    ready = proc.stdout.readline().strip()
    before = token_integrity_of_process(proc.pid)
    note = "未降级"
    if downgrade:
        note = downgrade_to_low(proc.pid)
    after = token_integrity_of_process(proc.pid)
    proc.stdin.write("go\n"); proc.stdin.flush()
    out, err = proc.communicate(timeout=60)
    return {
        "ready": ready, "before": before, "downgrade": note, "after": after,
        "stdout": out.strip(), "stderr": err.strip()[:200],
    }


def main() -> None:
    interpreter = sys.executable
    print("================ C1 Low Integrity scratch 最小实验 ================")
    print("Windows:", sys.getwindowsversion())
    print("父进程解释器:", interpreter)
    print("父进程完整性:", current_integrity())
    base = tempfile.mkdtemp(prefix="lip-base-")
    print("基准目录:", base, "| 文件系统:", fs_of(base))
    print("基准目录标签（读回）:", read_label(base))
    print("基准目录 icacls 标签行:", icacls_lines(base))

    targets = {}
    for key in ("scratch", "user", "qio"):
        path = os.path.join(base, key)
        os.makedirs(path, exist_ok=True)
        targets[key] = path

    for name, mode in (("SDDL", "sddl"), ("icacls", "icacls")):
        print()
        print(f"---------------- 标签方式：{name} ----------------")
        path = targets["scratch"]
        before = read_label(path)
        applied = label_via_sddl(path) if mode == "sddl" else label_via_icacls(path)
        after = read_label(path)
        print(f"scratch 标签 设置前={before!r} 调用={applied} 设置后={after!r}")
        print("scratch icacls 标签行:", icacls_lines(path))
        result = run_child(interpreter, targets, downgrade=True)
        print("child READY:", result["ready"], "| 降级前 IL:", result["before"],
              "| 降级:", result["downgrade"], "| 降级后 IL:", result["after"])
        print(result["stdout"])
        if result["stderr"]:
            print("child stderr:", result["stderr"])
        for key in ("scratch", "user", "qio"):
            probe = os.path.join(targets[key], "probe.txt")
            if os.path.exists(probe):
                os.remove(probe)

    print()
    print("---------------- 对照：不降级 ----------------")
    result = run_child(interpreter, targets, downgrade=False)
    print("child READY:", result["ready"], "| IL:", result["before"], "->", result["after"])
    print(result["stdout"])
    for key in ("scratch", "user", "qio"):
        probe = os.path.join(targets[key], "probe.txt")
        if os.path.exists(probe):
            os.remove(probe)
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
