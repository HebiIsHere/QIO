# 三分支整合（对话页 v2 / 互动页 v2 / R9-H13）— 2026-10-25

> 本文记录本轮整合的**来源、关系、冲突决策、迁移设计、分工、验证台账与已知限制**。
> 状态口径见 `docs/status.md`；结构与契约见 `docs/architecture.md`。

## 1. 冻结来源与实际变化

开工时间：2026-10-25 19:31 +08:00（重新 `git fetch` 四条分支，refspec `+refs/heads/*:refs/remotes/origin/*` 本身覆盖全部远端引用，仍显式逐条 fetch 复核）。

| 角色 | 引用 | 完整 SHA | 与开工台账 |
| --- | --- | --- | --- |
| 正式基线 | `origin/main` | `249a63443c72b3dad011b5446ee9920f4dadf0fd` | 一致 |
| 对话页 v2 | `origin/feat/conversation-page-v2` | `13ec09357cd1477348635096a3999eb2e4ef8298` | 一致 |
| 互动页 v2 | `origin/feat/interactive-page-v2` | `9b23634892fa7fac0a8ccd471626d07d2bdefd0e` | 一致（仍为整合祖先） |
| 权限审批线 | `origin/r9h13/main` | `6f56d815d6be3136c446ecdf9ad7591256a5350e` | 一致 |
| **互动页线追加交付** | `origin/fix/interactive-v2-frontend-flow` | `2881346365062ab6b36adfdd08c7433b83576514` | **本轮新增**（见 §1.2） |

### 1.1 祖先关系（`git merge-base --is-ancestor` 实测）

- `main` 是三条来源的祖先；三者共同祖先均为 `249a6344`。
- `feat/conversation-page-v2` 是 `feat/interactive-page-v2` 的祖先（mb = `13ec0935`），因此先合 cv 再合 i2 不会重复摘取。
- cv / i2 / r9 互不为祖先；unique commits：cv 28、i2 115、r9 291。
- R9-H13 的提交历史已包含 R9 → R9F → R9G → R9H → R9H2…R9H12 的全部修复，本轮**没有**把历史编号当作独立来源重复 cherry-pick。

### 1.2 开工后远端推进：互动页线又完成一次交付

开工冻结之后复查远端，发现互动页线在 `9b236348` 之上又推送了 `fix/interactive-v2-frontend-flow`（`28813463`，2026-10-25 18:43 +0800）。

- 差异审查：相对 `9b236348` 共 49 文件、+9371/-196，**纯前端**（页面 / store / 组件 / 手势 / 恢复 / 测试 + 受控验收脚本），未改后端、权限、提示词、数据库、Tauri 或依赖。
- 交付状态：该分支上的完整 CI run `40185956396` 结论 success。
- 内容：F01–F10 十项互动页前端操作逻辑修复（材料影响确认、意图视图唯一维护、任务面板、恢复事项摘要、读取失败保留旧内容、重复拖线不覆盖关系、菜单移出空组、收起组定位、关系胶囊缩放手势、意图位置回写）。其中 F01（影响确认界面缺失导致保存永久停在等待态）、F03（任务面板未实现、无暂停/取消）、F04（重复拖线覆盖已有关系含义）、F05（恢复事项计数错误）、F08（收起组定位无效）属于**核心可用性缺陷**，按任务书第九条列为本轮必修。
- 处理：保留原冻结记录，把该分支作为互动页线的最新完成交付追加进来源清单并按顺序合入（见 §2 第 4 步）。原始 `9b236348` 仍是最终整合提交的祖先。

### 1.3 开工时仓库状态

- 主克隆 `D:\qio-dev\qio`；328 个既有 worktree，其中 38 个有未提交改动，**本轮没有删除、重置或复用其中任何一个**。
- 整合 worktree 新建于 `D:\qio-dev\qio-int-3way`（分支 `integrate/three-branches-20261025`），从`origin/main` 起。
- 操作前 main 完整 SHA：`249a63443c72b3dad011b5446ee9920f4dadf0fd`（代码回退基准）。

## 2. 整合分支与提交关系

