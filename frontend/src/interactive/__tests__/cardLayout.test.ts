/**
 * cardLayout 用例（_IML-COMMON.md §1 F1「正式几何是唯一权威」）。
 *
 * 这一轮**删除**的旧行为（对应断言已改写为新行为，不再保留旧期望）：
 * - 展开 = 原位放大到阅读尺寸 / 放不下向内收敛；
 * - 展开让位（未展开卡被推开，最多 8 轮）与 `displaced`；
 * - 展开卡之间互相避让（expandedAvoid）；
 * - sizedCards 展开沿用用户尺寸；
 * - 视野夹取（stage / stageOrigin 把展开卡拉进可视区）、按可用区域收敛尺寸。
 *
 * 新口径只有一条：`slots` 逐字段等于正式矩形（恒等映射），`displaced` 恒为 `[]`；
 * 视野 / 展开尺寸 / 让位开关传进来也只被忽略（这正是「缩放平移不改变卡片外框」）。
 *
 * 只测纯函数：不挂载组件、不读 DOM、不碰 store / 网络。
 */
import { describe, expect, it } from "vitest";
import {
  CARD_MAX_SIZE,
  CARD_MIN_SIZE,
  DEFAULT_LAYOUT_GAP,
  EXPANDED_PREVIEW_SIZE,
  cardSizeLimits,
  planExpandedLayout,
  type LayoutInput,
  type LayoutRect,
} from "../cardLayout";

const STAGE = { w: 2400, h: 1600 };
const EXPANDED = { w: 560, h: 420 };

function card(id: string, x: number, y: number, w = 120, h = 80) {
  return { id, x, y, w, h };
}

function plan(cards: ReturnType<typeof card>[], expanded: string[], extra: Partial<LayoutInput> = {}) {
  return planExpandedLayout({
    cards,
    expanded,
    expandedSize: EXPANDED,
    stage: STAGE,
    ...extra,
  });
}

/** 正式位置（输入）：恒等映射的唯一来源 */
function officialSlots(cards: ReturnType<typeof card>[]): Record<string, LayoutRect> {
  const out: Record<string, LayoutRect> = {};
  for (const item of cards) out[item.id] = { x: item.x, y: item.y, w: item.w, h: item.h };
  return out;
}

describe("planExpandedLayout · 恒等映射（F1）", () => {
  it("空白板面：没有卡片就没有槽位，也没有异常", () => {
    const result = plan([], []);
    expect(result.slots).toEqual({});
    expect(result.expanded).toEqual([]);
    expect(result.displaced).toEqual([]);
  });

  it("没有展开卡时，所有槽位逐字段等于正式位置", () => {
    const cards = [card("a", 100, 100), card("b", 400, 260, 200, 140)];
    const result = plan(cards, []);
    expect(result.slots).toEqual(officialSlots(cards));
    expect(result.displaced).toEqual([]);
  });

  it("展开**不改变任何外框**：slots 与没有展开时逐字段相同（含被展开的那张卡）", () => {
    const cards = [card("a", 100, 100), card("b", 600, 200)];
    const closed = plan(cards, []);
    const opened = plan(cards, ["a"]);
    expect(opened.slots).toEqual(closed.slots);
    expect(opened.slots).toEqual(officialSlots(cards));
    expect(opened.expanded).toEqual(["a"]);
    expect(opened.displaced).toEqual([]);
  });

  it("多张卡同时展开也不推任何人：displaced 恒为 []（没有「让位」这回事）", () => {
    const cards = [card("a", 0, 0), card("b", 20, 10)];
    const result = plan(cards, ["a", "b"], { expandedAvoid: true });
    expect(result.slots).toEqual(officialSlots(cards));
    expect(result.expanded).toEqual(["a", "b"]);
    expect(result.displaced).toEqual([]);
  });

  it("展开 id 不存在 / 重复 / 空串：不产生槽位、不崩、不重复回传", () => {
    const cards = [card("a", 100, 100)];
    const result = plan(cards, ["ghost", "a", "a", ""]);
    expect(Object.keys(result.slots)).toEqual(["a"]);
    expect(result.expanded).toEqual(["a"]);
    expect(result.displaced).toEqual([]);
  });

  it("重复 id 的输入只产生一个槽位（第一份为准）", () => {
    const cards = [card("a", 10, 20, 100, 50), card("a", 999, 999, 1, 1)];
    expect(plan(cards, []).slots).toEqual({ a: { x: 10, y: 20, w: 100, h: 50 } });
  });
});

