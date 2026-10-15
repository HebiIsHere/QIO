/**
 * 提交相关的纯函数（子智能体 B 负责）。
 *
 * 契约：docs/interactive-mode-contract.md §1.4 / §1.5 / §4.4。
 *
 * 这里**只做展示用的推导**，不做权限判断：
 * 真正决定「哪些内容交给 QIO」的是服务端（从已保存状态推导，见
 * agent/interactive/submission.py）。`localVisibleRange` 只是让界面在服务端响应之前
 * 也能说清「未勾选 / 明确隐藏的不在其中」，规则与 models.visible_except_deleted 一致：
 * 文字卡要勾选（整卡一个许可，覆盖卡内文字 / 链接 / 代码）；材料（file：图片与其它文件都是文件卡的附件）默认在范围内；
 * 遗留 code / url 卡**整体不可见**（迁移完成前也不回退成可见）；reply / hidden / deleted 不在。
 */
import { CHECKABLE_KINDS } from "./board";
import type {
  AcceptanceLookup,
  BoardCard,
  BoardState,
  Expression,
  ExpressionKind,
  FrozenView,
  SubmissionResult,
  VisibleRange,
} from "./types";

export type SubmitStatusLike =
  | "idle"
  | "submitting"
  | "succeeded"
  | "failed"
  | "empty"
  | "duplicate"
  /** F10：没有拿到提交回执 —— 提交结果**未知**，不能假定成功或失败 */
  | "unknown";

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
  /** F4：可见结果卡数量（与注释分开报，不混为一谈） */
  results: number;
  materials: number;
  groups: number;
  links: number;
  notVisible: number;
}

/** 材料类卡片（契约 §2.2：code / url 不再是材料默认可见通道；2026-10-15 起材料只有 file）。 */
const MATERIAL_KINDS = ["file"];

/** 遗留的字符类卡片：迁移完成前**整体不可交付**（与服务端 LEGACY_TEXT_CARD_KINDS 一致）。 */
const LEGACY_TEXT_CARD_KINDS = ["code", "url"];

/**
 * 单张卡片是否在本次允许查看的范围内（与服务端 models.visible_except_deleted 一致）。
 *
 * 未许可的文字卡**整体**不可见：卡内文字、链接、代码与派生正文都进不去提交载荷。
 */
export function isVisibleCard(card: BoardCard): boolean {
  if (card.deleted || card.hidden) return false;
  if (LEGACY_TEXT_CARD_KINDS.includes(card.kind)) return false;
  // F4：文字注释与结果卡都要「勾选才允许 QIO 查看」（默认未勾选）；材料默认在范围内。
  if (CHECKABLE_KINDS.includes(card.kind)) return card.checked;
  return true;
}

// --- F6：客户端冻结视图（显示态桥） -----------------------------------------
//
// 与 viewport.publishBoardArea 同一个模式：只读显示态，不进板面状态、不产生表达、不排保存。
// 画布在 scale 变化 / 卸载时发布；没发布过（jsdom / 画布未挂载）时是 null —— 提交时如实
// 传 null，绝不编造一个 scale。区域来自 viewport.readBoardArea()。

let liveScale: number | null = null;

export function publishBoardScale(scale: number | null): void {
  liveScale = typeof scale === "number" && Number.isFinite(scale) && scale > 0 ? scale : null;
}

export function readBoardScale(): number | null {
  return liveScale;
}

/**
 * 组装提交用的**冻结视图**（F6）：板面坐标下的可见区域 + 当前缩放。
 * 任何一项量不到（null / 非有限数 / 面积或缩放不为正）→ 整体返回 null（不编造）。
 */
