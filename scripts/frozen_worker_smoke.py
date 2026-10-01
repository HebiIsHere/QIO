"""冻结产物的工具 worker 冒烟测试（stdlib only）。

源码测试跑的是 `python tool_worker.py`，用户机器上跑的是**打包后的 exe**
（`qio-backend.exe --tool-worker`）。入口分流、worker 源码有没有真的打进包、
多文件项目的 import 能不能成立，只有对着冻结产物跑才算验过。

用法：python scripts/frozen_worker_smoke.py <qio-backend.exe>
退出码：0 = 三项全过；1 = 有失败（逐条打印 PASS/FAIL）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT_SECONDS = 180


def _run_worker(exe: str, payload: dict, cwd: Path) -> tuple[int, str, str]:
    process = subprocess.run(
        [exe, "--tool-worker"],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        cwd=str(cwd),
        timeout=TIMEOUT_SECONDS,
    )
    return (
        process.returncode,
        process.stdout.decode("utf-8", errors="replace"),
        process.stderr.decode("utf-8", errors="replace"),
    )


def _single_file(exe: str, work: Path) -> tuple[bool, str]:
    code, out, err = _run_worker(
        exe,
        {
            "code": "def run(**kwargs):\n    return {'value': kwargs['x'] * 2}\n",
            "arguments": {"x": 21},
        },
        work,
    )
    if code != 0:
        return False, f"退出码 {code}（stderr：{err.strip()[:200]}）"
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) != 1:
        return False, f"结果通道不是恰好一行（实际 {len(lines)} 行）"
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError:
        return False, "返回的不是合法 JSON"
    if payload.get("value") != {"value": 42}:
        return False, f"返回值不对：{payload.get('value')!r}"
    return True, "单文件代码：一行 JSON，值正确"


def _multi_file(exe: str, work: Path) -> tuple[bool, str]:
    package = work / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "util.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    (package / "main.py").write_text(
        "from .util import double\n\ndef run(**kwargs):\n    return {'value': double(kwargs['x'])}\n",
        encoding="utf-8",
    )
    code, out, err = _run_worker(
        exe, {"code": "", "entry": "pkg.main:run", "arguments": {"x": 4}}, work
    )
    if code != 0:
        return False, f"退出码 {code}（stderr：{err.strip()[:200]}）"
    try:
        payload = json.loads(out.strip() or "{}")
    except json.JSONDecodeError:
        return False, "返回的不是合法 JSON"
    if payload.get("value") != {"value": 8}:
        return False, f"返回值不对：{payload.get('value')!r}（包内相对 import 没成立？）"
    return True, "多文件项目 + 包内相对 import：值正确"


def _broken_worker_is_rejected(exe: str, work: Path) -> tuple[bool, str]:
    """先打印一行形似成功的结果、再以非零码退出：父进程不能当成成功。"""
    code, out, _err = _run_worker(
        exe,
        {
            "code": "import sys\nprint('{\"ok\": true, \"value\": {}}')\nsys.exit(17)\n",
            "arguments": {},
        },
        work,
    )
    if code == 0:
        return False, "工具以非零码退出，worker 却报了成功"
    if out.strip():
        return False, "异常退出时仍然把结果写进了结果通道"
    return True, f"异常退出（exit {code}）没有结果输出"


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：python scripts/frozen_worker_smoke.py <qio-backend.exe>")
        return 1
    exe = str(Path(argv[0]).resolve())
    if not Path(exe).is_file():
        print(f"[NOT RUN] 找不到冻结产物：{exe}")
        return 1
    work = Path(tempfile.mkdtemp(prefix="frozen-worker-smoke-"))
    failures = 0
    try:
        for name, check in (
            ("单文件代码", _single_file),
            ("多文件项目", _multi_file),
            ("坏 worker 被拒", _broken_worker_is_rejected),
        ):
            try:
                ok, detail = check(exe, work)
            except (OSError, subprocess.SubprocessError) as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")
            failures += 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
