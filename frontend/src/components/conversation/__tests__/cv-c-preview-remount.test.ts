/**
 * 返修轮 · 代码预览「从设置返回后卡死」定向用例：
 *
 * 稳定复现：对话里生成代码 → 打开预览（正文与搜索可用）→ 关闭标签 → 进设置 →
 * 用返回键回到对话 → 重新打开同一段代码 → 阅读器停在「正在读取这个文件…」，
 * 刷新后恢复。
 *
 * 根因：ConversationView 卸载时调 preview.dispose()，它把 store 的 `_disposed` 与
 * 模块级 `disposed` 都置 true，而 ensureLoaded / loadAttachment / checkForUpdate 都以
 * 它们为闸门；重新挂载后没有任何地方复位 —— 运行时永远停在 loading。
 *
 * 本文件覆盖：
 *   1. 打开 → 卸载（dispose）→ 重新挂载（resume）→ 再次打开同一代码块：进入 ready 且正文可读；
 *   2. 卸载期间在途请求被中止；重新挂载后新请求能发出（不是被闸门吞掉）；
 *   3. 卸载前留下的 loading 残影不会让重新挂载后的读取永远停住；
 *   4. 临时消息 id → 持久化消息 id 的交接：同一段代码不重复开标签；
 *   5. 失败时给可理解的原因与可用动作，不无限读取。
 *
 * 运行：cd frontend; npx --no-install vitest run src/components/conversation/__tests__/cv-c-preview-remount.test.ts
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  fetchPreview: vi.fn(),
  getUISettings: vi.fn(),
  saveConversationLayout: vi.fn(),
  fetchTaskFeed: vi.fn(),
  fetchTaskDetail: vi.fn(),
  acknowledgeTask: vi.fn(),
  requestTaskCancel: vi.fn(),
}));

vi.mock("../../../services/taskFeedApi", () => ({
  fetchTaskFeed: mocks.fetchTaskFeed,
  fetchTaskDetail: mocks.fetchTaskDetail,
  acknowledgeTask: mocks.acknowledgeTask,
  requestTaskCancel: mocks.requestTaskCancel,
}));

vi.mock("../../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/attachments")>();
  return {
    ...actual,
    fetchAttachmentPreview: mocks.fetchPreview,
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

import PreviewPanel from "../PreviewPanel.vue";
import ConversationView from "../../../views/ConversationView.vue";
import { AttachmentRequestError, type PreviewEnvelope } from "../../../services/attachments";
import { api } from "../../../services/api";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import { useConversationTasksStore } from "../../../stores/conversationTasks";
import type { TaskCounts, TaskFeedPage } from "../../../conversation/contracts";

const ZERO_COUNTS: TaskCounts = {
  queued: 0,
  running: 0,
  waitingApproval: 0,
  waitingUser: 0,
  completed: 0,
  partial: 0,
  failed: 0,
  cancelled: 0,
  budgetExhausted: 0,
  unknown: 0,
};

function taskPage(): TaskFeedPage {
  return {
    items: [],
    nextCursor: null,
    counts: ZERO_COUNTS,
    serverTime: "2026-11-14T00:00:00.000Z",
    instanceId: "inst-1",
  };
}

/** 对话页的真实子组件太多，这里只替身与预览无关的部分；PreviewPanel 用真实组件。 */
const viewStubs = {
  ChatColumn: { name: "ChatColumn", template: '<div class="stub-chat" />' },
  UnifiedApprovalLayer: { name: "UnifiedApprovalLayer", template: "<div />" },
  PlanetDock: { name: "PlanetDock", emits: ["open"], template: '<button class="stub-dock" type="button" />' },
  PlanetView: { name: "PlanetView", template: "<div />" },
  RecoveryInbox: { name: "RecoveryInbox", template: "<div />" },
};

function envelope(overrides: Partial<PreviewEnvelope> = {}): PreviewEnvelope {
  return {
    status: "ready",
    kind: "code",
    version: "v1",
    attachmentId: "att1",
    recipe: 1,
    data: { kind: "code", lines: ["alpha", "beta"], language: "python", encoding: "utf-8", truncated: false },
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
  mocks.fetchPreview.mockResolvedValue(envelope());
  mocks.getUISettings.mockReset();
  mocks.getUISettings.mockResolvedValue({ conversation_layout: null });
  mocks.saveConversationLayout.mockReset();
  mocks.saveConversationLayout.mockImplementation(async (prefs: unknown) => ({
    conversation_layout: prefs,
  }));
  mocks.fetchTaskFeed.mockReset();
  mocks.fetchTaskFeed.mockResolvedValue(taskPage());
  vi.spyOn(api, "getSessionContext").mockResolvedValue({
    topic_id: "t1",
    topic_name: "预览返修",
    anchor_fragment: null,
    messages: [],
    has_more: false,
    next_before: null,
    tool_records: [],
    turn_facts: [],
  } as never);
  preview = useConversationPreviewStore();
  shell = useConversationShellStore();
});

