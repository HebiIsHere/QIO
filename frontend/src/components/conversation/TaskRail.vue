<script setup lang="ts">
/**
 * 左侧任务栏（A 维护）。
 *
 * 只显示后端真实记录的工作：运行/排队/需要处理的任务与历史记录；统一平铺、不按父子
 * 嵌套、不分区。当前/历史两个视图各自保留滚动与展开状态；需要处理置顶；动作只来自
 * 后端 supported 的清单。数据与状态见 stores/conversationTasks.ts。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { useConversationShellStore } from "../../stores/conversationShell";
import { useConversationTasksStore } from "../../stores/conversationTasks";
import TaskRailItem from "./TaskRailItem.vue";

const props = withDefaults(defineProps<{ drawer?: boolean }>(), { drawer: false });
const emit = defineEmits<{ close: [] }>();

const shell = useConversationShellStore();
const tasks = useConversationTasksStore();

const listRef = ref<HTMLElement | null>(null);
const highlightId = ref("");
let highlightTimer: ReturnType<typeof setTimeout> | null = null;

const view = computed(() => shell.prefs.taskTab);
const state = computed(() => tasks.view(view.value));
const items = computed(() => tasks.displayItems(view.value));
const taskAttention = computed(() => tasks.taskAttentionCount);
const approvalCount = computed(() => tasks.approvalCount);
const activeCount = computed(() => tasks.activeCount);
const summaryText = computed(() => {
  const parts: string[] = [];
  if (view.value !== "current") return "";
  if (taskAttention.value > 0) parts.push(taskAttention.value + " 项需要处理");
  if (approvalCount.value > 0) parts.push(approvalCount.value + " 项待审批");
  if (activeCount.value > 0) parts.push(activeCount.value + " 项运行或排队");
  return parts.join(" · ");
});
const showEmpty = computed(() => state.value.loaded && !state.value.error && items.value.length === 0);
const firstLoading = computed(() => !state.value.loaded && !state.value.error);
const errorWithoutItems = computed(
  () => Boolean(state.value.error) && items.value.length === 0 && !state.value.loading,
);

function flashHighlight(id: string) {
  highlightId.value = id;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = setTimeout(() => {
    highlightTimer = null;
    highlightId.value = "";
  }, 1800);
}

function rowElement(id: string): HTMLElement | null {
  const root = listRef.value;
  if (!root) return null;
  const rows = root.querySelectorAll("[data-task-id]");
  for (const row of Array.from(rows)) {
    if ((row as HTMLElement).dataset.taskId === id) return row as HTMLElement;
  }
  return null;
}

function restoreScroll() {
  const root = listRef.value;
  if (!root) return;
  root.scrollTop = tasks.scrollTop[view.value] || 0;
}

async function switchTab(tab: "current" | "history") {
  if (tab === view.value) return;
  if (listRef.value) tasks.setScroll(view.value, listRef.value.scrollTop);
  await tasks.switchView(tab);
  await nextTick();
  restoreScroll();
}

function onListScroll() {
  if (!listRef.value) return;
  tasks.setScroll(view.value, listRef.value.scrollTop);
}

function refresh() {
  void tasks.refresh(view.value);
}

function loadMore() {
  void tasks.loadMore(view.value);
}

function toggleItem(id: string) {
  tasks.toggleExpanded(view.value, id);
}

function onAction(itemId: string, kind: string) {
  const item = items.value.find((row) => row.id === itemId);
  if (!item) return;
  void tasks.runAction(item, kind);
}

function openApproval(itemId: string) {
  const item = items.value.find((row) => row.id === itemId);
  tasks.openApprovalFor(item);
}

function dismissFeedback() {
  tasks.setFeedback("");
}

watch(view, async () => {
  await nextTick();
  restoreScroll();
});

watch(
  () => tasks.focusTarget,
  async (focus) => {
    if (!focus) return;
    await nextTick();
    const row = rowElement(focus.id);
    if (!row) return;
    row.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    flashHighlight(focus.id);
  },
  { immediate: true },
);

// 聚焦请求可能先于列表数据到达（focusTask 期间在刷新）：行出现后再定位一次。
watch(
  () => items.value.length,
  async () => {
    const focus = tasks.focusTarget;
    if (!focus || highlightId.value === focus.id) return;
    await nextTick();
    const row = rowElement(focus.id);
    if (!row) return;
    row.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    flashHighlight(focus.id);
  },
);

onMounted(async () => {
  void tasks.ensureLoaded();
  await nextTick();
  restoreScroll();
});

onBeforeUnmount(() => {
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = null;
});
</script>

<template>
  <aside class="task-rail" :class="{ 'is-drawer': props.drawer }" aria-label="任务">
    <header class="rail-head">
      <h2 class="rail-title">任务</h2>
      <div class="rail-head-actions">
        <button
          class="icon-btn"
          type="button"
          title="刷新任务列表"
          aria-label="刷新任务列表"
          :disabled="state.loading"
          :aria-busy="state.loading ? 'true' : 'false'"
          @click="refresh"
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M3 6.5a5 5 0 0 1 8.6-2.4L13 5.5" />
            <path d="M13 2.8v2.9H10" />
            <path d="M13 9.5a5 5 0 0 1-8.6 2.4L3 10.5" />
            <path d="M3 13.2v-2.9H6" />
          </svg>
        </button>
        <button
          v-if="props.drawer"
          class="icon-btn"
          type="button"
          title="关闭任务列表"
          aria-label="关闭任务列表"
          @click="emit('close')"
        >
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" /></svg>
        </button>
        <button
          v-else
          class="icon-btn"
          type="button"
          title="收起任务栏"
          aria-label="收起任务栏"
          @click="shell.toggleLeft()"
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <rect x="2" y="3" width="12" height="10" rx="1.5" />
            <path d="M6 3v10" />
          </svg>
        </button>
      </div>
    </header>

    <div class="rail-tabs" role="tablist" aria-label="任务视图">
      <button
        type="button"
        role="tab"
        :aria-selected="view === 'current' ? 'true' : 'false'"
        :class="{ selected: view === 'current' }"
        @click="switchTab('current')"
      >
        当前任务
        <span v-if="taskAttention > 0" class="tab-count mono" data-tone="attention">{{ taskAttention }}</span>
        <span v-else-if="activeCount > 0" class="tab-count mono">{{ activeCount }}</span>
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="view === 'history' ? 'true' : 'false'"
        :class="{ selected: view === 'history' }"
        @click="switchTab('history')"
      >
        历史记录
      </button>
    </div>

    <p v-if="summaryText" class="rail-summary mono">{{ summaryText }}</p>

    <p v-if="tasks.connectionHint" class="rail-warn" role="status">{{ tasks.connectionHint }}</p>
    <p v-if="tasks.focusError" class="rail-warn" role="alert">{{ tasks.focusError }}</p>
    <div v-if="tasks.feedback" class="rail-feedback" role="status">
      <span>{{ tasks.feedback }}</span>
      <button type="button" aria-label="关闭提示" title="关闭提示" @click="dismissFeedback">×</button>
    </div>

    <div
      ref="listRef"
      v-qio-scrollbar
      class="rail-list"
      data-qio-fit
      role="tabpanel"
      :aria-label="view === 'current' ? '当前任务列表' : '历史任务列表'"
      @scroll.passive="onListScroll"
    >
      <p v-if="firstLoading" class="rail-empty" role="status">正在读取任务…</p>

      <div v-else-if="errorWithoutItems" class="rail-error">
        <p role="alert">{{ state.error }}</p>
        <button class="qio-btn quiet mini" type="button" @click="refresh">重试</button>
      </div>

      <template v-else>
        <p v-if="view === 'current' && showEmpty" class="rail-empty">当前没有任务</p>
        <p v-else-if="view === 'history' && showEmpty" class="rail-empty">还没有历史记录</p>

        <p v-if="state.error && items.length" class="rail-warn" role="status">
          {{ state.error }}（下面显示的是上次读取结果）
        </p>

        <div v-for="item in items" :key="item.id" class="rail-row" :data-task-id="item.id">
          <TaskRailItem
            :item="item"
            :expanded="tasks.isExpanded(view, item.id)"
            :detail="tasks.detailFor(item.id)"
            :detail-loading="tasks.detailLoadingFor(item.id)"
            :detail-error="tasks.detailErrorFor(item.id)"
            :notice="tasks.noticeFor(item.id)"
            :retained-note="tasks.retainedNoteFor(item.id)"
            :busy="tasks.actionBusyFor(item.id)"
            :busy-kind="
              tasks.actionBusy.startsWith(item.id + ':') ? tasks.actionBusy.slice(item.id.length + 1) : ''
            "
            :highlighted="highlightId === item.id"
            @toggle="toggleItem(item.id)"
            @action="onAction(item.id, $event)"
            @open-approval="openApproval(item.id)"
          />
        </div>

        <p v-if="state.moreError" class="rail-warn" role="alert">{{ state.moreError }}</p>
        <button
          v-if="state.nextCursor && items.length"
          class="rail-more qio-btn quiet mini"
          type="button"
          :disabled="state.loadingMore"
          :aria-busy="state.loadingMore ? 'true' : 'false'"
          @click="loadMore"
        >
          {{ state.loadingMore ? "正在读取更早的任务…" : "加载更早的任务" }}
        </button>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.task-rail {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--bg-surface);
}
.rail-head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 10px 6px 13px;
}
.rail-title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-md);
  font-weight: 400;
  color: var(--text-strong);
}
.rail-head-actions {
  display: flex;
  align-items: center;
  gap: 2px;
}
.icon-btn {
  width: 26px;
  height: 26px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.icon-btn svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.3;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.icon-btn:hover {
  color: var(--text-strong);
  background: var(--layer-hover);
}
.icon-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.icon-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.rail-tabs {
  flex: none;
  display: flex;
  gap: 3px;
  margin: 0 10px 6px;
  padding: 3px;
  border-radius: var(--r-sm);
  background: var(--bg-inset);
}
.rail-tabs button {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 5px 4px;
  border: 0;
  border-radius: var(--r-xs);
  background: transparent;
  color: var(--text-secondary);
  font-family: inherit;
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.rail-tabs button:hover { color: var(--text-strong); }
.rail-tabs button.selected {
  background: var(--bg-elevated);
  color: var(--text-strong);
  box-shadow: var(--shadow-1);
}
.rail-tabs button:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -1px;
}
.tab-count {
  font-size: 10px;
  color: var(--text-muted);
}
.tab-count[data-tone="attention"] { color: var(--warning); }
.rail-summary {
  flex: none;
  margin: 0 13px 6px;
  font-size: 10.5px;
  color: var(--text-muted);
}
.rail-warn {
  flex: none;
  margin: 0 13px 6px;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--warning);
  overflow-wrap: anywhere;
}
.rail-feedback {
  flex: none;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0 12px 6px;
  padding: 6px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: var(--bg-base);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.rail-feedback span { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.rail-feedback button {
  flex: none;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;
  padding: 2px;
}
.rail-feedback button:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.rail-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  overscroll-behavior: contain;
  padding: 2px 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.rail-empty {
  margin: 12px 3px;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.rail-error {
  margin: 10px 3px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  font-size: var(--fs-sm);
  color: var(--danger);
  line-height: 1.7;
}
.rail-error p { margin: 0; overflow-wrap: anywhere; }
.rail-row { min-width: 0; }
.rail-more {
  align-self: center;
  margin: 6px 0 2px;
}
.is-drawer .rail-list { padding-bottom: 18px; }
</style>
