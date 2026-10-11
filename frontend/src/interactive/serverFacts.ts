/**
 * 服务器事实与本地候选的版本合并（F3/N5/F07 的唯一口径）。
 *
 * 板面同时存在两种东西：
 * - 服务器上的「已保存事实」（`seq` 是唯一版本号）；
 * - 用户正在编辑的「候选」（可能基于更旧的 `seq`）。
 *
 * 另一页面、提交清理、N4 撤回都可能让服务器前进。这时既不能整块覆盖候选
 * （会丢用户的输入），也不能为了保护候选连服务器事实都不接收（基线 F3 的真实缺陷：
 * 候选永远带着旧 seq，保存一直被预判/校验拒绝，用户点一次失败一次）。
 *
 * 合并口径（对每个对象按字段做三路比较：base = 上一次与服务器一致的快照）：
 * - 本地没改、服务器改了       → 取服务器（独立变化被协调进来）；
 * - 本地改了、服务器没改       → 取本地（用户自己的编辑不丢）；
 * - 两边都改                   → 布局/选择类字段以本地为准（用户正在动的这一版最新），
 *                                正文（content）记成**内容冲突**交用户决定，绝不静默挑一个；
 * - 只有一边存在的对象         → 都保留（另一页面新增的对象、本页新建的卡片都不丢）。
 *
 * F07（本地删除的组和关联被恢复）：组/关联被用户解散、删除后，`normalizeState` 会让它们
 * **从数组里消失**（不是留一个 deleted 标记）—— 「本地没有」于是与「服务器新增的」形状相同。
 * 只按形状无法区分，必须用 base 三路判定：
 * - base 有、local 没有、server 与 base 相同 → **本地删除成立**，保持删除（不复活）；
 * - base 有、local 没有、server 与 base 不同 → 删除与对方的实质修改相撞：
 *   保持本地删除，同时把服务器那一份作为**依据**登记到 removalConflicts，等用户明确选择；
 * - base 没有、local 没有、server 有   → 服务器新增的对象，保留（绝不误删）。
 *
 * 本模块是纯函数：不碰 store、不发请求、不改存储，合并规则可以被独立测试。
 */
import type { BoardCard, BoardGroup, BoardLink, BoardState } from "./types";

export interface BoardContentConflict {
  cardId: string;
  local: string;
  server: string;
}

/**
 * F07：本地删除与服务器对同一对象的实质修改相撞。
 *
 * 这个形状就是「保留依据」：服务器的完整那一份留在 `server` 里，用户可以选择
 * 「保持删除」（放弃对方的修改）或「恢复服务器版」（放弃本地删除），
 * 两种选择都必须是用户明确做出的 —— 既不静默复活，也不悄悄丢掉对方的修改。
 */
export interface RemovalConflict {
  kind: "group" | "link";
  id: string;
  /** 本地在 base 之后删掉了它 */
  local: "removed";
  /** 服务器上它现在是这一份（被对方改过） */
  server: Record<string, unknown>;
}

export interface MergeOutcome {
  state: BoardState;
  /** 同一张卡片的正文两边都改了：必须由用户决定保留哪一份（不静默覆盖） */
  conflicts: BoardContentConflict[];
  /** F07：本地删除与服务器修改相撞：必须由用户决定删除还是恢复（不静默复活） */
  removalConflicts: RemovalConflict[];
}

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

type AnyRecord = Record<string, unknown>;

interface MergeContext {
  conflicts: BoardContentConflict[];
  removalConflicts: RemovalConflict[];
  kind: "card" | "group" | "link";
}

/** 本地在 base 之后改过哪些字段（服务器没改这些字段时直接取本地）。 */
function localChangedFields(base: AnyRecord | undefined, local: AnyRecord): string[] {
  const changed: string[] = [];
  for (const key of Object.keys(local)) {
    if (key === "id") continue;
    if (!base || !same(base[key], local[key])) changed.push(key);
  }
  return changed;
}

/** 两边都改过的对象：逐字段决定（本地改过的字段取本地），正文冲突单独登记。 */
function mergeBothChanged(
  base: AnyRecord | undefined,
  local: AnyRecord,
  server: AnyRecord,
): AnyRecord {
  const merged = { ...server };
  for (const key of localChangedFields(base, local)) {
    merged[key] = local[key];
  }
  return merged;
}

/**
 * F07：本地删除（对象已不在本地数组里）时的三路判定。
 *
 * 返回 undefined = 这个对象在合并结果里不出现（本地删除成立）；
 * 只有服务器新增（base 没有）才把它带回来。
 */
function mergeRemoved<T extends { id: string }>(
  base: AnyRecord | undefined,
  server: AnyRecord | undefined,
  ctx: MergeContext,
): T | undefined {
  if (!server) return undefined;
  if (!base) return server as unknown as T;
  if (same(base, server)) return undefined;
  // 删除与对方的实质修改相撞：保留依据（服务器那一份），等用户明确选择
  ctx.removalConflicts.push({
    kind: ctx.kind === "group" ? "group" : "link",
    id: String(server.id ?? ""),
    local: "removed",
    server: { ...server },
  });
  return undefined;
}

