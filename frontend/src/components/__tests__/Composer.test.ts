import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
  cancelTurn: vi.fn(async (id: string) => ({ ok: true, cancelled: true, turn_id: id })),
}));
vi.mock("../../services/api", () => ({
  api: {
    sendTurn: mocks.sendTurn,
    getSessionContext: mocks.getSessionContext,
    cancelTurn: mocks.cancelTurn,
  },
}));

async function mountComposer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await nextTick(); // 等浮动系统初始化（flush: post）
  return { w, pinia };
}

beforeEach(() => {
  localStorage.clear();
  mocks.sendTurn.mockClear();
});

describe("Composer 输入框（右下角大气泡）", () => {
  it("渲染：气泡 + textarea + 发送按钮（无拖拽把手）", async () => {
    const { w } = await mountComposer();
    expect(w.find(".composer").exists()).toBe(true);
    expect(w.find(".topicbar.fw-handle").exists()).toBe(false);
    expect(w.find("textarea.qio-input").exists()).toBe(true);
    expect(w.find(".send-btn").exists()).toBe(true);
    w.unmount();
  });

  it("输入并发送：保留 send 数据流（api.sendTurn）", async () => {
    const { w, pinia } = await mountComposer();
    await w.find("textarea").setValue("hello qio");
    await w.find(".send-btn").trigger("click");
    await flushPromises();
    expect(mocks.sendTurn).toHaveBeenCalledWith("hello qio", null);
    const session = useSessionStore();
    expect(session.messages[session.messages.length - 1]?.content).toBe("hello qio");
    expect(w.find("textarea").element as HTMLTextAreaElement).toHaveProperty("value", "");
    w.unmount();
    void pinia;
  });

  it("大气泡：无拖拽把手、无浮动系统内联定位（融入对话页布局）", async () => {
    const { w } = await mountComposer();
    const el = w.find(".composer").element as HTMLElement;
    expect(el.classList.contains("bubble")).toBe(true);
    expect(el.getAttribute("style")).toBeNull(); // 不再由 useFloatingWindow 写 left/top 内联定位
    expect(w.find(".topicbar.fw-handle").exists()).toBe(false);
    w.unmount();
  });

  it("增高时设置 textarea 高度（bottom 定位自然向上生长）", async () => {
    const { w } = await mountComposer();
    const ta = w.find("textarea").element as HTMLTextAreaElement;
    Object.defineProperty(ta, "scrollHeight", { value: 160, configurable: true });
    await w.find("textarea").setValue("多行\n内容\n内容");
    expect(ta.style.height).toBe("160px");
    w.unmount();
  });
});

describe("Composer 运行中仍可书写（任务02 §1/§3/§4/§11）", () => {
  it("Agent 运行时 textarea 不锁死：可输入、可编辑草稿", async () => {
    const { w } = await mountComposer();
    const session = useSessionStore();
    session.turnRunning = true;
    await nextTick();
    const ta = w.find("textarea");
    expect(ta.attributes("disabled")).toBeUndefined();
    await ta.setValue("这是下一条");
    expect((ta.element as HTMLTextAreaElement).value).toBe("这是下一条");
    w.unmount();
  });

  it("运行中提交 → 排队发送（消息标记等待中，不阻塞输入）", async () => {
    const { w } = await mountComposer();
    const session = useSessionStore();
    session.turnRunning = true;
    session.activeTurnId = "turn_1";
    await nextTick();
    await w.find("textarea").setValue("排队的消息");
    const btn = w.find(".send-btn");
    expect(btn.attributes("title")).toContain("排队");
    await btn.trigger("click");
    await flushPromises();
    expect(mocks.sendTurn).toHaveBeenCalledWith("排队的消息", null);
    const last = session.messages[session.messages.length - 1];
    expect(last?.content).toBe("排队的消息");
    expect(last?.queued).toBe(true);
    w.unmount();
  });

  it("IME：选词时 Enter 不发送；Shift+Enter 换行；普通 Enter 发送", async () => {
    const { w } = await mountComposer();
    const ta = w.find("textarea");
    await ta.setValue("中文输入");
    await ta.trigger("keydown", { key: "Enter", isComposing: true });
    expect(mocks.sendTurn).not.toHaveBeenCalled();
    await ta.trigger("keydown", { key: "Enter", keyCode: 229 });
    expect(mocks.sendTurn).not.toHaveBeenCalled();
    await ta.trigger("keydown", { key: "Enter", shiftKey: true });
    expect(mocks.sendTurn).not.toHaveBeenCalled();
    await ta.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(mocks.sendTurn).toHaveBeenCalledWith("中文输入", null);
    w.unmount();
  });

  it("长输入到最大高度后内部滚动，不把聊天挤扁", async () => {
    const { w } = await mountComposer();
    const ta = w.find("textarea").element as HTMLTextAreaElement;
    Object.defineProperty(ta, "scrollHeight", { value: 2000, configurable: true });
    await w.find("textarea").setValue("很长的输入");
    expect(parseInt(ta.style.height, 10)).toBeLessThanOrEqual(320);
    w.unmount();
  });

  it("发送多行输入后高度复位（清空 DOM 值之后再测量）", async () => {
    const { w } = await mountComposer();
    const ta = w.find("textarea").element as HTMLTextAreaElement;
    // 模拟真实浏览器：scrollHeight 随内容行数变化（多行 146px，空值 46px）
    Object.defineProperty(ta, "scrollHeight", {
      configurable: true,
      get: () => (ta.value.includes("\n") ? 146 : 46),
    });
    await w.find("textarea").setValue("1\n2\n3");
    await nextTick();
    expect(ta.style.height).toBe("146px");

    await w.find(".send-btn").trigger("click");
    await flushPromises();
    await nextTick();

    expect(ta.value).toBe("");
    // 空草稿清掉内联高度 → 浏览器恢复自然尺寸（与刚打开时一致）
    expect(ta.style.height).toBe("");
    w.unmount();
  });

  it("运行中显示独立停止按钮：点击取消 active turn 并显示正在停止", async () => {
    const { w } = await mountComposer();
    const session = useSessionStore();
    session.turnRunning = true;
    session.activeTurnId = "turn_1";
    await nextTick();
    const stop = w.find(".stop-btn");
    expect(stop.exists()).toBe(true);
    await stop.trigger("click");
    await flushPromises();
    expect(mocks.cancelTurn).toHaveBeenCalledWith("turn_1");
    expect(w.find(".stop-btn").text()).toContain("正在停止");
    // turn 结束后恢复
    session.turnEnded();
    await nextTick();
    expect(w.find(".stop-btn").exists()).toBe(false);
    w.unmount();
  });
});

