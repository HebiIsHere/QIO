# W2b 报告：审批执行器 + app.py 执行边界接线（集成期补口）

- Worktree：`D:\qio-dev\qio-r9-w2b`；分支：`r9/w2b-exec`（只提交本分支；未 push）
- 起点：从集成树 `r9/main` 拉出（已含 W1+W2+W3core+W5）；随后按 Lead 指示把主树
  `git merge r9/main` 合入本分支（fast-forward 到 `09e2c9df`，含 W3 回收站/附件迁移/授权适配 + W4/W5）。
- 提交：
  - `088e4f5b` W2b 里程碑：app.py 两处 SandboxExecutor 接线 + approved_operations 执行器 + AgentScope 联调 + 测试
  - 本报告所在提交：附件审批执行器（attachment.write_back / attachment.recycle / attachment.read）+ 真实回收站测试 + 报告
- 契约：`docs/r9-contracts.md`（冻结）；本文区分 **已实现 / 已运行验证 / 仅源码检查 / 未完成边界**。

---

## 一、已实现

### 1. services/app.py：执行层边界真正接线（缺口 1）

- 新增 `_make_tool_sandbox()`，两处都使用同一套注入：
  - `self.tool_sandbox`（开发测试/复测/注册后执行共用）；
  - `_restore_tools()` 里恢复自建工具用的 `sandbox`。
- 注入内容：
  ```python
  SandboxExecutor(
      mode_provider=lambda: self.authorization.mode().value,
      security=self.authorization,
      protected_scope_provider=lambda: [
          self.computer.root(), *self._internal_data_paths()
      ],
  )
  ```
  于是 read_only 拒绝任意脚本、restricted 强制 Low IL、自动放行执行的事后快照核验
  在应用内真正生效（而不是只存在于工具类里）。

### 2. 审批通过后的真实执行器（缺口 2）

新增 `backend/src/agent/tools/approved_operations.py`，并在 AppContext 初始化时
`register_approved_operations(self.operations, ...)` 注册（app.py）。

| operation kind | 行为 |
| --- | --- |
| `fs.write` | 重核对对象身份 + 提交审批时的 `base_content_sha256` + 待写内容摘要；不符 → 不写、`detail.invalidated=true`、failed 落库；相等才原子写（临时文件 + os.replace） |
| `fs.patch` | 重核对身份/基内容摘要；读取当前内容确认 old 仍在 → 替换 → 再核对新内容摘要 → 原子写 |
| `fs.delete` | 重核对身份后调用 `agent/services/recycle_bin.send_to_recycle_bin`（W3 已合入）；**只在回收站成功时报告成功**；失败/不可用保留文件并如实说明，绝不 unlink |
| `proc.run_program` / `proc.shell` / `proc.kill` | 复用 cmd_tools 的 `run()` 真实执行路径（超时/输出截断/进程树收尾不另写）；`_ApprovedReplayGateway` 不再重复弹审批，但 `mode()` 仍返回当前真实模式 → 只读执行层兜底照常生效 |
| `subagent.create` | 按审批单快照的 AgentScope 子集重建子智能体（`SubagentTool.execute_after_approval`）；不重新申请、不扩大范围 |
| `attachment.write_back` | 校验 base64/sha256/身份/旧内容哈希后调用 W3 `AttachmentService._write_back_source_now`（W3 内部再核对 identity+hash 并原子写、更新登记） |
| `attachment.recycle` | 对每条材料重核对身份 → W3 `recycle_bin.send_to_recycle_bin`；部分失败保留文件并如实报告 `deleted_permanently=false` |
| `attachment.read` | 走 W3 `resolve_source`（唯一解析入口）读取源文件，按 offset/limit 返回预览（≤20000 字符，经 redact） |
| `dep.install` / `net.egress` / `internal.data_write` | 当前没有独立执行器：显式注册「不可用」处理器 + 原因，不允许静默丢单（与任务要求一致） |
| `tool.invoke` | **审计结论**：app.py 自己的 `_execute_approved_tool` 用 `registry.execute(call, approval_id=...)` 重跑完整工具管线；registry 的 `_approved_execution` 标记会跳过重复审批，参数校验/执行/输出校验/事件/审计照常 —— 与「批准后重跑管线」一致，本次未改 |

