"""停止 e2e 服务进程（按 pidfile + 真实端口探测，避免「报成功但端口仍被占用」）。

三点实测教训：
1. `taskkill` 在受限环境下会失败，而且旧的实现把输出丢掉了 → 看起来「已停止」，
   其实进程还在，下一次启动的后端 bind 失败，而探测请求被旧进程应答。
2. `Get-NetTCPConnection` 同一环境下会安静地返回空列表 → 不能作为「端口空闲」的依据。
   一律用真实 TCP 连接判断。
3. **A05**：旧实现用 `if not port_open(backend) or not port_open(5199): return "ok"`
   —— 任一端口关闭就报成功，而且报在真实确认之前。现在改成「对该进程负责的那个端口
   做**有界轮询确认**」，只有真实探测确认不再响应才报成功；确认不了就如实说未确认停止。
   （更新流程依赖这个结论决定要不要安装，见 `frontend/src/services/updater.ts`。）
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ports import port_open  # noqa: E402 - 脚本内部的本地工具模块

PORTS = {"backend": 8734, "vite": 5199}

# 有界确认预算：不永久等待，也不在真实确认之前报成功。
CONFIRM_BUDGET_SECONDS = 5.0
CONFIRM_STEP_SECONDS = 0.2


def confirm_stopped(port: int, *, budget: float = CONFIRM_BUDGET_SECONDS) -> tuple[bool, str]:
    """在 budget 秒内轮询真实 TCP 连接，确认端口不再响应。

    返回 `(是否确认停止, 可读原因)`。超时即返回未确认——绝不假装已停止。
    """
    deadline = time.monotonic() + budget
    while True:
        if not port_open(port):
            return True, f"端口 {port} 已确认不再响应（真实探测，预算 {budget:.1f}s）"
        if time.monotonic() >= deadline:
            return False, f"端口 {port} 在 {budget:.1f}s 内仍可连接：未确认停止"
        time.sleep(CONFIRM_STEP_SECONDS)


def _kill_with_taskkill(pid: int) -> str:
    try:
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return f"taskkill 失败: {exc}"
    detail = (proc.stdout or proc.stderr or "").strip().splitlines()
    return detail[0] if detail else f"exit={proc.returncode}"


def stop_pid(pid: int, port: int) -> tuple[bool, str]:
    """先 Stop-Process，再用**真实探测**确认；确认不了再补一次 taskkill 后重试。

    返回 `(是否确认停止, 可读结果)`，供调用方决定退出码与提示。
    """
    ps = f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - 尽力而为
        return False, f"powershell 失败: {exc}"

    confirmed, detail = confirm_stopped(port)
    if confirmed:
        return True, detail

    kill_detail = _kill_with_taskkill(pid)
    confirmed, detail = confirm_stopped(port)
    if confirmed:
        return True, f"{detail}（taskkill: {kill_detail}）"
    return False, f"{detail}；taskkill: {kill_detail}"


pidfile = Path(__file__).parent / ".e2e-pids"
recorded: dict[str, int] = {}
if pidfile.exists():
    recorded = json.loads(pidfile.read_text(encoding="utf-8"))

targets: dict[str, set[int]] = {name: set() for name in PORTS}
for name, pid in recorded.items():
    if name in targets:
        targets[name].add(int(pid))

unconfirmed: list[str] = []
# pidfile 记的是启动器/包装进程；真正监听端口的多是它的子进程，按端口一并清理
for name, pids in targets.items():
    for pid in sorted(pids):
        ok, detail = stop_pid(pid, PORTS[name])
        print(f"stop {name} ({pid}): {'ok' if ok else '未确认'} — {detail}")
        if not ok:
            unconfirmed.append(f"{name}({pid})")

pidfile.unlink(missing_ok=True)

busy = [f"{name}:{port}" for name, port in PORTS.items() if port_open(port)]
if busy:
    print(f"仍然有服务在监听：{', '.join(busy)}（可能是别的进程占用了端口）")
    sys.exit(1)
if unconfirmed:
    # 端口最终是空的，但过程里有进程没被真实确认过：如实说明，不当成完全成功。
    print(f"端口都已空闲，但以下进程未被真实探测确认：{', '.join(unconfirmed)}")
print("e2e 服务已停止，两个端口都不再响应（真实探测确认）。")
