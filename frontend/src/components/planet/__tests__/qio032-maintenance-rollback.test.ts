/**
 * #33 整批撤回结果可读化 + #35 已撤回历史态与幂等。
 *
 * 前端验证：
 * - 预览读取失败写「暂时读不到可撤回的内容」并保留重试；确认确实为空才写「没有可撤回的内容」；
 * - 结果主信息是用户可辨认的知识名称与数量，原始 id / 状态枚举 / 时间戳默认收进展开详情；
 * - 已撤回且没有可恢复项时只显示历史结果，不提供再次执行的确认入口。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MaintenanceRecords from "../MaintenanceRecords.vue";

const mocks = vi.hoisted(() => ({
  listBatches: vi.fn(),
  getBatch: vi.fn(),
  previewRestore: vi.fn(),
  restore: vi.fn(),
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listMaintenanceBatches: mocks.listBatches,
      getMaintenanceBatch: mocks.getBatch,
      previewMaintenanceRestore: mocks.previewRestore,
      restoreMaintenance: mocks.restore,
    },
  };
});

const completedBatch = {
  id: "mb_1",
  trigger: "change_count",
  status: "completed",
  model: "fake-model",
  created_at: "2026-10-21T01:00:00+00:00",
  finished_at: "2026-10-21T01:03:00+00:00",
  stats: { applied: 1 },
  error: null,
};

const operations = [
  {
    id: "op_merge",
    batch_id: "mb_1",
    action: "merge",
    status: "applied",
    target_ids: [{ knowledge_id: "kn_a" }],
    result_id: "kn_merged",
    before: [
      { id: "kn_a", content: "示例知识 A" },
      { id: "kn_b", content: "示例知识 B" },
    ],
    after: [{ id: "kn_merged", content: "示例知识合并结果" }],
    created_at: "2026-10-21T01:01:00+00:00",
  },
  {
    id: "op_normalize",
    batch_id: "mb_1",
    action: "normalize",
    status: "applied",
    target_ids: [{ knowledge_id: "kn_c" }],
    result_id: "kn_c2",
    before: [{ id: "kn_c", content: "示例知识 C" }],
    after: [{ id: "kn_c2", content: "示例知识 C 整理后" }],
    created_at: "2026-10-21T01:02:00+00:00",
  },
];

function detailOf(batch: typeof completedBatch, ops = operations) {
  return { batch, items: [], operations: ops, available: true };
}

const ONE_RESTORABLE = {
  ok: true,
  preview: {
    ok: true,
    status: "preview",
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
    ],
    restoreable_count: 1,
    partial_count: 0,
    blocked_count: 0,
    already_restored_count: 0,
    warnings: [],
  },
};

const EMPTY_PREVIEW = {
  ok: true,
  preview: {
    ok: true,
    status: "preview",
    operations: [],
    restoreable_count: 0,
    partial_count: 0,
    blocked_count: 0,
    already_restored_count: 0,
    warnings: [],
  },
};

const ALREADY_ROLLED_BACK_PREVIEW = {
  ok: true,
  preview: {
    ok: true,
    status: "preview",
    operations: [
      {
        operation_id: "op_merge",
        action: "merge",
        restoreable: false,
        partial: false,
        already_restored: false,
        reasons: ["kn_merged：维护产物状态为 revoked，已被后续操作改变"],
        targets: [{ knowledge_id: "kn_a", status: "active" }],
        product: { id: "kn_merged", state: "modified", reason: "维护产物状态为 revoked" },
      },
    ],
    restoreable_count: 0,
    partial_count: 0,
    blocked_count: 1,
    already_restored_count: 0,
    warnings: [],
  },
};

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MaintenanceRecords, { global: { plugins: [pinia] } });
}

async function openBatch(w: ReturnType<typeof mountPanel>) {
  await flushPromises();
  await w.find('[data-record-id="mb_1"]').trigger("click");
  await flushPromises();
}

async function openDialog(w: ReturnType<typeof mountPanel>) {
  await openBatch(w);
  await w.find(".mr-preview-batch").trigger("click");
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listBatches.mockResolvedValue({
    batches: [completedBatch],
    total: 1,
    truncated: false,
    available: true,
    warnings: [],
  });
  mocks.getBatch.mockResolvedValue(detailOf(completedBatch));
  mocks.previewRestore.mockResolvedValue(ONE_RESTORABLE);
  mocks.restore.mockResolvedValue({
    ok: true,
    status: "restored",
    restored: 1,
    blocked: 0,
    operations: [],
  });
});

describe("#6 整理记录读取失败文案与重试", () => {
  it("列表读取失败写「暂时读不到整理记录」并保留重试", async () => {
    mocks.listBatches.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    const error = w.find(".mr-error");
    expect(error.text()).toContain("暂时读不到整理记录");
    expect(error.text()).toContain("offline");
    expect(error.find("button").text()).toContain("重试");
    w.unmount();
  });

  it("撤回预览读取失败写「暂时读不到可撤回的内容」并保留重试，不写成没有可撤回的内容", async () => {
    mocks.previewRestore.mockRejectedValueOnce(new Error("preview offline"));
    const w = mountPanel();
    await openDialog(w);
    const dialog = w.find(".restore-dialog");
    expect(dialog.text()).toContain("暂时读不到可撤回的内容");
    expect(dialog.text()).toContain("preview offline");
    expect(dialog.text()).not.toContain("没有可撤回的内容");
    expect(dialog.find(".restore-dialog__retry-preview").exists()).toBe(true);
    w.unmount();
  });
});

describe("#35 已撤回且没有可恢复项：只显示历史结果，不提供再次执行入口", () => {
  it("服务端确认没有可撤回的内容：写「没有可撤回的内容」且隐藏确认按钮", async () => {
    mocks.previewRestore.mockResolvedValue(EMPTY_PREVIEW);
    const w = mountPanel();
    await openDialog(w);
    const dialog = w.find(".restore-dialog");
    expect(dialog.find("[data-restore-empty]").text()).toContain("没有可撤回的内容");
    expect(dialog.text()).not.toContain("暂时读不到可撤回的内容");
    expect(dialog.find(".restore-dialog__confirm").exists()).toBe(false);
    expect(dialog.attributes("data-restore-readonly")).toBe("true");
    w.unmount();
  });

  it("已撤回批次：入口改为查看撤回情况，弹窗只读历史、无确认入口", async () => {
    const rolledBackBatch = { ...completedBatch, status: "rolled_back" };
    mocks.listBatches.mockResolvedValue({
      batches: [rolledBackBatch],
      total: 1,
      truncated: false,
      available: true,
      warnings: [],
    });
    mocks.getBatch.mockResolvedValue(detailOf(rolledBackBatch));
    mocks.previewRestore.mockResolvedValue(ALREADY_ROLLED_BACK_PREVIEW);
    const w = mountPanel();
    await openBatch(w);
    const openButton = w.find(".mr-preview-batch");
    expect(openButton.text()).toContain("查看撤回情况");
    await openButton.trigger("click");
    await flushPromises();
    const dialog = w.find(".restore-dialog");
    expect(dialog.attributes("data-restore-readonly")).toBe("true");
    expect(dialog.find(".restore-dialog__confirm").exists()).toBe(false);
    expect(dialog.find("[data-restore-readonly-note]").text()).toContain("已经撤回");
    expect(dialog.text()).toContain("不可安全恢复");
    // 历史结果仍然用知识名称展示，不是裸操作 id
    expect(dialog.text()).toContain("示例知识 A");
    w.unmount();
  });
});

describe("#33 撤回结果可读化", () => {
  it("成功结果展示知识名称、已恢复数量与收起详情；原始 id/状态/时间默认收起", async () => {
    mocks.restore.mockResolvedValueOnce({
      ok: true,
      status: "rolled_back",
      restored: 1,
      blocked: 0,
      operations: [
        {
          operation_id: "op_merge",
          status: "restored",
          restored_knowledge_ids: ["kn_restored_new"],
          blocked_knowledge_ids: [],
          reasons: [],
        },
      ],
    });
    const w = mountPanel();
    await openDialog(w);
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    const result = w.find("[data-restore-result]");
    expect(result.find("[data-restore-result-label]").text()).toContain("已撤回");
    expect(result.find("[data-restore-result-counts]").text()).toContain("已恢复 1 条");
    expect(result.find("[data-restore-result-counts]").text()).toContain("未恢复 0 条");
    const entry = result.find('[data-restore-entry="op_merge"]');
    const title = entry.find("[data-restore-entry-title]").text();
    expect(title).toContain("示例知识 A");
    expect(title).not.toContain("op_merge");
    // 原始信息默认收起：details 没有 open，且只在详情里出现裸 id / 状态枚举
    const raw = entry.find("[data-restore-entry-detail]");
    expect(raw.exists()).toBe(true);
    expect(raw.attributes("open")).toBeUndefined();
    expect(raw.text()).toContain("op_merge");
    expect(raw.text()).toContain("restored");
    expect(raw.text()).toContain("2026-10-21T01:01:00+00:00");
    w.unmount();
  });

  it("部分恢复按真实结果显示：不写成完全恢复，未恢复原因用知识名称", async () => {
    mocks.restore.mockResolvedValueOnce({
      ok: true,
      status: "rollback_partial",
      restored: 1,
      blocked: 1,
      operations: [
        {
          operation_id: "op_merge",
          status: "restored",
          restored_knowledge_ids: ["kn_a_restored"],
          blocked_knowledge_ids: [],
          reasons: [],
        },
        {
          operation_id: "op_normalize",
          status: "blocked",
          restored_knowledge_ids: [],
          blocked_knowledge_ids: ["kn_c"],
          reasons: ["kn_c：后来被用户修改，拒绝覆盖"],
        },
      ],
    });
    const w = mountPanel();
    await openDialog(w);
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    const result = w.find("[data-restore-result]");
    expect(result.find("[data-restore-result-label]").text()).toContain("部分恢复");
    expect(result.text()).not.toContain("恢复已完成");
    expect(result.find("[data-restore-result-counts]").text()).toContain("已恢复 1 条");
    expect(result.find("[data-restore-result-counts]").text()).toContain("未恢复 1 条");
    const blockedEntry = result.find('[data-restore-entry="op_normalize"]');
    expect(blockedEntry.find("[data-restore-entry-title]").text()).toContain("示例知识 C");
    // 服务端原因里的裸 id 换成了知识名称
    expect(blockedEntry.text()).toContain("示例知识 C：后来被用户修改");
    w.unmount();
  });

  it("一条都没有恢复时按真实结果显示失败，不写成部分恢复", async () => {
    mocks.restore.mockResolvedValueOnce({
      ok: true,
      status: "rollback_partial",
      restored: 0,
      blocked: 1,
      operations: [
        {
          operation_id: "op_normalize",
          status: "failed",
          restored_knowledge_ids: [],
          blocked_knowledge_ids: [],
          reasons: ["写入时发生异常（已脱敏）"],
        },
      ],
    });
    const w = mountPanel();
    await openDialog(w);
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    const result = w.find("[data-restore-result]");
    expect(result.find("[data-restore-result-label]").text()).toContain("恢复失败");
    expect(result.find("[data-restore-result-label]").text()).not.toContain("部分恢复");
    expect(result.find("[data-restore-result-counts]").text()).toContain("已恢复 0 条");
    expect(result.find("[data-restore-result-note]").text()).toContain("没有任何内容被恢复");
    w.unmount();
  });

  it("重复整批撤回 already_rolled_back：如实说此前已撤回，不写成又一次恢复完成", async () => {
    mocks.restore.mockResolvedValueOnce({
      ok: true,
      status: "already_rolled_back",
      restored: 0,
      blocked: 0,
      operations: [],
    });
    const w = mountPanel();
    await openDialog(w);
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    const result = w.find("[data-restore-result]");
    expect(result.find("[data-restore-result-label]").text()).toContain("此前已撤回");
    expect(result.find("[data-restore-result-note]").text()).toContain("没有再次执行任何撤回写入");
    expect(result.text()).not.toContain("恢复已完成");
    w.unmount();
  });
});
