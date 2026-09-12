"""停止 e2e 服务进程（按 pidfile + 端口兜底，避免「报成功但端口仍被占用」）。"""
import json
import subprocess
from pathlib import Path

PORTS = {"backend": 8734, "vite": 5199}


def listeners(port: int) -> set[int]:
    """返回监听该端口的 PID 集合（Windows）。"""
    ps = (
        f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
        "-ErrorAction SilentlyContinue).OwningProcess"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return set()
    return {int(x) for x in out.split() if x.strip().isdigit()}


pidfile = Path(__file__).parent / ".e2e-pids"
recorded: dict[str, int] = {}
if pidfile.exists():
    recorded = json.loads(pidfile.read_text(encoding="utf-8"))

targets: dict[str, set[int]] = {name: set() for name in PORTS}
for name, pid in recorded.items():
    if name in targets:
        targets[name].add(int(pid))
# pidfile 记的是启动器/包装进程；真正监听端口的多是它的子进程，按端口一并清理
for name, port in PORTS.items():
    targets[name] |= listeners(port)

for name, pids in targets.items():
    for pid in sorted(pids):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
            print(f"killed {name} ({pid})")
        except Exception as exc:
            print(f"{name} ({pid}): {exc}")

for name, port in PORTS.items():
    remaining = listeners(port)
    if remaining:
        print(f"WARNING {name} 仍在监听 :{port}（pid={sorted(remaining)}）")

pidfile.unlink(missing_ok=True)
