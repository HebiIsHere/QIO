<script setup lang="ts">
/**
 * 一轮的**唯一**过程区域（契约 §1.5）。
 *
 * 默认可见区（运行中也不例外）= 状态行 + 当前阶段名 + 最新一条说明 + 一行工具摘要：
 * * 运行中**不再自动展开**历史（问题 4：旧阶段 / 旧说明 / 逐项工具卡都不该默认可见）；
 * * 历史抽屉与「当前阶段明细（逐项调用）」是**两个独立状态**，各自记自己的展开键；
 * * 完成 / 失败 / 停止自动收起，但用户手动开过、或正在上翻阅读时不动；
 * * 失败 / 停止显示后端给的一句话原因 + **确实可用**的操作（问题 7），详情默认折叠。
 *
 * 内联审批与弹窗共用 `ApprovalFacts.vue`（问题 1）：内联卡说完整的
 * 「做什么 / 为什么 / 真实操作 / 范围 / 风险 / 验证 / 预算」，并提供
 * 「查看完整信息」打开原弹窗；任一时刻同一 approval_id 只有一套有效按钮。
 *
 * 展开状态存在 store（stores/turnProcess.ts）：虚拟列表会卸载条目，组件内的 ref
 * 会在滚出视野时丢掉；键里带 topic / turn_id / stage_id，跨刷新也能恢复。
 */
import { computed, onBeforeUnmount, ref, watch } from "vue";
import MessageItem from "./MessageItem.vue";
import NarrativeStage from "./NarrativeStage.vue";
import TurnTimingPanel from "./TurnTimingPanel.vue";
import ApprovalFacts from "./ApprovalFacts.vue";
import {
  approvalFacts,
  useApprovalsStore,
  type ApprovalBudget,
} from "../stores/approvals";
import {
  currentStageOf,
  groupTurnItems,
  normalizeTurnActionsForStatus,
  useSessionStore,
  type StreamMessage,
  type TurnAction,
  type TurnFacts,
  type TurnStage,
} from "../stores/session";
import {
  isProcessExpanded,
  isProcessManual,
  isStageDetailManual,
  isStageDetailOpen,
  processKey,
  setProcessExpanded,
  setStageDetailOpen,
  toggleProcess,
  toggleStageDetail as toggleStageDetailState,
} from "../stores/turnProcess";

const props = defineProps<{
  /** 这一轮的 turn_id（空串 = 旧历史记录，没有 turn 归属） */
  turnId: string;
  /** 过程条目（中间话 + 工具卡 + legacy 叙事行），按到达顺序 */
  items: StreamMessage[];
  /** 阶段（契约 §1.3；空数组 = legacy 平铺，不伪造阶段） */
  stages: TurnStage[];
  /** TURN_END 的权威事实（没有 = 还在跑 / 旧记录） */
  facts: TurnFacts | null;
  /** 这一轮正在运行 */
  running: boolean;
  /** 这一轮已受理但还没开始 */
  queued?: boolean;
}>();

const session = useSessionStore();
const approvals = useApprovalsStore();

// -- 展开状态：历史抽屉 / 当前阶段明细 分开管理 -------------------------

/** 历史抽屉的键：话题 + 轮（整轮级区域）—— 跨轮 / 跨话题不串 */
const historyKey = computed(() =>
  processKey(session.currentTopicId, props.turnId || props.items[0]?.id || "-"),
);
const historyManual = computed(() => isProcessManual(historyKey.value));
const historyOpen = computed(() => isProcessExpanded(historyKey.value));

/** 当前阶段明细（逐项调用）的键：**带 stage_id** —— 与历史抽屉是两个独立状态 */
const stageKey = computed(() => {
  const stage = currentStageOf(props.stages);
  return props.turnId && stage
    ? processKey(session.currentTopicId, props.turnId, stage.stageId)
    : "";
});
const stageDetailOpen = computed(() => !!stageKey.value && isStageDetailOpen(stageKey.value));

function toggleHistory() {
  toggleProcess(historyKey.value, !historyOpen.value);
}

function toggleStageDetail() {
  if (!stageKey.value) return;
  toggleStageDetailState(stageKey.value, !stageDetailOpen.value);
}

/**
 * 默认展开策略（契约 §1.5）：
 * * 运行中**不自动展开**任何东西 —— 默认可见区已经够用；
 * * 完成 / 失败 / 停止 → 自动收起历史与当前阶段明细；
 * * 用户手动开合过 → 不再自动改（保留既有保护）；
 * * 用户正在上翻阅读（没在跟随底部）→ 不强制收起，避免把正在读的内容抽走。
 */
watch(
  () => props.running,
  (now, before) => {
    if (now === before) return;
    if (now) return;
    if (!session.streamFollowing) return;
    if (!historyManual.value) setProcessExpanded(historyKey.value, false);
    const key = stageKey.value;
    if (key && !isStageDetailManual(key)) setStageDetailOpen(key, false);
  },
  { immediate: true },
);

