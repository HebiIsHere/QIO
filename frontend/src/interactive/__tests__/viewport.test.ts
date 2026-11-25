/**
 * 板面视口坐标换算用例（子智能体 B 负责）。
 *
 * 契约：docs/interactive-mode-contract.md §8.2「坐标」一行：
 * 平移缩放后，卡片拖动、框选、连线、预览与局部工具栏必须仍然准确。
 * 这里只测纯函数；jsdom 没有真实布局，所以「组件里的 getBoundingClientRect」由
 * boardCanvas.test.ts 用桩值覆盖，坐标正确性以本文件的数学断言为准。
 */
import { describe, expect, it } from "vitest";
import {
  clampScale,
  clampScroll,
  clampViewport,
  dotGridMetrics,
  effectiveRect,
  fitViewToBounds,
  gridPhase,
  IDENTITY_VIEWPORT,
  MAX_SCALE,
  MIN_SCALE,
  overlapArea,
  panBy,
  pointInRect,
  rectFromDrag,
  rectsIntersect,
  safeScale,
  scrollFromViewport,
  toBoardPoint,
  toScreenPoint,
  visibleWorldRect,
  wheelDeltaY,
  wheelZoomFactor,
  worldBoundsOf,
  zoomAt,
  zoomAtScroll,
  type Viewport,
} from "../viewport";

/** 板面内容容器左上角在屏幕上的位置（模拟 getBoundingClientRect + 滚动）。 */
const RECT = { left: 120, top: 40 };

describe("坐标换算：屏幕 ↔ 板面", () => {
  it("恒等视口下只减去容器原点", () => {
    expect(toBoardPoint(IDENTITY_VIEWPORT, { x: 200, y: 90 }, RECT)).toEqual({ x: 80, y: 50 });
    expect(toScreenPoint(IDENTITY_VIEWPORT, { x: 80, y: 50 }, RECT)).toEqual({ x: 200, y: 90 });
  });

  it("平移后换算仍然准确", () => {
    const view: Viewport = { scale: 1, x: -60, y: 25 };
    // 屏幕 (200,90) → 板面 ((200-120) - (-60), (90-40) - 25) = (140, 25)
    expect(toBoardPoint(view, { x: 200, y: 90 }, RECT)).toEqual({ x: 140, y: 25 });
    expect(toScreenPoint(view, { x: 140, y: 25 }, RECT)).toEqual({ x: 200, y: 90 });
  });

  it("缩放后换算仍然准确（含小数）", () => {
    const view: Viewport = { scale: 1.5, x: 30, y: -20 };
    const point = toBoardPoint(view, { x: 300, y: 160 }, RECT);
    expect(point.x).toBeCloseTo(((300 - 120) - 30) / 1.5, 10);
    expect(point.y).toBeCloseTo(((160 - 40) + 20) / 1.5, 10);
    const back = toScreenPoint(view, point, RECT);
    expect(back.x).toBeCloseTo(300, 10);
    expect(back.y).toBeCloseTo(160, 10);
  });

  it("平移 + 缩放同时存在时来回换算无损", () => {
    const view: Viewport = { scale: 0.4, x: -320, y: 180 };
    const client = { x: 12, y: 733 };
    const point = toBoardPoint(view, client, RECT);
    const back = toScreenPoint(view, point, RECT);
    expect(back.x).toBeCloseTo(client.x, 9);
    expect(back.y).toBeCloseTo(client.y, 9);
  });

  it("effectiveRect 把滚动量算进容器原点（否则滚动后拖动会偏）", () => {
    expect(effectiveRect({ left: 120, top: 40 }, 0, 0)).toEqual({ left: 120, top: 40 });
    expect(effectiveRect({ left: 120, top: 40 }, 200, 60)).toEqual({ left: -80, top: -20 });
    // 容器滚动 200 后，屏幕 (120,40) 处的板面点就是 x=200
    const rect = effectiveRect({ left: 120, top: 40 }, 200, 60);
    expect(toBoardPoint(IDENTITY_VIEWPORT, { x: 120, y: 40 }, rect)).toEqual({ x: 200, y: 60 });
  });

  it("非法数值不会让坐标变成 NaN", () => {
    const point = toBoardPoint({ scale: Number.NaN, x: Number.NaN, y: 0 }, { x: 10, y: 10 }, RECT);
    expect(Number.isFinite(point.x)).toBe(true);
    expect(Number.isFinite(point.y)).toBe(true);
  });
});

