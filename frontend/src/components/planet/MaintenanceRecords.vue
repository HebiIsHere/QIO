<script setup lang="ts">
/**
 * 整理记录（B · 管理流程）。
 *
 * 列表 → 单次整理详情。详情给「这次整理做了什么、影响了什么、现在是什么状态」：
 * - 整理摘要：状态 / 触发原因 / 统计 / 模型 / 起止时间（有真实值才显示）；
 * - 受影响条目：真实 operations（动作、目标、状态、时间），点开才展开修改前后对比，
 *   默认不铺开全部差异，也**不展示任何原始日志**；
 * - 整理事项：维护留下的问题与处理结果（有才显示）；
 * - 撤回 / 恢复：**预览与确认都复用中央弹窗** MaintenanceRestoreDialog（摘要、可恢复性、
 *   原因、前后快照、恢复约束）；服务端会拒绝「整理之后又被改过」的部分并给出原因，
 *   界面照实展示，不把「部分恢复」说成完全恢复。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import {
  api,
  type MaintenanceBatchDetail,
  type MaintenanceBatchSummary,
  type MaintenanceRestoreResult,
} from "../../services/api";
import QIcon from "../ui/QIcon.vue";
import MaintenanceRestoreDialog, { type RestoreDialogItem } from "./MaintenanceRestoreDialog.vue";
import { useModalArbiterStore } from "../../stores/modalArbiter";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const props = withDefaults(defineProps<{ focusBatchId?: string | null }>(), {
  focusBatchId: null,
});
const emit = defineEmits<{ changed: [] }>();

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
const ITEM_STATUS_LABELS: Record<string, string> = {
  awaiting_answer: "待回答",
  processed: "已处理",
  deferred: "稍后处理",
  rejected: "已拒绝",
  failed: "失败",
  skipped: "跳过",
};
const OPERATION_STATUS_LABELS: Record<string, string> = {
  applied: "已生效",
  failed: "失败",
  skipped: "跳过",
  restored: "已恢复",
  already_restored: "此前已恢复过",
};

function statusLabel(value: string | null | undefined): string {
  if (!value) return "未知状态";
  return BATCH_STATUS_LABELS[value] || value;
}
function triggerLabel(value: string | null | undefined): string {
  if (!value) return "触发原因未记录";
  return TRIGGER_LABELS[value] || value;
}
function itemStatusLabel(value: string | null | undefined): string {
  if (!value) return "状态未记录";
  return ITEM_STATUS_LABELS[value] || value;
}
function operationStatusLabel(value: string | null | undefined): string {
  if (!value) return "状态未记录";
  return OPERATION_STATUS_LABELS[value] || value;
}
function batchTone(status: string): string {
  if (status === "completed") return "ok";
  if (status === "failed") return "warn";
  if (status === "rollback_partial") return "warn";
  return "quiet";
}
function statsText(batch: MaintenanceBatchSummary): string {
  const stats = batch.stats || {};
  const parts: string[] = [];
  for (const key of Object.keys(STATS_LABELS)) {
    const value = stats[key];
    if (typeof value === "number" && value > 0) parts.push(STATS_LABELS[key] + " " + value);
  }
  return parts.length ? parts.join(" · ") : "无统计";
}
/** 起止时间与耗时：只展示服务端真实给出的时间（不猜）。 */
function batchTimes(batch: MaintenanceBatchSummary): string {
  const parts: string[] = [];
  if (batch.created_at) parts.push("开始 " + batch.created_at);
  if (batch.finished_at) parts.push("结束 " + batch.finished_at);
  const start = batch.created_at ? Date.parse(batch.created_at) : NaN;
  const end = batch.finished_at ? Date.parse(batch.finished_at) : NaN;
  if (Number.isFinite(start) && Number.isFinite(end) && end >= start) {
    const seconds = Math.round((end - start) / 1000);
    parts.push(seconds < 60 ? "耗时 " + seconds + " 秒" : "耗时 " + Math.round(seconds / 60) + " 分钟");
  }
  return parts.join(" · ") || "时间未记录";
}

// -- 列表 -------------------------------------------------------------------

const batches = ref<MaintenanceBatchSummary[]>([]);
const listLoading = ref(false);
const listLoaded = ref(false);
const listError = ref("");
const listAvailable = ref(true);
const listTotal = ref(0);
const listTruncated = ref(false);
const listWarnings = ref<string[]>([]);

// -- 列表 ↔ 详情 -------------------------------------------------------------

type PanelView = "list" | "detail";
const view = ref<PanelView>("list");
const activeBatchId = ref<string | null>(null);
const rootRef = ref<HTMLElement | null>(null);
let listScrollTop = 0;

const detail = ref<MaintenanceBatchDetail | null>(null);
const detailLoading = ref(false);
const detailError = ref("");

function scrollRootTo(top: number) {
  const root = rootRef.value;
  if (!root) return;
  root.scrollTop = Math.max(0, top);
}

async function loadBatches() {
  listLoading.value = true;
  listError.value = "";
  try {
    const result = await api.listMaintenanceBatches();
    batches.value = result.batches;
    listTotal.value = result.total;
    listTruncated.value = result.truncated;
    listAvailable.value = result.available !== false;
    listWarnings.value = result.warnings || [];
    listLoaded.value = true;
  } catch (error) {
    console.error("[maintenance-records] batches failed:", error);
    listError.value = "暂时读不到整理记录：" + ((error as Error).message || "未知原因");
  } finally {
    listLoading.value = false;
  }
}

