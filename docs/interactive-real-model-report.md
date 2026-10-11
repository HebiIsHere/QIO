# 互动模式「真实模型理解 + 真实执行」交付报告（2026-10-11 第九轮）

契约：`docs/interactive-mode-contract.md` §14。状态：见 `docs/status.md` 的 P34 一节。

## 1. 基线与交付

| 项 | 值 |
| --- | --- |
| 真实 main 基线 | `8fa845255c118a7104b1de8e37298ca745924482`（开始时重新 `git fetch origin --prune` 后核对 `origin/main` 的实际顶端，与上一轮记录一致） |
| 交付分支 | `feat/interactive-real-model` |
| 集成分支起点 | `81b166f`（契约与迁移冻结提交）→ `1b9a95b`（迁移清单断言改为按清单核对） |
| 交付工作区 | `D:\qio-dev\qio-irm-lead` |
| 是否合并 main / 发布 | 都没有：不自行合并 `main`、不发布版本 |
| 是否改历史迁移 | 没有：只追加迁移 37 与补偿 38 |

## 2. 子智能体任务、worktree、分支与合入结果

| 路 | 任务 | worktree | 分支 | 成果 |
| --- | --- | --- | --- | --- |
| Lead | task-6：契约、公共文件、迁移、集成、验收与交付 | `qio-irm-lead` | `feat/interactive-real-model` | 契约 §14、迁移 37/38、`models` 常量、前端契约类型与 API 客户端、端到端验收用例、本文档与 `docs/status.md` |
| A | task-1：提交可靠受理、材料真实读取、理解过程路由 | `qio-irm-a` | `feat/irm-a` | `interactive/materials.py`（新）、`submission.py`、`api/interactive_store.py`、附件关联前端与材料纯函数；集成期修正 `b843bdb` |
| B | task-2：真实模型理解与结构化输出校验 | `qio-irm-b` | `feat/irm-b`、`fix/irm-b-understanding-context` | `interactive/understanding.py`（新）、`interactive/prompts.py`（新）；集成期修正 `927a37a` |
| C | task-3：批准后真实执行、调度、暂停取消与恢复 | `qio-irm-c` | `feat/irm-c` | `interactive/execution.py`（新）、`intents.py`、`api/interactive_intents.py` |
| D | task-4：前端状态、流式显示、预览、审批与失败恢复 | `qio-irm-d` | `feat/irm-d` | `stores/interactive.ts`、`interactive/approval.ts`、互动组件与视图 |
| E | task-5：独立审查与验收 | `qio-irm-e` | `feat/irm-e` | `docs/interactive-real-model-review.md`、`scripts/interactive-real-model-verify/` |

合入方式：主智能体在 `feat/interactive-real-model` 上按 A→B→C→D 顺序逐个 merge（`--no-ff`），
冲突由主智能体解决；每一路合并后立即跑「该路直接影响的测试」。

**集成期发现并修掉的真实问题**（都是并行开发的接口口径差异，不是演示）：

1. **理解上下文的「有效变化 / 用户备注」来源**：受理只把内存里的 `after` 投影冻结进快照，
   而理解侧原实现从快照里取 `expressions`/`note` → 用户要求为空。
   改成按 `submission_id` 从 `board_submissions` 补读（**仍然不读当前板面**），并如实标注来源。
2. **`empty` / `duplicate` 判定顺序与契约 §1.5 不符**：原实现先判「去掉显示类表达后为空」，
   导致「材料可见 → 提交成功 → 只改位置再提交」返回 `empty` 而不是 `duplicate`。
   改成先判 duplicate、再判 empty（两种状态的安全语义不变：都不更新基准、都不调用模型）。
3. **「理解模块不可用」的测试手法在集成后失效**：原来靠删 `sys.modules` 模拟，模块真实存在后会重新 import 成功。
   改成替换加载点，行为判定一字未改。
4. **前端成功文案仍在说「第一阶段没有接入 QIO 模型调用」**：与 §14 事实冲突，改成如实显示「已受理，正在读取并真实理解」。

## 3. 已实现的链路

用户提交获准材料与变化 → 服务端从**已保存状态**推导可见范围并冻结这次提交的快照 →
提交被**可靠受理**（建立理解请求，`delivery.delivered=true / status='accepted'`）→
后台真实读取材料 → 调用**真实模型**理解（结构化上下文，不是把板面 JSON 当聊天文字）→
程序校验模型输出 → 落成意图（等待审批）或请求补充说明或明确「当前无需开展任务」→
界面显示待审批预览 → 用户批准 → **真实执行**允许的板面操作 → 结果卡与来源引用可相互定位。

