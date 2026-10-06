"""工具真正的执行进程是谁：venv 的 python.exe 是不是再 spawn 一个真解释器。

为什么重要：sandbox 用 sys.executable 启动 worker，harden() 只对**那一个** pid 下手。
如果 .venv\Scripts\python.exe 是 uv 的 trampoline（先起一个真解释器再转发参数），
降级与 job 指派就落在了转发进程上，真正跑工具代码的那个进程既不在 job 里、也不是 Low。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

wt = sys.argv[1] if len(sys.argv) > 1 else r"D:\qio-dev\qio-p3-c"
VENV_PY = wt + r"\backend\.venv\Scripts\python.exe"

adv = ctypes.WinDLL("advapi32")
k32 = ctypes.WinDLL("kernel32")
adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]


def il_of(pid: int) -> str:
    handle = k32.OpenProcess(0x0400, False, pid)
    if not handle:
        return f"OpenProcess err={ctypes.get_last_error()}"
    token = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
            return f"OpenProcessToken err={ctypes.get_last_error()}"
        buf = ctypes.create_string_buffer(64)
        size = wintypes.DWORD(0)
        adv.GetTokenInformation(token, 25, buf, 64, ctypes.byref(size))
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
        text = ctypes.c_wchar_p()
        adv.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text))
        return text.value
    finally:
        k32.CloseHandle(handle)
        if token:
            k32.CloseHandle(token)


def tree(root_pid: int) -> list[dict]:
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=60,
    )
    rows = json.loads(ps.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    by_parent: dict[int, list[dict]] = {}
    for row in rows:
        by_parent.setdefault(int(row["ParentProcessId"]), []).append(row)
    out: list[dict] = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        for child in by_parent.get(pid, []):
            out.append(child)
            stack.append(int(child["ProcessId"]))
    return out


def main() -> None:
    print("父进程完整性:", il_of(os.getpid()), "| 父进程:", sys.executable)
    proc = subprocess.Popen([VENV_PY, "-c", "import time; time.sleep(25)"])
    print("sandbox 会 spawn 的进程:", VENV_PY, "-> pid", proc.pid, "IL:", il_of(proc.pid))
    time.sleep(2.0)
    kids = tree(proc.pid)
    print("它的后代进程数:", len(kids))
    for kid in kids:
        print("  - pid", kid["ProcessId"], kid["Name"], "IL:", il_of(int(kid["ProcessId"])),
              "| cmd:", (kid.get("CommandLine") or "")[:90])
    proc.kill()
    proc.wait(timeout=10)


if __name__ == "__main__":
    main()
