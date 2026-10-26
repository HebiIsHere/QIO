# 合并后遗留问题修复（P91）— 2026-10-26

> 修复分支：fix/post-merge-followups-20261026（未合入 main、未发布）。
> 基线：origin/fix/post-merge-integration-cleanup-20261025 = 373fda40bf904ea0c715d8560fda71c58a6bc051。
> 状态：进行中（收口时回填结论、SHA、CI 运行号）。

## 0. 给用户的说明（非技术）

<收口回填：修复了什么、如何验证、还有什么未解决>

## 1. 基线与分支

| 项目 | 值 |
| --- | --- |
| 仓库 | HebiIsHere/QIO |
| 开工 fetch 的上游分支 | fix/post-merge-integration-cleanup-20261025 = 373fda40 |
| origin/main | 1b03541e（未前进；本轮不碰 main） |
| 修复分支 | fix/post-merge-followups-20261026 |
| 受测代码 SHA | <收口回填> |
| 最终 HEAD | <收口回填> |

## 2. 工作流与文件 owner

| 工作流 | 分支 | worktree | 主要交付 |
| --- | --- | --- | --- |
| A 话题归属 | fix/fu2-topic-attribution | D:\qio-dev\fixq2-a | 确认/恢复/重试原话题优先、无法确认明确失败 |
| B 审批后端 | fix/fu2-approval-backend | D:\qio-dev\fixq2-b | legacy 身份、/respond 路由、执行结果持久化历史、overrides 语义 |
| C 审批前端 | fix/fu2-approval-frontend | D:\qio-dev\fixq2-c | 重连补读合并、结果闭环展示、预算只读/可改一致 |
| D 通知投递 | fix/fu2-notification-delivery | D:\qio-dev\fixq2-d | 持久化队列、可靠投递、重启恢复、去掉 200 静默丢弃 |
| E 文档许可 | docs/fu2-docs-license | D:\qio-dev\fixq2-e | 阶段状态更正、P91 记录、许可证概括、证据 |
| F 独立验收 | verify/fu2-acceptance | D:\qio-dev\fixq2-f | 基线反例 + 候选独立验收 |
| Lead | fix/post-merge-followups-20261026 | D:\qio-dev\fixq2-lead | 契约冻结、集成、全量、CI、本文档 |

## 3. 五项修复逐项

### 3.1 互动续作的话题归属
<收口回填：问题/反例/修复位置/验证/未解决>
### 3.2 旧审批恢复与预算
<收口回填>
### 3.3 审批结果闭环
<收口回填>
### 3.4 子任务通知容量与可靠投递
<收口回填>
### 3.5 文档与许可证
<收口回填>

## 4. 验证台账

### 4.1 反例（基线红）
<收口回填>
### 4.2 定向验证（开发期）
<收口回填>
### 4.3 本地全量（仅一次）
<收口回填：SHA、命令、结果、失败处置>
### 4.4 完整 CI（仅一次）与定向复验
<收口回填：运行号、SHA、逐 job>
### 4.5 真实界面/桌面证据
<收口回填：如未验证如实列出>

## 5. 保留机制与保护清单
<收口回填：三次确认/期限/CAS/单次执行/执行前重核/旧 respond/历史迁移未改写 等>

## 6. 未解决与未验证
<收口回填>

## 7. 合并准备
<收口回填：未来 main 合并的 CI 处理、PR 材料、数据库兼容性>
