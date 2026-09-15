import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(async () => ({ ok: true })),
    getSessionContext: vi.fn(async () => ({ topic_id: "t1", topic_name: "话题", anchor_fragment: null, messages: [] })),
  },
}));

/** jsdom 不做布局：手动给滚动容器一个可测的几何 */
function shape(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
}

/** 等 watcher 内部 await nextTick + 贴底写入全部落定 */
async function settle() {
  await nextTick();
  await nextTick();
  await nextTick();
}

async function mountStream(): Promise<{
  w: ReturnType<typeof mount>;
  session: ReturnType<typeof useSessionStore>;
  stream: HTMLElement;
  pinia: Pinia;
}> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const w = mount(MessageStream, { global: { plugins: [pinia] } });
  await nextTick();
  const stream = w.find(".stream").element as HTMLElement;
  shape(stream, 1000, 400); // 底部 = scrollTop 600
  return { w, session, stream, pinia };
}

beforeEach(() => {
  localStorage.clear();
});

describe("MessageStream 流式跟随（问题12）", () => {
  it("接近底部时新消息继续跟随到底", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 550; // 距底部 50px < 120 → 仍在跟随
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息");
    await settle();

    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("用户主动向上滚动后立即停止跟随（不被强行拉回）", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120; // 距底部 480px
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息");
    await settle();

    expect(stream.scrollTop).toBe(120);
    w.unmount();
  });

  it("用户重新滚到底部后恢复跟随", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();
    stream.scrollTop = 600;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("之后的消息");
    await settle();

    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("助手消息内容持续增长（同一条流式消息）也跟随底部", async () => {
    const { w, session, stream } = await mountStream();
    session.pushAssistant("第一段", undefined, true, true);
    await settle();
    stream.scrollTop = 550;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    // 同一条消息增长：messages.length 不变，只有 content 变长
    session.pushAssistant("第一段 + 第二段", undefined, true, true);
    await settle();

    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("向上滚动后内容增长也不被拉回", async () => {
    const { w, session, stream } = await mountStream();
    session.pushAssistant("第一段", undefined, true, true);
    await settle();
    stream.scrollTop = 100;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushAssistant("第一段 + 第二段", undefined, true, true);
    await settle();

    expect(stream.scrollTop).toBe(100);
    w.unmount();
  });

  it("轮次标签是中文（逻辑见 utils/turnLabel，这里防止组件回退成英文）", async () => {
    const { w } = await mountStream();
    // 虚拟列表在 jsdom 里不渲染条目，这里只断言组件没有把英文标签写进模板
    const src = w.html();
    expect(src).not.toContain("TURN ");
    expect(src).not.toContain(">NOW<");
    w.unmount();
  });
});

describe("MessageStream 跟随触发源（P0：只有本机发送才拉回底部）", () => {
  it("后台任务开始（turnRunning 变 true）不打断正在向上阅读的位置", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();
    expect(stream.scrollTop).toBe(120);

    session.turnRunning = true; // 等价于收到后台/排队任务的 TURN_START
    await settle();

    expect(stream.scrollTop).toBe(120);
    w.unmount();
  });

  it("本机发送后回到跟随（即使之前在上翻阅读）", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.localSendSeq += 1; // 本机发送
    await settle();

    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("离开（打开设置/星球）时记住阅读位置，回来按原位置恢复而不是甩回最新", async () => {
    const { w, session, stream, pinia } = await mountStream();
    stream.scrollTop = 250;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    w.unmount(); // 打开设置：对话页组件被卸载
    expect(session.streamScrollTop).toBe(250);
    expect(session.streamFollowing).toBe(false);

    const w2 = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream2 = w2.find(".stream").element as HTMLElement;
    shape(stream2, 1000, 400);
    await new Promise((r) => setTimeout(r, 30)); // 等恢复逻辑写完位置
    expect(stream2.scrollTop).toBe(250);
    w2.unmount();
  });

  it("离开时在跟随底部：回来仍然跟随最新", async () => {
    const { w, session, stream, pinia } = await mountStream();
    stream.scrollTop = 600;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();
    w.unmount();
    expect(session.streamFollowing).toBe(true);

    const w2 = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream2 = w2.find(".stream").element as HTMLElement;
    shape(stream2, 1000, 400);
    await new Promise((r) => setTimeout(r, 30));
    expect(stream2.scrollTop).toBe(0); // 恢复逻辑不写位置；跟随由新内容触发
    w2.unmount();
  });

  it("上翻阅读时新消息只计数，点击「回到最新消息」才滚动并恢复跟随", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息 1");
    session.pushUser("新消息 2");
    await settle();
    expect(stream.scrollTop).toBe(120); // 没有被强行拉回
    const btn = w.find(".back-latest");
    expect(btn.exists()).toBe(true);
    expect(btn.text()).toContain("回到最新消息");
    expect(btn.text()).toContain("2"); // 未读计数

    await btn.trigger("click");
    await settle();
    // 平滑滚动按帧推进：轮询等到落到底部（最长约 800ms）
    for (let i = 0; i < 40 && stream.scrollTop !== 600; i++) {
      await new Promise((r) => setTimeout(r, 20));
    }
    expect(stream.scrollTop).toBe(600);
    expect(session.streamFollowing).toBe(true);
    expect(w.find(".back-latest").exists()).toBe(false);
    w.unmount();
  });

  it("回到最新：滚动过程中内容还在变高，也要落到真正的底部（不是开始时的估算底部）", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息");
    await settle();
    // 虚拟列表在滚动中逐条测量：总高度从 1000 涨到 1600（真实底部 1200）
    setTimeout(() => shape(stream, 1600, 400), 30);
    await w.find(".back-latest").trigger("click");
    for (let i = 0; i < 60 && stream.scrollTop !== 1200; i++) {
      await new Promise((r) => setTimeout(r, 20));
    }
    expect(stream.scrollTop).toBe(1200);
    expect(session.streamFollowing).toBe(true);
    w.unmount();
  });

  it("等待布局时用户先上翻：不再执行那次安排好的贴底", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 550; // 仍在跟随区
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息"); // 触发 watcher（内部会 await nextTick）
    stream.scrollTop = 120; // 布局等待期间用户上翻
    stream.dispatchEvent(new Event("scroll"));
    await settle();

    expect(stream.scrollTop).toBe(120);
    w.unmount();
  });

  it("等待模型响应时给出诚实的阶段文案；已有助手内容后不再显示等待指示", async () => {
    const { w, session } = await mountStream();
    session.pushUser("问题");
    session.turnStarted();
    session.turnPhase = "waiting";
    await settle();
    expect(w.find(".typing").exists()).toBe(true);
    expect(w.find(".typing").text()).toContain("等待模型响应");

    session.turnPhase = "generating";
    session.pushAssistant("开始回答", undefined, true, true);
    await settle();
    expect(w.find(".typing").exists()).toBe(false);
    w.unmount();
  });

  it("消息区是可聚焦的滚动区域（键盘用户能滚动，焦点环可见）", async () => {
    const { w } = await mountStream();
    const stream = w.find(".stream");
    expect(stream.attributes("tabindex")).toBe("0");
    expect(stream.attributes("aria-label")).toBe("对话消息");
    w.unmount();
  });
});

