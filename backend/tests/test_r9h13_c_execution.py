"""R9-H13 C：读取先行 / 内容绑定 / 参数脚本 / 递归删除 / 链接语义（契约 §3.C/§3.D/§3.G）。

真实流程边界（契约 §5）：
* 真实：AuthorizationEngine + 要求台账（SQLite）、真实工具（run_program/run_shell/
  fs_delete）、真实临时文件、真实解释器/系统 shell 启动（用 marker 文件证明真的执行了）、
  真实文件身份（volume+file_id+birth）/stat 元数据、真实 mtime 操作（os.utime）。
* 模型：本文件不调用任何模型（unrestricted 模式，普通操作不进入模型审查）。
* 模拟并显式标注：
  1) 「审查/授权等待窗口」= 在 security_gate.authorize 返回前后注入一次内容改写
     （保持文件大小与 mtime_ns 不变）。这正是契约里“模型审查期间内容变化”的时间窗口，
     判定仍由真实工具/真实启动链路完成。
  2) §10 的回收站为**测试替身**（monkeypatch agent.services.recycle_bin.send_to_recycle_bin）：
     替身记录真实收到的路径，按可恢复语义把“用户指定的目录项”移动到隔离目录；
     它不是操作系统回收站的真实复现，只用于证明工具把哪个目录项交给回收入口。
  3) Windows 本机无 SeCreateSymbolicLinkPrivilege（文件符号链接 WinError 1314）：
     目录链接用真实 junction（mklink /J）覆盖；文件符号链接在支持的平台（POSIX/CI）
     走同一 helper 的真实 os.symlink 分支。测试不 skip，按平台能力选择真实链接类型。
* 只读断言、源码字符串检查均不使用；每个拒绝用例都有相邻的正常放行用例。

基线（e68fb9b7）上本文件为红：C1 在最终拒绝前已读取禁止文件内容；D1 摘要缓存被
同大小同 mtime 的内容变化骗过；D2 python xxx.txt 未入绑定、参数脚本超过采集上限被
静默遗漏；D3 递归删除命令只带目录对象、漏掉被禁止子文件；§10 回收符号链接先解析
成原文件、最终移动原文件而链接留在原处。
"""

from __future__ import annotations

import os
import stat as stat_module
import subprocess
import sys
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import launch as launch_boundary
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.services import recycle_bin
from agent.tools import fs_tools, security_gate
from agent.tools.cmd_tools import RunCmdTool, RunProgramTool
from agent.tools.fs_tools import FsDeleteTool

INTERPRETER = sys.executable
WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------
# 真实上下文 / 要求台账
# --------------------------------------------------------------------------


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


def _unrestricted(ctx) -> None:
    ctx.settings_store.set("computer.permission_mode", "unrestricted")


def _save_limit(ctx, *, text: str, operations, objects, tag: str):
    """真实要求台账 + 结构化限制保存（与 H12 同一真实入口）。"""
    requirement = ctx.requirements.record_user_message(
        text=text, message_id="m-r9h13c-" + tag, turn_id="t-r9h13c-" + tag
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [
            {
                "operations": [str(item) for item in operations],
                "objects": [dict(item) for item in objects],
                "reason": text,
            }
        ],
        source="test-r9h13-c:" + tag,
        state=requirement_limits.STATE_STRUCTURED,
    )
    return ctx.requirements.binding_for(requirement.requirement_id)


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _benign_binding(ctx, tag: str):
    """真实用户要求（无限制）绑定：执行前重核对需要一条仍有效的具体要求。"""
    requirement = ctx.requirements.record_user_message(
        text="回收我指定的这个链接目录项", message_id="m-r9h13c-benign-" + tag,
        turn_id="t-r9h13c-benign-" + tag,
    )
    return ctx.requirements.binding_for(requirement.requirement_id)


# --------------------------------------------------------------------------
# 真实读取/启动计数（真实 I/O 仍发生，只记录事实）
# --------------------------------------------------------------------------


