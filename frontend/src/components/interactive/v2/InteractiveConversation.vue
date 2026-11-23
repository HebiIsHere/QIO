<!--
  底部互动对话区（Phase 3-B / 互动模式底部操作区 2026-11）。

  契约：docs/interactive-page-v2-contract.md §6.4；本次按用户确认的「互动模式底部操作区」重做显示层：
  - 收起：一条轻量操作行（左＝模式切换 + 真实状态，中＝常驻把手，右＝板面工具）+ 下方独立胶囊输入框；
  - 展开：消息区从操作行上方长出，操作行仍在输入框上方；同一根把手留在**面板顶边**，
    顶边由把手与左右两段细线接成，两侧细线沿面板边向下、到输入胶囊水平中线前淡出；
  - 线框用两条镜像 SVG 开放路径按弧长揭示：出现 / 消失同速，共用一条
    easeInOutCubic（慢-快-慢）时间轴，与高度同帧更新；
  - 消息内容固定在面板顶边下方、与把手同速升降；被操作行裁切时同步虚化（blur + 淡出），
    把手始终在消息上方、不遮挡消息；
  - 玻璃材质集中在聊天面板：按真实面板范围裁剪、随揭示渐显，板面从后面透出；
  - 开合 / 拖高只改显示：不卸载 Composer、不清草稿与附件、不停止回复、不动排队；
  - 板面操作（interactive store 的 lastOpLabel 前进）时自动收起，但回复继续 —— 收起不是停止；
  - 高度拖动 / 键盘 / 记忆口径仍在 conversationLayout 的纯函数；动效与线框几何在 conversationMotion；
  - 真实可见几何继续发布到 chatFrameState（展开发布、收起 / 卸载发布 null）。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";
import { vQioTip } from "../../../composables/useQioTip";
import MessageStream from "../../MessageStream.vue";
import Composer from "../../Composer.vue";
import ComposerEntries from "../../conversation/ComposerEntries.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useSessionStore } from "../../../stores/session";
import { contentViewport } from "../../../utils/windowFrame";
import { publishChatFrame } from "../../../interactive/chatFrameState";
import { registerEscapeLayer } from "../../../composables/useEscapeLayer";
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
import {
  CONVERSATION_MOTION,
  conversationContentMist,
  conversationFrameGeometry,
  easeInOutCubic,
} from "./conversationMotion";

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
const fadeId = "ic-frame-fade-" + uid;

/* ── DOM 引用 ───────────────────────────────────────────────────────────── */
const rootEl = ref<HTMLElement | null>(null);
const glassEl = ref<HTMLElement | null>(null);
const outlineEl = ref<SVGSVGElement | null>(null);
const leftPathEl = ref<SVGPathElement | null>(null);
const rightPathEl = ref<SVGPathElement | null>(null);
const streamWrapEl = ref<HTMLElement | null>(null);
const streamEl = ref<HTMLElement | null>(null);
const railEl = ref<HTMLElement | null>(null);
const entriesEl = ref<HTMLElement | null>(null);
const composerEl = ref<HTMLElement | null>(null);
const leverEl = ref<HTMLButtonElement | null>(null);

/* ── 展开状态 / 高度记忆（口径不变：记忆的是「展开时整块面板总高」） ──────── */
const expandedState = ref(Boolean(props.expanded));
/** 只有真正展开过一次才挂载会话流：之后用高度 0 收起，保留滚动位置与已渲染内容。 */
const openedOnce = ref(Boolean(props.expanded));

function viewportHeight(): number {
  /* 可见内容区高度：透明留白窗里不再把 2*pad 算进可用高度 */
  return typeof window === "undefined" ? 0 : contentViewport().height;
}

const storageKey = computed(
  () =>
    props.storageKey ||
    "qio-interactive-conversation:" + (props.binding?.boardId?.trim() || "default") + ":height",
);

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

/** 初始高度：显式 prop 优先，其次本机记忆，最后默认值（都在视口口径内）。 */
function initialHeight(): number {
  if (props.height > 0) return effectiveHeight(props.height);
  const stored = readStoredHeight();
  return effectiveHeight(stored ?? CONVERSATION_DEFAULT_HEIGHT);
}

const panelHeight = ref(initialHeight());
/** 操作行 + 事项行 + 输入区（不含消息区）的实测高度：面板总高 - 消息区高。 */
const chromeHeight = ref(0);

watch(panelHeight, (value) => writeStoredHeight(value));

