/**
 * 无边框窗口的自定义标题条。
 *
 * 系统页眉被 `decorations: false` 去掉之后，「能拖窗口」「能最小化/最大化/关闭」
 * 这两件事只剩下前端这一处承担。这些测试守的就是它们不会悄悄失灵：
 * - 浏览器预览里不许画出点了没反应的按钮；
 * - 三个按钮必须真的调到壳的窗口接口，且最大化状态跟着系统走；
 * - 按钮不能长在拖拽区里面（否则点击会被 Tauri 的拖拽逻辑吃掉）。
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
  return { win, unlisten, state };
});

vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => shell.win }));

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

beforeEach(() => {
  vi.clearAllMocks();
  shell.state.resized = null;
  shell.win.isMaximized.mockResolvedValue(false);
  setShell(true);
});

afterEach(() => {
  setShell(false);
});

describe("WindowChrome 自定义标题条", () => {
  it("拖拽面始终存在（窗口没有系统标题栏，只能靠它移动）", async () => {
    const w = await mountChrome();
    expect(w.find("[data-tauri-drag-region]").exists()).toBe(true);
    w.unmount();
  });

  it("浏览器预览（没有 Tauri 壳）不画按钮：画了也点不动", async () => {
    setShell(false);
    const w = await mountChrome();
    expect(w.find(".window-controls").exists()).toBe(false);
    expect(w.findAll("button")).toHaveLength(0);
    w.unmount();
  });

  it("壳里渲染三个窗口按钮，且带可访问标签", async () => {
    const w = await mountChrome();
    const labels = w.findAll("button").map((b) => b.attributes("aria-label"));
    expect(labels).toEqual(["最小化", "最大化", "关闭"]);
    w.unmount();
  });

  it("按钮不在拖拽区内部：点按钮不会被拖拽逻辑吃掉", async () => {
    const w = await mountChrome();
    const controls = w.find(".window-controls").element;
    expect(controls.closest("[data-tauri-drag-region]")).toBeNull();
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
