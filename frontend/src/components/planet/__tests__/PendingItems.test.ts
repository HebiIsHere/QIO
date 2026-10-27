import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import PendingItems from "../PendingItems.vue";
import type { PendingItemView } from "../../../services/api";
import type { EntityCandidateView } from "../../../services/entityCandidatesApi";

const mocks = vi.hoisted(() => ({
  refreshPending: vi.fn(async () => {}),
  submitApproval: vi.fn(async () => ({ ok: true, decision: "approve" })),
  answerPending: vi.fn(async () => ({ ok: true, status: "processed", item_id: "mq_1" })),
  deferPending: vi.fn(async () => ({ ok: true, status: "deferred", item: null })),
  adoptCandidate: vi.fn(async () => ({ ok: true, entity: null, adopted: null })),
  dismissCandidate: vi.fn(async () => ({ ok: true, dismissed: true })),
  listAllCandidates: vi.fn(),
  listMemoryPending: vi.fn(),
  refreshUnified: vi.fn(async () => {}),
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listMemoryPending: mocks.listMemoryPending,
      answerMemoryPending: mocks.answerPending,
      deferMemoryPending: mocks.deferPending,
    },
  };
});

vi.mock("../../../services/entityCandidatesApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/entityCandidatesApi")>();
  return {
    ...actual,
    listAllCandidates: mocks.listAllCandidates,
    adoptCandidate: mocks.adoptCandidate,
    dismissCandidate: mocks.dismissCandidate,
  };
});

vi.mock("../../../stores/unifiedApprovals", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../stores/unifiedApprovals")>();
  return {
    ...actual,
    submitKnowledgeApprovalRequest: mocks.submitApproval,
    useUnifiedApprovalsStore: () => ({
      knowledgeLoadedAt: null,
      refreshKnowledgePending: mocks.refreshUnified,
    }),
  };
});

vi.mock("../../../stores/planetPending", () => ({
  usePlanetPendingStore: () => ({ count: 0, refresh: mocks.refreshPending }),
}));

const KNOWLEDGE_ITEM: PendingItemView = {
  id: "kn_1",
  kind: "knowledge_proposal",
  status: "awaiting_approval",
  handler: "knowledge_proposal",
  title: "用户喜欢毛肚",
  knowledge: [
    {
      id: "kn_1",
      version: 2,
      content: "用户喜欢毛肚",
      category: "user_profile",
      scope: ["全局（你）"],
      epistemic_status: "user_explicit",
      source: "对话",
      state: "pending_review",
    },
  ],
  before: [{ content: "旧内容" }],
  after: [{ content: "用户喜欢毛肚" }],
  evidence: [{ id: "msg_1" }],
  actions: ["approve", "approve_modified", "reject"],
  created_at: "2026-10-20T10:00:00+08:00",
  updated_at: "2026-10-21T10:00:00+08:00",
};

const QUESTION_ITEM: PendingItemView = {
  id: "mq_1",
  kind: "maintenance_question",
  status: "awaiting_answer",
  handler: "maintenance",
  title: "关于健康记录",
  question: "你最近还养着那只鹅吗？",
  knowledge: [
    {
      id: "kn_q",
      version: 1,
      content: "用户养了一只鹅",
      category: "general_fact",
      scope: ["全局（你）"],
      source: "对话",
      state: "active",
    },
  ],
  before: [],
  after: [],
  evidence: [{ id: "kn_q" }],
  actions: ["answer", "defer"],
  created_at: "2026-10-22T10:00:00+08:00",
  updated_at: "2026-10-22T10:00:00+08:00",
};

const CANDIDATE: EntityCandidateView = {
  candidate_id: "pc_1",
  entity_id: "ec_1",
  entity_name: "王翠华",
  card_state: "active",
  field: "summary",
  field_label: "摘要",
  kind: "summary",
  current_value: "我妈妈",
  candidate_value: "我妈妈，住在成都",
  reason: "user_value_conflict",
  reason_label: "你已经手动写过这个字段，自动结果不会覆盖它",
  created_at: "2026-10-23T10:00:00+08:00",
  card_revision: 4,
  adoptable: true,
  blocked_reason: "",
};