| 步骤 | 提交 | 内容 |
| --- | --- | --- |
| 起点 | `249a6344` | origin/main |
| 1 | `b85d5986` | merge `feat/conversation-page-v2` (`13ec0935`)，无冲突 |
| 2 | `2497b372` | merge `feat/interactive-page-v2` (`9b236348`)，无冲突 |
| 3 | `505c8d28` | merge `r9h13/main` (`6f56d815`)，19 个冲突文件全部解决 |
| 4 | `5f92239b` | merge `fix/interactive-v2-frontend-flow` (`28813463`)，自动合并无冲突，vue-tsc + 74 项 i2v2 用例复核 |
| 检查点 | `505c8d28` / `5f92239b` | 工作流 A–D 的并行基线（含临时决策，**不是**最终验收候选） |

## 3. 19 个冲突文件与决策

预合并分析用 `git merge-tree --write-tree --name-only` 生成（不是靠人工猜测），实测 19 个冲突文件；`git merge --no-ff` 复现结果一致。逐文件记录：两侧目的 / 最终保留行为 / 位置 / 验证。

| 文件 | 两侧目的 | 最终保留行为 |
| --- | --- | --- |
| `backend/src/agent/api/server.py`（5 处） | 页面线：互动启动恢复；R9：审批 v2 恢复 + 临时文件清理；设置接口各加一个字段 | 启动段三条顺序为 `approvals.recover() → 互动 startup_recovery → tempfiles.sweep()`，各自 try/except 互不阻断；`/api/settings/ui` GET/PUT 字段并集（`typewriter_cps` + `conversation_layout` + `close_behavior`），PUT 统一返回读回值（R9 `test_ui_close_behavior` 与两个前端 store 的硬约束） |
| `backend/src/agent/interactive/board_tools.py`（3 处） | 页面线：`meta.display` 与组可见成员快照；R9：经附件服务核验的源引用事实 | `_card_snapshot(card, *, attachment_service=None)`：既返回 display/组可见范围，也返回 `file_ref`；`_attachments()`/`_card_file_ref()` 保留；8 个调用点全部传 `attachment_service` |
| `backend/src/agent/storage/schema.py`（1 处） | 页面线：迁移 55/56/57；R9：迁移 52/53/55/57 + 58…66 | 保留 main 历史与 R9 链完全不动；页面线迁移改号到 66 之后（67/69），68/70 为统一补偿；`SCHEMA_VERSION = COMPENSATION_VERSION = 70` |
| `backend/src/agent/storage/migrate.py`（1 处） | 两侧各自扩 `REQUIRED_OBJECTS` | 并集：页面线 `task_feed`/`task_ack`/`board_intent_turns`/`board_intent_retries` + 6 个索引，R9 的对象与列全部保留 |
| `backend/tests/test_p3_d_migrations.py`（5 处） | 两侧都把「版本必须等于 51」改成「收敛到最新」 | 取 R9 的通用口径（`apply_migrations == COMPENSATION_VERSION`、无缺对象、幂等） |
| `backend/tests/test_p4_d_oldpaths_static.py`（4 处） | 页面线：守卫「52/53/54 是 R9 冻结号段」；R9：守卫「追加式、编号唯一升序、版本收敛」 | 取 R9 版本：51/49/50 保留、编号唯一升序、SCHEMA/COMP 等于最新；R9 的号段已是既成事实，页面线的旧守卫已不成立 |
| `docs/status.md`（2 处） | 两条线各自追加「最后核对」与里程碑 | 两侧条目**并存**（页面线与 R9 各自的最后核对、P73 各自成节），不合并成一段 |
| `frontend/src/App.vue`（1 处） | 页面线：按路由门控旧审批入口；R9：`security.load()` | 路由门控（`isConversation`/`isChatRoute`）+ `const security = useSecurityStore()` 并存 |
| `frontend/src/components/Composer.vue`（1 处） | 页面线：删除旧话题栏；R9：话题栏内加权限模式徽标 | 采用页面线的删除（v2 输入区只保留胶囊），徽标改挂到 `ChatColumn.vue` 的常驻话题栏（R9「模式常驻可见」不丢） |
| `frontend/src/components/WindowChrome.vue`（5 处） | 页面线：共用圆角全局栏 / 品牌 / 设置入口 / 标题条高度；R9：CloseRequested / ask / 托盘 / 真正退出 | 外观与布局取页面线，关闭协议取 R9：`window.close()` 交壳、ask 先 ACK 再弹三选、记住选择写回并同步壳、壳侧退出只置守卫；不引入未使用的 `inTauriShell` |
| `frontend/src/components/__tests__/WindowChrome.test.ts`（5 处） | 两套并行的 mock 装置（`shell` vs `mocks`） | 重写为并集：统一 `mocks`、修复悬空 `shell.push` 与 `TITLEBAR_PROPERTY` 作用域两个合并 bug；页面线品牌/设置/高度用例 + R9 ask/托盘/退出/记住选择的 5 条用例都在 |
| `frontend/src/components/interactive/BoardCanvas.vue`（1 处） | 页面线：勾选同时写 participation；R9：「作为互动材料」文案 | v2 语义（participation 唯一事实源）+ R9 文案 |
| `frontend/src/components/interactive/BoardCard.vue`（5 处） | 页面线：参与控件 + 圆形图标工具栏（许可 UI 已移除）；R9：整卡「作为互动材料」徽章 / 勾选框文案 / `mergeAttachmentIntoMeta` | v2 DOM（participation + icon-tools）为最终行为；保留 R9 的 `mergeAttachmentIntoMeta` 导入用于文件卡 meta 合并 |
| `frontend/src/components/interactive/__tests__/{iml-b-reply-markdown,irl-b-card-states,textCardUnified}.test.ts`（13 处） | 页面线：断言许可 UI 已移除、参与控件仍在；R9：断言「作为互动材料」文案 | 取 v2 断言（与 BoardCard 最终 DOM 一致），R9 的文件源引用断言由工作流 C 复核补齐 |
| `frontend/src/services/api.ts`（2 处） | 页面线：conversation_layout 类型；R9：quitGuard 与 close_behavior | 并集（imports + `getUISettings` 返回类型三字段） |
| `frontend/src/stores/approvals.ts`（1 处） | 页面线：`inlineSuppressed`（统一弹窗唯一按钮）；R9：history/lastResult/recovered/recoveryError | 并集（`inlineSuppressed` + 全部 R9 字段） |
| `frontend/src/views/InteractiveView.vue`（1 处） | 页面线：v2 工作区/画布/意图层；R9：旧顶栏 + 「本次互动材料」文案 | 取 v2 结构；R9 的文案改动在 v2 位置同步（`本次互动材料的内容`） |

