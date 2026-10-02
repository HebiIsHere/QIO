"""工具子进程隔离：真实强制的证据 + 诚实的边界（见 tools/isolation.py）。

这个文件回答两个问题，且只用**真实执行**回答：

1. 工具进程真的被强制了什么？（Job Object 的内存/活动进程上限、关句柄即收整棵树；
   Windows MIC 低完整性降级 —— 父进程侧读回子进程的完整性级别作为证据）
2. 它**没有**被强制什么？（读没有隔离、路径猜得到就能访问、网络完全没有隔离）

平台说明：Job Object 与 MIC 是 Windows 内核能力；POSIX 上本模块明确返回 unsupported，
行为与改动前完全一致（容器隔离由 executor=docker 负责）。这里没有用平台特判去掩盖失败：
每个 Windows 用例都是真跑内核 API，POSIX 用例断言的就是「不声称有隔离」。

默认值（2026-10-02 起）：**Job Object 默认生效；低完整性降级默认关闭**
（QIO_TOOL_LOW_INTEGRITY=1 打开）。原因见 docs/security/tool-execution-isolation.md：
CI（windows-latest）实测降级一旦生效，工具连自己的 scratch 与 mock 夹具都写不进去 ——
标签没有可核实的落地。打开时也会先**读回核实**标签，核实不了就跳过降级（fail-safe）。
本文件对两种状态都有断言：关着时断言「诚实的未生效 + 原因」，打开时断言真实边界。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from agent.tools import isolation
from agent.tools.sandbox import SandboxExecutor

WINDOWS = sys.platform == 'win32'
WINDOWS_ONLY = pytest.mark.skipif(not WINDOWS, reason='Windows 内核能力（Job Object / MIC）')


def _enable_low_integrity(monkeypatch) -> None:
    """显式打开低完整性降级（默认是关的）。"""
    monkeypatch.setenv(isolation.LOW_INTEGRITY_ENV, '1')


async def _run(code: str, arguments: dict | None = None, *, timeout_seconds: float | None = None):
    sandbox = (
        SandboxExecutor(executor='subprocess', timeout_seconds=timeout_seconds)
        if timeout_seconds
        else SandboxExecutor(executor='subprocess')
    )
    return await sandbox.execute(code, arguments or {})


# ---------------------------------------------------------------- 强制生效的证据


async def test_the_isolation_hook_never_breaks_a_tool_call():
    """无论平台：钩子跑过之后工具照常执行，结果里带上「实际拿到了什么」。"""
    result = await _run("def run(**kwargs):\n    return {'ok': 1}\n")

    assert result.ok is True, result.error
    assert json.loads(result.value.__str__().replace("'", '"'))['ok'] == 1
    assert result.isolation is not None
    if WINDOWS:
        assert result.isolation['applied'] is True
        assert 'job_object' in result.isolation['mechanisms']
    else:
        # POSIX：明确不声称有强制隔离，行为与改动前一致
        assert result.isolation['applied'] is False
        assert 'POSIX' in result.isolation['detail']


@WINDOWS_ONLY
async def test_low_integrity_is_off_by_default_and_that_is_reported(monkeypatch):
    """默认关闭时：降级不生效，但必须在结果里说清「没生效 + 怎么开」。"""
    monkeypatch.delenv(isolation.LOW_INTEGRITY_ENV, raising=False)
    observed: dict[str, str] = {}
    original = isolation.harden

    def spy(process, **kwargs):
        outcome = original(process, **kwargs)
        observed['integrity'] = isolation.integrity_of_process(process.pid)
        return outcome

    monkeypatch.setattr(isolation, 'harden', spy)

    result = await _run("def run(**kwargs):\n    return {'ok': 1}\n")

    assert result.ok is True, result.error
    assert isolation.low_integrity_enabled() is False
    info = result.isolation or {}
    assert 'low_integrity' not in info.get('mechanisms', [])
    assert any(isolation.LOW_INTEGRITY_ENV in problem for problem in info.get('problems', []))
    # Job Object 仍然是默认生效的那一层
    assert 'job_object' in info.get('mechanisms', [])
    assert observed['integrity'], '读不到子进程完整性级别，说明进程没跑起来'


@WINDOWS_ONLY
async def test_enabling_low_integrity_downgrades_the_child(monkeypatch):
    """打开开关：要么真的降到 Low（父进程侧读回核实），要么按 fail-safe 明确跳过并给出原因。"""
    _enable_low_integrity(monkeypatch)
    observed: dict[str, str] = {}
    original = isolation.harden

    def spy(process, **kwargs):
        outcome = original(process, **kwargs)
        observed['integrity'] = isolation.integrity_of_process(process.pid)
        return outcome

    monkeypatch.setattr(isolation, 'harden', spy)

    result = await _run("def run(**kwargs):\n    return {'ok': 1}\n")

    assert result.ok is True, result.error
    info = result.isolation or {}
    if 'low_integrity' in info.get('mechanisms', []):
        assert observed['integrity'] == isolation.LOW_INTEGRITY_SID
    else:
        # fail-safe 分支：标签核实不了就不降级；必须留下可诊断的原因，不能悄悄放过
        assert any('低完整性' in problem for problem in info.get('problems', [])), info


@WINDOWS_ONLY
async def test_job_object_limits_are_reported_and_attached_to_the_result():
    result = await _run("def run(**kwargs):\n    return {'ok': 1}\n")

    info = result.isolation or {}
    assert info['applied'] is True
    limits = info['limits']
    assert limits['process_memory_bytes'] == isolation.JOB_PROCESS_MEMORY_BYTES
    assert limits['active_processes'] == isolation.JOB_ACTIVE_PROCESS_LIMIT
    assert limits['kill_on_close'] is True


@WINDOWS_ONLY
async def test_memory_limit_is_enforced_by_the_kernel():
    """工具想分配 1.5 GiB，而上限是 1 GiB：应当是失败，而不是把整机吃光。"""
    code = (
        'def run(**kwargs):\n'
        '    blocks = []\n'
        '    for _ in range(24):\n'
        '        blocks.append(bytearray(64 * 1024 * 1024))\n'
        "    return {'blocks': len(blocks)}\n"
    )

    result = await _run(code)

    assert result.ok is False
    detail = (result.error or '') + result.diagnostic() + (result.stderr or '')
    assert 'MemoryError' in detail, detail


@WINDOWS_ONLY
async def test_active_process_limit_is_enforced_by_the_kernel():
    """工具想拉起 40 个常驻子进程，上限是 32：超出的被内核以 WinError 1816 拒绝。

    注意 .venv 的 python.exe 是 uv 的 trampoline（它自己还要再起一个真解释器），
    所以「启动被配额拒绝」既可能直接抛 OSError，也可能让 trampoline 自己以 1 退出；
    两种都算配额生效 —— 断言用 winerror 与并发存活数，不依赖本地化文案。
    """
    # 用 ping.exe 当「常驻子进程」而不是 sys.executable：.venv 的 python.exe 是 uv 的
    # trampoline（它自己还要再起一个真解释器），进程计数与失败形态都不直观；
    # ping 一个进程就是一个进程，配额拒绝会直接抛 OSError(winerror=1816)。
    code = """
