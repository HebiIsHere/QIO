<!--
  顶部「任务」浮层 + 靠近板面虚线预览的单项审批浮条（子智能体 D）。

  契约 §9.6：
  - 任务浮层与批量列表、聊天共用右侧同一片区域，所以位置不能写死：
    这里按**实际可用区域**在几个候选位置里挑一个与聊天面板 / 批量列表 / 底部工具栏
    相交面积最小的（同级里再按「右上 → 聊天左边 → 聊天上方」的优先级）；
  - 单项批准 / 拒绝入口靠近对应预览：板面本身由 BoardPreviewLayer 画虚线预览，
    这里在预览附近放一条审批浮条（IntentPreviewCard docked），同样要避开聊天与批量面板；
  - 键盘与指针边界：浮层内空格不触发板面框选、滚轮优先滚自己、Escape 先收浮层；
  - 不把演示状态说成真实执行成功：正式路径的批准会真实启动执行（契约 §14.4），
    演示推进（/demo/advance）只走可控状态机、不得驱动真实任务，两类状态在界面上必须分清。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  executionActions,
  executionProgress,
  executionText,
  locatePreview,
  previewBounds,
  statusText,
} from "../../interactive/approval";
import type { Intent } from "../../interactive/types";
import { chatObstacleFrame } from "../../interactive/chatFrameState";
import { OVERLAY_EDGE, planDockStrip, type BoxRect } from "../../interactive/overlayLayout";
import IntentPreviewCard from "./IntentPreviewCard.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const store = useInteractiveStore();

const busy = ref(false);
const notice = ref<string | null>(null);
const error = ref<string | null>(null);
/** 只重试读取任务清单（F11）：不提交、不审批、不改任何板面事实 */
const refreshing = ref(false);
async function retryIntents(): Promise<void> {
  if (refreshing.value) return;
  refreshing.value = true;
  try {
    await store.loadIntents();
  } finally {
    refreshing.value = false;
  }
}
/** 当前聚焦查看的任务（点「在板面上定位」时设置；用来在预览附近放审批浮条） */
const focusedId = ref<string | null>(null);
/** 每行任务默认只显示「状态 + 操作」，点「详情」才展开进度与预览位置（浮层保持小巧，少挡板面） */
const expanded = ref<Record<string, boolean>>({});

const popEl = ref<HTMLElement | null>(null);
/** 打开前焦点在哪：关闭后还回去 */
let lastFocused: HTMLElement | null = null;

/** 浮层与审批浮条之间的默认间距，和几何层保持同一档 */
const GAP = 12;
/** 浮层宽度上限（与样式里的 380px 一致，用于候选位置的相交估算） */
const POP_WIDTH = 380;

/**
 * 需要在这里管的任务：已批准（含等待前项）、排队 / 执行中、已暂停，以及任何已经有真实执行记录的。
 * 执行中与已暂停的排序沿用 store.activeIntents，其余按原顺序附在后面。
 */
const tasks = computed(() => {
  const seen = new Set<string>();
  const list: Intent[] = [];
  for (const intent of store.intents) {
    const managed =
      ["running", "paused", "waiting_dependency"].includes(intent.status) ||
      Boolean(intent.execution) ||
      Boolean(store.latestExecution(intent.id));
    if (!managed || seen.has(intent.id)) continue;
    seen.add(intent.id);
    list.push(intent);
  }
  return list;
});

/** 这个任务最近一次真实执行（没有记录就是 null：界面不假装它在运行） */
function executionOf(intent: Intent) {
  return store.latestExecution(intent.id);
}
function executionStatusOf(intent: Intent): string {
  return executionOf(intent)?.status ?? store.executionStatus[intent.id] ?? "none";
}
function execText(intent: Intent) {
  return executionText(executionOf(intent), intent);
}
function execActions(intent: Intent) {
  return executionActions(executionOf(intent), intent);
}
function progressText(intent: Intent): string {
  const execution = executionOf(intent);
  if (execution) return executionProgress(execution).text;
  return intent.progress?.text ?? "还没有执行记录";
}

const controlBusy = ref<string | null>(null);
const controlNote = ref<string | null>(null);

