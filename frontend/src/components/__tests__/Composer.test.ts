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

describe("Composer 输入框（浮动窗口）", () => {
  it("渲染：header 拖拽把手 + textarea + 发送按钮 + 记忆滑块", async () => {
    const { w } = await mountComposer();
    expect(w.find(".composer").exists()).toBe(true);
    expect(w.find(".topicbar.fw-handle").exists()).toBe(true);
    expect(w.find("textarea.qio-input").exists()).toBe(true);
    expect(w.find(".send-btn").exists()).toBe(true);
    expect(w.find(".strength-slider").exists()).toBe(true);
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

  it("贴边：初始位置为底部居中（像素 left/top，position fixed）", async () => {
    const { w } = await mountComposer();
    const el = w.find(".composer").element as HTMLElement;
    expect(el.style.position).toBe("fixed");
    expect(el.style.left).toMatch(/^\d+px$/);
    expect(el.style.top).toMatch(/^\d+px$/);
    w.unmount();
  });
});


