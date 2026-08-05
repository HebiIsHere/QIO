"""启动后端 + 前端 dev server（e2e 验证用），pid 写入 scripts/.e2e-pids。"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
NODE = r"C:\Program Files\nodejs\node.exe"

backend_env = dict(os.environ)
backend_env["PYTHONPATH"] = str(ROOT / "backend" / "src")
backend_env["QIO_PORT"] = "8734"
backend_env["QIO_DATA_DIR"] = str(Path(os.environ.get("TEMP", ".")) / "qio-e2e")

backend = subprocess.Popen(
    [PY, "-m", "uvicorn", "agent.main:create_app", "--factory",
     "--host", "127.0.0.1", "--port", "8734", "--log-level", "warning"],
    cwd=str(ROOT / "backend"), env=backend_env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
vite = subprocess.Popen(
    [NODE, "node_modules/vite/bin/vite.js", "--port", "5199",
     "--strictPort", "--host", "127.0.0.1"],
    cwd=str(ROOT / "frontend"), env=os.environ,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
Path(__file__).parent.joinpath(".e2e-pids").write_text(
    json.dumps({"backend": backend.pid, "vite": vite.pid}), encoding="utf-8"
)
print(f"backend={backend.pid} vite={vite.pid}")