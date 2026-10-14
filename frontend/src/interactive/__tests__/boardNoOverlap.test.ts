/**
 * 卡片不允许叠放（2026-10-14 用户要求，方案 A：落点被占就挪到最近空位）。
 *
 * 以前拖到别的卡上、又不成组时会直接压上去；现在 dropCard 会先找空位。
 */
import { describe, expect, it } from "vitest";
import { dropCard, emptyState, freeDropSpot, normalizeState } from "../board";
import type { BoardCard, BoardState } from "../types";

const STAMP = "2026-10-14T00:00:00.000Z";
function card(id: string, x: number, y: number, w = 260, h = 170): BoardCard {
  return { id, kind: "text", content: "", meta: {}, x, y, w, h, checked: false, hidden: false, folded: false, bookmarked: false, deleted: false, createdAt: STAMP, updatedAt: STAMP };
}
function state(...cards: BoardCard[]): BoardState {
  return normalizeState({ ...emptyState("board_t"), cards });
}
function overlaps(a: BoardCard, b: BoardCard): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

describe("卡片不允许叠放", () => {
  it("落点被占（且不会成组）：自动挪到不与别人相交的位置，且不改变别的卡片", () => {
    // 目标卡已在组里 → 不触发「两张未分组卡片重叠自动成组」那条路径，才轮到让位
    const base = state(card("a", 0, 0), card("b", 400, 0));
    // 落点与 b 只重叠几像素：不到「明确重叠自动成组」的程度，也不指向某张卡 → 该让位
    const before = base;
    const next = dropCard(before, "a", 655, 0).state;
    const a = next.cards.find((c) => c.id === "a")!;
    const b = next.cards.find((c) => c.id === "b")!;
    expect(overlaps(a, b), "落下后不许压住别的卡片").toBe(false);
    expect({ x: b.x, y: b.y }, "别的卡片不许被挪动").toEqual({ x: 400, y: 0 });
    // 挪动幅度要小：最近的空位就在旁边
    expect(Math.abs(a.x - 400) + Math.abs(a.y - 0)).toBeLessThanOrEqual(400);
  });

  it("落点是空的：一个像素都不改", () => {
    const next = dropCard(state(card("a", 0, 0), card("b", 400, 0)), "a", 900, 300).state;
    const a = next.cards.find((c) => c.id === "a")!;
    expect({ x: a.x, y: a.y }).toEqual({ x: 900, y: 300 });
  });

  it("freeDropSpot 是纯函数：不改入参状态", () => {
    const before = state(card("a", 0, 0), card("b", 400, 0));
    const snapshot = JSON.stringify(before);
    freeDropSpot(before, "a", 400, 0);
    expect(JSON.stringify(before)).toBe(snapshot);
  });

  it("整个画布都被占满时如实返回原落点（不假装让开了）", () => {
    // 一张铺满画布的卡片：任何位置都相交 —— 找不到就如实返回落点，不许硬塞
    const spot = freeDropSpot(state(card("a", 0, 0), card("wall", 0, 0, 2400, 1600)), "a", 400, 0);
    expect(spot).toEqual({ x: 400, y: 0 });
  });
});
