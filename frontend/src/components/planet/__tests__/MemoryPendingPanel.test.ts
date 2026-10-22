import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MemoryPendingPanel from "../MemoryPendingPanel.vue";
import type { MaintenanceBatchDetail, MaintenanceBatchSummary, PendingItemView } from "../../../services/api";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  apiMock: {
    listMemoryPending: vi.fn(),
    answerMemoryPending: vi.fn(),
    deferMemoryPending: vi.fn(),
    verifyKnowledge: vi.fn(),
    rejectKnowledge: vi.fn(),
    reviseKnowledge: vi.fn(),
    listMaintenanceBatches: vi.fn(),
    getMaintenanceBatch: vi.fn(),
    previewMaintenanceRestore: vi.fn(),
    restoreMaintenance: vi.fn(),
  },
}));

vi.mock("../../../services/api", () => ({ api: mocks.apiMock, request: mocks.request }));

const proposal: PendingItemView = {
  id: "kp_1",
  kind: "knowledge_proposal",
  status: "awaiting_approval",
  handler: "knowledge_proposal",
  title: "用户偏好五里关火锅",
  question: null,
  knowledge: [
    {
      id: "kp_1",
      version: 1,
      content: "用户偏好五里关火锅",
      category: "general_fact",
      scope: ["全局（你）"],
      epistemic_status: "user_explicit",
      source: "主对话",
      state: "pending_review",
    },
  ],
  before: [],
  after: [{ content: "用户偏好五里关火锅" }],
  evidence: [{ id: "frag_1" }],
  actions: ["approve", "approve_modified", "reject"],
  created_at: "2026-10-21T00:00:00+00:00",
  updated_at: "2026-10-21T00:00:00+00:00",
  batch_id: null,
  error: null,
};

const question: PendingItemView = {
  id: "mq_1",
  kind: "maintenance_question",
  status: "awaiting_answer",
  handler: "maintenance",
  title: "用户提到的常去的火锅店是哪一家？",
  question: "用户提到的常去的火锅店是哪一家？",
  knowledge: [
    {
      id: "kn_1",
      version: 2,
      content: "用户住在成都",
      category: "general_fact",
      scope: ["全局（你）"],
      epistemic_status: null,
      source: "后台整理",
      state: "active",
    },
  ],
  before: [],
  after: [],
  evidence: [{ id: "ev_1" }],
  actions: ["answer", "defer"],
  created_at: "2026-10-21T00:01:00+00:00",
  updated_at: "2026-10-21T00:01:00+00:00",
  batch_id: null,
  error: null,
};

const batch: MaintenanceBatchSummary = {
  id: "mb_1",
  trigger: "change_count",
  status: "completed",
  model: "fake-model",
  created_at: "2026-10-21T01:00:00+00:00",
  finished_at: "2026-10-21T01:02:00+00:00",
  stats: { applied: 2, asked: 1, rejected: 0 },
  error: null,
};

const batchDetail: MaintenanceBatchDetail = {
  batch,
  items: [
    {
      id: "mi_op1",
      batch_id: "mb_1",
      kind: "normalize",
      status: "applied",
      question: null,
      reason: null,
      error: null,
    },
  ],
  operations: [
    {
      id: "op_1",
      batch_id: "mb_1",
      item_id: "mi_op1",
      action: "normalize",
      status: "applied",
      result_id: "kn_new",
    },
  ],
  available: true,
};

// 真实 maintenance.recovery.preview_restore 的形状（operations 计划 + 计数 + warnings）
const previewPayload = {
  ok: true,
  preview: {
    ok: true,
    status: "preview",
    batch_id: "mb_1",
    operations: [
      {
        operation_id: "op_merge",
        action: "merge",
        restoreable: true,
        partial: false,
        already_restored: false,
        reasons: [],
        targets: [{ knowledge_id: "kn_a", status: "restorable" }],
        product: { id: "kn_merged", state: "current", reason: "" },
      },
      {
        operation_id: "op_normalize",
        action: "normalize",
        restoreable: false,
        partial: false,
        already_restored: false,
        reasons: ["kn_old：后来被用户修改：维护产物已不是当前链版本"],
        targets: [{ knowledge_id: "kn_old", status: "blocked" }],
        product: { id: "kn_norm", state: "superseded", reason: "维护产物已不是当前链版本" },
      },
    ],
    restoreable: false,
    restoreable_count: 1,
    partial_count: 0,
    blocked_count: 1,
    already_restored_count: 0,
    warnings: ["op_normalize：kn_old：后来被用户修改：维护产物已不是当前链版本"],
  },
};