### 3.1 没有文字冲突但仍审查过的文件

- `backend/src/agent/services/app.py`：合并结果 = R9 版 + 页面线两处 task_feed 接线；AST 无重复定义；审批/授权只有一套权威上下文（`self.approvals` / `self.authorization` / `self.authorizations`），权限键唯一 `computer.permission_mode`；启动恢复两层（构造期归属判定 + lifespan `recover_stale`）是协同不是覆盖。
- `backend/src/agent/services/turn_orchestrator.py`：R9 附件 READ 授权与页面线 `_finish_interactive_turn`/重试政策/轮次元数据位置不重叠。
- `backend/src/agent/prompts.py`：互动行为规则与 R9 权限/授权/要求解析规则都在，键与消费方一一匹配，固定提示词仍集中；未见恢复/重试绕过审批的描述。
- `backend/src/agent/interactive/materials.py`、`frontend/src/stores/interactive.ts`、`frontend/src/interactive/{board,types}.ts`：由工作流 C 复核（材料可见性 / 参与状态 / 源引用 / 恢复与类型一致）。
- 审批链路：页面线统一层与 R9 `approvalLifecycle` 的完整调用链由工作流 B 复核。
- 窗口生命周期与事件清理：由工作流 D 复核（`onBeforeUnmount` 释放 resize + 两个壳事件监听 + keydown）。

## 4. 数据库：撞号与统一方案

### 4.1 开工实测

| 来源 | MIGRATIONS 追加链 | COMPENSATION_VERSION |
| --- | --- | --- |
| main `249a6344` | …43,45,48,51 | 51 |
| 对话线 `13ec0935` | +55（task_feed/task_ack，且 55 兼作补偿入口） | 55 |
| 互动线 `9b236348` | +55, 57（56 = `migration_p3_d.py`；57 = 补偿入口） | 57 |
| R9 `6f56d815` | +52,53,55,57,58,59,60,61,62,63,64,65,66 | 66 |

撞号：页面线 55/57 与 R9 的 55/57 **同号不同义**。

### 4.2 统一方案（首版，工作流 A 验证/修正）

