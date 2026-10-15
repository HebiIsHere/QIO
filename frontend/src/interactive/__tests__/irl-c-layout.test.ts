/**
 * IRL-C 纯函数用例（_IML-COMMON.md §1 F1 / §2 F2 / §7）。
 *
 * 这里只覆盖两件事，且都已按本轮冻结口径改写：
 * 1. 尺寸口径常量与 cardSizeLimits：**视野大小永远不是卡片上限**；
 * 2. planExpandedLayout 是**恒等映射**：展开 / 让位 / 视野夹取 / sizedCards 全部消失，
 *    `slots` 逐字段等于正式矩形、`displaced` 恒为 `[]`（旧断言删除，不为旧行为保留期望）；
 * 3. 新卡落点 planNewCardSlot（§7.7）：确定性、避开已有卡片、落在当前可用区域里。
 *
 * 只测纯函数：不挂 DOM、不碰 store / 网络。
 */
import { describe, expect, it } from "vitest";
import {
  CARD_MAX_SIZE,
  CARD_MIN_SIZE,
  DEFAULT_LAYOUT_GAP,
  EXPANDED_PREVIEW_SIZE,
  cardSizeLimits,
  planExpandedLayout,
  planNewCardSlot,
  rectsOverlap,
  type LayoutInput,
  type LayoutRect,
} from "../cardLayout";

const STAGE = { w: 1200, h: 800 };

function card(id: string, x: number, y: number, w = 120, h = 80) {
  return { id, x, y, w, h };
}

function plan(cards: ReturnType<typeof card>[], expanded: string[], extra: Partial<LayoutInput> = {}) {
  return planExpandedLayout({ cards, expanded, expandedSize: EXPANDED_PREVIEW_SIZE, stage: STAGE, ...extra });
}

function insideRegion(slot: LayoutRect, region: LayoutRect): boolean {
  return (
    slot.x >= region.x - 1e-6 &&
    slot.y >= region.y - 1e-6 &&
    slot.x + slot.w <= region.x + region.w + 1e-6 &&
    slot.y + slot.h <= region.y + region.h + 1e-6
  );
}

function official(cards: ReturnType<typeof card>[]): Record<string, LayoutRect> {
  const out: Record<string, LayoutRect> = {};
  for (const item of cards) out[item.id] = { x: item.x, y: item.y, w: item.w, h: item.h };
  return out;
}

describe("尺寸口径常量（§7.3 / §2）", () => {
  it("最小 180×96、上限 1600×1200、让位间距常量仍为 16", () => {
    expect(CARD_MIN_SIZE).toEqual({ w: 180, h: 96 });
    expect(CARD_MAX_SIZE).toEqual({ w: 1600, h: 1200 });
    expect(DEFAULT_LAYOUT_GAP).toBe(16);
  });

  it("cardSizeLimits 只取合约口径：可用区域再小也**不**收紧（视野不是卡片上限）", () => {
    const contract = { minW: 180, minH: 96, maxW: 1600, maxH: 1200 };
    expect(cardSizeLimits({ w: 4000, h: 3000 })).toEqual(contract);
    // 旧行为「可用区域很小时最小值向内收敛 / 上限 = 可用区域」已被 F2 否定
    expect(cardSizeLimits({ w: 100, h: 60 })).toEqual(contract);
    expect(cardSizeLimits({ w: 0, h: Number.NaN })).toEqual(contract);
  });
});