/** 暂停 / 继续 / 取消：同一时间只处理一项，结果以服务端返回为准。 */
async function control(intent: Intent, action: "pause" | "resume" | "cancel"): Promise<void> {
  if (controlBusy.value) return;
  controlBusy.value = intent.id;
  controlNote.value = null;
  try {
    const result =
      action === "pause"
        ? await store.pauseExecution(intent.id)
        : action === "resume"
          ? await store.resumeExecution(intent.id)
          : await store.cancelExecution(intent.id);
    const verb = action === "pause" ? "暂停" : action === "resume" ? "继续" : "取消";
    controlNote.value = result.ok
      ? "已" + verb + "：" + (result.detail ?? "以服务端返回的执行记录为准。")
      : "没有" + verb + "：" + (result.reason ?? result.detail ?? "服务端未说明原因");
  } finally {
    controlBusy.value = null;
  }
}

/** 浮动审批条上的执行控制：按 id 找到任务再走同一条入口 */
function controlById(intentId: string, action: "pause" | "resume" | "cancel"): void {
  const intent = store.intentById(intentId);
  if (intent) void control(intent, action);
}

/** 浮动审批条上的批准 / 拒绝：与列表里走同一个 store 入口（重复点击不会重复提交） */
async function approveById(intentId: string, confirmDependency: boolean): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.approve(intentId, confirmDependency);
    notice.value = result.ok
      ? result.detail ?? "已批准：任务已经交给真实执行，进度以执行记录为准。"
      : "没有批准：" + (result.detail ?? result.reason ?? "服务端未说明原因");
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

async function rejectById(intentId: string): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.reject(intentId);
    notice.value = result.ok
      ? "已拒绝：预览消失，板面原内容保留。"
      : "没有拒绝：" + (result.detail ?? result.reason ?? "服务端未说明原因");
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

/** 重新查一次真实执行记录（不猜、不补进度） */
async function refreshExecutions(): Promise<void> {
  await store.loadExecutions();
  await store.loadIntents();
}

// 打开浮层时查一次（开合仍由用户决定；这里只读事实，不自动打开面板）
watch(
  () => store.tasksOpen,
  (open) => {
    if (open) void refreshExecutions();
  },
);
/**
 * 浮条展示的意图：优先用用户刚聚焦的那一项，否则用「执行中」的任务
 * （执行中的任务在板面上有虚线预览，浮条能贴着它；暂停的任务按契约不画预览）。
 */
/** 浮条 / 浮窗用的意图：把服务端最近一次执行记录带进 props（intent.execution 可能为空） */
const focusedWithExecution = computed<Intent | null>(() => {
  const intent = focused.value;
  if (!intent) return null;
  const latest = store.latestExecution(intent.id);
  return latest ? { ...intent, execution: latest } : intent;
});

/**
 * 还在等待审批的条目（任务书 §5.4）：任务浮层本身只管已批准 / 执行中的任务，
 * 所以浮条单独把「待审批」也作为来源之一 —— 聊天收起时审批入口仍然可达，
 * 而且 1~3 项（不够批量列表门槛）也有批准 / 拒绝 / 定位的入口。
 */
const pendingApprovals = computed(() =>
  store.understandingIntents.filter((item) =>
    ["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status),
  ),
);

const focused = computed<Intent | null>(() => {
  const byFocus = focusedId.value ? store.intentById(focusedId.value) : undefined;
  if (byFocus && ["running", "paused"].includes(byFocus.status)) return byFocus;
  return (
    tasks.value.find((item) => item.status === "running") ??
    tasks.value[0] ??
    pendingApprovals.value[0] ??
    null
  );
});

interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

function boxOf(selector: string): Box | null {
  if (typeof document === "undefined") return null;
  const node = document.querySelector(selector);
  if (!node) return null;
  const rect = node.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };
}

function overlap(a: Box, b: Box): number {
  const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  if (width <= 0 || height <= 0) return 0;
  return width * height;
}

/**
 * 对话窗口的避让事实以共享桥为准（IRL §7.1）：它已经处理了「收起 = 这块空间可用」，
 * 也不依赖 ChatDock 的 DOM 钩子（那是 A 的组件，本轮在精简结构）；桥还没发布时
 * 退回既有 DOM 查询，两者都量不到就不避让 —— 绝不凭空造一个矩形。
 */