function listing(items: PendingItemView[] = [KNOWLEDGE_ITEM, QUESTION_ITEM]) {
  return {
    items,
    total: items.length,
    truncated: false,
    maintenance_available: true,
    warnings: [],
  };
}
function candidateListing(candidates: EntityCandidateView[] = [CANDIDATE]) {
  return {
    candidates,
    total: candidates.length,
    shown: candidates.length,
    truncated: false,
  };
}

function mountPanel(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(PendingItems, { props, global: { plugins: [pinia] } });
}

function detailVisible(w: ReturnType<typeof mountPanel>): boolean {
  return (w.find('[data-pending-detail]').element as HTMLElement).style.display !== "none";
}
function listVisible(w: ReturnType<typeof mountPanel>): boolean {
  return (w.find('[data-pending-list]').element as HTMLElement).style.display !== "none";
}
async function openRow(w: ReturnType<typeof mountPanel>, id: string) {
  await flushPromises();
  await w.find('[data-pending-id="' + id + '"]').trigger("click");
  await nextTick();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listMemoryPending.mockResolvedValue(listing());
  mocks.listAllCandidates.mockResolvedValue(candidateListing());
  mocks.submitApproval.mockResolvedValue({ ok: true, decision: "approve" });
  mocks.answerPending.mockResolvedValue({ ok: true, status: "processed", item_id: "mq_1" });
  mocks.deferPending.mockResolvedValue({ ok: true, status: "deferred", item: null });
  mocks.adoptCandidate.mockResolvedValue({ ok: true, entity: null, adopted: null });
  mocks.dismissCandidate.mockResolvedValue({ ok: true, dismissed: true });
});

describe("PendingItems 合并两类真实来源", () => {
  it("知识建议 + 整理问题 + 实体候选都在同一个列表里，数量真实", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.find('[data-pending-id="kn_1"]').attributes("data-pending-kind")).toBe("knowledge_proposal");
    expect(w.find('[data-pending-id="mq_1"]').attributes("data-pending-kind")).toBe("maintenance_question");
    expect(w.find('[data-pending-id="pc_1"]').attributes("data-pending-kind")).toBe("entity_candidate");
    expect(w.find(".p-count").text()).toBe("3 项");
    w.unmount();
  });

  it("一类来源读失败仍然显示另一类，并如实提示", async () => {
    mocks.listAllCandidates.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    expect(w.find('[data-pending-id="kn_1"]').exists()).toBe(true);
    expect(w.text()).toContain("实体更新候选");
    expect(w.text()).toContain("offline");
    w.unmount();
  });

  it("两类都读失败：给原因与重试，不显示「暂无待处理事项」", async () => {
    mocks.listMemoryPending.mockRejectedValueOnce(new Error("boom"));
    mocks.listAllCandidates.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    expect(w.find(".p-error").exists()).toBe(true);
    expect(w.find(".p-error").text()).toContain("offline");
    expect(w.text()).not.toContain("暂无待处理事项");

    mocks.listMemoryPending.mockResolvedValueOnce(listing());
    mocks.listAllCandidates.mockResolvedValueOnce(candidateListing());
    await w.find(".p-error button").trigger("click");
    await flushPromises();
    expect(w.find(".p-error").exists()).toBe(false);
    w.unmount();
  });

  it("维护事项存储不可用时如实说明", async () => {
    mocks.listMemoryPending.mockResolvedValueOnce({
      items: [KNOWLEDGE_ITEM],
      total: 1,
      truncated: false,
      maintenance_available: false,
      warnings: ["维护事项存储不可用（迁移 47 未应用）"],
    });
    const w = mountPanel();
    await flushPromises();
    expect(w.text()).toContain("维护事项存储不可用");
    w.unmount();
  });
});

