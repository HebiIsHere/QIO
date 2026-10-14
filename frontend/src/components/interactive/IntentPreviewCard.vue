<!--
  QIO 意图的虚线预览卡片（子智能体 C 原有，子智能体 D 调整单项审批入口与定位）。

  契约 §1.6 / §4.4 / §8.1：
  - 预览用虚线展示位置、结构（组）与关系（链接），不能只给一个文字任务列表；
  - 批准 / 拒绝入口保留在预览附近：除了列表里的完整卡片，本组件还能以 docked 形式
    靠在板面下沿，作为「靠近板面虚线预览的单项审批浮条」；
  - 状态同时用文字说明（颜色只是辅助）；
  - 演示意图必须写明「演示」，批准只表示「成为任务」，不代表已经真的执行成功。
-->
<script setup lang="ts">
import { computed, ref, type CSSProperties } from "vue";
import {
  approveAvailability,
  cardLabel,
  executionActions,
  executionProgress,
  executionText,
  locatePreview,
  operationsText,
  previewBounds,
  rejectAvailability,
  statusText,
  waitingForLabels,
} from "../../interactive/approval";
import type { Intent } from "../../interactive/types";
import { limitsForArea, type ResizeBox, type ResizeHandle } from "../../interactive/resize";
import { useResizeGesture } from "../../composables/useResizeGesture";
import ResizeHandles from "./ResizeHandles.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const props = withDefaults(
  defineProps<{ intent: Intent; allIntents?: Intent[]; active?: boolean; docked?: boolean }>(),
  { allIntents: () => [], active: false, docked: false },
);

const emit = defineEmits<{
  (event: "approve", intentId: string, confirmDependency: boolean): void;
  (event: "reject", intentId: string): void;
  (event: "locate", intentId: string): void;
  (event: "pause", intentId: string): void;
  (event: "resume", intentId: string): void;
  (event: "cancel", intentId: string): void;
}>();

const VIEW_W = 260;
const VIEW_H = 150;
const PAD = 10;

interface Placed {
  id: string;
  label: string;
  style: CSSProperties;
}

