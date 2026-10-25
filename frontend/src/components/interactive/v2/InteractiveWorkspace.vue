<!--
  互动页 v2 布局外壳（Phase 1-B，子智能体 p1-layout 负责）。

  契约：docs/interactive-page-v2-contract.md §2.3。
  - 板面占主区域；底部工具栏与输入区明确分层（工具栏在输入区上方），
    它们都**参与布局**而不是浮在板面上 —— 区域矩形互不相交，不覆盖板面；
  - 对话区从底部输入区一侧向上展开，有高度上限，且永远给板面保留最小高度；
  - 书签栏：宽档（≥1024）为右侧窄栏，中/窄档为受限高度的下侧区域；
  - 本组件**不读任何业务数据**、不输出标题栏：区域内容由 slot 提供，
    Lead 把现有 BoardCanvas / BoardToolbar / 后续对话组件填进对应 slot 即可。
  - 颜色/字体/圆角/动效一律用 var(--*) 令牌；几何尺寸由布局纯函数算出后写成 CSS 变量。

  对外契约（详见同目录 README.md）：
  props: bookmarksOpen（默认 true）、chatOpen（默认 false）
  slots: board / bookmarks / chat / toolbar / composer
  expose: measure()（重新测量，集成测试或外部布局变化时用）、layout（当前几何结果）
  根节点 data 属性：data-iw-band / data-iw-bookmark-dock / data-iw-bookmarks / data-iw-chat / data-iw-layout-issues
  根节点 CSS 变量：--iw-gap / --iw-bookmark-size / --iw-chat-max-h
-->
<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onBeforeUpdate,
  onMounted,
  reactive,
  ref,
  useSlots,
} from "vue";
import {
  COMPOSER_FALLBACK_HEIGHT,
  GAP,
  TOOLBAR_FALLBACK_HEIGHT,
  resolveWorkspaceLayout,
  type WorkspaceLayout,
} from "./layout";

const props = withDefaults(
  defineProps<{
    /** 书签栏是否展开（默认展开；没有 #bookmarks slot 时不会渲染）。 */
    bookmarksOpen?: boolean;
    /** 对话区是否展开（默认收起；没有 #chat slot 时不会渲染）。 */
    chatOpen?: boolean;
  }>(),
  { bookmarksOpen: true, chatOpen: false },
);

const slots = useSlots();

const root = ref<HTMLElement | null>(null);
const inner = ref<HTMLElement | null>(null);
const toolbarEl = ref<HTMLElement | null>(null);
const composerEl = ref<HTMLElement | null>(null);

/** 实测尺寸：宽度/高度取工作区内容盒（.iw-inner），工具栏/输入区取各自真实高度。 */
const metrics = reactive({ width: 0, height: 0, toolbar: 0, composer: 0 });

/*
 * slot 是否存在不是响应式的（useSlots 返回的是普通对象），但布局必须跟着它变：
 * 这里在每次更新前同步一份响应式快照，避免「父级后来才提供 #toolbar」时区域不出现。
 */
const slotPresence = reactive({
  toolbar: false,
  composer: false,
  bookmarks: false,
  chat: false,
});

function syncSlotPresence(): void {
  slotPresence.toolbar = Boolean(slots.toolbar);
  slotPresence.composer = Boolean(slots.composer);
  slotPresence.bookmarks = Boolean(slots.bookmarks);
  slotPresence.chat = Boolean(slots.chat);
}

syncSlotPresence();
onBeforeUpdate(syncSlotPresence);

const hasToolbar = computed(() => slotPresence.toolbar);
const hasComposer = computed(() => slotPresence.composer);
const hasBookmarks = computed(() => slotPresence.bookmarks);
const hasChat = computed(() => slotPresence.chat);

/** 布局决策与别处共用同一份纯函数（Lead / 测试可直接 import 复算）。 */
const layout = computed<WorkspaceLayout>(() =>
  resolveWorkspaceLayout({
    width: metrics.width,
    height: metrics.height,
    bookmarksOpen: props.bookmarksOpen && hasBookmarks.value,
    chatOpen: props.chatOpen && hasChat.value,
    toolbarHeight: hasToolbar.value ? metrics.toolbar || TOOLBAR_FALLBACK_HEIGHT : 0,
    composerHeight: hasComposer.value ? metrics.composer || COMPOSER_FALLBACK_HEIGHT : 0,
  }),
);

function box(el: HTMLElement | null): { width: number; height: number } {
  if (!el) return { width: 0, height: 0 };
  const rect = typeof el.getBoundingClientRect === "function" ? el.getBoundingClientRect() : null;
  return { width: Math.round(rect?.width ?? 0), height: Math.round(rect?.height ?? 0) };
}

/** 重新测量一次。ResizeObserver 变化时自动调用；也可由集成方主动调用（如页面恢复后）。 */
function measure(): void {
  const innerBox = box(inner.value);
  if (innerBox.width > 0) metrics.width = innerBox.width;
  if (innerBox.height > 0) metrics.height = innerBox.height;
  metrics.toolbar = box(toolbarEl.value).height;
  metrics.composer = box(composerEl.value).height;
}

