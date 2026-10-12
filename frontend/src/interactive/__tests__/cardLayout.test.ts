/**
 * 临时布局纯函数用例（契约 2026-10-12 §3.6）。
 *
 * 这些用例只测**纯函数**：不挂载组件、不读 DOM、不碰 store / 网络。
 * 契约硬规则（逐条对应下面的 describe）：
 * 1. slots 只给渲染用，输入里的正式位置一个字节都不许改（纯度 + 不改板面状态）；
 * 2. 展开卡在原位置原位放大到 expandedSize，放不下时向内收敛、不越出 stage；
 * 3. 与展开矩形相交的未展开卡按最短方向让开（右/下优先，其次左/上），最多 8 轮；
 *    让开后仍相交的记进 displaced（诚实报告「让不开」，不隐藏、也不再硬推）；
 * 4. 多个展开卡之间**不互相让位**；
 * 5. 收起 = expanded 里没有的卡一律回到正式位置；用户展开期间手动移动过的卡，
 *    收起后停在用户的新位置（因为正式位置本来就是用户改的那份）。
 *
 * 约定（本文件同时锁定实现口径）：
 * - `displaced` 指「推了最多 8 轮仍与展开矩形相交」的卡片（契约 §3.6 的原文口径），
 *   不是「被推动过的卡片」；
 * - 展开顺序 = `expanded` 数组顺序；
 * - stage 尺寸无效（0 / 非有限数）时按「无界」处理：宁可保持原位放大，也不产生 0 尺寸卡片。
 */
import { describe, expect, it } from "vitest";
import {
  DEFAULT_LAYOUT_GAP,
  EXPANDED_PREVIEW_SIZE,
  MAX_DISPLACE_ROUNDS,
  planExpandedLayout,
  type LayoutInput,
  type LayoutRect,
} from "../cardLayout";

const STAGE = { w: 2400, h: 1600 };
const EXPANDED = { w: 560, h: 420 };

function card(id: string, x: number, y: number, w = 120, h = 80) {
  return { id, x, y, w, h };
}

function plan(
  cards: ReturnType<typeof card>[],
  expanded: string[],
  extra: Partial<LayoutInput> = {},
) {
  return planExpandedLayout({
    cards,
    expanded,
    expandedSize: EXPANDED,
    stage: STAGE,
    ...extra,
  });
}

function rect(cards: ReturnType<typeof card>[], id: string): LayoutRect {
  const found = cards.find((item) => item.id === id);
  if (!found) throw new Error("测试夹具缺少卡片 " + id);
  return { x: found.x, y: found.y, w: found.w, h: found.h };
}

function intersects(a: LayoutRect, b: LayoutRect): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/** 正式位置（展开前的输入）：收起后必须逐字段回到这里 */
function officialSlots(cards: ReturnType<typeof card>[]): Record<string, LayoutRect> {
  const out: Record<string, LayoutRect> = {};
  for (const item of cards) out[item.id] = { x: item.x, y: item.y, w: item.w, h: item.h };
  return out;
}

function insideStage(slot: LayoutRect, stage = STAGE): boolean {
  return slot.x >= 0 && slot.y >= 0 && slot.x + slot.w <= stage.w + 1e-6 && slot.y + slot.h <= stage.h + 1e-6;
}

describe("planExpandedLayout · 空白板面与基本形状", () => {
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
    expect(result.expanded).toEqual([]);
    expect(result.displaced).toEqual([]);
  });

  it("展开 id 不存在时：不产生槽位、不崩、不把它算进 expanded", () => {
    const cards = [card("a", 100, 100)];
    const result = plan(cards, ["ghost", "a"]);
    expect(Object.keys(result.slots)).toEqual(["a"]);
    expect(result.expanded).toEqual(["a"]);
    expect(result.displaced).toEqual([]);
  });

  it("公开的默认常量与契约一致：gap 16、8 轮上限、展开尺寸有限", () => {
    expect(DEFAULT_LAYOUT_GAP).toBe(16);
    expect(MAX_DISPLACE_ROUNDS).toBe(8);
    expect(EXPANDED_PREVIEW_SIZE.w).toBeGreaterThan(0);
    expect(EXPANDED_PREVIEW_SIZE.h).toBeGreaterThan(0);
  });
});