describe("PendingItems 列表 → 详情 → 返回", () => {
  it("打开详情展示判断所需内容与来源，返回保留滚动位置", async () => {
    const w = mountPanel();
    await flushPromises();
    const root = w.find('[data-scroll="pending-list"]').element as HTMLElement;
    root.scrollTop = 150;
    await w.find('[data-pending-id="kn_1"]').trigger("click");
    await nextTick();
    expect(detailVisible(w)).toBe(true);
    const detail = w.find('[data-pending-detail]').text();
    expect(detail).toContain("用户喜欢毛肚");
    expect(detail).toContain("来源：对话");
    expect(detail).toContain("旧内容");
    expect(detail).toContain("1 条真实记录");
    await w.find('[data-pending-back]').trigger("click");
    await flushPromises();
    await nextTick();
    expect(listVisible(w)).toBe(true);
    expect(root.scrollTop).toBe(150);
    w.unmount();
  });

  it("focusId 直接打开对应事项", async () => {
    const w = mountPanel({ focusId: "pc_1" });
    await flushPromises();
    await nextTick();
    expect(detailVisible(w)).toBe(true);
    expect(w.find('[data-pending-detail]').text()).toContain("王翠华");
    w.unmount();
  });
});

describe("PendingItems 知识建议动作（确认 / 修改后确认 / 不保存）", () => {
  it("确认走原子审批入口并带上 expected_*", async () => {
    const w = mountPanel();
    await openRow(w, "kn_1");
    await w.find('[data-pending-action="approve"]').trigger("click");
    await flushPromises();
    expect(mocks.submitApproval).toHaveBeenCalledTimes(1);
    expect(mocks.submitApproval).toHaveBeenCalledWith("kn_1", "approve", {
      content: undefined,
      expectedVersion: 2,
      expectedContent: "用户喜欢毛肚",
      expectedUpdatedAt: "2026-10-21T10:00:00+08:00",
    });
    expect(mocks.refreshPending).toHaveBeenCalled();
    expect(listVisible(w)).toBe(true);
    w.unmount();
  });

  it("修改后确认只发一个 approve_modified（不会先确认原文再改）", async () => {
    const w = mountPanel();
    await openRow(w, "kn_1");
    await w.find('[data-pending-action="approve_modified"]').trigger("click");
    await nextTick();
    const area = w.find(".p-modify");
    await area.setValue("用户特别喜欢毛肚");
    await w.find('[data-pending-action="approve_modified"]').trigger("click");
    await flushPromises();
    expect(mocks.submitApproval).toHaveBeenCalledTimes(1);
    expect(mocks.submitApproval).toHaveBeenCalledWith("kn_1", "approve_modified", {
      content: "用户特别喜欢毛肚",
      expectedVersion: 2,
      expectedContent: "用户喜欢毛肚",
      expectedUpdatedAt: "2026-10-21T10:00:00+08:00",
    });
    w.unmount();
  });

  it("不保存走 reject 决策（不写第二个接口）", async () => {
    const w = mountPanel();
    await openRow(w, "kn_1");
    await w.find('[data-pending-action="reject"]').trigger("click");
    await flushPromises();
    expect(mocks.submitApproval).toHaveBeenCalledWith("kn_1", "reject", expect.any(Object));
    w.unmount();
  });

  it("409 版本变化：刷新真实状态、保留草稿、不显示为成功", async () => {
    mocks.submitApproval.mockRejectedValueOnce(
      Object.assign(new Error("409 version_changed"), {
        status: 409,
        body: { code: "version_changed", error: "版本已变化" },
      }),
    );
    const w = mountPanel();
    await openRow(w, "kn_1");
    await w.find('[data-pending-action="approve_modified"]').trigger("click");
    await nextTick();
    await w.find(".p-modify").setValue("我的修改");
    mocks.listMemoryPending.mockResolvedValue(listing([KNOWLEDGE_ITEM]));
    await w.find('[data-pending-action="approve_modified"]').trigger("click");
    await flushPromises();
    // 草稿还在（没有静默丢弃）
    expect((w.find(".p-modify").element as HTMLTextAreaElement).value).toBe("我的修改");
    // 提示是冲突/刷新，而不是成功
    expect(w.find(".p-row-notice").text()).toContain("已经变化");
    expect(mocks.submitApproval).toHaveBeenCalledTimes(1);
    w.unmount();
  });

  it("防重复提交：同一条事项只有一个请求在飞", async () => {
    const control: { release?: (value: { ok: boolean; decision: string }) => void } = {};
    mocks.submitApproval.mockImplementationOnce(
      () => new Promise<{ ok: boolean; decision: string }>((resolve) => { control.release = resolve; }),
    );
    const w = mountPanel();
    await openRow(w, "kn_1");
    const approve = w.find('[data-pending-action="approve"]');
    await approve.trigger("click");
    await approve.trigger("click");
    expect(mocks.submitApproval).toHaveBeenCalledTimes(1);
    control.release?.({ ok: true, decision: "approve" });
    await flushPromises();
    w.unmount();
  });
});

