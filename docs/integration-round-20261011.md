# 整合轮记录：可靠性线 × 真实模型线（2026-10-11）

> 本文件由主智能体维护，记录**本次整合**的来源、冲突处理、集成缺口修复、验证台账与 main 合并方案。
> 冻结契约（接口与编号）见 `docs/integration-contract-20261011.md`；独立审查见 `docs/integration-review-20261011.md`；
> 定向验证入口与台账见 `docs/verify-ledger-integration-20261011.md`。

## 1. 实际采用的来源 SHA（fetch 后逐一核对）

| 角色 | 分支 | 实际 SHA |
| --- | --- | --- |
| 基线 | `origin/main` | `8fa845255c118a7104b1de8e37298ca745924482` |
| 可靠性来源 | `fix/main-audit-reliability-8fa8452` | `4890870eddfa6f0f15e600bbbeecc03740714ff7` |
| 真实模型来源 | `feat/interactive-real-model` | `7158152d84b0b24cac5156f4862a59e22f204dc4` |
| 整合分支 | `integrate/reliability-interactive-real-model-20261011` | 见 §6 |

三条远端 SHA 与任务书给出的已观察起点**逐一相同**，没有出现「远端已推进」的情况；
`integrate/reliability-interactive-real-model-20261011` 在远端原本**不存在**（先前的
`integrate/interactive-main-20261011` 是另一条线，与本轮无关）。

## 2. 合并与子智能体分工

| 步骤 | 提交 | 来源 | 说明 |
| --- | --- | --- | --- |
| 基线 | `8fa8452` | `origin/main` | `git worktree add -b integrate/… origin/main` |
| 合并可靠性 | `1b9cccd` | `4890870` | `merge --no-ff`：**0 冲突**，未使用 ours/theirs |
| 合并真实模型 | `c6a03b1` | `7158152` | 3 处文本冲突按行为重组（§3），其余自动合并 |
| 冻结契约 | `0ed432f` | Lead | `docs/integration-contract-20261011.md` |
| 整合记录 | `1f36e10` | Lead | 本文件 |
| 合并 A（前端） | `252c437` | `wt/irm-int-a-frontend` `c9d1c23` | 契约 §1.3 前端侧 + F10 隔离修复 |
| 合并 D（CI） | `bd9d2ee` | `wt/irm-int-d-ci` `c47572e` | 触发去重 + 定向验证入口 + 台账 |
| 合并 A（收口） | `5e4b186` | `wt/irm-int-a-frontend` `c7639e5` | 查证命中 delivered=false 按明确失败 |
| 合并 C（生命周期） | `d6924ee` | `wt/irm-int-c-lifecycle` `74109a5` | 理解后台任务统一生命周期 + 迟到写回护栏 |
| 合并 B（身份） | `1105c3f` | `wt/irm-int-b-identity` `88274c3` | 迁移 39 / 补偿 40 + 幂等 + 精确查证 |

五路并行（各自独立 worktree 与分支，起点 `c6a03b1`）：

| 路 | worktree | 分支 | 成果 SHA | 范围 |
| --- | --- | --- | --- | --- |
| A | `qio-irm-int-a` | `wt/irm-int-a-frontend` | `c7639e5` | 前端冲突收口、提交/理解状态、任务读取、审批、真实执行展示 |
| B | `qio-irm-int-b` | `wt/irm-int-b-identity` | `88274c3` | 稳定提交身份、精确查证、服务端幂等、迁移 39 / 补偿 40 |
| C | `qio-irm-int-c` | `wt/irm-int-c-lifecycle` | `74109a5` | 理解后台任务统一生命周期、关闭与重启恢复 |
| D | `qio-irm-int-d` | `wt/irm-int-d-ci` | `c47572e` | CI 触发去重、定向验证入口、验证台账 |
| E | `qio-irm-int-e` | `wt/irm-int-e-verify` | 见 `docs/integration-review-20261011.md` | 独立审查、迁移验证、跨模块验收 |

覆盖核对（`git diff --name-only` 集合差）：可靠性线改动 76 个文件、真实模型线 63 个文件，
两条线的改动集合都**完整出现在整合分支**（无遗漏），共同修改文件 9 个，全部人工核对。

## 3. 冲突处理（三处，全部按行为重组）