interface PlacedLink {
  id: string;
  meaning: string;
  direction: boolean;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

const status = computed(() => statusText(props.intent));
const approve = computed(() => approveAvailability(props.intent));
const reject = computed(() => rejectAvailability(props.intent));
const waiting = computed(() => waitingForLabels(props.intent, props.allIntents));

/* 真实执行：状态与操作都取服务端的执行记录，不在这里猜进度 */
const execution = computed(() => props.intent.execution ?? null);
const exec = computed(() => executionText(execution.value, props.intent));
const execActions = computed(() => executionActions(execution.value, props.intent));
const execProgress = computed(() => executionProgress(execution.value));
/* 模型提议的具体操作：只是预览，不能直接改正式内容 */
const operations = computed(() => operationsText(props.intent));
/**
 * 真实执行已经接管暂停 / 继续 / 取消时，不再显示「批准」这一类入口：
 * 同一个动作只留一个入口，避免两条路径给出不同结果。
 */
const executionControlsOnly = computed(
  () =>
    Boolean(execution.value) &&
    ["queued", "running", "paused"].includes(String(execution.value?.status ?? "")),
);

/* ------------------------------------------------------------------------
 * 待审批预览卡的查看尺寸（任务书 §3.1）
 *
 * 与板面卡片共用同一套尺寸原语（ResizeHandles + useResizeGesture + resize.ts）：
 * 八向拖动、键盘 −/＋、拖动中跟手、取消恢复；最小 / 上限由当前可用区域推出来。
 *
 * 三个边界：
 * - 这是**查看**操作：不写板面状态、不产生表达、不申请审批、不调用模型；
 * - 位置由父组件按板面预览锚点算（position: fixed），这里只叠加**相对锚点**的位移，
 *   所以拉西 / 北边时左 / 上边跟着走（跟手），而锚点本身不被改写；
 * - 只在浮条形态（docked）下生效：列表里的完整卡片由列表自己的排布决定宽度。
 * ---------------------------------------------------------------------- */
const cardEl = ref<HTMLElement | null>(null);
const dockSize = ref<{ w: number; h: number } | null>(null);
const dragSize = ref<{ w: number; h: number } | null>(null);
const dockOffset = ref<{ x: number; y: number }>({ x: 0, y: 0 });
/** 最小尺寸：标题 + 状态 + 批准 / 拒绝 / 定位仍然可用 */
const DOCK_MIN = { w: 280, h: 150 };

const dockLimits = computed(() => {
  const width = typeof window === "undefined" ? 1440 : window.innerWidth || 1440;
  const height = typeof window === "undefined" ? 900 : window.innerHeight || 900;
  // 可用区域扣掉工具栏那一档（96px）与浮条四周的余量；窗口不足时最小值向内收敛
  return limitsForArea({ w: Math.max(320, width), h: Math.max(240, height - 96) }, DOCK_MIN, 32);
});

function measuredSize(): { w: number; h: number } {
  const rect = cardEl.value?.getBoundingClientRect();
  if (rect && rect.width > 0 && rect.height > 0) {
    return { w: Math.round(rect.width), h: Math.round(rect.height) };
  }
  // 没有布局（jsdom / 首帧）：按样式里的宽度上限与常见高度兜底，保证键盘步进有确定起点
  return { w: 360, h: 220 };
}

/** 当前框用**相对锚点**的坐标：父组件给 fixed 的 left/top，位移由 margin 叠加。 */
function currentBox(): ResizeBox {
  const size = dragSize.value ?? dockSize.value ?? measuredSize();
  return { x: dockOffset.value.x, y: dockOffset.value.y, w: size.w, h: size.h };
}

const {
  active: resizeActive,
  onHandleDown: beginResize,
  nudge: nudgeResize,
} = useResizeGesture({
  box: () => currentBox(),
  limits: () => dockLimits.value,
  // 浮层在屏幕坐标系：与对话窗口同口径（scale = 1），不乘画布缩放
  scale: () => 1,
  onPreview: (box) => {
    dragSize.value = { w: box.w, h: box.h };
    dockOffset.value = { x: box.x, y: box.y };
  },
  onCommit: (box) => {
    dockSize.value = { w: box.w, h: box.h };
    dockOffset.value = { x: box.x, y: box.y };
    dragSize.value = null;
  },
  onCancel: (box) => {
    // 取消（Esc / 失焦 / 指针取消 / 卸载）回到拖动前那一版
    dragSize.value = null;
    dockOffset.value = { x: box.x, y: box.y };
  },
});

function onResizeDown(handle: ResizeHandle, event: PointerEvent): void {
  if (!props.docked) return;
  beginResize(handle, event);
}

function onResizeNudge(axis: "w" | "h", direction: 1 | -1): void {
  if (!props.docked) return;
  nudgeResize(axis, direction);
}

/** 尺寸只在浮条形态下生效：宽 / 高与相对锚点的位移都走行内样式（不写任何状态、不排保存）。 */
const dockFrameStyle = computed<CSSProperties>(() => {
  if (!props.docked) return {};
  const style: CSSProperties = {};
  const size = dragSize.value ?? dockSize.value;
  if (size) {
    style.width = Math.round(size.w) + "px";
    style.height = Math.round(size.h) + "px";
  }
  if (dockOffset.value.x) style.marginLeft = Math.round(dockOffset.value.x) + "px";
  if (dockOffset.value.y) style.marginTop = Math.round(dockOffset.value.y) + "px";
  return style;
});

const preview = computed(() => props.intent.preview ?? { cards: [], groups: [], links: [] });
const hasPreview = computed(
  () => (preview.value.cards?.length ?? 0) + (preview.value.groups?.length ?? 0) > 0,
);

const approveLabel = computed(() => {
  switch (props.intent.status) {
    case "waiting_confirm":
      return "确认并开始";
    case "waiting_dependency":
      return "已批准，等待前项";
    case "needs_update":
      return "需要更新，不能批准";
    case "paused":
      return "继续（按当前材料）";
    default:
      return "批准";
  }
});

const layout = computed(() => {
  const cards = preview.value.cards ?? [];
  const groups = preview.value.groups ?? [];
  const boxes = [
    ...groups.map((group) => ({ x: group.x, y: group.y, w: group.w, h: group.h })),
    ...cards.map((card) => ({ x: card.x, y: card.y, w: card.w, h: card.h })),
  ].filter((box) => Number.isFinite(box.x) && Number.isFinite(box.y));
  if (!boxes.length) {
    return { cards: [] as Placed[], groups: [] as Placed[], links: [] as PlacedLink[] };
  }
  const minX = Math.min(...boxes.map((box) => box.x));
  const minY = Math.min(...boxes.map((box) => box.y));
  const maxX = Math.max(...boxes.map((box) => box.x + Math.max(box.w, 1)));
  const maxY = Math.max(...boxes.map((box) => box.y + Math.max(box.h, 1)));
  const width = Math.max(maxX - minX, 1);
  const height = Math.max(maxY - minY, 1);
  const scale = Math.min((VIEW_W - PAD * 2) / width, (VIEW_H - PAD * 2) / height);
  const offsetX = (VIEW_W - width * scale) / 2 - minX * scale;
  const offsetY = (VIEW_H - height * scale) / 2 - minY * scale;

  const rect = (box: { x: number; y: number; w: number; h: number }) => ({
    left: offsetX + box.x * scale,
    top: offsetY + box.y * scale,
    width: Math.max(box.w * scale, 14),
    height: Math.max(box.h * scale, 12),
  });
  const center = (box: { x: number; y: number; w: number; h: number }) => ({
    x: offsetX + (box.x + Math.max(box.w, 1) / 2) * scale,
    y: offsetY + (box.y + Math.max(box.h, 1) / 2) * scale,
  });

  const anchors = new Map<string, { x: number; y: number }>();
  for (const group of groups) anchors.set(group.id, center(group));
  for (const card of cards) anchors.set(card.id, center(card));

  const placedGroups: Placed[] = groups.map((group) => {
    const box = rect(group);
    return {
      id: group.id,
      label: group.name + (group.ordered ? "（有序）" : ""),
      style: {
        left: box.left.toFixed(1) + "px",
        top: box.top.toFixed(1) + "px",
        width: box.width.toFixed(1) + "px",
        height: box.height.toFixed(1) + "px",
      },
    };
  });

  const placedCards: Placed[] = cards.map((card) => {
    const box = rect(card);
    return {
      id: card.id,
      label: cardLabel(card),
      style: {
        left: box.left.toFixed(1) + "px",
        top: box.top.toFixed(1) + "px",
        width: box.width.toFixed(1) + "px",
        height: box.height.toFixed(1) + "px",
      },
    };
  });

  const links: PlacedLink[] = (preview.value.links ?? [])
    .map((link) => {
      const src = anchors.get(link.src);
      const dst = anchors.get(link.dst);
      if (!src || !dst) return null;
      return {
        id: link.id,
        meaning: link.meaning,
        direction: Boolean(link.direction),
        x1: Number(src.x.toFixed(1)),
        y1: Number(src.y.toFixed(1)),
        x2: Number(dst.x.toFixed(1)),
        y2: Number(dst.y.toFixed(1)),
      };
    })
    .filter((item): item is PlacedLink => item !== null);

  return { cards: placedCards, groups: placedGroups, links };
});

const revertText = computed(() => props.intent.revert?.reasonText ?? "");

/**
 * 浮条靠在板面下沿，避免盖住虚线预览本身（板面坐标由预览范围给出）。
 * 预览在板面下方时把浮条放到它上面；没有坐标时退回板面下沿。
 */
const dockedStyle = computed<CSSProperties>(() => {
  if (!props.docked) return {};
  const bounds = previewBounds(preview.value);
  // 板面坐标不等于屏幕坐标（板面可以平移、缩放）：这里只用它做左右倾向，
  // 真正贴着预览定位由放在板面舞台上的父组件（IntentStatusPopover）用屏幕矩形覆盖。
  if (!bounds) return { top: "auto", bottom: "var(--sp-6)", left: "50%", transform: "translateX(-50%)" };
  const centerX = Math.round(bounds.x + Math.max(bounds.w, 1) / 2);
  return {
    left: "min(max(180px, " + centerX + "px), calc(100% - 180px))",
    top: "auto",
    bottom: "var(--sp-6)",
    transform: "translateX(-50%)",
  };
});

/** 单项审批入口的说明：把服务端的判定结果原样说给用户听 */
const dockedNote = computed(() => {
  if (props.intent.status === "needs_update") return status.value.detail;
  if (!approve.value.allowed && reject.value.allowed) return approve.value.text;
  if (exec.value.running) return exec.value.detail;
  if (props.intent.demo) return "演示任务不是真实执行；批准只表示它成为任务。";
  return "批准后才会成为任务：批准前这里是虚线预览，不会改动板面的正式内容。";
});

/** 定位到板面上的虚线预览（沿用 window 事件，A 的 BoardCanvas 监听它） */
function onLocate(): void {
  const bounds = previewBounds(preview.value);
  locatePreview(props.intent.id, bounds);
  emit("locate", props.intent.id);
}
const progressText = computed(() => props.intent.progress?.text ?? "");
</script>

<template>
  <article
    ref="cardEl"
    class="intent-card"
    v-qio-scrollbar
    data-qio-fit
    :class="{ active, docked }"
    :style="[dockedStyle, dockFrameStyle]"
    data-im="intent"
    :data-docked="docked ? '1' : '0'"
    :data-intent-id="intent.id"
    :data-intent-status="intent.status"
  >
    <header class="head">
      <h3 class="title">{{ intent.title }}</h3>
      <span v-if="intent.demo" class="badge demo" data-im="demo-badge">演示</span>
      <span class="badge status" :class="'tone-' + status.tone">{{ status.label }}</span>
    </header>

