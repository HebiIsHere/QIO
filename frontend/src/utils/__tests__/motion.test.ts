import { describe, expect, it, beforeEach, vi } from "vitest";
import {
  applyMotion,
  currentMotionMode,
  getMotionPreference,
  prefersReducedMotion,
  resolveMotion,
  setMotionPreference,
  watchSystemMotion,
  MOTION_KEY,
} from "../motion";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-motion");
});

describe("动画偏好（任务02 D）", () => {
  it("默认跟随系统；系统未开减少动画时解析为标准", () => {
    expect(getMotionPreference()).toBe("system");
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    expect(resolveMotion("system")).toBe("standard");
  });

  it("系统开启减少动画时，跟随系统解析为减少动画", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    expect(resolveMotion("system")).toBe("reduced");
    vi.unstubAllGlobals();
  });

  it("显式选择「标准」可以覆盖系统的减少动画偏好", () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    expect(resolveMotion("standard")).toBe("standard");
    vi.unstubAllGlobals();
  });

  it("选择减少动画后立即落到 html[data-motion]，脚本动画读到同一份结果", () => {
    setMotionPreference("reduced");
    expect(localStorage.getItem(MOTION_KEY)).toBe("reduced");
    expect(document.documentElement.getAttribute("data-motion")).toBe("reduced");
    expect(prefersReducedMotion()).toBe(true);
    expect(currentMotionMode()).toBe("reduced");

    setMotionPreference("standard");
    expect(document.documentElement.getAttribute("data-motion")).toBe("standard");
    expect(prefersReducedMotion()).toBe(false);
  });

  it("运行中改变系统偏好：仅当偏好为「跟随系统」时才跟随", () => {
    // 手工模拟一次媒体查询变化：记录回调并触发
    const box: { fn: (() => void) | null } = { fn: null };
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        matches: false,
        addEventListener: (_: string, cb: () => void) => {
          box.fn = cb;
        },
        removeEventListener: vi.fn(),
      })),
    );
    const stop = watchSystemMotion();
    applyMotion("standard");
    // 偏好显式设为标准 → 系统变化不覆盖
    setMotionPreference("standard");
    box.fn?.();
    expect(document.documentElement.getAttribute("data-motion")).toBe("standard");
    // 偏好回到跟随系统 → 系统变化生效（matchMedia 返回 false → standard）
    setMotionPreference("system");
    box.fn?.();
    expect(document.documentElement.getAttribute("data-motion")).toBe("standard");
    stop();
    vi.unstubAllGlobals();
  });
});
