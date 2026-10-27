<script setup lang="ts">
/**
 * 待处理（B · 管理流程）。
 *
 * 合并**两类真实来源**、覆盖基线全部事项类型：
 * 1. api.listMemoryPending() —— 知识建议（knowledge state=pending_review）与
 *    记忆整理问题（maintenance_items kind=ask）；
 * 2. services/entityCandidatesApi.ts —— 实体更新候选（被保护而没落的自动值）。
 *
 * 交互：列表 → 事项详情（判断所需内容 / 来源 / before-after / 依据）→ 动作 → 回列表。
 * 列表用 v-show 保活，完成一条后回到**原来的列表位置**。
 *
 * 几条底线（都是改版硬要求）：
 * - 数量真实：列表数量与 stores/planetPending.ts 的角标都来自上面两个接口的 total；
 * - 防重复提交：同一事项同一时刻只有一个请求在飞（不同事项互不影响）；
 * - 失败保留输入与上下文：回答草稿、修改后确认的正文都留在原地；
 * - 过期内容不提交：「修改后确认」走既有原子接口 submitKnowledgeApprovalRequest，
 *   带 expected_*，409 就刷新真实状态并说明，**绝不**先确认原文再改；
 * - 动作只在详情里出现，列表只负责「有什么在等我决定」。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { getActivePinia } from "pinia";
import { api, type PendingKnowledgeRef, type PendingItemView } from "../../services/api";
import {
  adoptCandidate,
  dismissCandidate,
  isCandidateConflict,
  listAllCandidates,
  type EntityCandidateView,
} from "../../services/entityCandidatesApi";
import {
  classifyKnowledgeSubmitError,
  submitKnowledgeApprovalRequest,
  useUnifiedApprovalsStore,
} from "../../stores/unifiedApprovals";
import { usePlanetPendingStore } from "../../stores/planetPending";
import QSelect from "../ui/QSelect.vue";
import QIcon from "../ui/QIcon.vue";
import { vQioScrollbar } from "../../composables/useQioScrollbars";
import { useActionFeedback } from "../../composables/useActionFeedback";

const props = withDefaults(defineProps<{ focusId?: string | null }>(), { focusId: null });
const emit = defineEmits<{ changed: [] }>();

type PendingKind = "knowledge_proposal" | "maintenance_question" | "entity_candidate";

interface PendingRow {
  /** 稳定身份：知识/维护事项 = 真实 id；实体候选 = (entity_id, candidate_id) 复合键。 */
  key: string;
  /** 界面与 focusId 使用的 id（知识/维护事项 id；实体候选 candidate_id）。 */
  id: string;
  kind: PendingKind;
  status: string;
  title: string;
  createdAt: string;
  /** 来源一句话（知识来源 / 实体卡名）；拿不到就留空，不编造。 */
  sourceLabel: string;
  memory: PendingItemView | null;
  candidate: EntityCandidateView | null;
}

const KIND_LABELS: Record<PendingKind, string> = {
  knowledge_proposal: "知识建议",
  maintenance_question: "整理问题",
  entity_candidate: "实体更新候选",
};
const STATUS_LABELS: Record<string, string> = {
  awaiting_answer: "待回答",
  awaiting_approval: "待确认",
  processing: "处理中",
  processed: "已处理",
  deferred: "稍后处理",
  rejected: "已拒绝",
  failed: "失败",
  blocked: "不可采纳",
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

// -- 读取 -------------------------------------------------------------------

const rows = ref<PendingRow[]>([]);
const loading = ref(false);
const loadError = ref("");
const warnings = ref<string[]>([]);
const memoryTotal = ref(0);
const candidateTotal = ref(0);
const truncated = ref(false);
const maintenanceAvailable = ref(true);
const feedback = useActionFeedback();
const pendingStore = getActivePinia() ? usePlanetPendingStore() : null;
function refreshPendingCount() {
  void pendingStore?.refresh();
}

/** 面板级成功提示：事项处理完会从列表消失，条目内的反馈会跟着消失。 */
const notice = ref("");
let noticeTimer: ReturnType<typeof setTimeout> | null = null;
function announce(text: string) {
  notice.value = text;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    notice.value = "";
  }, 5000);
}

