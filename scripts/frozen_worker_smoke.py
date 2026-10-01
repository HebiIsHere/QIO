"""工具 worker 冒烟测试（stdlib only）：真子进程，跑真协议。

两种跑法，**同一套三项检查**：

* 冻结产物（用户机器上真正跑的东西）：
  `python scripts/frozen_worker_smoke.py <qio-backend.exe>`
* 源码 worker（Linux CI 上的「真 worker 冒烟」）：
  `python scripts/frozen_worker_smoke.py --script <backend/src/agent/tool_worker.py>`

检查：单文件代码出一行 JSON / 多文件项目 + 包内相对 import / 坏 worker（自己打印
一行假成功再非零退出）被拒。退出码：0 = 三项全过；1 = 有失败（逐条打印 PASS/FAIL）。

____ 编码（2026-10-02 修）____

报告层以前在 cp1252 控制台（GitHub 的 windows-latest）上**打印第一条结果就崩**：
`UnicodeEncodeError: 'charmap' codec can't encode ...`，于是 CI 里只看到 traceback，
一条 PASS/FAIL 都没有 —— 「冒烟到底过没过」反而没人知道。现在：

* 输出通道先按「能不能编码」降级（编不出来的字符转义，绝不抛异常）；
* 重定向/CI 场景直接写 UTF-8 字节，日志按 UTF-8 解码；
* 子进程协议输出仍按 UTF-8 解释（协议是字节层的 UTF-8，见 agent/tool_worker.py）。

注意：这里**不**给子进程设 PYTHONIOENCODING —— 那会掩盖「worker 自己没钉住编码」
这类真实缺陷（正是它让 windows CI 的 3 条 worker 用例变红）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT_SECONDS = 180

USAGE = (
    "用法：python scripts/frozen_worker_smoke.py <qio-backend.exe>\n"
    "      python scripts/frozen_worker_smoke.py --script <backend/src/agent/tool_worker.py>"
)


def _configure_output() -> None:
    """报告层必须能在任何控制台编码下工作（cp1252 的 CI runner 也不能崩）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            is_tty = bool(getattr(stream, "isatty", lambda: False)())
        except (OSError, ValueError):
            is_tty = False
        try:
            if is_tty:
                # 老控制台（cp1252/cp936）：保留它的编码，编不出来的字符转义，别崩。
                reconfigure(errors="backslashreplace")
            else:
                # 重定向 / CI：写 UTF-8 字节，日志按 UTF-8 解码。
                reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            continue


def _launch_command(argv: list[str]) -> tuple[list[str], str]:
    """解析命令行 → (命令前缀, 人类可读的模式名)。"""
    if argv[:1] == ["--script"]:
        if len(argv) < 2:
            raise SystemExit(USAGE)
        script = Path(argv[1]).resolve()
        if not script.is_file():
            raise SystemExit(f"[NOT RUN] 找不到 worker 源码：{script}")
        return [sys.executable, str(script)], "源码 worker"
    if not argv:
        raise SystemExit(USAGE)
    exe = str(Path(argv[0]).resolve())
    if not Path(exe).is_file():
        raise SystemExit(f"[NOT RUN] 找不到冻结产物：{exe}")
    return [exe, "--tool-worker"], "冻结产物"


def _run_worker(command: list[str], payload: dict, cwd: Path) -> tuple[int, str, str]:
    process = subprocess.run(
        command,
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


def _single_file(command: list[str], work: Path) -> tuple[bool, str]:
    code, out, err = _run_worker(
        command,
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


def _multi_file(command: list[str], work: Path) -> tuple[bool, str]:
    package = work / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "util.py").write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    (package / "main.py").write_text(
        "from .util import double\n\ndef run(**kwargs):\n    return {'value': double(kwargs['x'])}\n",
        encoding="utf-8",
    )
    code, out, err = _run_worker(
        command, {"code": "", "entry": "pkg.main:run", "arguments": {"x": 4}}, work
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


def _broken_worker_is_rejected(command: list[str], work: Path) -> tuple[bool, str]:
    """先打印一行形似成功的结果、再以非零码退出：父进程不能当成成功。"""
    code, out, _err = _run_worker(
        command,
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
    _configure_output()
    command, mode = _launch_command(argv)
    work = Path(tempfile.mkdtemp(prefix="frozen-worker-smoke-"))
    failures = 0
    print(f"[RUN ] {mode}：{' '.join(command)}")
    try:
        for name, check in (
            ("单文件代码", _single_file),
            ("多文件项目", _multi_file),
            ("坏 worker 被拒", _broken_worker_is_rejected),
        ):
            try:
                ok, detail = check(command, work)
            except (OSError, subprocess.SubprocessError) as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            print(f"[{'PASS' if ok else 'FAIL'}] {name} — {detail}")
            failures += 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"[DONE] {mode}：{3 - failures}/3 通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
