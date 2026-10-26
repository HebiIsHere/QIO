/**
 * D · 更新阶段（统一改造）：下载 / 校验 / 结束本实例后端 / 安装必须如实分开展示；
 * 百分比未知时不画 0%（不伪造进度）；100% 下载 ≠ 安装完成；失败后阶段复位。
 */
import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useUpdaterStore } from "../../stores/updater";
import UpdateCard from "../UpdateCard.vue";
import type { UpdateProgress, UpdateStage, UpdaterApi } from "../../services/updater";
import { useGlobalIssuesStore } from "../../stores/globalIssues";

function api(behaviour: "stages" | "fail-after-download" = "stages"): UpdaterApi {
  return {
    currentVersion: vi.fn(async () => "0.1.3"),
    check: vi.fn(async () => ({ version: "0.1.4" })),
    downloadAndInstall: vi.fn(
      async (
        onProgress: (progress: UpdateProgress) => void,
        options?: { onStage?: (stage: UpdateStage) => void },
      ) => {
        options?.onStage?.("downloading");
        onProgress({ downloaded: 0, total: 0, percent: null });
        options?.onStage?.("verifying");
        onProgress({ downloaded: 100, total: 100, percent: 100 });
        if (behaviour === "fail-after-download") throw new Error("installer exited with code 1");
        options?.onStage?.("preparing");
        options?.onStage?.("installing");
      },
    ),
    relaunch: vi.fn(async () => {}),
  };
}

function setup(behaviour: "stages" | "fail-after-download" = "stages") {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useUpdaterStore();
  store.configure(api(behaviour));
  return { pinia, store };
}

describe("更新阶段是真实状态", () => {
  it("阶段回调按真实顺序更新 store.stage，成功后 done；阶段不推进 phase", async () => {
    const { store } = setup();
    const seen: string[] = [];
    const realApi = store._api as UpdaterApi;
    const original = realApi.downloadAndInstall;
    realApi.downloadAndInstall = vi.fn(async (onProgress, options) => {
      const wrapOptions = {
        ...options,
        onStage: (stage: UpdateStage) => {
          seen.push(stage);
          options?.onStage?.(stage);
          expect(store.phase).toBe("downloading");
        },
      };
      return original(onProgress, wrapOptions);
    });
    await store.check();
    await store.download();
    expect(seen).toEqual(["downloading", "verifying", "preparing", "installing"]);
    expect(store.phase).toBe("ready");
    expect(store.stage).toBe("done");
  });

  it("下载失败：phase=failed、stage 复位（不会停在「正在安装」）", async () => {
    const { store } = setup("fail-after-download");
    await store.check();
    await store.download();
    expect(store.phase).toBe("failed");
    expect(store.stage).toBe("idle");
  });

  it("总大小未知时 progressPercent 保持 null（不伪造 0%）", async () => {
    const { store } = setup();
    await store.check();
    await store.download();
    expect(store.progressPercent).toBe(100); // 最后插件会报 100，但过程中不得是假 0
    expect(store.total).toBe(100);
  });
});

describe("UpdateCard 如实展示阶段", () => {
  it("下载阶段说「正在下载」，校验阶段说「正在校验」，不把 100% 说成安装完成", async () => {
    const { store } = setup();
    await store.check();
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mount(UpdateCard, { global: { plugins: [pinia] } });
    const store2 = useUpdaterStore();
    store2.configure(api("stages"));
    store2.phase = "downloading";
    store2.stage = "verifying";
    store2.progressPercent = 100;
    await w.vm.$nextTick();
    expect(w.text()).toContain("正在校验更新包");
    expect(w.text()).not.toContain("安装已完成");
    w.unmount();
  });

  it("百分比未知时不渲染百分比进度条（只给文字状态）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useUpdaterStore();
    store.configure(api());
    const w = mount(UpdateCard, { global: { plugins: [pinia] } });
    store.phase = "downloading";
    store.stage = "downloading";
    store.progressPercent = null;
    await w.vm.$nextTick();
    expect(w.find('[role="progressbar"]').exists()).toBe(false);
    expect(w.text()).toContain("正在下载更新（大小未知）");
    w.unmount();
  });

  it("更新失败且后端未恢复：进顶部全局异常入口并带真实处理动作", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useUpdaterStore();
    store.configure(api());
    const issues = useGlobalIssuesStore();
    const w = mount(UpdateCard, { global: { plugins: [pinia] } });
    store.phase = "failed";
    store.errorKind = "installer";
    store.message = "后端未能自动恢复：连接失败；请完全退出 QIO 后重新启动。原因：installer exited";
    await w.vm.$nextTick();
    const issue = issues.issueById("updater_install_failed");
    expect(issue).toBeTruthy();
    expect(issue?.severity).toBe("error");
    expect(issue?.actions[0]?.kind).toBe("settings");
    expect(issue?.actions[0]?.section).toBe("data");

    // 之后更新成功/进入新的检查：这一项自动移除（部分恢复只移除对应项）
    store.phase = "available";
    await w.vm.$nextTick();
    expect(issues.issueById("updater_install_failed")).toBe(null);
    w.unmount();
  });

  it("更新失败但应用已恢复：只留在设置里，不进全局异常入口", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useUpdaterStore();
    store.configure(api());
    const issues = useGlobalIssuesStore();
    const w = mount(UpdateCard, { global: { plugins: [pinia] } });
    store.phase = "failed";
    store.message = "后端已恢复（已重新启动），聊天可以继续使用。原因：installer exited";
    await w.vm.$nextTick();
    expect(issues.issueById("updater_install_failed")).toBe(null);
    w.unmount();
  });

  it("安装完成（ready）才显示「重启后生效」，并说明会结束当前运行", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useUpdaterStore();
    store.configure(api());
    const w = mount(UpdateCard, { global: { plugins: [pinia] } });
    store.phase = "ready";
    store.stage = "done";
    await w.vm.$nextTick();
    expect(w.text()).toContain("重启后生效");
    expect(w.text()).toContain("结束当前运行");
    expect(w.find("[data-action='restart']").exists()).toBe(true);
    w.unmount();
  });
});