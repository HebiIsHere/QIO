/**
 * 第三阶段 C：意图视图 / 批量确认 / 等待 / 账本 / 结果 / 恢复的**纯逻辑**。
 *
 * 契约：docs/qio-refactor-contract.md §13.5。
 *
 * 这里只放可单测的规则与展示映射，不发请求、不读全局状态：
 * - 批量确认门槛必须是两条（与后端 api/interactive_intents.py 的 BATCH_CONFIRM_MIN 同值）；
 * - 状态文案只做「真实状态 → 人话」映射，不另造状态；
 * - 「已受理 / 已确认」绝不说成「已完成」：只有服务端返回 completed 才是完成。
 */

/** 同批批量确认的最少条数（契约 §13.5：前后端统一为两条；单条走单条确认）。 */
export const BATCH_CONFIRM_MIN = 2;

/** 契约 §13.2 冻结的意图状态（tool_models.INTENT_STATUSES）。 */
export type IntentViewStatus =
  | "proposed"
  | "awaiting_confirmation"
  | "approved"
  | "running"
  | "paused"
  | "completed"
  | "partially_completed"
  | "failed"
  | "cancelled"
  | "rejected";

export interface IntentBasisRefView {
  card_id: string;
  version: number;
  role?: string;
}

export interface IntentScopeView {
  read_card_ids?: string[];
  write_card_ids?: string[];
  group_ids?: string[];
  allowed_actions?: string[];
  max_new_cards?: number;
  max_new_groups?: number;
  max_new_links?: number;
}

export interface IntentConflictView {
  intent_id: string;
  status?: string;
}

export interface IntentOperationsSummary {
  applied?: number;
  running?: number;
  pending?: number;
  failed?: number;
  conflict?: number;
}

/** B 的 list_intents_view / get_intent 返回的单条意图视图（snake_case 冻结）。 */
export interface IntentView {
  intent_id: string;
  board_id?: string;
  client_ref?: string;
  submission_id?: string | null;
  version: number;
  status: IntentViewStatus | string;
  title: string;
  goal?: string;
  expected?: string;
  basis?: IntentBasisRefView[];
  scope?: IntentScopeView;
  depends_on?: string[];
  conflicts_with?: string[];
  conflicts?: IntentConflictView[];
  authorization?: Record<string, unknown>;
  authorization_valid?: boolean;
  outcome?: string;
  summary?: string;
  attempt?: number;
  operations_summary?: IntentOperationsSummary;
  created_at?: string;
  updated_at?: string;
  finished_at?: string | null;
  /** 旧实现兼容：原始旧状态（不冒充新状态） */
  raw_legacy_status?: string;
  demo?: boolean;
}

export interface BatchConfirmGate {
  min: number;
  eligible_ids: string[];
  available: boolean;
}

export interface IntentViewListResponse {
  available: boolean;
  source?: "service" | "legacy" | string;
  board_id?: string;
  seq?: number;
  intents: IntentView[];
  batch_confirm?: BatchConfirmGate;
  operations?: {
    available?: boolean;
    counts?: Record<string, number>;
    reason?: string;
  };
}

