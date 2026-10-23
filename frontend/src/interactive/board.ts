/**
 * 板面操作语义（纯函数）：卡片、选择、拖动、分组、顺序、关系链接。
 *
 * 契约：docs/interactive-mode-contract.md §1.2 / §1.3 / §4.2，默认组名以 §9.1 为准
 * （覆盖 §1.3 / §6.1 的「组 N」写法）。
 * 与后端 backend/src/agent/interactive/board.py **语义一致**：同一批规则、同一批默认值；
 * 两边改动必须同步，否则前端的预演会和服务端归一化结果对不上。
 *
 * 三条容易搞错的语义（与后端一致）：
 * 1. 位置只影响显示：普通移动 / 缩放不构成意图依据；组框位置由成员推导。
 * 2. 方向不解释成因果：link.direction 只是用户写明的方向，meaning 是用户写的原话。
 * 3. 拖动期间只预演（previewDrop 不改状态），放下才完成（dropCard）。
 *
 * 预演字段：groupId = 将落入的**已存在**的组（与未分组卡片成组时组还不存在 → null）；
 * index = 在该组里的插入位置；mergesWith = 将被合并进来的对象 id
 * （两个已有组合并时是被并入的那一组 id，与未分组卡片重叠成组时是那张卡片 id）。
 */
import type {
  BoardCard,
  BoardGroup,
  BoardLink,
  BoardState,
  CardKind,
  Participation,
  VisibleRange,
} from "./types";
import { cardDocFields, emptyDoc, parseDocText, type TextDoc } from "./textCard";
import {
  GROUP_ENDPOINT_PREFIX,
  parseEndpoint,
  resolveEndpoint,
  sameEndpoint,
} from "./linkEndpoints";
import { readImageDisplay } from "./renderers/imageDisplay";

/** 位置与大小的矩形（板面坐标）。 */
export interface BoardRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** 拖动预演结果（契约 §3 / §4.2）。 */
export interface DropPreview {
  groupId: string | null;
  index: number | null;
  mergesWith: string | null;
}

/** 放下结果（契约 §3 / §4.2）。 */
export interface DropResult {
  state: BoardState;
  groupId: string | null;
  merged: boolean;
  index: number | null;
}

/** 组件展示用：卡片类别文字（状态不能只靠颜色，必须有文字）。 */
export const CARD_KIND_LABELS: Record<CardKind, string> = {
  text: "文字注释",
  file: "文件",
  code: "代码",
  url: "网址",
  reply: "QIO 结果",
};

/**
 * 材料类卡片：添加材料本身不等于要求总结 / 比较 / 修改 / 执行。
 *
 * 契约（2026-10-15 图片展示简化）：**材料只有 file** —— 图片与其它文件都是文件卡的附件，
 * 不再有独立的图片卡类型。历史上 code / url 的「材料默认可见」通道取消 ——
 * 卡内文字、链接与代码一律走统一文字卡的**整卡许可**。
 */
export const MATERIAL_KINDS: CardKind[] = ["file"];

/**
 * 用户能从工具栏添加的卡片：**文字卡 / 文件**（契约 2026-10-12 §1.7；图片是文件卡的附件）。
 *
 * 代码与网址不再是入口：它们与正文写在**同一张文字卡**里（卡内代码块可编辑）。
 * CARD_KINDS 仍保留全部旧取值用于读历史数据与导入文件。
 */
export const ADDABLE_CARD_KINDS: CardKind[] = ["text", "file"];

/**
 * 新卡片的默认内容与元信息（同一份默认值，避免工具栏与画布各写一份而漂移）。
 * 默认内容只是占位文字，不代表任何意图；卡片添加后由用户填写。
 *
 * 文字卡（契约 §1.4）：默认**空块序列 + 空编辑区文本**；
 * 而 `addCard` 的默认 `checked = false` —— 新建的文字卡默认未许可。
 */
export function defaultCardSeed(kind: CardKind): { kind: CardKind; content: string; meta: Record<string, unknown> } {
  switch (kind) {
    case "text":
      return { kind, content: "", meta: { doc: emptyDoc(), docText: "" } };
    case "file":
      return { kind, content: "待补充文件说明", meta: { name: "未命名文件" } };
    case "code":
      return { kind, content: "// 待补充代码", meta: { language: "text" } };
    case "url":
      return { kind, content: "", meta: { href: "https://", title: "待补充标题" } };
    case "reply":
      return { kind, content: "", meta: {} };
    default:
      return { kind, content: "", meta: {} };
  }
}

/**
 * 需要勾选才允许 QIO 查看的卡片：文字注释与 QIO 结果卡（材料默认在范围内）。
 *
 * F4：结果卡（reply）与文字卡同一套许可语义 —— 默认**未**许可（QIO 自己生成 ≠ 获得许可），
 * 用户勾选后持续有效；隐藏 / 删除 / 取消勾选立即退出范围。
 */
export const CHECKABLE_KINDS: CardKind[] = ["text", "reply"];

/** 参与讨论的唯一状态；选中/多选/框选不改变它。 */
export type { Participation };

/**
 * 遗留的 code / url 卡：迁移后仍**整体不可交付**（契约 §5.1），
 * 参与状态永远 excluded —— 显式传入 included 也不放行（否则本地预览与后端会分叉）。
 */
export const LEGACY_UNSHIPPABLE_KINDS: CardKind[] = ["code", "url"];

/**
 * 由旧 code / url 迁移而来的文字卡（契约 §5.1：旧 code/url → excluded）。
 * 判定依据与后端 models._legacy_participation 同一口径：带 language 或 url 块。
 */
export function isMigratedLegacyText(card: { kind: CardKind; meta?: Record<string, unknown> | null }): boolean {
  if (card.kind !== "text") return false;
  const meta = card.meta ?? {};
  if (typeof meta.language === "string" && meta.language.length > 0) return true;
  const url = meta.url;
  return Boolean(url && typeof url === "object");
}

/**
 * 一张卡当前的参与状态（唯一事实源；与后端 models.card_participation 同一口径）。
 * - hidden = 明确退出范围，**优先级最高**（即使显式写了 included，也绝不放行）；
 * - 遗留 code / url：永远 excluded（不可交付）；
 * - 新版显式 participation 优先；
 * - 旧数据按一次性迁移规则推导：file → included；text / reply 保留旧 checked 意图；
 *   旧 code / url 迁移来的文字卡 → excluded；其余 → excluded。
 */
export function participationOf(card: {
  participation?: Participation;
  hidden: boolean;
  checked: boolean;
  kind: CardKind;
  meta?: Record<string, unknown> | null;
}): Participation {
  if (card.hidden) return "excluded";
  if (LEGACY_UNSHIPPABLE_KINDS.includes(card.kind)) return "excluded";
  if (card.participation === "included" || card.participation === "excluded") return card.participation;
  if (isMigratedLegacyText(card)) return "excluded";
  if (card.kind === "file") return "included";
  if (card.kind === "text" || card.kind === "reply") return card.checked ? "included" : "excluded";
  return "excluded";
}

/** 是否允许 QIO 读取这张卡（参与状态；与选中/书签/阅读状态正交）。 */
export function isParticipating(card: Pick<BoardCard, "participation" | "hidden" | "checked" | "kind">): boolean {
  return participationOf(card) === "included";
}

/**
 * 把旧兼容字段（checked / hidden）同步成与 participation **同一口径**：
 * - 参与 → hidden=false；文字注释 / 结果卡 checked=true（旧服务端仍读这两个字段）；
 * - 不参与 → 文字注释 / 结果卡 checked=false；材料（file）hidden=true
 *   （旧规则里材料默认可见，只有 hidden 能让它退出范围）。
 * 遗留 code / url 不动：它们无论如何都不可交付。
 * 返回是否真的改了字段（调用方据此决定 changed）。
 */
export function syncLegacyParticipation(card: BoardCard): boolean {
  if (LEGACY_UNSHIPPABLE_KINDS.includes(card.kind) || isMigratedLegacyText(card)) return false;
  const included = isParticipating(card);
  let changed = false;
  if (included) {
    if (card.hidden) {
      card.hidden = false;
      changed = true;
    }
    if ((card.kind === "text" || card.kind === "reply") && !card.checked) {
      card.checked = true;
      changed = true;
    }
    return changed;
  }
  if (card.kind === "text" || card.kind === "reply") {
    if (card.checked) {
      card.checked = false;
      changed = true;
    }
  } else if (card.kind === "file" && !card.hidden) {
    card.hidden = true;
    changed = true;
  }
  return changed;
}

/**
 * 一次性迁移标记（契约 §5.1）：participationVersion=1 表示已按 v2 规则迁移。
 * 归一化本身总会推导出 participation 并同步旧字段；这里只负责补标记。
 * 不做任何版本自增（迁移是显示/兼容口径，不是材料语义变化）。
 */
export function migrateParticipation(state: BoardState): BoardState {
  const work = normalizeState(state);
  const version = Number(work.participationVersion);
  if (Number.isFinite(version) && version >= 1) return work;
  return { ...work, participationVersion: 1 };
}

/**
 * 只应用参与口径（补 participation、同步旧镜像、补迁移标记）：
 * **不补时间戳、不做其它归一化**。
 *
 * 服务器事实在进入合并三路比较之前必须用它对齐 —— normalizeState 会给缺
 * updatedAt 的卡片补当前时间，等于凭空造出一次「无关改动」，让 F07 的删除/解散
 * 合并误判成冲突（既有回归用例抓到过）。
 */
