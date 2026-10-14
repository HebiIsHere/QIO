/**
 * 板面坐标必须落在画布范围内（2026-10-14 第七个问题）。
 *
 * 现象：卡片可以被拖到板面**顶边之外**（实测某张卡 y = −63.84），它的顶部落在滚动区外，
 * 既看不见也够不到；用户报告「页眉挡住板面上方，一部分卡片被截断，有些卡片还能放进被遮住的地方」。
 * 根因：dropCard 直接写 card.x / card.y，addCard 直接收调用方给的 x / y，updateCard 也不夹取 ——
 * 板面没有任何一处把坐标约束在 [0, 画布尺寸 − 卡片尺寸]。
 */
import { describe, expect, it } from "vitest";
import {
  BOARD_SURFACE_H,
  BOARD_SURFACE_W,
  addCard,
  clampBoardBounds,
  dropCard,
  emptyState,
  updateCard,
} from "../board";
import type { BoardCard, BoardState } from "../types";

function card(patch: Partial<BoardCard> = {}): BoardCard {
  const stamp = "2026-10-14T00:00:00.000Z";
  return {
    id: "c1",
    kind: "text",
    content: "",
    meta: {},
    x: 100,
    y: 100,
    w: 260,
    h: 170,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: stamp,
    updatedAt: stamp,
    ...patch,
  };
}

function state(...cards: BoardCard[]): BoardState {
  return { ...emptyState("board_t"), cards };
}

const pos = (s: BoardState, id = "c1") => {
  const found = s.cards.find((c) => c.id === id);
  return { x: found?.x, y: found?.y };
};

describe("板面坐标夹在画布范围内", () => {
  it("拖到画布左上之外：夹回原点（负坐标会让卡片永远看不见）", () => {
    const next = dropCard(state(card()), "c1", -64, -63.84).state;
    expect(pos(next)).toEqual({ x: 0, y: 0 });
  });

  /*
    2026-10-14（用户要求「板面无限」，方案 B）：右上不再有边界 —— 画布随内容增长
    （BoardCanvas 的 surfaceSize），卡片可以拖到任意远。这里只保证数值仍然有限。
  */
  it("拖到画布右下之外：不再夹回旧画布（画布会跟着长）", () => {
    const next = dropCard(state(card()), "c1", 99999, 99999).state;
    const p = pos(next);
    expect(p.x).toBeGreaterThan(BOARD_SURFACE_W);
    expect(p.y).toBeGreaterThan(BOARD_SURFACE_H);
    expect(Number.isFinite(p.x as number) && Number.isFinite(p.y as number)).toBe(true);
  });

  it("范围内的位置一个字都不改（夹取只在越界时生效）", () => {
    const next = dropCard(state(card()), "c1", 320, 240).state;
    expect(pos(next)).toEqual({ x: 320, y: 240 });
  });

  it("新建卡片：调用方给的负坐标同样被夹住", () => {
    const next = addCard(emptyState("board_t"), { kind: "text", x: -40, y: -10 });
    const created = next.cards[0];
    expect(created.x).toBe(0);
    expect(created.y).toBe(0);
  });

  it("clampBoardBounds：只改越界的卡片，卡片数量与其它字段（seq 等）都保留", () => {
    const raw = {
      boardId: "board_default",
      seq: 121,
      updatedAt: "2026-10-14T01:43:33.086432+00:00",
      cards: [card({ id: "c1", y: -63.84 }), card({ id: "c2", x: 100, y: 100 })],
      groups: [],
      links: [],
      selection: [],
    } as unknown as BoardState;
    const out = clampBoardBounds(raw);
    expect(out.cards).toHaveLength(2);
    expect(out.cards[0].y).toBe(0);
    expect(out.cards[1].y).toBe(100);
    expect((out as unknown as { seq: number }).seq).toBe(121);
    // 没有越界卡片时必须原样返回（不改对象、不改字段）
    const clean = { ...raw, cards: [card({ id: "c2", x: 100, y: 100 })] } as unknown as BoardState;
    expect(clampBoardBounds(clean)).toBe(clean);
  });

  it("updateCard 也不能把卡片挪出画布（展开让位 / 尺寸调整走同一条路）", () => {
    const moved = updateCard(state(card()), "c1", { x: -5, y: -5 });
    expect(pos(moved)).toEqual({ x: 0, y: 0 });
    const resized = updateCard(state(card()), "c1", { w: BOARD_SURFACE_W + 500, h: BOARD_SURFACE_H + 500 });
    const grown = resized.cards.find((c) => c.id === "c1")!;
    // 2026-10-14：右上不再夹取 —— 尺寸可以超出旧画布（画布会跟着长），只保证数值有限
    expect(grown.w).toBe(BOARD_SURFACE_W + 500);
    expect(Number.isFinite(grown.x + grown.w) && Number.isFinite(grown.y + grown.h)).toBe(true);
  });
});
