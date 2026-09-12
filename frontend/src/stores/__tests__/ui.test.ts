import { describe, expect, it, beforeEach } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useUiStore, DEVELOPER_MODE_KEY } from "../ui";

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
});

describe("ui store 开发者模式", () => {
  it("默认关闭（正常模式不暴露 FPS / token 等诊断）", () => {
    expect(useUiStore().developerMode).toBe(false);
  });

  it("开启后持久化到 localStorage，重新加载仍为开启", () => {
    useUiStore().setDeveloperMode(true);
    expect(localStorage.getItem(DEVELOPER_MODE_KEY)).toBe("1");
    setActivePinia(createPinia());
    expect(useUiStore().developerMode).toBe(true);
  });

  it("关闭后清除持久化标记", () => {
    const ui = useUiStore();
    ui.setDeveloperMode(true);
    ui.setDeveloperMode(false);
    expect(localStorage.getItem(DEVELOPER_MODE_KEY)).toBeNull();
    setActivePinia(createPinia());
    expect(useUiStore().developerMode).toBe(false);
  });
});
