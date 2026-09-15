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

const approvals = useApprovalsStore();
</script>

<template>
  <button
    v-if="approvals.pendingCount > 0 && !approvals.visible"
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
/* 顶部居中：避开右下角的星球入口与设置入口（它们是可拖动浮动组件） */
.approval-entry {
  position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
  z-index: 190; display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 14px; border-radius: var(--r-pill);
  background: var(--bg-elevated); color: var(--text-strong);
  border: 1px solid var(--warning); font-family: var(--sans); font-size: 12.5px;
  box-shadow: var(--shadow-2); cursor: pointer;
  transition: transform var(--dur-press) var(--ease-out), border-color var(--dur-fast) var(--ease);
}
.approval-entry:hover { border-color: var(--accent); }
.approval-entry:active { transform: translateX(-50%) translateY(var(--press-shift)); }
.approval-entry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
.mark {
  display: inline-flex; align-items: center; justify-content: center;
  width: 16px; height: 16px; border-radius: 50%;
  background: var(--warning); color: var(--bg-base);
  font-family: var(--mono); font-size: 11px; font-weight: 700;
}
</style>
