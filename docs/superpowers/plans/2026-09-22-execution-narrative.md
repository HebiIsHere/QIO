# Execution Narrative Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让模型自主决定工具执行过程要不要说、说什么，并让审批带上模型生成的 explanation，同时保证真实操作事实与安全边界不被模型文案影响。

**Architecture:** 模型在工具调用参数里携带可选保留字段 `_qio`；adapter 解析时剥离成 `ToolCall.narrative`；`AgentLoop` 每批调用最多取一条叙事，经注入的 sink 落库（`messages` 行，`content_type='narrative'`）后广播 `NARRATIVE`；批次结束时由系统把真实调用结果补写进同一条记录的 `raw.calls`。前端把叙事行渲染成默认收起的抽屉头，历史里用 `raw.calls` 作为抽屉内容。审批 explanation 复用同一个 `Narrative`，通过 ContextVar 在审批请求构造时合并进 payload 的 `explanation` 字段。

**Tech Stack:** Python 3.10+/FastAPI/SQLite（后端，pytest + uv）、Vue 3 + Pinia + TypeScript（前端，vitest + vue-tsc）。

**Spec:** `docs/superpowers/specs/2026-09-22-execution-narrative-design.md`

## Global Constraints

* 不修改工具执行语义、风险判断、沙箱判定、审批权限与单次使用约束。
* 改 schema 只能追加新迁移；本计划不新增迁移（复用 `messages` 表）。
* 任何落库/广播/日志/测试输出都不得出现密钥原文；新增输出路径必须过 `agent/trace/redact.py`。
* 前端颜色一律用 `var(--*)`，禁止硬编码色值。
* 叙事文本不参与任何分支判断；`silent` 只表示"不输出说明"。
* 后端测试命令：`cd backend; uv run --frozen pytest`；前端：`npx vue-tsc --noEmit` + `npm test`。
* 每个任务结束都要提交（commit）。

---

### Task 1: 叙事结构与解析

**Files:**
- Create: `backend/src/agent/core/narrative.py`
- Test: `backend/tests/test_execution_narrative.py`

**Interfaces:**
- Consumes: `agent.trace.redact.redact_text`
- Produces: `NARRATIVE_KEY = "_qio"`、`NARRATIVE_KINDS`、`Narrative(kind, text, explanation, silent)`、`parse_narrative(raw) -> Narrative | None`、`split_narrative_arguments(arguments) -> tuple[dict, dict | None]`、`narrative_event_payload(narrative_id, turn_id, narrative, *, tool, call_id, call_ids, created_at) -> dict`

- [ ] **Step 1: Write the failing test**