/* ── 动效：单条缓动时间轴（高度 / 揭示 / 虚化同一进度；出现与消失同速） ──── */
/** 归一化开合进度：0 收起、1 展开（消息高度、线框揭示、内容虚化共用）。 */
const progress = ref(0);
/** 当前消息区显示高度（px，动画值） */
const streamShown = ref(0);
/** 正在进行的开合动画；中途反向时从当前进度重启，时长按剩余距离等比缩短。 */
let progressAnim: { from: number; to: number; startedAt: number; durationMs: number } | null = null;
/** 消息区是否可见（收起动画归零后隐藏，保留 DOM 与阅读位置；挂载即展开时首帧就可见） */
const streamVisible = ref(Boolean(props.expanded));
const dragging = ref(false);
const reduced = ref(false);

const rootMediaQuery: MediaQueryList | null =
  typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;

function prefersReducedMotion(): boolean {
  return Boolean(rootMediaQuery?.matches);
}

let motionRaf: number | null = null;

function motionAllowed(): boolean {
  if (prefersReducedMotion()) return false;
  const root = rootEl.value;
  if (!root || typeof root.getBoundingClientRect !== "function") return false;
  /* jsdom / 隐藏中的面板量不到真实宽度：直接落最终几何，不做看起来动不了的动画。 */
  const rect = root.getBoundingClientRect();
  return Boolean(rect && rect.width > 0);
}

function measureChromeHeight(): number {
  const root = rootEl.value;
  if (!root) return 0;
  const streamH = streamWrapEl.value?.offsetHeight ?? 0;
  return Math.max(0, root.offsetHeight - streamH);
}

/** 展开态消息区目标高度（面板总高 - 操作行/事项行/输入区实测高度）。 */
function openStreamHeight(): number {
  return Math.max(0, panelHeight.value - chromeHeight.value);
}

/** 进度 → 消息区高度（px）；非法进度按 0 处理。 */
function streamHeightAt(value: number): number {
  const p = Math.min(1, Math.max(0, Number.isFinite(value) ? value : 0));
  return Math.max(0, openStreamHeight() * p);
}

/** 当前展开状态对应的目标进度。 */
function targetProgress(): number {
  return expandedState.value ? 1 : 0;
}

/** 用当前状态重算固定区高度（操作行 / 事项行 / 输入区，不含消息区）。 */
function syncTargets(): void {
  chromeHeight.value = measureChromeHeight();
}

/** 直接落到指定进度（减少动画 / 量不到真实宽度时的降级路径）。 */
function snapToProgress(target: number): void {
  stopMotion();
  progress.value = Math.min(1, Math.max(0, Number.isFinite(target) ? target : 0));
  streamShown.value = streamHeightAt(progress.value);
}

function stopMotion(): void {
  progressAnim = null;
  if (motionRaf !== null) {
    window.cancelAnimationFrame(motionRaf);
    motionRaf = null;
  }
}

/**
 * 启动 / 继续开合动画：出现与消失都是 durationMs（650ms）的 easeInOutCubic；
 * 中途反向时从当前进度出发、时长按剩余距离等比缩短，速度口径保持一致。
 */
function startMotion(): void {
  if (!motionAllowed()) {
    snapToProgress(targetProgress());
    drawFrame();
    return;
  }
  const target = targetProgress();
  const distance = Math.abs(target - progress.value);
  if (distance <= CONVERSATION_MOTION.progressEpsilon) {
    stopMotion();
    progress.value = target;
    streamShown.value = streamHeightAt(target);
    drawFrame();
    return;
  }
  progressAnim = {
    from: progress.value,
    to: target,
    startedAt: performance.now(),
    durationMs: Math.max(1, CONVERSATION_MOTION.durationMs * distance),
  };
  if (motionRaf === null) motionRaf = window.requestAnimationFrame(stepMotion);
}

function stepMotion(now: number): void {
  motionRaf = null;
  const anim = progressAnim;
  if (!anim) return;
  const u = Math.min(1, Math.max(0, (now - anim.startedAt) / anim.durationMs));
  progress.value = anim.from + (anim.to - anim.from) * easeInOutCubic(u);
  streamShown.value = streamHeightAt(progress.value);
  drawFrame();
  if (u >= 1) {
    progressAnim = null;
    progress.value = anim.to;
    streamShown.value = streamHeightAt(anim.to);
    drawFrame();
    return;
  }
  motionRaf = window.requestAnimationFrame(stepMotion);
}

/* ── 线框 / 玻璃 / 面板几何（每帧从实时 DOM 量，同一帧更新） ─────────────── */
function rectOf(element: Element | null | undefined): DOMRect | null {
  if (!element || typeof (element as HTMLElement).getBoundingClientRect !== "function") return null;
  const rect = (element as HTMLElement).getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return null;
  return rect;
}

/** 输入胶囊的实测矩形（线框淡出终点 = 胶囊水平中线）。 */
function composerCapsuleRect(): DOMRect | null {
  const capsule = composerEl.value?.querySelector<HTMLElement>(".input-pill") ?? null;
  return rectOf(capsule) ?? rectOf(composerEl.value);
}

