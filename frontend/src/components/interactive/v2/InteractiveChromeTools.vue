<!--
  互动页顶栏控件（Phase 1-B，子智能体 p1-layout 负责）：内联搜索 + 唯一的保存状态。

  契约：docs/interactive-page-v2-contract.md §2.1 / §2.3。
  - 挂载点由 A 在 WindowChrome 里提供：#qio-interactive-chrome-extra（品牌与设置之间）；
    本组件用 Teleport 挂进去，同时**不接管拖动面**（拖动面是容器兄弟，见 WindowChrome）。
  - FU4：搜索不再是「按钮 + 弹窗」，而是直接渲染在挂载点里的**内联输入框**：
    初始较窄，点击 / 聚焦即进入输入状态并加宽（宽度过渡），不需要点第二次；
    除 input 自身 placeholder 外没有任何标题 / label / 说明 / scope / 关闭按钮文字。
  - 结果列表是锚定在输入框下方的**小浮层列表**（不是弹窗）：无标题、无说明，
    Teleport 到 body + fixed 定位并由 chromePlacement 纯函数把矩形钳进视口
    —— 不被全局栏裁切、不被推出屏幕；点结果仍 dispatch qio:interactive:locate-card。
  - 搜索用真实 store 的板面 + 现有 searchCards 纯函数，不造假数据；Esc / 点击外部收起结果。
  - 保存状态是互动页**唯一**的一处：说准「保存只落库 ≠ QIO 已收到」，
    失败原因一句话，完整原文放 title。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from "vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { CARD_KIND_LABELS, cardById, searchCards } from "../../../interactive/board";
import { vQioScrollbar } from "../../../composables/useQioScrollbars";
import {
  CHROME_POPOVER_GAP,
  CHROME_POPOVER_MARGIN,
  CHROME_SEARCH_MAX_WIDTH,
  CHROME_SEARCH_WIDTH,
  placeChromePopover,
  type ChromePopoverPlacement,
} from "./chromePlacement";
import { useChromeExtraTarget } from "./chromeTarget";
import { saveStatusView } from "./saveStatus";

const props = withDefaults(
  defineProps<{
    /** 挂载点选择器；默认是 A 在互动路由渲染的容器。 */
    target?: string;
  }>(),
  { target: "#qio-interactive-chrome-extra" },
);

const store = useInteractiveStore();
const mountTarget = useChromeExtraTarget(() => props.target);

/* ── 唯一保存状态 ──────────────────────────────────────────────────────── */
const saveView = computed(() =>
  saveStatusView({
    status: store.saveStatus,
    dirty: store.dirty,
    error: store.saveError,
    // F01：影响确认等待期间显示「等待确认，尚未保存」，绝不显示已保存
    pendingImpact: Boolean(store.pendingImpact),
  }),
);

/**
 * 保存失败时的**唯一**就地重试入口。
 *
 * 为什么放在顶栏保存状态这里：同一时刻只允许一处常驻的「保存失败」——
 * 之前的 SaveFailureBanner 和这里的保存状态说的是同一个问题，两条常驻横幅会让
 * 用户以为有两件事要处理。现在失败原因与状态都在这一处，动作也在这一处；
 * 草稿本机保存失败、提交后刷新失败不属于「保存状态」，仍就近留在输入区上方。
 */
const retryBusy = ref(false);
async function retrySave(): Promise<void> {
  if (retryBusy.value) return;
  retryBusy.value = true;
  try {
    await store.saveNow();
  } finally {
    retryBusy.value = false;
  }
}

/* ── 板内搜索（内联输入 + 下方结果浮层列表） ───────────────────────────── */
interface Hit {
  id: string;
  kindLabel: string;
  summary: string;
}

const query = ref("");
const expanded = ref(false);
const locatedId = ref<string | null>(null);
const fieldEl = ref<HTMLElement | null>(null);
const inputEl = ref<HTMLInputElement | null>(null);
const resultsEl = ref<HTMLElement | null>(null);
let listenersOn = false;
/**
 * 从结果列表按 Esc 收起后会把焦点还给输入框；这次「聚焦」只用于恢复焦点，
 * 不能再次把结果展开（否则 Esc 等于没反应）。
 */
