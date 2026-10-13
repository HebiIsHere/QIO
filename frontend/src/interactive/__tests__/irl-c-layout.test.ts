/**
 * IRL-C 纯函数用例（任务书 §3.1-3.10 / §7.1-§7.5 / §7.7）。
 *
 * 覆盖四件在旧实现里没有的事：
 * 1. 展开尺寸**按该卡常态尺寸与可用区域收敛**，而不是无脑 560×420；
 * 2. 可用区域带**原点**（平移 / 滚动后的可视起点）：展开卡会被拉进可视区，
 *    不再留在板面原点附近；
 * 3. `expandedAvoid`：多张卡同时展开时互相避让，并且继续处理让位造成的**新碰撞**
 *    （最多 8 轮，仍让不开就如实记 displaced）；缺省关闭 = 契约 §3.6 的既有口径不变；
 * 4. `sizedCards`：用户在展开期间亲自调过尺寸的卡片，展开沿用用户尺寸。
 *
 * 只测纯函数：不挂 DOM、不碰 store / 网络。
 */
import { describe, expect, it } from "vitest";
import {
  CARD_MAX_SIZE,
  CARD_MIN_SIZE,
  DEFAULT_LAYOUT_GAP,
  EXPANDED_PREVIEW_SIZE,
  MAX_DISPLACE_ROUNDS,
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

/**
 * 让位之后不许留下「原本并不重叠、现在叠上了」的新遮挡。
 * `ids` 只传**未展开**的卡片：展开卡自己撑大去盖住别人是展开的本来含义，
 * 不算新遮挡；新遮挡说的是「被让位的卡片之间 / 被让位与没动过的卡片之间」。
 */
function newOverlaps(rects: Record<string, LayoutRect>, official: Record<string, LayoutRect>, ids: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < ids.length; i += 1) {
    for (let j = i + 1; j < ids.length; j += 1) {
      const a = ids[i];
      const b = ids[j];
      if (!rectsOverlap(rects[a], rects[b])) continue;
      if (rectsOverlap(official[a], official[b])) continue;
      out.push(a + "+" + b);
    }
  }
  return out;
}

describe("尺寸口径常量（§7.3）", () => {
  it("最小 180×96、上限 1600×1200", () => {
    expect(CARD_MIN_SIZE).toEqual({ w: 180, h: 96 });
    expect(CARD_MAX_SIZE).toEqual({ w: 1600, h: 1200 });
  });

  it("cardSizeLimits：可用区域大时用合约上限", () => {
    expect(cardSizeLimits({ w: 4000, h: 3000 })).toEqual({ minW: 180, minH: 96, maxW: 1600, maxH: 1200 });
  });

  it("cardSizeLimits：可用区域很小时最小值向内收敛（不会出现「最小值比屏幕还宽」）", () => {
    const limits = cardSizeLimits({ w: 100, h: 60 });
    expect(limits.maxW).toBe(100);
    expect(limits.maxH).toBe(60);
    expect(limits.minW).toBeLessThanOrEqual(limits.maxW);
    expect(limits.minH).toBeLessThanOrEqual(limits.maxH);
  });

  it("cardSizeLimits：面积还没量到（0 / NaN）时给上限，不把卡片压成 1×1", () => {
    expect(cardSizeLimits({ w: 0, h: Number.NaN })).toEqual({ minW: 180, minH: 96, maxW: 1600, maxH: 1200 });
  });
});

describe("展开尺寸按常态尺寸与可用空间收敛（§7.4）", () => {
  it("可用区域比阅读尺寸小：展开尺寸收敛到可用区域", () => {
    const result = plan([card("a", 100, 50)], ["a"], { stage: { w: 400, h: 300 } });
    expect(result.slots.a).toEqual({ x: 0, y: 0, w: 400, h: 300 });
  });

  it("可用区域比卡片本身还小：不把卡片压得比它自己还小", () => {
    const result = plan([card("a", 0, 0, 500, 400)], ["a"], { stage: { w: 300, h: 200 } });
    expect(result.slots.a.w).toBe(500);
    expect(result.slots.a.h).toBe(400);
  });

  it("卡本来就比阅读尺寸大：保持原尺寸（不缩小）", () => {
    const result = plan([card("a", 100, 100, 800, 600)], ["a"]);
    expect(result.slots.a).toEqual({ x: 100, y: 100, w: 800, h: 600 });
  });
});

