<script setup lang="ts">
/**
 * 输入区与上方五个图标入口（B 负责完整实现；这里先接回现有 Composer，
 * 保证基线发送/停止/附件/队列等能力立刻可用）。
 */
import Composer from "../Composer.vue";
</script>

<template>
  <div class="composer-dock">
    <div class="entry-row" aria-label="输入区快捷入口"></div>
    <Composer />
  </div>
</template>

<style scoped>
.composer-dock {
  flex: none;
  min-width: 0;
  position: relative;
  z-index: 2;
}
.entry-row {
  display: flex;
  justify-content: center;
  gap: 6px;
  min-height: 0;
  padding: 2px 0 4px;
}
</style>