describe("本机发送被拒绝后不留后遗症（P0：没发出去的消息不该改变阅读位置）", () => {
  /** 位置写入发生在 nextTick 之后的一帧里，等它落定 */
  async function waitFrame() {
    await new Promise((r) => setTimeout(r, 40));
    await settle();
  }

  it("上翻阅读时发送失败：位置放回发送前，且不进入跟随", async () => {
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("network down"));
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();
    expect(session.streamFollowing).toBe(false);

    const ok = await session.send("这条会失败");
    await waitFrame();

    expect(ok).toBe(false);
    expect(session.sendRejectedSeq).toBe(1);
    expect(stream.scrollTop).toBe(120); // 回到发送前的位置
    expect(session.streamFollowing).toBe(false); // 不因为一次失败就进入跟随
    expect(w.find(".back-latest").exists()).toBe(false); // 没有留下虚假的未读计数

    // 之后到达的内容同样不把阅读位置拽到底部
    session.pushUser("别人发的新消息");
    await settle();
    expect(stream.scrollTop).toBe(120);
    w.unmount();
  });

  it("发送成功时仍然跟随到底（只有失败才回原位）", async () => {
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    const ok = await session.send("正常发送");
    await waitFrame();

    expect(ok).toBe(true);
    expect(session.sendRejectedSeq).toBe(0);
    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("发送失败后用户自己滚过：不再强行放回原位", async () => {
    // 用「手动拒绝」的请求来固定时序：先让跟随发生，再让用户接管，最后才失败
    let rejectSend!: (e: Error) => void;
    vi.mocked(api.sendTurn).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectSend = reject; }),
    );
    const { w, session, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    const pending = session.send("这条会失败");
    await settle(); // 本机发送已经跟随到底（600）
    stream.scrollTop = 260;
    stream.dispatchEvent(new Event("scroll"));
    stream.dispatchEvent(new Event("wheel"));
    rejectSend(new Error("network down")); // 请求这时才失败
    await pending;
    await waitFrame();

    expect(stream.scrollTop).toBe(260);
    w.unmount();
  });
});