let lastFrame: ReturnType<typeof conversationFrameGeometry> | null = null;

function drawFrame(): void {
  const root = rootEl.value;
  if (!root) return;
  const streamH = Math.max(0, streamShown.value);
  root.style.setProperty("--ic-stream-h", streamH.toFixed(2) + "px");
  root.style.setProperty("--ic-panel-h", Math.round(streamH + chromeHeight.value) + "px");
  const showStream = streamH > 0.5;
  if (streamVisible.value !== showStream) streamVisible.value = showStream;

  const reveal = Math.min(1, Math.max(0, progress.value));
  root.dataset.icReveal = reveal.toFixed(4);
  /* 消息内容虚化：与开合同一条进度（下沉时模糊 + 淡出，升起时反向恢复）。 */
  const mist = conversationContentMist(reveal);
  root.dataset.icMist = mist.mist.toFixed(4);
  const stream = streamEl.value;
  if (stream) {
    stream.style.opacity = mist.opacity.toFixed(4);
    stream.style.filter = mist.blurPx > 0.01 ? "blur(" + mist.blurPx.toFixed(2) + "px)" : "none";
  }

  const rootRect = typeof root.getBoundingClientRect === "function" ? root.getBoundingClientRect() : null;
  const leverRect = rectOf(leverEl.value);
  const inputRect = composerCapsuleRect();
  const width = Math.round(rootRect?.width ?? root.clientWidth ?? 0);
  const frame = conversationFrameGeometry({
    panelWidth: width,
    leverWidth: leverRect?.width ?? 0,
    leverTop: (leverRect?.top ?? 0) - (rootRect?.top ?? 0),
    leverHeight: leverRect?.height ?? 0,
    inputTop: (inputRect?.top ?? 0) - (rootRect?.top ?? 0),
    inputHeight: inputRect?.height ?? 0,
    reveal,
  });
  lastFrame = frame;

  /* 消息 / 滚动指示条只在可见顶边线以下绘制（把手中心线）：防止滚动内容或滚动条溢到边框上方。 */
  root.style.setProperty("--ic-frame-top", Math.max(0, frame.leverY).toFixed(2) + "px");
  /* 空态没有可滚动内容：标记 data-ic-empty 以隐藏滚动指示条；
     空态的位置由 .ic-stream 的顶部 clip + 自身 40px 偏移保证，不会被滚动内容带出边框。 */
  const scrollHost = streamEl.value?.querySelector<HTMLElement>(".stream") ?? null;
  const emptyOverlay = scrollHost?.querySelector<HTMLElement>(".empty") ?? null;
  if (emptyOverlay) root.dataset.icEmpty = "true";
  else delete root.dataset.icEmpty;
  /* 操作行左右两组共用一个确定宽度：右侧槽（板面工具栏）据此实测自身可用宽度并降级。 */
  root.style.setProperty("--ic-rail-side", Math.max(96, Math.floor(width / 2 - 62)) + "px");
  root.dataset.icFramePathLength = frame.pathLength.toFixed(2);
  root.dataset.icFrameHorizontal = frame.horizontalLength.toFixed(2);
  root.dataset.icCornerReveal = frame.cornerReveal.toFixed(4);

  /* 玻璃：只覆盖实际面板范围（顶边 = 把手中心），水平按揭示长度裁剪、透明度随进度渐显。 */
  const glass = glassEl.value;
  if (glass) {
    glass.style.top = frame.leverY.toFixed(2) + "px";
    glass.style.setProperty("--ic-glass-inset", frame.glassInset.toFixed(2) + "px");
    /* 透明度跟揭示进度走（宽度量不到时也保持正确语义；裁剪量由几何给出）。 */
    glass.style.opacity = reveal.toFixed(4);
  }

  /* 线框：宽度 = 面板宽，高度 = 胶囊水平中线；两条镜像路径按弧长用 dasharray 揭示。 */
  const outline = outlineEl.value;
  if (outline) {
    outline.toggleAttribute("hidden", !frame.visible);
    const svgWidth = Math.max(0, Math.round(frame.width));
    const svgHeight = Math.max(0, Math.round(frame.height));
    outline.setAttribute("width", String(svgWidth));
    outline.setAttribute("height", String(svgHeight));
    outline.setAttribute("viewBox", "0 0 " + svgWidth + " " + svgHeight);
    outline.style.width = svgWidth + "px";
    outline.style.height = svgHeight + "px";
  }
  /* 渐变端点按实测几何逐帧写入（不依赖模板 ref：defs 内的 SVG 节点直接查询最稳）。 */
  const fade = outlineEl.value?.querySelector<SVGLinearGradientElement>("linearGradient") ?? null;
  if (fade) {
    fade.setAttribute("y1", String(frame.fadeY1));
    fade.setAttribute("y2", String(frame.fadeY2));
  }
  const dash = reveal >= 1 ? "none" : frame.strokeDasharray;
  if (leftPathEl.value) {
    leftPathEl.value.setAttribute("d", frame.leftPath);
    leftPathEl.value.style.strokeDasharray = dash;
  }
  if (rightPathEl.value) {
    rightPathEl.value.setAttribute("d", frame.rightPath);
    rightPathEl.value.style.strokeDasharray = dash;
  }

  schedulePublishGeometry();
}

