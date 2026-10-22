<script setup lang="ts">
/**
 * 对话页 v2 外壳（Lead 维护的接线文件；区域实现由 A/B/C/D 各自的组件承担）。
 *
 * 布局：
 *   顶部全局栏（WindowChrome，由 A 负责：品牌 / 设置 / 窗口按钮）
 *   ├─ 左侧任务栏（A）        ← 可拖宽、可收起；窄窗口临时收起
 *   ├─ 中央对话工作区（B）     ← 话题栏 / 消息流 / 五入口 + 输入区
 *   └─ 右侧只读预览（C）      ← 可拖宽、可收起、可全窗口
 *
 * 右下角记忆星球入口保持现有 PlanetDock（用户明确：样式与功能不改）。
 * 审批统一走 D 的 UnifiedApprovalLayer；旧的顶部居中入口只留给其他页面。
 */
import { defineAsyncComponent, onMounted, onUnmounted, ref, watch } from "vue";
import { useSessionStore } from "../stores/session";
import { useConversationShellStore } from "../stores/conversationShell";
import { clampLeftWidth, clampRightWidth, LEFT_MIN, RIGHT_MIN } from "../conversation/layoutPrefs";
import TaskRail from "../components/conversation/TaskRail.vue";
import ChatColumn from "../components/conversation/ChatColumn.vue";
import PreviewPanel from "../components/conversation/PreviewPanel.vue";
import UnifiedApprovalLayer from "../components/conversation/UnifiedApprovalLayer.vue";
import PlanetDock from "../components/PlanetDock.vue";
import PlanetBoot from "../components/PlanetBoot.vue";
import SettingsFloat from "../components/SettingsFloat.vue";

const PlanetView = defineAsyncComponent({
  loader: () => import("./PlanetView.vue"),
  loadingComponent: PlanetBoot,
  delay: 0,
});

const session = useSessionStore();
const shell = useConversationShellStore();

const planetOpen = ref(false);
const planetMounted = ref(false);
const planetSeq = ref(0);
let stopViewport: (() => void) | null = null;

// ── 三栏拖动 ───────────────────────────────────────────────────────────────

type DragKind = "left" | "right" | null;
const dragKind = ref<DragKind>(null);
let dragStartX = 0;
let dragStartWidth = 0;

function beginDrag(kind: Exclude<DragKind, null>, event: PointerEvent) {
  dragKind.value = kind;
  dragStartX = event.clientX;
  dragStartWidth = kind === "left" ? shell.prefs.leftWidth : shell.prefs.rightWidth;
  (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
  event.preventDefault();
}

function moveDrag(event: PointerEvent) {
  if (!dragKind.value) return;
  const delta = event.clientX - dragStartX;
  if (dragKind.value === "left") {
    shell.prefs.leftWidth = clampLeftWidth(dragStartWidth + delta);
    shell.prefs.leftCollapsed = false;
  } else if (dragKind.value === "right") {
    shell.prefs.rightWidth = clampRightWidth(dragStartWidth - delta, shell.viewportWidth, shell.leftWidth);
  }
}

function endDrag(event: PointerEvent) {
  if (!dragKind.value) return;
  const kind = dragKind.value;
  dragKind.value = null;
  (event.currentTarget as HTMLElement).releasePointerCapture?.(event.pointerId);
  // 拖动过程只改内存；松手才持久化（避免每移动一像素写一次服务端）。
  if (kind === "left") shell.setLeftWidth(shell.prefs.leftWidth);
  else shell.setRightWidth(shell.prefs.rightWidth);
}

function nudgeSplitter(kind: "left" | "right", event: KeyboardEvent) {
  const step = event.shiftKey ? 32 : 12;
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  event.preventDefault();
  const direction = event.key === "ArrowRight" ? 1 : -1;
  if (kind === "left") shell.setLeftWidth(shell.prefs.leftWidth + direction * step);
  else shell.setRightWidth(shell.prefs.rightWidth - direction * step);
}

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

// 全窗口预览时按 Esc 返回对话（C 的完整版也应支持；这里给外壳兜底）。
function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && shell.effectivePreviewFull) {
    shell.setPreviewFull(false);
  }
}

onMounted(() => {
  void shell.load();
  stopViewport = shell.installViewportTracking();
  session.loadHistory();
  mountPlanetWhenIdle();
  window.addEventListener("keydown", onKeydown);
});

onUnmounted(() => {
  stopViewport?.();
  stopViewport = null;
  window.removeEventListener("keydown", onKeydown);
});

// 从设置页返回时恢复阅读位置等由各区域组件各自负责；这里只确保标签栏状态一致。
watch(
  () => shell.prefs.previewTabs.length,
  (count) => {
    if (count === 0 && shell.prefs.previewFull) shell.setPreviewFull(false);
  },
);
</script>

