<!-- 板面区域（子智能体 A 负责：板面、卡片、选择、拖动、分组、顺序、关系）。 -->
<script setup lang="ts">
import { useInteractiveStore } from "../../stores/interactive";

const store = useInteractiveStore();
</script>

<template>
  <section class="board-placeholder">
    <p class="hint">板面区域：卡片、分组、关系、拖动与选择在这里。</p>
    <p class="hint mono">
      当前有 {{ store.board?.cards.filter((c) => !c.deleted).length ?? 0 }} 张卡片，
      保存序号 {{ store.board?.seq ?? 0 }}。
    </p>
    <p class="hint">（板面渲染由子智能体 A 接管；此处为骨架占位。）</p>
  </section>
</template>

<style scoped>
.board-placeholder {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  align-items: center;
  justify-content: center;
  padding: var(--sp-6);
  color: var(--text-muted);
  text-align: center;
}
.hint { margin: 0; font-size: var(--fs-sm); }
</style>
