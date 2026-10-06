<!--
  右上角批量审批入口（子智能体 D）。

  契约 §8.1 / §8.5：
  - **只有同一批**等待审批的意图 ≥4 时出现；默认收起，由用户点击展开；
    数量变化不抢占用户的开合决定（这里不写任何 watch 去自动展开 / 收起）；
  - 不同批次的未处理意图**不累加**：判定在 interactive/approval.ts 的 batchesWithList；
  - 列表简洁展示说明，点击条目定位到板面上的虚线预览
    （window 事件 qio:interactive:locate-preview，detail {intentId, bounds}）；
  - 支持选择部分或全部、批量批准 / 拒绝；未选中的继续等待审批；
  - 冲突、依赖、材料变化与权限都由服务端判定，这里只显示结果，不自己下结论。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  batchesWithList,
  batchEntryText,
  groupIntentsByBatch,
  batchSummaryIn,
  clearBatchSelectionIn,
  locatePreview,
  previewBounds,
  pruneBatchSelection,
  selectAllInBatch,
  splitBatchIn,
  statusText,
  toggleBatchSelectionIn,
  type IntentBatch,
} from "../../interactive/approval";
import type { Intent } from "../../interactive/types";

const store = useInteractiveStore();

/** 同一批 ≥4 项才需要列表；不同批次不合并 */
const batches = computed(() => batchesWithList(store.intents));
/** 每一批的选中项各自独立（键 = 批次 key） */
const selectedByBatch = ref<Record<string, string[]>>({});
const busy = ref(false);
const notice = ref<string | null>(null);
const error = ref<string | null>(null);

/**
 * 面板可用的最大高度：不能越过底部悬浮工具栏，入口与提交区必须始终可见可点。
 *
 * 优先用页面壳维护的 --im-toolbar-clearance；拿不到就自己量页面上**最下面那条**工具栏
 * （[role="toolbar"]，即页面壳底部工具栏）的顶边；工具栏由别的组件渲染，这里只读不写。
 */
const panelMax = ref<string | null>(null);

function measureClearance(): void {
  if (typeof document === "undefined") return;
  // 取最下面那条工具栏（页面壳底部工具栏）：面板不许越过它；拿不到就退回 CSS 变量兜底
  const bars = Array.from(document.querySelectorAll('[role="toolbar"]'));
  let bottom: Element | null = null;
  for (const el of bars) {
    if (!bottom || el.getBoundingClientRect().top > bottom.getBoundingClientRect().top) bottom = el;
  }
  const clearance = bottom
    ? Math.round(Math.max(0, window.innerHeight - bottom.getBoundingClientRect().top) + 16)
    : null;
  panelMax.value = "min(60vh, calc(100% - " + (clearance ?? 120) + "px - var(--sp-4)))";
}

const onResize = () => window.setTimeout(measureClearance, 0);

onMounted(() => {
  measureClearance();
  window.addEventListener("resize", onResize);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onResize);
});

/** 入口只说明「这一批」与「其他批次还等着多少」，不把不同批次相加 */
function entryText(batch: IntentBatch): string {
  const other = groupIntentsByBatch(store.intents)
    .filter((item) => item.key !== batch.key)
    .reduce((sum, item) => sum + item.pendingIds.length, 0);
  return batchEntryText(batch, other);
}

function selectedFor(batch: IntentBatch): string[] {
  return selectedByBatch.value[batch.key] ?? [];
}

function setSelected(batch: IntentBatch, ids: string[]): void {
  selectedByBatch.value = { ...selectedByBatch.value, [batch.key]: ids };
}

function statusLabel(intent: Intent): string {
  return statusText(intent).label;
}

function approveSplit(batch: IntentBatch) {
  return splitBatchIn(store.intents, batch, selectedFor(batch), "approve");
}

function rejectSplit(batch: IntentBatch) {
  return splitBatchIn(store.intents, batch, selectedFor(batch), "reject");
}

