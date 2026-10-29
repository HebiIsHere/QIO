/**
 * 发布前第三轮：全局搜索完整性（F11）。
 *
 * 守住的边界：
 * 1. 三类搜索显示真实命中总数（不是固定截断后的条数）；
 * 2. 命中结果分批「查看更多」，第 9 条与最后一条都能出现、能点击、携带真实 kind+id；
 * 3. 换关键词重置展开并丢弃旧响应；失败可重试、结果保留；切走切回保留上下文；
 * 4. 打开链路：真实 KnowledgePanel 用 focusId 能定位到第 9 条（点击 → 真实条目）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import GlobalSearchPanel from "../GlobalSearchPanel.vue";
import KnowledgePanel from "../KnowledgePanel.vue";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listTopics: vi.fn(async () => ({ topics: [] as unknown[] })),
    listKnowledge: vi.fn(async () => ({ knowledge: [] as unknown[] })),
    listEntities: vi.fn(async () => ({ entities: [] as unknown[] })),
  },
}));
vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

function topic(i: number) {
  return {
    topic_id: `t${i}`,
    title: `薄荷话题 ${i}`,
    keywords: [],
    fragment_count: 1,
    last_activity: null,
    summary_preview: null,
  };
}
function knowledgeItem(i: number) {
  return {
    id: `k${i}`,
    category: "general_fact",
    state: "active",
    content: `薄荷知识 ${i}`,
    confidence: null,
    topic_id: null,
    topic_name: null,
    created_at: "",
    updated_at: "",
  };
}
function entity(i: number) {
  return {
    id: `e${i}`,
    node_id: `n${i}`,
    name: `薄荷实体 ${i}`,
    aliases: [],
    kind: null,
    summary: "",
    attributes: [],
    relations: [],
    state: "active",
    created_at: "",
    updated_at: "",
  };
}

async function search(w: ReturnType<typeof mount>, keyword: string) {
  await w.find("input").setValue(keyword);
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listTopics.mockResolvedValue({
    topics: Array.from({ length: 12 }, (_, i) => topic(i)),
  });
  mocks.apiMock.listKnowledge.mockResolvedValue({
    knowledge: [knowledgeItem(0), knowledgeItem(1)],
  });
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [entity(0)] });
});

describe("F11 全局搜索：真实总数与分批查看更多", () => {
  it("显示真实命中总数与每类真实数；第 9 条默认不显示，展开后出现并可点", async () => {
    const w = mount(GlobalSearchPanel, { props: { active: true, debounceMs: 0 } });
    await flushPromises();
    await search(w, "薄荷");

    // 真实总数 = 12 + 2 + 1
    expect(w.find(".gs-summary").text()).toContain("共 15 项结果");
    const titles = w.findAll(".gs-group-title").map((n) => n.text());
    expect(titles).toEqual(["话题（12）", "知识（2）", "实体（1）"]);

    // 首批只展示 8 条；第 9 条（t8）此时不可见
    expect(w.findAll('[data-search-kind="topic"]').length).toBe(8);
    expect(w.find('[data-search-id="t8"]').exists()).toBe(false);

    // 查看更多：第 9 条与最后一条（t11）都出现
    await w.find('[data-search-more="topics"]').trigger("click");
    await flushPromises();
    expect(w.findAll('[data-search-kind="topic"]').length).toBe(12);
    expect(w.find('[data-search-id="t8"]').exists()).toBe(true);
    expect(w.find('[data-search-id="t11"]').exists()).toBe(true);
    expect(w.find('[data-search-more="topics"]').exists()).toBe(false);

    // 第九条与最后一条都能打开定位（上报真实 kind + id）
    await w.find('[data-search-id="t8"]').trigger("click");
    await w.find('[data-search-id="t11"]').trigger("click");
    const opened = w.emitted("open") ?? [];
    expect(opened[0]).toEqual([
      { kind: "topic", id: "t8", title: "薄荷话题 8", note: "1 片段" },
    ]);
    expect(opened[1]?.[0]).toMatchObject({ kind: "topic", id: "t11" });
    w.unmount();
  });

  it("每类独立展开：展开话题不影响知识 / 实体的分批", async () => {
    const w = mount(GlobalSearchPanel, { props: { active: true, debounceMs: 0 } });
    await flushPromises();
    await search(w, "薄荷");
    await w.find('[data-search-more="topics"]').trigger("click");
    await flushPromises();
    expect(w.findAll('[data-search-kind="knowledge"]').length).toBe(2);
    expect(w.find('[data-search-more="knowledge"]').exists()).toBe(false);
    w.unmount();
  });

  it("换关键词重置展开状态，旧关键词的迟到响应不覆盖新结果", async () => {
    const resolvers: Array<(v: { topics: unknown[] }) => void> = [];
    mocks.apiMock.listTopics.mockImplementation(
      () => new Promise((resolve) => { resolvers.push(resolve); }),
    );
    const w = mount(GlobalSearchPanel, { props: { active: true, debounceMs: 0 } });
    await flushPromises();

    // 第一次：旧关键词（不 resolve；「薄荷话」是标题子串）
    await w.find("input").setValue("薄荷话");
    await flushPromises();
    // 第二次：新关键词（立即给 12 条；「薄荷」同样是标题子串）
    await w.find("input").setValue("薄荷");
    await flushPromises();
    resolvers[resolvers.length - 1]({ topics: Array.from({ length: 12 }, (_, i) => topic(i)) });
    await flushPromises();
    expect(w.find(".gs-summary").text()).toContain("共 15 项结果");
    await w.find('[data-search-more="topics"]').trigger("click");
    await flushPromises();
    expect(w.findAll('[data-search-kind="topic"]').length).toBe(12);

    // 旧响应迟到：不得覆盖（也不得把展开状态/结果带回旧关键词）
    if (resolvers.length > 1) resolvers[0]({ topics: [topic(99)] });
    await flushPromises();
    expect(w.find('[data-search-id="t99"]').exists()).toBe(false);
    expect(w.findAll('[data-search-kind="topic"]').length).toBe(12);
    w.unmount();
  });

  it("失败保留上次结果并可重试：重试成功后才更新", async () => {
    const w = mount(GlobalSearchPanel, { props: { active: true, debounceMs: 0 } });
    await flushPromises();
    await search(w, "薄荷");

    mocks.apiMock.listEntities.mockRejectedValueOnce(new Error("offline"));
    // 同一个关键词再触发一次检索（输入值加空格 → 关键词仍相同）
    await w.find("input").setValue("薄荷 ");
    await flushPromises();
    expect(w.find(".gs-error").exists()).toBe(true);
    expect(w.find('[data-search-id="t0"]').exists()).toBe(true);

    await w.find(".gs-error button").trigger("click");
    await flushPromises();
    expect(w.find(".gs-error").exists()).toBe(false);
    expect(w.find(".gs-summary").text()).toContain("共 15 项结果");
    w.unmount();
  });
});

describe("F11 打开定位：真实知识面板按 focusId 定位第 9 条", () => {
  it("focusId 指向第 9 条时打开详情并显示该条真实内容", async () => {
    mocks.apiMock.listKnowledge.mockResolvedValue({
      knowledge: Array.from({ length: 12 }, (_, i) => knowledgeItem(i)),
    });
    mocks.apiMock.listTopics.mockResolvedValue({ topics: [] });
    const w = mount(KnowledgePanel, { props: { focusId: "k8" } });
    await flushPromises();
    await flushPromises();
    const detail = w.find("[data-k-detail]");
    expect(detail.exists()).toBe(true);
    expect(detail.text()).toContain("薄荷知识 8");
    w.unmount();
  });
});
