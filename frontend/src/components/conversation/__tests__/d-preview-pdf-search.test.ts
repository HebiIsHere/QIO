/**
 * D 组定向用例（问题 9）：PDF 搜索失败不能被显示成「无匹配」。
 *
 * 验证五态：未搜索 / 搜索中 / 有匹配 / 无匹配 / 搜索失败；
 * 文字读取失败时明确说明搜索未完成并可重试，重试成功后显示真实命中；
 * 快速修改查询、切换文件后旧结果/旧错误不能覆盖当前状态。
 * pdf.js 渲染层用受控假实现，不联网。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  fetchBytes: vi.fn(),
  fetchContent: vi.fn(),
  loadRenderer: vi.fn(),
  renderPage: vi.fn(),
  destroy: vi.fn(),
  pageText: vi.fn(),
  getUISettings: vi.fn(),
  saveConversationLayout: vi.fn(),
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    fetchAttachmentContent: mocks.fetchContent,
    fetchAttachmentBytes: mocks.fetchBytes,
    fetchAttachmentPreview: vi.fn(async () => {
      throw new Error("本用例不应发起预览请求");
    }),
  };
});

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

vi.mock("../../../interactive/renderers", () => ({
  loadRenderer: mocks.loadRenderer,
  disposeRenderer: vi.fn(),
}));

import PreviewReader from "../preview/PreviewReader.vue";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import type { PreviewTabState } from "../../../conversation/previewTypes";

/** 每个 PDF 页面文字提取的假实现：由各用例控制（正常/失败/挂起）。 */
let pageTextImpl: (page: number) => Promise<string> = async () => "";
let pdfAttachmentSeq = 0;

function seedPdfTab(options: { search?: string; attachmentId?: string; version?: string } = {}): PreviewTabState {
  pdfAttachmentSeq += 1;
  const attachmentId = options.attachmentId ?? "att-pdf";
  const version = options.version ?? "v1";
  const tab: PreviewTabState = {
    id: "attachment:" + attachmentId + "@" + version,
    target: {
      id: "attachment:" + attachmentId + "@" + version,
      kind: "pdf",
      title: "文档.pdf",
      source: "attachment",
      attachmentId,
      version,
    },
    position: options.search ? { search: options.search } : {},
  };
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = [ ...shell.prefs.previewTabs, tab ];
  shell.prefs.activePreviewTabId = tab.id;
  shell.prefs.rightOpen = true;
  const preview = useConversationPreviewStore();
  preview.runtime[tab.id] = {
    phase: "ready",
    envelope: {
      status: "ready",
      kind: "pdf",
      version,
      attachmentId,
      recipe: 1,
      data: { kind: "pdf", pages: 3, title: "示例", encrypted: false, textChars: 12 },
      reason: "",
      truncated: false,
      stats: {},
    },
    problem: null,
    version,
    notice: "",
    updateError: "",
    updateFailures: 0,
    loadedAt: 1,
  };
  return tab;
}

function mountReader(tab: PreviewTabState) {
  return mount(PreviewReader, { props: { tab }, attachTo: document.body });
}

async function waitSearchDebounce(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 260));
  await flushPromises();
  await flushPromises();
}

beforeEach(() => {
  setActivePinia(createPinia());
  pdfAttachmentSeq = 0;
  pageTextImpl = async () => "";
  mocks.fetchBytes.mockReset();
  mocks.fetchBytes.mockResolvedValue(new ArrayBuffer(8));
  mocks.renderPage.mockReset();
  mocks.renderPage.mockResolvedValue({ width: 100, height: 200 });
  mocks.destroy.mockReset();
  mocks.destroy.mockResolvedValue(undefined);
  mocks.pageText.mockReset();
  mocks.pageText.mockImplementation((page: number) => pageTextImpl(page));
  mocks.loadRenderer.mockReset();
  mocks.loadRenderer.mockResolvedValue({
    view: () => ({
      ok: true,
      view: { pages: 3, title: "示例", encrypted: false, textChars: 12, renderable: true, reason: "" },
    }),
    openPdf: async () => ({ pages: 3, renderPage: mocks.renderPage, destroy: mocks.destroy, pageText: mocks.pageText }),
  });
  mocks.getUISettings.mockReset();
  mocks.getUISettings.mockResolvedValue({ conversation_layout: null });
  mocks.saveConversationLayout.mockReset();
  mocks.saveConversationLayout.mockImplementation(async (prefs: unknown) => ({ conversation_layout: prefs }));
});

