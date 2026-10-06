/**
 * 板面操作语义（纯函数）：卡片、选择、拖动、分组、顺序、关系链接。
 *
 * 契约：docs/interactive-mode-contract.md §1.2 / §1.3 / §4.2。
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
import type { BoardCard, BoardGroup, BoardLink, BoardState, CardKind } from "./types";

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
  image: "图片",
  code: "代码",
  url: "网址",
  reply: "QIO 结果",
};

/** 材料类卡片：添加材料本身不等于要求总结 / 比较 / 修改 / 执行。 */
export const MATERIAL_KINDS: CardKind[] = ["file", "image", "code", "url"];

/** 需要勾选才允许 QIO 查看的卡片：文字注释（材料默认在范围内）。 */
export const CHECKABLE_KINDS: CardKind[] = ["text"];

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

const DEFAULT_GROUP_PREFIX = "组";
const DEFAULT_NAME_RE = /^组\s*(\d+)$/;

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

function isDefaultName(name: string): boolean {
  return DEFAULT_NAME_RE.test(name);
}

/** 下一个没人用过的默认组名「组 N」；N 只增不减，避免重名。 */
export function nextDefaultGroupName(taken: Iterable<string>): string {
  let highest = 0;
  for (const name of taken) {
    const match = DEFAULT_NAME_RE.exec(String(name ?? ""));
    if (match) highest = Math.max(highest, Number(match[1]));
  }
  return DEFAULT_GROUP_PREFIX + " " + String(highest + 1);
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

function normalizeCard(raw: unknown, stamp: string): BoardCard | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Partial<BoardCard> & Record<string, unknown>;
  const kind = (["text", "file", "image", "code", "url", "reply"] as CardKind[]).includes(
    source.kind as CardKind,
  )
    ? (source.kind as CardKind)
    : "text";
  const checked = Boolean(source.checked);
  const hidden = Boolean(source.hidden);
  return {
    id: String(source.id ?? "") || newId("c"),
    kind,
    content: asText(source.content),
    meta: source.meta && typeof source.meta === "object" ? { ...(source.meta as Record<string, unknown>) } : {},
    x: asFloat(source.x, 0),
    y: asFloat(source.y, 0),
    w: Math.max(1, asFloat(source.w, DEFAULT_CARD_W)),
    h: Math.max(1, asFloat(source.h, DEFAULT_CARD_H)),
    // G6 明确隐藏与勾选互斥；G7 reply 不参与注释勾选
    checked: hidden || kind === "reply" ? false : checked,
    hidden,
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
  let name = asText(source.name).trim();
  let defaultName = typeof source.defaultName === "boolean" ? source.defaultName : isDefaultName(name);
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
    deleted: false,
    createdAt: asText(source.createdAt) || ctx.stamp,
    updatedAt: asText(source.updatedAt) || asText(source.createdAt) || ctx.stamp,
  };
  reflowGroup(group, ctx.cards);
  return group;
}