// -- 过程内容 ---------------------------------------------------------

const stages = computed(() => props.stages);
const currentStage = computed(() =>
  props.running || props.queued ? currentStageOf(stages.value) : null,
);
const historyStages = computed(() => {
  const current = currentStage.value;
  return current ? stages.value.filter((s) => s.stageId !== current.stageId) : stages.value;
});

const tools = computed(() => props.items.filter((m) => m.role === "tool"));
const interims = computed(() => props.items.filter((m) => m.role === "assistant"));

function toolsOf(stageId: string): StreamMessage[] {
  return tools.value.filter((m) => (m.stageId ?? "") === stageId);
}
const looseTools = computed(() => tools.value.filter((m) => !m.stageId));

/** 当前阶段的逐项调用（默认收起：逐项工具卡不进默认可见区） */
const currentStageTools = computed(() =>
  currentStage.value ? toolsOf(currentStage.value.stageId) : [],
);
const currentStageRunningTools = computed(() =>
  currentStageTools.value.filter((m) => m.toolStatus === "running" || m.toolRunning === true),
);
const currentStageDetailLabel = computed(() => {
  const all = currentStageTools.value;
  const bits = [`本阶段 ${all.length} 次调用`];
  if (currentStageRunningTools.value.length) {
    bits.push(`${currentStageRunningTools.value.length} 项运行中`);
  }
  return bits.join(" · ");
});

/**
 * 没有阶段归属的中间话（旧后端 / 旧记录）：仍然按过程说明行渲染。
 *
 * 带 stage_id 的中间话**不在这里** —— 它是该阶段的历次说明之一（StageNote），
 * 由阶段的 notes 渲染，避免出现并列的过程气泡。
 * 兜底：如果它的文字在任何阶段说明里都找不到（阶段被裁剪 / 归属失败），
 * 仍然按过程说明行渲染 —— 绝不因为归属问题把已经生成的字丢掉。
 */
const looseInterims = computed(() => {
  const noteTexts = new Set<string>();
  for (const stage of stages.value) {
    for (const note of stage.notes) noteTexts.add(note.text);
  }
  return interims.value.filter((m) => {
    if (!m.stageId) return true;
    return !noteTexts.has(m.content.trim());
  });
});

/** 这条中间话归属的阶段（找不到 = 未归属，显式 stage_id=null 的生成中文字就是这种） */
function assignedStageId(m: StreamMessage): string {
  const id = String(m.stageId ?? "").trim();
  return id && stages.value.some((s) => s.stageId === id) ? id : "";
}
/** 未归属的中间话：它是「正在生成」的说明（契约 §1.1：等带 stage_id 的快照就地归位） */
const unassignedInterims = computed(() =>
  interims.value.filter((m) => m.content.trim() && !assignedStageId(m)),
);
const looseLiveNote = computed(
  () => unassignedInterims.value[unassignedInterims.value.length - 1]?.content.trim() ?? "",
);

/**
 * 当前说明（默认可见的那一句）：当前阶段的最新说明与「未归属的生成中文字」谁更新就显示谁。
 * 两者都可能出现（先实时到达、后补 stage_id），所以按到达顺序比一次，不做猜测。
 */
const currentNote = computed(() => {
  const stage = currentStage.value;
  const note = stage?.notes.length ? stage.notes[stage.notes.length - 1] : null;
  const loose = unassignedInterims.value[unassignedInterims.value.length - 1];
  if (!note) return loose?.content.trim() ?? "";
  if (!loose) return note.text;
  const noteIndex = props.items.findIndex((m) => m.id === note.narrativeId);
  const looseIndex = props.items.indexOf(loose);
  if (noteIndex === -1) return loose.content.trim();
  return looseIndex > noteIndex ? loose.content.trim() : note.text;
});
/**
 * 当前阶段**更早的说明**（最新一条已经在当前阶段块里突出显示）。
 *
 * 没有它时，运行中的用户只能看到本阶段的最后一句；前几句要等转阶段后才进历史。
 * 这里把它们放进可展开历史，且与当前块不重复（同一段文字只出现一次）。
 */
const currentStageEarlierNotes = computed(() => {
  const stage = currentStage.value;
  if (!stage || stage.notes.length <= 1) return [];
  return stage.notes.slice(0, -1);
});

/** legacy（没有阶段）：运行中把最后一句中间话当作「当前说明」，但不给它编一个阶段名 */
const lastLooseLine = computed(() => {
  const last = props.items[props.items.length - 1];
  if (!last || last.role !== "assistant") return "";
  return last.content.trim();
});

// -- 系统事实 ---------------------------------------------------------

