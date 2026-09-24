# 工具失败与静默失败加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让工具调用失败时「失败得成」（环境就位、审批等得及）并且「失败说得清」（原因准确、落库、不再出现空白回答）。

**Architecture:** 七处独立改动，全部落在既有边界内：配置层补齐默认工作区目录；开发工作区把内存注册表变成「内存 + 磁盘回填」；命令类工具的工具级超时把审批窗口算进去；审批结局用一张映射表翻译成中文；抓取失败的判据收紧并新增「需要 JavaScript」这一类；主循环在结束前保证有文本；轨迹表新增 `error` 列；前端放宽失败原因显示上限。

**Tech Stack:** Python 3.12 / pytest / SQLite（顺序迁移）/ Vue 3 + Pinia + vitest。

**Spec:** `docs/superpowers/specs/2026-09-24-tool-failure-hardening-spec.md`

## Global Constraints

- 迁移只允许追加新的一条，禁止修改历史迁移。
- 任何日志、事件、Trace、错误信息、测试输出都不得出现密钥原文。
- 文案一律中文；失败不得被说成成功，取消不得被说成失败。
- 不写会过期的硬编码数字到文档（测试数、事件数、表数）。
- 本仓库工作树是多路并行状态：不要执行 `git` 写操作（不 add / 不 commit / 不 checkout），只改本计划列出的文件。
- 每个任务独立跑测试；后端收尾跑全量 `uv run --frozen pytest`，前端收尾跑 `npx vue-tsc --noEmit` 与 `npm test`。

---

### Task 1: 默认工作区根目录存在

**Files:**
- Modify: `backend/src/agent/config.py`（`workspace_dir` 属性 + `ensure_dirs`）
- Modify: `backend/src/agent/services/computer.py`（`ensure_root`）
- Modify: `backend/src/agent/services/app.py:213`（构造后建一次）
- Modify: `backend/src/agent/api/server.py:586-596`（保存设置时建一次）
- Test: `backend/tests/test_workspace_root.py`（新建）

**Interfaces:**
- Produces: `Settings.workspace_dir -> Path`；`ComputerSandbox.ensure_root() -> Path`（幂等，建不出来只记日志）

- [x] **Step 1: 写失败测试**

```python
from __future__ import annotations

from pathlib import Path

from agent.config import Settings
from agent.services.computer import ComputerSandbox


def test_ensure_dirs_creates_default_workspace_root(tmp_path: Path):
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    assert (tmp_path / "workspace").is_dir()


def test_computer_ensure_root_creates_configured_root(tmp_path: Path):
    root = tmp_path / "elsewhere"
    sb = ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: "default")
    assert sb.ensure_root() == root.resolve()
    assert root.is_dir()


def test_computer_ensure_root_does_not_raise_on_broken_path(tmp_path: Path):
    """路径上有文件挡着时：只记日志，不抛 —— 由工具如实报路径错误。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    sb = ComputerSandbox(
        resolve_root=lambda: str(blocker / "workspace"), permission_mode=lambda: "default"
    )
    sb.ensure_root()  # 不抛异常
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_workspace_root.py -v`
Expected: FAIL（`AttributeError: 'Settings' object has no attribute 'workspace_dir'` / `ensure_root`）

- [x] **Step 3: 实现**

`config.py`：新增属性并加进 `ensure_dirs`：

```python
    @property
    def workspace_dir(self) -> Path:
        """电脑操控的默认工作区根目录（`computer.root_dir` 留空时用它）。"""
        return self.data_dir / "workspace"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.archive_dir, self.log_dir, self.workspace_dir):
            p.mkdir(parents=True, exist_ok=True)
```

`computer.py`：文件顶部加 `import logging` 与 `logger = logging.getLogger(__name__)`，类里加：

```python
    def ensure_root(self) -> Path:
        """建工作区根目录（幂等）。

        根目录不存在时，相对路径的 fs_* 会一律报「系统找不到指定的路径」，
        模型分不清是自己写错了路径还是环境没准备好（真实事故：默认根
        `%APPDATA%\\qio\\workspace` 从来没被创建过）。建不出来只记日志：
        不在这里伪造成功，工具仍然会如实报错。
        """
        root = self._root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("computer workspace root unavailable: %s (%s)", root, exc)
        return root
```

`app.py:213` 之后（`self.services.register("computer", self.computer)` 之前或之后均可）：

```python
        self.computer = ComputerSandbox(resolve_root=_computer_root, permission_mode=_computer_mode)
        # 启动即就位：默认根目录不存在会让所有相对路径的文件工具直接失败
        self.computer.ensure_root()
```

