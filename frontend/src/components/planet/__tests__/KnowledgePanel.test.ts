import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import KnowledgePanel from "../KnowledgePanel.vue";
import type { KnowledgeItem, TopicFingerprint } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  refreshPending: vi.fn(async () => {}),
  apiMock: {
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
    listTopics: vi.fn<() => Promise<{ topics: TopicFingerprint[] }>>(async () => ({ topics: [] })),
    createKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
    verifyKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "", state: "active" } })),
    rejectKnowledge: vi.fn(async () => ({ ok: true, knowledge: { id: "", state: "draft" } })),
    reviseKnowledge: vi.fn(async () => ({ ok: true, knowledge_id: "kn_new", version: 2 })),
    revokeKnowledge: vi.fn(async () => ({ ok: true })),
    endKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
    resumeKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
    setKnowledgeScope: vi.fn(async () => ({ ok: true, knowledge: {} as KnowledgeItem })),
  },
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return { ...actual, api: mocks.apiMock };
});

/** 待处理数量 store：真实实现在这里只承担「写入后刷新角标」，测试里用替身记录调用。 */
vi.mock("../../../stores/planetPending", () => ({
  usePlanetPendingStore: () => ({ count: 0, refresh: mocks.refreshPending }),
}));

const K: KnowledgeItem = {
  id: "kn_1",
  category: "user_profile",
  state: "pending_review",
  content: "用户最爱五里关火锅",
  confidence: 0.9,
  topic_id: "t1",
  topic_name: "吃火锅",
  source: "对话",
  scope: "全局（你）",
  version: 1,
  evidence_ids: ["msg_1", "tr_2"],
  epistemic_status: "user_explicit",
  created_at: "2026-10-01T10:00:00+08:00",
  updated_at: "2026-10-02T11:00:00+08:00",
};

function mountPanel(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(KnowledgePanel, { props, global: { plugins: [pinia] } });
}

function detailVisible(w: ReturnType<typeof mountPanel>): boolean {
  const el = w.find('[data-k-detail]').element as HTMLElement;
  return el.style.display !== "none";
}
function listVisible(w: ReturnType<typeof mountPanel>): boolean {
  const el = w.find('[data-k-list]').element as HTMLElement;
  return el.style.display !== "none";
}

async function openFirst(w: ReturnType<typeof mountPanel>) {
  await flushPromises();
  await w.find('[data-k-id="kn_1"]').trigger("click");
  await nextTick();
  return w;
}

function conflictError(version = 2) {
  return Object.assign(new Error("/api/knowledge/kn_1/revise -> 409: 版本冲突"), {
    status: 409,
    body: { conflict: true, current_version: version, current_id: "kn_v2", error: "版本冲突" },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [K] });
  mocks.apiMock.listTopics.mockResolvedValue({ topics: [] });
  mocks.apiMock.reviseKnowledge.mockResolvedValue({ ok: true, knowledge_id: "kn_new", version: 2 });
});

describe("KnowledgePanel 列表 → 详情 → 返回", () => {
  it("列表渲染分类/状态徽章，点条目打开详情", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.find('[data-k-list]').exists()).toBe(true);
    expect(w.find(".k-cat").text()).toBe("用户画像");
    expect(w.find('[data-k-id="kn_1"] .k-state').text()).toBe("待审核");
    await w.find('[data-k-id="kn_1"]').trigger("click");
    await nextTick();
    expect(detailVisible(w)).toBe(true);
    expect(listVisible(w)).toBe(false);
    expect(w.find('[data-k-detail]').text()).toContain("用户最爱五里关火锅");
    w.unmount();
  });

  it("返回列表保留搜索词、筛选与滚动位置（不重建、不跳顶部）", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-k-search]').setValue("火锅");
    await flushPromises();
    const root = w.find('[data-scroll="knowledge-list"]').element as HTMLElement;
    root.scrollTop = 180;
    expect(w.find('[data-k-id="kn_1"]').exists()).toBe(true);

    await w.find('[data-k-id="kn_1"]').trigger("click");
    await nextTick();
    expect(detailVisible(w)).toBe(true);

    await w.find('[data-k-back]').trigger("click");
    await flushPromises();
    await nextTick();
    expect(listVisible(w)).toBe(true);
    expect((w.find('[data-k-search]').element as HTMLInputElement).value).toBe("火锅");
    expect(root.scrollTop).toBe(180);
    w.unmount();
  });

  it("详情保留基线能力：来源/分类/状态/适用范围/关联话题/依据数/版本与时间", async () => {
    const w = mountPanel();
    await openFirst(w);
    const detail = w.find('[data-k-detail]').text();
    expect(detail).toContain("来源");
    expect(detail).toContain("对话");
    expect(detail).toContain("用户画像");
    expect(detail).toContain("待审核");
    expect(detail).toContain("适用范围");
    expect(detail).toContain("吃火锅");
    expect(detail).toContain("2 条真实记录");
    expect(detail).toContain("v1");
    expect(detail).toContain("2026-10-02T11:00:00+08:00");
    expect(w.find(".k-scope-select").exists()).toBe(true);
    w.unmount();
  });

  it("关联话题仍 emit focus-topic", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find(".k-topic-link").trigger("click");
    expect(w.emitted("focus-topic")).toEqual([["t1"]]);
    w.unmount();
  });

  it("scope 调整调用 setKnowledgeScope 并刷新", async () => {
    mocks.apiMock.listTopics.mockResolvedValue({
      topics: [
        {
          topic_id: "t9",
          title: "SQLite",
          keywords: [],
          fragment_count: 0,
          last_activity: null,
          summary_preview: null,
        },
      ],
    });
    const w = mountPanel();
    await openFirst(w);
    await w.find(".k-scope-select").trigger("click");
    const opt = w.findAll(".k-scope-select .opt").find((o) => o.text().includes("SQLite"));
    await opt!.trigger("click");
    await flushPromises();
    expect(mocks.apiMock.setKnowledgeScope).toHaveBeenCalledWith("kn_1", {
      type: "topic",
      topic_id: "t9",
    });
    w.unmount();
  });
});

