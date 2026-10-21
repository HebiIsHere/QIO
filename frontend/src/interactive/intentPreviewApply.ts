/**
 * 意图预演 → 「批准即落地」（Stage 1）的前端纯逻辑。
 *
 * 契约：docs/superpowers/specs/2026-10-21-intent-preview-apply-design.md §4 / §5。
 *
 * 这里只做可以离线验证的规则，不挂组件、不发请求：
 * - 哪些意图该在板面上画虚线预演（新状态 proposed / awaiting_confirmation / approved，
 *   以及旧状态；落地（applied 非空）或结束后必须消失，不能和真卡叠在一起）；
 * - 批准走哪条接口：**新状态只能走** `POST /api/interactive/intents/{id}/confirm`，
 *   旧状态才继续走 `/approve`（旧接口对新意图语义不对）；
 * - 幽灵卡的拖动 / 删除怎么改 operations：预览卡片 id 与服务端
 *   `preview_from_operations` 同口径（add 操作的 ref / cardId，缺省 p_<下标>），
 *   删除时连带清理引用它的 group_cards / add_link / move_card / order_group；
 * - PATCH plan 失败时**回滚本地预演**（返回修改前的 preview / operations），绝不假成功。
 */
import type {
  Intent,
  IntentOperation,
  IntentOperationKind,
  IntentPreview,
  IntentStatus,
} from "./types";

/** 新增类操作：只有它们会产生预览里的幽灵卡。 */
export const ADD_INTENT_OPS: IntentOperationKind[] = ["add_reply_card", "add_text_card"];

/**
 * 仍在等待 / 尚未落地的意图：虚线预演要一直画在真实板面上。
 *
 * 新状态：proposed（待确认）/ awaiting_confirmation（待再次确认）/ approved（已确认待落地）；
 * 旧状态保留兼容（0.2.10 及以前的数据仍可能读回来）。
 */
export const PREVIEW_INTENT_STATUSES: IntentStatus[] = [
  "pending",
  "needs_update",
  "waiting_dependency",
  "waiting_confirm",
  "proposed",
  "awaiting_confirmation",
  "approved",
  "running",
];

/** 允许改预演（拖动 / 删除幽灵卡）的未批准状态（契约 §4.3：approved 之后 409 not_editable）。 */
export const PLAN_EDITABLE_STATUSES: IntentStatus[] = ["proposed", "awaiting_confirmation"];

/** 必须走新 confirm 接口的状态；旧 /approve 对它们语义不对。 */
export const NEW_CONFIRM_STATUSES: IntentStatus[] = ["proposed", "awaiting_confirmation"];

export interface PreviewIntentLike {
  status: IntentStatus;
  preview?: IntentPreview | null;
  applied?: Intent["applied"];
}

export interface PlanEditableLike {
  status: IntentStatus;
  operations?: IntentOperation[];
}

/** applied 里真的落了对象（空对象 / 空数组不算）。 */
export function hasAppliedObjects(applied: Intent["applied"] | null | undefined): boolean {
  if (!applied || typeof applied !== "object") return false;
  return Boolean(
    (applied.cardIds?.length ?? 0) + (applied.groupIds?.length ?? 0) + (applied.linkIds?.length ?? 0),
  );
}

/** 预览里是否有可画的东西（只有连线不算：连线两端必须有卡）。 */
export function previewHasContent(preview: IntentPreview | null | undefined): boolean {
  if (!preview || typeof preview !== "object") return false;
  const cards = (preview.cards ?? []).filter((card) => card && !card.deleted);
  const groups = (preview.groups ?? []).filter((group) => group && !group.deleted);
  return cards.length > 0 || groups.length > 0;
}

/**
 * 这条意图是否该画虚线预演：
 * 状态仍是未落地状态、还没有 applied 对象、并且真的有可画内容。
 */
export function shouldPreviewIntent(intent: PreviewIntentLike | null | undefined): boolean {
  if (!intent) return false;
  if (!PREVIEW_INTENT_STATUSES.includes(intent.status)) return false;
  if (hasAppliedObjects(intent.applied)) return false;
  return previewHasContent(intent.preview);
}

