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
  name: "PendingItems",
  props: { focusId: { type: String, default: null } },
  emits: ["changed"],
  setup: () => () => null,
});
const recordsStub = defineComponent({
  name: "MaintenanceRecords",
  props: { focusBatchId: { type: String, default: null } },
  emits: ["changed"],
  setup: () => () => null,
});
const searchStub = defineComponent({
  name: "GlobalSearchPanel",
  props: { active: { type: Boolean, default: false }, debounceMs: Number },
  emits: ["open"],
  setup: () => () => null,
});

/**
 * 知识面板替身（带跨分支接口）：hasUnsavedDraft / requestOpenKnowledge 由 knowledge 分支
 * 用 defineExpose 提供；管理区把它们写成可选调用，这里替身用于验证接线顺序。
 */
const knowledgeApi = {
  hasUnsavedDraft: vi.fn((): boolean => false),
  requestOpenKnowledge: vi.fn(async (_id: string): Promise<boolean> => true),
};
const knowledgeApiStub = defineComponent({
  name: "KnowledgePanel",
  props: { focusId: { type: String, default: null } },
  emits: ["focus-topic", "changed"],
  setup(_props, { expose }) {
    expose({
      hasUnsavedDraft: () => knowledgeApi.hasUnsavedDraft(),
      requestOpenKnowledge: (id: string) => knowledgeApi.requestOpenKnowledge(id),
    });
    return () => null;
  },
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function mountArea(props: Record<string, unknown> = {}, stubOverrides: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(ManagementArea, {
    props: { open: true, ...props },
    global: {
      plugins: [pinia],
      stubs: {
        KnowledgePanel: knowledgeStub,
        EntityPanel: entityStub,
        PendingItems: pendingStub,
        MaintenanceRecords: recordsStub,
        GlobalSearchPanel: searchStub,
        ...stubOverrides,
      },
    },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  knowledgeMounts.length = 0;
  knowledgeApi.hasUnsavedDraft.mockReset().mockReturnValue(false);
  knowledgeApi.requestOpenKnowledge.mockReset().mockResolvedValue(true);
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

    await w.find("[data-mp-context-back]").trigger("click");
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
    expect(w.findComponent(pendingStub).props("focusId")).toBe("p1");
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

/**
 * #29 搜索/记忆页 → 知识条目的统一离开检查接线。
 *
 * 有未保存草稿时管理区**不直接**改 focusId（那会绕过面板的确认层），
 * 而是先切到知识标签页、等一帧，再把目标交给面板自己处理。
 */
describe("管理区：知识条目打开前的离开检查（#29 接线）", () => {
  const HIT = { kind: "knowledge", id: "k1", title: "知识", note: "" };

  async function mountWithKnowledgeMounted(dirty: boolean) {
    knowledgeApi.hasUnsavedDraft.mockReturnValue(dirty);
    const w = mountArea({}, { KnowledgePanel: knowledgeApiStub });
    await flushPromises();
    // 先激活知识标签页让面板挂载（之后 v-show 保活）
    await w.find('[data-mp-tab="knowledge"]').trigger("click");
    await flushPromises();
    await w.setProps({ searchTicket: 1 });
    await flushPromises();
    expect(w.attributes("data-view")).toBe("search");
    return w;
  }

  it("有草稿：先切到知识标签页，再委托面板 requestOpenKnowledge（不直接改 focusId）", async () => {
    const w = await mountWithKnowledgeMounted(true);
    w.findComponent(searchStub).vm.$emit("open", HIT);
    await flushPromises();

    expect(knowledgeApi.hasUnsavedDraft).toHaveBeenCalled();
    expect(knowledgeApi.requestOpenKnowledge).toHaveBeenCalledWith("k1");
    expect(w.attributes("data-view")).toBe("tabs");
    expect(w.attributes("data-tab")).toBe("knowledge");
    // 面板内部完成跳转；管理区不补一次 focusId 定位（否则会绕过确认层）
    expect(w.findComponent(knowledgeApiStub).props("focusId")).toBeNull();
  });

  it("面板返回 false（继续编辑 / 保存失败）：什么都不做，不补跳旧目标", async () => {
    knowledgeApi.requestOpenKnowledge.mockResolvedValue(false);
    const w = await mountWithKnowledgeMounted(true);
    w.findComponent(searchStub).vm.$emit("open", HIT);
    await flushPromises();

    expect(knowledgeApi.requestOpenKnowledge).toHaveBeenCalledWith("k1");
    expect(w.findComponent(knowledgeApiStub).props("focusId")).toBeNull();
    expect(w.attributes("data-tab")).toBe("knowledge");
  });

  it("确认期间再点另一个知识目标：只转发两次，旧 Promise 返回后不补跳", async () => {
    const first = deferred<boolean>();
    knowledgeApi.requestOpenKnowledge.mockImplementationOnce(() => first.promise).mockResolvedValueOnce(true);
    const w = await mountWithKnowledgeMounted(true);

    w.findComponent(searchStub).vm.$emit("open", { ...HIT, id: "k1" });
    await flushPromises();
    w.findComponent(searchStub).vm.$emit("open", { ...HIT, id: "k2" });
    await flushPromises();

    expect(knowledgeApi.requestOpenKnowledge).toHaveBeenNthCalledWith(1, "k1");
    expect(knowledgeApi.requestOpenKnowledge).toHaveBeenNthCalledWith(2, "k2");
    // 旧确认（k1）此刻才返回 false：管理区不能据此再补一次旧目标
    first.resolve(false);
    await flushPromises();
    expect(w.findComponent(knowledgeApiStub).props("focusId")).toBeNull();
    expect(knowledgeApi.requestOpenKnowledge).toHaveBeenCalledTimes(2);
  });

  it("无草稿：直接走 focusKnowledge（focusId 定位真实条目）", async () => {
    const w = await mountWithKnowledgeMounted(false);
    w.findComponent(searchStub).vm.$emit("open", HIT);
    await flushPromises();

    expect(knowledgeApi.hasUnsavedDraft).toHaveBeenCalled();
    expect(knowledgeApi.requestOpenKnowledge).not.toHaveBeenCalled();
    expect(w.findComponent(knowledgeApiStub).props("focusId")).toBe("k1");
  });

  it("面板未挂载：走 focusKnowledge（挂载后带 focusId 定位）", async () => {
    const w = mountArea({ searchTicket: 1 }, { KnowledgePanel: knowledgeApiStub });
    await flushPromises();
    expect(w.findComponent(knowledgeApiStub).exists()).toBe(false);

    w.findComponent(searchStub).vm.$emit("open", HIT);
    await flushPromises();

    expect(knowledgeApi.hasUnsavedDraft).not.toHaveBeenCalled();
    expect(knowledgeApi.requestOpenKnowledge).not.toHaveBeenCalled();
    expect(w.findComponent(knowledgeApiStub).props("focusId")).toBe("k1");
  });

  it("记忆页关联知识链接走同一离开检查", async () => {
    knowledgeApi.hasUnsavedDraft.mockReturnValue(true);
    const w = mountArea({}, { KnowledgePanel: knowledgeApiStub });
    await flushPromises();
    await w.find('[data-mp-tab="knowledge"]').trigger("click");
    await flushPromises();
    await w.find('[data-mp-tab="memory"]').trigger("click");
    await flushPromises();

    w.findComponent({ name: "MemoryTab" }).vm.$emit("focus-knowledge", "kn_1");
    await flushPromises();

    expect(knowledgeApi.requestOpenKnowledge).toHaveBeenCalledWith("kn_1");
    expect(w.attributes("data-tab")).toBe("knowledge");
  });
});
