/**
 * 组端点 / 连线显示几何 / 组框折叠槽位（Phase 2-B 纯函数，唯一事实源）。
 *
 * 契约：docs/interactive-page-v2-contract.md §5.3。
 *
 * 边界（与 board.ts / geometry.ts 同一条）：
 * - 只做算术与校验：不读 DOM、不读 store、不发请求、不产生第二套位置；
 * - 输入对象一个字段都不改（返回的矩形 / 端点 / 连线都是新的普通对象）；
 * - 同样的输入永远得到同样的输出（确定性）。
 *
 * 四条口径：
 * 1. **端点身份**：卡片 = 裸 card id（旧数据不需要迁移），组 = `group:<groupId>`。
 *    组端点必须校验「组存在 / 未删 / 至少一名存活（获准）成员」；没有成员就没有这条线。
 * 2. **显示锚点**：连线锚在**最终显示矩形**的边缘（不是中心点），拖动 / 缩放的预览坐标
 *    直接由调用方传入，所以显示与命中永远一致。
 * 3. **组收起**：成员对外连线可以视觉汇聚到组边缘（aggregated），但 `src/dst` 的真实
 *    端点身份一个字不改；组内成员之间的线收起期间不画，展开后恢复。组端点线只有一条，
 *    它表示**当前成员集合**（由组框表达），不按成员复制、不暴露成员名。
 * 4. **隐私**：参与范围由调用方的 `includedCardIds` 给出；组端点按「至少一名获准成员」
 *    校验。无障碍文案只用组名与「卡片」，绝不列成员名 / 正文 / id。
 */

import type { BoardCard, BoardGroup, BoardLink, LinkCurve } from "./types";

export interface BoardRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Point {
  x: number;
  y: number;
}

/** 组端点前缀（端点字符串的唯一来源；组件不要再拼一份）。 */
export const GROUP_ENDPOINT_PREFIX = "group:";

/** 与 board.ts 的 GROUP_PAD_TOP / GROUP_PAD_X / GROUP_PAD_BOTTOM 同口径（那边是私有常量）。 */
export const GROUP_HEADER_HEIGHT = 58;
export const GROUP_PAD_X = 16;
export const GROUP_PAD_BOTTOM = 16;

/** 收起概览的固定槽位尺寸与数量上限（固定槽位 = 收起 / 展开不跳动）。 */
export const GROUP_OVERVIEW_SLOT = { w: 64, h: 44, gap: 8 } as const;
export const GROUP_OVERVIEW_MAX_SLOTS = 5;

export type GroupResizeEdge = "e" | "s" | "se";
export const GROUP_RESIZE_EDGES: readonly GroupResizeEdge[] = ["e", "s", "se"];

export interface GroupHandle {
  edge: GroupResizeEdge;
  x: number;
  y: number;
  cursor: string;
  label: string;
}

// --- 端点解析 / 归一化 ------------------------------------------------------

export type EndpointKind = "card" | "group";

export interface Endpoint {
  kind: EndpointKind;
  id: string;
  /** 规范化后的持久化字符串：卡片 = 裸 id；组 = group:<id>。 */
  value: string;
}

export type EndpointParseReason = "empty" | "missing-id" | "invalid-id" | "unknown-prefix";

export type EndpointParse =
  | { ok: true; endpoint: Endpoint }
  | { ok: false; raw: string; reason: EndpointParseReason };

function rawString(raw: unknown): string {
  if (typeof raw === "string") return raw;
  if (typeof raw === "number" || typeof raw === "bigint") return String(raw);
  return "";
}

function hasWhitespace(value: string): boolean {
  return /\s/.test(value);
}

/**
 * 解析端点字符串。
 * - 卡片：裸 id（不含冒号、不含空白）；
 * - 组：`group:<id>`（前缀大小写不敏感，规范化后统一小写）；
 * - 其余（空 / 缺 id / 带未知前缀 / id 内有空白）一律判无效，**不猜**。
 */
