"""审批内容必须让普通用户看懂（第三阶段 spec 第 66~70 条）。

第一阶段保证了审批**安全**；这里守住审批**可理解**：
用户要能回答「想做什么 / 会访问什么 / 影响 / 一次性还是长期」，
而不是看到 `fs_write path=...` 或内部术语。
"""

from __future__ import annotations

import asyncio
import json

from agent.adapters.base import ToolCall
from agent.tools.approval import ApprovalResult
from agent.tools.approval_present import describe_tool_call
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _StubApprovals:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **_: object) -> ApprovalResult:
        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", "approved")


class _WriteTool(Tool):
    name = "fs_write"
    description = "写入文件"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    requires_approval = True

    async def run(self, **kwargs: object) -> ToolResult:
        return ToolResult(ok=True, content="written")


def test_fs_write_describes_files_not_raw_arguments():
    one = describe_tool_call("fs_write", {"path": "a.txt", "content": "x"})
    assert one["description"] == "想修改当前项目中的一个文件"
    assert one["access"] == ["写入：a.txt"]
    assert one["scope"] == "once"
    assert "写入文件：是" in one["capabilities"]
    assert "副作用：write" in one["capabilities"]

    many = describe_tool_call(
        "fs_write", {"paths": ["a.txt", "b.txt", "c.txt"], "content": "x"}
    )
    assert many["description"] == "想修改当前项目中的 3 个文件"
    assert len(many["access"]) == 3


def test_run_shell_is_careful_and_points_at_the_detail():
    out = describe_tool_call("run_shell", {"cmd": "python build.py"})
    assert out["description"] == "想运行一条 shell 命令"
    assert out["risk"] == "careful"
    assert "启动进程：是" in out["capabilities"]
    # 命令原文进详情，不放首屏描述里
    assert "python build.py" in out["detail"]
    assert "python build.py" not in out["description"]


def test_run_program_shows_program_and_argv():
    out = describe_tool_call("run_program", {"program": "git", "argv": ["status", "-s"]})
    assert out["description"] == "想运行一个程序"
    assert out["detail"].startswith("程序：git")
    assert "status -s" in out["detail"]


def test_proc_kill_is_careful():
    out = describe_tool_call("proc_kill", {"pid": "1234"})
    assert out["description"] == "想结束进程 1234"
    assert out["risk"] == "careful"
    assert "副作用：destructive" in out["capabilities"]


def test_tool_registration_is_long_term():
    out = describe_tool_call("dev_submit_tool", {"workspace": "dev_1", "definition": "{}"})
    assert out["scope"] == "long_term"
    assert "一直可用" in " ".join(out["access"])

    draft = describe_tool_call("create_tool", {"request": "把 Excel 转成 CSV"})
    assert draft["scope"] == "long_term"


def test_network_tools_say_network():
    search = describe_tool_call("web_search", {"query": "QIO"})
    assert "联网：是" in search["capabilities"]
    assert any("QIO" in a for a in search["access"])

    fetch = describe_tool_call("web_fetch", {"url": "https://example.com/x"})
    assert "联网：是" in fetch["capabilities"]
    assert any("example.com" in a for a in fetch["access"])


def test_unknown_tool_does_not_invent_behavior():
    out = describe_tool_call("totally_custom_tool", {"whatever": 1})
    assert out["description"] == "想执行「totally_custom_tool」"
    assert out["access"] == []
    assert out["capabilities"] == []


def test_no_internal_terms_leak_into_user_visible_fields():
    out = describe_tool_call("fs_write", {"path": "a.txt"})
    blob = json.dumps(out, ensure_ascii=False)
    for term in ("fingerprint", "policy_hash", "sandbox", "keychain"):
        assert term not in blob


def test_registry_approval_payload_carries_human_description():
    """审批事件里的 payload 必须带人话描述，而不是只有工具名与参数。"""
    approvals = _StubApprovals()
    registry = ToolRegistry(approvals=approvals)
    registry.register(_WriteTool())

    result = asyncio.run(
        registry.execute(ToolCall(id="c1", name="fs_write", arguments={"path": "a.txt"}))
    )
    assert result.ok
    assert approvals.requests, "审批没有被请求"
    kind, payload = approvals.requests[0]
    assert kind == "tool_execution"
    assert payload["description"] == "想修改当前项目中的一个文件"
    assert payload["scope"] == "once"
    assert payload["access"] == ["写入：a.txt"]
    # 原始调用参数仍然保留（高级详情要用），但不再是用户唯一能看到的东西
    assert payload["arguments"] == {"path": "a.txt"}


def test_computer_action_approvals_are_also_human_readable():
    """文件 / 命令 / 进程类审批走的是 `kind="computer"`，同样必须说人话。"""
    from agent.tools.approval_present import describe_computer_action

    write = describe_computer_action({"action": "write", "path": "C:/tmp/out.txt"})
    assert write["description"] == "想修改当前项目中的一个文件"
    assert write["access"] == ["写入：C:/tmp/out.txt"]
    assert write["scope"] == "once"

    shell = describe_computer_action({"action": "run_shell", "cmd": "dir", "risk": "danger"})
    assert shell["description"] == "想运行一条 shell 命令"
    assert "启动进程：是" in shell["capabilities"]
    assert "dir" in shell["detail"]
    assert "danger" in shell["detail"]  # 沙箱判定保留在详情里

    program = describe_computer_action(
        {"action": "run_program", "program": "git", "args": ["status"], "risk": "caution"}
    )
    assert program["description"] == "想运行一个程序"
    assert "git" in program["detail"]

    kill = describe_computer_action({"action": "proc_kill", "pid": "42"})
    assert kill["description"] == "想结束进程 42"
    # 界面风险靠能力标签表达；`risk` 不能覆盖 sandbox 的机器可读判定
    assert "risk" not in kill
    assert "副作用：destructive" in kill["capabilities"]

    unknown = describe_computer_action({"action": "something_new"})
    assert unknown["description"] == "这项操作需要你的确认"
    assert unknown["access"] == []


def test_computer_action_payload_keeps_raw_fields():
    """行为化描述只能**增补**字段，不能替掉机器可读的原始载荷。"""
    from agent.tools.approval_present import describe_computer_action

    raw = {"action": "run_program", "program": "git", "args": ["status"], "risk": "caution"}
    merged = dict(raw)
    merged.update(describe_computer_action(raw))
    assert merged["action"] == "run_program"
    assert merged["program"] == "git"
    assert merged["args"] == ["status"]
    assert merged["risk"] == "caution"  # sandbox 判定不被覆盖
    assert merged["description"] == "想运行一个程序"