function blockedReason(batch: IntentBatch, intentId: string): string {
  return approveSplit(batch).blocked.find((item) => item.id === intentId)?.reason ?? "";
}

function toggle(batch: IntentBatch, intentId: string): void {
  setSelected(batch, toggleBatchSelectionIn(batch, selectedFor(batch), intentId));
}

function selectAll(batch: IntentBatch): void {
  setSelected(batch, selectAllInBatch(batch));
}

function clearAll(batch: IntentBatch): void {
  setSelected(batch, clearBatchSelectionIn());
}

/** 点击条目：在板面上定位这项虚线预览（沿用 A 的 window 事件） */
function locate(intentId: string): void {
  const intent = store.intentById(intentId);
  locatePreview(intentId, intent ? previewBounds(intent.preview) : null);
}

async function decide(batch: IntentBatch, decision: "approve" | "reject"): Promise<void> {
  const split = decision === "approve" ? approveSplit(batch) : rejectSplit(batch);
  if (!split.ids.length) {
    notice.value = "没有可提交的项：未选中的继续等待审批。";
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decideBatch(
      decision === "approve" ? split.ids : [],
      decision === "reject" ? split.ids : [],
    );
    const failed = (result.results ?? []).filter((item) => !item.ok);
    notice.value =
      (decision === "approve" ? "批量批准" : "批量拒绝") +
      "：批准 " +
      result.approved.length +
      " 项，拒绝 " +
      result.rejected.length +
      " 项；未选中的继续等待。" +
      (failed.length
        ? "未成功 " +
          failed.length +
          " 项（" +
          failed.map((item) => item.detail ?? item.reason ?? "服务端未说明").join("；") +
          "）。"
        : "");
    clearAll(batch);
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

/**
 * 只做清理：把已经不在这一批里的选择去掉。
 * 这是「不改变用户选择」的修剪，不做展开 / 收起，也不因为数量变化就重置选择。
 */
watch(
  batches,
  (list) => {
    const next: Record<string, string[]> = {};
    for (const batch of list) {
      const kept = pruneBatchSelection(batch, selectedByBatch.value[batch.key] ?? []);
      if (kept.length) next[batch.key] = kept;
    }
    selectedByBatch.value = next;
  },
  { deep: false },
);
</script>

<template>
  <div v-if="batches.length" class="batch-tray" data-im="batch-area">
    <!-- 入口默认收起：只有用户点击才展开 -->
    <button
      v-for="batch in batches"
      :key="batch.key"
      class="entry"
      type="button"
      data-im="batch-entry"
      :data-batch-key="batch.key"
      :aria-expanded="store.batchOpen"
      aria-controls="im-batch-list"
      @click="store.batchOpen = !store.batchOpen"
    >
      <span class="entry-title">批量审批</span>
      <span class="entry-count mono">{{ entryText(batch) }}</span>
      <span class="entry-arrow" aria-hidden="true">{{ store.batchOpen ? "收起" : "展开" }}</span>
    </button>

    <section
      v-if="store.batchOpen"
      id="im-batch-list"
      class="panel"
      data-im="batch-list"
      :style="panelMax ? { maxHeight: panelMax } : undefined"
    >
      <header class="panel-head">
        <h2 class="panel-title">同一批的待审批条目</h2>
        <button class="close" type="button" data-im="batch-close" @click="store.batchOpen = false">
          收起
        </button>
      </header>
      <p class="hint" role="note">
        只列同一批产生的条目（不同批次不合并）。勾选部分或全部后批量批准 / 拒绝；未选中的继续等待审批。
        能不能批准由服务端判定：互不相容、等待前项、材料已变化都会在这里显示结果。
      </p>
      <p v-if="notice" class="notice" role="status" data-im="batch-notice">{{ notice }}</p>
      <p v-if="error" class="error" role="alert" data-im="batch-error">批量操作失败：{{ error }}</p>

      <div v-for="batch in batches" :key="batch.key" class="batch-block" :data-batch-key="batch.key">
        <p class="batch-head mono">{{ entryText(batch) }}</p>
        <p class="summary">{{ batchSummaryIn(store.intents, batch, selectedFor(batch)) }}</p>

        <ul class="rows">
          <li
            v-for="intentId in batch.pendingIds"
            :key="intentId"
            class="row"
            data-im="batch-item"
            :data-intent-id="intentId"
          >
            <label class="pick">
              <input
                type="checkbox"
                :checked="selectedFor(batch).includes(intentId)"
                :data-intent-id="intentId"
                :disabled="busy"
                @change="toggle(batch, intentId)"
              />
              <span class="name">{{ store.intentById(intentId)?.title ?? intentId }}</span>
            </label>
            <span class="status">
              {{ store.intentById(intentId) ? statusLabel(store.intentById(intentId) as Intent) : "状态未知" }}
            </span>
            <button
              class="locate"
              type="button"
              data-im="batch-locate"
              :data-intent-id="intentId"
              @click="locate(intentId)"
            >
              定位预览
            </button>
            <p v-if="blockedReason(batch, intentId)" class="blocked">
              {{ blockedReason(batch, intentId) }}（仍可以批量拒绝）
            </p>
          </li>
        </ul>

        <div class="actions">
          <button
            class="btn primary"
            type="button"
            data-im="batch-approve"
            :data-batch-key="batch.key"
            :disabled="busy || !approveSplit(batch).ids.length"
            @click="decide(batch, 'approve')"
          >
            批量批准（{{ approveSplit(batch).ids.length }}）
          </button>
          <button
            class="btn"
            type="button"
            data-im="batch-reject"
            :data-batch-key="batch.key"
            :disabled="busy || !rejectSplit(batch).ids.length"
            @click="decide(batch, 'reject')"
          >
            批量拒绝（{{ rejectSplit(batch).ids.length }}）
          </button>
          <button class="btn ghost" type="button" data-im="batch-all" @click="selectAll(batch)">
            全选
          </button>
          <button class="btn ghost" type="button" data-im="batch-clear" @click="clearAll(batch)">
            清空选择
          </button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 右上角：与右下聊天、顶部身份栏错开，不遮挡板面中央 */
.batch-tray {
  position: absolute;
  right: var(--sp-4);
  top: var(--sp-4);
  z-index: 30;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-2);
}
.entry {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font: inherit;
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: var(--sp-1) var(--sp-4);
  cursor: pointer;
  box-shadow: 0 8px 22px var(--shadow-soft, rgba(0, 0, 0, 0.28));
}
.entry:hover { border-color: var(--accent); }
.entry-title {
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.entry-count { font-size: var(--fs-xs); color: var(--text-secondary); }
.entry-arrow { font-size: var(--fs-xs); color: var(--link); }
.panel {
  width: min(400px, calc(100vw - var(--sp-6)));
  max-height: min(60vh, 520px);
  overflow: auto;
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.panel-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}
.panel-title {
  margin: 0;
  flex: 1;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
}
.close {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.hint,
.notice,
.error,
.summary,
.blocked,
.batch-head {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--text-muted);
}
.hint { color: var(--text-faint); }
.notice { color: var(--text-secondary); }
.error { color: var(--danger); }
.batch-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-2);
}
.batch-head { color: var(--text-secondary); }
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
  padding-bottom: var(--sp-1);
  border-bottom: 1px solid var(--border-subtle);
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
  font-size: var(--fs-sm);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.status { font-size: var(--fs-xs); color: var(--text-muted); white-space: nowrap; }
.locate {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.locate:hover { color: var(--accent-hover); }
.blocked { flex-basis: 100%; color: var(--danger); padding-left: var(--sp-4); }
.actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding-top: var(--sp-1);
}
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-surface);
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
.entry:focus-visible,
.btn:focus-visible,
.close:focus-visible,
.locate:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }

@media (max-width: 900px) {
  .batch-tray { right: var(--sp-2); top: var(--sp-2); }
  .panel { width: min(400px, calc(100vw - var(--sp-4))); }
}
</style>
