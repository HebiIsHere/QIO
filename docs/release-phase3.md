# QIO 第三阶段验收报告：能力完整性、状态表达与可达性

日期：2026-09-15　范围：第三阶段（能力完整性 / 状态可理解性 / 操作可达性）。
执行计划：`docs/superpowers/plans/2026-09-15-capability-completeness-phase3.md`。

验证口径（命令与结果都在本文第 8~9 节）：

```powershell
cd backend; uv run --frozen pytest -q      # 784 collected，全绿（1 skipped）
cd ..\frontend; npx vue-tsc --noEmit       # exit 0
cd ..\frontend; npm test                   # 47 files / 454 tests passed
python scripts/check_docs.py               # 通过
python scripts/verify_phase3.py --live-turn --live-knowledge   # 真实运行验收
```

---

## 1. 第三阶段修改摘要

| 分类 | 改了什么 |
| --- | --- |
| Memory | Fragment 封块改成真正的「轮」（`fragment.max_turns`，一条 user = 一轮，工具消息不计入，半轮不封块）；Topic Detail 补齐摘要 / 关键词 / 最近活动 / 真实消息数；前端新增「片段 → 查看原文（按需 + 分页）→ 从这里继续」的完整浏览链路 |
| Knowledge | 高影响候选在**回答完成之后**进入对话（`KNOWLEDGE_CANDIDATE` + 保存 / 修改 / 忽略卡）；新增 `POST /api/knowledge/{id}/ignore`；忽略后同类候选进入 30 分钟冷却，不再重复打扰；Knowledge Panel 仍是浏览 / 修正 / 归档入口，但不再是唯一确认入口 |
| Tool Creation | 同一张卡按 `group_id` 原地推进九个阶段（提案 → 构建 → 测试 → 等待确认 → 启用 → 已创建 / 失败），不再产生一串互不相关的工具卡；失败都带可理解原因 |
| Tool Runtime | `TOOL_START` 立刻出现「运行中」的工具卡；`TOOL_END` 按 `call_id` 原地更新并带上耗时；失败结论写在卡面上，完整输出折叠 |
| Agent Status | 全局只保留一句整体状态（正在处理 / 正在使用工具 / 等待你确认 / 正在处理独立任务 / 正在整理独立任务的结果）；细节留在各自卡片；系统轮（`notify`）不再被当成用户消息轮 |
| Capability | `CAPABILITY` 只在模式变化时发；进入兼容文本模式那一次发 `FALLBACK`，一次性、低干扰、可忽略 |
| Credential | 凭据不可用 / 暂停 / 失效会进协议；前端只显示人话（不含 `key_id`）；恢复后提示自动消失 |
| Events | 事件集合前后端完全一致（守卫测试）；删除 `MEMORY_INJECT`；新增 `TOOL_CREATE_STATUS` / `KNOWLEDGE_CANDIDATE`；补齐 `TOOL_START` / `APPROVAL_RESULT` / `CREDENTIAL_STATUS` / `FALLBACK` 的前端消费 |
| Subagent | 独立任务有独立卡片（开始 / 进行中 / 已完成 / 失败），按 `task_id` 原地更新，只显示目标、状态、结果，不暴露内部推理 |
| Approval | 工具执行与文件 / 命令 / 进程类审批都给出行为化描述（想做什么 / 会访问什么 / 影响 / 一次性还是长期）；内部动作名与策略指纹只在默认折叠的高级详情里 |
| Knowledge / Entity Feedback | 统一走 `useActionFeedback`：进行中 / 成功（短暂）/ 失败（保留可重试）；修掉了 `PlanetView` 知识修正、归档失败只 `console.error` 的静默失败；全局 Toast 收回到卡片内 |
| Feature Reachability | 补上「撤销密钥」入口（后端有、界面没有）；`GET /api/graph/positions`、`POST /api/turns/cancel` 等明确标注为内部能力；完整清单见 `architecture.md` 第 12.3 节 |

---

## 2. 原有问题、根本原因与修改方式