export function applyParticipation(state: BoardState): BoardState {
  const source = (state ?? {}) as BoardState;
  const list = (source.cards as BoardCard[] | undefined) ?? [];
  let changed = false;
  const cards = list.map((card) => {
    const participation = participationOf(card);
    const explicit = card.participation === "included" || card.participation === "excluded";
    let next: BoardCard | null = explicit && card.participation === participation ? null : { ...card, participation };
    /*
      旧兼容镜像只在没有显式 participation 的卡片上推导（显式字段是权威，checked/hidden 保持原样）。
    */
    if (!explicit) {
      const probe = next ?? card;
      if (!LEGACY_UNSHIPPABLE_KINDS.includes(probe.kind)) {
        const mirror = { ...probe, participation };
        if (syncLegacyParticipation(mirror)) next = mirror;
      }
    }
    if (!next) return card;
    changed = true;
    return next;
  });
  const marker = Number(source.participationVersion);
  const missingMarker = !(Number.isFinite(marker) && marker >= 1);
  if (!changed && !missingMarker) return source;
  return {
    ...source,
    cards,
    ...(missingMarker ? { participationVersion: 1 } : {}),
  };
}

/** 显式设置单卡参与状态；旧 checked/hidden 同步成同一口径（兼容旧服务端）。 */
export function setParticipation(state: BoardState, cardId: string, included: boolean): BoardState {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card || card.deleted) return work;
  if (LEGACY_UNSHIPPABLE_KINDS.includes(card.kind)) return work;
  const participation: Participation = included ? "included" : "excluded";
  if (participationOf(card) === participation && card.participation === participation) return work;
  card.participation = participation;
  /*
    顺序很重要：hidden 在参与判定里优先级最高（与后端一致），
    所以「重新参与」必须先把 hidden 清掉，否则 syncLegacyParticipation 读到的仍是 excluded，
    材料卡会停在被隐藏的状态里（participation 与镜像分叉）。
  */
  if (participation === "included") card.hidden = false;
  syncLegacyParticipation(card);
  card.updatedAt = nowIso();
  work.updatedAt = nowIso();
  return normalizeState(work);
}

/** 组级参与：全部参与 / 全部不参与；组本身没有独立 participation。 */
export function setGroupParticipation(state: BoardState, groupId: string, included: boolean): BoardState {
  const work = normalizeState(state);
  const group = work.groups.find((item) => item.id === groupId && !item.deleted);
  if (!group) return work;
  let next = work;
  for (const cardId of group.members) next = setParticipation(next, cardId, included);
  return next;
}

/**
 * 本地「本次允许 QIO 查看的范围」（Phase 2 §5.1 唯一规则的前端实现）：
 * - 卡片按 participation（不再看 checked/hidden 的推导值，但归一化已把两者同步）；
 * - 组：至少一名参与成员才出现，只列出参与成员；
 * - 连线：两端都在范围内才出现；组端点要求该组有参与成员。
 *
 * 权威结果仍以服务端 visible_range 为准；这里只用于提交前预览 / 范围说明。
 */
export function participationVisibleRange(state: BoardState | null | undefined): VisibleRange {
  const cards = state?.cards ?? [];
  const live = cards.filter((card) => !card.deleted);
  const visible = live.filter(isParticipating);
  const visibleIds = new Set(visible.map((card) => card.id));
  const groups = (state?.groups ?? [])
    .filter((group) => !group.deleted)
    .map((group) => ({ ...group, members: group.members.filter((id) => visibleIds.has(id)) }))
    .filter((group) => group.members.length > 0);
  const groupsById = new Map(groups.map((group) => [group.id, group]));
  const endpointInRange = (raw: string): boolean => {
    const parsed = parseEndpoint(raw);
    if (!parsed.ok) return false;
    if (parsed.endpoint.kind === "card") return visibleIds.has(parsed.endpoint.id);
    const group = groupsById.get(parsed.endpoint.id);
    return Boolean(group && group.members.length > 0);
  };
  const links = (state?.links ?? []).filter(
    (link) => !link.deleted && endpointInRange(link.src) && endpointInRange(link.dst),
  );
  const selection = (state?.selection ?? []).filter((id) => visibleIds.has(id));
  return {
    cards: visible,
    groups,
    links,
    selection,
    empty: visible.length === 0 && groups.length === 0 && links.length === 0,
    notVisibleCount: Math.max(0, live.length - visible.length),
  };
}

/** 组的参与状态派生：全参与 / 全不参与 / 部分参与。 */
export function groupParticipation(state: BoardState, groupId: string): "none" | "all" | "partial" {
  const group = state.groups.find((item) => item.id === groupId && !item.deleted);
  if (!group || !group.members.length) return "none";
  const values = group.members
    .map((id) => cardById(state, id))
    .filter((card): card is BoardCard => Boolean(card) && card!.deleted === false)
    .map((card) => isParticipating(card));
  if (!values.length) return "none";
  if (values.every(Boolean)) return "all";
  if (values.every((value) => !value)) return "none";
  return "partial";
}

//: 组框相对成员的留白（顶部留两行：组名一行 + 序号 / 成员一行，序号条不压住卡片）。
//: 位置只影响显示，不是意图依据。
const GROUP_PAD_X = 16;
const GROUP_PAD_TOP = 58;
const GROUP_PAD_BOTTOM = 16;

const DEFAULT_CARD_W = 260;
const DEFAULT_CARD_H = 170;
//: 新卡片默认按网格摆放：不互相压住（压住会让「点这张卡片」点到上面那张）
const DEFAULT_CARD_GAP = 24;
const DEFAULT_CARD_ORIGIN = 60;
const DEFAULT_CARD_COLUMNS = 4;
//: 复制卡片的偏移（故意叠一点，表示这是副本）
const DUPLICATE_OFFSET = 32;

/**
 * 系统默认组名（契约 §9.1，覆盖 §1.3 / §6.1 的「组 N」）：
 * 需要系统给名字的所有路径统一用它。它只表示「这个名字是系统给的」，
 * 不替用户表达任何关系判断，也不承诺名字唯一。
 */
export const DEFAULT_GROUP_NAME = "默认组名";

/** 兼容名：默认组名的判定前缀就是它本身（组件拼提示文字时用）。 */
export const DEFAULT_GROUP_NAME_PREFIX = DEFAULT_GROUP_NAME;

/** 历史默认名「组 N」的前缀：只用于**识别**旧数据，不再用于生成新名字。 */
export const LEGACY_GROUP_NAME_PREFIX = "组";

//: 默认名判定同时认两种形态：新的「默认组名」与历史数据里的「组 N」（契约 §9.1）。
const DEFAULT_NAME_RE = /^(?:默认组名|组\s*\d+)$/;

let idCounter = 0;

function newId(prefix: string): string {
  idCounter += 1;
  const random = Math.random().toString(36).slice(2, 6);
  return prefix + "_" + Date.now().toString(36) + idCounter.toString(36) + random;
}

function nowIso(): string {
  return new Date().toISOString();
}

function asFloat(value: unknown, fallback: number): number {
  const result = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(result)) return fallback;
  return result;
}

/**
 * 画布（板面坐标系）尺寸 —— **板面坐标的唯一事实源**。
 * BoardCanvas 用同一对常量渲染 surface 与滚动范围；卡片坐标也必须夹在这对边界内：
 * 负坐标或超出画布的位置**永远看不见**（滚动范围被 clampScroll 限制在 [0, 内容 − 视口]，
 * 画布原点之外的内容既滚不到、也拖不回来）。
 * 2026-10-14 第七个问题：拖到板面顶边之外会写出 y = −63.84 这样的坐标，
 * 卡片顶部被页眉遮住的那一块再也回不来。
 */
export const BOARD_SURFACE_W = 2400;
export const BOARD_SURFACE_H = 1600;

/**
 * 画布尺寸的**上限哨兵**（2026-10-14 起画布随内容增长，见 BoardCanvas 的 surfaceSize）。
 * 这里不再夹到 2400×1600 —— 只挡明显离谱的数值（浮点异常 / 外部数据），
 * 保证坐标始终是有限数：**画布会长大，坐标不会失控**。
 */
export const BOARD_COORD_LIMIT = 1_000_000;

/**
 * 把卡片矩形夹进画布范围：尺寸先收敛到画布内，再把左上角夹到 [0, 画布 − 尺寸]。
 * 所有写入路径（新建 / 拖动落下 / updateCard）都走这里，夹取规则只有一份。
 */
export function clampCardRect(rect: { x: number; y: number; w: number; h: number }): {
  x: number;
  y: number;
  w: number;
  h: number;
} {
  const w = Math.max(1, Math.min(BOARD_COORD_LIMIT, asFloat(rect.w, 1)));
  const h = Math.max(1, Math.min(BOARD_COORD_LIMIT, asFloat(rect.h, 1)));
  /*
    2026-10-14（用户要求「板面无限」，方案 B）：
    右上不再有边界 —— 画布随内容增长（BoardCanvas 的 surfaceSize），卡片可以拖到任意远。
    左边与上边仍以 0 为界：**坐标原点不漂**（否则卡片的 x/y 会变负数，
    渲染、连线、组框、工具栏换算全都要跟着改一遍，代价远大于收益）。
  */
  /*
    2026-10-14（方案 B 第二步）：左右上下都不再有边界 —— 坐标允许为负，
    由 rebaseToFit 在落下之后把整块板面整体平移回 ≥0 的区域（原点重定位）。
    这里只挡离谱值，保证坐标始终是有限数。
  */
  return {
    w,
    h,
    x: Math.max(-BOARD_COORD_LIMIT, Math.min(BOARD_COORD_LIMIT - w, asFloat(rect.x, 0))),
    y: Math.max(-BOARD_COORD_LIMIT, Math.min(BOARD_COORD_LIMIT - h, asFloat(rect.y, 0))),
  };
}

