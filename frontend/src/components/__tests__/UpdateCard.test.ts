/**
 * 更新卡片的文案契约：按钮文案与可用性必须与真实状态一致，
 * 「检查失败」不能显示成「已是最新」（spec §3 / §9）。
 */
import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
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

  it("检查失败：主界面说清失败与下一步，原始错误进按需展开的诊断", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => {
        throw new Error("failed to fetch: network unreachable");
      }),
    });
    await store.check();
    expect(w.text()).not.toContain("已是最新");
    // 主界面：按阶段说「检查更新失败」+ 原因 + 实际可用的下一步（重试重新检查）
    expect(w.find("[data-update-failure]").text()).toBe("检查更新失败");
    expect(w.find("[data-update-reason]").text()).toContain("更新源请求失败");
    expect(w.find("[data-update-next]").exists()).toBe(true);
    expect(w.find("[data-action='check']").text()).toContain("重试");
    // 原始错误不铺在常态失败行里，但也不丢：在默认折叠的诊断里
    const diagnostics = w.find("[data-update-diagnostics]");
    expect(diagnostics.exists()).toBe(true);
    expect(diagnostics.attributes("open")).toBeUndefined();
    expect(diagnostics.text()).toContain("network unreachable");
    expect(w.find("[data-update-failure]").text()).not.toContain("network unreachable");
  });

  it("下载完成：给出「立即重启」并提示重启后生效", async () => {
    const { w, store } = mountCard({ check: vi.fn(async () => ({ version: "0.1.4" })) });
    await store.check();
    await store.download();
    expect(w.text()).toContain("重启");
    expect(w.find("[data-action='restart']").exists()).toBe(true);
  });
});

