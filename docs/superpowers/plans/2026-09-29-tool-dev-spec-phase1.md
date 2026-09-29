# QIO 工具开发规范与可靠性修复 · 第一阶段 实施计划

**Goal:** 让 QIO 的开发类工具遵循一条「可被程序校验」的权威规范，产出「声明条件下经过验证、可恢复、可维护」的工具；同时修掉几处会掩盖真实运行/状态/检索缺陷的可靠性问题。

**Range:** 本计划只做第一阶段。整体设计同时给出后续（网页/桌面操作、定时监听、长期任务、协作）如何复用同一套项目、权限、状态、版本、取消与恢复协议；这些扩展本次不实现，接口不包装成已完成能力。

**Tech Stack:** Python 3.12 / pytest / SQLite（顺序迁移，只追加）/ Vue 3 + vitest（仅必要处）。

## 0. 当前代码事实（核对于本次会话，未改动源码前）

基线：`main`（提交 `5162662`，即本提示词给出的核对基线）。以下结论以实际代码为准：

> 循环导入那条（`agent.tools.*` 作为首个导入时的 `registry → core.narrative → core.__init__ → loop → registry`）**已由 main 的 `5ab27f2 fix(imports)` 修掉**（改的是 `registry.py` 的局部导入），本阶段不重复处理。

| # | 位置 | 事实 |
| --- | --- | --- |
| 1 | `backend/src/agent/core/loop.py:~632` | 工具结果回填模型时只写 `content=result.content`；`ToolResult.error` 不进入模型消息。失败且 `content` 为空时模型收到空正文。**仍未修复**。 |
| 2 | `backend/src/agent/tools/sandbox.py` | 子进程用 `sys.executable -c`；冻结后该路径是后端 exe，入口只启动服务。Docker 分支独立、可用时不受影响。**仍未修复**。 |
| 3 | `backend/src/agent/tools/tester.py`、`tools/runtime_tools.py` | 沙箱捕获的 `stderr` 未作为可诊断信息传到上层（tester 只取 `result.error`；CodeTool 失败只回 `result.error`）。**仍未修复**。 |
| 4 | `backend/src/agent/tools/lifecycle.py` | 测试失败提前返回、不产生创建审批；测试执行发生在注册审批之前。需在后续阶段把「测试用隔离数据/模拟资源」与执行授权一起核验。 |
| 5 | `backend/src/agent/tools/dev_workspace.py` | 已有磁盘回填（重启后 `task()` 可用）；缺少对 agent 暴露的任务枚举与完整状态。 |
| 6 | `backend/src/agent/tools/dev_tools.py` | `dev_submit_tool` 仍要求模型再传完整 `definition`，存在重复输出与「工作区文件 / 测试对象 / 提交对象」不一致的风险。 |
| 7 | `backend/src/agent/services/memory_lifecycle.py`、`services/retrieval.py` | 片段记忆索引依赖封存后的摘要成功；开放片段及摘要失败内容缺少独立原文检索路径。 |
| 8 | 单次工具调用 | 已有 `tool_records` 持久化历史、运行状态查询与部分重连恢复；缺的是**开发任务级别**的权威状态。 |
| 9 | `continuation_intents` | 属于历史片段续接，与工具创建审批无关。 |
| 10 | `docs/superpowers/plans/2026-09-24-tool-failure-hardening.md` | 已完成的可靠性加固（默认工作区目录、开发工作区跨进程存活等）保留，不回退。 |

> 本机日志里的失败次数、耗时、token、数据库内容属于用户提供的诊断证据，未经本机复核不写成「亲自复现」。

## 1. 第一阶段修改范围

按依赖顺序：