export function parseEndpoint(raw: unknown): EndpointParse {
  const text = rawString(raw).trim();
  if (!text) return { ok: false, raw: text, reason: "empty" };
  if (text.toLowerCase().startsWith(GROUP_ENDPOINT_PREFIX)) {
    const id = text.slice(GROUP_ENDPOINT_PREFIX.length).trim();
    if (!id) return { ok: false, raw: text, reason: "missing-id" };
    if (hasWhitespace(id)) return { ok: false, raw: text, reason: "invalid-id" };
    return { ok: true, endpoint: { kind: "group", id, value: GROUP_ENDPOINT_PREFIX + id } };
  }
  if (text.includes(":")) return { ok: false, raw: text, reason: "unknown-prefix" };
  if (hasWhitespace(text)) return { ok: false, raw: text, reason: "invalid-id" };
  return { ok: true, endpoint: { kind: "card", id: text, value: text } };
}

/** 构造卡片端点；空 / 非法 id 返回 null（不产生 `group:` 之类的假端点）。 */
export function cardEndpoint(cardId: string): Endpoint | null {
  const parsed = parseEndpoint(cardId);
  return parsed.ok && parsed.endpoint.kind === "card" ? parsed.endpoint : null;
}

/** 构造组端点；空 / 非法 id 返回 null。 */
export function groupEndpoint(groupId: string): Endpoint | null {
  const id = String(groupId ?? "").trim();
  if (!id || hasWhitespace(id) || id.includes(":")) return null;
  return { kind: "group", id, value: GROUP_ENDPOINT_PREFIX + id };
}

/** 规范化后的端点字符串；无效返回空串。 */
export function endpointValue(raw: unknown): string {
  const parsed = parseEndpoint(raw);
  return parsed.ok ? parsed.endpoint.value : "";
}

/** 用于同一对端点比较 / 去重的规范键（含 kind，卡片与组同 id 也不同）。 */
export function endpointKey(raw: unknown): string {
  const parsed = parseEndpoint(raw);
  return parsed.ok ? parsed.endpoint.kind + ":" + parsed.endpoint.id : "";
}

export function sameEndpoint(a: unknown, b: unknown): boolean {
  const keyA = endpointKey(a);
  return keyA !== "" && keyA === endpointKey(b);
}

// --- 端点校验（存在 / 未删 / 获准成员） ------------------------------------

export interface EndpointContext {
  cards: readonly BoardCard[];
  groups?: readonly BoardGroup[];
  /**
   * 参与范围（Phase 2 §5.1）：只有列在这里的卡片才算「可交给 QIO」。
   * 缺省 / null = 不区分参与（用户视野）。传空数组 = 没有任何获准成员。
   */
  includedCardIds?: readonly string[] | null;
}

export interface ResolvedEndpoint {
  kind: EndpointKind;
  id: string;
  value: string;
  rect: BoardRect;
  /** 组端点：当前成员集合（存活 + 获准）；卡片端点：[自身]。不用于渲染成员名。 */
  memberIds: string[];
  card?: BoardCard;
  group?: BoardGroup;
  includedMemberCount: number;
}

function finite(value: unknown, fallback = 0): number {
  const result = typeof value === "number" ? value : Number(value);
  return Number.isFinite(result) ? result : fallback;
}

function cardRectOf(card: BoardCard): BoardRect {
  return {
    x: finite(card.x, 0),
    y: finite(card.y, 0),
    w: Math.max(1, finite(card.w, 1)),
    h: Math.max(1, finite(card.h, 1)),
  };
}

function groupRectOf(group: BoardGroup): BoardRect {
  return {
    x: finite(group.x, 0),
    y: finite(group.y, 0),
    w: Math.max(1, finite(group.w, 1)),
    h: Math.max(1, finite(group.h, 1)),
  };
}

function includedSet(ctx: EndpointContext): Set<string> | null {
  if (!Array.isArray(ctx.includedCardIds)) return null;
  return new Set(ctx.includedCardIds.filter((id): id is string => typeof id === "string"));
}

