"""R9 §12：退出只结束**本实例**的进程树，其他实例不受影响（W5 真机取证）。

契约要求（docs/r9-contracts.md §11 / §12）：

* 结束进程树只能按 **pid**（`taskkill /PID <pid> /T /F`），**绝不按进程名批量结束**；
* 「结束了」必须是核验过的结论：结束前后都拿 pid 名单，逐个确认原进程真的消失；
* 两个独立实例并存时，退出其中一个不能误伤另一个（含它同名映像的子进程）。

本脚本用两棵**独立进程树**模拟两个实例（`cmd` → 真正干活的子进程，正好对应 onefile
的 launcher → child 两层结构），在本机真实执行：

1. 同时建两棵树，按进程快照展开全部后代，确认 pid 集合不相交；
2. 按 pid + /T 结束树 A，逐 pid 核验 A 整棵消失；
3. 核验树 B 的根与全部后代仍持有**原来的进程身份**（创建时间 + 映像路径）—— 证明没有
   按映像名批量杀（两棵树里都有同名的 ping.exe）；
4. 再造一棵树 C，只结束根、故意留下后代，再按**事前记录的 pid 名单**清理并核验
   （对应壳侧「root 先退出，孤儿后代按记录名单再清一遍」的兜底路径）；
5. 对不存在的 pid 探测必须返回「不在」（不误报还活着），最后清理全部测试进程。

证据写入 `_r9-evidence/w5-process-tree.txt`；任何一条 FAIL 都以退出码 1 结束。
脚本只操作自己启动的 pid，从不按映像名找进程 —— 与产品实现同一纪律。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "_r9-evidence" / "w5-process-tree.txt"

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 结束动作之后的核验预算（与壳侧 STOP_VERIFY_BUDGET=5s 同量级；本机测试给足 15s）。
VERIFY_TIMEOUT = 15.0
POLL_INTERVAL = 0.15

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_records: list[tuple[str, str, str]] = []
_failures: list[str] = []


def record(rid: str, title: str, ok: bool, detail: str) -> None:
    """写一条可核对的结论（同时进证据文件与 stdout）。"""
    state = "PASS" if ok else "FAIL"
    line = "[%s] %s %s — %s" % (state, rid, title, detail)
    print(line)
    _records.append((state, rid, title, detail))
    if not ok:
        _failures.append(rid)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 进程快照 / 身份

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WINDOWS else None

if IS_WINDOWS:
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def process_table() -> list[tuple[int, int, str]]:
    """(pid, ppid, 映像名) 全量快照。"""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid:
        raise OSError("CreateToolhelp32Snapshot 失败: %s" % ctypes.get_last_error())
    rows: list[tuple[int, int, str]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
    while ok:
        rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile)))
        ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    _kernel32.CloseHandle(snapshot)
    return rows


def descendants(root_pid: int, table: list[tuple[int, int, str]] | None = None) -> list[int]:
    """按父 pid 关系 BFS 展开全部后代（含孙代）。"""
    rows = table if table is not None else process_table()
    out: list[int] = []
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, ppid, _name in rows:
            if ppid == parent and pid != root_pid and pid not in out:
                out.append(pid)
                frontier.append(pid)
    return out


def process_identity(pid: int) -> dict | None:
    """进程身份：创建时间 + 映像路径。打不开（已退出/无权限）→ None。"""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        created, exited, kernel, user = (
            wintypes.FILETIME(),
            wintypes.FILETIME(),
            wintypes.FILETIME(),
            wintypes.FILETIME(),
        )
        if not _kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        path = ""
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            path = buffer.value
        created_ft = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return {"pid": int(pid), "created": created_ft, "exe": path}
    finally:
        _kernel32.CloseHandle(handle)


def identity_alive(identity: dict | None) -> bool:
    """原进程是否还活着（创建时间必须一致：pid 被复用时不能算「还活着」）。"""
    if not identity:
        return False
    current = process_identity(int(identity["pid"]))
    if current is None:
        return False
    return current["created"] == identity["created"]


def capture_tree(root_pid: int) -> list[dict]:
    """root + 全部后代的身份快照（名单是事后核验的唯一依据）。"""
    out: list[dict] = []
    for pid in [root_pid] + descendants(root_pid):
        identity = process_identity(pid)
        if identity is not None:
            out.append(identity)
    return out


def taskkill_pid(pid: int, *, tree: bool) -> tuple[bool, str]:
    """只按 pid 结束（tree=True 时 /T 带走整棵树）。返回 (命令成功, 输出尾部)。"""
    cmd = ["taskkill"]
    if tree:
        cmd.append("/T")
    cmd.extend(["/PID", str(int(pid)), "/F"])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW
        )
        text = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, text[-300:]
    except Exception as exc:  # noqa: BLE001 - 起不动 taskkill 不等于没杀掉，以核验为准
        return False, "taskkill 调用失败：%s" % exc


def wait_identities_gone(identities: list[dict], timeout: float = VERIFY_TIMEOUT) -> tuple[bool, list[dict]]:
    """等名单里每一条进程身份都消失；返回 (是否全部消失, 幸存名单)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        survivors = [item for item in identities if identity_alive(item)]
        if not survivors:
            return True, []
        time.sleep(POLL_INTERVAL)
    return False, [item for item in identities if identity_alive(item)]