/**
 * **只**把卡片矩形夹进画布范围：不动任何其它字段、不补时间戳。
 *
 * 为什么不复用 normalizeState：它会给缺 updatedAt 的卡片补当前时间，等于凭空造出一次
 * 「无关改动」—— 服务器事实比对（F07 的删除 / 解散合并）按时间戳判断改了哪些卡片，
 * 被污染的归一化会让它误判成冲突（全量测试抓到过）。
 */
export function clampBoardBounds(state: BoardState): BoardState {
  const source = (state ?? {}) as BoardState;
  const list = (source.cards as BoardCard[] | undefined) ?? [];
  let changed = false;
  const cards = list.map((card) => {
    const bounded = clampCardRect({ x: card.x, y: card.y, w: card.w, h: card.h });
    if (bounded.x === card.x && bounded.y === card.y && bounded.w === card.w && bounded.h === card.h) {
      return card;
    }
    changed = true;
    return { ...card, x: bounded.x, y: bounded.y, w: bounded.w, h: bounded.h };
  });
  return changed ? { ...source, cards } : source;
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function dedupe(values: readonly unknown[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const key = String(value);
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(key);
  }
  return result;
}

/**
 * 这个名字是不是系统默认名：同时认「默认组名」与历史「组 N」（契约 §9.1）。
 * 只表示「系统给的名字」，不代表任何关系含义。
 */
export function isDefaultGroupName(name: string): boolean {
  return DEFAULT_NAME_RE.test(String(name ?? "").trim());
}

/**
 * 系统默认组名：**固定返回「默认组名」**，不再编号。
 *
 * 契约 §9.1：组身份按 id 区分，不依赖名称唯一性，默认名可以重复；
 * 因此不再按已有名字生成「组 N」。`_taken` 只保留参数位，
 * 避免调用方（组件 / 用例）改动，且不影响任何行为。
 */
export function nextDefaultGroupName(_taken: Iterable<string> = []): string {
  return DEFAULT_GROUP_NAME;
}

function clampIndex(index: number | null | undefined, size: number): number {
  if (index === null || index === undefined) return size;
  const value = Math.trunc(Number(index));
  if (!Number.isFinite(value)) return size;
  return Math.max(0, Math.min(size, value));
}

/** 两个矩形是否有面积重叠（只碰到边不算重叠）。 */
export function rectsOverlap(a: BoardRect, b: BoardRect): boolean {
  return (
    Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x) > 0 &&
    Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y) > 0
  );
}

export function pointInRect(x: number, y: number, rect: BoardRect): boolean {
  return rect.x <= x && x <= rect.x + rect.w && rect.y <= y && y <= rect.y + rect.h;
}

export function cardRect(card: BoardCard): BoardRect {
  return {
    x: asFloat(card.x, 0),
    y: asFloat(card.y, 0),
    w: Math.max(1, asFloat(card.w, DEFAULT_CARD_W)),
    h: Math.max(1, asFloat(card.h, DEFAULT_CARD_H)),
  };
}

export function groupFrame(group: BoardGroup): BoardRect {
  return {
    x: asFloat(group.x, 0),
    y: asFloat(group.y, 0),
    w: Math.max(1, asFloat(group.w, 1)),
    h: Math.max(1, asFloat(group.h, 1)),
  };
}

export function groupById(state: BoardState, groupId: string): BoardGroup | null {
  return state.groups.find((group) => group.id === groupId && !group.deleted) ?? null;
}

/** 卡片所属的组（G1 保证最多一个）。 */
export function groupOfCard(state: BoardState, cardId: string): BoardGroup | null {
  return state.groups.find((group) => !group.deleted && group.members.includes(cardId)) ?? null;
}

export function cardById(state: BoardState, cardId: string): BoardCard | null {
  return state.cards.find((card) => card.id === cardId) ?? null;
}

// --- 归一化：契约 §1.2 的 G1..G8 -------------------------------------------

/** 端点字符串上限（与后端 models.MAX_ENDPOINT_CHARS 同口径）。 */
const MAX_ENDPOINT_CHARS = 200;

/**
 * 板面层的端点归一化（Phase 2 §5.3；与后端 models.normalize_link_endpoint 同口径）：
 * - 卡片：裸 card id，或兼容写法 `card:<id>`（归一化后落成裸 id，旧数据零迁移）；
 * - 组：`group:<groupId>`；
 * - 空 / 超长 / 含空白 / 未知前缀 → 空串（调用方拒绝整条关系，不猜）。
 */
function normalizeEndpointString(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text || text.length > MAX_ENDPOINT_CHARS) return "";
  if (/^card:/i.test(text)) {
    const bare = text.slice("card:".length).trim();
    const parsed = parseEndpoint(bare);
    return parsed.ok && parsed.endpoint.kind === "card" ? parsed.endpoint.value : "";
  }
  const parsed = parseEndpoint(text);
  return parsed.ok ? parsed.endpoint.value : "";
}

/**
 * meta.display 与 w/h 的统一收敛（Phase 2 §5.2；与后端 models.apply_card_display 同口径）：
 * - display 只接受「宽高都是有限正数」：非法 / 未知 → 删掉 display（不写 0 / NaN / 猜比例）；
 * - 合法时保留规范化的 {width,height,aspectRatio}，并以 **width 为准** 算 height，
 *   height < 1 时改为保底 1 并反推 width（与后端一致）；
 * - 只改显示几何，**不碰 version / updatedAt**：display 不参与材料语义版本。
 */
function applyDisplayRatio(
  kind: CardKind,
  meta: Record<string, unknown>,
  w: number,
  h: number,
): { meta: Record<string, unknown>; w: number; h: number } {
  /*
    与后端 models.clean_card_display 对齐：
    - 只有 file 卡（图片是 file 卡的附件）才有 display；其它 kind 上的 display 一律清掉；
    - 宽高都存在：以宽高算比例（不信任调用方临时算的 aspectRatio）；
    - 只有 aspectRatio：接受并只保留比例（后端同样这么处理）；
    - 宽高只给一个 / 非法 / 超上限：整体丢弃（不猜）。
  */
  const rawBlock = meta && typeof meta.display === "object" && meta.display !== null
    ? (meta.display as Record<string, unknown>)
    : null;
  const positive = (value: unknown): number | null => {
    const num = typeof value === "number" ? value : Number(value);
    return Number.isFinite(num) && num > 0 ? num : null;
  };
  const rawWidth = positive(rawBlock?.width);
  const rawHeight = positive(rawBlock?.height);
  const rawRatio = positive(rawBlock?.aspectRatio);
  const ratioValid = (value: number): boolean =>
    value > 0 && value <= 1000 && 1 / value <= 1000;
  let display: { width?: number; height?: number; aspectRatio: number } | null = null;
  if (kind === "file" && rawBlock) {
    if (rawWidth !== null || rawHeight !== null) {
      if (rawWidth === null || rawHeight === null) display = null;
      else if (rawWidth > 100000 || rawHeight > 100000) display = null;
      else {
        const pxW = Math.round(rawWidth);
        const pxH = Math.round(rawHeight);
        const computed = pxH > 0 ? pxW / pxH : 0;
        display =
          pxW >= 1 && pxH >= 1 && ratioValid(computed)
            ? { width: pxW, height: pxH, aspectRatio: Math.round(computed * 1e6) / 1e6 }
            : null;
      }
    } else if (rawRatio !== null && ratioValid(rawRatio)) {
      display = { aspectRatio: Math.round(rawRatio * 1e6) / 1e6 };
    }
  }
  if (!display) {
    if (!Object.prototype.hasOwnProperty.call(meta, "display")) return { meta, w, h };
    const cleaned = { ...meta };
    delete cleaned.display;
    return { meta: cleaned, w, h };
  }
  const nextMeta = { ...meta, display: { ...display } };
  const ratio = display.aspectRatio;
  let width = Number.isFinite(w) && w >= 1 ? w : 1;
  let height = width / ratio;
  if (!(height >= 1)) {
    height = 1;
    width = ratio >= 1 ? ratio : 1;
    if (!(width >= 1)) {
      width = 1;
      height = width / ratio;
    }
  }
  const roundedW = Math.round(width * 1000) / 1000;
  const roundedH = Math.round(height * 1000) / 1000;
  if (roundedW === w && roundedH === h) return { meta: nextMeta, w, h };
  return { meta: nextMeta, w: roundedW, h: roundedH };
}