describe("缩放：以指针附近为中心", () => {
  it("缩放前后指针下的板面点落在同一屏幕位置", () => {
    const before: Viewport = { scale: 1, x: 0, y: 0 };
    const client = { x: 400, y: 300 };
    const anchor = toBoardPoint(before, client, RECT);
    const after = zoomAt(before, 1.6, client, RECT);
    expect(after.scale).toBeCloseTo(1.6, 10);
    const anchorScreen = toScreenPoint(after, anchor, RECT);
    expect(anchorScreen.x).toBeCloseTo(client.x, 9);
    expect(anchorScreen.y).toBeCloseTo(client.y, 9);
  });

  it("已经平移 + 缩放过时，再次缩放仍以指针为中心", () => {
    const before: Viewport = { scale: 0.8, x: -140, y: 60 };
    const client = { x: 500, y: 260 };
    const anchor = toBoardPoint(before, client, RECT);
    const after = zoomAt(before, 0.5, client, RECT);
    const anchorScreen = toScreenPoint(after, anchor, RECT);
    expect(after.scale).toBeCloseTo(0.4, 10);
    expect(anchorScreen.x).toBeCloseTo(client.x, 9);
    expect(anchorScreen.y).toBeCloseTo(client.y, 9);
  });

  it("缩放收敛在上下限之间", () => {
    expect(zoomAt({ scale: 1, x: 0, y: 0 }, 100, { x: 0, y: 0 }, RECT).scale).toBe(MAX_SCALE);
    expect(zoomAt({ scale: 1, x: 0, y: 0 }, 0.001, { x: 0, y: 0 }, RECT).scale).toBe(MIN_SCALE);
    expect(clampScale(99)).toBe(MAX_SCALE);
    expect(clampScale(0)).toBe(MIN_SCALE);
    expect(clampScale(Number.NaN)).toBe(1);
  });

  it("到达上下限后继续缩放，锚点仍然不漂", () => {
    const atMax = zoomAt({ scale: MAX_SCALE, x: -50, y: -50 }, 3, { x: 300, y: 200 }, RECT);
    expect(atMax.scale).toBe(MAX_SCALE);
    const anchor = toBoardPoint({ scale: MAX_SCALE, x: -50, y: -50 }, { x: 300, y: 200 }, RECT);
    const screen = toScreenPoint(atMax, anchor, RECT);
    expect(screen.x).toBeCloseTo(300, 9);
    expect(screen.y).toBeCloseTo(200, 9);
  });

  it("滚轮增量归一化并按方向放大 / 缩小", () => {
    expect(wheelDeltaY({ deltaY: 100 })).toBe(100);
    expect(wheelDeltaY({ deltaY: 3, deltaMode: 1 })).toBe(48);
    expect(wheelDeltaY({ deltaY: 1, deltaMode: 2 })).toBe(400);
    expect(wheelZoomFactor(100)).toBeLessThan(1); // 向下滚 = 缩小
    expect(wheelZoomFactor(-100)).toBeGreaterThan(1); // 向上滚 = 放大
    expect(wheelZoomFactor(0)).toBe(1);
  });
});

