from __future__ import annotations

import pytest

from agent.tools.fs_tools import (
    FsReadTool,
    FsWriteTool,
    FsPatchTool,
    FsListTool,
    FsFindTool,
    FsInfoTool,
)


class _FakeSandbox:
    def __init__(self, verdict="auto", mode="default") -> None:
        self._verdict = verdict
        self._mode = mode

    def check_path(self, path: str) -> str:
        return self._verdict

    def read_verdict(self, path: str) -> str:
        return self._verdict

    def write_verdict(self, path: str) -> str:
        return self._verdict

    def command_verdict(self, cmd: str):
        return self._verdict, "low"

    @property
    def mode(self) -> str:
        return self._mode


class _FakeApproval:
    def __init__(self, decision="approved") -> None:
        self._decision = decision
        self.calls: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict):
        self.calls.append((kind, payload))
        return type("R", (), {"decision": self._decision})()


@pytest.mark.asyncio
async def test_fs_read_within_root(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    t = FsReadTool()
    t.computer = _FakeSandbox("auto")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(target))
    assert res.ok
    assert res.content == "hello"


@pytest.mark.asyncio
async def test_fs_read_redline_denied(tmp_path):
    t = FsReadTool()
    t.computer = _FakeSandbox("deny")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(tmp_path / ".env"))
    assert not res.ok
    assert "拒绝" in res.error


@pytest.mark.asyncio
async def test_fs_read_outside_root_approve(tmp_path):
    target = tmp_path / "other.txt"
    target.write_text("outside", encoding="utf-8")
    t = FsReadTool()
    t.computer = _FakeSandbox("approve")
    appr = _FakeApproval()
    t.approvals = appr
    res = await t.run(path=str(target))
    # 根外读走审批；approved → 执行读
    assert res.ok
    assert res.content == "outside"
    assert len(appr.calls) == 1


@pytest.mark.asyncio
async def test_fs_write_redline_denied(tmp_path):
    t = FsWriteTool()
    t.computer = _FakeSandbox("deny")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(tmp_path / ".env"), content="SECRET=1")
    assert not res.ok
    assert "拒绝" in res.error


@pytest.mark.asyncio
async def test_fs_write_approved(tmp_path):
    target = tmp_path / "b.txt"
    t = FsWriteTool()
    t.computer = _FakeSandbox("approve")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(target), content="hi")
    assert res.ok
    assert target.read_text(encoding="utf-8") == "hi"


@pytest.mark.asyncio
async def test_fs_write_rejected(tmp_path):
    t = FsWriteTool()
    t.computer = _FakeSandbox("approve")
    t.approvals = _FakeApproval(decision="rejected")
    res = await t.run(path=str(tmp_path / "c.txt"), content="hi")
    assert not res.ok
    assert "未获批准" in res.error


@pytest.mark.asyncio
async def test_fs_patch_edits_in_place(tmp_path):
    target = tmp_path / "d.txt"
    target.write_text("line1\nline2\n", encoding="utf-8")
    t = FsPatchTool()
    t.computer = _FakeSandbox("approve")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(target), old="line1", new="LINE1")
    assert res.ok
    assert target.read_text(encoding="utf-8") == "LINE1\nline2\n"


@pytest.mark.asyncio
async def test_fs_patch_missing_old_raises(tmp_path):
    target = tmp_path / "e.txt"
    target.write_text("aaa\n", encoding="utf-8")
    t = FsPatchTool()
    t.computer = _FakeSandbox("approve")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(target), old="zzz", new="x")
    assert not res.ok


@pytest.mark.asyncio
async def test_fs_list(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    t = FsListTool()
    t.computer = _FakeSandbox("auto")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(tmp_path))
    assert res.ok
    assert "a.txt" in res.content and "b.txt" in res.content


@pytest.mark.asyncio
async def test_fs_find(tmp_path):
    (tmp_path / "needle.txt").write_text("n", encoding="utf-8")
    (tmp_path / "other.md").write_text("o", encoding="utf-8")
    t = FsFindTool()
    t.computer = _FakeSandbox("auto")
    t.approvals = _FakeApproval()
    res = await t.run(query="needle", dir=str(tmp_path))
    assert res.ok
    assert "needle.txt" in res.content


@pytest.mark.asyncio
async def test_fs_info(tmp_path):
    target = tmp_path / "info.txt"
    target.write_text("x" * 10, encoding="utf-8")
    t = FsInfoTool()
    t.computer = _FakeSandbox("auto")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(target))
    assert res.ok
    assert "info.txt" in res.content


@pytest.mark.asyncio
async def test_fs_missing_path_rejected():
    t = FsReadTool()
    t.computer = _FakeSandbox("auto")
    t.approvals = _FakeApproval()
    res = await t.run()
    assert not res.ok
    assert "必填" in res.error
