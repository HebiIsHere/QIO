"""本轮「唯一一次全量本地验证」的执行器（Lead 专用）。

为什么要有它：本轮口径是**全量验证只执行一次**，且每一项各只跑一次。
把清单写成代码，跑完就落盘每一单项的命令、退出码与输出尾，避免"顺手再跑一遍"
或事后无法证明"哪一项跑了什么、结果是什么"。

用法（在集成分支的 worktree 根目录下）：

    python scripts/uc_full_verify.py --run

产物：`docs/superpowers/plans/uc-full-verify-<时间戳>/<序号>-<名称>.txt`
     + `summary.json`（每项：名称 / 命令 / 退出码 / 耗时 / 输出文件）

注意：**不要**为了"再确认一次"重复执行本脚本。失败时按台账口径只重跑失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "docs" / "superpowers" / "plans"

#: 全量本地验证的完整清单（各执行一次）
CHECKS: list[tuple[str, list[str], str]] = [
    (
        "backend-full",
        ["python", "-m", "pytest"],
        "backend",
    ),
    (
        "frontend-typecheck",
        ["npx", "vue-tsc", "--noEmit"],
        "frontend",
    ),
    (
        "frontend-tests",
        ["npx", "vitest", "run"],
        "frontend",
    ),
    (
        "frontend-build",
        ["npm", "run", "build"],
        "frontend",
    ),
    (
        "docs-consistency",
        ["python", "scripts/check_docs.py"],
        ".",
    ),
]


def _python() -> str:
    """后端子进程统一用仓库自己的 venv 解释器（避免 uv 在 CI 之外解析依赖）。"""
    venv = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    return sys.executable


def _npm(name: str) -> str:
    """Windows 上 npm / npx 是 npm.cmd / npx.cmd：**必须**经 cmd 启动。

    第一次跑这一套时就是踩在这里：直接 CreateProcess("npx") 会 WinError 2，
    而那一项根本没被执行（不是测试失败）。这里是真正的修复，不是绕过。
    """
    import shutil

    for candidate in (f"{name}.cmd", f"{name}.exe", name):
        found = shutil.which(candidate)
        if found:
            return found
    return name


def _command_for(name: str, command: list[str], cwd: Path) -> list[str]:
    if command[:2] == ["python", "-m"] and name == "backend-full":
        return [_python(), "-m", "pytest"]
    if name == "docs-consistency":
        return [sys.executable, "scripts/check_docs.py"]
    if command and command[0] in ("npx", "npm"):
        return [_npm(command[0]), *command[1:]]
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="唯一一次全量本地验证")
    parser.add_argument("--run", action="store_true", help="真的执行（不加只打印清单）")
    parser.add_argument("--only", default="", help="逗号分隔，只跑指定项（用于失败后的**定向补验**）")
    args = parser.parse_args(argv)

    only = {item.strip() for item in args.only.split(",") if item.strip()}
    checks = [c for c in CHECKS if not only or c[0] in only]

    if not args.run:
        for name, command, where in checks:
            print(f"{name}	({' '.join(command)})	(cwd={where})")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / f"uc-full-verify-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("QIO_DATA_DIR", str(ROOT / ".tmp-full-verify-data"))
    env.setdefault("QIO_DISABLE_DB_CHECK", "1")

    summary: list[dict] = []
    for index, (name, command, where) in enumerate(checks, start=1):
        cwd = (ROOT / where).resolve()
        real = _command_for(name, command, cwd)
        started = time.time()
        print(f"[{index}/{len(checks)}] {name}: {' '.join(real)} (cwd={cwd})", flush=True)
        proc = subprocess.run(real, cwd=str(cwd), env=env, capture_output=True, text=True)
        elapsed = time.time() - started
        log = out_dir / f"{index:02d}-{name}.txt"
        log.write_text(
            f"# {name}\n# command: {' '.join(real)}\n# cwd: {cwd}\n"
            f"# exit: {proc.returncode}\n# seconds: {elapsed:.1f}\n\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n",
            encoding="utf-8",
        )
        summary.append(
            {
                "name": name,
                "command": " ".join(real),
                "cwd": str(cwd.relative_to(ROOT)) or ".",
                "exit": proc.returncode,
                "seconds": round(elapsed, 1),
                "log": str(log.relative_to(ROOT)),
            }
        )
        print(f"    exit={proc.returncode} ({elapsed:.1f}s) -> {log.relative_to(ROOT)}", flush=True)

    (out_dir / "summary.json").write_text(
        json.dumps({"generatedAt": stamp, "checks": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    failed = [item["name"] for item in summary if item["exit"] != 0]
    print()
    print(f"全量验证完成：{len(summary) - len(failed)}/{len(summary)} 通过；失败项：{failed or '（无）'}")
    print(f"产物目录：{out_dir.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