async function loadDetail(batchId: string) {
  detailLoading.value = true;
  detailError.value = "";
  try {
    detail.value = await api.getMaintenanceBatch(batchId);
  } catch (error) {
    console.error("[maintenance-records] batch detail failed:", error);
    detailError.value = "加载整理详情失败：" + ((error as Error).message || "未知原因");
  } finally {
    detailLoading.value = false;
  }
}

function openBatch(batchId: string) {
  if (view.value === "list") listScrollTop = rootRef.value?.scrollTop ?? 0;
  activeBatchId.value = batchId;
  view.value = "detail";
  detail.value = null;
  detailError.value = "";
  expandedItemId.value = null;
  restoreResult.value = null;
  restoreError.value = "";
  void nextTick(() => scrollRootTo(0));
  void loadDetail(batchId);
}

function backToList() {
  view.value = "list";
  activeBatchId.value = null;
  detail.value = null;
  detailError.value = "";
  expandedItemId.value = null;
  restoreResult.value = null;
  restoreError.value = "";
  const top = listScrollTop;
  void nextTick(() => scrollRootTo(top));
}

/** 点受影响条目才展开修改前后对比（默认折叠；不展示原始日志）。 */
const expandedItemId = ref<string | null>(null);
function toggleItem(itemId: string) {
  expandedItemId.value = expandedItemId.value === itemId ? null : itemId;
}

// -- 受影响条目（真实 operations；没有操作记录时退回 items） ------------------

interface AffectedItem {
  id: string;
  action: string;
  status: string;
  targetIds: string;
  resultId: string;
  at: string;
  reason: string;
  before: string;
  after: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
function entriesOf(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}
function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
function shortText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}
function snapshotsOf(value: unknown): string {
  return entriesOf(value)
    .map((entry) => shortText(entry.content))
    .filter(Boolean)
    .join("\n");
}
function targetIdsOf(value: unknown): string {
  if (Array.isArray(value)) {
    return value
      .map((entry) => (isRecord(entry) ? shortText(entry.knowledge_id) || shortText(entry.id) : shortText(entry)))
      .filter(Boolean)
      .join("、");
  }
  if (isRecord(value)) return shortText(value.knowledge_id) || shortText(value.id);
  return shortText(value);
}

/**
 * 受影响条目：优先真实操作记录（merge/normalize/…，带 before/after 快照）；
 * 没有操作记录时用整理事项。任何地方都不拼接内部 JSON 给用户看。
 */
const affectedItems = computed<AffectedItem[]>(() => {
  const data = detail.value;
  if (!data) return [];
  const operations = data.operations || [];
  if (operations.length) {
    return operations.map((operation, index) => {
      const id = String(operation.id || "op_" + index);
      return {
        id,
        action: operation.action || "未记录动作",
        status: operation.status || "",
        targetIds: targetIdsOf(operation.target_ids),
        resultId: operation.result_id || "",
        at: operation.created_at || "",
        reason: "",
        before: snapshotsOf(operation.before),
        after: snapshotsOf(operation.after),
      };
    });
  }
  return (data.items || []).map((item, index) => {
    const id = String(item.id || "item_" + index);
    return {
      id,
      action: item.kind || "整理事项",
      status: item.status || "",
      targetIds: "",
      resultId: "",
      at: item.updated_at || item.created_at || "",
      reason: item.reason || item.error || "",
      before: snapshotsOf(item.before),
      after: snapshotsOf(item.after),
    };
  });
});

/** 整理事项（维护留下的问题/结果）：有才显示。 */
const maintenanceItems = computed(() => {
  const data = detail.value;
  if (!data) return [];
  return (data.items || []).map((item, index) => ({
    id: String(item.id || "item_" + index),
    kind: item.kind || "未记录类型",
    status: item.status || "",
    question: item.question || "",
    reason: item.reason || "",
    error: item.error || "",
    at: item.updated_at || item.created_at || "",
  }));
});

// -- 中央弹窗：预览 + 撤回/恢复 ----------------------------------------------

const restoreArbiter = useModalArbiterStore();
const restoreDialogOpen = ref(false);
/**
 * 弹窗的稳定身份：打开时确定，关闭过程中**不能**跟着 restoreTarget 一起变空。
 * 否则 dialog 关闭时按空 id 去仲裁器注销，原 id 永远留在栈里 —— 之后所有恢复弹窗都会被
 * 「另一个中央弹窗正在处理」挡住（只读历史态关闭后复现）。
 */
const restoreModalId = ref("");
const restoreTarget = ref<{ batch_id?: string; item_id?: string } | null>(null);
const restoreTargetLabel = ref("");
const restoreDialogTitle = ref("整批撤回这次记忆整理？");
const restoreDialogConfirmText = ref("整批撤回");
const restoreBlocked = ref("");
const previewData = ref<Record<string, unknown> | null>(null);
const previewLoading = ref(false);
const previewError = ref("");
const restoreLoading = ref(false);
const restoreError = ref("");
const restoreResult = ref<MaintenanceRestoreResult | null>(null);

function previewOf(): Record<string, unknown> | null {
  return previewData.value;
}

async function previewRestore(target: { batch_id?: string; item_id?: string }) {
  previewLoading.value = true;
  previewError.value = "";
  previewData.value = null;
  try {
    const result = await api.previewMaintenanceRestore(target);
    const preview = result.preview;
    // 服务端明确拒绝（ok=false）：这是「读不到可信预览」，不是「确实没有可撤回的内容」。
    if (preview && preview.ok === false) {
      previewData.value = null;
      previewError.value =
        "暂时读不到可撤回的内容：" + (shortText(preview.error) || "服务端没有返回可撤回的影响预览");
      return;
    }
    previewData.value = preview;
  } catch (error) {
    // 读取失败：如实说「暂时读不到」，绝不把读取失败当成「没有可撤回的内容」。
    previewError.value = "暂时读不到可撤回的内容：" + ((error as Error).message || "未知原因");
  } finally {
    previewLoading.value = false;
  }
}

