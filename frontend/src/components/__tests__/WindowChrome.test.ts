/**
 * 无边框窗口的自定义标题条 + 关闭行为（R9 契约 §11，真机 E2E 后的约定）。
 *
 * 关闭行为的执行主体是**壳**（Rust）：前端点 X 只发 window.close()，壳按 ui.close_behavior
 * 执行 tray/quit；只有 ask 会发事件回来让前端弹确认框。这些测试守的是：
 * - 浏览器预览里不许画出点了没反应的按钮；
 * - 最小化/最大化/关闭调到壳的窗口接口，最大化状态跟着系统走；
 * - 按钮不能长在拖拽区里面；
 * - X 一律交给壳（不在前端自行判断 behavior）；
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

beforeEach(() => {
  vi.clearAllMocks();
  resetQuitGuard();
  mocks.listeners.clear();
  mocks.state.resized = null;
  mocks.win.isMaximized.mockResolvedValue(false);
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
    const labels = w.findAll(".window-controls button").map((b) => b.attributes("aria-label"));
    expect(labels).toEqual(["最小化", "最大化", "关闭"]);
    w.unmount();
  });

  it("按钮不在拖拽区内部：点按钮不会被拖拽逻辑吃掉", async () => {
    const w = await mountChrome();
    const controls = w.find(".window-controls").element;
    expect(controls.closest("[data-tauri-drag-region]")).toBeNull();
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
    const w = await mountChrome("tray"); // 即使设置是 tray，前端也仍只发关闭请求
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
    expect(mocks.unlisten).toHaveBeenCalled();
    expect(mocks.resizeUnlisten).toHaveBeenCalled();
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
