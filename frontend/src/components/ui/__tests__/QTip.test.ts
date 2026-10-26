/**
 * 用途说明浮层（2026-10-26 统一改造）。
 *
 * 守的是“看起来最简单、实际最容易出问题”的三层：
 * 1) 定位算法：下方放不下翻到上方、靠近右边缘夹回视口、永不越出视口；
 * 2) 全局唯一：同一时刻只有一个 role=tooltip，浮层由 QTipLayer 单点承载；
 * 3) 清理：隐藏后状态归零，不会在换页/滚动/卸载后留一条悬空说明。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { readFileSync } from "node:fs";
import QTipLayer from "../QTipLayer.vue";
import {
  computeTipPosition,
  hideTip,
  showTip,
  tipState,
  TIP_GAP_PX,
  TIP_VIEWPORT_MARGIN_PX,
  vQioTip,
  type TipAnchorRect,
} from "../../../composables/useQioTip";

afterEach(() => {
  hideTip();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

function anchorRect(overrides: Partial<TipAnchorRect> = {}): TipAnchorRect {
  return { top: 100, left: 100, right: 140, bottom: 140, width: 40, height: 40, ...overrides };
}

describe("computeTipPosition", () => {
  it("下方空间足够：放在锚点正下方并水平居中", () => {
    const pos = computeTipPosition(anchorRect(), { width: 120, height: 30 }, { width: 800, height: 600 });
    expect(pos.placement).toBe("bottom");
    expect(pos.top).toBe(140 + TIP_GAP_PX);
    // 锚点中心 120，浮层宽 120 → 左边缘 60
    expect(pos.left).toBe(60);
  });

  it("下方放不下、上方够：翻到锚点上方", () => {
    const pos = computeTipPosition(
      anchorRect({ top: 540, bottom: 580 }),
      { width: 120, height: 30 },
      { width: 800, height: 600 },
    );
    expect(pos.placement).toBe("top");
    expect(pos.top).toBe(540 - TIP_GAP_PX - 30);
  });

  it("靠近右边缘：左移夹回视口（不横向溢出）", () => {
    const pos = computeTipPosition(
      anchorRect({ left: 780, right: 800, width: 20, bottom: 140, top: 100 }),
      { width: 120, height: 30 },
      { width: 800, height: 600 },
    );
    expect(pos.left).toBe(800 - 120 - TIP_VIEWPORT_MARGIN_PX);
  });

  it("靠近左边缘：至少留出视口边距", () => {
    const pos = computeTipPosition(
      anchorRect({ left: 0, right: 10, width: 10 }),
      { width: 120, height: 30 },
      { width: 800, height: 600 },
    );
    expect(pos.left).toBe(TIP_VIEWPORT_MARGIN_PX);
  });

  it("上下都放不下：仍不越出视口（取可用空间较大的一侧并夹住）", () => {
    const pos = computeTipPosition(
      anchorRect({ top: 290, bottom: 310, height: 20 }),
      { width: 120, height: 400 },
      { width: 800, height: 600 },
    );
    expect(pos.top).toBeGreaterThanOrEqual(TIP_VIEWPORT_MARGIN_PX);
    expect(pos.top).toBeLessThanOrEqual(600 - 400 - TIP_VIEWPORT_MARGIN_PX);
  });
});

function flush(): Promise<void> {
  return nextTick().then(() => nextTick()).then(() => undefined);
}

describe("QTipLayer 全局唯一浮层", () => {
  it("初始不渲染说明；showTip 后出现唯一 role=tooltip，文字与锚点对应", async () => {
    const layer = mount(QTipLayer, { attachTo: document.body });
    const anchor = document.createElement("button");
    document.body.appendChild(anchor);
    expect(document.querySelector('[role="tooltip"]')).toBeNull();

    showTip("复制到剪贴板", anchor);
    await flush();
    const tips = document.querySelectorAll('[role="tooltip"]');
    expect(tips.length).toBe(1);
    expect(tips[0].textContent?.trim()).toBe("复制到剪贴板");
    expect(tips[0].hasAttribute("data-qio-tip")).toBe(true);
    // 已有真实位置（jsdom 尺寸为 0 也会算出确定性坐标）
    expect(tipState.position).not.toBeNull();

    hideTip();
    await flush();
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
    layer.unmount();
  });

  it("同时只存在一条：换锚点直接替换内容，不叠加", async () => {
    const layer = mount(QTipLayer, { attachTo: document.body });
    const a = document.createElement("button");
    const b = document.createElement("button");
    document.body.append(a, b);

    showTip("第一条", a);
    await flush();
    showTip("第二条", b);
    await flush();
    const tips = document.querySelectorAll('[role="tooltip"]');
    expect(tips.length).toBe(1);
    expect(tips[0].textContent?.trim()).toBe("第二条");
    layer.unmount();
  });

  it("浮层自带 14px 圆角 token 与不可点击（不挡操作）", () => {
    const sourcePath = "../QTipLayer.vue";
    const source = readFileSync(new URL(sourcePath, import.meta.url), "utf8");
    expect(source).toContain("border-radius: var(--qio-ui-radius)");
    expect(source).toContain("pointer-events: none");
    expect(source).toContain('role="tooltip"');
  });

  it("卸载时清掉监听与状态：之后滚动/按键不再产生浮层", async () => {
    const layer = mount(QTipLayer, { attachTo: document.body });
    const anchor = document.createElement("button");
    document.body.appendChild(anchor);
    showTip("说明", anchor);
    await flush();
    layer.unmount();
    expect(tipState.open).toBe(false);
    window.dispatchEvent(new Event("scroll"));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(document.querySelector('[role="tooltip"]')).toBeNull();
  });
});

describe("v-qio-tip 指令清理", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("同一元素改文字后只显示新文字（不会叠加两条记录）", async () => {
    const layer = mount(QTipLayer, { attachTo: document.body });
    const w = mount(
      {
        components: {},
        template: '<button class="target" v-qio-tip="text">目标</button>',
        data: () => ({ text: "第一版说明" }),
        directives: { qioTip: vQioTip },
      },
      { attachTo: document.body },
    );
    const btn = w.find(".target").element;
    btn.dispatchEvent(new Event("pointerenter"));
    vi.advanceTimersByTime(600);
    await flush();
    expect(document.querySelectorAll('[role="tooltip"]').length).toBe(1);
    expect(document.querySelector('[role="tooltip"]')?.textContent?.trim()).toBe("第一版说明");

    // 更新绑定值（指令 updated 路径）
    (w.vm as unknown as { text: string }).text = "第二版说明";
    await nextTick();
    await nextTick();
    btn.dispatchEvent(new Event("pointerenter"));
    vi.advanceTimersByTime(600);
    await flush();
    expect(document.querySelectorAll('[role="tooltip"]').length).toBe(1);
    expect(document.querySelector('[role="tooltip"]')?.textContent?.trim()).toBe("第二版说明");
    w.unmount();
    layer.unmount();
  });
});
