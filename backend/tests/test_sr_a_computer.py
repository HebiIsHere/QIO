"""契约 1（Agent A）：命令自动放行 = 可信程序 × 显式只读语法 × 实际只读访问 × 可验证的可执行身份。

核心反例（修复前基线上应失败）：

* git tag/branch/remote 的写形态以前被判 LOW → auto，审批被 stub 拒绝后仓库
  仍可能被动过（真实 git 验证零变化）；
* --git-dir= / --output= 等号形式以前只查完整 token，能绕过；
* run_program cat 以前不看文件操作数：工作区外/红线路径照样 auto，借 cat
  绕过 fs_read 的拒绝；
* temp cwd 里的假 git.exe/.bat 替身以前不影响判定。

全部用例只依赖可观察行为（verdict / ok / 真实 git 状态 / 输出内容），
不引用内部实现细节；不使用真实密钥、不联网。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from agent.services.computer import CommandRisk, ComputerSandbox
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsReadTool


def _sandbox(root, mode: str = "default") -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: mode)


class _RejectingApprovals:
    """stub approvals：直接拒绝并计数。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, kind: str, payload: dict):
        self.calls.append(payload)
        return type("R", (), {"decision": "rejected"})()


def _install(tool, computer: ComputerSandbox, approvals) -> None:
    tool.computer = computer
    tool.approvals = approvals


