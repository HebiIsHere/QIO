/**
 * 管理区（A 交付 §3）的结构与导航测试。
 *
 * 守四件事：
 * 1. 三标签 + 待处理 / 整理记录都占据同一块管理内容区域，切换保留各自状态（v-show 保活）；
 * 2. 「返回」恢复进入前的标签页；搜索关闭恢复进入搜索前的上下文；
 * 3. 定位靠真实 id：搜索结果 / 记忆页关联条目 → 切标签页 + focusId（B 组件契约）；
 * 4. 待处理数量来自真实 store（不是装饰数字）。
 *
 * B 的面板组件在这里用替身：它们的内部行为由 B 自己的测试覆盖，
 * 这里只验证「管理区怎么接它们」——prop / emit / 显示哪一个视图。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent, onMounted } from "vue";
import ManagementArea from "../ManagementArea.vue";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listTopics: vi.fn(async () => ({ topics: [] })),
    getTopicDetail: vi.fn(async () => ({})),
    listMemoryPending: vi.fn(async () => ({ items: [], total: 3, truncated: false, maintenance_available: true })),
  },
}));
vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

const knowledgeMounts: string[] = [];
const knowledgeStub = defineComponent({
  name: "KnowledgePanel",
  props: { focusId: { type: String, default: null } },
  emits: ["focus-topic"],
  setup() {
    onMounted(() => knowledgeMounts.push("knowledge"));
    return () => null;
  },
});
const entityStub = defineComponent({
  name: "EntityPanel",
  props: { focusId: { type: String, default: null }, openCardId: String, openByNodeId: String },
  emits: ["changed"],
  setup: () => () => null,
});
const pendingStub = defineComponent({
  name: "MemoryPendingPanel",
  props: { focusPendingId: { type: String, default: null }, focusId: { type: String, default: null } },
  setup: () => () => null,
});
const searchStub = defineComponent({
  name: "GlobalSearchPanel",
  props: { active: { type: Boolean, default: false }, debounceMs: Number },
  emits: ["open"],
  setup: () => () => null,
});

function mountArea(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(ManagementArea, {
    props: { open: true, ...props },
    global: {
      plugins: [pinia],
      stubs: {
        KnowledgePanel: knowledgeStub,
        EntityPanel: entityStub,
        MemoryPendingPanel: pendingStub,
        GlobalSearchPanel: searchStub,
      },
    },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  knowledgeMounts.length = 0;
  mocks.apiMock.listTopics.mockResolvedValue({ topics: [] });
  mocks.apiMock.listMemoryPending.mockResolvedValue({ items: [], total: 3, truncated: false, maintenance_available: true });
});

describe("管理区：三标签 + 状态保活", () => {
  it("三个标签是 记忆 / 知识 / 实体（同一块内容区域，不是三栏并列）", async () => {
    const w = mountArea();
    await flushPromises();
    expect(w.attributes("data-tab")).toBe("memory");
    expect(w.attributes("data-view")).toBe("tabs");
    const tabs = w.findAll("[data-mp-tab]").map((n) => n.attributes("data-mp-tab"));
    expect(tabs).toEqual(["memory", "knowledge", "entity"]);
    expect(w.find('[data-view-panel="memory"]').exists()).toBe(true);
  });

  it("切换标签页：首次激活才挂载，切回来状态还在（不重新挂载）", async () => {
    const w = mountArea();
    await flushPromises();
    await w.find('[data-mp-tab="memory"]').trigger("click");
    await flushPromises();
    await w.find(".mt-list-head input").setValue("保留我");
    await flushPromises();

    await w.find('[data-mp-tab="knowledge"]').trigger("click");
    await flushPromises();
    expect(w.attributes("data-tab")).toBe("knowledge");
    expect(knowledgeMounts).toEqual(["knowledge"]);

    await w.find('[data-mp-tab="memory"]').trigger("click");
    await flushPromises();
    expect((w.find(".mt-list-head input").element as HTMLInputElement).value).toBe("保留我");

    await w.find('[data-mp-tab="knowledge"]').trigger("click");
    await flushPromises();
    expect(knowledgeMounts).toEqual(["knowledge"]); // 只挂载过一次
  });

  it("切换标签页不触发任何星球动作（只上报状态，父级据此渲染只读属性）", async () => {
    const w = mountArea();
    await flushPromises();
    await w.find('[data-mp-tab="entity"]').trigger("click");
    await flushPromises();
    const states = w.emitted("state")!;
    expect(states[states.length - 1]).toEqual([{ tab: "entity", view: "tabs" }]);
  });
});

describe("管理区：待处理 / 整理记录", () => {
  it("待处理入口带真实未处理数量（来自 store）", async () => {
    const w = mountArea();
    await flushPromises();
    const entry = w.find('[data-mp-aux="pending"]');
    expect(entry.exists()).toBe(true);
    expect(entry.attributes("data-mp-pending-count")).toBe("3");
    expect(entry.text()).toContain("3");
  });

  it("进入辅助视图后返回，恢复进入前的标签页", async () => {
    const w = mountArea();
    await flushPromises();
    await w.find('[data-mp-tab="knowledge"]').trigger("click");
    await flushPromises();

    await w.find('[data-mp-aux="records"]').trigger("click");
    await flushPromises();
    expect(w.attributes("data-view")).toBe("records");
    expect(w.find('[data-view-panel="records"]').exists()).toBe(true);

    await w.find(".ma-back").trigger("click");
    await flushPromises();
    expect(w.attributes("data-view")).toBe("tabs");
    expect(w.attributes("data-tab")).toBe("knowledge");
  });
});

describe("管理区：搜索结果与定位", () => {
  it("打开搜索 → 点结果：切到对应标签页并传 focusId 给 B 组件", async () => {
    const w = mountArea({ searchTicket: 1 });
    await flushPromises();
    expect(w.attributes("data-view")).toBe("search");
    expect(w.findComponent(searchStub).props("active")).toBe(true);

    w.findComponent(searchStub).vm.$emit("open", { kind: "knowledge", id: "k1", title: "知识", note: "" });
    await flushPromises();
    expect(w.attributes("data-view")).toBe("tabs");
    expect(w.attributes("data-tab")).toBe("knowledge");
    expect(w.findComponent(knowledgeStub).props("focusId")).toBe("k1");
    // 出现「返回搜索结果」，点击后回到搜索视图（关键词/结果/滚动都还在）
    const back = w.find("[data-mp-search-return]");
    expect(back.exists()).toBe(true);
    await back.trigger("click");
    await flushPromises();
    expect(w.attributes("data-view")).toBe("search");
  });

  it("记忆页里的关联实体 → 切到实体标签并定位该 id", async () => {
    const w = mountArea();
    await flushPromises();
    // 记忆页的 emit 经管理区转成「切标签 + focusId」
    w.findComponent({ name: "MemoryTab" }).vm.$emit("focus-entity", "n1");
    await flushPromises();
    expect(w.attributes("data-tab")).toBe("entity");
    expect(w.findComponent(entityStub).props("focusId")).toBe("n1");
  });

  it("记忆页里的关联知识 → 切到知识标签并定位该 id", async () => {
    const w = mountArea();
    await flushPromises();
    w.findComponent({ name: "MemoryTab" }).vm.$emit("focus-knowledge", "kn_1");
    await flushPromises();
    expect(w.attributes("data-tab")).toBe("knowledge");
    expect(w.findComponent(knowledgeStub).props("focusId")).toBe("kn_1");
  });

  it("关闭搜索恢复进入搜索前的管理上下文", async () => {
    const w = mountArea();
    await flushPromises();
    await w.find('[data-mp-tab="entity"]').trigger("click");
    await flushPromises();
    await w.setProps({ searchTicket: 1 });
    await flushPromises();
    expect(w.attributes("data-view")).toBe("search");

    await w.find('[aria-label="关闭搜索"]').trigger("click");
    await flushPromises();
    expect(w.attributes("data-view")).toBe("tabs");
    expect(w.attributes("data-tab")).toBe("entity");
  });
});

describe("管理区：外部落点（深链 / 记忆提醒）", () => {
  it("initialTab=memory（外部语义 = 待处理）落到待处理视图并传事项 id", async () => {
    const w = mountArea({ requestView: "memory", requestFocusId: "p1", requestSeq: 1 });
    await flushPromises();
    expect(w.attributes("data-view")).toBe("pending");
    await flushPromises();
    expect(w.findComponent(pendingStub).props("focusPendingId")).toBe("p1");
  });

  it("initialTab=entity 落到实体标签", async () => {
    const w = mountArea({ requestView: "entity", requestSeq: 2 });
    await flushPromises();
    expect(w.attributes("data-tab")).toBe("entity");
    expect(w.attributes("data-view")).toBe("tabs");
  });

  it("没有请求代号时，不会把默认值当成一次请求", async () => {
    const w = mountArea();
    await flushPromises();
    expect(w.attributes("data-view")).toBe("tabs");
    expect(w.attributes("data-tab")).toBe("memory");
  });
});