| 问题 | 根本原因 | 修改方式 |
| --- | --- | --- |
| 设置页写「10 轮」，实际只有 5 轮 | 后端键是 `fragment.max_messages`，按**消息条数**封块（一轮 = user + assistant 两条） | `FragmentManager(max_turns=...)` 只数 `role='user'`；最后一条是 user 时不封块；键正名为 `fragment.max_turns`，旧键只作一次性迁移回退 |
| `TOOL_START` 完全没有被消费 | 前端没有分支，用户要等工具跑完才看到卡 | 后端补 `call_id` / `presentation` / `duration_ms`；前端按 `call_id` 建卡与原地更新 |
| 同一个工具出现重复卡片 | `TOOL_END` 每次都 push 一条新消息 | 按 `call_id` 就地更新（找不到才补一张，保证重连 / 丢帧不丢结果） |
| 工具创建是一串互不相关的卡 | 没有「一次创建流程」的身份 | `TOOL_CREATE_STATUS` 以开发工作区 id 为 `group_id`，前端同一张卡推进九阶段 |
| 高影响知识只能自己去 Knowledge Panel 发现 | 候选停在 `pending_review`，没有任何对话内入口 | 回答完成后发 `KNOWLEDGE_CANDIDATE`，前端只在 `TURN_END` 之后显示候选卡 |
| 忽略后又被问一遍近乎同一件事 | 只按字符串去重，而模型每次换说法 | 忽略时记录该**类别**的冷却起点（30 分钟），冷却期内同类候选不再弹到对话（仍留在知识面板） |
| 普通用户看到 `pending_review` / `run_shell` / `policy fingerprint` 这类内部词 | 界面直接渲染内部枚举与原始参数 | 状态中文化；审批行为化；内部字段只进高级详情；`MEMORY_INJECT` 事件删除（改看 `/debug` 的 Trace `injection`） |
| 知识修正 / 归档失败什么都不显示 | 只有 `console.error` | 统一 `useActionFeedback`：失败留在对应条目并可重试 |
| 「凭据已撤销」状态与筛选都在，但没有撤销入口 | 后端有 `POST /api/credentials/{id}/revoke`，前端没有 UI | 凭据卡新增「撤销密钥」（确认文案说明与「删除」的区别） |
| 审批 payload 只有工具名与参数 | 审批只传 `{tool, arguments}` | `describe_tool_call` / `describe_computer_action` 产出 `description / access / capabilities / scope`（原始字段保留） |

---

## 3. Event Protocol（最终事件表）

| 事件 | 产生方 | 消费方 | 用户可见 | 用途 |
| --- | --- | --- | --- | --- |
| `TURN_START` | `core/turn.py::TurnManager` | `stores/events.ts` | 是（全局轻状态） | 一轮开始；`notify=true` 为系统轮 |
| `TURN_END` | 同上（`finally`，恰好一次） | `stores/events.ts` | 是 | 唯一终态 + 最终回答唯一权威来源 |
| `TURN_QUEUE` | 同上 | `QueueChip.vue` | 是（有排队时） | 排队 / 取消快照 |
| `ASSISTANT` | `core/loop.py` | `stores/events.ts` | 是 | 流式正文 / 工具前中间话 |
| `TOOL_START` | `core/loop.py` | 工具卡 | 是 | 开始执行（`call_id` + `presentation`） |
| `TOOL_END` | `core/loop.py` | 同一张工具卡 | 是 | 结果 / 失败原因 / 耗时（`call_id`） |
| `SUBAGENT_STATUS` | `tools/task_manager.py` | 独立任务卡 | 是 | 独立任务 queued/running/done/failed |
| `TOOL_CREATE_STATUS` | `tools/dev_tools.py`、`tools/lifecycle.py` | 工具创建卡 | 是 | 同 `group_id` 的阶段推进 |
| `KNOWLEDGE_CANDIDATE` | `services/memory_lifecycle.py` + turn 收尾 | 对话内确认卡 | 是（回答完成后） | 高影响知识的保存 / 修改 / 忽略 |
| `APPROVAL_REQUIRED` | `tools/approval.py` | `stores/approvals.ts` | 是 | 需要用户决定的操作 |
| `APPROVAL_RESULT` | `tools/approval.py` | `stores/approvals.ts` | 是（状态收敛） | 授权结局（单次使用） |
| `CAPABILITY` | `services/app.py`（模式变化时） | `stores/events.ts` | 否 | 适配档位 |
| `FALLBACK` | `services/app.py`（进入兼容模式那一次） | 一次性轻提示 | 是（仅降级时） | 能力降级说明 |
| `CREDENTIAL_STATUS` | `api/server.py` + `turn_orchestrator` | `stores/events.ts` | 仅当阻止功能 | 凭据可用性（不含内部标识） |
| `ANCHOR` | `services/app.py` | `stores/events.ts` | 是（话题行） | 当前位置变化 |
| `TOPIC_SWITCH_SUGGESTED` | `services/turn_orchestrator.py` | `TopicSwitchPrompt.vue` | 是 | 推测切换待确认 |
| `USAGE` | `core/loop.py` | `stores/events.ts` | 否（仅 Developer Mode） | 单轮用量 |
| `WARNING` / `ERROR` | `services/app.py`、`core/loop.py` | `ConversationView.vue` | 是 | 提示 / 出错（`ERROR` 不结束 turn） |
| ~~`MEMORY_INJECT`~~ | — | — | **已删除** | 内部机制；开发者改看 `/api/traces/{turn_id}.injection` |

