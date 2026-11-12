"""#12 文件实际读取失败：后端结构化事实、轮内事件与三处去重。

用户任务（0.3.2 PM 记录/反馈组 #12）：文件**实际读取失败**时，界面应在受影响的
文件或回复旁显示一次可理解的提示；工具名与诊断按需展开，面向模型的内部指令不上屏。
判定只认结构化事实（tool_name / code / category / facts），不从正文关键词猜；
不得把权限 / 审批 / 限额 / 格式不支持等其它结果归成文件读取失败。

本文件锁住：
* 三条真实读取失败路径（路径不可用 / 坏 ZIP / OSError）统一 code=file_read_failed，
  并给出用户安全的 facts["file_read"]（reason / next_step 不含模型指令，diagnostic 脱敏）；
* 模型可见正文（tool_feedback.message）与改动前逐字一致（fail-closed：增量事实不改语义）；
* 权限 / 等待审批 / 限额 / 格式不支持 / 准备中不产生文件读取失败事实；
* TOOL_END 事件带上 file_read（缺失时如实为 None）；
* turn_facts 每个受影响文件只出 1 条可读事实，答案注记不重复轮内提示；
* task_feed 不把单次 read_attachment 文件读取失败另列一条，执行记录原样保留。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import AdapterMode, ToolCall
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.core import tool_feedback
from agent.core.loop import AgentLoop
from agent.core.turn_facts import TurnFacts
from agent.services import attachment_access
from agent.services.attachments import AttachmentService
from agent.storage import tool_records
from agent.tools import attachment_tools as tool_mod
from agent.tools.attachment_tools import AttachmentLimitExceeded, ReadAttachmentTool
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

# 面向模型的内部指令：用户可见事实里绝不能出现这些句子。
_MODEL_ONLY_INSTRUCTIONS = ("可以让用户", "可以先不传", "请提供 attachment_id")


# ── 装置：真实文件事实，不伪造读取结果 ──────────────────────────────────────


@pytest.fixture()
def svc(db_conn: sqlite3.Connection, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _prepare(svc: AttachmentService, path: Path, *, turn_id: str | None = "turn_1"):
    att = svc.prepare(str(path), topic_id="t1", turn_id=turn_id)
    return svc.run_prepare(att.id)


def _tool(
    svc: AttachmentService, turn_id: str | None = "turn_1", *, security=None
) -> ReadAttachmentTool:
    return ReadAttachmentTool(svc, active_turn_id=lambda: turn_id, security=security)


def _assert_user_safe(fact: dict) -> None:
    """reason / next_step 必须是用户可读、绝不含模型指令的结构化事实。"""
    assert isinstance(fact, dict)
    assert fact.get("attachment_id")
    assert fact.get("name")
    assert fact.get("reason")
    assert fact.get("next_step")
    blob = json.dumps(fact, ensure_ascii=False)
    for phrase in _MODEL_ONLY_INSTRUCTIONS:
        assert phrase not in blob, f"用户可见事实泄露了模型指令：{phrase} / {blob}"


# ── 1. 三条真实失败路径：稳定 code + 用户安全 facts ─────────────────────────


async def _path_missing_case(svc: AttachmentService, tmp_path: Path):
    source = _write(tmp_path / "丢了.txt", "会被删掉".encode("utf-8"))
    att = _prepare(svc, source)
    Path(att.stored_path).unlink()
    source.unlink()
    return att, await _tool(svc).run(attachment_id=att.id)


async def _bad_zip_case(svc: AttachmentService, tmp_path: Path):
    path = _write(tmp_path / "损坏.docx", b"this is not a zip file at all" * 10)
    att = _prepare(svc, path)
    return att, await _tool(svc).run(attachment_id=att.id)


async def _oserror_case(svc: AttachmentService, tmp_path: Path, monkeypatch):
    source = _write(tmp_path / "读盘失败.txt", "内容".encode("utf-8"))
    att = _prepare(svc, source)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003 - 注入真实 OSError 分支
        raise OSError("盘读取失败 api_key=sk-abcdef123456")

    monkeypatch.setattr(tool_mod, "_extract_text", _boom)
    return att, await _tool(svc).run(attachment_id=att.id)


async def test_path_unavailable_failure_has_stable_code_and_safe_facts(svc, tmp_path):
    att, result = await _path_missing_case(svc, tmp_path)
    assert result.ok is False
    assert result.code == "file_read_failed"
    assert result.category == "unavailable", "category 保留原来的失败类别"
    fact = (result.facts or {}).get("file_read")
    _assert_user_safe(fact)
    assert fact["attachment_id"] == att.id
    assert fact["name"] == att.original_name
    # 模型可见正文必须保留原有原因与指令（增量事实不改模型语义）
    assert "读不到附件" in (result.error or "")
    assert "可以让用户重新指定位置后再读" in (result.error or "")


async def test_corrupt_zip_failure_has_stable_code_and_safe_facts(svc, tmp_path):
    att, result = await _bad_zip_case(svc, tmp_path)
    assert result.ok is False
    assert result.code == "file_read_failed"
    assert result.category == "invalid_data"
    fact = (result.facts or {}).get("file_read")
    _assert_user_safe(fact)
    assert fact["attachment_id"] == att.id
    assert "Word 文档" in fact["reason"]
    assert "损坏" in fact["reason"] or "改名" in fact["reason"]


async def test_oserror_failure_has_stable_code_and_redacted_diagnostic(svc, tmp_path, monkeypatch):
    att, result = await _oserror_case(svc, tmp_path, monkeypatch)
    assert result.ok is False
    assert result.code == "file_read_failed"
    assert result.category == "io_error"
    fact = (result.facts or {}).get("file_read")
    _assert_user_safe(fact)
    assert fact["attachment_id"] == att.id
    diagnostic = fact.get("diagnostic")
    assert isinstance(diagnostic, str) and diagnostic
    assert "sk-abcdef123456" not in diagnostic, "diagnostic 必须脱敏"
    assert "redacted" in diagnostic or "***" in diagnostic
    assert "OSError" in diagnostic, "诊断保留技术细节供展开查看"
    # 工具给模型的 error 仍按原有口径脱敏（正文语义不变）
    assert "sk-abcdef123456" not in (result.error or "")
    assert "读取附件" in (result.error or "")


# ── 2. 模型可见正文与改动前一致 ─────────────────────────────────────────────


async def test_model_visible_message_is_identical_to_legacy_shape(svc, tmp_path, monkeypatch):
    """/#12 只增加事实字段：tool_feedback.message 的字面输出必须逐字不变。"""
    cases = [
        await _path_missing_case(svc, tmp_path),
        await _bad_zip_case(svc, tmp_path),
        await _oserror_case(svc, tmp_path, monkeypatch),
    ]
    for att, result in cases:
        # 改动前的等价结果：只有 ok/error/category/recoverable，没有 code/facts。
        legacy = ToolResult(
            ok=False,
            error=result.error,
            category=result.category,
            recoverable=result.recoverable,
        )
        assert tool_feedback.message(result, tool_name="read_attachment") == (
            tool_feedback.message(legacy, tool_name="read_attachment")
        ), f"{att.original_name}：增量事实改变了模型可见正文"
        assert result.content == ""


