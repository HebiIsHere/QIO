import { describe, expect, it, beforeEach } from "vitest";
import { getTheme, setTheme, toggleTheme, THEME_KEY } from "../theme";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
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
});