- 保留 main 历史迁移与 R9 已形成的链（52…66）**不动**。
- 页面线需要的新对象排到 66 之后：
  - `67` = `task_feed` / `task_ack` + 2 索引（原页面线 55 的 DDL）；
  - `68` = 统一补偿（迁移 66 的全部语句 + 67，去重）；
  - `69` = `migration_p3_d.py` 的内容（`board_intents.display` + `board_intent_turns` + `board_intent_retries`），模块内版本号同步改为 69；
  - `70` = 统一补偿（68 的全部语句 + 69，去重）。
- `SCHEMA_VERSION = MIGRATIONS[-1][0]`，`COMPENSATION_VERSION = 70`。
- `REQUIRED_OBJECTS` 取两线并集；已知缺口由前置自愈 + 对象级复核覆盖（见 §4.3）。

### 4.3 双向补偿与前置自愈（工作流 A）

- **R9 66 库 → 页面对象**：67/68 提供。
- **页面 55/57 库 → R9 对象**：页面库跳过 R9 的低号迁移（`apply_migrations` 只按版本号前进），
  由 58/60/62/64/66 的补偿链补齐；但实测 `cv_55` 会在迁移 57 处失败
  （`no such table: user_requirements`），因此需要**主循环之前的前置自愈**：
  仅当 `51 < current_version < COMPENSATION_VERSION` 且对象级校验发现缺对象时，
  以 CREATE/ALTER 语句重放一次补偿（不写 schema_version、不推进版本、非预期错误照抛、日志标为兼容修复）。
- 版本号已登记但对象缺失的库：末尾对象级复核 + 补偿重放照旧生效。

### 4.4 迁移的副作用边界

迁移与回填**不得**启动工具、重新授权、消费审批、增加确认次数、重新执行意图或重置重试次数。
补偿链为纯追加 DDL（唯一例外是继承自 51 聚合的 1 条幂等 `INSERT OR IGNORE`），不重放 61/63 的一次性数据 UPDATE。

## 5. 并行分工与文件归属

