import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import KnowledgePanel from "../KnowledgePanel.vue";
import type { KnowledgeItem, TopicFingerprint } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
    listTopics: vi.fn<() => Promise<{ topics: TopicFingerprint[] }>>(async () => ({ topics: [] })),
    createKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
    verifyKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "", state: "active" } })),
    rejectKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "", state: "draft" } })),
    reviseKnowledge: vi.fn(async () => ({ ok: true })),
    revokeKnowledge: vi.fn(async () => ({ ok: true })),
  },
}));

vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

const K: KnowledgeItem = {
  id: "kn_1", category: "user_profile", state: "pending_review",
  content: "用户最爱五里关火锅", confidence: 0.9,
  topic_id: "t1", topic_name: "吃火锅", created_at: "", updated_at: "",
};

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(KnowledgePanel, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [K] });
  mocks.apiMock.listTopics.mockResolvedValue({ topics: [] });
});

describe("KnowledgePanel", () => {
  it("加载并渲染知识列表（分类/状态 badge）", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.text()).toContain("用户最爱五里关火锅");
    expect(w.find(".k-cat").text()).toBe("用户画像");
    expect(w.find(".k-state").text()).toBe("待审核");
    expect(w.find(".k-state").classes()).toContain("pending_review");
    w.unmount();
  });

  it("搜索过滤列表", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find("input").setValue("火锅");
    await flushPromises();
    expect(w.text()).toContain("用户最爱五里关火锅");
    await w.find("input").setValue("不存在");
    await flushPromises();
    expect(w.text()).not.toContain("用户最爱五里关火锅");
    w.unmount();
  });

  it("批准调用 verifyKnowledge 并刷新", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-approve").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.verifyKnowledge).toHaveBeenCalledWith("kn_1");
    expect(mocks.apiMock.listKnowledge).toHaveBeenCalledTimes(2);
    w.unmount();
  });

  it("打回调用 rejectKnowledge", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-reject").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.rejectKnowledge).toHaveBeenCalledWith("kn_1");
    w.unmount();
  });

  it("手动新建调用 createKnowledge（可选关联话题）", async () => {
    mocks.apiMock.listTopics.mockResolvedValue({
      topics: [{ topic_id: "t9", title: "测试话题", keywords: [], fragment_count: 0, last_activity: null, summary_preview: null }],
    });
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-create-open").trigger("click");
    await w.find(".k-category").trigger("click");
    await w.findAll(".k-category .opt").find((o) => o.text() === "目标")!.trigger("click");
    await w.find(".k-topic").trigger("click");
    await w.findAll(".k-topic .opt").find((o) => o.text() === "测试话题")!.trigger("click");
    await w.find(".create-form textarea").setValue("SQLite 支持 WAL");
    await w.find(".create-form").trigger("submit");
    await flushPromises();
    expect(mocks.apiMock.createKnowledge).toHaveBeenCalledWith({
      category: "goal",
      content: "SQLite 支持 WAL",
      topic_id: "t9",
    });
    w.unmount();
  });

  it("手动新建不选话题：topic_id 为 null", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-create-open").trigger("click");
    await w.find(".create-form textarea").setValue("无话题知识");
    await w.find(".create-form").trigger("submit");
    await flushPromises();
    expect(mocks.apiMock.createKnowledge).toHaveBeenCalledWith({
      category: "general_fact",
      content: "无话题知识",
      topic_id: null,
    });
    w.unmount();
  });

  it("点击关联话题 → emit focus-topic", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-topic-link").trigger("click");
    expect(w.emitted("focus-topic")).toEqual([["t1"]]);
    w.unmount();
  });
});

describe("KnowledgePanel 筛选框（P0：关闭态要有可见标签，且能回到全部）", () => {
  it("两个筛选框关闭态显示「全部分类 / 全部状态」，不是空白方块", async () => {
    const w = mountPanel();
    await flushPromises();
    const vals = w.findAll(".filters .qio-select-val").map((v) => v.text());
    expect(vals).toEqual(["全部分类", "全部状态"]);
    w.unmount();
  });

  it("选择某一分类后仍可切回「全部分类」", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.findAll(".filters .qio-select")[0].trigger("click");
    await w.findAll(".filters .qio-select")[0].findAll(".opt").find((o) => o.text() === "目标")!.trigger("click");
    await nextTick();
    expect(w.find(".filters .qio-select-val").text()).toBe("目标");

    await w.findAll(".filters .qio-select")[0].trigger("click");
    await w.findAll(".filters .qio-select")[0].findAll(".opt").find((o) => o.text() === "全部分类")!.trigger("click");
    await nextTick();
    expect(w.find(".filters .qio-select-val").text()).toBe("全部分类");
    w.unmount();
  });
});