实测冲突清单（`git diff --name-only --diff-filter=U`）：`docs/status.md`、
`frontend/src/components/interactive/IntentStatusPopover.vue`、`frontend/src/stores/interactive.ts`（9 段）。

| 文件 | 冲突内容 | 处理 |
| --- | --- | --- |
| `stores/interactive.ts` | `loadIntents` 成功路径 / `submit` 初始化 / 提交成功分支 / 回执丢失分支 / `approve` / `reject` / `decideBatch` / 导出清单 | 两边行为都保留：读取失败保留最后成功清单**并**接收真实执行记录；提交成功立即记录受理事实与理解身份**并**把读取移出提交事实；未拿到回执保持「结果未知」；审批/批量同时保留防重复锁与最新事实核验（锁先取、`finally` 释放、核验失败不放行）；导出取并集 |
| `components/interactive/IntentStatusPopover.vue` | 空清单提示 | 保留清单刷新失败提示 + 只读重试入口，同时保留真实执行进度/暂停/继续/取消/终态 |
| `docs/status.md` | 双方都新增 P34 | 保留可靠性 **P34**，真实模型项改为 **P35**，同步「尚未完成」段落引用；`python scripts/check_docs.py` 通过（47 个里程碑条目） |

未整份采用 ours/theirs，未用覆盖文件的方式处理业务冲突。

### 3.1 集成缺口一：F10 提交事实被读取失败改判（已修）

真实模型线的 `submit()` 成功路径**本来就在 POST 的 try 里做读取**（来源分支 2403–2416 行），
它自己那条线没有 `refreshAfterSubmit`；可靠性线把读取拆到 `refreshAfterSubmit()`（来源分支 2520–2550 行）
并新增了 `src/acceptance-state-recovery/f10-submit-refresh.test.ts`。合并后两者并存：

* 读取失败（`refreshBoardFromServer`/`refreshVisibleRange` 抛错）被 `catch` 走 `submitReceiptUnknown`
  判成「结果未知」→ 成功提交被改判，可能诱导用户在查证前重复提交；
* 成功路径 `recordNewIntents` 执行两次、三次读取各执行两次。

复现（整合基线 `c6a03b1`）：`cd frontend; npm test -- src/acceptance-state-recovery/f10-submit-refresh.test.ts`
→ 3 用例 1 失败（`expected null not to be null`，test:80）。E 用独立探针实测提交后读取增量 2/2/2（期望 1/1/1）。
修法：删掉 POST 的 try 内那 4 行读取/批次记录，读取与批次记录完全交给 `refreshAfterSubmit`；
受理/理解分支只依赖 `result`，保留不动。修复后 Lead 复核：`f10-submit-refresh` 3 项 + `n8-submit-read-once` 3 项通过。

## 4. 必须保留的双方成果（核对结论）

* **覆盖核对**：两条来源分支改动的文件集合都完整落在整合分支内；共同修改的 9 个文件全部人工看过。
  E 的符号级核对（独立脚本）：两侧新增行为符号 630 个，整合基线缺失 0；缺失的 2 个字面量已定性
  （P34→P35 编号改写、popover 空态兜底文案），另有 1 处旧认领文案按契约 §1.3 有意不并入。
* **自动合并文件也逐对核对**：`backend/src/agent/api/interactive_store.py`、`frontend/src/interactive/submission.ts`、
  `types.ts`、`services/interactive.ts` 的双方接口与行为都在。
* **提交与读取分开**：明确成功后的刷新失败只记 `submitRefreshState='error'` + `submitRefreshError`，
  保留 `lastSubmission` / 受理身份 / 基准依据，只提供只读重试入口，绝不自动重复提交。
* **受理立即启动理解**：受理回执（`delivery.status='accepted'`）当场记录 `submissionId`/`understandingId`
  并启动理解查询，不等全部刷新成功。
