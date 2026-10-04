/**
 * §5 连接缓存：**只有有效连接信息才配进缓存**。
 *
 * 受控验证的既有缺口：
 * - 桌面壳（有 Tauri 桥）读取连接信息失败时，旧实现返回开发模式默认地址并把它当成功结果缓存
 *   —— 之后壳恢复了也还是那个不存在的端口（原生读取次数停在 1）；
 * - `resetBackend()` 只置空、没有代次：reset 前发出的旧解析晚到会重新写回缓存；
 * - 原生读取挂住时没有上界，启动握手会一直等下去。
 *
 * 三类结论里的「受控验证」都在这里：注入桥、注入失败、控制放行顺序。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

/**
 * 注入方式：**不 mock 模块，直接把 IPC 放进桥里**。
 *
 * 为什么不能用 `vi.mock("@tauri-apps/api/core")`：本文件要用 `vi.resetModules()`
 * 拿到全新的 `backend.ts`（缓存与代次是模块级状态），而实测（官方 vitest）在
 * `resetModules()` 之后模块内部的 `await import("@tauri-apps/api/core")` 会拿到**真模块**，
 * IPC mock 一次都不会被调用 —— 那几条「期望失败」的用例会碰巧绿，只有「期望成功」的会红。
 *
 * 真实 `@tauri-apps/api/core` 的 `invoke` 就是转发到 `window.__TAURI_INTERNALS__.invoke`，
 * 所以把 `invoke` 放进桥里，走的仍是**真实的 Tauri 客户端代码路径**，而且不受 resetModules 影响。
 */
const ipcMock = vi.fn();

type Globals = Record<string, unknown>;

function internalsTargets(): Globals[] {
  const g = globalThis as unknown as Globals;
  const targets = [g];
  const win = (g as { window?: Globals }).window;
  // vitest 的 jsdom 环境里 window 可能是独立对象：两边都放，真实 invoke 读的是 window 上那份
  if (win && (win as unknown) !== (g as unknown)) targets.push(win);
  return targets;
}

function bridgeUp() {
  const internals = { invoke: ipcMock };
  for (const target of internalsTargets()) target.__TAURI_INTERNALS__ = internals;
}

function bridgeDown() {
  for (const target of internalsTargets()) delete target.__TAURI_INTERNALS__;
}

/** 每个用例都用全新的模块实例：缓存与代次都是模块级状态。 */
async function freshBackend() {
  vi.resetModules();
  return await import("../backend");
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  ipcMock.mockReset();
  bridgeDown();
  vi.unstubAllEnvs();
});

