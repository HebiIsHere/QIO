<script setup lang="ts">
/**
 * 全局异常入口（B）：放在顶部全局栏里（D 在 WindowChrome 挂一行），
 * 一个入口显示独立问题数量；没有问题时整块不渲染（不占位、不装装饰）。
 *
 * - 同一根因已由 stores/globalIssues 去重，这里只显示数量；
 * - 图标 + 文字 + 数量：不是「只有图标却讲不清用途」的按钮，可访问名称完整；
 * - 点击展开问题列表（GlobalIssuePanel 是同一个 store 的另一个组件）；
 * - 有中央弹窗时不抢层：面板自己会让位（见 GlobalIssuePanel）。
 */
import { computed } from "vue";
import { useGlobalIssuesStore } from "../../stores/globalIssues";

const issues = useGlobalIssuesStore();
const label = computed(
  () => issues.summary + "，打开问题列表（每个问题都有原因与处理方式）",
);

function toggle(): void {
  issues.togglePanel();
}
</script>

<template>
  <button
    v-if="issues.hasIssues"
    class="uf-issue-entry"
    :class="{ expanded: issues.panelOpen, error: issues.errorCount > 0 }"
    type="button"
    data-uf-issue-entry
    :aria-label="label"
    :aria-expanded="issues.panelOpen ? 'true' : 'false'"
    @mousedown.prevent
    @click="toggle"
  >
    <span class="uf-issue-entry__mark" aria-hidden="true">
      <svg viewBox="0 0 24 24">
        <path d="M12 8v5" />
        <path d="M12 16.4v.2" />
        <path d="M12 3.6 21 20H3z" />
      </svg>
    </span>
    <span class="uf-issue-entry__text">问题</span>
    <span class="uf-issue-entry__count mono">{{ issues.count }}</span>
  </button>
</template>

<style scoped>
.uf-issue-entry {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 9px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: transparent;
  color: var(--text-secondary);
  font-family: var(--sans);
  font-size: 11.5px;
  line-height: 1;
  cursor: pointer;
  pointer-events: auto;
  transition:
    color var(--dur-fast) var(--ease-1),
    border-color var(--dur-fast) var(--ease-1),
    background var(--dur-fast) var(--ease-1);
}
.uf-issue-entry:hover,
.uf-issue-entry.expanded {
  color: var(--text-strong);
  border-color: var(--border-strong);
  background: var(--layer-hover);
}
.uf-issue-entry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.uf-issue-entry__mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  color: var(--warning);
}
.uf-issue-entry.error .uf-issue-entry__mark {
  color: var(--danger);
}
.uf-issue-entry__mark svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.uf-issue-entry__text {
  white-space: nowrap;
}
.uf-issue-entry__count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 17px;
  height: 17px;
  padding: 0 4px;
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--warning-soft, var(--bg-inset));
  color: var(--text-strong);
  font-size: 10.5px;
}
.uf-issue-entry.error .uf-issue-entry__count {
  background: var(--danger-soft);
  color: var(--danger);
}
@media (max-width: 420px) {
  .uf-issue-entry__text {
    display: none;
  }
}
</style>
