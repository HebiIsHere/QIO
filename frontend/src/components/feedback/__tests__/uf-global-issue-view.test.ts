/**
 * 全局异常入口 + 面板：一个入口、数量、逐项原因/动作/诊断折叠、部分恢复、让位。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import GlobalIssueEntry from "../GlobalIssueEntry.vue";
import GlobalIssuePanel from "../GlobalIssuePanel.vue";
import { useGlobalIssuesStore, type GlobalIssue } from "../../../stores/globalIssues";
import { useModalArbiterStore } from "../../../stores/modalArbiter";
import { useUiStore } from "../../../stores/ui";

function issue(over: Partial<GlobalIssue> = {}): GlobalIssue {
  return {
    id: "sse_disconnected",
    kind: "sse_disconnected",
    title: "与后端的实时连接已断开",
    reason: "SSE 连接断开，请确认后端已启动",
    actions: [{ label: "重新连接", kind: "retry-connection" }],
    diagnostics: ["界面显示的是上次读取到的状态"],
    severity: "error",
    ...over,
  };
}

async function tick(): Promise<void> {
  await nextTick();
  await nextTick();
}

describe("全局异常入口：数量 + 逐项 + 诊断折叠", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    document.body.innerHTML = "";
  });

  it("没有问题时入口不渲染；有问题显示独立问题数量", async () => {
    const issues = useGlobalIssuesStore();
    const w = mount(GlobalIssueEntry);
    expect(w.find("[data-uf-issue-entry]").exists()).toBe(false);

    issues.upsert(issue());
    issues.upsert(issue({ id: "no_credentials", kind: "no_credentials", title: "没有可用的模型凭据", severity: "error" }));
    await tick();
    const entry = w.find("[data-uf-issue-entry]");
    expect(entry.exists()).toBe(true);
    expect(entry.text()).toContain("问题");
    expect(entry.text()).toContain("2");
    expect(String(entry.attributes("aria-label"))).toContain("2 个独立问题");
    w.unmount();
  });

  it("根因去重（同一 id 不计两次），面板逐项显示原因并可展开诊断", async () => {
    const issues = useGlobalIssuesStore();
    issues.upsert(issue());
    issues.upsert(issue({ reason: "第二次上报的原因" }));
    expect(issues.count).toBe(1);
    issues.openPanel();
    const w = mount(GlobalIssuePanel, { attachTo: document.body });
    await tick();

    expect(w.findAll(".uf-issue")).toHaveLength(1);
    expect(w.text()).toContain("第二次上报的原因");
    expect(w.text()).not.toContain("界面显示的是上次读取到的状态");
    await w.find('[data-test="global-issue-diagnostics-sse_disconnected"]').trigger("click");
    expect(w.text()).toContain("界面显示的是上次读取到的状态");
    w.unmount();
  });

  it("部分恢复只移除对应一项；全部恢复后入口与面板一起消失", async () => {
    const issues = useGlobalIssuesStore();
    issues.upsert(issue());
    issues.upsert(issue({ id: "restore_failed", kind: "restore_failed", title: "界面状态可能不是最新的" }));
    issues.openPanel();
    const w = mount(GlobalIssuePanel, { attachTo: document.body });
    await tick();
    expect(w.findAll(".uf-issue")).toHaveLength(2);

    issues.resolve("sse_disconnected");
    await tick();
    expect(w.findAll(".uf-issue")).toHaveLength(1);

    issues.resolve("restore_failed");
    await tick();
    expect(issues.panelOpen).toBe(false);
    expect(w.find("[data-uf-issue-panel]").exists()).toBe(false);
    w.unmount();
  });

  it("处理动作指向设置页真实分区；动作后收起面板", async () => {
    const issues = useGlobalIssuesStore();
    const ui = useUiStore();
    issues.upsert(issue({ id: "no_credentials", kind: "no_credentials", title: "没有可用的模型凭据", actions: [{ label: "打开凭据设置", kind: "settings", section: "cred" }] }));
    issues.openPanel();
    const w = mount(GlobalIssuePanel, { attachTo: document.body });
    await tick();

    await w.find('[data-test="global-issue-action-no_credentials"]').trigger("click");
    await tick();
    expect(ui.settingsSection).toBe("cred");
    expect(issues.panelOpen).toBe(false);
    w.unmount();
  });

  it("有中央弹窗时面板自动收起（不与弹窗抢层）", async () => {
    const issues = useGlobalIssuesStore();
    const arbiter = useModalArbiterStore();
    issues.upsert(issue());
    issues.openPanel();
    const w = mount(GlobalIssuePanel, { attachTo: document.body });
    await tick();
    expect(w.find("[data-uf-issue-panel]").exists()).toBe(true);

    arbiter.open({ id: "impact-1", kind: "impact" });
    await tick();
    expect(issues.panelOpen).toBe(false);
    expect(w.find("[data-uf-issue-panel]").exists()).toBe(false);
    w.unmount();
  });

  it("Esc 关闭面板并把焦点还给入口", async () => {
    const issues = useGlobalIssuesStore();
    issues.upsert(issue());
    issues.openPanel();
    // 模拟 WindowChrome 里的入口
    const entryButton = document.createElement("button");
    entryButton.setAttribute("data-uf-issue-entry", "");
    document.body.appendChild(entryButton);
    const w = mount(GlobalIssuePanel, { attachTo: document.body });
    await tick();

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await tick();
    expect(issues.panelOpen).toBe(false);
    expect(document.activeElement).toBe(entryButton);
    w.unmount();
  });
});
