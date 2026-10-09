/**
 * E 组 · M11 受控验收：`services/updater.ts` 对 `Update`（Tauri `Resource`）的资源释放。
 *
 * 替身按 **SDK 实际行为**建模（`@tauri-apps/plugin-updater/dist-js/index.js`）：
 * * `download()` 会创建一个下载资源（`downloadedBytes`）；
 * * `install()` 成功时由 Rust 侧消费掉下载资源，SDK 把 `downloadedBytes` 置空 ——
 *   所以此后 `close()` 只关 `Update` 自己，**不会**二次关闭下载资源；
 * * `close()` 对已经释放的 rid 再调会抛「resource not found」（真实壳里安装成功后就是这样）。
 *
 * 这里钉死四件事：
 * 1. `check()` 取完信息就释放（成功 / 抛错 / 无更新三种出口都不泄漏）；
 * 2. `downloadAndInstall()` 的每个出口（版本不匹配 / 下载失败 / 停止后端失败 /
 *    安装失败 / 安装成功）都释放**恰好一次**；
 * 3. 释放异常不得掩盖原始失败，也不得让安装成功变成失败；
 * 4. 后端恢复仍然发生，且恢复结论与原因都保留在错误里。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  check: vi.fn(),
  relaunch: vi.fn(),
  getVersion: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("@tauri-apps/plugin-updater", () => ({ check: mocks.check }));
vi.mock("@tauri-apps/plugin-process", () => ({ relaunch: mocks.relaunch }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: mocks.getVersion }));

import { tauriUpdaterApi } from "../updater";

const PREP_OK = {
  alreadyStopped: false,
  pid: 4242,
  stopped: true,
  verified: true,
  detail: "已结束本实例后端进程树",
};
const RESTORE_OK = {
  restored: true,
  started: true,
  port: 12345,
  detail: "已重新拉起后端 pid 777",
};

interface ResourceStub {
  close: () => Promise<void>;
  closed: boolean;
}

/** 与 SDK 行为一致的 Update 替身：资源释放次数逐项可见。 */
class UpdateStub {
  version = "0.1.4";
  body: string | undefined = "notes";
  date: string | undefined = "2026-10-05";
  /** `Update.close()` 被调用的次数（我们的实现必须让它 <= 1） */
  closeCalls = 0;
  /** 下载资源被关闭的次数（SDK 在 install 成功时把它置空 → 这里应保持 0） */
  bytesCloseCalls = 0;
  /** 是否已经在 Rust 侧被消费（模拟安装成功后 close 报 resource not found） */
  consumedByInstall = false;
  downloadError: Error | null = null;
  installError: Error | null = null;
  closeError: Error | null = null;
  downloadCalls = 0;
  installCalls = 0;
  private downloadedBytes: ResourceStub | null = null;

  async download(onEvent?: (event: unknown) => void): Promise<void> {
    this.downloadCalls += 1;
    if (this.downloadError) throw this.downloadError;
    if (this.consumedByInstall) throw new Error("resource not found: download");
    // SDK 行为：download 内部创建下载资源
    this.downloadedBytes = {
      closed: false,
      close: async () => {
        this.bytesCloseCalls += 1;
      },
    };
    onEvent?.({ event: "Started", data: { contentLength: 100 } });
    onEvent?.({ event: "Progress", data: { chunkLength: 100 } });
    onEvent?.({ event: "Finished" });
  }

  async install(): Promise<void> {
    this.installCalls += 1;
    if (this.installError) throw this.installError;
    // SDK 行为：安装成功 = Rust 侧已消费下载资源，`this.downloadedBytes = undefined`
    // （JS 注释：Don't need to call close, we did it in rust side already）
    this.downloadedBytes = null;
    this.consumedByInstall = true;
  }

  async close(): Promise<void> {
    this.closeCalls += 1;
    const bytes = this.downloadedBytes;
    this.downloadedBytes = null;
    if (bytes) await bytes.close();
    if (this.closeError) throw this.closeError;
    if (this.consumedByInstall) throw new Error("resource not found: update rid");
  }
}

function routeCommands(overrides: Record<string, () => unknown> = {}) {
  mocks.invoke.mockImplementation(async (command: string) => {
    if (overrides[command]) return overrides[command]();
    if (command === "qio_refresh_updater_proxy") return null;
    if (command === "qio_prepare_for_update") return PREP_OK;
    if (command === "qio_restore_backend") return RESTORE_OK;
    throw new Error(`未预期的命令：${command}`);
  });
}

/** 一次检查里各命令实际被调用的次数 */
function commandCalls(command: string): number {
  return mocks.invoke.mock.calls.filter((args) => args[0] === command).length;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getVersion.mockResolvedValue("0.1.3");
});

