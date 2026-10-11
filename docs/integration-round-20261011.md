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
| 整合分支 | `integrate/reliability-interactive-real-model-20261011` | 见 §6 最终 SHA |

三条远端 SHA 与任务书给出的已观察起点**逐一相同**，没有出现「远端已推进」的情况；
`integrate/reliability-interactive-real-model-20261011` 在远端原本**不存在**（先前的
`integrate/interactive-main-20261011` 是另一条线，与本轮无关）。

整合分支从**实际 main** 建立，按「先可靠性、后真实模型」顺序合入，保留双方提交历史：

| 步骤 | 提交 | 说明 |
| --- | --- | --- |
| 基线 | `8fa8452` | `git worktree add -b integrate/… origin/main` |
| 合并可靠性 | `1b9cccd` | `merge --no-ff --no-commit` 后检查：**0 冲突**，未使用 ours/theirs |
| 合并真实模型 | `c6a03b1` | 3 处文本冲突按行为重组（见 §2），其余自动合并 |
| 冻结契约 | `0ed432f` | `docs/integration-contract-20261011.md` |

覆盖核对（脚本 `git diff --name-only` 集合差）：可靠性线改动 76 个文件、真实模型线 63 个文件，
两条线两个集合都**完整出现在整合分支**的改动集合里（无遗漏），共同修改文件 9 个。

## 2. 冲突处理（三处，全部按行为重组）

实测冲突清单（`git diff --name-only --diff-filter=U`）：`docs/status.md`、
`frontend/src/components/interactive/IntentStatusPopover.vue`、`frontend/src/stores/interactive.ts`（9 段）。

| 文件 | 冲突内容 | 处理 |
| --- | --- | --- |
| `stores/interactive.ts` | `loadIntents` 成功路径 / `submit` 初始化 / 提交成功分支 / 回执丢失分支 / `approve` / `reject` / `decideBatch` / 导出清单 | 两边行为都保留：读取失败保留最后成功清单**并**接收真实执行记录；提交成功立即记录受理事实与理解身份**并**把读取移出提交事实；未拿到回执保持「结果未知」；审批/批量同时保留防重复锁与最新事实核验（锁先取、`finally` 释放、核验失败不放行）；导出取并集 |
| `components/interactive/IntentStatusPopover.vue` | 空清单提示 | 保留清单刷新失败提示 + 只读重试入口，同时保留真实执行进度/暂停/继续/取消/终态 |
| `docs/status.md` | 双方都新增 P34 | 保留可靠性 **P34**，真实模型项改为 **P35**，同步「尚未完成」段落引用；`python scripts/check_docs.py` 通过（47 个里程碑条目） |

未整份采用 ours/theirs，未用覆盖文件的方式处理业务冲突。

### 2.1 集成缺口（两条线各自成立、合起来互相否定）——已确认并修复

**F10 提交事实被读取失败改判**（E 独立复现，Lead 独立核实）：

* 真实模型线的 `submit()` 成功路径**本来就在 POST 的 try 里做读取**（来源分支 2403–2416 行），它自己那条线没有 `refreshAfterSubmit`；
* 可靠性线把读取拆到 `refreshAfterSubmit()`（来源分支 2520–2550 行）并新增了 `src/acceptance-state-recovery/f10-submit-refresh.test.ts`；
* 合并后两者并存 ⇒ 读取失败（`refreshBoardFromServer` / `refreshVisibleRange` 抛错）会被 `catch` 走 `submitReceiptUnknown` 判成「结果未知」，
  且成功路径 `recordNewIntents` 执行两次。
* 复现（整合基线 `c6a03b1`）：`cd frontend; npm test -- src/acceptance-state-recovery/f10-submit-refresh.test.ts` → 3 用例 1 失败
  （`AssertionError: 提交请求本身已经成功，必须返回成功结果: expected null not to be null`，test:80）。
* 修法：删掉 POST 的 try 内那 4 行读取/批次记录，读取与批次记录完全交给 `refreshAfterSubmit`；受理/理解分支只依赖 `result`，保留不动。
* 修复与复跑结果见 §5 台账。

## 3. 必须保留的双方成果（核对结论）

（见 `docs/integration-review-20261011.md` 的逐条符号级清单；本节记录 Lead 的覆盖核对与结论。）

* **覆盖核对**：两条来源分支改动的文件集合都完整落在整合分支内（无遗漏），共同修改的 9 个文件全部人工看过。
* **自动合并文件也逐对核对**：`backend/src/agent/api/interactive_store.py`、`frontend/src/interactive/submission.ts`、
  `types.ts`、`services/interactive.ts` 的双方接口与行为都在（不因为「自动合并成功」就当作正确）。
* **提交与读取分开**：明确成功后的刷新失败只记 `submitRefreshState='error'` + `submitRefreshError`，
  保留 `lastSubmission` / 受理身份 / 基准依据，只提供只读重试入口，绝不自动重复提交。
* **受理立即启动理解**：受理回执（`delivery.status='accepted'`）当场记录 `submissionId`/`understandingId`
  并启动理解查询，不等全部刷新成功。