执行前重核对统一走审批执行器 `ExecutionContext.authorize_started()`（模式快照），
executor 内部再做对象级核对；任何不符都返回 failed + `invalidated` 事实并落库。

### 3. AgentScope 联调（Lead 指出的 2 条失败）

- `security_gate.make_agent_scope` 按 W1 权威字段适配：
  `parent_instruction_id / grants / depth / purpose / label`（不再用契约说明里的临时字段）。
- `create_child_scope`：优先用 W1 `AuthorizationEngine.child_scope`；**单层限制**
  （父上下文已在子智能体里 depth>=1 → 不允许再建下一层，嵌套由主智能体统一申请）。
- `scope_summary` 兼容权威字段（grants → parent_grant_ids 摘要）。
- `test_subagent_scope.py` 改用 `security_gate.make_agent_scope` / 权威字段构造，
  新增「真联调」用例：`with_instruction` + 显式 grant → `SubagentTool.run` →
  子循环内 `engine.authorize`：
  - 项目授权范围内 WRITE = `allow`；
  - 项目外 WRITE = `approve`（必须回到主智能体申请）；
  - 子 scope.grants 恰好等于父指令 active_grants，结束后 ContextVar 恢复。
- `fs_tools` 提交审批时新增 `base_content_sha256`（文件身份体积+file_id 在编辑后不变，
  只有内容摘要能挡住「审批期间被改过仍然覆盖」）。

---

## 二、已运行验证（命令与结果）

前置（本 worktree 首次）：

```
cd D:\qio-dev\qio-r9-w2b\backend
uv sync --frozen --extra dev        # exit 0
```

合并主树（Lead 指示的一次辅助合并）：

```
cd D:\qio-dev\qio-r9-w2b
git merge r9/main --no-edit         # fast-forward 到 09e2c9df（含 W3 recycle_bin/attachment_access）
```

1）任务要求的完整定向矩阵（13 个契约测试文件 + test_approved_operations +
test_approval_lifecycle + test_approval_restart + test_app_lifecycle）：

```
uv run --frozen python -m pytest \
  tests/test_fs_containment.py tests/test_shell_boundary.py \
  tests/test_sandbox_executor.py tests/test_sandbox_worker.py tests/test_sandbox_readonly.py \
  tests/test_computer_sandbox.py tests/test_subagent.py tests/test_subagent_integration.py \
  tests/test_subagent_scope.py tests/test_tool_envs.py tests/test_tool_env_integration.py \
  tests/test_tool_env_base_python.py tests/test_tool_isolation.py \
  tests/test_approved_operations.py tests/test_approval_lifecycle.py \
  tests/test_approval_restart.py tests/test_app_lifecycle.py \
  -o addopts='' -q --tb=short
→ 246 passed, 1 warning in 64.75s
```

2）审批执行器专项（含附件真实写回/真实回收站）：

```
uv run --frozen python -m pytest tests/test_approved_operations.py -o addopts='' -q
→ 23 passed in 8.22s
```

覆盖：批准后真实写文件（内容正确、原子、无临时残留）；身份变化/基内容变化不写并
failed+invalidated 落库；批准→执行→completed 落库；重复领取不执行；回收站不可用保留
文件；**真实系统回收站**（Windows SHFileOperation：原路径消失、mechanism 如实）；
附件写回源文件（内容、登记哈希更新）、源文件被改后拒绝、附件读取路径+预览、
回收失败保留文件；proc.run_program/proc.shell 真实执行、proc.kill 非法 pid 失败；
subagent.create 用快照 scope 重建；不可用执行器显式失败。

3）导入冒烟：

```
uv run --frozen python -c "import agent.services.app, agent.tools.approved_operations, agent.tools.subagent_tool, agent.tools.security_gate"
→ imports ok
```

4）回归（W2 补充批，确认接线/执行器改动未破坏开发与工具环境链路）：

