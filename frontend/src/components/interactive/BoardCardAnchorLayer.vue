<!--
  卡片端点锚点层（只负责视觉，不改任何板面状态）。

  设计来源：docs/interactive-v2/qio-card-interaction-demo.html 的卡片端口。
  - 组作为**真实关系端点**时，只画出已经被关系使用的边缘锚点；没被用到的边不出现，
    也不常驻四个端口。
  - 静止：空心圆点（线正好落在圆点中心）；卡片悬停 / 选中：圆点变短条；
    锚点自身悬停：向卡片边框**外侧**鼓出圆角半圆，贴住卡片边框边缘、不伸进卡片内。
  - 锚点坐标与 BoardLinkLayer 的线端点完全同源（linkEndpoints.groupEdgeAnchors），
    所以线、点、命中区始终对齐；曲线与 curve 字段不参与这里的计算。
  - 本层在**未缩放**的滚动内容层里（与 BoardResizeLayer 同一套定位），
    位置 = 板面坐标 × scale：缩到 25% 时锚点仍是可点中的真实屏幕像素。
-->

<script setup lang="ts">
import { computed } from "vue";
import type { CardAnchor, GroupAnchorSide } from "../../interactive/linkEndpoints";

const props = withDefaults(
  defineProps<{
    /** 只包含被关系实际使用的组边缘锚点（空数组 = 一个都不画）。 */
    anchors: CardAnchor[];
    /** 画布缩放：屏幕 px = 板面坐标 × scale。 */
    scale: number;
    /** 选中的组：锚点从空心圆点变短条（纯显示）。 */
    selectedCardIds?: string[];
    /** 指针悬停的组：锚点从空心圆点变短条（纯显示）。 */
    hoveredCardId?: string | null;
  }>(),
  { selectedCardIds: () => [], hoveredCardId: null },
);

const emit = defineEmits<{
  (e: "anchor-down", anchor: CardAnchor, event: PointerEvent): void;
}>();

const safeScale = computed(() => (Number.isFinite(props.scale) && props.scale > 0 ? props.scale : 1));

const selected = computed(() => new Set(props.selectedCardIds));

/** 锚点宿主的位置：精确落在卡片边框边缘的交点上（中心对齐）。 */
function anchorStyle(anchor: CardAnchor): Record<string, string> {
  return {
    left: anchor.x * safeScale.value + "px",
    top: anchor.y * safeScale.value + "px",
  };
}

function anchorKey(anchor: CardAnchor): string {
  return [anchor.cardId, anchor.side, anchor.x.toFixed(3), anchor.y.toFixed(3)].join(":");
}

/** 卡片悬停 / 选中时显示短条；锚点自身悬停时由 CSS 让位给外侧半圆。 */
function isActive(anchor: CardAnchor): boolean {
  return selected.value.has(anchor.cardId) || props.hoveredCardId === anchor.cardId;
}

const SIDE_LABELS: Record<GroupAnchorSide, string> = {
  top: "上边",
  right: "右边",
  bottom: "下边",
  left: "左边",
};

function titleOf(anchor: CardAnchor): string {
  return "从卡片" + SIDE_LABELS[anchor.side] + "锚点拖线建立关系";
}

/** 外侧圆角半圆：与演示稿的四向 path 同一形状；基座贴边，向外鼓出。 */
const HALF_PORTS: Record<GroupAnchorSide, { viewBox: string; d: string }> = {
  right: { viewBox: "0 0 14 20", d: "M 0 4 Q 0 0 4 0 A 10 10 0 0 1 4 20 Q 0 20 0 16 Z" },
  left: { viewBox: "0 0 14 20", d: "M 14 4 Q 14 0 10 0 A 10 10 0 0 0 10 20 Q 14 20 14 16 Z" },
  top: { viewBox: "0 0 20 14", d: "M 4 14 Q 0 14 0 10 A 10 10 0 0 1 20 10 Q 20 14 16 14 Z" },
  bottom: { viewBox: "0 0 20 14", d: "M 4 0 Q 0 0 0 4 A 10 10 0 0 0 20 4 Q 20 0 16 0 Z" },
};

function shapeOf(side: GroupAnchorSide): { viewBox: string; d: string } {
  return HALF_PORTS[side];
}

function onAnchorDown(anchor: CardAnchor, event: PointerEvent): void {
  if (event.button !== 0 && event.pointerType === "mouse") return;
  event.preventDefault();
  event.stopPropagation();
  emit("anchor-down", anchor, event);
}
</script>

