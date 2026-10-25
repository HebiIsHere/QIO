<script setup lang="ts">
/**
 * 审批结果与历史面板（A / F02、F03、F04）。
 *
 * 数据只来自两个真实来源（见 stores/approvalResults.ts）：
 * * SSE APPROVAL_RESULT（实时结局，按 approval_id 建键）；
 * * GET /api/approvals/history（持久化终态；刷新/换页后重建）。
 *
 * 面板自己不做任何状态推进：只呈现 + 提供两种**互不混淆**的重试
 * （恢复待审批失败 / 读取历史失败），另外把知识待确认读取失败单独说明。
 */
import { computed, onMounted } from "vue";
import { useApprovalsStore } from "../../stores/approvals";
import {
  isTerminalApprovalState,
  useApprovalResultsStore,
  type ApprovalResultEntry,
} from "../../stores/approvalResults";
import { useUnifiedApprovalsStore } from "../../stores/unifiedApprovals";
import { APPROVAL_KIND_TITLES } from "../../stores/unifiedApprovals";
import { whenText } from "../conversation/entries";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const approvals = useApprovalsStore();
const results = useApprovalResultsStore();
const unified = useUnifiedApprovalsStore();

const entries = computed(() => results.entries);
const historyFailed = computed(() => Boolean(results.historyError));

function kindTitle(kind: string): string {
  return APPROVAL_KIND_TITLES[kind] ?? "需要你确认的操作";
}

function entryTone(entry: ApprovalResultEntry): "ok" | "warn" | "info" | "fail" {
  if (entry.state === "completed") return "ok";
  if (entry.state === "failed") return "fail";
  if (isTerminalApprovalState(entry.state)) return "warn";
  return "info";
}

/** 已批准 / 执行中：明确说「等待执行」，不能显示成完成。 */
function waitingText(entry: ApprovalResultEntry): string {
  if (entry.state === "approved") {
    return "已批准，等待后端执行；执行结果会在完成后出现在这里（不等于已完成）。";
  }
  if (entry.state === "executing") {
    return "后端正在执行；结果还没回来，稍后刷新这里查看。";
  }
  return "";
}

async function retryRecovery(): Promise<void> {
  await approvals.retryRecovery();
  if (!approvals.recoveryError) await results.loadHistory();
}

async function reloadHistory(): Promise<void> {
  await results.loadHistory();
}

async function retryKnowledge(): Promise<void> {
  await unified.refreshKnowledgePending();
}

/**
 * 面板每次挂载（统一弹窗打开 / 互动页任务中心切到本分区）都补一次历史：
 * 超过 15 秒没读过才真正请求，避免重复打开造成抖动。
 */
onMounted(() => {
  results.ensureFresh();
});
</script>

<template>
  <div class="ar" data-test="approval-results-panel">
    <!-- F04：待审批恢复失败（与下面两种读取失败分开说明；各自重试） -->
    <div v-if="approvals.recoveryError" class="ar-alert" role="alert" data-test="approval-recovery-error">
      <p class="ar-alert-text">{{ approvals.recoveryError }}</p>
      <button
        class="ar-btn"
        type="button"
        :disabled="approvals.recoveryBusy"
        data-test="approval-recovery-retry"
        @click="retryRecovery"
      >
        {{ approvals.recoveryBusy ? "正在重试…" : "重试恢复待审批" }}
      </button>
    </div>
    <p v-else-if="approvals.recoveryBusy" class="ar-note" role="status">正在向服务端核对待审批列表…</p>

    <!-- 历史读取失败：保留已有结果，明确说「读不到」而不是「没有历史」 -->
    <div v-if="historyFailed" class="ar-alert" role="alert" data-test="approval-history-error">
      <p class="ar-alert-text">{{ results.historyError }}</p>
      <button
        class="ar-btn"
        type="button"
        :disabled="results.historyLoading"
        data-test="approval-history-retry"
        @click="reloadHistory"
      >
        {{ results.historyLoading ? "正在重读…" : "重试读取历史" }}
      </button>
    </div>
    <p v-else-if="results.historyLoading" class="ar-note" role="status" data-test="approval-history-loading">
      正在读取审批历史…
    </p>

    <!-- 知识待确认读取失败：与审批历史/恢复失败分开说（它只影响知识类申请） -->
    <div
      v-if="unified.knowledgeStatus === 'error' && unified.knowledgeError"
      class="ar-alert"
      role="status"
      data-test="knowledge-pending-error"
    >
      <p class="ar-alert-text">{{ unified.knowledgeError }}</p>
      <button class="ar-btn" type="button" @click="retryKnowledge">重试读取待确认知识</button>
    </div>

    <ul v-if="entries.length" class="ar-list" data-test="approval-results-list">
      <li
        v-for="entry in entries"
        :key="entry.approvalId"
        class="ar-entry"
        :data-approval-id="entry.approvalId"
        :data-state="entry.state"
      >
        <header class="ar-head">
          <span class="ar-state" :data-tone="entryTone(entry)" data-test="approval-result-state">
            {{ entry.stateLabel }}
          </span>
          <span class="ar-when mono">{{ whenText(entry.at) }}</span>
          <span v-if="entry.source === 'history'" class="ar-src mono" title="来自后端持久化历史（刷新后仍可查看）">
            持久记录
          </span>
          <span v-else class="ar-src mono" title="本次会话实时收到的事件">实时</span>
        </header>

        <p class="ar-label" data-test="approval-result-label">
          {{ entry.label || kindTitle(entry.kind) }}
        </p>
        <p v-if="entry.summary" class="ar-summary" data-test="approval-result-summary">{{ entry.summary }}</p>
        <p v-if="waitingText(entry)" class="ar-waiting" role="status" data-test="approval-results-waiting">
          {{ waitingText(entry) }}
        </p>

        <!-- 批量逐项结果（只用后端给的 item_id / 对象字段；未执行不标成功） -->
        <div v-if="entry.items.length" class="ar-items" data-test="approval-result-items">
          <p class="ar-batch mono" data-test="approval-result-batch-summary">{{ entry.batchSummary }}</p>
          <ul class="ar-item-list" v-qio-scrollbar="{ policy: 'auto', axis: 'y' }">
            <li
              v-for="(item, index) in entry.items"
              :key="(item.itemId || item.objectText || 'item') + ':' + index"
              class="ar-item"
              data-test="approval-result-item"
              :data-item-id="item.itemId"
            >
              <span class="ar-item-status" :data-tone="item.ok ? 'ok' : 'fail'">{{ item.statusText }}</span>
              <span class="ar-item-object mono">
                {{ item.objectText || (item.itemId ? "对象 " + item.itemId : "未提供对象身份") }}
              </span>
              <span v-if="item.itemId && item.objectText" class="ar-item-id mono">{{ item.itemId }}</span>
              <span v-if="item.reason" class="ar-item-reason">{{ item.reason }}</span>
            </li>
          </ul>
        </div>
      </li>
    </ul>

    <!-- 空态：只有在「历史确实读成功」时才说没有记录 -->
    <p
      v-else-if="!historyFailed && !results.historyLoading"
      class="ar-empty"
      data-test="approval-results-empty"
    >
      还没有审批结果。批准或拒绝的申请完成后会在这里留下记录（来自后端历史，刷新后仍可查看）。
    </p>

    <footer class="ar-foot">
      <span class="ar-hint">结果与历史是只读记录；这里不能批准或拒绝任何申请。</span>
      <button class="ar-btn" type="button" :disabled="results.historyLoading" @click="reloadHistory">
        刷新历史
      </button>
    </footer>
  </div>
