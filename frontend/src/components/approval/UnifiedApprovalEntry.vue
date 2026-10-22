<script setup lang="ts">
/**
 * 对话页统一审批入口（D）。
 *
 * 常驻在对话工作区顶部居中（与旧入口同一位置语义；旧入口只在其他页面出现）。
 * 只在「有真实待办且弹窗没显示」时出现；文字与数量都来自统一队列，不造假。
 */
import { computed } from "vue";
import { useUnifiedApprovalsStore } from "../../stores/unifiedApprovals";

const unified = useUnifiedApprovalsStore();

const toolCount = computed(() => unified.items.filter((item) => item.kind === "tool").length);
const knowledgeCount = computed(() =>
  unified.items.filter((item) => item.kind === "knowledge").length,
);
const parts = computed(() => {
  const out: string[] = [];
  if (toolCount.value) out.push("权限 " + toolCount.value);
  if (knowledgeCount.value) out.push("知识 " + knowledgeCount.value);
  return out.join(" · ");
});
const title = computed(() => {
  const stale = unified.knowledgeStatus === "error" ? "（知识待办数量可能不是最新）" : "";
  return "有 " + unified.count + " 项申请等待确认" + stale;
});

function open() {
  unified.open();
}
</script>

<template>
  <button
    v-if="unified.count > 0 && !unified.visible"
    class="unified-approval-entry"
    type="button"
    data-test="unified-approval-entry"
    :aria-label="title + '，打开审批窗口'"
    @mousedown.prevent
    @click="open"
  >
    <span class="mark" aria-hidden="true">!</span>
    <span class="text">有 {{ unified.count }} 项申请等待确认</span>
    <span v-if="parts" class="breakdown mono" aria-hidden="true">{{ parts }}</span>
  </button>
</template>

<style scoped>
/* 位置与旧 ApprovalEntry 一致：顶部居中、落在窗口标题条下方；不改动布局容器。 */
.unified-approval-entry {
  position: fixed;
  top: var(--qio-top-notes-offset, calc(var(--titlebar-height) + 12px));
  left: 50%;
  transform: translateX(-50%);
  z-index: 90;
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2, 8px);
  max-width: min(560px, calc(100vw - 32px));
  padding: 6px 14px;
  border-radius: var(--r-pill);
  border: 1px solid var(--warning);
  background: var(--bg-elevated);
  color: var(--text-strong);
  font-family: var(--sans);
  font-size: 12.5px;
  box-shadow: var(--shadow-2);
  cursor: pointer;
  transition:
    transform var(--dur-press) var(--ease-out),
    border-color var(--dur-fast) var(--ease);
}
.unified-approval-entry:hover {
  border-color: var(--accent);
}
.unified-approval-entry:active {
  transform: translateX(-50%) translateY(var(--shift-1, 1px));
}
.unified-approval-entry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--warning);
  color: var(--bg-base);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
}
.breakdown {
  color: var(--text-muted);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  white-space: nowrap;
}
</style>
