/**
 * 聊天输入的空白判定 / 规范化纯函数守卫（仍在生产代码里使用：
 * stores/session.ts 用 isBlankText / normalizeSendText 判定空白与真正发出的文字）。
 *
 * 2026-10-26（合并后清理 · C2 收尾）：旧 ChatDock 的文案与按键判定函数已删除，
 * 对应的纯函数用例一并移除 —— 那些能力由 v2/InteractiveConversation + components/Composer
 * 承担（Enter / Shift+Enter / 输入法选字的组件级用例在 components/__tests__/Composer.test.ts）。
 * 负向断言（零宽字符不算内容、空白不能发、正文一个字都不改）保留在本文件。
 */
import { describe, expect, it } from "vitest";
import { isBlankText, normalizeSendText } from "../chat";

describe("空白判定（空白不能发送）", () => {
  it("空串 / null / undefined 都算空白", () => {
    expect(isBlankText("")).toBe(true);
    expect(isBlankText(null)).toBe(true);
    expect(isBlankText(undefined)).toBe(true);
  });

  it("纯空格不发：半角 / 全角 / 制表 / 换行都算空白", () => {
    for (const blank of [" ", "   ", "\t", "\n", "\r\n", "\u3000", "\u3000\u3000", " \u3000 \t\n"]) {
      expect(isBlankText(blank), JSON.stringify(blank)).toBe(true);
    }
    // 不换行空格也是空白（复制网页文字时很常见）
    expect(isBlankText("\u00A0\u00A0")).toBe(true);
  });

  it("看不见的零宽字符不算内容（否则会发出一条空消息）", () => {
    expect(isBlankText("\u200B")).toBe(true);
    expect(isBlankText("\u200B\u200C\u200D\uFEFF")).toBe(true);
    expect(isBlankText("\u2060\u200B")).toBe(true);
  });

  it("有真实内容就不是空白（前后空白不影响判断）", () => {
    expect(isBlankText("你好")).toBe(false);
    expect(isBlankText("  你好  ")).toBe(false);
    expect(isBlankText("嗨 \u3000")).toBe(false);
    expect(isBlankText("1")).toBe(false);
    // 只有 emoji 也算内容（不要用「有没有汉字」这类判断把用户的话挡掉）
    expect(isBlankText("🙂")).toBe(false);
  });
});

describe("发出去的文字只有输入本身", () => {
  it("只去掉首尾空白，正文一个字都不改", () => {
    expect(normalizeSendText("  你好，帮我看下  ")).toBe("你好，帮我看下");
    expect(normalizeSendText("\u3000嗨\u3000")).toBe("嗨");
  });

  it("中间的空行与缩进原样保留（不替用户改写）", () => {
    const text = "第一行\n\n  缩进的第二行";
    expect(normalizeSendText(text)).toBe(text);
  });

  it("不会追加板面 / 未提交改动 / 注释 / 选择范围，也不会删掉用户写的字", () => {
    const text = "把这两张卡片的关系改成「依赖」";
    // 输出必须与「输入去首尾空白」完全一致：任何附加内容都会在这里现形
    expect(normalizeSendText(text)).toBe(text.trim());
    expect(normalizeSendText(text)).not.toContain("board");
    expect(normalizeSendText(text)).not.toContain("卡片 id");
  });
});