/** 这条意图的预演现在能不能改（拖动位置 / 删除新增卡）。 */
export function isPlanEditable(intent: PlanEditableLike | null | undefined): boolean {
  if (!intent) return false;
  if (!PLAN_EDITABLE_STATUSES.includes(intent.status)) return false;
  return (intent.operations?.length ?? 0) > 0;
}

export type ApprovalRoute = "confirm" | "legacy-approve" | "none";

/**
 * 批准该走哪条接口。
 * - confirm：新状态；成功即在同一请求里落地（applied / execution）；
 * - legacy-approve：旧状态（pending / waiting_confirm / paused），沿用既有 /approve；
 * - none：已确认 / 执行中 / 已结束，不再提供批准入口。
 */
export function approvalRoute(intent: { status: IntentStatus } | null | undefined): ApprovalRoute {
  if (!intent) return "none";
  if (NEW_CONFIRM_STATUSES.includes(intent.status)) return "confirm";
  if (["pending", "waiting_confirm", "paused"].includes(intent.status)) return "legacy-approve";
  return "none";
}

function isAddOperation(operation: IntentOperation | null | undefined): boolean {
  return Boolean(operation && ADD_INTENT_OPS.includes(operation.op));
}

/**
 * 预览里的幽灵卡 id（与服务端 execution.preview_from_operations 逐字同口径）：
 * add 操作的 `ref`（其次 `cardId`）；都没有时是 `p_<操作下标>`。
 */
export function previewCardIdForOperation(
  operation: IntentOperation | null | undefined,
  index: number,
): string {
  const symbol = String(operation?.ref ?? operation?.cardId ?? "").trim();
  return symbol || "p_" + String(index);
}

/** 幽灵卡 id → 它在 operations 里的 add 操作下标（找不到 -1）。 */
export function previewCardOperationIndex(
  operations: IntentOperation[] | null | undefined,
  previewCardId: string,
): number {
  const list = operations ?? [];
  for (let index = 0; index < list.length; index += 1) {
    const operation = list[index];
    if (!isAddOperation(operation)) continue;
    if (previewCardIdForOperation(operation, index) === previewCardId) return index;
  }
  return -1;
}

export interface PlanPoint {
  x: number;
  y: number;
}

export type PlanEditResult =
  | { ok: true; operations: IntentOperation[] }
  | { ok: false; reason: string };

function finiteRounded(value: unknown): number | null {
  const number = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.round(number);
}

/**
 * 拖动幽灵卡：把对应 add 操作的 x/y 改成板面坐标（并去掉旧的相对摆放 place，
 * 避免「显式坐标 + 相对摆放」两种落点并存）。
 */
export function movePreviewCardInPlan(
  operations: IntentOperation[] | null | undefined,
  previewCardId: string,
  point: PlanPoint,
): PlanEditResult {
  const list = operations ?? [];
  const index = previewCardOperationIndex(list, previewCardId);
  if (index < 0) {
    return { ok: false, reason: "这张预演卡片没有对应的新增操作，不能修改位置。" };
  }
  const x = finiteRounded(point?.x);
  const y = finiteRounded(point?.y);
  if (x === null || y === null) {
    return { ok: false, reason: "拖动落点坐标无效，这次修改没有提交。" };
  }
  const next = list.slice();
  const moved: IntentOperation = { ...list[index], x, y };
  delete moved.place;
  next[index] = moved;
  return { ok: true, operations: next };
}

function bareCardKey(value: string): string {
  return value.startsWith("card:") ? value.slice("card:".length) : value;
}

/** 一个 add 操作在 operations 里可能被引用到的全部写法（id 本身 / 裸符号 / card: 前缀）。 */
function cardReferenceAliases(
  operation: IntentOperation | null | undefined,
  previewCardId: string,
  previewRefs?: Record<string, string> | null,
): Set<string> {
  const aliases = new Set<string>();
  const add = (value: unknown): void => {
    const key = String(value ?? "").trim();
    if (!key) return;
    aliases.add(key);
    aliases.add("card:" + bareCardKey(key));
    aliases.add(bareCardKey(key));
  };
  add(previewCardId);
  add(operation?.ref);
  add(operation?.cardId);
  // 服务端 preview.refs 里的两种写法（裸符号与 card: 别名）也认
  for (const [key, value] of Object.entries(previewRefs ?? {})) {
    if (String(value ?? "") === previewCardId) add(key);
  }
  return aliases;
}

