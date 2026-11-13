/**
 * #57 预览反馈（面板侧）：
 *
 * 成功打开的反馈由预览面板自己承担：一个 aria-live=polite 的读屏状态说出
 * 「已打开「name」的只读预览」，视觉上沿用既有「只读」徽标与激活标签高亮；
 * 文案严格由当前激活标签派生 —— 切换标签立即更新，关闭标签 / 收起面板后不残留
 * 旧成功文案；同名不同文件按附件 id 是两个标签。
 *
 * 运行：cd frontend; npx vitest run src/components/conversation/__tests__/cv-c-preview-feedback.test.ts
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, h, nextTick, ref } from "vue";

const mocks = vi.hoisted(() => ({
  getUISettings: vi.fn(),
  saveConversationLayout: vi.fn(),
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getUISettings: mocks.getUISettings,
      saveConversationLayout: mocks.saveConversationLayout,
    },
  };
});

import PreviewPanel from "../PreviewPanel.vue";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import type { PreviewTabState } from "../../../conversation/previewTypes";

function attachmentTab(attachmentId: string, title: string): PreviewTabState {
  return {
    id: "attachment:" + attachmentId,
    target: {
      id: "attachment:" + attachmentId,
      kind: "text",
      title,
      source: "attachment",
      attachmentId,
    },
    position: {},
  };
}

function seedTabs(tabs: PreviewTabState[]): void {
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = tabs;
  shell.prefs.activePreviewTabId = tabs[0]?.id ?? null;
  shell.prefs.rightOpen = tabs.length > 0;
  const preview = useConversationPreviewStore();
  for (const tab of tabs) {
    preview.runtime[tab.id] = {
      phase: "loading",
      envelope: null,
      problem: null,
      version: "",
      notice: "",
      updateError: "",
      updateFailures: 0,
      loadedAt: 0,
    };
  }
}

function statusText(w: ReturnType<typeof mount>): string {
  const el = w.find("[data-test='preview-open-status']");
  return el.exists() ? el.text() : "";
}

beforeEach(() => {
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  mocks.getUISettings.mockReset();
  mocks.getUISettings.mockResolvedValue({ conversation_layout: null });
  mocks.saveConversationLayout.mockReset();
  mocks.saveConversationLayout.mockImplementation(async (prefs: unknown) => ({
    conversation_layout: prefs,
  }));
});

afterEach(() => {
  useConversationPreviewStore().resetForTests();
  useConversationShellStore().dispose();
  vi.restoreAllMocks();
});

describe("预览面板的读屏反馈", () => {
  it("提供 aria-live=polite 的状态：说出当前打开文件与「只读」", async () => {
    seedTabs([attachmentTab("att_a", "报告.pdf")]);
    const w = mount(PreviewPanel);
    await flushPromises();
    const live = w.find("[data-test='preview-open-status']");
    expect(live.exists()).toBe(true);
    expect(live.attributes("role")).toBe("status");
    expect(live.attributes("aria-live")).toBe("polite");
    expect(live.text()).toBe("已打开「报告.pdf」的只读预览");
    // 视觉上沿用既有「只读」徽标，不新增可见的成功提示条
    expect(w.find(".pv-head-badge").text()).toBe("只读");
    expect(w.text()).not.toContain("已在右侧打开");
    w.unmount();
  });

  it("切换标签：状态立即跟随新的激活标签，不残留旧文件文案", async () => {
    seedTabs([attachmentTab("att_a", "第一份.pdf"), attachmentTab("att_b", "第二份.pdf")]);
    const w = mount(PreviewPanel);
    await flushPromises();
    expect(statusText(w)).toBe("已打开「第一份.pdf」的只读预览");

    const tabs = w.findAll('[role="tab"]');
    await tabs[1].trigger("click");
    await nextTick();
    expect(w.find("[data-test='preview-open-status']").text()).toBe(
      "已打开「第二份.pdf」的只读预览",
    );
    expect(w.find("[data-test='preview-open-status']").text()).not.toContain("第一份.pdf");
    w.unmount();
  });

  it("关闭标签：不再保留已关闭文件的成功文案；关掉最后一个后状态清空", async () => {
    seedTabs([attachmentTab("att_a", "第一份.pdf"), attachmentTab("att_b", "第二份.pdf")]);
    const w = mount(PreviewPanel);
    await flushPromises();

    // 关当前激活的「第一份」：状态应切到剩下的「第二份」
    await w.findAll(".pv-tab-close")[0].trigger("click");
    await nextTick();
    expect(statusText(w)).toBe("已打开「第二份.pdf」的只读预览");
    expect(statusText(w)).not.toContain("第一份.pdf");

    // 关掉最后一个：没有打开中的文件 → 不留旧成功文案
    await w.findAll(".pv-tab-close")[0].trigger("click");
    await nextTick();
    expect(statusText(w)).toBe("");
    expect(w.text()).not.toContain("已打开");
    w.unmount();
  });

  it("同名不同文件是两个标签：状态跟随附件身份，关掉一个后仍指向剩下的那个", async () => {
    seedTabs([attachmentTab("att_a", "同名.pdf"), attachmentTab("att_b", "同名.pdf")]);
    const w = mount(PreviewPanel);
    await flushPromises();
    expect(w.findAll('[role="tab"]')).toHaveLength(2);
    expect(statusText(w)).toBe("已打开「同名.pdf」的只读预览");

    await w.findAll(".pv-tab-close")[0].trigger("click");
    await nextTick();
    // 剩下的是 att_b：同名文案仍然成立，但状态不是从旧标签复制来的
    expect(useConversationShellStore().prefs.previewTabs.map((t) => t.id)).toEqual([
      "attachment:att_b",
    ]);
    expect(statusText(w)).toBe("已打开「同名.pdf」的只读预览");
    w.unmount();
  });

  it("收起面板（父层卸载）后不残留状态；再次打开只显示当前标签", async () => {
    seedTabs([attachmentTab("att_a", "第一份.pdf"), attachmentTab("att_b", "第二份.pdf")]);
    const open = ref(true);
    const Host = defineComponent({
      setup() {
        return () =>
          h("div", [
            open.value
              ? h(PreviewPanel, {
                  onClose: () => {
                    open.value = false;
                  },
                })
              : null,
          ]);
      },
    });
    const w = mount(Host);
    await flushPromises();
    expect(w.find("[data-test='preview-open-status']").text()).toBe(
      "已打开「第一份.pdf」的只读预览",
    );

    // 点「收起」→ 父层移除面板 → 成功文案随组件一起消失
    const close = w.findAll("button").find((button) => button.text() === "收起");
    expect(close).toBeTruthy();
    await close!.trigger("click");
    await nextTick();
    expect(open.value).toBe(false);
    expect(w.find("[data-test='preview-open-status']").exists()).toBe(false);
    expect(w.text()).not.toContain("已打开");

    // 再次打开（切到第二个标签）：只显示当前标签，不残留上一次的成功文案
    useConversationShellStore().setActivePreviewTab("attachment:att_b");
    open.value = true;
    await nextTick();
    await flushPromises();
    expect(w.find("[data-test='preview-open-status']").text()).toBe(
      "已打开「第二份.pdf」的只读预览",
    );
    w.unmount();
  });
});
