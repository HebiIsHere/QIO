<!--
  提交区（子智能体 C 负责实现）：放在底部工具栏右端，由 A 在 BoardToolbar 里渲染。

  契约：docs/interactive-mode-contract.md §8.1 / §8.4，以及 §1.5 的保存 / 提交分离。

  这里必须说准四件事，不许含糊：
  1. 保存状态与提交状态分开说：**「已保存」不等于「QIO 已收到」**，
     只有提交成功才更新基准；点击按钮或请求刚发出都不算成功。
  2. 提交按钮只调 store.submit()（保存 → 服务端求差 → 提交），提交中禁用并显示「正在提交…」。
  3. 「本次有效改动」与「本次允许 QIO 查看的范围」都能看到：
     visible-range 始终可见（自动化与用户都不必先展开），改动列表按需展开。
  4. 失败保留改动与勾选、说出真实原因，并允许直接重试（不假装成功、不清空板面）。

  2026-10-15 布局重构（两行主栏 + 长说明按需展开）：
  - .submit-cluster 是 display: contents：四个子块成为 BoardToolbar 的 grid item，
    各自声明 grid-area（history / stats / notes / actions），位置全部由外层栅格决定；
  - 主栏常态只说**简短状态**：说明正文（understanding.summary）不再参与主栏横向空间分配；
  - 三类内容一律进按需展开的面板（绝对定位在主栏上方、宽度 = 功能栏完整宽度）：
    ① 上次提交结果（提交 / 理解记录，只读）② 本次提交范围与改动 ③ 上次提交过程；
    三者互斥、同一时间只开一个、再点同一入口收起，数据绝不相互串用。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useInteractiveStore } from "../../stores/interactive";
import { fetchBoardState } from "../../services/interactive";
import {
  BATCH_LIST_MIN,
  approveAvailability,
  currentUnderstandingStep,
  locatePreview,
  materialReadText,
  previewBounds,
  rejectAvailability,
  understandingHistory,
  understandingOutcome,
  understandingSettled,
  understandingStatusLabel,
  understandingStepText,
  understandingText,
} from "../../interactive/approval";
import type { BoardStateResponse, Expression, Intent } from "../../interactive/types";
import {
  draftHintText,
  localVisibleRange,
  saveStateText,
  submitFailureBrief,
  submitStateLabel,
  submitStatusText,
  summarizeChanges,
  visibleRangeCounts,
  visibleRangeText,
  type SubmitStatusLike,
} from "../../interactive/submission";
import BoardChangeList from "./BoardChangeList.vue";
import { humanizeFailure, scrubInternalTerms } from "./displayText";
import { vQioScrollbar } from "../../composables/useQioScrollbars";

const props = withDefaults(
  defineProps<{
    /** 悬浮对话工具栏：收起统计文字，只留提交按钮和可展开的状态图标。 */
    compact?: boolean;
  }>(),
  { compact: false },
);
const store = useInteractiveStore();

/**
 * 展开面板（三选一，同一时间只开一个；再次点击同一入口收起）。
 *
 * 「上次提交结果」「本次提交范围与改动」「上次提交过程」是三类**不同**的数据：
 * 用同一个状态互斥，既保证不会同时压两层，也保证不会把上一次的结果说成本次的范围。
 */
type PanelKind = "result" | "details" | "process";
const openPanel = ref<PanelKind | null>(null);
function togglePanel(kind: PanelKind): void {
  openPanel.value = openPanel.value === kind ? null : kind;
}
function closePanel(): void {
  openPanel.value = null;
}

const busy = computed(() => store.submitStatus === "submitting");

/* ------------------------------------------------------------------------
 * 受理与理解分开（契约 §14.1 / §14.6）
 *
 * 「已提交」只是这次提交记录本身的结果；模型理解到了哪一步由理解状态单独表达。
 * 这里不伪造进度：没有说明时先显示统一状态，说明由服务端查询结果补上。
 * ---------------------------------------------------------------------- */
const uStatus = computed(() => store.understandingStatus);
const submitting = computed(() => store.submitStatus === "submitting");
const uText = computed(() => {
  // 正在提交时先显示这一次的事实：上一次的理解结果不许冒充这一次（受理与理解分开）
  if (submitting.value) {
    return {
      label: "正在提交",
      detail: "正在保存板面并把这次表达交给 QIO；受理成功后会立刻显示「已受理」。",
      tone: "waiting" as const,
      settled: false,
      needsUser: false,
      known: false,
    };
  }
  /**
   * 提交成功、但回执没有给出理解请求身份（或查询还没回来）：
   * 绝不能说成「还没有提交给 QIO」——那会把已经发生的事实说反。
   * 这里只说本页已知的：已提交；理解状态以服务端记录为准。
   */
  if (!store.understandingStatus && store.submitStatus === "succeeded") {
    return {
      label: "已提交",
      detail: "这次提交已经记录；回执没有给出理解请求身份，理解状态以服务端记录为准。",
      tone: "waiting" as const,
      settled: false,
      needsUser: false,
      known: false,
    };
  }
  return understandingText(store.understanding, store.understandingStatus);
});
const uSettled = computed(() => understandingSettled(uStatus.value));
const uOutcome = computed(() => understandingOutcome(uStatus.value));
const uCurrent = computed(() => currentUnderstandingStep(store.understanding));
const uHistory = computed(() => understandingHistory(store.understanding));
const materialsText = computed(() =>
  store.understanding?.materials?.length ? materialReadText(store.understanding.materials) : "",
);
const missingText = computed(() => {
  const missing = store.understanding?.clarification?.missing ?? [];
  return missing.length ? missing.join("、") : "请补充这次要做什么";
});
const understoodIntents = computed(() => store.understandingIntents.length);

/* ------------------------------------------------------------------------
 * 本次提交的当前状态（任务书 §5.1 — §5.3）
 *
 * 只显示**最新一条事实**：受理 → 理解 → 审批 / 执行。
 * 说明属于写下它的那个阶段：阶段一过，它只出现在可展开的过程面板里 ——
 * 「本次无需任务」下面不许再压着一句旧的「正在理解」。
 * ---------------------------------------------------------------------- */