function normalizeCard(raw: unknown, stamp: string): BoardCard | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Partial<BoardCard> & Record<string, unknown>;
  const parsedKind = (["text", "file", "code", "url", "reply"] as CardKind[]).includes(
    source.kind as CardKind,
  )
    ? (source.kind as CardKind)
    : "text";
  /*
    2026-10-15 图片展示简化：不再有 `kind="image"` 这个卡片类型，**本轮不做旧图片卡兼容** ——
    遗留的 `kind="image"` 与其它未知 kind 一样按既有兜底落到 "text"，
    也不再把 `meta.image` 搬到 `meta.file`（附件只读 `meta.file`）。
  */
  const kind: CardKind = parsedKind;
  const sourceMeta =
    source.meta && typeof source.meta === "object" ? { ...(source.meta as Record<string, unknown>) } : {};
  const checked = Boolean(source.checked);
  const hidden = Boolean(source.hidden);
  /*
    画布范围是**状态不变量**：归一化时就夹住（不只在新写入时）。
    这样已经存成负坐标的卡片（第七个问题实测 y = −63.84）在下次加载时自动回到画布内，
    不会一直「顶部被页眉遮住、拖也拖不回来」。
  */
  const bounded = clampCardRect({
    x: asFloat(source.x, 0),
    y: asFloat(source.y, 0),
    w: Math.max(1, asFloat(source.w, DEFAULT_CARD_W)),
    h: Math.max(1, asFloat(source.h, DEFAULT_CARD_H)),
  });
  const explicitParticipation =
    source.participation === "included" || source.participation === "excluded"
      ? source.participation
      : undefined;
  // 与后端 models.card_participation 同一口径：hidden 最高优先，其次遗留不可交付卡，
  // 再才是显式 participation 与旧 checked 意图。
  const participation: Participation = participationOf({
    kind,
    meta: sourceMeta,
    hidden,
    checked,
    participation: explicitParticipation,
  });
  /*
    旧兼容字段与 participation 同口径（§5.1：迁移后 checked/hidden 不再决定可见性，
    但仍作为旧服务端的兼容镜像继续同步）：参与 → hidden=false、可勾选卡 checked=true；
    不参与 → 可勾选卡 checked=false、材料 hidden=true。
  */
  const included = participation === "included";
  /*
    兼容镜像只在**旧数据**（没有显式 participation）上推导：
    一旦卡片带显式 participation，checked / hidden 就是历史字段，原样保留，
    不再被 participation 反写 —— 否则旧界面上「未勾选的注释」会在加载后突然变成已勾选
    （契约 §5.1：迁移后 checked 不再决定可见性，也不再被它反向改写）。
  */
  const legacyMirror = explicitParticipation === undefined;
  const legacyChecked = legacyMirror
    ? kind === "text" || kind === "reply"
      ? included
      : hidden
        ? false
        : checked
    : checked;
  const legacyHidden = legacyMirror ? (included ? false : kind === "file" ? true : hidden) : hidden;
  /*
    Phase 2 §5.2：meta.display 与卡片 w/h 在同一处收敛（与后端 save/read 的清洗一致）；
    只改显示几何，不碰 version / updatedAt。
  */
  const display = applyDisplayRatio(kind, sourceMeta, bounded.w, bounded.h);
  const sized = clampCardRect({ x: bounded.x, y: bounded.y, w: display.w, h: display.h });
  return {
    id: String(source.id ?? "") || newId("c"),
    kind,
    content: asText(source.content),
    meta: display.meta,
    x: sized.x,
    y: sized.y,
    w: sized.w,
    h: sized.h,
    participation,
    ...(typeof source.version === "number" && Number.isFinite(source.version)
      ? { version: source.version }
      : {}),
    // G6 明确隐藏与勾选互斥（F4：结果卡同样保留用户明确给出的许可）
    checked: legacyChecked,
    hidden: legacyHidden,
    folded: Boolean(source.folded),
    bookmarked: Boolean(source.bookmarked),
    deleted: Boolean(source.deleted),
    createdAt: asText(source.createdAt) || stamp,
    updatedAt: asText(source.updatedAt) || asText(source.createdAt) || stamp,
  };
}

/** 组框跟着成员走：位置 / 大小只影响显示，不是意图依据。 */
function reflowGroup(group: BoardGroup, cards: Map<string, BoardCard>): void {
  const members = group.members
    .map((id) => cards.get(id))
    .filter((card): card is BoardCard => Boolean(card));
  if (!members.length) return;
  const left = Math.min(...members.map((card) => card.x)) - GROUP_PAD_X;
  const top = Math.min(...members.map((card) => card.y)) - GROUP_PAD_TOP;
  const right = Math.max(...members.map((card) => card.x + card.w)) + GROUP_PAD_X;
  const bottom = Math.max(...members.map((card) => card.y + card.h)) + GROUP_PAD_BOTTOM;
  group.x = left;
  group.y = top;
  group.w = Math.max(1, right - left);
  group.h = Math.max(1, bottom - top);
}

function normalizeGroup(
  raw: unknown,
  ctx: { claimed: Set<string>; cards: Map<string, BoardCard>; takenNames: Set<string>; stamp: string },
): BoardGroup | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Partial<BoardGroup> & Record<string, unknown>;
  if (source.deleted) return null;
  const members: string[] = [];
  for (const value of (source.members as unknown[]) ?? []) {
    const cardId = String(value);
    const card = ctx.cards.get(cardId);
    // G1 一张卡只属于一个组；G5 已删除的卡片不进组
    if (ctx.claimed.has(cardId) || !card || card.deleted) continue;
    ctx.claimed.add(cardId);
    members.push(cardId);
  }
  // G3 成员全被移除或删除的组自动消失（不存在空组）
  if (!members.length) return null;
  // 历史「组 N」不被批量覆盖：defaultName 按存储值保留，缺失时才按名字形态推断（契约 §9.1）
  let name = asText(source.name).trim();
  let defaultName =
    typeof source.defaultName === "boolean" ? source.defaultName : isDefaultGroupName(name);
  if (!name) {
    name = nextDefaultGroupName(ctx.takenNames);
    defaultName = true;
  }
  const group: BoardGroup = {
    id: String(source.id ?? "") || newId("g"),
    name,
    defaultName,
    ordered: Boolean(source.ordered),
    x: asFloat(source.x, 0),
    y: asFloat(source.y, 0),
    w: Math.max(1, asFloat(source.w, 1)),
    h: Math.max(1, asFloat(source.h, 1)),
    members,
    // 收起是纯显示状态（Phase 2 §5.4）：旧数据缺省 = 展开（不写字段，避免无意义 diff）；
    // 不影响成员 / 顺序 / 语义版本。
    ...(source.collapsed ? { collapsed: true } : {}),
    deleted: false,
    createdAt: asText(source.createdAt) || ctx.stamp,
    updatedAt: asText(source.updatedAt) || asText(source.createdAt) || ctx.stamp,
    ...(typeof source.version === "number" && Number.isFinite(source.version)
      ? { version: source.version }
      : {}),
  };
  reflowGroup(group, ctx.cards);
  return group;
}

/**
 * 归一化一条关系（Phase 2 §5.3）：
 * - 端点身份：卡片 = 裸 card id（旧数据不变），组 = `group:<id>`；
 * - 组端点必须校验「组存在 / 未删 / 至少一名存活成员」；卡片端点必须存活；
 * - 自己连自己 / 端点非法 / 端点不存在 → 整条丢弃（不画假线、不保存半条关系）；
 * - version 原样保留（后端权威自增，前端绝不用内容重算成 1）。
 */
function normalizeLink(
  raw: unknown,
  ctx: { cards: BoardCard[]; groups: BoardGroup[] },
  stamp: string,
): BoardLink | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Partial<BoardLink> & Record<string, unknown>;
  if (source.deleted) return null;
  const src = normalizeEndpointString(source.src);
  const dst = normalizeEndpointString(source.dst);
  if (!src || !dst) return null;
  if (sameEndpoint(src, dst)) return null;
  const endpointContext = { cards: ctx.cards, groups: ctx.groups };
  // G4 端点必须存在且未删除（G5 已删除卡片不进链接）；组端点另需至少一名存活成员。
  if (!resolveEndpoint(src, endpointContext) || !resolveEndpoint(dst, endpointContext)) return null;
  return {
    id: String(source.id ?? "") || newId("l"),
    src,
    dst,
    direction: Boolean(source.direction),
    meaning: asText(source.meaning),
    deleted: false,
    createdAt: asText(source.createdAt) || stamp,
    updatedAt: asText(source.updatedAt) || asText(source.createdAt) || stamp,
    ...(typeof source.version === "number" && Number.isFinite(source.version)
      ? { version: source.version }
      : {}),
  };
}

/** 落实契约 §1.2 的全部不变式（G1..G8），返回**新**状态（不原地改）。 */
export function normalizeState(state: BoardState): BoardState {
  const source = (state ?? {}) as BoardState;
  const stamp = nowIso();

  const cards: BoardCard[] = [];
  const byId = new Map<string, BoardCard>();
  for (const raw of (source.cards as unknown[]) ?? []) {
    const card = normalizeCard(raw, stamp);
    if (!card || byId.has(card.id)) continue;
    byId.set(card.id, card);
    cards.push(card);
  }
  const live = new Set<string>();
  byId.forEach((card, id) => {
    if (!card.deleted) live.add(id);
  });

  const groups: BoardGroup[] = [];
  const ctx = { claimed: new Set<string>(), cards: byId, takenNames: new Set<string>(), stamp };
  for (const raw of (source.groups as unknown[]) ?? []) {
    const group = normalizeGroup(raw, ctx);
    if (!group) continue;
    ctx.takenNames.add(group.name);
    groups.push(group);
  }

  const links: BoardLink[] = [];
  const seenPairs = new Set<string>();
  // 组端点的校验需要「组已归一化」这一事实，所以关系归一化排在组之后。
  const linkContext = { cards: [...byId.values()], groups };
  for (const raw of (source.links as unknown[]) ?? []) {
    const link = normalizeLink(raw, linkContext, stamp);
    if (!link) continue;
    const pair = [link.src, link.dst].sort().join("\u0000");
    if (seenPairs.has(pair)) continue;
    seenPairs.add(pair);
    links.push(link);
  }

  const selection = dedupe((source.selection as unknown[]) ?? []).filter((id) => live.has(id));

  return {
    boardId: asText(source.boardId) || "board_default",
    seq: Math.trunc(asFloat(source.seq, 0)),
    updatedAt: asText(source.updatedAt) || stamp,
    cards,
    groups,
    links,
    selection,
    ...(typeof source.participationVersion === "number" &&
    Number.isFinite(source.participationVersion)
      ? { participationVersion: source.participationVersion }
      : {}),
  };
}

// --- 卡片：添加 / 编辑 / 删除 / 复制 ---------------------------------------

export function emptyState(boardId: string): BoardState {
  return {
    boardId,
    seq: 0,
    updatedAt: nowIso(),
    cards: [],
    groups: [],
    links: [],
    selection: [],
  };
}

