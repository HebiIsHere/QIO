# QIO 电脑操控能力 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 qio 增加护栏式的"电脑操控"能力——文件系统读写、命令执行、进程/系统信息，配分级授权（低危自动、高危审批、红线拒绝）。

**Architecture:** 新增一个共享安全层 `ComputerSandbox`（统一路径规范、敏感拦截、命令风险判定、权限档位），供三个工具组（文件系统 / 命令执行 / 进程系统）通过 `ServiceRegistry` 注入；权限判定在 `run()` 内部做，复用现有 `ApprovalService`。配置走 `SettingsStore` + `GET/PUT /api/settings/computer`，前端设置页加"电脑操控"卡片。

**Tech Stack:** Python (FastAPI, httpx, pathlib), Vue3 (TS), vitest, pytest。

**Spec:** [2026-09-09-computer-control-design.md](/C:/Users/zxy/Documents/Front agent/qio/docs/superpowers/specs/2026-09-09-computer-control-design.md)

## Global Constraints

- 后端工具必须继承 `agent.tools.base.Tool`，返回 `ToolResult(ok=bool, content=str, error=str|None)`。
- 通过 `ServiceRegistry`（`inject` 列表）注入共享服务；`ToolRegistry.register` 挂载。
- 安全逻辑不散落在各工具里，统一走 `ComputerSandbox`（`inject=["computer"]`）。
- 权限判定在 `run()` 内动态做（复用 `ApprovalService.request(kind="computer", payload)`），**不使用** `Tool.requires_approval` 静态标志（同一工具既含低危又含高危动作）。
- 测试后端用 `PYTHONPATH=backend/src` + pytest；fixture 有 `db_conn`、`settings`、以及 `client(db_conn, settings)`（见 `backend/tests/test_api_routes.py`）。
- 前端颜色一律 `var(--*)`（`frontend/src/styles/tokens.css`），禁止硬编码色值；三声部字体：标题/话题=`--serif`，正文=`--sans`，数据/密钥/预算/ID=`--mono`。
- 前端测试 `node node_modules/vitest/vitest.mjs run`（cwd=`frontend`）；类型检查 `node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`。
- 工具命名 snake_case 描述式：`fs_read`、`run_cmd`、`proc_list`。
- 后端无热加载，改后需重启 uvicorn（`127.0.0.1:8734`）。

---

## File Structure

- `backend/src/agent/services/computer.py` —— `ComputerSandbox`（路径规范化、红线拦截、命令风险判定、权限档位）；`CommandRisk` 枚举。
- `backend/src/agent/tools/fs_tools.py` —— `FsReadTool` / `FsWriteTool` / `FsPatchTool` / `FsListTool` / `FsFindTool` / `FsInfoTool`。
- `backend/src/agent/tools/cmd_tools.py` —— `RunCmdTool` / `SysInfoTool` / `ProcListTool` / `ProcKillTool`。
- `backend/src/agent/api/server.py` —— 新增 `GET/PUT /api/settings/computer`。
- `backend/src/agent/services/app.py` —— 实例化 `ComputerSandbox`、注册服务、挂载工具。
- `backend/src/agent/services/tool_router.py` —— 把 `fs_*` / `run_cmd` / `proc_*` 加入路由（可选，或按相似度）。
- `frontend/src/services/api.ts` —— 新增 `getComputerSettings` / `updateComputerSettings`。
- `frontend/src/views/SettingsView.vue` —— 偏好页新增「电脑操控」卡片。
- 测试：`backend/tests/test_computer_sandbox.py`、`test_fs_tools.py`、`test_cmd_tools.py`、`test_settings_computer_api.py`。

---

### Task 1: 共享安全层 `ComputerSandbox`（`services/computer.py`）

**Files:**
- Create: `backend/src/agent/services/computer.py`
- Test: `backend/tests/test_computer_sandbox.py`

