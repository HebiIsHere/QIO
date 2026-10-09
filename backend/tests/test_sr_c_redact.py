"""契约 3 验收（Lead 代 C 完成）：报错出口先打码再截断，实时事件与重放缓冲都不泄漏。"""
from __future__ import annotations

import logging

import pytest

from agent.api.bus import EventBus
from agent.api.events import EventType, make_event, sse_format
from agent.trace import redact as redact_mod
from agent.trace.redact import redact_any, sanitize_error_text

SECRET = "sk-srtest-9f3a1b2c-plumbus-no-prefix-class"


def _register():
    redact_mod.clear_registered_secrets()
    assert redact_mod.register_secret(SECRET, source="sr-test")


async def test_sanitize_error_text_redacts_then_truncates():
    _register()
    # 截断点切过密钥本体（密钥在前、长文本在后）：截断后的文本仍不得留原值前缀
    long = SECRET + "x" * 300
    out = sanitize_error_text(long)
    assert SECRET not in out
    assert redact_mod.REDACTED in out
    # 截断上限生效
    assert len(out) <= 201
    _cleanup()


def _cleanup():
    redact_mod.clear_registered_secrets()


async def test_redact_any_structured_secret_field_and_identity_kept():
    _register()
    data = {
        "api_key": "raw-value-should-vanish",
        "turn_id": "turn_abc",
        "total_tokens": 42,
        "message": "boom " + SECRET,
    }
    out = redact_any(data)
    assert "raw-value-should-vanish" not in str(out)
    assert out["turn_id"] == "turn_abc"
    assert out["total_tokens"] == 42
    assert SECRET not in out["message"]
    _cleanup()


async def test_bus_publish_gate_hits_realtime_and_replay():
    _register()
    bus = EventBus(replay_limit=10)
    import asyncio

    collected: list[str] = []

    async def collector():
        it = aiter(bus.stream())
        for _ in range(2):
            chunk = await it.__anext__()
            collected.append(chunk)

    task = asyncio.get_running_loop().create_task(collector())
    await asyncio.sleep(0)
    err = make_event(EventType.ERROR, {"code": "m", "message": "模型 balabala " + SECRET + " tail"})
    await bus.publish(err)
    await asyncio.sleep(0.05)
    blob = "".join(collected)
    assert SECRET not in blob, blob
    assert redact_mod.REDACTED in blob
    # 重放缓冲里存的也是打码后的事件
    replay_blob = "".join(sse_format(e) for e in list(bus._history))
    assert SECRET not in replay_blob
    task.cancel()
    _cleanup()


async def test_orchestrator_style_error_text_via_sanitize():
    # 等价于 execute_loop 失败出口的构造（orchestrator 已改用 sanitize_error_text）
    _register()
    exc = RuntimeError("provider said: " + SECRET)
    text = sanitize_error_text(str(exc))
    assert SECRET not in text
    _cleanup()