function chatBox(): Box | null {
  const frame = chatObstacleFrame();
  if (frame) {
    return { left: frame.x, top: frame.y, right: frame.x + frame.w, bottom: frame.y + frame.h };
  }
  return boxOf('[data-im="chat-panel"]');
}

/** 现在必须避开的浮层：对话窗口、批量列表整体、底部工具栏 */
function avoidBoxes(): Box[] {
  const boxes: Box[] = [];
  const chat = chatBox();
  if (chat) boxes.push(chat);
  const batch = boxOf('[data-im="batch-area"]');
  if (batch) boxes.push(batch);
  const toolbar = boxOf('[data-im="board-toolbar"]');
  if (toolbar) boxes.push(toolbar);
  return boxes;
}

/** 一个候选位置与所有必须避开的浮层相交的总面积 */
function penalty(box: Box, avoid: Box[]): number {
  return avoid.reduce((sum, item) => sum + overlap(box, item), 0);
}

const popStyle = ref<CSSProperties>({});
/**
 * 板面上那处虚线预览的屏幕位置（fixed 定位用）。
 * 初值就是「退回板面下沿居中」：首次量到之前也不会先按文档流排一下再跳。
 */
const dockStyle = ref<CSSProperties>({
  position: "fixed",
  left: "50%",
  bottom: "var(--sp-6)",
  transform: "translateX(-50%)",
  top: "auto",
});

/**
 * 量一次：给任务浮层与审批浮条各挑一个相交最小的位置。
 * 只看别人的矩形（聊天面板 / 批量列表 / 工具栏 / 预览），自己的尺寸从不参与计算，
 * 所以不会出现「自己变高 → 位置变 → 自己变高」的循环。
 */
function measure(): void {
  if (typeof window === "undefined" || typeof document === "undefined") return;
  const viewWidth = window.innerWidth || 1440;
  const viewHeight = window.innerHeight || 900;
  const avoid = avoidBoxes();
  const own = popEl.value?.getBoundingClientRect();
  const ownHeight = own && own.height > 0 ? own.height : 320;
  const width = Math.min(POP_WIDTH, viewWidth - 24);

  const candidates: CSSProperties[] = [];
  // ① 右上（默认位置）
  candidates.push({ right: 16 + "px", top: 64 + "px", maxHeight: viewHeight - 64 - 96 + "px" });
  // ② 聊天左边（并排时的自然位置）
  const chat = boxOf('[data-im="chat-panel"]');
  if (chat) {
    const right = Math.round(viewWidth - chat.left + GAP);
    if (right + width + 8 <= viewWidth) {
      candidates.push({ right: right + "px", top: 64 + "px", maxHeight: viewHeight - 64 - 96 + "px" });
    }
    // ③ 聊天上方：宽度让不开时，退到聊天上面那一条
    const room = Math.round(chat.top - 64 - GAP);
    if (room >= 180) {
      candidates.push({ right: 16 + "px", top: 64 + "px", maxHeight: room + "px" });
    }
  }
  // ④ 左下（右上全被占时，退到工具栏左上方）
  candidates.push({ right: Math.round(width + 16) + "px", top: 64 + "px", maxHeight: viewHeight - 64 - 96 + "px" });

  let best = candidates[0];
  let bestPenalty = Number.POSITIVE_INFINITY;
  for (const candidate of candidates) {
    const right = Number.parseFloat(String(candidate.right)) || 0;
    const top = Number.parseFloat(String(candidate.top)) || 0;
    const maxHeight = Number.parseFloat(String(candidate.maxHeight)) || ownHeight;
    const box: Box = {
      left: viewWidth - right - width,
      top,
      right: viewWidth - right,
      bottom: top + Math.min(ownHeight, maxHeight),
    };
    const score = penalty(box, avoid);
    if (score < bestPenalty) {
      bestPenalty = score;
      best = candidate;
    }
    if (score === 0) break;
  }
  popStyle.value = best;
  /**
   * 浮条还要避开**自己这个任务浮层**：多个面板不许互相遮挡。
   * 只单向避让（浮条避开浮层），浮层的候选位置不看浮条 —— 两个浮层互相避让会来回抖。
   */
  const ownBox =
    own && own.width > 0 && own.height > 0
      ? { left: own.left, top: own.top, right: own.right, bottom: own.bottom }
      : null;
  measureDock(ownBox ? [...avoid, ownBox] : avoid, viewWidth, viewHeight);
}

