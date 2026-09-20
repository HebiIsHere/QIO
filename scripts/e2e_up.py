"""启动后端 + 前端 dev server（e2e 验证用），pid 写入 scripts/.e2e-pids。

两点必须注意（否则会「报成功但后端根本没起来」）：
1. 后端必须用项目虚拟环境的解释器：依赖（keyring 等）只装在 backend/.venv，
   系统 Python 会在 import 阶段就退出。
2. 日志写文件而不是 DEVNULL：启动失败时能立刻看到原因（例如缺依赖、端口占用）。

安全模式（本机 API 身份认证）：

* 默认：`QIO_DEV_INSECURE=1` + `QIO_ENABLE_TEST_EVENTS=1`，与既有 QA 脚本
  （`scripts/baseline/*`、`scripts/e2e-checklist/*`）兼容 —— 那些脚本不带令牌。
* `--secure`：额外生成 256-bit 会话令牌，同时注入后端与 Vite
  （`QIO_SESSION_TOKEN` / `VITE_QIO_SESSION_TOKEN`），用于验证
  「没有令牌就调不动本机 API」这条边界。令牌只写进文件（打印的是路径，不是令牌）。
"""
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ports import port_open  # noqa: E402 - 脚本内部的本地工具模块

ROOT = Path(__file__).resolve().parents[1]
NODE = r"C:\Program Files\nodejs\node.exe"
LOG_DIR = Path(os.environ.get("TEMP", ".")) / "qio-e2e-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def backend_python() -> list[str]:
    """选一个**真的能 import 依赖**的解释器。

    只看文件存在是不够的：机器上可能留着一个坏掉的 venv（解释器 launcher 起不来、
    或 site-packages 被 ACL 挡住），那会表现为「脚本报成功但后端没起来」。
    """
    candidates: list[list[str]] = []
    override = os.environ.get("QIO_PYTHON")
    if override:
        candidates.append([override])
    win = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    posix = ROOT / "backend" / ".venv" / "bin" / "python"
    if win.exists():
        candidates.append([str(win)])
    elif posix.exists():
        candidates.append([str(posix)])
    candidates.append(["uv", "run", "--frozen", "python"])
    candidates.append([sys.executable])
    for cmd in candidates:
        try:
            probe = subprocess.run(
                [*cmd, "-c", "import fastapi, uvicorn, keyring"],
                capture_output=True, timeout=90,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return cmd
    raise SystemExit(
        "找不到可用的后端解释器（试过 QIO_PYTHON / backend/.venv / uv / 当前解释器）。"
    )


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


def wait_http_authed(url: str, timeout_s: float, env: dict) -> bool:
    """同上，但带上会话令牌（secure 模式下 /api/health 之外的入口都要求认证）。"""
    import urllib.error
    import urllib.request

    token = env.get("QIO_SESSION_TOKEN", "")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        request = urllib.request.Request(url)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=2) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


PY = backend_python()
SECURE = "--secure" in sys.argv[1:]

backend_env = dict(os.environ)
backend_env["PYTHONPATH"] = str(ROOT / "backend" / "src")
backend_env["QIO_PORT"] = "8734"
# 数据目录可被外部覆盖（验收想跑一个干净的数据域时用），默认仍是 qio-e2e。
backend_env["QIO_DATA_DIR"] = os.environ.get("QIO_DATA_DIR") or str(
    Path(os.environ.get("TEMP", ".")) / "qio-e2e"
)
# 开发豁免 + 测试事件注入口（生产构建里这条路由根本不存在）
backend_env["QIO_DEV_INSECURE"] = "1"
backend_env["QIO_ENABLE_TEST_EVENTS"] = "1"

vite_env = dict(os.environ)
token_file = Path(os.environ.get("TEMP", ".")) / "qio-e2e-session-token.txt"
if SECURE:
    token = secrets.token_urlsafe(32)
    token_file.write_text(token, encoding="utf-8")
    backend_env["QIO_SESSION_TOKEN"] = token
    vite_env["VITE_QIO_SESSION_TOKEN"] = token
    vite_env["VITE_QIO_BACKEND_URL"] = "http://127.0.0.1:8734"
    print(f"secure mode: session token written to {token_file}")

backend_log = open(LOG_DIR / "backend.log", "w", encoding="utf-8")
vite_log = open(LOG_DIR / "vite.log", "w", encoding="utf-8")

# 端口被占用时不要「启动—等就绪—看起来成功」：旧进程可能正拿着这个端口
# 回你的探测（实测过：新后端 bind 失败，但 /api/health 仍然是旧进程答的）。
for port in (8734, 5199):
    if port_open(port):
        print(f"端口 {port} 已被占用：先运行 python scripts/e2e_down.py 再启动。")
        sys.exit(2)

backend = subprocess.Popen(
    [*PY, "-m", "uvicorn", "agent.main:create_app", "--factory",
     "--host", "127.0.0.1", "--port", "8734", "--log-level", "warning"],
    cwd=str(ROOT / "backend"), env=backend_env,
    stdout=backend_log, stderr=subprocess.STDOUT,
)
vite = subprocess.Popen(
    [NODE, "node_modules/vite/bin/vite.js", "--port", "5199",
     "--strictPort", "--host", "127.0.0.1"],
    cwd=str(ROOT / "frontend"), env=vite_env,
    stdout=vite_log, stderr=subprocess.STDOUT,
)
Path(__file__).parent.joinpath(".e2e-pids").write_text(
    json.dumps({"backend": backend.pid, "vite": vite.pid}), encoding="utf-8"
)
print(f"backend={backend.pid} vite={vite.pid} (python={' '.join(PY)})")

backend_ok = wait_http_authed("http://127.0.0.1:8734/api/health", 25, backend_env)
vite_ok = wait_http("http://127.0.0.1:5199/", 25)
# 子进程活着才算真的起来了（否则上面探测到的可能是别的进程）
backend_alive = backend.poll() is None
vite_alive = vite.poll() is None
print(
    f"backend_ready={backend_ok} vite_ready={vite_ok} "
    f"backend_alive={backend_alive} vite_alive={vite_alive}"
)
if not backend_ok or not backend_alive:
    print(f"后端未就绪，日志见 {LOG_DIR / 'backend.log'}（最后 20 行）：")
    lines = (LOG_DIR / "backend.log").read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-20:]))
if not vite_ok or not vite_alive:
    print(f"前端未就绪，日志见 {LOG_DIR / 'vite.log'}")
sys.exit(0 if (backend_ok and vite_ok and backend_alive and vite_alive) else 1)
