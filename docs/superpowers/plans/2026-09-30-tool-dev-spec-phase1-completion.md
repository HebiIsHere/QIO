# QIO 工具开发规范与可靠性修复 · 第一阶段收尾 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把第一阶段的「可信状态 / 版本证据 / 提交复测写回 / 注册恢复 / 执行协议严格校验」落到代码：测试通过不能靠写文件伪造，提交后的项目不再被删除，注册与持久化失败可回滚，worker 的假冒成功结果被拒绝。

**Architecture:** 沿用既有 `DevWorkspace`（工作区目录 + `state.json`）与 `ToolLifecycle`（审批 + 注册）。证据一律以「内容摘要」为版本标识：只在 `record_test` 里生成，内容一变立即失效。`ToolStore` 与注册表改为「先持久化、后注册，失败即回滚」。`SandboxExecutor` 的 worker 协议从「读最后一行」改为「严格单行 + 退出码 + 字段类型」校验，并加系统环境白名单与有界读取。

**Tech Stack:** Python 3.11 / pytest / asyncio。

**Spec:** 本批次实现 `docs/status.md`「工具开发规范与可靠性修复 · 第一阶段（2026-09-29）」与「工具 worker 模式（2026-09-29）」两节中标注为 *仍未实现 / NOT RUN* 的五项，以及 `docs/superpowers/plans/2026-09-29-tool-dev-spec-phase1.md` 后续修法的一部分。

## Global Constraints

- 改 schema 只能追加新迁移，禁止修改历史迁移（本批次不新增迁移）。
- 任何日志、事件、Trace、错误信息、测试输出都不得出现密钥原文；新增输出路径必须过 `agent/trace/redact.py`。
- 安全表述必须诚实：受限子进程不是安全沙箱；保留文件名（`state.json`）不是完整安全边界，文档里必须照实写。
- 不要引入以真实 API Key 或联网为前提的测试；模型调用一律用 fake/mock provider。
- 沟通、提交信息、文档一律中文。

---

### Task 1: 测试证据绑定内容摘要，内容一变立即失效

**Files:**
- Modify: `backend/src/agent/tools/dev_workspace.py`
- Modify: `backend/src/agent/tools/dev_tools.py`
- Test: `backend/tests/test_dev_tools.py`

**Interfaces:**
- Consumes: `DevWorkspace.content_digest(task_id) -> str | None`（已存在）。
- Produces: `DevTask.last_test_digest: str | None`、`DevTask.evidence_state: str`（`"none" | "current" | "stale"`）；`DevWorkspace.status(task_id) -> dict` 新增键 `evidence_state`、`last_test_digest`。

- [ ] **Step 1: 写失败测试**

```python
def test_test_evidence_is_invalidated_when_content_changes(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.record_test(task.id, True, "1/1 tests passed")
    assert ws.status(task.id)["evidence_state"] == "current"
    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 999")
    state = ws.status(task.id)
    assert state["evidence_state"] == "stale"
    assert state["last_test_passed"] is True  # 历史保留，但不再是可用证据
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_tools.py -k evidence_is_invalidated -q`
Expected: FAIL（`KeyError: 'evidence_state'`）

- [ ] **Step 3: 实现**