* **未知回执**：`submitStatus='unknown'` + `submitResultUnknown=true`，保留改动与勾选，不假定成功也不假定失败。
* **勾选代次保护**：`checkedAtSubmit` 快照 + `applyCheckedCleared` 按对象与代次比对，等待期间用户重新勾选的不被旧回执清除。
* **任务清单读取失败**：保留最后成功清单、标过期风险、给只读重试，不伪装成成功空集合；成功读取同时接收真实执行记录。
* **审批**：单项/批量都保留防重复操作、最新事实核验、执行回执采纳与真实状态更新；核验失败不放行。
* **草稿**：逐键三态协议（`mode=patch` + `baseRev` + `removed` + `keyVersions`）、删除冲突选择、服务器版本保护都在。
* **文档**：P34 保留、真实模型项为 P35；「尚无真实模型人工验收」的限制保留（见 §7）。

## 4. 两条集成缺口的修复

### 4.1 精确查证本次提交（稳定请求身份）

见冻结契约 §1。要点：客户端在发出提交**之前**生成 `clientRequestId` 并持久保存，
服务端保存身份与受理结果并支持精确查询；同身份重试返回原事实（不重复创建提交、理解请求或模型调用），
同身份不同内容拒绝；查证只认精确匹配，时间/最近一条/板面变化都不能单独作为受理证据。

### 4.2 理解后台任务统一生命周期

见冻结契约 §2。要点：理解调度接入应用统一后台任务注册表与关闭报告，
关闭时拒绝新调度并按既有有界等待/取消规则确认终止，再释放模型连接与数据库；不用固定 sleep 假定退出；
关闭或重启中断的理解有明确状态且可从原快照恢复，旧实例/旧 attempt 的迟到结果不写入当前记录。

## 5. 验证台账（额度：本地全量 1 轮 + 全量 CI 1 次）

| 阶段 | 范围 | SHA | 结果 |
| --- | --- | --- | --- |
| 开发期定向（Lead） | `pytest tests/test_interactive_real_model.py tests/test_audit_f04_acceptance_receipt.py` | `c6a03b1` | 12 passed |
| 开发期定向（Lead） | `npx vue-tsc --noEmit` | `c6a03b1` | 通过 |
| 开发期定向（Lead） | `npm test -- src/stores/__tests__` | `c6a03b1` | 93 文件 / 661 用例全过 |
| 开发期定向（Lead） | `pytest tests/test_topic_decision_eval.py tests/test_boundary_semantic_eval.py tests/test_fu_w3_b01_schema.py` | `c6a03b1` | 21 passed |
| 开发期定向（Lead） | `python scripts/check_docs.py` | `c6a03b1`/`0ed432f` | 通过（47 个里程碑条目） |
| 开发期定向（Lead） | `cargo test`（src-tauri，占位 sidecar/模型目录） | `c6a03b1` | 通过 |
| **唯一一轮本地全量** | 见 §5.1 | 候选 SHA（§6） | 见 §5.1 |
| **唯一一次全量 CI** | `ci.yml` 全量 | 候选 SHA（§6） | 见 §5.2 |

### 5.1 唯一一轮本地全量（待测）

（冻结候选 SHA 后填写：后端全量 `uv run --frozen pytest -q`（含 `-m "not requires_docker"` 口径）、
前端 `npx vue-tsc --noEmit` + `npm test` + `npm run build`、文档 `python scripts/check_docs.py`、
Rust `cargo check` + `cargo test`、按 AGENTS.md 必须执行的评测集合、以及 `scripts/release_gate.py --selftest`。）

### 5.2 唯一一次全量 CI（待触发）

（填写 run id / attempt / 结论。）

## 6. 最终 SHA 与覆盖差异

（待冻结后填写。）

## 7. 未验证项与限制

* **真实模型人工验收未完成**：本环境没有可用且已明确授权的模型凭据；自动化证据全部来自 fake/mock provider。
  接口接通与 mock 验证**不等于**真实模型验收。
* 图片视觉理解未接入（当前模型链路没有视觉能力），界面与上下文如实标注 `image_unsupported`。
* 引用型附件（无副本、直接引用原文件）的读取路径没有专门用例覆盖。
* 前端实机视觉核对的范围与结论见 `docs/integration-review-20261011.md`。
* （其余待各子智能体报告与全量结果补充。）

## 8. main 合并方案与回退

（待候选冻结、全量与 CI 完成后填写最终版本；下为骨架。）

### 8.1 合并步骤

1. `git fetch --all --prune`，核对 `origin/main` 是否仍为已验证的基线 SHA。
2. **main 未推进**：只合入整合分支一次，使用保留历史的合并提交
   （`git merge --no-ff integrate/reliability-interactive-real-model-20261011`），
   两条来源分支**不再分别合入**。
3. **main 已推进**：先检查新增差异并纳入整合分支，按已使用额度做必要定向验证，
   **不复用旧证据**宣称新内容已全量通过。
4. 合并后核对合并所得内容与已验证候选一致（`git diff <候选> <合并结果>` 应为空或仅合并元数据）。
5. 承接同一验证台账，防止 push / PR / main 事件再次触发全量。

### 8.2 回退方案

* **代码回退**：`git revert -m 1 <合并提交>`（保留历史）。
* **数据回退**：升级数据库前保存可恢复副本（复制 sqlite 文件 + WAL）。
  代码回退与数据回退**分开说明**：未经验证不得假定升级后的数据库能直接配合旧代码运行。
