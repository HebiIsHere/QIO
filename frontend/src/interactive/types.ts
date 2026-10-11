/**
 * 互动模式的前端类型（与后端 `agent/interactive/models.py` 同构）。
 *
 * 契约：docs/interactive-mode-contract.md。字段含义以那里为准，改这里必须同步改契约。
 */

export type CardKind = "text" | "file" | "image" | "code" | "url" | "reply";

export interface BoardCard {
  id: string;
  kind: CardKind;
  content: string;
  meta: Record<string, unknown>;
  x: number;
  y: number;
  w: number;
  h: number;
  /** 注释勾选：本次是否允许 QIO 查看（默认 false）。勾选仍须提交 */
  checked: boolean;
  /** 明确隐藏 = 退出讨论范围；与 checked 互斥 */
  hidden: boolean;
  /** 折叠只改变显示 */
  folded: boolean;
  /** 书签只提高查找优先级，不产生意图 */
  bookmarked: boolean;
  /** 删除 = 撤回当前材料或关系 */
  deleted: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface BoardGroup {
  id: string;
  name: string;
  defaultName: boolean;
  /** 有序组显示明确序号；普通组的自由摆放不表示先后 */
  ordered: boolean;
  x: number;
  y: number;
  w: number;
  h: number;
  members: string[];
  deleted: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface BoardLink {
  id: string;
  src: string;
  dst: string;
  /** 方向是用户写明的；系统不得自行解释成因果 / 支持 / 执行顺序 */
  direction: boolean;
  meaning: string;
  deleted: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface BoardState {
  boardId: string;
  seq: number;
  updatedAt: string;
  cards: BoardCard[];
  groups: BoardGroup[];
  links: BoardLink[];
  /** 提交时仍然有效的多选 / 区域选择 */
  selection: string[];
}

/** 提交载荷里的板面状态：只含本次允许查看的范围 */
export interface Snapshot {
  cards: BoardCard[];
  groups: BoardGroup[];
  links: BoardLink[];
  selection: string[];
  empty: boolean;
}

export interface VisibleRange extends Snapshot {
  /** 本次不会交给 QIO 的卡片数量（只给用户看，不进载荷） */
  notVisibleCount?: number;
}

export type ExpressionKind =
  | "note_added"
  | "note_edited"
  | "note_deleted"
  | "material_added"
  | "material_removed"
  | "link_added"
  | "link_removed"
  | "link_meaning_changed"
  | "group_formed"
  | "group_merged"
  | "group_renamed"
  | "group_membership_changed"
  | "order_changed"
  | "ordered_changed"
  | "focus_selection"
  | "layout_only";

export interface Expression {
  id: string;
  kind: ExpressionKind;
  /** false = 只影响显示，不作为意图依据 */
  intentBearing: boolean;
  summary: string;
  cardIds: string[];
  groupId: string | null;
  linkId: string | null;
}

export interface SubmissionRecord {
  id: string;
  seq: number;
  status: "succeeded" | "failed" | "empty" | "duplicate";
  createdAt: string;
  visible?: VisibleRange;
  expressions?: Expression[];
  delivery?: DeliveryInfo;
  error?: string | null;
}

export interface DeliveryInfo {
  /** 是否真的把表达交给 QIO：受理成功 = 理解请求已可靠落库（模型随后在后台开始） */
  delivered: boolean;
  reason: string;
  detail: string;
  marking?: { updated: string[]; error?: string };
  /** 本次受理对应的理解请求（delivered=true 时给出；受理失败时为 null） */
  understandingId?: string | null;
  /** 受理状态：accepted = 已可靠受理、模型还没开始 */
  status?: UnderstandingStatus | null;
}

export interface BaselineInfo {
  updated: boolean;
  firstSubmission?: boolean;
  previousSeq: number | null;
  seq?: number;
}

export interface SubmissionResult {
  status: "succeeded" | "failed" | "empty" | "duplicate";
  submission: SubmissionRecord;
  before: Snapshot;
  after: Snapshot;
  expressions: Expression[];
  baseline: BaselineInfo;
  delivery: DeliveryInfo;
  visibleRange: VisibleRange;
  checkedCleared: string[];
  error?: string;
}

export interface BoardStateResponse {
  board: { id: string; title: string };
  state: BoardState;
  seq: number;
  baseline: { seq: number; submittedAt: string } | null;
  submissions: SubmissionRecord[];
  drafts: { drafts: Record<string, string>; updatedAt: string | null };
  /** 未提交的有效改动（服务端每次保存后重算；它不进 QIO） */
  pending?: Expression[];
  /** 本次允许查看的范围（服务端以已保存状态推导） */
  visibleRange?: VisibleRange;
}

/**
 * 一次影响确认/取消的**真实结果**（N2）：界面只依据它声明「已保存 / 任务已暂停」。
 *
 * - `saved`：PUT 真的落到这一次候选（seq 前进），这才是成功；
 * - `needs_confirm`：候选保留、没落库，正在等用户再次确认；
 * - `cancelled`：用户取消，改动不生效、任务继续；
 * - `check_failed`：保存前的影响预判没完成（原因见 reason），没落库；
 * - `save_failed`：写入失败（原因见 reason），没落库；
 * - `superseded`：确认对应的候选已被更新的改动取代，已重新核实，本次没落库。
 */
export type ImpactConfirmOutcome =
  | "saved"
  | "needs_confirm"
  | "cancelled"
  | "check_failed"
  | "save_failed"
  | "superseded";

export interface ImpactConfirmResult {
  outcome: ImpactConfirmOutcome;
  /** 只在未成功时给可读的真实原因（不截断） */
  reason?: string;
  /** 仅 `saved`：这一次真实被暂停的意图（来自服务端 materialImpact.paused，不是客户端猜的） */
  paused?: Intent[];
}

export interface IntentPreview {
  cards: BoardCard[];
  groups: BoardGroup[];
  links: BoardLink[];
  note?: string;
}

export interface IntentImpact {
  objects: string[];
  tasks: string[];
  consequences: string[];
}

export type IntentStatus =
  | "pending"
  | "needs_update"
  | "rejected"
  | "waiting_dependency"
  | "waiting_confirm"
  | "running"
  | "paused"
  | "done"
  | "failed"
  | "cancelled";

export interface Intent {
  id: string;
  boardId: string;
  submissionId: string | null;
  title: string;
  summary: string;
  status: IntentStatus;
  preview: IntentPreview;
  impact: IntentImpact;
  dependsOn: string[];
  conflictsWith: string[];
  conflictKey: string;
  materialRefs: string[];
  progress: { done: number; total: number; text: string };
  reason: string;
  /** true = 演示意图（界面上必须写明） */
  demo: boolean;
  /** 真实理解产出的目标（演示意图也给出，便于一致显示） */
  goal?: string;
  /** 预期结果 */
  expected?: string;
  /** 具体板面操作（只可能是用户在互动模式里能做的子集） */
  operations?: IntentOperation[];
  /** 依据的材料与版本 */
  basis?: IntentBasis[];
  /** 稳定身份：同一件工作更新时沿用，不复制新任务 */
  intentKey?: string;
  /** 来自哪一次理解请求（结果卡、引用与任务记录靠它互相定位） */
  understandingId?: string | null;
  /** 最近一次真实执行记录（未执行过时为 null） */
  execution?: Execution | null;
  createdAt: string;
  updatedAt: string;
  /** 等待依赖时列出还没完成的前项 */
  waitingFor?: string[];
  applied?: { cardIds: string[]; groupIds: string[]; linkIds: string[] };
  revert?: RevertReport;
}

export interface RevertReport {
  reverted: string[];
  kept: string[];
  pendingDecision: { id: string; reason: string; impact: string }[];
  reasonText: string;
}

export interface IntentListResponse {
  intents: Intent[];
  conflicts: string[][];
  batchAvailable: boolean;
  recovery: { paused: string[] };
  /** 本板面最近的真实执行记录（服务端给出；前端不自己拼） */
  executions?: Execution[];
}

export interface DecideResult {
  ok: boolean;
  intent?: Intent;
  reason?: string;
  detail?: string;
  waitingFor?: string[];
  /** 批准后真实启动的那次执行（契约 §14.4：approve 必须同步返回它） */
  execution?: Execution | null;
}

export function emptyBoardState(boardId: string): BoardState {
  return {
    boardId,
    seq: 0,
    updatedAt: new Date().toISOString(),
    cards: [],
    groups: [],
    links: [],
    selection: [],
  };
}

export function cloneState<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}


// --- 真实模型理解与真实执行（第 9 轮，2026-10-11）-------------------------
// 契约：docs/interactive-mode-contract.md §14（本轮冻结接口）

/** 一次提交的理解过程状态：受理与理解分开，不再用 succeeded 兼代表两者 */
export type UnderstandingStatus =
  | "accepted"
  | "understanding"
  | "understood"
  | "needs_clarification"
  | "no_action"
  | "failed";

export type UnderstandingKind = "intent" | "clarification" | "no_action";

/** 材料在本次理解中的真实来源：拿到地址 / 文件名都不等于读到正文 */
export type MaterialSource =
  | "inline"
  | "attachment_text"
  | "url_fetched"
  | "url_address_only"
  | "image_unsupported"
  | "unreadable"
  | "unsupported";

export interface UnderstandingMaterial {
  cardId: string;
  kind: CardKind;
  label: string;
  source: MaterialSource;
  /** 材料版本（内容指纹 / 附件版本）；审批与执行按它对账 */
  version: string;
  /** 是否真的读到了正文 */
  read: boolean;
  truncated: boolean;
  bytes: number;
  characters: number;
  /** read=false 时给出可读原因；read=true 时为空 */
  reason: string;
  attachmentId?: string | null;
}

export interface UnderstandingStep {
  key: "accepted" | "materials" | "model" | "validation" | "persist" | string;
  label: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  detail?: string;
}

export interface UnderstandingClarification {
  question: string;
  missing: string[];
}

export interface Understanding {
  id: string;
  boardId: string;
  submissionId: string | null;
  requestId: string;
  status: UnderstandingStatus;
  /** 当前阶段的说明（同阶段更新它，阶段切换才收起上一阶段） */
  stage: string;
  kind: UnderstandingKind | null;
  steps: UnderstandingStep[];
  materials: UnderstandingMaterial[];
  summary: string;
  clarification: UnderstandingClarification | null;
  validation: { ok: boolean; reasons: string[] };
  error: string | null;
  attempt: number;
  adapter: string;
  intentIds: string[];
  memory: { text: string; kind: string }[];
  createdAt: string;
  updatedAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
}

export interface UnderstandingResponse {
  understanding: Understanding | null;
  understandings: Understanding[];
}

/** 允许的板面操作：用户在互动模式中能做的子集 */
export type IntentOperationKind =
  | "add_reply_card"
  | "add_text_card"
  | "move_card"
  | "group_cards"
  | "order_group"
  | "add_link";

export interface IntentOperation {
  op: IntentOperationKind;
  content?: string;
  title?: string;
  cardId?: string;
  cardIds?: string[];
  groupId?: string;
  memberOrder?: string[];
  ordered?: boolean;
  name?: string;
  src?: string;
  dst?: string;
  direction?: boolean;
  meaning?: string;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
}

export interface IntentBasis {
  cardId: string;
  role: "material" | "requirement" | "context";
  version: string;
  label?: string;
}

export type ExecutionStatus = "queued" | "running" | "paused" | "done" | "failed" | "cancelled";

export interface ExecutionStep {
  index: number;
  op: IntentOperationKind | string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  cardId?: string | null;
  groupId?: string | null;
  linkId?: string | null;
  detail?: string;
}

export interface Execution {
  id: string;
  intentId: string;
  boardId: string;
  attempt: number;
  status: ExecutionStatus;
  steps: ExecutionStep[];
  result: { cardIds: string[]; groupIds: string[]; linkIds: string[] };
  error: string | null;
  createdAt: string;
  updatedAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  ownerInstanceId?: string | null;
}

export interface ExecutionControlResult {
  ok: boolean;
  intent?: Intent;
  execution?: Execution;
  reason?: string;
  detail?: string;
}

