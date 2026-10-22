"""R9-F W3：fs_write / fs_patch / fs_delete 的后端来源核实（契约 §3.4）。

* 真实 FileOriginStore + 真实临时 DB/文件；
* fake 网关只用于记录 effect（验证"后端核实过的 file_origin 传给授权引擎"），
  不冒充端到端授权结论；
* 重点：创建→修改→回收归属正确；用户文件不被冒充生成物；模型传入 origin 不被信任；
  原子替换更新身份；外部替换不继承；删除后来源事实保留。
"""

from __future__ import annotations

import os

import pytest

from agent.security.effects import AuthDecision, Verdict
from agent.services import file_origins as fo
from agent.services.computer import ComputerSandbox
from agent.services.recycle_bin import send_to_recycle_bin
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.fs_tools import FsDeleteTool, FsPatchTool, FsWriteTool


class RecordingGateway:
    """只记录 effect / 返回 allow 的 fake 网关（接口级测试，不是端到端权限判定）。"""

    def __init__(self) -> None:
        self.effects: list = []

    def mode(self):
        return "restricted"

    def instruction(self):
        return None

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        return AuthDecision(verdict=Verdict.ALLOW, reason="fake: allow (接口测试)")

    async def submit(self, operation, *, ctx=None):  # pragma: no cover - 本文件不测审批
        raise AssertionError("本文件不应提交审批")


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    conn = connect(tmp_path / "origins_fs.db")
    apply_migrations(conn)
    store = fo.FileOriginStore(conn, tmp_path / "workspace-root")
    sandbox = ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: "restricted"
    )
    gateway = RecordingGateway()
    yield {"root": root, "store": store, "sandbox": sandbox, "gateway": gateway}
    conn.close()


def _tool(cls, env):
    return cls(computer=env["sandbox"], security=env["gateway"], origins=env["store"])


def _last_effect(env):
    assert env["gateway"].effects, "授权网关没有收到 effect"
    return env["gateway"].effects[-1]


@pytest.mark.asyncio
async def test_create_edit_recycle_tracks_origin(env):
    """创建（generated）→ 编辑（保留 generated）→ 回收（保留来源、标记 missing）。"""
    root = env["root"]
    tool = _tool(FsWriteTool, env)

    created = await tool.run(path="报告.md", content="第一版")
    assert created.ok, created.error
    report = root / "报告.md"
    assert report.is_file()
    first = env["store"].lookup(report)
    assert first is not None and first.origin == fo.ORIGIN_QIO_GENERATED
    first_identity = first.identity_key
    create_effect = _last_effect(env)
    assert create_effect.detail["file_origin"] is None, "新文件创建前来源未知"
    assert create_effect.detail["file_exists"] is False

    edited = await tool.run(path="报告.md", content="第二版：整理后的内容")
    assert edited.ok, edited.error
    edit_effect = _last_effect(env)
    assert edit_effect.detail["file_origin"] == fo.ORIGIN_QIO_GENERATED, (
        "编辑已有生成物时，授权引擎必须看到后端核实过的 generated"
    )
    assert edit_effect.detail["file_exists"] is True
    after_edit = env["store"].lookup(report)
    assert after_edit is not None and after_edit.origin == fo.ORIGIN_QIO_GENERATED

    # 回收：文件进回收站（测试里用假 mover 删除），来源事实保留为 missing
    def _fake_recycle(path):
        os.unlink(path)
        return {"ok": True, "mechanism": "fake-recycle", "deleted_permanently": False}

    monkeypatch = pytest.MonkeyPatch()
    try:
        import agent.services.recycle_bin as recycle_bin

        monkeypatch.setattr(recycle_bin, "send_to_recycle_bin", _fake_recycle)
        deleted = await _tool(FsDeleteTool, env).run(paths=[str(report)])
    finally:
        monkeypatch.undo()
    assert deleted.ok, deleted.error
    assert not report.exists()
    gone = env["store"].lookup(report)
    assert gone is not None, "删除后来源事实必须保留（历史归属可查）"
    assert gone.origin == fo.ORIGIN_QIO_GENERATED
    assert gone.missing_at
    delete_effect = _last_effect(env)
    assert delete_effect.detail["file_origin"] == fo.ORIGIN_QIO_GENERATED
    # 创建→编辑→回收期间身份确实变化过（原子替换的 OS 事实），来源没变
    assert after_edit.identity_key != first_identity


