# QIO 迭代预算与跑飞护栏 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 qio 迭代上限从 native 5 / text 3 调高到 128 / 64，token 闸改测输出 token 并可配置，预算耗尽时给「继续」，并加跑飞护栏。

**Architecture:** `agent/core/budget.py` 改默认值与输出-token 语义；新增 `agent/core/guard.py`（RunawayGuard）；`AgentLoop` 接护栏、耗尽时用 `ApprovalService` 挂起等用户「继续/停止」；配置走 `SettingsStore` + `GET/PUT /api/settings/loop`；前端加「对话深度」卡片与「继续/停止」操作条。

**Tech Stack:** Python (FastAPI, asyncio), Vue3 (TS), pytest, vitest。

**Spec:** [2026-09-10-iteration-budget-design.md](/C:/Users/zxy/Documents/Front agent/qio/docs/superpowers/specs/2026-09-10-iteration-budget-design.md)

## Global Constraints

- 后端遵循现有模式；`IterationBudget` 变更保持向后兼容。
- 复用现有 `ApprovalService`（`request(kind, payload)` 返回 `ApprovalResult(decision)`；`respond(id, decision, scope, overrides)`），不另造审批通道。
- token 闸统计 **`completion_tokens`**（不是 total_tokens，缺字段时回退 total_tokens）。
- 后端测试 `PYTHONPATH=backend/src` + pytest；fixture：`db_conn`、`settings`、`client(db_conn, settings)`。
- 前端颜色 `var(--*)`；三声部字体；测试 `node node_modules/vitest/vitest.mjs run`；类型 `node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`。
- 后端无热加载，改后需重启 uvicorn（127.0.0.1:8734）。

---

## File Structure

- `backend/src/agent/core/budget.py` —— 默认迭代 128/64；输出 token 语义；`raise_limits`。
- `backend/src/agent/core/guard.py` —— `RunawayGuard`（重复失败分级）。
- `backend/src/agent/core/loop.py` —— 接 guard；`_tokens_of` 改 completion_tokens；耗尽挂起「继续」。
- `backend/src/agent/api/server.py` —— `GET/PUT /api/settings/loop`。
- `backend/src/agent/services/app.py` —— 读配置构造 loop（max_iterations/token_budget/approvals/guard）。
- `frontend/src/services/api.ts` —— `getLoopSettings`/`updateLoopSettings`。
- `frontend/src/views/SettingsView.vue` —— 「对话深度」卡片。
- `frontend/src/stores/events.ts` + `session.ts` + `MessageStream.vue` —— 「继续/停止」操作条。
- 测试：`backend/tests/test_budget_defaults.py`、`test_runaway_guard.py`、`test_loop_continue.py`、`test_settings_loop_api.py`；前端 `SettingsView.test.ts`。

---

### Task 1: 迭代与 token 预算默认值（`core/budget.py`）

**Files:**
- Modify: `backend/src/agent/core/budget.py`
- Test: `backend/tests/test_budget_defaults.py`

**Interfaces:**
- Produces: `default_iterations(mode) -> int`（native 128 / text 64）；`DEFAULT_OUTPUT_TOKENS_PER_ITER = 400`；`IterationBudget.consume_output_tokens(n)`；`IterationBudget.raise_limits(extra_iterations, extra_tokens)`；`exhausted` 基于迭代数或输出 token。

- [ ] **Step 1: 写失败测试**

```python
from agent.adapters.base import AdapterMode
from agent.core.budget import (
    DEFAULT_OUTPUT_TOKENS_PER_ITER,
    IterationBudget,
    default_iterations,
)


def test_default_iterations_native_and_text():
    assert default_iterations(AdapterMode.NATIVE) == 128
    assert default_iterations(AdapterMode.TEXT) == 64


def test_output_tokens_per_iter_value():
    assert DEFAULT_OUTPUT_TOKENS_PER_ITER == 400


def test_exhausted_by_output_tokens():
    b = IterationBudget(max_iterations=128, token_budget=800)
    b.consume_output_tokens(400)
    assert not b.exhausted
    b.consume_output_tokens(400)
    assert b.exhausted


def test_raise_limits_extends_budget():
    b = IterationBudget(max_iterations=8, token_budget=3200)
    b.raise_limits(32, 12800)
    assert b.max_iterations == 40
    assert b.token_budget == 16000


def test_zero_token_budget_means_no_token_gate():
    b = IterationBudget(max_iterations=3, token_budget=0)
    b.consume_output_tokens(10_000)
    assert not b.exhausted  # 只受迭代数约束
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_budget_defaults.py -v`
Expected: FAIL（`consume_output_tokens`/`raise_limits` 不存在、默认值仍 5/3）