function cardReferenceMatches(value: unknown, aliases: Set<string>): boolean {
  const key = String(value ?? "").trim();
  if (!key) return false;
  return aliases.has(key) || aliases.has(bareCardKey(key));
}

/** 一个 group_cards 操作在 operations 里可能被引用的写法（给 order_group 的 groupId 用）。 */
function groupReferenceAliases(
  operation: IntentOperation | null | undefined,
  fallbackId: string,
): Set<string> {
  const aliases = new Set<string>();
  const key = String(operation?.ref ?? operation?.groupId ?? "").trim() || fallbackId;
  aliases.add(key);
  aliases.add("group:" + bareCardKey(key));
  aliases.add(bareCardKey(key));
  return aliases;
}

function groupReferenceMatches(value: unknown, aliases: Set<string>): boolean {
  const key = String(value ?? "").trim();
  if (!key) return false;
  return aliases.has(key) || aliases.has(bareCardKey(key));
}

/**
 * 删除幽灵卡：移除它对应的 add 操作，以及引用它的依赖操作。
 *
 * - group_cards：成员里有这张卡 → 整个组操作移除（组只剩别的成员时也不保留半组，
 *   与服务端 preview 的派生口径一致：组只由一次 group_cards 产生）；
 * - add_link / move_card：src / dst / cardId 引用它 → 移除；
 * - order_group：目标组被移除 → 移除；否则 memberOrder 里去掉这张卡
 *   （顺序操作本身仍然有效，不留悬空成员）。
 */
export function removePreviewCardFromPlan(
  operations: IntentOperation[] | null | undefined,
  previewCardId: string,
  previewRefs?: Record<string, string> | null,
): PlanEditResult {
  const list = operations ?? [];
  const index = previewCardOperationIndex(list, previewCardId);
  if (index < 0) {
    return { ok: false, reason: "这张预演卡片没有对应的新增操作，不能删除。" };
  }
  const aliases = cardReferenceAliases(list[index], previewCardId, previewRefs);

  // 第一遍：找出因成员被删而整体移除的组，以及它们的别名（给 order_group 用）。
  const droppedGroupAliases = new Set<string>();
  list.forEach((operation, position) => {
    if (operation.op !== "group_cards") return;
    const members = operation.cardIds ?? [];
    if (!members.some((member) => cardReferenceMatches(member, aliases))) return;
    for (const alias of groupReferenceAliases(operation, "pg_" + String(position))) {
      droppedGroupAliases.add(alias);
    }
  });

  // 第二遍：移除该 add 操作与全部依赖操作。
  const next: IntentOperation[] = [];
  list.forEach((operation, position) => {
    if (position === index) return;
    switch (operation.op) {
      case "group_cards": {
        const members = operation.cardIds ?? [];
        if (members.some((member) => cardReferenceMatches(member, aliases))) return;
        next.push(operation);
        return;
      }
      case "add_link": {
        if (
          cardReferenceMatches(operation.src, aliases) ||
          cardReferenceMatches(operation.dst, aliases)
        ) {
          return;
        }
        next.push(operation);
        return;
      }
      case "move_card": {
        if (cardReferenceMatches(operation.cardId, aliases)) return;
        next.push(operation);
        return;
      }
      case "order_group": {
        if (
          droppedGroupAliases.size > 0 &&
          groupReferenceMatches(operation.groupId, droppedGroupAliases)
        ) {
          return;
        }
        const members = operation.memberOrder;
        if (members?.length) {
          const kept = members.filter((member) => !cardReferenceMatches(member, aliases));
          if (!kept.length) return;
          if (kept.length !== members.length) {
            next.push({ ...operation, memberOrder: kept });
            return;
          }
        }
        next.push(operation);
        return;
      }
      default:
        next.push(operation);
    }
  });
  return { ok: true, operations: next };
}

export interface PlanEditBaseline {
  preview: IntentPreview;
  operations: IntentOperation[];
}

export interface PlanPatchIntentLike {
  preview?: IntentPreview | null;
  operations?: IntentOperation[];
}

export interface PlanPatchResponseLike {
  ok?: boolean;
  intent?: PlanPatchIntentLike | null;
  error?: string;
  detail?: string;
  reason?: string;
}

