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
  /** 是否真的把表达交给 QIO。第一阶段没有接入模型调用，这里必须是 false */
  delivered: boolean;
  reason: string;
  detail: string;
  marking?: { updated: string[]; error?: string };
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
}

export interface DecideResult {
  ok: boolean;
  intent?: Intent;
  reason?: string;
  detail?: string;
  waitingFor?: string[];
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