describe("平移与收敛", () => {
  it("panBy 只改查看位置，不动缩放", () => {
    expect(panBy({ scale: 1.4, x: 10, y: 20 }, -5, 7)).toEqual({ scale: 1.4, x: 5, y: 27 });
  });

  it("clampViewport：内容比容器小时贴住原点", () => {
    const result = clampViewport({ scale: 1, x: -500, y: 900 }, { w: 1000, h: 800 }, { w: 600, h: 400 });
    expect(result).toEqual({ scale: 1, x: 0, y: 0 });
  });

  it("clampViewport：内容比容器大时不允许拖到看不见内容", () => {
    const container = { w: 1000, h: 800 };
    const content = { w: 2000, h: 1600 };
    const far = clampViewport({ scale: 1, x: 5000, y: 5000 }, container, content, 48);
    expect(far.x).toBe(48);
    expect(far.y).toBe(48);
    const back = clampViewport({ scale: 1, x: -5000, y: -5000 }, container, content, 48);
    expect(back.x).toBe(1000 - 2000 - 48);
    expect(back.y).toBe(800 - 1600 - 48);
    const inside = clampViewport({ scale: 1, x: -300, y: -200 }, container, content, 48);
    expect(inside).toEqual({ scale: 1, x: -300, y: -200 });
  });
});

describe("框选范围：屏幕拖动 → 板面矩形", () => {
  it("恒等视口下就是两个屏幕点之差", () => {
    const rect = rectFromDrag(IDENTITY_VIEWPORT, { x: 150, y: 100 }, { x: 350, y: 260 }, RECT);
    expect(rect).toEqual({ x: 30, y: 60, w: 200, h: 160 });
  });

  it("反向拖动也给出正宽高", () => {
    const rect = rectFromDrag(IDENTITY_VIEWPORT, { x: 350, y: 260 }, { x: 150, y: 100 }, RECT);
    expect(rect).toEqual({ x: 30, y: 60, w: 200, h: 160 });
  });

  it("缩放后框选范围按板面坐标换算（不是屏幕像素）", () => {
    const view: Viewport = { scale: 2, x: -100, y: -50 };
    // 屏幕 (200,100) → 板面 ((200-120)+100)/2 = 90, ((100-40)+50)/2 = 55
    const rect = rectFromDrag(view, { x: 200, y: 100 }, { x: 400, y: 200 }, RECT);
    expect(rect.x).toBeCloseTo(90, 10);
    expect(rect.y).toBeCloseTo(55, 10);
    expect(rect.w).toBeCloseTo(100, 10);
    expect(rect.h).toBeCloseTo(50, 10);
  });

  it("平移 + 缩放后框选范围与卡片矩形在同一坐标系里比较", () => {
    const view: Viewport = { scale: 0.5, x: 60, y: -30 };
    const start = toScreenPoint(view, { x: 0, y: 0 }, RECT);
    const end = toScreenPoint(view, { x: 400, y: 300 }, RECT);
    const rect = rectFromDrag(view, start, end, RECT);
    expect(rect.x).toBeCloseTo(0, 9);
    expect(rect.y).toBeCloseTo(0, 9);
    expect(rect.w).toBeCloseTo(400, 9);
    expect(rect.h).toBeCloseTo(300, 9);
  });
});