/**
 * 板面虚线预览的屏幕矩形：按意图 id 找 BoardPreviewLayer 画出来的那处预览。
 *
 * 同一意图底下有外层容器与具体卡片 / 组 / 连线多个元素，取**最小的那个可见矩形**，
 * 它才是用户在板面上看到的那处预览；没有可见元素时返回 null（浮条退回板面下沿）。
 */
function previewRect(intentId: string): DOMRect | null {
  if (typeof document === "undefined") return null;
  const nodes = document.querySelectorAll('[data-im="preview"][data-intent-id="' + intentId + '"]');
  let best: DOMRect | null = null;
  for (const node of nodes) {
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    if (!best || rect.width * rect.height < best.width * best.height) best = rect;
  }
  return best;
}

/**
 * 审批浮条：贴着预览放，但绝不压住聊天 / 批量列表 / 工具栏，也绝不出视口。
 *
 * 位置统一交给几何层（overlayLayout.planDockStrip）：四方向贴靠、完全离屏的预览
 * 不作为锚点、视口约束优先、其次才避让浮层 —— 这几条是与板面定位共用的同一套事实，
 * 组件里不再写第二份特例（上一版自己算候选，既漏视口夹取，也和几何层的口径不一致）。
 */
function measureDock(avoid: Box[], viewWidth: number, viewHeight: number): void {
  const intent = focused.value;
  // 审批浮条的根元素（IntentPreviewCard 的 data-im="intent"）：尺寸可能被用户调过，
  // 所以宽度也从 DOM 量，量不到才退回默认值（jsdom 没有布局）。
  const card = document.querySelector('[data-im="intent"]') as HTMLElement | null;
  const cardRect = card?.getBoundingClientRect();
  const stripWidth =
    cardRect && cardRect.width > 0 ? Math.round(cardRect.width) : Math.min(360, viewWidth - 40);
  const cardHeight = cardRect && cardRect.height > 0 ? Math.round(cardRect.height) : 158;
  const rect = intent ? previewRect(intent.id) : null;
  const anchor: BoxRect | null = rect
    ? { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }
    : null;
  const toolbar = boxOf('[data-im="board-toolbar"]');
  const plan = planDockStrip({
    viewport: { width: viewWidth, height: viewHeight },
    strip: { width: stripWidth, height: cardHeight },
    anchor,
    avoid: avoid.map((box) => ({ left: box.left, top: box.top, right: box.right, bottom: box.bottom })),
    bottomLimit: toolbar ? toolbar.top : viewHeight - 96,
    topLimit: 64,
    edge: OVERLAY_EDGE,
    gap: 10,
  });
  dockStyle.value = fixedAt(plan.left, plan.top);
}

function fixedAt(left: number, top: number): CSSProperties {
  return { position: "fixed", left: Math.round(left) + "px", top: Math.round(top) + "px", transform: "none", bottom: "auto" };
}

function onViewportChange(): void {
  if (measureTimer) return;
  measureTimer = window.setTimeout(() => {
    measureTimer = null;
    measure();
  }, 0);
}
let measureTimer: number | null = null;

/** 聚焦项 / 展开状态变化后再量一次（DOM 更新之后） */
watch([focused, () => store.tasksOpen, () => store.chatOpen, () => store.batchOpen], () => onViewportChange(), {
  flush: "post",
});

function onKeydown(event: KeyboardEvent): void {
  // 浮层内的按键不再外泄给板面（空格不框选、删除不删卡片）；Escape 先收浮层
  if (event.key !== "Escape") return;
  if (store.tasksOpen) {
    store.tasksOpen = false;
    event.preventDefault();
  }
}

onMounted(async () => {
  window.addEventListener("resize", onViewportChange);
  // 板面滚动（平移）与缩放都由视口承载：滚动与滚轮都重新量一次（节流到下一帧）
  window.addEventListener("scroll", onViewportChange, true);
  window.addEventListener("wheel", onViewportChange, { passive: true, capture: true });
  measureTimer = window.setTimeout(() => {
    measureTimer = null;
    measure();
  }, 60);
  await nextTick();
  const active = document.activeElement as HTMLElement | null;
  lastFocused = active && active !== document.body ? active : null;
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onViewportChange);
  window.removeEventListener("scroll", onViewportChange, true);
  window.removeEventListener("wheel", onViewportChange, true);
  if (measureTimer) window.clearTimeout(measureTimer);
  measureTimer = null;
  recallFocus();
});