import subprocess, time

def run(**kwargs):
    started = 0
    blocked = 0
    win_errors = []
    procs = []
    for _ in range(40):
        try:
            procs.append(subprocess.Popen(
                ["ping", "-n", "30", "127.0.0.1"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
            started += 1
        except OSError as exc:
            blocked += 1
            win_errors.append(getattr(exc, "winerror", None))
    time.sleep(1.0)
    alive = sum(1 for p in procs if p.poll() is None)
    return {
        "started": started,
        "blocked": blocked,
        "alive": alive,
        "win_errors": [e for e in win_errors if e],
    }
"""

    # 40 次解释器启动本身就超过默认 10s 超时：这里给足时间，测的是内核配额而不是速度。
    result = await _run(code, timeout_seconds=180)

    assert result.ok is True, result.error
    assert result.value['blocked'] > 0, '内核没有拦住超额进程'
    assert 1816 in result.value['win_errors'], result.value['win_errors']  # ERROR_NOT_ENOUGH_QUOTA
    # 真正被保护的量是**并发**：存活数不许超过上限
    assert result.value['alive'] <= isolation.JOB_ACTIVE_PROCESS_LIMIT


@WINDOWS_ONLY
def test_closing_the_job_handle_reaps_descendants_created_after_assignment():
    """关句柄 = 整棵树被内核收掉（不依赖 taskkill 有没有跑成功）。

    用真实的执行时序：先指派 job，再让子进程**根据 stdin 指令**拉起孙进程 ——
    与 sandbox 一样（worker 在读到请求之前不会执行工具代码，而请求是在 harden 之后才写的）。
    """
    script = (
        'import subprocess, sys, time\n'
        'sys.stdin.readline()\n'
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "print(child.pid, flush=True)\n"
        'time.sleep(300)\n'
    )
    worker = subprocess.Popen(
        [sys.executable, '-c', script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        outcome = isolation.harden(worker, scratch_dir=None, policy=None)
        assert outcome.applied is True

        worker.stdin.write('go\n')
        worker.stdin.flush()
        grandchild = int(worker.stdout.readline().strip())
        assert _pid_alive(worker.pid) and _pid_alive(grandchild)

        isolation.release(worker)
        _wait_until_dead(worker.pid)
        _wait_until_dead(grandchild)

        assert not _pid_alive(worker.pid)
        assert not _pid_alive(grandchild)
    finally:
        with contextlib_suppress():
            worker.kill()
        with contextlib_suppress():
            worker.wait(timeout=10)
        isolation.release(worker)


@WINDOWS_ONLY
def test_a_child_created_before_assignment_is_not_contained():
    """诚实边界（不夸大）：job 只包含**指派之后**创建的后代。

    在指派之前就已经存在的孙进程不在 job 里，关句柄也收不掉它。真实工具执行里
    不会出现这个窗口：worker 读到请求之前不执行工具代码，而请求是在 harden 之后才写的
    （见上一条用例的时序）。但作为通用 API，isolation.harden() 的保证就是这个范围。
    """
    script = (
        'import subprocess, sys, time\n'
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "print(child.pid, flush=True)\n"
        'time.sleep(300)\n'
    )
    worker = subprocess.Popen(
        [sys.executable, '-c', script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    early_child = int(worker.stdout.readline().strip())
    try:
        outcome = isolation.harden(worker, scratch_dir=None, policy=None)
        assert outcome.applied is True

        isolation.release(worker)
        _wait_until_dead(worker.pid)

        assert not _pid_alive(worker.pid)
        assert _pid_alive(early_child), '这一条锁住的是「指派前的后代不在 job 里」这个事实'
    finally:
        with contextlib_suppress():
            subprocess.run(
                ['taskkill', '/PID', str(early_child), '/T', '/F'],
                capture_output=True, timeout=15,
            )
        with contextlib_suppress():
            worker.kill()
        with contextlib_suppress():
            worker.wait(timeout=10)
        isolation.release(worker)


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(Exception)


def _pid_alive(pid: int) -> bool:
    """进程还活着吗？用内核 API 判定 —— tasklist 的文本匹配会误命中内存列的数字。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _wait_until_dead(pid: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.2)


# ---------------------------------------------------------------- 诚实的边界


async def test_tool_gets_no_pointer_to_the_qio_data_directory(monkeypatch):
    """filesystem staging 渗透测试：工具主动去找 QIO 数据目录，找不到任何指针。

    检查的是「有没有人把路径递给它」：环境变量 / cwd / sys.path / argv。
    这不是「访问不到」—— 见下面那条诚实边界用例。
    """
    data_dir = os.path.abspath(os.path.join(os.getcwd(), 'qio-data-probe'))
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setenv('QIO_DATA_DIR', data_dir)

    code = (
        'import os, sys\n'
        'def run(**kwargs):\n'
        '    return {\n'
        "        'env_values': list(os.environ.values()),\n"
        "        'env_keys': sorted(os.environ.keys()),\n"
        "        'cwd': os.getcwd(),\n"
        "        'sys_path': list(sys.path),\n"
        "        'argv': list(sys.argv),\n"
        '    }\n'
    )
    result = await _run(code)

    assert result.ok is True, result.error
    value = result.value
    needles = (data_dir, os.path.basename(data_dir))
    assert 'QIO_DATA_DIR' not in value['env_keys']
    assert not any(any(n in v for n in needles) for v in value['env_values'])
    assert not any(n in value['cwd'] for n in needles)
    assert not any(any(n in p for n in needles) for p in value['sys_path'])
    assert not any(any(n in a for n in needles) for a in value['argv'])


async def test_reads_are_not_isolated_that_is_the_honest_boundary(tmp_path):
    """诚实边界：低完整性只挡写。工具知道路径就仍然能读 —— 指针没泄露 ≠ 访问不到。"""
    secretish = tmp_path / 'outside-scratch.txt'
    secretish.write_text('outside content', encoding='utf-8')

    code = (
        'def run(**kwargs):\n'
        '    try:\n'
        "        with open(kwargs['path'], encoding='utf-8') as fh:\n"
        '            text = fh.read()\n'
        "        return {'read': text}\n"
        '    except OSError as exc:\n'
        "        return {'read': 'DENIED ' + type(exc).__name__}\n"
    )
    result = await _run(code, {'path': str(secretish)})

    assert result.ok is True, result.error
    assert result.value['read'] == 'outside content'


@WINDOWS_ONLY
async def test_low_integrity_keeps_the_scratch_writable(tmp_path, monkeypatch):
    """打开降级时的硬契约：工具**必须**还能写自己的一次性 scratch 目录。

    这一条是 CI 上真出过的事故：标签没落地，工具连 scratch 都写不进去，
    mock 服务报告、进程树标记文件全写不出来。无论降级是否真的生效，这条都必须成立。
    """
    _enable_low_integrity(monkeypatch)
    outside = tmp_path / 'outside'
    outside.mkdir()

    code = (
        'import os\n'
        'def _touch(path):\n'
        '    try:\n'
        "        with open(os.path.join(path, 'probe.txt'), 'w') as fh:\n"
        "            fh.write('x')\n"
        "        return 'WRITE-OK'\n"
        '    except OSError:\n'
        "        return 'WRITE-DENIED'\n"
        'def run(**kwargs):\n'
        "    return {'scratch': _touch(os.getcwd()), 'outside': _touch(kwargs['outside'])}\n"
    )
    result = await _run(code, {'outside': str(outside)})

    assert result.ok is True, result.error
    assert result.value['scratch'] == 'WRITE-OK', result.isolation

    # 外面那个目录的标签高于 Low 时（Medium 会话/CI），写必须被内核拒绝；
    # 本会话自身就在 Low 完整性（父进程造不出更高标签的对象）时，它也是 Low，写得进去才是对的。
    label = isolation.integrity_label_of(str(outside))
    if isolation.label_is_low(label) or not label:
        assert result.value['outside'] == 'WRITE-OK'
    else:
        assert result.value['outside'] == 'WRITE-DENIED'
        assert not (outside / 'probe.txt').exists()


@WINDOWS_ONLY
async def test_extra_writable_dirs_are_kept_writable_when_declared(tmp_path, monkeypatch):
    """调用方声明「工具合法需要写」的目录（例如 mock 夹具目录）时，它同样必须可写。"""
    _enable_low_integrity(monkeypatch)
    declared = tmp_path / 'declared'
    declared.mkdir()

    code = (
        'import os\n'
        'def run(**kwargs):\n'
        '    try:\n'
        "        with open(os.path.join(kwargs['path'], 'report.json'), 'w') as fh:\n"
        "            fh.write('{}')\n"
        "        return {'declared': 'WRITE-OK'}\n"
        '    except OSError:\n'
        "        return {'declared': 'WRITE-DENIED'}\n"
    )
    sandbox = SandboxExecutor(executor='subprocess')
    result = await sandbox.execute(
        code, {'path': str(declared)}, extra_writable_dirs=[str(declared)]
    )

    assert result.ok is True, result.error
    assert result.value['declared'] == 'WRITE-OK', result.isolation


# ---------------------------------------------------------------- 标签与声明必须与事实一致（C5）


@WINDOWS_ONLY
def test_label_is_low_only_accepts_a_real_label_ace():
    """实测过的两个读回值：一个是真标签，一个是「调用成功但没落上」的坏结果。"""
    # icacls 落上的正确结果（实测读回）
    assert isolation.label_is_low('S:AI(ML;OICI;NW;;;LW)') is True
    # SetNamedSecurityInfoW 返回 0 时的读回（根本没有标签 ACE）—— 必须判为否
    assert isolation.label_is_low('S:AINO_ACCESS_CONTROL') is False
    assert isolation.label_is_low('') is False
    assert isolation.label_is_low('S:AI(ML;OICI;NW;;;ME)') is False  # 中完整性不是低


@WINDOWS_ONLY
def test_label_low_refuses_when_the_readback_does_not_show_low(tmp_path, monkeypatch):
    """打标签必须读回核实：读回不是低标签 → 返回空串（调用方据此不降级）。"""
    target = tmp_path / 'labelled'
    target.mkdir()

    monkeypatch.setattr(isolation, 'integrity_label_of', lambda path: 'S:AINO_ACCESS_CONTROL')

    assert isolation.label_low(str(target)) == '', '读回不是低标签时必须拒绝'


@WINDOWS_ONLY
def test_low_integrity_is_not_claimed_when_the_token_did_not_downgrade(monkeypatch):
    """C5：SetTokenInformation 成功但令牌没变时，不许声明 low_integrity。"""
    _enable_low_integrity(monkeypatch)
    process = _spawn_sleeper()
    try:
        # 令牌读回永远是中完整性 —— 模拟「调用了但没生效」
        monkeypatch.setattr(
            isolation, 'integrity_of_process', lambda pid: isolation.MEDIUM_INTEGRITY_SID
        )
        outcome = isolation.harden(process, scratch_dir=None, policy=None)

        assert 'low_integrity' not in outcome.mechanisms
        assert any('读回' in problem for problem in outcome.problems), outcome.problems
    finally:
        isolation.release(process)
        _kill(process)


@WINDOWS_ONLY
def test_job_object_is_not_claimed_when_the_assignment_fails(monkeypatch):
    """C5：指派失败时不许声明 job_object（声明 == 事实）。"""
    process = _spawn_sleeper()
    try:
        def boom(job, pid):
            raise OSError('injected: assign failed')

        monkeypatch.setattr(isolation, '_assign_job', boom)
        outcome = isolation.harden(process, scratch_dir=None, policy=None)

        assert 'job_object' not in outcome.mechanisms
        assert any('job_object' in problem for problem in outcome.problems), outcome.problems
        assert outcome.limits == {}
    finally:
        isolation.release(process)
        _kill(process)


def _spawn_sleeper():
    return subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(60)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _kill(process) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        process.kill()
    with contextlib.suppress(Exception):
        process.wait(timeout=10)


@WINDOWS_ONLY
async def test_the_declared_triple_holds_with_a_non_low_parent(tmp_path, monkeypatch):
    """C3 目标三件套：scratch 可写 / 用户目录不可写 / QIO 数据目录不可写。

    只有在父进程**高于 Low** 时才可能观察到「拒绝」：本机 `uv run` 出来的 python 自己在 Low
    完整性，降级是 no-op，目标目录也是 Low —— 那时写被允许才是正确行为，这时本条**无法验证拒绝**，
    只断言「状态与标签一致」并把事实打印出来。CI（windows-latest）是普通完整性，会走完整断言。
    """
    _enable_low_integrity(monkeypatch)
    base = tmp_path / 'c3'
    scratch = base / 'scratch'
    user_dir = base / 'user-files'
    qio_dir = base / 'qio-data'
    for path in (scratch, user_dir, qio_dir):
        path.mkdir(parents=True)

    code = (
        'import os\n'
        'def _touch(path):\n'
        '    try:\n'
        "        with open(os.path.join(path, 'probe.txt'), 'w') as fh:\n"
        "            fh.write('x')\n"
        "        return 'WRITE-OK'\n"
        '    except OSError as exc:\n'
        "        return 'WRITE-DENIED ' + type(exc).__name__\n"
        'def run(**kwargs):\n'
        '    return {\n'
        "        'scratch': _touch(os.getcwd()),\n"
        "        'user': _touch(kwargs['user']),\n"
        "        'qio': _touch(kwargs['qio']),\n"
        '    }\n'
    )
    sandbox = SandboxExecutor(executor='subprocess')
    result = await sandbox.execute(
        code, {'user': str(user_dir), 'qio': str(qio_dir)}
    )

    assert result.ok is True, result.error
    # 硬契约：工具自己的 scratch 永远必须可写
    assert result.value['scratch'] == 'WRITE-OK', result.isolation

    parent_integrity = isolation.integrity_of_process(os.getpid())
    user_label = isolation.integrity_label_of(str(user_dir))
    print(
        '[C3] parent=' + str(parent_integrity),
        'user_label=' + repr(user_label),
        'isolation=' + str(result.isolation),
    )
    if parent_integrity == isolation.LOW_INTEGRITY_SID or isolation.label_is_low(user_label):
        # 本会话自己就是 Low：这里无法验证「拒绝」，如实断言不拒绝
        assert result.value['user'] == 'WRITE-OK'
        assert result.value['qio'] == 'WRITE-OK'
    else:
        assert result.value['user'] == 'WRITE-DENIED', result.isolation
        assert result.value['qio'] == 'WRITE-DENIED', result.isolation
        assert not (user_dir / 'probe.txt').exists()
        assert not (qio_dir / 'probe.txt').exists()
