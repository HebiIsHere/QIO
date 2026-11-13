/**
 * #31 待处理列表分源状态：每个来源分别维护 loading / error / empty / success。
 *
 * 硬要求：
 * - 局部失败保留成功部分，用一处状态说明失败范围；
 * - 同一来源重复刷新替换警告/错误，不叠加多条；
 * - 只有全部来源成功且为空才显示总空态。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
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
      refreshKnowledgePending: vi.fn(async () => {}),
    }),
  };
});

vi.mock("../../../stores/planetPending", () => ({
  usePlanetPendingStore: () => ({ count: 0, refresh: mocks.refreshPending }),
}));

const KNOWLEDGE_ITEM: PendingItemView = {
  id: "kn_example",
  kind: "knowledge_proposal",
  status: "awaiting_approval",
  handler: "knowledge_proposal",
  title: "示例知识条目",
  knowledge: [
    {
      id: "kn_example",
      version: 1,
      content: "示例知识条目",
      category: "general_fact",
      scope: ["全局（你）"],
      source: "测试",
      state: "pending_review",
    },
  ],
  before: [],
  after: [],
  evidence: [],
  actions: ["approve", "reject"],
  created_at: "2026-10-20T10:00:00+08:00",
  updated_at: "2026-10-21T10:00:00+08:00",
};

const CANDIDATE: EntityCandidateView = {
  candidate_id: "pc_example",
  entity_id: "ec_example",
  entity_name: "示例实体",
  card_state: "active",
  field: "summary",
  field_label: "摘要",
  kind: "summary",
  current_value: "旧值",
  candidate_value: "候选值",
  reason: "user_value_conflict",
  reason_label: "你已经手动写过这个字段",
  created_at: "2026-10-23T10:00:00+08:00",
  card_revision: 2,
  adoptable: true,
  blocked_reason: "",
};

function listing(items: PendingItemView[] = [], warnings: string[] = []) {
  return {
    items,
    total: items.length,
    truncated: false,
    maintenance_available: true,
    warnings,
  };
}
function candidateListing(candidates: EntityCandidateView[] = []) {
  return {
    candidates,
    total: candidates.length,
    shown: candidates.length,
    truncated: false,
  };
}

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(PendingItems, { global: { plugins: [pinia] } });
}

async function refresh(w: ReturnType<typeof mountPanel>) {
  await w.find(".p-refresh").trigger("click");
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listMemoryPending.mockResolvedValue(listing());
  mocks.listAllCandidates.mockResolvedValue(candidateListing());
});

describe("#31 待处理：只有全部来源成功且为空才是总空态", () => {
  it("两个来源都成功且为空：显示总空态", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.find("[data-pending-empty]").text()).toContain("暂无待处理事项");
    expect(w.find("[data-pending-source-status]").exists()).toBe(false);
    w.unmount();
  });

  it("一个来源失败且另一来源为空：说明失败范围，不显示总空态", async () => {
    mocks.listAllCandidates.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    const status = w.find("[data-pending-source-status]");
    expect(status.exists()).toBe(true);
    expect(status.text()).toContain("暂时读不到待处理事项");
    expect(status.text()).toContain("实体更新候选");
    expect(status.text()).toContain("offline");
    expect(w.text()).not.toContain("暂无待处理事项");
    w.unmount();
  });

  it("局部失败保留成功来源的真实条目", async () => {
    mocks.listMemoryPending.mockRejectedValueOnce(new Error("memory offline"));
    mocks.listAllCandidates.mockResolvedValueOnce(candidateListing([CANDIDATE]));
    const w = mountPanel();
    await flushPromises();
    expect(w.find('[data-pending-id="pc_example"]').exists()).toBe(true);
    const status = w.find("[data-pending-source-status]");
    expect(status.text()).toContain("记忆待确认");
    expect(status.text()).not.toContain("实体更新候选");
    expect(w.text()).not.toContain("暂无待处理事项");
    w.unmount();
  });

  it("失败来源可以单独重试，成功后回到正常列表", async () => {
    mocks.listAllCandidates.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    expect(w.find("[data-pending-source-status]").exists()).toBe(true);
    mocks.listAllCandidates.mockResolvedValueOnce(candidateListing([CANDIDATE]));
    await w.find(".p-retry").trigger("click");
    await flushPromises();
    expect(w.find("[data-pending-source-status]").exists()).toBe(false);
    expect(w.find('[data-pending-id="pc_example"]').exists()).toBe(true);
    w.unmount();
  });
});

describe("#31 待处理：重复刷新替换同一来源的错误与警告，不叠加", () => {
  it("同一来源连续失败：只保留最新一条错误", async () => {
    mocks.listMemoryPending.mockRejectedValueOnce(new Error("第一次读取失败"));
    const w = mountPanel();
    await flushPromises();
    expect(w.text()).toContain("第一次读取失败");

    mocks.listMemoryPending.mockRejectedValueOnce(new Error("第二次读取失败"));
    await refresh(w);
    expect(w.text()).toContain("第二次读取失败");
    expect(w.text()).not.toContain("第一次读取失败");
    expect(w.findAll('[data-pending-source="memory"]').length).toBe(1);
    w.unmount();
  });

  it("同一来源连续刷新的服务端警告：用新一组替换，不叠加旧警告", async () => {
    mocks.listMemoryPending.mockResolvedValueOnce(listing([], ["旧警告：维护事项存储异常"]));
    const w = mountPanel();
    await flushPromises();
    expect(w.text()).toContain("旧警告");

    mocks.listMemoryPending.mockResolvedValueOnce(listing([], ["新警告：维护事项已恢复"]));
    await refresh(w);
    expect(w.text()).toContain("新警告");
    expect(w.text()).not.toContain("旧警告");
    w.unmount();
  });

  it("一次成功后错误状态消失，不会与上一次失败叠加", async () => {
    mocks.listAllCandidates.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    expect(w.find("[data-pending-source-status]").exists()).toBe(true);
    mocks.listAllCandidates.mockResolvedValueOnce(candidateListing([CANDIDATE]));
    await refresh(w);
    expect(w.find("[data-pending-source-status]").exists()).toBe(false);
    expect(w.text()).not.toContain("offline");
    expect(w.find('[data-pending-id="pc_example"]').exists()).toBe(true);
    w.unmount();
  });
});