* **未知回执**：`submitStatus='unknown'` + `submitResultUnknown=true`，保留改动与勾选，不假定成功也不假定失败。
* **勾选代次保护**：`checkedAtSubmit` 快照 + `applyCheckedCleared` 按对象与代次比对。
* **任务清单读取失败**：保留最后成功清单、标过期风险、给只读重试，不伪装成成功空集合；成功读取同时接收真实执行记录。
* **审批**：单项/批量都保留防重复操作、最新事实核验、执行回执采纳与真实状态更新；核验失败不放行。
* **草稿**：逐键三态协议、删除冲突选择、服务器版本保护都在。
* **材料/附件入口**：互动模式的材料入口是 `BoardToolbar` 常驻的 `AddMenu.vue`（五类），
  `ChatDock` 只发文字、不带附件入口——这是既有两个来源分支的共同事实，不是回退；
  验收口径是「材料入口与未决发送处理都不丢」，A 已用真实组件断言锁住。
* **文档**：P34 保留、真实模型项为 P35；「尚无真实模型人工验收」的限制保留（见 §7）。

## 5. 两条集成缺口的修复

### 5.1 精确查证本次提交（稳定请求身份）

* **服务端**：迁移 **39** 给 `board_submissions` 追加 `client_request_id` 与唯一索引，补偿入口 **40**，
  `REQUIRED_OBJECTS` 同步；身份命中判定**排在 `stale_state` 之前**（否则同身份重试永远进不到幂等分支）；
  同身份重试返回原记录事实（`replayed: true`）且不落新行、不新建理解请求、不启动后台理解、不调用模型；
  同身份不同内容 → `409 request_identity_conflict`；`acceptance_lookup` 只做精确查询，查不到返回 None。
* **接口**：`POST /api/interactive/boards/{board_id}/submissions` 接受可选 `clientRequestId`；
  `GET /api/interactive/boards/{board_id}/submissions/acceptance?clientRequestId=` 返回冻结形状，
  查不到 404 + `{"acceptance": null, "reason": ...}`（**不是**推导出的成功）。
* **前端**：身份在发出提交**之前**生成并落本机台账（重启后同一未决提交仍是同一身份）；
  `confirmAcceptanceAfterUnclear` 只认精确匹配，删除「按创建时间接近 + 排除上次 submissionId」的认领；
  查不到 / 查询失败继续「结果未知」，保留原内容与勾选，不盲目重发；
  理解状态绑定本次提交（`understandingEpoch` + `understandingBoundSubmissionId`），
  旧查询迟到不落地、上一次理解完成不覆盖本次失败/未知。

### 5.2 理解后台任务统一生命周期

* 生产路径经 `AppContext.background` 登记（名字 `interactive-understanding:<understandingId>`），
  底层执行单元探针 = 理解记录是否已到终结状态；关闭开始后 `register` 抛 `BackgroundClosed`，
  调用方拒绝新调度并如实说明（不静默改成同步跑模型）。
* `aclose()` 的 `CloseReport` 新增阶段名 `understanding_stopped` / `understanding_unfinished`；
  未确认结束时 `clean=False` 且 `unfinished` 有名字。真实执行与理解使用同一份应用归属
  （修掉了「登记到 FastAPI app、没人 shutdown」的真实缺口）。
* 关闭/重启中断的理解落成明确失败状态、保留快照、可从原快照重试；
  `run_understanding` 增加 attempt 护栏：被更新的一次尝试取代时丢弃迟到结果，不写终结状态、不建意图。

## 6. 验证台账（额度：本地全量 1 轮 + 全量 CI 1 次）

| 阶段 | 范围 | SHA | 结果 |
| --- | --- | --- | --- |
| 开发期定向（Lead） | `pytest tests/test_interactive_real_model.py tests/test_audit_f04_acceptance_receipt.py` | `c6a03b1` | 12 passed |
| 开发期定向（Lead） | `npx vue-tsc --noEmit` | `c6a03b1` | 通过 |
| 开发期定向（Lead） | `npm test -- src/stores/__tests__` | `c6a03b1` | 93 文件 / 661 用例 |
| 开发期定向（Lead） | `pytest tests/test_topic_decision_eval.py tests/test_boundary_semantic_eval.py tests/test_fu_w3_b01_schema.py` | `c6a03b1` | 21 passed |
| 开发期定向（Lead） | `python scripts/check_docs.py` | 各步 | 通过（47 个里程碑条目） |
| 开发期定向（Lead） | `cargo test`（src-tauri，占位 sidecar/模型目录） | `c6a03b1` | 通过 |
| 开发期定向（Lead） | E 的 `verify_migrations.py` 独立复跑 | `1f36e10` | 43/43 |
| 开发期定向（Lead） | D 的 `verify_workflow_contract.py`(+--selftest) 与 `test_verify_sha.py` 复跑 | `bd9d2ee` | 60 assertions / 7 checks / 17 passed |
| 开发期定向（Lead） | `npm run build` | `bd9d2ee` | 通过 |
| 开发期定向（Lead） | A 的两套前端套件在整合树上复跑 | `5e4b186` | 46 文件 / 493 用例 |
| 开发期定向（Lead） | B+C 后端定向（身份/接口/迁移/生命周期/真实模型/schema） | `1105c3f` | 72 passed |
| **唯一一轮本地全量** | §6.1 | 候选 SHA | 见 §6.1 |
| **唯一一次全量 CI** | `ci.yml` | 候选 SHA | 见 §6.2 |

