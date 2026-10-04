/**
 * 更新编排的受控验证（修复提示词 §2）—— 用假的 Tauri 插件/命令驱动真实实现：
 *
 * * **顺序**：check → download（含签名校验）→ 才 `qio_prepare_for_update` → install；
 *   绝不在重新检查版本或开始下载之前结束后端；
 * * **真实结果**：结束后端失败（超时/权限/退出验证失败）不吞异常、不继续安装；
 * * **失败可用**：结束后端之后安装失败 → 必须调 `qio_restore_backend`，错误里说明后端状态；
 * * **代理按操作**：探测结果作为 `check({ proxy })` 传给插件（检查/下载共用同一上下文），
 *   不改进程级环境变量。
 *
 * 插件与命令都是 mock：这里验证的是**编排**，网络与安装器行为在 Rust 侧。
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

interface UpdateStub {
  version: string;
  body?: string;
  date?: string;
  download: (onEvent?: (event: unknown) => void) => Promise<void>;
  install: () => Promise<void>;
  /** 旧实现用的是插件的 downloadAndInstall；留着它，修复前的红才是「行为不同」而不是「方法不存在」 */
  downloadAndInstall: (onEvent?: (event: unknown) => void) => Promise<void>;
}

function updateStub(overrides: Partial<UpdateStub> = {}): UpdateStub {
  const stub: UpdateStub = {
    version: "0.1.4",
    body: "notes",
    date: "2026-10-05",
    download: vi.fn(async (onEvent?: (event: unknown) => void) => {
      onEvent?.({ event: "Started", data: { contentLength: 100 } });
      onEvent?.({ event: "Progress", data: { chunkLength: 100 } });
      onEvent?.({ event: "Finished" });
    }),
    install: vi.fn(async () => {}),
    downloadAndInstall: vi.fn(async (onEvent?: (event: unknown) => void) => {
      await stub.download(onEvent);
      await stub.install();
    }),
    ...overrides,
  };
  return stub;
}

/** 记录命令调用顺序，并按命令返回预设结果。 */
function routeCommands(order: string[], overrides: Record<string, () => unknown> = {}) {
  mocks.invoke.mockImplementation(async (command: string) => {
    order.push(command);
    if (overrides[command]) return overrides[command]();
    if (command === "qio_refresh_updater_proxy") return null;
    if (command === "qio_prepare_for_update") return PREP_OK;
    if (command === "qio_restore_backend") return RESTORE_OK;
    throw new Error(`未预期的命令：${command}`);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getVersion.mockResolvedValue("0.1.3");
});

describe("下载与校验完成之后才结束后端", () => {
  it("顺序必须是 check → download → 结束后端 → install", async () => {
    const order: string[] = [];
    routeCommands(order);
    const update = updateStub({
      download: vi.fn(async () => {
        order.push("download");
      }),
      install: vi.fn(async () => {
        order.push("install");
      }),
    });
    mocks.check.mockImplementation(async () => {
      order.push("check");
      return update;
    });

    const api = await tauriUpdaterApi();
    await api.downloadAndInstall(() => {});

    expect(order).toEqual([
      "qio_refresh_updater_proxy",
      "check",
      "download",
      "qio_prepare_for_update",
      "install",
    ]);
  });

  it("下载（校验）失败时不结束后端、不安装", async () => {
    const order: string[] = [];
    routeCommands(order);
    const update = updateStub({
      download: vi.fn(async () => {
        throw new Error("signature verification failed");
      }),
    });
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/signature/);

    expect(order).not.toContain("qio_prepare_for_update");
    expect(order).not.toContain("qio_restore_backend");
    expect(update.install).not.toHaveBeenCalled();
  });

  it("更新源版本与界面显示不一致时放弃安装（不下载、不结束后端）", async () => {
    const order: string[] = [];
    routeCommands(order);
    const update = updateStub({ version: "0.1.5" });
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(
      api.downloadAndInstall(() => {}, { expectedVersion: "0.1.4" }),
    ).rejects.toThrow(/版本已变化/);

    expect(update.download).not.toHaveBeenCalled();
    expect(order).not.toContain("qio_prepare_for_update");
  });
});

describe("结束后端的真实结果与失败恢复", () => {
  it("结束后端失败（超时/权限/未验证退出）→ 不安装，并尝试恢复后端", async () => {
    const order: string[] = [];
    routeCommands(order, {
      qio_prepare_for_update: () => {
        throw new Error("结束后端进程树失败：仍有进程存活 [4242]");
      },
    });
    const update = updateStub();
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/仍有进程存活/);

    expect(update.install).not.toHaveBeenCalled();
    expect(order).toContain("qio_restore_backend");
  });

  it("后端返回「未确认退出」的结果（verified=false）也算失败，绝不继续安装", async () => {
    const order: string[] = [];
    routeCommands(order, {
      qio_prepare_for_update: () => ({ ...PREP_OK, verified: false, stopped: false }),
    });
    const update = updateStub();
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/未确认退出/);

    expect(update.install).not.toHaveBeenCalled();
    expect(order).toContain("qio_restore_backend");
  });

  it("结束后端成功但安装失败 → 恢复后端，错误信息说明后端状态", async () => {
    const order: string[] = [];
    routeCommands(order);
    const update = updateStub({
      install: vi.fn(async () => {
        throw new Error("ShellExecute failed");
      }),
    });
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/后端已恢复/);

    expect(order).toContain("qio_restore_backend");
  });

  it("恢复失败时给出明确、可恢复的状态（不假装没事）", async () => {
    const order: string[] = [];
    routeCommands(order, {
      qio_restore_backend: () => ({
        restored: false,
        started: false,
        port: 0,
        detail: "重新拉起后端失败：sidecar spawn failed",
      }),
    });
    const update = updateStub({
      install: vi.fn(async () => {
        throw new Error("ShellExecute failed");
      }),
    });
    mocks.check.mockResolvedValue(update);

    const api = await tauriUpdaterApi();
    await expect(api.downloadAndInstall(() => {})).rejects.toThrow(/请完全退出 QIO 后重新启动/);
  });
});

describe("一次更新操作使用自己的代理配置", () => {
  it("探测结果显式传给 check（检查/下载共用同一 Update 上下文）", async () => {
    const order: string[] = [];
    routeCommands(order, { qio_refresh_updater_proxy: () => "http://127.0.0.1:7890" });
    mocks.check.mockResolvedValue(updateStub());

    const api = await tauriUpdaterApi();
    await api.downloadAndInstall(() => {});

    expect(mocks.check).toHaveBeenCalledWith({ proxy: "http://127.0.0.1:7890" });
    // 探测命令在这条操作里只跑一次：检查与下载共用同一个结论
    expect(order.filter((cmd) => cmd === "qio_refresh_updater_proxy")).toHaveLength(1);
  });

  it("探测结论是直连时不传 proxy（也不写任何进程级环境变量）", async () => {
    const order: string[] = [];
    routeCommands(order);
    mocks.check.mockResolvedValue(updateStub());

    const api = await tauriUpdaterApi();
    await api.downloadAndInstall(() => {});

    expect(mocks.check).toHaveBeenCalledWith(undefined);
  });

  it("普通检查也带上本次操作的代理", async () => {
    const order: string[] = [];
    routeCommands(order, { qio_refresh_updater_proxy: () => "http://127.0.0.1:7890" });
    mocks.check.mockResolvedValue(updateStub());

    const api = await tauriUpdaterApi();
    const info = await api.check();

    expect(info?.version).toBe("0.1.4");
    expect(mocks.check).toHaveBeenCalledWith({ proxy: "http://127.0.0.1:7890" });
  });
});
