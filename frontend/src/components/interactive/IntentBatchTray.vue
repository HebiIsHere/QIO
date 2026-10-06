<!-- 右上批量列表（子智能体 D 负责实现；这里是骨架占位）。 -->
<script setup lang="ts">
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();
</script>

<template>
  <div v-if="store.listBatches.length" class="batch-tray-placeholder">
    <button
      class="entry"
      type="button"
      data-im="batch-entry"
      :aria-expanded="store.batchOpen"
      @click="store.batchOpen = !store.batchOpen"
    >
      待审批 {{ store.listBatches.reduce((sum, batch) => sum + batch.pendingIds.length, 0) }}
    </button>
    <div v-if="store.batchOpen" class="panel" data-im="batch-list">
      <p class="hint">同一批等待审批达到四项时的批量列表（由子智能体 D 接管）。</p>
    </div>
  </div>
</template>

<style scoped>
.batch-tray-placeholder {
  position: absolute;
  right: var(--sp-4);
  top: var(--sp-3);
  z-index: 26;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-2);
}
.entry {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: var(--sp-1) var(--sp-4);
  cursor: pointer;
}
.panel {
  width: min(360px, 88vw);
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
}
.hint { margin: 0; font-size: var(--fs-sm); color: var(--text-secondary); }
</style>