/* ── 开合（只改显示） ───────────────────────────────────────────────────── */
const leverLabel = computed(() => (expandedState.value ? "收起对话" : "展开对话"));
const leverTip = computed(() =>
  expandedState.value
    ? "收起对话；上下拖动调整高度（↑/↓ 每次 " +
      CONVERSATION_KEYBOARD_STEP +
      "px，Home/End 到两端）"
    : "展开对话（点击，或把焦点移入输入框也会展开）",
);

function applyExpandedState(value: boolean, emitEvent: boolean): void {
  if (expandedState.value === value) return;
  expandedState.value = value;
  if (value) openedOnce.value = true;
  if (emitEvent) {
    emit("update:expanded", value);
    if (value) emit("expand");
    else emit("collapse");
  }
  syncTargets();
  startMotion();
  schedulePublishGeometry();
}

function setExpanded(next: boolean): void {
  applyExpandedState(Boolean(next), true);
}

const expand = () => setExpanded(true);
const collapse = () => setExpanded(false);
const toggle = () => setExpanded(!expandedState.value);

watch(
  () => props.expanded,
  (value) => applyExpandedState(Boolean(value), false),
);

watch(
  () => props.height,
  (value) => {
    if (!(value > 0)) return;
    panelHeight.value = effectiveHeight(value);
    if (expandedState.value) {
      syncTargets();
      startMotion();
    }
  },
);

/** 父级收紧/放宽高度预算（例如窄窗口给板面留空间）时，立即把面板夹回合法范围。 */
watch(
  () => [props.minHeight, props.maxHeight] as const,
  () => {
    panelHeight.value = effectiveHeight(panelHeight.value);
    syncTargets();
    startMotion();
  },
);

/* ── 收起态 / 展开态共用的一句真实状态 ─────────────────────────────────── */
const statusText = computed(() =>
  conversationStatusText({
    turnRunning: session.turnRunning,
    queuedCount: session.turnQueue?.queued?.length ?? 0,
    hasDraft: Boolean(String(session.draft ?? "").trim()),
  }),
);

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

/* ── 同一根把手：点击开合 / 展开态拖动调高 / 键盘（Esc 走层级栈） ───────── */
interface LeverDrag {
  pointerId: number | null;
  startY: number;
  startTotal: number;
  moved: boolean;
}

let dragState: LeverDrag | null = null;
let suppressClick = false;

function displayedTotalHeight(): number {
  return Math.max(0, streamShown.value) + chromeHeight.value;
}

function removeDragListeners(): void {
  document.removeEventListener("pointermove", onLeverPointerMove, true);
  document.removeEventListener("pointerup", onLeverPointerUp, true);
  document.removeEventListener("pointercancel", onLeverPointerCancel, true);
}

function onLeverPointerDown(event: PointerEvent): void {
  if (event.button !== undefined && event.button !== 0) return;
  /* 收起状态：只做点击开合，不进入拖动（与预览一致）。 */
  if (!expandedState.value) return;
  dragState = {
    pointerId: typeof event.pointerId === "number" ? event.pointerId : null,
    startY: event.clientY,
    startTotal: displayedTotalHeight(),
    moved: false,
  };
  suppressClick = false;
  emit("resize-start");
  if (dragState.pointerId !== null) {
    try {
      leverEl.value?.setPointerCapture?.(dragState.pointerId);
    } catch {
      // 合成事件没有真实指针：忽略即可，window 上的监听仍然可用
    }
  }
  document.addEventListener("pointermove", onLeverPointerMove, true);
  document.addEventListener("pointerup", onLeverPointerUp, true);
  document.addEventListener("pointercancel", onLeverPointerCancel, true);
  event.preventDefault();
}

function onLeverPointerMove(event: PointerEvent): void {
  if (!dragState) return;
  if (
    dragState.pointerId !== null &&
    typeof event.pointerId === "number" &&
    event.pointerId !== dragState.pointerId
  ) {
    return;
  }
  const delta = dragState.startY - event.clientY;
  if (!dragState.moved && Math.abs(delta) < 4) return;
  dragState.moved = true;
  dragging.value = true;
  const total = effectiveHeight(dragState.startTotal + delta);
  panelHeight.value = total;
  const streamTarget = Math.max(0, total - chromeHeight.value);
  /* 拖动是直接操作：停掉时间轴，消息高度跟手，线框保持全揭示（把手仍在面板顶边）。 */
  stopMotion();
  progress.value = 1;
  streamShown.value = streamTarget;
  drawFrame();
  emit("update:height", total);
  schedulePublishGeometry();
}

