/**
 * A（关闭行为保存，第三轮返修）：连续两次保存时，**后一次先失败并完成回读、前一次随后才写入成功**。
 *
 * 触发顺序：R1（较早的选择）还在途 → R2（最新选择）失败先返回 → R2 的回读此刻还看不到 R1
 * 的写入 → R1 随后写入成功。旧实现会把 R2 的回读当成最终结果，于是实际保存值（R1 的值）
 * 与界面/壳不一致，且回退到更早的旧值。
 *
 * 修复口径：还有保存请求在途时**不能**把回读当最终结果；等全部结算后再补读一次服务端
 * 真实值作为最终状态；对较新选择的保护（序号）保留。
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

async function loadAskBaseline(ui: ReturnType<typeof useUiStore>) {
  mocks.getUISettings.mockResolvedValueOnce(serverValue("ask"));
  await ui.load();
  expect(ui.closeBehavior).toBe("ask");
  mocks.syncBehavior.mockClear();
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.syncBehavior.mockResolvedValue(undefined);
  mocks.updateUISettings.mockResolvedValue({ ok: true, typewriter_cps: 50 });
  setActivePinia(createPinia());
});

describe("关闭行为：后一次先失败完成回读、前一次随后写入成功", () => {
  it("在途请求未结算时不做最终回读；全部结算后补读真实值，界面/壳与保存值一致", async () => {
    const ui = useUiStore();
    await loadAskBaseline(ui);
    mocks.getUISettings.mockClear();

    const older = deferred<ReturnType<typeof serverValue>>();
    mocks.updateCloseBehavior
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => Promise.reject(new Error("后端不可用")));

    const quitTask = ui.setCloseBehavior("quit");
    const trayTask = ui.setCloseBehavior("tray");
    await expect(trayTask).rejects.toThrow("后端不可用");

    // R1 还在途：此刻的回读不是最终结果，绝不能被当成结论
    expect(mocks.getUISettings).not.toHaveBeenCalled();
    expect(ui.closeBehavior).toBe("ask");
    expect(ui.closeBehaviorEffective).toBe("ask");

    // 全部结算后补读一次服务端真实值（R1 已经写进去了）
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
    expect(synced).not.toContain("tray");
  });

  it("两次都失败：全部结算后补读，停在真实旧值而不是失败的新值", async () => {
    const ui = useUiStore();
    await loadAskBaseline(ui);
    mocks.getUISettings.mockClear();

    const older = deferred<ReturnType<typeof serverValue>>();
    mocks.updateCloseBehavior
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => Promise.reject(new Error("后端不可用")));

    const quitTask = ui.setCloseBehavior("quit");
    const trayTask = ui.setCloseBehavior("tray");
    await expect(trayTask).rejects.toThrow("后端不可用");
    expect(mocks.getUISettings).not.toHaveBeenCalled();

    mocks.getUISettings.mockResolvedValueOnce(serverValue("ask"));
    older.reject(new Error("旧请求失败"));
    await expect(quitTask).resolves.toBeUndefined();
    await Promise.resolve();
    await Promise.resolve();

    expect(mocks.getUISettings).toHaveBeenCalledTimes(1);
    expect(ui.closeBehavior).toBe("ask");
    expect(ui.closeBehaviorEffective).toBe("ask");
    expect(syncedValues()).not.toContain("tray");
  });

  it("补读期间用户又选了更新的值：以新选择为准，不被旧补读覆盖", async () => {
    const ui = useUiStore();
    await loadAskBaseline(ui);
    mocks.getUISettings.mockClear();

    const older = deferred<ReturnType<typeof serverValue>>();
    mocks.updateCloseBehavior
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => Promise.reject(new Error("后端不可用")))
      .mockImplementationOnce(() => Promise.resolve(serverValue("tray")));

    const quitTask = ui.setCloseBehavior("quit");
    const trayTask = ui.setCloseBehavior("tray");
    await expect(trayTask).rejects.toThrow("后端不可用");

    // 在 R1 结算前，用户又选了 tray 并成功：这是更新的选择
    await ui.setCloseBehavior("tray");
    expect(ui.closeBehavior).toBe("tray");

    // R1 随后成功返回（旧响应）：不能把界面改回 quit
    older.resolve(serverValue("quit"));
    await quitTask;
    await Promise.resolve();
    await Promise.resolve();

    expect(ui.closeBehavior).toBe("tray");
    expect(ui.closeBehaviorEffective).toBe("tray");
    const synced = syncedValues();
    expect(synced[synced.length - 1]).toBe("tray");
    expect(synced[synced.length - 1]).not.toBe("quit");
  });
});