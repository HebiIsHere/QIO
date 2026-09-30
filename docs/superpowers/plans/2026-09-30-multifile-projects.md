# 多文件项目与依赖契约 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让开发类工具能做「一个项目」而不是「一段代码」：工作区里可以有子目录与多个模块，沙箱按项目跑，提交后注册的工具仍然带着这些模块。

**Architecture:** 工作区是唯一事实源。`dev_*` 文件工具接受相对路径（拒绝绝对路径、`..`、保留名，任意深度）；`tool.json` 仍是清单，`code` 是入口；提交与测试时后端**把工作区里的其它文件收进定义**（`ToolDefinition.files`），沙箱执行时把它们写进一次性临时目录并让入口代码能 import 兄弟模块。`ToolDefinition` 随定义 JSON 一起落库，因此**不需要新迁移**。依赖用 `requirements` 声明：运行期缺依赖时如实报 `missing_dependency` 并指出是哪个声明的依赖；「自动装依赖的受管环境」属于下一阶段，本次只做契约与诚实失败。

**Tech Stack:** Python 3.11 + SQLite（无新迁移）+ pytest；worker 协议（`agent/tool_worker.py`）保持「只 import 标准库」。

**Spec:** `docs/superpowers/plans/2026-09-29-tool-dev-spec-phase1.md`（第 1 节第 4 条、第 2 节「项目模型」）

## Global Constraints

- 不经用户批准不执行生成代码（既有闸门继续生效，不因多文件放宽）。
- 工作区文件不得写到工作区之外；保留名（`state.json` / `request.md`）在任何深度都不可写。
- 注册过的工具必须自包含：重启后即使工作区目录被清掉，工具仍能跑（定义里带着它的模块）。
- 不新增第三方依赖、不新增数据库迁移。
- 做不到的事（自动装依赖、资源配额）必须明确失败或明确写成未实现，不静默降级。

---

## File Structure

- `backend/src/agent/tools/dev_workspace.py` — 相对路径校验与子目录读写；`project_files()` 收集项目文件。
- `backend/src/agent/tools/spec.py` — `ToolDefinition.files` / `requirements` 与路径、体积校验。
- `backend/src/agent/tools/sandbox.py` — `execute(..., files=...)`：写进一次性临时目录；docker 分支同语义。
- `backend/src/agent/tool_worker.py` — 把工作目录放进 `sys.path`，让入口能 import 兄弟模块。
- `backend/src/agent/tools/tester.py`、`tools/runtime_tools.py` — 测试与运行都带上项目文件。
- `backend/src/agent/tools/dev_tools.py` — `dev_write_file` 接受相对路径；提交时收项目文件。
- `backend/tests/test_dev_workspace_multifile.py`、`test_tool_project_execution.py`、`test_dev_multifile_submit.py`

## Task 1: 工作区接受相对路径

**Files:** `tools/dev_workspace.py` + `tests/test_dev_workspace_multifile.py`

- [ ] **Step 1: 写失败测试**（子目录读写/枚举、`../` 与绝对路径被拒、任意深度的保留名被拒、摘要覆盖子目录）
- [ ] **Step 2: 跑测试确认失败**：`cd backend; .venv\Scripts\python.exe -m pytest tests/test_dev_workspace_multifile.py -q` → FAIL（`single file names only`）
- [ ] **Step 3: 实现**：`_safe_rel_path()`（拒绝绝对路径 / `..` / 空段 / 非法字符 / 保留名 / 过深）+ `write_file` 建父目录 + `list_files` 递归相对路径
- [ ] **Step 4: 跑测试确认通过** + 既有 `tests/test_dev_tools.py` 不回归
- [ ] **Step 5: 提交**

## Task 2: 沙箱按项目执行

**Files:** `tools/sandbox.py`、`agent/tool_worker.py` + `tests/test_tool_project_execution.py`

- [ ] **Step 1: 写失败测试**（入口 import 兄弟模块能跑通；`files` 里的越界路径被拒；文件不进工作区/不污染后端进程）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**：`execute(..., files=None)`；子路径写进临时目录；worker `sys.path.insert(0, os.getcwd())`；docker 分支把文件写进容器 `/tmp/project`
- [ ] **Step 4: 跑测试确认通过**（含既有 `test_sandbox_worker.py`、`test_tool_worker.py`）
- [ ] **Step 5: 提交**

## Task 3: 定义带上项目文件与依赖契约

**Files:** `tools/spec.py`、`tools/tester.py`、`tools/runtime_tools.py`、`tools/dev_tools.py` + `tests/test_dev_multifile_submit.py`

- [ ] **Step 1: 写失败测试**（`files` 路径/体积校验；测试器把文件带进沙箱；注册的 CodeTool 重启后仍能 import 兄弟模块；提交时工作区文件收进定义；`requirements` 缺依赖时报 `missing_dependency` 并点名）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现** + `tool.json` 模板补 `requirements`
- [ ] **Step 4: 跑测试确认通过**（含 `test_tool_lifecycle.py`、`test_dev_tools.py`、`test_tool_restore.py`）
- [ ] **Step 5: 提交并同步 `docs/status.md`**