def describe(identities: list[dict], limit: int = 8) -> str:
    def label(item: dict) -> str:
        name = Path(item.get("exe") or "").name or "?"
        return "%s(pid=%s)" % (name, item.get("pid"))

    shown = "、".join(label(item) for item in identities[:limit])
    if len(identities) > limit:
        shown += " 等共 %d 个" % len(identities)
    return shown or "（空）"


# ---------------------------------------------------------------- 测试进程树

def spawn_tree(label: str) -> subprocess.Popen:
    """起一棵两层进程树：cmd → ping（对应 onefile 的 launcher → child）。

    环境变量只作为血统标记（不参与任何判定）：判定永远只看 pid + 创建时间。
    """
    env = dict(os.environ)
    env["QIO_R9_W5_TREE"] = label
    return subprocess.Popen(
        ["cmd", "/c", "ping -n 120 127.0.0.1"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


def wait_tree_ready(proc: subprocess.Popen, label: str) -> list[dict]:
    """等**真正干活的子进程**（ping.exe）出现，再拿完整身份快照。

    为什么不能「看到任意后代就收工」：cmd 会先起 conhost.exe，此刻 ping 还没建出来 ——
    那样拿到的名单漏掉真正要验证的子进程，后面的「整棵树消失」就变成了假结论
    （首版实测就漏过一次，taskkill 输出里多出来的那个 pid 正是没进名单的 ping）。
    """
    deadline = time.time() + 20.0
    last: list[dict] = []
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        last = capture_tree(proc.pid)
        names = {Path(item.get("exe") or "").name.lower() for item in last[1:]}
        if "ping.exe" in names:  # root + 至少一个真正的后代进程
            return last
        time.sleep(0.15)
    return last


def cleanup(procs: list[subprocess.Popen]) -> None:
    """收尾：只按 pid 结束本次脚本启动的树（绝不影响别的进程）。"""
    for proc in procs:
        if proc.poll() is None:
            taskkill_pid(proc.pid, tree=True)
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    if not IS_WINDOWS:
        print("本脚本只在 Windows 上取真机证据（契约 §12 要求）")
        return 2
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    record(
        "W5-PT-000",
        "环境",
        True,
        "Windows；脚本 pid=%s；开始时间=%s；证据=%s" % (os.getpid(), now_text(), EVIDENCE),
    )

    procs: list[subprocess.Popen] = []
    try:
        # ---- 场景 1：两个独立实例并存，只结束实例 A ----
        proc_a = spawn_tree("A")
        proc_b = spawn_tree("B")
        procs.extend([proc_a, proc_b])
        tree_a = wait_tree_ready(proc_a, "A")
        tree_b = wait_tree_ready(proc_b, "B")
        ids_a = {item["pid"] for item in tree_a}
        ids_b = {item["pid"] for item in tree_b}
        record(
            "W5-PT-001",
            "两个独立实例的进程树都已建立（各有后代，pid 集合不相交）",
            len(tree_a) >= 2 and len(tree_b) >= 2 and not (ids_a & ids_b),
            "实例A：%s；实例B：%s；交集=%s"
            % (describe(tree_a), describe(tree_b), sorted(ids_a & ids_b) or "无"),
        )
        before_a, before_b = list(tree_a), list(tree_b)

        # 结束实例 A：完全复刻壳侧「按 pid 结束整棵树」的动作。
        ok, out = taskkill_pid(proc_a.pid, tree=True)
        record(
            "W5-PT-002",
            "按 pid 结束实例 A 的整棵树（taskkill /PID <pid> /T /F）",
            True,
            "pid=%s taskkill 返回=%s；输出=%s" % (proc_a.pid, ok, out or "（空）"),
        )
        gone, survivors = wait_identities_gone(before_a)
        record(
            "W5-PT-003",
            "实例 A 整棵树确认消失（逐 pid 核验，含后代）",
            gone,
            "共核验 %d 个 pid；幸存=%s" % (len(before_a), describe(survivors) if survivors else "无"),
        )
        b_intact = [item for item in before_b if not identity_alive(item)]
        record(
            "W5-PT-004",
            "实例 B 完全不受影响（根与全部后代仍持有原进程身份）",
            not b_intact,
            "实例B丢失/变化的进程=%s；其余 %d 个 pid 身份未变"
            % (describe(b_intact) if b_intact else "无", len(before_b)),
        )
        # 不按映像名批量的直接证据：B 树里与 A 同名的 ping.exe 还活着（上面的身份核验已经证明），
        # 这里把它写成一条独立结论，避免「只看了根进程」的误读。
        b_children = [item for item in before_b if item["pid"] != proc_b.pid]
        record(
            "W5-PT-005",
            "没有按进程名批量结束：实例 B 的同名子进程（%s）仍存活"
            % (", ".join(sorted({Path(item["exe"]).name for item in b_children})) or "无"),
            bool(b_children) and all(identity_alive(item) for item in b_children),
            "实例B子进程=%s" % describe(b_children),
        )

        # ---- 场景 2：root 先退出、后代残留 → 按事前记录的 pid 名单清理 ----
        proc_c = spawn_tree("C")
        procs.append(proc_c)
        tree_c = wait_tree_ready(proc_c, "C")
        # 先只要根：/T 会把后代一起带走，这里故意只杀根，制造 orphan 场景。
        taskkill_pid(proc_c.pid, tree=False)
        try:
            proc_c.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        leftover_children = [item for item in tree_c[1:] if identity_alive(item)]
        record(
            "W5-PT-006",
            "构造「根先退出、后代残留」场景（模拟 onefile launcher 先走）",
            bool(leftover_children),
            "已单独结束根 pid=%s；残留后代=%s" % (proc_c.pid, describe(leftover_children)),
        )
        cleaned: list[dict] = []
        for item in leftover_children:
            taskkill_pid(int(item["pid"]), tree=True)
            cleaned.append(item)
        gone_c, survivors_c = wait_identities_gone(leftover_children)
        record(
            "W5-PT-007",
            "按事前记录的 pid 名单清理孤儿后代并核验消失（不按进程名）",
            gone_c,
            "名单=%s；幸存=%s" % (describe(cleaned), describe(survivors_c) if survivors_c else "无"),
        )

        # ---- 场景 3：探测一个不存在的 pid，不能误报「还活着」 ----
        bogus_pid = 0xFFFF_FFF0
        record(
            "W5-PT-008",
            "不存在的 pid 探测为「不在」（核验逻辑不会误报还活着）",
            process_identity(bogus_pid) is None,
            "pid=%s identity=%s" % (bogus_pid, process_identity(bogus_pid)),
        )

        # 收尾：B 树也要按 pid 结束（只影响本轮脚本自己的进程）。
        taskkill_pid(proc_b.pid, tree=True)
        b_gone, b_left = wait_identities_gone(before_b)
        record(
            "W5-PT-009",
            "脚本只结束自己启动的进程：收尾后两棵树都不残留",
            b_gone,
            "实例B幸存=%s" % (describe(b_left) if b_left else "无"),
        )
    finally:
        cleanup(procs)

    evidence_lines = [
        "# R9 W5 进程树真机取证（scripts/verify_r9_process_tree.py）",
        "# 主机时间：%s" % now_text(),
        "# 判定：只按 pid 结束本实例的进程树；其他实例与同名进程不受影响；结束后逐 pid 核验。",
        "",
    ]
    evidence_lines += ["[%s] %s %s — %s" % row for row in _records]
    evidence_lines.append("")
    evidence_lines.append("结论：%s（%d 条，FAIL=%d）" % ("PASS" if not _failures else "FAIL", len(_records), len(_failures)))
    EVIDENCE.write_text("\n".join(evidence_lines) + "\n", encoding="utf-8")
    print("")
    print("证据已写入：%s" % EVIDENCE)
    if _failures:
        print("FAIL 项：%s" % "、".join(_failures))
        return 1
    print("全部通过：%d 条" % len(_records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
