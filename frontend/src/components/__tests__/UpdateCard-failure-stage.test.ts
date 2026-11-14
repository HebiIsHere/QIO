/**
 * 更新失败按阶段说话（返修 b）：
 *
 * 检查失败 / 下载失败 / 安装失败（应用已恢复 / 未恢复）四种状态的主界面文案，
 * 重试动作必须与界面所说的操作一致（检查失败 → 重新检查；下载或安装失败 → 重新下载安装），
 * 原始错误与路径继续留在默认折叠的诊断里。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import UpdateCard from "../UpdateCard.vue";
import { useUpdaterStore } from "../../stores/updater";
import { useGlobalIssuesStore } from "../../stores/globalIssues";
import { UpdateFailureError, type UpdaterApi } from "../../services/updater";

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

const checkFailed = () => new Error("failed to fetch: network unreachable");
const installRestored = () =>
  new UpdateFailureError("更新没有安装成功；应用已恢复运行，可以重试", {
    kind: "installer",
    detail: "后端已重新启动；ShellExecute failed",
    recovery: "restored",
  });
const installUnrecovered = () =>
  new UpdateFailureError("更新没有安装成功，且后端未能自动恢复", {
    kind: "installer",
    detail: "后端未能自动恢复：sidecar spawn failed；ShellExecute failed",
    recovery: "unrecovered",
  });

/** 主界面（失败行 + 下一步）不得混入安装失败 / 退出应用 / 文件写入示例。 */
function expectNoInstallCopy(text: string) {
  expect(text).not.toContain("安装");
  expect(text).not.toContain("完全退出");
  expect(text).not.toContain("Can't write");
  expect(text).not.toContain("qio-backend.exe");
  expect(text).not.toContain("更新没有安装");
}

beforeEach(() => {
  localStorage.clear();
});

describe("仅检查更新失败：只说检查，不出现安装说明", () => {
  it("主行说「检查更新失败」，下一步是重试重新检查", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => {
        throw checkFailed();
      }),
    });
    await store.check();

    expect(w.find("[data-update-failure]").text()).toBe("检查更新失败");
    // 失败原因的人话单独一行（store 的 message），下一步只说检查与重试
    expect(w.find("[data-update-reason]").text()).toContain("更新源请求失败");
    const next = w.find("[data-update-next]").text();
    expect(next).toContain("重试");
    expect(next).toContain("重新检查");
    expectNoInstallCopy(w.find("[data-update-failure]").text());
    expectNoInstallCopy(next);
  });

  it("点「重试」真的重新检查（不是重新下载安装）", async () => {
    const check = vi.fn(async () => {
      throw checkFailed();
    });
    const downloadAndInstall = vi.fn(async () => {});
    const { w, store } = mountCard({ check, downloadAndInstall });
    await store.check();
    expect(check).toHaveBeenCalledTimes(1);

    await w.find("[data-action='check']").trigger("click");
    await flushPromises();

    expect(check).toHaveBeenCalledTimes(2);
    expect(downloadAndInstall).not.toHaveBeenCalled();
  });

  it("诊断默认折叠，原始错误只在里面", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => {
        throw checkFailed();
      }),
    });
    await store.check();

    const diagnostics = w.find("[data-update-diagnostics]");
    expect(diagnostics.exists()).toBe(true);
    expect(diagnostics.attributes("open")).toBeUndefined();
    expect(diagnostics.text()).toContain("network unreachable");
    expect(w.find("[data-update-failure]").text()).not.toContain("network unreachable");
    expect(w.find("[data-update-next]").text()).not.toContain("network unreachable");
  });
});

