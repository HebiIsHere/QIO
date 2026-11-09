"""用户提示词 §一：工具失败进入任务栏的条件（run_shell / web_search / web_fetch）。

产品口径（2026-11-09 本轮）：
* 单次工具失败默认只保留在执行记录里；任务栏只展示值得持续关注的工作、
  需要用户处理的阻碍、以及确实需要持续核查的结果。
* 判断只用结构化事实（tool_name / status / code / category / recoverable /
  turn 结束事实 / 同一轮内的尝试顺序），不按错误文本关键词猜。
* 过滤统一发生在后端快照生成处：当前列表、历史列表、计数、详情入口、
  顶部通知（前端只消费 requiresAttention）都从同一批 item 派生。

本文件锁住：结构化事实入库、同一 turn 同一工具的尝试链只认最后一次、
permission / missing_dependency / 环境 / 搜索未配置 / run_shell 超时的展示、
exit_code 失败不进任务栏、旧记录同规则、计数与分页一致、执行记录原文保留。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import AdapterMode, ToolCall
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.core.loop import AgentLoop
from agent.storage import tool_records
from agent.tools.base import Tool, ToolResult
from agent.tools.cmd_tools import RunCmdTool
from agent.tools.registry import ToolRegistry
from agent.tools.web_fetch import WebFetchTool
from agent.tools.web_search import WebSearchTool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


# ── 装置：真实事实，不伪造 ──────────────────────────────────────────────────


def _end_turn(conn: sqlite3.Connection, turn_id: str, status: str = "completed") -> None:
    """让这一轮在 turn_journal 里真实结束（任务栏判断要用结束事实）。"""
    moment = _now()
    conn.execute(
        "INSERT OR REPLACE INTO turn_journal "
        "(turn_id, message, topic_id, notify, status, created_at, started_at, ended_at, updated_at) "
        "VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?)",
        (turn_id, "测试消息", status, moment, moment, moment, moment),
    )
    conn.commit()


def _record(
    conn: sqlite3.Connection,
    turn_id: str,
    tool_name: str,
    *,
    seq: int = 1,
    status: str = "failed",
    error: str = "",
    code: str | None = None,
    category: str | None = None,
    recoverable: bool | None = None,
    result_status: str | None = None,
    arguments: dict | None = None,
    output: str = "",
) -> str:
    record_id = tool_records.record_tool_call(
        conn,
        turn_id=turn_id,
        tool_name=tool_name,
        arguments=arguments or {},
        output=output,
        status=status,
        error=error,
        duration_ms=1200,
        seq=seq,
        code=code,
        category=category,
        recoverable=recoverable,
        result_status=result_status,
    )
    assert record_id, "工具记录没有写进去（装置失效）"
    return record_id


def _feed(client, view: str = "current", limit: int = 100) -> dict:
    response = client.get(f"/api/tasks?view={view}&limit={limit}")
    assert response.status_code == 200, response.text
    return response.json()


def _all_items(client) -> list[dict]:
    """当前 + 历史两个视图的全部分页项（过滤必须同时作用于两边）。"""
    items: list[dict] = []
    for view in ("current", "history"):
        cursor = None
        while True:
            url = f"/api/tasks?view={view}&limit=100"
            if cursor:
                url += f"&cursor={cursor}"
            response = client.get(url)
            assert response.status_code == 200, response.text
            payload = response.json()
            items.extend(payload["items"])
            cursor = payload.get("nextCursor")
            if not cursor:
                break
    return items


def _find(client, task_id: str, view: str = "current") -> dict | None:
    for item in _feed(client, view)["items"]:
        if item["id"] == task_id:
            return item
    return None


def _tool_ids(items: list[dict]) -> set[str]:
    return {item["id"] for item in items if item["source"] == "tool"}


# ── 1. 同一 turn 的「尝试链」：后续成功补齐的不进任务栏 ────────────────────


def test_run_shell_failure_then_same_command_success_has_no_feed_item(client):
    ctx = client.app.state.ctx
    turn = "turn_shell_retry"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "run_shell", seq=1,
        error="命令退出码 1", code="exit_code", category="code_error",
        result_status="failed", arguments={"cmd": "pytest -q", "cwd": "D:\\work"},
    )
    succeeded = _record(
        ctx.conn, turn, "run_shell", seq=2, status="success", output="全部通过",
        result_status="completed", arguments={"cmd": "pytest -q", "cwd": "D:\\work"},
    )

    items = _all_items(client)
    assert f"tool:{failed}" not in _tool_ids(items), "同一命令随后成功：失败调用只留在执行记录里"
    assert f"tool:{succeeded}" in _tool_ids(items), "成功执行仍按既有规则展示（不重做成功展示）"
    assert _feed(client)["counts"]["failed"] == 0

    # 执行记录保留真实失败与退出码；任务栏详情入口不再提供这一条。
    record = client.get(f"/api/tool-records/{failed}")
    assert record.status_code == 200
    body = record.json()
    assert body["status"] == "failed" and "退出码 1" in body["error"]
    assert client.get(f"/api/tasks/tool:{failed}").status_code == 404


def test_web_search_failure_then_replacement_query_success_has_no_item(client):
    ctx = client.app.state.ctx
    turn = "turn_search_replace"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "web_search", seq=1, error="无法联网搜索：上游 502",
        code="search_failed", category="external_service",
        arguments={"query": "北京 天气"},
    )
    succeeded = _record(
        ctx.conn, turn, "web_search", seq=2, status="success", output="- 结果",
        result_status="completed", arguments={"query": "北京市 天气预报"},
    )

    items = _all_items(client)
    assert f"tool:{failed}" not in _tool_ids(items), "换了查询并成功：原失败是替换尝试，不进任务栏"
    assert f"tool:{succeeded}" in _tool_ids(items)
    assert _feed(client)["counts"]["failed"] == 0


def test_web_fetch_failure_then_same_url_success_has_no_item(client):
    ctx = client.app.state.ctx
    turn = "turn_fetch_retry"
    _end_turn(ctx.conn, turn)
    url = "https://example.com/doc"
    failed = _record(
        ctx.conn, turn, "web_fetch", seq=1, error="抓取失败：连不上该地址",
        code="fetch_network_error", category="external_service", arguments={"url": url},
    )
    succeeded = _record(
        ctx.conn, turn, "web_fetch", seq=2, status="success", output="正文",
        result_status="completed", arguments={"url": url},
    )

    assert f"tool:{failed}" not in _tool_ids(_all_items(client))
    assert _feed(client)["counts"]["failed"] == 0
    assert succeeded  # 成功记录存在，只是失败尝试不再单列


def test_failing_search_chain_is_one_item_not_two(client):
    """换查询后仍然失败：同一尝试链只收敛成一条，且不是每条失败都进任务栏。"""
    ctx = client.app.state.ctx
    turn = "turn_search_chain"
    _end_turn(ctx.conn, turn)
    first = _record(
        ctx.conn, turn, "web_search", seq=1, error="搜索服务未配置",
        code="search_not_configured", category="environment", arguments={"query": "a"},
    )
    second = _record(
        ctx.conn, turn, "web_search", seq=2, error="搜索服务未配置",
        code="search_not_configured", category="environment", arguments={"query": "b"},
    )

    items = _all_items(client)
    tool_items = [item for item in items if item["source"] == "tool"]
    assert len(tool_items) == 1, f"同一条失败链只能出现一条：{tool_items}"
    assert tool_items[0]["id"] == f"tool:{second}"
    assert f"tool:{first}" not in _tool_ids(items)
    assert _feed(client)["counts"]["failed"] == 1


# ── 2. 瞬时失败：本轮正常结束且没有后续处理事项 → 不进任务栏 ───────────────


def test_transient_failure_with_ended_turn_has_no_item(client):
    ctx = client.app.state.ctx
    turn = "turn_transient"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "web_fetch", seq=1,
        error="无法读取正文（HTTP 404）", code="fetch_http_error",
        category="external_service", arguments={"url": "https://example.com/missing"},
    )

    assert f"tool:{failed}" not in _tool_ids(_all_items(client))
    assert _feed(client)["counts"]["failed"] == 0
    record = client.get(f"/api/tool-records/{failed}").json()
    assert record["status"] == "failed" and "HTTP 404" in record["error"]
    # 消息详情随轮次带回的工具预览也仍保留这条真实失败（过滤只作用于任务栏）。
    previews = tool_records.previews_for_turns(ctx.conn, [turn])
    kept = [row for row in previews if row["id"] == failed]
    assert kept and kept[0]["status"] == "failed", "执行记录/消息详情不得删除被过滤的调用"


def test_run_shell_exit_code_failure_has_no_attention_item(client):
    """非零退出码可能是检查结果：如实保留在记录里，不推导成需要处理的任务。"""
    ctx = client.app.state.ctx
    turn = "turn_exit_code"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "run_shell", seq=1, error="命令退出码 2",
        code="exit_code", category="code_error", arguments={"cmd": "git diff --quiet"},
    )

    assert f"tool:{failed}" not in _tool_ids(_all_items(client))
    assert _feed(client)["counts"]["failed"] == 0
    record = client.get(f"/api/tool-records/{failed}").json()
    assert record["status"] == "failed" and "退出码 2" in record["error"]


def test_open_turn_failure_is_not_a_standalone_item(client):
    """这一轮还在跑：模型可能正在重试，任务栏不先冒一条独立失败；结束后再按事实判定。"""
    ctx = client.app.state.ctx
    turn = "turn_while_running"
    ctx.conn.execute(
        "INSERT INTO turn_journal (turn_id, message, status, created_at, updated_at) "
        "VALUES (?, ?, 'running', ?, ?)",
        (turn, "测试消息", _now(), _now()),
    )
    ctx.conn.commit()
    failed = _record(
        ctx.conn, turn, "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "rm -rf build"},
    )

    assert f"tool:{failed}" not in _tool_ids(_all_items(client))

    # 这一轮真实结束之后（读时重新生成），真正的需要处理项才出现。
    ctx.conn.execute(
        "UPDATE turn_journal SET status = 'completed', ended_at = ?, updated_at = ? "
        "WHERE turn_id = ?",
        (_now(), _now(), turn),
    )
    ctx.conn.commit()
    item = _find(client, f"tool:{failed}")
    assert item is not None and item["requiresAttention"] is True


# ── 3. 需要用户处理 / 待核查的失败：展示且说清要做什么 ─────────────────────


def test_permission_failure_is_shown_with_actionable_summary(client):
    ctx = client.app.state.ctx
    turn = "turn_permission"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "run_shell", seq=1,
        error="命令在当前模式下被拒绝：shell 命令需要用户批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "rm -rf build"},
    )

    item = _find(client, f"tool:{failed}")
    assert item is not None, "缺少授权是用户必须处理的阻碍，应进入任务栏"
    assert item["status"] == "failed" and item["rawStatus"] == "failed"
    assert item["requiresAttention"] is True
    assert item["summary"], "需要处理的条目必须有可读说明"
    assert ("授权" in item["summary"] or "权限" in item["summary"]), item["summary"]
    assert ("批准" in item["summary"] or "允许" in item["summary"]), item["summary"]

    detail = client.get(f"/api/tasks/tool:{failed}")
    assert detail.status_code == 200
    texts = [part["text"] for part in detail.json()["parts"]]
    assert any("需要用户批准" in text for text in texts), "原始错误仍在详情里可读"
    assert _feed(client)["counts"]["failed"] == 1


def test_missing_dependency_failure_is_shown_and_says_what_to_do(client):
    ctx = client.app.state.ctx
    turn = "turn_missing_dep"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "run_shell", seq=1,
        error="ModuleNotFoundError: No module named 'pandas'",
        code="missing_dependency", category="missing_dependency", recoverable=False,
        arguments={"cmd": "python report.py"},
    )

    item = _find(client, f"tool:{failed}")
    assert item is not None
    assert item["requiresAttention"] is True
    assert "依赖" in item["summary"] or "补齐" in item["summary"], item["summary"]
    assert _feed(client)["counts"]["failed"] == 1


def test_run_shell_timeout_is_shown_as_needs_verification(client):
    """命令超时时可能已经产生副作用，无法确认是否生效 → 待核查，不是普通瞬时失败。"""
    ctx = client.app.state.ctx
    turn = "turn_timeout"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "run_shell", seq=1, error="命令执行超时",
        code="timeout", category="timeout", recoverable=True,
        arguments={"cmd": "python migrate.py"},
    )

    item = _find(client, f"tool:{failed}")
    assert item is not None, "无法确认命令是否已生效，必须让用户持续核查"
    assert item["requiresAttention"] is True
    assert ("核查" in item["summary"] or "确认" in item["summary"]), item["summary"]
    assert _feed(client)["counts"]["failed"] == 1


def test_search_not_configured_is_shown_with_configuration_hint(client):
    ctx = client.app.state.ctx
    turn = "turn_search_config"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "web_search", seq=1, error="搜索服务未配置",
        code="search_not_configured", category="environment",
        arguments={"query": "今天的新闻"},
    )

    item = _find(client, f"tool:{failed}")
    assert item is not None
    assert item["requiresAttention"] is True
    assert "配置" in item["summary"] and "搜索" in item["summary"], item["summary"]


def test_other_tools_keep_existing_failure_display_rules(client):
    """本轮只处理这三个工具：其它工具的展示规则不变（不把同一规则无条件下沉）。"""
    ctx = client.app.state.ctx
    knowledge = _record(
        ctx.conn, "turn_other_knowledge", "propose_knowledge", seq=1,
        error="参数校验失败：content 不能为空",
    )
    program = _record(
        ctx.conn, "turn_other_program", "run_program", seq=1,
        error="程序退出码 1", code="exit_code", category="code_error",
    )

    first = _find(client, f"tool:{knowledge}")
    second = _find(client, f"tool:{program}")
    assert first is not None and first["requiresAttention"] is True
    assert second is not None and second["requiresAttention"] is True


# ── 4. 其他调用成功不能清除真正的阻碍；失败不能跨工具「抵消」 ───────────────


def test_success_in_same_turn_does_not_clear_actionable_failure(client):
    ctx = client.app.state.ctx
    turn = "turn_mixed"
    _end_turn(ctx.conn, turn)
    denied = _record(
        ctx.conn, turn, "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "rm -rf build"},
    )
    _record(
        ctx.conn, turn, "web_search", seq=2, status="success", output="- 结果",
        result_status="completed", arguments={"query": "build 目录是什么"},
    )

    item = _find(client, f"tool:{denied}")
    assert item is not None, "另一条成功调用不能把需要授权的失败清掉"
    assert _feed(client)["counts"]["failed"] == 1


# ── 5. 计数 / 当前 / 历史 / 分页一致；详情入口随过滤变化 ───────────────────


def test_filtered_failures_never_appear_in_views_counts_or_pages(client):
    ctx = client.app.state.ctx
    _end_turn(ctx.conn, "turn_filtered_exit")
    exit_failed = _record(
        ctx.conn, "turn_filtered_exit", "run_shell", seq=1, error="命令退出码 1",
        code="exit_code", category="code_error", arguments={"cmd": "pytest -q"},
    )
    _end_turn(ctx.conn, "turn_filtered_fetch")
    http_failed = _record(
        ctx.conn, "turn_filtered_fetch", "web_fetch", seq=1,
        error="无法读取正文（HTTP 404）", code="fetch_http_error",
        category="external_service", arguments={"url": "https://example.com/x"},
    )
    _end_turn(ctx.conn, "turn_keep_permission")
    kept_permission = _record(
        ctx.conn, "turn_keep_permission", "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "rm -rf build"},
    )
    _end_turn(ctx.conn, "turn_keep_timeout")
    kept_timeout = _record(
        ctx.conn, "turn_keep_timeout", "run_shell", seq=1, error="命令执行超时",
        code="timeout", category="timeout", recoverable=True, arguments={"cmd": "npm install"},
    )

    # 当前视图分页：逐页取回，过滤项一页都不出现，保留项都在。
    current_ids: list[str] = []
    cursor = None
    while True:
        url = "/api/tasks?view=current&limit=1"
        if cursor:
            url += f"&cursor={cursor}"
        page = client.get(url).json()
        current_ids.extend(item["id"] for item in page["items"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    assert f"tool:{exit_failed}" not in current_ids
    assert f"tool:{http_failed}" not in current_ids
    assert {f"tool:{kept_permission}", f"tool:{kept_timeout}"} <= set(current_ids)

    history_ids = {
        item["id"] for item in _feed(client, "history", limit=100)["items"]
    }
    assert f"tool:{exit_failed}" not in history_ids
    assert f"tool:{http_failed}" not in history_ids

    counts = _feed(client)["counts"]
    assert counts["failed"] == 2, "需要处理数量只算真正保留的两条"

    assert client.get(f"/api/tasks/tool:{kept_permission}").status_code == 200
    assert client.get(f"/api/tasks/tool:{kept_timeout}").status_code == 200
    assert client.get(f"/api/tasks/tool:{exit_failed}").status_code == 404
    assert client.get(f"/api/tasks/tool:{http_failed}").status_code == 404
    # 执行记录本身仍可读（过滤只作用于任务栏）。
    assert client.get(f"/api/tool-records/{exit_failed}").json()["status"] == "failed"
    assert client.get(f"/api/tool-records/{http_failed}").json()["status"] == "failed"


def test_repeated_reads_do_not_duplicate_or_resurface_items(client):
    """稳定身份去重：同一失败链重复读取只出现一条，不重放。"""
    ctx = client.app.state.ctx
    turn = "turn_stable"
    _end_turn(ctx.conn, turn)
    failed = _record(
        ctx.conn, turn, "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "rm -rf build"},
    )

    ids = []
    for _ in range(3):
        items = _all_items(client)
        ids.append(sorted(_tool_ids(items)))
    assert ids[0] == ids[1] == ids[2] == [f"tool:{failed}"]
    assert _feed(client)["counts"]["failed"] == 1


def test_failure_without_turn_end_fact_is_not_shown(client):
    """没有 turn 结束事实（旧记录 / 台账已清理）时不猜「本轮已正常结束」，默认不展示。"""
    ctx = client.app.state.ctx
    failed = _record(
        ctx.conn, "turn_without_journal", "run_shell", seq=1, error="命令未获批准",
        code="permission_denied", category="permission", recoverable=False,
        arguments={"cmd": "rm -rf build"},
    )

    assert f"tool:{failed}" not in _tool_ids(_all_items(client))
    assert _feed(client)["counts"]["failed"] == 0
    # 真实失败仍在执行记录里（不删除、不改写成成功）。
    assert client.get(f"/api/tool-records/{failed}").json()["status"] == "failed"


# ── 6. 旧记录（没有新列 / 空 code）同样过滤 ────────────────────────────────


def test_legacy_rows_without_structured_facts_are_filtered(client):
    """迁移前的失败行没有 code/category：默认按「不值得进任务栏」处理，不重放历史错误。"""
    ctx = client.app.state.ctx
    turn = "turn_legacy"
    _end_turn(ctx.conn, turn)
    for index, (tool_name, error) in enumerate(
        (("run_shell", "命令退出码 1"), ("web_fetch", "抓取失败：连不上 目标地址"))
    ):
        ctx.conn.execute(
            "INSERT INTO tool_records "
            "(id, turn_id, topic_id, call_id, seq, tool_name, arguments, output, status, "
            " error, duration_ms, truncated, output_missing, missing_reason, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, '', 'failed', ?, 900, 0, 0, '', ?)",
            (
                f"tr_legacy_{index}",
                turn,
                f"call_legacy_{index}",
                index + 1,
                tool_name,
                json.dumps({"cmd": "pytest -q"} if tool_name == "run_shell" else {"url": "https://x"}),
                error,
                _now(),
            ),
        )
    ctx.conn.commit()

    tool_ids = _tool_ids(_all_items(client))
    assert tool_ids == set(), f"旧失败行不得在首次加载/重连后重新冒出来：{tool_ids}"
    assert _feed(client)["counts"]["failed"] == 0
    row = ctx.conn.execute(
        "SELECT code, category FROM tool_records WHERE id = 'tr_legacy_0'"
    ).fetchone()
    assert row["code"] is None and row["category"] is None, "旧行的结构化事实如实为空"


# ── 7. 数据链：TOOL_END 与 tool_records 的结构化事实（与 §四 的接缝）──────


class _AwaitingDemoTool(Tool):
    name = "propose_demo"
    description = "演示：提交后等待审批"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(
            ok=True,
            content="已提交审批，等待用户确认",
            status="awaiting_approval",
            code="submitted",
            protocol_version=1,
        )


class _PermissionDeniedDemoTool(Tool):
    name = "shell_demo"
    description = "演示：缺少授权"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(
            ok=False,
            error="命令未获批准",
            category="permission",
            recoverable=False,
            code="permission_denied",
        )


class _Adapter:
    mode = AdapterMode.NATIVE
    model = "fake"


def _http_events(chunks: list[str]) -> list[dict]:
    out: list[dict] = []
    for line in chunks:
        for part in line.splitlines():
            if part.startswith("data: "):
                out.append(json.loads(part[6:]))
    return out


def _dispatch_and_collect(trace, tool, *, turn_id: str) -> tuple[list[dict], str | None]:
    registry = ToolRegistry()
    registry.register(tool)
    bus = EventBus()
    loop = AgentLoop(_Adapter(), registry, bus, tool_trace=trace, turn_id=turn_id)

    async def run() -> list[dict]:
        chunks: list[str] = []

        async def consume() -> None:
            async for chunk in bus.stream():
                chunks.append(chunk)
                if len([e for e in _http_events(chunks) if e["type"] == "TOOL_END"]) >= 1:
                    return

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        await loop._dispatch_tool_calls(
            [ToolCall(id=f"call_{tool.name}_{turn_id}", name=tool.name, arguments={})]
        )
        await asyncio.wait_for(task, timeout=5)
        return _http_events(chunks)

    events = asyncio.run(run())
    end = next(event for event in events if event["type"] == "TOOL_END")["data"]
    return events, end.get("record_id")


def test_tool_end_exposes_code_and_real_result_status(client):
    """§四 的接缝：事件带稳定 code 与真实 ToolResult.status（等待审批不能被归一成成功）。"""
    ctx = client.app.state.ctx
    events, record_id = _dispatch_and_collect(
        ctx._record_tool_call, _AwaitingDemoTool(), turn_id="turn_seam_ok"
    )
    end = next(event for event in events if event["type"] == "TOOL_END")["data"]
    assert end["status"] == "success", "事件层终态仍是归一后的 success"
    assert end["code"] == "submitted"
    assert end["result_status"] == "awaiting_approval", "真实等待审批事实不能被归一成完成"

    assert record_id
    record = tool_records.get_record(ctx.conn, record_id)
    assert record["status"] == "success", "tool_records.status 仍只允许 success/failed/cancelled"
    assert record["result_status"] == "awaiting_approval", "真实等待审批事实不得丢失"
    assert record["code"] == "submitted"


def test_tool_end_failure_exposes_code_category_and_recoverable(client):
    ctx = client.app.state.ctx
    events, record_id = _dispatch_and_collect(
        ctx._record_tool_call, _PermissionDeniedDemoTool(), turn_id="turn_seam_fail"
    )
    end = next(event for event in events if event["type"] == "TOOL_END")["data"]
    assert end["status"] == "failed"
    assert end["code"] == "permission_denied"
    assert end["category"] == "permission"
    assert end["recoverable"] is False

    assert record_id
    record = tool_records.get_record(ctx.conn, record_id)
    assert record["status"] == "failed"
    assert record["code"] == "permission_denied"
    assert record["category"] == "permission"
    assert record["recoverable"] is False


class _ExitCodeShellStub(Tool):
    name = "run_shell"
    description = "演示：命令以非零退出码结束"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=False, error="命令退出码 1", code="exit_code")


def test_full_chain_run_shell_exit_code_is_recorded_then_filtered(client):
    """ToolResult.code → loop 轨迹 → tool_records → 任务栏：整条链的端到端事实。"""
    ctx = client.app.state.ctx
    turn = "turn_chain_exit"
    _end_turn(ctx.conn, turn)
    events, record_id = _dispatch_and_collect(
        ctx._record_tool_call, _ExitCodeShellStub(), turn_id=turn
    )
    end = next(event for event in events if event["type"] == "TOOL_END")["data"]
    assert end["code"] == "exit_code"
    assert record_id

    record = tool_records.get_record(ctx.conn, record_id)
    assert record["status"] == "failed" and record["code"] == "exit_code"
    assert "退出码 1" in record["error"]

    assert f"tool:{record_id}" not in _tool_ids(_all_items(client))
    assert _feed(client)["counts"]["failed"] == 0


# ── 8. 工具侧：只补少量稳定 code，不靠中文文本匹配 ─────────────────────────


def test_web_search_failures_have_stable_codes():
    not_configured = asyncio.run(WebSearchTool(search_service=None).run(query="q"))
    assert not_configured.code == "search_not_configured"
    assert not_configured.category == "environment"

    missing = asyncio.run(WebSearchTool(search_service=None).run(query=""))
    assert missing.code == "missing_argument"

    class _ErrorService:
        async def search(self, query, top_k):
            return SimpleNamespace(status="error", error="上游 502", hits=[])

    failed = asyncio.run(WebSearchTool(search_service=_ErrorService()).run(query="q"))
    assert failed.code == "search_failed"
    assert failed.category == "external_service"


def test_web_fetch_failures_have_stable_codes():
    async def _raise_network(url):
        raise ConnectionError("dns 失败")

    async def _http_404(url):
        return 404, "<html>not found</html>"

    async def _login_wall(url):
        return 200, "<html>Access Denied：please sign in</html>"

    async def _js_required(url):
        return 200, "<html>Please enable JavaScript to continue</html>"

    async def _empty(url):
        return 200, "<html><head><style>body{}</style></head><body><script>x</script></body></html>"

    async def _run(handler, url="https://example.com/x"):
        return await WebFetchTool(fetch_html=handler).run(url=url)

    missing = asyncio.run(WebFetchTool(fetch_html=_http_404).run(url=""))
    assert missing.code == "missing_argument"

    network = asyncio.run(_run(_raise_network))
    assert network.code == "fetch_network_error" and network.category == "external_service"

    http_error = asyncio.run(_run(_http_404))
    assert http_error.code == "fetch_http_error" and http_error.category == "external_service"

    login = asyncio.run(_run(_login_wall))
    assert login.code == "fetch_login_required"

    js = asyncio.run(_run(_js_required))
    assert js.code == "fetch_js_required"

    empty = asyncio.run(_run(_empty))
    assert empty.code == "fetch_empty"


def test_run_shell_failure_codes_and_missing_command():
    missing = asyncio.run(RunCmdTool().run(cmd=""))
    assert missing.code == "missing_argument"

    tool = RunCmdTool()
    exit_code = tool._launch_result_to_tool(
        SimpleNamespace(
            ok=False, category="exit_code", returncode=2, error=None,
            stdout="out", stderr="err", isolation=None, guard=None, spec_digest="d",
        ),
        label="命令",
    )
    assert exit_code.code == "exit_code"
    assert "退出码 2" in (exit_code.error or "")

    timeout = tool._launch_result_to_tool(
        SimpleNamespace(
            ok=False, category="timeout", returncode=None, error="执行超时",
            stdout="", stderr="", isolation=None, guard=None, spec_digest="d",
        ),
        label="命令",
    )
    assert timeout.code == "timeout"
    assert timeout.category == "timeout"


# ── 9. 迁移：只追加新列，旧行默认值可用 ────────────────────────────────────


def test_tool_records_gains_structured_fact_columns(db_conn: sqlite3.Connection):
    columns = {str(row[1]) for row in db_conn.execute("PRAGMA table_info(tool_records)")}
    assert {"code", "category", "recoverable", "result_status"} <= columns

    from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

    assert SCHEMA_VERSION == COMPENSATION_VERSION == max(target for target, _ in MIGRATIONS)
    compensation = "\n".join(
        str(statement)
        for target, statements in MIGRATIONS
        if target == COMPENSATION_VERSION
        for statement in statements
    )
    for column in ("code", "category", "recoverable", "result_status"):
        assert f"ADD COLUMN {column}" in compensation, (
            f"补偿迁移必须能补回 tool_records.{column}（REQUIRED_OBJECTS 同口径）"
        )