class _ReadSpy:
    """正文/摘要读取计数：content_sha256、Path.read_bytes、shebang 2 字节探测。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def install(self, monkeypatch) -> "_ReadSpy":
        real_sha = security_gate.content_sha256
        real_shebang = launch_boundary._has_script_shebang
        real_read_bytes = Path.read_bytes

        def sha(path):
            self.calls.append(("sha256", str(path)))
            return real_sha(path)

        def shebang(path):
            self.calls.append(("shebang", str(path)))
            return real_shebang(path)

        def read_bytes(self_path, *args, **kwargs):
            self.calls.append(("read_bytes", str(self_path)))
            return real_read_bytes(self_path, *args, **kwargs)

        monkeypatch.setattr(security_gate, "content_sha256", sha)
        monkeypatch.setattr(launch_boundary, "_has_script_shebang", shebang)
        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        return self

    def paths(self) -> list[str]:
        return [path for _, path in self.calls]


def _install_launch_counter(monkeypatch) -> list[dict]:
    """真实启动计数：包装受控启动器（真的会启动；只记录 launch 尝试）。"""
    calls: list[dict] = []
    real_launch = launch_boundary.ControlledLauncher.launch

    async def spy(self, *args, **kwargs):
        spec = args[0] if args else kwargs.get("spec")
        calls.append(
            {
                "program": str(getattr(spec, "program", "")),
                "argv": [str(item) for item in tuple(getattr(spec, "argv", ()) or ())],
            }
        )
        return await real_launch(self, *args, **kwargs)

    monkeypatch.setattr(launch_boundary.ControlledLauncher, "launch", spy)
    return calls


def _mutate_during_authorize(monkeypatch, mutate) -> dict:
    """审查/授权窗口注入：真实 authorize 返回后、执行前改写一次内容（只注入时间窗口）。"""
    real = security_gate.authorize
    state = {"done": False}

    async def spy(gateway, effect, *, ctx=None):
        decision = await real(gateway, effect, ctx=ctx)
        try:
            kind = str(getattr(getattr(effect, "kind", ""), "value", effect.kind))
        except Exception:  # noqa: BLE001 - 只做记录
            kind = ""
        if not state["done"] and kind == "execute":
            state["done"] = True
            mutate()
        return decision

    monkeypatch.setattr(security_gate, "authorize", spy)
    return state


def _rewrite_same_size_same_mtime(path: Path, new_text: str) -> None:
    """保持文件大小与 mtime_ns 不变地改写内容（模型审查期间的可疑变化）。"""
    before = path.stat()
    payload = new_text.encode("utf-8")
    assert len(payload) == before.st_size, "测试前提：改写后大小必须完全相同"
    path.write_bytes(payload)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def _python_script(marker: Path, word: str) -> str:
    return "from pathlib import Path\nPath({!r}).write_text({!r})\n".format(
        str(marker), word
    )


def _marker_text(marker: Path) -> str:
    return marker.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------
# 真实可执行程序副本（拒绝用例旁边必须有“本可启动”的控制组）
# --------------------------------------------------------------------------

_PROGRAM_SOURCE = (
    os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    if WINDOWS
    else (os.environ.get("SHELL") or "/bin/sh")
)


def _program_copy(directory: Path, name: str = "runner-prog") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS and not name.lower().endswith((".exe", ".cmd", ".bat")):
        name += ".exe"
    target = directory / name
    import shutil

    shutil.copy2(_PROGRAM_SOURCE, target)
    if not WINDOWS:
        target.chmod(0o755)
    return target


def _program_echo_args(marker: Path) -> list[str]:
    if WINDOWS:
        return ["/c", "echo ran>" + str(marker)]
    return ["-c", "printf ran > " + str(marker)]


# ==========================================================================
# C1：最终拒绝执行前不得读取被禁止文件来计算摘要（拒绝时读取次数 0、启动 0 次）
# ==========================================================================


async def test_c1_denied_program_not_read_and_not_launched(
    ctx, tmp_path, workspace, monkeypatch
):
    """禁止读取程序文件 → run_program 必须在内容读取/启动之前拒绝。

    * 读取禁止是用户明确限制（operations=["read"]），程序本身真实可执行；
    * 断言：结果拒绝、content_sha256/正文读取次数为 0、程序 0 次启动、marker 不存在；
    * 相邻控制组：同一程序在没有该限制时真的启动并写 marker（证明不是“程序坏了”）。
    """
    _unrestricted(ctx)
    program = _program_copy(tmp_path / "c1-bin")
    marker = tmp_path / "c1-marker.txt"
    args = _program_echo_args(marker)
    binding = _save_limit(
        ctx,
        text="不要读取这个程序文件",
        operations=["read"],
        objects=[{"kind": "path", "value": str(program)}],
        tag="c1",
    )
    spy = _ReadSpy().install(monkeypatch)
    launches = _install_launch_counter(monkeypatch)
    tool = RunProgramTool(computer=ctx.computer, security=ctx.authorization)

    with requirement_context(binding):
        denied = await tool.run(program=str(program), args=args, timeout=60)

    assert denied.ok is False, ("用户禁止读取该程序时必须拒绝执行", denied)
    assert spy.calls == [], (
        "最终拒绝前不得读取被禁止文件正文/摘要/shebang：" + repr(spy.calls)
    )
    assert not marker.exists(), "拒绝时程序必须 0 次启动（marker 不得出现）"
    assert launches == [], "拒绝时不得进入受控启动"

    # 相邻控制组：没有该限制时同一程序真的会启动。
    control_marker = tmp_path / "c1-control-marker.txt"
    nature = await tool.run(
        program=str(program), args=_program_echo_args(control_marker), timeout=60
    )
    assert nature.ok is True, ("控制组必须真的启动", nature)
    assert control_marker.exists(), "控制组必须留下真实执行痕迹"
    assert len(launches) == 1, launches


# ==========================================================================
# D1：摘要缓存不得掩盖“同大小、同 mtime”的内容变化
# ==========================================================================


async def test_d1_same_size_mtime_change_not_hidden_by_cache(
    ctx, workspace, tmp_path, monkeypatch
):
    """模型审查窗口内改写被执行的脚本（保持大小/mtime）→ 旧摘要不得继续生效。

    * 真实 python 解释器 + 真实脚本文件；marker 证明实际执行的是哪份内容；
    * 注入点：authorize 返回后改写脚本内容（大小与 mtime_ns 不变）；
    * 断言：拒绝执行、marker 不存在；控制组未变化的内容照常执行；
    * 同一份事实生成器（build_launch_facts）在内容变化后必须给出不同 spec_digest。
    """
    _unrestricted(ctx)
    marker = tmp_path / "d1-marker.txt"
    script = workspace / "job.py"
    script.write_bytes(_python_script(marker, "OLD").encode("utf-8"))
    before_facts = launch_boundary.build_launch_facts(
        launch_boundary.LaunchSpec(
            kind="program", program=INTERPRETER, argv=(str(script),), cwd=str(workspace)
        ),
        computer=ctx.computer,
    )

    tool = RunProgramTool(computer=ctx.computer, security=ctx.authorization)
    _mutate_during_authorize(
        monkeypatch,
        lambda: _rewrite_same_size_same_mtime(
            script, _python_script(marker, "NEW")
        ),
    )
    result = await tool.run(
        program=INTERPRETER, args=[str(script)], cwd=str(workspace), timeout=60
    )

    assert result.ok is False, (
        "审查期间脚本内容变化（大小/mtime 相同）必须使旧结论失效",
        result,
    )
    assert not marker.exists(), "不得实际执行变化后的新内容"
    assert "NEW" not in (result.content or "")

    # 共享事实生产者：内容变化后 digest 必须变化（工具/审查/审批恢复同语义）。
    after_facts = launch_boundary.build_launch_facts(
        launch_boundary.LaunchSpec(
            kind="program", program=INTERPRETER, argv=(str(script),), cwd=str(workspace)
        ),
        computer=ctx.computer,
    )
    assert before_facts["spec_digest"] != after_facts["spec_digest"], (
        "缓存不得让内容变化的脚本沿用旧 spec_digest"
    )

    # 相邻控制组：未变化的脚本正常执行。
    control_marker = tmp_path / "d1-control-marker.txt"
    control_script = workspace / "control.py"
    control_script.write_bytes(_python_script(control_marker, "OK").encode("utf-8"))
    control = await tool.run(
        program=INTERPRETER,
        args=[str(control_script)],
        cwd=str(workspace),
        timeout=60,
    )
    assert control.ok is True, ("未变化脚本必须正常执行", control)
    assert _marker_text(control_marker) == "OK"


# ==========================================================================
# D2：参数脚本必须按真实启动语义绑定（python xxx.txt）
# ==========================================================================


async def test_d2_python_txt_script_content_bound(
    ctx, workspace, tmp_path, monkeypatch
):
    """`python script.txt`：入口脚本按“解释器 argv 位置”识别，不能只靠后缀/shebang。

    * 真实解释器 + 无 shebang 的 .txt 脚本（后缀名单与 shebang 都不命中）；
    * 审查窗口注入同大小同 mtime 内容变化 → 必须检测并拒绝；
    * 控制组：未变化时真的执行并留下 marker。
    """
    _unrestricted(ctx)
    marker = tmp_path / "d2-txt-marker.txt"
    script = workspace / "job.txt"
    script.write_bytes(_python_script(marker, "OLD").encode("utf-8"))
    assert not script.read_bytes().startswith(b"#!"), "测试前提：没有 shebang"

    tool = RunProgramTool(computer=ctx.computer, security=ctx.authorization)
    _mutate_during_authorize(
        monkeypatch,
        lambda: _rewrite_same_size_same_mtime(script, _python_script(marker, "NEW")),
    )
    result = await tool.run(
        program=INTERPRETER, args=[str(script)], cwd=str(workspace), timeout=60
    )

    assert result.ok is False, (
        "python 的 .txt 入口脚本内容变化必须进入绑定并拒绝",
        result,
    )
    assert not marker.exists(), "不得实际执行变化后的 .txt 脚本"

    control_marker = tmp_path / "d2-txt-control.txt"
    control_script = workspace / "control.txt"
    control_script.write_bytes(_python_script(control_marker, "OK").encode("utf-8"))
    control = await tool.run(
        program=INTERPRETER,
        args=[str(control_script)],
        cwd=str(workspace),
        timeout=60,
    )
    assert control.ok is True, ("未变化的 .txt 脚本必须能正常执行", control)
    assert _marker_text(control_marker) == "OK"


def _batch_script_text(index: int, word: str) -> str:
    if WINDOWS:
        return "@echo {}>m{}.txt\r\n".format(word, index)
    return "printf {} > m{}.txt\n".format(word, index)


def _multi_script_command(count: int) -> str:
    if WINDOWS:
        return " & ".join("call s%d.bat" % index for index in range(1, count + 1))
    return "; ".join("sh s%d.sh" % index for index in range(1, count + 1))


async def test_d2_many_script_args_not_silently_omitted(
    ctx, workspace, monkeypatch
):
    """参数脚本超过旧采集上限（16）后，后段脚本变化仍必须被绑定/发现。

    * 真实 shell（cmd/sh）依次执行 18 个真实脚本，每个脚本写自己的 marker；
    * 审查窗口内改写第 18 个脚本（同大小同 mtime）→ 必须拒绝执行；
    * 控制组：未变化时 18 个脚本全部真实执行（不能普遍拒绝正常任务）；
    * 事实层：正常数量时 argument_files 必须完整；超上限时明确标记不完整而不是静默截断。
    """
    _unrestricted(ctx)
    count = 18
    for index in range(1, count + 1):
        name = "s%d.bat" % index if WINDOWS else "s%d.sh" % index
        (workspace / name).write_text(
            _batch_script_text(index, "OLD"), encoding="utf-8", newline=""
        )
    cmd = _multi_script_command(count)
    last = workspace / ("s18.bat" if WINDOWS else "s18.sh")

    tool = RunCmdTool(computer=ctx.computer, security=ctx.authorization)
    _mutate_during_authorize(
        monkeypatch,
        lambda: _rewrite_same_size_same_mtime(
            last, _batch_script_text(count, "NEW")
        ),
    )
    result = await tool.run(cmd=cmd, cwd=str(workspace), timeout=120)

    assert result.ok is False, (
        "第 18 个参数脚本变化（旧实现超过采集上限被静默遗漏）必须被发现并拒绝",
        result,
    )
    for index in range(1, count + 1):
        assert not (workspace / ("m%d.txt" % index)).exists(), (
            "检测到脚本变化后不得执行任何脚本（marker %d 出现）" % index
        )

    # 相邻控制组：18 个脚本都未变化时全部真实执行（先复位审查窗口内的改写）。
    last.write_bytes(_batch_script_text(count, "OLD").encode("utf-8"))
    control = await tool.run(cmd=cmd, cwd=str(workspace), timeout=120)
    assert control.ok is True, ("未变化的多脚本命令必须能正常执行", control)
    for index in range(1, count + 1):
        marker_file = workspace / ("m%d.txt" % index)
        assert marker_file.exists(), "控制组脚本 %d 必须真的执行" % index
        assert marker_file.read_text(encoding="utf-8").strip() == "OLD"

    # 事实层：完整采集，而不是“静默截断 16 个后宣称完整”。
    spec = launch_boundary.LaunchSpec(
        kind="shell", shell_cmd=cmd, cwd=str(workspace)
    )
    facts = launch_boundary.build_spec_facts(spec, computer=ctx.computer)
    assert facts.get("argument_files_complete") is True, facts
    assert len(facts.get("argument_files") or ()) == count, (
        "正常数量的参数脚本必须全部进入内容绑定",
        [item.get("path") for item in facts.get("argument_files") or ()],
    )

    # 超上限：必须显式标记“不完整/未覆盖”，不能静默丢弃后仍称完整。
    over = launch_boundary.LaunchSpec(
        kind="program",
        program=INTERPRETER,
        argv=tuple("missing_%03d.py" % index for index in range(1, 201)),
        cwd=str(workspace),
    )
    over_facts = launch_boundary.build_spec_facts(over, computer=ctx.computer)
    assert over_facts.get("argument_files_complete") is False, over_facts
    assert over_facts.get("argument_files_incomplete_reason"), over_facts


# ==========================================================================
# D3：递归删除命令必须携带真实子对象影响
# ==========================================================================


def _recursive_dir_command(target: Path) -> str:
    if WINDOWS:
        return "rd /s /q " + str(target)
    return "rm -rf " + str(target)


async def test_d3_recursive_delete_hits_prohibited_child(
    ctx, workspace, monkeypatch
):
    """禁止删除目录内指定文件 → 递归删除该目录的命令必须命中并拒绝。

    * 真实 shell 递归删除（Windows rd /s /q、POSIX rm -rf）；
    * 断言：命令被拒、目录与被禁止文件都还在（真的没有删除）；程序 0 次执行；
    * 同时断言普通文件工具（fs_delete）对同一删除影响给出同样结论（不能相反）；
    * 相邻控制组：去掉该限制后同一目录命令真的删除成功（证明不是永远拒绝）。
    """
    _unrestricted(ctx)
    victim_dir = workspace / "victim"
    victim_dir.mkdir()
    secret = victim_dir / "secret.txt"
    secret.write_text("must stay", encoding="utf-8")
    binding = _save_limit(
        ctx,
        text="不要删除 secret.txt",
        operations=["delete"],
        objects=[{"kind": "path", "value": str(secret)}],
        tag="d3",
    )
    cmd = _recursive_dir_command(victim_dir)
    tool = RunCmdTool(computer=ctx.computer, security=ctx.authorization)

    with requirement_context(binding):
        denied = await tool.run(cmd=cmd, cwd=str(workspace), timeout=120)

    assert denied.ok is False, ("递归删除目录不能漏掉被禁止的子文件", denied)
    assert victim_dir.exists(), "命令被拒时目录必须保留"
    assert secret.exists(), "被禁止删除的文件必须保留"

    # 同一删除影响的普通文件工具不能得出相反结论（拒绝发生在回收之前）。
    def _mover_must_not_run(path):
        raise AssertionError("被禁止的删除不得进入回收入口：" + str(path))

    monkeypatch.setattr(recycle_bin, "send_to_recycle_bin", _mover_must_not_run)
    with requirement_context(binding):
        fs_denied = await FsDeleteTool(
            computer=ctx.computer, security=ctx.authorization
        ).run(paths=[str(victim_dir)])
    assert fs_denied.ok is False, ("普通文件工具必须同样拒绝", fs_denied)
    assert secret.exists(), "普通文件工具拒绝时不得删除任何子对象"

    # 相邻控制组：没有该限制时命令真的删除（证明拒绝不是“什么都不允许”）。
    control = await tool.run(cmd=cmd, cwd=str(workspace), timeout=120)
    assert control.ok is True, ("控制组必须真的执行", control)
    assert not victim_dir.exists(), "控制组必须真的递归删除了目录"


async def test_d3_incomplete_recursive_impact_not_executed_as_complete(
    ctx, workspace
):
    """目录影响枚举不完整（超过上限）时，递归删除命令不得按完整方案执行。

    * 真实 505 个文件（超过有界枚举上限 500）；
    * 用户只禁止删除最后（未枚举）的那个文件；影响不完整 → 命令不得执行；
    * 断言：目录与全部文件保留，命令明确报告影响不完整。
    """
    _unrestricted(ctx)
    victim_dir = workspace / "big"
    victim_dir.mkdir()
    for index in range(505):
        (victim_dir / ("f%03d.txt" % index)).write_text("x", encoding="utf-8")
    late = victim_dir / "zzz-late.txt"
    late.write_text("late", encoding="utf-8")
    binding = _save_limit(
        ctx,
        text="不要删除 zzz-late.txt",
        operations=["delete"],
        objects=[{"kind": "path", "value": str(late)}],
        tag="d3-big",
    )
    tool = RunCmdTool(computer=ctx.computer, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(
            cmd=_recursive_dir_command(victim_dir), cwd=str(workspace), timeout=180
        )

    assert result.ok is False, ("影响不完整时不得按完整方案执行", result)
    assert victim_dir.exists(), "影响不完整时目录必须保留"
    assert late.exists(), "未枚举到的子对象必须保留"
    assert (victim_dir / "f000.txt").exists(), "影响不完整时任何子对象都不得删除"
    assert result.code in (
        "delete_impact_incomplete",
        "content_binding_incomplete",
        "permission_denied",
    ), result


# ==========================================================================
# §10：路径比较不得改变实际操作语义（删除链接时保留用户指定的目录项）
# ==========================================================================


def _make_directory_link(link: Path, target: Path) -> bool:
    """真实目录链接：POSIX 用 os.symlink；Windows 无符号链接特权时用 junction。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if WINDOWS:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and os.path.lexists(link)
    return False


