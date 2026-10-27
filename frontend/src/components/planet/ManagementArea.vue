<script setup lang="ts">
/**
 * 管理区（契约 §3.5）—— 记忆 / 知识 / 实体三个标签页 + 待处理 / 整理记录两条辅助入口，
 * 全部占据**同一块**管理内容区域（不是额外侧栏、不是抽屉）。
 *
 * 三条硬规则：
 * 1. **v-show 保活**：标签面板首次激活才挂载，之后一律 v-show —— 切换标签页保留各自的
 *    列表位置、搜索词、已打开条目；也**绝不**触碰星球（不换景、不动相机）；
 * 2. **返回恢复上下文**：进待处理 / 整理记录前记住当前标签页，返回时回到原标签页与原条目；
 *    搜索结果打开条目后可以回到搜索结果（关键词、结果、滚动都在）；
 * 3. **定位靠真实 id**：知识 / 实体 / 待处理都用 B 组件的 `focusId` prop 打开对应条目，
 *    这里只负责切标签页与传 id（不猜、不构造假条目）。
 */
import { nextTick, onMounted, reactive, ref, watch } from "vue";
import QIconButton from "../ui/QIconButton.vue";
import MemoryTab from "./MemoryTab.vue";
import KnowledgePanel from "./KnowledgePanel.vue";
import EntityPanel from "./EntityPanel.vue";
/**
 * 待处理 / 整理记录：B 会交付独立的 `PendingItems.vue` / `MaintenanceRecords.vue`（契约 §4.2）。
 * 在它们落地前先用基线 `MemoryPendingPanel` 顶上（真实接口、真实数据、无假状态）；
 * 集成时**只需要替换下面两行 import 与模板里对应的两个组件标签**。
 */
import MemoryPendingPanel from "./MemoryPendingPanel.vue";
import GlobalSearchPanel from "./GlobalSearchPanel.vue";
import { usePlanetPendingStore } from "../../stores/planetPending";
import {
  resolveInitialRequest,
  type ManageTab,
  type ManageView,
  type PlanetInitialTab,
  type SearchHit,
} from "../../planet/manageViews";

const props = withDefaults(
  defineProps<{
    /** 管理区是否展开（= C 的 progress 目标态）；关闭时不渲染内容焦点也不刷新数据 */
    open?: boolean;
    /** 外部落点（对话页深链 / 记忆提醒）：原样透传 PlanetView 的 initialTab */
    requestView?: PlanetInitialTab | null;
    /** 外部要求定位的条目 id（待处理事项等） */
    requestFocusId?: string | null;
    /** 请求代号：同一落点重复请求也要生效 */
    requestSeq?: number;
    /** 页面工具行「搜索」的请求代号 */
    searchTicket?: number;
    /** 星球上选中的话题（只在用户点了话题点时变，不随自动聚焦抖动） */
    focusedTopicId?: string | null;
    /** 锚点切换的进行中 / 失败原因（来自 PlanetView 的唯一状态） */
    continueBusy?: boolean;
    continueError?: string;
  }>(),
  {
    open: false,
    requestView: null,
    requestFocusId: null,
    requestSeq: 0,
    searchTicket: 0,
    focusedTopicId: null,
    continueBusy: false,
    continueError: "",
  },
);

const emit = defineEmits<{
  /** 收起管理区（回到默认浏览态） */
  close: [];
  /** 记忆页选中话题 → 星球同步聚焦（浏览，不改起点） */
  "topic-select": [topicId: string];
  /** 继续讨论 / 从这里继续 → 由 PlanetView 走既有锚点切换机制 */
  continue: [payload: { topicId: string; fragmentId: string | null }];
  /** 话题结束/恢复后：星球侧的数据也要跟着刷新 */
  "topics-changed": [];
  /** 管理区当前状态（供页面根元素上的只读验收属性 data-tab / data-view 使用） */
  state: [state: { tab: ManageTab; view: ManageView }];
}>();

const pending = usePlanetPendingStore();

const tab = ref<ManageTab>("memory");
const view = ref<ManageView>("tabs");
/** 首次激活才挂载（之后一律 v-show 保活） */
const mountedTabs = reactive<Record<ManageTab, boolean>>({ memory: false, knowledge: false, entity: false });
const mountedAux = reactive<Record<"pending" | "records" | "search", boolean>>({
  pending: false,
  records: false,
  search: false,
});

