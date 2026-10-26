/**
 * 统一审批弹窗：结果与历史视图（A / F02、F03、F05、F06）组件级测试。
 *
 * 覆盖：0 待办可打开结果、历史三态（加载/失败+重试/真空）、恢复失败可重试、
 * approved/executing 不显示完成、批量逐项展示、窄窗队列标签可区分、
 * 状态/期限不重复、预算可编辑/只读与「未修改不发 overrides」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const mocks = vi.hoisted(() => ({
  getApprovalHistory: vi.fn(),
  getPendingApprovals: vi.fn(),
  respondApproval: vi.fn(),
}));

vi.mock("../../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/approvalLifecycle")>();
  return {
    ...actual,
    getApprovalHistory: mocks.getApprovalHistory,
    getPendingApprovals: mocks.getPendingApprovals,
  };
});
vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return {
    ...actual,
    api: { ...(actual as { api?: object }).api, respondApproval: mocks.respondApproval },
  };
});

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useApprovalResultsStore } from "../../../stores/approvalResults";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return {
    pinia,
    approvals: useApprovalsStore(),
    results: useApprovalResultsStore(),
    unified: useUnifiedApprovalsStore(),
  };
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
  mocks.getPendingApprovals.mockResolvedValue([]);
  mocks.respondApproval.mockResolvedValue({ ok: true });
  localStorage.clear();
});

describe("结果与历史：0 待办也能打开（F02）", () => {
  it("没有待审批时打开结果视图：标题、面板、真空态；待审批标签不可点", async () => {
    const { pinia, unified } = setup();
    unified.openResults();
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    await flushPromises();

    expect(w.find('[data-test="approval-results-panel"]').exists()).toBe(true);
    expect(w.text()).toContain("审批结果与历史");
    const queueTab = w.get('[data-test="unified-approval-tab-queue"]');
    expect(queueTab.attributes("disabled")).toBeDefined();
    expect(queueTab.text()).toContain("待审批 0");
    // 历史读取成功后才是「真的没有记录」
    expect(w.find('[data-test="approval-results-empty"]').exists()).toBe(true);
    expect(w.find('[data-test="approval-history-error"]').exists()).toBe(false);
    w.unmount();
  });

  it("历史读取中不显示空态；失败显示原因与重试，重试成功后显示记录", async () => {
    const { pinia, results, unified } = setup();
    let resolveHistory: (rows: unknown[]) => void = () => undefined;
    mocks.getApprovalHistory.mockImplementationOnce(
      () => new Promise((resolve) => {
        resolveHistory = resolve as (rows: unknown[]) => void;
      }),
    );
    unified.openResults();
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    await flushPromises();

    // 读取中：只说正在读，不说没有
    expect(w.find('[data-test="approval-history-loading"]').exists()).toBe(true);
    expect(w.find('[data-test="approval-results-empty"]').exists()).toBe(false);

    resolveHistory([]);
    await flushPromises();
    expect(w.find('[data-test="approval-results-empty"]').exists()).toBe(true);

    // 失败（保留已有记录 + 可重试）
    results.put({
      approvalId: "ap_keep",
      kind: "tool_execution",
      label: "已有结果",
      phase: "executed",
      state: "completed",
      stateLabel: "已完成",
      summary: "之前完成的操作",
      ok: true,
      detail: {},
      items: [],
      batchSummary: "",
      source: "event",
      at: new Date().toISOString(),
      updatedAt: Date.now(),
    });
    mocks.getApprovalHistory.mockRejectedValueOnce(new Error("网络断开"));
    await results.loadHistory();
    await flushPromises();
    expect(w.find('[data-test="approval-history-error"]').exists()).toBe(true);
    expect(w.text()).toContain("读取审批历史失败");
    expect(w.find('[data-test="approval-results-empty"]').exists()).toBe(false);
    expect(w.text()).toContain("之前完成的操作");

    mocks.getApprovalHistory.mockResolvedValueOnce([]);
    await w.get('[data-test="approval-history-retry"]').trigger("click");
    await flushPromises();
    expect(results.historyError).toBeNull();
    w.unmount();
  });

  it("恢复失败在结果面板显示原因与重试；重试成功后错误消失（F04）", async () => {
    const { pinia, approvals, unified } = setup();
    unified.openResults();
    approvals.recoveryError = "恢复待审批项失败：连接超时（已有待办保留；可以重试）";
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    await flushPromises();

    const alert = w.get('[data-test="approval-recovery-error"]');
    expect(alert.text()).toContain("恢复待审批项失败");
    expect(alert.text()).toContain("已有待办保留");

    await w.get('[data-test="approval-recovery-retry"]').trigger("click");
    await flushPromises();
    expect(mocks.getPendingApprovals).toHaveBeenCalled();
    expect(approvals.recoveryError).toBeNull();
    w.unmount();
  });
});

describe("结果语义：approved/executing ≠ 完成；批量逐项（F03）", () => {
  it("已批准/执行中显示「等待执行」，不写成完成", async () => {
    const { pinia, results, unified } = setup();
    results.recordEvent({ approval_id: "ap_wait", phase: "approved", approval: { kind: "computer", label: "写入文件" } });
    results.recordEvent({ approval_id: "ap_run", phase: "executing", approval: { kind: "computer", label: "执行命令" } });
    unified.openResults();
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    await flushPromises();

    const states = w.findAll('[data-test="approval-result-state"]').map((node) => node.text());
    expect(states.some((text) => text.includes("已批准（等待执行）"))).toBe(true);
    expect(states.some((text) => text.includes("执行中"))).toBe(true);
    expect(states.every((text) => !text.includes("已完成"))).toBe(true);
    expect(w.findAll('[data-test="approval-results-waiting"]').length).toBeGreaterThan(0);
    expect(w.text()).toContain("不等于已完成");
    w.unmount();
  });

  it("批量逐项：item_id/对象/状态/原因 + 整批汇总都来自后端 detail", async () => {
    const { pinia, results, unified } = setup();
    results.recordEvent({
      approval_id: "ap_batch",
      phase: "failed",
      ok: false,
      summary: "批量写回部分完成：1/2 项已写回，其余项已保留文件",
      approval: { kind: "tool_execution", label: "批量写回附件源文件" },
      detail: {
        results: [
          { item_id: "item_1", path: "C:\\work\\a.txt", ok: true, status: "completed" },
          { item_id: "item_2", path: "C:\\work\\b.txt", ok: false, status: "failed", reason: "磁盘写满" },
        ],
      },
    });
    unified.openResults();
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    await flushPromises();

    expect(w.text()).toContain("批量写回附件源文件");
    expect(w.get('[data-test="approval-result-batch-summary"]').text()).toContain("部分成功");
    const items = w.findAll('[data-test="approval-result-item"]');
    expect(items).toHaveLength(2);
    expect(items[0].attributes("data-item-id")).toBe("item_1");
    expect(items[0].text()).toContain("成功");
    expect(items[1].attributes("data-item-id")).toBe("item_2");
    expect(items[1].text()).toContain("失败");
    expect(items[1].text()).toContain("磁盘写满");
    w.unmount();
  });
});

describe("窄窗队列（F05）：标签换行可区分、状态与期限不重复", () => {
  function seedThree() {
    const { pinia, approvals, unified } = setup();
    const payloads = [
      { description: "覆盖写入 C:\\work\\很长的报告文件名.txt" },
      { description: "删除旧缓存目录" },
      { description: "运行构建命令 npm run build" },
    ];
    payloads.forEach((payload, index) => {
      approvals.enqueue("ap_" + index, "tool_execution", payload, {
        autoOpen: false,
        meta: { state: "waiting", expiresAt: "2026-10-26T10:10:00+08:00" },
      });
    });
    unified.open("tool:ap_0");
    return { pinia, approvals, unified };
  }

  it("三条标签文案互不相同，并带完整 aria（不是三个相同「权限」）", () => {
    const { pinia } = seedThree();
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    const chips = w.findAll('[data-test="unified-approval-chip"]');
    expect(chips).toHaveLength(3);
    const texts = chips.map((chip) => chip.text());
    expect(new Set(texts).size).toBe(3);
    expect(texts[0]).toContain("覆盖写入");
    expect(texts[1]).toContain("删除旧缓存");
    expect(texts[2]).toContain("运行构建命令");
    for (const chip of chips) {
      expect(String(chip.attributes("aria-label"))).toContain("权限：");
    }
    // 队列容器是换行布局（不是只藏滚动条）：480px 下第 3 条不能被裁切
    const source = readFileSync(
      resolve(process.cwd(), "src/components/approval/UnifiedApprovalModal.vue"),
      "utf8",
    );
    const queueRule = source.slice(source.indexOf(".ua-queue {"), source.indexOf(".ua-queue {") + 260);
    expect(queueRule).toContain("flex-wrap: wrap");
    expect(queueRule).not.toContain("overflow-x: auto");
    w.unmount();
  });

  it("三条共享前缀的申请：可见文本与读屏文案都带各自对象身份（截断后仍可区分，不用原生 title）", () => {
    const { pinia, approvals, unified } = setup();
    for (const version of [1, 2, 3]) {
      const name = "发布材料-第" + version + "版.md";
      approvals.enqueue(
        "ap_prefix_" + version,
        "tool_execution",
        {
          description: "修改发布材料：第 1 章 摘要（D:\\work\\发布材料\\" + name + "）",
          version,
          arguments: { path: "D:\\work\\发布材料\\" + name },
        },
        { autoOpen: false, meta: { state: "waiting" } },
      );
    }
    unified.open("tool:ap_prefix_1");
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });

    const chips = w.findAll('[data-test="unified-approval-chip"]');
    expect(chips).toHaveLength(3);
    const visible = chips.map((chip) => chip.text());
    const arias = chips.map((chip) => String(chip.attributes("aria-label")));
    // 原生 title 已按硬约束移除：完整可区分文案只走 aria-label（读屏/键盘可达）
    for (const chip of chips) expect(chip.attributes("title")).toBeUndefined();

    // 可见文本：对象身份在前，三条互不相同
    expect(new Set(visible).size).toBe(3);
    [1, 2, 3].forEach((version, index) => {
      expect(visible[index], "可见文本必须带各自对象身份").toContain("第" + version + "版");
    });
    // aria：完整未截断的对象身份 + 操作摘要，三者互不相同
    expect(new Set(arias).size).toBe(3);
    [1, 2, 3].forEach((version, index) => {
      const name = "发布材料-第" + version + "版.md";
      expect(arias[index]).toContain(name);
      expect(arias[index]).toContain("修改发布材料：第 1 章 摘要");
    });
    w.unmount();
  });

  it("状态/期限只在一处（弹窗状态行），事实框不再重复", () => {
    const { pinia } = seedThree();
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    expect(w.findAll('[data-test="unified-approval-state"]')).toHaveLength(1);
    expect(w.find('[data-test="unified-approval-expires"]').exists()).toBe(true);
    expect(w.find('[data-test="approval-lifecycle-state"]').exists()).toBe(false);
    expect(w.find('[data-test="approval-lifecycle"]').text()).not.toContain("过期时间");
    w.unmount();
  });
});

describe("子智能体预算（F06）", () => {
  function legacySubagentCreate() {
    const { pinia, approvals, unified } = setup();
    approvals.enqueue(
      "ap_create",
      "tool_create",
      {
        description: "创建子智能体工具 deep_researcher",
        tool_type: "subagent",
        subagent_budget: { max_iterations: 5, max_tokens: 100000, output_limit_chars: 2000 },
      },
      { autoOpen: false },
    );
    unified.open("tool:ap_create");
    return { pinia, approvals, unified };
  }

  it("旧 tool_create 子智能体路径：可编辑；未修改时不发 overrides", async () => {
    const { pinia } = legacySubagentCreate();
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    expect(w.find('[data-test="approval-budget-editable"]').exists()).toBe(true);
    expect(w.find('[data-test="approval-budget-readonly"]').exists()).toBe(false);

    await w.get('[data-test="unified-approval-allow"]').trigger("click");
    await flushPromises();
    const call = mocks.respondApproval.mock.calls[0];
    expect(call[0]).toBe("ap_create");
    expect(call[1]).toBe("approved");
    expect(call[2]).toBeUndefined();
    w.unmount();
  });

  it("用户改了预算才把批准值随 overrides 发出（执行器会用这组值）", async () => {
    const { pinia } = legacySubagentCreate();
    const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
    // 第一组 QNumber 是「最大迭代」：点 + 一次 → 5 → 6
    await w.get('[data-test="approval-budget-editable"] .q-number .step.up').trigger("click");
    await w.get('[data-test="unified-approval-allow"]').trigger("click");
    await flushPromises();
    const call = mocks.respondApproval.mock.calls[0];
    expect(call[2]).toEqual({
      subagent_budget: { max_iterations: 6, max_tokens: 100000, output_limit_chars: 2000 },
    });
    w.unmount();
  });

  it("已批准 / 执行中的生命周期记录：预算只读，不再给可编辑控件", async () => {
    for (const state of ["approved", "executing", "completed"]) {
      const { pinia, approvals, unified } = setup();
      approvals.enqueue(
        "ap_life_" + state,
        "tool_create",
        {
          description: "创建子智能体工具",
          tool_type: "subagent",
          subagent_budget: { max_iterations: 3, max_tokens: 50000, output_limit_chars: 1000 },
        },
        { autoOpen: false, meta: { state: state as "approved" } },
      );
      unified.open("tool:ap_life_" + state);
      const w = mount(UnifiedApprovalModal, { global: { plugins: [pinia] } });
      expect(w.find('[data-test="approval-budget-editable"]').exists()).toBe(false);
      expect(w.find('[data-test="approval-budget-readonly"]').exists()).toBe(true);
      expect(w.text()).toContain("不支持在这里修改执行预算");
      w.unmount();
    }
  });
});
