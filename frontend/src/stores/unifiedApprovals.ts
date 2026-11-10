/**
 * 对话页 v2 · 统一审批 store（D 负责）。
 *
 * 把「所有需要用户明确批准/拒绝的单次申请」收进**一个队列**，一次只展示一项：
 *
 * * 工具/权限/凭据申请：身份是 approval_id，真实的提交接口是
 *   `POST /api/approvals/{approval_id}/respond`（复用 stores/approvals 的既有校验、
 *   单次消费、stale 处理与预算 overrides）；
 * * 知识保存申请：身份是 knowledge id（没有 approval_id），提交走知识自己的原子接口
 *   `POST /api/knowledge/{id}/approval`；
 * * 话题申请：身份同样是 approval_id（后端走既有 ApprovalService.request，kind ∈
 *   {topic_switch, topic_create}），`POST /api/approvals/{id}/respond` 提交，
 *   只多带可选 edits（新话题名）/scope（留在当前话题）。三类**绝不串线**：分派按
 *   结构化字段（kind / 有没有 approval_id），不看标题文案。
 *
 * 队列规则（契约 §4.4）：
 * - 同一审批 id / 同一知识 id 只入队一次；
 * - 新申请不替换正在阅读/编辑的那一项；用户可主动切换；
 * - 关闭/稍后处理只隐藏，不批准也不拒绝；已暂缓的同一项不再自动弹；
 * - 提交期间防重复；失败 / 超时 / 失效 / 已在别处处理分别说明；
 * - 自动弹出：工具/权限类（含后台工作与子智能体）按规则自动；主对话知识保存
 *   从不自动，只更新入口数量；已有其他弹窗/用户正在输入时不叠加。
 */
import { defineStore } from "pinia";
import { request } from "../services/api";
import type { MemoryPendingListing, PendingItemView } from "../services/api";
import { approvalFacts, useApprovalsStore, type ApprovalItem } from "./approvals";
import { useApprovalResultsStore } from "./approvalResults";
import { canAutoOpenApproval } from "./feedbackGate";
import {
  useSessionStore,
  type TopicAnswerRecovery,
  type TopicPhase,
} from "./session";

export type UnifiedKind = "tool" | "knowledge" | "topic";
export type ToolOrigin = "main_agent" | "background" | "subagent" | "unknown";
export type LoadStatus = "idle" | "loading" | "ready" | "error";

/** 来源文案：如实说清是谁提出的（不把内部枚举直接给用户看）。 */
export const TOOL_ORIGIN_LABELS: Record<ToolOrigin, string> = {
  main_agent: "主智能体",
  background: "后台工作",
  subagent: "子智能体",
  unknown: "来源未记录",
};

export const APPROVAL_KIND_TITLES: Record<string, string> = {
  tool_create: "工具创建审批",
  credential_grant: "凭据授权审批",
  high_impact_knowledge: "知识维护确认",
  tool_execution: "需要你确认的操作",
  computer: "电脑操作审批",
  dependency_install: "安装依赖",
  tool_candidate: "工具建议确认",
  create_topic: "新建话题确认",
  continue: "继续本轮",
};

export const KNOWLEDGE_CATEGORY_LABELS: Record<string, string> = {
  user_profile: "关于你",
  agent_self: "QIO 对自己的认识",
  goal: "长期目标",
  general_fact: "一般事实",
  tool_experience: "工具经验",
};

/** 「确定程度」是事实属性，不是审批状态（照实展示，不替用户判断真假）。 */
export const EPISTEMIC_LABELS: Record<string, string> = {
  user_explicit: "你明确说过",
  shared_understanding: "你们已形成的理解",
  qio_observed: "QIO 观察到",
  qio_inference: "QIO 的推断",
};

/** 后台维护类审批：来自后台工作（会自动弹出，但来源必须如实写出来）。 */
const BACKGROUND_APPROVAL_KINDS = new Set([
  "tool_candidate",
  "high_impact_knowledge",
  "maintenance",
  "dependency_install",
]);

/** 后端结构化来源字段 → 展示来源。按字段判定，不猜标题。 */
export function classifyToolOrigin(
  kind: string,
  payload: Record<string, unknown> | null | undefined,
): ToolOrigin {
  const p = (payload ?? {}) as Record<string, unknown>;
  const source = String(p.source ?? p.origin ?? "").trim().toLowerCase();
  if (
    source === "background" ||
    source === "maintenance" ||
    source.startsWith("maintenance_") ||
    BACKGROUND_APPROVAL_KINDS.has(kind)
  ) {
    return "background";
  }
  if (
    p.subagent === true ||
    Object.prototype.hasOwnProperty.call(p, "subagent_id") ||
    Object.prototype.hasOwnProperty.call(p, "agent_id") ||
    String(p.executor ?? "").toLowerCase().includes("subagent")
  ) {
    return "subagent";
  }
  if (!kind) return "unknown";
  return "main_agent";
}

export interface SourceFact {
  label: string;
  value: string;
}

/**
 * 来源 / 任务 / 执行者等事实行：**只从结构化字段取值**，缺失就不显示。
 * 不编造「来自某话题」之类后端没给的归属。
 */
export function toolSourceFacts(item: {
  kind: string;
  origin: ToolOrigin;
  approvalId: string;
  payload: Record<string, unknown> | null | undefined;
}): SourceFact[] {
  const p = (item.payload ?? {}) as Record<string, unknown>;
  const text = (value: unknown): string => (typeof value === "string" ? value.trim() : "");
  const facts: SourceFact[] = [{ label: "来源", value: TOOL_ORIGIN_LABELS[item.origin] }];
  if (item.origin === "background" && item.kind === "high_impact_knowledge") {
    facts.push({ label: "来源说明", value: "后台记忆整理提出的知识调整；不会自动执行" });
  }
  const executor = text(p.executor) || text(p.agent) || text(p.subagent);
  if (executor) facts.push({ label: "执行者", value: executor });
  if (text(p.tool_name)) facts.push({ label: "工具", value: text(p.tool_name) });
  const task = text(p.workspace) || text(p.task_id);
  if (task) facts.push({ label: "来源任务", value: task });
  const topic = text(p.topic_id);
  if (topic) facts.push({ label: "话题", value: topic });
  const knowledgeId = text(p.knowledge_id);
  if (knowledgeId) facts.push({ label: "知识", value: knowledgeId });
  if (text(p.action)) {
    const ACTION_LABELS: Record<string, string> = {
      degrade: "降低使用权重",
      expire: "停用这条知识",
      correct: "修正这条知识",
      merge: "合并这条知识",
      delete: "删除这条知识",
    };
    facts.push({
      label: "建议动作",
      value: ACTION_LABELS[text(p.action)] ?? text(p.action),
    });
  }
  return facts;
}

export interface KnowledgePendingDetail {
  id: string;
  version: number | null;
  content: string;
  category: string;
  scope: string[];
  epistemicStatus: string | null;
  source: string | null;
  evidenceIds: string[];
  updatedAt: string | null;
}

/** 知识候选详情（范围/依据/确定程度）的加载状态：事件先到、权威详情随后补齐。 */
export type DetailStatus = "ready" | "loading" | "failed";

export interface KnowledgeApprovalItem extends KnowledgePendingDetail {
  key: string;
  /** 候选被提出时的原因（事件里有就显示；没有就不显示）。 */
  reason: string;
  arrivedAt: number;
  detailStatus: DetailStatus;
  detailError: string;
}

/** 话题申请的两种 legacy kind（后端 ApprovalService.request；payload 见契约 §2.1）。 */
export const TOPIC_APPROVAL_KINDS = ["topic_switch", "topic_create"] as const;
export type TopicApprovalKind = (typeof TOPIC_APPROVAL_KINDS)[number];

export function isTopicApprovalKind(
  kind: string | null | undefined,
): kind is TopicApprovalKind {
  return kind === "topic_switch" || kind === "topic_create";
}

/**
 * 话题申请的正文事实：**只**含契约 §2.1 允许展示的字段。
 *
 * 原问题 / 摘要 / 预览一律不进这里（payload 里也没有；即使后端多带了别的字段，
 * 映射时也只读下面这几个字段），从结构上杜绝把申请理由之外的内容露给用户。
 */
export interface TopicApprovalItem {
  /** topic:<approval_id>（与工具审批一样按 approval_id 唯一） */
  key: string;
  approvalId: string;
  kind: TopicApprovalKind;
  action: "switch" | "create";
  /** 当前话题名（from_topic_name；缺失时界面说「未记录」，不猜）。 */
  currentTopicName: string;
  /** switch 的目标话题（target_topic_*）；create 时为空。 */
  targetTopicName: string;
  targetTopicId: string;
  /** create 的模型建议名（用户可改）。 */
  suggestedName: string;
  /** 简短的申请原因（模型给，≤80 字）。 */
  reason: string;
  /** 这条申请属于哪一轮（payload.turn_id，缺失时回退审批行的 turn_id）。 */
  turnId: string;
  /**
   * 稳定操作身份（P1-05：payload.operation_id）。
   * 可选：旧后端没有该字段、组件测试也可以只造展示所需的最小字段。
   */
  operationId?: string;
  /** 带版本的参数（payload.param_version；缺失 null）。 */
  paramVersion?: number | null;
  /** 申请建立时的导航版本快照（payload.base_nav_version；缺失 null）。 */
  baseNavVersion?: number | null;
  /** 相似话题冲突（payload.conflict 结构化字段；缺失就是 null，不猜）。 */
  conflict?: TopicApprovalConflict | null;
  /** 是否已失效（后端结构化判定 / 本地 APPROVAL_RESULT 同步过来的事实）。 */
  invalidated?: boolean;
  invalidatedReason?: string;
  invalidatedCode?: string;
  approval: ApprovalItem;
}

