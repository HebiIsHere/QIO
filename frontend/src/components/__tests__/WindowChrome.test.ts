/**
 * 无边框窗口的自定义标题条 + 关闭行为（R9 契约 §11）。
 *
 * 系统页眉被 `decorations: false` 去掉之后，「能拖窗口」「能最小化/最大化/关闭」
 * 这两件事只剩下前端这一处承担。这些测试守的就是它们不会悄悄失灵：
 * - 浏览器预览里不许画出点了没反应的按钮；
 * - 三个按钮必须真的调到壳的窗口接口，且最大化状态跟着系统走；
 * - 按钮不能长在拖拽区里面（否则点击会被 Tauri 的拖拽逻辑吃掉）；
 * - 关闭按钮必须走关闭行为：ask 弹确认框 / tray 隐藏 / quit 真正退出。
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
  const listeners = new Map<string, () => void>();
  const state: { resized: (() => void) | null } = { resized: null };
  const win = {
    minimize: vi.fn(async () => {}),
    toggleMaximize: vi.fn(async () => {}),
    isMaximized: vi.fn(async () => false),
    onResized: vi.fn(async (cb: () => void) => {
      state.resized = cb;
      return resizeUnlisten;
    }),
  };
  const invoke = vi.fn(async (cmd: string, args?: Record<string, unknown>) => {
    if (cmd === "request_close") {
      const action = String(args?.action ?? "ask");
      return {
        action: action === "ask" ? "ask" : action === "tray" ? "hidden" : "quitting",
        hidden: action === "tray",
        quitting: action === "quit",
        detail: "fake",
      };
    }
    return null;
  });
  const listen = vi.fn(async (event: string, cb: () => void) => {
    listeners.set(event, cb);
    return unlisten;
  });
  const shutdownBackend = vi.fn(async () => ({
    done: true,
    already: false,
    timeout: false,
    grace_seconds: 8,
    approvals_restored: 0,
    report: { clean: true, unfinished: [], phases: [], detail: "fake clean" },
  }));
  const getUISettings = vi.fn(async () => ({ typewriter_cps: 50, close_behavior: "ask" }));
  return {
    win,
    unlisten,
    resizeUnlisten,
    listeners,
    state,
    invoke,
    listen,
    shutdownBackend,
    getUISettings,
  };
});

vi.mock("@tauri-apps/api/window", () => ({ getCurrentWindow: () => mocks.win }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: mocks.listen }));
vi.mock("../../services/api", () => ({
  api: {
    getUISettings: mocks.getUISettings,
    updateUISettings: vi.fn(async () => ({ ok: true, typewriter_cps: 50 })),
    updateCloseBehavior: vi.fn(async (behavior: string) => ({
      ok: true,
      typewriter_cps: 50,
      close_behavior: behavior,
    })),
    shutdownBackend: mocks.shutdownBackend,
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
  // 关闭前会从后端重读一次设置（真机脚本用 API 改设置的场景）→ mock 返回本次行为。
  mocks.getUISettings.mockResolvedValue({ typewriter_cps: 50, close_behavior: behavior });
  const ui = useUiStore();
  ui.loaded = true;
  ui.closeBehavior = behavior;
  const w = mount(WindowChrome, { global: { plugins: [pinia] } });
  await flushPromises();
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  resetQuitGuard(); // 退出守卫是模块级状态：每个用例从「未退出」开始
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

  it("关闭按钮走统一关闭入口，不再直接 close()", async () => {
    const w = await mountChrome("ask");

    await w.find('button[aria-label="关闭"]').trigger("click");
    await flushPromises();

    expect(mocks.invoke).toHaveBeenCalledWith("request_close", { action: "ask" });
    expect("close" in mocks.win).toBe(false); // 不再直接 close()：否则会绕过关闭行为
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

  it("卸载时取消订阅（窗口缩放 + 壳关闭事件），不留悬挂监听", async () => {
    const w = await mountChrome();
    w.unmount();
    expect(mocks.unlisten).toHaveBeenCalled();
    expect(mocks.resizeUnlisten).toHaveBeenCalled();
  });
});

describe("WindowChrome 关闭行为（R9 §11）", () => {
  it("ask：弹出确认框，三个选择与契约一致", async () => {
    const w = await mountChrome("ask");
    await w.find('button[aria-label="关闭"]').trigger("click");
    await flushPromises();

    const dialog = w.find('[role="dialog"]');
    expect(dialog.exists()).toBe(true);
    const labels = dialog.findAll("button").map((b) => b.text());
    expect(labels).toEqual(["最小化到托盘", "退出 QIO", "取消"]);

    // 取消不触发任何退出/隐藏动作
    await dialog.findAll("button")[2].trigger("click");
    await flushPromises();
    expect(w.find('[role="dialog"]').exists()).toBe(false);
    expect(mocks.invoke).toHaveBeenCalledTimes(1); // 只有打开前的那次 ask 门禁
    w.unmount();
  });

  it("ask 选「最小化到托盘」：壳只隐藏窗口，不触发退出", async () => {
    const w = await mountChrome("ask");
    await w.find('button[aria-label="关闭"]').trigger("click");
    await flushPromises();
    await w.find('[role="dialog"] button.primary').trigger("click");
    await flushPromises();

    expect(mocks.invoke).toHaveBeenCalledWith("request_close", { action: "tray" });
    expect(mocks.shutdownBackend).not.toHaveBeenCalled();
    w.unmount();
  });

  it("ask 选「退出 QIO」：先走后端有界收尾，再走壳真正退出", async () => {
    const w = await mountChrome("ask");
    await w.find('button[aria-label="关闭"]').trigger("click");
    await flushPromises();
    await w.find('[role="dialog"] button.danger').trigger("click");
    await flushPromises();

    expect(mocks.shutdownBackend).toHaveBeenCalledTimes(1);
    expect(mocks.invoke).toHaveBeenCalledWith("request_close", { action: "quit" });
    w.unmount();
  });

  it("关闭行为 = tray：点关闭直接隐藏，不弹框", async () => {
    const w = await mountChrome("tray");
    await w.find('button[aria-label="关闭"]').trigger("click");
    await flushPromises();

    expect(w.find('[role="dialog"]').exists()).toBe(false);
    expect(mocks.invoke).toHaveBeenCalledWith("request_close", { action: "tray" });
    w.unmount();
  });

  it("关闭行为 = quit：点关闭直接进入真正退出流程", async () => {
    const w = await mountChrome("quit");
    await w.find('button[aria-label="关闭"]').trigger("click");
    await flushPromises();

    expect(w.find('[role="dialog"]').exists()).toBe(false);
    expect(mocks.shutdownBackend).toHaveBeenCalledTimes(1);
    expect(mocks.invoke).toHaveBeenCalledWith("request_close", { action: "quit" });
    w.unmount();
  });

  it("系统关闭（Alt+F4 / WM_CLOSE）与标题条走同一套行为", async () => {
    const w = await mountChrome("tray");
    const closeRequested = mocks.listeners.get("qio://close-requested");
    expect(closeRequested).toBeTypeOf("function");
    closeRequested?.();
    await flushPromises();

    expect(mocks.invoke).toHaveBeenCalledWith("request_close", { action: "tray" });
    w.unmount();
  });

  it("托盘菜单「退出 QIO」直接走真正退出流程（不再弹 ask 框）", async () => {
    const w = await mountChrome("ask");
    const trayQuit = mocks.listeners.get("qio://tray-quit");
    expect(trayQuit).toBeTypeOf("function");
    trayQuit?.();
    await flushPromises();

    expect(w.find('[role="dialog"]').exists()).toBe(false);
    expect(mocks.shutdownBackend).toHaveBeenCalledTimes(1);
    expect(mocks.invoke).toHaveBeenCalledWith("request_close", { action: "quit" });
    w.unmount();
  });
});