**Interfaces:**
- Produces: `CommandRisk`（`LOW / HIGH / DANGER`）；`ComputerSandbox(resolve_root, permission_mode)`；`ComputerSandbox.check_path(path) -> "auto" | "approve" | "deny"`；`ComputerSandbox.classify_command(cmd) -> CommandRisk`；`ComputerSandbox.mode`（property）。

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

from agent.services.computer import ComputerSandbox, CommandRisk


def _make(root="C:/work") -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: root, permission_mode=lambda: "default")


def test_classify_low_commands():
    s = _make()
    assert s.classify_command("git status") == CommandRisk.LOW
    assert s.classify_command("ls -la") == CommandRisk.LOW
    assert s.classify_command("pwd") == CommandRisk.LOW


def test_classify_high_commands():
    s = _make()
    assert s.classify_command("rm -rf /tmp/x") == CommandRisk.HIGH
    assert s.classify_command("pip install requests") == CommandRisk.HIGH
    assert s.classify_command("sudo reboot") == CommandRisk.HIGH


def test_classify_fail_closed_unknown():
    s = _make()
    # 未匹配白名单的命令应视为 HIGH（fail-closed），而非放行
    assert s.classify_command("python weird_script.py") == CommandRisk.HIGH


def test_check_path_within_root_auto():
    s = _make()
    assert s.check_path("C:/work/a.txt") == "auto"


def test_check_path_redline_deny():
    s = _make()
    assert s.check_path("C:/work/.env") == "deny"
    assert s.check_path("C:/work/.git/config") == "deny"


def test_check_path_outside_root_approve():
    s = _make()
    assert s.check_path("C:/other/file.txt") == "approve"