function openRestoreDialog(
  target: { batch_id?: string; item_id?: string },
  title: string,
  confirmText: string,
) {
  if (restoreLoading.value) return;
  restoreError.value = "";
  restoreBlocked.value = "";
  restoreResult.value = null;
  const modalId = target.batch_id
    ? "memory-batch-restore:" + target.batch_id
    : "memory-item-restore:" + (target.item_id || "");
  restoreModalId.value = modalId;
  const allowed = restoreArbiter.open({ id: modalId, kind: "recovery", dismissible: true });
  if (!allowed) {
    restoreBlocked.value = "另一个中央弹窗正在处理，先完成或关闭它，再执行恢复。";
    return;
  }
  restoreTarget.value = target;
  restoreTargetLabel.value = target.batch_id ? "批次 " + target.batch_id : "事项 " + (target.item_id || "");
  restoreDialogTitle.value = title;
  restoreDialogConfirmText.value = confirmText;
  restoreDialogOpen.value = true;
  void previewRestore(target);
}

function closeRestoreDialog() {
  if (restoreLoading.value) return;
  restoreDialogOpen.value = false;
  restoreError.value = "";
  restoreBlocked.value = "";
  restoreTarget.value = null;
}

async function confirmRestore() {
  const target = restoreTarget.value;
  if (!target) return;
  restoreLoading.value = true;
  restoreError.value = "";
  try {
    restoreResult.value = await api.restoreMaintenance({ ...target, confirm: true });
    emit("changed");
    await loadBatches();
    if (activeBatchId.value) await loadDetail(activeBatchId.value);
    restoreDialogOpen.value = false;
  } catch (error) {
    // 失败留在弹窗里：真实原因 + 可重试，不静默、也不假装恢复完成。
    restoreError.value = "恢复没有完成：" + ((error as Error).message || "未知原因");
  } finally {
    restoreLoading.value = false;
  }
}

const previewItems = computed<{ label: string; tone: string; title: string; reason: string }[]>(() => {
  const data = previewOf();
  if (!data) return [];
  const operations = entriesOf(data.operations);
  if (operations.length) {
    return operations.map((operation) => {
      const reasons = stringList(operation.reasons);
      const product = isRecord(operation.product) ? operation.product : {};
      const productReason = shortText(product.reason);
      const action = shortText(operation.action) || "操作";
      const operationId = shortText(operation.operation_id) || "未标注操作";
      const rawReason = (reasons.length ? reasons : productReason ? [productReason] : []).join("；");
      const reason = humanizeRestoreReason(operationId, rawReason);
      const ids = targetIdsOf(operation.targets) || shortText(product.id);
      let label = "不可安全恢复";
      let tone = "quiet";
      if (operation.restoreable === true) {
        label = "可恢复";
        tone = "ok";
      } else if (operation.already_restored === true) {
        label = "已恢复过";
        tone = "info";
      } else if (operation.partial === true) {
        label = "部分可恢复";
        tone = "warn";
      }
      return {
        label,
        tone,
        // 主信息是知识名称；只有预览与操作记录都拿不到内容时，才退回动作 + 内部 id。
        title: readableOperationTitle(operationId) || action + " · " + operationId + (ids ? " · " + ids : ""),
        reason,
      };
    });
  }
  return entriesOf(data.items).map((entry) => {
    const restorable = entry.restorable !== false;
    const id =
      shortText(entry.knowledge_id) || shortText(entry.id) || shortText(entry.result_id) || "未标注对象";
    const content = shortText(entry.content);
    return {
      label: restorable ? "可恢复" : "不可安全恢复",
      tone: restorable ? "ok" : "quiet",
      // 有内容就用内容当主信息；没有内容才退回 id（不把裸 id 当主信息）。
      title: content || id,
      reason: shortText(entry.reason),
    };
  });
});

const previewSummary = computed(() => {
  const data = previewOf();
  if (!data) return "";
  const parts: string[] = [];
  const restoreable = typeof data.restoreable_count === "number" ? data.restoreable_count : null;
  const partial = typeof data.partial_count === "number" ? data.partial_count : null;
  const blocked = typeof data.blocked_count === "number" ? data.blocked_count : null;
  const already = typeof data.already_restored_count === "number" ? data.already_restored_count : null;
  if (restoreable !== null) parts.push("可恢复 " + restoreable);
  if (partial !== null && partial > 0) parts.push("部分 " + partial);
  if (blocked !== null) parts.push("不可恢复 " + blocked);
  if (already !== null && already > 0) parts.push("已恢复过 " + already);
  if (!parts.length) {
    parts.push("可恢复 " + previewItems.value.filter((item) => item.tone === "ok").length);
    parts.push("不可恢复 " + previewItems.value.filter((item) => item.tone === "quiet").length);
  }
  return parts.join(" · ");
});
const previewWarnings = computed(() => stringList(previewOf()?.warnings));

/** 弹窗里的受影响条目：预览给可恢复性，批次操作记录给修改前后内容。 */
const restoreDialogItems = computed<RestoreDialogItem[]>(() => {
  const operations = entriesOf(previewData.value?.operations);
  const records = detail.value?.operations || [];
  const items = detail.value?.items || [];
  return previewItems.value.map((entry, index) => {
    const operation = operations[index] ?? {};
    const operationId = shortText(operation.operation_id) || "第 " + (index + 1) + " 项";
    const record =
      records.find((item) => String(item.id || "") === operationId) ||
      items.find((item) => String(item.id || "") === operationId);
    const beforeText = snapshotsOf(record?.before);
    const afterText = snapshotsOf(record?.after);
    return {
      key: operationId,
      label: entry.label,
      tone: entry.tone,
      title: entry.title,
      reason: entry.reason,
      before: beforeText,
      after: afterText,
    };
  });
});