afterEach(() => {
  useConversationTasksStore().dispose();
  preview.resetForTests();
  shell.dispose();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("卸载 → 重新挂载后预览恢复可用", () => {
  it("打开 → dispose → resume → 再次打开同一代码块：运行时进入 ready，正文可读", async () => {
    // 1) 第一次打开：正文在内存里，运行时 ready。
    preview.openReplyCode({ messageId: "m1", blockIndex: 0, text: "print(1)\nprint(2)", language: "python" });
    const tabId = "replycode:m1:0";
    expect(preview.runtimeOf(tabId)?.phase).toBe("ready");
    expect(preview.documentOf(tabId)?.text).toContain("print(1)");

    // 2) 关闭标签（复现步骤：先关掉这个标签）。
    preview.closeTab(tabId);
    expect(shell.prefs.previewTabs).toHaveLength(0);

    // 3) 进入设置页 = 对话页卸载。
    preview.dispose();

    // 4) 返回对话 = 重新挂载。
    preview.resume();

    // 5) 再次打开同一段代码：必须重新进入可读态，而不是停在 loading。
    const outcome = preview.openReplyCode({
      messageId: "m1",
      blockIndex: 0,
      text: "print(1)\nprint(2)",
      language: "python",
    });
    expect(outcome).toBe("opened");
    const runtime = preview.runtimeOf(tabId);
    expect(runtime?.phase, "重新挂载后必须能读到正文，不能停在 loading").toBe("ready");
    expect(preview.documentOf(tabId)?.text).toContain("print(2)");
  });

  it("重新挂载后阅读器真的渲染出正文（不是只有 store 状态对）", async () => {
    preview.openReplyCode({ messageId: "m1", blockIndex: 0, text: "alpha\nbeta", language: "python" });
    preview.closeTab("replycode:m1:0");
    preview.dispose();
    preview.resume();
    preview.openReplyCode({ messageId: "m1", blockIndex: 0, text: "alpha\nbeta", language: "python" });

    const wrapper = mount(PreviewPanel);
    await flushPromises();
    expect(wrapper.find("[data-pv-loading]").exists()).toBe(false);
    expect(wrapper.findAll("[data-line]")).toHaveLength(2);
    expect(wrapper.text()).toContain("alpha");
    wrapper.unmount();
  });

  it("dispose 的清理语义不退化：在途请求被中止，计时器与守卫都清掉", async () => {
    const pending = deferred<PreviewEnvelope>();
    mocks.fetchPreview.mockImplementationOnce(() => pending.promise);
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    expect(preview.runtimeOf(tabId)?.phase).toBe("loading");

    preview.dispose();
    // 在途请求被 abort：迟到的成功结果不得把运行时写成 ready。
    pending.resolve(envelope());
    await flushPromises();
    expect(preview.runtimeOf(tabId)?.phase, "卸载后迟到的响应不能落地").toBe("loading");
  });

  it("卸载期间在途请求被中止；重新挂载后新请求能发出（不被闸门吞掉）", async () => {
    const first = deferred<PreviewEnvelope>();
    mocks.fetchPreview.mockImplementationOnce(() => first.promise);
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    expect(mocks.fetchPreview).toHaveBeenCalledTimes(1);

    // 卸载：在途请求中止，运行时停在 loading（残影）。
    preview.dispose();
    first.reject(new Error("aborted"));
    await flushPromises();

    // 重新挂载：当前标签要能重新发起读取。
    mocks.fetchPreview.mockResolvedValueOnce(envelope());
    preview.resume();
    await flushPromises();
    expect(mocks.fetchPreview, "重新挂载后必须重新发出请求").toHaveBeenCalledTimes(2);
    // 读到真实版本后标签身份会更新为 attachment:att1@v1（版本变化复用同一标签）。
    const tabId = shell.prefs.previewTabs[0].id;
    expect(preview.runtimeOf(tabId)?.phase).toBe("ready");
  });

  it("卸载前留下的 loading 残影不会让重新挂载后的读取永远停住", async () => {
    // 直接造一个「停在 loading 但没有在途请求」的残影（旧实现里这就是永久卡死）。
    const tab = {
      id: "attachment:att1@v1",
      target: {
        id: "attachment:att1@v1",
        kind: "image" as const,
        title: "图.png",
        source: "attachment" as const,
        attachmentId: "att1",
        version: "v1",
      },
      position: {},
    };
    shell.prefs.previewTabs = [tab];
    shell.prefs.activePreviewTabId = tab.id;
    shell.prefs.rightOpen = true;
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

    preview.dispose();
    preview.resume();
    await flushPromises();

    expect(mocks.fetchPreview).toHaveBeenCalledTimes(1);
    expect(preview.runtimeOf(tab.id)?.phase).toBe("ready");
  });

  it("resume 是幂等的：重复调用不会重复发请求，也不清掉已有内容", async () => {
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    expect(preview.runtimeOf(tabId)?.phase).toBe("ready");

    preview.resume();
    preview.resume();
    await flushPromises();
    expect(mocks.fetchPreview).toHaveBeenCalledTimes(1);
    expect(preview.runtimeOf(tabId)?.phase).toBe("ready");
  });
});

describe("对话页真实挂载 / 卸载 / 重新挂载（设置页往返）", () => {
  function mountView() {
    return mount(ConversationView, {
      attachTo: document.body,
      global: { stubs: viewStubs },
    });
  }

  it("打开预览 → 卸载（进设置）→ 重新挂载（返回对话）→ 再次打开同一代码块：正文可读", async () => {
    const first = mountView();
    await flushPromises();

    // 第一次打开：正文与搜索框可用。
    preview.openReplyCode({ messageId: "m1", blockIndex: 0, text: "alpha\nbeta", language: "python" });
    await flushPromises();
    expect(first.find("[data-pv-loading]").exists()).toBe(false);
    expect(first.findAll("[data-line]")).toHaveLength(2);

    // 关闭这个标签（复现步骤）。
    preview.closeTab("replycode:m1:0");
    await flushPromises();

    // 进设置页 = 对话页卸载。
    first.unmount();
    await flushPromises();

    // 返回对话 = 重新挂载。
    const second = mountView();
    await flushPromises();

    // 重新打开同一段代码：必须进入可读态，而不是停在「正在读取这个文件…」。
    preview.openReplyCode({ messageId: "m1", blockIndex: 0, text: "alpha\nbeta", language: "python" });
    await flushPromises();
    expect(second.find("[data-pv-loading]").exists(), "不能停在加载态").toBe(false);
    expect(second.findAll("[data-line]")).toHaveLength(2);
    expect(second.text()).toContain("alpha");
    expect(preview.runtimeOf("replycode:m1:0")?.phase).toBe("ready");
    second.unmount();
  });

  it("卸载时在途请求被中止；重新挂载后当前标签的新请求能发出", async () => {
    const pending = deferred<PreviewEnvelope>();
    mocks.fetchPreview.mockImplementationOnce(() => pending.promise);

    const first = mountView();
    await flushPromises();
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    expect(mocks.fetchPreview).toHaveBeenCalledTimes(1);

    first.unmount();
    await flushPromises();
    pending.reject(new Error("aborted"));
    await flushPromises();

    // 重新挂载：预览栏仍展开（标签还在），当前标签要重新发起读取。
    mocks.fetchPreview.mockResolvedValueOnce(envelope());
    const second = mountView();
    await flushPromises();
    expect(mocks.fetchPreview, "重新挂载后必须重新发出请求").toHaveBeenCalledTimes(2);
    const tabId = shell.prefs.previewTabs[0].id;
    expect(preview.runtimeOf(tabId)?.phase).toBe("ready");
    second.unmount();
  });

  it("预览栏收起时重新挂载不发起读取（没有阅读器在等）", async () => {
    const first = mountView();
    await flushPromises();
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    expect(mocks.fetchPreview).toHaveBeenCalledTimes(1);

    preview.collapsePanel();
    first.unmount();
    await flushPromises();

    const second = mountView();
    await flushPromises();
    expect(mocks.fetchPreview, "收起状态下不制造无谓读取").toHaveBeenCalledTimes(1);
    second.unmount();
  });
});

describe("临时消息身份 → 持久化消息身份的交接", () => {
  it("同一条消息换 id：标签跟着迁移，不重复开标签，正文与位置保留", () => {
    preview.openReplyCode({ messageId: "local_1_1", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:local_1_1:0", { scrollTop: 42, search: "print" });
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:local_1_1:0"]);

    const moved = preview.reconcileReplyCodeIdentities([
      { id: "msg_abc", role: "assistant", content: "说明\n\n```python\nprint(1)\n```" },
    ]);

    expect(moved).toBe(1);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
    expect(shell.prefs.activePreviewTabId).toBe("replycode:msg_abc:0");
    expect(preview.documentOf("replycode:msg_abc:0")?.text).toBe("print(1)");
    expect(preview.positionOf("replycode:msg_abc:0")).toMatchObject({ scrollTop: 42, search: "print" });
    expect(preview.runtimeOf("replycode:msg_abc:0")?.phase).toBe("ready");
  });

  it("正文对不上就不猜：旧标签原样保留，仍然可读", () => {
    preview.openReplyCode({ messageId: "local_1_1", blockIndex: 0, text: "print(1)", language: "python" });
    const moved = preview.reconcileReplyCodeIdentities([
      { id: "msg_abc", role: "assistant", content: "```python\nprint(999)\n```" },
    ]);
    expect(moved).toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:local_1_1:0"]);
    expect(preview.runtimeOf("replycode:local_1_1:0")?.phase).toBe("ready");
  });

  it("两段一模一样的代码（候选不唯一）不迁移，避免把标签挂到错的消息上", () => {
    preview.openReplyCode({ messageId: "local_1_1", blockIndex: 0, text: "print(1)", language: "python" });
    const moved = preview.reconcileReplyCodeIdentities([
      { id: "msg_a", role: "assistant", content: "```python\nprint(1)\n```" },
      { id: "msg_b", role: "assistant", content: "```python\nprint(1)\n```" },
    ]);
    expect(moved).toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:local_1_1:0"]);
  });

  it("持久化身份的标签已经存在时合并成它，不留下两份同一段代码", () => {
    preview.openReplyCode({ messageId: "local_1_1", blockIndex: 0, text: "print(1)", language: "python" });
    preview.openReplyCode({ messageId: "msg_abc", blockIndex: 0, text: "print(1)", language: "python" });
    expect(shell.prefs.previewTabs).toHaveLength(2);

    const moved = preview.reconcileReplyCodeIdentities([
      { id: "msg_abc", role: "assistant", content: "```python\nprint(1)\n```" },
    ]);

    expect(moved).toBe(1);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
    expect(preview.runtimeOf("replycode:local_1_1:0")).toBeNull();
  });

  it("消息 id 还在列表里时什么都不做（不误迁移）", () => {
    preview.openReplyCode({ messageId: "msg_abc", blockIndex: 0, text: "print(1)", language: "python" });
    const moved = preview.reconcileReplyCodeIdentities([
      { id: "msg_abc", role: "assistant", content: "```python\nprint(1)\n```" },
    ]);
    expect(moved).toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
  });
});

describe("失败时给可理解的原因与可用动作，不无限读取", () => {
  it("附件读取失败：进入 problem 态并给出原因，不是一直 loading", async () => {
    mocks.fetchPreview.mockRejectedValueOnce(
      new AttachmentRequestError(404, "/preview", "没有这个附件"),
    );
    preview.openAttachment({ attachmentId: "gone", title: "x.pdf" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    const runtime = preview.runtimeOf(tabId);
    expect(runtime?.phase).toBe("problem");
    expect(runtime?.problem?.title).toContain("不存在");
    expect(runtime?.problem?.retryable).toBe(false);
  });

  it("重新挂载后失败态不会被当成「还没读」而无限重发", async () => {
    mocks.fetchPreview.mockRejectedValueOnce(
      new AttachmentRequestError(500, "/preview", "后端暂时不可用"),
    );
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    expect(preview.runtimeOf(tabId)?.phase).toBe("problem");
    expect(mocks.fetchPreview).toHaveBeenCalledTimes(1);

    preview.dispose();
    preview.resume();
    await flushPromises();
    // 失败态是真实结论，不是残影：重新挂载不自动重发（用户点「重试」才重发）。
    expect(mocks.fetchPreview).toHaveBeenCalledTimes(1);
    expect(preview.runtimeOf(tabId)?.phase).toBe("problem");
  });

  it("用户点「重试」才重新读取，成功后进入 ready", async () => {
    mocks.fetchPreview.mockRejectedValueOnce(
      new AttachmentRequestError(500, "/preview", "后端暂时不可用"),
    );
    preview.openAttachment({ attachmentId: "att1", title: "图.png" });
    await flushPromises();
    const tabId = shell.prefs.previewTabs[0].id;
    expect(preview.runtimeOf(tabId)?.phase).toBe("problem");

    mocks.fetchPreview.mockResolvedValueOnce(envelope());
    preview.retryTab(tabId);
    await flushPromises();
    // 重试读到真实版本后标签身份更新为 attachment:att1@v1。
    expect(preview.runtimeOf(shell.prefs.previewTabs[0].id)?.phase).toBe("ready");
  });
});