    <p class="status-detail">{{ status.detail }}</p>
    <p v-if="intent.summary" class="summary">{{ intent.summary }}</p>

    <!--
      模型提议（目标 / 预期 / 具体操作）：只是预览的一部分，
      提交与理解都不构成执行批准，这些提议不会直接改正式板面内容。
    -->
    <div v-if="intent.goal || intent.expected || operations.length" class="proposal" data-im="intent-proposal">
      <p v-if="intent.goal" class="proposal-line" data-im="intent-goal">
        <span class="proposal-key">目标</span>{{ intent.goal }}
      </p>
      <p v-if="intent.expected" class="proposal-line" data-im="intent-expected">
        <span class="proposal-key">预期</span>{{ intent.expected }}
      </p>
      <p v-for="(item, index) in operations" :key="'op-' + index" class="proposal-line" data-im="intent-operation">
        <span class="proposal-key">操作 {{ index + 1 }}</span>{{ item }}
      </p>
      <p class="proposal-hint">这些是 QIO 的提议，只在这里预览；批准之前不会改动正式板面内容。</p>
    </div>

    <!-- 真实执行状态：排队 / 执行 / 暂停 / 结束各有明确文字与可用操作 -->
    <div
      v-if="execution || intent.status === 'running' || intent.status === 'paused'"
      class="execution"
      data-im="intent-execution"
      :data-intent-id="intent.id"
      :data-execution-status="execution?.status ?? 'none'"
    >
      <p class="exec-line">
        <strong class="exec-label" :class="'tone-' + exec.tone" data-im="intent-execution-label">{{ exec.label }}</strong>
        <span class="exec-detail" data-im="intent-execution-detail">{{ exec.detail }}</span>
      </p>
      <p v-if="execution?.steps?.length" class="exec-progress mono" data-im="intent-execution-progress">
        {{ execProgress.text }}
      </p>
      <span v-if="execActions.canPause || execActions.canResume || execActions.canCancel" class="actions">
        <button
          v-if="execActions.canPause"
          class="btn"
          type="button"
          data-im="execution-pause"
          :data-intent-id="intent.id"
          @click="emit('pause', intent.id)"
        >
          暂停
        </button>
        <button
          v-if="execActions.canResume"
          class="btn"
          type="button"
          data-im="execution-resume"
          :data-intent-id="intent.id"
          @click="emit('resume', intent.id)"
        >
          继续执行
        </button>
        <button
          v-if="execActions.canCancel"
          class="btn ghost"
          type="button"
          data-im="execution-cancel"
          :data-intent-id="intent.id"
          @click="emit('cancel', intent.id)"
        >
          取消任务
        </button>
      </span>
      <p v-if="execActions.reason && executionControlsOnly" class="exec-hint" data-im="intent-execution-hint">
        {{ execActions.reason }}
      </p>
    </div>