// 真实 maintenance.recovery.restore 的形状（restored/blocked 是数量 + 逐操作结果 + 原因）
const restoreOperationResults = [
  {
    operation_id: "op_merge",
    status: "restored",
    restored_knowledge_ids: ["kn_a"],
    blocked_knowledge_ids: [],
    reasons: [],
  },
  {
    operation_id: "op_normalize",
    status: "blocked",
    restored_knowledge_ids: [],
    blocked_knowledge_ids: ["kn_old"],
    reasons: ["kn_old：后来被用户修改：维护产物已不是当前链版本"],
  },
];
const restorePayload = {
  ok: true,
  restored: 1,
  blocked: 1,
  status: "rollback_partial",
  blocked_reasons: [
    {
      operation_id: "op_normalize",
      reasons: ["kn_old：后来被用户修改：维护产物已不是当前链版本"],
    },
  ],
  operations: restoreOperationResults,
  result: {
    ok: true,
    restored: 1,
    blocked: 1,
    status: "rollback_partial",
    blocked_reasons: [
      {
        operation_id: "op_normalize",
        reasons: ["kn_old：后来被用户修改：维护产物已不是当前链版本"],
      },
    ],
    operations: restoreOperationResults,
  },
};

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MemoryPendingPanel, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listMemoryPending.mockResolvedValue({
    items: [proposal, question],
    total: 2,
    truncated: false,
    maintenance_available: true,
    warnings: [],
  });
  mocks.apiMock.answerMemoryPending.mockResolvedValue({
    ok: true,
    status: "processed",
    item_id: "mq_1",
    item: null,
  });
  mocks.apiMock.deferMemoryPending.mockResolvedValue({
    ok: true,
    status: "deferred",
    item: null,
  });
  mocks.apiMock.verifyKnowledge.mockResolvedValue({
    ok: true,
    knowledge: { id: "kp_1", state: "active" },
  });
  mocks.apiMock.rejectKnowledge.mockResolvedValue({
    ok: true,
    knowledge: { id: "kp_1", state: "draft" },
  });
  mocks.apiMock.reviseKnowledge.mockResolvedValue({ ok: true, knowledge_id: "kp_new" });
  mocks.request.mockResolvedValue({
    ok: true,
    decision: "approve_modified",
    already_handled: false,
    knowledge: { id: "kp_new", state: "active" },
  });
  mocks.apiMock.listMaintenanceBatches.mockResolvedValue({
    batches: [batch],
    total: 1,
    truncated: false,
    available: true,
  });
  mocks.apiMock.getMaintenanceBatch.mockResolvedValue(batchDetail);
  mocks.apiMock.previewMaintenanceRestore.mockResolvedValue(previewPayload);
  mocks.apiMock.restoreMaintenance.mockResolvedValue(restorePayload);
});

