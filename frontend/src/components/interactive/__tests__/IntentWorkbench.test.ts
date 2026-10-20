import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import IntentWorkbench from "../IntentWorkbench.vue";

const mocks = vi.hoisted(() => ({
  api: {
    fetchIntentViews: vi.fn(),
    confirmSingleIntent: vi.fn(),
    batchConfirmIntents: vi.fn(),
    fetchWaiting: vi.fn(),
    fetchLedger: vi.fn(),
    fetchIntentResults: vi.fn(),
    fetchRecoveryPreview: vi.fn(),
    recoverIntent: vi.fn(),
  },
}));

vi.mock("../../../services/interactive", () => mocks.api);
vi.mock("../../../stores/interactive", () => ({
  useInteractiveStore: () => ({ boardId: "board_1" }),
}));

function view(overrides: Record<string, unknown> = {}) {
  return {
    intent_id: "in_1",
    board_id: "board_1",
    client_ref: "c_in_1",
    version: 1,
    status: "proposed",
    title: "整理采访记录",
    goal: "把采访记录整理成清单",
    expected: "得到可读的清单",
    basis: [{ card_id: "card_1", version: 1, role: "source" }],
    scope: {
      read_card_ids: ["card_1"],
      write_card_ids: [],
      allowed_actions: ["create_card"],
      max_new_cards: 2,
    },
    depends_on: [],
    conflicts_with: [],
    authorization: { status: "valid" },
    authorization_valid: true,
    operations_summary: { applied: 0, running: 0, pending: 0, failed: 0, conflict: 0 },
    created_at: "2026-10-21T00:00:00+00:00",
    updated_at: "2026-10-21T00:00:00+00:00",
    ...overrides,
  };
}

const longContent = [
  "## 整理结果",
  "",
  "| 项 | 值 |",
  "| --- | --- |",
  "| 主题 | 访谈 |",
  "",
  "代码块：",
  "",
  "    print(1)",
  "",
  "结尾标记 TAIL-MARKER-完整正文",
  "长内容段 ".repeat(300),
].join("\n");

const baseViews = [
  view({ intent_id: "in_1", status: "proposed", title: "整理采访记录" }),
  view({ intent_id: "in_2", status: "awaiting_confirmation", title: "归类材料" }),
  view({ intent_id: "in_3", status: "running", title: "生成摘要" }),
  view({ intent_id: "in_4", status: "partially_completed", title: "合并表格", version: 2 }),
];

function mountPanel() {
  return mount(IntentWorkbench, { attachTo: document.body });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.api.fetchIntentViews.mockResolvedValue({
    available: true,
    source: "service",
    board_id: "board_1",
    intents: baseViews.map((item) => ({ ...item })),
    batch_confirm: { min: 2, eligible_ids: ["in_1", "in_2"], available: true },
  });
  mocks.api.confirmSingleIntent.mockResolvedValue({
    results: [{ intent_id: "in_1", ok: true, status: "approved" }],
  });
  mocks.api.batchConfirmIntents.mockResolvedValue({
    batch: true,
    results: [
      { intent_id: "in_1", ok: true, status: "approved" },
      { intent_id: "in_2", ok: true, status: "approved" },
    ],
  });
  mocks.api.fetchWaiting.mockResolvedValue({
    available: true,
    waiting: {
      board_id: "board_1",
      submission_id: "sub_1",
      question: "你希望整理成表格还是清单？",
    },
  });
  mocks.api.fetchLedger.mockResolvedValue({
    available: true,
    counts: { applied: 2, running: 1, pending: 0, failed: 0, conflict: 0 },
    operations: [
      {
        operation_id: "op_1",
        intent_id: "in_3",
        operation_key: "k1",
        action: "create_card",
        status: "applied",
      },
    ],
  });
  mocks.api.fetchIntentResults.mockResolvedValue({
    available: true,
    results: [
      {
        result_id: "res_long",
        card_id: "card_result_1",
        intent_id: "in_3",
        title: "整理后的采访记录",
        content: longContent,
      },
      {
        result_id: "res_short",
        card_id: "card_result_2",
        intent_id: "in_3",
        title: "短结果",
        content: "短正文结果",
      },
      { result_id: "res_empty", card_id: "card_result_3", title: "空正文", content: "   " },
    ],
  });
  mocks.api.fetchRecoveryPreview.mockResolvedValue({
    available: true,
    preview: {
      intent_id: "in_4",
      status: "partially_completed",
      can_resume: true,
      reasons: [],
      unfinished_operations: ["op_3"],
      unfinished_dependencies: [],
    },
  });
  mocks.api.recoverIntent.mockResolvedValue({ ok: true, status: "approved" });
});

