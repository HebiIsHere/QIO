import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import KnowledgePanel from "../KnowledgePanel.vue";
import EntityPanel from "../EntityPanel.vue";
import type { EntityCard, KnowledgeItem } from "../../../services/api";

/**
 * PM-QIO032 #34：子页标题与详情层级。
 *
 * - 子页标题只出现一次（面板头/标签页已经说明这是「知识 / 实体」，详情头不再重复打印）；
 * - 实体类型用可读名（未知值原样保留）；
 * - 适用范围贴着现有下拉控件显示，不再单独重复打印一遍；
 * - 用户查证所需详情保持默认可见，内部编号与原始时间戳进「更多信息」折叠区。
 */

const mocks = vi.hoisted(() => ({
  refreshPending: vi.fn(async () => {}),
  apiMock: {
    listKnowledge: vi.fn<() => Promise<{ knowledge: KnowledgeItem[] }>>(async () => ({ knowledge: [] })),
    listTopics: vi.fn(async () => ({ topics: [] })),
    listEntities: vi.fn<() => Promise<{ entities: EntityCard[] }>>(async () => ({ entities: [] })),
    reviseEntity: vi.fn(async () => ({ ok: true, entity: null })),
    revokeEntity: vi.fn(async () => ({ ok: true })),
    reviseKnowledge: vi.fn(async () => ({ ok: true, knowledge_id: "kn_new", version: 2 })),
    createKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} })),
    verifyKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} })),
    rejectKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} })),
    revokeKnowledge: vi.fn(async () => ({ ok: true })),
    endKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} })),
    resumeKnowledge: vi.fn(async () => ({ ok: true, knowledge: {} })),
    setKnowledgeScope: vi.fn(async () => ({ ok: true, knowledge: {} })),
  },
  candidatesMock: {
    listEntityCandidates: vi.fn(async () => ({
      entity: null,
      candidates: [],
      total: 0,
      truncated: false,
    })),
    adoptCandidate: vi.fn(async () => ({ ok: true, entity: null, adopted: null })),
    dismissCandidate: vi.fn(async () => ({ ok: true, dismissed: true })),
  },
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return { ...actual, api: mocks.apiMock };
});

vi.mock("../../../services/entityCandidatesApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/entityCandidatesApi")>();
  return { ...actual, ...mocks.candidatesMock };
});

vi.mock("../../../stores/planetPending", () => ({
  usePlanetPendingStore: () => ({ count: 0, refresh: mocks.refreshPending }),
}));

const K: KnowledgeItem = {
  id: "kn_1",
  category: "user_profile",
  state: "active",
  content: "用户称呼：祠莎",
  confidence: 0.9,
  topic_id: null,
  topic_name: null,
  source: "引导",
  scope: "全局（你）",
  version: 2,
  evidence_ids: ["msg_1"],
  epistemic_status: "user_explicit",
  created_at: "2026-10-01T10:00:00+08:00",
  updated_at: "2026-10-02T11:00:00+08:00",
};

const CARD: EntityCard = {
  id: "ec_1",
  node_id: "node_9f2c",
  name: "王翠华",
  aliases: ["妈"],
  kind: "person",
  summary: "我妈妈",
  attributes: [{ key: "生日", value: "3 月 5 日", confidence: 0.9 }],
  relations: [],
  state: "active",
  revision: 3,
  identity: "用户家里的长辈",
  creation_reason: "持续对话出现",
  evidence_ids: ["msg_1"],
  created_at: "2026-10-01T10:00:00+08:00",
  updated_at: "2026-10-02T11:00:00+08:00",
};

function pinia() {
  const p = createPinia();
  setActivePinia(p);
  return p;
}

async function mountKnowledge(): Promise<ReturnType<typeof mount>> {
  const w = mount(KnowledgePanel, { global: { plugins: [pinia()] } });
  await flushPromises();
  return w;
}

