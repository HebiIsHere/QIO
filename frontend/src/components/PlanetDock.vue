<script setup lang="ts">
/**
 * 悬浮球（v1 占位）：星球 3D 场景集成在下一阶段。
 * v1 形态：圆形按钮 + 当前话题状态点。
 */
import { useSessionStore } from "../stores/session";

const session = useSessionStore();
const emit = defineEmits<{ open: [] }>();
</script>

<template>
  <button class="dock" @click="emit('open')" title="话题星球">
    <svg viewBox="0 0 40 40" width="34" height="34">
      <circle cx="20" cy="20" r="14" fill="#2b1226" stroke="#8a2f6b" stroke-width="1.5" />
      <circle cx="16" cy="17" r="3.2" fill="#dd3399" />
      <circle cx="24" cy="15" r="2.2" fill="#48c78e" />
      <circle cx="22" cy="24" r="2.6" fill="#e0a06a" />
      <circle cx="13" cy="24" r="2" fill="#b07ae0" />
    </svg>
    <span class="activity" :class="{ on: session.turnRunning }"></span>
  </button>
</template>

<style scoped>
.dock {
  position: fixed; right: 18px; bottom: 110px;
  width: 52px; height: 52px; border-radius: 50%;
  background: var(--bg-accent-subtle); border: 1px solid var(--border-strong);
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
  transition: transform 0.15s ease;
}
.dock:hover { transform: scale(1.06); }
.activity {
  position: absolute; top: 4px; right: 4px; width: 9px; height: 9px;
  border-radius: 50%; background: var(--text-muted); border: 2px solid var(--bg-accent-subtle);
}
.activity.on { background: var(--warning); }
</style>