describe("IntentWorkbench 意图托盘与批量确认门槛", () => {
  it("渲染意图目标 / 预期 / 依据 / 范围 / 状态 / 版本（真实状态不冒充完成）", async () => {
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    const cards = w.findAll('[data-im="iw-intent"]');
    expect(cards).toHaveLength(4);
    const text = w.text();
    expect(text).toContain("整理采访记录");
    expect(text).toContain("目标");
    expect(text).toContain("预期");
    expect(text).toContain("card_1 · v1");
    expect(text).toContain("读取 1 张卡");
    expect(text).toContain("部分完成");
    expect(text).toContain("执行中");
    expect(cards[0].find('[data-im="iw-version"]').text()).toContain("v1");
    // 未确认状态可确认；执行中的不可确认（没有确认按钮）
    const runningCard = w.find('[data-intent-id="in_3"]');
    expect(runningCard.find('[data-im="iw-confirm"]').exists()).toBe(false);
    w.unmount();
  });

  it("批量确认门槛两条：只选一条时按钮禁用并说明「单条请用单项确认」", async () => {
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    const batchButton = w.find('[data-im="iw-batch-confirm"]');
    expect(batchButton.attributes("disabled")).toBeDefined();
    const checkboxes = w.findAll('[data-im="iw-select"]');
    expect(checkboxes).toHaveLength(2);
    await checkboxes[0].setValue(true);
    await flushPromises();
    expect(batchButton.attributes("disabled")).toBeDefined();
    const hint = w.find('[data-im="iw-batch-hint"]').text();
    expect(hint).toContain("至少 2 条");
    expect(hint).toContain("单条");
    expect(w.find('[data-im="iw-batch-summary"]').text()).toContain("已选 1");
    expect(mocks.api.batchConfirmIntents).not.toHaveBeenCalled();
    w.unmount();
  });

  it("选够两条即可同批确认：调用批量接口并如实说明「已确认不等于已完成」", async () => {
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    const checkboxes = w.findAll('[data-im="iw-select"]');
    await checkboxes[0].setValue(true);
    await checkboxes[1].setValue(true);
    await flushPromises();
    const batchButton = w.find('[data-im="iw-batch-confirm"]');
    expect(batchButton.attributes("disabled")).toBeUndefined();
    await batchButton.trigger("click");
    await flushPromises();
    await flushPromises();
    expect(mocks.api.batchConfirmIntents).toHaveBeenCalledWith("board_1", ["in_1", "in_2"]);
    const notice = w.find('[data-im="iw-notice"]').text();
    expect(notice).toContain("同批确认结果");
    expect(notice).toContain("已确认（等待执行）");
    expect(notice).toContain("已确认不等于已完成");
    w.unmount();
  });

  it("单条确认走单项接口（不冒充批量）", async () => {
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    await w.find('[data-intent-id="in_1"] [data-im="iw-confirm"]').trigger("click");
    await flushPromises();
    expect(mocks.api.confirmSingleIntent).toHaveBeenCalledWith("in_1", "board_1");
    w.unmount();
  });
});

describe("IntentWorkbench 等待 / 账本 / 局部失败 / 恢复", () => {
  it("等待事项与账本摘要如实渲染（只有 applied 算完成）", async () => {
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    expect(w.find('[data-im="iw-waiting"]').text()).toContain("你希望整理成表格还是清单");
    const ledger = w.find('[data-im="iw-ledger"]');
    expect(ledger.text()).toContain("已完成操作");
    expect(ledger.text()).toContain("执行中");
    expect(w.find('[data-im="iw-count:applied"]').text()).toContain("2");
    w.unmount();
  });

  it("等待 / 账本不可用时如实说明，不伪造数据", async () => {
    mocks.api.fetchWaiting.mockResolvedValue({
      available: false,
      waiting: null,
      reason: "等待持久化尚未接入",
    });
    mocks.api.fetchLedger.mockResolvedValue({
      available: false,
      reason: "权威操作账本不可用（迁移 50 未应用）",
    });
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    expect(w.find('[data-im="iw-waiting-unavailable"]').text()).toContain("等待持久化尚未接入");
    expect(w.find('[data-im="iw-ledger-unavailable"]').text()).toContain("迁移 50");
    w.unmount();
  });

  it("失败 / 部分完成 / 旧授权失效如实显示，并提供恢复入口", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [
        view({ intent_id: "in_failed", status: "failed", title: "失败任务" }),
        view({
          intent_id: "in_partial",
          status: "partially_completed",
          title: "部分完成任务",
          version: 3,
          authorization_valid: false,
        }),
      ],
      batch_confirm: { min: 2, eligible_ids: [], available: false },
    });
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    const text = w.text();
    expect(text).toContain("失败");
    expect(text).toContain("部分完成");
    expect(text).toContain("旧授权已失效");
    // 授权无效的意图不提供恢复入口（不越过授权检查）
    expect(w.find('[data-intent-id="in_partial"] [data-im="iw-recover-preview"]').exists()).toBe(false);
    expect(w.find('[data-intent-id="in_failed"] [data-im="iw-recover-preview"]').exists()).toBe(true);
    w.unmount();
  });

  it("恢复预览与恢复：只有 can_resume 才可确认，结果如实说明「不会自动执行」", async () => {
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    const partialCard = w.find('[data-intent-id="in_4"]');
    await partialCard.find('[data-im="iw-recover-preview"]').trigger("click");
    await flushPromises();
    const preview = w.find('[data-im="iw-recovery-preview"]');
    expect(preview.exists()).toBe(true);
    expect(preview.text()).toContain("未完成操作");
    expect(preview.text()).toContain("op_3");
    await preview.find('[data-im="iw-recover-confirm"]').trigger("click");
    await flushPromises();
    expect(mocks.api.recoverIntent).toHaveBeenCalledWith("in_4", "board_1", 2);
    const notice = w.find('[data-im="iw-notice"]').text();
    expect(notice).toContain("恢复已受理");
    expect(notice).toContain("不会自动执行操作");
    w.unmount();
  });

  it("恢复预览不可续作时确认按钮禁用（不越过真实条件）", async () => {
    mocks.api.fetchRecoveryPreview.mockResolvedValue({
      available: true,
      preview: {
        intent_id: "in_4",
        status: "partially_completed",
        can_resume: false,
        reasons: ["旧授权已失效，需要重新确认"],
        unfinished_operations: [],
        unfinished_dependencies: [],
      },
    });
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    await w.find('[data-intent-id="in_4"] [data-im="iw-recover-preview"]').trigger("click");
    await flushPromises();
    const preview = w.find('[data-im="iw-recovery-preview"]');
    expect(preview.text()).toContain("旧授权已失效");
    expect(preview.find('[data-im="iw-recover-confirm"]').attributes("disabled")).toBeDefined();
    w.unmount();
  });
});