export function frozenViewPayload(
  area: { x: number; y: number; w: number; h: number } | null | undefined,
  scale: number | null | undefined,
): FrozenView | null {
  if (!area || typeof scale !== "number" || !Number.isFinite(scale) || scale <= 0) return null;
  if (![area.x, area.y, area.w, area.h].every((value) => Number.isFinite(value))) return null;
  if (!(area.w > 0) || !(area.h > 0)) return null;
  return { x: area.x, y: area.y, w: area.w, h: area.h, scale };
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
    results: cards.filter((card) => card.kind === "reply").length,
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
  if (counts.results > 0) parts.push(`结果卡 ${counts.results} 张`);
  if (counts.groups > 0) parts.push(`组 ${counts.groups} 个`);
  if (counts.links > 0) parts.push(`关系 ${counts.links} 条`);
  const tail =
    counts.notVisible > 0
      ? `另有 ${counts.notVisible} 张卡片不在其中（未勾选的注释与结果卡、明确隐藏的卡片都不会被查看）。`
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
  // F4：可见结果卡的新增 / 正文变化
  result_added: "新增可见结果卡",
  result_updated: "可见结果卡内容变化",
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
    parts.push(`其中 ${nonIntent.length} 项只记录变化（材料增删、撤回、结果卡变化等）`);
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
  if (status === "unknown") return "提交结果未知";
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
  /**
   * 内容保留情况：由 {@link submitFailureBrief} 按**当前事实**给出。
   * 传了就照它说，不传才退回固定说法（板面保存也失败时固定说法会不准确）。
   */
  retention?: string;
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
      const delivery = context.result?.delivery;
      // 受理与理解分开（契约 §14.1）：delivered=true 只表示「已可靠受理」，
      // 不等于模型已经理解完成；这里必须如实说明下一步，不能假装已经懂。
      const understanding =
        delivery?.delivered === true
          ? `已受理（${delivery.status ?? "accepted"}）：QIO 正在读取本次材料并真实理解；` +
            "模型失败会记在理解记录上（可从保存的快照重试），不影响本次提交"
          : delivery?.reason ||
            "本次表达已记录，但没有可靠受理：改动与勾选已保留，可以直接再试一次";
      return [
        `已提交：本次 ${count} 项改动已记录`,
        understanding,
        cleared > 0 ? `提交成功后已自动取消 ${cleared} 条注释的勾选（不是删除或撤回）` : "",
      ]
        .filter(Boolean)
        .join("；");
    }
    case "failed":
      return `提交失败：${context.error || "原因未知"}；${
        context.retention || "本次改动与注释勾选已保留，基准没有更新，可以直接再试一次。"
      }`;
    case "unknown":
      /**
       * F10：没有回执 ≠ 失败。服务端可能已经受理，所以既不宣称成功、也不诱导重复提交。
       */
      return `提交结果未知：${context.error || "没有拿到服务端回执"}；这次提交可能已经受理，改动与勾选都保留，请先确认结果再决定是否重试。`;
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

/** 结果里带的诊断文字（服务端返回的失败说明或投递原因） */
function resultDiagnostic(result?: SubmissionResult | null): string {
  return String(result?.submission?.error || result?.delivery?.reason || "").trim();
}

/**
 * 提交失败默认区要说的三件事（契约 §11.6）。
 *
 * 分层：{@link reason} 与 {@link retention} 是**默认就要看见**的（不必展开详情）；
 * {@link detail} 是较长诊断，放进详情区，但不是了解基本失败原因的必经入口。
 */
export interface SubmitFailureBrief {
  /** 本次请求的简短真实原因（不是上一次提交结果里的原因） */
  reason: string;
  /** 内容保留情况：单独一句，按当前事实说（板面保存也失败时不许说「已保存」） */
  retention: string;
  /** 下一步：直接说明点哪个按钮、会发生什么 */
  nextStep: string;
  /** 较长诊断（详情区）：保存失败原因、上一次提交返回的说明、范围与改动列表的指引 */
  detail: string;
}

export interface SubmitFailureContext {
  /** 本次请求的状态；只有 failed 才给失败说明 */
  status: SubmitStatusLike;
  /** 本次请求的真实原因（store.submitError） */
  error?: string | null;
  /** 当前板面保存状态与原因：用来准确说明保留情况 */
  saveStatus?: SaveStatusLike;
  saveError?: string | null;
  dirty?: boolean;
  /**
   * 上一次提交结果：**只**用于详情里的排查信息。
   *
   * 唯一的例外是「服务端直接返回失败结果」这条路：那时异常路径没有设置本次原因，
   * 这份结果本身就是本次的事实（status 也是 failed），所以可以当原因用。
   */
  lastResult?: SubmissionResult | null;
}

