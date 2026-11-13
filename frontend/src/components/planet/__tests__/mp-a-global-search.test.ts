/**
 * 全局搜索（A 交付 §5）的组件级测试。
 *
 * 守住的边界：
 * 1. 范围同时覆盖话题/知识/实体（独立于当前标签页），真实数据并行取回；
 * 2. 三个来源各自维护 loading / ok / error：一个来源失败只影响自己，其余结果仍可用，
 *    失败来源可就地单源重试；
 * 3. 旧响应不得覆盖当前关键词（请求序号 + 关键词 + 来源三重校验）—— 快速换词、
 *    单源重试慢、顶部刷新都可能让旧请求晚到；
 * 4. 只有「全部来源成功且命中为 0」才是总空态；加载/失败不冒充没有结果；
 * 5. 返回时保留关键词与结果（v-show 保活）；刷新按钮悬停只写「重新搜索」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import GlobalSearchPanel from "../GlobalSearchPanel.vue";
import QIconButton from "../../ui/QIconButton.vue";

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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

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
    // 三个来源各自是成功态
    for (const key of ["topics", "knowledge", "entities"]) {
      expect(w.find(`[data-search-source="${key}"]`).attributes("data-source-status")).toBe("ok");
    }
  });

  it("点击结果只上报真实 kind + id（切标签页与打开条目由管理区负责）", async () => {
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");
    await w.find('[data-search-id="k1"]').trigger("click");
    expect(w.emitted("open")?.[0]).toEqual([
      { kind: "knowledge", id: "k1", title: "薄荷需要充足阳光", note: "常识 · 生效" },
    ]);
  });

  it("空关键词不算搜索：不请求、不给「没有结果」的假空状态", async () => {
    const w = mountSearch();
    await flushPromises();
    expect(w.find(".gs-hint").exists()).toBe(false);
    // 搜索范围只留在输入框 placeholder 里（不再在空态重复解释一遍）
    expect(w.find("input").attributes("placeholder")).toContain("话题、知识、实体");
    expect(mocks.apiMock.listTopics).not.toHaveBeenCalled();
  });

  it("刷新按钮悬停只写「重新搜索」", async () => {
    const w = mountSearch();
    await flushPromises();
    const refresh = w.findComponent(QIconButton);
    expect(refresh.props("label")).toBe("重新搜索");
    expect(refresh.props("description")).toBe("");
  });
});

describe("全局搜索：分源状态（loading / ok / error）", () => {
  it("加载中：明确说明正在搜索，不闪成空结果", async () => {
    const topics = deferred<{ topics: unknown[] }>();
    mocks.apiMock.listTopics.mockImplementationOnce(() => topics.promise);
    const w = mountSearch();
    await flushPromises();
    await w.find("input").setValue("薄荷");
    await flushPromises();
    expect(w.find(".gs-status").text()).toContain("正在搜索「薄荷」");
    topics.resolve({ topics: [TOPIC] });
    await flushPromises();
    expect(w.find(".gs-status").exists()).toBe(false);
    expect(w.find('[data-search-id="t1"]').exists()).toBe(true);
  });

  it("一个来源 503：只有它就地报错，其他来源结果照常可用，并可单源重试", async () => {
    mocks.apiMock.listEntities.mockRejectedValueOnce(new Error("listEntities -> 503: 服务不可用"));
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");

    const error = w.find('[data-search-source-error="entities"]');
    expect(error.exists()).toBe(true);
    expect(error.text()).toContain("实体搜索失败");
    expect(error.text()).toContain("503");

    // 其余来源照常可用，总数只算成功来源
    expect(w.find('[data-search-id="t1"]').exists()).toBe(true);
    expect(w.find('[data-search-id="k1"]').exists()).toBe(true);
    expect(w.find('[data-search-id="e1"]').exists()).toBe(false);
    expect(w.find(".gs-summary").text()).toContain("共 2 项结果");
    // 部分失败不是「没有结果」
    expect(w.find(".gs-hint").exists()).toBe(false);

    // 单源重试：只重跑失败的那一个来源
    await w.find('[data-search-source-retry="entities"]').trigger("click");
    await flushPromises();
    expect(mocks.apiMock.listTopics).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.listKnowledge).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.listEntities).toHaveBeenCalledTimes(2);
    expect(w.find('[data-search-source-error="entities"]').exists()).toBe(false);
    expect(w.find('[data-search-id="e1"]').exists()).toBe(true);
    expect(w.find(".gs-summary").text()).toContain("共 3 项结果");
  });

  it("全部来源失败：三个就地错误，不冒充空态", async () => {
    mocks.apiMock.listTopics.mockRejectedValueOnce(new Error("listTopics -> 503: 服务不可用"));
    mocks.apiMock.listKnowledge.mockRejectedValueOnce(new Error("listKnowledge -> 503: 服务不可用"));
    mocks.apiMock.listEntities.mockRejectedValueOnce(new Error("listEntities -> 503: 服务不可用"));
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");

    expect(w.findAll("[data-search-source-error]").length).toBe(3);
    expect(w.find(".gs-hint").exists()).toBe(false);
  });

  it("全部来源成功且命中为 0 才显示总空态", async () => {
    mocks.apiMock.listTopics.mockResolvedValue({ topics: [] });
    mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [] });
    mocks.apiMock.listEntities.mockResolvedValue({ entities: [] });
    const w = mountSearch();
    await flushPromises();
    await search(w, "不存在");
    expect(w.find(".gs-hint").text()).toContain("没有匹配");
    w.unmount();

    // 有一个来源失败时：即使其余来源命中为 0，也不能说「没有匹配」
    mocks.apiMock.listEntities.mockRejectedValueOnce(new Error("offline"));
    const w2 = mountSearch();
    await flushPromises();
    await search(w2, "不存在");
    expect(w2.find('[data-search-source-error="entities"]').exists()).toBe(true);
    expect(w2.find(".gs-hint").exists()).toBe(false);
    w2.unmount();
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

  it("快速换词：旧关键词三个来源的响应都迟到，也一条都不能落到当前关键词下面", async () => {
    const oldTopics = deferred<{ topics: unknown[] }>();
    const oldKnowledge = deferred<{ knowledge: unknown[] }>();
    const oldEntities = deferred<{ entities: unknown[] }>();
    mocks.apiMock.listTopics.mockImplementationOnce(() => oldTopics.promise);
    mocks.apiMock.listKnowledge.mockImplementationOnce(() => oldKnowledge.promise);
    mocks.apiMock.listEntities.mockImplementationOnce(() => oldEntities.promise);
    const w = mountSearch();
    await flushPromises();

    await w.find("input").setValue("旧");
    await flushPromises();
    await w.find("input").setValue("薄荷");
    await flushPromises();

    // 新关键词已经出了结果
    expect(w.find('[data-search-id="t1"]').exists()).toBe(true);
    expect(w.find('[data-search-id="k1"]').exists()).toBe(true);
    expect(w.find('[data-search-id="e1"]').exists()).toBe(true);

    // 旧关键词的响应此刻才回来
    oldTopics.resolve({ topics: [{ ...TOPIC, topic_id: "t-old", title: "旧结果" }] });
    oldKnowledge.resolve({ knowledge: [{ ...KNOWLEDGE, id: "k-old", content: "旧结果" }] });
    oldEntities.resolve({ entities: [{ ...ENTITY, id: "e-old", name: "旧结果" }] });
    await flushPromises();

    expect(w.find('[data-search-id="t-old"]').exists()).toBe(false);
    expect(w.find('[data-search-id="k-old"]').exists()).toBe(false);
    expect(w.find('[data-search-id="e-old"]').exists()).toBe(false);
    expect(w.find(".gs-summary").text()).toContain("共 3 项结果");
  });

  it("单源重试慢于换词：重试响应迟到不覆盖新关键词", async () => {
    mocks.apiMock.listEntities.mockRejectedValueOnce(new Error("listEntities -> 503: 服务不可用"));
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");
    expect(w.find('[data-search-source-error="entities"]').exists()).toBe(true);

    // 重试挂起
    const slowRetry = deferred<{ entities: unknown[] }>();
    mocks.apiMock.listEntities.mockImplementationOnce(() => slowRetry.promise);
    await w.find('[data-search-source-retry="entities"]').trigger("click");
    await flushPromises();

    // 换词：新关键词「薄」也能命中实体
    await w.find("input").setValue("薄");
    await flushPromises();
    expect(w.find('[data-search-id="e1"]').exists()).toBe(true);

    // 旧重试现在才回来：不能覆盖
    slowRetry.resolve({ entities: [{ ...ENTITY, id: "e-stale", name: "薄荷" }] });
    await flushPromises();
    expect(w.find('[data-search-id="e-stale"]').exists()).toBe(false);
    expect(w.find('[data-search-id="e1"]').exists()).toBe(true);
  });
});

describe("全局搜索：顶部刷新", () => {
  it("顶部刷新重跑当前关键词的全部来源", async () => {
    const w = mountSearch();
    await flushPromises();
    await search(w, "薄荷");
    expect(w.find('[data-search-id="t1"]').exists()).toBe(true);

    await w.find('button[aria-label="重新搜索"]').trigger("click");
    await flushPromises();

    expect(mocks.apiMock.listTopics).toHaveBeenCalledTimes(2);
    expect(mocks.apiMock.listKnowledge).toHaveBeenCalledTimes(2);
    expect(mocks.apiMock.listEntities).toHaveBeenCalledTimes(2);
    expect(w.find('[data-search-id="t1"]').exists()).toBe(true);
    expect(w.find(".gs-summary").text()).toContain("共 3 项结果");
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
