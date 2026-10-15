/**
 * geometry 用例（_IML-COMMON.md §2 F2）——统一几何约束服务的**唯一**权威。
 *
 * 只测纯函数：不挂 DOM、不碰 store / 网络。硬规则：
 * 1. 尺寸口径只由 [CARD_MIN_SIZE, CARD_MAX_SIZE] 决定，**视野从来不是上限**；
 * 2. bounded（拉伸）：只把被拉伸的那条边停在「第一个碰撞邻卡边界 − gap」处，绝不移动别的卡片；
 * 3. free（拖动 / 新建 / QIO 移动）：重叠才找空位，确定性顺序，同样不动别的卡片；
 * 4. 输入一个字段都不改（纯度）。
 */
import { describe, expect, it } from "vitest";
import {
  CARD_MAX_SIZE,
  CARD_MIN_SIZE,
  DEFAULT_PLACEMENT_GAP,
  clampCardSize,
  placeRelative,
  rectsOverlap,
  resolvePlacement,
  type GeoRect,
} from "../geometry";

function rect(x: number, y: number, w: number, h: number): GeoRect {
  return { x, y, w, h };
}

describe("尺寸口径 clampCardSize（§2）", () => {
  it("最小 180×96、上限 1600×1200", () => {
    expect(CARD_MIN_SIZE).toEqual({ w: 180, h: 96 });
    expect(CARD_MAX_SIZE).toEqual({ w: 1600, h: 1200 });
    expect(DEFAULT_PLACEMENT_GAP).toBe(16);
    expect(clampCardSize({ w: 1, h: 1 })).toEqual({ w: 180, h: 96 });
    expect(clampCardSize({ w: 99999, h: 99999 })).toEqual({ w: 1600, h: 1200 });
    expect(clampCardSize({ w: 400, h: 300 })).toEqual({ w: 400, h: 300 });
  });

  it("坏数据不产生 NaN / 0 尺寸：非有限值收敛到最小口径", () => {
    expect(clampCardSize({ w: Number.NaN, h: Number.POSITIVE_INFINITY })).toEqual({ w: 180, h: 96 });
    expect(clampCardSize({ w: -50, h: -20 })).toEqual({ w: 180, h: 96 });
  });

  it("视野再小也不影响上限：**没有**任何按可用区域收紧的参数", () => {
    // 上限只可能是合约值；调用方拿不到「按视口收敛」的口径
    const big = clampCardSize({ w: 5000, h: 5000 });
    expect(big.w).toBe(CARD_MAX_SIZE.w);
    expect(big.h).toBe(CARD_MAX_SIZE.h);
  });
});

describe("resolvePlacement · free（拖动 / 新建 / QIO 移动）", () => {
  it("不重叠：原样返回，reason=ok", () => {
    const result = resolvePlacement({ rect: rect(0, 0, 260, 170), others: [rect(400, 400, 200, 100)], policy: "free" });
    expect(result.rect).toEqual({ x: 0, y: 0, w: 260, h: 170 });
    expect(result.adjusted).toBe(false);
    expect(result.reason).toBe("ok");
  });

  it("重叠：确定性找附近空位，结果与任何邻卡都不相交（不动别的卡片）", () => {
    const others = [rect(200, 0, 260, 170), rect(0, 300, 260, 170)];
    const input = { rect: rect(220, 20, 260, 170), others, policy: "free" as const };
    const result = resolvePlacement(input);
    expect(result.adjusted).toBe(true);
    expect(result.reason).toBe("resolved");
    for (const other of others) expect(rectsOverlap(result.rect, other)).toBe(false);
    // 相邻空位在冲突处的旁边，而不是被丢到板面另一头
    expect(result.rect.x).toBeLessThan(1200);
    expect(result.rect.y).toBeLessThan(1200);
    // 别的卡片一个字段都没改
    expect(others).toEqual([rect(200, 0, 260, 170), rect(0, 300, 260, 170)]);
  });

  it("确定性：同样的输入永远得到同一个落点", () => {
    const others = [rect(0, 0, 400, 300)];
    const a = resolvePlacement({ rect: rect(100, 100, 260, 170), others, policy: "free" });
    const b = resolvePlacement({ rect: rect(100, 100, 260, 170), others, policy: "free" });
    expect(a).toEqual(b);
  });

  it("邻卡铺满整个平面：仍在簇外侧给出零重叠落点（搜索范围自带一张卡的余量）", () => {
    const others = [
      rect(-10000, -10000, 10000, 10000),
      rect(0, -10000, 10000, 10000),
      rect(-10000, 0, 10000, 10000),
      rect(0, 0, 10000, 10000),
    ];
    const result = resolvePlacement({ rect: rect(10, 10, 260, 170), others, policy: "free", gap: 0 });
    expect(result.reason).toBe("resolved");
    for (const other of others) expect(rectsOverlap(result.rect, other)).toBe(false);
    expect(Number.isFinite(result.rect.x + result.rect.y + result.rect.w + result.rect.h)).toBe(true);
  });

  it("size 不参与推导：落点尺寸恒等于期望矩形的 w/h", () => {
    const result = resolvePlacement({ rect: rect(0, 0, 300, 200), others: [rect(0, 0, 300, 200)], policy: "free" });
    expect(result.rect.w).toBe(300);
    expect(result.rect.h).toBe(200);
  });
});

