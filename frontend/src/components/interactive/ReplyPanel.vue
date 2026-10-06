<!-- QIO 回复区与任务列表（子智能体 C 负责：虚线预览、单项 / 批量审批、进度与影响说明）。 -->
<script setup lang="ts">
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();
</script>

<template>
  <section class="reply-placeholder">
    <h2 class="title">QIO 回复与任务</h2>
    <p class="hint">
      待审批 {{ store.pendingIntents.length }} 项 · 执行中 {{ store.activeIntents.length }} 项 ·
      已结束 {{ store.settledIntents.length }} 项
    </p>
    <p class="hint">（回复区、虚线预览与审批入口由子智能体 C 接管；此处为骨架占位。）</p>
  </section>
</template>

<style scoped>
.reply-placeholder {
  padding: var(--sp-4) var(--sp-5);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.hint { margin: 0; font-size: var(--fs-sm); color: var(--text-muted); }
</style>
