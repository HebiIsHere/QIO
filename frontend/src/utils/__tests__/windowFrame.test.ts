/**
 * utils/windowFrame：可见内容区几何（运行时单测）。
 *
 * 事实来源是 CSS 令牌，但测试里不能依赖浏览器排版/样式表加载：
 * 用 <html data-window-frame> 标记 + innerWidth/innerHeight 桩驱动兜底分支，
 * 与真实应用里 useWindowFrame 写的标记同一条路径。
 */
import { afterEach, describe, expect, it } from "vitest";
import {
  clampToContentRect,
  contentRect,
  contentViewport,
  frameInsetPx,
  framePadPx,
  isFrameZeroed,
  windowRadiusPx,
} from "../windowFrame";

const root = () => document.documentElement;
const originalWidth = Object.getOwnPropertyDescriptor(window, "innerWidth");
const originalHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");

function setViewport(width: number, height: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: height });
}

afterEach(() => {
  delete root().dataset.windowFrame;
  delete root().dataset.windowState;
  if (originalWidth) Object.defineProperty(window, "innerWidth", originalWidth);
  if (originalHeight) Object.defineProperty(window, "innerHeight", originalHeight);
});

describe("frameInsetPx / contentRect（透明留白窗）", () => {
  it("css 模式：28px 留白、20px 圆角，内容视口 = 视口 - 2*pad", () => {
    root().dataset.windowFrame = "css";
    setViewport(1200, 800);
    expect(isFrameZeroed()).toBe(false);
    expect(framePadPx()).toBe(28);
    expect(frameInsetPx()).toBe(28);
    expect(windowRadiusPx()).toBe(20);
    expect(contentViewport()).toEqual({ width: 1144, height: 744, inset: 28 });
    expect(contentRect()).toEqual({ left: 28, top: 28, right: 1172, bottom: 772, width: 1144, height: 744 });
  });

  it("最大化/贴靠：留白/圆角归零（内容铺满可见区）", () => {
    root().dataset.windowFrame = "css";
    root().dataset.windowState = "snapped";
    setViewport(1200, 800);
    expect(isFrameZeroed()).toBe(true);
    expect(framePadPx()).toBe(0);
    expect(windowRadiusPx()).toBe(0);
    expect(contentRect()).toEqual({ left: 0, top: 0, right: 1200, bottom: 800, width: 1200, height: 800 });
  });

  it("native/dwm：无透明留白（等价 0.3.1 全窗布局）", () => {
    root().dataset.windowFrame = "native";
    setViewport(1200, 800);
    expect(frameInsetPx()).toBe(0);
    root().dataset.windowFrame = "dwm";
    expect(frameInsetPx()).toBe(0);
  });

  it("没有壳标记（jsdom/旧壳）时按 0，与改造前行为一致", () => {
    setViewport(1200, 800);
    expect(frameInsetPx()).toBe(0);
    expect(windowRadiusPx()).toBe(0);
    expect(contentRect().left).toBe(0);
  });
});

describe("clampToContentRect（左右锚点菜单/提示钳制）", () => {
  it("左/上越界被推回可见内容区，右/下越界被收回", () => {
    root().dataset.windowFrame = "css";
    setViewport(1200, 800);
    const leftTop = clampToContentRect(-50, -80, 300, 180, 8);
    expect(leftTop.left).toBe(36);
    expect(leftTop.top).toBe(36);
    expect(leftTop.clampedX).toBe(true);
    expect(leftTop.clampedY).toBe(true);

    const rightBottom = clampToContentRect(1100, 700, 300, 180, 8);
    expect(rightBottom.left).toBe(1172 - 8 - 300);
    expect(rightBottom.top).toBe(772 - 8 - 180);
    expect(rightBottom.clampedX).toBe(true);
    expect(rightBottom.clampedY).toBe(true);
  });

  it("已在可见区内时原样返回（不引入第二套基准）", () => {
    root().dataset.windowFrame = "css";
    setViewport(1200, 800);
    const inside = clampToContentRect(200, 160, 240, 180, 8);
    expect(inside).toEqual({ left: 200, top: 160, clampedX: false, clampedY: false });
  });

  it("最大化时不钳制到留白（留白归零），钳制边界 = 整个视口", () => {
    root().dataset.windowFrame = "css";
    root().dataset.windowState = "maximized";
    setViewport(1200, 800);
    const rect = clampToContentRect(-20, 0, 100, 80, 4);
    expect(rect.left).toBe(4);
  });
});