describe("可用区域带原点：平移 / 滚动后的可视起点（§7.2）", () => {
  const region: LayoutRect = { x: 1800, y: 900, w: 800, h: 600 };

  it("平移之后展开：展开卡被拉进可视区，不再留在原来（视口外）的坐标", () => {
    const result = plan([card("a", 100, 100)], ["a"], {
      stage: { w: region.w, h: region.h },
      stageOrigin: { x: region.x, y: region.y },
    });
    expect(result.slots.a).toEqual({ x: 1800, y: 900, w: 560, h: 420 });
    expect(insideRegion(result.slots.a, region)).toBe(true);
  });

  it("已经在可视区里的卡：原位放大（不被挪走）", () => {
    const result = plan([card("a", 2000, 1000)], ["a"], {
      stage: { w: region.w, h: region.h },
      stageOrigin: { x: region.x, y: region.y },
    });
    expect(result.slots.a).toEqual({ x: 2000, y: 1000, w: 560, h: 420 });
    expect(insideRegion(result.slots.a, region)).toBe(true);
  });

  it("让位也只在可用区域里进行：交界的未展开卡不会跑出可视区", () => {
    const result = plan([card("a", 1900, 1000), card("b", 2300, 1000)], ["a"], {
      stage: { w: region.w, h: region.h },
      stageOrigin: { x: region.x, y: region.y },
      expandedSize: { w: 400, h: 300 },
    });
    expect(insideRegion(result.slots.b, region)).toBe(true);
    expect(rectsOverlap(result.slots.b, result.slots.a)).toBe(false);
  });
});

