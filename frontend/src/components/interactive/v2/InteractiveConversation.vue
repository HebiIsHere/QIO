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
import { vQioTip } from "../../../composables/useQioTip";
import MessageStream from "../../MessageStream.vue";
import Composer from "../../Composer.vue";
import ComposerEntries from "../../conversation/ComposerEntries.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useSessionStore } from "../../../stores/session";
import { publishChatFrame } from "../../../interactive/chatFrameState";
import type { ChatFrame } from "../../../interactive/chatFrame";
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

/** 父级收紧/放宽高度预算（例如窄窗口给板面留空间）时，立即把面板夹回合法范围。 */
watch(
  () => [props.minHeight, props.maxHeight] as const,
  () => {
    panelHeight.value = effectiveHeight(panelHeight.value);
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

/* ------------------------------------------------------------------
 * C01：把**真实可见几何**发布给跨浮层避让桥（chatFrameState）
 *
 * 消费方 BoardCanvas 一直读 chatObstacleFrame()；本组件是这条桥在互动页的唯一生产者
 * （展开时发布，收起 / 卸载发布 null）。
 *
 * 发布口径（COMMON §4.3 / 简报 §1 C01）：
 * - 只量**实际可见内容**：展开时的消息面板 + 输入区（入口条 / 工具栏 / 输入框各自
 *   最大 860px、居中），不量外层全宽容器 —— 否则会把整块板面误判成不可用；
 * - 屏幕坐标（视口左上角为原点），与 BoardCanvas 的消费口径一致；
 * - 展开才发布矩形；收起 / 卸载发布 null（收起时输入区常驻但按既有契约不让浮层避让，
 *   也不留幽灵矩形）;
 * - 触发：挂载、展开 / 收起、高度变化、窗口 resize、根元素尺寸变化（ResizeObserver）。
 *   板面平移缩放不会移动这块浮层（它与画布同级、固定在舞台底部）；它带来的
 *   滚动条 / 舞台尺寸变化会经 ResizeObserver 触发重新测量，每次测量都读实时 DOM，
 *   不缓存、不从 localStorage 恢复任何假显示框。
 * - 用 requestAnimationFrame 合并同一帧内的多次触发；卸载时取消待执行的测量。
 * ------------------------------------------------------------------ */
const rootEl = ref<HTMLElement | null>(null);
/** 实际可见的组成块：消息面板（展开时）+ 输入区四块（各自居中、最大 860px）。 */
const VISIBLE_PART_SELECTORS = ["[data-ic-bar]", ".ic-entries", "[data-ic-toolbar]", ".ic-composer"] as const;

function rectOf(element: Element | null | undefined): DOMRect | null {
  if (!element || typeof (element as HTMLElement).getBoundingClientRect !== "function") return null;
  const rect = (element as HTMLElement).getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return null;
  return rect;
}

/** 当前实际可见内容的外接矩形（屏幕坐标）；量不到真实几何时返回 null（不编造）。 */
function measureVisibleFrame(): ChatFrame | null {
  const root = rootEl.value;
  if (!root) return null;
  const parts: DOMRect[] = [];
  // 消息面板：只有展开且真的渲染出来了才算（v-show 隐藏时不计入）
  if (expandedState.value && openedOnce.value) {
    const stream = rectOf(root.querySelector<HTMLElement>("[data-ic-stream]"));
    if (stream) parts.push(stream);
  }
  for (const selector of VISIBLE_PART_SELECTORS) {
    const part = rectOf(root.querySelector<HTMLElement>(selector));
    if (part) parts.push(part);
  }
  if (!parts.length) return null;
  let left = Number.POSITIVE_INFINITY;
  let top = Number.POSITIVE_INFINITY;
  let right = Number.NEGATIVE_INFINITY;
  let bottom = Number.NEGATIVE_INFINITY;
  for (const rect of parts) {
    left = Math.min(left, rect.left);
    top = Math.min(top, rect.top);
    right = Math.max(right, rect.right);
    bottom = Math.max(bottom, rect.bottom);
  }
  const width = Math.round(right - left);
  const height = Math.round(bottom - top);
  if (!(width > 0 && height > 0)) return null;
  return { x: Math.round(left), y: Math.round(top), w: width, h: height };
}

let publishRaf: number | null = null;

/** 立即按当前 DOM 发布一次：展开 → 真实可见矩形；收起 → null（不让浮层避让不存在的空间）。 */
function publishGeometryNow(): void {
  if (!expandedState.value) {
    publishChatFrame(null, true);
    return;
  }
  publishChatFrame(measureVisibleFrame(), false);
}

function schedulePublishGeometry(): void {
  if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
    publishGeometryNow();
    return;
  }
  if (publishRaf !== null) return;
  publishRaf = window.requestAnimationFrame(() => {
    publishRaf = null;
    publishGeometryNow();
  });
}

let observer: ResizeObserver | null = null;
let resizeBound = false;

function onWindowResize(): void {
  schedulePublishGeometry();
}

onMounted(() => {
  if (typeof window !== "undefined") {
    window.addEventListener("resize", onWindowResize);
    resizeBound = true;
  }
  // 根元素尺寸 = 消息面板 + 输入区整体外框：展开 / 收起 / 拖高 / 窗口变化 / 滚动条出现
  // 都会让它变化。元素尺寸变化不改变可见内容宽度时，下一次测量结果相同，发布幂等。
  const root = rootEl.value;
  if (root && typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(() => schedulePublishGeometry());
    observer.observe(root);
  }
  schedulePublishGeometry();
});

// 展开 / 收起 / 高度变化后 DOM 已更新，下一帧按新几何重新发布
watch([expandedState, panelHeight, openedOnce], () => schedulePublishGeometry(), { flush: "post" });

onBeforeUnmount(() => {
  if (typeof window !== "undefined" && resizeBound) window.removeEventListener("resize", onWindowResize);
  resizeBound = false;
  observer?.disconnect();
  observer = null;
  if (publishRaf !== null && typeof window !== "undefined") {
    window.cancelAnimationFrame(publishRaf);
    publishRaf = null;
  }
  // 卸载 = 这块空间不存在了：必须清空，不留幽灵障碍
  publishChatFrame(null, false);
});

defineExpose({
  expand,
  collapse,
  toggle,
  focusInput,
  expanded: expandedState,
  height: panelHeight,
  /** 只读给定向测试：当前可见内容的外接矩形（与发布值同一份算法） */
  measureVisibleFrame,
});

/** 发送绑定投影：只保留会话层认识的字段；没有绑定就是普通聊天（不带板面身份）。 */
const sendBinding = computed(() => conversationBindingForSend(props.binding));
</script>

<template>
  <section
    ref="rootEl"
    class="ic"
    data-ic="conversation"
    :data-ic-expanded="expandedState ? 'true' : 'false'"
    :style="{ '--ic-panel-h': panelHeight + 'px' }"
    role="region"
    :aria-label="title + '（底部互动对话）'"
  >
    <!-- 会话流：第一次展开才挂载，之后只隐藏 —— 收起不等于清空阅读位置与已渲染内容 -->
    <div v-if="openedOnce" v-show="expandedState" class="ic-stream" data-ic-stream :id="panelId">
      <button
        class="ic-close"
        type="button"
        data-ic-close
        aria-label="收起对话"
        v-qio-tip="'收起对话（不会停止正在进行的回复）'"
        @click="collapse"
      >×</button>
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
          :aria-label="expandedState ? '收起对话（不影响正在进行的回复）' : '展开对话（从输入区向上展开）'"
          v-qio-tip="expandedState ? '收起对话（不影响正在进行的回复）' : '展开对话（从输入区向上展开）'"
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

      <!-- 板面工具栏：固定在输入区上方；对话展开时消息在它上方增长，它和输入区不动。 -->
      <div class="ic-toolbar" data-ic-toolbar>
        <slot name="toolbar" />
      </div>

      <!-- 输入区：始终挂载；收起只改变可见区域，不清草稿 / 附件 / 排队，也不停止回复。
           点击 / 聚焦输入区时自动展开消息面板。 -->
      <div class="ic-composer" @focusin="expand">
        <div class="ic-composer-row">
          <slot name="composer-leading" />
          <div class="ic-composer-input">
            <Composer :chat-binding="sendBinding">
              <template #leading>
                <slot name="leading" />
              </template>
            </Composer>
          </div>
        </div>
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
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  margin: 0 var(--sp-3);
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--border-subtle) 72%, transparent);
  border-radius: var(--r-lg);
  /* FU7：对话栏透明 + 背景高斯模糊（板面从栏后透出）。 */
  background: transparent;
}
/*
  FU7 底层：整栏一层弱模糊 + 很淡的半透明底（只负责“透出板面”，不负责文字区域）。
  不把 backdrop-filter 直接放在 .ic-stream 上：那样会给子元素建立 backdrop root，
  文字块自己的更强模糊就不再采样板面了。放在 ::before 这一层，文字块仍在它之上。
*/
.ic-stream::before {
  content: "";
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  border-radius: inherit;
  background: color-mix(in srgb, var(--bg-base) 30%, transparent);
  -webkit-backdrop-filter: blur(12px) saturate(1.06);
  backdrop-filter: blur(12px) saturate(1.06);
}
.ic-stream > * {
  position: relative;
  z-index: 1;
}
/* 消息流自己原来是整块不透明底：在互动面板里必须让位给模糊层。 */
.ic-stream :deep(.stream) {
  background: transparent;
}
/*
  FU7 文字层：无框文字（用户消息 / 过程行 / 系统事实）下方再叠一层更强的高斯模糊。
  羽化 mask 让边界是“渐变过渡”，既和周围模糊有区别、又不会出现硬边；
  模糊层挂在文字块上，随消息滚动一起移动（不需要 JS 同步）。
*/
.ic-stream :deep(.user-bubble),
.ic-stream :deep(.process-line),
.ic-stream :deep(.system-fact) {
  position: relative;
  isolation: isolate;
}
.ic-stream :deep(.user-bubble)::before,
.ic-stream :deep(.process-line)::before,
.ic-stream :deep(.system-fact)::before {
  content: "";
  position: absolute;
  inset: -10px -14px;
  z-index: -1;
  border-radius: 18px;
  pointer-events: none;
  -webkit-backdrop-filter: blur(26px) saturate(1.08);
  backdrop-filter: blur(26px) saturate(1.08);
  background: color-mix(in srgb, var(--bg-base) 10%, transparent);
  -webkit-mask-image: linear-gradient(
      to bottom,
      transparent 0%,
      #000 14%,
      #000 86%,
      transparent 100%
    ),
    linear-gradient(to right, transparent 0%, #000 12%, #000 88%, transparent 100%);
  -webkit-mask-composite: source-in;
  mask-image: linear-gradient(
      to bottom,
      transparent 0%,
      #000 14%,
      #000 86%,
      transparent 100%
    ),
    linear-gradient(to right, transparent 0%, #000 12%, #000 88%, transparent 100%);
  mask-composite: intersect;
}
/* 浏览器不支持 backdrop-filter：退回半透明底（不假装有模糊）。 */
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  .ic-stream::before {
    background: color-mix(in srgb, var(--bg-base) 88%, transparent);
  }
}
/* 消息面板右上角圆形收起按钮：只收显示，不打断正在进行的回复。 */
.ic-close {
  position: absolute;
  top: 10px;
  right: 12px;
  z-index: 4;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid var(--border-subtle);
  border-radius: 50%;
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font: inherit;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}
.ic-close:hover { color: var(--accent); border-color: var(--accent); }
.ic-stream :deep(.empty) {
  padding: 0 var(--sp-3);
}
.ic-stream :deep(.empty .greet) {
  max-width: 100%;
  font-size: clamp(20px, 5vw, 34px);
  overflow-wrap: anywhere;
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

.ic-toolbar {
  min-width: 0;
  max-width: 860px;
  margin: 0 auto var(--sp-2);
  width: 100%;
}
/*
  输入行（FU5）：三列 grid —— 左侧 1fr | 中间输入列（≤860）| 右侧 1fr。
  * 输入列因此在面板内**真正居中**：不再被模式切换占掉的宽度推右；
  * 模式切换（composer-leading 槽位）放第一列、靠右紧贴输入列左侧，不占输入列宽度；
  * 窄窗（≤700）改单列：模式切换在输入上方、左对齐。
  * 两侧 1fr 允许收缩（minmax(0, …)），窄面板不会溢出。
*/
.ic-composer-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 860px) minmax(0, 1fr);
  align-items: end;
  column-gap: var(--sp-3);
  max-width: none;
  margin: 0;
  min-width: 0;
}
.ic-composer-input {
  grid-column: 2;
  min-width: 0;
}
/* 槽位内容由父级渲染（slot）：用 :slotted 才能给它定列。 */
.ic-composer-row :slotted(.im-mode-switch) {
  grid-column: 1;
  justify-self: end;
  align-self: end;
  min-width: 0;
  /* FU5：再往左让 20px —— 模式切换文字不要贴到输入胶囊的圆角/描边。 */
  margin-right: 20px;
}
@media (max-width: 1120px) {
  /* 窄窗：输入列铺满；模式切换放到「输入框上方的工具栏」同一行，不再单独占一行，
     也不允许被挤出窗口（此前的消失问题）。 */
  .ic-composer-row {
    grid-template-columns: minmax(0, 1fr);
    position: relative;
  }
  .ic-composer-input {
    grid-column: 1;
  }
  .ic-composer-row :slotted(.im-mode-switch) {
    position: absolute;
    left: 0;
    bottom: calc(100% + 2px);
    margin-right: 0;
    z-index: 2;
  }
  .ic-toolbar {
    /* 让开左侧的模式切换，工具按钮仍在剩余区域居中 */
    padding-left: 92px;
  }
}
@media (max-width: 700px) {
  .ic-composer-row {
    grid-template-columns: minmax(0, 1fr);
    row-gap: 6px;
    align-items: stretch;
  }
  .ic-composer-row :slotted(.im-mode-switch) {
    grid-column: 1;
    grid-row: 1;
    justify-self: start;
    align-self: start;
    margin-right: 0;
  }
  .ic-composer-input {
    grid-column: 1;
    grid-row: 2;
  }
}
.ic-composer {
  /* 外层必须铺满面板，grid 的三列才有「两侧 1fr」可用（max-width 交给中间列）。 */
  width: 100%;
  max-width: none;
  margin: 0;
  min-width: 0;
}
</style>
