<script setup lang="ts">
import { useRouter } from "vue-router";

const router = useRouter();
function openSettings() {
  void router.push({ name: "settings" });
}
</script>

<template>
  <!-- 设置入口：右上角浮动 ⚙（浮动窗口体系见 Task B，本阶段仅「按钮 + 打开整页设置」） -->
  <button
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
  right: 26px;
  top: 18px;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  user-select: none;
  z-index: 20;
  transition: border-color 0.18s, color 0.18s, background 0.18s;
}
.settings-float:hover {
  border-color: var(--border-strong);
  color: var(--text-strong);
  background: var(--bg-elevated);
}
</style>