# ── 3. 其它结果不得归成文件读取失败 ─────────────────────────────────────────


async def test_permission_denied_is_not_a_file_read_failure(svc, tmp_path, monkeypatch):
    source = _write(tmp_path / "授权.txt", "内容".encode("utf-8"))
    att = _prepare(svc, source)
    monkeypatch.setattr(attachment_access, "read_allowed", lambda _id: False)

    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is False
    assert result.code == "read_not_authorized"
    assert result.category == "permission"
    assert (result.facts or {}).get("file_read") is None


class _ApproveGateway:
    """最小网关桩：读取需要用户审批（不是读取失败）。"""

    def authorize(self, effect):  # noqa: ANN001 - 与真实网关同形状
        return {"verdict": "approve", "reason": "需要用户确认"}


async def test_pending_approval_is_not_a_file_read_failure(svc, tmp_path):
    source = _write(tmp_path / "待审批.txt", "内容".encode("utf-8"))
    att = _prepare(svc, source)

    result = await _tool(svc, security=_ApproveGateway()).run(attachment_id=att.id)
    assert result.ok is True
    assert result.status == "awaiting_approval"
    assert result.code == "pending_approval"
    assert (result.facts or {}).get("file_read") is None


async def test_limit_exceeded_is_not_a_file_read_failure(svc, tmp_path, monkeypatch):
    path = _write(tmp_path / "大文档.docx", b"placeholder")
    att = _prepare(svc, path)

    def _limit(*args, **kwargs):  # noqa: ANN002, ANN003 - 注入资源边界分支
        raise AttachmentLimitExceeded("当前不可读取：累计解压超过上限", kind="total_bytes")

    monkeypatch.setattr(tool_mod, "_extract_docx", _limit)
    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is True, "限额超限按既有口径是「不可读取」而不是读取失败"
    assert result.code is None
    assert (result.facts or {}).get("file_read") is None


async def test_unsupported_format_is_not_a_file_read_failure(svc, tmp_path):
    path = _write(tmp_path / "程序.exe", b"MZ")
    att = _prepare(svc, path)

    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is True
    payload = json.loads(result.content)
    assert payload["readable"] is False
    assert (result.facts or {}).get("file_read") is None