export interface WaitingView {
  board_id?: string;
  submission_id?: string;
  question?: string;
  missing_fields?: string[];
  question_ids?: string[];
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface WaitingResponse {
  available: boolean;
  waiting: WaitingView | WaitingView[] | null;
  reason?: string;
}

export interface LedgerOperationView {
  operation_id?: string;
  intent_id?: string;
  operation_key?: string;
  action?: string;
  status?: string;
  result?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface LedgerSummaryView {
  available: boolean;
  source?: string;
  counts?: Record<string, number>;
  operations?: LedgerOperationView[];
  reason?: string;
}

export interface IntentResultView {
  result_id: string;
  card_id?: string;
  intent_id?: string;
  kind?: string;
  title?: string;
  content?: string;
  source?: string;
  created_at?: string;
}

export interface IntentResultsResponse {
  available: boolean;
  source?: string;
  results: IntentResultView[];
  reason?: string;
}

export interface RecoveryPreviewView {
  intent_id: string;
  status?: string;
  can_resume?: boolean;
  reasons?: string[];
  unfinished_operations?: string[];
  unfinished_dependencies?: string[];
}

export interface RecoveryPreviewResponse {
  available: boolean;
  preview: RecoveryPreviewView | null;
}

export interface RecoveryResultView {
  ok: boolean;
  status?: string;
  reason?: string;
}

/**
 * P73 Stage 2：无操作计划的意图确认后，服务端自动排续作（同一响应里给出）。
 * 前端只如实转述，不把「已排入续作」说成「已完成」。
 */
export interface ConfirmContinuation {
  enqueued?: boolean;
  turn_id?: string | null;
}

export interface ConfirmItemResult {
  intent_id: string;
  ok?: boolean;
  status?: string;
  reason?: string;
  /** P73：确认写授权成功、但同一请求里的落地没完成时的真实原因（不会假报已落地） */
  apply_error?: string;
  /** P73：这一条确认即落地时真实新增的对象 id */
  applied?: ConfirmAppliedRefs;
  execution?: { id?: string; status?: string; steps?: unknown[] } | null;
  intent?: Record<string, unknown> | null;
  /** P73 Stage 2：这一条的续作信息（批量时可能逐项给出） */
  continuation?: ConfirmContinuation | null;
}

/** 新意图确认成功后，同一请求内真实落地的对象 id（服务端 applied 原样）。 */
export interface ConfirmAppliedRefs {
  cardIds?: string[];
  groupIds?: string[];
  linkIds?: string[];
}

export interface ConfirmResponse {
  ok?: boolean;
  results?: ConfirmItemResult[];
  confirmed?: string[];
  conflicts?: unknown[];
  min?: number;
  batch?: boolean;
  gate?: string;
  /**
   * Stage 1：确认即落地时给出真实对象 id（没有落地时缺省）。
   *
   * 两种形状都兼容：单条接口的实现按 intentId 分组
   * （`{ [intentId]: { cardIds, groupIds, linkIds } }`，批量确认天然需要分组），
   * 契约文字描述的是扁平形状；前端两种都认，不依赖某一种。
   */
  applied?: Record<string, ConfirmAppliedRefs> | ConfirmAppliedRefs;
  /** Stage 1：落地对应的执行摘要（服务端事实；没有执行时缺省/null） */
  execution?: { id?: string; status?: string; steps?: unknown[] } | null;
  /** P73 Stage 2：无操作计划意图确认后自动续作（enqueued=true 表示已排入续作轮） */
  continuation?: ConfirmContinuation | null;
}

export type StatusTone = "open" | "waiting" | "active" | "settled" | "problem";

/** 状态 → 人话（只映射真实状态；未知状态原样显示，绝不猜成完成）。 */
export const INTENT_STATUS_LABELS: Record<string, string> = {
  proposed: "待确认",
  awaiting_confirmation: "待再次确认",
  approved: "已确认（等待执行）",
  running: "执行中",
  paused: "已暂停",
  completed: "已完成",
  partially_completed: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  rejected: "已拒绝",
};

export function statusLabel(status: string | null | undefined): string {
  if (!status) return "状态未知";
  return INTENT_STATUS_LABELS[status] ?? status;
}

export function statusTone(status: string | null | undefined): StatusTone {
  switch (status) {
    case "completed":
      return "settled";
    case "partially_completed":
    case "failed":
    case "rejected":
      return "problem";
    case "running":
      return "active";
    case "paused":
    case "awaiting_confirmation":
      return "waiting";
    case "approved":
      return "waiting";
    default:
      return "open";
  }
}

/** 只有「尚未受理」的意图可以确认；已确认 / 执行中 / 已结束的不能再确认。 */
export function isConfirmable(view: IntentView): boolean {
  return view.status === "proposed" || view.status === "awaiting_confirmation";
}

export function confirmEligibleIds(intents: IntentView[]): string[] {
  return intents.filter(isConfirmable).map((view) => view.intent_id);
}

/** 已选条数是否达到同批批量确认门槛（两条）。 */
export function batchConfirmEnabled(selectedCount: number): boolean {
  return selectedCount >= BATCH_CONFIRM_MIN;
}

export function batchConfirmHint(selectedCount: number): string {
  if (selectedCount <= 0) {
    return "同批确认至少选择 " + BATCH_CONFIRM_MIN + " 条；只处理一条时请用单项确认。";
  }
  if (selectedCount < BATCH_CONFIRM_MIN) {
    return (
      "已选 " + selectedCount + " 条：同批批量确认至少 " + BATCH_CONFIRM_MIN +
      " 条；单条请用该项自己的「确认」，这里不会提交。"
    );
  }
  return "已选 " + selectedCount + " 条：可同批确认（依赖 / 冲突仍由服务端逐项校验）。";
}

/** 失败 / 部分完成 / 暂停且授权仍有效时可以进入恢复；其他状态不提供入口。 */
export function canRecover(view: IntentView): boolean {
  if (view.authorization_valid === false) return false;
  return ["paused", "failed", "partially_completed"].includes(view.status);
}

export function scopeSummary(scope: IntentScopeView | undefined): string {
  if (!scope) return "范围未记录";
  const parts: string[] = [];
  const writes = scope.write_card_ids?.length ?? 0;
  const reads = scope.read_card_ids?.length ?? 0;
  const groups = scope.group_ids?.length ?? 0;
  const actions = scope.allowed_actions?.length ?? 0;
  if (reads) parts.push("读取 " + reads + " 张卡");
  if (writes) parts.push("修改 " + writes + " 张卡");
  if (groups) parts.push("组 " + groups + " 个");
  if (actions) parts.push("动作 " + actions + " 类");
  const created = (scope.max_new_cards ?? 0) + (scope.max_new_groups ?? 0) + (scope.max_new_links ?? 0);
  if (created) parts.push("最多新建 " + created + " 个对象");
  return parts.length ? parts.join(" · ") : "范围未记录";
}

export function dependencyText(view: IntentView): string {
  const parts: string[] = [];
  if (view.depends_on?.length) parts.push("依赖 " + view.depends_on.length + " 项");
  if (view.conflicts_with?.length) parts.push("与 " + view.conflicts_with.length + " 项冲突");
  if (view.conflicts?.length) {
    const names = view.conflicts.map((item) => item.intent_id).filter(Boolean);
    if (names.length) parts.push("冲突对象：" + names.join("、"));
  }
  return parts.join(" · ");
}

export function operationsSummaryText(view: IntentView): string {
  const summary = view.operations_summary;
  if (!summary) return "";
  const labels: Array<[keyof IntentOperationsSummary, string]> = [
    ["applied", "已完成操作"],
    ["running", "执行中"],
    ["pending", "未开始"],
    ["failed", "失败"],
    ["conflict", "冲突"],
  ];
  const parts: string[] = [];
  for (const [key, label] of labels) {
    const value = summary[key] ?? 0;
    if (value > 0) parts.push(label + " " + value);
  }
  return parts.join(" · ");
}

/** 确认结果的人话：approved 只说「已确认」，绝不说「已完成」。 */
export function confirmOutcomeText(result: ConfirmResponse | null | undefined): string {
  const items = result?.results ?? [];
  if (!items.length) return "服务端没有返回逐项结果，状态以意图列表为准。";
  const lines = items.map((item) => {
    const label = statusLabel(item.status);
    const reason = (item.reason ?? "").trim();
    // P73：确认写授权成功、但同一请求里的落地没完成时也要如实说（不假装已落地）
    const applyError = String(item.apply_error ?? "").trim();
    const detail = [
      reason,
      applyError ? "落地没有完成：" + applyError : "",
    ]
      .filter(Boolean)
      .join("；");
    return item.intent_id + "：" + label + (detail ? "（" + detail + "）" : "");
  });
  const prefix = result?.batch ? "同批确认结果：" : "确认结果：";
  // P73 Stage 2：无操作计划的意图确认后由后端自动起续作轮 —— 如实转述，不说成已完成
  const continuation =
    result?.continuation ??
    items.find((item) => item.continuation?.enqueued)?.continuation ??
    null;
  const continuationNote = continuation?.enqueued
    ? "服务端已自动排入续作执行（正在继续执行" +
      (String(continuation.turn_id ?? "").trim()
        ? "，续作轮 " + String(continuation.turn_id).trim()
        : "") +
      "）。"
    : "";
  return (
    prefix +
    lines.join("；") +
    "。已确认不等于已完成，执行结果以账本与结果卡为准。" +
    continuationNote
  );
}

export function recoveryOutcomeText(result: RecoveryResultView | null | undefined): string {
  if (!result) return "恢复没有返回结果。";
  if (!result.ok) {
    return "恢复没有完成：" + ((result.reason ?? "").trim() || "服务端未说明原因") + "。";
  }
  return "恢复已受理：当前状态「" + statusLabel(result.status) + "」（不会自动执行操作，后续由 QIO 继续）。";
}

/** 结果正文是否属于「长卡片」：默认折叠，展开后在自己的滚动层里看全文。 */
export function isLongResultContent(content: string | null | undefined): boolean {
  const text = String(content ?? "");
  if (!text) return false;
  return text.length > 1200 || text.split("\n").length > 18;
}

/** 空正文不算结果卡：不显示假卡，也不二次生成正文。 */
export function hasResultContent(result: IntentResultView): boolean {
  return String(result.content ?? "").trim().length > 0;
}