def test_check_path_traversal_safe():
    s = _make()
    # ../ 穿越应被纳入拒绝或规范化后落在根外 → approve（需审批），绝不误判为根内自动
    assert s.check_path("C:/work/../../etc/passwd") == "approve"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_computer_sandbox.py -v`
Expected: FAIL（`ModuleNotFoundError: agent.services.computer`）

- [ ] **Step 3: 实现 `services/computer.py`**

```python
"""Shared computer-control safety layer: path normalization, redline
interception, command risk classification, permission mode. All
computer-control tools route their permission checks through this; no tool
implements its own security logic."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CommandRisk(str, Enum):
    LOW = "low"
    HIGH = "high"
    DANGER = "danger"


# 绝对红线：即使在工作区内也拒绝
REDLINE_NAMES = {".env", ".git", ".ssh", ".config", "credentials"}
REDLINE_SUFFIXES = ("id_rsa", "id_ed25519", ".pem", "cookie", "cookies.sqlite")

# 低危命令前缀白名单（fail-closed：未匹配即 HIGH）
LOW_CMD_PREFIXES = ("git status", "git diff", "git log", "ls", "pwd", "which",
                    "find", "grep", "cat", "head", "tail", "echo", "wc", "df", "du")


def _normalize(path: str) -> Path:
    return Path(path).resolve()


class ComputerSandbox:
    """Resolve permission verdicts for computer-control actions."""

    def __init__(self, resolve_root, permission_mode) -> None:
        self._resolve_root = resolve_root
        self._mode = permission_mode

    @property
    def mode(self) -> str:
        return self._mode() if callable(self._mode) else str(self._mode)

    def _root(self) -> Path:
        return _normalize(self._resolve_root() if callable(self._resolve_root) else str(self._resolve_root))

    def _is_redline(self, p: Path) -> bool:
        for part in p.parts:
            if part.lower() in REDLINE_NAMES:
                return True
        low = p.name.lower()
        if low in REDLINE_NAMES:
            return True
        return any(low.endswith(s) for s in REDLINE_SUFFIXES)

    def check_path(self, path: str) -> str:
        """Return 'auto' | 'approve' | 'deny' for a filesystem path."""
        try:
            p = _normalize(path)
        except Exception:
            return "deny"
        if self._is_redline(p):
            return "deny"
        root = self._root()
        try:
            is_inside = p.is_relative_to(root)
        except Exception:
            is_inside = False
        # 工作区内：default 下读自动（由调用方按读/写细分）；这里对写类返回需审批
        return "auto" if is_inside else "approve"

    def classify_command(self, cmd: str) -> CommandRisk:
        c = cmd.strip().lower()
        if any(c.startswith(x) for x in LOW_CMD_PREFIXES):
            return CommandRisk.LOW
        if c.startswith(("rm -rf", "sudo", "rm ")):
            return CommandRisk.DANGER
        # fail-closed：未匹配 → HIGH
        return CommandRisk.HIGH
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_computer_sandbox.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/services/computer.py backend/tests/test_computer_sandbox.py
git commit -m "feat(computer): add ComputerSandbox shared safety layer"
```

---

### Task 2: 文件系统工具（`tools/fs_tools.py`）

**Files:**
- Create: `backend/src/agent/tools/fs_tools.py`
- Test: `backend/tests/test_fs_tools.py`

**Interfaces:**
- Consumes: `ComputerSandbox`（`inject=["computer"]`），`ApprovalService`（注入）。
- Produces: `FsReadTool` / `FsWriteTool` / `FsPatchTool` / `FsListTool` / `FsFindTool` / `FsInfoTool`。

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest

from agent.tools.fs_tools import FsReadTool, FsWriteTool


class _FakeSandbox:
    def __init__(self, verdict="auto") -> None:
        self.verdict = verdict
    def check_path(self, path: str) -> str:
        return self.verdict
    @property
    def mode(self) -> str:
        return "default"


class _FakeApproval:
    async def request(self, kind: str, payload: dict):
        return type("R", (), {"decision": "approved"})()


@pytest.mark.asyncio
async def test_fs_read_within_root(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    t = FsReadTool()
    t.computer = _FakeSandbox("auto")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(target))
    assert res.ok
    assert res.content == "hello"


@pytest.mark.asyncio
async def test_fs_write_redline_denied(tmp_path):
    t = FsWriteTool()
    t.computer = _FakeSandbox("deny")
    t.approvals = _FakeApproval()
    res = await t.run(path=str(tmp_path / ".env"), content="SECRET=1")
    assert not res.ok
    assert "拒绝" in res.error
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_fs_tools.py -v`
Expected: FAIL（`ModuleNotFoundError: agent.tools.fs_tools`）

- [ ] **Step 3: 实现 `tools/fs_tools.py`**

```python
"""Filesystem tools for computer control (patch-style edits, redline guard)."""

from __future__ import annotations

from typing import Any

from agent.tools.base import Tool, ToolResult


class _FsTool(Tool):
    inject = ["computer", "approvals"]

    def __init__(self, computer=None, approvals=None) -> None:
        self.computer = computer
        self.approvals = approvals


class FsReadTool(_FsTool):
    name = "fs_read"
    description = "读取一个文件的内容。path 必填。自动放行（工作区内）。"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        if not path:
            return ToolResult(ok=False, error="path 必填")
        verdict = self.computer.check_path(path)
        if verdict == "deny":
            return ToolResult(ok=False, error="拒绝访问该路径（敏感路径）")
        if verdict == "approve":
            # 工作区外读：默认审批（read 也可放宽；此处统一审批）
            r = await self.approvals.request("computer", {"action": "read", "path": path})
            if r.decision != "approved":
                return ToolResult(ok=False, error="读取未获批准，未读取")
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取失败：{exc}")
        return ToolResult(ok=True, content=text)


class FsWriteTool(_FsTool):
    name = "fs_write"
    description = "写入/覆盖一个文件。path、content 必填。需审批。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path") or "").strip()
        content = str(kwargs.get("content") or "")
        if not path:
            return ToolResult(ok=False, error="path 必填")
        verdict = self.computer.check_path(path)
        if verdict == "deny":
            return ToolResult(ok=False, error="拒绝访问该路径（敏感路径）")
        r = await self.approvals.request("computer", {"action": "write", "path": path})
        if r.decision != "approved":
            return ToolResult(ok=False, error="写入未获批准，未写入")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            return ToolResult(ok=False, error=f"写入失败：{exc}")
        return ToolResult(ok=True, content=f"已写入 {path}")


# 说明：fs_patch / fs_list / fs_find / fs_info 按同一模式扩展（见 spec §4.1）。
# 为控制篇幅，示例给出 read/write；实现时补齐其余四个工具，逻辑一致。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_fs_tools.py -v`
Expected: PASS（实现补齐 `fs_patch`/`fs_list`/`fs_find`/`fs_info` 后本文件测试通过）

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/tools/fs_tools.py backend/tests/test_fs_tools.py
git commit -m "feat(computer): add filesystem tools with redline guard"
```

---

### Task 3: 命令执行与进程工具（`tools/cmd_tools.py`）

**Files:**
- Create: `backend/src/agent/tools/cmd_tools.py`
- Test: `backend/tests/test_cmd_tools.py`

**Interfaces:**
- Consumes: `ComputerSandbox`（`inject=["computer"]`），`ApprovalService`。
- Produces: `RunCmdTool` / `SysInfoTool` / `ProcListTool` / `ProcKillTool`。

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest

from agent.services.computer import CommandRisk
from agent.tools.cmd_tools import RunCmdTool


class _FakeSandbox:
    def classify_command(self, cmd: str) -> CommandRisk:
        return CommandRisk.LOW if cmd == "echo hi" else CommandRisk.HIGH
    @property
    def mode(self) -> str:
        return "default"


class _FakeApproval:
    async def request(self, kind: str, payload: dict):
        return type("R", (), {"decision": "approved"})()


@pytest.mark.asyncio
async def test_run_cmd_low_auto():
    t = RunCmdTool()
    t.computer = _FakeSandbox()
    t.approvals = _FakeApproval()
    res = await t.run(cmd="echo hi")
    assert res.ok
    assert "hi" in res.content


@pytest.mark.asyncio
async def test_run_cmd_high_needs_approval():
    t = RunCmdTool()
    t.computer = _FakeSandbox()
    t.approvals = _FakeApproval()
    # HIGH 命令应走审批；fake 审批 approved → 执行
    res = await t.run(cmd="rm -rf /tmp/x")
    assert res.ok
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_cmd_tools.py -v`
Expected: FAIL（`ModuleNotFoundError: agent.tools.cmd_tools`）

- [ ] **Step 3: 实现 `tools/cmd_tools.py`**

```python
"""Command and process tools for computer control (risk-gated)."""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from agent.services.computer import CommandRisk
from agent.tools.base import Tool, ToolResult


class _CmdTool(Tool):
    inject = ["computer", "approvals"]

    def __init__(self, computer=None, approvals=None) -> None:
        self.computer = computer
        self.approvals = approvals


class RunCmdTool(_CmdTool):
    name = "run_cmd"
    description = "执行一条 shell 命令，捕获 stdout/stderr。低危命令自动放行；高危命令需审批。"
    parameters = {
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        "required": ["cmd"],
    }
    timeout_ms = 40_000

    async def run(self, **kwargs: Any) -> ToolResult:
        cmd = str(kwargs.get("cmd") or "").strip()
        if not cmd:
            return ToolResult(ok=False, error="cmd 必填")
        cwd = str(kwargs.get("cwd") or "") or None
        risk = self.computer.classify_command(cmd)
        if risk != CommandRisk.LOW:
            r = await self.approvals.request("computer", {"action": "run_cmd", "cmd": cmd, "risk": risk.value})
            if r.decision != "approved":
                return ToolResult(ok=False, error="命令执行未获批准，未执行")
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=float(kwargs.get("timeout", 30)))
        except asyncio.TimeoutError:
            return ToolResult(ok=False, error="命令执行超时")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(ok=False, error=f"命令执行失败：{exc}")
        content = out.decode(errors="replace").strip()
        if err:
            content += ("\n[stderr]\n" + err.decode(errors="replace").strip())
        return ToolResult(ok=True, content=content or "(无输出)")


class SysInfoTool(_CmdTool):
    name = "sys_info"
    description = "返回系统平台/CPU/内存/磁盘信息。自动放行。"
    parameters = {"type": "object", "properties": {}}
    is_concurrency_safe = True

    async def run(self, **kwargs: Any) -> ToolResult:
        info = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
        }
        return ToolResult(ok=True, content="\n".join(f"{k}: {v}" for k, v in info.items()))


# 说明：proc_list / proc_kill 按同一模式扩展。proc_list 自动；proc_kill 审批。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_cmd_tools.py -v`
Expected: PASS（实现补齐 `proc_list`/`proc_kill` 后通过）

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/tools/cmd_tools.py backend/tests/test_cmd_tools.py
git commit -m "feat(computer): add command/process tools with risk gating"
```

---

### Task 4: 后端 `GET/PUT /api/settings/computer`

**Files:**
- Modify: `backend/src/agent/api/server.py`（在 `/api/settings/ui` 附近）
- Test: `backend/tests/test_settings_computer_api.py`

**Interfaces:**
- Produces: `GET /api/settings/computer -> {root_dir, permission_mode}`；`PUT /api/settings/computer` 接收 `{root_dir?, permission_mode?}`。

- [ ] **Step 1: 写失败测试**

```python
from __future__ import annotations


def test_get_computer_defaults(client):
    r = client.get("/api/settings/computer")
    assert r.status_code == 200
    assert r.json()["permission_mode"] == "default"


def test_put_computer_saves(client):
    r = client.put("/api/settings/computer", json={"permission_mode": "accept-edits", "root_dir": "C:/work"})
    assert r.status_code == 200
    body = client.get("/api/settings/computer").json()
    assert body["permission_mode"] == "accept-edits"
    assert body["root_dir"] == "C:/work"


def test_put_rejects_bad_mode(client):
    r = client.put("/api/settings/computer", json={"permission_mode": "hack"})
    assert r.status_code == 400
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_settings_computer_api.py -v`
Expected: FAIL（路由 404）

- [ ] **Step 3: 实现接口**

```python
    PERMISSION_MODES = ("default", "plan", "accept-edits", "bypass")

    @app.get("/api/settings/computer")
    async def get_computer_settings() -> dict:
        store = ctx.settings_store
        return {
            "root_dir": store.get("computer.root_dir", "") or "",
            "permission_mode": store.get("computer.permission_mode", "default") or "default",
        }

    @app.put("/api/settings/computer")
    async def update_computer_settings(body: dict) -> dict:
        store = ctx.settings_store
        if "root_dir" in body:
            store.set("computer.root_dir", str(body.get("root_dir") or ""))
        if "permission_mode" in body:
            mode = str(body["permission_mode"])
            if mode not in PERMISSION_MODES:
                raise HTTPException(status_code=400, detail="invalid permission_mode")
            store.set("computer.permission_mode", mode)
        return await get_computer_settings()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && $env:PYTHONPATH="src"; pytest tests/test_settings_computer_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/agent/api/server.py backend/tests/test_settings_computer_api.py
git commit -m "feat(computer): add GET/PUT /api/settings/computer"
```

---

### Task 5: 接入 `AppContext`（实例化 + 挂载工具）

**Files:**
- Modify: `backend/src/agent/services/app.py`

**Interfaces:**
- Produces: `AppContext.computer`（`ComputerSandbox`）。

- [ ] **Step 1: 实例化并注册服务 + 挂载工具**

在 `app.py` 的 `SearchService` 代码之后添加：
```python
        from agent.services.computer import ComputerSandbox
        from agent.tools.fs_tools import FsReadTool, FsWriteTool
        from agent.tools.cmd_tools import RunCmdTool, SysInfoTool

        def _computer_root() -> str:
            return self.settings_store.get("computer.root_dir", "") or str(self.settings.data_dir / "workspace")

        def _computer_mode() -> str:
            return self.settings_store.get("computer.permission_mode", "default") or "default"

        self.computer = ComputerSandbox(resolve_root=_computer_root, permission_mode=_computer_mode)
        self.services.register("computer", self.computer)
        # 注册文件系统 + 命令/进程工具
        self.registry.register(FsReadTool())
        self.registry.register(FsWriteTool())
        self.registry.register(RunCmdTool())
        self.registry.register(SysInfoTool())
```

> 注：`RunCmdTool` / `Fs*Tool` 通过 `inject=["computer","approvals"]` 自动注入服务；构造时传 `None`，由 `ServiceRegistry.attach` setattr。

- [ ] **Step 2: 运行现有后端测试，确认无回归**

Run: `cd backend && $env:PYTHONPATH="src"; pytest -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add backend/src/agent/services/app.py
git commit -m "feat(computer): wire ComputerSandbox + register tools"
```

---

### Task 6: 前端 api 客户端

**Files:**
- Modify: `frontend/src/services/api.ts`

**Interfaces:**
- Produces: `interface ComputerSettings { root_dir: string; permission_mode: string }`；`api.getComputerSettings` / `api.updateComputerSettings`。

- [ ] **Step 1: 添加类型与方法**

```ts
export interface ComputerSettings {
  root_dir: string;
  permission_mode: string;
}
```

在 `api` 对象里追加：
```ts
  getComputerSettings: () =>
    request<ComputerSettings>("/api/settings/computer"),
  updateComputerSettings: (body: Record<string, unknown>) =>
    request<ComputerSettings>("/api/settings/computer", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
```

- [ ] **Step 2: 跑类型检查**

Run: `cd frontend && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/services/api.ts
git commit -m "feat(computer): add computer settings api client"
```

---

### Task 7: 前端「电脑操控」卡片

**Files:**
- Modify: `frontend/src/views/SettingsView.vue`
- Test: `frontend/src/views/__tests__/SettingsView.test.ts`

**Interfaces:**
- Consumes: `api.getComputerSettings` / `api.updateComputerSettings`。

- [ ] **Step 1: 在偏好页加「电脑操控」section**

在「输出速度」section 之后加：
- 工作区根目录（文本输入，绑定 `computerRootDir`）。
- 权限模式（QSelect，四档 `default/plan/accept-edits/bypass`）。
- 保存按钮，保存后 `settingsNotice` 显示「已保存电脑操控配置」。

样式用 `var(--*)`，标题 `--serif`，路径用 `--mono`。

- [ ] **Step 2: 更新测试 `SettingsView.test.ts`**

新增用例：加载时渲染电脑操控卡片；保存后显示成功 notice。mock `api.getComputerSettings` / `api.updateComputerSettings`。

- [ ] **Step 3: 跑前端测试与类型检查**

Run: `cd frontend && node node_modules/vitest/vitest.mjs run && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/views/SettingsView.vue frontend/src/views/__tests__/SettingsView.test.ts
git commit -m "feat(computer): add computer-control settings card"
```

---

### Task 8: 端到端验证

**Files:** 无新增。

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && $env:PYTHONPATH="src"; pytest -q -p no:cacheprovider`
Expected: 全绿

- [ ] **Step 2: 前端全量测试 + 类型检查**

Run: `cd frontend && node node_modules/vitest/vitest.mjs run && node node_modules/vue-tsc/bin/vue-tsc.js --noEmit`
Expected: 全绿

- [ ] **Step 3: 手动冒烟**

重启后端后：在对话里让 agent 调用 `fs_read`（读一个文件）、`run_cmd`（先低危命令自动放、再高危命令走审批）、`sys_info`，确认审批弹窗正常呈现、拒绝/超时无副作用。

- [ ] **Step 4: Commit（如有改动）**

```bash
git add -A
git commit -m "test(computer): e2e verification for computer-control tools"
```
