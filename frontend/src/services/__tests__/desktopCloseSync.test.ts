/**
 * A（F05）：同步桌面壳的路径也要防「旧同步过程覆盖较新的有效结果」。
 *
 * 场景：保存成功后会 invoke("qio_set_close_behavior")；两次选择紧接着发生时，
 * 两个 invoke 可能并发，先发的晚到，壳最终缓存了旧值。
 * 这里要求同步串行执行、并且以**最后一次请求的值**收尾。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));

import { syncCloseBehaviorToShell } from "../desktop";

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

/** 让动态 import / promise 链跑到下一次 invoke 真正发起（一个宏任务足矣）。 */
async function flushAsync(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function behaviorCalls(): string[] {
  return mocks.invoke.mock.calls
    .filter((call) => call[0] === "qio_set_close_behavior")
    .map((call) => String((call[1] as { behavior?: string } | undefined)?.behavior ?? ""));
}

/** 每次 invoke 拿一个可手动结算的 promise：调用顺序与结算顺序完全受控。 */
function queueInvocations(): Deferred<unknown>[] {
  const pending: Deferred<unknown>[] = [];
  mocks.invoke.mockImplementation(() => {
    const next = deferred<unknown>();
    pending.push(next);
    return next.promise;
  });
  return pending;
}

beforeEach(() => {
  vi.clearAllMocks();
  (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
});

afterEach(() => {
  delete (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

describe("同步关闭行为到壳：串行且以最新值收尾", () => {
  it("两次同步重叠时，第二次 invoke 必须等第一次结束（不并发）", async () => {
    const pending = queueInvocations();

    const one = syncCloseBehaviorToShell("quit");
    const two = syncCloseBehaviorToShell("tray");
    await flushAsync();

    // 第一次还没结束：壳不能同时收到第二个关闭行为（否则先后顺序由网络决定）
    expect(behaviorCalls()).toEqual(["quit"]);

    pending[0].resolve(undefined);
    await one;
    await flushAsync();
    expect(behaviorCalls()).toEqual(["quit", "tray"]);

    pending[1].resolve(undefined);
    await two;
    expect(behaviorCalls()[behaviorCalls().length - 1]).toBe("tray");
  });

  it("等待期间又选了新值：壳最终收到的是最新值（旧值不会最后落地）", async () => {
    const pending = queueInvocations();

    const one = syncCloseBehaviorToShell("quit");
    const two = syncCloseBehaviorToShell("tray");
    // tray 同步还没开始，用户又改回 quit：最终仍要以 quit 收尾
    const three = syncCloseBehaviorToShell("quit");
    await flushAsync();
    expect(behaviorCalls()).toEqual(["quit"]);

    pending[0].resolve(undefined);
    await one;
    await flushAsync();
    expect(behaviorCalls()).toEqual(["quit", "tray"]);

    pending[1].resolve(undefined);
    await two;
    await flushAsync();
    expect(behaviorCalls()[behaviorCalls().length - 1]).toBe("quit");

    pending[2].resolve(undefined);
    await three;
    expect(behaviorCalls()).toEqual(["quit", "tray", "quit"]);
  });

  it("一次同步失败不影响后续同步（失败只记日志，不阻塞链）", async () => {
    const pending = queueInvocations();
    const one = syncCloseBehaviorToShell("quit");
    await flushAsync();
    pending[0].reject(new Error("IPC 失败"));
    await expect(one).resolves.toBeUndefined();

    const two = syncCloseBehaviorToShell("tray");
    await flushAsync();
    expect(behaviorCalls()[behaviorCalls().length - 1]).toBe("tray");
    pending[1].resolve(undefined);
    await two;
  });

  it("浏览器预览（无壳）不发 IPC", async () => {
    delete (globalThis as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    await syncCloseBehaviorToShell("tray");
    expect(mocks.invoke).not.toHaveBeenCalled();
  });
});