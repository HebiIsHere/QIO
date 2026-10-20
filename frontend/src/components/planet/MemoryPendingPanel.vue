<script setup lang="ts">
/**
 * 「记忆待确认」入口 + 维护记录（阶段二 C，契约 §12.5）。
 *
 * 为什么需要它：待审批的知识提议和后台维护提出的独立问题此前没有统一的可见入口；
 * 维护批次与安全恢复也没有界面。这里把两类事项放进同一个清单，但**处理程序分开**：
 *
 * - kind=knowledge_proposal（handler=knowledge_proposal）：批准 / 修改后批准 / 拒绝，
 *   走既有 verify / revise / reject API；
 * - kind=maintenance_question（handler=maintenance）：回答 / 稍后处理，
 *   走维护执行器（后端 ctx.maintenance_runner），回答不进入主对话。
 *
 * 五条刻意的设计：
 * 1. 状态只做展示映射，不另造状态字符串（raw 值同时以 class 形式保留，便于测试）；
 * 2. 审批中的知识明确标「尚未生效」，不和已生效的事实混在一起；
 * 3. 动作反馈留在对应条目里（useActionFeedback），失败比成功持久，绝不静默；
 * 4. 恢复必须先预览再确认：可恢复 / 不可恢复原因逐项列出，无法安全恢复的部分明说；
 * 5. 服务端说某块存储不可用（迁移 47 未应用 / 调度器未接入）时如实说明，不伪造数据。
 */
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  api,
  type MaintenanceBatchDetail,
  type MaintenanceBatchSummary,
  type MaintenanceRestoreResult,
  type PendingItemView,
  type PendingKnowledgeRef,
} from "../../services/api";
import QConfirm from "../ui/QConfirm.vue";
import QSelect from "../ui/QSelect.vue";
import { useActionFeedback } from "../../composables/useActionFeedback";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const KIND_LABELS: Record<string, string> = {
  knowledge_proposal: "知识待审批",
  maintenance_question: "后台维护提问",
};
const CATEGORY_LABELS: Record<string, string> = {
  user_profile: "用户画像",
  agent_self: "自我",
  goal: "目标",
  general_fact: "常识",
  tool_experience: "工具经验",
};
const EPISTEMIC_LABELS: Record<string, string> = {
  user_explicit: "用户明确",
  shared_understanding: "共同认识",
  qio_observed: "实际观测",
  qio_inference: "QIO 推测",
};
/** 契约 §12.5 的真实状态（待回答 / 待审批 / 处理中 / 已处理 / 稍后处理 / 已拒绝 / 失败）。 */
const STATUS_LABELS: Record<string, string> = {
  awaiting_answer: "待回答",
  awaiting_approval: "待审批",
  processing: "处理中",
  processed: "已处理",
  deferred: "稍后处理",
  rejected: "已拒绝",
  failed: "失败",
};
const BATCH_STATUS_LABELS: Record<string, string> = {
  running: "进行中",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
  rolled_back: "已撤回",
  rollback_partial: "部分撤回",
};
const TRIGGER_LABELS: Record<string, string> = {
  change_count: "变化数达到阈值",
  waiting_turns: "等待轮数达到阈值",
};
const STATS_LABELS: Record<string, string> = {
  applied: "生效",
  asked: "提问",
  rejected: "拒绝",
  merged: "合并",
  normalized: "整理",
  failed: "失败",
};

type PanelView = "pending" | "records";
const view = ref<PanelView>("pending");

// -- 待确认事项 -------------------------------------------------------------

const items = ref<PendingItemView[]>([]);
const total = ref(0);
const truncated = ref(false);
const warnings = ref<string[]>([]);
const maintenanceAvailable = ref(true);
const loading = ref(false);
const loadError = ref("");
const kindFilter = ref("");
const sourceFilter = ref("");
const feedback = useActionFeedback();
/** 每条后台提问自己的回答草稿（按事项 id） */
const answers = ref<Record<string, string>>({});
const editingId = ref<string | null>(null);
const editDraft = ref("");
/**
 * 面板级成功提示：事项处理完会从列表消失，条目内的反馈如果跟着消失，
 * 用户就看不到「已经处理了」这件事（静默成功）。所以重要动作完成后在面板顶部
 * 留一条可见、会自动收回的说明。
 */
const notice = ref("");
let noticeTimer: ReturnType<typeof setTimeout> | null = null;
function showNotice(text: string) {
  notice.value = text;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    notice.value = "";
  }, 5000);
}

