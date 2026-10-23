<script setup lang="ts">
/**
 * 意图与结果工作台（阶段三 C，契约 §13.5）。
 *
 * 为什么需要它：「意图提出」与「执行结果」现在发生在同一条主循环里，用户需要一处
 * 能看到：意图目标 / 预期 / 依据 / 影响范围 / 依赖与冲突 / 版本 / 授权 / 真实状态，
 * 能单条或同批（>=2）确认；执行后能看到等待、账本摘要、结果卡、部分完成 / 局部冲突 /
 * 失败与恢复入口。这里只做展示与转发，**不自己判定**依赖、冲突、授权与完成状态。
 *
 * 三条硬性口径：
 * 1. 同批批量确认至少两条（BATCH_CONFIRM_MIN，与后端一致），单条走单项确认；
 * 2. 「已受理 / 已确认」绝不说成「已完成」：完成只认服务端 completed 与账本 applied；
 * 3. 服务未接入 / 账本不可用时如实说明（available=false / 503 原文），不伪造数据。
 */
import { computed, onMounted, ref } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import {
  batchConfirmIntents,
  confirmSingleIntent,
  fetchIntentResults,
  fetchIntentViews,
  fetchLedger,
  fetchRecoveryPreview,
  fetchWaiting,
  recoverIntent,
} from "../../services/interactive";
import {
  BATCH_CONFIRM_MIN,
  batchConfirmEnabled,
  batchConfirmHint,
  canRecover,
  confirmEligibleIds,
  confirmOutcomeText,
  dependencyText,
  isConfirmable,
  operationsSummaryText,
  recoveryOutcomeText,
  scopeSummary,
  statusLabel,
  statusTone,
  type IntentResultView,
  type IntentView,
  type IntentViewListResponse,
  type LedgerSummaryView,
  type RecoveryPreviewView,
  type WaitingResponse,
  type WaitingView,
} from "../../interactive/intentWorkbench";
// Phase 3-A：确认结果逐项显式 ids / 部分失败 / 真实 turn_id（纯函数，与 v2 虚拟层共用）
import {
  confirmBlockReason,
  confirmBlockText,
  isNeedsUpdate,
  summarizeConfirm,
  type ConfirmSummary,
} from "./v2/intentDisplay";
import IntentResultCards from "./IntentResultCards.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const emit = defineEmits<{ close: [] }>();

const store = useInteractiveStore();
const boardId = computed(() => store.boardId);

const loading = ref(false);
const loadError = ref("");
const views = ref<IntentView[]>([]);
const source = ref<string>("service");
const batchGate = ref<{ min: number; eligible_ids: string[]; available: boolean } | null>(null);
const selectedIds = ref<string[]>([]);

const waiting = ref<WaitingResponse | null>(null);
const waitingError = ref("");
const ledger = ref<LedgerSummaryView | null>(null);
const ledgerError = ref("");
const results = ref<IntentResultView[]>([]);
const resultsError = ref("");

const actionBusy = ref("");
const notice = ref("");
const actionError = ref("");
/** 最近一次确认的逐项结果（显式 ids；只认服务端真实 status / turn_id） */
const confirmSummary = ref<ConfirmSummary | null>(null);

const recoveryTargetId = ref("");
const recoveryPreview = ref<RecoveryPreviewView | null>(null);
const recoveryError = ref("");