let suppressNextActivate = false;

/** 结果只在「已进入输入状态 + 有查询词」时出现；空结果不渲染任何说明文字。 */
const resultsOpen = computed(() => expanded.value && query.value.trim().length > 0);

const hits = computed<Hit[]>(() => {
  const state = store.board;
  if (!state || !query.value.trim()) return [];
  return searchCards(state, query.value)
    .map((id) => cardById(state, id))
    .filter((card): card is NonNullable<typeof card> => Boolean(card))
    .map((card) => {
      const text = (card.content || String(card.meta?.name ?? card.meta?.title ?? "")).trim();
      return {
        id: card.id,
        kindLabel: CARD_KIND_LABELS[card.kind],
        summary: text ? (text.length > 28 ? text.slice(0, 28) + "…" : text) : "（无内容）",
      };
    });
});

/** 初始位置只是个安全值；打开结果后会用输入框的真实矩形重算。 */
const placement = ref<ChromePopoverPlacement>({
  left: CHROME_POPOVER_MARGIN,
  top: CHROME_POPOVER_MARGIN,
  width: CHROME_SEARCH_WIDTH,
  maxHeight: 0,
});

const resultsStyle = computed(() => ({
  position: "fixed" as const,
  left: placement.value.left + "px",
  top: placement.value.top + "px",
  width: placement.value.width + "px",
  maxHeight: placement.value.maxHeight + "px",
}));

/** 重新计算结果浮层位置：右对齐输入框，被视口钳制。窗口变化 / 滚动时调用。 */
function updatePlacement(): void {
  const anchor = fieldEl.value;
  if (!anchor || typeof window === "undefined") return;
  const rect = anchor.getBoundingClientRect();
  placement.value = placeChromePopover(
    {
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      width: rect.width,
      height: rect.height,
    },
    { width: window.innerWidth || 0, height: window.innerHeight || 0 },
    {
      margin: CHROME_POPOVER_MARGIN,
      gap: CHROME_POPOVER_GAP,
      width: CHROME_SEARCH_WIDTH,
      maxWidth: CHROME_SEARCH_MAX_WIDTH,
    },
  );
}

function onDocumentPointerDown(event: Event): void {
  if (!expanded.value) return;
  const target = event.target as Node | null;
  if (!target) return;
  if (fieldEl.value?.contains(target) || resultsEl.value?.contains(target)) return;
  closeResults();
}

function addGlobalListeners(): void {
  if (listenersOn || typeof window === "undefined") return;
  listenersOn = true;
  window.addEventListener("resize", updatePlacement);
  window.addEventListener("scroll", updatePlacement, true);
  document.addEventListener("pointerdown", onDocumentPointerDown, true);
}

function removeGlobalListeners(): void {
  if (!listenersOn || typeof window === "undefined") return;
  listenersOn = false;
  window.removeEventListener("resize", updatePlacement);
  window.removeEventListener("scroll", updatePlacement, true);
  document.removeEventListener("pointerdown", onDocumentPointerDown, true);
}

/** 点击 / 聚焦 = 进入输入状态并加宽（一次点击即可输入，不需要再点第二次）。 */
async function activateSearch(): Promise<void> {
  if (expanded.value) return;
  expanded.value = true;
  addGlobalListeners();
  await nextTick();
  updatePlacement();
}

/** 收起结果（不清空输入，沿用既有习惯；输入框保持焦点时 Esc 不会再次展开）。 */
function closeResults(): void {
  if (!expanded.value) return;
  expanded.value = false;
  locatedId.value = null;
  removeGlobalListeners();
}

function onInputKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    // Esc 只收结果：不让板面把它当成「取消拖动 / 清空选择」
    event.stopPropagation();
    event.preventDefault();
    closeResults();
    return;
  }
  if (event.key === "ArrowDown" && resultsOpen.value) {
    // 键盘可达：从输入框直接进入结果列表
    const first = resultsEl.value?.querySelector<HTMLElement>("[data-iw-chrome-search-result]");
    if (first) {
      event.preventDefault();
      first.focus();
    }
  }
}

function onResultsKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  event.stopPropagation();
  event.preventDefault();
  closeResults();
  // 焦点还给输入框，但不要因为这次聚焦又把结果展开
  suppressNextActivate = true;
  inputEl.value?.focus();
  queueMicrotask(() => {
    suppressNextActivate = false;
  });
}

/** 点搜索框的任何位置（含图标区）都直接进入输入：一次点击即可打字。 */
function focusInput(): void {
  inputEl.value?.focus();
  void activateSearch();
}

/** 聚焦只用于「进入输入状态」；从结果列表返回焦点的这一次不重新展开。 */
function onFocus(): void {
  if (suppressNextActivate) {
    suppressNextActivate = false;
    return;
  }
  void activateSearch();
}

function onQueryInput(): void {
  void activateSearch();
  void nextTick().then(updatePlacement);
}

function locate(hit: Hit): void {
  locatedId.value = hit.id;
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent("qio:interactive:locate-card", { detail: { cardId: hit.id } }),
  );
}

onBeforeUnmount(removeGlobalListeners);
</script>

<template>
  <!-- 顶栏里的控件：内联搜索 + 唯一保存状态（不接管拖动面，pointer-events 只作用于自己） -->
  <Teleport v-if="mountTarget" :to="mountTarget">
    <div class="iw-chrome" data-iw-chrome-tools>
      <div class="iw-save-group">
        <p
          class="iw-save"
          :data-tone="saveView.tone"
          data-iw-chrome-save
          role="status"
          :aria-label="'保存状态：' + saveView.text + (saveView.detail ? '（' + saveView.detail + '）' : '')"
        >
          <span class="iw-dot" aria-hidden="true"></span>
          <span class="iw-save-text">{{ saveView.text }}</span>
        </p>
        <button
          v-if="saveView.tone === 'err'"
          class="iw-save-retry"
          type="button"
          data-iw-chrome-save-retry
          :disabled="retryBusy"
          aria-label="重试保存（只重新保存，不会重复提交）"
          @click="retrySave"
        >{{ retryBusy ? "重试中…" : "重试" }}</button>
      </div>

      <!--
        内联搜索框：这里就是唯一的搜索入口（没有按钮、没有弹窗）。
        除 input 自己的 placeholder 外不渲染任何文字提示；放大镜图标在输入框内。
      -->
      <div
        ref="fieldEl"
        class="iw-search-field"
        data-iw-chrome-search-field
        :data-expanded="expanded ? 'true' : 'false'"
        @click="focusInput"
      >
        <svg class="iw-search-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
          <circle cx="7" cy="7" r="4.1" fill="none" stroke="currentColor" stroke-width="1.4" />
          <path
            d="M10.1 10.1 13.8 13.8"
            fill="none"
            stroke="currentColor"
            stroke-width="1.4"
            stroke-linecap="round"
          />
        </svg>
        <input
          ref="inputEl"
          v-model="query"
          class="iw-search-input"
          type="search"
          data-iw-chrome-search-input
          placeholder="搜板内卡片"
          aria-label="搜索板面"
          autocomplete="off"
          spellcheck="false"
          @focus="onFocus()"
          @click="activateSearch()"
          @input="onQueryInput"
          @keydown="onInputKeydown"
        />
      </div>
    </div>
  </Teleport>

  <!-- 结果列表：小浮层（不是弹窗）→ Teleport 到 body + fixed，避免被全局栏裁切 -->
  <Teleport v-if="mountTarget" to="body">
    <ul
      v-if="resultsOpen && hits.length"
      ref="resultsEl"
      class="iw-search-results"
      data-iw-chrome-search-results
      role="list"
      :style="resultsStyle"
      v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
      @keydown="onResultsKeydown"
    >
      <li v-for="hit in hits" :key="hit.id">
        <button
          class="iw-search-result"
          type="button"
          data-iw-chrome-search-result
          :data-card-id="hit.id"
          :class="{ located: locatedId === hit.id }"
          @click="locate(hit)"
        >
          <span class="kind mono">{{ hit.kindLabel }}</span>
          <span class="summary">{{ hit.summary }}</span>
        </button>
      </li>
    </ul>
  </Teleport>
</template>

