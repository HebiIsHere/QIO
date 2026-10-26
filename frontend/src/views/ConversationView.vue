<script setup lang="ts">
import { vQioTip } from "../composables/useQioTip";
/**
 * 对话页 v2 外壳（A 负责外层布局与顶部全局栏入口；中央/右侧内容由 B/C 各自组件承担）。
 *
 * 布局：
 *   顶部全局栏（WindowChrome，A）—— QIO 字标 / 设置 / 窗口按钮
 *   ├─ 左侧任务栏（A）        ← 可拖宽、可收起；窄窗口临时收起（不写偏好）
 *   ├─ 中央对话工作区（B）
 *   └─ 右侧只读预览（C）      ← 可拖宽、可收起、可全窗口
 *
 * 页面切换（对话/互动/设置）不初始化业务状态：shell.load() 与 loadHistory() 只在
 * 第一次进入时执行，返回时最多做一次幂等刷新；草稿/附件/阅读位置各自在 store 里保留。
 * 右下角记忆星球入口保持现有 PlanetDock（用户明确：样式与功能不改）。
 */
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useSessionStore } from "../stores/session";
import { useConversationShellStore } from "../stores/conversationShell";
import { useConversationTasksStore } from "../stores/conversationTasks";
import {
  LEFT_AUTO_COLLAPSE_BELOW,
  LEFT_DEFAULT,
  LEFT_MAX,
  LEFT_MIN,
  RIGHT_DEFAULT,
  RIGHT_MAX,
  RIGHT_MIN,
  clampLeftWidth,
  clampRightWidth,
} from "../conversation/layoutPrefs";
import { listenFocusTask } from "../services/conversationTasks";
import TaskRail from "../components/conversation/TaskRail.vue";
import ChatColumn from "../components/conversation/ChatColumn.vue";
import PreviewPanel from "../components/conversation/PreviewPanel.vue";
import UnifiedApprovalLayer from "../components/conversation/UnifiedApprovalLayer.vue";
import PlanetDock from "../components/PlanetDock.vue";
import PlanetBoot from "../components/PlanetBoot.vue";
import RecoveryInbox from "../components/RecoveryInbox.vue";
import { useConversationPreviewStore } from "../stores/conversationPreview";
import {
  registerConversationPreviewOpener,
  registerConversationReplyCodeOpener,
} from "../components/conversation/previewBridge";
import { registerExecutionTaskLocator } from "../components/conversation/taskLocator";
import type { MemoryOpenTarget } from "../stores/memoryQuestions";

const PlanetView = defineAsyncComponent({
  loader: () => import("./PlanetView.vue"),
  loadingComponent: PlanetBoot,
  delay: 0,
});

const session = useSessionStore();
const shell = useConversationShellStore();
const tasks = useConversationTasksStore();
const preview = useConversationPreviewStore();

const planetOpen = ref(false);
const planetMounted = ref(false);
const planetSeq = ref(0);
/** 打开星球时定位到哪个页签 / 哪条待确认问题（记忆问题提醒 → 星球待确认）。 */
const planetTab = ref<"topic" | "knowledge" | "entity" | "memory">("topic");
const planetFocusPendingId = ref<string | null>(null);
/** 「未完成事项」恢复区：左栏任务的「去处理」会打开它（原有恢复动作都在这里）。 */
const recoveryOpen = ref(false);
let stopViewport: (() => void) | null = null;
let stopFocusListener: (() => void) | null = null;

// 接线桥必须在**本组件 setup 期间**注册：消息流/任务栏子组件在 setup 时读取
// 「有没有接线」决定是否渲染按钮（放到 onMounted 会晚于子组件 setup，按钮永远不出现）。
registerExecutionTaskLocator((ref) => {
  void tasks.focusTask(`${ref.source}:${ref.sourceId}`);
  return true;
});
registerConversationPreviewOpener((target) => {
  void preview.openPreview(target);
});
registerConversationReplyCodeOpener((input) => {
  void preview.openReplyCode(input);
});

// ── 三栏拖动 ───────────────────────────────────────────────────────────────

type DragKind = "left" | "right" | null;
const dragKind = ref<DragKind>(null);
let dragStartX = 0;
let dragStartWidth = 0;

function setDraggingChrome(on: boolean) {
  if (typeof document === "undefined") return;
  document.body.style.cursor = on ? "col-resize" : "";
  document.body.style.userSelect = on ? "none" : "";
}