describe("check()：取到信息就释放", () => {
  it("成功检查：返回纯数据，Update 恰好 close 一次，且绝不下载/安装", async () => {
    routeCommands();
    const update = new UpdateStub();
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    const info = await api.check();

    expect(info).toEqual({ version: "0.1.4", notes: "notes", date: "2026-10-05" });
    expect(update.closeCalls).toBe(1);
    expect(update.downloadCalls).toBe(0);
    expect(update.installCalls).toBe(0);
    expect(commandCalls("qio_prepare_for_update")).toBe(0);
  });

  it("没有更新（check 返回 null）：不报错、不释放任何东西", async () => {
    routeCommands();
    mocks.check.mockResolvedValue(null);

    const api = await tauriUpdaterApi();
    await expect(api.check()).resolves.toBeNull();
  });

  it("释放异常不掩盖「检查成功」：仍然返回版本信息", async () => {
    routeCommands();
    const update = new UpdateStub();
    update.closeError = new Error("resource not found: update rid");
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.check()).resolves.toMatchObject({ version: "0.1.4" });
    expect(update.closeCalls).toBe(1);
  });
});

describe("downloadAndInstall()：每个出口释放一次", () => {
  it("版本不匹配：释放一次，不下载、不结束后端、不恢复", async () => {
    routeCommands();
    const update = new UpdateStub();
    update.version = "0.1.5";
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(
      api.downloadAndInstall(() => {}, { expectedVersion: "0.1.4" }),
    ).rejects.toThrow(/版本已变化/);

    expect(update.closeCalls).toBe(1);
    expect(update.downloadCalls).toBe(0);
    expect(commandCalls("qio_prepare_for_update")).toBe(0);
    expect(commandCalls("qio_restore_backend")).toBe(0);
  });

  it("下载（签名校验）失败：释放一次，不结束后端、不恢复后端", async () => {
    routeCommands();
    const update = new UpdateStub();
    update.downloadError = new Error("signature verification failed");
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/signature/);

    expect(update.closeCalls).toBe(1);
    expect(commandCalls("qio_prepare_for_update")).toBe(0);
    expect(commandCalls("qio_restore_backend")).toBe(0);
  });

  it("停止后端失败：释放一次、不安装、恢复后端仍发生，且原因保留", async () => {
    routeCommands({
      qio_prepare_for_update: () => {
        throw new Error("结束后端进程树失败：仍有进程存活 [4242]");
      },
    });
    const update = new UpdateStub();
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/仍有进程存活/);

    expect(update.closeCalls).toBe(1);
    expect(update.installCalls).toBe(0);
    expect(commandCalls("qio_restore_backend")).toBe(1);
  });

  it("安装失败：释放一次、后端恢复发生、错误同时给出恢复状态与原始原因", async () => {
    routeCommands();
    const update = new UpdateStub();
    update.installError = new Error("ShellExecute failed");
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/后端已恢复/);
    // 原始原因不能被恢复文案顶掉
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/ShellExecute failed/);

    expect(update.closeCalls).toBe(2); // 两次独立操作 → 每次各释放一次
    expect(commandCalls("qio_restore_backend")).toBe(2);
  });

  it("安装成功：释放一次；已被 SDK 消费的下载资源不重复关闭", async () => {
    routeCommands();
    const update = new UpdateStub();
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).resolves.toBeUndefined();

    expect(update.installCalls).toBe(1);
    expect(update.closeCalls).toBe(1);
    // install 已经把下载资源交给 Rust 侧消费 → 释放时只关 Update 自己
    expect(update.bytesCloseCalls).toBe(0);
  });

  it("安装成功后 close 报 resource not found（真实壳行为）：不得让成功变成失败", async () => {
    routeCommands();
    const update = new UpdateStub();
    update.closeError = new Error("resource not found: update rid");
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).resolves.toBeUndefined();
    expect(update.closeCalls).toBe(1);
    // 成功后没有恢复后端（后端已被安装器接管）
    expect(commandCalls("qio_restore_backend")).toBe(0);
  });

  it("释放异常不掩盖原始失败：错误里既有安装失败原因也有后端恢复结论", async () => {
    routeCommands({
      qio_restore_backend: () => ({
        restored: false,
        started: false,
        port: 0,
        detail: "重新拉起后端失败：sidecar spawn failed",
      }),
    });
    const update = new UpdateStub();
    update.installError = new Error("ShellExecute failed");
    update.closeError = new Error("resource not found: update rid");
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(
      /请完全退出 QIO 后重新启动.*ShellExecute failed/s,
    );

    expect(update.closeCalls).toBe(1);
  });

  it("更新源没有新版本：直接失败，不释放不存在的 Update", async () => {
    routeCommands();
    mocks.check.mockResolvedValue(null);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/没有可用的新版本/);
    expect(commandCalls("qio_prepare_for_update")).toBe(0);
  });
});
