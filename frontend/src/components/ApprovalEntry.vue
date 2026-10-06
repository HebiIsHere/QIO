<script setup lang="ts">
/**
 * 「有 N 项操作等待确认」入口。
 *
 * 为什么需要它：后台任务请求确认时，如果无条件弹出审批窗口，正在编辑表单的用户
 * 会被抢走焦点（历史录像：工具确认盖住凭据编辑）。现在窗口只在用户没在输入时自动
 * 出现，其余情况（或用户按 Esc /「稍后处理」收起后）由这个常驻入口把待办亮出来，
 * 用户主动点开——既不打断，也不会出现「看不见的待审批项」。
 */
import { useApprovalsStore } from "../stores/approvals";
import { useSessionStore } from "../stores/session";
import { computed } from "vue";

const approvals = useApprovalsStore();
const session = useSessionStore();

/**
 * 上一次进程结束时没回答完的那次操作：**不会再恢复等待**，所以这里不是待办、
 * 也没有可点的「继续」——只是一句事实，免得用户以为那件事做过了。
 */
const interrupted = computed(() => session.interruptedOperations);
const interruptedHint = computed(() => {
  const items = interrupted.value;
  if (!items.length) return "";
  const first = items[0]?.what || "一项操作";
  return items.length === 1
    ? `上次有一项操作没有执行：${first}`
    : `上次有 ${items.length} 项操作没有执行（例如 ${first}）`;
});
</script>

<template>
  <p v-if="interruptedHint" class="interrupted-note" role="status">{{ interruptedHint }}</p>
  <button
    v-if="approvals.pendingCount > 0 && !approvals.visible && !approvals.inlineClaimed"
    class="approval-entry"
    type="button"
    :aria-label="`有 ${approvals.pendingCount} 项操作等待确认，打开审批窗口`"
    @mousedown.prevent
    @click="approvals.openNow()"
  >
    <span class="mark" aria-hidden="true">!</span>
    有 {{ approvals.pendingCount }} 项操作等待确认
  </button>
</template>

<style scoped>
/*
 * 位置由 App.vue 的 `.top-notes` 容器统一负责（顶部居中，避开右下角的星球入口
 * 与设置入口）。这里只描述这一行本身为什么长这样。
 */
.approval-entry {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 14px; border-radius: var(--r-pill);
  background: var(--bg-elevated); color: var(--text-strong);
  border: 1px solid var(--warning); font-family: var(--sans); font-size: 12.5px;
  box-shadow: var(--shadow-2); cursor: pointer; pointer-events: auto;
  transition: transform var(--dur-press) var(--ease-out), border-color var(--dur-fast) var(--ease);
}
.approval-entry:hover { border-color: var(--accent); }
.approval-entry:active { transform: translateY(var(--press-shift)); }
.approval-entry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.mark {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--warning); color: var(--bg-base);
  font-family: var(--mono); font-size: 11px; font-weight: 700;
}
/* 「上次那项操作没有执行」：一句事实，不是待办 —— 不能看起来像能点的按钮 */
.interrupted-note {
  margin: 0; max-width: min(560px, calc(100vw - 32px));
  padding: 6px 14px; border-radius: var(--r-pill);
  background: var(--bg-elevated); color: var(--text-secondary);
  border: 1px solid var(--border-subtle); font-family: var(--sans); font-size: 12.5px;
  box-shadow: var(--shadow-1);
}
</style>