let observer: ResizeObserver | null = null;

onMounted(() => {
  measure();
  if (typeof window === "undefined") return;
  if (typeof ResizeObserver === "undefined") {
    window.addEventListener("resize", measure);
    return;
  }
  observer = new ResizeObserver(() => measure());
  for (const target of [inner.value, toolbarEl.value, composerEl.value]) {
    if (target) observer.observe(target);
  }
});

onBeforeUnmount(() => {
  observer?.disconnect();
  observer = null;
  if (typeof window !== "undefined") window.removeEventListener("resize", measure);
});

const styleVars = computed(() => ({
  "--iw-gap": GAP + "px",
  "--iw-bookmark-size": layout.value.bookmarkSize + "px",
  "--iw-chat-max-h": layout.value.chatMaxHeight + "px",
}));

defineExpose({ measure, layout });
</script>

<template>
  <div
    ref="root"
    class="iw"
    :data-iw-band="layout.band"
    :data-iw-bookmark-dock="layout.bookmarkDock"
    :data-iw-bookmarks="layout.bookmarks ? 'open' : 'closed'"
    :data-iw-chat="layout.chat ? 'open' : 'closed'"
    :data-iw-layout-issues="layout.issues.length ? layout.issues.join(' ') : undefined"
    :style="styleVars"
  >
    <div ref="inner" class="iw-inner">
      <div class="iw-body">
        <section class="iw-board" data-iw-region="board" aria-label="板面">
          <slot name="board" />
        </section>

        <!-- 桌面：书签栏在板面右侧，是一条窄栏（不覆盖板面，板面自己让出宽度） -->
        <aside
          v-if="layout.bookmarks && layout.bookmarkDock === 'side'"
          class="iw-bookmarks iw-bookmarks--side"
          data-iw-region="bookmarks"
          aria-label="书签栏"
        >
          <slot name="bookmarks" />
        </aside>
      </div>

      <!-- 对话区：打开时从底部输入区一侧向上展开；高度上限来自布局纯函数 -->
      <section v-if="layout.chat" class="iw-chat" data-iw-region="chat" aria-label="对话区">
        <slot name="chat" />
      </section>

      <!-- 窄窗口：书签栏改为受限高度的下侧区域 -->
      <aside
        v-if="layout.bookmarks && layout.bookmarkDock === 'bottom'"
        class="iw-bookmarks iw-bookmarks--bottom"
        data-iw-region="bookmarks"
        aria-label="书签栏"
      >
        <slot name="bookmarks" />
      </aside>

      <!-- 工具栏在输入区上方：两块各自独立成层，中间留同一档间距 -->
      <section
        v-if="hasToolbar"
        ref="toolbarEl"
        class="iw-toolbar"
        data-iw-region="toolbar"
        aria-label="板面工具"
      >
        <slot name="toolbar" />
      </section>
      <section
        v-if="hasComposer"
        ref="composerEl"
        class="iw-composer"
        data-iw-region="composer"
        aria-label="输入区"
      >
        <slot name="composer" />
      </section>
    </div>
  </div>
</template>

<style scoped>
/* 页面外沿与对话页同一套节奏：顶部 2px（紧贴全局栏卡片下沿）、左右/底 8px。 */
.iw {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  min-width: 0;
  padding: 2px 8px 8px;
  color: var(--text-primary);
  font-family: var(--sans);
}

/* 内容盒：布局纯函数量到的宽高就是它。 */
.iw-inner {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--iw-gap);
  min-height: 0;
  min-width: 0;
}

.iw-body {
  flex: 1;
  display: flex;
  gap: var(--iw-gap);
  min-height: 0;
  min-width: 0;
}

/* 板面主区：圆形卡片容器，同时是现有板面浮层（工具栏/聊天）的定位舞台。 */
.iw-board {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-surface);
}

.iw-bookmarks {
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-surface);
}
.iw-bookmarks--side {
  flex: none;
  width: var(--iw-bookmark-size);
}
.iw-bookmarks--bottom {
  flex: none;
  width: 100%;
  height: var(--iw-bookmark-size);
}

/*
  对话区：按布局纯函数给出的高度展开（上游已保证板面仍 ≥ 最小高度），
  内容在自己的区域内滚动；进入时只做一次短淡入 + 轻位移，减少动画时由 base.css 压到 90ms。
*/
.iw-chat {
  flex: none;
  display: flex;
  flex-direction: column;
  height: var(--iw-chat-max-h);
  max-height: var(--iw-chat-max-h);
  min-height: 0;
  overflow: hidden;
  animation: iw-chat-in var(--mo-2-in) var(--ease-2) both;
}
@keyframes iw-chat-in {
  from {
    opacity: 0;
    transform: translateY(var(--shift-4));
  }
  to {
    opacity: 1;
    transform: none;
  }
}

/* 工具栏 / 输入区只占位，不替内容画表面：视觉由现有组件自己的令牌样式负责。 */
.iw-toolbar,
.iw-composer {
  flex: none;
  min-width: 0;
}
</style>
