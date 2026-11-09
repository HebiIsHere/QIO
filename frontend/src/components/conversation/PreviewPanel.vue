<script setup lang="ts">
import { vQioTip } from "../../composables/useQioTip";
/**
 * 对话页右侧只读文件预览（C 负责）。
 *
 * 结构：标题行（只读标记 + 全窗口/收起）→ 多标签栏 → 当前标签的阅读器。
 * 标签与阅读位置都在 conversationShell（服务端 conversation_layout 持久化）：
 *   * 收起整个栏 / 切全窗口 / 页面刷新后，标签与位置继续；
 *   * 关闭一个标签只关这一份预览，不删文件；
 *   * 新附件/新成果不自动展开——只有用户点击打开才写 rightOpen。
 *
 * 本组件只做外壳与标签管理；渲染、搜索、缩放、更新检查都在 preview/PreviewReader。
 */
import { computed, getCurrentInstance, nextTick, ref } from "vue";
import { useConversationShellStore } from "../../stores/conversationShell";
import { useConversationPreviewStore } from "../../stores/conversationPreview";
import { PREVIEW_KIND_LABEL } from "../../conversation/preview";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { useEscapeLayer } from "../../composables/useEscapeLayer";
import PreviewReader from "./preview/PreviewReader.vue";

const props = withDefaults(defineProps<{ full?: boolean }>(), { full: false });
const emit = defineEmits<{ close: []; toggleFull: [] }>();

const shell = useConversationShellStore();
const preview = useConversationPreviewStore();

const tabs = computed(() => shell.prefs.previewTabs);
const activeTab = computed(() => preview.activeTab);

function activate(tabId: string): void {
  preview.activateTab(tabId);
}

function closeTab(tabId: string): void {
  preview.closeTab(tabId);
}

const tabStrip = ref<HTMLElement | null>(null);
/** 标签按钮（v-for 顺序与 tabs 一致）；方向键切换后把真实焦点同步到新标签。 */
const tabMainButtons = ref<HTMLButtonElement[]>([]);

/**
 * 把键盘焦点放到指定标签上，并保证它在标签栏里可见。
 *
 * 修复「文件已切换、焦点还在旧标签」：以前只改 store 的 active id，DOM 焦点仍留在旧按钮上，
 * 继续按 Enter 会重新点回旧标签。现在切换后显式 focus 新标签。
 */
function focusTab(tabId: string): void {
  const index = tabs.value.findIndex((tab) => tab.id === tabId);
  const button = index >= 0 ? tabMainButtons.value[index] ?? null : null;
  if (!button) return;
  button.focus({ preventScroll: true });
  revealTab(button);
}

/** 标签多到超出可视宽度时，把当前标签滚进标签栏（scrollIntoView 不可用的环境用容器算术）。 */
function revealTab(button: HTMLButtonElement): void {
  if (typeof button.scrollIntoView === "function") {
    button.scrollIntoView({ block: "nearest", inline: "nearest" });
    return;
  }
  const strip = tabStrip.value;
  if (!strip) return;
  const left = button.offsetLeft;
  const right = left + button.offsetWidth;
  const viewLeft = strip.scrollLeft;
  const viewRight = viewLeft + strip.clientWidth;
  if (left < viewLeft) strip.scrollLeft = Math.max(0, left - 4);
  else if (right > viewRight) strip.scrollLeft = right - strip.clientWidth + 4;
}

/**
 * 标签栏键盘：←/→ 循环切换、Home/End 到首尾，选中项、当前文件、真实焦点、可见性一起变；
 * Tab / Shift+Tab 不受影响（不 preventDefault），按现有 ARIA tabs 约定进出标签组。
 * 删除键只删当前标签（键盘关闭路径），删完把焦点交给新的当前标签。
 */