const kindOptions = [
  { value: "", label: "全部类型" },
  { value: "knowledge_proposal", label: "知识待审批" },
  { value: "maintenance_question", label: "后台维护提问" },
];
/** 来源筛选按 API 返回的标签匹配（不自行推断来源键） */
const sourceOptions = computed(() => {
  const labels = new Set<string>();
  for (const item of items.value) {
    for (const knowledge of item.knowledge) {
      if (knowledge.source) labels.add(knowledge.source);
    }
  }
  return [
    { value: "", label: "全部来源" },
    ...[...labels].sort().map((label) => ({ value: label, label })),
  ];
});
const filtered = computed(() =>
  items.value.filter((item) => {
    if (kindFilter.value && item.kind !== kindFilter.value) return false;
    if (sourceFilter.value && !item.knowledge.some((k) => k.source === sourceFilter.value)) {
      return false;
    }
    return true;
  }),
);
const hasFilters = computed(() => Boolean(kindFilter.value || sourceFilter.value));

function clearFilters() {
  kindFilter.value = "";
  sourceFilter.value = "";
}

function statusLabel(value: string): string {
  return STATUS_LABELS[value] || value;
}
function statusTone(value: string): string {
  if (value === "processed") return "ok";
  if (value === "failed") return "warn";
  if (value === "rejected" || value === "deferred") return "quiet";
  return "warn";
}
function kindLabel(value: string): string {
  return KIND_LABELS[value] || value;
}
function categoryLabel(value?: string | null): string {
  if (!value) return "";
  return CATEGORY_LABELS[value] || value;
}
function epistemicLabel(value?: string | null): string {
  if (!value) return "";
  return EPISTEMIC_LABELS[value] || value;
}
function batchStatusLabel(value?: string | null): string {
  if (!value) return "未知状态";
  return BATCH_STATUS_LABELS[value] || value;
}
function triggerLabel(value?: string | null): string {
  if (!value) return "触发原因未记录";
  return TRIGGER_LABELS[value] || value;
}
function can(item: PendingItemView, action: string): boolean {
  return item.actions.includes(action);
}
function firstKnowledge(item: PendingItemView): PendingKnowledgeRef | null {
  return item.knowledge[0] || null;
}
function scopeText(knowledge: PendingKnowledgeRef): string {
  return knowledge.scope.length ? knowledge.scope.join("、") : "未指定";
}
function shortText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}
function contentOf(entry: Record<string, unknown> | undefined): string {
  if (!entry) return "";
  return shortText(entry.content);
}
function answerOf(itemId: string): string {
  return answers.value[itemId] || "";
}
function setAnswer(itemId: string, value: string) {
  answers.value = { ...answers.value, [itemId]: value };
}
function feedbackKey(item: PendingItemView): string {
  return "item:" + item.id;
}

async function loadPending(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const result = await api.listMemoryPending();
    items.value = result.items;
    total.value = result.total;
    truncated.value = result.truncated;
    warnings.value = result.warnings || [];
    maintenanceAvailable.value = result.maintenance_available !== false;
  } catch (error) {
    console.error("[memory-pending] load failed:", error);
    loadError.value = "加载待确认事项失败：" + ((error as Error).message || "未知原因");
  } finally {
    loading.value = false;
  }
}

async function approveProposal(item: PendingItemView): Promise<void> {
  const ok = await feedback.run(
    feedbackKey(item),
    async () => {
      await api.verifyKnowledge(item.id);
      await loadPending();
    },
    { okText: "已批准并生效", failText: "批准没有成功" },
  );
  if (ok) showNotice("已批准并生效：" + (firstKnowledge(item)?.content || item.title));
}

async function rejectProposal(item: PendingItemView): Promise<void> {
  const ok = await feedback.run(
    feedbackKey(item),
    async () => {
      await api.rejectKnowledge(item.id);
      await loadPending();
    },
    { okText: "已拒绝这条提议", failText: "拒绝没有成功" },
  );
  if (ok) showNotice("已拒绝这条知识提议（原文已打回草稿，没有生效）。");
}

function startModify(item: PendingItemView) {
  const knowledge = firstKnowledge(item);
  editingId.value = item.id;
  editDraft.value = String(contentOf(item.after[0]) || knowledge?.content || "");
}

function cancelModify() {
  editingId.value = null;
  editDraft.value = "";
}

async function approveModified(item: PendingItemView): Promise<void> {
  const content = editDraft.value.trim();
  if (!content) return;
  const ok = await feedback.run(
    feedbackKey(item),
    async () => {
      // 既有生命周期只允许对「已生效」条目做版本纠正：
      // ① 先用 verify 批准这条提议（pending_review -> active）；
      // ② 再用 revise 原子替换成用户改过的内容（新版本）。
      // 两步都是既有 API；第二步失败时如实说明「原内容已批准、修正未生效」。
      await api.verifyKnowledge(item.id);
      try {
        await api.reviseKnowledge(item.id, content);
      } catch (error) {
        throw new Error(
          "原内容已批准，但修正没有生效：" + ((error as Error).message || "未知原因"),
        );
      }
      editingId.value = null;
      editDraft.value = "";
      await loadPending();
    },
    { okText: "已按修改后的内容生效", failText: "修改后批准没有完成" },
  );
  if (ok) showNotice("已按修改后的内容批准并生效。");
}

