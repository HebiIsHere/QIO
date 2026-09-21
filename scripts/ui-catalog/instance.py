"""UI 目录采集用的**隔离实例**启动器（后端 + 前端 + 独立数据目录）。

为什么需要：后端的 SSE 事件总线是**全局广播**（见 backend/src/agent/api/bus.py）——
`POST /api/events/test` 注入的事件会送给所有订阅者。多个采集脚本并行跑在同一个实例上
时，别人的事件会出现在你的页面里，截出来的图就不是你要的状态。

所以每个并行采集任务各起一套：自己的后端端口、自己的数据目录、自己的前端 dev server。

用法：
    # 起一套隔离实例（默认 backend 8834 / frontend 6199 / 数据目录 %TEMP%\\qio-ui-catalog\\<name>）
    python scripts/ui-catalog/instance.py up --name settings --seed
    # 关掉它
    python scripts/ui-catalog/instance.py down --name settings

采集脚本通过环境变量连到这套实例（scripts/ui-catalog/lib.mjs 读同一批变量）：
    QIO_BASE=http://127.0.0.1:<frontend_port>
    QIO_API=http://127.0.0.1:<backend_port>
`up` 会把这两个变量连同端口一起写进 `scripts/ui-catalog/.instance-<name>.json`，
脚本里用 `node scripts/ui-catalog/env.mjs <name> -- <命令>` 包装即可（见 README）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # qio/
sys.path.insert(0, str(ROOT / "scripts"))
from _ports import port_open  # noqa: E402 - 与仓库既有脚本共用同一个端口工具

NODE = r"C:\Program Files\nodejs\node.exe"
STATE_DIR = Path(__file__).resolve().parent
CATALOG_TMP_NAME = "qio-ui-catalog"


def state_file(name: str) -> Path:
    return STATE_DIR / f".instance-{name}.json"


def default_data_dir(name: str) -> Path:
    return Path(os.environ.get("TEMP", ".")) / CATALOG_TMP_NAME / name


def default_log_dir(name: str) -> Path:
    return Path(os.environ.get("TEMP", ".")) / f"{CATALOG_TMP_NAME}-logs" / name


def backend_python() -> list[str]:
    """挑一个真的能 import 依赖的解释器（与 scripts/e2e_up.py 同一套判断）。"""
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
                capture_output=True,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return cmd
    raise SystemExit("找不到可用的后端解释器（试过 QIO_PYTHON / backend/.venv / uv / 当前解释器）。")


def wait_http(url: str, timeout_s: float) -> bool:
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


def clean_data_dir(target: Path) -> None:
    """只允许清理我们自己的临时数据目录（避免拼错路径时删掉别的东西）。"""
    resolved = target.resolve()
    if CATALOG_TMP_NAME not in str(resolved):
        raise SystemExit(f"拒绝清理：{resolved} 不在 {CATALOG_TMP_NAME} 下")
    if resolved.exists():
        shutil.rmtree(resolved, ignore_errors=True)
        print(f"clean: 已清空 {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)


def up(args: argparse.Namespace) -> int:
    name = args.name
    backend_port = args.backend_port
    frontend_port = args.frontend_port
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir(name)
    log_dir = Path(args.log_dir) if args.log_dir else default_log_dir(name)
    log_dir.mkdir(parents=True, exist_ok=True)

    if args.clean:
        clean_data_dir(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if state_file(name).exists():
        print(f"实例 {name} 已有状态文件，先 down：{state_file(name)}")
        return 2
    for port in (backend_port, frontend_port):
        if port_open(port):
            print(f"端口 {port} 已被占用：换一个端口，或先 down 掉占用它的实例。")
            return 2

    py = backend_python()
    origins = (
        f"http://127.0.0.1:{frontend_port},http://localhost:{frontend_port},"
        "http://127.0.0.1:5199,http://localhost:5199"
    )
    backend_env = dict(os.environ)
    backend_env.update(
        {
            "PYTHONPATH": str(ROOT / "backend" / "src"),
            "QIO_PORT": str(backend_port),
            "QIO_DATA_DIR": str(data_dir),
            "QIO_DEV_INSECURE": "1",
            "QIO_ENABLE_TEST_EVENTS": "1",
            "QIO_ALLOWED_ORIGINS": origins,
        }
    )
    vite_env = dict(os.environ)
    vite_env["VITE_QIO_BACKEND_URL"] = f"http://127.0.0.1:{backend_port}"

    if args.seed:
        seed_env = dict(os.environ)
        seed_env["QIO_DATA_DIR"] = str(data_dir)
        seed = subprocess.run(
            [*py, str(ROOT / "scripts" / "baseline" / "seed_fixtures.py"), "--reset"],
            cwd=str(ROOT),
            env=seed_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print((seed.stdout or "").strip()[-400:])
        if seed.returncode != 0:
            print((seed.stderr or "").strip()[-800:])
            return 1

    backend_log = open(log_dir / "backend.log", "w", encoding="utf-8")
    vite_log = open(log_dir / "vite.log", "w", encoding="utf-8")
    backend = subprocess.Popen(
        [
            *py,
            "-m",
            "uvicorn",
            "agent.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT / "backend"),
        env=backend_env,
        stdout=backend_log,
        stderr=subprocess.STDOUT,
    )
    vite = subprocess.Popen(
        [
            NODE,
            "node_modules/vite/bin/vite.js",
            "--port",
            str(frontend_port),
            "--strictPort",
            "--host",
            "127.0.0.1",
        ],
        cwd=str(ROOT / "frontend"),
        env=vite_env,
        stdout=vite_log,
        stderr=subprocess.STDOUT,
    )

    state = {
        "name": name,
        "backend_pid": backend.pid,
        "vite_pid": vite.pid,
        "backend_port": backend_port,
        "frontend_port": frontend_port,
        "base": f"http://127.0.0.1:{frontend_port}",
        "api": f"http://127.0.0.1:{backend_port}",
        "data_dir": str(data_dir),
        "log_dir": str(log_dir),
    }
    state_file(name).write_text(json.dumps(state, indent=2), encoding="utf-8")

    backend_ok = wait_http(f"http://127.0.0.1:{backend_port}/api/health", 30)
    vite_ok = wait_http(f"http://127.0.0.1:{frontend_port}/", 30)
    alive = backend.poll() is None and vite.poll() is None
    print(
        f"instance={name} backend={backend.pid}:{backend_port} vite={vite.pid}:{frontend_port} "
        f"ready={backend_ok and vite_ok} alive={alive}"
    )
    print(f"QIO_BASE={state['base']} QIO_API={state['api']}")
    if not (backend_ok and vite_ok and alive):
        for log in ("backend.log", "vite.log"):
            p = log_dir / log
            if p.exists():
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
                print(f"--- {p} ---\n" + "\n".join(lines))
        return 1
    return 0


def down(args: argparse.Namespace) -> int:
    path = state_file(args.name)
    if not path.exists():
        print(f"没有实例状态文件：{path}")
        return 0
    state = json.loads(path.read_text(encoding="utf-8"))
    for key in ("backend_pid", "vite_pid"):
        pid = state.get(key)
        if pid:
            # 只杀这一棵进程树（pid 来自我们自己的状态文件）
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
    path.unlink(missing_ok=True)
    print(f"instance={args.name} 已停止")
    return 0


def status(args: argparse.Namespace) -> int:
    path = state_file(args.name)
    if not path.exists():
        print(f"没有实例 {args.name}")
        return 1
    state = json.loads(path.read_text(encoding="utf-8"))
    state["backend_open"] = port_open(state["backend_port"])
    state["frontend_open"] = port_open(state["frontend_port"])
    print(json.dumps(state, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="UI 目录采集隔离实例")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_up = sub.add_parser("up", help="启动隔离实例")
    p_up.add_argument("--name", required=True)
    p_up.add_argument("--backend-port", type=int, default=8834)
    p_up.add_argument("--frontend-port", type=int, default=6199)
    p_up.add_argument("--data-dir", default="")
    p_up.add_argument("--log-dir", default="")
    p_up.add_argument("--seed", action="store_true", help="播种基线测试数据")
    p_up.add_argument("--clean", action="store_true", help="先清空该实例的数据目录")
    p_up.set_defaults(func=up)

    p_down = sub.add_parser("down", help="停止隔离实例")
    p_down.add_argument("--name", required=True)
    p_down.set_defaults(func=down)

    p_status = sub.add_parser("status", help="查看隔离实例状态")
    p_status.add_argument("--name", required=True)
    p_status.set_defaults(func=status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
