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

（见 §6.1 定向 / §6.2 一次本地全量轮次 / §6.3 一次完整 CI 轮次 —— 由收口时回填真实运行号与结论。）

### 6.1 定向验证（按工作流）

（回填）

### 6.2 本地全量轮次（仅一次）

（回填：后端 `uv run --frozen pytest`、前端 `npx vitest run`、`vue-tsc --noEmit`、生产构建、文档/许可检查、离线评估）

### 6.3 完整 CI 轮次（仅一次）

（回填：运行号、SHA、逐 job 结论、失败与定向复验）

## 7. 已知限制与未验证事项

（回填）
