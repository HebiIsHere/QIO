<!-- 顶部「任务」浮层（子智能体 D 负责实现；这里是骨架占位）。 -->
<script setup lang="ts">
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();
</script>

<template>
  <div v-if="store.tasksOpen" class="tasks-pop-placeholder" data-im="tasks-popover">
    <p class="hint">执行中 {{ store.runningIntents.length }} 项 · 已结束 {{ store.finishedIntents.length }} 项</p>
    <p class="hint">（由子智能体 D 接管；此处为骨架占位。）</p>
  </div>
</template>

<style scoped>
/* 放左上：右上留给批量列表，右下留给聊天，避免浮层互相遮挡 */
.tasks-pop-placeholder {
  position: absolute;
  left: var(--sp-4);
  top: var(--sp-3);
  z-index: 26;
  width: min(340px, 88vw);
  padding: var(--sp-3);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
}
.hint { margin: 0 0 var(--sp-1); font-size: var(--fs-sm); color: var(--text-secondary); }
</style>