afterEach(() => {
  useConversationPreviewStore().resetForTests();
  useConversationShellStore().dispose();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("PDF 搜索状态区分", () => {
  it("正常命中：显示「序号 / 命中数」，状态 ready", async () => {
    pageTextImpl = async (page) => ["第一页 needle", "第二页", "第三页 needle"][page - 1] ?? "";
    const wrapper = mountReader(seedPdfTab());
    await flushPromises();
    await wrapper.get('input[type="search"]').setValue("needle");
    await waitSearchDebounce();

    const count = wrapper.get("[data-pv-search-count]");
    expect(count.text()).toContain("/ 2");
    expect(count.attributes("data-pv-search-state")).toBe("ready");
    expect(wrapper.find("[data-pv-search-error]").exists()).toBe(false);
    wrapper.unmount();
  });

  it("正常无结果：搜索完成后才说「无匹配」", async () => {
    pageTextImpl = async () => "这份文档里没有目标词";
    const wrapper = mountReader(seedPdfTab());
    await flushPromises();
    await wrapper.get('input[type="search"]').setValue("needle");
    await waitSearchDebounce();

    const count = wrapper.get("[data-pv-search-count]");
    expect(count.text()).toContain("无匹配");
    expect(count.attributes("data-pv-search-state")).toBe("ready");
    wrapper.unmount();
  });

  it("文字读取失败：说明搜索未完成并提供重试，不能显示成无匹配；重试成功后显示真实命中", async () => {
    pageTextImpl = async () => {
      throw new Error("文字层读取中断");
    };
    const wrapper = mountReader(seedPdfTab());
    await flushPromises();
    await wrapper.get('input[type="search"]').setValue("needle");
    await waitSearchDebounce();

    const count = wrapper.get("[data-pv-search-count]");
    expect(count.text()).toContain("搜索未完成");
    expect(count.text()).not.toContain("无匹配");
    expect(count.attributes("data-pv-search-state")).toBe("error");
    const error = wrapper.get("[data-pv-search-error]");
    expect(error.text()).toContain("搜索未完成");
    expect(error.text()).toContain("文字层读取中断");

    // 重试：这次文字读取成功，只显示真实结果。
    pageTextImpl = async (page) => (page === 1 ? "第一页真的有个 needle" : "其它页没有");
    await wrapper.get("[data-pv-search-retry]").trigger("click");
    await waitSearchDebounce();
    expect(wrapper.get("[data-pv-search-count]").text()).toContain("/ 1");
    expect(wrapper.find("[data-pv-search-error]").exists()).toBe(false);
    wrapper.unmount();
  });

  it("快速修改查询：新查询词下不显示旧结果；旧查询的迟到结果被丢弃", async () => {
    const pending: Array<{ resolve: (text: string) => void; reject: (reason: unknown) => void }> = [];
    pageTextImpl = () =>
      new Promise<string>((resolve, reject) => {
        pending.push({ resolve, reject });
      });
    const wrapper = mountReader(seedPdfTab());
    await flushPromises();

    await wrapper.get('input[type="search"]').setValue("old");
    await waitSearchDebounce();
    expect(pending).toHaveLength(1);

    await wrapper.get('input[type="search"]').setValue("new");
    // 防抖还没触发新搜索：旧命中已隔离，界面只显示「搜索中…」，绝不显示旧结果或无匹配。
    expect(wrapper.get("[data-pv-search-count]").text()).toContain("搜索中");
    await waitSearchDebounce();
    expect(pending).toHaveLength(2);

    // 旧查询的迟到结果：控制器已中止，不应产生任何结果覆盖当前状态。
    pending.shift()!.resolve("old 查询命中");
    await flushPromises();
    expect(wrapper.get("[data-pv-search-count]").text()).toContain("搜索中");

    pending.shift()!.resolve("new 查询命中");
    await flushPromises();
    // 还剩两页要提取
    pending.shift()!.resolve("第二页没有");
    await flushPromises();
    pending.shift()!.resolve("第三页没有");
    await flushPromises();
    await waitSearchDebounce();

    const count = wrapper.get("[data-pv-search-count]");
    expect(count.text()).toContain("/ 1");
    expect(count.attributes("data-pv-search-state")).toBe("ready");
    expect(wrapper.find("[data-pv-search-error]").exists()).toBe(false);
    wrapper.unmount();
  });

  it("切换文件：旧文件的搜索命中/错误不带到新文件", async () => {
    pageTextImpl = async (page) => (page === 1 ? "needle 在这里" : "");
    const first = seedPdfTab();
    const second = seedPdfTab({ attachmentId: "att-pdf-2", version: "v2" });
    const wrapper = mountReader(first);
    await flushPromises();
    await wrapper.get('input[type="search"]').setValue("needle");
    await waitSearchDebounce();
    expect(wrapper.get("[data-pv-search-count]").text()).toContain("/ 1");

    // 切到另一份文件；新文件没有保存搜索词，也不该显示上一份文件的结果。
    await wrapper.setProps({ tab: second });
    await flushPromises();
    await flushPromises();
    expect(wrapper.find("[data-pv-search-count]").exists()).toBe(false);
    expect(wrapper.find("[data-pv-search-error]").exists()).toBe(false);
    wrapper.unmount();
  });
});