async def test_preparing_attachment_is_not_a_file_read_failure(svc, tmp_path):
    source = _write(tmp_path / "准备中.txt", b"x")
    att = svc.prepare(str(source))  # 只登记，不准备

    result = await _tool(svc).run(attachment_id=att.id)
    assert result.ok is False
    assert "还在准备中" in (result.error or "")
    assert result.code is None
    assert (result.facts or {}).get("file_read") is None


# ── 4. TOOL_END 携带 file_read（轮内唯一一次就地提示的数据来源）─────────────


class _Adapter:
    mode = AdapterMode.NATIVE
    model = "fake"


def _http_events(chunks: list[str]) -> list[dict]:
    events: list[dict] = []
    for chunk in chunks:
        for part in chunk.splitlines():
            if part.startswith("data: "):
                events.append(json.loads(part[6:]))
    return events


async def _dispatch(tool: Tool, arguments: dict, *, call_id: str = "call_fr_1") -> list[dict]:
    registry = ToolRegistry()
    registry.register(tool)
    bus = EventBus()
    # tool_trace 非 None 才会注册 tool/result 监听：TOOL_END 的结构化事实（code /
    # file_read）由那一步暂存的完整 ToolResult 计算，与生产 wiring 一致。
    loop = AgentLoop(
        _Adapter(), registry, bus, turn_id="turn_file_read_end",
        tool_trace=lambda record: None,
    )
    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in bus.stream():
            chunks.append(chunk)
            if any(event["type"] == "TOOL_END" for event in _http_events(chunks)):
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    await loop._dispatch_tool_calls(
        [ToolCall(id=call_id, name=tool.name, arguments=arguments)]
    )
    await asyncio.wait_for(task, timeout=5)
    return _http_events(chunks)


async def test_tool_end_event_carries_file_read_fact(svc, tmp_path):
    path = _write(tmp_path / "轮内损坏.docx", b"not a zip")
    att = _prepare(svc, path)

    events = await _dispatch(_tool(svc), {"attachment_id": att.id})
    end = next(event for event in events if event["type"] == "TOOL_END")["data"]
    assert end["status"] == "failed"
    assert end["code"] == "file_read_failed"
    # TOOL_END 的 category 是跨工具归一后的类别（invalid_data 不在公共枚举里，
    # 既有口径会归一成 code_error）；界面识别「文件读取失败」只认稳定 code。
    assert end["category"]
    fact = end["file_read"]
    _assert_user_safe(fact)
    assert fact["attachment_id"] == att.id
    assert fact["name"] == att.original_name


class _PermissionStub(Tool):
    name = "read_attachment"
    description = "演示：权限拒绝（没有文件读取事实）"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):  # noqa: ANN003
        return ToolResult(
            ok=False,
            error="这条附件没有通过本轮读取授权，本次没有读取任何内容。",
            category="permission",
            recoverable=False,
            code="read_not_authorized",
        )


async def test_tool_end_file_read_is_none_for_other_failures():
    events = await _dispatch(_PermissionStub(), {}, call_id="call_perm_1")
    end = next(event for event in events if event["type"] == "TOOL_END")["data"]
    assert end["code"] == "read_not_authorized"
    assert end["file_read"] is None, "缺少结构化事实时如实为 None，不把其它错误归成文件读取"


# ── 5. turn_facts：每个受影响文件 1 条可读事实，答案注记不重复 ──────────────

_READ_FACT = {
    "attachment_id": "att_1",
    "name": "季度报告.docx",
    "reason": "文件已经不在原来保存的位置（可能被移动或删除），这次没有读到内容。",
    "next_step": "重新指定文件位置后才能继续读取。",
}


def _record_read_failure(facts: TurnFacts, error: str | None = None) -> None:
    facts.record_tool(
        call_id="c1",
        tool_name="read_attachment",
        ok=False,
        status="failed",
        category="unavailable",
        error=error
        or "读不到附件「季度报告.docx」：文件不在原来登记的位置（状态 missing）。"
        "可以让用户重新指定位置后再读。",
    )
    facts.record_facts({"file_read": dict(_READ_FACT)})


def test_one_file_read_failure_yields_one_readable_fact():
    facts = TurnFacts()
    _record_read_failure(facts)

    items = facts.unresolved()
    assert len(items) == 1, f"同一失败不得在台账里重复：{items}"
    line = items[0]
    assert "季度报告.docx" in line
    assert "这次没有读到内容" in line
    assert "重新指定文件位置" in line
    for phrase in _MODEL_ONLY_INSTRUCTIONS:
        assert phrase not in line, f"台账事实泄露了模型指令：{line}"
    assert facts.file_read_failures == [_READ_FACT]