function beginDrag(kind: Exclude<DragKind, null>, event: PointerEvent) {
  dragKind.value = kind;
  dragStartX = event.clientX;
  dragStartWidth = kind === "left" ? shell.prefs.leftWidth : shell.prefs.rightWidth;
  (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
  setDraggingChrome(true);
  event.preventDefault();
}

function moveDrag(event: PointerEvent) {
  if (!dragKind.value) return;
  const delta = event.clientX - dragStartX;
  if (dragKind.value === "left") {
    // 拖动过程中只改内存：松手才落盘，避免每移动一像素写一次服务端。
    shell.prefs.leftWidth = clampLeftWidth(dragStartWidth + delta);
    shell.prefs.leftCollapsed = false;
  } else {
    shell.prefs.rightWidth = clampRightWidth(
      dragStartWidth - delta,
      shell.viewportWidth,
      shell.leftWidth,
    );
  }
}

function endDrag() {
  if (!dragKind.value) return;
  const kind = dragKind.value;
  dragKind.value = null;
  setDraggingChrome(false);
  // 松手才持久化；用户手动调整的值优先于默认值。
  if (kind === "left") shell.setLeftWidth(shell.prefs.leftWidth);
  else shell.setRightWidth(shell.prefs.rightWidth);
}

/** 双击分隔条恢复该栏默认宽度（并持久化）。 */
function resetWidth(kind: "left" | "right") {
  if (kind === "left") shell.setLeftWidth(LEFT_DEFAULT);
  else shell.setRightWidth(RIGHT_DEFAULT);
}

function nudgeSplitter(kind: "left" | "right", event: KeyboardEvent) {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  const step = event.shiftKey ? 32 : 12;
  event.preventDefault();
  const direction = event.key === "ArrowRight" ? 1 : -1;
  if (kind === "left") shell.setLeftWidth(shell.prefs.leftWidth + direction * step);
  else shell.setRightWidth(shell.prefs.rightWidth - direction * step);
}

// ── 收起后的任务入口 / 窄窗口抽屉 ─────────────────────────────────────────

const railDrawer = ref(false);
const railDrawerEl = ref<HTMLElement | null>(null);
const railEntryEl = ref<HTMLButtonElement | null>(null);

const railBadge = computed(() => {
  const count = tasks.badgeCount;
  return count > 0 ? String(count) : "";
});
const railBadgeTone = computed(() => (tasks.attentionCount > 0 ? "attention" : "active"));
const railEntryLabel = computed(() => {
  const base = shell.autoCollapsedLeft ? "打开任务列表（窗口较窄，任务栏暂时收起）" : "展开任务栏";
  const parts = [base];
  if (tasks.attentionCount > 0) parts.push(tasks.attentionCount + " 项需要处理");
  else if (tasks.activeCount > 0) parts.push(tasks.activeCount + " 项运行或排队");
  return parts.join("，");
});

function onRailEntry() {
  // 窄窗口：抽屉式临时展开（不改写用户偏好）；否则按用户手动收起状态展开。
  if (shell.viewportWidth < LEFT_AUTO_COLLAPSE_BELOW) railDrawer.value = true;
  else shell.toggleLeft();
}

async function closeRailDrawer() {
  if (!railDrawer.value) return;
  railDrawer.value = false;
  await nextTick();
  railEntryEl.value?.focus();
}

// 手动收起/变宽后任务栏恢复可见：抽屉不再需要。
watch(
  () => shell.leftVisible,
  (visible) => {
    if (visible && railDrawer.value) railDrawer.value = false;
  },
);

// 中央执行记录要求定位任务时，如果左栏正被（手动或临时）收起，就临时展开抽屉。
watch(
  () => tasks.focusTarget,
  (focus) => {
    if (focus && !shell.leftVisible) railDrawer.value = true;
  },
);

watch(railDrawer, async (open) => {
  if (!open) return;
  await nextTick();
  const root = railDrawerEl.value;
  if (!root) return;
  const first = root.querySelector<HTMLElement>("button:not(:disabled)");
  first?.focus();
});

// ── 星球入口（保持现有行为） ───────────────────────────────────────────────

function mountPlanetWhenIdle() {
  const start = () => {
    if (planetMounted.value) return;
    if (session.turnRunning) {
      window.setTimeout(start, 1500);
      return;
    }
    planetMounted.value = true;
  };
  const ric = (window as unknown as { requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number })
    .requestIdleCallback;
  if (typeof ric === "function") ric(start, { timeout: 4000 });
  else window.setTimeout(start, 2000);
}

function openPlanet() {
  planetSeq.value += 1;
  planetMounted.value = true;
  planetOpen.value = true;
}

function closePlanet(seq?: number) {
  if (seq !== undefined && seq !== planetSeq.value) return;
  planetOpen.value = false;
}

function hasOpenModal(): boolean {
  if (typeof document === "undefined") return false;
  return Boolean(document.querySelector('[role="dialog"][aria-modal="true"]'));
}

// 全窗口预览时按 Esc 返回对话；任务抽屉先关。弹窗打开时不抢它的 Esc。
function onKeydown(event: KeyboardEvent) {
  if (recoveryOpen.value) {
    if (event.key === "Escape") {
      event.preventDefault();
      recoveryOpen.value = false;
    }
    return;
  }
  if (railDrawer.value) {
    if (event.key === "Escape") {
      event.preventDefault();
      void closeRailDrawer();
      return;
    }
    if (event.key === "Tab") trapDrawerFocus(event);
    return;
  }
  if (event.key !== "Escape") return;
  if (!shell.effectivePreviewFull || planetOpen.value) return;
  if (event.defaultPrevented || hasOpenModal()) return;
  shell.setPreviewFull(false);
}

/** 抽屉是模态层：Tab 在抽屉内循环，不跑到背后的页面。 */
function trapDrawerFocus(event: KeyboardEvent) {
  const root = railDrawerEl.value;
  if (!root) return;
  const focusables = Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not(:disabled), [href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ),
  );
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  const inside = Boolean(active && root.contains(active));
  if (event.shiftKey) {
    if (!inside || active === first) {
      event.preventDefault();
      last.focus();
    }
    return;
  }
  if (!inside || active === last) {
    event.preventDefault();
    first.focus();
  }
}