- [ ] **Step 3: 实现**

```python
DEFAULT_OUTPUT_TOKENS_PER_ITER = 400


def default_iterations(mode: AdapterMode) -> int:
    return 128 if mode == AdapterMode.NATIVE else 64


@dataclass
class IterationBudget:
    max_iterations: int
    token_budget: int = 0          # 0 = 不设输出 token 上限
    used_iterations: int = 0
    used_tokens: int = 0           # 输出 token 累计

    @property
    def exhausted(self) -> bool:
        if self.used_iterations >= self.max_iterations:
            return True
        return self.token_budget > 0 and self.used_tokens >= self.token_budget

    @property
    def iterations_left(self) -> int:
        return max(0, self.max_iterations - self.used_iterations)

    def consume_iteration(self) -> None:
        self.used_iterations += 1

    def consume_output_tokens(self, n: int) -> None:
        if n > 0:
            self.used_tokens += n

    # 向后兼容别名（旧调用点仍传 total-token 语义）
    def consume_tokens(self, n: int) -> None:
        self.consume_output_tokens(n)

    def raise_limits(self, extra_iterations: int, extra_tokens: int) -> None:
        """「继续」时追加一批预算。"""
        self.max_iterations += extra_iterations
        if self.token_budget > 0:
            self.token_budget += extra_tokens
```

> 注意：原 `DEFAULT_TOKEN_BUDGET` 常量保留（值改为 `128 * DEFAULT_OUTPUT_TOKENS_PER_ITER`），避免其他引用报错。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_budget_defaults.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/core/budget.py backend/tests/test_budget_defaults.py
git commit -m "feat(budget): raise default iterations to 128/64 + output-token semantics"
```

---

### Task 2: 跑飞护栏（`core/guard.py`）

**Files:**
- Create: `backend/src/agent/core/guard.py`
- Test: `backend/tests/test_runaway_guard.py`

**Interfaces:**
- Produces: `GuardVerdict`（`OK/WARN/BLOCK/HALT`）；`RunawayGuard.observe(tool: str, arguments: dict, ok: bool) -> GuardVerdict`。

- [ ] **Step 1: 写失败测试**

```python
from agent.core.guard import GuardVerdict, RunawayGuard


def test_same_call_thresholds():
    g = RunawayGuard()
    args = {"q": "x"}
    assert g.observe("web_search", args, False) == GuardVerdict.OK
    assert g.observe("web_search", args, False) == GuardVerdict.WARN   # 2
    assert g.observe("web_search", args, False) == GuardVerdict.WARN   # 3
    assert g.observe("web_search", args, False) == GuardVerdict.WARN   # 4
    assert g.observe("web_search", args, False) == GuardVerdict.BLOCK  # 5


def test_same_tool_halt_at_eight():
    g = RunawayGuard()
    verdicts = [g.observe("run_cmd", {"c": i}, False) for i in range(8)]
    assert verdicts[-1] == GuardVerdict.HALT
    assert GuardVerdict.HALT not in verdicts[:-1]


def test_success_never_escalates():
    g = RunawayGuard()
    for _ in range(10):
        assert g.observe("web_search", {"q": "x"}, True) == GuardVerdict.OK


