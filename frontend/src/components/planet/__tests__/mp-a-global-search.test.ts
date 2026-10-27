/**
 * 全局搜索（A 交付 §5）的组件级测试。
 *
 * 四条硬要求：范围同时覆盖话题/知识/实体（独立于当前标签页）、真实数据并行取回、
 * 旧响应不得覆盖新关键词、加载/空/失败三态必须区分开。再加「返回时保留关键词与结果」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import GlobalSearchPanel from "../GlobalSearchPanel.vue";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listTopics: vi.fn(async () => ({ topics: [] as unknown[] })),
    listKnowledge: vi.fn(async () => ({ knowledge: [] as unknown[] })),
    listEntities: vi.fn(async () => ({ entities: [] as unknown[] })),
  },
}));
vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

const TOPIC = { topic_id: "t1", title: "薄荷种植", keywords: [], fragment_count: 1, last_activity: null, summary_preview: null };
const KNOWLEDGE = {
  id: "k1",
  category: "general_fact",
  state: "active",
  content: "薄荷需要充足阳光",
  confidence: null,
  topic_id: null,
  topic_name: null,
  created_at: "",
  updated_at: "",
};
const ENTITY = {
  id: "e1",
  node_id: "n1",
  name: "薄荷",
  aliases: [],
  kind: null,
  summary: "",
  attributes: [],
  relations: [],
  state: "active",
  created_at: "",
  updated_at: "",
};

function mountSearch(props: Record<string, unknown> = {}) {
  return mount(GlobalSearchPanel, { props: { active: true, debounceMs: 0, ...props } });
}

async function search(w: ReturnType<typeof mountSearch>, keyword: string) {
  await w.find("input").setValue(keyword);
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listTopics.mockResolvedValue({ topics: [TOPIC] });
  mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [KNOWLEDGE] });
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [ENTITY] });
});

describe("全局搜索：范围与分组", () => {
  it("一次搜索并行取三类真实数据，并按话题/知识/实体分组", async () => {
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");

    expect(mocks.apiMock.listTopics).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.listKnowledge).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.listEntities).toHaveBeenCalledTimes(1);

    const groups = w.findAll(".gs-group-title").map((n) => n.text());
    expect(groups).toEqual(["话题（1）", "知识（1）", "实体（1）"]);
    expect(w.find('[data-search-kind="topic"][data-search-id="t1"]').exists()).toBe(true);
    expect(w.find('[data-search-kind="knowledge"][data-search-id="k1"]').exists()).toBe(true);
    expect(w.find('[data-search-kind="entity"][data-search-id="e1"]').exists()).toBe(true);
    expect(w.find("[data-mp-search-results]").exists()).toBe(true);
  });

  it("点击结果只上报真实 kind + id（切标签页与打开条目由管理区负责）", async () => {
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");
    await w.find('[data-search-id="k1"]').trigger("click");
    expect(w.emitted("open")?.[0]).toEqual([
      { kind: "knowledge", id: "k1", title: "薄荷需要充足阳光", note: "知识" },
    ]);
  });

  it("空关键词不算搜索：不请求、不给「没有结果」的假空状态", async () => {
    const w = mountSearch();
    await flushPromises();
    expect(w.find(".gs-hint").text()).toContain("一次搜索同时覆盖");
    expect(mocks.apiMock.listTopics).not.toHaveBeenCalled();
  });
});

describe("全局搜索：三态", () => {
  it("加载中：明确说明正在搜索，不闪成空结果", async () => {
    let resolveTopics!: (v: { topics: unknown[] }) => void;
    mocks.apiMock.listTopics.mockImplementation(() => new Promise((r) => { resolveTopics = r; }));
    const w = mountSearch();
    await flushPromises();
    await w.find("input").setValue("薄荷");
    await flushPromises();
    expect(w.find(".gs-status").text()).toContain("正在搜索「薄荷」");
    resolveTopics({ topics: [TOPIC] });
    await flushPromises();
    expect(w.find(".gs-status").exists()).toBe(false);
    expect(w.find('[data-search-id="t1"]').exists()).toBe(true);
  });

  it("空结果：说清没有命中，而不是只给一个空列表", async () => {
    mocks.apiMock.listTopics.mockResolvedValue({ topics: [] });
    mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [] });
    mocks.apiMock.listEntities.mockResolvedValue({ entities: [] });
    const w = mountSearch();
    await flushPromises();
    await search(w, "不存在");
    expect(w.find(".gs-hint").text()).toContain("没有匹配");
  });

  it("失败：原因可见且可重试（不把失败显示成「没有结果」）", async () => {
    mocks.apiMock.listEntities.mockRejectedValueOnce(new Error("offline"));
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");
    const error = w.find(".gs-error");
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("搜索失败");
    await error.find("button").trigger("click");
    await flushPromises();
    expect(w.find(".gs-error").exists()).toBe(false);
    expect(w.find('[data-search-id="e1"]').exists()).toBe(true);
  });
});

describe("全局搜索：旧响应不得覆盖新关键词", () => {
  it("先发起的关键词后返回时被丢弃，界面只显示最新关键词的结果", async () => {
    const resolvers: Array<(v: { topics: unknown[] }) => void> = [];
    mocks.apiMock.listTopics.mockImplementation(
      () => new Promise((resolve) => { resolvers.push(resolve); }),
    );
    const w = mountSearch();
    await flushPromises();

    // 第一次：旧关键词（不 resolve）
    await w.find("input").setValue("旧");
    await flushPromises();
    // 第二次：新关键词（立刻 resolve 成「新」的结果）
    await w.find("input").setValue("新");
    await flushPromises();
    resolvers[1]({ topics: [{ ...TOPIC, topic_id: "t-new", title: "新结果" }] });
    await flushPromises();
    expect(w.find('[data-search-id="t-new"]').exists()).toBe(true);

    // 旧响应迟到：不得覆盖
    resolvers[0]({ topics: [{ ...TOPIC, topic_id: "t-old", title: "旧结果" }] });
    await flushPromises();
    expect(w.find('[data-search-id="t-old"]').exists()).toBe(false);
    expect(w.find('[data-search-id="t-new"]').exists()).toBe(true);
  });
});

describe("全局搜索：返回时保留关键词与结果", () => {
  it("视图切走再切回（v-show 保活）不重新搜索、结果仍在", async () => {
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");
    const calls = mocks.apiMock.listTopics.mock.calls.length;

    await w.setProps({ active: false });
    await w.setProps({ active: true });
    await flushPromises();

    expect((w.find("input").element as HTMLInputElement).value).toBe("薄荷");
    expect(w.find('[data-search-id="t1"]').exists()).toBe(true);
    expect(mocks.apiMock.listTopics.mock.calls.length).toBe(calls);
  });
});
