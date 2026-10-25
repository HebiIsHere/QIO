<!--
  互动页顶栏控件（Phase 1-B，子智能体 p1-layout 负责）：搜索 + 唯一的保存状态。

  契约：docs/interactive-page-v2-contract.md §2.1 / §2.3。
  - 挂载点由 A 在 WindowChrome 里提供：#qio-interactive-chrome-extra（品牌与设置之间）；
    本组件用 Teleport 挂进去，同时**不接管拖动面**（拖动面是容器兄弟，见 WindowChrome）。
  - 全局栏自身 overflow: hidden，所以搜索结果浮层 Teleport 到 body 并用 fixed 定位，
    再由 chromePlacement 纯函数把矩形钳进视口 —— 不被顶栏裁切、不被推出屏幕。
  - 搜索用真实 store 的板面 + 现有 searchCards 纯函数，点结果 dispatch
    qio:interactive:locate-card（沿用互动页既有的定位事件名，旧的独立搜索面板已删除），不造假数据。
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

/* ── 板内搜索 ──────────────────────────────────────────────────────────── */
interface Hit {
  id: string;
  kindLabel: string;
  summary: string;
  bookmarked: boolean;
  checked: boolean;
  hidden: boolean;
}

const searchOpen = ref(false);
const query = ref("");
const locatedId = ref<string | null>(null);
const triggerEl = ref<HTMLButtonElement | null>(null);
const inputEl = ref<HTMLInputElement | null>(null);
const panelEl = ref<HTMLElement | null>(null);

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
        bookmarked: card.bookmarked,
        checked: card.checked,
        hidden: card.hidden,
      };
    });
});

/** 初始位置只是个安全值；打开后会用触发按钮的真实矩形重算。 */
const placement = ref<ChromePopoverPlacement>({
  left: CHROME_POPOVER_MARGIN,
  top: CHROME_POPOVER_MARGIN,
  width: CHROME_SEARCH_WIDTH,
  maxHeight: 0,
});

const panelStyle = computed(() => ({
  position: "fixed" as const,
  left: placement.value.left + "px",
  top: placement.value.top + "px",
  width: placement.value.width + "px",
  maxHeight: placement.value.maxHeight + "px",
}));

/** 重新计算浮层位置：右对齐搜索按钮，被视口钳制。窗口变化 / 滚动时调用。 */
function updatePlacement(): void {
  const trigger = triggerEl.value;
  if (!trigger || typeof window === "undefined") return;
  const rect = trigger.getBoundingClientRect();
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
  if (!searchOpen.value) return;
  const target = event.target as Node | null;
  if (!target) return;
  if (panelEl.value?.contains(target) || triggerEl.value?.contains(target)) return;
  void closeSearch(false);
}

function addGlobalListeners(): void {
  if (typeof window === "undefined") return;
  window.addEventListener("resize", updatePlacement);
  window.addEventListener("scroll", updatePlacement, true);
  document.addEventListener("pointerdown", onDocumentPointerDown, true);
}

function removeGlobalListeners(): void {
  if (typeof window === "undefined") return;
  window.removeEventListener("resize", updatePlacement);
  window.removeEventListener("scroll", updatePlacement, true);
  document.removeEventListener("pointerdown", onDocumentPointerDown, true);
}

async function openSearch(): Promise<void> {
  if (searchOpen.value) return;
  searchOpen.value = true;
  await nextTick();
  updatePlacement();
  addGlobalListeners();
  inputEl.value?.focus();
}

async function closeSearch(restoreFocus = true): Promise<void> {
  if (!searchOpen.value) return;
  searchOpen.value = false;
  locatedId.value = null;
  removeGlobalListeners();
  if (!restoreFocus) return;
  await nextTick();
  triggerEl.value?.focus();
}

function toggleSearch(): void {
  if (searchOpen.value) void closeSearch();
  else void openSearch();
}

function onPanelKeydown(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  // Esc 只收搜索浮层：不让板面把它当成「取消拖动 / 清空选择」
  event.stopPropagation();
  event.preventDefault();
  void closeSearch();
}

function locate(hit: Hit): void {
  locatedId.value = hit.id;
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent("qio:interactive:locate-card", { detail: { cardId: hit.id } }),
  );
}

onBeforeUnmount(() => {
  removeGlobalListeners();
});
</script>