`server.py` 的 `update_computer_settings`：`store.set("computer.root_dir", ...)` 之后补
`ctx.computer.ensure_root()`。

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_workspace_root.py tests/test_settings_computer_api.py tests/test_computer_sandbox.py -v`
Expected: PASS

---

### Task 2: 开发工作区跨进程存活

**Files:**
- Modify: `backend/src/agent/tools/dev_workspace.py`（`__init__` + `_restore` + `_read_request`）
- Test: `backend/tests/test_dev_tools.py`（追加）

**Interfaces:**
- Consumes: `DevWorkspace(root_dir)`
- Produces: 重启后 `task(task_id)` 返回 `DevTask`（`request` 为去掉文件头的需求正文）

- [x] **Step 1: 写失败测试**

```python
def test_workspace_survives_process_restart(tmp_path):
    root = tmp_path / "ws"
    first = DevWorkspace(root)
    task = first.create("检索原神测试服爆料")
    first.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 1")

    reborn = DevWorkspace(root)  # 模拟后端/应用重启

    restored = reborn.task(task.id)
    assert restored is not None
    assert restored.request == "检索原神测试服爆料"
    assert reborn.list_files(task.id) == ["request.md", "tool.json", "tool.py"]
    assert "def run" in reborn.read_file(task.id, "tool.py")