/** 添加一张卡片。位置默认错开摆放（位置只影响显示，不构成意图依据）。 */
export function addCard(state: BoardState, card: Partial<BoardCard> & { kind: CardKind }): BoardState {
  const work = normalizeState(state);
  const liveCount = work.cards.filter((item) => !item.deleted).length;
  const column = liveCount % DEFAULT_CARD_COLUMNS;
  const row = Math.floor(liveCount / DEFAULT_CARD_COLUMNS) % 8;
  const defaultX = DEFAULT_CARD_ORIGIN + column * (DEFAULT_CARD_W + DEFAULT_CARD_GAP);
  const defaultY = DEFAULT_CARD_ORIGIN + row * (DEFAULT_CARD_H + DEFAULT_CARD_GAP);
  const stamp = nowIso();
  // 调用方给的坐标同样要夹：新卡片不许落进「页眉遮住的那块」（画布原点之外）
  const size = clampCardRect({
    x: asFloat(card.x, defaultX),
    y: asFloat(card.y, defaultY),
    w: Math.max(1, asFloat(card.w, DEFAULT_CARD_W)),
    h: Math.max(1, asFloat(card.h, DEFAULT_CARD_H)),
  });
  work.cards.push({
    id: String(card.id ?? "") || newId("c"),
    kind: card.kind,
    content: asText(card.content),
    meta: card.meta && typeof card.meta === "object" ? { ...card.meta } : {},
    x: size.x,
    y: size.y,
    w: size.w,
    h: size.h,
    // 参与状态是 v2 的唯一事实源：文件/文字默认参与，结果卡默认不参与；
    // 旧 checked/hidden 仍写入以兼容旧服务端。
    participation:
      card.participation === "included" || card.participation === "excluded"
        ? card.participation
        : card.kind === "file" || card.kind === "text"
          ? "included"
          : "excluded",
    checked: card.checked === undefined ? card.kind === "text" : Boolean(card.checked),
    hidden: Boolean(card.hidden),
    folded: Boolean(card.folded),
    bookmarked: Boolean(card.bookmarked),
    deleted: Boolean(card.deleted),
    createdAt: asText(card.createdAt) || stamp,
    updatedAt: asText(card.updatedAt) || stamp,
  });
  work.updatedAt = stamp;
  return normalizeState(work);
}

/**
 * 新建一张统一文字卡（契约 §1.4 / §1.6）。
 *
 * - 默认**提交给 QIO**（`checked = true`，2026-10-14 第十个问题）：整卡一个许可，
 *   覆盖卡内文字、链接与代码；用户随时可以在卡片工具栏里取消勾选（取消即不提交）；
 * - `content` 是块序列的纯文本派生，`meta.doc` 是权威，`meta.docText` 是编辑区文本；
 * - 位置只影响显示（给了 x / y 就用，没给按网格错开）。
 */
export function addTextCard(
  state: BoardState,
  opts: { x?: number; y?: number; doc?: TextDoc } = {},
): { state: BoardState; cardId: string } {
  const doc = opts.doc ?? emptyDoc();
  const fields = cardDocFields(doc);
  const cardId = newId("c");
  const next = addCard(state, {
    id: cardId,
    kind: "text",
    content: fields.content,
    meta: { doc: fields.meta.doc, docText: fields.meta.docText },
    // 新建文字卡默认提交给 QIO（可取消）
    checked: true,
    ...(typeof opts.x === "number" ? { x: opts.x } : {}),
    ...(typeof opts.y === "number" ? { y: opts.y } : {}),
  });
  return { state: next, cardId };
}

/**
 * 把块序列（或编辑区文本）写回卡片（契约 §1.4：块序列是唯一权威表示）。
 *
 * `content` 与 `meta.docText` 都是**派生**字段，两者由同一次调用一起更新 ——
 * 不允许出现「doc 变了但 content 还是旧的」这种半份状态。
 * 卡片不存在时原样返回（不新建、不抛错）。
 */
export function setCardDoc(state: BoardState, cardId: string, doc: TextDoc | { docText: string }): BoardState {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card) return work;
  const resolved: TextDoc = isTextDoc(doc) ? doc : parseDocText((doc as { docText: string }).docText ?? "");
  const fields = cardDocFields(resolved);
  return updateCard(work, cardId, {
    content: fields.content,
    meta: { ...(card.meta ?? {}), doc: fields.meta.doc, docText: fields.meta.docText },
  });
}

/** 形状判定：有 blocks 数组就是块序列，否则按「带 docText 的编辑区文本」处理。 */
function isTextDoc(value: TextDoc | { docText: string }): value is TextDoc {
  return Boolean(value) && Array.isArray((value as TextDoc).blocks);
}

export function updateCard(state: BoardState, cardId: string, patch: Partial<BoardCard>): BoardState {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card) return work;
  let changed = false;
  const assign = <K extends keyof BoardCard>(key: K, value: BoardCard[K]) => {
    if (card[key] === value) return;
    card[key] = value;
    changed = true;
  };
  if (patch.kind !== undefined) assign("kind", patch.kind);
  if (patch.content !== undefined) assign("content", asText(patch.content));
  if (patch.meta !== undefined) {
    assign("meta", patch.meta && typeof patch.meta === "object" ? { ...patch.meta } : {});
  }
  if (patch.x !== undefined) assign("x", asFloat(patch.x, card.x));
  if (patch.y !== undefined) assign("y", asFloat(patch.y, card.y));
  if (patch.w !== undefined) assign("w", Math.max(1, asFloat(patch.w, card.w)));
  if (patch.h !== undefined) assign("h", Math.max(1, asFloat(patch.h, card.h)));
  /*
    位置 / 尺寸写入统一夹进画布范围（展开让位、尺寸调整、拖动都走这里）：
    夹取只改越界的值，范围内的坐标一个字都不动。
  */
  if (patch.x !== undefined || patch.y !== undefined || patch.w !== undefined || patch.h !== undefined) {
    const bounded = clampCardRect({ x: card.x, y: card.y, w: card.w, h: card.h });
    assign("x", bounded.x);
    assign("y", bounded.y);
    assign("w", bounded.w);
    assign("h", bounded.h);
  }
  /*
    参与状态与旧兼容字段（checked / hidden）必须同步修改（Phase 2 §5.1）：
    任何一个入口只改其中一边，都会让本地可见范围与 participation 分叉。
  */
  const shippable = !LEGACY_UNSHIPPABLE_KINDS.includes(card.kind);
  if (
    shippable &&
    (patch.participation === "included" || patch.participation === "excluded")
  ) {
    assign("participation", patch.participation);
    // hidden 在参与判定里优先级最高：显式「参与」要同时清掉 hidden（除非本次也显式要求隐藏）
    if (patch.participation === "included" && patch.hidden !== true) assign("hidden", false);
  }
  if (patch.checked !== undefined) {
    assign("checked", Boolean(patch.checked));
    if (shippable && CHECKABLE_KINDS.includes(card.kind)) {
      assign("participation", patch.checked ? "included" : "excluded");
    }
  }
  if (patch.hidden !== undefined) {
    assign("hidden", Boolean(patch.hidden));
    if (shippable) assign("participation", patch.hidden ? "excluded" : "included");
  }
  if (syncLegacyParticipation(card)) changed = true;
  if (patch.folded !== undefined) assign("folded", Boolean(patch.folded));
  if (patch.bookmarked !== undefined) assign("bookmarked", Boolean(patch.bookmarked));
  if (patch.deleted !== undefined) assign("deleted", Boolean(patch.deleted));
  if (changed) {
    card.updatedAt = nowIso();
    work.updatedAt = nowIso();
  }
  return normalizeState(work);
}

/** 删除 = 撤回当前材料或关系（卡片保留 deleted 标记，可被撤销恢复）。 */
export function removeCard(state: BoardState, cardId: string): BoardState {
  return updateCard(state, cardId, { deleted: true });
}

/** 复制一张卡片：新 id、位置错开、勾选重置（副本是新材料，要重新确认）。 */
export function duplicateCard(state: BoardState, cardId: string): BoardState {
  const work = normalizeState(state);
  const source = cardById(work, cardId);
  if (!source || source.deleted) return work;
  const stamp = nowIso();
  const copy: BoardCard = {
    ...source,
    id: newId("c"),
    meta: JSON.parse(JSON.stringify(source.meta ?? {})) as Record<string, unknown>,
    x: source.x + DUPLICATE_OFFSET,
    y: source.y + DUPLICATE_OFFSET,
    /*
      副本是**新材料**：文字 / 结果卡需要用户重新确认（旧行为 checked=false），
      材料（file）沿用「材料默认在范围内」；旧兼容字段由 normalizeState 按 participation 同步。
      version 不复制（新对象由后端权威分配，前端不自造版本）。
    */
    checked: false,
    hidden: false,
    participation: source.kind === "file" ? "included" : "excluded",
    deleted: false,
    createdAt: stamp,
    updatedAt: stamp,
  };
  work.cards.push(copy);
  // 副本留在原来的组里，紧跟原卡片
  const group = groupOfCard(work, cardId);
  if (group) {
    group.members.splice(group.members.indexOf(cardId) + 1, 0, copy.id);
    group.updatedAt = stamp;
  }
  work.updatedAt = stamp;
  return normalizeState(work);
}

// --- 分组与顺序 ------------------------------------------------------------

/**
 * 把若干卡片组成一个新组（普通组，默认名或用户给的名字）。
 * 已在别的组里的卡片会先离开原来的组（G1）；没有活卡片时不建组（G3）。
 * 这是「显式分组」入口；拖动重叠自动成组走 dropCard。
 */