| 工作流 | 负责 | 拥有的文件 |
| --- | --- | --- |
| A 数据库 | 迁移撞号、双向补偿、五类来源升级 | `backend/src/agent/storage/**`、迁移相关测试 |
| B 审批与页面 | 统一审批层对接 R9 生命周期、输入区、公共外壳 | `frontend/src/services/api.ts`、`App.vue`、`Composer.vue`、`stores/approvals.ts`、`stores/unifiedApprovals.ts`、`components/approval/**` |
| C 互动与文件 | 板面/卡片/组/连线、文件源引用、快照与恢复重试 | `backend/src/agent/interactive/**`、`frontend/src/components/interactive/**`、`InteractiveView.vue`、`stores/interactive.ts`、`frontend/src/interactive/**` |
| D 后端与桌面 | 接口、应用初始化、执行调度、提示词、桌面生命周期 | `backend/src/agent/api/server.py`、`services/**`、`prompts.py`、`WindowChrome.vue` 及其测试 |
| E 独立验收 | 跨工作流契约、对抗场景、真实界面 | 只写证据（`D:\qio-dev\_int3\evidence\`） |
| Lead | 来源冻结、合并与冲突决策、共享契约、文档、全量与 CI、main | `docs/**`、最终整合提交 |

子智能体各自在独立 worktree / 分支工作，只有 Lead 提交整合分支、触发 CI、更新 main。

## 6. 验证台账

### 6.1 定向验证（按工作流，均为定向筛选，不是全量）

| 工作流 | 范围 | 结果 | 证据 |
| --- | --- | --- | --- |
| A 数据库 | 迁移/升级 10 文件（含新增 `test_integ_a_migration_upgrade.py`） | 84 passed | `_int3/evidence/A-integ-targeted.log` |
| B 审批 | 前端 5 文件 57 例 + 后端 `test_approval_api.py`/`test_knowledge_approval_api.py` 18 例 | 全绿 | `_int3/evidence/B-approval-mapping.md` §阶段二 |
| C 互动 | 后端 8 文件 208 + 50；前端 14 文件 127 + 8 文件 176 + irl-b 29 | 全绿 | `_int3/evidence/C-interactive-seams-review.md` |
| D 后端/桌面 | 后端 16 文件 214；前端 WindowChrome/p1a/design-guards 45；`vue-tsc` 0 | 全绿 | `_int3/evidence/D-backend-desktop-plan.md` §9–§12 |
| E 独立验收 | 在最终候选 97b98368 上：后端 28 文件 433 / i2 后端 19 文件 329 / 迁移 8 文件 56 / 前端 28 文件 448；知识审批 11 文件 75 / `test_r9g_cmd_binding` 17；五类来源库升级 | 全部 0 红 | `_int3/evidence/E-verification-report.md`、`E-06-*.{xml,log}` |
| Lead 集成 | 迁移冒烟、设置/审批/互动/迁移定向批次、真实界面抽查 | 见 6.2 | `_int3/evidence/` |

### 6.2 本地全量轮次（仅一次，候选 `c56b7033`）

- 后端：`cd backend; uv run --frozen pytest -q` → 在 `c56b7033` 上首跑：**2 failed**，其余通过。两条失败分别是 `test_r9g_cmd_binding.py::test_replay_invalidates_when_unrestricted_switched_back_to_restricted`与 `test_sr_d_fs_threading.py::test_fs_patch_io_off_event_loop_thread`；**两条都在冻结基线 `r9h13/main` 6f56d815 上同样失败**（我在 qio-r9h13-main worktree 实测，非整合引入）。处置见 6.3。
- 前端：`cd frontend; npx vitest run` → **474 files / 4336 tests 全部通过（exit 0）**
- `npx vue-tsc --noEmit` → exit 0；`npx vite build` → 成功（构建产物 dist/，未提交）
- 文档一致性：`python scripts/check_docs.py` → 通过（98 个里程碑）；自检 `--selftest` 见 CI
- 第三方声明：`python scripts/licenses/check_third_party.py` → 通过（锁文件组件 914 / 声明 918 条 / 许可证 42 种）；锁文件相对 `origin/main` **零变化**，未改依赖、未改版本号
- 离线评估：`uv run --frozen python -m agent.eval.run` 在候选与 `r9h13/main` 基线上各跑一次，指标一致（`verdict: skip`），日志 `_int3/evidence/eval-candidate.json`、`eval-r9h13.json`
- 真实界面：对话/互动/统一审批/长文本与宽表格在 1440×900 与 480×600 两档截图检查（E 的 `_int3/evidence/shots/`），无页面横向溢出、控制台 0 error

### 6.3 全量失败处置（同一轮内的修复与定向复验）

| 用例 | 归属 | 根因 | 处置 | 复验 |
| --- | --- | --- | --- | --- |
| `test_r9g_cmd_binding::test_replay_invalidates_when_unrestricted_switched_back_to_restricted` | 基线既有（r9h13 已红） | R9-H13 §12 删除了「仅模式字符串不同即失效」；重放路径按**当前模式**重建许可并重判规则。旧期望过期 | 用例改名 `test_replay_continues_after_unrestricted_switched_back_to_restricted`，断言 `ok=True` + `recheck.mode_now/binding_mode/confirmations_required` + `permit.mode=restricted`；**新增** `test_replay_stops_when_switched_to_read_only` 守住「收紧到不允许则停止」 | `tests/test_r9g_cmd_binding.py` 全文件通过 |
| `test_sr_d_fs_threading::test_fs_patch_io_off_event_loop_thread` | 真实回退（H5/H7/H13 引入） | `fs_tools.recheck_before_execute` 的 `_verify_local_preconditions` 是同步文件 I/O（identity/stat + 正文读取 + 摘要），H13 C1「读取先于内容」让 fs_patch 必经此处，于是在事件循环线程读正文 | 修实现：整个同步核对交给共享有界执行器（`get_blocking_executor`），与工具其余 I/O 同一线程纪律；**测试一行未改** | `tests/test_sr_d_fs_threading.py` 全文件通过 |

复验记录：`uv run --frozen pytest -q tests/test_r9g_cmd_binding.py tests/test_sr_d_fs_threading.py` → 29 passed（exit 0）；邻近 7 个文件（fs_tools / r9h5 / r9h6 / r9h_recheck_limits / r9f_origin_fs_tools / r9h_content_binding）→ 77 passed。修复提交 `c152e6d1` 合入后，**最终 SHA 与最初全量 SHA 不同**，按「原候选全量 + 新提交定向复验」记录；最终 SHA 的完整矩阵由 §6.3 的完整 CI 覆盖。

### 6.4 完整 CI 轮次（仅一次）

- 触发方式：把整合分支 push 到 `origin/integrate/three-branches-20261025`（`ci.yml` 对 push 全分支 + PR 生效）。本轮**只创建了这一次**会跑完整矩阵的远端事件：子工作流全程在本地 worktree，未 push 分支、未开 PR。
- 运行：**run 40196935297**，SHA `97b983682def71cbe5bb1d45bb41f3560f7504ed`（分支 integrate/three-branches-20261025）。
- 逐 job 结论（全部真实执行，无 skipped）：ci dedup preflight ✓ / docs consistency ✓ / frontend（typecheck + 测试 + 构建）✓ / rust (ubuntu) ✓ / rust (windows) ✓ / frozen worker (windows) ✓ / install e2e (windows) ✓ / backend (py3.11) ✓ / backend (py3.12) ✗ / backend (windows-latest) ✓。
- **唯一失败**：`backend (py3.12)` 的 `tests/test_r9h9_regressions.py::test_program_binding_survives_path_change_and_restart`（第 172 行 `assert named.ok is False`，实际 `named.ok=True`）。
  定位证据：同一 SHA 的 `backend (py3.11)` 整套通过；`targeted-verify` 单跑该用例 py3.11 success（run 40200984283）、py3.12 failure（run 40200822357）；把同一命令打到**冻结基线 r9h13/main 6f56d815** 的 py3.12 也是 failure（run 40201030638）→ **基线既有失败，非本整合引入**。
  根因（经代码与环境核对）：hostedtoolcache 的 py3.12 解释器被复制到临时目录后仍能运行，于是真实行为暴露——名称限制绑定的是保存时对象 A 的身份，PATH 改到另一目录后同名 `runner` 解析到**另一个对象 B**（身份不同），当前实现放行 B；py3.11 与 Windows 上复制出来的解释器跑不起来（`ok=False`），旧断言被“假绿”满足。
- 处置与复验：（回填 D 的修复 + py3.11/py3.12 双定向复验）
- 补充（第三条失败的处置）：上条 py3.12 用例经核对是**旧期望过期**：`object_state._name_branch` 的最终契约（R9-H13 §3.B3，代码注释与 H13 报告 B1-B3「对象判定过严纠正」一致）明确「同名入口解析到**不同的真实对象**时按当前模式继续」，而旧用例断言「同名即拒绝」，且在 py3.11/Windows 上因为**副本跑不起来**（Python 解释器被复制到别的目录）而假绿。
  处置（只改测试，不改产品实现）：被测程序改为一定可运行的系统 shell 副本（Windows `cmd.exe` / POSIX `/bin/sh`）并用 marker 证明真的执行；断言改为身份语义——`binding.identity/path` 必须等于 A 的真实身份、按绝对路径跑 A 一律被拒（重启后仍被拒）、按名字落到 B 时允许且必须证明执行的是 B、绑定不得被改写成 B。
  复验：候选 `880880be` 上 `targeted-verify` 双版本单跑 `tests/test_r9h9_regressions.py` —— **py3.12 success（run 40201799979）+ py3.11 success（run 40201811835）**；本地该文件 8 项通过。不再有「假绿」，也不再与契约冲突。
- 说明：本轮完整 CI 轮次只有这一次（9/10 job 绿 + 上述单条基线失败）；未使用 `Re-run failed jobs` 冒充新提交通过，也未降低任何门禁。

（回填：运行号、SHA、逐 job 结论、失败与定向复验）

### 6.5 最终候选状态

- 完整 CI 验证的代码候选：`97b983682def71cbe5bb1d45bb41f3560f7504ed`（run 40196935297，9/10 job 绿）。
- 失败修复后的定向验证候选：`880880be81da439ce9e7f3573ecaa91e19dc1b1e`（py3.12 + py3.11 双定向 success，run 40201799979 / 40201811835）。
- **最终提交（本轮 main 指向）**：见文末「交付状态」一节；它与 `880880be` 的差异仅为本说明文档的补充。
- 三个冻结来源 + 追加交付均为其祖先（`git merge-base --is-ancestor` 实测）
- 工作树干净、全树无冲突标记；`backend` 子树在 854d9c39→c56b7033 之间未变（`git rev-parse <sha>:backend` 相同）
## 7. 已知限制与未验证事项

### 7.1 未验证（如实记录，不用浏览器预览冒充）

- **真机桌面壳**：Tauri `CloseRequested → tray/quit/ask`、托盘菜单与恢复、`request_close` / `qio_set_close_behavior` / `qio_close_ack` 三个 IPC、`POST /api/lifecycle/shutdown` 的有界收尾与 `taskkill /PID /T` 核验、ask 的 1.8s ACK 兜底、安装形态 `install_e2e`。
  本轮**没有**编译候选壳（时间预算与无人值守交互限制），只在组件级（mock Tauri）与源码级协议不变量上验证。
- **真实模型/真实厂商链路**：全部模型调用使用 fake/mock provider；离线评估用的也是离线夹具。
- **系统回收站 / 符号链接 / 硬链接 / 跨卷移动**的真实文件系统行为：只有仓库既有 Windows 用例覆盖，本轮未新增真机证据。
- **POSIX 平台**：本轮本机验证全部在 Windows；CI 会覆盖 Linux 两个 Python 版本，但那是 CI 的事实，不是本地结论。

### 7.2 已知限制（非阻塞）

- 批量审批：v2 生命周期路径只有「整批一次决定」，逐项成败在结果文本/事件里，前端没有逐项列表（两端原本都没有批量 UI，本轮不扩大范围）。
- 聊天页没有「执行结果历史」入口：R9 的执行结果历史仍在非聊天路由的 `ApprovalModal` 里（按路由门控，聊天页由统一弹窗承载）。
- v2 生命周期审批的子智能体预算只读展示：`/decide` 不支持 `overrides`，本轮不新增后端参数。
- 页面来源库升级时，52/53/57/68/69 的 ADD COLUMN 预检会按既有「半截迁移自愈」路径打 warning（对象已存在 → 跳过），属日志噪声不是错误。
- 迁移号 54/56 保持空号（历史语义），后续新迁移从 71 起。
- 统一审批弹窗在窄窗口（480px）下，≥3 条待审批时第 3 个类型标签右缘被裁切（`overflow-x:auto` 可横向滚动但无提示）；不影响提交，属 P2 可及性问题。
- 审批弹窗里「状态 / 期限」在事实框与 lifecycle 行各显示一次（重复但不矛盾）；同类型 3 条待审批的队列标签文案无法互相区分（可用性建议）。

### 7.3 基线既有问题与本轮处置（另一批）

- `test_r9h3_identity_refresh.py` 6 条、`test_r9h_content_binding.py` 1 条在冻结基线 `r9h13/main` 上就是红的（H13 §8 三次确认、H8 §6 `script_sha256` 语义变更后旧期望未修订）。
  本轮按契约修订期望（**收紧而非放宽**：新增 0/1/2 次确认全部被拒的负向断言、新增 `operation_fingerprint` 不等断言），未以「原来就红」通过。
- `test_p3_d_intent_resume_retry` 的迁移号断言随页面线迁移 56 → 69 更新，并断言 56 空号。
- H13 报告把 `test_r9h_content_binding` 归因为「Windows 共享 venv junction 环境」，实测不成立（`script_sha256` 恒为 None 与 venv 无关），按 H8 §6 语义修正；该归因错误记录在此，不回写历史报告。

## 8. 交付状态与回退路径

- **本轮最终提交（main 指向）**：本文件所在提交（`git -C <worktree> rev-parse HEAD`）。
- **完整 CI 验证候选**：`97b983682def71cbe5bb1d45bb41f3560f7504ed`（run 40196935297：9/10 job 绿，唯一失败为 py3.12 单条基线用例）。
- **失败修复后定向验证候选**：`880880be81da439ce9e7f3573ecaa91e19dc1b1e`（py3.12 / py3.11 双定向 success）。
- **最终提交与上述候选的关系**：`880880be` 之后只增加了本说明文档的补充（纯文档），代码与测试树与 `880880be` 一致；文档改动用 `python scripts/check_docs.py` 复验。
- **操作前 main**：`249a63443c72b3dad011b5446ee9920f4dadf0fd`。
- **代码回退路径**：把 main 指回操作前提交即可（`git push --force-with-lease origin 249a6344…:main`）。
  但**数据库升级不可随代码一起回退**：本轮迁移把 `SCHEMA_VERSION` 从 51 推进到 70（新增 52…70 的对象与页面线对象），
  旧版本的二进制读到 70 版库不会自动降级。回退到旧代码前必须先用**升级前的数据目录备份**恢复数据，
  不能只切代码。回退前请先 `apply_migrations` 之前的备份存在。
- **远端记录**：整合分支 `origin/integrate/three-branches-20261025`；用于定向验证的临时 tag `integ3-candidate-2`（指向 `880880be`，可删除）。
