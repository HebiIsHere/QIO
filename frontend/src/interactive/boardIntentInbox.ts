/**
 * 对话内「待批准板面意图」的纯逻辑（Stage 2）。
 *
 * 契约（Lead 2026-10-21 冻结）：普通对话请求默认携带 interactive: { mode: "board" }；
 * board 模式下 QIO 可以产生 proposed / awaiting_confirmation 意图（不需要提交绑定）。
 * 对话视图据此列出 board_default 的待批准意图，提供「确认并落地 / 拒绝」，
 * 并如实转述 confirm 响应里的 applied / continuation。
 *
 * 这里只做可离线验证的映射，不发请求、不挂组件：
 * - 哪些意图进对话内批准列表；
 * - 标题 / 目标 / 简短预演摘要（不在对话里堆整段原文）；
 * - 确认结果文字：落地了就说落地，续作了就说「正在继续执行」，绝不假报「已完成」。
 */
import { INTENT_STATUS_LABELS } from "./approval";
import type { Intent, IntentStatus } from "./types";

/** 普通对话关联的板面（后端默认板面；对话请求不传 boardId 时就是它）。 */
export const CONVERSATION_BOARD_ID = "board_default";

/** 对话内可批准的意图状态（与后端 _CONFIRMABLE_STATUSES 同口径）。 */
export const BOARD_PENDING_STATUSES: IntentStatus[] = ["proposed", "awaiting_confirmation"];

export interface ConfirmContinuationLike {
  enqueued?: boolean;
  turn_id?: string | null;
}

export function isBoardPendingIntent(intent: Intent | null | undefined): boolean {
  if (!intent) return false;
  return BOARD_PENDING_STATUSES.includes(intent.status);
}

/**
 * 对话内待批准列表：只收 board_default 上未确认的意图，保持服务端顺序。
 * 别的板面的意图不进对话（对话只读它自己那一块板面）。
 */
export function boardPendingIntents(intents: Intent[] | null | undefined): Intent[] {
  return (intents ?? []).filter(
    (intent) =>
      isBoardPendingIntent(intent) &&
      String(intent.boardId ?? "").trim() === CONVERSATION_BOARD_ID,
  );
}

export interface ConversationIntentSummary {
  title: string;
  goal: string;
  statusLabel: string;
  /** 一句话预演摘要（数量 + 正文片段；没有内容时如实说没有） */
  previewText: string;
}

const SNIPPET_LIMIT = 18;
const SNIPPET_COUNT = 2;

function snippet(text: string): string {
  const flat = String(text ?? "").replace(/\s+/g, " ").trim();
  if (!flat) return "";
  return flat.length > SNIPPET_LIMIT ? flat.slice(0, SNIPPET_LIMIT) + "…" : flat;
}

/** 意图摘要：标题 / 目标 / 简短预演（数量与正文片段）。 */
export function conversationIntentSummary(intent: Intent): ConversationIntentSummary {
  const preview = intent.preview;
  const cards = (preview?.cards ?? []).filter((card) => card && !card.deleted);
  const groups = (preview?.groups ?? []).filter((group) => group && !group.deleted);
  const links = (preview?.links ?? []).filter((link) => link && !link.deleted);
  const counts: string[] = [];
  if (cards.length) counts.push(cards.length + " 张卡片");
  if (groups.length) counts.push(groups.length + " 个组");
  if (links.length) counts.push(links.length + " 条关系");
  let previewText: string;
  if (!counts.length) {
    previewText = "没有可展示的预演内容（确认后不会改动板面）。";
  } else {
    const snippets = cards
      .map((card) => snippet(card.content || String(card.meta?.name ?? "")))
      .filter(Boolean)
      .slice(0, SNIPPET_COUNT);
    previewText =
      counts.join("、") + (snippets.length ? "：" + snippets.join(" · ") : "");
  }
  return {
    title: String(intent.title ?? "").trim() || "QIO 提议的板面任务",
    goal: String(intent.goal ?? "").trim(),
    statusLabel: INTENT_STATUS_LABELS[intent.status] ?? String(intent.status ?? ""),
    previewText,
  };
}

/** 续作（continuation）的一句话；没有续作时为空串。 */
export function continuationText(
  continuation: ConfirmContinuationLike | null | undefined,
): string {
  if (!continuation?.enqueued) return "";
  const turnId = String(continuation.turn_id ?? "").trim();
  return (
    "后端已排入续作执行（正在继续执行" +
    (turnId ? "，续作轮 " + turnId : "") +
    "）"
  );
}

export interface ConfirmDetailInput {
  ok: boolean;
  /** 服务端给的失败原因（逐项 reason） */
  reason?: string;
  /** 同一请求里落地没完成的真实原因（board_changed / 某一步失败） */
  applyError?: string;
  /** 真实新增对象的可读摘要（appliedSummaryText 的结果） */
  landedText?: string;
  /** confirm 响应里的续作字段 */
  continuation?: ConfirmContinuationLike | null;
  /** 板面 / 任务清单刷新失败（不改变确认事实，但要如实附上） */
  refreshError?: string | null;
}

/**
 * 确认结果文字（Stage 2 冻结口径）：
 * 落地 ≠ 完成；续作 ≠ 完成 —— 只有服务端账本 applied 才算完成。
 */
export function confirmDetailText(input: ConfirmDetailInput): string {
  const continuation = continuationText(input.continuation);
  let detail: string;
  if (input.applyError) {
    detail =
      "确认已受理，但落地没有完成：" +
      input.applyError +
      "。意图可能已退回待确认，请刷新后重试。";
  } else if (!input.ok) {
    detail = "确认没有完成：" + (input.reason || "服务端未说明原因") + "。";
  } else if (input.landedText) {
    detail =
      "已确认并落地：板面新增 " +
      input.landedText +
      (continuation
        ? "；" + continuation + "。"
        : "（结果以板面与服务端账本为准）。");
  } else if (continuation) {
    detail = "已确认：" + continuation + "；结果以任务清单与账本为准。";
  } else {
    detail = "已确认：服务端没有返回落地结果，执行情况以任务清单与账本为准。";
  }
  if (input.refreshError) {
    detail += "（板面刷新失败：" + input.refreshError + "，请刷新重试）";
  }
  return detail;
}
