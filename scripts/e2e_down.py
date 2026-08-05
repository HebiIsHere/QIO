"""停止 e2e 服务进程。"""
import json
import subprocess
import sys
from pathlib import Path

pidfile = Path(__file__).parent / ".e2e-pids"
if pidfile.exists():
    pids = json.loads(pidfile.read_text(encoding="utf-8"))
    for name, pid in pids.items():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
            print(f"killed {name} ({pid})")
        except Exception as exc:
            print(f"{name}: {exc}")
    pidfile.unlink(missing_ok=True)