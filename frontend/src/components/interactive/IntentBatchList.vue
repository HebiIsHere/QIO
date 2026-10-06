<!--
  批量审批的简洁列表（子智能体 C）。

  契约 §1.6：同一批达到 4 项时提供简洁列表，支持选择部分或全部、
  批量批准或拒绝、点击定位；未选中的继续等待。
-->
<script setup lang="ts">
import { computed } from "vue";
import {
  approveAvailability,
  batchCandidates,
  batchSummary,
  clearBatchSelection,
  selectAllBatch,
  splitBatchDecision,
  statusText,
  toggleBatchSelection,
} from "../../interactive/approval";
import type { Intent } from "../../interactive/types";

const props = defineProps<{ intents: Intent[]; selected: string[] }>();

const emit = defineEmits<{
  (event: "update:selected", value: string[]): void;
  (event: "decide", payload: { approve: string[]; reject: string[] }): void;
  (event: "locate", intentId: string): void;
}>();

const candidates = computed(() => batchCandidates(props.intents));
const summary = computed(() => batchSummary(props.intents, props.selected));
const approveSplit = computed(() => splitBatchDecision(props.intents, props.selected, "approve"));
const rejectSplit = computed(() => splitBatchDecision(props.intents, props.selected, "reject"));
const blockedMap = computed(() => {
  const map = new Map<string, string>();
  for (const item of approveSplit.value.blocked) map.set(item.id, item.reason);
  return map;
});

function statusLabel(intent: Intent): string {
  return statusText(intent).label;
}

function toggle(intentId: string): void {
  emit("update:selected", toggleBatchSelection(props.selected, intentId));
}

function selectAll(): void {
  emit("update:selected", selectAllBatch(props.intents));
}

function clearAll(): void {
  emit("update:selected", clearBatchSelection());
}

function approveSelected(): void {
  emit("decide", { approve: approveSplit.value.ids, reject: [] });
}

function rejectSelected(): void {
  emit("decide", { approve: [], reject: rejectSplit.value.ids });
}
</script>

<template>
  <section class="batch" data-im="batch-list">
    <header class="head">
      <h3 class="title">批量审批（{{ candidates.length }} 项）</h3>
      <p class="summary">{{ summary }}</p>
    </header>

    <ul class="rows">
      <li
        v-for="item in candidates"
        :key="item.id"
        class="row"
        data-im="batch-item"
        :data-intent-id="item.id"
        :data-intent-status="item.status"
      >
        <label class="pick">
          <input
            type="checkbox"
            :checked="selected.includes(item.id)"
            :data-intent-id="item.id"
            @change="toggle(item.id)"
          />
          <span class="name" @click="emit('locate', item.id)">{{ item.title }}</span>
        </label>
        <span class="status">{{ statusLabel(item) }}</span>
        <p v-if="blockedMap.get(item.id)" class="blocked">
          {{ blockedMap.get(item.id) }}（仍可单独或批量拒绝）
        </p>
      </li>
    </ul>

    <div class="actions">
      <button
        class="btn primary"
        type="button"
        data-im="batch-approve"
        :disabled="!approveSplit.ids.length"
        @click="approveSelected"
      >
        批量批准（{{ approveSplit.ids.length }}）
      </button>
      <button
        class="btn"
        type="button"
        data-im="batch-reject"
        :disabled="!rejectSplit.ids.length"
        @click="rejectSelected"
      >
        批量拒绝（{{ rejectSplit.ids.length }}）
      </button>
      <button class="btn ghost" type="button" data-im="batch-all" @click="selectAll">全选</button>
      <button class="btn ghost" type="button" data-im="batch-clear" @click="clearAll">清空</button>
    </div>
    <p class="hint">
      未选中的项继续等待，不会被处理；所有判定（冲突、依赖、材料变化）都由服务端给出结果。
    </p>
  </section>
</template>

<style scoped>
.batch {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  background: var(--bg-inset);
}
.head {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.summary,
.hint,
.blocked {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: var(--lh-base);
}
.hint { color: var(--text-faint); }
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border-subtle);
  padding-bottom: var(--sp-1);
}
.pick {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 1;
  min-width: 0;
  cursor: pointer;
}
.name {
  font-size: var(--fs-xs);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.name:hover { color: var(--link); }
.status {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  white-space: nowrap;
}
.blocked {
  flex-basis: 100%;
  color: var(--danger);
  padding-left: var(--sp-4);
}
.actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-3);
  cursor: pointer;
}
.btn.primary {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
}
.btn.primary:hover:enabled { background: var(--accent-hover); }
.btn.ghost { background: none; color: var(--link); }
.btn:disabled { opacity: 0.55; cursor: default; }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
</style>