const runningTools = computed(() =>
  tools.value.filter((m) => m.toolStatus === "running" || m.toolRunning === true),
);
function toolStateOf(m: StreamMessage): string {
  return m.toolStatus ?? (m.toolOk === false ? "failed" : "success");
}
/** 失败 / 取消 / 结果未收到分开数：不把「取消」说成「失败」 */
const failedTools = computed(() => tools.value.filter((m) => toolStateOf(m) === "failed"));
const cancelledTools = computed(() => tools.value.filter((m) => toolStateOf(m) === "cancelled"));
const unknownTools = computed(() => tools.value.filter((m) => toolStateOf(m) === "unknown"));
const runningToolLabel = computed(
  () => runningTools.value[0]?.presentation?.title || runningTools.value[0]?.toolName || "",
);

const STATUS_WORD: Record<string, string> = {
  completed: "已完成",
  failed: "已失败",
  cancelled: "已停止",
  stopped: "已停止",
  unavailable: "未完成",
  /**
   * 不完整结束（契约 §七 C2）：流在结束标记之前 EOF —— 已确认正文保留，
   * 但这**不是完成**。界面必须说「未完成」，绝不能收成「已完成」的样子。
   */
  incomplete: "未完成",
};

const statusWord = computed(() => {
  if (props.running) {
    // 只有这条审批**确实内联在本轮过程区**时才说「等待确认」；
    // 非当前轮 / 恢复路径的审批仍由全局状态条说那句话
    if (inlineApproval.value && modalOwnsApproval.value === false) return "等待确认";
    return session.turnPhase === "generating" ? "运行中" : "正在处理";
  }
  if (props.queued) return "等待开始";
  if (props.facts) return STATUS_WORD[props.facts.status] ?? "已结束";
  return "已结束";
});

/**
 * 状态行是否已经给出了「工具 / 调用数量」这一事实。
 *
 * 契约 §1.5 要求「一行简短状态和真实数量」，且同一内容只出现一次：
 * 状态行已经说过数量时，历史抽屉摘要不再重复同一个数字
 * （真机截图缺陷：`已完成 2 次调用 · 2 个阶段 · 2 次调用`）。
 */
const statusHasToolCount = computed(
  () => runningTools.value.length > 0 || (props.running && tools.value.length > 0),
);

/** 状态行里的工具 / 阶段事实（**一行**，不堆状态） */
const statusDetail = computed(() => {
  const bits: string[] = [];
  if (runningTools.value.length) {
    const label = runningToolLabel.value || "工具";
    bits.push(`${label} · ${runningTools.value.length} 项工具运行中`);
  } else if (props.running && tools.value.length) {
    bits.push(`已完成 ${tools.value.length} 次调用`);
  } else if (!props.running && stages.value.length) {
    // 当前阶段块会显示阶段名，这里只在收起后（没有当前阶段块）才补一句
    const last = stages.value[stages.value.length - 1];
    if (last?.name) bits.push(last.name);
  }
  if (failedTools.value.length) {
    /**
     * 失败不能只留一个计数：状态行是默认可见区，把**第一项**失败的一句话原因带上
     * （工具参数 / 完整输出仍在展开后）。可恢复的单次工具错误由此一眼可见。
     */
    const reason = (failedTools.value[0]?.toolError ?? "").trim();
    const short = reason.length > 40 ? `${reason.slice(0, 40)}…` : reason;
    bits.push(reason ? `${failedTools.value.length} 项失败：${short}` : `${failedTools.value.length} 项失败`);
  }
  if (cancelledTools.value.length) bits.push(`${cancelledTools.value.length} 项已取消`);
  if (unknownTools.value.length) bits.push(`${unknownTools.value.length} 项结果未收到`);
  return bits.join(" · ");
});

const dataState = computed(() => {
  if (props.running) return "running";
  if (props.queued) return "waiting";
  if (props.facts?.status === "failed") return "failed";
  if (props.facts?.status === "cancelled" || props.facts?.status === "stopped") return "stopped";
  // 不完整结束：既不是成功（绿），也不是失败（红）—— 它是「未完成」这一档
  if (props.facts?.status === "incomplete") return "incomplete";
  return "ready";
});

// -- 问题 7：结束原因 + 确实可用的操作 ---------------------------------

/** 后端给的一句话人话原因；没有就是空串（旧记录不伪造原因） */
const reasonLine = computed(() => props.facts?.reason ?? "");
/**
 * 渲染用的可用动作（K3.4 防御性归一）：
 * 历史或旧留痕里的 cancelled + resend 是一个点不通的死按钮（resend 只对 interrupted
 * 生效），渲染时同样归一成 retry —— 不依赖上游一定已经归一过。
 */
