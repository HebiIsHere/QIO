/**
 * B · 整理记录：撤回 / 恢复走**中央弹窗**（原基线用例挂在 MemoryPendingPanel 上）。
 *
 * 集成阶段基线 MemoryPendingPanel.vue 已删除，能力迁移到 MaintenanceRecords.vue
 * （待处理视图迁到 PendingItems.vue）。本文件保留原用例的**全部断言意图**，
 * 只把挂载点与选择器换成新组件的真实结构：
 * - 弹窗展示摘要、受影响条目、部分不可恢复原因与恢复约束；
 * - 可展开真实前后快照；没有快照时如实说明；
 * - 取消不发请求，确认才调用 restoreMaintenance({batch_id, confirm:true})；
 * - 更高优先级中央弹窗占用时不强行叠层，给出真实说明（仲裁身份 memory-batch-restore:mb_1 保留）；
 * - 撤回失败留在弹窗里显示真实原因并可重试。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MaintenanceRecords from "../MaintenanceRecords.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listMaintenanceBatches: vi.fn(),
    getMaintenanceBatch: vi.fn(),
    previewMaintenanceRestore: vi.fn(),
    restoreMaintenance: vi.fn(),
  },
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return { ...actual, api: { ...actual.api, ...mocks.apiMock } };
});

const batch = {
  id: "mb_1",
  trigger: "change_count",
  status: "completed",
  model: "fake",
  created_at: "2026-10-21T01:00:00+00:00",
  finished_at: "2026-10-21T01:02:00+00:00",
  stats: { applied: 1 },
  error: null,
};

const detail = {
  batch,
  items: [],
  operations: [
    {
      id: "op_merge",
      batch_id: "mb_1",
      action: "merge",
      status: "applied",
      before: [{ content: "合并前的知识 A" }],
      after: [{ content: "合并后的知识 Z" }],
      result_id: "kn_z",
      created_at: "2026-10-21T01:00:30+00:00",
    },
    {
      id: "op_normalize",
      batch_id: "mb_1",
      action: "normalize",
      status: "applied",
      before: [{ content: "整理前的知识 B" }],
      after: [{ content: "整理后的知识 B2" }],
      result_id: "kn_b2",
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
      {
        operation_id: "op_normalize",
        action: "normalize",
        restoreable: false,
        partial: false,
        already_restored: false,
        reasons: ["kn_b2：后来被用户修改：维护产物已不是当前链版本"],
        targets: [{ knowledge_id: "kn_b", status: "blocked" }],
        product: { id: "kn_b2", state: "superseded", reason: "维护产物已不是当前链版本" },
      },
    ],
    restoreable_count: 1,
    partial_count: 0,
    blocked_count: 1,
    already_restored_count: 0,
    warnings: ["op_normalize：kn_b2：后来被用户修改"],
  },
};

function mountPanel() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(MaintenanceRecords, { global: { plugins: [pinia] } });
}

/** 列表 → 单次整理详情 → 点「整批撤回…」→ 中央弹窗（预览真实影响）。 */
async function openDialog(w: ReturnType<typeof mountPanel>) {
  await flushPromises();
  await w.find('[data-record-id="mb_1"]').trigger("click");
  await flushPromises();
  await w.find(".mr-preview-batch").trigger("click");
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listMaintenanceBatches.mockResolvedValue({
    batches: [batch],
    total: 1,
    truncated: false,
    available: true,
    warnings: [],
  });
  mocks.apiMock.getMaintenanceBatch.mockResolvedValue(detail);
  mocks.apiMock.previewMaintenanceRestore.mockResolvedValue(preview);
  mocks.apiMock.restoreMaintenance.mockResolvedValue({
    ok: true,
    restored: 1,
    blocked: 1,
    status: "rollback_partial",
  });
});