describe("MemoryPendingPanel 待确认列表", () => {
  it("两类事项按 kind / handler 区分，状态与来源如实显示", async () => {
    const w = mountPanel();
    await flushPromises();
    expect(w.find(".kind-knowledge_proposal").exists()).toBe(true);
    expect(w.find(".kind-maintenance_question").exists()).toBe(true);
    expect(w.findAll(".mp-kind").map((k) => k.text())).toEqual([
      "知识待审批",
      "后台维护提问",
    ]);
    const statuses = w.findAll(".mp-status");
    expect(statuses.map((s) => s.text())).toEqual(["待审批", "待回答"]);
    expect(statuses[0].classes()).toContain("awaiting_approval");
    expect(statuses[1].classes()).toContain("awaiting_answer");
    const text = w.text();
    // 审批中的知识不得显示成「事实已证实」
    expect(text).toContain("尚未生效");
    expect(text).toContain("范围：全局（你）");
    expect(text).toContain("来源：主对话");
    expect(text).toContain("来源：后台整理");
    expect(text).toContain("处理程序：后台维护");
    expect(text).toContain("用户提到的常去的火锅店是哪一家？");
    // 两类动作集合不同
    expect(w.find(".mp-approve").exists()).toBe(true);
    expect(w.find(".mp-answer-submit").exists()).toBe(true);
    w.unmount();
  });

  it("批准 / 拒绝走既有知识 API 并刷新列表", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-approve").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.verifyKnowledge).toHaveBeenCalledWith("kp_1");
    expect(mocks.apiMock.listMemoryPending).toHaveBeenCalledTimes(2);
    await w.find(".mp-reject").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.rejectKnowledge).toHaveBeenCalledWith("kp_1");
    w.unmount();
  });

  it("修改后批准：走知识原子接口（一次请求，不先批准原文）", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-modify-open").trigger("click");
    const edit = w.find(".mp-edit");
    expect((edit.element as HTMLTextAreaElement).value).toBe("用户偏好五里关火锅");
    await edit.setValue("用户其实更爱另一家火锅");
    await w.find(".mp-save-modified").trigger("click");
    await flushPromises();

    // 只走一个原子请求：提交的是用户编辑后的内容，并带回用户看到的这一版（CAS）
    expect(mocks.request).toHaveBeenCalledTimes(1);
    const [path, init] = mocks.request.mock.calls[0];
    expect(path).toBe("/api/knowledge/kp_1/approval");
    const body = JSON.parse(String(init.body));
    expect(body.decision).toBe("approve_modified");
    expect(body.content).toBe("用户其实更爱另一家火锅");
    expect(body.expected_version).toBe(1);
    expect(body.expected_content).toBe("用户偏好五里关火锅");
    // 绝不沿用「先 verify 批准原文，再 revise」的非原子流程
    expect(mocks.apiMock.verifyKnowledge).not.toHaveBeenCalled();
    expect(mocks.apiMock.reviseKnowledge).not.toHaveBeenCalled();
    expect(mocks.apiMock.listMemoryPending).toHaveBeenCalledTimes(2);
    w.unmount();
  });

  it("修改后批准失败时如实说明，且没有先批准原文", async () => {
    mocks.request.mockRejectedValueOnce(
      Object.assign(new Error("409"), { body: { error: "内容在你确认前已经变化" } }),
    );
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-modify-open").trigger("click");
    await w.find(".mp-edit").setValue("改过的内容");
    await w.find(".mp-save-modified").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("内容在你确认前已经变化");
    expect(w.text()).not.toContain("原内容已批准");
    expect(mocks.apiMock.verifyKnowledge).not.toHaveBeenCalled();
    w.unmount();
  });

  it("后台提问：提交回答调用维护 API，不在前端复制后台逻辑", async () => {
    const w = mountPanel();
    await flushPromises();
    const textarea = w.find(".mp-answer");
    const submit = w.find(".mp-answer-submit");
    expect(submit.attributes("disabled")).toBeDefined();
    await textarea.setValue("她最爱五里关火锅");
    await w.find(".mp-answer-submit").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.answerMemoryPending).toHaveBeenCalledWith(
      "mq_1",
      "她最爱五里关火锅",
    );
    expect(w.text()).toContain("回答已交给后台维护");
    w.unmount();
  });

  it("后台提问：稍后处理调用 defer（持久化）", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-defer").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.deferMemoryPending).toHaveBeenCalledWith("mq_1");
    w.unmount();
  });

  it("来源筛选按 API 返回标签匹配（后台整理可筛出）", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-source-filter").trigger("click");
    const option = w
      .findAll(".mp-source-filter .opt")
      .find((item) => item.text() === "后台整理");
    expect(option).toBeTruthy();
    await option!.trigger("click");
    await flushPromises();
    expect(w.find(".kind-maintenance_question").exists()).toBe(true);
    expect(w.find(".kind-knowledge_proposal").exists()).toBe(false);
    w.unmount();
  });

  it("类型筛选区分两类事项", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-kind-filter").trigger("click");
    const option = w
      .findAll(".mp-kind-filter .opt")
      .find((item) => item.text() === "知识待审批");
    await option!.trigger("click");
    await flushPromises();
    expect(w.find(".kind-knowledge_proposal").exists()).toBe(true);
    expect(w.find(".kind-maintenance_question").exists()).toBe(false);
    w.unmount();
  });

  it("加载失败显示原因和重试（不伪装成空集合）", async () => {
    mocks.apiMock.listMemoryPending.mockRejectedValueOnce(new Error("后端 503"));
    const w = mountPanel();
    await flushPromises();
    expect(w.find(".mp-error").text()).toContain("加载待确认事项失败");
    expect(w.find(".mp-error").text()).toContain("后端 503");
    await w.find(".mp-error button").trigger("click");
    await flushPromises();
    expect(w.find(".kind-knowledge_proposal").exists()).toBe(true);
    w.unmount();
  });

  it("空状态如实显示「暂无待确认事项」", async () => {
    mocks.apiMock.listMemoryPending.mockResolvedValueOnce({
      items: [],
      total: 0,
      truncated: false,
      maintenance_available: true,
      warnings: [],
    });
    const w = mountPanel();
    await flushPromises();
    expect(w.text()).toContain("暂无待确认事项");
    w.unmount();
  });

  it("维护事项存储不可用时如实说明（迁移 47 未应用）", async () => {
    mocks.apiMock.listMemoryPending.mockResolvedValueOnce({
      items: [proposal],
      total: 1,
      truncated: false,
      maintenance_available: false,
      warnings: ["维护事项存储不可用（迁移 47 未应用）：当前只显示知识待审批。"],
    });
    const w = mountPanel();
    await flushPromises();
    expect(w.find(".mp-warn").text()).toContain("迁移 47 未应用");
    w.unmount();
  });
});

