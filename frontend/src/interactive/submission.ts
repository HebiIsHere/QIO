/**
 * 提交相关的纯函数（子智能体 B 负责）。
 *
 * 契约：docs/interactive-mode-contract.md §1.4 / §1.5 / §4.4。
 *
 * 这里**只做展示用的推导**，不做权限判断：
 * 真正决定「哪些内容交给 QIO」的是服务端（从已保存状态推导，见
 * agent/interactive/submission.py）。`localVisibleRange` 只是让界面在服务端响应之前
 * 也能说清「未勾选 / 明确隐藏的不在其中」，规则与 models.selectable_cards 一致：
 * 文字注释要勾选；材料（file/image/code/url）默认在范围内；reply / hidden / deleted 不在。
 */
import type {
  BoardCard,
  BoardState,
  Expression,
  ExpressionKind,
  SubmissionResult,
  VisibleRange,
} from "./types";

export type SubmitStatusLike =
  | "idle"
  | "submitting"
  | "succeeded"
  | "failed"
  | "empty"
  | "duplicate";

export type SaveStatusLike = "idle" | "saving" | "saved" | "error";

export interface ChangeSummary {
  /** 作为「用户提出的表达」的改动（intentBearing=true） */
  bearing: Expression[];
  /** 仍然记录为板面变化，但不构成工作请求（材料增删、撤回等） */
  nonIntent: Expression[];
  /** 普通移动 / 缩放：只影响显示 */
  layoutOnly: Expression[];
  total: number;
  bearingCount: number;
  layoutCount: number;
  /** 是否有可提交内容：只有 layout_only 时没有 */
  hasContent: boolean;
  /** 一句话摘要（永远用文字说明，不靠颜色） */
  headline: string;
}

export interface VisibleRangeCounts {
  cards: number;
  notes: number;
  materials: number;
  groups: number;
  links: number;
  notVisible: number;
}

const MATERIAL_KINDS = ["file", "image", "code", "url"];

/** 单张卡片是否在本次允许查看的范围内（与服务端 models.selectable_cards 一致）。 */
export function isVisibleCard(card: BoardCard): boolean {
  if (card.deleted || card.hidden) return false;
  if (card.kind === "reply") return false;
  if (card.kind === "text") return card.checked;
  return true;
}

/** 本地预览用的可见范围（权威结果以服务端为准）。 */
export function localVisibleRange(state: BoardState | null | undefined): VisibleRange {
  const cards = state?.cards ?? [];
  const visible = cards.filter(isVisibleCard);
  const ids = new Set(visible.map((card) => card.id));
  const groups = (state?.groups ?? [])
    .filter((group) => !group.deleted && group.members.some((id) => ids.has(id)))
    .map((group) => ({ ...group, members: group.members.filter((id) => ids.has(id)) }));
  const links = (state?.links ?? []).filter(
    (link) => !link.deleted && ids.has(link.src) && ids.has(link.dst),
  );
  const selection = (state?.selection ?? []).filter((id) => ids.has(id));
  const live = cards.filter((card) => !card.deleted);
  return {
    cards: visible,
    groups,
    links,
    selection,
    empty: visible.length === 0 && groups.length === 0 && links.length === 0,
    notVisibleCount: Math.max(0, live.length - visible.length),
  };
}

export function visibleRangeCounts(range: VisibleRange | null | undefined): VisibleRangeCounts {
  const cards = range?.cards ?? [];
  return {
    cards: cards.length,
    notes: cards.filter((card) => card.kind === "text").length,
    materials: cards.filter((card) => MATERIAL_KINDS.includes(card.kind)).length,
    groups: range?.groups?.length ?? 0,
    links: range?.links?.length ?? 0,
    notVisible: range?.notVisibleCount ?? 0,
  };
}

/** 允许查看范围的展示文案：明确写出「未勾选的不在其中」。 */
export function visibleRangeText(range: VisibleRange | null | undefined): string {
  if (!range) {
    return "服务端还没有返回本次允许查看的范围：未勾选与明确隐藏的注释不会被查看，也不会进入提交载荷。";
  }
  const counts = visibleRangeCounts(range);
  if (counts.cards === 0 && counts.groups === 0 && counts.links === 0) {
    return "本次允许 QIO 查看的范围是空的：没有任何内容会被交给 QIO（未勾选与明确隐藏的注释不在其中）。";
  }
  const parts = [`注释 ${counts.notes} 条`, `材料 ${counts.materials} 项`];
  if (counts.groups > 0) parts.push(`组 ${counts.groups} 个`);
  if (counts.links > 0) parts.push(`关系 ${counts.links} 条`);
  const tail =
    counts.notVisible > 0
      ? `另有 ${counts.notVisible} 张卡片不在其中（未勾选的注释、明确隐藏的卡片与 QIO 结果都不会被查看）。`
      : "没有被排除的卡片。";
  return `本次允许 QIO 查看：${parts.join("、")}。${tail}`;
}

const KIND_LABELS: Record<ExpressionKind, string> = {
  note_added: "新增注释",
  note_edited: "修改注释",
  note_deleted: "撤回注释",
  material_added: "新增材料",
  material_removed: "移除材料",
  link_added: "新增关系",
  link_removed: "移除关系",
  link_meaning_changed: "修改关系含义",
  group_formed: "形成组",
  group_merged: "合并组",
  group_renamed: "组改名",
  group_membership_changed: "组成员变化",
  order_changed: "调整顺序",
  ordered_changed: "有序设置变化",
  focus_selection: "关注范围",
  layout_only: "普通移动 / 缩放",
};

export function changeKindLabel(kind: ExpressionKind): string {
  return KIND_LABELS[kind] ?? String(kind);
}

