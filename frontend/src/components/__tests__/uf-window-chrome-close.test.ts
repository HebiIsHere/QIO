/**
 * D · 关闭确认 / 关闭失败 / 更新小标记 / 全局异常入口（统一改造）。
 *
 * 锁定的行为：
 * - 关闭询问登记 modalArbiter(kind=close-ask) 并 suspendAll("close-ask")，取消后 resumeAll；
 * - 三选项文字保留、只说明「退出会结束当前运行」，不展示任务概况；
 * - 托盘/退出失败显示在**仍可见**的标题条下方状态条里，可重试、可取消，busy 不锁死；
 * - 后台发现更新只在设置入口留小标记（不弹通知）；
 * - 全局异常入口在全局栏里（B 的组件，无问题时不占位）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import WindowChrome from "../WindowChrome.vue";
import { useUiStore } from "../../stores/ui";
import { useUpdaterStore } from "../../stores/updater";
import { useModalArbiterStore } from "../../stores/modalArbiter";
import { useGlobalIssuesStore } from "../../stores/globalIssues";
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
    action,
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
  return {
    win, unlisten, resizeUnlisten, listeners, state, push, requestClose, ack, syncBehavior, noteQuit, listen,
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
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50, close_behavior: "ask" })),
    updateUISettings: vi.fn(async () => ({ ok: true, typewriter_cps: 50 })),
    updateCloseBehavior: vi.fn(async (behavior: string) => ({
      ok: true,
      typewriter_cps: 50,
      close_behavior: behavior,
    })),
  },
}));
vi.mock("../../router", async () => {
  const { ref } = await import("vue");
  const currentRoute = ref<{ name: string }>({ name: "conversation" });
  return { default: { currentRoute, push: mocks.push } };
});

function setShell(on: boolean) {
  const g = globalThis as unknown as Record<string, unknown>;
  if (on) g.__TAURI_INTERNALS__ = {};
  else delete g.__TAURI_INTERNALS__;
}

async function mountChrome() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const ui = useUiStore();
  ui.loaded = true;
  ui.closeBehavior = "ask";
  const w = mount(WindowChrome, { global: { plugins: [pinia] } });
  await flushPromises();
  await flushPromises();
  return w;
}

async function emitCloseRequested() {
  const cb = mocks.listeners.get("qio://close-requested");
  expect(cb).toBeTypeOf("function");
  cb?.();
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  resetQuitGuard();
  mocks.listeners.clear();
  mocks.state.resized = null;
  setShell(true);
});
afterEach(() => setShell(false));

describe("关闭询问与弹窗仲裁", () => {
  it("打开时登记 close-ask 并 suspendAll，取消后 resumeAll（不丢业务弹窗）", async () => {
    const w = await mountChrome();
    const arbiter = useModalArbiterStore();
    await emitCloseRequested();
    expect(arbiter.isOpen("qio-close-ask")).toBe(true);
    expect(arbiter.suspendedBy).toBe("close-ask");
    const dialog = w.find('[role="dialog"]');
    expect(dialog.text()).toContain("最小化到托盘");
    expect(dialog.text()).toContain("退出 QIO");
    expect(dialog.text()).toContain("取消");
    // 只说明「退出会结束当前运行」，不展示任务概况/名称/数量
    expect(dialog.text()).toContain("结束当前运行");
    // 取消 = 关框 + 恢复业务弹窗
    await dialog.findAll("button").find((b) => b.text() === "取消")!.trigger("click");
    await flushPromises();
    expect(w.find('[role="dialog"]').exists()).toBe(false);
    expect(arbiter.isOpen("qio-close-ask")).toBe(false);
    expect(arbiter.suspendedBy).toBe(null);
    w.unmount();
  });

  it("记住选择使用自定义外观 checkbox（仍是真实 input，可键盘操作）", async () => {
    const w = await mountChrome();
    await emitCloseRequested();
    const check = w.find('[role="dialog"] input[type="checkbox"]');
    expect(check.exists()).toBe(true);
    expect(check.classes()).toContain("close-ask__check");
    w.unmount();
  });
});

describe("关闭失败必须可见、可重试、不锁死 busy", () => {
  it("最小化到托盘失败：状态条显示原因与重试，重试会真的再发一次壳请求", async () => {
    mocks.requestClose.mockRejectedValueOnce(new Error("shell unavailable"));
    const w = await mountChrome();
    await emitCloseRequested();
    await w.find('[role="dialog"]').findAll("button").find((b) => b.text() === "最小化到托盘")!.trigger("click");
    await flushPromises();
    const failure = w.find(".chrome-failure");
    expect(failure.exists()).toBe(true);
    expect(failure.text()).toContain("无法隐藏到系统托盘");
    expect(failure.find(".chrome-failure__retry").text()).toContain("重试");
    await failure.find(".chrome-failure__retry").trigger("click");
    await flushPromises();
    expect(mocks.requestClose).toHaveBeenCalledTimes(2);
    expect(w.find(".chrome-failure").exists()).toBe(false);
    w.unmount();
  });

  it("退出请求返回但应用仍在运行：结束「正在退出」等待态并给重试/取消，不永远 busy", async () => {
    const w = await mountChrome();
    await emitCloseRequested();
    await w.find('[role="dialog"]').findAll("button").find((b) => b.text() === "退出 QIO")!.trigger("click");
    await flushPromises();
    expect(mocks.requestClose).toHaveBeenCalledWith("quit");
    // 壳没有退出（测试替身 resolve）：必须结束 busy 并给出可见状态
    expect(w.find(".close-busy").exists()).toBe(false);
    const failure = w.find(".chrome-failure");
    expect(failure.exists()).toBe(true);
    expect(failure.text()).toContain("退出还没有完成");
    expect(failure.find(".chrome-failure__cancel").exists()).toBe(true);
    await failure.find(".chrome-failure__cancel").trigger("click");
    await flushPromises();
    expect(w.find(".chrome-failure").exists()).toBe(false);
    w.unmount();
  });
});

describe("更新小标记与全局异常入口", () => {
  it("后台发现更新：设置入口出现小标记与可访问名称，不弹通知", async () => {
    const w = await mountChrome();
    const updater = useUpdaterStore();
    updater.phase = "available";
    updater.availableVersion = "9.9.9";
    await flushPromises();
    expect(w.find(".chrome-update-dot").exists()).toBe(true);
    expect(w.find('button[aria-label="设置（有可用更新）"]').exists()).toBe(true);
    w.unmount();
  });

  it("全局异常入口在全局栏里；没有问题时不占位", async () => {
    const w = await mountChrome();
    expect(w.find("[data-uf-issue-entry]").exists()).toBe(false);
    const issues = useGlobalIssuesStore();
    issues.upsert({
      id: "sse_disconnected",
      kind: "sse_disconnected",
      title: "事件流已断开",
      reason: "连接被拒绝",
      actions: [],
      diagnostics: [],
      severity: "error",
    });
    await flushPromises();
    expect(w.find("[data-uf-issue-entry]").exists()).toBe(true);
    w.unmount();
  });
});