describe("视野 / 展开尺寸 / 让位开关都被忽略（缩放平移不改变卡片外框）", () => {
  it("stage / stageOrigin / expandedSize 怎么变，slots 都不变", () => {
    const cards = [card("a", 2000, 1400), card("b", 2050, 1450, 300, 200)];
    const base = plan(cards, ["a"], { expandedSize: { w: 560, h: 420 }, stage: { w: 2400, h: 1600 } });
    const tiny = plan(cards, ["a"], { expandedSize: { w: 100, h: 80 }, stage: { w: 200, h: 120 } });
    const panned = plan(cards, ["a"], {
      expandedSize: { w: 1600, h: 1200 },
      stage: { w: 800, h: 600 },
      stageOrigin: { x: 1800, y: 900 },
    });
    expect(tiny.slots).toEqual(base.slots);
    expect(panned.slots).toEqual(base.slots);
    expect(base.slots).toEqual(officialSlots(cards));
  });

  it("sizedCards 不再有意义：展开沿用正式 card.w/h（本来就是那一份）", () => {
    const cards = [card("a", 100, 100, 300, 200)];
    expect(plan(cards, ["a"], { sizedCards: ["a"] }).slots).toEqual(plan(cards, ["a"]).slots);
    expect(plan(cards, ["a"]).slots.a).toEqual({ x: 100, y: 100, w: 300, h: 200 });
  });

  it("stage 无效（0 / NaN）与有效时结果一致：不再有「无界 = 放大」这条分支", () => {
    const cards = [card("a", 100, 100)];
    expect(plan(cards, ["a"], { stage: { w: 0, h: Number.NaN } }).slots).toEqual(officialSlots(cards));
    expect(plan(cards, ["a"], { stage: { w: 2400, h: 1600 } }).slots).toEqual(officialSlots(cards));
  });

  it("坏坐标不产生 NaN：非有限值按 0 处理，宽高不为负", () => {
    const cards = [{ id: "a", x: Number.NaN, y: Number.POSITIVE_INFINITY, w: -10, h: -5 }];
    expect(plan(cards, []).slots).toEqual({ a: { x: 0, y: 0, w: 0, h: 0 } });
  });
});

describe("尺寸口径与常量（§2）", () => {
  it("最小 180×96、上限 1600×1200、gap 16", () => {
    expect(CARD_MIN_SIZE).toEqual({ w: 180, h: 96 });
    expect(CARD_MAX_SIZE).toEqual({ w: 1600, h: 1200 });
    expect(DEFAULT_LAYOUT_GAP).toBe(16);
  });

  it("cardSizeLimits：**视野大小不是卡片上限** —— 可用区域再小也用合约口径", () => {
    const contract = { minW: 180, minH: 96, maxW: 1600, maxH: 1200 };
    expect(cardSizeLimits({ w: 4000, h: 3000 })).toEqual(contract);
    expect(cardSizeLimits({ w: 100, h: 60 })).toEqual(contract);
    expect(cardSizeLimits({ w: 0, h: Number.NaN })).toEqual(contract);
    expect(cardSizeLimits()).toEqual(contract);
  });

  it("EXPANDED_PREVIEW_SIZE 保留但已废弃：仍是有限值，且不参与任何卡片外框", () => {
    expect(EXPANDED_PREVIEW_SIZE.w).toBeGreaterThan(0);
    expect(EXPANDED_PREVIEW_SIZE.h).toBeGreaterThan(0);
  });
});

describe("纯度：不改输入、不共享引用", () => {
  it("输入对象与数组在调用前后逐字段不变", () => {
    const cards = [card("a", 100, 100), card("b", 350, 100)];
    const expanded = ["a"];
    const snapshot = JSON.stringify({ cards, expanded });
    const result = plan(cards, expanded);
    expect(JSON.stringify({ cards, expanded })).toBe(snapshot);
    result.slots.a.x = -999;
    expect(cards[0].x).toBe(100);
  });

  it("连续两次调用结果一致（纯函数、不依赖调用次数或内部状态）", () => {
    const cards = [card("a", 0, 0), card("b", 350, 0)];
    const options = { expandedSize: { w: 400, h: 300 }, stage: { w: 1200, h: 800 } };
    expect(plan(cards, ["a"], options)).toEqual(plan(cards, ["a"], options));
  });

  it("slots 只包含输入里真实存在的卡片，且顺序与输入一致", () => {
    const cards = [card("a", 0, 0), card("b", 300, 0), card("c", 600, 0)];
    expect(Object.keys(plan(cards, ["b"]).slots)).toEqual(["a", "b", "c"]);
  });
});
