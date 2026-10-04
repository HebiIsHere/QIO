/**
 * 自动检查定时器与「单一有效操作」的契约（修复提示词 §2 最后一段）：
 *
 * * 首次静默检查与周期检查的**全部句柄**都要保存；stopAutoCheck / 停用 / 重复启动时清理，
 *   停掉之后首检不得再触发（旧实现只清了 interval，首检的 setTimeout 会漏出去）；
 * * 手动检查、自动检查、安装三者之间**同一时刻只有一个有效操作**；
 * * 旧操作（被停用/被新操作取代）的回调不得再改状态；
 * * 自动检查仍然只检查，不下载不安装。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import type { UpdateInfo, UpdateProgress, UpdaterApi } from "../../services/updater";
import { CHECK_INTERVAL_MS, FIRST_CHECK_DELAY_MS, useUpdaterStore } from "../updater";

function fakeApi(overrides: Partial<UpdaterApi> = {}): UpdaterApi {
  return {
    currentVersion: vi.fn(async () => "0.1.3"),
    check: vi.fn(async () => null),
    downloadAndInstall: vi.fn(async () => {}),
    relaunch: vi.fn(async () => {}),
    ...overrides,
  };
}

function setup(api: UpdaterApi) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useUpdaterStore();
  store.configure(api);
  return store;
}

/** startAutoCheck 只在 Tauri 壳里起定时器（浏览器预览里刻意不起）——测试里显式装一个标记。 */
function markTauriShell() {
  (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
}

function unmarkTauriShell() {
  delete (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  markTauriShell();
});

afterEach(() => {
  vi.useRealTimers();
  unmarkTauriShell();
  localStorage.clear();
});

describe("自动检查的定时器句柄", () => {
  it("stopAutoCheck 之后首次自动检查不得再触发（首检句柄必须被保存并清理）", async () => {
    const api = fakeApi();
    const store = setup(api);

    store.startAutoCheck();
    store.stopAutoCheck();
    await vi.advanceTimersByTimeAsync(FIRST_CHECK_DELAY_MS + 1_000);

    expect(api.check).not.toHaveBeenCalled();
  });

  it("stopAutoCheck 同时清掉周期检查（24 小时内不再有请求）", async () => {
    const api = fakeApi();
    const store = setup(api);

    store.startAutoCheck();
    await vi.advanceTimersByTimeAsync(FIRST_CHECK_DELAY_MS + 10);
    expect(api.check).toHaveBeenCalledTimes(1);

    store.stopAutoCheck();
    await vi.advanceTimersByTimeAsync(CHECK_INTERVAL_MS * 2);
    expect(api.check).toHaveBeenCalledTimes(1);
  });

  it("重复 startAutoCheck 不会叠加出两个首检", async () => {
    const api = fakeApi();
    const store = setup(api);

    store.startAutoCheck();
    store.startAutoCheck();
    store.startAutoCheck();
    await vi.advanceTimersByTimeAsync(FIRST_CHECK_DELAY_MS + 10);

    expect(api.check).toHaveBeenCalledTimes(1);
  });

  it("自动检查只检查：发现新版本也不下载、不安装", async () => {
    const api = fakeApi({ check: vi.fn(async () => ({ version: "9.9.9" }) as UpdateInfo) });
    const store = setup(api);

    store.startAutoCheck();
    await vi.advanceTimersByTimeAsync(FIRST_CHECK_DELAY_MS + 10);

    expect(api.check).toHaveBeenCalledTimes(1);
    expect(api.downloadAndInstall).not.toHaveBeenCalled();
    expect(store.phase).toBe("available");
  });

  it("自动检查开关关掉之后就不再请求更新源", async () => {
    const api = fakeApi();
    const store = setup(api);

    store.setAutoCheck(false);
    store.startAutoCheck();
    await vi.advanceTimersByTimeAsync(FIRST_CHECK_DELAY_MS + CHECK_INTERVAL_MS + 10);

    expect(api.check).not.toHaveBeenCalled();
  });
});

describe("单一有效操作与旧回调", () => {
  it("检查进行中时再次检查是空操作（单飞）", async () => {
    let release!: (value: UpdateInfo | null) => void;
    const check = vi.fn(
      () =>
        new Promise<UpdateInfo | null>((resolve) => {
          release = resolve;
        }),
    );
    const api = fakeApi({ check });
    const store = setup(api);

    const first = store.check();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.phase).toBe("checking");

    await store.check(); // 第二次：直接返回，不得再打一次更新源
    expect(check).toHaveBeenCalledTimes(1);

    release(null);
    await first;
    expect(store.phase).toBe("up-to-date");
  });

  it("下载进行中时自动检查不会插进来（不产生第二个有效操作）", async () => {
    let releaseDownload!: () => void;
    const api = fakeApi({
      check: vi.fn(async () => ({ version: "0.1.4" }) as UpdateInfo),
      downloadAndInstall: vi.fn(
        () =>
          new Promise<void>((resolve) => {
            releaseDownload = resolve;
          }),
      ),
    });
    const store = setup(api);
    await store.check();
    const downloading = store.download();
    await vi.advanceTimersByTimeAsync(0);

    store.startAutoCheck();
    await vi.advanceTimersByTimeAsync(FIRST_CHECK_DELAY_MS + 10);
    expect(api.check).toHaveBeenCalledTimes(1); // 只有开始下载前那一次

    releaseDownload();
    await downloading;
    expect(store.phase).toBe("ready");
  });

  it("下载失败后，迟到的进度回调不得改新状态", async () => {
    let lateProgress!: (progress: UpdateProgress) => void;
    const check = vi
      .fn()
      .mockResolvedValueOnce({ version: "0.1.4" } as UpdateInfo)
      .mockResolvedValueOnce(null);
    const api = fakeApi({
      check,
      downloadAndInstall: vi.fn(async (onProgress: (progress: UpdateProgress) => void) => {
        lateProgress = onProgress;
        onProgress({ downloaded: 10, total: 100, percent: 10 });
        throw new Error("installer exited with code 1");
      }),
    });
    const store = setup(api);
    await store.check();
    await store.download();
    expect(store.phase).toBe("failed");

    // 用户重新检查（新操作），失败的下载不得再影响它
    await store.check();
    expect(store.phase).toBe("up-to-date");
    const downloadedBefore = store.downloaded;

    lateProgress({ downloaded: 100, total: 100, percent: 100 });

    expect(store.progressPercent).not.toBe(100);
    expect(store.downloaded).toBe(downloadedBefore);
    expect(store.phase).toBe("up-to-date");
  });

  it("停用自动检查后，在飞的首检回调不得把状态推到「已是最新」", async () => {
    let release!: (value: UpdateInfo | null) => void;
    const check = vi.fn(
      () =>
        new Promise<UpdateInfo | null>((resolve) => {
          release = resolve;
        }),
    );
    const api = fakeApi({ check });
    const store = setup(api);
    expect(store.phase).toBe("idle");

    store.startAutoCheck();
    await vi.advanceTimersByTimeAsync(FIRST_CHECK_DELAY_MS + 10);
    expect(check).toHaveBeenCalledTimes(1);
    expect(store.phase).toBe("checking");

    store.setAutoCheck(false); // 用户在检查途中关掉开关
    release(null);
    await vi.advanceTimersByTimeAsync(0);

    expect(store.phase).toBe("idle");
  });
});