`DevTask` 增加 `last_test_digest` / `evidence_state`；`record_test` 设 `"current"` 并记录当时的 `content_digest`；`write_file` 写完后若摘要与 `last_test_digest` 不符则改 `"stale"`；`status()` 按当前摘要兜底重算（防越权改文件）；`_write_state` / `_restore` 带上这两个字段。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/tools/dev_workspace.py backend/tests/test_dev_tools.py
git commit -m "feat(dev): 测试证据绑定内容摘要，内容变更即失效"
```

### Task 2: `state.json` 由后端独占，恢复时校验 schema 与来源

**Files:**
- Modify: `backend/src/agent/tools/dev_workspace.py`
- Test: `backend/tests/test_dev_tools.py`

**Interfaces:**
- Produces: `DevWorkspace.write_file` 对 `state.json` / `request.md` 抛 `ValueError`；`state.json` 顶层带 `"schema": 2` 与 `"source": "qio.dev_workspace"`。

- [ ] **Step 1: 写失败测试**

```python
def test_agent_cannot_forge_state_file(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    with pytest.raises(ValueError):
        ws.write_file(task.id, "state.json", '{"last_test_passed": true, "test_runs": 99}')


def test_restore_rejects_state_without_schema(tmp_path):
    root = tmp_path / "ws"
    ws = DevWorkspace(root)
    task = ws.create("x")
    (task.dir / "state.json").write_text('{"last_test_passed": true, "test_runs": 99}', encoding="utf-8")
    restored = DevWorkspace(root).task(task.id)
    assert restored.last_test_passed is None
    assert restored.test_runs == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_tools.py -q -k "forge or without_schema"`
Expected: FAIL（现在能写进去、也能读回来）

- [ ] **Step 3: 实现**

`_write_state` 写 `schema` / `source`；`_read_state` 只在 `schema == 2 and source == "qio.dev_workspace"` 时返回数据，否则空字典；`write_file` 拒绝保留名。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/tools/dev_workspace.py backend/tests/test_dev_tools.py
git commit -m "feat(dev): state.json 由后端独占并校验 schema/来源"
```

### Task 3: 提交成功后保留项目，并把复测结果写回同一任务

**Files:**
- Modify: `backend/src/agent/tools/dev_workspace.py`（新增 `archive`）
- Modify: `backend/src/agent/tools/dev_tools.py`
- Modify: `backend/src/agent/tools/lifecycle.py`（`ToolOutcome` 带测试摘要）
- Test: `backend/tests/test_dev_tools.py`、`backend/tests/test_tool_lifecycle.py`、`backend/tests/test_dev_workflow_integration.py`

**Interfaces:**
- Produces: `ToolOutcome(..., test_passed: bool | None = None, test_summary: str | None = None)`；`DevWorkspace.archive(task_id) -> Path | None`。

- [ ] **Step 1: 写失败测试**

```python
async def test_dev_submit_keeps_project_and_writes_back_retest():
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    ws.write_definition(task.id, ToolDefinition(**definition))
    tool = DevSubmitTool(ws, lifecycle_builder=builder)
    r = await tool.run(workspace=task.id, explanation="说明")
    assert r.ok
    assert ws.task(task.id) is not None                  # 项目保留，不再删目录
    assert ws.status(task.id)["submitted"] is True
    assert ws.status(task.id)["last_test_summary"] == "2/2 tests passed"  # 复测写回同一任务
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_tools.py -k keeps_project -q`
Expected: FAIL（现在 `cleanup` 删掉了工作区）

- [ ] **Step 3: 实现**

`DevSubmitTool` 成功后 `mark_submitted` + `archive`（复制到 `<root>/archive/<task_id>/`），不再 `cleanup`；`lifecycle.submit_definition` 把复测结果放进 `ToolOutcome`，`DevSubmitTool` 用 `record_test` 写回。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_tools.py tests/test_tool_lifecycle.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/tools backend/tests
git commit -m "feat(dev): 提交后保留项目并把复测写回任务"
```

### Task 4: 注册与持久化可恢复（先落库，失败即回滚）

**Files:**
- Modify: `backend/src/agent/storage/tool_store.py`（新增 `load(name)`）
- Modify: `backend/src/agent/tools/lifecycle.py`
- Test: `backend/tests/test_tool_lifecycle.py`

**Interfaces:**
- Produces: `ToolStore.load(name: str) -> ToolDefinition | None`；`_approve_and_register` 失败时保证「注册表里没有半成品、持久层保留上一可用版本」。

- [ ] **Step 1: 写失败测试**

```python
async def test_register_rolls_back_when_store_save_fails(...):
    # tool_store.save 抛 sqlite3.OperationalError（磁盘错误）
    outcome = await lifecycle.submit_definition(definition, "说明")
    assert not outcome.ok
    assert registry.get("add_numbers") is None    # 不能留在注册表里可调用
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_tool_lifecycle.py -k rollback -q`
Expected: FAIL（现在先注册再保存，保存失败后工具仍可调用）

- [ ] **Step 3: 实现**

顺序改为「先 `tool_store.save`，后 `_register`」；`except Exception` 里：已注册则用 disposer 撤销，已保存则恢复上一版本（无上一版本则 `remove`），再回报失败。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_tool_lifecycle.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/storage/tool_store.py backend/src/agent/tools/lifecycle.py backend/tests/test_tool_lifecycle.py
git commit -m "fix(tools): 注册与持久化改为可恢复顺序 + 失败回滚"
```

### Task 5: worker 协议严格校验 + Windows 系统环境 + 有界读取

**Files:**
- Modify: `backend/src/agent/tools/sandbox.py`
- Test: `backend/tests/test_sandbox_worker.py`

**Interfaces:**
- Produces: `_execute_subprocess` 只在「退出码 0 + 恰好一行 JSON + `ok` 是布尔 + 成功时 `value` 是对象」时返回 `ok=True`；环境白名单含 `SystemRoot` 等系统变量；stdout / stderr 有读取上限，超限即终止并报错。

- [ ] **Step 1: 写失败测试**

```python
async def test_fake_success_with_nonzero_exit_is_rejected(tmp_path):
    # 用假 worker 脚本：先打印 {"ok": true, "value": {...}}，再 exit 17
    result = await SandboxExecutor().execute("def run(**kwargs):\n    return {}\n", {})
    assert result.ok is False


async def test_system_env_is_visible_to_the_worker(monkeypatch):
    # 白名单变量（如 SystemRoot / TEMP）可见；非白名单变量（如 QIO_SECRET_…）不可见
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_sandbox_worker.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

严格校验（退出码 / 单行 / 类型）；`_system_env()` 白名单；`_drain()` 有界读取（stdout 2 MiB、stderr 512 KiB），超限 kill 进程树并返回 `output_format` 失败。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_sandbox_worker.py tests/test_tool_worker.py tests/test_tool_lifecycle.py tests/test_dev_tools.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/src/agent/tools/sandbox.py backend/tests/test_sandbox_worker.py
git commit -m "fix(sandbox): worker 协议严格校验 + 系统环境白名单 + 有界读取"
```

### Task 6: 同步 `docs/status.md`

**Files:**
- Modify: `docs/status.md`

- [ ] **Step 1: 更新第一阶段两节的状态**（哪些从 planned 变为已实现、哪些仍是 NOT RUN / 未实现）
- [ ] **Step 2:** Run `python scripts/check_docs.py` — Expected: 通过
- [ ] **Step 3:** 后端相关测试全绿，并在 status 里如实写明本次未跑全量的部分