@pytest.mark.asyncio
async def test_existing_user_file_is_not_claimed_generated(env):
    """已存在的用户文件：授权引擎看到的是 user_original，不是模型声称的 generated。"""
    root = env["root"]
    user_file = root / "用户笔记.txt"
    user_file.write_text("用户自己的内容", encoding="utf-8")
    tool = _tool(FsWriteTool, env)

    result = await tool.run(
        path="用户笔记.txt",
        content="用户自己的内容（已被 QIO 追加一行）",
        # 模拟模型传入的 origin：工具必须忽略它，用后端核实值
        origin="qio_generated",
    )
    assert result.ok, result.error
    effect = _last_effect(env)
    assert effect.detail["file_origin"] is None, "写入前没有来源记录，不得凭空声称 generated"
    assert "qio_generated" not in str(effect.detail), "模型传入 origin 不得进入 effect"
    record = env["store"].lookup(user_file)
    assert record is not None and record.origin == fo.ORIGIN_USER_ORIGINAL, (
        "已存在的用户文件写入后只能记为 user_original，不得冒充生成物"
    )

    # 第二次写入：引擎拿到后端核实过的 user_original
    again = await tool.run(path="用户笔记.txt", content="再改一次", origin="qio_generated")
    assert again.ok, again.error
    assert _last_effect(env).detail["file_origin"] == fo.ORIGIN_USER_ORIGINAL


@pytest.mark.asyncio
async def test_patch_keeps_origin(env):
    """编辑保留来源：用户文件 patch 后仍 user_original；生成物 patch 后仍 generated。"""
    root = env["root"]
    user_file = root / "配置.txt"
    user_file.write_text("OLD=1", encoding="utf-8")
    patch_tool = _tool(FsPatchTool, env)

    patched = await patch_tool.run(path="配置.txt", old="OLD=1", new="OLD=2")
    assert patched.ok, patched.error
    record = env["store"].lookup(user_file)
    assert record is not None and record.origin == fo.ORIGIN_USER_ORIGINAL

    # QIO 生成物：先创建，再 patch，归属保持 generated
    write_tool = _tool(FsWriteTool, env)
    assert (await write_tool.run(path="生成配置.txt", content="A=1")).ok
    generated = env["store"].lookup(root / "生成配置.txt")
    assert generated is not None and generated.origin == fo.ORIGIN_QIO_GENERATED
    patched2 = await patch_tool.run(path="生成配置.txt", old="A=1", new="A=2")
    assert patched2.ok, patched2.error
    assert _last_effect(env).detail["file_origin"] == fo.ORIGIN_QIO_GENERATED
    kept = env["store"].lookup(root / "生成配置.txt")
    assert kept is not None and kept.origin == fo.ORIGIN_QIO_GENERATED


@pytest.mark.asyncio
async def test_external_replacement_does_not_inherit_generated(env):
    """外部替换（新身份）后再写：不得把外部文件继承成 QIO 生成物。"""
    root = env["root"]
    tool = _tool(FsWriteTool, env)
    assert (await tool.run(path="生成物.txt", content="qio-v1")).ok
    target = root / "生成物.txt"
    assert env["store"].is_generated(target)

    # 外部程序删除并重建同名文件（os.replace 模拟原子替换）
    temp = root / "生成物.txt.external"
    temp.write_text("someone-else", encoding="utf-8")
    os.replace(temp, target)

    assert env["store"].lookup(target) is None, "外部替换的文件不得继承旧来源"
    result = await tool.run(path="生成物.txt", content="qio-v2")
    assert result.ok, result.error
    effect = _last_effect(env)
    assert effect.detail["file_origin"] is None, "写前来源未知（外部文件），不得声称 generated"
    record = env["store"].lookup(target)
    assert record is not None and record.origin == fo.ORIGIN_USER_ORIGINAL, (
        "外部替换后的同名文件只能被保守当作 user_original"
    )