const kindFilter = ref("");
const sourceFilter = ref("");
const kindOptions = [
  { value: "", label: "全部类型" },
  { value: "knowledge_proposal", label: "知识建议" },
  { value: "maintenance_question", label: "整理问题" },
  { value: "entity_candidate", label: "实体更新候选" },
];
const sourceOptions = computed(() => {
  const labels = new Set<string>();
  for (const row of rows.value) {
    if (row.sourceLabel) labels.add(row.sourceLabel);
  }
  return [
    { value: "", label: "全部来源" },
    ...[...labels].sort().map((label) => ({ value: label, label })),
  ];
});
const filtered = computed(() =>
  rows.value.filter((row) => {
    if (kindFilter.value && row.kind !== kindFilter.value) return false;
    if (sourceFilter.value && row.sourceLabel !== sourceFilter.value) return false;
    return true;
  }),
);
const hasFilters = computed(() => Boolean(kindFilter.value || sourceFilter.value));
function clearFilters() {
  kindFilter.value = "";
  sourceFilter.value = "";
}
/** 真实未处理总数（两类来源的 total 之和；读失败的一侧保留上一次真实值）。 */
const totalCount = computed(() => memoryTotal.value + candidateTotal.value);
const hiddenCount = computed(() => Math.max(0, rows.value.length - filtered.value.length));

function shortText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
}
function contentOf(entry: Record<string, unknown> | undefined): string {
  return entry ? shortText(entry.content) : "";
}
function memorySourceLabel(item: PendingItemView): string {
  if (item.kind === "knowledge_proposal") {
    const ref = item.knowledge[0];
    return ref?.source?.trim() || "";
  }
  return item.question ? "记忆整理" : "";
}
function memoryRowTitle(item: PendingItemView): string {
  if (item.kind === "knowledge_proposal") {
    return item.knowledge[0]?.content || item.title;
  }
  return item.question || item.title;
}
function rowOfMemory(item: PendingItemView): PendingRow {
  return {
    key: "m:" + item.id,
    id: item.id,
    kind: item.kind === "knowledge_proposal" ? "knowledge_proposal" : "maintenance_question",
    status: item.status,
    title: memoryRowTitle(item),
    createdAt: item.created_at || "",
    sourceLabel: memorySourceLabel(item),
    memory: item,
    candidate: null,
  };
}
function rowOfCandidate(c: EntityCandidateView): PendingRow {
  return {
    key: "c:" + c.entity_id + "\u0000" + c.candidate_id,
    id: c.candidate_id,
    kind: "entity_candidate",
    status: c.adoptable ? "awaiting_approval" : "blocked",
    title: c.entity_name + " · " + (c.field_label || c.field),
    createdAt: c.created_at || "",
    sourceLabel: c.entity_name?.trim() || "",
    memory: null,
    candidate: c,
  };
}

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const [memoryResult, candidateResult] = await Promise.allSettled([
      (async () => api.listMemoryPending({ limit: 500 }))(),
      (async () => listAllCandidates(true, 500))(),
    ]);
    const problems: string[] = [];
    let memoryOk = false;
    let candidatesOk = false;
    if (memoryResult.status === "fulfilled") {
      memoryOk = true;
      const listing = memoryResult.value;
      memoryTotal.value = Number.isFinite(listing.total) ? listing.total : listing.items.length;
      maintenanceAvailable.value = listing.maintenance_available !== false;
      truncated.value = Boolean(listing.truncated);
      warnings.value = listing.warnings || [];
    } else {
      problems.push("读取记忆待确认失败：" + ((memoryResult.reason as Error)?.message || "未知原因"));
    }
    if (candidateResult.status === "fulfilled") {
      candidatesOk = true;
      const listing = candidateResult.value;
      candidateTotal.value = Number.isFinite(listing.total) ? listing.total : listing.candidates.length;
    } else {
      problems.push(
        "读取实体更新候选失败：" + ((candidateResult.reason as Error)?.message || "未知原因"),
      );
    }
    if (!memoryOk && !candidatesOk) {
      loadError.value = problems.join(" ");
      return;
    }
    if (problems.length) warnings.value = [...warnings.value, ...problems];
    const nextRows: PendingRow[] = [];
    if (memoryResult.status === "fulfilled") {
      for (const item of memoryResult.value.items) nextRows.push(rowOfMemory(item));
    }
    if (candidateResult.status === "fulfilled") {
      for (const candidate of candidateResult.value.candidates) nextRows.push(rowOfCandidate(candidate));
    }
    nextRows.sort((a, b) => (a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0));
    rows.value = nextRows;
    if (view.value === "detail" && activeKey.value && !nextRows.some((r) => r.key === activeKey.value)) {
      activeKey.value = null;
    }
  } catch (error) {
    loadError.value = "加载待处理事项失败：" + ((error as Error).message || "未知原因");
  } finally {
    loading.value = false;
  }
}

// -- 列表 ↔ 详情 -------------------------------------------------------------

type PanelView = "list" | "detail";
const view = ref<PanelView>("list");
const activeKey = ref<string | null>(null);
const rootRef = ref<HTMLElement | null>(null);
let listScrollTop = 0;
const activeRow = computed<PendingRow | null>(
  () => rows.value.find((row) => row.key === activeKey.value) ?? null,
);

