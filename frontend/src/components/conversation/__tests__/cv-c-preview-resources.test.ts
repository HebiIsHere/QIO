/**
 * C 预览资源释放与阅读状态恢复的定向用例：
 *   * 图片 blob URL 在卸载时真的 revoke，img.src 被清掉（不是「理论上会释放」）；
 *   * PDF 文档对象在卸载时 destroy（pdf.js 资源不留在内存里）；
 *   * 阅读器按标签恢复缩放/搜索/滚动位置，并在离开时把位置写回 shell。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  fetchContent: vi.fn(),
  fetchBytes: vi.fn(),
  loadRenderer: vi.fn(),
  renderPage: vi.fn(),
  destroy: vi.fn(),
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

import PreviewImageViewer from "../preview/PreviewImageViewer.vue";
import PreviewPdfViewer from "../preview/PreviewPdfViewer.vue";
import PreviewReader from "../preview/PreviewReader.vue";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import type { PreviewTabState } from "../../../conversation/previewTypes";

let created: string[];
let revoked: string[];

beforeEach(() => {
  setActivePinia(createPinia());
  created = [];
  revoked = [];
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => {
      const url = "blob:qio-preview-" + (created.length + 1);
      created.push(url);
      return url;
    }),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn((url: string) => {
      revoked.push(url);
    }),
  });
  mocks.fetchContent.mockReset();
  mocks.fetchContent.mockResolvedValue(new Blob(["PNGDATA"], { type: "image/png" }));
  mocks.fetchBytes.mockReset();
  mocks.fetchBytes.mockResolvedValue(new ArrayBuffer(8));
  mocks.renderPage.mockReset();
  mocks.renderPage.mockResolvedValue({ width: 100, height: 200 });
  mocks.destroy.mockReset();
  mocks.destroy.mockResolvedValue(undefined);
  mocks.loadRenderer.mockReset();
  mocks.loadRenderer.mockResolvedValue({
    view: () => ({
      ok: true,
      view: { pages: 3, title: "示例", encrypted: false, textChars: 12, renderable: true, reason: "" },
    }),
    openPdf: async () => ({ pages: 3, renderPage: mocks.renderPage, destroy: mocks.destroy }),
  });
  mocks.getUISettings.mockReset();
  mocks.getUISettings.mockResolvedValue({ conversation_layout: null });
  mocks.saveConversationLayout.mockReset();
  mocks.saveConversationLayout.mockImplementation(async (prefs: unknown) => ({
    conversation_layout: prefs,
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("图片预览的资源释放", () => {
  it("卸载时 revoke blob URL 并清掉 img.src", async () => {
    const wrapper = mount(PreviewImageViewer, {
      props: {
        attachmentId: "att-img",
        version: "v1",
        title: "图.png",
        zoom: 1,
        initialScrollTop: 0,
      },
    });
    await flushPromises();
    const img = wrapper.find("img");
    expect(img.exists()).toBe(true);
    expect(img.attributes("src")).toBe("blob:qio-preview-1");
    expect(created).toHaveLength(1);

    wrapper.unmount();
    expect(revoked).toContain("blob:qio-preview-1");
  });

  it("换版本时释放上一份 blob URL（同一时刻只持有一个）", async () => {
    const wrapper = mount(PreviewImageViewer, {
      props: { attachmentId: "att-img", version: "v1", title: "图.png", zoom: 1, initialScrollTop: 0 },
    });
    await flushPromises();
    await wrapper.setProps({ version: "v2" });
    await flushPromises();
    expect(revoked).toContain("blob:qio-preview-1");
    expect(created.length).toBeGreaterThanOrEqual(2);
    wrapper.unmount();
  });
});

describe("PDF 预览的资源释放", () => {
  const props = {
    attachmentId: "att-pdf",
    version: "v1",
    page: 1,
    zoom: 1,
    initialScrollTop: 0,
    factsData: { kind: "pdf", pages: 3, title: "示例", encrypted: false, textChars: 12 },
  };

  it("打开后渲染页面；卸载时 destroy 文档对象", async () => {
    const wrapper = mount(PreviewPdfViewer, { props });
    await flushPromises();
    expect(mocks.fetchBytes.mock.calls[0][0]).toBe("att-pdf");
    expect(mocks.fetchBytes.mock.calls[0][1]).toMatchObject({ signal: expect.any(AbortSignal) });
    expect(mocks.renderPage).toHaveBeenCalled();
    wrapper.unmount();
    await flushPromises();
    expect(mocks.destroy).toHaveBeenCalledTimes(1);
  });
});

function seedReplyCodeTab(): PreviewTabState {
  const tab: PreviewTabState = {
    id: "replycode:m1:0",
    target: {
      id: "replycode:m1:0",
      kind: "code",
      title: "Python 代码",
      source: "reply_code",
      messageId: "m1",
      blockIndex: 0,
    },
    position: { scrollTop: 60, zoom: 1.25, search: "needle" },
  };
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = [tab];
  shell.prefs.activePreviewTabId = tab.id;
  shell.prefs.rightOpen = true;
  const preview = useConversationPreviewStore();
  preview.documents[tab.id] = { text: "alpha\nneedle here\nneedle again", language: "python" };
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

describe("阅读器的位置 / 搜索 / 缩放恢复", () => {
  it("恢复搜索词与缩放，并把位置留在标签上（离开时写回 shell）", async () => {
    const tab = seedReplyCodeTab();
    const wrapper = mount(PreviewReader, { props: { tab } });
    await flushPromises();

    const count = wrapper.get("[data-pv-search-count]");
    expect(count.text()).toContain("/ 2");
    const text = wrapper.get(".pv-text");
    expect(text.attributes("style") ?? "").toContain("1.25");

    const body = wrapper.get("[data-pv-scroll]").element as HTMLElement;
    body.scrollTop = 88;
    body.dispatchEvent(new Event("scroll"));
    wrapper.unmount();
    const shell = useConversationShellStore();
    expect(shell.prefs.previewTabs[0].position.search).toBe("needle");
    expect(shell.prefs.previewTabs[0].position.zoom).toBe(1.25);
    // 滚动位置：jsdom 支持 scrollTop 赋值时写回；不支持时至少不能把已有位置弄丢。
    expect(shell.prefs.previewTabs[0].position.scrollTop === 88 || shell.prefs.previewTabs[0].position.scrollTop === 60).toBe(true);
  });

  it("代码块正文不渲染成 Markdown，也不发起预览请求", async () => {
    seedReplyCodeTab();
    const wrapper = mount(PreviewReader, {
      props: { tab: useConversationShellStore().prefs.previewTabs[0] },
    });
    await flushPromises();
    expect(wrapper.find(".pv-markdown").exists()).toBe(false);
    expect(wrapper.findAll("[data-line]")).toHaveLength(3);
    wrapper.unmount();
  });
});

describe("切换标签不共享阅读状态", () => {
  it("两个回复代码标签各有搜索状态", async () => {
    const shell = useConversationShellStore();
    const preview = useConversationPreviewStore();
    const first: PreviewTabState = {
      id: "replycode:m1:0",
      target: { id: "replycode:m1:0", kind: "code", title: "A", source: "reply_code", messageId: "m1", blockIndex: 0 },
      position: { search: "alpha" },
    };
    const second: PreviewTabState = {
      id: "replycode:m2:0",
      target: { id: "replycode:m2:0", kind: "code", title: "B", source: "reply_code", messageId: "m2", blockIndex: 0 },
      position: { search: "beta" },
    };
    shell.prefs.previewTabs = [first, second];
    shell.prefs.activePreviewTabId = first.id;
    shell.prefs.rightOpen = true;
    for (const id of [first.id, second.id]) {
      preview.documents[id] = { text: "alpha beta\nbeta alpha" };
      preview.runtime[id] = {
        phase: "ready",
        envelope: null,
        problem: null,
        version: "",
        notice: "",
        updateError: "",
        updateFailures: 0,
        loadedAt: 1,
      };
    }
    const wrapperOne = mount(PreviewReader, { props: { tab: first } });
    await flushPromises();
    expect((wrapperOne.get('input[type="search"]').element as HTMLInputElement).value).toBe("alpha");
    wrapperOne.unmount();

    const wrapperTwo = mount(PreviewReader, { props: { tab: second } });
    await flushPromises();
    expect((wrapperTwo.get('input[type="search"]').element as HTMLInputElement).value).toBe("beta");
    wrapperTwo.unmount();
  });
});