/**
 * 提交失败的默认说明（契约 §11.6 / §11.8）。
 *
 * 取原因的规则（顺序固定、可被单测证明）：
 * 1. 本次请求自己的原因优先（store.submitError）—— 上一次提交结果里的原因**绝不能**冒充本次；
 * 2. 本次没有原因、而最后一次提交结果本身就是失败结果时，用这份结果的说明（它就是本次的事实）；
 * 3. 都拿不到就如实说「没有拿到原因」，不编造。
 */
export function submitFailureBrief(context: SubmitFailureContext): SubmitFailureBrief | null {
  if (context?.status !== "failed") return null;
  const current = String(context.error ?? "").trim();
  const diagnostic = resultDiagnostic(context.lastResult);
  const fromResult = !current && context.lastResult?.status === "failed";
  const reason = current || (fromResult ? diagnostic : "") || "本次没有拿到失败原因";

  const saveFailed = context.saveStatus === "error";
  const savePending = !saveFailed && (context.saveStatus === "saving" || context.dirty === true);
  const retention = saveFailed
    ? "板面改动也保存失败了：内容还在这一页，还没有存到服务器；本次注释勾选保留。"
    : savePending
      ? "板面还有改动没有保存到服务器；本次注释勾选保留，上次成功提交的基准没有更新。"
      : "已保存的板面与本次注释勾选都保留，上次成功提交的基准没有更新。";
  const nextStep = saveFailed
    ? "可以直接点「重新提交」再试一次：会先重新保存板面，再提交。"
    : "可以直接点「重新提交」再试一次。";

  const saveError = String(context.saveError ?? "").trim();
  // 详情里先给「不会丢改动」那句较长诊断（只说保留情况，原因已经写在默认区，不重复、也不误标来源）
  const keepSentence = submitFailureText(null, saveFailed);
  const detail = [
    keepSentence,
    saveFailed && saveError ? `保存失败原因：${saveError}` : "",
    !fromResult && diagnostic ? `上一次提交返回的说明（供排查）：${diagnostic}` : "",
    "本次允许查看的范围与改动列表在下面的详情里；重试会先重新保存板面，再重新提交。",
  ]
    .filter(Boolean)
    .join("；");

  return { reason, retention, nextStep, detail };
}

/**
 * 失败时到底保留了什么（较长诊断句，详情区用；不要把失败说成「没保存」）。
 *
 * saveFailed = true（板面保存也失败）时不再声称「已保存的板面都保留」。
 */