- 提交记录、理解请求、意图、执行记录、结果卡片通过 `submission_id` / `understanding_id` / `intentId`
  与卡片 `meta.reply` 互相定位。
- 聊天发送与板面提交保持分离：聊天只发用户输入，不读取当前板面、未提交改动、未获准注释或关注范围。

## 4. 本轮支持的材料与操作

材料（`resolve_materials`，来源口径诚实）：

| 卡片 | 行为 |
| --- | --- |
| 文字注释（勾选后） | 正文进上下文；未勾选 / 隐藏 / 删除的一律不进（含间接链接） |
| 代码卡 | 正文进上下文（`inline`） |
| 文件卡 | 有 `meta.file.attachmentId` 且附件真实可用时读正文（`attachment_text`）；没有关联附件时如实标为未读取正文 |
| 图片卡 | 当前模型链路没有视觉能力 → `image_unsupported`，不假装看懂 |
| 网址卡 | 已有正文 → `url_fetched`；只有地址 → `url_address_only` |

允许的板面操作（`ALLOWED_INTENT_OPS`，用户在互动模式中能做的子集）：
生成结果卡、生成文字卡、移动卡片、成组、有序组排序、建立关系链接。
**不允许**删除、隐藏、勾选用户内容、改用户已有卡片正文、直接写数据库或绕过审批。

## 5. 证据

### 5.1 模拟模型自动化证据（fake / mock provider，不联网、不用真实 Key）

- 各模块定向用例：提交受理与材料（A）、理解与校验（B）、执行与调度（C）、前端状态（D）。
- 主智能体端到端验收：`backend/tests/test_interactive_real_model.py`
  （提交 → 受理 → 材料 → 理解 → 意图 → 审批 → 执行 → 结果卡与来源引用；
  换材料与要求结果相应变化；未勾选 / 隐藏 / 间接链接不进上下文；重复提交不额外调用模型、不新建理解请求；
  需要补充与无需行动都不产生任务；重复批准不重复执行、结果卡不重复）。
- 迁移：`backend/tests/test_interactive_migration_integration.py` 与
  `backend/tests/test_fu_w3_b01_schema.py`（迁移 37 / 补偿 38 与必需对象清单）。

### 5.2 真实模型人工验收证据

**未完成**。本环境没有可用的、已明确授权的模型凭据，因此本轮**没有**真实模型人工验收记录，
也没有把任何模拟验证描述成真实模型验收。界面与文档都如实写明这一点。

## 6. 测试与 CI 台账

见本报告末尾的「验证台账」一节（候选 SHA 冻结后由主智能体写入唯一轮次的全量测试与唯一一次全量 CI 记录）。

## 7. 数据迁移与兼容性

- 只追加迁移 37：`board_understanding`、`board_executions` 两张表与相关索引，
  以及 `board_intents` 的 `goal / expected / operations / basis / intent_key / understanding_id` 六列（都有默认值）。
- 追加补偿迁移 38（= 迁移 36 的全部语句 + 本轮对象），`COMPENSATION_VERSION` 由 36 抬到 38，
  `REQUIRED_OBJECTS` 同步补进新对象；历史迁移与用户数据不动。
- 老库升级路径：版本号低于 37 的库按顺序迁移补齐；版本号已经到 38 但对象缺失的库由补偿入口重放补齐。

## 8. 剩余风险与未完成项

1. **真实模型判断质量与真实凭据端到端链路未经人工验收**（本环境无可用凭据）。
2. 图片视觉理解未接入：界面与上下文都标注不支持，接入视觉链路后才能打开。
3. 长期记忆复用既有知识 / 长期记忆机制（落待审批），没有改写用户会话 transcript。
4. 引用型附件（直接引用原文件、没有副本）的读取路径没有专门用例。
5. 结果卡正文来自模型在理解阶段产出、并在预览中被用户批准的内容；执行阶段只在方案缺正文时调用模型生成。
6. 前端窄窗口与主题只做了 DOM 与样式令牌级验证，未做真实浏览器截图核对。
7. 演示入口仍然存在（`demo=true` 与 `/demo/advance`）：演示推进不得驱动真实任务，正式路径不显示演示内容。

## 9. 验证台账

### 9.1 唯一全量测试轮次

