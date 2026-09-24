# 工具调用历史（参数与输出落库）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每次工具调用都落库（打码后的参数与输出全文、状态、错误、耗时），刷新/重开/切话题之后仍能在对话里展开看到当时到底发生了什么。

**Architecture:** 新建一张用途单一的表 `tool_records`（不碰 `messages`、不改轨迹表），写入挂在主循环的工具终态事件上，读取走历史接口附带预览 + 按 id 取全文，两个设置项控制"是否存输出全文"与"输出保留几天"。

**Tech Stack:** Python 3.12 / pytest / SQLite 顺序迁移 / Vue 3 + Pinia + vitest。

**Spec:** `docs/superpowers/specs/2026-09-24-tool-record-history-design.md`

## Global Constraints

- 迁移只允许追加（本轮是 20），禁止修改历史迁移。
- 密钥原文不得入库：新增写入路径必须过 `agent/trace/redact.py`。
- 工具记录**不进**上下文、片段摘要、检索索引；模型读不到（用户已选「只给人看」）。
- 子任务内部的工具调用不入表（与「子任务不进主对话」的既有边界一致）。
- 历史卡片与实时卡片是同一张卡、同一份数据来源。
- 不写会过期的硬编码数字到文档；文案一律中文。
- 工作树是多路并行状态：不执行 `git` 写操作，只改本计划列出的文件。
- 收尾验证：后端全量 `uv run --frozen pytest`、评测 `uv run --frozen python -m agent.eval.run`、前端 `npx vue-tsc --noEmit` + `npm test`、`python scripts/check_docs.py`、隔离实例截图。

---

### Task 1: 存储层（新表 + 迁移 20 + 读写清理）

**Files:**
- Modify: `backend/src/agent/storage/schema.py`（迁移 20）
- Create: `backend/src/agent/storage/tool_records.py`
- Test: `backend/tests/test_tool_records.py`

**Interfaces:**
- Produces:
  - `record_tool_call(conn, *, turn_id, topic_id, call_id, seq, tool_name, arguments, output, status, error, duration_ms, save_output) -> str | None`
  - `previews_for_turns(conn, turn_ids) -> list[dict]`
  - `get_record(conn, record_id) -> dict | None`
  - `prune_outputs(conn, retention_days) -> int`
  - 常量 `MAX_TEXT_CHARS = 40_000`、`PREVIEW_CHARS = 400`

- [x] **Step 1: 写失败测试**