async function mountEntity(): Promise<ReturnType<typeof mount>> {
  const w = mount(EntityPanel, { global: { plugins: [pinia()] } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listKnowledge.mockResolvedValue({ knowledge: [K] });
  mocks.apiMock.listTopics.mockResolvedValue({ topics: [] });
  mocks.apiMock.listEntities.mockResolvedValue({ entities: [CARD] });
});

describe("#34 知识子页：标题/范围/详情层级", () => {
  it("详情头不再重复打印子页标题「知识」", async () => {
    const w = await mountKnowledge();
    await w.find('[data-k-id="kn_1"]').trigger("click");
    await nextTick();
    const head = w.find(".k-detail-head").text();
    expect(head).not.toContain("知识详情");
    expect(head).toContain("返回");
    expect(head).toContain("修正");
    w.unmount();
  });

  it("适用范围只在现有下拉控件里出现一次", async () => {
    const w = await mountKnowledge();
    await w.find('[data-k-id="kn_1"]').trigger("click");
    await nextTick();
    const select = w.find(".k-scope-select");
    expect(select.exists()).toBe(true);
    expect(select.text()).toContain("全局（你）");
    // 不再有第二份相同的范围文字（只有下拉本身）
    expect(w.find(".k-scope-detail").exists()).toBe(false);
    w.unmount();
  });

  it("查证详情默认可见；原始时间戳与内部编号在「更多信息」里默认收起", async () => {
    const w = await mountKnowledge();
    await w.find('[data-k-id="kn_1"]').trigger("click");
    await nextTick();
    const detail = w.find('[data-k-detail]').text();
    // 用户查证需要的仍在默认层
    expect(detail).toContain("来源");
    expect(detail).toContain("分类");
    expect(detail).toContain("状态");
    expect(detail).toContain("适用范围");
    expect(detail).toContain("依据");
    expect(detail).toContain("v2");

    const more = w.find("[data-k-more]");
    expect(more.exists()).toBe(true);
    expect(more.attributes("open")).toBeUndefined();
    expect((more.element as HTMLDetailsElement).open).toBe(false);
    expect(more.text()).toContain("2026-10-02T11:00:00+08:00");
    expect(more.text()).toContain("kn_1");
    w.unmount();
  });
});

describe("#34 实体子页：可读类型与详情层级", () => {
  it("内部 kind slug 显示可读中文名（列表与详情一致）", async () => {
    const w = await mountEntity();
    expect(w.find(".e-item .e-kind").text()).toBe("人物");
    await w.find('[data-e-id="ec_1"]').trigger("click");
    await nextTick();
    expect(w.find(".e-read-kind").text()).toBe("人物");
    w.unmount();
  });

  it("未知类型原样保留（不臆造）", async () => {
    mocks.apiMock.listEntities.mockResolvedValue({
      entities: [{ ...CARD, kind: "custom_kind" }],
    });
    const w = await mountEntity();
    expect(w.find(".e-item .e-kind").text()).toBe("custom_kind");
    await w.find('[data-e-id="ec_1"]').trigger("click");
    await nextTick();
    expect(w.find(".e-read-kind").text()).toBe("custom_kind");
    w.unmount();
  });

  it("详情标题只出现一次；节点编号与原始时间戳默认收起，查证详情保留", async () => {
    const w = await mountEntity();
    await w.find('[data-e-id="ec_1"]').trigger("click");
    await nextTick();
    const detail = w.find('[data-e-detail]');
    const text = detail.text();
    expect(text.split("王翠华").length - 1).toBe(1);
    // 查证所需
    expect(text).toContain("我妈妈");
    expect(text).toContain("用户家里的长辈");
    expect(text).toContain("持续对话出现");
    expect(text).toContain("生日 = 3 月 5 日");
    expect(text).toContain("v3");

    const more = detail.find("[data-e-more]");
    expect(more.exists()).toBe(true);
    expect(more.attributes("open")).toBeUndefined();
    expect((more.element as HTMLDetailsElement).open).toBe(false);
    expect(more.text()).toContain("node_9f2c");
    expect(more.text()).toContain("2026-10-02T11:00:00+08:00");
    w.unmount();
  });
});
