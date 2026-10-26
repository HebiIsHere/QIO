<script setup lang="ts">
/**
 * 非聊天路由（设置 / 调试 / 星球…）的统一审批入口。
 *
 * 迁移自旧 ApprovalEntry.vue 的两项**有效能力**（旧文件由 C 删除）：
 * 1. 有待审批时亮出一行入口（不抢焦点、自动弹窗被挡/收起后仍能找回待办）；
 * 2. 「上次有一项操作没有执行」的只读事实提示（不是待办、不可点）。
 *
 * 入口只打开**统一审批弹窗**（UnifiedApprovalLayer 挂载的同一个弹窗）：
 * 有待办时打开队列，没有待办时打开「结果与历史」。
 * 不是第二套常驻悬浮按钮 —— 聊天页使用输入区 ⑤ 入口，这里只在非聊天路由出现。
 */
import { computed } from "vue";
import { useApprovalsStore } from "../../stores/approvals";
import { useApprovalResultsStore } from "../../stores/approvalResults";
import { useUnifiedApprovalsStore } from "../../stores/unifiedApprovals";
import { useSessionStore } from "../../stores/session";

const approvals = useApprovalsStore();
const results = useApprovalResultsStore();
const unified = useUnifiedApprovalsStore();
const session = useSessionStore();

/** 上一次进程结束时没回答完的那次操作：不会再恢复等待，只是一句事实。 */
const interrupted = computed(() => session.interruptedOperations);
const interruptedHint = computed(() => {
  const items = interrupted.value;
  if (!items.length) return "";
  const first = items[0]?.what || "一项操作";
  return items.length === 1
    ? `上次有一项操作没有执行：${first}`
    : `上次有 ${items.length} 项操作没有执行（例如 ${first}）`;
});

const visible = computed(
  () =>
    unified.count > 0 ||
    results.hasAny ||
    Boolean(results.historyError) ||
    Boolean(approvals.recoveryError),
);

/** 可见失败标记（F04）：恢复/历史读取失败时入口上有一个红色「!」，正常时不渲染。 */
const errorText = computed(() => {
  if (approvals.recoveryError) return "恢复待审批项失败";
  if (results.historyError) return "审批历史读取失败";
  return "";
});

const label = computed(() => {
  if (unified.count > 0) return `有 ${unified.count} 项操作等待确认`;
  if (approvals.recoveryError) return "待审批列表读取失败，打开查看原因与重试";
  if (results.historyError) return "审批历史读取失败，打开查看原因与重试";
  return "查看审批结果与历史";
});

function open() {
  if (unified.count > 0) unified.open();
  else unified.openResults();
}
</script>

<template>
  <p v-if="interruptedHint" class="interrupted-note" role="status" data-test="interrupted-operations-note">
    {{ interruptedHint }}
  </p>
  <button
    v-if="visible"
    class="nonchat-approval-entry"
    type="button"
    data-test="nonchat-approval-entry"
    :aria-label="label + '，打开审批窗口'"
    @mousedown.prevent
    @click="open"
  >
    <span class="mark" :class="{ error: errorText }" aria-hidden="true">!</span>
    <span class="text">{{ label }}</span>
    <span
      v-if="errorText"
      class="entry-error"
      data-test="approval-entry-error"
      aria-hidden="true"
    ></span>
  </button>
</template>

<style scoped>
.nonchat-approval-entry {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: min(560px, calc(100vw - 32px));
  padding: 6px 14px;
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--bg-elevated);
  color: var(--text-strong);
  border: 1px solid var(--warning);
  font-family: var(--sans);
  font-size: 12.5px;
  box-shadow: var(--shadow-2);
  cursor: pointer;
  pointer-events: auto;
  transition:
    transform var(--dur-press) var(--ease-out),
    border-color var(--dur-fast) var(--ease);
}
.nonchat-approval-entry:hover {
  border-color: var(--accent);
}
.nonchat-approval-entry:active {
  transform: translateY(var(--press-shift));
}
.nonchat-approval-entry:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.mark.error {
  background: var(--danger);
}
/* 读取失败的可见警示标（绝对定位，不挤坏窄窗布局） */
.nonchat-approval-entry {
  position: relative;
}
.nonchat-approval-entry .entry-error {
  position: absolute;
  left: -4px;
  bottom: -4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  border: 2px solid var(--bg-elevated);
  background: var(--danger);
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
.text {
  overflow-wrap: anywhere;
}
/* 「上次那项操作没有执行」：一句事实，不是待办 —— 不能看起来像能点的按钮 */
.interrupted-note {
  margin: 0;
  max-width: min(560px, calc(100vw - 32px));
  padding: 6px 14px;
  border-radius: var(--qio-ui-radius, var(--r-lg));
  background: var(--bg-elevated);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  font-family: var(--sans);
  font-size: 12.5px;
  box-shadow: var(--shadow-1);
  pointer-events: auto;
}
</style>