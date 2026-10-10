"""V 组独立验证 · B02：进程存活探测必须先分平台，再判特殊 PID。

原缺陷（基线 `da0436b`，见 `_contracts` §7）

    `_default_pid_alive()` 在**分平台之前**就把 `pid <= 4` 一律判成 `None`：

        if ivalue <= 4:
            # 0/负数没有意义；Windows 上 0/4 是内核伪 pid，查询行为不可靠。
            return None
        if os.name == "nt":
            ...

    `pid <= 4` 这条规则只在 Windows 上成立（0/4 是内核伪 pid）。POSIX 上
    1 / 2 / 3 / 4 都是**合法 PID**（init 是 1），必须正常探测。基线把整段
    POSIX 低 PID 判成「判不出来」→ `owner_alive()` 只能返回 unknown →
    归属者明明已经死了却永远恢复不了（保守保留变成永久卡住）。

验证手段（受控替身，**不碰任何真实进程**）

    `agent.storage.instance_registry` 里的 `os` 是模块级名字，函数体按模块全局
    查找它。于是 `monkeypatch.setattr(ir, "os", 替身)` 就能给出一个受控的
    「POSIX 平台 + 记录型 kill」，断言：
      * 探测**确实发生**（kill(pid, 0) 被调用，信号必须是 0）；
      * 各分支返回值符合冻结契约。

断言只看返回值与替身记录，不看实现细节；原缺陷在时红、修复后仍绿。

判据说明（补充修复轮修正，详见 `scripts/fu-verify/README.md`）

    「其它 OSError」不能用 `OSError(13, ...)` 构造：EACCES 在 Python 3.3+ 会按
    errno 映射成 `PermissionError`，那正是「进程在、只是没权限 → True」的既有
    分支。表达基础 `OSError` 要用不会被映射的 errno（本文件用 `errno.EIO`）。
"""

from __future__ import annotations

import errno

import pytest

import agent.storage.instance_registry as ir
from agent.storage.instance_registry import _default_pid_alive


class _FakeOS:
    """受控 platform 替身：只暴露函数体真正用到的两个成员。"""

    def __init__(self, name: str, kill) -> None:  # noqa: ANN001
        self.name = name
        self._kill = kill

    def kill(self, pid: int, sig: int):  # noqa: ANN201
        return self._kill(pid, sig)


def _posix(kill):  # noqa: ANN001, ANN202
    return _FakeOS("posix", kill)


def _windows(kill):  # noqa: ANN001, ANN202
    return _FakeOS("nt", kill)


# --------------------------------------------------------------------------
# POSIX：1 / 2 / 3 / 4 是合法 PID
# --------------------------------------------------------------------------


def test_posix_low_pids_are_probed_not_short_circuited(monkeypatch):
    """POSIX 上 1/2/3/4 必须真的去探测（基线一行不探就返回 None）。"""
    seen: list[tuple[int, int]] = []

    def kill(pid: int, sig: int):  # noqa: ANN202
        seen.append((pid, sig))
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(ir, "os", _posix(kill))

    for pid in (1, 2, 3, 4):
        assert _default_pid_alive(pid) is False, f"POSIX PID {pid} 不存在时必须返回 False"

    assert [item[0] for item in seen] == [1, 2, 3, 4], "低 PID 必须逐个探测，不得整段短路"
    assert {item[1] for item in seen} == {0}, "POSIX 存在性探测只能用信号 0"


def test_posix_low_pid_exists_returns_true(monkeypatch):
    """信号 0 正常返回 = 进程存在（PID 1 也一样）。"""

    def kill(pid: int, sig: int):  # noqa: ANN202
        return None

    monkeypatch.setattr(ir, "os", _posix(kill))

    for pid in (1, 2, 3, 4):
        assert _default_pid_alive(pid) is True, f"POSIX PID {pid} 存在时必须返回 True"


def test_posix_low_pid_permission_error_means_alive(monkeypatch):
    """权限不足说明进程存在（别人家的 init / 系统进程）→ True，不是 unknown。"""

    def kill(pid: int, sig: int):  # noqa: ANN202
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(ir, "os", _posix(kill))

    assert _default_pid_alive(1) is True, "PID 1 权限不足 → 进程在"
    assert _default_pid_alive(4) is True, "PID 4 权限不足 → 进程在"


def test_posix_low_pid_other_oserror_is_unknown(monkeypatch):
    """其它 OSError 判不出来 → None（绝不当作死）。

    注意构造方式：Python 3.3+ 的 `OSError(errno, ...)` 会按 errno **映射出子类**
    —— `OSError(13, ...)` 就是 `PermissionError`（EACCES），那属于「进程在、只是
    没权限」的既有分支（→ True），表达不了「其它 OSError」。所以这里用不会被映射
    成已知子类的 `errno.EIO`（EIO → 基础 `OSError`）。
    """

    other_oserror = OSError(errno.EIO, "I/O error")
    assert not isinstance(other_oserror, (PermissionError, ProcessLookupError)), (
        "本用例必须构造一个**没有**被映射成已知子类的 OSError"
    )

    def kill(pid: int, sig: int):  # noqa: ANN202
        raise other_oserror

    monkeypatch.setattr(ir, "os", _posix(kill))

    assert _default_pid_alive(3) is None