```python
"""Execution Narrative：模型文案与工具事实分离。"""
from __future__ import annotations

from agent.core.narrative import (
    NARRATIVE_KEY,
    Narrative,
    narrative_event_payload,
    parse_narrative,
    split_narrative_arguments,
)


def test_parse_narrative_accepts_known_kinds():
    out = parse_narrative({"kind": "announce", "text": "  我先确认审批链路。  "})
    assert out == Narrative(kind="announce", text="我先确认审批链路。")


def test_parse_narrative_rejects_unknown_kind_and_empty_text():
    assert parse_narrative({"kind": "speak", "text": "x"}) is None
    assert parse_narrative({"kind": "announce", "text": "   "}) is None
    assert parse_narrative("not-a-dict") is None
    assert parse_narrative(None) is None


def test_parse_narrative_keeps_explanation_only():
    out = parse_narrative({"explanation": "为了写入叙事记录，需要新增一个模块文件。"})
    assert out is not None
    assert out.silent is True
    assert out.text == ""
    assert out.explanation.startswith("为了写入叙事记录")


def test_parse_narrative_drops_unknown_keys_and_caps_length():
    out = parse_narrative(
        {
            "kind": "progress",
            "text": "甲" * 400,
            "explanation": "乙" * 500,
            "risk": "danger",
            "capabilities": ["写入文件：是"],
            "text_override": "骗你的",
        }
    )
    assert out is not None
    assert len(out.text) <= 120
    assert len(out.explanation) <= 200
    assert not hasattr(out, "risk")


def test_parse_narrative_redacts_secret_shaped_text():
    out = parse_narrative({"kind": "warning", "text": "api_key=sk-abcdef123456"})
    assert out is not None
    assert "sk-abcdef123456" not in out.text


def test_split_narrative_arguments_removes_reserved_key():
    clean, raw = split_narrative_arguments(
        {"path": "a.txt", NARRATIVE_KEY: {"kind": "announce", "text": "先看文件"}}
    )
    assert clean == {"path": "a.txt"}
    assert raw == {"kind": "announce", "text": "先看文件"}
    assert split_narrative_arguments({"path": "a.txt"}) == ({"path": "a.txt"}, None)


def test_narrative_event_payload_carries_system_provenance():
    payload = narrative_event_payload(
        "msg_1",
        "turn_1",
        Narrative(kind="announce", text="先看审批链路"),
        tool="grep_search",
        call_id="call_a",
        call_ids=["call_a", "call_b"],
        created_at="2026-09-22T09:41:09+00:00",
    )
    assert payload["narrative_id"] == "msg_1"
    assert payload["tool"] == "grep_search"
    assert payload["call_ids"] == ["call_a", "call_b"]
    assert payload["text"] == "先看审批链路"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'agent.core.narrative'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Execution Narrative：模型自主决定的过程说明。

这里只定义「模型说了什么」与「怎么安全地把它交给前端」；工具事实、风险与权限
不经过本模块，也不受本模块影响。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.trace.redact import redact_text

NARRATIVE_KEY = "_qio"
NARRATIVE_KINDS = ("announce", "progress", "warning", "result")
MAX_TEXT_CHARS = 120
MAX_EXPLANATION_CHARS = 200


@dataclass(frozen=True)
class Narrative:
    kind: str
    text: str
    explanation: str = ""
    silent: bool = False


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = redact_text(value).strip()
    return text[:limit]


def parse_narrative(raw: object) -> Narrative | None:
    """白名单解析模型给的 `_qio`；任何不合法输入都返回 None（= 安静）。"""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    text = _clean(raw.get("text"), MAX_TEXT_CHARS)
    explanation = _clean(raw.get("explanation"), MAX_EXPLANATION_CHARS)
    if kind not in NARRATIVE_KINDS:
        # 只带 explanation 时 kind 缺省为 progress
        if not text and explanation:
            kind = "progress"
        else:
            return None
    if not text and not explanation:
        return None
    return Narrative(kind=str(kind), text=text, explanation=explanation, silent=not text)


def split_narrative_arguments(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
    """把保留字段从工具参数里剥出来：工具与风险判断永远看不到它。"""
    clean = {k: v for k, v in dict(arguments or {}).items() if k != NARRATIVE_KEY}
    raw = (arguments or {}).get(NARRATIVE_KEY)
    return clean, raw if isinstance(raw, dict) else None


def narrative_event_payload(
    narrative_id: str,
    turn_id: str | None,
    narrative: Narrative,
    *,
    tool: str | None = None,
    call_id: str | None = None,
    call_ids: list[str] | None = None,
    created_at: str | None = None,
) -> dict:
    return {
        "narrative_id": narrative_id,
        "turn_id": turn_id,
        "kind": narrative.kind,
        "text": narrative.text,
        "tool": tool,
        "call_id": call_id,
        "call_ids": list(call_ids or []),
        "created_at": created_at,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/core/narrative.py backend/tests/test_execution_narrative.py
git commit -m "feat(narrative): 叙事结构与白名单解析"
```

---

### Task 2: 适配器把 `_qio` 解析成 ToolCall.narrative

**Files:**
- Modify: `backend/src/agent/adapters/base.py`（`ToolCall` 增加 `narrative` 字段）
- Modify: `backend/src/agent/adapters/native.py`、`backend/src/agent/adapters/text.py`
- Modify: `backend/src/agent/prompts.py`（`_qio` 字段描述 + text 档协议补一句）
- Test: `backend/tests/test_execution_narrative.py`

**Interfaces:**
- Consumes: Task 1 的 `NARRATIVE_KEY`、`split_narrative_arguments`
- Produces: `ToolCall(id, name, arguments, narrative=None)`；`ToolRegistry.specs()` 里每个工具 parameters 含 `_qio` 属性

- [ ] **Step 1: Write the failing test**