const eligibleIds = computed(() =>
  batchGate.value?.eligible_ids ?? confirmEligibleIds(views.value),
);
const selectedEligible = computed(() =>
  selectedIds.value.filter((intentId) => eligibleIds.value.includes(intentId)),
);
const batchReady = computed(() => batchConfirmEnabled(selectedEligible.value.length));
const batchHint = computed(() => batchConfirmHint(selectedEligible.value.length));
const waitingItems = computed<WaitingView[]>(() => {
  const value = waiting.value?.waiting;
  if (!value) return [];
  const list: WaitingView[] = Array.isArray(value)
    ? value
    : typeof value === "object"
      ? [value as WaitingView]
      : [];
  // A 的真实 waiting_state 用 camelCase；API 已规范成 snake_case，这里再兼容两种写法。
  return list.map((item) => {
    const record = item as Record<string, unknown>;
    return {
      ...item,
      submission_id: item.submission_id ?? (record.submissionId as string | undefined),
      missing_fields:
        item.missing_fields ?? ((record.missingFields as string[] | undefined) ?? []),
    };
  });
});

function errorMessage(error: unknown): string {
  const message = (error as Error)?.message ?? String(error);
  return message || "原因未知";
}

function isSelected(intentId: string): boolean {
  return selectedIds.value.includes(intentId);
}

function toggleSelect(view: IntentView): void {
  if (!isConfirmable(view)) return;
  if (isSelected(view.intent_id)) {
    selectedIds.value = selectedIds.value.filter((item) => item !== view.intent_id);
  } else {
    selectedIds.value = [...selectedIds.value, view.intent_id];
  }
}

function pruneSelection(): void {
  selectedIds.value = selectedIds.value.filter((intentId) =>
    eligibleIds.value.includes(intentId),
  );
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  actionError.value = "";
  try {
    const payload: IntentViewListResponse = await fetchIntentViews(boardId.value);
    views.value = Array.isArray(payload.intents) ? payload.intents : [];
    source.value = payload.source ?? "service";
    batchGate.value = payload.batch_confirm ?? null;
    pruneSelection();
  } catch (error) {
    loadError.value = "读取意图失败：" + errorMessage(error);
    views.value = [];
  } finally {
    loading.value = false;
  }
  const [waitingResult, ledgerResult, resultsResult] = await Promise.allSettled([
    fetchWaiting(boardId.value),
    fetchLedger(boardId.value),
    fetchIntentResults(boardId.value),
  ]);
  if (waitingResult.status === "fulfilled") {
    waiting.value = waitingResult.value;
    waitingError.value =
      waitingResult.value.available === false
        ? waitingResult.value.reason ?? "等待事项不可用"
        : "";
  } else {
    waitingError.value = "读取等待事项失败：" + errorMessage(waitingResult.reason);
  }
  if (ledgerResult.status === "fulfilled") {
    ledger.value = ledgerResult.value;
    ledgerError.value = ledgerResult.value.available === false
      ? ledgerResult.value.reason ?? "操作账本不可用"
      : "";
  } else {
    ledgerError.value = "读取操作账本失败：" + errorMessage(ledgerResult.reason);
  }
  if (resultsResult.status === "fulfilled") {
    results.value = Array.isArray(resultsResult.value.results)
      ? resultsResult.value.results
      : [];
    resultsError.value = resultsResult.value.available === false
      ? resultsResult.value.reason ?? "结果卡不可用"
      : "";
  } else {
    resultsError.value = "读取结果卡失败：" + errorMessage(resultsResult.reason);
  }
}

async function confirmOne(view: IntentView): Promise<void> {
  // 可确认判定只走一次口径：needs_update / 未知状态 / 已开始都不冒充可确认
  if (actionBusy.value || confirmBlockReason(view) !== null) return;
  actionBusy.value = view.intent_id;
  notice.value = "";
  actionError.value = "";
  try {
    const result = await confirmSingleIntent(view.intent_id, boardId.value);
    // 显式单 id；结果逐项可见，只有服务端 completed 才算完成
    confirmSummary.value = summarizeConfirm(result, [view.intent_id], { batch: false });
    notice.value = confirmSummary.value.notice || confirmOutcomeText(result);
    await load();
  } catch (error) {
    actionError.value = "确认没有完成：" + errorMessage(error);
  } finally {
    actionBusy.value = "";
  }
}