describe("滚动模型：内容用 transform 缩放 + 容器滚动查看", () => {
  const viewSize = { w: 1000, h: 700 };
  const content = { w: 2400, h: 1600 };

  it("scrollFromViewport 与 translate 是同一件事的两种写法", () => {
    expect(scrollFromViewport({ scale: 1, x: -120, y: 60 })).toEqual({ x: 120, y: -60 });
  });

  it("clampScroll：负数收敛到 0（浏览器不接受负滚动），上界是内容超出容器的部分", () => {
    expect(clampScroll({ x: 500, y: -500 }, 1, { w: 3000, h: 2000 }, content)).toEqual({ x: 0, y: 0 });
    const far = clampScroll({ x: 99999, y: 99999 }, 1, viewSize, content);
    expect(far.x).toBe(2400 - 1000);
    expect(far.y).toBe(1600 - 700);
    expect(clampScroll({ x: -99999, y: -99999 }, 1, viewSize, content)).toEqual({ x: 0, y: 0 });
    // 缩放变大后，可滚动范围跟着变大
    const zoomed = clampScroll({ x: 99999, y: 99999 }, 2, viewSize, content);
    expect(zoomed.x).toBe(2400 * 2 - 1000);
  });

  it("zoomAtScroll：缩放后指针下的板面点仍在同一屏幕位置", () => {
    const client = { x: 400, y: 300 };
    const rect = { left: 0, top: 0 };
    const scroll = { x: 120, y: 80 };
    const scale = 1;
    const anchor = toBoardPoint({ scale, x: -scroll.x, y: -scroll.y }, client, rect);
    const result = zoomAtScroll(scroll, scale, 2, client, rect, viewSize, content);
    expect(result.scale).toBeCloseTo(2, 10);
    const screen = toScreenPoint({ scale: result.scale, x: -result.scroll.x, y: -result.scroll.y }, anchor, rect);
    expect(screen.x).toBeCloseTo(client.x, 9);
    expect(screen.y).toBeCloseTo(client.y, 9);
  });

  it("zoomAtScroll：到达缩放上下限时锚点也不漂", () => {
    const client = { x: 700, y: 500 };
    const rect = { left: 0, top: 0 };
    const result = zoomAtScroll({ x: 0, y: 0 }, MAX_SCALE, 5, client, rect, viewSize, content);
    expect(result.scale).toBe(MAX_SCALE);
    const anchor = toBoardPoint({ scale: MAX_SCALE, x: 0, y: 0 }, client, rect);
    const screen = toScreenPoint({ scale: result.scale, x: -result.scroll.x, y: -result.scroll.y }, anchor, rect);
    expect(screen.x).toBeCloseTo(client.x, 9);
    expect(screen.y).toBeCloseTo(client.y, 9);
  });

  it("滚动换算与 translate 换算给出同一板面点", () => {
    const rect = { left: 40, top: 100 };
    const scroll = { x: 260, y: 140 };
    const scale = 1.5;
    const client = { x: 500, y: 400 };
    const byScroll = toBoardPoint({ scale, x: -scroll.x, y: -scroll.y }, client, rect);
    const byTranslate = toBoardPoint({ scale, x: -260, y: -140 }, client, rect);
    expect(byScroll).toEqual(byTranslate);
  });
});