describe("更新卡片文案与安装路径（#19 / #25）", () => {
  it("自动检查开关写「启动时检查更新」，并说明只检查、不自动下载安装", async () => {
    const { w } = mountCard();
    const toggle = w.find("[role='switch']");
    expect(toggle.attributes("aria-label")).toBe("启动时检查更新");
    expect(w.text()).toContain("启动时检查更新");
    expect(w.text()).toContain("启动时只检查更新，不会自动下载或安装");
  });

  it("代理与日志诊断默认不出现在卡片上，只在失败详情里出现", async () => {
    const { w, store } = mountCard();
    await store.check();
    expect(w.text()).not.toContain("updater-proxy.txt");
    expect(w.text()).not.toContain("qio.log");
  });

  it("失败时代理与日志路径只在按需展开的诊断里（默认折叠）", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => {
        throw new Error("failed to fetch: network unreachable");
      }),
    });
    await store.check();
    const diagnostics = w.find("[data-update-diagnostics]");
    expect(diagnostics.exists()).toBe(true);
    expect(diagnostics.attributes("open")).toBeUndefined();
    expect(diagnostics.text()).toContain("updater-proxy.txt");
    expect(diagnostics.text()).toContain("qio.log");
    // 主界面（失败行本身）不出现排查路径
    expect(w.find("[data-update-failure]").text()).not.toContain("updater-proxy.txt");
    expect(w.find("[data-update-next]").text()).not.toContain("qio.log");
  });

  it("下载失败：停在结果页并给出「重试下载安装」，代理与日志进失败详情", async () => {
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
    expect(w.find("[data-update-next]").text()).toContain("重试下载安装");
    const diagnostics = w.find("[data-update-diagnostics]");
    expect(diagnostics.text()).toContain("updater-proxy.txt");
    expect(diagnostics.text()).toContain("qio.log");
    expect(diagnostics.text()).toContain("signature verify failed");
  });

  it("有新版本：只有「下载并安装」一个安装入口，说明关闭与重开，不新增安装前确认框", async () => {
    const { w, store } = mountCard({ check: vi.fn(async () => ({ version: "0.1.4" })) });
    await store.check();
    expect(w.findAll("[data-action='download']").length).toBe(1);
    expect(w.text()).toContain("安装时 QIO 会关闭，安装完成后需要重新打开");
    expect(w.text()).toContain("签名校验不通过就不会安装");
    // 基线没有安装前二次确认框：本轮不新增（签名校验由 updater 在安装前强制）。
    expect(w.find(".qio-confirm").exists()).toBe(false);
  });

  it("安装完成：结果页统一说「安装已完成，重启 QIO 后生效」", async () => {
    const { w, store } = mountCard({ check: vi.fn(async () => ({ version: "0.1.4" })) });
    await store.check();
    await store.download();
    expect(w.text()).toContain("安装已完成，重启 QIO 后生效");
    expect(w.find("[data-action='restart']").text()).toContain("立即重启");
  });

  it("安装失败且后端已恢复：主界面说「应用已恢复运行，可以重试」，原始原因进诊断", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => ({ version: "0.1.4" })),
      downloadAndInstall: vi.fn(async () => {
        throw new UpdateFailureError("更新没有安装成功；应用已恢复运行，可以重试", {
          kind: "installer",
          detail: "后端已重新启动；ShellExecute failed",
          recovery: "restored",
        });
      }),
    });
    await store.check();
    await store.download();

    expect(w.find("[data-update-failure]").text()).toBe("安装更新失败");
    // 安装失败：下一步那句已经说清「没装上 + 已恢复 + 该做什么」，不再重复一行原因
    expect(w.find("[data-update-reason]").exists()).toBe(false);
    expect(w.find("[data-update-next]").text()).toContain("应用已恢复运行");
    expect(w.find("[data-update-next]").text()).toContain("重试下载安装");
    // 原始原因只在诊断里
    expect(w.find("[data-update-failure]").text()).not.toContain("ShellExecute");
    expect(w.find("[data-update-diagnostics]").text()).toContain("ShellExecute failed");
    // 已恢复：不进全局异常入口
    expect(useGlobalIssuesStore().issueById("updater_install_failed")).toBe(null);
  });

  it("安装失败且后端未恢复：主界面说清「完全退出再启动」，并进全局异常入口", async () => {
    const { w, store } = mountCard({
      check: vi.fn(async () => ({ version: "0.1.4" })),
      downloadAndInstall: vi.fn(async () => {
        throw new UpdateFailureError("更新没有安装成功，且后端未能自动恢复", {
          kind: "installer",
          detail: "后端未能自动恢复：sidecar spawn failed；ShellExecute failed",
          recovery: "unrecovered",
        });
      }),
    });
    await store.check();
    await store.download();

    expect(w.find("[data-update-failure]").text()).toBe("安装更新失败");
    expect(w.find("[data-update-reason]").exists()).toBe(false);
    expect(w.find("[data-update-next]").text()).toContain("完全退出 QIO 再重新启动");
    expect(w.find("[data-update-diagnostics]").text()).toContain("sidecar spawn failed");
    const issue = useGlobalIssuesStore().issueById("updater_install_failed");
    expect(issue).toBeTruthy();
    expect(issue?.severity).toBe("error");
    // 全局栏的诊断里带原始原因（按需展开），主行只放人话
    expect(issue?.reason).toBe("更新没有安装成功，且后端未能自动恢复");
    expect(issue?.diagnostics.join(" ")).toContain("sidecar spawn failed");
  });

  it("检查失败与安装失败的下一步不同：检查失败给「重试」，安装失败给退出/重启指引", async () => {
    const checkFail = mountCard({
      check: vi.fn(async () => {
        throw new Error("failed to fetch: network unreachable");
      }),
    });
    await checkFail.store.check();
    expect(checkFail.w.find("[data-update-next]").text()).toContain("重试");
    expect(checkFail.w.find("[data-update-next]").text()).not.toContain("完全退出 QIO 再重新启动");
    // 检查失败不出现安装失败 / 退出应用 / 文件写入示例
    expect(checkFail.w.find("[data-update-failure]").text()).toBe("检查更新失败");
    expect(checkFail.w.find("[data-update-next]").text()).not.toContain("安装");
    expect(checkFail.w.text()).not.toContain("Can't write");
    checkFail.w.unmount();

    const installFail = mountCard({
      check: vi.fn(async () => ({ version: "0.1.4" })),
      downloadAndInstall: vi.fn(async () => {
        throw new UpdateFailureError("更新没有安装成功，且后端未能自动恢复", {
          kind: "installer",
          detail: "后端未能自动恢复",
          recovery: "unrecovered",
        });
      }),
    });
    await installFail.store.check();
    await installFail.store.download();
    expect(installFail.w.find("[data-update-next]").text()).toContain("完全退出 QIO 再重新启动");
    installFail.w.unmount();
  });

  it("源码里保持单一路径：没有第二个安装按钮、没有真人确认框调用", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const source = readFileSync(resolve(process.cwd(), "src/components/UpdateCard.vue"), "utf8");
    expect(source).not.toContain("QConfirm");
    expect(source).not.toMatch(/window\.(confirm|alert|prompt)/);
    expect([...source.matchAll(/data-action="download"/g)].length).toBe(1);
    expect(source).not.toMatch(/data-action="install"/);
  });
});