describe("resolvePlacement · bounded（用户拉伸）", () => {
  it("向东拉伸：右边界停在邻卡左边 − gap，左 / 上 / 下分毫不动", () => {
    const anchor = rect(0, 0, 300, 200);
    const desired = rect(0, 0, 700, 200); // 右边界从 300 拉到 700
    const others = [rect(500, 0, 200, 200)];
    const result = resolvePlacement({ rect: desired, others, policy: "bounded", gap: 16, anchor });
    expect(result.reason).toBe("bounded");
    expect(result.adjusted).toBe(true);
    expect(result.rect).toEqual({ x: 0, y: 0, w: 500 - 16, h: 200 });
  });

  it("向北拉伸（拉上边）：上边界停在邻卡下边 + gap，右下角分毫不动", () => {
    const anchor = rect(0, 300, 300, 200);
    const desired = rect(0, 100, 300, 400); // 上边界从 300 拉到 100
    const others = [rect(0, 0, 300, 200)];
    const result = resolvePlacement({ rect: desired, others, policy: "bounded", gap: 16, anchor });
    expect(result.reason).toBe("bounded");
    // 上边界停在邻卡下边 200 + gap 16；下边界仍是起始框的 500（分毫不动）
    expect(result.rect).toEqual({ x: 0, y: 200 + 16, w: 300, h: 500 - (200 + 16) });
  });

  it("向南拉伸：下边界停在邻卡上边 − gap", () => {
    const anchor = rect(0, 0, 300, 200);
    const desired = rect(0, 0, 300, 600);
    const others = [rect(0, 400, 300, 200)];
    const result = resolvePlacement({ rect: desired, others, policy: "bounded", gap: 10, anchor });
    expect(result.rect).toEqual({ x: 0, y: 0, w: 300, h: 400 - 10 });
  });

  it("向西拉伸（拉左边）：左边界停在邻卡右边 + gap，右边界分毫不动", () => {
    const anchor = rect(500, 0, 300, 200);
    const desired = rect(100, 0, 700, 200); // 左边界从 500 拉到 100
    const others = [rect(0, 0, 300, 200)];
    const result = resolvePlacement({ rect: desired, others, policy: "bounded", gap: 16, anchor });
    expect(result.rect).toEqual({ x: 300 + 16, y: 0, w: 800 - (300 + 16), h: 200 });
  });

  it("**绝不移动别的卡片**：bounded 只返回自己的矩形，邻卡由调用方负责不写回", () => {
    const others = [rect(500, 0, 200, 200)];
    const snapshot = JSON.parse(JSON.stringify(others));
    resolvePlacement({ rect: rect(0, 0, 900, 200), others, policy: "bounded", anchor: rect(0, 0, 300, 200) });
    expect(others).toEqual(snapshot);
  });

  it("不重叠：原样通过（reason=ok），不为了「想拦」就把边停下来", () => {
    const result = resolvePlacement({
      rect: rect(0, 0, 500, 200),
      others: [rect(2000, 2000, 100, 100)],
      policy: "bounded",
      anchor: rect(0, 0, 300, 200),
    });
    expect(result.reason).toBe("ok");
    expect(result.rect).toEqual({ x: 0, y: 0, w: 500, h: 200 });
  });

  it("起点就已经重叠（邻卡压在起始框里）：不给虚假的缩小，返回最接近可行的矩形并记 bounded", () => {
    const anchor = rect(100, 100, 120, 80);
    const desired = rect(100, 100, 220, 200);
    const others = [rect(150, 140, 120, 80)];
    const result = resolvePlacement({ rect: desired, others, policy: "bounded", gap: 16, anchor });
    expect(result.reason).toBe("bounded");
    // 起点就重叠时，边界约束无解：保留期望矩形（不做比起点还小的伪收缩）
    expect(result.rect).toEqual(desired);
  });

  it("没有 anchor（旧签名调用）：bounded 退化为 free 的找空位口径", () => {
    const result = resolvePlacement({ rect: rect(10, 10, 260, 170), others: [rect(0, 0, 300, 200)], policy: "bounded" });
    expect(rectsOverlap(result.rect, rect(0, 0, 300, 200))).toBe(false);
    expect(result.reason).toBe("resolved");
  });

  it("gap 默认 16", () => {
    const result = resolvePlacement({
      rect: rect(0, 0, 600, 200),
      others: [rect(400, 0, 200, 200)],
      policy: "bounded",
      anchor: rect(0, 0, 300, 200),
    });
    expect(result.rect.w).toBe(400 - DEFAULT_PLACEMENT_GAP);
  });
});

describe("placeRelative（F7 相对摆放）", () => {
  const anchor = rect(100, 200, 300, 150);

  it("right / left / above / below 用程序算坐标，间距生效", () => {
    expect(placeRelative(anchor, "right", { w: 260, h: 170 }, 24)).toEqual({ x: 424, y: 200, w: 260, h: 170 });
    expect(placeRelative(anchor, "left", { w: 260, h: 170 }, 24)).toEqual({ x: -184, y: 200, w: 260, h: 170 });
    expect(placeRelative(anchor, "above", { w: 260, h: 170 }, 24)).toEqual({ x: 100, y: 6, w: 260, h: 170 });
    expect(placeRelative(anchor, "below", { w: 260, h: 170 }, 24)).toEqual({ x: 100, y: 374, w: 260, h: 170 });
  });

  it("尺寸同样走 clampCardSize 口径（不静默变 1×1 / 不超上限）", () => {
    expect(placeRelative(anchor, "right", { w: 1, h: 1 }, 0).w).toBe(CARD_MIN_SIZE.w);
    expect(placeRelative(anchor, "right", { w: 99999, h: 99999 }, 0).w).toBe(CARD_MAX_SIZE.w);
  });
});
