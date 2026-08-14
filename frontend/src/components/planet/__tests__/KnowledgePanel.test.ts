import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import KnowledgePanel from "../KnowledgePanel.vue";
import type { KnowledgeItem } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
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

  it("手动新建调用 createKnowledge", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".k-create-open").trigger("click");
    await w.find(".k-category").trigger("click");
    await w.findAll(".k-category .opt").find((o) => o.text() === "常识")!.trigger("click");
    await w.find(".k-content").setValue("SQLite 支持 WAL");
    await w.find(".create-form").trigger("submit");
    await flushPromises();
    expect(mocks.apiMock.createKnowledge).toHaveBeenCalledWith({
      category: "general_fact",
      content: "SQLite 支持 WAL",
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