async function onTabsKeydown(event: KeyboardEvent): Promise<void> {
  const ids = tabs.value.map((tab) => tab.id);
  if (!ids.length) return;
  if ((event.key === "Delete" || event.key === "Backspace") && activeTab.value) {
    event.preventDefault();
    event.stopPropagation();
    const closingId = activeTab.value.id;
    closeTab(closingId);
    await nextTick();
    const nextActive = activeTab.value?.id;
    if (nextActive) focusTab(nextActive);
    return;
  }
  const currentIndex = Math.max(0, ids.indexOf(activeTab.value?.id ?? ids[0]));
  let next = -1;
  if (event.key === "ArrowRight") next = (currentIndex + 1) % ids.length;
  else if (event.key === "ArrowLeft") next = (currentIndex - 1 + ids.length) % ids.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = ids.length - 1;
  if (next < 0) return;
  event.preventDefault();
  event.stopPropagation();
  const nextId = ids[next];
  activate(nextId);
  await nextTick();
  focusTab(nextId);
}

function retrySave(): void {
  void shell.flush();
}

/**
 * 前方是否还有一层还没接入 Esc 层级栈的阻挡弹层（恢复区 / 任务抽屉等）。
 * 用与 ConversationView 兜底 Esc 完全相同的判断：这类弹层自己的 Esc 处理在冒泡阶段，
 * 预览层若在捕获阶段先收走 Esc 就会「关弹层同时退出全窗口」—— 这里必须让开这一层。
 */
function hasBlockingDialogInFront(): boolean {
  if (typeof document === "undefined") return false;
  return Boolean(document.querySelector('[role="dialog"][aria-modal="true"]'));
}

/**
 * 全窗口时 Esc 返回对话。注册到共用 Esc 层级栈（composables/useEscapeLayer）：
 * 前方弹窗 / 下拉先注册成为内层，它们处理过的 Esc 不会再传到预览；
 * 未接入层级栈的阻挡弹层（恢复区 / 任务抽屉）打开时，本层返回 false 让给它们；
 * 这里不再自己挂 window keydown，避免和层级栈抢先后。
 */
const instanceId = getCurrentInstance()?.uid ?? 0;
useEscapeLayer({
  id: "preview-full-" + instanceId,
  active: () => props.full || shell.effectivePreviewFull,
  onEscape: () => {
    if (!(props.full || shell.effectivePreviewFull)) return false;
    if (hasBlockingDialogInFront()) return false;
    emit("toggleFull");
    return true;
  },
});
</script>

<template>
  <aside class="preview-panel" :class="{ full }" aria-label="文件预览" data-preview-panel>
    <header class="pv-head">
      <div class="pv-head-title">
        <span class="pv-head-label">文件预览</span>
        <span class="pv-head-badge mono">只读</span>
      </div>
      <div class="pv-head-actions">
        <button
          type="button"
          class="qio-btn quiet mini"
          v-qio-tip="full ? '返回对话布局' : '全窗口预览'"
          :aria-label="full ? '返回对话布局' : '全窗口预览'"
          @click="emit('toggleFull')"
        >
          {{ full ? "返回对话" : "全窗口" }}
        </button>
        <button
          type="button"
          class="qio-btn quiet mini"
          v-qio-tip="'收起预览，保留标签与阅读位置'"
          aria-label="收起预览，保留标签与阅读位置"
          @click="emit('close')"
        >
          收起
        </button>
      </div>
    </header>

    <div
      v-if="tabs.length"
      ref="tabStrip"
      class="pv-tabs"
      role="tablist"
      aria-label="已打开的文件"
      v-qio-scrollbar="{ policy: 'auto', axis: 'x' }"
      @keydown="onTabsKeydown"
    >
      <div
        v-for="tab in tabs"
        :key="tab.id"
        class="pv-tab"
        :class="{ 'is-active': tab.id === activeTab?.id }"
      >
        <button
          ref="tabMainButtons"
          type="button"
          class="pv-tab-main"
          role="tab"
          :tabindex="tab.id === activeTab?.id ? 0 : -1"
          :aria-selected="tab.id === activeTab?.id"
          v-qio-tip="tab.target.title + ' · ' + PREVIEW_KIND_LABEL[tab.target.kind]"
          @click="activate(tab.id)"
        >
          <span class="pv-tab-title">{{ tab.target.title }}</span>
          <span class="pv-tab-kind mono">{{ PREVIEW_KIND_LABEL[tab.target.kind] }}</span>
        </button>
        <button
          type="button"
          class="pv-tab-close"
          :aria-label="'关闭预览：' + tab.target.title"
          v-qio-tip="'关闭这个预览（不删除文件）'"
          @click.stop="closeTab(tab.id)"
        >×</button>
      </div>
    </div>

    <PreviewReader v-if="activeTab" :key="activeTab.id" :tab="activeTab" :full="full" />

    <div v-else class="pv-empty" data-preview-empty>
      <p class="pv-empty-title">从对话或任务里打开文件后在这里阅读</p>
      <p class="pv-empty-note">
        附件、回复里的代码块、任务成果都可以用「预览」在右侧打开；这里只读，不会修改原文件。
      </p>
    </div>

    <p v-if="shell.saveError" class="pv-save-error" role="alert">
      {{ shell.saveError }}
      <button type="button" class="pv-retry" @click="retrySave">重试</button>
    </p>
  </aside>