const displayActions = computed<TurnAction[]>(() =>
  normalizeTurnActionsForStatus(
    props.facts?.status,
    props.facts?.reasonCode,
    props.facts?.actions ?? [],
  ),
);
/** 重试要重发的就是这一轮的用户消息：找不到它，按钮就不该出现 */
const retrySource = computed(() => (props.turnId ? session.userMessageFor(props.turnId) : null));
const canRetry = computed(
  () => displayActions.value.includes("retry") && !!retrySource.value?.content.trim(),
);
const canResend = computed(() => displayActions.value.includes("resend"));
const actionButtons = computed<{ key: "retry" | "resend"; label: string }[]>(() => {
  const out: { key: "retry" | "resend"; label: string }[] = [];
  if (canRetry.value) out.push({ key: "retry", label: "重试" });
  if (canResend.value) out.push({ key: "resend", label: "重新发送" });
  return out;
});
/** 后端列了 retry、但本地找不到这一轮的用户消息：说清为什么没有这个入口 */
const actionNote = computed(() =>
  displayActions.value.includes("retry") && !canRetry.value
    ? "找不到这一轮的用户消息，无法重试"
    : "",
);
const actionBusy = computed(
  () => !!props.turnId && (session.turnActionBusy ?? "").startsWith(`${props.turnId}:`),
);
const actionFeedback = computed(() =>
  props.turnId ? (session.turnActionFeedback[props.turnId] ?? "") : "",
);
/** continue 由既有的「继续 / 停止」操作条承担：过程区只提示入口位置，绝不重复出按钮 */
const continueHint = computed(() =>
  displayActions.value.includes("continue") && session.pendingContinue
    ? "继续或停止请用下方的操作条"
    : "",
);
/** 详情（默认折叠）：完整原因 / 错误原文 / 原因码 —— 只放真实拿到的字段 */
const outcomeDetail = computed(() => {
  const facts = props.facts;
  if (!facts) return "";
  return [
    facts.reason ? `原因：${facts.reason}` : "",
    facts.errorText ? `错误：${facts.errorText}` : "",
    facts.reasonCode ? `原因码：${facts.reasonCode}` : "",
    facts.stoppedBy ? `由谁停止：${facts.stoppedBy === "user" ? "你" : "系统"}` : "",
  ]
    .filter(Boolean)
    .join("\n");
});
const hasActions = computed(
  () => actionButtons.value.length > 0 || !!actionNote.value || !!actionFeedback.value,
);
const hasOutcome = computed(() => !!reasonLine.value || hasActions.value || !!continueHint.value);

function runAction(kind: "retry" | "resend") {
  const id = props.turnId;
  if (!id || actionBusy.value) return;
  void (kind === "retry" ? session.retryTurn(id) : session.resendTurn(id));
}

// -- 展开历史 ---------------------------------------------------------

const hasDrawer = computed(() => props.items.length > 0 || stages.value.length > 0);
/**
 * 历史抽屉的规模摘要（阶段数 + 调用数）。
 *
 * 「同一内容只出现一次」：状态行已经给出调用数量时，这里只补它没有的阶段数 ——
 * 否则折叠态会出现「已完成 2 次调用 · 2 个阶段 · 2 次调用」（D 的真机截图实测缺陷）。
 * 状态行没有数量（例如完成态只给了最后阶段名）时才补调用数。
 */
const drawerSummary = computed(() => {
  const bits: string[] = [];
  if (stages.value.length) bits.push(`${stages.value.length} 个阶段`);
  if (tools.value.length && !statusHasToolCount.value) bits.push(`${tools.value.length} 次调用`);
  return bits.join(" · ");
});

/**
 * legacy 平铺的条目：运行中把最后一句中间话留给上面的「当前说明」显示，
 * 避免同一段文字在折叠头与历史里各出现一次。
 */
const legacyItems = computed(() => {
  if (!props.running) return props.items;
  // 正在显示的那一条（未归属的生成中文字）不再在历史里重复一遍
  const live = unassignedInterims.value[unassignedInterims.value.length - 1];
  if (!live) return props.items;
  return props.items.filter((m) => m.id !== live.id);
});

/** legacy 平铺：一行叙事收纳它之后的调用卡（不伪造阶段） */
const legacyGroups = computed(() =>
  groupTurnItems(legacyItems.value).map((group) =>
    group.kind === "stage"
      ? { key: group.narrative.id, narrative: group.narrative, lines: [] as StreamMessage[], calls: group.calls }
      : {
          key: `loose_${group.items[0]?.id ?? "x"}`,
          narrative: null,
          lines: group.items,
          calls: [] as StreamMessage[],
        },
  ),
);

// -- 内联审批（复用既有 approvals store，绝不自己发请求） --------------

const inlineApproval = computed(() => {
  if (!props.running || !props.turnId) return null;
  // 只有「当前正在跑的这一轮」才内联；非当前轮 / 过期 / 恢复路径仍走全局入口
  if (session.activeTurnId !== props.turnId) return null;
  const current = approvals.current;
  if (!current) return null;
  if (current.turnId && current.turnId !== props.turnId) return null;
  return current;
});

const APPROVAL_KIND_LABELS: Record<string, string> = {
  tool_create: "工具创建审批",
  credential_grant: "凭据授权审批",
  high_impact_knowledge: "高影响知识确认",
  tool_execution: "需要你确认的操作",
  computer: "电脑操作审批",
  dependency_install: "安装依赖",
};

