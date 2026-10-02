"""C3 验证：用**生产代码**（tools/isolation.py）在普通完整性父进程下验证目标三件套。

三件套：Low 工具写自己的 scratch = WRITE-OK；写用户目录 = WRITE-DENIED；写 QIO 数据目录 = WRITE-DENIED。

为什么要单独一个脚本：本机 `uv run` 出来的 python 自己在 Low 完整性（降级是 no-op），
必须用普通完整性的解释器当父进程才复现得了 CI 场景；而 venv 是 3.11、系统 python 是 3.12，
直接 import agent.* 会撞 pydantic ABI，所以这里用 importlib 单文件加载 isolation.py（它只用标准库）。

用法：& "<系统 python312>\python.exe" scripts\low_integrity_real_check.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISOLATION = ROOT / "backend" / "src" / "agent" / "tools" / "isolation.py"

spec = importlib.util.spec_from_file_location("qio_isolation", ISOLATION)
assert spec and spec.loader
isolation = importlib.util.module_from_spec(spec)
sys.modules["qio_isolation"] = isolation  # dataclass 需要模块已在 sys.modules 里
spec.loader.exec_module(isolation)

os.environ[isolation.LOW_INTEGRITY_ENV] = "1"  # 显式打开（默认是关的）

CHILD = (
    "import os, sys\n"
    "def touch(path):\n"
    "    try:\n"
    "        with open(os.path.join(path, 'probe.txt'), 'w') as fh:\n"
    "            fh.write('x')\n"
    "        return 'WRITE-OK'\n"
    "    except OSError as exc:\n"
    "        return 'WRITE-DENIED ' + type(exc).__name__\n"
    "sys.stdout.write('READY\\n'); sys.stdout.flush()\n"
    "sys.stdin.readline()\n"
    "print('scratch=' + touch(sys.argv[1]))\n"
    "print('user=' + touch(sys.argv[2]))\n"
    "print('qio=' + touch(sys.argv[3]))\n"
)


def main() -> None:
    interpreter = os.environ.get("QIO_PROBE_PYTHON") or sys.executable
    print("=== C3 生产代码验证（isolation.py）===")
    print("父进程解释器:", interpreter)
    print("父进程完整性:", isolation.integrity_of_process(os.getpid()))
    print("low_integrity_enabled:", isolation.low_integrity_enabled())

    base = Path(tempfile.mkdtemp(prefix="c3-base-"))
    scratch = base / "scratch"
    user_dir = base / "user-files"
    qio_dir = base / "qio-data"
    for path in (scratch, user_dir, qio_dir):
        path.mkdir()
    print("基准目录:", base)

    how = isolation.label_low(str(scratch))
    print("label_low(scratch) ->", repr(how))
    print("读回 scratch 标签:", repr(isolation.integrity_label_of(str(scratch))))
    print("label_is_low:", isolation.label_is_low(isolation.integrity_label_of(str(scratch))))

    proc = subprocess.Popen(
        [interpreter, "-c", CHILD, str(scratch), str(user_dir), str(qio_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    print("child READY:", proc.stdout.readline().strip())
    print("child 降级前 IL:", isolation.integrity_of_process(proc.pid))
    outcome = isolation.harden(proc, scratch_dir=str(scratch), policy=None, extra_writable_dirs=[str(scratch)])
    print("harden outcome:", json.dumps(outcome.as_dict(), ensure_ascii=False))
    print("child 降级后 IL:", isolation.integrity_of_process(proc.pid))
    proc.stdin.write("go\n"); proc.stdin.flush()
    out, err = proc.communicate(timeout=60)
    print(out.strip())
    if err.strip():
        print("child stderr:", err.strip()[:300])
    isolation.release(proc)


if __name__ == "__main__":
    main()
