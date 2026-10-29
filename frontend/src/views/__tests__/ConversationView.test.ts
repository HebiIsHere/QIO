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
  props: { seq: { type: Number, default: 0 }, open: { type: Boolean, default: true } },
  emits: ["close"],
  // 暴露 open：关闭不再是「display:none」，而是切换成入口球态（由星球页自己按 open 决定）
  template: `<div class="stub-planet" :data-seq="seq" :data-open="String(open)"></div>`,
};
/**
 * v2：五入口与模式切换通过 Composer 的命名插槽进入输入气泡。
 * 测试里用真实插槽结构的替身，才能验证「输入区附近的入口」这一行为。
 */
const composerStub = {
  name: "Composer",
  template: `<div class="stub-composer">
    <slot name="entries"></slot>
    <slot name="leading"></slot>
    <textarea id="composer-input"></textarea>
  </div>`,
};

function mountView() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(ConversationView, {
    global: {
      plugins: [pinia],
      stubs: {
        MessageStream: true,
        Composer: composerStub,
        PlanetDock: dockStub,
        PlanetView: planetStub,
      },
    },
  });
}

describe("话题切换建议（spec 第 29~30 条，v2 为输入区上方入口③）", () => {
  it("没有建议时不出现该入口", () => {
    const w = mountView();
    expect(w.find('[data-entry="topicSwitch"]').exists()).toBe(false);
  });

  it("有建议时入口出现在输入区附近，展开后显示目标话题名与两个选择", async () => {
    const w = mountView();
    const session = useSessionStore();
    session.setPendingSwitch({ topicId: "t2", topicName: "顺丁橡胶降解" });

    await w.vm.$nextTick();
    await w.vm.$nextTick();

    const entry = w.find('[data-entry="topicSwitch"]');
    expect(entry.exists()).toBe(true);

    await entry.trigger("click");
    const panel = w.find('[data-section="topic-switch"]');
    expect(panel.exists()).toBe(true);
    expect(panel.text()).toContain("顺丁橡胶降解");
    expect(panel.text()).toContain("保留当前");
    w.unmount();
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
    expect(w.find(".stub-planet").attributes("data-open")).toBe("true");

    // 当前这一层自己的 close 才生效
    await w.findComponent({ name: "PlanetView" }).vm.$emit("close", secondSeq);
    await flushPromises();
    expect(w.find(".stub-planet").exists()).toBe(true); // 实例常驻（复用同一个 WebGL 场景）
    // 关掉 = 回到入口球态（星球页自己按 open=false 切），不是把组件卸载
    expect(w.find(".stub-planet").attributes("data-open")).toBe("false");
    // 再打开时仍是同一个实例（序号只是递增，不会重建组件）
    await w.find(".stub-dock").trigger("click");
    await flushPromises();
    expect(w.find(".stub-planet").attributes("data-open")).toBe("true");
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
    // v2：跳转入口随对话列一起渲染，必须是对话列里的第一个元素，
    // 否则长消息流仍会把键盘用户挡在复制按钮之间。
    const chatColumn = w.find(".chat-column").element;
    expect(chatColumn.firstElementChild?.classList.contains("skip-link")).toBe(true);

    await skip.trigger("click");
    expect(document.activeElement).toBe(target);

    w.unmount();
    target.remove();
  });
});

describe("ConversationView 顶部状态行与底部候选列表（统一滚动/宽度规则）", () => {
  it("错误原文完整显示（不再单行截断），状态行在宽度链上", async () => {
    const w = mountView();
    const session = useSessionStore();
    const long = "这一轮执行失败：" + "导致失败的完整原因".repeat(20);
    session.lastError = long;
    await w.vm.$nextTick();

    const notice = w.find(".notice.err");
    expect(notice.exists()).toBe(true);
    expect(notice.attributes("data-qio-fit")).not.toBeUndefined();
    // 全文都在 DOM 里：读屏与视觉读到的都是同一句话
    expect(notice.find(".text").text()).toBe(long);
    w.unmount();
  });

  it("知识候选卡不再在主对话挂载（数据保留，由统一审批层 / 记忆星球各自处理）", async () => {
    const w = mountView();
    const session = useSessionStore();
    session.knowledgeCandidates = [{ knowledgeId: "k1", category: "fact", content: "候选内容" }];
    await w.vm.$nextTick();
    await w.vm.$nextTick();

    // 生产挂载已移除：主对话里没有候选卡区（也不是「假装没有数据」——store 里的候选仍在）
    expect(w.find(".candidates").exists()).toBe(false);
    expect(w.find(".candidate.qio-card").exists()).toBe(false);
    expect(session.knowledgeCandidates).toHaveLength(1);
    w.unmount();
  });
});