async function confirmBatch(): Promise<void> {
  if (actionBusy.value || !batchReady.value) return;
  const intentIds = [...selectedEligible.value];
  if (intentIds.length < BATCH_CONFIRM_MIN) return;
  actionBusy.value = "batch";
  notice.value = "";
  actionError.value = "";
  try {
    const result = await batchConfirmIntents(boardId.value, intentIds);
    // 显式 ids（只含可确认的已选项）；部分失败会逐项列出
    confirmSummary.value = summarizeConfirm(result, intentIds, { batch: true });
    notice.value = confirmSummary.value.notice || confirmOutcomeText(result);
    selectedIds.value = [];
    await load();
  } catch (error) {
    actionError.value = "同批确认没有完成：" + errorMessage(error);
  } finally {
    actionBusy.value = "";
  }
}

async function openRecoveryPreview(view: IntentView): Promise<void> {
  if (actionBusy.value || !canRecover(view)) return;
  recoveryTargetId.value = view.intent_id;
  recoveryPreview.value = null;
  recoveryError.value = "";
  actionBusy.value = "preview:" + view.intent_id;
  try {
    const response = await fetchRecoveryPreview(view.intent_id);
    if (!response.available || !response.preview) {
      recoveryError.value = "恢复预览不可用：服务端没有返回预览，不臆造可恢复结论。";
      return;
    }
    recoveryPreview.value = response.preview;
  } catch (error) {
    recoveryError.value = "读取恢复预览失败：" + errorMessage(error);
  } finally {
    actionBusy.value = "";
  }
}

async function recover(view: IntentView): Promise<void> {
  const preview = recoveryPreview.value;
  if (actionBusy.value) return;
  if (!preview || preview.intent_id !== view.intent_id) return;
  if (preview.can_resume !== true) return;
  actionBusy.value = "recover:" + view.intent_id;
  notice.value = "";
  actionError.value = "";
  try {
    const result = await recoverIntent(view.intent_id, boardId.value, view.version);
    notice.value = recoveryOutcomeText(result);
    if (!result.ok) {
      actionError.value = recoveryOutcomeText(result);
    }
    recoveryTargetId.value = "";
    recoveryPreview.value = null;
    await load();
  } catch (error) {
    actionError.value = "恢复没有完成：" + errorMessage(error);
  } finally {
    actionBusy.value = "";
  }
}

function cancelRecovery(): void {
  recoveryTargetId.value = "";
  recoveryPreview.value = null;
  recoveryError.value = "";
}

/** 真实 preview_recovery 的未完成操作 / 依赖是结构化对象；这里只做可读映射，不丢信息。 */
function recoveryItemsText(items: unknown[] | undefined): string {
  return (items ?? [])
    .map((item) => {
      if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
        const key =
          record.operation_key ?? record.operation_id ?? record.action ?? record.intent_id ?? record.id;
        const status = record.status ? "（" + String(record.status) + "）" : "";
        return (key ? String(key) : "未标注项") + status;
      }
      return String(item);
    })
    .join("、");
}

function ledgerCountEntries(): Array<{ key: string; label: string; value: number }> {
  const counts = ledger.value?.counts ?? {};
  const labels: Record<string, string> = {
    applied: "已完成操作",
    running: "执行中",
    pending: "未开始",
    failed: "失败",
    conflict: "冲突",
  };
  return Object.keys(labels).map((key) => ({
    key,
    label: labels[key],
    value: Number(counts[key] ?? 0),
  }));
}

onMounted(() => {
  void load();
});
</script>