describe("MemoryPendingPanel 维护记录", () => {
  it("批次列表 / 详情 / 恢复预览 / 恢复结果真实渲染", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-tab-records").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.listMaintenanceBatches).toHaveBeenCalledTimes(1);
    const listText = w.text();
    expect(listText).toContain("已完成");
    expect(listText).toContain("变化数达到阈值");
    expect(listText).toContain("生效 2");

    await w.find(".mp-batch-open").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.getMaintenanceBatch).toHaveBeenCalledWith("mb_1");
    expect(w.text()).toContain("normalize");
    expect(w.text()).toContain("结果 kn_new");

    await w.find(".mp-preview").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.previewMaintenanceRestore).toHaveBeenCalledWith({
      batch_id: "mb_1",
    });
    const previewText = w.text();
    // 真实 recovery 计划：逐操作状态 + 计数 + 不可恢复原因
    expect(previewText).toContain("可恢复 1");
    expect(previewText).toContain("不可恢复 1");
    expect(previewText).toContain("不可安全恢复");
    expect(previewText).toContain("后来被用户修改");
    expect(previewText).toContain("op_merge");
    expect(previewText).toContain("op_normalize");

    await w.find(".mp-restore").trigger("click");
    await flushPromises();
    const confirm = w
      .findAll(".qio-confirm button")
      .find((button) => button.text() === "整批撤回");
    expect(confirm).toBeTruthy();
    await confirm!.trigger("click");
    await flushPromises();
    expect(mocks.apiMock.restoreMaintenance).toHaveBeenCalledWith({
      batch_id: "mb_1",
      confirm: true,
    });
    const resultText = w.text();
    expect(resultText).toContain("恢复结果");
    // 真实 recovery 部分失败时 ok=true + status=rollback_partial：不能显示成「已完成」
    expect(resultText).toContain("恢复未全部完成");
    expect(resultText).toContain("已恢复 1 项");
    expect(resultText).toContain("无法安全恢复 1 项");
    expect(resultText).toContain("rollback_partial");
    expect(resultText).toContain("op_normalize");
    w.unmount();
  });

  it("恢复失败如实显示错误（不静默）", async () => {
    mocks.apiMock.restoreMaintenance.mockRejectedValueOnce(new Error("409 已被用户修改"));
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-tab-records").trigger("click");
    await flushPromises();
    await w.find(".mp-batch-open").trigger("click");
    await flushPromises();
    await w.find(".mp-restore").trigger("click");
    await flushPromises();
    const confirm = w
      .findAll(".qio-confirm button")
      .find((button) => button.text() === "整批撤回");
    await confirm!.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("恢复没有完成");
    expect(w.text()).toContain("409 已被用户修改");
    w.unmount();
  });

  it("维护记录不可用 / 加载失败都有真实说明与重试", async () => {
    mocks.apiMock.listMaintenanceBatches.mockResolvedValueOnce({
      batches: [],
      total: 0,
      truncated: false,
      available: false,
      warnings: ["维护批次存储不可用（迁移 47 未应用）。"],
    });
    const w = mountPanel();
    await flushPromises();
    await w.find(".mp-tab-records").trigger("click");
    await flushPromises();
    expect(w.find(".mp-warn").text()).toContain("不可用");

    mocks.apiMock.listMaintenanceBatches.mockRejectedValueOnce(
      new Error("连接被拒绝"),
    );
    await w.find(".mp-batches-refresh").trigger("click");
    await flushPromises();
    expect(w.find(".mp-error").text()).toContain("加载维护记录失败");
    w.unmount();
  });
});
