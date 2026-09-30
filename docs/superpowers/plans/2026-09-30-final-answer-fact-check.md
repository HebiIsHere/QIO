# 最终结论的事实校正 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「本轮到底做了什么」成为后端手里的事实台账，让模型只能在这本账上声明「完成 / 可用」，并在本轮存在未解决失败时给最终答复补一段后端事实说明。

**Architecture:** 工具结果新增机器可读的 `facts` 通道（`ToolResult.facts`）；开发类工具把「任务 / 版本 / 测试证据 / 是否提交」上报进去；`core/turn_facts.py` 收成一份每轮台账并生成注记；`core/loop.py` 在收尾时按规则追加；新工具 `declare_completion` 用同一份权威来源逐条核对模型声明。

**Tech Stack:** Python 3.11 / pytest / asyncio（全部 fake，不联网、不调真实模型）。

**Spec:** `docs/superpowers/specs/2026-09-30-final-answer-fact-check-design.md`

## Global Constraints

- 不新增数据库迁移；台账只存在内存里（每轮一份）。
- 不重写模型正文：注记只追加在后端产出的最终答复末尾。
- 注记先过 `agent/trace/redact.py::redact_text` 再限长；文案里不出现花括号。
- 取消不算失败；后台维护轮（空 `ToolRegistry()`）不得产生注记。
- 不要引入以真实 API Key 或联网为前提的测试。

---

### Task 1: 事实通道与每轮台账（纯逻辑）

**Files:**
- Modify: `backend/src/agent/tools/base.py`
- Create: `backend/src/agent/core/turn_facts.py`
- Test: `backend/tests/test_turn_facts.py`

**Interfaces:**
- Produces: `ToolResult.facts: dict | None`；`DevTaskFact(task_id, tool_name, phase, version, submitted, requires_tests, test_state, test_passed, test_summary)`；`TurnFacts.record_tool/record_dev_task/record_declaration/declaration_accepted/unresolved/annotation`；常量 `DEV_TEST_NONE/CURRENT/STALE`。

- [ ] **Step 1: 写失败测试**

```python
def test_clean_turn_adds_nothing():
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="dev_run_tests", ok=True, status="success")
    assert facts.annotation() is None


def test_unresolved_tool_failure_is_annotated():
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="dev_run_tests", ok=False,
                      status="failed", category="assertion", error="断言不匹配")
    note = facts.annotation()
    assert note and "dev_run_tests" in note and "断言不匹配" in note


def test_later_success_clears_the_same_tool():
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="dev_run_tests", ok=False, status="failed")
    facts.record_tool(call_id="c2", tool_name="dev_run_tests", ok=True, status="success")
    assert facts.annotation() is None


def test_cancelled_is_not_a_failure():
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="run_shell", ok=False, status="cancelled")
    assert facts.annotation() is None


def test_accepted_declaration_suppresses_the_note():
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="dev_run_tests", ok=False, status="failed")
    facts.record_declaration(accepted=True, basis="测试 2/2 通过 · 版本 a1b2")
    assert facts.annotation() is None


def test_dev_task_without_evidence_is_annotated():
    facts = TurnFacts()
    facts.record_dev_task(DevTaskFact(task_id="ws_ab12cd34ef56", tool_name="create_tool"))
    note = facts.annotation()
    assert note and "ws_ab12cd34ef56" in note and "测试证据" in note


def test_subagent_task_does_not_require_tests():
    facts = TurnFacts()
    facts.record_dev_task(DevTaskFact(
        task_id="ws_ab12cd34ef56", tool_name="dev_run_tests", requires_tests=False))
    assert facts.annotation() is None


def test_annotation_is_redacted_and_has_no_braces():
    facts = TurnFacts()
    facts.record_tool(call_id="c1", tool_name="web_fetch", ok=False, status="failed",
                      error="auth failed with api_key=sk-abcdef123456")
    note = facts.annotation() or ""
    assert "sk-abcdef123456" not in note
    assert "{" not in note and "}" not in note
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_turn_facts.py -q`
Expected: FAIL（`ModuleNotFoundError: agent.core.turn_facts`）

- [ ] **Step 3: 实现**