function scrollRootTo(top: number) {
  const root = rootRef.value;
  if (!root) return;
  root.scrollTop = Math.max(0, top);
}

function openDetail(row: PendingRow) {
  if (view.value === "list") listScrollTop = rootRef.value?.scrollTop ?? 0;
  activeKey.value = row.key;
  view.value = "detail";
  feedback.reset();
  void nextTick(() => scrollRootTo(0));
}

function backToList() {
  view.value = "list";
  activeKey.value = null;
  feedback.reset();
  const top = listScrollTop;
  void nextTick(() => scrollRootTo(top));
}

/** 完成一条后：回列表并保留原来的位置（不把用户弹到顶部）。 */
async function afterDone(message: string) {
  announce(message);
  await load();
  backToList();
  emit("changed");
  refreshPendingCount();
  syncUnifiedKnowledgePending();
}

/** 与统一审批入口共享同一份权威数据：处理完让它后台刷新，一处处理两处同步。 */
function syncUnifiedKnowledgePending(): void {
  try {
    const unified = useUnifiedApprovalsStore();
    if (unified.knowledgeLoadedAt !== null) void unified.refreshKnowledgePending();
  } catch {
    /* 统一审批 store 不可用不影响星球本地处理 */
  }
}

// -- 每行的就地状态（忙碌 / 输入 / 提示） ------------------------------------

const busyKeys = ref<string[]>([]);
function isBusy(row: PendingRow): boolean {
  return busyKeys.value.includes(row.key);
}
function markBusy(row: PendingRow) {
  busyKeys.value = [...busyKeys.value, row.key];
}
function clearBusy(row: PendingRow) {
  busyKeys.value = busyKeys.value.filter((k) => k !== row.key);
}
const rowNotice = ref<Record<string, { tone: "err" | "conflict"; text: string }>>({});
function setRowNotice(row: PendingRow, tone: "err" | "conflict", text: string) {
  rowNotice.value = { ...rowNotice.value, [row.key]: { tone, text } };
}
function clearRowNotice(row: PendingRow) {
  const next = { ...rowNotice.value };
  delete next[row.key];
  rowNotice.value = next;
}
function noticeOf(row: PendingRow): { tone: "err" | "conflict"; text: string } | null {
  return rowNotice.value[row.key] ?? null;
}

/** 回答草稿（按事项 key 保存）：失败/稍后处理后再回来仍在。 */
const answers = ref<Record<string, string>>({});
function answerOf(row: PendingRow): string {
  return answers.value[row.key] || "";
}
function setAnswer(row: PendingRow, value: string) {
  answers.value = { ...answers.value, [row.key]: value };
}
/** 「修改后确认」的正文草稿（按事项 key 保存）。 */
const modifyDrafts = ref<Record<string, string>>({});
const modifyingKey = ref<string | null>(null);
function modifyDraftOf(row: PendingRow): string {
  return modifyDrafts.value[row.key] ?? "";
}
function setModifyDraft(row: PendingRow, value: string) {
  modifyDrafts.value = { ...modifyDrafts.value, [row.key]: value };
}
function startModify(row: PendingRow) {
  if (!modifyDrafts.value[row.key]) {
    const knowledge = row.memory?.knowledge[0];
    setModifyDraft(row, contentOf(row.memory?.after[0]) || knowledge?.content || "");
  }
  modifyingKey.value = row.key;
}
function cancelModify(row: PendingRow) {
  modifyingKey.value = null;
  const next = { ...modifyDrafts.value };
  delete next[row.key];
  modifyDrafts.value = next;
}

function knowledgeOf(row: PendingRow): PendingKnowledgeRef | null {
  return row.memory?.knowledge[0] ?? null;
}
function scopeText(knowledge: PendingKnowledgeRef): string {
  return knowledge.scope.length ? knowledge.scope.join("、") : "未指定";
}
function can(row: PendingRow, action: string): boolean {
  return Boolean(row.memory?.actions?.includes(action));
}
function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "（空）";
  if (Array.isArray(value)) return value.length ? value.map((x) => formatValue(x)).join("、") : "（空）";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "（无法显示）";
    }
  }
  const text = String(value);
  return text.trim() ? text : "（空）";
}

/** 409 / 已被处理：刷新真实状态，不提交过期内容。 */
async function refreshAfterConflict(row: PendingRow, text: string) {
  clearRowNotice(row);
  setRowNotice(row, "conflict", text);
  await load();
  refreshPendingCount();
  if (!rows.value.some((r) => r.key === row.key)) {
    // 这条已经被处理掉了：留在详情只会让用户对着空壳操作。
    backToList();
  }
}