async def _run_git(cwd, *args):
    """直接（不经工具）跑真实 git，用于布置与核验仓库状态。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _run_git_ok(cwd, *args):
    rc, out, err = await _run_git(cwd, *args)
    assert rc == 0, f"测试布置 git 失败：{' '.join(args)}\n{err}"
    return out


@pytest.fixture()
def git_repo(tmp_path):
    """临时真实 git 仓库（工作区根之外）+ 一次空提交，保证写操作本可成功。

    返回 (root, repo)：root 是声明的工作区根，repo 是真实 git 仓库。
    """
    root = tmp_path / "root"
    root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    asyncio.run(_build_repo(repo))
    return root, repo


def _build_repo(repo: Path):
    async def build():
        await _run_git_ok(repo, "init", "-b", "main")
        await _run_git_ok(
            repo, "-c", "user.email=sr-a@example.invalid", "-c", "user.name=sr-a",
            "commit", "--allow-empty", "-m", "init",
        )

    return build()


# ---------------------------------------------------------------------------
# 1) resolve_program：真实可执行文件路径
# ---------------------------------------------------------------------------


def test_resolve_program_finds_real_git(tmp_path):
    s = _sandbox(tmp_path)
    resolved = s.resolve_program("git")
    assert resolved is not None
    assert Path(resolved).is_file()
    assert Path(resolved).name.lower().startswith("git")


def test_resolve_program_missing_name_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    s = _sandbox(tmp_path)
    assert s.resolve_program("definitely-not-a-program-sr-a") is None


def test_resolve_program_absolute_path_used_directly(tmp_path):
    s = _sandbox(tmp_path)
    real = shutil.which("git")
    if real:
        assert Path(s.resolve_program(real)) == Path(real).resolve()
    assert s.resolve_program(str(tmp_path / "nope-sr-a.exe")) is None


def test_resolve_program_missing_absolute_path_returns_none(tmp_path):
    s = _sandbox(tmp_path)
    # 显式路径解析失败 → None（判定层按 HIGH 处理）
    assert s.resolve_program(str(tmp_path / "missing" / "prog.exe")) is None


# ---------------------------------------------------------------------------
# 2) git 写形态 → 非 auto（default 模式 approve，plan 模式 deny）
# ---------------------------------------------------------------------------

GIT_WRITE_FORMS = [
    ["tag", "v1"],                     # 创建 tag
    ["tag", "v1", "--force"],          # 创建 + 强制
    ["tag", "-d", "v1"],               # 删除
    ["tag", "--delete", "v1"],         # 删除（长选项）
    ["branch", "feature-x"],           # 新建分支
    ["branch", "-D", "feature-x"],     # 强制删除
    ["branch", "-d", "feature-x"],     # 删除
    ["branch", "--copy", "a", "b"],
    ["branch", "--move", "a", "b"],
    ["branch", "--set-upstream-to=origin/main"],
    ["remote", "add", "origin", "https://example.invalid/x.git"],
    ["remote", "rename", "origin", "up"],
    ["remote", "remove", "origin"],
    ["remote", "rm", "origin"],
    ["remote", "set-url", "origin", "https://example.invalid/y.git"],
    ["remote", "prune", "origin"],
    ["remote", "update"],
]


@pytest.mark.parametrize("args", GIT_WRITE_FORMS)
def test_git_write_forms_never_auto_in_default(tmp_path, args):
    s = _sandbox(tmp_path)
    verdict, risk = s.command_verdict_for_program("git", args)
    assert verdict != "auto", f"git {' '.join(args)} 不应自动放行"
    assert risk in (CommandRisk.HIGH, CommandRisk.DANGER)


@pytest.mark.parametrize("args", GIT_WRITE_FORMS)
def test_git_write_forms_denied_in_plan(tmp_path, args):
    s = _sandbox(tmp_path, mode="plan")
    verdict, risk = s.command_verdict_for_program("git", args)
    assert verdict == "deny", f"plan 模式下 git {' '.join(args)} 必须直接拒绝"
    assert risk != CommandRisk.LOW


# ---------------------------------------------------------------------------
# 3) 合法只读体验保持 auto（不得把白名单整体禁用）
# ---------------------------------------------------------------------------

GIT_READONLY_FORMS = [
    ["status"],
    ["log", "--oneline"],
    ["diff", "--stat"],
    ["show", "HEAD"],
    ["tag"],
    ["tag", "-l", "v*"],
    ["tag", "--list"],
    ["branch"],
    ["branch", "-a"],
    ["branch", "-r"],
    ["branch", "-l"],
    ["branch", "--list"],
    ["branch", "--show-current"],
    ["branch", "-vv"],
    ["branch", "--contains", "HEAD"],
    ["branch", "--merged", "main"],
    ["remote", "-v"],
    ["remote", "show", "origin"],
    ["remote", "get-url", "origin"],
    ["log", "--max-count=3"],
    ["log", "--max-count", "3"],
    ["log", "-5"],
    ["rev-parse", "HEAD"],
]


@pytest.mark.parametrize("args", GIT_READONLY_FORMS)
def test_git_readonly_forms_stay_auto(tmp_path, args):
    s = _sandbox(tmp_path)
    verdict, risk = s.command_verdict_for_program("git", args)
    assert verdict == "auto" and risk == CommandRisk.LOW, f"git {' '.join(args)} 应保持只读自动放行"


def test_plan_mode_keeps_readonly_auto(tmp_path):
    s = _sandbox(tmp_path, mode="plan")
    assert s.command_verdict_for_program("git", ["status"])[0] == "auto"
    assert s.command_verdict_for_program("git", ["tag"])[0] == "auto"
    assert s.command_verdict_for_program("git", ["tag", "-l", "v*"])[0] == "auto"


def test_branch_option_case_is_significant(tmp_path):
    s = _sandbox(tmp_path)
    # -l = 列表（只读）；-L 不是只读列表语义（大小写敏感，不得混判）
    assert s.command_verdict_for_program("git", ["branch", "-l"])[0] == "auto"
    assert s.command_verdict_for_program("git", ["branch", "-L"])[0] != "auto"


def test_ls_and_status_keep_auto(tmp_path):
    s = _sandbox(tmp_path)
    assert s.command_verdict_for_program("ls", ["-la"])[0] == "auto"
    assert s.command_verdict_for_program("git", ["status"])[0] == "auto"


# ---------------------------------------------------------------------------
# 4) 全局 token 扫描：FORBIDDEN 完整 token / 等号赋值形式 / shell 元字符 / git -C
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--git-dir=/tmp/x/.git", "status"],
        ["--work-tree=C:/tmp-wt", "status"],
        ["--exec-path=/tmp/evil", "status"],
        ["--upload-pack=/tmp/evil", "log", "--oneline"],
        ["--receive-pack=/tmp/evil", "log", "--oneline"],
        ["status", "--config=alias.x"],
        ["status", "--output=/tmp/x"],
        ["log", "--output=x"],
    ],
)
def test_equal_form_escape_tokens_never_auto(tmp_path, args):
    s = _sandbox(tmp_path)
    verdict, risk = s.command_verdict_for_program("git", args)
    assert verdict != "auto" and risk == CommandRisk.HIGH


@pytest.mark.parametrize(
    "args",
    [
        ["--git-dir", "/tmp/x/.git", "status"],
        ["--upload-pack", "/tmp/evil", "fetch", "origin"],
    ],
)
def test_bare_forbidden_tokens_never_auto(tmp_path, args):
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("git", args)
    assert verdict != "auto"


def test_metachar_in_args_is_never_auto(tmp_path):
    s = _sandbox(tmp_path)
    assert s.command_verdict_for_program("git", ["log", "--oneline", "a|b"])[1] == CommandRisk.HIGH
    assert s.command_verdict_for_program("git", ["log", "--oneline", "a>b"])[1] == CommandRisk.HIGH


def test_git_dash_c_outside_root_is_not_auto(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside-repo")
    s = _sandbox(tmp_path)  # 工作区根 = tmp_path，outside 在根外
    verdict, risk = s.command_verdict_for_program("git", ["-C", str(outside), "status"])
    assert verdict == "approve" and risk == CommandRisk.HIGH


def test_git_dash_c_inside_root_continues_readonly(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    s = _sandbox(tmp_path)
    verdict, risk = s.command_verdict_for_program("git", ["-C", "repo", "status"])
    assert verdict == "auto" and risk == CommandRisk.LOW


def test_git_dash_c_redline_is_denied(tmp_path):
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("git", ["-C", ".env", "status"])
    assert verdict == "deny"


def test_git_dash_c_missing_value_is_not_auto(tmp_path):
    s = _sandbox(tmp_path)
    assert s.command_verdict_for_program("git", ["-C", "status"])[0] != "auto"


# ---------------------------------------------------------------------------
# 5) 文件路径操作数一致性：cat/ls/... 与 fs_read 同一规则
# ---------------------------------------------------------------------------


def test_cat_inside_root_relative_auto(tmp_path):
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("cat", ["notes.md"])
    assert verdict == "auto"


def test_cat_inside_root_env_denied(tmp_path):
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("cat", [".env"])
    assert verdict == "deny"


def test_cat_relative_traversal_outside_root_approve(tmp_path):
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("cat", ["../outside/x.txt"])
    assert verdict == "approve"


def test_cat_outside_root_plain_approve(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("cat", [str(outside / "x.txt")])
    assert verdict == "approve"


def test_grep_plain_pattern_is_not_a_path_operand(tmp_path):
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("grep", ["-r", "TODO", "."])
    assert verdict == "auto"


def test_grep_pathish_pattern_counts_as_operand(tmp_path):
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("grep", ["-e", "../outside/secret", "."])
    assert verdict == "approve"


def test_grep_outside_file_operand_approve(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    s = _sandbox(tmp_path)
    verdict, _ = s.command_verdict_for_program("rg", ["pattern", str(outside / "x.txt")])
    assert verdict == "approve"


# ---------------------------------------------------------------------------
# 6) 程序级写参数表
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [".", "-name", "*.py", "-delete"],
        [".", "-name", "*.py", "-exec", "rm", "{}", ";"],
        [".", "-name", "*.py", "-execdir", "rm", "{}", ";"],
        [".", "-name", "*.py", "-fprint", "out.txt"],
        [".", "-name", "*.py", "-fls", "out.txt"],
    ],
)
def test_find_write_options_never_auto(tmp_path, args):
    s = _sandbox(tmp_path)
    verdict, risk = s.command_verdict_for_program("find", args)
    assert verdict != "auto" and risk == CommandRisk.HIGH


def test_find_readonly_form_auto(tmp_path):
    s = _sandbox(tmp_path)
    assert s.command_verdict_for_program("find", [".", "-name", "*.py"])[0] == "auto"


def test_output_style_option_never_auto(tmp_path):
    s = _sandbox(tmp_path)
    assert s.command_verdict_for_program("du", ["--output=out.txt", "."])[0] == "approve"
    assert s.command_verdict_for_program("ls", ["--output", "x"])[0] == "approve"


# ---------------------------------------------------------------------------
# 7) 程序身份：DANGER / 未知 / argv[0] 绝对路径 / 可执行替身
# ---------------------------------------------------------------------------


def test_danger_and_unknown_programs_unchanged(tmp_path):
    s = _sandbox(tmp_path)
    assert s.command_verdict_for_program("rm", ["-rf", "/x"])[1] == CommandRisk.DANGER
    assert s.command_verdict_for_program("curl", ["http://example.invalid"])[1] == CommandRisk.HIGH
    assert s.command_verdict_for_program("python", ["-c", "x"])[1] == CommandRisk.HIGH


def test_absolute_argv0_of_real_program(tmp_path):
    s = _sandbox(tmp_path)
    git_path = shutil.which("git")
    if git_path:
        assert s.command_verdict_for_program(git_path, ["status"])[0] == "auto"


def test_absolute_argv0_inside_root_is_untrusted(tmp_path):
    s = _sandbox(tmp_path)
    fake = tmp_path / "git.exe"
    fake.write_bytes(b"")
    verdict, risk = s.command_verdict_for_program(str(fake), ["status"])
    assert verdict == "approve" and risk == CommandRisk.HIGH


def test_absolute_argv0_missing_is_not_auto(tmp_path):
    s = _sandbox(tmp_path)
    assert s.command_verdict_for_program(str(tmp_path / "nope-sr-a.exe"), ["x"])[0] == "approve"


def test_fake_git_in_cwd_is_never_auto(tmp_path, monkeypatch):
    """temp cwd 放假 git.bat：替身不得 auto（走审批），真实系统 git 不受影响。"""
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    (fake_dir / "git.bat").write_text("@echo off\necho hacked\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()

    monkeypatch.setenv("PATH", str(fake_dir) + os.pathsep + os.environ.get("PATH", ""))
    s = _sandbox(root)

    # cwd 指向放替身的目录 → 替身
    verdict, risk = s.command_verdict_for_program("git", ["status"], cwd=str(fake_dir))
    assert verdict != "auto" and risk == CommandRisk.HIGH
    resolved = s.resolve_program("git", cwd=str(fake_dir))
    assert resolved is not None and Path(resolved).parent == fake_dir

    # 无 cwd 但替身所在目录被加进 PATH 且就是工作区根 → 同样不 auto
    s2 = _sandbox(fake_dir)
    verdict2, _ = s2.command_verdict_for_program("git", ["status"])
    assert verdict2 != "auto"

    # 真实系统 git 路径不受影响：PATH 恢复原样后，解析回归真实系统 git
    real = shutil.which("git")
    if real:
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        s3 = _sandbox(tmp_path / "another-root")
        assert Path(s3.resolve_program("git")) == Path(real).resolve()
        assert s3.command_verdict_for_program("git", ["status"])[0] == "auto"


def test_whitelist_bare_program_unresolved_stays_low(tmp_path, monkeypatch):
    """裸程序名解析不到时不做 HIGH 升级（本机没有 ls 也不能全面禁用白名单）。

    真实执行会以「找不到程序」如实失败，不构成放行风险。
    """
    monkeypatch.setenv("PATH", "")
    s = _sandbox(tmp_path)
    assert s.command_verdict_for_program("ls", ["-la"])[0] == "auto"


# ---------------------------------------------------------------------------
# 8) 真实 git 仓库 + RunProgramTool 真实实例：拒绝后零变化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_git_tag_leaves_repo_unchanged(git_repo):
    root, repo = git_repo
    t = RunProgramTool()
    _install(t, _sandbox(root), (appr := _RejectingApprovals()))
    res = await t.run(program="git", args=["tag", "v1"], cwd=str(repo))
    assert res.ok is False
    assert len(appr.calls) == 1, "git tag 创建必须走审批"
    out = await _run_git_ok(repo, "tag", "--list")
    assert out.strip() == "", "拒绝后仓库里不能出现新 tag"


@pytest.mark.asyncio
async def test_rejected_git_branch_leaves_repo_unchanged(git_repo):
    root, repo = git_repo
    t = RunProgramTool()
    _install(t, _sandbox(root), (appr := _RejectingApprovals()))
    res = await t.run(program="git", args=["branch", "feature-x"], cwd=str(repo))
    assert res.ok is False
    assert len(appr.calls) == 1
    out = await _run_git_ok(repo, "branch", "--list")
    assert "feature-x" not in out, "拒绝后不能出现新分支"


@pytest.mark.asyncio
async def test_rejected_git_remote_leaves_repo_unchanged(git_repo):
    root, repo = git_repo
    t = RunProgramTool()
    _install(t, _sandbox(root), (appr := _RejectingApprovals()))
    res = await t.run(program="git", args=["remote", "add", "origin", "https://example.invalid/x.git"], cwd=str(repo))
    assert res.ok is False
    assert len(appr.calls) == 1
    out = await _run_git_ok(repo, "remote")
    assert out.strip() == "", "拒绝后不能出现新 remote"


@pytest.mark.asyncio
async def test_approved_git_tag_actually_runs(git_repo):
    """对照组：审批放行时同一调用必须真的创建 tag（证明测试机器真实可执行）。"""
    root, repo = git_repo
    t = RunProgramTool()
    t.computer = _sandbox(root)
    t.approvals = _AcceptingApprovals()
    res = await t.run(program="git", args=["tag", "v1"], cwd=str(repo))
    assert res.ok is True
    out = await _run_git_ok(repo, "tag", "--list")
    assert "v1" in out


class _AcceptingApprovals:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def request(self, kind: str, payload: dict):
        self.calls.append(payload)
        return type("R", (), {"decision": "approved"})()


@pytest.mark.asyncio
async def test_plan_mode_tool_denies_git_write_without_approval(git_repo):
    root, repo = git_repo
    t = RunProgramTool()
    _install(t, _sandbox(root, mode="plan"), (appr := _RejectingApprovals()))
    res = await t.run(program="git", args=["tag", "v1"])
    assert res.ok is False
    assert not appr.calls, "plan 模式应直接拒绝，不进审批"


# ---------------------------------------------------------------------------
# 9) 旁路一致性：fs_read 拒绝的路径，run_program cat 不得 auto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cat_outside_env_denied_like_fs_read(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    env_file = outside / ".env"
    env_file.write_text("SR_A_TEST_MARKER=1\n", encoding="utf-8")
    s = _sandbox(root)

    # fs_read：红线拒绝
    fs = FsReadTool()
    _install(fs, s, (fs_appr := _RejectingApprovals()))
    r_fs = await fs.run(path=str(env_file))
    assert r_fs.ok is False
    assert not fs_appr.calls, "红线路径连审批都不进入"

    # run_program cat 同一路径：不得 auto（deny），且真实执行被拦
    verdict, _ = s.command_verdict_for_program("cat", [str(env_file)])
    assert verdict == "deny"
    t = RunProgramTool()
    _install(t, s, (appr := _RejectingApprovals()))
    res = await t.run(program="cat", args=[str(env_file)])
    assert res.ok is False
    assert not appr.calls


@pytest.mark.asyncio
async def test_cat_outside_root_plain_needs_approval(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "plain.txt"
    f.write_text("plain\n", encoding="utf-8")
    s = _sandbox(root)
    verdict, _ = s.command_verdict_for_program("cat", [str(f)])
    assert verdict == "approve"
    t = RunProgramTool()
    _install(t, s, (appr := _RejectingApprovals()))
    res = await t.run(program="cat", args=[str(f)])
    assert res.ok is False
    assert len(appr.calls) == 1, "根外普通文件读取要走审批（与 fs_read 一致）"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("cat") is None, reason="本机 PATH 无 cat（Windows），auto 判定另行断言")
async def test_cat_root_file_real_content(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    f = root / "hello.txt"
    f.write_text("SR_A_HELLO_CONTENT\n", encoding="utf-8")
    t = RunProgramTool()
    _install(t, _sandbox(root), _RejectingApprovals())
    res = await t.run(program="cat", args=[str(f)], cwd=str(root))
    assert res.ok is True
    assert "SR_A_HELLO_CONTENT" in res.content


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("find") is None,
    reason="需要 Windows find.exe（system32）作为根内文件真实读取的等价验证",
)
async def test_windows_find_real_read_of_root_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    f = root / "hello.txt"
    # Windows find.exe 要求 pattern 带引号：argv 里用含空格 token，list2cmdline 会加引号
    f.write_text("SR_A TEST MARKER\n", encoding="utf-8")
    t = RunProgramTool()
    _install(t, _sandbox(root), _RejectingApprovals())
    res = await t.run(program="find", args=["/c", "SR_A TEST", str(f)], cwd=str(root))
    assert res.ok is True
    assert "1" in res.content, "find /c 应真实读到根内文件并输出命中计数"
