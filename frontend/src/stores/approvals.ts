import { defineStore } from "pinia";
import { api } from "../services/api";
import { useApprovalResultsStore } from "./approvalResults";
import {
  approvalStateFromPhase,
  approvalStateLabel,
  confirmApprovalStage,
  decideApproval,
  getPendingApprovals,
  toPendingApproval,
  type PendingApproval,
} from "../services/approvalLifecycle";

export interface ApprovalItem {
  approval_id: string;
  kind: string;
  payload: Record<string, unknown>;
  /**
   * 这次审批原本属于哪一轮 / 哪个会话 / 哪个请求摘要。
   * 应答时原样回传，后端据此确认「批准的就是这一次请求」——
   * 用户操作方式不变，只有审批与请求不匹配时才会被拒。
   */
  turnId?: string | null;
  sessionId?: string | null;
  requestDigest?: string | null;
  /**
   * 后端已经没有这条审批（别处已应答 / 已过期）。
   * 这时批准与拒绝都会 404，继续保留成一个「可重试」的待办只会把用户卡死 ——
   * 所以标记为失效：界面明确说清楚，并给一个「知道了」把它清掉。
   */
  stale?: boolean;

  // ---- R9 审批生命周期 v2（契约 §5；老后端事件没有这些字段 → 全部可选） ----
  /** 新状态机权威状态：waiting / approved / executing / completed / failed / … */
  state?: string;
  /** 人类可读标题（后端 label；缺失时按 kind 映射） */
  label?: string;
  /** 这次操作用的什么用途（进入审批单的 purpose） */
  purpose?: string;
  /** 来源：tool / subagent / background / user */
  source?: string;
  /** 操作摘要（后端 operation {kind,label,summary}，只读；不含执行参数细节） */
  operationSummary?: string;
  createdAt?: string;
  expiresAt?: string;
  /** 内部数据三次确认进度（后端 CAS 为准，前端只呈现） */
  confirmedStage?: number;
  requiresConfirmations?: number;
  /** 后端 CAS 修订号（decide / confirm 携带 expected_revision） */
  revision?: number;
  /** 最近一次确认 / 决定失败的原因（不覆盖 approvals.error 的全局语义时用） */
  lifecycleError?: string;
}

/** 这条审批是否走 R9 新状态机（老后端事件没有 state 字段 → 走旧 /respond 兼容路径）。 */
export function isLifecycleApproval(item: ApprovalItem | null | undefined): boolean {
  return Boolean(item && typeof item.state === "string" && item.state);
}

/** v2 pending 条目 → store 待办字段（snake_case → camelCase；未知字段留在 payload）。 */
export function approvalMetaFromPending(pending: PendingApproval): Partial<ApprovalItem> {
  return {
    state: pending.state,
    label: pending.label,
    purpose: pending.purpose,
    source: pending.source,
    operationSummary: pending.operation?.summary ?? "",
    createdAt: pending.created_at,
    expiresAt: pending.expires_at ?? undefined,
    confirmedStage: pending.confirmed_stage,
    requiresConfirmations: pending.requires_confirmations,
    ...(pending.revisions ? { revision: pending.revisions } : {}),
  };
}

/**
 * 「它想做什么」：审批里最该先读的一句人话。
 *
 * 与统一审批弹窗的 intent 共用这一份取值顺序
 * （description → tool_name → name → explanation → reason）——
 * 内联卡与弹窗必须说同一句话，不能各写一套。
 */
export function approvalIntent(payload: Record<string, unknown>): string {
  const first = [payload.description, payload.tool_name, payload.name, payload.explanation, payload.reason]
    .map((v) => (typeof v === "string" ? v.trim() : ""))
    .find((v) => v.length > 0);
  return first ?? "该操作需要你的授权";
}

/** 后端 policy.describe() 的能力清单（人话）；没有就不显示，不编造。 */
export function approvalCapabilities(payload: Record<string, unknown>): string[] {
  const raw = payload.capabilities;
  return Array.isArray(raw) ? raw.map((x) => String(x)) : [];
}


/** 子 agent 执行预算（可修改后批准） */
export interface ApprovalBudget {
  maxIterations: number;
  maxTokens: number;
  outputLimitChars: number;
}

/** 「验证了吗」：拿不到测试信息时为 null —— 宁可不显示，也不假装已验证 */
export interface ApprovalVerification {
  verified: boolean;
  label: string;
  detail: string;
}

/**
 * 审批界面要用到的**全部**事实。
 *
 * 契约 §1.3：这份整理**只有一处**（approvalFacts），统一审批弹窗与过程区内联卡
 * 共用同一份 —— 禁止两套显示规则、禁止内联卡只说一句话。
 *
 * 两条底线：
 * * 模型 `explanation` 与系统 `description` **分别保留**，模型文案不能替换系统事实；
 * * 拿不到的字段就是空/null，不编造（不假装已验证、不编造路径）。
 */