function onLeverPointerUp(): void {
  if (!dragState) return;
  const moved = dragState.moved;
  dragState = null;
  dragging.value = false;
  removeDragListeners();
  if (!moved) return;
  suppressClick = true;
  writeStoredHeight(panelHeight.value);
  emit("resize-end", panelHeight.value);
  syncTargets();
  startMotion();
}

function onLeverPointerCancel(): void {
  if (!dragState) return;
  const before = dragState.startTotal;
  dragState = null;
  dragging.value = false;
  removeDragListeners();
  /* Esc / pointercancel 取消这次拖动后，松手产生的 click 不能再开合一次（只取消拖动）。 */
  suppressClick = true;
  panelHeight.value = effectiveHeight(before);
  syncTargets();
  startMotion();
  schedulePublishGeometry();
}

function onLeverClick(event: MouseEvent): void {
  /* 点击与超过阈值的拖动分开：真实指针 click（detail > 0）在拖动结束后不再开合；
     键盘 Enter / Space 产生的 click（detail === 0）照常开合。 */
  if (suppressClick && event.detail > 0) {
    suppressClick = false;
    return;
  }
  suppressClick = false;
  toggle();
}

const handleMin = computed(() => effectiveHeight(props.minHeight));
const handleMax = computed(() => effectiveHeight(props.maxHeight));

/** 键盘：↑/↓ 每次 ±步长，Home/End 到两端（与拖动共用同一套夹取）；Enter/Space 是原生 click 开合。 */
function onLeverKeydown(event: KeyboardEvent): void {
  const keys = ["ArrowUp", "ArrowDown", "Home", "End"];
  if (!keys.includes(event.key)) return;
  event.preventDefault();
  if (!expandedState.value) {
    expand();
  }
  let next = panelHeight.value;
  if (event.key === "ArrowUp") next += CONVERSATION_KEYBOARD_STEP;
  else if (event.key === "ArrowDown") next -= CONVERSATION_KEYBOARD_STEP;
  else if (event.key === "Home") next = handleMin.value;
  else next = handleMax.value;
  next = effectiveHeight(next);
  panelHeight.value = next;
  writeStoredHeight(next);
  emit("update:height", next);
  emit("resize-end", next);
  syncTargets();
  startMotion();
}

/* ── Esc 层级：拖动中先取消拖动（恢复拖动前高度），再收起对话 ───────────── */
let disposeEscape: (() => void) | null = null;

function syncEscapeLayer(openState: boolean): void {
  if (openState && !disposeEscape) {
    disposeEscape = registerEscapeLayer({
      id: "interactive-conversation",
      active: () => expandedState.value,
      onEscape: () => {
        if (dragState) {
          onLeverPointerCancel();
          return true;
        }
        collapse();
        return true;
      },
    });
  } else if (!openState && disposeEscape) {
    disposeEscape();
    disposeEscape = null;
  }
}
watch(expandedState, (value) => syncEscapeLayer(value), { immediate: true });
onBeforeUnmount(() => {
  disposeEscape?.();
  disposeEscape = null;
});

function focusInput(): void {
  if (typeof document === "undefined") return;
  document.getElementById("composer-input")?.focus();
}

/* ── chatFrameState：发布真实可见几何（展开 → 矩形；收起 / 卸载 → null） ── */
const VISIBLE_PART_SELECTORS = ["[data-ic-stream]", "[data-ic-rail]", "[data-ic-entries]", "[data-ic-composer]"] as const;