<template>
  <div class="card-anchor-layer" data-im="card-anchor-layer">
    <span
      v-for="anchor in anchors"
      :key="anchorKey(anchor)"
      class="card-anchor"
      :class="{ 'is-active': isActive(anchor) }"
      :style="anchorStyle(anchor)"
      data-im="card-anchor"
      :data-card-id="anchor.cardId"
      :data-endpoint="anchor.endpoint"
      :data-side="anchor.side"
      :data-link-ids="anchor.linkIds.join(',')"
      :data-active="isActive(anchor) ? 'true' : 'false'"
      role="button"
      tabindex="0"
      :title="titleOf(anchor)"
      :aria-label="titleOf(anchor)"
      @pointerdown="onAnchorDown(anchor, $event)"
    >
      <svg class="card-anchor-shape" :viewBox="shapeOf(anchor.side).viewBox" aria-hidden="true">
        <path :d="shapeOf(anchor.side).d" />
      </svg>
    </span>
  </div>
</template>

<style scoped>
/*
  整层只定位，不接管指针；只有锚点自己的 34×34 命中区接管。
  z-index 低于 BoardResizeLayer（45）：同位置的卡片连接点更具体，永远优先。
*/
.card-anchor-layer {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
  pointer-events: none;
  z-index: 44;
}

/*
  锚点宿主：中心对准卡片边框边缘上的线端点。
  静止态 = 空心圆点（7px，1.5px 描边，底色用纸面色）。
*/
.card-anchor {
  position: absolute;
  width: 34px;
  height: 34px;
  margin: -17px 0 0 -17px;
  padding: 0;
  border: 0;
  background: none;
  pointer-events: auto;
  touch-action: none;
  cursor: crosshair;
  color: var(--accent);
}
.card-anchor::before {
  content: "";
  position: absolute;
  left: 50%;
  top: 50%;
  width: 7px;
  height: 7px;
  transform: translate(-50%, -50%);
  box-sizing: border-box;
  border: 1.5px solid var(--accent);
  border-radius: 50%;
  background: var(--bg-surface);
  box-shadow: var(--shadow-1);
  opacity: 1;
  transition: opacity var(--dur-fast) var(--ease-1);
}

/* 卡片悬停 / 选中：空心圆点让位给短条（横边 16×6，竖边 6×16）。 */
.card-anchor::after {
  content: "";
  position: absolute;
  left: 50%;
  top: 50%;
  width: 6px;
  height: 16px;
  transform: translate(-50%, -50%);
  border-radius: 8px;
  background: var(--accent);
  box-shadow: var(--shadow-1);
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease-1);
}
.card-anchor[data-side="top"]::after,
.card-anchor[data-side="bottom"]::after {
  width: 16px;
  height: 6px;
}
.card-anchor.is-active::before { opacity: 0; }
.card-anchor.is-active::after { opacity: 0.72; }

/*
  锚点悬停：圆点 / 短条都收起，从贴着卡片边框的那条边向外鼓出圆角半圆。
  半圆的基座固定在锚点所在的边缘，只向外长，不伸进卡片边框。
*/
.card-anchor-shape {
  position: absolute;
  opacity: 0;
  pointer-events: none;
  overflow: visible;
  transition:
    opacity var(--dur-fast) var(--ease-1),
    transform var(--dur-fast) var(--ease-1);
}
.card-anchor[data-side="right"] .card-anchor-shape {
  left: 17px;
  top: 7px;
  width: 14px;
  height: 20px;
  transform: scale(0.62);
  transform-origin: left center;
}
.card-anchor[data-side="left"] .card-anchor-shape {
  right: 17px;
  top: 7px;
  width: 14px;
  height: 20px;
  transform: scale(0.62);
  transform-origin: right center;
}
.card-anchor[data-side="top"] .card-anchor-shape {
  left: 7px;
  bottom: 17px;
  width: 20px;
  height: 14px;
  transform: scale(0.62);
  transform-origin: center bottom;
}
.card-anchor[data-side="bottom"] .card-anchor-shape {
  left: 7px;
  top: 17px;
  width: 20px;
  height: 14px;
  transform: scale(0.62);
  transform-origin: center top;
}
.card-anchor-shape path { fill: var(--accent); }
.card-anchor:hover::before,
.card-anchor:hover::after { opacity: 0; }
.card-anchor:hover .card-anchor-shape,
.card-anchor:focus-visible .card-anchor-shape {
  opacity: 1;
  transform: scale(1);
}
.card-anchor:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: 50%;
}
</style>