// 窗口重新可见（休眠唤醒 / 切回桌面）时补一次幂等刷新。
function onVisibilityChange() {
  if (typeof document === "undefined" || document.visibilityState !== "visible") return;
  if (tasks.current.loaded) void tasks.refresh("current", { silent: true });
  if (shell.prefs.taskTab === "history" && tasks.history.loaded) {
    void tasks.refresh("history", { silent: true });
  }
}

// ── 恢复事项（原有恢复动作集中在这里，不复制一套） ─────────────────────────

function openRecoveryFromTask(): void {
  recoveryOpen.value = true;
}

function closeRecovery(): void {
  recoveryOpen.value = false;
}

// ── 记忆问题提醒 → 星球待确认定位（不改星球入口样式/功能） ────────────────

function onOpenMemory(target: MemoryOpenTarget): void {
  planetTab.value = target.tab === "memory" ? "memory" : "topic";
  planetFocusPendingId.value = target.itemId ?? null;
  openPlanet();
}

/**
 * 右侧预览开/关会改变「右下角是否被固定面板占据」：让星球入口重新计算一次位置
 * （useFloatingWindow 在 resize 时会避开 [data-preview-panel] 等固定控件）。
 * 只触发布局重算，不改星球入口样式或拖动行为。
 */
watch(
  () => shell.rightVisible,
  () => {
    if (typeof window === "undefined") return;
    nextTick(() => window.dispatchEvent(new Event("resize")));
  },
);

// ── 生命周期（页面切换不重复初始化） ──────────────────────────────────────

onMounted(() => {
  // 只有第一次进入才读布局偏好与历史；从设置/互动页返回时不重置现有状态。
  if (!shell.loaded) void shell.load();
  stopViewport = shell.installViewportTracking();
  if (session.history.status === "idle") void session.loadHistory();

  tasks.bindEvents();
  tasks.startPolling();
  if (tasks.current.loaded) {
    // 幂等刷新：离开期间可能出现新任务/新状态，但不清空、不重置展开与滚动。
    void tasks.refresh("current", { silent: true });
    if (shell.prefs.taskTab === "history" && tasks.history.loaded) {
      void tasks.refresh("history", { silent: true });
    }
  } else {
    void tasks.ensureLoaded();
  }
  stopFocusListener = listenFocusTask((taskId) => {
    void tasks.focusTask(taskId);
  });
  window.addEventListener("qio:open-recovery", openRecoveryFromTask);

  mountPlanetWhenIdle();
  window.addEventListener("keydown", onKeydown);
  document.addEventListener("visibilitychange", onVisibilityChange);
});