const RESTORE_STATUS_LABELS: Record<string, string> = {
  rolled_back: "已撤回（全部完成）",
  restored: "恢复已完成",
  already_rolled_back: "此前已撤回",
  already_restored: "此前已恢复",
  rollback_partial: "部分恢复",
  partial: "部分恢复",
  nothing_to_restore: "没有可撤回的内容",
  failed: "恢复失败",
};

interface SnapshotEntry {
  id: string;
  content: string;
}
interface OperationContext {
  action: string;
  at: string;
  /** 操作涉及的快照内容（用户可辨认的知识名称） */
  names: string[];
  /** 快照 id → 内容：把服务端原因里的裸 id 换成人能读懂的名称 */
  byId: Map<string, string>;
}
interface RestoreResultEntry {
  key: string;
  label: string;
  tone: string;
  /** 主信息是知识名称（来自预览 / 批次操作记录的真实内容），不是裸 id */
  title: string;
  reason: string;
  detail: {
    operationId: string;
    status: string;
    restoredIds: string;
    blockedIds: string;
    at: string;
  };
}
interface RestoreResultView {
  label: string;
  tone: string;
  restoredCount: number;
  blockedCount: number;
  note: string;
  entries: RestoreResultEntry[];
}

function snapshotEntriesOf(value: unknown): SnapshotEntry[] {
  return entriesOf(value)
    .map((entry) => ({ id: shortText(entry.id) || shortText(entry.knowledge_id), content: shortText(entry.content) }))
    .filter((entry) => Boolean(entry.id || entry.content));
}

/** operation_id → 可读上下文：来自批次操作记录与恢复预览（都只消费服务端真实字段）。 */
const operationContext = computed<Map<string, OperationContext>>(() => {
  const map = new Map<string, OperationContext>();
  const push = (operationId: string, action: string, at: string, snapshots: SnapshotEntry[]) => {
    if (!operationId) return;
    const existing = map.get(operationId);
    const byId = new Map(existing?.byId || []);
    for (const snapshot of snapshots) {
      if (snapshot.id && snapshot.content) byId.set(snapshot.id, snapshot.content);
    }
    const names = [...(existing?.names || []), ...snapshots.map((snapshot) => snapshot.content)].filter(Boolean);
    map.set(operationId, {
      action: action || existing?.action || "",
      at: at || existing?.at || "",
      names: [...new Set(names)],
      byId,
    });
  };
  for (const record of detail.value?.operations || []) {
    const before = snapshotEntriesOf(record.before);
    const after = snapshotEntriesOf(record.after);
    // 主信息用「恢复前的知识内容」（真正会被恢复的对象）；after 只用于把原因里的 id 换成名称。
    push(String(record.id || ""), record.action || "", record.created_at || "", before);
    const current = map.get(String(record.id || ""));
    if (current) {
      for (const snapshot of after) {
        if (snapshot.id && snapshot.content) current.byId.set(snapshot.id, snapshot.content);
      }
    }
  }
  for (const plan of entriesOf(previewOf()?.operations)) {
    const snapshots = entriesOf(plan.targets).flatMap((target) =>
      snapshotEntriesOf(isRecord(target.before) ? [target.before] : []),
    );
    push(shortText(plan.operation_id), shortText(plan.action), "", snapshots);
  }
  return map;
});

/** 把服务端原因里的裸 id 换成人能读懂的知识名称（映射不到就保留原样）。 */
function humanizeRestoreReason(operationId: string, reason: string): string {
  const context = operationContext.value.get(operationId);
  if (!context || !reason) return reason;
  return reason
    .split(/[；;]/)
    .map((part) => {
      const trimmed = part.trim();
      const index = trimmed.search(/[：:]/);
      if (index > 0) {
        const rawId = trimmed.slice(0, index).trim();
        const content = context.byId.get(rawId);
        if (content) return content + trimmed.slice(index);
      }
      return trimmed;
    })
    .filter(Boolean)
    .join("；");
}

/** 可辨认的操作标题：知识名称（来自预览 / 操作记录），拿不到才返回空串让调用方兜底。 */
function readableOperationTitle(operationId: string): string {
  if (!operationId) return "";
  const names = operationContext.value.get(operationId)?.names || [];
  const unique = [...new Set(names.filter(Boolean))];
  if (!unique.length) return "";
  const head = unique.slice(0, 3).join("；");
  return unique.length > 3 ? head + " 等 " + unique.length + " 条" : head;
}

/** 结果条目主信息：知识名称，拿不到才说「未标注整理对象」（不把裸 id 当主信息）。 */
function restoreEntryTitle(operationId: string): string {
  return readableOperationTitle(operationId) || "未标注整理对象";
}

function stringIdList(value: unknown): string {
  return stringList(value).join("、");
}

/**
 * 恢复结果的可读视图：知识名称 + 已恢复数量 + 未恢复原因；
 * 原始 id / 状态枚举 / 时间戳放进默认收起的展开详情。
 */