function mergeCard(
  base: BoardCard | undefined,
  local: BoardCard | undefined,
  server: BoardCard | undefined,
  ctx: MergeContext,
): BoardCard | undefined {
  if (!local && !server) return undefined;
  // 只有服务器有 / 只有本地有：保留存在的那一份（另一页面新增的对象不能丢）
  if (!local) return server;
  if (!server) return base && same(base, local) ? undefined : local;
  if (same(base, local)) return server;
  if (same(base, server)) return local;
  // 两边都改了：逐字段决定；正文冲突登记下来交用户
  const merged = mergeBothChanged(base as unknown as AnyRecord | undefined, local as unknown as AnyRecord, server as unknown as AnyRecord);
  const baseContent = base ? base.content : undefined;
  const contentBothChanged = !same(baseContent, local.content) && !same(baseContent, server.content);
  if (contentBothChanged) {
    ctx.conflicts.push({ cardId: local.id, local: local.content, server: server.content });
  }
  return merged as unknown as BoardCard;
}

function mergeGroup(
  base: BoardGroup | undefined,
  local: BoardGroup | undefined,
  server: BoardGroup | undefined,
  ctx: MergeContext,
): BoardGroup | undefined {
  if (!local && !server) return undefined;
  if (!local) {
    return mergeRemoved<BoardGroup>(
      base as unknown as AnyRecord | undefined,
      server as unknown as AnyRecord | undefined,
      ctx,
    );
  }
  if (!server) return base && same(base, local) ? undefined : local;
  if (same(base, local)) return server;
  if (same(base, server)) return local;
  return mergeBothChanged(
    base as unknown as AnyRecord | undefined,
    local as unknown as AnyRecord,
    server as unknown as AnyRecord,
  ) as unknown as BoardGroup;
}

function mergeLink(
  base: BoardLink | undefined,
  local: BoardLink | undefined,
  server: BoardLink | undefined,
  ctx: MergeContext,
): BoardLink | undefined {
  if (!local && !server) return undefined;
  if (!local) {
    return mergeRemoved<BoardLink>(
      base as unknown as AnyRecord | undefined,
      server as unknown as AnyRecord | undefined,
      ctx,
    );
  }
  if (!server) return base && same(base, local) ? undefined : local;
  if (same(base, local)) return server;
  if (same(base, server)) return local;
  return mergeBothChanged(
    base as unknown as AnyRecord | undefined,
    local as unknown as AnyRecord,
    server as unknown as AnyRecord,
  ) as unknown as BoardLink;
}

function mergeList<T extends { id: string }>(
  base: T[] | undefined,
  local: T[],
  server: T[],
  merge: (b: T | undefined, l: T | undefined, s: T | undefined, ctx: MergeContext) => T | undefined,
  ctx: MergeContext,
): T[] {
  const baseById = new Map((base ?? []).map((item) => [item.id, item]));
  const localById = new Map(local.map((item) => [item.id, item]));
  const serverById = new Map(server.map((item) => [item.id, item]));
  // 顺序：先按服务器顺序（已保存事实的顺序是用户看到过的），再补上本页新建的
  const ids: string[] = server.map((item) => item.id);
  for (const item of local) if (!serverById.has(item.id)) ids.push(item.id);
  for (const item of base ?? []) if (!serverById.has(item.id) && !localById.has(item.id)) ids.push(item.id);
  const out: T[] = [];
  for (const id of ids) {
    const merged = merge(baseById.get(id), localById.get(id), serverById.get(id), ctx);
    if (merged) out.push(merged);
  }
  return out;
}

/**
 * 把服务器事实安全合并进本地候选。
 *
 * `base` 传「上一次与服务器一致的快照」（store 的 cleanState）：没有它就无法判断
 * 某一边到底是「改过」还是「没动过」，退化成整块覆盖（正是要避免的）。
 */
export function mergeServerInto(
  local: BoardState,
  server: BoardState,
  base: BoardState | null,
): MergeOutcome {
  const ctx: MergeContext = { conflicts: [], removalConflicts: [], kind: "card" };
  const cards = mergeList(base?.cards, local.cards ?? [], server.cards ?? [], mergeCard, ctx);
  ctx.kind = "group";
  const groups = mergeList(base?.groups, local.groups ?? [], server.groups ?? [], mergeGroup, ctx);
  ctx.kind = "link";
  const links = mergeList(base?.links, local.links ?? [], server.links ?? [], mergeLink, ctx);
  // 选择是「查看/提交范围」的即时状态：本地动过就以本地为准，否则跟随服务器
  const localSelectionChanged = !base || !same(base.selection, local.selection);
  return {
    state: {
      boardId: server.boardId || local.boardId,
      seq: server.seq,
      updatedAt: server.updatedAt || local.updatedAt,
      cards,
      groups,
      links,
      selection: localSelectionChanged ? (local.selection ?? []) : (server.selection ?? []),
    },
    conflicts: ctx.conflicts,
    removalConflicts: ctx.removalConflicts,
  };
}
