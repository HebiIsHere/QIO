<script setup lang="ts">
/**
 * ResizeHandles —— 卡片 / 对话窗口共用的尺寸调整标记（互动模式契约 2026-10-13 · IRL §2）
 *
 * 纯展示组件：只把「按了哪个方向」和「按了 −/＋」发出去，不认识板面状态、不写任何存储。
 *
 * 分工（任务书 §3.2）：
 * - 四条边 + 四个角 = 指针拖动；角比边更好点；
 * - 右下角一个克制的标记，说明「这里可以拉」；
 * - − / ＋ 两对按钮（宽 / 高）永远在无障碍树里，键盘 Tab 可达，鼠标下淡出（`opacity`）。
 *
 * 为什么不用 `visibility: hidden` 藏按钮：那会把它们从 Tab 顺序里摘掉，
 * 只剩鼠标能用 —— 任务书要的是「可通过键盘操作」。
 */
import { RESIZE_HANDLES, type ResizeHandle } from "../../interactive/resize";

const props = withDefaults(
  defineProps<{
    /** 视觉强调：常态下一个很淡的角标，hover / 选中时更清楚 */
    visible?: boolean;
    /** 右下角标记 */
    showMark?: boolean;
    /** − / ＋ 按钮 */
    showKeys?: boolean;
    /** 无障碍名称前缀 */
    label?: string;
    /** 不可交互（例如只读预览） */
    disabled?: boolean;
    handles?: readonly ResizeHandle[];
  }>(),
  {
    visible: true,
    showMark: true,
    showKeys: true,
    label: "尺寸调整",
    disabled: false,
    handles: () => RESIZE_HANDLES,
  },
);

const emit = defineEmits<{
  (e: "handle-down", handle: ResizeHandle, event: PointerEvent): void;
  (e: "nudge", axis: "w" | "h", direction: 1 | -1): void;
}>();

function onDown(handle: ResizeHandle, event: PointerEvent): void {
  if (props.disabled) return;
  emit("handle-down", handle, event);
}

/** 四条边的热区拆成两段（中间 24px 缺口留给关系连接点）；四个角仍是整块。 */
const EDGE_HANDLES: readonly ResizeHandle[] = ["n", "s", "e", "w"];
function isEdgeHandle(handle: ResizeHandle): boolean {
  return EDGE_HANDLES.includes(handle);
}
</script>

<template>
  <!--
    pointerdown 在本层就止住：同一个指针操作不许既开始尺寸调整、又触发宿主的卡片拖动 / 板面平移 /
    对话窗口拖动（实际接管的 useResizeGesture 也会 stopPropagation，这里再兜一层，键盘按钮同样适用）。
  -->
  <div
    class="rz-root"
    :class="{ 'rz-quiet': !props.visible, 'rz-off': props.disabled }"
    data-resize-root="1"
    @pointerdown.stop
  >
    <template v-if="!props.disabled">
      <span
        v-for="handle in props.handles"
        :key="handle"
        class="rz-hit"
        :class="'rz-hit-' + handle"
        :data-resize-handle="handle"
        :aria-hidden="true"
        @pointerdown="onDown(handle, $event)"
      >
        <!--
          边的热区拆成两段：中间 24px 缺口让给关系连接点（用户决策 4）。
          外层仍只有 8 个 [data-resize-handle]（契约/用例口径不变），真实命中由两段子热区承担。
        -->
        <template v-if="isEdgeHandle(handle)">
          <span class="rz-seg" :class="'rz-seg-' + handle + '-a'" data-resize-segment="a" />
          <span class="rz-seg" :class="'rz-seg-' + handle + '-b'" data-resize-segment="b" />
        </template>
      </span>
    </template>
    <span v-if="props.showMark && !props.disabled" class="rz-mark" aria-hidden="true" />
    <div
      v-if="props.showKeys && !props.disabled"
      class="rz-keys"
      role="group"
      :aria-label="props.label"
      data-resize-keys="1"
    >
      <span class="rz-axis" aria-hidden="true">宽</span>
      <button
        type="button"
        class="rz-key"
        data-resize-key="w-"
        :aria-label="props.label + '：减小宽度'"
        @click="emit('nudge', 'w', -1)"
      >
        −
      </button>
      <button
        type="button"
        class="rz-key"
        data-resize-key="w+"
        :aria-label="props.label + '：增大宽度'"
        @click="emit('nudge', 'w', 1)"
      >
        ＋
      </button>
      <span class="rz-axis" aria-hidden="true">高</span>
      <button
        type="button"
        class="rz-key"
        data-resize-key="h-"
        :aria-label="props.label + '：减小高度'"
        @click="emit('nudge', 'h', -1)"
      >
        −
      </button>
      <button
        type="button"
        class="rz-key"
        data-resize-key="h+"
        :aria-label="props.label + '：增大高度'"
        @click="emit('nudge', 'h', 1)"
      >
        ＋
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 覆盖在宿主元素之上：宿主必须是 position: relative / absolute。 */
.rz-root {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 3;
}