def test_annotation_does_not_repeat_the_in_round_file_read_notice():
    """轮内工具卡已就地显示一次（TOOL_END.file_read），答案注记不再重复。"""
    facts = TurnFacts()
    _record_read_failure(facts)

    assert facts.annotation() is None, "只有文件读取失败时，不应再补一条答案注记"


def test_other_failures_are_still_annotated_but_not_the_file_read_one():
    facts = TurnFacts()
    _record_read_failure(facts)
    facts.record_tool(
        call_id="c2", tool_name="dev_run_tests", ok=False, status="failed",
        category="assertion", error="断言不匹配",
    )

    note = facts.annotation()
    assert note is not None
    assert "dev_run_tests" in note
    assert "季度报告.docx" not in note
    assert "没读到" not in note
    assert len(facts.unresolved()) == 2


def test_read_attachment_permission_failure_is_still_annotated():
    """没有 file_read 事实的 read_attachment 失败（权限等）不能被顺手藏掉。"""
    facts = TurnFacts()
    facts.record_tool(
        call_id="c1", tool_name="read_attachment", ok=False, status="failed",
        category="permission", error="这条附件没有通过本轮读取授权，本次没有读取任何内容。",
    )
    note = facts.annotation() or ""
    assert "read_attachment 执行失败" in note
    assert facts.file_read_failures == []


def test_later_fact_for_the_same_attachment_overrides_the_earlier_one():
    facts = TurnFacts()
    facts.record_facts({"file_read": dict(_READ_FACT)})
    updated = dict(_READ_FACT, reason="换了个位置，仍然读不到内容。")
    facts.record_facts({"file_read": updated})

    assert len(facts.file_read_failures) == 1, "同一附件后续事实覆盖前一条"
    assert facts.file_read_failures[0]["reason"] == "换了个位置，仍然读不到内容。"
    assert len(facts.unresolved()) == 1


def test_two_affected_files_yield_two_facts():
    facts = TurnFacts()
    facts.record_facts({"file_read": dict(_READ_FACT)})
    facts.record_facts(
        {
            "file_read": {
                "attachment_id": "att_2",
                "name": "明细.xlsx",
                "reason": "文件看起来不是有效的 Excel 表格，这次没有读到内容。",
                "next_step": "确认文件没有损坏后再试。",
            }
        }
    )
    assert len(facts.file_read_failures) == 2
    items = facts.unresolved()
    assert len(items) == 2
    assert any("季度报告.docx" in line for line in items)
    assert any("明细.xlsx" in line for line in items)
    assert facts.annotation() is None


def test_file_read_facts_ignore_garbage():
    facts = TurnFacts()
    facts.record_facts({"file_read": "nope"})
    facts.record_facts({"file_read": {"attachment_id": "", "reason": "x"}})
    facts.record_facts({"file_read": {"attachment_id": "att_x", "reason": ""}})
    assert facts.file_read_failures == []
    assert facts.annotation() is None


# ── 6. task_feed：单次文件读取失败只留在执行记录 ────────────────────────────


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _all_items(client) -> list[dict]:
    items: list[dict] = []
    for view in ("current", "history"):
        response = client.get(f"/api/tasks?view={view}&limit=100")
        assert response.status_code == 200, response.text
        items.extend(response.json()["items"])
    return items


def test_single_file_read_failure_is_not_listed_but_execution_record_is_kept(client):
    ctx = client.app.state.ctx
    failed = tool_records.record_tool_call(
        ctx.conn,
        turn_id="turn_file_read_feed",
        tool_name="read_attachment",
        arguments={"attachment_id": "att_1"},
        output="",
        status="failed",
        error="读不到附件「季度报告.docx」：文件不在原来登记的位置。可以让用户重新指定位置后再读。",
        duration_ms=1200,
        code="file_read_failed",
        category="unavailable",
        recoverable=True,
        result_status="failed",
    )
    control = tool_records.record_tool_call(
        ctx.conn,
        turn_id="turn_file_read_feed",
        tool_name="read_attachment",
        arguments={"attachment_id": "att_2"},
        output="",
        status="failed",
        error="这条附件没有通过本轮读取授权，本次没有读取任何内容。",
        duration_ms=1200,
        code="read_not_authorized",
        category="permission",
        recoverable=False,
        result_status="failed",
    )
    assert failed and control

    ids = {item["id"] for item in _all_items(client)}
    assert f"tool:{failed}" not in ids, "单次文件读取失败不得在任务栏另列（轮内已就地显示一次）"
    assert f"tool:{control}" in ids, "结构化字段判定：权限失败不能被归成文件读取失败而隐藏"
    # 执行记录原样保留：状态、稳定 code 与失败原因都还能读到。
    record = client.get(f"/api/tool-records/{failed}")
    assert record.status_code == 200
    body = record.json()
    assert body["status"] == "failed"
    assert body["code"] == "file_read_failed"
    assert "读不到附件" in body["error"]