const restoreResultView = computed<RestoreResultView>(() => {
  const view: RestoreResultView = {
    label: "",
    tone: "quiet",
    restoredCount: 0,
    blockedCount: 0,
    note: "",
    entries: [],
  };
  const result = restoreResult.value;
  if (!result) return view;
  const status = String(result.status || "");
  view.label = RESTORE_STATUS_LABELS[status] || (result.ok ? "恢复结果" : "恢复未完成");
  view.tone = ["rolled_back", "restored", "already_rolled_back", "already_restored", "nothing_to_restore"].includes(status)
    ? "ok"
    : status
      ? "warn"
      : result.ok
        ? "ok"
        : "warn";
  view.restoredCount = typeof result.restored === "number" ? result.restored : 0;
  view.blockedCount = typeof result.blocked === "number" ? result.blocked : 0;

  const data = result.result ?? undefined;
  const topOperations = result.operations ?? data?.operations;
  const restored: RestoreResultEntry[] = [];
  const blocked: RestoreResultEntry[] = [];
  for (const operation of entriesOf(topOperations)) {
    const operationId = shortText(operation.operation_id);
    if (!operationId) continue;
    const operationStatus = shortText(operation.status);
    const reasons = stringList(operation.reasons).join("；");
    const entry: RestoreResultEntry = {
      key: operationId,
      label: "无法安全恢复",
      tone: "quiet",
      title: restoreEntryTitle(operationId),
      reason: humanizeRestoreReason(operationId, reasons),
      detail: {
        operationId,
        status: operationStatus,
        restoredIds: stringIdList(operation.restored_knowledge_ids),
        blockedIds: stringIdList(operation.blocked_knowledge_ids),
        at: operationContext.value.get(operationId)?.at || "",
      },
    };
    if (operationStatus === "restored") {
      restored.push({ ...entry, label: "已恢复", tone: "ok" });
    } else if (operationStatus === "already_restored") {
      restored.push({ ...entry, label: "此前已恢复过", tone: "ok" });
    } else if (operationStatus === "partial") {
      blocked.push({ ...entry, label: "部分恢复", tone: "warn" });
    } else if (operationStatus === "failed") {
      blocked.push({ ...entry, label: "恢复失败", tone: "warn" });
    } else {
      blocked.push(entry);
    }
  }
  if (!restored.length && !blocked.length) {
    for (const entry of entriesOf(result.blocked_reasons)) {
      const operationId = shortText(entry.operation_id);
      if (!operationId) continue;
      blocked.push({
        key: operationId,
        label: "无法安全恢复",
        tone: "quiet",
        title: restoreEntryTitle(operationId),
        reason: humanizeRestoreReason(operationId, stringList(entry.reasons).join("；")),
        detail: {
          operationId,
          status: "blocked",
          restoredIds: "",
          blockedIds: "",
          at: operationContext.value.get(operationId)?.at || "",
        },
      });
    }
  }
  if (!view.restoredCount) view.restoredCount = restored.length;
  if (!view.blockedCount) view.blockedCount = blocked.length;
  view.entries = [...restored, ...blocked];

  // 区分「已撤回」「部分恢复」「失败」：一条都没恢复时不能写成部分恢复。
  if (status === "rollback_partial" || status === "partial") {
    if (view.restoredCount > 0) {
      view.label = "部分恢复";
    } else if (view.entries.some((entry) => entry.label === "恢复失败")) {
      view.label = "恢复失败";
    } else {
      view.label = "未能恢复";
    }
  }

  if (status === "already_rolled_back") {
    view.note = "服务端确认这次整理此前已经撤回，本次没有再次执行任何撤回写入。";
  } else if (status === "nothing_to_restore") {
    view.note = "服务端确认没有可撤回的内容，本次没有执行任何写入。";
  } else if (status === "rollback_partial" || status === "partial") {
    view.note =
      view.restoredCount > 0
        ? "有部分内容没有恢复：下面逐项列出未恢复原因；历史不会被删除，已恢复的项已如实计入。"
        : "本次没有任何内容被恢复：下面逐项列出真实原因；历史不会被删除，也不会覆盖你后来的修改。";
  } else {
    view.note = "恢复通过新的版本记录实现，历史不会被删除。";
  }
  return view;
});

/** 恢复是否「全部完成」：部分失败时也返回 ok=true + status=rollback_partial，必须如实说。 */
const restoreCompleted = computed(() => restoreResultView.value.tone === "ok");

/** 预览是否已经确认「没有可恢复项」：此时只展示历史结果，不提供再次执行的确认入口。 */
const previewRestorableCount = computed(() => {
  const data = previewOf();
  const num = (key: string) => (typeof data?.[key] === "number" ? (data[key] as number) : 0);
  return num("restoreable_count") + num("partial_count");
});
const previewHistoryOnly = computed(() => {
  if (!previewOf() || previewLoading.value || previewError.value) return false;
  return previewRestorableCount.value === 0;
});
const restoreHistoryNote = computed(() => {
  const batchStatus = detail.value?.batch.status || "";
  if (batchStatus === "rolled_back" || batchStatus === "rollback_partial") {
    return "这次整理已经撤回，没有可再次恢复的内容；下面是服务端返回的历史结果，不会执行任何写入。";
  }
  return "服务端确认没有可再次执行恢复的内容；下面是真实结果，不会执行任何写入。";
});
const batchRolledBack = computed(() => detail.value?.batch.status === "rolled_back");

// -- 生命周期 ---------------------------------------------------------------

function openFocusBatch() {
  const id = String(props.focusBatchId || "");
  if (!id) return;
  if (!batches.value.some((batch) => batch.id === id)) return;
  openBatch(id);
}

watch(
  () => props.focusBatchId,
  (id) => {
    if (id) openFocusBatch();
  },
);
watch(
  () => batches.value.length,
  () => openFocusBatch(),
);

async function bootstrap() {
  await loadBatches();
  openFocusBatch();
}

void bootstrap();

