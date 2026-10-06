"""D 独立验证：轮次结束事实（审计问题 7 / plan §1.2）。

契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 7 条 + §1.2。
只依据产品规则：

* TURN_END.data 必须带上：reason_code / reason / stopped_by / actions（只列确实可用的操作）；
* 原因属于**正确的轮次**，且来自系统事实（不是编出来的一句话）；
* 可恢复的单次工具错误**不等于**整轮失败；
* 用户停止要说「用户停止」，程序中断要说「程序中断」，不能都说成厂商故障；
* 已知耗时（duration_ms / queue_ms）不受失败明细影响。

基线（e428bb9）现状：TURN_END 只有 status / final_content / error / 耗时，
失败与停止在界面上只有状态词与工具计数 —— 因此本文件在修复前应当是**红的**。

模型调用一律用假 adapter（不联网、不读任何密钥）。
运行：cd backend; uv run --frozen --extra dev pytest tests/test_audit_turn_end_facts_verify.py -q
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.adapters.errors import ProviderInternalError
from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.credentials.store import MemoryKeyring
from agent.tools.base import Tool, ToolResult

REASON_CODES = {
    "provider_error",
    "tool_failed",
    "budget",
    "no_progress",
    "guard_halt",
    "user_stopped",
    "interrupted",
    # 运行时/配置类失败（Lead 2026-10-06 批准加入枚举，A 已实现）：
    "internal_error",
    "credential_unavailable",
    "none",
}
ACTIONS = {"retry", "resend", "continue"}


class _FailingTool(Tool):
    name = "audit_failing_tool"
    description = "永远失败的工具（验证用）"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    is_concurrency_safe = False

    async def run(self, **kwargs):
        return ToolResult(ok=False, error="这一步失败了，但可以重试", category="verify")


class _BoomAdapter:
    """厂商故障：模型调用抛出**归一化后的**供应商错误（adapters/errors.py 的家族）。

    分类口径（core/turn.py::_failure_facts）：按**异常类名**判定 ——
    ProviderError 家族 → provider_error；其它未知异常 → internal_error（那是内部故障，
    不是厂商故障，两者不能混为一谈）。
    """

    mode = "text"
    model = "fake-boom"
    supports_stream = False

    async def complete(self, messages, tools, **kwargs):
        raise ProviderInternalError("厂商返回 500：上游错误（fake provider）")


class _UnexpectedBoomAdapter:
    """未归一化的意外异常：必须如实归成 internal_error，不能谎报成厂商故障。"""

    mode = "text"
    model = "fake-unexpected"
    supports_stream = False

    async def complete(self, messages, tools, **kwargs):
        raise RuntimeError("验证用的意外内部错误")


class _SlowAdapter:
    mode = "text"
    model = "fake-slow"
    supports_stream = False

    async def complete(self, messages, tools, **kwargs):
        await asyncio.sleep(10)
        return Completion(message=ChatMessage(role="assistant", content="迟到的回答"))


@pytest.fixture()
def app_client(db_conn, settings):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        yield client, app


def _turn_ends(app) -> list[dict]:
    return [
        e.data for e in app.state.ctx.bus._history if e.type.value == "TURN_END"
    ]


def _turn_end(app, turn_id: str, timeout: float = 30.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for item in _turn_ends(app):
            if str(item.get("turn_id")) == turn_id:
                return item
        time.sleep(0.02)
    return None


def _submit(client, message: str) -> str:
    resp = client.post("/api/turns", json={"message": message, "attachment_ids": []})
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    return str(resp.json()["turn_id"])


def _wait_running(client, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/api/turns/queue").json().get("running"):
            return True
        time.sleep(0.02)
    return False


def _check_common_fields(end: dict) -> None:
    assert "reason_code" in end, (
        "TURN_END 必须带 reason_code（plan §1.2）",
        {"status": end.get("status"), "keys": sorted(end)},
    )
    assert end["reason_code"] in REASON_CODES, ("reason_code 取值必须在契约枚举里", end["reason_code"])
    assert "reason" in end, ("TURN_END 必须带一句话人话原因", sorted(end))
    assert "stopped_by" in end, ("TURN_END 必须说清是谁停的", sorted(end))
    assert end["stopped_by"] in ("user", "system", None), end["stopped_by"]
    assert "actions" in end, ("TURN_END 必须列出当前确实可用的操作", sorted(end))
    actions = end["actions"]
    assert isinstance(actions, list), actions
    assert set(str(a) for a in actions) <= ACTIONS, ("只列契约里的三种操作", actions)
    if end["reason_code"] not in ("none",):
        reason = end.get("reason")
        assert isinstance(reason, str) and reason.strip(), ("失败/停止必须给出人话原因", reason)
        assert len(reason) <= 200, ("reason 有长度上限", len(reason))
        assert "sk-" not in reason and "Bearer" not in reason, ("原因不得包含密钥原文", reason)


# ---- 1. 厂商失败 -------------------------------------------------------------------


def test_provider_error_is_reported_on_the_right_turn(app_client):
    client, app = app_client
    ctx = app.state.ctx
    ctx.build_adapter = AsyncMock(return_value=_BoomAdapter())

    turn_id = _submit(client, "这一轮模型会失败")
    end = _turn_end(app, turn_id)
    assert end is not None, "TURN_END 没有到达"
    _check_common_fields(end)
    assert end["status"] == "failed", ("厂商失败 → 这一轮必须如实失败", end["status"])
    assert end["reason_code"] == "provider_error", (
        "模型调用抛错必须归类成 provider_error",
        end["reason_code"],
    )
    assert end["stopped_by"] == "system", end["stopped_by"]
    assert "retry" in [str(a) for a in end["actions"]], (
        "厂商故障是可以用「重试」恢复的，必须给出重试入口",
        end["actions"],
    )
    assert isinstance(end.get("duration_ms"), int) and end["duration_ms"] >= 0, (
        "已知耗时不受失败明细影响",
        end.get("duration_ms"),
    )


def test_unexpected_exception_is_reported_as_internal_error(app_client):
    """未归一化的意外异常 = 内部故障（internal_error），不得冒充成厂商故障。"""
    client, app = app_client
    app.state.ctx.build_adapter = AsyncMock(return_value=_UnexpectedBoomAdapter())

    turn_id = _submit(client, "这一轮会抛一个未归一化的异常")
    end = _turn_end(app, turn_id)
    assert end is not None, "TURN_END 没有到达"
    _check_common_fields(end)
    assert end["status"] == "failed", end["status"]
    assert end["reason_code"] == "internal_error", (
        "未知异常必须归成 internal_error（按异常类名判定，不猜厂商）",
        end["reason_code"],
    )
    assert "RuntimeError" in str(end.get("reason") or ""), (
        "原因必须来自系统事实（异常类名），不是编出来的"
        ,
        end.get("reason"),
    )
    assert "retry" in [str(a) for a in end["actions"]], end["actions"]


# ---- 2. 用户停止 -------------------------------------------------------------------


def test_user_stop_is_not_reported_as_provider_error(app_client):
    client, app = app_client
    app.state.ctx.build_adapter = AsyncMock(return_value=_SlowAdapter())

    turn_id = _submit(client, "这一轮会被用户停掉")
    assert _wait_running(client), "turn 没有进入运行中"
    assert client.post("/api/turns/cancel").json().get("cancelled") is True

    end = _turn_end(app, turn_id)
    assert end is not None, "TURN_END 没有到达"
    _check_common_fields(end)
    assert end["status"] == "cancelled", end["status"]
    assert end["reason_code"] == "user_stopped", (
        "用户主动停止必须说成 user_stopped，不能归成厂商故障",
        end["reason_code"],
    )
    assert end["stopped_by"] == "user", end["stopped_by"]
    assert "resend" in [str(a) for a in end["actions"]], (
        "被用户停掉的一轮应当能「重发」",
        end["actions"],
    )


# ---- 3. 可恢复的工具错误 != 整轮失败 -----------------------------------------------


def test_recoverable_tool_error_is_not_a_failed_turn(app_client):
    client, app = app_client
    ctx = app.state.ctx
    ctx.registry.register(_FailingTool())
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[ScriptedToolCall(id="c_fail", name="audit_failing_tool", arguments={})]
            ),
            StreamScript(text="工具失败了，但我换了个办法，这是正式回答。"),
        ]
    )
    ctx.build_adapter = AsyncMock(return_value=adapter)

    turn_id = _submit(client, "调用一个会失败的工具")
    end = _turn_end(app, turn_id)
    assert end is not None, "TURN_END 没有到达"
    _check_common_fields(end)
    assert end["status"] == "completed", (
        "可恢复的单次工具错误不等于整轮失败",
        end,
    )
    assert end["reason_code"] == "none", (
        "没有整轮级失败时 reason_code 必须是 none",
        end["reason_code"],
    )
    assert end["stopped_by"] is None, end["stopped_by"]
    assert end["actions"] == [], ("没有失败就不该给「重试」之类入口", end["actions"])
    assert isinstance(end.get("duration_ms"), int) and end["duration_ms"] >= 0, end.get("duration_ms")
    assert isinstance(end.get("queue_ms"), int) and end["queue_ms"] >= 0, end.get("queue_ms")

    # 模型正文必须原样保留；末尾追加的系统核对注记是**既有产品行为**
    # （core/turn_facts.py::ANNOTATION_HEADER，round-1 就有，A 没改）。
    # 这里要证的是「可恢复工具错误 ≠ 整轮失败」，不是「答复里一个字都不许多」。
    model_text = "工具失败了，但我换了个办法，这是正式回答。"
    final = str(end.get("final_content") or "")
    assert final.startswith(model_text), (
        "模型正文必须原样保留在 final_content 开头（不得被注记改写/截断）",
        final,
    )
    appended = final[len(model_text):]
    if appended.strip():
        assert appended.strip().startswith("—— 系统核对"), (
            "末尾追加的只能是既有的系统核对注记（后端事实），不得是别的东西",
            appended,
        )
    assert end["status"] in ("completed",), ("可恢复的工具失败绝不能让整轮变成 failed", end)


# ---- 4. 程序中断（应用关闭） -------------------------------------------------------


def test_interrupted_turn_is_not_a_provider_error(db_conn, settings):
    """应用关闭（程序中断）时：reason_code=interrupted、stopped_by=system。

    这里走真实关闭路径（TestClient 退出 = lifespan shutdown），不伪造状态。
    """
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    turn_id: str | None = None
    with TestClient(app) as client:
        app.state.ctx.build_adapter = AsyncMock(return_value=_SlowAdapter())
        turn_id = _submit(client, "这一轮会被程序中断")
        assert _wait_running(client), "turn 没有进入运行中"
    # 退出 with = 应用关闭：不留下永远等不到结果的等待
    ends = [item for item in _turn_ends(app) if str(item.get("turn_id")) == turn_id]
    assert ends, "程序中断后没有 TURN_END（等待者会永远挂住）"
    end = ends[-1]
    _check_common_fields(end)
    assert end["status"] == "cancelled", end["status"]
    assert end["reason_code"] == "interrupted", (
        "进程被关闭属于「程序中断」，不是厂商故障、也不是用户取消",
        end["reason_code"],
    )
    assert end["stopped_by"] == "system", end["stopped_by"]