describe("下载 / 校验失败：应用仍可用，重试重新下载安装", () => {
  it("主行说「下载更新失败」，下一步给重试下载安装，不说完全退出", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => ({ version: "0.1.4" })),
      downloadAndInstall: vi.fn(async () => {
        throw new Error("signature verify failed");
      }),
    });
    await store.check();
    await store.download();

    expect(w.find("[data-update-failure]").text()).toBe("下载更新失败");
    expect(w.find("[data-update-reason]").text()).toContain("更新包校验未通过");
    const next = w.find("[data-update-next]").text();
    expect(next).toContain("重试下载安装");
    // 下载阶段从未停掉后端：不能说「完全退出」，也不能说「应用已恢复」
    expect(next).not.toContain("完全退出");
    expect(next).not.toContain("可以继续使用");
    expect(w.find("[data-update-diagnostics]").attributes("open")).toBeUndefined();
  });

  it("点「重试下载安装」真的重新下载安装（不重新检查）", async () => {
    const check = vi.fn(async () => ({ version: "0.1.4" }));
    const downloadAndInstall = vi.fn(async () => {
      throw new Error("signature verify failed");
    });
    const { w, store } = mountCard({ check, downloadAndInstall });
    await store.check();
    await store.download();
    expect(check).toHaveBeenCalledTimes(1);
    expect(downloadAndInstall).toHaveBeenCalledTimes(1);

    await w.find("[data-action='retry-install']").trigger("click");
    await flushPromises();

    expect(downloadAndInstall).toHaveBeenCalledTimes(2);
    expect(check).toHaveBeenCalledTimes(1);
  });
});

describe("安装失败且应用已恢复", () => {
  it("主行说「安装更新失败」，下一步说可以继续使用并重试下载安装", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => ({ version: "0.1.4" })),
      downloadAndInstall: vi.fn(async () => {
        throw installRestored();
      }),
    });
    await store.check();
    await store.download();

    expect(w.find("[data-update-failure]").text()).toBe("安装更新失败");
    // 安装失败不再重复一行原因：下一步那句已经说清「没装上 + 已恢复 + 该做什么」
    expect(w.find("[data-update-reason]").exists()).toBe(false);
    const next = w.find("[data-update-next]").text();
    expect(next).toContain("应用已恢复运行");
    expect(next).toContain("可以继续使用");
    expect(next).toContain("重试下载安装");
    expect(next).not.toContain("完全退出");
    // 已恢复：不进全局异常入口
    expect(useGlobalIssuesStore().issueById("updater_install_failed")).toBe(null);
    // 原始原因只在折叠诊断里
    expect(w.find("[data-update-failure]").text()).not.toContain("ShellExecute");
    expect(w.find("[data-update-diagnostics]").text()).toContain("ShellExecute failed");
  });
});

describe("安装失败且后端未恢复", () => {
  it("主行说「安装更新失败」，下一步说完全退出再启动，并进全局异常入口", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => ({ version: "0.1.4" })),
      downloadAndInstall: vi.fn(async () => {
        throw installUnrecovered();
      }),
    });
    await store.check();
    await store.download();

    expect(w.find("[data-update-failure]").text()).toBe("安装更新失败");
    expect(w.find("[data-update-reason]").exists()).toBe(false);
    const next = w.find("[data-update-next]").text();
    expect(next).toContain("完全退出 QIO 再重新启动");
    expect(next).not.toContain("可以继续使用");
    const issue = useGlobalIssuesStore().issueById("updater_install_failed");
    expect(issue).toBeTruthy();
    expect(issue?.reason).toBe("更新没有安装成功，且后端未能自动恢复");
    expect(issue?.diagnostics.join(" ")).toContain("sidecar spawn failed");
  });

  it("未恢复时不给「重试下载安装」：按钮只做它真正会做的事（重新检查）", async () => {
    const check = vi.fn(async () => ({ version: "0.1.4" }));
    const downloadAndInstall = vi.fn(async () => {
      throw installUnrecovered();
    });
    const { w, store } = mountCard({ check, downloadAndInstall });
    await store.check();
    await store.download();

    expect(w.find("[data-action='retry-install']").exists()).toBe(false);
    const button = w.find("[data-action='check']");
    expect(button.text()).toBe("重新检查更新");
    await button.trigger("click");
    await flushPromises();
    // 点它真的重新检查，而不是再试一次安装
    expect(check).toHaveBeenCalledTimes(2);
    expect(downloadAndInstall).toHaveBeenCalledTimes(1);
  });
});
