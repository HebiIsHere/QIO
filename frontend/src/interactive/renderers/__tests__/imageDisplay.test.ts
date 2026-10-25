/**
 * Phase 2-C：图片显示比例的纯函数用例。
 *
 * 验收口径：
 * * 真实解码尺寸 → meta.display（正整数、正比例；0 / NaN / 负数不写）；
 * * meta.display 读取只接受真实字段；缺失 / 脏值返回 null，绝不猜；
 * * 卡片尺寸按比例约束：所有路径共用一个函数，结果在卡片口径内且比例误差 ≤ 2%；
 * * 长图 / 竖图阈值必须有详情入口（不能永远打不开）。
 */
import { describe, expect, it } from "vitest";
import {
  IMAGE_CARD_LIMITS,
  IMAGE_DETAIL_OBJECT_FIT,
  IMAGE_LONG_RATIO,
  IMAGE_RATIO_TOLERANCE,
  IMAGE_TALL_RATIO,
  constrainCardSizeForCard,
  constrainCardSizeToRatio,
  decodeDisplayMeta,
  imageDetailHint,
  imageDetailLabel,
  isAspectSatisfied,
  isLongImage,
  isRatioFeasible,
  isTallImage,
  imageRatioForCard,
  normalizeAspectRatio,
  readImageDisplay,
  sameAspectRatio,
  shouldOfferImageDetail,
  suggestedCardSizeForRatio,
  writeImageDisplay,
} from "../imageDisplay";

const PNG_800x600 = { width: 800, height: 600 };

describe("解码尺寸 → meta.display", () => {
  it("真实正整数宽高写入 meta.display，比例为 width / height", () => {
    const display = decodeDisplayMeta(800, 600);
    expect(display).not.toBeNull();
    expect(display!.width).toBe(800);
    expect(display!.height).toBe(600);
    expect(display!.aspectRatio).toBeCloseTo(4 / 3, 5);
    expect(Object.keys(display!).sort()).toEqual(["aspectRatio", "height", "width"]);
  });

  it("0 / NaN / Infinity / 负数 / 非整数 / 超范围一律不写（不猜、不写 0）", () => {
    for (const [w, h] of [
      [0, 600],
      [800, 0],
      [Number.NaN, 600],
      [800, Number.POSITIVE_INFINITY],
      [-1, 600],
      [800.5, 600],
      [100_001, 600],
      ["800", 600],
    ] as const) {
      expect(decodeDisplayMeta(w as never, h as never), String(w) + "x" + String(h)).toBeNull();
    }
  });

  it("normalizeAspectRatio 不产生 NaN / Infinity", () => {
    expect(normalizeAspectRatio(0, 600)).toBeNull();
    expect(normalizeAspectRatio(800, Number.NaN)).toBeNull();
    expect(normalizeAspectRatio(800, 600)).toBeCloseTo(4 / 3, 5);
  });
});

describe("比例比较", () => {
  it("sameAspectRatio 只接受有限正数，并遵守 2% 误差", () => {
    expect(IMAGE_RATIO_TOLERANCE).toBe(0.02);
    expect(sameAspectRatio(4 / 3, 4 / 3 + 0.01)).toBe(true);
    expect(sameAspectRatio(4 / 3, 2)).toBe(false);
    expect(sameAspectRatio(0, 1)).toBe(false);
    expect(sameAspectRatio(Number.NaN, 1)).toBe(false);
  });
});