/** 收起浮层后把焦点还给打开它的入口（键盘用户不会掉到页面开头） */
function recallFocus(): void {
  const entry = document.querySelector<HTMLElement>('[data-im="tasks-entry"]');
  const target = lastFocused && document.contains(lastFocused) ? lastFocused : entry;
  lastFocused = null;
  target?.focus?.({ preventScroll: true });
}

function label(intent: Intent): string {
  return statusText(intent).label;
}

function toggleDetail(intentId: string): void {
  expanded.value = { ...expanded.value, [intentId]: !expanded.value[intentId] };
}

/**
 * 定位（19b）：**真实通知板面**把预览带进视口，而不是只改局部聚焦项。
 *
 * 反例：原来只设 focusedId + 重新测量浮条，板面纹丝不动，
 * 用户点「在板面上定位」之后仍然看不到预览在哪里。
 * 这里与单项审批浮条共用同一条通道（approval.locatePreview → window 事件），
 * 由 BoardCanvas 真实平移板面；没有可展示位置的按预览如实不派发，不伪造 bounds。
 */
function locate(intentId: string): void {
  focusedId.value = intentId;
  const intent = store.intentById(intentId);
  const bounds = intent ? previewBounds(intent.preview) : null;
  if (bounds) locatePreview(intentId, bounds);
  // 板面滚动过去之后再量一次浮条位置，避免浮条停在旧坐标
  window.setTimeout(measure, 320);
}

async function resume(intent: Intent): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    // 已有真实执行记录：走「继续执行」（服务端会校验材料与已完成步骤，不重复已完成操作）
    if (intent.status === "paused" && executionOf(intent)) {
      const result = await store.resumeExecution(intent.id);
      notice.value = result.ok
        ? result.detail ?? "已继续：会校验材料与已完成步骤，不重复已经完成的操作。"
        : "没有继续：" + (result.reason ?? result.detail ?? "服务端未说明原因");
      return;
    }
    const result = await store.approve(intent.id, true);
    notice.value = result.ok
      ? result.detail ?? "已确认按当前材料继续。"
      : "没有继续：" + (result.detail ?? result.reason ?? "服务端未说明原因");
  } catch (err) {
    error.value = (err as Error).message;
  } finally {
    busy.value = false;
  }
}

