"""项目级依赖环境：QIO 管理的专用 Python（按需准备，绝不碰系统环境）。

为什么需要它：声明了第三方依赖也没有任何地方能把它装上 —— 工具永远跑不起来，
用户还得自己猜要装什么、装到哪。这里给每个**依赖集合**准备一个专用虚拟环境：
按需创建、只装声明过的包、记录指纹；之后这个项目的测试与运行都用它。

边界（说清楚，不夸大）：

* 只装 `requirements` 里声明的东西；不升级、不改系统环境、不注入凭据；
* 安装是一次网络 + 磁盘动作，**必须先拿到用户确认**（`dependency_install`）；
* 环境按「依赖集合」复用：同一组依赖只准备一次，目录在 QIO 数据目录下；
* 环境没准备好时**不静默回落到随包解释器** —— 那等于假装依赖已经装上了。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Sequence

DEFAULT_INSTALL_TIMEOUT_SECONDS = 600.0
MANIFEST_NAME = "qio-env.json"
_MANIFEST_SCHEMA = 1
# 安装输出只留末尾这些字符：报错要看得到，内存不能被 pip 的长日志吃掉。
_OUTPUT_TAIL_CHARS = 2000

EnvRunner = Callable[..., Awaitable[tuple[bool, str]]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_env() -> dict[str, str]:
    """安装用的环境：系统必需项 + 无业务变量、无凭据。"""
    keep = (
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
    )
    env = {key: os.environ[key] for key in keep if os.environ.get(key)}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def _default_runner(argv: list[str], timeout: float, cwd: str | None = None):
    """真的把命令跑起来（默认实现）。返回 (成功与否, 输出末尾)。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=_clean_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return False, f"无法启动安装命令：{exc}"
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        return False, f"安装超时（超过 {timeout:g} 秒）"
    text = output.decode("utf-8", errors="replace").strip()
    return process.returncode == 0, text[-_OUTPUT_TAIL_CHARS:]


@dataclass(frozen=True)
class EnvStatus:
    """一次「环境是否可用」的结论：不编造、不含糊。"""

    ok: bool
    interpreter: str | None
    reason: str | None = None
    reused: bool = False
    installed: list[str] = field(default_factory=list)


