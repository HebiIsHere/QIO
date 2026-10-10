import { defineStore } from "pinia";
import { api } from "../services/api";

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
}

/**
 * 「它想做什么」：审批里最该先读的一句人话。
 *
 * 与 ApprovalModal 的 intent 共用这一份取值顺序
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
 * 契约 §1.3：这份整理**只有一处**（approvalFacts），ApprovalModal 与过程区内联卡
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
  budget: ApprovalBudget | null;
  isSubagentCreate: boolean;
  /** 结构化补充行（工具 / 类型 / 凭据 / 授权工具 / 知识内容 / 示例请求 …） */
  rows: { label: string; value: string }[];
  /** 高级详情（默认折叠）：策略指纹 / 逐条测试结果 / raw params */
  advanced: { lines: string[]; raw: string };
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
  input: { kind?: string; payload?: Record<string, unknown> } | null | undefined,
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
  const budget: ApprovalBudget = {
    maxIterations: budgetNumber(budgetRaw.max_iterations, 5),
    maxTokens: budgetNumber(budgetRaw.max_tokens, 100000),
    outputLimitChars: budgetNumber(budgetRaw.output_limit_chars, 2000),
  };
  const isSubagentCreate = kind === "tool_create" && p.tool_type === "subagent";

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
    budget: isSubagentCreate ? budget : null,
    isSubagentCreate,
    rows,
    advanced: { lines: advLines, raw: prettyJson(p) },
  };
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
     * 同一时刻只允许一套按钮：内联卡在显示时，全局 ApprovalEntry / ApprovalModal
     * 不再对同一个 approval_id 显示按钮（按 id 门控，不是按「有没有审批」）。
     */
    inlineId: null as string | null,
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
