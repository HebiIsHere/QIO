import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import MaintenanceRecords from "../MaintenanceRecords.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

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

const batch = {
  id: "mb_1",
  trigger: "change_count",
  status: "completed",
  model: "fake-model",
  created_at: "2026-10-21T01:00:00+00:00",
  finished_at: "2026-10-21T01:03:00+00:00",
  stats: { applied: 1 },
  error: null,
};

const detail = {
  batch,
  items: [
    {
      id: "mi_1",
      batch_id: "mb_1",
      kind: "ask",
      status: "processed",
      question: "你最近还养着那只鹅吗？",
      reason: "需要确认现状",
      error: null,
      before: [{ content: "旧问题快照" }],
      after: [{ content: "新问题快照" }],
      updated_at: "2026-10-21T01:02:00+00:00",
    },
  ],
  operations: [
    {
      id: "op_merge",
      batch_id: "mb_1",
      action: "merge",
      status: "applied",
      target_ids: [{ knowledge_id: "kn_a" }],
      result_id: "kn_z",
      before: [{ content: "合并前的知识 A" }],
      after: [{ content: "合并后的知识 Z" }],
      created_at: "2026-10-21T01:01:00+00:00",
    },
  ],
  available: true,
};

const preview = {
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
        product: { id: "kn_z", state: "current", reason: "" },
      },
    ],
    restoreable_count: 1,
    partial_count: 0,
    blocked_count: 0,
    already_restored_count: 0,
    warnings: [],
  },
};

function mountPanel(props: Record<string, unknown> = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MaintenanceRecords, { props, global: { plugins: [pinia] } });
}

function detailVisible(w: ReturnType<typeof mountPanel>): boolean {
  return (w.find('[data-record-detail]').element as HTMLElement).style.display !== "none";
}

async function openBatch(w: ReturnType<typeof mountPanel>) {
  await flushPromises();
  await w.find('[data-record-id="mb_1"]').trigger("click");
  await flushPromises();
  await nextTick();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listBatches.mockResolvedValue({
    batches: [batch],
    total: 1,
    truncated: false,
    available: true,
    warnings: [],
  });
  mocks.getBatch.mockResolvedValue(detail);
  mocks.previewRestore.mockResolvedValue(preview);
  mocks.restore.mockResolvedValue({
    ok: true,
    status: "restored",
    operations: [
      {
        operation_id: "op_merge",
        status: "restored",
        restored_knowledge_ids: ["kn_a"],
        blocked_knowledge_ids: [],
        reasons: [],
      },
    ],
  });
});

describe("MaintenanceRecords 列表 → 详情", () => {
  it("列表展示真实批次（状态/触发原因/时间/统计），点开进入详情", async () => {
    const w = mountPanel();
    await flushPromises();
    const row = w.find('[data-record-id="mb_1"]');
    expect(row.exists()).toBe(true);
    expect(row.text()).toContain("已完成");
    expect(row.text()).toContain("变化数达到阈值");
    expect(row.text()).toContain("2026-10-21T01:00:00+00:00");
    await requestOpen(w);
    expect(detailVisible(w)).toBe(true);
    const text = w.find('[data-record-detail]').text();
    expect(text).toContain("生效 1");
    expect(text).toContain("耗时 3 分钟");
    expect(text).toContain("fake-model");
  });

  it("打开详情后返回列表", async () => {
    const w = mountPanel();
    await openBatch(w);
    await w.find('[data-records-back]').trigger("click");
    await flushPromises();
    await nextTick();
    expect(detailVisible(w)).toBe(false);
    expect(w.find('[data-record-id="mb_1"]').exists()).toBe(true);
  });

  it("列表读取失败给原因与重试；不可用时不伪造数据", async () => {
    mocks.listBatches.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await flushPromises();
    expect(w.find(".mr-error").text()).toContain("offline");
    w.unmount();

    mocks.listBatches.mockResolvedValueOnce({
      batches: [],
      total: 0,
      truncated: false,
      available: false,
      warnings: ["维护记录存储不可用"],
    });
    const w2 = mountPanel();
    await flushPromises();
    expect(w2.text()).toContain("维护记录存储不可用");
    w2.unmount();
  });

  it("focusBatchId 直接打开对应批次详情", async () => {
    const w = mountPanel({ focusBatchId: "mb_1" });
    await flushPromises();
    await nextTick();
    expect(detailVisible(w)).toBe(true);
    expect(w.find('[data-record-detail]').text()).toContain("整理详情");
    w.unmount();
  });
});