async function answerQuestion(item: PendingItemView): Promise<void> {
  const text = answerOf(item.id).trim();
  if (!text) return;
  const ok = await feedback.run(
    feedbackKey(item),
    async () => {
      const outcome = await api.answerMemoryPending(item.id, text);
      if (!outcome.ok) {
        throw new Error(outcome.error || "维护执行器返回 " + outcome.status);
      }
      answers.value = { ...answers.value, [item.id]: "" };
      await loadPending();
    },
    { okText: "回答已交给后台维护", failText: "回答没有成功" },
  );
  if (ok) showNotice("回答已交给后台维护，该事项已处理（不再出现在待确认列表）。");
}

async function deferQuestion(item: PendingItemView): Promise<void> {
  await feedback.run(
    feedbackKey(item),
    async () => {
      await api.deferMemoryPending(item.id);
      await loadPending();
    },
    { okText: "已标记稍后处理", failText: "稍后处理没有保存" },
  );
}

// -- 维护记录 ---------------------------------------------------------------

const batches = ref<MaintenanceBatchSummary[]>([]);
const batchesLoading = ref(false);
const batchesLoaded = ref(false);
const batchesError = ref("");
const batchesAvailable = ref(true);
const batchesTotal = ref(0);
const batchesTruncated = ref(false);
const batchesWarnings = ref<string[]>([]);
const selectedBatchId = ref("");
const detail = ref<MaintenanceBatchDetail | null>(null);
const detailLoading = ref(false);
const detailError = ref("");
const previewData = ref<Record<string, unknown> | null>(null);
const previewLoading = ref(false);
const previewError = ref("");
const previewTarget = ref("");
const restoreResult = ref<MaintenanceRestoreResult | null>(null);
const restoreLoading = ref(false);
const restoreError = ref("");
const confirmBatchId = ref("");
const confirmItemId = ref("");

function switchView(next: PanelView) {
  view.value = next;
  if (next === "records" && !batchesLoaded.value && !batchesLoading.value) {
    void loadBatches();
  }
}

async function loadBatches(): Promise<void> {
  batchesLoading.value = true;
  batchesError.value = "";
  try {
    const result = await api.listMaintenanceBatches();
    batches.value = result.batches;
    batchesTotal.value = result.total;
    batchesTruncated.value = result.truncated;
    batchesAvailable.value = result.available !== false;
    batchesWarnings.value = result.warnings || [];
    batchesLoaded.value = true;
  } catch (error) {
    console.error("[memory-pending] batches failed:", error);
    batchesError.value = "加载维护记录失败：" + ((error as Error).message || "未知原因");
  } finally {
    batchesLoading.value = false;
  }
}

async function openBatch(batchId: string): Promise<void> {
  selectedBatchId.value = batchId;
  detail.value = null;
  detailError.value = "";
  detailLoading.value = true;
  previewData.value = null;
  previewError.value = "";
  restoreResult.value = null;
  restoreError.value = "";
  try {
    detail.value = await api.getMaintenanceBatch(batchId);
  } catch (error) {
    detailError.value = "加载批次详情失败：" + ((error as Error).message || "未知原因");
  } finally {
    detailLoading.value = false;
  }
}

function closeBatch() {
  selectedBatchId.value = "";
  detail.value = null;
  detailError.value = "";
  previewData.value = null;
  previewError.value = "";
  restoreResult.value = null;
  restoreError.value = "";
}

async function previewRestore(target: { batch_id?: string; item_id?: string }): Promise<void> {
  previewTarget.value = target.batch_id
    ? "批次 " + target.batch_id
    : "事项 " + (target.item_id || "");
  previewLoading.value = true;
  previewError.value = "";
  previewData.value = null;
  try {
    const result = await api.previewMaintenanceRestore(target);
    previewData.value = result.preview;
  } catch (error) {
    previewError.value = "恢复预览失败：" + ((error as Error).message || "未知原因");
  } finally {
    previewLoading.value = false;
  }
}

