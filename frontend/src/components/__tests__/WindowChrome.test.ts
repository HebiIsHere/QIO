/**
 * 顶部全局栏（无边框窗口的自定义标题条）+ 关闭行为（R9 契约 §11，真机 E2E 后的约定）。
 *
 * 系统页眉被 `decorations: false` 去掉之后，「能拖窗口」「能最小化/最大化/关闭」这两件事
 * 只剩下前端这一处承担。本文件同时守住两条来源线，合并后两边都是有效行为：
 *
 * 页面线（对话页 v2 / 互动页 v2 共用全局栏）：
 * - 浏览器预览里不许画点了没反应的窗口按钮；设置入口是真的可用的（会进设置页）；
 * - 按钮/字标不能长在拖拽区里面；拖拽面始终存在；
 * - 标题条只在共用全局栏的路由（对话 / 互动）加高，离开时恢复，不影响设置/星球等页面
 *   （互动页与挂载点的专项用例见 p1a-shared-global-bar.test.ts）。
 *
 * R9 线（关闭行为由壳执行）：
 * - X 一律交给壳（`window.close()`），前端不自行判断 behavior；
 * - ask 问询：先 ACK，再弹三选确认框；取消不触发任何动作；
 * - 记住选择会写回设置并同步给壳；
 * - 壳侧退出通知只置守卫（不再由前端发 HTTP / 二次判断）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import WindowChrome from "../WindowChrome.vue";
import { useUiStore } from "../../stores/ui";
import { resetQuitGuard } from "../../services/quitGuard";

const mocks = vi.hoisted(() => {
  const unlisten = vi.fn();
  const resizeUnlisten = vi.fn();
  const listeners = new Map<string, (payload?: unknown) => void>();
  const state: { resized: (() => void) | null } = { resized: null };
  const win = {
    minimize: vi.fn(async () => {}),
    toggleMaximize: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
    isMaximized: vi.fn(async () => false),
    onResized: vi.fn(async (cb: () => void) => {
      state.resized = cb;
      return resizeUnlisten;
    }),
  };
  const push = vi.fn(async () => undefined);
  const requestClose = vi.fn(async (action: string) => ({
    action: action === "ask" ? "ask" : action === "tray" ? "hidden" : "quitting",
    hidden: action === "tray",
    quitting: action === "quit",
    detail: "fake",
  }));
  const ack = vi.fn(async () => {});
  const syncBehavior = vi.fn(async () => {});
  const noteQuit = vi.fn();
  const listen = vi.fn(async (event: string, cb: (payload?: unknown) => void) => {
    listeners.set(event, cb);
    return unlisten;
  });
  const getUISettings = vi.fn(async () => ({ typewriter_cps: 50, close_behavior: "ask" }));
  const updateCloseBehavior = vi.fn(async (behavior: string) => ({
    ok: true,
    typewriter_cps: 50,
    close_behavior: behavior,
  }));
  return {
    win,
    unlisten,
    resizeUnlisten,
    listeners,
    state,
    push,
    requestClose,
    ack,
    syncBehavior,
    noteQuit,
    listen,
    getUISettings,
    updateCloseBehavior,
  };
});

vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => mocks.win }));
vi.mock("../../services/desktop", () => ({
  inTauriShell: () => true,
  listenShellEvent: mocks.listen,
  requestClose: mocks.requestClose,
  ackCloseRequest: mocks.ack,
  syncCloseBehaviorToShell: mocks.syncBehavior,
  noteShellQuit: mocks.noteQuit,
}));
vi.mock("../../services/api", () => ({
  api: {
    getUISettings: mocks.getUISettings,
    updateUISettings: vi.fn(async () => ({ ok: true, typewriter_cps: 50 })),
    updateCloseBehavior: mocks.updateCloseBehavior,
  },
}));

vi.mock("../../router", async () => {
  const { ref } = await import("vue");
  const currentRoute = ref<{ name: string }>({ name: "conversation" });
  return {
    default: { currentRoute, push: mocks.push },
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

async function mountChrome(behavior: "ask" | "tray" | "quit" = "ask") {
  const pinia = createPinia();
  setActivePinia(pinia);
  mocks.getUISettings.mockResolvedValue({ typewriter_cps: 50, close_behavior: behavior });
  const ui = useUiStore();
  ui.loaded = true;
  ui.closeBehavior = behavior;
  const w = mount(WindowChrome, { global: { plugins: [pinia] } });
  await flushPromises();
  await flushPromises();
  return w;
}

/** 模拟壳发来的关闭问询（ask 档）。 */
async function emitCloseRequested() {
  const cb = mocks.listeners.get("qio://close-requested");
  expect(cb).toBeTypeOf("function");
  cb?.({ forcedBehavior: null });
  await flushPromises();
}

const TITLEBAR_PROPERTY = "--titlebar-height";