onBeforeUnmount(() => {
  if (restoreDialogOpen.value && restoreTarget.value) {
    const target = restoreTarget.value;
    restoreArbiter.close(
      target.batch_id ? "memory-batch-restore:" + target.batch_id : "memory-item-restore:" + (target.item_id || ""),
    );
  }
});
</script>

<template>
  <div
    ref="rootRef"
    class="mrecords"
    v-qio-scrollbar
    data-qio-fit
    data-scroll="records-list"
  >
    <!-- 列表 -->
    <div v-show="view === 'list'" class="mr-list-view" data-records-list>
      <div class="mr-head">
        <span class="mr-title serif">整理记录</span>
        <span class="mr-count mono">{{ listTotal }} 次</span>
        <span class="spacer"></span>
        <button class="qio-btn mini quiet mr-refresh" type="button" :disabled="listLoading" @click="loadBatches">
          {{ listLoading ? "刷新中…" : "刷新" }}
        </button>
      </div>
      <p class="hint">整理批次是后台整理的真实运行记录；恢复前先预览影响。</p>

      <p v-if="restoreBlocked" class="mr-warn" role="status">{{ restoreBlocked }}</p>

      <div v-if="listLoading && !listLoaded" class="hint">正在加载整理记录…</div>
      <div v-else-if="listError" class="mr-error" role="alert">
        <span>{{ listError }}</span>
        <button class="qio-btn mini" type="button" @click="loadBatches">重试</button>
      </div>
      <p v-else-if="!listAvailable" class="mr-warn" role="status">
        {{ listWarnings[0] || "整理记录不可用：没有可展示的批次，也不伪造数据。" }}
      </p>
      <template v-else>
        <p v-if="listTruncated" class="hint">
          共 {{ listTotal }} 个批次，当前只显示 {{ batches.length }} 个（服务端已截断）。
        </p>
        <p v-if="!batches.length" class="hint mr-empty">
          尚无整理批次：后台整理还没有运行过，或运行记录不可用。
        </p>
        <ul class="mr-batches">
          <li
            v-for="batch in batches"
            :key="batch.id"
            class="mr-batch qio-card qio-card--quiet"
            :data-record-id="batch.id"
            role="button"
            tabindex="0"
            :class="batch.status"
            @click="openBatch(batch.id)"
            @keydown.enter.prevent="openBatch(batch.id)"
            @keydown.space.prevent="openBatch(batch.id)"
          >
            <!-- 层级：状态 + 触发原因（左）/ 时间（右）→ 统计一行 → 错误（有才显示），全部左对齐 -->
            <div class="mr-batch-head">
              <span class="qio-state mr-batch-status" :class="[batch.status, batchTone(batch.status)]">
                {{ statusLabel(batch.status) }}
              </span>
              <span class="mr-batch-trigger">{{ triggerLabel(batch.trigger) }}</span>
              <span class="mr-batch-spacer"></span>
              <span class="mr-batch-time mono">{{ batchTimes(batch) }}</span>
            </div>
            <span class="mr-batch-stats">{{ statsText(batch) }}</span>
            <p v-if="batch.error" class="mr-item-error" role="alert">批次错误：{{ batch.error }}</p>
          </li>
        </ul>
      </template>
    </div>

    <!-- 详情 -->
    <div v-show="view === 'detail'" class="mr-detail" data-record-detail>
      <div class="mr-detail-head">
        <button class="qio-btn mini quiet mr-back" type="button" data-records-back @click="backToList">
          <QIcon name="chevron-left" :size="12" /> 返回
        </button>
        <span class="mr-detail-title serif">整理详情</span>
        <span class="spacer"></span>
        <span class="mr-id mono">{{ activeBatchId }}</span>
      </div>

      <p v-if="restoreBlocked" class="mr-warn" role="status">{{ restoreBlocked }}</p>

      <div v-if="detailLoading" class="hint">正在加载整理详情…</div>
      <div v-else-if="detailError" class="mr-error" role="alert">
        <span>{{ detailError }}</span>
        <button class="qio-btn mini" type="button" @click="activeBatchId && loadDetail(activeBatchId)">重试</button>
      </div>
      <template v-else-if="detail">
        <!-- 整理摘要 -->
        <section class="mr-summary qio-card" aria-label="整理摘要">
          <div class="mr-head">
            <span class="qio-state" :class="batchTone(detail.batch.status)">
              {{ statusLabel(detail.batch.status) }}
            </span>
            <span class="mr-trigger">{{ triggerLabel(detail.batch.trigger) }}</span>
            <span class="spacer"></span>
            <span v-if="detail.batch.model" class="mr-model mono">{{ detail.batch.model }}</span>
          </div>
          <p class="mr-stats">{{ statsText(detail.batch) }}</p>
          <p class="mr-times mono">{{ batchTimes(detail.batch) }}</p>
          <p v-if="detail.batch.error" class="mr-item-error" role="alert">
            批次错误：{{ detail.batch.error }}
          </p>
          <div class="mr-actions">
            <button
              class="qio-btn mini mr-preview-batch"
              type="button"
              :disabled="restoreLoading"
              @click="
                openRestoreDialog(
                  { batch_id: detail.batch.id },
                  batchRolledBack ? '这次整理已经撤回' : '整批撤回这次记忆整理？',
                  batchRolledBack ? '查看撤回情况' : '整批撤回',
                )
              "
            >{{ batchRolledBack ? "查看撤回情况…" : "整批撤回…" }}</button>
            <span class="hint">撤回不删除历史；整理之后又被你改过的部分会被拒绝并说明原因。已撤回且没有可恢复项时只显示历史结果。</span>
          </div>
        </section>

        <div v-if="restoreError" class="mr-error" role="alert">
          <span>{{ restoreError }}</span>
        </div>

        <!-- 恢复结果（可读状态，不是原始日志）：知识名称 + 数量 + 未恢复原因，原始信息默认收起 -->
        <section
          v-if="restoreResult"
          class="mr-result qio-card"
          aria-label="恢复结果"
          data-restore-result
        >
          <div class="mr-head">
            <span
              class="qio-state"
              :class="restoreResultView.tone"
              data-restore-result-label
            >
              {{ restoreResultView.label }}
            </span>
            <span class="mr-result-counts" data-restore-result-counts>
              已恢复 {{ restoreResultView.restoredCount }} 条 · 未恢复 {{ restoreResultView.blockedCount }} 条
            </span>
          </div>
          <p class="hint" data-restore-result-note>{{ restoreResultView.note }}</p>
          <ul v-if="restoreResultView.entries.length" class="mr-result-list">
            <li
              v-for="entry in restoreResultView.entries"
              :key="entry.key"
              class="mr-result-entry"
              :data-restore-entry="entry.key"
            >
              <div class="mr-result-entry-head">
                <span class="qio-state" :class="entry.tone">{{ entry.label }}</span>
                <span class="mr-result-title" data-restore-entry-title>{{ entry.title }}</span>
              </div>
              <p v-if="entry.reason" class="mr-reason mr-result-reason">{{ entry.reason }}</p>
              <details class="mr-result-detail" data-restore-entry-detail>
                <summary>查看原始结果</summary>
                <div class="mr-result-detail-body">
                  <p class="hint mono">操作 {{ entry.detail.operationId }} · 状态 {{ entry.detail.status }}</p>
                  <p v-if="entry.detail.restoredIds" class="hint mono">
                    已恢复对象 {{ entry.detail.restoredIds }}
                  </p>
                  <p v-if="entry.detail.blockedIds" class="hint mono">
                    未恢复对象 {{ entry.detail.blockedIds }}
                  </p>
                  <p v-if="entry.detail.at" class="hint mono">时间（原样） {{ entry.detail.at }}</p>
                </div>
              </details>
            </li>
          </ul>
          <p v-else class="hint">服务端没有返回可展示的逐项结果。</p>
        </section>

        <!-- 受影响条目：点开才展开修改前后对比 -->
        <section class="mr-affected" aria-label="受影响条目">
          <p class="mr-label">受影响条目（{{ affectedItems.length }}）</p>
          <p v-if="!affectedItems.length" class="hint">这次整理没有留下受影响条目记录。</p>
          <ul class="mr-items">
            <li
              v-for="item in affectedItems"
              :key="item.id"
              class="mr-item"
              :data-record-item-id="item.id"
            >
              <button
                class="mr-item-head"
                type="button"
                :aria-expanded="expandedItemId === item.id"
                @click="toggleItem(item.id)"
              >
                <span class="qio-tag">{{ item.action }}</span>
                <span v-if="item.status" class="qio-state quiet">{{ operationStatusLabel(item.status) }}</span>
                <span v-if="item.targetIds" class="mr-item-targets mono">{{ item.targetIds }}</span>
                <span v-if="item.at" class="mr-item-time mono">{{ item.at }}</span>
                <span class="spacer"></span>
                <QIcon :name="expandedItemId === item.id ? 'chevron-down' : 'chevron-right'" :size="12" />
              </button>
              <p v-if="item.reason" class="mr-reason">{{ item.reason }}</p>
              <div v-if="expandedItemId === item.id" class="mr-diff" data-record-diff>
                <div class="mr-diff-part">
                  <span class="mr-label">修改前</span>
                  <p class="mr-diff-text">{{ item.before || "（这次操作没有留下修改前快照）" }}</p>
                </div>
                <div class="mr-diff-part">
                  <span class="mr-label">修改后</span>
                  <p class="mr-diff-text">{{ item.after || "（这次操作没有留下修改后快照）" }}</p>
                </div>
                <p v-if="item.resultId" class="hint mono">结果条目 {{ item.resultId }}</p>
              </div>
            </li>
          </ul>
        </section>

        <!-- 整理事项（维护留下的问题/处理结果） -->
        <section v-if="maintenanceItems.length" class="mr-maintenance" aria-label="整理事项">
          <p class="mr-label">整理事项（{{ maintenanceItems.length }}）</p>
          <ul class="mr-items">
            <li v-for="item in maintenanceItems" :key="item.id" class="mr-item" :data-record-item-id="item.id">
              <div class="mr-item-head static">
                <span class="qio-tag">{{ item.kind }}</span>
                <span class="qio-state quiet">{{ itemStatusLabel(item.status) }}</span>
                <span v-if="item.at" class="mr-item-time mono">{{ item.at }}</span>
              </div>
              <p v-if="item.question" class="mr-question">{{ item.question }}</p>
              <p v-if="item.reason" class="hint">原因：{{ item.reason }}</p>
              <p v-if="item.error" class="mr-item-error" role="alert">错误：{{ item.error }}</p>
              <div class="mr-item-restore">
                <button
                  class="qio-btn mini quiet mr-item-restore-open"
                  type="button"
                  :disabled="restoreLoading"
                  @click="openRestoreDialog({ item_id: item.id }, '恢复这一项的维护修改？', '恢复这一项')"
                >恢复这一项…</button>
                <span class="hint">先用中央弹窗预览影响；之后被你改过的部分会被拒绝恢复。</span>
              </div>
            </li>
          </ul>
        </section>
      </template>
    </div>

    <!-- 撤回/恢复：复用既有中央弹窗（预览 + 确认） -->
    <MaintenanceRestoreDialog
      :open="restoreDialogOpen"
      :modal-id="restoreModalId"
      :title="restoreDialogTitle"
      :confirm-text="restoreDialogConfirmText"
      :target-label="restoreTargetLabel"
      :summary="previewSummary"
      :items="restoreDialogItems"
      :warnings="previewWarnings"
      :preview-loading="previewLoading"
      :preview-error="previewError"
      :restoring="restoreLoading"
      :restore-error="restoreError"
      :read-only="previewHistoryOnly"
      :read-only-note="restoreHistoryNote"
      @confirm="confirmRestore"
      @cancel="closeRestoreDialog"
      @retry-preview="restoreTarget && previewRestore(restoreTarget)"
    />
  </div>