describe("IntentWorkbench 结果卡 Markdown", () => {
  it("完整正文支持表格 / 代码块，长卡片默认折叠并可展开，空正文不显示假卡", async () => {
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    const cards = w.findAll('[data-im="result-card"]');
    // 空正文的 res_empty 不渲染
    expect(cards).toHaveLength(2);
    const longCard = w.find('[data-result-id="res_long"]');
    expect(longCard.find("table").exists()).toBe(true);
    expect(longCard.text()).toContain("主题");
    expect(longCard.text()).toContain("print(1)");
    // 正文没有被截断：结尾标记仍在 DOM（折叠只是显示高度）
    expect(longCard.text()).toContain("TAIL-MARKER-完整正文");
    expect(longCard.find('[data-im="result-body:res_long"]').classes()).toContain("clipped");
    await longCard.find('[data-im="result-toggle"]').trigger("click");
    await flushPromises();
    expect(longCard.find('[data-im="result-body:res_long"]').classes()).not.toContain("clipped");
    // 短结果不折叠、没有展开入口
    expect(w.find('[data-result-id="res_short"] [data-im="result-toggle"]').exists()).toBe(false);
    w.unmount();
  });

  it("结果卡不可用 / 读取失败都如实显示", async () => {
    mocks.api.fetchIntentResults.mockResolvedValue({
      available: false,
      results: [],
      reason: "结果引用不可用（B 的 intent_service 未接入）",
    });
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    expect(w.find('[data-im="iw-results-unavailable"]').text()).toContain("结果引用不可用");
    w.unmount();
  });
});

describe("IntentWorkbench 错误与旧数据兼容", () => {
  it("意图读取失败显示原因与重试", async () => {
    mocks.api.fetchIntentViews.mockRejectedValueOnce(new Error("后端 503"));
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    const error = w.find('[data-im="iw-load-error"]');
    expect(error.text()).toContain("读取意图失败");
    expect(error.text()).toContain("后端 503");
    await error.find("button").trigger("click");
    await flushPromises();
    await flushPromises();
    expect(w.findAll('[data-im="iw-intent"]')).toHaveLength(4);
    w.unmount();
  });

  it("旧数据只读兼容时明确标注来源，不冒充第三阶段视图", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "legacy",
      intents: [view({ intent_id: "old_1", raw_legacy_status: "pending" })],
      batch_confirm: { min: 2, eligible_ids: ["old_1"], available: false },
    });
    const w = mountPanel();
    await flushPromises();
    await flushPromises();
    expect(w.find('[data-im="iw-source"]').text()).toContain("旧数据只读兼容");
    expect(w.text()).toContain("旧状态 pending");
    w.unmount();
  });

  it("收起按钮向父组件发 close 事件", async () => {
    const w = mountPanel();
    await flushPromises();
    await w.find('[data-im="iw-close"]').trigger("click");
    expect(w.emitted("close")).toHaveLength(1);
    w.unmount();
  });
});