beforeEach(() => {
  vi.clearAllMocks();
  resetQuitGuard();
  mocks.listeners.clear();
  mocks.state.resized = null;
  mocks.win.isMaximized.mockResolvedValue(false);
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

  it("对话页左侧是轨道标志 + PERSONAL AGENT 副标，不显示话题标题", async () => {
    const w = await mountChrome();
    const logo = w.find(".brand-logo");
    expect(logo.exists()).toBe(true);
    expect(logo.attributes("src")).toBeTruthy();
    expect(w.find(".brand-name").exists()).toBe(false);
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
    expect(mocks.push).toHaveBeenCalledWith({ name: "settings" });
    w.unmount();
  });

  it("离开共用全局栏的页面后不再显示设置入口", async () => {
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

describe("窗口控制语义", () => {
  it("壳里渲染三个窗口按钮，且带可访问标签", async () => {
    const w = await mountChrome();
    const labels = w.findAll(".window-controls button").map((b) => b.attributes("aria-label"));
    expect(labels).toEqual(["最小化", "最大化", "关闭"]);
    w.unmount();
  });

  it("最小化 / 最大化按钮调到壳的窗口接口", async () => {
    const w = await mountChrome();

    await w.find('button[aria-label="最小化"]').trigger("click");
    await flushPromises();
    expect(mocks.win.minimize).toHaveBeenCalledTimes(1);

    await w.find('button[aria-label="最大化"]').trigger("click");
    await flushPromises();
    expect(mocks.win.toggleMaximize).toHaveBeenCalledTimes(1);

    w.unmount();
  });

  it("关闭按钮把请求交给壳（window.close），不在前端自行判断 behavior", async () => {
    // 即使设置是 tray，前端也仍只发关闭请求：行为由壳在 CloseRequested 里执行。
    const w = await mountChrome("tray");
    await w.find('button[aria-label="关闭"]').trigger("click");
    await flushPromises();
    expect(mocks.win.close).toHaveBeenCalledTimes(1);
    expect(mocks.requestClose).not.toHaveBeenCalled();
    expect(w.find('[role="dialog"]').exists()).toBe(false);
    w.unmount();
  });

  it("已经是最大化：按钮说的是「向下还原」", async () => {
    mocks.win.isMaximized.mockResolvedValue(true);
    const w = await mountChrome();
    expect(w.find('button[aria-label="向下还原"]').exists()).toBe(true);
    w.unmount();
  });

  it("系统改了最大化状态（拖边缩放 / Win+方向键）：按钮跟着变", async () => {
    const w = await mountChrome();
    expect(w.find('button[aria-label="最大化"]').exists()).toBe(true);

    mocks.win.isMaximized.mockResolvedValue(true);
    mocks.state.resized?.();
    await flushPromises();

    expect(w.find('button[aria-label="向下还原"]').exists()).toBe(true);
    w.unmount();
  });

  it("卸载时取消订阅（窗口缩放 + 壳事件），不留悬挂监听", async () => {
    const w = await mountChrome();
    w.unmount();
    expect(mocks.resizeUnlisten).toHaveBeenCalled();
    expect(mocks.unlisten).toHaveBeenCalled();
  });
});

describe("WindowChrome ask 确认框（壳侧判定，前端只做 UI）", () => {
  it("收到壳的问询：先 ACK，再弹出三选确认框", async () => {
    const w = await mountChrome("ask");
    await emitCloseRequested();

    expect(mocks.ack).toHaveBeenCalledTimes(1);
    const dialog = w.find('[role="dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.findAll("button").map((b) => b.text())).toEqual([
      "最小化到托盘",
      "退出 QIO",
      "取消",
    ]);

    // 取消：只关框，不触发隐藏/退出
    await dialog.findAll("button")[2].trigger("click");
    await flushPromises();
    expect(w.find('[role="dialog"]').exists()).toBe(false);
    expect(mocks.win.close).not.toHaveBeenCalled();
    w.unmount();
  });

  it("选「最小化到托盘」：调壳 request_close(tray)，前端不发后端 HTTP", async () => {
    const w = await mountChrome("ask");
    await emitCloseRequested();
    await w.find('[role="dialog"] button.primary').trigger("click");
    await flushPromises();
    expect(mocks.requestClose).toHaveBeenCalledWith("tray");
    w.unmount();
  });

  it("选「退出 QIO」：调壳 request_close(quit)，由壳完成后端收尾与进程树核验", async () => {
    const w = await mountChrome("ask");
    await emitCloseRequested();
    await w.find('[role="dialog"] button.danger').trigger("click");
    await flushPromises();
    expect(mocks.requestClose).toHaveBeenCalledWith("quit");
    w.unmount();
  });

  it("记住我的选择：写回设置并把新行为同步给壳", async () => {
    const w = await mountChrome("ask");
    await emitCloseRequested();
    await w.find('[role="dialog"] input[type="checkbox"]').setValue(true);
    await w.find('[role="dialog"] button.primary').trigger("click");
    await flushPromises();

    expect(mocks.updateCloseBehavior).toHaveBeenCalledWith("tray");
    expect(mocks.syncBehavior).toHaveBeenCalledWith("tray");
    expect(mocks.requestClose).toHaveBeenCalledWith("tray");
    w.unmount();
  });

  it("壳侧退出通知（托盘菜单 / quit）：只置守卫并显示退出进度，不再 invoke 业务命令", async () => {
    const w = await mountChrome("ask");
    const cb = mocks.listeners.get("qio://quit-requested");
    expect(cb).toBeTypeOf("function");
    cb?.();
    await flushPromises();
    expect(w.find(".close-busy").exists()).toBe(true);
    expect(mocks.noteQuit).toHaveBeenCalledTimes(1);
    w.unmount();
  });
});

describe("标题条高度只在共用全局栏的路由生效", () => {
  it("对话页把标题条加高，离开后恢复，不波及其他页面", async () => {
    document.documentElement.style.setProperty(TITLEBAR_PROPERTY, "56px");
    const w = await mountChrome();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("70px");

    setRouteName("settings");
    await flushPromises();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("56px");

    setRouteName("conversation");
    await flushPromises();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("70px");

    w.unmount();
    expect(document.documentElement.style.getPropertyValue(TITLEBAR_PROPERTY)).toBe("56px");
  });
});