```python
from __future__ import annotations

import sqlite3

from agent.storage.tool_records import (
    MAX_TEXT_CHARS,
    get_record,
    previews_for_turns,
    prune_outputs,
    record_tool_call,
)


def _record(conn, **over):
    payload = dict(
        turn_id="turn_1", topic_id="topic_1", call_id="call_1", seq=1,
        tool_name="fs_list", arguments={"path": "."}, output="列目录成功",
        status="success", error="", duration_ms=12, save_output=True,
    )
    payload.update(over)
    return record_tool_call(conn, **payload)


def test_record_and_read_full(db_conn: sqlite3.Connection):
    rid = _record(db_conn, output="完整输出")
    assert rid and rid.startswith("tr_")
    row = get_record(db_conn, rid)
    assert row["arguments"] == {"path": "."}
    assert row["output"] == "完整输出"
    assert row["status"] == "success"
    assert row["output_missing"] == 0


def test_secrets_are_redacted_before_storage(db_conn: sqlite3.Connection):
    rid = _record(
        db_conn,
        arguments={"path": ".", "api_key": "sk-live-abcdef123456"},
        output="token=sk-live-abcdef123456\nBearer abcdefghijklmn",
    )
    row = get_record(db_conn, rid)
    assert "sk-live-abcdef123456" not in row["output"]
    assert "***redacted***" in row["output"]
    assert row["arguments"]["api_key"] == "***redacted***"


def test_long_output_is_truncated_with_marker(db_conn: sqlite3.Connection):
    rid = _record(db_conn, output="A" * (MAX_TEXT_CHARS + 500))
    row = get_record(db_conn, rid)
    assert row["truncated"] is True
    assert len(row["output"]) < MAX_TEXT_CHARS + 200
    assert "已截断" in row["output"]


def test_output_at_limit_is_not_truncated(db_conn: sqlite3.Connection):
    rid = _record(db_conn, output="A" * MAX_TEXT_CHARS)
    assert get_record(db_conn, rid)["truncated"] is False


def test_save_output_off_keeps_metadata(db_conn: sqlite3.Connection):
    rid = _record(db_conn, output="不该被保存", save_output=False)
    row = get_record(db_conn, rid)
    assert row["output"] == ""
    assert row["output_missing"] == 1
    assert row["missing_reason"] == "setting"
    assert row["arguments"] == {"path": "."}


def test_same_call_written_twice_stays_one_row(db_conn: sqlite3.Connection):
    first = _record(db_conn)
    second = _record(db_conn)
    assert first == second
    count = db_conn.execute("SELECT COUNT(*) c FROM tool_records").fetchone()["c"]
    assert count == 1


def test_previews_for_turns_order_and_shape(db_conn: sqlite3.Connection):
    _record(db_conn, call_id="c1", seq=2, output="X" * 1000)
    _record(db_conn, call_id="c2", seq=1, output="第二个")
    items = previews_for_turns(db_conn, ["turn_1"])
    assert [i["seq"] for i in items] == [1, 2]
    assert len(items[0]["preview"]) == 400 or len(items[0]["preview"]) == len("第二个")
    assert items[0]["title"]          # 中文展示名由 tool_label 现算
    assert items[0]["output_chars"] == len("第二个")


def test_prune_clears_output_but_keeps_record(db_conn: sqlite3.Connection):
    old = _record(db_conn, call_id="old", output="很久以前的输出")
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,)
    )
    fresh = _record(db_conn, call_id="fresh", output="今天的输出")

    purged = prune_outputs(db_conn, 90)

    assert purged == 1
    assert get_record(db_conn, old)["output"] == ""
    assert get_record(db_conn, old)["missing_reason"] == "retention"
    assert get_record(db_conn, old)["arguments"] == {"path": "."}
    assert get_record(db_conn, fresh)["output"] == "今天的输出"


def test_retention_zero_never_prunes(db_conn: sqlite3.Connection):
    old = _record(db_conn, call_id="old", output="很久以前的输出")
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,)
    )
    assert prune_outputs(db_conn, 0) == 0
    assert get_record(db_conn, old)["output"] == "很久以前的输出"


def test_missing_record_returns_none(db_conn: sqlite3.Connection):
    assert get_record(db_conn, "tr_nope") is None


def test_legacy_db_gets_tool_records_table(tmp_path):
    """老库升级：19 版本之前没有这张表，迁移必须补上且不丢旧数据。"""
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations
    from agent.storage.schema import MIGRATIONS

    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > 19:
            break
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) "
            "VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )
    conn.execute(
        "INSERT INTO tool_calls (id, tool_name, arguments, result, ok, created_at) "
        "VALUES ('tc_old', 'fs_list', '{}', '', 0, '2026-01-01T00:00:00+00:00')"
    )

    apply_migrations(conn)

    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tool_records" in tables
    assert conn.execute("SELECT COUNT(*) c FROM tool_calls").fetchone()["c"] == 1
    conn.close()
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_tool_records.py -q`
Expected: FAIL（`ModuleNotFoundError: agent.storage.tool_records`）

- [x] **Step 3: 实现迁移 20（追加在 `MIGRATIONS` 末尾）**

```python
    (
        20,
        [
            # 工具调用历史：用户能回看的完整记录（打码后的参数与输出）。
            # 与轨迹表分工不同 —— 轨迹是审计摘要且可被用户关掉，这里是复盘用的正文。
            """
            CREATE TABLE IF NOT EXISTS tool_records (
                id             TEXT PRIMARY KEY,
                turn_id        TEXT NOT NULL,
                topic_id       TEXT,
                call_id        TEXT NOT NULL DEFAULT '',
                seq            INTEGER NOT NULL DEFAULT 0,
                tool_name      TEXT NOT NULL,
                arguments      TEXT NOT NULL DEFAULT '{}',
                output         TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'success',
                error          TEXT NOT NULL DEFAULT '',
                duration_ms    INTEGER,
                truncated      INTEGER NOT NULL DEFAULT 0,
                output_missing INTEGER NOT NULL DEFAULT 0,
                missing_reason TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tool_records_turn ON tool_records(turn_id)",
            "CREATE INDEX IF NOT EXISTS idx_tool_records_created ON tool_records(created_at)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_records_call
                ON tool_records(turn_id, call_id) WHERE call_id <> ''
            """,
        ],
    ),
```

- [x] **Step 4: 实现 `storage/tool_records.py`**