export interface PlanEditOutcome {
  ok: boolean;
  /** 成功 = 服务端返回的预览；失败 = 修改前的预览（本地回滚的依据）。 */
  preview: IntentPreview;
  operations: IntentOperation[];
  /** 失败时的真实原因（空串 = 成功）。 */
  error: string;
}

function responseReason(response: PlanPatchResponseLike | null | undefined): string {
  const detail = String(response?.detail ?? "").trim();
  const reason = String(response?.reason ?? "").trim();
  const error = String(response?.error ?? "").trim();
  return detail || reason || error;
}

/**
 * 把一次预演修改发给服务端（PATCH plan），并给出本地该采用哪一版：
 * - 成功：采用服务端返回的 preview / operations（服务端事实优先）；
 * - 失败（ok:false 或抛错）：返回修改前的 baseline —— 调用方据此回滚本地预演、
 *   并把真实原因提示给用户，绝不假装保存成功。
 */
export async function persistPlanEdit(args: {
  baseline: PlanEditBaseline;
  nextOperations: IntentOperation[];
  patch: (operations: IntentOperation[]) => Promise<PlanPatchResponseLike | null | undefined>;
}): Promise<PlanEditOutcome> {
  const baseline = args.baseline;
  try {
    const response = await args.patch(args.nextOperations);
    if (!response || response.ok === false) {
      return {
        ok: false,
        preview: baseline.preview,
        operations: baseline.operations,
        error: responseReason(response) || "服务端没有接受这次预演修改。",
      };
    }
    return {
      ok: true,
      preview: response.intent?.preview ?? baseline.preview,
      operations: response.intent?.operations ?? args.nextOperations,
      error: "",
    };
  } catch (error) {
    return {
      ok: false,
      preview: baseline.preview,
      operations: baseline.operations,
      error: (error as Error)?.message || "预演修改没有保存：原因未知。",
    };
  }
}

// --- 确认即落地的 applied 解析 -------------------------------------------------
//
// 服务端单条 / 批量 confirm 的 applied 实际按 intentId 分组
// （{ [intentId]: { cardIds, groupIds, linkIds, previewIdMap } }，批量天然需要分组），
// 契约文字描述的是扁平形状。前端两种都认，绝不依赖某一种，也不把别的意图的落地
// 算到当前这一条头上。

export interface ConfirmAppliedRefsLike {
  cardIds?: string[];
  groupIds?: string[];
  linkIds?: string[];
  previewIdMap?: Record<string, string>;
  appliedAt?: string;
}

export type ConfirmAppliedLike =
  | Record<string, ConfirmAppliedRefsLike>
  | ConfirmAppliedRefsLike
  | null
  | undefined;

function looksLikeAppliedRefs(value: unknown): value is ConfirmAppliedRefsLike {
  if (!value || typeof value !== "object") return false;
  const record = value as ConfirmAppliedRefsLike;
  return (
    Array.isArray(record.cardIds) ||
    Array.isArray(record.groupIds) ||
    Array.isArray(record.linkIds)
  );
}

/** 从确认响应的 applied 里取**指定意图**的真实落地对象（没有就 null）。 */
export function appliedForIntent(
  applied: ConfirmAppliedLike,
  intentId: string,
): ConfirmAppliedRefsLike | null {
  if (!applied || typeof applied !== "object") return null;
  if (looksLikeAppliedRefs(applied)) return applied;
  const entry = (applied as Record<string, ConfirmAppliedRefsLike>)[intentId];
  return looksLikeAppliedRefs(entry) ? entry : null;
}

/** applied 的可读摘要（只描述服务端给出的真实 id 数量；没有落地返回空串）。 */
export function appliedSummaryText(
  applied: ConfirmAppliedRefsLike | null | undefined,
): string {
  if (!applied) return "";
  const cards = applied.cardIds?.length ?? 0;
  const groups = applied.groupIds?.length ?? 0;
  const links = applied.linkIds?.length ?? 0;
  if (!cards && !groups && !links) return "";
  const parts: string[] = [];
  if (cards) parts.push(cards + " 张卡片");
  if (groups) parts.push(groups + " 个组");
  if (links) parts.push(links + " 条关系");
  return parts.join("、");
}