def test_posix_nonpositive_pid_is_meaningless(monkeypatch):
    """通用规则：pid <= 0 没有意义 → None（这条两个平台都必须成立）。"""

    def kill(pid: int, sig: int):  # noqa: ANN202
        raise AssertionError("pid <= 0 不该去探测")

    monkeypatch.setattr(ir, "os", _posix(kill))

    for pid in (0, -1, -999):
        assert _default_pid_alive(pid) is None


def test_posix_real_high_pid_uses_signal_zero(monkeypatch):
    """POSIX 高 PID 的既有行为不得倒退（对照）。"""
    seen: list[tuple[int, int]] = []

    def kill(pid: int, sig: int):  # noqa: ANN202
        seen.append((pid, sig))
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(ir, "os", _posix(kill))

    assert _default_pid_alive(4242) is False
    assert seen == [(4242, 0)]


# --------------------------------------------------------------------------
# Windows：0 / 1 / 2 / 3 / 4 一律 None，且**绝不调用 os.kill**
# --------------------------------------------------------------------------


def test_windows_special_pids_never_probed(monkeypatch):
    """Windows 上 0/1/2/3/4 都是不可靠查询（0/4 是内核伪 pid）→ None，且不探测。"""

    def kill(pid: int, sig: int):  # noqa: ANN202
        raise AssertionError("Windows 分支绝不能走 os.kill")

    monkeypatch.setattr(ir, "os", _windows(kill))

    for pid in (0, -1, 1, 2, 3, 4):
        assert _default_pid_alive(pid) is None, f"Windows PID {pid} 必须保守判 unknown"


def test_windows_above_four_goes_through_openprocess_probe(monkeypatch):
    """Windows 上 >4 的 PID 走只读 OpenProcess 探测（受控替身），不得用 os.kill。"""

    def kill(pid: int, sig: int):  # noqa: ANN202
        raise AssertionError("Windows 分支绝不能走 os.kill")

    monkeypatch.setattr(ir, "os", _windows(kill))
    monkeypatch.setattr(ir, "_windows_pid_alive", lambda pid: True if pid == 4321 else False)

    assert _default_pid_alive(4321) is True
    assert _default_pid_alive(5555) is False


def test_windows_probe_failure_is_unknown(monkeypatch):
    """Windows 探测抛异常 → None（判不出来就承认判不出来）。"""

    def kill(pid: int, sig: int):  # noqa: ANN202
        raise AssertionError("Windows 分支绝不能走 os.kill")

    def boom(pid: int) -> bool:  # noqa: ANN001
        raise OSError("ctypes unavailable")

    monkeypatch.setattr(ir, "os", _windows(kill))
    monkeypatch.setattr(ir, "_windows_pid_alive", boom)

    assert _default_pid_alive(4321) is None


# --------------------------------------------------------------------------
# 安全：整个模块的用例都不许结束任何真实进程
# --------------------------------------------------------------------------


def test_no_test_in_this_module_touches_a_real_process(monkeypatch):
    """自证：本模块的探测**全部**在受控替身下完成，且平台分支只用信号 0。

    原来的 `assert os.getpid() > 4` 是一条**环境假设**：核查环境里 pytest 进程
    PID = 2，这条断言因此失败（187 passed / 1 failed），而它断言的并不是产品行为
    —— POSIX 上 1/2/3/4 都是合法 PID，低 PID 必须被正常探测（B02 的冻结契约）。

    改成两条与 PID 大小无关的自证：

    1. POSIX 分支：低 PID 逐个探测，且**只**用 `kill(pid, 0)`（纯存在性检查，
       不投递任何会结束进程的信号）；
    2. Windows 分支：绝不落到 `os.kill`，只走只读 `OpenProcess` 替身。
    """
    seen: list[tuple[int, int]] = []

    def kill(pid: int, sig: int):  # noqa: ANN202
        seen.append((pid, sig))
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(ir, "os", _posix(kill))
    for pid in (1, 2, 3, 4):
        _default_pid_alive(pid)
    assert seen == [(1, 0), (2, 0), (3, 0), (4, 0)], (
        "POSIX 低 PID 必须逐个用信号 0 探测（不假设测试进程 PID 的大小）"
    )

    def kill_win(pid: int, sig: int):  # noqa: ANN202
        raise AssertionError("Windows 分支绝不能走 os.kill")

    monkeypatch.setattr(ir, "os", _windows(kill_win))
    monkeypatch.setattr(ir, "_windows_pid_alive", lambda pid: None)
    for pid in (1, 2, 3, 4, 5):
        _default_pid_alive(pid)
    assert callable(_default_pid_alive)


@pytest.mark.parametrize("pid", [1, 2, 3, 4])
def test_posix_low_pid_matrix_is_explicit(monkeypatch, pid):
    """把 1/2/3/4 逐个参数化，失败时能直接看出是哪一个 PID 没被探测。"""
    seen: list[int] = []

    def kill(value: int, sig: int):  # noqa: ANN202
        seen.append(value)
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(ir, "os", _posix(kill))

    assert _default_pid_alive(pid) is False
    assert seen == [pid]