describe("整理记录的整批撤回中央弹窗", () => {
  it("展示摘要、受影响条目、部分不可恢复原因与恢复约束", async () => {
    const w = mountPanel();
    await openDialog(w);
    const dialog = w.find(".restore-dialog");
    expect(dialog.exists()).toBe(true);
    expect(dialog.attributes("role")).toBe("dialog");
    expect(dialog.text()).toContain("整批撤回这次记忆整理");
    // 摘要与部分不可恢复的实际范围
    expect(dialog.find('[data-testid="restore-summary"]').text()).toContain("可恢复 1");
    expect(dialog.find('[data-testid="restore-summary"]').text()).toContain("不可恢复 1");
    expect(dialog.text()).toContain("不可安全恢复");
    expect(dialog.text()).toContain("后来被用户修改");
    // 受影响条目逐项列出（摘要之外的真实操作记录）
    expect(dialog.text()).toContain("受影响条目（2）");
    // 恢复约束：不改写历史、不覆盖后续修改
    expect(dialog.text()).toContain("不删除历史");
    expect(dialog.text()).toContain("不会被覆盖");
    // 弹窗登记了仲裁器（kind=recovery），与其它中央弹窗互斥；身份仍是批次 id
    const arbiter = useModalArbiterStore();
    expect(arbiter.isOpen("memory-batch-restore:mb_1")).toBe(true);
    w.unmount();
  });

  it("可展开真实的前后快照内容", async () => {
    const w = mountPanel();
    await openDialog(w);
    const toggles = w.findAll(".restore-dialog__toggle");
    expect(toggles.length).toBe(2);
    await toggles[0].trigger("click");
    const diff = w.find(".restore-dialog__diff");
    expect(diff.exists()).toBe(true);
    expect(diff.text()).toContain("合并前的知识 A");
    expect(diff.text()).toContain("合并后的知识 Z");
    w.unmount();
  });

  it("没有快照的条目如实说明，不臆造内容", async () => {
    mocks.apiMock.getMaintenanceBatch.mockResolvedValueOnce({
      ...detail,
      operations: [
        detail.operations[0],
        {
          ...detail.operations[1],
          before: [{ content: "整理前的知识 B" }],
          after: [],
        },
      ],
    });
    const w = mountPanel();
    await openDialog(w);
    const toggles = w.findAll(".restore-dialog__toggle");
    expect(toggles.length).toBe(2);
    await toggles[1].trigger("click");
    const diff = w.find(".restore-dialog__diff");
    expect(diff.text()).toContain("整理前的知识 B");
    expect(diff.text()).toContain("没有留下修改后快照");
    w.unmount();
  });

  it("取消不发撤回请求；确认才调用 restoreMaintenance(confirm=true)", async () => {
    const w = mountPanel();
    await openDialog(w);
    await w.find(".restore-dialog__cancel").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.restoreMaintenance).not.toHaveBeenCalled();
    expect(w.find(".restore-dialog").exists()).toBe(false);

    await w.find(".mr-preview-batch").trigger("click");
    await flushPromises();
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.restoreMaintenance).toHaveBeenCalledWith({
      batch_id: "mb_1",
      confirm: true,
    });
    w.unmount();
  });

  it("更高优先级中央弹窗占用时不强行叠层，给出真实说明", async () => {
    const w = mountPanel();
    await flushPromises();
    const arbiter = useModalArbiterStore();
    arbiter.open({ id: "qio-close-ask", kind: "close-ask", dismissible: true });
    await openDialog(w);
    expect(w.find(".restore-dialog").exists()).toBe(false);
    expect(w.text()).toContain("另一个中央弹窗正在处理");
    arbiter.reset();
    w.unmount();
  });

  it("撤回失败留在弹窗里显示真实原因并可重试", async () => {
    mocks.apiMock.restoreMaintenance.mockRejectedValueOnce(new Error("409 已被用户修改"));
    const w = mountPanel();
    await openDialog(w);
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    const dialog = w.find(".restore-dialog");
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("恢复没有完成");
    expect(dialog.text()).toContain("409 已被用户修改");
    expect(dialog.find(".restore-dialog__confirm").text()).toContain("重试撤回");
    w.unmount();
  });
});