export function createGroup(state: BoardState, cardIds: string[], name?: string): BoardState {
  let work = normalizeState(state);
  const members: string[] = [];
  for (const cardId of dedupe(cardIds ?? [])) {
    const card = cardById(work, cardId);
    if (!card || card.deleted) continue;
    members.push(cardId);
  }
  if (!members.length) return work;
  const stamp = nowIso();
  const memberSet = new Set(members);
  for (const group of work.groups) {
    if (group.members.some((id) => memberSet.has(id))) {
      group.members = group.members.filter((id) => !memberSet.has(id));
      group.updatedAt = stamp;
    }
  }
  // 先让被搬空的组消失，再取默认名（避免新组拿到一个刚刚空掉的旧组名）
  work = normalizeState(work);
  const cleaned = asText(name).trim();
  work.groups.push({
    id: newId("g"),
    name: cleaned || nextDefaultGroupName(work.groups.map((item) => item.name)),
    defaultName: !cleaned || isDefaultGroupName(cleaned),
    ordered: false,
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    members,
    deleted: false,
    createdAt: stamp,
    updatedAt: stamp,
  });
  work.updatedAt = stamp;
  return normalizeState(work);
}

/** 把卡片加入组（会先离开原来的组）。有序组里 index 决定序号；组名不变。 */
export function joinGroup(state: BoardState, cardId: string, groupId: string, index?: number): BoardState {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  const target = groupById(work, groupId);
  if (!card || card.deleted || !target) return work;
  const stamp = nowIso();
  for (const group of work.groups) {
    if (group.id === groupId) continue;
    const at = group.members.indexOf(cardId);
    if (at >= 0) {
      group.members.splice(at, 1);
      group.updatedAt = stamp;
    }
  }
  const at = target.members.indexOf(cardId);
  if (at >= 0) target.members.splice(at, 1);
  target.members.splice(clampIndex(index, target.members.length), 0, cardId);
  target.updatedAt = stamp;
  work.updatedAt = stamp;
  return normalizeState(work);
}

/** 把卡片移出组（自由摆放，位置不变）。组空了会自动消失（G3）。 */
export function removeFromGroup(state: BoardState, cardId: string): BoardState {
  const work = normalizeState(state);
  const stamp = nowIso();
  let changed = false;
  for (const group of work.groups) {
    const at = group.members.indexOf(cardId);
    if (at >= 0) {
      group.members.splice(at, 1);
      group.updatedAt = stamp;
      changed = true;
    }
  }
  if (changed) work.updatedAt = stamp;
  return normalizeState(work);
}

/**
 * 解除组：成员全部恢复自由摆放，卡片本身不动。
 *
 * Phase 2 §5.3：以**组本身**为端点的关系随组一起消失（`group:<id>` 两端都不再存在），
 * 成员卡片之间的线**保留** —— 组只是组织方式，卡片的关联不因组消失而消失。
 */
export function dissolveGroup(state: BoardState, groupId: string): BoardState {
  const work = normalizeState(state);
  const group = groupById(work, groupId);
  if (!group) return work;
  const groupValue = GROUP_ENDPOINT_PREFIX + group.id;
  work.links = work.links.filter((link) => link.src !== groupValue && link.dst !== groupValue);
  group.members = [];
  group.deleted = true;
  work.updatedAt = nowIso();
  return normalizeState(work);
}

/**
 * 组收起 / 展开（Phase 2 §5.4）：**纯显示状态**，持久化到板面状态以便刷新后保持，
 * 但绝不参与语义版本 / 材料版本 / 意图依据（version 字段一个字都不动）。
 */
export function setGroupCollapsed(state: BoardState, groupId: string, collapsed: boolean): BoardState {
  const work = normalizeState(state);
  const group = groupById(work, groupId);
  if (!group) return work;
  const value = Boolean(collapsed);
  if (Boolean(group.collapsed) === value) return work;
  group.collapsed = value;
  group.updatedAt = nowIso();
  work.updatedAt = group.updatedAt;
  return normalizeState(work);
}

/**
 * 改组名。输入即用；留空 / 取消改名不改动任何东西 —— 组仍然成立并保留原来的名字
 * （本来就是默认名的组继续保留「默认组名」，契约 §9.1 / §8.2）。
 * 名字是不是系统默认名由名字本身决定（保留默认名也能提交）。
 */
export function renameGroup(state: BoardState, groupId: string, name: string): BoardState {
  const work = normalizeState(state);
  const group = groupById(work, groupId);
  if (!group) return work;
  const cleaned = asText(name).trim();
  if (!cleaned || cleaned === group.name) return work;
  group.name = cleaned;
  group.defaultName = isDefaultGroupName(cleaned);
  group.updatedAt = nowIso();
  work.updatedAt = group.updatedAt;
  return normalizeState(work);
}

/** 有序 / 普通切换。普通组的自由摆放不表示先后；设为有往后列表顺序即序号。 */
export function setGroupOrdered(state: BoardState, groupId: string, ordered: boolean): BoardState {
  const work = normalizeState(state);
  const group = groupById(work, groupId);
  if (!group || group.ordered === Boolean(ordered)) return work;
  group.ordered = Boolean(ordered);
  group.updatedAt = nowIso();
  work.updatedAt = group.updatedAt;
  return normalizeState(work);
}

/** 组内顺序调整：把成员移到 index（越界收敛到两端）。 */
export function moveWithinGroup(state: BoardState, groupId: string, cardId: string, index: number): BoardState {
  const work = normalizeState(state);
  const group = groupById(work, groupId);
  if (!group) return work;
  const at = group.members.indexOf(cardId);
  if (at < 0) return work;
  group.members.splice(at, 1);
  group.members.splice(clampIndex(index, group.members.length), 0, cardId);
  group.updatedAt = nowIso();
  work.updatedAt = group.updatedAt;
  return normalizeState(work);
}

/**
 * 合并两个组：目标组保留 id 但换成系统默认名「默认组名」（原组不再独立保留，
 * 契约 §9.1：合并等所有系统命名路径统一用它；组身份仍按 id）。
 * - 被拖入组的成员**连续插入**目标位置；
 * - 两个有序组 → 仍是有序，各自内部顺序保留，统一编号；
 * - 有序组与普通组合并 → 普通组（取消序号），用户可重新设为有序。
 */
export function mergeGroups(
  state: BoardState,
  sourceGroupId: string,
  targetGroupId: string,
  insertIndex?: number,
): BoardState {
  const work = normalizeState(state);
  const source = groupById(work, sourceGroupId);
  const target = groupById(work, targetGroupId);
  if (!source || !target || source.id === target.id) return work;
  const moved = [...source.members];
  if (!moved.length) return work;
  const index = clampIndex(insertIndex, target.members.length);
  target.members.splice(index, 0, ...moved);
  target.ordered = Boolean(source.ordered) && Boolean(target.ordered);
  target.name = nextDefaultGroupName(
    work.groups.filter((group) => group.id !== source.id).map((group) => group.name),
  );
  target.defaultName = true;
  target.updatedAt = nowIso();
  source.deleted = true;
  work.updatedAt = target.updatedAt;
  return normalizeState(work);
}

// --- 关系链接 --------------------------------------------------------------

/**
 * 建立关系：方向与含义都由用户写明，系统不解释成因果 / 支持 / 执行顺序。
 * 同一对端点只保留一条（G4）：重复建立时更新已有那条的方向与含义。
 */
export function addLink(
  state: BoardState,
  src: string,
  dst: string,
  direction: boolean,
  meaning: string,
): BoardState {
  const work = normalizeState(state);
  // Phase 2 §5.3：端点是卡片裸 id（兼容 card:<id>）或 group:<id>；非法 / 不存在 / 自连一律拒绝。
  const srcValue = normalizeEndpointString(src);
  const dstValue = normalizeEndpointString(dst);
  if (!srcValue || !dstValue) return work;
  if (sameEndpoint(srcValue, dstValue)) return work;
  const endpointContext = { cards: work.cards, groups: work.groups };
  if (!resolveEndpoint(srcValue, endpointContext) || !resolveEndpoint(dstValue, endpointContext)) {
    return work;
  }
  const stamp = nowIso();
  const pair = [srcValue, dstValue].sort().join("\u0000");
  const existing = work.links.find((link) => [link.src, link.dst].sort().join("\u0000") === pair);
  if (existing) {
    existing.direction = Boolean(direction);
    existing.meaning = asText(meaning);
    existing.updatedAt = stamp;
  } else {
    work.links.push({
      id: newId("l"),
      src: srcValue,
      dst: dstValue,
      direction: Boolean(direction),
      meaning: asText(meaning),
      deleted: false,
      createdAt: stamp,
      updatedAt: stamp,
    });
  }
  work.updatedAt = stamp;
  return normalizeState(work);
}

export function updateLink(state: BoardState, linkId: string, patch: Partial<BoardLink>): BoardState {
  const work = normalizeState(state);
  const link = work.links.find((item) => item.id === linkId);
  if (!link) return work;
  // 端点改动同样走 Phase 2 §5.3 的解析 / 校验：非法、自连、不存在都整次拒绝（不改任何字段）。
  const endpointContext = { cards: work.cards, groups: work.groups };
  let nextSrc = link.src;
  let nextDst = link.dst;
  if (patch.src !== undefined) {
    const value = normalizeEndpointString(patch.src);
    if (!value) return work;
    nextSrc = value;
  }
  if (patch.dst !== undefined) {
    const value = normalizeEndpointString(patch.dst);
    if (!value) return work;
    nextDst = value;
  }
  if (sameEndpoint(nextSrc, nextDst)) return work;
  if (!resolveEndpoint(nextSrc, endpointContext) || !resolveEndpoint(nextDst, endpointContext)) {
    return work;
  }
  let changed = false;
  if (nextSrc !== link.src) {
    link.src = nextSrc;
    changed = true;
  }
  if (nextDst !== link.dst) {
    link.dst = nextDst;
    changed = true;
  }
  if (patch.direction !== undefined && Boolean(patch.direction) !== link.direction) {
    link.direction = Boolean(patch.direction);
    changed = true;
  }
  if (patch.meaning !== undefined && asText(patch.meaning) !== link.meaning) {
    link.meaning = asText(patch.meaning);
    changed = true;
  }
  if (patch.deleted !== undefined && Boolean(patch.deleted) !== link.deleted) {
    link.deleted = Boolean(patch.deleted);
    changed = true;
  }
  if (changed) {
    link.updatedAt = nowIso();
    work.updatedAt = link.updatedAt;
  }
  return normalizeState(work);
}

