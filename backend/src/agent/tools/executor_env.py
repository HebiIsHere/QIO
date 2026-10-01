"""工具执行命令的解析：用哪个程序、加哪些前置参数。

同一个 `agent/tool_worker.py`（只依赖标准库）承担执行；区别只在由谁把它跑起来：

* 正式（冻结）环境：`qio-backend.exe --tool-worker` —— 复用随包已经验证过的
  运行资源与依赖版本，不需要用户另装 Python，也不再把冻结 exe 当作
  `python -c` 解释器使用（那是另一回事：它没有解释器入口）。
* 开发与测试环境：`python <agent/tool_worker.py>` —— 与正式环境同一份 worker
  源码、同一套 stdin/stdout 协议。
* 容器隔离执行：`docker run … python -c <引导脚本>` —— 引导脚本把 `worker_source()`
  读到的**同一份源码**写进容器再执行（见 tools/sandbox.py），协议不变。

冻结产物里没有 `agent/tool_worker.py` 这个文件，但 PyInstaller 包内带了它的副本
（`--add-data`，见 scripts/build_sidecar.ps1），`worker_source()` 会读那一份。

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

# 冻结产物里 worker 源码的位置：build_sidecar.ps1 用 --add-data 把它放进包内
# （PyInstaller onefile 的解包目录 = sys._MEIPASS）。容器隔离执行要把源码文本
# 带进容器，冻结态就得从这里读。
FROZEN_WORKER_RESOURCE = ("agent", "tool_worker.py")


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


def worker_source() -> str:
    """worker 源码文本：容器隔离执行要把**同一份**实现带进容器跑。

    容器里看不到宿主文件系统，源码只能以文本递进去。这里保证递进去的就是当前这
    一份实现（冻结产物读包内资源，开发态读源码路径）；找不到就如实报环境错误，
    绝不退回「在容器里另写一段跑工具代码的脚本」—— 那会变成第二套协议实现，
    结果格式与退出码语义立刻开始漂移（2026-10-02 之前就是这样）。
    """
    candidates = _worker_source_candidates()
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8")
        except OSError:
            continue
    raise ToolRuntimeUnavailable(
        "容器隔离执行需要 worker 源码（冻结产物应随包携带 agent/tool_worker.py）："
        + "、".join(str(path) for path in candidates)
    )


def _worker_source_candidates() -> list[Path]:
    """按优先级列出可能的 worker 源码位置（冻结资源 → 源码路径）。"""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass).joinpath(*FROZEN_WORKER_RESOURCE))
    candidates.append(_worker_script_path())
    return candidates


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
