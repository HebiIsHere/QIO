"""R9-H §7：固定查询的 POSIX 系统安装目录兼容性（真实流程回归 + /opt 复现）。

背景（真实 CI）：
* run 39903261628（SHA c085b745，Linux py3.11/py3.12）唯一失败项：
  FixedQueryFacts(kind='program', program_path='/usr/local/bin/npm', program_name='npm',
  argv=('--version',)) → None。根因：程序身份只按入口目录白名单（当时无 /usr/local/bin）
  且真实脚本目标（npm-cli.js）与入口名不同族。
* 后续 b0eb34b3 补了 /usr/local/bin 等精确目录；但 Linux 上 node 也可能安装在
  /opt/<...>/bin（如 /opt/hostedtoolcache/node/20.x/x64/bin、/opt/node/bin），
  且工具真实流程传给分类器的是 resolve 之后的脚本目标（npm-cli.js），
  这类入口仍拿不到固定查询。

环境假设（如实）：
* 本机是 Windows，无法创建真实的 /opt 目录树；POSIX 路径规则用生产代码的纯规则函数
  （_identity_rule_problem / _is_posix_system_bin_dir / _under_posix_system_roots /
  _posix_script_entry_allowed / _family_from_script_target）以 POSIX 字符串验证。
* 真实文件系统（存在性/可执行位/符号链接解析）由 host 平台用例与 Linux CI 覆盖；
  classify_fixed_query 的完整路径不在本文件用假文件系统伪造。
* 负向纪律不变：工作区/cwd/临时目录/任意可写目录的同名替身、任意脚本、整个 /opt 任意程序都不放行。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import agent.security.fixed_queries as fq
from agent.security.fixed_queries import (
    FixedQueryFacts,
    classify_fixed_query,
)

WINDOWS = sys.platform == "win32"


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if WINDOWS:
        for base in (Path("C:/Program Files/nodejs"), Path("C:/Program Files/Git/usr/bin")):
            for suffix in ("", ".exe", ".cmd", ".bat"):
                candidate = base / (name + suffix)
                if candidate.is_file():
                    return str(candidate)
    return None


def _facts(
    *,
    program_path: str,
    program_name: str,
    argv: tuple[str, ...],
    cwd: str | None = None,
    shell: bool = False,
) -> FixedQueryFacts:
    return FixedQueryFacts(
        kind="shell" if shell else "program",
        program_path=program_path,
        program_name=program_name,
        argv=argv,
        cwd=cwd,
        shell=shell,
    )


def _rule_problem(raw_path: str, real_text: str, *, program_name: str, cwd: str | None = None) -> str | None:
    return fq._identity_rule_problem(
        _facts(program_path=raw_path, program_name=program_name, argv=("--version",), cwd=cwd),
        raw_path=raw_path,
        real_text=real_text,
    )


# ---------------------------------------------------------------------------
# 1. 复现与契约目标：POSIX 系统安装目录（含 /opt/.../bin）的包管理器脚本入口
# ---------------------------------------------------------------------------


def test_ci_py312_usr_local_npm_entry_is_fixed_query_identity():
    """run 39903261628 的真实事实：/usr/local/bin/npm（入口名 npm、真实目标 npm-cli.js）。"""
    problems = _rule_problem(
        "/usr/local/bin/npm",
        "/usr/local/lib/node_modules/npm/bin/npm-cli.js",
        program_name="npm",
    )
    assert problems is None, problems


def test_opt_hostedtoolcache_npm_entry_is_fixed_query_identity():
    """契约 §7 目标：/opt/.../bin 的 npm 符号链接入口（GitHub Actions hostedtoolcache 布局）。"""
    entry = "/opt/hostedtoolcache/node/20.19.0/x64/bin/npm"
    real = "/opt/hostedtoolcache/node/20.19.0/x64/lib/node_modules/npm/bin/npm-cli.js"
    assert fq._is_posix_system_bin_dir(Path(entry).parent.as_posix()) is True
    assert fq._under_posix_system_roots(real) is True
    assert _rule_problem(entry, real, program_name="npm") is None


def test_opt_pnpm_and_yarn_entries_are_family_related_scripts():
    """同族包管理器入口（pnpm.cjs / yarn.js）在 /opt 系统目录下也按族放行。"""
    cases = (
        ("/opt/node/bin/pnpm", "/opt/node/lib/node_modules/pnpm/bin/pnpm.cjs", "pnpm"),
        ("/opt/node/bin/yarn", "/opt/node/lib/node_modules/yarn/bin/yarn.js", "yarn"),
    )
    for entry, real, name in cases:
        assert fq._posix_script_entry_allowed(entry, real, name) is True, (entry, real)
        assert _rule_problem(entry, real, program_name=name) is None


def test_posix_system_bin_dir_accepts_only_real_system_layouts():
    accepted = (
        "/usr/bin",
        "/usr/local/bin",
        "/bin",
        "/sbin",
        "/usr/sbin",
        "/opt/node/bin",
        "/opt/hostedtoolcache/node/20.19.0/x64/bin",
        "/opt/homebrew/bin",
        "/snap/bin",
    )
    rejected = (
        "/opt",
        "/opt/node",
        "/opt/evil/lib",
        "/tmp/bin",
        "/var/tmp/bin",
        "/home/user/bin",
        "/home/user/project/node_modules/.bin",
        "bin",
        "relative/bin",
    )
    for path in accepted:
        assert fq._is_posix_system_bin_dir(path) is True, path
    for path in rejected:
        assert fq._is_posix_system_bin_dir(path) is False, path


def test_posix_system_roots_reject_workspace_and_temp_targets():
    assert fq._under_posix_system_roots("/usr/local/lib/node_modules/npm/bin/npm-cli.js") is True
    assert fq._under_posix_system_roots("/opt/node/lib/node_modules/npm/bin/npm-cli.js") is True
    for path in (
        "/tmp/evil/npm-cli.js",
        "/var/tmp/evil/npm-cli.js",
        "/home/user/project/npm-cli.js",
        "/workspace/npm-cli.js",
        "npm-cli.js",
        "/",
    ):
        assert fq._under_posix_system_roots(path) is False, path


def test_imposter_entries_and_targets_are_rejected():
    """同名替身：入口不在系统目录，或目标不在系统目录/不同族。"""
    for entry in ("/tmp/bin/npm", "/home/user/project/bin/npm", "/workspace/bin/npm"):
        assert _rule_problem(entry, "/tmp/npm-cli.js", program_name="npm") is not None, entry
    for real in ("/tmp/evil/npm-cli.js", "/home/user/project/npm-cli.js", "/workspace/npm-cli.js"):
        assert _rule_problem("/usr/local/bin/npm", real, program_name="npm") is not None, real
    for real in ("/usr/local/lib/evil.js", "/usr/local/lib/python3.11/site.py", "/opt/x/lib/tool.js"):
        assert _rule_problem("/usr/local/bin/npm", real, program_name="npm") is not None, real
    assert _rule_problem("/usr/local/bin/npm", "/usr/local/lib/x/npm-cli.js", program_name="python") is not None


def test_resolved_script_target_derives_family_only_inside_system_roots():
    """真实流程中工具传的是 resolve 后的脚本目标（npm-cli.js）：仅在系统根内按同族识别。"""
    assert fq._family_from_script_target("npm-cli.js") == "npm"
    assert fq._family_from_script_target("pnpm.cjs") == "pnpm"
    assert fq._family_from_script_target("yarn.js") == "yarn"
    assert fq._family_from_script_target("evil.js") is None
    assert fq._family_from_script_target("npm") is None
    assert fq._resolved_script_family("/usr/local/lib/node_modules/npm/bin/npm-cli.js") == "npm"
    assert fq._resolved_script_family("/opt/node/lib/node_modules/npm/bin/npm-cli.js") == "npm"
    assert fq._resolved_script_family("/tmp/evil/npm-cli.js") is None
    assert fq._resolved_script_family("/home/user/project/npm-cli.js") is None
    assert fq._resolved_script_family("/usr/local/lib/evil.js") is None


def test_arbitrary_program_under_opt_is_not_a_fixed_query():
    """/opt/**/bin 只是系统目录；任意程序仍需已知程序族 + 精确 argv 结构。"""
    assert fq._family("aws") is None
    assert fq._family("mytool") is None
    assert fq._posix_script_entry_allowed("/opt/tools/bin/aws", "/opt/tools/lib/aws.js", "aws") is False
    assert fq._posix_script_entry_allowed("/opt/tools/bin/tool", "/opt/tools/lib/tool", "tool") is False
    assert classify_fixed_query(
        _facts(program_path="/opt/tools/bin/aws", program_name="aws", argv=("--version",), shell=True)
    ) is None


# ---------------------------------------------------------------------------
# 2. host 平台：真实程序 + 同名替身负向
# ---------------------------------------------------------------------------


def test_host_platform_node_npm_version_forms():
    """本机真实 node/npm（Windows 为 .cmd/真实文件；Linux CI 覆盖 /usr/local 或 /opt 入口）。"""
    checked = 0
    for name, forms in (("node", (("--version",), ("-v",))), ("npm", (("--version",), ("-v",)))):
        path = _which(name)
        if not path:
            continue
        checked += 1
        for form in forms:
            facts = _facts(program_path=path, program_name=name, argv=form)
            result = classify_fixed_query(facts)
            assert result is not None and result.kind == "version", (name, form, path, result)
        assert classify_fixed_query(
            _facts(program_path=path, program_name=name, argv=("install", "x"))
        ) is None
    if checked == 0:
        assert _rule_problem(
            "/opt/hostedtoolcache/node/20.19.0/x64/bin/npm",
            "/opt/hostedtoolcache/node/20.19.0/x64/lib/node_modules/npm/bin/npm-cli.js",
            program_name="npm",
        ) is None


def test_host_platform_same_name_imposters_rejected(tmp_path):
    real_path = _which("python") or sys.executable
    assert real_path
    fake = tmp_path / ("python.exe" if WINDOWS else "python")
    fake.write_bytes(b"fake")
    assert classify_fixed_query(
        _facts(program_path=str(fake), program_name="python", argv=("--version",))
    ) is None, "临时目录同名替身不得是固定查询"
    cwd_dir = tmp_path / "work"
    cwd_dir.mkdir()
    cwd_fake = cwd_dir / ("python.exe" if WINDOWS else "python")
    cwd_fake.write_bytes(b"fake")
    assert classify_fixed_query(
        _facts(program_path=str(cwd_fake), program_name="python", argv=("--version",), cwd=str(cwd_dir))
    ) is None, "cwd 同名替身不得是固定查询"


# ---------------------------------------------------------------------------
# 3. 真实流程回归：真实 create_app + 注册表 + 引擎
# ---------------------------------------------------------------------------


def _app_client(tmp_path):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.config import Settings
    from agent.credentials.store import MemoryKeyring
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations
    from agent.storage.settings import SettingsStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    store = SettingsStore(conn)
    store.set("computer.root_dir", str(workspace))
    store.set("computer.permission_mode", "read_only")
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    client = TestClient(app)
    client.__enter__()
    return client, workspace


def _exec(client, name, arguments):
    from agent.adapters.base import ToolCall

    async def _do():
        return await client.app.state.ctx.registry.execute(
            ToolCall(id="call_r9h_fixed", name=name, arguments=arguments)
        )

    return client.portal.call(_do)


def _close(client) -> None:
    client.__exit__(None, None, None)


def test_real_app_fixed_query_allowed_in_read_only_and_restricted(tmp_path):
    """真实流程：只读/受限下固定查询（python --version）直接允许、不弹审批；普通执行拒绝。"""
    client, workspace = _app_client(tmp_path)
    try:
        result = _exec(client, "run_program", {"program": sys.executable, "args": ["--version"]})
        assert result.ok is True, result.error
        pending = client.get("/api/approvals/pending").json()["approvals"]
        assert pending == [], f"固定查询不应产生审批：{pending}"

        denied = _exec(client, "run_program", {"program": sys.executable, "args": ["-c", "print(1)"]})
        assert denied.ok is False, "只读模式下非固定查询的执行必须拒绝"

        resp = client.put("/api/security/mode", json={"mode": "restricted", "user_confirmed": True})
        assert resp.status_code == 200, resp.text
        result2 = _exec(client, "run_program", {"program": sys.executable, "args": ["-V"]})
        assert result2.ok is True, result2.error
        assert client.get("/api/approvals/pending").json()["approvals"] == []

        node = _which("node")
        if node:
            node_result = _exec(client, "run_program", {"program": node, "args": ["--version"]})
            assert node_result.ok is True, node_result.error
    finally:
        _close(client)
