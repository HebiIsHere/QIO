<!--
  顶部「任务」浮层 + 靠近板面虚线预览的单项审批浮条（子智能体 D）。

  契约 §8.1 / §4.4：
  - 任务浮层放左上，右上留给批量列表、右下留给聊天，互不遮挡；
  - 单项批准 / 拒绝入口靠近对应预览：板面本身由 BoardPreviewLayer 画虚线预览，
    这里在板面下沿放一条**靠在预览附近的审批浮条**（IntentPreviewCard docked），
    点条目的「定位预览」会高亮板面上那处预览；
  - 不把演示状态说成真实执行成功：第一阶段没有接入真实执行。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { previewBounds, statusText } from "../../interactive/approval";
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

/** 板面上那处虚线预览的屏幕位置（fixed 定位用）：拿不到就退回板面下沿 */
const anchor = ref<{ x: number; y: number } | null>(null);

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

/** 量一次：浮条放在预览下方；预览太靠下就放到它上方；保证不超出窗口与板面下沿 */
function measure(): void {
  const intent = focused.value;
  if (!intent) {
    anchor.value = null;
    return;
  }
  const rect = previewRect(intent.id);
  if (!rect) {
    anchor.value = null;
    return;
  }
  const stripWidth = Math.min(360, typeof window === "undefined" ? 360 : window.innerWidth - 40);
  const center = rect.left + rect.width / 2;
  const left = Math.min(Math.max(center - stripWidth / 2, 16), (window.innerWidth || 1440) - stripWidth - 16);
  let top = rect.bottom + 10;
  if (top > (window.innerHeight || 900) - 190) top = Math.max(72, rect.top - 158);
  anchor.value = { x: left, y: top };
}

/** 浮条位置：能定位到预览就贴着预览，拿不到坐标就退回板面下沿居中 */
const dockStyle = computed<CSSProperties>(() => {
  if (!anchor.value) {
    return {
      position: "fixed",
      left: "50%",
      bottom: "var(--sp-6)",
      transform: "translateX(-50%)",
      top: "auto",
    };
  }
  return {
    position: "fixed",
    left: anchor.value.x + "px",
    top: anchor.value.y + "px",
    transform: "none",
    bottom: "auto",
  };
});

const onViewportChange = () => window.setTimeout(measure, 0);
let measureTimer: ReturnType<typeof setTimeout> | null = null;

/** 聚焦项 / 展开状态变化后再量一次（DOM 更新之后） */
watch([focused, () => store.tasksOpen], () => {
  onViewportChange();
}, { flush: "post" });

onMounted(() => {
  window.addEventListener("resize", onViewportChange);
  // 板面滚动（平移）与缩放都由视口承载：滚动与滚轮都重新量一次（节流到下一帧）
  window.addEventListener("scroll", onViewportChange, true);
  window.addEventListener("wheel", onViewportChange, { passive: true, capture: true });
  if (measureTimer) clearTimeout(measureTimer);
  measureTimer = setTimeout(measure, 60);
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", onViewportChange);
  window.removeEventListener("scroll", onViewportChange, true);
  window.removeEventListener("wheel", onViewportChange, true);
  if (measureTimer) clearTimeout(measureTimer);
});

function label(intent: Intent): string {
  return statusText(intent).label;
}

function toggleDetail(intentId: string): void {
  expanded.value = { ...expanded.value, [intentId]: !expanded.value[intentId] };
}

/** 定位：记下聚焦项，并等板面滚动过去之后量一次预览位置 */
function locate(intentId: string): void {
  focusedId.value = intentId;
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
  return (
    "预览位置 (" + bounds.x + ", " + bounds.y + ")，大小 " + bounds.w + "×" + bounds.h
  );
}
</script>
<template>
  <section v-if="store.tasksOpen" class="tasks-pop" data-im="tasks-popover">
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
  放右上：与右上角批量入口上下错开（批量入口在 top:16px，这里从 64px 起），
  右下留给聊天；单项审批浮条由锚点定位在预览附近，不再被这个浮层盖住。
*/
.tasks-pop {
  position: absolute;
  right: var(--sp-4);
  top: 64px;
  z-index: 30;
  width: min(380px, calc(100vw - var(--sp-6)));
  max-height: min(48vh, 420px);
  overflow: auto;
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
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
  align-items: center;  gap: var(--sp-1);
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
.close:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }

@media (max-width: 900px) {
  .tasks-pop { right: var(--sp-2); top: 56px; width: min(340px, calc(100vw - var(--sp-4))); }
}
</style>