<template>
  <!-- 顶栏里的控件：搜索 + 唯一保存状态（不接管拖动面，pointer-events 只作用于自己） -->
  <Teleport v-if="mountTarget" :to="mountTarget">
    <div class="iw-chrome" data-iw-chrome-tools>
      <p
        class="iw-save"
        :data-tone="saveView.tone"
        data-iw-chrome-save
        role="status"
        :title="saveView.detail || undefined"
        :aria-label="'保存状态：' + saveView.text"
      >
        <span class="iw-dot" aria-hidden="true"></span>
        <span class="iw-save-text">{{ saveView.text }}</span>
      </p>

      <button
        ref="triggerEl"
        class="iw-search-btn"
        type="button"
        data-iw-chrome-search
        aria-haspopup="dialog"
        aria-controls="iw-chrome-search-panel"
        :aria-expanded="searchOpen ? 'true' : 'false'"
        aria-label="搜索板面"
        title="板内搜索：只查你自己的板面"
        @click="toggleSearch"
      >
        <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
          <circle cx="7" cy="7" r="4.1" fill="none" stroke="currentColor" stroke-width="1.4" />
          <path
            d="M10.1 10.1 13.8 13.8"
            fill="none"
            stroke="currentColor"
            stroke-width="1.4"
            stroke-linecap="round"
          />
        </svg>
        <span class="iw-search-label">搜索</span>
      </button>
    </div>
  </Teleport>

  <!-- 结果浮层：单独 Teleport 到 body + fixed，避免被全局栏 overflow 裁切 -->
  <Teleport v-if="mountTarget" to="body">
    <section
      v-if="searchOpen"
      id="iw-chrome-search-panel"
      ref="panelEl"
      class="iw-search"
      data-iw-chrome-search-panel
      role="dialog"
      aria-modal="false"
      aria-label="板内搜索"
      :style="panelStyle"
      v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
      @keydown="onPanelKeydown"
    >
      <header class="iw-search-head">
        <h2 class="iw-search-title">板内搜索</h2>
        <button
          class="iw-search-close"
          type="button"
          data-iw-chrome-search-close
          title="收起搜索（Esc）"
          @click="closeSearch()"
        >
          收起
        </button>
      </header>

      <label class="iw-search-field">
        <span class="iw-search-field-label">搜你自己的板面</span>
        <input
          ref="inputEl"
          v-model="query"
          type="search"
          data-iw-chrome-search-input
          placeholder="搜文字卡、材料名称或链接地址"
          aria-label="板内搜索"
        />
      </label>

      <ul v-if="hits.length" class="iw-search-results">
        <li v-for="hit in hits" :key="hit.id">
          <button
            class="iw-search-result"
            type="button"
            data-iw-chrome-search-result
            :data-card-id="hit.id"
            :class="{ located: locatedId === hit.id }"
            @click="locate(hit)"
          >
            <span class="row">
              <span class="kind mono">{{ hit.kindLabel }}</span>
              <span class="summary">{{ hit.summary }}</span>
            </span>
            <span class="tags">
              <span v-if="hit.bookmarked" class="tag bookmark">书签优先</span>
              <span v-if="hit.hidden" class="tag hidden">已隐藏（退出讨论范围）</span>
              <span v-else-if="hit.checked" class="tag checked">已勾选</span>
              <span v-else class="tag">未勾选</span>
              <span v-if="locatedId === hit.id" class="tag located-tag">已在板面上定位</span>
            </span>
          </button>
        </li>
      </ul>
      <p v-else-if="query.trim()" class="iw-search-note">没有匹配的卡片。</p>
      <p v-else class="iw-search-note">输入关键词后，点结果会在板面上定位（不会修改板面）。</p>
      <p class="iw-search-scope">搜索只查你自己的板面；未勾选的注释不会因此交给 QIO。</p>
    </section>
  </Teleport>
</template>

<style scoped>
/* ── 顶栏控件 ───────────────────────────────────────────────────────────── */
.iw-chrome {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  height: 100%;
  min-width: 0;
  /* 全局栏的拖动面是兄弟节点；这里只接收自己这一块区域的指针事件 */
  pointer-events: auto;
  font-family: var(--sans);
}

.iw-save {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  margin: 0;
  min-width: 0;
  max-width: min(240px, 24vw);
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

.iw-search-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  flex: none;
  height: 28px;
  padding: 0 var(--sp-2);
  border: 1px solid transparent;
  border-radius: var(--r-pill);
  background: none;
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease);
}
.iw-search-btn:hover {
  color: var(--text-strong);
  background: var(--layer-hover);
  border-color: var(--border-subtle);
}
.iw-search-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.iw-search-btn svg {
  width: 14px;
  height: 14px;
  flex: none;
}

/* ── 搜索结果浮层（Teleport 到 body；position/尺寸由面板内联样式给出）─────── */
.iw-search {
  /* 高于全局栏（240），保证浮层出现在顶栏之上而不是被它裁掉 */
  z-index: 260;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
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

.iw-search-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
.iw-search-title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--text-strong);
}
.iw-search-close {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  padding: 1px var(--sp-2);
  cursor: pointer;
}
.iw-search-close:hover {
  color: var(--text-strong);
  border-color: var(--border-strong);
}
.iw-search-close:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}

.iw-search-field {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.iw-search-field-label {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.iw-search-field input {
  font: inherit;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-inset);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  padding: var(--sp-1) var(--sp-2);
}
.iw-search-field input:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}

.iw-search-note,
.iw-search-scope {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-tight);
  color: var(--text-faint);
}

.iw-search-results {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.iw-search-result {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 2px;
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
.row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-1);
  min-width: 0;
}
.kind {
  flex: none;
  color: var(--text-muted);
}
.summary {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
  color: var(--text-faint);
}
.tag {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-1);
}
.tag.bookmark {
  color: var(--warning);
  border-color: var(--warning);
}
.tag.hidden {
  color: var(--text-muted);
  border-style: dashed;
}
.tag.checked {
  color: var(--success);
  border-color: var(--success);
}
.tag.located-tag {
  color: var(--warning);
  border-color: var(--warning);
}
</style>