function normalizeLink(raw: unknown, live: Set<string>, stamp: string): BoardLink | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Partial<BoardLink> & Record<string, unknown>;
  if (source.deleted) return null;
  const src = String(source.src ?? "");
  const dst = String(source.dst ?? "");
  // G4 端点必须存在且未删除（G5 已删除卡片不进链接）
  if (!live.has(src) || !live.has(dst)) return null;
  return {
    id: String(source.id ?? "") || newId("l"),
    src,
    dst,
    direction: Boolean(source.direction),
    meaning: asText(source.meaning),
    deleted: false,
    createdAt: asText(source.createdAt) || stamp,
    updatedAt: asText(source.updatedAt) || asText(source.createdAt) || stamp,
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
  for (const raw of (source.links as unknown[]) ?? []) {
    const link = normalizeLink(raw, live, stamp);
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
  work.cards.push({
    id: String(card.id ?? "") || newId("c"),
    kind: card.kind,
    content: asText(card.content),
    meta: card.meta && typeof card.meta === "object" ? { ...card.meta } : {},
    x: asFloat(card.x, defaultX),
    y: asFloat(card.y, defaultY),
    w: Math.max(1, asFloat(card.w, DEFAULT_CARD_W)),
    h: Math.max(1, asFloat(card.h, DEFAULT_CARD_H)),
    checked: card.kind === "reply" ? false : Boolean(card.checked),
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
  if (patch.checked !== undefined) assign("checked", Boolean(patch.checked));
  if (patch.hidden !== undefined) assign("hidden", Boolean(patch.hidden));
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
    checked: false,
    hidden: false,
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
    defaultName: !cleaned || isDefaultName(cleaned),
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

/** 解除组：成员全部恢复自由摆放，卡片本身不动。 */
export function dissolveGroup(state: BoardState, groupId: string): BoardState {
  const work = normalizeState(state);
  const group = groupById(work, groupId);
  if (!group) return work;
  group.members = [];
  group.deleted = true;
  work.updatedAt = nowIso();
  return normalizeState(work);
}

/** 改组名。留空不生效；名字是否还是默认名由名字本身决定（保留默认名也能提交）。 */
export function renameGroup(state: BoardState, groupId: string, name: string): BoardState {
  const work = normalizeState(state);
  const group = groupById(work, groupId);
  if (!group) return work;
  const cleaned = asText(name).trim();
  if (!cleaned || cleaned === group.name) return work;
  group.name = cleaned;
  group.defaultName = isDefaultName(cleaned);
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
 * 合并两个组：目标组保留 id 但换成默认名（原组不再独立保留）。
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
  if (src === dst) return work;
  const live = new Set(work.cards.filter((card) => !card.deleted).map((card) => card.id));
  if (!live.has(src) || !live.has(dst)) return work;
  const stamp = nowIso();
  const pair = [src, dst].sort().join("\u0000");
  const existing = work.links.find((link) => [link.src, link.dst].sort().join("\u0000") === pair);
  if (existing) {
    existing.direction = Boolean(direction);
    existing.meaning = asText(meaning);
    existing.updatedAt = stamp;
  } else {
    work.links.push({
      id: newId("l"),
      src,
      dst,
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
  let changed = false;
  if (patch.src !== undefined && String(patch.src) !== link.src) {
    link.src = String(patch.src);
    changed = true;
  }
  if (patch.dst !== undefined && String(patch.dst) !== link.dst) {
    link.dst = String(patch.dst);
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
  if (card.checked === value && !(value && card.hidden)) return work;
  card.checked = value;
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
  if (card.hidden) card.checked = false;
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
 */
function overlappingFreeCards(state: BoardState, cardId: string, x: number, y: number): BoardCard[] {
  const card = cardById(state, cardId);
  if (!card) return [];
  const rect: BoardRect = { x, y, w: Math.max(1, card.w), h: Math.max(1, card.h) };
  return state.cards.filter(
    (other) =>
      other.id !== cardId &&
      !other.deleted &&
      !groupOfCard(state, other.id) &&
      rectsOverlap(rect, cardRect(other)),
  );
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
  const partners = overlappingFreeCards(state, cardId, x, y);
  return { target: null, index: null, mergesWith: partners.length ? partners[0].id : null };
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
export function dropCard(state: BoardState, cardId: string, x: number, y: number): DropResult {
  const px = asFloat(x, 0);
  const py = asFloat(y, 0);
  let work = normalizeState(state);
  const card = cardById(work, cardId);
  if (!card || card.deleted) {
    return { state: work, groupId: null, merged: false, index: null };
  }

  const resolved = resolveDrop(work, cardId, px, py);
  const stamp = nowIso();
  card.x = px;
  card.y = py;
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
    const partners = overlappingFreeCards(work, cardId, px, py);
    if (partners.length) {
      // 两张（或多张）未分组卡片重叠 → 自动成组，默认组名
      work.groups.push({
        id: newId("g"),
        name: nextDefaultGroupName(work.groups.map((item) => item.name)),
        defaultName: true,
        ordered: false,
        x: px,
        y: py,
        w: Math.max(1, card.w + GROUP_PAD_X * 2),
        h: Math.max(1, card.h + GROUP_PAD_TOP + GROUP_PAD_BOTTOM),
        members: [...partners.map((item) => item.id), cardId],
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
