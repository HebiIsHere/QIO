<script setup lang="ts">
/**
 * 悬浮球：SVG 微缩星球（透明球径向渐变 + 玫红轮廓 + 三圈融合环 + 圆点），
 * 浮动窗口（贴边，默认右侧居中）；点击展开全屏星球（保留事件，拖动不误触发）。
 */
import { ref } from "vue";
import { useSessionStore } from "../stores/session";
import { useFloatingWindow } from "../composables/useFloatingWindow";

const session = useSessionStore();
const emit = defineEmits<{ open: [] }>();
const elRef = ref<HTMLElement | null>(null);

const float = useFloatingWindow(elRef, {
  id: "planet-dock",
  dockMode: "edge",
  defaultPos: (el, vp) => ({
    x: vp.width - 26 - (el.offsetWidth || 96),
    y: Math.round((vp.height - (el.offsetHeight || 96)) / 2),
  }),
});

function onClick() {
  if (float.moved.value) return; // 拖动不误触发
  emit("open");
}
</script>

<template>
  <button ref="elRef" class="dock" @click="onClick" title="话题星球" aria-label="打开话题星球">
    <svg class="globe" viewBox="0 0 96 96" width="84" height="84" aria-hidden="true">
      <defs>
        <radialGradient id="qio-dock-g" cx="50%" cy="38%" r="65%">
          <stop class="g0" offset="0%" />
          <stop class="g1" offset="55%" />
          <stop class="g2" offset="100%" />
        </radialGradient>
      </defs>
      <circle class="globe-rim" cx="48" cy="48" r="38" fill="url(#qio-dock-g)" />
      <ellipse class="ring thin" cx="48" cy="48" rx="30" ry="26" />
      <ellipse class="ring mid" cx="48" cy="48" rx="22" ry="18" />
      <ellipse class="ring thick" cx="48" cy="48" rx="14" ry="10" />
      <circle class="dotp" cx="48" cy="38" r="2.6" />
      <circle class="dotp" cx="58" cy="50" r="2" />
      <circle class="dotp" cx="42" cy="56" r="1.6" />
    </svg>
    <span class="lbl mono">话题星球</span>
    <span class="activity" :class="{ on: session.turnRunning }"></span>
  </button>
</template>

<style scoped>
/* 浮动窗口（Task B）：定位由 useFloatingWindow 用 left/top 像素控制，整元素可拖 */
.dock {
  position: fixed;
  z-index: 11;
  width: 96px;
  height: 96px;
  padding: 0;
  background: transparent;
  border: none;
  cursor: grab;
  user-select: none;
  -webkit-user-select: none;
  -webkit-user-drag: none;
  touch-action: none;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.2s ease, opacity 0.3s ease;
}
.dock:active {
  cursor: grabbing;
}
.dock:hover {
  transform: scale(1.06);
}
/* 贴靠隐藏：淡化（mouseenter 展开） */
.dock.fw-hidden {
  opacity: 0.08;
}
.g0 {
  stop-color: var(--accent-soft);
}
.g1 {
  stop-color: var(--bg-elevated);
  stop-opacity: 0.5;
}
.g2 {
  stop-color: var(--bg-base);
  stop-opacity: 0;
}
.globe-rim {
  stroke: var(--link);
  stroke-opacity: 0.85;
  stroke-width: 1.6;
}
.globe .ring {
  fill: none;
  stroke: var(--link);
  stroke-opacity: 0.5;
}
.globe .ring.thin {
  stroke-width: 2;
}
.globe .ring.mid {
  stroke-width: 3.5;
  stroke-opacity: 0.35;
}
.globe .ring.thick {
  stroke-width: 6;
  stroke-opacity: 0.2;
}
.globe .dotp {
  fill: var(--accent);
}
.lbl {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  bottom: -6px;
  font-size: 10px;
  color: var(--text-muted);
  white-space: nowrap;
  pointer-events: none;
}
.activity {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--text-muted);
  border: 2px solid var(--bg-base);
  pointer-events: none;
}
.activity.on {
  background: var(--warning);
  box-shadow: 0 0 6px var(--warning);
}
</style>