describe("读取 meta.display", () => {
  it("只认 meta.display；宽度高度是非零正数才有效", () => {
    expect(readImageDisplay({ display: { width: 800, height: 600, aspectRatio: 4 / 3 } })).toEqual({
      width: 800,
      height: 600,
      aspectRatio: expect.closeTo(4 / 3, 5),
    });
    expect(readImageDisplay({ display: { width: 0, height: 600, aspectRatio: 0 } })).toBeNull();
    expect(readImageDisplay({ display: { width: 800, height: 600, aspectRatio: 0 } })).toEqual({
      width: 800,
      height: 600,
      aspectRatio: expect.closeTo(4 / 3, 5),
    });
    expect(readImageDisplay(null)).toBeNull();
    expect(readImageDisplay({})).toBeNull();
    expect(readImageDisplay({ display: { width: "800", height: 600 } })).toBeNull();
  });

  it("aspectRatio 与宽高明显不符时按宽高算（宽高是权威事实）", () => {
    const display = readImageDisplay({ display: { width: 800, height: 600, aspectRatio: 10 } });
    expect(display!.aspectRatio).toBeCloseTo(4 / 3, 5);
  });

  it("writeImageDisplay 只替换 display，不丢其它 meta 字段，也不接受脏值", () => {
    const meta = { name: "照片.png", file: { attachmentId: "att_1" }, display: { width: 1, height: 1 } };
    const next = writeImageDisplay(meta, decodeDisplayMeta(800, 600)!);
    expect(next.name).toBe("照片.png");
    expect((next.file as { attachmentId: string }).attachmentId).toBe("att_1");
    expect((next.display as { aspectRatio: number }).aspectRatio).toBeCloseTo(4 / 3, 5);
    expect(writeImageDisplay(meta, null)).toEqual({ ...meta, display: { width: 1, height: 1 } });
    // 脏值不写，也不把旧 display 弄成 0/NaN
    expect(writeImageDisplay({}, decodeDisplayMeta(0, 0) as never)).toEqual({});
  });
});

describe("卡片尺寸按比例约束（全路径共用一个口径）", () => {
  it("suggestedCardSizeForRatio：保留面积、比例误差在 2% 内、落在卡片口径里", () => {
    const size = suggestedCardSizeForRatio({ w: 320, h: 260 }, 4 / 3);
    expect(size.w).toBeGreaterThanOrEqual(IMAGE_CARD_LIMITS.minW);
    expect(size.h).toBeGreaterThanOrEqual(IMAGE_CARD_LIMITS.minH);
    expect(size.w).toBeLessThanOrEqual(IMAGE_CARD_LIMITS.maxW);
    expect(size.h).toBeLessThanOrEqual(IMAGE_CARD_LIMITS.maxH);
    expect(isAspectSatisfied(size, 4 / 3)).toBe(true);
    expect(size.w * size.h).toBeGreaterThan(0);
  });

  it("极端竖图 / 长图也不会被压成 0 或越界；能否满足比例如实报告", () => {
    expect(isRatioFeasible(4 / 3)).toBe(true);
    expect(isRatioFeasible(0.2)).toBe(true);
    expect(isRatioFeasible(30)).toBe(false);
    expect(isRatioFeasible(0)).toBe(false);
    for (const ratio of [0.05, 0.2, 5, 30]) {
      const size = suggestedCardSizeForRatio({ w: 500, h: 500 }, ratio);
      expect(size.w).toBeGreaterThanOrEqual(1);
      expect(size.h).toBeGreaterThanOrEqual(1);
      expect(size.w).toBeLessThanOrEqual(IMAGE_CARD_LIMITS.maxW);
      expect(size.h).toBeLessThanOrEqual(IMAGE_CARD_LIMITS.maxH);
      // 口径内可满足的比例必须真的满足；无解的极端比例不许假装满足
      expect(isAspectSatisfied(size, ratio, 0.02)).toBe(isRatioFeasible(ratio));
    }
  });

  it("constrainCardSizeToRatio：驱动维由调用方决定，结果始终在限制内且满足比例", () => {
    const current = { w: 320, h: 260 };
    const byWidth = constrainCardSizeToRatio(current, { w: 400, h: 260 }, 4 / 3, { driver: "width" });
    expect(byWidth.w).toBe(400);
    expect(isAspectSatisfied(byWidth, 4 / 3)).toBe(true);
    const byHeight = constrainCardSizeToRatio(current, { w: 320, h: 300 }, 4 / 3, { driver: "height" });
    expect(byHeight.h).toBe(300);
    expect(isAspectSatisfied(byHeight, 4 / 3)).toBe(true);
    const minClipped = constrainCardSizeToRatio(current, { w: 10, h: 10 }, 4 / 3, { driver: "width" });
    expect(minClipped.w).toBe(IMAGE_CARD_LIMITS.minW);
    expect(minClipped.h).toBeGreaterThan(0);
  });

  it("接线入口：imageRatioForCard / constrainCardSizeForCard 与纯函数同一口径", () => {
    const card = { meta: { display: { width: 800, height: 600, aspectRatio: 4 / 3 } } };
    expect(imageRatioForCard(card)).toBeCloseTo(4 / 3, 5);
    expect(imageRatioForCard({ meta: {} })).toBeNull();
    expect(imageRatioForCard(null)).toBeNull();
    const size = constrainCardSizeForCard(card, { w: 320, h: 260 }, { w: 500, h: 260 }, { driver: "width" });
    expect(size.w).toBe(500);
    expect(isAspectSatisfied(size, 4 / 3)).toBe(true);
  });

  it("比例未知时只夹尺寸，不伪造比例", () => {
    const size = constrainCardSizeToRatio({ w: 320, h: 260 }, { w: 99999, h: -1 }, null);
    expect(size.w).toBe(IMAGE_CARD_LIMITS.maxW);
    expect(size.h).toBe(260);
  });
});

