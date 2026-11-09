import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";
import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";
import { useChatViewportStore, type SavedReadingPosition } from "../../stores/chatViewport";
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

/** 构造一个保存过的阅读位置（测试只关心恢复策略，不伪造真实布局）。 */
function savedPosition(overrides: Partial<SavedReadingPosition> = {}): SavedReadingPosition {
  return {
    topicId: null,
    turnKey: "",
    anchorKey: null,
    anchorOffset: 0,
    turnOffset: 0,
    textSnippet: null,
    textOffset: null,
    scrollTop: 0,
    following: false,
    messageCount: 0,
    lastMessageId: null,
    savedAt: Date.now(),
    viewportWidth: 1000,
    ...overrides,
  };
}

async function mountStream(): Promise<{
  w: ReturnType<typeof mount>;
  session: ReturnType<typeof useSessionStore>;
  viewport: ReturnType<typeof useChatViewportStore>;
  stream: HTMLElement;
  pinia: Pinia;
}> {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  const viewport = useChatViewportStore();
  const w = mount(MessageStream, { global: { plugins: [pinia] } });
  await nextTick();
  const stream = w.find(".stream").element as HTMLElement;
  shape(stream, 1000, 400); // 底部 = scrollTop 600
  return { w, session, viewport, stream, pinia };
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
    session.pushAssistant("第一段", true, true);
    await settle();
    stream.scrollTop = 550;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    // 同一条消息增长：messages.length 不变，只有 content 变长
    session.pushAssistant("第一段 + 第二段", true, true);
    await settle();

    expect(stream.scrollTop).toBe(600);
    w.unmount();
  });

  it("向上滚动后内容增长也不被拉回", async () => {
    const { w, session, stream } = await mountStream();
    session.pushAssistant("第一段", true, true);
    await settle();
    stream.scrollTop = 100;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushAssistant("第一段 + 第二段", true, true);
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
    // 修复 §二.1：离开前在底部 → 返回后主动贴底并继续跟随，不依赖「之后再来一条消息」
    await new Promise((r) => setTimeout(r, 60));
    expect(stream2.scrollTop).toBe(600);
    expect(session.streamFollowing).toBe(true);
    w2.unmount();
  });

  it("上翻阅读时新消息只计数，入口请求「回到最新」才滚动并恢复跟随", async () => {
    const { w, session, viewport, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息 1");
    session.pushUser("新消息 2");
    await settle();
    expect(stream.scrollTop).toBe(120); // 没有被强行拉回
    // 入口行（ComposerEntries）读的是同一份共享状态；这里验证状态与消费链路
    expect(viewport.following).toBe(false);
    expect(viewport.unseen).toBe(2);

    viewport.requestBackToLatest();
    await settle();
    // 平滑滚动按帧推进：轮询等到落到底部（最长约 800ms）
    for (let i = 0; i < 40 && stream.scrollTop !== 600; i++) {
      await new Promise((r) => setTimeout(r, 20));
    }
    expect(stream.scrollTop).toBe(600);
    expect(session.streamFollowing).toBe(true);
    expect(viewport.following).toBe(true);
    expect(viewport.unseen).toBe(0);
    w.unmount();
  });

  it("回到最新的平滑滚动可被用户滚动中断（用户操作优先）", async () => {
    const { w, viewport, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    viewport.requestBackToLatest();
    await nextTick();
    // 动画进行中用户自己滚动：立刻中断，不再把位置拉回底部
    stream.dispatchEvent(new Event("wheel"));
    stream.scrollTop = 200;
    stream.dispatchEvent(new Event("scroll"));
    await new Promise((r) => setTimeout(r, 200));
    expect(stream.scrollTop).toBe(200);
    expect(viewport.following).toBe(false);
    w.unmount();
  });

  it("回到最新：滚动过程中内容还在变高，也要落到真正的底部（不是开始时的估算底部）", async () => {
    const { w, session, viewport, stream } = await mountStream();
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await nextTick();

    session.pushUser("新消息");
    await settle();
    // 虚拟列表在滚动中逐条测量：总高度从 1000 涨到 1600（真实底部 1200）
    setTimeout(() => shape(stream, 1600, 400), 30);
    viewport.requestBackToLatest();
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

  /**
   * 契约变更（2026-10-06 §1.5 归并，Lead 裁决「状态重复必须收口」）：
   *
   * 这一轮的运行状态由**过程区状态行**表达；当前轮已经有过程区时，
   * 全局状态条不再显示同一条状态（旧断言期望这里仍出现「正在处理」）。
   * 全局条只保留过程区覆盖不到的情况：排队等待 / 独立任务 / 系统通知 / 没有过程区的旧记录。
   */
  it("当前轮已有过程区：全局状态条不再重复「正在处理」；覆盖不到的状态仍然显示", async () => {
    const { w, session } = await mountStream();
    session.pushUser("问题");
    session.turnStarted();
    session.turnPhase = "waiting";
    await settle();
    // 过程区（TurnProcess 的状态行）在说「正在处理」：全局条让位
    expect(w.find(".typing").exists()).toBe(false);

    // 过程区覆盖不到：独立任务 / 系统通知
    session.activity = "subagent";
    await settle();
    expect(w.find(".typing").text()).toContain("正在处理独立任务");
    session.activity = "notify";
    await settle();
    expect(w.find(".typing").text()).toContain("正在整理独立任务的结果");

    // 没有在跑的轮 = 没有过程区：等待 / 排队仍由全局条表达
    session.turnRunning = false;
    session.activity = "waiting";
    await settle();
    expect(w.find(".typing").text()).toContain("正在处理");

    // 生成中本来就不重复
    session.turnRunning = true;
    session.turnPhase = "generating";
    session.activity = "generating";
    session.pushAssistant("开始回答", true, true);
    await settle();
    expect(w.find(".typing").exists()).toBe(false);
    w.unmount();
  });

  /**
   * 第三阶段 spec 第 35~38、90 条：用户要知道 QIO 现在在做什么，
   * 但不能出现内部事件名，也不能把几种状态同时堆在页面上。
   */
  it("工具运行不再有机械整体提示；审批 / 独立任务仍各有一句中文状态，且不出现内部事件名", async () => {
    const { w, session } = await mountStream();
    session.pushUser("问题");
    session.turnStarted();

    // 「正在使用工具」是第四阶段要替换掉的机械提示：过程改由执行叙事表达，
    // 工具自己的状态由工具卡承担（spec 2026-09-22）。
    session.activity = "tool";
    await settle();
    expect(w.find(".typing").exists()).toBe(false);

    session.activity = "approval";
    await settle();
    expect(w.find(".typing").text()).toContain("等待你确认");

    session.activity = "subagent";
    await settle();
    expect(w.find(".typing").text()).toContain("正在处理独立任务");

    session.activity = "notify";
    await settle();
    expect(w.find(".typing").text()).toContain("正在整理独立任务的结果");

    const text = w.find(".stream").text();
    for (const internal of ["TOOL_RUNNING", "TOOL_START", "MODEL_WAIT", "SUBAGENT_PENDING", "TURN_END"]) {
      expect(text).not.toContain(internal);
    }
    w.unmount();
  });

  it("消息区是可聚焦的滚动区域（键盘用户能滚动，焦点环可见）", async () => {
    const { w } = await mountStream();
    const stream = w.find(".stream");
    expect(stream.attributes("tabindex")).toBe("0");
    expect(stream.attributes("aria-label")).toBe("对话消息");
    w.unmount();
  });

  it("消息区是唯一的主要纵向滚动视口：对话根带 data-qio-scope，滚动条走共享引擎（仅纵向）", async () => {
    const { w, stream } = await mountStream();
    // 引擎把测量合并到一帧；滚动一次（用户滚动也会这样）让它立刻量一次
    stream.dispatchEvent(new Event("scroll"));
    await new Promise((r) => setTimeout(r, 40));
    await nextTick();
    // 对话根：整套对话适配（长 URL 换行 / 代码软换行 / 媒体按宽度适配）挂在这里
    expect(stream.getAttribute("data-qio-scope")).toBe("chat");
    // 原生滚动条样式不再由本组件声明（全局统一消除），改用共享指示条
    expect(stream.classList.contains("qio-scroll-host")).toBe(true);
    expect(stream.dataset.qioScroll).toBe("auto");

    // 纵向溢出 → 允许出现纵向指示条；横向不声明 → 不出现横向指示条
    expect(stream.dataset.qioRange).toBe("y");
    expect(stream.querySelector('.qio-scroll-rail[data-axis="y"]')).not.toBeNull();
    expect(stream.querySelector('.qio-scroll-rail[data-axis="x"]')).toBeNull();
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
    // 契约 5：明确拒绝 = 后端 4xx（无响应失败现在是「正在确认」，不撤消息）。
    vi.mocked(api.sendTurn).mockRejectedValueOnce(Object.assign(new Error("network down"), { status: 400 }));
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
    expect(useChatViewportStore().unseen).toBe(0); // 没有留下虚假的未读计数

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
/**
 * 阅读位置恢复（修复提示词 §二）：稳定身份、有界校正、用户优先、话题隔离。
 *
 * jsdom 没有布局、虚拟列表也不渲染真实轮次，所以「按锚点校正到同一内容位置」的
 * 真实虚拟列表行为由浏览器回归场景覆盖；这里钉住恢复策略、取消条件与回退说明。
 */
describe("MessageStream 阅读位置恢复（§二）", () => {
  it("恢复期间用户先滚动：恢复与校正立即作废，不再把位置拉回", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const viewport = useChatViewportStore();
    session.currentTopicId = "t1";
    viewport.savePosition(
      savedPosition({ topicId: "t1", following: true, scrollTop: 0 }),
    );
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream = w.find(".stream").element as HTMLElement;
    shape(stream, 1000, 400);
    // 贴底恢复还在进行时用户自己滚动
    stream.scrollTop = 120;
    stream.dispatchEvent(new Event("scroll"));
    await new Promise((r) => setTimeout(r, 80));
    expect(stream.scrollTop).toBe(120);
    expect(session.streamFollowing).toBe(false);
    expect(viewport.following).toBe(false);
    w.unmount();
  });

  it("离开期间新增消息：未读数量按保存时最后一条消息计算", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const viewport = useChatViewportStore();
    session.currentTopicId = "t1";
    session.pushUser("旧消息 1");
    session.pushUser("旧消息 2");
    const lastId = session.messages[session.messages.length - 1]?.id ?? null;
    viewport.savePosition(
      savedPosition({
        topicId: "t1",
        following: false,
        messageCount: session.messages.length,
        lastMessageId: lastId,
      }),
    );
    // 离开期间来了两条新消息
    session.pushUser("新消息 1");
    session.pushUser("新消息 2");
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream = w.find(".stream").element as HTMLElement;
    shape(stream, 1000, 400);
    await new Promise((r) => setTimeout(r, 60));
    expect(viewport.following).toBe(false);
    expect(viewport.unseen).toBe(2);
    expect(stream.scrollTop).toBe(0);
    w.unmount();
  });

  it("原阅读位置不在已加载范围：回退到最近可用内容并简短说明（不静默跳到底部）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const viewport = useChatViewportStore();
    session.currentTopicId = "t1";
    session.pushUser("现在能看到的消息");
    viewport.savePosition(
      savedPosition({
        topicId: "t1",
        turnKey: "turn_no_longer_loaded",
        following: false,
        scrollTop: 300,
      }),
    );
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream = w.find(".stream").element as HTMLElement;
    shape(stream, 1000, 400);
    await new Promise((r) => setTimeout(r, 60));
    const note = w.find('[data-test="restore-note"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toContain("原来的阅读位置");
    expect(stream.scrollTop).toBe(0);
    expect(viewport.following).toBe(false);
    w.unmount();
  });

  it("恢复按话题隔离：别的话题保存的位置不会套用到当前话题", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    const viewport = useChatViewportStore();
    session.currentTopicId = "topic_b";
    viewport.savePosition(
      savedPosition({
        topicId: "topic_a",
        turnKey: "turn_a",
        following: false,
        scrollTop: 250,
      }),
    );
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    const stream = w.find(".stream").element as HTMLElement;
    shape(stream, 1000, 400);
    await new Promise((r) => setTimeout(r, 60));
    // 当前话题没有保存位置：按跟随底部处理，不拿另一个话题的位置跳过去
    expect(stream.scrollTop).toBe(600);
    expect(viewport.following).toBe(true);
    expect(w.find('[data-test="restore-note"]').exists()).toBe(false);
    w.unmount();
  });
});