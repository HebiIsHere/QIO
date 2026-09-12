"""启动后端 + 前端 dev server（e2e 验证用），pid 写入 scripts/.e2e-pids。

两点必须注意（否则会「报成功但后端根本没起来」）：
1. 后端必须用项目虚拟环境的解释器：依赖（keyring 等）只装在 backend/.venv，
   系统 Python 会在 import 阶段就退出。
2. 日志写文件而不是 DEVNULL：启动失败时能立刻看到原因（例如缺依赖、端口占用）。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = r"C:\Program Files\nodejs\node.exe"
LOG_DIR = Path(os.environ.get("TEMP", ".")) / "qio-e2e-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def backend_python() -> list[str]:
    """优先用 backend/.venv；缺失时退回 `uv run --frozen`（会自动用项目环境）。"""
    win = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    posix = ROOT / "backend" / ".venv" / "bin" / "python"
    if win.exists():
        return [str(win)]
    if posix.exists():
        return [str(posix)]
    return ["uv", "run", "--frozen", "python"]


def wait_http(url: str, timeout_s: float) -> bool:
    """轮询就绪（stdlib，不引依赖）。"""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


PY = backend_python()

backend_env = dict(os.environ)
backend_env["PYTHONPATH"] = str(ROOT / "backend" / "src")
backend_env["QIO_PORT"] = "8734"
backend_env["QIO_DATA_DIR"] = str(Path(os.environ.get("TEMP", ".")) / "qio-e2e")

backend_log = open(LOG_DIR / "backend.log", "w", encoding="utf-8")
vite_log = open(LOG_DIR / "vite.log", "w", encoding="utf-8")

backend = subprocess.Popen(
    [*PY, "-m", "uvicorn", "agent.main:create_app", "--factory",
     "--host", "127.0.0.1", "--port", "8734", "--log-level", "warning"],
    cwd=str(ROOT / "backend"), env=backend_env,
    stdout=backend_log, stderr=subprocess.STDOUT,
)
vite = subprocess.Popen(
    [NODE, "node_modules/vite/bin/vite.js", "--port", "5199",
     "--strictPort", "--host", "127.0.0.1"],
    cwd=str(ROOT / "frontend"), env=os.environ,
    stdout=vite_log, stderr=subprocess.STDOUT,
)
Path(__file__).parent.joinpath(".e2e-pids").write_text(
    json.dumps({"backend": backend.pid, "vite": vite.pid}), encoding="utf-8"
)
print(f"backend={backend.pid} vite={vite.pid} (python={' '.join(PY)})")

backend_ok = wait_http("http://127.0.0.1:8734/api/session/context", 25)
vite_ok = wait_http("http://127.0.0.1:5199/", 25)
print(f"backend_ready={backend_ok} vite_ready={vite_ok}")
if not backend_ok:
    print(f"后端未就绪，日志见 {LOG_DIR / 'backend.log'}（最后 20 行）：")
    lines = (LOG_DIR / "backend.log").read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-20:]))
if not vite_ok:
    print(f"前端未就绪，日志见 {LOG_DIR / 'vite.log'}")
sys.exit(0 if (backend_ok and vite_ok) else 1)