def test_workspace_restore_ignores_foreign_entries(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    (root / "not-a-workspace").mkdir()
    (root / "ws_short").mkdir()
    (root / "loose.txt").write_text("x", encoding="utf-8")
    ws = DevWorkspace(root)
    assert ws.task("not-a-workspace") is None
    assert ws.task("ws_short") is None


def test_workspace_restore_tolerates_missing_request_file(tmp_path):
    root = tmp_path / "ws"
    root.mkdir(parents=True)
    (root / "ws_0123456789ab").mkdir()
    ws = DevWorkspace(root)
    task = ws.task("ws_0123456789ab")
    assert task is not None
    assert task.request == ""
    assert ws.list_files(task.id) == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_dev_tools.py -v -k restart`
Expected: FAIL（`restored is None`）

- [x] **Step 3: 实现**

```python
_TASK_ID = re.compile(r"^ws_[0-9a-f]{12}$")


def _read_request(task_dir: Path) -> str:
    """读回工作区的需求正文（写入时带了「# 开发需求」文件头，这里去掉）。"""
    try:
        text = (task_dir / "request.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    marker = "# 开发需求"
    if text.lstrip().startswith(marker):
        text = text.lstrip()[len(marker):]
    return text.strip()
```

`DevWorkspace.__init__` 末尾调用 `self._restore()`，并加：

```python
    def _restore(self) -> None:
        """把磁盘上已有的工作区登记回内存。

        以前只有内存字典：应用一重启，磁盘上的 ws_* 目录还在（文件一个没少），
        但 dev_* 工具一律回「找不到工作区」，工具创建流程就断在那里 ——
        模型只会拿着同一个 id 反复重试（真实事故：连续 5 次失败）。
        """
        try:
            entries = sorted(self.root_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or not _TASK_ID.match(entry.name):
                continue
            try:
                created = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
            except OSError:
                created = datetime.now(timezone.utc)
            self._tasks[entry.name] = DevTask(
                id=entry.name,
                request=_read_request(entry),
                dir=entry,
                created_at=created.isoformat(),
            )
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_dev_tools.py tests/test_dev_workflow_integration.py tests/test_tool_lifecycle.py -v`
Expected: PASS

---

### Task 3: 审批等待不被工具超时截断，且结局分开表述

**Files:**
- Modify: `backend/src/agent/tools/approval.py`（`refusal_reason`）
- Modify: `backend/src/agent/tools/cmd_tools.py`（`APPROVAL_SLACK_MS` + 两个工具的超时 + 结局文案）
- Modify: `backend/src/agent/tools/fs_tools.py`（`_permitted` 结局文案）
- Modify: `backend/src/agent/tools/registry.py:312`（复用 `refusal_reason`）
- Test: `backend/tests/test_cmd_tools.py`、`backend/tests/test_fs_tools.py`（各追加）

**Interfaces:**
- Produces: `refusal_reason(decision: str) -> str`；`APPROVAL_SLACK_MS: int`

- [x] **Step 1: 写失败测试**

```python
def test_command_tool_timeout_covers_approval_window():
    """真实事故：run_shell 的工具超时是 45 秒，而审批给用户 5 分钟，
    于是用户还没点确认，工具就已经报「超时」结束。审批窗口必须算进工具超时。"""
    approval_ms = int(DEFAULT_TIMEOUT_SECONDS * 1000)
    assert APPROVAL_SLACK_MS == approval_ms
    assert RunCmdTool.timeout_ms >= approval_ms + 45_000
    assert RunProgramTool.timeout_ms >= approval_ms + 45_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision, expected",
    [("timeout", "等你的确认"), ("rejected", "你点了拒绝"), ("cancelled", "本轮已停止")],
)
async def test_shell_approval_outcome_is_specific(decision, expected):
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="approve")
    t.approvals = _FakeApproval(decision=decision)
    res = await t.run(cmd="echo hi")
    assert not res.ok
    assert "未获批准" in res.error
    assert expected in res.error
```

`test_fs_tools.py` 里同样的参数化版本，工具换成 `FsReadTool`（`path` 指向根外）。

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_cmd_tools.py -v -k approval_outcome`
Expected: FAIL（错误文案里只有「未获批准，未执行」）

- [x] **Step 3: 实现**

`approval.py`：

```python
def refusal_reason(decision: str) -> str:
    """审批没通过的人话原因：超时 / 拒绝 / 取消必须分开说。

    真实事故：三种结局都写成「未获批准，未执行」，读起来像是用户拒绝了，
    实际是 5 分钟过期（一轮里 6 次）。
    """
    if decision == "timeout":
        return "等你的确认超过 5 分钟，已自动取消"
    if decision == "rejected":
        return "你点了拒绝"
    if decision == "cancelled":
        return "本轮已停止"
    return "未获批准"
```

`cmd_tools.py`：`from agent.tools.approval import DEFAULT_TIMEOUT_SECONDS as APPROVAL_TIMEOUT_SECONDS`
与 `from agent.tools.approval import refusal_reason`，然后

```python
APPROVAL_SLACK_MS = int(APPROVAL_TIMEOUT_SECONDS * 1000)
```

`RunProgramTool` / `RunCmdTool` 的 `timeout_ms` 改成 `45_000 + APPROVAL_SLACK_MS`，并把
两处 `if r.decision != "approved": return ToolResult(ok=False, error="…未获批准，未执行")`
改成 `return ToolResult(ok=False, error=f"{action} 未获批准：{refusal_reason(r.decision)}")`
（`action` 分别是 `"程序执行"` / `"命令执行"`）。

`fs_tools.py` 的 `_permitted` 同样改成 `f"{payload.get('action')} 未获批准：{refusal_reason(r.decision)}"`。

`registry.py` 的 `_approval_policy` 末尾：

```python
        return ToolResult(
            ok=False, error=f"工具「{tool.name}」未执行（{refusal_reason(decision.decision)}）"
        )
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_cmd_tools.py tests/test_fs_tools.py tests/test_tool_policy.py tests/test_tool_event_payload.py -v`
Expected: PASS

---

### Task 4: 抓取失败的原因可核

**Files:**
- Modify: `backend/src/agent/tools/web_fetch.py`（判据收紧 + 连接失败写地址 + JS 页面单独成类）
- Modify: `backend/src/agent/tools/registry.py`（`_exception_text`）
- Test: `backend/tests/test_web_fetch_tool.py`、`backend/tests/test_tool_pipeline.py`（追加）

**Interfaces:**
- Produces: `_exception_text(exc: BaseException) -> str`

- [x] **Step 1: 写失败测试**

```python
async def test_javascript_only_page_reported_as_such():
    tool = WebFetchTool()
    html = (
        "<html><head><title>Project Amber</title></head><body><div id=\"app\"></div>"
        "<noscript>We're sorry but Project Amber doesn't work properly without "
        "JavaScript enabled. Please enable it to continue.</noscript></body></html>"
    )

    async def fake(url: str) -> tuple[int, str]:
        return (200, html)

    tool.fetch_html = fake
    res = await tool.run(url="http://a.com")
    assert not res.ok
    assert "JavaScript" in res.error
    assert "登录" not in res.error and "验证" not in res.error


async def test_agent_hint_containing_verify_word_is_not_a_login_wall():
    """真实事故：页面里写着邀请 AI agent 读 /api/AGENTS 的说明（含 verify），
    以前被判成「该页面需要登录或验证」，模型因此放弃这些站点。"""
    tool = WebFetchTool()
    html = (
        "<html><head><meta name=\"description\" content=\"For code assistants, agents or "
        "chat models: to verify agentic access, please fetch the page at /api/AGENTS\">"
        "</head><body><article><p>正文在这里</p></article></body></html>"
    )

    async def fake(url: str) -> tuple[int, str]:
        return (200, html)

    tool.fetch_html = fake
    res = await tool.run(url="http://a.com")
    assert res.ok
    assert "正文在这里" in res.content


async def test_connection_failure_names_the_url_and_cause():
    tool = WebFetchTool()

    async def boom(url: str) -> tuple[int, str]:
        raise RuntimeError("")

    tool.fetch_html = boom
    res = await tool.run(url="http://a.com/x")
    assert not res.ok
    assert "http://a.com/x" in res.error
    assert "RuntimeError" in res.error
```

`test_tool_pipeline.py`（或本任务新建的 `test_tool_errors.py`）：

```python
async def test_empty_exception_message_still_explains():
    """真实事故：错误信息是「ConnectError: 」，冒号后面什么都没有。"""
    registry = ToolRegistry()
    registry.register(_NoMessageTool())
    res = await registry.execute(ToolCall(id="c1", name="nomsg", arguments={}))
    assert not res.ok
    assert "没有给出说明" in res.error


async def test_exception_cause_is_shown_when_message_is_empty():
    registry = ToolRegistry()
    registry.register(_EmptyWithCauseTool())
    res = await registry.execute(ToolCall(id="c2", name="cause", arguments={}))
    assert "getaddrinfo failed" in res.error
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_web_fetch_tool.py -v`
Expected: FAIL（`test_agent_hint_containing_verify_word_is_not_a_login_wall` 报「需要登录或验证」）

- [x] **Step 3: 实现**

`web_fetch.py`：

```python
# 明确短语才判「登录/验证墙」：裸词 verify 会命中页面里无关的文本
# （真实事故：站点邀请 AI agent 读 /api/AGENTS 的说明被判成验证墙）。
BAD_HTML_FLAGS = (
    "captcha",
    "安全验证",
    "人机验证",
    "access denied",
    "verify you are human",
    "checking your browser",
    "just a moment",
)
# 页面自己声明「要有 JavaScript 才能用」：这是脚本渲染，不是登录墙。
JS_REQUIRED_FLAGS = ("without javascript", "enable javascript", "javascript is disabled")
```

`WebFetchTool.run` 里：包住抓取调用、在 BAD_HTML_FLAGS 之前先判 JS：

```python
        try:
            code, html = await self.fetch_html(url)
        except Exception as exc:  # noqa: BLE001 - 网络层失败要说清是哪个地址
            return ToolResult(
                ok=False,
                error=f"抓取失败：连不上 {url}（{type(exc).__name__}: {str(exc).strip() or '没有更多说明'}）",
            )
        if code != 200:
            return ToolResult(ok=False, error=f"无法读取正文（HTTP {code}）")
        low = html.lower()
        if any(f in low for f in JS_REQUIRED_FLAGS):
            return ToolResult(
                ok=False,
                error=(
                    "网页正文由脚本在浏览器里生成，抓取器读不到"
                    "（不是登录或验证问题；该站点若有数据接口，直接抓接口）"
                ),
            )
        if any(f in low for f in BAD_HTML_FLAGS):
            return ToolResult(ok=False, error="该页面需要登录或验证，无法读取正文")
```

`registry.py`：

```python
def _exception_text(exc: BaseException) -> str:
    """把异常写成「类型 + 说明」；说明为空时给出可查的替代信息。

    以前是 f"{type(exc).__name__}: {exc}"：底层异常没有文本时只剩
    「ConnectError: 」这半句（真实事故里出现 22 次）。"""
    detail = str(exc).strip()
    if not detail:
        cause = exc.__cause__ or exc.__context__
        detail = str(cause).strip() if cause is not None else ""
    if not detail:
        detail = "（底层错误没有给出说明）"
    return f"{type(exc).__name__}: {detail}"
```

`_default_execute` 的兜底分支改成 `error=_exception_text(exc)`。

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_web_fetch_tool.py tests/test_tool_pipeline.py tests/test_search.py -v`
Expected: PASS

---

### Task 5: 轮次结束不能是空白回答

**Files:**
- Modify: `backend/src/agent/core/loop.py`（`_stop_note` + 结束前兜底）
- Test: `backend/tests/test_loop_continue.py`（追加）

**Interfaces:**
- Produces: `TurnResult.final_content` 在「非取消」轮次里必定是非空文本

- [x] **Step 1: 写失败测试**

```python
def test_guard_halt_never_leaves_an_empty_answer():
    """真实事故：护栏终止后回答是空串，界面上只剩一个空气泡，用户不知道发生了什么。"""
    guard = RunawayGuard()
    loop = _loop(_ScriptedAdapter("boom"), guard=guard)
    loop.budget = IterationBudget(max_iterations=50, token_budget=0)
    result = asyncio.run(loop.run("go"))
    assert (result.final_content or "").strip()
    assert "boom" in result.final_content
    assert "nope" in result.final_content  # 带上最后一次失败原因


def test_budget_stop_never_leaves_an_empty_answer():
    approvals = _ApproveThenReject()
    loop = _loop(_ScriptedAdapter(), approvals=approvals)
    result = asyncio.run(loop.run("go"))
    assert (result.final_content or "").strip()
    assert "达到上限" in result.final_content
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_loop_continue.py -v -k empty_answer`
Expected: FAIL（`final_content` 为 `None`）

- [x] **Step 3: 实现**

`loop.py`：`__init__` 里加 `self._stop_note: str | None = None`。

护栏 HALT 的两个分支在设置 `self._halted = True` 之前写：

```python
                    self._stop_note = (
                        f"本轮没有产生回答：工具 {call.name} 连续失败 {failures} 次后已停止。"
                        f"最后一次失败原因：{result.error or '（没有更多说明）'}。"
                    )
```

预算分支在 `break` 之前写：

```python
                self._stop_note = f"本轮没有产生回答：{reason}，按你的选择停下来了。"
```

（`reason` 是该分支已有的那句「迭代次数达到上限（x/y）」/「输出 token 预算耗尽（x/y）」。）

`_run` 收尾、构造 `TurnResult` 之前：

```python
        cancelled = self.is_cancelled()
        if cancelled:
            phase = LoopPhase.STOPPED
        if not cancelled and not (final_content or "").strip():
            # 静默失败收口：护栏终止 / 预算停止 / 模型什么都没说，都必须留下人话
            final_content = self._stop_note or "本轮没有产生回答，也没有给出原因。"
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_loop_continue.py tests/test_loop.py tests/test_runaway_guard.py tests/test_turn_lifecycle_protocol.py -v`
Expected: PASS（取消轮仍然 `final_content is None`）

---

### Task 6: 失败原因落到轨迹表

**Files:**
- Modify: `backend/src/agent/core/loop.py:250-255`（`tool_trace` 载荷带 `error`）
- Modify: `backend/src/agent/services/app.py:952-977`（写 `error`）
- Modify: `backend/src/agent/storage/schema.py`（迁移 19）
- Test: `backend/tests/test_tool_call_audit.py`（新建）

**Interfaces:**
- Consumes: `tool_trace({"tool_name", "arguments", "ok", "result", "error"})`
- Produces: `tool_calls.error` 列

- [x] **Step 1: 写失败测试**

```python
from __future__ import annotations

import sqlite3

from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.schema import MIGRATIONS


def test_migration_adds_error_column_to_tool_calls(db_conn: sqlite3.Connection):
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(tool_calls)")}
    assert "error" in cols


def test_legacy_tool_calls_table_gets_error_column(tmp_path):
    """老库升级：tool_calls 没有 error 列时，迁移必须补上且不丢旧行。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > 18:
            break
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )
    conn.execute(
        "INSERT INTO tool_calls (id, tool_name, arguments, result, ok, created_at) "
        "VALUES ('tc_old', 'fs_list', '{}', '', 0, '2026-01-01T00:00:00+00:00')"
    )

    apply_migrations(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tool_calls)")}
    assert "error" in cols
    row = conn.execute("SELECT tool_name, error FROM tool_calls WHERE id='tc_old'").fetchone()
    assert row["tool_name"] == "fs_list"
    assert row["error"] is None
    conn.close()


def test_failed_tool_call_records_its_reason(app_ctx):
    app_ctx._record_tool_call(
        {"tool_name": "fs_list", "arguments": {}, "ok": False, "result": "", "error": "找不到工作区：ws_x"}
    )
    row = app_ctx.conn.execute(
        "SELECT tool_name, error FROM tool_calls ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["tool_name"] == "fs_list"
    assert row["error"] == "找不到工作区：ws_x"
```

（`app_ctx` 用 `tests/test_runtime_state.py` 里已有的 AppContext fixture；若那里的 fixture 名不同，就用同名 fixture 的写法照抄。）

- [x] **Step 2: 跑测试确认失败**

Run: `cd backend; uv run --frozen pytest tests/test_tool_call_audit.py -v`
Expected: FAIL（`error` 列不存在）

- [x] **Step 3: 实现**

`schema.py` 末尾追加（不要改历史迁移）：

```python
    (
        19,
        [
            # 轨迹表以前只落「结果正文」，失败的调用落下来是一片空白：
            # 事后无法回答「它当时为什么失败」。失败原因必须有自己的列。
            "ALTER TABLE tool_calls ADD COLUMN error TEXT",
        ],
    ),
```

`loop.py::_on_pipeline_result` 的载荷加一项：

```python
                "ok": result.ok,
                "result": result.content,
                "error": result.error,
```

`app.py::_record_tool_call`：

```python
        result = str(trace.get("result") or "")[:200]
        error = str(trace.get("error") or "")[:200]
        self.conn.execute(
            "INSERT INTO tool_calls (id, topic_id, tool_name, arguments, result, error, ok, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("tc"),
                active.topic_id if active else None,
                trace.get("tool_name", "?"),
                args,
                result,
                error,
                1 if trace.get("ok") else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
```

- [x] **Step 4: 跑测试确认通过**

Run: `cd backend; uv run --frozen pytest tests/test_tool_call_audit.py tests/test_storage.py tests/test_m12_trace.py tests/test_trace_turn.py -v`
Expected: PASS

---

### Task 7: 前端失败原因可读 + 文档同步

**Files:**
- Modify: `frontend/src/components/MessageItem.vue:107-120`
- Modify: `frontend/src/components/__tests__/MessageItem.test.ts`
- Modify: `docs/status.md`（末尾追加本轮小节）

**Interfaces:**
- Produces: `.tool-fail-line` 显示 120 字符以内的原始原因

- [x] **Step 1: 写失败测试**

```ts
it("失败原因在 120 字以内原样显示（不折叠成笼统文案）", () => {
  const reason =
    "列目录失败：[WinError 3] 系统找不到指定的路径。: 'C:\\Users\\zxy\\AppData\\Roaming\\qio\\workspace'";
  expect(reason.length).toBeGreaterThan(60);
  expect(reason.length).toBeLessThanOrEqual(120);
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mountItem(
    makeMessage({ role: "tool", toolName: "fs_list", content: "", toolOk: false, toolError: reason }),
    pinia,
  );
  expect(w.find(".tool-fail-line").text()).toBe(reason);
  w.unmount();
});

it("超过 120 字的失败原因截断并提示展开", () => {
  const reason = "连接失败：".repeat(30);
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mountItem(
    makeMessage({ role: "tool", toolName: "web_fetch", content: "", toolOk: false, toolError: reason }),
    pinia,
  );
  const line = w.find(".tool-fail-line").text();
  expect(line).toContain("展开可看完整原因");
  expect(line.length).toBeLessThan(reason.length);
  w.unmount();
});
```

- [x] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/components/__tests__/MessageItem.test.ts`
Expected: FAIL（第一条得到「这次执行没有成功，展开可看原因」）

- [x] **Step 3: 实现**

```ts
/** 工具失败时卡片上的一行结论（完整错误仍在折叠详情里） */
const FAILURE_LINE_LIMIT = 120;
const toolFailureLine = computed(() => {
  if (!toolFailed.value) return "";
  const raw = (props.message.toolError ?? "").trim();
  if (raw) {
    if (raw.length <= FAILURE_LINE_LIMIT) return raw;
    return raw.slice(0, FAILURE_LINE_LIMIT) + "…（展开可看完整原因）";
  }
  if (toolState.value === "cancelled") return "这次执行已取消";
  if (toolState.value === "unknown") return "结果未收到";
  return "这次执行没有成功";
});
```

`docs/status.md` 末尾追加一节，记录：五类根因、各自修法与验证命令（不写测试数量等会过期的数字）。

- [x] **Step 4: 跑测试确认通过**

Run: `cd frontend; npx vue-tsc --noEmit; npx vitest run src/components/__tests__/MessageItem.test.ts`
Expected: PASS

---

## 收尾验证（全部任务完成后）

```powershell
cd backend; uv run --frozen pytest
uv run --frozen python -m agent.eval.run        # 本轮改了工具策略与主循环，需与基线对比
cd ..; python scripts/check_docs.py
cd frontend; npx vue-tsc --noEmit; npm test
```
