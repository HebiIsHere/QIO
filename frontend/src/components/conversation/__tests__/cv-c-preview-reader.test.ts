/**
 * C 预览阅读器内容渲染定向用例（真实渲染器，不 mock 渲染层）：
 *   * DOCX 结构化正文真的渲染出来，并明确说明「不是原始排版」的范围；
 *   * 搜索按段落命中并跳转到对应段落（不是只显示一个数字）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

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

import PreviewReader from "../preview/PreviewReader.vue";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import type { PreviewTabState } from "../../../conversation/previewTypes";

function seedDocxTab(): PreviewTabState {
  const tab: PreviewTabState = {
    id: "attachment:att-doc@v1",
    target: {
      id: "attachment:att-doc@v1",
      kind: "docx",
      title: "方案.docx",
      source: "attachment",
      attachmentId: "att-doc",
      version: "v1",
    },
    position: {},
  };
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = [tab];
  shell.prefs.activePreviewTabId = tab.id;
  shell.prefs.rightOpen = true;
  const preview = useConversationPreviewStore();
  preview.runtime[tab.id] = {
    phase: "ready",
    envelope: {
      status: "ready",
      kind: "docx",
      version: "v1",
      attachmentId: "att-doc",
      recipe: 1,
      data: {
        kind: "docx",
        blocks: [
          {
            type: "paragraph",
            level: null,
            ordered: null,
            runs: [{ text: "第一段有 needle", bold: false, italic: false }],
            text: "第一段有 needle",
            marker: "",
          },
          {
            type: "heading",
            level: 1,
            ordered: null,
            runs: [{ text: "标题", bold: false, italic: false }],
            text: "标题",
            marker: "H1",
          },
        ],
        tables: [{ rows: [["单元格", "needle 在表格里"]], columns: 2 }],
        truncated: false,
        empty: false,
      },
      reason: "",
      truncated: false,
      stats: {},
    },
    problem: null,
    version: "v1",
    notice: "",
    updateError: "",
    updateFailures: 0,
    loadedAt: 1,
  };
  return tab;
}

function seedMarkdownTab(): PreviewTabState {
  const tab: PreviewTabState = {
    id: "task:markdown-task:output",
    target: {
      id: "task:markdown-task:output",
      kind: "markdown",
      title: "说明.md",
      source: "task_output",
      taskId: "markdown-task",
    },
    position: {},
  };
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = [tab];
  shell.prefs.activePreviewTabId = tab.id;
  shell.prefs.rightOpen = true;
  const preview = useConversationPreviewStore();
  preview.documents[tab.id] = {
    text: "# 标题\n\n这里有 needle，另一处 needle。\n\n- 列表项",
    language: "markdown",
  };
  preview.runtime[tab.id] = {
    phase: "ready",
    envelope: null,
    problem: null,
    version: "",
    notice: "",
    updateError: "",
    updateFailures: 0,
    loadedAt: 1,
  };
  return tab;
}

beforeEach(() => {
  setActivePinia(createPinia());
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

describe("Markdown 按正文解析 + 搜索高亮", () => {
  it("渲染标题/列表，搜索时在正文里标出命中并计数", async () => {
    const tab = seedMarkdownTab();
    const wrapper = mount(PreviewReader, { props: { tab } });
    await flushPromises();
    expect(wrapper.find(".pv-markdown").exists()).toBe(true);
    expect(wrapper.find(".pv-markdown h1").text()).toContain("标题");
    expect(wrapper.find(".pv-markdown li").exists()).toBe(true);

    await wrapper.get('input[type="search"]').setValue("needle");
    await new Promise((resolve) => setTimeout(resolve, 260));
    await flushPromises();
    expect(wrapper.get("[data-pv-search-count]").text()).toContain("/ 2");
    expect(wrapper.findAll("mark.pv-hit")).toHaveLength(2);
    wrapper.unmount();
  });
});

describe("DOCX 结构化阅读", () => {
  it("渲染段落/标题/表格，并说明结构化预览的范围", async () => {
    const tab = seedDocxTab();
    const wrapper = mount(PreviewReader, { props: { tab } });
    await flushPromises();
    await vi.waitFor(() => {
      expect(wrapper.findAll("[data-block]")).toHaveLength(2);
    });
    expect(wrapper.get('[data-block="0"]').text()).toContain("第一段有 needle");
    expect(wrapper.get('[data-block="1"]').text()).toContain("标题");
    expect(wrapper.find("[data-table]").exists()).toBe(true);
    expect(wrapper.text()).toContain("不保证与原始 Office 排版完全一致");
    wrapper.unmount();
  });

  it("搜索命中段落/表格，并能跳到对应段落", async () => {
    const tab = seedDocxTab();
    const wrapper = mount(PreviewReader, { props: { tab } });
    await flushPromises();
    await flushPromises();

    const input = wrapper.get('input[type="search"]');
    await input.setValue("needle");
    await new Promise((resolve) => setTimeout(resolve, 260));
    await flushPromises();
    expect(wrapper.get("[data-pv-search-count]").text()).toContain("/ 2");

    const next = wrapper.findAll("button").find((button) => button.attributes("aria-label") === "下一个匹配");
    expect(next).toBeTruthy();
    await next!.trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-block="0"]').classes()).toContain("is-hit");
    wrapper.unmount();
  });
});