describe("单卡展开与收起", () => {
  it("在原位置原位放大到 expandedSize", () => {
    const cards = [card("a", 100, 100)];
    const result = plan(cards, ["a"]);
    expect(result.slots.a).toEqual({ x: 100, y: 100, w: 560, h: 420 });
    expect(result.expanded).toEqual(["a"]);
    expect(result.displaced).toEqual([]);
  });

  it("展开不会把比 expandedSize 更大的卡片缩小", () => {
    const cards = [card("a", 100, 100, 800, 600)];
    expect(plan(cards, ["a"]).slots.a).toEqual({ x: 100, y: 100, w: 800, h: 600 });
  });

  it("右 / 下越界：向内收敛到 stage 内，宽度高度保持不变（放得下时）", () => {
    const result = plan([card("a", 2000, 1400)], ["a"]);
    expect(result.slots.a).toEqual({ x: 2400 - 560, y: 1600 - 420, w: 560, h: 420 });
    expect(insideStage(result.slots.a)).toBe(true);
  });

  it("左 / 上越界：收敛到 0，不出现负坐标", () => {
    const result = plan([card("a", -80, -60)], ["a"]);
    expect(result.slots.a).toEqual({ x: 0, y: 0, w: 560, h: 420 });
  });

  it("收起后回到正式位置（同一份输入，一次调用即可恢复）", () => {
    const cards = [card("a", 100, 100), card("b", 900, 700)];
    const opened = plan(cards, ["a"]);
    expect(opened.slots.a.w).toBe(560);
    const closed = plan(cards, []);
    expect(closed.slots).toEqual(officialSlots(cards));
  });

  it("窄 / 矮的 stage：展开卡不越界（尺寸与位置一起收敛）", () => {
    const stage = { w: 200, h: 120 };
    const result = plan([card("a", 150, 60, 100, 80)], ["a"], { stage });
    expect(result.slots.a).toEqual({ x: 0, y: 0, w: 200, h: 120 });
    expect(insideStage(result.slots.a, stage)).toBe(true);
  });

  it("stage 尺寸无效（0 / 非有限数）时按无界处理：保持原位放大，不产生 0 尺寸卡片", () => {
    const stage = { w: 0, h: Number.NaN };
    const result = plan([card("a", 100, 100)], ["a"], { stage });
    expect(result.slots.a).toEqual({ x: 100, y: 100, w: 560, h: 420 });
  });
});

