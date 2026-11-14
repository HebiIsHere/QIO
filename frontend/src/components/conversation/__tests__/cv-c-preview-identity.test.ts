/**
 * 返修轮 task-6 · 代码预览标签的身份归属（预览b）。
 *
 * 反例（已复现）：`reconcileReplyCodeIdentities()` 以前会给「不在当前消息列表里」的标签
 * 按代码正文找候选 —— 原标签即使已经绑定正式 `msg_` id，只要当前另一条回复有唯一一段
 * 相同代码，标签就会被改绑到那条回复上。切换话题、历史分页变化都会让原消息暂时离开列表。
 *
 * 本文件覆盖两组验收：
 *   1. 不同回复 / 话题包含同一段代码，原消息离开列表后，原标签身份与阅读位置不变；
 *   2. 同一回复从临时 id 转为正式 id：只留一个标签，正文、搜索与滚动可用。
 *
 * 运行：cd frontend; npx --no-install vitest run src/components/conversation/__tests__/cv-c-preview-identity.test.ts
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  fetchPreview: vi.fn(),
  saveConversationLayout: vi.fn(),
  getUISettings: vi.fn(),
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

import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";

/** 一条助手回复（正文里第 index 个围栏代码块就是 code）。 */
function reply(
  id: string,
  code: string,
  turnId: string,
  overrides: Record<string, unknown> = {},
): { id: string; role: string; content: string; turnId: string } {
  return {
    id,
    role: "assistant",
    content: "说明\n\n```python\n" + code + "\n```",
    turnId,
    ...overrides,
  };
}

let preview: ReturnType<typeof useConversationPreviewStore>;
let shell: ReturnType<typeof useConversationShellStore>;

beforeEach(() => {
  setActivePinia(createPinia());
  mocks.fetchPreview.mockReset();
  mocks.saveConversationLayout.mockReset();
  mocks.getUISettings.mockReset();
  mocks.saveConversationLayout.mockImplementation(async (prefs: unknown) => ({
    conversation_layout: prefs,
  }));
  mocks.getUISettings.mockResolvedValue({ conversation_layout: null });
  preview = useConversationPreviewStore();
  shell = useConversationShellStore();
  // 模块级观察表是跨实例的：每个用例从干净状态开始。
  preview.resetForTests();
});

afterEach(() => {
  preview.resetForTests();
  shell.dispose();
});