守卫测试 `backend/tests/test_event_protocol.py`：前后端事件集合必须完全相等、每个事件都要有生产者、都必须被前端消费、已删除事件不得残留。

---

## 4. Tool Creation（提案 → 完成）

| 阶段 | `phase` | 界面表现（同一张卡） |
| --- | --- | --- |
| 提案 | `proposal` | 卡片出现，说明「已收到创建需求」，进程点停在第 1 步 |
| 构建 | `building` | 「正在构建」+ 已写入文件数（不含路径与源码） |
| 测试 | `testing` → `testing_passed` / `testing_failed` | 「正在测试」→「测试通过」/「测试失败」 |
| 权限 | 由审批卡表达 | 需要联网 / 文件 / 命令 / 凭据时，审批卡列出具体访问清单 |
| 审批 | `waiting_approval` | 卡片显示「等待你的确认」（用户此刻要看的其实是审批卡） |
| 注册 | `registering` | 「正在启用」 |
| 完成 | `ready` | 「已创建」+「现在可以使用了」（不弹大型 Toast） |
| 失败 | `failed` | 「创建没有完成：<人话原因>」+「可以让 QIO 继续修」 |

流程里的开发工具调用（`create_tool` / `dev_write_file` / `dev_run_tests` /
`dev_submit_tool`）**不再各出一张普通工具卡**：进度汇总到同一张创建卡，失败原因也回写到它。

---

## 5. 用户可见状态

- 一轮的整体情况：正在处理 / 正在生成 / 正在使用工具 / 等待你确认 / 正在处理独立任务 / 正在整理独立任务的结果（只一句，不堆事件名）。
- 工具卡：运行中 / 完成 / 失败 + 耗时 + 一行失败结论。
- 独立任务卡：开始 / 进行中 / 已完成 / 失败 + 任务目标 + 最终结果。
- 工具创建卡：九个阶段 + 失败原因 + 「现在可以使用了」。
- 高影响知识候选卡：保存 / 修改 / 忽略 + 进行中 / 失败（可重试）。
- 审批卡：想做什么 / 会访问什么 / 会改变什么 / 为什么需要 / 授权范围（仅这一次 / 长期生效）。
- 凭据状态：没有可用凭据 / 凭据已暂停 / 凭据已失效（都带「去哪里处理」）。
- 知识 / 实体操作：进行中 / 已保存（短暂）/ 失败（保留 + 可重试）。
- 话题目录：标题、一句摘要、最近活动 / 片段数 / 消息数、少量关键词；片段时间 / 摘要 / 消息数。

## 6. 内部状态（只进 Developer Mode / 不显示）

