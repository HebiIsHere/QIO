/**
 * 底部互动对话区的纯函数（Phase 3-B）：高度口径 / 收起态文案 / 可信绑定投影 / 本机高度解析。
 *
 * 契约：docs/interactive-page-v2-contract.md §6.4。
 */
import { describe, expect, it } from "vitest";
import {
  CONVERSATION_DEFAULT_HEIGHT,
  CONVERSATION_MAX_HEIGHT,
  CONVERSATION_MIN_HEIGHT,
  clampConversationHeight,
  conversationBindingForSend,
  conversationStatusText,
  parseStoredConversationHeight,
} from "../conversationLayout";

describe("高度口径：拖动 / 键盘 / 视口约束", () => {
  it("夹在 [min, max] 之间；非法值退回默认值", () => {
    expect(clampConversationHeight(500)).toBe(500);
    expect(clampConversationHeight(10)).toBe(CONVERSATION_MIN_HEIGHT);
    expect(clampConversationHeight(9999)).toBe(CONVERSATION_MAX_HEIGHT);
    expect(clampConversationHeight(Number.NaN)).toBe(CONVERSATION_DEFAULT_HEIGHT);
    expect(clampConversationHeight(Number.POSITIVE_INFINITY)).toBe(CONVERSATION_DEFAULT_HEIGHT);
    expect(clampConversationHeight(500.4)).toBe(500);
  });

  it("矮视口按比例收紧上限，但不会低于最小高度", () => {
    expect(clampConversationHeight(700, { viewportHeight: 300 })).toBe(240);
    expect(clampConversationHeight(700, { viewportHeight: 200 })).toBe(CONVERSATION_MIN_HEIGHT);
    expect(clampConversationHeight(700, { viewportHeight: 4000 })).toBe(700);
  });

  it("自定义上下限可用（Lead 集成时按槽位约束）", () => {
    expect(clampConversationHeight(400, { min: 300, max: 360 })).toBe(360);
    expect(clampConversationHeight(200, { min: 300, max: 360 })).toBe(300);
  });
});

describe("收起态文案：只说真实状态，不编造未读", () => {
  it("回复中 / 排队 / 草稿 / 空", () => {
    expect(conversationStatusText({ turnRunning: true })).toBe("正在回复…");
    expect(conversationStatusText({ turnRunning: true, queuedCount: 2 })).toBe("正在回复 · 已排队 2 条");
    expect(conversationStatusText({ turnRunning: false, queuedCount: 3 })).toBe("已排队 3 条");
    expect(conversationStatusText({ hasDraft: true })).toBe("草稿已保留");
    expect(conversationStatusText({})).toBe("点开继续对话");
    expect(conversationStatusText({ queuedCount: -5 })).toBe("点开继续对话");
  });
});

describe("可信绑定投影：只给会话层认识的字段，绝不进入用户原文", () => {
  it("没有绑定 → undefined（普通聊天不带板面身份）", () => {
    expect(conversationBindingForSend(undefined)).toBeUndefined();
    expect(conversationBindingForSend(null)).toBeUndefined();
  });

  it("有绑定 → mode 缺省 interactive，board/submission 透传，空白视为没有", () => {
    expect(conversationBindingForSend({ boardId: "b1", submissionId: "s1" })).toEqual({
      mode: "interactive",
      boardId: "b1",
      submissionId: "s1",
    });
    expect(conversationBindingForSend({ mode: "normal", boardId: " b2 ", submissionId: "  " })).toEqual({
      mode: "normal",
      boardId: "b2",
    });
  });

  it("intentId 不进入发送选项（当前会话层只认 board/submission）——它不会被拼进正文", () => {
    const projected = conversationBindingForSend({ boardId: "b1", intentId: "i1" }) as unknown as Record<
      string,
      unknown
    >;
    expect(projected.intentId).toBeUndefined();
    expect(JSON.stringify(projected)).not.toContain("i1");
  });
});

describe("本机高度解析", () => {
  it("只接受正的有限数；损坏值返回 null 让调用方用默认值", () => {
    expect(parseStoredConversationHeight("480")).toBe(480);
    expect(parseStoredConversationHeight(" 360.2 ")).toBe(360);
    expect(parseStoredConversationHeight("abc")).toBeNull();
    expect(parseStoredConversationHeight("0")).toBeNull();
    expect(parseStoredConversationHeight("-20")).toBeNull();
    expect(parseStoredConversationHeight("")).toBeNull();
    expect(parseStoredConversationHeight(null)).toBeNull();
  });
});