/** 删除关系 = 撤回这条关联，不表示否定两端内容。 */
export function removeLink(state: BoardState, linkId: string): BoardState {
  return updateLink(state, linkId, { deleted: true });
}

// --- 选择 / 勾选 / 隐藏 / 折叠 / 书签 / 搜索 --------------------------------

export function setSelection(state: BoardState, cardIds: string[]): BoardState {
  const work = normalizeState(state);
  work.selection = dedupe(cardIds ?? []);
  work.updatedAt = nowIso();
  return normalizeState(work);
}

/** 区域选择：与矩形有面积重叠的卡片。additive=true 时并入当前选择。 */
export function selectInRect(state: BoardState, rect: BoardRect, additive: boolean): BoardState {
  const work = normalizeState(state);
  const area: BoardRect = {
    x: Math.min(rect.x, rect.x + rect.w),
    y: Math.min(rect.y, rect.y + rect.h),
    w: Math.abs(rect.w),
    h: Math.abs(rect.h),
  };
  const hit = work.cards
    .filter((card) => !card.deleted && rectsOverlap(cardRect(card), area))
    .map((card) => card.id);
  work.selection = dedupe([...(additive ? work.selection : []), ...hit]);
  work.updatedAt = nowIso();
  return normalizeState(work);
}

/**
 * 勾选只对文字注释有意义（材料没有勾选框，reply 不参与）。
 * 勾选与明确隐藏互斥：勾上等于回到讨论范围，所以会取消隐藏。
 */
export function setChecked(state: BoardState, cardId: string, checked: boolean): BoardState {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card || card.deleted || !CHECKABLE_KINDS.includes(card.kind)) return work;
  const value = Boolean(checked);
  const participation = value ? "included" : "excluded";
  if (card.checked === value && card.participation === participation && !(value && card.hidden)) return work;
  card.checked = value;
  card.participation = participation;
  if (value) card.hidden = false;
  card.updatedAt = nowIso();
  work.updatedAt = card.updatedAt;
  return normalizeState(work);
}

/** 明确隐藏 = 退出讨论范围（与勾选互斥）；取消隐藏不等于重新勾选。 */
export function setHidden(state: BoardState, cardId: string, hidden: boolean): BoardState {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card || card.deleted || card.hidden === Boolean(hidden)) return work;
  card.hidden = Boolean(hidden);
  card.participation = card.hidden ? "excluded" : "included";
  /*
    hidden 与 checked 的兼容镜像（旧服务端 / 旧界面读它们）：
    - 隐藏 = 退出参与 → 可勾选卡 checked=false；
    - 取消隐藏 = 重新参与 → 可勾选卡 checked=true（Phase 2 的 participation 才是唯一事实源）。
  */
  if (CHECKABLE_KINDS.includes(card.kind)) card.checked = !card.hidden;
  card.updatedAt = nowIso();
  work.updatedAt = card.updatedAt;
  return normalizeState(work);
}

/** 折叠只改变显示，不影响任何含义。 */
export function setFolded(state: BoardState, cardId: string, folded: boolean): BoardState {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card || card.deleted || card.folded === Boolean(folded)) return work;
  card.folded = Boolean(folded);
  card.updatedAt = nowIso();
  work.updatedAt = card.updatedAt;
  return normalizeState(work);
}

/** 书签只提高查找优先级，不产生意图。 */
export function setBookmark(state: BoardState, cardId: string, bookmarked: boolean): BoardState {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card || card.deleted || card.bookmarked === Boolean(bookmarked)) return work;
  card.bookmarked = Boolean(bookmarked);
  card.updatedAt = nowIso();
  work.updatedAt = card.updatedAt;
  return normalizeState(work);
}

/**
 * 板内局部搜索：只查用户自己的板面（**不是交给 QIO，也不受勾选限制**）。
 * 命中的卡片 id 按「书签优先，其次板面顺序」返回。
 */
export function searchCards(state: BoardState, query: string): string[] {
  const work = normalizeState(state);
  const text = asText(query).trim().toLowerCase();
  if (!text) return [];
  const hits: Array<{ bookmarked: number; index: number; id: string }> = [];
  work.cards.forEach((card, index) => {
    if (card.deleted) return;
    const haystack: string[] = [card.content];
    for (const value of Object.values(card.meta ?? {})) {
      if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
        haystack.push(String(value));
      } else if (Array.isArray(value)) {
        haystack.push(...value.map((item) => String(item)));
      }
    }
    if (haystack.some((part) => part.toLowerCase().includes(text))) {
      hits.push({ bookmarked: card.bookmarked ? 0 : 1, index, id: card.id });
    }
  });
  hits.sort((a, b) => a.bookmarked - b.bookmarked || a.index - b.index);
  return hits.map((hit) => hit.id);
}

// --- 拖动：预演与放下 ------------------------------------------------------

/** 落点所在的组；多个组框重叠时取面积最小的那个（最具体）。 */
function groupAtPoint(state: BoardState, x: number, y: number): BoardGroup | null {
  let bestArea = Number.POSITIVE_INFINITY;
  let bestIndex = Number.POSITIVE_INFINITY;
  let best: BoardGroup | null = null;
  state.groups.forEach((group, index) => {
    if (group.deleted || !group.members.length) return;
    const frame = groupFrame(group);
    if (!pointInRect(x, y, frame)) return;
    const area = frame.w * frame.h;
    if (area < bestArea || (area === bestArea && index < bestIndex)) {
      bestArea = area;
      bestIndex = index;
      best = group;
    }
  });
  return best;
}

/**
 * 按落点算插入位置：数一数有几个成员的**中心**排在落点之前（阅读顺序 y,x）。
 * 排除正在拖动的卡片自己，这样「没移动 → 同一位置」不会把自己算进去。
 */
function insertIndex(
  state: BoardState,
  group: BoardGroup,
  excludeCardId: string | null,
  x: number,
  y: number,
): number {
  const centers: Array<[number, number]> = [];
  for (const cardId of group.members) {
    if (cardId === excludeCardId) continue;
    const card = cardById(state, cardId);
    if (!card) continue;
    centers.push([card.y + card.h / 2, card.x + card.w / 2]);
  }
  centers.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return centers.filter(([cy, cx]) => cy < y || (cy === y && cx <= x)).length;
}

/**
 * 与卡片矩形重叠的、未分组的活卡片（自动成组的对象）。
 * 矩形按**落点**算：拖动预演时卡片还没有真的移动过去。
 *
 * 契约 §1.3 只要求「明确重叠」才算成组，所以这里用两个写清的阈值过滤：
 * - 重叠面积 ≥ 较小矩形面积的 {@link MERGE_MIN_AREA_RATIO}；
 * - 拖动卡片的**中心**落在目标矩形里，或反过来（中心覆盖）。
 * 靠近、只碰到边、只有一条细缝都不算重叠 —— 边框相碰绝不擅自合并。
 */
export const MERGE_MIN_AREA_RATIO = 0.25;
export const MERGE_REQUIRE_CENTER_COVER = true;

/** 两个矩形的重叠面积占**较小矩形**面积的比例（0..1；无重叠为 0）。 */
export function rectOverlapRatio(a: BoardRect, b: BoardRect): number {
  const aw = Math.max(0, asFloat(a.w, 0));
  const ah = Math.max(0, asFloat(a.h, 0));
  const bw = Math.max(0, asFloat(b.w, 0));
  const bh = Math.max(0, asFloat(b.h, 0));
  const base = Math.min(aw * ah, bw * bh);
  if (base <= 0) return 0;
  const overlapW = Math.min(a.x + aw, b.x + bw) - Math.max(a.x, b.x);
  const overlapH = Math.min(a.y + ah, b.y + bh) - Math.max(a.y, b.y);
  if (overlapW <= 0 || overlapH <= 0) return 0;
  return (overlapW * overlapH) / base;
}

/**
 * 中心覆盖：**落点**（指针位置）在目标矩形里，或者目标矩形的中心落在被拖动卡片的矩形里。
 * 两个方向任一成立即可 —— 这样「把卡片拖到另一张卡片上」和「拖到目标正中间」都算明确重叠。
 */
export function rectCenterCovered(dragged: BoardRect, target: BoardRect): boolean {
  if (pointInRect(dragged.x, dragged.y, target)) return true;
  const targetX = target.x + target.w / 2;
  const targetY = target.y + target.h / 2;
  return pointInRect(targetX, targetY, dragged);
}

/** 重叠判定结果：目标卡片 + 重叠比例（拖动期间提示与放下成组用同一份判定）。 */
export interface MergeCandidate {
  cardId: string;
  ratio: number;
}

interface OverlapCandidate extends MergeCandidate {
  card: BoardCard;
  index: number;
}