/* --- 指针热区：边 10px、角 16px -------------------------------------------
   用户决策 4：边中点让出 24px 缺口给关系连接点 —— 边的热区拆成两段子热区，
   尺寸调整从边中点两侧拖；昆点本体固定在卡片边框中点（见 BoardResizeLayer）。
   外层 .rz-hit 只作定位容器（pointer-events: none），真实命中在 .rz-seg 上。 */

.rz-hit {
  position: absolute;
  pointer-events: none;
  touch-action: none;
}

.rz-hit-n,
.rz-hit-s {
  left: 0;
  right: 0;
  height: 10px;
  cursor: ns-resize;
}
.rz-hit-n {
  top: 0;
}
.rz-hit-s {
  bottom: 0;
}

.rz-hit-e,
.rz-hit-w {
  top: 0;
  bottom: 0;
  width: 10px;
  cursor: ew-resize;
}
.rz-hit-e {
  right: 0;
}
.rz-hit-w {
  left: 0;
}

/* 两段子热区：中间各让出 12px（合起来 24px 缺口给连接点的 18px 命中区）。 */
.rz-seg {
  position: absolute;
  pointer-events: auto;
  touch-action: none;
}
.rz-seg-n-a,
.rz-seg-s-a {
  left: 0;
  width: calc(50% - 12px);
  height: 100%;
}
.rz-seg-n-b,
.rz-seg-s-b {
  left: calc(50% + 12px);
  right: 0;
  height: 100%;
}
.rz-seg-e-a,
.rz-seg-w-a {
  top: 0;
  height: calc(50% - 12px);
  width: 100%;
}
.rz-seg-e-b,
.rz-seg-w-b {
  top: calc(50% + 12px);
  bottom: 0;
  width: 100%;
}

.rz-hit-ne,
.rz-hit-nw,
.rz-hit-se,
.rz-hit-sw {
  width: 16px;
  height: 16px;
  pointer-events: auto;
}
.rz-hit-ne {
  top: 0;
  right: 0;
  cursor: nesw-resize;
}
.rz-hit-nw {
  top: 0;
  left: 0;
  cursor: nwse-resize;
}
.rz-hit-se {
  bottom: 0;
  right: 0;
  cursor: nwse-resize;
}
.rz-hit-sw {
  bottom: 0;
  left: 0;
  cursor: nesw-resize;
}

.rz-off .rz-hit,
.rz-off .rz-seg {
  pointer-events: none;
}

/* --- 右下角标记 ------------------------------------------------------------ */

.rz-mark {
  position: absolute;
  right: 3px;
  bottom: 3px;
  width: 10px;
  height: 10px;
  pointer-events: none;
  opacity: 0.4;
  background:
    linear-gradient(135deg, transparent 0 46%, var(--text-faint) 46% 58%, transparent 58%),
    linear-gradient(135deg, transparent 0 74%, var(--text-faint) 74% 86%, transparent 86%);
  transition: opacity var(--dur-toggle) var(--ease-1);
}

.rz-root:hover .rz-mark,
.rz-root:focus-within .rz-mark {
  opacity: 0.85;
}

/*
 * visible=false：只降**视觉强调**，不摘掉键盘可达性（按钮仍在无障碍树里）。
 * 卡片密集 / 只读浏览时用这一档，让空间看起来干净，但功能一个不少。
 */
.rz-quiet .rz-mark {
  opacity: 0.12;
}

.rz-quiet:hover .rz-mark,
.rz-quiet:focus-within .rz-mark {
  opacity: 0.6;
}

/* --- −/＋ 键盘按钮 --------------------------------------------------------- */

.rz-keys {
  position: absolute;
  /* 让开东 / 南两侧各 10px 的指针热区：按钮面板压在热区上会让「拉右边」变难点。 */
  right: 14px;
  bottom: 18px;
  display: grid;
  grid-template-columns: auto auto auto;
  gap: 2px var(--sp-1);
  align-items: center;
  padding: 2px var(--sp-1);
  border-radius: 8px;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  opacity: 0;
  transition: opacity var(--dur-toggle) var(--ease-1);
  pointer-events: none;
}

/* 键盘 Tab 进来 / 鼠标悬停时才出现；出现前不可点，避免挡住正文。 */
.rz-root:hover .rz-keys,
.rz-root:focus-within .rz-keys {
  opacity: 1;
  pointer-events: auto;
}

.rz-axis {
  font-size: var(--fs-tech);
  color: var(--text-muted);
  font-family: var(--mono);
}

.rz-key {
  min-width: 20px;
  height: 20px;
  padding: 0;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-elevated);
  color: var(--text-primary);
  font-size: var(--fs-sm);
  line-height: 1;
  cursor: pointer;
  pointer-events: auto;
}

.rz-key:hover {
  border-color: var(--border-strong);
  color: var(--text-strong);
}

.rz-key:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

/* 减少动画：不做淡入淡出，直接显示。 */
@media (prefers-reduced-motion: reduce) {
  .rz-mark,
  .rz-keys {
    transition: none;
  }
}
</style>