export interface ApprovalFacts {
  kind: string;
  /** 它想做什么（系统事实：description → tool_name → name，再回落到 explanation） */
  description: string;
  /** 模型说明（payload.explanation），与 description 分别保留 */
  explanation: string;
  /** 为什么需要（与上面两者重复时为空，不重复说同一句话） */
  why: string;
  /** 它会访问什么（后端给的具体清单：路径 / 命令 / 网址） */
  access: string[];
  /** policy.describe() 的能力清单（原始约定串） */
  capabilities: string[];
  /** 去掉「副作用：」内部枚举后的展示清单 */
  capabilityList: string[];
  sideEffect: string;
  /** 会改变什么（把副作用枚举翻成人话） */
  changeSummary: string;
  /** 授权范围：仅这一次 / 长期生效 */
  scopeLabel: string;
  /** 真实操作事实：具体命令 */
  command: string;
  /** 真实操作事实：具体路径（工具参数里的 path/paths/files + 明细里的工作目录） */
  paths: string[];
  /** 真实操作事实：工具参数（pretty JSON；没有就是空串） */
  params: string;
  /** 后端给的技术明细原文（实际命令 / 工作目录 / 完整路径） */
  detail: string;
  /** 风险行为标签（会联网 / 会修改文件 / 会执行命令 / 会使用凭据 / 只读） */
  risks: string[];
  /** 高风险：给批准按钮降调，别让品牌色看起来像「推荐你点」 */
  highRisk: boolean;
  verification: ApprovalVerification | null;
  /**
   * 可修改后批准的预算（**仅**旧 tool_create 子智能体路径：走 /respond overrides，
   * 执行器真的会用用户批准的值）。v2 生命周期记录为 null —— 那里不支持改预算。
   */
  budget: ApprovalBudget | null;
  /** 只读预算：v2 生命周期记录带了预算信息时照实展示，但不提供任何可编辑控件。 */
  budgetReadOnly: ApprovalBudget | null;
  isSubagentCreate: boolean;
  /** 结构化补充行（工具 / 类型 / 凭据 / 授权工具 / 知识内容 / 示例请求 …） */
  rows: { label: string; value: string }[];
  /** 高级详情（默认折叠）：策略指纹 / 逐条测试结果 / raw params */
  advanced: { lines: string[]; raw: string };