/** 这次理解产出的、还在等待审批的条目（批准前不改动板面）。 */
const pendingApprovals = computed(() =>
  store.understandingIntents.filter((item) =>
    ["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status),
  ),
);

/** 这一轮是否真有一个可展示的批量列表（资格判据与 IntentBatchTray 同一口径）。 */
const batchListAvailable = computed(() =>
  store.batches.some(
    (batch) => batch.intentIds.length >= BATCH_LIST_MIN && batch.pendingIds.length > 0,
  ),
);

/**
 * 阶段说明的归属：当前阶段的说在**当前区**；已经过去的说只进可展开过程。
 * submitNote 写的是「正在理解」，理解有结论之后它就只是历史。
 */
const stageNotes = computed(() => {
  const list: { key: string; text: string; current: boolean }[] = [];
  if (store.understandingNote) {
    list.push({
      key: "understanding",
      text: store.understandingNote,
      current: store.understandingNoteOwnsCurrentStage,
    });
  }
  if (store.submitNote) {
    list.push({ key: "submit", text: store.submitNote, current: store.submitNoteOwnsCurrentStage });
  }
  return list;
});
const currentNotes = computed(() => stageNotes.value.filter((note) => note.current));
const pastNotes = computed(() => stageNotes.value.filter((note) => !note.current));
/** 历史里的旧理解过程（本板面；当前这一条不算历史）。 */
const pastUnderstandings = computed(() => store.understandingHistory);

/**
 * 有没有**真实存在过**的上次提交：只认服务端记录（提交结果 / 理解记录 / 过程历史）。
 * 都没有时整块「上次提交」不渲染 —— 绝不凭空编一个「上次提交」状态。
 */
const hasHistory = computed(
  () =>
    Boolean(store.lastSubmission) ||
    Boolean(store.understanding) ||
    Boolean(store.understandingStatus) ||
    pastUnderstandings.value.length > 0,
);

/** 上次提交的**简短**状态（主栏只说这一句；完整说明在「上次提交结果」面板里）。 */
const outcomeLabel = computed(() => {
  const uState = store.understandingStatus ?? store.understanding?.status ?? null;
  if (uState) return understandingStatusLabel(uState);
  const last = store.lastSubmission;
  if (last) {
    const map: Record<string, string> = {
      succeeded: "已提交",
      failed: "提交失败",
      empty: "无内容可提交",
      duplicate: "未重复提交",
    };
    return map[String(last.status ?? "")] ?? "已提交";
  }
  return stateLabel.value;
});

/**
 * chip 上的短标签：outcomeLabel 里的「本次无需开展任务」拼到「上次提交 · 」后面会读成
 * 「上次提交 · 本次无需开展任务」，所以只在 chip 上剥掉开头的「本次」（面板正文保留原样）。
 */
const historyLabel = computed(() => outcomeLabel.value.replace(/^本次/, ""));

/**
 * 主栏「理解说明」的短句：带 summary 的状态（理解完成 / 无需任务）只给结论，
 * 正文留在「上次提交结果」面板里 —— 长说明不再参与主栏的横向空间分配。
 */
const noteDetail = computed(() => {
  if (uOutcome.value === "intent") return "已产出可以审批的意图；完整结果说明在「上次提交」里。";
  if (uOutcome.value === "no_action") return "";
  return uText.value.detail;
});

/**
 * 「上次提交结果」面板的正文：只取**上次提交 / 理解记录**的事实，
 * 不掺入本次未提交的改动列表（那是「详情」的范围）。
 */
/** 无需行动时的结论正文：服务端给了真实 summary 就用它，没有才用标准说法。 */
const noActionResultText = computed(() => {
  const summary = String(store.understanding?.summary ?? "").trim();
  return summary || "这次提交没有需要开展的任务：没有生成任何任务，板面保持原样。";
});

const lastResultBody = computed(() => {
  const summary = String(store.understanding?.summary ?? "").trim();
  if (summary) return summary;
  if (!submitting.value && uStatus.value) return uText.value.detail;
  const result = store.lastSubmission;
  if (result) {
    return submitStatusText((result.status ?? "succeeded") as SubmitStatusLike, {
      result,
      pendingCount: pendingExpressions.value.length,
    });
  }
  if (submitting.value) return "本次提交正在进行中：上面显示的是上一次已记录的结果。";
  return statusText.value;
});

/** 上次提交过程的只读记录（旧理解记录 / 已过去步骤 / 已过去阶段说明）。 */
const processHistory = computed(() =>
  pastUnderstandings.value.map((item) => ({
    key: item.id,
    label: understandingStatusLabel(item.status),
    text: String(item.summary ?? "").trim() || String(item.stage ?? "").trim(),
  })),
);
const processSteps = computed(() =>
  uHistory.value
    .map((step) => understandingStepText(step))
    .filter((text): text is string => Boolean(text)),
);
const processNotes = computed(() => pastNotes.value.map((note) => note.text).filter(Boolean));
/** 有过程可看时才给「查看过程」入口（不编造过程，也不启动任何新任务）。 */
const hasProcess = computed(
  () =>
    processHistory.value.length > 0 ||
    processSteps.value.length > 0 ||
    processNotes.value.length > 0,
);

/**
 * 审批之后的执行事实（取服务端执行记录）：不把「已经批准」说成「已完成」，
 * 也不伪造进度 —— 只如实报「执行中 / 等待执行记录 / 已结束」各几项。
 */
const handledSummary = computed(() => {
  const items = store.understandingIntents;
  if (!items.length || pendingApprovals.value.length) return "";
  let live = 0;
  let waiting = 0;
  let settled = 0;
  for (const intent of items) {
    const status = store.executionStatus[intent.id] ?? intent.execution?.status ?? "none";
    if (status === "queued" || status === "running") live += 1;
    else if (status === "done" || status === "failed" || status === "cancelled") settled += 1;
    else waiting += 1; // 已批准但还没有执行记录 / 已暂停：算「等待」，不假装已经结束
  }
  const parts: string[] = [];
  if (live) parts.push("执行中 " + live + " 项");
  if (waiting) parts.push("等待执行记录 " + waiting + " 项");
  if (settled) parts.push("已结束 " + settled + " 项");
  return parts.length ? "本次任务：" + parts.join(" · ") + "（取服务端执行记录）" : "";
});

const decisionBusy = ref<string | null>(null);
const decisionNote = ref<string | null>(null);

/** 批准 / 拒绝：与批量列表、聊天走同一个 store 入口（同一意图只提交一次，不重复执行）。 */
async function decide(intent: Intent, decision: "approve" | "reject"): Promise<void> {
  if (decisionBusy.value) return;
  decisionBusy.value = intent.id;
  decisionNote.value = null;
  try {
    const result =
      decision === "approve"
        ? await store.approve(intent.id, approveAvailability(intent).needsConfirm)
        : await store.reject(intent.id);
    decisionNote.value = result.ok
      ? decision === "approve"
        ? result.detail ?? "已批准：任务已经交给真实执行，进度以执行记录为准。"
        : "已拒绝：预览消失，板面原内容保留。"
      : "没有" +
        (decision === "approve" ? "批准" : "拒绝") +
        "：" +
        (result.detail ?? result.reason ?? "服务端未说明原因");
  } finally {
    decisionBusy.value = null;
  }
}

/** 定位到板面上的虚线预览（沿用既有 window 事件；不产生审批、不调用模型）。 */
function locateOnBoard(intent: Intent): void {
  locatePreview(intent.id, previewBounds(intent.preview));
}

/** 打开既有批量列表：不新建第二套批量入口；空间不足时由几何层二选一。 */
function openBatchList(): void {
  store.batchOpen = true;
}

/**
 * 主栏说明区只在「需要用户看到或行动」时存在：正常状态（已定局且无需行动）不占第三行、
 * 也不把「上次提交 · <结论>」这句 chip 又用一段说明重复一遍。
 * 已定局的理解结论由 chip 与「上次提交结果」面板表达，阶段说明（currentNotes）单独渲染。
 */
const showUnderstanding = computed(
  () =>
    uOutcome.value === "clarification" ||
    uStatus.value === "failed" ||
    Boolean(uStatus.value && !uSettled.value) ||
    pendingApprovals.value.length > 0,
);

/** 说明区（.sc-notes）要不要整块出现：失败 / 未知 / 被挡 / 需补充 / 待审批 / 阶段说明等。 */
const hasNotes = computed(
  () =>
    promotedCount.value > 0 ||
    ["submitting", "unknown", "empty", "duplicate"].includes(store.submitStatus) ||
    isFailed.value ||
    refreshFailed.value ||
    removalConflicts.value.length > 0 ||
    Boolean(impactCheckErrorText.value) ||
    uOutcome.value === "clarification" ||
    uStatus.value === "failed" ||
    pendingApprovals.value.length > 0 ||
    currentNotes.value.length > 0 ||
    Boolean(understandingActionNote.value) ||
    Boolean(handledSummary.value),
);

const retrying = ref(false);
const understandingActionNote = ref("");
async function retryUnderstandingNow(): Promise<void> {
  if (retrying.value) return;
  retrying.value = true;
  understandingActionNote.value = "";
  try {
    const result = await store.retryUnderstanding();
    if (!result.ok) understandingActionNote.value = result.reason ?? "没有开始重试";
  } finally {
    retrying.value = false;
  }
}

const refreshing = ref(false);
async function refreshUnderstandingNow(): Promise<void> {
  if (refreshing.value) return;
  refreshing.value = true;
  understandingActionNote.value = "";
  try {
    await store.refreshUnderstanding();
  } finally {
    refreshing.value = false;
  }
}

/*
  2026-10-14 第十个问题：**详情默认收起**。
  原来「要补充 / 理解失败」会自动把详情摊开，于是板面底部常年挂着一大段过程文字。
  现在完全不自动展开：失败原因（reason）与保留说明（retention）本来就在折叠区**之外**
  （契约 §11.2 的分层口径，见 submission.ts 的说明），默认就能看见，不会藏住重要信息；
  过程详情只在用户点「查看过程」时展开。

  2026-10-15：三个展开面板共用一个 openPanel，默认都不打开（展开不触发提交、不启动任务）。
*/

/**
 * 未提交的有效改动由**服务端**在每次保存后算好（board_pending），这里只读取展示；
 * 不在前端做第二套求差逻辑，免得两边语义漂移。
 */
const pendingExpressions = ref<Expression[]>([]);
const pendingError = ref<string | null>(null);
let fetchSeq = 0;

async function refreshPending() {
  if (!store.board) {
    pendingExpressions.value = [];
    return;
  }
  const seq = ++fetchSeq;
  try {
    const payload = (await fetchBoardState(store.boardId)) as BoardStateResponse & {
      pending?: { expressions?: Expression[]; error?: string };
    };
    if (seq !== fetchSeq) return;
    pendingExpressions.value = payload.pending?.expressions ?? [];
    pendingError.value = payload.pending?.error ?? null;
  } catch (err) {
    if (seq !== fetchSeq) return;
    pendingExpressions.value = [];
    pendingError.value = (err as Error).message;
  }
}

onMounted(() => {
  void refreshPending();
});

// 载入完成、保存完成、提交结束后各刷新一次预览（提交与保存由 store 负责，这里只读）
watch(
  () => [store.loading, store.saveStatus, store.submitStatus] as const,
  ([loading]) => {
    if (!loading) void refreshPending();
  },
);

const summary = computed(() => summarizeChanges(pendingExpressions.value));
/** 保存状态 + 「已保存 ≠ QIO 已收到」这条边界，合成一句话（顶部状态行是 save-status 的唯一持有者） */
const saveText = computed(
  () =>
    `${saveStateText(store.saveStatus, store.dirty, store.lastSavedAt)}。已保存 ≠ QIO 已收到；只有提交成功才会更新基准。`,
);
const stateLabel = computed(() =>
  submitStateLabel(store.submitStatus, {
    pendingCount: pendingExpressions.value.length,
    hasSubmission: Boolean(store.lastSubmission),
  }),
);
/** 主栏的未提交数量（低强调 pill；0 时也说清「没有」，不留空白歧义）。 */
const pendingLabel = computed(() =>
  summary.value.total > 0 ? `未提交 ${summary.value.total} 项改动` : "没有未提交的改动",
);
/**
 * 失败说明（契约 §11.6）：原因、保留情况、下一步全部取**本次请求**的事实。
 *
 * 上一次提交结果只作为面板里的排查信息；它绝不能冒充本次失败原因 ——
 * 那正是上一轮「默认失败区显示的是上一次结果」的缺陷。
 */
const failure = computed(() =>
  submitFailureBrief({
    status: store.submitStatus,
    error: store.submitError,
    saveStatus: store.saveStatus,
    saveError: store.saveError,
    dirty: store.dirty,
    lastResult: store.lastSubmission,
  }),
);
const statusText = computed(() => {
  // 受理与理解分开：成功只说「这次提交记录下来了」，理解到了哪一步由理解状态单独说
  if (store.submitStatus === "succeeded") {
    if (store.submitAcceptedByLookup) {
      // 这次「已受理」是按请求身份精确查证确认的：本机没有完整回执，
      // 所以绝不借用上一次提交的改动条数（那正是「上一次冒充本次」的错误）。
      return [
        "已提交：服务端确认这次请求已被可靠受理（按本次请求身份精确查证；本机没有拿到完整回执，改动条数以服务端记录为准）",
        "受理只表示 QIO 可靠收下了这次提交；理解结果与任务执行状态分别由理解状态与任务列表表达，提交不构成执行批准。",
      ].join("；");
    }
    const count = store.lastSubmission?.expressions?.length ?? 0;
    /**
     * checkedCleared 是**历史字段**：2026-10-12 起许可持续有效，服务端恒返回空数组。
     * 这里保留读取，是为了万一遇到旧后端真的清了勾选时仍能如实说明；新规则下它不会出现。
     */
    const cleared = store.lastSubmission?.checkedCleared?.length ?? 0;
    return [
      `已提交：本次 ${count} 项改动已记录` +
        (cleared > 0
          ? `；有 ${cleared} 条旧记录在提交时被清掉了勾选（服务端按旧规则回报；新规则下许可不会因提交失效）`
          : "；已提交的卡片仍然保持许可，只有你手动取消才会失效"),
      "受理只表示 QIO 可靠收下了这次提交；理解结果与任务执行状态分别由理解状态与任务列表表达，提交不构成执行批准。",
    ].join("；");
  }
  return submitStatusText(store.submitStatus, {
    result: store.lastSubmission,
    pendingCount: pendingExpressions.value.length,
    error: failure.value?.reason ?? store.submitError,
    retention: failure.value?.retention,
  });
});
const range = computed(() => localVisibleRange(store.board));
const rangeText = computed(() => visibleRangeText(range.value));
/**
 * 常态只留一行**简短**范围（契约 §10.8）：完整排除清单与说明放进「详情」，
 * 不再靠父组件 :deep() 压缩多段文字来维持高度。
 */
const rangeShort = computed(() => {
  const counts = visibleRangeCounts(range.value);
  if (!range.value) return "本次可见：还没有可提交的内容";
  return `本次可见：材料 ${counts.materials} 项 · 注释 ${counts.notes} 条`;
});
/**
 * 是不是**本次**失败：只看当前请求的状态。
 * 不再看 lastSubmission —— 上一次的失败结果不该在成功之后继续显示成失败。
 */
const isFailed = computed(() => failure.value !== null);
/**
 * 失败原因的展示（本轮验收点 4：文案准确简短、层级清楚、不泄露开发术语）。
 *
 * 默认区的原因**不截断**（上一轮验收已明确「很长的失败原因在默认区完整显示」），
 * 但必须先剥掉传输外壳（/api/… -> 500:）与内部代码（stale_check 之流），并解开底层的
 * 重复包裹 ——「完整」指的是真实原因完整，不是把实现用语与重复句子一起铺出来。
 * 辅助诊断（保存失败原因、上一次提交的说明）仍在展开的面板里。
 */
const failureShort = computed(() => humanizeFailure(failure.value?.reason).short);
const failureDetailText = computed(() => scrubInternalTerms(failure.value?.detail ?? ""));

/**
 * 保存前的影响预判失败 / 确认过期：它**阻塞提交**，所以必须有一个清楚、可达的重试入口。
 * 只调 store.saveNow()（重新预判并保存，不自动提交，也不会循环自动重试）。
 */
const impactCheckErrorText = computed(() => {
  const raw = store.impactCheckError;
  // 只有真的有错才占位：humanizeFailure(null) 会返回「没有拿到失败原因」，直接用会凭空多出一行
  return raw ? humanizeFailure(raw, { maxShort: 60 }).short : "";
});
const rechecking = ref(false);
async function recheckImpact(): Promise<void> {
  if (rechecking.value) return;
  rechecking.value = true;
  try {
    await store.saveNow();
  } finally {
    rechecking.value = false;
  }
}
/**
 * F10：提交**已经明确成功**、但随后读取最新页面事实失败。
 *
 * 提交成功是既成事实（lastSubmission 与基准依据都保留），这里只说明「页面刷新失败」，
 * 并提供**只触发读取**的重试 —— 绝不诱导重新提交，也绝不自动重复提交。
 */
const refreshFailed = computed(
  () => store.submitRefreshState === "error" || Boolean(store.submitRefreshError),
);
const refreshErrorText = computed(() =>
  store.submitRefreshError ? humanizeFailure(store.submitRefreshError).short : "原因未知",
);
const refreshRetrying = ref(false);
/**
 * F10 + 契约 §1.3：受理情况未知时的**精确查证**入口。
 *
 * 只按这次提交的请求身份向服务端核对（fetchAcceptance）：命中才认受理，
 * 查不到 / 查询失败就说清「结果仍然未知」—— 不推断、不重复提交、不改判失败。
 */
const verifyingAcceptance = ref(false);
async function verifyAcceptance(): Promise<void> {
  if (verifyingAcceptance.value) return;
  verifyingAcceptance.value = true;
  try {
    await store.confirmAcceptanceAfterUnclear();
  } finally {
    verifyingAcceptance.value = false;
  }
}
async function retryRefresh(): Promise<void> {
  if (refreshRetrying.value) return;
  refreshRetrying.value = true;
  try {
    await store.retrySubmitRefresh();
  } finally {
    refreshRetrying.value = false;
  }
}
/**
 * F07：本地删除（解散的组 / 删除的关联）与服务器对**同一对象**的修改相撞。
 *
 * 未解决之前保存会被挡住（store.saveNow 的同一口径门）；这里给出**明确、可达**的选择：
 * 「保持删除」（放弃对方这次修改）或「恢复服务器版」（放弃本地删除）。
 * 两种选择都只改本页候选并排一次保存，不调用 QIO。
 */
const removalConflicts = computed(() => Object.values(store.boardRemovalConflicts));
function resolveRemovalConflict(id: string, choice: "keep-deleted" | "restore-server"): void {
  store.resolveBoardRemovalConflict(id, choice);
}
const draftText = draftHintText();
/**
 * P1：这一次提交**真的**把几条「未点完成编辑」的文字提升成了正文。
 *
 * 只有 store 拿到回执、并核对本次快照里确实有这份正文时才计数（不强报「已生效」）。
 * 没有可提升的草稿时这里是 0：界面多一个字都不显示（与今天逐字一致）。
 */
const promotedCount = computed(() => store.promotedDraftsCount);
/*
 * P1：这次**没有**提升的草稿（未决冲突）不在这里再写一遍 —— 原因与涉事卡片已经在
 * 失败区（data-im="submit-failure"）里逐条说清了（store.submitError）。
 * 同一条原因在一屏里说两遍只会稀释它，所以只保留 store 的 draftsNotPromoted 供核对。
 */
/** 提交按钮文案：失败后明确写成「重新提交」，让重试一眼可见 */
const submitLabel = computed(() => {
  if (busy.value) return "正在提交…";
  if (isFailed.value) return "重新提交";
  /**
   * F10 + 契约 §1.3：结果未知时按钮必须和「不会重复提交」这句文案一致 ——
   * 重试会**沿用同一个请求身份**，服务端据此返回原事实（不重复创建提交与理解请求），
   * 所以这里明写「同一身份」，而不是含混的「提交给 QIO」。
   */
  if (store.submitStatus === "unknown") return "用同一身份重试";
  return "提交给 QIO";
});

function onSubmit() {
  void store.submit();
}
</script>

<template>
  <!--
    两行主栏（契约 §9.6 / 2026-10-15 重构）：位置全部由 BoardToolbar 的 grid 决定。
    第一行左＝编辑操作，右＝简短上次提交状态 + 查看过程；
    第二行左＝本次可见统计 + 未提交改动数量，右＝详情 + 提交给 QIO。
    长说明（上次提交结果 / 本次提交范围与改动 / 上次提交过程）都进按需展开的面板。
  -->
  <div
    class="submit-cluster"
    data-im="submit-cluster"
    :data-im-failed="isFailed ? '1' : undefined"
    @keydown.esc="closePanel"
  >
    <!--
      第一行右：上次提交的**简短**状态 + 查看过程。
      只有真的存在过上次提交（提交结果 / 理解记录 / 过程历史）才渲染 —— 没有就不编。
    -->
    <div v-if="!compact && hasHistory" class="sc-history" data-im="submit-history">
      <button
        class="sc-chip"
        type="button"
        data-im="submit-history-toggle"
        :aria-expanded="openPanel === 'result'"
        aria-controls="submit-result-panel"
        title="展开上次提交的完整结果说明（只读，不会再次提交）"
        @click="togglePanel('result')"
      >
        上次提交 · {{ historyLabel }}
      </button>
      <button
        v-if="hasProcess"
        class="details"
        type="button"
        data-im="understanding-history-toggle"
        :aria-expanded="openPanel === 'process'"
        aria-controls="submit-process-panel"
        title="查看上次提交的过程记录（只读，不会启动新任务）"
        @click="togglePanel('process')"
      >
        {{ openPanel === "process" ? "收起过程" : "查看过程" }}
      </button>
    </div>

    <!-- 第二行左：本次可见统计 + 未提交改动数量（悬浮模式收进详情面板） -->
    <div v-if="!compact" class="sc-stats" data-im="submit-stats">
      <span class="sc-stat" data-im="visible-range">{{ rangeShort }}</span>
      <span class="sc-pill" data-im="submit-pending">{{ pendingLabel }}</span>
    </div>

    <!--
      说明区：失败 / 未知 / 被挡 / 理解补充说明与阶段说明都**默认可见**（不进面板）；
      只有长正文（理解结论、改动列表、过程记录）按需展开。整块永远独占整行。
    -->
    <div v-if="hasNotes" class="sc-notes" data-im="submit-notes">
      <!--
        常态（idle）不重复第二行那句「未提交 N 项改动」；只有提交真的在进行/有结论/被挡时
        才在这里说「本次提交：<状态>」，避免同一条事实在一屏里说两遍。
      -->
      <p
        v-if="store.submitStatus !== 'idle'"
        class="line state-line"
        data-im="submit-status"
        role="status"
      >本次提交：{{ stateLabel }}</p>

      <!--
        P1：提交时把「未点完成编辑」的草稿提升成了正文。这件事必须说出来（契约 4）：
        用户的字确实进了这次提交快照（QIO 读得到），不是「没点完成编辑就没了」。
        没有可提升的草稿时这一段完全不渲染。
      -->
      <p
        v-if="promotedCount > 0"
        class="promoted line faint"
        data-im="submit-draft-promoted"
        role="status"
      >
        这次有 {{ promotedCount }} 条未点完成编辑的文字已随本次提交生效（已作为正式正文提交给 QIO，不是丢弃）。
      </p>

      <!--
        理解过程（契约 §14.1 / §14.6）：受理成功就先出现统一状态「已受理」，
        说明随查询结果补上；需要补充 / 理解失败 / 无需行动都是明确状态，不靠颜色表达。
        这里只放**简短**状态与说明，理解结论正文在「上次提交结果」面板里。
      -->
      <div
        v-if="showUnderstanding"
        class="understanding"
        data-im="understanding"
        :data-understanding-status="uStatus ?? 'none'"
        :data-understanding-outcome="uOutcome"
      >
        <p class="u-line">
          <strong class="u-label" :class="'tone-' + uText.tone" data-im="understanding-status">
            {{ uText.label }}
          </strong>
          <span
            v-if="noteDetail"
            class="u-detail"
            data-im="understanding-detail"
            :title="uText.detail"
          >{{ noteDetail }}</span>
        </p>
        <span class="u-actions">
          <button
            v-if="!submitting && uStatus === 'failed'"
            class="details"
            type="button"
            data-im="understanding-retry"
            :disabled="retrying"
            @click="retryUnderstandingNow"
          >
            {{ retrying ? "重试中…" : "重试理解（从这次提交的快照恢复）" }}
          </button>
          <button
            v-else-if="!submitting && !uSettled && uStatus"
            class="details"
            type="button"
            data-im="understanding-refresh"
            :disabled="refreshing"
            @click="refreshUnderstandingNow"
          >
            {{ refreshing ? "查询中…" : "刷新理解状态" }}
          </button>
        </span>

        <!--
          待审批出现时审批入口就在这里展开（任务书 §5.4）：不必先去聊天窗口或批量列表找，
          聊天收起时这一段仍然可达。批准 / 拒绝 / 定位与既有批量操作都在，
          批准前不会有任何改动；同一个意图只提交一次，不重复执行。
        -->
        <div
          v-if="pendingApprovals.length"
          class="approvals"
          data-im="submit-approvals"
          :data-approval-count="pendingApprovals.length"
        >
          <p class="approvals-head" data-im="submit-approval-state" role="status">
            待审批 {{ pendingApprovals.length }} 项（批准前不会改动板面）
          </p>
          <ul class="approval-rows" v-qio-scrollbar data-qio-fit>
            <li
              v-for="intent in pendingApprovals"
              :key="intent.id"
              class="approval-row"
              data-im="submit-approval-item"
              :data-intent-id="intent.id"
            >
              <span v-if="intent.demo" class="approval-demo">演示</span>
              <span class="approval-name">{{ intent.title }}</span>
              <button
                class="mini"
                type="button"
                data-im="submit-approve"
                :data-intent-id="intent.id"
                :disabled="decisionBusy === intent.id || !approveAvailability(intent).allowed"
                :title="approveAvailability(intent).text"
                @click="decide(intent, 'approve')"
              >
                {{ decisionBusy === intent.id ? "处理中…" : "批准" }}
              </button>
              <button
                class="mini"
                type="button"
                data-im="submit-reject"
                :data-intent-id="intent.id"
                :disabled="decisionBusy === intent.id || !rejectAvailability(intent).allowed"
                :title="rejectAvailability(intent).text"
                @click="decide(intent, 'reject')"
              >
                拒绝
              </button>
              <button
                class="mini ghost"
                type="button"
                data-im="submit-locate"
                :data-intent-id="intent.id"
                @click="locateOnBoard(intent)"
              >
                定位
              </button>
            </li>
          </ul>
          <span class="approvals-actions">
            <button
              v-if="batchListAvailable"
              class="details"
              type="button"
              data-im="submit-open-batch"
              @click="openBatchList"
            >
              打开批量审批
            </button>
            <span v-if="decisionNote" class="u-note" data-im="submit-decision-note" role="status">
              {{ decisionNote }}
            </span>
          </span>
        </div>

        <p v-if="uOutcome === 'clarification'" class="u-note" data-im="understanding-missing">
          需要你补充：{{ missingText }}；这次没有生成任何任务。
        </p>
      </div>

      <!-- 审批之后的执行事实：只报服务端执行记录，不把「已批准」说成「已完成」 -->
      <p v-if="handledSummary" class="u-note" data-im="submit-handled-state" role="status">
        {{ handledSummary }}
      </p>

      <!--
        当前阶段的说明：只放**当前**阶段的那一/两条。
        阶段已经过去的说明（例如受理时那句「正在理解」）不在这一区，只进可展开过程面板。
      -->
      <p
        v-for="note in currentNotes"
        :key="'note-' + note.key"
        class="u-note"
        :data-im="note.key === 'submit' ? 'submit-acceptance-note' : 'understanding-note'"
      >
        {{ note.text }}
      </p>
      <p v-if="understandingActionNote" class="u-note err" data-im="understanding-action-note">
        {{ understandingActionNote }}
      </p>

      <!--
        失败必须立刻可见（真实错误不许藏进面板）：默认就说清「本次原因 → 保留情况 → 重试」。
        顺序固定，面板只补充较长诊断（保存失败原因、上一次提交返回的说明），不是了解原因的必经入口。
      -->
      <p v-if="failure" class="failure" role="alert" data-im="submit-failure">
        <span class="failure-reason" data-im="submit-failure-reason"><span class="failure-label">失败原因：</span>{{ failureShort }}</span>
        <span class="failure-retention" data-im="submit-failure-retention">{{ failure.retention }}</span>
      </p>

      <!--
        F10：没有拿到回执 = 提交结果**未知**（既不是成功也不是失败）。
        保留改动与勾选，明确要求先确认结果，不诱导直接重复提交。
      -->
      <p v-else-if="store.submitStatus === 'unknown'" class="blocked" role="status" data-im="submit-unknown">
        <span class="blocked-text" data-im="submit-unknown-reason">
          提交结果未知：没有拿到服务端回执，这次提交可能已经受理。改动与勾选都保留；请先查证结果，不要直接重复提交
          （查证只按这次请求身份向服务端精确核对，不会自动重复提交）。
        </span>
        <!--
          F10 + 契约 §1.3：未知时给的是**只读查证**入口（按请求身份精确匹配 + 重新读取），
          不是「重新提交」—— 绝不用一次新的提交去赌原请求有没有受理。
        -->
        <button
          class="details"
          type="button"
          data-im="submit-unknown-verify"
          :disabled="verifyingAcceptance"
          @click="verifyAcceptance"
        >
          {{ verifyingAcceptance ? "查证中…" : "查证受理结果（按请求身份）" }}
        </button>
        <button
          class="details"
          type="button"
          data-im="submit-unknown-refresh"
          :disabled="refreshRetrying"
          @click="retryRefresh"
        >
          {{ refreshRetrying ? "重新读取中…" : "只重新读取页面事实" }}
        </button>
        <span v-if="store.acceptanceLookupNote" class="blocked-text" data-im="submit-unknown-lookup-note">
          {{ store.acceptanceLookupNote }}
        </span>
      </p>

      <!--
        F10：提交已经成功、只是**页面刷新失败**。成功事实与基准依据不变，这里只提供重试刷新。
      -->
      <p v-else-if="refreshFailed" class="blocked" role="status" data-im="submit-refresh-failed">
        <span class="blocked-text" data-im="submit-refresh-reason">
          提交成功，页面刷新失败（{{ refreshErrorText }}）：这次提交已经成立、基准已经更新，
          页面上的板面与可见范围可能不是最新事实。
        </span>
        <button
          class="details"
          type="button"
          data-im="submit-refresh-retry"
          :disabled="refreshRetrying"
          @click="retryRefresh"
        >
          {{ refreshRetrying ? "重新读取中…" : "重试刷新" }}
        </button>
      </p>

      <!--
        F07：本地删除与服务器修改相撞 —— 保存被挡住，这里给每个对象两个明确选择。
        没有它，用户只能看到「保存失败」，拿不到「保持删除 / 恢复服务器版」的入口。
      -->
      <p
        v-else-if="removalConflicts.length"
        class="blocked"
        role="status"
        data-im="board-removal-conflict"
      >
        <span class="blocked-text" data-im="board-removal-conflict-reason">
          本地删除的对象在服务器上被改过：请选择保持删除，还是恢复服务器版（两种选择都会保存一次）。
        </span>
        <span
          v-for="item in removalConflicts"
          :key="item.id"
          class="removal-row"
          data-im="board-removal-conflict-row"
        >
          <span class="mono">{{ item.id }}</span>
          <button
            class="details"
            type="button"
            data-im="board-removal-keep-deleted"
            :data-object-id="item.id"
            @click="resolveRemovalConflict(item.id, 'keep-deleted')"
          >
            保持删除
          </button>
          <button
            class="details"
            type="button"
            data-im="board-removal-restore-server"
            :data-object-id="item.id"
            @click="resolveRemovalConflict(item.id, 'restore-server')"
          >
            恢复服务器版
          </button>
        </span>
      </p>

      <!--
        提交被「影响预判没完成 / 确认过期」挡住：这不是提交失败，而是保存前的检查没做完。
        所以单独一条、层级低于失败区，并给**一次**可达的重试（重新预判并保存，不自动提交）。
      -->
      <p v-else-if="impactCheckErrorText" class="blocked" role="status" data-im="submit-blocked">
        <span class="blocked-text" data-im="submit-blocked-reason">{{ impactCheckErrorText }}</span>
        <button
          class="details"
          type="button"
          data-im="submit-recheck"
          :disabled="rechecking"
          @click="recheckImpact"
        >
          {{ rechecking ? "重新预判中…" : "重新预判并保存" }}
        </button>
      </p>
    </div>

    <!-- 第二行右：详情 + 提交给 QIO（主提交按钮用强调色；其余一律低强调） -->
    <div class="actions" data-im="submit-actions">
      <button
        class="details"
        :class="{ 'sc-details-icon': compact }"
        type="button"
        data-im="submit-details"
        :data-state-tone="isFailed ? 'problem' : store.submitStatus === 'idle' ? 'plain' : 'active'"
        :aria-label="compact ? '提交状态与详情' : undefined"
        :aria-expanded="openPanel === 'details'"
        aria-controls="submit-details-panel"
        :title="compact ? '提交状态与本次提交范围（点击查看详情）' : '本次提交范围与改动（只读，不会再次提交）'"
        @click="togglePanel('details')"
      >
        <template v-if="compact">
          <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <circle cx="8" cy="8" r="5.6" />
            <path d="M8 7.2v4" />
            <circle cx="8" cy="4.9" r="0.7" fill="currentColor" stroke="none" />
          </svg>
        </template>
        <template v-else>{{ openPanel === "details" ? "收起详情" : "详情" }}</template>
      </button>
      <button
        class="submit"
        type="button"
        data-im="submit"
        :title="failure?.nextStep"
        :disabled="busy || !store.board"
        @click="onSubmit"
      >
        {{ submitLabel }}
      </button>
    </div>

    <!--
      展开面板：三类内容互斥，同一时间只开一个。
      绝对定位 → 不参与主栏高度、不横向挤压按钮；宽度 = 功能栏完整宽度（left/right: 0，
      包含块是 .board-toolbar）；限高 + **唯一一层**纵向滚动，横向永不滚动。
    -->
    <Transition name="sc-fade">
      <section
        v-if="openPanel"
        class="sc-panels"
        v-qio-scrollbar="{ policy: 'auto', axis: 'y' }"
        data-qio-fit
        @wheel.stop
      >
        <!-- ① 上次提交结果：只来自上次提交 / 理解记录，绝不掺入本次未提交改动 -->
        <div
          v-if="openPanel === 'result'"
          id="submit-result-panel"
          class="sc-panel"
          role="region"
          aria-label="上次提交结果"
          data-im="submit-result-panel"
        >
          <p class="sc-panel-head" data-im="submit-result-head">上次提交结果</p>
          <p class="sc-panel-line" data-im="submit-result-outcome">上次提交 · {{ outcomeLabel }}</p>
          <!--
            「本次无需开展任务」优先展示服务端给出的**真实结论正文**（模型 summary）；
            只有服务端确实没给正文时才退回标准说法 —— 标准说法不能顶掉真实说明，
            否则「完整结果说明」就成了一句套话（参考图里展开的就是那段真实长文）。
          -->
          <p
            v-if="uOutcome === 'no_action'"
            class="sc-panel-text"
            data-im="understanding-no-action"
          >{{ noActionResultText }}</p>
          <p v-else-if="lastResultBody" class="sc-panel-text" data-im="submit-result-body">{{ lastResultBody }}</p>
          <p v-if="materialsText" class="sc-panel-text" data-im="submit-result-materials">{{ materialsText }}</p>
          <p class="sc-panel-note">这里只说上次提交与理解记录；本次还没提交的改动在「详情」里。</p>
        </div>

        <!-- ② 本次提交范围与改动：本次可见范围明细 + 未提交改动列表 -->
        <div
          v-else-if="openPanel === 'details'"
          id="submit-details-panel"
          class="sc-panel"
          role="region"
          aria-label="本次提交范围与改动"
          data-im="submit-details-box"
        >
          <p class="sc-panel-head" data-im="submit-details-head">本次提交范围与改动</p>
          <p class="sc-panel-text" data-im="submit-details-range">{{ rangeText }}</p>
          <BoardChangeList
            :expressions="pendingExpressions"
            title="本次有效改动（尚未提交）"
            empty-text="本次还没有可提交的改动（服务端在每次保存后重算）。"
          />
        </div>

        <!-- ③ 上次提交过程：只读记录，不触发提交、不启动新任务 -->
        <div
          v-else
          id="submit-process-panel"
          class="sc-panel"
          role="region"
          aria-label="上次提交过程"
          data-im="submit-process-panel"
        >
          <p class="sc-panel-head" data-im="submit-process-head">上次提交过程</p>
          <p v-if="!hasProcess" class="sc-panel-text" data-im="submit-process-empty">
            服务端还没有留下可以查看的过程记录。
          </p>
          <ul v-if="processHistory.length" class="sc-process-list" data-im="submit-process-history">
            <li v-for="item in processHistory" :key="item.key" class="sc-process-item">
              <span class="sc-process-label">{{ item.label }}</span>
              <span class="sc-process-text">{{ item.text || "（服务端没有给出说明）" }}</span>
            </li>
          </ul>
          <ul v-if="processSteps.length" class="sc-process-list" data-im="submit-process-steps">
            <li
              v-for="(text, index) in processSteps"
              :key="'step-' + index"
              class="sc-process-item"
            >{{ text }}</li>
          </ul>
          <ul v-if="processNotes.length" class="sc-process-list" data-im="submit-process-notes">
            <li
              v-for="(text, index) in processNotes"
              :key="'note-' + index"
              class="sc-process-item"
            >{{ text }}</li>
          </ul>
          <p class="sc-panel-note">只读记录：不会重新提交，也不会启动新任务。</p>
        </div>
      </section>
    </Transition>
  </div>
</template>

<style scoped>
/*
  主栏排布完全交给 BoardToolbar 的 grid：这里 display: contents，
  四个子块各自声明 grid-area（契约区名：edit / history / stats / notes / actions）。
  旧版的 flex / 边框 / 内边距在 display: contents 下全部失效，因此删除，不保留死样式。
*/
.submit-cluster {
  display: contents;
}

/* 第一行右：上次提交简短状态 + 查看过程（低强调；主强调只留给主提交按钮） */
.sc-history {
  grid-area: history;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-2);
  min-width: 0;
}
.sc-chip {
  font: inherit;
  font-size: var(--fs-xs);
  line-height: var(--lh-tight);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  max-width: 100%;
  text-align: left;
  overflow-wrap: anywhere;
  cursor: pointer;
}
.sc-chip:hover { color: var(--text-strong); border-color: var(--border-strong); }
.sc-chip:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
.sc-chip[aria-expanded="true"] {
  color: var(--text-strong);
  border-color: var(--border-strong);
  background: var(--bg-inset);
}

