# -*- coding: utf-8 -*-
"""D 独立契约测试：工具开发闭环（契约 §14.4、§14.7）。

验收点：
* 完整版本摘要进入 ToolResult.data（模型可见），不再只存在 facts；
  dev_list_tasks / dev_run_tests / dev_submit_tool / declare_completion 都返回当前完整版本证据；
* 模拟测试 != 真实可用：claims 保留 test_passed/registered/usable，真实验证状态单独保存；
* 已放弃任务允许只读查看 / 材料复用，禁止写入/测试/提交；重新开发创建新任务；
* 工具自身重大缺陷可停用（保留工作）、修复验证后恢复；
* 文件/命令/进程/运行环境表述与实际能力一致（受限子进程不得称安全沙箱）。

装置：真实 DevWorkspace + AppContext；fake 模型；实现未合入时按契约失败。
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path

import pytest

CLAUSE = "契约 §14.4/§14.7"
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src" / "agent"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：{name} 不可导入（实现未合入）：{exc!r}", pytrace=False)


def _run(value):
    if inspect.iscoroutine(value):
        return asyncio.run(value)
    return value


import asyncio  # noqa: E402


@pytest.fixture()
def ctx(db_conn, settings):
    from agent.api.server import create_app
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    context = app.state.ctx
    context.credentials._kr = MemoryKeyring()
    return context


def _workspace(ctx):
    workspace = ctx.dev_workspaces
    task = workspace.create("生成一个把会议记录整理成清单的工具")
    from agent.tools.spec import ToolDefinition

    workspace.write_definition(task.id, ToolDefinition(
        name="meeting_digest", description="整理会议记录", tool_type="function",
        code="def run(**kwargs):\n    return {'ok': True}",
        tests=[{"name": "t1", "input": {}, "expect": {"ok": True}}],
    ))
    workspace.record_test(task.id, True, "1/1 tests passed (simulated)")
    return workspace, task


def _tool(ctx, name):
    tool = ctx.registry.get(name)
    if tool is None:
        pytest.fail(f"{CLAUSE}：工具 {name} 未注册（实现未合入）", pytrace=False)
    return tool


def test_dev_list_tasks_returns_full_version_evidence_to_model(ctx):
    """§14.4：完整版本摘要必须进入模型可见的 data，不只存在 facts。"""
    workspace, task = _workspace(ctx)
    result = _run(_tool(ctx, "dev_list_tasks").run())
    assert result.ok is True, f"{CLAUSE}：dev_list_tasks 失败：{result.error}"
    data = result.data or {}
    assert data, f"{CLAUSE}：dev_list_tasks 没有 data（模型拿不到完整版本证据）"
    text = json.dumps(data, ensure_ascii=False)
    assert task.id in text, f"{CLAUSE}：data 缺少任务 id"
    assert "version" in text, f"{CLAUSE}：data 缺少当前完整版本摘要（version）"
    assert "test" in text.lower(), f"{CLAUSE}：data 缺少测试证据状态"
    facts = result.facts or {}
    if facts:
        assert text != json.dumps(facts, ensure_ascii=False), (
            f"{CLAUSE}：完整证据不能只放在 facts（模型看不到）")


def test_four_dev_tools_include_data_version_summary():
    """§14.4：四个关键工具的模型可见结果都带完整版本摘要。"""
    source = (SRC / "tools" / "dev_tools.py").read_text(encoding="utf-8")
    completion = (SRC / "tools" / "declare_completion.py").read_text(encoding="utf-8")
    missing = []
    for name, text in (
        ("dev_list_tasks", source),
        ("dev_run_tests", source),
        ("dev_submit_tool", source),
        ("declare_completion", completion),
    ):
        # 找到该工具类之后直到下一个 class 的代码段，检查成功返回带 data= 且含 version
        pattern = re.compile(r"class\\s+(\\w*" + name.replace("_", "") + r"\\w*)", re.IGNORECASE)
        match = pattern.search(text)
        if match is None:
            pattern = re.compile(r"name\\s*=\\s*\"" + re.escape(name) + r"\"")
            match = pattern.search(text)
        section = text[match.start():] if match else text
        next_class = section.find("\nclass ", 1)
        if next_class > 0:
            section = section[:next_class]
        if "data=" not in section:
            missing.append(name + " 无 data=")
            continue
        if not any(token in section for token in ("version", "version_summary", "content_digest")):
            missing.append(name + " data 无 version")
    assert not missing, (
        f"{CLAUSE}：以下工具没有把完整版本摘要放进 ToolResult.data：{missing}")


def test_simulated_tests_do_not_make_tool_usable(ctx):
    """§14.4：模拟测试 + 注册不能代表真实可用；真实验证状态单独保存。"""
    workspace, task = _workspace(ctx)
    status = workspace.status(task.id)
    assert "test_evidence_current" in status, f"{CLAUSE}：状态缺少测试证据字段"
    text = json.dumps(status, ensure_ascii=False)
    assert "real" in text.lower() or "真实" in text, (
        f"{CLAUSE}：状态未区分「模拟测试通过」与「真实条件验证」：{text[:400]}")
    tool = _tool(ctx, "declare_completion")
    result = _run(tool.run(task_id=task.id, claims={
        "test_passed": True, "registered": True, "usable": True}))
    payload = json.dumps(result.data or {}, ensure_ascii=False) + str(result.content or "") + str(result.error or "")
    assert result.ok is False or "待验证" in payload or "real_verification" in payload or "真实" in payload, (
        f"{CLAUSE}：只有模拟测试时不得声称真实可用：{payload[:500]}")
    prompts = _load("agent.prompts")
    assert "注册和模拟测试不能替代必要的真实使用条件验证" in prompts.DEV_GUIDE_SECTIONS["claims"], (
        f"{CLAUSE}：claims 指南必须说明模拟测试不能替代真实验证")


def test_abandoned_task_is_read_only_and_material_reusable(ctx):
    """§14.4：已放弃任务允许只读查看/材料复用，禁止写入/测试/提交。"""
    workspace, task = _workspace(ctx)
    try:
        workspace.abandon(task.id)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：无法放弃任务（前置失败）：{exc!r}", pytrace=False)
    list_files = _run(_tool(ctx, "dev_list_files").run(workspace=task.id))
    read_file = _run(_tool(ctx, "dev_read_file").run(workspace=task.id, name="tool.json"))
    assert list_files.ok is True, (
        f"{CLAUSE}：已放弃任务必须允许只读查看文件（实际 {list_files.error}）")
    assert read_file.ok is True, (
        f"{CLAUSE}：已放弃任务必须允许读取材料复用（实际 {read_file.error}）")
    write = _run(_tool(ctx, "dev_write_file").run(
        workspace=task.id, name="x.py", content="print(1)"))
    run_tests = _run(_tool(ctx, "dev_run_tests").run(workspace=task.id))
    submit = _run(_tool(ctx, "dev_submit_tool").run(
        workspace=task.id, explanation="继续提交"))
    for name, result in (("write", write), ("run_tests", run_tests), ("submit", submit)):
        assert result.ok is False, (
            f"{CLAUSE}：已放弃任务不得{name}（实际 {result}）")
        payload = str(result.error or "") + str(result.data or "") + str(result.content or "")
        assert "放弃" in payload or "abandoned" in payload.lower() or "只读" in payload, (
            f"{CLAUSE}：{name} 拒绝原因必须说明任务已放弃：{payload[:300]}")


def test_dev_workspace_supports_suspend_and_resume():
    """§14.4：工具自身重大缺陷时可暂时停用（保留工作），修复验证后恢复。"""
    from pathlib import Path

    source = (SRC / "tools" / "dev_workspace.py").read_text(encoding="utf-8")
    for name in ("suspend", "resume"):
        assert re.search(rf"def {name}\\(", source), (
            f"{CLAUSE}：DevWorkspace 缺少 {name}（停用/恢复能力未实现）")
    assert "保留" in source, f"{CLAUSE}：停用必须保留工作（未见保留语义）"


def test_restricted_subprocess_is_not_called_safe_sandbox():
    """§14.4：受限子进程不得称为安全沙箱；表述与实际能力一致。"""
    files = [
        SRC / "tools" / "sandbox.py",
        SRC / "tools" / "dev_tools.py",
        SRC / "prompts.py",
    ]
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer("安全沙箱", text):
            window = text[max(0, match.start() - 30): match.start()]
            if not any(token in window for token in ("不是", "不等于", "不构成", "并非", "不能称为")):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"{CLAUSE}：出现「安全沙箱」正面表述：{offenders}")
    prompts = _load("agent.prompts")
    assert "受限子进程不等于安全沙箱" in prompts.DEV_GUIDE_SECTIONS["contract"], (
        f"{CLAUSE}：开发指南契约节必须写明受限子进程不等于安全沙箱")


def test_real_verification_state_is_stored_separately():
    """§14.4：真实验证状态单独保存（不重命名旧协议 test_passed/registered/usable）。"""
    from agent.tools.dev_workspace import DevWorkspace

    source = (SRC / "tools" / "dev_workspace.py").read_text(encoding="utf-8")
    assert "real_verification" in source or "真实" in source, (
        f"{CLAUSE}：DevWorkspace 未保存真实验证状态（与模拟测试分开）")
    status_source = source[source.find("def status"):]
    assert "last_test_passed" in status_source, (
        f"{CLAUSE}：旧协议 test_passed 必须保留（不重命名、不弱化）")
    combined = source + (SRC / "tools" / "declare_completion.py").read_text(encoding="utf-8")
    for claim in ("test_passed", "registered", "usable"):
        assert claim in combined, f"{CLAUSE}：机器 claims 字段 {claim} 被移除"


def test_evidence_becomes_stale_when_content_changes(ctx):
    """§14.4：证据按当前版本判定；内容变化后不得继续当作有效证据。"""
    workspace, task = _workspace(ctx)
    status = workspace.status(task.id)
    assert status.get("test_evidence_current") is True, (
        f"{CLAUSE}：刚写入并测试的版本证据应为 current")
    workspace.write_file(task.id, "extra.py", "print('changed')")
    status = workspace.status(task.id)
    assert status.get("test_evidence_current") is False, (
        f"{CLAUSE}：内容变化后旧测试证据必须失效（current=False）")
    assert status.get("evidence_state") in ("stale", "none"), (
        f"{CLAUSE}：证据状态必须标为 stale/none（实际 {status.get('evidence_state')}）")