def _install_recycle_standin(monkeypatch, quarantine: Path) -> list[str]:
    """【模拟】回收站替身：记录真实收到的路径，把该目录项可恢复地移入隔离目录。

    这不是操作系统回收站的真实复现；只用于证明工具交给回收入口的是哪一个目录项，
    以及删除后原位置/目标对象各自发生了什么。
    """
    received: list[str] = []
    quarantine.mkdir(parents=True, exist_ok=True)

    def standin(path):
        raw = str(path)
        received.append(raw)
        entry = Path(raw)
        if not os.path.lexists(entry):
            return {
                "ok": False,
                "path": raw,
                "mechanism": "test_standin",
                "reason": "目录项不存在（替身）",
                "deleted_permanently": False,
            }
        destination = quarantine / ("item-%d" % len(received))
        os.replace(entry, destination)
        return {
            "ok": True,
            "path": raw,
            "mechanism": "test_standin",
            "reason": "测试回收站替身：已按可恢复语义移走该目录项",
            "trashed_path": str(destination),
            "deleted_permanently": False,
        }

    monkeypatch.setattr(recycle_bin, "send_to_recycle_bin", standin)
    return received


async def test_path_delete_link_entry_not_target(
    ctx, workspace, tmp_path, monkeypatch
):
    """请求回收目录链接 → 必须回收链接目录项本身，原目录与内容保持不动。

    * 真实目录链接（junction/symlink）+ 真实目标目录及内容；
    * 回收入口为测试替身（显式标注，不是操作系统回收站复现）；
    * 断言：交给回收入口的是用户指定链接路径；链接消失；目标目录与内容原样保留；
    * 失效链接场景：链接目标不存在时同样只处理链接目录项。
    """
    _unrestricted(ctx)
    target_dir = workspace / "protected"
    target_dir.mkdir()
    inner = target_dir / "inner.txt"
    inner.write_text("keep me", encoding="utf-8")
    links = workspace / "links"
    links.mkdir()
    link = links / "target-link"
    assert _make_directory_link(link, target_dir), "测试前提：必须能创建真实目录链接"
    broken = links / "broken-link"
    assert _make_directory_link(broken, tmp_path / "does-not-exist"), (
        "测试前提：必须能创建失效目录链接"
    )

    received = _install_recycle_standin(monkeypatch, tmp_path / "quarantine")
    tool = FsDeleteTool(computer=ctx.computer, security=ctx.authorization)
    binding = _benign_binding(ctx, "link")

    with requirement_context(binding):
        result = await tool.run(paths=[str(link)])
    assert result.ok is True, ("回收链接目录项本身必须成功", result)
    assert received == [str(link)], (
        "交给回收入口的必须是用户指定的目录项（链接），不是解析后的目标",
        received,
    )
    assert not os.path.lexists(link), "链接目录项必须真的消失"
    assert target_dir.exists(), "符号链接的目标目录不得被移动/删除"
    assert inner.exists() and inner.read_text(encoding="utf-8") == "keep me"

    with requirement_context(binding):
        broken_result = await tool.run(paths=[str(broken)])
    assert broken_result.ok is True, ("失效链接也必须按目录项回收", broken_result)
    assert received[-1] == str(broken), received
    assert not os.path.lexists(broken), "失效链接目录项必须真的消失"