describe("无限画布：视野、适配与点阵相位", () => {
  it("visibleWorldRect：空板同样是一块有限的板面区域（不依赖任何卡片）", () => {
    const rect = visibleWorldRect({ scale: 1, x: 0, y: 0 }, { w: 800, h: 600 });
    expect(rect).toEqual({ x: 0, y: 0, w: 800, h: 600 });
    // 平移到负坐标区域：视野跟着走（四个方向都连续）
    const panned = visibleWorldRect({ scale: 1, x: 8000, y: 6000 }, { w: 800, h: 600 });
    expect(panned).toEqual({ x: -8000, y: -6000, w: 800, h: 600 });
    // 缩放：视野覆盖的板面范围跟着变大
    const zoomed = visibleWorldRect({ scale: 0.5, x: 0, y: 0 }, { w: 800, h: 600 });
    expect(zoomed).toEqual({ x: 0, y: 0, w: 1600, h: 1200 });
  });

  it("worldBoundsOf：包含负坐标；空数组返回 null（不编造一个框）", () => {
    expect(worldBoundsOf([])).toBeNull();
    const bounds = worldBoundsOf([
      { x: -8000, y: -6000, w: 260, h: 170 },
      { x: 100, y: 100, w: 260, h: 170 },
    ]);
    expect(bounds).toEqual({ x: -8000, y: -6000, w: 8360, h: 6270 });
  });

  it("fitViewToBounds：包围盒中心对准可用区中心，比例按可用宽高求", () => {
    const bounds = { x: -1000, y: -500, w: 2000, h: 1000 };
    const view = fitViewToBounds(bounds, { w: 800, h: 600 }, {
      area: { left: 40, top: 100, right: 840, bottom: 700 },
      rect: { left: 40, top: 100 },
      margin: 0,
      maxScale: 1,
      minScale: 0.05,
    })!;
    // 可用 800×600，包围盒 2000×1000 → 比例 = min(0.4, 0.6) = 0.4
    expect(view.scale).toBeCloseTo(0.4, 10);
    // 包围盒中心 (0,0) 落在可用区中心（相对视口原点 = 400, 300）
    expect(view.x).toBeCloseTo(400, 10);
    expect(view.y).toBeCloseTo(300, 10);
    // 反算：包围盒中心投影回屏幕就是可用区中心
    const center = toScreenPoint(view, { x: 0, y: 0 }, { left: 40, top: 100 });
    expect(center.x).toBeCloseTo(440, 10);
    expect(center.y).toBeCloseTo(400, 10);
  });

  it("fitViewToBounds：两张远卡需要低于 25% 时如实给出更小的比例", () => {
    // 两张卡相距 20000：可用区 800 宽 → 需要 0.04
    const bounds = { x: -10000, y: 0, w: 20000, h: 200 };
    const view = fitViewToBounds(bounds, { w: 800, h: 600 }, {
      area: { left: 0, top: 0, right: 800, bottom: 600 },
      rect: { left: 0, top: 0 },
      margin: 0,
      maxScale: 1,
      minScale: 0.01,
    })!;
    expect(view.scale).toBeLessThan(MIN_SCALE);
    expect(view.scale).toBeCloseTo(0.04, 10);
    // 这个比例必须能贯通到投影 / 逆投影（不能被 clampScale 夹回 0.25）
    const point = toBoardPoint(view, { x: 400, y: 300 }, { left: 0, top: 0 });
    expect(point.x).toBeCloseTo(0, 6);
    const back = toScreenPoint(view, point, { left: 0, top: 0 });
    expect(back.x).toBeCloseTo(400, 6);
  });

  it("fitViewToBounds：单卡居中且不被异常放大（比例上限 1）", () => {
    const bounds = { x: 100, y: 100, w: 260, h: 170 };
    const view = fitViewToBounds(bounds, { w: 800, h: 600 }, {
      area: { left: 0, top: 0, right: 800, bottom: 600 },
      rect: { left: 0, top: 0 },
      margin: 0,
      maxScale: 1,
      minScale: MIN_SCALE,
    })!;
    expect(view.scale).toBe(1);
    // 卡片中心 (230, 185) 落在可用区中心 (400, 300)
    expect(view.x).toBeCloseTo(400 - 230, 10);
    expect(view.y).toBeCloseTo(300 - 185, 10);
  });

  it("fitViewToBounds：空包围盒 / 零面积可用区返回 null（调用方回到初始视野）", () => {
    expect(fitViewToBounds(null, { w: 800, h: 600 }, {
      area: { left: 0, top: 0, right: 800, bottom: 600 },
      rect: { left: 0, top: 0 },
    })).toBeNull();
    expect(fitViewToBounds({ x: 0, y: 0, w: 100, h: 100 }, { w: 800, h: 600 }, {
      area: { left: 0, top: 0, right: 0, bottom: 0 },
      rect: { left: 0, top: 0 },
    })).toBeNull();
  });

  it("clampScale 接受更小的下限：查看全部之后滚轮不会跳回 0.25", () => {
    expect(clampScale(0.1)).toBe(MIN_SCALE);
    expect(clampScale(0.1, 0.05)).toBeCloseTo(0.1, 10);
    // 下限本身也被夹在 [0, MAX_SCALE] 里
    expect(clampScale(0.1, -1)).toBeCloseTo(0.1, 10);
    expect(clampScale(99, 0.05)).toBe(MAX_SCALE);
  });

  it("zoomAt 接受更小的下限：低于 25% 时继续缩放不跳", () => {
    const view: Viewport = { scale: 0.1, x: 0, y: 0 };
    const zoomed = zoomAt(view, 0.5, { x: 400, y: 300 }, RECT, 0.05);
    expect(zoomed.scale).toBeCloseTo(0.05, 10);
    // 指针下的板面点仍在同一屏幕位置
    const anchor = toBoardPoint(view, { x: 400, y: 300 }, RECT);
    const screen = toScreenPoint(zoomed, anchor, RECT);
    expect(screen.x).toBeCloseTo(400, 6);
    expect(screen.y).toBeCloseTo(300, 6);
  });

  it("panBy 不夹缩放：查看全部压到 10% 之后平移一次不会突然放大", () => {
    const panned = panBy({ scale: 0.1, x: 0, y: 0 }, 30, 40);
    expect(panned).toEqual({ scale: 0.1, x: 30, y: 40 });
  });

  it("safeScale 只挡非法值，不夹到 MIN_SCALE（投影与画面必须读同一个比例）", () => {
    expect(safeScale(0.1)).toBe(0.1);
    expect(safeScale(0)).toBe(1);
    expect(safeScale(Number.NaN)).toBe(1);
    expect(safeScale(-2)).toBe(1);
  });

  it("gridPhase：负偏移用取模收口，往返跨过零点不跳格", () => {
    expect(gridPhase(0, 24)).toBe(0);
    expect(gridPhase(24, 24)).toBe(0);
    expect(gridPhase(-24, 24)).toBe(0);
    expect(gridPhase(-1, 24)).toBe(23);
    expect(gridPhase(-25, 24)).toBe(23);
    expect(gridPhase(25, 24)).toBe(1);
    // 往返：-7 与 +17 是同一个相位
    expect(gridPhase(-7, 24)).toBe(gridPhase(17, 24));
    // 非法间距不产生 NaN
    expect(gridPhase(5, 0)).toBe(0);
    expect(gridPhase(5, Number.NaN)).toBe(0);
  });

  it("dotGridMetrics：100% 时 24px；缩得很小时按 2 的倍数合并点", () => {
    expect(dotGridMetrics({ scale: 1, x: 0, y: 0 }, 24, 8).spacing).toBe(24);
    // 50%：12px ≥ 8px 下限，不合并
    expect(dotGridMetrics({ scale: 0.5, x: 0, y: 0 }, 24, 8).spacing).toBe(12);
    // 25%：6px < 8px → 世界间距翻倍到 48，屏幕间距 12px
    expect(dotGridMetrics({ scale: 0.25, x: 0, y: 0 }, 24, 8).spacing).toBe(12);
    // 10%：2.4px → 翻倍两次到 96，屏幕间距 9.6px
    expect(dotGridMetrics({ scale: 0.1, x: 0, y: 0 }, 24, 8).spacing).toBeCloseTo(9.6, 10);
    // 相位用同一个世界原点算：合并前后不跳格
    const metrics = dotGridMetrics({ scale: 0.25, x: -30, y: 18 }, 24, 8);
    expect(metrics.phaseX).toBe(gridPhase(-30, metrics.spacing));
    expect(metrics.phaseY).toBe(gridPhase(18, metrics.spacing));
  });
});