describe("KnowledgePanel 条目动作（详情内，能力不丢）", () => {
  it("批准调用 verifyKnowledge 并刷新", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find(".k-approve").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.verifyKnowledge).toHaveBeenCalledWith("kn_1");
    expect(mocks.apiMock.listKnowledge).toHaveBeenCalledTimes(2);
    expect(mocks.refreshPending).toHaveBeenCalled();
    w.unmount();
  });

  it("打回调用 rejectKnowledge", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find(".k-reject").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.rejectKnowledge).toHaveBeenCalledWith("kn_1");
    w.unmount();
  });

  it("标记结束/恢复调用 endKnowledge/resumeKnowledge", async () => {
    mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [{ ...K, ended: true }] });
    const w = mountPanel();
    await openFirst(w);
    await w.find(".k-toggle-ended").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.resumeKnowledge).toHaveBeenCalledWith("kn_1");
    w.unmount();
  });

  it("归档先展开确认层说明影响，确认后才调用接口", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find(".k-archive").trigger("click");
    const confirm = w.find('[data-k-detail] .qio-confirm');
    expect(confirm.exists()).toBe(true);
    expect(confirm.text()).toContain("不再参与回答");
    expect(mocks.apiMock.revokeKnowledge).not.toHaveBeenCalled();
    await confirm.findAll("button")[0].trigger("click");
    expect(w.find('[data-k-detail] .qio-confirm').exists()).toBe(false);
    await w.find(".k-archive").trigger("click");
    await w.find('[data-k-detail] .qio-confirm').findAll("button")[1].trigger("click");
    await flushPromises();
    expect(mocks.apiMock.revokeKnowledge).toHaveBeenCalledWith("kn_1");
    w.unmount();
  });

  it("新建调用 createKnowledge（可选关联话题）", async () => {
    mocks.apiMock.listTopics.mockResolvedValue({
      topics: [
        {
          topic_id: "t9",
          title: "测试话题",
          keywords: [],
          fragment_count: 0,
          last_activity: null,
          summary_preview: null,
        },
      ],
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
});

describe("KnowledgePanel 草稿化编辑（§6 关键要求）", () => {
  it("进入编辑只是本地草稿：不提交任何请求", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-k-edit]').trigger("click");
    expect(w.find('[data-k-draft]').exists()).toBe(true);
    expect(mocks.apiMock.reviseKnowledge).not.toHaveBeenCalled();
    const area = w.find('[data-k-draft] textarea');
    await area.setValue("用户最爱的是毛肚");
    expect(mocks.apiMock.reviseKnowledge).not.toHaveBeenCalled();
    w.unmount();
  });

  it("保存一次性提交，并带上开始编辑时的 expected_version", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-k-edit]').trigger("click");
    await w.find('[data-k-draft] textarea').setValue("用户最爱的是毛肚");
    await w.find('[data-k-save]').trigger("click");
    await flushPromises();
    expect(mocks.apiMock.reviseKnowledge).toHaveBeenCalledTimes(1);
    expect(mocks.apiMock.reviseKnowledge).toHaveBeenCalledWith("kn_1", "用户最爱的是毛肚", {
      expectedVersion: 1,
    });
    expect(mocks.refreshPending).toHaveBeenCalled();
    w.unmount();
  });

  it("取消丢弃草稿，原内容不变（不调用接口）", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-k-edit]').trigger("click");
    await w.find('[data-k-draft] textarea').setValue("乱改的内容");
    await w.find('[data-k-cancel]').trigger("click");
    await nextTick();
    expect(mocks.apiMock.reviseKnowledge).not.toHaveBeenCalled();
    expect(w.find('[data-k-draft]').exists()).toBe(false);
    expect(w.find('[data-k-detail]').text()).toContain("用户最爱五里关火锅");
    w.unmount();
  });

  it("保存失败：草稿与可读错误都保留", async () => {
    mocks.apiMock.reviseKnowledge.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-k-edit]').trigger("click");
    await w.find('[data-k-draft] textarea').setValue("用户最爱的是毛肚");
    await w.find('[data-k-save]').trigger("click");
    await flushPromises();
    expect(w.find('[data-k-draft]').exists()).toBe(true);
    expect((w.find('[data-k-draft] textarea').element as HTMLTextAreaElement).value).toBe("用户最爱的是毛肚");
    expect(w.text()).toContain("offline");
    expect(w.text()).toContain("草稿还在");
    w.unmount();
  });

  it("409 版本冲突：提示冲突、保留用户输入、刷新服务端当前内容（不静默覆盖）", async () => {
    mocks.apiMock.reviseKnowledge.mockRejectedValueOnce(conflictError(2));
    mocks.apiMock.listKnowledge
      .mockResolvedValueOnce({ knowledge: [K] })
      .mockResolvedValueOnce({ knowledge: [{ ...K, content: "别处改过的内容", version: 2 }] });
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-k-edit]').trigger("click");
    await w.find('[data-k-draft] textarea').setValue("我的修改");
    await w.find('[data-k-save]').trigger("click");
    await flushPromises();
    // 用户输入没有丢
    expect((w.find('[data-k-draft] textarea').element as HTMLTextAreaElement).value).toBe("我的修改");
    // 冲突说明 + 服务端当前内容可见
    expect(w.text()).toContain("被更新过");
    expect(w.text()).toContain("别处改过的内容");
    w.unmount();
  });

  it("未保存修改时返回先确认：继续编辑 / 放弃修改", async () => {
    const w = mountPanel();
    await openFirst(w);
    await w.find('[data-k-edit]').trigger("click");
    await w.find('[data-k-draft] textarea').setValue("没保存的修改");
    await w.find('[data-k-back]').trigger("click");
    await nextTick();
    expect(w.find('[data-unsaved-guard]').exists()).toBe(true);
    expect(listVisible(w)).toBe(false);

    await w.find('[data-unsaved-keep]').trigger("click");
    await nextTick();
    expect(w.find('[data-unsaved-guard]').exists()).toBe(false);
    expect(w.find('[data-k-draft]').exists()).toBe(true);

    await w.find('[data-k-back]').trigger("click");
    await nextTick();
    await w.find('[data-unsaved-discard]').trigger("click");
    await flushPromises();
    await nextTick();
    expect(listVisible(w)).toBe(true);
    expect(mocks.apiMock.reviseKnowledge).not.toHaveBeenCalled();
    w.unmount();
  });
});