/** 与「落点处的卡片矩形」重叠且达到阈值的未分组活卡片（按重叠比例降序，稳定）。 */
function mergeCandidates(state: BoardState, cardId: string, x: number, y: number): OverlapCandidate[] {
  const card = cardById(state, cardId);
  if (!card) return [];
  const rect: BoardRect = {
    x: asFloat(x, 0),
    y: asFloat(y, 0),
    w: Math.max(1, asFloat(card.w, DEFAULT_CARD_W)),
    h: Math.max(1, asFloat(card.h, DEFAULT_CARD_H)),
  };
  const result: OverlapCandidate[] = [];
  state.cards.forEach((other, index) => {
    if (other.id === cardId || other.deleted) return;
    if (groupOfCard(state, other.id)) return;
    const otherRect = cardRect(other);
    const ratio = rectOverlapRatio(rect, otherRect);
    if (ratio < MERGE_MIN_AREA_RATIO) return;
    if (MERGE_REQUIRE_CENTER_COVER && !rectCenterCovered(rect, otherRect)) return;
    result.push({ cardId: other.id, ratio, card: other, index });
  });
  // 比例高者优先；比例相同时按卡片顺序取第一个 —— 确定性目标，绝不把一叠卡片一起合并
  return result.sort((a, b) => b.ratio - a.ratio || a.index - b.index);
}

/**
 * 明确的成组目标：拖到另一张**未分组**卡片上时给出那张卡片。
 * 靠近不成组、边框相碰不合并、没有达到阈值一律返回 null（此时不显示「松开后合并成组」）。
 */
export function bestMergeTarget(
  state: BoardState,
  cardId: string,
  x: number,
  y: number,
): MergeCandidate | null {
  const work = normalizeState(state);
  const best = mergeCandidates(work, cardId, x, y)[0];
  return best ? { cardId: best.cardId, ratio: best.ratio } : null;
}

function overlappingFreeCards(state: BoardState, cardId: string, x: number, y: number): BoardCard[] {
  return mergeCandidates(state, cardId, x, y).map((item) => item.card);
}

function resolveDrop(
  state: BoardState,
  cardId: string,
  x: number,
  y: number,
): { target: BoardGroup | null; index: number | null; mergesWith: string | null } {
  const target = groupAtPoint(state, x, y);
  const current = groupOfCard(state, cardId);
  if (target) {
    const index = insertIndex(state, target, cardId, x, y);
    const mergesWith = current && current.id !== target.id ? current.id : null;
    return { target, index, mergesWith };
  }
  // 未分组卡片之间：只认**明确**的成组目标（阈值见 MERGE_MIN_AREA_RATIO）
  const best = bestMergeTarget(state, cardId, x, y);
  return { target: null, index: null, mergesWith: best ? best.cardId : null };
}

/** 拖动期间显示将要加入的组或插入位置 —— **不改状态**。 */
export function previewDrop(state: BoardState, cardId: string, x: number, y: number): DropPreview {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card || card.deleted) return { groupId: null, index: null, mergesWith: null };
  const resolved = resolveDrop(work, cardId, asFloat(x, 0), asFloat(y, 0));
  return {
    groupId: resolved.target ? resolved.target.id : null,
    index: resolved.index,
    mergesWith: resolved.mergesWith,
  };
}

/** 放下后才完成操作：重叠成组 / 加入已有组 / 合并两组 / 有序组按落点插入。 */
//: 找空位时向外搜索的步长与最大圈数（40 圈 × 16px = 最多挪 640px）
const FREE_DROP_STEP = 16;
const FREE_DROP_MAX_RINGS = 40;

/** 第 ring 圈的候选偏移：按与落点的远近排（先四边，再四角）。 */
function freeDropRing(ring: number): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (let i = -ring; i <= ring; i++) {
    if (Math.abs(i) === ring) out.push([i, -ring], [i, ring], [-ring, i], [ring, i]);
  }
  return out;
}

/**
 * 落点被占就挪到最近的空位（2026-10-14 用户要求：**卡片不允许叠放**）。
 *
 * 纯函数、不改状态：以落点为中心一圈圈向外找第一个与**别的卡片**都不相交的位置。
 * 找不到就如实返回原落点 —— 不假装让开了，也不把卡片推出画布（夹取由 clampCardRect 兜住）。
 */
/** 整块板面整体平移：卡片与组框一起走，语义不变（位置不构成意图依据）。 */
export function rebaseOrigin(state: BoardState, dx: number, dy: number): BoardState {
  if (!dx && !dy) return state;
  const work = normalizeState(state);
  const cards = work.cards.map((card) =>
    card.deleted ? card : { ...card, x: card.x + dx, y: card.y + dy },
  );
  const groups = work.groups.map((group) => ({ ...group, x: group.x + dx, y: group.y + dy }));
  return normalizeState({ ...work, cards, groups });
}

/**
 * 让板面回到「内容全部 ≥ pad」的坐标系，返回需要整体平移的量（2026-10-14 方案 B 第二步）。
 *
 * 为什么用"整体平移"而不是"允许负坐标"：卡片的 x/y 会被渲染、连线、组框、工具栏换算、
 * 板面可用区发布各自读一遍 —— 让坐标变负就得改所有这些地方；整体平移把它们一次性免掉，
 * 语义上也没有损失（位置只是位置）。
 */
export function rebaseToFit(state: BoardState, pad: number): { dx: number; dy: number } {
  const work = normalizeState(state);
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  for (const card of work.cards) {
    if (card.deleted) continue;
    minX = Math.min(minX, card.x);
    minY = Math.min(minY, card.y);
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return { dx: 0, dy: 0 };
  return {
    dx: minX < pad ? pad - minX : 0,
    dy: minY < pad ? pad - minY : 0,
  };
}

export function freeDropSpot(
  state: BoardState,
  cardId: string,
  x: number,
  y: number,
): { x: number; y: number } {
  const work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card) return { x, y };
  const others = work.cards.filter((item) => !item.deleted && item.id !== cardId);
  const hits = (px: number, py: number): boolean =>
    others.some((o) => px < o.x + o.w && o.x < px + card.w && py < o.y + o.h && o.y < py + card.h);
  if (!hits(x, y)) return { x, y };
  for (let ring = 1; ring <= FREE_DROP_MAX_RINGS; ring++) {
    for (const [dx, dy] of freeDropRing(ring)) {
      const spot = clampCardRect({
        x: x + dx * FREE_DROP_STEP,
        y: y + dy * FREE_DROP_STEP,
        w: card.w,
        h: card.h,
      });
      if (!hits(spot.x, spot.y)) return { x: spot.x, y: spot.y };
    }
  }
  return { x, y };
}

export function dropCard(state: BoardState, cardId: string, x: number, y: number): DropResult {
  const px = asFloat(x, 0);
  const py = asFloat(y, 0);
  let work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card || card.deleted) {
    return { state: work, groupId: null, merged: false, index: null };
  }

  // 落点先夹进画布范围：负坐标的卡片顶部永远看不见（见 clampCardRect 的说明）
  const placed = clampCardRect({ x: px, y: py, w: card.w, h: card.h });
  const resolved = resolveDrop(work, cardId, placed.x, placed.y);
  /*
    不成组时**不许叠放**：落点被别的卡片占住就自动挪到最近的空位。
    成组时不挪：位置由组框排布接管（合并成组本来就是用户明确要的语义）。
  */
  /*
    会成组就不挪：位置由组框排布接管。
    注意有**两条**成组路径 —— resolved.target（落点明确指向某张卡）与 bestMergeTarget
    （两张未分组卡片明确重叠 → 自动成组）。只看前者会让后者也被让位，
    于是「重叠自动成组」的卡片先被推开、再成组（既有用例抓到过）。
  */
  const willGroup = Boolean(resolved.target) || Boolean(bestMergeTarget(work, cardId, px, py));
  const spot = willGroup ? { x: placed.x, y: placed.y } : freeDropSpot(work, cardId, placed.x, placed.y);
  const stamp = nowIso();
  card.x = spot.x;
  card.y = spot.y;
  card.updatedAt = stamp;
  work.updatedAt = stamp;

  let merged = false;
  if (resolved.target) {
    const current = groupOfCard(work, cardId);
    if (current && current.id !== resolved.target.id) {
      // 两个已有组重叠 → 合并（新组用默认名，被拖入组的成员连续插入）
      work = mergeGroups(work, current.id, resolved.target.id, resolved.index ?? undefined);
      merged = true;
    } else {
      work = joinGroup(work, cardId, resolved.target.id, resolved.index ?? undefined);
    }
  } else {
    if (groupOfCard(work, cardId)) {
      // 拖出组：自由摆放，组空了会自动消失
      work = removeFromGroup(work, cardId);
    }
    // 只把**明确目标**那张卡片并进来：靠近 / 边框相碰不合并，一叠卡片也不会被整堆合并
    const best = bestMergeTarget(work, cardId, px, py);
    if (best) {
      const partner = cardById(work, best.cardId);
      // 两张未分组卡片明确重叠 → 自动成组，系统默认名「默认组名」（组框由成员推导，位置只影响显示）
      work.groups.push({
        id: newId("g"),
        name: nextDefaultGroupName(work.groups.map((item) => item.name)),
        defaultName: true,
        ordered: false,
        x: px,
        y: py,
        w: Math.max(1, card.w + GROUP_PAD_X * 2),
        h: Math.max(1, card.h + GROUP_PAD_TOP + GROUP_PAD_BOTTOM),
        members: partner ? [partner.id, cardId] : [cardId],
        deleted: false,
        createdAt: stamp,
        updatedAt: stamp,
      });
      work.updatedAt = stamp;
    }
  }

  const final = normalizeState(work);
  const group = groupOfCard(final, cardId);
  return {
    state: final,
    groupId: group ? group.id : null,
    merged,
    index: group ? group.members.indexOf(cardId) : null,
  };
}