onBeforeUnmount(() => {
  stopViewport?.();
  stopViewport = null;
  stopFocusListener?.();
  stopFocusListener = null;
  window.removeEventListener("keydown", onKeydown);
  document.removeEventListener("visibilitychange", onVisibilityChange);
  window.removeEventListener("qio:open-recovery", openRecoveryFromTask);
  registerExecutionTaskLocator(null);
  registerConversationPreviewOpener(null);
  registerConversationReplyCodeOpener(null);
  setDraggingChrome(false);
  tasks.dispose();
  preview.dispose();
  // 最后一次布局改动要落盘（防抖窗口内切走页面时不能丢）。
  void shell.flush();
});
</script>

<template>
  <div
    class="conversation-workspace"
    :class="{
      'left-collapsed': !shell.leftVisible,
      'preview-full': shell.effectivePreviewFull,
    }"
    :data-dragging="dragKind ?? undefined"
    :data-left-auto="shell.autoCollapsedLeft ? 'true' : 'false'"
  >
    <div class="workspace-row">
      <TaskRail
        v-if="shell.leftVisible"
        class="left-pane"
        :style="{ width: shell.leftWidth + 'px' }"
      />

      <div
        v-if="shell.leftVisible"
        class="splitter"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整左侧任务栏宽度"
        :aria-valuemin="LEFT_MIN"
        :aria-valuemax="LEFT_MAX"
        :aria-valuenow="shell.leftWidth"
        :aria-valuetext="shell.leftWidth + ' 像素'"
        tabindex="0"
        @pointerdown="beginDrag('left', $event)"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @keydown="nudgeSplitter('left', $event)"
        @dblclick="resetWidth('left')"
      ></div>

      <div v-else class="collapsed-rail" aria-label="任务栏已收起">
        <button
          ref="railEntryEl"
          class="rail-btn"
          type="button"
          data-rail-entry
          v-qio-tip="railEntryLabel"
          :aria-label="railEntryLabel"
          :aria-expanded="railDrawer ? 'true' : 'false'"
          @click="onRailEntry"
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <rect x="2.5" y="3" width="11" height="10" rx="1.5" />
            <path d="M6.5 3v10M9 6h2M9 10h2" />
          </svg>
          <span class="rail-label">任务</span>
          <span
            v-if="railBadge"
            class="rail-badge mono"
            :data-tone="railBadgeTone"
            aria-hidden="true"
          >{{ railBadge }}</span>
        </button>
      </div>

      <ChatColumn class="center-pane">
        <template #planet>
          <PlanetDock anchored @open="openPlanet" />
        </template>
      </ChatColumn>

      <div
        v-if="shell.rightVisible && !shell.effectivePreviewFull"
        class="splitter"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整文件预览宽度"
        :aria-valuemin="RIGHT_MIN"
        :aria-valuemax="RIGHT_MAX"
        :aria-valuenow="shell.rightWidth"
        :aria-valuetext="shell.rightWidth + ' 像素'"
        tabindex="0"
        @pointerdown="beginDrag('right', $event)"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @keydown="nudgeSplitter('right', $event)"
        @dblclick="resetWidth('right')"
      ></div>

      <PreviewPanel
        v-if="shell.rightVisible && !shell.effectivePreviewFull"
        class="right-pane"
        :style="{ width: shell.rightWidth + 'px' }"
        @close="shell.setRightOpen(false)"
        @toggle-full="shell.setPreviewFull(true)"
      />
    </div>

    <!-- 全窗口预览：外层负责卡片网格的内缩/圆角，内层只填满它 -->
    <div v-if="shell.rightVisible && shell.effectivePreviewFull" class="preview-overlay">
      <PreviewPanel
        full
        @close="shell.setRightOpen(false)"
        @toggle-full="shell.setPreviewFull(false)"
      />
    </div>

    <!-- 任务栏被窗口宽度临时收起时的抽屉（不改写偏好；Esc / 点外部关闭） -->
    <div v-if="railDrawer" class="rail-drawer-layer">
      <div class="rail-drawer-backdrop" aria-hidden="true" @click="closeRailDrawer"></div>
      <div
        ref="railDrawerEl"
        class="rail-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="任务列表"
        :style="{ width: shell.prefs.leftWidth + 'px' }"
      >
        <TaskRail drawer @close="closeRailDrawer" />
      </div>
    </div>

    <!-- 预览栏收起但仍有标签：贴边小入口恢复（与右下角星球入口错开：在中线偏上） -->
    <button
      v-if="!shell.rightVisible && shell.prefs.previewTabs.length"
      class="preview-reopen"
      type="button"
      v-qio-tip="'打开文件预览（' + shell.prefs.previewTabs.length + ' 个标签）'"
      :aria-label="'打开文件预览，' + shell.prefs.previewTabs.length + ' 个标签'"
      @click="shell.setRightOpen(true)"
    >
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10 4.5 5.5 8 10 11.5" /></svg>
      <span class="preview-reopen-count mono">{{ shell.prefs.previewTabs.length }}</span>
    </button>

    <!-- 未完成事项（恢复区）：原有继续 / 重发 / 忽略 / 重试动作都在这里，
         左栏任务的「去处理」把它打开；不另做一套恢复逻辑。 -->
    <div v-if="recoveryOpen" class="recovery-layer">
      <div class="recovery-backdrop" aria-hidden="true" @click="closeRecovery"></div>
      <section
        class="recovery-panel"
        role="dialog"
        aria-modal="true"
        aria-label="未完成事项"
        @keydown.esc.stop="closeRecovery"
      >
        <header class="recovery-head">
          <h2>未完成事项</h2>
          <button class="qio-btn quiet mini" type="button" @click="closeRecovery">关闭</button>
        </header>
        <p class="recovery-hint">
          这些是上次没有执行完、或归属无法确认的事项。继续、重发或忽略都由你决定；
          关掉这个窗口不会处理它们。
        </p>
        <RecoveryInbox />
      </section>
    </div>

    <UnifiedApprovalLayer @open-memory="onOpenMemory" />
    <PlanetView
      v-if="planetMounted"
      :seq="planetSeq"
      :open="planetOpen"
      :initial-tab="planetTab"
      :focus-pending-id="planetFocusPendingId"
      @close="closePlanet"
    />
  </div>
