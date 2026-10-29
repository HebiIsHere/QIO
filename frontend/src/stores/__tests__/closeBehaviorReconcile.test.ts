/**
 * A（F05 返修）：连续保存「前一次成功、后一次失败」时，实际保存值、界面与壳必须一致，
 * 不能回退到更早的旧值。
 *
 * 触发条件：两次保存请求的返回顺序不由前端控制。最新一次失败时若只回退到本地记住的值，
 * 就可能退回更早的旧值（前一次其实已经写进去了），壳和界面还停在不一致的状态。
 * 修复：每次成功都采纳服务端持久值作为基线；还有请求在途时不做「最终回读」，
 * 等全部结算后再补读一次真实值（见 closeBehaviorFinalSettle.test.ts）。
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
  return { typewriter_cps: 50, close_behavior: behavior };
}

function syncedValues(): string[] {
  return mocks.syncBehavior.mock.calls.map((call) => String(call[0]));
}

/** 清空上一个用例可能残留在 mock 队列里的「一次性返回值」，避免跨用例串味。 */
beforeEach(() => {
  vi.resetAllMocks();
  mocks.syncBehavior.mockResolvedValue(undefined);
  mocks.updateUISettings.mockResolvedValue({ ok: true, typewriter_cps: 50 });
  setActivePinia(createPinia());
});

describe("关闭行为返修：前一次成功、后一次失败", () => {
  it("前一次成功保存 quit、后一次 tray 失败：界面/壳都停在 quit，不回退到更早的 ask", async () => {
    const ui = useUiStore();
    mocks.getUISettings.mockResolvedValueOnce(serverValue("ask"));
    await ui.load();
    mocks.syncBehavior.mockClear();

    mocks.updateCloseBehavior.mockResolvedValueOnce(serverValue("quit"));
    await ui.setCloseBehavior("quit");
    expect(ui.closeBehavior).toBe("quit");
    expect(ui.closeBehaviorEffective).toBe("quit");

    // 后一次单条在途失败：只有本次请求，回读就是最终结果
    mocks.updateCloseBehavior.mockRejectedValueOnce(new Error("后端不可用"));
    mocks.getUISettings.mockResolvedValueOnce(serverValue("quit"));
    await expect(ui.setCloseBehavior("tray")).rejects.toThrow("后端不可用");

    expect(ui.closeBehavior).toBe("quit");
    expect(ui.closeBehaviorEffective).toBe("quit");
    const synced = syncedValues();
    expect(synced[synced.length - 1]).toBe("quit");
    expect(synced.slice(synced.lastIndexOf("quit"))).not.toContain("ask");
  });

  it("后一次先失败、前一次的成功响应迟到：等全部结算后补读真实值，不回退更早旧值", async () => {
    const ui = useUiStore();
    mocks.getUISettings.mockResolvedValueOnce(serverValue("ask"));
    await ui.load();
    mocks.syncBehavior.mockClear();
    mocks.getUISettings.mockClear();

    const older = deferred<ReturnType<typeof serverValue>>();
    const newer = deferred<ReturnType<typeof serverValue>>();
    mocks.updateCloseBehavior
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => newer.promise);

    const quitTask = ui.setCloseBehavior("quit");
    const trayTask = ui.setCloseBehavior("tray");

    // 最新一次（tray）先失败，但前一次还在途：此刻回读不是最终结果
    newer.reject(new Error("网络超时"));
    await expect(trayTask).rejects.toThrow("网络超时");
    expect(mocks.getUISettings).not.toHaveBeenCalled();
    expect(ui.closeBehavior).toBe("ask");

    // 前一次的迟到成功随后到达（它确实写进去了）：全部结算后补读，收敛到 quit
    mocks.getUISettings.mockResolvedValueOnce(serverValue("quit"));
    older.resolve(serverValue("quit"));
    await quitTask;
    await Promise.resolve();
    await Promise.resolve();

    expect(mocks.getUISettings).toHaveBeenCalledTimes(1);
    expect(ui.closeBehavior).toBe("quit");
    expect(ui.closeBehaviorEffective).toBe("quit");
    const synced = syncedValues();
    expect(synced[synced.length - 1]).toBe("quit");
    expect(synced).not.toContain("ask");
  });

  it("最新失败且服务端校准也失败：退回最近一次已确认值（不是更早的值）并保留错误", async () => {
    const ui = useUiStore();
    mocks.getUISettings.mockResolvedValueOnce(serverValue("ask"));
    await ui.load();
    mocks.syncBehavior.mockClear();

    mocks.updateCloseBehavior.mockResolvedValueOnce(serverValue("quit"));
    await ui.setCloseBehavior("quit");

    mocks.updateCloseBehavior.mockRejectedValueOnce(new Error("后端不可用"));
    mocks.getUISettings.mockRejectedValueOnce(new Error("读取也失败"));
    await expect(ui.setCloseBehavior("tray")).rejects.toThrow("后端不可用");

    expect(ui.closeBehavior).toBe("quit");
    expect(ui.closeBehaviorEffective).toBe("quit");
    const synced = syncedValues();
    expect(synced[synced.length - 1]).toBe("quit");
  });

  it("迟到的旧成功在最新失败之后到达：全部结算后收敛到真实持久值，不停在旧的 ask", async () => {
    const ui = useUiStore();
    mocks.getUISettings.mockResolvedValueOnce(serverValue("ask"));
    await ui.load();
    mocks.syncBehavior.mockClear();
    mocks.getUISettings.mockClear();

    const older = deferred<ReturnType<typeof serverValue>>();
    mocks.updateCloseBehavior
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => Promise.reject(new Error("后端不可用")));

    const quitTask = ui.setCloseBehavior("quit");
    await expect(ui.setCloseBehavior("tray")).rejects.toThrow("后端不可用");
    // 前一次还在途：先停在已知值，不做最终回读
    expect(ui.closeBehavior).toBe("ask");
    expect(mocks.getUISettings).not.toHaveBeenCalled();

    // 前一次此时才成功返回：它已经写进去了，全部结算后补读真实值 → quit
    mocks.getUISettings.mockResolvedValueOnce(serverValue("quit"));
    older.resolve(serverValue("quit"));
    await quitTask;
    await Promise.resolve();
    await Promise.resolve();
    expect(ui.closeBehavior).toBe("quit");
    expect(ui.closeBehaviorEffective).toBe("quit");
    const synced = syncedValues();
    expect(synced[synced.length - 1]).toBe("quit");
  });
});