  // ---- R9 审批生命周期 v2（老审批没有这些字段 → 空串 / 0，界面不显示） ----
  /** 状态机权威状态（waiting / approved / executing / completed / failed / …） */
  lifecycleState: string;
  /** 状态中文标签（可直接展示） */
  stateLabel: string;
  /** 内部数据确认进度文案（"已确认 1/3 次"）；普通审批为空 */
  confirmationProgress: string;
  requiresConfirmations: number;
  confirmedStage: number;
  isInternalData: boolean;
  /** 用途（进入审批单的 purpose） */
  purpose: string;
  /** 操作摘要（后端 operation.summary，只读；不含执行参数细节） */
  operationSummary: string;
  /** 来源标签（工具 / 子智能体 / 后台任务 / 用户发起） */
  sourceLabel: string;
  /** 过期时间（给人看的原样字符串；没有就不显示） */
  expiresAt: string;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function trimmed(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** 第一个非空字符串（保持调用处可读） */
function firstText(...values: unknown[]): string {
  for (const value of values) {
    const text = trimmed(value);
    if (text) return text;
  }
  return "";
}

/** 具体命令：明细里的「实际命令：」优先，其次 payload/工具参数里的 cmd / command */
function extractCommand(payload: Record<string, unknown>, detail: string): string {
  for (const line of detail.split(/\r?\n/)) {
    const text = line.trim();
    if (text.startsWith("实际命令：")) return text.slice("实际命令：".length).trim();
  }
  const args = asRecord(payload.arguments);
  return firstText(payload.cmd, payload.command, args.cmd, args.command);
}

/** 具体路径：工具参数里的 path / paths / files + 明细里的工作目录 */
function extractPaths(payload: Record<string, unknown>, detail: string): string[] {
  const out: string[] = [];
  const args = asRecord(payload.arguments);
  for (const key of ["path", "paths", "files"]) {
    const raw = args[key] ?? payload[key];
    if (typeof raw === "string" && raw.trim()) out.push(raw.trim());
    else if (Array.isArray(raw)) {
      for (const item of raw) {
        const text = trimmed(item);
        if (text) out.push(text);
      }
    }
  }
  for (const line of detail.split(/\r?\n/)) {
    const match = /^(?:工作目录|目录|路径)[:：]\s*(.+)$/.exec(line.trim());
    const value = match?.[1]?.trim();
    if (value) out.push(value);
  }
  return [...new Set(out)];
}

function prettyJson(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/**
 * 审批载荷 → 用户可见事实（**唯一**的一份整理）。
 *
 * 取值顺序沿用既有 approvalIntent（description → tool_name → name → explanation → reason），
 * 保证弹窗的第一句话没有回归。
 */
export function approvalFacts(
  input:
    | {
        kind?: string;
        payload?: Record<string, unknown>;
        state?: string;
        requiresConfirmations?: number;
        confirmedStage?: number;
        purpose?: string;
        operationSummary?: string;
        source?: string;
        expiresAt?: string;
      }
    | null
    | undefined,
): ApprovalFacts | null {
  if (!input) return null;
  const p = asRecord(input.payload);
  const kind = String(input.kind ?? "");
  const intent = approvalIntent(p);
  const explanation = trimmed(p.explanation);
  const description = firstText(p.description, p.tool_name, p.name) || intent;
  /**
   * 「为什么需要」：模型 explanation 已经单独成段时只在后端另有 reason 时显示，
   * 否则用 explanation 兜底 —— 同一句话不重复出现。
   */
  const reasonText = trimmed(p.reason);
  const explanationShown = explanation && explanation !== description ? explanation : "";
  const whyText = explanationShown
    ? reasonText && reasonText !== description && reasonText !== explanationShown
      ? reasonText
      : ""
    : reasonText && reasonText !== description
      ? reasonText
      : "";

  const capabilities = approvalCapabilities(p);
  const sideEffectRaw = capabilities.find((c) => c.startsWith("副作用："));
  const sideEffect = sideEffectRaw ? sideEffectRaw.slice("副作用：".length).trim().toLowerCase() : "";
  const CHANGE_SUMMARY: Record<string, string> = {
    destructive: "会删除或覆盖已有数据",
    write: "会写入或修改数据",
    read: "只读取，不修改数据",
    pure: "不修改任何数据",
  };

  const risks: string[] = [];
  const yes = (prefix: string) => capabilities.some((c) => c.startsWith(prefix) && c.includes("是"));
  if (yes("联网：")) risks.push("会联网");
  if (yes("写入文件：")) risks.push("会修改文件");
  if (capabilities.some((c) => /读取文件：是/.test(c))) risks.push("会读取文件");
  if (yes("启动进程：")) risks.push("会执行命令");
  if (capabilities.some((c) => c.startsWith("使用凭据：") && !c.endsWith("无"))) risks.push("会使用凭据");
  if (!risks.length && capabilities.length) risks.push("只读");

  const summary = trimmed(p.test_summary);
  const testDetails = Array.isArray(p.test_details) ? (p.test_details as unknown[]) : [];
  let verification: ApprovalVerification | null = null;
  if (summary || testDetails.length) {
    const unverified = summary === "" || /^n\/a/i.test(summary);
    verification = {
      verified: !unverified,
      label: unverified ? "未验证" : "已验证",
      detail: unverified && /subagent/i.test(summary) ? "子 agent 型工具，没有自动测试报告" : summary,
    };
  }

  const rawScope = trimmed(p.scope);
  const longTerm =
    rawScope === "long_term" ||
    (!rawScope && ["tool_create", "credential_grant", "high_impact_knowledge"].includes(kind));

  const budgetRaw = asRecord(p.subagent_budget);
  const budgetNumber = (value: unknown, fallback: number): number => {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  };
  const budgetCandidate: ApprovalBudget = {
    maxIterations: budgetNumber(budgetRaw.max_iterations, 5),
    maxTokens: budgetNumber(budgetRaw.max_tokens, 100000),
    outputLimitChars: budgetNumber(budgetRaw.output_limit_chars, 2000),
  };
  const isSubagentCreate = kind === "tool_create" && p.tool_type === "subagent";
  /**
   * F06：两类子智能体审批必须分开呈现（COMMON §4.1）。
   *
   * * 旧 tool_create 子智能体创建审批（老事件没有 state，走 /respond overrides）：
   *   预算**可修改**，执行器（tools/lifecycle.py）真的采用用户批准的值；
   * * R9 生命周期记录（有 state，/decide 不支持 overrides）：只有只读值，
   *   绝不显示可编辑控件或「可修改后批准」文案，避免「看起来改了、实际用旧值」。
   */
  const lifecycleStateValue = String(input.state ?? "").trim();
  const isLifecycleRecord = lifecycleStateValue.length > 0;
  /**
   * 只读判据：已批准 / 执行中（含恢复出来的 pending 快照）与所有终态都不允许再改预算
   * —— 这些状态的执行已按登记值开始或结束，界面不能再承诺「改了会用新值」。
   * 只有仍在等待用户决定的记录（waiting / 老事件无 state）保留可编辑。
   */
  const BUDGET_LOCKED_STATES = new Set([
    "approved",
    "executing",
    "completed",
    "failed",
    "rejected",
    "cancelled",
    "expired",
    "invalidated",
    "interrupted_unverified",
  ]);
  const budgetLocked = isLifecycleRecord && BUDGET_LOCKED_STATES.has(lifecycleStateValue);
  const hasBudgetPayload = Object.keys(budgetRaw).length > 0 || isSubagentCreate;
  const budget: ApprovalBudget | null = isSubagentCreate && !budgetLocked ? budgetCandidate : null;
  // 不能编辑但确有预算事实（锁定状态 / 非子智能体创建路径）：照实只读展示
  const budgetReadOnly: ApprovalBudget | null =
    !budget && hasBudgetPayload ? budgetCandidate : null;

  const rows: { label: string; value: string }[] = [];
  const pushRow = (label: string, value: unknown) => {
    const text = trimmed(value) || (typeof value === "number" ? String(value) : "");
    if (!text || text === description) return;
    rows.push({ label, value: text });
  };
  pushRow("工具", p.name);
  if (p.tool_type) pushRow("类型", p.tool_type === "subagent" ? "子 agent 型" : "函数型");
  pushRow("凭据", p.key_id);
  pushRow("授权工具", p.tool_name);
  pushRow("知识内容", p.content);
  if (p.action && !p.description) pushRow("建议动作", p.action);
  pushRow("示例请求", p.example);
  pushRow("相似请求数", p.source_count);

  // ---- R9 生命周期 v2 展示事实（老审批输入没有这些字段 → 空/0，不显示） ----
  const lifecycleState = String(input.state ?? "");
  const stateLabel = lifecycleState ? approvalStateLabel(lifecycleState) : "";
  const requiresConfirmations = Math.max(1, Number(input.requiresConfirmations ?? 1));
  const confirmedStage = Math.max(0, Number(input.confirmedStage ?? 0));
  const isInternalData = requiresConfirmations > 1;
  const confirmationProgress = isInternalData
    ? (confirmedStage >= requiresConfirmations
        ? "已完成 " + requiresConfirmations + "/" + requiresConfirmations + " 次确认"
        : "已确认 " + confirmedStage + "/" + requiresConfirmations + " 次（还差 " + (requiresConfirmations - confirmedStage) + " 次）")
    : "";
  const purpose = trimmed(input.purpose);
  const operationSummary = trimmed(input.operationSummary);
  const sourceLabel = approvalSourceLabel(String(input.source ?? ""));
  const expiresAt = trimmed(input.expiresAt);

  const advLines: string[] = [];
  if (p.policy_fingerprint) advLines.push(`策略指纹：${String(p.policy_fingerprint)}`);
  if (p.action) advLines.push(`内部动作名：${String(p.action)}`);
  if (p.risk) advLines.push(`沙箱判定：${String(p.risk)}`);
  for (const detail of testDetails) {
    const d = asRecord(detail);
    const name = String(d.name ?? "未命名检查");
    const passed = d.passed ? "通过" : "失败";
    const extra = d.detail ? ` · ${String(d.detail)}` : "";
    advLines.push(`测试 ${name}：${passed}${extra}`);
  }

  return {
    kind,
    description,
    explanation: explanation && explanation !== description ? explanation : "",
    why: whyText,
    access: Array.isArray(p.access) ? p.access.map((x) => String(x)).filter((x) => x.trim().length > 0) : [],
    capabilities,
    capabilityList: capabilities.filter((c) => !c.startsWith("副作用：")),
    sideEffect,
    changeSummary: CHANGE_SUMMARY[sideEffect] ?? "",
    scopeLabel: longTerm
      ? "长期生效：同意后它会一直可用（或一直生效），直到你撤销"
      : "仅这一次：只对本次操作有效，之后同类操作会再问你",
    command: extractCommand(p, trimmed(p.detail)),
    paths: extractPaths(p, trimmed(p.detail)),
    params: prettyJson(p.arguments),
    detail: trimmed(p.detail),
    risks,
    highRisk: risks.some((r) => r !== "只读" && r !== "会读取文件"),
    verification,
    budget,
    budgetReadOnly,
    isSubagentCreate,
    rows,
    advanced: { lines: advLines, raw: prettyJson(p) },
    lifecycleState,
    stateLabel,
    confirmationProgress,
    requiresConfirmations,
    confirmedStage,
    isInternalData,
    purpose,
    operationSummary,
    sourceLabel,
    expiresAt,
  };
}

/** APPROVAL_SOURCE → 中文标签（未知来源如实显示原值）。 */
function approvalSourceLabel(source: string): string {
  switch (source) {
    case "tool":
      return "工具执行";
    case "subagent":
      return "子智能体";
    case "background":
      return "后台任务";
    case "user":
      return "你发起的操作";
    default:
      return source ? "来源：" + source : "";
  }
}

export const useApprovalsStore = defineStore("approvals", {
  state: () => ({
    queue: [] as ApprovalItem[],
    responding: null as string | null,
    /** 最近一次 respond 失败原因（成功后清空）；用于「失败 ≠ 已授权」的可见反馈 */
    error: null as string | null,
    /**
     * 用户已经「稍后处理」：窗口收起但待审批项保留。
     * 直接把窗口藏起来而不做这个标记，会留下一个用户找不到的待审批项；反之
     * 无条件弹出，会在用户正编辑表单时抢走焦点（历史录像：工具确认盖住凭据编辑）。
     */
    deferred: false,
    /**
     * 过程区里已经**内联显示按钮**的那条审批 id。
     *
     * 同一时刻只允许一套按钮：内联卡在显示时，统一入口不再对同一个 approval_id
     * 显示按钮（按 id 门控，不是按「有没有审批」）。
     */
    inlineId: null as string | null,
    /**
     * 对话页统一审批层接管期间置 true：过程区内联卡不再声明按钮，按钮只留在
     * 统一弹窗（同一时刻只有一套有效提交按钮）。其他页面保持 false，内联审批照旧。
     */
    inlineSuppressed: false,
    /** R9：pending 恢复是否已尝试（失败可重试；不因一次失败永久放弃） */
    recovered: false,
    /** R9：恢复请求进行中（重试按钮防重复；恢复不改变任何审批状态） */
    recoveryBusy: false,
    /** R9：pending 恢复失败原因（只影响「恢复」这件事，不改变已有待办） */
    recoveryError: null as string | null,
  }),
  getters: {
    current: (state) => state.queue[0] ?? null,
    /** 队列里有待办、且用户没有选择稍后 → 窗口应当显示 */
    visible: (state) => state.queue.length > 0 && !state.deferred,
    /** 待确认数量（入口文案用） */
    pendingCount: (state) => state.queue.length,
    /** 内联卡正在显示的就是当前这条审批 */
    inlineClaimed: (state) =>
      !!state.inlineId && state.queue.length > 0 && state.queue[0]?.approval_id === state.inlineId,
    /** 内联审批是否被统一审批层接管（接管时内联卡不显示按钮） */
    inlineAllowed: (state) => !state.inlineSuppressed,
  },
  actions: {
    enqueue(
      approvalId: string,
      kind: string,
      payload: Record<string, unknown>,
      opts: {
        autoOpen?: boolean;
        turnId?: string | null;
        sessionId?: string | null;
        requestDigest?: string | null;
        /** R9：v2 状态机字段（state / confirmed_stage / requires_confirmations / purpose …） */
        meta?: Partial<ApprovalItem>;
      } = {},
    ) {
      if (this.queue.some((a) => a.approval_id === approvalId)) return;
      const first = this.queue.length === 0;
      this.queue.push({
        approval_id: approvalId,
        kind,
        payload,
        turnId: opts.turnId ?? null,
        sessionId: opts.sessionId ?? null,
        requestDigest: opts.requestDigest ?? null,
        ...(opts.meta ?? {}),
      });
      // 编辑中到达的确认不抢焦点：保留待办并亮出可发现的入口，由用户主动打开。
      if (first) this.deferred = opts.autoOpen === false;
      else if (opts.autoOpen !== false) this.deferred = false;
    },
    /** 过程区内联卡声明「这条审批的按钮由我显示」（按 approval_id） */
    claimInline(approvalId: string) {
      if (approvalId) this.inlineId = approvalId;
    },
    /** 释放内联声明（内联卡卸载 / 轮次结束）：全局入口恢复显示 */
    releaseInline(approvalId?: string | null) {
      if (!approvalId || this.inlineId === approvalId) this.inlineId = null;
    },
    /**
     * 「查看完整信息」：把这条审批交给原来的弹窗（契约 §1.3）。
     *
     * 先释放内联声明、再让窗口显示 —— 同一 approval_id 任一时刻**只有一套按钮**：
     * 弹窗拿到之后内联卡不再显示批准/拒绝，用户按 Esc /「稍后处理」收起弹窗时，
     * 内联卡会重新声明并由它接管（见 TurnProcess 的门控 watch）。
     */
    showFull(approvalId: string) {
      if (!approvalId) return;
      if (this.inlineId === approvalId) this.inlineId = null;
      this.openNow();
    },
    /** 用户主动打开（入口点击 / 直接相关的确认） */
    openNow() {
      if (this.queue.length) this.deferred = false;
    },
    /** 稍后处理：只收起窗口，待审批项与失败提示都保留 */
    defer() {
      if (this.queue.length) this.deferred = true;
    },
    /**
     * 后端报告某项审批已经有结局（APPROVAL_RESULT）。
     *
     * 本地通常已经处理过（用户点了批准/拒绝），但同一审批也可能由别处应答、
     * 或本地状态与后端不一致。按 id 收敛是幂等的：不在队列里就什么都不做；
     * 这样不会留下「看不见的待审批项」，也不会把已经处理过的项重复移除。
     */
    resolve(approvalId: string) {
      if (!approvalId) return;
      const before = this.queue.length;
      this.queue = this.queue.filter((a) => a.approval_id !== approvalId);
      if (this.queue.length !== before) this.error = null;
      if (!this.queue.length) this.deferred = false;
    },
    /**
     * 用服务端的权威快照**替换**待办集合。
     *
     * 服务器没有列出来的审批 = 已经不再 pending（被批准/拒绝/过期/取消），
     * 本地必须移除，否则会出现「后端早已有结局，界面还留着 Allow / Reject」。
     * 只做移除、不在这里添加：添加要按 kind 决定走哪条 UI（见 events store 的统一入口）。
     */
    reconcile(keepIds: string[]) {
      const keep = new Set(keepIds);
      const before = this.queue.length;
      this.queue = this.queue.filter((a) => keep.has(a.approval_id));
      if (this.queue.length !== before) this.error = null;
      if (!this.queue.length) this.deferred = false;
    },
    // -- R9：pending 恢复 / 决定 / 三次确认 / 执行历史 ---------------------

    /** 把一条服务端 pending 事实写进队列（已存在则只更新状态，不重复入队）。 */
    applyPending(pending: PendingApproval, opts: { autoOpen?: boolean } = {}) {
      const meta = approvalMetaFromPending(pending);
      const existing = this.queue.find((a) => a.approval_id === pending.approval_id);
      if (existing) {
        Object.assign(existing, meta, { payload: pending.payload });
        // 后端说已经不在等待（还会列在 pending 里但状态前进）→ 保留展示真实状态
        return existing;
      }
      return this.enqueue(pending.approval_id, pending.kind || "unknown", pending.payload, {
        // 恢复出来的审批不抢焦点：保留待办 + 亮出入口（与 RESYNC 恢复同一规则）
        autoOpen: opts.autoOpen ?? false,
        meta,
      });
    },

    /**
     * 断线 / 刷新 / 重启后恢复待审批项（契约 §5.1 / §5.4 的唯一恢复入口）。
     *
     * * 成功：服务端 pending 就是权威集合 —— 已在队列但服务端没列出的项移除；
     * * 404 / 老后端没有这个接口：保持现状并标记已尝试（不伪造恢复成功）；
     * * 其它失败：保留已有待办，如实给出可重试原因。
     */
    async recoverPending(): Promise<void> {
      if (this.recoveryBusy) return;
      if (this.responding) return;
      this.recoveryBusy = true;
      try {
        const pending = await getPendingApprovals();
        // kind=continue 的「继续/停止」走会话层操作条，不进入审批队列（与事件入口一致）
        const rows = pending.filter((item) => item.kind !== "continue");
        this.reconcile(rows.map((item) => item.approval_id));
        for (const item of rows) this.applyPending(item, { autoOpen: false });
        this.recoveryError = null;
        this.recovered = true;
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 404) {
          // 老后端（W1 未合入 / 旧版本）：没有 pending 接口，不当作失败，也不清空待办
          this.recoveryError = null;
          this.recovered = true;
          return;
        }
        // 失败：已有待办原样保留（没有服务端证据就不清空）；
        // 「没读到」不等于「没有待审批」—— 界面必须依据这里显示可重试原因。
        this.recoveryError =
          "恢复待审批项失败：" + ((e as Error)?.message || "未知原因") + "（已有待办保留；可以重试）";
        this.recovered = true;
      } finally {
        this.recoveryBusy = false;
      }
    },

    /**
     * 用户点「重试恢复」：与启动恢复同一条只读路径。
     *
     * 只重新读服务端快照：不重置任何审批期限、不重复创建、不推进确认阶段。
     */
    async retryRecovery(): Promise<boolean> {
      if (this.recoveryBusy) return false;
      if (this.responding) {
        this.recoveryError = "有审批正在提交，等这次提交完成后再重试恢复。";
        return false;
      }
      this.recoveryError = null;
      await this.recoverPending();
      return !this.recoveryError;
    },

    /**
     * 用户对当前审批做出决定（R9 新接口：approve / reject / cancel）。
     * 失败**不移除**待办：看起来批准了、后端没批准是绝不允许的状态。
     */
    async decide(decision: "approve" | "reject" | "cancel", approvalId?: string) {
      const item = approvalId
        ? this.queue.find((a) => a.approval_id === approvalId)
        : this.current;
      if (!item || this.responding) return;
      // 内部数据多次确认：批准的第一动作是「第 1 次确认」，不是决定
      if (decision === "approve" && (item.requiresConfirmations ?? 1) > 1) {
        return this.confirmNext(item.approval_id);
      }
      if (decision === "approve") return this._decideItem(item, "approve");
      return this._decideItem(item, decision);
    },

    async _decideItem(item: ApprovalItem, decision: "approve" | "reject" | "cancel") {
      if (item.stale) return;
      this.responding = item.approval_id;
      this.error = null;
      item.lifecycleError = undefined;
      try {
        const updated = await decideApproval(item.approval_id, decision, {
          expectedRevision: item.revision,
        });
        if (updated) this._applyLifecycle(item, updated);
        this._settleAfterAction(item, decision === "approve" ? "approved" : decision === "reject" ? "rejected" : "cancelled");
      } catch (e) {
        this._lifecycleFailure(item, e);
      } finally {
        this.responding = null;
      }
    },

    /**
     * 内部数据「连续三次确认」的下一击（契约 §4）。
     *
     * 前端只发 `stage = confirmed_stage + 1`，是否允许由后端 CAS 判定：
     * 越级 / 重复 / 范围变化后重置都会返回 409，这里把原因原样显示并把进度
     * 以后端返回为准覆盖（绝不在前端自行把 stage 加一）。
     */
    async confirmNext(approvalId?: string) {
      const item = approvalId
        ? this.queue.find((a) => a.approval_id === approvalId)
        : this.current;
      if (!item || this.responding) return;
      if (item.stale) return;
      const required = Math.max(1, item.requiresConfirmations ?? 1);
      const done = Math.max(0, item.confirmedStage ?? 0);
      if (done >= required) return;
      this.responding = item.approval_id;
      this.error = null;
      item.lifecycleError = undefined;
      const stage = done + 1;
      try {
        const updated = await confirmApprovalStage(item.approval_id, stage, {
          expectedRevision: item.revision,
        });
        if (updated) this._applyLifecycle(item, updated);
        if (stage >= required) {
          // 第三次确认成功：后端状态变为 approved 并自动开始执行 —— 没有更多用户动作
          this._settleAfterAction(item, "confirmed", "已完成 " + required + "/" + required + " 次确认，后端已批准并开始执行");
        }
      } catch (e) {
        this._lifecycleFailure(item, e);
      } finally {
        this.responding = null;
      }
    },

    /** 把服务端返回的 v2 事实写回待办项（以服务端为准，不自行推进状态）。 */
    _applyLifecycle(item: ApprovalItem, updated: PendingApproval) {
      Object.assign(item, approvalMetaFromPending(updated), { payload: updated.payload });
    },

    /** 动作成功后的收口：移除待办 / 记录最近结局；终态才出队。 */
    _settleAfterAction(item: ApprovalItem, phase: string, summary = "") {
      const state = item.state ?? phase;
      const settled =
        summary ||
        (phase === "approved"
          ? "已批准，等待后端执行"
          : phase === "rejected"
            ? "已拒绝，不会执行"
            : phase === "cancelled"
              ? "已取消，不会执行"
              : "");
      // 结果事实只有一处：stores/approvalResults.ts（按 approval_id 建键）。
      // 用户决定是**真的发生过**的事实；执行结果仍以后端事件/历史为准。
      useApprovalResultsStore().recordLocal({
        approvalId: item.approval_id,
        phase,
        state,
        summary: settled,
        ok: phase === "rejected" || phase === "cancelled" ? false : null,
        kind: item.kind,
        label: item.label ?? "",
      });
      // 决定 / 三次确认成功 = 不再等待用户输入；执行结果由 APPROVAL_RESULT 事件补上
      this.resolve(item.approval_id);
    },

    /** 决定 / 确认失败：保留待办、给出结论先行的原因；409 表示状态已变（以后端为准）。 */
    _lifecycleFailure(item: ApprovalItem, error: unknown) {
      const status = (error as { status?: number }).status;
      const message = (error as Error)?.message || "未知原因";
      if (status === 404) {
        item.stale = true;
        this.error = "这项确认已经有结局（可能已在别处处理或已过期），不会再等待你的授权。未做出任何授权：" + message;
        return;
      }
      item.lifecycleError = message;
      this.error = "审批请求失败，未做出任何授权：" + message + "（可重试）";
    },

    /**
     * 处理 APPROVAL_RESULT（W1：复用既有事件，payload.phase 区分状态）。
     *
     * * waiting 之后的进度（confirmed / approved / executing）：更新状态，不移除；
     * * 终态（executed / failed / invalidated / interrupted_unverified / expired / rejected / cancelled）：
     *   移除待办，并记下最近结局（失败/待核对也给可见事实，不静默）。
     */
    recordResult(d: Record<string, unknown>) {
      const approvalId = String(d.approval_id ?? "");
      const phase = String(d.phase ?? "");
      if (!approvalId) return;
      // 旧后端没有 phase：保持旧语义（结果事件 = 已有结局，按 id 收敛）
      if (!phase) {
        this.resolve(approvalId);
        return;
      }
      const state = approvalStateFromPhase(phase);
      const item = this.queue.find((a) => a.approval_id === approvalId);
      if (item) {
        // 中间态事件（confirm_stage）也带进度事实：即使 phase 没有对应状态名，
        // 也要把 confirmed_stage / requires_confirmations 以后端为准写回来。
        if (state) item.state = state;
        if (typeof d.confirmed_stage === "number") item.confirmedStage = d.confirmed_stage;
        if (typeof d.requires_confirmations === "number") item.requiresConfirmations = d.requires_confirmations;
      }
      /**
       * 新结果模型（按 approval_id 建键）：approved / executing 也要接住 ——
       * 「已批准、等待执行」不是完成；终态照实落。事件里的 detail（含批量
       * item_id 逐项结果）原样保留，不再丢弃。
       */
      const results = useApprovalResultsStore();
      if (phase !== "confirm_stage" && phase !== "confirmed") {
        results.recordEvent(d);
      }
      const terminal = new Set([
        "executed",
        "failed",
        "invalidated",
        "interrupted_unverified",
        "expired",
        "rejected",
        "cancelled",
      ]);
      if (terminal.has(phase)) {
        this.resolve(approvalId);
        // 有结局后刷新历史（失败也不打断：历史只是补充事实；错误在结果面板可见可重试）
        void results.loadHistory().catch(() => undefined);
      }
      // 进度事件（confirm_stage / confirmed）只更新待办进度，不伪造结果条目：
      // 「确认到第 N 次」不是执行结局，结果模型不写它。
    },

    async respond(decision: "approved" | "rejected", overrides?: Record<string, unknown>) {
      const item = this.current;
      if (!item) return;
      await this._respondItem(item, decision, overrides);
    },
    /**
     * 按 approval_id 应答（过程区内联卡用）。
     *
     * 为什么不能直接用 respond()：内联卡上的按钮属于**那一条**审批；
     * 请求在飞或审批已被别处处理时 current 可能已经换成下一条 ——
     * 按 id 定位，绝不误伤下一项；重复点击也只会命中同一条，被 responding 挡住。
     */
    async respondById(
      approvalId: string,
      decision: "approved" | "rejected",
      overrides?: Record<string, unknown>,
    ) {
      const item = this.queue.find((a) => a.approval_id === approvalId);
      if (!item) return;
      await this._respondItem(item, decision, overrides);
    },
    /** 一条审批的应答（respond / respondById 共用，语义完全一致） */
    async _respondItem(
      item: ApprovalItem,
      decision: "approved" | "rejected",
      overrides?: Record<string, unknown>,
    ) {
      // 双提交防护：请求进行中忽略后续点击（按钮同时 disabled）
      if (this.responding) return;
      // 已经判定失效的项不再发请求：后端已经没有它了，重试只会一直 404
      if (item.stale) return;
      /**
       * R9：v2 审批只走新接口（decide / confirm）；旧审批保留 /respond 兼容。
       * 内部数据的三次确认：点「允许」的第一次动作是第 1 次确认（stage 由后端 CAS）。
       */
      if (isLifecycleApproval(item)) {
        /**
         * F06：v2 生命周期路径（/decide）不支持 overrides。若调用方仍带了预算修改，
         * 必须明确拒绝这次提交 —— 绝不能「看起来改了、实际执行用旧预算」。
         */
        if (overrides && Object.keys(overrides).length > 0) {
          this.error =
            "这条记录由后端生命周期管理，不支持修改执行预算；这次没有提交任何决定。请按只读预算核对后再批准。";
          item.lifecycleError = this.error;
          return;
        }
        if (decision === "rejected") return this.decide("reject", item.approval_id);
        if ((item.requiresConfirmations ?? 1) > 1) return this.confirmNext(item.approval_id);
        return this.decide("approve", item.approval_id);
      }
      this.responding = item.approval_id;
      this.error = null;
      try {
        await api.respondApproval(item.approval_id, decision, overrides, {
          turnId: item.turnId,
          sessionId: item.sessionId,
          requestDigest: item.requestDigest,
        });
        // 成功才出队（按 id 过滤，避免并发事件让 shift 移除错项）
        this.queue = this.queue.filter((a) => a.approval_id !== item.approval_id);
        if (!this.queue.length) this.deferred = false;
        // 这条已经没有待办了：内联声明一起释放，避免门控残留
        if (this.inlineId === item.approval_id) this.inlineId = null;
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status === 404) {
          // 「没有这条审批 / 已经有结局」：它已经被处理过。不静默移除（用户得知道发生了什么），
          // 但也不留成一个永远点不掉的待办 —— 标记失效，界面只给一个「知道了」。
          item.stale = true;
          this.error = `这项确认已经有结局（可能已在别处处理或已过期），不会再等待你的授权。未做出任何授权：${(e as Error).message}`;
          return;
        }
        // 失败保留审批项：UI 上「看起来批准了、后端没批准」是绝不允许的状态
        // 文案必须先说结论：失败 ≠ 已授权，再给原因与退路。
        this.error = `审批请求失败，未做出任何授权：${(e as Error).message}（可重试）`;
      } finally {
        this.responding = null;
      }
    },
  },
});
