/**
 * C 预览 store 定向用例：打开/去重/关闭/收起/全窗口、版本更新与竞争、失败不覆盖旧内容。
 *
 * 模拟边界：只替换「网络」两个边界（services/attachments 的预览/打开/下载与 services/api 的布局保存），
 * 其余全部真实实现（shell store、preview 纯逻辑、标签持久化路径）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";

const mocks = vi.hoisted(() => ({
  fetchPreview: vi.fn(),
  openAttachment: vi.fn(),
  downloadAttachment: vi.fn(),
  saveConversationLayout: vi.fn(),
  getUISettings: vi.fn(),
}));

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    fetchAttachmentPreview: mocks.fetchPreview,
    openAttachment: mocks.openAttachment,
    downloadAttachment: mocks.downloadAttachment,
  };
});

vi.mock("../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getUISettings: mocks.getUISettings,
      saveConversationLayout: mocks.saveConversationLayout,
    },
  };
});

import { AttachmentRequestError, type PreviewEnvelope } from "../../services/attachments";
import { useConversationShellStore } from "../../stores/conversationShell";
import { useConversationPreviewStore } from "../../stores/conversationPreview";

function envelope(overrides: Partial<PreviewEnvelope> = {}): PreviewEnvelope {
  return {
    status: "ready",
    kind: "image",
    version: "v1",
    attachmentId: "att1",
    recipe: 1,
    data: { kind: "image", width: 10, height: 10, bytes: 100 },
    reason: "",
    truncated: false,
    stats: {},
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let preview: ReturnType<typeof useConversationPreviewStore>;
let shell: ReturnType<typeof useConversationShellStore>;

beforeEach(() => {
  setActivePinia(createPinia());
  mocks.fetchPreview.mockReset();
  mocks.openAttachment.mockReset();
  mocks.downloadAttachment.mockReset();
  mocks.saveConversationLayout.mockReset();
  mocks.getUISettings.mockReset();
  mocks.fetchPreview.mockResolvedValue(envelope());
  mocks.saveConversationLayout.mockImplementation(async (prefs: unknown) => ({
    conversation_layout: prefs,
  }));
  mocks.getUISettings.mockResolvedValue({ conversation_layout: null });
  preview = useConversationPreviewStore();
  shell = useConversationShellStore();
});

afterEach(() => {
  preview.resetForTests();
  shell.dispose();
});

describe("打开与标签", () => {
  it("打开附件才展开预览栏，并把标签写进 shell（不写正文）", async () => {
    expect(shell.prefs.rightOpen).toBe(false);
    const outcome = preview.openAttachment({ attachmentId: "att1", title: "图.png", size: 100 });
    expect(outcome).toBe("opened");
    expect(shell.prefs.rightOpen).toBe(true);
    await flushPromises();
    expect(shell.prefs.previewTabs).toHaveLength(1);
    expect(shell.prefs.activePreviewTabId).toBe("attachment:att1@v1");
    const runtime = preview.runtimeOf("attachment:att1@v1");
    expect(runtime?.phase).toBe("ready");
    expect(runtime?.version).toBe("v1");
    // 正文不在偏好里（契约：previewTabs 只存身份与轻量位置）。
    expect(JSON.stringify(shell.prefs.previewTabs)).not.toContain('"data"');
    expect(JSON.stringify(shell.prefs.previewTabs)).not.toContain('"text"');
  });

  it("同一文件再次打开定位已有标签；同名不同附件是两个标签", async () => {
    preview.openAttachment({ attachmentId: "attA", title: "同名.pdf" });
    await flushPromises();
    preview.openAttachment({ attachmentId: "attA", title: "同名.pdf" });
    await flushPromises();
    expect(shell.prefs.previewTabs).toHaveLength(1);
    preview.openAttachment({ attachmentId: "attB", title: "同名.pdf" });
    await flushPromises();
    expect(shell.prefs.previewTabs).toHaveLength(2);
    expect(shell.prefs.activePreviewTabId).toBe(shell.prefs.previewTabs[1].id);
  });

  it("回复代码块：正文只在内存里，切换标签后仍能读；关闭后释放", async () => {
    preview.openReplyCode({ messageId: "m1", blockIndex: 0, text: "print(1)\nprint(2)", language: "python" });
    const tabId = "replycode:m1:0";
    expect(shell.prefs.previewTabs[0].id).toBe(tabId);
    expect(preview.runtimeOf(tabId)?.phase).toBe("ready");
    expect(preview.documentOf(tabId)?.text).toContain("print(1)");
    preview.closeTab(tabId);
    expect(shell.prefs.previewTabs).toHaveLength(0);
    expect(shell.prefs.rightOpen).toBe(false);
    expect(preview.runtimeOf(tabId)).toBeNull();
    expect(preview.documentOf(tabId)).toBeNull();
  });

  it("关闭一个标签只关这一个，激活邻居；另一个标签与位置保留", async () => {
    preview.openReplyCode({ messageId: "m1", blockIndex: 0, text: "a" });
    preview.openReplyCode({ messageId: "m2", blockIndex: 0, text: "b" });
    preview.savePosition("replycode:m1:0", { scrollTop: 42 });
    preview.closeTab("replycode:m2:0");
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:m1:0"]);
    expect(shell.prefs.activePreviewTabId).toBe("replycode:m1:0");
    expect(preview.positionOf("replycode:m1:0").scrollTop).toBe(42);
    expect(shell.prefs.rightOpen).toBe(true);
  });

  it("音视频不发起内嵌预览请求，但保留打开/下载入口", async () => {
    preview.openAttachment({ attachmentId: "att-sound", title: "录音.mp3" });
    await flushPromises();
    expect(mocks.fetchPreview).not.toHaveBeenCalled();
    const tab = shell.prefs.previewTabs[0];
    const runtime = preview.runtimeOf(tab.id);
    expect(runtime?.phase).toBe("problem");
    expect(runtime?.problem?.kind).toBe("media");
    expect(runtime?.problem?.openable).toBe(true);
  });
});

describe("收起 / 全窗口 / 位置", () => {
  it("收起整个栏保留标签与位置；再次打开继续（不新建标签）", async () => {
    mocks.fetchPreview.mockResolvedValueOnce(
      envelope({ kind: "pdf", data: { kind: "pdf", pages: 5, title: "报告", encrypted: false, textChars: 10 } }),
    );
    preview.openAttachment({ attachmentId: "att1", title: "报告.pdf" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    preview.savePosition(tabId, { page: 5, zoom: 1.5, scrollTop: 30 });
    preview.collapsePanel();
    expect(shell.prefs.rightOpen).toBe(false);
    expect(shell.prefs.previewTabs).toHaveLength(1);
    expect(preview.positionOf(tabId)).toMatchObject({ page: 5, zoom: 1.5, scrollTop: 30 });
    preview.reopenPanel();
    expect(shell.prefs.rightOpen).toBe(true);
    expect(shell.prefs.previewTabs).toHaveLength(1);
    expect(shell.prefs.activePreviewTabId).toBe(tabId);
  });

  it("全窗口切换只改变外壳状态，标签与位置不动", async () => {
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    preview.savePosition(tabId, { zoom: 2 });
    preview.openFull();
    expect(shell.prefs.previewFull).toBe(true);
    expect(shell.prefs.previewTabs).toHaveLength(1);
    expect(preview.positionOf(tabId).zoom).toBe(2);
    preview.closeFull();
    expect(shell.prefs.previewFull).toBe(false);
    expect(shell.prefs.previewTabs[0].id).toBe(tabId);
  });
});

describe("文件更新：真实版本比较", () => {
  it("版本变了原地更新并提示；不切换标签、不重新打开已收起的栏、位置保留", async () => {
    mocks.fetchPreview.mockResolvedValueOnce(
      envelope({ kind: "docx", data: { kind: "docx", blocks: [], tables: [], truncated: false } }),
    );
    preview.openAttachment({ attachmentId: "att1", title: "调研.docx" });
    preview.openReplyCode({ messageId: "m9", blockIndex: 0, text: "code" });
    await flushPromises();
    const fileTabId = "attachment:att1@v1";
    preview.savePosition(fileTabId, { scrollTop: 120, zoom: 1.25 });
    preview.collapsePanel();
    expect(shell.prefs.activePreviewTabId).toBe("replycode:m9:0");

    mocks.fetchPreview.mockResolvedValueOnce(
      envelope({ version: "v2", kind: "docx", data: { kind: "docx", blocks: [], tables: [] } }),
    );
    const result = await preview.checkForUpdate(fileTabId);
    expect(result).toBe("updated");

    const updated = shell.prefs.previewTabs.find((tab) => tab.target.attachmentId === "att1");
    expect(updated?.id).toBe("attachment:att1@v2");
    expect(updated?.target.version).toBe("v2");
    expect(updated?.position).toMatchObject({ scrollTop: 120, zoom: 1.25 });
    expect(shell.prefs.activePreviewTabId).toBe("replycode:m9:0");
    expect(shell.prefs.rightOpen).toBe(false);
    expect(preview.runtimeOf("attachment:att1@v2")?.notice).toContain("文件已更新");
  });

  it("版本没变时不做任何事（不误报「已更新」）", async () => {
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    mocks.fetchPreview.mockResolvedValueOnce(envelope({ version: "v1" }));
    const result = await preview.checkForUpdate(tabId);
    expect(result).toBe("unchanged");
    expect(preview.runtimeOf(tabId)?.notice).toBe("");
  });

  it("更新检查失败：保留上次内容并明确说明看到的是旧内容", async () => {
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    mocks.fetchPreview.mockRejectedValueOnce(new AttachmentRequestError(500, "/preview", "后端暂时不可用"));
    const result = await preview.checkForUpdate(tabId);
    expect(result).toBe("failed");
    const runtime = preview.runtimeOf(tabId);
    expect(runtime?.phase).toBe("ready");
    expect(runtime?.version).toBe("v1");
    expect(runtime?.updateError).toContain("当前显示的是上次读取的内容");
  });

  it("旧请求不覆盖新版本：先发的旧版本结果迟到时被丢弃", async () => {
    const first = deferred<PreviewEnvelope>();
    const second = deferred<PreviewEnvelope>();
    mocks.fetchPreview
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    preview.retryTab(tabId);
    second.resolve(envelope({ version: "v2" }));
    await flushPromises();
    expect(shell.prefs.previewTabs[0].id).toBe("attachment:att1@v2");
    first.resolve(envelope({ version: "v1" }));
    await flushPromises();
    expect(shell.prefs.previewTabs[0].id).toBe("attachment:att1@v2");
    expect(preview.runtimeOf("attachment:att1@v2")?.version).toBe("v2");
  });
});

describe("失败状态与外部入口", () => {
  it("附件不存在（404）→ 缺失态，不提供假打开入口", async () => {
    mocks.fetchPreview.mockRejectedValueOnce(new AttachmentRequestError(404, "/preview", "没有这个附件"));
    preview.openAttachment({ attachmentId: "gone", title: "x.pdf" });
    await flushPromises();
    const tab = shell.prefs.previewTabs[0];
    const runtime = preview.runtimeOf(tab.id);
    expect(runtime?.phase).toBe("problem");
    expect(runtime?.problem?.kind).toBe("missing");
    expect(runtime?.problem?.openable).toBe(false);
    expect(runtime?.problem?.title).toContain("不存在");
  });

  it("任务成果正文被清理 → 如实说明，不显示成空文件", () => {
    preview.openTaskOutput({
      taskId: "task-1",
      title: "输出",
      unavailableReason: "这份任务输出已被保留策略清理（超过保留天数）",
    });
    const tab = shell.prefs.previewTabs[0];
    const runtime = preview.runtimeOf(tab.id);
    expect(runtime?.phase).toBe("problem");
    expect(runtime?.problem?.kind).toBe("archived");
    expect(runtime?.problem?.detail).toContain("保留策略");
  });

  it("打开/下载走既有附件链路；无真实附件时不给假结果", async () => {
    mocks.openAttachment.mockResolvedValue({ action: "open", note: "已交给系统默认程序打开" });
    mocks.downloadAttachment.mockResolvedValue({ action: "download", note: "已开始下载" });
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tab = shell.prefs.previewTabs[0];
    const opened = await preview.openInSystem(tab.id);
    expect(opened).toMatchObject({ ok: true });
    expect(mocks.openAttachment).toHaveBeenCalled();
    const downloaded = await preview.downloadCopy(tab.id);
    expect(downloaded).toMatchObject({ ok: true });
    preview.openReplyCode({ messageId: "m1", blockIndex: 0, text: "x" });
    const replyTab = shell.prefs.previewTabs.find((item) => item.id === "replycode:m1:0");
    const replyOpen = await preview.openInSystem(replyTab!.id);
    expect(replyOpen.ok).toBe(false);
    expect(replyOpen.note).toContain("没有可打开的文件");
  });
});