afterEach(() => {
  bridgeDown();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

describe("有 Tauri 桥：读不到就是「尚未就绪」，不是开发模式默认地址", () => {
  it("原生读取失败 → 明确的可重试错误，绝不当成成功结果", async () => {
    ipcMock.mockRejectedValue(new Error("shell not ready"));
    bridgeUp();
    const backend = await freshBackend();

    await expect(backend.resolveBackend()).rejects.toThrow(/尚未就绪|重试/);
    expect(ipcMock).toHaveBeenCalledTimes(1);
  });

  it("失败不缓存：第二次调用会重新读原生，壳恢复后就能拿到真实连接", async () => {
    ipcMock
      .mockRejectedValueOnce(new Error("shell not ready"))
      .mockResolvedValueOnce({ port: 4321, token: "tk_real" });
    bridgeUp();
    const backend = await freshBackend();

    await expect(backend.resolveBackend()).rejects.toThrow(/尚未就绪|重试/);
    const info = await backend.resolveBackend();

    expect(info).toEqual({ base: "http://127.0.0.1:4321", token: "tk_real", source: "tauri" });
    expect(ipcMock).toHaveBeenCalledTimes(2);
  });

  it("原生读取挂住 → 有界等待（超时按「尚未就绪」处理），不会无限等", async () => {
    vi.useFakeTimers();
    ipcMock.mockImplementation(() => new Promise(() => {}));
    bridgeUp();
    const backend = await freshBackend();

    const pending = backend.resolveBackend();
    // 先等这次解析真的把超时计时器排上（原生调用是异步发起的），再推进虚拟时间
    for (let i = 0; i < 200 && vi.getTimerCount() === 0; i += 1) await Promise.resolve();
    expect(vi.getTimerCount()).toBe(1);

    // **先挂上拒绝处理者，再推进虚拟时间**：advanceTimersByTimeAsync 会清空微任务队列，
    // 那一刻若还没人处理这次拒绝，vitest 会判为 unhandled rejection（全量退出码变非零）。
    const rejected = expect(pending).rejects.toThrow(/尚未就绪|重试/);
    await vi.advanceTimersByTimeAsync(backend.NATIVE_READ_TIMEOUT_MS + 50);
    await rejected;
  });

  it("壳发布了空端口 → 也算尚未就绪，不退化成默认地址", async () => {
    ipcMock.mockResolvedValue({ port: 0, token: "" });
    bridgeUp();
    const backend = await freshBackend();

    await expect(backend.resolveBackend()).rejects.toThrow(/尚未就绪|重试/);
  });
});

describe("纯浏览器开发模式：既有行为保留", () => {
  it("没有桥 + 有 VITE_* 配置 → 用 env 里的地址与令牌", async () => {
    bridgeDown();
    vi.stubEnv("VITE_QIO_BACKEND_URL", "http://127.0.0.1:9999");
    vi.stubEnv("VITE_QIO_SESSION_TOKEN", "dev_tk");
    const backend = await freshBackend();

    await expect(backend.resolveBackend()).resolves.toEqual({
      base: "http://127.0.0.1:9999",
      token: "dev_tk",
      source: "env",
    });
    expect(ipcMock).not.toHaveBeenCalled();
  });

  it("没有桥 + 什么都没配置 → 回落到默认端口（开发后端专用）", async () => {
    bridgeDown();
    vi.stubEnv("VITE_QIO_BACKEND_URL", "");
    vi.stubEnv("VITE_QIO_SESSION_TOKEN", "");
    const backend = await freshBackend();

    await expect(backend.resolveBackend()).resolves.toEqual({
      base: "http://127.0.0.1:8734",
      token: "",
      source: "default",
    });
  });
});

describe("reset 的代次：旧解析晚到不得写回缓存", () => {
  it("reset 后旧解析才成功：缓存保持新连接，下次调用不再重读", async () => {
    const slowOld = deferred<{ port: number; token: string }>();
    ipcMock.mockImplementationOnce(() => slowOld.promise);
    bridgeUp();
    const backend = await freshBackend();

    const stale = backend.resolveBackend(); // 旧解析在飞
    backend.resetBackend(); // 连接信息变化（后端重启 / 认证失效）

    ipcMock.mockResolvedValueOnce({ port: 2222, token: "tk_new" });
    const fresh = backend.resolveBackend();
    await expect(fresh).resolves.toMatchObject({ base: "http://127.0.0.1:2222", source: "tauri" });

    // 旧解析这时才成功：它自己的调用者可以拿到旧值，但**缓存里必须还是新的**
    slowOld.resolve({ port: 1111, token: "tk_old" });
    await stale.catch(() => undefined);

    await expect(backend.resolveBackend()).resolves.toMatchObject({
      base: "http://127.0.0.1:2222",
    });
    expect(ipcMock).toHaveBeenCalledTimes(2);
  });

  it("reset 之后：旧解析的结果不会被当成当前连接（调用方拿到的是「请重新解析」）", async () => {
    const slowOld = deferred<{ port: number; token: string }>();
    ipcMock.mockImplementationOnce(() => slowOld.promise);
    bridgeUp();
    const backend = await freshBackend();

    const stale = backend.resolveBackend();
    backend.resetBackend();

    slowOld.resolve({ port: 1111, token: "tk_old" });
    await expect(stale).rejects.toThrow(/重新|更新|过期/);
  });
});
