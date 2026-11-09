/**
 * 用途说明浮层（QTip）的真实尺寸定位链路验证（用户提示词 §五.6）。
 *
 * 现状：定位算法 computeTipPosition 已有纯函数单测；但「QTipLayer 先量浮层真实尺寸、
 * 再写回位置」这一步在 jsdom 里因为不做排版（尺寸都是 0）从没被验证过。这里用
 * getBoundingClientRect 桩件模拟真实布局尺寸，证明：
 * - 水平/垂直定位读的是**量到的**浮层尺寸（不是估算值）；
 * - 下方放不下时翻到上方（若用 0 高度估算会误判为「下方放得下」）；
 * - 靠近视口边缘时留出 8px 视口留白（TIP_VIEWPORT_MARGIN_PX）。
 *
 * 本文件只验证既有实现，不修改组件；发现真实缺陷才修（本轮未发现）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import QTipLayer from "../ui/QTipLayer.vue";
import {
  hideTip,
  showTip,
  tipState,
  TIP_GAP_PX,
  TIP_VIEWPORT_MARGIN_PX,
} from "../../composables/useQioTip";

const VIEWPORT = { width: 800, height: 600 };
const TIP_SIZE = { width: 120, height: 30 };

interface FakeRect {
  top: number;
  left: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
}

function rect(overrides: Partial<FakeRect> = {}): FakeRect {
  const left = overrides.left ?? 0;
  const top = overrides.top ?? 0;
  const width = overrides.width ?? 0;
  const height = overrides.height ?? 0;
  return {
    top,
    left,
    width,
    height,
    right: overrides.right ?? left + width,
    bottom: overrides.bottom ?? top + height,
  };
}

function flush(): Promise<void> {
  return nextTick().then(() => nextTick()).then(() => undefined);
}

const ORIGINAL_INNER_WIDTH = Object.getOwnPropertyDescriptor(window, "innerWidth");
const ORIGINAL_INNER_HEIGHT = Object.getOwnPropertyDescriptor(window, "innerHeight");
let spy: ReturnType<typeof vi.spyOn> | null = null;

afterEach(() => {
  hideTip();
  spy?.mockRestore();
  spy = null;
  if (ORIGINAL_INNER_WIDTH) Object.defineProperty(window, "innerWidth", ORIGINAL_INNER_WIDTH);
  if (ORIGINAL_INNER_HEIGHT) Object.defineProperty(window, "innerHeight", ORIGINAL_INNER_HEIGHT);
  document.body.innerHTML = "";
});

function installLayout(anchor: HTMLElement, anchorRect: FakeRect, tipSize: { width: number; height: number }) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: VIEWPORT.width });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: VIEWPORT.height });
  (anchor as HTMLElement & { __rect?: FakeRect }).__rect = anchorRect;
  spy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    if (this.classList?.contains("qio-tip")) return rect(tipSize) as unknown as DOMRect;
    const own = (this as HTMLElement & { __rect?: FakeRect }).__rect;
    return (own ?? rect()) as unknown as DOMRect;
  });
}

describe("QTipLayer 用真实测量尺寸定位", () => {
  it("下方放不下时翻到上方：用的是量到的浮层高度，不是 0 的估算", async () => {
    const layer = mount(QTipLayer, { attachTo: document.body });
    const anchor = document.createElement("button");
    document.body.appendChild(anchor);
    // 锚点贴底：下方只剩 600-560=40px，放不下 30px 浮层（含 8px 间距与 8px 留白 = 46px）
    installLayout(anchor, rect({ top: 520, left: 100, width: 40, height: 40, bottom: 560 }), TIP_SIZE);

    showTip("说明", anchor);
    await flush();

    expect(tipState.position).not.toBeNull();
    expect(tipState.position?.placement).toBe("top");
    expect(tipState.position?.top).toBe(520 - TIP_GAP_PX - TIP_SIZE.height);
    // 真写到 DOM：可见且坐标来自测量结果
    const tip = document.querySelector('[role="tooltip"]') as HTMLElement;
    expect(tip).toBeTruthy();
    expect(tip.style.top).toBe(String(520 - TIP_GAP_PX - TIP_SIZE.height) + "px");
    expect(tip.style.left).toBe(60 + "px");
    expect(tip.style.visibility).toBe("visible");

    layer.unmount();
  });

  it("靠近右边缘：按量到的宽度夹回视口，保留 8px 留白", async () => {
    const layer = mount(QTipLayer, { attachTo: document.body });
    const anchor = document.createElement("button");
    document.body.appendChild(anchor);
    installLayout(anchor, rect({ top: 100, left: 780, width: 20, height: 40, bottom: 140 }), TIP_SIZE);

    showTip("说明", anchor);
    await flush();

    expect(tipState.position?.placement).toBe("bottom");
    expect(tipState.position?.left).toBe(VIEWPORT.width - TIP_SIZE.width - TIP_VIEWPORT_MARGIN_PX);
    const tip = document.querySelector('[role="tooltip"]') as HTMLElement;
    expect(tip.style.left).toBe(String(VIEWPORT.width - TIP_SIZE.width - TIP_VIEWPORT_MARGIN_PX) + "px");

    layer.unmount();
  });

  it("上下都放不下：仍然夹在视口内（至少 8px 留白），不会把浮层推出屏幕", async () => {
    const layer = mount(QTipLayer, { attachTo: document.body });
    const anchor = document.createElement("button");
    document.body.appendChild(anchor);
    // 浮层比可用空间高：任何一侧都放不下，必须夹到视口留白内
    installLayout(
      anchor,
      rect({ top: 280, left: 100, width: 40, height: 40, bottom: 320 }),
      { width: 120, height: 560 },
    );

    showTip("说明", anchor);
    await flush();

    expect(tipState.position).not.toBeNull();
    expect(tipState.position!.top).toBeGreaterThanOrEqual(TIP_VIEWPORT_MARGIN_PX);
    expect(tipState.position!.left).toBeGreaterThanOrEqual(TIP_VIEWPORT_MARGIN_PX);

    layer.unmount();
  });
});
