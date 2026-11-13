/**
 * #12 文件读取失败的界面呈现（工具卡）：
 *
 * 1. 只认程序字段：toolCode === "file_read_failed"，旧数据回退到
 *    toolName === "read_attachment" + category ∈ io_error / invalid_data / unavailable；
 * 2. 卡面给一次可读结论「这次没读到文件内容」（有 name 时带上），再接真实原因与下一步；
 * 3. toolError 里可能是给模型的内部指令（「可以让用户…」「可以先不传…」），
 *    绝不作为可见文案；工具名 / attachment_id / code / category / diagnostic
 *    放在展开后的诊断区（默认折叠）；
 * 4. 其它工具失败（以及取消、审批拒绝这类）保持原有行为，不归成文件读取。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/MessageItemFileReadFailure.test.ts
 */
import { beforeEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import MessageItem from "../MessageItem.vue";
import type { StreamMessage } from "../../stores/session";

const MODEL_INSTRUCTION =
  "可以让用户重新上传附件；可以先不传这个文件继续回答，不要把它当成读取成功。";

function makeMessage(over: Partial<StreamMessage> & { role: StreamMessage["role"] }): StreamMessage {
  return {
    id: "m1",
    content: "",
    contentType: "text",
    createdAt: "2026-11-12T09:42:11+08:00",
    ...over,
  };
}

function mountItem(msg: StreamMessage, pinia: Pinia) {
  return mount(MessageItem, {
    props: { message: msg },
    global: { plugins: [pinia], stubs: { MarkdownContent: true } },
  });
}

function fileReadFailure(over: Partial<StreamMessage> = {}): StreamMessage {
  return makeMessage({
    role: "tool",
    toolName: "read_attachment",
    presentation: { title: "读取附件", tool: "read_attachment" },
    toolStatus: "failed",
    toolOk: false,
    toolCode: "file_read_failed",
    toolCategory: "io_error",
    toolCategoryLabel: "读写错误",
    toolRecoverable: true,
    toolResultStatus: "failed",
    toolError: MODEL_INSTRUCTION,
    toolFileRead: {
      attachmentId: "att_1",
      name: "季度报告.docx",
      reason: "文件内容读不出来（压缩结构损坏）",
      nextStep: "请重新上传这个文件，或先把内容贴成文本。",
      diagnostic: "zip: bad central directory",
    },
    ...over,
  });
}

function countPhrase(text: string, phrase: string): number {
  return text.split(phrase).length - 1;
}

beforeEach(() => {
  localStorage.clear();
});

describe("文件读取失败：卡面结论", () => {
  it("显示一次「这次没读到文件内容」+ 文件名 + 真实原因与下一步", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(fileReadFailure(), pinia);
    const card = w.find(".tool-card");
    expect(card.exists()).toBe(true);
    expect(card.attributes("data-state")).toBe("failed");

    const text = card.text();
    expect(countPhrase(text, "这次没读到文件内容")).toBe(1);
    expect(text).toContain("季度报告.docx");
    expect(text).toContain("文件内容读不出来（压缩结构损坏）");
    expect(text).toContain("请重新上传这个文件，或先把内容贴成文本。");
    w.unmount();
  });

  it("绝不把 toolError 里的模型内部指令当可见文案", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(fileReadFailure(), pinia);
    const card = w.find(".tool-card");
    expect(card.text()).not.toContain("可以让用户");
    expect(card.text()).not.toContain("可以先不传");
    expect(card.text()).not.toContain(MODEL_INSTRUCTION);
    // 展开后也不显示原文：诊断区只放结构化字段
    w.find(".tool-head").trigger("click");
    expect(w.find(".tool-detail").text()).not.toContain("可以让用户");
    w.unmount();
  });

  it("没有结构化原因时用 code/category 的可读回退（不空着、不拿错误原文顶替）", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      fileReadFailure({
        toolFileRead: {
          attachmentId: "att_9",
          name: "",
          reason: "",
          nextStep: "",
          diagnostic: "",
        },
      }),
      pinia,
    );
    const line = w.find(".tool-fail-line").text();
    expect(line).toContain("这次没读到文件内容");
    const text = w.find(".tool-card").text();
    expect(text.trim().length).toBeGreaterThan(line.length);
    expect(text).not.toContain("可以让用户");
    w.unmount();
  });
});

describe("文件读取失败：诊断区按需展开（默认折叠）", () => {
  it("默认折叠；展开后能看到工具名、attachment_id、code/category 与 diagnostic", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(fileReadFailure(), pinia);
    const wrap = w.find(".tool-detail-wrap");
    expect(wrap.classes()).not.toContain("open");

    await w.find(".tool-head").trigger("click");
    expect(w.find(".tool-detail-wrap").classes()).toContain("open");
    const detail = w.find(".tool-detail").text();
    expect(detail).toContain("read_attachment");
    expect(detail).toContain("att_1");
    expect(detail).toContain("file_read_failed");
    expect(detail).toContain("io_error");
    expect(detail).toContain("zip: bad central directory");
    w.unmount();
  });
});

describe("文件读取失败：只认程序字段", () => {
  it("旧数据回退：read_attachment + io_error/invalid_data/unavailable 才认", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    for (const category of ["io_error", "invalid_data", "unavailable"]) {
      const w = mountItem(
        fileReadFailure({
          toolCode: null,
          toolCategory: category,
          toolFileRead: null,
        }),
        pinia,
      );
      expect(w.find(".tool-fail-line").text(), category).toContain("这次没读到文件内容");
      expect(w.find(".tool-card").text()).not.toContain("可以让用户");
      w.unmount();
    }
  });

  it("read_attachment 但类别不在文件读取范围（如超时）：不归成文件读取", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      fileReadFailure({
        toolCode: null,
        toolCategory: "timeout",
        toolCategoryLabel: "超时",
        toolFileRead: null,
        toolError: "读取超时：30s 内没有返回",
      }),
      pinia,
    );
    const line = w.find(".tool-fail-line").text();
    expect(line).not.toContain("这次没读到文件内容");
    expect(line).toContain("读取超时");
    w.unmount();
  });

  it("其它稳定 code（权限 / 审批 / 限额）不归成文件读取，保持原有失败文案", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      fileReadFailure({
        toolCode: "permission_denied",
        toolCategory: "permission",
        toolCategoryLabel: "没有权限",
        toolFileRead: null,
        toolError: "没有权限读取这个附件",
      }),
      pinia,
    );
    expect(w.find(".tool-fail-line").text()).not.toContain("这次没读到文件内容");
    expect(w.find(".tool-fail-line").text()).toContain("没有权限读取这个附件");
    w.unmount();
  });

  it("取消：不冒充「没读到内容」", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      fileReadFailure({
        toolStatus: "cancelled",
        toolCode: null,
        toolFileRead: null,
        toolError: "这次读取已取消",
      }),
      pinia,
    );
    const line = w.find(".tool-fail-line").text();
    expect(line).not.toContain("这次没读到文件内容");
    expect(line).toContain("取消");
    w.unmount();
  });

  it("普通工具失败（哪怕错误文本提到文件）仍按普通失败呈现，不从中文关键词判断", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const w = mountItem(
      makeMessage({
        role: "tool",
        toolName: "web_search",
        toolOk: false,
        toolStatus: "failed",
        toolError: "文件读取失败：这次没读到文件内容（这句只是错误原文）",
        presentation: { title: "网络搜索", tool: "web_search" },
      }),
      pinia,
    );
    expect(w.find(".tool-fail-line").text()).toContain("这句只是错误原文");
    w.unmount();
  });
});
