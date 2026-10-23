<!--
  底部互动对话区（Phase 3-B）：输入区在下、会话流从输入区向上展开。

  契约：docs/interactive-page-v2-contract.md §6.4。
  - 复用现有 MessageStream / Composer / ComposerEntries / 会话 store，**不新建简化模型**：
    发送/停止/排队/媒体/草稿/失败恢复/五入口全部由这些既有组件承担；
  - 展开 / 收起只改变**显示**：不卸载 Composer、不清草稿与附件、不停止回复、不动排队；
  - 高度可以拖动（分隔条）或用键盘调整，口径与视口约束在 conversationLayout 的纯函数里；
  - 板面操作（interactive store 的 lastOpLabel 前进）时自动收起，但回复继续 —— 收起不是停止；
  - 互动绑定由 props 可信传入（board/submission/intent），只作为发送选项投影给会话层，
    绝不拼进用户原文（板面提交与聊天发送是两件事）。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";
import MessageStream from "../../MessageStream.vue";
import Composer from "../../Composer.vue";
import ComposerEntries from "../../conversation/ComposerEntries.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useSessionStore } from "../../../stores/session";
import type { ExtraComposerEntry } from "../../conversation/ComposerEntries.vue";
import {
  CONVERSATION_DEFAULT_HEIGHT,
  CONVERSATION_KEYBOARD_STEP,
  CONVERSATION_MAX_HEIGHT,
  CONVERSATION_MIN_HEIGHT,
  clampConversationHeight,
  conversationBindingForSend,
  conversationStatusText,
  parseStoredConversationHeight,
  type ConversationBinding,
} from "./conversationLayout";

const props = withDefaults(
  defineProps<{
    /** 是否展开（支持 v-model:expanded；不传时也能自己切换）。 */
    expanded?: boolean;
    /** 展开面板高度（0 = 用本机记忆 / 默认值）。 */
    height?: number;
    minHeight?: number;
    maxHeight?: number;
    /** 板面操作（interactive store 的 lastOpLabel 前进）时自动收起；只收显示，不打断回复。 */
    autoCollapseOnBoardAction?: boolean;
    title?: string;
    /** 可信传入的互动绑定：只投影给会话层发送选项，不进入用户原文。 */
    binding?: ConversationBinding;
    /** 高度记忆的存储键（缺省按板面身份）。 */
    storageKey?: string;
    /** 额外事项行入口（Phase 3-C：恢复与收尾）。 */
    extraEntries?: ExtraComposerEntry[];
  }>(),
  {
    expanded: false,
    height: 0,
    minHeight: CONVERSATION_MIN_HEIGHT,
    maxHeight: CONVERSATION_MAX_HEIGHT,
    autoCollapseOnBoardAction: true,
    title: "对话",
    storageKey: "",
    extraEntries: () => [],
  },
);

const emit = defineEmits<{
  (e: "update:expanded", value: boolean): void;
  (e: "expand"): void;
  (e: "collapse"): void;
  (e: "update:height", value: number): void;
  (e: "resize-start"): void;
  (e: "resize-end", value: number): void;
}>();

const interactive = useInteractiveStore();
const session = useSessionStore();
const uid = useId();
const panelId = "ic-panel-" + uid;
const barId = "ic-bar-" + uid;

const expandedState = ref(Boolean(props.expanded));
/** 只有真正展开过一次才挂载会话流：之后用 v-show 保留滚动位置与已渲染内容。 */
const openedOnce = ref(Boolean(props.expanded));
/** 初始高度：显式 prop 优先，其次本机记忆，最后默认值（都在视口口径内）。 */
function bootstrapHeight(): number {
  if (props.height > 0) return effectiveHeight(props.height);
  return effectiveHeight(readStoredHeight() ?? CONVERSATION_DEFAULT_HEIGHT);
}
const panelHeight = ref(bootstrapHeight());