function liveCard(ctx: EndpointContext, cardId: string): BoardCard | null {
  return ctx.cards.find((card) => card.id === cardId && !card.deleted) ?? null;
}

function liveGroup(ctx: EndpointContext, groupId: string): BoardGroup | null {
  return (ctx.groups ?? []).find((group) => group.id === groupId && !group.deleted) ?? null;
}

function liveMembers(ctx: EndpointContext, group: BoardGroup, included: Set<string> | null): BoardCard[] {
  const members: BoardCard[] = [];
  for (const cardId of group.members) {
    const card = liveCard(ctx, cardId);
    if (!card) continue;
    if (included && !included.has(card.id)) continue;
    members.push(card);
  }
  return members;
}

/**
 * 解析成「可用于显示」的端点：
 * - 卡片端点：存在且未删除即可（未参与 ≠ 不可显示 —— 用户自己的板面仍看得见）；
 * - 组端点：组存在、未删除，且至少一名存活成员；给了参与范围时还要求至少一名获准成员。
 * 解析不出来返回 null（调用方如实跳过，不画假线）。
 */
export function resolveEndpoint(raw: unknown, ctx: EndpointContext): ResolvedEndpoint | null {
  const parsed = parseEndpoint(raw);
  if (!parsed.ok) return null;
  const included = includedSet(ctx);
  if (parsed.endpoint.kind === "card") {
    const card = liveCard(ctx, parsed.endpoint.id);
    if (!card) return null;
    const inIncluded = !included || included.has(card.id);
    return {
      kind: "card",
      id: card.id,
      value: card.id,
      rect: cardRectOf(card),
      memberIds: [card.id],
      card,
      includedMemberCount: inIncluded ? 1 : 0,
    };
  }
  const group = liveGroup(ctx, parsed.endpoint.id);
  if (!group) return null;
  const members = liveMembers(ctx, group, included);
  if (!members.length) return null;
  return {
    kind: "group",
    id: group.id,
    value: GROUP_ENDPOINT_PREFIX + group.id,
    rect: groupRectOf(group),
    memberIds: members.map((card) => card.id),
    group,
    includedMemberCount: members.length,
  };
}

export type EndpointRangeReason =
  | "invalid"
  | "missing-card"
  | "missing-group"
  | "no-members"
  | "not-included"
  | "no-included-members";

export interface EndpointRangeCheck {
  ok: boolean;
  reason?: EndpointRangeReason;
}

/** 这个端点能否进入「可交给 QIO」的范围投影（不是用户视野可见性）。 */
export function endpointInVisibleRange(raw: unknown, ctx: EndpointContext): EndpointRangeCheck {
  const parsed = parseEndpoint(raw);
  if (!parsed.ok) return { ok: false, reason: "invalid" };
  const included = includedSet(ctx);
  if (parsed.endpoint.kind === "card") {
    const card = liveCard(ctx, parsed.endpoint.id);
    if (!card) return { ok: false, reason: "missing-card" };
    if (included && !included.has(card.id)) return { ok: false, reason: "not-included" };
    return { ok: true };
  }
  const group = liveGroup(ctx, parsed.endpoint.id);
  if (!group) return { ok: false, reason: "missing-group" };
  const members = liveMembers(ctx, group, null);
  if (!members.length) return { ok: false, reason: "no-members" };
  if (included && !members.some((card) => included.has(card.id))) {
    return { ok: false, reason: "no-included-members" };
  }
  return { ok: true };
}

/** 组端点校验的便捷入口（给组件 / store 接线用）。 */
export function checkGroupEndpoint(groupId: string, ctx: EndpointContext): EndpointRangeCheck {
  const endpoint = groupEndpoint(groupId);
  if (!endpoint) return { ok: false, reason: "invalid" };
  return endpointInVisibleRange(endpoint.value, ctx);
}

// --- 连线归一化（保留旧裸 card id；同一对端点只留一条） --------------------

export type NormalizeLinkRejectReason = "self" | "missing-endpoint" | "duplicate";