- `MEMORY_INJECT`（已删除）：本轮用了哪些 Fragment / Knowledge / Entity → `/debug` 单轮详情的 `injection`。
- `capability fingerprint`、`policy hash`、`sandbox profile`、内部动作名（`run_shell`）、沙箱判定（`danger`）：审批卡默认折叠的「高级详情」。
- `credential id` / keychain identifier / endpoint resolver / provider adapter：不显示（凭据页只显示用户填写的备注与掩码）。
- `USAGE`（token / 迭代 / 工具计数）：Developer Mode 才显示。
- 内部事件名、内部状态机、检索得分、Chain of Thought / hidden reasoning / system prompt：任何 UI 都不显示。

## 7. Capability Reachability

完整清单在 `docs/architecture.md` 第 12.3 节。本阶段处理的三类：

| 类别 | 例子 | 处理 |
| --- | --- | --- |
| 有后端、无入口、有合理触发场景 | 撤销凭据密钥 | 补直接入口（凭据卡「撤销密钥」） |
| 有后端、无入口、无合理场景 | `GET /api/graph/positions`、`POST /api/turns/cancel` | 明确标为内部能力（前端按 `turn_id` 取消；星球改用 overview/browse） |
| 有后端、场景天然存在但用户看不懂 | 工具执行审批、工具创建流程、独立任务 | 做成卡片 + 行为化文案 + 同一张卡原地更新 |

## 8. Tests

| 测试 | 目的 | 结果 |
| --- | --- | --- |
| `backend/tests/test_event_protocol.py` | 事件集合前后端相等、每个事件都有生产者与消费者、删除的事件无残留 | 通过 |
| `backend/tests/test_fragment_turn_semantics.py` | 10 轮 = 20 条消息才封块、工具消息不计轮、半轮不封块 | 通过 |
| `backend/tests/test_tool_event_payload.py` | `TOOL_START` / `TOOL_END` 共享 `call_id`、`TOOL_END` 带 `duration_ms` 与失败原因 | 通过 |
| `backend/tests/test_capability_events.py` | native 不发降级、text 只发一次 `FALLBACK`、模式未变不发 `CAPABILITY` | 通过 |
| `backend/tests/test_credential_status_events.py` | 无凭据 → `CREDENTIAL_STATUS(unavailable)` 与 `WARNING` 并存且不含 `key_id` | 通过 |
| `backend/tests/test_knowledge_candidate_event.py` | 只有高影响候选进对话、回答完成后才发、忽略后（含换说法）不再打扰 | 通过 |
| `backend/tests/test_tool_create_events.py` | 同一 `group_id` 一张卡、九阶段、失败可理解 | 通过 |
| `backend/tests/test_approval_present.py` | 文件 / 命令 / 进程 / 工具注册审批都有行为化描述与授权范围 | 通过 |
| `backend/tests/test_topic_detail_fields.py` | 详情层有摘要 / 关键词 / 最近活动 / 消息数，且不内联原文 | 通过 |
| `frontend/src/stores/__tests__/phase3Cards.test.ts` | `TOOL_START→TOOL_END` 同一张卡、独立任务按 `task_id`、创建卡按 `group_id`（含把开发工具调用折叠进创建卡、失败回写原因）、候选只在 `TURN_END` 后出现、凭据/降级提示不含内部标识、notify 轮不清排队标记 | 通过 |
| `frontend/src/composables/__tests__/useActionFeedback.test.ts` | 进行中 / 成功短暂 / 失败保留可重试 / 防重复提交 | 通过 |
| `frontend/src/views/__tests__/PlanetView.test.ts`（新增 7 例） | 详情分层、原文按需分页、只读提示、从这里继续、状态中文化、知识修正失败可见可重试 | 通过 |
| `frontend/src/components/__tests__/ApprovalModal.test.ts`（新增 4 例） | 授权范围（仅这一次 / 长期生效）、具体访问清单、首屏无内部术语 | 通过 |
| 全量后端 / 前端 / 文档 | 见开头命令 | 后端 784 collected 全绿（1 skipped）、前端 454 passed、`vue-tsc` exit 0、`check_docs.py` 通过 |

## 9. Manual Verification（真实运行）