const storageKey = computed(
  () =>
    props.storageKey ||
    "qio-interactive-conversation:" + (props.binding?.boardId?.trim() || "default") + ":height",
);

function viewportHeight(): number {
  return typeof window === "undefined" ? 0 : window.innerHeight;
}

function effectiveHeight(value: number): number {
  return clampConversationHeight(value, {
    min: props.minHeight,
    max: props.maxHeight,
    viewportHeight: viewportHeight(),
  });
}

function readStoredHeight(): number | null {
  try {
    return parseStoredConversationHeight(globalThis.localStorage?.getItem(storageKey.value));
  } catch {
    return null;
  }
}

function writeStoredHeight(value: number): void {
  try {
    globalThis.localStorage?.setItem(storageKey.value, String(value));
  } catch {
    // 存储不可用只影响「下次打开的高度」，不影响对话本身
  }
}

function initialHeight(): number {
  if (props.height > 0) return effectiveHeight(props.height);
  const stored = readStoredHeight();
  return effectiveHeight(stored ?? CONVERSATION_DEFAULT_HEIGHT);
}

watch(
  () => props.expanded,
  (value) => {
    if (expandedState.value === value) return;
    expandedState.value = value;
    if (value) openedOnce.value = true;
  },
);

watch(
  () => props.height,
  (value) => {
    if (value > 0) panelHeight.value = effectiveHeight(value);
  },
);

watch(panelHeight, (value) => writeStoredHeight(value));

onMounted(() => {
  // 视口可能在挂载时才可读（jsdom / 首次渲染）：再对齐一次口径
  panelHeight.value = initialHeight();
  // 首次也记一次：下次打开保持同一高度（记忆的是「用户看到的那一档」）
  writeStoredHeight(panelHeight.value);
  if (expandedState.value) openedOnce.value = true;
});

/** 展开 / 收起：只动显示，不调用会话层的任何方法（回复、草稿、附件、排队都不受影响）。 */
function setExpanded(next: boolean): void {
  const value = Boolean(next);
  if (expandedState.value === value) return;
  expandedState.value = value;
  if (value) openedOnce.value = true;
  emit("update:expanded", value);
  if (value) emit("expand");
  else emit("collapse");
}

const expand = () => setExpanded(true);
const collapse = () => setExpanded(false);
const toggle = () => setExpanded(!expandedState.value);

/** 收起态 / 展开态共用的一句真实状态（回复没有被收起打断时要看得见）。 */
const statusText = computed(() =>
  conversationStatusText({
    turnRunning: session.turnRunning,
    queuedCount: session.turnQueue?.queued?.length ?? 0,
    hasDraft: Boolean(String(session.draft ?? "").trim()),
  }),
);

const open = computed(() => expandedState.value);

/**
 * 板面操作自动收起：interactive store 的 lastOpLabel 只在 commit（板面操作）时前进，
 * 发送聊天不会触发它 —— 所以这里能把「板面操作」与「聊天」分开。
 * 只改显示；正在进行的回复、草稿、附件与排队一个字都不动。
 */
watch(
  () => interactive.lastOpLabel,
  (label, previous) => {
    if (!props.autoCollapseOnBoardAction) return;
    if (!expandedState.value) return;
    if (!label || label === previous) return;
    setExpanded(false);
  },
);

/* ── 高度调整（分隔条：指针拖动 + 键盘） ────────────────────────────────── */
interface ResizeDrag {
  startY: number;
  startHeight: number;
  moved: boolean;
}

let drag: ResizeDrag | null = null;

function removeResizeListeners(): void {
  document.removeEventListener("pointermove", onResizeMove, true);
  document.removeEventListener("pointerup", onResizeEnd, true);
  document.removeEventListener("pointercancel", onResizeCancel, true);
  document.removeEventListener("keydown", onResizeKeydown, true);
}