describe("PendingItems 整理问题动作（回答 / 稍后处理）", () => {
  it("提交回答调用 answerMemoryPending，成功后回列表", async () => {
    const w = mountPanel();
    await openRow(w, "mq_1");
    await w.find(".p-answer").setValue("还养着，上周刚看过医生");
    await w.find('[data-pending-action="answer"]').trigger("click");
    await flushPromises();
    expect(mocks.answerPending).toHaveBeenCalledWith("mq_1", "还养着，上周刚看过医生");
    expect(listVisible(w)).toBe(true);
    w.unmount();
  });

  it("回答失败保留输入与错误，可重试", async () => {
    mocks.answerPending.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await openRow(w, "mq_1");
    await w.find(".p-answer").setValue("我的回答");
    await w.find('[data-pending-action="answer"]').trigger("click");
    await flushPromises();
    expect((w.find(".p-answer").element as HTMLTextAreaElement).value).toBe("我的回答");
    expect(w.text()).toContain("回答没有成功");
    w.unmount();
  });

  it("稍后处理调用 deferMemoryPending", async () => {
    const w = mountPanel();
    await openRow(w, "mq_1");
    await w.find('[data-pending-action="defer"]').trigger("click");
    await flushPromises();
    expect(mocks.deferPending).toHaveBeenCalledWith("mq_1");
    w.unmount();
  });
});

describe("PendingItems 实体更新候选动作（采纳 / 丢弃）", () => {
  it("采纳调用 adoptCandidate 并带上卡片 revision", async () => {
    const w = mountPanel();
    await openRow(w, "pc_1");
    await w.find('[data-pending-action="accept"]').trigger("click");
    await flushPromises();
    expect(mocks.adoptCandidate).toHaveBeenCalledWith("ec_1", "pc_1", 4);
    w.unmount();
  });

  it("丢弃调用 dismissCandidate", async () => {
    const w = mountPanel();
    await openRow(w, "pc_1");
    await w.find('[data-pending-action="discard"]').trigger("click");
    await flushPromises();
    expect(mocks.dismissCandidate).toHaveBeenCalledWith("ec_1", "pc_1", 4);
    w.unmount();
  });

  it("409 冲突：提示已被改动并刷新，不假装成功", async () => {
    mocks.adoptCandidate.mockRejectedValueOnce(
      Object.assign(new Error("409 conflict"), { status: 409, currentRevision: 5 }),
    );
    const w = mountPanel();
    await openRow(w, "pc_1");
    await w.find('[data-pending-action="accept"]').trigger("click");
    await flushPromises();
    expect(w.text()).toContain("已被改动");
    expect(w.text()).not.toContain("已采纳这条候选值");
    w.unmount();
  });

  it("不可采纳的候选禁用采纳按钮但允许丢弃", async () => {
    mocks.listAllCandidates.mockResolvedValueOnce(
      candidateListing([
        { ...CANDIDATE, adoptable: false, blocked_reason: "card_archived", card_state: "archived" },
      ]),
    );
    const w = mountPanel();
    await openRow(w, "pc_1");
    expect(w.find('[data-pending-action="accept"]').attributes("disabled")).toBeDefined();
    expect(w.find('[data-pending-action="discard"]').attributes("disabled")).toBeUndefined();
    w.unmount();
  });
});
