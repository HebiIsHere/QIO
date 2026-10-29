# 发布前第四轮：死代码清理（2026-10-29）

- 基线：`04c7df1ecf2aedd45d54b0b9d06ee47a97df3538`（分支 `fix/release-step3-settings-reliability-20261029` 尖端，
  即第三轮返修五交付）。开工核对：该提交就是上一轮验收通过的完整交付，前三轮成果全部保留。
- 工作分支：`fix/release-step4-dead-code-cleanup-20261029`
- 范围：只处理本轮指定的七项死代码（旧设置页凭据卡/弹窗、知识候选旧卡、两个旧 composable、
  ui store 的未用状态与旧 action）及其直接受影响的旧测试、占位、滚动策略清单与失效文档指向。
- 不做的事：不合并 main、不打标签、不打包、不发布、不开始下一轮；不改 CI 工作流；不改历史迁移；
  不做无关重构与视觉改版；不引入真实 API Key 或联网测试；不触碰受保护文件（见下）。
- 验证口径：只跑定向前端 vitest、前端类型检查、文档一致性检查与悬空引用终检；
  未跑全量测试、未跑构建，未触发 CI（本轮提交使用仓库支持的 `[skip ci]`）。

## 七项处理表

| 项 | 对象 | 处理 | 引用依据（基线 `04c7df1e`） |
| --- | --- | --- | --- |
| 1 | `frontend/src/views/settings/CredentialCard.vue` | 删除（连同专属测试） | 基线全树唯一 import 是它自己的旧测试 `frontend/src/views/settings/__tests__/CredentialCard.test.ts:3`；设置页现只挂五个分类（`frontend/src/views/SettingsView.vue:37-41`），Key/连接卡由当前模型面板的 `frontend/src/components/models/KeyAccessSection.vue:11`（ConnectionCard）承载。 |
| 2 | `frontend/src/views/settings/CredentialModal.vue` | 删除（连同两个专属测试） | 基线无生产 import：只被两个旧专属测试 import（`CredentialModal.test.ts:8`、`uf2-credential-modal-yield.test.ts:36`）与滚动策略清单（`frontend/src/components/__tests__/gs-scroll-policy.test.ts:81`，基线行号）；当前 Key 弹窗由 `frontend/src/components/models/KeyAccessSection.vue:12` 的 KeyForm + `:14` 的 QModal 承载。 |
| 3 | `frontend/src/components/KnowledgeCandidateCard.vue` | 删除 | 基线只有旧测试占位 `frontend/src/components/conversation/__tests__/ChatColumn.test.ts:56`（`stubs`）与历史文档引用；知识候选现由统一审批层直接 ingest：`frontend/src/components/conversation/UnifiedApprovalLayer.vue:81-90`（读取 `session.knowledgeCandidates`）、`UnifiedApprovalLayer.vue:158` 调用，生产 ChatColumn 已不引用该卡。 |
| 4 | `frontend/src/composables/useComposerClearance.ts` + `frontend/src/composables/__tests__/useComposerClearance.test.ts` | 删除 | 基线除自身测试 `useComposerClearance.test.ts:13` 外无任何 import；输入区底部让位现由 `frontend/src/components/MessageStream.vue:307`、`:311` 订阅共享 `composerMetrics` 统一处理（同文件 `:270` 注释说明），该 composable 是旧的三处各量一遍的残留。 |
| 5 | `frontend/src/composables/usePresence.ts` | 删除 | 基线全树只有定义本身（`usePresence.ts:13`），无任何 import；浮层进出场现由 `frontend/src/components/ui/QModal.vue` 的 `<Transition>`（`:251`、`:301`）与 QConfirm/模态仲裁承担。 |
| 6 | `frontend/src/stores/ui.ts` 的 `settingsDraft` | 删除状态与过期注释 | 基线只有声明 `frontend/src/stores/ui.ts:93`，全树无读写；设置页五个分区常驻、仅 `v-show` 切换（`frontend/src/views/SettingsView.vue:225-255`），草稿随组件生命周期保留，不需要全局草稿。 |
| 7 | `frontend/src/stores/ui.ts` 的 `setCps` | 删除 action | 基线只有定义 `frontend/src/stores/ui.ts:226`，无调用；输出速度已改走 `saveCps`（现 `frontend/src/stores/ui.ts:225`，调用方 `frontend/src/views/settings/sections/RegularSection.vue:124`），它只返回本次接口真实结果、不写界面，语义见第三轮返修五记录。 |

七项结论统一为「删除」：均无生产入口、无动态 `import()`、无现用脚本依赖，只有旧专属测试、
滚动策略清单、旧 ChatColumn 占位与历史文档引用。生产替代关系已在上表逐项给出。

## 旧测试 / mock / 导入 / 失效路径的同步

- 删除 4 个旧专属测试文件：`views/settings/__tests__/CredentialCard.test.ts`、
  `CredentialModal.test.ts`、`uf2-credential-modal-yield.test.ts`、
  `composables/__tests__/useComposerClearance.test.ts`。
- `frontend/src/components/__tests__/gs-scroll-policy.test.ts`：`converted` 清单移除
  `views/settings/CredentialModal.vue`（基线第 81 行），其余清单与断言不变。
- `frontend/src/components/conversation/__tests__/ChatColumn.test.ts`：`stubs` 移除
  `KnowledgeCandidateCard: true`（基线第 56 行），其余 stub 与用例不变。
- 没有为被删组件保留 mock：旧测试使用的 `settingsApiMock` / `settingsTestUtils` 仍被其它设置测试
  使用，未改动。
