"""停止 e2e 服务进程（按 pidfile + 真实端口探测，避免「报成功但端口仍被占用」）。

两点实测教训：
1. `taskkill` 在受限环境下会失败，而且旧的实现把输出丢掉了 → 看起来「已停止」，
   其实进程还在，下一次启动的后端 bind 失败，而探测请求被旧进程应答。
2. `Get-NetTCPConnection` 同一环境下会安静地返回空列表 → 不能作为「端口空闲」的依据。
   一律用真实 TCP 连接判断。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ports import port_open  # noqa: E402 - 脚本内部的本地工具模块

PORTS = {"backend": 8734, "vite": 5199}


def stop_pid(pid: int) -> str:
    """先 Stop-Process，失败再退到 taskkill；返回可读结果。"""
    ps = f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 - 尽力而为
        return f"powershell 失败: {exc}"
    if not port_open(PORTS.get("backend", 8734)) or not port_open(5199):
        return "ok"
    try:
        proc = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                              capture_output=True, text=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return f"taskkill 失败: {exc}"
    detail = (proc.stdout or proc.stderr or "").strip().splitlines()
    return detail[0] if detail else f"exit={proc.returncode}"


pidfile = Path(__file__).parent / ".e2e-pids"
recorded: dict[str, int] = {}
if pidfile.exists():
    recorded = json.loads(pidfile.read_text(encoding="utf-8"))

targets: dict[str, set[int]] = {name: set() for name in PORTS}
for name, pid in recorded.items():
    if name in targets:
        targets[name].add(int(pid))
# pidfile 记的是启动器/包装进程；真正监听端口的多是它的子进程，按端口一并清理
for name, pids in targets.items():
    for pid in sorted(pids):
        print(f"stop {name} ({pid}): {stop_pid(pid)}")

pidfile.unlink(missing_ok=True)

busy = [f"{name}:{port}" for name, port in PORTS.items() if port_open(port)]
if busy:
    print(f"仍然有服务在监听：{', '.join(busy)}（可能是别的进程占用了端口）")
    sys.exit(1)
print("e2e 服务已停止，两个端口都不再响应。")