</template>

<style scoped>
/* 面板正文层：只有这一层纵向滚动（列表与详情共用同一个阅读区）。 */
.mrecords {
  display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px;
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto; overflow-x: clip; overscroll-behavior: contain;
}
.mr-list-view, .mr-detail { display: flex; flex-direction: column; gap: 8px; }
.mr-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mr-head .spacer { flex: 1; }
.mr-detail-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mr-detail-head .spacer { flex: 1; }
.mr-title { font-size: 15px; color: var(--text-strong); }
.mr-detail-title { font-size: 14px; color: var(--text-secondary); }
.mr-count { font-size: 11.5px; color: var(--text-muted); }
.mr-id { font-size: 11px; color: var(--text-faint); }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; }
.hint { font-size: 12px; color: var(--text-muted); }
.mr-batches, .mr-items, .mr-result-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
/* 列表卡是列方向：显式 stretch，避免全局行式类把内容水平居中/缩窄。 */
.mr-batch {
  display: flex; flex-direction: column; align-items: stretch; justify-content: flex-start;
  gap: 4px; width: 100%; padding: var(--sp-3); border-radius: var(--qio-ui-radius);
  cursor: pointer; text-align: left;
  transition: background var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1);
}
.mr-batch:hover { background: var(--layer-hover); }
.mr-batch:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.mr-batch-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.mr-batch-spacer { flex: 1; min-width: 0; }
.mr-batch-trigger { font-size: 12.5px; color: var(--text-primary); min-width: 0; overflow-wrap: anywhere; }
.mr-batch-time, .mr-batch-stats { font-size: 11.5px; color: var(--text-muted); }
.mr-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: var(--qio-ui-radius); font-size: 12px;
  border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger);
}
.mr-warn { margin: 0; font-size: 12px; line-height: 1.6; color: var(--warning); }
.mr-empty { margin: 0; }
.mr-summary { display: flex; flex-direction: column; gap: 8px; padding: var(--sp-4); }
.mr-trigger { font-size: 13px; color: var(--text-primary); }
.mr-model { font-size: 11.5px; color: var(--text-muted); }
.mr-stats { margin: 0; font-size: 12.5px; color: var(--text-secondary); }
.mr-times { margin: 0; font-size: 11.5px; color: var(--text-muted); overflow-wrap: anywhere; }
.mr-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mr-result { display: flex; flex-direction: column; gap: 6px; padding: var(--sp-4); }
.mr-status-text { font-size: 11px; color: var(--text-muted); }
.mr-result-counts { font-size: 11.5px; color: var(--text-muted); }
.mr-result-list { display: flex; flex-direction: column; gap: 8px; }
.mr-result-entry { display: flex; flex-direction: column; gap: 3px; }
.mr-result-entry-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.mr-result-title {
  font-size: 12.5px; line-height: 1.65; color: var(--text-primary);
  overflow-wrap: anywhere;
}
.mr-result-reason { overflow-wrap: anywhere; }
.mr-result-detail {
  font-size: 11.5px; color: var(--text-muted);
  border-top: 1px solid var(--border-subtle); padding-top: 4px;
}
.mr-result-detail > summary {
  cursor: pointer; color: var(--text-secondary); font-size: 11.5px;
  list-style-position: inside;
}
.mr-result-detail > summary:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.mr-result-detail-body { display: flex; flex-direction: column; gap: 2px; padding: 4px 0 0 14px; }
.mr-label { font-size: 11px; color: var(--text-secondary); letter-spacing: 0.03em; }
.mr-affected, .mr-maintenance { display: flex; flex-direction: column; gap: 6px; }
.mr-item {
  display: flex; flex-direction: column; align-items: stretch; justify-content: flex-start;
  gap: 4px; padding: 8px 10px; border: 1px solid var(--border-subtle);
  border-radius: var(--qio-ui-radius); background: var(--bg-surface); text-align: left;
}
.mr-item-head {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  background: none; border: none; padding: 0; color: inherit; font: inherit; cursor: pointer; text-align: left;
}
.mr-item-head.static { cursor: default; }
.mr-item-head:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.mr-item-head .spacer { flex: 1; }
.mr-item-targets, .mr-item-time { font-size: 11px; color: var(--text-muted); overflow-wrap: anywhere; }
.mr-reason { margin: 0; font-size: 12px; line-height: 1.6; color: var(--text-secondary); }
.mr-diff { display: flex; flex-direction: column; gap: 6px; padding: 8px; border-radius: var(--qio-ui-radius); background: var(--bg-inset); }
.mr-diff-part { display: flex; flex-direction: column; gap: 2px; }
.mr-diff-text { margin: 0; font-size: 12px; line-height: 1.65; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
.mr-item-restore { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mr-question { margin: 0; font-size: 12.5px; line-height: 1.65; color: var(--text-primary); white-space: pre-wrap; }
.mr-item-error { margin: 0; font-size: 12px; color: var(--danger); }
.mono { font-family: var(--mono); }
</style>