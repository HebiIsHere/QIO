/**
 * 服务器事实与本地候选的版本合并（本轮 F3/N5 的唯一口径，Lead 维护）。
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
 * 本模块是纯函数：不碰 store、不发请求、不改存储，合并规则可以被独立测试。
 */
import type { BoardCard, BoardGroup, BoardLink, BoardState } from "./types";

export interface BoardContentConflict {
  cardId: string;
  local: string;
  server: string;
}

export interface MergeOutcome {
  state: BoardState;
  /** 同一张卡片的正文两边都改了：必须由用户决定保留哪一份（不静默覆盖） */
  conflicts: BoardContentConflict[];
}

const same = (a: unknown, b: unknown): boolean => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

type AnyRecord = Record<string, unknown>;

/** 本地在 base 之后改过哪些字段（服务器没改这些字段时直接取本地）。 */
function localChangedFields(base: AnyRecord | undefined, local: AnyRecord): string[] {
  const changed: string[] = [];
  for (const key of Object.keys(local)) {
    if (key === "id") continue;
    if (!base || !same(base[key], local[key])) changed.push(key);
  }
  return changed;
}

function mergeCard(
  base: BoardCard | undefined,
  local: BoardCard | undefined,
  server: BoardCard | undefined,
  conflicts: BoardContentConflict[],
): BoardCard | undefined {
  if (!local && !server) return undefined;
  // 只有服务器有 / 只有本地有：保留存在的那一份（另一页面新增的对象不能丢）
  if (!local) return server;
  if (!server) return base && same(base, local) ? undefined : local;
  if (same(base, local)) return server;
  if (same(base, server)) return local;
  // 两边都改了：逐字段决定；正文冲突登记下来交用户
  const merged = { ...server } as unknown as AnyRecord;
  const localRec = local as unknown as AnyRecord;
  for (const key of localChangedFields(base as unknown as AnyRecord | undefined, localRec)) {
    merged[key] = localRec[key];
  }
  const baseContent = base ? base.content : undefined;
  const contentBothChanged = !same(baseContent, local.content) && !same(baseContent, server.content);
  if (contentBothChanged) {
    conflicts.push({ cardId: local.id, local: local.content, server: server.content });
  }
  return merged as unknown as BoardCard;
}

function mergeGroup(
  base: BoardGroup | undefined,
  local: BoardGroup | undefined,
  server: BoardGroup | undefined,
): BoardGroup | undefined {
  if (!local && !server) return undefined;
  if (!local) return server;
  if (!server) return base && same(base, local) ? undefined : local;
  if (same(base, local)) return server;
  if (same(base, server)) return local;
  const merged = { ...server } as unknown as AnyRecord;
  const localRec = local as unknown as AnyRecord;
  for (const key of localChangedFields(base as unknown as AnyRecord | undefined, localRec)) {
    merged[key] = localRec[key];
  }
  return merged as unknown as BoardGroup;
}

function mergeLink(
  base: BoardLink | undefined,
  local: BoardLink | undefined,
  server: BoardLink | undefined,
): BoardLink | undefined {
  if (!local && !server) return undefined;
  if (!local) return server;
  if (!server) return base && same(base, local) ? undefined : local;
  if (same(base, local)) return server;
  if (same(base, server)) return local;
  const merged = { ...server } as unknown as AnyRecord;
  const localRec = local as unknown as AnyRecord;
  for (const key of localChangedFields(base as unknown as AnyRecord | undefined, localRec)) {
    merged[key] = localRec[key];
  }
  return merged as unknown as BoardLink;
}

function mergeList<T extends { id: string }>(
  base: T[] | undefined,
  local: T[],
  server: T[],
  merge: (b: T | undefined, l: T | undefined, s: T | undefined, conflicts: BoardContentConflict[]) => T | undefined,
  conflicts: BoardContentConflict[],
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
    const merged = merge(baseById.get(id), localById.get(id), serverById.get(id), conflicts);
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
  const conflicts: BoardContentConflict[] = [];
  const cards = mergeList(base?.cards, local.cards ?? [], server.cards ?? [], mergeCard, conflicts);
  const groups = mergeList(base?.groups, local.groups ?? [], server.groups ?? [], mergeGroup, conflicts);
  const links = mergeList(base?.links, local.links ?? [], server.links ?? [], mergeLink, conflicts);
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
    conflicts,
  };
}