describe("KnowledgePanel 空态与失败区分（任务05 E：失败不能显示成无数据）", () => {
  it("加载失败：显示可读错误与重试入口，不显示「无知识条目」", async () => {
    mocks.apiMock.listKnowledge.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    const err = w.find(".k-load-error");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("offline");
    expect(err.find("button").text()).toContain("重试");
    expect(w.text()).not.toContain("暂无知识记录");

    mocks.apiMock.listKnowledge.mockResolvedValueOnce({ knowledge: [K] });
    await err.find("button").trigger("click");
    await flushPromises();
    expect(w.find(".k-load-error").exists()).toBe(false);
    expect(w.text()).toContain("用户最爱五里关火锅");
    w.unmount();
  });

  it("「暂无记录」与「没有匹配」用不同文案，并能一键清除筛选恢复集合", async () => {
    mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [] });
    const empty = mountPanel();
    await flushPromises();
    expect(empty.find(".k-empty").text()).toContain("暂无知识记录");
    empty.unmount();

    mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [K] });
    const w = mountPanel();
    await flushPromises();
    await w.find("input").setValue("不存在的关键词");
    await flushPromises();
    expect(w.find(".k-empty").text()).toContain("没有匹配");
    await w.find(".k-clear-filters").trigger("click");
    await flushPromises();
    expect(w.find(".k-item").exists()).toBe(true);
    w.unmount();
  });
});

/**
 * 第四阶段：默认阅读、局部编辑 + QIO 自己的确认层。
 *
 * 1) 阅读态下管理动作（修正/归档）默认退场，只有「待确认」这类当前操作对象常驻；
 * 2) 归档不再走浏览器原生 confirm，而是就地展开的确认层，并说明真实影响。
 */
describe("KnowledgePanel 阅读态与确认层（第四阶段）", () => {
  it("阅读态：管理动作容器默认收起，内容本身在明处", async () => {
    const w = mountPanel();
    await flushPromises();
    const actions = w.find(".k-item .k-actions");
    expect(actions.exists()).toBe(true);
    expect(actions.classes()).not.toContain("open");
    expect(w.find(".k-item .k-content").text()).toContain("用户最爱五里关火锅");
    w.unmount();
  });

  it("进入编辑：就地进入（内联编辑），保存按钮在同一处", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.find(".k-item textarea").exists()).toBe(false);
    await w.find(".k-edit-open").trigger("click");
    const area = w.find(".k-item textarea");
    expect(area.exists()).toBe(true);
    expect(area.classes()).toContain("qio-inline-edit");
    expect(w.find(".k-save").exists()).toBe(true);
    w.unmount();
  });

  it("归档展开说明影响的确认层，确认后才调用接口（不用原生 confirm）", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-archive").trigger("click");
    const confirm = w.find(".k-item .qio-confirm");
    expect(confirm.exists()).toBe(true);
    expect(confirm.text()).toContain("归档这条知识？");
    expect(confirm.text()).toContain("不再参与回答");
    expect(mocks.apiMock.revokeKnowledge).not.toHaveBeenCalled();

    // 取消：什么都不发生，确认层收起
    await confirm.findAll("button")[0].trigger("click");
    expect(w.find(".k-item .qio-confirm").exists()).toBe(false);
    expect(mocks.apiMock.revokeKnowledge).not.toHaveBeenCalled();

    // 确认：才真的归档
    await w.find(".k-archive").trigger("click");
    await w.find(".k-item .qio-confirm").findAll("button")[1].trigger("click");
    await flushPromises();
    expect(mocks.apiMock.revokeKnowledge).toHaveBeenCalledWith("kn_1");
    w.unmount();
  });
});