```
uv run --frozen python -m pytest tests/test_tool_lifecycle.py tests/test_dev_tools.py \
  tests/test_dev_test_authorization.py tests/test_tool_creation_entrypoints.py \
  tests/test_dev_multifile_submit.py tests/test_tool_policy.py tests/test_executor_env.py \
  tests/test_tool_project_execution.py tests/test_tool_mock_services.py \
  tests/test_dev_authorization_scope.py tests/test_dev_abandon.py tests/test_dev_abandon_persist.py \
  tests/test_tool_container_env.py tests/test_tool_credentials.py -o addopts='' -q
→ 240 passed, 1 skipped（既有 docker 环境跳过）
```

---

## 三、仅源码检查 / 审计（未运行验证）

1. `tool.invoke`：阅读 app.py `_execute_approved_tool` + registry.execute/_approved_execution
   + tools/approval.py `_execute_approved`，确认「批准后重跑完整工具管线、不重复弹审批」
   语义一致；未单写端到端用例（审批执行器的 tool.invoke 路径由 W1 侧既有测试覆盖）。
2. `proc.kill`：只测了非法 pid 的失败路径；**没有**在测试里真杀一个进程（避免误伤宿主/
   其他实例；cmd_tools 自身的进程结束路径由既有测试覆盖）。真实结束进程依赖用户批准后
   `taskkill /f /pid`。
3. `attachment.write_back` 内部走 W3 同步实现（`asyncio.to_thread` 调用），W3 内部会再核对
   identity+content hash 并原子写、更新 attachments 行；本报告只验证到「写回后文件内容与
   登记哈希更新」。
4. `attachment.read` 只把 offset/limit 预览写入审批结果 detail（≤20000 字符，经 redact）；
   不把全文塞进审批历史。原 turn 已经结束时，模型看不到正文（执行器只发通知）——
   这是审批 deferred 执行的产品边界，不是本次引入的缺陷。

---

## 四、未完成边界（诚实说明）

1. **受保护范围含内部运行数据可能误报**：按 Lead 指定
   `protected_scope_provider=[工作区根, *内部数据路径]`；QIO 后台自身会写 DB/日志，
   在某些脚本执行窗口内可能被事后核验当成越界修改（结果会被标 boundary_violation）。
   本次只做了单元级接线验证，未在完整应用里跑「自建工具执行 + 后台持续写库」的时序。
   集成期若出现误报，建议把 DB/journal/日志等热文件从 guard 范围排除，或只保护
   「工作区 + 内部数据目录的结构性文件」，需要 Lead 决策。
2. **真实系统回收站的跨平台验证**：本机 Windows 实测通过（mechanism=windows_recycle_bin）；
   POSIX freedesktop Trash 分支本次未在 Linux 上运行（W3 模块自身有实现）。
3. **attachment.recycle/write_back 未在完整 AppContext 里端到端跑**（api/server.py 创建
   AttachmentService 的装配）：executor 用 `attachments_getter=self._attachment_service()`
   延迟取，单测用真实 AttachmentService(sqlite + tmp data_dir) 覆盖；完整 server 装配的
   一次真实调用建议集成期补一条 E2E。
4. **subagent.create 在后台执行**时没有 parent origin provider，任务记录 origin 可能不含
   当前轮/话题（scope 子集与参数已按审批单快照重建并测试）；通知/原 turn 消息由 W1 执行器
   统一发送。
5. **dep.install / net.egress / internal.data_write**：按要求注册「不可用」处理器并说明
   （没有真实执行器；不允许静默丢单）。
6. **未跑全量 pytest / CI**（纪律：全量由 Lead 跑一次）；未改前端/未跑前端测试。

---

## 五、复现命令汇总

```
cd D:\qio-dev\qio-r9-w2b\backend
uv sync --frozen --extra dev
uv run --frozen python -m pytest tests/test_approved_operations.py -o addopts='' -q
uv run --frozen python -m pytest <13 个契约文件 + approval lifecycle/restart + app_lifecycle> \
  -o addopts='' -q --tb=short
uv run --frozen python -c "import agent.services.app, agent.tools.approved_operations"
```