    <!-- 虚线预览：位置 / 结构 / 关系。虚线 = 还没成为正式内容 -->
    <div class="preview-wrap">
      <p class="preview-caption">
        虚线预览{{ hasPreview ? "（位置、结构、关系）" : "" }}：还没有成为正式内容。
      </p>
      <div v-if="hasPreview" class="preview" data-im="preview" :data-intent-id="intent.id">
        <div
          v-for="item in layout.groups"
          :key="item.id"
          class="preview-group"
          data-im="preview"
          :data-intent-id="intent.id"
          :style="item.style"
        >
          <span class="preview-group-name">{{ item.label }}</span>
        </div>
        <svg
          v-if="layout.links.length"
          class="preview-links"
          :viewBox="'0 0 ' + VIEW_W + ' ' + VIEW_H"
          preserveAspectRatio="xMidYMid meet"
        >
          <line
            v-for="item in layout.links"
            :key="item.id"
            class="preview-link"
            data-im="preview"
            :data-intent-id="intent.id"
            :x1="item.x1"
            :y1="item.y1"
            :x2="item.x2"
            :y2="item.y2"
          />
          <text
            v-for="item in layout.links"
            :key="item.id + '-label'"
            class="preview-link-meaning"
            :x="(item.x1 + item.x2) / 2"
            :y="(item.y1 + item.y2) / 2 - 3"
            text-anchor="middle"
          >
            {{ (item.direction ? "→ " : "") + (item.meaning || "关系") }}
          </text>
        </svg>
        <div
          v-for="item in layout.cards"
          :key="item.id"
          class="preview-card"
          data-im="preview"
          :data-intent-id="intent.id"
          :style="item.style"
        >
          <span class="preview-card-label">{{ item.label }}</span>
        </div>
      </div>
      <p v-else class="preview-empty">这项预览没有可展示的内容。</p>
      <p v-if="preview.note" class="preview-note">{{ preview.note }}</p>
    </div>