export interface NormalizeLinksResult {
  links: BoardLink[];
  rejected: Array<{ id: string; reason: NormalizeLinkRejectReason }>;
}

function endpointExists(raw: unknown, ctx: EndpointContext): boolean {
  const parsed = parseEndpoint(raw);
  if (!parsed.ok) return false;
  if (parsed.endpoint.kind === "card") return liveCard(ctx, parsed.endpoint.id) !== null;
  const group = liveGroup(ctx, parsed.endpoint.id);
  if (!group) return false;
  return group.members.some((cardId) => liveCard(ctx, cardId) !== null);
}

/**
 * 归一化一组连线：
 * - 旧数据裸 card id 原样保留；组端点规范成 `group:<id>`；
 * - 未知字段（如将来新增的 version）原样保留；
 * - 自己连自己 / 端点解析失败 / 给了上下文时端点不存在 → 拒绝，并把原因如实返回；
 * - 同一对（无向）端点只留第一条（契约 G4）。
 */
export function normalizeBoardLinks(
  links: readonly BoardLink[],
  ctx?: EndpointContext,
): NormalizeLinksResult {
  const result: NormalizeLinksResult = { links: [], rejected: [] };
  const seen = new Set<string>();
  for (const link of links ?? []) {
    if (!link || typeof link !== "object") continue;
    if (link.deleted) continue;
    const src = parseEndpoint(link.src);
    const dst = parseEndpoint(link.dst);
    if (!src.ok || !dst.ok) {
      result.rejected.push({ id: String(link.id), reason: "missing-endpoint" });
      continue;
    }
    if (src.endpoint.value === dst.endpoint.value) {
      result.rejected.push({ id: String(link.id), reason: "self" });
      continue;
    }
    if (ctx && (!endpointExists(src.endpoint.value, ctx) || !endpointExists(dst.endpoint.value, ctx))) {
      result.rejected.push({ id: String(link.id), reason: "missing-endpoint" });
      continue;
    }
    const pair = [src.endpoint.value, dst.endpoint.value].sort().join("\u0000");
    if (seen.has(pair)) {
      result.rejected.push({ id: String(link.id), reason: "duplicate" });
      continue;
    }
    seen.add(pair);
    result.links.push({ ...link, src: src.endpoint.value, dst: dst.endpoint.value });
  }
  return result;
}

// --- 显示锚点：最终显示矩形的边缘 ------------------------------------------

export function rectCenter(rect: BoardRect): Point {
  return { x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 };
}

/**
 * 从矩形「边缘」沿「中心 → toward」方向求交点。
 * 中心与 toward 重合时退化为右边中点（不产生 NaN / 0 除）。
 */
export function edgeAnchor(rect: BoardRect, toward: Point): Point {
  const center = rectCenter(rect);
  const dx = toward.x - center.x;
  const dy = toward.y - center.y;
  if (!Number.isFinite(dx) || !Number.isFinite(dy) || (dx === 0 && dy === 0)) {
    return { x: rect.x + rect.w, y: center.y };
  }
  const tx = dx === 0 ? Number.POSITIVE_INFINITY : rect.w / 2 / Math.abs(dx);
  const ty = dy === 0 ? Number.POSITIVE_INFINITY : rect.h / 2 / Math.abs(dy);
  const t = Math.min(tx, ty);
  return { x: center.x + dx * t, y: center.y + dy * t };
}

/** 两个最终显示矩形之间的连线：两端各自锚在自己的边缘上。 */
export function linkAnchor(a: BoardRect, b: BoardRect): { start: Point; end: Point } {
  return { start: edgeAnchor(a, rectCenter(b)), end: edgeAnchor(b, rectCenter(a)) };
}

// --- 组端点锚点：只用关系实际用到的那些边缘位置 -----------------------------

export type GroupAnchorSide = "top" | "right" | "bottom" | "left";

