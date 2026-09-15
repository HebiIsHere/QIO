import { describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ConversationView from "../ConversationView.vue";
import { useSessionStore } from "../../stores/session";

vi.mock("../../services/api", () => ({
  api: {
    loadHistory: vi.fn(async () => ({ messages: [], anchor: null })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50 })),
  },
}));

const dockStub = {
  name: "PlanetDock",
  emits: ["open"],
  template: `<button class="stub-dock" @click="$emit('open')">dock</button>`,
};
const planetStub = {
  name: "PlanetView",
  props: { seq: { type: Number, default: 0 } },
  emits: ["close"],
  template: `<div class="stub-planet" :data-seq="seq"></div>`,
};

function mountView() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(ConversationView, {
    global: {
      plugins: [pinia],
      stubs: {
        MessageStream: true,
        Composer: true,
        SettingsFloat: true,
        PlanetDock: dockStub,
        PlanetView: planetStub,
      },
    },
  });
}

describe("待确认切换条（spec 第 29~30 条）", () => {
  it("没有建议时不出现", () => {
    const w = mountView();
    expect(w.find(".topic-switch").exists()).toBe(false);
  });

  it("有建议时出现在输入区附近，并显示目标话题名", async () => {
    const w = mountView();
    const session = useSessionStore();
    session.setPendingSwitch({ topicId: "t2", topicName: "顺丁橡胶降解" });

    await w.vm.$nextTick();

    const prompt = w.find(".topic-switch");
    expect(prompt.exists()).toBe(true);
    expect(prompt.text()).toContain("顺丁橡胶降解");
    expect(prompt.text()).toContain("保留当前");
  });
});

describe("ConversationView 星球层开合（任务05 A：旧回调不得关闭新页面）", () => {
  it("关闭动画期间的旧 close 回调不会关掉后来重新打开的星球", async () => {
    const w = mountView();
    await flushPromises();
    expect(w.find(".stub-planet").exists()).toBe(false);

    await w.find(".stub-dock").trigger("click");
    await flushPromises();
    expect(w.find(".stub-planet").exists()).toBe(true);
    const firstSeq = Number(w.find(".stub-planet").attributes("data-seq"));

    // 关闭动画进行中用户又点了一次入口（此时覆盖层已不再拦截点击）
    await w.find(".stub-dock").trigger("click");
    await flushPromises();
    const secondSeq = Number(w.find(".stub-planet").attributes("data-seq"));
    expect(secondSeq).toBeGreaterThan(firstSeq);

    // 第一次关闭的迟到回调不得关掉现在这一层
    await w.findComponent({ name: "PlanetView" }).vm.$emit("close", firstSeq);
    await flushPromises();
    expect(w.find(".stub-planet").attributes("style") ?? "").not.toContain("display: none");

    // 当前这一层自己的 close 才生效
    await w.findComponent({ name: "PlanetView" }).vm.$emit("close", secondSeq);
    await flushPromises();
    expect(w.find(".stub-planet").exists()).toBe(true); // 实例常驻（复用同一个 WebGL 场景）
    expect(w.find(".stub-planet").attributes("style") ?? "").toContain("display: none");
    // 再打开时仍是同一个实例（序号只是递增，不会重建组件）
    await w.find(".stub-dock").trigger("click");
    await flushPromises();
    expect(w.find(".stub-planet").attributes("style") ?? "").not.toContain("display: none");
    w.unmount();
  });
});

describe("ConversationView 键盘通道（任务 05 I：长消息流不该挡住输入框）", () => {
  it("第一个焦点位是「跳到输入框」，点击后焦点落到输入框", async () => {
    // 真实页面里输入框由 Composer 渲染；这里用同名元素代替，验证跳转逻辑本身
    const target = document.createElement("textarea");
    target.id = "composer-input";
    document.body.appendChild(target);

    const w = mountView();
    await flushPromises();

    const skip = w.find("a.skip-link");
    expect(skip.exists()).toBe(true);
    expect(skip.text()).toContain("跳到输入框");
    expect(skip.attributes("href")).toBe("#composer-input");
    // 必须是 DOM 里的第一个可聚焦元素：否则长消息流会把它挤到后面
    expect(w.element.firstElementChild?.classList.contains("skip-link")).toBe(true);

    await skip.trigger("click");
    expect(document.activeElement).toBe(target);

    w.unmount();
    target.remove();
  });
});
