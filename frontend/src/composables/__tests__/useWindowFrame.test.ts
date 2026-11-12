/**
 * useWindowFrame 状态通道的归一化与落盘（不启动真实 IPC）。
 *
 * 壳 payload 是「可选字段 + 超集」：前端必须把任何缺字段/类型不对的输入归一化成
 * 安全状态（normal/css），并把结果写进 <html data-window-frame / data-window-state>
 * —— 这两个属性是 CSS 令牌唯一的开关。
 */
import { afterEach, describe, expect, it } from "vitest";
import {
  applyWindowFrameState,
  normalizeWindowState,
} from "../useWindowFrame";

const root = () => document.documentElement;

afterEach(() => {
  delete root().dataset.windowFrame;
  delete root().dataset.windowState;
});

describe("normalizeWindowState", () => {
  it("读 pad 与 padding 两种壳字段，并保留 dpi/zone/passthrough 等", () => {
    const withPad = normalizeWindowState({
      state: "snapped",
      zone: "left-half",
      frame: "css",
      pad: 56,
      radius: 40,
      dpi: 192,
      scaleFactor: 2,
      passthrough: "poll",
    });
    expect(withPad.state).toBe("snapped");
    expect(withPad.zone).toBe("left-half");
    expect(withPad.paddingPx).toBe(56);
    expect(withPad.radiusPx).toBe(40);
    expect(withPad.dpi).toBe(192);
    expect(withPad.passthrough).toBe("poll");

    const withPadding = normalizeWindowState({ padding: 56, radius: 40 });
    expect(withPadding.paddingPx).toBe(56);
  });

  it("未知/缺字段落安全默认：normal + css", () => {
    expect(normalizeWindowState(null)).toMatchObject({ state: "normal", frame: "css" });
    expect(normalizeWindowState({ state: "weird", frame: "unknown" })).toMatchObject({
      state: "normal",
      frame: "css",
    });
    expect(normalizeWindowState({ rc: { x: 1 } }).rect).toBeNull();
  });

  it("rect 归一化成 {x,y,w,h}（缺的分量补 0）", () => {
    const parsed = normalizeWindowState({ rect: { x: 10, y: 20 } });
    expect(parsed.rect).toEqual({ x: 10, y: 20, w: 0, h: 0 });
  });
});

describe("applyWindowFrameState", () => {
  it("写 data-window-frame/state；CSS 令牌据此归零/恢复", () => {
    applyWindowFrameState(normalizeWindowState({ state: "normal", frame: "css" }));
    expect(root().dataset.windowFrame).toBe("css");
    expect(root().dataset.windowState).toBe("normal");

    applyWindowFrameState(normalizeWindowState({ state: "maximized", frame: "css" }));
    expect(root().dataset.windowState).toBe("maximized");

    applyWindowFrameState(normalizeWindowState({ state: "normal", frame: "native" }));
    expect(root().dataset.windowFrame).toBe("native");
  });
});