async function doRestore(target: { batch_id?: string; item_id?: string }): Promise<void> {
  confirmBatchId.value = "";
  confirmItemId.value = "";
  restoreLoading.value = true;
  restoreError.value = "";
  try {
    restoreResult.value = await api.restoreMaintenance({ ...target, confirm: true });
    await loadBatches();
  } catch (error) {
    restoreError.value = "恢复没有完成：" + ((error as Error).message || "未知原因");
  } finally {
    restoreLoading.value = false;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
function entriesOf(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord);
}
const previewEntries = computed<Record<string, unknown>[]>(() => {
  const data = previewData.value;
  if (!data) return [];
  return entriesOf(data.items);
});
const restoredEntries = computed<Record<string, unknown>[]>(() =>
  entriesOf(restoreResult.value?.restored),
);
const blockedEntries = computed<Record<string, unknown>[]>(() =>
  entriesOf(restoreResult.value?.blocked),
);
function entryId(entry: Record<string, unknown>): string {
  return (
    shortText(entry.knowledge_id) ||
    shortText(entry.id) ||
    shortText(entry.result_id) ||
    "未标注对象"
  );
}
function previewEntryLabel(entry: Record<string, unknown>): string {
  const content = shortText(entry.content);
  return content ? entryId(entry) + "：" + content : entryId(entry);
}
function previewRestorable(entry: Record<string, unknown>): boolean {
  return entry.restorable !== false;
}
function previewReason(entry: Record<string, unknown>): string {
  return shortText(entry.reason);
}
function outcomeLabel(entry: Record<string, unknown>): string {
  const reason = shortText(entry.reason);
  return reason ? entryId(entry) + "：" + reason : entryId(entry);
}
function countText(value: unknown): string {
  if (typeof value === "number") return String(value);
  if (Array.isArray(value)) return String(value.length);
  return "—";
}
function statsText(batch: MaintenanceBatchSummary): string {
  const stats = batch.stats || {};
  const parts: string[] = [];
  for (const key of Object.keys(STATS_LABELS)) {
    const value = stats[key];
    if (typeof value === "number" && value > 0) {
      parts.push(STATS_LABELS[key] + " " + value);
    }
  }
  return parts.length ? parts.join(" · ") : "无统计";
}

onMounted(() => {
  void loadPending();
});

onBeforeUnmount(() => {
  if (noticeTimer) clearTimeout(noticeTimer);
});
</script>

<template>
  <div class="mpanel" v-qio-scrollbar data-qio-fit>
    <div class="mp-tabs" role="tablist" aria-label="记忆与维护">
      <button
        class="mp-tab mp-tab-pending"
        :class="{ active: view === 'pending' }"
        type="button"
        @click="switchView('pending')"
      >待确认</button>
      <button
        class="mp-tab mp-tab-records"
        :class="{ active: view === 'records' }"
        type="button"
        @click="switchView('records')"
      >维护记录</button>
      <span class="mp-tabs-hint">待审批的记忆与后台维护的问题都在这里处理</span>
    </div>

    <!-- 待确认事项 -->
    <template v-if="view === 'pending'">
      <div class="row filters">
        <QSelect v-model="kindFilter" :options="kindOptions" class="grow mp-kind-filter" />
        <QSelect v-model="sourceFilter" :options="sourceOptions" class="grow mp-source-filter" />
      </div>

      <div v-if="loading" class="hint">加载中…</div>
      <div v-else-if="loadError" class="mp-error" role="alert">
        <span>{{ loadError }}</span>
        <button class="qio-btn mini" type="button" @click="loadPending">重试</button>
      </div>
      <template v-else>
        <p v-if="notice" class="mp-notice" role="status">{{ notice }}</p>
        <p v-if="!maintenanceAvailable" class="mp-warn" role="status">
          {{ warnings[0] || '维护事项存储不可用：后台维护问题暂无数据可读（不伪造数据）。' }}
        </p>
        <p v-else-if="warnings.length" class="mp-warn" role="status">{{ warnings.join(" ") }}</p>
        <p v-if="truncated" class="hint">共 {{ total }} 条，当前只显示 {{ items.length }} 条（服务端已截断）。</p>

        <ul class="mp-list">
          <li
            v-for="item in filtered"
            :key="item.id"
            class="mp-item qio-card qio-card--quiet"
            :class="['kind-' + item.kind, item.status]"
          >
            <div class="mp-head">
              <span class="qio-tag mp-kind">{{ kindLabel(item.kind) }}</span>
              <span class="qio-state mp-status" :class="[item.status, statusTone(item.status)]">
                {{ statusLabel(item.status) }}
              </span>
              <span v-if="item.kind === 'knowledge_proposal'" class="mp-ineffective">尚未生效</span>
              <span v-if="item.kind === 'maintenance_question'" class="mp-handler">处理程序：后台维护</span>
            </div>

            <!-- 知识待审批：正文 / 范围 / 来源 / 确定程度 / 修改前后 -->
            <template v-if="item.kind === 'knowledge_proposal'">
              <p class="mp-title">{{ firstKnowledge(item)?.content || item.title }}</p>
              <div v-if="firstKnowledge(item)" class="mp-meta">
                <span v-if="categoryLabel(firstKnowledge(item)?.category)" class="mp-category">
                  {{ categoryLabel(firstKnowledge(item)?.category) }}
                </span>
                <span class="mp-scope">范围：{{ scopeText(firstKnowledge(item)!) }}</span>
                <span v-if="firstKnowledge(item)?.source" class="mp-source">
                  来源：{{ firstKnowledge(item)?.source }}
                </span>
                <span v-if="epistemicLabel(firstKnowledge(item)?.epistemic_status)" class="mp-epistemic">
                  依据：{{ epistemicLabel(firstKnowledge(item)?.epistemic_status) }}
                </span>
                <span class="mp-id mono">{{ firstKnowledge(item)?.id }} · v{{ firstKnowledge(item)?.version }}</span>
              </div>
              <div v-if="item.before.length" class="mp-diff">
                <div class="mp-diff-part">
                  <span class="mp-label">修改前</span>
                  <p class="mp-diff-text">{{ contentOf(item.before[0]) }}</p>
                </div>
                <div class="mp-diff-part">
                  <span class="mp-label">修改后</span>
                  <p class="mp-diff-text">{{ contentOf(item.after[0]) || firstKnowledge(item)?.content }}</p>
                </div>
              </div>
              <div v-if="item.evidence.length" class="mp-evidence">
                依据 {{ item.evidence.length }} 条真实记录
              </div>

              <textarea
                v-if="editingId === item.id"
                v-model="editDraft"
                class="qio-input mp-edit"
                rows="3"
                aria-label="修改后批准的内容"
              ></textarea>

              <div class="mp-actions">
                <template v-if="editingId === item.id">
                  <button class="qio-btn mini quiet mp-cancel-modify" type="button" @click="cancelModify">取消</button>
                  <button
                    class="qio-btn mini primary mp-save-modified"
                    type="button"
                    :disabled="feedback.stateOf(feedbackKey(item)) === 'busy'"
                    @click="approveModified(item)"
                  >
                    {{ feedback.stateOf(feedbackKey(item)) === 'busy' ? '保存中…' : '保存并批准' }}
                  </button>
                </template>
                <template v-else>
                  <button
                    v-if="can(item, 'approve')"
                    class="qio-btn mini primary mp-approve"
                    type="button"
                    :disabled="feedback.stateOf(feedbackKey(item)) === 'busy'"
                    @click="approveProposal(item)"
                  >批准</button>
                  <button
                    v-if="can(item, 'approve_modified')"
                    class="qio-btn mini mp-modify-open"
                    type="button"
                    @click="startModify(item)"
                  >修改后批准</button>
                  <button
                    v-if="can(item, 'reject')"
                    class="qio-btn mini quiet mp-reject"
                    type="button"
                    :disabled="feedback.stateOf(feedbackKey(item)) === 'busy'"
                    @click="rejectProposal(item)"
                  >拒绝</button>
                </template>
                <span v-if="feedback.stateOf(feedbackKey(item)) === 'ok'" class="mp-feedback ok" role="status">
                  {{ feedback.okTextOf(feedbackKey(item)) }}
                </span>
              </div>
              <p v-if="feedback.stateOf(feedbackKey(item)) === 'failed'" class="mp-feedback err" role="alert">
                {{ feedback.errorOf(feedbackKey(item)) }}
              </p>
            </template>

            <!-- 后台维护提问：问题 / 涉及知识 / 证据 / 回答或稍后处理 -->
            <template v-else>
              <p class="mp-question">{{ item.question || item.title }}</p>
              <ul v-if="item.knowledge.length" class="mp-knowledge">
                <li v-for="knowledge in item.knowledge" :key="knowledge.id" class="mp-knowledge-item">
                  <span class="mp-id mono">{{ knowledge.id }} · v{{ knowledge.version }}</span>
                  <span class="mp-knowledge-content">{{ knowledge.content }}</span>
                  <span v-if="knowledge.source" class="mp-source">来源：{{ knowledge.source }}</span>
                </li>
              </ul>
              <div v-if="item.evidence.length" class="mp-evidence">
                依据 {{ item.evidence.length }} 条真实记录
              </div>
              <textarea
                class="qio-input mp-answer"
                rows="3"
                :value="answerOf(item.id)"
                :disabled="feedback.stateOf(feedbackKey(item)) === 'busy'"
                placeholder="告诉 QIO 你的回答（只用于后台整理，不会出现在主对话里）"
                aria-label="回答后台维护提问"
                @input="setAnswer(item.id, ($event.target as HTMLTextAreaElement).value)"
              ></textarea>
              <div class="mp-actions">
                <button
                  v-if="can(item, 'answer')"
                  class="qio-btn mini primary mp-answer-submit"
                  type="button"
                  :disabled="!answerOf(item.id).trim() || feedback.stateOf(feedbackKey(item)) === 'busy'"
                  @click="answerQuestion(item)"
                >
                  {{ feedback.stateOf(feedbackKey(item)) === 'busy' ? '提交中…' : '提交回答' }}
                </button>
                <button
                  v-if="can(item, 'defer')"
                  class="qio-btn mini quiet mp-defer"
                  type="button"
                  :disabled="feedback.stateOf(feedbackKey(item)) === 'busy'"
                  @click="deferQuestion(item)"
                >稍后处理</button>
                <span v-if="feedback.stateOf(feedbackKey(item)) === 'ok'" class="mp-feedback ok" role="status">
                  {{ feedback.okTextOf(feedbackKey(item)) }}
                </span>
              </div>
              <p v-if="feedback.stateOf(feedbackKey(item)) === 'failed'" class="mp-feedback err" role="alert">
                {{ feedback.errorOf(feedbackKey(item)) }}
              </p>
            </template>
            <p v-if="item.error" class="mp-item-error" role="alert">失败原因：{{ item.error }}</p>
          </li>

          <li v-if="!filtered.length" class="hint mp-empty">
            <template v-if="items.length && hasFilters">
              没有匹配当前筛选的待确认事项。
              <button class="link mp-clear-filters" type="button" @click="clearFilters">清除筛选</button>
            </template>
            <template v-else>暂无待确认事项。</template>
          </li>
        </ul>
      </template>
    </template>

    <!-- 维护记录 -->
    <template v-else>
      <div class="row filters">
        <button
          class="qio-btn mini mp-batches-refresh"
          type="button"
          :disabled="batchesLoading"
          @click="loadBatches"
        >{{ batchesLoading ? '刷新中…' : '刷新' }}</button>
        <span class="hint">维护批次是后台整理的真实运行记录；恢复前先预览影响。</span>
      </div>

      <div v-if="batchesLoading && !batchesLoaded" class="hint">正在加载维护记录…</div>
      <div v-else-if="batchesError" class="mp-error" role="alert">
        <span>{{ batchesError }}</span>
        <button class="qio-btn mini" type="button" @click="loadBatches">重试</button>
      </div>
      <p v-else-if="!batchesAvailable" class="mp-warn" role="status">
        {{ batchesWarnings[0] || '维护记录不可用：没有可展示的批次，也不伪造数据。' }}
      </p>
      <template v-else>
        <p v-if="batchesTruncated" class="hint">
          共 {{ batchesTotal }} 个批次，当前只显示 {{ batches.length }} 个（服务端已截断）。
        </p>
        <p v-if="!batches.length" class="hint mp-batches-empty">
          尚无维护批次：后台整理还没有运行过，或运行记录不可用。
        </p>
        <ul class="mp-batches">
          <li
            v-for="batch in batches"
            :key="batch.id"
            class="mp-batch qio-card qio-card--quiet"
            :class="batch.status"
          >
            <button class="mp-batch-open" type="button" @click="openBatch(batch.id)">
              <span class="qio-state mp-batch-status" :class="[batch.status, batch.status === 'completed' ? 'ok' : batch.status === 'failed' ? 'warn' : 'quiet']">
                {{ batchStatusLabel(batch.status) }}
              </span>
              <span class="mp-batch-trigger">{{ triggerLabel(batch.trigger) }}</span>
              <span class="mp-batch-time mono">{{ batch.finished_at || batch.created_at }}</span>
              <span class="mp-batch-stats">{{ statsText(batch) }}</span>
              <span class="mp-batch-id mono">{{ batch.id }}</span>
            </button>
            <p v-if="batch.error" class="mp-item-error" role="alert">批次错误：{{ batch.error }}</p>
          </li>
        </ul>

        <div v-if="selectedBatchId" class="mp-detail">
          <div class="row mp-detail-head">
            <span class="mp-label">批次详情</span>
            <span class="mp-id mono">{{ selectedBatchId }}</span>
            <button class="link mp-detail-close" type="button" @click="closeBatch">收起</button>
          </div>
          <div v-if="detailLoading" class="hint">正在加载批次详情…</div>
          <div v-else-if="detailError" class="mp-error" role="alert">
            <span>{{ detailError }}</span>
            <button class="qio-btn mini" type="button" @click="openBatch(selectedBatchId)">重试</button>
          </div>
          <template v-else-if="detail">
            <div class="mp-actions mp-restore-actions">
              <button
                class="qio-btn mini mp-preview"
                type="button"
                :disabled="previewLoading"
                @click="previewRestore({ batch_id: selectedBatchId })"
              >{{ previewLoading ? '预览中…' : '预览整批撤回' }}</button>
              <button
                class="qio-btn mini mp-restore"
                type="button"
                :disabled="restoreLoading"
                @click="confirmBatchId = selectedBatchId"
              >整批撤回</button>
              <span class="hint">撤回不删除历史；已被你后续修改的部分会拒绝恢复并说明原因。</span>
            </div>
            <QConfirm
              v-if="confirmBatchId === selectedBatchId"
              :open="true"
              variant="inline"
              tone="danger"
              title="整批撤回这次维护的修改？"
              detail="会用新的版本记录恢复维护前的内容；不会删除历史，也不会覆盖你后来做的修改（这些项会被拒绝并列出原因）。"
              confirm-text="整批撤回"
              @confirm="doRestore({ batch_id: selectedBatchId })"
              @cancel="confirmBatchId = ''"
            />

            <div v-if="previewError" class="mp-error" role="alert">
              <span>{{ previewError }}</span>
            </div>
            <div v-if="previewData" class="mp-preview-result">
              <div class="mp-head">
                <span class="mp-label">恢复预览</span>
                <span class="mp-preview-target mono">{{ previewTarget }}</span>
              </div>
              <ul v-if="previewEntries.length" class="mp-preview-items">
                <li
                  v-for="(entry, index) in previewEntries"
                  :key="index"
                  class="mp-preview-item"
                  :class="previewRestorable(entry) ? 'restorable' : 'blocked'"
                >
                  <span class="qio-state" :class="previewRestorable(entry) ? 'ok' : 'quiet'">
                    {{ previewRestorable(entry) ? '可恢复' : '不可安全恢复' }}
                  </span>
                  <span class="mp-preview-label mono">{{ previewEntryLabel(entry) }}</span>
                  <span v-if="previewReason(entry)" class="mp-reason">{{ previewReason(entry) }}</span>
                </li>
              </ul>
              <p v-else class="hint">
                这次预览没有逐项结果（服务端未返回 items）；不臆造可恢复结论。
              </p>
            </div>

            <div v-if="restoreError" class="mp-error" role="alert">
              <span>{{ restoreError }}</span>
            </div>
            <div v-if="restoreResult" class="mp-restore-result">
              <div class="mp-head">
                <span class="mp-label">恢复结果</span>
                <span class="qio-state" :class="restoreResult.ok ? 'ok' : 'warn'">
                  {{ restoreResult.ok ? '恢复已完成' : '恢复未全部完成' }}
                </span>
                <span v-if="restoreResult.status" class="mp-status-text mono">status={{ restoreResult.status }}</span>
              </div>
              <p class="mp-restore-count">
                已恢复 {{ countText(restoreResult.restored) }} 项；无法安全恢复
                {{ countText(restoreResult.blocked) }} 项。
              </p>
              <ul v-if="restoredEntries.length" class="mp-restore-list">
                <li v-for="(entry, index) in restoredEntries" :key="index" class="mp-restored-item">
                  <span class="qio-state ok">已恢复</span>
                  <span class="mono">{{ outcomeLabel(entry) }}</span>
                </li>
              </ul>
              <ul v-if="blockedEntries.length" class="mp-restore-list">
                <li v-for="(entry, index) in blockedEntries" :key="index" class="mp-blocked-item">
                  <span class="qio-state quiet">无法安全恢复</span>
                  <span class="mono">{{ outcomeLabel(entry) }}</span>
                </li>
              </ul>
            </div>

            <div class="mp-detail-section">
              <span class="mp-label">涉及事项（{{ detail.items.length }}）</span>
              <p v-if="!detail.items.length" class="hint">这个批次没有留下事项记录。</p>
              <ul class="mp-detail-items">
                <li v-for="entry in detail.items" :key="entry.id" class="mp-detail-item">
                  <div class="mp-head">
                    <span class="qio-tag">{{ entry.kind || '未记录类型' }}</span>
                    <span class="qio-state quiet">{{ entry.status || '未记录状态' }}</span>
                    <span class="mp-id mono">{{ entry.id }}</span>
                  </div>
                  <p v-if="entry.question" class="mp-question">{{ entry.question }}</p>
                  <p v-if="entry.reason" class="hint">原因：{{ entry.reason }}</p>
                  <p v-if="entry.error" class="mp-item-error" role="alert">错误：{{ entry.error }}</p>
                  <div class="mp-actions">
                    <button
                      class="qio-btn mini quiet mp-item-preview"
                      type="button"
                      @click="previewRestore({ item_id: entry.id })"
                    >预览恢复这一项</button>
                    <button
                      class="qio-btn mini quiet mp-item-restore"
                      type="button"
                      @click="confirmItemId = entry.id || ''"
                    >恢复这一项</button>
                  </div>
                  <QConfirm
                    v-if="confirmItemId === entry.id"
                    :open="true"
                    variant="inline"
                    tone="danger"
                    title="恢复这一项的维护修改？"
                    detail="会用新的版本记录恢复修改前的内容；如果之后已被你改过，将拒绝恢复并说明原因。历史不会删除。"
                    confirm-text="恢复这一项"
                    @confirm="doRestore({ item_id: entry.id })"
                    @cancel="confirmItemId = ''"
                  />
                </li>
              </ul>
            </div>

            <div class="mp-detail-section">
              <span class="mp-label">实际操作记录（{{ detail.operations.length }}）</span>
              <p v-if="!detail.operations.length" class="hint">这个批次没有留下操作记录。</p>
              <ul class="mp-detail-items">
                <li v-for="operation in detail.operations" :key="operation.id" class="mp-detail-item">
                  <div class="mp-head">
                    <span class="qio-tag">{{ operation.action || '未记录动作' }}</span>
                    <span class="qio-state quiet">{{ operation.status || '未记录状态' }}</span>
                    <span class="mp-id mono">{{ operation.id }}</span>
                    <span v-if="operation.result_id" class="mp-id mono">结果 {{ operation.result_id }}</span>
                  </div>
                </li>
              </ul>
            </div>
          </template>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
/* 面板正文层：只有这一层纵向滚动（与 KnowledgePanel 一致）。 */
.mpanel {
  display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px;
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto; overflow-x: clip; overscroll-behavior: contain;
}
.row { display: flex; align-items: center; gap: 8px; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; border-radius: 8px; }
.grow { flex: 1; }
.mp-tabs { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding-bottom: 2px; }
.mp-tab {
  font-size: 12.5px; padding: 4px 10px; border-radius: 8px; cursor: pointer;
  background: none; border: 1px solid transparent; color: var(--text-secondary);
}
.mp-tab:hover { color: var(--text-strong); border-color: var(--border-subtle); }
.mp-tab.active { color: var(--text-strong); border-color: var(--border-strong); background: var(--bg-inset); }
.mp-tabs-hint { font-size: 11px; color: var(--text-muted); }
.filters { margin-bottom: 4px; flex-wrap: wrap; }
.mp-list, .mp-batches, .mp-preview-items, .mp-restore-list, .mp-detail-items, .mp-knowledge {
  list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px;
}
.mp-item { padding: var(--sp-3); border-radius: var(--r-lg); display: flex; flex-direction: column; gap: 6px; }
.mp-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.mp-ineffective, .mp-handler { font-size: 11px; color: var(--text-muted); }
/* 标题/话题层用衬线（字体三声部）；正文默认无衬线 */
.mp-title {
  margin: 2px 0 0; font-family: var(--serif); font-size: 14.5px;
  line-height: 1.65; color: var(--text-primary); white-space: pre-wrap;
}
.mp-question { margin: 2px 0 0; font-size: 13.5px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; }
.mp-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11.5px; color: var(--text-muted); }
.mp-category { color: var(--text-secondary); }
.mp-source, .mp-scope, .mp-epistemic { color: var(--text-muted); }
.mp-id { font-size: 11px; color: var(--text-faint); }
.mono { font-family: var(--mono); }
.mp-diff { display: flex; flex-direction: column; gap: 6px; }
.mp-diff-part { border-left: 2px solid var(--border-subtle); padding-left: 8px; }
.mp-label { font-size: 11px; color: var(--text-muted); letter-spacing: .02em; }
.mp-diff-text { margin: 2px 0 0; font-size: 12.5px; line-height: 1.65; color: var(--text-primary); white-space: pre-wrap; }
.mp-evidence { font-size: 11px; color: var(--text-muted); }
.mp-knowledge-item { display: flex; flex-direction: column; gap: 2px; border-left: 2px solid var(--border-subtle); padding-left: 8px; }
.mp-knowledge-content { font-size: 12.5px; line-height: 1.65; color: var(--text-primary); white-space: pre-wrap; }
.mp-edit, .mp-answer { width: 100%; font-size: 13px; resize: vertical; min-height: 64px; }
.mp-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 2px; }
.mp-feedback { font-size: 11.5px; }
.mp-feedback.ok { color: var(--success); }
.mp-feedback.err { margin: 0; color: var(--danger); }
.mp-item-error { margin: 0; font-size: 11.5px; color: var(--danger); }
.mp-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: 8px; font-size: 12px;
  border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger);
}
.mp-warn {
  margin: 0; padding: 8px 10px; border-radius: 8px; font-size: 12px;
  border: 1px solid var(--warning-soft); background: var(--warning-soft); color: var(--warning);
}
.mp-notice {
  margin: 0; padding: 8px 10px; border-radius: 8px; font-size: 12px;
  border: 1px solid var(--success-soft); background: var(--success-soft); color: var(--success);
}
.mp-empty { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
.mp-batch { padding: var(--sp-2) var(--sp-3); border-radius: var(--r-md); }
.mp-batch-open {
  width: 100%; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  background: none; border: none; cursor: pointer; text-align: left; padding: 0;
  color: var(--text-secondary); font-size: 12px;
}
.mp-batch-open:hover { color: var(--text-strong); }
.mp-batch-trigger { color: var(--text-primary); }
.mp-batch-time { font-size: 11px; color: var(--text-muted); }
.mp-batch-stats { font-size: 11.5px; color: var(--text-secondary); }
.mp-batch-id { font-size: 10.5px; color: var(--text-faint); margin-left: auto; }
.mp-detail { border: 1px dashed var(--border-subtle); border-radius: var(--r-lg); padding: var(--sp-3); display: flex; flex-direction: column; gap: 8px; }
.mp-detail-head { flex-wrap: wrap; }
.mp-detail-section { display: flex; flex-direction: column; gap: 6px; }
.mp-detail-item { padding: var(--sp-2) 0; border-bottom: 1px solid var(--border-subtle); display: flex; flex-direction: column; gap: 4px; }
.mp-preview-result, .mp-restore-result {
  border: 1px solid var(--border-subtle); border-radius: var(--r-md);
  padding: var(--sp-2) var(--sp-3); display: flex; flex-direction: column; gap: 6px;
}
.mp-preview-item, .mp-restored-item, .mp-blocked-item { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }
.mp-preview-label, .mp-reason { font-size: 11.5px; }
.mp-reason { color: var(--text-muted); }
.mp-restore-count { margin: 0; font-size: 12px; color: var(--text-secondary); }
.mp-status-text { font-size: 11px; color: var(--text-muted); }
@media (max-width: 720px) {
  .mp-tabs-hint { display: none; }
}
</style>