/**
 * 组作为**真实端点**时，关系线在组框边缘留下的锚点。
 *
 * 这里刻意不提供「四条边各一个端口」：只有真的被关系使用的边缘位置才出现。
 * 锚点坐标与 {@link buildDisplayLinks} 的线端点逐字段相同，所以视觉圆点、
 * 线的起止、命中目标永远在同一处；曲线与 curve 字段不受影响。
 *
 * 组收起后成员对外线的汇聚点不算组端点（真实端点仍是成员卡），因此不会
 * 在这里凭空长出一个组锚点；成员卡自己的连接点仍由卡片端点层表示。
 */
export interface GroupAnchor {
  groupId: string;
  /** 组端点字符串：group:<id>（与关系的 src/dst 同一口径）。 */
  endpoint: string;
  groupName: string;
  side: GroupAnchorSide;
  /** 板面坐标：精确落在线与组框边缘的交点上。 */
  x: number;
  y: number;
  /** 使用这个锚点的关系 id（同一位置的多条关系合并为一个锚点）。 */
  linkIds: string[];
}

/** 点在矩形哪条边上；角点按「朝另一端的主要方向」判，保证结果确定。 */
function groupAnchorSideOf(rect: BoardRect, point: Point, other: Point): GroupAnchorSide {
  const epsilon = 0.75;
  const onLeft = Math.abs(point.x - rect.x) <= epsilon;
  const onRight = Math.abs(point.x - (rect.x + rect.w)) <= epsilon;
  const onTop = Math.abs(point.y - rect.y) <= epsilon;
  const onBottom = Math.abs(point.y - (rect.y + rect.h)) <= epsilon;
  const horizontal: GroupAnchorSide | null = onLeft ? "left" : onRight ? "right" : null;
  const vertical: GroupAnchorSide | null = onTop ? "top" : onBottom ? "bottom" : null;
  if (horizontal && vertical) {
    const dx = other.x - (rect.x + rect.w / 2);
    const dy = other.y - (rect.y + rect.h / 2);
    return Math.abs(dx) >= Math.abs(dy) ? horizontal : vertical;
  }
  if (horizontal) return horizontal;
  if (vertical) return vertical;
  // 兜底：取最近的一条边（只在浮点误差让四条边都不满足 epsilon 时走到）。
  const distances: Array<{ side: GroupAnchorSide; distance: number }> = [
    { side: "left", distance: Math.abs(point.x - rect.x) },
    { side: "right", distance: Math.abs(point.x - (rect.x + rect.w)) },
    { side: "top", distance: Math.abs(point.y - rect.y) },
    { side: "bottom", distance: Math.abs(point.y - (rect.y + rect.h)) },
  ];
  distances.sort((a, b) => a.distance - b.distance);
  return distances[0].side;
}

/**
 * 从显示层关系里提取组端点锚点。
 *
 * - 只看**真实组端点**（srcKind/dstKind = group）；组收起时成员线汇聚到组边，
 *   但那是显示汇聚，不是组端点，不产生锚点。
 * - 同一组同一位置的多条关系合并成一个锚点（linkIds 保持关系出现顺序）。
 * - 位置与 buildDisplayLinks 的输出完全一致，不另算一套几何。
 */
export function groupEdgeAnchors(input: DisplayLinksInput): GroupAnchor[] {
  const groups = input.groups ?? [];
  const anchors = new Map<string, GroupAnchor>();

  const add = (link: DisplayLink, end: "src" | "dst"): void => {
    const endpoint = end === "src" ? link.src : link.dst;
    const kind = end === "src" ? link.srcKind : link.dstKind;
    const aggregated = end === "src" ? link.aggregatedSrc : link.aggregatedDst;
    if (kind !== "group" || aggregated) return;
    const groupId = endpoint.slice(GROUP_ENDPOINT_PREFIX.length);
    const group = groups.find((item) => item.id === groupId && !item.deleted);
    if (!group) return;
    const point: Point = end === "src" ? { x: link.x1, y: link.y1 } : { x: link.x2, y: link.y2 };
    const other: Point = end === "src" ? { x: link.x2, y: link.y2 } : { x: link.x1, y: link.y1 };
    const side = groupAnchorSideOf(groupRectOf(group), point, other);
    const key = [groupId, Math.round(point.x * 10), Math.round(point.y * 10)].join("\u0000");
    const existing = anchors.get(key);
    if (existing) {
      if (!existing.linkIds.includes(link.id)) existing.linkIds.push(link.id);
      return;
    }
    anchors.set(key, {
      groupId,
      endpoint: GROUP_ENDPOINT_PREFIX + groupId,
      groupName: group.name || "未命名组",
      side,
      x: point.x,
      y: point.y,
      linkIds: [link.id],
    });
  };

  const display = buildDisplayLinks(input);
  for (const link of display.links) {
    add(link, "src");
    add(link, "dst");
  }
  return [...anchors.values()];
}