function onResizeMove(event: Event): void {
  if (!drag) return;
  const pointer = event as MouseEvent;
  const delta = drag.startHeight - (pointer.clientY - drag.startY);
  const next = effectiveHeight(delta);
  if (Math.abs(next - drag.startHeight) >= 1) drag.moved = true;
  panelHeight.value = next;
  emit("update:height", next);
}

function onResizeEnd(): void {
  if (!drag) return;
  const moved = drag.moved;
  drag = null;
  removeResizeListeners();
  if (moved) emit("resize-end", panelHeight.value);
}

function onResizeCancel(): void {
  if (!drag) return;
  const before = drag.startHeight;
  drag = null;
  removeResizeListeners();
  panelHeight.value = before;
  emit("update:height", before);
}

function onResizeKeydown(event: Event): void {
  if (!drag) return;
  if ((event as KeyboardEvent).key !== "Escape") return;
  (event as KeyboardEvent).preventDefault();
  onResizeCancel();
}

function onResizePointerDown(event: MouseEvent): void {
  if (drag) return;
  if (event.button !== undefined && event.button !== 0) return;
  drag = { startY: event.clientY, startHeight: panelHeight.value, moved: false };
  emit("resize-start");
  document.addEventListener("pointermove", onResizeMove, true);
  document.addEventListener("pointerup", onResizeEnd, true);
  document.addEventListener("pointercancel", onResizeCancel, true);
  document.addEventListener("keydown", onResizeKeydown, true);
  event.preventDefault();
}

const handleMin = computed(() => effectiveHeight(props.minHeight));
const handleMax = computed(() => effectiveHeight(props.maxHeight));

/** 键盘调整高度：上下方向键 ±步长，Home / End 到两端（与拖动共用同一套夹取）。 */
function onHandleKeydown(event: KeyboardEvent): void {
  const keys = ["ArrowUp", "ArrowDown", "Home", "End"];
  if (!keys.includes(event.key)) return;
  event.preventDefault();
  let next = panelHeight.value;
  if (event.key === "ArrowUp") next += CONVERSATION_KEYBOARD_STEP;
  else if (event.key === "ArrowDown") next -= CONVERSATION_KEYBOARD_STEP;
  else if (event.key === "Home") next = handleMin.value;
  else next = handleMax.value;
  next = effectiveHeight(next);
  panelHeight.value = next;
  emit("update:height", next);
  emit("resize-end", next);
}

function focusInput(): void {
  if (typeof document === "undefined") return;
  document.getElementById("composer-input")?.focus();
}

onBeforeUnmount(removeResizeListeners);

defineExpose({ expand, collapse, toggle, focusInput, expanded: expandedState, height: panelHeight });

/** 发送绑定投影：只保留会话层认识的字段；没有绑定就是普通聊天（不带板面身份）。 */
const sendBinding = computed(() => conversationBindingForSend(props.binding));
</script>

