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
import QIconButton from "../ui/QIconButton.vue";
import TaskRailItem from "./TaskRailItem.vue";

const props = withDefaults(defineProps<{ drawer?: boolean }>(), { drawer: false });
const emit = defineEmits<{ close: [] }>();

const shell = useConversationShellStore();
const tasks = useConversationTasksStore();

const listRef = ref<HTMLElement | null>(null);
const currentTab = ref<HTMLButtonElement | null>(null);
const historyTab = ref<HTMLButtonElement | null>(null);
/** 本轮定位要一起高亮的一组任务（单目标时只有一项）。 */
const highlightIds = ref<string[]>([]);
let highlightTimer: ReturnType<typeof setTimeout> | null = null;
/** 最近一次已定位的请求签名：避免同一请求在数据抖动时反复滚动。 */
let lastFocusKey = "";

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

function flashHighlight(ids: string[]) {
  highlightIds.value = ids;
  if (highlightTimer) clearTimeout(highlightTimer);
  highlightTimer = setTimeout(() => {
    highlightTimer = null;
    highlightIds.value = [];
  }, 1800);
}

/** 当前定位请求的全部目标（focusTasks 的集合；旧代码只写 focusTarget 时也兼容）。 */
function focusIdList(): string[] {
  if (tasks.focusTargets.length) return tasks.focusTargets.slice();
  return tasks.focusTarget ? [tasks.focusTarget.id] : [];
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

const TAB_ORDER: Array<"current" | "history"> = ["current", "history"];

async function switchTab(tab: "current" | "history") {
  if (tab === view.value) return;
  if (listRef.value) tasks.setScroll(view.value, listRef.value.scrollTop);
  await tasks.switchView(tab);
  await nextTick();
  restoreScroll();
}

function tabElement(tab: "current" | "history"): HTMLButtonElement | null {
  return tab === "current" ? currentTab.value : historyTab.value;
}

/**
 * tablist 键盘：←/→ 在两个视图间循环，Home/End 跳到首/末。
 *
 * 为什么必须做：两个 tab 是同一组选项，键盘用户不该被迫 Tab 两次再到列表；
 * 同时只让当前 tab 进 Tab 序列（roving tabindex），与 ARIA tabs 模式一致 ——
 * 这样「切换视图」和「进入任务列表」是两条清晰路径，不会把焦点困在 tab 上。
 */
async function onTabsKeydown(event: KeyboardEvent): Promise<void> {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  event.stopPropagation();
  const index = TAB_ORDER.indexOf(view.value);
  let nextIndex = index;
  if (event.key === "ArrowLeft") nextIndex = (index - 1 + TAB_ORDER.length) % TAB_ORDER.length;
  else if (event.key === "ArrowRight") nextIndex = (index + 1) % TAB_ORDER.length;
  else if (event.key === "Home") nextIndex = 0;
  else nextIndex = TAB_ORDER.length - 1;
  const next = TAB_ORDER[nextIndex];
  await switchTab(next);
  await nextTick();
  tabElement(next)?.focus();
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

/**
 * 定位当前 focus 请求：**全部目标**一起高亮，只把第一个真正渲染出来的目标滚进视野。
 * 找不到行时不报错（store 的 focusError 负责说明失败原因）。
 */
async function locateFocus(): Promise<void> {
  const ids = focusIdList();
  if (!ids.length) return;
  const seq = tasks.focusTarget?.seq ?? 0;
  const rows = ids
    .map((id) => rowElement(id))
    .filter((row): row is HTMLElement => Boolean(row));
  // 行还没出现（数据/视图刚切换）时也先记下高亮；行出现后 items.length 变化会再定位一次。
  const key = seq + "|" + ids.join("|") + "|" + rows.length;
  if (key === lastFocusKey) return;
  lastFocusKey = key;
  flashHighlight(ids);
  rows[0]?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
}

watch(
  () => [tasks.focusTarget?.seq ?? 0, tasks.focusTargets.join("|")],
  async () => {
    await nextTick();
    await locateFocus();
  },
  { immediate: true },
);

// 聚焦请求可能先于列表数据到达（focusTasks 期间在刷新）：行出现后再定位一次。
watch(
  () => items.value.length,
  async () => {
    await nextTick();
    await locateFocus();
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
        <QIconButton
          icon="refresh"
          label="刷新任务列表"
          description="重新读取任务清单（不会执行或取消任何任务）"
          size="sm"
          tone="quiet"
          :busy="state.loading"
          @click="refresh"
        />
        <QIconButton
          v-if="props.drawer"
          icon="close"
          label="关闭任务列表"
          description="关闭任务列表，任务本身不受影响"
          size="sm"
          tone="quiet"
          @click="emit('close')"
        />
        <QIconButton
          v-else
          icon="collapse"
          label="收起任务栏"
          description="收起左侧任务栏，展开入口仍在左栏边缘"
          size="sm"
          tone="quiet"
          @click="shell.toggleLeft()"
        />
      </div>
    </header>

    <div class="rail-tabs" role="tablist" aria-label="任务视图" @keydown="onTabsKeydown">
      <button
        ref="currentTab"
        type="button"
        role="tab"
        data-task-tab="current"
        :tabindex="view === 'current' ? 0 : -1"
        :aria-selected="view === 'current' ? 'true' : 'false'"
        :class="{ selected: view === 'current' }"
        @click="switchTab('current')"
      >
        当前任务
        <span v-if="taskAttention > 0" class="tab-count mono" data-tone="attention">{{ taskAttention }}</span>
        <span v-else-if="activeCount > 0" class="tab-count mono">{{ activeCount }}</span>
      </button>
      <button
        ref="historyTab"
        type="button"
        role="tab"
        data-task-tab="history"
        :tabindex="view === 'history' ? 0 : -1"
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
      <QIconButton
        icon="close"
        label="关闭提示"
        description="只关闭这条提示，不影响任务与数据"
        size="sm"
        tone="quiet"
        @click="dismissFeedback"
      />
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
            :highlighted="highlightIds.includes(item.id)"
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
.rail-tabs {
  flex: none;
  display: flex;
  gap: 3px;
  margin: 0 10px 6px;
  padding: 3px;
  border-radius: var(--qio-ui-radius);
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
  border-radius: var(--qio-ui-radius);
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
  border-radius: var(--qio-ui-radius);
  background: var(--bg-base);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.rail-feedback span { flex: 1; min-width: 0; overflow-wrap: anywhere; }
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