describe("让位：最短方向 + 右 / 下优先", () => {
  it("相交的未展开卡按最短方向让开（向右最短 → 向右）", () => {
    const cards = [card("a", 0, 0), card("b", 350, 0)];
    const result = plan(cards, ["a"], { expandedSize: { w: 400, h: 300 }, stage: { w: 1200, h: 800 } });
    expect(result.slots.a).toEqual({ x: 0, y: 0, w: 400, h: 300 });
    // 右移距离 = 展开矩形右缘 + gap − 卡片左缘 = 400 + 16 − 350
    expect(result.slots.b).toEqual({ x: 416, y: 0, w: 120, h: 80 });
    expect(result.displaced).toEqual([]);
  });

  it("右与下代价相同时取右（先试向右、再向下）", () => {
    const cards = [card("a", 0, 0), card("b", 150, 150, 50, 50)];
    const result = plan(cards, ["a"], { expandedSize: { w: 200, h: 200 }, stage: { w: 1200, h: 800 } });
    expect(result.slots.b).toEqual({ x: 216, y: 150, w: 50, h: 50 });
    expect(result.displaced).toEqual([]);
  });

  it("向左更短时向左让开（不是只会往右下推）", () => {
    const cards = [card("a", 1000, 1000), card("b", 700, 1100, 320, 80)];
    const result = plan(cards, ["a"], { expandedSize: { w: 400, h: 300 }, stage: { w: 2000, h: 2000 } });
    expect(result.slots.b).toEqual({ x: 1000 - 16 - 320, y: 1100, w: 320, h: 80 });
    expect(result.displaced).toEqual([]);
  });

  it("向上更短时向上让开", () => {
    const cards = [card("a", 1000, 1000), card("b", 1100, 800, 100, 220)];
    const result = plan(cards, ["a"], { expandedSize: { w: 400, h: 300 }, stage: { w: 2000, h: 2000 } });
    expect(result.slots.b).toEqual({ x: 1100, y: 1000 - 16 - 220, w: 100, h: 220 });
    expect(result.displaced).toEqual([]);
  });

  it("同一张卡被多个展开矩形夹住时，最终与所有展开矩形都不相交", () => {
    const cards = [card("a", 0, 0), card("b", 0, 340), card("c", 100, 280)];
    const expandedSize = { w: 400, h: 300 };
    const result = plan(cards, ["a", "b"], { expandedSize, stage: { w: 1200, h: 800 } });
    const inA = result.slots.a;
    const inB = result.slots.b;
    expect(intersects(result.slots.c, inA)).toBe(false);
    expect(intersects(result.slots.c, inB)).toBe(false);
    expect(result.displaced).toEqual([]);
  });

  it("gap 可配置：gap=0 时贴着展开矩形边缘让开", () => {
    const cards = [card("a", 0, 0), card("b", 350, 0)];
    const result = plan(cards, ["a"], {
      expandedSize: { w: 400, h: 300 },
      stage: { w: 1200, h: 800 },
      gap: 0,
    });
    expect(result.slots.b.x).toBe(400);
  });
});

describe("让不开时诚实记进 displaced", () => {
  it("展开矩形铺满 stage：让不开的卡不会消失，也不会被推到 stage 外", () => {
    const stage = { w: 400, h: 300 };
    const cards = [card("a", 0, 0), card("b", 10, 10, 50, 50)];
    const result = plan(cards, ["a"], { expandedSize: { w: 400, h: 300 }, stage });
    expect(result.slots.a).toEqual({ x: 0, y: 0, w: 400, h: 300 });
    expect(result.displaced).toEqual(["b"]);
    expect(insideStage(result.slots.b, stage)).toBe(true);
  });

  it("被夹在两张展开卡之间且无处可去时：终止（不无限推）并记进 displaced", () => {
    // A 覆盖 y 0..300、B 覆盖 y 340..640（两张都铺满 0..400 宽）；C 卡在中间那条 40px 走廊里。
    // stage 收成 500x640：右 / 下都被边界挡住，左 / 上会退出 stage —— 四个方向都不可行，
    // 只能诚实报告「让不开」，而且绝不无限推。
    const stage = { w: 500, h: 640 };
    const cards = [card("a", 0, 0), card("b", 0, 340), card("c", 100, 280)];
    const result = plan(cards, ["a", "b"], { expandedSize: { w: 400, h: 300 }, stage });
    expect(result.displaced).toEqual(["c"]);
    expect(insideStage(result.slots.c, stage)).toBe(true);
  });

  it("空板面 + 无效 expanded 不产生 displaced", () => {
    expect(plan([], ["a", "b"]).displaced).toEqual([]);
  });
});

