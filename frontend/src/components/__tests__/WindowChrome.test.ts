/**
 * 顶部全局栏（无边框窗口的自定义标题条）。
 *
 * 守的几条：
 * - 浏览器预览里不许画点了没反应的窗口按钮；设置入口是真的可用的（会进设置页）；
 * - 三个窗口按钮必须真的调到壳的窗口接口，且最大化状态跟着系统走；
 * - 按钮/字标不能长在拖拽区里面；拖拽面始终存在；
 * - 标题条只在对话页加高，离开时恢复，不影响设置/星球/互动页。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import WindowChrome from "../WindowChrome.vue";

const shell = vi.hoisted(() => {
  const unlisten = vi.fn();
  const state: { resized: (() => void) | null } = { resized: null };
  const win = {
    minimize: vi.fn(async () => {}),
    toggleMaximize: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
    isMaximized: vi.fn(async () => false),
    onResized: vi.fn(async (cb: () => void) => {
      state.resized = cb;
      return unlisten;
    }),
  };
  const push = vi.fn(async () => undefined);
  return { win, unlisten, state, push };
});

vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => shell.win }));

vi.mock("../../router", async () => {
  const { ref } = await import("vue");
  const currentRoute = ref<{ name: string }>({ name: "conversation" });
  return {
    default: { currentRoute, push: shell.push },
    setRouteName: (name: string) => {
      currentRoute.value = { name };
    },
  };
});

import * as routerModule from "../../router";

function setRouteName(name: string) {
  (routerModule as unknown as { setRouteName: (name: string) => void }).setRouteName(name);
}

function setShell(on: boolean) {
  const g = globalThis as unknown as Record<string, unknown>;
  if (on) g.__TAURI_INTERNALS__ = {};
  else delete g.__TAURI_INTERNALS__;
}

async function mountChrome() {
  const w = mount(WindowChrome);
  await flushPromises();
  await flushPromises();
  return w;
}

const TITLEBAR_PROPERTY = "--titlebar-height";

beforeEach(() => {
  vi.clearAllMocks();
  shell.state.resized = null;
  shell.win.isMaximized.mockResolvedValue(false);
  setShell(true);
  setRouteName("conversation");
  document.documentElement.style.removeProperty(TITLEBAR_PROPERTY);
});

afterEach(() => {
  setShell(false);
  document.documentElement.style.removeProperty(TITLEBAR_PROPERTY);
});

describe("顶部全局栏布局", () => {
  it("拖拽面始终存在（窗口没有系统标题栏，只能靠它移动）", async () => {
    const w = await mountChrome();
    expect(w.find("[data-tauri-drag-region]").exists()).toBe(true);
    w.unmount();
  });

  it("对话页左侧是 QIO 字标 + PERSONAL AGENT 副标，不显示话题标题", async () => {
    const w = await mountChrome();
    const brand = w.find(".brand-name");
    expect(brand.exists()).toBe(true);
    expect(brand.text()).toBe("QIO");
    expect(w.find(".brand-caption").text()).toBe("PERSONAL AGENT");
    // 话题标题属于中央工作区顶部，不在全局栏
    expect(w.text()).not.toContain("话题");
    w.unmount();
  });

  it("右侧固定顺序：设置入口 → 窗口控制（最右）", async () => {
    const w = await mountChrome();
    expect(w.find('button[aria-label="设置"]').exists()).toBe(true);
    // 设置按钮与窗口控制容器都在，窗口控制在最右
    expect(w.find(".window-controls").exists()).toBe(true);
    w.unmount();
  });

  it("设置按钮打开现有完整设置页", async () => {
    const w = await mountChrome();
    await w.find('button[aria-label="设置"]').trigger("click");
    expect(shell.push).toHaveBeenCalledWith({ name: "settings" });
    w.unmount();
  });

  it("离开对话页后不再显示对话页专属的设置入口", async () => {
    const w = await mountChrome();
    expect(w.find('button[aria-label="设置"]').exists()).toBe(true);
    setRouteName("settings");
    await flushPromises();
    expect(w.find('button[aria-label="设置"]').exists()).toBe(false);
    w.unmount();
  });

  it("浏览器预览（没有 Tauri 壳）不画窗口按钮：画了也点不动", async () => {
    setShell(false);
    const w = await mountChrome();
    expect(w.find(".window-controls").exists()).toBe(false);
    expect(w.findAll(".wc-btn")).toHaveLength(0);
    // 设置入口不依赖 Tauri，浏览器里是真实可用的
    expect(w.find('button[aria-label="设置"]').exists()).toBe(true);
    w.unmount();
  });

  it("按钮与字标都不在拖拽区内部：点它们不会被拖拽逻辑吃掉", async () => {
    const w = await mountChrome();
    expect(w.find(".window-controls").element.closest("[data-tauri-drag-region]")).toBeNull();
    expect(w.find('button[aria-label="设置"]').element.closest("[data-tauri-drag-region]")).toBeNull();
    expect(w.find(".brand").element.closest("[data-tauri-drag-region]")).toBeNull();
    w.unmount();
  });
});

describe("窗口控制语义不变", () => {
  it("壳里渲染三个窗口按钮，且带可访问标签", async () => {
    const w = await mountChrome();
    const labels = w.findAll(".window-controls button").map((b) => b.attributes("aria-label"));
    expect(labels).toEqual(["最小化", "最大化", "关闭"]);
    w.unmount();
  });

  it("三个按钮分别调到壳的 minimize / toggleMaximize / close", async () => {
    const w = await mountChrome();

    await w.find('button[aria-label="最小化"]').trigger("click");
    await flushPromises();
    expect(shell.win.minimize).toHaveBeenCalledTimes(1);

    await w.find('button[aria-label="最大化"]').trigger("click");
    await flushPromises();
    expect(shell.win.toggleMaximize).toHaveBeenCalledTimes(1);

    await w.find('button[aria-label="关闭"]').trigger("click");
    await flushPromises();
    expect(shell.win.close).toHaveBeenCalledTimes(1);

    w.unmount();
  });

  it("已经是最大化：按钮说的是「向下还原」", async () => {
    shell.win.isMaximized.mockResolvedValue(true);
    const w = await mountChrome();
    expect(w.find('button[aria-label="向下还原"]').exists()).toBe(true);
    w.unmount();
  });

  it("系统改了最大化状态（拖边缩放 / Win+方向键）：按钮跟着变", async () => {
    const w = await mountChrome();
    expect(w.find('button[aria-label="最大化"]').exists()).toBe(true);

    shell.win.isMaximized.mockResolvedValue(true);
    shell.state.resized?.();
    await flushPromises();

    expect(w.find('button[aria-label="向下还原"]').exists()).toBe(true);
    w.unmount();
  });

  it("卸载时取消订阅，不留悬挂监听", async () => {
    const w = await mountChrome();
    w.unmount();
    expect(shell.unlisten).toHaveBeenCalled();
  });
});

describe("标题条高度只在对话页生效", () => {
  it("对话页把标题条加高，离开后恢复，不波及其他页面", async () => {
    document.documentElement.style.setProperty(TITLEBAR_PROPERTY, "56px");
    const w = await mountChrome();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("80px");

    setRouteName("settings");
    await flushPromises();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("56px");

    setRouteName("conversation");
    await flushPromises();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("80px");

    w.unmount();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("56px");
  });
});
