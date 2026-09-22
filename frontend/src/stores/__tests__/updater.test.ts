/**
 * 应用内更新的状态机与错误映射（spec 2026-09-22-updater-design §3 / §9）：
 *
 * * 按钮文案必须与真实状态一致 —— 「检查失败」不能显示成「已是最新」；
 * * 下载与安装只能由用户点击触发（store 不自动下载）；
 * * 校验失败必须归到 signature，且不重试安装。
 */
import { describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import {
  compareVersions,
  describeUpdateError,
  type UpdaterApi,
} from "../../services/updater";
import { useUpdaterStore } from "../updater";

function fakeApi(overrides: Partial<UpdaterApi> = {}): UpdaterApi {
  return {
    currentVersion: vi.fn(async () => "0.1.3"),
    check: vi.fn(async () => null),
    downloadAndInstall: vi.fn(async (onProgress) => {
      onProgress({ downloaded: 50, total: 100, percent: 50 });
      onProgress({ downloaded: 100, total: 100, percent: 100 });
    }),
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

describe("版本比较", () => {
  it("按数字段比较，不做字符串比较", () => {
    expect(compareVersions("0.1.10", "0.1.2")).toBeGreaterThan(0);
    expect(compareVersions("0.1.2", "0.1.10")).toBeLessThan(0);
    expect(compareVersions("v0.2.0", "0.1.9")).toBeGreaterThan(0);
    expect(compareVersions("0.1.2", "0.1.2")).toBe(0);
  });
});

describe("错误分类", () => {
  it("校验失败 / 网络失败 / 安装器失败各有各的说法", () => {
    expect(describeUpdateError(new Error("signature verification failed")).kind).toBe("signature");
    expect(describeUpdateError(new Error("failed to fetch: network unreachable")).kind).toBe("network");
    expect(describeUpdateError(new Error("installer exited with code 1")).kind).toBe("installer");
  });

  it("未知错误也不吞，给原文", () => {
    const out = describeUpdateError(new Error("boom"));
    expect(out.kind).toBe("unknown");
    expect(out.message).toContain("boom");
  });

  it("任何分类都必须带出原始信息（不许用我们猜的原因盖掉真因）", () => {
    // 实测：插件连不上、代理不对、清单解析失败都会报同一句
    // "Could not fetch a valid release JSON from the remote"
    const out = describeUpdateError(
      new Error("Could not fetch a valid release JSON from the remote"),
    );
    expect(out.message).toContain("Could not fetch a valid release JSON from the remote");
  });
});

describe("更新状态机", () => {
  it("没有新版本 → up-to-date（只有这条路径能显示「已是最新」）", async () => {
    const store = setup(fakeApi());
    await store.check();
    expect(store.phase).toBe("up-to-date");
    expect(store.currentVersion).toBe("0.1.3");
  });

  it("检查失败 → failed，且不是 up-to-date", async () => {
    const store = setup(
      fakeApi({
        check: vi.fn(async () => {
          throw new Error("failed to fetch: network unreachable");
        }),
      }),
    );
    await store.check();
    expect(store.phase).toBe("failed");
    expect(store.errorKind).toBe("network");
    expect(store.message).toContain("更新源请求失败");
    expect(store.message).toContain("network unreachable"); // 原始信息必须可见
  });

  it("发现新版本 → available，但不自动下载", async () => {
    const api = fakeApi({
      check: vi.fn(async () => ({ version: "0.1.4", notes: "修了 X", date: "2026-09-22" })),
    });
    const store = setup(api);
    await store.check();
    expect(store.phase).toBe("available");
    expect(store.availableVersion).toBe("0.1.4");
    expect(api.downloadAndInstall).not.toHaveBeenCalled();
  });

  it("下载并安装 → downloading → ready，进度可读", async () => {
    const api = fakeApi({
      check: vi.fn(async () => ({ version: "0.1.4" })),
    });
    const store = setup(api);
    await store.check();
    await store.download();
    expect(store.phase).toBe("ready");
    expect(store.progressPercent).toBe(100);
  });

  it("校验失败 → failed(signature)，不进入 ready", async () => {
    const api = fakeApi({
      check: vi.fn(async () => ({ version: "0.1.4" })),
      downloadAndInstall: vi.fn(async () => {
        throw new Error("signature verification failed");
      }),
    });
    const store = setup(api);
    await store.check();
    await store.download();
    expect(store.phase).toBe("failed");
    expect(store.errorKind).toBe("signature");
    expect(store.message).toContain("校验");
  });

  it("失败之后可以重新检查（不卡死在 failed）", async () => {
    const check = vi
      .fn()
      .mockRejectedValueOnce(new Error("failed to fetch"))
      .mockResolvedValueOnce(null);
    const store = setup(fakeApi({ check }));
    await store.check();
    expect(store.phase).toBe("failed");
    await store.check();
    expect(store.phase).toBe("up-to-date");
  });

  it("只有用户点「立即重启」才会调用 relaunch", async () => {
    const api = fakeApi({ check: vi.fn(async () => ({ version: "0.1.4" })) });
    const store = setup(api);
    await store.check();
    await store.download();
    expect(api.relaunch).not.toHaveBeenCalled();
    await store.restart();
    expect(api.relaunch).toHaveBeenCalledTimes(1);
  });
});
