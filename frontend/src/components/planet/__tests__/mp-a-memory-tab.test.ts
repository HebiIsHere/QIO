/**
 * 记忆标签页（A 交付 §4）的组件级测试。
 *
 * 守的是「浏览 ≠ 换话题」这条线：列表 → 概览 → 完整讨论记录三层都能来回走，
 * 每层状态保留；只有按「继续讨论 / 从这里继续」才 emit continue（锚点切换由父级执行），
 * 浏览、搜索、切标签页一律不碰锚点。关联知识/实体只 emit 出去，由管理区切标签页定位。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MemoryTab from "../MemoryTab.vue";
import { useSessionStore } from "../../../stores/session";
import type { TopicDetail, TopicFingerprint } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listTopics: vi.fn<() => Promise<{ topics: TopicFingerprint[] }>>(async () => ({ topics: [] })),
    getTopicDetail: vi.fn<(id: string) => Promise<TopicDetail>>(async () => ({}) as TopicDetail),
    fragmentMessages: vi.fn(async (_id: string, offset = 0, limit = 20) => ({
      fragment_id: _id,
      topic_id: "t1",
      total: 0,
      offset,
      limit,
      messages: [] as { id: string; role: string; content: string; content_type: string; created_at: string }[],
    })),
    endTopic: vi.fn(async (id: string) => ({ ok: true, topic_id: id })),
    resumeTopic: vi.fn(async (id: string) => ({ ok: true, topic_id: id })),
  },
}));

vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

const TOPICS: TopicFingerprint[] = [
  { topic_id: "t1", title: "话题 A", keywords: ["A"], fragment_count: 3, last_activity: null, summary_preview: null },
  { topic_id: "t2", title: "选课", keywords: ["学期"], fragment_count: 1, last_activity: null, summary_preview: null, ended: true },
];

const FRAG_OLD = {
  fragment_id: "f10",
  summary: "上一段讨论",
  closed_at: "2026-09-10T08:00:00Z",
  created_at: "2026-09-10T07:00:00Z",
  message_count: 4,
};
const FRAG_NEW = {
  fragment_id: "f13",
  summary: "最近一段讨论",
  closed_at: null,
  created_at: "2026-09-14T07:00:00Z",
  message_count: 2,
};

const DETAIL: TopicDetail = {
  topic_id: "t1",
  name: "话题 A",
  summary: "一句摘要",
  keywords: ["Anchor", "生命周期"],
  last_activity: "2026-09-14T09:00:00Z",
  message_count: 42,
  fragments: [FRAG_OLD, FRAG_NEW],
  entities: [{ id: "n1", name: "薄荷" }],
  knowledge: [
    { id: "kn_1", category: "user_profile", state: "pending_review", content: "用户偏好简洁", confidence: null, updated_at: "2026-09-14T09:00:00Z" },
  ],
};

function mountTab(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MemoryTab, { props, global: { plugins: [pinia] } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listTopics.mockResolvedValue({ topics: TOPICS });
  mocks.apiMock.getTopicDetail.mockResolvedValue(DETAIL);
  mocks.apiMock.fragmentMessages.mockImplementation(async (id: string, offset = 0, limit = 20) => ({
    fragment_id: id,
    topic_id: "t1",
    total: 3,
    offset,
    limit,
    messages: Array.from({ length: 2 }, (_, i) => ({
      id: `${id}-m${offset + i}`,
      role: i % 2 ? "assistant" : "user",
      content: `第 ${offset + i + 1} 条`,
      content_type: "text",
      created_at: "2026-09-10T07:00:00Z",
    })),
  }));
  mocks.apiMock.endTopic.mockResolvedValue({ ok: true, topic_id: "t1" });
  mocks.apiMock.resumeTopic.mockResolvedValue({ ok: true, topic_id: "t2" });
});

describe("记忆标签页：话题列表", () => {
  it("进行中与已结束分组显示；已结束仍可被搜到、被恢复", async () => {
    const w = mountTab();
    await flushPromises();
    const main = w.find(".topic-list:not(.ended-list)");
    expect(main.text()).toContain("话题 A");
    expect(main.text()).not.toContain("选课");
    expect(w.find(".ended-list").text()).toContain("已结束");
    expect(w.find(".ended-list").text()).toContain("选课");

    await w.find(".topic-resume").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.resumeTopic).toHaveBeenCalledWith("t2");
    expect(w.emitted("topics-changed")).toBeTruthy();
  });

  it("结束进行中的话题，并发出 topics-changed（星球侧数据也要刷新）", async () => {
    const w = mountTab();
    await flushPromises();
    await w.find(".topic-toggle-ended").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.endTopic).toHaveBeenCalledWith("t1");
    expect(w.emitted("topics-changed")).toBeTruthy();
  });

  it("搜索只过滤列表，不改变任何条目（结束/恢复仍可用）", async () => {
    const w = mountTab();
    await flushPromises();
    await w.find(".mt-list-head input").setValue("学期");
    await flushPromises();
    expect(w.find(".topic-list:not(.ended-list)").text()).not.toContain("话题 A");
    expect(w.find(".ended-list").text()).toContain("选课");
  });

  it("话题项带真实 id，列表容器可被验收脚本定位", async () => {
    const w = mountTab();
    await flushPromises();
    const container = w.find('[data-memory-topics]');
    expect(container.exists()).toBe(true);
    expect(w.findAll("[data-topic-id]").map((n) => n.attributes("data-topic-id"))).toEqual(["t1", "t2"]);
  });
});

describe("记忆标签页：话题概览", () => {
  it("点击话题：先让星球聚焦（浏览），再读概览；不碰锚点", async () => {
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();

    expect(w.emitted("topic-select")?.[0]).toEqual(["t1"]);
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledWith("t1");
    expect(mocks.apiMock.endTopic).not.toHaveBeenCalled();
    expect(w.find("[data-memory-overview]").exists()).toBe(true);
    expect(w.find("[data-memory-overview]").text()).toContain("话题 A");
    expect(w.find(".mt-summary").text()).toContain("一句摘要");
    expect(w.find(".mt-facts").text()).toContain("2 个片段");
    expect(w.find(".mt-facts").text()).toContain("42 条消息");
  });

  it("明确区分「正在浏览」与「对话当前话题」，并说明浏览不改位置", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.currentTopicId = "t2";
    session.topicName = "选课";
    const w = mount(MemoryTab, { global: { plugins: [pinia] } });
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();

    const identity = w.find(".mt-identity").text();
    expect(identity).toContain("正在浏览");
    expect(identity).toContain("话题 A");
    expect(identity).toContain("对话当前话题");
    expect(identity).toContain("选课");
    // 浏览不是换话题：没有提交任何锚点
    expect(w.emitted("continue")).toBeFalsy();
    expect(w.find(".mt-action-hint").text()).toContain("继续讨论会把对话起点切换");
  });

  it("对话当前话题就是它时，文案改成「回到对话继续」", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    useSessionStore().currentTopicId = "t1";
    const w = mount(MemoryTab, { global: { plugins: [pinia] } });
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();
    expect(w.find("[data-memory-overview] [data-memory-continue]").text()).toContain("回到对话继续");
    expect(w.find(".mt-action-hint").text()).toContain("这是对话当前的话题");
  });

  it("「继续讨论」只 emit 真实 topicId + 空 fragment（锚点切换由父级执行）", async () => {
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();
    await w.find("[data-memory-overview] [data-memory-continue]").trigger("click");
    expect(w.emitted("continue")?.[0]).toEqual([{ topicId: "t1", fragmentId: null, historic: false }]);
    expect(mocks.apiMock.endTopic).not.toHaveBeenCalled();
  });

  it("概览失败：原因可见 + 重试（不拿空概览冒充话题）", async () => {
    mocks.apiMock.getTopicDetail.mockRejectedValueOnce(new Error("boom"));
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();
    const block = w.find('[data-memory-overview] [role="alert"]');
    expect(block.text()).toContain("读取话题概览失败");
    await block.find("button").trigger("click");
    await flushPromises();
    expect(w.find(".mt-title").text()).toBe("话题 A");
  });

  it("快速切换话题：慢的旧响应不得覆盖当前话题", async () => {
    const pending = new Map<string, (d: TopicDetail) => void>();
    mocks.apiMock.getTopicDetail.mockImplementation(
      (id: string) => new Promise<TopicDetail>((resolve) => pending.set(id, resolve)),
    );
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();
    await w.find('[data-topic-id="t2"]').trigger("click");
    await flushPromises();

    pending.get("t1")!({ ...DETAIL, name: "话题 A" });
    await flushPromises();
    expect(w.find("[data-memory-overview]").text()).not.toContain("话题 A");

    pending.get("t2")!({ ...DETAIL, topic_id: "t2", name: "选课" });
    await flushPromises();
    expect(w.find("[data-memory-overview] .mt-title").text()).toBe("选课");
  });

  it("关联知识 / 关联实体只 emit 出去（切标签页与定位由管理区负责）", async () => {
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();
    await w.find('[data-rel-knowledge-id="kn_1"]').trigger("click");
    await w.find('[data-rel-entity-id="n1"]').trigger("click");
    expect(w.emitted("focus-knowledge")?.[0]).toEqual(["kn_1"]);
    expect(w.emitted("focus-entity")?.[0]).toEqual(["n1"]);
  });
});

describe("记忆标签页：完整讨论记录", () => {
  it("概览 → 完整记录：真实分页读取，只读说明，可返回概览且状态保留", async () => {
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();

    await w.find(".mt-open-record").trigger("click");
    await flushPromises();
    expect(w.find("[data-memory-record]").exists()).toBe(true);
    // 默认打开最新片段
    expect(mocks.apiMock.fragmentMessages).toHaveBeenCalledWith("f13", 0, 20);
    expect(w.find("[data-memory-record] .mt-hint").text()).toContain("只读");

    // 继续读取（total=3 → 第一页 2 条，还有 1 条）
    await w.find(".raw-more").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.fragmentMessages).toHaveBeenLastCalledWith("f13", 2, 20);
    expect(w.findAll(".raw-item").length).toBe(4); // 第一页 2 条 + 追加 2 条（不重复第一页）

    // 返回概览（记录页的返回按钮），再回到列表：三层都在 DOM 里、状态没重置
    await w.find("[data-memory-record] .mt-back").trigger("click");
    await flushPromises();
    expect(w.find("[data-memory-overview]").isVisible()).toBe(true);
    await w.find("[data-memory-overview] .mt-back").trigger("click");
    await flushPromises();
    expect(w.find("[data-memory-topics]").isVisible()).toBe(true);
  });

  it("切到历史片段：重新从第一页读，并且「从这里继续」带该片段 id", async () => {
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();
    await w.find(".mt-open-record").trigger("click");
    await flushPromises();

    const fragments = w.findAll(".mt-fragment");
    expect(fragments.length).toBe(2);
    await fragments[0].trigger("click");
    await flushPromises();
    expect(mocks.apiMock.fragmentMessages).toHaveBeenLastCalledWith("f10", 0, 20);

    const continueBtn = w.find("[data-memory-record] [data-memory-continue]");
    expect(continueBtn.text()).toContain("从这里继续（历史位置）");
    await continueBtn.trigger("click");
    const emitted = w.emitted("continue")!;
    // 历史片段：historic=true（父级据此**不**先取消旧接续）
    expect(emitted[emitted.length - 1]).toEqual([{ topicId: "t1", fragmentId: "f10", historic: true }]);
  });

  it("当前开放片段：historic=false（父级据此先取消旧接续）", async () => {
    mocks.apiMock.getTopicDetail.mockResolvedValue({
      ...DETAIL,
      fragments: [FRAG_OLD, { ...FRAG_NEW, closed_at: null }],
    });
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();
    await w.find(".mt-open-record").trigger("click");
    await flushPromises();
    await w.find("[data-memory-record] [data-memory-continue]").trigger("click");
    const emitted = w.emitted("continue")!;
    expect(emitted[emitted.length - 1]).toEqual([{ topicId: "t1", fragmentId: "f13", historic: false }]);
  });

  it("记录读取失败：原因可见并可重试", async () => {
    mocks.apiMock.fragmentMessages.mockRejectedValueOnce(new Error("offline"));
    const w = mountTab();
    await flushPromises();
    await w.find('[data-topic-id="t1"]').trigger("click");
    await flushPromises();
    await w.find(".mt-open-record").trigger("click");
    await flushPromises();
    const alert = w.find("[data-memory-record] [role=\"alert\"]");
    expect(alert.text()).toContain("读取讨论记录失败");
    await alert.find("button").trigger("click");
    await flushPromises();
    expect(w.findAll(".raw-item").length).toBeGreaterThan(0);
  });
});

describe("记忆标签页：外部定位", () => {
  it("星球选中话题（focusTicket）→ 打开该话题概览，不改锚点", async () => {
    const w = mountTab({ focusTopicId: "t1", focusTicket: 1 });
    await flushPromises();
    expect(w.find("[data-memory-overview]").exists()).toBe(true);
    expect(mocks.apiMock.getTopicDetail).toHaveBeenCalledWith("t1");
    expect(w.emitted("continue")).toBeFalsy();
  });
});