function boundsText(intent: Intent): string {
  const bounds = previewBounds(intent.preview);
  if (!bounds) return "预览没有可展示的位置";
  return "预览位置 (" + bounds.x + ", " + bounds.y + ")，大小 " + bounds.w + "×" + bounds.h;
}
</script>
<template>
  <section
    v-if="store.tasksOpen"
    ref="popEl"
    class="tasks-pop"
    v-qio-scrollbar
    data-qio-fit
    data-im="tasks-popover"
    role="dialog"
    aria-label="任务"
    tabindex="-1"
    :style="popStyle"
    @keydown.stop="onKeydown"
    @keyup.stop
    @wheel.stop
  >
    <header class="head">
      <h2 class="title">任务</h2>
      <p class="counts mono">
        执行中 {{ tasks.length }} 项 · 已结束 {{ store.finishedIntents.length }} 项
      </p>
      <button class="close" type="button" data-im="tasks-refresh" @click="refreshExecutions">
        刷新
      </button>
      <button class="close" type="button" data-im="tasks-close" @click="store.tasksOpen = false">
        收起
      </button>
    </header>

    <p class="hint" role="note">
      待审批的条目走批量审批入口；这里列出已批准（含等待前项）、排队、执行中、已暂停与已结束的任务。
      状态和进度都取服务端的真实执行记录：排队、执行、暂停与结束各自显示，不会把「已批准」一直显示成运行中。
    </p>
    <p v-if="tasks.some((item) => item.demo)" class="hint" data-im="tasks-demo-note">
      演示任务不是真实执行：推进演示不会写回任何真实任务。
    </p>
    <p v-if="notice" class="notice" role="status" data-im="tasks-notice">{{ notice }}</p>
    <p v-if="controlNote" class="notice" role="status" data-im="tasks-control-note">{{ controlNote }}</p>
    <p v-if="error" class="error" role="alert">操作失败：{{ error }}</p>
    <p v-if="store.executionError" class="error" role="status" data-im="tasks-execution-error">
      执行记录读取失败：{{ store.executionError }}
    </p>

    <!--
      F11：读取失败**不是**「任务消失」。下面列出的仍然是最后一次成功读到的任务
      （不是当前权威事实），并给一个只触发读取的重试入口。
    -->
    <p v-if="store.intentsLoadState === 'error'" class="notice warn" role="status" data-im="tasks-refresh-failed">
      任务清单刷新失败：{{ store.intentsLoadError || "原因未知" }}。下面是最后一次成功读到的任务，
      不代表当前状态；依赖最新事实的审批会先重新核验，核验不成会暂停。
      <button
        class="btn ghost"
        type="button"
        data-im="tasks-refresh-retry"
        :disabled="refreshing"
        @click="retryIntents"
      >
        {{ refreshing ? "重新读取中…" : "重试刷新" }}
      </button>
    </p>

    <p v-if="!tasks.length" class="hint">{{ store.intentsLoadState === "error" ? "清单读取失败：这里的空白不是「没有任务」，请先重试刷新。" : "现在没有需要管理的任务。" }}</p>
    <ul class="rows">
      <li
        v-for="intent in tasks"
        :key="intent.id"
        class="row"
        data-im="task-item"
        :data-intent-id="intent.id"
        :data-intent-status="intent.status"
      >
        <span class="task-title">
          <span v-if="intent.demo" class="badge">演示</span>
          {{ intent.title }}
        </span>
        <span class="task-status">{{ label(intent) }}</span>
        <span
          class="task-exec"
          data-im="task-execution"
          :data-intent-id="intent.id"
          :data-execution-status="executionStatusOf(intent)"
          :class="'tone-' + execText(intent).tone"
        >
          <span class="task-exec-label">{{ execText(intent).label }}</span>
          <span class="task-exec-detail">{{ execText(intent).detail }}</span>
        </span>
        <span class="task-actions">
          <button
            class="btn ghost"
            type="button"
            data-im="task-locate"
            :data-intent-id="intent.id"
            @click="locate(intent.id)"
          >
            在板面上定位
          </button>
          <button
            class="btn ghost"
            type="button"
            data-im="task-detail"
            :data-intent-id="intent.id"
            :aria-expanded="Boolean(expanded[intent.id])"
            @click="toggleDetail(intent.id)"
          >
            {{ expanded[intent.id] ? "收起详情" : "详情" }}
          </button>
          <button
            v-if="execActions(intent).canPause"
            class="btn"
            type="button"
            data-im="task-pause"
            :data-intent-id="intent.id"
            :disabled="controlBusy === intent.id"
            @click="control(intent, 'pause')"
          >
            {{ controlBusy === intent.id ? "处理中…" : "暂停" }}
          </button>
          <button
            v-if="intent.status === 'paused'"
            class="btn"
            type="button"
            data-im="task-resume"
            :data-intent-id="intent.id"
            :disabled="busy || controlBusy === intent.id"
            @click="resume(intent)"
          >
            继续（按当前材料）
          </button>
          <button
            v-else-if="execActions(intent).canResume"
            class="btn"
            type="button"
            data-im="task-resume-execution"
            :data-intent-id="intent.id"
            :disabled="controlBusy === intent.id"
            @click="control(intent, 'resume')"
          >
            继续执行
          </button>
          <button
            v-if="execActions(intent).canCancel"
            class="btn ghost"
            type="button"
            data-im="task-cancel"
            :data-intent-id="intent.id"
            :disabled="controlBusy === intent.id"
            @click="control(intent, 'cancel')"
          >
            取消任务
          </button>
        </span>
        <span v-if="expanded[intent.id]" class="task-detail">
          <span class="task-progress mono" data-im="task-progress">{{ progressText(intent) }}</span>
          <span class="task-bounds">{{ boundsText(intent) }}</span>
          <span class="task-control-hint" data-im="task-control-hint">{{ execActions(intent).reason }}</span>
        </span>
      </li>
    </ul>
  </section>

  <!-- 单项审批浮条：贴着板面上那处虚线预览（拿不到坐标就退回板面下沿居中） -->
  <IntentPreviewCard
    v-if="store.tasksOpen && focusedWithExecution"
    :intent="focusedWithExecution"
    :all-intents="store.intents"
    :style="dockStyle"
    docked
    active
    @approve="approveById"
    @reject="rejectById"
    @pause="(id: string) => controlById(id, 'pause')"
    @resume="(id: string) => controlById(id, 'resume')"
    @cancel="(id: string) => controlById(id, 'cancel')"
  />
