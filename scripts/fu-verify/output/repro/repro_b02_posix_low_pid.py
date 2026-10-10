# -*- coding: utf-8 -*-
"""V 组反例证据（B02）：POSIX 低 PID（1/2/3/4）被当成「判不出来」。

现场（基线 `da0436b`）：`_default_pid_alive` 在分平台**之前**就 `pid <= 4 → None`。
用受控替身把平台设成 POSIX，函数一行都不探测就返回 None —— 归属者明明已经死了，
`owner_alive()` 也只能给 unknown，记录永远恢复不了。
"""
from __future__ import annotations

import os
import sys

import agent.storage.instance_registry as ir  # noqa: E402
from agent.storage.instance_registry import _default_pid_alive  # noqa: E402


class FakeOS:
    """受控平台替身：只暴露函数体真正用到的两个成员。"""

    def __init__(self, name: str, kill) -> None:  # noqa: ANN001
        self.name = name
        self._kill = kill

    def kill(self, pid: int, sig: int):  # noqa: ANN201
        return self._kill(pid, sig)


def main() -> None:
    seen: list[tuple[int, int]] = []

    def kill(pid: int, sig: int):  # noqa: ANN202
        seen.append((pid, sig))
        raise ProcessLookupError(3, "No such process")

    real_os = ir.os
    ir.os = FakeOS("posix", kill)
    try:
        results = {pid: _default_pid_alive(pid) for pid in (1, 2, 3, 4)}
        print("[基线观察] 平台=posix，os.kill 替身记录 =", seen)
        print("[基线观察] _default_pid_alive(1/2/3/4) =", results)
        print("[基线观察] 真实平台 os.name =", real_os.name, " 测试进程 pid =", os.getpid())
    finally:
        ir.os = real_os

    print("[结论] POSIX 上 1/2/3/4 是合法 PID，却全部被判成「判不出来」（None）；"
          "探测函数一次都没被调用。")

    # 对照：Windows 特殊 PID 的既有行为（不受本缺陷影响）
    def never_kill(pid: int, sig: int):  # noqa: ANN202
        raise AssertionError("Windows 分支不得调用 os.kill")

    ir.os = FakeOS("nt", never_kill)
    try:
        print("[对照] 平台=nt，_default_pid_alive(0..4) =",
              {pid: _default_pid_alive(pid) for pid in (0, 1, 2, 3, 4)})
    finally:
        ir.os = real_os

    sys.stdout.flush()


if __name__ == "__main__":
    main()