// -- 知识建议：确认 / 修改后确认 / 不保存 -----------------------------------

function expectedOf(row: PendingRow) {
  const knowledge = knowledgeOf(row);
  return {
    expectedVersion: knowledge?.version ?? null,
    expectedContent: knowledge?.content ?? null,
    expectedUpdatedAt: row.memory?.updated_at || null,
  };
}

async function decideKnowledge(
  row: PendingRow,
  decision: "approve" | "approve_modified" | "reject",
  content: string | undefined,
  okText: string,
  failText: string,
) {
  const knowledge = knowledgeOf(row);
  if (!knowledge || isBusy(row)) return;
  markBusy(row);
  clearRowNotice(row);
  try {
    const result = await submitKnowledgeApprovalRequest(knowledge.id, decision, {
      content,
      ...expectedOf(row),
    });
    if (result?.already_handled) {
      await afterDone("这条申请此前已经处理过，已刷新真实状态。");
      return;
    }
    await afterDone(okText);
  } catch (error) {
    const info = classifyKnowledgeSubmitError(error);
    if (info.terminal || info.refresh) {
      // 版本/内容冲突或结果未知：先刷新真实状态，保留输入，不自动重发。
      await refreshAfterConflict(row, info.message);
      return;
    }
    setRowNotice(row, "err", failText + "：" + info.message);
  } finally {
    clearBusy(row);
  }
}

async function approveProposal(row: PendingRow) {
  await decideKnowledge(row, "approve", undefined, "已确认并生效。", "确认没有成功");
}

async function rejectProposal(row: PendingRow) {
  await decideKnowledge(row, "reject", undefined, "已不保存这条建议（原文已打回草稿，没有生效）。", "不保存没有完成");
}

async function approveModified(row: PendingRow) {
  const content = modifyDraftOf(row).trim();
  if (!content) return;
  await decideKnowledge(row, "approve_modified", content, "已按修改后的内容生效。", "修改后确认没有完成");
  if (!rowNotice.value[row.key]) {
    modifyingKey.value = null;
    const next = { ...modifyDrafts.value };
    delete next[row.key];
    modifyDrafts.value = next;
  }
}

// -- 整理问题：回答 / 稍后处理 -----------------------------------------------

async function answerQuestion(row: PendingRow) {
  const text = answerOf(row).trim();
  if (!text || isBusy(row)) return;
  const memory = row.memory;
  if (!memory) return;
  markBusy(row);
  clearRowNotice(row);
  try {
    const outcome = await api.answerMemoryPending(memory.id, text);
    if (!outcome?.ok) {
      throw new Error(outcome?.error || "维护执行器返回 " + String(outcome?.status ?? "未知状态"));
    }
    const next = { ...answers.value };
    delete next[row.key];
    answers.value = next;
    await afterDone("回答已交给后台整理，该事项已处理。");
  } catch (error) {
    // 失败保留输入与上下文；若事项已被别处处理，刷新真实状态。
    setRowNotice(row, "err", "回答没有成功：" + ((error as Error).message || "未知原因"));
    await load();
    refreshPendingCount();
  } finally {
    clearBusy(row);
  }
}

async function deferQuestion(row: PendingRow) {
  const memory = row.memory;
  if (!memory || isBusy(row)) return;
  markBusy(row);
  clearRowNotice(row);
  try {
    await api.deferMemoryPending(memory.id);
    await afterDone("已标记稍后处理，这条还会留在这里。");
  } catch (error) {
    setRowNotice(row, "err", "稍后处理没有保存：" + ((error as Error).message || "未知原因"));
    await load();
    refreshPendingCount();
  } finally {
    clearBusy(row);
  }
}

// -- 实体更新候选：采纳 / 丢弃 -----------------------------------------------

async function decideCandidate(row: PendingRow, action: "adopt" | "discard") {
  const candidate = row.candidate;
  if (!candidate || isBusy(row)) return;
  markBusy(row);
  clearRowNotice(row);
  try {
    const result =
      action === "adopt"
        ? await adoptCandidate(candidate.entity_id, candidate.candidate_id, candidate.card_revision)
        : await dismissCandidate(candidate.entity_id, candidate.candidate_id, candidate.card_revision);
    if (result && result.ok === false) {
      setRowNotice(
        row,
        "err",
        result.blocked_reason
          ? "这次没有写入（" + result.blocked_reason + "）"
          : "这次没有写入，实体内容未改变。",
      );
      await load();
      refreshPendingCount();
      return;
    }
    await afterDone(action === "adopt" ? "已采纳这条候选值。" : "已丢弃这条候选（实体当前值未改变）。");
  } catch (error) {
    if (isCandidateConflict(error)) {
      await refreshAfterConflict(row, "这条实体已被改动，已刷新真实状态；请再看一遍后决定。");
      return;
    }
    setRowNotice(
      row,
      "err",
      (action === "adopt" ? "采纳" : "丢弃") + "没有成功：" + ((error as Error).message || "未知原因"),
    );
  } finally {
    clearBusy(row);
  }
}

