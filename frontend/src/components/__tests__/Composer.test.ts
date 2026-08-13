import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(async () => ({ ok: true, topic_id: null })),
  getSessionContext: vi.fn(async () => ({ topic_id: "", topic_name: null, anchor_fragment: null, messages: [] })),
}));
vi.mock("../../services/api", () => ({
  api: { sendTurn: mocks.sendTurn, getSessionContext: mocks.getSessionContext },
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

  it("固定气泡：无拖拽把手、无浮动系统内联定位（left/top）", async () => {
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