    <p v-if="waiting.length" class="waiting">
      等待前项：{{ waiting.join("、") }}（前项没有成功完成之前不会开始，也不会自动开始）
    </p>
    <p v-if="progressText && (intent.status === 'running' || intent.status === 'paused')" class="progress">
      进度：{{ progressText }}
    </p>
    <p v-if="revertText" class="revert-text">{{ revertText }}</p>

    <div class="actions">
      <button
        v-if="!executionControlsOnly"
        class="btn primary"
        type="button"
        :data-im="intent.status === 'paused' ? 'resume' : 'approve'"
        :data-intent-id="intent.id"
        :disabled="!approve.allowed"
        :title="approve.text"
        @click="emit('approve', intent.id, approve.needsConfirm)"
      >
        {{ approveLabel }}
      </button>
      <button
        class="btn"
        type="button"
        data-im="reject"
        :data-intent-id="intent.id"
        :disabled="!reject.allowed"
        :title="reject.text"
        @click="emit('reject', intent.id)"
      >
        拒绝
      </button>
      <button
        class="btn ghost"
        type="button"
        data-im="preview-locate"
        :data-intent-id="intent.id"
        @click="onLocate"
      >
        在板面上定位
      </button>
    </div>
    <p v-if="intent.status === 'paused'" class="action-hint">
      继续不会自动重试，也不会重新执行已完成的部分；材料依据会按当前板面重新记录。
    </p>
    <p v-else-if="!approve.allowed" class="action-hint">{{ approve.text }}</p>
    <p v-if="docked" class="action-hint docked-note">{{ dockedNote }}</p>
    <!--
      浮条的查看尺寸：与卡片同一套把手（八向 + 键盘 −/＋），只改这一处的显示大小，
      不产生审批、不写板面状态、不调用模型。
    -->
    <ResizeHandles
      v-if="docked"
      :label="'审批预览：' + intent.title"
      :visible="resizeActive"
      @handle-down="onResizeDown"
      @nudge="onResizeNudge"
    />
  </article>
</template>

<style scoped>
.intent-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  background: var(--bg-surface);
}
.intent-card.active {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
/*
  靠近板面虚线预览的单项审批浮条：只保留说明 + 批准 / 拒绝 / 定位。
  绝对定位（left 由预览在板面上的位置算出），不抢板面中央、也不遮住预览本身。
*/
.intent-card.docked {
  position: absolute;
  /* 尺寸可被用户调整（任务书 §3.1）：内容超出时区内滚动，不把把手顶出去。
     只有这一层纵向滚动（滚动条走统一策略），横向永不滚动；批准 / 拒绝固定在卡片底部，
     卡片被用户缩小时也不会把审批按钮滚出可视区（正文滚动与固定操作分离）。 */
  overflow-y: auto;
  overflow-x: clip;
  overscroll-behavior: contain;
  z-index: 34;
  width: min(360px, calc(100vw - var(--sp-6)));
  padding: var(--sp-2) var(--sp-3);
  border-style: dashed;
  border-color: var(--link);
  background: var(--bg-elevated);
  /* 备用阴影也必须走令牌（契约 §10.8）：双主题各有自己的阴影，硬编码色值会把暗色阴影搬到亮色上 */
  box-shadow: var(--shadow-2);
}
.intent-card.docked .preview-wrap,
.intent-card.docked .proposal,
.intent-card.docked .summary,
.intent-card.docked .progress,
.intent-card.docked .revert-text,
.intent-card.docked .waiting { display: none; }
.intent-card.docked .title { font-size: var(--fs-sm); }
.intent-card.docked .status-detail { display: none; }
.intent-card.docked .actions {
  /* 落实「固定操作」：卡片内滚到任何位置，批准 / 拒绝 / 定位都还在 */
  position: sticky;
  bottom: 0;
  z-index: 2;
  padding-top: var(--sp-1);
  background: var(--bg-elevated);
}
.intent-card.docked .docked-note { color: var(--text-faint); }
.head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-sm);
  color: var(--text-strong);
  flex: 1;
  min-width: 0;
}
.badge {
  font-size: var(--fs-xs);
  border-radius: var(--r-pill);
  padding: 1px var(--sp-2);
  border: 1px solid var(--border-strong);
  color: var(--text-secondary);
  white-space: nowrap;
}
.badge.demo {
  color: var(--warning);
  border-color: var(--warning);
  background: var(--warning-soft);
}
.tone-open { color: var(--link); border-color: var(--link); }
.tone-waiting { color: var(--warning); border-color: var(--warning); }
.tone-active { color: var(--accent); border-color: var(--accent); }
.tone-settled { color: var(--text-muted); }
.tone-problem { color: var(--danger); border-color: var(--danger); }
.status-detail,
.summary,
.preview-caption,
.preview-note,
.waiting,
.progress,
.revert-text,
.action-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: var(--lh-base);
}
.summary { color: var(--text-secondary); }
/* 模型提议：与正式内容在层级上分开；长文换行，窄窗口不溢出 */
.proposal { display: flex; flex-direction: column; gap: 2px; }
.proposal-line {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  line-height: var(--lh-base);
  overflow-wrap: anywhere;
}
.proposal-key {
  display: inline-block;
  margin-right: var(--sp-1);
  font-family: var(--mono);
  color: var(--text-muted);
}
.proposal-hint { margin: 0; font-size: var(--fs-xs); color: var(--text-faint); line-height: var(--lh-base); }
/* 真实执行：只在确实有执行记录（或已批准）时出现；状态文字 + 可用的暂停 / 继续 / 取消 */
.execution {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  border-left: 2px solid var(--border-strong);
  padding-left: var(--sp-2);
}
.exec-line {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  margin: 0;
  min-width: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-base);
}
.exec-label {
  flex: none;
  font-family: var(--mono);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
}
.exec-label.tone-open { color: var(--link); border-color: var(--link); }
.exec-label.tone-waiting { color: var(--warning); border-color: var(--warning); }
.exec-label.tone-active { color: var(--accent); border-color: var(--accent); }
.exec-label.tone-problem { color: var(--danger); border-color: var(--danger); }
.exec-detail { min-width: 0; color: var(--text-secondary); overflow-wrap: anywhere; }
.exec-progress { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.exec-hint { margin: 0; font-size: var(--fs-xs); color: var(--text-faint); }
.revert-text { color: var(--text-secondary); }
.preview-wrap {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.preview {
  position: relative;
  width: 260px;
  height: 150px;
  max-width: 100%;
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-sm);
  background: var(--bg-inset);
  overflow: hidden;
}
.preview-group {
  position: absolute;
  border: 1px dashed var(--link);
  border-radius: var(--r-xs);
}
.preview-group-name {
  position: absolute;
  top: 1px;
  left: 3px;
  font-size: var(--fs-xs);
  color: var(--link);
  white-space: nowrap;
}
.preview-links {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.preview-link {
  stroke: var(--link);
  stroke-width: 1;
  stroke-dasharray: 4 3;
}
.preview-link-meaning {
  fill: var(--text-muted);
  font-size: 9px;
}
.preview-card {
  position: absolute;
  border: 1px dashed var(--accent);
  border-radius: var(--r-xs);
  background: var(--accent-veil);
  overflow: hidden;
  padding: 1px 3px;
}
.preview-card-label {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  display: block;
  white-space: nowrap;
  text-overflow: ellipsis;
  overflow: hidden;
}
.preview-empty {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  padding: var(--sp-1) var(--sp-3);
  cursor: pointer;
}
.btn.primary {
  color: var(--on-accent);
  background: var(--accent);
  border-color: var(--accent);
}
.btn.primary:hover:enabled { background: var(--accent-hover); }
.btn.ghost { background: none; color: var(--link); }
.btn:disabled { opacity: 0.55; cursor: default; }
.btn:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
</style>