class ToolEnvManager:
    """按依赖集合管理专用 Python 环境。"""

    def __init__(
        self,
        root: str | Path,
        *,
        base_python: str | None = None,
        runner: EnvRunner | None = None,
        timeout_seconds: float = DEFAULT_INSTALL_TIMEOUT_SECONDS,
    ) -> None:
        self.root = Path(root)
        # 用哪个 Python 去建环境：随包的这一个（冻结态由 executor_env 保证不是后端 exe）。
        self.base_python = base_python or sys.executable
        self._runner: EnvRunner = runner or _default_runner
        self.timeout_seconds = timeout_seconds

    # -- 位置与指纹 -------------------------------------------------------

    @property
    def interpreter_name(self) -> str:
        return "Scripts/python.exe" if os.name == "nt" else "bin/python"

    def key_for(self, requirements: Sequence[str]) -> str:
        """依赖集合的稳定指纹（顺序无关）：同一组依赖共用一个环境。"""
        payload = json.dumps(sorted(str(item).strip() for item in requirements), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def directory_for(self, requirements: Sequence[str]) -> Path:
        return self.root / self.key_for(requirements)

    def interpreter_for(self, requirements: Sequence[str]) -> str:
        return str(self.directory_for(requirements) / self.interpreter_name)

    def _manifest_path(self, requirements: Sequence[str]) -> Path:
        return self.directory_for(requirements) / MANIFEST_NAME

    def _read_manifest(self, requirements: Sequence[str]) -> dict | None:
        try:
            raw = self._manifest_path(requirements).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != _MANIFEST_SCHEMA:
            return None
        recorded = data.get("requirements")
        if not isinstance(recorded, list):
            return None
        if sorted(str(item) for item in recorded) != sorted(
            str(item).strip() for item in requirements
        ):
            return None
        return data

    def is_ready(self, requirements: Sequence[str]) -> bool:
        """环境真的可用 = 记录对得上 **且** 解释器真的在。"""
        if not requirements:
            return False
        if self._read_manifest(requirements) is None:
            return False
        return Path(self.interpreter_for(requirements)).is_file()

    def status_for(self, requirements: Sequence[str]) -> EnvStatus:
        """只查不建：没有依赖 → 用随包环境；有依赖但没准备好 → 明确说没准备好。"""
        if not requirements:
            return EnvStatus(ok=True, interpreter=None)
        if self.is_ready(requirements):
            return EnvStatus(ok=True, interpreter=self.interpreter_for(requirements), reused=True)
        return EnvStatus(
            ok=False,
            interpreter=None,
            reason=(
                "这个工具声明了第三方依赖，专用环境还没准备好："
                "需要先跑一次测试（会问你是否允许安装依赖）再调用它。"
            ),
        )

    # -- 准备 -------------------------------------------------------------

    async def ensure(
        self,
        requirements: Sequence[str],
        *,
        approvals=None,
        tool_name: str = "",
        task_id: str | None = None,
    ) -> EnvStatus:
        """按需准备专用环境；没有依赖时直接用随包环境（interpreter=None）。"""
        wanted = [str(item).strip() for item in requirements if str(item).strip()]
        if not wanted:
            return EnvStatus(ok=True, interpreter=None)
        if self.is_ready(wanted):
            return EnvStatus(ok=True, interpreter=self.interpreter_for(wanted), reused=True)
        if approvals is None:
            return EnvStatus(
                ok=False,
                interpreter=None,
                reason=(
                    "声明了第三方依赖，但没有可用的确认通道来征求安装许可："
                    "这次没有准备专用环境。"
                ),
            )
        decision = await approvals.request(
            "dependency_install",
            {
                "packages": list(wanted),
                "tool": tool_name,
                "task_id": task_id,
                "detail": (
                    "这个工具声明了第三方依赖，需要为它准备一个专用环境并安装："
                    + "、".join(wanted)
                    + "。只装这些包，不升级其它东西，也不碰系统环境。"
                ),
            },
        )
        if getattr(decision, "decision", None) != "approved":
            return EnvStatus(
                ok=False,
                interpreter=None,
                reason="你（或超时）没有同意安装依赖：这次没有准备专用环境，测试未执行。",
            )
        return await self._prepare(wanted)

    async def _prepare(self, requirements: list[str]) -> EnvStatus:
        directory = self.directory_for(requirements)
        try:
            directory.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return EnvStatus(False, None, f"无法创建专用环境的目录：{exc}")
        ok, output = await self._runner(
            [self.base_python, "-m", "venv", str(directory)],
            self.timeout_seconds,
            str(directory.parent),
        )
        if not ok:
            return EnvStatus(False, None, f"创建专用环境失败：{output or '（没有输出）'}")
        interpreter = self.interpreter_for(requirements)
        if not Path(interpreter).is_file():
            return EnvStatus(False, None, "专用环境建好了，但找不到它的 Python 解释器")
        ok, output = await self._runner(
            [
                interpreter,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *requirements,
            ],
            self.timeout_seconds,
            str(directory),
        )
        if not ok:
            # 不写记录：失败的安装不能被记成「已就绪」，否则以后永远跑不起来。
            return EnvStatus(False, None, f"安装依赖失败：{output or '（没有输出）'}")
        try:
            self._manifest_path(requirements).write_text(
                json.dumps(
                    {
                        "schema": _MANIFEST_SCHEMA,
                        "requirements": sorted(requirements),
                        "created_at": _now(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            return EnvStatus(False, None, f"安装完成但无法记录环境信息：{exc}")
        return EnvStatus(
            ok=True,
            interpreter=interpreter,
            reused=False,
            installed=sorted(requirements),
        )