```python
"""工具调用历史：写入 / 读取 / 清理。

与轨迹表的分工：`tool_calls`、`turn_traces` 是审计用途（截断摘要、可被用户关掉）；
这里存的是用户能回看的完整记录（打码后的参数与输出全文，可设置是否保存、按天数清理），
只服务界面复盘 —— 不进上下文、不进摘要、不进检索索引，模型读不到。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.trace.redact import redact_any, redact_text

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 40_000
PREVIEW_CHARS = 400
STATUSES = ("success", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    from agent.memory.fragment import new_id

    return new_id("tr")


def _clip(text: str) -> tuple[str, bool]:
    """超长文本截断（写进正文里，前端只看 truncated 徽标）。"""
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return f"{text[:MAX_TEXT_CHARS]}\n…（已截断，原文 {len(text)} 字）", True


def record_tool_call(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    topic_id: str | None = None,
    call_id: str = "",
    seq: int = 0,
    tool_name: str,
    arguments: Any = None,
    output: str = "",
    status: str = "success",
    error: str = "",
    duration_ms: int | None = None,
    save_output: bool = True,
) -> str | None:
    """写一行工具记录；返回记录 id（重复调用返回已有行的 id，写库失败返回 None）。"""
    if status not in STATUSES:
        status = "failed"
    args_text, args_truncated = _clip(
        json.dumps(redact_any(arguments or {}), ensure_ascii=False)
    )
    if save_output:
        output_text, truncated = _clip(redact_text(output or ""))
        output_missing, missing_reason = 0, ""
    else:
        output_text, truncated, output_missing, missing_reason = "", False, 1, "setting"
    record_id = _new_id()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tool_records "
            "(id, turn_id, topic_id, call_id, seq, tool_name, arguments, output, status, "
            " error, duration_ms, truncated, output_missing, missing_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                str(turn_id or ""),
                topic_id,
                str(call_id or ""),
                int(seq or 0),
                str(tool_name or "?"),
                args_text,
                output_text,
                status,
                str(error or ""),
                int(duration_ms) if duration_ms is not None else None,
                1 if (truncated or args_truncated) else 0,
                output_missing,
                missing_reason,
                _now(),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:  # 记历史失败绝不改变工具结果
        logger.warning("tool record insert failed: %s", exc)
        return None
    if call_id:
        row = conn.execute(
            "SELECT id FROM tool_records WHERE turn_id = ? AND call_id = ?",
            (str(turn_id or ""), str(call_id)),
        ).fetchone()
        if row is not None:
            return row["id"]
    return record_id


def _preview(row: dict) -> dict:
    from agent.tools.display import tool_label  # 惰性导入：避免 tools 包初始化顺序问题

    output = row.get("output") or ""
    return {
        "id": row["id"],
        "turn_id": row["turn_id"],
        "call_id": row["call_id"],
        "seq": row["seq"],
        "tool_name": row["tool_name"],
        "title": tool_label(row["tool_name"]),
        "status": row["status"],
        "error": row["error"],
        "duration_ms": row["duration_ms"],
        "truncated": bool(row["truncated"]),
        "output_missing": bool(row["output_missing"]),
        "missing_reason": row["missing_reason"],
        "output_chars": len(output),
        "preview": output[:PREVIEW_CHARS],
        "created_at": row["created_at"],
    }


def previews_for_turns(conn: sqlite3.Connection, turn_ids: list[str]) -> list[dict]:
    ids = [str(t) for t in turn_ids if str(t or "")]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT id, turn_id, call_id, seq, tool_name, output, status, error, duration_ms, "
        "truncated, output_missing, missing_reason, created_at FROM tool_records "
        f"WHERE turn_id IN ({placeholders}) ORDER BY created_at ASC, seq ASC",
        ids,
    ).fetchall()
    return [_preview(dict(r)) for r in rows]


def get_record(conn: sqlite3.Connection, record_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tool_records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return None
    data = _preview(dict(row))
    raw_args = row["arguments"] or "{}"
    try:
        data["arguments"] = json.loads(raw_args)
    except (TypeError, ValueError):
        data["arguments"] = raw_args
    data["output"] = row["output"] or ""
    return data


def prune_outputs(
    conn: sqlite3.Connection, retention_days: int, *, now: datetime | None = None
) -> int:
    """清掉过期记录的输出全文（记录本身保留）；`retention_days<=0` 表示永久保留。"""
    if not retention_days or int(retention_days) <= 0:
        return 0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=int(retention_days))).isoformat()
    cur = conn.execute(
        "UPDATE tool_records SET output = '', output_missing = 1, missing_reason = 'retention' "
        "WHERE output_missing = 0 AND created_at < ?",
        (cutoff,),
    )
    conn.commit()
    return int(cur.rowcount or 0)
```

