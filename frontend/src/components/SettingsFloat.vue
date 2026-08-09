<script setup lang="ts">
/**
 * 设置入口：右上角浮动 ⚙（浮动窗口体系 Task B，贴角模式）；
 * 点击打开整页设置（保留事件，拖动不误触发）。
 */
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useFloatingWindow } from "../composables/useFloatingWindow";

const router = useRouter();
const elRef = ref<HTMLElement | null>(null);

const float = useFloatingWindow(elRef, {
  id: "settings-float",
  dockMode: "corner",
  defaultPos: (el, vp) => ({
    x: vp.width - 26 - (el.offsetWidth || 44),
    y: 18,
  }),
});

function openSettings() {
  if (float.moved.value) return; // 拖动不误触发
  void router.push({ name: "settings" });
}
</script>

<template>
  <button
    ref="elRef"
    class="settings-float"
    type="button"
    @click="openSettings"
    title="设置"
    aria-label="打开设置"
  >⚙</button>
</template>

<style scoped>
.settings-float {
  position: fixed;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  font-size: 18px;
  line-height: 1;
  cursor: grab;
  display: flex;
  align-items: center;
  justify-content: center;
  user-select: none;
  -webkit-user-select: none;
  -webkit-user-drag: none;
  touch-action: none;
  z-index: 20;
  transition: border-color 0.18s, color 0.18s, background 0.18s, opacity 0.3s;
}
.settings-float:active {
  cursor: grabbing;
}
.settings-float:hover {
  border-color: var(--border-strong);
  color: var(--text-strong);
  background: var(--bg-elevated);
}
/* 贴靠隐藏：淡化（mouseenter 展开） */
.settings-float.fw-hidden {
  opacity: 0.15;
}
</style>
