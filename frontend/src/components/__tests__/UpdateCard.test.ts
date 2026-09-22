/**
 * 更新卡片的文案契约：按钮文案与可用性必须与真实状态一致，
 * 「检查失败」不能显示成「已是最新」（spec §3 / §9）。
 */
import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import UpdateCard from "../UpdateCard.vue";
import { useUpdaterStore } from "../../stores/updater";
import type { UpdaterApi } from "../../services/updater";

function api(overrides: Partial<UpdaterApi> = {}): UpdaterApi {
  return {
    currentVersion: vi.fn(async () => "0.1.3"),
    check: vi.fn(async () => null),
    downloadAndInstall: vi.fn(async () => {}),
    relaunch: vi.fn(async () => {}),
    ...overrides,
  };
}

function mountCard(overrides: Partial<UpdaterApi> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useUpdaterStore();
  store.configure(api(overrides));
  const w = mount(UpdateCard, { global: { plugins: [pinia] } });
  return { w, store };
}

beforeEach(() => {
  localStorage.clear();
});

describe("更新卡片", () => {
  it("初始状态：能看到当前版本与「检查更新」按钮", async () => {
    const { w, store } = mountCard();
    await store.check();
    expect(w.text()).toContain("0.1.3");
    const button = w.find("[data-action='check']");
    expect(button.exists()).toBe(true);
    expect(button.text()).toContain("检查更新");
  });

  it("已是最新：明确说「已是最新版本」，且没有下载按钮", async () => {
    const { w, store } = mountCard();
    await store.check();
    expect(w.text()).toContain("已是最新版本");
    expect(w.find("[data-action='download']").exists()).toBe(false);
  });

  it("有新版本：显示版本号与「下载并安装」，不自动下载", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => ({ version: "0.1.4", notes: "修了 X" })),
    });
    await store.check();
    const button = w.find("[data-action='download']");
    expect(button.exists()).toBe(true);
    expect(button.text()).toContain("下载并安装");
    expect(w.text()).toContain("0.1.4");
  });

  it("检查失败：显示失败原因，不写「已是最新」", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => {
        throw new Error("failed to fetch: network unreachable");
      }),
    });
    await store.check();
    expect(w.text()).not.toContain("已是最新");
    expect(w.text()).toContain("更新源请求失败");
    expect(w.text()).toContain("原始信息"); // 真因必须显示出来，不能被我们的措辞盖掉
    expect(w.find("[data-action='check']").text()).toContain("重试");
  });

  it("下载完成：给出「立即重启」并提示重启后生效", async () => {
    const { w, store } = mountCard({ check: vi.fn(async () => ({ version: "0.1.4" })) });
    await store.check();
    await store.download();
    expect(w.text()).toContain("重启");
    expect(w.find("[data-action='restart']").exists()).toBe(true);
  });
});