工具：`scripts/verify_phase3.py`（对着**真实运行的 uvicorn** 跑 HTTP + SSE，不是进程内调用）。

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| 1 Memory 浏览 | 部分通过 | 真实运行验证了详情层字段（P2）、原文分页与「继续读取」无重复（P3）、`/api/fragments/{id}/messages` 页大小与 `total`；「进入话题 → 展开原文 → 从片段继续」的**人工点选链路未跑**（本机浏览器工具不可用），该链路靠 `PlanetView.test.ts` 的 7 条组件测试覆盖 |
| 2 高影响 Knowledge | 通过 | 真实一轮（临时把封块阈值降到 1 轮）→ 回答完成 → 片段封块 → 提炼 → `KNOWLEDGE_CANDIDATE` 出现在协议里（`category=user_profile`）；忽略后再跑同一句话，第二轮候选事件 **0 条** |
| 3 普通 Tool | 通过 | 真实一轮里 `TOOL_START` 与 `TOOL_END` 各 1 条、`call_id` 配对、`duration_ms=308`（含真实审批链路那次 308ms） |
| 4 Tool Creation | **未运行** | 九阶段、同一张卡、失败原因有自动化测试；「真实模型提议 → 真实沙箱测试 → 两次审批 → 注册 → 立刻可用」的完整链路没跑通（第一次真实运行里模型确实启动了 `dev_run_tests`，但那是在修复 `call_id` 之前，未走完） |
| 5 Fallback | **未运行** | 本机没有「不支持原生工具调用」的可用模型；`FALLBACK` 的语义（native 不提示、text 只提示一次）有测试 |
| 6 Credential Failure | 部分通过 | 真实运行验证了 `CREDENTIAL_STATUS(unavailable)` 的线上格式与前端映射（人话、不含 `key_id`）；「让某专项凭据不可用」的真实场景未跑 |
| 7 Subagent | **未运行** | 独立任务卡的 queued/running/done/failed 有测试；分钟级真实子任务未跑 |
| 8 Approval | 通过 | 真实一轮触发 `kind=computer` 审批：`description="想运行一条 shell 命令"`、`access=[...]`、`scope=once`；脚本拒绝后工具返回「未执行」，turn 正常收尾（`TURN_END status=completed`，工具耗时 308ms） |
| 9 Knowledge / Entity 修改 | 部分通过 | 进行中 / 成功 / 失败 + 重试有组件测试（PlanetView、KnowledgePanel、EntityPanel）；真实手动编辑未跑 |

## 10. 剩余问题（如实列出）

1. **视觉验证未运行**：本机 Computer Use 浏览器不可用（`Codex auth token is unavailable`），所以 spec 第 108 节要求的目视检查（工具卡是否太吵、审批是否过于技术化、候选卡是否打断回答、独立任务是否过度占空间、Topic Detail 是否像后台管理页、状态文字与徽章是否过多）**没有做人眼确认**。替代证据只有 DOM 级断言（首屏不出现内部术语、每类卡片只有一张、状态文案为中文）。
2. **工具创建完整链路未跑**：真实沙箱 + 两次审批 + 注册 + 「立刻可用」没有端到端跑通。
3. **降级提示未在真实 provider 上触发**：需要一个不支持原生工具调用的模型。
4. **子 agent 长任务未验证**：并发上限、长任务等待、完成通知只跑了自动化测试。
5. **触屏与窄窗口下的新卡片未检查**：工具创建卡、候选卡、原文分页都是新界面元素。
6. **同类冷却的取舍**：忽略某类高影响知识后 30 分钟内不再弹同类候选（候选仍留在知识面板的待确认列表里）。这是为了挡住「换成说法又问一遍」，代价是这段时间内同类新知识不会主动出现。
7. **默认封块粒度变粗**：`fragment.max_turns` 默认 10 轮 = 最多 20 条消息（修复前 10 条消息就封块）。旧值通过一次性迁移沿用，用户若想更细可以在设置里调到 1–30。
8. **本次真实运行产生的数据**：`%TEMP%\qio-e2e` 数据域里新增了若干对话消息、知识条目（含 1 条被忽略的候选）与一次工具开发工作区；`fragment_max_turns` 已恢复为运行前的值。
9. **Tauri 打包环境未运行**：桌面壳、生产 CSP、随机端口 + 令牌交接仍是既有验证范围（第一阶段），本阶段未重跑。