/*
  第二行左：本次可见统计 + 未提交改动数量。
  统计允许**受控换行**（长数字 / 窄栏都不竖排、不省略号截断）。
*/
.sc-stats {
  grid-area: stats;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-2);
  min-width: 0;
}
.sc-stat {
  min-width: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.sc-pill {
  min-width: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  color: var(--text-secondary);
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-2);
  white-space: normal;
  overflow-wrap: anywhere;
}

/* 说明区：失败 / 未知 / 被挡 / 理解补充说明与阶段说明默认可见；永远整行独占 */
.sc-notes {
  grid-area: notes;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
  font-family: var(--sans);
}
.sc-notes > * { min-width: 0; }
.state-line { color: var(--text-muted); }

.line {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  color: var(--text-muted);
}
/*
  理解过程（契约 §14.1 / §14.6）：常态一行统一状态 + 需要用户做事时的明确说明。
  颜色只做辅助：每个状态都有文字；说明不裁切（换行显示），完整文本同时给 title。
*/
.understanding {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}
.u-line {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-2);
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  min-width: 0;
}
.u-label {
  flex: none;
  font-family: var(--mono);
  font-size: var(--fs-xs);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
}
.u-label.tone-open { color: var(--link); border-color: var(--link); }
.u-label.tone-waiting { color: var(--warning); border-color: var(--warning); }
.u-label.tone-active { color: var(--accent); border-color: var(--accent); }
.u-label.tone-settled { color: var(--text-secondary); }
.u-label.tone-problem { color: var(--danger); border-color: var(--danger); }
.u-detail {
  min-width: 0;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.u-actions { display: flex; align-items: center; gap: var(--sp-1); flex-wrap: wrap; }
.u-note {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.u-note.err { color: var(--danger); }
/*
  待审批入口（任务书 §5.4）：整块展开在提交状态下方；列表高度有上限、超出时区内滚动 ——
  审批再多也不会把底部工具栏越撑越高（「常态工具栏高度有上限」那条口径仍然成立）。
*/
.approvals {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-1) var(--sp-2);
  border-left: 2px solid var(--accent);
  border-radius: var(--r-xs);
  background: var(--bg-inset);
}
.approvals-head {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  color: var(--text-primary);
}
.approval-rows {
  /* 这一列是**独立列表**（每条自带批准 / 拒绝），允许自己有唯一一层纵向滚动：
     滚动条走统一策略（原生外观已全局移除），横向永不滚动；列表之外的内容不自开滚动层。 */
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: min(200px, 26vh);
  overflow-y: auto;
  overflow-x: clip;
  overscroll-behavior: contain;
}
.approval-row {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-2);
  min-width: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
}
.approval-name {
  flex: 1 1 8ch;
  min-width: 0;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.approval-demo {
  flex: none;
  font-size: var(--fs-xs);
  color: var(--warning);
  border: 1px solid var(--warning);
  border-radius: var(--r-pill);
  padding: 0 var(--sp-1);
}
/* 审批行内的小按钮：只保留可点面积与可读性，不做装饰色（颜色一律走令牌） */
.mini {
  flex: none;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
  cursor: pointer;
}
.mini:hover:enabled { color: var(--text-strong); border-color: var(--border-strong); }
.mini.ghost { background: none; color: var(--link); }
.mini:disabled { opacity: 0.55; cursor: default; }
.mini:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.approvals-actions {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.approvals-actions .u-note { flex: 1 1 auto; }
/*
  失败区（契约 §11.6 / §11.8）：默认就给出「本次原因 + 保留情况」，各占一行、顺序固定。
  长原因允许换行（overflow-wrap），内容策略是**完整显示**、不自开滚动层 ——
  唯一一层滚动只由展开面板承担，所以既不会撑坏提交按钮，也不会藏住原因。
*/
.failure {
  flex: none;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: var(--sp-1) var(--sp-2);
  overflow-wrap: anywhere;
  border-left: 2px solid var(--danger);
  border-radius: var(--r-xs);
  background: var(--danger-soft);
  font-size: var(--fs-xs);
  line-height: 1.45;
}
/* 被挡住的那一条（影响预判未完成）：与失败区同宽、层级低一档，文字用正文色、按钮可点 */
.blocked {
  flex: none;
  min-width: 0;
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-2);
  margin: 0;
  padding: var(--sp-1) var(--sp-2);
  border-left: 2px solid var(--warning);
  border-radius: var(--r-xs);
  background: var(--warning-soft);
  font-size: var(--fs-xs);
  line-height: 1.45;
}
.blocked-text { color: var(--text-secondary); overflow-wrap: anywhere; }
/* F07：同一行里放对象 id 与两个明确选择，窄栏自动换行 */
.removal-row {
  display: inline-flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-1);
}
.removal-row .mono { font-family: var(--mono); font-size: var(--fs-xs); color: var(--text-muted); }
/* 失败原因这一行是**要读的内容**：颜色用正文色保证对比度（实测 --danger 铺在 --danger-soft 上
   只有 4.07:1，小字不达标），错误信号由左侧红边与「失败原因：」这个标签承担 */