<template>
  <section class="iw" role="region" aria-label="意图与结果" data-im="intent-workbench">
    <header class="iw-head">
      <div class="iw-title-row">
        <h2 class="iw-title">意图与结果</h2>
        <span class="iw-source mono" data-im="iw-source" :data-source="source">
          {{ source === "legacy" ? "旧数据只读兼容" : "第三阶段意图视图" }}
        </span>
      </div>
      <div class="iw-head-actions">
        <button
          class="iw-btn quiet"
          type="button"
          data-im="iw-refresh"
          :disabled="loading"
          @click="load"
        >{{ loading ? "刷新中…" : "刷新" }}</button>
        <button class="iw-btn quiet" type="button" data-im="iw-close" @click="emit('close')">收起</button>
      </div>
    </header>

    <div v-if="loading && !views.length" class="iw-hint" data-im="iw-loading">正在读取意图与结果…</div>
    <div v-if="loadError" class="iw-error" role="alert" data-im="iw-load-error">
      <span>{{ loadError }}</span>
      <button class="iw-btn quiet" type="button" @click="load">重试</button>
    </div>
    <p v-if="notice" class="iw-notice" role="status" data-im="iw-notice">{{ notice }}</p>
    <p v-if="actionError" class="iw-error" role="alert" data-im="iw-action-error">{{ actionError }}</p>

    <!-- Phase 3-A：确认结果逐项（显式 id / 真实 turn_id / 部分失败分开列） -->
    <div v-if="confirmSummary" class="iw-confirm" data-im="iw-confirm-summary">
      <ul class="iw-rows" data-im="iw-confirm-rows">
        <li
          v-for="row in confirmSummary.rows"
          :key="row.intentId"
          data-im="iw-confirm-row"
          :data-intent-id="row.intentId"
          :data-ok="row.ok ? 'true' : 'false'"
          :data-turn-id="row.turnId ?? ''"
          :data-completed="row.completed ? 'true' : 'false'"
        >{{ row.intentId }} · {{ row.statusText }}<span v-if="row.turnId"> · 执行轮次 {{ row.turnId }}</span><span v-if="row.reason"> · {{ row.reason }}</span></li>
      </ul>
      <p v-if="confirmSummary.failedIds.length" class="iw-row iw-warn" data-im="iw-confirm-partial">
        部分失败：{{ confirmSummary.failedIds.join("、") }}
      </p>
      <p v-if="confirmSummary.startedUnknownIds.length" class="iw-row iw-warn" data-im="iw-confirm-started-unknown">
        没有返回执行轮次：{{ confirmSummary.startedUnknownIds.join("、") }}（不能认为已经开始执行）
      </p>
    </div>

    <div class="iw-body" v-qio-scrollbar="{ policy: 'auto', axis: 'y' }">
      <!-- 同批批量确认（门槛两条，前后端同口径） -->
      <div class="iw-batch-bar" data-im="iw-batch-bar">
        <span class="iw-batch-summary mono" data-im="iw-batch-summary">
          已选 {{ selectedEligible.length }} / 可确认 {{ eligibleIds.length }} 条
        </span>
        <span class="iw-batch-hint" data-im="iw-batch-hint">{{ batchHint }}</span>
        <button
          class="iw-btn primary"
          type="button"
          data-im="iw-batch-confirm"
          :disabled="!batchReady || Boolean(actionBusy)"
          :aria-disabled="!batchReady"
          :title="batchHint"
          @click="confirmBatch"
        >{{ actionBusy === "batch" ? "提交中…" : "同批确认 " + BATCH_CONFIRM_MIN + " 条起" }}</button>
      </div>

      <!-- 意图列表 -->
      <section class="iw-section" aria-label="意图">
        <h3 class="iw-heading">意图（{{ views.length }}）</h3>
        <p v-if="!views.length && !loading" class="iw-hint" data-im="iw-empty">
          还没有意图。提交给 QIO 后，意图会按真实状态出现在这里。
        </p>
        <ul v-if="views.length" class="iw-intents">
          <li
            v-for="view in views"
            :key="view.intent_id"
            class="iw-intent"
            data-im="iw-intent"
            :data-intent-id="view.intent_id"
            :data-status="view.status"
          >
            <div class="iw-intent-head">
              <label v-if="isConfirmable(view)" class="iw-check">
                <input
                  type="checkbox"
                  data-im="iw-select"
                  :checked="isSelected(view.intent_id)"
                  @change="toggleSelect(view)"
                />
                选择
              </label>
              <span class="iw-intent-title">{{ view.title }}</span>
              <span class="iw-status" :class="statusTone(view.status)" data-im="iw-status">
                {{ statusLabel(view.status) }}
              </span>
              <span class="iw-version mono" data-im="iw-version">v{{ view.version }}</span>
              <span v-if="view.demo" class="iw-tag">演示</span>
              <span v-if="view.raw_legacy_status" class="iw-tag mono">旧状态 {{ view.raw_legacy_status }}</span>
            </div>
            <p v-if="view.goal" class="iw-goal" data-im="iw-goal"><span class="iw-label">目标</span>{{ view.goal }}</p>
            <p v-if="view.expected" class="iw-row"><span class="iw-label">预期</span>{{ view.expected }}</p>
            <p class="iw-row"><span class="iw-label">范围</span>{{ scopeSummary(view.scope) }}</p>
            <p v-if="view.basis && view.basis.length" class="iw-row" data-im="iw-basis">
              <span class="iw-label">依据</span>
              {{ view.basis.map((item) => item.card_id + " · v" + item.version).join("、") }}
            </p>
            <p v-if="dependencyText(view)" class="iw-row iw-warn" data-im="iw-dependencies">
              <span class="iw-label">依赖 / 冲突</span>{{ dependencyText(view) }}
            </p>
            <p v-if="operationsSummaryText(view)" class="iw-row" data-im="iw-operations">
              <span class="iw-label">操作账本</span>{{ operationsSummaryText(view) }}
            </p>
            <p v-if="isNeedsUpdate(view)" class="iw-row iw-warn" data-im="iw-needs-update">
              <span class="iw-label">需要更新</span>{{ confirmBlockText(view) }}
            </p>
            <p v-if="view.authorization_valid === false" class="iw-row iw-warn" data-im="iw-auth">
              <span class="iw-label">授权</span>旧授权已失效：需要重新确认当前目标与影响范围。
            </p>
            <p v-if="view.summary" class="iw-row iw-summary"><span class="iw-label">说明</span>{{ view.summary }}</p>
            <div class="iw-intent-actions">
              <button
                v-if="isConfirmable(view)"
                class="iw-btn primary"
                type="button"
                data-im="iw-confirm"
                :disabled="Boolean(actionBusy)"
                @click="confirmOne(view)"
              >{{ actionBusy === view.intent_id ? "提交中…" : "确认这一条" }}</button>
              <button
                v-if="canRecover(view)"
                class="iw-btn"
                type="button"
                data-im="iw-recover-preview"
                :disabled="Boolean(actionBusy)"
                @click="openRecoveryPreview(view)"
              >恢复预览</button>
            </div>

            <!-- 恢复预览与恢复（只把可恢复的意图放回可继续状态，不自动执行操作） -->
            <div
              v-if="recoveryTargetId === view.intent_id"
              class="iw-recovery"
              data-im="iw-recovery-preview"
            >
              <p v-if="recoveryError" class="iw-error" role="alert">{{ recoveryError }}</p>
              <template v-if="recoveryPreview">
                <p class="iw-row">
                  <span class="iw-label">可恢复</span>
                  {{ recoveryPreview.can_resume === true ? "是" : "否（不会写入，等待条件满足后再试）" }}
                </p>
                <p v-if="recoveryPreview.reasons && recoveryPreview.reasons.length" class="iw-row iw-warn">
                  <span class="iw-label">原因</span>{{ recoveryPreview.reasons.join("；") }}
                </p>
                <p v-if="recoveryPreview.unfinished_operations && recoveryPreview.unfinished_operations.length" class="iw-row">
                  <span class="iw-label">未完成操作</span>{{ recoveryItemsText(recoveryPreview.unfinished_operations) }}
                </p>
                <p v-if="recoveryPreview.unfinished_dependencies && recoveryPreview.unfinished_dependencies.length" class="iw-row">
                  <span class="iw-label">未完成依赖</span>{{ recoveryItemsText(recoveryPreview.unfinished_dependencies) }}
                </p>
                <div class="iw-intent-actions">
                  <button
                    class="iw-btn primary"
                    type="button"
                    data-im="iw-recover-confirm"
                    :disabled="recoveryPreview.can_resume !== true || Boolean(actionBusy)"
                    @click="recover(view)"
                  >{{ actionBusy && actionBusy.indexOf("recover:") === 0 ? "恢复中…" : "确认恢复" }}</button>
                  <button class="iw-btn quiet" type="button" data-im="iw-recover-cancel" @click="cancelRecovery">取消</button>
                </div>
              </template>
            </div>
          </li>
        </ul>
      </section>

      <!-- 等待事项（A 的提交—等待—续作持久化） -->
      <section class="iw-section" aria-label="等待" data-im="iw-waiting">
        <h3 class="iw-heading">等待</h3>
        <p v-if="waitingError" class="iw-hint" data-im="iw-waiting-unavailable">{{ waitingError }}</p>
        <template v-else-if="waitingItems.length">
          <div v-for="(item, index) in waitingItems" :key="index" class="iw-waiting-item" data-im="iw-waiting-item">
            <p class="iw-waiting-question">{{ item.question || "（等待事项没有留下问题正文）" }}</p>
            <p v-if="item.submission_id" class="iw-row mono">
              <span class="iw-label">提交</span>{{ item.submission_id }}
            </p>
            <p v-if="item.missing_fields && item.missing_fields.length" class="iw-row iw-warn">
              <span class="iw-label">缺少</span>{{ item.missing_fields.join("、") }}
            </p>
          </div>
        </template>
        <p v-else class="iw-hint">当前没有等待事项。</p>
      </section>

      <!-- 操作账本摘要（只有 applied 算完成） -->
      <section class="iw-section" aria-label="操作账本" data-im="iw-ledger">
        <h3 class="iw-heading">操作账本</h3>
        <p v-if="ledgerError" class="iw-hint" data-im="iw-ledger-unavailable">{{ ledgerError }}</p>
        <template v-else-if="ledger && ledger.available">
          <ul class="iw-counts">
            <li v-for="entry in ledgerCountEntries()" :key="entry.key" class="iw-count" :data-im="'iw-count:' + entry.key">
              <span class="mono">{{ entry.value }}</span> {{ entry.label }}
            </li>
          </ul>
          <ul v-if="ledger.operations && ledger.operations.length" class="iw-ops">
            <li v-for="operation in ledger.operations.slice(0, 8)" :key="operation.operation_id" class="iw-op">
              <span class="iw-tag mono">{{ operation.action || "操作" }}</span>
              <span class="iw-op-status">{{ operation.status || "状态未知" }}</span>
              <span class="mono">{{ operation.operation_key || operation.operation_id }}</span>
            </li>
          </ul>
        </template>
        <p v-else class="iw-hint">没有可展示的操作账本。</p>
      </section>

      <!-- 结果卡：完整 Markdown（表格 / 代码块），长卡片折叠 + 展开 -->
      <section class="iw-section" aria-label="结果卡">
        <h3 class="iw-heading">结果卡</h3>
        <p v-if="resultsError" class="iw-hint" data-im="iw-results-unavailable">{{ resultsError }}</p>
        <IntentResultCards v-else :results="results" />
      </section>
    </div>
  </section>