export function submitFailureText(result: SubmissionResult | null | undefined, saveFailed = false): string {
  const error = resultDiagnostic(result);
  const keep = saveFailed
    ? "提交失败不会丢改动：改动还在这台机器的当前页面里，但板面还没有保存到服务器，本次注释勾选保留"
    : "提交失败不会丢改动：已保存的板面、本次注释勾选都保留，上次成功提交的基准没有被更新";
  return [keep, error ? `失败原因：${error}` : ""].filter(Boolean).join("；");
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

/* ------------------------------------------------------------------------
 * 契约 §1.3：稳定提交请求身份（纯函数，不做 IO、不碰 store）
 *
 * 一次提交请求在**发出之前**就有身份，服务端按 (boardId, clientRequestId) 做幂等：
 * 同身份重试返回原事实，同身份但内容不同按冲突拒绝。前端在这里只负责
 * 「生成 / 校验 / 归属判定 / 本机台账的序列化」，持久化由 store 调用 localStorage 完成。
 *
 * 身份**不是**受理证据：拿它去查证（`fetchAcceptance`）命中的记录才是证据（§1.1.4）。
 * ---------------------------------------------------------------------- */

/** 请求身份前缀：可读、可核对，并与服务端其它 id 明确区分。 */
export const SUBMISSION_REQUEST_ID_PREFIX = "creq_";

/** 本机台账的 localStorage 键前缀（每个板面一条未决身份）。 */
export const SUBMISSION_IDENTITY_STORAGE_PREFIX = "qio.interactive.pending-submission.";

/**
 * 这一次**未决**提交的稳定身份。
 *
 * `baseStateVersion` / `note` 是这次提交的内容依据：同身份但这两项不同，
 * 服务端会按冲突拒绝 —— 所以前端只在两项都没变时才复用同一个身份。
 */
export interface SubmissionIdentity {
  clientRequestId: string;
  boardId: string;
  /** 这次提交基于的服务器版本（拿不到时为 null） */
  baseStateVersion: number | null;
  /** 这次提交的说明（提交载荷里的 note） */
  note: string;
  /** 本机**生成身份**的时间：只用于展示与本地排序，**不得**作为受理证据 */
  createdAt: number;
}

const REQUEST_ID_RE = /^creq_[A-Za-z0-9_-]{8,}$/;

function randomSuffix(): string {
  const cryptoLike = (globalThis as { crypto?: { getRandomValues?: (array: Uint8Array) => Uint8Array } })
    .crypto;
  const bytes = new Uint8Array(12);
  if (typeof cryptoLike?.getRandomValues === "function") {
    cryptoLike.getRandomValues(bytes);
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }
  return (Math.random().toString(16).slice(2) + Date.now().toString(16)).padEnd(24, "0").slice(0, 24);
}

/** 生成一个新的请求身份（发出提交**之前**调用）。 */
export function newSubmissionRequestId(): string {
  return `${SUBMISSION_REQUEST_ID_PREFIX}${Date.now().toString(36)}_${randomSuffix()}`;
}

/** 这个值能不能作为请求身份送去服务端（空字符串 = 旧客户端，不参与幂等）。 */
export function isUsableSubmissionRequestId(value: unknown): value is string {
  return typeof value === "string" && REQUEST_ID_RE.test(value.trim());
}

/**
 * 这一次提交该用哪个身份：**内容没变就沿用**，变了（或没有）就新建。
 *
 * 「内容」= 板面 id + 基准版本 + 说明。沿用同一个身份是安全重试的前提：
 * 服务端会返回原事实，不会重复创建提交与理解请求（§1.1.2）。
 */
export function submissionIdentityFor(options: {
  boardId: string;
  baseStateVersion?: number | null;
  note?: string;
  existing?: SubmissionIdentity | null;
  now?: number;
  makeId?: () => string;
}): SubmissionIdentity {
  const existing = options.existing ?? null;
  const wanted = {
    boardId: options.boardId,
    baseStateVersion: options.baseStateVersion ?? null,
    note: options.note ?? "",
  };
  if (existing && isUsableSubmissionRequestId(existing.clientRequestId) && submissionIdentityMatches(existing, wanted)) {
    return existing;
  }
  return {
    clientRequestId: (options.makeId ?? newSubmissionRequestId)(),
    boardId: wanted.boardId,
    baseStateVersion: wanted.baseStateVersion,
    note: wanted.note,
    createdAt: options.now ?? Date.now(),
  };
}

/** 身份是不是属于这一份内容（板面 / 基准版本 / 说明都一致）。 */
export function submissionIdentityMatches(
  identity: SubmissionIdentity | null | undefined,
  other: { boardId?: string; baseStateVersion?: number | null; note?: string },
): boolean {
  if (!identity) return false;
  if (typeof other.boardId === "string" && identity.boardId !== other.boardId) return false;
  if (other.baseStateVersion !== undefined) {
    const wanted = other.baseStateVersion ?? null;
    if ((identity.baseStateVersion ?? null) !== wanted) return false;
  }
  if (typeof other.note === "string" && identity.note !== other.note) return false;
  return true;
}

/** 这个身份能不能拿去查证（必须有可用的请求身份；没有身份就不许推断受理）。 */
export function canLookupAcceptance(identity: SubmissionIdentity | null | undefined): boolean {
  return Boolean(identity && identity.boardId && isUsableSubmissionRequestId(identity.clientRequestId));
}

/**
 * 查证结果是不是**这一次**提交的（契约 §1.1.3 / §1.1.4）：
 * 只认精确身份；服务端同时给出内容依据（版本 / 说明）时必须与这次请求一致。
 * 任何一项不符 = 不是这次提交的事实，界面不得据此认领受理。
 */
export function acceptanceMatchesIdentity(
  identity: SubmissionIdentity | null | undefined,
  acceptance: AcceptanceLookup | null | undefined,
): acceptance is AcceptanceLookup {
  if (!canLookupAcceptance(identity) || !acceptance) return false;
  if (!isUsableSubmissionRequestId(acceptance.clientRequestId)) return false;
  if (acceptance.clientRequestId !== identity!.clientRequestId) return false;
  if (typeof acceptance.submissionId !== "string" || acceptance.submissionId.length === 0) return false;
  if (
    typeof acceptance.baseStateVersion === "number" &&
    identity!.baseStateVersion !== null &&
    acceptance.baseStateVersion !== identity!.baseStateVersion
  ) {
    return false;
  }
  if (typeof acceptance.note === "string" && acceptance.note !== identity!.note) return false;
  return true;
}

export function serializeSubmissionIdentity(identity: SubmissionIdentity): string {
  return JSON.stringify({ v: 1, ...identity });
}

/** 读回本机台账：形状不合法一律当没有（不许拿半条记录去当身份）。 */
export function parseStoredSubmissionIdentity(raw: string | null | undefined): SubmissionIdentity | null {
  if (typeof raw !== "string" || raw.length === 0) return null;
  let box: Partial<SubmissionIdentity> | null = null;
  try {
    box = JSON.parse(raw) as Partial<SubmissionIdentity>;
  } catch {
    return null;
  }
  if (!box || typeof box !== "object") return null;
  if (!isUsableSubmissionRequestId(box.clientRequestId)) return null;
  if (typeof box.boardId !== "string" || box.boardId.length === 0) return null;
  const version = typeof box.baseStateVersion === "number" && Number.isFinite(box.baseStateVersion)
    ? box.baseStateVersion
    : null;
  return {
    clientRequestId: box.clientRequestId,
    boardId: box.boardId,
    baseStateVersion: version,
    note: typeof box.note === "string" ? box.note : "",
    createdAt: typeof box.createdAt === "number" && Number.isFinite(box.createdAt) ? box.createdAt : 0,
  };
}

export function submissionIdentityStorageKey(boardId: string): string {
  return SUBMISSION_IDENTITY_STORAGE_PREFIX + String(boardId || "default");
}

/** 服务端的「同身份但内容不同」冲突（409 request_identity_conflict）。 */
export const REQUEST_IDENTITY_CONFLICT = "request_identity_conflict";

/** 这次失败是不是「同身份但内容不同」的服务端冲突（必须按冲突说明，不许认领受理）。 */
export function isRequestIdentityConflict(error: unknown): boolean {
  const status = (error as { status?: unknown } | null)?.status;
  const payload = (error as { payload?: { error?: unknown } | null } | null)?.payload;
  return status === 409 && payload?.error === REQUEST_IDENTITY_CONFLICT;
}

/**
 * 身份冲突的如实说明：服务端确实拒绝过，就要说清「没有创建提交、没有更新基准、
 * 没有调用模型」，并且下一次提交会换新身份（不能拿被拒的身份继续认领什么）。
 */
export function identityConflictText(): string {
  return (
    "服务端按同一个请求身份找到了内容不同的记录，已按冲突拒绝这次提交：" +
    "本次没有创建提交、没有更新基准、也没有调用模型。改动与勾选都保留；" +
    "下一次提交会使用新的请求身份。"
  );
}

/**
 * 受理情况不明时，界面要如实说明「查证到了什么」：
 * `confirmed` 为 false 时**必须**继续「结果未知」，不许改判失败，也不许诱导重复提交。
 */
export function acceptanceLookupText(options: {
  confirmed: boolean;
  reason?: string | null;
  replayed?: boolean;
}): string {
  if (options.confirmed) {
    const replayed = options.replayed
      ? "，且服务端返回的是原记录（没有重复创建提交与理解请求）"
      : "";
    return (
      "这次提交的回执在传输中丢了：已按**同一个请求身份**查证，服务端确认它已被可靠受理" +
      replayed +
      "，正在理解。不会重复提交。"
    );
  }
  const why = String(options.reason ?? "").trim() || "没有查到这条请求身份的记录";
  return (
    "已按同一个请求身份向服务端查证：" +
    why +
    "。结果仍然未知：这次提交可能已经受理，也不排除没有到达服务端；" +
    "改动与勾选都保留，不会自动重复提交。"
  );
}