- [x] **Step 5: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_tool_records.py -q`
Expected: PASS

---

### Task 2: 写入接线（循环终态 + 审计回调）

**Files:**
- Modify: `backend/src/agent/core/loop.py`（`_on_pipeline_result` 暂存、`_on_pipeline_end` 写记录、TOOL_END 带 record_id）
- Modify: `backend/src/agent/services/app.py:952`（`_record_tool_call` 同时写历史记录）
- Test: `backend/tests/test_tool_call_audit.py`（追加）

**Interfaces:**
- Consumes: `record_tool_call(...)`（Task 1）
- Produces: `TOOL_END` 事件新增 `record_id`；`_record_tool_call(trace) -> str | None`

- [x] **Step 1: 写失败测试**

```python
def test_tool_record_is_written_with_full_output(ctx: AppContext):
    ctx._record_tool_call({
        "tool_name": "fs_list", "arguments": {"path": "."}, "ok": False,
        "result": "列目录失败：找不到路径", "error": "列目录失败：找不到路径",
        "call_id": "call_x", "turn_id": "turn_x", "seq": 1, "duration_ms": 7,
        "status": "failed",
    })
    row = ctx.conn.execute(
        "SELECT turn_id, call_id, output, status, error, duration_ms "
        "FROM tool_records ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["turn_id"] == "turn_x" and row["call_id"] == "call_x"
    assert row["status"] == "failed" and row["duration_ms"] == 7
    assert "找不到路径" in row["output"]


def test_record_outputs_setting_off_skips_output(ctx: AppContext):
    ctx.settings_store.set("tools.record_outputs", "0")
    ctx._record_tool_call({
        "tool_name": "fs_list", "arguments": {}, "ok": True, "result": "秘密内容",
        "error": "", "call_id": "call_y", "turn_id": "turn_y", "seq": 1,
        "duration_ms": 1, "status": "success",
    })
    row = ctx.conn.execute(
        "SELECT output, output_missing, missing_reason FROM tool_records "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["output"] == "" and row["output_missing"] == 1
    assert row["missing_reason"] == "setting"


async def test_tool_end_event_carries_record_id():
    """实时卡片要能拿到 record_id 才能和历史上一样取全文。"""
    loop, bus = _loop_with_recorder()          # 见下方实现说明：注册一个返回 "tr_test" 的 tool_trace
    await loop.run("go")
    ends = [e for e in bus.events if e.type == "tool_end"]
    assert ends and ends[-1].data["record_id"] == "tr_test"
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_tool_call_audit.py -q`
Expected: FAIL（`no such table: tool_records` / 载荷里没有新字段）

- [x] **Step 3: 实现**

`loop.py::__init__` 加：`self._call_seq: dict[str, int] = {}`、`self._call_seq_next = 0`、
`self._pending_tool_io: dict[str, dict] = {}`（`tool/result` 时暂存 `call` 与 `result`）；
`tool_trace` 的回调返回值（记录 id）存进 `_tool_facts`。

`_dispatch_tool_calls` 派发时分配序号：

```python
        for c in calls:
            self._dispatched_call_ids.add(c.id)
            self._call_seq_next += 1
            self._call_seq[c.id] = self._call_seq_next
```

`_on_pipeline_result` 改为暂存（不再直接写审计行）：

```python
    async def _on_pipeline_result(self, data: dict) -> None:
        result = data.get("result")
        call = data.get("call")
        if result is None or call is None:
            return
        if getattr(call, "id", None) not in self._dispatched_call_ids:
            return
        # 终态在 tool/end 才权威（取消与失败要分开），全文只有这里拿得到 → 先暂存
        self._pending_tool_io[str(call.id)] = {"call": call, "result": result}
```

`_on_pipeline_end` 在算出 `status` / `duration_ms` 之后、发 `TOOL_END` 之前落库：

```python
        record_id = None
        pending = self._pending_tool_io.pop(str(call_id or ""), None)
        if self.tool_trace is not None and pending is not None:
            call = pending["call"]
            result = pending["result"]
            try:
                record_id = self.tool_trace({
                    "tool_name": tool_name,
                    "arguments": getattr(call, "arguments", {}),
                    "ok": bool(result.ok),
                    "result": result.content,
                    "error": result.error,
                    "call_id": str(call_id or ""),
                    "turn_id": self.turn_id,
                    "seq": self._call_seq.get(str(call_id or ""), 0),
                    "duration_ms": duration_ms,
                    # 终态语义：取消不是失败的一种（与核心状态层一致）
                    "status": "cancelled" if data.get("cancelled") else (
                        "success" if result.ok else "failed"
                    ),
                })
            except Exception:  # noqa: BLE001 - 审计与历史都不得影响工具结果
                logger.warning("tool trace failed for %s", tool_name, exc_info=True)
```

`_tool_facts` 合并写入（**不能整条覆盖**，否则 record_id 会丢）：

```python
        if call_id:
            self._tool_facts[str(call_id)] = {
                **(self._tool_facts.get(str(call_id)) or {}),
                "status": status,
                "duration_ms": duration_ms,
                "error": data.get("error"),
                "record_id": record_id,
            }
```

`TOOL_END` 载荷加 `"record_id": (self._tool_facts.get(str(call_id or "")) or {}).get("record_id")`。

`app.py::_record_tool_call` 在写完审计行后写历史记录并返回 id：

```python
        from agent.storage.tool_records import record_tool_call

        record_id = record_tool_call(
            self.conn,
            turn_id=str(trace.get("turn_id") or ""),
            topic_id=active.topic_id if active else None,
            call_id=str(trace.get("call_id") or ""),
            seq=int(trace.get("seq") or 0),
            tool_name=str(trace.get("tool_name") or "?"),
            arguments=trace.get("arguments") or {},
            output=str(trace.get("result") or ""),
            status=str(trace.get("status") or ("success" if trace.get("ok") else "failed")),
            error=str(trace.get("error") or ""),
            duration_ms=trace.get("duration_ms"),
            save_output=self.settings_store.get_bool("tools.record_outputs", True),
        )
        return record_id
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_tool_call_audit.py tests/test_trace_turn.py tests/test_loop.py tests/test_tool_event_payload.py tests/test_tool_state.py -q`
Expected: PASS（审计行行为不变；`TOOL_END` 多一个字段）

---

### Task 3: 读取接口（历史附带预览 + 按 id 取全文）

**Files:**
- Modify: `backend/src/agent/services/app.py:1294`（`session_messages_page` 附带记录）
- Modify: `backend/src/agent/api/server.py`（两个历史接口 + 新接口）
- Test: `backend/tests/test_tool_record_api.py`

**Interfaces:**
- Consumes: `previews_for_turns`、`get_record`（Task 1）
- Produces: `page["tool_records"]`；`GET /api/tool-records/{record_id}`

- [x] **Step 1: 写失败测试**

```python
def test_history_page_carries_tool_records_in_order(ctx):
    topic = ctx.topics.nodes.create_topic("工具历史话题").id
    ctx.memory.append_message(topic_id=topic, role="user", content="帮我查", turn_id="turn_t1")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="查完了", turn_id="turn_t1")
    record_tool_call(
        ctx.conn, turn_id="turn_t1", topic_id=topic, call_id="c1", seq=1,
        tool_name="fs_list", arguments={"path": "."}, output="很早的输出",
        status="failed", error="列目录失败：找不到路径", duration_ms=7,
    )

    page = ctx.session_messages_page(topic, limit=50)

    assert [m["role"] for m in page["messages"]] == ["user", "assistant"]
    records = page["tool_records"]
    assert len(records) == 1
    assert records[0]["call_id"] == "c1"
    assert records[0]["status"] == "failed"
    assert records[0]["error"] == "列目录失败：找不到路径"
    assert records[0]["title"]                    # 中文展示名现算
    assert "output" not in records[0]             # 预览里没有全文
    assert records[0]["output_chars"] == len("很早的输出")


def test_history_page_without_messages_has_no_records(ctx):
    topic = ctx.topics.nodes.create_topic("空话题").id
    assert ctx.session_messages_page(topic, limit=50)["tool_records"] == []


def test_full_record_endpoint_returns_arguments_and_output(client, db_conn):
    record_id = record_tool_call(
        db_conn, turn_id="turn_api", call_id="c9", seq=1, tool_name="fs_read",
        arguments={"path": "C:/x.txt"}, output="文件正文", status="success",
    )
    body = client.get(f"/api/tool-records/{record_id}").json()
    assert body["arguments"] == {"path": "C:/x.txt"}
    assert body["output"] == "文件正文"


def test_full_record_endpoint_404(client):
    assert client.get("/api/tool-records/tr_missing").status_code == 404


def test_session_context_carries_tool_records(client, ctx, db_conn):
    """进入对话（/api/session/context）同样要带上记录。"""
    topic = ctx.topics.nodes.create_topic("上下文话题").id
    ctx.memory.append_message(topic_id=topic, role="user", content="问", turn_id="turn_ctx")
    record_tool_call(
        db_conn, turn_id="turn_ctx", call_id="c_ctx", seq=1, tool_name="now",
        arguments={}, output="2026-09-24", status="success",
    )
    ctx.navigation.anchors.set_active(topic_id=topic)   # 当前话题 = 这一条
    body = client.get("/api/session/context").json()
    assert [r["call_id"] for r in body["tool_records"]] == ["c_ctx"]
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_tool_record_api.py -q`
Expected: FAIL（`KeyError: 'tool_records'`）

- [x] **Step 3: 实现**

`app.py::session_messages_page` 返回前加：

```python
        from agent.storage.tool_records import previews_for_turns

        # 工具记录按 turn_id 归属：工具换话题时整轮会跟着走，用话题过滤在那个瞬间不可靠；
        # 按时间窗口切又会把分页边界上的调用切丢。前端按记录 id 去重，跨页最多重复一次。
        turn_ids = sorted({str(m.get("turn_id") or "") for m in page} - {""})
        tool_records = previews_for_turns(self.conn, turn_ids)
        return {
            "messages": page,
            "tool_records": tool_records,
            "has_more": has_more,
            "next_before": next_before,
        }
```

`server.py`：`/api/session/context` 与 `/api/session/messages` 的返回体各加
`"tool_records": page["tool_records"]`，并新增：

```python
    @app.get("/api/tool-records/{record_id}")
    async def tool_record(record_id: str) -> dict:
        """按 id 取一条工具调用的全文（参数 + 输出）。"""
        from agent.storage.tool_records import get_record

        record = get_record(ctx.conn, record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="tool record not found")
        return record
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_tool_record_api.py tests/test_session_pagination.py tests/test_api_routes.py -q`
Expected: PASS

---

### Task 4: 设置项与清理时机

**Files:**
- Modify: `backend/src/agent/storage/tool_records.py`（两个常量）
- Modify: `backend/src/agent/services/app.py`（`prune_tool_outputs` + 启动时清理）
- Modify: `backend/src/agent/services/maintenance.py`（维护时清理）
- Modify: `backend/src/agent/api/server.py`（`GET/PUT /api/settings/tools`）
- Test: `backend/tests/test_settings_tools_api.py`

**Interfaces:**
- Produces: `AppContext.prune_tool_outputs() -> int`；
  `GET/PUT /api/settings/tools`（PUT 响应含 `purged`）

- [x] **Step 1: 写失败测试**

```python
def test_tool_history_settings_defaults(client):
    body = client.get("/api/settings/tools").json()
    assert body["record_outputs"] is True
    assert body["output_retention_days"] == 90


def test_tool_history_settings_roundtrip(client):
    body = client.put(
        "/api/settings/tools",
        json={"record_outputs": False, "output_retention_days": 7},
    ).json()
    assert body["record_outputs"] is False
    assert body["output_retention_days"] == 7
    assert client.get("/api/settings/tools").json()["output_retention_days"] == 7


def test_retention_days_rejects_non_number(client):
    assert client.put(
        "/api/settings/tools", json={"output_retention_days": "很久"}
    ).status_code == 400


def test_retention_days_clamps_out_of_range(client):
    body = client.put("/api/settings/tools", json={"output_retention_days": 99999}).json()
    assert body["output_retention_days"] == 3650
    assert client.put(
        "/api/settings/tools", json={"output_retention_days": -5}
    ).json()["output_retention_days"] == 0


def test_saving_settings_purges_expired_outputs_immediately(client, db_conn):
    old = record_tool_call(
        db_conn, turn_id="t_old", call_id="c_old", seq=1, tool_name="fs_list",
        arguments={}, output="很久以前的输出", status="success",
    )
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,)
    )
    body = client.put("/api/settings/tools", json={"output_retention_days": 30}).json()
    assert body["purged"] == 1
    assert get_record(db_conn, old)["output"] == ""