```python
def test_native_adapter_strips_narrative_from_arguments():
    from agent.adapters.native import NativeAdapter

    class _Fn:
        name = "fs_read"
        arguments = '{"path": "a.txt", "_qio": {"kind": "announce", "text": "先读它"}}'

    class _Call:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = None
        tool_calls = [_Call()]

    class _Choice:
        message = _Msg()
        finish_reason = "tool_calls"

    class _Raw:
        choices = [_Choice()]
        usage = None

    completion = NativeAdapter(client=None, model="m")._to_completion(_Raw())
    call = completion.tool_calls[0]
    assert call.arguments == {"path": "a.txt"}
    assert call.narrative == {"kind": "announce", "text": "先读它"}


def test_text_adapter_parses_narrative_from_json_block():
    from agent.adapters.text import TextAdapter

    adapter = TextAdapter(client=None, model="m")
    parsed = adapter._parse(
        '```json\n{"tool_calls": [{"name": "fs_read", "arguments": '
        '{"path": "a.txt", "_qio": {"kind": "progress", "text": "继续核对"}}}]}\n```'
    )
    assert parsed is not None
    raw = parsed["tool_calls"][0]["arguments"]
    clean, narrative = split_narrative_arguments(raw)
    assert clean == {"path": "a.txt"}
    assert narrative["kind"] == "progress"


def test_registry_specs_declare_narrative_field():
    from agent.tools.base import Tool, ToolResult
    from agent.tools.registry import ToolRegistry

    class _Echo(Tool):
        name = "echo"
        description = "echo"
        parameters = {"type": "object", "properties": {"q": {"type": "string"}}}

        async def run(self, **kwargs):
            return ToolResult(ok=True, content="ok")

    reg = ToolRegistry()
    reg.register(_Echo())
    spec = reg.specs()[0]
    assert NARRATIVE_KEY in spec.parameters["properties"]
    assert "required" not in spec.parameters or NARRATIVE_KEY not in spec.parameters["required"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py -v`
Expected: FAIL —— `TypeError: ToolCall.__init__() got an unexpected keyword argument 'narrative'`（或 `_qio` 仍在 arguments 里）

- [ ] **Step 3: Write minimal implementation**

`adapters/base.py`：

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    narrative: dict[str, Any] | None = field(default=None, compare=False)
```

`adapters/native.py::_to_completion`：

```python
from agent.core.narrative import split_narrative_arguments

arguments = parse_arguments(raw_arguments)
arguments, narrative = split_narrative_arguments(arguments)
tool_calls.append(
    ToolCall(id=tc.id, name=tc.function.name, arguments=arguments, narrative=narrative)
)
```

`adapters/text.py::complete` 构造 ToolCall 时：

```python
clean, narrative = split_narrative_arguments(dict(item.get("arguments") or {}))
ToolCall(id=..., name=item["name"], arguments=clean, narrative=narrative)
```

`tools/registry.py::specs()`：

```python
from agent.core.narrative import NARRATIVE_KEY
from agent.prompts import NARRATIVE_FIELD_DESCRIPTION

def _with_narrative_field(parameters: dict[str, Any]) -> dict[str, Any]:
    spec = dict(parameters or {})
    properties = dict(spec.get("properties") or {})
    properties.setdefault(
        NARRATIVE_KEY,
        {
            "type": "object",
            "description": NARRATIVE_FIELD_DESCRIPTION,
            "properties": {
                "kind": {"type": "string", "enum": list(NARRATIVE_KINDS)},
                "text": {"type": "string"},
                "explanation": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )
    spec["properties"] = properties
    spec.setdefault("type", "object")
    return spec
```

`prompts.py` 新增：

```python
# 工具调用的可选叙事信封字段说明。定义于 tools/registry.py _with_narrative_field；
# 作用：告诉模型可以用一句话说明这次调用想让用户知道什么，不写就是保持安静。
NARRATIVE_FIELD_DESCRIPTION = (
    "可选。用一句中文说明这次调用想让用户知道的意图（kind: "
    "announce/progress/warning/result）。不要复述工具名或参数；"
    "不说明就省略整个字段（保持安静）。explanation 可选：若这次调用可能触发用户确认，"
    "说明为什么需要执行、准备做什么、可能影响什么。"
)
```

并在 `SYSTEM_PROMPT_TEXT_MODE` 末尾补一句：`"调用工具时可用 arguments._qio 说明意图；不需要说明就省略。"`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py tests/test_tool_schema_present.py tests/test_tool_registry_reversible.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/adapters backend/src/agent/prompts.py backend/src/agent/tools/registry.py backend/tests/test_execution_narrative.py
git commit -m "feat(narrative): 工具调用携带叙事信封并在 adapter 剥离"
```

---

### Task 3: 审批 explanation 接入（ContextVar + payload 合并）

**Files:**
- Modify: `backend/src/agent/tools/registry.py`（设置/复位当前叙事）
- Modify: `backend/src/agent/tools/approval.py`（合并 explanation）
- Test: `backend/tests/test_execution_narrative.py`

**Interfaces:**
- Consumes: Task 1 `parse_narrative`、Task 2 `ToolCall.narrative`
- Produces: `agent.tools.registry.current_narrative()`（ContextVar 读取）；`ApprovalService.request` 只在 `payload["explanation"]` 为空时写入模型 explanation

- [ ] **Step 1: Write the failing test**

```python
async def test_approval_payload_gets_model_explanation():
    from agent.adapters.base import ToolCall
    from agent.tools.approval import ApprovalResult
    from agent.tools.base import Tool, ToolResult
    from agent.tools.registry import ToolRegistry

    class _Approvals:
        def __init__(self):
            self.requests = []

        async def request(self, kind, payload, **kwargs):
            self.requests.append((kind, payload))
            return ApprovalResult("appr_1", "approved")

    class _Write(Tool):
        name = "fs_write"
        description = "write"
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
        requires_approval = True

        async def run(self, **kwargs):
            return ToolResult(ok=True, content="written")

    approvals = _Approvals()
    reg = ToolRegistry(approvals=approvals)
    reg.register(_Write())
    await reg.execute(
        ToolCall(
            id="c1",
            name="fs_write",
            arguments={"path": "a.txt"},
            narrative={"kind": "announce", "text": "写入叙事模块", "explanation": "为了让过程说明可恢复。"},
        )
    )
    kind, payload = approvals.requests[0]
    assert kind == "tool_execution"
    assert payload["explanation"] == "为了让过程说明可恢复。"
    assert payload["description"] == "想修改当前项目中的一个文件"
    assert payload["arguments"] == {"path": "a.txt"}


async def test_approval_keeps_existing_explanation():
    ...  # 用一个自带 explanation 的 payload 直接调用 ApprovalService.request，断言不被覆盖


async def test_approval_survives_without_narrative():
    ...  # 不带 narrative 的调用：payload["explanation"] == ""，审批照常发生
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py -k approval -v`
Expected: FAIL —— `payload["explanation"]` 为空字符串

- [ ] **Step 3: Write minimal implementation**

`tools/registry.py`：

```python
from contextvars import ContextVar
from agent.core.narrative import parse_narrative

_current_narrative: ContextVar[Narrative | None] = ContextVar("qio_current_narrative", default=None)

def current_narrative() -> Narrative | None:
    return _current_narrative.get()

async def execute(self, call: ToolCall) -> ToolResult:
    narrative = parse_narrative(getattr(call, "narrative", None))
    token = _current_narrative.set(narrative)
    try:
        ...  # 现有管线原样，只把 body 包进 try
    finally:
        _current_narrative.reset(token)
```

`tools/approval.py::request`：

```python
from agent.core.narrative import Narrative
from agent.tools.registry import current_narrative  # 惰性导入以避免循环

payload = dict(payload)
if not str(payload.get("explanation") or "").strip():
    narrative = current_narrative()
    if narrative is not None and narrative.explanation:
        payload["explanation"] = narrative.explanation
```

- [ ] **Step 4: Run tests**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py tests/test_approval_present.py tests/test_approval_binding.py tests/test_approval_present.py -v`
Expected: PASS（含既有审批测试）

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/tools/registry.py backend/src/agent/tools/approval.py backend/tests/test_execution_narrative.py
git commit -m "feat(narrative): 审批携带模型 explanation（事实字段不变）"
```

---

### Task 4: NARRATIVE 事件类型与总线分类

**Files:**
- Modify: `backend/src/agent/api/events.py`、`backend/src/agent/api/bus.py`
- Test: `backend/tests/test_execution_narrative.py`

- [ ] **Step 1: Write the failing test**

```python
def test_narrative_event_is_registered_and_critical():
    from agent.api.bus import CRITICAL_EVENTS
    from agent.api.events import EventType, make_event, sse_format

    assert EventType.NARRATIVE.value == "NARRATIVE"
    assert EventType.NARRATIVE in CRITICAL_EVENTS
    text = sse_format(make_event(EventType.NARRATIVE, {"text": "先确认链路"}))
    assert "event: NARRATIVE" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py -k event -v`
Expected: FAIL —— `AttributeError: NARRATIVE`

- [ ] **Step 3: Add the event type and classification**

`api/events.py`：在 `EventType` 里新增 `NARRATIVE = "NARRATIVE"`，并加注释说明它是"模型过程说明，不是工具事实"。
`api/bus.py`：把 `EventType.NARRATIVE` 加进 `CRITICAL_EVENTS`。

- [ ] **Step 4: Run tests**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py tests/test_events.py tests/test_events_backpressure.py tests/test_bus_replay_policy.py -v`
Expected: PASS（含分类完备守卫）

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/api/events.py backend/src/agent/api/bus.py backend/tests/test_execution_narrative.py
git commit -m "feat(narrative): 新增 NARRATIVE 事件并归入关键事件"
```

---

### Task 5: AgentLoop 每批一条叙事 + 落库/结算钩子

**Files:**
- Modify: `backend/src/agent/core/loop.py`
- Test: `backend/tests/test_execution_narrative.py`

**Interfaces:**
- Consumes: Task 1 `parse_narrative`；Task 2 `ToolCall.narrative`
- Produces: `AgentLoop(..., narrative_sink=None, narrative_settler=None)`；`narrative_sink(turn_id, narrative, call, call_ids) -> str | None` 返回 `narrative_id`；`narrative_settler(narrative_id, results, calls) -> None`

- [ ] **Step 1: Write the failing test**

```python
async def test_loop_emits_one_narrative_before_tool_batch():
    ...
    order: list[str] = []
    seen: list[dict] = []

    async def sink(turn_id, narrative, call, call_ids):
        order.append("NARRATIVE")
        seen.append({"kind": narrative.kind, "text": narrative.text, "call_ids": call_ids})
        return "msg_1"

    loop = AgentLoop(_ScriptedAdapter(), registry, EventBus(), turn_id="turn_1",
                     narrative_sink=sink)
    ...  # 消费 bus，执行 loop.run
    assert order == ["NARRATIVE"]
    assert seen[0]["call_ids"] == ["c1", "c2", "c3"]
    assert events.index("NARRATIVE") < events.index("TOOL_START")


async def test_loop_silent_when_no_narrative():
    ...  # 三个调用都不带 _qio → sink 一次都没被调用


async def test_loop_settles_narrative_with_real_results():
    ...  # narrative_settler 收到 narrative_id 与这一批的真实 ToolResult 列表
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py -k loop -v`
Expected: FAIL —— `AgentLoop.__init__() got an unexpected keyword argument 'narrative_sink'`

- [ ] **Step 3: Implement**

```python
def __init__(self, ..., narrative_sink=None, narrative_settler=None):
    self.narrative_sink = narrative_sink
    self.narrative_settler = narrative_settler

async def _emit_batch_narrative(self, calls) -> str | None:
    if self.narrative_sink is None:
        return None
    for call in calls:
        narrative = parse_narrative(getattr(call, "narrative", None))
        if narrative is None:
            continue
        try:
            return await self.narrative_sink(
                self.turn_id, narrative, call, [c.id for c in calls]
            )
        except Exception:  # noqa: BLE001 - 叙事失败不得影响工具执行
            logger.warning("narrative emission failed", exc_info=True)
            return None
    return None

async def _dispatch_tool_calls(self, calls):
    for c in calls:
        self._dispatched_call_ids.add(c.id)
    narrative_id = await self._emit_batch_narrative(calls)
    ...  # 现有并发/串行执行逻辑
    if narrative_id and self.narrative_settler is not None:
        try:
            await self.narrative_settler(narrative_id, results, calls)
        except Exception:  # noqa: BLE001 - 结算失败不影响结果
            logger.warning("narrative settle failed", exc_info=True)
    return results
```

- [ ] **Step 4: Run tests**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py tests/test_tool_event_isolation.py tests/test_tool_parallel_cancel.py tests/test_loop.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/core/loop.py backend/tests/test_execution_narrative.py
git commit -m "feat(narrative): 循环每批最多一条叙事并在批次结束结算"
```

---

### Task 6: AppContext 落库、结算与快照

**Files:**
- Modify: `backend/src/agent/services/app.py`、`backend/src/agent/services/turn_orchestrator.py`、`backend/src/agent/memory/fragment.py`
- Test: `backend/tests/test_execution_narrative.py`

**Interfaces:**
- Consumes: Task 5 的 sink/settler 契约
- Produces: `AppContext._on_narrative(turn_id, narrative, call, call_ids) -> str | None`、`AppContext._settle_narrative(narrative_id, results, calls) -> None`、`AppContext.active_turn_narratives() -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
async def test_narrative_is_persisted_then_broadcast(app_ctx):
    ...  # 直接 await app_ctx._on_narrative(...)
    row = app_ctx.conn.execute(
        "SELECT role, content, content_type, raw FROM messages WHERE id = ?", (narrative_id,)
    ).fetchone()
    assert row["role"] == "assistant"
    assert row["content_type"] == "narrative"
    assert json.loads(row["raw"])["narrative"]["kind"] == "announce"
    assert payload["narrative_id"] == narrative_id


async def test_settle_narrative_writes_system_call_summary(app_ctx):
    ...  # 结算后 raw["calls"] 含 status / duration_ms / error，且模型文本不进入该字段


def test_narrative_rows_do_not_count_toward_fragment_capacity(db_conn):
    ...  # 同一片段插入叙事行后 content_tokens 不变
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py -k narrative -v`
Expected: FAIL —— `AttributeError: 'AppContext' object has no attribute '_on_narrative'`

- [ ] **Step 3: Implement**

`services/app.py`：

```python
async def _on_narrative(self, turn_id, narrative, call, call_ids) -> str | None:
    from agent.api.events import EventType, make_event
    from agent.core.narrative import narrative_event_payload

    binding = self.bindings.binding_for(turn_id)
    if binding is None:
        return None
    raw = {
        "narrative": {
            "kind": narrative.kind,
            "tool": call.name,
            "call_id": call.id,
            "silent": narrative.silent,
        },
        "calls": [],
    }
    message_id, _ = self.memory.append_message(
        topic_id=binding.topic_id,
        role="assistant",
        content=narrative.text,
        content_type="narrative",
        fragment_id=binding.fragment_id,
        turn_id=turn_id,
        raw=raw,
    )
    await self.bus.publish(
        make_event(
            EventType.NARRATIVE,
            narrative_event_payload(
                message_id, turn_id, narrative,
                tool=call.name, call_id=call.id, call_ids=call_ids,
            ),
        )
    )
    return message_id

async def _settle_narrative(self, narrative_id, results, calls) -> None:
    rows = self.conn.execute("SELECT raw FROM messages WHERE id = ?", (narrative_id,)).fetchone()
    if rows is None:
        return
    raw = json.loads(rows["raw"] or "{}")
    raw["calls"] = [
        {
            "call_id": call.id,
            "tool": call.name,
            "title": tool_label(call.name),
            "status": _terminal_tool_status(results.get(call.id)),
            "error": (results.get(call.id).error if results.get(call.id) else None),
            "duration_ms": self._tool_durations.get(call.id),
        }
        for call in calls
    ]
    self.conn.execute(
        "UPDATE messages SET raw = ? WHERE id = ?",
        (json.dumps(raw, ensure_ascii=False), narrative_id),
    )

def active_turn_narratives(self) -> list[dict]:
    ...  # 只查主 turn（复用 _is_main_turn_id），返回 narrative_id/kind/text/calls/created_at
```

`services/turn_orchestrator.py`：构造 `AgentLoop` 时加
`narrative_sink=app._on_narrative, narrative_settler=app._settle_narrative`。

`memory/fragment.py::content_tokens`：SQL 加 `AND content_type <> 'narrative'`。

- [ ] **Step 4: Run tests**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py tests/test_fragment_capacity.py tests/test_turn_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/services backend/src/agent/memory/fragment.py backend/tests/test_execution_narrative.py
git commit -m "feat(narrative): 叙事落库、结算与容量排除"
```

---

### Task 7: runtime state 与历史分页暴露叙事

**Files:**
- Modify: `backend/src/agent/api/server.py`、`backend/src/agent/services/app.py`
- Test: `backend/tests/test_execution_narrative.py`

- [ ] **Step 1: Write the failing test**

```python
def test_runtime_state_carries_active_turn_narratives(client, app_ctx):
    ...  # GET /api/runtime/state → state["narratives"] 里含刚落库的叙事


def test_session_context_returns_narrative_raw(client, app_ctx):
    ...  # GET /api/session/context → 叙事消息带 raw.narrative.kind
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py -k "runtime or session" -v`
Expected: FAIL —— `KeyError: 'narratives'`

- [ ] **Step 3: Implement**

* `/api/runtime/state` 返回体加 `"narratives": ctx.active_turn_narratives()`；
* `AppContext.session_messages_page` 的 SELECT 增加 `raw`（叙事行需要还原 kind 与 calls）；
* `fragment_messages` 同步返回 `raw`（星球原文视图一致）。

- [ ] **Step 4: Run tests**

Run: `cd backend; uv run --frozen pytest tests/test_execution_narrative.py tests/test_sse_replay.py tests/test_session_pagination.py tests/test_api_routes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/api/server.py backend/src/agent/services/app.py backend/tests/test_execution_narrative.py
git commit -m "feat(narrative): runtime state 与历史分页暴露叙事与调用摘要"
```

---

### Task 8: 前端事件与运行时状态接线

**Files:**
- Modify: `frontend/src/services/events.ts`、`frontend/src/services/api.ts`、`frontend/src/stores/events.ts`
- Test: `frontend/src/stores/__tests__/executionNarrative.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
it("NARRATIVE 事件按 narrative_id 去重", () => {
  const { events, session } = setup();
  const payload = { narrative_id: "msg_1", turn_id: "turn_1", kind: "announce", text: "先确认链路", call_ids: ["c1"] };
  events.dispatch({ type: "TURN_START", id: "e1", ts: "", data: { turn_id: "turn_1" } });
  events.dispatch({ type: "NARRATIVE", id: "e2", ts: "", data: payload });
  events.dispatch({ type: "NARRATIVE", id: "e3", ts: "", data: payload });
  expect(session.messages.filter((m) => m.role === "narrative")).toHaveLength(1);
});

it("runtime state 合并叙事不重复且按时间插入", () => {
  ...  // applyRuntimeState({ narratives: [...] }) 两次 → 仍只有一条
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend; npx vitest run src/stores/__tests__/executionNarrative.test.ts`
Expected: FAIL —— 未知事件类型 / `applyNarrative is not a function`

- [ ] **Step 3: Implement**

* `services/events.ts`：`EVENT_TYPES` 加 `"NARRATIVE"`；
* `services/api.ts`：`getSessionContext` 消息项加 `raw?: string`；runtime state 类型加 `narratives`；
* `stores/events.ts`：`case "NARRATIVE"` → `session.applyNarrative(...)`；`applyRuntimeState` 调用
  `session.mergeNarratives(state.narratives ?? [])`。

- [ ] **Step 4: Run tests**

Run: `cd frontend; npx vitest run src/stores && npx vue-tsc --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/services frontend/src/stores
git commit -m "feat(narrative): 前端接收 NARRATIVE 并合并运行时叙事"
```

---

### Task 9: session store 的叙事消息与分组

**Files:**
- Modify: `frontend/src/stores/session.ts`
- Test: `frontend/src/stores/__tests__/executionNarrative.test.ts`

**Interfaces:**
- Produces: `StreamMessage.role` 增加 `"narrative"`；`narrativeKind`、`narrativeCallIds`、`narrativeCalls`；
  `applyNarrative(payload)`、`mergeNarratives(list)`、`groupTurnItems(items)`（叙事行收纳后随的工具卡）

- [ ] **Step 1: Write the failing test**

```ts
it("叙事行收纳它之后、下一行叙事之前的工具卡", () => {
  const items = [
    { id: "n1", role: "narrative", narrativeCallIds: ["c1", "c2"] },
    { id: "t1", role: "tool", callId: "c1" },
    { id: "t2", role: "tool", callId: "c2" },
    { id: "n2", role: "narrative", narrativeCallIds: ["c3"] },
    { id: "t3", role: "tool", callId: "c3" },
  ];
  const groups = groupTurnItems(items);
  expect(groups.map((g) => g.kind)).toEqual(["stage", "stage"]);
  expect(groups[0].calls.map((c) => c.id)).toEqual(["t1", "t2"]);
  expect(groups[1].calls.map((c) => c.id)).toEqual(["t3"]);
});

it("轮次开头的无声调用不挂在任何叙事下", () => {
  const groups = groupTurnItems([{ id: "t1", role: "tool", callId: "c9" }, { id: "n1", role: "narrative" }]);
  expect(groups[0].kind).toBe("loose");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend; npx vitest run src/stores/__tests__/executionNarrative.test.ts`
Expected: FAIL —— `groupTurnItems is not a function`

- [ ] **Step 3: Implement**

```ts
export type NarrativeKind = "announce" | "progress" | "warning" | "result";
export interface NarrativeCallRecord {
  callId: string; tool: string; title?: string;
  status: "success" | "failed" | "cancelled"; error?: string | null; durationMs?: number;
}

export type TurnItemGroup =
  | { kind: "stage"; narrative: StreamMessage; calls: StreamMessage[] }
  | { kind: "loose"; items: StreamMessage[] };

export function groupTurnItems(items: StreamMessage[]): TurnItemGroup[] { ... }
```

`_historyMessage` 把 `content_type === "narrative"` 映射成 `role: "narrative"`，
并从 `raw` 解析 `kind` / `calls`；`applyNarrative` 按 `narrative_id` 去重，`mergeNarratives`
按 `Date.parse(created_at)` 插入到时间正确的位置。

- [ ] **Step 4: Run tests**

Run: `cd frontend; npx vitest run src/stores && npx vue-tsc --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/stores/session.ts frontend/src/stores/__tests__/executionNarrative.test.ts
git commit -m "feat(narrative): 叙事消息、抽屉分组与历史还原"
```

---

### Task 10: 消息流渲染抽屉 + 去掉机械提示

**Files:**
- Modify: `frontend/src/components/MessageStream.vue`、`frontend/src/components/MessageItem.vue`
- Test: `frontend/src/components/__tests__/ExecutionNarrativeDrawer.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
it("叙事抽屉默认收起，点击后展开", async () => {
  ...  // mount NarrativeStage（或 MessageStream）→ 断言 aria-expanded=false、抽屉高度 0fr
  await head.trigger("click");
  expect(head.attributes("aria-expanded")).toBe("true");
});

it("折叠头显示 1 运行中 / 1 失败", () => { ... });
it("消息流不再出现『正在使用工具』", () => { ... });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend; npx vitest run src/components/__tests__/ExecutionNarrativeDrawer.test.ts`
Expected: FAIL —— 找不到 `.qio-narrative` / 仍能匹配到「正在使用工具」

- [ ] **Step 3: Implement**

* 新增 `MessageStream` 内的分组渲染：`stage` 渲染 `<NarrativeStage>`（默认 `data-open="false"`），
  `loose` 按原样渲染；
* `MessageItem` 增加 `role === "narrative"` 分支（独立一行时使用）与历史 `raw.calls` 列表；
* `ACTIVITY_LABELS` 去掉 `tool: "正在使用工具"`，`showGlobalStatus` 排除 `activity === "tool"`；
* 抽屉样式用令牌：`--mo-2-in` / `--ease-2` / `--border-subtle`，减少动画时只保留短淡入。

- [ ] **Step 4: Run tests**

Run: `cd frontend; npx vitest run src/components && npx vue-tsc --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components
git commit -m "feat(narrative): 叙事抽屉默认收起，移除机械工具提示"
```

---

### Task 11: 审批窗口展示 QIO 说明

**Files:**
- Modify: `frontend/src/components/ApprovalModal.vue`
- Test: `frontend/src/components/__tests__/ApprovalModal.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
it("有 explanation 时单独成段显示，系统事实仍在", () => { ... });
it("没有 explanation 时与现状一致（不出现该段）", () => { ... });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend; npx vitest run src/components/__tests__/ApprovalModal.test.ts`
Expected: FAIL —— 找不到 "QIO 的说明"

- [ ] **Step 3: Implement**

`intent` 保持优先系统 `description`；新增 `qioExplanation`（取 `payload.explanation`）并在 facts 之前渲染；
`whyNeeded` 不再重复输出 explanation（explanation 为空时仍回退 `reason`）。

- [ ] **Step 4: Run tests**

Run: `cd frontend; npx vitest run src/components/__tests__/ApprovalModal.test.ts && npx vue-tsc --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ApprovalModal.vue frontend/src/components/__tests__/ApprovalModal.test.ts
git commit -m "feat(narrative): 审批窗口优先展示 QIO 说明并保留系统事实"
```

---

### Task 12: 文档同步与全量验证

**Files:**
- Modify: `docs/architecture.md`、`docs/status.md`

- [ ] **Step 1: 更新架构与进度文档**

`docs/architecture.md`：事件协议表加 `NARRATIVE`；恢复契约加 `runtime/state.narratives`；
`docs/status.md`：登记本功能与已知限制（工具卡仍不落库、历史抽屉用系统生成的调用摘要）。

- [ ] **Step 2: 全量验证**

Run: `cd backend; uv run --frozen pytest`
Expected: 全绿

Run: `cd frontend; npx vue-tsc --noEmit; npm test`
Expected: 全绿

Run: `python scripts/check_docs.py`
Expected: 通过

Run: `cd backend; uv run --frozen python -m agent.eval.run`（prompt / 工具定义变更的基线对比）
Expected: 与基线无回退

- [ ] **Step 3: 手工目视检查（改前端后必做）**

重启 uvicorn（后端无热加载），前端带 `?fresh=N` 打开，窄窗口与滚动下检查抽屉展开/收起、
代码块与工具卡不溢出。

- [ ] **Step 4: Commit**

```bash
git add docs
git commit -m "docs(narrative): 同步事件与恢复契约、进度与已知限制"
```