/**
 * 相似话题冲突（契约 §6：冲突 ≠ 创建成功 ≠ 整轮结束）。
 * 只从 payload 的结构化字段读取；status 不认识就保留原值，不猜语义。
 */
export interface TopicApprovalConflict {
  status: "conflict_pending" | "conflict_confirmed" | "conflict_declined" | string;
  existingTopicId: string;
  existingTopicName: string;
}

export interface UnifiedApprovalItem {
  key: string;
  kind: UnifiedKind;
  /** 真实身份：approval_id（工具/话题类）或 knowledge id（知识类）。 */
  identity: string;
  version: number | null;
  title: string;
  origin: ToolOrigin;
  highRisk: boolean;
  stale: boolean;
  deferred: boolean;
  approval: ApprovalItem | null;
  knowledge: KnowledgeApprovalItem | null;
  /** kind === "topic" 时的正文事实（其余类型为 null）。 */
  topic: TopicApprovalItem | null;
  /** 稳定操作身份（payload.operation_id；旧后端为空串）。 */
  operationId: string;
  /** 相似话题二次确认的来源申请（payload.source_approval_id；旧后端为空串）。 */
  sourceTopicApprovalId: string;
  /** 是否已失效（失效 ≠ 拒绝；原确认不可执行、保留原因与恢复入口）。 */
  invalidated: boolean;
  invalidatedReason: string;
  invalidatedCode: string;
  /** 该条 operation+turn 匹配到本轮 TOPIC_PHASE 时的阶段（否则 null）。 */
  stage: TopicPhase | null;
  /** 上下文准备失败的恢复事实（只有 recoverable_failure 时非 null）。 */
  recovery: TopicAnswerRecovery | null;
}

export function unifiedKeyForTool(approvalId: string): string {
  return `tool:${approvalId}`;
}

export function unifiedKeyForKnowledge(knowledgeId: string): string {
  return `knowledge:${knowledgeId}`;
}

export function unifiedKeyForTopic(approvalId: string): string {
  return `topic:${approvalId}`;
}

/** 用户是否正在输入（与 events store 的判定保持一致）。 */
export function isUserEditing(): boolean {
  if (typeof document === "undefined") return false;
  const active = document.activeElement as HTMLElement | null;
  if (!active) return false;
  return (
    active.tagName === "INPUT" ||
    active.tagName === "TEXTAREA" ||
    active.isContentEditable === true
  );
}

/**
 * 是否已有**别的**模态打开（不叠加、不替换）。
 * 统一审批自己的对话框带 data-unified-approval-modal，不算「别的」。
 */
export function isBlockingModalOpen(): boolean {
  if (typeof document === "undefined") return false;
  const nodes = Array.from(
    document.querySelectorAll('[role="dialog"][aria-modal="true"], dialog[open]'),
  );
  return nodes.some((node) => !node.hasAttribute("data-unified-approval-modal"));
}

/**
 * 审批是否已经到期（真实期限来自后端；期限只读，不在前端放宽）。
 *
 * 到期后不再提供「批准」：本地钟只是最后一道保护，服务端自己也会拒绝；
 * 这里绝不改写 expiresAt，也不把过期说成已处理。
 */
export function isExpiredApproval(
  approval: ApprovalItem | null | undefined,
  now: number = Date.now(),
): boolean {
  if (!approval) return false;
  const state = String(approval.state ?? "").trim();
  if (state === "expired") return true;
  const raw = String(approval.expiresAt ?? "").trim();
  if (!raw) return false;
  const at = Date.parse(raw);
  return Number.isFinite(at) && at <= now;
}

/**
 * 距离这条审批到期的毫秒数（只读后端给出的真实期限）。
 *
 * F08：界面到期的**时钟**用它排定时器，不依赖后端再推事件。
 * 返回 null = 没有可解析的期限 / 状态已经是 expired（不需要再排时钟）；
 * 返回 0 = 已经到期但状态还没标（界面应立刻按事实显示，不在前端改写期限）。
 */
export function approvalExpiryRemaining(
  approval: ApprovalItem | null | undefined,
  now: number = Date.now(),
): number | null {
  if (!approval) return null;
  if (String(approval.state ?? "").trim() === "expired") return null;
  const raw = String(approval.expiresAt ?? "").trim();
  if (!raw) return null;
  const at = Date.parse(raw);
  if (!Number.isFinite(at)) return null;
  return Math.max(0, at - now);
}

export interface KnowledgeSubmitError {
  message: string;
  code: string;
  /** 需要重新拉一次待确认列表（版本/内容冲突、已在别处处理、超时结果未知）。 */
  refresh: boolean;
  /** 申请已不存在 / 已有结局：从队列里收口，不再提供提交按钮。 */
  terminal: boolean;
}

/** 把知识接口的结构化失败翻译成「结论先行」的中文（测试直接覆盖这一层）。 */
export function classifyKnowledgeSubmitError(error: unknown): KnowledgeSubmitError {
  const err = error as {
    status?: number;
    body?: { code?: string; error?: string; current?: { version?: number } };
    name?: string;
    message?: string;
  };
  const status = Number(err?.status ?? 0);
  const body = (err?.body ?? {}) as { code?: string; error?: string };
  const code = String(body.code ?? "");
  const detail = String(body.error ?? "").trim();
  if (status === 409) {
    if (code === "version_changed" || code === "content_changed" || code === "superseded") {
      return {
        message: "这条申请在你确认前已经变化，这次没有保存任何内容；已重新加载，请再看一遍后确认。",
        code: code || "content_changed",
        refresh: true,
        terminal: false,
      };
    }
    return {
      message: detail || "这条申请已经在别处被处理过；已重新加载待确认列表。",
      code: code || "already_handled",
      refresh: true,
      terminal: code === "already_handled" || code === "invalid_state",
    };
  }
  if (status === 404) {
    return {
      message: "这条申请已经不存在（可能已在别处处理）；没有保存任何内容。",
      code: "not_found",
      refresh: true,
      terminal: true,
    };
  }
  if (status === 400) {
    return {
      message: detail || "这次操作没有被服务端接受；没有保存任何内容。",
      code: code || "invalid",
      refresh: false,
      terminal: false,
    };
  }
  if (err?.name === "ApiTimeoutError") {
    return {
      message: "没有等到服务端响应：这次是否保存成功还不确定，正在重新核对待确认列表。",
      code: "timeout",
      refresh: true,
      terminal: false,
    };
  }
  if (status >= 500) {
    return {
      message: `服务端没有完成这次保存（${status}）；没有确认到结果，可以重试。`,
      code: "server_error",
      refresh: true,
      terminal: false,
    };
  }
  return {
    message: "没有连上服务端，这次是否保存成功还不确定；正在重新核对待确认列表。",
    code: "network",
    refresh: true,
    terminal: false,
  };
}

/** 把「记忆待确认」里的知识候选视图映射成知识保存申请（字段缺失就留空，不编造）。 */
export function knowledgeDetailFromView(view: PendingItemView): KnowledgePendingDetail | null {
  const ref = Array.isArray(view.knowledge) ? view.knowledge[0] : undefined;
  if (!ref || !ref.id) return null;
  return {
    id: String(ref.id),
    version: typeof ref.version === "number" ? ref.version : null,
    content: String(ref.content ?? ""),
    category: String(ref.category ?? ""),
    scope: Array.isArray(ref.scope) ? ref.scope.map((s) => String(s)) : [],
    epistemicStatus: ref.epistemic_status ? String(ref.epistemic_status) : null,
    source: ref.source ? String(ref.source) : null,
    evidenceIds: (view.evidence ?? [])
      .map((entry) => String((entry as { id?: unknown }).id ?? ""))
      .filter((id) => id.length > 0),
    updatedAt: view.updated_at ? String(view.updated_at) : null,
  };
}

/** 知识保存申请在队列里的排序：先到先处理。 */
function nextKnowledgeArrival(): number {
  return Date.now();
}

export interface KnowledgeApprovalResponse {
  ok: boolean;
  decision: string;
  already_handled?: boolean;
  message?: string;
  knowledge?: Record<string, unknown>;
}

/**
 * 知识保存申请的真实提交入口（统一弹窗与星球「待确认」共用同一个实现）。
 *
 * 只发一个请求、只走知识原子接口；调用方负责把 expected_* 原样带上，服务端据此
 * 拒绝「用户确认的不是当前这一版」。工具/权限审批**不能**调用本函数。
 */