/** 当前实际可见内容的外接矩形（屏幕坐标）；量不到真实几何时返回 null（不编造）。 */
function measureVisibleFrame(): ChatFrame | null {
  const root = rootEl.value;
  if (!root) return null;
  const parts: DOMRect[] = [];
  if (expandedState.value) {
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

/* ── 布局变化：窗口 / 操作行 / 事项行 / 输入区尺寸变化后重算夹取与目标 ───── */
let observer: ResizeObserver | null = null;

function onGeometryChanged(): void {
  const chrome = measureChromeHeight();
  if (Math.abs(chrome - chromeHeight.value) > 0.5) chromeHeight.value = chrome;
  if (expandedState.value) {
    startMotion();
  } else {
    progress.value = 0;
    streamShown.value = 0;
    drawFrame();
  }
  schedulePublishGeometry();
}

function onWindowResize(): void {
  panelHeight.value = effectiveHeight(panelHeight.value);
  onGeometryChanged();
  syncTargets();
  startMotion();
}

function onReducedMotionChange(): void {
  reduced.value = prefersReducedMotion();
  if (reduced.value) {
    snapToProgress(targetProgress());
    drawFrame();
  }
}

onMounted(() => {
  panelHeight.value = initialHeight();
  writeStoredHeight(panelHeight.value);
  if (expandedState.value) openedOnce.value = true;
  reduced.value = prefersReducedMotion();
  rootMediaQuery?.addEventListener?.("change", onReducedMotionChange);
  window.addEventListener("resize", onWindowResize);
  if (typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(() => onGeometryChanged());
    for (const target of [railEl.value, entriesEl.value, composerEl.value]) {
      if (target) observer.observe(target);
    }
  }
  chromeHeight.value = measureChromeHeight();
  progress.value = expandedState.value ? 1 : 0;
  streamShown.value = streamHeightAt(progress.value);
  streamVisible.value = streamShown.value > 0.5;
  drawFrame();
  publishGeometryNow();
  /**
   * 互动页的历史入口（R5b）：普通对话页负责首次加载；直接进入 / 刷新互动页时
   * history 仍是 idle，只调 reloadHistoryIfStale（idle 下是空操作）会让确认后的
   * 执行记录永远等不到刷新。这里复用同一个既有 loadHistory（同一接口、同一去重与
   * 话题归属保护，不轮询、不伪造记录、不跳页），已加载过则继续走既有 stale 刷新。
   */
  if (session.history.status === "idle") void session.loadHistory();
  else void session.reloadHistoryIfStale();
});

// 展开 / 收起 / 高度变化后 DOM 已更新，下一帧按新几何重新发布
watch([expandedState, panelHeight, openedOnce], () => schedulePublishGeometry(), { flush: "post" });

onBeforeUnmount(() => {
  stopMotion();
  removeDragListeners();
  if (publishRaf !== null) {
    window.cancelAnimationFrame(publishRaf);
    publishRaf = null;
  }
  if (typeof window !== "undefined") window.removeEventListener("resize", onWindowResize);
  rootMediaQuery?.removeEventListener?.("change", onReducedMotionChange);
  observer?.disconnect();
  observer = null;
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
  progress,
  /** 只读给定向测试：当前可见内容的外接矩形（与发布值同一份算法） */
  measureVisibleFrame,
  /** 只读给定向测试 / E2E：最后一帧的线框几何（与画面同一份算法） */
  frameGeometry: () => lastFrame,
});

/** 发送绑定投影：只保留会话层认识的字段；没有绑定就是普通聊天（不带板面身份）。 */
const sendBinding = computed(() => conversationBindingForSend(props.binding));

function onComposerPointerDown(): void {
  /* 点击输入区（附件 / 文本 / 发送都在胶囊内）同样自动展开；只改显示。 */
  expand();
}
</script>

<template>
  <section
    ref="rootEl"
    class="ic"
    data-ic="conversation"
    :data-ic-expanded="expandedState ? 'true' : 'false'"
    :data-ic-dragging="dragging ? 'true' : undefined"
    :data-ic-reduced="reduced ? 'true' : undefined"
    role="region"
    :aria-label="title + '（底部互动对话）'"
  >
    <!-- 玻璃材质：只覆盖真实面板范围，随揭示渐显；板面从后面透出（每帧由 JS 更新裁剪/透明度）。 -->
    <div ref="glassEl" class="ic-glass" data-ic-glass aria-hidden="true"></div>

    <!-- 顶边线框：把手 + 两条镜像开放路径；按弧长揭示（SVG 尺寸/路径每帧更新）。 -->
    <svg ref="outlineEl" class="ic-outline" data-ic-frame aria-hidden="true" hidden>
      <defs>
        <linearGradient :id="fadeId" x1="0" y1="0" x2="0" y2="1" gradientUnits="userSpaceOnUse">
          <stop offset="0" style="stop-color: var(--border-strong)" />
          <stop offset="1" style="stop-color: var(--border-strong); stop-opacity: 0" />
        </linearGradient>
      </defs>
      <path
        ref="leftPathEl"
        data-ic-frame-path="left"
        :style="{ stroke: 'url(#' + fadeId + ')' }"
      />
      <path
        ref="rightPathEl"
        data-ic-frame-path="right"
        :style="{ stroke: 'url(#' + fadeId + ')' }"
      />
    </svg>

    <!-- 会话流：第一次展开才挂载，之后只把高度收为 0 —— 收起不等于清空阅读位置与已渲染内容。 -->
    <div
      v-if="openedOnce"
      ref="streamWrapEl"
      class="ic-stream-wrap"
      data-ic-stream-wrap
      :style="{ visibility: streamVisible ? 'visible' : 'hidden' }"
      :aria-hidden="expandedState ? 'false' : 'true'"
      :inert="expandedState ? undefined : true"
    >
      <div ref="streamEl" class="ic-stream" data-ic-stream :id="panelId">
        <MessageStream empty-variant="interactive" />
      </div>
    </div>

    <!-- 操作行：左＝模式切换 + 真实状态；中＝常驻把手（绝对居中，不随左右内容宽度偏移）；右＝板面工具。 -->
    <div ref="railEl" class="ic-rail" data-ic-rail>
      <div class="ic-rail-left">
        <slot name="mode-switch" />
        <span v-if="statusText" class="ic-status mono" data-ic-status role="status">{{ statusText }}</span>
      </div>
      <button
        ref="leverEl"
        class="ic-lever"
        data-ic-toggle
        data-ic-resize
        type="button"
        :aria-expanded="expandedState ? 'true' : 'false'"
        :aria-controls="panelId"
        :aria-label="leverLabel"
        v-qio-tip="leverTip"
        @click="onLeverClick"
        @pointerdown="onLeverPointerDown"
        @keydown="onLeverKeydown"
      >
        <span class="ic-lever-bar" aria-hidden="true"></span>
      </button>
      <div class="ic-rail-right">
        <slot name="toolbar" />
      </div>
    </div>

    <div class="ic-dock">
      <!-- 五入口（附件 / 失败恢复 / 排队 / 历史继续 / 话题建议 / 审批）：始终在输入区正上方 -->
      <div ref="entriesEl" class="ic-entries" data-ic-entries>
        <ComposerEntries context="interactive" :extra-entries="props.extraEntries">
          <template #extra-panel="scope">
            <slot name="extra-panel" v-bind="scope" />
          </template>
        </ComposerEntries>
      </div>

      <!-- 输入区：始终挂载；收起只改变可见区域，不清草稿 / 附件 / 排队，也不停止回复。
           点击 / 聚焦输入区时自动展开消息面板。 -->
      <div
        ref="composerEl"
        class="ic-composer"
        data-ic-composer
        @focusin="expand"
        @pointerdown="onComposerPointerDown"
      >
        <div class="ic-composer-input">
          <Composer :chat-binding="sendBinding">
            <template #leading>
              <slot name="leading" />
            </template>
          </Composer>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ic {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  width: 100%;
  font-family: var(--sans);
  color: var(--text-primary);
}
/*
  玻璃材质集中在聊天面板（只此一处）：
  - 顶边从把手中心开始，按真实面板范围裁到顶角 14px 圆角，底部保持开口；
  - clip-path 与 opacity 每帧由 JS 写入（水平随线框揭示渐开、整体渐显）；
  - 板面内容从玻璃后面透出，明暗主题都用 bg-base 混合保证消息可读。
*/
.ic-glass {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  top: var(--ic-glass-top, 17px);
  z-index: 0;
  pointer-events: none;
  border-radius: var(--qio-ui-radius) var(--qio-ui-radius) 0 0;
  background: color-mix(in srgb, var(--bg-base) 54%, transparent);
  -webkit-backdrop-filter: blur(16px) saturate(1.06);
  backdrop-filter: blur(16px) saturate(1.06);
  clip-path: inset(
    0 var(--ic-glass-inset, 50%) 0 var(--ic-glass-inset, 50%) round var(--qio-ui-radius)
      var(--qio-ui-radius) 0 0
  );
  opacity: var(--ic-glass-opacity, 0);
}
/* 浏览器不支持 backdrop-filter：退回更实的半透明底（不假装有模糊）。 */
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  .ic-glass {
    background: color-mix(in srgb, var(--bg-base) 90%, transparent);
  }
}
/* 顶边线框：两条镜像开放路径，stroke 颜色由渐变（顶边实、胶囊中线透明）给出。 */
.ic-outline {
  position: absolute;
  top: 0;
  left: 0;
  z-index: 2;
  overflow: visible;
  pointer-events: none;
}
.ic-outline path {
  fill: none;
  stroke-width: 1.2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
/* 消息区：高度按帧动画（0 = 收起），内容保留在 DOM 里（滚动位置 / 已渲染内容不丢）。 */
.ic-stream-wrap {
  position: relative;
  z-index: 1;
  flex: none;
  height: var(--ic-stream-h, 0px);
  overflow: hidden;
  min-width: 0;
}
.ic-stream {
  position: relative;
  display: flex;
  width: 100%;
  max-width: 860px;
  height: 100%;
  margin: 0 auto;
  overflow: hidden;
  background: transparent;
  /* 消息 / 滚动条只在「可见顶边线」以下绘制：面板顶到把手中心 17px 属于边框区域，
     不让滚动内容或指示条从这里溢到边框外（clip 值由 drawFrame 按实测 leverY 写入）。 */
  clip-path: inset(var(--ic-frame-top, 17px) 0 0 0);
}
.ic-stream :deep(.stream) {
  flex: 1;
  min-height: 0;
  height: 100%;
  /* 顶边（把手 + 细线）占掉 30px：第一条消息不被线框压住。 */
  padding-top: 30px;
  background: transparent;
  /* 开合时消息区高度随面板变化：关掉浏览器滚动锚定，消息内容才能与把手同速升降；
     历史插入/阅读位置的锚定由 MessageStream 自己的逻辑负责（见 readingAnchor）。 */
  overflow-anchor: none;
}
/* 空态：与把手同速升降（固定在面板顶边下方 40px），底边被操作行裁切；
   把手永远在消息上方且不遮挡。虚化由 .ic-stream 的 opacity/filter 统一驱动。 */
.ic-stream :deep(.empty) {
  inset: 0 0 auto 0;
  justify-content: flex-start;
  padding: var(--ic-empty-top, 40px) var(--sp-3) 0;
}
/* 空态没有可滚动内容：不显示 QIO 滚动指示条（贴底 settle 早期会把空态算成可滚动）。 */
.ic[data-ic-empty="true"] :deep(.qio-scroll-rail) {
  display: none !important;
}
.ic-stream :deep(.empty .greet) {
  max-width: 100%;
  font-size: clamp(20px, 5vw, 34px);
  overflow-wrap: anywhere;
}
/*
  操作行：透明底；左右两组等距内边距，把手绝对居中（左右宽度变化不影响居中）。
  刻意不给 .ic-rail 自身 position：把手是面板顶边的控件（展开后消息区把操作行往下推，
  把手仍留在面板顶部），所以它的绝对定位包含块必须是 .ic（见 .ic-lever），而不是这一行。
*/
.ic-rail {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  min-height: 36px;
  /* 按钮组再往里收：外缘落在输入胶囊两端半圆（radius = 胶囊高/2 = 27px）的
     水平范围内，而不是顶到胶囊最外侧的直边端点（用户 2026-11-23 反馈）。 */
  padding: 1px var(--sp-6);
  background: transparent;
}
.ic-rail-left,
.ic-rail-right {
  position: relative;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
  /* 给中间的把手留出空间：左右内容再长也不会压到手柄。 */
  max-width: var(--ic-rail-side, calc(50% - 62px));
}
.ic-rail-right {
  /* 确定宽度：板面工具栏（rail）量到的是真实可用宽度，而不是自身内容宽度。 */
  flex: 0 0 auto;
  width: var(--ic-rail-side, calc(50% - 62px));
}
.ic-rail-left {
  justify-content: flex-start;
  gap: var(--sp-2);
}
.ic-rail-right {
  justify-content: flex-end;
}
.ic-status {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
/* 常驻把手：扁长外形（5px 长条）+ 32px 命中区；收起时点击开合，展开时点击收起、上下拖动调高。 */
.ic-lever {
  position: absolute;
  top: 1px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 5;
  display: grid;
  place-items: center;
  width: clamp(52px, 15%, 112px);
  height: 32px;
  padding: 0 4px;
  border: 0;
  border-radius: 12px;
  background: transparent;
  cursor: pointer;
  touch-action: none;
}
.ic-lever-bar {
  display: block;
  width: 100%;
  height: 5px;
  border-radius: 4px;
  background: var(--border-strong);
  transition: background var(--dur-fast) var(--ease);
}
.ic-lever:hover .ic-lever-bar,
.ic-lever:focus-visible .ic-lever-bar {
  background: color-mix(in srgb, var(--accent) 58%, var(--border-strong));
}
.ic[data-ic-expanded="true"] .ic-lever {
  cursor: ns-resize;
}
.ic[data-ic-dragging="true"] .ic-lever-bar {
  background: var(--accent);
}
.ic-lever:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
/* 输入区：始终挂载；胶囊宽度跟随面板（自己的轮廓收住整组界面）。 */
.ic-dock {
  position: relative;
  z-index: 1;
  flex: none;
  min-width: 0;
  padding: 0 var(--sp-3) var(--sp-2);
}
.ic-entries {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
  min-width: 0;
}
.ic-composer {
  width: 100%;
  max-width: none;
  margin: 0;
  min-width: 0;
}
.ic-composer :deep(.composer) {
  /* 旧布局把输入列钳在 860 内；现在线框与操作行同宽，胶囊也铺满这条 860 列。 */
  max-width: none;
}
.ic-composer-input {
  width: 100%;
  min-width: 0;
}
/* 减少动画：直接落最终几何，只保留项目既有短淡入淡出。 */
.ic[data-ic-reduced="true"] .ic-glass,
.ic[data-ic-reduced="true"] .ic-outline {
  transition: opacity var(--dur-fast) var(--ease);
}
</style>