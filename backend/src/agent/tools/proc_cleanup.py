"""进程树清理（契约 2）：只清自己的、清完要验证、不确定就承认。

原则：

* **只按本次启动拿到的 pid 操作**，绝不按程序名匹配 —— 同名的其他实例
  （诱饵进程）不能被误伤；
* Windows：taskkill /T /F /PID <pid> 真实调用，/T 把孙进程一并入树；
* POSIX：子进程以 start_new_session 创建（自成进程组），清理时对整组发
  SIGKILL，孙进程一并收割；
* 终止后要 **await 真正退出验证**；确认不了就如实返回 False，调用方必须把
  「清理未确认，进程可能仍在运行」写进错误，不得伪装成已经停止。
"""

from __future__ import annotations

import asyncio
import os
import signal

IS_WINDOWS = os.name == "nt"

# 终止动作之后等待子进程真正退出的宽限；taskkill /F 下通常 <1s。
WAIT_EXIT_SECONDS = 10.0


def new_session_kwargs() -> dict:
    """create_subprocess_exec / create_subprocess_shell 的进程组参数。

    POSIX 上让子进程自立进程组（清理用 killpg 连孙进程一起收割）；
    Windows 不支持该参数（用 taskkill /T 按树收割），返回空字典。
    """
    if IS_WINDOWS:
        return {}
    return {"start_new_session": True}


async def terminate_process_tree(
    proc: asyncio.subprocess.Process, *, wait_exit_seconds: float = WAIT_EXIT_SECONDS
) -> bool:
    """终止本次启动的进程树，并 await 验证其真正退出。

    返回 True = 已确认退出（终止动作执行 + 直接子进程确实退出）；
    返回 False = 清理未确认，调用方必须如实报告「进程可能仍在运行」。
    """
    if proc is None:
        return False
    pid = proc.pid
    if IS_WINDOWS:
        await _taskkill_tree(pid)
    else:
        _kill_group_posix(pid)
    return await _wait_child_exit(proc, timeout=wait_exit_seconds)


async def _taskkill_tree(pid: int) -> None:
    """Windows：按 pid 收割整棵树。taskkill 起不动时由退出验证兜底发现。"""
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(killer.communicate(), timeout=15)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 起不动 taskkill 不等于没杀掉，以退出验证为准
        pass


def _kill_group_posix(pid: int) -> None:
    """POSIX：对子进程所属进程组发 SIGKILL（start_new_session 时 pid 即组长）。"""
    try:
        os.killpg(pid, signal.SIGKILL)
        return
    except ProcessLookupError:
        return  # 已经退出
    except (OSError, PermissionError):
        pass  # 组权限不足时退回直接杀子进程
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        return  # 杀不动只能让退出验证去发现


async def _wait_child_exit(proc: asyncio.subprocess.Process, *, timeout: float) -> bool:
    """终止之后 await 直接子进程真正退出（这就是「退出验证」）。"""
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