export async function submitKnowledgeApprovalRequest(
  knowledgeId: string,
  decision: "approve" | "approve_modified" | "ignore" | "reject",
  options: {
    content?: string;
    expectedVersion?: number | null;
    expectedContent?: string | null;
    expectedUpdatedAt?: string | null;
  } = {},
): Promise<KnowledgeApprovalResponse> {
  const body: Record<string, unknown> = { decision };
  if (decision === "approve_modified") body.content = String(options.content ?? "");
  if (options.expectedVersion !== null && options.expectedVersion !== undefined) {
    body.expected_version = options.expectedVersion;
  }
  if (options.expectedContent) body.expected_content = options.expectedContent;
  if (options.expectedUpdatedAt) body.expected_updated_at = options.expectedUpdatedAt;
  return request<KnowledgeApprovalResponse>(
    `/api/knowledge/${encodeURIComponent(knowledgeId)}/approval`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

/* ------------------------------------------------------------------ */
/* P1R：阶段 / 失效 / 操作身份的只读派生                                  */
/* ------------------------------------------------------------------ */

/**
 * 当前轮的话题阶段事实（只读 session）。
 *
 * 旧后端没有 TOPIC_PHASE / session 尚未初始化 → 全部为空：界面不渲染阶段行、
 * 不伪造恢复信息，既有行为保持不变。
 */
function liveTopicState(): {
  stage: TopicPhase | null;
  operationId: string;
  turnId: string;
  recovery: TopicAnswerRecovery | null;
} {
  try {
    const session = useSessionStore();
    return {
      stage: session.stage,
      operationId: String(session.topicStageOperationId ?? ""),
      turnId: String(session.topicStageTurnId ?? ""),
      recovery: session.recovery,
    };
  } catch {
    return { stage: null, operationId: "", turnId: "", recovery: null };
  }
}

/** 阶段归属：operation 与 turn 都能对上（一方缺失时按另一方判断）才给这一条。 */
function stageForTopic(
  topic: { operationId?: string | null; turnId: string },
  live: ReturnType<typeof liveTopicState>,
): TopicPhase | null {
  if (!live.stage) return null;
  const operationId = String(topic.operationId ?? "").trim();
  if (operationId && live.operationId && operationId !== live.operationId) return null;
  if (topic.turnId && live.turnId && topic.turnId !== live.turnId) return null;
  return live.stage;
}

function payloadNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 相似话题冲突：只读 payload 的结构化字段（缺失就是 null，不猜）。 */
function topicConflictFromPayload(
  payload: Record<string, unknown>,
): TopicApprovalConflict | null {
  const raw = payload.conflict;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const status = String(record.status ?? "").trim();
  if (!status) return null;
  return {
    status,
    existingTopicId: String(record.existing_topic_id ?? record.existingTopicId ?? "").trim(),
    existingTopicName: String(
      record.existing_topic_name ?? record.existingTopicName ?? "",
    ).trim(),
  };
}

/** 操作身份（P1-05）：payload 结构化字段；旧后端缺失时为空串。 */
function identityFields(approval: ApprovalItem): {
  operationId: string;
  sourceTopicApprovalId: string;
} {
  const payload = (approval.payload ?? {}) as Record<string, unknown>;
  return {
    operationId: String(approval.operationId ?? payload.operation_id ?? "").trim(),
    sourceTopicApprovalId: String(
      approval.sourceTopicApprovalId ??
        payload.source_approval_id ??
        payload.source_topic_approval_id ??
        "",
    ).trim(),
  };
}

/** 工具/权限审批项（统一队列与 toolItems 共用一份映射规则）。 */
function toolUnifiedItem(approval: ApprovalItem, deferredKeys: string[]): UnifiedApprovalItem {
  const key = unifiedKeyForTool(approval.approval_id);
  const facts = approvalFacts(approval);
  return {
    key,
    kind: "tool",
    identity: approval.approval_id,
    version: null,
    title: APPROVAL_KIND_TITLES[approval.kind] ?? "需要你确认的操作",
    origin: classifyToolOrigin(approval.kind, approval.payload),
    highRisk: facts?.highRisk === true,
    stale: approval.stale === true,
    deferred: deferredKeys.includes(key),
    approval,
    knowledge: null,
    topic: null,
    ...identityFields(approval),
    invalidated: false,
    invalidatedReason: "",
    invalidatedCode: "",
    stage: null,
    recovery: null,
  };
}

/** approvals 队列里的 topic_* 行 → 话题申请项（只读契约 §2.1 的字段）。 */
function topicUnifiedItem(
  approval: ApprovalItem,
  state: {
    collapsedTopicKeys: Set<string>;
    deferredKeys: string[];
    invalidatedTopics: Record<
      string,
      { reason: string; code: string; operationId: string; at: number }
    >;
  },
): UnifiedApprovalItem {
  const key = unifiedKeyForTopic(approval.approval_id);
  const payload = (approval.payload ?? {}) as Record<string, unknown>;
  const text = (value: unknown): string => (typeof value === "string" ? value.trim() : "");
  const kind: TopicApprovalKind =
    approval.kind === "topic_create" ? "topic_create" : "topic_switch";
  const action: "switch" | "create" =
    kind === "topic_create" || text(payload.action) === "create" ? "create" : "switch";
  /**
   * 失效的事实有两个来源：本地 APPROVAL_RESULT decision=stale（invalidatedTopics）
   * 与后端行的结构化 409（approval.invalidated）。两者都不改 payload、不写拒绝范围。
   */
  const invalidation = state.invalidatedTopics[approval.approval_id] ?? null;
  const invalidated = approval.invalidated === true || Boolean(invalidation);
  const invalidatedReason = approval.invalidatedReason || invalidation?.reason || "";
  const invalidatedCode = approval.invalidatedCode || invalidation?.code || "";
  const topic: TopicApprovalItem = {
    key,
    approvalId: approval.approval_id,
    kind,
    action,
    currentTopicName: text(payload.from_topic_name),
    targetTopicName: text(payload.target_topic_name),
    targetTopicId: text(payload.target_topic_id),
    suggestedName: text(payload.new_name),
    reason: text(payload.reason),
    turnId: text(payload.turn_id) || String(approval.turnId ?? ""),
    operationId: String(payload.operation_id ?? approval.operationId ?? "").trim(),
    paramVersion: payloadNumber(payload.param_version ?? approval.paramVersion),
    baseNavVersion: payloadNumber(payload.base_nav_version ?? approval.baseNavVersion),
    conflict: topicConflictFromPayload(payload),
    invalidated,
    invalidatedReason,
    invalidatedCode,
    approval,
  };
  const live = liveTopicState();
  const stage = stageForTopic(topic, live);
  return {
    key,
    kind: "topic",
    identity: approval.approval_id,
    version: null,
    title: action === "create" ? "新建话题确认" : "切换话题确认",
    origin: classifyToolOrigin(approval.kind, approval.payload),
    highRisk: false,
    stale: approval.stale === true,
    // 已失效的申请不自动重弹：仍有说明与恢复入口，但不再算「待用户当场决定」
    deferred: state.collapsedTopicKeys.has(key) || state.deferredKeys.includes(key) || invalidated,
    approval,
    knowledge: null,
    topic,
    ...identityFields(approval),
    invalidated,
    invalidatedReason,
    invalidatedCode,
    stage,
    recovery: stage === "recoverable_failure" ? live.recovery : null,
  };
}

/** 任意「指向一条话题申请」的入参：key / approval_id / 队列项。 */
export type TopicSelector = TopicApprovalItem | UnifiedApprovalItem | string | null | undefined;

/** 从入参解析统一队列项；找不到返回 null（不拿别的条目凑数）。 */
function selectTopicItem(
  selector: TopicSelector,
  items: UnifiedApprovalItem[],
): UnifiedApprovalItem | null {
  if (!selector) return null;
  if (typeof selector === "string") {
    const raw = selector.trim();
    if (!raw) return null;
    const key = raw.startsWith("topic:") ? raw : unifiedKeyForTopic(raw);
    return (
      items.find((item) => item.key === key) ??
      items.find((item) => item.identity === raw) ??
      null
    );
  }
  const key = String((selector as { key?: string }).key ?? "");
  if (!key) return null;
  return items.find((item) => item.key === key) ?? null;
}

/**
 * 统一队列的唯一拼装实现（getter 共用）。
 *
 * 顺序：approvals 队列按到达顺序（工具/话题混排，保持真实到达顺序）→ 知识到达顺序。
 * topic 项**不**混进 toolItems：两类按钮与提交路径不同，入口计数统一用 count。
 */
function mergeUnifiedItems(
  state: {
    knowledge: KnowledgeApprovalItem[];
    deferredKeys: string[];
    collapsedTopicKeys: Set<string>;
    invalidatedTopics: Record<
      string,
      { reason: string; code: string; operationId: string; at: number }
    >;
  },
  queue: ApprovalItem[],
): UnifiedApprovalItem[] {
  const approvals: UnifiedApprovalItem[] = queue.map((approval) =>
    isTopicApprovalKind(approval.kind)
      ? topicUnifiedItem(approval, state)
      : toolUnifiedItem(approval, state.deferredKeys),
  );
  const knowledge: UnifiedApprovalItem[] = state.knowledge.map((item) => ({
    key: item.key,
    kind: "knowledge" as const,
    identity: item.id,
    version: item.version,
    title: "保存这条知识？",
    origin: (item.source && item.source !== "主对话" ? "background" : "main_agent") as ToolOrigin,
    highRisk: false,
    stale: false,
    deferred: state.deferredKeys.includes(item.key),
    approval: null,
    knowledge: item,
    topic: null,
    operationId: "",
    sourceTopicApprovalId: "",
    invalidated: false,
    invalidatedReason: "",
    invalidatedCode: "",
    stage: null,
    recovery: null,
  }));
  return [...approvals, ...knowledge];
}

/* ------------------------------------------------------------------ */
/* F05：队列标签的「对象身份 + 操作摘要」                                */
/* ------------------------------------------------------------------ */

function squashText(value: unknown): string {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
}

/** 路径末段（文件名）：/a/b/报告.md 与 D:\work\报告.md 都取「报告.md」。 */
function pathTail(path: string): string {
  const parts = String(path ?? "").split(/[\\/]+/).filter((part) => part.trim().length > 0);
  return parts.length ? parts[parts.length - 1] : String(path ?? "").trim();
}

/**
 * 中间省略：保留头尾（对象身份的区别常在尾部，例如「…第 2 版.md」）。
 * 只影响可见文本；title/aria 用完整文本，不用它。
 */
export function middleEllipsis(text: string, limit = 18): string {
  const value = squashText(text);
  if (value.length <= limit) return value;
  if (limit <= 3) return value.slice(0, limit) + "…";
  const head = Math.ceil((limit - 1) / 2);
  const tail = limit - 1 - head;
  return value.slice(0, head) + "…" + (tail > 0 ? value.slice(value.length - tail) : "");
}

/** 从审批载荷里收集真实对象路径（payload / arguments 里的 path/paths/files，最多两层）。 */
function collectObjectPaths(payload: unknown): string[] {
  const out: string[] = [];
  const push = (value: unknown): void => {
    if (typeof value === "string" && value.trim()) out.push(value.trim());
    else if (Array.isArray(value)) for (const item of value) push(item);
  };
  const visit = (node: unknown, depth: number): void => {
    if (!node || typeof node !== "object" || depth > 2) return;
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
      if (["path", "paths", "file", "files", "filename"].includes(key)) push(value);
      else if (depth < 2 && value && typeof value === "object") visit(value, depth + 1);
    }
  };
  visit(payload, 0);
  return [...new Set(out)];
}

/**
 * 这条申请针对的**对象身份**（F05 的关键：先给能区分的东西，再给操作摘要）。
 *
 * * 工具类：对象路径末段（多对象注明数量）+ 版本号（有就带）；没有路径就用工具名；
 *   再没有就用审批 id 尾段兜底（保证同类型多条永远能区分，不靠猜）。
 * * 知识类：内容摘要（没有内容才退回 id）。
 * * 话题类：目标/新话题名（导出的是话题名，不是审批标题）。
 *
 * `short=true` 时做中间省略（给可见 chip 用）；title/aria 用完整文本。
 */
export function unifiedItemIdentity(
  item: UnifiedApprovalItem | null | undefined,
  opts: { short?: boolean } = {},
): string {
  if (!item) return "";
  const shorten = (value: string, limit = 18): string =>
    opts.short ? middleEllipsis(value, limit) : value;
  if (item.kind === "knowledge") {
    const content = squashText(item.knowledge?.content);
    const id = String(item.knowledge?.id ?? "");
    const identity = content || id;
    return shorten(identity, 18);
  }
  if (item.kind === "topic") {
    const topic = item.topic;
    if (!topic) return shorten(item.title, 18);
    const label =
      topic.action === "create"
        ? "新话题：" + (squashText(topic.suggestedName) || "未命名")
        : topic.targetTopicName
          ? "转到：" + squashText(topic.targetTopicName)
          : "切换话题";
    return shorten(label, 24);
  }
  const payload = (item.approval?.payload ?? {}) as Record<string, unknown>;
  const versionRaw = payload.version ?? payload.revision ?? payload.expected_version;
  const version =
    versionRaw === null || versionRaw === undefined || typeof versionRaw === "object"
      ? ""
      : String(versionRaw).trim();
  const paths = collectObjectPaths(payload);
  if (paths.length) {
    const first = pathTail(paths[0]);
    const many = paths.length > 1 ? ` 等 ${paths.length} 个对象` : "";
    const versionText = version ? ` v${version}` : "";
    return shorten(first + versionText + many, 22);
  }
  const batchItems = Array.isArray(payload.items) ? payload.items : [];
  if (batchItems.length) {
    const firstPath = collectObjectPaths({ items: batchItems })[0] ?? "";
    const first = firstPath ? pathTail(firstPath) : "";
    const label = `${batchItems.length} 项` + (first ? ` · ${first}` : "");
    return shorten(label, 22);
  }
  const toolName = squashText(payload.name) || squashText(payload.tool_name);
  if (toolName) return shorten(toolName + (version ? ` v${version}` : ""), 22);
  if (version) return "v" + version;
  const identity = String(item.identity ?? "");
  return identity ? "#" + identity.slice(-8) : "";
}

/** 操作摘要全文（不截断；title/aria 用）。 */
export function unifiedItemSummaryFull(item: UnifiedApprovalItem | null | undefined): string {
  if (!item) return "";
  if (item.kind === "knowledge") {
    const category =
      KNOWLEDGE_CATEGORY_LABELS[String(item.knowledge?.category ?? "")] ?? "知识";
    const content = squashText(item.knowledge?.content);
    return content ? category + "：" + content : category;
  }
  if (item.kind === "topic") {
    const topic = item.topic;
    if (!topic) return item.title || "话题申请";
    const current = topic.currentTopicName ? "当前话题：" + squashText(topic.currentTopicName) : "";
    const reason = squashText(topic.reason);
    return [current, reason].filter((part) => part.length > 0).join(" · ") || item.title;
  }
  const facts = item.approval ? approvalFacts(item.approval) : null;
  const text = squashText(facts?.description || facts?.operationSummary);
  return text || item.title || "需要你确认的操作";
}

/**
 * 可见 chip 的一句话（F05）：**对象身份在前**，操作摘要在后。
 * 这样即使后半段被 CSS 省略号截掉，同类型多条也能在可见部分区分。
 */
export function unifiedItemSummary(item: UnifiedApprovalItem | null | undefined): string {
  if (!item) return "";
  const identity = unifiedItemIdentity(item, { short: true });
  const summary = unifiedItemSummaryFull(item);
  const summaryShort = middleEllipsis(summary, 20);
  if (!identity) return summaryShort;
  if (!summaryShort || identity === summaryShort) return identity;
  return identity + " · " + summaryShort;
}

/** 标签上的类型分组（短标签，不替代摘要）。 */
export function unifiedItemGroupLabel(item: UnifiedApprovalItem | null | undefined): string {
  if (!item) return "";
  if (item.kind === "knowledge") return "知识";
  if (item.kind === "topic") return "话题";
  return "权限";
}

/** chip 的完整可区分文案（title/aria 用，绝不复用被截断的可见文本）。 */
export function unifiedItemFullLabel(item: UnifiedApprovalItem | null | undefined): string {
  if (!item) return "";
  const identity = unifiedItemIdentity(item);
  const summary = unifiedItemSummaryFull(item);
  const parts = [unifiedItemGroupLabel(item)];
  if (identity) parts.push(identity);
  if (summary && summary !== identity) parts.push(summary);
  return parts.join("：");
}

const DEFERRED_STORAGE_KEY = "qio.unifiedApprovals.deferred";
const DEFERRED_MAX = 200;

function loadDeferredKeys(): string[] {
  try {
    const raw = globalThis.localStorage?.getItem(DEFERRED_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => typeof item === "string").slice(-DEFERRED_MAX);
  } catch {
    return [];
  }
}

function persistDeferredKeys(keys: string[]): void {
  try {
    globalThis.localStorage?.setItem(
      DEFERRED_STORAGE_KEY,
      JSON.stringify(keys.slice(-DEFERRED_MAX)),
    );
  } catch {
    // 隐私模式 / 存储不可用：只影响跨重启记忆，不影响本次会话的暂缓语义
  }
}

let knowledgeInflight: Promise<boolean> | null = null;

export const useUnifiedApprovalsStore = defineStore("unifiedApprovals", {
  state: () => ({
    knowledge: [] as KnowledgeApprovalItem[],
    knowledgeStatus: "idle" as LoadStatus,
    knowledgeError: "",
    /** 最近一次从服务端读到待确认列表的时间（毫秒）；null = 还没成功读过 */
    knowledgeLoadedAt: null as number | null,
    visible: false,
    /**
     * 「结果与历史」视图是否打开（F02）。
     *
     * 与 visible 分开：待审批数为 0 时也要能打开结果；两个视图共用一个弹窗，
     * 同一时刻只显示其中一个（不是第二套常驻入口）。
     */
    resultsVisible: false,
    activeKey: null as string | null,
    /** 用户「稍后处理 / 关闭」过的申请：同一项不再自动弹 */
    deferredKeys: loadDeferredKeys(),
    /**
     * 用户收起过的话题申请 key。
     *
     * 收起 = 只记「先不处理」，不调用 decide、不消费申请；同一申请不自动重开
     * （用户从入口主动打开、或真有一条新申请到达时除外）。
     */
    collapsedTopicKeys: new Set<string>(),
    /** 话题申请里用户改过的新话题名：收起 / 失败后保留，不静默丢弃。 */
    topicDrafts: {} as Record<string, string>,
    /** 本会话已经自动弹出过的申请：不因轮询/重渲染反复弹 */
    autoShownKeys: [] as string[],
    /** 自动弹出被「别的弹窗 / 正在输入」挡住：条件恢复后可再试 */
    autoPending: false,
    /** 正在提交决定的申请 key（防重复提交；一次只提交一条）。 */
    submittingKeys: [] as string[],
    submitError: "",
    submitErrorCode: "",
    /** 处理完一条后的一句话结果（成功/已处理），入口与弹窗都可展示 */
    lastNotice: "",
    editingKey: null as string | null,
    drafts: {} as Record<string, string>,
    /**
     * 处理完一项后的「队列列表」态：回到列表，由用户选下一条；
     * 不自动进入下一条详情（用户任务书：处理完一项回列表保位置）。
     */
    queueListOnly: false,
    /**
     * 用户收起弹窗时的到达序号基线。
     *
     * 收起只表示「稍后处理」：已经存在的申请不再自动弹；只有真正新到达
     * （arrivalSeq 更大）的申请才按规则自动弹出。
     */
    autoBlockedSeq: 0,
    /**
     * 后端结构化判定失效的话题申请（approval_id → 原因）。
     *
     * 失效 ≠ 拒绝：不写拒绝范围、不自动重弹、不覆盖当前选择；申请仍留在队列里
     * 说明原因并保留恢复入口，由用户主动收口。
     */
    invalidatedTopics: {} as Record<
      string,
      { reason: string; code: string; operationId: string; at: number }
    >,
    /** 已由用户主动收口的失效申请（有界；只影响展示，不通知后端）。 */
    acknowledgedInvalidations: [] as string[],
    /** 恢复重试单飞：正在重试的 record_id（空串 = 空闲）。 */
    retryingAnswerRecord: "",
  }),
  getters: {
    /** 统一队列：工具/权限类在前（更紧急），知识保存按到达顺序在后。 */
    items(state): UnifiedApprovalItem[] {
      const approvals = useApprovalsStore();
      return mergeUnifiedItems(state, approvals.queue);
    },
    toolItems(state): UnifiedApprovalItem[] {
      const approvals = useApprovalsStore();
      return mergeUnifiedItems(state, approvals.queue).filter((item) => item.kind === "tool");
    },
    knowledgeItems(state): UnifiedApprovalItem[] {
      return mergeUnifiedItems(state, []).filter((item) => item.kind === "knowledge");
    },
    /**
     * 当前 pending 的话题申请（D/C 的待处理入口与 topicPendingForTurn 共用同一份映射）。
     *
     * 每项至少给出 key / approvalId / turnId 与契约 §2.1 允许展示的字段；
     * topic 项**不**混进 toolItems（两类按钮与提交路径不同）。
     */
    topicItems(state): TopicApprovalItem[] {
      const approvals = useApprovalsStore();
      return mergeUnifiedItems(state, approvals.queue)
        .map((item) => item.topic)
        .filter((topic): topic is TopicApprovalItem => topic !== null);
    },
    /**
     * 某一轮是否有等待用户选择的话题申请（返回该项，没有则 null）。
     *
     * 只读：D 的等待文案 / 发送限制 / 草稿跟随都从这里派生，不另建第二份状态。
     */
    topicPendingForTurn(state): (turnId: string | null | undefined) => TopicApprovalItem | null {
      const approvals = useApprovalsStore();
      const topics = mergeUnifiedItems(state, approvals.queue)
        .map((item) => item.topic)
        .filter((topic): topic is TopicApprovalItem => topic !== null);
      return (turnId: string | null | undefined): TopicApprovalItem | null => {
        const id = typeof turnId === "string" ? turnId.trim() : "";
        if (!id) return null;
        /**
         * 已失效的申请不再是「等待用户当场决定」：发送限制 / 等待文案随之解除，
         * 但申请本身仍留在队列里（原因与恢复入口不消失）。
         */
        return (
          topics.find((topic) => topic.turnId === id && !topic.invalidated) ?? null
        );
      };
    },
    /**
     * 某条话题申请当前的阶段（按该条 operationId + turnId 匹配本轮 TOPIC_PHASE）。
     * 旧后端没有阶段事件 / 对不上 → null（D 不渲染阶段行，不伪造）。
     */
    topicStageFor(state): (selector: TopicSelector) => TopicPhase | null {
      const items = mergeUnifiedItems(state, useApprovalsStore().queue);
      return (selector: TopicSelector): TopicPhase | null =>
        selectTopicItem(selector, items)?.stage ?? null;
    },
    /** 某条话题申请上下文的恢复事实（只有 recoverable_failure 时非 null）。 */
    topicRecoveryFor(state): (selector: TopicSelector) => TopicAnswerRecovery | null {
      const items = mergeUnifiedItems(state, useApprovalsStore().queue);
      return (selector: TopicSelector): TopicAnswerRecovery | null =>
        selectTopicItem(selector, items)?.recovery ?? null;
    },
    /**
     * 某条话题申请的原确认按钮是否应禁用（失效 / 已处理 / 到期 / 正在提交）。
     * 只读派生：D 不再在组件里另写一套规则。
     */
    isTopicDecisionDisabled(state): (selector: TopicSelector) => boolean {
      const items = mergeUnifiedItems(state, useApprovalsStore().queue);
      return (selector: TopicSelector): boolean => {
        const item = selectTopicItem(selector, items);
        if (!item || item.kind !== "topic") return false;
        if (item.invalidated || item.stale || item.approval?.stale) return true;
        if (isExpiredApproval(item.approval)) return true;
        return state.submittingKeys.includes(item.key);
      };
    },
    /** 正在提交的申请 key（老字段名保留：一次只提交一条）。 */
    submittingKey(state): string | null {
      return state.submittingKeys[0] ?? null;
    },
    count(state): number {
      return useApprovalsStore().queue.length + state.knowledge.length;
    },
    toolCount(): number {
      return useApprovalsStore().queue.length;
    },
    knowledgeCount(state): number {
      return state.knowledge.length;
    },
    activeItem(state): UnifiedApprovalItem | null {
      const items = mergeUnifiedItems(state, useApprovalsStore().queue);
      if (!items.length) return null;
      if (state.activeKey) return items.find((item) => item.key === state.activeKey) ?? null;
      // 列表态没有「当前项」：弹窗显示队列列表，不自动进入某一条详情
      return state.queueListOnly ? null : (items[0] ?? null);
    },
    activeIndex(state): number {
      const items = mergeUnifiedItems(state, useApprovalsStore().queue);
      if (!items.length) return -1;
      const active = state.activeKey
        ? items.find((item) => item.key === state.activeKey)
        : state.queueListOnly
          ? null
          : items[0];
      if (!active) return -1;
      return items.findIndex((item) => item.key === active.key);
    },
    knowledgeLoading(state): boolean {
      return state.knowledgeStatus === "loading";
    },
  },
  actions: {
    /** 从服务端读「待确认」里的知识保存申请（权威快照；失败如实保留错误，不当作 0）。 */
    async refreshKnowledgePending(): Promise<boolean> {
      if (knowledgeInflight) return knowledgeInflight;
      this.knowledgeStatus = "loading";
      knowledgeInflight = (async () => {
        try {
          const listing = await request<MemoryPendingListing>(
            "/api/memory/pending?kind=knowledge_proposal&limit=500",
          );
          const views = Array.isArray(listing.items) ? listing.items : [];
          const previous = new Map(this.knowledge.map((item) => [item.id, item]));
          const next: KnowledgeApprovalItem[] = [];
          const seen = new Set<string>();
          for (const view of views) {
            const detail = knowledgeDetailFromView(view);
            if (!detail || seen.has(detail.id)) continue;
            seen.add(detail.id);
            const prior = previous.get(detail.id);
            next.push({
              ...detail,
              key: unifiedKeyForKnowledge(detail.id),
              reason: prior?.reason ?? "",
              arrivedAt: prior?.arrivedAt ?? nextKnowledgeArrival(),
              detailStatus: "ready",
              detailError: "",
            });
          }
          this.knowledge = next;
          this.knowledgeStatus = "ready";
          this.knowledgeError = "";
          this.knowledgeLoadedAt = Date.now();
          this.noteQueueChanged();
          return true;
        } catch (error) {
          this.knowledgeStatus = "error";
          this.knowledgeError =
            "没能读到待确认的知识列表：" +
            ((error as Error)?.message || "未知原因") +
            "（已有条目保留；数量可能不是最新）";
          return false;
        } finally {
          knowledgeInflight = null;
        }
      })();
      return knowledgeInflight;
    },

    /**
     * 实时候选事件（KNOWLEDGE_CANDIDATE）：先入队（数量立刻更新），再拉权威详情。
     * 主对话提出的知识保存**从不自动弹窗**，只更新入口数量。
     */
    ingestKnowledgeCandidate(candidate: {
      knowledgeId: string;
      category: string;
      content: string;
      reason?: string;
    }): void {
      if (!candidate.knowledgeId) return;
      const key = unifiedKeyForKnowledge(candidate.knowledgeId);
      const existing = this.knowledge.find((item) => item.id === candidate.knowledgeId);
      if (existing) {
        if (candidate.reason) existing.reason = candidate.reason;
        if (candidate.content && !existing.content) existing.content = candidate.content;
      } else {
        this.knowledge.push({
          id: candidate.knowledgeId,
          key,
          version: null,
          content: candidate.content,
          category: candidate.category,
          scope: [],
          epistemicStatus: null,
          source: "主对话",
          evidenceIds: [],
          updatedAt: null,
          reason: candidate.reason ?? "",
          arrivedAt: nextKnowledgeArrival(),
          detailStatus: "loading",
          detailError: "",
        });
      }
      void this.refreshKnowledgePending();
    },

    /** 审批队列变化（响应外部 resolve/reconcile）：当前项没了就切到下一条。 */
    noteQueueChanged(): void {
      const items = this.items as UnifiedApprovalItem[];
      if (!items.length) {
        this.visible = false;
        this.activeKey = null;
        this.editingKey = null;
        this.queueListOnly = false;
        // 注意：不清 submitError —— 队列被权威刷新清空时，失败原因仍要能说清楚
        return;
      }
      if (this.queueListOnly) {
        // 列表态：队列怎么变都保持列表，不自动选中一条详情
        if (this.activeKey && !items.some((item) => item.key === this.activeKey)) {
          this.activeKey = null;
        }
        return;
      }
      if (!this.activeKey || !items.some((item) => item.key === this.activeKey)) {
        this.activeKey = items[0]?.key ?? null;
        this.editingKey = null;
      }
    },

    /**
     * 自动弹出规则（工具/权限类与真正新到达的话题申请；知识保存从不自动）：
     * - 主智能体新增权限 / 后台工作 / 子智能体 / 删除覆盖类重点确认 → 自动；
     * - 话题申请：新到达时自动展开；**用户收起过的同一申请不自动重开**；
     * - 已暂缓的同一项不重复弹；本会话自动弹过的不重复弹；
     * - 已经打开 / 用户正在输入 / 别的弹窗开着 → 不叠加（保留申请，条件恢复再试）；
     * - 内联审批正在显示这一条 → 不自动弹（同一时刻只有一套按钮；只影响工具类）。
     */
    maybeAutoOpen(): boolean {
      if (this.visible || this.resultsVisible) return false;
      const approvals = useApprovalsStore();
      /**
       * 注意：这里**不能**用 approvals.deferred 早退。
       *
       * 那个字段同时被「用户稍后处理」与「恢复路径/编辑中到达」复用；一旦早退，
       * 输入中到达的审批在失焦后会永远不弹，autoPending 也不会被置位（真实故障）。
       * 用户主动收起由 unified.deferredKeys + autoBlockedSeq 表达；恢复路径由
       * ApprovalItem.restored 表达，两者都不影响「真正新到达的申请」。
       */
      const items = this.items as UnifiedApprovalItem[];
      const candidate = items.find(
        (item) =>
          (item.kind === "tool" || item.kind === "topic") &&
          !item.deferred &&
          // 已失效的申请不自动重弹：保留说明与恢复入口，由用户主动收口
          !item.invalidated &&
          // 话题申请收起过就不再自动重开（入口主动打开不受此限）
          (item.kind !== "topic" || !this.collapsedTopicKeys.has(item.key)) &&
          !this.autoShownKeys.includes(item.key) &&
          !item.stale &&
          !isExpiredApproval(item.approval) &&
          // 恢复路径补读出来的旧审批不抢焦点：亮入口，等用户主动打开
          item.approval?.restored !== true &&
          // 收起弹窗之前就已经存在的申请：不因一次收起/重渲染又弹回来；
          // 真正新到达（arrivalSeq 更大）的申请仍按规则自动弹。
          (item.approval?.arrivedSeq ?? 0) > this.autoBlockedSeq,
      );
      if (!candidate) return false;
      // 内联审批只属于工具类；话题申请不参与内联认领，也不因它挡下
      if (candidate.kind === "tool" && approvals.inlineClaimed) {
        this.autoPending = true;
        return false;
      }
      /**
       * 自动弹出门控（集中判断，不散落）：
       * 输入中 / 输入法组合输入 / 刚离开输入框（延迟） / 有中央弹窗 / 记忆星球覆盖层 /
       * 设置与调试页 / 窗口后台 —— 任一成立都不弹，条件恢复后再试。
       */
      const gate = canAutoOpenApproval();
      if (!gate.ok) {
        this.autoPending = gate.retry;
        return false;
      }
      this.autoShownKeys = [...this.autoShownKeys.slice(-99), candidate.key];
      this.autoPending = false;
      this.open(candidate.key);
      return true;
    },

    /** 用户主动打开（入口点击 / 桌面通知等）：清除全局「稍后」并显示。 */
    open(key?: string | null): boolean {
      const items = this.items as UnifiedApprovalItem[];
      if (!items.length) return false;
      const requested = key ? items.find((item) => item.key === key) ?? null : null;
      /**
       * 明确给了 key 却不在队列里：不拿别的条目凑数。不同 kind 的 key 前缀不同，
       * 拿错条目会把「查看工具审批」打开成话题申请（或反过来）。
       */
      if (key && !requested) return false;
      const target = requested ?? this.activeItem ?? items[0];
      if (!target) return false;
      const approvals = useApprovalsStore();
      if (target.kind === "tool") {
        // 内联审批已经显示这条的按钮：同一时刻只能有一套按钮，不叠加。
        // 用户可在过程区内联卡上点「查看完整信息」把按钮交回统一弹窗。
        if (approvals.inlineClaimed) {
          this.lastNotice = "这项确认的按钮正在对话过程中显示；可以在那里查看完整信息。";
          return false;
        }
        approvals.openNow();
      }
      this.activeKey = target.key;
      this.queueListOnly = false;
      this.visible = true;
      this.resultsVisible = false;
      this.submitError = "";
      this.submitErrorCode = "";
      return true;
    },

    /**
     * 打开「审批结果与历史」（F02）：**不要求**有待审批项。
     *
     * 结果来自后端 /api/approvals/history + 实时 APPROVAL_RESULT；
     * 打开时刷新一次历史（失败会在面板里显示原因与重试，不当作空历史）。
     */
    openResults(): boolean {
      const results = useApprovalResultsStore();
      void results.loadHistory();
      this.visible = false;
      this.resultsVisible = true;
      this.submitError = "";
      this.submitErrorCode = "";
      return true;
    },

    /**
     * 回到待审批队列（没有待办时返回 false，不显示空队列）。
     * 有正在看的项就保持它；否则显示队列列表，由用户选下一条。
     */
    showQueue(): boolean {
      this.resultsVisible = false;
      const items = this.items as UnifiedApprovalItem[];
      if (!items.length) {
        this.visible = false;
        this.queueListOnly = false;
        return false;
      }
      const keep = this.activeKey && items.some((item) => item.key === this.activeKey);
      this.activeKey = keep ? this.activeKey : null;
      this.queueListOnly = !keep;
      this.visible = true;
      return true;
    },

    /** 关闭整个面板（队列与结果都收起；不批准也不拒绝）。 */
    closePanel(): void {
      if (this.resultsVisible && !this.visible) {
        this.resultsVisible = false;
        return;
      }
      this.close();
    },

    /**
     * 关闭：只隐藏，不批准也不拒绝；当前项记为「稍后」，不因轮询反复自动弹。
     *
     * 同时记下到达序号基线：收起之后**已存在的**申请不再自动弹（不得立刻连弹下一条）；
     * 真正新到达的申请仍按规则处理。
     */
    close(): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (active) {
        this.deferKey(active.key);
        // 话题申请：收起只记「已收起」，**不调用 decide**、不消费申请
        if (active.kind === "topic") this.collapsedTopicKeys.add(active.key);
      }
      this.autoBlockedSeq = useApprovalsStore().arrivalSeq;
      this.visible = false;
      this.resultsVisible = false;
      this.activeKey = null;
      this.queueListOnly = false;
      this.editingKey = null;
      this.autoPending = false;
      this.submitError = "";
      this.submitErrorCode = "";
    },

    /** 「稍后处理」当前项：隐藏这一项；队列里还有别的就展示下一条。 */
    deferActive(): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (!active) {
        this.close();
        return;
      }
      this.deferKey(active.key);
      if (active.kind === "topic") {
        // 话题申请「稍后处理」= 收起这一项；不提交任何决定
        this.collapsedTopicKeys.add(active.key);
      }
      if (active.kind === "tool") {
        // 与既有 store 语义一致：收起窗口但保留待审批项
        useApprovalsStore().defer();
      }
      const items = this.items as UnifiedApprovalItem[];
      if (!items.length) {
        this.visible = false;
        this.activeKey = null;
      } else {
        this.activeKey = items[0]?.key ?? null;
        this.visible = true;
      }
      this.editingKey = null;
      this.submitError = "";
      this.submitErrorCode = "";
      this.queueListOnly = false;
    },

    deferKey(key: string): void {
      if (!key || this.deferredKeys.includes(key)) return;
      this.deferredKeys = [...this.deferredKeys, key].slice(-DEFERRED_MAX);
      persistDeferredKeys(this.deferredKeys);
    },

    /** 用户主动切换到队列中的另一项（不改变任何申请状态）。 */
    switchTo(key: string): boolean {
      const items = this.items as UnifiedApprovalItem[];
      const target = items.find((item) => item.key === key);
      if (!target) return false;
      if (target.kind === "tool") {
        const approvals = useApprovalsStore();
        if (approvals.inlineClaimed && approvals.inlineId !== target.identity) {
          this.lastNotice = "另一项确认的按钮正在对话过程中显示；先在那里处理完再切换。";
          return false;
        }
        approvals.showFull(target.identity);
      }
      this.activeKey = key;
      this.queueListOnly = false;
      this.visible = true;
      this.submitError = "";
      this.submitErrorCode = "";
      this.editingKey = null;
      return true;
    },

    /**
     * 进入编辑：**保留已有草稿**（切换队列项再切回来不能丢失已改内容）。
     * 只有明确「取消修改」才丢弃草稿。
     */
    startEdit(): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (!active || active.kind !== "knowledge") return;
      if (this.drafts[active.key] === undefined) {
        this.drafts[active.key] = active.knowledge?.content ?? "";
      }
      this.editingKey = active.key;
    },

    /** 取消修改：明确丢弃这份草稿（不是「切换项」的默认行为）。 */
    stopEdit(): void {
      const key = this.editingKey;
      if (key && Object.prototype.hasOwnProperty.call(this.drafts, key)) {
        delete this.drafts[key];
      }
      this.editingKey = null;
    },

    updateDraft(text: string): void {
      const key = this.editingKey;
      if (!key) return;
      this.drafts[key] = text;
    },

    draftFor(key: string | null): string {
      if (!key) return "";
      return this.drafts[key] ?? "";
    },

    /** 「知道了」：后端已经不存在这条审批（stale）时唯一的出口。 */
    dismissStale(): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (!active || active.kind !== "tool" || !active.approval) return;
      useApprovalsStore().resolve(active.identity);
      this.noteQueueChanged();
    },

    /* ── 话题申请（统一弹窗内，不另起常驻面板）────────────────────────── */

    /**
     * 用户从入口主动打开一条话题申请（D 的 ComposerEntries 入口调用）。
     *
     * 只看 topic 项；已收起的同一申请也能被主动打开（收起只禁止**自动**重开）。
     * 不提交任何决定。
     */
    openTopicApproval(key?: string | null): boolean {
      const items = this.items as UnifiedApprovalItem[];
      const topics = items.filter((item) => item.kind === "topic");
      const requested = key ? topics.find((item) => item.key === key) ?? null : null;
      if (key && !requested) return false;
      const target = requested ?? topics.find((item) => !item.deferred) ?? topics[0] ?? null;
      if (!target) return false;
      this.activeKey = target.key;
      this.queueListOnly = false;
      this.visible = true;
      this.resultsVisible = false;
      this.submitError = "";
      this.submitErrorCode = "";
      // 用户已经明确打开过：之后不因轮询 / 重渲染再自动弹这一项
      if (!this.autoShownKeys.includes(target.key)) {
        this.autoShownKeys = [...this.autoShownKeys.slice(-99), target.key];
      }
      return true;
    },

    /** 收起一条话题申请：只记「先不处理」，不调用 decide、不消费申请。 */
    collapseTopic(key?: string | null): void {
      const active = this.activeItem as UnifiedApprovalItem | null;
      const items = this.items as UnifiedApprovalItem[];
      const target = key ? items.find((item) => item.key === key) ?? null : active;
      if (target && target.kind === "topic") {
        this.collapsedTopicKeys.add(target.key);
        this.deferKey(target.key);
      }
      this.autoBlockedSeq = useApprovalsStore().arrivalSeq;
      this.visible = false;
      this.resultsVisible = false;
      this.activeKey = null;
      this.queueListOnly = false;
      this.editingKey = null;
      this.autoPending = false;
      this.submitError = "";
      this.submitErrorCode = "";
    },

    /** 收起一条已经有结局的话题申请（stale：后端已无 waiter / 已过期）。 */
    dismissStaleTopic(itemOrKey?: TopicApprovalItem | UnifiedApprovalItem | string | null): void {
      const key = typeof itemOrKey === "string" ? itemOrKey : itemOrKey?.key ?? null;
      const items = this.items as UnifiedApprovalItem[];
      const active = this.activeItem as UnifiedApprovalItem | null;
      const target = key ? items.find((item) => item.key === key) ?? null : active;
      if (!target || target.kind !== "topic" || !target.approval) return;
      useApprovalsStore().resolve(target.identity);
      this.collapsedTopicKeys.delete(target.key);
      this.noteQueueChanged();
    },

    /** 这条话题申请里用户当前编辑的新名称（create）；未编辑时用模型建议名。 */
    topicDraftFor(topic: TopicApprovalItem | null | undefined): string {
      if (!topic) return "";
      const stored = this.topicDrafts[topic.key];
      return typeof stored === "string" ? stored : topic.suggestedName;
    },

    /** 记录用户改过的新话题名（收起 / 失败后保留，不静默丢弃）。 */
    updateTopicDraft(key: string, name: string): void {
      if (!key) return;
      this.topicDrafts[key] = String(name ?? "");
    },

    /**
     * 话题申请的唯一决定入口（与工具审批同一个 /respond 路径；不新增平行提交）。
     *
     * approve = 转到并回答（switch）/ 新建并回答（create，可带 edits.name）；
     * reject  = 在当前话题回答（scope.reason="stay_current"）。
     * 空名称在本地阻断；过期本地阻断批准；失败保留申请并就地给出原因，不重复提交。
     */
    async decideTopic(
      itemOrKey: TopicApprovalItem | UnifiedApprovalItem | string,
      options: { decision: "approve" | "reject"; edits?: { name?: string } },
    ): Promise<boolean> {
      const key = typeof itemOrKey === "string" ? itemOrKey : itemOrKey.key;
      // 双提交防护：任何一项正在提交都不再发第二次（按钮同时 disabled）
      if (this.submittingKeys.length > 0) return false;
      const items = this.items as UnifiedApprovalItem[];
      const active = items.find((item) => item.key === key) ?? null;
      const topic = active?.topic ?? null;
      if (!active || !topic) {
        /**
         * 幂等：同一申请已经提交成功（双击 / 重放 / 刷新后重复回调）时返回既有结果，
         * 不再发第二次请求，也不把「队列里已经没有它」说成失败。
         */
        const approvalId = key.startsWith("topic:") ? key.slice("topic:".length) : key;
        const receipt = useApprovalsStore().topicReceiptFor(approvalId);
        if (receipt?.ok) {
          this.lastNotice = "这次话题选择此前已经提交成功；没有重复提交。";
          return true;
        }
        return false;
      }
      if (active.invalidated) {
        // 失效 ≠ 拒绝：原确认不可执行；原因与恢复入口保留，不提交任何决定
        this.submitError =
          active.invalidatedReason ||
          "这项话题申请已经失效（本轮绑定的话题已变化或申请不再等待处理），不会再执行；可以在当前话题继续提问。";
        this.submitErrorCode = "invalidated";
        return false;
      }
      if (active.stale || active.approval?.stale) {
        this.submitError =
          "这项话题申请已经有结局（可能已在别处处理或已过期），不会再执行；没有提交任何决定。";
        this.submitErrorCode = "stale";
        return false;
      }
      const decision = options.decision;
      // 到期禁止批准（拒绝 / 在当前话题回答仍可提交：那是「不执行」）
      if (decision === "approve" && isExpiredApproval(active.approval)) {
        this.submitError =
          "这项话题申请已经到期，不能再批准（期限来自后端，不会被放宽）。可以选「在当前话题回答」。";
        this.submitErrorCode = "expired";
        return false;
      }
      let edits: { name: string } | null = null;
      if (decision === "approve" && topic.action === "create") {
        const name = String(options.edits?.name ?? this.topicDraftFor(topic)).trim();
        if (!name) {
          this.submitError = "话题名不能为空；请填写新话题名后再提交。";
          this.submitErrorCode = "topic_name_required";
          return false;
        }
        edits = { name };
        // 用户改过的名字记下来：失败后重开 / 收起都不丢
        this.updateTopicDraft(active.key, name);
      }
      this.submittingKeys = [...this.submittingKeys, active.key];
      this.submitError = "";
      this.submitErrorCode = "";
      try {
        const outcome = await useApprovalsStore().respondTopicById(
          topic.approvalId,
          decision === "approve" ? "approved" : "rejected",
          {
            ...(edits ? { edits } : {}),
            ...(decision === "reject" ? { scope: { reason: "stay_current" } } : {}),
          },
        );
        if (!outcome.ok) {
          this.submitError = outcome.message;
          this.submitErrorCode = outcome.code;
          if (outcome.terminal) {
            this.lastNotice = outcome.message;
            /**
             * 后端结构化失效（409 topic_application_stale）：标记这条申请，
             * 保留原因与恢复入口、禁用原确认、不自动重弹；不写拒绝范围。
             */
            if (outcome.code === "topic_application_stale") {
              this.markTopicInvalidated(topic.approvalId, {
                code: outcome.code,
                reason: outcome.message,
                operationId: topic.operationId,
              });
            }
          }
          return false;
        }
        // 成功前不得显示「已切换 / 已创建」：只如实说已提交、等执行结果
        this.lastNotice =
          decision === "reject"
            ? "已选择在当前话题回答；本轮会留在当前话题。"
            : "已提交你的话题选择；执行结果会显示在对话里。";
        this.finishHandled(active.key);
        return true;
      } finally {
        this.submittingKeys = this.submittingKeys.filter((entry) => entry !== active.key);
      }
    },

    /* ── 失效收口 / 恢复入口（契约 §5 / §8；失效 ≠ 拒绝）────────────────── */

    /**
     * 标记一条话题申请已失效（后端结构化判定；唯一登记处）。
     *
     * 行为：保留队列项与真实原因、禁用原确认、不自动重弹、不写拒绝范围、
     * 不覆盖当前选择；只按 approval_id 处理这一条，不动别的申请。
     */
    markTopicInvalidated(
      approvalIdOrKey: string,
      input: { code?: string; reason?: string; operationId?: string } = {},
    ): void {
      const raw = String(approvalIdOrKey ?? "").trim();
      if (!raw) return;
      const approvalId = raw.startsWith("topic:") ? raw.slice("topic:".length) : raw;
      const previous = this.invalidatedTopics[approvalId];
      const reason =
        String(input.reason ?? "").trim() ||
        previous?.reason ||
        "这条话题申请已经失效（本轮绑定的话题已变化或申请不再等待处理），不会再执行。";
      const code = String(input.code ?? previous?.code ?? "topic_application_stale");
      const operationId = String(input.operationId ?? previous?.operationId ?? "");
      this.invalidatedTopics = {
        ...this.invalidatedTopics,
        [approvalId]: { reason, code, operationId, at: Date.now() },
      };
      // 后端权威失效：同步到审批行（原因可见、按钮禁用），不删除这一条
      const approvals = useApprovalsStore();
      const item = approvals.queue.find((entry) => entry.approval_id === approvalId);
      if (item) {
        item.invalidated = true;
        item.invalidatedReason = reason;
        item.invalidatedCode = code;
      }
      const key = unifiedKeyForTopic(approvalId);
      this.deferKey(key);
      this.collapsedTopicKeys.add(key);
      this.autoBlockedSeq = Math.max(this.autoBlockedSeq, approvals.arrivalSeq);
      this.noteQueueChanged();
    },

    /**
     * 用户主动收口一条失效申请：**纯本地**动作。
     *
     * 不发 /respond、不写拒绝范围、不提交任何决定；改过的名字（topicDrafts）保留，
     * 只把这一条从待办里移走并记下已收口。
     */
    acknowledgeInvalidatedTopic(selector: TopicSelector): void {
      const items = this.items as UnifiedApprovalItem[];
      const item = selectTopicItem(selector, items);
      if (!item || item.kind !== "topic") return;
      const approvalId = item.identity;
      useApprovalsStore().resolve(approvalId);
      this.collapsedTopicKeys.delete(item.key);
      if (!this.acknowledgedInvalidations.includes(approvalId)) {
        this.acknowledgedInvalidations = [
          ...this.acknowledgedInvalidations.slice(-49),
          approvalId,
        ];
      }
      // 注意：topicDrafts[item.key] 故意保留（用户改过的名字不因失效丢失）
      this.noteQueueChanged();
    },

    /**
     * 恢复入口（上下文准备失败后重试回答）：只重做未完成步骤。
     * 成功给可见结果；失败原因如实写进 submitError（不假装成功、不重复提交）。
     */
    async retryTopicAnswer(recordId: string): Promise<boolean> {
      const id = String(recordId ?? "").trim();
      if (!id) return false;
      // 单飞：同一次恢复的重复点击不重复发请求
      if (this.retryingAnswerRecord) return false;
      this.retryingAnswerRecord = id;
      this.submitError = "";
      this.submitErrorCode = "";
      try {
        const session = this.sessionStore();
        const result = session
          ? await session.retryTopicAnswer(id)
          : { ok: false, code: "no_session", message: "会话状态尚未准备好，没有提交重试。" };
        if (result.ok) {
          this.lastNotice = "已请求重新回答；不会重复发送你的消息，也不会重新执行已成功的话题操作。";
          return true;
        }
        this.submitError = result.message || "没能重新开始回答（原因未知）；没有重发任何消息。";
        this.submitErrorCode = result.code || "retry_failed";
        return false;
      } finally {
        this.retryingAnswerRecord = "";
      }
    },

    async retryKnowledgeLoad(): Promise<boolean> {
      return this.refreshKnowledgePending();
    },

    /**
     * 提交当前申请。decision 只接受本 store 认识的四种动作；
     * 工具类 → approvals.respondById（approval_id）；知识类 → 知识原子接口（knowledge id）。
     * 返回是否成功（失败会保留申请并把可读原因写进 submitError）。
     */
    async submit(
      decision: "approve" | "approve_modified" | "ignore" | "reject",
      options: { overrides?: Record<string, unknown> } = {},
    ): Promise<boolean> {
      const active = this.activeItem as UnifiedApprovalItem | null;
      if (!active) return false;
      // 话题申请有专门的 decideTopic；这里不接受它，避免两条提交路径串线
      if (active.kind === "topic") return false;
      if (this.submittingKeys.length > 0) return false;
      if (active.kind === "tool") {
        if (decision !== "approve" && decision !== "reject") return false;
        if (active.stale) return false;
        /**
         * 到期禁止批准：真实期限来自后端，到期后服务端也会拒绝；这里先给出可理解原因，
         * 不发请求、不放宽期限、不假装已处理。
         */
        if (decision === "approve" && isExpiredApproval(active.approval)) {
          this.submitError =
            "这项申请已经到期，不能再批准（期限不会放宽）。需要的话请让后端重新发起这次申请。";
          this.submitErrorCode = "expired";
          return false;
        }
        this.submittingKeys = [...this.submittingKeys, active.key];
        this.submitError = "";
        this.submitErrorCode = "";
        try {
          const approvals = useApprovalsStore();
          await approvals.respondById(
            active.identity,
            decision === "approve" ? "approved" : "rejected",
            options.overrides,
          );
          /**
           * R9：失败与否只看 store 的 error —— **不能**用「还在队列里」当失败判据。
           * 内部运行数据按次确认（1/3 → 2/3 → 3/3），前两次成功后该项按设计仍留在队列，
           * 那是「进度前进了」而不是「没提交成功」。
           */
          const failure = approvals.error;
          if (failure) {
            this.submitError = failure;
            this.submitErrorCode = "tool_failed";
            return false;
          }
          const still = approvals.queue.find((item) => item.approval_id === active.identity);
          if (still) {
            const required = Math.max(1, Number(still.requiresConfirmations ?? 1));
            const done = Math.max(0, Number(still.confirmedStage ?? 0));
            this.lastNotice =
              required > 1
                ? "已确认 " + done + "/" + required + " 次（确认阶段以后端为准）。"
                : "提交已送出，正在等待后端结果。";
            return true;
          }
          this.lastNotice = decision === "approve" ? "已确认这一次操作。" : "已拒绝这一次操作。";
          this.finishHandled(active.key);
          return true;
        } finally {
          this.submittingKeys = this.submittingKeys.filter((entry) => entry !== active.key);
        }
      }
      // 知识保存申请：只走知识自己的原子接口
      const knowledge = active.knowledge;
      if (!knowledge) return false;
      const content = decision === "approve_modified" ? this.draftFor(active.key).trim() : undefined;
      if (decision === "approve_modified" && !content) {
        this.submitError = "修改后的内容不能为空；请先填写要保存的内容。";
        this.submitErrorCode = "empty_content";
        return false;
      }
      this.submittingKeys = [...this.submittingKeys, active.key];
      this.submitError = "";
      this.submitErrorCode = "";
      try {
        const result = await submitKnowledgeApprovalRequest(knowledge.id, decision, {
          content,
          expectedVersion: knowledge.version,
          expectedContent: knowledge.content || null,
          expectedUpdatedAt: knowledge.updatedAt,
        });
        const messages: Record<string, string> = {
          approve: "已保存你确认的这条知识。",
          approve_modified: "已按你修改后的内容保存。",
          ignore: "这条知识没有保存，之后不会再询问同一类内容。",
          reject: "已把这条申请打回草稿。",
        };
        this.lastNotice = result?.message || messages[decision] || "已处理。";
        this.removeKnowledge(knowledge.id);
        this.finishHandled(active.key);
        return true;
      } catch (error) {
        const classified = classifyKnowledgeSubmitError(error);
        this.submitError = classified.message;
        this.submitErrorCode = classified.code;
        if (classified.terminal) {
          // 申请已经有结局：收口（不假装成功，也不留一个点不动的按钮）；
          // 结局原因留在入口提示里，不让用户以为「点了一下什么都没发生」。
          this.lastNotice = classified.message;
          this.removeKnowledge(knowledge.id);
          this.finishHandled(active.key);
        }
        if (classified.refresh) {
          void this.refreshKnowledgePending().then(() => {
            if (classified.code === "timeout" && !this.knowledge.some((item) => item.id === knowledge.id)) {
              this.lastNotice = "已重新核对：这条已不在待确认列表里（可能已经保存），没有重复提交。";
            }
          });
        }
        return false;
      } finally {
        this.submittingKeys = this.submittingKeys.filter((entry) => entry !== active.key);
      }
    },

    removeKnowledge(knowledgeId: string): void {
      this.knowledge = this.knowledge.filter((item) => item.id !== knowledgeId);
      const session = this.sessionStore();
      if (session) {
        try {
          session._dropCandidate(knowledgeId);
        } catch {
          // 会话 store 尚未初始化 / 候选已不在：不影响审批结果本身
        }
      }
    },

    sessionStore(): ReturnType<typeof useSessionStore> | null {
      try {
        // 会话 store 尚未初始化（单元测试只装 pinia 也能跑）：候选卡清理失败不影响审批结果
        return useSessionStore();
      } catch {
        return null;
      }
    },

    /**
     * 一项已经处理完（提交成功 / 已失效 / 已有结局）。
     *
     * 规则（用户任务书）：回到**队列列表**并保留位置与展开状态，由用户自己选下一条；
     * 不得自动进入下一条详情，也不立刻弹出剩余项的详情。
     */
    finishHandled(key: string): void {
      // 这一项已经有结局：它的编辑草稿不再有意义，删掉（其余项的草稿原样保留）
      if (key && Object.prototype.hasOwnProperty.call(this.drafts, key)) {
        delete this.drafts[key];
      }
      // 话题申请的「改过的名字」与「已收起」标记也随结局一起收口
      if (key && Object.prototype.hasOwnProperty.call(this.topicDrafts, key)) {
        delete this.topicDrafts[key];
      }
      if (key) this.collapsedTopicKeys.delete(key);
      const items = this.items as UnifiedApprovalItem[];
      const remaining = items.filter((item) => item.key !== key);
      if (!remaining.length) {
        this.visible = false;
        this.activeKey = null;
        this.queueListOnly = false;
        this.editingKey = null;
        return;
      }
      // 回到列表：activeKey 置空、列表态打开；队列顺序与展开状态都还在
      this.activeKey = null;
      this.queueListOnly = true;
      this.visible = true;
      this.editingKey = null;
      // 刚处理完的这一轮不再因轮询/队列变化自动弹回详情
      this.autoBlockedSeq = useApprovalsStore().arrivalSeq;
    },

    reset(): void {
      this.knowledge = [];
      this.knowledgeStatus = "idle";
      this.knowledgeError = "";
      this.knowledgeLoadedAt = null;
      this.visible = false;
      this.resultsVisible = false;
      this.activeKey = null;
      this.deferredKeys = [];
      this.collapsedTopicKeys = new Set<string>();
      this.topicDrafts = {};
      this.autoShownKeys = [];
      this.autoPending = false;
      this.submittingKeys = [];
      this.submitError = "";
      this.submitErrorCode = "";
      this.lastNotice = "";
      this.editingKey = null;
      this.drafts = {};
      this.queueListOnly = false;
      this.autoBlockedSeq = 0;
      this.invalidatedTopics = {};
      this.acknowledgedInvalidations = [];
      this.retryingAnswerRecord = "";
      persistDeferredKeys([]);
    },
  },
});