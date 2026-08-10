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
  it("渲染：header 拖拽把手 + textarea + 发送按钮（记忆滑块已移除）", async () => {
    const { w } = await mountComposer();
    expect(w.find(".composer").exists()).toBe(true);
    expect(w.find(".topicbar.fw-handle").exists()).toBe(true);
    expect(w.find("textarea.qio-input").exists()).toBe(true);
    expect(w.find(".send-btn").exists()).toBe(true);
    expect(w.find(".strength-slider").exists()).toBe(false);
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
  it("增高时向上生长：保持底边不动（非顶部贴靠）", async () => {
    const { w } = await mountComposer();
    const root = w.find(".composer").element as HTMLElement;
    const ta = w.find("textarea").element as HTMLTextAreaElement;
    root.style.top = "500px";
    // 模拟布局：offsetHeight 随 textarea 高度级联（jsdom 无布局，用 getter 模拟）
    Object.defineProperty(ta, "scrollHeight", { value: 160, configurable: true });
    Object.defineProperty(root, "offsetHeight", {
      get: () => parseInt(ta.style.height, 10) || 120,
      configurable: true,
    });
    await w.find("textarea").setValue("多行\n内容\n内容");
    expect(root.style.top).toBe("460px"); // 底边 500+120=620 保持，向上生长 160-120=40
    w.unmount();
  });

  it("顶部贴靠时增高向下生长（保持顶边不动）", async () => {
    const { floatingState } = await import("../../composables/floatingState");
    const { w } = await mountComposer();
    floatingState.composer.dockedTo = "top";
    const root = w.find(".composer").element as HTMLElement;
    const ta = w.find("textarea").element as HTMLTextAreaElement;
    root.style.top = "10px";
    Object.defineProperty(ta, "scrollHeight", { value: 160, configurable: true });
    Object.defineProperty(root, "offsetHeight", {
      get: () => parseInt(ta.style.height, 10) || 120,
      configurable: true,
    });
    await w.find("textarea").setValue("多行\n内容\n内容");
    expect(root.style.top).toBe("10px");
    floatingState.composer.dockedTo = null;
    w.unmount();
  });

  it("贴靠隐藏：hidden 时挂 fw-hidden + 贴靠方向类（hover 展开由 CSS :hover 处理）", async () => {
    const { floatingState } = await import("../../composables/floatingState");
    const { w } = await mountComposer();
    const el = w.find(".composer");
    floatingState.composer.dockedTo = "top";
    floatingState.composer.hidden = true;
    await nextTick();
    expect(el.classes()).toContain("fw-hidden");
    expect(el.classes()).toContain("dock-top");
    // 还原状态避免影响后续用例
    floatingState.composer.hidden = false;
    floatingState.composer.dockedTo = null;
    await nextTick();
    expect(el.classes()).not.toContain("fw-hidden");
    w.unmount();
  });
});