describe("KnowledgePanel 空态与失败区分", () => {
  it("加载失败：显示可读错误与重试入口，不显示「暂无知识记录」", async () => {
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

  it("「暂无记录」与「没有匹配」用不同文案，并能一键清除筛选", async () => {
    mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [] });
    const empty = mountPanel();
    await flushPromises();
    expect(empty.find(".k-empty").text()).toContain("暂无知识记录");
    empty.unmount();

    mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [K] });
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-k-search]').setValue("不存在的关键词");
    await flushPromises();
    expect(w.find(".k-empty").text()).toContain("没有匹配");
    await w.find(".k-clear-filters").trigger("click");
    await flushPromises();
    expect(w.find('[data-k-id="kn_1"]').exists()).toBe(true);
    w.unmount();
  });
});

describe("KnowledgePanel 来源筛选（阶段二 C：后台整理）", () => {
  it("后台维护写入的条目（source=后台整理）能被「后台整理」筛出", async () => {
    mocks.apiMock.listKnowledge.mockResolvedValue({
      knowledge: [
        {
          ...K,
          id: "kn_bg",
          content: "后台整理写入的知识条目",
          source: "后台整理",
          provenance_source: "maintenance_merge",
        },
        {
          ...K,
          id: "kn_chat",
          content: "主对话学到的知识条目",
          source: "主对话",
          provenance_source: "main_chat",
        },
      ],
    });
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-k-filter-source]').trigger("click");
    await w
      .findAll('[data-k-filter-source] .opt')
      .find((option) => option.text() === "后台整理")!
      .trigger("click");
    await nextTick();
    expect(w.text()).toContain("后台整理写入的知识条目");
    expect(w.text()).not.toContain("主对话学到的知识条目");
    w.unmount();
  });
});

describe("KnowledgePanel focusId（从搜索结果/记忆页跳转）", () => {
  it("打开对应条目的详情", async () => {
    const w = mountPanel({ focusId: "kn_1" });
    await flushPromises();
    await nextTick();
    expect(detailVisible(w)).toBe(true);
    expect(w.find('[data-k-detail]').text()).toContain("用户最爱五里关火锅");
    w.unmount();
  });
});