/** 进辅助视图 / 搜索前记住的标签页：返回时恢复 */
const tabBeforeAux = ref<ManageTab>("memory");
const viewBeforeSearch = ref<{ view: ManageView; tab: ManageTab }>({ view: "tabs", tab: "memory" });
/** 从搜索结果打开过条目 → 顶部提供「返回搜索结果」 */
const searchReturn = ref(false);

/** 定位 id：B 组件的 focusId（知识 / 实体 / 待处理各自一份，互不干扰） */
const knowledgeFocusId = ref<string | null>(null);
const entityFocusId = ref<string | null>(null);
const entityNodeId = ref<string | null>(null);
const pendingFocusId = ref<string | null>(null);
const memoryFocusTopicId = ref<string | null>(null);
const memoryFocusTicket = ref(0);

/**
 * 同一个 id 再次定位也要生效：先把 prop 清空，等一帧再写入。
 * 否则「搜同一个条目两次」第二次不会触发子组件的 watch（值没变）。
 */
async function refocus<T extends string | null>(target: { value: T }, id: T): Promise<void> {
  target.value = null as unknown as T;
  await nextTick();
  target.value = id;
}

function activateTab(next: ManageTab) {
  tab.value = next;
  mountedTabs[next] = true;
  view.value = "tabs";
}

function selectTab(next: ManageTab) {
  activateTab(next);
}

async function focusKnowledge(id: string) {
  activateTab("knowledge");
  await refocus(knowledgeFocusId, id);
}

async function focusEntity(id: string) {
  activateTab("entity");
  // 基线 EntityPanel 按 node_id 打开，B 的新实现按 focusId 打开：两者都传，集成时按 B 收口
  await refocus(entityFocusId, id);
  await refocus(entityNodeId, id);
}

async function focusMemoryTopic(id: string) {
  activateTab("memory");
  memoryFocusTopicId.value = id;
  memoryFocusTicket.value += 1;
}

/** 辅助入口：待处理 / 整理记录 */
function openAux(next: "pending" | "records") {
  if (view.value === "tabs") tabBeforeAux.value = tab.value;
  mountedAux[next] = true;
  view.value = next;
  if (next === "pending") void pending.refresh();
}

/** 返回：从辅助视图回原标签页；从搜索回进入搜索前的上下文 */
function backFromView() {
  if (view.value === "search") {
    closeSearch();
    return;
  }
  view.value = "tabs";
  tab.value = tabBeforeAux.value;
  mountedTabs[tabBeforeAux.value] = true;
}

function openSearch() {
  if (view.value !== "search") viewBeforeSearch.value = { view: view.value, tab: tab.value };
  searchReturn.value = false;
  mountedAux.search = true;
  view.value = "search";
}

/** 关闭搜索（不是「返回上一个视图」，而是恢复进入搜索前的管理上下文） */
function closeSearch() {
  view.value = viewBeforeSearch.value.view;
  tab.value = viewBeforeSearch.value.tab;
  if (view.value === "tabs") mountedTabs[viewBeforeSearch.value.tab] = true;
  searchReturn.value = false;
}

function returnToSearch() {
  view.value = "search";
}

/** 搜索结果点开：切到对应标签页并定位真实条目（记忆页 / 知识页 / 实体页各自保留状态） */
async function openHit(hit: SearchHit) {
  searchReturn.value = true;
  if (hit.kind === "topic") {
    await focusMemoryTopic(hit.id);
    return;
  }
  if (hit.kind === "knowledge") {
    await focusKnowledge(hit.id);
    return;
  }
  await focusEntity(hit.id);
}

/** 外部落点（深链 / 记忆提醒）：解析成管理区视图 + 标签页 */
function applyRequest() {
  const resolved = resolveInitialRequest(props.requestView ?? "topic");
  if (resolved.view === "pending") {
    openAux("pending");
  } else {
    activateTab(resolved.tab);
  }
  const focusId = props.requestFocusId;
  if (focusId) {
    if (resolved.view === "pending") {
      mountedAux.pending = true;
      pendingFocusId.value = focusId;
    } else if (resolved.tab === "knowledge") void refocus(knowledgeFocusId, focusId);
    else if (resolved.tab === "entity") void focusEntity(focusId);
  }
}

