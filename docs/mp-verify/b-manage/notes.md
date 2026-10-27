# B 组验证笔记（已知限制 / 与本次无关的观察）

## 已知限制（诚实记录）

1. **`MemoryPendingPanel.vue` 仍是基线实现**：本次交付的 `PendingItems.vue` / `MaintenanceRecords.vue`
   取代了它的两个视图，但 B 不修改 A 独占的 `PlanetView.vue`，所以旧文件暂时保留（避免集成时
   旧挂载点编译失败）。A 的 PlanetView 切到新组件后，`MemoryPendingPanel.vue` 与它的两份测试
   （`MemoryPendingPanel.test.ts` / `uf-maintenance-restore-dialog.test.ts`）可以一并删除。
2. **实体卡的 `pending_relations` 没有读写端点**：`/api/entities/candidates` 只暴露
   `field_meta.pending`。所以「待处理」的数量与清单只统计可处理的两类（记忆待确认 + 实体字段候选），
   不把用户无法处理的 `pending_relations` 计成一个假待办。要做需要后端补一个「确认关系目标」端点，
   超出本次「最小后端补齐」的范围。
3. 截图时的面板顶部 8px 被全局栏压住：基线 PlanetView 没有 `.planet-content` 包裹层（A 的改造负责），
   B 没有改 `PlanetView.vue`。截图里这是基线布局的已知现象。
4. `pendingStore.refresh()` 在面板写入成功后与挂载时各触发一次；
   A 若在角标上需要更即时，可在自己的事件里再调一次（接口幂等、并发单飞）。

## 与本次无关、但值得记录的观察

- `scripts/visual_probe.mjs` 在没有 pinia / 首屏引导未完成时无法取到面板：引导层会挡住整页
  （截图前需要 `POST /api/onboarding/complete`），验证脚本可考虑显式处理这一点。
- 维护批次列表接口的 `available=false` 分支在本次开发库里没有真实触发（迁移 47 已应用），
  只用单测覆盖了「不可用不伪装成空数据」的文案分支。
