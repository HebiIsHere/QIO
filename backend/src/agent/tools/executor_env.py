"""工具执行命令的解析：用哪个程序、加哪些前置参数。

同一个 `agent/tool_worker.py`（只依赖标准库）承担执行；区别只在由谁把它跑起来：

* 正式（冻结）环境：`qio-backend.exe --tool-worker` —— 复用随包已经验证过的
  运行资源与依赖版本，不需要用户另装 Python，也不再把冻结 exe 当作
  `python -c` 解释器使用（那是另一回事：它没有解释器入口）。
* 开发与测试环境：`python <agent/tool_worker.py>` —— 与正式环境同一份 worker
  源码、同一套 stdin/stdout 协议。

上层（`tools/sandbox.py`）只依赖返回的 `ToolExecutorSpec`，不关心是哪种。
额外依赖的项目级隔离环境（QIO 管理的专用 Python）是后续阶段的工作，本阶段
默认只复用随包依赖；`QIO_TOOL_PYTHON` 只作为「显式指定解释器」的逃生口保留。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

WORKER_FLAG = "--tool-worker"


class ToolRuntimeUnavailable(RuntimeError):
    """没有可用的工具执行方式；消息面向用户/模型，说明缺什么。"""


@dataclass(frozen=True)
class ToolExecutorSpec:
    """执行一次工具请求需要的命令前缀。

    `argv` 是完整的命令行前缀；结构化请求随后从标准输入传入。
    `kind` 只用于诊断与日志（`worker-exe` / `worker-script`）。
    """

    kind: str
    argv: list[str] = field(default_factory=list)


def _worker_script_path() -> Path:
    """worker 源码路径：`agent/tools/executor_env.py` 的上一级目录。"""
    return Path(__file__).resolve().parents[1] / "tool_worker.py"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resolve_tool_executor(interpreter: str | None = None) -> ToolExecutorSpec:
    """解析工具执行命令；没有可用方式时抛 ToolRuntimeUnavailable。

    `interpreter` 用于**项目级专用环境**（见 tools/tool_envs.py）：声明了第三方
    依赖的工具由那条路径指定它自己的 Python。仍然跑同一份 worker 源码、同一套
    协议，所以上层（sandbox）不需要知道这是哪一种解释器。
    """
    if interpreter:
        script = _worker_script_path()
        if not Path(interpreter).is_file():
            raise ToolRuntimeUnavailable(f"专用环境的解释器不存在：{interpreter}")
        if not script.is_file():
            raise ToolRuntimeUnavailable(
                "专用环境只支持开发态（找不到 worker 源码）："
                f"{script}"
            )
        return ToolExecutorSpec("worker-project-env", [interpreter, str(script)])

    explicit = os.environ.get("QIO_TOOL_PYTHON")
    if explicit:
        # 逃生口：显式指定一个受控解释器（开发/诊断用）。仍走同一份 worker 源码。
        script = _worker_script_path()
        if not Path(explicit).is_file():
            raise ToolRuntimeUnavailable(f"指定的工具解释器不存在：{explicit}")
        if not script.is_file():
            raise ToolRuntimeUnavailable(
                "指定了 QIO_TOOL_PYTHON，但找不到 worker 源码（冻结环境不支持这种方式）："
                f"{script}"
            )
        return ToolExecutorSpec("worker-script", [explicit, str(script)])

    if is_frozen():
        if not sys.executable:
            raise ToolRuntimeUnavailable("冻结环境下拿不到自身可执行文件路径。")
        return ToolExecutorSpec("worker-exe", [sys.executable, WORKER_FLAG])

    script = _worker_script_path()
    if not script.is_file():
        raise ToolRuntimeUnavailable(
            f"找不到工具 worker（{script}）：开发环境的安装不完整。"
        )
    return ToolExecutorSpec("worker-script", [sys.executable, str(script)])
