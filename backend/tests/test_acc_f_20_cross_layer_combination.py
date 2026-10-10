"""F 独立验证（阶段二 · 跨层组合）：正常路径与异常路径分开跑（plan §六.2）。

正常路径（test_normal_*）：
  A 流式执行（假 provider，工具调用处用 hold 钉死「正在跑」）→ B 排队（不得改变 A 的归属）
  → 取消 B（cancelled / user_stopped / retry，落 turn_journal）→ A 工具失败产生系统核对注记
  （独立 annotation 字段；正文保持纯正文）→ A 正常完成 → C 正常完成。
  工具失败原因里放一个**随机合成敏感值**，验证注记与所有可观测出口都先脱敏再发布。

异常路径（test_abnormal_*）：
  A 走**真实 HTTP + 真 SSE** 的假厂商端点（NativeAdapter），分片流到一半干净收束但缺
  finish_reason（abort_after=2），期间 B 排队并被取消。A 必须以 incomplete /
  incomplete_stream / retry 收口；敏感值跨分块切开时不得出现「半个」或完整原文。

两类断言：turn 归属（A/C 的 ASSISTANT 只归自己、B 从未 TURN_START）、正文唯一（BODY 恰好一次且
不含注记表头）、注记完整（表头 + 结论句）、结束事实（status / reason_code / actions / 耗时）与
历史恢复（turn_journal 落库）。

运行：cd backend; uv run --frozen pytest -q tests/test_acc_f_20_cross_layer_combination.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.core.turn_facts import ANNOTATION_HEADER
from agent.tools.base import Tool, ToolResult
from agent.trace.redact import REDACTED, clear_registered_secrets, register_secret

REPO_ROOT = Path(__file__).resolve().parents[2]
BODY = "====正文====\n这是这一轮唯一的正式回答。"
TOOL_NAME = "acc_f_combination_tool"
_SECRET: dict[str, str] = {}

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(autouse=True)
def _register_synthetic_secret():
    """随机合成敏感值：正则认不出，只能靠登记表；断言失败也不打印原值。"""
    value = "accf20-" + uuid.uuid4().hex
    _SECRET["value"] = value
    assert register_secret(value) is True
    try:
        yield
    finally:
        clear_registered_secrets()
        _SECRET.clear()


def _secret() -> str:
    return _SECRET["value"]


class _FailingTool(Tool):
    name = TOOL_NAME
    description = "永远失败的工具（跨层组合验证用），失败原因里带合成敏感值"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    is_concurrency_safe = False

    async def run(self, **kwargs):
        return ToolResult(ok=False, error="工具失败，诊断值：" + _secret(), category="verify")


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _history(app) -> list:
    return list(app.state.ctx.bus._history)


def _of(app, kind: str, turn_id: str | None = None) -> list:
    out = []
    for event in _history(app):
        if event.type.value != kind:
            continue
        if turn_id is not None and str(event.data.get("turn_id")) != turn_id:
            continue
        out.append(event)
    return out


async def _wait(predicate, timeout: float = 60.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def _turn_end(app, turn_id: str) -> dict | None:
    for event in _of(app, "TURN_END", turn_id):
        return event.data
    return None


async def _wait_turn_end(app, turn_id: str, timeout: float = 60.0) -> dict | None:
    await _wait(lambda: _turn_end(app, turn_id) is not None, timeout)
    return _turn_end(app, turn_id)


def _wire_dump(app) -> str:
    return "\n".join(e.model_dump_json() for e in _history(app))


def _load_provider_module():
    path = REPO_ROOT / "scripts" / "verify_stream_provider.py"
    assert path.exists(), "验证资产缺失：%s" % path
    spec = importlib.util.spec_from_file_location("verify_stream_provider_accf20", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def provider():
    module = _load_provider_module()
    server = module.StreamingProvider(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _native_adapter(server):
    from openai import AsyncOpenAI

    from agent.adapters.native import NativeAdapter

    client = AsyncOpenAI(
        base_url="http://127.0.0.1:%d/v1" % server.server_port,
        api_key="sk-accf20-fake-0001",
    )
    return NativeAdapter(client=client, model="verify-model")


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    application.state.ctx.registry.register(_FailingTool())
    return application


# ---------------------------------------------------------------- 正常路径


async def test_normal_stream_queue_cancel_annotation_and_normal_end(app):
    """A 流式 + B 排队取消 + A 工具失败注记 + A/C 正常结束（正常路径）。"""
    hold = asyncio.Event()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[ScriptedToolCall(id="c_accf20", name=TOOL_NAME, arguments={})],
                hold_after=1,
                hold=hold,
                gap_ms=30,
            ),
            StreamScript(),
            StreamScript(text=BODY),
            StreamScript(text="C 的回答。"),
        ]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        a = await client.post("/api/turns", json={"message": "A 请回答"})
        assert a.status_code == 200, a.text[:200]
        turn_a = str(a.json()["turn_id"])
        assert await _wait(lambda: _of(app, "TURN_START", turn_a), timeout=20), "A 没有启动"

        b = await client.post("/api/turns", json={"message": "B 请回答"})
        turn_b = str(b.json()["turn_id"])
        await asyncio.sleep(0.15)
        snapshot = (await client.get("/api/turns/queue")).json()
        assert snapshot["running"]["turn_id"] == turn_a, snapshot
        assert [q["turn_id"] for q in snapshot["queued"]] == [turn_b], snapshot

        # 排队期间归属仍是 A：B 不得有 TURN_START
        assert [str(e.data.get("turn_id")) for e in _of(app, "TURN_START")] == [turn_a], (
            "B 排队期间不得出现 TURN_START",
            [str(e.data.get("turn_id")) for e in _of(app, "TURN_START")],
        )
        assert _turn_end(app, turn_a) is None, "A 还没结束"

        cancel = await client.post("/api/turns/%s/cancel" % turn_b)
        assert cancel.status_code == 200, cancel.text[:200]
        assert cancel.json().get("cancelled") is True, cancel.json()
        end_b = await _wait_turn_end(app, turn_b, timeout=20)

        hold.set()
        end_a = await _wait_turn_end(app, turn_a, timeout=60)
        c = await client.post("/api/turns", json={"message": "C 请回答"})
        assert c.status_code == 200, c.text[:200]
        turn_c = str(c.json()["turn_id"])
        end_c = await _wait_turn_end(app, turn_c, timeout=60)
        queue_after = (await client.get("/api/turns/queue")).json()

    assert end_a is not None and end_b is not None and end_c is not None, "有 TURN_END 没到"
    secret = _secret()

    # -- A：正常完成、正文唯一、注记完整且独立 ---
    assert end_a["status"] == "completed", ("A 必须正常完成", end_a.get("status"))
    final_a = str(end_a.get("final_content") or "")
    assert final_a == BODY, ("final_content 必须是纯正文", final_a)
    assert ANNOTATION_HEADER not in final_a, "注记不得混进 final_content"
    note_a = str(end_a.get("annotation") or end_a.get("final_annotation") or "")
    assert ANNOTATION_HEADER in note_a, ("注记必须独立交付且含表头", sorted(end_a.keys()))
    assert "不能当作「已完成 / 可使用」" in note_a, note_a
    assert secret not in note_a and secret not in final_a, "注记/正文泄漏了登记敏感值"

    # -- B：排队取消的结束事实 ---
    assert end_b["status"] == "cancelled", end_b
    assert end_b["reason_code"] == "user_stopped", end_b.get("reason_code")
    assert end_b["stopped_by"] == "user", end_b.get("stopped_by")
    assert "retry" in [str(x) for x in end_b.get("actions") or []], end_b.get("actions")
    assert not str(end_b.get("final_content") or "").strip(), "取消的排队轮不得伪造正文"

    # -- C：正常完成，回答归 C ---
    assert end_c["status"] == "completed", end_c.get("status")
    assert str(end_c.get("final_content") or "") == "C 的回答。", end_c.get("final_content")

    # -- 归属：B 没有任何 ASSISTANT，A/C 各归自己；每个 turn 恰好一次 START/END ---
    assistant_turns = [str(e.data.get("turn_id")) for e in _of(app, "ASSISTANT")]
    assert turn_b not in assistant_turns, ("取消的排队轮不得有 ASSISTANT", assistant_turns)
    assert set(assistant_turns) <= {turn_a, turn_c}, assistant_turns
    assert _of(app, "TURN_START", turn_b) == [], "B 从未启动，不该有 TURN_START"
    # 只有真正开始执行的 turn 才有 TURN_START（B 排队后被取消 → 从未启动）。
    for turn_id in (turn_a, turn_c):
        assert len(_of(app, "TURN_START", turn_id)) == 1, turn_id
    for turn_id in (turn_a, turn_b, turn_c):
        assert len(_of(app, "TURN_END", turn_id)) == 1, turn_id

    # -- 结束事实落库（历史恢复） ---
    facts = app.state.ctx.turn_journal.facts([turn_b]).get(turn_b)
    assert facts is not None, "B 的结束事实没有落进 turn_journal"
    assert facts["status"] == "cancelled", facts
    assert facts["reason_code"] == "user_stopped", facts
    assert "retry" in facts["actions"], facts

    # -- 脱敏（本组合的出口范围）：正文 / 注记 / 最终校准都不得带登记敏感值。
    #    注意：**TOOL_END 事件载荷的 error 目前仍是原文**（契约 §C3 覆盖范围内的缺口）——
    #    已单独登记为最小反例 tests/test_acc_f_21_tool_end_redaction.py 并上报 Lead；
    #    这里不把「整条线上的序列化」混进来，避免两个问题互相掩盖。
    assert secret not in final_a and secret not in note_a, "正文/注记泄漏了登记敏感值"
    assert all(
        secret not in str(e.data.get("content") or "") for e in _of(app, "ASSISTANT")
    ), "ASSISTANT 泄漏了登记敏感值"

    assert not queue_after.get("running") and not queue_after.get("queued"), queue_after
    print(
        "[诊断] F20 正常组合：A=%s 正文纯净=%s 注记=%d B=%s/%s/%s C=%s 归属=%s 无泄漏=是"
        % (
            end_a["status"],
            final_a == BODY,
            len(note_a),
            end_b["status"],
            end_b["reason_code"],
            end_b.get("actions"),
            end_c["status"],
            sorted(set(assistant_turns)),
        )
    )


# ---------------------------------------------------------------- 异常路径


async def test_abnormal_incomplete_stream_with_queued_cancel_and_split_redaction(app, provider):
    """A 不完整结束（真 SSE 缺 finish_reason）+ B 排队取消 + 跨分块脱敏（异常路径）。"""
    secret = _secret()
    head = secret[:12]  # 第一片末尾的「半个敏感值」：尾部缓冲必须拦住
    app.state.ctx.build_adapter = AsyncMock(return_value=_native_adapter(provider))
    provider.script.set(
        [
            {
                "abort_after": 2,
                "chunk_delay_ms": 600,
                "chunks": ["前缀 " + head, secret[12:] + " 后缀", "永远不会发出的后缀"],
            }
        ]
    )

    async with _client(app) as client:
        a = await client.post("/api/turns", json={"message": "A 请回答"})
        assert a.status_code == 200, a.text[:200]
        turn_a = str(a.json()["turn_id"])
        # A 的 TURN_START 一发出就提交 B：不依赖 ASSISTANT 增量时机（未声明前缀会被有界
        # 缓冲到流结束才发布，等增量会把「排队窗口」等到流结束之后）。
        assert await _wait(lambda: _of(app, "TURN_START", turn_a), timeout=20), "A 没有启动"

        b = await client.post("/api/turns", json={"message": "B 请回答"})
        turn_b = str(b.json()["turn_id"])
        await asyncio.sleep(0.1)
        snapshot = (await client.get("/api/turns/queue")).json()
        assert (snapshot.get("running") or {}).get("turn_id") == turn_a, {
            "snapshot": snapshot,
            "a_end": _turn_end(app, turn_a) is not None,
            "events": [e.type.value for e in _history(app)],
        }
        assert [q["turn_id"] for q in snapshot["queued"]] == [turn_b], snapshot
        assert _turn_end(app, turn_a) is None, "B 排队时 A 不应已经结束"

        cancel = await client.post("/api/turns/%s/cancel" % turn_b)
        assert cancel.status_code == 200 and cancel.json().get("cancelled") is True, cancel.text[:200]
        end_b = await _wait_turn_end(app, turn_b, timeout=20)
        end_a = await _wait_turn_end(app, turn_a, timeout=60)
        streamed = await _wait(
            lambda: any(
                str(e.data.get("turn_id")) == turn_a and str(e.data.get("content") or "")
                for e in _of(app, "ASSISTANT")
            ),
            timeout=20,
        )
        assert streamed, "装置失效：A 没有产出流式增量"

    assert end_a is not None and end_b is not None, "有 TURN_END 没到"

    # -- A：不完整终态 + 已确认正文保留 + 可重试 ---
    assert end_a["status"] == "incomplete", (
        "缺 finish_reason 的 EOF 必须落 incomplete",
        {"status": end_a.get("status"), "reason_code": end_a.get("reason_code")},
    )
    assert end_a["reason_code"] == "incomplete_stream", end_a.get("reason_code")
    assert "retry" in [str(x) for x in end_a.get("actions") or []], end_a.get("actions")
    final_a = str(end_a.get("final_content") or "")
    assert "前缀" in final_a and "后缀" in final_a, ("已确认正文必须保留", final_a)
    assert "永远不会发出的后缀" not in final_a, "未确认的后缀不得凭空出现"
    assert REDACTED in final_a, ("正文里的敏感值必须替换成打码标记", final_a)

    # -- B：排队取消事实 ---
    assert end_b["status"] == "cancelled", end_b
    assert end_b["reason_code"] == "user_stopped", end_b.get("reason_code")
    assert "retry" in [str(x) for x in end_b.get("actions") or []], end_b.get("actions")

    # -- 归属 ---
    assistant_turns = {str(e.data.get("turn_id")) for e in _of(app, "ASSISTANT")}
    assert assistant_turns == {turn_a}, ("取消 B 影响了 A 的归属", assistant_turns)
    assert _of(app, "TURN_START", turn_b) == [], "B 不该有 TURN_START"

    # -- 跨分块脱敏：半个 / 完整原文都不得出现在任何可观测出口 ---
    contents = [str(e.data.get("content") or "") for e in _of(app, "ASSISTANT")]
    assert all(secret not in text for text in contents), "ASSISTANT 泄漏了完整敏感值"
    assert all(head not in text for text in contents), "ASSISTANT 泄漏了『半个敏感值』（尾部缓冲失效）"
    assert secret not in _wire_dump(app), "事件出口泄漏了完整敏感值"
    assert head not in _wire_dump(app), "事件出口泄漏了半个敏感值"
    assert secret not in str(end_a.get("final_content") or ""), "最终正文泄漏了敏感值"

    facts_b = app.state.ctx.turn_journal.facts([turn_b]).get(turn_b)
    assert facts_b is not None and facts_b["reason_code"] == "user_stopped", facts_b

    print(
        "[诊断] F20 异常组合：A=%s/%s actions=%s B=%s/%s 半个泄漏=否 完整泄漏=否 归属=%s"
        % (
            end_a["status"],
            end_a["reason_code"],
            end_a.get("actions"),
            end_b["status"],
            end_b["reason_code"],
            sorted(assistant_turns),
        )
    )