// -- 外部定位（对话页记忆问题提醒 / A 的跳转） --------------------------------

function openFocusId() {
  const id = String(props.focusId || "");
  if (!id) return;
  const row = rows.value.find((r) => r.id === id || r.key === id);
  if (row) openDetail(row);
}

watch(
  () => props.focusId,
  (id) => {
    if (id) openFocusId();
  },
);
watch(
  () => rows.value.length,
  () => openFocusId(),
);

async function bootstrap() {
  await load();
  openFocusId();
  refreshPendingCount();
}

void bootstrap();

onBeforeUnmount(() => {
  if (noticeTimer) clearTimeout(noticeTimer);
});
</script>

<template>
  <div
    ref="rootRef"
    class="ppanel"
    v-qio-scrollbar
    data-qio-fit
    data-scroll="pending-list"
  >
    <!-- 列表 -->
    <div v-show="view === 'list'" class="p-list-view" data-pending-list>
      <div class="p-head">
        <span class="p-title serif">待处理</span>
        <span class="p-count mono">{{ totalCount }} 项</span>
        <span class="spacer"></span>
        <button class="qio-btn mini quiet p-refresh" type="button" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </div>
      <div class="row p-filters">
        <QSelect v-model="kindFilter" :options="kindOptions" class="grow p-kind-filter" />
        <QSelect v-model="sourceFilter" :options="sourceOptions" class="grow p-source-filter" />
      </div>

      <p v-if="notice" class="p-notice" role="status">{{ notice }}</p>
      <p v-if="!maintenanceAvailable" class="p-warn" role="status">
        {{ warnings[0] || "维护事项存储不可用：后台维护问题暂无数据可读（不伪造数据）。" }}
      </p>
      <p v-else-if="warnings.length" class="p-warn" role="status">{{ warnings.join(" ") }}</p>
      <p v-if="truncated" class="hint">服务端一次只返回一部分，列表可能不是全部。</p>

      <div v-if="loading && !rows.length" class="hint">加载中…</div>
      <div v-else-if="loadError" class="p-error" role="alert">
        <span>{{ loadError }}</span>
        <button class="qio-btn mini" type="button" @click="load">重试</button>
      </div>
      <template v-else>
        <ul class="p-list">
          <li
            v-for="row in filtered"
            :key="row.key"
            class="p-item qio-card qio-card--quiet"
            :data-pending-id="row.id"
            :data-pending-kind="row.kind"
            role="button"
            tabindex="0"
            @click="openDetail(row)"
            @keydown.enter.prevent="openDetail(row)"
            @keydown.space.prevent="openDetail(row)"
          >
            <div class="p-item-head">
              <span class="qio-tag p-kind">{{ KIND_LABELS[row.kind] }}</span>
              <span class="qio-state quiet p-status" :class="row.status">
                {{ STATUS_LABELS[row.status] || row.status }}
              </span>
              <span v-if="row.kind === 'knowledge_proposal'" class="p-ineffective">尚未生效</span>
              <span v-if="row.sourceLabel" class="p-source">{{ row.sourceLabel }}</span>
            </div>
            <p class="p-item-title">{{ row.title }}</p>
            <p v-if="row.candidate" class="p-item-values">
              <span class="p-value-label">当前</span>
              <span class="p-value">{{ formatValue(row.candidate.current_value) }}</span>
              <span class="p-value-label">候选</span>
              <span class="p-value candidate">{{ formatValue(row.candidate.candidate_value) }}</span>
            </p>
          </li>
          <li v-if="!filtered.length" class="hint p-empty">
            <template v-if="rows.length && hasFilters">
              没有匹配当前筛选的待处理事项。
              <button class="link p-clear-filters" type="button" @click="clearFilters">清除筛选</button>
            </template>
            <template v-else>暂无待处理事项。</template>
          </li>
        </ul>
        <p v-if="hiddenCount > 0" class="hint p-hidden">当前筛选隐藏了 {{ hiddenCount }} 项。</p>
      </template>
    </div>

    <!-- 详情 -->
    <div v-show="view === 'detail'" class="p-detail" data-pending-detail>
      <div class="p-detail-head">
        <button class="qio-btn mini quiet p-back" type="button" data-pending-back @click="backToList">
          <QIcon name="chevron-left" :size="12" /> 返回
        </button>
        <span class="p-detail-title serif">事项详情</span>
        <span class="spacer"></span>
        <span v-if="activeRow" class="qio-tag">{{ KIND_LABELS[activeRow.kind] }}</span>
      </div>

      <div v-if="!activeRow" class="p-empty-detail">
        <p class="hint">这条事项已经不在待处理列表里（可能已在别处处理）。</p>
        <button class="qio-btn mini primary" type="button" @click="backToList">返回列表</button>
      </div>

      <div v-else class="p-card qio-card">
        <div class="p-head">
          <span class="qio-tag">{{ KIND_LABELS[activeRow.kind] }}</span>
          <span class="qio-state quiet" :class="activeRow.status">
            {{ STATUS_LABELS[activeRow.status] || activeRow.status }}
          </span>
          <span v-if="activeRow.sourceLabel" class="p-source">来源：{{ activeRow.sourceLabel }}</span>
        </div>

        <p v-if="noticeOf(activeRow)" class="p-row-notice" :class="noticeOf(activeRow)?.tone" role="alert">
          {{ noticeOf(activeRow)?.text }}
        </p>

        <!-- 知识建议 -->
        <template v-if="activeRow.kind === 'knowledge_proposal'">
          <p class="p-title-text">{{ knowledgeOf(activeRow)?.content || activeRow.title }}</p>
          <dl v-if="knowledgeOf(activeRow)" class="p-meta">
            <div class="p-meta-row">
              <dt>分类</dt>
              <dd>{{ CATEGORY_LABELS[knowledgeOf(activeRow)!.category] || knowledgeOf(activeRow)!.category }}</dd>
            </div>
            <div class="p-meta-row">
              <dt>范围</dt>
              <dd>{{ scopeText(knowledgeOf(activeRow)!) }}</dd>
            </div>
            <div class="p-meta-row">
              <dt>依据</dt>
              <dd>{{ EPISTEMIC_LABELS[knowledgeOf(activeRow)!.epistemic_status || ""] || knowledgeOf(activeRow)!.epistemic_status || "未标注" }}</dd>
            </div>
            <div class="p-meta-row">
              <dt>版本</dt>
              <dd class="mono">{{ knowledgeOf(activeRow)!.id }} · v{{ knowledgeOf(activeRow)!.version }}</dd>
            </div>
            <div v-if="activeRow.memory?.evidence.length" class="p-meta-row">
              <dt>证据</dt>
              <dd>{{ activeRow.memory!.evidence.length }} 条真实记录</dd>
            </div>
          </dl>
          <div v-if="activeRow.memory?.before.length" class="p-diff">
            <div class="p-diff-part">
              <span class="p-label">修改前</span>
              <p class="p-diff-text">{{ contentOf(activeRow.memory?.before[0]) }}</p>
            </div>
            <div class="p-diff-part">
              <span class="p-label">修改后</span>
              <p class="p-diff-text">{{ contentOf(activeRow.memory?.after[0]) || knowledgeOf(activeRow)?.content }}</p>
            </div>
          </div>
          <p v-if="activeRow.memory?.updated_at" class="hint">
            更新时间 {{ activeRow.memory.updated_at }}
          </p>

          <div v-if="modifyingKey === activeRow.key" class="p-draft">
            <label class="p-label" :for="'p-modify-' + activeRow.key">
              修改后确认的内容（整体成功或整体失败，不会先确认原文）
            </label>
            <textarea
              :id="'p-modify-' + activeRow.key"
              class="qio-input p-modify"
              rows="4"
              :value="modifyDraftOf(activeRow)"
              @input="setModifyDraft(activeRow, ($event.target as HTMLTextAreaElement).value)"
            ></textarea>
          </div>

          <div class="p-actions">
            <template v-if="modifyingKey === activeRow.key">
              <button class="qio-btn mini quiet" type="button" @click="cancelModify(activeRow)">取消</button>
              <button
                class="qio-btn mini primary p-modify-save"
                type="button"
                data-pending-action="approve_modified"
                :disabled="isBusy(activeRow) || !modifyDraftOf(activeRow).trim()"
                @click="approveModified(activeRow)"
              >
                {{ isBusy(activeRow) ? "保存中…" : "保存并确认" }}
              </button>
            </template>
            <template v-else>
              <button
                v-if="can(activeRow, 'approve')"
                class="qio-btn mini primary p-approve"
                type="button"
                data-pending-action="approve"
                :disabled="isBusy(activeRow)"
                @click="approveProposal(activeRow)"
              >确认</button>
              <button
                v-if="can(activeRow, 'approve_modified')"
                class="qio-btn mini p-modify-open"
                type="button"
                data-pending-action="approve_modified"
                :disabled="isBusy(activeRow)"
                @click="startModify(activeRow)"
              >修改后确认</button>
              <button
                v-if="can(activeRow, 'reject')"
                class="qio-btn mini quiet p-reject"
                type="button"
                data-pending-action="reject"
                :disabled="isBusy(activeRow)"
                @click="rejectProposal(activeRow)"
              >不保存</button>
            </template>
          </div>
        </template>

        <!-- 整理问题 -->
        <template v-else-if="activeRow.kind === 'maintenance_question'">
          <p class="p-question">{{ activeRow.memory?.question || activeRow.title }}</p>
          <ul v-if="activeRow.memory?.knowledge.length" class="p-knowledge">
            <li v-for="knowledge in activeRow.memory!.knowledge" :key="knowledge.id" class="p-knowledge-item">
              <span class="p-id mono">{{ knowledge.id }} · v{{ knowledge.version }}</span>
              <span class="p-knowledge-content">{{ knowledge.content }}</span>
              <span v-if="knowledge.source" class="p-source">来源：{{ knowledge.source }}</span>
            </li>
          </ul>
          <p v-if="activeRow.memory?.evidence.length" class="hint">
            依据 {{ activeRow.memory!.evidence.length }} 条真实记录
          </p>
          <label class="p-label" :for="'p-answer-' + activeRow.key">
            你的回答（只用于后台整理，不会出现在主对话里）
          </label>
          <textarea
            :id="'p-answer-' + activeRow.key"
            class="qio-input p-answer"
            rows="4"
            :value="answerOf(activeRow)"
            :disabled="isBusy(activeRow)"
            placeholder="告诉 QIO 你的答案…"
            @input="setAnswer(activeRow, ($event.target as HTMLTextAreaElement).value)"
          ></textarea>
          <div class="p-actions">
            <button
              v-if="can(activeRow, 'answer')"
              class="qio-btn mini primary p-answer-submit"
              type="button"
              data-pending-action="answer"
              :disabled="isBusy(activeRow) || !answerOf(activeRow).trim()"
              @click="answerQuestion(activeRow)"
            >{{ isBusy(activeRow) ? "提交中…" : "提交回答" }}</button>
            <button
              v-if="can(activeRow, 'defer')"
              class="qio-btn mini quiet p-defer"
              type="button"
              data-pending-action="defer"
              :disabled="isBusy(activeRow)"
              @click="deferQuestion(activeRow)"
            >稍后处理</button>
          </div>
        </template>

        <!-- 实体更新候选 -->
        <template v-else>
          <p class="p-title-text">
            {{ activeRow.candidate?.entity_name }} · {{ activeRow.candidate?.field_label }}
          </p>
          <div class="p-values">
            <div class="p-value-row">
              <span class="p-value-label">当前值</span>
              <span class="p-value">{{ formatValue(activeRow.candidate?.current_value) }}</span>
            </div>
            <div class="p-value-row">
              <span class="p-value-label">候选值</span>
              <span class="p-value candidate">{{ formatValue(activeRow.candidate?.candidate_value) }}</span>
            </div>
          </div>
          <p class="p-reason">{{ activeRow.candidate?.reason_label || activeRow.candidate?.reason }}</p>
          <p v-if="activeRow.candidate && !activeRow.candidate.adoptable" class="p-blocked">
            这条候选现在不能采纳（{{ activeRow.candidate.blocked_reason || "条件不满足" }}）；仍然可以丢弃。
          </p>
          <p v-if="activeRow.candidate?.created_at" class="hint mono">
            提出于 {{ activeRow.candidate.created_at }}
          </p>
          <div class="p-actions">
            <button
              class="qio-btn mini primary p-adopt"
              type="button"
              data-pending-action="accept"
              :disabled="isBusy(activeRow) || !activeRow.candidate?.adoptable"
              @click="decideCandidate(activeRow, 'adopt')"
            >{{ isBusy(activeRow) ? "处理中…" : "采纳" }}</button>
            <button
              class="qio-btn mini quiet p-discard"
              type="button"
              data-pending-action="discard"
              :disabled="isBusy(activeRow)"
              @click="decideCandidate(activeRow, 'discard')"
            >丢弃</button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 面板正文层：只有这一层纵向滚动（列表与详情共用同一个阅读区）。 */