</template>

<style scoped>
.conversation-workspace {
  /* 卡片之间唯一间距来源：顶栏→卡片、左栏→话题、话题→消息、左右预览都用它 */
  --conversation-card-gap: 12px;
  position: relative;
  isolation: isolate;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  /* 背景改由整窗 ::before 承担：从窗口 y=0 开始，而不是从 workspace 的 y=80 开始 */
  background: transparent;
  color: var(--text-primary);
  font-family: var(--sans);
  animation: workspace-in var(--dur-page-out) var(--ease-out) both;
}
@keyframes workspace-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
/* 整窗背景层：fixed 到视口，覆盖全局栏四周；全局栏/卡片仍是表面色，不被染色 */
.conversation-workspace::before {
  content: "";
  position: fixed;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  background:
    radial-gradient(ellipse at 0% 0%, var(--accent-soft) 0, transparent 46%),
    var(--bg-base);
}
.workspace-row {
  display: flex;
  flex: 1;
  min-height: 0;
  min-width: 0;
  /* 顶栏下沿 y=70 → 卡片顶 y=82（12px 间距）；左右/底边保持 8px 外沿 */
  padding: 2px 8px 8px;
  gap: 0;
  overflow: hidden;
}
.left-pane {
  flex: none;
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  overflow: hidden;
}
.center-pane {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.right-pane {
  flex: none;
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  overflow: hidden;
}
.splitter {
  flex: none;
  width: var(--conversation-card-gap, 12px);
  position: relative;
  cursor: col-resize;
  touch-action: none;
}
.splitter::after {
  position: absolute;
  content: "";
  width: 2px;
  top: 42%;
  height: 40px;
  left: calc(50% - 1px);
  border-radius: var(--qio-ui-radius);
  background: transparent;
  transition: background var(--dur-fast) var(--ease-1);
}
.splitter:hover::after,
.splitter:focus-visible::after {
  background: var(--border-strong);
}
.splitter:active::after {
  background: var(--accent);
}
.splitter:focus-visible { outline: none; }
[data-dragging="left"] .center-pane,
[data-dragging="right"] .center-pane { user-select: none; }
[data-dragging] .splitter { cursor: col-resize; }

/* ---- 收起后的任务入口 ---- */
.collapsed-rail {
  flex: none;
  width: 48px;
  /* 收起后仍保留和其他卡片一样的 12px 槽位，避免左→中变成 0 */
  margin-right: var(--conversation-card-gap, 12px);
  display: flex;
  flex-direction: column;
  align-items: center;
  /* FU5：任务按钮就是收起栏本身 —— 不再在栏里再套一个小圆角按钮。
     栏与按钮用同一圈圆角、同一条边：按钮铺满整栏，圆角完全对齐。 */
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
}
.rail-btn {
  position: relative;
  width: 100%;
  flex: 1;
  padding: 14px 0;
  border: 0;
  border-radius: inherit;
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center; /* 图标 + 「任务」整组在收起栏里垂直居中 */
  gap: 5px;
}
.rail-btn svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.2;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.rail-btn:hover { color: var(--text-strong); background: var(--layer-hover); }
.rail-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
/* 竖排「任务」：竖排行盒给一个偶数宽度（line-height:16px）——在 46px 宽的按钮里
   从整数像素开始、左右对称留白，避免半像素盒宽在高 DPI 下看起来偏左/右；
   再对字体墨迹的轻微右侧偏重做 0.5px 光学补偿。 */
.rail-label {
  writing-mode: vertical-rl;
  letter-spacing: 0.14em;
  line-height: 16px;
  text-align: center;
  transform: translateX(-0.5px);
}
.rail-badge {
  position: absolute;
  /* 收起栏 overflow:hidden（保证按钮与栏圆角完全对齐），徽标改为贴内侧右上角 */
  top: 4px;
  right: 4px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  color: var(--text-secondary);
  font-size: 9.5px;
  line-height: 14px;
  text-align: center;
}
.rail-badge[data-tone="attention"] {
  color: var(--warning);
  border-color: var(--warning);
  background: var(--warning-soft);
}
.rail-badge[data-tone="active"] {
  color: var(--accent);
  border-color: var(--accent);
  background: var(--accent-soft);
}

/* ---- 全窗口预览 ---- */
.preview-overlay {
  position: absolute;
  /* 与话题/左栏同一条卡片顶线：y=82；左右/底边与全局栏同一 8px 外沿 */
  inset: 2px 8px 8px;
  z-index: 30;
  border-radius: var(--qio-ui-radius);
  overflow: hidden;
  box-shadow: var(--shadow-3);
}

/* ---- 任务抽屉（窄窗口临时展开，不写偏好） ---- */
.rail-drawer-layer {
  position: absolute;
  inset: 0;
  z-index: 35;
  display: flex;
}
.rail-drawer-backdrop {
  position: absolute;
  inset: 0;
  background: var(--bg-overlay);
}
.rail-drawer {
  position: absolute;
  top: 6px;
  bottom: 0;
  left: 8px;
  max-width: calc(100vw - 24px);
  display: flex;
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-3);
  overflow: hidden;
  animation: drawer-in var(--dur-panel) var(--ease-1) both;
}
@keyframes drawer-in {
  from { transform: translateX(calc(-1 * var(--shift-panel))); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}

/* ---- 预览栏收起的贴边入口（中线偏上，避让右下角星球入口） ---- */
.preview-reopen {
  position: absolute;
  right: 0;
  top: 22%;
  z-index: 8;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  width: 26px;
  padding: 10px 0;
  border: 1px solid var(--border-subtle);
  border-right: 0;
  border-radius: var(--qio-ui-radius) 0 0 var(--r-sm);
  background: var(--bg-elevated);
  color: var(--text-muted);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.preview-reopen svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.preview-reopen-count { font-size: 9.5px; }
.preview-reopen:hover {
  color: var(--text-strong);
  border-color: var(--border-strong);
}
.preview-reopen:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
.preview-reopen .badge { font-size: 10px; color: var(--text-muted); }

/* ---- 未完成事项（恢复区） ---- */
.recovery-layer {
  position: absolute;
  inset: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.recovery-backdrop {
  position: absolute;
  inset: 0;
  background: var(--bg-overlay);
  backdrop-filter: blur(2px);
}
.recovery-panel {
  position: relative;
  width: min(680px, 100%);
  max-height: min(80vh, 720px);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-4) var(--sp-5);
  border: 1px solid var(--border-strong);
  border-radius: var(--qio-ui-radius);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-3);
  overflow-y: auto;
}
.recovery-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.recovery-head h2 {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-lg);
  font-weight: 500;
  color: var(--text-strong);
}
.recovery-hint {
  margin: 0 0 var(--sp-2);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--text-secondary);
}
</style>