.failure-reason { color: var(--text-primary); }
.failure-label { color: var(--danger); }
.failure-retention { color: var(--text-secondary); }
.line.retain { color: var(--warning); }
.line.err { color: var(--danger); }
/* 低强调提示（提升说明等）用可读的元数据色，不做装饰 */
.line.faint { color: var(--text-muted); }
/*
  P1：提升说明是**要读的内容**（用户的字到底去哪了），不做装饰、不用警告色抢注意力：
  它只报告一件已经发生的事实，需要用户行动的情况由失败区承担。
*/
.promoted { overflow-wrap: anywhere; }
/* 第二行右：详情 + 提交（窄档的右对齐由 BoardToolbar 统一给，这里不写 justify-self） */
.actions {
  grid-area: actions;
  display: flex;
  align-items: center;
  flex: none;
  justify-content: flex-end;
  gap: var(--sp-2);
}
.details {
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-xs);
  padding: 2px var(--sp-2);
  cursor: pointer;
}
.details:hover { color: var(--text-strong); border-color: var(--border-strong); }
.details:focus-visible { outline: 2px solid var(--link); outline-offset: 1px; }
.details[aria-expanded="true"] {
  color: var(--text-strong);
  border-color: var(--border-strong);
  background: var(--bg-inset);
}
/* 悬浮工具栏只显示图标状态：形状 + 颜色双通道，文字只留在可访问名称/提示里。 */
.sc-details-icon {
  width: 28px;
  height: 28px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--r-pill);
}
.sc-details-icon svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.sc-details-icon[data-state-tone="active"] {
  color: var(--accent);
  border-color: var(--accent);
}
.sc-details-icon[data-state-tone="problem"] {
  color: var(--danger);
  border-color: var(--danger);
}
/* 主提交按钮：整个功能栏唯一的强调色操作 */
.submit {
  font: inherit;
  font-size: var(--fs-base);
  color: var(--on-accent);
  background: var(--accent);
  border: none;
  border-radius: var(--r-sm);
  padding: var(--sp-2) var(--sp-5);
  cursor: pointer;
}
.submit:hover:not(:disabled) { background: var(--accent-hover); }
.submit:disabled { opacity: 0.55; cursor: default; }
.submit:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }

/*
  展开面板：绝对定位（包含块 = .board-toolbar），宽度 = 功能栏完整宽度，长在主栏上方。
  - 不参与主栏高度，也不横向挤压按钮（left/right: 0 独立于主栏栅格）；
  - 限高 + **唯一一层**纵向滚动；overflow-x: clip 保证永不出现横向滚动条；
  - 长文本自然换行（overflow-wrap: anywhere），不用省略号隐藏正文；
  - user-select: text：面板里的说明可以选中复制（舞台层全局是 none）。
*/
.sc-panels {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc(100% + var(--sp-2));
  z-index: 30;
  max-height: min(46vh, 420px);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-3);
  overflow-y: auto;
  overflow-x: clip;
  overscroll-behavior: contain;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-2);
  font-family: var(--sans);
  user-select: text;
}
.sc-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.sc-panel-head {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-strong);
}
.sc-panel-line {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.sc-panel-text {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.55;
  color: var(--text-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.sc-panel-note {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.45;
  color: var(--text-muted);
  overflow-wrap: anywhere;
}
.sc-process-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.sc-process-item {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-2);
  min-width: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}
.sc-process-label {
  flex: none;
  font-family: var(--mono);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xs);
  padding: 0 var(--sp-1);
}
.sc-process-text { min-width: 0; overflow-wrap: anywhere; }

/*
  改动列表（BoardChangeList）嵌在唯一滚动层里：这里把它自带的「列表内滚动 +
  逐行省略号」收掉 —— 面板只允许一层纵向滚动，改动摘要是要读的正文，不用省略号截断。
*/
.sc-panel :deep(.changes-list) {
  max-height: none;
  overflow: visible;
}
.sc-panel :deep(.changes-headline),
.sc-panel :deep(.change-summary) {
  white-space: normal;
  overflow: visible;
  text-overflow: clip;
  overflow-wrap: anywhere;
}
.sc-panel :deep(.change) { align-items: flex-start; flex-wrap: wrap; }
.sc-panel :deep(.change-bearing) { white-space: normal; }

/* 展开 / 收起的过渡克制（低频、短时长）；reduced-motion 由动效令牌统一归零 */
.sc-fade-enter-active,
.sc-fade-leave-active { transition: opacity var(--dur-menu) var(--ease-1); }
.sc-fade-enter-from,
.sc-fade-leave-to { opacity: 0; }

/*
  视口兜底（老窗口尺寸下的小屏）：只压内边距与间距，**不缩字号、不隐藏任何说明**。
  真正的窄档重排由 BoardToolbar 按功能栏实际可用宽度写 data-tb-band + 栅格完成。
*/
@media (max-width: 900px) {
  .actions { gap: var(--sp-1); }
  .submit { padding: var(--sp-1) var(--sp-4); }
}
@media (max-width: 620px) {
  .actions { gap: var(--sp-1); }
  .submit { padding: 1px var(--sp-2); }
  .details { padding: 1px var(--sp-2); }
}
</style>