const approvalTitle = computed(() =>
  inlineApproval.value
    ? (APPROVAL_KIND_LABELS[inlineApproval.value.kind] ?? "需要你确认的操作")
    : "",
);
/** 共用事实里也带着标题用的 kind；这里只取一次，供模板与提交复用 */
const inlineFacts = computed(() => approvalFacts(inlineApproval.value));
const approvalBusy = computed(
  () => !!inlineApproval.value && approvals.responding === inlineApproval.value.approval_id,
);

/** 子 agent 预算（内联卡里也能改后批准） */
const approvalBudget = ref<ApprovalBudget | null>(null);
watch(
  () => inlineApproval.value?.approval_id,
  () => {
    approvalBudget.value = inlineFacts.value?.budget ?? null;
  },
  { immediate: true },
);

function respond(decision: "approved" | "rejected") {
  const item = inlineApproval.value;
  if (!item) return;
  const facts = inlineFacts.value;
  const overrides =
    decision === "approved" && facts?.isSubagentCreate
      ? {
          subagent_budget: {
            max_iterations: approvalBudget.value?.maxIterations ?? facts.budget?.maxIterations ?? 5,
            max_tokens: approvalBudget.value?.maxTokens ?? facts.budget?.maxTokens ?? 100000,
            output_limit_chars:
              approvalBudget.value?.outputLimitChars ?? facts.budget?.outputLimitChars ?? 2000,
          },
        }
      : undefined;
  // 按 approval_id 应答：绝不误伤队列里的下一项（重复点击由 store 挡住）
  void approvals.respondById(item.approval_id, decision, overrides);
}

/**
 * 用户**显式**要求看完整信息（区别于 enqueue 的 autoOpen）。
 *
 * 契约 §1.3 第 1 项：当前轮的审批必须「在同一个过程区域内自动展示说明、真实操作信息
 * **与操作按钮**」。所以内联卡接管这条审批期间要**抑制 autoOpen** —— 弹窗一自动显示，
 * 按钮就会按 id 让位给弹窗，过程区里只剩一句说明。
 * 只有用户点了「查看完整信息」才把按钮交给弹窗。
 */
const fullViewRequested = ref(false);

/** 「查看完整信息」：把这条交给原弹窗（内联卡按钮让位，复用既有 claim 机制） */
function openFull() {
  const item = inlineApproval.value;
  if (!item) return;
  fullViewRequested.value = true;
  approvals.showFull(item.approval_id);
}

/** 内联声明：本组件显示这条审批的按钮时，全局入口 / 弹窗让位（按 approval_id 门控） */
const claimedId = computed(() => inlineApproval.value?.approval_id ?? null);
/**
 * 弹窗正在显示这一条、**并且是用户显式点开的** → 内联卡不再显示批准/拒绝
 * （同一 approval_id 只有一套有效按钮）。弹窗被「稍后处理」/ Esc 收起后，
 * 内联卡重新声明并接管。
 */
const modalOwnsApproval = computed(() => {
  const id = claimedId.value;
  return (
    !!id &&
    fullViewRequested.value &&
    approvals.visible &&
    approvals.current?.approval_id === id
  );
});
const inlineOwnsButtons = computed(() => !!claimedId.value && !modalOwnsApproval.value);

/** 上一次声明/接管的审批 id：换了一条就释放旧声明并清掉「显式查看」标记 */
let lastClaimedId: string | null = null;

watch(
  [claimedId, () => approvals.visible, () => approvals.current?.approval_id ?? ""],
  ([id]) => {
    if (id !== lastClaimedId) {
      if (lastClaimedId) approvals.releaseInline(lastClaimedId);
      fullViewRequested.value = false;
      lastClaimedId = id;
    }
    if (!id) return;
    // 弹窗没显示（含「稍后处理」/ Esc 之后）→ 内联卡是唯一的按钮；显式查看标记随之作废
    if (!approvals.visible) fullViewRequested.value = false;
    /**
     * 内联卡接管期间抑制 autoOpen：弹窗不自动弹，按钮留在过程区。
     * 用户随后仍可用「查看完整信息」显式打开弹窗（那时按钮才交给弹窗）。
     */
    if (approvals.visible && approvals.current?.approval_id === id && !fullViewRequested.value) {
      approvals.defer();
    }
    if (modalOwnsApproval.value) approvals.releaseInline(id);
    else approvals.claimInline(id);
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  const id = claimedId.value;
  if (id) approvals.releaseInline(id);
});
</script>