</template>

<style scoped>
.iw {
  position: absolute;
  top: var(--sp-3);
  right: var(--sp-3);
  bottom: var(--sp-3);
  width: min(560px, 92vw);
  z-index: 46;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-lg);
  background: var(--bg-panel);
  box-shadow: var(--shadow-2);
  color: var(--text-primary);
  font-family: var(--sans);
}
.iw-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4) var(--sp-2);
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}
.iw-title-row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
  flex-wrap: wrap;
}
.iw-title {
  margin: 0;
  font-family: var(--serif);
  font-size: var(--fs-md);
  color: var(--text-strong);
}
.iw-source {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.iw-source[data-source="legacy"] { color: var(--warning); }
.iw-head-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: none;
}
.iw-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: clip;
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.iw-batch-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  background: var(--bg-inset);
}
.iw-batch-summary { font-size: var(--fs-xs); color: var(--text-secondary); }
.iw-batch-hint { flex: 1; min-width: 180px; font-size: var(--fs-xs); color: var(--text-muted); }
.iw-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.iw-heading {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-secondary);
}
.iw-intents, .iw-ops, .iw-counts {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.iw-intent {
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-md);
  padding: var(--sp-2) var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  background: var(--bg-elevated);
  min-width: 0;
}
.iw-intent-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.iw-check {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}
.iw-intent-title {
  font-size: var(--fs-sm);
  color: var(--text-strong);
  min-width: 0;
}
.iw-status {
  font-size: var(--fs-xs);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
  color: var(--text-secondary);
}
.iw-status.open { color: var(--link); border-color: var(--accent-soft); }
.iw-status.waiting { color: var(--warning); border-color: var(--warning-soft); }
.iw-status.active { color: var(--accent); border-color: var(--accent-soft); }
.iw-status.settled { color: var(--success); border-color: var(--success-soft); }
.iw-status.problem { color: var(--danger); border-color: var(--danger-soft); }
.iw-version { font-size: var(--fs-xs); color: var(--text-muted); }
.iw-tag {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
}
.iw-goal, .iw-row {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.iw-label {
  display: inline-block;
  margin-right: var(--sp-1);
  color: var(--text-muted);
}
.iw-warn { color: var(--warning); }
.iw-warn .iw-label { color: var(--warning); }
.iw-summary { color: var(--text-secondary); }
.iw-intent-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-top: var(--sp-1);
}
.iw-recovery {
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-md);
  padding: var(--sp-2);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.iw-waiting-item {
  border-left: 2px solid var(--warning-soft);
  padding-left: var(--sp-2);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.iw-waiting-question {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-primary);
  line-height: 1.65;
}
.iw-counts { flex-direction: row; flex-wrap: wrap; gap: var(--sp-3); }
.iw-count { font-size: var(--fs-xs); color: var(--text-secondary); }
.iw-op { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-xs); color: var(--text-secondary); }
.iw-op-status { color: var(--text-muted); }
.iw-error {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin: 0;
  padding: var(--sp-2);
  border: 1px solid var(--border-danger);
  border-radius: var(--r-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: var(--fs-xs);
}
.iw-notice {
  margin: 0;
  padding: var(--sp-2);
  border: 1px solid var(--success-soft);
  border-radius: var(--r-sm);
  background: var(--success-soft);
  color: var(--success);
  font-size: var(--fs-xs);
}
.iw-hint { margin: 0; font-size: var(--fs-xs); color: var(--text-muted); }
.iw-btn {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: none;
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.iw-btn:hover { color: var(--text-strong); border-color: var(--border-strong); }
.iw-btn:disabled { opacity: 0.5; cursor: default; }
.iw-btn.primary { color: var(--on-accent); background: var(--accent); border-color: var(--accent); }
.iw-btn.primary:hover { background: var(--accent-hover); color: var(--on-accent); }
.iw-btn.quiet { border-color: transparent; }
.mono { font-family: var(--mono); }
@media (max-width: 720px) {
  .iw { left: var(--sp-2); right: var(--sp-2); width: auto; top: var(--sp-2); bottom: var(--sp-2); }
}
</style>