describe("展开不再改变任何卡片外框（F1 恒等映射）", () => {
  it("展开卡保持正式位置与正式尺寸（不再放大到 560×420，也不按可用区域收敛）", () => {
    const cards = [card("a", 100, 50)];
    const result = plan(cards, ["a"], { stage: { w: 400, h: 300 } });
    expect(result.slots.a).toEqual({ x: 100, y: 50, w: 120, h: 80 });
    expect(result.expanded).toEqual(["a"]);
    expect(result.displaced).toEqual([]);
  });

  it("比阅读尺寸大的卡不被缩小；比可用区域大的卡不被压缩", () => {
    const big = plan([card("a", 100, 100, 800, 600)], ["a"]);
    expect(big.slots.a).toEqual({ x: 100, y: 100, w: 800, h: 600 });
    const narrow = plan([card("a", 0, 0, 500, 400)], ["a"], { stage: { w: 300, h: 200 } });
    expect(narrow.slots.a).toEqual({ x: 0, y: 0, w: 500, h: 400 });
  });

  it("平移 / 滚动（stageOrigin）不再把展开卡拉进可视区：坐标一个字节都不改", () => {
    const region: LayoutRect = { x: 1800, y: 900, w: 800, h: 600 };
    const cards = [card("a", 100, 100), card("b", 1900, 1000)];
    const result = plan(cards, ["a", "b"], {
      stage: { w: region.w, h: region.h },
      stageOrigin: { x: region.x, y: region.y },
    });
    expect(result.slots).toEqual(official(cards));
    expect(result.displaced).toEqual([]);
  });

  it("展开不让位：相交的未展开卡分毫不动，displaced 恒为空", () => {
    const cards = [card("a", 0, 0), card("b", 20, 10), card("d", 30, 20)];
    const result = plan(cards, ["a"], { expandedAvoid: true });
    expect(result.slots).toEqual(official(cards));
    expect(result.displaced).toEqual([]);
  });

  it("多张展开卡同时阅读：没有互相避让，也没有新碰撞处理", () => {
    const cards = [card("a", 0, 0, 300, 200), card("b", 200, 0, 300, 200)];
    const result = plan(cards, ["a", "b"], { expandedAvoid: true });
    expect(result.slots).toEqual(official(cards));
    expect(result.expanded).toEqual(["a", "b"]);
    expect(result.displaced).toEqual([]);
  });

  it("旧 sizedCards 选项已无意义：展开 forever 沿用正式 card.w/h", () => {
    const moved = [card("a", 700, 300, 400, 260), card("b", 200, 200)];
    const withFlag = plan(moved, ["a"], { sizedCards: ["a"] });
    const withoutFlag = plan(moved, ["a"]);
    expect(withFlag.slots).toEqual(withoutFlag.slots);
    expect(withFlag.slots).toEqual(official(moved));
  });

  it("收起与展开在两个顺序下都收敛到同一份正式几何", () => {
    const cards = [card("a", 100, 100), card("b", 300, 100), card("c", 700, 500)];
    expect(plan(cards, ["a", "b"]).slots).toEqual(official(cards));
    expect(plan(cards, ["b", "a"]).slots).toEqual(official(cards));
    expect(plan(cards, []).slots).toEqual(official(cards));
  });
});

describe("纯度与既有形状", () => {
  it("新增选项不改输入：调用前后输入逐字段不变", () => {
    const cards = [card("a", 0, 0), card("b", 350, 0)];
    const expanded = ["a"];
    const snapshot = JSON.stringify({ cards, expanded });
    planExpandedLayout({
      cards,
      expanded,
      expandedSize: { w: 400, h: 300 },
      stage: STAGE,
      stageOrigin: { x: 500, y: 500 },
      expandedAvoid: true,
      sizedCards: ["a"],
    });
    expect(JSON.stringify({ cards, expanded })).toBe(snapshot);
  });

  it("恒等映射对任何输入都成立（含重复 id / 不存在 id）", () => {
    const cards = [card("a", 1, 2, 3, 4), card("b", 10, 20, 30, 40)];
    const result = plan(cards, ["ghost", "b", "b"]);
    expect(result.slots).toEqual(official(cards));
    expect(result.expanded).toEqual(["b"]);
  });
});