async function requestOpen(w: ReturnType<typeof mountPanel>) {
  await w.find('[data-record-id="mb_1"]').trigger("click");
  await flushPromises();
}

describe("MaintenanceRecords 受影响条目：默认折叠、点开才看前后对比", () => {
  it("默认不展开差异；点开显示真实修改前后内容，再点收起", async () => {
    const w = mountPanel();
    await openBatch(w);
    const item = w.find('[data-record-item-id="op_merge"]');
    expect(item.exists()).toBe(true);
    expect(w.find('[data-record-diff]').exists()).toBe(false);
    await item.find(".mr-item-head").trigger("click");
    await nextTick();
    const diff = w.find('[data-record-diff]');
    expect(diff.exists()).toBe(true);
    expect(diff.text()).toContain("合并前的知识 A");
    expect(diff.text()).toContain("合并后的知识 Z");
    await w.find('[data-record-item-id="op_merge"] .mr-item-head').trigger("click");
    await nextTick();
    expect(w.find('[data-record-diff]').exists()).toBe(false);
    w.unmount();
  });

  it("没有快照时如实说明，而不是留空", async () => {
    mocks.getBatch.mockResolvedValueOnce({
      ...detail,
      operations: [{ ...detail.operations[0], before: [], after: [] }],
    });
    const w = mountPanel();
    await openBatch(w);
    await w.find('[data-record-item-id="op_merge"] .mr-item-head').trigger("click");
    await nextTick();
    const diff = w.find('[data-record-diff]').text();
    expect(diff).toContain("没有留下修改前快照");
    expect(diff).toContain("没有留下修改后快照");
    w.unmount();
  });
});

describe("MaintenanceRecords 撤回 / 恢复：预览与确认复用中央弹窗", () => {
  it("整批撤回：先预览影响，取消不发请求，确认才调用恢复", async () => {
    const w = mountPanel();
    await openBatch(w);
    await w.find(".mr-preview-batch").trigger("click");
    await flushPromises();
    expect(mocks.previewRestore).toHaveBeenCalledWith({ batch_id: "mb_1" });
    const dialog = w.find(".restore-dialog");
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("整批撤回这次记忆整理");
    expect(dialog.find('[data-testid="restore-summary"]').text()).toContain("可恢复 1");

    await w.find(".restore-dialog__cancel").trigger("click");
    await flushPromises();
    expect(mocks.restore).not.toHaveBeenCalled();
    expect(w.find(".restore-dialog").exists()).toBe(false);

    await w.find(".mr-preview-batch").trigger("click");
    await flushPromises();
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    expect(mocks.restore).toHaveBeenCalledWith({ batch_id: "mb_1", confirm: true });
    expect(w.find(".restore-dialog").exists()).toBe(false);
    expect(w.find(".mr-result").text()).toContain("恢复已完成");
    expect(w.find(".mr-result").text()).toContain("已恢复");
    w.unmount();
  });

  it("单条恢复：中央弹窗标题与确认文案是「这一项」，并带真实 item_id", async () => {
    const w = mountPanel();
    await openBatch(w);
    await w.find(".mr-item-restore-open").trigger("click");
    await flushPromises();
    expect(mocks.previewRestore).toHaveBeenCalledWith({ item_id: "mi_1" });
    const dialog = w.find(".restore-dialog");
    expect(dialog.text()).toContain("恢复这一项的维护修改");
    expect(dialog.find(".restore-dialog__confirm").text()).toContain("恢复这一项");
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    expect(mocks.restore).toHaveBeenCalledWith({ item_id: "mi_1", confirm: true });
    w.unmount();
  });

  it("恢复失败留在弹窗里、给出原因，不假装完成", async () => {
    mocks.restore.mockRejectedValueOnce(new Error("offline"));
    const w = mountPanel();
    await openBatch(w);
    await w.find(".mr-preview-batch").trigger("click");
    await flushPromises();
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    expect(w.find(".restore-dialog").exists()).toBe(true);
    expect(w.find(".restore-dialog").text()).toContain("恢复没有完成");
    expect(w.find(".restore-dialog").text()).toContain("offline");
    w.unmount();
  });

  it("更高优先级中央弹窗占用时不强行叠层，给出真实说明", async () => {
    const w = mountPanel();
    await openBatch(w);
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "qio-close-ask", kind: "close-ask", dismissible: true });
    await w.find(".mr-preview-batch").trigger("click");
    await flushPromises();
    expect(w.find(".restore-dialog").exists()).toBe(false);
    expect(w.text()).toContain("另一个中央弹窗正在处理");
    arbiter.reset();
    w.unmount();
  });
});
