"""B02 受控验收：PID 存活探测先分平台，再判特殊 PID。

基线反例（在本 worktree 基线实测，见回报里的原始输出）：
POSIX 上 `_default_pid_alive(1/2/3/4)` 全部返回 None、`os.kill` 一次都没被调用 ——
无条件 `pid <= 4 → None` 把 POSIX 的合法低 PID 当成「Windows 内核伪 pid」，
于是容器里恰好是 1 号进程的归属者永远判不出来，它的记录永远无法恢复。

这里全部用**受控探测替身**：替换 `instance_registry.os` 或 `_windows_pid_alive`，
绝不结束、也不探测任何真实进程。
"""

from __future__ import annotations

import errno
from datetime import datetime, timedelta, timezone

import pytest

from agent.storage import instance_registry as ir

from test_fu_w1_support import HOST, add_instance, migrated_conn


class _FakeOS:
    """替身：只记下 `os.kill(pid, 0)` 的调用，不碰真实进程。"""

    def __init__(self, name: str, errors: dict[int, BaseException] | None = None) -> None:
        self.name = name
        self.calls: list[tuple[int, int]] = []
        self._errors = dict(errors or {})

    def kill(self, pid: int, sig: int) -> None:
        self.calls.append((int(pid), int(sig)))
        error = self._errors.get(int(pid))
        if error is not None:
            raise error


@pytest.fixture()
def posix_os(monkeypatch):
    fake = _FakeOS("posix")
    monkeypatch.setattr(ir, "os", fake)
    return fake


@pytest.fixture()
def windows_probe(monkeypatch):
    """Windows 分支：os.name = "nt"，OpenProcess 探测换成受控替身。"""
    fake = _FakeOS("nt")
    monkeypatch.setattr(ir, "os", fake)
    answers: dict[int, bool | None] = {}
    calls: list[int] = []

    def _probe(pid: int) -> bool | None:
        calls.append(int(pid))
        return answers.get(int(pid), True)

    monkeypatch.setattr(ir, "_windows_pid_alive", _probe)
    return {"os": fake, "answers": answers, "calls": calls}


# -- POSIX：低 PID 是合法 PID，必须真的探测 ---------------------------------


@pytest.mark.parametrize("pid", [1, 2, 3, 4])
def test_posix_low_pids_are_actually_probed(posix_os, pid):
    assert ir._default_pid_alive(pid) is True, (
        f"POSIX 上 pid={pid} 是合法 PID，必须正常探测（不能直接判 unknown）"
    )
    assert posix_os.calls == [(pid, 0)], f"必须调用 os.kill(pid, 0)；实际 {posix_os.calls}"


def test_posix_non_positive_is_unknown_without_probing(posix_os):
    for pid in (0, -1, -999):
        assert ir._default_pid_alive(pid) is None
    assert posix_os.calls == [], "0 / 负数没有意义，不该去探测"


def test_posix_missing_and_denied_and_other_errors(posix_os):
    posix_os._errors = {
        4242: ProcessLookupError(errno.ESRCH, "no such process"),
        5353: PermissionError(errno.EPERM, "operation not permitted"),
        6464: OSError(errno.EIO, "i/o error"),
    }
    assert ir._default_pid_alive(4242) is False, "pid 不存在 → 死"
    assert ir._default_pid_alive(5353) is True, "权限不足说明进程存在 → 活"
    assert ir._default_pid_alive(6464) is None, "其它意外 → unknown（绝不当成死）"
    assert posix_os.calls == [(4242, 0), (5353, 0), (6464, 0)]


def test_non_numeric_pid_is_unknown(posix_os):
    assert ir._default_pid_alive("not-a-pid") is None  # type: ignore[arg-type]
    assert posix_os.calls == []


# -- Windows：0/4 是内核伪 pid，保持不探测，且只用只读 OpenProcess ----------


def test_windows_special_pids_stay_unknown_and_never_probe(windows_probe):
    for pid in (1, 2, 3, 4):
        assert ir._default_pid_alive(pid) is None
    assert windows_probe["calls"] == [], "Windows 的 0/4 一类伪 pid 不该去 OpenProcess"
    assert windows_probe["os"].calls == [], "Windows 分支绝不能调用 os.kill"


def test_windows_normal_pid_uses_the_read_only_probe(windows_probe):
    windows_probe["answers"][1234] = True
    windows_probe["answers"][5678] = False
    windows_probe["answers"][9012] = None
    assert ir._default_pid_alive(1234) is True
    assert ir._default_pid_alive(5678) is False
    assert ir._default_pid_alive(9012) is None
    assert windows_probe["calls"] == [1234, 5678, 9012]
    assert windows_probe["os"].calls == []


def test_windows_zero_is_unknown_without_probe(windows_probe):
    assert ir._default_pid_alive(0) is None
    assert windows_probe["calls"] == []


# -- 真实后果：心跳过期 + POSIX 低 pid 的归属者能被判定为「活」 -------------


def test_expired_heartbeat_with_posix_low_pid_resolves_to_alive(tmp_path, monkeypatch):
    """归属者是容器里的 1 号进程：心跳过期，但 pid 探测说它在 → 判定必须成功。"""
    conn = migrated_conn(tmp_path)
    fake = _FakeOS("posix")
    monkeypatch.setattr(ir, "os", fake)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    add_instance(conn, "container_init", pid=1, host=HOST)
    # 把心跳改成「很久以前」：心跳过期，于是必须靠 pid 探测下结论。
    conn.execute(
        "UPDATE instances SET last_heartbeat = ? WHERE instance_id = ?",
        ((base - timedelta(seconds=600)).isoformat(), "container_init"),
    )
    registry = ir.InstanceRegistry(
        conn, "me", pid=4242, host=HOST, clock=lambda: base
    )
    assert registry.owner_alive("container_init") is True, (
        "POSIX 低 PID 的心跳过期归属者必须能判定为活（基线会返回 None）"
    )
    assert fake.calls == [(1, 0)]
    conn.close()
