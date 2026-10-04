/**
 * 独立验证（验证方维护，`*.verify.test.ts`，不覆盖实现方的 `backendConnection.test.ts`）：
 * **连接缓存与无限等待**（契约 WS1 §5，验收清单第 4 条）。
 *
 * 断言只依据冻结契约：
 *   * 有 Tauri 桥但**原生读取失败**时，必须返回可重试错误（「尚未就绪」一类），
 *     **不得**把 `DEFAULT_BASE` 当成功结果缓存；
 *   * 纯浏览器开发模式的既有行为保留（没有 Tauri 桥 → 回落到 env/default）；
 *   * `resetBackend()` 之后：**代次变化前发起的旧异步解析不得再变成当前连接**
 *     （旧请求的失败不得清掉 reset 之后建立的新缓存）。
 *
 * 注入方式（Lead 在真实 vitest 下实测过）：**直接注入 IPC**，不要 mock 模块 ——
 * 真实 `@tauri-apps/api/core` 的 `invoke` 就是
 * `window.__TAURI_INTERNALS__.invoke(cmd, args, options)`（`node_modules/@tauri-apps/api/core.js`）。
 * 之前用 `vi.mock("@tauri-apps/api/core")` + `vi.resetModules()` 在真实 vitest 下**失效**：
 * 测试文件里的 import 拿到 mock，而 `backend.ts` 内部的**动态 import 拿到真模块**
 * （报 `window.__TAURI_INTERNALS__.invoke is not a function`），IPC 调用次数为 0。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import * as backend from "../../services/backend";

const ipcMock = vi.fn();

/** 壳已就绪：IPC 可用（真实模块会转发到这里的 invoke） */
function bridgeUp() {
  (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {
    invoke: ipcMock,
  };
}
/** 纯浏览器开发模式：没有壳 */
function bridgeDown() {
  delete (globalThis as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
}

/** 「必须抛错且错误信息匹配」：不用 `.rejects`，这样在精简 runner 里也能跑 */
async function expectRejects(promise: Promise<unknown>, pattern: RegExp) {
  let error: unknown = null;
  try {
    await promise;
  } catch (e) {
    error = e;
  }
  expect(error).not.toBeNull();
  expect(String((error as Error)?.message ?? error)).toMatch(pattern);
}

beforeEach(() => {
  ipcMock.mockReset();
  bridgeDown();
  backend.resetBackend(); // 每个用例从「没有缓存」开始
});

describe("WS1 §5：原生握手失败不得被当成 default 缓存", () => {
  it("有 Tauri 桥但原生读取失败 → 可重试错误，而不是 default 地址", async () => {
    bridgeUp();
    ipcMock.mockRejectedValueOnce(new Error("shell not ready"));

    await expectRejects(backend.resolveBackend(), /就绪|重试|not ready/);
  });

  it("原生恢复之后要真的重试（原生读取次数 ≥ 2），并返回 Tauri 地址", async () => {
    bridgeUp();
    ipcMock.mockRejectedValueOnce(new Error("shell not ready"));
    await backend.resolveBackend().catch(() => undefined); // 第一次：失败

    ipcMock.mockResolvedValueOnce({ port: 8899, token: "tk_native" });
    const info = await backend.resolveBackend();

    expect(info.base).toBe("http://127.0.0.1:8899");
    expect(info.source).toBe("tauri");
    expect(info.token).toBe("tk_native");
    expect(ipcMock).toHaveBeenCalledTimes(2); // 没有把失败永久缓存
  });

  it("没有 Tauri 桥（纯浏览器开发模式）→ 保留既有回落行为", async () => {
    bridgeDown();
    const info = await backend.resolveBackend();
    expect(info.source).toBe("default");
    expect(info.base).toContain("127.0.0.1");
    expect(ipcMock).toHaveBeenCalledTimes(0);
  });
});

describe("WS1 §5：reset 之后旧异步解析不得写回缓存", () => {
  it("reset 前发起的旧请求失败晚到：不得清掉 reset 之后建立的新缓存", async () => {
    bridgeUp();
    ipcMock.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          setTimeout(() => reject(new Error("old request failed late")), 30);
        }),
    );

    const oldRequest = backend.resolveBackend(); // 旧代次（慢，会失败）
    let oldRejected = false;
    void oldRequest.catch(() => {
      oldRejected = true;
    });
    backend.resetBackend(); // 代次变化

    ipcMock.mockResolvedValueOnce({ port: 2222, token: "tk_new" });
    const fresh = await backend.resolveBackend();
    expect(fresh.base).toBe("http://127.0.0.1:2222");

    await oldRequest.catch(() => undefined); // 旧请求晚到

    // 前提：旧请求必须**真的抛错**（修复前它被吞掉、回落成 default —— 这一步就会红）
    expect(oldRejected).toBe(true);

    const again = await backend.resolveBackend();
    expect(again.base).toBe("http://127.0.0.1:2222"); // 缓存没被旧失败清掉
    expect(ipcMock).toHaveBeenCalledTimes(2); // 也没有因为缓存被清而重新走原生读取
  });

  it("reset 之后旧请求成功晚到：不得把旧地址写回缓存（防回归）", async () => {
    bridgeUp();
    ipcMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          setTimeout(() => resolve({ port: 1111, token: "tk_old" }), 30);
        }),
    );

    const oldRequest = backend.resolveBackend();
    // 信息性记录（不断言具体错误类型）：当前实现把过期的解析判为
    // `BackendConnectionChangedError`（Lead 在真实 vitest 下实测过）。
    let stale: { rejected: boolean; name: string } = { rejected: false, name: "" };
    void oldRequest.then(
      () => undefined,
      (e: Error) => {
        stale = { rejected: true, name: e?.name ?? "" };
      },
    );
    backend.resetBackend();
    ipcMock.mockResolvedValueOnce({ port: 2222, token: "tk_new" });
    const fresh = await backend.resolveBackend();
    expect(fresh.base).toBe("http://127.0.0.1:2222");

    await oldRequest.catch(() => undefined);
    expect(stale.rejected).toBe(true); // 旧解析不得静默变成当前连接

    const again = await backend.resolveBackend();
    expect(again.base).toBe("http://127.0.0.1:2222"); // 旧地址没有写回缓存
    expect(ipcMock).toHaveBeenCalledTimes(2);
  });
});
