<!--
  顶部「任务」浮层 + 靠近板面虚线预览的单项审批浮条（子智能体 D）。

  契约 §9.6：
  - 任务浮层与批量列表、聊天共用右侧同一片区域，所以位置不能写死：
    这里按**实际可用区域**在几个候选位置里挑一个与聊天面板 / 批量列表 / 底部工具栏
    相交面积最小的（同级里再按「右上 → 聊天左边 → 聊天上方」的优先级）；
  - 单项批准 / 拒绝入口靠近对应预览：板面本身由 BoardPreviewLayer 画虚线预览，
    这里在预览附近放一条审批浮条（IntentPreviewCard docked），同样要避开聊天与批量面板；
  - 键盘与指针边界：浮层内空格不触发板面框选、滚轮优先滚自己、Escape 先收浮层；
  - 不把演示状态说成真实执行成功：第一阶段没有接入真实执行。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { locatePreview, previewBounds, statusText } from "../../interactive/approval";
import type { Intent } from "../../interactive/types";
import IntentPreviewCard from "./IntentPreviewCard.vue";

const store = useInteractiveStore();

const busy = ref(false);
const notice = ref<string | null>(null);
const error = ref<string | null>(null);
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

/** 执行中 / 已暂停：需要用户决定的任务 */
const tasks = computed(() => store.activeIntents);
/**
 * 浮条展示的意图：优先用用户刚聚焦的那一项，否则用「执行中」的任务
 * （执行中的任务在板面上有虚线预览，浮条能贴着它；暂停的任务按契约不画预览）。
 */
const focused = computed<Intent | null>(() => {
  const byFocus = focusedId.value ? store.intentById(focusedId.value) : undefined;
  if (byFocus && ["running", "paused"].includes(byFocus.status)) return byFocus;
  return tasks.value.find((item) => item.status === "running") ?? tasks.value[0] ?? null;
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

/** 现在必须避开的浮层：聊天面板、批量列表整体、底部工具栏 */
function avoidBoxes(): Box[] {
  const boxes: Box[] = [];
  const chat = boxOf('[data-im="chat-panel"]');
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
  measureDock(avoid, viewWidth, viewHeight);
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

/** 审批浮条：贴着预览放，但绝不压住聊天 / 批量列表 / 工具栏 */
function measureDock(avoid: Box[], viewWidth: number, viewHeight: number): void {
  const intent = focused.value;
  const stripWidth = Math.min(360, viewWidth - 40);
  // 审批浮条的根元素（IntentPreviewCard 的 data-im="intent"）
  const card = document.querySelector('[data-im="intent"]') as HTMLElement | null;
  const cardHeight = card && card.getBoundingClientRect().height > 0 ? card.getBoundingClientRect().height : 158;
  const rect = intent ? previewRect(intent.id) : null;

  const options: CSSProperties[] = [];
  if (rect) {
    const center = rect.left + rect.width / 2;
    const left = Math.min(Math.max(center - stripWidth / 2, 16), viewWidth - stripWidth - 16);
    // ① 预览下方
    options.push(fixedAt(left, rect.bottom + 10));
    // ② 预览上方
    options.push(fixedAt(left, Math.max(72, rect.top - cardHeight - 10)));
    // ③ 预览左侧（窄窗口里预览靠右时）
    options.push(fixedAt(Math.max(16, rect.left - stripWidth - 12), Math.min(rect.top, viewHeight - cardHeight - 16)));
  }
  // ④ 退回板面下沿居中
  options.push({
    position: "fixed",
    left: "50%",
    bottom: "var(--sp-6)",
    transform: "translateX(-50%)",
    top: "auto",
  });

  let best = options[options.length - 1];
  let bestPenalty = Number.POSITIVE_INFINITY;
  for (const option of options) {
    const left = option.left === "50%" ? viewWidth / 2 - stripWidth / 2 : Number.parseFloat(String(option.left)) || 0;
    const top = String(option.top) === "auto" ? viewHeight - 24 - cardHeight : Number.parseFloat(String(option.top)) || 0;
    const box: Box = { left, top, right: left + stripWidth, bottom: top + cardHeight };
    const score = penalty(box, avoid);
    if (score < bestPenalty) {
      bestPenalty = score;
      best = option;
    }
    if (score === 0) break;
  }
  dockStyle.value = best;
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
      <button class="close" type="button" data-im="tasks-close" @click="store.tasksOpen = false">
        收起
      </button>
    </header>

    <p class="hint" role="note">
      待审批的条目走批量审批入口；这里只列已经批准、正在执行或已暂停的任务。
      第一阶段没有接入真实执行，进度来自演示入口，不能说成真实执行成功。
    </p>
    <p v-if="notice" class="notice" role="status" data-im="tasks-notice">{{ notice }}</p>
    <p v-if="error" class="error" role="alert">操作失败：{{ error }}</p>

    <p v-if="!tasks.length" class="hint">现在没有执行中或已暂停的任务。</p>
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
            v-if="intent.status === 'paused'"
            class="btn"
            type="button"
            data-im="task-resume"
            :data-intent-id="intent.id"
            :disabled="busy"
            @click="resume(intent)"
          >
            继续（按当前材料）
          </button>
        </span>
        <span v-if="expanded[intent.id]" class="task-detail">
          <span class="task-progress mono">{{ intent.progress.text || "进度未记录" }}</span>
          <span class="task-bounds">{{ boundsText(intent) }}</span>
        </span>
      </li>
    </ul>
  </section>

  <!-- 单项审批浮条：贴着板面上那处虚线预览（拿不到坐标就退回板面下沿居中） -->
  <IntentPreviewCard
    v-if="store.tasksOpen && focused"
    :intent="focused"
    :all-intents="store.intents"
    :style="dockStyle"
    docked
    active
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
  max-height: min(48vh, 420px);
  overflow: auto;
  overscroll-behavior: contain;
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