`tools/base.py` 的 `ToolResult` 增加 `facts: dict[str, Any] | None = None`；
新建 `core/turn_facts.py`：`DevTaskFact`（dataclass，含 `from_payload`）；`TurnFacts`
（`_tools` 按工具名后写覆盖、`_dev_tasks` 按任务覆盖、`_declaration`）；
`unresolved()` 产出人话条目（工具失败 / 测试失败 / 证据失效 / 还没有测试证据，最后一条只在
`requires_tests` 为真时出现）；`annotation()` 在「有未解决失败且没有被接受的声明」时输出，
输出前 `redact_text` + 限长 600 字符 + 最多 3 条 + 文案不含花括号。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_turn_facts.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/tools/base.py backend/src/agent/core/turn_facts.py backend/tests/test_turn_facts.py
git commit -m "feat(core): 每轮事实台账与工具结果 facts 通道"
```

### Task 2: 开发工具上报任务事实

**Files:**
- Modify: `backend/src/agent/tools/dev_workspace.py`
- Modify: `backend/src/agent/tools/dev_tools.py`
- Test: `backend/tests/test_dev_tools.py`

**Interfaces:**
- Consumes: `DevWorkspace.status(task_id)` 的既有字段（`phase`/`submitted`/`evidence_state`/`last_test_passed`/`last_test_summary`/`content_digest`）。
- Produces: `DevWorkspace.fact_for(task_id: str, tool_name: str) -> dict`，形状 `{"dev_task": {...}}`。

- [ ] **Step 1: 写失败测试**

```python
def test_fact_for_reports_evidence_and_requires_tests(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    fact = ws.fact_for(task.id, "create_tool")["dev_task"]
    assert fact["id"] == task.id
    assert fact["test"]["state"] == "none"
    assert fact["requires_tests"] is True
    assert fact["version"] == ws.content_digest(task.id)


async def test_dev_run_tests_reports_facts():
    # 跑一次 dev_run_tests，断言 result.facts["dev_task"]["test"]["state"] == "current"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_tools.py -q -k fact_for`
Expected: FAIL（`AttributeError: 'DevWorkspace' object has no attribute 'fact_for'`）

- [ ] **Step 3: 实现**

`fact_for` 由 `status()` + `read_definition()` 组装（`requires_tests` 取 `tool_type != "subagent"`）；
`create_tool` / `dev_write_file` / `dev_run_tests` / `dev_submit_tool` 的每个返回分支都带
`facts=self.workspaces.fact_for(workspace, self.name)`（失败的返回也要带，记的是「操作之后」的真实状态）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/tools/dev_workspace.py backend/src/agent/tools/dev_tools.py backend/tests/test_dev_tools.py
git commit -m "feat(dev): 开发工具上报任务事实（版本 / 证据 / 提交）"
```

### Task 3: `declare_completion` 声明核对

**Files:**
- Create: `backend/src/agent/tools/declare_completion.py`
- Test: `backend/tests/test_declare_completion.py`

**Interfaces:**
- Consumes: `DevWorkspace.status/read_definition`、`ToolRegistry.get`。
- Produces: `DeclareCompletionTool(workspaces, registry, tool_store=None)`，`name = "declare_completion"`，
  `facts={"declaration": {"accepted": bool, "task_id": str, "version": str, "claims": [...], "basis": str | None, "missing": [...]}}`。

- [ ] **Step 1: 写失败测试**（判定矩阵：全满足 / 版本不符 / 证据失效 / 未提交 / 未知任务 / subagent 不要求测试 / 非法 claim 词）

```python
async def test_declare_accepts_when_everything_matches(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    registry = ToolRegistry()
    task = ws.create("x")
    ws.write_definition(task.id, _definition())
    ws.record_test(task.id, True, "1/1 tests passed")
    registry.register(_DummyTool("add_numbers"))
    ws.mark_submitted(task.id)
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(task_id=task.id, version=ws.content_digest(task.id),
                       claims=["test_passed", "registered", "usable"])
    assert r.ok and r.facts["declaration"]["accepted"] is True


async def test_declare_rejects_stale_evidence(tmp_path):
    # record_test 之后 write_file 改内容 → test_passed 不成立，错误里要提到「失效 / 重跑」


async def test_declare_rejects_wrong_version(tmp_path):
    # 版本对不上 → 提示声明的是旧版本
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_declare_completion.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

按规格第 3 条的判定表逐条核对（`registered` 还要求 `submitted_digest == version` 且注册表里拿得到工具；
`usable` 对 `function` 型要求测试证据、对 `subagent` 型不要求）；非法 claim 词直接拒绝并列出合法取值。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_declare_completion.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/tools/declare_completion.py backend/tests/test_declare_completion.py
git commit -m "feat(tools): declare_completion 按后端事实核对完成结论"
```

### Task 4: 主循环记账与收尾注记

**Files:**
- Modify: `backend/src/agent/core/loop.py`
- Test: `backend/tests/test_final_answer_fact_check.py`

**Interfaces:**
- Consumes: `TurnFacts`（Task 1）、`ToolResult.facts`（Task 2/3）。
- Produces: `AgentLoop.turn_facts`（每轮新建）、`AgentLoop._record_turn_facts(call, result)`。

- [ ] **Step 1: 写失败测试**（真事故形状：工具失败 + 模型说「测试全部通过」）

```python
async def test_success_claim_after_failed_tool_is_corrected():
    registry = ToolRegistry()
    registry.register(FailingDevTool())
    loop = AgentLoop(_ClaimAdapter(), registry, EventBus())
    result = await loop.run("做一个工具")
    assert "测试全部通过，工具已就绪" in result.final_content  # 模型的话原样保留
    assert "系统核对" in result.final_content                  # 末尾补了后端事实


async def test_accepted_declaration_leaves_the_answer_untouched():
    # 工具返回 facts={"declaration": {"accepted": True, "basis": "..."}} → 不加注记


async def test_clean_turn_is_untouched():
    # 成功的工具 + 正常回答 → final_content 原样
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_final_answer_fact_check.py -q`
Expected: FAIL（最终答复里没有「系统核对」）

- [ ] **Step 3: 实现**

`_run` 开头 `self.turn_facts = TurnFacts()`；回填工具结果时调 `_record_turn_facts`；
收尾（`final_content` 定稿之后、构造 `TurnResult` 之前）在**未取消**时追加 `annotation()`。
`declare_completion` 的 `facts["declaration"]` 由同一条记账路径写进台账。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_final_answer_fact_check.py tests/test_tool_feedback.py tests/test_loop.py tests/test_dev_workflow_integration.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/core/loop.py backend/tests/test_final_answer_fact_check.py
git commit -m "feat(core): 主循环记账并在收尾补后端事实说明"
```

### Task 5: 装配、提示词与展示名

**Files:**
- Modify: `backend/src/agent/services/app.py`
- Modify: `backend/src/agent/prompts.py`
- Modify: `backend/src/agent/tools/display.py`
- Test: `backend/tests/test_active_tools.py`

**Interfaces:**
- Produces: 应用装配后 `registry.get("declare_completion")` 可调用。

- [ ] **Step 1: 写失败测试**

```python
def test_declare_completion_is_registered(ctx):  # AppContext fixture
    assert ctx.registry.get("declare_completion") is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_active_tools.py -q -k declare`
Expected: FAIL

- [ ] **Step 3: 实现**

`AppContext.__init__` 在 `self.tool_store` 建好之后（`_restore_tools()` 附近）注册
`DeclareCompletionTool(self.dev_workspaces, self.registry, self.tool_store)`；
提示词说明「要说完成 / 可用之前先声明」；展示名「开发：声明完成结论」。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_active_tools.py tests/test_app_integration.py tests/test_settings_tools_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/services/app.py backend/src/agent/prompts.py backend/src/agent/tools/display.py backend/tests
git commit -m "feat(tools): 装配 declare_completion 与提示词"
```

### Task 6: 文档与全量验证

- [ ] **Step 1:** `docs/status.md` 的「第一阶段收尾」一节补上本段（状态从 planned 改成已实现的部分，以及仍未做的前端标记）
- [ ] **Step 2:** `python scripts/check_docs.py` — Expected: 通过
- [ ] **Step 3:** `cd backend; uv run --frozen pytest`（或 `.venv` pytest）全量必须全绿