<template>
  <div
    class="conversation-workspace"
    :class="{
      'left-collapsed': !shell.leftVisible,
      'preview-full': shell.effectivePreviewFull,
    }"
    :data-dragging="dragKind ?? undefined"
  >
    <div class="workspace-row">
      <TaskRail v-if="shell.leftVisible" class="left-pane" :style="{ width: shell.leftWidth + 'px' }" />

      <div
        v-if="shell.leftVisible"
        class="splitter"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整左侧任务栏宽度"
        :aria-valuemin="LEFT_MIN"
        :aria-valuemax="420"
        :aria-valuenow="shell.leftWidth"
        tabindex="0"
        @pointerdown="beginDrag('left', $event)"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @keydown="nudgeSplitter('left', $event)"
      ></div>

      <div v-else class="collapsed-rail" aria-label="任务栏已收起">
        <button
          class="rail-btn"
          type="button"
          title="展开任务栏"
          aria-label="展开任务栏"
          @click="shell.toggleLeft()"
        >
          <span class="rail-label">任务</span>
        </button>
      </div>

      <ChatColumn class="center-pane" />

      <div
        v-if="shell.rightVisible && !shell.effectivePreviewFull"
        class="splitter"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整文件预览宽度"
        :aria-valuemin="RIGHT_MIN"
        :aria-valuemax="760"
        :aria-valuenow="shell.rightWidth"
        tabindex="0"
        @pointerdown="beginDrag('right', $event)"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @keydown="nudgeSplitter('right', $event)"
      ></div>

      <PreviewPanel
        v-if="shell.rightVisible && !shell.effectivePreviewFull"
        class="right-pane"
        :style="{ width: shell.rightWidth + 'px' }"
        @close="shell.setRightOpen(false)"
        @toggle-full="shell.setPreviewFull(true)"
      />
    </div>

    <!-- 全窗口预览：覆盖工作区，返回后恢复原布局、标签与位置 -->
    <PreviewPanel
      v-if="shell.rightVisible && shell.effectivePreviewFull"
      class="preview-overlay"
      full
      @close="shell.setRightOpen(false)"
      @toggle-full="shell.setPreviewFull(false)"
    />

    <!-- 预览栏收起但还有标签：贴边小入口，点击恢复（不放回已失效的拖动设置） -->
    <button
      v-if="!shell.rightVisible && shell.prefs.previewTabs.length"
      class="preview-reopen"
      type="button"
      title="打开文件预览"
      aria-label="打开文件预览"
      @click="shell.setRightOpen(true)"
    >
      文件
      <span class="badge mono">{{ shell.prefs.previewTabs.length }}</span>
    </button>

    <UnifiedApprovalLayer />
    <SettingsFloat />
    <PlanetDock @open="openPlanet" />
    <PlanetView v-if="planetMounted" :seq="planetSeq" :open="planetOpen" @close="closePlanet" />
  </div>
</template>

<style scoped>
.conversation-workspace {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  background:
    radial-gradient(ellipse at 0% 0%, var(--accent-soft) 0, transparent 46%),
    var(--bg-base);
  color: var(--text-primary);
  font-family: var(--sans);
  animation: workspace-in var(--dur-page-out) var(--ease-out) both;
}
@keyframes workspace-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.workspace-row {
  display: flex;
  flex: 1;
  min-height: 0;
  min-width: 0;
  padding: 6px 8px 0 8px;
  gap: 0;
}
.left-pane {
  flex: none;
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  overflow: hidden;
}
.center-pane {
  flex: 1;
  min-width: 0;
  min-height: 0;
}
.right-pane {
  flex: none;
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  overflow: hidden;
}
.splitter {
  flex: none;
  width: 10px;
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
  left: 4px;
  border-radius: 5px;
  background: transparent;
  transition: background var(--dur-fast) var(--ease-1);
}
.splitter:hover::after,
.splitter:focus-visible::after {
  background: var(--border-strong);
}
.splitter:focus-visible { outline: none; }
[data-dragging="left"] .center-pane,
[data-dragging="right"] .center-pane { user-select: none; }
.collapsed-rail {
  flex: none;
  width: 48px;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding-top: 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-lg);
  background: var(--bg-surface);
}
.rail-btn {
  width: 36px;
  padding: 7px 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
}
.rail-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.rail-btn:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.rail-label { writing-mode: vertical-rl; letter-spacing: 0.12em; }
.preview-overlay {
  position: absolute;
  inset: 6px 8px 0;
  z-index: 30;
  box-shadow: var(--shadow-3);
}
.preview-reopen {
  position: absolute;
  right: 12px;
  top: 12px;
  z-index: 8;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-pill);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.preview-reopen:hover { color: var(--text-strong); border-color: var(--accent); }
.preview-reopen:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.preview-reopen .badge { font-size: 10px; color: var(--text-muted); }
</style>