describe("长图 / 竖图阈值：详情入口不能被锁死", () => {
  it("阈值常量与判定方向一致", () => {
    expect(imageDisplayRatio(IMAGE_LONG_RATIO + 0.1)).toBe(true);
    expect(isLongImage({ aspectRatio: IMAGE_LONG_RATIO + 0.1 })).toBe(true);
    expect(isLongImage({ aspectRatio: 1 })).toBe(false);
    expect(isTallImage({ aspectRatio: IMAGE_TALL_RATIO - 0.01 })).toBe(true);
    expect(isTallImage({ aspectRatio: 1 })).toBe(false);
  });

  it("极端长图 / 竖图即使卡片比例已满足也仍提供详情入口；比例未知同样提供", () => {
    const long = { width: 3000, height: 500, aspectRatio: 6 };
    expect(shouldOfferImageDetail(long, { w: 600, h: 100 })).toBe(true);
    const tall = { width: 300, height: 3000, aspectRatio: 0.1 };
    expect(shouldOfferImageDetail(tall, { w: 100, h: 1000 })).toBe(true);
    expect(shouldOfferImageDetail(null, { w: 320, h: 260 })).toBe(true);
  });

  it("普通比例且卡片已满足比例时不需要额外详情提示；比例不符时需要", () => {
    const normal = { width: 800, height: 600, aspectRatio: 4 / 3 };
    expect(shouldOfferImageDetail(normal, { w: 400, h: 300 })).toBe(false);
    expect(shouldOfferImageDetail(normal, { w: 320, h: 260 })).toBe(true);
  });

  it("详情文案与标签不冒充文字卡的「展开预览」", () => {
    expect(imageDetailHint({ width: 3000, height: 500, aspectRatio: 6 })).toContain("长");
    expect(imageDetailHint({ width: 300, height: 3000, aspectRatio: 0.1 })).toContain("长");
    expect(imageDetailHint({ width: 800, height: 600, aspectRatio: 4 / 3 })).toContain("详情");
    expect(imageDetailLabel(false)).toBe("查看详情");
    expect(imageDetailLabel(true)).toBe("收起详情");
    expect(IMAGE_DETAIL_OBJECT_FIT).toBe("contain");
  });
});

function imageDisplayRatio(ratio: number): boolean {
  return isLongImage({ aspectRatio: ratio });
}