def test_maintenance_prunes_tool_outputs(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(ctx, "prune_tool_outputs", lambda: calls.append(1) or 0)
    asyncio.run(ctx.maintenance.run_once())
    assert calls
```

（`client` fixture 与 `ctx` fixture 参照 `tests/test_runtime_state.py`：`create_app(settings, db_conn)` + `TestClient`。）

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_settings_tools_api.py -q`
Expected: FAIL（404 / `AttributeError: prune_tool_outputs`）

- [x] **Step 3: 实现**

`storage/tool_records.py` 加常量：

```python
DEFAULT_RETENTION_DAYS = 90
MAX_RETENTION_DAYS = 3650
```

`app.py`（放在 `_record_tool_call` 附近）：

```python
    def prune_tool_outputs(self) -> int:
        """清掉超过保留期的输出全文（记录保留）。启动、维护与保存设置时都会调。"""
        from agent.storage.tool_records import DEFAULT_RETENTION_DAYS, prune_outputs

        days = self.settings_store.get_int(
            "tools.output_retention_days", DEFAULT_RETENTION_DAYS
        )
        try:
            return prune_outputs(self.conn, days)
        except Exception as exc:  # noqa: BLE001 - 清理失败不阻塞启动与维护
            logger.warning("tool output prune failed: %s", exc)
            return 0
```

`AppContext.__init__` 末尾（数据库与设置存储就绪之后）加：

```python
        # 启动清理一次：把天数调小之后重启也立刻生效；失败只记日志
        self.prune_tool_outputs()
```

`maintenance.run_once()` 里在原有工作之后加一行 `pruned = self.ctx.prune_tool_outputs()`，
并把 `"tool_outputs_purged": pruned` 放进它返回的字典。

`server.py` 新增（放在 `/api/settings/computer` 之后）：

```python
    @app.get("/api/settings/tools")
    async def get_tool_history_settings() -> dict:
        from agent.storage.tool_records import DEFAULT_RETENTION_DAYS

        store = ctx.settings_store
        days = store.get_int("tools.output_retention_days", DEFAULT_RETENTION_DAYS)
        return {
            "record_outputs": store.get_bool("tools.record_outputs", True),
            "output_retention_days": max(0, days),
        }

    @app.put("/api/settings/tools")
    async def update_tool_history_settings(body: dict) -> dict:
        from agent.storage.tool_records import MAX_RETENTION_DAYS

        store = ctx.settings_store
        if "record_outputs" in body:
            store.set("tools.record_outputs", "1" if body.get("record_outputs") else "0")
        if "output_retention_days" in body:
            try:
                days = int(body["output_retention_days"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="invalid output_retention_days")
            store.set(
                "tools.output_retention_days", str(max(0, min(days, MAX_RETENTION_DAYS)))
            )
        # 保存即生效：把天数调小要马上清掉过期输出
        payload = await get_tool_history_settings()
        payload["purged"] = ctx.prune_tool_outputs()
        return payload
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_settings_tools_api.py tests/test_m12_maintenance.py tests/test_settings_ui_api.py tests/test_maintenance*.py -q`
Expected: PASS

---

### Task 5: 前端（历史卡片 + 懒取全文 + 设置界面）

**Files:**
- Modify: `frontend/src/services/api.ts`、`frontend/src/stores/session.ts`、
  `frontend/src/stores/events.ts`、`frontend/src/components/MessageItem.vue`、
  `frontend/src/views/SettingsView.vue`
- Test: `frontend/src/stores/__tests__/toolRecordHistory.test.ts`、
  `frontend/src/components/__tests__/MessageItem.test.ts`（追加）

**Interfaces:**
- Consumes: `GET /api/tool-records/{id}`、`GET/PUT /api/settings/tools`、`TOOL_END.record_id`
- Produces: `StreamMessage` 新增 `toolRecordId / toolArgs / toolTruncated /
  toolOutputMissing / toolMissingReason / toolRecordLoaded / toolRecordLoading / toolRecordError`；
  store 动作 `loadToolRecord(messageId)`

- [x] **Step 1: 写失败测试**

```ts
it("历史加载把工具记录插在对应轮次里，并按时间排序", async () => {
  const session = useSessionStore();
  api.getSessionContext = vi.fn(async () => ({
    topic_id: "t1",
    messages: [
      { id: "m1", role: "user", content: "帮我查", content_type: "text", created_at: "2026-09-24T10:00:00+00:00" },
      { id: "m2", role: "assistant", content: "查完了", content_type: "text", created_at: "2026-09-24T10:00:05+00:00" },
    ],
    tool_records: [
      { id: "tr1", turn_id: "turn_t1", call_id: "c1", seq: 1, tool_name: "fs_list",
        title: "列目录", status: "failed", error: "找不到路径", duration_ms: 7,
        truncated: false, output_missing: false, missing_reason: "", output_chars: 20,
        preview: "列目录失败：找不到路径", created_at: "2026-09-24T10:00:03+00:00" },
    ],
    has_more: false,
    next_before: null,
  }));
  await session.loadHistory();
  expect(session.messages.map((m) => m.role)).toEqual(["user", "tool", "assistant"]);
  const card = session.messages[1];
  expect(card.toolRecordId).toBe("tr1");
  expect(card.toolStatus).toBe("failed");
  expect(card.toolError).toBe("找不到路径");
});

it("展开历史卡片时才取全文，并把参数与输出写回这条消息", async () => {
  const session = useSessionStore();
  session.messages = [/* 上面那张卡 */];
  api.getToolRecord = vi.fn(async () => ({
    id: "tr1", arguments: { path: "." }, output: "完整输出", output_missing: false,
    missing_reason: "", truncated: false,
  }));
  await session.loadToolRecord(cardId);
  expect(api.getToolRecord).toHaveBeenCalledWith("tr1");
  expect(session.messages[0].content).toBe("完整输出");
  expect(session.messages[0].toolArgs).toContain("path");
  expect(session.messages[0].toolRecordLoaded).toBe(true);
});

it("取全文失败时给出原因并可重试", async () => {
  api.getToolRecord = vi.fn(async () => { throw new Error("网络断了"); });
  await session.loadToolRecord(cardId);
  expect(session.messages[0].toolRecordError).toContain("网络断了");
  expect(session.messages[0].toolRecordLoaded).toBe(false);
});
```

`MessageItem.test.ts` 追加：展开时触发 `loadToolRecord`；输出已清理时显示
「输出已按保留设置清理」；截断时显示「已截断」。

- [x] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/stores/__tests__/toolRecordHistory.test.ts`
Expected: FAIL（`session.loadToolRecord is not a function` / 历史里没有工具条目）

- [x] **Step 3: 实现**

`services/api.ts`：加三个调用（`getToolRecord(id)`、`getToolHistorySettings()`、
`updateToolHistorySettings(body)`），类型 `ToolRecordPreview` / `ToolRecordFull`。

`stores/session.ts`：

- `StreamMessage` 加上述字段；新增 `_historyToolRecord(record)` 映射
  （`role: "tool"`、`toolRecordId`、`toolName`、`presentation: { title }`、
  `toolStatus`、`toolError`、`toolDurationMs`、`toolTruncated`、`toolOutputMissing`、
  `toolMissingReason`、`content: preview`）。
- `loadHistory()` 与更早一页加载：把消息与记录合并、按 `created_at` 排序、按
  `toolRecordId` 去重（跨页边界）。
- 新增 `loadToolRecord(messageId)`：取全文 → 写回该消息（`content=output`、
  `toolArgs=格式化 JSON`、截断与缺失标记）；失败写 `toolRecordError`。

`stores/events.ts`：`TOOL_END` 把 `record_id` 存到卡片的 `toolRecordId`。

`MessageItem.vue`：

- 展开详情分两段：`v-if="toolArgsText"` 的「参数」+ 一直存在的「输出」。
- `open` 变为展开且 `toolRecordId && !toolRecordLoaded && !toolOutputMissing` 时调
  `session.loadToolRecord(message.id)`；`toolRecordLoading` 显示「正在读取…」，
  `toolRecordError` 显示原因 + 「重试」按钮。
- `toolOutputMissing` 时按原因显示两句话（未保存 / 已清理），且**保留已有预览内容**
  （实时卡片本来就有 200 字预览，不能因为"没存全文"把它藏起来）。
- `toolTruncated` 时显示「已截断」徽标。

`SettingsView.vue`：工具与权限页新增「工具调用历史」一节（布尔开关的写法与同页既有
开关一致）：保存输出全文 + 输出保留天数（数字输入 0–3650，0 = 永久），
保存后显示是否清理了旧输出。

- [x] **Step 4: 跑测试确认通过**

Run: `cd frontend; npx vue-tsc --noEmit; npx vitest run src/stores/__tests__/toolRecordHistory.test.ts src/components/__tests__/MessageItem.test.ts src/views/__tests__/SettingsView.test.ts`
Expected: PASS

---

### Task 6: 文档、视觉检查与全量验证

**Files:**
- Modify: `docs/status.md`
- Modify: `scripts/ui-catalog/tool-fail-line.mjs`（或新增一个同风格脚本）

- [x] **Step 1: 视觉检查（隔离实例）**

```powershell
python scripts/ui-catalog/instance.py up --name toolhist --backend-port 8843 --frontend-port 6208 --clean
$env:QIO_BASE="http://127.0.0.1:6208"; $env:QIO_API="http://127.0.0.1:8843"
node scripts/ui-catalog/tool-history.mjs      # 注入一组历史工具记录 → 展开 → 截图 + 断言不溢出
python scripts/ui-catalog/instance.py down --name toolhist
```

断言：展开后参数与输出都在；长输出换行、卡片横向不溢出；窄窗口（820px）可读。

- [x] **Step 2: 全量验证**

```powershell
cd backend; uv run --frozen pytest
uv run --frozen python -m agent.eval.run
cd ..; python scripts/check_docs.py
cd frontend; npx vue-tsc --noEmit; npm test
```

- [x] **Step 3: 文档**

`docs/status.md` 追加本轮小节：新表与迁移 20、两个设置项、读取路径、渲染方式、
以及【已知限制】里把原先"工具卡不落库"的条目改成"原始输出受打码与保留期约束；
本功能上线前的历史没有记录"。