watch(
  () => [props.requestSeq, props.requestView] as const,
  ([seq], previous) => {
    // 只在真的有请求代号时才应用（避免打开页面时把默认值当成请求）
    if (!seq) return;
    if (previous && previous[0] === seq) return;
    applyRequest();
  },
  { flush: "post" },
);

watch(
  () => props.searchTicket,
  (ticket) => {
    if (ticket > 0) openSearch();
  },
);

watch(
  () => props.open,
  (open) => {
    // 展开时刷一次真实未处理数量（角标要反映当下，不是打开页面那一刻）
    if (open) void pending.refresh();
  },
);

/** 星球上真的点了话题点：记忆页打开这个话题的概览（同一个话题再点一次也要重新打开） */
watch(
  () => props.focusedTopicId,
  (id) => {
    if (!id) return;
    memoryFocusTopicId.value = id;
    memoryFocusTicket.value += 1;
  },
);

/** 状态上报：页面根元素上的 data-tab / data-view 是验收脚本的只读读数，不参与布局 */
watch(
  [tab, view],
  () => emit("state", { tab: tab.value, view: view.value }),
  { immediate: true },
);

onMounted(() => {
  mountedTabs.memory = true;
  if (props.open) void pending.refresh();
});

/** 供父级在收起管理区时保留现场：重新展开时不清空任何状态 */
defineExpose({ selectTab });
</script>