- `docs/frontend-components.md` §3 / §3.1：按当前真实结构更新为设置页五分类
  （常规 / 模型与服务 / 记忆与整理 / 工具与权限 / 关于与更新）与 `views/settings` 的
  `sections` + `shell` 结构；说明 `CredentialForm.vue` 现仅用于首次引导
  （`frontend/src/components/onboarding/OnboardingWizard.vue`），设置页模型/凭据入口是
  `components/models` 的 `KeyForm` / `KeyAccessSection`；不再提已删除组件。
- `scripts/ui-catalog/README.md` 第 154 行：旧「CredentialModal：create / meta / rotate」待办
  改为指向当前设置页「模型与服务」的 Key 编辑弹窗（KeyForm），同文件其它内容不动。
- `docs/status.md`：顶部新增第四轮核对记录；当前事实段（原第 1802-1803 行）改为
  候选进入统一审批「待确认」、不再有对话流内的候选卡（历史 Implementation / 阶段记录行不改）。
- `docs/architecture.md`：§12.6 的流程图与关键约束、以及能力表（原第 938 行）同步为
  「统一审批待确认」，不再描述对话流内的候选卡。
- 悬空引用终检：`frontend/src` 与 `scripts/` 全树对
  `CredentialCard` / `CredentialModal` / `KnowledgeCandidateCard` / `useComposerClearance` /
  `usePresence` / `settingsDraft` / `setCps` 的检索结果见「定向验证」；`scripts/` 仅剩历史证据日志
  （`scripts/audit-verify-8fa8452/evidence`、`scripts/final-verify-subagent/evidence`）与
  `scripts/ui-catalog/dev-abandon.mjs:1478`/`:1664` 的检查名（该检查验的是「凭据授权确认卡」这个
  审批弹窗，不读取被删组件），均不是 import/路径依赖，按历史记录保留原样。

## 受保护项（本轮未改动）

1. `frontend/src/components/credentials/CredentialForm.vue`（首次引导表单，`OnboardingWizard.vue:16`、`:429` 使用）
   与 `frontend/src/services/credentials.ts`（`useCredentialForm` 在 `:194`）。
2. 当前模型面板：`frontend/src/components/models/`（`ModelServicesPanel` / `KeyAccessSection` /
   `KeyForm` / 默认模型 / 用途分配）。
3. 统一审批链路：`frontend/src/components/conversation/UnifiedApprovalLayer.vue`、
   `frontend/src/components/approval/`。
4. 前三轮的保存与恢复逻辑：`useQioSettingsSave`、`ui.saveCps`、关闭行为、搜索定位与草稿机制。
5. 历史迁移 / 兼容 / 恢复重试逻辑与历史验收记录（历史 docs、迁移文件）。

## 定向验证（实际命令）

工作目录 `frontend/`：

```
npx vitest run src/views/settings src/views/__tests__/SettingsView.test.ts \
  src/views/__tests__/uf-settings-notice-lifecycle.test.ts src/components/credentials \
  src/components/onboarding src/components/models src/components/approval \
  src/components/conversation src/components/__tests__/gs-scroll-policy.test.ts \
  src/stores/__tests__/ui.test.ts src/stores/__tests__/unifiedApprovals.test.ts
npx vue-tsc --noEmit
```

工作目录仓库根：

```
python scripts/check_docs.py
```

结果：

- vitest：exit 0，`Test Files  51 passed (51)`、`Tests  423 passed (423)`（Duration 27.95s）。
- `npx vue-tsc --noEmit`：exit 0（无输出）。
- `python scripts/check_docs.py`：exit 0，输出「文档一致性检查通过（106 个里程碑条目）」。
- 悬空引用终检（grep 全工作树，排除 `.git` / `node_modules`）：`frontend/src` 对七名零命中
  （`useQioSettingsSave.ts` 里的 `SettingsDraftValue` / `SettingsDraftShape` 是既有的分组草稿类型名，
  不是被删的 store 状态）；`scripts/` 只剩历史证据日志与 `dev-abandon.mjs` 的检查名，无 import/路径依赖；
  `docs/` 只剩历史验收/计划/规范与旧截图日志（按「历史记录不改写」保留）。
- 观察（如实记录）：首次把 vitest 与 vue-tsc 并行执行时，
  `src/views/settings/__tests__/settings-search.test.ts` 的「点普通项：进入对应分类、定位并短暂高亮」
  一条用例偶发失败（测试用 40ms 等待两个动画帧，CPU 争用下帧等待超时）；单文件复跑与随后
  串行整套复跑均通过，属测试时序抖动，不是本轮改动引起。
- 未跑全量测试、未跑构建、未运行 CI（提交带 `[skip ci]`，不触发也不重跑工作流）。

## 已知限制

1. 本轮只删除「已确认无入口」的七项，未做更大范围的旧文件盘点；其它历史组件的类似残留不在本轮范围。
2. `scripts/ui-catalog/settings-root.mjs`（`set-cred-modal-create` 等）与 `scripts/ui-catalog/conv*.mjs`
   里的旧凭据/知识候选 scene 在基线（P96 设置页改版、对话候选卡退出后）就已与界面不符，属**既有失效清单**，
   不是本轮删除造成；本轮不清理这些脚本（不扩大范围），它们不在运行时/生产路径上，无生产依赖。
3. `scripts/` 下的历史证据日志与 `dev-abandon.mjs` 的检查名保留原样（历史记录与检查语义不改写）。
4. 全量测试、构建与 CI 本轮未运行；未合并 main、未打标签、未发布。
5. 远端完整 SHA 以交付回执为准。

## 状态

- 七项清理、文档同步与定向验证已完成实现；本轮改动由 Lead 统一提交并推送（所有新增提交带 `[skip ci]`），远端完整 SHA 以交付回执为准。
- 未合并 main、未打标签、未发布、未开始下一轮。
