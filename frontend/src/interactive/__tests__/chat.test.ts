import { describe, expect, it } from "vitest";
import {
  canSend,
  chatDraftHintText,
  chatScopeText,
  chatStatusText,
  isBlankText,
  normalizeSendText,
  olderMessagesText,
  sendFailureText,
  shouldSendOnKeydown,
} from "../chat";

describe("能不能发送（空白不发）", () => {
  it("空串不发，并给出原因", () => {
    const verdict = canSend("");
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toBe("还没有输入内容");
    expect(canSend(null as unknown as string).ok).toBe(false);
    expect(canSend(undefined as unknown as string).ok).toBe(false);
  });

  it("纯空格不发：半角 / 全角 / 制表 / 换行都算空白", () => {
    for (const blank of [" ", "   ", "\t", "\n", "\r\n", "\u3000", "\u3000\u3000", " \u3000 \t\n"]) {
      expect(canSend(blank).ok, JSON.stringify(blank)).toBe(false);
      expect(isBlankText(blank), JSON.stringify(blank)).toBe(true);
    }
    // 不换行空格也是空白（复制网页文字时很常见）
    expect(canSend("\u00A0\u00A0").ok).toBe(false);
  });

  it("看不见的零宽字符不算内容（否则会发出一条空消息）", () => {
    expect(canSend("\u200B").ok).toBe(false);
    expect(canSend("\u200B\u200C\u200D\uFEFF").ok).toBe(false);
    expect(canSend("\u2060\u200B").ok).toBe(false);
  });

  it("有真实内容就能发（前后空白不影响判断）", () => {
    expect(canSend("你好").ok).toBe(true);
    expect(canSend("  你好  ").ok).toBe(true);
    expect(canSend("嗨 \u3000").ok).toBe(true);
    expect(canSend("1").ok).toBe(true);
    // 只有 emoji 也算内容（不要用「有没有汉字」这类判断把用户的话挡掉）
    expect(canSend("🙂").ok).toBe(true);
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

describe("输入法状态判定（中文选字时不发送）", () => {
  it("Enter 发送", () => {
    expect(shouldSendOnKeydown({ key: "Enter" })).toBe(true);
  });

  it("Shift+Enter 换行，不发送", () => {
    expect(shouldSendOnKeydown({ key: "Enter", shiftKey: true })).toBe(false);
  });

  it("输入法正在选字时不发送：isComposing 为 true", () => {
    expect(shouldSendOnKeydown({ key: "Enter", isComposing: true })).toBe(false);
    // 组合中的 Shift+Enter 同样不发送
    expect(shouldSendOnKeydown({ key: "Enter", isComposing: true, shiftKey: true })).toBe(false);
  });

  it("只给 keyCode 229 的输入法同样不发送（部分输入法只有这个信号）", () => {
    expect(shouldSendOnKeydown({ key: "Enter", keyCode: 229 })).toBe(false);
    expect(shouldSendOnKeydown({ key: "Process", keyCode: 229 })).toBe(false);
  });

  it("其它按键不发送，交给输入框自己处理（字母、Esc、Tab、方向键、空的 keyCode）", () => {
    for (const key of ["a", "Escape", "Tab", "ArrowUp", "Enter"].slice(0, 4)) {
      expect(shouldSendOnKeydown({ key }), key).toBe(false);
    }
    expect(shouldSendOnKeydown({ key: "Enter", keyCode: 13 })).toBe(true);
    expect(shouldSendOnKeydown(null)).toBe(false);
    expect(shouldSendOnKeydown(undefined)).toBe(false);
  });
});

describe("失败文案（保留输入并给真实原因）", () => {
  it("带原因时原样说出原因，并写明输入已保留", () => {
    const text = sendFailureText("后端没有响应");
    expect(text).toContain("发送失败");
    expect(text).toContain("后端没有响应");
    expect(text).toContain("输入已保留");
    expect(text).toContain("可以重试");
  });

  it("没有原因时也说清楚，不假装成功", () => {
    const text = sendFailureText(null);
    expect(text).toContain("原因未知");
    expect(text).toContain("输入已保留");
    expect(sendFailureText("")).toBe(text);
    expect(sendFailureText("   ")).toBe(text);
  });

  it("原因里的多余空白被规整，但原因本身不被改写", () => {
    expect(sendFailureText("  网络中断  ")).toContain("网络中断");
    expect(sendFailureText("401 未授权")).toContain("401 未授权");
  });
});

describe("面板文案（如实说明能力边界）", () => {
  it("范围说明写明只发文字、不带板面、不调用提交接口，并承认尚未接入", () => {
    const text = chatScopeText();
    expect(text).toContain("只发送输入的文字");
    expect(text).toContain("不带板面");
    expect(text).toContain("不会调用板面提交接口");
    expect(text).toContain("尚未接入");
  });

  it("状态说明区分「正在回答」与「排队中」，空闲时给明确文字", () => {
    expect(chatStatusText({ turnRunning: true })).toContain("正在回答");
    expect(chatStatusText({ turnRunning: true, queuedCount: 2 })).toContain("2 条排队中");
    expect(chatStatusText({ turnRunning: false, queuedCount: 1 })).toContain("1 条消息排队中");
    expect(chatStatusText({ turnRunning: false })).toContain("空闲");
  });

  it("更早的消息被折叠时说清在哪里，不让人以为丢了", () => {
    expect(olderMessagesText(0)).toBe("");
    expect(olderMessagesText(-3)).toBe("");
    expect(olderMessagesText(12)).toContain("12");
    expect(olderMessagesText(12)).toContain("不会丢");
  });

  it("草稿说明写明收起面板不清草稿", () => {
    expect(chatDraftHintText()).toContain("不会清掉草稿");
  });
});
/**
 * 组件级验证（真实挂载 ChatDock）。
 *
 * 纯函数能证明「判定对不对」，但不能证明「界面真的用了这些判定」。
 * 这里用真 pinia + 真会话 store 挂载组件，覆盖三条最容易退化的行为：
 * 收起面板不清消息 / 不删草稿 / 不打断进行中的对话；Enter 与输入法选字的区别；失败保留输入。
 */
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, vi } from "vitest";
import ChatDock from "../../components/interactive/ChatDock.vue";
import { useInteractiveStore } from "../../stores/interactive";
import { useSessionStore, type StreamMessage } from "../../stores/session";

function message(id: string, role: StreamMessage["role"], content: string): StreamMessage {
  return { id, role, content, contentType: "text/plain", createdAt: "2026-10-07T04:00:00.000Z" };
}

function enterKey(textarea: HTMLTextAreaElement, isComposing = false) {
  const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
  if (isComposing) Object.defineProperty(event, "isComposing", { value: true });
  textarea.dispatchEvent(event);
  return event;
}

describe("悬浮聊天面板（组件级）", () => {
  let pinia: ReturnType<typeof createPinia>;

  beforeEach(() => {
    pinia = createPinia();
    setActivePinia(pinia);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function open(chatOpen = true) {
    const session = useSessionStore();
    const interactive = useInteractiveStore();
    interactive.chatOpen = chatOpen;
    const wrapper = mount(ChatDock, { global: { plugins: [pinia] } });
    return { wrapper, session, interactive };
  }

  it("收起面板不清消息、不删草稿、不打断进行中的对话", async () => {
    const { wrapper, session, interactive } = open();
    session.messages = [message("m1", "user", "给我看看板面上的材料"), message("m2", "assistant", "好的。")];
    session.draft = "还没发出去的草稿";
    session.turnRunning = true;
    await flushPromises();

    expect(wrapper.find('[data-im="chat-panel"]').exists()).toBe(true);
    expect((wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement).value).toBe(
      "还没发出去的草稿",
    );
    expect(wrapper.findAll(".stream-item")).toHaveLength(2);

    // 收起：只是面板不渲染
    await wrapper.find('[data-im="chat-toggle"]').trigger("click");
    expect(wrapper.find('[data-im="chat-panel"]').exists()).toBe(false);
    expect(session.messages).toHaveLength(2);
    expect(session.draft).toBe("还没发出去的草稿");
    expect(session.turnRunning).toBe(true);

    // 再展开：消息与草稿都在，进行中的对话没有被重开
    await wrapper.find('[data-im="chat-toggle"]').trigger("click");
    await flushPromises();
    expect(wrapper.findAll(".stream-item")).toHaveLength(2);
    expect((wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement).value).toBe(
      "还没发出去的草稿",
    );
    expect(interactive.chatOpen).toBe(true);
    expect(session.turnRunning).toBe(true);
  });

  it("Enter 只发送输入的文字（去首尾空白），并清空草稿", async () => {
    const { wrapper, session } = open();
    const send = vi.spyOn(session, "send").mockResolvedValue(true);
    session.draft = "  把这两张卡片连起来  ";
    await flushPromises();

    const textarea = wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement;
    const event = enterKey(textarea);
    expect(event.defaultPrevented).toBe(true);
    await flushPromises();

    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith("把这两张卡片连起来");
    expect(session.draft).toBe("");
  });

  it("输入法选字中的 Enter 不发送，草稿原样保留", async () => {
    const { wrapper, session } = open();
    const send = vi.spyOn(session, "send").mockResolvedValue(true);
    session.draft = "zhong";
    await flushPromises();

    const textarea = wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement;
    const event = enterKey(textarea, true);
    expect(event.defaultPrevented).toBe(false);
    await flushPromises();

    expect(send).not.toHaveBeenCalled();
    expect(session.draft).toBe("zhong");
  });

  it("纯空格 + Enter 不发送，并说明原因", async () => {
    const { wrapper, session } = open();
    const send = vi.spyOn(session, "send").mockResolvedValue(true);
    session.draft = "   ";
    await flushPromises();

    enterKey(wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement);
    await flushPromises();

    expect(send).not.toHaveBeenCalled();
    expect(session.draft).toBe("   ");
    expect(wrapper.find('[data-im="chat-blank"]').text()).toContain("还没有输入内容");
  });

  it("发送失败保留输入并显示真实原因（不伪装成功）", async () => {
    const { wrapper, session } = open();
    vi.spyOn(session, "send").mockImplementation(async () => {
      session.lastError = "网络中断";
      return false;
    });
    session.draft = "这条发不出去的话";
    await flushPromises();

    enterKey(wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement);
    await flushPromises();

    expect(session.draft).toBe("这条发不出去的话");
    const failure = wrapper.find('[data-im="chat-failure"]').text();
    expect(failure).toContain("网络中断");
    expect(failure).toContain("输入已保留");
  });
});