describe("多个展开卡互不让位", () => {
  it("两张展开卡各自原位放大，即使互相重叠也不推对方", () => {
    const cards = [card("a", 0, 0), card("b", 200, 0)];
    const result = plan(cards, ["a", "b"], { expandedSize: { w: 400, h: 300 }, stage: { w: 1200, h: 800 } });
    expect(result.slots.a).toEqual({ x: 0, y: 0, w: 400, h: 300 });
    expect(result.slots.b).toEqual({ x: 200, y: 0, w: 400, h: 300 });
    expect(result.expanded).toEqual(["a", "b"]);
    expect(result.displaced).toEqual([]);
  });

  it("展开顺序（数组顺序）不影响展开卡自身的位置；两种顺序都收敛且不越界", () => {
    const cards = [card("a", 0, 0), card("b", 600, 0), card("c", 390, 100, 240, 60)];
    const options = { expandedSize: { w: 400, h: 300 }, stage: { w: 1200, h: 800 } };
    const forward = plan(cards, ["a", "b"], options);
    const backward = plan(cards, ["b", "a"], options);
    expect(forward.slots.a).toEqual(backward.slots.a);
    expect(forward.slots.b).toEqual(backward.slots.b);
    for (const slot of Object.values(forward.slots)) expect(insideStage(slot, options.stage)).toBe(true);
    for (const slot of Object.values(backward.slots)) expect(insideStage(slot, options.stage)).toBe(true);
  });
});

describe("收起：顺序无关，且停在用户的新位置", () => {
  const cards = [card("a", 100, 100), card("b", 300, 100), card("c", 700, 500)];

  it("先收 A 再收 B 与先收 B 再收 A，最终布局完全一致（都回到正式位置）", () => {
    const opened = plan(cards, ["a", "b"]);
    expect(opened.slots.a.w).toBe(560);
    // 路径一：先收 A（剩 B 展开）→ 再收 B
    const closeAFirst = plan(cards, ["b"]);
    const closeAThenB = plan(cards, []);
    // 路径二：先收 B（剩 A 展开）→ 再收 A
    const closeBFirst = plan(cards, ["a"]);
    const closeBThenA = plan(cards, []);
    // 中间态不同（各自还展开着一张），但两条收起路径的终点必须逐字段一致
    expect(closeAFirst.slots).not.toEqual(closeBFirst.slots);
    expect(closeAThenB.slots).toEqual(closeBThenA.slots);
    expect(closeAThenB.slots).toEqual(officialSlots(cards));
    expect(closeBFirst.slots.a.w).toBe(560); // 只剩 A 展开：A 仍是放大态
    expect(closeAFirst.slots.b.w).toBe(560); // 只剩 B 展开：B 仍是放大态
    expect(closeAFirst.displaced).toEqual([]);
    expect(closeBFirst.displaced).toEqual([]);
  });

  it("展开期间用户手动移动过：收起后停在用户的新位置，而不是旧位置", () => {
    const before = plan(cards, ["a"]);
    expect(before.slots.a.x).toBe(100);
    // 用户在展开状态下把这张卡拖到了新位置：正式位置被用户改过
    const moved = cards.map((item) => (item.id === "a" ? { ...item, x: 900, y: 700 } : item));
    const stillOpen = plan(moved, ["a"]);
    expect(stillOpen.slots.a).toEqual({ x: 900, y: 700, w: 560, h: 420 });
    const closed = plan(moved, []);
    expect(closed.slots.a).toEqual({ x: 900, y: 700, w: 120, h: 80 });
    expect(closed.slots).toEqual(officialSlots(moved));
  });
});

describe("纯度：不改输入、不共享引用", () => {
  it("输入对象与数组在调用前后逐字段不变", () => {
    const cards = [card("a", 100, 100), card("b", 350, 100)];
    const expanded = ["a"];
    const snapshot = JSON.stringify({ cards, expanded });
    const result = plan(cards, expanded);
    expect(JSON.stringify({ cards, expanded })).toBe(snapshot);
    // 返回的槽位不是输入对象的别名：改返回值不会写回输入
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

  it("展开卡在 slots 里的矩形就是渲染矩形（w / h 与 expandedSize 一致）", () => {
    const result = plan([card("a", 0, 0)], ["a"]);
    expect(rect([card("a", 0, 0)], "a")).toEqual({ x: 0, y: 0, w: 120, h: 80 });
    expect(result.slots.a.w).toBe(EXPANDED.w);
    expect(result.slots.a.h).toBe(EXPANDED.h);
  });
});
