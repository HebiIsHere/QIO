/**
 * A（F05）：关闭行为「按最新有效选择生效」。
 *
 * 复现的是报告里的时序：先选「直接退出」再选「最小化到托盘」，
 * 较早的请求晚返回时**不能**覆盖运行状态、也不能把旧值同步给桌面壳。
 *
 * 这些用例在基线（f5d0e744）上是红的：setCloseBehavior 无条件写状态 + 同步壳，
 * 旧响应迟到就会把新选择盖回去。修复把序号保护放进 store 的真正写入点。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getUISettings: vi.fn(),
  updateUISettings: vi.fn(),
  updateCloseBehavior: vi.fn(),
  syncBehavior: vi.fn(async (_behavior: string) => {}),
}));

vi.mock("../../services/desktop", () => ({
  syncCloseBehaviorToShell: mocks.syncBehavior,
}));

vi.mock("../../services/api", () => ({
  api: {
    getUISettings: mocks.getUISettings,
    updateUISettings: mocks.updateUISettings,
    updateCloseBehavior: mocks.updateCloseBehavior,
  },
}));

import { useUiStore } from "../ui";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function serverValue(behavior: string) {
  return { ok: true, typewriter_cps: 50, close_behavior: behavior };
}

function syncedValues(): string[] {
  return mocks.syncBehavior.mock.calls.map((call) => String(call[0]));
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.syncBehavior.mockResolvedValue(undefined);
  setActivePinia(createPinia());
});

describe("关闭行为：旧响应不得覆盖较新的有效选择", () => {
  it("连续切换（直接退出 → 最小化到托盘）时，晚返回的旧响应既不写状态也不同步壳", async () => {
    const ui = useUiStore();
    const first = deferred<ReturnType<typeof serverValue>>();
    const second = deferred<ReturnType<typeof serverValue>>();
    mocks.updateCloseBehavior
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    const older = ui.setCloseBehavior("quit");
    const newer = ui.setCloseBehavior("tray");

    second.resolve(serverValue("tray"));
    await newer;
    expect(ui.closeBehavior).toBe("tray");
    expect(ui.closeBehaviorEffective).toBe("tray");

    first.resolve(serverValue("quit"));
    await older;

    // 页面状态保持最新有效选择，且壳只收到最新值（没有迟到的 quit 覆盖）
    expect(ui.closeBehavior).toBe("tray");
    expect(ui.closeBehaviorEffective).toBe("tray");
    expect(syncedValues()).toEqual(["tray"]);
  });

  it("较新的保存失败：界面回到真实生效值，并把失败抛给调用方给出明确反馈", async () => {
    const ui = useUiStore();
    mocks.updateCloseBehavior.mockResolvedValueOnce(serverValue("tray"));
    await ui.setCloseBehavior("tray");
    expect(ui.closeBehavior).toBe("tray");
    mocks.syncBehavior.mockClear();

    mocks.updateCloseBehavior.mockRejectedValueOnce(new Error("后端不可用"));
    await expect(ui.setCloseBehavior("quit")).rejects.toThrow("后端不可用");

    // 不能停在「看起来改了、其实没保存」的值上
    expect(ui.closeBehavior).toBe("tray");
    expect(ui.closeBehaviorEffective).toBe("tray");
    // 失败的值没有被当成生效值同步给壳
    expect(syncedValues()).not.toContain("quit");
  });

  it("较早的保存失败不影响较新的有效结果（旧失败被丢弃，不抛给新选择的调用方）", async () => {
    const ui = useUiStore();
    const olderRequest = deferred<ReturnType<typeof serverValue>>();
    const newerRequest = deferred<ReturnType<typeof serverValue>>();
    mocks.updateCloseBehavior
      .mockImplementationOnce(() => olderRequest.promise)
      .mockImplementationOnce(() => newerRequest.promise);

    const older = ui.setCloseBehavior("quit");
    const newer = ui.setCloseBehavior("tray");
    newerRequest.resolve(serverValue("tray"));
    await newer;

    olderRequest.reject(new Error("旧请求失败"));
    await expect(older).resolves.toBeUndefined();

    expect(ui.closeBehavior).toBe("tray");
    expect(syncedValues()).toEqual(["tray"]);
  });

  it("启动加载的旧响应晚到：不覆盖用户期间做的较新选择（其它设置照常加载）", async () => {
    const ui = useUiStore();
    const loading = deferred<{ typewriter_cps: number; close_behavior: string }>();
    mocks.getUISettings.mockImplementationOnce(() => loading.promise);

    const loadingTask = ui.load();
    mocks.updateCloseBehavior.mockResolvedValueOnce(serverValue("quit"));
    await ui.setCloseBehavior("quit");

    loading.resolve({ typewriter_cps: 25, close_behavior: "tray" });
    await loadingTask;

    expect(ui.closeBehavior).toBe("quit");
    expect(ui.closeBehaviorEffective).toBe("quit");
    expect(ui.typewriterCps).toBe(25);
    expect(syncedValues()).toEqual(["quit"]);
    expect(ui.loaded).toBe(true);
  });

  it("加载成功时把服务端值同时记为「真实生效值」（失败回退的基线）", async () => {
    const ui = useUiStore();
    mocks.getUISettings.mockResolvedValueOnce({ typewriter_cps: 75, close_behavior: "tray" });
    await ui.load();
    expect(ui.closeBehavior).toBe("tray");
    expect(ui.closeBehaviorEffective).toBe("tray");
    expect(syncedValues()).toEqual(["tray"]);
  });
});
