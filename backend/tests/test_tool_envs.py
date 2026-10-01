"""项目级依赖环境：按需准备专用 Python，并把依赖版本锁定下来。

回归的缺口（两处）：

1. 声明了第三方依赖也没有任何地方能把它装上 —— 工具永远跑不起来；
2. 装上去的只是「声明里的约束」，不是锁定：过一阵子重新建环境（删了、换了机器），
   同一个工具可能拿到**别的**依赖版本，而没人知道上次装的到底是什么。

这里给每个「依赖集合 + Python + 平台」准备一个 QIO 管理的专用环境，把 pip 解析出来
的精确版本（含来源与产物哈希）落成锁定清单，并保证：没有记录不算就绪、锁定版本对不
上不算就绪、删了环境还能按同一批版本重建；清理入口必须显式确认，仍在被工具引用的
环境不提示就不删。

测试里的安装器替身会按 venv / pip 的真实行为落文件（`pyvenv.cfg`、dist-info、
`--report` 的 JSON），所以「记录 → 核对 → 复用 → 清理」这条链路是真走通的。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent.tools import tool_envs as tool_envs_module
from agent.tools.tool_envs import ToolEnvManager

SHA = "a" * 64
REQUIREMENTS = ["requests>=2.31"]
PACKAGE = {
    "name": "requests",
    "version": "2.32.3",
    "source": "https://files.pythonhosted.org/packages/requests-2.32.3-py3-none-any.whl",
    "hash": f"sha256:{SHA}",
    "requested": True,
}


def _report_payload(packages: list[dict], *, pip_version: str = "pip 24.0") -> dict:
    """pip `--report` 的真实形状（这里只留我们用到的那几层）。"""
    install: list[dict] = []
    for package in packages:
        info: dict = {"url": package["source"], "archive_info": {}}
        if package.get("hash"):
            digest = package["hash"].split(":", 1)[1]
            info["archive_info"] = {
                "hash": f"sha256={digest}",
                "hashes": {"sha256": digest},
            }
        install.append(
            {
                "download_info": info,
                "requested": bool(package.get("requested", True)),
                "metadata": {"name": package["name"], "version": package["version"]},
            }
        )
    # 真实报告的顶层 `version` 是「报告格式」版本，pip 自己的版本在 `pip_version` 里。
    return {"version": "1", "pip_version": pip_version, "install": install}


class _FakeRunner:
    """记录命令行、按脚本返回结果的安装器替身（测试里不真的建 venv / 联网）。

    * `-m venv`：把解释器与 `pyvenv.cfg` 写出来；
    * `pip install`：把 `--report` 的 JSON 与每个包的 dist-info 写出来（像真 pip 一样）；
    * `pip freeze`：给出精确版本。
    """

    def __init__(
        self,
        *,
        packages: list[dict] | None = None,
        install_results: list[dict] | None = None,
        freeze_output: str | None = None,
        python_version: str | None = None,
        report: bool = True,
        pip_in_base: bool = True,
    ) -> None:
        self.calls: list[list[str]] = []
        self.packages = list([PACKAGE] if packages is None else packages)
        self.install_results = list(install_results or [])
        self.freeze_output = freeze_output
        self.python_version = python_version or ".".join(str(item) for item in sys.version_info[:3])
        self.report = report
        # False = 模拟 uv 管理的 venv（没有 pip）：解析要退回到新建 venv 里的 pip
        self.pip_in_base = pip_in_base

    async def __call__(self, argv: list[str], timeout: float, cwd=None):
        self.calls.append(list(argv))
        if len(argv) >= 4 and argv[1:3] == ["-m", "venv"]:
            target = Path(argv[3])
            name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            (target / name).write_text("", encoding="utf-8")
            (target / "pyvenv.cfg").write_text(
                f"home = C:\\python\nversion = {self.python_version}\n", encoding="utf-8"
            )
            return True, ""
        if len(argv) >= 4 and argv[1:3] == ["-m", "pip"]:
            if argv[3] == "--version":
                if self.pip_in_base or argv[0] != "C:/python.exe":
                    return True, "pip 24.0 from C:/python.exe (python 3.11)"
                return False, "C:/python.exe: No module named pip"
            if argv[3] == "install":
                return self._install(argv)
            if argv[3] == "freeze":
                output = self.freeze_output
                if output is None:
                    output = "\n".join(
                        f"{item['name']}=={item['version']}" for item in self.packages
                    )
                return True, output
        return True, ""

    def _install(self, argv: list[str]) -> tuple[bool, str]:
        if "--report" in argv and not self.report:
            # 老 pip（< 22.2）：不认识 `--report` —— 管理端要能退回 freeze。
            return False, "ERROR: no such option: --report"
        if self.install_results:
            result = self.install_results.pop(0)
            if not result.get("ok", True):
                return False, str(result.get("output") or "安装失败")
            packages = list(result.get("packages") or self.packages)
        else:
            packages = list(self.packages)
        if "--dry-run" not in argv:
            # 真 pip 的 --dry-run 只解析、不落盘：替身也不能在这里造出 dist-info。
            directory = Path(argv[0]).parent.parent
            site = directory / (
                "Lib/site-packages" if os.name == "nt" else "lib/python3.11/site-packages"
            )
            site.mkdir(parents=True, exist_ok=True)
            for package in packages:
                dist = f"{package['name'].replace('-', '_')}-{package['version']}.dist-info"
                (site / dist).mkdir(exist_ok=True)
        if "--report" in argv:
            report_path = Path(argv[argv.index("--report") + 1])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(_report_payload(packages)), encoding="utf-8")
        return True, "Successfully installed " + ", ".join(
            f"{item['name']}-{item['version']}" for item in packages
        )


class _Approvals:
    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs):
        from agent.tools.approval import ApprovalResult

        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", self.decision)


def _manager(tmp_path, runner, **kwargs) -> ToolEnvManager:
    return ToolEnvManager(tmp_path / "envs", base_python="C:/python.exe", runner=runner, **kwargs)


def _manifest(manager: ToolEnvManager, requirements=None) -> dict:
    path = manager._manifest_path(requirements or REQUIREMENTS)
    return json.loads(path.read_text(encoding="utf-8"))


# -- 基础行为（原有回归） --------------------------------------------------


def test_no_requirements_needs_no_environment(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure([]))

    assert status.ok
    assert status.interpreter is None
    assert runner.calls == []


def test_declared_dependencies_are_not_ready_before_preparing(tmp_path):
    manager = _manager(tmp_path, _FakeRunner())

    assert manager.is_ready(["requests"]) is False
    assert manager.status_for(["requests"]).ok is False


def test_prepare_asks_the_user_and_installs_only_declared_packages(tmp_path):
    runner = _FakeRunner()
    approvals = _Approvals()
    manager = _manager(tmp_path, runner)

    status = asyncio.run(
        manager.ensure(["requests>=2.31"], approvals=approvals, tool_name="weather")
    )

    assert status.ok, status.reason
    assert status.interpreter
    assert status.interpreter.replace("\\", "/").endswith(manager.interpreter_name)
    # 问过用户，而且把要装的东西写清了
    assert approvals.requests
    assert approvals.requests[0][0] == "dependency_install"
    assert "requests>=2.31" in approvals.requests[0][1]["packages"]
    # 创建环境 + 安装依赖，两步都只针对声明的包
    assert runner.calls[0][:3] == ["C:/python.exe", "-m", "venv"]
    install = runner.calls[1]
    assert install[-1] == "requests>=2.31"
    assert "-m" in install and "pip" in install
    # 向 pip 要机器可读的解析结果：锁定清单的唯一来源
    assert "--report" in install


def test_a_refusal_creates_nothing(tmp_path):
    runner = _FakeRunner()
    approvals = _Approvals(decision="rejected")
    manager = _manager(tmp_path, runner)

    status = asyncio.run(
        manager.ensure(["requests"], approvals=approvals, tool_name="weather")
    )

    assert status.ok is False
    assert "没有同意" in (status.reason or "")
    assert runner.calls == []
    assert not (tmp_path / "envs").exists() or not manager.is_ready(["requests"])


def test_a_ready_environment_is_reused_without_installing_again(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)
    asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))
    calls_after_first = len(runner.calls)

    again = asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))

    assert again.ok and again.reused is True
    assert len(runner.calls) == calls_after_first


def test_the_same_dependency_set_maps_to_one_environment(tmp_path):
    manager = _manager(tmp_path, _FakeRunner())

    assert manager.key_for(["b", "a"]) == manager.key_for(["a", "b"])
    assert manager.key_for(["a"]) != manager.key_for(["a", "b"])


def test_a_failed_install_is_reported_and_not_remembered_as_ready(tmp_path):
    runner = _FakeRunner(install_results=[{"ok": False, "output": "ERROR: 网络不通"}])
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure(["requests"], approvals=_Approvals()))

    assert status.ok is False
    assert "网络不通" in (status.reason or "")
    # 失败不写记录：下次还会重新尝试（而不是永远卡在「已就绪」的假象里）
    assert manager.is_ready(["requests"]) is False
    # 只有 venv + 一次安装：别的失败不该被当成「老 pip 不支持 --report」再试一遍
    assert len(runner.calls) == 2


# -- E1 依赖可复现 ---------------------------------------------------------


def test_the_environment_identity_covers_the_python_and_the_platform(tmp_path, monkeypatch):
    """同一组依赖在不同 Python / 不同平台上不是同一个环境。"""
    manager = _manager(tmp_path, _FakeRunner())
    baseline = manager.fingerprint_for(REQUIREMENTS)

    monkeypatch.setattr(tool_envs_module.sysconfig, "get_platform", lambda: "linux-aarch64")
    assert manager.fingerprint_for(REQUIREMENTS) != baseline

    monkeypatch.undo()
    monkeypatch.setattr(tool_envs_module.sys, "version_info", (3, 12, 0, "final", 0))
    assert manager.fingerprint_for(REQUIREMENTS) != baseline


def test_the_lock_manifest_records_resolved_versions_sources_and_hashes(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))

    assert status.ok, status.reason
    assert status.resolution == "resolved"
    assert status.fingerprint == manager.fingerprint_for(REQUIREMENTS)
    manifest = _manifest(manager)
    assert manifest["fingerprint"] == manager.fingerprint_for(REQUIREMENTS)
    assert manifest["requirements"] == REQUIREMENTS
    assert manifest["resolution"] == "resolved"
    # 至少要有：包名、解析版本、来源、哈希、Python 版本、平台
    assert manifest["packages"] == [PACKAGE]
    assert manifest["python"]["version"] == runner.python_version
    assert manifest["python"]["source"] == "pyvenv.cfg"
    assert manifest["system"]["platform"] == sys.platform
    assert manifest["installer"] == "pip 24.0"  # 报告的 version 是格式版本，别把它当成 pip 版本
    assert manifest["python"]["base"] is None or manifest["python"]["base"]
    assert manifest["lock"]["hashes"] is True
    # 锁定清单同时落在环境里与持久目录里（后者删环境也不删）
    lock_text = manager.lock_file_for(REQUIREMENTS).read_text(encoding="utf-8")
    assert "requests==2.32.3" in lock_text
    assert f"--hash=sha256:{SHA}" in lock_text
    record = manager.lock_for(REQUIREMENTS)
    assert record["packages"] == [PACKAGE]
    assert record["identity"]["requirements"] == REQUIREMENTS
    assert manager.locked_packages_for(REQUIREMENTS) == ["requests==2.32.3"]


def test_lock_never_records_credentials_or_signatures_from_package_urls(tmp_path):
    runner = _FakeRunner(
        packages=[
            {
                "name": "requests",
                "version": "2.32.3",
                "source": (
                    "https://user:s3cret@packages.internal.example/simple/"
                    "requests-2.32.3-py3-none-any.whl?token=abc123"
                ),
                "hash": f"sha256:{SHA}",
                "requested": True,
            }
        ]
    )
    manager = _manager(tmp_path, runner)

    asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))

    record = manager.lock_for(REQUIREMENTS)
    source = record["packages"][0]["source"]
    assert source == "https://packages.internal.example/simple/requests-2.32.3-py3-none-any.whl"
    for text in (
        json.dumps(record, ensure_ascii=False),
        manager.lock_file_for(REQUIREMENTS).read_text(encoding="utf-8"),
        json.dumps(_manifest(manager), ensure_ascii=False),
    ):
        assert "s3cret" not in text
        assert "token=abc123" not in text


def test_a_partly_hashed_report_does_not_enable_pip_hash_checking(tmp_path):
    """有一个包没有哈希时整份锁定文件都不带 --hash（pip 的哈希校验要求全有）。"""
    packages = [
        PACKAGE,
        {"name": "urllib3", "version": "2.2.1", "source": "", "hash": "", "requested": False},
    ]
    runner = _FakeRunner(packages=packages)
    manager = _manager(tmp_path, runner)

    assert asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals())).ok

    lock_text = manager.lock_file_for(REQUIREMENTS).read_text(encoding="utf-8")
    assert "urllib3==2.2.1" in lock_text
    assert "--hash=" not in lock_text
    assert _manifest(manager)["lock"]["hashes"] is False


def test_a_deleted_environment_is_rebuilt_from_the_recorded_lock(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)
    asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))
    lock_file = manager.lock_file_for(REQUIREMENTS)
    lock_before = lock_file.read_text(encoding="utf-8")
    # 删掉环境目录（模拟清理 / 换机器上重建）：锁定清单还在
    import shutil

    shutil.rmtree(manager.directory_for(REQUIREMENTS))
    runner.calls.clear()

    status = asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))

    assert status.ok, status.reason
    assert status.resolution == "locked"
    install = runner.calls[1]
    assert install[install.index("-r") + 1] == str(lock_file)
    assert lock_file.read_text(encoding="utf-8") == lock_before
    assert manager.locked_packages_for(REQUIREMENTS) == ["requests==2.32.3"]
    # 审批内容也要说清「按锁定版本重建」，用户才知道批准的是什么
    approvals = _Approvals()
    shutil.rmtree(manager.directory_for(REQUIREMENTS))
    asyncio.run(manager.ensure(REQUIREMENTS, approvals=approvals))
    assert "锁定版本" in approvals.requests[0][1]["detail"]
    assert approvals.requests[0][1]["lock"]["packages"] == ["requests==2.32.3"]


def test_an_unusable_lock_is_reported_and_re_resolved_explicitly(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)
    asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))
    import shutil

    shutil.rmtree(manager.directory_for(REQUIREMENTS))
    # 重建时锁定版本装不上（索引上没了 / 哈希对不上），按声明重新解析到新版本
    runner.packages = [
        {
            "name": "requests",
            "version": "2.33.0",
            "source": PACKAGE["source"],
            "hash": f"sha256:{'b' * 64}",
            "requested": True,
        }
    ]
    runner.install_results = [
        {"ok": False, "output": "ERROR: No matching distribution found for requests==2.32.3"}
    ]
    runner.calls.clear()

    status = asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))

    assert status.ok, status.reason
    assert status.resolution == "re-resolved"
    assert "锁定版本" in (status.note or "")
    assert runner.calls[1][runner.calls[1].index("-r") + 1] == str(
        manager.lock_file_for(REQUIREMENTS)
    )
    assert runner.calls[2][-1] == "requests>=2.31"
    # 新解析出来的版本要如实记进锁定清单（而不是继续声称旧的）
    assert manager.locked_packages_for(REQUIREMENTS) == ["requests==2.33.0"]
    assert _manifest(manager)["resolution"] == "re-resolved"


def test_an_old_pip_without_report_still_locks_the_versions(tmp_path):
    runner = _FakeRunner(report=False)
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))

    assert status.ok, status.reason
    assert status.lock_available is True
    # 第一次带 --report 失败 → 退回普通安装 → pip freeze 拿版本
    assert "--report" in runner.calls[1]
    assert "--report" not in runner.calls[2]
    assert runner.calls[3][3] == "freeze"
    manifest = _manifest(manager)
    assert manifest["fidelity"] == "freeze"
    assert manifest["lock"]["hashes"] is False
    assert [item["version"] for item in manifest["packages"]] == ["2.32.3"]
    assert manager.lock_for(REQUIREMENTS)["packages"][0]["source"] == ""


def test_a_venv_with_another_python_is_not_accepted(tmp_path):
    runner = _FakeRunner(python_version="3.12.1")
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))

    assert status.ok is False
    assert "不一致" in (status.reason or "")
    assert manager.is_ready(REQUIREMENTS) is False
    assert not manager._manifest_path(REQUIREMENTS).exists()


def test_an_environment_whose_versions_drifted_is_not_ready(tmp_path):
    """有人手动 pip install -U 过 → 锁定版本对不上 → 明确要求重新准备。"""
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)
    asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))
    assert manager.is_ready(REQUIREMENTS) is True
    site = manager.directory_for(REQUIREMENTS) / (
        "Lib/site-packages" if os.name == "nt" else "lib/python3.11/site-packages"
    )
    (site / "requests-2.32.3.dist-info").rename(site / "requests-2.40.0.dist-info")

    assert manager.is_ready(REQUIREMENTS) is False
    ok, why = manager.readiness(REQUIREMENTS)
    assert ok is False
    assert "不一致" in (why or "")
    status = manager.status_for(REQUIREMENTS)
    assert status.ok is False
    assert "requests==2.32.3" in (status.reason or "")


def test_a_legacy_environment_is_not_considered_ready(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)
    directory = manager.directory_for(REQUIREMENTS)
    name = manager.interpreter_name
    (directory / name).parent.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("", encoding="utf-8")
    (directory / "qio-env.json").write_text(
        json.dumps({"schema": 1, "requirements": REQUIREMENTS, "created_at": "2026-01-01"}),
        encoding="utf-8",
    )

    assert manager.is_ready(REQUIREMENTS) is False
    assert "旧格式" in (manager.readiness(REQUIREMENTS)[1] or "")


def test_last_used_is_recorded_for_a_real_tool_call(tmp_path):
    runner = _FakeRunner()
    manager = _manager(tmp_path, runner)
    asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals()))
    manifest_path = manager._manifest_path(REQUIREMENTS)
    data = _manifest(manager)
    data["last_used_at"] = "2000-01-01T00:00:00+00:00"
    manifest_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert manager.status_for(REQUIREMENTS).ok is True

    assert _manifest(manager)["last_used_at"] != "2000-01-01T00:00:00+00:00"


class _FakeDocker:
    """docker 命令行替身（本机没有 Docker：只验证管理端的命令与状态机）。

    * §docker image inspect <tag>§：按 §images§ 集合回答（命中 = 本机已有镜像）；
    * §docker build ... -t <tag> ...§：按 §build_ok§ 回答，成功后把 tag 加进 §images§；
    * 其它命令（venv / pip）交给 §_FakeRunner§（§calls§ 里只有这些）。
    """

    def __init__(
        self,
        *,
        images: tuple[str, ...] = (),
        build_ok: bool = True,
        build_output: str = "Successfully built qio-tool-env",
        **runner_kwargs,
    ) -> None:
        self.images = set(images)
        self.build_ok = build_ok
        self.build_output = build_output
        self.docker_calls: list[list[str]] = []
        self._inner = _FakeRunner(**runner_kwargs)

    @property
    def calls(self) -> list[list[str]]:
        return self._inner.calls

    async def __call__(self, argv: list[str], timeout: float, cwd=None):
        if argv and argv[0] == "docker":
            self.docker_calls.append(list(argv))
            if argv[1:3] == ["image", "inspect"]:
                if argv[3] in self.images:
                    return True, "[]"
                return False, "Error: No such image: " + argv[3]
            if argv[1] == "build":
                if self.build_ok:
                    self.images.add(argv[argv.index("-t") + 1])
                return self.build_ok, self.build_output
            return False, "unknown docker command"
        return await self._inner(argv, timeout, cwd)

    def builds(self) -> list[list[str]]:
        return [call for call in self.docker_calls if call[1] == "build"]


# -- E2 环境清理 -----------------------------------------------------------


class _Definition:
    def __init__(self, name: str, requirements: list[str]) -> None:
        self.name = name
        self.requirements = requirements


def _prepared(tmp_path, requirements=None, **runner_kwargs):
    requirements = requirements or REQUIREMENTS
    runner = _FakeRunner(**runner_kwargs)
    manager = _manager(tmp_path, runner)
    assert asyncio.run(manager.ensure(requirements, approvals=_Approvals())).ok
    return manager, runner, requirements


def test_inventory_attributes_environments_to_the_tools_that_need_them(tmp_path):
    manager, _runner, requirements = _prepared(tmp_path)
    table = manager.references_from_definitions(
        [_Definition("weather", REQUIREMENTS), _Definition("plain", [])]
    )

    entries = manager.inventory(referenced_by=table)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.fingerprint == manager.fingerprint_for(requirements)
    assert entry.requirements == REQUIREMENTS
    assert entry.referenced_by == ["weather"]
    assert entry.orphan is False
    assert entry.ready is True and entry.state == "ready"
    assert entry.lock_available is True
    assert entry.locked_packages == ["requests==2.32.3"]
    assert entry.last_used_at
    assert manager.cleanup_candidates(referenced_by=table) == []


def test_inventory_still_attributes_pre_upgrade_directories(tmp_path):
    """升级前（schema 1，目录名只按依赖集合）建的环境也要能看出是谁在用。"""
    manager, _runner, requirements = _prepared(tmp_path)
    legacy = manager.root / tool_envs_module._legacy_key_for(REQUIREMENTS)
    (legacy / "Scripts").mkdir(parents=True, exist_ok=True)
    (legacy / "qio-env.json").write_text(
        json.dumps({"schema": 1, "requirements": REQUIREMENTS, "created_at": "2026-01-01"}),
        encoding="utf-8",
    )
    table = manager.references_from_definitions([_Definition("weather", REQUIREMENTS)])

    entries = {entry.fingerprint: entry for entry in manager.inventory(referenced_by=table)}

    old = entries[legacy.name]
    assert old.state == "legacy"
    assert old.referenced_by == ["weather"]
    assert old.orphan is False
    # 现在这个环境仍然是「有引用的」
    current = entries[manager.fingerprint_for(requirements)]
    assert current.referenced_by == ["weather"]


def test_removing_an_environment_needs_an_explicit_confirmation(tmp_path):
    manager, _runner, requirements = _prepared(tmp_path)

    result = asyncio.run(manager.remove(manager.fingerprint_for(requirements)))

    assert result.ok is False
    assert result.removed is False
    assert "确认" in (result.reason or "")
    assert manager.is_ready(requirements) is True


def test_an_environment_used_by_a_registered_tool_is_not_removed_without_a_prompt(tmp_path):
    manager, _runner, requirements = _prepared(tmp_path)
    fingerprint = manager.fingerprint_for(requirements)
    table = manager.references_from_definitions([_Definition("weather", REQUIREMENTS)])

    blocked = asyncio.run(manager.remove(fingerprint, confirm=True, referenced_by=table))

    assert blocked.ok is False and blocked.removed is False
    assert blocked.referenced_by == ["weather"]
    assert "正在被已注册的工具使用" in (blocked.reason or "")
    assert "weather" in (blocked.reason or "")
    assert manager.is_ready(requirements) is True  # 没提示就不许删

    allowed = asyncio.run(
        manager.remove(fingerprint, confirm=True, referenced_by=table, allow_in_use=True)
    )
    assert allowed.ok is True and allowed.removed is True
    assert allowed.referenced_by == ["weather"]
    assert "重新准备" in (allowed.next_step or "")
    assert "weather" in (allowed.next_step or "")
    assert allowed.kept_lock is True
    assert manager.is_ready(requirements) is False


def test_a_ready_environment_is_kept_when_the_references_are_unknown(tmp_path):
    """没有引用信息时对「可用」的环境保守：不许一句 confirm 就删掉。"""
    manager, _runner, requirements = _prepared(tmp_path)
    fingerprint = manager.fingerprint_for(requirements)

    result = asyncio.run(manager.remove(fingerprint, confirm=True))

    assert result.ok is False
    assert "无法确认" in (result.reason or "")
    assert manager.is_ready(requirements) is True
    # 明确说了「没有工具引用」才允许删（{} 是「查过了，没人用」）
    assert asyncio.run(manager.remove(fingerprint, confirm=True, referenced_by={})).ok is True


def test_removing_an_environment_keeps_the_lock_and_tells_the_tool_what_to_do(tmp_path):
    manager, _runner, requirements = _prepared(tmp_path)
    fingerprint = manager.fingerprint_for(requirements)

    result = asyncio.run(manager.remove(fingerprint, confirm=True, referenced_by={}))

    assert result.ok and result.removed
    assert result.kept_lock is True
    assert manager.lock_file_for(requirements).is_file()
    assert manager.locked_packages_for(requirements) == ["requests==2.32.3"]
    status = manager.status_for(requirements)
    assert status.ok is False
    assert "专用环境还没准备好" in (status.reason or "")
    assert "重新准备" in (result.next_step or "")
    # 工具侧能明确说清「怎么补上」以及「重建会用哪批版本」
    assert status.lock_available is True
    assert "requests==2.32.3" in (status.reason or "")


def test_a_directory_without_a_manifest_is_never_attributed_but_can_be_cleaned(tmp_path):
    manager = _manager(tmp_path, _FakeRunner())
    unknown = manager.root / "deadbeefdeadbeef"
    (unknown / "Scripts").mkdir(parents=True, exist_ok=True)

    entries = manager.inventory()

    assert [entry.state for entry in entries] == ["unknown"]
    assert entries[0].orphan is True
    assert entries[0].referenced_by == []
    # 默认不当成清理候选：归属不明的目录不自动动
    assert manager.cleanup_candidates() == []
    assert asyncio.run(manager.remove(unknown.name, confirm=True)).ok is True
    assert unknown.exists() is False


def test_a_lock_left_behind_without_an_environment_is_reported(tmp_path):
    manager, _runner, requirements = _prepared(tmp_path)
    import shutil

    shutil.rmtree(manager.directory_for(requirements))

    entries = manager.inventory()

    assert [entry.state for entry in entries] == ["lock-only"]
    entry = entries[0]
    assert entry.directory is None
    assert entry.locked_packages == ["requests==2.32.3"]
    assert entry.orphan is True
    # 只剩锁定清单时，普通删除没有东西可删；要删就得明说「连锁定一起忘掉」
    refused = asyncio.run(manager.remove(entry.fingerprint, confirm=True))
    assert refused.ok is False and refused.removed is False
    assert "锁定清单" in (refused.reason or "")
    removed = asyncio.run(manager.remove(entry.fingerprint, confirm=True, forget_lock=True))
    assert removed.ok is True
    assert manager.lock_for(requirements) is None


def test_cleanup_reports_what_it_removed_and_what_it_kept(tmp_path):
    manager, _runner, requirements = _prepared(tmp_path)
    other = ToolEnvManager(manager.root, base_python="C:/python.exe", runner=_FakeRunner())
    other_requirements = ["urllib3>=2"]
    assert asyncio.run(other.ensure(other_requirements, approvals=_Approvals())).ok
    table = manager.references_from_definitions([_Definition("weather", REQUIREMENTS)])

    dry_run = asyncio.run(manager.cleanup(referenced_by=table))

    assert dry_run.candidates == 1  # 被引用的那个不算候选
    assert dry_run.removed == []
    assert dry_run.ok is False
    assert "确认" in (dry_run.skipped[0].reason or "")

    report = asyncio.run(manager.cleanup(confirm=True, referenced_by=table))

    assert report.ok is True
    assert [item.fingerprint for item in report.removed] == [
        manager.fingerprint_for(other_requirements)
    ]
    assert manager.is_ready(requirements) is True


def test_the_maintenance_cli_lists_and_removes_with_confirmation(tmp_path, capsys):
    manager, _runner, requirements = _prepared(tmp_path)
    root = str(manager.root)
    fingerprint = manager.fingerprint_for(requirements)

    assert tool_envs_module.main(["--root", root, "--json", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["fingerprint"] == fingerprint
    assert listed[0]["state"] == "ready"
    assert listed[0]["locked_packages"] == ["requests==2.32.3"]
    assert listed[0]["orphan"] is True  # CLI 不知道注册表，如实说「没有工具引用」

    assert tool_envs_module.main(["--root", root, "remove", fingerprint]) == 1
    assert manager.is_ready(requirements) is True  # 没有 --yes：不删
    assert tool_envs_module.main(["--root", root, "remove", fingerprint, "--yes"]) == 1
    assert manager.is_ready(requirements) is True  # 可用环境且没有引用信息：保守拒绝

    assert tool_envs_module.main(
        ["--root", root, "remove", fingerprint, "--yes", "--allow-in-use"]
    ) == 0
    assert manager.is_ready(requirements) is False
    assert manager.lock_file_for(requirements).is_file()

# -- E3 容器执行路径的依赖（准备侧；本机没有 Docker，用替身验证命令与状态机） ----


def test_the_container_image_identity_follows_the_environment(tmp_path):
    manager = _manager(tmp_path, _FakeDocker())
    tag = manager.container_image_for(REQUIREMENTS)

    assert tag.startswith("qio-tool-env:")
    assert tag.endswith(manager.fingerprint_for(REQUIREMENTS))
    assert manager.container_image_for(["other>=1"]) != tag
    plan = manager.container_plan(REQUIREMENTS)
    assert plan["base_image"] == manager.container_base_image()
    assert "FROM python:" in plan["dockerfile"]
    assert "COPY requirements.lock" in plan["dockerfile"]
    assert "-r /tmp/qio-requirements.lock" in plan["dockerfile"]
    assert plan["never"] == "不在每次工具调用时联网解析或安装依赖"


def test_resolve_only_does_not_install_anything_on_the_host(tmp_path):
    runner = _FakeDocker()
    manager = _manager(tmp_path, runner)

    ok, packages, installer = asyncio.run(manager.resolve_only(REQUIREMENTS))

    assert ok is True
    assert packages == [PACKAGE]
    assert installer == "pip 24.0"
    argv = [call for call in runner.calls if "--dry-run" in call][0]
    assert argv[:3] == ["C:/python.exe", "-m", "pip"]
    assert "--dry-run" in argv and "--ignore-installed" in argv
    assert argv[-1] == "requests>=2.31"
    # 只解析：没有建环境、没有安装、也不算「环境就绪」
    assert not any(call[1:3] == ["-m", "venv"] for call in runner.calls)
    assert not any("install" in call and "--dry-run" not in call for call in runner.calls)
    assert manager.is_ready(REQUIREMENTS) is False
    assert manager.lock_for(REQUIREMENTS) is None


def test_resolve_only_reports_a_failure_instead_of_an_empty_lock(tmp_path):
    runner = _FakeDocker(install_results=[{"ok": False, "output": "ERROR: 没有网络"}])
    manager = _manager(tmp_path, runner)

    ok, packages, installer = asyncio.run(manager.resolve_only(REQUIREMENTS))

    assert ok is False
    assert packages == []
    assert "没有网络" in installer


def test_a_container_image_is_built_once_from_the_lock_and_reused_offline(tmp_path):
    runner = _FakeDocker()
    manager = _manager(tmp_path, runner)
    approvals = _Approvals()

    first = asyncio.run(
        manager.ensure_container_image(REQUIREMENTS, approvals=approvals, tool_name="weather")
    )

    assert first.ok, first.reason
    assert first.built is True and first.reused is False
    assert first.image == manager.container_image_for(REQUIREMENTS)
    # 没有为了拿锁定清单在宿主上装东西：是 pip --dry-run 解析出来的
    assert not any(call[1:3] == ["-m", "venv"] for call in runner.calls)
    assert not any("install" in call and "--dry-run" not in call for call in runner.calls)
    assert manager.lock_for(REQUIREMENTS)["fidelity"] == "pip-report-resolve-only"
    assert manager.locked_packages_for(REQUIREMENTS) == ["requests==2.32.3"]
    # 构建上下文只有锁定清单 + Dockerfile，且 Dockerfile 只按锁定清单装
    build = runner.builds()[0]
    assert build[1:3] == ["build", "-f"]
    assert build[build.index("-t") + 1] == first.image
    context = Path(build[-1])
    assert (context / "requirements.lock").read_text(encoding="utf-8") == (
        manager.lock_file_for(REQUIREMENTS).read_text(encoding="utf-8")
    )
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY requirements.lock" in dockerfile
    assert "requests>=2.31" not in dockerfile  # 镜像里不做临时解析
    # 构建结果记进持久锁定记录
    assert manager.lock_for(REQUIREMENTS)["container"]["image"] == first.image

    before = len(runner.calls)
    second_approvals = _Approvals()
    second = asyncio.run(manager.ensure_container_image(REQUIREMENTS, approvals=second_approvals))

    assert second.ok and second.reused is True and second.built is False
    assert second_approvals.requests == []  # 镜像已有：不再打扰用户
    assert len(runner.calls) == before  # 也不再动 pip / venv
    assert len(runner.builds()) == 1


def test_the_container_path_asks_for_approval_before_building(tmp_path):
    runner = _FakeDocker()
    manager = _manager(tmp_path, runner)

    no_channel = asyncio.run(manager.ensure_container_image(REQUIREMENTS))

    assert no_channel.ok is False
    assert "确认通道" in (no_channel.reason or "")
    assert runner.builds() == []
    assert [call for call in runner.docker_calls if call[1] == "image"] != []  # 只探测，不构建

    rejected = asyncio.run(
        manager.ensure_container_image(REQUIREMENTS, approvals=_Approvals(decision="rejected"))
    )

    assert rejected.ok is False
    assert "没有同意" in (rejected.reason or "")
    assert runner.builds() == []
    assert manager.lock_for(REQUIREMENTS) is None


def test_a_container_build_is_not_attempted_when_versions_cannot_be_resolved(tmp_path):
    runner = _FakeDocker(install_results=[{"ok": False, "output": "ERROR: 解析不了"}])
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure_container_image(REQUIREMENTS, approvals=_Approvals()))

    assert status.ok is False
    assert "解析依赖版本失败" in (status.reason or "")
    assert runner.builds() == []
    assert manager.lock_for(REQUIREMENTS) is None


def test_a_failed_build_is_reported_and_not_recorded_as_ready(tmp_path):
    runner = _FakeDocker(build_ok=False, build_output="ERROR: no space left on device")
    manager = _manager(tmp_path, runner)

    status = asyncio.run(manager.ensure_container_image(REQUIREMENTS, approvals=_Approvals()))

    assert status.ok is False
    assert "no space left on device" in (status.reason or "")
    record = manager.lock_for(REQUIREMENTS) or {}
    assert "container" not in record


def test_the_container_plan_uses_an_existing_lock_without_resolving(tmp_path):
    runner = _FakeDocker()
    manager = _manager(tmp_path, runner)
    assert asyncio.run(manager.ensure(REQUIREMENTS, approvals=_Approvals())).ok
    calls_before = len(runner.calls)

    status = asyncio.run(manager.ensure_container_image(REQUIREMENTS, approvals=_Approvals()))

    assert status.ok and status.built is True
    assert status.pinned == ["requests==2.32.3"]  # 用的是宿主环境那份锁定清单
    assert len(runner.calls) == calls_before  # 没有再解析
    assert manager.lock_for(REQUIREMENTS)["fidelity"] == "pip-report"

def test_resolve_falls_back_to_a_venv_pip_when_the_base_interpreter_has_none(tmp_path):
    """uv 管理的 venv 默认没有 pip（实测过）：解析要退回新建 venv 里的 pip，而不是直接失败。"""
    runner = _FakeDocker(pip_in_base=False)
    manager = _manager(tmp_path, runner)

    ok, packages, installer = asyncio.run(manager.resolve_only(REQUIREMENTS))

    assert ok is True
    assert packages == [PACKAGE]
    assert any(call[1:3] == ["-m", "venv"] for call in runner.calls)
    resolve = [call for call in runner.calls if "--dry-run" in call][0]
    assert resolve[0].replace("\\", "/").endswith(manager.interpreter_name)
    # 宿主上仍然什么都没装：只建了一个空环境 + 解析
    assert not any("install" in call and "--dry-run" not in call for call in runner.calls)
    assert manager.is_ready(REQUIREMENTS) is False