<template>
  <section
    class="turn-process"
    :class="{ open: historyOpen, running, failed: failedTools.length > 0 }"
    :data-state="dataState"
    :data-turn="turnId || undefined"
    data-test="turn-process"
    aria-label="本轮过程"
  >
    <!-- 状态行：系统事实。展开按钮与耗时入口是两个独立控件（不能嵌套按钮） -->
    <div class="tp-status" data-test="turn-process-status">
      <button
        class="tp-toggle"
        type="button"
        data-test="turn-process-toggle"
        :aria-expanded="historyOpen ? 'true' : 'false'"
        :title="historyOpen ? '收起过程历史' : '展开过程历史'"
        @click="toggleHistory"
      >
        <span class="tp-dot" aria-hidden="true"></span>
        <span class="tp-state">{{ statusWord }}</span>
        <span v-if="statusDetail" class="tp-detail">{{ statusDetail }}</span>
        <span v-if="hasDrawer" class="tp-chev" aria-hidden="true">{{ historyOpen ? "▾" : "▸" }}</span>
      </button>
      <!--
        耗时入口：这里**不传 status** —— 状态词由过程区状态行唯一负责，面板只输出「耗时 X」。
        （注释里不写状态词本身：DOM 文本计数断言不该被注释污染。）
      -->
      <span v-if="turnId && !running" class="tp-duration" data-test="turn-process-duration">
        <TurnTimingPanel
          :turn-id="turnId"
          :duration-ms="facts?.durationMs ?? null"
          :queue-ms="facts?.queueMs ?? null"
        />
      </span>
      <span v-if="hasDrawer && drawerSummary" class="tp-count mono">{{ drawerSummary }}</span>
    </div>

    <!-- 问题 7：失败 / 停止的一句话原因 + 确实可用的操作；详情默认折叠 -->
    <div v-if="hasOutcome" class="tp-outcome" data-test="turn-process-outcome">
      <p v-if="reasonLine" class="tp-reason" data-test="turn-process-reason">{{ reasonLine }}</p>
      <div v-if="hasActions" class="tp-outcome-actions" data-test="turn-process-actions">
        <button
          v-for="action in actionButtons"
          :key="action.key"
          class="qio-btn quiet"
          type="button"
          :data-test="`turn-process-action-${action.key}`"
          :disabled="actionBusy"
          :aria-busy="actionBusy ? 'true' : undefined"
          @click="runAction(action.key)"
        >
          {{ action.label }}
        </button>
        <span v-if="actionNote" class="tp-outcome-note" data-test="turn-process-action-note">
          {{ actionNote }}
        </span>
      </div>
      <p v-if="continueHint" class="tp-outcome-hint" data-test="turn-process-continue-hint">
        {{ continueHint }}
      </p>
      <p v-if="actionFeedback" class="tp-outcome-err" role="status" data-test="turn-process-action-error">
        {{ actionFeedback }}
      </p>
      <details v-if="outcomeDetail" class="tp-outcome-detail" data-test="turn-process-outcome-detail">
        <summary>详情</summary>
        <pre class="tp-outcome-pre mono">{{ outcomeDetail }}</pre>
      </details>
    </div>

    <!-- 当前阶段：名字 + 最新说明，突出显示；逐项调用在独立开关后面（默认收起） -->
    <div v-if="currentStage" class="tp-current" data-test="turn-process-current">
      <div class="tp-cur-name serif">{{ currentStage.name || "当前阶段" }}</div>
      <p v-if="currentNote" class="tp-cur-text">{{ currentNote }}</p>
      <div v-if="currentStageTools.length" class="tp-cur-detail">
        <button
          class="tp-cur-detail-toggle"
          type="button"
          data-test="turn-process-stage-toggle"
          :aria-expanded="stageDetailOpen ? 'true' : 'false'"
          :title="stageDetailOpen ? '收起本阶段明细' : '展开本阶段明细'"
          @click="toggleStageDetail"
        >
          <span class="tp-chev" aria-hidden="true">{{ stageDetailOpen ? "▾" : "▸" }}</span>
          <span>{{ stageDetailOpen ? "收起本阶段明细" : currentStageDetailLabel }}</span>
        </button>
        <div v-if="stageDetailOpen" class="tp-cur-detail-body" data-test="turn-process-stage-tools">
          <MessageItem v-for="m in currentStageTools" :key="m.id" :message="m" />
        </div>
      </div>
    </div>
    <!-- legacy（没有阶段）：未归属的生成中说明就是「当前说明」 -->
    <div v-else-if="running && looseLiveNote" class="tp-current" data-test="turn-process-current">
      <p class="tp-cur-text">{{ looseLiveNote }}</p>
    </div>

    <!-- 内联审批：事实与弹窗共用 ApprovalFacts；同一 approval_id 只有一套有效按钮 -->
    <div v-if="inlineApproval" class="tp-approval" data-test="turn-process-approval" role="group">
      <div class="tp-ap-head">
        <span class="tp-ap-mark" aria-hidden="true">!</span>
        <span class="tp-ap-title serif">{{ approvalTitle }}</span>
      </div>
      <ApprovalFacts
        :item="inlineApproval"
        :budget="approvalBudget"
        @update:budget="approvalBudget = $event"
      />
      <p v-if="approvals.error" class="tp-ap-err" role="status">{{ approvals.error }}</p>
      <div class="tp-ap-actions">
        <template v-if="inlineOwnsButtons">
          <button
            class="qio-btn"
            type="button"
            data-test="turn-process-approval-full"
            @click="openFull"
          >
            查看完整信息
          </button>
          <button
            class="qio-btn"
            type="button"
            data-test="turn-process-approval-reject"
            :disabled="approvalBusy"
            :aria-busy="approvalBusy ? 'true' : undefined"
            @click="respond('rejected')"
          >
            拒绝
          </button>
          <button
            class="qio-btn primary"
            type="button"
            data-test="turn-process-approval-allow"
            :disabled="approvalBusy"
            :aria-busy="approvalBusy ? 'true' : undefined"
            @click="respond('approved')"
          >
            允许
          </button>
        </template>
        <p v-else class="tp-ap-inmodal" data-test="turn-process-approval-in-modal">
          这次确认已经在完整窗口中打开：按钮在那里，这里不再重复一套。
        </p>
      </div>
    </div>

    <!-- 可展开历史：阶段顺序 + 历次说明 + 关联工具；legacy 记录平铺。
         收起时整块不渲染（DOM 里不存在）——「同一内容只出现一次」也包括不可见的重复。 -->
    <div v-if="hasDrawer && historyOpen" class="tp-drawer open" data-test="turn-process-history">
      <div class="tp-drawer-clip">
        <div class="tp-drawer-body">
          <template v-if="stages.length">
            <section v-for="stage in historyStages" :key="stage.stageId" class="tp-stage">
              <div class="tp-stage-head">
                <span class="tp-stage-index mono">{{ stage.index }}</span>
                <span class="tp-stage-name serif">{{ stage.name || "过程" }}</span>
                <span class="tp-stage-state mono" :data-state="stage.status">
                  {{ stage.status === "done" ? "已完成" : "进行中" }}
                </span>
              </div>
              <!-- 阶段说明（含 STAGE.text 与工具轮中间话）：一个阶段一个列表，各出现一次 -->
              <p v-for="note in stage.notes" :key="note.narrativeId || note.text" class="tp-note" :data-kind="note.kind">
                {{ note.text }}
              </p>
              <MessageItem v-for="m in toolsOf(stage.stageId)" :key="m.id" :message="m" />
            </section>
            <!-- 当前阶段更早的说明：运行中也能回看（最新一条在上面突出显示） -->
            <section v-if="currentStageEarlierNotes.length" class="tp-stage" data-test="turn-process-current-notes">
              <div class="tp-stage-head">
                <span v-if="currentStage" class="tp-stage-index mono">{{ currentStage.index }}</span>
                <span class="tp-stage-name serif">{{ currentStage?.name || "当前阶段" }}</span>
                <span class="tp-stage-state mono" data-state="running">更早的说明</span>
              </div>
              <p
                v-for="note in currentStageEarlierNotes"
                :key="note.narrativeId || note.text"
                class="tp-note"
                :data-kind="note.kind"
              >
                {{ note.text }}
              </p>
            </section>
            <section v-if="looseTools.length || looseInterims.length" class="tp-stage">
              <div class="tp-stage-head">
                <span class="tp-stage-name serif">整轮</span>
                <span class="tp-stage-state mono" data-state="loose">没有阶段归属</span>
              </div>
              <MessageItem v-for="m in looseInterims" :key="m.id" :message="m" />
              <MessageItem v-for="m in looseTools" :key="m.id" :message="m" />
            </section>
          </template>
          <template v-else>
            <div v-for="group in legacyGroups" :key="group.key" class="tp-legacy">
              <NarrativeStage v-if="group.narrative" :narrative="group.narrative" :calls="group.calls">
                <MessageItem v-for="m in group.calls" :key="m.id" :message="m" />
              </NarrativeStage>
              <template v-else>
                <MessageItem v-for="m in group.lines" :key="m.id" :message="m" />
              </template>
            </div>
          </template>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 过程区 = 一轮里唯一的「过程」容器：一条极淡的轨，内容收在里面 */