<template>
  <div class="management" :data-view="view" :data-tab="tab">
    <header class="ma-head">
      <div v-if="view === 'tabs'" class="ma-tabs" role="tablist" aria-label="管理区">
        <button
          class="ma-tab"
          type="button"
          role="tab"
          data-tab="memory"
          data-mp-tab="memory"
          :aria-selected="tab === 'memory'"
          :class="{ active: tab === 'memory' }"
          @click="selectTab('memory')"
        >
          记忆
        </button>
        <button
          class="ma-tab"
          type="button"
          role="tab"
          data-tab="knowledge"
          data-mp-tab="knowledge"
          :aria-selected="tab === 'knowledge'"
          :class="{ active: tab === 'knowledge' }"
          @click="selectTab('knowledge')"
        >
          知识
        </button>
        <button
          class="ma-tab"
          type="button"
          role="tab"
          data-tab="entity"
          data-mp-tab="entity"
          :aria-selected="tab === 'entity'"
          :class="{ active: tab === 'entity' }"
          @click="selectTab('entity')"
        >
          实体
        </button>
      </div>
      <div v-else class="ma-context">
        <button class="qio-btn quiet mini ma-back" type="button" @click="backFromView">← 返回</button>
        <span class="ma-context-title serif">{{ view === "pending" ? "待处理" : view === "records" ? "整理记录" : "全局搜索" }}</span>
      </div>
      <span class="spacer"></span>
      <div class="ma-aux">
        <button
          v-if="searchReturn && view === 'tabs'"
          class="ma-aux-btn return-search"
          type="button"
          data-mp-search-return
          @click="returnToSearch"
        >
          返回搜索结果
        </button>
        <button
          class="ma-aux-btn aux-pending"
          type="button"
          data-mp-aux="pending"
          :data-mp-pending-count="pending.count.value"
          :class="{ active: view === 'pending' }"
          :aria-pressed="view === 'pending'"
          @click="openAux('pending')"
        >
          待处理<span v-if="pending.count.value > 0" class="count mono">{{ pending.count.value }}</span>
        </button>
        <button
          class="ma-aux-btn aux-records"
          type="button"
          data-mp-aux="records"
          :class="{ active: view === 'records' }"
          :aria-pressed="view === 'records'"
          @click="openAux('records')"
        >
          整理记录
        </button>
        <QIconButton
          :icon="view === 'search' ? 'close' : 'collapse'"
          :label="view === 'search' ? '关闭搜索' : '收起管理'"
          :description="view === 'search' ? '关闭搜索并回到之前的管理内容' : '收起管理区，回到星球中央浏览'"
          size="sm"
          @click="view === 'search' ? closeSearch() : emit('close')"
        />
      </div>
    </header>

    <div class="ma-body">
      <!-- 标签页：首次激活挂载，之后 v-show 保活（列表位置 / 搜索 / 打开条目都留着） -->
      <div v-show="view === 'tabs' && tab === 'memory'" class="ma-view" data-view-panel="memory">
        <MemoryTab
          v-if="mountedTabs.memory"
          :focus-topic-id="memoryFocusTopicId"
          :focus-ticket="memoryFocusTicket"
          :continue-busy="continueBusy"
          :continue-error="continueError"
          @topic-select="emit('topic-select', $event)"
          @continue="emit('continue', $event)"
          @topics-changed="emit('topics-changed')"
          @focus-knowledge="focusKnowledge"
          @focus-entity="focusEntity"
        />
      </div>
      <div v-show="view === 'tabs' && tab === 'knowledge'" class="ma-view" data-view-panel="knowledge">
        <KnowledgePanel
          v-if="mountedTabs.knowledge"
          :focus-id="knowledgeFocusId"
          @focus-topic="focusMemoryTopic"
        />
      </div>
      <div v-show="view === 'tabs' && tab === 'entity'" class="ma-view" data-view-panel="entity">
        <EntityPanel
          v-if="mountedTabs.entity"
          :focus-id="entityFocusId"
          :open-by-node-id="entityNodeId ?? ''"
          @changed="void 0"
        />
      </div>
      <!-- 待处理 / 整理记录：B 组件落地后替换下面两个分支 -->
      <div v-show="view === 'pending'" class="ma-view" data-view-panel="pending">
        <MemoryPendingPanel v-if="mountedAux.pending" :focus-pending-id="pendingFocusId" />
      </div>
      <div v-show="view === 'records'" class="ma-view" data-view-panel="records">
        <MemoryPendingPanel v-if="mountedAux.records" />
      </div>
      <div v-show="view === 'search'" class="ma-view" data-view-panel="search">
        <GlobalSearchPanel
          v-if="mountedAux.search"
          :active="view === 'search'"
          @open="openHit"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.management {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  min-width: 0;
}
.ma-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3) var(--sp-2) var(--sp-4);
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
  /* 窄窗口/长文案换行，绝不把面板撑出横向溢出 */
  flex-wrap: wrap;
  min-width: 0;
}
.ma-tabs {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
  flex-wrap: wrap;
}
.ma-tab {
  padding: 5px 10px;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
  background: none;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: var(--fs-base);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease-1), background var(--dur-fast) var(--ease-1);
}
.ma-tab:hover {
  color: var(--text-strong);
  background: var(--layer-hover);
}
.ma-tab.active {
  color: var(--text-strong);
  background: var(--accent-soft);
  font-weight: 600;
}
.ma-tab:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.ma-context {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
}
.ma-context-title {
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.ma-back {
  padding-left: 0;
}
.ma-head .spacer {
  flex: 1;
  min-width: var(--sp-2);
}
.ma-aux {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  flex-wrap: wrap;
  min-width: 0;
}
.ma-aux-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 9px;
  border: 1px solid transparent;
  border-radius: var(--qio-ui-radius);
  background: none;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: var(--fs-sm);
  cursor: pointer;
  white-space: nowrap;
  transition: color var(--dur-fast) var(--ease-1), background var(--dur-fast) var(--ease-1),
    border-color var(--dur-fast) var(--ease-1);
}
.ma-aux-btn:hover {
  color: var(--text-strong);
  background: var(--layer-hover);
}
.ma-aux-btn.active {
  color: var(--accent);
  border-color: var(--accent-soft);
  background: var(--accent-soft);
}
.ma-aux-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.ma-aux-btn .count {
  font-size: var(--fs-xs);
  color: var(--link);
  font-variant-numeric: tabular-nums;
}
.ma-body {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  /* 布局容器不承担滚动：各视图自己滚，避免多一层可被程序性滚动的容器 */
  overflow: clip;
}
.ma-view {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  /* 长内容一律换行，横向不滚动的根因修复 */
  overflow-wrap: anywhere;
}
.ma-view :deep(*) {
  min-width: 0;
}
</style>
