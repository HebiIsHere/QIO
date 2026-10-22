/**
 * 两个接线桥（B 提供、Lead/A/C 集成时注册）：
 * - 执行记录 → 左侧任务栏定位；
 * - 附件「在右侧查看」→ C 的只读预览（身份按契约 §4.3，不以文件名当身份）。
 *
 * 另外用源码守卫钉住「聊天区域不得横向滚动」的布局策略（jsdom 不做真实布局，
 * 断言容器已声明数据属性与 overflow-x: clip，而不是脆弱的像素值）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  hasExecutionTaskLocator,
  locateExecutionTask,
  registerExecutionTaskLocator,
} from "../taskLocator";
import {
  canPreviewAttachment,
  hasConversationPreviewOpener,
  openConversationPreview,
  previewKindOf,
  previewTargetForAttachment,
  registerConversationPreviewOpener,
} from "../previewBridge";
import MessageItem from "../../MessageItem.vue";
import { useSessionStore } from "../../../stores/session";
import type { StreamMessage } from "../../../stores/session";

beforeEach(() => {
  localStorage.clear();
  registerExecutionTaskLocator(null);
  registerConversationPreviewOpener(null);
});

describe("执行记录 → 左侧任务栏定位桥", () => {
  it("没有注册时不渲染按钮、定位返回 false（不出现点不动的假按钮）", () => {
    expect(hasExecutionTaskLocator()).toBe(false);
    expect(locateExecutionTask({ source: "tool", sourceId: "call_1", turnId: null })).toBe(false);
  });

  it("注册后按 callId 精确定位；定位结果原样返回", () => {
    const calls: unknown[] = [];
    registerExecutionTaskLocator((ref) => {
      calls.push(ref);
      return ref.sourceId === "call_1";
    });
    expect(hasExecutionTaskLocator()).toBe(true);
    expect(locateExecutionTask({ source: "tool", sourceId: "call_1", turnId: "turn_1" })).toBe(true);
    expect(calls).toEqual([{ source: "tool", sourceId: "call_1", turnId: "turn_1" }]);
    expect(locateExecutionTask({ source: "tool", sourceId: "call_404", turnId: null })).toBe(false);
  });

  it("定位函数抛错时返回 false（不让一个坏接线把消息流弄崩）", () => {
    registerExecutionTaskLocator(() => {
      throw new Error("left rail exploded");
    });
    expect(locateExecutionTask({ source: "subagent", sourceId: "task_1", turnId: null })).toBe(false);
  });

  it("工具卡上出现定位按钮，点击把 callId 交给接线函数", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const seen = vi.fn(() => true);
    registerExecutionTaskLocator(seen);
    const message: StreamMessage = {
      id: "m1",
      role: "tool",
      content: "输出",
      contentType: "text",
      createdAt: new Date().toISOString(),
      callId: "call_1",
      toolName: "read_file",
      turnId: "turn_1",
      presentation: { title: "读取文件", tool: "read_file", summary: "输出" } as never,
    };
    const w = mount(MessageItem, { props: { message }, global: { plugins: [pinia] } });
    await w.vm.$nextTick();
    const button = w.find(".task-locate");
    expect(button.exists()).toBe(true);
    await button.trigger("click");
    expect(seen).toHaveBeenCalledWith({ source: "tool", sourceId: "call_1", turnId: "turn_1", title: "读取文件" });
    w.unmount();
  });
});

describe("附件「在右侧查看」→ 预览桥", () => {
  it("未接线 / 不支持的格式不显示按钮（不打开空白面板）", () => {
    expect(hasConversationPreviewOpener()).toBe(false);
    expect(canPreviewAttachment({ name: "报告.pdf" })).toBe(false);
    registerConversationPreviewOpener(() => undefined);
    expect(canPreviewAttachment({ name: "报告.pdf" })).toBe(true);
    expect(canPreviewAttachment({ name: "未知格式.xyz" })).toBe(false);
    expect(openConversationPreview({ id: "x", kind: "text", title: "t", source: "attachment" })).toBe(true);
  });

  it("身份用附件 id（不是文件名）：同名不同文件是两个标签，版本变化产生新身份", () => {
    const a = previewTargetForAttachment({ id: "att_1", name: "同名.pdf", topicId: "t1", size: 10 });
    const b = previewTargetForAttachment({ id: "att_2", name: "同名.pdf", topicId: "t1" });
    expect(a.id).toBe("attachment:att_1");
    expect(b.id).toBe("attachment:att_2");
    expect(a.id).not.toBe(b.id);
    const versioned = previewTargetForAttachment({ id: "att_1", name: "同名.pdf", version: "v2" });
    expect(versioned.id).toBe("attachment:att_1@v2");
    expect(versioned.id).not.toBe(a.id);
    expect(a.source).toBe("attachment");
    expect(a.attachmentId).toBe("att_1");
    expect(a.topicId).toBe("t1");
  });

  it("按扩展名选择渲染器类型", () => {
    expect(previewKindOf("截图.PNG")).toBe("image");
    expect(previewKindOf("合同.pdf")).toBe("pdf");
    expect(previewKindOf("表格.xlsx")).toBe("xlsx");
    expect(previewKindOf("说明.md")).toBe("markdown");
    expect(previewKindOf("script.py")).toBe("code");
    expect(previewKindOf("录音.mp3")).toBe("audio");
    expect(previewKindOf("未知.xyz")).toBe("unsupported");
  });
});

describe("聊天区域不得横向滚动（布局策略守卫）", () => {
  function source(path: string): string {
    return readFileSync(resolve(process.cwd(), "src", path), "utf8");
  }

  it("消息流：只声明纵向滚动指示条，横向是 clip，根带 data-qio-fit", () => {
    const stream = source("components/MessageStream.vue");
    expect(stream).toContain('axis: "y"');
    expect(stream).toContain("overflow-x: clip");
    expect(stream).toContain('data-qio-fit');
    expect(stream).toContain('data-qio-scope="chat"');
  });

  it("输入气泡：不建立横向滚动范围（软换行 + overflow-x: clip）", () => {
    const composer = source("components/Composer.vue");
    expect(composer).toContain('data-qio-fit');
    expect(composer).toContain("overflow-x: clip");
    expect(composer).toContain("overflow-wrap: anywhere");
  });

  it("中央工作区根声明对话作用域与宽度链，话题栏/状态区文本可换行", () => {
    const column = source("components/conversation/ChatColumn.vue");
    expect(column).toContain('data-qio-scope="chat"');
    expect(column).toContain('data-qio-fit');
    expect(column).toContain("overflow-wrap: anywhere");
  });
});