describe("多张展开卡互相避让（expandedAvoid，§7.2）", () => {
  const both = [card("a", 0, 0, 300, 200), card("b", 200, 0, 300, 200)];
  const options = { expandedSize: { w: 500, h: 400 } };

  it("后展开的卡给先展开的卡让位：两张展开卡不再互相遮挡", () => {
    const result = plan(both, ["a", "b"], { ...options, expandedAvoid: true });
    expect(result.slots.a).toEqual({ x: 0, y: 0, w: 500, h: 400 });
    expect(result.slots.b).toEqual({ x: 516, y: 0, w: 500, h: 400 });
    expect(rectsOverlap(result.slots.a, result.slots.b)).toBe(false);
    expect(result.expanded).toEqual(["a", "b"]);
    expect(result.displaced).toEqual([]);
  });

  it("缺省（不传 expandedAvoid）仍是契约 §3.6 的旧口径：展开卡之间互不让位", () => {
    const result = plan(both, ["a", "b"], options);
    expect(result.slots.a).toEqual({ x: 0, y: 0, w: 500, h: 400 });
    expect(result.slots.b).toEqual({ x: 200, y: 0, w: 500, h: 400 });
    expect(rectsOverlap(result.slots.a, result.slots.b)).toBe(true);
  });

  it("让位造成的**新碰撞**会被继续处理：最终不再压住原本没有重叠的那张卡", () => {
    // a 展开成 0..400 × 0..300；b（350..410）被推到 416..476，正好压住 d（416..476 × 60..140）。
    // 继续迭代一轮：b 向下让开 d（最近方向），最终既不离开展开卡的右边、也不再压 d。
    const cards = [card("a", 0, 0), card("b", 350, 100, 60, 80), card("d", 416, 60, 60, 80)];
    const official: Record<string, LayoutRect> = {
      a: { x: 0, y: 0, w: 120, h: 80 },
      b: { x: 350, y: 100, w: 60, h: 80 },
      d: { x: 416, y: 60, w: 60, h: 80 },
    };
    const result = plan(cards, ["a"], { expandedSize: { w: 400, h: 300 }, expandedAvoid: true });
    expect(rectsOverlap(result.slots.b, result.slots.d)).toBe(false);
    expect(result.slots.b).toEqual({ x: 416, y: 156, w: 60, h: 80 });
    expect(newOverlaps(result.slots, official, ["b", "d"])).toEqual([]);
    expect(result.displaced).toEqual([]);
  });

  it("实在让不开：如实记 displaced，且绝不留下新的两两遮挡", () => {
    // a 展开成 0..400；b（350..410）先被推到 416，压住 d（440..560）；夹缝不足（416..476 与
    // 424..440 之间放不下 60 宽），来回两轮后停在 364 仍然压着展开卡 —— 必须如实报告。
    const cards = [card("a", 0, 0), card("b", 350, 100, 60, 80), card("d", 440, 100, 120, 80)];
    const official: Record<string, LayoutRect> = {
      a: { x: 0, y: 0, w: 120, h: 80 },
      b: { x: 350, y: 100, w: 60, h: 80 },
      d: { x: 440, y: 100, w: 120, h: 80 },
    };
    const result = plan(cards, ["a"], { expandedSize: { w: 400, h: 300 }, expandedAvoid: true });
    expect(result.displaced).toContain("b");
    expect(rectsOverlap(result.slots.b, result.slots.a)).toBe(true);
    expect(rectsOverlap(result.slots.b, result.slots.d)).toBe(false);
    expect(newOverlaps(result.slots, official, ["b", "d"])).toEqual([]);
  });

  it("两张展开卡都无处可去时：不隐藏、不越界，双方都进 displaced", () => {
    const stage = { w: 500, h: 400 };
    const cards = [card("a", 0, 0, 400, 300), card("b", 10, 10, 400, 300)];
    const result = plan(cards, ["a", "b"], { stage, expandedSize: { w: 400, h: 300 }, expandedAvoid: true });
    expect(insideRegion(result.slots.a, { x: 0, y: 0, w: stage.w, h: stage.h })).toBe(true);
    expect(insideRegion(result.slots.b, { x: 0, y: 0, w: stage.w, h: stage.h })).toBe(true);
    expect(result.displaced).toEqual(expect.arrayContaining(["a", "b"]));
  });

  it("不加 expandedAvoid 时既有测试锁定的行为逐字段不变（对照）", () => {
    const cards = [card("a", 0, 0), card("b", 200, 0)];
    const legacy = plan(cards, ["a", "b"], { expandedSize: { w: 400, h: 300 } });
    expect(legacy.slots.a).toEqual({ x: 0, y: 0, w: 400, h: 300 });
    expect(legacy.slots.b).toEqual({ x: 200, y: 0, w: 400, h: 300 });
  });
});

describe("展开期间用户亲自调过尺寸（§7.1）", () => {
  it("sizedCards：展开沿用用户尺寸，不再套默认阅读尺寸", () => {
    const cards = [card("a", 100, 100, 300, 200)];
    const userSize = plan(cards, ["a"], { sizedCards: ["a"] });
    expect(userSize.slots.a).toEqual({ x: 100, y: 100, w: 300, h: 200 });
    const reading = plan(cards, ["a"]);
    expect(reading.slots.a).toEqual({ x: 100, y: 100, w: 560, h: 420 });
  });

  it("收起后回到常态位置与用户尺寸（旧快照不会覆盖用户改过的 card.w/h）", () => {
    // 用户在展开状态下把卡片改到 400×260 并挪到 (700,300)：正式状态就是这一份
    const moved = [card("a", 700, 300, 400, 260), card("b", 200, 200)];
    const closed = planExpandedLayout({
      cards: moved,
      expanded: [],
      expandedSize: EXPANDED_PREVIEW_SIZE,
      stage: STAGE,
      expandedAvoid: true,
      sizedCards: ["a"],
    });
    expect(closed.slots.a).toEqual({ x: 700, y: 300, w: 400, h: 260 });
    expect(closed.slots.b).toEqual({ x: 200, y: 200, w: 120, h: 80 });
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

  it("gap 与轮数常量保持契约值", () => {
    expect(DEFAULT_LAYOUT_GAP).toBe(16);
    expect(MAX_DISPLACE_ROUNDS).toBe(8);
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
});