</template>

<style scoped>
.preview-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--bg-surface);
  border-left: 1px solid var(--border-subtle);
}
.preview-panel.full {
  /* 位置由父层 .preview-overlay 的 8px 卡片网格决定；这里只负责填满它。
     之前 inset:0 的绝对定位会覆盖父层 inset，导致预览贴死窗口左/右/下边。 */
  position: relative;
  inset: auto;
  z-index: 1;
  height: 100%;
  border-left: 0;
  border-radius: inherit;
  background: var(--bg-surface);
  box-shadow: none;
}
.pv-head {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  min-width: 0;
}
.pv-head-title {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}
.pv-head-label {
  font-family: var(--serif);
  color: var(--text-strong);
  font-size: var(--fs-sm);
  white-space: nowrap;
}
.pv-head-badge {
  flex: none;
  font-size: 10px;
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 0 6px;
}
.pv-head-actions {
  display: flex;
  gap: var(--sp-1);
  flex: none;
}
.pv-tabs {
  flex: none;
  display: flex;
  gap: 2px;
  overflow-x: auto;
  overflow-y: hidden;
  padding: 0 var(--sp-2);
  border-bottom: 1px solid var(--border-subtle);
  scrollbar-width: thin;
}
.pv-tab {
  flex: none;
  display: flex;
  align-items: center;
  min-width: 0;
  max-width: 210px;
  border-bottom: 2px solid transparent;
}
.pv-tab.is-active {
  border-bottom-color: var(--accent);
}
.pv-tab-main {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  min-width: 0;
  max-width: 180px;
  padding: var(--sp-1) var(--sp-1) var(--sp-1) var(--sp-2);
  border: 0;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  text-align: left;
}
.pv-tab.is-active .pv-tab-main {
  color: var(--text-strong);
}
.pv-tab-main:hover {
  color: var(--text-primary);
}
.pv-tab-main:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
  border-radius: var(--qio-ui-radius);
}
.pv-tab-title {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-xs);
}
.pv-tab-kind {
  font-size: 10px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.pv-tab-close {
  flex: none;
  width: 18px;
  height: 18px;
  margin-right: 2px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--qio-ui-radius);
  background: transparent;
  color: var(--text-faint);
  font-size: var(--fs-sm);
  line-height: 1;
  cursor: pointer;
}
.pv-tab-close:hover {
  background: var(--layer-hover);
  color: var(--text-strong);
}
.pv-tab-close:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.pv-empty {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: var(--sp-2);
  padding: var(--sp-6) var(--sp-5);
  color: var(--text-muted);
}
.pv-empty-title {
  margin: 0;
  font-family: var(--serif);
  color: var(--text-secondary);
  font-size: var(--fs-md);
  line-height: var(--lh-tight);
  overflow-wrap: anywhere;
}
.pv-empty-note {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  overflow-wrap: anywhere;
}
.pv-save-error {
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin: 0;
  padding: var(--sp-1) var(--sp-3);
  background: var(--danger-soft);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.pv-retry {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: transparent;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  padding: 0 var(--sp-2);
  cursor: pointer;
}
.pv-retry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
@media (prefers-reduced-motion: reduce) {
  .preview-panel *,
  .preview-panel *::before,
  .preview-panel *::after {
    transition: none !important;
    animation: none !important;
  }
}
</style>