describe("第一组：不同回复 / 话题包含同一段代码，原标签不被改绑", () => {
  it("已绑定正式消息 id 的标签：原消息离开列表后仍指向原消息，正文与阅读位置不变", () => {
    preview.openReplyCode({ messageId: "msg_A", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:msg_A:0", { scrollTop: 120, search: "print" });

    // 切换话题 / 历史分页变化：原消息暂时不在当前列表里，另一条回复有同一段代码。
    const moved = preview.reconcileReplyCodeIdentities([reply("msg_B", "print(1)", "turn_2")]);

    expect(moved, "正式 id 的标签永远不迁移").toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_A:0"]);
    expect(shell.prefs.activePreviewTabId).toBe("replycode:msg_A:0");
    expect(preview.positionOf("replycode:msg_A:0")).toMatchObject({ scrollTop: 120, search: "print" });
    expect(preview.documentOf("replycode:msg_A:0")?.text).toBe("print(1)");
    expect(preview.runtimeOf("replycode:msg_A:0")?.phase).toBe("ready");
  });

  it("正式 id 的标签：同一段代码在另一条回复里出现多次也不改绑", () => {
    preview.openReplyCode({ messageId: "msg_A", blockIndex: 0, text: "print(1)", language: "python" });
    const moved = preview.reconcileReplyCodeIdentities([
      reply("msg_B", "print(1)", "turn_2"),
      reply("msg_C", "print(1)", "turn_3"),
    ]);
    expect(moved).toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_A:0"]);
  });

  it("临时标签：从未观察到这条消息的轮次身份时不猜（不迁移、不关闭，正文仍可读）", () => {
    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:local_9_1:0", { scrollTop: 33 });

    const moved = preview.reconcileReplyCodeIdentities([reply("msg_B", "print(1)", "turn_2")]);

    expect(moved, "确认不了身份就不迁移").toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:local_9_1:0"]);
    expect(preview.positionOf("replycode:local_9_1:0")).toMatchObject({ scrollTop: 33 });
    expect(preview.documentOf("replycode:local_9_1:0")?.text).toBe("print(1)");
    expect(preview.runtimeOf("replycode:local_9_1:0")?.phase).toBe("ready");
  });

  it("临时标签：候选属于另一轮（轮次身份对不上）时不迁移", () => {
    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    // 临时消息出现时被观察到：它属于 turn_1。
    expect(preview.reconcileReplyCodeIdentities([reply("local_9_1", "print(1)", "turn_1")])).toBe(0);

    // 另一条回复（turn_2）有同一段代码：不是同一条回复，不能改绑。
    const moved = preview.reconcileReplyCodeIdentities([reply("msg_B", "print(1)", "turn_2")]);

    expect(moved).toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:local_9_1:0"]);
    expect(preview.runtimeOf("replycode:local_9_1:0")?.phase).toBe("ready");
  });

  it("临时标签：同一轮里两条助手消息都有同一段代码（候选不唯一）时不迁移", () => {
    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    expect(preview.reconcileReplyCodeIdentities([reply("local_9_1", "print(1)", "turn_1")])).toBe(0);

    const moved = preview.reconcileReplyCodeIdentities([
      reply("msg_a", "print(1)", "turn_1"),
      reply("msg_b", "print(1)", "turn_1"),
    ]);

    expect(moved).toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:local_9_1:0"]);
  });

  it("临时标签：正文对不上就不猜（旧标签原样保留，仍然可读）", () => {
    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    expect(preview.reconcileReplyCodeIdentities([reply("local_9_1", "print(1)", "turn_1")])).toBe(0);

    const moved = preview.reconcileReplyCodeIdentities([reply("msg_abc", "print(999)", "turn_1")]);

    expect(moved).toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:local_9_1:0"]);
    expect(preview.runtimeOf("replycode:local_9_1:0")?.phase).toBe("ready");
  });

  it("消息 id 还在列表里时什么都不做（不误迁移）", () => {
    preview.openReplyCode({ messageId: "msg_abc", blockIndex: 0, text: "print(1)", language: "python" });
    const moved = preview.reconcileReplyCodeIdentities([reply("msg_abc", "print(1)", "turn_1")]);
    expect(moved).toBe(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
  });
});

describe("第二组：同一回复从临时 id 转为正式 id", () => {
  it("标签跟着迁移，正文、搜索与滚动保留，只留一个标签", () => {
    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:local_9_1:0", { scrollTop: 42, search: "print" });
    // 临时消息出现时被观察到：它属于 turn_1。
    expect(preview.reconcileReplyCodeIdentities([reply("local_9_1", "print(1)", "turn_1")])).toBe(0);

    // 历史快照接手：同一条回复换成后端 id，轮次身份不变。
    const moved = preview.reconcileReplyCodeIdentities([reply("msg_abc", "print(1)", "turn_1")]);

    expect(moved).toBe(1);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
    expect(shell.prefs.activePreviewTabId).toBe("replycode:msg_abc:0");
    expect(preview.documentOf("replycode:msg_abc:0")?.text).toBe("print(1)");
    expect(preview.positionOf("replycode:msg_abc:0")).toMatchObject({ scrollTop: 42, search: "print" });
    expect(preview.runtimeOf("replycode:msg_abc:0")?.phase).toBe("ready");
    expect(preview.runtimeOf("replycode:local_9_1:0")).toBeNull();
  });

  it("迁移后重开同一段代码只保留一个标签，正文与搜索可用", () => {
    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    preview.reconcileReplyCodeIdentities([reply("local_9_1", "print(1)", "turn_1")]);
    preview.reconcileReplyCodeIdentities([reply("msg_abc", "print(1)", "turn_1")]);

    const outcome = preview.openReplyCode({
      messageId: "msg_abc",
      blockIndex: 0,
      text: "print(1)",
      language: "python",
    });

    expect(outcome).toBe("opened");
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
    expect(preview.documentOf("replycode:msg_abc:0")?.text).toBe("print(1)");
    expect(preview.runtimeOf("replycode:msg_abc:0")?.phase).toBe("ready");
  });

  it("正式标签已经打开时合并成一个，保住用户刚操作的那个标签的位置", () => {
    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:local_9_1:0", { scrollTop: 88, search: "print" });
    preview.openReplyCode({ messageId: "msg_abc", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:msg_abc:0", { scrollTop: 5 });
    // 用户刚操作的是临时标签（正在读它）。
    preview.activateTab("replycode:local_9_1:0");
    expect(preview.reconcileReplyCodeIdentities([reply("local_9_1", "print(1)", "turn_1")])).toBe(0);

    const moved = preview.reconcileReplyCodeIdentities([reply("msg_abc", "print(1)", "turn_1")]);

    expect(moved).toBe(1);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
    expect(shell.prefs.activePreviewTabId).toBe("replycode:msg_abc:0");
    expect(preview.positionOf("replycode:msg_abc:0")).toMatchObject({ scrollTop: 88, search: "print" });
    expect(preview.documentOf("replycode:msg_abc:0")?.text).toBe("print(1)");
    expect(preview.runtimeOf("replycode:msg_abc:0")?.phase).toBe("ready");
    expect(preview.runtimeOf("replycode:local_9_1:0")).toBeNull();
  });

  it("正式标签没有正文（刷新后从布局恢复）时合并：正文交给留下的标签，仍然可读", () => {
    // 刷新后布局里恢复了正式标签，但正文只在内存里 —— 它自己没有正文。
    shell.prefs.previewTabs = [
      {
        id: "replycode:msg_abc:0",
        target: {
          id: "replycode:msg_abc:0",
          kind: "code",
          title: "python",
          source: "reply_code",
          messageId: "msg_abc",
          blockIndex: 0,
        },
        position: {},
      },
    ];
    shell.prefs.activePreviewTabId = "replycode:msg_abc:0";
    shell.prefs.rightOpen = true;
    preview.ensureLoaded("replycode:msg_abc:0");
    expect(preview.runtimeOf("replycode:msg_abc:0")?.phase).toBe("problem");

    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:local_9_1:0", { scrollTop: 64, search: "print" });
    preview.activateTab("replycode:local_9_1:0");
    expect(preview.reconcileReplyCodeIdentities([reply("local_9_1", "print(1)", "turn_1")])).toBe(0);

    const moved = preview.reconcileReplyCodeIdentities([reply("msg_abc", "print(1)", "turn_1")]);

    expect(moved).toBe(1);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
    expect(preview.documentOf("replycode:msg_abc:0")?.text).toBe("print(1)");
    expect(preview.runtimeOf("replycode:msg_abc:0")?.phase).toBe("ready");
    expect(preview.positionOf("replycode:msg_abc:0")).toMatchObject({ scrollTop: 64, search: "print" });
  });

  it("正式标签是当前标签时合并：保留正式标签自己的位置，不被旧临时位置覆盖", () => {
    preview.openReplyCode({ messageId: "local_9_1", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:local_9_1:0", { scrollTop: 88 });
    preview.openReplyCode({ messageId: "msg_abc", blockIndex: 0, text: "print(1)", language: "python" });
    preview.savePosition("replycode:msg_abc:0", { scrollTop: 5 });
    expect(shell.prefs.activePreviewTabId).toBe("replycode:msg_abc:0");
    expect(preview.reconcileReplyCodeIdentities([reply("local_9_1", "print(1)", "turn_1")])).toBe(0);

    const moved = preview.reconcileReplyCodeIdentities([reply("msg_abc", "print(1)", "turn_1")]);

    expect(moved).toBe(1);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(["replycode:msg_abc:0"]);
    expect(preview.positionOf("replycode:msg_abc:0")).toMatchObject({ scrollTop: 5 });
  });
});
