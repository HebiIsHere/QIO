/**
 * #57 预览反馈（消息侧）：
 *
 * * 成功打开右侧只读预览时，消息区不再留下「已在右侧打开「…」的只读预览」；
 *   右侧预览面板本身负责反馈（见 cv-c-preview-feedback.test.ts）；
 * * 真正打开失败（没接线）仍在附件行就地提示；
 * * 同名不同文件是两个附件 id、两个标签：各自打开互不串，不留成功文案；
 * * 回复代码块预览保持成功静默。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/MessageItemPreviewFeedback.test.ts
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import {
  registerConversationPreviewOpener,
  registerConversationReplyCodeOpener,
} from "../conversation/previewBridge";
import type { AttachmentRef } from "../../services/attachments";
import type { StreamMessage } from "../../stores/session";

function makeMessage(over: Partial<StreamMessage> & { role: StreamMessage["role"] }): StreamMessage {
  return {
    id: "m1",
    content: "看看这些文件",
    contentType: "text",
    createdAt: "2026-11-12T09:42:11+08:00",
    ...over,
  };
}

function attachment(over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_a",
    name: "同名.pdf",
    sizeBytes: 1024,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function mountItem(msg: StreamMessage) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MessageItem, {
    props: { message: msg },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

beforeEach(() => {
  localStorage.clear();
  registerConversationPreviewOpener(null);
  registerConversationReplyCodeOpener(null);
});

afterEach(() => {
  registerConversationPreviewOpener(null);
  registerConversationReplyCodeOpener(null);
});

describe("附件预览：成功静默、失败就地提示", () => {
  it("同名不同文件各自打开：调用两个附件 id，消息区不出现「已在右侧打开」", async () => {
    const opened: string[] = [];
    registerConversationPreviewOpener((target) => {
      opened.push(target.id);
    });
    const w = mountItem(
      makeMessage({
        role: "user",
        attachments: [
          attachment({ id: "att_a", name: "同名.pdf" }),
          attachment({ id: "att_b", name: "同名.pdf" }),
        ],
      }),
    );
    const buttons = w.findAll(".attach-item .act.preview-open");
    expect(buttons).toHaveLength(2);
    await buttons[0].trigger("click");
    await buttons[1].trigger("click");

    expect(opened).toEqual(["attachment:att_a", "attachment:att_b"]);
    expect(w.text()).not.toContain("已在右侧打开");
    expect(w.find("[data-test='attachment-notice']").exists()).toBe(false);
    w.unmount();
  });

  it("普通消息附件打开成功：同样不在消息区留成功文案", async () => {
    const opened: string[] = [];
    registerConversationPreviewOpener((target) => {
      opened.push(target.id);
    });
    const w = mountItem(
      makeMessage({ role: "user", attachments: [attachment({ id: "att_one", name: "合同.docx" })] }),
    );
    await w.find(".attach-item .act.preview-open").trigger("click");
    expect(opened).toEqual(["attachment:att_one"]);
    expect(w.find("[data-test='attachment-notice']").exists()).toBe(false);
    expect(w.text()).not.toContain("只读预览");
    w.unmount();
  });

  it("真正打开失败（预览没接线）：仍在附件行就地提示", async () => {
    // 先在接线状态下挂载（按钮可见），再模拟接线失效：点击必须如实说没打开。
    registerConversationPreviewOpener(() => undefined);
    const w = mountItem(makeMessage({ role: "user", attachments: [attachment()] }));
    registerConversationPreviewOpener(null);
    await w.find(".attach-item .act.preview-open").trigger("click");

    const notice = w.find("[data-test='attachment-notice']");
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("还没有接上右侧预览");
    expect(notice.text()).not.toContain("已在右侧打开");
    w.unmount();
  });

  it("回复代码块预览保持成功静默（成功不弹说明）", async () => {
    const opened: string[] = [];
    registerConversationReplyCodeOpener((input) => {
      opened.push(input.messageId + ":" + String(input.blockIndex));
    });
    const w = mountItem(
      makeMessage({
        role: "assistant",
        content: "示例：\n\n```ts\nconst a = 1;\n```\n",
        contentType: "text",
      }),
    );
    const button = w.find(".code-preview-btn");
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    expect(opened).toEqual(["m1:0"]);
    expect(w.find(".code-preview-notice").exists()).toBe(false);
    w.unmount();
  });
});