.turn-process {
  margin: 2px 0 6px;
  border-left: 1px solid var(--border-subtle);
  padding-left: 2px;
}
.turn-process[data-state="failed"] {
  border-left-color: var(--border-danger);
}
.turn-process[data-state="incomplete"] {
  border-left-color: var(--warning);
}
.tp-status {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  flex-wrap: wrap;
  padding: 3px 2px;
}
.tp-toggle {
  display: inline-flex;
  align-items: baseline;
  gap: var(--sp-2);
  background: none;
  border: none;
  padding: 3px 4px;
  font: inherit;
  color: inherit;
  text-align: left;
  cursor: pointer;
  border-radius: var(--r-xs);
}
.tp-toggle:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.tp-dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-faint);
  align-self: center;
}
.turn-process[data-state="running"] .tp-dot {
  background: var(--link);
}
.turn-process[data-state="waiting"] .tp-dot {
  background: var(--warning);
}
.turn-process[data-state="failed"] .tp-dot {
  background: var(--danger);
}
.turn-process[data-state="stopped"] .tp-dot {
  background: var(--text-faint);
}
.turn-process[data-state="incomplete"] .tp-dot {
  background: var(--warning);
}
.turn-process[data-state="ready"] .tp-dot {
  background: var(--success);
}
.tp-state {
  font-family: var(--sans);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.turn-process[data-state="running"] .tp-state {
  color: var(--text-strong);
}
.turn-process[data-state="failed"] .tp-state {
  color: var(--danger);
}
.turn-process[data-state="incomplete"] .tp-state {
  color: var(--warning);
}
.tp-detail {
  font-family: var(--sans);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.tp-chev {
  font-size: 10px;
  color: var(--text-faint);
}
.tp-count {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.tp-duration {
  display: inline-flex;
  align-items: baseline;
}
/* 结束原因与可用操作：失败/停止时最该先看到的东西 */
.tp-outcome {
  margin: 2px 0 2px 4px;
  padding-left: var(--sp-3);
  border-left: 1px solid var(--border-danger);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.turn-process[data-state="stopped"] .tp-outcome {
  border-left-color: var(--border-strong);
}
/* 未完成：不是失败也不是正常结束 —— 用警示色，原因文字保持可读 */
.turn-process[data-state="incomplete"] .tp-outcome {
  border-left-color: var(--warning);
}
.tp-reason {
  margin: 0;
  font-family: var(--sans);
  font-size: var(--fs-sm);
  color: var(--danger);
  line-height: 1.6;
}
.turn-process[data-state="stopped"] .tp-reason {
  color: var(--text-secondary);
}
.turn-process[data-state="incomplete"] .tp-reason {
  color: var(--text-secondary);
}
.tp-outcome-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.tp-outcome-note {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.tp-outcome-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.tp-outcome-err {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--danger);
}
.tp-outcome-detail {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.tp-outcome-detail > summary {
  cursor: pointer;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  list-style: none;
}
.tp-outcome-detail > summary::-webkit-details-marker {
  display: none;
}
.tp-outcome-detail > summary::before {
  content: "›";
  display: inline-block;
  color: var(--text-muted);
  transition: transform var(--dur-toggle) var(--ease);
}
.tp-outcome-detail[open] > summary::before {
  transform: rotate(90deg);
}
.tp-outcome-detail > summary:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.tp-outcome-pre {
  margin: 6px 0 0;
  padding: 8px 10px;
  max-height: 160px;
  overflow: auto;
  background: var(--bg-inset);
  border: 1px solid var(--border-subtle);
  border-radius: var(--r-sm);
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
}
/* 当前阶段：突出但不喧哗 */
.tp-current {
  margin: 2px 0 2px 4px;
  padding-left: var(--sp-3);
  border-left: 1px solid var(--border-strong);
}
.tp-cur-name {
  font-size: var(--fs-base);
  color: var(--text-strong);
  line-height: 1.6;
}
.tp-cur-text {
  margin: 1px 0 2px;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  line-height: 1.7;
}
.tp-cur-detail {
  margin-top: 2px;
}
.tp-cur-detail-toggle {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  background: none;
  border: none;
  padding: 2px 0;
  font: inherit;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  cursor: pointer;
  border-radius: var(--r-xs);
}
.tp-cur-detail-toggle:hover {
  color: var(--text-secondary);
}
.tp-cur-detail-toggle:focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
}
.tp-cur-detail-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 2px;
}
/* 内联审批：真实操作信息 + 与弹窗共用的按钮门控 */
.tp-approval {
  margin: var(--sp-2) 0 var(--sp-2) var(--sp-3);
  padding: var(--sp-3);
  max-width: min(560px, 100%);
  background: var(--bg-elevated);
  border: 1px solid var(--warning);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-1);
}
.tp-ap-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}
.tp-ap-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--warning);
  color: var(--bg-base);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
}
.tp-ap-title {
  font-size: var(--fs-base);
  color: var(--text-strong);
}
.tp-ap-err {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-sm);
  color: var(--danger);
}
.tp-ap-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  flex-wrap: wrap;
}
.tp-ap-inmodal {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  line-height: 1.6;
}
/* 展开历史：0fr→1fr 网格行做真实高度过渡（与工具卡一致） */
.tp-drawer {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--dur-toggle) var(--ease);
}
.tp-drawer.open {
  grid-template-rows: 1fr;
}
.tp-drawer-clip {
  overflow: hidden;
}
.tp-drawer-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-2) 0 var(--sp-1) var(--sp-3);
}
.tp-stage {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tp-stage-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
}
.tp-stage-index {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.tp-stage-name {
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.tp-stage-state {
  font-size: var(--fs-xs);
  color: var(--text-faint);
}
.tp-stage-state[data-state="running"] {
  color: var(--link);
}
.tp-note {
  margin: 0 0 0 var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--text-muted);
  line-height: 1.7;
}
.tp-note[data-kind="warning"] {
  color: var(--warning);
}
.tp-legacy {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .tp-drawer {
    transition: none;
  }
}
</style>
