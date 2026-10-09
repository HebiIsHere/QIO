"""F 独立验证（阶段一 · F07）：流式事件绕过脱敏（含跨分块切开）。

契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C3 ——
「一切可观测输出（增量、累计快照、一次性正文、最终校准、事件、Trace、历史、错误）统一走
agent/trace/redact.py::redact_text」「先脱敏再发布」「已登记敏感值跨分块切开的部分必须可识别
—— 采用有界未定稿尾部缓冲」。

做法：测试内生成一个**随机合成敏感值**（无密钥形状，只能靠精确登记表识别），用
agent.trace.redact.register_secret 登记，随后即清理；断言只输出布尔与通过状态，
**绝不打印原值**（断言用 _contains_secret 助手，失败信息里也不出现原值；测试函数
不带敏感值参数，避免 pytest 在失败头部打印 fixture 取值）。链路：假 provider
（FakeStreamAdapter）→ adapter → AgentLoop → 事件总线。

两种位置：
* 完整出现：整段正文里包含完整敏感值；
* 跨分块切开：敏感值被 SSE 分片从中间切开，前一片已能触发一次累计快照发布。

基线：ASSISTANT 载荷直接发布 _confirmed 原文，登记表未被消费 → 原值泄漏。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_f_07_stream_redaction.py -q
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.registry import ToolRegistry
from agent.trace.redact import REDACTED, clear_registered_secrets, register_secret

ANSWER_MARKER = "[[QIO:ANSWER]]"
_SECRET: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _register_synthetic_secret():
    value = "accf07-" + uuid.uuid4().hex  # 随机串：正则认不出，只能靠登记表
    _SECRET["value"] = value
    assert register_secret(value) is True
    try:
        yield
    finally:
        clear_registered_secrets()
        _SECRET.clear()


def _secret() -> str:
    return _SECRET["value"]


def _contains_secret(text: str) -> bool:
    """只返回布尔：即使断言失败，信息里也不会出现原值。"""
    return _secret() in text


def _assistant_payloads(loop) -> list[dict]:
    return [e.data for e in loop.bus._history if e.type.value == "ASSISTANT"]


def _wire_dump(loop) -> str:
    return "\n".join(e.model_dump_json() for e in loop.bus._history)


def _run(chunks):
    async def _go():
        adapter = FakeStreamAdapter([StreamScript(text_chunks=chunks, finish_reason="stop")])
        loop = AgentLoop(adapter, ToolRegistry(), EventBus(), turn_id="turn_acc_f_07")
        result = await asyncio.wait_for(loop.run("请回答"), timeout=20)
        return loop, result

    return asyncio.run(_go())


def test_whole_secret_never_appears_in_stream_or_wire():
    secret = _secret()
    chunks = [ANSWER_MARKER + "\n前缀 " + secret + " 后缀"]
    loop, result = _run(chunks)

    contents = [str(p.get("content") or "") for p in _assistant_payloads(loop)]
    leaked = [i for i, text in enumerate(contents) if _contains_secret(text)]
    assert contents, "必须有 ASSISTANT 事件（装置失效）"
    assert not leaked, (
        "完整出现：ASSISTANT 累计快照泄漏了已登记敏感值",
        {"events": len(contents), "leaked_indices": leaked},
    )
    assert not _contains_secret(_wire_dump(loop)), "SSE 线上（事件序列化）泄漏了敏感值"
    assert not _contains_secret(str(result.final_content or "")), (
        "最终校准正文泄漏了敏感值（契约 §C3：一次性正文 / 最终校准也要脱敏）"
    )
    assert REDACTED in contents[-1], (
        "必须真的做了替换（出现打码标记），而不是把内容整段丢掉"
    )
    print(
        "[诊断] F07 完整出现：ASSISTANT=%d 泄漏=否 打码标记=%s"
        % (len(contents), REDACTED in contents[-1])
    )


def test_secret_split_across_chunks_is_buffered_and_redacted():
    secret = _secret()
    head = secret[:12]  # 第一片末尾的「半个敏感值」：契约要求尾部不发布
    chunks = [ANSWER_MARKER + "\n" + "x" * 20 + head, secret[12:] + "y" * 20]
    loop, result = _run(chunks)

    contents = [str(p.get("content") or "") for p in _assistant_payloads(loop)]
    leaked = [i for i, text in enumerate(contents) if _contains_secret(text)]
    partial = [i for i, text in enumerate(contents) if head in text]
    assert contents, "必须有 ASSISTANT 事件（装置失效）"
    assert len(contents) >= 2, (
        "跨分块场景需要至少两次累计快照才能验证尾部缓冲",
        {"events": len(contents)},
    )
    assert not leaked, (
        "跨分块切开：累计快照泄漏了完整敏感值",
        {"events": len(contents), "leaked_indices": leaked},
    )
    assert not partial, (
        "跨分块切开：发布了「半个敏感值」的尾部（契约要求有界未定稿尾部缓冲）",
        {"events": len(contents), "partial_indices": partial},
    )
    assert not _contains_secret(_wire_dump(loop)), "SSE 线上（事件序列化）泄漏了敏感值"
    assert not _contains_secret(str(result.final_content or "")), "最终正文泄漏了敏感值"
    assert REDACTED in contents[-1], "跨分块场景也必须真的替换成打码标记"
    print(
        "[诊断] F07 跨分块：ASSISTANT=%d 完整泄漏=否 半个泄漏=否 打码标记=%s"
        % (len(contents), REDACTED in contents[-1])
    )
