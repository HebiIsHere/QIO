# 合并后修复与旧代码清理（P90）— 2026-10-26

> 本轮修复分支：fix/post-merge-integration-cleanup-20261025（未合入 main）。
> 基线：origin/main 1b03541eedd2dcaa9b82d6102108d717c71c6fb4。
> 状态：进行中（本文在收口时回填完整状态）。

## 0. 给用户的说明（非技术）

<收口时填写：用户现在能从哪里操作新增能力、批准后在哪里看最终结果、哪些旧代码被清理、哪些问题仍存在。>

## 1. 基线与工作分支

| 项目 | 值 |
| --- | --- |
| 仓库 | HebiIsHere/QIO |
| 开工 fetch 的 origin/main | 1b03541eedd2dcaa9b82d6102108d717c71c6fb4 |
| 本分支 | fix/post-merge-integration-cleanup-20261025 |
| 本分支 HEAD | <收口回填> |
| 上一轮整合 | run 40196935297 / SHA 97b98368；定向复验 880880be；文档 docs/integration-3way-20261025.md |

## 2. 工作流、文件 owner 与分支

| 工作流 | 分支 | worktree | 主要交付 |
| --- | --- | --- | --- |
| Lead | fix/post-merge-integration-cleanup-20261025 | D:\qio-dev\fixq-lead | 冻结契约、能力对应表、集成、全量、CI、文档、推送 |
| A 审批与结果 | fix/pm-a-approval-results | D:\qio-dev\fixq-a | F02/F03/F04/F05（审批弹窗部分）/F06 |
| B 互动与几何 | fix/pm-b-interactive-mode | D:\qio-dev\fixq-b | F01/F05（互动部分）/C01 生产者/C02 + 旧互动能力迁移 |
| C 代码清理 | fix/pm-c-legacy-cleanup | D:\qio-dev\fixq-c | 引用图、17 旧文件、C03/C04/C05、旧测试清理 |
| D 桌面与工程记录 | fix/pm-d-desktop-ci-docs | D:\qio-dev\fixq-d | F07、CI 防重复、D01/D02 |
| E 独立验收 | verify/pm-e-acceptance | D:\qio-dev\fixq-e | 独立验收、反例、真实界面证据 |

共享文件唯一 owner（冻结）：

| 文件 | owner |
| --- | --- |
| frontend/src/App.vue | A |
| frontend/src/stores/approvals.ts | A |
| frontend/src/stores/unifiedApprovals.ts | A |
| frontend/src/components/approval/**、ApprovalFacts.vue、ApprovalModal.vue、ApprovalEntry.vue | A |
| frontend/src/components/conversation/UnifiedApprovalLayer.vue、ComposerEntries.vue | A |
| frontend/src/views/InteractiveView.vue | B |
| frontend/src/components/interactive/** | B |
| frontend/src/stores/interactive.ts | B |
| frontend/src/interactive/**（几何/布局） | B |
| frontend/src/stores/security.ts、PermissionModeBadge.vue | B |
| 17 个旧文件删除、styles/interactive-shell.css、styles/layers.css、main.ts | C |
| components/interactive/v2/intentDisplay.ts、components/ui/QSlider.vue、QioScrollArea.vue、services/interactive.ts 成员 | C |
| .github/workflows/**、docs/integration-3way-20261025.md、frontend/src-tauri/** | D |
| docs/status.md、docs/architecture.md、本文 | Lead |

## 3. 后端能力 → 前端入口对应表

### 3.1 用户操作型（必须有可达入口）

| 后端能力 | 所属页面 | 实际入口 | 接口/事件 | 权威状态 | 成功/失败反馈 | 刷新与重启 | 验证证据 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| <收口回填> | | | | | | | |

### 3.2 结果展示型（不新增按钮但结果必须可见）

| 后端能力 | 所属页面 | 实际入口 | 接口/事件 | 权威状态 | 成功/失败反馈 | 刷新与重启 | 验证证据 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 审批执行结果（executed/failed/invalidated/expired/interrupted_unverified） | | | | | | | |
| 批量逐项结果（item_id 级） | | | | | | | |
| 待审批恢复失败 | | | | | | | |
| 审批历史读取失败 | | | | | | | |

### 3.3 后台自动型（不新增按钮）

| 能力 | 生效方式 | 验证证据 |
| --- | --- | --- |
| 身份核验/防重复执行/迁移等 | | |

## 4. F01–F07 逐项状态

<收口回填：每项 状态/修改位置/验证/未完成原因>

## 5. C01–C05 逐项状态

<收口回填>

## 6. D01–D02 记录更正

<收口回填>

## 7. 删除清单

<收口回填：每个被删文件/符号/状态/样式/测试的理由、原消费者、替代位置、验证结果；必要保留的兼容代码及真实消费者>

## 8. 旧行为断言迁移对照

<收口回填：旧测试断言 → 新验收位置>

## 9. 验证台账

### 9.1 定向验证（开发期，非全量）

<收口回填>

### 9.2 本地全量轮次（仅一次）

<收口回填：SHA、命令、结果、失败处置>

### 9.3 完整 CI 轮次（仅一次）与定向复验

<收口回填：运行号、SHA、逐 job、筛选>

### 9.4 真实界面与桌面验证

<收口回填：窄窗 480x600、执行结果、批量部分失败、三次确认、桌面壳验证与边界>

## 10. 未验证与边界

<收口回填>

## 11. 合并准备

<收口回填：未来合入 main 如何避免重复触发 CI、PR 材料、数据库兼容性>