.ppanel {
  display: flex; flex-direction: column; gap: 8px; padding: 0 14px 20px;
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto; overflow-x: clip; overscroll-behavior: contain;
}
.p-list-view, .p-detail { display: flex; flex-direction: column; gap: 8px; }
.p-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.p-head .spacer { flex: 1; }
.p-detail-head { display: flex; align-items: center; gap: 8px; }
.p-detail-head .spacer { flex: 1; }
.p-title { font-size: 15px; color: var(--text-strong); }
.p-detail-title { font-size: 14px; color: var(--text-secondary); }
.p-count { font-size: 11.5px; color: var(--text-muted); }
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.qio-btn.mini { height: auto; padding: 4px 10px; font-size: 11px; }
.grow { flex: 1; }
.p-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
/* 列表卡是列方向：显式 stretch，避免全局行式类把内容水平居中/缩窄。 */
.p-item {
  display: flex; flex-direction: column; align-items: stretch; justify-content: flex-start;
  gap: 4px; width: 100%; padding: var(--sp-3); border-radius: var(--qio-ui-radius);
  cursor: pointer; text-align: left;
  transition: background var(--dur-fast) var(--ease-1), border-color var(--dur-fast) var(--ease-1);
}
.p-item:hover { background: var(--layer-hover); }
.p-item:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
/* 层级：标签行 → 标题行（加粗、唯一主视觉）→ 正文/取值行，全部左对齐成一条竖线。 */
.p-item-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.p-ineffective, .p-source { font-size: 11px; color: var(--text-muted); }
.p-item-title {
  margin: 2px 0 0; font-size: 13.5px; font-weight: 600; line-height: 1.65;
  color: var(--text-strong); white-space: pre-wrap; overflow-wrap: anywhere;
}
.p-item-values { margin: 2px 0 0; display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; font-size: 12px; }
.p-value-label { color: var(--text-muted); font-size: 11px; }
.p-value { color: var(--text-primary); overflow-wrap: anywhere; }
.p-value.candidate { color: var(--link); }
.hint { font-size: 12px; color: var(--text-muted); }
.p-empty { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.p-error {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 8px 10px; border-radius: var(--qio-ui-radius); font-size: 12px;
  border: 1px solid var(--danger); background: var(--danger-soft); color: var(--danger);
}
.link { background: none; border: none; color: var(--link); cursor: pointer; font-size: 12px; padding: 0; }
.link:hover { text-decoration: underline; }
.p-notice { margin: 0; font-size: 12px; color: var(--text-secondary); }
.p-warn { margin: 0; font-size: 12px; line-height: 1.6; color: var(--warning); }
.p-hidden { margin: 0; }
.p-card { display: flex; flex-direction: column; gap: 10px; padding: var(--sp-4); }
.p-empty-detail { display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.p-title-text { margin: 0; font-size: 14px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
.p-question { margin: 0; font-size: 13.5px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
.p-meta { display: flex; flex-direction: column; gap: 5px; margin: 0; }
.p-meta-row { display: flex; gap: 10px; align-items: flex-start; }
.p-meta-row dt { flex: none; width: 48px; font-size: 11px; color: var(--text-secondary); }
.p-meta-row dd { margin: 0; min-width: 0; font-size: 12.5px; color: var(--text-primary); overflow-wrap: anywhere; }
.p-meta-row dd.mono { font-family: var(--mono); font-size: 11.5px; color: var(--text-muted); }
.p-label { font-size: 11px; color: var(--text-secondary); letter-spacing: 0.03em; }
.p-diff { display: flex; flex-direction: column; gap: 6px; }
.p-diff-part { border-left: 2px solid var(--border-subtle); padding-left: 8px; }
.p-diff-text { margin: 2px 0 0; font-size: 12.5px; line-height: 1.65; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
.p-draft { display: flex; flex-direction: column; gap: 6px; }
.p-modify, .p-answer { width: 100%; font-size: 13px; resize: vertical; }
.p-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.p-row-notice { margin: 0; font-size: 12px; line-height: 1.6; }
.p-row-notice.err { color: var(--danger); }
.p-row-notice.conflict { color: var(--warning); }
.p-knowledge { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.p-knowledge-item { display: flex; flex-direction: column; gap: 2px; border-left: 2px solid var(--border-subtle); padding-left: 8px; }
.p-knowledge-content { font-size: 12.5px; line-height: 1.65; color: var(--text-primary); white-space: pre-wrap; overflow-wrap: anywhere; }
.p-id { font-size: 11px; color: var(--text-faint); }
.p-values { display: flex; flex-direction: column; gap: 4px; }
.p-value-row { display: flex; gap: 8px; align-items: baseline; font-size: 13px; }
.p-value-row .p-value-label { flex: none; width: 44px; }
.p-reason { margin: 0; font-size: 12px; line-height: 1.6; color: var(--text-secondary); }
.p-blocked { margin: 0; font-size: 12px; line-height: 1.6; color: var(--warning); }
.mono { font-family: var(--mono); }
</style>