def test_halt_dominates_block():
    g = RunawayGuard()
    # 同一调用反复失败到达工具级 8 次 halt
    last = None
    for _ in range(8):
        last = g.observe("web_search", {"q": "same"}, False)
    assert last == GuardVerdict.HALT
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_runaway_guard.py -v`
Expected: FAIL（`ModuleNotFoundError: agent.core.guard`）

- [ ] **Step 3: 实现**

```python
"""Runaway guard: detect repeated failing tool calls and escalate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class GuardVerdict(str, Enum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


@dataclass
class RunawayGuard:
    """同调用失败 2 次 WARN / 5 次 BLOCK；同工具失败 3 次 WARN / 8 次 HALT。"""

    call_warn: int = 2
    call_block: int = 5
    tool_warn: int = 3
    tool_halt: int = 8
    _call_fails: dict[str, int] = field(default_factory=dict)
    _tool_fails: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def _key(tool: str, arguments: dict) -> str:
        return tool + "::" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)

    def observe(self, tool: str, arguments: dict, ok: bool) -> GuardVerdict:
        if ok:
            return GuardVerdict.OK
        key = self._key(tool, arguments)
        self._call_fails[key] = self._call_fails.get(key, 0) + 1
        self._tool_fails[tool] = self._tool_fails.get(tool, 0) + 1
        if self._tool_fails[tool] >= self.tool_halt:
            return GuardVerdict.HALT
        if self._call_fails[key] >= self.call_block:
            return GuardVerdict.BLOCK
        if self._tool_fails[tool] >= self.tool_warn:
            return GuardVerdict.WARN
        if self._call_fails[key] >= self.call_warn:
            return GuardVerdict.WARN
        return GuardVerdict.OK
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_runaway_guard.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/core/guard.py backend/tests/test_runaway_guard.py
git commit -m "feat(core): add RunawayGuard for repeated failing tool calls"
```

---

### Task 3: loop 接护栏 + token 闸改输出 + 耗尽挂起「继续」

**Files:**
- Modify: `backend/src/agent/core/loop.py`
- Test: `backend/tests/test_loop_continue.py`

**Interfaces:**
- Consumes: `RunawayGuard`、`IterationBudget.consume_output_tokens/raise_limits`、`ApprovalService.request`。
- Produces: `AgentLoop.__init__` 新增 `guard=None`、`approvals=None`、`continue_batch_iterations=32`、`continue_batch_tokens=12800`。

- [ ] **Step 1: 写失败测试**

```python
import asyncio
from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall, ToolSpec
from agent.core.loop import AgentLoop
from agent.core.budget import IterationBudget
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _Bus:
    async def publish(self, event):  # 忽略事件
        return None


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}
    async def run(self, **kwargs):
        return ToolResult(ok=True, content="ok")


class _ScriptedAdapter:
    mode = AdapterMode.NATIVE
    model = "test"

    def __init__(self):
        self.n = 0
    async def complete(self, messages, tools, **kw):
        self.n += 1
        # 一直返回工具调用，逼到预算耗尽
        tc = ToolCall(id=f"t{self.n}", name="echo", arguments={})
        return Completion(message=ChatMessage(role="assistant", content=None, tool_calls=[tc]),
                          usage={"completion_tokens": 1})


class _ApproveOnce:
    def __init__(self):
        self.calls = 0
    async def request(self, kind, payload):
        self.calls += 1
        return type("R", (), {"decision": "approved"})()


def test_loop_continues_after_exhaustion():
    reg = ToolRegistry()
    reg.register(_EchoTool())
    adapter = _ScriptedAdapter()
    approvals = _ApproveOnce()
    loop = AgentLoop(adapter, reg, _Bus(), approvals=approvals,
                     continue_batch_iterations=2, continue_batch_tokens=0)
    loop.budget = IterationBudget(max_iterations=1, token_budget=0)
    asyncio.run(loop.run("go"))
    assert approvals.calls >= 1  # 触发过「继续」
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_loop_continue.py -v`
Expected: FAIL（`AgentLoop` 无 `approvals` 参数）

- [ ] **Step 3: 实现**

在 `AgentLoop.__init__` 增加参数并保存：
```python
        approvals=None,
        guard=None,
        continue_batch_iterations: int = 32,
        continue_batch_tokens: int = 12800,
        ...
        self.approvals = approvals
        self.guard = guard
        self.continue_batch_iterations = continue_batch_iterations
        self.continue_batch_tokens = continue_batch_tokens
        self._halted = False
```

`_tokens_of` 改为输出 token：
```python
    def _tokens_of(self, completion: Completion) -> int:
        usage = completion.usage or {}
        return int(usage.get("completion_tokens", usage.get("total_tokens", 0)) or 0)
```

耗尽处改为「挂起等继续」：
```python
            if self.budget.exhausted:
                if self.approvals is None:
                    phase = LoopPhase.STOPPED
                    self._warn(f"预算耗尽（{self.budget.used_iterations}/{self.budget.max_iterations}）")
                    await self._emit(EventType.WARNING, {
                        "code": "budget_exhausted",
                        "message": f"迭代达到上限（{self.budget.used_iterations}/{self.budget.max_iterations}）",
                        "recoverable": True,
                    })
                    break
                decision = await self.approvals.request("continue", {
                    "used_iterations": self.budget.used_iterations,
                    "max_iterations": self.budget.max_iterations,
                    "used_tokens": self.budget.used_tokens,
                    "token_budget": self.budget.token_budget,
                })
                if decision.decision == "approved":
                    self.budget.raise_limits(
                        self.continue_batch_iterations, self.continue_batch_tokens
                    )
                    continue
                phase = LoopPhase.STOPPED
                break
```

护栏接入 `_guarded_execute`（它在每个工具调用外层，最合适）：把返回值改为先执行、再按 guard 判定：
```python
    async def _guarded_execute(self, call) -> Any:
        """在独立 task 中执行，登记到 _active_tool_tasks 以便 cancel()。"""
        task = asyncio.current_task()
        if task is not None:
            self._active_tool_tasks.add(task)
        try:
            result = await self.registry.execute(call)
            if self.guard is not None:
                verdict = self.guard.observe(call.name, dict(call.arguments), result.ok)
                if verdict == GuardVerdict.BLOCK:
                    return ToolResult(ok=False, error="guard: 重复失败已被拦截")
                if verdict == GuardVerdict.HALT:
                    self._halted = True
                    self._warn("guard: 同一工具反复失败，终止本轮")
                elif verdict == GuardVerdict.WARN:
                    self._warn(f"guard: {call.name} 反复失败，建议换方法")
            return result
        finally:
            if task is not None:
                self._active_tool_tasks.discard(task)
```

顶部导入：`from agent.core.guard import GuardVerdict, RunawayGuard`。
在 `while True` 循环体最上（`if self.budget.exhausted` 之前）加：
```python
            if self._halted:
                phase = LoopPhase.STOPPED
                break
```

- [ ] **Step 4: 运行确认通过 + 既有 loop 测试无回归**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_loop_continue.py tests/test_loop.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/core/loop.py backend/tests/test_loop_continue.py
git commit -m "feat(loop): output-token gate + runaway guard + continue-on-exhaustion"
```

---

### Task 4: 后端 `GET/PUT /api/settings/loop` + app 接线

**Files:**
- Modify: `backend/src/agent/api/server.py`、`backend/src/agent/services/app.py`
- Test: `backend/tests/test_settings_loop_api.py`

**Interfaces:**
- Produces: `GET/PUT /api/settings/loop`，字段 `max_iterations`、`output_token_budget`。

- [ ] **Step 1: 写失败测试**

```python
def test_loop_settings_defaults(client):
    r = client.get("/api/settings/loop")
    assert r.status_code == 200
    assert r.json()["max_iterations"] == 128


def test_loop_settings_roundtrip(client):
    r = client.put("/api/settings/loop", json={"max_iterations": 200, "output_token_budget": 90000})
    assert r.status_code == 200
    body = client.get("/api/settings/loop").json()
    assert body["max_iterations"] == 200
    assert body["output_token_budget"] == 90000


def test_loop_settings_reject_bad(client):
    assert client.put("/api/settings/loop", json={"max_iterations": 0}).status_code == 400
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_settings_loop_api.py -v`
Expected: FAIL（路由 404）

- [ ] **Step 3: 实现**

`server.py`（放在 `/api/settings/ui` 之后）：
```python
    LOOP_MAX_ITERATIONS_LIMIT = 1000

    @app.get("/api/settings/loop")
    async def get_loop_settings() -> dict:
        store = ctx.settings_store
        return {
            "max_iterations": store.get_int("loop.max_iterations", 128),
            "output_token_budget": store.get_int("loop.output_token_budget", 51200),
        }

    @app.put("/api/settings/loop")
    async def update_loop_settings(body: dict) -> dict:
        store = ctx.settings_store
        if "max_iterations" in body:
            try:
                v = int(body["max_iterations"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_iterations must be an integer")
            if not (1 <= v <= LOOP_MAX_ITERATIONS_LIMIT):
                raise HTTPException(status_code=400, detail="max_iterations out of range")
            store.set("loop.max_iterations", str(v))
        if "output_token_budget" in body:
            try:
                v = int(body["output_token_budget"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="output_token_budget must be an integer")
            if v < 0:
                raise HTTPException(status_code=400, detail="output_token_budget must be >= 0")
            store.set("loop.output_token_budget", str(v))
        return await get_loop_settings()
```

`app.py` 两处 `AgentLoop(...)` 构造改为传配置：
```python
        from agent.core.guard import RunawayGuard
        _max_iter = self.settings_store.get_int("loop.max_iterations", 0)
        _tok = self.settings_store.get_int("loop.output_token_budget", 0)
        loop = AgentLoop(
            adapter, self.registry, self.bus,
            tool_trace=self._record_tool_call,
            tool_selector=self._route_tools,
            max_iterations=_max_iter or None,
            token_budget=_tok or None,
            approvals=self.approvals,
            guard=RunawayGuard(),
        )
```

- [ ] **Step 4: 运行确认通过 + 后端全量无回归**

Run: `cd backend && $env:PYTHONPATH="src"; pytest -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/api/server.py backend/src/agent/services/app.py backend/tests/test_settings_loop_api.py
git commit -m "feat(loop): add /api/settings/loop + wire config into AgentLoop"
```

---

### Task 5: 前端设置卡片「对话深度」

**Files:**
- Modify: `frontend/src/services/api.ts`、`frontend/src/views/SettingsView.vue`
- Test: `frontend/src/views/__tests__/SettingsView.test.ts`

**Interfaces:**
- Produces: `interface LoopSettings { max_iterations: number; output_token_budget: number }`；`api.getLoopSettings`/`api.updateLoopSettings`。

- [ ] **Step 1: api.ts 加类型与方法**

```ts
export interface LoopSettings {
  max_iterations: number;
  output_token_budget: number;
}
```
```ts
  getLoopSettings: () =>
    request<LoopSettings>("/api/settings/loop"),
  updateLoopSettings: (body: Record<string, unknown>) =>
    request<LoopSettings>("/api/settings/loop", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
```

- [ ] **Step 2: 偏好页加「对话深度」卡片**

在「输出速度」section 后插入：迭代上限（QNumber，1–1000）、输出 token 预算（QNumber，0–500000），保存按钮，保存后 settingsNotice 显示「已保存对话深度」。

- [ ] **Step 3: 测试 + vue-tsc**

在 `SettingsView.test.ts` 的 api mock 增加 `getLoopSettings`/`updateLoopSettings`；新增用例：加载渲染「对话深度」卡片。运行：
Run: `cd frontend && node node_modules/vitest/vitest.mjs run && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/services/api.ts frontend/src/views/SettingsView.vue frontend/src/views/__tests__/SettingsView.test.ts
git commit -m "feat(settings): add conversation-depth card (iterations + output budget)"
```

---

### Task 6: 前端「继续/停止」操作条

**Files:**
- Modify: `frontend/src/stores/events.ts`、`frontend/src/stores/session.ts`
- Create: `frontend/src/components/ContinueBar.vue`
- Modify: `frontend/src/components/MessageStream.vue`（挂载 ContinueBar）
- Test: `frontend/src/stores/__tests__/events.test.ts`

**Interfaces:**
- Consumes: `APPROVAL_REQUIRED`（kind="continue"）事件；`POST /api/approvals/{id}/respond`。
- Produces: `session.pendingContinue`（`{id, used, max} | null`）；ContinueBar 组件。

- [ ] **Step 1: session 加状态 + events 识别**

`session.ts` state 加：
```ts
    pendingContinue: null as { id: string; used: number; max: number } | null,
```
`events.ts` 的 `APPROVAL_REQUIRED` 分支：若 `approval.kind === "continue"`，写 `session.pendingContinue = { id, used, max }` 并 return（不进入 approvals 队列）。

- [ ] **Step 2: ContinueBar 组件**

渲染「已用 x/y 迭代 · 是否继续？[继续][停止]」；点击调用 `api.respondApproval(id, "approved"|"rejected")`（若 api.ts 无此方法则新增 `respondApproval`），成功后清空 `session.pendingContinue`。

- [ ] **Step 3: 挂载到 MessageStream**

在 `.stream` 底部、`showTyping` 附近渲染 `<ContinueBar v-if="session.pendingContinue" />`。

- [ ] **Step 4: 测试 + vue-tsc**

`events.test.ts` 加用例：kind="continue" 的 APPROVAL_REQUIRED → session.pendingContinue 有值。
Run: `cd frontend && node node_modules/vitest/vitest.mjs run && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/stores/events.ts frontend/src/stores/session.ts frontend/src/components/ContinueBar.vue frontend/src/components/MessageStream.vue frontend/src/stores/__tests__/events.test.ts
git commit -m "feat(ui): add continue/stop bar for exhausted iteration budget"
```

---

### Task 7: 端到端验证 + 视觉确认

- [ ] **Step 1: 后端全量**：`cd backend && $env:PYTHONPATH="src"; pytest -q -p no:cacheprovider`（EXITCODE=0）
- [ ] **Step 2: 前端全量**：`cd frontend && node node_modules/vitest/vitest.mjs run && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`（全过）
- [ ] **Step 3: 重启后端**，实测 `/api/settings/loop` 读写；设很小迭代上限触发「继续」→ 点继续续跑；护栏在重复失败时告警/拦截。
- [ ] **Step 4: Playwright 截图**视觉确认设置页「对话深度」卡片 + 「继续/停止」操作条渲染正确。