<style scoped>
/* ── 顶栏控件 ───────────────────────────────────────────────────────────── */
.iw-chrome {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  height: 100%;
  min-width: 0;
  max-width: 100%;
  /* 全局栏的拖动面是兄弟节点；这里只接收自己这一块区域的指针事件 */
  pointer-events: auto;
  font-family: var(--sans);
}

.iw-save-group {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
  max-width: min(300px, 30vw);
}
.iw-save {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  margin: 0;
  min-width: 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  white-space: nowrap;
}
/* 状态不只靠颜色：文案已经说明，色点只是辅助 */
.iw-dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-faint);
}
.iw-save[data-tone="ok"] .iw-dot {
  background: var(--success);
}
.iw-save[data-tone="busy"] .iw-dot {
  background: var(--warning);
}
.iw-save[data-tone="err"] {
  color: var(--danger);
}
.iw-save[data-tone="err"] .iw-dot {
  background: var(--danger);
}
.iw-save-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 失败时唯一就地的重试入口：只在保存失败（tone=err）时出现 */
.iw-save-retry {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-strong);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-2);
  min-height: 22px;
  cursor: pointer;
}
.iw-save-retry:disabled { color: var(--text-muted); cursor: default; }
.iw-save-retry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }

/*
  内联搜索框（FU4）：初始较窄，点击 / 聚焦后加宽（宽度过渡）。
  只有 input 自己；除 placeholder 外没有任何可见文字。
*/
.iw-search-field {
  position: relative;
  display: inline-flex;
  align-items: center;
  flex: 0 1 auto;
  width: 128px;
  max-width: calc(100vw - 240px);
  height: 26px;
  padding: 0 var(--sp-2) 0 24px;
  border: 1px solid transparent;
  border-radius: var(--r-pill);
  background: var(--bg-inset);
  color: var(--text-secondary);
  transition:
    width var(--dur-menu) var(--ease-out),
    border-color var(--dur-fast) var(--ease),
    background var(--dur-fast) var(--ease);
}
.iw-search-field:hover {
  border-color: var(--border-subtle);
}
.iw-search-field:focus-within {
  border-color: var(--border-strong);
  background: var(--bg-surface);
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.iw-search-field[data-expanded="true"] {
  width: 260px;
  max-width: min(320px, calc(100vw - 240px));
}
.iw-search-icon {
  position: absolute;
  left: 8px;
  width: 13px;
  height: 13px;
  flex: none;
  pointer-events: none;
  color: var(--text-muted);
}
.iw-search-input {
  width: 100%;
  height: 100%;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-primary);
  font: inherit;
  font-size: var(--fs-sm);
  outline: none;
}
.iw-search-input::placeholder {
  color: var(--text-muted);
}
.iw-search-input:focus-visible {
  outline: none;
}

/* ── 搜索结果：输入框下方的小浮层列表（不是弹窗）────────────────────────── */
.iw-search-results {
  z-index: 260;
  list-style: none;
  margin: 0;
  padding: var(--sp-1);
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  box-shadow: var(--shadow-2);
  color: var(--text-primary);
  font-family: var(--sans);
  animation: iw-search-in var(--dur-menu) var(--ease-out) both;
}
@keyframes iw-search-in {
  from {
    opacity: 0;
    transform: translateY(calc(var(--shift-2) * -1));
  }
  to {
    opacity: 1;
    transform: none;
  }
}
.iw-search-result {
  width: 100%;
  display: flex;
  align-items: baseline;
  gap: var(--sp-1);
  text-align: left;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: var(--sp-1) var(--sp-2);
  cursor: pointer;
}
.iw-search-result:hover {
  border-color: var(--border-strong);
  background: var(--bg-elevated);
}
.iw-search-result:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.iw-search-result.located {
  border-color: var(--warning);
}
.kind {
  flex: none;
  color: var(--text-muted);
}
.summary {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 窄窗：保存状态与搜索框都不许把全局栏挤溢出（480 仍可用） */
@media (max-width: 760px) {
  .iw-save-group {
    max-width: 120px;
  }
  .iw-save {
    max-width: 72px;
  }
  .iw-search-field {
    width: 104px;
    max-width: calc(100vw - 220px);
  }
  .iw-search-field[data-expanded="true"] {
    width: 180px;
    max-width: min(200px, calc(100vw - 200px));
  }
}
</style>