</template>

<style scoped>
.ar {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
  font-family: var(--sans);
  color: var(--text-primary);
}
.ar-alert {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--danger);
  border-radius: var(--r-md);
  background: var(--danger-soft);
}
.ar-alert-text {
  flex: 1 1 220px;
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}
.ar-note {
  margin: 0;
  font-size: 12px;
  color: var(--text-muted);
}
.ar-btn {
  flex: none;
  min-height: 28px;
  padding: 3px 10px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--text-primary);
  font-family: var(--sans);
  font-size: 11.5px;
  cursor: pointer;
}
.ar-btn:hover:not(:disabled) {
  background: var(--bg-inset);
}
.ar-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.ar-btn:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 1px;
}
.ar-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ar-entry {
  min-width: 0;
  padding: 9px 11px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-inset);
}
.ar-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 10px;
}
.ar-state {
  font-family: var(--mono);
  font-size: 11.5px;
  letter-spacing: 0.02em;
  padding: 1px 7px;
  border-radius: var(--r-pill);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}
.ar-state[data-tone="ok"] {
  color: var(--success);
  border-color: var(--success);
}
.ar-state[data-tone="fail"] {
  color: var(--danger);
  border-color: var(--danger);
}
.ar-state[data-tone="warn"] {
  color: var(--warning);
  border-color: var(--warning);
}
.ar-state[data-tone="info"] {
  color: var(--text-secondary);
  border-color: var(--border-strong);
}
.ar-when {
  font-size: 10.5px;
  color: var(--text-muted);
}
.ar-src {
  font-size: 10px;
  color: var(--text-faint);
  letter-spacing: 0.04em;
}
.ar-label {
  margin: 6px 0 0;
  font-size: 13px;
  line-height: 1.55;
  color: var(--text-strong);
  overflow-wrap: anywhere;
}
.ar-summary {
  margin: 3px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.ar-waiting {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.ar-items {
  margin-top: 8px;
  padding-top: 7px;
  border-top: 1px dashed var(--border-subtle);
  min-width: 0;
}
.ar-batch {
  margin: 0 0 5px;
  font-size: 11px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.ar-item-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 180px;
  overflow-y: auto;
  overscroll-behavior: contain;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.ar-item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
  min-width: 0;
  font-size: 11.5px;
  line-height: 1.55;
}
.ar-item-status {
  flex: none;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-secondary);
}
.ar-item-status[data-tone="ok"] {
  color: var(--success);
}
.ar-item-status[data-tone="fail"] {
  color: var(--danger);
}
.ar-item-object {
  min-width: 0;
  color: var(--text-primary);
  overflow-wrap: anywhere;
  word-break: break-word;
}
.ar-item-id {
  color: var(--text-faint);
  font-size: 10px;
}
.ar-item-reason {
  flex: 1 1 100%;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.ar-empty {
  margin: 2px 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-muted);
}
.ar-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
}
.ar-hint {
  flex: 1 1 200px;
  font-size: 10.5px;
  color: var(--text-muted);
  line-height: 1.5;
}
</style>