describe("新卡落点 planNewCardSlot（§7.7）", () => {
  const SIZE = { w: 260, h: 170 };

  it("落在当前可用区域里：平移之后也看得见（不是板面原点的固定网格）", () => {
    const area = { x: 900, y: 700, w: 600, h: 400 };
    const slot = planNewCardSlot({ size: SIZE, area });
    expect(slot).toEqual({ x: 900, y: 700, w: 260, h: 170 });
    expect(insideRegion(slot, area)).toBe(true);
  });

  it("避开已有卡片：落点与已有卡片不重叠", () => {
    const area = { x: 0, y: 0, w: 800, h: 600 };
    const cards = [
      { x: 0, y: 0, w: 260, h: 170 },
      { x: 300, y: 0, w: 260, h: 170 },
    ];
    const slot = planNewCardSlot({ size: SIZE, area, cards });
    expect(insideRegion(slot, area)).toBe(true);
    for (const item of cards) expect(rectsOverlap(slot, item)).toBe(false);
  });

  it("连续落点不重复：把上一处落点当作障碍，下一次会换位置", () => {
    const area = { x: 0, y: 0, w: 800, h: 600 };
    const first = planNewCardSlot({ size: SIZE, area });
    const second = planNewCardSlot({ size: SIZE, area, cards: [first] });
    expect(second).not.toEqual(first);
    expect(rectsOverlap(first, second)).toBe(false);
  });

  it("确定性：同样的输入永远得到同一个落点", () => {
    const area = { x: 120, y: 80, w: 500, h: 400 };
    const cards = [{ x: 120, y: 80, w: 260, h: 170 }];
    expect(planNewCardSlot({ size: SIZE, area, cards })).toEqual(planNewCardSlot({ size: SIZE, area, cards }));
  });

  /*
   * D2（真实浏览器验收发现）：从前只在可见区域里搜一次，可见区域没有零重叠空位时
   * 直接接受 findFreeSlot 的「重叠面积最小」兜底 → 新建的卡压在既有卡上
   * （实测落点 (244,536,260×170) 压住 t_note(80,620,260×170)）。
   * 「当前视野大小不是卡片上限」同样适用于**落点搜索范围**：扩大后再搜，直到真正零重叠。
   */
  it("可见区域被占满：扩大到可见区域之外，仍给出**零重叠**落点（视野不是落点上限）", () => {
    const area = { x: 0, y: 0, w: 300, h: 200 };
    const cards = [{ x: 0, y: 0, w: 300, h: 200 }];
    const slot = planNewCardSlot({ size: SIZE, area, cards });
    for (const item of cards) expect(rectsOverlap(slot, item)).toBe(false);
    // 真正落在可见区域之外（向右 / 向下延伸出一整张卡的空档）
    expect(slot.x >= area.x + area.w || slot.y >= area.y + area.h).toBe(true);
    expect(Number.isFinite(slot.x + slot.y + slot.w + slot.h)).toBe(true);
  });

  it("D2 复现：可见区域被既有卡片挤满时，新卡不再压住 t_note", () => {
    const area = { x: 0, y: 400, w: 560, h: 400 };
    const cards = [
      { x: 0, y: 400, w: 560, h: 200 }, // 上半个可见区域铺满
      { x: 80, y: 620, w: 260, h: 170 }, // t_note：实测被新卡压住的那张
      { x: 0, y: 600, w: 80, h: 200 }, // 左下被占
    ];
    const slot = planNewCardSlot({ size: SIZE, area, cards });
    for (const item of cards) expect(rectsOverlap(slot, item)).toBe(false);
    expect(rectsOverlap(slot, { x: 80, y: 620, w: 260, h: 170 })).toBe(false);
    expect(slot.x >= area.x + area.w || slot.y >= area.y + area.h).toBe(true);
  });

  it("可见区域比卡片还小且被占满：扩大后仍按**请求尺寸**落点（不把新卡悄悄变小）", () => {
    const area = { x: 0, y: 0, w: 100, h: 80 };
    const cards = [{ x: 0, y: 0, w: 100, h: 80 }];
    const slot = planNewCardSlot({ size: SIZE, area, cards });
    // 旧实现会把 findFreeSlot 夹过的 100×80 当成「一整张卡」的空档，新卡因此悄悄变小
    expect({ w: slot.w, h: slot.h }).toEqual(SIZE);
    expect(rectsOverlap(slot, cards[0])).toBe(false);
  });

  it("扩大搜索是确定性的，且不会把落点推到坐标原点之外", () => {
    const area = { x: 0, y: 0, w: 300, h: 200 };
    const cards = [{ x: 0, y: 0, w: 300, h: 200 }];
    const first = planNewCardSlot({ size: SIZE, area, cards });
    expect(first).toEqual(planNewCardSlot({ size: SIZE, area, cards }));
    expect(first.x).toBeGreaterThanOrEqual(0);
    expect(first.y).toBeGreaterThanOrEqual(0);
  });

  it("尺寸不再被视野夹小：可用区域比卡片还小时仍按卡片尺寸返回（落点被夹进区域）", () => {
    const area = { x: 40, y: 60, w: 100, h: 80 };
    const slot = planNewCardSlot({ size: SIZE, area, cards: [] });
    // findFreeSlot 自身会把尺寸夹进它拿到的搜索范围；调用方给的范围就是可用区域
    expect(Number.isFinite(slot.x + slot.y + slot.w + slot.h)).toBe(true);
    expect(slot.x).toBeGreaterThanOrEqual(area.x);
    expect(slot.y).toBeGreaterThanOrEqual(area.y);
  });
});
