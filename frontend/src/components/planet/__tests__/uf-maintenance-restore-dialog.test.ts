/**
 * D · 整批撤回记忆整理：中央弹窗（摘要 / 受影响条目 / 可恢复性 / 原因 / 前后内容 / 确认取消）。
 * 部分不可恢复时必须如实说明实际范围；取消不发请求；失败留在弹窗可重试。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import MemoryPendingPanel from "../MemoryPendingPanel.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

const mocks = vi.hoisted(() => ({
  apiMock: {
    listMemoryPending: vi.fn(),
    listMaintenanceBatches: vi.fn(),
    getMaintenanceBatch: vi.fn(),
    previewMaintenanceRestore: vi.fn(),
    restoreMaintenance: vi.fn(),
    verifyKnowledge: vi.fn(),
    rejectKnowledge: vi.fn(),
    answerMemoryPending: vi.fn(),
    deferMemoryPending: vi.fn(),
  },
}));

vi.mock("../../../services/api", () => ({ api: mocks.apiMock }));

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
    },
    {
      id: "op_normalize",
      batch_id: "mb_1",
      action: "normalize",
      status: "applied",
      before: [{ content: "整理前的知识 B" }],
      after: [{ content: "整理后的知识 B2" }],
      result_id: "kn_b2",
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
  return mount(MemoryPendingPanel, { global: { plugins: [pinia] } });
}

async function openDialog(w: ReturnType<typeof mountPanel>) {
  await w.find(".mp-tab-records").trigger("click");
  await flushPromises();
  await w.find(".mp-batch-open").trigger("click");
  await flushPromises();
  await w.find(".mp-restore").trigger("click");
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.apiMock.listMemoryPending.mockResolvedValue({
    items: [],
    total: 0,
    truncated: false,
    maintenance_available: true,
    warnings: [],
  });
  mocks.apiMock.listMaintenanceBatches.mockResolvedValue({
    batches: [batch],
    total: 1,
    truncated: false,
    available: true,
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

describe("整批撤回中央弹窗", () => {
  it("展示摘要、受影响条目、部分不可恢复原因与恢复约束", async () => {
    const w = mountPanel();
    await flushPromises();
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
    // 恢复约束：不改写历史、不覆盖后续修改
    expect(dialog.text()).toContain("不删除历史");
    expect(dialog.text()).toContain("不会被覆盖");
    // 弹窗登记了仲裁器（kind=recovery），与其它中央弹窗互斥
    const arbiter = useModalArbiterStore();
    expect(arbiter.isOpen("memory-batch-restore:mb_1")).toBe(true);
    w.unmount();
  });

  it("可展开真实的前后快照内容；没有快照的条目如实说明", async () => {
    const w = mountPanel();
    await flushPromises();
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

  it("取消不发撤回请求；确认才调用 restoreMaintenance(confirm=true)", async () => {
    const w = mountPanel();
    await flushPromises();
    await openDialog(w);
    await w.find(".restore-dialog__cancel").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.restoreMaintenance).not.toHaveBeenCalled();
    expect(w.find(".restore-dialog").exists()).toBe(false);

    await w.find(".mp-restore").trigger("click");
    await flushPromises();
    await w.find(".restore-dialog__confirm").trigger("click");
    await flushPromises();
    expect(mocks.apiMock.restoreMaintenance).toHaveBeenCalledWith({
      batch_id: "mb_1",
      confirm: true,
    });
    w.unmount();
  });

  it("撤回失败留在弹窗里显示真实原因并可重试", async () => {
    mocks.apiMock.restoreMaintenance.mockRejectedValueOnce(new Error("409 已被用户修改"));
    const w = mountPanel();
    await flushPromises();
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
