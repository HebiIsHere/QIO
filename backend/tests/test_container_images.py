"""F1：容器镜像的 inventory / 引用判定 / 清理候选 / 显式清理。

硬约束（任务书）：**绝不能删掉「被已注册工具引用的唯一可用镜像」**。这里的用例把这条
约束拆开测：候选阶段不出现、删除阶段即使显式确认也拒绝、没有引用表时保守拒绝、
别人的镜像一律不碰。

本机没有 Docker 守护进程，所以真实命令路径用**替身 runner** 覆盖（命令行、拒绝时不发 rm
都能断言）；真正的 `docker image ls / image rm` 由 `requires_docker` 那条在 ubuntu CI 上跑
（windows 任务按标记过滤；CI 上守护进程不可用会直接 fail，不静默跳过）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools import sandbox as sandbox_module
from agent.tools import tool_envs as tool_envs_module
from agent.tools.tool_envs import ToolEnvManager, parse_docker_images

REQS_A = ["six>=1.16"]
REQS_B = ["httpx>=0.27"]


def _images_output(*entries: dict) -> str:
    return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n"


class _FakeDockerRunner:
    """替身：记录命令行，按子命令给结果。`ls` 的输出就是「本机镜像清单」。"""

    def __init__(self, images_output: str = "", *, ready: bool = True, rm_ok: bool = True):
        self.calls: list[list[str]] = []
        self.images_output = images_output
        self.ready = ready
        self.rm_ok = rm_ok

    async def __call__(self, argv, timeout, cwd=None):  # noqa: ANN001 - 与 EnvRunner 同形
        self.calls.append(list(argv))
        tail = argv[1:3]
        if tail == ["image", "ls"]:
            return (self.ready, self.images_output if self.ready else "docker: command not found")
        if tail == ["image", "rm"]:
            return (self.rm_ok, "" if self.rm_ok else "Error response from daemon: No such image")
        if tail == ["image", "inspect"]:
            return (True, "[]")
        return (False, "")

    @property
    def rm_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:3] == ["image", "rm"]]


def _manager(tmp_path: Path, runner: _FakeDockerRunner) -> ToolEnvManager:
    return ToolEnvManager(tmp_path / "envs", base_python="C:/python.exe", runner=runner)


def _record_env(manager: ToolEnvManager, requirements: list[str], image: str, *, used_at: str) -> str:
    """按真实记录形状造一个「已建过镜像的环境」：锁定清单 + container 段 + 最后使用时间。"""
    fingerprint = manager.fingerprint_for(requirements)
    record_path = manager.lock_record_for(requirements)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    tool_envs_module._write_json(
        record_path,
        {
            "schema": 2,
            "fingerprint": fingerprint,
            "requirements": list(requirements),
            "packages": [{"name": "six", "version": "1.17.0", "source": "", "hash": ""}],
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_used_at": used_at,
        },
    )
    manager._record_container(requirements, image)
    return fingerprint


def _referenced_table(manager: ToolEnvManager, *definitions) -> dict:
    return manager.references_from_definitions(list(definitions))


def _definition(name: str, requirements: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, requirements=list(requirements))


# -- 纯逻辑：解析 ----------------------------------------------------------


def test_parsing_keeps_only_qio_images_and_reads_their_size():
    output = _images_output(
        {"Repository": "qio-tool-env", "Tag": "3.11-abc123", "ID": "sha256:1", "Size": "1.2GB"},
        {"Repository": "python", "Tag": "3.11-slim", "ID": "sha256:2", "Size": "120MB"},
        {"Repository": "<none>", "Tag": "<none>", "ID": "sha256:3", "Size": "10MB"},
        {"Repository": "qio-tool-env", "Tag": "3.11-def456", "ID": "sha256:4", "Size": "456MB"},
    )
    images = parse_docker_images(output)

    assert [item["image"] for item in images] == [
        "qio-tool-env:3.11-abc123",
        "qio-tool-env:3.11-def456",
    ]
    assert [item["fingerprint"] for item in images] == ["3.11-abc123", "3.11-def456"]
    assert images[0]["size_bytes"] == 1_200_000_000
    assert images[1]["size_bytes"] == 456_000_000


def test_parsing_ignores_garbage_lines():
    assert parse_docker_images("not json\n{ broken\n\n") == []
    assert parse_docker_images(None) == []


# -- inventory 与引用判定 --------------------------------------------------


def test_inventory_marks_who_references_an_image(tmp_path: Path):
    image_b = _manager_image(tmp_path, "b")
    runner = _FakeDockerRunner()
    manager = _manager(tmp_path, runner)
    image_a = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image_a, used_at="2026-02-01T00:00:00+00:00")
    _record_env(manager, REQS_B, image_b, used_at="2026-02-02T00:00:00+00:00")
    table = _referenced_table(manager, _definition("six_tool", REQS_A))

    entries = {entry.image: entry for entry in manager.container_inventory(referenced_by=table)}

    referenced = entries[image_a]
    orphan = entries[image_b]
    assert referenced.state == "referenced"
    assert referenced.referenced_by == ["six_tool"]
    assert referenced.protected is True
    assert referenced.last_used_at == "2026-02-01T00:00:00+00:00"
    assert referenced.built_at, "构建时间来自锁定记录里的 container 段"
    assert orphan.state == "orphan"
    assert orphan.protected is False
    assert orphan.last_used_at == "2026-02-02T00:00:00+00:00"


def _manager_image(tmp_path: Path, seed: str) -> str:
    """另一个管理器算出来的 image 名（用来造「两个环境」的场景）。"""
    manager = ToolEnvManager(tmp_path / ("probe-" + seed), base_python="C:/python.exe")
    return manager.container_image_for(REQS_B)


def test_inventory_does_not_claim_absence_when_docker_is_missing(tmp_path: Path):
    runner = _FakeDockerRunner(ready=False)
    manager = _manager(tmp_path, runner)
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")

    available, images = asyncio_run(manager.list_local_images())
    assert available is False and images == []

    entry = manager.container_inventory(referenced_by={})[0]
    assert entry.present_locally is None, "不知道就是不知道，不能写成「本机没有」"


def _asyncio_run(coro):  # pragma: no cover - 这里只给一个统一入口
    import asyncio

    return asyncio.run(coro)


def asyncio_run(coro):
    return _asyncio_run(coro)


# -- 清理候选：被引用的绝不能出现 ------------------------------------------


def test_cleanup_candidates_never_include_a_referenced_image(tmp_path: Path):
    runner = _FakeDockerRunner()
    manager = _manager(tmp_path, runner)
    image_a = manager.container_image_for(REQS_A)
    image_b = manager.container_image_for(REQS_B)
    _record_env(manager, REQS_A, image_a, used_at="2026-02-01T00:00:00+00:00")
    _record_env(manager, REQS_B, image_b, used_at="2026-02-02T00:00:00+00:00")
    table = _referenced_table(manager, _definition("six_tool", REQS_A))

    candidates = manager.container_cleanup_candidates(
        referenced_by=table, docker_available=False, local_images=[]
    )

    assert [entry.image for entry in candidates] == [image_b]
    assert image_a not in [entry.image for entry in candidates]


# -- 显式清理：三道闸 ------------------------------------------------------


def test_removing_a_referenced_image_is_refused_even_with_confirmation(tmp_path: Path):
    runner = _FakeDockerRunner()
    manager = _manager(tmp_path, runner)
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")
    table = _referenced_table(manager, _definition("six_tool", REQS_A))

    result = asyncio_run(
        manager.remove_container_image(
            image, confirm=True, referenced_by=table, docker_available=True, local_images=[]
        )
    )

    assert result.removed is False
    assert "引用" in (result.reason or "")
    assert runner.rm_calls == [], "被引用的镜像连 rm 命令都不该发出去"
    assert result.referenced_by == ["six_tool"]


def test_removing_without_reference_information_is_refused(tmp_path: Path):
    runner = _FakeDockerRunner()
    manager = _manager(tmp_path, runner)
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")

    result = asyncio_run(
        manager.remove_container_image(image, confirm=True, docker_available=True, local_images=[])
    )

    assert result.removed is False
    assert "没有引用信息" in (result.reason or "")
    assert runner.rm_calls == []


def test_removing_an_orphan_needs_explicit_confirmation(tmp_path: Path):
    runner = _FakeDockerRunner()
    manager = _manager(tmp_path, runner)
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")

    refused = asyncio_run(
        manager.remove_container_image(
            image, confirm=False, referenced_by={}, docker_available=True, local_images=[]
        )
    )
    assert refused.removed is False and runner.rm_calls == []

    removed = asyncio_run(
        manager.remove_container_image(
            image, confirm=True, referenced_by={}, docker_available=True, local_images=[]
        )
    )
    assert removed.removed is True
    assert runner.rm_calls == [["docker", "image", "rm", image]]


def test_a_failed_docker_rm_is_reported_not_faked_as_removed(tmp_path: Path):
    runner = _FakeDockerRunner(rm_ok=False)
    manager = _manager(tmp_path, runner)
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")

    result = asyncio_run(
        manager.remove_container_image(
            image, confirm=True, referenced_by={}, docker_available=True, local_images=[]
        )
    )

    assert result.removed is False
    assert "No such image" in (result.reason or "")


def test_foreign_images_are_never_touched(tmp_path: Path):
    runner = _FakeDockerRunner()
    manager = _manager(tmp_path, runner)

    result = asyncio_run(
        manager.remove_container_image(
            "python:3.11-slim", confirm=True, referenced_by={}, docker_available=True
        )
    )

    assert result.removed is False
    assert "不碰" in (result.reason or "")
    assert runner.rm_calls == []


def test_docker_unavailable_blocks_removal_but_not_inventory(tmp_path: Path):
    runner = _FakeDockerRunner(ready=False)
    manager = _manager(tmp_path, runner)
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")

    result = asyncio_run(
        manager.remove_container_image(
            image, confirm=True, referenced_by={}, docker_available=False
        )
    )

    assert result.removed is False
    assert "docker" in (result.reason or "")
    assert runner.rm_calls == []
    assert manager.container_inventory(referenced_by={})[0].image == image


def test_forget_lock_drops_the_container_record(tmp_path: Path):
    runner = _FakeDockerRunner()
    manager = _manager(tmp_path, runner)
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")
    record_path = manager.lock_record_for(REQS_A)
    assert json.loads(record_path.read_text(encoding="utf-8"))["container"]["image"] == image

    result = asyncio_run(
        manager.remove_container_image(
            image,
            confirm=True,
            referenced_by={},
            forget_lock=True,
            docker_available=True,
            local_images=[],
        )
    )

    assert result.removed is True
    assert "container" not in json.loads(record_path.read_text(encoding="utf-8"))


def test_local_images_without_a_record_are_unknown_not_candidates(tmp_path: Path):
    stray = "qio-tool-env:3.11-deadbeef"
    runner = _FakeDockerRunner(
        _images_output({"Repository": "qio-tool-env", "Tag": "3.11-deadbeef", "Size": "10MB"})
    )
    manager = _manager(tmp_path, runner)
    available, images = asyncio_run(manager.list_local_images())
    assert available is True and [item["image"] for item in images] == [stray]

    entries = manager.container_inventory(
        referenced_by={}, local_images=images, docker_available=True
    )
    assert [entry.state for entry in entries] == ["unknown"]
    assert manager.container_cleanup_candidates(
        referenced_by={}, local_images=images, docker_available=True
    ) == []


# -- CLI：只做清单与候选，删除要显式声明「确认过没人用」 --------------------


def test_cli_lists_images_as_json(tmp_path: Path):
    root = tmp_path / "envs"
    manager = ToolEnvManager(root, base_python="C:/python.exe")
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")

    exit_code = tool_envs_module.main(["--root", str(root), "--json", "images"])

    assert exit_code == 0


def test_cli_refuses_to_clean_without_declaring_references(tmp_path: Path, capsys):
    root = tmp_path / "envs"
    manager = ToolEnvManager(root, base_python="C:/python.exe")
    image = manager.container_image_for(REQS_A)
    _record_env(manager, REQS_A, image, used_at="2026-02-01T00:00:00+00:00")

    exit_code = tool_envs_module.main(["--root", str(root), "rm-image", image, "--yes"])

    assert exit_code == 1
    assert "没有删除" in capsys.readouterr().out


# -- 真 docker：只有 ubuntu CI 能给出结论 ----------------------------------


@pytest.mark.requires_docker
async def test_real_docker_lists_and_removes_only_orphan_images(tmp_path: Path):
    """真 docker：清单来自 `docker image ls`，删除真的走 `docker image rm`。

    全程离线：用 `FROM scratch` 造一个本地镜像（不 pull、不联网），tag 成 QIO 的指纹格式。
    本机没有守护进程时按条件跳过；CI 上不允许跳过（跳过等于这条验证不存在）。
    """
    if not await sandbox_module.docker_daemon_ready():
        if os.environ.get("CI"):
            pytest.fail("CI 上必须有可用的 docker 守护进程：这条用例不能跳过")
        pytest.skip("本机没有 docker 守护进程：这条只能在 ubuntu CI 上真跑")

    import asyncio
    import subprocess

    manager = ToolEnvManager(tmp_path / "envs", base_python="C:/python.exe")
    orphan_fp = "ci" + os.getpid().__str__() + "a"
    referenced_fp = "ci" + os.getpid().__str__() + "b"
    orphan_image = f"{tool_envs_module.CONTAINER_IMAGE_PREFIX}3.11-{orphan_fp}"
    referenced_image = f"{tool_envs_module.CONTAINER_IMAGE_PREFIX}3.11-{referenced_fp}"
    context = tmp_path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\nLABEL qio.test=\"1\"\n", encoding="utf-8")

    def build(tag: str) -> None:
        subprocess.run(
            ["docker", "build", "-t", tag, str(context)],
            check=True,
            capture_output=True,
            timeout=180,
        )

    build(orphan_image)
    build(referenced_image)
    try:
        available, images = asyncio.run(manager.list_local_images())
        assert available is True
        names = [item["image"] for item in images]
        assert orphan_image in names and referenced_image in names

        # 让 referenced_image 成为「被注册工具引用」的镜像（tag 就是那个环境的身份）
        tool_envs_module._write_json(
            manager.locks_root / referenced_fp / tool_envs_module.LOCK_RECORD_NAME,
            {"requirements": REQS_A, "packages": [], "created_at": "2026-01-01T00:00:00+00:00"},
        )
        manager._record_container(REQS_A, referenced_image)
        # 让 referenced_image 的指纹与它记录的依赖集合一致：直接改写成被测环境的指纹
        record_path = manager.locks_root / referenced_fp / tool_envs_module.LOCK_RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["requirements"] = [f"pkg-for-{referenced_fp}"]
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        table = _referenced_table(manager, _definition("six_tool", [f"pkg-for-{referenced_fp}"]))

        inventory = {entry.image: entry for entry in manager.container_inventory(
            referenced_by=table, local_images=images, docker_available=True
        )}
        assert inventory[referenced_image].protected is True
        assert inventory[orphan_image].state == "unknown"

        refused = await manager.remove_container_image(
            referenced_image,
            confirm=True,
            referenced_by=table,
            local_images=images,
            docker_available=True,
        )
        assert refused.removed is False
        still_there = subprocess.run(
            ["docker", "image", "inspect", referenced_image], capture_output=True, timeout=60
        )
        assert still_there.returncode == 0, "被引用的镜像绝不能被删掉"

        removed = await manager.remove_container_image(
            orphan_image,
            confirm=True,
            referenced_by=table,
            local_images=images,
            docker_available=True,
        )
        assert removed.removed is True, removed.reason
        gone = subprocess.run(
            ["docker", "image", "inspect", orphan_image], capture_output=True, timeout=60
        )
        assert gone.returncode != 0
    finally:
        for tag in (orphan_image, referenced_image):
            subprocess.run(
                ["docker", "image", "rm", "-f", tag], capture_output=True, timeout=60, check=False
            )