async def test_path_delete_link_does_not_read_restricted_target(
    ctx, workspace, tmp_path, monkeypatch
):
    """删除链接不得读取目标内容：目标内容读取被禁止时链接仍可回收且目标完好。

    * 用户禁止读取目标目录里的文件内容；
    * 删除指向该目录的链接：真实操作只是移除链接目录项，不需要读取目标内容；
    * 断言：0 次目标正文/摘要读取；链接消失；目标及被保护内容原样保留。
    """
    _unrestricted(ctx)
    target_dir = workspace / "restricted-target"
    target_dir.mkdir()
    inner = target_dir / "secret.txt"
    inner.write_text("target content", encoding="utf-8")
    links = workspace / "linkdir2"
    links.mkdir()
    link = links / "restricted-link"
    assert _make_directory_link(link, target_dir)
    binding = _save_limit(
        ctx,
        text="不要读取 restricted-target 里的内容",
        operations=["read"],
        objects=[{"kind": "path", "value": str(inner)}],
        tag="path-read-target",
    )
    spy = _ReadSpy().install(monkeypatch)
    received = _install_recycle_standin(monkeypatch, tmp_path / "quarantine2")
    tool = FsDeleteTool(computer=ctx.computer, security=ctx.authorization)

    with requirement_context(binding):
        result = await tool.run(paths=[str(link)])

    assert result.ok is True, ("删除链接本身不应被目标内容的读取限制阻止", result)
    assert received == [str(link)], received
    assert not os.path.lexists(link), "链接目录项必须消失"
    assert target_dir.exists() and inner.exists(), "目标对象（受保护内容）必须原样保留"
    assert spy.calls == [], (
        "删除链接不得读取目标正文/摘要：" + repr(spy.calls)
    )