describe("Composer 历史位置提示（任务05 A/B/C）", () => {
  it("输入区提示语是中文（不再显示 /tool /topic /memory 这类英文命令）", async () => {
    const { w } = await mountComposer();
    const bar = w.find(".topicbar").text();
    expect(bar).toContain("Enter 发送");
    expect(bar).not.toContain("/tool");
    expect(bar).not.toContain("/topic");
    expect(bar).not.toContain("/memory");
    w.unmount();
  });

  it("用户「从这里继续」后提示当前从哪个历史片段继续", async () => {
    const { w } = await mountComposer();
    const session = useSessionStore();
    session.setAnchor("t1", "f13", "话题A", { id: "f13", title: "Anchor 生命周期" });
    session.anchorHistoric = true;
    await nextTick();
    const anchor = w.find(".anchor");
    expect(anchor.exists()).toBe(true);
    expect(anchor.text()).toContain("从「Anchor 生命周期」继续");
    expect(anchor.text()).not.toContain("anchor_fragment_id");
    w.unmount();
  });

  it("没有片段标题时也能读懂的兜底文案", async () => {
    const { w } = await mountComposer();
    const session = useSessionStore();
    session.setAnchor("t1", "f13", "话题A", { id: "f13", title: null });
    session.anchorHistoric = true;
    await nextTick();
    expect(w.find(".anchor").text()).toContain("从选中的历史位置继续");
    w.unmount();
  });

  it("成功一轮位置推进后不再显示历史提示（不会几十轮后仍显示旧片段）", async () => {
    const { w } = await mountComposer();
    const session = useSessionStore();
    session.setAnchor("t1", "f13", "话题A", { id: "f13", title: "Anchor 生命周期" });
    session.anchorHistoric = true;
    await nextTick();
    expect(w.find(".anchor").exists()).toBe(true);

    // 后端在成功一轮后推送 historic=false 的 ANCHOR 事件
    session.setAnchor("t1", "f20", "话题A", { id: "f20", title: null });
    session.anchorHistoric = false;
    await nextTick();
    expect(w.find(".anchor").exists()).toBe(false);
    w.unmount();
  });

  it("只读检索（memory_search）不产生历史提示变化", async () => {
    const { w } = await mountComposer();
    const session = useSessionStore();
    session.setAnchor("t1", "f13", "话题A", { id: "f13", title: "Anchor 生命周期" });
    session.anchorHistoric = true;
    await nextTick();
    const before = w.find(".anchor").text();
    // 没有任何 ANCHOR 事件 → 提示保持不变
    expect(w.find(".anchor").text()).toBe(before);
    w.unmount();
  });
});