/** 每条改动是否作为「用户提出的表达」，用文字写明（不能只靠颜色区分）。 */
export function changeBearingText(expression: Expression): string {
  if (expression.kind === "layout_only") return "只影响显示，不作为表达";
  return expression.intentBearing ? "作为表达" : "只记录变化，不作为工作请求";
}

/** 本次有效改动摘要（服务端求差的结果，或保存后重算的未提交改动）。 */
export function summarizeChanges(expressions: Expression[] | null | undefined): ChangeSummary {
  const list = expressions ?? [];
  const layoutOnly = list.filter((item) => item.kind === "layout_only");
  const rest = list.filter((item) => item.kind !== "layout_only");
  const bearing = rest.filter((item) => item.intentBearing);
  const nonIntent = rest.filter((item) => !item.intentBearing);

  const parts: string[] = [];
  if (!list.length) {
    parts.push("本次还没有可提交的改动");
  } else if (bearing.length) {
    parts.push(`本次有 ${bearing.length} 项有效表达`);
  } else {
    parts.push(`本次有 ${rest.length} 项板面变化，都不构成工作请求`);
  }
  if (bearing.length && nonIntent.length) {
    parts.push(`其中 ${nonIntent.length} 项只记录变化（材料增删、撤回等）`);
  }
  if (layoutOnly.length) {
    parts.push(`${layoutOnly.length} 项普通移动 / 缩放只影响显示`);
  }
  return {
    bearing,
    nonIntent,
    layoutOnly,
    total: list.length,
    bearingCount: bearing.length,
    layoutCount: layoutOnly.length,
    hasContent: rest.length > 0,
    headline: parts.join("；"),
  };
}

/** 「未提交 / 已提交」状态标签（同时给文字，不靠颜色）。 */
export function submitStateLabel(
  status: SubmitStatusLike,
  options: { pendingCount?: number; hasSubmission?: boolean } = {},
): string {
  const pending = options.pendingCount ?? 0;
  const hasSubmission = Boolean(options.hasSubmission);
  if (status === "submitting") return "正在提交";
  if (status === "failed") return "提交失败（未提交）";
  if (status === "empty") return "无内容可提交";
  if (status === "duplicate") return "未重复提交";
  if (status === "succeeded") return "已提交";
  if (!hasSubmission) return "未提交";
  return pending > 0 ? "已提交过：有新的改动尚未提交" : "已提交：没有新的改动";
}

export interface SubmitStatusContext {
  result?: SubmissionResult | null;
  pendingCount?: number;
  error?: string | null;
}

/** 提交状态说明：说清做了什么、没做什么，绝不假装 QIO 已经理解。 */
export function submitStatusText(
  status: SubmitStatusLike,
  context: SubmitStatusContext = {},
): string {
  const pending = context.pendingCount ?? 0;
  switch (status) {
    case "submitting":
      return "正在提交：先保存板面，再让服务端算出差值（这一步才会调用 QIO 入口）。";
    case "succeeded": {
      const count = context.result?.expressions?.length ?? 0;
      const cleared = context.result?.checkedCleared?.length ?? 0;
      return [
        `已提交：本次 ${count} 项改动已记录`,
        "第一阶段没有接入 QIO 模型调用，QIO 还没有真正读取或理解这些内容",
        cleared > 0 ? `提交成功后已自动取消 ${cleared} 条注释的勾选（不是删除或撤回）` : "",
      ]
        .filter(Boolean)
        .join("；");
    }
    case "failed":
      return `提交失败：${context.error || "原因未知"}；本次改动与注释勾选已保留，基准没有更新，可以直接再试一次。`;
    case "empty":
      return "没有可提交内容：只有普通移动 / 缩放，或本次没有允许查看的内容；未更新基准、未调用 QIO。";
    case "duplicate":
      return "与上次成功提交内容一致：未重复提交、未更新基准、未调用 QIO。";
    default:
      return pending > 0
        ? `尚未提交：当前有 ${pending} 项改动还没有交给 QIO。编辑与保存都不会调用 QIO。`
        : "尚未提交：编辑与保存都不会调用 QIO，点「提交」才会把本次允许查看的表达交给 QIO。";
  }
}

/** 失败时到底保留了什么（不要把失败说成「没保存」）。 */
export function submitFailureText(result: SubmissionResult | null | undefined): string {
  const error = result?.submission?.error || result?.delivery?.reason || "";
  return [
    "提交失败不会丢改动：已保存的板面、本次注释勾选都保留，上次成功提交的基准没有被更新",
    error ? `失败原因：${error}` : "",
  ]
    .filter(Boolean)
    .join("；");
}

/** 保存状态说明（保存与提交是两件事）。 */
export function saveStateText(
  status: SaveStatusLike,
  dirty: boolean,
  lastSavedAt: string | null,
): string {
  if (status === "saving") return "正在保存板面…（保存不调用 QIO）";
  if (status === "error") return "保存失败：改动还在本地，未提交，也未调用 QIO";
  if (dirty) return "有改动尚未保存（保存不调用 QIO）";
  if (lastSavedAt) return `已保存（${formatTime(lastSavedAt)}），保存不调用 QIO`;
  return "尚未保存过（保存不调用 QIO）";
}

/** 草稿与自动保存的真实时机（不承诺未保存的输入能恢复）。 */
export function draftHintText(): string {
  return [
    "文字输入停止约 0.6 秒后保存为草稿，板面操作停止约 0.45 秒后自动保存",
    "草稿不是提交内容，重新打开会恢复已保存的草稿且仍未提交",
    "意外退出只保证已经保存的内容能恢复，不承诺未保存的输入能恢复",
  ].join("；");
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