</template>

<style scoped>
/*
  位置（right / top / max-height）由脚本按实际可用区域算出来，写在 :style 上；
  这里的 right/top 只是「还没量到」时的兜底。
*/
.tasks-pop {
  position: absolute;
  right: var(--sp-4);
  top: 64px;
  z-index: var(--im-z-tasks, 30);
  width: min(380px, calc(100vw - var(--sp-6)));
  /* 独立浮层（不是同一阅读流的一部分）：允许自己有**一层**主要纵向滚动，
     max-height 的真实值由脚本按可用区域算（这里是量不到时的兜底）。
     滚动条走统一策略（原生外观已全局消除，用共享 QIO 指示条）：
     横向永不滚动 —— data-qio-fit + 长文本换行。 */
  max-height: min(48vh, 420px);
  overflow-y: auto;
  overflow-x: clip;
  overscroll-behavior: contain;
  min-width: 0;
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  box-shadow: var(--elev-floating, var(--shadow-2));
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.tasks-pop:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.title {
  margin: 0;
  flex: 1;
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.counts { margin: 0; flex-basis: 100%; font-size: var(--fs-xs); color: var(--text-muted); }
.close {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--link);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
}
.hint,
.notice,
.error {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
  color: var(--text-muted);
}
.hint { color: var(--text-faint); }
.notice { color: var(--text-secondary); }
/* F11：读取失败是「事实可能过期」的提示 —— 用警告色令牌，并让重试按钮在同一行可达 */
.notice.warn {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-2);
  color: var(--text-primary);
  border-left: 2px solid var(--warning);
  background: var(--warning-soft);
  border-radius: var(--r-xs);
  padding: var(--sp-1) var(--sp-2);
}
.error { color: var(--danger); }
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-1);
}
.task-title {
  font-size: var(--fs-sm);
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: var(--sp-1);
}
.badge {
  font-size: var(--fs-xs);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-1);
  border: 1px solid var(--warning);
  color: var(--warning);
  background: var(--warning-soft);
}
.task-status { font-size: var(--fs-xs); color: var(--text-secondary); }
/*
  真实执行状态：文字说清「排队 / 执行 / 暂停 / 结束」，颜色只做辅助；
  长说明换行显示，窄窗口也不会把操作按钮挤出去。
*/
.task-exec {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
}
.task-exec-label {
  flex: none;
  font-family: var(--mono);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
}
.task-exec.tone-open .task-exec-label { color: var(--link); border-color: var(--link); }
.task-exec.tone-waiting .task-exec-label { color: var(--warning); border-color: var(--warning); }
.task-exec.tone-active .task-exec-label { color: var(--accent); border-color: var(--accent); }
.task-exec.tone-problem .task-exec-label { color: var(--danger); border-color: var(--danger); }
.task-exec-detail { min-width: 0; color: var(--text-secondary); overflow-wrap: anywhere; }
.task-control-hint { color: var(--text-faint); }
.task-actions { display: flex; gap: var(--sp-2); flex-wrap: wrap; padding-top: 2px; }
.task-detail { display: flex; flex-direction: column; gap: 2px; }
.task-progress,
.task-bounds { font-size: var(--fs-xs); color: var(--text-faint); line-height: var(--lh-base); }
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-3);
  cursor: pointer;
}
.btn.ghost { background: none; color: var(--link); }
.btn:disabled { opacity: 0.55; cursor: default; }
.btn:focus-visible,
.close:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }

@media (max-width: 900px) {
  .tasks-pop { width: min(340px, calc(100vw - var(--sp-4))); }
}
</style>