1. **统一失败反馈**（`core/tool_feedback.py` + `core/loop.py`）：公共执行层把工具结果交给模型时，至少带上「成功与否 / 错误类别 / 简短原因 / 是否可重试 / call_id / 诊断详情（脱敏限长）」。原生与文本模式同一条路径。
2. **诊断透传**（`tools/sandbox.py`、`tools/tester.py`、`tools/runtime_tools.py`）：`SandboxResult` 增加错误类别，`stderr`/退出码进入 tester 细节与 CodeTool 错误。
3. **专用执行器选择**（`tools/executor_env.py` + `tools/sandbox.py`）：显式解析工具解释器（`QIO_TOOL_PYTHON` → 随包分发的解释器 → 开发态当前解释器），冻结态绝不把后端 exe 当解释器；解析失败给出明确环境诊断而不是静默用 PATH 里的未知 Python。
4. **开发工作区为权威**（`tools/dev_workspace.py`、`tools/dev_tools.py`）：`dev_submit_tool` 不再要求重复传 `definition`，改为读取工作区 `tool.json` 并绑定内容摘要；新增任务枚举 `dev_list_tasks` 与状态字段（提交、测试、摘要）。
5. **单一权威开发规范**（`prompts.py` / 规范文本）：把开发规范拆成分节，按当前步骤注入，避免每轮重复注入整份长文。
6. **文档**：`docs/status.md`、本计划、必要运行说明。

## 2. 整体扩展设计（后续阶段复用点，本次不实现）

- **项目模型**：`ToolProject`（id、version、入口、契约、平台、依赖锁定、资源清单、申请能力、凭据引用、测试与验收用例、验证记录）。第一阶段的 workspace 只是该模型的最小实现，后续网页/桌面/定时工具都落到同一张项目表与同一套版本/摘要协议。
- **执行器**：`executor_env` 预留 `kind`（`python` / 未来 `browser` / `desktop` / `container`），第一阶段的 Python 执行器是它的第一个实现。
- **权限**：沿用 `tools/policy.py` 的能力分级 + `ApprovalService`。范围授权（目录/服务/凭据引用）作为 policy 的字段扩展，不新造体系。
- **状态/版本**：开发任务表保存「项目 + 版本 + 阶段 + 最新测试证据 + 授权关联 + 阻碍 + 后续步骤」；审批、注册、启用都绑定同一 `(project, version, digest)`。
- **取消/恢复**：复用 `core/tool_state.py` 与 `turn` 的持久化；重启后只提示、不自动执行。
- **协作**：子 agent 与主 agent 共用同一项目表与执行器契约。

## 3. 数据迁移

本阶段**不新增数据库迁移**：开发任务状态先落在工作区目录内的 `state.json`（与已有 `ws_*` 目录同源，重启可回填）。等第二阶段引入项目表时再加一条迁移，只追加、不改历史。

## 4. 授权与验证状态

- 授权沿用现有 `ApprovalService` + `policy_fingerprint`；`dev_submit` 额外把工作区内容摘要写进审批载荷与工作区状态，作为「测试/审批/注册绑定同一版本」的第一步。
- 验证状态区分「待实际验证」与「可用」：本阶段先把**模拟测试通过**与**状态记录**落库到工作区，真实环境验证仍按既有 `verification` 语义处理。

## 5. 恢复策略

- 工作区：磁盘回填（已有）+ 新增 `state.json`（提交/测试/摘要）读回；缺 `state.json` 时标记为「未知」，不因目录存在就推断测试通过。
- 恢复只展示与查询，不自动执行、不自动弹审批。

## 6. 验收计划

| 场景 | 验收 |
| --- | --- |
| 工具失败且 content 为空 | 模型收到失败事实、类别、原因、call_id；原生/文本共用路径 |
| 生成代码抛已知异常 | 错误类别 + 脱敏、限长的 stderr 诊断 |
| 冻结态子进程 | 解析器拒绝把后端 exe 当解释器，报明确环境问题 |
| 提交不一致 | `dev_submit` 以工作区 `tool.json` 为准，拒绝不一致的旧参数 |
| 任务枚举/状态 | `dev_list_tasks` 返回任务及阶段、测试、摘要，重启后仍可查 |
| 规范可执行 | 规范分节注入，创建任务时给「当前步骤」规则 |

**NOT RUN（当前环境无条件执行，如实标注）**：真实冻结产物（安装包）执行链、Windows 无系统 Python/无 Docker 的真机验证、真实外部服务验证、前端界面验收。这些不因本计划完成而宣称已验收。