// --- 组框缩放把手 -----------------------------------------------------------

/** 组的缩放把手：右 / 下 / 右下（组框左上角由成员推出，因此只开放这三条边）。 */
export function groupResizeHandles(group: BoardRect): GroupHandle[] {
  const right = group.x + group.w;
  const bottom = group.y + group.h;
  return [
    { edge: "e", x: right, y: group.y + group.h / 2, cursor: "ew-resize", label: "调整组宽" },
    { edge: "s", x: group.x + group.w / 2, y: bottom, cursor: "ns-resize", label: "调整组高" },
    { edge: "se", x: right, y: bottom, cursor: "nwse-resize", label: "同时调整组宽与组高" },
  ];
}

// --- 组收起概览：固定槽位 ---------------------------------------------------

export interface GroupOverviewSlot {
  /** null = 空槽（固定槽位：成员不足时也保留位置，收起 / 展开不跳动）。 */
  cardId: string | null;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface GroupOverview {
  slots: GroupOverviewSlot[];
  /** 固定槽位数量（= slots.length，没有成员时也保留空槽）。 */
  slotCount: number;
  /** 放不下的存活成员数量。 */
  overflow: number;
  /** 概览可用内区（板面坐标，已扣掉标题带与两侧留白）。 */
  inner: BoardRect;
}

/**
 * 收起概览的固定槽位布局（**只排显示，不代表执行顺序**）。
 * - 位置只由槽位序号决定（与卡片 id / 组是否有序无关）；
 * - 成员按组的 members 顺序填槽，不足留空槽，多了如实计入 overflow；
 * - 只统计存活成员（已删除 / 不存在的成员不凭空出现）。
 */
export function groupOverviewSlots(input: {
  group: BoardGroup;
  cards: readonly BoardCard[];
  maxSlots?: number;
}): GroupOverview {
  const rect = groupRectOf(input.group);
  const inner: BoardRect = {
    x: rect.x + GROUP_PAD_X,
    y: rect.y + GROUP_HEADER_HEIGHT,
    w: Math.max(0, rect.w - GROUP_PAD_X * 2),
    h: Math.max(0, rect.h - GROUP_HEADER_HEIGHT - GROUP_PAD_BOTTOM),
  };
  const memberCards = input.group.members
    .map((cardId) => input.cards.find((card) => card.id === cardId && !card.deleted))
    .filter((card): card is BoardCard => Boolean(card));
  const slot = GROUP_OVERVIEW_SLOT;
  const maxSlots = Math.max(0, Math.trunc(finite(input.maxSlots, GROUP_OVERVIEW_MAX_SLOTS)));
  const columns = inner.w >= slot.w ? Math.max(1, Math.floor((inner.w + slot.gap) / (slot.w + slot.gap))) : 0;
  const rows = inner.h >= slot.h ? Math.max(1, Math.floor((inner.h + slot.gap) / (slot.h + slot.gap))) : 0;
  const capacity = Math.min(columns * rows, maxSlots);
  const slots: GroupOverviewSlot[] = [];
  for (let index = 0; index < capacity; index += 1) {
    const column = index % Math.max(1, columns);
    const row = Math.floor(index / Math.max(1, columns));
    slots.push({
      cardId: memberCards[index]?.id ?? null,
      x: inner.x + column * (slot.w + slot.gap),
      y: inner.y + row * (slot.h + slot.gap),
      w: slot.w,
      h: slot.h,
    });
  }
  return { slots, slotCount: slots.length, overflow: Math.max(0, memberCards.length - slots.length), inner };
}

// --- 显示层连线（组端点 / 收起汇聚 / 可见范围） -----------------------------

export type SkippedLinkReason =
  | "invalid-endpoint"
  | "missing-endpoint"
  | "self"
  | "duplicate"
  | "collapsed-internal"
  | "out-of-range";

export interface DisplayLink {
  id: string;
  /** 真实端点身份：组收起时也不变（卡片仍是裸 id，组仍是 group:<id>）。 */
  src: string;
  dst: string;
  srcKind: EndpointKind;
  dstKind: EndpointKind;
  /** 实际用于显示锚点的端点（组收起时成员端会汇聚成 group:<id>）。 */
  displaySrcId: string;
  displayDstId: string;
  aggregatedSrc: boolean;
  aggregatedDst: boolean;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  labelX: number;
  labelY: number;
  labelW: number;
  /** 用户拖出的曲线偏移；默认 {0,0} 表示标准曲线。 */
  curve: LinkCurve;
  direction: boolean;
  meaning: string;
  active: boolean;
  /** 这条线是否可进入「可交给 QIO」的范围（两端都通过参与校验）。 */
  rangeVisible: boolean;
  /** 无障碍名称：含义 + 两端的「组名 / 卡片」，绝不列成员名 / 正文 / id。 */
  ariaLabel: string;
}

export interface DisplayLinksInput {
  links: readonly BoardLink[];
  cards: readonly BoardCard[];
  groups?: readonly BoardGroup[];
  /** 已收起的组：成员的对外连线汇聚到组边缘。 */
  foldedGroupIds?: readonly string[];
  activeLinkId?: string | null;
  /** 参与范围（§5.1）。缺省 = 不区分（用户视野）。 */
  includedCardIds?: readonly string[] | null;
  /** true = 只输出可交给 QIO 的线（其余记入 skipped）；默认 false = 用户视野照常显示。 */
  onlyInRange?: boolean;
}

export interface DisplayLinksResult {
  links: DisplayLink[];
  skipped: Array<{ id: string; reason: SkippedLinkReason }>;
}

function foldedGroupOf(
  cardId: string,
  groups: readonly BoardGroup[],
  folded: ReadonlySet<string>,
): BoardGroup | null {
  for (const group of groups) {
    if (group.deleted || !folded.has(group.id)) continue;
    if (group.members.includes(cardId)) return group;
  }
  return null;
}

function endpointLabel(endpoint: ResolvedEndpoint): string {
  if (endpoint.kind === "group") return "组「" + (endpoint.group?.name || "未命名组") + "」";
  return "卡片";
}

function ariaLabelFor(meaning: string, src: ResolvedEndpoint, dst: ResolvedEndpoint): string {
  return "关系「" + meaning + "」；起点：" + endpointLabel(src) + "；终点：" + endpointLabel(dst);
}

/**
 * 计算显示层连线：
 * - 卡片 / 组端点都锚在最终显示边缘；
 * - 组收起时成员端点汇聚到组边缘（真实 src/dst 不变），组内成员之间的线收起期间不画；
 * - 端点不存在 / 已删除 / 自己连自己 / 端点字符串非法 → 如实跳过并给原因；
 * - given includedCardIds 时逐条计算 rangeVisible；onlyInRange=true 时范围外的线整条跳过。
 */
export function buildDisplayLinks(input: DisplayLinksInput): DisplayLinksResult {
  const cards = input.cards ?? [];
  const groups = input.groups ?? [];
  const folded = new Set(input.foldedGroupIds ?? []);
  const baseCtx: EndpointContext = { cards, groups };
  const hasIncludedFilter = Array.isArray(input.includedCardIds);
  const rangeCtx: EndpointContext = hasIncludedFilter
    ? { cards, groups, includedCardIds: input.includedCardIds }
    : baseCtx;

  const links: DisplayLink[] = [];
  const skipped: Array<{ id: string; reason: SkippedLinkReason }> = [];
  const seenPairs = new Set<string>();

  for (const link of input.links ?? []) {
    if (!link || typeof link !== "object" || link.deleted) continue;
    const parsedSrc = parseEndpoint(link.src);
    const parsedDst = parseEndpoint(link.dst);
    if (!parsedSrc.ok || !parsedDst.ok) {
      skipped.push({ id: String(link.id), reason: "invalid-endpoint" });
      continue;
    }
    const src = resolveEndpoint(parsedSrc.endpoint.value, baseCtx);
    const dst = resolveEndpoint(parsedDst.endpoint.value, baseCtx);
    if (!src || !dst) {
      skipped.push({ id: String(link.id), reason: "missing-endpoint" });
      continue;
    }
    if (src.value === dst.value) {
      skipped.push({ id: String(link.id), reason: "self" });
      continue;
    }
    const pair = [src.value, dst.value].sort().join("\u0000");
    if (seenPairs.has(pair)) {
      skipped.push({ id: String(link.id), reason: "duplicate" });
      continue;
    }
    seenPairs.add(pair);

    const srcRange = endpointInVisibleRange(src.value, rangeCtx);
    const dstRange = endpointInVisibleRange(dst.value, rangeCtx);
    const rangeVisible = srcRange.ok && dstRange.ok;
    if (input.onlyInRange && !rangeVisible) {
      skipped.push({ id: String(link.id), reason: "out-of-range" });
      continue;
    }

    const srcFolded = src.kind === "card" ? foldedGroupOf(src.id, groups, folded) : null;
    const dstFolded = dst.kind === "card" ? foldedGroupOf(dst.id, groups, folded) : null;
    const srcGroupDisplay = srcFolded
      ? resolveEndpoint(GROUP_ENDPOINT_PREFIX + srcFolded.id, baseCtx)
      : null;
    const dstGroupDisplay = dstFolded
      ? resolveEndpoint(GROUP_ENDPOINT_PREFIX + dstFolded.id, baseCtx)
      : null;
    const displaySrc = srcGroupDisplay ?? src;
    const displayDst = dstGroupDisplay ?? dst;
    const aggregatedSrc = Boolean(srcGroupDisplay);
    const aggregatedDst = Boolean(dstGroupDisplay);
    if ((aggregatedSrc || aggregatedDst) && displaySrc.value === displayDst.value) {
      skipped.push({ id: String(link.id), reason: "collapsed-internal" });
      continue;
    }

    const anchor = linkAnchor(displaySrc.rect, displayDst.rect);
    const meaning =
      link.meaning.trim() || (link.direction ? "有方向（含义待填写）" : "关联（含义待填写）");
    links.push({
      id: String(link.id),
      src: src.value,
      dst: dst.value,
      srcKind: src.kind,
      dstKind: dst.kind,
      displaySrcId: displaySrc.value,
      displayDstId: displayDst.value,
      aggregatedSrc,
      aggregatedDst,
      x1: anchor.start.x,
      y1: anchor.start.y,
      x2: anchor.end.x,
      y2: anchor.end.y,
      labelX: (anchor.start.x + anchor.end.x) / 2,
      labelY: (anchor.start.y + anchor.end.y) / 2,
      labelW: Math.max(64, meaning.length * 13 + 18),
      curve: {
        x: Number.isFinite(Number(link.curve?.x)) ? Number(link.curve?.x) : 0,
        y: Number.isFinite(Number(link.curve?.y)) ? Number(link.curve?.y) : 0,
      },
      direction: Boolean(link.direction),
      meaning,
      active: input.activeLinkId === link.id,
      rangeVisible,
      ariaLabel: ariaLabelFor(meaning, displaySrc, displayDst),
    });
  }

  return { links, skipped };
}