describe("矩形工具", () => {
  it("rectsIntersect：只碰到边不算相交", () => {
    const a = { x: 0, y: 0, w: 100, h: 60 };
    expect(rectsIntersect(a, { x: 100, y: 0, w: 100, h: 60 })).toBe(false);
    expect(rectsIntersect(a, { x: 99, y: 0, w: 100, h: 60 })).toBe(true);
    expect(rectsIntersect(a, { x: 0, y: 60, w: 100, h: 60 })).toBe(false);
    expect(rectsIntersect(a, { x: 0, y: 59, w: 100, h: 60 })).toBe(true);
    expect(rectsIntersect(a, { x: 200, y: 200, w: 10, h: 10 })).toBe(false);
  });

  it("overlapArea 与 pointInRect", () => {
    const a = { x: 0, y: 0, w: 100, h: 60 };
    expect(overlapArea(a, { x: 50, y: 30, w: 100, h: 60 })).toBe(50 * 30);
    expect(overlapArea(a, { x: 100, y: 0, w: 10, h: 10 })).toBe(0);
    expect(pointInRect({ x: 0, y: 0 }, a)).toBe(true);
    expect(pointInRect({ x: 100, y: 60 }, a)).toBe(true);
    expect(pointInRect({ x: 100.5, y: 60 }, a)).toBe(false);
  });
});