### 6.1 唯一一轮本地全量（待测）

（冻结候选 SHA 后填写：后端全量、前端类型/测试/构建、文档一致性、Rust 检查与测试、
按 AGENTS.md 必须执行的评测集合、发布闸门自检。）

### 6.2 唯一一次全量 CI（待触发）

（填写 run id / attempt / 各 job 结论。）

## 7. 未验证项与限制

* **真实模型人工验收未完成**：本环境没有可用且已明确授权的模型凭据；自动化证据全部来自 fake/mock provider。
  接口接通与 mock 验证**不等于**真实模型验收。
* 图片视觉理解未接入（当前模型链路没有视觉能力），界面与上下文如实标注 `image_unsupported`。
* 引用型附件（无副本、直接引用原文件）的读取路径没有专门用例覆盖。
* 跨进程迟到写回窗口：`board_understanding` 没有 `owner_instance_id` 列（本轮**未加**，见 B 报告 §6），
  实际收敛靠两层既有机制：① `run_understanding` 的 attempt 护栏（被接手即丢弃迟到结果）；
  ② `create_intents_from_understanding` 按 understanding_id 幂等（旧实例迟到**不会重复生成意图**）。
  仍无法区分的情形是「两个进程共享同一数据库且都没有推进 attempt」。彻底闭合需要
  「迁移增列 + `request_understanding` 写当前实例 id + `run_understanding` 丢弃判定」三处一起改，本轮不做。
* 硬杀（未走关闭流程）留下的 `accepted`/`understanding` 记录在重启时**不自动清扫**，
  由用户重试或下一次关闭收敛。
* B 的并发反例是**顺序化的两窗口**（同进程同一连接），不是真实线程/进程竞争；并发安全网是
  `(board_id, client_request_id)` 的部分唯一索引（并发插入第二个会命中唯一约束并被拒绝）。
* 迁移 39 之前的老提交记录没有 `client_request_id`：不参与幂等，`acceptance.baseStateVersion`
  可能为 null（前端只用于身份比对，对老记录按查不到处理）。
* 前端实机视觉核对的范围与结论见 `docs/integration-review-20261011.md`。
* 其余见 `docs/verify-ledger-integration-20261011.md` §8 与 E 的审查报告。

## 8. main 合并方案与回退

### 8.1 合并步骤

1. `git fetch --all --prune`，核对 `origin/main` 是否仍为已验证的基线 SHA。
2. **main 未推进**：只合入整合分支一次，使用保留历史的合并提交
   （`git merge --no-ff integrate/reliability-interactive-real-model-20261011`）；
   两条来源分支**不再分别合入**。
3. **main 已推进**：先检查新增差异并纳入整合分支，按已用额度做必要定向验证，
   **不复用旧证据**宣称新内容已全量通过。
4. 合并后核对合并所得内容与已验证候选一致（`git diff <候选> <合并结果>` 应为空或仅合并元数据）。
5. 承接同一验证台账，防止 push / PR / main 事件再次触发全量（D 的 concurrency 去重按提交 SHA 生效）。

### 8.2 回退方案

* **代码回退**：`git revert -m 1 <合并提交>`（保留历史）。
* **数据回退**：升级数据库前保存可恢复副本（复制 sqlite 文件 + WAL）。
  代码回退与数据回退**分开说明**：未经验证不得假定升级后的数据库能直接配合旧代码运行。
