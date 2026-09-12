import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import {
  getTheme,
  getThemePreference,
  setTheme,
  setThemePreference,
  toggleTheme,
  watchSystemTheme,
  THEME_KEY,
} from "../theme";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("theme util", () => {
  it("无任何设置时默认暗色", () => {
    expect(getTheme()).toBe("dark");
  });

  it("setTheme 写入 html[data-theme] 与 localStorage qio-theme", () => {
    setTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    setTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
  });

  it("getTheme 优先读 data-theme，其次 localStorage", () => {
    localStorage.setItem(THEME_KEY, "light");
    expect(getTheme()).toBe("light");
    document.documentElement.dataset.theme = "dark";
    expect(getTheme()).toBe("dark");
  });

  it("toggleTheme 翻转并持久化", () => {
    expect(toggleTheme()).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    expect(toggleTheme()).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
  });

  it("localStorage 抛 SecurityError 时内存兜底，main 启动不白屏", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError: access denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("SecurityError: access denied");
    });
    // main.ts 启动路径：setTheme(getTheme()) 不抛异常
    expect(() => setTheme(getTheme())).not.toThrow();
    setTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    document.documentElement.removeAttribute("data-theme");
    expect(getTheme()).toBe("light"); // 内存兜底（localStorage 不可用）
    expect(toggleTheme()).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});

describe("theme 偏好（system / dark / light）", () => {
  function mockMatchMedia(initialDark: boolean) {
    const listeners: (() => void)[] = [];
    let dark = initialDark;
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: (query: string) => ({
        matches: dark,
        media: query,
        addEventListener: (_t: string, cb: () => void) => listeners.push(cb),
        removeEventListener: () => {},
      }),
    });
    return {
      listeners,
      setDark(v: boolean) {
        dark = v;
      },
    };
  }

  it("默认偏好是 system：跟随系统主题解析", () => {
    const mm = mockMatchMedia(true);
    expect(getThemePreference()).toBe("system");
    expect(getTheme()).toBe("dark");
    mm.setDark(false);
    expect(getTheme()).toBe("light");
    Reflect.deleteProperty(window, "matchMedia");
  });

  it("setThemePreference 持久化偏好并立即应用", () => {
    setThemePreference("light");
    expect(getThemePreference()).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    setThemePreference("system");
    expect(getThemePreference()).toBe("system");
    expect(localStorage.getItem(THEME_KEY)).toBe("system");
  });

  it("watchSystemTheme：偏好为 system 时跟随系统切换；显式 dark/light 时不跟随", () => {
    const mm = mockMatchMedia(true);
    setThemePreference("system");
    const stop = watchSystemTheme();

    mm.setDark(false);
    mm.listeners.forEach((cb) => cb());
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(getThemePreference()).toBe("system"); // 系统变化不覆盖偏好

    setThemePreference("dark");
    mm.setDark(false);
    mm.listeners.forEach((cb) => cb());
    expect(document.documentElement.dataset.theme).toBe("dark");

    stop();
    Reflect.deleteProperty(window, "matchMedia");
  });
});