- **冻结候选 SHA：** `c44744f22f2087ce17225ff27054e5fa5e92ee31`（A–D 全部合入 + 独立审查报告合入后的候选）。
- **执行环境：** 本机 Windows，Node 22 / Python 3.11（`backend/.venv`）。
- **范围与命令（本轮各执行一次）：**

| 集合 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `cd backend; uv sync --frozen --extra dev; uv run --frozen pytest -q` | 1 项失败（见 9.2），其余全部通过 |
| 前端类型 | `cd frontend; npx vue-tsc --noEmit` | 通过（exit 0） |
| 前端全量 | `cd frontend; npx vitest run` | 41 项失败 / 2363 项通过（300 个文件）；其中 40 项是基线既有失败、1 项是本轮回归（见 9.2） |
| 文档一致性 | `python scripts/check_docs.py` | 通过（46 个里程碑条目） |
| 适用评测 | `cd backend; uv run --frozen python -m agent.eval.run` | 通过（exit 0） |

### 9.2 失败项分析与定向重跑

1. **后端 1 项：** `tests/test_r7_disconnect_cancel_verify.py::test_disconnect_does_not_start_the_turn_after_gate_release`
   - 定向重跑（只跑这一条）两次：第 1 次通过、第 2 次失败 —— 本身不稳定。
   - 在**基线 `8fa8452` 的干净 worktree** 上定向重跑同一用例 3 次：**3 次全部失败**。
   - 结论：**基线就存在的、与本轮无关的失败**（Windows 平台下的断连 / 取消时序），本轮没有为它改任何代码。
2. **前端 41 项：** 先把 12 个失败文件在**基线 `8fa8452`** 上定向重跑 → 其中 **40 项同样失败**（失败用例名逐条一致）。
   逐条对比后，只有 1 项只在本轮候选上失败：`src/stores/__tests__/final-lead-m1.test.ts > 提交返回 stale_state：给出可操作的真实原因、不推进提交状态、不清改动`。
   - 根因：本轮给互动服务模块新增了导出，而该文件用的是**部分 mock**；Vitest 对「未声明的导出」会直接抛错，
     能力探测 `apiSupports` 因此炸掉（它自己写的约定是「缺少新接口时如实降级、不抛错」）。
   - 修复：`frontend/src/stores/interactive.ts` 的 `apiSupports` 包一层 `try/catch`。**没有改任何测试、没有放宽断言。**
   - 定向重跑：只跑该文件（失败发生在模块级能力探测上，无法只跑单条而不加载该模块）→ 10 项全过。
   - 其余 40 项是**基线就存在的失败**（`failedSend` 持久化 / 失败恢复一族），本轮没有改 `stores/session.ts` 与相关组件。
3. **修复后的 SHA：** `df68476960086bdc8aa78a0418f228649bab6d27`（相对候选只在 `frontend/src/stores/interactive.ts` 多了一处 `try/catch`）。

### 9.3 候选与最终修复 SHA 的覆盖差异

- 后端全量、前端全量、类型检查、文档一致性、评测都跑在候选 `c44744f` 上。
- 最终 SHA `df68476` 相对候选**只改了前端一个文件**（后端无任何改动）。
- 因此：**后端全量的结论适用于最终 SHA**（后端源码一致）；
  前端在最终 SHA 上只做了**定向重跑**（`npx vitest run src/stores/__tests__/final-lead-m1.test.ts` → 10 项全过）
  与**类型检查**（`npx vue-tsc --noEmit` → exit 0）；
  **没有在最终 SHA 上重新跑前端全量**，此处如实记录，不声称最终 SHA 经历了新的前端全量验证。

### 9.4 唯一全量 CI

- 交付分支推送**一次**：`feat/interactive-real-model`。
- **不创建 PR**：`ci.yml` 同时监听 `push` 与 `pull_request`；创建 PR 会用 `pull_request` 事件再触发一遍全量 CI，
  等于第二次全量，因此本轮不建 PR（与「不自行合并 main」也一致）。
- CI 记录在推送完成后补写进本表；**补写用带 `[skip ci]` 的提交**，避免再触发一次全量 CI。

| 项 | 值 |
| --- | --- |
| workflow | CI（`ci.yml`） |
| run ID / attempt | 推送后补充 |
| 触发 SHA | 推送后补充 |
| jobs / matrix | docs consistency；backend (py3.11)、(py3.12)；backend (windows-latest)；以及仓库既有前端任务 |
| 结果 | 推送后补充 |
| 失败项与定向重跑 | 推送后补充 |
