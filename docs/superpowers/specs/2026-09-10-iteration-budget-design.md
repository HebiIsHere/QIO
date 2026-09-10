# QIO 迭代预算与跑飞护栏设计文档

日期：2026-09-10
状态：设计评审稿（待用户确认后进入实现计划）

## 1. 背景与目标

qio 的 agent 单轮任务迭代上限**硬编码**在 `agent/core/budget.py`：native 模式 5 次、text 模式 3 次，token 预算固定 `DEFAULT_TOKEN_BUDGET = 16_000`。这对"多步调研 + 多轮工具调用 + 归纳"的复杂任务**明显不够用**（主流 agent 如 OpenHands=100、Codex≈128、LangGraph=25）。

本设计：调高默认迭代上限、把 token 闸改为按输出估算、做成可配置、在耗尽时给"继续"出路，并加"跑飞护栏"防止上限调高后陷入无效重试。

### 目标

- 默认迭代：**native 5→128**、**text 3→64**。
- token 闸改为累计**输出 token**（`completion_tokens`），预算 = 迭代上限 × 每轮平均输出。
- 迭代上限 / token 预算**可配置**（设置页 + API）。
- 预算耗尽时给用户**「继续」/「停止」**出路，而非静默结束。
- 加**跑飞护栏**：监控重复失败，分级 warn/block/halt。
- 迭代上限或 token 耗尽时，前端显示 `已用 x/y` 并可继续。

### 非目标

- **不做上下文压力警告**（用户判断：qio 记忆架构按需注入，不会把大量历史塞进上下文，基本不会溢出）。
- 不改记忆/注入架构。

## 2. 调研结论（依据）

主流 agent 的迭代上限：OpenHands `MAX_ITERATIONS=100`、Codex CLI `max_iterations≈128`、LangGraph `recursion_limit=25`（deep agent 100–2000）、Cline `maxRequests` 10–20、Aider `--max-reflections=3`、Claude Code 每轮工具 ~10–20。

共同范式：**较高步数 + 明确停止信号 + 预算/成本护栏**，几乎全部**可配置**；停止信号靠"无工具调用→正常结束"、"预算耗尽→止损"；跑飞护栏监控 `(tool, args, result)` 重复失败（同调用失败 2 次警告/5 次拦截，同工具失败 3 次警告/8 次 halt）。

qio 现状：`default_iterations` 硬编码 5/3；`_tokens_of` 用 `total_tokens`（输入+输出）累计，导致 16000 的闸可能几轮就触发、远早于迭代上限。

## 3. 迭代与 token 预算（核心变更）

### 3.1 默认值

- `default_iterations`：native **128**、text **64**。
- token 预算：改为按输出估算 → `DEFAULT_OUTPUT_TOKENS_PER_ITER = 400`（首版估值，实现时用实测校准），预算 = `max_iterations × 400`（native 128→51200，text 64→25600）。

### 3.2 token 闸改测输出

`loop._tokens_of` 从 `total_tokens` 改为 **`completion_tokens`**（实测 `usage` 为 OpenAI `model_dump()`，含 `prompt_tokens/completion_tokens/total_tokens`）。这样累计的是"模型每轮真正的输出量"，与"每轮平均输出"语义一致，且不会因输入重复计数而提前卡死。

### 3.3 概念澄清

- token 闸（输出预算）= 防止模型话痨/输出失控。
- 上下文窗口 = 由 qio 记忆架构自然约束（本设计不设压力警告）。

## 4. 预算耗尽时的「继续」出路

当前 `force_continue` 是**构造参数、从未接线**。本设计把它改为**运行时按用户决定续跑**：

- loop 检测到 `budget.exhausted`（迭代或输出 token 触顶）时，**不直接 break**，而是：
1. 发出一个 `BUDGET_EXHAUSTED`（或复用 APPROVAL_REQUIRED 语义）事件，携带 `used/max`、`reason`；
2. **挂起等待**用户决定（复用 `ApprovalService` 的 request/response 挂起模式，新增 `ContinuationService` 或扩展 ApprovalService）；
3. 用户选「继续」→ **提升该预算上限**（例如再追加一批迭代/输出预算）并继续循环；用户选「停止」→ 正常结束，返回已完成部分。
- **超时/无响应** → 视为停止，返回已完成部分（不无限等待）。