<template>
  <section
    class="ic"
    data-ic="conversation"
    :data-ic-expanded="expandedState ? 'true' : 'false'"
    :style="{ '--ic-panel-h': panelHeight + 'px' }"
    role="region"
    :aria-label="title + '（底部互动对话）'"
  >
    <!-- 会话流：第一次展开才挂载，之后只隐藏 —— 收起不等于清空阅读位置与已渲染内容 -->
    <div v-if="openedOnce" v-show="expandedState" class="ic-stream" data-ic-stream :id="panelId">
      <MessageStream />
    </div>

    <!-- 高度分隔条：指针拖动 / 上下方向键；收起时不占位 -->
    <div
      v-if="expandedState"
      class="ic-resize"
      data-ic-resize
      role="separator"
      aria-orientation="horizontal"
      :aria-label="title + '高度调整（上下方向键，Home/End 到两端）'"
      :aria-valuemin="handleMin"
      :aria-valuemax="handleMax"
      :aria-valuenow="panelHeight"
      :aria-valuetext="panelHeight + ' 像素'"
      tabindex="0"
      @pointerdown="onResizePointerDown"
      @keydown="onHandleKeydown"
    ></div>

    <div class="ic-dock">
      <!-- 收起条：状态（回复中/排队/草稿）+ 唯一的展开入口 -->
      <div class="ic-bar" data-ic-bar :id="barId">
        <span class="ic-status mono" role="status" data-ic-status>{{ statusText }}</span>
        <button
          class="ic-toggle"
          type="button"
          data-ic-toggle
          :aria-expanded="expandedState ? 'true' : 'false'"
          :aria-controls="panelId"
          :title="expandedState ? '收起对话（不影响正在进行的回复）' : '展开对话（从输入区向上展开）'"
          @click="toggle"
        >
          <span class="ic-toggle-mark" aria-hidden="true">{{ expandedState ? "▾" : "▴" }}</span>
          {{ expandedState ? "收起对话" : "展开对话" }}
        </button>
        <span class="ic-bar-spacer"></span>
        <slot name="header-extra" />
      </div>

      <!-- 五入口（附件 / 失败恢复 / 排队 / 历史继续 / 话题建议 / 审批）：始终在输入区正上方 -->
      <div class="ic-entries">
        <ComposerEntries context="interactive" :extra-entries="props.extraEntries">
          <template #extra-panel="scope">
            <slot name="extra-panel" v-bind="scope" />
          </template>
        </ComposerEntries>
      </div>

      <!-- 输入区：始终挂载；收起只改变可见区域，不清草稿 / 附件 / 排队，也不停止回复 -->
      <div class="ic-composer">
        <Composer :chat-binding="sendBinding">
          <template #leading>
            <slot name="leading" />
          </template>
        </Composer>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ic {
  display: flex;
  flex-direction: column;
  min-width: 0;
  width: 100%;
  font-family: var(--sans);
  color: var(--text-primary);
}
/* 展开：面板高度来自本机记忆 / 拖动；会话流占满剩余，输入区固定在底部。 */
.ic[data-ic-expanded="true"] {
  height: var(--ic-panel-h);
  min-height: 0;
}
.ic-stream {
  flex: 1;
  min-height: 0;
  display: flex;
  margin: 0 var(--sp-3);
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-base);
}
.ic-stream :deep(.stream) {
  flex: 1;
  min-height: 0;
  height: 100%;
  padding-top: var(--sp-5);
}
/* 分隔条：10px 手势带 + 中间一枚短横；视觉克制，热区清楚。 */
.ic-resize {
  flex: none;
  position: relative;
  height: 10px;
  margin: 2px var(--sp-3) 0;
  cursor: ns-resize;
  touch-action: none;
}
.ic-resize::after {
  content: "";
  position: absolute;
  left: 50%;
  top: 50%;
  width: 42px;
  height: 2px;
  transform: translate(-50%, -50%);
  border-radius: var(--r-pill);
  background: var(--border-strong);
}
.ic-resize:hover::after,
.ic-resize:focus-visible::after {
  background: var(--accent);
}
.ic-resize:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: -2px;
}
.ic-dock {
  flex: none;
  min-width: 0;
  padding: 0 var(--sp-3) var(--sp-2);
}
.ic-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  max-width: 860px;
  margin: 0 auto;
  min-height: 32px;
  min-width: 0;
}
.ic-status {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.ic-toggle {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  height: 26px;
  padding: 0 var(--sp-2);
  border: 1px solid transparent;
  border-radius: var(--r-pill);
  background: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease),
    border-color var(--dur-fast) var(--ease);
}
.ic-toggle:hover {
  color: var(--text-strong);
  background: var(--layer-hover);
  border-color: var(--border-subtle);
}
.ic-toggle:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.ic-toggle-mark {
  color: var(--text-faint);
}
.ic-bar-spacer {
  flex: 1;
  min-width: 0;
}
.ic-entries {
  max-width: 860px;
  margin: 0 auto;
  min-width: 0;
}
.ic-composer {
  max-width: 860px;
  margin: 0 auto;
  min-width: 0;
}
</style>