### 4.1 关键约束

- "继续"是**同一条 loop 内续跑**，不是重开新回合（`run_turn` 每次新建 loop，无法恢复现场）。
- 挂起期间不得阻塞 SSE 与其他工具；超时兜底。
- 达到上限时必须返回**已完成部分**（`final_content` 不为空则保留），不产生"断篇空响应"。

## 5. 跑飞护栏（Runaway Guard）

监控每轮的 `(tool_name, arguments, ok/result)`：

- **同一调用**（同工具+同参数）失败 **2 次** → 发 WARNING（提示模型换方法）；
- **同一调用**失败 **5 次** → **拦截**该调用（返回失败结果，不再真正执行）；
- **同一工具**反复失败 **3 次** → WARNING；
- **同一工具**反复失败 **8 次** → **halt** 本轮（结束，给出已完成部分 + 原因）。

阈值参考主流的 nanobot 做法。护栏的目标：**让"把上限调到 128"是安全的**——没有它，调高上限更容易跑飞。

### 5.1 实现位置

- 在 `AgentLoop` 内维护一个小计数器（`dict[(tool, args_hash)] -> fail_count`、`dict[tool] -> fail_count`），每轮 tool 执行后更新。
- 达阈值时通过现有 WARNING 事件通道通知，并在拦截/halt 时短路。

## 6. 配置（SettingsStore + API）

- `loop.max_iterations`：用户可配的**主对话迭代上限**，默认 128。若主模型走 text 模式，未显式配置时用 `default_iterations(text)=64` 作为默认；用户一旦在设置里配置了值，则该值同时用于两种模式（显式配置优先）。
- `loop.output_token_budget`：默认 = `default_iterations(当前模式) × 400`（native 51200 / text 25600）；用户可配。
- 后端新增 `GET/PUT /api/settings/loop`。
- 前端设置页「偏好」加「对话深度」卡片（迭代上限 + 输出预算）。

## 7. 前端

- 预算耗尽事件 → 消息流出现一个**「继续」/「停止」**的操作条（复用 qio 的提示条/工具卡风格）。
- 显示 `已用 x/y 迭代`、输出 token 用量。
- 遵循 qio 设计风格：`var(--*)`、三声部字体。

## 8. 测试策略

### 后端（pytest）

- `test_budget.py`（扩展）：默认 128/64；token 闸用 completion_tokens；预算 = 迭代 × 每轮输出。
- `test_runaway_guard.py`：同调用失败 2/5、同工具失败 3/8 的 warn/block/halt。
- `test_loop_continue.py`：耗尽时挂起 → 继续 → 续跑；超时 → 停止；返回已完成部分。
- `test_settings_loop_api.py`：`GET/PUT /api/settings/loop`。

### 前端（vitest + vue-tsc）

- 「继续/停止」操作条的渲染与交互。
- 设置页「对话深度」卡片读写。
- `vue-tsc --noEmit` 通过。

## 9. 设计风格约定

- 后端：`IterationBudget` 扩展保持向后兼容；护栏逻辑放 `AgentLoop` 或独立 `agent/core/guard.py`。
- 前端：`var(--*)`、三声部字体、卡片与提示条参照现有风格。

## 10. 已确认决策（2026-09-10）

1. 默认迭代：native **128**、text **64**。
2. token 闸：**C 方案**——改测输出 token，预算 = 迭代上限 × 每轮平均输出；双闸解耦（迭代管轮数、token 管输出）。
3. 可配置：认可（设置页 + API）。
4. 耗尽给「继续」：认可。
5. 跑飞护栏：本设计一起做。
6. 上下文压力警告：**不做**（用户判断 qio 记忆架构不会溢出）。

确认后进入 `writing-plans`。
